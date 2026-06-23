#!/usr/bin/env python3
"""
test_cashflow_forecaster.py — HARD unit tests for the projection math.

Confidently-wrong money/date math is the failure mode, so these tests construct
tiny synthetic streams with KNOWN occurrence days and assert:
  - the EXACT day-by-day balance curve,
  - the correct LOW and OVERDRAFT days + the charge that tipped each,
  - the safe-by date (day before the first breach),
  - the buffer BOUNDARY (== buffer is NOT low; one cent under IS),
  - roll_forward occurrence enumeration,
  - balance resolution order (--balance vs balance.json vs error),
  - the summary dict carries no raw transaction rows.

Run: python3 test_cashflow_forecaster.py
"""

import datetime as dt
import json
import os
import tempfile
import unittest

from finance_mcp.engines import cashflow_forecaster as cf


def D(s):
    return dt.datetime.strptime(s, "%Y-%m-%d").date()


def stream(merchant, direction, cadence, avg, last_date):
    """Minimal stream record matching recurring.streams() output shape."""
    return {
        "key": ["name", merchant.upper()],
        "merchant": merchant,
        "direction": direction,
        "cadence": cadence,
        "per_year": 365 // cf.cadence_days(cadence),
        "last_amount": avg,
        "avg_amount": avg,
        "first_date": last_date,
        "last_date": last_date,
        "n": 3,
        "is_active": True,
        "next_date": last_date,
    }


class TestRollForward(unittest.TestCase):
    def test_weekly_occurrences_strictly_after_as_of(self):
        # last charge on as_of itself -> first future occurrence is +7d
        s = stream("Pay", "inflow", "weekly", 800.0, D("2026-06-15"))
        as_of = D("2026-06-15")
        occ = cf.roll_forward(s, as_of, as_of + dt.timedelta(days=21))
        self.assertEqual(occ, [D("2026-06-22"), D("2026-06-29"), D("2026-07-06")])

    def test_last_date_before_as_of_catches_up(self):
        # last charge 10 days before as_of, weekly -> next falls 4 days after as_of
        s = stream("Pay", "inflow", "weekly", 800.0, D("2026-06-05"))
        as_of = D("2026-06-15")
        occ = cf.roll_forward(s, as_of, as_of + dt.timedelta(days=10))
        # 06-05 +7=06-12 (<=as_of, skip) +7=06-19, +7=06-26(>end)
        self.assertEqual(occ, [D("2026-06-19")])

    def test_none_in_window(self):
        s = stream("Rent", "outflow", "monthly", 405.0, D("2026-06-14"))
        as_of = D("2026-06-15")
        occ = cf.roll_forward(s, as_of, as_of + dt.timedelta(days=5))
        self.assertEqual(occ, [])  # next is ~07-14, past the 5-day horizon


class TestProjectionCurve(unittest.TestCase):
    """The headline test: one income, one debit, zero burn -> exact curve."""

    def setUp(self):
        self.as_of = D("2026-06-15")
        # income $500 on day +3 (06-18); debit $700 on day +5 (06-20)
        self.income = [stream("Job", "inflow", "weekly", 500.0, D("2026-06-11"))]
        # weekly debit last 06-13 -> +7 = 06-20 (day +5). Good.
        self.oblig = [stream("Bill", "outflow", "weekly", 700.0, D("2026-06-13"))]

    def test_exact_curve_no_burn(self):
        # verify the synthetic occurrence days first (horizon end = as_of+7 = 06-22)
        self.assertEqual(cf.roll_forward(self.income[0], self.as_of,
                                         self.as_of + dt.timedelta(days=7)),
                         [D("2026-06-18")])
        self.assertEqual(cf.roll_forward(self.oblig[0], self.as_of,
                                         self.as_of + dt.timedelta(days=7)),
                         [D("2026-06-20")])

        start = 1000.0
        days = 7
        buffer = 100.0
        proj = cf.project(start, self.as_of, days, buffer,
                          self.income, self.oblig, daily_burn=0.0)

        # expected end_balance per day (burn=0):
        # 06-16: 1000
        # 06-17: 1000
        # 06-18: +500 = 1500
        # 06-19: 1500
        # 06-20: -700 = 800
        # 06-21: 800
        # 06-22: 800
        expected = {
            "2026-06-16": 1000.0, "2026-06-17": 1000.0, "2026-06-18": 1500.0,
            "2026-06-19": 1500.0, "2026-06-20": 800.0, "2026-06-21": 800.0,
            "2026-06-22": 800.0,
        }
        got = {r["date"]: r["end_balance"] for r in proj["curve"]}
        self.assertEqual(got, expected)
        self.assertEqual(proj["min_balance"], 800.0)
        self.assertEqual(proj["min_date"], "2026-06-20")
        self.assertEqual(proj["end_balance"], 800.0)
        self.assertEqual(proj["low_days"], [])
        self.assertEqual(proj["overdraft_days"], [])

    def test_overdraft_flag_and_cause(self):
        # start=150: 06-18 +500 -> 650; 06-20 -700 -> -50 overdraft; then it stays
        # negative (no further events, burn=0) on 06-21 and 06-22 too.
        proj = cf.project(150.0, self.as_of, 7, 100.0,
                          self.income, self.oblig, daily_burn=0.0)
        # three breaching days, but the FIRST is the one that matters for safe-by
        self.assertEqual([r["date"] for r in proj["overdraft_days"]],
                         ["2026-06-20", "2026-06-21", "2026-06-22"])
        od = proj["overdraft_days"][0]
        self.assertEqual(od["date"], "2026-06-20")
        self.assertEqual(od["end_balance"], -50.0)
        tip = cf._tipping_charges(od)
        self.assertEqual(tip, [{"name": "Bill", "amount": 700.0}])
        self.assertEqual(cf.safe_by_date(od["date"]), "2026-06-19")


class TestBufferBoundary(unittest.TestCase):
    def setUp(self):
        self.as_of = D("2026-06-15")
        # single debit on day +2 (06-17), no income
        self.oblig = [stream("Bill", "outflow", "weekly", 100.0, D("2026-06-10"))]

    def test_exactly_at_buffer_is_not_low(self):
        # 06-17: -100 from start 200 -> 100 == buffer -> NOT low (strict <)
        self.assertEqual(cf.roll_forward(self.oblig[0], self.as_of,
                                         self.as_of + dt.timedelta(days=4)),
                         [D("2026-06-17")])
        proj = cf.project(200.0, self.as_of, 4, 100.0, [], self.oblig, 0.0)
        self.assertEqual(proj["low_days"], [])
        self.assertEqual(proj["min_balance"], 100.0)

    def test_one_cent_under_buffer_is_low(self):
        # 06-17 -100 from 199.99 -> 99.99 (< buffer), then stays there 06-18, 06-19
        proj = cf.project(199.99, self.as_of, 4, 100.0, [], self.oblig, 0.0)
        self.assertEqual([r["date"] for r in proj["low_days"]],
                         ["2026-06-17", "2026-06-18", "2026-06-19"])
        low = proj["low_days"][0]
        self.assertEqual(low["date"], "2026-06-17")
        self.assertEqual(low["end_balance"], 99.99)
        # and it should NOT be an overdraft (still positive)
        self.assertEqual(proj["overdraft_days"], [])


class TestBurnApplied(unittest.TestCase):
    def test_burn_drains_each_day(self):
        as_of = D("2026-06-15")
        # no streams, just burn of 50/day from 1000 over 4 days
        proj = cf.project(1000.0, as_of, 4, 100.0, [], [], daily_burn=50.0)
        got = [r["end_balance"] for r in proj["curve"]]
        self.assertEqual(got, [950.0, 900.0, 850.0, 800.0])
        self.assertEqual(proj["min_balance"], 800.0)


class TestBalanceResolution(unittest.TestCase):
    def test_cli_wins(self):
        self.assertEqual(cf.resolve_balance(1234.5, "/nonexistent.json"), 1234.5)

    def test_balance_json_fallback(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"balance": 777.0}, f)
            path = f.name
        try:
            self.assertEqual(cf.resolve_balance(None, path), 777.0)
        finally:
            os.unlink(path)

    def test_missing_errors(self):
        with self.assertRaises(SystemExit):
            cf.resolve_balance(None, "/definitely/not/here.json")


class TestSummaryNoRawRows(unittest.TestCase):
    """The summary feeding narration must carry NO raw transaction rows."""

    def test_summary_structure_and_no_raw(self):
        # build a tiny real-ish txn list so build_summary runs end to end
        def txn(date, amt, name, debit=True):
            return {
                "type": "debit" if debit else "credit",
                "amount": -amt if debit else amt,
                "date": date,
                "merchantName": name,
                "category": "GENERAL_SERVICES_OTHER_GENERAL_SERVICES",
                "description": name,
                "rawData": {"amount": amt, "date": date,
                            "counterparties": [{"name": name, "type": "merchant",
                                                "entity_id": None, "confidence_level": "LOW"}]},
            }
        txns = []
        # weekly income, 4 charges
        for d in ("2026-05-21", "2026-05-28", "2026-06-04", "2026-06-11"):
            txns.append(txn(d, 800.0, "Paycheck", debit=False))
        # weekly obligation, 4 charges
        for d in ("2026-05-16", "2026-05-23", "2026-05-30", "2026-06-06"):
            txns.append(txn(d, 405.0, "Rent Co"))
        # some non-recurring noise for burn
        txns.append(txn("2026-06-10", 30.0, "Random Cafe A"))
        txns.append(txn("2026-06-12", 22.5, "Random Shop B"))

        summary, proj = cf.build_summary(txns, 1000.0, 35, 100.0, include_burn=True)

        # required contract keys
        for k in ("tool", "as_of", "window", "headline", "detail", "flags"):
            self.assertIn(k, summary)
        self.assertEqual(summary["tool"], "cashflow_forecaster")
        self.assertIn("start", summary["window"])
        self.assertIn("end", summary["window"])

        # JSON-serializable and contains no raw txn fields anywhere
        blob = json.dumps(summary)
        for forbidden in ("rawData", "counterparties", "reference", "accountId",
                          "category_id", "authorized_date"):
            self.assertNotIn(forbidden, blob)
        # dollar figures are floats, dates are strings
        self.assertIsInstance(summary["headline"]["start_balance"], float)
        self.assertIsInstance(summary["as_of"], str)
        # daily_burn computed from the 2 non-recurring outflows over 60 days
        self.assertAlmostEqual(summary["headline"]["daily_burn"], round(52.5 / 60, 2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
