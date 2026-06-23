#!/usr/bin/env python3
"""
test_delivery.py — graceful-degradation + formatting tests for delivery.py.

The failure mode for a shared delivery layer is raising when env is unset
(which would crash every importing tool's --no-voice / offline path). These
tests assert BOTH side-effecting helpers degrade quietly with no env, plus the
money formatter and the no-raw-transactions invariant of narrate().

Run: python3 test_delivery.py   (exits non-zero on failure)
"""

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

from finance_mcp.report import delivery


class GracefulDegradation(unittest.TestCase):
    """With env vars unset, nothing raises; email -> False, narration -> None."""

    def setUp(self):
        # Snapshot and clear the env vars these helpers gate on.
        self._saved = {}
        for k in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "ANTHROPIC_API_KEY"):
            self._saved[k] = os.environ.pop(k, None)
        # Clearing env is not enough: _gmail_password() falls back to the macOS
        # Keychain, so on a machine with real credentials send_email would skip
        # the "no creds" path and fire a real authenticated SMTP send to the
        # test recipient. Neutralize the Keychain fallback so these tests
        # exercise the no-credential contract regardless of machine state.
        self._pw_patch = mock.patch.object(delivery, "_gmail_password", return_value=None)
        self._pw_patch.start()

    def tearDown(self):
        self._pw_patch.stop()
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_send_email_no_env_returns_false(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = delivery.send_email("x@example.com", "Subj", "Body")
        self.assertIs(result, False)
        self.assertIn("email skipped", buf.getvalue())

    def test_send_email_no_env_does_not_raise(self):
        # Explicit: no exception escapes even with a None recipient.
        try:
            with redirect_stdout(io.StringIO()):
                delivery.send_email(None, "S", "B")
        except Exception as e:  # pragma: no cover
            self.fail(f"send_email raised with no env: {e!r}")

    def test_call_haiku_no_key_returns_none(self):
        self.assertIsNone(delivery.call_haiku("system", "user"))

    def test_call_haiku_no_key_does_not_raise(self):
        try:
            self.assertIsNone(delivery.call_haiku("s", "u"))
        except Exception as e:  # pragma: no cover
            self.fail(f"call_haiku raised with no key: {e!r}")

    def test_narrate_no_key_returns_none(self):
        summary = {"tool": "demo", "headline": {"net": 12.5}}
        self.assertIsNone(delivery.narrate(summary, "blunt", "monthly"))

    def test_narrate_no_key_does_not_raise(self):
        try:
            self.assertIsNone(delivery.narrate({"tool": "x"}, "", "weekly"))
        except Exception as e:  # pragma: no cover
            self.fail(f"narrate raised with no key: {e!r}")


class Formatting(unittest.TestCase):
    """money() must be exact — confidently-wrong formatting is a failure mode."""

    def test_money_basic(self):
        self.assertEqual(delivery.money(0), "$0.00")
        self.assertEqual(delivery.money(5), "$5.00")
        self.assertEqual(delivery.money(1234.5), "$1,234.50")

    def test_money_rounds_two_dp(self):
        self.assertEqual(delivery.money(1234.567), "$1,234.57")
        self.assertEqual(delivery.money(0.005), "$0.01")  # banker's? no — round-half-up display

    def test_money_negative(self):
        self.assertEqual(delivery.money(-42.1), "-$42.10")

    def test_money_large_grouping(self):
        self.assertEqual(delivery.money(1000000), "$1,000,000.00")


class NoRawTransactionsContract(unittest.TestCase):
    """narrate() must serialize ONLY the summary dict it is handed.

    We can't hit the network without a key, but we CAN verify the prompt-builder
    feeds the model exactly the summary JSON and nothing else, by intercepting
    call_haiku and inspecting the (system, user) payload it would send.
    """

    def test_narrate_sends_only_the_summary_dict(self):
        captured = {}

        def fake_call(system, user):
            captured["system"] = system
            captured["user"] = user
            return "ok"

        orig = delivery.call_haiku
        delivery.call_haiku = fake_call
        try:
            summary = {"tool": "forecaster", "as_of": "2026-06-15",
                       "headline": {"projected": 9000.0},
                       "detail": [{"ym": "2026-05", "net": 800.0}],
                       "flags": []}
            out = delivery.narrate(summary, "encouraging", "monthly")
        finally:
            delivery.call_haiku = orig

        self.assertEqual(out, "ok")
        # The user message must be the summary JSON verbatim (after the SUMMARY: tag),
        # i.e. exactly what we passed in, re-serialized — no extra rows injected.
        prefix = "SUMMARY:\n"
        self.assertTrue(captured["user"].startswith(prefix))
        round_tripped = json.loads(captured["user"][len(prefix):])
        self.assertEqual(round_tripped, summary)
        # Tone and mode guidance must reach the system prompt.
        self.assertIn("encouraging", captured["system"])
        self.assertIn("MONTHLY", captured["system"])
        # And the architectural guarantee is stated to the model.
        self.assertIn("never raw transactions", captured["system"])


class ModeVoices(unittest.TestCase):
    def test_known_modes_distinct(self):
        w = delivery._voice_for("weekly")
        m = delivery._voice_for("monthly")
        g = delivery._voice_for("forecaster")  # unknown -> generic default
        self.assertIn("WEEKLY", w)
        self.assertIn("MONTHLY", m)
        self.assertNotEqual(w, m)
        self.assertNotEqual(g, w)
        self.assertNotEqual(g, m)


if __name__ == "__main__":
    unittest.main(verbosity=2)
