"""demo.py — synthetic data + a one-command end-to-end demo.

The real suite reads bank data via Plaid; this module produces a realistic,
fully synthetic substitute so the whole pipeline can run with NO real financial
data. `generate()` is also the source of the committed test fixture
(tests/fixtures/transactions.sample.json), so it is deterministic: a fixed END
date and a seeded RNG yield the same dataset every run. No real merchants,
people, amounts, or accounts appear here.

    python -m finance_mcp demo        # build + print a digest from synthetic data
"""
import datetime as dt
import json
import os
import random
import sys

END = dt.date(2026, 5, 1)       # fixed anchor → reproducible
START = END - dt.timedelta(days=120)
OWNER = "primary"
_DATA = os.path.join(os.path.dirname(__file__), "data")


def _txn(date, amount, *, debit, merchant, category, tid):
    signed = -abs(amount) if debit else abs(amount)
    return {
        "id": tid,
        "accountId": "acct_demo",
        "amount": round(signed, 2),
        "date": str(date),
        "type": "debit" if debit else "credit",
        "category": category,
        "merchantName": merchant,
        "description": merchant,
        "pending": False,
        "rawData": {
            "id": tid,
            "account_id": "acct_demo",
            "amount": round(abs(amount), 2),
            "date": str(date),
            "category": category,
            "personal_finance_category": {"detailed": category},
            "counterparties": [],
            "account_owner": OWNER,
        },
    }


def generate():
    """Return a deterministic list of synthetic transactions in the engine shape."""
    rng = random.Random(42)
    rows = []
    n = 0

    def add(date, amount, *, debit, merchant, category):
        nonlocal n
        n += 1
        rows.append(_txn(date, amount, debit=debit, merchant=merchant,
                         category=category, tid=f"t{n:04d}"))

    # Weekly payroll (recurring inflow) — every Thursday.
    d = START
    while d <= END:
        if d.weekday() == 3:
            add(d, 800.00, debit=False, merchant="PAYROLL", category="INCOME_WAGES")
        d += dt.timedelta(days=1)

    # Monthly fixed subscriptions (recurring outflows; mirror examples/obligations).
    subs = [
        ("AI Assistant", 20.00, "GENERAL_MERCHANDISE_SOFTWARE"),
        ("Phone Plan", 45.00, "GENERAL_SERVICES_TELECOMMUNICATIONS"),
        ("Gym", 30.00, "PERSONAL_CARE_GYMS_AND_FITNESS_CENTERS"),
        ("Music Streaming", 11.99, "ENTERTAINMENT_STREAMING"),
        ("Newsletter", 5.00, "GENERAL_MERCHANDISE_SOFTWARE"),
    ]
    for month_offset in range(4):
        day = END - dt.timedelta(days=30 * month_offset)
        for merch, amt, cat in subs:
            add(day, amt, debit=True, merchant=merch, category=cat)

    # Metered/variable cloud hosting (recurring; amount drifts).
    for month_offset in range(4):
        day = END - dt.timedelta(days=30 * month_offset + 2)
        add(day, round(35.00 + rng.uniform(-4, 6), 2), debit=True,
            merchant="Cloud Hosting", category="GENERAL_MERCHANDISE_SOFTWARE")

    # Amortizing car loan (recurring outflow with an end date in the registry).
    for month_offset in range(4):
        day = END - dt.timedelta(days=30 * month_offset + 5)
        add(day, 285.00, debit=True, merchant="Car Loan",
            category="LOAN_PAYMENTS_CAR_PAYMENT")

    # Variable groceries (weekly-ish, high amount variance).
    d = START
    while d <= END:
        if d.weekday() == 6:
            add(d, round(rng.uniform(55, 110), 2), debit=True,
                merchant="Grocery Market", category="FOOD_AND_DRINK_GROCERIES")
        d += dt.timedelta(days=1)

    # Restaurants / coffee (irregular discretionary).
    for _ in range(18):
        day = START + dt.timedelta(days=rng.randint(0, 120))
        add(day, round(rng.uniform(8, 45), 2), debit=True,
            merchant="Corner Cafe", category="FOOD_AND_DRINK_RESTAURANT")

    # A few bank fees (fee/fraud should surface these).
    add(END - dt.timedelta(days=40), 4.95, debit=True, merchant="Monthly Service Fee",
        category="BANK_FEES_OVERDRAFT_FEES")
    add(END - dt.timedelta(days=10), 4.95, debit=True, merchant="Monthly Service Fee",
        category="BANK_FEES_OVERDRAFT_FEES")

    # A duplicate charge on the same day (fee/fraud dup detector should catch it).
    add(END - dt.timedelta(days=7), 49.99, debit=True, merchant="Online Store",
        category="GENERAL_MERCHANDISE_ONLINE_MARKETPLACES")
    add(END - dt.timedelta(days=7), 49.99, debit=True, merchant="Online Store",
        category="GENERAL_MERCHANDISE_ONLINE_MARKETPLACES")

    # A self-transfer (must be excluded from savings math).
    add(END - dt.timedelta(days=20), 200.00, debit=True, merchant="Transfer to Savings",
        category="TRANSFER_OUT_ACCOUNT_TRANSFER")

    rows.sort(key=lambda r: r["date"])
    return rows


def run_demo():
    """Build and print a full digest from synthetic data — no real bank needed."""
    from finance_mcp import finance_agent as fa
    from finance_mcp.store import obligation_registry as oblreg

    # Point the forward-plan registry at the bundled synthetic obligations.
    oblreg.REGISTRY_PATH = os.path.join(_DATA, "obligations.demo.json")
    rules_path = os.path.join(_DATA, "rules.demo.md")

    txns = generate()
    digest = fa.build_digest(
        txns, balance=1200.0, mode="monthly", forecast_days=35,
        buffer=100.0, include_burn=True, scan_days=30, rules_path=rules_path)
    print(fa.render(digest))
    print("\n" + fa.headline_line(digest))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--json":
        print(json.dumps(generate(), indent=2))
    else:
        run_demo()


if __name__ == "__main__":
    main()
