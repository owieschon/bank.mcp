#!/usr/bin/env python3
"""
test_finance_agent.py — unit tests for finance_agent.py's own math + invariants.

The four cores (forecaster, budget_scorer, fee_fraud_scan, recurring) have their
own test suites; here we test the UNIFIED runner's additive logic:

  - the combined digest contains NO raw transaction rows (the load-bearing rule),
  - section headline numbers are carried through faithfully from the cores,
  - the recurring monthly-runrate math (annualized/12, net) is correct,
  - the no-balance path reports the forecast UNAVAILABLE (and the run continues),
  - the headline line reflects forecast status + budget pace + fee/fraud total,
  - the digest is JSON-serializable and compact.

Run: python3 test_finance_agent.py
"""

import datetime as dt
import json
import os
import unittest

from finance_mcp.store import subscription_creep as sc
from finance_mcp.engines import budget_scorer as bs
from finance_mcp.engines import cashflow_forecaster as cf
from finance_mcp.engines import fee_fraud_scan as ff
from finance_mcp.engines import recurring as rec
from finance_mcp import finance_agent as fa

_HERE = os.path.dirname(os.path.abspath(__file__))
TXNS_PATH = os.path.join(_HERE, "fixtures", "transactions.sample.json")
RULES_PATH = os.path.join(_HERE, "..", "examples", "rules.example.md")


def _txn(date, amount, debit=True, merchant="ACME", cat="GENERAL_MERCHANDISE_OTHER"):
    """A minimal txn row in the dataset's shape (signed top-level amount)."""
    signed = -abs(amount) if debit else abs(amount)
    return {
        "type": "debit" if debit else "credit",
        "amount": signed,
        "date": date,
        "merchantName": merchant,
        "category": cat,
        "description": merchant,
        "rawData": {
            "amount": abs(amount),
            "category": cat,
            "counterparties": [],
            "date": date,
        },
    }


class TestNoRawRows(unittest.TestCase):
    """The digest must never carry a raw transaction row into anything that could
    feed a prompt. We check by deep-walking the serialized digest for the
    tell-tale raw-row shape (a dict with both 'rawData' and 'merchantName')."""

    @classmethod
    def setUpClass(cls):
        cls.txns = sc.load_transactions(TXNS_PATH)
        cls.digest = fa.build_digest(
            cls.txns, balance=1200.0, mode="monthly",
            forecast_days=35, buffer=100.0, include_burn=True,
            scan_days=30, rules_path=RULES_PATH)

    def _walk(self, obj):
        if isinstance(obj, dict):
            yield obj
            for v in obj.values():
                yield from self._walk(v)
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                yield from self._walk(v)

    def test_no_rawdata_anywhere(self):
        for d in self._walk(self.digest):
            self.assertNotIn("rawData", d,
                             "raw transaction row leaked into the digest")

    def test_no_raw_row_signature(self):
        # a raw row in this feed has merchantName + rawData together
        for d in self._walk(self.digest):
            if "merchantName" in d and "rawData" in d:
                self.fail("raw transaction row signature found in digest")

    def test_digest_is_json_serializable(self):
        s = json.dumps(self.digest)   # raises if any non-serializable value
        self.assertIsInstance(s, str)

    def test_digest_is_compact(self):
        # ~1K tokens ≈ a few KB of JSON; guard against the whole feed sneaking in.
        size = len(json.dumps(self.digest))
        self.assertLess(size, 20000,
                        f"digest too large ({size} bytes) — likely carrying rows")


class TestSectionFidelity(unittest.TestCase):
    """Section headline numbers must equal what the underlying cores compute."""

    @classmethod
    def setUpClass(cls):
        cls.txns = sc.load_transactions(TXNS_PATH)

    def test_forecast_matches_core(self):
        sec = fa._forecast_section(self.txns, 1200.0, 35, 100.0, True)
        # The section now drives the forecast from the forward-plan registry +
        # budget; the reference call must use the same inputs to stay faithful.
        registry = fa.oblreg.load_registry()
        budget = registry.get("discretionary_budget_monthly") if registry else None
        summary, _ = cf.build_summary(self.txns, 1200.0, 35, 100.0, include_burn=True,
                                      registry=registry, discretionary_budget=budget)
        self.assertTrue(sec["available"])
        self.assertEqual(sec["headline"]["min_balance"],
                         summary["headline"]["min_balance"])
        self.assertEqual(sec["headline"]["overdraft"],
                         summary["headline"]["overdraft"])
        self.assertEqual(sec["headline"]["safe_by"],
                         summary["headline"]["safe_by"])
        self.assertEqual(sec["headline"]["projected_end_balance"],
                         summary["headline"]["projected_end_balance"])

    def test_budget_matches_core(self):
        sec = fa._budget_section(self.txns, RULES_PATH, "monthly", balance=1200.0)
        R = bs.parse_rules(RULES_PATH)
        s = bs.build_summary(self.txns, R, "monthly", balance=1200.0)
        self.assertEqual(sec["headline"]["running_total"],
                         s["goal"]["running_total"])
        self.assertEqual(sec["headline"]["pct_to_target"], s["goal"]["pct"])
        self.assertEqual(sec["headline"]["status"], s["goal"]["status"])
        self.assertEqual(sec["headline"]["net_saved_window"],
                         s["cashflow"]["net_saved"])

    def test_fee_fraud_matches_core(self):
        sec = fa._fee_fraud_section(self.txns, 30)
        s = ff.scan(self.txns, days=30)
        self.assertEqual(sec["headline"]["avoidable_plus_suspect"],
                         s["headline"]["avoidable_plus_suspect"])
        self.assertEqual(sec["headline"]["fees_total"], s["headline"]["fees_total"])
        self.assertEqual(sec["headline"]["dup_recoverable"],
                         s["headline"]["dup_recoverable"])


class TestRecurringRunrate(unittest.TestCase):
    """The recurring section's monthly-runrate math must be annualized/12 and the
    net must be inflow_runrate − outflow_runrate, on a controlled fixture."""

    def test_synthetic_runrate(self):
        # one weekly inflow of $100 (per_year ~52) and one monthly outflow of $30.
        # Both streams must end near the SAME max date or is_active (keyed off the
        # dataset max date) drops the staler one — so anchor both at the end.
        txns = []
        end = dt.date(2026, 5, 1)
        # weekly inflow: 8 occurrences, 7 days apart, last == end
        for i in range(8):
            txns.append(_txn(str(end - dt.timedelta(days=7 * i)), 100,
                             debit=False, merchant="PAYCHECK"))
        # monthly outflow: 5 occurrences ~30 days apart, last == end
        for i in range(5):
            txns.append(_txn(str(end - dt.timedelta(days=30 * i)), 30,
                             debit=True, merchant="GYM"))

        sec = fa._recurring_section(txns)
        h = sec["headline"]
        self.assertEqual(h["n_active_inflow"], 1)
        self.assertEqual(h["n_active_outflow"], 1)

        # recompute expected runrate straight from the core to stay in lock-step
        found = rec.streams(txns)
        ai = [s for s in found if s["direction"] == "inflow" and s["is_active"]]
        ao = [s for s in found if s["direction"] == "outflow" and s["is_active"]]
        exp_in = round(sum(rec.annualized(s) for s in ai) / 12.0, 2)
        exp_out = round(sum(rec.annualized(s) for s in ao) / 12.0, 2)
        self.assertEqual(h["inflow_monthly_runrate"], exp_in)
        self.assertEqual(h["outflow_monthly_runrate"], exp_out)
        self.assertEqual(h["net_monthly_runrate"], round(exp_in - exp_out, 2))

    def test_net_is_difference(self):
        txns = sc.load_transactions(TXNS_PATH)
        sec = fa._recurring_section(txns)
        h = sec["headline"]
        self.assertAlmostEqual(
            h["net_monthly_runrate"],
            round(h["inflow_monthly_runrate"] - h["outflow_monthly_runrate"], 2),
            places=2)

    def test_top_runrate_is_annualized_over_12(self):
        txns = sc.load_transactions(TXNS_PATH)
        sec = fa._recurring_section(txns)
        found = rec.streams(txns)
        bykey = {s["merchant"]: s for s in found if s["is_active"]}
        for row in sec["detail"]["top_outflow"]:
            s = bykey.get(row["merchant"])
            if s is None:
                continue
            self.assertEqual(row["monthly_runrate"],
                             round(rec.annualized(s) / 12.0, 2))


class TestNoBalancePath(unittest.TestCase):
    """With no balance the forecast section is reported unavailable, the run
    still produces a full digest, and the headline says so."""

    @classmethod
    def setUpClass(cls):
        cls.txns = sc.load_transactions(TXNS_PATH)
        cls.digest = fa.build_digest(
            cls.txns, balance=None, mode="monthly",
            forecast_days=35, buffer=100.0, include_burn=True,
            scan_days=30, rules_path=RULES_PATH)

    def test_forecast_unavailable(self):
        fc = self.digest["sections"]["forecast"]
        self.assertFalse(fc["available"])
        self.assertEqual(fc["headline"], {})
        self.assertTrue(any("skipped" in f.lower() for f in fc["flags"]))

    def test_other_sections_present(self):
        sec = self.digest["sections"]
        self.assertIn("budget", sec)
        self.assertIn("fee_fraud", sec)
        self.assertIn("recurring", sec)
        # budget still computed a real pct
        self.assertIn("pct_to_target", sec["budget"]["headline"])

    def test_headline_says_unavailable(self):
        hl = fa.headline_line(self.digest)
        self.assertIn("UNAVAILABLE", hl)


class TestHeadline(unittest.TestCase):
    """The headline carries forecast status + budget pace + fee/fraud total."""

    @classmethod
    def setUpClass(cls):
        cls.txns = sc.load_transactions(TXNS_PATH)

    def test_headline_components(self):
        digest = fa.build_digest(
            self.txns, balance=1200.0, mode="monthly",
            forecast_days=35, buffer=100.0, include_burn=True,
            scan_days=30, rules_path=RULES_PATH)
        hl = fa.headline_line(digest)
        h = digest["sections"]["forecast"]["headline"]
        fh = digest["sections"]["fee_fraud"]["headline"]
        # forecast status word
        status = ("OVERDRAFT" if h["overdraft"]
                  else ("LOW" if h["low_balance"] else "CLEAR"))
        self.assertIn(status, hl)
        self.assertIn("Goal", hl)
        # fee/fraud now reports recoverable $ + anomaly count (not a lumped total)
        self.assertIn(fa.money(fh["avoidable"]), hl)
        self.assertIn("recoverable", hl)

    def test_weekly_mode_flows_through(self):
        digest = fa.build_digest(
            self.txns, balance=1200.0, mode="weekly",
            forecast_days=35, buffer=100.0, include_burn=True,
            scan_days=30, rules_path=RULES_PATH)
        self.assertEqual(digest["mode"], "weekly")
        self.assertEqual(digest["sections"]["budget"]["mode"], "weekly")


class TestReconciliationSection(unittest.TestCase):
    """Receipt reconciliation: graceful degradation and digest integration."""

    @classmethod
    def setUpClass(cls):
        cls.txns = sc.load_transactions(TXNS_PATH)

    def test_reconciliation_section_present_in_digest(self):
        digest = fa.build_digest(
            self.txns, balance=1200.0, mode="monthly",
            forecast_days=35, buffer=100.0, include_burn=True,
            scan_days=30, rules_path=RULES_PATH)
        self.assertIn("reconciliation", digest["sections"])
        recon = digest["sections"]["reconciliation"]
        self.assertEqual(recon["tool"], "receipt_reconciliation")
        self.assertIn("headline", recon)

    def test_reconciliation_unavailable_gracefully(self):
        """Without receipts, the reconciliation section reports unavailable."""
        sec = fa._reconciliation_section(None)
        self.assertFalse(sec.get("available", True))
        self.assertIn("reason", sec)
        self.assertEqual(sec["headline"]["total_receipts"], 0)

    def test_reconciliation_with_receipts(self):
        """With synthetic receipts, the reconciliation engine runs correctly."""
        receipts = [
            {"thread_id": "t1", "merchant": "TestStore", "amount": 25.00,
             "date": "2026-01-15", "category": "GENERAL_MERCHANDISE",
             "subject": "Order confirmation", "from": "test@store.com",
             "type": "receipt"},
        ]
        # Run reconciliation with no matching transactions
        recon = fa.rs.reconcile(receipts, [])
        summary = fa.rs.reconciliation_to_summary(recon)
        self.assertEqual(summary["headline"]["total_receipts"], 1)
        self.assertEqual(summary["headline"]["matched"], 0)
        self.assertEqual(summary["tool"], "receipt_reconciliation")

    def test_render_includes_receipt_coverage(self):
        digest = fa.build_digest(
            self.txns, balance=1200.0, mode="monthly",
            forecast_days=35, buffer=100.0, include_burn=True,
            scan_days=30, rules_path=RULES_PATH)
        md = fa.render(digest)
        recon = digest["sections"]["reconciliation"]
        # If receipts are available, render shows coverage; if not, no section
        if recon.get("available", True) and recon["headline"]["total_receipts"] > 0:
            self.assertIn("coverage", md.lower())
        # Either way, no crash

    def test_headline_without_receipts_has_no_receipt_part(self):
        """When receipts are unavailable, the headline omits the receipt part."""
        digest = fa.build_digest(
            self.txns, balance=1200.0, mode="monthly",
            forecast_days=35, buffer=100.0, include_burn=True,
            scan_days=30, rules_path=RULES_PATH)
        recon = digest["sections"]["reconciliation"]
        hl = fa.headline_line(digest)
        if not recon.get("available", True) or recon["headline"]["total_receipts"] == 0:
            self.assertNotIn("receipts", hl.lower())

    def test_reconciliation_results_flow_to_fee_fraud(self):
        """Discrepancies and unmatched charges appear in the fee_fraud section."""
        digest = fa.build_digest(
            self.txns, balance=1200.0, mode="monthly",
            forecast_days=35, buffer=100.0, include_burn=True,
            scan_days=30, rules_path=RULES_PATH)
        fee = digest["sections"]["fee_fraud"]
        # These keys should exist even if empty
        self.assertIn("n_receipt_discrepancies", fee["headline"])
        self.assertIn("receipt_discrepancies", fee["detail"])
        self.assertIn("unverified_charges", fee["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
