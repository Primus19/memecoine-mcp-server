from __future__ import annotations

import unittest

from app.auth_config import oauth_callback_url, validate_public_base_url


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


if __name__ == "__main__":
    unittest.main()
