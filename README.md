# bank.mcp

I wanted to read my own money — track how fast I was saving toward a cross-border move, catch the fee or duplicate charge I'd otherwise eat — without handing the arithmetic to a language model. A wrong "you're on pace" or a forecast that says you clear your buffer when you don't has a real price. So every binding number here is computed in plain, deterministic, integer-cents Python, and unit-tested. The model never touches it.

What the LLM is allowed to do is narrow: narrate a finished summary, match two merchant-name strings, pull the amount out of a receipt email. That's the whole surface. **Raw transaction rows never enter a prompt** — no amounts-with-identity, no account numbers, no transaction ids. If there's no API key, or the model returns garbage, the deterministic result stands unchanged. `--no-voice` runs the entire pipeline at **zero tokens and no network**, and is fully correct. The model is an optional voice on top of an engine that already has the answer.

It's standard-library Python — `dependencies = []`. No ORM, no framework, no SDK; a single-file SQLite store and seven engines.

## What it does

Turns a SQLite store of bank transactions into **one digest**: a cash-flow / overdraft forecast, savings-goal pace against a target and a move date, a spending breakdown, a fee + duplicate-charge scan, recurring-stream detection, and receipt reconciliation. One orchestrator (`finance_agent.build_digest`) reconciles receipts first, runs each engine, and collates a single ~1K-token summary — which is the *only* thing narration ever sees.

```
bank-mcp demo        # builds + prints the digest from bundled synthetic data — no bank, no key, no network
```

```
# bank.mcp — UNIFIED MONTHLY DIGEST
## What matters
- Clear: balance stays at or above the $100.00 buffer for the full 35-day horizon (min $1,087.44 on May 6, 2026).
- Fee/fraud: $49.99 recoverable this 30d.
## savings pace
- Pace $2,966.67/mo vs $1,257.14/mo needed → projected $21,966.69 by Dec 1, 2026 → AHEAD (12.0% to $10,000.00)
## Fee + fraud scan (30d)
- duplicates $49.99 recoverable (1)  · dup: Online Store $49.99 2026-04-24 & 2026-04-24
```

The demo data is deterministic (fixed anchor date, seeded RNG) and doubles as the committed test fixture — it plants a duplicate charge, two bank fees, a recurring drift, and a self-transfer so each detector has something real to catch.

## The line I care about

- **`money.py` is the single integer-cents authority.** `store/db.py` writes `amount` as integer cents, so the SQL read-models sum exact integers and aggregation never drifts. Rounding is half-up in one place.
- **The math is checked twice.** The SQL reporting rollups in `store/queries.sql` (monthly cash flow with a running total + month-over-month delta, category share-of-spend, ranked top merchants — CTEs and `SUM() OVER` / `LAG()` / `RANK()`) are cross-checked against an independent Python recompute in `tests/test_analytics.py`. Two paths to the same number.
- **The LLM boundary is a tested invariant, not a comment.** `tests/test_finance_agent.py::TestNoRawRows` walks the assembled digest and fails the build if a raw-transaction shape ever appears in the thing narration receives. `llm_matcher` sends only merchant-name strings; absent a key it returns empty and the deterministic reconciliation pass stands.
- **Ingestion is idempotent.** `store/db.py`'s upsert is keyed on transaction id, and a posted charge supersedes its stale `pending` row — re-ingesting a batch never double-counts. `owner` and `currency` are columns, so a second account holder or currency is just more rows, no migration.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"        # dev tools only; the package itself needs nothing

bank-mcp demo                  # full digest from synthetic data
bank-mcp analytics             # the SQL reporting rollups
pytest -q                      # 339 tests
```

**339 tests pass at 75% coverage** (CI gates ≥70% on the testable core across Python 3.10–3.13; `ruff` and `mypy` clean). `bank-mcp-server` exposes the engines to MCP clients (Claude Desktop, etc.) — a JSON-RPC 2.0 server over stdio with four tools (`build_digest`, `monthly_cashflow`, `category_breakdown`, `top_merchants`), MCP protocol implemented directly so the project stays dependency-free.

> Public work-sample of a tool I run on my own accounts. **Everything in the tree is synthetic** — the demo generator produces fake merchants, amounts, and dates; there is no real financial data here. Secrets resolve from env → macOS Keychain, never the repo.

## Where to look first

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — the layered pipeline, the SQLite schema, and the LLM boundary, with file references.
- `src/bank_mcp/money.py` — the integer-cents rounding authority.
- `src/bank_mcp/store/queries.sql` — the SQL reporting read-models, written to read top to bottom.
- `src/bank_mcp/engines/` — the seven deterministic cores (forecast, budget, fee/fraud, recurring, receipt reconciliation, dispute, categorizer); `llm_matcher.py` is the only one that calls the model.
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — why SQLite over Postgres, and the set-based-reporting-in-SQL / algorithmic-forecasting-in-Python split.

Apache-2.0 — see [LICENSE](LICENSE).
