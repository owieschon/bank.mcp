#!/usr/bin/env python3
"""
test_analytics.py — verify the SQL analytical read-models (store/queries.sql) by
cross-checking every result against an independent Python recomputation over the
same synthetic dataset. If the SQL and the Python disagree, one of them is wrong.

The SQL aggregates the raw stored rows (it is descriptive reporting, so it does not
apply the engines' transfer/income exclusions); the Python checks mirror that.
"""
import unittest
from collections import defaultdict

from finance_mcp import demo
from finance_mcp.store import db, analytics


class AnalyticsSQLTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.txns = demo.generate()
        cls.conn = db.connect(":memory:")
        db.init_schema(cls.conn)
        db.upsert_transactions(cls.conn, cls.txns)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    # --- Python reference computations (over the same raw rows the SQL sees) ---
    @staticmethod
    def _is_credit(t):
        return t["amount"] >= 0

    def test_monthly_cashflow_matches_python(self):
        inc, spend = defaultdict(float), defaultdict(float)
        for t in self.txns:
            mo, amt = t["date"][:7], abs(t["amount"])
            (inc if self._is_credit(t) else spend)[mo] += amt

        rows = analytics.monthly_cashflow(self.conn)
        self.assertEqual([r["month"] for r in rows], sorted(inc | spend))

        running = 0.0
        prev_net = None
        for r in rows:
            mo = r["month"]
            self.assertAlmostEqual(r["income"], round(inc[mo], 2), places=2)
            self.assertAlmostEqual(r["spend"], round(spend[mo], 2), places=2)
            net = round(inc[mo] - spend[mo], 2)
            self.assertAlmostEqual(r["net"], net, places=2)
            running += net
            self.assertAlmostEqual(r["running_net"], round(running, 2), places=2)
            if prev_net is None:
                self.assertIsNone(r["net_mom_change"])
            else:
                self.assertAlmostEqual(r["net_mom_change"], round(net - prev_net, 2), places=2)
            prev_net = net

    def test_category_breakdown_matches_python(self):
        spend = defaultdict(float)
        for t in self.txns:
            if not self._is_credit(t):
                spend[t["category"] or "UNCATEGORIZED"] += abs(t["amount"])

        rows = analytics.category_breakdown(self.conn)
        self.assertEqual(len(rows), len(spend))
        # ordered by total desc
        totals = [r["total"] for r in rows]
        self.assertEqual(totals, sorted(totals, reverse=True))
        by_cat = {r["category"]: r for r in rows}
        for cat, total in spend.items():
            self.assertAlmostEqual(by_cat[cat]["total"], round(total, 2), places=2)
        # shares sum to ~100%
        self.assertAlmostEqual(sum(r["pct_of_spend"] for r in rows), 100.0, places=0)

    def test_top_merchants_ranked_and_capped(self):
        spend = defaultdict(float)
        for t in self.txns:
            if not self._is_credit(t):
                spend[t["merchantName"]] += abs(t["amount"])
        expected_top = max(spend, key=spend.get)

        rows = analytics.top_merchants(self.conn, limit=5)
        self.assertLessEqual(len(rows), 5)
        self.assertEqual(rows[0]["rank"], 1)
        self.assertEqual(rows[0]["merchant"], expected_top)
        self.assertAlmostEqual(rows[0]["total"], round(spend[expected_top], 2), places=2)
        # rank is monotonic with descending spend
        self.assertEqual([r["total"] for r in rows], sorted((r["total"] for r in rows), reverse=True))

    def test_owner_filter_isolates(self):
        # all demo rows are owner='primary'; a different owner yields nothing
        self.assertEqual(analytics.monthly_cashflow(self.conn, owner="nobody"), [])
        self.assertTrue(analytics.monthly_cashflow(self.conn, owner="primary"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
