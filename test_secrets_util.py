import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from secrets_util import (
    StartupRefused, check_startup_guard, enforce_startup_guard,
    redact_headers, redact_text,
)

KEY = "abcd1234-key-VALUE"
SECRET = "SECRETVALUE9876"
PASS = "myPassphrase!7"
BASE = "https://openapi.okx.com"
OK_CONFIG = {"perm": "read_only,trade", "acctLv": "1"}


class TestRedaction(unittest.TestCase):
    def test_known_secrets_removed(self):
        text = f"error for key {KEY} secret {SECRET} pass {PASS}"
        out = redact_text(text, [KEY, SECRET, PASS])
        for value in (KEY, SECRET, PASS):
            self.assertNotIn(value, out)

    def test_header_values_removed_even_if_unknown(self):
        text = "OK-ACCESS-KEY: zzz111 OK-ACCESS-SIGN=sig999 ok-access-passphrase: pw000"
        out = redact_text(text)
        for value in ("zzz111", "sig999", "pw000"):
            self.assertNotIn(value, out)

    def test_headers_dict_masked_case_insensitive(self):
        headers = {
            "OK-ACCESS-KEY": KEY, "ok-access-sign": "sig",
            "OK-ACCESS-PASSPHRASE": PASS, "Content-Type": "application/json",
            "x-simulated-trading": "1",
        }
        out = redact_headers(headers)
        self.assertEqual(out["OK-ACCESS-KEY"], "***")
        self.assertEqual(out["ok-access-sign"], "***")
        self.assertEqual(out["OK-ACCESS-PASSPHRASE"], "***")
        self.assertEqual(out["Content-Type"], "application/json")
        self.assertEqual(headers["OK-ACCESS-KEY"], KEY)   # 原字典不变

    def test_plain_text_unchanged(self):
        self.assertEqual(redact_text("order filled"), "order filled")


class TestStartupGuard(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(
            check_startup_guard(BASE, [BASE], True, OK_CONFIG), []
        )
        enforce_startup_guard(BASE + "/", [BASE], True, OK_CONFIG)

    def test_empty_allowlist_refuses(self):
        self.assertIn("ALLOWLIST_EMPTY",
                      check_startup_guard(BASE, [], True, OK_CONFIG))
        self.assertIn("ALLOWLIST_EMPTY",
                      check_startup_guard(BASE, None, True, OK_CONFIG))

    def test_unlisted_domain_refuses(self):
        for bad in ("https://www.okx.com", "https://www.openapi.com",
                    "http://openapi.okx.com", "https://openapi.okx.com.evil.com"):
            self.assertIn("BASE_URL_NOT_ALLOWED",
                          check_startup_guard(bad, [BASE], True, OK_CONFIG))

    def test_demo_flag_must_be_true(self):
        for flag in (False, None, 1, "1"):
            self.assertIn("DEMO_FLAG_OFF",
                          check_startup_guard(BASE, [BASE], flag, OK_CONFIG))

    def test_withdraw_permission_refuses(self):
        cfg = {"perm": "read_only,withdraw,trade"}
        self.assertIn("WITHDRAW_PERMISSION",
                      check_startup_guard(BASE, [BASE], True, cfg))

    def test_unknown_permission_refuses(self):
        for cfg in ({}, None, {"perm": ""}, {"perm": None}):
            self.assertIn("PERM_UNKNOWN",
                          check_startup_guard(BASE, [BASE], True, cfg))

    def test_enforce_raises(self):
        with self.assertRaises(StartupRefused):
            enforce_startup_guard(BASE, [BASE], False, OK_CONFIG)


if __name__ == "__main__":
    unittest.main(verbosity=2)

