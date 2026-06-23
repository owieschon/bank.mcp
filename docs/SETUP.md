# Setup

## Requirements

- Python **3.10+** (developed and tested on 3.14).
- No third-party runtime dependencies. `ruff` and `pytest` are only needed for
  development and are installed via the `dev` extra.

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Run the demo (no bank, no real data)

```bash
finance-mcp demo            # build + print a digest from synthetic data
finance-mcp demo --json     # dump the synthetic transaction dataset
```

The synthetic dataset is generated deterministically by `src/finance_mcp/demo.py`
and is also the source of the test fixture (`tests/fixtures/transactions.sample.json`).

## Tests and lint

```bash
pytest -q                   # 292 tests
ruff check src tests
```

## Build the static report site

```bash
python -m finance_mcp.report.build_site --balance 1200 --txns path/to/transactions.json
# writes ./site/  (index.html landing page + report.html + assets)
```

With no `--txns` it defaults to `transactions.json` in the working directory; with no
`rules.md` present it falls back to the bundled example rules, so a build always runs.

## Using real data

The suite reads real data from files that are **gitignored by default** — nothing real
is ever committed. To wire it up:

1. Copy the templates and fill them in:
   ```bash
   cp examples/rules.example.md          rules.md
   cp examples/obligations.example.json  obligations.json
   cp examples/plaid_items.example.json  plaid_items.json
   ```
2. Connect a bank. Transport lives in `src/finance_mcp/ingest/`:
   - a bank-mcp subprocess fork (reads its own
     `~/.bank-mcp/config.json`), or
   - direct Plaid — set `PLAID_ACCESS_TOKEN` (env var or macOS Keychain). Mint a token
     for a new bank Item with `python -m finance_mcp.ingest.plaid_link`.
3. Credentials resolve **env var → macOS Keychain** (e.g. `ANTHROPIC_API_KEY`,
   `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `PLAID_*`). Nothing is hardcoded.

## Deploy (optional, author-local)

`ops/deploy.sh` deploys the static site to a private, auth-gated Vercel project. It
reads all infrastructure IDs from a gitignored `.env` / CI secrets
(`VERCEL_ORG_ID`, `VERCEL_PROJECT_ID`, `VERCEL_SCOPE`, `VERCEL_STABLE_URL`; the Vercel
CLI reads `VERCEL_TOKEN` itself) and then strips any publicly-reachable alias so the
report stays 401-gated. `ops/com.example.finance-daily.plist` is a launchd template
that runs `ops/daily.sh` on a schedule (replace the placeholder paths first).
