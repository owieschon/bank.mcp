"""db.py — the canonical local datastore for finance.mcp.

SQLite, standard library only, single file. It is the source of record for the
read AND write paths: `sync` upserts here and the report/digest read back through
the adapter below.

No-rewrite guarantee: each transaction's full original dict is stored verbatim in
the `raw` column, so `load_transactions_from_db()` reconstructs the exact dict the
engines already consume — `subscription_creep`'s accessors work on it unchanged.
The typed columns exist only for querying/filtering (owner, date, currency, ...).

Multi-source / multi-currency / multi-owner are first-class from day one (the
`accounts.owner` + native `currency` columns), so a second account holder is just
rows with owner='secondary'; engines filter by owner. No schema migration later.

CLI:  python3 db.py transactions.json [--db finance.db]   # init + (idempotent) backfill
"""

import json
import os
import sqlite3
import sys
import datetime as dt

from finance_mcp.store import subscription_creep as sc   # canonical accessors — store what engines compute

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "finance.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  account_id   TEXT PRIMARY KEY,
  source       TEXT NOT NULL,
  institution  TEXT,
  name         TEXT,
  currency     TEXT NOT NULL,
  owner        TEXT NOT NULL DEFAULT 'primary',
  active       INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS transactions (
  id            TEXT PRIMARY KEY,
  account_id    TEXT NOT NULL,
  owner         TEXT NOT NULL,
  date          TEXT NOT NULL,
  amount        REAL NOT NULL,
  direction     TEXT NOT NULL,
  currency      TEXT NOT NULL,
  merchant_name TEXT,
  description   TEXT,
  reference     TEXT,
  category_raw  TEXT,
  category_human TEXT,
  pending       INTEGER NOT NULL DEFAULT 0,
  raw           TEXT,
  ingested_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_txn_owner_date ON transactions(owner, date);
CREATE INDEX IF NOT EXISTS ix_txn_account ON transactions(account_id);
CREATE TABLE IF NOT EXISTS merchant_overrides (
  match_key      TEXT PRIMARY KEY,
  category_human TEXT NOT NULL,
  note           TEXT,
  updated_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fx_rates (
  day TEXT NOT NULL, base TEXT NOT NULL, quote TEXT NOT NULL,
  rate REAL NOT NULL, PRIMARY KEY (day, base, quote)
);
CREATE TABLE IF NOT EXISTS envelopes (
  name TEXT NOT NULL, owner TEXT NOT NULL, period TEXT NOT NULL,
  allocated REAL NOT NULL, currency TEXT NOT NULL,
  PRIMARY KEY (name, owner, period)
);
"""


def connect(path=DEFAULT_DB):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn):
    conn.executescript(_SCHEMA)
    conn.commit()


def _owner_of(t):
    # Explicit stamp from multi-Item config takes priority over rawData.
    stamped = t.get("_owner")
    if stamped:
        return stamped
    return (sc.raw(t).get("account_owner") or "primary")


def _source_of(t):
    """Transaction source: explicit stamp, or 'plaid' by default."""
    return t.get("_source") or "plaid"


def _currency_of(t):
    return (t.get("currency") or t.get("_currency")
            or sc.raw(t).get("iso_currency_code") or "USD")


def _accounts_in(txns):
    """Distinct account rows derived from a transaction batch."""
    seen = {}
    for t in txns:
        aid = t.get("accountId") or sc.raw(t).get("account_id")
        if not aid or aid in seen:
            continue
        seen[aid] = (aid, _source_of(t), None, None, _currency_of(t), _owner_of(t), 1)
    return list(seen.values())


def upsert_transactions(conn, txns, ingested_at=None):
    """Idempotent upsert (keyed on transaction id). Re-ingesting the same batch
    updates in place — never duplicates. Pending charges that have since posted
    (a new row carrying `pending_transaction_id`) supersede the stale pending row.

    Returns a diff dict {added, modified, removed, total} (added = ids not already
    stored; modified = stored but content changed; removed = superseded pending
    rows). This is what the sync result + staleness guard consume. Account rows are
    upserted first so the data is self-contained.
    """
    ts = ingested_at or dt.datetime.now().isoformat(timespec="seconds")

    conn.executemany(
        """INSERT INTO accounts (account_id, source, institution, name, currency, owner, active)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(account_id) DO UPDATE SET
             currency=excluded.currency, owner=excluded.owner, active=excluded.active""",
        _accounts_in(txns),
    )

    rows, supersede = [], []
    for t in txns:
        tid = t.get("id") or sc.raw(t).get("transaction_id")
        if not tid:
            continue
        d = sc.parse_date(t)
        rows.append((
            tid,
            t.get("accountId") or sc.raw(t).get("account_id"),
            _owner_of(t),
            d.isoformat() if d else "",
            sc.amount_magnitude(t) or 0.0,
            "debit" if sc.is_outflow(t) else "credit",
            _currency_of(t),
            t.get("merchantName") or sc.raw(t).get("merchant_name"),
            t.get("description"),
            t.get("reference"),
            t.get("category"),
            None,                       # category_human: reserved (populated by the override path)
            1 if sc.raw(t).get("pending") else 0,
            json.dumps(t),              # lossless: read adapter reconstructs the exact dict
            ts,
        ))
        pid = sc.raw(t).get("pending_transaction_id")
        if pid:
            supersede.append((pid,))

    # Diff vs what's already stored (for the sync result + staleness guard).
    ids = [r[0] for r in rows]
    existing = {}
    for i in range(0, len(ids), 800):              # stay under SQLite's variable cap
        chunk = ids[i:i + 800]
        q = "SELECT id, raw FROM transactions WHERE id IN (%s)" % ",".join("?" * len(chunk))
        existing.update({row["id"]: row["raw"] for row in conn.execute(q, chunk)})
    added = sum(1 for r in rows if r[0] not in existing)
    modified = sum(1 for r in rows if r[0] in existing and existing[r[0]] != r[13])

    conn.executemany(
        """INSERT INTO transactions
             (id, account_id, owner, date, amount, direction, currency, merchant_name,
              description, reference, category_raw, category_human, pending, raw, ingested_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             account_id=excluded.account_id, owner=excluded.owner, date=excluded.date,
             amount=excluded.amount, direction=excluded.direction, currency=excluded.currency,
             merchant_name=excluded.merchant_name, description=excluded.description,
             reference=excluded.reference, category_raw=excluded.category_raw,
             pending=excluded.pending, raw=excluded.raw, ingested_at=excluded.ingested_at""",
        rows,
    )
    removed = 0
    if supersede:
        # Drop now-posted pending rows, but never delete a row we just wrote.
        current = {r[0] for r in rows}
        kill = [(pid,) for (pid,) in supersede if pid not in current]
        if kill:
            conn.executemany("DELETE FROM transactions WHERE id = ?", kill)
            removed = len(kill)
    conn.commit()
    return {"added": added, "modified": modified, "removed": removed, "total": len(rows)}


def load_transactions_from_db(conn, owner=None, since=None):
    """Read adapter: return transaction dicts in the EXACT shape the engines
    consume (reconstructed from the stored `raw` blob). `owner` / `since` (ISO date
    str) filter at the SQL layer without touching engine logic.
    """
    q = "SELECT raw FROM transactions WHERE 1=1"
    args = []
    if owner is not None:
        q += " AND owner = ?"; args.append(owner)
    if since is not None:
        q += " AND date >= ?"; args.append(since)
    q += " ORDER BY date"
    return [json.loads(r["raw"]) for r in conn.execute(q, args)]


def count(conn):
    return conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]


def integrity_report(conn, owner=None):
    """Verified-complete check: row count, date range, and the
    largest gap between consecutive transaction days. A large gap flags a possibly
    missed sync window rather than asserting silently."""
    q = "SELECT DISTINCT date FROM transactions"
    args = []
    if owner is not None:
        q += " WHERE owner = ?"; args.append(owner)
    days = [r[0] for r in conn.execute(q + " ORDER BY date", args) if r[0]]
    if owner is None:
        total = count(conn)
    else:
        total = conn.execute("SELECT COUNT(*) FROM transactions WHERE owner=?",
                             (owner,)).fetchone()[0]
    max_gap, gap_between = 0, None
    for a, b in zip(days, days[1:]):
        try:
            g = (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days
        except ValueError:
            continue
        if g > max_gap:
            max_gap, gap_between = g, [a, b]
    return {"count": total, "first": days[0] if days else None,
            "last": days[-1] if days else None, "active_days": len(days),
            "max_gap_days": max_gap, "max_gap_between": gap_between}


def export_snapshot(conn, path, owner=None):
    """Write the DB back out to a JSON snapshot (atomic). With the DB as source of
    record, `transactions.json` becomes a derived backup/export, not the source."""
    txns = load_transactions_from_db(conn, owner=owner)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(txns, f, indent=2)
    os.replace(tmp, path)
    return len(txns)


if __name__ == "__main__":
    argv = sys.argv[1:]
    db_path = DEFAULT_DB
    if "--db" in argv:
        i = argv.index("--db"); db_path = argv[i + 1]; del argv[i:i + 2]
    conn = connect(db_path)
    init_schema(conn)
    if argv and argv[0] == "--integrity":
        print(json.dumps(integrity_report(conn), indent=2))
    elif argv and argv[0] == "--export":
        out = argv[1] if len(argv) > 1 else "transactions.export.json"
        print(f"[db] exported {export_snapshot(conn, out)} txns -> {out}")
    elif argv:
        txns = sc.load_transactions(argv[0])
        n = upsert_transactions(conn, txns)["total"]
        print(f"[db] upserted {n} txns into {db_path} · total rows now {count(conn)}")
    else:
        raise SystemExit(
            "Usage: python3 db.py <transactions.json> | --integrity | --export <path> [--db finance.db]")
