#!/usr/bin/env python3
"""
test_recurring.py — unit tests for recurring.py money/date math.

Confidently-wrong math is the failure mode, so the synthetic streams here
pin down cadence classification, next_date arithmetic, is_active recency,
and the inflow/outflow split. Run: python3 test_recurring.py
"""

import unittest
from datetime import date, timedelta

from finance_mcp.engines import recurring


def _txn(merchant, amount, d, debit=True):
    """Build a transaction in the bank-mcp shape recurring.py consumes."""
    return {
        "type": "debit" if debit else "credit",
        "amount": -amount if debit else amount,   # top-level amount is signed
        "date": d.isoformat(),
        "merchantName": merchant,
        "category": "GENERAL_SERVICES_OTHER_GENERAL_SERVICES",
        "description": merchant,
        "rawData": {
            "amount": amount,                     # rawData amount is unsigned magnitude
            "date": d.isoformat(),
            "merchant_name": merchant,
            "counterparties": [],
        },
    }


def _monthly_charges(merchant, amount, start, n, gap=30, debit=True):
    return [_txn(merchant, amount, start + timedelta(days=gap * i), debit) for i in range(n)]


class TestMonthlyStream(unittest.TestCase):
    def setUp(self):
        # 4 charges ~30 days apart; last one is the dataset max date
        self.start = date(2026, 3, 1)
        self.txns = _monthly_charges("Netflix", 15.49, self.start, 4, gap=30)
        found = recurring.streams(self.txns)
        self.assertEqual(len(found), 1, "exactly one stream expected")
        self.s = found[0]

    def test_cadence_monthly(self):
        self.assertEqual(self.s["cadence"], "monthly")
        self.assertEqual(self.s["per_year"], 12)

    def test_direction_outflow(self):
        self.assertEqual(self.s["direction"], "outflow")

    def test_n_and_amounts(self):
        self.assertEqual(self.s["n"], 4)
        self.assertEqual(self.s["last_amount"], 15.49)
        self.assertEqual(self.s["avg_amount"], 15.49)

    def test_dates(self):
        self.assertEqual(self.s["first_date"], self.start)
        self.assertEqual(self.s["last_date"], self.start + timedelta(days=90))

    def test_next_date(self):
        # last_date + median gap (30 days)
        expected = self.start + timedelta(days=90) + timedelta(days=30)
        self.assertEqual(self.s["next_date"], expected)

    def test_is_active_true(self):
        # last charge IS the dataset max date -> active
        self.assertTrue(self.s["is_active"])

    def test_annualized(self):
        self.assertAlmostEqual(recurring.annualized(self.s), 15.49 * 12, places=2)


class TestInactiveStream(unittest.TestCase):
    def test_lapsed_outflow_is_inactive(self):
        # a monthly stream that stopped, plus a much later unrelated charge that
        # advances the dataset max date well beyond 1.5 median-gaps
        txns = _monthly_charges("OldGym", 40.0, date(2026, 1, 1), 3, gap=30)
        # Rent runs much longer, pushing the dataset max date well past
        # 1.5 monthly cycles after OldGym's last charge (2026-03-02).
        txns += _monthly_charges("Rent", 1200.0, date(2026, 1, 5), 8, gap=30)
        found = {s["merchant"]: s for s in recurring.streams(txns)}
        self.assertIn("OldGym", found)
        self.assertFalse(found["OldGym"]["is_active"],
                         "stream silent >1.5 cycles before max date must be inactive")
        self.assertTrue(found["Rent"]["is_active"])


class TestInflowDetection(unittest.TestCase):
    def test_biweekly_paycheck_is_inflow(self):
        txns = _monthly_charges("ACME Payroll", 2000.0, date(2026, 4, 3), 5,
                                gap=14, debit=False)
        found = recurring.streams(txns)
        self.assertEqual(len(found), 1)
        s = found[0]
        self.assertEqual(s["direction"], "inflow")
        self.assertEqual(s["cadence"], "biweekly")
        self.assertEqual(s["per_year"], 26)


class TestSplitSameMerchant(unittest.TestCase):
    def test_inflow_and_outflow_same_merchant_are_two_streams(self):
        merchant = "Venmo"
        txns = _monthly_charges(merchant, 50.0, date(2026, 1, 1), 3, gap=30, debit=True)
        txns += _monthly_charges(merchant, 75.0, date(2026, 1, 10), 3, gap=30, debit=False)
        dirs = sorted(s["direction"] for s in recurring.streams(txns))
        self.assertEqual(dirs, ["inflow", "outflow"])


class TestThresholds(unittest.TestCase):
    def test_two_charges_not_a_stream(self):
        txns = _monthly_charges("Rare", 9.99, date(2026, 5, 1), 2, gap=30)
        self.assertEqual(recurring.streams(txns), [])

    def test_irregular_gaps_not_classified(self):
        # gaps of 2, 200, 5 days -> median 5 lands in weekly band only if median is 5;
        # build clearly non-cadence: 3,300 day gaps -> median 300 (~ between bands) no hit
        d0 = date(2026, 1, 1)
        txns = [_txn("Chaos", 10.0, d0),
                _txn("Chaos", 10.0, d0 + timedelta(days=3)),
                _txn("Chaos", 10.0, d0 + timedelta(days=303))]
        # median gap = 300 -> not in any cadence band
        self.assertEqual(recurring.streams(txns), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
