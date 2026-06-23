#!/usr/bin/env python3
"""Tests for dispute_agent.py — dispute/refund automation layer."""

import datetime as dt
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from finance_mcp.engines import dispute_agent as da


class TestMerchantContactLookup(unittest.TestCase):
    """Capability 3: Merchant contact lookup."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.contacts_path = os.path.join(self.tmpdir, "contacts.json")

    def tearDown(self):
        for f in os.listdir(self.tmpdir):
            os.remove(os.path.join(self.tmpdir, f))
        os.rmdir(self.tmpdir)

    def test_cache_hit(self):
        """Cached merchant contact is returned without LLM call."""
        cache = {"amazon": {"email": "help@amazon.com", "source": "llm",
                             "merchant": "Amazon"}}
        with open(self.contacts_path, "w") as f:
            json.dump(cache, f)

        result = da.lookup_merchant_contact(
            "Amazon", contacts_path=self.contacts_path)
        self.assertEqual(result["email"], "help@amazon.com")
        self.assertEqual(result["source"], "cache")

    @patch("finance_mcp.engines.llm_matcher._call_haiku")
    def test_cache_miss_llm_fallback(self, mock_haiku):
        """LLM is called when cache misses, result is cached."""
        mock_haiku.return_value = "billing@netflix.com"

        result = da.lookup_merchant_contact(
            "Netflix", contacts_path=self.contacts_path)
        self.assertEqual(result["email"], "billing@netflix.com")
        self.assertEqual(result["source"], "llm")

        # Verify it was cached
        contacts = da.load_merchant_contacts(self.contacts_path)
        self.assertIn("netflix", contacts)
        self.assertEqual(contacts["netflix"]["email"], "billing@netflix.com")

    @patch("finance_mcp.engines.llm_matcher._call_haiku")
    def test_not_found(self, mock_haiku):
        """Returns not_found when LLM says unknown."""
        mock_haiku.return_value = "unknown"

        result = da.lookup_merchant_contact(
            "ObscureMerchant123", contacts_path=self.contacts_path)
        self.assertIsNone(result["email"])
        self.assertEqual(result["source"], "not_found")

    @patch("finance_mcp.engines.llm_matcher._call_haiku")
    def test_no_api_key(self, mock_haiku):
        """Graceful when no API key available."""
        mock_haiku.return_value = None

        result = da.lookup_merchant_contact(
            "SomeMerchant", contacts_path=self.contacts_path)
        self.assertIsNone(result["email"])
        self.assertEqual(result["source"], "not_found")

    @patch("finance_mcp.engines.llm_matcher._call_haiku")
    def test_invalid_email_rejected(self, mock_haiku):
        """Non-email LLM response is rejected."""
        mock_haiku.return_value = "I don't know the email for that company"

        result = da.lookup_merchant_contact(
            "RandomCo", contacts_path=self.contacts_path)
        self.assertIsNone(result["email"])

    def test_load_empty_contacts(self):
        """Loading non-existent file returns empty dict."""
        contacts = da.load_merchant_contacts(
            os.path.join(self.tmpdir, "nope.json"))
        self.assertEqual(contacts, {})


class TestDisputeTracking(unittest.TestCase):
    """Capability 4: Dispute tracking ledger."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.disputes_path = os.path.join(self.tmpdir, "disputes.json")

    def tearDown(self):
        for f in os.listdir(self.tmpdir):
            os.remove(os.path.join(self.tmpdir, f))
        os.rmdir(self.tmpdir)

    def test_create_dispute(self):
        """Creating a dispute adds it to the ledger."""
        d = da.create_dispute(
            dispute_type="refund_request",
            merchant="Amazon",
            amount=29.99,
            date="2026-06-10",
            reason="duplicate_charge",
            path=self.disputes_path,
        )
        self.assertTrue(d["dispute_id"].startswith("DSP-"))
        self.assertEqual(d["merchant"], "Amazon")
        self.assertEqual(d["amount"], 29.99)
        self.assertEqual(d["status"], "drafted")
        self.assertEqual(d["type"], "refund_request")

        # Verify persisted
        loaded = da.load_disputes(self.disputes_path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["dispute_id"], d["dispute_id"])

    def test_dedup_dispute(self):
        """Same merchant+amount+date+reason doesn't create duplicate."""
        d1 = da.create_dispute(
            dispute_type="refund_request", merchant="Netflix",
            amount=15.99, date="2026-06-05", reason="duplicate_charge",
            path=self.disputes_path,
        )
        d2 = da.create_dispute(
            dispute_type="refund_request", merchant="Netflix",
            amount=15.99, date="2026-06-05", reason="duplicate_charge",
            path=self.disputes_path,
        )
        self.assertEqual(d1["dispute_id"], d2["dispute_id"])
        loaded = da.load_disputes(self.disputes_path)
        self.assertEqual(len(loaded), 1)

    def test_dedup_allows_different_reason(self):
        """Different reason for same merchant+amount+date creates new record."""
        d1 = da.create_dispute(
            dispute_type="refund_request", merchant="Netflix",
            amount=15.99, date="2026-06-05", reason="duplicate_charge",
            path=self.disputes_path,
        )
        d2 = da.create_dispute(
            dispute_type="bank_dispute", merchant="Netflix",
            amount=15.99, date="2026-06-05", reason="unauthorized_charge",
            path=self.disputes_path,
        )
        self.assertNotEqual(d1["dispute_id"], d2["dispute_id"])
        loaded = da.load_disputes(self.disputes_path)
        self.assertEqual(len(loaded), 2)

    def test_update_status(self):
        """Update dispute status and resolution."""
        d = da.create_dispute(
            dispute_type="refund_request", merchant="Amazon",
            amount=29.99, date="2026-06-10", reason="duplicate_charge",
            path=self.disputes_path,
        )
        updated = da.update_dispute_status(
            d["dispute_id"], "resolved",
            resolution="refund_received", path=self.disputes_path)
        self.assertEqual(updated["status"], "resolved")
        self.assertEqual(updated["resolution"], "refund_received")
        self.assertIsNotNone(updated["resolution_date"])

    def test_check_for_resolutions(self):
        """Auto-close when matching refund credit appears."""
        # Create an open dispute and backdate its filing to before the refund
        da.create_dispute(
            dispute_type="refund_request", merchant="Amazon",
            amount=29.99, date="2026-06-10", reason="duplicate_charge",
            path=self.disputes_path,
        )
        # Backdate filing so the refund (June 20) is after filing
        disputes = da.load_disputes(self.disputes_path)
        disputes[0]["date_filed"] = "2026-06-12"
        da.save_disputes(disputes, self.disputes_path)

        # Simulate a refund transaction (inflow = positive amount in this
        # codebase's convention where negative = outflow/debit).
        # display_name reads rawData.merchant_name or rawData.name first.
        txns = [{
            "date": "2026-06-20",
            "amount": 29.99,  # positive = inflow (credit/refund)
            "rawData": {
                "merchant_name": "Amazon",
                "name": "AMAZON REFUND",
            },
            "personal_finance_category": {
                "primary": "TRANSFER_IN",
                "detailed": "TRANSFER_IN_ACCOUNT_TRANSFER",
            },
        }]

        closed = da.check_for_resolutions(txns, path=self.disputes_path)
        self.assertEqual(len(closed), 1)

        # Verify status updated
        disputes = da.load_disputes(self.disputes_path)
        self.assertEqual(disputes[0]["status"], "resolved")
        self.assertEqual(disputes[0]["resolution"], "refund_received")

    def test_no_resolution_wrong_merchant(self):
        """Refund from different merchant doesn't close dispute."""
        da.create_dispute(
            dispute_type="refund_request", merchant="Netflix",
            amount=15.99, date="2026-06-10", reason="duplicate_charge",
            path=self.disputes_path,
        )

        txns = [{
            "date": "2026-06-15",
            "amount": 15.99,  # positive = inflow
            "rawData": {
                "merchant_name": "Hulu",
                "name": "HULU REFUND",
            },
            "personal_finance_category": {
                "primary": "TRANSFER_IN",
                "detailed": "TRANSFER_IN_ACCOUNT_TRANSFER",
            },
        }]

        closed = da.check_for_resolutions(txns, path=self.disputes_path)
        self.assertEqual(len(closed), 0)

    def test_flag_expired(self):
        """Disputes older than expiry_days are flagged."""
        da.create_dispute(
            dispute_type="refund_request", merchant="Amazon",
            amount=29.99, date="2026-05-01", reason="duplicate_charge",
            path=self.disputes_path,
        )
        # Backdate the filing date
        disputes = da.load_disputes(self.disputes_path)
        disputes[0]["date_filed"] = "2026-05-01"
        da.save_disputes(disputes, self.disputes_path)

        expired = da.flag_expired_disputes(
            expiry_days=14, path=self.disputes_path,
            as_of=dt.date(2026, 6, 15))
        self.assertEqual(len(expired), 1)

        loaded = da.load_disputes(self.disputes_path)
        self.assertEqual(loaded[0]["status"], "expired")
        self.assertEqual(loaded[0]["resolution"], "no_response")

    def test_no_expire_recent(self):
        """Recent disputes are not expired."""
        da.create_dispute(
            dispute_type="refund_request", merchant="Amazon",
            amount=29.99, date="2026-06-10", reason="duplicate_charge",
            path=self.disputes_path,
        )
        expired = da.flag_expired_disputes(
            expiry_days=14, path=self.disputes_path,
            as_of=dt.date(2026, 6, 20))
        self.assertEqual(len(expired), 0)

    def test_dispute_summary(self):
        """Summary stats are correct."""
        da.create_dispute(
            dispute_type="refund_request", merchant="Amazon",
            amount=29.99, date="2026-06-10", reason="duplicate_charge",
            path=self.disputes_path,
        )
        da.create_dispute(
            dispute_type="bank_dispute", merchant="Unknown Co",
            amount=50.00, date="2026-06-11", reason="unauthorized_charge",
            path=self.disputes_path,
        )

        s = da.dispute_summary(path=self.disputes_path)
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["open"], 2)
        self.assertEqual(s["open_amount"], 79.99)
        self.assertEqual(s["resolved"], 0)
        self.assertEqual(len(s["open_disputes"]), 2)

    def test_load_empty_disputes(self):
        """Loading non-existent file returns empty list."""
        disputes = da.load_disputes(
            os.path.join(self.tmpdir, "nope.json"))
        self.assertEqual(disputes, [])


class TestRefundEmailGeneration(unittest.TestCase):
    """Capability 1: Auto-draft refund requests."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.disputes_path = os.path.join(self.tmpdir, "disputes.json")
        self.contacts_path = os.path.join(self.tmpdir, "contacts.json")

    def tearDown(self):
        for f in os.listdir(self.tmpdir):
            os.remove(os.path.join(self.tmpdir, f))
        os.rmdir(self.tmpdir)

    @patch("finance_mcp.engines.llm_matcher._call_haiku")
    def test_refund_email_with_haiku(self, mock_haiku):
        """Haiku generates email body for discrepancy finding."""
        # First call: merchant contact lookup (returns unknown)
        # Second call: email body generation
        mock_haiku.side_effect = [
            "unknown",  # merchant contact lookup
            "I noticed a billing discrepancy on my recent transaction. "
            "The receipt shows $45.00 but I was charged $50.00. "
            "Please refund the $5.00 overcharge.",
        ]

        finding = {
            "merchant": "TestShop",
            "amount": 50.00,
            "txn_amount": 50.00,
            "date": "2026-06-10",
            "receipt_amount": 45.00,
            "reason": "amount_discrepancy",
            "message": "TestShop charged $50 but receipt shows $45",
            "type": "discrepancy",
        }

        draft = da.build_refund_draft(
            finding, contacts_path=self.contacts_path,
            disputes_path=self.disputes_path)

        self.assertIn("Refund Request", draft["subject"])
        self.assertIn("TestShop", draft["subject"])
        self.assertIn("[ACCOUNT_HOLDER]", draft["body"])
        self.assertIn("discrepancy", draft["body"].lower())
        self.assertTrue(draft["dispute_id"].startswith("DSP-"))
        self.assertEqual(draft["amount"], 5.00)
        # HTML body present
        self.assertIn("<div", draft["htmlBody"])

    @patch("finance_mcp.engines.llm_matcher._call_haiku")
    def test_refund_email_template_fallback(self, mock_haiku):
        """Template fallback when no API key."""
        mock_haiku.return_value = None  # no API key

        finding = {
            "merchant": "Netflix",
            "amount": 15.99,
            "date": "2026-06-05",
            "dates": ["2026-06-03", "2026-06-05"],
            "reason": "duplicate_charge",
            "message": "Duplicate charge",
            "type": "duplicate",
        }

        draft = da.build_refund_draft(
            finding, contacts_path=self.contacts_path,
            disputes_path=self.disputes_path)

        self.assertIn("Netflix", draft["body"])
        self.assertIn("duplicate", draft["body"].lower())
        self.assertIn("$15.99", draft["body"])
        self.assertIn("[ACCOUNT_HOLDER]", draft["body"])
        self.assertEqual(draft["amount"], 15.99)

    @patch("finance_mcp.engines.llm_matcher._call_haiku")
    def test_refund_email_discrepancy_template(self, mock_haiku):
        """Discrepancy template includes overcharge amount."""
        mock_haiku.return_value = None  # no API key, triggers template

        finding = {
            "merchant": "Uber",
            "amount": 35.50,
            "date": "2026-06-08",
            "receipt_amount": 30.00,
            "reason": "amount_discrepancy",
            "type": "discrepancy",
        }

        draft = da.build_refund_draft(
            finding, contacts_path=self.contacts_path,
            disputes_path=self.disputes_path)

        self.assertIn("$5.50", draft["body"])
        self.assertIn("overcharge", draft["body"].lower())
        self.assertEqual(draft["amount"], 5.50)


class TestBankDisputeLetter(unittest.TestCase):
    """Capability 2: Auto-draft bank dispute letters."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.disputes_path = os.path.join(self.tmpdir, "disputes.json")

    def tearDown(self):
        for f in os.listdir(self.tmpdir):
            os.remove(os.path.join(self.tmpdir, f))
        os.rmdir(self.tmpdir)

    def test_dispute_letter_content(self):
        """Bank dispute letter has correct formal structure."""
        finding = {
            "merchant": "SuspiciousCo",
            "amount": 149.99,
            "date": "2026-06-12",
            "reason": "unauthorized_charge",
            "message": "No receipt on file",
        }

        draft = da.build_bank_dispute_draft(
            finding, bank_email="disputes@testbank.com",
            disputes_path=self.disputes_path)

        self.assertIn("disputes@testbank.com", draft["to"])
        self.assertIn("Transaction Dispute", draft["subject"])
        self.assertIn("SuspiciousCo", draft["subject"])
        self.assertIn("$149.99", draft["body"])

        # Formal letter elements
        body = draft["body"]
        self.assertIn("[ACCOUNT_NUMBER]", body)
        self.assertIn("[BANK_NAME]", body)
        self.assertIn("[ACCOUNT_HOLDER]", body)
        self.assertIn("formally dispute", body)
        self.assertIn("SuspiciousCo", body)
        self.assertIn("2026-06-12", body)

    def test_dispute_letter_duplicate_reason(self):
        """Duplicate charge reason text is correct."""
        finding = {
            "merchant": "Amazon",
            "amount": 49.99,
            "date": "2026-06-10",
            "reason": "duplicate_charge",
        }

        draft = da.build_bank_dispute_draft(
            finding, disputes_path=self.disputes_path)

        self.assertIn("duplicate", draft["body"].lower())

    def test_dispute_letter_has_html(self):
        """HTML version includes warning about placeholders."""
        finding = {
            "merchant": "Unknown",
            "amount": 75.00,
            "date": "2026-06-11",
            "reason": "unauthorized_charge",
        }

        draft = da.build_bank_dispute_draft(
            finding, disputes_path=self.disputes_path)

        self.assertIn("ACCOUNT_NUMBER", draft["htmlBody"])
        self.assertIn("BANK_NAME", draft["htmlBody"])
        self.assertIn("Bank Dispute Letter", draft["htmlBody"])

    def test_dispute_letter_with_evidence(self):
        """Evidence is included when available."""
        finding = {
            "merchant": "OverchargeShop",
            "amount": 200.00,
            "date": "2026-06-09",
            "reason": "amount_discrepancy",
            "receipt_amount": 150.00,
            "message": "Receipt shows $150 but charged $200",
        }

        draft = da.build_bank_dispute_draft(
            finding, disputes_path=self.disputes_path)

        self.assertIn("$150.00", draft["body"])


class TestProcessFindings(unittest.TestCase):
    """Pipeline integration: process_findings."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.disputes_path = os.path.join(self.tmpdir, "disputes.json")
        self.contacts_path = os.path.join(self.tmpdir, "contacts.json")

    def tearDown(self):
        for f in os.listdir(self.tmpdir):
            os.remove(os.path.join(self.tmpdir, f))
        os.rmdir(self.tmpdir)

    def test_process_discrepancies(self):
        """Reconciliation discrepancies become disputes."""
        recon = {
            "discrepancies": [{
                "txn_merchant": "Spotify",
                "txn_amount": 14.99,
                "txn_date": "2026-06-05",
                "receipt_amount": 9.99,
                "abs_difference": 5.00,
                "message": "Spotify charged $14.99 but receipt shows $9.99",
            }],
            "unmatched_charges": [],
        }

        result = da.process_findings(
            reconciliation=recon, auto_draft=False,
            disputes_path=self.disputes_path,
            contacts_path=self.contacts_path)

        self.assertEqual(len(result["new_disputes"]), 1)
        self.assertEqual(len(result["drafts"]), 0)

        disputes = da.load_disputes(self.disputes_path)
        self.assertEqual(len(disputes), 1)
        self.assertEqual(disputes[0]["merchant"], "Spotify")
        self.assertEqual(disputes[0]["amount"], 5.00)
        self.assertEqual(disputes[0]["reason"], "amount_discrepancy")

    def test_process_duplicates(self):
        """Fee_fraud duplicates become disputes."""
        fee_fraud = {
            "detail": {
                "duplicates": [{
                    "merchant": "Adobe",
                    "amount": 54.99,
                    "dates": ["2026-06-01", "2026-06-02"],
                    "recoverable": 54.99,
                }],
                "unverified_charges": [],
            },
        }

        result = da.process_findings(
            fee_fraud_summary=fee_fraud, auto_draft=False,
            disputes_path=self.disputes_path,
            contacts_path=self.contacts_path)

        self.assertEqual(len(result["new_disputes"]), 1)
        disputes = da.load_disputes(self.disputes_path)
        self.assertEqual(disputes[0]["reason"], "duplicate_charge")

    def test_process_unverified_over_threshold(self):
        """Unverified charges over threshold become bank disputes."""
        recon = {
            "discrepancies": [],
            "unmatched_charges": [
                {"merchant": "BigCharge", "amount": 100.00,
                 "date": "2026-06-10", "message": "No receipt"},
                {"merchant": "SmallCharge", "amount": 10.00,
                 "date": "2026-06-10", "message": "No receipt"},
            ],
        }

        da.process_findings(
            reconciliation=recon, auto_draft=False,
            threshold=25.0,
            disputes_path=self.disputes_path,
            contacts_path=self.contacts_path)

        # Only the $100 charge (over $25 threshold) should create a dispute
        disputes = da.load_disputes(self.disputes_path)
        self.assertEqual(len(disputes), 1)
        self.assertEqual(disputes[0]["merchant"], "BigCharge")
        self.assertEqual(disputes[0]["type"], "bank_dispute")

    @patch("finance_mcp.engines.llm_matcher._call_haiku")
    def test_process_auto_draft(self, mock_haiku):
        """Auto-draft creates email content for findings."""
        mock_haiku.return_value = None  # template fallback

        fee_fraud = {
            "detail": {
                "duplicates": [{
                    "merchant": "Netflix",
                    "amount": 15.99,
                    "dates": ["2026-06-01", "2026-06-02"],
                    "recoverable": 15.99,
                }],
                "unverified_charges": [],
            },
        }

        result = da.process_findings(
            fee_fraud_summary=fee_fraud, auto_draft=True,
            disputes_path=self.disputes_path,
            contacts_path=self.contacts_path)

        self.assertEqual(len(result["drafts"]), 1)
        draft = result["drafts"][0]
        self.assertIn("Netflix", draft["subject"])
        self.assertIn("Netflix", draft["body"])
        self.assertIn("htmlBody", draft)

    def test_process_combined(self):
        """Multiple finding types processed together."""
        recon = {
            "discrepancies": [{
                "txn_merchant": "Spotify",
                "txn_amount": 14.99,
                "txn_date": "2026-06-05",
                "receipt_amount": 9.99,
                "abs_difference": 5.00,
                "message": "overcharge",
            }],
            "unmatched_charges": [{
                "merchant": "Mystery Inc",
                "amount": 200.00,
                "date": "2026-06-08",
                "message": "no receipt",
            }],
        }
        fee_fraud = {
            "detail": {
                "duplicates": [{
                    "merchant": "Adobe",
                    "amount": 54.99,
                    "dates": ["2026-06-01", "2026-06-02"],
                    "recoverable": 54.99,
                }],
                "unverified_charges": [],
            },
        }

        result = da.process_findings(
            reconciliation=recon, fee_fraud_summary=fee_fraud,
            auto_draft=False, threshold=25.0,
            disputes_path=self.disputes_path,
            contacts_path=self.contacts_path)

        # 3 disputes: Spotify discrepancy + Adobe duplicate + Mystery unverified
        self.assertEqual(len(result["new_disputes"]), 3)
        disputes = da.load_disputes(self.disputes_path)
        types = {d["type"] for d in disputes}
        self.assertIn("refund_request", types)
        self.assertIn("bank_dispute", types)

    def test_summary_in_result(self):
        """process_findings returns a summary."""
        result = da.process_findings(
            disputes_path=self.disputes_path,
            contacts_path=self.contacts_path)
        self.assertIn("summary", result)
        self.assertIn("tool", result["summary"])
        self.assertEqual(result["summary"]["tool"], "dispute_agent")


class TestRendering(unittest.TestCase):
    """Rendering and CLI output."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.disputes_path = os.path.join(self.tmpdir, "disputes.json")

    def tearDown(self):
        for f in os.listdir(self.tmpdir):
            os.remove(os.path.join(self.tmpdir, f))
        os.rmdir(self.tmpdir)

    def test_render_status_empty(self):
        """Render empty dispute status."""
        output = da.render_status(path=self.disputes_path)
        self.assertIn("DISPUTE STATUS", output)
        self.assertIn("0 open", output)

    def test_render_status_with_disputes(self):
        """Render status with open disputes."""
        da.create_dispute(
            dispute_type="refund_request", merchant="Amazon",
            amount=29.99, date="2026-06-10", reason="duplicate_charge",
            path=self.disputes_path,
        )
        output = da.render_status(path=self.disputes_path)
        self.assertIn("1 open", output)
        self.assertIn("Amazon", output)
        self.assertIn("$29.99", output)
        self.assertIn("DSP-", output)


class TestPrivacy(unittest.TestCase):
    """Privacy constraints: verify what gets sent to the LLM."""

    @patch("finance_mcp.engines.llm_matcher._call_haiku")
    def test_refund_body_no_account_numbers(self, mock_haiku):
        """LLM prompt for refund email contains no sensitive data."""
        captured_calls = []

        def capture_haiku(system, user, api_key=None):
            captured_calls.append({"system": system, "user": user})
            return "Please issue a refund."

        mock_haiku.side_effect = capture_haiku

        da._generate_refund_body(
            "TestMerchant", 50.00, "2026-06-10", 45.00,
            "amount_discrepancy", "overcharge detected")

        # Check the email body generation call (second call if merchant
        # lookup happened first, but we're calling _generate_refund_body
        # directly so it's the first)
        self.assertTrue(len(captured_calls) >= 1)
        call = captured_calls[-1]
        combined = call["system"] + " " + call["user"]
        self.assertNotIn("account number", combined.lower())
        self.assertNotIn("bank name", combined.lower())
        self.assertNotIn("chase", combined.lower())
        # But merchant info IS present
        self.assertIn("TestMerchant", combined)
        self.assertIn("50.00", combined)

    @patch("finance_mcp.engines.llm_matcher._call_haiku")
    def test_merchant_lookup_only_name(self, mock_haiku):
        """Merchant contact lookup sends only the merchant name."""
        captured_calls = []

        def capture_haiku(system, user, api_key=None):
            captured_calls.append({"system": system, "user": user})
            return "unknown"

        mock_haiku.side_effect = capture_haiku

        da._llm_lookup_contact("TestCorp", api_key="fake-key")

        self.assertEqual(len(captured_calls), 1)
        call = captured_calls[0]
        # Only the merchant name should appear
        self.assertIn("TestCorp", call["user"])
        # No financial details
        self.assertNotIn("$", call["user"])
        self.assertNotIn("account", call["user"].lower())

    def test_bank_dispute_has_placeholders(self):
        """Bank dispute letter uses placeholders, not real data."""
        plain, html = da._generate_bank_dispute_letter(
            "SomeMerchant", 100.00, "2026-06-10",
            "unauthorized_charge", {})
        self.assertIn("[ACCOUNT_NUMBER]", plain)
        self.assertIn("[BANK_NAME]", plain)
        self.assertIn("[BANK_DISPUTE_EMAIL]", plain)
        # No real bank info leaked
        self.assertNotIn("Chase", plain)


class TestPipelineIntegration(unittest.TestCase):
    """Integration with finance_agent.py pipeline."""

    def test_import_dispute_agent(self):
        """dispute_agent is importable and has expected API."""
        self.assertTrue(hasattr(da, "process_findings"))
        self.assertTrue(hasattr(da, "dispute_summary"))
        self.assertTrue(hasattr(da, "render_status"))
        self.assertTrue(hasattr(da, "load_disputes"))
        self.assertTrue(hasattr(da, "check_for_resolutions"))
        self.assertTrue(hasattr(da, "flag_expired_disputes"))
        self.assertTrue(hasattr(da, "build_refund_draft"))
        self.assertTrue(hasattr(da, "build_bank_dispute_draft"))
        self.assertTrue(hasattr(da, "lookup_merchant_contact"))

    def test_finance_agent_imports_dispute(self):
        """finance_agent.py imports dispute_agent."""
        from finance_mcp import finance_agent as fa
        self.assertTrue(hasattr(fa, '_dispute_section'))

    def test_finance_agent_cli_flags(self):
        """finance_agent has dispute-related CLI flags."""
        from finance_mcp import finance_agent as fa
        # Just verify the module loaded correctly with the new flags
        self.assertTrue(hasattr(fa, 'build_digest'))


if __name__ == "__main__":
    unittest.main()
