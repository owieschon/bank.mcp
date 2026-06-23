#!/usr/bin/env python3
"""
test_plaid_bridge.py — tests for the Plaid-to-local bridge.

Tests the transaction normalizer, cursor persistence, and transport fallback
logic. Uses isolated temp files and mocked transports.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from finance_mcp.ingest import plaid_bridge


class NormalizePlaidTxnTest(unittest.TestCase):
    """Test normalize_plaid_txn maps Plaid → bank-mcp shape correctly."""

    def test_already_bankmcp_shape_passthrough(self):
        """If rawData is present, return as-is (bank-mcp already normalized)."""
        txn = {
            "id": "TX_1",
            "amount": -42.50,
            "date": "2026-06-10",
            "type": "debit",
            "category": "FOOD_AND_DRINK_RESTAURANT",
            "merchantName": "Pizza Place",
            "rawData": {"transaction_id": "TX_1", "amount": 42.50},
        }
        result = plaid_bridge.normalize_plaid_txn(txn)
        self.assertIs(result, txn)  # exact same object (passthrough)

    def test_raw_plaid_to_bankmcp_shape(self):
        """Convert a raw Plaid response into bank-mcp shape."""
        raw = {
            "transaction_id": "plaid_tx_123",
            "amount": 15.99,  # Plaid: positive = debit
            "date": "2026-06-12",
            "merchant_name": "Netflix",
            "name": "NETFLIX.COM",
            "pending": False,
            "personal_finance_category": {
                "detailed": "ENTERTAINMENT_TV_AND_MOVIES",
                "primary": "ENTERTAINMENT",
            },
        }
        result = plaid_bridge.normalize_plaid_txn(raw)

        self.assertEqual(result["id"], "plaid_tx_123")
        self.assertEqual(result["amount"], -15.99)  # signed: debit is negative
        self.assertEqual(result["date"], "2026-06-12")
        self.assertEqual(result["type"], "debit")
        self.assertEqual(result["category"], "ENTERTAINMENT_TV_AND_MOVIES")
        self.assertEqual(result["merchantName"], "Netflix")
        self.assertIs(result["rawData"], raw)  # original preserved

    def test_credit_stays_positive(self):
        """Plaid credits (negative amount) stay positive after normalization."""
        raw = {
            "transaction_id": "plaid_credit_1",
            "amount": -500.00,  # Plaid: negative = credit/income
            "date": "2026-06-15",
            "merchant_name": "EMPLOYER INC",
            "name": "PAYROLL",
        }
        result = plaid_bridge.normalize_plaid_txn(raw)
        self.assertEqual(result["amount"], 500.00)
        self.assertEqual(result["type"], "credit")

    def test_missing_fields_safe(self):
        """Normalizer handles missing fields without crashing."""
        raw = {"transaction_id": "sparse", "amount": 5.00}
        result = plaid_bridge.normalize_plaid_txn(raw)
        self.assertEqual(result["id"], "sparse")
        self.assertEqual(result["amount"], -5.00)
        self.assertEqual(result["date"], "")
        self.assertEqual(result["merchantName"], "")
        self.assertFalse(result["pending"])


class CursorPersistenceTest(unittest.TestCase):
    """Test cursor load/save with isolated temp files."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "cursor.json")

    def test_load_nonexistent_returns_default(self):
        result = plaid_bridge.load_cursor(self.path)
        self.assertIsNone(result["cursor"])
        self.assertIsNone(result["last_sync"])

    def test_save_then_load_roundtrips(self):
        plaid_bridge.save_cursor("cur_abc_123", self.path)
        result = plaid_bridge.load_cursor(self.path)
        self.assertEqual(result["cursor"], "cur_abc_123")
        self.assertIsNotNone(result["last_sync"])

    def test_save_overwrites(self):
        plaid_bridge.save_cursor("old_cursor", self.path)
        plaid_bridge.save_cursor("new_cursor", self.path)
        result = plaid_bridge.load_cursor(self.path)
        self.assertEqual(result["cursor"], "new_cursor")

    def test_corrupt_file_returns_default(self):
        with open(self.path, "w") as f:
            f.write("not json{{{")
        result = plaid_bridge.load_cursor(self.path)
        self.assertIsNone(result["cursor"])


class FetchFromSnapshotTest(unittest.TestCase):
    """Test file-based fallback path."""

    def test_snapshot_normalizes_txns(self):
        txns = [
            {
                "id": "TX_SNAP_1",
                "amount": -25.00,
                "date": "2026-06-01",
                "type": "debit",
                "merchantName": "Test Store",
                "rawData": {
                    "transaction_id": "TX_SNAP_1",
                    "amount": 25.00,
                    "date": "2026-06-01",
                },
            }
        ]
        path = os.path.join(tempfile.mkdtemp(), "snap.json")
        with open(path, "w") as f:
            json.dump(txns, f)

        result = plaid_bridge.fetch_from_snapshot(path)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "TX_SNAP_1")
        self.assertIn("rawData", result[0])


class CheckConnectionTest(unittest.TestCase):
    """Test the connection status checker."""

    def test_returns_status_dict(self):
        status = plaid_bridge.check_connection()
        self.assertIn("any_available", status)
        self.assertIn("bank_mcp_subprocess", status)
        self.assertIn("bank_mcp_http", status)
        self.assertIn("plaid_direct", status)
        self.assertIsInstance(status["any_available"], bool)


class TransportFallbackTest(unittest.TestCase):
    """Test that _call tries transports in order and collects errors."""

    def test_all_fail_raises_bankmcperror(self):
        """When all transports fail, BankMCPError is raised with all errors."""
        # Force every transport to fail so the test is hermetic regardless of
        # whether a real bank-mcp server happens to be reachable on this machine.
        def boom(method, params):
            raise plaid_bridge.BankMCPError("simulated transport failure")

        with mock.patch.object(plaid_bridge, "_try_bank_mcp_subprocess", boom), \
                mock.patch.object(plaid_bridge, "_try_bank_mcp_http", boom), \
                mock.patch.object(plaid_bridge, "_try_plaid_direct", boom):
            with self.assertRaises(plaid_bridge.BankMCPError) as ctx:
                plaid_bridge._call("sync_transactions", {})
        # The error should mention multiple transport failures
        msg = str(ctx.exception)
        self.assertIn("All transports failed", msg)


class FetchRangeCompletenessTest(unittest.TestCase):
    """fetch_transactions_range must return COMPLETE history despite the per-call
    cap — by walking windows and bisecting saturated ones. Mocks the single-call
    primitive so no bank/network is touched."""

    import datetime as _dt

    @staticmethod
    def _dataset(start, n, span_days):
        base = FetchRangeCompletenessTest._dt.date.fromisoformat(start)
        return [{"id": f"t{i}", "rawData": {"transaction_id": f"t{i}"},
                 "date": (base + FetchRangeCompletenessTest._dt.timedelta(
                     days=(i * span_days) // n)).isoformat()} for i in range(n)]

    @staticmethod
    def _capped_fake(dataset, limit):
        def fake(date_from, date_to, limit=limit, **kwargs):
            sel = [t for t in dataset if date_from <= t["date"] <= date_to]
            return sel[:limit]                     # the cap that truncates
        return fake

    def test_collects_full_span_across_windows(self):
        data = self._dataset("2025-06-01", 300, 360)
        with mock.patch.object(plaid_bridge, "fetch_transactions_list",
                               side_effect=self._capped_fake(data, 500)):
            got = plaid_bridge.fetch_transactions_range(
                "2025-06-01", "2026-05-31", window_days=90, limit=500)
        self.assertEqual({t["id"] for t in got}, {t["id"] for t in data})

    def test_bisects_on_saturation_no_truncation(self):
        # 600 txns in a single 90-day window > cap(500): non-bisecting fetch loses
        # 100. Bisection must recover all 600.
        data = self._dataset("2026-01-01", 600, 89)
        with mock.patch.object(plaid_bridge, "fetch_transactions_list",
                               side_effect=self._capped_fake(data, 500)):
            got = plaid_bridge.fetch_transactions_range(
                "2026-01-01", "2026-03-31", window_days=90, limit=500)
        self.assertEqual(len(got), 600)

    def test_dedupes_duplicate_ids(self):
        data = self._dataset("2026-01-01", 10, 10)
        with mock.patch.object(plaid_bridge, "fetch_transactions_list",
                               side_effect=self._capped_fake(data + data, 500)):
            got = plaid_bridge.fetch_transactions_range(
                "2026-01-01", "2026-01-31", window_days=15, limit=500)
        self.assertEqual(len(got), 10)


class FetchBalanceTest(unittest.TestCase):
    """fetch_balance must parse both transport response shapes."""

    def test_bankmcp_list_shape(self):
        rows = [{"type": "current", "amount": 950.00, "currency": "USD"},
                {"type": "available", "amount": 1000.00, "currency": "USD"}]
        with mock.patch.object(plaid_bridge, "_call", return_value=rows):
            b = plaid_bridge.fetch_balance()
        self.assertEqual(b["available"], 1000.00)
        self.assertEqual(b["current"], 950.00)
        self.assertEqual(b["currency"], "USD")

    def test_plaid_accounts_shape(self):
        resp = {"accounts": [{"balances": {"available": 100.0, "current": 90.0,
                                            "iso_currency_code": "USD"}}]}
        with mock.patch.object(plaid_bridge, "_call", return_value=resp):
            b = plaid_bridge.fetch_balance()
        self.assertEqual(b["available"], 100.0)
        self.assertEqual(b["current"], 90.0)

    def test_missing_amounts_safe(self):
        with mock.patch.object(plaid_bridge, "_call", return_value=[]):
            b = plaid_bridge.fetch_balance()
        self.assertIsNone(b["available"])
        self.assertIsNone(b["current"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
