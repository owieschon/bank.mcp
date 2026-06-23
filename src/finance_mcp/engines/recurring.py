#!/usr/bin/env python3
"""
recurring.py — recurring STREAMS (the 'recurring' half of the finance extension).

Detects every recurring money stream in a transaction set — both OUTFLOWS
(subscriptions, obligations) and INFLOWS (paychecks, transfers in) — by grouping
on subscription_creep's merchant key and classifying cadence from the median gap.

Reuses subscription_creep (imported, never reimplemented) for all field
extraction, grouping, and cadence math. Nothing is duplicated here except the
stream-shaping and inflow/outflow split, which subscription_creep does not do.

A stream needs:
  - n >= 3 charges (enough to establish recurrence), and
  - a sc.classify_cadence hit on the median gap.

Per-stream record (see streams()):
  {key, merchant, direction, cadence, per_year, last_amount, avg_amount,
   first_date, last_date, n, is_active, next_date}

Usage:
    python3 recurring.py transactions.json
prints inflow + outflow streams sorted by annualized amount.
"""

import sys
from collections import defaultdict
from datetime import timedelta
from statistics import median, mean

from finance_mcp.store import subscription_creep as sc   # reuse grouping / cadence / field extraction


# ------------------------------- core detection -----------------------------

def streams(txns):
    """Detect recurring streams (inflow + outflow) in a transaction list.

    Returns a list of stream dicts. direction is 'inflow' for credits
    (sc.is_outflow False) and 'outflow' for debits (sc.is_outflow True).
    Inflows and outflows are grouped SEPARATELY even when they share a merchant
    key, so a merchant you both pay and get refunded by yields two streams.
    """
    # group by (direction, merchant_key); keep raw charge tuples per group
    groups = defaultdict(lambda: {"name": None, "charges": []})
    for t in txns:
        amt = sc.amount_magnitude(t)
        d = sc.parse_date(t)
        if amt is None or d is None:
            continue
        direction = "outflow" if sc.is_outflow(t) else "inflow"
        gkey = (direction, sc.merchant_key(t))
        g = groups[gkey]
        if g["name"] is None:
            g["name"] = sc.display_name(t)
        g["charges"].append((d, amt))

    # dataset max date anchors the is_active recency test
    all_dates = [sc.parse_date(t) for t in txns if sc.parse_date(t)]
    if not all_dates:
        return []
    max_date = max(all_dates)

    out = []
    for (direction, mkey), g in groups.items():
        charges = sorted(g["charges"])
        if len(charges) < 3:                       # need a few to establish recurrence
            continue
        gaps = [(charges[i + 1][0] - charges[i][0]).days for i in range(len(charges) - 1)]
        cadence, per_year = sc.classify_cadence(gaps)
        if not cadence:
            continue

        amounts = [a for _, a in charges]
        first_date = charges[0][0]
        last_date = charges[-1][0]
        med_gap = median(gaps)

        # next expected charge = last seen + median gap
        next_date = last_date + timedelta(days=round(med_gap))

        # active if the most recent charge is within ~1.5 median-gaps of the
        # dataset's max date (i.e. it hasn't gone silent for more than ~1.5 cycles)
        is_active = (max_date - last_date).days <= 1.5 * med_gap

        last_amount = round(charges[-1][1], 2)
        avg_amount = round(mean(amounts), 2)

        out.append({
            "key": list(mkey),                     # JSON-serializable form of the tuple
            "merchant": g["name"],
            "direction": direction,
            "cadence": cadence,
            "per_year": per_year,
            "last_amount": last_amount,
            "avg_amount": avg_amount,
            "first_date": first_date,
            "last_date": last_date,
            "n": len(charges),
            "is_active": is_active,
            "next_date": next_date,
        })
    return out


def annualized(s):
    """Annual run-rate of a stream (avg amount * cadences/yr)."""
    return s["avg_amount"] * s["per_year"]


# --------------------------------- rendering --------------------------------

def _print_section(title, rows):
    print("\n" + "=" * 68)
    print(f" {title}")
    print("=" * 68)
    if not rows:
        print(" None detected.")
        return
    total = 0.0
    for s in sorted(rows, reverse=True, key=annualized):
        ann = annualized(s)
        total += ann
        active = "" if s["is_active"] else "  (inactive)"
        print(f"\n  {s['merchant']}  ({s['cadence']}){active}")
        print(f"    {sc.money(s['last_amount'])}/charge  ~  {sc.money(ann)}/yr")
        print(f"    {s['n']} charges, {s['first_date']:%Y-%m} to {s['last_date']:%Y-%m}"
              f"  ·  next ~{s['next_date']:%Y-%m-%d}")
    print("\n" + "-" * 68)
    print(f"  {len(rows)} streams  ~  {sc.money(total)}/yr ({sc.money(total / 12)}/mo)")
    print("-" * 68)


def main():
    if len(sys.argv) < 2:
        raise SystemExit("Usage: python3 recurring.py <transactions.json>")
    txns = sc.load_transactions(sys.argv[1])
    found = streams(txns)
    inflows = [s for s in found if s["direction"] == "inflow"]
    outflows = [s for s in found if s["direction"] == "outflow"]
    _print_section("INFLOW STREAMS  (paychecks, transfers in)", inflows)
    _print_section("OUTFLOW STREAMS  (subscriptions, obligations)", outflows)


if __name__ == "__main__":
    main()
