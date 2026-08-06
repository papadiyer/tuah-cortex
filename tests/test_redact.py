"""Secret redaction: credentials are masked, ordinary prose is not."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.redact import MASK, lekiu_redact, redact_log_fields, redact_mapping  # noqa: E402


class TestSecretsAreMasked(unittest.TestCase):
    def test_openai_style_key(self):
        out = lekiu_redact("here is sk-abcdefghijklmnopqrstuvwxyz123456 for you")
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", out)
        self.assertIn(MASK, out)

    def test_github_token(self):
        out = lekiu_redact("token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
        self.assertIn(MASK, out)
        self.assertNotIn("ABCDEFGHIJKLMNOPQRSTUVWXYZ", out)

    def test_aws_access_key_id(self):
        self.assertIn(MASK, lekiu_redact("AKIAIOSFODNN7EXAMPLE"))

    def test_jwt(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        self.assertIn(MASK, lekiu_redact(jwt))

    def test_authorization_header(self):
        out = lekiu_redact("Authorization: Bearer supersecretvalue12345")
        self.assertNotIn("supersecretvalue12345", out)

    def test_key_value_assignment(self):
        out = lekiu_redact("api_key=abcd1234efgh5678")
        self.assertNotIn("abcd1234efgh5678", out)

    def test_json_style_secret(self):
        out = lekiu_redact('{"password": "hunter2hunter2"}')
        self.assertNotIn("hunter2hunter2", out)

    def test_private_key_block(self):
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQ\n-----END RSA PRIVATE KEY-----"
        self.assertNotIn("MIIEowIBAAKCAQ", lekiu_redact(pem))


class TestOrdinaryTextSurvives(unittest.TestCase):
    """Over-redaction would make the memory digest useless."""

    def test_plain_prose_untouched(self):
        text = "The context builder merges vector and graph memory under a char budget."
        self.assertEqual(lekiu_redact(text), text)

    def test_word_token_alone_is_not_redacted(self):
        text = "We should cap the token budget at 8192 tokens."
        self.assertEqual(lekiu_redact(text), text)

    def test_code_reference_untouched(self):
        text = "core/vector_store.py imports sqlite3 and json"
        self.assertEqual(lekiu_redact(text), text)

    def test_empty_and_none(self):
        self.assertEqual(lekiu_redact(""), "")
        self.assertIsNone(lekiu_redact(None))


class TestStructures(unittest.TestCase):
    def test_known_secret_fields_masked_wholesale(self):
        out = redact_mapping({"admin_token": "anything-at-all", "actor": "faisal"})
        self.assertEqual(out["admin_token"], MASK)
        self.assertEqual(out["actor"], "faisal")

    def test_nested_dicts_and_lists(self):
        out = lekiu_redact({"items": [{"password": "letmein123"}], "ok": "fine"})
        self.assertEqual(out["items"][0]["password"], MASK)
        self.assertEqual(out["ok"], "fine")

    def test_log_fields_helper(self):
        out = redact_log_fields(request_id="r1", token="secret-token-value")
        self.assertEqual(out["request_id"], "r1")
        self.assertEqual(out["token"], MASK)

    def test_non_string_types_pass_through(self):
        self.assertEqual(lekiu_redact(42), 42)
        self.assertEqual(lekiu_redact(True), True)


if __name__ == "__main__":
    unittest.main()
