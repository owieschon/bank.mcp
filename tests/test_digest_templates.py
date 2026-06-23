#!/usr/bin/env python3
"""
test_digest_templates.py — unit tests for digest_templates.py template engine.

Tests:
  - select_hero() priority logic across all severity levels
  - render_weekly_html() with full and receipts-only digests
  - render_monthly_html() with monthly-specific sections
  - render_email_html() email-only output
  - _format_date_range() same-month and cross-month formatting

Run: python3 -m pytest test_digest_templates.py -v
  or: python3 -m unittest test_digest_templates -v
"""

import unittest

from finance_mcp.report.digest_templates import (
    select_hero,
    render_weekly_html,
    render_monthly_html,
    render_email_html,
    _format_date_range,
)


# ─────────────────────────── mock digest builder ─────────────────────────────

def _mock_digest(mode="weekly", overdraft=True, behind=True, fees=22.50):
    return {
        "tool": "finance_agent",
        "as_of": "2026-06-15",
        "mode": mode,
        "window": {"start": "2026-06-09", "end": "2026-06-15"},
        "sections": {
            "forecast": {
                "tool": "cashflow_forecaster",
                "available": True,
                "headline": {
                    "start_balance": 2847.50,
                    "buffer": 100.0,
                    "horizon_days": 35,
                    "projected_end_balance": -2985.33,
                    "min_balance": -2985.33,
                    "min_date": "2026-07-20",
                    "overdraft": overdraft,
                    "low_balance": overdraft,
                    "safe_by": "2026-06-30" if overdraft else None,
                    "daily_burn": 2.31,
                    "next_income": None,
                },
                "detail": {
                    "overdraft_days": [],
                    "low_days": [],
                    "biggest_obligations": [
                        {"date": "2026-07-01", "merchant": "Landlord ACH", "amount": 1500.00},
                        {"date": "2026-07-01", "merchant": "ACME Corp", "amount": 4200.00},
                    ],
                },
                "flags": ["OVERDRAFT projected..."] if overdraft else [],
            },
            "budget": {
                "tool": "budget_scorer",
                "available": True,
                "mode": mode,
                "as_of": "2026-06-15",
                "window": {"start": "2026-06-09", "end": "2026-06-15"},
                "headline": {
                    "target": 25000,
                    "move_date": "2026-12-01",
                    "net_saved_window": -183.31,
                    "income_window": 0,
                    "spend_window": 183.31,
                    "running_total": -36750.21,
                    "pct_to_target": -147,
                    "current_pace_mo": -6206.98,
                    "required_pace_mo": 2272.73,
                    "projected": -71137.37,
                    "status": "behind" if behind else "on track",
                    "gap": 96137.37,
                    "months_remaining": 5.5,
                    "saved_vs_habit": 0,
                },
                "rule_tally": {"on_track": 0, "drifting": 0, "slipped": 0},
                "detail": {"off_track_rules": []},
                "flags": [],
            },
            "fee_fraud": {
                "tool": "fee_fraud_scan",
                "as_of": "2026-06-15",
                "window": {"start": "2026-05-17", "end": "2026-06-15", "days": 30},
                "headline": {
                    "avoidable_plus_suspect": fees,
                    "avoidable": fees,
                    "suspect": 0,
                    "fees_total": 19.00,
                    "dup_recoverable": 3.50,
                    "not_theirs_total": 0,
                    "low_conf_total": 0,
                    "n_fees": 3,
                    "n_duplicates": 1,
                    "n_suspect_merchants": 0,
                },
                "detail": {
                    "fees": [{"merchant": "Monthly Service Fee", "amount": 12.00, "date": "2026-06-10", "description": "Checking account"}],
                    "duplicates": [{"merchant": "ATM Surcharge", "amount": 3.50, "dates": ["2026-06-05", "2026-06-05"], "description": "same-day dup"}],
                    "suspicious": [],
                },
                "flags": [],
            },
            "recurring": {
                "tool": "recurring",
                "headline": {
                    "n_active_inflow": 0,
                    "n_active_outflow": 5,
                    "inflow_monthly_runrate": 0,
                    "outflow_monthly_runrate": 5751.98,
                    "net_monthly_runrate": -5751.98,
                },
                "detail": {
                    "top_inflow": [],
                    "top_outflow": [
                        {"merchant": "ACME Corp Payroll", "cadence": "Monthly", "monthly_runrate": 4200, "avg_amount": 4200, "next_date": "2026-07-01"},
                        {"merchant": "Landlord ACH", "cadence": "Monthly", "monthly_runrate": 1500, "avg_amount": 1500, "next_date": "2026-07-01"},
                    ],
                },
                "flags": [],
            },
            "reconciliation": {
                "tool": "receipt_reconciliation",
                "available": True,
                "headline": {
                    "coverage_pct": 85.7,
                    "total_receipts": 14,
                    "matched": 12,
                    "verified_amount": 200.00,
                    "n_discrepancies": 1,
                    "discrepancy_amount": 2.84,
                    "n_unmatched_receipts": 1,
                    "unmatched_receipt_amount": 50.00,
                    "n_unmatched_charges": 1,
                    "unmatched_charge_amount": 15.99,
                    "n_price_changes": 0,
                },
                "for_fee_fraud": {
                    "discrepancies": [],
                    "unmatched_charges": [],
                },
                "for_subscriptions": {"price_changes": []},
                "for_cashflow": {"pending_receipts": []},
                "flags": [],
            },
        },
        "flags": ["OVERDRAFT projected Jul 1"],
    }


def _receipts_only_digest():
    """A digest where bank sections are unavailable (receipts-only mode)."""
    d = _mock_digest(overdraft=False, behind=False, fees=0)
    d["sections"]["forecast"]["available"] = False
    d["sections"]["budget"]["available"] = False
    d["sections"]["fee_fraud"]["available"] = False
    d["sections"]["recurring"]["available"] = False
    return d


# ─────────────────────────── 1. select_hero tests ────────────────────────────

class TestSelectHero(unittest.TestCase):

    def test_overdraft_risk(self):
        """Priority 1: overdraft risk -> severity red, badge 'Overdraft Risk'."""
        digest = _mock_digest(overdraft=True, behind=True, fees=22.50)
        hero = select_hero(digest)
        self.assertEqual(hero["severity"], "red")
        self.assertEqual(hero["badge_text"], "Overdraft Risk")

    def test_low_balance(self):
        """Priority 2: low balance (low_balance True but overdraft False) -> amber."""
        digest = _mock_digest(overdraft=False, behind=False, fees=0)
        # Set low_balance without overdraft
        fh = digest["sections"]["forecast"]["headline"]
        fh["overdraft"] = False
        fh["low_balance"] = True
        fh["min_balance"] = 50.0
        fh["min_date"] = "2026-07-10"
        fh["buffer"] = 100.0
        hero = select_hero(digest)
        self.assertEqual(hero["severity"], "amber")
        self.assertEqual(hero["badge_text"], "Low Balance")

    def test_behind_pace(self):
        """Priority 3: behind pace (no forecast issues) -> red, badge contains 'Behind'."""
        digest = _mock_digest(overdraft=False, behind=True, fees=0)
        fh = digest["sections"]["forecast"]["headline"]
        fh["overdraft"] = False
        fh["low_balance"] = False
        hero = select_hero(digest)
        self.assertEqual(hero["severity"], "red")
        self.assertIn("Behind", hero["badge_text"])

    def test_fees_flagged_only(self):
        """Priority 4: fees flagged (no forecast/budget issues) -> amber."""
        digest = _mock_digest(overdraft=False, behind=False, fees=22.50)
        fh = digest["sections"]["forecast"]["headline"]
        fh["overdraft"] = False
        fh["low_balance"] = False
        hero = select_hero(digest)
        self.assertEqual(hero["severity"], "amber")
        self.assertEqual(hero["badge_text"], "Fees Flagged")

    def test_all_clear(self):
        """Priority 5: no issues at all -> green, badge 'All Clear'."""
        digest = _mock_digest(overdraft=False, behind=False, fees=0)
        fh = digest["sections"]["forecast"]["headline"]
        fh["overdraft"] = False
        fh["low_balance"] = False
        hero = select_hero(digest)
        self.assertEqual(hero["severity"], "green")
        self.assertEqual(hero["badge_text"], "All Clear")


# ─────────────────────────── 2. render_weekly_html tests ─────────────────────

class TestRenderWeeklyHtml(unittest.TestCase):

    def test_full_digest_content(self):
        """Full weekly digest contains key elements."""
        digest = _mock_digest()
        html = render_weekly_html(digest)
        self.assertIn("finance.mcp", html)
        self.assertIn("hero-card", html)
        self.assertIn("vitals-strip", html)
        self.assertIn("Weekly Finance Digest", html)
        self.assertIn("report-footer", html)
        # Section headers present
        self.assertIn("Balance forecast", html)
        self.assertIn("Savings Pace", html)
        self.assertIn("Fee + Fraud Scan", html)
        self.assertIn("Recurring Snapshot", html)
        # Receipts are invisible infrastructure — no standalone section

    def test_receipts_only_digest(self):
        """Receipts-only digest shows 'Connect your bank' placeholder cards."""
        digest = _receipts_only_digest()
        html = render_weekly_html(digest)
        self.assertIn("Connect your bank", html)
        self.assertIn("unavailable-card", html)

    def test_html_well_formed(self):
        """Output starts with <!DOCTYPE and has closing </html>."""
        digest = _mock_digest()
        html = render_weekly_html(digest)
        self.assertTrue(html.strip().startswith("<!DOCTYPE html>"))
        self.assertTrue(html.strip().endswith("</html>"))


# ─────────────────────────── 3. render_monthly_html tests ────────────────────

class TestRenderMonthlyHtml(unittest.TestCase):

    def test_monthly_specific_sections(self):
        """Unified snapshot has all sections (weekly/monthly collapsed into one)."""
        digest = _mock_digest(mode="monthly")
        html = render_monthly_html(digest)   # alias for the unified snapshot
        self.assertIn("Month by month", html)
        self.assertIn("Savings Pace", html)
        self.assertIn("Where your money goes", html)
        self.assertIn("Your snapshot", html)

    def test_monthly_badge(self):
        """Monthly report contains 'Monthly' badge."""
        digest = _mock_digest(mode="monthly")
        html = render_monthly_html(digest)
        self.assertIn("Monthly", html)

    def test_monthly_well_formed(self):
        """Monthly output starts with <!DOCTYPE and has closing </html>."""
        digest = _mock_digest(mode="monthly")
        html = render_monthly_html(digest)
        self.assertTrue(html.strip().startswith("<!DOCTYPE html>"))
        self.assertTrue(html.strip().endswith("</html>"))

    def test_monthly_has_key_elements(self):
        """Monthly digest contains hero card, vitals, footer."""
        digest = _mock_digest(mode="monthly")
        html = render_monthly_html(digest)
        self.assertIn("finance.mcp", html)
        self.assertIn("hero-card", html)
        self.assertIn("vitals-strip", html)
        self.assertIn("report-footer", html)


# ─────────────────────────── 4. render_email_html tests ──────────────────────

class TestRenderEmailHtml(unittest.TestCase):

    def test_email_only_portion(self):
        """Email HTML produces only the email portion (no full report sections)."""
        digest = _mock_digest()
        html = render_email_html(digest, report_url="https://example.com/report")
        self.assertIn("email-portion", html)
        # Should NOT contain the actual divider or full-report HTML elements
        # (they do appear in the shared CSS, so check for the HTML markup instead)
        self.assertNotIn('<div class="divider-section">', html)
        self.assertNotIn('<div class="full-report"', html)

    def test_cta_link(self):
        """CTA button links to the provided report_url."""
        url = "https://example.com/my-weekly-report"
        digest = _mock_digest()
        html = render_email_html(digest, report_url=url)
        self.assertIn(url, html)
        self.assertIn("cta-button", html)

    def test_no_full_report_content(self):
        """Email HTML does not contain the full-report divider or report sections."""
        digest = _mock_digest()
        html = render_email_html(digest, report_url="https://example.com/report")
        # Check that the actual Full Report HTML elements are absent
        # (CSS class definitions in <style> are expected and fine)
        self.assertNotIn('<div class="divider-label">Full Report</div>', html)
        self.assertNotIn('<div class="report-header">', html)


# ─────────────────────────── 5. _format_date_range tests ─────────────────────

class TestFormatDateRange(unittest.TestCase):

    def test_same_month(self):
        """Same month: 'Jun 9 &ndash; 15, 2026'."""
        result = _format_date_range("2026-06-09", "2026-06-15")
        self.assertEqual(result, "Jun 9 &ndash; 15, 2026")

    def test_cross_month(self):
        """Cross month: 'Jun 28 &ndash; Jul 4, 2026'."""
        result = _format_date_range("2026-06-28", "2026-07-04")
        self.assertEqual(result, "Jun 28 &ndash; Jul 4, 2026")


if __name__ == "__main__":
    unittest.main()
