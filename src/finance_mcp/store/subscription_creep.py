#!/usr/bin/env python3
"""
subscription_creep.py — find recurring charges and silent price increases.

Input: JSON from bank-mcp `list_transactions` (an array of transaction objects,
each with normalized top-level fields plus a nested `rawData` Plaid object).
Pull as much history as you have and save it to a file, e.g.:
    (in Claude Code) list_transactions dateFrom 2025-06-01 limit 2000  -> save JSON

Usage:
    python3 subscription_creep.py transactions.json

It prints two things:
  1) PRICE INCREASES — recurring charges whose amount stepped up (ranked by $/yr).
  2) ALL SUBSCRIPTIONS — every recurring stream it found, with annualized cost.

Design notes (driven by the real data shape):
  - Merchant grouping uses entity_id when present, else a normalized descriptor,
    because long-tail merchants come back with null entity_id.
  - Cadence is derived from the MEDIAN gap between charges, so missing months in
    the feed don't break recurrence detection.
  - Price steps must PERSIST to count, so a one-off odd charge isn't flagged.
  - Variable-amount recurring bills (utilities) are detected and NOT mislabeled
    as price creep.
"""

import json
import re
import sys
from datetime import datetime
from statistics import median, mean


# ----------------------------- field extraction -----------------------------

def raw(t):
    return t.get("rawData") or {}


def is_outflow(t):
    """True for debits (money leaving the account)."""
    if t.get("type") == "debit":
        return True
    amt = t.get("amount")
    if isinstance(amt, (int, float)):
        return amt < 0          # top-level amount is signed; debit is negative
    return False


def amount_magnitude(t):
    r = raw(t)
    if isinstance(r.get("amount"), (int, float)):
        return abs(r["amount"])         # rawData amount is unsigned magnitude
    amt = t.get("amount")
    return abs(amt) if isinstance(amt, (int, float)) else None


def parse_date(t):
    s = t.get("date") or raw(t).get("date")
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


_NOISE = re.compile(
    r"\b(RECURRING|PURCHASE|PAYMENT|POS|DEBIT|CARD|VISA|PPD|ID|REF|AUTH|TST|SQ|"
    r"PAYPAL|GOOGLE|APL|APPLE\.COM/BILL|WWW|COM|HTTP[S]?)\b",
    re.I,
)
_NONALNUM = re.compile(r"[^A-Z0-9 ]+")
_NUMTOK = re.compile(r"\b\d{2,}\b")


def normalize(s):
    """Canonicalize a raw descriptor into a stable grouping key."""
    s = (s or "").upper()
    s = _NONALNUM.sub(" ", s)
    s = _NUMTOK.sub(" ", s)         # drop store / reference numbers
    s = _NOISE.sub(" ", s)
    toks = [w for w in s.split() if len(w) > 1]
    return " ".join(toks[:3]) or "UNKNOWN"


def merchant_key(t):
    r = raw(t)
    eid = r.get("merchant_entity_id")
    if eid:
        return ("eid", eid)
    for cp in (r.get("counterparties") or []):
        if cp.get("type") == "merchant" and cp.get("entity_id"):
            return ("eid", cp["entity_id"])
    name = r.get("merchant_name") or t.get("merchantName")
    if name:
        return ("name", normalize(name))
    return ("desc", normalize(r.get("name") or t.get("description")))


def display_name(t):
    r = raw(t)
    return (r.get("merchant_name") or t.get("merchantName")
            or r.get("name") or t.get("description") or "Unknown")


# ----------------------------- cadence + steps ------------------------------

CADENCE_BANDS = [          # (label, per_year, (min_gap, max_gap) in days)
    ("weekly",        52, (5, 9)),
    ("biweekly",      26, (11, 17)),
    # monthly lower bound is 21 (not 25) so the 18-24 day dead-zone between
    # biweekly and monthly is classified as roughly-monthly rather than dropped.
    # Real subscriptions that bill "monthly" on a calendar day frequently land a
    # median gap of 21-24 days when early partial/usage charges share the merchant
    # key (e.g. a SaaS billed on a fixed calendar day, gaps [17,15,31,32], median 24).
    ("monthly",       12, (21, 35)),
    ("every 2 months", 6, (50, 70)),
    ("quarterly",      4, (80, 100)),
    ("semiannual",     2, (160, 200)),
    ("annual",         1, (330, 400)),
]


CADENCE_DAYS = {          # canonical label -> representative interval (days);
    "weekly": 7, "biweekly": 14, "monthly": 30, "every 2 months": 61,
    "quarterly": 91, "semiannual": 182, "annual": 365, "yearly": 365,
}


def classify_cadence(gaps):
    if not gaps:
        return None, None
    m = median(gaps)
    for label, per_year, (lo, hi) in CADENCE_BANDS:
        if lo <= m <= hi:
            return label, per_year
    return None, None


def detect_steps(charges):
    """charges: list of (date, amount) sorted by date.
    Returns list of (effective_date, old_amount, new_amount) for persistent
    step changes."""
    steps = []
    level = charges[0][1]
    for i in range(1, len(charges)):
        d, a = charges[i]
        if level <= 0:
            level = a
            continue
        changed = abs(a - level) / level > 0.05 and abs(a - level) >= 0.50
        if not changed:
            continue
        # require persistence: a FOLLOWING charge must confirm the new level, so a
        # lone end-of-series usage spike can't masquerade as a permanent increase
        if i < len(charges) - 1 and abs(charges[i + 1][1] - a) / max(a, 0.01) <= 0.05:
            steps.append((d, level, a))
            level = a
        # else: transient anomaly, ignore and keep the old level
    return steps


def variable_ratio(amounts):
    """Fraction of charges with a distinct amount. Fixed subscriptions repeat the
    same amount (low ratio); utilities/groceries differ every time (high ratio).
    A subscription that STEPPED still has only a few distinct levels, so it stays
    low — which is how we tell price-creep apart from a noisy variable bill."""
    return len({round(a, 2) for a in amounts}) / len(amounts)


# --------------------------------- main -------------------------------------

def load_transactions(path):
    with open(path) as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("transactions", "data", "results", "items"):
            if isinstance(data.get(k), list):
                return data[k]
        for v in data.values():               # first list value, as a fallback
            if isinstance(v, list):
                return v
    raise SystemExit("Could not find a transaction array in the JSON.")


def money(x):
    return f"${x:,.2f}"


def main():
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python3 subscription_creep.py <transactions.json>")

    txns = load_transactions(sys.argv[1])

    # group outflows by merchant
    groups = {}
    for t in txns:
        if not is_outflow(t):
            continue
        amt = amount_magnitude(t)
        d = parse_date(t)
        if amt is None or d is None:
            continue
        key = merchant_key(t)
        groups.setdefault(key, {"name": display_name(t), "charges": []})
        groups[key]["charges"].append((d, amt))

    subscriptions = []
    for key, g in groups.items():
        charges = sorted(g["charges"])
        if len(charges) < 3:                  # need a few to establish recurrence
            continue
        gaps = [(charges[i + 1][0] - charges[i][0]).days for i in range(len(charges) - 1)]
        cadence, per_year = classify_cadence(gaps)
        if not cadence:
            continue
        amounts = [a for _, a in charges]
        variable = variable_ratio(amounts) >= 0.6     # noisy bill vs fixed subscription
        steps = [] if variable else detect_steps(charges)
        current = charges[-1][1]
        avg = mean(amounts)
        basis = avg if variable else current          # annualize variable bills on average
        span = f"{charges[0][0]:%Y-%m} to {charges[-1][0]:%Y-%m}"
        subscriptions.append({
            "name": g["name"], "cadence": cadence, "per_year": per_year,
            "current": current, "avg": avg, "annual": basis * per_year,
            "n": len(charges), "span": span, "steps": steps, "variable": variable,
        })

    # ---- Section 1: price increases (cumulative drift, fixed subs only) ----
    increases = []
    for s in subscriptions:
        if s["variable"] or not s["steps"]:
            continue
        original = s["steps"][0][1]                    # level before the first step
        latest = s["steps"][-1][2]                     # current level
        if latest <= original:                         # only flag net increases
            continue
        annual_impact = (latest - original) * s["per_year"]
        first_eff = s["steps"][0][0]
        increases.append((annual_impact, s, original, latest, first_eff, len(s["steps"])))
    increases.sort(reverse=True, key=lambda x: x[0])

    print("\n" + "=" * 68)
    print(" PRICE INCREASES  (recurring charges that crept up)")
    print("=" * 68)
    if not increases:
        print(" None detected.")
    else:
        total = sum(i[0] for i in increases)
        for annual_impact, s, original, latest, first_eff, n_steps in increases:
            since = f"since ~{first_eff:%Y-%m}"
            n_note = "" if n_steps == 1 else f" over {n_steps} increases"
            print(f"\n  {s['name']}  ({s['cadence']})")
            print(f"    {money(original)} -> {money(latest)}  {since}{n_note}")
            print(f"    +{money(latest - original)}/charge  =  +{money(annual_impact)}/yr")
            if n_steps > 1:
                trail = "  ".join(f"{d:%Y-%m}:{money(new)}" for d, _, new in s["steps"])
                print(f"    steps: {money(original)} (start)  {trail}")
            print("    -> review or cancel")
        print(f"\n  Combined silent increase: +{money(total)}/yr")

    # ---- Section 2: subscriptions split fixed vs variable ----
    fixed = sorted([s for s in subscriptions if not s["variable"]],
                   reverse=True, key=lambda s: s["annual"])
    varbl = sorted([s for s in subscriptions if s["variable"]],
                   reverse=True, key=lambda s: s["annual"])

    print("\n" + "=" * 68)
    print(" SUBSCRIPTIONS  (fixed recurring charges)")
    print("=" * 68)
    for s in fixed:
        bump = "  <- price went up" if s["steps"] and s["steps"][-1][2] > s["steps"][0][1] else ""
        print(f"\n  {s['name']}{bump}")
        print(f"    {s['cadence']}, {money(s['current'])}/charge  ~  {money(s['annual'])}/yr")
        print(f"    {s['n']} charges, {s['span']}")
    fixed_total = sum(s["annual"] for s in fixed)
    print("\n" + "-" * 68)
    print(f"  {len(fixed)} subscriptions  ~  {money(fixed_total)}/yr ({money(fixed_total / 12)}/mo)")
    print("-" * 68)

    if varbl:
        print("\n" + "=" * 68)
        print(" VARIABLE RECURRING  (regular merchants, amount varies — not cancelable subs)")
        print("=" * 68)
        for s in varbl:
            print(f"\n  {s['name']}")
            print(f"    {s['cadence']}, varies ~{money(s['avg'])}/charge  ~  {money(s['annual'])}/yr")
            print(f"    {s['n']} charges, {s['span']}")
        print()


if __name__ == "__main__":
    main()
