"""email_html.py — the HTML email renderer for the digest.

Builds the multipart/alternative HTML body the daily email sends, from the
same digest dict the Markdown report uses. Split out of delivery.py so that
module is delivery primitives (send / narrate / format) rather than ~700 lines
of HTML. Formatting helpers are imported from delivery (one source of truth).
"""
import datetime as dt

from bank_mcp.report.delivery import (
    _e, money, fmt_date, _updated_stamp, _format_window,
)


_FONT_SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif"
_FONT_MONO = "'SF Mono','Fira Code',Consolas,monospace"


def digest_subject_line(digest):
    """Generate a concise subject line from the digest.

    Format: "bank.mcp Digest -- Jun 17 -- 9 receipts scanned, 4 unmatched"
    Falls back to date + mode when specific data isn't available.
    """
    as_of = digest.get("as_of") or str(dt.date.today())
    try:
        d = dt.date.fromisoformat(as_of)
        date_str = d.strftime("%b %-d")
    except (ValueError, TypeError):
        date_str = as_of

    parts = []

    # Receipt highlight (always real data since it uses Gmail)
    rcpt = digest.get("sections", {}).get("receipts", {})
    rh = rcpt.get("headline", {})
    if rcpt.get("available", True) and rh.get("total_receipts", 0) > 0:
        parts.append(f"{rh['total_receipts']} receipts scanned, "
                     f"{rh['unmatched']} unmatched")

    # Budget status
    bud = digest.get("sections", {}).get("budget", {})
    bh = bud.get("headline", {})
    if bh.get("status"):
        parts.append(f"Goal {bh['status']}")

    # Fee/fraud if nonzero
    fee = digest.get("sections", {}).get("fee_fraud", {})
    fh = fee.get("headline", {})
    if fh.get("avoidable_plus_suspect", 0) > 0:
        parts.append(f"{money(fh['avoidable_plus_suspect'])} flagged")

    summary = ", ".join(parts) if parts else digest.get("mode", "digest")
    return f"bank.mcp Digest — {date_str} — {summary}"


def _html_section_header(title, border_color="#009C53"):
    """Render a section header row with a left-border accent."""
    return (
        f'<tr><td bgcolor="#ffffff" style="padding:0 0 8px 0;background-color:#ffffff;">'
        f'<table width="100%" cellpadding="0" cellspacing="0" border="0">'
        f'<tr><td bgcolor="#ffffff" style="border-left:4px solid {border_color};padding:10px 16px;'
        f'background-color:#ffffff;color:#15171C;font-size:16px;font-weight:700;'
        f'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;'
        f'border-radius:0 4px 4px 0;">'
        f'{title}</td></tr></table></td></tr>'
    )


def _html_unavailable_card(section_name, description):
    """Render a muted 'Connect your bank' placeholder card."""
    return (
        f'<tr><td bgcolor="#ffffff" style="padding:0 0 24px 0;background-color:#ffffff;">'
        f'<table width="100%" cellpadding="0" cellspacing="0" border="0">'
        f'<tr><td bgcolor="#ffffff" style="background-color:#ffffff;border:1px dashed #767B86;'
        f'border-radius:8px;padding:24px;text-align:center;'
        f'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;">'
        f'<span style="font-size:24px;color:#15171C;">&#009C53;</span><br>'
        f'<span style="font-size:15px;font-weight:700;color:#15171C;">'
        f'Connect your bank to enable {section_name}</span><br>'
        f'<span style="font-size:13px;color:#4A4E58;line-height:1.6;">'
        f'{description}</span>'
        f'</td></tr></table></td></tr>'
    )


def _html_kv_row(label, value, value_color=None, mono=False):
    """Render a label: value row inside a section card."""
    val_font = _FONT_MONO if mono else _FONT_SANS
    color_css = f"color:{value_color};" if value_color else "color:#15171C;"
    return (
        '<tr>'
        f'<td bgcolor="#ffffff" style="padding:4px 0;color:#4A4E58;font-size:13px;'
        f'background-color:#ffffff;font-family:{_FONT_SANS};'
        f'vertical-align:top;" width="45%">{label}</td>'
        f'<td bgcolor="#ffffff" style="padding:4px 0;font-size:14px;{color_css}'
        f'background-color:#ffffff;font-family:{val_font};'
        f'font-weight:600;vertical-align:top;">{value}</td>'
        '</tr>'
    )


def _html_badge(text, bg_color, text_color="#ffffff"):
    """Render an inline status badge."""
    return (
        f'<span style="display:inline-block;background-color:{bg_color};color:{text_color};'
        f'font-size:11px;font-weight:700;padding:3px 10px;border-radius:12px;'
        f'letter-spacing:0.5px;text-transform:uppercase;">{text}</span>'
    )


def _html_flag_item(text, color="#F2B705"):
    """Render a single flag/alert item."""
    return (
        f'<tr><td bgcolor="#ffffff" style="padding:6px 0 6px 12px;border-left:3px solid {color};'
        f'background-color:#ffffff;font-size:13px;color:#15171C;line-height:1.5;'
        f'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;">'
        f'{text}</td></tr>'
    )


def _html_progress_bar(pct, bar_color="#009C53"):
    """Render a horizontal progress bar."""
    pct_clamped = max(0, min(100, pct))
    return (
        f'<table width="100%" cellpadding="0" cellspacing="0" border="0">'
        f'<tr><td bgcolor="#ffffff" style="padding:4px 0;background-color:#ffffff;">'
        f'<table width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'style="background-color:#E7E8E3;border-radius:6px;overflow:hidden;">'
        f'<tr><td style="width:{pct_clamped}%;height:12px;background-color:{bar_color};'
        f'border-radius:6px;"></td>'
        f'<td style="height:12px;background-color:#E7E8E3;"></td></tr>'
        f'</table></td></tr></table>'
    )


def _email_balance_change(digest):
    _out = []
    # ── What moved your balance (since the last update) ─────────────────────
    bc = digest.get("balance_change")
    if bc and bc.get("txns"):
        delta = bc.get("delta", 0)
        dcolor = "#009C53" if delta >= 0 else "#F0473E"
        trows = ""
        for t in bc["txns"][:12]:
            a = t.get("amount", 0)
            ac = "#009C53" if a >= 0 else "#15171C"
            trows += (
                '<tr><td style="padding:5px 0;border-top:1px solid #F2F3F0;font-size:12px;'
                f'color:#767B86;font-family:-apple-system,BlinkMacSystemFont,sans-serif;">'
                f'{_e(fmt_date(t.get("date","")))}</td>'
                '<td style="padding:5px 0;border-top:1px solid #F2F3F0;font-size:13px;'
                f'color:#15171C;font-family:-apple-system,BlinkMacSystemFont,sans-serif;">'
                f'{_e(t.get("merchant","?"))}</td>'
                '<td style="padding:5px 0;border-top:1px solid #F2F3F0;text-align:right;'
                f'font-size:13px;font-weight:600;color:{ac};font-family:\'SF Mono\',Consolas,monospace;">'
                f'{_e(money(a))}</td></tr>'
            )
        _out.append(_html_section_header("What moved your balance", "#1E50C8"))
        _out.append(
            '<tr><td bgcolor="#ffffff" style="padding:0 0 24px 0;background-color:#ffffff;">'
            '<table width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#ffffff" '
            'style="background-color:#ffffff;border:1px solid #E7E8E3;border-radius:8px;">'
            '<tr><td bgcolor="#ffffff" style="padding:20px;background-color:#ffffff;">'
            f'<div style="text-align:center;padding:4px 0 14px 0;">'
            f'<span style="font-size:20px;font-weight:700;color:#15171C;'
            f'font-family:\'SF Mono\',Consolas,monospace;">{_e(money(bc.get("prior_balance",0)))} '
            f'&rarr; {_e(money(bc.get("current_balance",0)))}</span>'
            f'<span style="display:block;font-size:13px;font-weight:600;color:{dcolor};margin-top:3px;">'
            f'{_e(money(delta))} since {_e(fmt_date(bc.get("prior_date","")))}</span></div>'
            '<div style="font-size:11px;font-weight:700;letter-spacing:0.5px;'
            'text-transform:uppercase;color:#767B86;padding-bottom:4px;'
            'font-family:-apple-system,BlinkMacSystemFont,sans-serif;">'
            f'Synced since last update &middot; net {_e(money(bc.get("net",0)))}</div>'
            '<table width="100%" cellpadding="0" cellspacing="0" border="0">'
            + trows +
            '</table>'
            + ('' if bc.get("reconciles", True) else
               '<div style="font-size:11px;color:#9aa0ab;padding-top:8px;'
               'font-family:-apple-system,BlinkMacSystemFont,sans-serif;">Newly synced '
               'rows — the live balance also reflects pending / just-posted activity '
               'not yet itemized (the feed lags ~1–2 days).</div>')
            + '</td></tr></table></td></tr>'
        )
    return "\n".join(_out)


def _email_what_matters(digest):
    _out = []
    # ── What matters (top-level flags) ──────────────────────────────────────
    flags = digest.get("flags", [])
    if flags:
        flag_rows = []
        for f in flags:
            fl = f.lower()
            if "overdraft" in fl or "behind" in fl:
                color = "#F0473E"
            elif "low" in fl or "suspect" in fl or "fee" in fl or "fraud" in fl:
                color = "#F2B705"
            else:
                color = "#009C53"
            flag_rows.append(_html_flag_item(_e(f), color))
        _out.append(
            _html_section_header("What Matters", "#F0473E")
            + '<tr><td bgcolor="#ffffff" style="padding:0 0 24px 0;background-color:#ffffff;">'
            + '<table width="100%" cellpadding="0" cellspacing="0" border="0" '
            + 'bgcolor="#ffffff" style="background-color:#ffffff;border-radius:8px;border:1px solid #E7E8E3;padding:16px;">'
            + '<tr><td bgcolor="#ffffff" style="padding:16px;background-color:#ffffff;">'
            + '<table width="100%" cellpadding="0" cellspacing="0" border="0">'
            + "".join(flag_rows)
            + '</table></td></tr></table></td></tr>'
        )
    return "\n".join(_out)


def _email_forecast(digest):
    _out = []
    # ── Cash-flow Forecast ──────────────────────────────────────────────────
    fc = digest.get("sections", {}).get("forecast", {})
    _out.append(_html_section_header("Cash-flow Forecast", "#009C53"))
    if fc.get("available", False):
        h = fc.get("headline", {})
        if h.get("overdraft"):
            status_badge = _html_badge("OVERDRAFT", "#F0473E")
            min_color = "#F0473E"
        elif h.get("low_balance"):
            status_badge = _html_badge("LOW BALANCE", "#F2B705")
            min_color = "#F2B705"
        else:
            status_badge = _html_badge("CLEAR", "#009C53")
            min_color = "#009C53"

        safe_by = f' &middot; safe by {h["safe_by"]}' if h.get("safe_by") else ""

        card = (
            '<tr><td bgcolor="#ffffff" style="padding:0 0 24px 0;background-color:#ffffff;">'
            '<table width="100%" cellpadding="0" cellspacing="0" border="0" '
            'bgcolor="#ffffff" style="background-color:#ffffff;border:1px solid #E7E8E3;border-radius:8px;">'
            '<tr><td bgcolor="#ffffff" style="padding:20px;background-color:#ffffff;color:#15171C;">'
            # Status badge
            f'<div style="margin-bottom:16px;">{status_badge}{safe_by}</div>'
            # Big min-balance number
            f'<div style="text-align:center;padding:12px 0;">'
            f'<span style="font-size:13px;color:#767B86;'
            f'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;'
            f'display:block;margin-bottom:4px;">Minimum Balance</span>'
            f'<span style="font-size:32px;font-weight:700;color:{min_color};'
            f'font-family:\'SF Mono\',\'Fira Code\',Consolas,monospace;">'
            f'{money(h.get("min_balance", 0))}</span>'
            f'<span style="font-size:13px;color:#767B86;display:block;'
            f'margin-top:4px;font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\','
            f'Roboto,sans-serif;">on {fmt_date(h.get("min_date", "?"))}</span>'
            f'</div>'
            # Key-value rows
            '<table width="100%" cellpadding="0" cellspacing="0" border="0" '
            'style="margin-top:12px;border-top:1px solid #FBFBF8;padding-top:12px;">'
            + _html_kv_row("Start balance", money(h.get("start_balance", 0)), mono=True)
            + _html_kv_row("Projected end", money(h.get("projected_end_balance", 0)), mono=True)
            + _html_kv_row("Daily burn", money(h.get("daily_burn", 0)), mono=True)
            + _html_kv_row("Horizon", f'{h.get("horizon_days", 0)} days')
        )

        ni = h.get("next_income")
        if ni:
            card += _html_kv_row(
                "Next income",
                f'{money(ni["amount"])} from {_e(ni["merchant"][:30])} on {_e(fmt_date(ni["date"]))}',
                value_color="#009C53",
            )

        card += '</table>'

        # Upcoming obligations
        obligations = fc.get("detail", {}).get("biggest_obligations", [])
        if obligations:
            card += (
                '<div style="margin-top:16px;padding-top:12px;'
                'border-top:1px solid #FBFBF8;">'
                '<span style="font-size:12px;color:#767B86;text-transform:uppercase;'
                'letter-spacing:0.5px;font-weight:600;'
                'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;">'
                'Upcoming obligations</span>'
                '<table width="100%" cellpadding="0" cellspacing="0" border="0" '
                'style="margin-top:8px;">'
            )
            for o in obligations[:3]:
                card += (
                    f'<tr>'
                    f'<td style="padding:3px 0;font-size:13px;color:#666;'
                    f'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;">'
                    f'{_e(fmt_date(o.get("date", "?")))}</td>'
                    f'<td style="padding:3px 0;font-size:13px;color:#333;'
                    f'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;">'
                    f'{_e(o.get("merchant", "?")[:36])}</td>'
                    f'<td style="padding:3px 0;font-size:13px;text-align:right;'
                    f'font-family:\'SF Mono\',\'Fira Code\',Consolas,monospace;'
                    f'color:#333;">{money(o.get("amount", 0))}</td>'
                    f'</tr>'
                )
            card += '</table></div>'

        card += '</td></tr></table></td></tr>'
        _out.append(card)
    else:
        _out.append(_html_unavailable_card(
            "Cash-flow Forecast",
            "Real-time balance tracking, overdraft alerts, and "
            "upcoming obligation previews.",
        ))
    return "\n".join(_out)


def _email_savings_pace(digest):
    _out = []
    # ── Savings Pace ─────────────────────────────────────────────────────────
    bud = digest.get("sections", {}).get("budget", {})
    _out.append(_html_section_header("Savings Pace", "#009C53"))
    bh = bud.get("headline", {})
    if bud.get("available", True) and bh.get("target"):
        status = bh.get("status", "on track")
        if status == "ahead":
            badge = _html_badge("AHEAD", "#009C53")
            bar_color = "#009C53"
        elif status == "behind":
            badge = _html_badge("BEHIND", "#F0473E")
            bar_color = "#F0473E"
        else:
            badge = _html_badge("ON TRACK", "#009C53")
            bar_color = "#009C53"

        pct = bh.get("pct_to_target", 0)
        rt = bud.get("rule_tally", {})

        card = (
            '<tr><td bgcolor="#ffffff" style="padding:0 0 24px 0;background-color:#ffffff;">'
            '<table width="100%" cellpadding="0" cellspacing="0" border="0" '
            'bgcolor="#ffffff" style="background-color:#ffffff;border:1px solid #E7E8E3;border-radius:8px;">'
            '<tr><td bgcolor="#ffffff" style="padding:20px;background-color:#ffffff;color:#15171C;">'
            # Badge + pct
            f'<div style="margin-bottom:12px;">{badge}'
            f'<span style="margin-left:12px;font-size:20px;font-weight:700;'
            f'font-family:\'SF Mono\',\'Fira Code\',Consolas,monospace;">'
            f'{pct}%</span>'
            f'<span style="font-size:13px;color:#888;margin-left:4px;'
            f'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;">'
            f'of {money(bh.get("target", 0))}</span></div>'
            # Progress bar
            + _html_progress_bar(pct, bar_color)
            + '<table width="100%" cellpadding="0" cellspacing="0" border="0" '
            'style="margin-top:12px;">'
            + _html_kv_row("Running total", money(bh.get("running_total", 0)), mono=True)
            + _html_kv_row("Net saved (window)",
                           money(bh.get("net_saved_window", 0)), mono=True)
            + _html_kv_row("Current pace",
                           f'{money(bh.get("current_pace_mo", 0))}/mo', mono=True)
            + _html_kv_row("Required pace",
                           f'{money(bh.get("required_pace_mo", 0))}/mo', mono=True)
            + _html_kv_row("Projected",
                           money(bh.get("projected", 0)),
                           value_color=("#009C53" if status == "ahead" else
                                        "#F0473E" if status == "behind" else None),
                           mono=True)
            + _html_kv_row("Gap", money(bh.get("gap", 0)), mono=True)
            + _html_kv_row("Move date", fmt_date(bh.get("move_date", "?")))
            + _html_kv_row("Months remaining", str(bh.get("months_remaining", "?")))
            + '</table>'
        )

        # Rule tally
        if rt:
            card += (
                '<div style="margin-top:16px;padding-top:12px;'
                'border-top:1px solid #FBFBF8;">'
                '<span style="font-size:12px;color:#767B86;text-transform:uppercase;'
                'letter-spacing:0.5px;font-weight:600;'
                'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;">'
                'Cut Rules</span><br>'
                f'<span style="margin-top:8px;display:inline-block;font-size:14px;'
                f'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;">'
                f'<span style="color:#009C53;font-weight:600;">'
                f'{rt.get("on_track", 0)} on track</span> &middot; '
                f'<span style="color:#F2B705;font-weight:600;">'
                f'{rt.get("drifting", 0)} drifting</span> &middot; '
                f'<span style="color:#F0473E;font-weight:600;">'
                f'{rt.get("slipped", 0)} slipped</span>'
                f'</span></div>'
            )

        # Off-track rules detail
        off_track = bud.get("detail", {}).get("off_track_rules", [])
        if off_track:
            card += (
                '<table width="100%" cellpadding="0" cellspacing="0" border="0" '
                'style="margin-top:8px;">'
            )
            for r in off_track[:5]:
                status_color = "#F2B705" if r.get("status") == "drifting" else "#F0473E"
                card += (
                    f'<tr>'
                    f'<td style="padding:3px 0;font-size:13px;color:#333;'
                    f'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;">'
                    f'{_e(r.get("leak", "?"))}</td>'
                    f'<td style="padding:3px 0;font-size:13px;text-align:right;'
                    f'font-family:\'SF Mono\',\'Fira Code\',Consolas,monospace;'
                    f'color:#333;">{money(r.get("spent", 0))} / {money(r.get("goal", 0))}</td>'
                    f'<td style="padding:3px 8px;font-size:12px;color:{status_color};'
                    f'font-weight:600;text-align:right;'
                    f'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;">'
                    f'{r.get("status", "?")}</td>'
                    f'</tr>'
                )
            card += '</table>'

        card += '</td></tr></table></td></tr>'
        _out.append(card)
    else:
        _out.append(_html_unavailable_card(
            "Savings Pace",
            "Track your savings progress toward your target, "
            "with pace projections and spending rule enforcement.",
        ))
    return "\n".join(_out)


def _email_fee_fraud(digest):
    _out = []
    # ── Fee / Fraud Scan ────────────────────────────────────────────────────
    fee = digest.get("sections", {}).get("fee_fraud", {})
    _out.append(_html_section_header("Fee + Fraud Scan", "#F0473E"))
    fh = fee.get("headline", {})
    if fee.get("available", True) and fh:
        avoidable = fh.get("avoidable", 0)
        n_anom = fh.get("n_anomalies", 0)
        rec_annual = fh.get("recurring_fee_annual", 0)
        flagged = avoidable > 0 or n_anom > 0 or rec_annual > 0
        total_color = "#F0473E" if flagged else "#009C53"
        d = fee.get("detail", {})

        def _ann_row(label, reason, amount, color):
            # one annotated line: WHAT + WHY (the source) + IMPACT — never a bare $.
            return (
                '<tr><td style="padding:7px 0;border-top:1px solid #F2F3F0;'
                'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;">'
                f'<span style="font-size:13px;font-weight:600;color:#15171C;">{_e(label)}</span>'
                f'<span style="display:block;font-size:12px;color:#767B86;">{_e(reason)}</span></td>'
                '<td style="text-align:right;vertical-align:top;font-size:14px;font-weight:600;'
                f'color:{color};font-family:\'SF Mono\',Consolas,monospace;">{_e(amount)}</td></tr>'
            )

        rows = ""
        for f in d.get("fees", [])[:4]:
            rows += _ann_row(f"\U0001F4B8 {f.get('merchant','?')}",
                             f"bank fee · {f.get('category','')} · {f.get('date','')}",
                             money(f.get("amount", 0)), "#F0473E")
        for rf in d.get("recurring_fees", [])[:3]:
            st = "active — avoidable" if rf.get("active") else f"stopped {fmt_date(rf.get('last',''))}"
            rows += _ann_row(f"\U0001F501 {rf.get('merchant','?')}",
                             f"recurring fee · {st}",
                             f"{money(rf.get('annual',0))}/yr", "#F2B705")
        for a in d.get("anomalies", [])[:5]:
            rows += _ann_row(f"⚠️ {a.get('merchant','?')}",
                             f"{a.get('kind','')} · {a.get('reason','')}",
                             f"+{money(a.get('amount',0))}", "#F2B705")
        for x in d.get("duplicates", [])[:3]:
            rows += _ann_row(f"❓ {x.get('merchant','?')}",
                             f"possible duplicate · {' & '.join(x.get('dates',[]))} · verify",
                             money(x.get("amount", 0)), "#767B86")
        if not rows:
            rows = ('<tr><td style="padding:10px 0;font-size:13px;color:#767B86;'
                    'font-family:-apple-system,BlinkMacSystemFont,sans-serif;">'
                    'Nothing flagged this window.</td></tr>')

        card = (
            '<tr><td bgcolor="#ffffff" style="padding:0 0 24px 0;background-color:#ffffff;">'
            '<table width="100%" cellpadding="0" cellspacing="0" border="0" '
            'bgcolor="#ffffff" style="background-color:#ffffff;border:1px solid #E7E8E3;border-radius:8px;">'
            '<tr><td bgcolor="#ffffff" style="padding:20px;background-color:#ffffff;color:#15171C;">'
            f'<div style="text-align:center;padding:8px 0 14px 0;">'
            f'<span style="font-size:13px;color:#767B86;display:block;margin-bottom:4px;'
            f'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;">'
            f'Recoverable</span>'
            f'<span style="font-size:28px;font-weight:700;color:{total_color};'
            f'font-family:\'SF Mono\',\'Fira Code\',Consolas,monospace;">{money(avoidable)}</span>'
            + (f'<span style="display:block;font-size:12px;color:#767B86;margin-top:4px;">'
               f'{n_anom} anomal{"y" if n_anom == 1 else "ies"} flagged below</span>'
               if n_anom else '')
            + '</div>'
            '<table width="100%" cellpadding="0" cellspacing="0" border="0">'
            + rows +
            '</table>'
            '</td></tr></table></td></tr>'
        )
        _out.append(card)
    else:
        _out.append(_html_unavailable_card(
            "Fee + Fraud Scan",
            "Detect bank fees, duplicate charges, and suspicious merchant "
            "activity in your transactions.",
        ))
    return "\n".join(_out)


def _email_recurring(digest):
    _out = []
    # ── Recurring Snapshot ──────────────────────────────────────────────────
    rcur = digest.get("sections", {}).get("recurring", {})
    _out.append(_html_section_header("Recurring Snapshot", "#009C53"))
    rch = rcur.get("headline", {})
    if rcur.get("available", True) and rch:
        net = rch.get("net_monthly_runrate", 0)
        net_color = "#009C53" if net >= 0 else "#F0473E"

        card = (
            '<tr><td bgcolor="#ffffff" style="padding:0 0 24px 0;background-color:#ffffff;">'
            '<table width="100%" cellpadding="0" cellspacing="0" border="0" '
            'bgcolor="#ffffff" style="background-color:#ffffff;border:1px solid #E7E8E3;border-radius:8px;">'
            '<tr><td bgcolor="#ffffff" style="padding:20px;background-color:#ffffff;color:#15171C;">'
            # Net run-rate
            f'<div style="text-align:center;padding:8px 0 16px 0;">'
            f'<span style="font-size:13px;color:#767B86;display:block;margin-bottom:4px;'
            f'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;">'
            f'Net Monthly Run-rate</span>'
            f'<span style="font-size:28px;font-weight:700;color:{net_color};'
            f'font-family:\'SF Mono\',\'Fira Code\',Consolas,monospace;">'
            f'{money(net)}/mo</span></div>'
            '<table width="100%" cellpadding="0" cellspacing="0" border="0" '
            'style="border-top:1px solid #FBFBF8;padding-top:12px;">'
            + _html_kv_row(
                "Inflows",
                f'{rch.get("n_active_inflow", 0)} streams '
                f'(~{money(rch.get("inflow_monthly_runrate", 0))}/mo)',
                value_color="#009C53",
            )
            + _html_kv_row(
                "Outflows",
                f'{rch.get("n_active_outflow", 0)} streams '
                f'(~{money(rch.get("outflow_monthly_runrate", 0))}/mo)',
                value_color="#F0473E",
            )
            + '</table>'
        )

        # Top outflows detail
        top_out = rcur.get("detail", {}).get("top_outflow", [])
        if top_out:
            card += (
                '<div style="margin-top:16px;padding-top:12px;'
                'border-top:1px solid #FBFBF8;">'
                '<span style="font-size:12px;color:#767B86;text-transform:uppercase;'
                'letter-spacing:0.5px;font-weight:600;'
                'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;">'
                'Top Outflows</span>'
                '<table width="100%" cellpadding="0" cellspacing="0" border="0" '
                'style="margin-top:8px;">'
            )
            for s in top_out[:5]:
                card += (
                    f'<tr>'
                    f'<td style="padding:3px 0;font-size:13px;color:#333;'
                    f'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;">'
                    f'{_e(s.get("merchant", "?")[:30])}</td>'
                    f'<td style="padding:3px 0;font-size:13px;color:#888;'
                    f'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;">'
                    f'{_e(s.get("cadence", "?"))}</td>'
                    f'<td style="padding:3px 0;font-size:13px;text-align:right;'
                    f'font-family:\'SF Mono\',\'Fira Code\',Consolas,monospace;'
                    f'color:#333;">~{money(s.get("monthly_runrate", 0))}/mo</td>'
                    f'</tr>'
                )
            card += '</table></div>'

        card += '</td></tr></table></td></tr>'
        _out.append(card)
    else:
        _out.append(_html_unavailable_card(
            "Recurring Snapshot",
            "See your active recurring income and expenses with "
            "monthly run-rate calculations.",
        ))
    return "\n".join(_out)


def _email_receipts(digest):
    _out = []
    # ── Receipt Scan ────────────────────────────────────────────────────────
    rcpt = digest.get("sections", {}).get("receipts", {})
    rh = rcpt.get("headline", {})
    _out.append(_html_section_header("Receipt Scan", "#009C53"))
    if rcpt.get("available", True) and rh.get("total_receipts", 0) > 0:
        card = (
            '<tr><td bgcolor="#ffffff" style="padding:0 0 24px 0;background-color:#ffffff;">'
            '<table width="100%" cellpadding="0" cellspacing="0" border="0" '
            'bgcolor="#ffffff" style="background-color:#ffffff;border:1px solid #E7E8E3;border-radius:8px;">'
            '<tr><td bgcolor="#ffffff" style="padding:20px;background-color:#ffffff;color:#15171C;">'
            '<table width="100%" cellpadding="0" cellspacing="0" border="0">'
            + _html_kv_row("Total receipts", str(rh.get("total_receipts", 0)))
            + _html_kv_row("Matched to bank",
                           f'{rh.get("matched", 0)} ({money(rh.get("matched_amount", 0))})',
                           value_color="#009C53")
            + _html_kv_row("Unmatched",
                           f'{rh.get("unmatched", 0)} ({money(rh.get("unmatched_amount", 0))})',
                           value_color="#F0473E" if rh.get("unmatched", 0) > 0 else None)
            + '</table>'
        )

        # Unmatched receipts table
        unmatched = rcpt.get("detail", {}).get("unmatched", [])
        if unmatched:
            card += (
                '<div style="margin-top:16px;padding-top:12px;'
                'border-top:1px solid #FBFBF8;">'
                '<span style="font-size:12px;color:#767B86;text-transform:uppercase;'
                'letter-spacing:0.5px;font-weight:600;'
                'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;">'
                'Unmatched Receipts</span>'
                '<table width="100%" cellpadding="0" cellspacing="0" border="0" '
                'style="margin-top:8px;">'
                # Header row
                '<tr>'
                '<td style="padding:6px 0;font-size:11px;color:#888;'
                'text-transform:uppercase;letter-spacing:0.5px;font-weight:600;'
                'border-bottom:1px solid #E7E8E3;'
                'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;">'
                'Merchant</td>'
                '<td style="padding:6px 0;font-size:11px;color:#888;'
                'text-transform:uppercase;letter-spacing:0.5px;font-weight:600;'
                'border-bottom:1px solid #E7E8E3;text-align:right;'
                'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;">'
                'Amount</td>'
                '<td style="padding:6px 0;font-size:11px;color:#888;'
                'text-transform:uppercase;letter-spacing:0.5px;font-weight:600;'
                'border-bottom:1px solid #E7E8E3;text-align:right;'
                'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;">'
                'Date</td>'
                '</tr>'
            )
            for u in unmatched[:8]:
                amt = money(u["amount"]) if u.get("amount") else "?"
                card += (
                    f'<tr>'
                    f'<td style="padding:6px 0;font-size:13px;color:#333;'
                    f'border-bottom:1px solid #FBFBF8;'
                    f'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;">'
                    f'{_e(u.get("merchant") or "Unknown")}</td>'
                    f'<td style="padding:6px 0;font-size:13px;text-align:right;'
                    f'border-bottom:1px solid #FBFBF8;'
                    f'font-family:\'SF Mono\',\'Fira Code\',Consolas,monospace;'
                    f'color:#333;">{amt}</td>'
                    f'<td style="padding:6px 0;font-size:13px;text-align:right;'
                    f'border-bottom:1px solid #FBFBF8;color:#888;'
                    f'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;">'
                    f'{_e(u.get("date") or "?")}</td>'
                    f'</tr>'
                )
            card += '</table></div>'

        # Matched sample
        matched = rcpt.get("detail", {}).get("matched_sample", [])
        if matched:
            card += (
                '<div style="margin-top:16px;padding-top:12px;'
                'border-top:1px solid #FBFBF8;">'
                '<span style="font-size:12px;color:#767B86;text-transform:uppercase;'
                'letter-spacing:0.5px;font-weight:600;'
                'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;">'
                'Matched Sample</span>'
                '<table width="100%" cellpadding="0" cellspacing="0" border="0" '
                'style="margin-top:8px;">'
            )
            for m in matched[:5]:
                amt = money(m["amount"]) if m.get("amount") else "?"
                card += (
                    f'<tr>'
                    f'<td style="padding:4px 0;font-size:13px;color:#333;'
                    f'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;">'
                    f'{_e(m.get("merchant") or "Unknown")}</td>'
                    f'<td style="padding:4px 0;font-size:13px;text-align:right;'
                    f'font-family:\'SF Mono\',\'Fira Code\',Consolas,monospace;'
                    f'color:#009C53;">{amt}</td>'
                    f'<td style="padding:4px 0;font-size:12px;text-align:right;'
                    f'color:#888;font-family:-apple-system,BlinkMacSystemFont,'
                    f'\'Segoe UI\',Roboto,sans-serif;">'
                    f'{_e(m.get("date") or "?")}</td>'
                    f'</tr>'
                )
            card += '</table></div>'

        card += '</td></tr></table></td></tr>'
        _out.append(card)
    elif not rcpt.get("available", True):
        _out.append(_html_unavailable_card(
            "Receipt Scan",
            "Automatically scan email receipts and reconcile them against "
            "your bank transactions.",
        ))
    else:
        _out.append(
            '<tr><td bgcolor="#ffffff" style="padding:0 0 24px 0;background-color:#ffffff;">'
            '<table width="100%" cellpadding="0" cellspacing="0" border="0" '
            'bgcolor="#ffffff" style="background-color:#ffffff;border:1px solid #E7E8E3;border-radius:8px;">'
            '<tr><td bgcolor="#ffffff" style="padding:20px;background-color:#ffffff;text-align:center;color:#767B86;font-size:14px;'
            'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;">'
            'No receipts to report this period.'
            '</td></tr></table></td></tr>'
        )
    return "\n".join(_out)


def render_digest_html(digest):
    """Render the digest dict as a clean, modern HTML email string.

    Produces an inline-styled, table-based layout suitable for all major email
    clients.  Sections whose ``available`` flag is False get a muted
    "Connect your bank" placeholder card; available sections render full data.
    Uses delivery.money() for all dollar formatting.
    """
    mode_label = "WEEKLY" if digest.get("mode") == "weekly" else "MONTHLY"
    window = digest.get("window", {})
    as_of = digest.get("as_of", "")

    sections_html = [s for s in (
        _email_balance_change(digest),
        _email_what_matters(digest),
        _email_forecast(digest),
        _email_savings_pace(digest),
        _email_fee_fraud(digest),
        _email_recurring(digest),
        _email_receipts(digest),
    ) if s]

    # ── Assemble full email ─────────────────────────────────────────────────
    body_rows = "\n".join(sections_html)

    try:
        d = dt.date.fromisoformat(as_of)
        footer_date = d.strftime("%B %-d, %Y")
    except (ValueError, TypeError):
        footer_date = as_of

    html = f"""\
<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><meta name="color-scheme" content="light only"><meta name="supported-color-schemes" content="light only"></head>
<body style="margin:0;padding:0;background-color:#ffffff;-webkit-font-smoothing:antialiased;">
<table width="100%" cellpadding="0" cellspacing="0" border="0" bgcolor="#ffffff" style="background-color:#ffffff;">
<tr><td align="center" bgcolor="#ffffff" style="padding:24px 16px;background-color:#ffffff;">
<table width="600" cellpadding="0" cellspacing="0" border="0" bgcolor="#ffffff" style="max-width:600px;width:100%;background-color:#ffffff;">

<!-- Header: light paper brandbar (matches the live report homepage) -->
<tr><td bgcolor="#FBFBF8" style="background-color:#FBFBF8;padding:22px 24px 18px 24px;border-radius:8px 8px 0 0;border-bottom:1px solid #E7E8E3;">
<table width="100%" cellpadding="0" cellspacing="0" border="0">
<tr>
<td style="font-family:Georgia,'Times New Roman',serif;font-size:21px;font-weight:700;color:#15171C;">
<span style="color:#009C53;">&#10022;</span>&nbsp;Savings&nbsp;Goal&nbsp; <span style="font-family:'SF Mono',Consolas,monospace;font-size:11px;font-weight:400;color:#9aa0ab;">bank.mcp</span></td>
<td style="text-align:right;vertical-align:middle;">
<span style="background-color:#E6F4EC;color:#00803F;font-size:11px;font-weight:700;padding:4px 10px;border-radius:11px;">&#128274; Private</span></td>
</tr>
</table>
</td></tr>

<!-- Snapshot header (matches the report's 'Your snapshot / How you're doing') -->
<tr><td bgcolor="#ffffff" style="background-color:#ffffff;padding:22px 24px 2px 24px;">
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:11px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;color:#009C53;">Your snapshot &middot; {mode_label}</div>
<div style="font-family:Georgia,'Times New Roman',serif;font-size:25px;font-weight:700;color:#15171C;padding-top:3px;">How you're doing</div>
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:12px;color:#767B86;padding-top:7px;">{_format_window(window)} &middot; as of {fmt_date(as_of)} &middot; Updated {_updated_stamp(digest)}</div>
</td></tr>

<!-- Body -->
<tr><td bgcolor="#ffffff" style="background-color:#ffffff;padding:24px 20px 8px 20px;">
<table width="100%" cellpadding="0" cellspacing="0" border="0">
{body_rows}
</table>
</td></tr>

<!-- Footer -->
<tr><td bgcolor="#ffffff" style="padding:16px 24px;text-align:center;border-top:1px solid #E7E8E3;background-color:#ffffff;">
<span style="font-size:12px;color:#767B86;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
Personal Finance Suite &middot; {footer_date}<br>
Generated automatically &mdash; numbers are deterministic, narration is optional.
</span>
</td></tr>

</table>
</td></tr></table>
</body>
</html>"""

    return html
