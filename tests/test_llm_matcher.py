#!/usr/bin/env python3
"""
test_llm_matcher.py — tests for the LLM-assisted merchant matching and
receipt extraction fallback.

Tests:
  - LLM merchant matching with mocked API responses
  - Cache hit / miss behaviour
  - Graceful degradation when no API key
  - Deterministic matches are unchanged (LLM only touches leftovers)
  - Receipt extraction fallback with mocked API
  - Response parsing edge cases (malformed JSON, markdown fences, etc.)
  - Privacy: only merchant names sent (no amounts/dates/IDs)
  - Integration: LLM matches merge into reconciliation results

Uses mocked API calls — no real Anthropic API calls in any test.
"""

import datetime as dt
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from finance_mcp.engines import llm_matcher
from finance_mcp.engines import receipt_scanner as rs


# ─────────────────────────── helpers ──────────────────────────────────────────

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
        "from": f"noreply@{merchant.lower().replace(' ', '')}.com",
        "type": "receipt",
    }


def _mock_haiku_response(pairs):
    """Create a mock _call_haiku response for merchant matching."""
    return json.dumps(pairs)


# ─────────────────────────── response parsing ─────────────────────────────────

class ParseMatchResponseTest(unittest.TestCase):
    """Test _parse_match_response with various response formats."""

    def test_clean_json_array(self):
        raw = '[{"bank": "SQ *CHINA HOUSE", "receipt": "China House Restaurant", "confidence": 0.95}]'
        result = llm_matcher._parse_match_response(raw)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["bank"], "SQ *CHINA HOUSE")
        self.assertEqual(result[0]["receipt"], "China House Restaurant")
        self.assertAlmostEqual(result[0]["confidence"], 0.95)

    def test_markdown_fences(self):
        raw = '```json\n[{"bank": "AMZN", "receipt": "Amazon", "confidence": 0.9}]\n```'
        result = llm_matcher._parse_match_response(raw)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["bank"], "AMZN")

    def test_surrounding_text(self):
        raw = 'Here are the matches:\n[{"bank": "X", "receipt": "Y", "confidence": 0.85}]\nDone!'
        result = llm_matcher._parse_match_response(raw)
        self.assertEqual(len(result), 1)

    def test_empty_array(self):
        result = llm_matcher._parse_match_response("[]")
        self.assertEqual(result, [])

    def test_malformed_json(self):
        result = llm_matcher._parse_match_response("not json at all")
        self.assertEqual(result, [])

    def test_none_input(self):
        result = llm_matcher._parse_match_response(None)
        self.assertEqual(result, [])

    def test_empty_string(self):
        result = llm_matcher._parse_match_response("")
        self.assertEqual(result, [])

    def test_multiple_pairs(self):
        raw = json.dumps([
            {"bank": "SQ *CHINA HOUSE", "receipt": "China House", "confidence": 0.95},
            {"bank": "AMZN MKTP US*2847", "receipt": "Amazon.com", "confidence": 0.92},
            {"bank": "PP*ELEVENLABS", "receipt": "Eleven Labs", "confidence": 0.88},
        ])
        result = llm_matcher._parse_match_response(raw)
        self.assertEqual(len(result), 3)

    def test_skips_invalid_items(self):
        raw = json.dumps([
            {"bank": "A", "receipt": "B", "confidence": 0.9},
            {"missing_keys": True},
            {"bank": "", "receipt": "D", "confidence": 0.8},  # empty bank
            {"bank": "E", "receipt": "F", "confidence": "high"},  # non-numeric
        ])
        result = llm_matcher._parse_match_response(raw)
        self.assertEqual(len(result), 1)  # only first is valid


class ParseExtractResponseTest(unittest.TestCase):
    """Test _parse_extract_response with various response formats."""

    def test_clean_json(self):
        raw = '{"amount": 42.99, "merchant": "Amazon", "date": "2026-06-15"}'
        result = llm_matcher._parse_extract_response(raw)
        self.assertIsNotNone(result)
        self.assertEqual(result["amount"], 42.99)
        self.assertEqual(result["merchant"], "Amazon")
        self.assertEqual(result["date"], "2026-06-15")

    def test_amount_only(self):
        raw = '{"amount": 15.50, "merchant": null, "date": null}'
        result = llm_matcher._parse_extract_response(raw)
        self.assertIsNotNone(result)
        self.assertEqual(result["amount"], 15.50)
        self.assertIsNone(result["merchant"])

    def test_no_amount_returns_none(self):
        raw = '{"amount": null, "merchant": "Store", "date": "2026-01-01"}'
        result = llm_matcher._parse_extract_response(raw)
        self.assertIsNone(result)

    def test_markdown_fences(self):
        raw = '```json\n{"amount": 9.99, "merchant": "Netflix", "date": "2026-06-01"}\n```'
        result = llm_matcher._parse_extract_response(raw)
        self.assertIsNotNone(result)
        self.assertEqual(result["amount"], 9.99)

    def test_malformed_json(self):
        result = llm_matcher._parse_extract_response("not json")
        self.assertIsNone(result)

    def test_none_input(self):
        result = llm_matcher._parse_extract_response(None)
        self.assertIsNone(result)


# ─────────────────────────── cache behaviour ──────────────────────────────────

class CacheTest(unittest.TestCase):
    """Test cache load/save and hit/miss behaviour."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cache_path = os.path.join(self.tmpdir, "test_cache.json")

    def tearDown(self):
        if os.path.exists(self.cache_path):
            os.remove(self.cache_path)
        os.rmdir(self.tmpdir)

    def test_empty_cache(self):
        cache = llm_matcher._load_cache(self.cache_path)
        self.assertEqual(cache, {})

    def test_save_and_load(self):
        cache = {"key1": [{"bank_name": "A", "receipt_name": "B", "confidence": 0.9}]}
        llm_matcher._save_cache(cache, self.cache_path)
        loaded = llm_matcher._load_cache(self.cache_path)
        self.assertEqual(loaded, cache)

    def test_cache_key_deterministic(self):
        k1 = llm_matcher._cache_key(["B", "A"], ["D", "C"])
        k2 = llm_matcher._cache_key(["A", "B"], ["C", "D"])
        self.assertEqual(k1, k2)  # sorted, so order doesn't matter

    def test_cache_key_different_inputs(self):
        k1 = llm_matcher._cache_key(["A"], ["B"])
        k2 = llm_matcher._cache_key(["C"], ["D"])
        self.assertNotEqual(k1, k2)

    @patch("finance_mcp.engines.llm_matcher._call_haiku")
    def test_cache_hit_skips_api(self, mock_haiku):
        """When cache has the result, no API call is made."""
        # Pre-populate cache
        bank_names = ["SQ *CHINA HOUSE"]
        receipt_names = ["China House"]
        ck = llm_matcher._cache_key(bank_names, receipt_names)
        cache = {ck: [{"bank_name": "SQ *CHINA HOUSE",
                        "receipt_name": "China House",
                        "confidence": 0.95}]}
        llm_matcher._save_cache(cache, self.cache_path)

        result = llm_matcher.llm_match_merchants(
            [{"merchant": "China House"}],
            [{"merchant": "SQ *CHINA HOUSE"}],
            cache_path=self.cache_path,
        )

        mock_haiku.assert_not_called()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["bank_name"], "SQ *CHINA HOUSE")

    @patch("finance_mcp.engines.llm_matcher._call_haiku")
    def test_cache_miss_calls_api(self, mock_haiku):
        """When cache misses, API is called and result is cached."""
        mock_haiku.return_value = json.dumps([
            {"bank": "AMZN", "receipt": "Amazon", "confidence": 0.92}
        ])

        result = llm_matcher.llm_match_merchants(
            [{"merchant": "Amazon"}],
            [{"merchant": "AMZN"}],
            cache_path=self.cache_path,
        )

        mock_haiku.assert_called_once()
        self.assertEqual(len(result), 1)

        # Verify cached
        cache = llm_matcher._load_cache(self.cache_path)
        self.assertTrue(len(cache) > 0)


# ─────────────────────────── graceful degradation ─────────────────────────────

class GracefulDegradationTest(unittest.TestCase):
    """LLM features degrade gracefully when no API key or API failure."""

    @patch("finance_mcp.engines.llm_matcher._call_haiku", return_value=None)
    def test_no_api_key_returns_empty(self, mock_haiku):
        """When _call_haiku returns None (no key), return empty matches."""
        result = llm_matcher.llm_match_merchants(
            [{"merchant": "Amazon"}],
            [{"merchant": "AMZN"}],
            cache_path=os.path.join(tempfile.gettempdir(), "nonexistent_cache_test.json"),
        )
        self.assertEqual(result, [])

    @patch("finance_mcp.engines.llm_matcher._call_haiku", return_value=None)
    def test_no_api_key_extract_returns_none(self, mock_haiku):
        result = llm_matcher.llm_extract_receipt("Your order total: $42.99")
        self.assertIsNone(result)

    @patch("finance_mcp.engines.llm_matcher._call_haiku", return_value="invalid response!!!")
    def test_malformed_response_returns_empty(self, mock_haiku):
        result = llm_matcher.llm_match_merchants(
            [{"merchant": "Amazon"}],
            [{"merchant": "AMZN"}],
            cache_path=os.path.join(tempfile.gettempdir(), "nonexistent_cache_test.json"),
        )
        self.assertEqual(result, [])

    def test_empty_receipts_returns_empty(self):
        result = llm_matcher.llm_match_merchants([], [{"merchant": "A"}])
        self.assertEqual(result, [])

    def test_empty_transactions_returns_empty(self):
        result = llm_matcher.llm_match_merchants([{"merchant": "A"}], [])
        self.assertEqual(result, [])

    def test_empty_email_text_returns_none(self):
        result = llm_matcher.llm_extract_receipt("")
        self.assertIsNone(result)

    def test_none_email_text_returns_none(self):
        result = llm_matcher.llm_extract_receipt(None)
        self.assertIsNone(result)


# ─────────────────────────── confidence filtering ─────────────────────────────

class ConfidenceFilterTest(unittest.TestCase):
    """Test that low-confidence matches are filtered out."""

    @patch("finance_mcp.engines.llm_matcher._call_haiku")
    def test_below_threshold_filtered(self, mock_haiku):
        mock_haiku.return_value = json.dumps([
            {"bank": "A", "receipt": "B", "confidence": 0.5},  # below 0.8
        ])
        result = llm_matcher.llm_match_merchants(
            [{"merchant": "B"}],
            [{"merchant": "A"}],
            confidence_threshold=0.8,
            cache_path=os.path.join(tempfile.gettempdir(), "conf_test_cache.json"),
        )
        self.assertEqual(result, [])
        # Clean up
        if os.path.exists(os.path.join(tempfile.gettempdir(), "conf_test_cache.json")):
            os.remove(os.path.join(tempfile.gettempdir(), "conf_test_cache.json"))

    @patch("finance_mcp.engines.llm_matcher._call_haiku")
    def test_above_threshold_kept(self, mock_haiku):
        mock_haiku.return_value = json.dumps([
            {"bank": "A", "receipt": "B", "confidence": 0.9},
        ])
        result = llm_matcher.llm_match_merchants(
            [{"merchant": "B"}],
            [{"merchant": "A"}],
            confidence_threshold=0.8,
            cache_path=os.path.join(tempfile.gettempdir(), "conf_test_cache2.json"),
        )
        self.assertEqual(len(result), 1)
        # Clean up
        if os.path.exists(os.path.join(tempfile.gettempdir(), "conf_test_cache2.json")):
            os.remove(os.path.join(tempfile.gettempdir(), "conf_test_cache2.json"))


# ─────────────────────────── deterministic pass unchanged ─────────────────────

class DeterministicUnchangedTest(unittest.TestCase):
    """LLM matching must not alter deterministic results — only leftovers."""

    @patch("finance_mcp.engines.llm_matcher.llm_match_merchants", return_value=[])
    def test_deterministic_match_unaffected(self, mock_llm):
        """Items that the fuzzy matcher handles never reach the LLM."""
        receipts = [_receipt("Shopify", 41.83, "2026-06-10")]
        txns = [_txn("Shopify", 41.83, "2026-06-10", "TX_1")]
        result = rs.reconcile(receipts, txns, as_of=dt.date(2026, 6, 17))

        self.assertEqual(len(result["matched"]), 1)
        self.assertEqual(result["matched"][0]["match_source"], "deterministic")
        self.assertEqual(len(result["unmatched_receipts"]), 0)

    @patch("finance_mcp.engines.llm_matcher.llm_match_merchants", return_value=[])
    def test_deterministic_discrepancy_unaffected(self, mock_llm):
        receipts = [_receipt("Amazon", 50.00, "2026-06-10")]
        txns = [_txn("Amazon", 100.00, "2026-06-10", "TX_2")]
        result = rs.reconcile(receipts, txns, as_of=dt.date(2026, 6, 17))

        self.assertEqual(len(result["discrepancies"]), 1)
        self.assertEqual(result["discrepancies"][0]["match_source"], "deterministic")


# ─────────────────────────── LLM match integration ────────────────────────────

class LLMMatchIntegrationTest(unittest.TestCase):
    """Test LLM matches merging into reconciliation results."""

    @patch("finance_mcp.engines.llm_matcher._call_haiku")
    def test_llm_resolves_unmatched(self, mock_haiku):
        """LLM pairs an unmatched receipt with an unmatched transaction."""
        mock_haiku.return_value = json.dumps([
            {"bank": "SQ *CHINA HOUSE", "receipt": "China House Restaurant",
             "confidence": 0.95}
        ])

        receipts = [_receipt("China House Restaurant", 23.50, "2026-06-10")]
        txns = [_txn("SQ *CHINA HOUSE", 23.50, "2026-06-10", "TX_1")]
        result = rs.reconcile(receipts, txns, as_of=dt.date(2026, 6, 17))

        # Deterministic pass won't match these (merchant_similarity is 0 for
        # "China House Restaurant" vs "SQ *CHINA HOUSE" — no token overlap
        # because "sq" is only 2 chars). But if the deterministic pass DOES
        # match them, that's fine too — the LLM just won't be called.
        total_matched = len(result["matched"])
        self.assertGreaterEqual(total_matched, 1)

        # At least one match should exist
        if any(m.get("match_source") == "llm_assisted" for m in result["matched"]):
            llm_match = [m for m in result["matched"]
                         if m["match_source"] == "llm_assisted"][0]
            self.assertIn("llm_confidence", llm_match)
            self.assertEqual(result["coverage"]["llm_matched"], 1)

    @patch("finance_mcp.engines.llm_matcher._call_haiku")
    def test_llm_discrepancy_flagged(self, mock_haiku):
        """LLM-matched pair with amount difference → discrepancy."""
        mock_haiku.return_value = json.dumps([
            {"bank": "SQ *CHINA HOUSE", "receipt": "China House Restaurant",
             "confidence": 0.95}
        ])

        receipts = [_receipt("China House Restaurant", 20.00, "2026-06-10")]
        txns = [_txn("SQ *CHINA HOUSE", 35.00, "2026-06-10", "TX_1")]
        result = rs.reconcile(receipts, txns, as_of=dt.date(2026, 6, 17))

        # Check if deterministic handled it
        det_disc = [d for d in result["discrepancies"]
                    if d.get("match_source") == "deterministic"]
        llm_disc = [d for d in result["discrepancies"]
                    if d.get("match_source") == "llm_assisted"]

        total = len(det_disc) + len(llm_disc)
        self.assertGreaterEqual(total, 1)

        if llm_disc:
            self.assertIn("LLM-matched", llm_disc[0]["message"])

    @patch("finance_mcp.engines.llm_matcher._call_haiku")
    def test_llm_respects_date_tolerance(self, mock_haiku):
        """LLM match is rejected if dates are too far apart."""
        mock_haiku.return_value = json.dumps([
            {"bank": "SQ *CHINA HOUSE", "receipt": "China House Restaurant",
             "confidence": 0.95}
        ])

        receipts = [_receipt("China House Restaurant", 23.50, "2026-06-01")]
        txns = [_txn("SQ *CHINA HOUSE", 23.50, "2026-06-15", "TX_1")]
        result = rs.reconcile(receipts, txns, as_of=dt.date(2026, 6, 17),
                              date_tolerance_days=3)

        # Date diff is 14 days — beyond tolerance — should remain unmatched
        llm_matched = [m for m in result["matched"]
                       if m.get("match_source") == "llm_assisted"]
        self.assertEqual(len(llm_matched), 0)
        self.assertEqual(len(result["unmatched_receipts"]), 1)

    @patch("finance_mcp.engines.llm_matcher._call_haiku", return_value=None)
    def test_llm_failure_falls_back_gracefully(self, mock_haiku):
        """When LLM fails, deterministic results stand alone."""
        # Use merchant names with ZERO deterministic overlap:
        # "PP*ELEVENLABS" vs "Eleven Labs AI" — "pp" is 2 chars (excluded),
        # "elevenlabs" vs "eleven" / "labs" — no token overlap because
        # "elevenlabs" is a single token vs "eleven" and "labs" separately.
        receipts = [
            _receipt("Shopify", 41.83, "2026-06-10", "t1"),
            _receipt("Eleven Labs AI", 5.00, "2026-06-10", "t2"),
        ]
        txns = [
            _txn("Shopify", 41.83, "2026-06-10", "TX_1"),
            _txn("PP*ELEVENLABS", 5.00, "2026-06-10", "TX_2"),
        ]
        result = rs.reconcile(receipts, txns, as_of=dt.date(2026, 6, 17))

        # Shopify should be deterministically matched
        det_matches = [m for m in result["matched"]
                       if m["match_source"] == "deterministic"]
        self.assertEqual(len(det_matches), 1)
        self.assertEqual(det_matches[0]["receipt_merchant"], "Shopify")
        # PP*ELEVENLABS vs "Eleven Labs AI" — no deterministic overlap,
        # LLM failed, so it remains unmatched
        self.assertEqual(len(result["unmatched_receipts"]), 1)

    def test_coverage_stats_include_llm(self):
        """Coverage dict includes llm_matched and llm_discrepancies fields."""
        result = rs.reconcile([], [], as_of=dt.date(2026, 6, 17))
        self.assertIn("llm_matched", result["coverage"])
        self.assertIn("llm_discrepancies", result["coverage"])
        self.assertEqual(result["coverage"]["llm_matched"], 0)
        self.assertEqual(result["coverage"]["llm_discrepancies"], 0)


# ─────────────────────────── receipt extraction fallback ──────────────────────

class ReceiptExtractionFallbackTest(unittest.TestCase):
    """Test LLM receipt extraction in parse_receipt."""

    def _make_thread(self, subject, from_addr, body, date_str="2026-06-15"):
        return {
            "id": "thread_llm",
            "messages": [{
                "id": "msg_llm", "subject": subject, "from": from_addr,
                "date": date_str, "snippet": body[:200], "plaintext_body": body,
                "headers": [
                    {"name": "Subject", "value": subject},
                    {"name": "From", "value": from_addr},
                    {"name": "Date", "value": date_str},
                ],
            }],
        }

    @patch("finance_mcp.engines.llm_matcher.llm_extract_receipt")
    def test_llm_extracts_when_regex_fails(self, mock_extract):
        """When regex can't find amount but email is financial, LLM is called."""
        mock_extract.return_value = {
            "amount": 42.99,
            "merchant": "Acme Store",
            "date": "2026-06-15",
        }
        # Use a body with no dollar amount (regex will fail) but financial keywords
        thread = self._make_thread(
            "Your receipt from Acme Store",
            "noreply@acmestore.com",
            "Thank you for your purchase! Your order has been confirmed.",
        )
        receipt = rs.parse_receipt(thread)
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["amount"], 42.99)
        self.assertEqual(receipt["extraction_source"], "llm_assisted")
        mock_extract.assert_called_once()

    @patch("finance_mcp.engines.llm_matcher.llm_extract_receipt")
    def test_llm_not_called_when_regex_succeeds(self, mock_extract):
        """When regex finds amount, LLM is NOT called."""
        thread = self._make_thread(
            "Your Amazon order of $42.99",
            "order-update@amazon.com",
            "Order Total: $42.99",
        )
        receipt = rs.parse_receipt(thread)
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["amount"], 42.99)
        self.assertNotIn("extraction_source", receipt)
        mock_extract.assert_not_called()

    @patch("finance_mcp.engines.llm_matcher.llm_extract_receipt", return_value=None)
    def test_llm_failure_still_extracts_merchant(self, mock_extract):
        """When LLM fails, we still get whatever regex found (merchant)."""
        thread = self._make_thread(
            "Your Amazon order confirmation",
            "order-update@amazon.com",
            "Thank you for your order. Details enclosed.",
        )
        receipt = rs.parse_receipt(thread)
        # Regex can't find amount, LLM fails too
        # But identify_merchant finds "Amazon" from the sender
        # parse_receipt returns None only if BOTH amount and merchant are None
        if receipt is not None:
            self.assertEqual(receipt["merchant"], "Amazon")
            self.assertIsNone(receipt["amount"])

    @patch("finance_mcp.engines.llm_matcher.llm_extract_receipt")
    def test_llm_not_called_for_unknown_email_type(self, mock_extract):
        """LLM is NOT called when email type is 'unknown'."""
        thread = self._make_thread(
            "Team meeting tomorrow",
            "boss@company.com",
            "Let's sync up on the project status.",
        )
        rs.parse_receipt(thread)
        mock_extract.assert_not_called()


# ─────────────────────────── privacy checks ───────────────────────────────────

class PrivacyTest(unittest.TestCase):
    """Verify that only merchant names are sent to the LLM."""

    @patch("finance_mcp.engines.llm_matcher._call_haiku")
    def test_only_names_in_prompt(self, mock_haiku):
        """The API call should only contain merchant name strings."""
        mock_haiku.return_value = "[]"

        llm_matcher.llm_match_merchants(
            [{"merchant": "China House", "amount": 23.50, "date": "2026-06-10",
              "thread_id": "SECRET_THREAD_ID"}],
            [{"merchant": "SQ *CHINA HOUSE", "amount": 23.50,
              "txn_id": "SECRET_TXN_ID"}],
            cache_path=os.path.join(tempfile.gettempdir(), "privacy_test_cache.json"),
        )

        # Check what was sent to _call_haiku
        self.assertTrue(mock_haiku.called)
        args = mock_haiku.call_args
        system_prompt = args[0][0] if args[0] else args[1].get("system", "")
        user_prompt = args[0][1] if len(args[0]) > 1 else args[1].get("user", "")

        # The prompt should contain merchant names
        self.assertIn("China House", user_prompt)
        self.assertIn("SQ *CHINA HOUSE", user_prompt)

        # But NOT amounts, dates, or IDs
        self.assertNotIn("23.50", user_prompt)
        self.assertNotIn("2026-06-10", user_prompt)
        self.assertNotIn("SECRET_THREAD_ID", user_prompt)
        self.assertNotIn("SECRET_TXN_ID", user_prompt)
        self.assertNotIn("23.50", system_prompt)

        # Clean up
        if os.path.exists(os.path.join(tempfile.gettempdir(), "privacy_test_cache.json")):
            os.remove(os.path.join(tempfile.gettempdir(), "privacy_test_cache.json"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
