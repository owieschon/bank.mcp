#!/usr/bin/env python3
"""build_site.py — assemble the static private report site for Vercel.

Optionally fetches a live USD→secondary-currency rate once (baked as the toggle's
fallback; USD-only unless REPORT_SECONDARY_CURRENCY is set), builds the weekly +
monthly reports on real bank data, fills the dark landing page, and copies the
toggle asset into a self-contained ./site directory. Static output — no framework,
so a Vercel deploy spends ~no build minutes.

    python3 build_site.py [--balance 1000.00]

Then deploy (interactive auth the first time):
    npx vercel deploy ./site --yes
"""
import logging
import argparse
import datetime as dt
import json
import os
import shutil

from bank_mcp.ingest import safehttp
from bank_mcp import finance_agent as fa
from bank_mcp.report import digest_templates as dtpl
from bank_mcp.store import db

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")          # bundled landing template + JS assets
SITE = os.path.abspath("site")            # build output, written under the CWD
# Fall back to the bundled example rules so a build runs on a clean clone.
DEFAULT_RULES = os.path.join(HERE, "..", "data", "rules.demo.md")


log = logging.getLogger(__name__)


def _secondary_currency():
    """Optional secondary display currency for the report's USD↔X toggle, from env.

    Unset → the report ships USD-only (no toggle), so the artifact carries no baked
    locale. To enable, set REPORT_SECONDARY_CURRENCY (ISO code, e.g. EUR, GBP, JPY),
    optionally REPORT_SECONDARY_LOCALE (default en-US) and REPORT_SECONDARY_PPP.
    """
    code = os.environ.get("REPORT_SECONDARY_CURRENCY", "").strip().upper()
    if not code:
        return None
    return {
        "ccy": code,
        "locale": os.environ.get("REPORT_SECONDARY_LOCALE", "en-US"),
        "ppp": float(os.environ.get("REPORT_SECONDARY_PPP", "0") or 0) or None,
    }


def fetch_fx():
    """Live USD→<secondary> rate for the report's currency toggle, if one is configured.

    Returns ``{"ccy": None}`` (USD-only) when no secondary currency is set or the live
    fetch fails — never a guessed rate.
    """
    sec = _secondary_currency()
    if not sec:
        return {"ccy": None}
    ccy = sec["ccy"]
    sources = [
        ("https://open.er-api.com/v6/latest/USD", lambda d: d["rates"][ccy]),
        (f"https://api.frankfurter.dev/v1/latest?base=USD&symbols={ccy}", lambda d: d["rates"][ccy]),
    ]
    for url, pick in sources:
        try:
            with safehttp.fetch(url, timeout=12, retries=2) as r:
                rate = float(pick(json.load(r)))
            if rate > 0:
                return {"rate": round(rate, 4), "ppp": sec["ppp"],
                        "date": dt.date.today().isoformat(), "ccy": ccy, "locale": sec["locale"]}
        except Exception:
            continue
    return {"ccy": None}


def _digest(txns, balance, mode, fx, rules_path):
    d = fa.build_digest(
        txns, balance=balance, mode=mode,
        forecast_days=fa.DEFAULT_FORECAST_DAYS, buffer=fa.DEFAULT_BUFFER,
        include_burn=True, scan_days=fa.DEFAULT_SCAN_DAYS, rules_path=rules_path,
        today=dt.date.today(),   # anchor forecast at today (balance is live, feed lags)
    )
    fa.attach_balance_change(d, txns, balance)   # what moved the balance since last build
    d["fx"] = fx
    return d


def _fill_landing(fx, as_of):
    src = os.path.join(WEB, "index.html")
    with open(src) as f:
        html = f.read()
    return (html
            .replace("{{WEEKLY_DATE}}", as_of or "—")
            .replace("{{MONTHLY_DATE}}", as_of or "—")
            .replace("{{SNAPSHOT_DATE}}", as_of or "—")
            .replace("{{GENERATED}}",
                     f"FX {fx['ccy']} {fx['rate']} · {fx['date']}" if fx.get("ccy") else (as_of or "—")))


def _load_canonical_txns(json_path):
    """Read transactions through the canonical DB rather than the raw JSON snapshot.

    Seed the DB from the current snapshot (idempotent), then read back through the
    adapter. The reconstructed dicts are byte-identical to the JSON, so engine output
    is unchanged, and the DB stays the single source of record (which is what lets
    additional account holders / currencies land here without a build change). Falls
    back to the raw JSON if the DB can't be used, so a build never hard-fails on the
    datastore.
    """
    try:
        with open(json_path) as f:
            snapshot = json.load(f)
        conn = db.connect()
        db.init_schema(conn)
        db.upsert_transactions(conn, snapshot)
        txns = db.load_transactions_from_db(conn)
        conn.close()
        if txns:
            log.info("read %d txns from canonical DB", len(txns))
            return txns
        log.warning("DB empty after seed; using JSON snapshot")
    except Exception as e:
        log.warning("DB read unavailable (%s); falling back to JSON snapshot", e)
    with open(json_path) as f:
        return json.load(f)


def main():
    from bank_mcp import _logging
    _logging.configure()
    ap = argparse.ArgumentParser()
    ap.add_argument("--balance", type=float, default=None,
                    help="account balance for the forecast (default: live fetch)")
    ap.add_argument("--txns", default="transactions.json")
    ap.add_argument("--rules", default=None,
                    help="rules file (default: ./rules.md if present, else bundled example)")
    args = ap.parse_args()

    rules_path = args.rules or ("rules.md" if os.path.exists("rules.md") else DEFAULT_RULES)

    balance = args.balance
    if balance is None:
        try:
            from bank_mcp.ingest import plaid_bridge as pb
            b = pb.fetch_balance()
            balance = b.get("available") or b.get("current")
            log.info("live balance fetched")
        except Exception as e:
            log.warning("live balance fetch failed (%s); forecast unavailable", e)

    txns = _load_canonical_txns(os.path.abspath(args.txns))

    fx = fetch_fx()
    if fx.get("ccy"):
        log.info("FX baked: US$1 = %s %s (ppp %s) on %s", fx["ccy"], fx["rate"], fx["ppp"], fx["date"])
    else:
        log.info("report currency: USD only (set REPORT_SECONDARY_CURRENCY to enable a toggle)")

    # One unified live snapshot (weekly/monthly collapsed). Monthly mode builds
    # the full data (month-by-month history + category breakdown).
    snapshot = _digest(txns, balance, "monthly", fx, rules_path)

    # Atomic transactions for drill-down (date / merchant / amount / human
    # category) — embedded only in the private report, NOT in the LLM digest.
    from bank_mcp.engines import budget_scorer as bs
    embed = [{"d": t["date"],
              "m": (t.get("merchantName") or t.get("description") or "?")[:32],
              "a": round(float(t["amount"]), 2),
              "c": bs._human_label(t) or "Other"}
             for t in txns if t.get("amount") and t.get("date")]
    embed.sort(key=lambda e: e["d"], reverse=True)   # recency-first; source-order independent
    report_html = dtpl.render_report_html(snapshot, txns_embed=embed)

    # Clean output dir
    if os.path.isdir(SITE):
        shutil.rmtree(SITE)
    os.makedirs(os.path.join(SITE, "assets"))

    # report.html is canonical; weekly/monthly kept as copies for old links.
    for name in ("report.html", "weekly.html", "monthly.html"):
        with open(os.path.join(SITE, name), "w") as f:
            f.write(report_html)

    for asset in ("currency.js", "drilldown.js"):
        shutil.copy(os.path.join(WEB, "assets", asset),
                    os.path.join(SITE, "assets", asset))

    with open(os.path.join(SITE, "index.html"), "w") as f:
        f.write(_fill_landing(fx, snapshot.get("as_of", "")))

    # Cache headers: assets immutable-ish, html always revalidated.
    with open(os.path.join(SITE, "vercel.json"), "w") as f:
        json.dump({
            "$schema": "https://openapi.vercel.sh/vercel.json",
            "headers": [
                {"source": "/assets/(.*)",
                 "headers": [{"key": "Cache-Control", "value": "public, max-age=3600"}]},
                {"source": "/(.*)\\.html",
                 "headers": [{"key": "Cache-Control", "value": "no-cache"}]},
            ],
        }, f, indent=2)

    log.info("wrote %s/ (index.html, report/weekly/monthly.html, assets, vercel.json)", SITE)


if __name__ == "__main__":
    main()
