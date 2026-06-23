#!/usr/bin/env python3
"""
digest_templates.py — v3 HTML template engine for finance.mcp finance digests.

Renders weekly and monthly digest HTML reports from the pipeline's digest dict.
Matches the approved designs in digest-weekly-sample.html and
digest-monthly-sample.html exactly.

Public API:
  select_hero(digest) -> dict          pick highest-priority hero card data
  render_weekly_html(digest, ...)      full weekly report (email + full report)
  render_monthly_html(digest, ...)     full monthly report (email + full report)
  render_email_html(digest, ...)       compact email-only portion

All money formatting goes through delivery.money_html(). No top-level side effects.
"""

import datetime as dt
import html as _html
from finance_mcp.report.delivery import money


# ─────────────────────────── helpers ───────────────────────────────────────────

def _esc(s):
    """HTML-escape a string."""
    return _html.escape(str(s)) if s else ""


def _format_date_range(start, end):
    """Format a date range like 'Jun 9 -- 15, 2026'.

    Handles same-month and cross-month ranges, with en-dash.
    """
    try:
        s = dt.date.fromisoformat(start)
        e = dt.date.fromisoformat(end)
    except (ValueError, TypeError):
        return f"{start} &ndash; {end}"

    if s.month == e.month and s.year == e.year:
        return f"{s.strftime('%b')} {s.day} &ndash; {e.day}, {e.year}"
    elif s.year == e.year:
        return f"{s.strftime('%b')} {s.day} &ndash; {e.strftime('%b')} {e.day}, {e.year}"
    else:
        return f"{s.strftime('%b')} {s.day}, {s.year} &ndash; {e.strftime('%b')} {e.day}, {e.year}"


def _format_date_long(date_str):
    """Format a date like 'Jun 17, 2026'."""
    try:
        d = dt.date.fromisoformat(date_str)
        return f"{d.strftime('%b')} {d.day}, {d.year}"
    except (ValueError, TypeError):
        return str(date_str) if date_str else ""


def _updated_stamp(digest):
    """Human-readable build/sync time (always current), e.g. 'Jun 20, 2026, 7:14 AM'.

    Distinct from `as_of` (last transaction date) — this is when the data was last
    refreshed, so a quiet day with no new transactions still reads as up to date.
    """
    raw = digest.get("generated_at")
    try:
        t = dt.datetime.fromisoformat(raw) if raw else dt.datetime.now()
    except (ValueError, TypeError):
        t = dt.datetime.now()
    hour = t.hour % 12 or 12
    return f"{t.strftime('%b')} {t.day}, {t.year}, {hour}:{t.minute:02d} {t.strftime('%p')}"


def _format_date_short(date_str):
    """Format a date like 'Jun 17'."""
    try:
        d = dt.date.fromisoformat(date_str)
        return f"{d.strftime('%b')} {d.day}"
    except (ValueError, TypeError):
        return str(date_str) if date_str else ""


def money_html(x):
    """HTML money with a data-usd hook for the client-side USD↔BRL toggle.

    Renders the canonical USD string but wraps it in a span carrying the raw
    numeric value, so assets/currency.js can re-denominate it to BRL in the
    hosted report (email clients ignore the attribute and just show USD).
    Non-numeric input falls back to plain money() so nothing breaks.
    """
    if not isinstance(x, (int, float)) or isinstance(x, bool):
        return money(x)
    return f'<span data-usd="{x:.2f}">{money(x)}</span>'


def money_html_short(x):
    """Short money ($2,847, no cents) with the data-usd toggle hook.

    Marked data-usd-short so currency.js keeps it cents-free in both currencies
    (hero amount, vitals strip, big numbers). Non-numeric falls back to plain.
    """
    if not isinstance(x, (int, float)) or isinstance(x, bool):
        return _money_short(x)
    return f'<span data-usd="{x:.2f}" data-usd-short>{_money_short(x)}</span>'


def _money_short(x):
    """Short money: $2,847 (no cents) for vitals strip."""
    if x is None:
        return "$0"
    return f"${x:,.0f}"


def _severity_color(severity):
    """Return the CSS variable name for a severity level."""
    return {"red": "var(--red)", "amber": "var(--amber)", "green": "var(--green)"}.get(severity, "var(--text-primary)")


def _severity_bg(severity):
    return {"red": "var(--red-bg)", "amber": "var(--amber-bg)", "green": "var(--green-bg)"}.get(severity, "var(--bg-gray-50)")


def _severity_border(severity):
    return {"red": "var(--red-border)", "amber": "var(--amber-border)", "green": "var(--green-border)"}.get(severity, "var(--border-light)")


def select_hero(digest):
    """Pick the highest-priority issue for the hero card.

    Priority order:
      1. Overdraft risk         -> red
      2. Low balance warning    -> amber
      3. goal behind pace     -> red
      4. Fee/fraud flagged      -> amber
      5. All clear              -> green

    Returns dict with severity, badge_text, amount, subtitle.
    """
    sections = digest.get("sections", {})

    # 1. Overdraft risk
    fc = sections.get("forecast", {})
    fh = fc.get("headline", {})
    if fc.get("available", False) and fh.get("overdraft"):
        min_bal = fh.get("min_balance", 0)
        min_date = fh.get("min_date", "")
        safe_by = fh.get("safe_by")
        try:
            md = dt.date.fromisoformat(min_date)
            date_display = f"{md.strftime('%b')} {md.day}"
        except (ValueError, TypeError):
            date_display = min_date
        subtitle = f"projected balance on <strong>{date_display}</strong>"
        if safe_by:
            try:
                sb = dt.date.fromisoformat(safe_by)
                safe_display = f"{sb.strftime('%b')} {sb.day}"
            except (ValueError, TypeError):
                safe_display = safe_by
            subtitle += f" &mdash; move money by {safe_display}"
        return {
            "severity": "red",
            "badge_text": "Overdraft Risk",
            "amount": money_html_short(min_bal),
            "subtitle": subtitle,
        }

    # 2. Low balance warning
    if fc.get("available", False) and fh.get("low_balance"):
        min_bal = fh.get("min_balance", 0)
        min_date = fh.get("min_date", "")
        buffer_val = fh.get("buffer", 0)
        try:
            md = dt.date.fromisoformat(min_date)
            date_display = f"{md.strftime('%b')} {md.day}"
        except (ValueError, TypeError):
            date_display = min_date
        subtitle = f"projected balance on <strong>{date_display}</strong> &mdash; below {money_html(buffer_val)} buffer"
        return {
            "severity": "amber",
            "badge_text": "Low Balance",
            "amount": money_html_short(min_bal),
            "subtitle": subtitle,
        }

    # 3. goal behind pace
    bud = sections.get("budget", {})
    bh = bud.get("headline", {})
    if bh.get("status") == "behind":
        projected = bh.get("projected", 0)
        move_date = bh.get("move_date", "")
        gap = bh.get("gap", 0)
        return {
            "severity": "red",
            "badge_text": "Behind Pace",
            "amount": money_html_short(projected),
            "subtitle": f"projected by <strong>{_esc(_format_date_long(move_date))}</strong> &mdash; {money_html(gap)} gap to target",
        }

    # 4. Large receipt discrepancy (from reconciliation)
    recon = sections.get("reconciliation", {})
    rech = recon.get("headline", {})
    if rech.get("discrepancy_amount", 0) >= 10:
        disc_amt = rech["discrepancy_amount"]
        n_disc = rech.get("n_discrepancies", 0)
        return {
            "severity": "amber",
            "badge_text": "Receipt Mismatch",
            "amount": money_html_short(disc_amt),
            "subtitle": f"{n_disc} charge{'s' if n_disc != 1 else ''} differ from receipt amount &mdash; verify with bank",
        }

    # 5. Fee/fraud flagged
    fee = sections.get("fee_fraud", {})
    ffh = fee.get("headline", {})
    if ffh.get("avoidable_plus_suspect", 0) > 0:
        total = ffh["avoidable_plus_suspect"]
        n_fees = ffh.get("n_fees", 0)
        n_dups = ffh.get("n_duplicates", 0)
        return {
            "severity": "amber",
            "badge_text": "Fees Flagged",
            "amount": money_html_short(total),
            "subtitle": f"{n_fees} bank fee{'s' if n_fees != 1 else ''}, {n_dups} duplicate{'s' if n_dups != 1 else ''} &mdash; review recommended",
        }

    # 5. All clear
    balance = fh.get("start_balance", 0) if fc.get("available", False) else 0
    return {
        "severity": "green",
        "badge_text": "All Clear",
        "amount": money_html_short(balance),
        "subtitle": "no urgent issues detected this period",
    }


# ─────────────────────────── CSS ──────────────────────────────────────────────

_CSS = """\
  :root {
    /* YOUNG & VIBRANT — modern-fintech energy. Saturated, confident color used
       intentionally (not traffic-light defaults). Crisp near-white base, high
       contrast, bold display type. Token names kept so rules recolor in place. */
    --red: #F0473E;            /* warm alert red — risk/deficit (used sparingly) */
    --red-bg: #FEE4E2;
    --red-border: #FBBFBA;
    --red-deep: #D62F26;
    --amber: #F2B705;          /* the secondary region amarelo — gold/caution/highlight */
    --amber-bg: #FFF3CC;
    --amber-border: #FBE08A;
    --green: #009C53;          /* the secondary region green — good/saved, the lead color */
    --green-bg: #D4F4E2;
    --green-border: #8FE3B6;
    --green-deep: #00803F;
    --blue: #1E50C8;           /* the secondary region blue — info/accent */
    --blue-bg: #DEE7FB;
    --blue-border: #B4C6F4;
    --text-primary: #15171C;   /* crisp near-black ink */
    --text-secondary: #4A4E58;
    --text-tertiary: #767B86;
    --text-faint: #A4A9B3;
    --bg-white: #FFFFFF;
    --bg-gray-50: #F2F3F0;     /* sunken */
    --bg-gray-100: #FBFBF8;    /* clean bright page */
    --border: #E7E8E3;
    --border-light: #F0F1ED;
    --navy: #101A14;           /* deep green-black for masthead */
    --navy-light: #1C2C22;
    --green-bright: #00E676;
    --gold-pop: #FFC93C;       /* sunshine gold accent */
    --shadow-sm: 0 1px 2px rgba(16,26,20,0.06);
    --shadow-md: 0 6px 16px -4px rgba(16,26,20,0.12), 0 2px 4px -1px rgba(16,26,20,0.05);
    --shadow-lg: 0 18px 40px -12px rgba(0,184,92,0.18), 0 6px 12px -4px rgba(16,26,20,0.06);
    --mono: 'Spline Sans Mono', ui-monospace, 'SF Mono', Menlo, monospace;
    --sans: 'Inter', -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Segoe UI', system-ui, sans-serif;
    --serif: 'Bricolage Grotesque', 'Inter', system-ui, sans-serif;
    --radius: 12px;
    --radius-sm: 8px;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }

  body {
    font-family: var(--sans);
    background: var(--bg-gray-100);
    color: var(--text-primary);
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
    line-height: 1.5;
  }

  .container {
    max-width: 640px;
    margin: 24px auto;
    background: var(--bg-white);
    border-radius: 12px;
    overflow: hidden;
    box-shadow: 0 1px 3px rgba(0,0,0,0.06), 0 8px 24px rgba(0,0,0,0.06);
  }

  .email-portion { background: var(--bg-white); }

  .email-header {
    background: var(--navy);
    padding: 14px 20px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-top: 3px solid var(--red);
  }

  .email-header-left h1 {
    font-size: 16px; font-weight: 600; color: #FFFFFF; letter-spacing: -0.01em;
  }

  .email-header-left .date {
    font-size: 11px; color: #A0AEC0; margin-top: 1px; font-weight: 400;
  }

  .badge-mode {
    font-size: 10px; font-weight: 600; letter-spacing: 0.08em;
    text-transform: uppercase;
    background: rgba(255,255,255,0.12); color: #E2E8F0;
    padding: 3px 10px; border-radius: 20px;
    border: 1px solid rgba(255,255,255,0.15);
  }

  .hero-card {
    margin: 16px 16px 0;
    background: var(--bg-white);
    border-radius: var(--radius);
    padding: 16px 18px;
    box-shadow: var(--shadow-md);
    position: relative;
    overflow: hidden;
  }

  .hero-card::after {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0; bottom: 0;
    pointer-events: none;
  }

  .hero-card > * { position: relative; z-index: 1; }

  .hero-badge {
    display: inline-flex; align-items: center; gap: 5px;
    font-size: 10px; font-weight: 700; letter-spacing: 0.06em;
    text-transform: uppercase;
    padding: 3px 9px; border-radius: 4px; margin-bottom: 8px;
  }

  .hero-badge .dot {
    width: 6px; height: 6px; border-radius: 50%;
    display: inline-block;
    animation: pulse-dot 2s ease-in-out infinite;
  }

  @keyframes pulse-dot {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
  }

  .hero-amount {
    font-family: var(--mono);
    font-size: 36px; font-weight: 700;
    letter-spacing: -0.03em; line-height: 1.1;
  }

  .hero-subtitle {
    font-size: 13px; color: var(--text-secondary);
    margin-top: 4px; line-height: 1.4;
  }

  .hero-subtitle strong { color: var(--text-primary); font-weight: 600; }

  .vitals-strip {
    display: grid; grid-template-columns: repeat(4, 1fr);
    gap: 8px; padding: 12px 16px; margin-top: 4px;
  }

  .vital-card {
    background: var(--bg-gray-50);
    border: 1px solid var(--border-light);
    border-radius: var(--radius-sm);
    padding: 10px 10px 9px; text-align: center;
    transition: box-shadow 0.15s ease;
  }

  .vital-card:hover { box-shadow: var(--shadow-sm); }

  .vital-label {
    font-size: 9px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.06em; color: var(--text-tertiary); margin-bottom: 3px;
  }

  .vital-value {
    font-family: var(--mono); font-size: 14px; font-weight: 700;
    color: var(--text-primary); letter-spacing: -0.02em;
  }

  .vital-value.red { color: var(--red); }
  .vital-value.amber { color: var(--amber); }
  .vital-value.green { color: var(--green); }

  .vital-indicator {
    font-size: 9px; font-weight: 500; color: var(--text-tertiary); margin-top: 1px;
  }

  .vital-indicator.red { color: var(--red); }
  .vital-indicator.amber { color: var(--amber); }

  .month-summary { padding: 0 16px; margin-bottom: 4px; }

  .month-summary-card {
    background: var(--bg-gray-50);
    border: 1px solid var(--border-light);
    border-radius: var(--radius-sm);
    padding: 12px 14px;
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
  }

  .month-summary-label { font-size: 12px; font-weight: 600; color: var(--text-secondary); }

  .month-summary-numbers {
    display: flex; align-items: center; gap: 6px;
    font-family: var(--mono); font-size: 11px; font-weight: 600;
  }

  .month-summary-numbers .in { color: var(--green); }
  .month-summary-numbers .out { color: var(--red); }
  .month-summary-numbers .net { color: var(--red); font-weight: 700; }
  .month-summary-numbers .sep { color: var(--text-faint); }

  .summaries { padding: 0 16px 16px; }

  .summary-row {
    display: flex; align-items: flex-start; gap: 10px;
    padding: 9px 0;
    border-bottom: 1px solid var(--border-light);
    font-size: 12.5px; line-height: 1.45; color: var(--text-secondary);
  }

  .summary-row:last-child { border-bottom: none; }

  .summary-icon {
    flex-shrink: 0; width: 20px; height: 20px; border-radius: 5px;
    display: flex; align-items: center; justify-content: center;
  }

  .summary-icon svg { width: 12px; height: 12px; }

  .summary-icon.red { background: var(--red-bg); color: var(--red); }
  .summary-icon.amber { background: var(--amber-bg); color: var(--amber); }
  .summary-icon.green { background: var(--green-bg); color: var(--green); }
  .summary-icon.neutral { background: var(--bg-gray-100); color: var(--text-tertiary); }

  .summary-label {
    font-weight: 600; color: var(--text-primary);
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em;
  }

  .cta-wrap { padding: 4px 16px 20px; text-align: center; }

  .cta-button {
    display: inline-block; width: 100%; padding: 13px 24px;
    background: var(--navy); color: #FFFFFF;
    font-family: var(--sans); font-size: 13px; font-weight: 600;
    text-decoration: none; border-radius: var(--radius);
    letter-spacing: 0.01em; transition: all 0.2s ease;
    box-shadow: 0 1px 3px rgba(26,32,44,0.2), 0 4px 12px rgba(26,32,44,0.1);
  }

  .cta-button:hover {
    background: var(--navy-light);
    box-shadow: 0 2px 6px rgba(26,32,44,0.25), 0 8px 20px rgba(26,32,44,0.12);
    transform: translateY(-1px);
  }

  .divider-section {
    background: var(--bg-gray-100);
    padding: 32px 16px; text-align: center;
    border-top: 1px solid var(--border);
    border-bottom: 1px solid var(--border);
  }

  .divider-label {
    font-size: 10px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.12em; color: var(--text-tertiary);
  }

  .divider-line {
    width: 40px; height: 2px; background: var(--border);
    margin: 8px auto 0; border-radius: 1px;
  }

  .full-report { padding: 0 0 24px; }

  .report-header {
    padding: 28px 20px 22px;
    border-bottom: 1px solid var(--border);
    background: linear-gradient(180deg, var(--bg-gray-50) 0%, var(--bg-white) 100%);
  }

  .report-header h2 {
    font-size: 20px; font-weight: 700; color: var(--text-primary);
    letter-spacing: -0.02em;
  }

  .report-header .report-meta {
    font-size: 12px; color: var(--text-tertiary); margin-top: 4px;
  }

  .report-section { border-bottom: 1px solid var(--border-light); }
  .report-section:last-of-type { border-bottom: none; }
  .report-section details { width: 100%; }

  .report-section summary {
    display: flex; align-items: center; justify-content: space-between;
    padding: 16px 20px; cursor: pointer; list-style: none;
    user-select: none; transition: background 0.1s ease;
  }

  .report-section summary:hover { background: var(--bg-gray-50); }
  .report-section summary::-webkit-details-marker { display: none; }
  .report-section summary::marker { display: none; content: ''; }

  .section-title-group { display: flex; align-items: center; gap: 10px; }

  .section-number {
    font-size: 10px; font-weight: 600; color: var(--text-tertiary);
    font-family: var(--mono); width: 18px;
  }

  .section-title {
    font-size: 14px; font-weight: 600; color: var(--text-primary);
    letter-spacing: -0.01em;
  }

  .section-status {
    font-size: 10px; font-weight: 700; text-transform: uppercase;
    letter-spacing: 0.06em; padding: 3px 9px; border-radius: 4px; flex-shrink: 0;
  }

  .status-red { background: var(--red-bg); color: var(--red); border: 1px solid var(--red-border); }
  .status-amber { background: var(--amber-bg); color: var(--amber); border: 1px solid var(--amber-border); }
  .status-green { background: var(--green-bg); color: var(--green); border: 1px solid var(--green-border); }
  .status-neutral { background: var(--bg-gray-100); color: var(--text-tertiary); border: 1px solid var(--border); }

  .chevron {
    width: 16px; height: 16px; color: var(--text-tertiary);
    transition: transform 0.2s ease; flex-shrink: 0; margin-left: 8px;
  }

  details[open] .chevron { transform: rotate(90deg); }

  .section-body { padding: 0 20px 20px; }

  .big-number-card {
    background: var(--bg-gray-50);
    border: 1px solid var(--border-light);
    border-radius: var(--radius);
    padding: 20px; text-align: center; margin-bottom: 16px;
  }

  .big-number-card.red-tint { background: var(--red-bg); border-color: var(--red-border); }
  .big-number-card.amber-tint { background: var(--amber-bg); border-color: var(--amber-border); }

  .big-number-label {
    font-size: 10px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.08em; color: var(--text-tertiary); margin-bottom: 4px;
  }

  .big-number {
    font-family: var(--mono); font-size: 32px; font-weight: 700;
    letter-spacing: -0.03em; line-height: 1.15;
  }

  .big-number.red { color: var(--red); }
  .big-number.amber { color: var(--amber); }
  .big-number.green { color: var(--green); }

  .big-number-sub { font-size: 12px; color: var(--text-tertiary); margin-top: 3px; }

  .kv-table { width: 100%; margin-bottom: 16px; }

  .kv-row {
    display: flex; justify-content: space-between; align-items: center;
    padding: 8px 0; border-bottom: 1px solid var(--border-light); font-size: 13px;
  }

  .kv-row:last-child { border-bottom: none; }

  .kv-key { color: var(--text-tertiary); font-weight: 400; }

  .kv-val {
    font-family: var(--mono); font-weight: 600; font-size: 13px;
    color: var(--text-primary); text-align: right;
  }

  .kv-val.red { color: var(--red); }
  .kv-val.amber { color: var(--amber); }
  .kv-val.green { color: var(--green); }

  .data-table {
    width: 100%; border-collapse: collapse; font-size: 12px; margin-bottom: 16px;
  }

  .data-table thead { border-bottom: 2px solid var(--border); }

  .data-table th {
    padding: 8px 8px 8px 0; text-align: left;
    font-size: 9px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.08em; color: var(--text-tertiary);
  }

  .data-table th:last-child { text-align: right; padding-right: 0; }

  .data-table td {
    padding: 9px 8px 9px 0; border-bottom: 1px solid var(--border-light);
    vertical-align: top;
  }

  .data-table td:last-child {
    text-align: right; padding-right: 0;
    font-family: var(--mono); font-weight: 600; font-size: 12px; white-space: nowrap;
  }

  .data-table tr:last-child td { border-bottom: none; }
  .data-table .merchant { font-weight: 500; color: var(--text-primary); }

  .data-table .detail {
    font-size: 11px; color: var(--text-tertiary); display: block; margin-top: 1px;
  }

  .sub-label {
    font-size: 10px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.08em; color: var(--text-tertiary);
    margin-bottom: 8px; margin-top: 16px;
  }

  .sub-label:first-child { margin-top: 0; }

  .progress-wrap { margin-bottom: 16px; }

  .progress-labels {
    display: flex; justify-content: space-between; font-size: 11px; margin-bottom: 6px;
  }

  .progress-labels .label-left { color: var(--text-tertiary); font-weight: 500; }

  .progress-labels .label-right {
    font-family: var(--mono); font-weight: 600; font-size: 11px;
  }

  .progress-track {
    height: 8px; background: var(--bg-gray-100); border-radius: 4px;
    overflow: visible; position: relative; border: 1px solid var(--border-light);
  }

  .progress-fill { height: 100%; border-radius: 4px 0 0 4px; transition: width 0.3s ease; }





  .runrate-card {
    display: flex; align-items: center; gap: 12px;
    border-radius: var(--radius-sm); padding: 12px 14px; margin-bottom: 16px;
  }

  .runrate-label { font-size: 11px; color: var(--text-secondary); font-weight: 500; }

  .runrate-value {
    font-family: var(--mono); font-size: 20px; font-weight: 700;
    letter-spacing: -0.02em;
  }

  .inline-badge {
    display: inline-block; font-size: 9px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.06em;
    padding: 2px 6px; border-radius: 3px; vertical-align: middle; margin-left: 4px;
  }

  .inline-badge.red { background: var(--red-bg); color: var(--red); border: 1px solid var(--red-border); }
  .inline-badge.amber { background: var(--amber-bg); color: var(--amber); border: 1px solid var(--amber-border); }
  .inline-badge.green { background: var(--green-bg); color: var(--green); border: 1px solid var(--green-border); }
  .inline-badge.neutral { background: var(--bg-gray-100); color: var(--text-tertiary); border: 1px solid var(--border); }

  .note-box {
    background: var(--bg-gray-50); border: 1px solid var(--border-light);
    border-radius: var(--radius-sm); padding: 10px 13px;
    font-size: 12px; color: var(--text-tertiary); margin-bottom: 16px; line-height: 1.5;
  }

  .note-box.red-note { background: var(--red-bg); border-color: var(--red-border); color: var(--red-deep); }
  .note-box.amber-note { background: var(--amber-bg); border-color: var(--amber-border); color: var(--amber); }















  .streams-row {
    display: grid; grid-template-columns: 1fr 1fr;
    gap: 8px; margin-bottom: 16px;
  }

  .stream-card {
    background: var(--bg-gray-50); border: 1px solid var(--border-light);
    border-radius: var(--radius-sm); padding: 12px; text-align: center;
  }

  .stream-card-label {
    font-size: 9px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.06em; color: var(--text-tertiary); margin-bottom: 2px;
  }

  .stream-card-value { font-family: var(--mono); font-size: 16px; font-weight: 700; }

  .mom-chart { margin-bottom: 16px; }

  .mom-month-row { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }

  .mom-month-label {
    font-size: 11px; font-weight: 600; color: var(--text-secondary);
    width: 32px; flex-shrink: 0; text-align: right;
  }

  .mom-bars { flex: 1; display: flex; flex-direction: column; gap: 2px; }

  .mom-bar-track {
    height: 10px; background: var(--bg-gray-100); border-radius: 3px;
    overflow: hidden; position: relative;
  }

  .mom-bar-fill { height: 100%; border-radius: 3px; transition: width 0.4s ease; }
  .mom-bar-fill.income { background: linear-gradient(90deg, #C6F6D5, #68D391); }
  .mom-bar-fill.spend { background: linear-gradient(90deg, #FED7D7, #FC8181); }

  .mom-net {
    font-family: var(--mono); font-size: 11px; font-weight: 700;
    width: 65px; text-align: right; flex-shrink: 0;
  }

  .mom-legend {
    display: flex; gap: 16px; justify-content: center;
    margin-top: 12px; font-size: 10px; color: var(--text-tertiary); font-weight: 500;
  }

  .mom-legend-dot {
    display: inline-block; width: 8px; height: 8px; border-radius: 2px;
    margin-right: 4px; vertical-align: middle;
  }

  .mom-legend-dot.income-dot { background: #68D391; }
  .mom-legend-dot.spend-dot { background: #FC8181; }




































  .report-footer {
    padding: 20px; text-align: center;
    border-top: 1px solid var(--border); margin-top: 8px;
  }

  .footer-brand {
    font-size: 11px; font-weight: 600; color: var(--text-tertiary);
    letter-spacing: 0.02em;
  }

  .footer-meta {
    font-size: 10px; color: #A0AEC0; margin-top: 4px; line-height: 1.5;
  }

  details .section-body { animation: slideDown 0.2s ease; }

  @keyframes slideDown {
    from { opacity: 0; transform: translateY(-4px); }
    to { opacity: 1; transform: translateY(0); }
  }

  /* Unavailable card */
  .unavailable-card {
    border: 2px dashed var(--border);
    border-radius: var(--radius);
    padding: 28px 20px;
    text-align: center;
    margin-bottom: 16px;
  }

  .unavailable-card .link-icon { font-size: 28px; margin-bottom: 8px; }

  .unavailable-card .unavail-title {
    font-size: 14px; font-weight: 600; color: var(--text-primary);
    margin-bottom: 4px;
  }

  .unavailable-card .unavail-desc {
    font-size: 12px; color: var(--text-tertiary); line-height: 1.5;
  }

  @media (max-width: 420px) {
    .vitals-strip { grid-template-columns: repeat(2, 1fr); }
    .vital-value { font-size: 13px; }
    .hero-amount { font-size: 30px; }
    .stat-grid { grid-template-columns: repeat(3, 1fr); }
    .streams-row { grid-template-columns: 1fr; }
    .month-summary-card { flex-direction: column; align-items: flex-start; gap: 4px; }
  }

  /* ── regional-Claude layer: serif headings, green masthead accent ── */
  .email-header { border-top-color: var(--green); }
  .email-header-left h1,
  .report-header h2,
  .section-title,
  .big-number-label,
  .footer-brand { font-family: var(--serif); font-weight: 500; letter-spacing: -0.01em; }
  .report-header h2 { font-size: 21px; }

  /* ── Currency toggle (hosted report only) ── */
  .report-header-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
  .fx { display: flex; align-items: center; gap: 10px; flex-shrink: 0; }
  .fx-rate { font-size: 11px; color: var(--text-tertiary); font-variant-numeric: tabular-nums; white-space: nowrap; }
  .fx-seg { display: inline-flex; background: var(--bg-gray-50); border: 1px solid var(--green-border); border-radius: 999px; padding: 3px; }
  .fx-seg button { font: inherit; font-size: 12px; font-weight: 600; border: 0; background: transparent; color: var(--text-tertiary); padding: 5px 13px; border-radius: 999px; cursor: pointer; transition: all 0.15s ease; }
  .fx-seg button.is-active { background: var(--green); color: #fff; box-shadow: 0 1px 4px rgba(0,166,81,0.45); }

  /* ── PPP orientation notes (BRL view only; toggled by currency.js) ── */
  .ppp-note { margin: 10px 0 2px; padding: 9px 13px; background: var(--green-bg); border-left: 2.5px solid var(--green); border-radius: 0 8px 8px 0; font-size: 12px; color: var(--green-deep, #008542); line-height: 1.45; }
  .ppp-note [data-ppp-value] { font-family: var(--mono); font-weight: 700; }
  .ppp-note .src { color: var(--text-tertiary); font-size: 11px; }
  .ppp-caveat { margin-top: 10px; font-size: 11px; color: var(--text-tertiary); font-style: italic; }

  @media (max-width: 420px) {
    .report-header-row { flex-direction: column; gap: 12px; }
  }

  /* ════ CLAUDIFY: make the hosted report read like the homepage ════
     Airy cream paper, landing brandbar, soft section cards, serif, space.
     These come last so they win the cascade over the legacy email styles. */

  body { background: var(--paper, #FBFAF3); padding: 44px 20px 64px; line-height: 1.55; }

  /* Shell: drop the boxed white container — let content breathe on paper */
  .container {
    max-width: 620px; margin: 0 auto;
    background: transparent; border-radius: 0; overflow: visible; box-shadow: none;
  }

  /* Landing-matched brandbar */
  .brandbar { display: flex; align-items: center; gap: 12px; margin-bottom: 30px; }
  .brandbar .spark { flex-shrink: 0; }
  .brandbar-word { font-family: var(--serif); font-size: 19px; font-weight: 500; letter-spacing: -0.01em; color: var(--text-primary); }
  .brandbar-mono { font-family: var(--sans); font-size: 11px; font-weight: 500; color: var(--text-faint); margin-left: 8px; letter-spacing: 0.02em; }
  .brandbar-private {
    margin-left: auto; display: inline-flex; align-items: center; gap: 6px;
    font-size: 11px; font-weight: 500; color: var(--green-deep, #008542);
    background: var(--green-bg); border: 1px solid var(--green-border);
    padding: 4px 11px; border-radius: 999px;
  }

  /* Report header: eyebrow + big serif headline (mirrors the landing hero) */
  .report-header { padding: 0; border: 0; background: transparent; margin-bottom: 30px; }
  .report-eyebrow {
    font-size: 12px; font-weight: 600; color: var(--green);
    display: inline-flex; align-items: center; gap: 7px; margin-bottom: 12px;
  }
  .report-eyebrow::before { content: ''; width: 18px; height: 1.5px; background: var(--green); display: inline-block; }
  .report-header h2 { font-family: var(--serif); font-size: 38px; line-height: 1.08; font-weight: 500; letter-spacing: -0.02em; color: var(--text-primary); }
  .report-header h2 em { font-style: italic; color: var(--green-deep, #008542); }
  .report-header .report-meta { font-size: 13px; color: var(--text-tertiary); margin-top: 12px; }

  /* Sections become soft paper cards instead of accordion rows */
  .full-report { padding: 0; }
  .report-section {
    background: var(--surface, #FFFFFF);
    border: 1px solid var(--line, #E6E9E1);
    border-radius: 16px; margin-bottom: 16px; overflow: hidden;
    transition: box-shadow 0.18s ease, border-color 0.18s ease;
  }
  .report-section:last-of-type { border-bottom: 1px solid var(--line, #E6E9E1); }
  .report-section:hover { box-shadow: 0 12px 28px -14px rgba(20,36,26,0.16); border-color: #D7DCD2; }
  .report-section summary { padding: 20px 24px; }
  .report-section summary:hover { background: transparent; }
  .section-title { font-family: var(--serif); font-size: 18px; font-weight: 500; letter-spacing: -0.01em; }
  .section-number { color: var(--text-faint); }
  .section-body { padding: 0 24px 24px; }

  /* Status chips, big-number cards, kv rows — softer & airier */
  .section-status { border-radius: 999px; font-size: 9.5px; padding: 3px 10px; }
  .big-number-card { border-radius: 14px; padding: 26px 20px; margin-bottom: 20px; background: var(--bg-gray-50); border-color: var(--line, #E6E9E1); }
  .big-number { font-size: 36px; }
  .kv-table { margin-bottom: 8px; }
  .kv-row { padding: 11px 0; border-bottom-color: var(--line-soft, #EFF1EA); font-size: 13.5px; }
  .kv-key { color: var(--text-secondary); }

  /* Footer: match the landing (serif wordmark + flag rule) */
  .report-footer { text-align: left; padding: 26px 4px 0; margin-top: 30px; border-top: 1px solid var(--line, #E6E9E1); }
  .report-footer > div:first-child { display: none; }
  .footer-brand { font-family: var(--serif); font-size: 14px; color: var(--text-secondary); }
  .footer-meta { font-size: 11px; color: var(--text-faint); margin-top: 6px; }

  @media (max-width: 480px) {
    .report-header h2 { font-size: 30px; }
    body { padding: 28px 16px 48px; }
  }

  /* ── Month-by-month: hero takeaway, live trend, no table ── */
  .mom-hero { font-size: 16px; line-height: 1.5; color: var(--text-secondary); margin-bottom: 20px; }
  .mom-hero strong { font-weight: 600; color: var(--text-primary); }
  .mom-hero.neg strong { color: var(--red); }
  .mom-hero.pos strong { color: var(--green); }
  .mom-month-row.current .mom-month-label { font-weight: 700; color: var(--text-primary); }
  .mom-month-row.current { opacity: 1; }
  .mom-month-row:not(.current) { opacity: 0.82; }
  .mom-net { font-variant-numeric: tabular-nums; }
  /* Spending isn't "bad" — a warm neutral, not alarm red. Net carries the verdict. */
  .mom-bar-fill.income { background: var(--green); }
  .mom-bar-fill.spend { background: #CBB89D; }
  .mom-legend-dot.income-dot { background: var(--green); }
  .mom-legend-dot.spend-dot { background: #CBB89D; }
  .mom-verdict { margin-top: 16px; padding-top: 14px; border-top: 1px solid var(--line-soft, #EFF1EA); font-size: 12.5px; color: var(--text-tertiary); }

  /* Forward-plan callout (forecast runs on the plan, not past habits) */
  .plan-note { margin: 4px 0 18px; padding: 11px 14px; background: var(--green-bg); border-left: 2.5px solid var(--green); border-radius: 0 8px 8px 0; font-size: 12.5px; color: var(--text-secondary); line-height: 1.5; }
  .plan-note strong { color: var(--text-primary); font-weight: 600; }
  .freed-note { margin-top: 10px; font-size: 12px; color: var(--green-deep, #008542); }

  /* Where your money goes — split bar + ranked category bars + kind chips */
  .split-bar-label { display: flex; justify-content: space-between; font-size: 12px; color: var(--text-secondary); margin-bottom: 8px; }
  .split-bar-label .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 5px; vertical-align: middle; }
  .split-bar-label .dot.blue { background: var(--blue); }
  .split-bar-label .dot.gold { background: var(--amber); }
  .split-bar { display: flex; height: 10px; border-radius: 6px; overflow: hidden; margin-bottom: 24px; background: var(--bg-gray-50); }
  .split-seg.blue { background: var(--blue); }
  .split-seg.gold { background: var(--amber); }
  .cat-row { margin-bottom: 16px; }
  .cat-head { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 5px; gap: 10px; }
  .cat-name { font-size: 13.5px; font-weight: 500; color: var(--text-primary); }
  .cat-merch { font-size: 11px; color: var(--text-faint); font-weight: 400; }
  .cat-amt { font-family: var(--mono); font-size: 13px; font-weight: 600; color: var(--text-primary); white-space: nowrap; }
  .cat-pct { color: var(--text-faint); font-weight: 500; font-size: 11px; }
  .cat-bar-track { height: 8px; background: var(--bg-gray-50); border-radius: 5px; overflow: hidden; }
  .cat-bar-fill { height: 100%; border-radius: 5px; }
  .cat-bar-fill.blue { background: var(--blue); }
  .cat-bar-fill.gold { background: var(--amber); }
  .cat-bar-fill.leak { background: #E07A5F; }
  .cat-chip { display: inline-block; margin-top: 6px; font-size: 10px; font-weight: 600; padding: 1px 8px; border-radius: 999px; letter-spacing: 0.02em; }
  .cat-chip.blue { background: var(--blue-bg); color: var(--blue); }
  .cat-chip.gold { background: var(--amber-bg); color: var(--amber); }
  .cat-chip.leak { background: #FBEAE4; color: #C75B43; }
  .cat-footnote { margin-top: 10px; font-size: 11px; color: var(--text-faint); font-style: italic; }

  /* Balance trajectory — real curve with axes */
  .traj-wrap { margin: 6px 0 18px; }
  .traj-wrap svg { width: 100%; height: auto; display: block; font-family: var(--mono); }

  /* Distance-to-goal bar */
  .goalbar-wrap { margin: 10px 0 22px; }
  .goalbar-head { display: flex; justify-content: space-between; gap: 12px; font-size: 12.5px; color: var(--text-secondary); margin-bottom: 16px; }
  .goalbar-track { position: relative; height: 12px; background: var(--bg-gray-50); border-radius: 6px; }
  .goalbar-climb { position: absolute; top: 0; height: 100%; border-radius: 0 6px 6px 0;
    background: repeating-linear-gradient(90deg, transparent 0 6px, rgba(46,118,87,0.10) 6px 7px); }
  .goalbar-fill { position: absolute; top: 0; height: 100%; border-radius: 6px; }
  .goalbar-zero { position: absolute; top: -4px; height: 20px; width: 1.5px; background: var(--text-faint); transform: translateX(-50%); }
  .goalbar-proj { position: absolute; top: -2px; height: 16px; width: 0; border-left: 1.5px dashed var(--green); transform: translateX(-50%); opacity: 0.7; }
  .goalbar-you { position: absolute; top: 6px; transform: translateX(-50%); display: flex; flex-direction: column; align-items: center; }
  .goalbar-you-dot { width: 11px; height: 11px; border-radius: 50%; background: var(--text-primary); border: 2.5px solid var(--bg-white); box-shadow: 0 1px 3px rgba(0,0,0,0.18); margin-top: -11px; }
  .goalbar-you-label { font-size: 10px; font-weight: 600; color: var(--text-secondary); margin-top: 3px; }
  .goalbar-scale { position: relative; margin-top: 24px; height: 14px; font-size: 11px; color: var(--text-faint); font-variant-numeric: tabular-nums; }
  .goalbar-scale > span:first-child { position: absolute; left: 0; }
  .goalbar-scale > span:last-child { position: absolute; right: 0; }
  .goalbar-zerolabel { position: absolute; transform: translateX(-50%); }

  /* Young & vibrant type: bold Bricolage display, tight tracking, energetic. */
  .report-header h2 { font-weight: 700; letter-spacing: -0.03em; font-size: 40px; line-height: 1.02; }
  .report-header h2 em { font-style: normal; color: var(--green); }
  .report-eyebrow { color: var(--green); font-weight: 700; }
  .section-title, .card-title { font-weight: 700; letter-spacing: -0.02em; }
  .big-number-label, .vital-label, .sub-label { font-weight: 700; letter-spacing: 0.04em; }
  .brandbar-word { font-weight: 700; letter-spacing: -0.02em; }
  .mom-net, .kv-val, .cat-amt, .vital-value, .goalbar-head { font-variant-numeric: tabular-nums; }

  /* Drill-down: every aggregate opens to its atomic transactions */
  .drillable { cursor: pointer; position: relative; border-radius: 8px; transition: background 0.12s ease; outline: none; }
  .drillable:hover { background: var(--bg-gray-50); }
  .drillable:focus-visible { box-shadow: 0 0 0 2px var(--green-border); }
  .drill-hint { font-family: var(--sans); font-size: 10.5px; font-weight: 600; color: var(--text-faint); background: var(--bg-gray-50); border: 1px solid var(--border); border-radius: 999px; padding: 0 7px; margin-left: 8px; white-space: nowrap; transition: all 0.12s ease; vertical-align: middle; }
  .drillable:hover .drill-hint, .drill-open .drill-hint { color: var(--green); border-color: var(--green-border); background: var(--green-bg); }
  .cat-row.drillable { padding: 4px; margin: -4px -4px 12px; }
  .cat-row .drill-hint { position: absolute; top: 4px; right: 4px; }
  .drill-tip { font-size: 11.5px; color: var(--text-tertiary); margin: 0 0 16px; padding: 9px 13px; background: var(--bg-gray-50); border-radius: 8px; }
  .drill-panel { margin: 4px 0 14px; padding: 4px 0; border-left: 2px solid var(--green-border); animation: drillIn 0.16s ease; }
  @keyframes drillIn { from { opacity: 0; transform: translateY(-3px); } to { opacity: 1; transform: none; } }
  .drill-head { font-size: 11px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; color: var(--text-tertiary); padding: 4px 14px 8px; }
  .drill-row { display: flex; align-items: baseline; gap: 12px; padding: 5px 14px; font-size: 12.5px; }
  .drill-row:nth-child(even) { background: var(--bg-gray-50); }
  .drill-date { font-family: var(--mono); font-size: 11px; color: var(--text-faint); width: 38px; flex-shrink: 0; }
  .drill-merch { flex: 1; color: var(--text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .drill-amt { font-family: var(--mono); font-weight: 500; color: var(--text-primary); font-variant-numeric: tabular-nums; }
  .drill-amt.pos { color: var(--green); }
"""

_CHEVRON_SVG = '<svg class="chevron" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><path d="M6 4l4 4-4 4"/></svg>'

_SVG_ICONS = {
    "forecast": '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 12L8 4l6 8H2z"/></svg>',
    "budget": '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8" r="6"/><path d="M8 5v4M6 9h4"/></svg>',
    "fee_fraud": '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8" r="6"/><path d="M8 5v3M8 10.5v.5"/></svg>',
    "receipts": '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="2" width="10" height="12" rx="1"/><path d="M6 5h4M6 8h4M6 11h2"/></svg>',
}


# ─────────────────────────── section builders ─────────────────────────────────

def _unavailable_card(section_name, description):
    """Render a dashed-border 'Connect your bank' placeholder."""
    return f"""\
          <div class="unavailable-card">
            <div class="link-icon">&#128279;</div>
            <div class="unavail-title">Connect your bank to enable {_esc(section_name)}</div>
            <div class="unavail-desc">{_esc(description)}</div>
          </div>"""


def _build_balance_trajectory_svg(digest):
    """Real day-by-day balance trajectory: the actual project() curve (paydays,
    obligation cliffs) plotted against the $0 line and the alert buffer, with a
    dated x-axis and $ y-labels. Colored by zone (green safe / gold below buffer
    / red overdraft)."""
    fc = digest.get("sections", {}).get("forecast", {})
    fh = fc.get("headline", {})
    curve = fc.get("detail", {}).get("curve", [])
    buffer = fh.get("buffer", 100) or 0
    horizon = fh.get("horizon_days", 35)

    if len(curve) < 2:   # graceful fallback
        sb = fh.get("start_balance", 0)
        eb = fh.get("projected_end_balance", 0)
        md = fh.get("min_date", "")
        curve = [{"date": str(as_of_safe(digest)), "balance": sb},
                 {"date": md, "balance": eb}]

    bals = [c["balance"] for c in curve]
    vmax = max(bals + [buffer, 0])
    vmin = min(bals + [0])
    pad = (vmax - vmin) * 0.12 or 1.0
    vmax += pad
    vmin -= pad
    span = (vmax - vmin) or 1.0

    W, H = 600, 170
    L, R, T, Bm = 54, 590, 14, 134
    n = len(curve)
    def X(i): return L + (R - L) * (i / (n - 1))
    def Y(v): return T + (Bm - T) * (1 - (v - vmin) / span)

    GREEN, AMBER, RED, GREY = "#00A651", "#E0A500", "#E04A2F", "#D8D2C2"
    def zone(b): return GREEN if b >= buffer else (AMBER if b >= 0 else RED)

    y0, yb = Y(0), Y(buffer)
    parts = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg">']
    # $0 reference line + label
    parts.append(f'<line x1="{L}" y1="{y0:.1f}" x2="{R}" y2="{y0:.1f}" stroke="{GREY}" stroke-width="1"/>')
    parts.append(f'<text x="{L-7}" y="{y0+3:.1f}" font-size="9" fill="#9AA29A" text-anchor="end">$0</text>')
    # buffer line (dashed gold) if visibly distinct from $0
    if buffer > 0 and abs(yb - y0) > 7:
        parts.append(f'<line x1="{L}" y1="{yb:.1f}" x2="{R}" y2="{yb:.1f}" stroke="{AMBER}" stroke-width="1" stroke-dasharray="3,3" opacity="0.65"/>')
        parts.append(f'<text x="{L-7}" y="{yb+3:.1f}" font-size="9" fill="{AMBER}" text-anchor="end">{money(buffer)}</text>')
    # peak label
    parts.append(f'<text x="{L-7}" y="{Y(max(bals))+3:.1f}" font-size="9" fill="#9AA29A" text-anchor="end">{money(max(bals))}</text>')
    # zone-colored segments
    for i in range(n - 1):
        c = zone(min(curve[i]["balance"], curve[i+1]["balance"]))
        parts.append(f'<line x1="{X(i):.1f}" y1="{Y(curve[i]["balance"]):.1f}" x2="{X(i+1):.1f}" y2="{Y(curve[i+1]["balance"]):.1f}" stroke="{c}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>')
    # endpoints
    parts.append(f'<circle cx="{X(0):.1f}" cy="{Y(bals[0]):.1f}" r="3.5" fill="{zone(bals[0])}"/>')
    parts.append(f'<circle cx="{X(n-1):.1f}" cy="{Y(bals[-1]):.1f}" r="3.5" fill="{zone(bals[-1])}"/>')
    # dated x ticks (first / mid / last)
    for i, anchor in ((0, "start"), (n // 2, "middle"), (n - 1, "end")):
        parts.append(f'<text x="{X(i):.1f}" y="{Bm+18}" font-size="9" fill="#9AA29A" text-anchor="{anchor}">{_esc(_format_date_short(curve[i].get("date","")))}</text>')
    parts.append('</svg>')
    svg = "\n              ".join(parts)
    return f"""\
          <div class="sub-label">Balance trajectory ({horizon}d) &mdash; vs your {money(buffer)} buffer</div>
          <div class="traj-wrap">
              {svg}
          </div>"""


def as_of_safe(digest):
    return digest.get("sections", {}).get("forecast", {}).get("as_of", "")

def _section_balance_change(digest, section_num="00"):
    """What moved your balance since the last report: prior -> current, the delta,
    and every transaction in between (so a balance change is always explainable)."""
    bc = digest.get("balance_change")
    if not bc or not bc.get("txns"):
        return ""
    delta = bc.get("delta", 0)
    dcolor = "var(--green)" if delta >= 0 else "var(--red)"
    rows = ""
    for t in bc["txns"]:
        a = t.get("amount", 0)
        acolor = "var(--green)" if a >= 0 else "var(--text-primary)"
        rows += f"""
              <tr>
                <td>{_esc(_format_date_short(t.get('date', '')))}</td>
                <td><span class="merchant">{_esc(t.get('merchant', '?'))}</span></td>
                <td style="color:{acolor};">{money_html(a)}</td>
              </tr>"""
    body = f"""\
          <div class="big-number-card">
            <div class="big-number-label">What moved your balance</div>
            <div class="big-number">{money_html(bc.get('prior_balance', 0))} &rarr; {money_html(bc.get('current_balance', 0))}</div>
            <div class="big-number-sub" style="color:{dcolor};">{money_html(delta)} since {_format_date_long(bc.get('prior_date', ''))}</div>
          </div>
          <div class="sub-label">Transactions synced since last update &middot; net {money_html(bc.get('net', 0))}</div>
          <table class="data-table">
            <thead><tr><th>Date</th><th>Merchant</th><th>Amount</th></tr></thead>
            <tbody>{rows}
            </tbody>
          </table>
"""
    if not bc.get("reconciles", True):
        body += ('          <div class="big-number-sub" style="margin-top:8px;color:var(--text-tertiary);">'
                 'Newly synced rows — the live balance also reflects pending / just-posted '
                 'activity not yet itemized (the bank feed lags ~1–2 days).</div>\n')
    return _report_section(section_num, "Balance Change", "status-neutral",
                           money(delta), False, body)


def _section_forecast(digest, section_num="01"):
    """Render the Cash-flow Forecast section."""
    fc = digest.get("sections", {}).get("forecast", {})
    fh = fc.get("headline", {})

    if not fc.get("available", False):
        return _report_section(
            section_num, "Cash-flow Forecast", "status-neutral", "N/A", False,
            _unavailable_card(
                "Cash-flow Forecast",
                "Real-time balance tracking, overdraft alerts, and upcoming obligation previews."
            )
        )

    # Status badge
    if fh.get("overdraft"):
        status_class, status_text = "status-red", "Overdraft"
        tint_class, num_class = "red-tint", "red"
    elif fh.get("low_balance"):
        status_class, status_text = "status-amber", "Low Balance"
        tint_class, num_class = "amber-tint", "amber"
    else:
        status_class, status_text = "status-green", "Clear"
        tint_class, num_class = "", "green"

    is_open = section_num in ("01", "02")

    min_bal = fh.get("min_balance", 0)
    min_date = fh.get("min_date", "")
    _format_date_long(min_date)
    start_bal = fh.get("start_balance", 0)
    end_bal = fh.get("projected_end_balance", 0)
    daily_burn = fh.get("daily_burn", 0)
    horizon = fh.get("horizon_days", 0)
    safe_by = fh.get("safe_by")
    next_income = fh.get("next_income")
    budget_driven = fh.get("budget_driven", False)
    disc_monthly = fh.get("discretionary_monthly")
    obl_floor = fh.get("obligation_floor_monthly")
    fh.get("historical_discretionary_monthly")
    fh.get("tracks") or {}

    # Hero: lead with the ACTUAL balance you have right now (the projected LOW
    # read like a current balance and confused people). Plain-language forecast.
    low_short = _format_date_short(min_date)
    if fh.get("overdraft"):
        sub = (f'Heads up — projected to dip below $0 around {low_short}. '
               f'Move money in by {_format_date_short(safe_by)}.')
    else:
        sub = (f'Dips to {money_html(min_bal)} on {low_short} before your next paycheck, '
               f'then climbs to {money_html(end_bal)} over the next {horizon} days.')
    body = f"""\
          <div class="big-number-card {tint_class}">
            <div class="big-number-label">Balance today</div>
            <div class="big-number {num_class}">{money_html(start_bal)}</div>
            <div class="big-number-sub">{sub}</div>
          </div>"""

    if budget_driven and obl_floor is not None:
        body += f"""
          <div class="plan-note">
            Based on your plan: <strong>{money_html(obl_floor)}/mo</strong> in bills &amp; subscriptions
            and a <strong>{money_html(disc_monthly)}/mo</strong> spending budget.
          </div>"""

    body += f"""
{_build_balance_trajectory_svg(digest)}

          <div class="kv-table">
            <div class="kv-row">
              <span class="kv-key">Balance in {horizon} days</span>
              <span class="kv-val{' red' if end_bal < 0 else ''}">{money_html(end_bal)}</span>
            </div>"""

    if budget_driven and obl_floor is not None:
        body += f"""
            <div class="kv-row">
              <span class="kv-key">Bills &amp; subscriptions</span>
              <span class="kv-val">{money_html(obl_floor)}/mo</span>
            </div>
            <div class="kv-row">
              <span class="kv-key">Spending budget</span>
              <span class="kv-val green">{money_html(disc_monthly)}/mo</span>
            </div>"""
    else:
        body += f"""
            <div class="kv-row">
              <span class="kv-key">Daily spending</span>
              <span class="kv-val{' red' if daily_burn > 0 else ''}">{money_html(daily_burn)}/day</span>
            </div>"""

    if next_income:
        body += f"""
            <div class="kv-row">
              <span class="kv-key">Next paycheck</span>
              <span class="kv-val green">{money_html(next_income['amount'])} on {_format_date_short(next_income['date'])}</span>
            </div>"""

    body += "\n          </div>"

    # Obligations table
    obligations = fc.get("detail", {}).get("biggest_obligations", [])
    if obligations:
        body += """

          <div class="sub-label">Coming up next</div>
          <table class="data-table">
            <thead>
              <tr>
                <th>When</th>
                <th>What</th>
                <th>Amount</th>
              </tr>
            </thead>
            <tbody>"""
        for o in obligations:
            amt = o.get("amount", 0)
            amt_style = ' style="color:var(--red);"' if amt >= 1000 else ""
            body += f"""
              <tr>
                <td>{_esc(_format_date_short(o.get('date', '')))}</td>
                <td><span class="merchant">{_esc(o.get('merchant', '?')[:36])}</span></td>
                <td{amt_style}>{money_html(amt)}</td>
              </tr>"""
        body += """
            </tbody>
          </table>"""

    # Pending receipts (from reconciliation)
    pending = fc.get("detail", {}).get("pending_receipts", [])
    if pending:
        body += """

          <div class="sub-label">Receipts not yet posted to your bank</div>
          <table class="data-table">
            <thead>
              <tr>
                <th>When</th>
                <th>What</th>
                <th>Amount</th>
              </tr>
            </thead>
            <tbody>"""
        for p in pending:
            body += f"""
              <tr>
                <td>{_esc(_format_date_short(p.get('date', '')))}<span class="detail">{p.get('days_since', '?')}d ago</span></td>
                <td><span class="merchant">{_esc(p.get('merchant', '?')[:36])}</span></td>
                <td style="color:var(--amber);">{money_html(p.get('amount', 0))}</td>
              </tr>"""
        body += """
            </tbody>
          </table>"""

    return _report_section(section_num, "Balance forecast", status_class, status_text, is_open, body)


def _section_goal_pace(digest, section_num="02"):
    """Render the Savings Pace / Goal Tracker section."""
    bud = digest.get("sections", {}).get("budget", {})
    bh = bud.get("headline", {})

    title = "Savings Pace"

    if bud.get("available") is False:
        return _report_section(
            section_num, title, "status-neutral", "N/A", False,
            _unavailable_card(
                title,
                "Track your savings progress toward your target, with pace projections and spending rule enforcement."
            )
        )

    status = bh.get("status", "saving")
    if status == "behind":
        status_class, status_text = "status-red", "Not saving"
    elif status == "ahead":
        status_class, status_text = "status-green", "On track"
    elif status == "saving":
        status_class, status_text = "status-green", "Saving"
    else:
        status_class, status_text = "status-neutral", "On track"

    is_open = section_num in ("01", "02", "03", "04")

    target = bh.get("target", 25000)
    running_total = bh.get("running_total", 0)
    pct = bh.get("pct_to_target", 0)
    current_pace = bh.get("current_pace_mo", 0)
    required_pace = bh.get("required_pace_mo", 0)
    projected = bh.get("projected", 0)
    bh.get("gap", 0)
    months_remaining = bh.get("months_remaining", 0)
    move_date = bh.get("move_date", "")
    bh.get("income_window", 0)
    bh.get("spend_window", 0)
    bh.get("net_saved_window", 0)

    pct_color = "var(--red)" if pct < 0 else ("var(--green)" if pct >= 100 else "var(--text-primary)")

    body = ""

    # Distance-to-goal bar: one honest axis from a data-derived floor through $0
    # to the target. Green = saved past $0; red = the hole below $0; faint track =
    # the climb still ahead. No hardcoded zones, no /50000 peg, no magic offsets.
    axis_min = min(0.0, running_total, projected)
    # round the floor down to a clean $1k and give a little headroom
    import math as _math
    axis_min = _math.floor((axis_min * 1.1) / 1000) * 1000 if axis_min < 0 else 0
    axis_span = (target - axis_min) or 1
    def _pos(v):
        return max(0.0, min(100.0, (v - axis_min) / axis_span * 100))
    zero_x = _pos(0)
    you_x = _pos(running_total)
    proj_x = _pos(projected)
    saved = running_total >= 0
    fill_left = min(zero_x, you_x)
    fill_w = abs(you_x - zero_x)
    fill_color = "var(--green)" if saved else "var(--red)"
    body += f"""\
          <div class="goalbar-wrap">
            <div class="goalbar-head">
              <span>Toward your {money_html(target)} goal</span>
              <span style="color:{pct_color};">{money_html(running_total)} saved &middot; {money_html(round(target - running_total, 2))} to go</span>
            </div>
            <div class="goalbar-track">
              <div class="goalbar-climb" style="left:{zero_x:.1f}%; width:{max(0, 100 - zero_x):.1f}%;"></div>
              <div class="goalbar-fill" style="left:{fill_left:.1f}%; width:{max(1.2, fill_w):.1f}%; background:{fill_color};"></div>
              <div class="goalbar-zero" style="left:{zero_x:.1f}%;"></div>
              <div class="goalbar-you" style="left:{you_x:.1f}%;"><span class="goalbar-you-dot"></span><span class="goalbar-you-label">You</span></div>
              <div class="goalbar-proj" style="left:{proj_x:.1f}%;" title="Projected by move date"></div>
            </div>
            <div class="goalbar-scale">
              <span>{money_html(axis_min)}</span>
              {'<span class="goalbar-zerolabel" style="left:' + format(zero_x, '.1f') + '%;">$0</span>' if 12 < zero_x < 88 else ''}
              <span>{money_html(target)}</span>
            </div>
          </div>
"""

    # Plain takeaway: lead with the POSITIVE lean-budget savings rate, show the math.
    income_mo = bh.get("monthly_income", 0)
    obl_mo = bh.get("obligation_floor", 0)
    budget_mo = bh.get("discretionary_budget", 0)
    projected = bh.get("projected", 0)
    sign = "+" if current_pace >= 0 else ""
    num_cls = "green" if current_pace >= 0 else "red"
    body += f"""\
          <div class="big-number-card">
            <div class="big-number-label">You're saving &middot; lean budget</div>
            <div class="big-number {num_cls}">{sign}{money_html_short(current_pace)}/mo</div>
            <div class="big-number-sub">{money_html(income_mo)} income &minus; {money_html(obl_mo)} bills &minus; {money_html(budget_mo)} budget &middot; on pace for ~{money_html(projected)} by {_format_date_short(move_date)}</div>
          </div>
          <div class="ppp-note" data-ppp data-ppp-usd="{target:.2f}">
            In the secondary region, {money_html(target)} lives like <span data-ppp-value></span> of US spending power.
            <span class="src">World Bank PPP</span>
          </div>
"""

    body += f"""\
          <div class="kv-table">
            <div class="kv-row">
              <span class="kv-key">You're saving now</span>
              <span class="kv-val{' red' if current_pace < 0 else ' green'}">{money_html(current_pace)}/mo</span>
            </div>
            <div class="kv-row">
              <span class="kv-key">Need to save</span>
              <span class="kv-val green">{money_html(required_pace)}/mo</span>
            </div>
            <div class="kv-row">
              <span class="kv-key">Time left</span>
              <span class="kv-val">{months_remaining} months</span>
            </div>
          </div>
          <div class="ppp-caveat" data-ppp>
            PPP reflects rent, food &amp; services &mdash; not electronics or imports, which cost the same or more.
          </div>
"""

    # Tailwind: obligations that end before the move free up savings
    for f in bh.get("freed_obligations", []):
        body += f"""\
          <div class="freed-note">
            &uarr; <strong>{money_html(f.get('freed_total', 0))}</strong> banked toward the goal when
            {_esc(f.get('name', ''))} ends ({_esc(str(f.get('ends', '')))}) &mdash; frees {money_html(f.get('monthly', 0))}/mo.
          </div>
"""

    # Rule tally note box
    rt = bud.get("rule_tally", {})
    on_track = rt.get("on_track", 0)
    drifting = rt.get("drifting", 0)
    slipped = rt.get("slipped", 0)
    bud.get("detail", {}).get("off_track_rules", [])

    if on_track == 0 and drifting == 0 and slipped == 0:
        body += """\
          <div class="note-box">
            No cut rules scored this period.
          </div>"""
    else:
        body += f"""\
          <div class="note-box">
            Cut rules: {on_track} on track, {drifting} drifting, {slipped} slipped.
          </div>"""

    return _report_section(section_num, title, status_class, status_text, is_open, body)


def _section_fee_fraud(digest, section_num="03"):
    """Render the Fee + Fraud Scan section."""
    fee = digest.get("sections", {}).get("fee_fraud", {})
    fh = fee.get("headline", {})

    if fee.get("available") is False:
        return _report_section(
            section_num, "Fee + Fraud Scan", "status-neutral", "N/A", False,
            _unavailable_card(
                "Fee + Fraud Scan",
                "Detect bank fees, duplicate charges, and suspicious merchant activity in your transactions."
            )
        )

    recoverable = fh.get("avoidable", 0)
    n_fees = fh.get("n_fees", 0)
    n_dups = fh.get("n_duplicates", 0)
    n_anom = fh.get("n_anomalies", 0)
    rec_fee_annual = fh.get("recurring_fee_annual", 0)
    flagged = recoverable > 0 or n_anom > 0 or rec_fee_annual > 0
    if flagged:
        status_class = "status-amber"
        status_text = money(recoverable) if recoverable > 0 else f"{n_anom} flagged"
        tint_class = "amber-tint"
        num_class = "amber"
    else:
        status_class = "status-green"
        status_text = "Clear"
        tint_class = ""
        num_class = "green"

    sub_bits = []
    if n_fees:
        sub_bits.append(f"{n_fees} bank fee{'s' if n_fees != 1 else ''}")
    if n_dups:
        sub_bits.append(f"{n_dups} to verify")
    if n_anom:
        sub_bits.append(f"{n_anom} anomal{'ies' if n_anom != 1 else 'y'}")
    sub = " &middot; ".join(sub_bits) or "nothing flagged"

    body = f"""\
          <div class="big-number-card {tint_class}">
            <div class="big-number-label">Recoverable</div>
            <div class="big-number {num_class}">{money_html(recoverable)}</div>
            <div class="big-number-sub">{sub}</div>
          </div>
"""

    # Bank fees table
    fees = fee.get("detail", {}).get("fees", [])
    if fees:
        body += """\
          <div class="sub-label">Bank Fees</div>
          <table class="data-table">
            <thead>
              <tr>
                <th>Date</th>
                <th>Description</th>
                <th>Amount</th>
              </tr>
            </thead>
            <tbody>"""
        for f in fees:
            body += f"""
              <tr>
                <td>{_esc(f.get('date', '?'))}</td>
                <td>
                  <span class="merchant">{_esc(f.get('description', f.get('merchant', '?')))}</span>
                  <span class="detail">{_esc(f.get('merchant', ''))}</span>
                </td>
                <td style="color:var(--amber);">{money_html(f.get('amount', 0))}</td>
              </tr>"""
        body += """
            </tbody>
          </table>
"""

    # Anomalies — every row shows WHY it's flagged (the annotation principle)
    anomalies = fee.get("detail", {}).get("anomalies", [])
    if anomalies:
        body += """\
          <div class="sub-label">Anomalies &mdash; what changed</div>
          <table class="data-table">
            <thead>
              <tr>
                <th>Merchant</th>
                <th>Why flagged</th>
                <th>Impact</th>
              </tr>
            </thead>
            <tbody>"""
        for a in anomalies:
            body += f"""
              <tr>
                <td>
                  <span class="merchant">{_esc(a.get('merchant', '?'))}</span>
                  <span class="detail">{_esc(a.get('kind', ''))}</span>
                </td>
                <td><span class="detail">{_esc(a.get('reason', ''))}</span></td>
                <td style="color:var(--amber);">+{money_html(a.get('amount', 0))}</td>
              </tr>"""
        body += """
            </tbody>
          </table>
"""

    # Recurring fees — annualized, with active/stopped annotation
    rfees = fee.get("detail", {}).get("recurring_fees", [])
    if rfees:
        body += """\
          <div class="sub-label">Recurring fees</div>
          <table class="data-table">
            <thead>
              <tr>
                <th>Fee</th>
                <th>Annualized</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>"""
        for rf in rfees:
            cad = rf.get("cadence")
            cad = cad[0] if isinstance(cad, (list, tuple)) and cad else cad
            status = ("active &mdash; avoidable" if rf.get("active")
                      else f"stopped {_esc(_format_date_long(rf.get('last', '')))}")
            body += f"""
              <tr>
                <td>
                  <span class="merchant">{_esc(rf.get('merchant', '?'))}</span>
                  <span class="detail">{_esc(str(cad))} &middot; {rf.get('n', 0)}&times; &middot; {money_html(rf.get('typical', 0))}/ea</span>
                </td>
                <td>{money_html(rf.get('annual', 0))}/yr</td>
                <td><span class="detail">{status}</span></td>
              </tr>"""
        body += """
            </tbody>
          </table>
"""

    # Duplicates table
    dups = fee.get("detail", {}).get("duplicates", [])
    if dups:
        body += """\
          <div class="sub-label">Possible duplicates &mdash; verify (no merchant ref to confirm)</div>
          <table class="data-table">
            <thead>
              <tr>
                <th>Date</th>
                <th>Description</th>
                <th>Amount</th>
              </tr>
            </thead>
            <tbody>"""
        for d in dups:
            dates = d.get("dates", [])
            date_str = dates[0] if dates else "?"
            body += f"""
              <tr>
                <td>{_esc(date_str)}</td>
                <td>
                  <span class="merchant">Duplicate Charge</span>
                  <span class="detail">{_esc(d.get('merchant', '?'))} &mdash; {_esc(d.get('description', 'double-billed'))}</span>
                </td>
                <td style="color:var(--amber);">{money_html(d.get('amount', 0))}</td>
              </tr>"""
        body += """
            </tbody>
          </table>
"""

    # Receipt discrepancies (from reconciliation)
    receipt_discs = fee.get("detail", {}).get("receipt_discrepancies", [])
    if receipt_discs:
        body += """\
          <div class="sub-label">Receipt Discrepancies</div>
          <table class="data-table">
            <thead>
              <tr>
                <th>Merchant</th>
                <th>Date</th>
                <th>Difference</th>
              </tr>
            </thead>
            <tbody>"""
        for rd in receipt_discs:
            body += f"""
              <tr>
                <td>
                  <span class="merchant">{_esc(rd.get('merchant', '?'))}</span>
                  <span class="detail">Receipt {money_html(rd.get('receipt_amount', 0))} vs bank {money_html(rd.get('bank_amount', 0))}</span>
                </td>
                <td>{_esc(rd.get('date', '?'))}</td>
                <td style="color:var(--amber);">{money_html(rd.get('difference', 0))}</td>
              </tr>"""
        body += """
            </tbody>
          </table>
"""

    # NOTE: "unverified charges (no receipt on file)" intentionally NOT shown here.
    # Lacking an email receipt is the norm, not fraud — it belongs in reconciliation
    # as a coverage metric, not in the fee/fraud section (it was 100% of the old noise).

    return _report_section(section_num, "Fee + Fraud Scan", status_class, status_text, False, body)


def _section_recurring(digest, section_num="04"):
    """Render the Recurring Snapshot section."""
    rcur = digest.get("sections", {}).get("recurring", {})
    rh = rcur.get("headline", {})

    if rcur.get("available") is False:
        return _report_section(
            section_num, "Recurring Snapshot", "status-neutral", "N/A", False,
            _unavailable_card(
                "Recurring Snapshot",
                "See your active recurring income and expenses with monthly run-rate calculations."
            )
        )

    net = rh.get("net_monthly_runrate", 0)
    net_color = "var(--red)" if net < 0 else "var(--green)"
    bg_color = "var(--red-bg)" if net < 0 else "var(--green-bg)"
    border_color = "var(--red-border)" if net < 0 else "var(--green-border)"
    status_class = "status-red" if net < 0 else "status-green"
    status_text = f"{_money_short(net)}/mo"

    n_in = rh.get("n_active_inflow", 0)
    n_out = rh.get("n_active_outflow", 0)

    body = f"""\
          <div class="runrate-card" style="background:{bg_color}; border-color:{border_color};">
            <div>
              <div class="runrate-label">Net Monthly Run-Rate</div>
              <div class="runrate-value" style="color:{net_color};">{_money_short(net)}/mo</div>
            </div>
          </div>

          <div class="streams-row">
            <div class="stream-card">
              <div class="stream-card-label">Inflows</div>
              <div class="stream-card-value" style="color:{'var(--text-tertiary)' if n_in == 0 else 'var(--green)'};">{n_in} stream{'s' if n_in != 1 else ''}</div>
            </div>
            <div class="stream-card">
              <div class="stream-card-label">Outflows</div>
              <div class="stream-card-value" style="color:{'var(--text-tertiary)' if n_out == 0 else 'var(--red)'};">{n_out} stream{'s' if n_out != 1 else ''}</div>
            </div>
          </div>
"""

    # Outflows table
    top_out = rcur.get("detail", {}).get("top_outflow", [])
    if top_out:
        body += """\
          <div class="sub-label">Recurring Outflows</div>
          <table class="data-table">
            <thead>
              <tr>
                <th>Merchant</th>
                <th>Cadence</th>
                <th>~$/mo</th>
              </tr>
            </thead>
            <tbody>"""
        for s in top_out[:5]:
            mr = s.get("monthly_runrate", 0)
            amt_style = ' style="color:var(--red);"' if mr >= 1000 else ""
            body += f"""
              <tr>
                <td><span class="merchant">{_esc(s.get('merchant', '?')[:30])}</span></td>
                <td>{_esc(s.get('cadence', '?'))}</td>
                <td{amt_style}>{money_html(mr)}</td>
              </tr>"""
        body += """
            </tbody>
          </table>
"""

    # Price changes (from receipt reconciliation)
    price_changes = rcur.get("detail", {}).get("price_changes", [])
    if price_changes:
        body += """\
          <div class="sub-label">Price Changes (from receipts)</div>
          <table class="data-table">
            <thead>
              <tr>
                <th>Merchant</th>
                <th>Date</th>
                <th>Change</th>
              </tr>
            </thead>
            <tbody>"""
        for pc in price_changes:
            change = pc.get("change", 0)
            change_color = "var(--red)" if change > 0 else "var(--green)"
            change_sign = "+" if change > 0 else ""
            body += f"""
              <tr>
                <td>
                  <span class="merchant">{_esc(pc.get('merchant', '?'))}</span>
                  <span class="detail">{money_html(pc.get('previous', 0))} &rarr; {money_html(pc.get('current', 0))}</span>
                </td>
                <td>{_esc(pc.get('date', '?'))}</td>
                <td style="color:{change_color};">{change_sign}{money_html(change)}</td>
              </tr>"""
        body += """
            </tbody>
          </table>
"""

    return _report_section(section_num, "Recurring Snapshot", status_class, status_text, False, body)


def _section_mom(digest, section_num="01"):
    """Render the Month-over-Month section (monthly only).

    Uses the budget section's mom_history if available, otherwise generates
    a placeholder from the current month's data.
    """
    bud = digest.get("sections", {}).get("budget", {})
    bh = bud.get("headline", {})
    mom = bud.get("detail", {}).get("mom_history", [])

    # If no mom_history, build a single-row from current data
    if not mom:
        income = bh.get("income_window", 0)
        spend = bh.get("spend_window", 0)
        net = bh.get("net_saved_window", 0)
        # Try to get month name from the window
        window = digest.get("window", {})
        try:
            d = dt.date.fromisoformat(window.get("start", ""))
            month_name = d.strftime("%B")
            month_short = d.strftime("%b")
        except (ValueError, TypeError):
            month_name = "This Month"
            month_short = "Now"
        mom = [{"month": month_name, "month_short": month_short,
                "income": income, "spend": spend, "net": net}]

    # Determine max value for bar scaling
    max_val = max(
        max((m.get("income", 0) for m in mom), default=1),
        max((m.get("spend", 0) for m in mom), default=1),
        1
    )
    scale = max_val * 1.05  # add 5% headroom

    last = mom[-1]
    ln = last.get("net", 0)
    lm = last.get("month", "This month")
    partial = last.get("partial", False)
    when = f"So far in {lm}" if partial else f"In {lm}"

    status_class = "status-green" if ln >= 0 else "status-red"
    status_text = "Surplus" if ln > 0 else ("Even" if ln == 0 else "Deficit")

    # ── Hero takeaway: lead with the human answer, not a grid ──
    if ln < 0:
        hero = f'{when}, you spent <strong>{money_html(abs(ln))} more than you earned</strong>.'
        hero_cls = "mom-hero neg"
    elif ln > 0:
        hero = f'{when}, you kept <strong>{money_html(ln)}</strong> — more came in than went out.'
        hero_cls = "mom-hero pos"
    else:
        hero = f'{when}, you broke even.'
        hero_cls = "mom-hero"
    body = f'          <div class="{hero_cls}">{hero}</div>\n'

    # ── Trend: income vs spend per month; current month flagged "so far" ──
    body += '          <div class="mom-chart">\n'
    for i, m in enumerate(mom):
        income = m.get("income", 0)
        spend = m.get("spend", 0)
        net = m.get("net", 0)
        month_label = m.get("month_short", m.get("month", "?")[:3])
        if m.get("partial"):
            month_label += " · so far"
        income_pct = (income / scale) * 100 if scale > 0 else 0
        spend_pct = (spend / scale) * 100 if scale > 0 else 0
        net_color = "var(--green)" if net >= 0 else "var(--red)"
        net_display = f"+{money_html(net)}" if net > 0 else money_html(net)
        row_cls = "mom-month-row" + (" current" if i == len(mom) - 1 else "")
        body += f"""\
            <div class="{row_cls}" data-drill="month:{m.get('ym','')}">
              <span class="mom-month-label">{_esc(month_label)}</span>
              <div class="mom-bars">
                <div class="mom-bar-track"><div class="mom-bar-fill income" style="width:{income_pct:.1f}%;"></div></div>
                <div class="mom-bar-track"><div class="mom-bar-fill spend" style="width:{spend_pct:.1f}%;"></div></div>
              </div>
              <span class="mom-net" style="color:{net_color};">{net_display}</span>
            </div>
"""
    body += '          </div>\n'
    body += """\
          <div class="mom-legend">
            <span><span class="mom-legend-dot income-dot"></span>In</span>
            <span><span class="mom-legend-dot spend-dot"></span>Out</span>
          </div>
"""

    # ── Verdict: the trend in one line (complete months only) ──
    complete = [m for m in mom if not m.get("partial")]
    if complete:
        nets = [m.get("net", 0) for m in complete]
        ahead = sum(1 for n in nets if n >= 0)
        body += (f'          <div class="mom-verdict">{ahead} of {len(complete)} '
                 f'complete months you came out ahead.</div>\n')

    return _report_section(section_num, "Month by month", status_class, status_text, True, body)


def _section_spending_breakdown(digest, section_num="04"):
    """Render the Spending Breakdown section (monthly only).

    Uses budget.detail.categories if available, otherwise builds a basic
    breakdown from available data.
    """
    bud = digest.get("sections", {}).get("budget", {})
    cb = bud.get("detail", {}).get("categories") or {}
    cats = cb.get("categories", [])
    spend = cb.get("spend", 0)
    obl = cb.get("obligations", 0)
    disc = cb.get("discretionary", 0)
    transfers = cb.get("transfers", 0)

    status_class = "status-neutral"
    status_text = money(spend)   # badge text is escaped — plain money, no span

    if not cats:
        body = """\
          <div class="note-box">No spend in this window yet.</div>
"""
        return _report_section(section_num, "Where your money goes", status_class, status_text, True, body)

    # Obligations vs discretionary split bar (the one meaningful 2-way view)
    obl_pct = (obl / spend * 100) if spend else 0
    body = f"""\
          <div class="split-bar-label">
            <span><span class="dot blue"></span>Obligations {money_html(obl)}</span>
            <span><span class="dot gold"></span>Discretionary {money_html(disc)}</span>
          </div>
          <div class="split-bar">
            <div class="split-seg blue" style="width:{obl_pct:.1f}%;"></div>
            <div class="split-seg gold" style="width:{100 - obl_pct:.1f}%;"></div>
          </div>
"""

    # Ranked "where it goes" bars — width relative to the top category so small
    # leaks stay visible; a kind chip on each (locked obligation / open discretionary / leak).
    max_amt = max((c.get("amount", 0) for c in cats), default=1) or 1
    chip = {"obligation": ('blue', '&#128274; obligation'),
            "leak": ('leak', '&#9873; leak'),
            "discretionary": ('gold', 'discretionary')}
    body += '          <div class="cat-list">\n'
    for c in cats:
        amt = c.get("amount", 0)
        w = max(2.0, amt / max_amt * 100)
        cls, label = chip.get(c.get("kind"), ('gold', ''))
        merch = c.get("top_merchant", "")
        merch_html = f' <span class="cat-merch">{_esc(merch)}</span>' if merch else ''
        body += f"""\
            <div class="cat-row" data-drill="cat:{_esc(c.get('name','?'))}">
              <div class="cat-head">
                <span class="cat-name">{_esc(c.get('name', '?'))}{merch_html}</span>
                <span class="cat-amt">{money_html(amt)} <span class="cat-pct">{c.get('pct', 0):.0f}%</span></span>
              </div>
              <div class="cat-bar-track"><div class="cat-bar-fill {cls}" style="width:{w:.1f}%;"></div></div>
              <span class="cat-chip {cls}">{label}</span>
            </div>
"""
    body += '          </div>\n'
    if transfers:
        body += f"""\
          <div class="cat-footnote">+ {money_html(transfers)} in transfers excluded (money moved, not spent).</div>
"""
    return _report_section(section_num, "Where your money goes", status_class, status_text, True, body)


def _report_section(num, title, status_class, status_text, is_open, body_html):
    """Wrap a section body in a <details> collapsible with header."""
    open_attr = " open" if is_open else ""
    return f"""\
    <div class="report-section">
      <details{open_attr}>
        <summary>
          <div class="section-title-group">
            <span class="section-number">{_esc(num)}</span>
            <span class="section-title">{_esc(title)}</span>
          </div>
          <div style="display:flex;align-items:center;gap:6px;">
            <span class="section-status {status_class}">{_esc(status_text)}</span>
            {_CHEVRON_SVG}
          </div>
        </summary>
        <div class="section-body">
{body_html}
        </div>
      </details>
    </div>
"""


# ─────────────────────────── email portion builder ────────────────────────────

def _build_email_portion(digest, report_url=None, mode="weekly"):
    """Build the email portion: header, hero, vitals, summaries, CTA."""
    window = digest.get("window", {})
    sections = digest.get("sections", {})
    hero = select_hero(digest)
    sev = hero["severity"]

    # Date display
    if mode == "monthly":
        try:
            d = dt.date.fromisoformat(window.get("start", ""))
            date_display = d.strftime("%B %Y")
        except (ValueError, TypeError):
            date_display = _format_date_range(window.get("start", ""), window.get("end", ""))
    else:
        date_display = _format_date_range(window.get("start", ""), window.get("end", ""))

    mode_label = "Monthly" if mode == "monthly" else "Weekly"

    # Hero card styling
    hero_color = _severity_color(sev)
    hero_bg = _severity_bg(sev)
    hero_border = _severity_border(sev)

    html = f"""\
  <div class="email-portion">

    <!-- Header -->
    <div class="email-header">
      <div class="email-header-left">
        <h1>finance.mcp</h1>
        <div class="date">{date_display}</div>
      </div>
      <span class="badge-mode">{mode_label}</span>
    </div>

    <!-- Hero Card -->
    <div class="hero-card" style="border: 1px solid {hero_border}; border-left: 4px solid {hero_color};">
      <div class="hero-badge" style="color:{hero_color}; background:{hero_bg}; border: 1px solid {hero_border};">
        <span class="dot" style="background:{hero_color};"></span>
        {_esc(hero['badge_text'])}
      </div>
      <div class="hero-amount" style="color:{hero_color};">{hero['amount']}</div>
      <div class="hero-subtitle">
        {hero['subtitle']}
      </div>
    </div>
"""

    # Vitals strip
    fc = sections.get("forecast", {})
    fh = fc.get("headline", {})
    bud = sections.get("budget", {})
    bh = bud.get("headline", {})
    fee = sections.get("fee_fraud", {})
    ffh = fee.get("headline", {})
    recon = sections.get("reconciliation", {})
    rh = recon.get("headline", {})

    # Balance vital
    if fc.get("available", False):
        bal = fh.get("start_balance", 0)
        bal_dollars, bal_cents = money_html_short(bal), ""
    else:
        bal_dollars, bal_cents = ("--", "")

    # Goal vital
    pct = bh.get("pct_to_target")
    status = bh.get("status", "")
    if pct is not None:
        goal_class = "red" if status == "behind" else ("green" if status == "ahead" else "")
        goal_val = f"{pct}%"
        goal_ind_class = "red" if status == "behind" else ""
        goal_ind = status
    else:
        goal_class = ""
        goal_val = "--"
        goal_ind_class = ""
        goal_ind = ""

    # Fees vital — show RECOVERABLE hard-dollars; anomalies are a deviation count,
    # not recoverable money, so they go in the indicator (not lumped into the $).
    recoverable = ffh.get("avoidable", 0)
    n_anom = ffh.get("n_anomalies", 0)
    if recoverable > 0 or n_anom > 0:
        fee_class = "amber"
        fee_val = money_html(recoverable)
        fee_ind_class = "amber"
        fee_ind = (f"{n_anom} anomal{'y' if n_anom == 1 else 'ies'}"
                   if n_anom else "to review")
    else:
        fee_class = ""
        fee_val = money_html_short(0)
        fee_ind_class = ""
        fee_ind = "clear"

    # Verified (receipt coverage) vital — replaces standalone "Receipts" stat
    coverage_pct = rh.get("coverage_pct", 0)
    total_rcpt = rh.get("total_receipts", 0)
    n_disc = rh.get("n_discrepancies", 0)
    if total_rcpt > 0:
        rcpt_val = f"{coverage_pct:.0f}%"
        if n_disc > 0:
            rcpt_class = "amber"
            rcpt_ind_class = "amber"
            rcpt_ind = f"{n_disc} mismatch"
        elif coverage_pct >= 90:
            rcpt_class = "green"
            rcpt_ind_class = ""
            rcpt_ind = "verified"
        else:
            rcpt_class = ""
            rcpt_ind_class = ""
            rcpt_ind = "verified"
    else:
        rcpt_val = "--"
        rcpt_class = ""
        rcpt_ind_class = ""
        rcpt_ind = ""

    html += f"""\
    <!-- Vitals Strip -->
    <div class="vitals-strip">
      <div class="vital-card">
        <div class="vital-label">Balance</div>
        <div class="vital-value">{bal_dollars}</div>
        <div class="vital-indicator">{bal_cents}</div>
      </div>
      <div class="vital-card">
        <div class="vital-label">Goal</div>
        <div class="vital-value {goal_class}">{goal_val}</div>
        <div class="vital-indicator {goal_ind_class}">{goal_ind}</div>
      </div>
      <div class="vital-card">
        <div class="vital-label">Review</div>
        <div class="vital-value {fee_class}">{fee_val}</div>
        <div class="vital-indicator {fee_ind_class}">{fee_ind}</div>
      </div>
      <div class="vital-card">
        <div class="vital-label">Verified</div>
        <div class="vital-value {rcpt_class}">{rcpt_val}</div>
        <div class="vital-indicator {rcpt_ind_class}">{rcpt_ind}</div>
      </div>
    </div>
"""

    # Monthly summary line
    if mode == "monthly":
        income = bh.get("income_window", 0)
        spend = bh.get("spend_window", 0)
        net = bh.get("net_saved_window", 0)
        try:
            d = dt.date.fromisoformat(window.get("start", ""))
            month_name = d.strftime("%B")
        except (ValueError, TypeError):
            month_name = "Month"

        html += f"""\
    <!-- Month Summary Line -->
    <div class="month-summary">
      <div class="month-summary-card">
        <span class="month-summary-label">{month_name} Summary</span>
        <div class="month-summary-numbers">
          <span class="in">{money_html(income)} in</span>
          <span class="sep">/</span>
          <span class="out">{money_html(spend)} out</span>
          <span class="sep">/</span>
          <span class="net">net {money_html(net)}</span>
        </div>
      </div>
    </div>
"""

    # Section summaries
    padding_top = "4px" if mode == "monthly" else "0"
    html += f"""\
    <!-- Section Summaries -->
    <div class="summaries" style="padding-top:{padding_top};">"""

    # Forecast summary
    if fc.get("available", False):
        if fh.get("overdraft"):
            fc_icon_class = "red"
            fc_min_date = _format_date_short(fh.get("min_date", ""))
            fc_safe = f" &mdash; safe-by {_format_date_short(fh.get('safe_by', ''))}" if fh.get("safe_by") else ""
            fc_text = f"Overdraft projected {fc_min_date}{fc_safe}"
        elif fh.get("low_balance"):
            fc_icon_class = "amber"
            fc_text = f"Low balance warning &mdash; min {money_html(fh.get('min_balance', 0))}"
        else:
            fc_icon_class = "green"
            fc_text = f"Clear &mdash; min balance {money_html(fh.get('min_balance', 0))}"
    else:
        fc_icon_class = "neutral"
        fc_text = "Connect bank for forecast"

    html += f"""
      <div class="summary-row">
        <div class="summary-icon {fc_icon_class}">
          {_SVG_ICONS['forecast']}
        </div>
        <div>
          <span class="summary-label">Forecast</span>&ensp;
          {fc_text}
        </div>
      </div>"""

    # Budget summary
    if bh.get("status"):
        bud_status = bh["status"]
        bud_icon_class = "red" if bud_status == "behind" else ("green" if bud_status == "ahead" else "neutral")
        bud_text = f"{bud_status.capitalize()} pace. {money_html(bh.get('current_pace_mo', 0))}/mo vs {money_html(bh.get('required_pace_mo', 0))}/mo needed"
    else:
        bud_icon_class = "neutral"
        bud_text = "Connect bank for budget tracking"

    html += f"""
      <div class="summary-row">
        <div class="summary-icon {bud_icon_class}">
          {_SVG_ICONS['budget']}
        </div>
        <div>
          <span class="summary-label">Budget</span>&ensp;
          {bud_text}
        </div>
      </div>"""

    # Fee/fraud summary
    if ffh.get("avoidable_plus_suspect", 0) > 0:
        ff_icon_class = "amber"
        ff_text = f"{money_html(ffh['avoidable_plus_suspect'])} avoidable &mdash; {ffh.get('n_fees', 0)} bank fee{'s' if ffh.get('n_fees', 0) != 1 else ''}, {ffh.get('n_duplicates', 0)} duplicate{'s' if ffh.get('n_duplicates', 0) != 1 else ''}"
    elif ffh:
        ff_icon_class = "green"
        ff_text = "No fees or fraud detected"
    else:
        ff_icon_class = "neutral"
        ff_text = "Connect bank for fee scanning"

    html += f"""
      <div class="summary-row">
        <div class="summary-icon {ff_icon_class}">
          {_SVG_ICONS['fee_fraud']}
        </div>
        <div>
          <span class="summary-label">Fee/Fraud</span>&ensp;
          {ff_text}
        </div>
      </div>"""

    # Receipt verification summary
    if total_rcpt > 0:
        if n_disc > 0:
            rcpt_icon_class = "amber"
            rcpt_text = (f"{coverage_pct:.0f}% verified &mdash; "
                         f"{n_disc} mismatch{'es' if n_disc != 1 else ''} "
                         f"({money_html(rh.get('discrepancy_amount', 0))} difference)")
        elif coverage_pct >= 90:
            rcpt_icon_class = "green"
            rcpt_text = f"{coverage_pct:.0f}% verified ({rh.get('matched', 0)}/{total_rcpt} receipts matched)"
        else:
            rcpt_icon_class = "neutral"
            rcpt_text = f"{coverage_pct:.0f}% verified ({rh.get('matched', 0)}/{total_rcpt})"
    else:
        rcpt_icon_class = "neutral"
        rcpt_text = "No receipts this period"

    html += f"""
      <div class="summary-row">
        <div class="summary-icon {rcpt_icon_class}">
          {_SVG_ICONS['receipts']}
        </div>
        <div>
          <span class="summary-label">Verified</span>&ensp;
          {rcpt_text}
        </div>
      </div>"""

    html += """
    </div>
"""

    # CTA
    cta_href = _esc(report_url) if report_url else "#full-report"
    html += f"""\
    <!-- CTA -->
    <div class="cta-wrap">
      <a href="{cta_href}" class="cta-button">View Full Report &rarr;</a>
    </div>

  </div>
"""

    return html


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
    """Bake the build-time FX rate + PPP factor and pull in the toggle module.

    Emitted only on the hosted full report (email clients ignore <script>).
    The pipeline puts the live-fetched rate in digest['fx']; defaults are a
    safe fallback so a report still renders if the fetch was skipped.
    """
    fx = digest.get("fx", {}) or {}
    rate = fx.get("rate", 5.07)
    ppp = fx.get("ppp", 2.5)
    date = fx.get("date", "")
    return f"""\
  <script>window.__FX = {{ rate: {rate}, ppp: {ppp}, date: "{_esc(str(date))}" }};</script>
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
    """USD↔BRL toggle + live-rate readout for the hosted report header."""
    return """\
      <div class="fx" data-fx-toggle role="group" aria-label="Display currency">
        <span class="fx-rate" data-fx-rate></span>
        <div class="fx-seg">
          <button type="button" data-cur="USD">US$</button>
          <button type="button" data-cur="BRL">R$</button>
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
        <g stroke="#00A651" stroke-width="2.1" stroke-linecap="round">
          <path d="M12 2.5v6"/><path d="M12 15.5v6"/><path d="M2.5 12h6"/><path d="M15.5 12h6"/>
        </g>
        <g stroke="#FFCB00" stroke-width="2.1" stroke-linecap="round">
          <path d="M5.2 5.2l4.2 4.2"/><path d="M14.6 14.6l4.2 4.2"/><path d="M18.8 5.2l-4.2 4.2"/><path d="M9.4 14.6l-4.2 4.2"/>
        </g>
        <circle cx="12" cy="12" r="2.3" fill="#0066CC"/>
      </svg>
      <div class="brandbar-word">Savings Goal<span class="brandbar-mono">finance.mcp</span></div>
      <span class="brandbar-private">
        <svg width="11" height="11" viewBox="0 0 16 16" fill="none"><path d="M4 7V5a4 4 0 0 1 8 0v2" stroke="#008542" stroke-width="1.5"/><rect x="3" y="7" width="10" height="7" rx="1.5" fill="#00A651"/></svg>
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

    html = _html_head("finance.mcp - Weekly Finance Digest")

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


# ─────────────────────────── render_email_html ────────────────────────────────

def render_email_html(digest, report_url):
    """Render the compact email-only HTML (header through CTA).

    This goes in the Gmail email body. The CTA links to report_url.
    No full report section.
    """
    mode = digest.get("mode", "weekly")

    html = _html_head(
        f"finance.mcp - {'Monthly' if mode == 'monthly' else 'Weekly'} Digest",
        is_monthly=(mode == "monthly"),
    )

    html += '<div class="container">\n'
    html += _build_email_portion(digest, report_url=report_url, mode=mode)
    html += '</div>\n\n</body>\n</html>'

    return html
