# finance.mcp

A personal-finance analysis suite that turns a stream of bank transactions into a
single digest — cash-flow forecast, savings-goal pace, spending breakdown,
fee/duplicate detection, recurring-charge detection, and receipt reconciliation —
rendered as both a Markdown/email digest and an auth-gated static web report.

The line it draws between the math and the LLM:

> All financial math is plain, deterministic, unit-tested Python. A language model
> is used only to *narrate* a compact summary, *match* merchant-name strings, and
> *extract* text from receipt emails. **Raw transaction rows never enter a model
> prompt** — only small per-section summary dicts do. A `--no-voice` run is fully
> correct with zero tokens and no network.

It runs on the **Python standard library only — zero runtime dependencies.**

> Status: a personal project, cleaned up as a work sample. All data in the repo is
> synthetic (`examples/`, `src/finance_mcp/demo.py`); there is no real financial data
> here. 303 tests pass; `ruff` and `mypy` are clean.

## Shape of the system

```
  bank (Plaid / bank-mcp)          ← real source, not committed
        │
        ▼
  ingest/   transport + sync ──────► store/   SQLite (canonical, lossless `raw` JSON)
                                          │
                                          ▼
                                     engines/  deterministic cores
                                     (forecast · pace · fees · recurring · receipts)
                                          │
                                          ▼   compact summary dicts (never raw rows)
                                     report/   digest (md/email) + static site
                                          ▲
                                     finance_agent.py  ← orchestrator
                                          │
                                     LLM: narrate / match / extract  (edges only)
```

The package layout mirrors that flow:

```
src/finance_mcp/
  ingest/    safehttp · plaid_bridge · plaid_link · sync
  store/     db (SQLite) · subscription_creep (field/cadence accessors) ·
             obligation_registry · merchant_categorizer ·
             queries.sql + analytics (SQL reporting read-models)
  engines/   cashflow_forecaster · budget_scorer · fee_fraud_scan ·
             recurring · receipt_scanner · dispute_agent · llm_matcher
  report/    delivery · digest_templates · build_site · web/
  finance_agent.py   # orchestrator: reconcile → run each engine → one digest
  demo.py            # synthetic data + `python -m finance_mcp demo`
tests/        unit tests + a synthetic transaction fixture
examples/     copy-these config templates (synthetic)
ops/          launchd plist + deploy scripts (author-local)
docs/         ARCHITECTURE · SETUP · DECISIONS
```

## Quickstart (two minutes)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

finance-mcp demo        # build + print a full digest from synthetic data
finance-mcp analytics   # SQL reporting rollups (see src/finance_mcp/store/queries.sql)
pytest -q               # 303 tests
ruff check src tests    # lint
mypy                    # type-check the package
```

`finance-mcp demo` needs no bank credentials and no real data — it generates a
synthetic dataset and runs the whole pipeline end to end. To build the static
report site from a dataset:

```bash
python -m finance_mcp.report.build_site --balance 1200 --txns path/to/transactions.json
# writes ./site/  (index.html + report.html + assets)
```

## Using it with real data

See [docs/SETUP.md](docs/SETUP.md). In short: copy the `examples/*.example.*`
templates to real filenames, point the loaders at them, and connect a bank via
Plaid / a bank-mcp subprocess fork (transport lives in `ingest/`). Real data
files are gitignored by default.

## Docs

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — layers, data flow, the SQLite
  schema, and the LLM boundary.
- [docs/DECISIONS.md](docs/DECISIONS.md) — why SQLite (not Postgres), the SQL/Python
  split, and what was left alone, and why.
- [docs/SETUP.md](docs/SETUP.md) — install, run, test, and the deploy model.
- [CHANGES.md](CHANGES.md) — what changed when this was prepared as a public work sample.

## License

MIT — see [LICENSE](LICENSE).
