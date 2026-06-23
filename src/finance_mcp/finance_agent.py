#!/usr/bin/env python3
"""
finance_agent.py — the UNIFIED runner for the finance.mcp finance-agent suite.

This is the single entrypoint the user will later schedule (this module builds NO
scheduler/cron of its own). It dispatches to the deterministic cores of the four
analysis tools, collates their compact summary dicts into ONE combined digest
(~1K tokens, NO raw transaction rows), runs at most ONE optional Haiku narration
pass over that whole digest, saves digest-<today>.md, prints the headline, and
optionally emails the digest.

Tools run (deterministic cores, imported — never reimplemented):
  - cashflow_forecaster.build_summary  → overdraft / min-balance forecast (headline)
  - budget_scorer.build_summary        → savings-goal pace (--weekly | --monthly)
  - fee_fraud_scan.scan                → avoidable fees + duplicate + suspect $
  - recurring.streams                  → active inflow/outflow recurring snapshot

ARCHITECTURE RULE (load-bearing): all financial math is deterministic Python in
the imported cores. The model NEVER sees raw transactions — narration runs
(delivery.narrate) over the combined COMPACT digest only. One cheap Haiku call,
not one per tool. With --no-voice the run is fully deterministic at $0 tokens.

Balance (forecaster, contract order):
  --balance FLOAT wins. Else balance.json ({"balance": <float>}). Else the
  forecaster section is reported as UNAVAILABLE (no starting balance) and the run
  continues — the other three tools don't need a balance.

Usage:
  python3 finance_agent.py transactions.json --no-voice
  python3 finance_agent.py transactions.json --weekly --balance 1200
  python3 finance_agent.py transactions.json --monthly --balance 1200 --email you@x.com
  python3 finance_agent.py transactions.json --balance 1200 --days 35 --buffer 100
"""

import argparse
import datetime as dt
import json
import os

from finance_mcp.report import digest_templates as dtpl

from finance_mcp.store import subscription_creep as sc
from finance_mcp.engines import budget_scorer as bs
from finance_mcp.engines import cashflow_forecaster as cf
from finance_mcp.store import obligation_registry as oblreg
from finance_mcp.engines import fee_fraud_scan as ff
from finance_mcp.engines import recurring as rec
from finance_mcp.engines import receipt_scanner as rs
from finance_mcp.engines import dispute_agent as da
from finance_mcp.report import delivery

money = delivery.money

DEFAULT_FORECAST_DAYS = cf.DEFAULT_DAYS
DEFAULT_BUFFER = cf.DEFAULT_BUFFER
DEFAULT_SCAN_DAYS = 30

# Where per-build balance snapshots are recorded, so every report can show what
# moved the balance since the last one.
BALANCE_SNAP_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "balance_snapshots.json")


def attach_balance_change(digest, txns, balance, snap_path=BALANCE_SNAP_PATH):
    """Record today's balance and attach `digest['balance_change']`: the prior
    snapshot, the dollar delta, and the transactions since then — so 'why did my
    balance change' is always answerable. Best-effort; never breaks the digest."""
    if balance is None:
        return digest

    def _tid(t):
        return t.get("id") or (t.get("rawData") or {}).get("transaction_id")

    try:
        today = str(dt.date.today())
        try:
            with open(snap_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            data = {}

        prior_ids = set(data.get("last_seen_ids") or [])
        cur_ids = [_tid(t) for t in txns if _tid(t)]
        # The transactions RESPONSIBLE for the change are the ones that newly
        # appeared since the last report — by ingestion, not by date. Late-posting
        # charges show up days after their transaction date, so an id-diff (not a
        # date filter) is what correctly attributes the balance move.
        if prior_ids and data.get("last_balance") is not None:
            new_txns = sorted(
                (t for t in txns if _tid(t) not in prior_ids and t.get("amount")),
                key=lambda t: t.get("date", ""), reverse=True)
            rows = [{"date": t.get("date", ""),
                     "merchant": (t.get("merchantName")
                                  or t.get("description") or "?")[:36],
                     "amount": round(float(t["amount"]), 2)} for t in new_txns]
            net = round(sum(r["amount"] for r in rows), 2)
            delta = round(balance - data["last_balance"], 2)
            digest["balance_change"] = {
                "prior_balance": round(data["last_balance"], 2),
                "prior_date": data.get("last_date", ""),
                "current_balance": round(balance, 2),
                "delta": delta,
                "net": net,
                # honest: with a lagging feed the newly-synced rows won't always sum
                # to the live-balance move (some of it is pending / just-posted).
                "reconciles": abs(net - delta) <= 1.0,
                "txns": rows[:15],
            }
        data["history"] = (data.get("history") or [])
        data["history"] = [h for h in data["history"] if h.get("date") != today]
        data["history"].append({"date": today, "balance": round(balance, 2)})
        data["history"] = data["history"][-120:]
        data["last_seen_ids"] = cur_ids
        data["last_balance"] = round(balance, 2)
        data["last_date"] = today
        with open(snap_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass
    return digest


# ----------------------------- tone (for narrate) ---------------------------

def _tone(rules_path):
    """Reuse the user's own 'How to read me' tone block from rules.md if present,
    else a sane default. Best-effort; the agent never depends on rules.md."""
    try:
        return bs.parse_rules(rules_path)["tone"]
    except Exception:
        return ("Direct and specific, blunt where there's risk — a mirror, not a "
                "warden. Tie back to the savings target and the move date.")


# ----------------------------- section builders -----------------------------
# Each returns a COMPACT dict (no raw rows). On failure each returns a small
# error stub so one tool's hiccup never sinks the whole digest.

def _forecast_section(txns, balance, days, buffer, include_burn,
                      reconciliation=None, today=None):
    """Cash-flow / overdraft forecast. balance is a float or None.

    With no balance the section is reported UNAVAILABLE (the other tools don't
    need one). The forecaster owns all the day-by-day math; we keep only the
    compact headline + a trimmed detail/flags slice."""
    if balance is None:
        return {
            "tool": "cashflow_forecaster",
            "available": False,
            "reason": "no starting balance (pass --balance or create balance.json)",
            "headline": {},
            "flags": ["Forecast skipped: no starting balance provided."],
        }
    # Forward-plan model: drive the forecast from the confirmed obligation
    # registry + the chosen discretionary budget, not historical extrapolation.
    registry = oblreg.load_registry()
    fwd_budget = registry.get("discretionary_budget_monthly") if registry else None
    summary, _proj = cf.build_summary(txns, balance, days, buffer,
                                      include_burn=include_burn,
                                      reconciliation=reconciliation,
                                      registry=registry,
                                      discretionary_budget=fwd_budget,
                                      today=today)
    h = summary["headline"]
    d = summary["detail"]
    return {
        "tool": "cashflow_forecaster",
        "available": True,
        "as_of": summary["as_of"],
        "window": summary["window"],
        "headline": {
            "start_balance": h["start_balance"],
            "buffer": h["buffer"],
            "horizon_days": h["horizon_days"],
            "projected_end_balance": h["projected_end_balance"],
            "min_balance": h["min_balance"],
            "min_date": h["min_date"],
            "overdraft": h["overdraft"],
            "low_balance": h["low_balance"],
            "safe_by": h["safe_by"],
            "daily_burn": h["daily_burn"],
            "next_income": h["next_income"],
            "n_pending_receipts": h.get("n_pending_receipts", 0),
            "budget_driven": h.get("budget_driven", False),
            "discretionary_monthly": h.get("discretionary_monthly"),
            "historical_discretionary_monthly": h.get("historical_discretionary_monthly"),
            "obligation_floor_monthly": h.get("obligation_floor_monthly"),
            "tracks": h.get("tracks"),
        },
        # keep only a few flagged days so the digest stays ~1K tokens
        "detail": {
            "overdraft_days": d["overdraft_days"][:3],
            "low_days": d["low_days"][:3],
            "biggest_obligations": d["biggest_obligations"][:3],
            "pending_receipts": d.get("pending_receipts", [])[:3],
            "curve": d.get("curve", []),
        },
        "flags": summary["flags"],
    }


def _budget_section(txns, rules_path, mode, balance=None):
    """savings-goal pace for the requested cadence (weekly|monthly). Reuses
    budget_scorer.build_summary verbatim and projects only the headline numbers
    a digest needs."""
    R = bs.parse_rules(rules_path)
    s = bs.build_summary(txns, R, mode, balance=balance)
    b = s["goal"]
    # The scorer already computed the ahead/behind projection verdict; we surface
    # that rather than recomputing a pace status here.
    return {
        "tool": "budget_scorer",
        "mode": s["mode"],
        "as_of": s["as_of"],
        "window": s["window"],
        "headline": {
            "target": s["target"],
            "move_date": s["move_date"],
            "net_saved_window": s["cashflow"]["net_saved"],
            "income_window": s["cashflow"]["income"],
            "spend_window": s["cashflow"]["spend"],
            "running_total": b["running_total"],
            "pct_to_target": b["pct"],
            "current_pace_mo": b["current_pace"],
            "required_pace_mo": b["required_pace"],
            "monthly_income": b.get("monthly_income", 0),
            "obligation_floor": b.get("obligation_floor", 0),
            "discretionary_budget": b.get("discretionary_budget", 0),
            "projected": b["projected"],
            "status": b["status"],
            "gap": b["gap"],
            "months_remaining": b["months_remaining"],
            "saved_vs_habit": s["discretionary"]["saved_vs_habit"],
            "tailwind": b.get("tailwind", 0),
            "freed_obligations": b.get("freed_obligations", []),
        },
        "rule_tally": s["rule_tally"],
        # only the slipped/drifting rules matter for a digest, capped
        "detail": {
            "off_track_rules": [r for r in s["rules"]
                                if r["status"] != "on track"][:5],
            "mom_history": bs.monthly_breakdown(txns, s["as_of"], 4),
            # Where the money goes — from the Plaid category on every txn.
            "categories": bs.category_breakdown(txns, s["window"], oblreg.load_registry()),
        },
        "flags": _budget_flags(s),
    }


def _budget_flags(s):
    """Compact flag lines from the budget summary's own flag buckets."""
    out = []
    f = s["flags"]
    for n in f.get("new_recurring", [])[:3]:
        out.append(f"new recurring: {n['merchant']} {money(n['amount'])} "
                   f"{n['cadence']} (since {n['since']})")
    for x in f.get("duplicates", [])[:3]:
        out.append(f"duplicate: {x['merchant']} {money(x['amount'])} "
                   f"{x['dates'][0]} & {x['dates'][1]}")
    for x in f.get("fees", [])[:3]:
        out.append(f"bank fee: {x['merchant']} {money(x['amount'])} {x['date']}")
    return out


def _fee_fraud_section(txns, days, reconciliation=None):
    """Fee + fraud/error scan. Reuses fee_fraud_scan.scan verbatim; keeps the
    headline + a trimmed detail slice.

    reconciliation : dict | None
        If provided, a reconciliation result from receipt_scanner.reconcile().
        Amount discrepancies and unmatched charges are injected as findings.
    """
    s = ff.scan(txns, days=days, reconciliation=reconciliation)
    h = s["headline"]
    d = s["detail"]
    return {
        "tool": "fee_fraud_scan",
        "as_of": s["as_of"],
        "window": s["window"],
        "headline": {
            "avoidable_plus_suspect": h["avoidable_plus_suspect"],
            "avoidable": h["avoidable"],
            "suspect": h["suspect"],
            "fees_total": h["fees_total"],
            "dup_recoverable": h["dup_recoverable"],
            "anomalies_total": h.get("anomalies_total", 0),
            "n_anomalies": h.get("n_anomalies", 0),
            "recurring_fee_annual": h.get("recurring_fee_annual", 0),
            "n_recurring_fees": h.get("n_recurring_fees", 0),
            "not_theirs_total": h["not_theirs_total"],
            "low_conf_total": h["low_conf_total"],
            "n_fees": h["n_fees"],
            "n_duplicates": h["n_duplicates"],
            "n_suspect_merchants": h["n_suspect_merchants"],
            "n_receipt_discrepancies": h.get("n_receipt_discrepancies", 0),
            "receipt_discrepancy_total": h.get("receipt_discrepancy_total", 0),
            "n_unverified_charges": h.get("n_unverified_charges", 0),
            "unverified_charge_total": h.get("unverified_charge_total", 0),
        },
        "detail": {
            "fees": d["fees"][:4],
            "recurring_fees": d.get("recurring_fees", [])[:4],
            "anomalies": d.get("anomalies", [])[:6],
            "duplicates": d["duplicates"][:4],
            "suspicious": d["suspicious"][:4],
            "receipt_discrepancies": d.get("receipt_discrepancies", [])[:4],
            "unverified_charges": d.get("unverified_charges", [])[:4],
        },
        "flags": s["flags"],
    }


def _recurring_section(txns):
    """Active recurring-stream snapshot: counts + monthly run-rate + a few of the
    biggest active streams each side. Reuses recurring.streams verbatim."""
    found = rec.streams(txns)
    active_in = [s for s in found if s["direction"] == "inflow" and s["is_active"]]
    active_out = [s for s in found if s["direction"] == "outflow" and s["is_active"]]

    def monthly_runrate(streams_list):
        # annualized run-rate / 12; recurring.annualized = avg_amount * per_year
        return round(sum(rec.annualized(s) for s in streams_list) / 12.0, 2)

    def top(streams_list, k=5):
        ranked = sorted(streams_list, key=rec.annualized, reverse=True)[:k]
        return [{"merchant": s["merchant"], "cadence": s["cadence"],
                 "avg_amount": s["avg_amount"],
                 "monthly_runrate": round(rec.annualized(s) / 12.0, 2),
                 "next_date": str(s["next_date"])}
                for s in ranked]

    return {
        "tool": "recurring",
        "headline": {
            "n_active_inflow": len(active_in),
            "n_active_outflow": len(active_out),
            "inflow_monthly_runrate": monthly_runrate(active_in),
            "outflow_monthly_runrate": monthly_runrate(active_out),
            "net_monthly_runrate": round(
                monthly_runrate(active_in) - monthly_runrate(active_out), 2),
        },
        "detail": {
            "top_inflow": top(active_in),
            "top_outflow": top(active_out),
        },
        "flags": [],
    }


def _load_receipts():
    """Load receipt records from receipts.json if it exists.

    Returns a list of receipt dicts, or an empty list if unavailable.
    """
    receipts_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "receipts.json")
    if not os.path.exists(receipts_path):
        return []
    try:
        with open(receipts_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _run_reconciliation(receipts, txns):
    """Run the reconciliation engine. Returns the raw reconciliation result
    dict, or None if no receipts are available."""
    if not receipts:
        return None
    return rs.reconcile(receipts, txns)


def _reconciliation_section(recon):
    """Build a compact reconciliation summary for inclusion in the digest.

    This is NOT rendered as its own section — its findings are distributed
    into fee_fraud, cashflow, and recurring.  But a compact summary is kept
    for the vitals strip and hero card logic.
    """
    if recon is None:
        return {
            "tool": "receipt_reconciliation",
            "available": False,
            "reason": "no receipts data (run receipt_scanner or connect Gmail MCP)",
            "headline": {
                "coverage_pct": 100.0, "total_receipts": 0, "matched": 0,
                "n_discrepancies": 0, "discrepancy_amount": 0,
                "n_unmatched_receipts": 0, "unmatched_receipt_amount": 0,
                "n_unmatched_charges": 0, "unmatched_charge_amount": 0,
                "n_price_changes": 0,
            },
            "flags": [],
        }
    return rs.reconciliation_to_summary(recon)


def _dispute_section(reconciliation, fee_fraud_result, txns, *,
                     auto_draft=False, threshold=da.DEFAULT_DISPUTE_THRESHOLD,
                     bank_email=da.DEFAULT_BANK_EMAIL):
    """Run the dispute agent: process findings, check for resolutions,
    flag expired disputes.  Returns a compact section dict.

    reconciliation : raw reconciliation result (from rs.reconcile)
    fee_fraud_result : full result from ff.scan (not the trimmed section)
    """
    result = da.process_findings(
        reconciliation=reconciliation,
        fee_fraud_summary=fee_fraud_result,
        transactions=txns,
        auto_draft=auto_draft,
        threshold=threshold,
        bank_email=bank_email,
    )
    summary = result["summary"]
    return {
        "tool": "dispute_agent",
        "available": True,
        "headline": {
            "total": summary["total"],
            "open": summary["open"],
            "open_amount": summary["open_amount"],
            "resolved": summary["resolved"],
            "resolved_amount": summary["resolved_amount"],
            "expired": summary["expired"],
            "recently_resolved": summary["recently_resolved"],
            "recently_resolved_amount": summary["recently_resolved_amount"],
        },
        "detail": {
            "open_disputes": summary["open_disputes"][:5],
            "n_new_disputes": len(result["new_disputes"]),
            "n_drafts_created": len(result["drafts"]),
            "n_auto_resolved": len(result["resolved"]),
            "n_expired": len(result["expired"]),
        },
        "drafts": result["drafts"],
        "flags": _dispute_flags(result),
    }


def _dispute_flags(result):
    """Build flag lines from dispute processing result."""
    flags = []
    nd = len(result["new_disputes"])
    ndraft = len(result["drafts"])
    nr = len(result["resolved"])
    ne = len(result["expired"])

    if ndraft > 0:
        flags.append(f"{ndraft} dispute draft(s) auto-created, review in Gmail")
    elif nd > 0:
        flags.append(f"{nd} new dispute(s) tracked (run with "
                     f"--auto-draft-disputes to create Gmail drafts)")
    if nr > 0:
        amt = sum(d["amount"] for d in da.load_disputes()
                  if d["dispute_id"] in result["resolved"])
        flags.append(f"{nr} dispute(s) auto-resolved "
                     f"(refund credit detected, {money(amt)})")
    if ne > 0:
        flags.append(f"{ne} dispute(s) expired (no response after "
                     f"{da.DISPUTE_EXPIRY_DAYS} days)")

    s = result["summary"]
    if s["open"] > 0:
        flags.append(f"{s['open']} dispute(s) pending, "
                     f"{money(s['open_amount'])} outstanding")
    return flags


# ----------------------------- digest assembly ------------------------------

def build_digest(txns, *, balance, mode, forecast_days, buffer, include_burn,
                 scan_days, rules_path, auto_draft_disputes=False,
                 dispute_threshold=da.DEFAULT_DISPUTE_THRESHOLD,
                 bank_email=da.DEFAULT_BANK_EMAIL, today=None):
    """Run all deterministic cores and collate ONE combined compact digest.

    RECONCILIATION FIRST: receipts are reconciled against bank transactions
    before any analysis runs.  The reconciliation result is passed to
    fee_fraud (discrepancies + unverified charges), cashflow (pending receipts),
    and the digest vitals (coverage stat).  Receipts never appear as a
    standalone section — they are invisible infrastructure.

    JSON-serializable, ~1K tokens, NO raw transaction rows. This is the ONLY
    thing that ever feeds narration."""
    dates = [d for d in (sc.parse_date(t) for t in txns) if d]
    as_of = str(max(dates)) if dates else None

    # --- STEP 0: reconciliation (runs first, feeds everything) ---
    receipts = _load_receipts()
    recon = _run_reconciliation(receipts, txns)
    recon_summary = _reconciliation_section(recon)

    # --- STEP 1: analysis sections (receive reconciliation results) ---
    fc = _forecast_section(txns, balance, forecast_days, buffer, include_burn,
                           reconciliation=recon, today=today)
    bud = _budget_section(txns, rules_path, mode, balance=balance)
    fee = _fee_fraud_section(txns, scan_days, reconciliation=recon)
    recur = _recurring_section(txns)

    # Inject price changes from reconciliation into recurring section
    if recon and recon.get("price_changes"):
        recur.setdefault("detail", {})["price_changes"] = [
            {"merchant": p.get("merchant", "?"),
             "current": p.get("current_amount", 0),
             "previous": p.get("previous_amount", 0),
             "change": p.get("change", 0),
             "date": p.get("current_date", ""),
             "message": p.get("message", "")}
            for p in recon["price_changes"]
        ][:6]
        if recon["price_changes"]:
            recur.setdefault("flags", []).append(
                f"{len(recon['price_changes'])} price change(s) detected "
                f"from receipt history"
            )

    # --- STEP 2: dispute agent (runs after reconciliation + fee_fraud) ---
    # Pass the FULL fee_fraud scan result (not the trimmed section) so the
    # dispute agent sees all duplicates/unverified charges.
    ff_full = ff.scan(txns, days=scan_days, reconciliation=recon)
    disp = _dispute_section(recon, ff_full, txns,
                            auto_draft=auto_draft_disputes,
                            threshold=dispute_threshold,
                            bank_email=bank_email)

    # combined top-level flags: the few lines that matter most, in priority order
    flags = []
    flags += fc["flags"][:2]
    if bud["headline"]["status"] == "behind":
        h = bud["headline"]
        flags.append(f"savings pace BEHIND: {money(h['current_pace_mo'])}/mo vs "
                     f"{money(h['required_pace_mo'])}/mo needed "
                     f"(projected {money(h['projected'])}, gap {money(h['gap'])}).")
    fh = fee["headline"]
    if fh["avoidable_plus_suspect"] > 0:
        n_anom = fh.get("n_anomalies", 0)
        anom_part = (f" + {n_anom} anomal{'y' if n_anom == 1 else 'ies'} "
                     f"({money(fh['suspect'])})" if n_anom else "")
        flags.append(f"Fee/fraud: {money(fh['avoidable'])} recoverable{anom_part} "
                     f"this {fee['window']['days']}d.")
    # Reconciliation flags bubble up into the top-level flags
    flags += recon_summary.get("flags", [])[:2]
    # Dispute flags
    flags += disp.get("flags", [])[:2]

    return {
        "tool": "finance_agent",
        "as_of": as_of,
        "mode": mode,
        "window": {"start": bud["window"]["start"], "end": bud["window"]["end"]},
        "sections": {
            "forecast": fc,
            "budget": bud,
            "fee_fraud": fee,
            "recurring": recur,
            "reconciliation": recon_summary,
            "disputes": disp,
        },
        "flags": flags,
    }


def build_digest_receipts_only(rules_path="rules.md"):
    """Build a digest with only receipt data (no bank transactions).

    All bank-transaction-dependent sections (forecast, budget, fee_fraud,
    recurring) are stubbed as unavailable with a 'connect your bank' message.
    Receipts are loaded but without bank data the reconciliation is limited
    to receipt-only findings (no matching, no discrepancies).
    """
    today = str(dt.date.today())
    receipts = _load_receipts()
    # Run reconciliation with empty transactions — produces only unmatched
    # receipts (since there are no bank charges to match against).
    recon = _run_reconciliation(receipts, []) if receipts else None
    recon_summary = _reconciliation_section(recon)

    fc_stub = {
        "tool": "cashflow_forecaster",
        "available": False,
        "reason": "Connect your bank account to enable cash-flow forecasting",
        "headline": {},
        "flags": [],
    }
    bud_stub = {
        "tool": "budget_scorer",
        "available": False,
        "reason": "Connect your bank account to enable budget tracking",
        "headline": {},
        "flags": [],
    }
    fee_stub = {
        "tool": "fee_fraud_scan",
        "available": False,
        "reason": "Connect your bank account to enable fee and fraud scanning",
        "headline": {},
        "flags": [],
    }
    rec_stub = {
        "tool": "recurring",
        "available": False,
        "reason": "Connect your bank account to enable recurring-stream detection",
        "headline": {},
        "flags": [],
    }
    disp_stub = {
        "tool": "dispute_agent",
        "available": False,
        "reason": "Connect your bank account to enable dispute tracking",
        "headline": {},
        "detail": {},
        "drafts": [],
        "flags": [],
    }

    flags = recon_summary.get("flags", [])[:4]

    return {
        "tool": "finance_agent",
        "as_of": today,
        "mode": "weekly",
        "window": {"start": today, "end": today},
        "sections": {
            "forecast": fc_stub,
            "budget": bud_stub,
            "fee_fraud": fee_stub,
            "recurring": rec_stub,
            "reconciliation": recon_summary,
            "disputes": disp_stub,
        },
        "flags": flags,
    }


# ----------------------------- headline -------------------------------------

def headline_line(digest):
    """The one-line headline the contract requires: forecaster min-balance /
    overdraft days + budget pace + fee/fraud total.

    Gracefully handles sections that are unavailable (available: False) —
    each part degrades to a short UNAVAILABLE note instead of crashing on
    missing headline keys."""
    fc = digest["sections"]["forecast"]
    bud = digest["sections"]["budget"]
    fee = digest["sections"]["fee_fraud"]

    if fc.get("available") is False:
        fc_part = f"forecast UNAVAILABLE ({fc.get('reason', 'no balance')})"
    else:
        h = fc["headline"]
        fc_status = ("OVERDRAFT" if h["overdraft"]
                     else ("LOW" if h["low_balance"] else "CLEAR"))
        fc_part = (f"forecast {fc_status}: min {money(h['min_balance'])} on "
                   f"{delivery.fmt_date(h['min_date'])}"
                   + (f", safe-by {h['safe_by']}" if h["safe_by"] else ""))

    if bud.get("available") is False:
        bud_part = "savings pace UNAVAILABLE (no bank data)"
    else:
        bh = bud["headline"]
        proj = bh.get("projected")
        tail = bh.get("tailwind") or 0
        # Lead with the projection the status is actually based on. Pace alone can sit
        # below "required" yet still be AHEAD when freed obligations (tailwind) close
        # the gap — so show the projection + the freed amount, not just the raw pace.
        if proj is not None:
            tail_part = f" + {money(tail)} freed" if tail else ""
            bud_part = (f"Goal {bh['status'].upper()} — projected {money(proj)} "
                        f"by {delivery.fmt_date(bh.get('move_date', 'move'))} "
                        f"(pace {money(bh['current_pace_mo'])}/mo{tail_part}, "
                        f"{bh['pct_to_target']}% to {money(bh['target'])})")
        else:
            bud_part = (f"Goal {bh['status'].upper()} "
                        f"({money(bh['current_pace_mo'])}/mo, "
                        f"{bh['pct_to_target']}% to {money(bh['target'])})")

    if fee.get("available") is False:
        fee_part = "fee/fraud UNAVAILABLE (no bank data)"
    else:
        fh = fee["headline"]
        n_anom = fh.get("n_anomalies", 0)
        fee_part = (f"fee/fraud {money(fh['avoidable'])} recoverable"
                    + (f", {n_anom} anomal{'y' if n_anom == 1 else 'ies'}"
                       if n_anom else ""))

    recon = digest["sections"].get("reconciliation", {})
    rh = recon.get("headline", {})
    if recon.get("available", True) and rh.get("total_receipts", 0) > 0:
        rcpt_part = f"receipts {rh.get('coverage_pct', 100)}% verified"
    else:
        rcpt_part = None

    disp = digest["sections"].get("disputes", {})
    dh = disp.get("headline", {})
    if disp.get("available", False) and dh.get("open", 0) > 0:
        disp_part = (f"disputes {dh['open']} open "
                     f"({money(dh.get('open_amount', 0))})")
    else:
        disp_part = None

    parts = [fc_part, bud_part, fee_part]
    if rcpt_part:
        parts.append(rcpt_part)
    if disp_part:
        parts.append(disp_part)
    return "[HEADLINE] " + " · ".join(parts)


# ----------------------------- rendering ------------------------------------

def render(digest):
    L = []
    head = "WEEKLY" if digest["mode"] == "weekly" else "MONTHLY"
    L.append(f"# finance.mcp — UNIFIED {head} DIGEST")
    L.append(f"_window {digest['window']['start']} → {digest['window']['end']} "
             f"· as of {digest['as_of']}_\n")

    # combined flags first (what to act on)
    L.append("## What matters")
    if digest["flags"]:
        for f in digest["flags"]:
            L.append(f"- {f}")
    else:
        L.append("- Nothing flagged across forecast, budget, or fee/fraud scan.")
    L.append("")

    # --- forecast ---
    fc = digest["sections"]["forecast"]
    L.append("## Cash-flow forecast")
    if fc.get("available"):
        h = fc["headline"]
        status = ("🔴 OVERDRAFT RISK" if h["overdraft"]
                  else ("🟡 LOW-BALANCE RISK" if h["low_balance"] else "🟢 CLEAR"))
        L.append(f"- Status: **{status}**"
                 + (f" · safe-by {h['safe_by']}" if h["safe_by"] else ""))
        L.append(f"- Start {money(h['start_balance'])} → projected end "
                 f"{money(h['projected_end_balance'])} "
                 f"({h['horizon_days']}d, buffer {money(h['buffer'])})")
        L.append(f"- Min balance {money(h['min_balance'])} on {delivery.fmt_date(h['min_date'])}")
        if h["next_income"]:
            ni = h["next_income"]
            L.append(f"- Next income {money(ni['amount'])} from "
                     f"{ni['merchant'][:40]} on {delivery.fmt_date(ni['date'])}")
        for o in fc["detail"]["biggest_obligations"]:
            L.append(f"  · upcoming: {delivery.fmt_date(o['date'])} {o['merchant'][:36]} {money(o['amount'])}")
        for p in fc["detail"].get("pending_receipts", []):
            L.append(f"  · pending: {p['merchant'][:30]} {money(p['amount'])} "
                     f"({p['days_since']}d, no bank charge)")
    else:
        L.append(f"- UNAVAILABLE: {fc['reason']}")
    L.append("")

    # --- budget / savings pace ---
    bud = digest["sections"]["budget"]
    L.append("## savings pace")
    if bud.get("available") is False:
        L.append(f"- {bud.get('reason', 'Connect your bank account to enable this section.')}")
    else:
        h = bud["headline"]
        t = bud["rule_tally"]
        L.append(f"- Net saved this window: {money(h['net_saved_window'])} "
                 f"(income {money(h['income_window'])} − spend {money(h['spend_window'])})")
        L.append(f"- Running total: {money(h['running_total'])} "
                 f"({h['pct_to_target']}% of {money(h['target'])})")
        L.append(f"- Pace {money(h['current_pace_mo'])}/mo vs "
                 f"{money(h['required_pace_mo'])}/mo needed → projected "
                 f"{money(h['projected'])} by {delivery.fmt_date(h['move_date'])} → "
                 f"**{h['status'].upper()}** (gap {money(h['gap'])}, "
                 f"{h['months_remaining']} mo left)")
        L.append(f"- Cut rules: {t['on_track']}✅ {t['drifting']}⚠️ {t['slipped']}🔻")
        for r in bud["detail"]["off_track_rules"]:
            L.append(f"  · {r['leak']}: {money(r['spent'])} vs {money(r['goal'])} "
                     f"goal — {r['status']}")
    L.append("")

    # --- fee / fraud ---
    fee = digest["sections"]["fee_fraud"]
    L.append("## Fee + fraud scan")
    if fee.get("available") is False:
        L.append(f"- {fee.get('reason', 'Connect your bank account to enable this section.')}")
    else:
        fh = fee["headline"]
        L[-1] = f"## Fee + fraud scan ({fee['window']['days']}d)"
        L.append(f"- Avoidable + suspect: {money(fh['avoidable_plus_suspect'])} "
                 f"({money(fh['avoidable'])} avoidable, {money(fh['suspect'])} suspect)")
        L.append(f"- Bank fees {money(fh['fees_total'])} ({fh['n_fees']}) · "
                 f"duplicates {money(fh['dup_recoverable'])} recoverable "
                 f"({fh['n_duplicates']}) · suspect merchants {fh['n_suspect_merchants']}")
        for x in fee["detail"]["duplicates"]:
            L.append(f"  · dup: {x['merchant']} {money(x['amount'])} "
                     f"{x['dates'][0]} & {x['dates'][1]}")
        for x in fee["detail"]["fees"]:
            L.append(f"  · fee: {x['merchant']} {money(x['amount'])} {x['date']}")
        for x in fee["detail"].get("receipt_discrepancies", []):
            L.append(f"  · discrepancy: {x.get('message', x['merchant'])}")
        for x in fee["detail"].get("unverified_charges", []):
            L.append(f"  · unverified: {x.get('message', x['merchant'])}")
    L.append("")

    # --- recurring snapshot ---
    rcur = digest["sections"]["recurring"]
    L.append("## Recurring snapshot (active)")
    if rcur.get("available") is False:
        L.append(f"- {rcur.get('reason', 'Connect your bank account to enable this section.')}")
    else:
        rh = rcur["headline"]
        L.append(f"- Inflows {rh['n_active_inflow']} "
                 f"(~{money(rh['inflow_monthly_runrate'])}/mo) · "
                 f"outflows {rh['n_active_outflow']} "
                 f"(~{money(rh['outflow_monthly_runrate'])}/mo) · "
                 f"net ~{money(rh['net_monthly_runrate'])}/mo")
        for s in rcur["detail"]["top_outflow"][:5]:
            L.append(f"  · out: {s['merchant'][:30]} {money(s['avg_amount'])}/"
                     f"{s['cadence']} (~{money(s['monthly_runrate'])}/mo)")
        for p in rcur.get("detail", {}).get("price_changes", []):
            L.append(f"  · price change: {p.get('message', p['merchant'])}")
    L.append("")

    # --- reconciliation coverage (not a standalone section) ---
    recon = digest["sections"].get("reconciliation", {})
    rh = recon.get("headline", {})
    if recon.get("available", True) and rh.get("total_receipts", 0) > 0:
        L.append(f"## Receipt coverage: {rh.get('coverage_pct', 100)}% verified "
                 f"({rh.get('matched', 0)}/{rh.get('total_receipts', 0)})")
        if rh.get("n_discrepancies"):
            L.append(f"- {rh['n_discrepancies']} discrepancy(ies) "
                     f"({money(rh['discrepancy_amount'])})")
        if rh.get("n_unmatched_receipts"):
            L.append(f"- {rh['n_unmatched_receipts']} pending receipt(s) "
                     f"({money(rh['unmatched_receipt_amount'])})")
        if rh.get("n_price_changes"):
            L.append(f"- {rh['n_price_changes']} price change(s) from receipts")
    L.append("")

    # --- disputes ---
    disp = digest["sections"].get("disputes", {})
    if disp.get("available", False):
        dh = disp.get("headline", {})
        dd = disp.get("detail", {})
        L.append(f"## Disputes ({dh.get('open', 0)} open, "
                 f"{dh.get('resolved', 0)} resolved, "
                 f"{dh.get('expired', 0)} expired)")
        if dh.get("open", 0) > 0:
            L.append(f"- Open amount: {money(dh.get('open_amount', 0))}")
        if dh.get("resolved_amount", 0) > 0:
            L.append(f"- Recovered: {money(dh['resolved_amount'])}")
        if dh.get("recently_resolved", 0) > 0:
            L.append(f"- Resolved this week: {dh['recently_resolved']} "
                     f"({money(dh.get('recently_resolved_amount', 0))})")
        if dd.get("n_drafts_created", 0) > 0:
            L.append(f"- {dd['n_drafts_created']} draft(s) auto-created, "
                     f"review in Gmail")
        if dd.get("n_auto_resolved", 0) > 0:
            L.append(f"- {dd['n_auto_resolved']} auto-resolved "
                     f"(refund credit detected)")
        for d in dd.get("open_disputes", []):
            L.append(f"  · [{d['dispute_id']}] {d['merchant']} "
                     f"{money(d['amount'])} ({d['reason']}) - {d['status']}")

    return "\n".join(L)


# ----------------------------- GitHub Pages --------------------------------

# --------------------------------- main -------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Unified finance.mcp finance digest (forecast + budget + "
                    "fee/fraud + recurring).")
    ap.add_argument("transactions", nargs="?", default="transactions.json",
                    help="transactions JSON path (default transactions.json)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--weekly", action="store_true",
                   help="weekly budget cadence (default)")
    g.add_argument("--monthly", action="store_true",
                   help="monthly budget cadence")
    ap.add_argument("--balance", type=float, default=None,
                    help="starting balance for the forecaster (wins over "
                         "balance.json; if neither, forecast is reported "
                         "unavailable)")
    ap.add_argument("--days", type=int, default=DEFAULT_FORECAST_DAYS,
                    help=f"forecast horizon days (default {DEFAULT_FORECAST_DAYS})")
    ap.add_argument("--buffer", type=float, default=DEFAULT_BUFFER,
                    help=f"low-balance buffer (default {DEFAULT_BUFFER})")
    ap.add_argument("--no-burn", action="store_true",
                    help="exclude discretionary burn from the forecast")
    ap.add_argument("--scan-days", type=int, default=DEFAULT_SCAN_DAYS,
                    help=f"fee/fraud trailing window (default {DEFAULT_SCAN_DAYS})")
    ap.add_argument("--rules", default="rules.md",
                    help="rules file (budget figures + narration tone)")
    ap.add_argument("--no-voice", action="store_true",
                    help="numbers-only, zero token spend")
    ap.add_argument("--email", nargs="?", const="__self__", default=None,
                    help="email the digest via Gmail SMTP "
                         "(defaults recipient to GMAIL_ADDRESS)")
    ap.add_argument("--json", action="store_true",
                    help="print the combined digest dict as JSON and exit")
    ap.add_argument("--receipts-only", action="store_true",
                    help="receipts-only mode: bank-dependent sections show "
                         "'connect your bank' instead of requiring transactions")
    ap.add_argument("--auto-draft-disputes", action="store_true",
                    help="generate Gmail drafts for actionable dispute findings")
    ap.add_argument("--dispute-status", action="store_true",
                    help="show open dispute status and exit")
    ap.add_argument("--dispute-threshold", type=float,
                    default=da.DEFAULT_DISPUTE_THRESHOLD,
                    help=f"minimum $ for bank dispute letters "
                         f"(default {da.DEFAULT_DISPUTE_THRESHOLD})")
    ap.add_argument("--bank-email", default=da.DEFAULT_BANK_EMAIL,
                    help=f"bank dispute email (default {da.DEFAULT_BANK_EMAIL})")
    a = ap.parse_args()

    # --- dispute status shortcut ---
    if a.dispute_status:
        if a.json:
            s = da.dispute_summary()
            print(json.dumps(s, indent=2))
        else:
            print(da.render_status())
        return

    mode = "monthly" if a.monthly else "weekly"   # weekly is the default

    if a.receipts_only:
        digest = build_digest_receipts_only(rules_path=a.rules)
    else:
        txns = sc.load_transactions(a.transactions)

        # Balance: --balance wins, else balance.json, else None (forecast skipped).
        balance = a.balance
        if balance is None and os.path.exists("balance.json"):
            try:
                balance = cf.resolve_balance(None)
            except SystemExit:
                balance = None

        digest = build_digest(
            txns,
            balance=balance,
            mode=mode,
            forecast_days=a.days,
            buffer=a.buffer,
            include_burn=not a.no_burn,
            scan_days=a.scan_days,
            rules_path=a.rules,
            auto_draft_disputes=a.auto_draft_disputes,
            dispute_threshold=a.dispute_threshold,
            bank_email=a.bank_email,
        )

    if a.json:
        print(json.dumps(digest, indent=2))
        return

    scorecard = render(digest)

    # ONE narration pass over the WHOLE combined digest (not one per tool).
    voice = None
    if not a.no_voice:
        voice = delivery.narrate(digest, _tone(a.rules), mode)
    report = scorecard + (("\n\n---\n\n## Read\n" + voice) if voice else "")

    today = str(dt.date.today())
    fname = f"digest-{today}.md"
    with open(fname, "w", encoding="utf-8") as f:
        f.write(report + "\n")

    print(report)
    hl = headline_line(digest)
    print(f"\n[saved {fname} · {'voice ON' if voice else 'NO-VOICE ($0)'}]")
    print(hl)

    # Generate v3 HTML report
    if mode == "weekly":
        report_html = dtpl.render_weekly_html(digest)
    else:
        report_html = dtpl.render_monthly_html(digest)

    report_filename = f"digest-{mode}-{today}.html"

    # Save HTML report locally
    with open(report_filename, "w", encoding="utf-8") as f:
        f.write(report_html)
    print(f"  [saved {report_filename}]")

    # Email: send compact version with link when pages URL available
    if a.email is not None:
        to = None if a.email == "__self__" else a.email
        subj = delivery.digest_subject_line(digest)

        delivery.send_email(to, subj, render(digest), html=report_html)


if __name__ == "__main__":
    main()
