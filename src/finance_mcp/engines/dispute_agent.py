#!/usr/bin/env python3
"""
dispute_agent.py — dispute/refund automation layer for the finance.mcp suite.

Four capabilities:

  1. AUTO-DRAFT REFUND REQUESTS
     Takes flagged findings from reconciliation (discrepancies) and fee_fraud
     (duplicates), generates polite refund request emails via Haiku, and
     creates Gmail drafts via the MCP.  Only drafts — never sends automatically.

  2. AUTO-DRAFT BANK DISPUTE LETTERS
     For unverified charges over a configurable threshold (default $25),
     generates formal dispute letters in standard bank dispute format.
     Includes placeholders for sensitive info ([ACCOUNT_NUMBER], [BANK_NAME])
     that the user fills in manually.

  3. MERCHANT CONTACT LOOKUP
     Given a merchant name, finds their support/billing email by:
       a. Checking local cache (merchant_contacts.json)
       b. Falling back to Haiku for best-guess support email
     Caches results for reuse.

  4. DISPUTE TRACKING
     Maintains disputes.json ledger tracking each dispute through its
     lifecycle: drafted -> sent -> pending -> resolved/expired.  Auto-closes
     disputes when a matching refund credit appears in bank transactions.
     Flags disputes with no response after 14 days.

PRIVACY (load-bearing):
  - LLM calls for email drafting send ONLY: merchant name, amount, date,
    dispute reason.  NEVER account numbers, bank name, full transaction IDs.
  - Bank dispute letter templates use [PLACEHOLDER] for sensitive fields.
  - disputes.json is stored locally, never transmitted.

ARCHITECTURE:
  - Uses delivery._anthropic_key() for Haiku credentials (same pattern as
    llm_matcher.py).
  - Uses llm_matcher._call_haiku() for LLM calls.
  - Graceful degradation: if no API key, generates template-based emails
    without Haiku narration.
  - Draft creation returns content dicts; the caller (finance_agent or MCP
    layer) handles actual Gmail MCP create_draft calls.
"""

import datetime as dt
import json
import os
import uuid

from finance_mcp.report import delivery
from finance_mcp.engines import llm_matcher

# Paths (co-located with the suite)
_SUITE_DIR = os.path.dirname(os.path.abspath(__file__))
DISPUTES_PATH = os.path.join(_SUITE_DIR, "disputes.json")
MERCHANT_CONTACTS_PATH = os.path.join(_SUITE_DIR, "merchant_contacts.json")

# Defaults
DEFAULT_DISPUTE_THRESHOLD = 25.0
DISPUTE_EXPIRY_DAYS = 14
DEFAULT_BANK_EMAIL = "disputes@example-bank.com"
# Signer on generated dispute/refund letters; override via env, else a placeholder.
ACCOUNT_HOLDER = os.environ.get("ACCOUNT_HOLDER", "[ACCOUNT_HOLDER]")

money = delivery.money

_FONT_SANS = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif"
_FONT_MONO = "'SF Mono','Fira Code',Consolas,monospace"


# ========================= MERCHANT CONTACT LOOKUP =========================

def load_merchant_contacts(path=None):
    """Load cached merchant contacts. Returns {} on failure."""
    path = path or MERCHANT_CONTACTS_PATH
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def save_merchant_contacts(contacts, path=None):
    """Persist merchant contacts cache."""
    path = path or MERCHANT_CONTACTS_PATH
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(contacts, f, indent=2, sort_keys=True)
            f.write("\n")
    except OSError:
        pass


def _normalize_contact_key(name):
    """Normalize merchant name for cache key."""
    return name.strip().lower().replace(" ", "_")


def lookup_merchant_contact(merchant_name, *, contacts_path=None, api_key=None):
    """Look up merchant support/billing email.

    Strategy:
      1. Check local cache (merchant_contacts.json)
      2. Fall back to Haiku (asks for support email)

    Returns {"email": str|None, "source": str}.
    Caches successful lookups.

    PRIVACY: Only the merchant NAME is sent to the model.
    """
    contacts_path = contacts_path or MERCHANT_CONTACTS_PATH
    contacts = load_merchant_contacts(contacts_path)
    key = _normalize_contact_key(merchant_name)

    # 1. Cache hit
    if key in contacts and contacts[key].get("email"):
        return {"email": contacts[key]["email"], "source": "cache"}

    # 2. LLM fallback
    email = _llm_lookup_contact(merchant_name, api_key=api_key)
    if email:
        contacts[key] = {
            "email": email,
            "source": "llm",
            "cached_at": str(dt.date.today()),
            "merchant": merchant_name,
        }
        save_merchant_contacts(contacts, contacts_path)
        return {"email": email, "source": "llm"}

    return {"email": None, "source": "not_found"}


def _llm_lookup_contact(merchant_name, *, api_key=None):
    """Ask Haiku for a merchant's support/billing email.

    PRIVACY: Only the merchant name is sent.
    Returns just the email string or None.
    """
    system = (
        "You are a customer support email lookup tool. The user gives you a "
        "merchant/company name and you return their customer support or billing "
        "dispute email address. Return ONLY the email address, nothing else. "
        "If you are not confident about the email, return 'unknown'."
    )
    user = (
        f"What is the customer support or billing dispute email for "
        f"{merchant_name}? Return just the email address."
    )

    raw = llm_matcher._call_haiku(system, user, api_key=api_key)
    if not raw:
        return None

    result = raw.strip().lower()
    if "unknown" in result:
        return None
    # Basic email validation
    if "@" in result and "." in result.split("@")[-1]:
        result = result.strip("\"'<>[] \n")
        # Reject if it still looks weird
        if " " in result or len(result) > 80:
            return None
        return result
    return None


# ========================= DISPUTE TRACKING =========================

def load_disputes(path=None):
    """Load the dispute ledger. Returns [] on failure."""
    path = path or DISPUTES_PATH
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_disputes(disputes, path=None):
    """Persist the dispute ledger."""
    path = path or DISPUTES_PATH
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(disputes, f, indent=2)
            f.write("\n")
    except OSError:
        pass


def _generate_dispute_id():
    """Generate a short unique dispute ID."""
    return "DSP-" + uuid.uuid4().hex[:8].upper()


def create_dispute(*, dispute_type, merchant, amount, date, reason,
                   evidence=None, draft_created=False, path=None):
    """Create a new dispute record and add it to the ledger.

    dispute_type : "refund_request" | "bank_dispute"
    reason       : "duplicate_charge" | "amount_discrepancy" |
                   "unauthorized_charge" | "goods_not_received"

    Returns the created dispute dict.  De-duplicates: if the same
    merchant + amount + date + reason already exists and is not
    resolved/expired, the existing record is returned.
    """
    path = path or DISPUTES_PATH
    disputes = load_disputes(path)

    # De-dup
    for d in disputes:
        if (d["merchant"] == merchant
                and d["amount"] == round(amount, 2)
                and d["date"] == date
                and d["reason"] == reason
                and d["status"] not in ("resolved", "expired")):
            return d

    dispute = {
        "dispute_id": _generate_dispute_id(),
        "type": dispute_type,
        "merchant": merchant,
        "amount": round(amount, 2),
        "date": date,
        "reason": reason,
        "date_filed": str(dt.date.today()),
        "status": "drafted",
        "evidence": evidence or {},
        "resolution": None,
        "resolution_date": None,
        "draft_created": draft_created,
    }

    disputes.append(dispute)
    save_disputes(disputes, path)
    return dispute


def update_dispute_status(dispute_id, status, *, resolution=None, path=None):
    """Update a dispute's status.

    status     : "drafted" | "sent" | "pending" | "resolved" | "expired"
    resolution : "refund_received" | "denied" | "no_response"
    """
    path = path or DISPUTES_PATH
    disputes = load_disputes(path)
    for d in disputes:
        if d["dispute_id"] == dispute_id:
            d["status"] = status
            if resolution:
                d["resolution"] = resolution
                d["resolution_date"] = str(dt.date.today())
            save_disputes(disputes, path)
            return d
    return None


def check_for_resolutions(transactions, *, path=None):
    """Scan bank transactions for refund credits matching open disputes.

    A match is an inflow from a merchant whose name similarity is >= 1,
    with amount within 5% or $1, dated on or after the dispute filing date.

    Returns list of auto-closed dispute IDs.
    """
    from finance_mcp.store import subscription_creep as sc
    from finance_mcp.engines.receipt_scanner import merchant_similarity

    path = path or DISPUTES_PATH
    disputes = load_disputes(path)
    closed = []

    open_disputes = [d for d in disputes
                     if d["status"] in ("drafted", "sent", "pending")]
    if not open_disputes:
        return closed

    # Build inflow list (potential refund credits)
    refunds = []
    for t in transactions:
        if sc.is_outflow(t):
            continue
        amt = sc.amount_magnitude(t)
        d = sc.parse_date(t)
        name = sc.display_name(t)
        if amt is not None and d is not None and name:
            refunds.append({"amount": round(amt, 2), "date": d,
                            "merchant": name})

    for dispute in open_disputes:
        d_amt = dispute["amount"]
        d_merchant = dispute["merchant"]
        try:
            filed = dt.datetime.strptime(dispute["date_filed"],
                                         "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue

        for rtxn in refunds:
            # Amount tolerance: within $1 or 5%
            if abs(rtxn["amount"] - d_amt) > max(1.0, d_amt * 0.05):
                continue
            # Merchant match
            if merchant_similarity(d_merchant, rtxn["merchant"]) < 1:
                continue
            # Date: refund should be on or after the dispute filing date
            if rtxn["date"] < filed:
                continue

            dispute["status"] = "resolved"
            dispute["resolution"] = "refund_received"
            dispute["resolution_date"] = str(rtxn["date"])
            closed.append(dispute["dispute_id"])
            break

    if closed:
        save_disputes(disputes, path)
    return closed


def flag_expired_disputes(*, expiry_days=DISPUTE_EXPIRY_DAYS, path=None,
                          as_of=None):
    """Flag disputes with no response after expiry_days.

    Returns list of expired dispute IDs.
    """
    path = path or DISPUTES_PATH
    disputes = load_disputes(path)
    today = as_of or dt.date.today()
    expired = []

    for d in disputes:
        if d["status"] not in ("drafted", "sent", "pending"):
            continue
        try:
            filed = dt.datetime.strptime(d["date_filed"], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        if (today - filed).days >= expiry_days:
            d["status"] = "expired"
            d["resolution"] = "no_response"
            d["resolution_date"] = str(today)
            expired.append(d["dispute_id"])

    if expired:
        save_disputes(disputes, path)
    return expired


def dispute_summary(disputes=None, *, path=None):
    """Compact dispute summary for the finance digest."""
    path = path or DISPUTES_PATH
    if disputes is None:
        disputes = load_disputes(path)

    by_status = {}
    for d in disputes:
        by_status.setdefault(d["status"], []).append(d)

    open_disputes = [d for d in disputes
                     if d["status"] in ("drafted", "sent", "pending")]
    resolved = by_status.get("resolved", [])
    expired = by_status.get("expired", [])

    open_amount = sum(d["amount"] for d in open_disputes)
    resolved_amount = sum(d["amount"] for d in resolved)

    week_ago = dt.date.today() - dt.timedelta(days=7)
    recently_resolved = []
    for d in resolved:
        if d.get("resolution_date"):
            try:
                rd = dt.datetime.strptime(d["resolution_date"],
                                          "%Y-%m-%d").date()
                if rd >= week_ago:
                    recently_resolved.append(d)
            except (ValueError, TypeError):
                pass

    return {
        "tool": "dispute_agent",
        "as_of": str(dt.date.today()),
        "total": len(disputes),
        "open": len(open_disputes),
        "open_amount": round(open_amount, 2),
        "resolved": len(resolved),
        "resolved_amount": round(resolved_amount, 2),
        "expired": len(expired),
        "recently_resolved": len(recently_resolved),
        "recently_resolved_amount": round(
            sum(d["amount"] for d in recently_resolved), 2),
        "by_status": {s: len(ds) for s, ds in by_status.items()},
        "open_disputes": [
            {"dispute_id": d["dispute_id"], "merchant": d["merchant"],
             "amount": d["amount"], "type": d["type"], "reason": d["reason"],
             "date_filed": d["date_filed"], "status": d["status"]}
            for d in open_disputes
        ][:10],
    }


# ========================= EMAIL DRAFT GENERATION =========================

def _generate_refund_body(merchant, amount, date, expected_amount, reason,
                          evidence_summary, *, api_key=None):
    """Use Haiku to generate a polite, professional refund request body.

    PRIVACY: Only merchant name, amount, date, expected amount, and reason
    are sent.  Never account numbers, bank name, or transaction IDs.

    Returns (plain_text, html_body) tuple.  Falls back to template when no
    API key is available.
    """
    if expected_amount and abs(amount - expected_amount) > 0.01:
        context = (
            f"Merchant: {merchant}\n"
            f"Transaction date: {date}\n"
            f"Amount charged: ${amount:.2f}\n"
            f"Expected amount (per receipt): ${expected_amount:.2f}\n"
            f"Overcharge: ${abs(amount - expected_amount):.2f}\n"
            f"Reason: {reason}\n"
        )
        refund_ask = abs(amount - expected_amount)
    else:
        context = (
            f"Merchant: {merchant}\n"
            f"Transaction date: {date}\n"
            f"Amount: ${amount:.2f}\n"
            f"Reason: {reason}\n"
        )
        refund_ask = amount

    if evidence_summary:
        context += f"Evidence: {evidence_summary}\n"

    system = (
        "You are a professional customer service email writer. Write a polite "
        "but firm refund request email. Be concise (under 150 words), "
        "professional, and include all relevant details. Do not include "
        "any greeting line (no 'Dear', 'Hi', etc.) or sign-off (no "
        "'Sincerely', 'Best', etc.) — those will be added separately. "
        "Just write the body paragraphs."
    )
    user = (
        f"Write a refund request email body for this situation:\n\n{context}\n"
        f"Request a refund of ${refund_ask:.2f}. Be polite but clear."
    )

    llm_body = llm_matcher._call_haiku(system, user, api_key=api_key)

    if llm_body and not llm_body.startswith("_("):
        plain = (f"Dear {merchant} Support,\n\n"
                 f"{llm_body}\n\n"
                 f"Thank you,\n{ACCOUNT_HOLDER}")
    else:
        # Template fallback
        if expected_amount and abs(amount - expected_amount) > 0.01:
            diff = abs(amount - expected_amount)
            plain = (
                f"Dear {merchant} Support,\n\n"
                f"I am writing to request a refund for a billing discrepancy. "
                f"On {date}, I was charged ${amount:.2f}, however my receipt "
                f"shows the correct amount should be ${expected_amount:.2f}. "
                f"This represents an overcharge of ${diff:.2f}.\n\n"
                f"I kindly request a refund of ${diff:.2f} "
                f"to my original payment method.\n\n"
                f"Thank you for your prompt attention to this matter.\n\n"
                f"{ACCOUNT_HOLDER}"
            )
        elif reason == "duplicate_charge":
            plain = (
                f"Dear {merchant} Support,\n\n"
                f"I am writing regarding a duplicate charge on my account. "
                f"On {date}, a charge of ${amount:.2f} was applied that "
                f"appears to be a duplicate of a previous legitimate "
                f"transaction.\n\n"
                f"I kindly request a refund of ${amount:.2f} for the "
                f"duplicate charge to my original payment method.\n\n"
                f"Thank you for your prompt attention to this matter.\n\n"
                f"{ACCOUNT_HOLDER}"
            )
        else:
            plain = (
                f"Dear {merchant} Support,\n\n"
                f"I am writing to request a refund for a charge of "
                f"${amount:.2f} on {date}. Reason: {reason}.\n\n"
                f"I kindly request a full refund of ${amount:.2f} to my "
                f"original payment method.\n\n"
                f"Thank you for your prompt attention to this matter.\n\n"
                f"{ACCOUNT_HOLDER}"
            )

    html = _wrap_refund_html(plain, merchant, amount, date, reason)
    return plain, html


def _wrap_refund_html(plain_text, merchant, amount, date, reason):
    """Wrap plain text in a clean HTML email template."""
    paras = plain_text.split("\n\n")
    body_parts = "".join(
        f'<p style="margin:0 0 12px 0;line-height:1.6;">{p.replace(chr(10), "<br>")}</p>'
        for p in paras if p.strip()
    )

    return (
        f'<div style="font-family:{_FONT_SANS};max-width:600px;'
        f'margin:0 auto;color:#333333;">'
        f'<div style="background:#f8f9fa;border-radius:8px;padding:16px;'
        f'margin-bottom:16px;border-left:4px solid #0f3460;">'
        f'<strong style="color:#0f3460;">Refund Request</strong><br>'
        f'<span style="color:#666;font-size:14px;">'
        f'{merchant} &middot; ${amount:.2f} &middot; {date}<br>'
        f'Reason: {reason}</span></div>'
        f'<div style="font-size:15px;">{body_parts}</div></div>'
    )


def _generate_bank_dispute_letter(merchant, amount, date, reason, evidence):
    """Generate a formal bank dispute letter with placeholders.

    Returns (plain_text, html_body) tuple.
    Placeholders: [ACCOUNT_NUMBER], [BANK_NAME], [BANK_DISPUTE_EMAIL]
    """
    reason_text = {
        "unauthorized_charge":
            "I did not authorize this transaction and do not recognize it.",
        "duplicate_charge":
            "This charge appears to be a duplicate of a previous legitimate "
            "transaction.",
        "amount_discrepancy":
            f"The amount charged (${amount:.2f}) differs from the agreed or "
            f"expected amount.",
        "goods_not_received":
            "I did not receive the goods or services associated with this "
            "charge.",
    }.get(reason, f"Disputed: {reason}")

    evidence_lines = []
    if evidence:
        if evidence.get("receipt_amount"):
            evidence_lines.append(
                f"  - Receipt shows: ${evidence['receipt_amount']:.2f}")
        if evidence.get("duplicate_dates"):
            evidence_lines.append(
                f"  - Original charge date: "
                f"{evidence['duplicate_dates'][0]}")
        if evidence.get("details"):
            evidence_lines.append(f"  - {evidence['details']}")
    evidence_text = "\n".join(evidence_lines)

    plain = (
        f"[BANK_NAME] Disputes Department\n"
        f"[BANK_DISPUTE_EMAIL]\n\n"
        f"Date: {dt.date.today().strftime('%B %d, %Y')}\n\n"
        f"Re: Dispute of Transaction - {merchant}, ${amount:.2f}, {date}\n\n"
        f"To Whom It May Concern,\n\n"
        f"I am writing to formally dispute a charge on my account.\n\n"
        f"Account Holder: {ACCOUNT_HOLDER}\n"
        f"Account Number: [ACCOUNT_NUMBER]\n"
        f"Transaction Date: {date}\n"
        f"Transaction Amount: ${amount:.2f}\n"
        f"Merchant: {merchant}\n\n"
        f"Reason for Dispute:\n"
        f"{reason_text}\n\n"
    )

    if evidence_text:
        plain += f"Supporting Evidence:\n{evidence_text}\n\n"

    plain += (
        f"I request that this charge of ${amount:.2f} be investigated and "
        f"credited back to my account. Please confirm receipt of this "
        f"dispute and provide a reference number for my records.\n\n"
        f"Sincerely,\n{ACCOUNT_HOLDER}"
    )

    html = _wrap_dispute_html(plain, merchant, amount, date, reason)
    return plain, html


def _wrap_dispute_html(plain_text, merchant, amount, date, reason):
    """Wrap dispute letter in formal HTML."""
    paras = plain_text.split("\n\n")
    body_parts = "".join(
        f'<p style="margin:0 0 12px 0;line-height:1.8;">'
        f'{p.replace(chr(10), "<br>")}</p>'
        for p in paras if p.strip()
    )

    return (
        f'<div style="font-family:Georgia,\'Times New Roman\',serif;'
        f'max-width:600px;margin:0 auto;color:#333333;">'
        f'<div style="background:#fff3cd;border-radius:8px;padding:16px;'
        f'margin-bottom:16px;border-left:4px solid #e94560;">'
        f'<strong style="color:#e94560;">Bank Dispute Letter</strong><br>'
        f'<span style="color:#666;font-size:14px;">'
        f'{merchant} &middot; ${amount:.2f} &middot; {date}<br>'
        f'Fill in [ACCOUNT_NUMBER] and [BANK_NAME] before sending'
        f'</span></div>'
        f'<div style="font-size:15px;">{body_parts}</div></div>'
    )


# ========================= DRAFT BUILDING =========================

def build_refund_draft(finding, *, api_key=None, contacts_path=None,
                       disputes_path=None):
    """Build a refund request draft from a reconciliation/fee_fraud finding.

    Returns a draft dict:
      {to, subject, body, htmlBody, dispute_id, merchant, amount}
    ready for Gmail MCP create_draft.
    """
    contacts_path = contacts_path or MERCHANT_CONTACTS_PATH
    disputes_path = disputes_path or DISPUTES_PATH

    merchant = (finding.get("merchant")
                or finding.get("txn_merchant")
                or "Unknown")
    amount = finding.get("amount") or finding.get("txn_amount", 0)
    date = finding.get("date") or finding.get("txn_date", "")
    expected = finding.get("receipt_amount") or finding.get("expected_amount")
    finding_type = finding.get("type", "")
    evidence_summary = finding.get("message", "")

    # Determine reason and refund amount
    if finding_type == "duplicate":
        reason = "duplicate_charge"
        refund_amount = amount
    elif expected and abs(amount - expected) > 0.01:
        reason = "amount_discrepancy"
        refund_amount = abs(amount - expected)
    else:
        reason = finding.get("reason", "billing_discrepancy")
        refund_amount = amount

    # Look up merchant contact
    contact = lookup_merchant_contact(merchant,
                                      contacts_path=contacts_path,
                                      api_key=api_key)
    to_email = contact["email"]

    # Build evidence dict
    evidence = {}
    if expected:
        evidence["receipt_amount"] = expected
    if finding.get("dates"):
        evidence["duplicate_dates"] = finding["dates"]
    if evidence_summary:
        evidence["details"] = evidence_summary

    # Create dispute record
    dispute = create_dispute(
        dispute_type="refund_request",
        merchant=merchant,
        amount=round(refund_amount, 2),
        date=date,
        reason=reason,
        evidence=evidence,
        draft_created=True,
        path=disputes_path,
    )

    # Generate email content
    plain, html = _generate_refund_body(
        merchant, amount, date, expected, reason, evidence_summary,
        api_key=api_key,
    )

    subject = f"Refund Request - {merchant} ${refund_amount:.2f} ({date})"

    return {
        "to": [to_email] if to_email else [],
        "subject": subject,
        "body": plain,
        "htmlBody": html,
        "dispute_id": dispute["dispute_id"],
        "merchant": merchant,
        "amount": round(refund_amount, 2),
    }


def build_bank_dispute_draft(finding, *, bank_email=None, api_key=None,
                              disputes_path=None):
    """Build a bank dispute letter draft from an unverified charge.

    Returns a draft dict ready for Gmail MCP create_draft.
    """
    bank_email = bank_email or DEFAULT_BANK_EMAIL
    disputes_path = disputes_path or DISPUTES_PATH

    merchant = finding.get("merchant", "Unknown")
    amount = finding.get("amount", 0)
    date = finding.get("date", "")
    reason = finding.get("reason", "unauthorized_charge")

    evidence = {}
    if finding.get("receipt_amount"):
        evidence["receipt_amount"] = finding["receipt_amount"]
        reason = "amount_discrepancy"
    if finding.get("message"):
        evidence["details"] = finding["message"]

    # Create dispute record
    dispute = create_dispute(
        dispute_type="bank_dispute",
        merchant=merchant,
        amount=round(amount, 2),
        date=date,
        reason=reason,
        evidence=evidence,
        draft_created=True,
        path=disputes_path,
    )

    plain, html = _generate_bank_dispute_letter(
        merchant, amount, date, reason, evidence)

    subject = f"Transaction Dispute - {merchant} ${amount:.2f} ({date})"

    return {
        "to": [bank_email],
        "subject": subject,
        "body": plain,
        "htmlBody": html,
        "dispute_id": dispute["dispute_id"],
        "merchant": merchant,
        "amount": round(amount, 2),
    }


# ========================= PIPELINE INTEGRATION =========================

def process_findings(reconciliation=None, fee_fraud_summary=None,
                     transactions=None, *,
                     auto_draft=False,
                     threshold=DEFAULT_DISPUTE_THRESHOLD,
                     bank_email=None,
                     api_key=None,
                     disputes_path=None,
                     contacts_path=None):
    """Process reconciliation + fee_fraud findings into disputes.

    Parameters
    ----------
    reconciliation : dict | None
        Raw result from receipt_scanner.reconcile().
    fee_fraud_summary : dict | None
        Result from fee_fraud_scan.scan().
    transactions : list | None
        Bank transactions for resolution checking.
    auto_draft : bool
        If True, generate email draft content for actionable findings.
    threshold : float
        Minimum amount for bank dispute letters (default $25).

    Returns
    -------
    dict with keys: drafts, new_disputes, resolved, expired, summary.
    """
    bank_email = bank_email or DEFAULT_BANK_EMAIL
    disputes_path = disputes_path or DISPUTES_PATH
    contacts_path = contacts_path or MERCHANT_CONTACTS_PATH

    drafts = []
    new_disputes = []

    # --- Discrepancies from reconciliation -> refund requests ---
    if reconciliation:
        for disc in reconciliation.get("discrepancies", []):
            finding = {
                "merchant": disc.get("txn_merchant",
                                     disc.get("receipt_merchant")),
                "amount": disc.get("txn_amount", 0),
                "txn_amount": disc.get("txn_amount", 0),
                "date": disc.get("txn_date", ""),
                "receipt_amount": disc.get("receipt_amount"),
                "reason": "amount_discrepancy",
                "message": disc.get("message", ""),
                "type": "discrepancy",
            }
            if auto_draft:
                draft = build_refund_draft(
                    finding, api_key=api_key,
                    contacts_path=contacts_path,
                    disputes_path=disputes_path)
                drafts.append(draft)
                new_disputes.append(draft["dispute_id"])
            else:
                dispute = create_dispute(
                    dispute_type="refund_request",
                    merchant=finding["merchant"],
                    amount=round(abs(disc.get("abs_difference", 0)), 2),
                    date=finding["date"],
                    reason="amount_discrepancy",
                    evidence={"receipt_amount": disc.get("receipt_amount"),
                              "details": disc.get("message", "")},
                    path=disputes_path,
                )
                new_disputes.append(dispute["dispute_id"])

    # --- Duplicates from fee_fraud -> refund requests ---
    if fee_fraud_summary:
        for dup in fee_fraud_summary.get("detail", {}).get("duplicates", []):
            finding = {
                "merchant": dup.get("merchant", "Unknown"),
                "amount": dup.get("recoverable", dup.get("amount", 0)),
                "date": (dup.get("dates", [""])[1]
                         if len(dup.get("dates", [])) > 1 else ""),
                "dates": dup.get("dates", []),
                "reason": "duplicate_charge",
                "message": (
                    f"Duplicate charge: {dup.get('merchant')} "
                    f"${dup.get('amount', 0):.2f} on "
                    f"{' & '.join(dup.get('dates', []))}"),
                "type": "duplicate",
            }
            if auto_draft:
                draft = build_refund_draft(
                    finding, api_key=api_key,
                    contacts_path=contacts_path,
                    disputes_path=disputes_path)
                drafts.append(draft)
                new_disputes.append(draft["dispute_id"])
            else:
                dispute = create_dispute(
                    dispute_type="refund_request",
                    merchant=finding["merchant"],
                    amount=round(finding["amount"], 2),
                    date=finding["date"],
                    reason="duplicate_charge",
                    evidence={"duplicate_dates": dup.get("dates", []),
                              "details": finding["message"]},
                    path=disputes_path,
                )
                new_disputes.append(dispute["dispute_id"])

    # --- Unverified charges over threshold -> bank disputes ---
    # Use reconciliation.unmatched_charges (source of truth) to avoid
    # double-counting with fee_fraud which already includes them.
    unverified_source = []
    if reconciliation:
        unverified_source = reconciliation.get("unmatched_charges", [])
    elif fee_fraud_summary:
        unverified_source = fee_fraud_summary.get(
            "detail", {}).get("unverified_charges", [])

    for uv in unverified_source:
        amt = uv.get("amount", 0)
        if amt < threshold:
            continue
        finding = {
            "merchant": uv.get("merchant", "Unknown"),
            "amount": amt,
            "date": uv.get("date", ""),
            "reason": "unauthorized_charge",
            "message": uv.get("message", ""),
        }
        if auto_draft:
            draft = build_bank_dispute_draft(
                finding, bank_email=bank_email, api_key=api_key,
                disputes_path=disputes_path)
            drafts.append(draft)
            new_disputes.append(draft["dispute_id"])
        else:
            dispute = create_dispute(
                dispute_type="bank_dispute",
                merchant=finding["merchant"],
                amount=round(amt, 2),
                date=finding["date"],
                reason="unauthorized_charge",
                evidence={"details": uv.get("message", "")},
                path=disputes_path,
            )
            new_disputes.append(dispute["dispute_id"])

    # --- Check for resolutions in bank transactions ---
    resolved = []
    if transactions:
        resolved = check_for_resolutions(transactions, path=disputes_path)

    # --- Flag expired disputes ---
    expired = flag_expired_disputes(path=disputes_path)

    # --- Build summary ---
    summary = dispute_summary(path=disputes_path)

    return {
        "drafts": drafts,
        "new_disputes": new_disputes,
        "resolved": resolved,
        "expired": expired,
        "summary": summary,
    }


# ========================= RENDERING =========================

def render_status(disputes=None, *, path=None):
    """Render dispute status as markdown."""
    path = path or DISPUTES_PATH
    if disputes is None:
        disputes = load_disputes(path)

    s = dispute_summary(disputes, path=path)
    L = []
    L.append("# finance.mcp - DISPUTE STATUS")
    L.append(f"_as of {dt.date.today()}_\n")
    L.append(f"## Overview: {s['open']} open, {s['resolved']} resolved, "
             f"{s['expired']} expired")
    L.append(f"- Open dispute amount: {money(s['open_amount'])}")
    L.append(f"- Recovered (resolved): {money(s['resolved_amount'])}")

    if s["recently_resolved"] > 0:
        L.append(f"- Resolved this week: {s['recently_resolved']} "
                 f"({money(s['recently_resolved_amount'])})")

    if s["open_disputes"]:
        L.append("\n## Open Disputes")
        for d in s["open_disputes"]:
            L.append(
                f"- [{d['dispute_id']}] {d['merchant']} "
                f"{money(d['amount'])} ({d['type']}, {d['reason']}) "
                f"- {d['status']} since {d['date_filed']}")

    all_resolved = [d for d in disputes if d["status"] == "resolved"]
    if all_resolved:
        L.append("\n## Resolved")
        for d in all_resolved[-5:]:
            L.append(
                f"- [{d['dispute_id']}] {d['merchant']} "
                f"{money(d['amount'])} - {d.get('resolution', '?')} "
                f"on {d.get('resolution_date', '?')}")

    return "\n".join(L)


# --------------------------------- CLI ----------------------------------------

def main():
    import argparse
    ap = argparse.ArgumentParser(
        description="Dispute/refund automation for the finance.mcp suite.")
    ap.add_argument("--status", action="store_true",
                    help="show current dispute status")
    ap.add_argument("--json", action="store_true",
                    help="output as JSON")
    ap.add_argument("--disputes-file", default=DISPUTES_PATH,
                    help="disputes ledger path")
    a = ap.parse_args()

    if a.status:
        if a.json:
            s = dispute_summary(path=a.disputes_file)
            print(json.dumps(s, indent=2))
        else:
            print(render_status(path=a.disputes_file))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
