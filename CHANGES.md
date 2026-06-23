# Portfolio preparation — changes from the private original

This repo is a public work-sample copy of a private personal-finance project. This
file records what changed and why, so the diff from the original is reviewable rather
than mysterious. The behavior of the financial engines was **preserved throughout** — the test suite
passed at every step (292 tests through the cleanup; 296 after the SQL analytics layer
below added four).

## Provenance / git history

The copy was created with `git archive HEAD` from the private repo, so it starts from
**clean, empty history** — none of the original commits (which contain personal detail
in messages and files) are inherited. A scan of the original's full history found **no
secrets, keys, tokens, or real-data snapshots ever committed**; the only sensitive
content there was personal PII in commit messages and a few committed files, none of
which crosses into this copy. The original's history is the owner's to rewrite or not;
nothing was done to it here.

## 1. PII and secrets removed

- Removed all personal identity from source: a real name and email (`delivery`,
  `dispute_agent`), a real bank name, hardcoded Vercel org/project IDs and account
  slug (`deploy.sh`), and real account balances (`build_site`, a test).
- Removed **personal data encoded in logic**, not just in strings: a hardcoded
  merchant-specific category special-case and a magic-number obligation-matching
  branch, both keyed to the author's real transactions. Neither generalized; removing
  them reverts to the generic behavior. (Noted because this is the one place PII
  removal nudged behavior.)
- Replaced real data files with synthetic templates under `examples/`
  (`rules`, `obligations`, `receipts`, `plaid_items`, `connection_owners`), and tightened
  `.gitignore` so the real filenames can never be committed.
- Deleted a personal `docs/` folder (dated working audits containing legal/health/
  family detail) and a personal deploy wrapper (`run_deploy.sh`, which also embedded a
  token in a git-push URL). The launchd plist was genericized to a username-free template.

## 2. De-personalized to a generic tool

The project was built around one person's specific goal. That framing was generalized to
a configurable **"savings goal"**:

- Renamed the `the secondary region` summary-dict key → `goal`, `project_the secondary region()` → `project_goal()`,
  `monk_budget` → `discretionary_budget`; "the secondary region Fund" branding → neutral product
  naming; example figures genericized.
- The static report's **USD↔BRL currency toggle + PPP orientation was kept** as a
  self-contained example of client-side currency re-denomination (removing a working
  feature, or generalizing it to arbitrary currencies, was out of scope). See DECISIONS.

## 3. Packaging and structure

- Flat root (33 modules at top level, bare imports) → an installable
  `src/finance_mcp/` package grouped by the data flow: `ingest / store / engines /
  report` + a top-level orchestrator. Imports rewritten to absolute package paths.
- Added `pyproject.toml` (PEP 621, **zero runtime dependencies**, a `finance-mcp`
  console script, ruff config), moved tests into `tests/` with a `conftest.py` and a
  synthetic fixture, added an MIT `LICENSE`, and a GitHub Actions CI workflow
  (lint + tests on Python 3.10/3.11/3.12).

## 4. Runs end-to-end on a clean clone

- Added a synthetic data generator (`demo.py`) and a `finance-mcp demo` command, so the
  whole pipeline runs with no bank credentials and no real data.
- `test_finance_agent` previously loaded the gitignored real `transactions.json` and
  failed on a fresh checkout; it now loads the committed synthetic fixture.
- `build_site` now falls back to bundled example rules when no `rules.md` is present, so
  the static-site build runs on a clean clone.

## 5. Cleanup (no behavior change)

- Removed unused imports and unused local variables across the codebase (verified by
  `ruff`); the suite stayed green, confirming the removals were dead.
- Corrected stale docstrings that described a long-finished storage migration as still
  "in progress" / "not yet wired" (the SQLite store is the live read+write path).

## 6. Bug found and fixed (flagged)

`budget_scorer.render_scorecard` referenced an **undefined `ICON` dict → a `NameError`**
whenever the standalone `budget_scorer` CLI scored a rule. It is **not** on the live
digest path (the demo and site go through `finance_agent`, which never calls it), which
is why it went unnoticed. Fixed with the obvious one-liner —
`ICON = {"on track": "✅", "drifting": "⚠️", "slipped": "🔻"}` — the same icons already
used literally one line above. This is the only intentional logic change in the cleanup;
called out here rather than slipped in silently.

## 7. Deliberately left alone (flagged, not done)

- A **~300-line dead-but-tested email renderer** in `report/digest_templates.py`
  (`render_email_html` / `_build_email_portion`) has no live caller, but it is covered by
  tests and interleaved with the live report renderer in a large module. Recommended as a
  deliberate follow-up removal rather than a risky surgical edit here.
- **Two small duplicated detectors** (transfer-exclusion and recurring-stream detection,
  each in two places with slightly different thresholds) were left intact — unifying them
  would change outputs and belongs in its own change with its own tests.
- **`llm_matcher._call_haiku`** still uses `urllib` directly instead of the `safehttp`
  SSRF wrapper. Routing it through `safehttp` is a sensible hardening follow-up, not done
  here because it alters a working network path.

## 8. Added a SQL analytics layer

The descriptive reporting rollups are now expressed in SQL, both because it is the
idiomatic tool for set-based analytics over a relational store and as a deliberate
demonstration of SQL competency. `store/queries.sql` holds three readable, commented
CTE queries using window functions — monthly cash flow with a running total
(`SUM() OVER`) and month-over-month delta (`LAG()`), category breakdown as a share of
spend (ratio-to-total window), and top merchants ranked (`RANK()`). `store/analytics.py`
runs them and `finance-mcp analytics` prints them. `tests/test_analytics.py` cross-checks
every query result against an independent Python recomputation so the SQL and the
engines can never silently diverge. The algorithmic forecasting/cadence math was left in
Python — SQL would be the wrong tool for it. (See `docs/DECISIONS.md` §3.)

## Verification

`pip install -e ".[dev]"` succeeds; `ruff check src tests` is clean; **296 tests pass**
via the installed package; `finance-mcp demo` and `build_site` both produce output from
synthetic data. PII/secret sweeps over the whole tree come back clean.
