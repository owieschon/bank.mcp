#!/usr/bin/env python3
"""
llm_matcher.py — LLM-assisted fallback for merchant matching and receipt extraction.

This module provides two LLM-backed fallback functions that augment the
deterministic engines:

  llm_match_merchants(unmatched_receipts, unmatched_transactions, api_key=None)
      Takes the leftovers from the deterministic reconciliation pass and asks
      Haiku to pair bank merchant names with receipt merchant names. Only
      merchant name strings are sent — never amounts, dates, account numbers,
      or transaction IDs.

  llm_extract_receipt(email_text, api_key=None)
      Takes raw email text that regex failed to parse and asks Haiku to
      extract {amount, merchant, date}. Only called when deterministic
      extraction returns None for amount.

PRIVACY (load-bearing):
  - llm_match_merchants sends ONLY merchant name strings to the model.
    No amounts, no dates, no account numbers, no transaction IDs.
  - llm_extract_receipt sends email text, but is only called on emails
    that were already identified as financial (regex found keywords but
    not structured amounts).

ARCHITECTURE:
  - Uses the same Keychain-or-env ANTHROPIC_API_KEY pattern as delivery.py.
  - Graceful degradation: if no API key or if the call fails, returns empty
    results so the deterministic pass stands unchanged.
  - Caches LLM merchant-match results in llm_match_cache.json so the same
    pair is never re-queried.
  - Uses claude-3-5-haiku (cheap, fast).
  - Batches all unmatched items into a single call.
"""

import hashlib
import json
import os
import urllib.error
import urllib.request

# Reuse delivery's credential pattern
from finance_mcp.report import delivery

MODEL = "claude-3-5-haiku-latest"
ANTHROPIC_URL = delivery.ANTHROPIC_URL
ANTHROPIC_VERSION = delivery.ANTHROPIC_VERSION

CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "llm_match_cache.json")

DEFAULT_CONFIDENCE_THRESHOLD = 0.8


# ----------------------------- cache layer ----------------------------------

def _load_cache(path=CACHE_PATH):
    """Load the LLM match cache. Returns {} on any failure."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cache, path=CACHE_PATH):
    """Persist the LLM match cache."""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, sort_keys=True)
            f.write("\n")
    except OSError:
        pass  # non-fatal: cache is a nicety


def _cache_key(bank_names, receipt_names):
    """Deterministic cache key from the two sorted name lists."""
    payload = json.dumps({
        "bank": sorted(bank_names),
        "receipts": sorted(receipt_names),
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# ----------------------------- API helper -----------------------------------

def _call_haiku(system, user, api_key=None):
    """Call Haiku with the given system/user prompts. Returns parsed JSON
    content string or None on any failure.

    Uses delivery._anthropic_key() if api_key is not provided.
    Graceful: returns None on missing key, HTTP errors, or parse failures.
    """
    key = api_key or delivery._anthropic_key()
    if not key:
        return None

    body = json.dumps({
        "model": MODEL,
        "max_tokens": 1024,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode()

    req = urllib.request.Request(
        ANTHROPIC_URL,
        data=body,
        headers={
            "x-api-key": key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
    )

    # Defense-in-depth: enforce HTTPS
    if not req.full_url.startswith("https://"):
        return None

    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        return "".join(b.get("text", "") for b in data.get("content", [])).strip()
    except (urllib.error.HTTPError, urllib.error.URLError, Exception):
        return None


# ----------------------------- merchant matching ----------------------------

def llm_match_merchants(unmatched_receipts, unmatched_transactions, *,
                        api_key=None,
                        confidence_threshold=DEFAULT_CONFIDENCE_THRESHOLD,
                        cache_path=CACHE_PATH):
    """LLM-assisted merchant name matching — fallback after deterministic pass.

    Parameters
    ----------
    unmatched_receipts : list[dict]
        Receipt dicts that the deterministic pass did not match. Each must
        have a "merchant" key.
    unmatched_transactions : list[dict]
        Bank transaction dicts that the deterministic pass did not match.
        Each must have a "merchant" key (the display name).
    api_key : str | None
        Anthropic API key. Falls back to delivery._anthropic_key().
    confidence_threshold : float
        Minimum confidence (0-1) to accept a match. Default 0.8.
    cache_path : str
        Path to the JSON cache file.

    Returns
    -------
    list[dict]
        Each dict: {bank_name, receipt_name, confidence, bank_index, receipt_index}
        where the indices refer to positions in the input lists.

    PRIVACY: Only merchant NAME STRINGS are sent to the model. No amounts,
    dates, account numbers, or transaction IDs are ever included in the prompt.
    """
    if not unmatched_receipts or not unmatched_transactions:
        return []

    # Extract only the merchant names (PRIVACY: nothing else goes to the model)
    receipt_names = []
    receipt_indices = {}
    for i, r in enumerate(unmatched_receipts):
        name = r.get("merchant") or ""
        if name:
            receipt_names.append(name)
            receipt_indices[name] = i

    bank_names = []
    bank_indices = {}
    for i, t in enumerate(unmatched_transactions):
        name = t.get("merchant") or ""
        if name:
            bank_names.append(name)
            bank_indices[name] = i

    if not receipt_names or not bank_names:
        return []

    # Check cache
    cache = _load_cache(cache_path)
    ck = _cache_key(bank_names, receipt_names)
    if ck in cache:
        cached = cache[ck]
        # Rebuild indices from cached name pairs
        results = []
        for pair in cached:
            bn = pair.get("bank_name", "")
            rn = pair.get("receipt_name", "")
            conf = pair.get("confidence", 0)
            if conf >= confidence_threshold and bn in bank_indices and rn in receipt_indices:
                results.append({
                    "bank_name": bn,
                    "receipt_name": rn,
                    "confidence": conf,
                    "bank_index": bank_indices[bn],
                    "receipt_index": receipt_indices[rn],
                })
        return results

    # Call the LLM — ONLY merchant name strings
    system = (
        "You are a merchant name matching assistant. You receive two lists of "
        "merchant names: one from bank statements and one from email receipts. "
        "Your job is to identify pairs that refer to the same business.\n\n"
        "Return ONLY a JSON array of objects with keys: "
        '"bank", "receipt", "confidence" (0.0 to 1.0).\n'
        "Only include pairs you are confident refer to the same business. "
        "Do not guess. Return an empty array [] if no matches are found.\n"
        "Return ONLY valid JSON, no markdown fences, no explanation."
    )

    user = (
        f"Match these bank statement merchant names to these receipt merchant names. "
        f"Return pairs that refer to the same business.\n\n"
        f"Bank: {json.dumps(bank_names)}\n"
        f"Receipts: {json.dumps(receipt_names)}\n\n"
        f"Return JSON array of {{bank, receipt, confidence}}."
    )

    raw = _call_haiku(system, user, api_key=api_key)
    if raw is None:
        return []

    # Parse response
    pairs = _parse_match_response(raw)

    # Cache the raw pairs (before filtering by threshold)
    cache[ck] = [{"bank_name": p["bank"], "receipt_name": p["receipt"],
                  "confidence": p["confidence"]} for p in pairs]
    _save_cache(cache, cache_path)

    # Filter by confidence and build result
    results = []
    used_bank = set()
    used_receipt = set()
    for p in sorted(pairs, key=lambda x: -x["confidence"]):
        bn = p["bank"]
        rn = p["receipt"]
        conf = p["confidence"]
        if conf < confidence_threshold:
            continue
        if bn not in bank_indices or rn not in receipt_indices:
            continue
        if bn in used_bank or rn in used_receipt:
            continue  # one-to-one matching
        used_bank.add(bn)
        used_receipt.add(rn)
        results.append({
            "bank_name": bn,
            "receipt_name": rn,
            "confidence": conf,
            "bank_index": bank_indices[bn],
            "receipt_index": receipt_indices[rn],
        })

    return results


def _parse_match_response(raw):
    """Parse the LLM response into a list of {bank, receipt, confidence} dicts.

    Handles markdown fences, trailing text, and malformed JSON gracefully.
    Returns [] on any parse failure.
    """
    if not raw:
        return []

    # Strip markdown fences if present
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last fence lines
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()

    # Try to find a JSON array in the text
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return []

    try:
        arr = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []

    if not isinstance(arr, list):
        return []

    results = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        bank = item.get("bank", "")
        receipt = item.get("receipt", "")
        confidence = item.get("confidence", 0)
        if bank and receipt and isinstance(confidence, (int, float)):
            results.append({
                "bank": str(bank),
                "receipt": str(receipt),
                "confidence": float(confidence),
            })

    return results


# ----------------------------- receipt extraction ---------------------------

def llm_extract_receipt(email_text, *, api_key=None):
    """LLM-assisted receipt data extraction — fallback after regex.

    Called only when deterministic regex extraction returns None for amount
    on an email that was already identified as financial.

    Parameters
    ----------
    email_text : str
        The raw email body text.
    api_key : str | None
        Anthropic API key. Falls back to delivery._anthropic_key().

    Returns
    -------
    dict | None
        {amount: float, merchant: str, date: str} or None on failure.
        'date' is in YYYY-MM-DD format if extractable.
    """
    if not email_text or not email_text.strip():
        return None

    system = (
        "You are a receipt data extractor. You receive the text of a financial "
        "email (receipt, invoice, or payment confirmation). Extract:\n"
        '- "amount": the total amount charged as a number (no $ sign)\n'
        '- "merchant": the business name\n'
        '- "date": the transaction date in YYYY-MM-DD format\n\n'
        "Return ONLY a JSON object with these three keys. "
        "Use null for any field you cannot determine. "
        "Return ONLY valid JSON, no markdown fences, no explanation."
    )

    user = (
        "Extract the total amount charged, merchant name, and date from "
        "this receipt email. Return JSON {amount, merchant, date}.\n\n"
        f"Email text:\n{email_text[:3000]}"  # cap to avoid token blowout
    )

    raw = _call_haiku(system, user, api_key=api_key)
    if raw is None:
        return None

    return _parse_extract_response(raw)


def _parse_extract_response(raw):
    """Parse the LLM extraction response. Returns dict or None."""
    if not raw:
        return None

    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()

    # Find JSON object
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None

    if not isinstance(obj, dict):
        return None

    amount = obj.get("amount")
    merchant = obj.get("merchant")
    date = obj.get("date")

    # Validate amount
    if amount is not None:
        try:
            amount = round(float(amount), 2)
        except (ValueError, TypeError):
            amount = None

    # At minimum, we need an amount to be useful
    if amount is None:
        return None

    return {
        "amount": amount,
        "merchant": str(merchant) if merchant else None,
        "date": str(date) if date else None,
    }
