#!/usr/bin/env python3
"""
test_transport.py — the integration layer: snapshot transport, multi-Item config
resolution, and delivery's date/window formatters. These are the bank-facing seams
(the "integration craft"), so they get real coverage, not just the pure engines.
"""
import json
import os
import tempfile
import unittest
from unittest import mock

from bank_mcp.ingest import plaid_bridge
from bank_mcp.report import delivery


class SnapshotTransportTest(unittest.TestCase):
    def test_fetch_from_snapshot_normalizes(self):
        raw = [{
            "transaction_id": "t1", "amount": 12.34, "date": "2026-06-01",
            "name": "Coffee Shop", "merchant_name": "Coffee Shop",
            "personal_finance_category": {"detailed": "FOOD_AND_DRINK_COFFEE"},
        }]
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            with open(path, "w") as f:
                json.dump(raw, f)
            txns = plaid_bridge.fetch_from_snapshot(path)
        finally:
            os.unlink(path)
        self.assertEqual(len(txns), 1)
        t = txns[0]
        self.assertIn("rawData", t)                 # normalized two-level shape
        self.assertEqual(t["amount"], -12.34)        # Plaid positive debit -> signed negative
        self.assertEqual(t["type"], "debit")
        self.assertEqual(t["merchantName"], "Coffee Shop")


class LoadItemsConfigTest(unittest.TestCase):
    def setUp(self):
        # point the config path somewhere that doesn't exist, to exercise the fallback
        self._patch = mock.patch.object(plaid_bridge, "PLAID_ITEMS_PATH", "/nonexistent/plaid_items.json")
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self._old = os.environ.pop("PLAID_ACCESS_TOKEN", None)
        self.addCleanup(lambda: os.environ.__setitem__("PLAID_ACCESS_TOKEN", self._old) if self._old else None)

    def test_single_token_fallback(self):
        with mock.patch.object(plaid_bridge, "_plaid_access_token", return_value="access-xyz"):
            items = plaid_bridge.load_plaid_items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "default")
        self.assertEqual(items[0]["owner"], "primary")

    def test_no_token_no_config_is_empty(self):
        with mock.patch.object(plaid_bridge, "_plaid_access_token", return_value=None):
            self.assertEqual(plaid_bridge.load_plaid_items(), [])

    def test_config_file_items_win(self):
        cfg = [{"name": "primary-checking", "token_env": "PLAID_ACCESS_TOKEN",
                "owner": "primary", "cursor_file": "~/c.json"}]
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            with open(path, "w") as f:
                json.dump(cfg, f)
            with mock.patch.object(plaid_bridge, "PLAID_ITEMS_PATH", path):
                items = plaid_bridge.load_plaid_items()
        finally:
            os.unlink(path)
        self.assertEqual(items[0]["name"], "primary-checking")
        self.assertTrue(os.path.isabs(items[0]["cursor_file"]))   # ~ expanded


class DeliveryFormatTest(unittest.TestCase):
    def test_fmt_date(self):
        self.assertEqual(delivery.fmt_date("2026-06-24"), "Jun 24, 2026")
        self.assertEqual(delivery.fmt_date(""), "")          # passthrough on empty
        self.assertEqual(delivery.fmt_date("not-a-date"), "not-a-date")

    def test_format_window_full_month_vs_range(self):
        self.assertEqual(delivery._format_window({"start": "2026-05-01", "end": "2026-05-31"}),
                         "May 2026")                          # full calendar month
        out = delivery._format_window({"start": "2026-05-03", "end": "2026-05-20"})
        self.assertIn("2026-05-03", out)                      # partial -> range


if __name__ == "__main__":
    unittest.main(verbosity=2)
