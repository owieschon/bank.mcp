#!/usr/bin/env python3
"""
merchant_categorizer.py — PRECISE merchant categorization without an MCC.

The bank feed has no Merchant Category Code, so we refine Plaid's coarse
personal-finance-category (PFC) using everything else the row carries. The
public entry point is:

    get_category(t) -> {
        "category":   <PFC-detailed string>,
        "confidence": "high" | "medium" | "low",
        "source":     "override" | "counterparty" | "pfc" | "heuristic",
    }

Precedence (first match wins):
  1. OVERRIDE     a user-set category keyed by sc.merchant_key(t). Always high.
  2. COUNTERPARTY a strong rawData counterparty (high confidence_level + a known
                  type/name with a real vertical) maps to a sensible category —
                  e.g. DoorDash (marketplace) -> restaurant delivery, Instacart
                  -> groceries, Toast (payment_terminal) -> restaurant, Klarna /
                  Albert (financial_institution) -> loan / financial. Only fires
                  when the counterparty actually implies a vertical AND
                  disagrees-or-confirms the PFC; generic acquirers (Square) are
                  deliberately NOT treated as a category signal.
  3. PFC          rawData.personal_finance_category.detailed, else top-level
                  category. This is the normal path.
  4. HEURISTIC    a descriptor keyword guess when no PFC is present at all.

Persistent learned overrides live at merchant_overrides.json
(merchant_key-string -> category). The CLI --set adds/updates one.

ARCHITECTURE: pure deterministic compute. No model is ever called here; refined
categories are a precise, auditable input that other tools adopt by importing
get_category. Reuses subscription_creep for all field extraction / merchant
keying so the money/date/merchant logic is never forked.
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict

from finance_mcp.store import subscription_creep as sc

OVERRIDES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "merchant_overrides.json")


# ----------------------------- field helpers --------------------------------

def _pfc(t):
    """PFC detailed string. rawData.personal_finance_category.detailed is the
    authoritative source; fall back to the top-level mirrored `category`, then
    rawData.category (often null in this feed)."""
    r = sc.raw(t)
    pfc = r.get("personal_finance_category") or {}
    return (pfc.get("detailed")
            or t.get("category")
            or r.get("category")
            or "")


def _pfc_confidence(t):
    """Plaid's own confidence in its PFC label, if present."""
    pfc = sc.raw(t).get("personal_finance_category") or {}
    return (pfc.get("confidence_level") or "").upper()


def _counterparties(t):
    return sc.raw(t).get("counterparties") or []


def _descriptor(t):
    r = sc.raw(t)
    return (r.get("name") or t.get("description")
            or t.get("merchantName") or r.get("merchant_name") or "")


# ------------------------- counterparty knowledge ---------------------------
# A counterparty implies a category ONLY when it carries a real vertical. Keyed
# by (type, normalized-name). Value = the PFC-detailed category it implies.
#
# Deliberately EXCLUDED: generic acquirers / processors that sit on top of any
# kind of merchant and therefore carry no vertical signal of their own —
# Square (seen on tobacco, coffee, consulting...), bank-of-america withdrawals,
# Apple Cash / Zelle / Venmo (peer transfers, already well-labeled by PFC).
COUNTERPARTY_CATEGORY = {
    ("marketplace", "doordash"):          "FOOD_AND_DRINK_RESTAURANT",
    ("marketplace", "instacart"):         "FOOD_AND_DRINK_GROCERIES",
    ("marketplace", "google play store"): "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES",
    ("marketplace", "google play"):       "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES",
    ("payment_terminal", "toast"):        "FOOD_AND_DRINK_RESTAURANT",
    ("financial_institution", "klarna"):  "LOAN_PAYMENTS_BNPL",
    ("financial_institution", "albert"):  "GENERAL_SERVICES_ACCOUNTING_AND_FINANCIAL_PLANNING",
}


def _counterparty_signal(t):
    """Return (category, cp_name) from the strongest qualifying counterparty, or
    None. Qualifies when: confidence_level is HIGH/VERY_HIGH AND the (type,name)
    is in COUNTERPARTY_CATEGORY. Marketplaces/terminals are preferred over the
    base merchant because they carry the cross-merchant vertical."""
    best = None
    for cp in _counterparties(t):
        conf = (cp.get("confidence_level") or "").upper()
        if conf not in ("HIGH", "VERY_HIGH"):
            continue
        key = ((cp.get("type") or "").lower(), sc.normalize(cp.get("name") or "").lower())
        # normalize() may strip too aggressively; also try the raw lowered name
        raw_name = (cp.get("name") or "").strip().lower()
        cat = COUNTERPARTY_CATEGORY.get(key) or COUNTERPARTY_CATEGORY.get(
            ((cp.get("type") or "").lower(), raw_name))
        if cat is None:
            continue
        # prefer non-merchant verticals (marketplace/terminal) — they win ties
        rank = 0 if cp.get("type") in ("marketplace", "payment_terminal",
                                       "financial_institution") else 1
        if best is None or rank < best[0]:
            best = (rank, cat, cp.get("name"))
    return (best[1], best[2]) if best else None


# ------------------------------- heuristics ---------------------------------
# Last-resort descriptor keyword guesses, used ONLY when no PFC exists at all.
_HEURISTICS = [
    (("liquor", "wine", "beer", "brew", "beverage", "tavern", "spirits"),
     "FOOD_AND_DRINK_BEER_WINE_AND_LIQUOR"),
    (("uber", "lyft", "ride"),         "TRANSPORTATION_TAXIS_AND_RIDE_SHARES"),
    (("doordash", "grubhub", "restaurant", "pizza", "grill", "kitchen"),
     "FOOD_AND_DRINK_RESTAURANT"),
    (("grocery", "supermarket", "market", "foods"), "FOOD_AND_DRINK_GROCERIES"),
    (("gas", "fuel", "speedway", "getgo", "shell", "exxon"), "TRANSPORTATION_GAS"),
    (("vape", "smoke", "tobacco"), "GENERAL_MERCHANDISE_TOBACCO_AND_VAPE"),
    (("pharmacy", "cvs", "walgreens", "rx"), "MEDICAL_PHARMACIES_AND_SUPPLEMENTS"),
    (("zelle", "venmo", "cash app", "transfer"), "TRANSFER_OUT_TRANSFER_OUT_FROM_APPS"),
]


def _heuristic(t):
    d = _descriptor(t).lower()
    for needles, cat in _HEURISTICS:
        if any(n in d for n in needles):
            return cat
    return None


# ------------------------------- overrides ----------------------------------

def _key_str(t):
    """Stable string form of sc.merchant_key for the overrides JSON."""
    kind, val = sc.merchant_key(t)
    return f"{kind}:{val}"


def load_overrides(path=OVERRIDES_PATH):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_overrides(overrides, path=OVERRIDES_PATH):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(overrides, f, indent=2, sort_keys=True)
        f.write("\n")


def set_override(substring, category, txns=None, path=OVERRIDES_PATH):
    """Add/update an override. The CLI passes a MERCHANT SUBSTRING; we resolve it
    to the concrete merchant_key(s) it matches in `txns` so the persisted key is
    the same one get_category looks up. If no txns match (or none supplied), we
    persist a literal name-key fallback so the intent is still recorded."""
    overrides = load_overrides(path)
    matched = []
    if txns:
        sub = substring.lower()
        seen = set()
        for t in txns:
            name = sc.display_name(t).lower()
            desc = _descriptor(t).lower()
            if sub in name or sub in desc:
                k = _key_str(t)
                if k not in seen:
                    seen.add(k)
                    matched.append((k, sc.display_name(t)))
    if not matched:
        # fall back to a normalized name key so future txns with that name hit it
        matched = [(f"name:{sc.normalize(substring)}", substring)]
    for k, _name in matched:
        overrides[k] = category
    save_overrides(overrides, path)
    return matched


# ----------------------------- core resolver --------------------------------

def get_category(t, overrides=None):
    """Refine the category for one transaction. See module docstring for the
    precedence. Returns {category, confidence, source}. Importable so other
    tools adopt refined categories instead of raw PFC."""
    if overrides is None:
        overrides = load_overrides()

    # 1) user override
    ov = overrides.get(_key_str(t))
    if ov:
        return {"category": ov, "confidence": "high", "source": "override"}

    pfc = _pfc(t)

    # 2) strong counterparty vertical
    sig = _counterparty_signal(t)
    if sig is not None:
        cat, _cp = sig
        # high when the counterparty confirms PFC or PFC is empty; still high
        # when it overrides PFC, but flagged as a conflict by the review queue.
        return {"category": cat, "confidence": "high", "source": "counterparty"}

    # 3) Plaid PFC
    if pfc:
        conf = "high" if _pfc_confidence(t) in ("HIGH", "VERY_HIGH") else "medium"
        return {"category": pfc, "confidence": conf, "source": "pfc"}

    # 4) descriptor heuristic
    h = _heuristic(t)
    if h:
        return {"category": h, "confidence": "low", "source": "heuristic"}

    return {"category": "GENERAL_SERVICES_OTHER_GENERAL_SERVICES",
            "confidence": "low", "source": "heuristic"}


# ----------------------------- review flagging ------------------------------

REVIEW_CATEGORIES = {
    # The user does NOT drink — any alcohol category is surfaced for review.
    "FOOD_AND_DRINK_BEER_WINE_AND_LIQUOR": "alcohol (user does not drink)",
}


def conflict_for(t, overrides=None):
    """If a strong counterparty vertical DISAGREES with the PFC, describe it.
    Returns a string reason or None. Override rows are never conflicts."""
    if overrides is None:
        overrides = load_overrides()
    if overrides.get(_key_str(t)):
        return None
    sig = _counterparty_signal(t)
    if sig is None:
        return None
    cp_cat, cp_name = sig
    pfc = _pfc(t)
    if pfc and pfc != cp_cat:
        return f"counterparty {cp_name} -> {cp_cat} but PFC says {pfc}"
    return None


# --------------------------------- CLI --------------------------------------

def _build_report(txns, overrides):
    """Deterministic aggregation for the CLI. Returns a summary-dict-shaped
    structure (no raw rows) plus per-merchant detail."""
    # per-merchant refined category (majority vote of refined results)
    by_merchant = defaultdict(lambda: {"name": None, "n": 0,
                                        "refined": Counter(), "raw_pfc": Counter(),
                                        "sources": Counter(), "confidences": Counter(),
                                        "alcohol_total": 0.0})
    moved = 0           # txns whose refined category != raw PFC
    total_outflow_alcohol = 0.0
    review = []         # list of dicts for the review queue
    n_scored = 0

    for t in txns:
        if not sc.is_outflow(t):
            continue
        amt = sc.amount_magnitude(t)
        if amt is None:
            continue
        n_scored += 1
        k = _key_str(t)
        res = get_category(t, overrides)
        raw_pfc = _pfc(t)
        m = by_merchant[k]
        m["name"] = sc.display_name(t)
        m["n"] += 1
        m["refined"][res["category"]] += 1
        m["raw_pfc"][raw_pfc] += 1
        m["sources"][res["source"]] += 1
        m["confidences"][res["confidence"]] += 1
        if res["category"] != raw_pfc and raw_pfc:
            moved += 1
        # Alcohol is surfaced on the RAW PFC label, not the refined one: a Toast
        # tavern tab that refinement re-files as RESTAURANT must still appear in
        # the review total. The user does not drink — nothing alcohol-tagged
        # should be silently reclassified out of view.
        if "BEER_WINE_AND_LIQUOR" in (raw_pfc or "") or "BEER_WINE_AND_LIQUOR" in res["category"]:
            m["alcohol_total"] += amt
            total_outflow_alcohol += amt

    # finalize per-merchant + build review queue
    merchants = []
    for k, m in by_merchant.items():
        refined_cat = m["refined"].most_common(1)[0][0]
        raw_cat = m["raw_pfc"].most_common(1)[0][0] if m["raw_pfc"] else ""
        # representative confidence/source = most common
        conf = m["confidences"].most_common(1)[0][0]
        src = m["sources"].most_common(1)[0][0]
        merchants.append({
            "key": k, "name": m["name"], "n": m["n"],
            "category": refined_cat, "raw_pfc": raw_cat,
            "confidence": conf, "source": src,
            "alcohol_total": round(m["alcohol_total"], 2),
        })

    merchants.sort(key=lambda x: (-x["n"], x["name"].lower()))

    # review queue: low confidence, conflicts, or suspicious (alcohol) categories
    seen_conflict = set()
    for t in txns:
        if not sc.is_outflow(t):
            continue
        k = _key_str(t)
        res = get_category(t, overrides)
        reasons = []
        if res["confidence"] == "low":
            reasons.append(f"low confidence ({res['source']})")
        c = conflict_for(t, overrides)
        if c and k not in seen_conflict:
            reasons.append(c)
            seen_conflict.add(k)
        elif c:
            continue  # already reported this merchant's conflict
        raw_pfc = _pfc(t)
        if res["category"] in REVIEW_CATEGORIES:
            reasons.append(REVIEW_CATEGORIES[res["category"]])
        elif raw_pfc in REVIEW_CATEGORIES:
            # alcohol per PFC even though refinement re-filed it (e.g. Toast bar)
            reasons.append(REVIEW_CATEGORIES[raw_pfc] +
                           f"; refined to {res['category']}")
        if reasons:
            review.append({
                "merchant": sc.display_name(t),
                "date": str(sc.parse_date(t)),
                "amount": round(sc.amount_magnitude(t) or 0.0, 2),
                "category": res["category"],
                "reasons": reasons,
            })

    return {
        "tool": "merchant_categorizer",
        "n_outflows": n_scored,
        "n_merchants": len(merchants),
        "reclassified": moved,
        "reclassified_pct": round(100 * moved / n_scored, 1) if n_scored else 0.0,
        "alcohol_total": round(total_outflow_alcohol, 2),
        "merchants": merchants,
        "review": review,
    }


def _print_report(rep):
    money = sc.money
    print("\n" + "=" * 72)
    print(" MERCHANT -> REFINED CATEGORY")
    print("=" * 72)
    print(f" {'merchant':<26} {'category':<42} {'n':>3} {'conf':<7} src")
    print(" " + "-" * 70)
    for m in rep["merchants"]:
        flag = " *" if m["category"] != m["raw_pfc"] and m["raw_pfc"] else ""
        print(f" {m['name'][:26]:<26} {m['category'][:42]:<42} {m['n']:>3} "
              f"{m['confidence']:<7} {m['source']}{flag}")

    print("\n" + "=" * 72)
    print(" REVIEW QUEUE  (low-confidence / conflicts / suspicious)")
    print("=" * 72)
    if not rep["review"]:
        print("  nothing flagged.")
    else:
        for r in rep["review"]:
            print(f"\n  {r['merchant']}  {money(r['amount'])}  {r['date']}")
            print(f"    category: {r['category']}")
            for reason in r["reasons"]:
                print(f"    - {reason}")
        if rep["alcohol_total"] > 0:
            print(f"\n  Alcohol (BEER_WINE_AND_LIQUOR) total flagged for review: "
                  f"{money(rep['alcohol_total'])}  — the user does not drink.")

    print("\n" + "=" * 72)
    print(" RECLASSIFICATION STATS")
    print("=" * 72)
    print(f"  outflow transactions scored : {rep['n_outflows']}")
    print(f"  distinct merchants          : {rep['n_merchants']}")
    print(f"  refined != raw PFC          : {rep['reclassified']} "
          f"({rep['reclassified_pct']}% of outflows)")
    print(f"  alcohol surfaced for review : {sc.money(rep['alcohol_total'])}")
    print()


def main():
    ap = argparse.ArgumentParser(description="Precise merchant categorization (no MCC).")
    ap.add_argument("transactions", nargs="?", help="transactions JSON path")
    ap.add_argument("--set", metavar="MERCHANT_SUBSTRING=CATEGORY",
                    help="add/update a persistent override, e.g. "
                         "--set \"Cloud=ENTERTAINMENT_TV_AND_MOVIES\"")
    ap.add_argument("--overrides", default=OVERRIDES_PATH, help="overrides JSON path")
    a = ap.parse_args()

    txns = sc.load_transactions(a.transactions) if a.transactions else None

    if a.set:
        if "=" not in a.set:
            sys.exit("--set expects MERCHANT_SUBSTRING=CATEGORY")
        sub, cat = a.set.split("=", 1)
        sub, cat = sub.strip(), cat.strip()
        if not sub or not cat:
            sys.exit("--set expects a non-empty substring and category")
        matched = set_override(sub, cat, txns, a.overrides)
        print(f"override set: \"{sub}\" -> {cat}")
        for k, name in matched:
            print(f"  keyed {k}  ({name})")
        print(f"  saved to {a.overrides}")
        if not txns:
            return

    if not txns:
        ap.error("transactions JSON path is required (unless only using --set)")

    overrides = load_overrides(a.overrides)
    rep = _build_report(txns, overrides)
    _print_report(rep)


if __name__ == "__main__":
    main()
