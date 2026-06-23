#!/bin/bash
# deploy.sh — private deploy of the report site to Vercel.
#
# On a Vercel Hobby plan, "Vercel Authentication" only protects
# "all_except_custom_domains", so a --prod deploy auto-creates a PUBLIC random
# production alias that bypasses auth. This script deploys to production (so the
# gated, stable project URL updates) and then removes ANY alias that is publicly
# reachable, leaving only gated URLs. (Robust to the random alias name changing.)
#
# Configure these in a gitignored .env (see examples/env.example) or CI secrets:
#   VERCEL_ORG_ID, VERCEL_PROJECT_ID, VERCEL_SCOPE, VERCEL_STABLE_URL
# (VERCEL_TOKEN is read by the Vercel CLI directly.)
#
# Usage:  ./deploy.sh [--balance 1000.00]
set -euo pipefail
cd "$(dirname "$0")"

# shellcheck disable=SC1091
[ -f ./.env ] && source ./.env

SCOPE="${VERCEL_SCOPE:?set VERCEL_SCOPE in .env}"
STABLE="${VERCEL_STABLE_URL:?set VERCEL_STABLE_URL in .env}"
# Pin to the project the stable URL is on, so deploys don't drift to a
# directory-named project. Without this, `vercel deploy ./site` targets a
# project named after the folder and the URL goes stale.
export VERCEL_ORG_ID="${VERCEL_ORG_ID:?set VERCEL_ORG_ID in .env}"
export VERCEL_PROJECT_ID="${VERCEL_PROJECT_ID:?set VERCEL_PROJECT_ID in .env}"

echo "[deploy] building site…"
python3 build_site.py "$@"

echo "[deploy] deploying to production…"
npx vercel deploy ./site --prod --yes --scope "$SCOPE" >/dev/null

echo "[deploy] scanning project aliases for any PUBLIC ones…"
npx vercel alias ls --scope "$SCOPE" 2>/dev/null \
  | grep -oE '[a-z0-9.-]+\.vercel\.app' | sort -u | while read -r a; do
    code=$(curl -s -o /tmp/_dep_check.html -w "%{http_code}" --max-time 15 "https://$a/" 2>/dev/null || echo 000)
    if [ "$code" = "200" ] && grep -qiE "Savings Pace|Your money|finance.mcp" /tmp/_dep_check.html 2>/dev/null; then
      echo "  PUBLIC alias found: $a — removing"
      npx vercel alias rm "$a" --yes --scope "$SCOPE" 2>/dev/null || true
    fi
  done

echo "[deploy] verifying privacy…"
code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 20 "$STABLE/")
echo "  $STABLE -> HTTP $code  (401 = private, as expected)"
[ "$code" = "401" ] || echo "  ⚠️  expected 401 — check protection settings!"
echo "[deploy] done. Private URL: $STABLE"
