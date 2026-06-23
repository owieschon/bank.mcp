"""analytics.py — run the SQL analytical read-models in queries.sql.

The descriptive reporting rollups (monthly cash flow, category mix, top merchants)
are set-based aggregations, so they are expressed as SQL over the canonical store
rather than recomputed in Python. This module just loads the named queries from
queries.sql and runs them; the SQL itself is the interesting part — read it there.

    python -m finance_mcp.store.analytics [--db finance.db] [--owner NAME]

With no --db it seeds an in-memory database from the bundled synthetic demo data,
so it runs with no real data.
"""
import argparse
import os
import re
import sqlite3

from finance_mcp.store import db

_QUERIES_PATH = os.path.join(os.path.dirname(__file__), "queries.sql")


def _load_queries(path=_QUERIES_PATH):
    """Parse queries.sql into {name: sql}, split on `-- name: <name>` markers."""
    text = open(path, encoding="utf-8").read()
    blocks = {}
    name = None
    buf = []
    for line in text.splitlines():
        m = re.match(r"--\s*name:\s*(\w+)\s*$", line)
        if m:
            if name:
                blocks[name] = "\n".join(buf).strip()
            name, buf = m.group(1), []
        elif name:
            buf.append(line)
    if name:
        blocks[name] = "\n".join(buf).strip()
    return blocks


_QUERIES = _load_queries()


def _rows(conn, sql, params):
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql, params)]


def monthly_cashflow(conn, owner=None):
    """Per-month income/spend/net with a running net and month-over-month change."""
    return _rows(conn, _QUERIES["monthly_cashflow"], {"owner": owner})


def category_breakdown(conn, owner=None):
    """Spend per category with each category's share of total spend."""
    return _rows(conn, _QUERIES["category_breakdown"], {"owner": owner})


def top_merchants(conn, owner=None, limit=10):
    """The top `limit` merchants by total spend, ranked."""
    return _rows(conn, _QUERIES["top_merchants"], {"owner": owner, "limit": limit})


def _print_table(title, rows):
    print(f"\n## {title}")
    if not rows:
        print("(no rows)")
        return
    cols = list(rows[0].keys())
    widths = {c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    print("  ".join("-" * widths[c] for c in cols))
    for r in rows:
        print("  ".join(str(r[c]).ljust(widths[c]) for c in cols))


def _seed_demo_conn():
    from finance_mcp import demo
    conn = db.connect(":memory:")
    db.init_schema(conn)
    db.upsert_transactions(conn, demo.generate())
    return conn


def main(argv=None):
    ap = argparse.ArgumentParser(description="SQL analytics over the transactions store")
    ap.add_argument("--db", default=None, help="SQLite file (default: in-memory synthetic demo data)")
    ap.add_argument("--owner", default=None, help="filter to one owner (default: all)")
    ap.add_argument("--top", type=int, default=10, help="how many merchants to list")
    args = ap.parse_args(argv)

    conn = db.connect(args.db) if args.db else _seed_demo_conn()
    try:
        _print_table("Monthly cash flow", monthly_cashflow(conn, args.owner))
        _print_table("Category breakdown", category_breakdown(conn, args.owner))
        _print_table(f"Top {args.top} merchants by spend", top_merchants(conn, args.owner, args.top))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
