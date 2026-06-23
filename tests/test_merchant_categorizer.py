#!/usr/bin/env python3
"""
test_merchant_categorizer.py — money/category/precedence math for the categorizer.

Run: python3 test_merchant_categorizer.py
Uses a temp overrides file so it never touches the real merchant_overrides.json.
"""

import os
import tempfile
import unittest

from finance_mcp.store import merchant_categorizer as mc
from finance_mcp.store import subscription_creep as sc


def txn(category=None, counterparties=None, merchant_name=None, descriptor=None,
        amount=-10.0, date="2026-01-15", pfc_conf="HIGH"):
    """Build a transaction in the real feed shape."""
    raw = {
        "amount": abs(amount),
        "date": date,
        "name": descriptor or merchant_name or "RAW DESCRIPTOR",
        "merchant_name": merchant_name,
        "merchant_entity_id": None,
        "category": None,  # rawData.category is null in this feed
        "counterparties": counterparties or [],
    }
    if category is not None:
        raw["personal_finance_category"] = {
            "detailed": category, "primary": category.split("_")[0],
            "confidence_level": pfc_conf, "version": "v2",
        }
    return {
        "type": "debit" if amount < 0 else "credit",
        "amount": amount,
        "date": date,
        "merchantName": merchant_name,
        "description": descriptor or merchant_name,
        "category": category or "",
        "rawData": raw,
    }


def cp(name, type_, conf="VERY_HIGH", entity_id="X"):
    return {"name": name, "type": type_, "confidence_level": conf,
            "entity_id": entity_id}


class TestPrecedence(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.remove(self.path)  # start absent

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    # --- counterparty resolution -------------------------------------------

    def test_doordash_marketplace_resolves_to_restaurant(self):
        t = txn(category="GENERAL_MERCHANDISE_SUPERSTORES",
                counterparties=[cp("Some Store", "merchant", "LOW"),
                                cp("DoorDash", "marketplace", "VERY_HIGH")],
                merchant_name="Some Store")
        res = mc.get_category(t, overrides={})
        self.assertEqual(res["category"], "FOOD_AND_DRINK_RESTAURANT")
        self.assertEqual(res["source"], "counterparty")
        self.assertEqual(res["confidence"], "high")

    def test_google_play_marketplace_resolves(self):
        t = txn(category="GENERAL_SERVICES_OTHER_GENERAL_SERVICES",
                counterparties=[cp("Cloud App", "merchant", "LOW"),
                                cp("Google Play Store", "marketplace", "VERY_HIGH")],
                merchant_name="Cloud App")
        res = mc.get_category(t, overrides={})
        self.assertEqual(res["category"], "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES")
        self.assertEqual(res["source"], "counterparty")

    def test_low_confidence_counterparty_does_not_fire(self):
        # DoorDash but LOW confidence -> fall through to PFC
        t = txn(category="FOOD_AND_DRINK_GROCERIES",
                counterparties=[cp("DoorDash", "marketplace", "LOW")],
                merchant_name="Mart")
        res = mc.get_category(t, overrides={})
        self.assertEqual(res["category"], "FOOD_AND_DRINK_GROCERIES")
        self.assertEqual(res["source"], "pfc")

    def test_generic_acquirer_square_is_not_a_signal(self):
        # Square is a generic terminal; PFC must win, source = pfc.
        t = txn(category="GENERAL_MERCHANDISE_TOBACCO_AND_VAPE",
                counterparties=[cp("Vape Shop", "merchant", "VERY_HIGH"),
                                cp("Square", "payment_terminal", "VERY_HIGH")],
                merchant_name="Vape Shop")
        res = mc.get_category(t, overrides={})
        self.assertEqual(res["category"], "GENERAL_MERCHANDISE_TOBACCO_AND_VAPE")
        self.assertEqual(res["source"], "pfc")

    # --- override precedence -----------------------------------------------

    def test_override_beats_pfc(self):
        t = txn(category="FOOD_AND_DRINK_RESTAURANT", merchant_name="Joe Cafe")
        key = mc._key_str(t)
        res = mc.get_category(t, overrides={key: "ENTERTAINMENT_TV_AND_MOVIES"})
        self.assertEqual(res["category"], "ENTERTAINMENT_TV_AND_MOVIES")
        self.assertEqual(res["source"], "override")
        self.assertEqual(res["confidence"], "high")

    def test_override_beats_counterparty(self):
        t = txn(category="GENERAL_MERCHANDISE_SUPERSTORES",
                counterparties=[cp("DoorDash", "marketplace", "VERY_HIGH")],
                merchant_name="Store")
        key = mc._key_str(t)
        res = mc.get_category(t, overrides={key: "MY_CUSTOM_CATEGORY"})
        self.assertEqual(res["category"], "MY_CUSTOM_CATEGORY")
        self.assertEqual(res["source"], "override")

    # --- PFC + heuristic fallback ------------------------------------------

    def test_pfc_used_when_no_counterparty(self):
        t = txn(category="TRANSPORTATION_GAS", merchant_name="Speedway")
        res = mc.get_category(t, overrides={})
        self.assertEqual(res["category"], "TRANSPORTATION_GAS")
        self.assertEqual(res["source"], "pfc")
        self.assertEqual(res["confidence"], "high")

    def test_pfc_medium_when_low_plaid_confidence(self):
        t = txn(category="FOOD_AND_DRINK_RESTAURANT", merchant_name="Diner",
                pfc_conf="LOW")
        res = mc.get_category(t, overrides={})
        self.assertEqual(res["confidence"], "medium")

    def test_heuristic_when_no_pfc(self):
        t = txn(category=None, merchant_name="Downtown Liquor & Wine",
                descriptor="DOWNTOWN LIQUOR WINE")
        res = mc.get_category(t, overrides={})
        self.assertEqual(res["category"], "FOOD_AND_DRINK_BEER_WINE_AND_LIQUOR")
        self.assertEqual(res["source"], "heuristic")
        self.assertEqual(res["confidence"], "low")

    # --- --set persistence --------------------------------------------------

    def test_set_override_persists_and_changes_result(self):
        t = txn(category="FOOD_AND_DRINK_RESTAURANT", merchant_name="Cloud 2zs",
                descriptor="CLOUD 2zS")
        # before: PFC
        before = mc.get_category(t, overrides=mc.load_overrides(self.path))
        self.assertEqual(before["source"], "pfc")
        # set an override by substring; pass the txn so it keys correctly
        matched = mc.set_override("Cloud", "ENTERTAINMENT_TV_AND_MOVIES",
                                  txns=[t], path=self.path)
        self.assertTrue(os.path.exists(self.path))
        self.assertTrue(any(k == mc._key_str(t) for k, _ in matched))
        # after: override wins, loaded fresh from disk
        after = mc.get_category(t, overrides=mc.load_overrides(self.path))
        self.assertEqual(after["category"], "ENTERTAINMENT_TV_AND_MOVIES")
        self.assertEqual(after["source"], "override")

    def test_set_override_fallback_when_no_txns(self):
        matched = mc.set_override("Mystery Merchant", "SOME_CATEGORY",
                                  txns=None, path=self.path)
        self.assertEqual(len(matched), 1)
        self.assertTrue(matched[0][0].startswith("name:"))
        loaded = mc.load_overrides(self.path)
        self.assertIn(matched[0][0], loaded)


class TestConflictAndReview(unittest.TestCase):
    def test_conflict_detected_when_counterparty_disagrees_with_pfc(self):
        t = txn(category="GENERAL_MERCHANDISE_SUPERSTORES",
                counterparties=[cp("DoorDash", "marketplace", "VERY_HIGH")],
                merchant_name="Store")
        reason = mc.conflict_for(t, overrides={})
        self.assertIsNotNone(reason)
        self.assertIn("DoorDash", reason)

    def test_no_conflict_when_counterparty_confirms_pfc(self):
        t = txn(category="FOOD_AND_DRINK_RESTAURANT",
                counterparties=[cp("DoorDash", "marketplace", "VERY_HIGH")],
                merchant_name="Store")
        self.assertIsNone(mc.conflict_for(t, overrides={}))

    def test_alcohol_flagged_in_report(self):
        txns = [
            txn(category="FOOD_AND_DRINK_BEER_WINE_AND_LIQUOR",
                merchant_name="Lucky's Beverage", amount=-25.08),
            txn(category="FOOD_AND_DRINK_BEER_WINE_AND_LIQUOR",
                merchant_name="Lucky's Beverage", amount=-7.99),
            txn(category="FOOD_AND_DRINK_RESTAURANT", merchant_name="Diner",
                amount=-12.0),
        ]
        rep = mc._build_report(txns, overrides={})
        self.assertAlmostEqual(rep["alcohol_total"], 33.07, places=2)
        self.assertTrue(any("alcohol" in " ".join(r["reasons"]).lower()
                            for r in rep["review"]))


class TestAlcoholTotal(unittest.TestCase):
    """Alcohol total must sum all outflows tagged BEER_WINE_AND_LIQUOR
    (by raw PFC or refined category), using synthetic data so the test
    is environment-independent."""

    def test_alcohol_total_from_synthetic(self):
        txns = [
            txn(category="FOOD_AND_DRINK_BEER_WINE_AND_LIQUOR",
                merchant_name="Corner Liquor", amount=-50.00, date="2026-02-01"),
            txn(category="FOOD_AND_DRINK_BEER_WINE_AND_LIQUOR",
                merchant_name="Wine Warehouse", amount=-32.57, date="2026-02-03"),
            txn(category="FOOD_AND_DRINK_BEER_WINE_AND_LIQUOR",
                merchant_name="Beer Barn", amount=-150.00, date="2026-02-05"),
            # Non-alcohol: should NOT be counted
            txn(category="FOOD_AND_DRINK_RESTAURANT",
                merchant_name="Diner", amount=-20.00, date="2026-02-02"),
            # Credit (inflow) — should NOT be counted (is_outflow = False)
            txn(category="FOOD_AND_DRINK_BEER_WINE_AND_LIQUOR",
                merchant_name="Refund Liquor", amount=10.00, date="2026-02-04"),
        ]
        rep = mc._build_report(txns, overrides={})
        self.assertAlmostEqual(rep["alcohol_total"], 232.57, places=2)

    def test_alcohol_total_includes_reclassified_pfc(self):
        """An item whose raw PFC is BEER_WINE_AND_LIQUOR still counts toward
        alcohol_total even if refinement reclassifies it (e.g. via counterparty)."""
        txns = [
            txn(category="FOOD_AND_DRINK_BEER_WINE_AND_LIQUOR",
                counterparties=[cp("Toast", "payment_terminal", "VERY_HIGH")],
                merchant_name="Tavern", amount=-45.00, date="2026-03-01"),
        ]
        rep = mc._build_report(txns, overrides={})
        self.assertAlmostEqual(rep["alcohol_total"], 45.00, places=2)


class TestReclassification(unittest.TestCase):
    """Reclassification count must reflect txns whose refined category differs
    from their raw PFC, using synthetic data with known counterparty mappings."""

    def test_reclassification_moves_some_txns(self):
        txns = [
            # DoorDash marketplace → FOOD_AND_DRINK_RESTAURANT (moves from superstore)
            txn(category="GENERAL_MERCHANDISE_SUPERSTORES",
                counterparties=[cp("DoorDash", "marketplace", "VERY_HIGH")],
                merchant_name="FoodPlace", amount=-25.00, date="2026-02-01"),
            # Google Play → ONLINE_MARKETPLACES (moves from general services)
            txn(category="GENERAL_SERVICES_OTHER_GENERAL_SERVICES",
                counterparties=[cp("Google Play Store", "marketplace", "VERY_HIGH")],
                merchant_name="SomeApp", amount=-5.99, date="2026-02-02"),
            # No reclassification: PFC matches refined
            txn(category="FOOD_AND_DRINK_RESTAURANT",
                merchant_name="Diner", amount=-12.00, date="2026-02-03"),
        ]
        rep = mc._build_report(txns, overrides={})
        self.assertGreater(rep["reclassified"], 0)
        # At least the DoorDash and Google Play rows should move
        self.assertGreaterEqual(rep["reclassified"], 2)

    def test_n_outflows_matches_actual_count(self):
        txns = [
            txn(category="FOOD_AND_DRINK_RESTAURANT", merchant_name="A",
                amount=-10.00, date="2026-02-01"),
            txn(category="FOOD_AND_DRINK_GROCERIES", merchant_name="B",
                amount=-20.00, date="2026-02-02"),
            # Credit — not an outflow
            txn(category="INCOME_WAGES", merchant_name="Employer",
                amount=1000.00, date="2026-02-01"),
        ]
        rep = mc._build_report(txns, overrides={})
        expected = sum(1 for t in txns
                       if sc.is_outflow(t) and sc.amount_magnitude(t) is not None)
        self.assertEqual(rep["n_outflows"], expected)


class TestSummaryNoRawRows(unittest.TestCase):
    """The report summary must not carry rawData or PFC internals."""

    def test_summary_has_no_raw_transaction_rows(self):
        txns = [
            txn(category="FOOD_AND_DRINK_RESTAURANT", merchant_name="Place",
                amount=-15.00, date="2026-02-01"),
        ]
        rep = mc._build_report(txns, overrides={})
        blob = repr(rep)
        self.assertNotIn("rawData", blob)
        self.assertNotIn("personal_finance_category", blob)


if __name__ == "__main__":
    unittest.main(verbosity=2)
