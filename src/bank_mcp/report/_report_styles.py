"""_report_styles.py — static CSS + inline SVG icons for the HTML report.

Pure presentation constants, split out of digest_templates.py so the renderer
module is logic, not an 800-line stylesheet. Imported back by digest_templates.
"""

_CSS = """\
  :root {
    /* Design tokens. Saturated, confident color (not traffic-light defaults), a
       crisp near-white base, high contrast, bold display type. Token names are stable
       so rules recolor in place. */
    --red: #F0473E;            /* warm alert red — risk/deficit (used sparingly) */
    --red-bg: #FEE4E2;
    --red-border: #FBBFBA;
    --red-deep: #D62F26;
    --amber: #F2B705;          /* gold / caution / highlight */
    --amber-bg: #FFF3CC;
    --amber-border: #FBE08A;
    --green: #009C53;          /* good / saved — the lead color */
    --green-bg: #D4F4E2;
    --green-border: #8FE3B6;
    --green-deep: #00803F;
    --blue: #1E50C8;           /* info / accent */
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

  /* ── serif-heading layer: serif headings, green masthead accent ── */
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

  /* ── PPP orientation notes (secondary-currency view only; toggled by currency.js) ── */
  .ppp-note { margin: 10px 0 2px; padding: 9px 13px; background: var(--green-bg); border-left: 2.5px solid var(--green); border-radius: 0 8px 8px 0; font-size: 12px; color: var(--green-deep, #00803F); line-height: 1.45; }
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
    font-size: 11px; font-weight: 500; color: var(--green-deep, #00803F);
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
  .report-header h2 em { font-style: italic; color: var(--green-deep, #00803F); }
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

  /* Footer: match the landing (serif wordmark + accent rule) */
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
  .freed-note { margin-top: 10px; font-size: 12px; color: var(--green-deep, #00803F); }

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
