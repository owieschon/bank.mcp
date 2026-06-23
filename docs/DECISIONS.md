# Design decisions

Why the project is shaped the way it is, including alternatives considered and
rejected. (For *what changed* during portfolio preparation, see `../CHANGES.md`.)

## Language fit: Python vs SQL vs PostgreSQL

**SQLite is the right datastore here; PostgreSQL would be over-engineering.**
This is a single-user, local tool: one file, a few thousand transactions, no
concurrency, no network, no multi-tenant access. The standard-library `sqlite3`
module covers it completely and keeps the project at **zero runtime
dependencies** — a property worth protecting. PostgreSQL would add an external
service to run, a third-party driver to install, and operational overhead, in
exchange for nothing this workload needs.

**The SQL / Python split follows the shape of each problem:**

- **SQL owns storage, retrieval, and descriptive analytics.** `store/db.py` defines
  the schema, does an idempotent `upsert` keyed on transaction id (with pending→posted
  supersession), indexes `(owner, date)`, and filters by `owner`/`since` in the query.
  The **reporting read-models** — monthly cash flow (with a running total and a
  month-over-month delta), category breakdown (each category's share of spend), and
  top merchants by spend — live in `store/queries.sql` and are run by
  `store/analytics.py`. These are set-based aggregations over a relational store, so
  SQL (CTEs, `GROUP BY`, and window functions: `SUM() OVER`, `LAG()`, `RANK()`) is the
  idiomatic, clearest tool, and the queries are written to be read top-to-bottom.
- **Python owns the algorithmic analysis** — median inter-charge-gap cadence
  classification, price-step detection, cash-flow projection / roll-forward,
  recurring-stream detection, dispute tracking. These are iterative, stateful
  computations; expressing them as `GROUP BY` would be the wrong tool. They are also
  the deterministic, unit-tested financial core, so they stay in Python where the
  test surface is single-language.

So the boundary is principled: **set-based reporting → SQL; algorithmic forecasting →
Python.** The dataset is small enough that SQL is chosen for clarity and idiom, not
performance, and `test_analytics.py` cross-checks every query result against an
independent Python recomputation so the two never silently diverge. (Window functions
need SQLite ≥ 3.25, which every supported Python ships.)

## Deterministic math, LLM only at the edges

Financial figures are computed by plain, unit-tested Python. The LLM is used only
to (a) narrate a compact summary dict, (b) match merchant-name strings, and
(c) extract text from receipt emails. **Raw transaction rows never enter a
prompt.** A `--no-voice` run is fully correct at zero tokens and no network. This
is the core design premise and the reason the analysis is auditable.

## Package layout mirrors the data flow

`src/finance_mcp/` is grouped into `ingest/` → `store/` → `engines/` → `report/`,
the direction data actually moves, with `finance_agent.py` as the single
orchestrator on top. The dependency direction is acyclic. A flat module package
was considered; the grouped layout was chosen so the architecture is legible from
the directory tree rather than only from reading imports.

## The USD↔BRL display is an example feature, not the product

The static report includes an optional client-side currency toggle (USD↔BRL) with
a World-Bank-PPP orientation figure. It is kept as a self-contained demonstration
of client-side currency re-denomination. It was left intact rather than generalized
to arbitrary currency pairs (that would be unrequested new work) or removed (that
would subtract a working feature).

## Deferred, not done (flagged honestly)

- **A ~300-line email renderer** in `report/digest_templates.py`
  (`render_email_html` / `_build_email_portion`) has no live caller — the live
  report path is `render_report_html` / `render_weekly_html` /
  `render_monthly_html`. It is still exercised by tests and is interleaved with
  the live `select_hero` / `render_report_html` code, so it was left intact rather
  than risk the live renderer. Recommended as a deliberate follow-up removal.
- **Two small duplications** were left as-is to avoid changing behavior during a
  cleanup: transfer-detection and recurring-detection each exist in two places
  with slightly different thresholds. Unifying them would change outputs and
  belongs in its own change with its own tests.
- **`engines/llm_matcher._call_haiku`** still uses `urllib` directly instead of
  the `ingest/safehttp` wrapper that the rest of the suite routes through. Routing
  it through `safehttp` is a sensible SSRF-hardening follow-up; it was not changed
  here because it alters a network path and deserves its own focused change.
