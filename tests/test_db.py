"""Round-trip tests for db.py (Phase A write path). Synthetic data only."""

import unittest
from finance_mcp.store import db
from finance_mcp.store import subscription_creep as sc


def _txn(tid, *, amount, date, type_="debit", owner="primary", currency="USD",
         pending=False, pending_of=None, merchant="Test Merchant"):
    """A synthetic transaction in the real plaid_bridge shape."""
    return {
        "id": tid,
        "accountId": f"acct_{owner}",
        "amount": -amount if type_ == "debit" else amount,
        "type": type_,
        "date": date,
        "currency": currency,
        "merchantName": merchant,
        "description": merchant,
        "reference": tid,
        "category": "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE",
        "rawData": {
            "transaction_id": tid,
            "account_id": f"acct_{owner}",
            "amount": amount,
            "date": date,
            "iso_currency_code": currency,
            "account_owner": owner,
            "merchant_name": merchant,
            "pending": pending,
            "pending_transaction_id": pending_of,
        },
    }


class TestDB(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        db.init_schema(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_schema_tables_exist(self):
        names = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"accounts", "transactions", "merchant_overrides",
                         "fx_rates"}.issubset(names))

    def test_insert_and_count(self):
        n = db.upsert_transactions(self.conn, [
            _txn("a", amount=10, date="2026-06-01"),
            _txn("b", amount=20, date="2026-06-02"),
        ])
        self.assertEqual(n["total"], 2)
        self.assertEqual(db.count(self.conn), 2)

    def test_idempotent_reingest(self):
        batch = [_txn("a", amount=10, date="2026-06-01"),
                 _txn("b", amount=20, date="2026-06-02")]
        db.upsert_transactions(self.conn, batch)
        db.upsert_transactions(self.conn, batch)   # same batch again
        self.assertEqual(db.count(self.conn), 2)    # no duplicates

    def test_update_in_place(self):
        db.upsert_transactions(self.conn, [_txn("a", amount=10, date="2026-06-01")])
        db.upsert_transactions(self.conn, [_txn("a", amount=99, date="2026-06-01")])
        self.assertEqual(db.count(self.conn), 1)
        row = self.conn.execute("SELECT amount FROM transactions WHERE id='a'").fetchone()
        self.assertEqual(row["amount"], 99.0)

    def test_pending_then_posted_supersedes(self):
        # A pending charge, then its posted successor referencing the pending id.
        db.upsert_transactions(self.conn, [
            _txn("pend1", amount=10, date="2026-06-01", pending=True)])
        db.upsert_transactions(self.conn, [
            _txn("post1", amount=10, date="2026-06-02", pending_of="pend1")])
        ids = {r[0] for r in self.conn.execute("SELECT id FROM transactions")}
        self.assertEqual(ids, {"post1"})            # stale pending row removed

    def test_round_trip_dict_shape_and_accessors(self):
        original = _txn("a", amount=42.50, date="2026-06-03", merchant="China House")
        db.upsert_transactions(self.conn, [original])
        got = db.load_transactions_from_db(self.conn)
        self.assertEqual(len(got), 1)
        # Reconstructed dict is identical → engines need zero changes.
        self.assertEqual(got[0], original)
        # And the canonical accessors produce the right values on it.
        self.assertTrue(sc.is_outflow(got[0]))
        self.assertEqual(sc.amount_magnitude(got[0]), 42.50)
        self.assertEqual(sc.parse_date(got[0]).isoformat(), "2026-06-03")

    def test_owner_filter(self):
        db.upsert_transactions(self.conn, [
            _txn("o1", amount=10, date="2026-06-01", owner="primary"),
            _txn("w1", amount=10, date="2026-06-01", owner="secondary"),
        ])
        secondary = db.load_transactions_from_db(self.conn, owner="secondary")
        self.assertEqual([t["id"] for t in secondary], ["w1"])
        self.assertEqual(db.count(self.conn), 2)    # both stored; filter is read-side

    def test_since_filter(self):
        db.upsert_transactions(self.conn, [
            _txn("old", amount=10, date="2026-05-01"),
            _txn("new", amount=10, date="2026-06-15"),
        ])
        recent = db.load_transactions_from_db(self.conn, since="2026-06-01")
        self.assertEqual([t["id"] for t in recent], ["new"])


    def test_integrity_report(self):
        db.upsert_transactions(self.conn, [
            _txn("a", amount=10, date="2026-06-01"),
            _txn("b", amount=10, date="2026-06-03"),   # 2-day gap
            _txn("c", amount=10, date="2026-06-12"),   # 9-day gap (the max)
        ])
        rep = db.integrity_report(self.conn)
        self.assertEqual(rep["count"], 3)
        self.assertEqual(rep["first"], "2026-06-01")
        self.assertEqual(rep["last"], "2026-06-12")
        self.assertEqual(rep["max_gap_days"], 9)
        self.assertEqual(rep["max_gap_between"], ["2026-06-03", "2026-06-12"])

    def test_export_snapshot_round_trip(self):
        import json
        import tempfile
        import os as _os
        batch = [_txn("a", amount=10, date="2026-06-01"),
                 _txn("b", amount=20, date="2026-06-02")]
        db.upsert_transactions(self.conn, batch)
        fd, path = tempfile.mkstemp(suffix=".json")
        _os.close(fd)
        try:
            n = db.export_snapshot(self.conn, path)
            self.assertEqual(n, 2)
            with open(path) as f:
                exported = json.load(f)
            self.assertEqual({t["id"] for t in exported}, {"a", "b"})
            # exported snapshot is re-ingestable with no change (idempotent loop)
            self.assertEqual(db.upsert_transactions(self.conn, exported)["total"], 2)
            self.assertEqual(db.count(self.conn), 2)
        finally:
            _os.unlink(path)


if __name__ == "__main__":
    unittest.main()
