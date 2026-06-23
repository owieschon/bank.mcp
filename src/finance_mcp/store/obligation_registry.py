#!/usr/bin/env python3
"""obligation_registry.py — the forward-plan registry of real commitments.

The forecast must model what WILL leave the account (the obligations the user is
committed to) + a discretionary budget they CHOOSE — not an extrapolation of past habits.
This module loads obligations.json (a curated, confirmed list) and adapts it into
the exact stream shape cashflow_forecaster.project()/roll_forward() already
consume, so the projection engine and its tests stay untouched.

Obligation types:
  fixed      — exact recurring amount (e.g. a music subscription)
  metered    — committed but variable; anchored at a chosen recent run-rate, never
               the lifetime mean or an onboarding burst (e.g. usage-billed cloud hosting)
  amortizing — a debt/term that ENDS (e.g. a car loan with a payoff date)
"""
import json
import os
import datetime as dt

from finance_mcp.store import subscription_creep as sc

REGISTRY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "obligations.json")


def load_registry(path=REGISTRY_PATH):
    """Load the confirmed obligation registry. Returns {} if absent."""
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _blob(t):
    return ((t.get("merchantName") or "") + " " + (t.get("description") or "")).lower()


def _matches(t, ob):
    """Does transaction t belong to obligation ob (for anchoring last_date)?"""
    if not sc.is_outflow(t):
        return False
    keys = ob.get("match") or [ob["name"].lower()]
    if not any(k in _blob(t) for k in keys):
        return False
    ex = ob.get("exact_amount")
    amt = sc.amount_magnitude(t)
    if ex is not None:
        return amt is not None and abs(amt - ex) < 0.01
    return True


def _last_date(txns, ob, as_of):
    """Most recent matching charge date, or as_of if none seen yet."""
    dates = [sc.parse_date(t) for t in txns if _matches(t, ob)]
    dates = [d for d in dates if d and d <= as_of]
    return max(dates) if dates else as_of


def registry_to_streams(registry, txns, as_of):
    """Adapt registry obligations into project()-compatible outflow streams.

    Amounts come from the registry (the PLAN), not from summing history. last_date
    is anchored to the real most-recent charge so cadence projection lands on the
    right days. Amortizing lines past their end_date are dropped.
    """
    streams = []
    for ob in registry.get("obligations", []):
        end = ob.get("end_date")
        if end:
            try:
                if dt.date.fromisoformat(end) < as_of:
                    continue   # already paid off / expired
            except ValueError:
                end = None
        cadence = ob.get("cadence", "monthly")
        streams.append({
            "merchant": ob["name"],
            "cadence": cadence,
            "avg_amount": float(ob["amount"]),
            "last_amount": float(ob["amount"]),
            "last_date": _last_date(txns, ob, as_of),
            "direction": "outflow",
            "is_active": True,
            "key": ["registry", ob["name"]],
            "end_date": end,
            "ob_type": ob.get("type", "fixed"),
        })
    return streams


def _monthly(ob):
    per = float(ob["amount"])
    return round(per * (30.0 / sc.CADENCE_DAYS.get(ob.get("cadence", "monthly"), 30)), 2)


def obligation_floor_monthly(registry, as_of=None):
    """Total committed $/mo from the registry (amortizing lines still active)."""
    as_of = as_of or dt.date.today()
    total = 0.0
    for ob in registry.get("obligations", []):
        end = ob.get("end_date")
        if end:
            try:
                if dt.date.fromisoformat(end) < as_of:
                    continue
            except ValueError:
                pass
        total += _monthly(ob)
    return round(total, 2)
