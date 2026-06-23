#!/usr/bin/env python3
"""
receipt_scanner.py — email receipt scanner for the finance.mcp suite.

Scans Gmail for transaction-related emails (purchase receipts, bank
notifications, bill payment confirmations) and extracts structured data
for reconciliation against the local ledger.

DATA FLOW:
  Gmail (search_threads + get_thread)
    → parse email body (deterministic regex extraction)
    → structured receipt records {date, amount, merchant, category, source}
    → cross-reference against ledger_store.json
    → flag unmatched receipts (things not yet in bank data)
    → summary report

ARCHITECTURE (load-bearing):
  - Raw email content NEVER enters a model prompt.
  - All extraction is deterministic regex/pattern matching.
  - The model is only used (optionally, via delivery.narrate) to narrate the
    final compact summary dict.
  - Gmail access via the connected Gmail MCP (search_threads, get_thread).
  - Falls back gracefully if Gmail MCP is unavailable.

Runs as part of the daily digest (sync.py) or on-demand.
"""

import datetime as dt
import json
import re
from collections import defaultdict

from finance_mcp.store import subscription_creep as sc
from finance_mcp.store import db
from finance_mcp.engines import llm_matcher


# ----------------------------- receipt patterns -------------------------------
# Each pattern group targets a specific email type. Patterns extract (amount,
# merchant, date) from the email body/subject using pure regex — no model.

# Amount patterns: $12.34, USD 12.34, 12,345.67, etc.
_AMT_RE = re.compile(
    r"\$\s*([\d,]+\.?\d*)"           # $12.34 or $1,234.56
    r"|USD\s*([\d,]+\.?\d*)"         # USD 12.34
    r"|([\d,]+\.\d{2})\s*(?:USD|dollars?)", re.I
)

# Date patterns in email bodies
_DATE_PATTERNS = [
    (re.compile(r"(\w+ \d{1,2},\s*\d{4})"), "%B %d, %Y"),         # June 15, 2026
    (re.compile(r"(\w+ \d{1,2},\s*\d{4})"), "%b %d, %Y"),         # Jun 15, 2026
    (re.compile(r"(\w+ \d{1,2} \d{4})"), "%B %d %Y"),             # June 15 2026
    (re.compile(r"(\w+ \d{1,2} \d{4})"), "%b %d %Y"),             # Jun 15 2026
    (re.compile(r"(\d{1,2}/\d{1,2}/\d{2,4})"), "%m/%d/%Y"),       # 06/15/2026
    (re.compile(r"(\d{4}-\d{2}-\d{2})"), "%Y-%m-%d"),             # 2026-06-15
    (re.compile(r"(\d{1,2}-\w{3}-\d{2,4})"), "%d-%b-%Y"),        # 15-Jun-2026
]

# Merchant/sender patterns for known receipt senders
RECEIPT_SENDERS = {
    # E-commerce
    "amazon": {"keywords": ["order", "shipment", "delivery", "purchase"],
               "category": "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES"},
    "apple": {"keywords": ["receipt", "invoice", "subscription", "purchase"],
              "category": "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES"},
    "google": {"keywords": ["receipt", "payment", "subscription", "purchase"],
               "category": "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES"},
    "paypal": {"keywords": ["receipt", "payment", "transaction"],
               "category": "TRANSFER_OUT_TRANSFER_OUT_FROM_APPS"},

    # Subscriptions
    "netflix": {"keywords": ["billing", "payment", "charge"],
                "category": "ENTERTAINMENT_TV_AND_MOVIES"},
    "spotify": {"keywords": ["receipt", "payment", "premium"],
                "category": "ENTERTAINMENT_MUSIC_AND_AUDIO"},
    "adobe": {"keywords": ["invoice", "subscription", "payment"],
              "category": "GENERAL_MERCHANDISE_SOFTWARE"},
    "anthropic": {"keywords": ["invoice", "billing", "usage"],
                  "category": "GENERAL_MERCHANDISE_SOFTWARE"},

    # Delivery / food
    "doordash": {"keywords": ["order", "receipt", "delivery"],
                 "category": "FOOD_AND_DRINK_RESTAURANT"},
    "uber": {"keywords": ["trip", "receipt", "ride", "eats"],
             "category": "TRANSPORTATION_TAXIS_AND_RIDE_SHARES"},
    "instacart": {"keywords": ["order", "receipt", "delivery"],
                  "category": "FOOD_AND_DRINK_GROCERIES"},
    "grubhub": {"keywords": ["order", "receipt", "delivery"],
                "category": "FOOD_AND_DRINK_RESTAURANT"},

    # Banks / financial
    "chase": {"keywords": ["alert", "transaction", "payment", "deposit", "withdrawal"],
              "category": "BANK_FEES_OTHER_BANK_FEES"},
    "wells fargo": {"keywords": ["alert", "transaction", "deposit"],
                    "category": "BANK_FEES_OTHER_BANK_FEES"},
    "bank of america": {"keywords": ["alert", "transaction", "payment"],
                        "category": "BANK_FEES_OTHER_BANK_FEES"},
    "venmo": {"keywords": ["paid", "charged", "completed"],
              "category": "TRANSFER_OUT_TRANSFER_OUT_FROM_APPS"},
    "zelle": {"keywords": ["payment", "sent", "received"],
              "category": "TRANSFER_OUT_TRANSFER_OUT_FROM_APPS"},

    # Bills / utilities
    "at&t": {"keywords": ["bill", "payment", "statement"],
             "category": "RENT_AND_UTILITIES_TELEPHONE"},
    "verizon": {"keywords": ["bill", "payment", "statement"],
                "category": "RENT_AND_UTILITIES_TELEPHONE"},
    "comcast": {"keywords": ["bill", "payment", "statement"],
                "category": "RENT_AND_UTILITIES_INTERNET_AND_CABLE"},
    "spectrum": {"keywords": ["bill", "payment", "statement"],
                 "category": "RENT_AND_UTILITIES_INTERNET_AND_CABLE"},
}

# Bank notification patterns (subject lines)
BANK_ALERT_PATTERNS = [
    re.compile(r"(?:direct\s+)?deposit.*?\$?([\d,]+\.?\d*)", re.I),
    re.compile(r"withdrawal.*?\$?([\d,]+\.?\d*)", re.I),
    re.compile(r"transfer.*?\$?([\d,]+\.?\d*)", re.I),
    re.compile(r"payment.*?\$?([\d,]+\.?\d*)", re.I),
    re.compile(r"transaction.*?\$?([\d,]+\.?\d*)", re.I),
    re.compile(r"charge.*?\$?([\d,]+\.?\d*)", re.I),
]


# ----------------------------- extraction helpers -----------------------------

def extract_amount(text):
    """Extract the first dollar amount from text. Returns float or None."""
    if not text:
        return None
    m = _AMT_RE.search(text)
    if not m:
        return None
    raw = next(g for g in m.groups() if g)
    try:
        return float(raw.replace(",", ""))
    except (ValueError, TypeError):
        return None


def extract_date(text, fallback=None):
    """Extract a date from text. Returns date or fallback."""
    if not text:
        return fallback
    for pattern, fmt in _DATE_PATTERNS:
        m = pattern.search(text)
        if m:
            try:
                raw = m.group(1)
                d = dt.datetime.strptime(raw, fmt).date()
                if d.year < 100:
                    d = d.replace(year=d.year + 2000)
                return d
            except ValueError:
                continue
    return fallback


def extract_date_from_email_date(date_str):
    """Parse an email Date header into a date object."""
    if not date_str:
        return None

    # Try Python's email.utils first (handles RFC2822 properly)
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(date_str).date()
    except Exception:
        pass

    # Try common email date formats
    for fmt in [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S",
        "%d %b %Y %H:%M:%S %z",
        "%d %b %Y %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ]:
        try:
            return dt.datetime.strptime(date_str.strip()[:30], fmt).date()
        except ValueError:
            continue
    # Fallback: try just the date portion
    m = re.search(r"(\d{4}-\d{2}-\d{2})", date_str)
    if m:
        try:
            return dt.datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            pass
    return None


def identify_merchant(from_addr, subject, body=""):
    """Identify the merchant/sender from email fields.

    Returns (merchant_name, category) or (None, None).
    """
    combined = f"{from_addr} {subject}".lower()

    for merchant, info in RECEIPT_SENDERS.items():
        if merchant in combined:
            # Check if any keyword matches too
            if any(kw in combined or kw in (body or "").lower()
                   for kw in info["keywords"]):
                return merchant.title(), info["category"]

    # Fallback: extract domain from email address — but only if the subject
    # or body contains a financial keyword (to avoid false positives from
    # generic business emails)
    financial_keywords = ("receipt", "order", "invoice", "payment", "charge",
                          "bill", "transaction", "purchase", "subscription",
                          "deposit", "withdrawal", "transfer", "refund")
    combined_for_fin = f"{subject} {body}".lower()
    if any(kw in combined_for_fin for kw in financial_keywords):
        m = re.search(r"@([\w.-]+)", from_addr or "")
        if m:
            domain = m.group(1).split(".")[0].lower()
            if domain not in ("gmail", "yahoo", "outlook", "hotmail", "icloud"):
                return domain.title(), "GENERAL_SERVICES_OTHER_GENERAL_SERVICES"

    return None, None


def classify_email_type(subject, body=""):
    """Classify the email type: receipt, bank_alert, bill, or unknown."""
    combined = f"{subject} {body}".lower()

    receipt_signals = ["receipt", "order confirmation", "purchase",
                       "invoice", "your order", "payment confirmation",
                       "subscription", "billing statement", "order of",
                       "has shipped", "order has"]
    if any(s in combined for s in receipt_signals):
        return "receipt"

    alert_signals = ["alert", "transaction alert", "deposit alert",
                     "withdrawal", "fraud alert", "suspicious",
                     "direct deposit", "ach credit"]
    if any(s in combined for s in alert_signals):
        return "bank_alert"

    bill_signals = ["bill is ready", "payment due", "statement ready",
                    "payment received", "autopay", "bill payment",
                    "your bill", "payment processed"]
    if any(s in combined for s in bill_signals):
        return "bill"

    return "unknown"


# ----------------------------- receipt record ---------------------------------

def parse_receipt(thread_data):
    """Parse a Gmail thread into a structured receipt record.

    thread_data: dict from Gmail MCP get_thread, containing messages.
    Returns a receipt dict or None if no financial data could be extracted.
    """
    messages = thread_data.get("messages") or []
    if not messages:
        return None

    # Use the first message (original)
    msg = messages[0]
    headers = {}
    for h in (msg.get("headers") or []):
        headers[h.get("name", "").lower()] = h.get("value", "")

    subject = headers.get("subject", msg.get("subject", ""))
    from_addr = headers.get("from", msg.get("from", ""))
    date_str = headers.get("date", msg.get("date", ""))
    body = msg.get("plaintext_body") or msg.get("snippet") or ""
    snippet = msg.get("snippet", "")

    # Extract structured data
    merchant, category = identify_merchant(from_addr, subject, body)
    email_type = classify_email_type(subject, body)

    # Amount: try body first, then subject
    amount = extract_amount(body)
    if amount is None:
        amount = extract_amount(subject)
    if amount is None:
        amount = extract_amount(snippet)

    # Date: try body, then email date header
    txn_date = extract_date(body)
    email_date = extract_date_from_email_date(date_str)
    if txn_date is None:
        txn_date = email_date

    # LLM fallback: if regex couldn't extract amount but email looks financial,
    # try LLM extraction. Only fires when amount is None AND the email was
    # classified as a receipt/bill/bank_alert (not unknown).
    llm_extracted = False
    if amount is None and email_type != "unknown" and body:
        llm_result = llm_matcher.llm_extract_receipt(body)
        if llm_result is not None:
            amount = llm_result.get("amount")
            llm_extracted = True
            # Also fill in merchant/date from LLM if regex couldn't get them
            if merchant is None and llm_result.get("merchant"):
                merchant = llm_result["merchant"]
                # Try to get category for the LLM-extracted merchant
                _, llm_cat = identify_merchant(
                    from_addr, f"{subject} {merchant}", body)
                if llm_cat:
                    category = llm_cat
                elif not category:
                    category = "GENERAL_SERVICES_OTHER_GENERAL_SERVICES"
            if txn_date is None and llm_result.get("date"):
                txn_date = _parse_date_str(llm_result["date"])
                if txn_date is None:
                    txn_date = email_date

    if amount is None and merchant is None:
        return None  # no financial data to extract

    thread_id = thread_data.get("id") or thread_data.get("threadId", "")

    result = {
        "thread_id": thread_id,
        "message_id": msg.get("id", ""),
        "type": email_type,
        "merchant": merchant,
        "amount": round(amount, 2) if amount is not None else None,
        "date": str(txn_date) if txn_date else None,
        "email_date": str(email_date) if email_date else None,
        "category": category,
        "subject": subject[:120],
        "from": from_addr[:80],
        "snippet": snippet[:200],
    }
    if llm_extracted:
        result["extraction_source"] = "llm_assisted"
    return result


# ----------------------------- merchant normalisation -------------------------

_SUFFIXES = re.compile(
    r"\b(inc|llc|ltd|corp|co|company|plc|gmbh|pbc|sa|srl|pty|ag)\b\.?",
    re.I,
)
_STRIP_CHARS = re.compile(r"[^a-z0-9 ]+")


def normalize_merchant_name(name):
    """Lowercase, strip Inc/LLC/etc, collapse whitespace. Deterministic."""
    if not name:
        return ""
    s = name.lower()
    s = _SUFFIXES.sub(" ", s)
    s = _STRIP_CHARS.sub(" ", s)
    return " ".join(s.split()).strip()


def merchant_similarity(name_a, name_b):
    """Score 0-3 for how well two merchant names match.

    3 = exact normalised match
    2 = one name is a substring of the other
    1 = any significant token overlap (tokens > 2 chars)
    0 = no overlap
    """
    a = normalize_merchant_name(name_a)
    b = normalize_merchant_name(name_b)
    if not a or not b:
        return 0
    if a == b:
        return 3
    if a in b or b in a:
        return 2
    toks_a = {t for t in a.split() if len(t) > 2}
    toks_b = {t for t in b.split() if len(t) > 2}
    if toks_a & toks_b:
        return 1
    return 0


# ----------------------------- reconciliation engine --------------------------
# The core of the rearchitecture: reconcile receipts against bank transactions,
# producing five categories of findings.  Pure deterministic Python — no model.

def _parse_date_str(s):
    """Parse a 'YYYY-MM-DD' string to a date. Returns None on failure."""
    if not s:
        return None
    try:
        return dt.datetime.strptime(s[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def reconcile(receipts, transactions, *,
              date_tolerance_days=3,
              amount_tolerance_pct=0.05,
              amount_tolerance_abs=2.0,
              pending_threshold_days=3,
              as_of=None,
              llm_api_key=None):
    """Cross-reference receipts against bank transactions.

    Matching is primarily deterministic Python: merchant name similarity, date
    proximity, and amount tolerance.  An optional LLM-assisted fallback layer
    runs after the deterministic pass — only unmatched items are sent, and ONLY
    merchant name strings are sent to the model (no amounts, dates, or IDs).

    Parameters
    ----------
    receipts : list[dict]
        Receipt records (from email extraction or receipts.json).
    transactions : list[dict]
        Bank transaction records (Plaid-shaped).
    date_tolerance_days : int
        Max days apart for a receipt-transaction pair to be considered matching.
    amount_tolerance_pct : float
        Max percentage difference for amounts to "match" (for verified pairs).
    amount_tolerance_abs : float
        Max absolute dollar difference for amounts to "match".
    pending_threshold_days : int
        Days after which an unmatched receipt is flagged as pending/heads-up.
    as_of : date | None
        Reference date for "days since" calculations (defaults to today).
    llm_api_key : str | None
        Anthropic API key for LLM fallback matching.  Falls back to
        delivery._anthropic_key() if None.  If no key available, the LLM
        pass is silently skipped.

    Returns
    -------
    dict with keys:
        matched         – receipt + txn pairs where merchant, date, AND amount
                          all agree within tolerance.  Verified; not surfaced.
        discrepancies   – matched on merchant + date but amounts differ beyond
                          tolerance.  Surfaced to fee_fraud.
        unmatched_receipts – receipt exists, no bank charge found.  Surfaced
                             as pending/heads-up.
        unmatched_charges  – bank outflow from a receipt-sending merchant with
                             no matching receipt.  Surfaced as "verify".
        price_changes   – receipt amount differs from the most recent prior
                          receipt for the same merchant.  Surfaced to
                          subscription_creep / recurring.
        coverage        – {total, matched, pct} summary stat.

    Each match entry includes a "match_source" field: "deterministic" for the
    fuzzy-match pass, "llm_assisted" for the LLM fallback.
    """
    if as_of is None:
        as_of = dt.date.today()

    # ----- build outflow-only transaction list with parsed fields -----
    parsed_txns = []
    for t in transactions:
        if not sc.is_outflow(t):
            continue
        t_date = sc.parse_date(t)
        t_amt = sc.amount_magnitude(t)
        if t_date is None or t_amt is None:
            continue
        parsed_txns.append({
            "txn": t,
            "date": t_date,
            "amount": round(t_amt, 2),
            "merchant": sc.display_name(t),
            "merchant_norm": normalize_merchant_name(sc.display_name(t)),
            "matched": False,
        })

    # ----- phase 1: match each receipt to best transaction -----
    matched = []
    discrepancies = []
    unmatched_receipts_list = []
    incomplete = []

    for receipt in receipts:
        r_amt = receipt.get("amount")
        r_date_str = receipt.get("date")
        r_merchant = receipt.get("merchant") or ""

        if r_amt is None or r_date_str is None:
            incomplete.append(receipt)
            continue

        r_date = _parse_date_str(r_date_str)
        if r_date is None:
            incomplete.append(receipt)
            continue

        normalize_merchant_name(r_merchant)

        best_idx = None
        best_score = -1

        for i, pt in enumerate(parsed_txns):
            if pt["matched"]:
                continue  # already claimed

            # Date gate
            day_diff = abs((pt["date"] - r_date).days)
            if day_diff > date_tolerance_days:
                continue

            # Merchant gate — require at least token overlap (score >= 1)
            m_score = merchant_similarity(r_merchant, pt["merchant"])
            if m_score == 0:
                continue

            # Amount proximity (for scoring, not gating — discrepancies
            # are kept as a separate category)
            if r_amt > 0:
                amt_ratio = abs(pt["amount"] - r_amt) / r_amt
            else:
                amt_ratio = abs(pt["amount"] - r_amt) if r_amt == 0 else 1.0

            # Composite score: date closeness + merchant name quality + amount
            score = ((10 - day_diff)
                     + (5 * m_score)
                     + (10 * max(0, 1 - amt_ratio)))
            if score > best_score:
                best_score = score
                best_idx = i

        if best_idx is not None:
            pt = parsed_txns[best_idx]
            pt["matched"] = True

            amt_diff = abs(pt["amount"] - r_amt)
            amt_pct = (amt_diff / r_amt) if r_amt > 0 else 0

            amounts_match = (amt_diff <= amount_tolerance_abs
                             and amt_pct <= amount_tolerance_pct)

            entry = {
                "receipt": receipt,
                "txn_merchant": pt["merchant"],
                "txn_amount": pt["amount"],
                "txn_date": str(pt["date"]),
                "receipt_amount": round(r_amt, 2),
                "receipt_date": r_date_str,
                "receipt_merchant": r_merchant,
                "match_source": "deterministic",
            }

            if amounts_match:
                matched.append(entry)
            else:
                entry["difference"] = round(pt["amount"] - r_amt, 2)
                entry["abs_difference"] = round(amt_diff, 2)
                entry["message"] = (
                    f"{pt['merchant']} charged ${pt['amount']:.2f} but receipt "
                    f"shows ${r_amt:.2f} — ${amt_diff:.2f} difference"
                )
                discrepancies.append(entry)
        else:
            # No matching bank transaction found
            days_since = (as_of - r_date).days
            status = "pending_or_declined" if days_since >= pending_threshold_days else "recent"
            unmatched_receipts_list.append({
                "receipt": receipt,
                "merchant": r_merchant,
                "amount": round(r_amt, 2),
                "date": r_date_str,
                "days_since": days_since,
                "status": status,
                "message": (
                    f"{r_merchant} (${r_amt:.2f}, {r_date_str}) — no matching "
                    f"bank charge after {days_since} day{'s' if days_since != 1 else ''}. "
                    f"{'Pending or declined?' if status == 'pending_or_declined' else 'Recent, may still post.'}"
                ),
            })

    # ----- phase 1b: LLM-assisted fallback matching -----
    # Only runs when there are unmatched items on BOTH sides AND an API key
    # is available.  Sends ONLY merchant name strings — no amounts, dates,
    # account numbers, or transaction IDs.
    llm_matched_count = 0
    llm_discrepancy_count = 0

    if unmatched_receipts_list and any(not pt["matched"] for pt in parsed_txns):
        # Build the unmatched-transaction list for the LLM
        unmatched_txn_dicts = []
        unmatched_txn_indices = []  # maps LLM list index -> parsed_txns index
        for i, pt in enumerate(parsed_txns):
            if not pt["matched"]:
                unmatched_txn_dicts.append({"merchant": pt["merchant"]})
                unmatched_txn_indices.append(i)

        if unmatched_txn_dicts:
            # Build receipt dicts with just the merchant name for the LLM
            unmatched_rcpt_dicts = [{"merchant": ur["merchant"]}
                                    for ur in unmatched_receipts_list]

            llm_pairs = llm_matcher.llm_match_merchants(
                unmatched_rcpt_dicts, unmatched_txn_dicts,
                api_key=llm_api_key,
            )

            # Process LLM matches — apply same amount-tolerance logic
            llm_resolved_receipt_indices = set()
            for pair in llm_pairs:
                ri = pair["receipt_index"]
                bi = pair["bank_index"]

                if ri in llm_resolved_receipt_indices:
                    continue
                if bi >= len(unmatched_txn_indices):
                    continue

                ur = unmatched_receipts_list[ri]
                pti = unmatched_txn_indices[bi]
                pt = parsed_txns[pti]

                if pt["matched"]:
                    continue  # claimed by an earlier LLM pair

                # Date gate — still enforce date tolerance
                r_date = _parse_date_str(ur["date"])
                if r_date is None:
                    continue
                day_diff = abs((pt["date"] - r_date).days)
                if day_diff > date_tolerance_days:
                    continue

                pt["matched"] = True
                llm_resolved_receipt_indices.add(ri)

                r_amt = ur["amount"]
                amt_diff = abs(pt["amount"] - r_amt)
                amt_pct = (amt_diff / r_amt) if r_amt > 0 else 0

                amounts_match = (amt_diff <= amount_tolerance_abs
                                 and amt_pct <= amount_tolerance_pct)

                receipt_obj = ur["receipt"]
                entry = {
                    "receipt": receipt_obj,
                    "txn_merchant": pt["merchant"],
                    "txn_amount": pt["amount"],
                    "txn_date": str(pt["date"]),
                    "receipt_amount": round(r_amt, 2),
                    "receipt_date": ur["date"],
                    "receipt_merchant": ur["merchant"],
                    "match_source": "llm_assisted",
                    "llm_confidence": pair["confidence"],
                }

                if amounts_match:
                    matched.append(entry)
                    llm_matched_count += 1
                else:
                    entry["difference"] = round(pt["amount"] - r_amt, 2)
                    entry["abs_difference"] = round(amt_diff, 2)
                    entry["message"] = (
                        f"{pt['merchant']} charged ${pt['amount']:.2f} but receipt "
                        f"shows ${r_amt:.2f} — ${amt_diff:.2f} difference "
                        f"(LLM-matched)"
                    )
                    discrepancies.append(entry)
                    llm_discrepancy_count += 1

            # Remove resolved receipts from unmatched list (reverse order)
            for ri in sorted(llm_resolved_receipt_indices, reverse=True):
                unmatched_receipts_list.pop(ri)

    # ----- phase 1b: merchant+date tier for amount-null receipts -----
    # Receipts whose amount couldn't be parsed still count: match them to a bank
    # charge by merchant + date (no amount cross-check). A match means the
    # purchase is accounted for; either way the receipt now enters the coverage
    # denominator instead of being silently dropped (which inflated the %).
    matched_no_amount = []
    still_incomplete = []
    for receipt in incomplete:
        r_date = _parse_date_str(receipt.get("date") or "")
        r_merchant = receipt.get("merchant") or ""
        hit = None
        if r_date is not None and r_merchant:
            for pt in parsed_txns:
                if pt["matched"]:
                    continue
                if abs((pt["date"] - r_date).days) > date_tolerance_days:
                    continue
                if merchant_similarity(r_merchant, pt["merchant"]) == 0:
                    continue
                hit = pt
                break
        if hit is not None:
            hit["matched"] = True
            matched_no_amount.append({
                "receipt": receipt,
                "txn_merchant": hit["merchant"],
                "txn_amount": hit["amount"],
                "txn_date": str(hit["date"]),
                "receipt_merchant": r_merchant,
                "receipt_date": receipt.get("date"),
                "match_source": "merchant_date",
                "note": "amount unverified — receipt had no parseable amount",
            })
        else:
            still_incomplete.append(receipt)

    # ----- phase 2: unmatched bank charges (verify) -----
    # Only flag charges from merchants known to send receipts AND within the
    # receipt date window, to avoid flooding with every grocery purchase.
    receipt_merchants = {normalize_merchant_name(r.get("merchant", ""))
                        for r in receipts if r.get("merchant")}
    known_senders = {normalize_merchant_name(k)
                     for k in RECEIPT_SENDERS}
    flaggable = receipt_merchants | known_senders

    # Determine the receipt date window
    receipt_dates = [_parse_date_str(r.get("date")) for r in receipts
                     if r.get("date")]
    receipt_dates = [d for d in receipt_dates if d is not None]
    if receipt_dates:
        window_start = min(receipt_dates) - dt.timedelta(days=date_tolerance_days)
        window_end = max(receipt_dates) + dt.timedelta(days=date_tolerance_days)
    else:
        window_start = window_end = as_of

    # Merchants we DO have a receipt for but couldn't parse an amount/date from
    # (incomplete). We have evidence the purchase is legit — we just can't verify
    # the figure — so their charges must NOT be flagged as suspect/unverified.
    covered_by_incomplete = {normalize_merchant_name(r.get("merchant", ""))
                             for r in incomplete if r.get("merchant")}

    unmatched_charges = []
    for pt in parsed_txns:
        if pt["matched"]:
            continue
        if not (window_start <= pt["date"] <= window_end):
            continue
        if pt["merchant_norm"] not in flaggable:
            continue
        if pt["merchant_norm"] in covered_by_incomplete:
            continue                       # receipt exists, amount just unparsed
        unmatched_charges.append({
            "merchant": pt["merchant"],
            "amount": pt["amount"],
            "date": str(pt["date"]),
            "message": (
                f"Charge from {pt['merchant']} ${pt['amount']:.2f} — "
                f"no receipt on file, verify"
            ),
        })
    unmatched_charges.sort(key=lambda x: (-x["amount"], x["date"]))

    # ----- phase 3: price change detection -----
    # Compare each receipt's amount to the most recent PRIOR receipt for the
    # same merchant.  A >5% or >$1 change is flagged.
    by_merchant = defaultdict(list)
    for r in receipts:
        if r.get("amount") and r.get("merchant") and r.get("date"):
            key = normalize_merchant_name(r["merchant"])
            by_merchant[key].append(r)
    for v in by_merchant.values():
        v.sort(key=lambda r: r["date"])

    price_changes = []
    for key, recs in by_merchant.items():
        if len(recs) < 2:
            continue
        for i in range(1, len(recs)):
            prev = recs[i - 1]
            curr = recs[i]
            if prev["amount"] is None or curr["amount"] is None:
                continue
            diff = curr["amount"] - prev["amount"]
            if abs(diff) < 1.0 and (prev["amount"] == 0 or
                                     abs(diff) / prev["amount"] < 0.05):
                continue
            price_changes.append({
                "merchant": curr["merchant"],
                "current_amount": round(curr["amount"], 2),
                "previous_amount": round(prev["amount"], 2),
                "change": round(diff, 2),
                "current_date": curr["date"],
                "previous_date": prev["date"],
                "message": (
                    f"{curr['merchant']} ${curr['amount']:.2f} vs "
                    f"last ${prev['amount']:.2f} — "
                    f"{'price increase' if diff > 0 else 'price decrease'}"
                ),
            })

    # ----- coverage stat -----
    # Honest denominator: every receipt that came in (amount-verified matches,
    # discrepancies, merchant+date matches, unmatched, and still-incomplete).
    # A parse failure can no longer flatter the % by dropping out of the count.
    total_receipts_in = (len(matched) + len(discrepancies) + len(matched_no_amount)
                         + len(unmatched_receipts_list) + len(still_incomplete))
    matched_count = len(matched) + len(discrepancies) + len(matched_no_amount)
    pct = round(matched_count / total_receipts_in * 100, 1) if total_receipts_in else 100.0

    return {
        "matched": matched,
        "discrepancies": discrepancies,
        "matched_no_amount": matched_no_amount,
        "unmatched_receipts": unmatched_receipts_list,
        "unmatched_charges": unmatched_charges,
        "price_changes": price_changes,
        "incomplete": still_incomplete,
        "coverage": {
            "total": total_receipts_in,
            "matched": matched_count,
            "amount_verified": len(matched) + len(discrepancies),
            "merchant_date_only": len(matched_no_amount),
            "pct": pct,
            "llm_matched": llm_matched_count,
            "llm_discrepancies": llm_discrepancy_count,
        },
    }


# ---- legacy wrapper for offline / test use with ledger store ----

def reconcile_from_store(receipts, store_path=None, **kwargs):
    """Back-compat: reconcile receipts against the canonical DB transactions.

    `store_path` is accepted for backward-compat but ignored (DB is the source).
    Returns the full reconciliation result dict.
    """
    conn = db.connect()
    txns = db.load_transactions_from_db(conn)
    conn.close()
    return reconcile(receipts, txns, **kwargs)


# ----------------------------- Gmail MCP integration --------------------------
# These functions call the Gmail MCP tools. They're designed to be called from
# the finance agent or sync pipeline, which has MCP access.

def build_gmail_queries(days=7):
    """Build Gmail search queries for receipt/notification emails.

    Returns a list of (query_string, description) tuples.
    """
    queries = []

    # Purchase receipts
    queries.append((
        f"(subject:receipt OR subject:invoice OR subject:order "
        f"OR subject:\"payment confirmation\" OR subject:purchase) "
        f"newer_than:{days}d -in:draft",
        "purchase receipts"
    ))

    # Bank notifications
    queries.append((
        f"(subject:\"transaction alert\" OR subject:\"direct deposit\" "
        f"OR subject:\"payment alert\" OR subject:withdrawal "
        f"OR subject:\"ach credit\" OR subject:\"fraud alert\") "
        f"newer_than:{days}d -in:draft",
        "bank notifications"
    ))

    # Bill/payment confirmations
    queries.append((
        f"(subject:\"bill is ready\" OR subject:\"payment due\" "
        f"OR subject:\"payment received\" OR subject:\"payment processed\" "
        f"OR subject:autopay OR subject:\"billing statement\") "
        f"newer_than:{days}d -in:draft",
        "bill confirmations"
    ))

    return queries


def reconciliation_to_summary(recon):
    """Build the compact summary dict from a reconciliation result.

    This is the ONLY thing that ever feeds narration — no raw email content.
    The summary distributes findings to the sections that should surface them.
    """
    m = recon["matched"]
    disc = recon["discrepancies"]
    ur = recon["unmatched_receipts"]
    uc = recon["unmatched_charges"]
    pc = recon["price_changes"]
    cov = recon["coverage"]

    total_verified_amt = sum(e["receipt_amount"] for e in m)
    total_disc_amt = sum(e["abs_difference"] for e in disc)
    total_unmatched_rcpt_amt = sum(e["amount"] for e in ur)
    total_unmatched_charge_amt = sum(e["amount"] for e in uc)

    flags = []
    if disc:
        flags.append(f"{len(disc)} amount discrepancy(ies) totaling "
                     f"${total_disc_amt:.2f}")
    pending = [r for r in ur if r["status"] == "pending_or_declined"]
    if pending:
        flags.append(f"{len(pending)} receipt(s) with no bank charge after "
                     f"threshold — pending or declined?")
    if uc:
        flags.append(f"{len(uc)} bank charge(s) with no receipt on file — verify")
    if pc:
        increases = [p for p in pc if p["change"] > 0]
        if increases:
            flags.append(f"{len(increases)} price increase(s) detected from receipts")

    return {
        "tool": "receipt_reconciliation",
        "as_of": str(dt.date.today()),
        "headline": {
            "coverage_pct": cov["pct"],
            "total_receipts": cov["total"],
            "matched": cov["matched"],
            "verified_amount": round(total_verified_amt, 2),
            "n_discrepancies": len(disc),
            "discrepancy_amount": round(total_disc_amt, 2),
            "n_unmatched_receipts": len(ur),
            "unmatched_receipt_amount": round(total_unmatched_rcpt_amt, 2),
            "n_unmatched_charges": len(uc),
            "unmatched_charge_amount": round(total_unmatched_charge_amt, 2),
            "n_price_changes": len(pc),
        },
        # Findings distributed to their target sections:
        "for_fee_fraud": {
            "discrepancies": [
                {"merchant": d["txn_merchant"],
                 "receipt_amount": d["receipt_amount"],
                 "bank_amount": d["txn_amount"],
                 "difference": d["abs_difference"],
                 "date": d["txn_date"],
                 "message": d["message"]}
                for d in disc
            ][:12],
            "unmatched_charges": [
                {"merchant": e["merchant"], "amount": e["amount"],
                 "date": e["date"], "message": e["message"]}
                for e in uc
            ][:12],
        },
        "for_subscriptions": {
            "price_changes": [
                {"merchant": p["merchant"],
                 "current": p["current_amount"],
                 "previous": p["previous_amount"],
                 "change": p["change"],
                 "date": p["current_date"],
                 "message": p["message"]}
                for p in pc
            ][:12],
        },
        "for_cashflow": {
            "pending_receipts": [
                {"merchant": e["merchant"], "amount": e["amount"],
                 "date": e["date"], "days_since": e["days_since"],
                 "message": e["message"]}
                for e in ur if e["status"] == "pending_or_declined"
            ][:12],
        },
        "flags": flags,
    }


# ---- legacy summary builder (back-compat for old-style matched/unmatched) ----

def render(summary):
    """Render a reconciliation summary to markdown."""
    from finance_mcp.report import delivery
    money = delivery.money
    h = summary["headline"]
    L = []

    # Handle both old-style and new-style summaries
    if summary.get("tool") == "receipt_reconciliation":
        L.append("# finance.mcp — RECEIPT RECONCILIATION")
        L.append(f"_as of {summary['as_of']}_\n")
        L.append(f"## Coverage: {h['coverage_pct']}% verified "
                 f"({h['matched']}/{h['total_receipts']})")
        if h["n_discrepancies"]:
            L.append(f"- Amount discrepancies: {h['n_discrepancies']} "
                     f"({money(h['discrepancy_amount'])})")
        if h["n_unmatched_receipts"]:
            L.append(f"- Unmatched receipts: {h['n_unmatched_receipts']} "
                     f"({money(h['unmatched_receipt_amount'])})")
        if h["n_unmatched_charges"]:
            L.append(f"- Unverified charges: {h['n_unmatched_charges']} "
                     f"({money(h['unmatched_charge_amount'])})")
        if h["n_price_changes"]:
            L.append(f"- Price changes: {h['n_price_changes']}")

        for section_key, title in [
            ("for_fee_fraud", "Discrepancies + Unverified Charges"),
            ("for_subscriptions", "Price Changes"),
            ("for_cashflow", "Pending Receipts"),
        ]:
            section = summary.get(section_key, {})
            items = []
            for k, v in section.items():
                items.extend(v)
            if items:
                L.append(f"\n## {title}")
                for item in items:
                    L.append(f"- {item.get('message', str(item))}")

        if summary.get("flags"):
            L.append("\n## Flags")
            for f in summary["flags"]:
                L.append(f"- {f}")
    else:
        # Legacy format
        L.append("# finance.mcp — RECEIPT SCAN")
        L.append(f"_as of {summary['as_of']}_\n")
        L.append(f"## Summary: {h['total_receipts']} receipts scanned")
        L.append(f"- Matched to bank transactions: {h['matched']} "
                 f"({money(h['matched_amount'])})")
        L.append(f"- Unmatched (not in bank data): {h['unmatched']} "
                 f"({money(h['unmatched_amount'])})")

        if summary.get("detail", {}).get("unmatched"):
            L.append("\n## Unmatched receipts")
            for r in summary["detail"]["unmatched"]:
                amt = money(r["amount"]) if r.get("amount") else "?"
                L.append(f"- {r.get('merchant') or 'Unknown'} {amt} "
                         f"{r.get('date') or '?'}")
        if summary.get("flags"):
            L.append("\n## Flags")
            for f in summary["flags"]:
                L.append(f"- {f}")

    return "\n".join(L)


# ----------------------------- offline scan (no MCP) --------------------------

def scan_from_receipts_file(path, store_path=None,
                           transactions=None):
    """Scan from a JSON file of pre-extracted receipt records.

    If `transactions` is provided, uses the new reconciliation engine directly.
    Otherwise, falls back to loading from the ledger store.

    Returns a reconciliation summary dict.
    """
    with open(path, encoding="utf-8") as f:
        receipts = json.load(f)

    if transactions is not None:
        recon = reconcile(receipts, transactions)
        return reconciliation_to_summary(recon)

    # Fallback: load from ledger store
    recon = reconcile_from_store(receipts, store_path=store_path)
    return reconciliation_to_summary(recon)


# --------------------------------- CLI ----------------------------------------

def main():
    import argparse
    ap = argparse.ArgumentParser(
        description="Email receipt scanner + bank reconciliation.")
    ap.add_argument("--days", type=int, default=7,
                    help="scan emails from the last N days (default 7)")
    ap.add_argument("--receipts-file",
                    help="scan from a pre-extracted receipts JSON (testing)")
    ap.add_argument("--store", default=None,
                    help="(deprecated, ignored) reconciliation reads the canonical DB")
    ap.add_argument("--json", action="store_true",
                    help="output summary as JSON")
    ap.add_argument("--no-voice", action="store_true",
                    help="skip narration (default)")
    ap.add_argument("--queries-only", action="store_true",
                    help="just print the Gmail queries (for debugging)")
    a = ap.parse_args()

    if a.queries_only:
        for q, desc in build_gmail_queries(a.days):
            print(f"[{desc}] {q}")
        return

    if a.receipts_file:
        summary = scan_from_receipts_file(a.receipts_file, store_path=a.store)
    else:
        print("Receipt scanning requires Gmail MCP access.")
        print("Use --receipts-file for offline testing, or run via the finance agent.")
        print("\nGmail queries that would be used:")
        for q, desc in build_gmail_queries(a.days):
            print(f"  [{desc}] {q}")
        return

    if a.json:
        print(json.dumps(summary, indent=2))
    else:
        print(render(summary))


if __name__ == "__main__":
    main()
