"""_report_sections.py — the per-section HTML builders for the report.

Each `_section_*` turns one engine's summary into an HTML section; `_report_section`
is the shared open/closed wrapper. Split out of digest_templates.py so that module
is page assembly (head/brandbar/footer + render_*) and this is section rendering.
Depends only on delivery.money and the leaf _report_format/_report_styles helpers.
"""
import datetime as dt

from bank_mcp.report.delivery import money
from bank_mcp.report._report_format import (
    _esc, money_html, money_html_short, _money_short,
    _format_date_long, _format_date_short,
)
from bank_mcp.report._report_styles import _CHEVRON_SVG


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

    GREEN, AMBER, RED, GREY = "#009C53", "#E0A500", "#E04A2F", "#D8D2C2"
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
            At local prices, {money_html(target)} lives like <span data-ppp-value></span> of US spending power.
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
