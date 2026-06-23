#!/usr/bin/env python3
"""Hand-checked unit tests for the money math. Run: python3 test_budget_scorer.py"""
import datetime as dt
from finance_mcp.engines import budget_scorer as bs

def approx(a, b, tol=0.05): return abs(a - b) <= tol

def test_project_goal():
    # running 5000 + 2400/mo * 10 mo = 29,000 vs 30k target -> behind, gap 1000
    r = bs.project_goal(5000, 2400, 10, 30000)
    assert r["projected"] == 29000, r
    assert r["status"] == "behind", r
    assert r["gap"] == 1000, r
    assert approx(r["pct"], 16.7), r
    # running 2000 + 2600/mo * 13 = 35,800 -> ahead, negative gap
    r = bs.project_goal(2000, 2600, 13, 30000)
    assert r["projected"] == 35800 and r["status"] == "ahead" and r["gap"] == -5800, r
    print("ok  project_goal")

def test_pace_status():
    assert bs.pace_status(20, 22) == "on track"          # under goal
    assert bs.pace_status(23, 22) == "on track"          # within 10%
    assert bs.pace_status(30, 22) == "drifting"          # 1.36x
    assert bs.pace_status(50, 22) == "slipped"           # 2.27x
    assert bs.pace_status(0, 0) == "on track"            # zero goal, zero spend
    assert bs.pace_status(3, 0) == "drifting"            # zero goal, tiny spend
    assert bs.pace_status(10, 0) == "slipped"            # zero goal, real spend
    print("ok  pace_status")

def test_months_between():
    # 2026-06-15 -> 2027-07-01 ≈ 12.54 months
    m = bs.months_between(dt.date(2026, 6, 15), dt.date(2027, 7, 1))
    assert approx(m, 12.54, 0.1), m
    # exact one year
    assert approx(bs.months_between(dt.date(2026, 1, 1), dt.date(2027, 1, 1)), 12.0, 0.01)
    print("ok  months_between")

def test_window_bounds():
    asof = dt.date(2026, 6, 15)
    s, e, _ = bs.window_bounds(asof, "weekly")
    assert s == dt.date(2026, 6, 9) and e == asof, (s, e)
    s, e, _ = bs.window_bounds(asof, "monthly")
    assert s == dt.date(2026, 5, 1) and e == dt.date(2026, 5, 31), (s, e)
    print("ok  window_bounds")

def test_trailing_net():
    inc = "INCOME_WAGES"   # only real income counts toward net now
    txns = [
        {"type": "credit", "category": inc, "date": "2026-03-10", "rawData": {"amount": 1000, "date": "2026-03-10"}},
        {"type": "debit",  "date": "2026-03-15", "rawData": {"amount": 400,  "date": "2026-03-15"}},
        {"type": "credit", "category": inc, "date": "2026-04-10", "rawData": {"amount": 1000, "date": "2026-04-10"}},
        {"type": "debit",  "date": "2026-04-15", "rawData": {"amount": 600,  "date": "2026-04-15"}},
        {"type": "debit",  "date": "2026-05-15", "rawData": {"amount": 2000, "date": "2026-05-15"}},
        {"type": "debit",  "date": "2026-06-05", "rawData": {"amount": 50,   "date": "2026-06-05"}},  # current partial, excluded
    ]
    avg, nets = bs.trailing_monthly_net(txns, dt.date(2026, 6, 15), 3)
    assert dict(nets) == {"2026-03": 600.0, "2026-04": 400.0, "2026-05": -2000.0}, nets
    assert approx(avg, -333.33), avg
    print("ok  trailing_monthly_net")

if __name__ == "__main__":
    test_project_goal(); test_pace_status(); test_months_between(); test_window_bounds()
    test_trailing_net()
    print("\nALL PASS")
