from __future__ import annotations

import os
import tempfile
import unittest

from app.auth_config import load_or_create_signing_key, oauth_callback_url, validate_public_base_url


class AuthConfigTests(unittest.TestCase):
    def test_accepts_https_origin_and_removes_trailing_slash(self):
        self.assertEqual(
            validate_public_base_url("https://service.example/"),
            "https://service.example",
        )

    def test_builds_exact_github_callback(self):
        self.assertEqual(
            oauth_callback_url("https://service.example"),
            "https://service.example/auth/callback",
        )

    def test_rejects_mcp_path(self):
        with self.assertRaisesRegex(ValueError, "HTTPS origin only"):
            validate_public_base_url("https://service.example/mcp")

    def test_rejects_http_and_query(self):
        for value in ("http://service.example", "https://service.example?x=1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_public_base_url(value)

    def test_uses_valid_environment_key_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            key, source = load_or_create_signing_key(directory, "x" * 32)
            self.assertEqual(key, "x" * 32)
            self.assertEqual(source, "environment")
            self.assertFalse(os.path.exists(os.path.join(directory, "oauth_jwt_signing_key")))

    def test_generates_and_reuses_persistent_key(self):
        with tempfile.TemporaryDirectory() as directory:
            first, first_source = load_or_create_signing_key(directory, None)
            second, second_source = load_or_create_signing_key(directory, None)
            self.assertEqual(first, second)
            self.assertGreaterEqual(len(first), 32)
            self.assertEqual(first_source, "persistent_file")
            self.assertEqual(second_source, "persistent_file")

    def test_rejects_short_environment_key(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "at least 32"):
                load_or_create_signing_key(directory, "too-short")


if __name__ == "__main__":
    unittest.main()
