#!/usr/bin/env python3
"""
budget_scorer.py — weekly/monthly progress reports against the savings goal.

Extends subscription_creep.py (imported, not duplicated). Reads ALL figures from
rules.md at runtime — nothing financial is hardcoded.

ARCHITECTURE RULE (the whole point):
  The model NEVER sees raw transactions. Deterministic Python reduces the full
  transaction set to a compact summary dict (~1K tokens). ONLY that summary +
  the tone block are ever sent to a model. Raw rows never enter a prompt.

Usage:
  python3 budget_scorer.py --weekly  transactions.json
  python3 budget_scorer.py --monthly transactions.json
  python3 budget_scorer.py --monthly transactions.json --no-voice   # $0 tokens
  python3 budget_scorer.py --monthly transactions.json --email you@gmail.com

Options:
  --rules PATH     rules file (default: rules.md)
  --no-voice       numbers-only scorecard, zero token spend
  --email [ADDR]   email the report via Gmail SMTP (needs env GMAIL_ADDRESS +
                   GMAIL_APP_PASSWORD; defaults recipient to GMAIL_ADDRESS)

Narration uses the cheapest model (Haiku) via ANTHROPIC_API_KEY, operating ONLY
on the summary dict + tone block. Without the key, it degrades to numbers-only.
"""

import argparse, re, sys
from finance_mcp.report import delivery
import datetime as dt
from collections import defaultdict

from finance_mcp.store import subscription_creep as sc   # reuse grouping / cadence / dup logic
from finance_mcp.store import obligation_registry as oblreg  # ending obligations free up cash
money = delivery.money

WEEKS_PER_MONTH = 4.3
DAYS_PER_MONTH = 30.44


# ----------------------------- rules.md parsing -----------------------------

def _num(s):
    """First number in a string, or None."""
    m = re.search(r"(\d[\d,]*\.?\d*)", s or "")
    return float(m.group(1).replace(",", "")) if m else None


def parse_rules(path):
    txt = open(path, encoding="utf-8").read()

    def find(pat, flags=re.I):
        m = re.search(pat, txt, flags)
        return m.group(1).strip() if m else None

    target = _num(find(r"Target amount[:*\s]*\**\s*\$([\d,]+)"))
    move_raw = find(r"Move date[:*\s]*\**\s*~?\s*([A-Za-z]+ \d{4})")
    base_raw = find(r"Baseline start[:*\s]*\**\s*(\d{4}-\d{2})")
    ceiling = _num(find(r"CEILING[:*\s]*~?\$?([\d,]+)"))

    if not (target and move_raw and base_raw):
        sys.exit(f"rules parse error in {path}: target={target} move={move_raw} baseline={base_raw}")

    move_date = dt.datetime.strptime(move_raw, "%B %Y").date().replace(day=1)
    baseline_date = dt.datetime.strptime(base_raw, "%Y-%m").date().replace(day=1)

    # cut rules table: rows like | n | leak | rule | prior | target |
    rules = []
    in_cut = False
    for line in txt.splitlines():
        if re.match(r"#+\s*Cut rules", line, re.I):
            in_cut = True; continue
        if in_cut and line.startswith("#"):
            break
        if in_cut and line.strip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) < 5 or cells[0] in ("#", "---") or set(cells[0]) <= set("-"):
                continue
            leak = cells[1]
            prior = _num(cells[3])           # "Prior $/mo" column
            tgt = _num(cells[4])             # "→ Target" column
            if leak and prior is not None:
                rules.append({"leak": leak, "prior": prior,
                              "target": tgt if tgt is not None else 0.0})

    # tone block ("How to read me")
    tm = re.search(r"#+\s*How to read me.*?\n(.*?)(?:\n#+\s|\Z)", txt, re.I | re.S)
    tone = tm.group(1).strip() if tm else "Direct, encouraging, blunt on slips, never shaming."

    return {"target": target, "move_date": move_date, "baseline_date": baseline_date,
            "ceiling": ceiling, "rules": rules, "tone": tone, "path": path}


# ------------------------------- matchers -----------------------------------
# Maps a human cut-rule label to a predicate over a transaction. Glue logic only;
# all DOLLAR figures still come from rules.md.

def _cat(t):  return (t.get("category") or "")
def _mer(t):  return (t.get("merchantName") or t.get("description") or "").lower()

# SELF-transfers (own money moving between pockets: ATM cash, account transfers,
# wires) are genuinely not spend or income — excluded from the savings math.
# P2P sends to PEOPLE (Zelle/Venmo/Cash App/Apple Cash to a named person) are NOT
# self-transfers — they're real money leaving (support, gifts) and count as spend.
_SELF_TRANSFER_MER = ("atm", "withdrawal", "account transfer", "online transfer",
                      "to savings", "wire transfer")
def _is_transfer(t):
    """Self-transfer only (excluded from savings math). P2P-to-people is spend."""
    if _cat(t) in ("TRANSFER_OUT_ACCOUNT_TRANSFER", "TRANSFER_IN_ACCOUNT_TRANSFER",
                   "TRANSFER_OUT_WITHDRAWAL"):
        return True
    m = _mer(t)
    return any(k in m for k in _SELF_TRANSFER_MER)


def _is_income(t):
    """Real recurring income only — paychecks. Refunds, disputed/temporary
    credits, and P2P money-in are NOT income (they inflate the savings rate)."""
    if sc.is_outflow(t):
        return False
    cat = _cat(t)
    m = _mer(t)
    if "refund" in cat.lower() or "refund" in m:
        return False
    if re.search(r"temporary credit|provisional|adjustment|claim\s*#|dispute", m):
        return False
    if cat.startswith("TRANSFER_IN"):
        return False
    return cat.startswith("INCOME_") or "payroll" in m


# P2P sends to PEOPLE (Zelle/Venmo/Cash App/Apple Cash). These count as real
# spend in HISTORY (the money left), but the user has stopped doing them, so they
# must NOT be extrapolated into the forward savings-pace projection.
_P2P_MER = ("zelle", "venmo", "cash app", "apple cash")
def _is_p2p_send(t):
    if not sc.is_outflow(t):
        return False
    if _cat(t) == "TRANSFER_OUT_TRANSFER_OUT_FROM_APPS":
        return True
    m = _mer(t)
    return any(k in m for k in _P2P_MER)

_PERSONAL_SUBS = ("albert", "netflix", "dashp", "perplexity", "adobe", "elevenlabs", "spotify")

def matcher_for(leak):
    s = leak.lower()
    if "vending" in s:                      return lambda t: _cat(t) == "FOOD_AND_DRINK_VENDING_MACHINES"
    if "convenience" in s:                  return lambda t: _cat(t) == "GENERAL_MERCHANDISE_CONVENIENCE_STORES"
    if "restaurant" in s or "delivery" in s:return lambda t: _cat(t) == "FOOD_AND_DRINK_RESTAURANT"
    if "rideshare" in s:                    return lambda t: _cat(t) == "TRANSPORTATION_TAXIS_AND_RIDE_SHARES"
    if "vape" in s or "tobacco" in s or "nicotine" in s:
                                            return lambda t: _cat(t) == "GENERAL_MERCHANDISE_TOBACCO_AND_VAPE"
    if "claude" in s or "api" in s:         return lambda t: ("anthropic" in _mer(t) or "claude" in _mer(t))
    if "sub" in s:                          return lambda t: any(k in _mer(t) for k in _PERSONAL_SUBS)
    if "cash" in s or "transfer" in s:      return lambda t: _cat(t).startswith("TRANSFER_OUT")
    return None   # unmatched rule — reported, not scored


# ------------------------------- money math ---------------------------------
# Deterministic and unit-tested. Confidently-wrong math is the failure mode.

def months_between(d1, d2):
    return (d2.year - d1.year) * 12 + (d2.month - d1.month) + (d2.day - d1.day) / DAYS_PER_MONTH

def pace_status(actual, target):
    """on track / drifting / slipped vs a spending target (lower is better)."""
    if target <= 0:
        if actual <= 0.01: return "on track"
        return "drifting" if actual < 5 else "slipped"
    r = actual / target
    return "on track" if r <= 1.1 else ("drifting" if r <= 1.6 else "slipped")

def project_goal(running_total, monthly_rate, months_remaining, target, tailwind=0.0):
    """tailwind = extra savings banked when amortizing obligations end before the
    goal date (e.g. a car loan's payment disappearing after its payoff date)."""
    projected = running_total + monthly_rate * months_remaining + tailwind
    return {"projected": round(projected, 2),
            "pct": round(100 * running_total / target, 1) if target else 0.0,
            "status": "ahead" if projected >= target else "behind",
            "gap": round(target - projected, 2),
            "tailwind": round(tailwind, 2)}


def ending_obligation_tailwind(as_of, move_date):
    """Savings freed by registry obligations that end between now and the move:
    each contributes monthly_amount × (months from its end_date to the move)."""
    reg = oblreg.load_registry()
    total = 0.0
    freed = []
    for ob in reg.get("obligations", []):
        end = ob.get("end_date")
        if not end:
            continue
        try:
            ed = dt.date.fromisoformat(end)
        except ValueError:
            continue
        if as_of < ed < move_date:
            free_months = max(0.0, months_between(ed, move_date))
            amt = oblreg._monthly(ob) * free_months
            total += amt
            freed.append({"name": ob["name"], "ends": end,
                          "monthly": oblreg._monthly(ob),
                          "freed_total": round(amt, 2)})
    return round(total, 2), freed

def trailing_monthly_net(txns, as_of, n=3):
    """Avg net cash flow over the last n FULL calendar months before as_of's month.
    Smooths lumpy income (4- vs 5-paycheck months) and one-off spikes so the
    projection is signal, not a single noisy month."""
    inflow, outflow = defaultdict(float), defaultdict(float)
    for t in txns:
        d = sc.parse_date(t)
        if not d:
            continue
        amt = sc.amount_magnitude(t) or 0.0
        key = (d.year, d.month)
        if sc.is_outflow(t):
            # Exclude self-transfers AND P2P sends to people: the FORWARD pace
            # must reflect the user's current behavior, and he has stopped Zelling.
            # (Both still count as real spend in the running total / history.)
            if _is_transfer(t) or _is_p2p_send(t):
                continue
            outflow[key] += amt
        elif _is_income(t):
            inflow[key] += amt
    cur = (as_of.year, as_of.month)
    full = sorted(m for m in (set(inflow) | set(outflow)) if m < cur)[-n:]
    nets = [(f"{y:04d}-{mo:02d}", round(inflow[(y, mo)] - outflow[(y, mo)], 2)) for (y, mo) in full]
    avg = round(sum(v for _, v in nets) / len(nets), 2) if nets else 0.0
    return avg, nets


# ── Spending category engine (uses the Plaid category already on every txn) ──
# Raw PERSONAL_FINANCE_CATEGORY → human label. Fail loud on anything unmapped so a
# new Plaid category surfaces in tests instead of silently vanishing.
CATEGORY_LABELS = {
    "FOOD_AND_DRINK_RESTAURANT": "Restaurants",
    "FOOD_AND_DRINK_GROCERIES": "Groceries",
    "FOOD_AND_DRINK_VENDING_MACHINES": "Vending",
    "FOOD_AND_DRINK_FAST_FOOD": "Fast food",
    "FOOD_AND_DRINK_COFFEE": "Coffee",
    "FOOD_AND_DRINK_BEER_WINE_AND_LIQUOR": "Alcohol",
    "TRANSPORTATION_TAXIS_AND_RIDE_SHARES": "Rideshare",
    "TRANSPORTATION_GAS": "Gas",
    "TRAVEL_FLIGHTS": "Flights",
    "GENERAL_MERCHANDISE_TOBACCO_AND_VAPE": "Vape & tobacco",
    "GENERAL_MERCHANDISE_ELECTRONICS": "Electronics",
    "GENERAL_MERCHANDISE_CLOTHING_AND_ACCESSORIES": "Clothing",
    "GENERAL_MERCHANDISE_SUPERSTORES": "Superstores",
    "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES": "Online shopping",
    "GENERAL_MERCHANDISE_CONVENIENCE_STORES": "Convenience",
    "GENERAL_MERCHANDISE_BOOKSTORES_AND_NEWSSTANDS": "Books",
    "GENERAL_MERCHANDISE_DEPARTMENT_STORES": "Department stores",
    "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE": "Shopping",
    "GENERAL_SERVICES_OTHER_GENERAL_SERVICES": "Services",  # AI override below
    "GENERAL_SERVICES_INSURANCE": "Ankle monitor",
    "GENERAL_SERVICES_CONSULTING_AND_LEGAL": "Legal",
    "GENERAL_SERVICES_ACCOUNTING_AND_FINANCIAL_PLANNING": "Financial",
    "GOVERNMENT_AND_NON_PROFIT_GOVERNMENT_DEPARTMENTS_AND_AGENCIES": "Government / court",
    "MEDICAL_PHARMACIES_AND_SUPPLEMENTS": "Pharmacy",
    "PERSONAL_CARE_HAIR_AND_BEAUTY": "Personal care",
    "ENTERTAINMENT_MUSIC_AND_AUDIO": "Music",
    "ENTERTAINMENT_TV_AND_MOVIES": "Streaming",
    "LOAN_PAYMENTS_BNPL": "Klarna (BNPL)",
    "LOAN_PAYMENTS_OTHER_PAYMENT": "Loan payment",
    "BANK_FEES_FOREIGN_TRANSACTION_FEES": "Bank fees",
    "BANK_FEES_OTHER_BANK_FEES": "Bank fees",
}
# Merchants whose generic GENERAL_SERVICES_OTHER charges are really AI/dev tooling.
AI_MERCHANTS = ("anthropic", "claude", "vercel", "runpod", "supabase",
                "perplexity", "eleven", "cloudflare", "openai", "twilio")
# Discretionary categories worth flagging as cuttable "leaks".
LEAK_LABELS = {"Vape & tobacco", "Rideshare", "Fast food", "Alcohol"}


def _human_label(t):
    """Human category label for a transaction. Merchant-gated overrides keep
    Plaid's generic tags from being hardcoded to the wrong thing."""
    cat = _cat(t)
    mer = _mer(t)
    if cat == "GENERAL_SERVICES_OTHER_GENERAL_SERVICES" and any(m in mer for m in AI_MERCHANTS):
        return "Software & AI"
    if cat == "GENERAL_SERVICES_INSURANCE":
        return "Insurance"
    return CATEGORY_LABELS.get(cat)   # None = unmapped (fail loud in tests)


def _is_obligation(t, registry):
    """True only for FIXED commitments (an exact recurring amount, e.g. a music
    subscription or a car loan). Metered/usage-billed lines (e.g. cloud hosting)
    are variable and CUTTABLE — they count as discretionary, not as an
    untouchable obligation, so the lean budget can see them."""
    amt = sc.amount_magnitude(t)
    blob = _mer(t)
    for ob in (registry or {}).get("obligations", []):
        if ob.get("type") == "metered":
            continue                      # variable work-tooling — cuttable
        if not any(k in blob for k in (ob.get("match") or [ob["name"].lower()])):
            continue
        ex = ob.get("exact_amount")
        if ex is not None:
            if amt is not None and abs(amt - ex) < 0.01:
                return True               # the fixed subscription amount
            continue                      # same merchant, different amount = not this sub
        return True                       # fixed/amortizing obligation
    return False


# Necessities — discretionary but not "leaks" you'd cut for lean budget.
ESSENTIAL_LABELS = {"Groceries", "Gas", "Pharmacy", "Personal care"}
ONEOFF_MIN = 150.0   # a single big charge from a one-time merchant = not a habit
LEAK_MIN = 75.0      # discretionary non-essential above this in-window = a leak


def category_breakdown(txns, window, registry=None, top=8):
    """Where the money goes, from the Plaid category on every txn. Ranked human
    categories with $/%/kind, obligation/discretionary/transfer totals, and a
    SEPARATE list of one-time charges (a $1,249 fine is not a spending habit and
    must not inflate the budget). Recurring patterns vs one-offs are split so the
    category bars and the budget comparison reflect habits, not noise."""
    w_start = _to_date(window.get("start")) if isinstance(window, dict) else None
    w_end = _to_date(window.get("end")) if isinstance(window, dict) else None

    # One-off detection: a merchant seen only once across ALL history, charged big.
    merch_count = defaultdict(int)
    for t in txns:
        if sc.is_outflow(t) and not _is_transfer(t):
            merch_count[_mer(t)] += 1
    def _oneoff(t):
        return merch_count.get(_mer(t), 0) <= 1 and (sc.amount_magnitude(t) or 0) >= ONEOFF_MIN

    agg = defaultdict(lambda: {"amount": 0.0, "n": 0, "obl": 0.0,
                               "merch": defaultdict(float), "unmapped": None})
    transfers = spend = obligations = discretionary = 0.0
    one_offs = []
    for t in txns:
        if not sc.is_outflow(t):
            continue
        d = sc.parse_date(t)
        amt = sc.amount_magnitude(t) or 0.0
        if d is None or amt == 0:
            continue
        if w_start and w_end and not (w_start <= d <= w_end):
            continue
        if _is_transfer(t):
            transfers += amt
            continue
        if _oneoff(t):
            one_offs.append({"name": _human_label(t) or "One-time",
                             "merchant": (_mer(t).title()[:28] or "?"),
                             "amount": round(amt, 2), "date": str(d)})
            continue                      # one-time, not a habit — out of the bars/budget
        spend += amt
        label = _human_label(t) or f"[{_cat(t)}]"   # bracketed = unmapped, visible
        a = agg[label]
        a["amount"] += amt
        a["n"] += 1
        a["merch"][_mer(t).title()[:24] or "?"] += amt
        if _human_label(t) is None:
            a["unmapped"] = _cat(t)
        if _is_obligation(t, registry):
            a["obl"] += amt
            obligations += amt
        else:
            discretionary += amt
    rows = []
    for label, a in agg.items():
        if a["obl"] >= a["amount"] / 2:
            kind = "obligation"
        elif label not in ESSENTIAL_LABELS and a["amount"] >= LEAK_MIN:
            kind = "leak"                 # cuttable, derived from $ — not a fixed list
        else:
            kind = "discretionary"
        top_merch = max(a["merch"].items(), key=lambda x: x[1])[0] if a["merch"] else ""
        rows.append({"name": label, "amount": round(a["amount"], 2),
                     "pct": round(100 * a["amount"] / spend, 1) if spend else 0.0,
                     "kind": kind, "n": a["n"], "top_merchant": top_merch,
                     "unmapped": a["unmapped"]})
    rows.sort(key=lambda r: -r["amount"])
    head, tail = rows[:top], rows[top:]
    if tail:
        head.append({"name": f"Other ({len(tail)} categories)",
                     "amount": round(sum(r["amount"] for r in tail), 2),
                     "pct": round(100 * sum(r["amount"] for r in tail) / spend, 1) if spend else 0.0,
                     "kind": "discretionary", "n": sum(r["n"] for r in tail),
                     "top_merchant": "", "unmapped": None})
    one_offs.sort(key=lambda x: -x["amount"])
    return {"categories": head, "spend": round(spend, 2),
            "obligations": round(obligations, 2),
            "discretionary": round(discretionary, 2),
            "transfers": round(transfers, 2),
            "one_offs": one_offs[:6],
            "oneoff_total": round(sum(o["amount"] for o in one_offs), 2)}


def _to_date(s):
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return None


def monthly_breakdown(txns, as_of, n=4):
    """Last n FULL calendar months as {month, month_short, income, spend, net},
    transfers excluded — the real month-over-month series for the digest."""
    if isinstance(as_of, str):
        try:
            as_of = dt.date.fromisoformat(as_of[:10])
        except (ValueError, TypeError):
            as_of = dt.date.today()
    inflow, outflow = defaultdict(float), defaultdict(float)
    for t in txns:
        d = sc.parse_date(t)
        if not d:
            continue
        amt = sc.amount_magnitude(t) or 0.0
        key = (d.year, d.month)
        if sc.is_outflow(t):
            if _is_transfer(t):
                continue
            outflow[key] += amt
        elif _is_income(t):
            inflow[key] += amt
    cur = (as_of.year, as_of.month)
    full = sorted(m for m in (set(inflow) | set(outflow)) if m < cur)[-n:]
    out = []
    for (y, mo) in full:
        inc = round(inflow[(y, mo)], 2)
        sp = round(outflow[(y, mo)], 2)
        label = dt.date(y, mo, 1)
        out.append({"month": label.strftime("%B"), "month_short": label.strftime("%b"),
                    "ym": f"{y:04d}-{mo:02d}",
                    "income": inc, "spend": sp, "net": round(inc - sp, 2), "partial": False})
    # Current month-to-date as the LIVE point (snapshot can run any day).
    if cur in (set(inflow) | set(outflow)):
        inc = round(inflow[cur], 2)
        sp = round(outflow[cur], 2)
        label = dt.date(cur[0], cur[1], 1)
        out.append({"month": label.strftime("%B"), "month_short": label.strftime("%b"),
                    "ym": f"{cur[0]:04d}-{cur[1]:02d}",
                    "income": inc, "spend": sp, "net": round(inc - sp, 2),
                    "partial": True, "days_elapsed": as_of.day})
    return out


# ------------------------------- windowing ----------------------------------

def window_bounds(as_of, mode):
    if mode == "weekly":
        return as_of - dt.timedelta(days=6), as_of, "weekly"
    first_this = as_of.replace(day=1)
    end = first_this - dt.timedelta(days=1)      # last day of previous month
    start = end.replace(day=1)
    return start, end, "monthly"


# ------------------------------- core scoring -------------------------------

def build_summary(txns, R, mode, balance=None):
    dates = [sc.parse_date(t) for t in txns if sc.parse_date(t)]
    as_of = max(dates)
    w_start, w_end, _ = window_bounds(as_of, mode)
    in_win = lambda t: (d := sc.parse_date(t)) and w_start <= d <= w_end
    win = [t for t in txns if in_win(t)]

    div = WEEKS_PER_MONTH if mode == "weekly" else 1.0   # monthly→window scale

    # per-rule scoring (status vs the GOAL target; savings vs prior habit)
    rule_rows, baseline_win, actual_win, unmatched = [], 0.0, 0.0, []
    for r in R["rules"]:
        pred = matcher_for(r["leak"])
        if pred is None:
            unmatched.append(r["leak"]); continue
        spent = sum(sc.amount_magnitude(t) for t in win if sc.is_outflow(t) and pred(t))
        goal_win = r["target"] / div
        prior_win = r["prior"] / div
        baseline_win += prior_win
        actual_win += spent
        rule_rows.append({"leak": r["leak"], "spent": round(spent, 2),
                          "goal": round(goal_win, 2), "prior": round(prior_win, 2),
                          "status": pace_status(spent, goal_win)})

    # cash flow in window (real money)
    income_win = sum(sc.amount_magnitude(t) for t in win if _is_income(t))
    spend_win = sum(sc.amount_magnitude(t) for t in win if sc.is_outflow(t))
    net_win = income_win - spend_win

    # running "savings" since baseline = income − NON-transfer spend.
    # (No dedicated savings account in the feed; transfers/cash moves are excluded
    # so the figure reflects living-within-income, not money shuffled between pockets.)
    since_base = [t for t in txns if (d := sc.parse_date(t)) and d >= R["baseline_date"]]
    running = 0.0
    for t in since_base:
        amt = sc.amount_magnitude(t) or 0.0
        if sc.is_outflow(t):
            if _is_transfer(t):
                continue
            running -= amt
        elif _is_income(t):
            running += amt
    base_days = (as_of - R["baseline_date"]).days + 1

    # MONK-MODE forward plan: the savings rate is what the user will save going
    # forward = monthly income − fixed obligations − the chosen discretionary
    # budget. NOT the historical trailing average (which bakes in pre-budget
    # spending and is wrong for someone who has changed behavior).
    reg = oblreg.load_registry()
    inc_by_month = defaultdict(float)
    for t in txns:
        if _is_income(t):
            dd = sc.parse_date(t)
            if dd:
                inc_by_month[(dd.year, dd.month)] += sc.amount_magnitude(t) or 0.0
    cur_m = (as_of.year, as_of.month)
    full_inc = sorted(m for m in inc_by_month if m < cur_m)[-3:]
    monthly_income = round(sum(inc_by_month[m] for m in full_inc) / len(full_inc), 2) if full_inc else 0.0
    obl_floor = oblreg.obligation_floor_monthly(reg, as_of) if reg else 0.0
    discretionary_budget = round(float(R.get("ceiling") or 500.00), 2)
    forward_rate = round(monthly_income - obl_floor - discretionary_budget, 2)

    smoothed_rate, trailing = trailing_monthly_net(txns, as_of, 3)
    months_left = max(0.0, months_between(as_of, R["move_date"]))
    tailwind, freed = ending_obligation_tailwind(as_of, R["move_date"])
    # Project forward from the CURRENT balance (anchored to reality);
    # accumulate at the forward rate + the ending-obligation tailwind.
    current_balance = balance if balance is not None else 0.0
    goal = project_goal(current_balance, forward_rate, months_left, R["target"], tailwind)
    # What you'd need to save monthly, from now, to fully hit the goal.
    # Pace needed FROM NOW, crediting what's already saved + the freed-obligation
    # tailwind. The naive target/months_left ignored both, so "required" read ABOVE
    # "current" even when the projection cleared the goal (the AHEAD paradox).
    _remaining = max(0.0, R["target"] - current_balance - tailwind)
    goal["required_pace"] = (round(_remaining / months_left, 2)
                               if months_left > 0 else _remaining)
    goal["current_pace"] = forward_rate          # lean-budget monthly savings
    goal["monthly_income"] = monthly_income
    goal["obligation_floor"] = round(obl_floor, 2)
    goal["discretionary_budget"] = discretionary_budget
    goal["historical_pace"] = round(smoothed_rate, 2)   # kept for reference only
    goal["projection_basis"] = trailing
    goal["freed_obligations"] = freed
    # Monk-mode status: net-positive saving is "on track", even if the projected
    # total lands short of the full target. Only a non-positive rate is "behind".
    if forward_rate <= 0:
        goal["status"] = "behind"
    elif goal["projected"] >= R["target"]:
        goal["status"] = "ahead"
    else:
        goal["status"] = "saving"

    # flags (deterministic) -------------------------------------------------
    # new/recently-started recurring: group all txns, flag recurring streams whose
    # FIRST charge is within ~60 days and that also charged in the window.
    groups = defaultdict(list)
    for t in txns:
        if sc.is_outflow(t) and sc.amount_magnitude(t) and sc.parse_date(t):
            groups[sc.merchant_key(t)].append((sc.parse_date(t), sc.amount_magnitude(t), sc.display_name(t)))
    new_recurring = []
    for g in groups.values():
        if len(g) < 3: continue
        g.sort()
        gaps = [(g[i + 1][0] - g[i][0]).days for i in range(len(g) - 1)]
        cad, _ = sc.classify_cadence(gaps)
        if not cad: continue
        first, last = g[0][0], g[-1][0]
        if (as_of - first).days <= 60 and w_start <= last <= w_end:
            new_recurring.append({"merchant": g[-1][2], "cadence": cad,
                                  "amount": round(g[-1][1], 2), "since": str(first)})

    # duplicates in window: same merchant + amount within 3 days
    dups = []
    bykey = defaultdict(list)
    for t in win:
        if sc.is_outflow(t) and sc.parse_date(t):
            bykey[(sc.display_name(t), round(sc.amount_magnitude(t), 2))].append(sc.parse_date(t))
    for (name, amt), ds in bykey.items():
        ds.sort()
        for i in range(len(ds) - 1):
            if (ds[i + 1] - ds[i]).days <= 3:
                dups.append({"merchant": name, "amount": amt,
                             "dates": [str(ds[i]), str(ds[i + 1])]})
    # bank fees in window
    fees = [{"merchant": sc.display_name(t), "amount": round(sc.amount_magnitude(t), 2),
             "date": str(sc.parse_date(t))}
            for t in win if sc.is_outflow(t) and _cat(t).startswith("BANK_FEES")]

    statuses = [r["status"] for r in rule_rows]
    return {
        "mode": mode,
        "as_of": str(as_of),
        "window": {"start": str(w_start), "end": str(w_end)},
        "target": R["target"], "move_date": str(R["move_date"]),
        "baseline_date": str(R["baseline_date"]), "ceiling": R["ceiling"],
        "rules": rule_rows,
        "rule_tally": {"on_track": statuses.count("on track"),
                       "drifting": statuses.count("drifting"),
                       "slipped": statuses.count("slipped")},
        "unmatched_rules": unmatched,
        "discretionary": {"actual": round(actual_win, 2), "baseline": round(baseline_win, 2),
                          "saved_vs_habit": round(baseline_win - actual_win, 2)},
        "cashflow": {"income": round(income_win, 2), "spend": round(spend_win, 2),
                     "net_saved": round(net_win, 2)},
        "goal": {**goal,
                   # Running total is the current balance (the real starting
                   # point). Historical net is kept for the record.
                   "running_total": current_balance,
                   "historical_net": round(running, 2),
                   "days_since_baseline": base_days, "months_remaining": round(months_left, 1)},
        "flags": {"new_recurring": new_recurring[:6], "duplicates": dups[:6], "fees": fees[:6]},
    }


# ------------------------------- rendering ----------------------------------

# Status → icon for the scorecard rule list (statuses set in _rule_status()).
ICON = {"on track": "✅", "drifting": "⚠️", "slipped": "🔻"}


def render_scorecard(s):
    L = []
    head = "WEEKLY PULSE" if s["mode"] == "weekly" else "MONTHLY CHECK-IN"
    L.append(f"# finance.mcp — {head}")
    L.append(f"_window {s['window']['start']} → {s['window']['end']} · as of {s['as_of']}_\n")

    b = s["goal"]
    L.append("## Savings")
    L.append(f"- Saved this window (net): {money(s['cashflow']['net_saved'])} "
             f"(income {money(s['cashflow']['income'])} − spend {money(s['cashflow']['spend'])})")
    L.append(f"- Running total since {s['baseline_date']}: {money(b['running_total'])} "
             f"({b['pct']}% of {money(s['target'])} target)")
    L.append(f"- Pace (trailing 3-mo avg): {money(b['current_pace'])}/mo vs {money(b['required_pace'])}/mo needed")
    basis = b.get("projection_basis", [])
    if basis:
        L.append("  ↳ months: " + " · ".join(f"{m} {money(v)}" for m, v in basis))
    L.append(f"- Projected by {delivery.fmt_date(s['move_date'])}: {money(b['projected'])} → **{b['status'].upper()}** "
             f"(gap {money(b['gap'])}, {b['months_remaining']} mo left)\n")

    t = s["rule_tally"]
    L.append(f"## Cut rules  ({t['on_track']}✅ {t['drifting']}⚠️ {t['slipped']}🔻)")
    for r in s["rules"]:
        L.append(f"- {ICON[r['status']]} {r['leak']}: {money(r['spent'])} "
                 f"vs {money(r['goal'])} goal — {r['status']}")
    if s["unmatched_rules"]:
        L.append(f"- _(unscored, no matcher: {', '.join(s['unmatched_rules'])})_")
    d = s["discretionary"]
    L.append(f"\nDiscretionary this window: {money(d['actual'])} vs {money(d['baseline'])} habit "
             f"→ saved {money(d['saved_vs_habit'])}\n")

    f = s["flags"]
    L.append("## Flags")
    if f["new_recurring"]:
        for n in f["new_recurring"]:
            L.append(f"- 🆕 new recurring: {n['merchant']} {money(n['amount'])} {n['cadence']} (since {n['since']})")
    if f["duplicates"]:
        for x in f["duplicates"]:
            L.append(f"- ⚠️ duplicate: {x['merchant']} {money(x['amount'])} on {x['dates'][0]} & {x['dates'][1]}")
    if f["fees"]:
        for x in f["fees"]:
            L.append(f"- 💸 bank fee: {x['merchant']} {money(x['amount'])} {delivery.fmt_date(x['date'])}")
    if not (f["new_recurring"] or f["duplicates"] or f["fees"]):
        L.append("- none")
    return "\n".join(L)


# ------------------------------- narration ----------------------------------

def append_log(rules_path, mode, s):
    txt = open(rules_path, encoding="utf-8").read()
    lines = txt.splitlines()
    header = "WEEKLY PULSE LOG" if mode == "weekly" else "MONTHLY CHECK-IN LOG"
    if mode == "weekly":
        t = s["rule_tally"]
        row = (f"| {s['as_of']} | {t['on_track']}✅ {t['drifting']}⚠️ {t['slipped']}🔻 "
               f"| net {money(s['cashflow']['net_saved'])} "
               f"| saved {money(s['discretionary']['saved_vs_habit'])} vs habit |")
    else:
        b = s["goal"]
        ym = s["window"]["start"][:7]
        row = (f"| {ym} | spend {money(s['cashflow']['spend'])} "
               f"| {money(s['cashflow']['net_saved'])} | {money(b['running_total'])} "
               f"| {b['pct']}% | {money(b['projected'])} ({b['status']}) |")
    out, i, n = [], 0, len(lines)
    while i < n:
        out.append(lines[i])
        if header in lines[i]:
            j = i + 1
            last_tbl = i
            while j < n and not lines[j].startswith("#"):
                if lines[j].strip().startswith("|"):
                    last_tbl = j
                j += 1
            for k in range(i + 1, last_tbl + 1):
                out.append(lines[k])
            out.append(row)
            i = last_tbl
        i += 1
    open(rules_path, "w", encoding="utf-8").write("\n".join(out) + "\n")

def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--weekly", action="store_true")
    g.add_argument("--monthly", action="store_true")
    ap.add_argument("transactions")
    ap.add_argument("--rules", default="rules.md")
    ap.add_argument("--balance", type=float, default=None,
                    help="current account balance (anchors the projection)")
    ap.add_argument("--no-voice", action="store_true")
    ap.add_argument("--email", nargs="?", const="__self__", default=None)
    a = ap.parse_args()

    mode = "weekly" if a.weekly else "monthly"
    txns = sc.load_transactions(a.transactions)
    R = parse_rules(a.rules)
    s = build_summary(txns, R, mode, balance=a.balance)

    scorecard = render_scorecard(s)
    voice = None if a.no_voice else delivery.narrate(s, R["tone"], mode)
    report = scorecard + (("\n\n---\n\n## Read\n" + voice) if voice else "")

    if mode == "weekly":
        fname = f"report-weekly-{s['as_of']}.md"
    else:
        fname = f"report-monthly-{s['window']['start'][:7]}.md"
    open(fname, "w", encoding="utf-8").write(report + "\n")
    append_log(a.rules, mode, s)

    # headline to stdout
    b = s["goal"]
    print(report)
    print(f"\n[saved {fname} · logged to {a.rules} · "
          f"{'voice ON' if voice else 'NO-VOICE ($0)'}]")
    print(f"[HEADLINE] net {money(s['cashflow']['net_saved'])} this {mode} · "
          f"{b['pct']}% to {money(s['target'])} · projected {money(b['projected'])} ({b['status']})")

    if a.email is not None:
        to = None if a.email == "__self__" else a.email
        subj = f"finance.mcp — {'Weekly Pulse' if mode=='weekly' else 'Monthly Check-In'} {s['window']['end']}"
        delivery.send_email(to, subj, report)


if __name__ == "__main__":
    main()
