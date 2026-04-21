"""Tests for redaction sweep on unmatched user messages (US-TB-01 AC-04)."""

import unittest

from engine.redact import redact_for_unmatched


class TestRedact(unittest.TestCase):
    def test_strips_absolute_posix_paths(self):
        out = redact_for_unmatched("see /Users/jon/secrets/key.pem for key")
        self.assertNotIn("/Users/jon", out)

    def test_strips_emails(self):
        out = redact_for_unmatched("ping alice@example.com about this")
        self.assertNotIn("alice@example.com", out)

    def test_strips_bearer_tokens(self):
        out = redact_for_unmatched("curl -H 'Authorization: Bearer sk-abc123xyz'")
        self.assertNotIn("sk-abc123xyz", out)

    def test_truncates_to_200_chars(self):
        out = redact_for_unmatched("a" * 500)
        self.assertLessEqual(len(out), 200)

    def test_preserves_ordinary_text(self):
        self.assertEqual(redact_for_unmatched("hello world"), "hello world")


if __name__ == "__main__":
    unittest.main()
