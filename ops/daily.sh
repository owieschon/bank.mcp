#!/bin/bash
# Daily finance update — the launchd job runs this at 6:30 AM.
#
#   1. sync the bank -> finance.db + ledger, analyze on the LIVE balance, and email
#      the digest (email is step 1 so a deploy hiccup never costs the daily email).
#   2. refresh the private web report so the link in the email is current (non-fatal).
#
# Self-sufficient PATH: launchd starts jobs with a minimal environment, so set the
# PATH explicitly here (python3, node/npx, vercel all live under homebrew).
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
set -uo pipefail
cd "$(dirname "$0")"

echo "[daily] $(date '+%Y-%m-%d %H:%M:%S') starting"

# 1) sync + analyze (live balance auto-fetched) + email — the deliverable.
#    --reliable: idempotent date-window pull (self-healing). The incremental cursor
#    sync could advance past transactions and silently lose them (caused a 4-day
#    stale gap on 2026-06-20); the date-window pull always returns the full window.
python3 sync.py --monthly --email --no-voice --reliable
SYNC_EXIT=$?
echo "[daily] sync exit=$SYNC_EXIT"
if [ "$SYNC_EXIT" -ne 0 ]; then
    echo "[daily] sync failed — skipping deploy to preserve last-good state"
    exit 1
fi

# 2) refresh the hosted report (non-fatal — the email is already out).
if ./deploy.sh; then
  echo "[daily] web report refreshed"
else
  echo "[daily] deploy failed (non-fatal) — email already sent"
fi

echo "[daily] done $(date '+%H:%M:%S')"
