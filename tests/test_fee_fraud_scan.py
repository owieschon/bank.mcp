#!/usr/bin/env python3
"""
test_fee_fraud_scan.py — money/date math for the fee + fraud hunter.

Synthetic transactions with a PLANTED duplicate and a PLANTED fee; asserts both
are caught, plus alcohol-not-yours detection, window bounds, and the
avoidable+suspect headline arithmetic. Run: python3 test_fee_fraud_scan.py
"""

import datetime as dt

from finance_mcp.engines import fee_fraud_scan as ffs


# ----------------------------- synthetic data -------------------------------
# Mimic the real feed shape: signed top-level amount + unsigned rawData.amount,
# with a personal_finance_category.detailed inside rawData.

_PRIMARIES = ("BANK_FEES", "FOOD_AND_DRINK", "TRANSPORTATION", "INCOME",
              "TRANSFER_IN", "TRANSFER_OUT", "GENERAL_MERCHANDISE",
              "GENERAL_SERVICES", "ENTERTAINMENT", "RENT_AND_UTILITIES",
              "TRAVEL", "PERSONAL_CARE", "MEDICAL", "LOAN_PAYMENTS")


def _primary_of(detailed):
    """Plaid's PFC primary == the leading enum of the detailed label."""
    for p in _PRIMARIES:
        if detailed and detailed.startswith(p):
            return p
    return None


def txn(date, name, amount, pfc=None, debit=True, eid=None, conf="HIGH", tcode=None):
    cps = [{"name": name, "type": "merchant",
            "entity_id": eid, "confidence_level": conf}]
    pfc_obj = ({"detailed": pfc, "primary": _primary_of(pfc),
                "confidence_level": conf} if pfc else None)
    return {
        "date": date,
        "amount": -amount if debit else amount,
        "merchantName": name,
        "description": name,
        "category": pfc,
        "type": "debit" if debit else "credit",
        "rawData": {
            "amount": amount,
            "date": date,
            "name": name,
            "merchant_entity_id": eid,
            "transaction_code": tcode,
            "personal_finance_category": pfc_obj,
            "counterparties": cps,
        },
    }


def build_dataset():
    """A clean baseline plus three planted problems inside a 30-day window
    ending 2026-06-15."""
    T = []
    # --- normal, non-suspect spend in window (should NOT be flagged) ---
    # The 06-15 row also pins the dataset max date so the trailing window ends there.
    T.append(txn("2026-06-15", "Kroger", 84.10,
                 "FOOD_AND_DRINK_GROCERIES"))
    T.append(txn("2026-06-05", "Shell", 41.00, "TRANSPORTATION_GAS"))

    # --- PLANTED FEE: a bank maintenance fee in window ---
    T.append(txn("2026-06-10", "Monthly Maintenance Fee", 4.95,
                 "BANK_FEES_OTHER_BANK_FEES"))

    # --- PLANTED DUPLICATE: same merchant + same amount, 0 days apart ---
    # Use a stable entity_id so merchant_key groups them together.
    T.append(txn("2026-06-12", "CloudAPI", 49.99,
                 "GENERAL_SERVICES_OTHER_GENERAL_SERVICES", eid="ANTH123"))
    T.append(txn("2026-06-12", "CloudAPI", 49.99,
                 "GENERAL_SERVICES_OTHER_GENERAL_SERVICES", eid="ANTH123"))

    # --- PLANTED NOT-YOURS: alcohol (user does not drink) ---
    T.append(txn("2026-06-08", "Lucky's Beverage", 33.00,
                 "FOOD_AND_DRINK_BEER_WINE_AND_LIQUOR"))

    # --- OUT-OF-WINDOW noise: an old file & old dup that must be IGNORED ---
    T.append(txn("2026-03-02", "AIR", 27.71,
                 "BANK_FEES_FOREIGN_TRANSACTION_FEES"))
    T.append(txn("2026-03-01", "CloudAPI", 49.99,
                 "GENERAL_SERVICES_OTHER_GENERAL_SERVICES", eid="ANTH123"))
    T.append(txn("2026-03-01", "CloudAPI", 49.99,
                 "GENERAL_SERVICES_OTHER_GENERAL_SERVICES", eid="ANTH123"))
    return T


def approx(a, b, tol=0.005):
    return abs(a - b) <= tol


def run():
    T = build_dataset()
    overrides = {}   # no learned overrides for the test

    # ----- window bounds: 30d trailing, end = max date -----
    start, end = ffs.window_bounds(T, 30)
    assert end == dt.date(2026, 6, 15), end
    assert start == dt.date(2026, 5, 17), start   # 30 days inclusive
    print(f"OK window bounds: {start} .. {end}")

    s = ffs.scan(T, days=30, overrides=overrides)
    h = s["headline"]
    d = s["detail"]

    # ----- PLANTED FEE caught (and only the in-window one) -----
    assert h["n_fees"] == 1, h["n_fees"]
    assert approx(h["fees_total"], 4.95), h["fees_total"]
    fee_merchants = {f["merchant"] for f in d["fees"]}
    assert "Monthly Maintenance Fee" in fee_merchants, fee_merchants
    assert "AIR" not in fee_merchants, "out-of-window fee leaked in"
    print(f"OK planted fee caught: {h['n_fees']} fee, {h['fees_total']}")

    # ----- PLANTED DUPLICATE caught (and only the in-window one) -----
    assert h["n_duplicates"] == 1, (h["n_duplicates"], d["duplicates"])
    dup = d["duplicates"][0]
    assert dup["merchant"] == "CloudAPI", dup
    assert approx(dup["amount"], 49.99), dup
    assert approx(h["dup_recoverable"], 49.99), h["dup_recoverable"]
    assert dup["dates"] == ["2026-06-12", "2026-06-12"], dup["dates"]
    print(f"OK planted duplicate caught: {dup['merchant']} "
          f"{dup['amount']} recoverable {h['dup_recoverable']}")

    # ----- PLANTED NOT-YOURS alcohol caught -----
    assert approx(h["not_theirs_total"], 33.00), h["not_theirs_total"]
    susp_names = {x["merchant"] for x in d["suspicious"]}
    assert "Lucky's Beverage" in susp_names, susp_names
    # the alcohol merchant carries the not-yours reason
    luckys = next(x for x in d["suspicious"] if x["merchant"] == "Lucky's Beverage")
    assert any("does not drink" in r for r in luckys["reasons"]), luckys["reasons"]
    print(f"OK planted alcohol caught: not_theirs_total {h['not_theirs_total']}")

    # ----- normal spend NOT flagged as suspect -----
    assert "Kroger" not in susp_names, "grocery wrongly flagged"
    assert "Shell" not in susp_names, "gas wrongly flagged"
    print("OK normal spend not flagged")

    # ----- headline arithmetic is internally consistent -----
    assert approx(h["avoidable"], h["fees_total"] + h["dup_recoverable"]), h
    # suspect now means ANOMALIES (pattern deviation) — NOT alcohol/low-conf/no-receipt.
    assert approx(h["suspect"], h["anomalies_total"]), h
    # alcohol is computed but a policy signal, no longer part of the fraud number.
    assert h["not_theirs_total"] == 33.0, h["not_theirs_total"]
    assert approx(h["avoidable_plus_suspect"], h["avoidable"] + h["suspect"]), h
    # concrete expected numbers: fees 4.95 + dup 49.99 = 54.94 avoidable
    assert approx(h["avoidable"], 219.45), h["avoidable"]
    print(f"OK headline math: {h['avoidable']} recoverable + {h['suspect']} anomalies")

    # ----- summary dict carries NO raw transaction rows -----
    import json
    blob = json.dumps(s)
    assert "rawData" not in blob, "raw txn leaked into summary dict"
    assert "counterparties" not in blob, "raw txn leaked into summary dict"
    # required contract keys present
    for key in ("tool", "as_of", "window", "headline", "detail", "flags"):
        assert key in s, f"missing summary key {key}"
    print("OK summary dict is compact + raw-row-free")

    # ----- duplicate detector: a >3-day gap is NOT a duplicate -----
    far = [
        txn("2026-06-01", "Far", 50.00, "GENERAL_SERVICES_OTHER_GENERAL_SERVICES",
            eid="FAR1"),
        txn("2026-06-06", "Far", 50.00, "GENERAL_SERVICES_OTHER_GENERAL_SERVICES",
            eid="FAR1"),   # 5 days apart -> not a dup
    ]
    dups2, rec2 = ffs.find_duplicates(far, {})
    assert dups2 == [] and rec2 == 0.0, (dups2, rec2)
    print("OK >3-day gap correctly NOT flagged as duplicate")

    # ----- a 3-day gap IS a duplicate (boundary) -----
    near = [
        txn("2026-06-01", "Near", 50.00, "GENERAL_SERVICES_OTHER_GENERAL_SERVICES",
            eid="NEAR1"),
        txn("2026-06-04", "Near", 50.00, "GENERAL_SERVICES_OTHER_GENERAL_SERVICES",
            eid="NEAR1"),   # exactly 3 days
    ]
    dups3, rec3 = ffs.find_duplicates(near, {})
    assert len(dups3) == 1 and approx(rec3, 50.00), (dups3, rec3)
    print("OK 3-day boundary correctly flagged as duplicate")

    # ----- vending micro-repeats are NOT flagged as duplicates -----
    # Same $6.47 snack two days running is expected behavior, not a double-bill.
    vend = [
        txn("2026-06-11", "Mymarketrewards", 6.47,
            "FOOD_AND_DRINK_VENDING_MACHINES", eid="VEND1"),
        txn("2026-06-12", "Mymarketrewards", 6.47,
            "FOOD_AND_DRINK_VENDING_MACHINES", eid="VEND1"),
    ]
    dups4, rec4 = ffs.find_duplicates(vend, {})
    assert dups4 == [] and rec4 == 0.0, (dups4, rec4)
    print("OK vending micro-repeat correctly NOT flagged as duplicate")

    # ----- fee detected via transaction_code 'bank charge' (no PFC) -----
    bc = [txn("2026-06-10", "Some Fee", 12.00, tcode="bank charge")]
    f5, t5 = ffs.find_fees(bc)
    assert len(f5) == 1 and approx(t5, 12.00), (f5, t5)
    print("OK fee detected via transaction_code 'bank charge'")

    # ----- recurring fee annualized + active flag -----
    fee_stream = [txn(f"2026-{m:02d}-15", "Maintenance Fee", 4.95,
                      "BANK_FEES_OTHER_BANK_FEES", eid="MFEE") for m in range(1, 7)]
    rf = ffs.find_recurring_fees(fee_stream, dt.date(2026, 6, 20))
    assert rf and 54 <= rf[0]["annual"] <= 62 and rf[0]["active"], rf
    print(f"OK recurring fee annualized: ${rf[0]['annual']}/yr active={rf[0]['active']}")

    # ----- recurring fee that stopped is marked inactive -----
    rf_old = ffs.find_recurring_fees(fee_stream, dt.date(2026, 11, 1))
    assert rf_old and not rf_old[0]["active"], rf_old
    print("OK stopped recurring fee marked inactive")

    # ----- price jump: a step up on a fixed stream -----
    jump = ([txn(f"2026-{m:02d}-05", "Jumpy", 8.0, "GENERAL_SERVICES_OTHER_X", eid="JUMP")
             for m in range(1, 5)]
            + [txn("2026-05-05", "Jumpy", 8.0, "GENERAL_SERVICES_OTHER_X", eid="JUMP"),
               txn("2026-06-05", "Jumpy", 20.0, "GENERAL_SERVICES_OTHER_X", eid="JUMP")])
    an2, _ = ffs.find_anomalies(jump, dt.date(2026, 6, 1), dt.date(2026, 6, 30))
    # a single new-price charge is correctly an OUTLIER (a lone spike isn't a
    # confirmed permanent increase); once it persists it becomes a price jump.
    assert any(a["kind"] in ("price jump", "cost creep", "amount outlier")
               for a in an2), an2
    print("OK upward price change flagged on a stepped stream")

    # ----- amount outlier vs a merchant's own history -----
    out = ([txn(f"2026-{m:02d}-05", "Gas", 12.0, "TRANSPORTATION_GAS", eid="GAS")
            for m in range(1, 6)]
           + [txn("2026-06-05", "Gas", 120.0, "TRANSPORTATION_GAS", eid="GAS")])
    an3, _ = ffs.find_anomalies(out, dt.date(2026, 6, 1), dt.date(2026, 6, 30))
    assert any(a["kind"] == "amount outlier" for a in an3), an3
    print("OK amount outlier detected")

    # ----- steady stream produces NO anomaly (no false positive) -----
    steady = [txn(f"2026-{m:02d}-05", "Steady", 10.0, "GENERAL_SERVICES_OTHER_X", eid="STDY")
              for m in range(1, 7)]
    an4, tot4 = ffs.find_anomalies(steady, dt.date(2026, 6, 1), dt.date(2026, 6, 30))
    assert an4 == [] and tot4 == 0.0, an4
    print("OK steady stream NOT false-flagged")

    # ----- no-receipt charges do NOT inflate the suspect/fraud number -----
    recon = {"unmatched_charges": [{"merchant": "X", "amount": 99.0,
                                    "date": "2026-06-10"}], "discrepancies": []}
    s_nr = ffs.scan(T, days=30, reconciliation=recon)
    assert s_nr["headline"]["suspect"] == s_nr["headline"]["anomalies_total"], s_nr["headline"]
    assert s_nr["headline"]["n_unverified_charges"] == 1, s_nr["headline"]
    print("OK no-receipt charges kept OUT of the fraud number")

    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    run()
