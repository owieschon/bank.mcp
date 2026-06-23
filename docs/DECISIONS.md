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

**The SQL / Python boundary is already where it should be, so no
language-fit refactor was made:**

- **SQL (SQLite) owns storage and retrieval** — `store/db.py` defines the schema,
  does an idempotent `upsert` keyed on transaction id (with pending→posted
  supersession), indexes `(owner, date)`, and filters by `owner`/`since` in the
  query. That is exactly what a relational store is good at, and it is done in
  SQL.
- **Python owns the analysis** — and most of it is *algorithmic, not set-based*:
  median inter-charge-gap cadence classification, price-step detection,
  cash-flow projection / roll-forward, recurring-stream detection, dispute
  tracking. These are iterative, stateful computations; expressing them as SQL
  `GROUP BY` would be the wrong tool and harder to read.

**The set-based spots were considered for SQL and deliberately left in Python.**
A handful of operations (category breakdowns, monthly in/out sums, date-range
filters) *are* set-based and *could* be `GROUP BY` queries over the typed
columns. They were left in Python because: (1) the dataset is tiny and already
in memory, so SQL yields no performance benefit; (2) this is the deterministic,
unit-tested financial core — pushing the math into SQL would split the tested
surface across two languages and invite subtle rounding/string-date differences;
(3) the engines intentionally read the lossless `raw` JSON column (the typed
columns are a query/filter index, not the engines' data source). Adding SQL here
would invert a deliberate design for no functional gain. Per "preserve what
works" and "don't add SQL to look sophisticated," it stays Python.

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
