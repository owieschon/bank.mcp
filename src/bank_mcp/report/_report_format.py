"""_report_format.py — small HTML formatting helpers for the report.

Date ranges, money spans (with the currency-toggle data hook), and severity
colors. Leaf functions split out of digest_templates.py so that module is the
renderer, not a grab-bag. Imported back by digest_templates.
"""

import datetime as dt
import html as _html

from bank_mcp.report.delivery import money


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


def _format_date_short(date_str):
    """Format a date like 'Jun 17'."""
    try:
        d = dt.date.fromisoformat(date_str)
        return f"{d.strftime('%b')} {d.day}"
    except (ValueError, TypeError):
        return str(date_str) if date_str else ""


def money_html(x):
    """HTML money with a data-usd hook for the client-side USD↔secondary-currency toggle.

    Renders the canonical USD string but wraps it in a span carrying the raw
    numeric value, so assets/currency.js can re-denominate it in the
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
