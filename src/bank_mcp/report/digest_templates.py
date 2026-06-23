#!/usr/bin/env python3
"""
digest_templates.py — v3 HTML template engine for bank.mcp finance digests.

Renders weekly and monthly digest HTML reports from the pipeline's digest dict.
Matches the approved designs in digest-weekly-sample.html and
digest-monthly-sample.html exactly.

Public API:
  render_weekly_html(digest, ...)      full weekly report (email + full report)
  render_monthly_html(digest, ...)     full monthly report (email + full report)

Money is formatted via money_html() / money(). No top-level side effects.
"""



# ─────────────────────────── helpers ───────────────────────────────────────────

from bank_mcp.report._report_styles import _CSS
from bank_mcp.report._report_sections import (
    _section_balance_change, _section_forecast, _section_goal_pace,
    _section_fee_fraud, _section_recurring, _section_mom,
    _section_spending_breakdown,
)
from bank_mcp.report._report_format import (
    _esc, _format_date_range, _format_date_long,
)
from bank_mcp.report.delivery import _updated_stamp


























# ─────────────────────────── CSS ──────────────────────────────────────────────





# ─────────────────────────── section builders ─────────────────────────────────






















# ─────────────────────────── email portion builder ────────────────────────────



# ─────────────────────────── full-page wrappers ───────────────────────────────

def _html_head(title, is_monthly=False):
    """Build the <head> with CSS and Google Fonts."""
    weight_range = "400;500;600;700;800" if is_monthly else "400;500;600;700"
    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="color-scheme" content="light only">
<title>{_esc(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,600;12..96,700;12..96,800&family=Inter:wght@{weight_range}&family=Spline+Sans+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
{_CSS}
</style>
</head>
<body>
"""


def _currency_assets(digest):
    """Bake the build-time FX config and pull in the toggle module.

    Emitted only on the hosted full report (email clients ignore <script>). The
    pipeline puts the live-fetched rate in digest['fx']; when no secondary currency
    is configured, ``ccy`` is null and currency.js leaves the report USD-only.
    """
    import json as _json
    fx = digest.get("fx", {}) or {}
    payload = {k: fx.get(k) for k in ("rate", "ppp", "date", "ccy", "locale")}
    return f"""\
  <script>window.__FX = {_json.dumps(payload)};</script>
  <script src="assets/currency.js" defer></script>
"""


def _drilldown_assets(txns_embed):
    """Embed the transactions (date/merchant/amount/category) + the drilldown
    module, so every tagged aggregate opens to its atomic transactions. Served
    only behind the private auth wall; omitted from the email."""
    if not txns_embed:
        return ""
    import json as _json
    return ("  <script>window.__TXNS = "
            + _json.dumps(txns_embed, separators=(",", ":"))
            + ";</script>\n  <script src=\"assets/drilldown.js\" defer></script>\n")


def _currency_toggle(digest):
    """USD↔secondary-currency toggle + live-rate readout for the hosted report header.

    Hidden by default; currency.js labels the secondary button and reveals the toggle
    only when a secondary currency was baked into __FX (REPORT_SECONDARY_CURRENCY).
    """
    return """\
      <div class="fx" data-fx-toggle role="group" aria-label="Display currency" hidden>
        <span class="fx-rate" data-fx-rate></span>
        <div class="fx-seg">
          <button type="button" data-cur="USD">US$</button>
          <button type="button" data-cur data-fx-secondary></button>
        </div>
      </div>
"""


def _report_brandbar(digest, mode):
    """Landing-matched paper brandbar for the hosted report (sunburst + serif
    wordmark + private tag). Replaces the dark email masthead so the report
    reads like the homepage rather than an email preview."""
    window = digest.get("window", {})
    _format_date_range(window.get("start", ""), window.get("end", ""))
    return """\
    <div class="brandbar">
      <svg class="spark" width="26" height="26" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <g stroke="#009C53" stroke-width="2.1" stroke-linecap="round">
          <path d="M12 2.5v6"/><path d="M12 15.5v6"/><path d="M2.5 12h6"/><path d="M15.5 12h6"/>
        </g>
        <g stroke="#FFCB00" stroke-width="2.1" stroke-linecap="round">
          <path d="M5.2 5.2l4.2 4.2"/><path d="M14.6 14.6l4.2 4.2"/><path d="M18.8 5.2l-4.2 4.2"/><path d="M9.4 14.6l-4.2 4.2"/>
        </g>
        <circle cx="12" cy="12" r="2.3" fill="#0066CC"/>
      </svg>
      <div class="brandbar-word">Savings Goal<span class="brandbar-mono">bank.mcp</span></div>
      <span class="brandbar-private">
        <svg width="11" height="11" viewBox="0 0 16 16" fill="none"><path d="M4 7V5a4 4 0 0 1 8 0v2" stroke="#00803F" stroke-width="1.5"/><rect x="3" y="7" width="10" height="7" rx="1.5" fill="#009C53"/></svg>
        Private
      </span>
    </div>
"""


def _html_footer(digest):
    """Build the report footer."""
    as_of = digest.get("as_of", "")
    window = digest.get("window", {})
    mode = digest.get("mode", "weekly")
    gen_date = _format_date_long(as_of) if as_of else ""
    window_str = _format_date_range(window.get("start", ""), window.get("end", ""))
    mode_label = "Monthly Digest" if mode == "monthly" else "Weekly Digest"

    return f"""\
    <div class="report-footer">
      <div style="width:32px;height:3px;background:var(--navy);border-radius:2px;margin:0 auto 12px;opacity:0.2;"></div>
      <div class="footer-brand">Personal Finance Suite</div>
      <div class="footer-meta">
        Generated {gen_date}<br>
        Window: {window_str} &middot; {mode_label} &middot; Deterministic, no AI in the numbers
      </div>
    </div>
"""


# ─────────────────────────── render_weekly_html ───────────────────────────────

def render_weekly_html(digest, report_url=None, txns_embed=None):
    """Render the full weekly HTML report.

    Structure:
    - Email portion (header, hero, vitals, summaries, CTA)
    - Divider
    - Full report (5 collapsible sections)
    - Footer
    """
    window = digest.get("window", {})
    date_range = _format_date_range(window.get("start", ""), window.get("end", ""))

    html = _html_head("bank.mcp - Weekly Finance Digest")

    html += '<div class="container">\n'

    # Landing-matched paper brandbar (replaces the old dark email masthead +
    # redundant email-preview block, so the hosted report reads like the homepage)
    html += _report_brandbar(digest, "weekly")

    # Full report
    html += f"""\
  <div class="full-report" id="full-report">

    <div class="report-header">
      <div class="report-header-row">
        <div>
          <div class="report-eyebrow">Your snapshot</div>
          <h2>How you're doing</h2>
          <div class="report-meta">As of {_format_date_long(digest.get("as_of", "")) or date_range}</div>
          <div class="report-meta" style="opacity:0.7">Updated {_updated_stamp(digest)}</div>
        </div>
{_currency_toggle(digest)}      </div>
    </div>

"""

    # ONE unified snapshot — every section in its clean variant. (Receipts are
    # invisible infrastructure — findings annotate forecast/fee_fraud/recurring.)
    html += _section_balance_change(digest)
    html += _section_forecast(digest, "01")
    html += _section_mom(digest, "02")
    html += _section_spending_breakdown(digest, "03")
    html += _section_goal_pace(digest, "04")
    html += _section_fee_fraud(digest, "05")
    html += _section_recurring(digest, "06")

    # Footer
    html += _html_footer(digest)

    html += "  </div>\n\n</div>\n\n"
    html += _currency_assets(digest)
    html += _drilldown_assets(txns_embed)
    html += "\n</body>\n</html>"

    return html


# ─────────────────────────── render_monthly_html ──────────────────────────────

def render_monthly_html(digest, report_url=None):
    """Deprecated — weekly/monthly collapsed into one live snapshot. Alias."""
    return render_report_html(digest, report_url)


# render_report_html is the single canonical snapshot renderer.
render_report_html = render_weekly_html



