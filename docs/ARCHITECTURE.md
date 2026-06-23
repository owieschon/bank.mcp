# Architecture

A layered pipeline with one canonical accessor at the base and one orchestrator on
top. Data flows in one direction; the dependency graph is acyclic.

```
   bank (Plaid / bank-mcp)
         │  pull
         ▼
   ┌──────────┐   upsert    ┌───────────────────────────┐
   │ ingest/  │ ──────────► │ store/  SQLite (finance.db)│
   │ sync     │             │  + canonical field layer   │
   └──────────┘             └───────────────────────────┘
                                   │ load (engine-shaped dicts)
                                   ▼
                            ┌──────────────┐
                            │  engines/    │  deterministic cores, each returns
                            │  (7 cores)   │  a compact summary dict — no raw rows
                            └──────────────┘
                                   │ summaries
                                   ▼
                            ┌──────────────┐
                            │ finance_agent│  orchestrator: reconcile, run each
                            │  (build_digest)│ core, assemble ONE digest
                            └──────────────┘
                              │            │
                   summary    │            │  summary dict (never raw rows)
                  ┌───────────▼──┐      ┌──▼─────────────────┐
                  │ report/      │      │ LLM (Haiku)        │
                  │ digest + site│◄─────│ narrate / match /  │
                  └──────────────┘      │ extract — edges only│
                                        └────────────────────┘
```

## Layers

**`ingest/` — getting data in.**
`safehttp.fetch()` is the single outbound-HTTP chokepoint (enforces HTTPS, bounds a
timeout, blocks non-HTTPS redirects). `plaid_bridge` is the bank transport (a
bank-mcp subprocess fork, direct Plaid, or a file snapshot, tried in order, with a
`BankMCPError` boundary). `plaid_link` mints access tokens. `sync` orchestrates a
pull → DB upsert → analysis run and persists cursor/sync state.

**`store/` — the source of record + the canonical field layer.**
`db.py` is a single-file SQLite store. The write path is an **idempotent upsert keyed
on transaction id**, where a posted charge supersedes its stale `pending` row, so
re-ingesting a batch never duplicates. The read adapter reconstructs the exact
engine-shaped dict from a lossless `raw` JSON column.
`subscription_creep.py` is the **canonical field accessor** — every engine reads
amounts, signs, dates, merchant identity, and cadence through it (`is_outflow`,
`amount_magnitude`, `parse_date`, `merchant_key`, `classify_cadence`, …) rather than
re-deriving them. `obligation_registry` models forward commitments; `merchant_categorizer`
maps transactions to human categories.
`queries.sql` + `analytics.py` are the **SQL reporting read-models** — monthly cash
flow (running total + month-over-month delta), category breakdown (share of spend),
and top merchants — computed as CTE/window-function queries over the typed columns,
because set-based reporting is what SQL is for (the algorithmic forecasting stays in
Python). `tests/test_analytics.py` cross-checks each query against a Python recompute.

**`engines/` — the deterministic cores.**
`cashflow_forecaster` (projection / overdraft), `budget_scorer` (savings-goal pace),
`fee_fraud_scan` (fees + duplicate charges), `recurring` (recurring streams),
`receipt_scanner` (reconciliation), `dispute_agent` (dispute tracking),
`merchant_categorizer`. Each returns a compact summary dict (~1K tokens), never raw
rows. `llm_matcher` is the only engine that calls the model (merchant matching /
receipt extraction).

**`report/` — rendering and serving.**
`delivery` holds the shared `money()` / `fmt_date()` / `send_email()` / `narrate()`
helpers. `digest_templates` renders HTML. `build_site` assembles a self-contained
static `./site` (landing page + report + assets) for an auth-gated Vercel deploy.

**`finance_agent.py` — the orchestrator.**
`build_digest()` runs reconciliation first, then each engine core (imported, never
reimplemented), collates one combined digest, and runs at most one LLM "narrate"
pass over the compact summary.

## The SQLite schema (shape)

`transactions` carries **typed columns for querying** — `id` (PK), `account_id`,
`owner`, `date`, `amount`, `direction`, `currency`, `merchant_name`, `category_raw`,
`category_human`, `pending` — plus a **`raw` TEXT column holding the original
transaction dict verbatim**. Indexed on `(owner, date)`.

The typed columns are a query/filter index; the engines' actual data source is the
`raw` blob, reconstructed losslessly by `load_transactions_from_db()`. That keeps the
engine output byte-identical whether it reads from JSON or the DB. `owner` /
`currency` are first-class, so a second account holder or a second currency is just
more rows — no schema migration.

## The LLM boundary (the load-bearing invariant)

Only three things ever reach a prompt: a **compact summary dict** (for narration), a
**merchant-name string** (for matching), or **receipt email text** (for extraction).
Amounts, dates, ids, and account numbers paired with identity never do. The math is
deterministic Python and unit-tested; a `--no-voice` run is correct at $0. This is
enforced in spirit by the layering and checked directly by a test that deep-walks the
assembled digest and fails if a raw-row shape leaks into it
(`tests/test_finance_agent.py::TestNoRawRows`).

See [DECISIONS.md](DECISIONS.md) for why the storage/analysis split is shaped this way.
