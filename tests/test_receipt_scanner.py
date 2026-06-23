#!/usr/bin/env python3
"""
test_receipt_scanner.py — tests for the receipt reconciliation engine.

Tests:
  - Amount/date/merchant extraction (unchanged helpers)
  - Email classification
  - Receipt parsing
  - Merchant normalisation + similarity scoring
  - Reconciliation matching (exact, fuzzy, no match)
  - Amount discrepancy detection
  - Pending receipt detection (configurable day threshold)
  - Price change detection
  - Unmatched bank charge detection
  - Integration with fee_fraud_scan (reconciliation parameter)
  - Graceful degradation (no receipts, no transactions)
  - Summary construction

Uses synthetic data — no Gmail MCP calls.
"""

import datetime as dt
import unittest

from finance_mcp.engines import receipt_scanner as rs


# ─────────────────────────── extraction helpers ──────────────────────────────

class ExtractAmountTest(unittest.TestCase):

    def test_dollar_sign_decimal(self):
        self.assertEqual(rs.extract_amount("Total: $42.99"), 42.99)

    def test_dollar_sign_thousands(self):
        self.assertEqual(rs.extract_amount("Amount: $1,234.56"), 1234.56)

    def test_usd_prefix(self):
        self.assertEqual(rs.extract_amount("Charged USD 15.00"), 15.00)

    def test_dollars_suffix(self):
        self.assertEqual(rs.extract_amount("Total 99.99 dollars"), 99.99)

    def test_no_amount_returns_none(self):
        self.assertIsNone(rs.extract_amount("No money here"))

    def test_empty_returns_none(self):
        self.assertIsNone(rs.extract_amount(""))
        self.assertIsNone(rs.extract_amount(None))

    def test_dollar_no_cents(self):
        self.assertEqual(rs.extract_amount("$50"), 50.0)

    def test_first_amount_wins(self):
        amt = rs.extract_amount("Subtotal $10.00 Tax $1.50 Total $11.50")
        self.assertEqual(amt, 10.00)


class ExtractDateTest(unittest.TestCase):

    def test_full_month_name(self):
        self.assertEqual(rs.extract_date("Date: June 15, 2026"), dt.date(2026, 6, 15))

    def test_slash_format(self):
        self.assertEqual(rs.extract_date("06/15/2026"), dt.date(2026, 6, 15))

    def test_iso_format(self):
        self.assertEqual(rs.extract_date("Transaction on 2026-06-15"), dt.date(2026, 6, 15))

    def test_no_date_returns_fallback(self):
        d = rs.extract_date("no date here", fallback=dt.date(2026, 1, 1))
        self.assertEqual(d, dt.date(2026, 1, 1))

    def test_empty_returns_fallback(self):
        self.assertIsNone(rs.extract_date(""))
        self.assertIsNone(rs.extract_date(None))


class ExtractDateFromEmailTest(unittest.TestCase):

    def test_rfc2822_format(self):
        d = rs.extract_date_from_email_date("Tue, 15 Jun 2026 14:30:00 +0000")
        self.assertEqual(d, dt.date(2026, 6, 15))

    def test_iso_format(self):
        d = rs.extract_date_from_email_date("2026-06-15")
        self.assertEqual(d, dt.date(2026, 6, 15))

    def test_none_returns_none(self):
        self.assertIsNone(rs.extract_date_from_email_date(None))
        self.assertIsNone(rs.extract_date_from_email_date(""))


class IdentifyMerchantTest(unittest.TestCase):

    def test_amazon_receipt(self):
        name, cat = rs.identify_merchant(
            "order-update@amazon.com", "Your Amazon.com order has shipped")
        self.assertEqual(name, "Amazon")
        self.assertEqual(cat, "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES")

    def test_netflix_billing(self):
        name, cat = rs.identify_merchant(
            "info@netflix.com", "Your Netflix billing statement")
        self.assertEqual(name, "Netflix")

    def test_unknown_domain_fallback(self):
        name, cat = rs.identify_merchant(
            "noreply@acmecorp.com", "Your receipt")
        self.assertEqual(name, "Acmecorp")

    def test_generic_email_returns_none(self):
        name, cat = rs.identify_merchant("friend@gmail.com", "Hey what's up")
        self.assertIsNone(name)


class ClassifyEmailTypeTest(unittest.TestCase):

    def test_receipt(self):
        self.assertEqual(rs.classify_email_type("Your order confirmation"), "receipt")

    def test_bank_alert(self):
        self.assertEqual(rs.classify_email_type("Transaction alert: Direct deposit"), "bank_alert")

    def test_bill(self):
        self.assertEqual(rs.classify_email_type("Your bill is ready"), "bill")

    def test_unknown(self):
        self.assertEqual(rs.classify_email_type("Meeting tomorrow"), "unknown")


class ParseReceiptTest(unittest.TestCase):

    def _make_thread(self, subject, from_addr, body, date_str="2026-06-15"):
        return {
            "id": "thread_123",
            "messages": [{
                "id": "msg_456", "subject": subject, "from": from_addr,
                "date": date_str, "snippet": body[:200], "plaintext_body": body,
                "headers": [
                    {"name": "Subject", "value": subject},
                    {"name": "From", "value": from_addr},
                    {"name": "Date", "value": date_str},
                ],
            }],
        }

    def test_amazon_order(self):
        thread = self._make_thread(
            "Your Amazon.com order of $42.99 has shipped",
            "shipment-tracking@amazon.com",
            "Order Total: $42.99\nShipped on June 15, 2026")
        receipt = rs.parse_receipt(thread)
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["merchant"], "Amazon")
        self.assertEqual(receipt["amount"], 42.99)
        self.assertEqual(receipt["type"], "receipt")

    def test_no_financial_data_returns_none(self):
        thread = self._make_thread(
            "Team meeting tomorrow", "boss@company.com",
            "Let's sync up on the project status.")
        self.assertIsNone(rs.parse_receipt(thread))

    def test_empty_messages_returns_none(self):
        self.assertIsNone(rs.parse_receipt({"messages": []}))
        self.assertIsNone(rs.parse_receipt({}))


# ─────────────────────────── merchant normalisation ──────────────────────────

class MerchantNormaliseTest(unittest.TestCase):

    def test_strip_suffix(self):
        self.assertEqual(rs.normalize_merchant_name("Anthropic, PBC"), "anthropic")

    def test_strip_inc(self):
        self.assertEqual(rs.normalize_merchant_name("Acme Inc."), "acme")

    def test_strip_llc(self):
        self.assertEqual(rs.normalize_merchant_name("Smith LLC"), "smith")

    def test_lowercase(self):
        self.assertEqual(rs.normalize_merchant_name("SHOPIFY"), "shopify")

    def test_empty(self):
        self.assertEqual(rs.normalize_merchant_name(""), "")
        self.assertEqual(rs.normalize_merchant_name(None), "")


class MerchantSimilarityTest(unittest.TestCase):

    def test_exact_match(self):
        self.assertEqual(rs.merchant_similarity("Shopify", "Shopify"), 3)

    def test_exact_after_normalisation(self):
        self.assertEqual(rs.merchant_similarity("Shopify Inc.", "SHOPIFY"), 3)

    def test_substring_match(self):
        self.assertEqual(rs.merchant_similarity("Netflix", "Netflix Premium"), 2)

    def test_token_overlap(self):
        self.assertEqual(rs.merchant_similarity("China House", "China House Restaurant"), 2)

    def test_no_match(self):
        self.assertEqual(rs.merchant_similarity("Amazon", "Netflix"), 0)

    def test_empty(self):
        self.assertEqual(rs.merchant_similarity("", "Netflix"), 0)


# ─────────────────────────── reconciliation engine ───────────────────────────

def _txn(name, amount, date, txn_id="TX_X"):
    """Helper: create a minimal bank transaction dict."""
    return {
        "transaction_id": txn_id,
        "type": "debit",
        "amount": amount,
        "date": date,
        "merchantName": name,
        "description": name,
        "rawData": {
            "transaction_id": txn_id,
            "amount": amount,
            "date": date,
            "merchant_name": name,
        },
    }


def _receipt(merchant, amount, date, thread_id="t_X"):
    """Helper: create a minimal receipt dict."""
    return {
        "thread_id": thread_id,
        "merchant": merchant,
        "amount": amount,
        "date": date,
        "category": "TEST",
        "subject": f"Receipt from {merchant}",
        "from": f"noreply@{merchant.lower()}.com",
        "type": "receipt",
    }


class ReconcileExactMatchTest(unittest.TestCase):
    """Receipts that exactly match a bank transaction → verified."""

    def test_exact_match(self):
        receipts = [_receipt("Shopify", 41.83, "2026-06-10")]
        txns = [_txn("Shopify", 41.83, "2026-06-10", "TX_1")]
        result = rs.reconcile(receipts, txns, as_of=dt.date(2026, 6, 17))

        self.assertEqual(len(result["matched"]), 1)
        self.assertEqual(len(result["discrepancies"]), 0)
        self.assertEqual(len(result["unmatched_receipts"]), 0)
        self.assertEqual(result["coverage"]["pct"], 100.0)

    def test_match_within_date_tolerance(self):
        """Receipt 2 days before the bank charge should still match."""
        receipts = [_receipt("Netflix", 15.99, "2026-06-12")]
        txns = [_txn("Netflix", 15.99, "2026-06-14", "TX_2")]
        result = rs.reconcile(receipts, txns, as_of=dt.date(2026, 6, 17))

        self.assertEqual(len(result["matched"]), 1)

    def test_match_within_amount_tolerance(self):
        """Amounts within 5% AND $2 should match as verified."""
        receipts = [_receipt("Shopify", 41.50, "2026-06-10")]
        txns = [_txn("Shopify", 41.83, "2026-06-10", "TX_1")]
        result = rs.reconcile(receipts, txns, as_of=dt.date(2026, 6, 17))

        # Difference is $0.33 and 0.8% — within both tolerances
        self.assertEqual(len(result["matched"]), 1)
        self.assertEqual(len(result["discrepancies"]), 0)

    def test_fuzzy_merchant_name(self):
        """Merchant names with suffix differences should still match."""
        receipts = [_receipt("Anthropic", 200.00, "2026-06-10")]
        txns = [_txn("Anthropic PBC", 200.00, "2026-06-10", "TX_3")]
        result = rs.reconcile(receipts, txns, as_of=dt.date(2026, 6, 17))

        self.assertEqual(len(result["matched"]), 1)

    def test_inflows_excluded_from_matching(self):
        """Credit transactions (inflows) should not be matched."""
        receipts = [_receipt("Payroll", 3000.00, "2026-06-15")]
        txns = [{
            "transaction_id": "TX_IN",
            "type": "credit",  # inflow — positive top-level amount
            "amount": 3000.00,
            "date": "2026-06-15",
            "merchantName": "Payroll",
            "rawData": {"amount": 3000.00, "date": "2026-06-15",
                        "merchant_name": "Payroll"},
        }]
        result = rs.reconcile(receipts, txns, as_of=dt.date(2026, 6, 17))

        self.assertEqual(len(result["matched"]), 0)
        self.assertEqual(len(result["unmatched_receipts"]), 1)


class ReconcileDiscrepancyTest(unittest.TestCase):
    """Merchant + date match but amount differs → discrepancy."""

    def test_amount_discrepancy(self):
        receipts = [_receipt("Shopify", 38.99, "2026-06-10")]
        txns = [_txn("Shopify", 41.83, "2026-06-10", "TX_1")]
        result = rs.reconcile(receipts, txns, as_of=dt.date(2026, 6, 17))

        self.assertEqual(len(result["matched"]), 0)
        self.assertEqual(len(result["discrepancies"]), 1)
        d = result["discrepancies"][0]
        self.assertAlmostEqual(d["abs_difference"], 2.84, places=2)
        self.assertIn("Shopify", d["message"])
        self.assertIn("$41.83", d["message"])
        self.assertIn("$38.99", d["message"])

    def test_large_discrepancy(self):
        receipts = [_receipt("Amazon", 50.00, "2026-06-10")]
        txns = [_txn("Amazon", 100.00, "2026-06-10", "TX_2")]
        result = rs.reconcile(receipts, txns, as_of=dt.date(2026, 6, 17))

        self.assertEqual(len(result["discrepancies"]), 1)
        self.assertEqual(result["discrepancies"][0]["abs_difference"], 50.0)


class ReconcileUnmatchedReceiptTest(unittest.TestCase):
    """Receipt exists but no matching bank charge."""

    def test_unmatched_receipt_recent(self):
        receipts = [_receipt("NewStore", 25.00, "2026-06-16")]
        txns = []
        result = rs.reconcile(receipts, txns, as_of=dt.date(2026, 6, 17),
                              pending_threshold_days=3)

        self.assertEqual(len(result["unmatched_receipts"]), 1)
        self.assertEqual(result["unmatched_receipts"][0]["status"], "recent")
        self.assertEqual(result["unmatched_receipts"][0]["days_since"], 1)

    def test_unmatched_receipt_pending(self):
        receipts = [_receipt("Runpod", 50.00, "2026-06-12")]
        txns = []
        result = rs.reconcile(receipts, txns, as_of=dt.date(2026, 6, 17),
                              pending_threshold_days=3)

        self.assertEqual(len(result["unmatched_receipts"]), 1)
        ur = result["unmatched_receipts"][0]
        self.assertEqual(ur["status"], "pending_or_declined")
        self.assertEqual(ur["days_since"], 5)
        self.assertIn("Runpod", ur["message"])
        self.assertIn("no matching bank charge", ur["message"])

    def test_configurable_threshold(self):
        """With a 7-day threshold, 5-day-old receipt should be 'recent'."""
        receipts = [_receipt("Runpod", 50.00, "2026-06-12")]
        txns = []
        result = rs.reconcile(receipts, txns, as_of=dt.date(2026, 6, 17),
                              pending_threshold_days=7)

        self.assertEqual(result["unmatched_receipts"][0]["status"], "recent")

    def test_incomplete_data_goes_to_incomplete(self):
        receipts = [_receipt("Unknown", None, None)]
        txns = []
        result = rs.reconcile(receipts, txns, as_of=dt.date(2026, 6, 17))

        self.assertEqual(len(result["incomplete"]), 1)
        self.assertEqual(len(result["unmatched_receipts"]), 0)


class ReconcileUnmatchedChargeTest(unittest.TestCase):
    """Bank charge from a receipt-sending merchant with no matching receipt."""

    def test_unmatched_charge_from_known_sender(self):
        receipts = [_receipt("Shopify", 41.83, "2026-06-10")]
        txns = [
            _txn("Shopify", 41.83, "2026-06-10", "TX_1"),  # matched
            _txn("Netflix", 15.99, "2026-06-11", "TX_2"),   # known sender, no receipt
        ]
        result = rs.reconcile(receipts, txns, as_of=dt.date(2026, 6, 17))

        self.assertEqual(len(result["matched"]), 1)
        # Netflix is a known receipt sender — should appear as unmatched charge
        self.assertTrue(any(u["merchant"] == "Netflix"
                            for u in result["unmatched_charges"]))

    def test_unknown_merchant_not_flagged(self):
        """Charges from merchants not in RECEIPT_SENDERS should not be flagged."""
        receipts = [_receipt("Shopify", 41.83, "2026-06-10")]
        txns = [
            _txn("Shopify", 41.83, "2026-06-10", "TX_1"),
            _txn("Corner Deli", 8.50, "2026-06-11", "TX_3"),  # not a receipt sender
        ]
        result = rs.reconcile(receipts, txns, as_of=dt.date(2026, 6, 17))

        self.assertFalse(any(u["merchant"] == "Corner Deli"
                             for u in result["unmatched_charges"]))


class ReconcilePriceChangeTest(unittest.TestCase):
    """Price changes detected from multiple receipts for the same merchant."""

    def test_price_increase(self):
        receipts = [
            _receipt("Netflix", 15.99, "2026-05-10", "t1"),
            _receipt("Netflix", 17.99, "2026-06-10", "t2"),
        ]
        txns = []
        result = rs.reconcile(receipts, txns, as_of=dt.date(2026, 6, 17))

        self.assertEqual(len(result["price_changes"]), 1)
        pc = result["price_changes"][0]
        self.assertEqual(pc["merchant"], "Netflix")
        self.assertAlmostEqual(pc["change"], 2.0)
        self.assertIn("price increase", pc["message"])

    def test_price_decrease(self):
        receipts = [
            _receipt("Spotify", 12.99, "2026-05-10", "t1"),
            _receipt("Spotify", 9.99, "2026-06-10", "t2"),
        ]
        txns = []
        result = rs.reconcile(receipts, txns, as_of=dt.date(2026, 6, 17))

        self.assertEqual(len(result["price_changes"]), 1)
        self.assertIn("price decrease", result["price_changes"][0]["message"])

    def test_stable_price_no_flag(self):
        """Two receipts at the same price → no price change flagged."""
        receipts = [
            _receipt("Shopify", 41.83, "2026-05-10", "t1"),
            _receipt("Shopify", 41.83, "2026-06-10", "t2"),
        ]
        txns = []
        result = rs.reconcile(receipts, txns, as_of=dt.date(2026, 6, 17))

        self.assertEqual(len(result["price_changes"]), 0)

    def test_minor_variation_no_flag(self):
        """Less than $1 and <5% → not flagged."""
        receipts = [
            _receipt("Shopify", 41.83, "2026-05-10", "t1"),
            _receipt("Shopify", 42.10, "2026-06-10", "t2"),
        ]
        txns = []
        result = rs.reconcile(receipts, txns, as_of=dt.date(2026, 6, 17))

        self.assertEqual(len(result["price_changes"]), 0)


class ReconcileCoverageTest(unittest.TestCase):
    """Coverage stat computation."""

    def test_full_coverage(self):
        receipts = [_receipt("Shopify", 41.83, "2026-06-10")]
        txns = [_txn("Shopify", 41.83, "2026-06-10")]
        result = rs.reconcile(receipts, txns, as_of=dt.date(2026, 6, 17))
        self.assertEqual(result["coverage"]["pct"], 100.0)

    def test_partial_coverage(self):
        receipts = [
            _receipt("Shopify", 41.83, "2026-06-10", "t1"),
            _receipt("Netflix", 15.99, "2026-06-11", "t2"),
        ]
        txns = [_txn("Shopify", 41.83, "2026-06-10")]
        result = rs.reconcile(receipts, txns, as_of=dt.date(2026, 6, 17))
        self.assertEqual(result["coverage"]["pct"], 50.0)

    def test_zero_receipts_100_pct(self):
        result = rs.reconcile([], [], as_of=dt.date(2026, 6, 17))
        self.assertEqual(result["coverage"]["pct"], 100.0)


class ReconcileGracefulDegradationTest(unittest.TestCase):
    """If no receipts or no transactions, existing analysis works unchanged."""

    def test_no_receipts(self):
        txns = [_txn("Shopify", 41.83, "2026-06-10")]
        result = rs.reconcile([], txns, as_of=dt.date(2026, 6, 17))

        self.assertEqual(len(result["matched"]), 0)
        self.assertEqual(len(result["discrepancies"]), 0)
        self.assertEqual(len(result["unmatched_receipts"]), 0)
        self.assertEqual(len(result["unmatched_charges"]), 0)
        self.assertEqual(result["coverage"]["pct"], 100.0)

    def test_no_transactions(self):
        receipts = [_receipt("Shopify", 41.83, "2026-06-10")]
        result = rs.reconcile(receipts, [], as_of=dt.date(2026, 6, 17))

        self.assertEqual(len(result["matched"]), 0)
        self.assertEqual(len(result["unmatched_receipts"]), 1)
        self.assertEqual(result["coverage"]["pct"], 0.0)


# ─────────────────────────── summary construction ────────────────────────────

class ReconciliationSummaryTest(unittest.TestCase):

    def test_summary_structure(self):
        receipts = [
            _receipt("Shopify", 41.83, "2026-06-10", "t1"),
            _receipt("Shopify", 38.99, "2026-06-10", "t2"),
        ]
        txns = [
            _txn("Shopify", 41.83, "2026-06-10", "TX_1"),
        ]
        recon = rs.reconcile(receipts, txns, as_of=dt.date(2026, 6, 17))
        summary = rs.reconciliation_to_summary(recon)

        self.assertEqual(summary["tool"], "receipt_reconciliation")
        self.assertIn("headline", summary)
        self.assertIn("for_fee_fraud", summary)
        self.assertIn("for_subscriptions", summary)
        self.assertIn("for_cashflow", summary)
        self.assertIn("flags", summary)

    def test_summary_coverage(self):
        receipts = [_receipt("Shopify", 41.83, "2026-06-10")]
        txns = [_txn("Shopify", 41.83, "2026-06-10")]
        recon = rs.reconcile(receipts, txns, as_of=dt.date(2026, 6, 17))
        summary = rs.reconciliation_to_summary(recon)

        self.assertEqual(summary["headline"]["coverage_pct"], 100.0)
        self.assertEqual(summary["headline"]["total_receipts"], 1)


# ─────────────────────────── integration with fee_fraud ──────────────────────

class FeeFraudIntegrationTest(unittest.TestCase):
    """Verify that reconciliation results flow into fee_fraud_scan.scan."""

    def test_discrepancy_appears_in_scan(self):
        from finance_mcp.engines import fee_fraud_scan as ff

        # Build minimal transaction list (needs enough for window_bounds)
        txns = [_txn(f"Merchant{i}", 10.0, f"2026-06-{10+i:02d}", f"TX_{i}")
                for i in range(5)]

        # Build a reconciliation result with a discrepancy
        recon = {
            "discrepancies": [{
                "txn_merchant": "Shopify",
                "receipt_amount": 38.99,
                "txn_amount": 41.83,
                "abs_difference": 2.84,
                "txn_date": "2026-06-10",
                "message": "Shopify charged $41.83 but receipt shows $38.99",
            }],
            "unmatched_charges": [{
                "merchant": "Netflix",
                "amount": 15.99,
                "date": "2026-06-11",
                "message": "Charge from Netflix $15.99 — no receipt on file",
            }],
        }

        s = ff.scan(txns, days=30, reconciliation=recon)
        h = s["headline"]
        d = s["detail"]

        self.assertEqual(h["n_receipt_discrepancies"], 1)
        self.assertAlmostEqual(h["receipt_discrepancy_total"], 2.84)
        self.assertEqual(h["n_unverified_charges"], 1)
        self.assertAlmostEqual(h["unverified_charge_total"], 15.99)
        self.assertTrue(len(d["receipt_discrepancies"]) > 0)
        self.assertTrue(len(d["unverified_charges"]) > 0)

    def test_scan_without_reconciliation(self):
        """scan() works identically to before when no reconciliation passed."""
        from finance_mcp.engines import fee_fraud_scan as ff

        txns = [_txn(f"Merchant{i}", 10.0, f"2026-06-{10+i:02d}", f"TX_{i}")
                for i in range(5)]

        s = ff.scan(txns, days=30)
        h = s["headline"]

        self.assertEqual(h.get("n_receipt_discrepancies", 0), 0)
        self.assertEqual(h.get("n_unverified_charges", 0), 0)


# ─────────────────────────── render ──────────────────────────────────────────

class RenderTest(unittest.TestCase):

    def test_render_reconciliation_summary(self):
        receipts = [_receipt("Shopify", 41.83, "2026-06-10")]
        txns = [_txn("Shopify", 41.83, "2026-06-10")]
        recon = rs.reconcile(receipts, txns, as_of=dt.date(2026, 6, 17))
        summary = rs.reconciliation_to_summary(recon)
        md = rs.render(summary)
        self.assertIn("RECEIPT RECONCILIATION", md)
        self.assertIn("100", md)  # 100% coverage


class GmailQueryTest(unittest.TestCase):

    def test_queries_are_nonempty(self):
        queries = rs.build_gmail_queries(days=7)
        self.assertTrue(len(queries) >= 3)
        for q, desc in queries:
            self.assertIn("newer_than:", q)


if __name__ == "__main__":
    unittest.main(verbosity=2)
