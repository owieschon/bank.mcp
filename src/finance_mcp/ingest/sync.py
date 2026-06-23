#!/usr/bin/env python3
"""
sync.py — transaction sync + analysis pipeline trigger.

The single command that:
  1. Pulls fresh transactions from the bank (via plaid_bridge)
  2. Writes them to the canonical SQLite DB (via db.upsert_transactions)
  3. Optionally triggers the full analysis pipeline (finance_agent.build_digest)
  4. Saves cursor state between runs for incremental sync

Can be run manually (python3 sync.py) or scheduled via launchd/cron.
Also importable as a library: sync_and_analyze() is the function the
real-time MCP tool wraps.

DATA FLOW:
  plaid_bridge.fetch_*  →  db.upsert_transactions(txns)  →  finance_agent.build_digest
        ↓                        ↓                                 ↓
  bank/Plaid API          local store update              analysis + digest

ARCHITECTURE: deterministic Python. No model calls here. The only model
interaction is in finance_agent (narration) and only when --voice is on.
Raw transactions never enter a model prompt.
"""

import argparse
import datetime as dt
import json
import os
import sys

from finance_mcp.ingest import plaid_bridge
from finance_mcp.store import db


# ----------------------------- sync state -------------------------------------

SYNC_STATE_PATH = os.environ.get(
    "SYNC_STATE_PATH",
    os.path.expanduser("~/Downloads/sync_state.json"),
)


def load_sync_state(path=SYNC_STATE_PATH):
    """Persistent state across sync runs."""
    if not os.path.exists(path):
        return {
            "last_sync": None,
            "last_cursor": None,
            "total_synced": 0,
            "runs": 0,
        }
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"last_sync": None, "last_cursor": None, "total_synced": 0, "runs": 0}


def save_sync_state(state, path=SYNC_STATE_PATH):
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())


# Staleness: how many consecutive weekday empty syncs before we warn.
STALENESS_THRESHOLD = 3

# Date-window width for the reliable (date-window) pull. Re-pulled in full every
# run, so any transaction that posts late — even 1-2 weeks after authorization —
# reappears inside this window and is captured (idempotent upsert). 120d gives wide
# margin over realistic bank posting delays; widening is free (re-pull is idempotent).
RELIABLE_SYNC_DAYS = 120


def check_staleness(sync_state, store_path=None):
    """Check if recent syncs suggest Plaid is returning empty data incorrectly.

    Returns a list of warning strings (empty list = no concerns).
    """
    warnings = []
    consecutive = sync_state.get("consecutive_empty_syncs", 0)

    # Warn if N consecutive weekday syncs returned nothing
    if consecutive >= STALENESS_THRESHOLD:
        warnings.append(
            f"STALE? {consecutive} consecutive syncs returned zero new "
            f"transactions (threshold={STALENESS_THRESHOLD})."
        )

    # Check if last known transaction is >3 days old (Plaid-broken-but-fine case)
    sync_state.get("last_success_ts")
    last_txn_date = sync_state.get("last_txn_date")
    if last_txn_date:
        try:
            ltd = dt.date.fromisoformat(last_txn_date)
            days_since = (dt.date.today() - ltd).days
            if days_since > 3 and consecutive > 0:
                warnings.append(
                    f"Last known transaction is {days_since} days old "
                    f"({last_txn_date}) but Plaid reports no new data — "
                    f"possible Plaid connectivity issue."
                )
        except ValueError:
            pass

    return warnings


def _newest_txn_date(store_path=None):
    """Return the most recent transaction date in the DB, or None.

    `store_path` is accepted for backward-compat but ignored (DB is the source).
    """
    try:
        conn = db.connect()
        row = conn.execute("SELECT MAX(date) FROM transactions").fetchone()
        conn.close()
        return row[0] if row and row[0] else None
    except Exception:
        return None


# ----------------------------- transaction stamping ---------------------------

def _stamp_transactions(txns, owner=None, source='plaid', currency=None):
    """Stamp owner/source/currency metadata on a batch of transactions.

    These stamps flow through to the DB upsert.
    db._owner_of() / db._source_of() check the stamps first, so Items from
    multi-source config get the correct tags without relying on rawData fields.
    """
    for t in txns:
        if owner is not None:
            t["_owner"] = owner
            # Also populate rawData.account_owner for engines that read it
            raw = t.get("rawData")
            if raw and isinstance(raw, dict):
                raw.setdefault("account_owner", owner)
        t["_source"] = source
        if currency is not None:
            t["_currency"] = currency


# ----------------------------- core sync --------------------------------------

def sync_from_bank(snapshot_path=None, use_sync_api=True,
                   date_from=None, date_to=None,
                   store_path=None,
                   state_path=None,
                   cursor_path=plaid_bridge.CURSOR_PATH,
                   sync_state_path=SYNC_STATE_PATH,
                   access_token=None, owner=None, source_tag='plaid'):
    """Pull transactions and persist them to the canonical DB (db.upsert_transactions).

    Strategy:
      1. If snapshot_path is given, load from file (offline / testing).
      2. Else try incremental sync (plaid_bridge.fetch_transactions_sync).
      3. Else try date-window list (plaid_bridge.fetch_transactions_list).
      4. Else fail gracefully.

    Multi-Item support:
      access_token: per-Item Plaid access token (None = use env/Keychain default).
      owner: stamp every transaction with this owner tag (None = no stamping).
      source_tag: stamp every transaction with this source ('plaid' default).

    Returns {
        'source': 'sync'|'list'|'snapshot'|'failed',
        'added': int, 'modified': int, 'removed': int,
        'store_size': int,
        'cursor': str,
        'error': str|None,
    }
    """
    result = {
        "source": "failed",
        "added": 0, "modified": 0, "removed": 0,
        "store_size": 0,
        "cursor": None,
        "error": None,
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
    }

    txns = None

    # Strategy 1: file snapshot
    if snapshot_path:
        try:
            txns = plaid_bridge.fetch_from_snapshot(snapshot_path)
            result["source"] = "snapshot"
        except Exception as e:
            result["error"] = f"Snapshot load failed: {e}"
            return result

    # Strategy 2: incremental sync API
    if txns is None and use_sync_api:
        try:
            sync_result = plaid_bridge.fetch_transactions_sync(
                cursor_path=cursor_path,
                access_token=access_token,
            )
            # For sync API, we get added/modified/removed directly.
            # Persist all of them to the canonical DB for durable storage.
            all_txns = sync_result["added"] + sync_result["modified"]
            if all_txns or sync_result["removed"]:
                # Stamp owner/source from Item config before writing anywhere
                if owner is not None or source_tag != 'plaid':
                    _stamp_transactions(all_txns, owner=owner, source=source_tag)

                # Persist to the canonical DB (single source of truth).
                _c = db.connect()
                db.init_schema(_c)
                diff = db.upsert_transactions(_c, all_txns)
                # Explicit Plaid 'removed' ids are deletions (pending->posted
                # supersessions are already handled inside db.upsert_transactions).
                extra_removed = 0
                if sync_result["removed"]:
                    ph = ",".join("?" * len(sync_result["removed"]))
                    cur = _c.execute(
                        f"DELETE FROM transactions WHERE id IN ({ph})",
                        list(sync_result["removed"]))
                    _c.commit()
                    extra_removed = cur.rowcount
                result["source"] = "sync"
                result["added"] = diff["added"]
                result["modified"] = diff["modified"]
                result["removed"] = diff["removed"] + extra_removed
                result["cursor"] = sync_result["cursor"]
                result["store_size"] = db.count(_c)
                _c.close()

                # Persist the Plaid cursor only after the DB is durable
                plaid_bridge.save_cursor(sync_result["cursor"], cursor_path)

                # Update sync state
                ss = load_sync_state(sync_state_path)
                ss["last_sync"] = result["timestamp"]
                ss["last_success_ts"] = result["timestamp"]
                ss["last_cursor"] = result["cursor"]
                ss["total_synced"] = ss.get("total_synced", 0) + result["added"]
                ss["runs"] = ss.get("runs", 0) + 1
                ss["consecutive_empty_syncs"] = 0  # reset on data
                newest = _newest_txn_date()
                if newest:
                    ss["last_txn_date"] = newest
                save_sync_state(ss, sync_state_path)
                return result
            else:
                # Empty sync — no new data.
                # Track consecutive empties for staleness detection.
                ss = load_sync_state(sync_state_path)
                is_weekday = dt.date.today().weekday() < 5
                # Always initialize the key; only increment on weekdays (weekend
                # quiet is expected, not stale). Pre-fix it was unset on weekends.
                ss["consecutive_empty_syncs"] = (
                    ss.get("consecutive_empty_syncs", 0) + (1 if is_weekday else 0)
                )
                ss["last_sync"] = result["timestamp"]
                ss["runs"] = ss.get("runs", 0) + 1
                # Ensure last_txn_date is populated if missing
                if not ss.get("last_txn_date"):
                    newest = _newest_txn_date()
                    if newest:
                        ss["last_txn_date"] = newest
                save_sync_state(ss, sync_state_path)

                # Run staleness check
                stale_warnings = check_staleness(ss)
                if stale_warnings:
                    result["stale_warnings"] = stale_warnings
                    for w in stale_warnings:
                        print(f"[sync] WARNING: {w}", file=sys.stderr)

                _c = db.connect()
                result["store_size"] = db.count(_c)
                _c.close()
                result["source"] = "sync"
                result["cursor"] = sync_result.get("cursor")
                return result
        except plaid_bridge.BankMCPError:
            pass  # fall through to list

    # Strategy 3: date-window list
    if txns is None:
        if date_from is None:
            date_from = (dt.date.today() - dt.timedelta(days=RELIABLE_SYNC_DAYS)).isoformat()
        try:
            # Complete (paginated/bisected) pull — not a single capped call — so a
            # wide initial/backfill window returns full history, not just 500 rows.
            txns = plaid_bridge.fetch_transactions_range(
                date_from=date_from, date_to=date_to,
                access_token=access_token,
            )
            result["source"] = "list"
        except plaid_bridge.BankMCPError as e:
            result["error"] = f"All fetch methods failed: {e}"
            return result

    # Persist fetched transactions to the canonical DB.
    if txns is not None:
        # Stamp owner/source from Item config before writing anywhere
        if owner is not None or source_tag != 'plaid':
            _stamp_transactions(txns, owner=owner, source=source_tag)

        _c = db.connect()
        db.init_schema(_c)
        diff = db.upsert_transactions(_c, txns)
        result["added"] = diff["added"]
        result["modified"] = diff["modified"]
        result["removed"] = diff["removed"]
        result["cursor"] = None              # date-window pull has no cursor
        result["store_size"] = db.count(_c)
        _c.close()

        # Update sync state
        ss = load_sync_state(sync_state_path)
        ss["last_sync"] = result["timestamp"]
        ss["last_cursor"] = result["cursor"]
        ss["total_synced"] = ss.get("total_synced", 0) + result["added"]
        ss["runs"] = ss.get("runs", 0) + 1
        if result["added"] > 0:
            ss["last_success_ts"] = result["timestamp"]
            ss["consecutive_empty_syncs"] = 0
            newest = _newest_txn_date()
            if newest:
                ss["last_txn_date"] = newest
        save_sync_state(ss, sync_state_path)

    return result


# --------------------------- multi-CONNECTION sync ----------------------------
# Each bank-mcp connection = one person's bank login. connection_owners.json maps
# connectionId -> owner; each connection is pulled separately and stamped with that
# owner. When the map exists it takes precedence over the single-connection path;
# absent, the original daily sync runs unchanged.

CONNECTION_OWNERS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "connection_owners.json")


def _load_connection_owners():
    """{connectionId: owner} from connection_owners.json. Empty if unconfigured."""
    if not os.path.exists(CONNECTION_OWNERS_PATH):
        return {}
    try:
        with open(CONNECTION_OWNERS_PATH, encoding="utf-8") as f:
            m = json.load(f)
        return m if isinstance(m, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def sync_connections(date_from=None, sync_state_path=SYNC_STATE_PATH):
    """Sync EACH configured bank-mcp connection by id, stamping its owner, into
    finance.db. Date-window pull (idempotent, transport-agnostic) — no ledger, no
    per-cursor state — so adding a person (e.g. a second account holder's bank) is just
    one more entry in connection_owners.json. Returns per-connection result dicts."""
    owners = _load_connection_owners()
    if date_from is None:
        date_from = (dt.date.today() - dt.timedelta(days=RELIABLE_SYNC_DAYS)).isoformat()
    conn = db.connect()
    db.init_schema(conn)
    results, total_added = [], 0
    for connection_id, owner in owners.items():
        r = {"item_name": connection_id, "owner": owner, "source": "list",
             "added": 0, "modified": 0, "removed": 0,
             "timestamp": dt.datetime.now().isoformat(timespec="seconds")}
        try:
            txns = plaid_bridge.fetch_transactions_range(
                date_from=date_from, connection_id=connection_id)
            _stamp_transactions(txns, owner=owner, source="plaid")
            diff = db.upsert_transactions(conn, txns)
            r["added"], r["modified"], r["removed"] = (
                diff["added"], diff["modified"], diff["removed"])
            r["pulled"] = len(txns)
            total_added += diff["added"]
        except Exception as e:
            r["source"] = "failed"
            r["error"] = str(e)
            print(f"[sync] connection {connection_id} ({owner}) failed: {e}",
                  file=sys.stderr)
        r["store_size"] = db.count(conn)
        results.append(r)
    conn.close()
    # Heartbeat so the failure-email machinery still has a signal.
    ss = load_sync_state(sync_state_path)
    ss["last_sync"] = dt.datetime.now().isoformat(timespec="seconds")
    ss["runs"] = ss.get("runs", 0) + 1
    if total_added > 0:
        ss["last_success_ts"] = ss["last_sync"]
    save_sync_state(ss, sync_state_path)
    return results


# ----------------------------- multi-source sync ------------------------------

def sync_all_sources(snapshot_path=None, use_sync_api=True,
                     store_path=None,
                     state_path=None,
                     sync_state_path=SYNC_STATE_PATH):
    """Sync from all configured sources: Plaid Items.

    Each source syncs independently with its own cursor/state. Results are
    merged into the same DB. Returns list of per-source result dicts.

    When no plaid_items.json exists, falls back to the existing single-token
    behavior with all transports — zero breaking changes.
    """
    # Multi-connection mode (one bank-mcp connection per person) takes precedence.
    if _load_connection_owners():
        return sync_connections(sync_state_path=sync_state_path)

    results = []
    from_config = os.path.exists(plaid_bridge.PLAID_ITEMS_PATH)
    items = plaid_bridge.load_plaid_items()

    if not items and not from_config:
        # No items and no config file — original single-call behavior
        result = sync_from_bank(
            snapshot_path=snapshot_path,
            use_sync_api=use_sync_api,
            store_path=store_path,
            state_path=state_path,
            sync_state_path=sync_state_path,
        )
        result["item_name"] = "default"
        return [result]

    for item in items:
        cursor_path = item.get("cursor_file", plaid_bridge.CURSOR_PATH)
        item_owner = item.get("owner", "primary")

        if from_config:
            # Per-item token -> Plaid direct (a distinct Item, e.g. a second
            # account). No token (None) -> sync_from_bank uses the working
            # all-transports path, i.e. the bank-mcp subprocess fork that holds
            # the primary credentials. So the primary Item needs NO token_env; only
            # additional accounts set one. Keep exactly ONE tokenless Item, or multiple
            # would re-pull the fork's single Item. (PLAID direct creds are not in
            # env/Keychain, so a tokenless item MUST use the subprocess path.)
            access_token = plaid_bridge._resolve_access_token(item)
            result = sync_from_bank(
                snapshot_path=snapshot_path,
                use_sync_api=use_sync_api,
                store_path=store_path,
                state_path=state_path,
                cursor_path=cursor_path,
                sync_state_path=sync_state_path,
                access_token=access_token,   # None -> subprocess/all-transports path
                owner=item_owner,
                source_tag="plaid",
            )
        else:
            # Single-item fallback: no access_token → all transports (backward compat)
            result = sync_from_bank(
                snapshot_path=snapshot_path,
                use_sync_api=use_sync_api,
                store_path=store_path,
                state_path=state_path,
                cursor_path=cursor_path,
                sync_state_path=sync_state_path,
            )

        result["item_name"] = item["name"]
        results.append(result)

    return results


def _aggregate_sync_results(sync_results):
    """Merge a list of per-source result dicts into one aggregate dict.

    Preserves backward compat: the rest of the pipeline (analysis, email, CLI
    display) can keep using a single sync_result dict.
    """
    agg = {
        "source": "multi" if len(sync_results) > 1
                  else sync_results[0].get("source", "failed") if sync_results
                  else "failed",
        "added": sum(r.get("added", 0) for r in sync_results),
        "modified": sum(r.get("modified", 0) for r in sync_results),
        "removed": sum(r.get("removed", 0) for r in sync_results),
        "store_size": max((r.get("store_size", 0) for r in sync_results), default=0),
        "cursor": None,
        "error": None,
        "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
        "per_source": sync_results,
    }

    # If ALL sources failed, mark aggregate as failed
    if not any(r.get("source", "failed") != "failed" for r in sync_results):
        agg["source"] = "failed"
        errors = "; ".join(
            f"{r.get('item_name', '?')}: {r.get('error', '?')}"
            for r in sync_results)
        agg["error"] = errors

    # Collect warnings and DB errors from all sources
    all_stale = []
    db_errors = []
    for r in sync_results:
        all_stale.extend(r.get("stale_warnings", []))
        if r.get("db_error"):
            db_errors.append(r["db_error"])
    if all_stale:
        agg["stale_warnings"] = all_stale
    if db_errors:
        agg["db_error"] = "; ".join(db_errors)

    return agg


# ----------------------------- analysis pipeline ------------------------------

def run_analysis(store_path=None, balance=None,
                 mode="weekly", no_voice=True, rules_path="rules.md"):
    """Run the full analysis pipeline on the canonical DB transactions.

    Returns the digest dict (or None if empty / analysis fails). The DB is the single
    source of truth — the email digest and the web report now read the same data, so
    they can't diverge. `store_path` is accepted for backward-compat but ignored.
    """
    conn = db.connect()
    txns = db.load_transactions_from_db(conn)
    conn.close()
    if not txns:
        return None

    # Resolve balance
    if balance is None:
        try:
            from finance_mcp.engines import cashflow_forecaster as cf
            balance = cf.resolve_balance(None)
        except SystemExit:
            balance = None

    try:
        from finance_mcp import finance_agent as fa
        digest = fa.build_digest(
            txns,
            balance=balance,
            mode=mode,
            forecast_days=fa.DEFAULT_FORECAST_DAYS,
            buffer=fa.DEFAULT_BUFFER,
            include_burn=True,
            scan_days=fa.DEFAULT_SCAN_DAYS,
            rules_path=rules_path,
        )
        fa.attach_balance_change(digest, txns, balance)
        return digest
    except Exception as e:
        print(f"  analysis failed: {e}", file=sys.stderr)
        return None


# ----------------------------- combined sync+analyze --------------------------

def sync_and_analyze(snapshot_path=None, balance=None, mode="weekly",
                     no_voice=True, rules_path="rules.md",
                     store_path=None,
                     state_path=None):
    """One-shot sync + analysis. This is the function the MCP tool wraps.

    Syncs all configured sources (Plaid Items), then runs
    the full analysis pipeline on the merged store.

    Returns {
        'sync': <sync result dict>,
        'digest': <digest dict or None>,
        'headline': <str or None>,
    }
    """
    sync_results = sync_all_sources(
        snapshot_path=snapshot_path,
        store_path=store_path,
        state_path=state_path,
    )
    sync_result = _aggregate_sync_results(sync_results)

    digest = None
    headline = None
    if sync_result["source"] != "failed":
        digest = run_analysis(
            store_path=store_path,
            balance=balance,
            mode=mode,
            no_voice=no_voice,
            rules_path=rules_path,
        )
        if digest:
            try:
                from finance_mcp import finance_agent as fa
                headline = fa.headline_line(digest)
            except Exception:
                pass

    return {
        "sync": sync_result,
        "digest": digest,
        "headline": headline,
    }




REPORT_URL = os.environ.get("REPORT_URL", "https://example.com/report")


def _send_failure_email(error_summary, error_detail=None):
    """Best-effort failure notification so the user knows the pipeline broke.

    Sends a short email with the error and last-success timestamp.
    Failures here are swallowed (the pipeline is already failing; crashing
    the error handler makes diagnosis harder, not easier).
    """
    try:
        from finance_mcp.report import delivery
        ss = load_sync_state()
        last_ok = ss.get("last_success_ts", "unknown")
        now = dt.datetime.now().isoformat(timespec="seconds")
        subj = f"[FAILED] Finance sync — {error_summary[:80]}"
        body = (
            f"Pipeline failure at {now}\n\n"
            f"Error: {error_summary}\n"
        )
        if error_detail:
            body += f"\nDetail:\n{error_detail}\n"
        body += (
            f"\nLast successful sync: {last_ok}\n"
            f"Runs to date: {ss.get('runs', '?')}\n"
        )
        delivery.send_email(None, subj, body)
        print("[sync] failure email sent", file=sys.stderr)
    except Exception as mail_err:
        print(f"[sync] could not send failure email: {mail_err}",
              file=sys.stderr)


def resolve_balance(explicit=None):
    """The balance for the forecast: an explicit value if given, else the live
    balance from the bank (available, falling back to current). Returns None only
    if no value is available — the forecast then reports itself unavailable rather
    than inventing a number."""
    if explicit is not None:
        return explicit
    try:
        b = plaid_bridge.fetch_balance()
        bal = b.get("available")
        if bal is None:
            bal = b.get("current")
        if bal is not None:
            print(f"[balance] live: ${bal:,.2f} (available)")
            return bal
    except Exception as e:
        print(f"[balance] live fetch failed ({e}); forecast may be unavailable")
    return None


# --------------------------------- CLI ----------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Transaction sync + analysis pipeline.",
        epilog="Run manually or install the launchd plist for daily automation.",
    )
    ap.add_argument("--snapshot", help="sync from a file snapshot instead of live")
    ap.add_argument("--no-analyze", action="store_true",
                    help="sync only, skip analysis pipeline")
    ap.add_argument("--balance", type=float, default=None,
                    help="starting balance for forecaster")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--weekly", action="store_true")
    g.add_argument("--monthly", action="store_true")
    ap.add_argument("--no-voice", action="store_true", default=True,
                    help="skip narration (default for CLI)")
    ap.add_argument("--voice", action="store_true",
                    help="enable narration")
    ap.add_argument("--rules", default="rules.md")
    ap.add_argument("--email", nargs="?", const="__self__", default=None,
                    help="email the digest")
    ap.add_argument("--json", action="store_true",
                    help="print results as JSON")
    ap.add_argument("--status", action="store_true",
                    help="show sync state and connection status")
    ap.add_argument("--reliable", action="store_true",
                    help="use the idempotent, self-healing date-window pull instead "
                         "of the incremental cursor (which can advance past txns and "
                         "lose them). Recommended for the scheduled job.")
    a = ap.parse_args()

    if a.status:
        ss = load_sync_state()
        conn = plaid_bridge.check_connection()
        print("=== Sync State ===")
        print(json.dumps(ss, indent=2))
        print("\n=== Connection Status ===")
        print(json.dumps(conn, indent=2))
        return

    mode = "weekly" if a.weekly else "monthly"
    no_voice = not a.voice  # --voice overrides the default --no-voice

    try:
        # Sync — all configured Plaid Items
        print(f"[sync] starting at {dt.datetime.now().strftime('%H:%M:%S')}...")
        sync_results = sync_all_sources(snapshot_path=a.snapshot,
                                        use_sync_api=not a.reliable)
        sync_result = _aggregate_sync_results(sync_results)

        if sync_result["source"] == "failed":
            print(f"[sync] FAILED: {sync_result['error']}")
            if a.email is not None:
                _send_failure_email(
                    sync_result.get("error", "sync returned source=failed"))
            if a.json:
                print(json.dumps(sync_result, indent=2))
            sys.exit(1)

        # Per-source status lines
        for r in sync_results:
            name = r.get("item_name", "?")
            print(f"[sync] {name} ({r.get('source', '?')}): "
                  f"+{r.get('added', 0)} added, "
                  f"~{r.get('modified', 0)} modified, "
                  f"-{r.get('removed', 0)} removed")
        print(f"[sync] total: "
              f"+{sync_result['added']} added, "
              f"~{sync_result['modified']} modified, "
              f"-{sync_result['removed']} removed "
              f"(store: {sync_result['store_size']} txns)")

        # Analysis
        if not a.no_analyze:
            balance = resolve_balance(a.balance)   # live balance unless overridden
            print(f"[analysis] running {mode} digest...")
            digest = run_analysis(
                balance=balance,
                mode=mode,
                no_voice=no_voice,
                rules_path=a.rules,
            )
            if digest:
                try:
                    from finance_mcp import finance_agent as fa
                    headline = fa.headline_line(digest)
                    report = fa.render(digest)

                    # Narration
                    voice_text = None
                    if not no_voice:
                        from finance_mcp.report import delivery
                        tone = fa._tone(a.rules)
                        voice_text = delivery.narrate(digest, tone, mode)

                    full_report = report
                    if voice_text:
                        full_report += "\n\n---\n\n## Read\n" + voice_text

                    # Save
                    today = str(dt.date.today())
                    fname = f"digest-{today}.md"
                    with open(fname, "w", encoding="utf-8") as f:
                        f.write(full_report + "\n")

                    print(f"\n{headline}")
                    print(f"[saved {fname}]")

                    # Email
                    if a.email is not None:
                        from finance_mcp.report import delivery
                        to = None if a.email == "__self__" else a.email
                        stale_prefix = ""
                        if sync_result.get("stale_warnings"):
                            stale_prefix = "[STALE?] "
                        db_warn = ""
                        if sync_result.get("db_error"):
                            db_warn = (f"\n\n⚠️ DB write error: "
                                       f"{sync_result['db_error']}")
                        subj = (f"{stale_prefix}finance.mcp — Daily Update · "
                                f"{dt.date.today().isoformat()}")
                        body = (f"{headline}\n\n{full_report}{db_warn}\n\n---\n"
                                f"Live report: {REPORT_URL}\n")
                        try:
                            email_html = delivery.render_digest_html(digest)
                        except Exception as he:
                            email_html = None
                            print(f"[email] HTML render failed ({he}); sending plain")
                        sent = delivery.send_email(to, subj, body, html=email_html)
                        print(f"[email] {'sent' if sent else 'NOT sent'} "
                              f"to {to or 'self'}")

                except Exception as e:
                    print(f"[analysis] rendering failed: {e}")
            else:
                print("[analysis] skipped (empty store or analysis error)")

        # Heartbeat: update last_success_ts on full pipeline completion
        ss = load_sync_state()
        ss["last_success_ts"] = dt.datetime.now().isoformat(timespec="seconds")
        save_sync_state(ss)

        if a.json:
            print(json.dumps(sync_result, indent=2, default=str))

    except SystemExit:
        raise  # let sys.exit() propagate
    except Exception as exc:
        print(f"[sync] UNHANDLED ERROR: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        if a.email is not None:
            _send_failure_email(str(exc), traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
