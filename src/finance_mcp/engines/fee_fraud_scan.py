#!/usr/bin/env python3
"""
fee_fraud_scan.py — fee + fraud/error hunter for the finance.mcp suite.

Over a trailing window (--days, default 30, ending at the dataset's max date),
it surfaces three classes of recoverable / suspect money:

  1. BANK FEES        — any outflow whose refined category starts with BANK_FEES
                        (maintenance fees, foreign-transaction fees). Summed and
                        listed; these are almost always avoidable.
  2. DUPLICATE CHARGES— same merchant + same amount within 3 days. Reuses the
                        budget_scorer duplicate approach, but keys on
                        sc.merchant_key (entity-id stable) so "Anthropic" charged
                        twice on the same day is caught even when the descriptor
                        text drifts. The SECOND charge of each pair is the
                        recoverable amount (the likely double-bill).
  3. SUSPICIOUS /     — merchants whose refined category is low-confidence, OR
     UNRECOGNIZED       categories the user has flagged as not-theirs (alcohol /
                        liquor — the user does not drink). Uses
                        merchant_categorizer.get_category for the refined label
                        and confidence, and the categorizer's own REVIEW_CATEGORIES
                        for the not-theirs list, so the policy lives in one place.

It reports an "avoidable + suspect $ this window" headline: fees + duplicate
second-charges (avoidable) plus alcohol/low-confidence spend (suspect).

ARCHITECTURE (load-bearing): all money/date math is deterministic Python reusing
subscription_creep + merchant_categorizer. The model NEVER sees raw transactions
— only the compact summary dict, narrated via delivery.narrate. --no-voice spends
$0 tokens.

Usage:
  python3 fee_fraud_scan.py transactions.json
  python3 fee_fraud_scan.py transactions.json --days 90
  python3 fee_fraud_scan.py transactions.json --no-voice        # $0 tokens
  python3 fee_fraud_scan.py transactions.json --email you@x.com  # Gmail SMTP
"""

import argparse
import datetime as dt
import json
from collections import defaultdict

from finance_mcp.store import subscription_creep as sc
from finance_mcp.store import merchant_categorizer as mc
from finance_mcp.report import delivery

# Same-merchant + same-amount within this many days = a likely duplicate bill.
DUP_WINDOW_DAYS = 3

# Categories where repeated identical small charges are EXPECTED behavior, not
# double-bills: vending machines / micro-snack merchants legitimately ring up the
# same item at the same price on consecutive days. Flagging those as "duplicate
# charges → recoverable" is a false positive (it inflates recoverable $ and tells
# the user to dispute a snack they actually bought twice). Excluded from dup
# detection; they still surface in recurring/categorizer where they belong.
DUP_EXCLUDE_CATEGORIES = {"FOOD_AND_DRINK_VENDING_MACHINES"}


# ------------------------------- windowing ----------------------------------

def window_bounds(txns, days):
    """Trailing `days`-wide window ending at the dataset's max transaction date.

    Returns (start, end) inclusive dates. end is the latest date present;
    start is end - (days - 1) so a --days of 30 covers exactly 30 calendar
    days inclusive of the end date.
    """
    dates = [d for d in (sc.parse_date(t) for t in txns) if d]
    if not dates:
        raise SystemExit("No dated transactions found.")
    end = max(dates)
    start = end - dt.timedelta(days=days - 1)
    return start, end


def _in_window(t, start, end):
    d = sc.parse_date(t)
    return d is not None and start <= d <= end


# ------------------------------- detectors ----------------------------------
# Each returns plain data (no raw rows); the caller assembles the summary dict.

def _is_fee(t):
    """A bank fee, identified the way the bank itself tags it: transaction_code
    'bank charge' or Plaid PFC primary == BANK_FEES. A refined-category/name match
    silently missed real maintenance + foreign-transaction fees (they came back
    BANK_FEES in the raw PFC but not the refined label), so key off the bank's tag.
    """
    r = sc.raw(t)
    if r.get("transaction_code") == "bank charge":
        return True
    return (r.get("personal_finance_category") or {}).get("primary") == "BANK_FEES"


def find_fees(win, overrides=None):
    """Bank fees in the window (see _is_fee) — real, recoverable/avoidable dollars.
    Returns (list_of_fee_dicts, total)."""
    fees = []
    total = 0.0
    for t in win:
        if not sc.is_outflow(t):
            continue
        amt = sc.amount_magnitude(t)
        if amt is None or not _is_fee(t):
            continue
        pfc = (sc.raw(t).get("personal_finance_category") or {}).get("detailed", "")
        fees.append({
            "merchant": sc.display_name(t),
            "amount": round(amt, 2),
            "date": str(sc.parse_date(t)),
            "category": pfc or "BANK_FEES",
        })
        total += amt
    fees.sort(key=lambda f: (f["date"], -f["amount"]))
    return fees, round(total, 2)


def find_duplicates(win, overrides=None):
    """Same merchant (by sc.merchant_key) + same amount within DUP_WINDOW_DAYS.

    Returns (list_of_dup_dicts, recoverable_total). The recoverable amount per
    pair is the SECOND charge — the likely double-bill you'd dispute. Each
    consecutive close pair is reported once; a triple yields two pairs.

    Vending-machine / micro-snack merchants (DUP_EXCLUDE_CATEGORIES) are skipped:
    buying the same $6.47 snack two days running is not a double-bill, and
    flagging it as recoverable is a false positive.
    """
    if overrides is None:
        overrides = mc.load_overrides()
    bykey = defaultdict(list)   # (merchant_key, rounded_amount) -> [entry dicts]
    for t in win:
        if not sc.is_outflow(t):
            continue
        amt = sc.amount_magnitude(t)
        d = sc.parse_date(t)
        if amt is None or d is None:
            continue
        if mc.get_category(t, overrides)["category"] in DUP_EXCLUDE_CATEGORIES:
            continue                     # expected micro-repeat, not a double-bill
        ref = (t.get("reference") or t.get("description") or "")
        # Bank-flagged RECURRING charges are subscriptions/renewals, not accidental
        # double-bills. Two same-price recurring charges the same day = two
        # different items (e.g. two domain renewals), NOT a duplicate to dispute.
        if "RECURRING" in ref.upper():
            continue
        bykey[(sc.merchant_key(t), round(amt, 2))].append(
            {"date": d, "name": sc.display_name(t),
             "id": t.get("id"), "reference": ref})

    dups = []
    recoverable = 0.0
    for (mkey, amt), entries in bykey.items():
        entries.sort(key=lambda e: e["date"])
        for i in range(len(entries) - 1):
            e0, e1 = entries[i], entries[i + 1]
            if (e1["date"] - e0["date"]).days <= DUP_WINDOW_DAYS:
                # Distinct references = almost certainly two different purchases.
                distinct_refs = (e0["reference"] != e1["reference"]
                                 and e0["reference"] and e1["reference"])
                dups.append({
                    "merchant": e0["name"],
                    "amount": amt,
                    "dates": [str(e0["date"]), str(e1["date"])],
                    "gap_days": (e1["date"] - e0["date"]).days,
                    "txn_ids": [e0["id"], e1["id"]],          # drillable to the atom
                    "references": [e0["reference"], e1["reference"]],
                    "confidence": "low" if distinct_refs else "medium",
                    "recoverable": 0.0 if distinct_refs else amt,
                })
                if not distinct_refs:
                    recoverable += amt
    dups.sort(key=lambda x: (-x["amount"], x["dates"][0]))
    return dups, round(recoverable, 2)


def find_suspicious(win, overrides):
    """Per-merchant suspect spend in the window. Two buckets:

      - not_theirs  : refined OR raw-PFC category is in mc.REVIEW_CATEGORIES
                      (alcohol/liquor — the user does not drink). Surfaced on the
                      RAW pfc too, so a tavern tab refinement re-files as
                      RESTAURANT is still caught.
      - low_conf    : merchant_categorizer.get_category confidence == "low".

    Returns (list_of_suspect_dicts, not_theirs_total, low_conf_total).
    """
    by_merchant = defaultdict(lambda: {
        "name": None, "category": None, "n": 0, "total": 0.0,
        "reasons": set(),
    })
    not_theirs_total = 0.0
    low_conf_total = 0.0

    for t in win:
        if not sc.is_outflow(t):
            continue
        amt = sc.amount_magnitude(t)
        if amt is None:
            continue
        res = mc.get_category(t, overrides)
        cat = res["category"]
        raw_pfc = mc._pfc(t)
        k = mc._key_str(t)
        m = by_merchant[k]
        m["name"] = sc.display_name(t)
        m["category"] = cat
        m["n"] += 1
        m["total"] += amt

        flagged = False
        # not-theirs: policy lives in merchant_categorizer.REVIEW_CATEGORIES
        if cat in mc.REVIEW_CATEGORIES:
            m["reasons"].add(mc.REVIEW_CATEGORIES[cat])
            not_theirs_total += amt
            flagged = True
        elif raw_pfc in mc.REVIEW_CATEGORIES:
            m["reasons"].add(mc.REVIEW_CATEGORIES[raw_pfc] +
                             f"; refined to {cat}")
            not_theirs_total += amt
            flagged = True
        # low-confidence categorization (unrecognized merchant)
        if res["confidence"] == "low":
            m["reasons"].add(f"low-confidence category ({res['source']})")
            low_conf_total += amt
            flagged = True

        if not flagged:
            # not suspect — drop it so untouched merchants don't clutter output
            m["_keep"] = m.get("_keep", False)
        else:
            m["_keep"] = True

    suspects = []
    for k, m in by_merchant.items():
        if not m.get("_keep"):
            continue
        suspects.append({
            "merchant": m["name"],
            "category": m["category"],
            "n": m["n"],
            "total": round(m["total"], 2),
            "reasons": sorted(m["reasons"]),
        })
    suspects.sort(key=lambda x: -x["total"])
    return suspects, round(not_theirs_total, 2), round(low_conf_total, 2)


# --------------------------- anomaly detectors ------------------------------
# These run over FULL history (not just the window) because recurring fees, price
# creep, and per-merchant outliers only mean anything against the past. Merchant
# grouping uses sc.merchant_key (entity_id when present — 59% of txns — else a
# normalized descriptor), so identity is stable across descriptor drift.

CREEP_MIN_ABS = 1.0      # flag a stream that has drifted up by >= max($1, 5%) ...
CREEP_MIN_PCT = 0.05     # ... from its early baseline to its recent baseline
OUTLIER_SIGMA = 3.0      # an in-window charge this many std-devs above the
OUTLIER_MIN_PRIORS = 5   # merchant's own (>= N) priors is an outlier worth a look


def _is_recurring(dates):
    """>=4 charges at a roughly regular cadence (a stream, not unrelated visits)."""
    if len(dates) < 4:
        return False
    gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
    avg = sum(gaps) / len(gaps)
    if avg <= 0:
        return False
    import statistics as _st
    return (_st.pstdev(gaps) / avg) <= 0.5      # low gap variance == regular


def find_recurring_fees(txns, end):
    """Recurring bank fees across full history -> annualized cost + active flag. A
    $4.95/mo maintenance fee reads as ~$59/yr; surfaced even outside the window,
    because the recurring cost (not one instance) is the real lever. The `active`
    flag distinguishes a live fee to kill from one that already stopped."""
    bykey = defaultdict(list)
    for t in txns:
        if sc.is_outflow(t) and _is_fee(t):
            d = sc.parse_date(t)
            amt = sc.amount_magnitude(t)
            if d and amt is not None:
                bykey[sc.merchant_key(t)].append((d, amt, sc.display_name(t)))
    out = []
    for items in bykey.values():
        if len(items) < 3:
            continue
        items.sort()
        dates = [d for d, _, _ in items]
        amts = [a for _, a, _ in items]
        gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
        avg_gap = sum(gaps) / len(gaps) if gaps else 30.0
        per_month = (sum(amts) / len(amts)) * (30.0 / avg_gap) if avg_gap else 0.0
        out.append({
            "merchant": items[-1][2],
            "n": len(items),
            "typical": round(amts[-1], 2),
            "cadence": sc.classify_cadence(gaps),
            "annual": round(per_month * 12, 2),
            "last": str(dates[-1]),
            "active": (end - dates[-1]).days <= max(45, int(avg_gap * 2)),
        })
    out.sort(key=lambda x: -x["annual"])
    return out


def find_anomalies(txns, start, end, known_amounts=None):
    """Pattern-deviation signals, each with a specific checkable reason, for charges
    that touch the window. Three kinds (fees excluded — they're their own tier):
      - price jump    : a recurring stream stepped up (sc.detect_steps)
      - cost creep    : a recurring stream drifted up gradually, no single step
      - amount outlier: an in-window charge far above the merchant's own history
    A charge whose amount matches a registered recurring obligation (`known_amounts`)
    is skipped — a known subscription at its known price is not an anomaly (e.g. a
    fixed monthly sub whose exact amount is registered as an obligation).
    Returns (list, total_extra_dollars)."""
    known = {round(a, 2) for a in (known_amounts or [])}
    bykey = defaultdict(list)
    names = {}
    for t in txns:
        if not sc.is_outflow(t) or _is_fee(t):
            continue
        d = sc.parse_date(t)
        amt = sc.amount_magnitude(t)
        if d and amt is not None:
            key = sc.merchant_key(t)
            bykey[key].append((d, amt))
            names[key] = sc.display_name(t)

    anomalies = []
    for key, items in bykey.items():
        items.sort()
        dates = [d for d, _ in items]
        amts = [a for _, a in items]
        name = names.get(key, "?")
        win_dates = [d for d in dates if start <= d <= end]
        if not win_dates:
            continue

        if _is_recurring(dates):
            steps = [(eff, old, new) for (eff, old, new) in sc.detect_steps(items)
                     if new > old and start <= eff <= end]
            for eff, old, new in steps:
                if round(new, 2) in known:      # known obligation at its known price
                    continue
                anomalies.append({
                    "merchant": name, "kind": "price jump",
                    "amount": round(new - old, 2),
                    "reason": f"{delivery.money(old)} → {delivery.money(new)} on {eff}",
                })
            if not steps:                      # gradual creep, no single jump
                deltas = [amts[i + 1] - amts[i] for i in range(len(amts) - 1)]
                mostly_up = sum(1 for x in deltas if x >= -0.01) / len(deltas) >= 0.7
                third = max(1, len(amts) // 3)
                early = sum(amts[:third]) / third
                late = sum(amts[-third:]) / third
                drift = late - early
                if (mostly_up and drift >= max(CREEP_MIN_ABS, CREEP_MIN_PCT * early)
                        and round(late, 2) not in known):
                    anomalies.append({
                        "merchant": name, "kind": "cost creep",
                        "amount": round(drift, 2),
                        "reason": (f"{delivery.money(early)} → {delivery.money(late)} "
                                   f"crept up over {len(items)} charges"),
                    })

        if len(items) >= OUTLIER_MIN_PRIORS + 1:
            import statistics as _st
            first_win = min(win_dates)
            prior = [a for d, a in items if d < first_win]
            if len(prior) >= OUTLIER_MIN_PRIORS:
                mu = _st.mean(prior)
                sd = _st.pstdev(prior) or (mu * 0.25) or 1.0
                for d, a in items:
                    if (start <= d <= end and a > mu + OUTLIER_SIGMA * sd
                            and a >= mu * 2 and round(a, 2) not in known):
                        anomalies.append({
                            "merchant": name, "kind": "amount outlier",
                            "amount": round(a - mu, 2),
                            "reason": f"{delivery.money(a)} vs usual ~{delivery.money(mu)}",
                        })

    # One strongest signal per merchant — a single vendor (or a split entity key)
    # shouldn't generate creep + outlier + a repeated row for the same charge.
    anomalies.sort(key=lambda a: -a["amount"])
    seen, deduped = set(), []
    for a in anomalies:
        if a["merchant"] in seen:
            continue
        seen.add(a["merchant"])
        deduped.append(a)
    total = round(sum(a["amount"] for a in deduped), 2)
    return deduped, total


# ------------------------------- summary ------------------------------------

def scan(txns, days=30, overrides=None, reconciliation=None):
    """Deterministic scan -> compact summary dict (no raw rows). This is the
    ONLY thing that ever feeds narration.

    reconciliation : dict | None
        If provided, a reconciliation result from receipt_scanner.reconcile().
        Amount discrepancies and unmatched bank charges are injected as
        additional findings alongside the existing fee/dup/suspect detectors.
    """
    if overrides is None:
        overrides = mc.load_overrides()
    start, end = window_bounds(txns, days)
    win = [t for t in txns if _in_window(t, start, end)]

    fees, fees_total = find_fees(win, overrides)
    dups, dup_recoverable = find_duplicates(win, overrides)
    suspects, not_theirs_total, low_conf_total = find_suspicious(win, overrides)

    # --- receipt reconciliation findings (if available) ---
    receipt_discrepancies = []
    receipt_unverified = []
    disc_total = 0.0
    unverified_total = 0.0

    if reconciliation:
        for d in reconciliation.get("discrepancies", []):
            receipt_discrepancies.append({
                "merchant": d.get("txn_merchant", d.get("receipt_merchant", "?")),
                "receipt_amount": d.get("receipt_amount", 0),
                "bank_amount": d.get("txn_amount", 0),
                "difference": d.get("abs_difference", 0),
                "date": d.get("txn_date", ""),
                "message": d.get("message", ""),
            })
            disc_total += d.get("abs_difference", 0)

        for u in reconciliation.get("unmatched_charges", []):
            receipt_unverified.append({
                "merchant": u.get("merchant", "?"),
                "amount": u.get("amount", 0),
                "date": u.get("date", ""),
                "message": u.get("message", ""),
            })
            unverified_total += u.get("amount", 0)

    disc_total = round(disc_total, 2)
    unverified_total = round(unverified_total, 2)

    avoidable = round(fees_total + dup_recoverable + disc_total, 2)

    # Anomalies (pattern deviation) + recurring fees, over FULL history.
    # Registered recurring obligations are KNOWN spend, not anomalies — exclude
    # their exact amounts from the deviation detectors.
    try:
        from finance_mcp.store import obligation_registry as _oblreg
        _reg = _oblreg.load_registry()
        _known = [o.get("amount") for o in (_reg.get("obligations") or [])
                  if o.get("amount") is not None]
    except Exception:
        _known = []
    anomalies, anomalies_total = find_anomalies(txns, start, end, known_amounts=_known)
    recurring_fees = find_recurring_fees(txns, end)
    active_fee_annual = round(sum(f["annual"] for f in recurring_fees if f["active"]), 2)

    # "Suspect" now means anomalies with a specific, checkable reason — NOT "no
    # receipt on file" or "merchant I don't recognize". Those are reconciliation
    # coverage + categorizer quality, not fraud, and were 100% of the old noise.
    suspect = anomalies_total
    avoidable_plus_suspect = round(avoidable + suspect, 2)

    flags = []
    if fees:
        flags.append(f"{len(fees)} bank fee(s) totaling {delivery.money(fees_total)}")
    for f in recurring_fees:
        if f["active"]:
            flags.append(f"recurring fee: {f['merchant']} {delivery.money(f['typical'])}/"
                         f"{f['cadence']} = {delivery.money(f['annual'])}/yr — avoidable")
    if dups:
        flags.append(f"{len(dups)} possible duplicate charge(s), "
                     f"{delivery.money(dup_recoverable)} to verify")
    for a in anomalies[:6]:
        flags.append(f"{a['kind']}: {a['merchant']} +{delivery.money(a['amount'])} "
                     f"({a['reason']})")
    if receipt_discrepancies:
        flags.append(f"{len(receipt_discrepancies)} receipt-vs-bank discrepancy(ies), "
                     f"{delivery.money(disc_total)} difference")
    if not_theirs_total > 0:
        flags.append(f"policy review: alcohol/not-yours spend "
                     f"{delivery.money(not_theirs_total)} (moved out of fraud $)")

    result = {
        "tool": "fee_fraud_scan",
        "as_of": str(end),
        "window": {"start": str(start), "end": str(end), "days": days},
        "headline": {
            "avoidable": avoidable,
            "suspect": suspect,                      # now = anomalies, not noise
            "avoidable_plus_suspect": avoidable_plus_suspect,
            "fees_total": fees_total,
            "dup_recoverable": dup_recoverable,
            "anomalies_total": anomalies_total,
            "n_anomalies": len(anomalies),
            "recurring_fee_annual": active_fee_annual,
            "n_recurring_fees": len(recurring_fees),
            "n_fees": len(fees),
            "n_duplicates": len(dups),
            # kept for backward-compat; alcohol is now a policy signal and
            # low_conf/unverified are no longer part of the fraud number.
            "not_theirs_total": not_theirs_total,
            "low_conf_total": 0.0,
            "n_suspect_merchants": len(suspects),
            "n_receipt_discrepancies": len(receipt_discrepancies),
            "receipt_discrepancy_total": disc_total,
            "n_unverified_charges": len(receipt_unverified),
            "unverified_charge_total": unverified_total,
        },
        "detail": {
            "fees": fees[:12],
            "recurring_fees": recurring_fees[:8],
            "duplicates": dups[:12],
            "anomalies": anomalies[:12],
            "suspicious": suspects[:12],
            "receipt_discrepancies": receipt_discrepancies[:12],
            "unverified_charges": receipt_unverified[:12],
        },
        "flags": flags,
    }
    return result


# ------------------------------- rendering ----------------------------------

def render(s):
    money = delivery.money
    h = s["headline"]
    d = s["detail"]
    L = []
    L.append("# finance.mcp — FEE + FRAUD SCAN")
    L.append(f"_window {s['window']['start']} → {s['window']['end']} "
             f"({s['window']['days']}d) · as of {s['as_of']}_\n")

    L.append(f"## Recoverable: {money(h['avoidable'])} "
             f"· {h.get('n_anomalies', 0)} anomal"
             f"{'y' if h.get('n_anomalies', 0) == 1 else 'ies'} ({money(h['suspect'])})")
    L.append(f"- Recoverable (fees + duplicates + receipt over-charges): {money(h['avoidable'])}")
    L.append(f"- Anomalies (price jumps / creep / outliers): {money(h['suspect'])}\n")

    rfees = d.get("recurring_fees", [])
    if rfees:
        L.append("## Recurring fees")
        for rf in rfees:
            cad = rf.get("cadence")
            cad = cad[0] if isinstance(cad, (list, tuple)) and cad else cad
            stat = "active — avoidable" if rf.get("active") else f"stopped {rf.get('last','')}"
            L.append(f"- 🔁 {rf['merchant']} {money(rf['typical'])}/{cad} = "
                     f"{money(rf['annual'])}/yr ({stat})")
        L.append("")

    anoms = d.get("anomalies", [])
    if anoms:
        L.append("## Anomalies — what changed")
        for a in anoms:
            L.append(f"- ⚠️ {a['merchant']} ({a['kind']}): {a['reason']} "
                     f"→ +{money(a['amount'])}")
        L.append("")

    L.append(f"## Bank fees  ({h['n_fees']}, {money(h['fees_total'])})")
    if d["fees"]:
        for f in d["fees"]:
            L.append(f"- 💸 {f['merchant']} {money(f['amount'])} {f['date']} "
                     f"[{f['category']}]")
    else:
        L.append("- none")

    L.append(f"\n## Duplicate charges  ({h['n_duplicates']}, "
             f"{money(h['dup_recoverable'])} recoverable)")
    if d["duplicates"]:
        for x in d["duplicates"]:
            L.append(f"- ⚠️ {x['merchant']} {money(x['amount'])} on "
                     f"{x['dates'][0]} & {x['dates'][1]} ({x['gap_days']}d apart) "
                     f"→ {money(x['recoverable'])} recoverable")
    else:
        L.append("- none")

    L.append(f"\n## Suspicious / unrecognized  ({h['n_suspect_merchants']})")
    if d["suspicious"]:
        for x in d["suspicious"]:
            L.append(f"- 🔎 {x['merchant']} {money(x['total'])} ({x['n']}x) "
                     f"[{x['category']}]")
            for r in x["reasons"]:
                L.append(f"    - {r}")
    else:
        L.append("- none")

    # Receipt reconciliation findings (if available)
    if d.get("receipt_discrepancies"):
        L.append(f"\n## Receipt discrepancies  ({h.get('n_receipt_discrepancies', 0)}, "
                 f"{money(h.get('receipt_discrepancy_total', 0))} difference)")
        for x in d["receipt_discrepancies"]:
            L.append(f"- 📧 {x['message']}")

    if d.get("unverified_charges"):
        L.append(f"\n## Unverified charges  ({h.get('n_unverified_charges', 0)}, "
                 f"{money(h.get('unverified_charge_total', 0))})")
        for x in d["unverified_charges"]:
            L.append(f"- ❓ {x['message']}")

    if s["flags"]:
        L.append("\n## Flags")
        for fl in s["flags"]:
            L.append(f"- {fl}")
    return "\n".join(L)


# --------------------------------- CLI --------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Fee + fraud/error hunter.")
    ap.add_argument("transactions", help="transactions JSON path")
    ap.add_argument("--days", type=int, default=30,
                    help="trailing window width in days (default 30)")
    ap.add_argument("--no-voice", action="store_true",
                    help="numbers-only, zero token spend")
    ap.add_argument("--email", nargs="?", const="__self__", default=None,
                    help="email the report via Gmail SMTP")
    ap.add_argument("--rules", default="rules.md",
                    help="rules file (for narration tone); optional")
    ap.add_argument("--json", action="store_true",
                    help="print the summary dict as JSON and exit")
    a = ap.parse_args()

    txns = sc.load_transactions(a.transactions)
    s = scan(txns, days=a.days)

    if a.json:
        print(json.dumps(s, indent=2))
        return

    report = render(s)

    # tone (optional): reuse rules.md tone block if present, else a sane default
    tone = "Direct, honest, in-your-corner. Blunt on waste, never shaming."
    try:
        from finance_mcp.engines import budget_scorer as bs
        tone = bs.parse_rules(a.rules)["tone"]
    except Exception:
        pass

    voice = None if a.no_voice else delivery.narrate(s, tone, "fee_fraud_scan")
    full = report + (("\n\n---\n\n## Read\n" + voice) if voice else "")

    print(full)
    h = s["headline"]
    print(f"\n[HEADLINE] avoidable+suspect {delivery.money(h['avoidable_plus_suspect'])} "
          f"this {s['window']['days']}d · fees {delivery.money(h['fees_total'])} · "
          f"dups {delivery.money(h['dup_recoverable'])} ({h['n_duplicates']}) · "
          f"suspect {delivery.money(h['suspect'])}")
    print(f"[{'voice ON' if voice else 'NO-VOICE ($0)'}]")

    if a.email is not None:
        to = None if a.email == "__self__" else a.email
        subj = f"finance.mcp — Fee + Fraud Scan {s['window']['end']}"
        delivery.send_email(to, subj, full)


if __name__ == "__main__":
    main()
