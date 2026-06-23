#!/usr/bin/env python3
"""
plaid_bridge.py — adapter between the bank-mcp fork and the local ledger.

The bank-mcp fork exposes Plaid transactions in its own normalized shape:
top-level {id, amount (signed), date, type, category, merchantName, description,
pending} plus a nested rawData Plaid object {transaction_id, amount (unsigned
magnitude), date, merchant_name, merchant_entity_id, personal_finance_category,
counterparties, name, pending, ...}.

This bridge:
  1. Calls the bank-mcp `sync_transactions` tool (or `list_transactions` for
     initial loads) via subprocess/HTTP/MCP-stdio as available.
  2. Normalizes the response into the transaction list shape that
     subscription_creep / ledger / recurring / the whole suite consumes.
  3. Handles cursor-based pagination (hasMore loop) for sync_transactions.
  4. Degrades gracefully: file-snapshot fallback when no live connection exists.

ARCHITECTURE: pure data plumbing. No model calls. No financial math (that
lives in db.py and the analysis tools). Credentials via env/Keychain
(PLAID_CLIENT_ID, PLAID_SECRET, BANK_MCP_URL) following the suite's pattern.
"""

import json
import os
import select
import subprocess
import time
import datetime as dt
from finance_mcp.ingest import safehttp


# ----------------------------- config -----------------------------------------

# Bank MCP server URL (if running as HTTP). Env or default.
BANK_MCP_URL = os.environ.get("BANK_MCP_URL", "http://localhost:3000")

# Plaid credentials (needed only for direct API calls; bank-mcp manages its own)
PLAID_CLIENT_ID = os.environ.get("PLAID_CLIENT_ID")
PLAID_SECRET = os.environ.get("PLAID_SECRET")
PLAID_ENV = os.environ.get("PLAID_ENV", "sandbox")

# Path to the bank-mcp-fork CLI (if installed locally)
BANK_MCP_CLI = os.environ.get("BANK_MCP_CLI", os.path.expanduser("~/dev/bank-mcp-fork"))

# Cursor state file — tracks the Plaid sync cursor between runs
CURSOR_PATH = os.environ.get(
    "PLAID_CURSOR_PATH",
    os.path.expanduser("~/Downloads/plaid_sync_cursor.json"),
)

# Multi-Item config file (gitignored — references token env var names).
# Each entry: {"name": "...", "token_env": "PLAID_ACCESS_TOKEN_X",
#              "owner": "primary", "cursor_file": "~/.finance-mcp/plaid_cursor_x.json"}
PLAID_ITEMS_PATH = os.environ.get(
    "PLAID_ITEMS_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "plaid_items.json"),
)


# ----------------------------- credential helpers -----------------------------

def _keychain_get(service):
    """Read a secret from the macOS Keychain. Returns None if unavailable."""
    try:
        r = subprocess.run(
            ["/usr/bin/security", "find-generic-password",
             "-a", os.environ.get("USER", ""), "-s", service, "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return None


def _plaid_client_id():
    return PLAID_CLIENT_ID or _keychain_get("PLAID_CLIENT_ID")


def _plaid_secret():
    return PLAID_SECRET or _keychain_get("PLAID_SECRET")


def _plaid_access_token(token_env="PLAID_ACCESS_TOKEN"):
    tok = os.environ.get(token_env)
    if tok:
        return tok
    return _keychain_get(token_env)


# ----------------------------- multi-Item config --------------------------------

def load_plaid_items():
    """Load multi-Item configuration from plaid_items.json.

    Each item: {"name": str, "token_env": str, "owner": str, "cursor_file": str}.
    Falls back to a single-item config derived from PLAID_ACCESS_TOKEN if the
    config file doesn't exist — zero breaking changes.
    """
    if os.path.exists(PLAID_ITEMS_PATH):
        try:
            with open(PLAID_ITEMS_PATH, encoding="utf-8") as f:
                items = json.load(f)
            if isinstance(items, list) and items:
                for item in items:
                    cf = item.get("cursor_file", "")
                    if cf:
                        item["cursor_file"] = os.path.expanduser(cf)
                return items
        except (json.JSONDecodeError, OSError):
            pass

    # Fallback: single-token from env var or Keychain
    tok = _plaid_access_token()
    if tok:
        return [{
            "name": "default",
            "token_env": "PLAID_ACCESS_TOKEN",
            "owner": "primary",
            "cursor_file": CURSOR_PATH,
        }]
    return []


def _resolve_access_token(item):
    """Resolve the access token for a Plaid Item config entry."""
    env_var = item.get("token_env", "PLAID_ACCESS_TOKEN")
    return _plaid_access_token(env_var)


# ----------------------------- cursor persistence -----------------------------

def load_cursor(path=CURSOR_PATH):
    """Load the persisted Plaid sync cursor. Returns dict with 'cursor' key."""
    if not os.path.exists(path):
        return {"cursor": None, "last_sync": None}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"cursor": None, "last_sync": None}


def save_cursor(cursor, path=CURSOR_PATH):
    """Persist the Plaid sync cursor after a successful sync."""
    data = {
        "cursor": cursor,
        "last_sync": dt.datetime.now().isoformat(timespec="seconds"),
    }
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())


# ----------------------------- transaction normalizer -------------------------

def normalize_plaid_txn(raw_plaid):
    """Convert a raw Plaid transaction object into the two-level shape the
    finance suite expects: top-level normalized fields + rawData.

    The bank-mcp fork already does this normalization, so this is a passthrough
    for bank-mcp responses. For direct Plaid API responses, this maps the flat
    Plaid fields into the expected shape.
    """
    # Already in bank-mcp shape (has rawData)? Return as-is.
    if "rawData" in raw_plaid:
        return raw_plaid

    # Direct Plaid API response — normalize into bank-mcp shape
    tid = raw_plaid.get("transaction_id") or raw_plaid.get("id") or ""
    plaid_amount = raw_plaid.get("amount", 0)  # Plaid: positive = debit
    signed = -abs(plaid_amount) if plaid_amount > 0 else abs(plaid_amount)

    pfc = raw_plaid.get("personal_finance_category") or {}
    category = pfc.get("detailed") or pfc.get("primary") or ""

    return {
        "id": tid,
        "amount": round(signed, 2),
        "date": raw_plaid.get("date", ""),
        "type": "debit" if signed < 0 else "credit",
        "category": category,
        "merchantName": raw_plaid.get("merchant_name") or raw_plaid.get("name") or "",
        "description": raw_plaid.get("name") or "",
        "pending": raw_plaid.get("pending", False),
        "rawData": raw_plaid,
    }


# ----------------------------- transport adapters -----------------------------
# Tried in order: bank-mcp subprocess, direct Plaid API, file snapshot.

class BankMCPError(Exception):
    """Raised when a bank-mcp call fails in a non-recoverable way."""


def _try_bank_mcp_subprocess(method, params):
    """Call the bank-mcp fork as an MCP stdio server.

    The fork is a standard @modelcontextprotocol/sdk stdio server, so a single
    raw JSON-RPC line is not enough — it requires the MCP handshake:
    ``initialize`` -> ``notifications/initialized`` -> ``tools/call``. The tool
    result arrives as a JSON string in ``result.content[0].text``, which is
    parsed and returned (this matches the dict shape the fetch_* callers expect:
    added/modified/removed/next_cursor for sync, transactions for list).

    Returns the parsed result dict or raises BankMCPError.
    """
    cli_path = BANK_MCP_CLI
    if not os.path.isdir(cli_path):
        raise BankMCPError(f"bank-mcp-fork not found at {cli_path}")

    # Look for the entry point
    entry = None
    for candidate in ("dist/index.js", "build/index.js", "src/index.ts", "index.js"):
        p = os.path.join(cli_path, candidate)
        if os.path.exists(p):
            entry = p
            break
    if entry is None:
        raise BankMCPError(f"No entry point found in {cli_path}")

    # Determine runtime
    if entry.endswith(".ts"):
        cmd = ["npx", "tsx", entry]
    else:
        cmd = ["node", entry]

    try:
        proc = subprocess.Popen(
            cmd, cwd=cli_path,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
    except OSError as e:
        raise BankMCPError(f"bank-mcp failed to launch ({cmd[0]}): {e}")

    def _send(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def _read_result(want_id, deadline):
        # Read newline-delimited JSON-RPC messages until one matches want_id.
        # select() bounds each wait so a silent server still honors the deadline.
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise BankMCPError("bank-mcp subprocess timed out")
            rlist, _, _ = select.select([proc.stdout], [], [], remaining)
            if not rlist:
                continue
            line = proc.stdout.readline()
            if line == "":
                raise BankMCPError("bank-mcp closed stdout before responding")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # ignore any non-JSON noise on stdout
            if msg.get("id") == want_id:
                return msg

    deadline = time.time() + 30
    try:
        _send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": "2024-11-05",
                          "capabilities": {},
                          "clientInfo": {"name": "plaid_bridge", "version": "1.0"}}})
        init = _read_result(1, deadline)
        if "error" in init:
            raise BankMCPError(f"bank-mcp initialize error: {init['error']}")
        _send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        _send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
               "params": {"name": method, "arguments": params or {}}})
        resp = _read_result(2, deadline)
    except (BrokenPipeError, OSError) as e:
        err = ""
        try:
            err = proc.stderr.read()[:200]
        except Exception:
            pass
        raise BankMCPError(f"bank-mcp subprocess I/O failed: {e}; {err}")
    finally:
        for closer in (lambda: proc.stdin.close(),
                       lambda: proc.terminate()):
            try:
                closer()
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()

    if "error" in resp:
        raise BankMCPError(f"bank-mcp error: {resp['error']}")
    result = resp.get("result") or {}
    content = result.get("content") or []
    if not content:
        raise BankMCPError("bank-mcp returned no content")
    text = content[0].get("text", "")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        raise BankMCPError(f"bank-mcp content was not JSON: {text[:200]}")
    if isinstance(payload, dict) and payload.get("error"):
        raise BankMCPError(f"bank-mcp tool error: {payload.get('message')}")
    return payload


def _try_bank_mcp_http(method, params):
    """Call bank-mcp via HTTP (if running as a server).

    Returns the result dict or raises BankMCPError.
    """
    import urllib.request
    import urllib.error

    url = f"{BANK_MCP_URL}/api/{method}"
    body = json.dumps(params).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
    )
    # Only allow http://localhost connections for the bank MCP
    if not (req.full_url.startswith("http://localhost") or
            req.full_url.startswith("http://127.0.0.1")):
        raise BankMCPError(f"Refusing non-localhost bank-mcp URL: {url}")
    try:
        with safehttp.fetch(req, timeout=30, allow_localhost=True) as r:
            data = json.loads(r.read())
        if "error" in data:
            raise BankMCPError(f"bank-mcp HTTP error: {data['error']}")
        return data.get("result", data)
    except urllib.error.URLError as e:
        raise BankMCPError(f"bank-mcp HTTP unreachable: {e}")
    except json.JSONDecodeError:
        raise BankMCPError("bank-mcp HTTP returned invalid JSON")


def _try_plaid_direct(method, params, access_token=None):
    """Call the Plaid API directly (last resort before file fallback).

    If access_token is provided, uses it directly (multi-Item support).
    Otherwise falls back to env/Keychain lookup.

    Returns the result dict or raises BankMCPError.
    """
    import urllib.request
    import urllib.error

    client_id = _plaid_client_id()
    secret = _plaid_secret()
    if access_token is None:
        access_token = _plaid_access_token()

    if not (client_id and secret and access_token):
        raise BankMCPError("No Plaid credentials (PLAID_CLIENT_ID, PLAID_SECRET, "
                           "PLAID_ACCESS_TOKEN via env or Keychain)")

    env_urls = {
        "sandbox": "https://sandbox.plaid.com",
        "development": "https://development.plaid.com",
        "production": "https://production.plaid.com",
    }
    base = env_urls.get(PLAID_ENV, env_urls["sandbox"])

    if method == "sync_transactions":
        url = f"{base}/transactions/sync"
        body = {
            "client_id": client_id,
            "secret": secret,
            "access_token": access_token,
            "cursor": params.get("cursor") or "",
            "count": params.get("count", 500),
        }
    elif method == "list_transactions":
        url = f"{base}/transactions/get"
        body = {
            "client_id": client_id,
            "secret": secret,
            "access_token": access_token,
            "start_date": params.get("dateFrom", "2020-01-01"),
            "end_date": params.get("dateTo",
                                   dt.date.today().isoformat()),
            "options": {"count": params.get("limit", 500), "offset": 0},
        }
    elif method == "get_balance":
        url = f"{base}/accounts/balance/get"
        body = {
            "client_id": client_id,
            "secret": secret,
            "access_token": access_token,
        }
    else:
        raise BankMCPError(f"Unknown Plaid method: {method}")

    if not url.startswith("https://"):
        raise BankMCPError(f"Refusing non-HTTPS Plaid URL: {url}")

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with safehttp.fetch(req, timeout=60) as r:
            data = json.loads(r.read())
        return data
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode()[:200]
        except Exception:
            pass
        raise BankMCPError(f"Plaid API HTTP {e.code}: {err_body}")
    except Exception as e:
        raise BankMCPError(f"Plaid API error: {e}")


# ----------------------------- high-level API ---------------------------------

def _call(method, params, access_token=None):
    """Try each transport in order; return the first success.

    If access_token is provided, only the direct Plaid transport is tried
    (subprocess/HTTP bank-mcp servers use their own stored credentials and
    cannot be pointed at a different Plaid Item).

    Raises BankMCPError only if ALL transports fail.
    """
    if access_token is not None:
        # Per-Item token: only direct Plaid supports custom access tokens.
        return _try_plaid_direct(method, params, access_token=access_token)

    errors = []
    for transport_name, transport_fn in [
        ("bank-mcp subprocess", _try_bank_mcp_subprocess),
        ("bank-mcp HTTP", _try_bank_mcp_http),
        ("Plaid direct", _try_plaid_direct),
    ]:
        try:
            return transport_fn(method, params)
        except BankMCPError as e:
            errors.append(f"{transport_name}: {e}")
    raise BankMCPError(
        "All transports failed:\n  " + "\n  ".join(errors)
    )


def fetch_transactions_sync(cursor=None, cursor_path=CURSOR_PATH,
                            access_token=None):
    """Incremental sync via Plaid's /transactions/sync.

    Pages through hasMore, collecting added/modified/removed. Updates the
    persisted cursor only after ALL pages are consumed (matches the DB-durable
    invariant #1 pattern: don't advance the cursor until data is safe).

    If access_token is provided, uses it for the Plaid API call (multi-Item).

    Returns {
        'added': [txn, ...],       # normalized bank-mcp shape
        'modified': [txn, ...],
        'removed': [id, ...],
        'cursor': <new cursor>,
        'pages': int,
    }
    """
    if cursor is None:
        state = load_cursor(cursor_path)
        cursor = state.get("cursor")

    all_added, all_modified, all_removed = [], [], []
    pages = 0
    has_more = True

    while has_more:
        params = {"cursor": cursor or ""}
        try:
            resp = _call("sync_transactions", params, access_token=access_token)
        except BankMCPError:
            if pages == 0:
                raise  # first page failed — propagate
            break  # partial page — keep what we have, don't advance cursor past it

        # Normalize the response shape (Plaid direct vs bank-mcp)
        added_raw = resp.get("added") or []
        modified_raw = resp.get("modified") or []
        removed_raw = resp.get("removed") or []

        all_added.extend(normalize_plaid_txn(t) for t in added_raw)
        all_modified.extend(normalize_plaid_txn(t) for t in modified_raw)

        # removed can be transaction objects or just IDs
        for r in removed_raw:
            if isinstance(r, dict):
                tid = r.get("transaction_id") or r.get("id")
            else:
                tid = r
            if tid:
                all_removed.append(tid)

        cursor = resp.get("next_cursor") or resp.get("cursor") or cursor
        has_more = resp.get("has_more", False) or resp.get("hasMore", False)
        pages += 1

        # Safety: cap at 50 pages to prevent infinite loops
        if pages >= 50:
            break

    return {
        "added": all_added,
        "modified": all_modified,
        "removed": all_removed,
        "cursor": cursor,
        "pages": pages,
    }


def fetch_transactions_list(date_from=None, date_to=None, limit=500,
                            access_token=None, connection_id=None):
    """Date-window snapshot via list_transactions (non-incremental).

    Simpler than sync; good for initial loads or when you don't have a cursor.
    `connection_id` targets a specific bank-mcp connection (e.g. a second account holder's bank);
    omit it for the default connection. Returns normalized transaction dicts.
    """
    if date_from is None:
        date_from = (dt.date.today() - dt.timedelta(days=90)).isoformat()
    if date_to is None:
        date_to = dt.date.today().isoformat()

    params = {"dateFrom": date_from, "dateTo": date_to, "limit": limit}
    if connection_id:
        params["connectionId"] = connection_id
    try:
        resp = _call("list_transactions", params, access_token=access_token)
    except BankMCPError:
        raise

    # Response may be a list or a dict with a transactions key
    if isinstance(resp, list):
        raw_txns = resp
    elif isinstance(resp, dict):
        raw_txns = (resp.get("transactions") or resp.get("added")
                    or resp.get("data") or [])
    else:
        raw_txns = []

    return [normalize_plaid_txn(t) for t in raw_txns]


def fetch_transactions_range(date_from, date_to=None, window_days=90, limit=500,
                             access_token=None, connection_id=None):
    """Complete transaction history over [date_from, date_to] — transport-agnostic.

    Root cause this fixes: fetch_transactions_list returns at most `limit` per call
    with no offset paging, and the bank-mcp list tool exposes no offset param — so a
    dense or wide range silently truncates (the symptom: a 3-year request returning
    only the most-recent 500). This walks the range in fixed windows and, if any
    window saturates the cap, BISECTS it recursively until it doesn't — so the result
    is complete regardless of transaction volume or which transport serves the data.
    Dedupes by transaction id across windows (overlaps are harmless).
    """
    if date_to is None:
        date_to = dt.date.today().isoformat()
    start = dt.date.fromisoformat(date_from)
    end = dt.date.fromisoformat(date_to)
    by_id = {}

    def pull(a, b):
        batch = fetch_transactions_list(date_from=a.isoformat(),
                                        date_to=b.isoformat(), limit=limit,
                                        access_token=access_token,
                                        connection_id=connection_id)
        if len(batch) >= limit and a < b:
            mid = a + (b - a) // 2          # saturated → bisect to avoid truncation
            pull(a, mid)
            pull(mid + dt.timedelta(days=1), b)
            return
        for t in batch:
            tid = t.get("id") or (t.get("rawData") or {}).get("transaction_id")
            if tid:
                by_id[tid] = t

    cur = start
    while cur <= end:
        wend = min(cur + dt.timedelta(days=window_days - 1), end)
        pull(cur, wend)
        cur = wend + dt.timedelta(days=1)
    return list(by_id.values())


def fetch_balance(access_token=None):
    """Live account balance via the bank-mcp `get_balance` tool (subprocess), with
    a Plaid-direct fallback. Returns {'available': float|None, 'current': float|None,
    'currency': str}. Lets the daily forecast anchor on the real balance instead of a
    hardcoded --balance. Raises BankMCPError only if every transport fails.
    """
    resp = _call("get_balance", {}, access_token=access_token)
    avail = current = None
    ccy = "USD"
    if isinstance(resp, list):
        # bank-mcp shape: [{accountId, amount, currency, type: current|available}]
        for r in resp:
            t = str(r.get("type") or "").lower()
            amt = r.get("amount")
            if not isinstance(amt, (int, float)):
                continue
            if "avail" in t:
                avail = float(amt)
            elif "current" in t or "book" in t:
                current = float(amt)
            ccy = r.get("currency") or ccy
    elif isinstance(resp, dict):
        # Plaid /accounts/balance/get shape: {accounts: [{balances: {...}}]}
        accts = resp.get("accounts") or resp.get("data") or []
        if isinstance(accts, list) and accts:
            bals = accts[0].get("balances") or {}
            avail = bals.get("available")
            current = bals.get("current")
            ccy = bals.get("iso_currency_code") or ccy
        else:
            avail = resp.get("available")
            current = resp.get("current")
    return {"available": avail, "current": current, "currency": ccy}


def fetch_from_snapshot(path):
    """File-based fallback — load a transactions.json snapshot.

    This is the current path (no live bank connection); it reads the same
    file format subscription_creep.load_transactions handles. Returns
    normalized transaction dicts.
    """
    from finance_mcp.store import subscription_creep as sc
    txns = sc.load_transactions(path)
    return [normalize_plaid_txn(t) for t in txns]


# ----------------------------- availability check -----------------------------

def check_connection():
    """Probe which transport is available. Returns a dict describing status.

    Non-destructive: does not consume any sync cursor or modify state.
    """
    status = {
        "bank_mcp_subprocess": False,
        "bank_mcp_http": False,
        "plaid_direct": False,
        "has_credentials": bool(_plaid_client_id() and _plaid_secret()),
        "has_access_token": bool(_plaid_access_token()),
        "bank_mcp_path": BANK_MCP_CLI,
        "bank_mcp_url": BANK_MCP_URL,
    }

    # Check subprocess
    if os.path.isdir(BANK_MCP_CLI):
        status["bank_mcp_subprocess"] = True

    # Check HTTP (quick connect test)
    try:
        url = f"{BANK_MCP_URL}/health"
        if url.startswith("http://localhost") or url.startswith("http://127.0.0.1"):
            with safehttp.fetch(url, timeout=3, allow_localhost=True) as r:
                status["bank_mcp_http"] = r.status == 200
    except Exception:
        pass

    # Direct Plaid only needs credentials
    if status["has_credentials"] and status["has_access_token"]:
        status["plaid_direct"] = True

    status["any_available"] = any([
        status["bank_mcp_subprocess"],
        status["bank_mcp_http"],
        status["plaid_direct"],
    ])
    return status


# --------------------------------- CLI ----------------------------------------

def main():
    import argparse
    ap = argparse.ArgumentParser(description="Plaid bridge — test connectivity and fetch.")
    ap.add_argument("--check", action="store_true", help="check which transports are available")
    ap.add_argument("--sync", action="store_true", help="run incremental sync")
    ap.add_argument("--list", action="store_true", help="fetch date-window snapshot")
    ap.add_argument("--from", dest="date_from", help="YYYY-MM-DD start date (for --list)")
    ap.add_argument("--to", dest="date_to", help="YYYY-MM-DD end date (for --list)")
    ap.add_argument("--snapshot", help="load from a file snapshot (fallback)")
    ap.add_argument("--json", action="store_true", help="output as JSON")
    a = ap.parse_args()

    if a.check:
        status = check_connection()
        print(json.dumps(status, indent=2))
        return

    if a.snapshot:
        txns = fetch_from_snapshot(a.snapshot)
        print(f"Loaded {len(txns)} transactions from {a.snapshot}")
        if a.json:
            print(json.dumps(txns[:5], indent=2, default=str))
        return

    if a.sync:
        try:
            result = fetch_transactions_sync()
            print(f"Sync complete: +{len(result['added'])} added, "
                  f"~{len(result['modified'])} modified, "
                  f"-{len(result['removed'])} removed "
                  f"({result['pages']} pages)")
            if a.json:
                print(json.dumps(result, indent=2, default=str))
        except BankMCPError as e:
            print(f"Sync failed: {e}")
            raise SystemExit(1)
        return

    if a.list:
        try:
            txns = fetch_transactions_list(date_from=a.date_from, date_to=a.date_to)
            print(f"Fetched {len(txns)} transactions")
            if a.json:
                print(json.dumps(txns[:5], indent=2, default=str))
        except BankMCPError as e:
            print(f"Fetch failed: {e}")
            raise SystemExit(1)
        return

    ap.print_help()


if __name__ == "__main__":
    main()
