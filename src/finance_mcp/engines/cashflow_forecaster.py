#!/usr/bin/env python3
"""
cashflow_forecaster.py — the OVERDRAFT / CASH-FLOW FORECASTER (headline tool).

Projects the checking balance DAY BY DAY across a horizon and flags the days it
dips below a safety buffer or goes negative (overdraft), naming the charge(s)
that tip it and the 'safe-by' date you must move money in by.

ARCHITECTURE RULE (the whole point): all financial math is deterministic Python.
The model NEVER sees raw transactions — only a compact precomputed summary dict
(delivery.narrate runs Haiku on that summary alone). If a raw transaction row
ever lands in a model prompt, the build is wrong.

What it does
------------
1. Recurring streams (recurring.streams) give recurring INCOME (inflow streams,
   e.g. weekly payroll ~$800 on Thursdays) and recurring OBLIGATIONS (outflow streams,
   e.g. a car loan ~$285/mo, subscriptions). Each ACTIVE stream is rolled
   forward by its cadence from last_date to enumerate predicted occurrences over
   the horizon.
2. Optional discretionary BURN: average NON-recurring outflow per day over the
   last 60 days, applied every day (conservative; --no-burn to exclude).
3. Project balance from the starting balance day by day: each day add predicted
   incomes (+), subtract predicted obligations (-) and burn (-).
4. Flag LOW days (< buffer) and OVERDRAFT days (< 0), naming the tipping charges
   and a 'safe-by' date. Report min_balance/min_date, next income, biggest
   upcoming obligations.

Balance input (contract order):
  --balance FLOAT wins. Else read balance.json ({"balance": <float>}). Else error.

Usage:
  python3 cashflow_forecaster.py transactions.json --balance 1200
  python3 cashflow_forecaster.py transactions.json --balance 1200 --days 35 --buffer 100
  python3 cashflow_forecaster.py transactions.json --balance 1200 --no-burn
  python3 cashflow_forecaster.py transactions.json --balance 1200 --no-voice   # $0 tokens
  python3 cashflow_forecaster.py transactions.json --balance 1200 --email you@gmail.com
"""

import argparse
import json
import os
import re
import datetime as dt
from collections import defaultdict

from finance_mcp.store import subscription_creep as sc      # field extraction / grouping / cadence
from finance_mcp.engines import recurring as rec              # recurring streams (inflow + outflow)
from finance_mcp.store import obligation_registry as oblreg # confirmed forward-plan obligations
from finance_mcp.report import delivery                      # canonical narrate + send_email + money

money = delivery.money

DEFAULT_DAYS = 35
DEFAULT_BUFFER = 100.0
BURN_WINDOW_DAYS = 60

# cadence label -> days between occurrences (the median real-world interval).


# ------------------------------ balance input -------------------------------

def resolve_balance(cli_balance, balance_path="balance.json"):
    """--balance wins; else balance.json {"balance": float}; else error clearly."""
    if cli_balance is not None:
        return float(cli_balance)
    if os.path.exists(balance_path):
        with open(balance_path, encoding="utf-8") as f:
            data = json.load(f)
        b = data.get("balance")
        if isinstance(b, (int, float)):
            return float(b)
        raise SystemExit(f"{balance_path} has no numeric 'balance' field: {data!r}")
    raise SystemExit(
        "No starting balance: pass --balance FLOAT or create balance.json "
        '({"balance": <float>}).'
    )


# ------------------------------ occurrences ---------------------------------

def cadence_days(cadence):
    """Days between occurrences for a cadence label (fallback 30 if unknown)."""
    return sc.CADENCE_DAYS.get(cadence, 30)


def roll_forward(stream, as_of, horizon_end):
    """Enumerate predicted occurrence dates for one stream across (as_of, horizon_end].

    Steps forward by the stream's cadence from its last_date, emitting every
    occurrence strictly after as_of and on/before horizon_end. We never emit on or
    before as_of (those are history already in the starting balance).
    """
    step = cadence_days(stream["cadence"])
    if step <= 0:
        return []
    occ = []
    d = stream["last_date"]
    # advance to the first occurrence strictly after as_of
    guard = 0
    while d <= as_of and guard < 100000:
        d = d + dt.timedelta(days=step)
        guard += 1
    while d <= horizon_end and guard < 100000:
        occ.append(d)
        d = d + dt.timedelta(days=step)
        guard += 1
    return occ


# Money that LEAVES checking but isn't day-to-day consumption: account/app
# transfers, Zelle/Venmo/Cash App, Apple Cash, ATM cash, wires. Counting these
# as "discretionary burn" both double-counts (cash gets spent elsewhere) and
# inflates the daily rate. Detected by Plaid category first, merchant text second.
_BURN_EXCLUDE_CAT = ("TRANSFER_OUT", "TRANSFER_IN")
_BURN_EXCLUDE_MERCHANT = re.compile(
    r"\b(zelle|venmo|cash\s?app|apple\s?cash|atm|wire|account transfer|"
    r"online transfer|to savings|withdrawal)\b", re.I)


def _is_transfer_like(t):
    cat = (t.get("category") or "")
    if cat.startswith(_BURN_EXCLUDE_CAT):
        return True
    blob = f"{t.get('merchantName') or ''} {t.get('description') or ''}"
    return bool(_BURN_EXCLUDE_MERCHANT.search(blob))


def _winsorize(values, p=0.90):
    """Clamp values above the p-th percentile down to it, so a single one-off
    lump (a one-time bill, a hardware purchase) isn't amortized into a perpetual
    daily rate. Returns a new list."""
    if len(values) < 5:
        return list(values)
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round(p * len(s))) - 1))
    cap = s[idx]
    return [min(v, cap) for v in values]


def discretionary_burn(txns, as_of, window_days=BURN_WINDOW_DAYS):
    """Robust avg NON-recurring CONSUMPTION outflow per day over the last
    `window_days` before as_of.

    Recurring outflows (any merchant_key that forms an active OR inactive stream)
    are excluded so they aren't double-counted against the rolled-forward
    obligations. Transfers / Zelle / ATM cash are excluded (not consumption), and
    one-off lumps are winsorized so a single large bill doesn't become a
    perpetual daily burn. Returns (daily_burn, total_in_window, n_recurring_keys).
    """
    streams = rec.streams(txns)
    recurring_keys = {tuple(s["key"]) for s in streams if s["direction"] == "outflow"}

    start = as_of - dt.timedelta(days=window_days - 1)
    amounts = []
    for t in txns:
        if not sc.is_outflow(t):
            continue
        d = sc.parse_date(t)
        amt = sc.amount_magnitude(t)
        if d is None or amt is None:
            continue
        if not (start <= d <= as_of):
            continue
        if sc.merchant_key(t) in recurring_keys:
            continue                       # belongs to a recurring stream
        if _is_transfer_like(t):
            continue                       # transfer / cash movement, not consumption
        amounts.append(amt)

    capped = _winsorize(amounts, 0.90)     # tame one-off lumps
    total = round(sum(capped), 2)
    daily = total / window_days if window_days > 0 else 0.0
    return round(daily, 2), total, len(recurring_keys)


# ------------------------------ projection ----------------------------------

def project(start_balance, as_of, days, buffer, income_streams,
            obligation_streams, daily_burn):
    """Deterministic day-by-day balance projection.

    For each day d in (as_of, as_of+days]:
        balance += sum(incomes due on d)
        balance -= sum(obligations due on d)
        balance -= daily_burn
    (Burn is applied AFTER scheduled flows, so an obligation that lands on a day
    is measured against the post-income / pre-burn balance the same way every day.)

    Returns a dict with the curve and derived flags. Pure; no I/O.
    """
    horizon_end = as_of + dt.timedelta(days=days)

    # Build per-day event maps from the streams.
    income_on = defaultdict(list)        # date -> [(name, amount)]
    oblig_on = defaultdict(list)
    for s in income_streams:
        for d in roll_forward(s, as_of, horizon_end):
            income_on[d].append((s["merchant"], round(s["avg_amount"], 2)))
    for s in obligation_streams:
        for d in roll_forward(s, as_of, horizon_end):
            oblig_on[d].append((s["merchant"], round(s["avg_amount"], 2)))

    balance = round(float(start_balance), 2)
    curve = []                           # [{date, income, obligations, burn, end_balance, events}]
    low_days = []                        # end_balance < buffer
    overdraft_days = []                  # end_balance < 0

    for i in range(1, days + 1):
        d = as_of + dt.timedelta(days=i)
        incs = income_on.get(d, [])
        obls = oblig_on.get(d, [])
        inc_sum = round(sum(a for _, a in incs), 2)
        obl_sum = round(sum(a for _, a in obls), 2)

        balance = round(balance + inc_sum, 2)
        balance = round(balance - obl_sum, 2)
        balance = round(balance - daily_burn, 2)

        events = ([{"dir": "in", "name": n, "amount": a} for n, a in incs]
                  + [{"dir": "out", "name": n, "amount": a} for n, a in obls])
        row = {
            "date": str(d),
            "income": inc_sum,
            "obligations": obl_sum,
            "burn": round(daily_burn, 2),
            "end_balance": balance,
            "events": events,
        }
        curve.append(row)

        if balance < 0:
            overdraft_days.append(row)
        if balance < buffer:
            low_days.append(row)

    # min balance over the curve
    if curve:
        min_row = min(curve, key=lambda r: r["end_balance"])
        min_balance, min_date = min_row["end_balance"], min_row["date"]
    else:
        min_balance, min_date = balance, str(as_of)

    return {
        "horizon_end": str(horizon_end),
        "curve": curve,
        "low_days": low_days,
        "overdraft_days": overdraft_days,
        "min_balance": min_balance,
        "min_date": min_date,
        "end_balance": balance,
    }


def _tipping_charges(row):
    """The outflow charges that landed on a flagged day (what tipped it)."""
    return [{"name": e["name"], "amount": e["amount"]}
            for e in row["events"] if e["dir"] == "out"]


def safe_by_date(first_breach_date):
    """The last day you must move money in: the day BEFORE the first breach.
    first_breach_date is a 'YYYY-MM-DD' string. Returns a 'YYYY-MM-DD' string."""
    d = dt.datetime.strptime(first_breach_date, "%Y-%m-%d").date()
    return str(d - dt.timedelta(days=1))


# ------------------------------ summary dict --------------------------------

def build_summary(txns, start_balance, days, buffer, include_burn=True,
                  reconciliation=None, registry=None, discretionary_budget=None,
                  today=None):
    """Reduce everything to the contract summary dict. No raw rows. ~1K tokens.

    Forward-plan model: when `registry` (obligations.json) and
    `discretionary_budget` (the monthly ceiling the user chose) are supplied, the
    projection debits CONFIRMED obligations + the chosen budget — it does NOT
    extrapolate historical discretionary spend. Historical burn is kept only as
    an "old-pace" reference track. Without them, falls back to the legacy
    detected-streams + historical-burn behavior.

    reconciliation : dict | None
        If provided, a reconciliation result from receipt_scanner.reconcile().
    """
    dates = [sc.parse_date(t) for t in txns if sc.parse_date(t)]
    latest = max(dates)
    # Anchor the forecast at TODAY when the balance is live but the transaction
    # feed lags (Plaid posts a paycheck to the balance days before the row shows
    # up). Otherwise we'd project a paycheck that already landed. `today` defaults
    # to the last transaction date (preserves deterministic tests).
    as_of = max(latest, today) if today and today >= latest else latest

    streams = rec.streams(txns)
    income_streams = [s for s in streams if s["direction"] == "inflow" and s["is_active"]]

    # Obligations from the confirmed registry (forward plan) when available,
    # else detected recurring streams (legacy fallback).
    if registry and registry.get("obligations"):
        obligation_streams = oblreg.registry_to_streams(registry, txns, as_of)
        obligation_floor = oblreg.obligation_floor_monthly(registry, as_of)
    else:
        obligation_streams = [s for s in streams if s["direction"] == "outflow" and s["is_active"]]
        obligation_floor = None

    # Historical discretionary burn — kept ONLY as a reference / old-pace track.
    if include_burn:
        hist_daily, burn_total, n_rec = discretionary_burn(txns, as_of)
    else:
        hist_daily, burn_total, _n_rec = 0.0, 0.0, 0

    # THE INVERSION: debit the CHOSEN forward budget, not the past 60 days of habit.
    if discretionary_budget is not None:
        _budget_monthly = round(float(discretionary_budget), 2)
        plan_daily = _budget_monthly / 30.44        # full precision for projection
        budget_driven = True
    else:
        _budget_monthly = None
        plan_daily = hist_daily
        budget_driven = False
    daily_burn = plan_daily

    proj = project(start_balance, as_of, days, buffer,
                   income_streams, obligation_streams, plan_daily)
    # Old-pace shadow track: same obligation+income spine, historical discretionary.
    proj_oldpace = project(start_balance, as_of, days, buffer,
                           income_streams, obligation_streams, hist_daily)

    # --- next income (first predicted inflow occurrence) ---
    horizon_end = as_of + dt.timedelta(days=days)
    next_income = None
    next_income_date = None
    for s in income_streams:
        for d in roll_forward(s, as_of, horizon_end):
            if next_income_date is None or d < next_income_date:
                next_income_date = d
                next_income = {"date": str(d), "merchant": s["merchant"],
                               "amount": round(s["avg_amount"], 2)}
            break  # only the first occurrence of each stream matters for "next"

    # --- biggest upcoming obligations (single predicted occurrences, ranked) ---
    upcoming = []
    for s in obligation_streams:
        for d in roll_forward(s, as_of, horizon_end):
            upcoming.append({"date": str(d), "merchant": s["merchant"],
                             "amount": round(s["avg_amount"], 2)})
    upcoming.sort(key=lambda x: (-x["amount"], x["date"]))
    biggest_obligations = upcoming[:6]

    # --- flags: name the tipping charges + safe-by dates ---
    low_flags, od_flags = [], []
    for row in proj["low_days"]:
        low_flags.append({"date": row["date"], "end_balance": row["end_balance"],
                          "tipped_by": _tipping_charges(row)})
    for row in proj["overdraft_days"]:
        od_flags.append({"date": row["date"], "end_balance": row["end_balance"],
                         "tipped_by": _tipping_charges(row)})

    flags = []
    safe_by = None
    first_low = proj["low_days"][0] if proj["low_days"] else None
    first_od = proj["overdraft_days"][0] if proj["overdraft_days"] else None
    if first_od:
        sb = safe_by_date(first_od["date"])
        safe_by = sb
        names = ", ".join(c["name"] for c in _tipping_charges(first_od)) or "discretionary burn"
        flags.append(f"OVERDRAFT projected {delivery.fmt_date(first_od['date'])} "
                     f"(ends {money(first_od['end_balance'])}) — tipped by {names}; "
                     f"move money in by {sb}.")
    if first_low:
        sb_low = safe_by_date(first_low["date"])
        if safe_by is None:
            safe_by = sb_low
        names = ", ".join(c["name"] for c in _tipping_charges(first_low)) or "discretionary burn"
        flags.append(f"LOW BALANCE projected {delivery.fmt_date(first_low['date'])} "
                     f"(ends {money(first_low['end_balance'])} < {money(buffer)} buffer) — "
                     f"tipped by {names}; safe-by {sb_low}.")
    if not flags:
        flags.append(f"Clear: balance stays at or above the {money(buffer)} buffer "
                     f"for the full {days}-day horizon (min {money(proj['min_balance'])} "
                     f"on {delivery.fmt_date(proj['min_date'])}).")

    # --- pending receipts (from reconciliation) ---
    pending_receipts = []
    if reconciliation:
        for ur in reconciliation.get("unmatched_receipts", []):
            if ur.get("status") == "pending_or_declined":
                pending_receipts.append({
                    "merchant": ur.get("merchant", "?"),
                    "amount": ur.get("amount", 0),
                    "date": ur.get("date", ""),
                    "days_since": ur.get("days_since", 0),
                    "message": ur.get("message", ""),
                })
        if pending_receipts:
            total_pending = sum(p["amount"] for p in pending_receipts)
            flags.append(
                f"{len(pending_receipts)} receipt(s) with no bank charge "
                f"({money(total_pending)}) — may be pending or declined."
            )

    summary = {
        "tool": "cashflow_forecaster",
        "as_of": str(as_of),
        "window": {"start": str(as_of), "end": proj["horizon_end"]},
        "headline": {
            "start_balance": round(float(start_balance), 2),
            "buffer": round(float(buffer), 2),
            "horizon_days": days,
            "projected_end_balance": proj["end_balance"],
            "min_balance": proj["min_balance"],
            "min_date": proj["min_date"],
            "overdraft": bool(proj["overdraft_days"]),
            "low_balance": bool(proj["low_days"]),
            "safe_by": safe_by,
            "daily_burn": round(daily_burn, 2),
            "burn_window_total": burn_total,
            "burn_window_days": BURN_WINDOW_DAYS if include_burn else 0,
            "next_income": next_income,
            "n_pending_receipts": len(pending_receipts),
            # --- forward-plan model ---
            "budget_driven": budget_driven,
            "discretionary_monthly": _budget_monthly if _budget_monthly is not None else round(plan_daily * 30.44, 2),
            "historical_discretionary_monthly": round(hist_daily * 30.44, 2),
            "obligation_floor_monthly": obligation_floor,
            "tracks": {
                "plan": {"min_balance": proj["min_balance"],
                         "end_balance": proj["end_balance"],
                         "overdraft": bool(proj["overdraft_days"])},
                "old_pace": {"min_balance": proj_oldpace["min_balance"],
                             "end_balance": proj_oldpace["end_balance"],
                             "overdraft": bool(proj_oldpace["overdraft_days"])},
                "monthly_gap": round((hist_daily - plan_daily) * 30.44, 2),
            },
        },
        "detail": {
            "income_streams": [
                {"merchant": s["merchant"], "cadence": s["cadence"],
                 "avg_amount": round(s["avg_amount"], 2), "last_date": str(s["last_date"])}
                for s in income_streams
            ],
            "obligation_streams": [
                {"merchant": s["merchant"], "cadence": s["cadence"],
                 "avg_amount": round(s["avg_amount"], 2), "last_date": str(s["last_date"])}
                for s in obligation_streams
            ],
            "biggest_obligations": biggest_obligations,
            "low_days": low_flags[:10],
            "overdraft_days": od_flags[:10],
            "pending_receipts": pending_receipts[:6],
            # Real day-by-day balance curve for the trajectory chart (paydays,
            # obligation cliffs) — start anchor first, then each projected day.
            "curve": ([{"date": str(as_of), "balance": round(float(start_balance), 2),
                        "income": 0.0, "obligations": 0.0}]
                      + [{"date": r["date"], "balance": r["end_balance"],
                          "income": r["income"], "obligations": r["obligations"]}
                         for r in proj["curve"]]),
        },
        "flags": flags,
    }
    return summary, proj


# ------------------------------ rendering -----------------------------------

def render(summary):
    h = summary["headline"]
    L = []
    L.append("# finance.mcp — CASH-FLOW / OVERDRAFT FORECAST")
    L.append(f"_horizon {summary['window']['start']} → {summary['window']['end']} "
             f"· {h['horizon_days']} days · as of {summary['as_of']}_\n")

    L.append("## Headline")
    L.append(f"- Starting balance: {money(h['start_balance'])} · buffer {money(h['buffer'])}")
    L.append(f"- Projected end balance: {money(h['projected_end_balance'])}")
    L.append(f"- Min balance: {money(h['min_balance'])} on {delivery.fmt_date(h['min_date'])}")
    status = ("🔴 OVERDRAFT RISK" if h["overdraft"]
              else ("🟡 LOW-BALANCE RISK" if h["low_balance"] else "🟢 CLEAR"))
    L.append(f"- Status: **{status}**" + (f" · safe-by {h['safe_by']}" if h["safe_by"] else ""))
    if h["next_income"]:
        ni = h["next_income"]
        L.append(f"- Next income: {money(ni['amount'])} from {ni['merchant'][:40]} on {delivery.fmt_date(ni['date'])}")
    L.append(f"- Est. daily discretionary burn: {money(h['daily_burn'])}"
             + (f" (from {money(h['burn_window_total'])} over {h['burn_window_days']}d)"
                if h["burn_window_days"] else " (excluded, --no-burn)"))

    d = summary["detail"]
    L.append("\n## Flags")
    for f in summary["flags"]:
        L.append(f"- {f}")

    if d["overdraft_days"]:
        L.append("\n## Overdraft days")
        for x in d["overdraft_days"]:
            tip = ", ".join(f"{c['name'][:28]} {money(c['amount'])}" for c in x["tipped_by"]) or "burn"
            L.append(f"- 🔴 {delivery.fmt_date(x['date'])}: ends {money(x['end_balance'])} — {tip}")
    if d["low_days"]:
        L.append("\n## Low-balance days")
        for x in d["low_days"]:
            tip = ", ".join(f"{c['name'][:28]} {money(c['amount'])}" for c in x["tipped_by"]) or "burn"
            L.append(f"- 🟡 {delivery.fmt_date(x['date'])}: ends {money(x['end_balance'])} — {tip}")

    if d["biggest_obligations"]:
        L.append("\n## Biggest upcoming obligations")
        for o in d["biggest_obligations"]:
            L.append(f"- {delivery.fmt_date(o['date'])}: {o['merchant'][:40]} {money(o['amount'])}")

    L.append("\n## Recurring streams in play")
    L.append(f"- Income ({len(d['income_streams'])}): "
             + (", ".join(f"{s['merchant'][:24]} {money(s['avg_amount'])}/{s['cadence']}"
                          for s in d["income_streams"]) or "none active"))
    L.append(f"- Obligations ({len(d['obligation_streams'])}): "
             + (", ".join(f"{s['merchant'][:24]} {money(s['avg_amount'])}/{s['cadence']}"
                          for s in d["obligation_streams"][:10]) or "none active"))
    return "\n".join(L)


# ------------------------------ tone (for narrate) --------------------------

def _tone_from_rules(rules_path="rules.md"):
    """Best-effort: pull the 'How to read me' tone block from rules.md if present.
    Forecaster doesn't depend on rules.md, so this degrades to a sensible default."""
    try:
        from finance_mcp.engines import budget_scorer as bs
        return bs.parse_rules(rules_path)["tone"]
    except Exception:
        return ("Direct and specific, blunt where there's risk — a mirror, not a "
                "warden. Tie back to the savings target and goal date.")


# --------------------------------- main -------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Overdraft / cash-flow forecaster")
    ap.add_argument("transactions")
    ap.add_argument("--balance", type=float, default=None,
                    help="starting balance (wins over balance.json)")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS, help="horizon (default 35)")
    ap.add_argument("--buffer", type=float, default=DEFAULT_BUFFER, help="low-balance buffer (default 100)")
    ap.add_argument("--no-burn", action="store_true", help="exclude discretionary burn")
    ap.add_argument("--rules", default="rules.md", help="rules file for tone (optional)")
    ap.add_argument("--no-voice", action="store_true", help="numbers only, $0 tokens")
    ap.add_argument("--email", nargs="?", const="__self__", default=None)
    a = ap.parse_args()

    txns = sc.load_transactions(a.transactions)
    start_balance = resolve_balance(a.balance)

    summary, _proj = build_summary(txns, start_balance, a.days, a.buffer,
                                   include_burn=not a.no_burn)

    scorecard = render(summary)
    voice = None
    if not a.no_voice:
        tone = _tone_from_rules(a.rules)
        voice = delivery.narrate(summary, tone, "cashflow")
    report = scorecard + (("\n\n---\n\n## Read\n" + voice) if voice else "")

    today = str(dt.date.today())
    fname = f"report-cashflow-{today}.md"
    with open(fname, "w", encoding="utf-8") as f:
        f.write(report + "\n")

    print(report)
    h = summary["headline"]
    status = ("OVERDRAFT" if h["overdraft"] else ("LOW" if h["low_balance"] else "CLEAR"))
    print(f"\n[saved {fname} · {'voice ON' if voice else 'NO-VOICE ($0)'}]")
    print(f"[HEADLINE] start {money(h['start_balance'])} → min {money(h['min_balance'])} "
          f"on {delivery.fmt_date(h['min_date'])} · {status}"
          + (f" · safe-by {h['safe_by']}" if h["safe_by"] else "")
          + f" · end {money(h['projected_end_balance'])}")

    if a.email is not None:
        to = None if a.email == "__self__" else a.email
        subj = (f"finance.mcp — Cash-Flow Forecast {today} "
                f"[{status}{(' safe-by ' + h['safe_by']) if h['safe_by'] else ''}]")
        delivery.send_email(to, subj, report)


if __name__ == "__main__":
    main()
