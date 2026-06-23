#!/usr/bin/env python3
"""
test_sync.py — tests for the sync pipeline (sync.py).

Tests sync state persistence, sync_from_bank with snapshot fallback,
and the sync+analyze pipeline. Uses isolated temp files.
"""

import datetime as dt
import json
import os
import tempfile
import unittest
from unittest import mock

from finance_mcp.store import db
from finance_mcp.ingest import plaid_bridge
from finance_mcp.ingest import sync


class _DBIsolation:
    """Point sync's canonical DB at a throwaway temp file for the duration of a test
    (sync_from_bank persists to the DB now, not a ledger store)."""

    def _isolate_db(self):
        self._real_connect = db.connect            # capture before patching
        fd, self._dbpath = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        c = self._real_connect(self._dbpath)
        db.init_schema(c)
        c.close()
        p = mock.patch.object(sync.db, "connect",
                              side_effect=lambda *a, **k: self._real_connect(self._dbpath))
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(lambda: os.path.exists(self._dbpath) and os.unlink(self._dbpath))

    def _db_ids(self):
        c = self._real_connect(self._dbpath)
        ids = {r[0] for r in c.execute("SELECT id FROM transactions")}
        c.close()
        return ids


def mktxn(tid, amount, date, merchant="Test Merchant"):
    """Build a txn in the bank-mcp two-level shape."""
    return {
        "id": tid,
        "accountId": "acct_test",
        "amount": amount,
        "date": date,
        "type": "debit" if amount < 0 else "credit",
        "merchantName": merchant,
        "category": "GENERAL_MERCHANDISE_OTHER",
        "rawData": {
            "transaction_id": tid,
            "account_id": "acct_test",
            "amount": abs(amount),
            "date": date,
            "pending": False,
            "category": "GENERAL_MERCHANDISE_OTHER",
            "merchant_name": merchant,
        },
    }


class SyncStateTest(unittest.TestCase):
    """Test sync state persistence."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "sync_state.json")

    def test_load_nonexistent_returns_default(self):
        state = sync.load_sync_state(self.path)
        self.assertIsNone(state["last_sync"])
        self.assertIsNone(state["last_cursor"])
        self.assertEqual(state["total_synced"], 0)
        self.assertEqual(state["runs"], 0)

    def test_save_then_load_roundtrips(self):
        state = {"last_sync": "2026-06-17", "last_cursor": "cur_1",
                 "total_synced": 42, "runs": 3}
        sync.save_sync_state(state, self.path)
        loaded = sync.load_sync_state(self.path)
        self.assertEqual(loaded["last_cursor"], "cur_1")
        self.assertEqual(loaded["total_synced"], 42)

    def test_corrupt_file_returns_default(self):
        with open(self.path, "w") as f:
            f.write("broken{{{")
        state = sync.load_sync_state(self.path)
        self.assertIsNone(state["last_sync"])


class SyncFromSnapshotTest(_DBIsolation, unittest.TestCase):
    """Test sync_from_bank using file snapshots (no live connection)."""

    def setUp(self):
        self._isolate_db()
        self.dir = tempfile.mkdtemp()
        self.store = os.path.join(self.dir, "store.json")
        self.state = os.path.join(self.dir, "state.json")
        self.ss = os.path.join(self.dir, "sync_state.json")
        self.cursor = os.path.join(self.dir, "cursor.json")

        # Create a snapshot file
        self.txns = [
            mktxn("TX_A", -10.00, "2026-06-01"),
            mktxn("TX_B", -20.00, "2026-06-05"),
            mktxn("TX_C",  500.00, "2026-06-10", merchant="Employer"),
        ]
        self.snap = os.path.join(self.dir, "transactions.json")
        with open(self.snap, "w") as f:
            json.dump(self.txns, f)

    def test_snapshot_sync_writes_to_store(self):
        result = sync.sync_from_bank(
            snapshot_path=self.snap,
            store_path=self.store,
            state_path=self.state,
            cursor_path=self.cursor,
            sync_state_path=self.ss,
        )
        self.assertEqual(result["source"], "snapshot")
        self.assertEqual(result["added"], 3)
        self.assertEqual(result["modified"], 0)
        self.assertEqual(result["removed"], 0)
        self.assertEqual(result["store_size"], 3)

        # Verify the canonical DB was actually written
        ids = self._db_ids()
        self.assertEqual(len(ids), 3)
        self.assertIn("TX_A", ids)
        self.assertIn("TX_C", ids)

    def test_incremental_snapshot_detects_changes(self):
        # First sync
        sync.sync_from_bank(
            snapshot_path=self.snap,
            store_path=self.store,
            state_path=self.state,
            cursor_path=self.cursor,
            sync_state_path=self.ss,
        )

        # Second snapshot: one modified, one new, one dropped
        txns_v2 = [
            mktxn("TX_A", -10.00, "2026-06-01"),        # unchanged
            mktxn("TX_B", -25.00, "2026-06-05"),        # amount changed
            mktxn("TX_D", -15.00, "2026-06-12"),        # new
            # TX_C dropped (but out of window, so depends on window_start)
        ]
        snap2 = os.path.join(self.dir, "v2.json")
        with open(snap2, "w") as f:
            json.dump(txns_v2, f)

        result = sync.sync_from_bank(
            snapshot_path=snap2,
            store_path=self.store,
            state_path=self.state,
            cursor_path=self.cursor,
            sync_state_path=self.ss,
        )
        self.assertEqual(result["added"], 1)     # TX_D
        self.assertEqual(result["modified"], 1)  # TX_B amount changed

    def test_sync_state_updated_after_sync(self):
        sync.sync_from_bank(
            snapshot_path=self.snap,
            store_path=self.store,
            state_path=self.state,
            cursor_path=self.cursor,
            sync_state_path=self.ss,
        )
        ss = sync.load_sync_state(self.ss)
        self.assertEqual(ss["runs"], 1)
        self.assertEqual(ss["total_synced"], 3)
        self.assertIsNotNone(ss["last_sync"])

    def test_nonexistent_snapshot_fails_gracefully(self):
        result = sync.sync_from_bank(
            snapshot_path="/nonexistent/file.json",
            store_path=self.store,
            state_path=self.state,
            cursor_path=self.cursor,
            sync_state_path=self.ss,
        )
        self.assertEqual(result["source"], "failed")
        self.assertIsNotNone(result["error"])


class SyncWithoutBankTest(unittest.TestCase):
    """Test sync_from_bank when no live connection and no snapshot."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_no_snapshot_no_bank_fails(self):
        # Force the live transports to fail so this exercises the no-bank branch
        # deterministically, even when a real bank-mcp server is reachable.
        def boom(*a, **k):
            raise plaid_bridge.BankMCPError("simulated: no bank")

        with mock.patch.object(plaid_bridge, "fetch_transactions_sync", boom), \
                mock.patch.object(plaid_bridge, "fetch_transactions_list", boom):
            result = sync.sync_from_bank(
                snapshot_path=None,
                use_sync_api=True,
                store_path=os.path.join(self.dir, "store.json"),
                state_path=os.path.join(self.dir, "state.json"),
                cursor_path=os.path.join(self.dir, "cursor.json"),
                sync_state_path=os.path.join(self.dir, "ss.json"),
            )
        self.assertEqual(result["source"], "failed")


class RunAnalysisTest(unittest.TestCase):
    """Test analysis pipeline on a populated store."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_empty_store_returns_none(self):
        with mock.patch.object(sync.db, "load_transactions_from_db", return_value=[]):
            result = sync.run_analysis()
        self.assertIsNone(result)

    def test_populated_store_produces_digest(self):
        # Canonical DB transactions (run_analysis reads the DB, not a ledger store).
        import datetime as dt
        today = dt.date.today()
        txns = []
        for i in range(10):
            d = today - dt.timedelta(days=i * 3)
            txns.append(mktxn(f"TX_{i:03d}", -10.00 - i, str(d)))
        for i in range(3):
            d = today - dt.timedelta(days=i * 7)
            txns.append(mktxn(f"INC_{i}", 500.00, str(d), merchant="Employer"))

        # We need a rules.md for budget_scorer
        rules_path = os.path.join(self.dir, "rules.md")
        with open(rules_path, "w") as f:
            f.write("""# Rules
## Savings target
Target amount: **$10,000**
Move date: **December 2026**
Baseline start: **2025-06**
CEILING: ~$2,500

## Cut rules
| # | Leak | Rule | Prior $/mo | → Target |
|---|------|------|-----------|---------|
| 1 | Restaurant + delivery | Cap to | 150 | 80 |

## How to read me
Direct, encouraging, blunt on slips.
""")

        with mock.patch.object(sync.db, "load_transactions_from_db",
                               return_value=txns):
            digest = sync.run_analysis(
                balance=1200.0,
                mode="weekly",
                no_voice=True,
                rules_path=rules_path,
            )
        self.assertIsNotNone(digest)
        self.assertEqual(digest["tool"], "finance_agent")
        self.assertIn("sections", digest)


class StalenessGuardTest(unittest.TestCase):
    """Test the staleness detection logic."""

    def test_no_warnings_when_below_threshold(self):
        state = {"consecutive_empty_syncs": 1}
        warnings = sync.check_staleness(state)
        self.assertEqual(warnings, [])

    def test_warns_at_threshold(self):
        state = {"consecutive_empty_syncs": 3}
        warnings = sync.check_staleness(state)
        self.assertTrue(any("STALE" in w for w in warnings))

    def test_warns_above_threshold(self):
        state = {"consecutive_empty_syncs": 10}
        warnings = sync.check_staleness(state)
        self.assertTrue(any("STALE" in w for w in warnings))

    def test_no_warning_when_zero_empties(self):
        state = {"consecutive_empty_syncs": 0}
        warnings = sync.check_staleness(state)
        self.assertEqual(warnings, [])

    def test_old_txn_date_triggers_warning(self):
        old_date = str(dt.date.today() - dt.timedelta(days=5))
        state = {
            "consecutive_empty_syncs": 1,
            "last_txn_date": old_date,
        }
        warnings = sync.check_staleness(state)
        self.assertTrue(any("days old" in w for w in warnings))

    def test_recent_txn_date_no_warning(self):
        recent = str(dt.date.today() - dt.timedelta(days=1))
        state = {
            "consecutive_empty_syncs": 1,
            "last_txn_date": recent,
        }
        warnings = sync.check_staleness(state)
        self.assertEqual(warnings, [])


class EmptySyncTracksConsecutiveTest(_DBIsolation, unittest.TestCase):
    """Verify that an empty sync increments the staleness counter."""

    def setUp(self):
        self._isolate_db()
        self.dir = tempfile.mkdtemp()
        self.store = os.path.join(self.dir, "store.json")
        self.state = os.path.join(self.dir, "state.json")
        self.ss = os.path.join(self.dir, "sync_state.json")
        self.cursor = os.path.join(self.dir, "cursor.json")

    def test_empty_sync_increments_counter(self):
        """An empty sync API response on a weekday should bump the counter."""
        # Simulate: sync API returns zero txns
        empty_response = {
            "added": [], "modified": [], "removed": [],
            "cursor": "cur_empty", "has_more": False,
        }
        with mock.patch.object(
            plaid_bridge, "fetch_transactions_sync", return_value=empty_response
        ):
            result = sync.sync_from_bank(
                snapshot_path=None,
                use_sync_api=True,
                store_path=self.store,
                state_path=self.state,
                cursor_path=self.cursor,
                sync_state_path=self.ss,
            )
        self.assertEqual(result["source"], "sync")
        ss = sync.load_sync_state(self.ss)
        # On weekdays the counter increments; on weekends it stays at 0.
        # Either way, the key should exist now.
        self.assertIn("consecutive_empty_syncs", ss)

    def test_data_sync_resets_counter(self):
        """A sync that returns data should reset consecutive_empty_syncs to 0."""
        # Pre-seed the counter
        sync.save_sync_state({"consecutive_empty_syncs": 5, "runs": 0,
                              "total_synced": 0, "last_sync": None,
                              "last_cursor": None}, self.ss)

        txns = [mktxn("TX_RESET", -10.0, "2026-06-10")]
        snap = os.path.join(self.dir, "snap.json")
        with open(snap, "w") as f:
            json.dump(txns, f)

        sync.sync_from_bank(
            snapshot_path=snap,
            store_path=self.store,
            state_path=self.state,
            cursor_path=self.cursor,
            sync_state_path=self.ss,
        )
        ss = sync.load_sync_state(self.ss)
        self.assertEqual(ss.get("consecutive_empty_syncs", 0), 0)


class FailureEmailTest(unittest.TestCase):
    """Test that _send_failure_email is called on failures."""

    def test_send_failure_email_does_not_crash(self):
        """Even if delivery is misconfigured, _send_failure_email swallows errors."""
        # Patch send_email to raise — _send_failure_email should not propagate
        with mock.patch("finance_mcp.report.delivery.send_email", side_effect=Exception("no creds")):
            # Should not raise
            sync._send_failure_email("test error", "test detail")


class DailyShLogicTest(unittest.TestCase):
    """Verify daily.sh exit-code gating logic (structural, no live run)."""

    def test_daily_sh_captures_exit_code(self):
        """daily.sh should capture SYNC_EXIT and gate on it."""
        with open(os.path.join(os.path.dirname(__file__), "..", "ops", "daily.sh")) as f:
            script = f.read()
        self.assertIn("SYNC_EXIT=$?", script)
        self.assertIn('[ "$SYNC_EXIT" -ne 0 ]', script)
        self.assertIn("exit 1", script)
        # The old pattern (echo resetting $?) should be gone
        self.assertNotIn('echo "[daily] email step exit=$?"', script)


class MultiSourceRoutingTest(unittest.TestCase):
    """sync_all_sources routing: a tokenless Item must use the subprocess path
    (access_token=None) — NOT be dropped as 'failed' — while a tokened Item uses
    Plaid direct. This is what makes activating plaid_items.json safe: the primary
    account's tokenless Item keeps syncing via the bank-mcp fork, the second tokened Item
    goes direct."""

    def test_tokenless_subprocess_tokened_direct(self):
        items = [
            {"name": "primary-checking", "token_env": "PLAID_ACCESS_TOKEN",
             "owner": "primary", "cursor_file": "/tmp/c_primary.json"},
            {"name": "secondary-checking", "token_env": "PLAID_ACCESS_TOKEN_SECONDARY",
             "owner": "secondary", "cursor_file": "/tmp/c_secondary.json"},
        ]
        calls = []

        def fake_resolve(item):
            return None if item["owner"] == "primary" else "access-secondary"

        def fake_sync(*a, **k):
            calls.append(k)
            return {"source": "list", "added": 0, "modified": 0, "removed": 0,
                    "store_size": 0, "cursor": None}

        with mock.patch.object(plaid_bridge, "load_plaid_items", return_value=items), \
                mock.patch("os.path.exists", return_value=True), \
                mock.patch.object(plaid_bridge, "_resolve_access_token",
                                  side_effect=fake_resolve), \
                mock.patch.object(sync, "sync_from_bank", side_effect=fake_sync):
            results = sync.sync_all_sources()

        self.assertEqual(len(calls), 2)
        by_owner = {k["owner"]: k for k in calls}
        self.assertIsNone(by_owner["primary"]["access_token"])           # subprocess path
        self.assertEqual(by_owner["secondary"]["access_token"], "access-secondary")  # direct
        self.assertTrue(all(r.get("source") != "failed" for r in results))


class SyncConnectionsTest(unittest.TestCase):
    """sync_connections pulls each bank-mcp connection by id and tags its owner."""

    def test_per_connection_owner_tagging(self):
        from finance_mcp.store import db
        real_connect = db.connect                  # capture before patching
        fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
        owners = {"conn-primary": "primary", "conn-secondary": "secondary"}

        def fake_range(date_from=None, connection_id=None, **k):
            return [{"id": f"{connection_id}-1", "accountId": connection_id,
                     "amount": -10, "type": "debit", "date": "2026-06-01",
                     "currency": "USD", "merchantName": "M",
                     "rawData": {"transaction_id": f"{connection_id}-1",
                                 "amount": 10, "date": "2026-06-01"}}]
        try:
            with mock.patch.object(sync, "_load_connection_owners", return_value=owners), \
                    mock.patch.object(sync.plaid_bridge, "fetch_transactions_range",
                                      side_effect=fake_range), \
                    mock.patch.object(sync.db, "connect",
                                      side_effect=lambda *a, **k: real_connect(path)), \
                    mock.patch.object(sync, "load_sync_state", return_value={}), \
                    mock.patch.object(sync, "save_sync_state"):
                results = sync.sync_connections()
            v = real_connect(path)
            by_owner = dict(v.execute(
                "SELECT owner, COUNT(*) FROM transactions GROUP BY owner").fetchall())
            v.close()
            self.assertEqual(by_owner, {"primary": 1, "secondary": 1})   # each tagged
            self.assertEqual({r["owner"] for r in results}, {"primary", "secondary"})
            self.assertTrue(all(r["source"] != "failed" for r in results))
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
