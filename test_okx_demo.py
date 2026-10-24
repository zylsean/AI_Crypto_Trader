import base64
import hashlib
import hmac
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock
from pathlib import Path
from urllib.parse import urlsplit

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from check_okx_demo import CANDIDATE_BASE_URLS, run_diagnosis
from exchange_base import ExchangeError, ExchangeTimeout
import okx_demo
from okx_demo import USER_AGENT, OKXApiError, OKXDemoClient, default_transport, sign
from secrets_util import (
    MissingCredentials, credentials_path_warnings, load_credentials,
    locate_credentials_file,
)

BASE = "https://openapi.okx.com"
KEY = "KEYVALUE-abc-123456"
SECRET = "SECRETVALUE-xyz-987654"
PASS = "PassPhrase#2026"
CREDS = {"api_key": KEY, "secret": SECRET, "passphrase": PASS}
NOW = 1_700_000_000.0           # 2023-11-14T22:13:20.000Z
NOW_ISO = "2023-11-14T22:13:20.000Z"


def ok(data):
    return 200, json.dumps({"code": "0", "msg": "", "data": data})


def err(code, msg="", status=200):
    return status, json.dumps({"code": code, "msg": msg, "data": []})


class FakeTransport:
    """按 (主机, 路径) 返回预设响应，并记录每次请求。"""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        parts = urlsplit(url)
        self.calls.append({
            "method": method, "url": url, "host": parts.netloc,
            "path": parts.path, "query": parts.query,
            "headers": dict(headers), "body": body,
        })
        route = self.routes.get((parts.netloc, parts.path))
        if route is None:
            route = self.routes.get(("*", parts.path))
        if route is None:
            return 404, "not found"
        if isinstance(route, Exception):
            raise route
        return route


def client(routes=None, base=BASE, creds=CREDS):
    transport = FakeTransport(routes or {})
    return OKXDemoClient(base, [base], creds, transport=transport,
                         clock=lambda: NOW), transport


class TestSigning(unittest.TestCase):
    def test_sign_matches_independent_computation(self):
        prehash = NOW_ISO + "GET" + "/api/v5/account/balance"
        expected = base64.b64encode(
            hmac.new(SECRET.encode(), prehash.encode(), hashlib.sha256).digest()
        ).decode()
        self.assertEqual(
            sign(SECRET, NOW_ISO, "get", "/api/v5/account/balance"), expected
        )

    def test_sign_includes_body_and_query(self):
        a = sign(SECRET, NOW_ISO, "POST", "/p", '{"a":1}')
        b = sign(SECRET, NOW_ISO, "POST", "/p", '{"a":2}')
        c = sign(SECRET, NOW_ISO, "POST", "/p?x=1", '{"a":1}')
        self.assertEqual(len({a, b, c}), 3)

    def test_client_signs_path_with_query_string(self):
        c, t = client({(("openapi.okx.com"), "/api/v5/account/x"): ok([])})
        c.request("GET", "/api/v5/account/x", params={"ccy": "BTC"}, private=True)
        call = t.calls[0]
        self.assertEqual(call["query"], "ccy=BTC")
        expected = sign(SECRET, NOW_ISO, "GET", "/api/v5/account/x?ccy=BTC")
        self.assertEqual(call["headers"]["OK-ACCESS-SIGN"], expected)
        self.assertEqual(call["headers"]["OK-ACCESS-TIMESTAMP"], NOW_ISO)


class TestHeadersAndGuards(unittest.TestCase):
    def test_private_request_has_all_headers_and_demo_flag(self):
        c, t = client({("openapi.okx.com", "/api/v5/account/balance"): ok([{}])})
        c.get_balance()
        h = t.calls[0]["headers"]
        self.assertEqual(h["x-simulated-trading"], "1")
        self.assertEqual(h["OK-ACCESS-KEY"], KEY)
        self.assertEqual(h["OK-ACCESS-PASSPHRASE"], PASS)
        self.assertNotIn(SECRET, "".join(str(v) for v in h.values()))
        self.assertNotIn("OK-ACCESS-SECRET", h)

    def test_public_request_has_demo_flag_but_no_key_headers(self):
        c, t = client({("openapi.okx.com", "/api/v5/public/time"): ok([{"ts": "1"}])})
        c.get_server_time()
        h = t.calls[0]["headers"]
        self.assertEqual(h["x-simulated-trading"], "1")
        for name in ("OK-ACCESS-KEY", "OK-ACCESS-SIGN", "OK-ACCESS-PASSPHRASE"):
            self.assertNotIn(name, h)

    def test_unlisted_base_url_refused(self):
        for bad in ("https://www.openapi.com", "https://evil.example.com",
                    "http://openapi.okx.com", "https://openapi.okx.com.evil.com"):
            with self.assertRaises(ValueError):
                OKXDemoClient(bad, [BASE], CREDS)
        with self.assertRaises(ValueError):
            OKXDemoClient(BASE, [], CREDS)

    def test_http_base_refused_even_if_listed(self):
        with self.assertRaises(ValueError):
            OKXDemoClient("http://openapi.okx.com", ["http://openapi.okx.com"], CREDS)

    def test_private_call_without_credentials_refused(self):
        c = OKXDemoClient(BASE, [BASE], None, transport=FakeTransport({}))
        with self.assertRaises(ValueError):
            c.get_balance()

    def test_repr_hides_credentials(self):
        c, _ = client()
        text = repr(c)
        for value in (KEY, SECRET, PASS):
            self.assertNotIn(value, text)

    def test_client_has_no_order_methods(self):
        c, _ = client()
        for name in ("place_order", "cancel_order"):
            self.assertFalse(hasattr(c, name))


class TestResponseHandling(unittest.TestCase):
    def test_success_returns_data_list(self):
        c, _ = client({("openapi.okx.com", "/api/v5/account/config"): ok([{"perm": "read_only"}])})
        self.assertEqual(c.get_account_config()["perm"], "read_only")

    def test_error_code_raises_okx_api_error(self):
        c, _ = client({("openapi.okx.com", "/api/v5/account/config"):
                       err("50119", "API key doesn't exist")})
        with self.assertRaises(OKXApiError) as ctx:
            c.get_account_config()
        self.assertEqual(ctx.exception.code, "50119")
        self.assertEqual(ctx.exception.http_status, 200)
        self.assertFalse(ctx.exception.unknown_outcome)

    def test_http_error_with_json_body(self):
        c, _ = client({("openapi.okx.com", "/api/v5/account/config"):
                       err("50011", "rate limit", status=429)})
        with self.assertRaises(OKXApiError) as ctx:
            c.get_account_config()
        self.assertEqual(ctx.exception.http_status, 429)

    def test_50004_is_unknown_outcome(self):
        c, _ = client({("openapi.okx.com", "/api/v5/account/config"):
                       err("50004", "timeout")})
        with self.assertRaises(OKXApiError) as ctx:
            c.get_account_config()
        self.assertTrue(ctx.exception.unknown_outcome)

    def test_non_json_response_is_timeout_style_error(self):
        c, _ = client({("openapi.okx.com", "/api/v5/account/config"):
                       (403, "<html>blocked</html>")})
        with self.assertRaises(ExchangeTimeout) as ctx:
            c.get_account_config()
        self.assertIn("403", str(ctx.exception))

    def test_unexpected_structure(self):
        c, _ = client({("openapi.okx.com", "/api/v5/account/config"): (200, "[1,2]")})
        with self.assertRaises(ExchangeTimeout):
            c.get_account_config()
        c2, _ = client({("openapi.okx.com", "/api/v5/account/config"): ok([])})
        with self.assertRaises(ExchangeTimeout):
            c2.get_account_config()

    def test_transport_failure_propagates_as_timeout(self):
        c, _ = client({("openapi.okx.com", "/api/v5/account/config"):
                       ExchangeTimeout("network error: URLError")})
        with self.assertRaises(ExchangeTimeout):
            c.get_account_config()

    def test_secrets_in_error_message_are_redacted(self):
        c, _ = client({("openapi.okx.com", "/api/v5/account/config"):
                       err("50113", f"bad key {KEY} secret {SECRET} pass {PASS}")})
        with self.assertRaises(OKXApiError) as ctx:
            c.get_account_config()
        text = str(ctx.exception) + str(ctx.exception.msg)
        for value in (KEY, SECRET, PASS):
            self.assertNotIn(value, text)


class TestNonJsonAndTransport(unittest.TestCase):
    def test_non_json_message_includes_readable_preview(self):
        body = "<html><body><h1>Access denied by policy</h1></body></html>"
        c, _ = client({("openapi.okx.com", "/api/v5/public/time"): (403, body)})
        with self.assertRaises(ExchangeTimeout) as ctx:
            c.get_server_time()
        text = str(ctx.exception)
        self.assertIn("403", text)
        self.assertIn("Access denied by policy", text)
        self.assertNotIn("<html>", text)

    def test_preview_is_truncated(self):
        c, _ = client({("openapi.okx.com", "/api/v5/public/time"): (502, "x" * 5000)})
        with self.assertRaises(ExchangeTimeout) as ctx:
            c.get_server_time()
        self.assertLess(len(str(ctx.exception)), 300)

    def test_empty_body_preview(self):
        c, _ = client({("openapi.okx.com", "/api/v5/public/time"): (403, "")})
        with self.assertRaises(ExchangeTimeout) as ctx:
            c.get_server_time()
        self.assertIn("（空）", str(ctx.exception))

    def test_preview_redacts_secrets(self):
        body = f"<p>bad request {KEY} {SECRET} {PASS}</p>"
        c, _ = client({("openapi.okx.com", "/api/v5/account/config"): (400, body)})
        with self.assertRaises(ExchangeTimeout) as ctx:
            c.get_account_config()
        for value in (KEY, SECRET, PASS):
            self.assertNotIn(value, str(ctx.exception))

    def test_user_agent_is_honest_and_present_on_all_requests(self):
        self.assertNotIn("Mozilla", USER_AGENT)
        c, t = client({
            ("openapi.okx.com", "/api/v5/public/time"): ok([{"ts": "1"}]),
            ("openapi.okx.com", "/api/v5/account/balance"): ok([{}]),
        })
        c.get_server_time()
        c.get_balance()
        for call in t.calls:
            self.assertEqual(call["headers"]["User-Agent"], USER_AGENT)

    def test_default_transport_returns_http_error_body_and_closes_it(self):
        error = urllib.error.HTTPError(
            "https://x", 403, "Forbidden", {}, io.BytesIO(b"blocked page"))
        with mock.patch.object(okx_demo.urllib.request, "urlopen", side_effect=error):
            status, text = default_transport("GET", "https://x", {}, None, 1.0)
        self.assertEqual((status, text), (403, "blocked page"))
        self.assertTrue(error.fp.closed)

    def test_default_transport_network_error_becomes_timeout(self):
        with mock.patch.object(
                okx_demo.urllib.request, "urlopen",
                side_effect=urllib.error.URLError("no route")):
            with self.assertRaises(ExchangeTimeout) as ctx:
                default_transport("GET", "https://x", {}, None, 1.0)
        self.assertIn("URLError", str(ctx.exception))


class TestClock(unittest.TestCase):
    def test_small_offset_ok(self):
        c, _ = client({("openapi.okx.com", "/api/v5/public/time"):
                       ok([{"ts": str(int((NOW - 2) * 1000))}])})
        offset, good = c.check_clock()
        self.assertAlmostEqual(offset, 2.0, places=3)
        self.assertTrue(good)

    def test_large_offset_flagged(self):
        c, _ = client({("openapi.okx.com", "/api/v5/public/time"):
                       ok([{"ts": str(int((NOW + 60) * 1000))}])})
        offset, good = c.check_clock()
        self.assertAlmostEqual(offset, -60.0, places=3)
        self.assertFalse(good)

    def test_bad_time_payload(self):
        c, _ = client({("openapi.okx.com", "/api/v5/public/time"): ok([{"x": 1}])})
        with self.assertRaises(ExchangeTimeout):
            c.get_server_time()

    def test_instruments_request_params(self):
        c, t = client({("openapi.okx.com", "/api/v5/public/instruments"):
                       ok([{"instId": "BTC-USDT", "minSz": "0.00001"}])})
        data = c.get_instruments("SPOT", "BTC-USDT")
        self.assertEqual(data[0]["instId"], "BTC-USDT")
        self.assertEqual(t.calls[0]["query"], "instType=SPOT&instId=BTC-USDT")


class TestCredentials(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, name, data):
        path = self.dir / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_from_environment(self):
        env = {"OKX_DEMO_API_KEY": KEY, "OKX_DEMO_SECRET": SECRET,
               "OKX_DEMO_PASSPHRASE": PASS}
        self.assertEqual(load_credentials(environ=env), CREDS)

    def test_from_file(self):
        path = self.write("c.json", CREDS)
        self.assertEqual(load_credentials(path, environ={}), CREDS)

    def test_file_inside_project_refused(self):
        project = self.dir / "project"
        project.mkdir()
        path = project / "c.json"
        path.write_text(json.dumps(CREDS), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_credentials(path, environ={}, project_root=project)

    def test_file_outside_project_allowed(self):
        project = self.dir / "project"
        project.mkdir()
        path = self.write("c.json", CREDS)
        self.assertEqual(
            load_credentials(path, environ={}, project_root=project), CREDS
        )

    def test_missing_file_and_fields(self):
        with self.assertRaises(MissingCredentials):
            load_credentials(self.dir / "nope.json", environ={})
        with self.assertRaises(MissingCredentials):
            load_credentials(environ={})
        path = self.write("bad.json", {"api_key": KEY, "secret": "", "passphrase": PASS})
        with self.assertRaises(MissingCredentials) as ctx:
            load_credentials(path, environ={})
        self.assertIn("secret", str(ctx.exception))
        for value in (KEY, PASS):
            self.assertNotIn(value, str(ctx.exception))

    def test_invalid_json_message_has_no_content(self):
        path = self.dir / "junk.json"
        path.write_text(f"{SECRET} not json", encoding="utf-8")
        with self.assertRaises(MissingCredentials) as ctx:
            load_credentials(path, environ={})
        self.assertNotIn(SECRET, str(ctx.exception))

    def test_icloud_path_warning(self):
        self.assertTrue(credentials_path_warnings(
            self.dir / "Mobile Documents" / "iCloud~x" / "c.json"))
        self.assertEqual(credentials_path_warnings(self.dir / "c.json"), [])


class TestLocateCredentialsFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.docs = Path(self.tmp.name) / "Documents"
        self.project = self.docs / "AI_Crypto_Trader"
        self.project.mkdir(parents=True)

    def test_finds_file_next_to_project_folder(self):
        beside = self.docs / "okx_demo_credentials.json"
        beside.write_text("{}", encoding="utf-8")
        found, misplaced = locate_credentials_file(
            [beside, self.project / "okx_demo_credentials.json"], self.project)
        self.assertEqual(found, beside)
        self.assertEqual(misplaced, [])

    def test_file_inside_project_is_reported_not_used(self):
        inside = self.project / "okx_demo_credentials.json"
        inside.write_text("{}", encoding="utf-8")
        found, misplaced = locate_credentials_file(
            [self.docs / "okx_demo_credentials.json", inside], self.project)
        self.assertIsNone(found)
        self.assertEqual(misplaced, [inside])

    def test_nothing_found(self):
        found, misplaced = locate_credentials_file(
            [self.docs / "okx_demo_credentials.json"], self.project)
        self.assertIsNone(found)
        self.assertEqual(misplaced, [])

    def test_first_existing_candidate_wins(self):
        first = self.docs / "okx_demo_credentials.json"
        second = Path(self.tmp.name) / "okx_demo_credentials.json"
        first.write_text("{}", encoding="utf-8")
        second.write_text("{}", encoding="utf-8")
        found, _ = locate_credentials_file([first, second], self.project)
        self.assertEqual(found, first)


GOOD_CONFIG = {"perm": "read_only", "acctLv": "1", "feeType": "0", "uid": "123456789"}
BALANCE = {"details": [{"ccy": "USDT", "cashBal": "100000"},
                       {"ccy": "BTC", "cashBal": "1"}]}


def server_routes(host, config=GOOD_CONFIG, ts=None):
    ts = ts if ts is not None else str(int(NOW * 1000))
    return {
        (host, "/api/v5/public/time"): ok([{"ts": ts}]),
        (host, "/api/v5/account/config"): ok([config]),
        (host, "/api/v5/account/balance"): ok([BALANCE]),
    }


class TestDiagnosis(unittest.TestCase):
    def run_it(self, routes, candidates=None):
        lines = []
        transport = FakeTransport(routes)
        result = run_diagnosis(
            candidates or ["https://openapi.okx.com", "https://www.okx.com"],
            CREDS, transport=transport, clock=lambda: NOW, out=lines.append,
        )
        return result, lines, transport

    def test_first_candidate_works(self):
        result, lines, t = self.run_it(server_routes("openapi.okx.com"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["working_base_url"], "https://openapi.okx.com")
        self.assertTrue(all(c["host"] == "openapi.okx.com" for c in t.calls))
        self.assertTrue(any("USDT=100000" in line for line in lines))

    def test_falls_through_to_next_candidate_after_50119(self):
        routes = server_routes("www.okx.com")
        routes.update({
            ("openapi.okx.com", "/api/v5/public/time"): ok([{"ts": str(int(NOW * 1000))}]),
            ("openapi.okx.com", "/api/v5/account/config"): err("50119", "API key doesn't exist"),
        })
        result, lines, _ = self.run_it(routes)
        self.assertEqual(result["working_base_url"], "https://www.okx.com")
        text = "\n".join(lines)
        self.assertIn("50119", text)
        self.assertIn("线索（不是结论）", text)

    def test_all_candidates_fail(self):
        routes = {}
        for host in ("openapi.okx.com", "www.okx.com"):
            routes[(host, "/api/v5/public/time")] = ok([{"ts": str(int(NOW * 1000))}])
            routes[(host, "/api/v5/account/config")] = err("50119", "API key doesn't exist")
        result, lines, _ = self.run_it(routes)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["working_base_url"])
        self.assertIn("所有候选基址都失败了", "\n".join(lines))

    def test_network_failure_reported_as_such(self):
        routes = {("*", "/api/v5/public/time"): ExchangeTimeout("network error: URLError")}
        result, lines, _ = self.run_it(routes)
        self.assertFalse(result["ok"])
        self.assertIn("公共时间接口失败", "\n".join(lines))

    def test_50101_on_some_hosts_gives_live_key_inference(self):
        routes = {}
        for host, code in (("openapi.okx.com", "50101"), ("us.okx.com", "50119")):
            routes[(host, "/api/v5/public/time")] = ok([{"ts": str(int(NOW * 1000))}])
            routes[(host, "/api/v5/account/config")] = err(
                code, "APIKey does not match current environment.", status=401)
        result, lines, _ = self.run_it(
            routes, ["https://openapi.okx.com", "https://us.okx.com"])
        text = "\n".join(lines)
        self.assertFalse(result["ok"])
        self.assertEqual(result["key_recognized_at"], ["https://openapi.okx.com"])
        self.assertIn("推断（不是结论）", text)
        self.assertIn("实盘 Key", text)

    def test_only_50119_gives_no_recognition_inference(self):
        routes = {}
        for host in ("openapi.okx.com", "us.okx.com"):
            routes[(host, "/api/v5/public/time")] = ok([{"ts": str(int(NOW * 1000))}])
            routes[(host, "/api/v5/account/config")] = err(
                "50119", "API key doesn't exist", status=401)
        result, lines, _ = self.run_it(
            routes, ["https://openapi.okx.com", "https://us.okx.com"])
        self.assertEqual(result["key_recognized_at"], [])
        self.assertNotIn("推断（不是结论）", "\n".join(lines))

    def test_all_403_on_public_endpoint_explains_network_layer(self):
        routes = {("*", "/api/v5/public/time"): (403, "<html>Forbidden</html>")}
        result, lines, t = self.run_it(routes)
        text = "\n".join(lines)
        self.assertFalse(result["ok"])
        self.assertIn("PUBLIC_ENDPOINT_FAILED", result["problems"])
        self.assertIn("HTTP 403", text)
        self.assertIn("不在 Key、域名或签名上", text)
        self.assertIn("不提供绕过访问限制的功能", text)
        # 凭证没有被用到：没有任何私有请求
        self.assertFalse(any("OK-ACCESS-KEY" in c["headers"] for c in t.calls))
        self.assertFalse(any(c["path"].startswith("/api/v5/account") for c in t.calls))

    def test_clock_offset_stops_diagnosis(self):
        routes = server_routes("openapi.okx.com", ts=str(int((NOW + 120) * 1000)))
        result, lines, t = self.run_it(routes)
        self.assertFalse(result["ok"])
        self.assertIn("CLOCK_OFFSET", result["problems"])
        self.assertFalse(any(c["path"].startswith("/api/v5/account") for c in t.calls))

    def test_withdraw_permission_fails_guard_but_base_is_found(self):
        cfg = dict(GOOD_CONFIG, perm="read_only,withdraw")
        result, lines, _ = self.run_it(server_routes("openapi.okx.com", config=cfg))
        self.assertFalse(result["ok"])
        self.assertEqual(result["working_base_url"], "https://openapi.okx.com")
        self.assertIn("WITHDRAW_PERMISSION", result["problems"])

    def test_output_never_contains_credentials_or_uid(self):
        routes = server_routes("openapi.okx.com")
        routes[("openapi.okx.com", "/api/v5/account/balance")] = err(
            "50113", f"echo {KEY} {SECRET} {PASS}")
        result, lines, _ = self.run_it(routes)
        text = "\n".join(lines)
        for value in (KEY, SECRET, PASS, "123456789"):
            self.assertNotIn(value, text)

    def test_only_get_requests_and_no_order_endpoints(self):
        _, _, t = self.run_it(server_routes("openapi.okx.com"))
        self.assertTrue(all(c["method"] == "GET" for c in t.calls))
        self.assertFalse(any("trade" in c["path"] for c in t.calls))

    def test_candidates_are_only_okx_domains(self):
        for url in CANDIDATE_BASE_URLS:
            self.assertTrue(url.startswith("https://"))
            host = urlsplit(url).netloc
            self.assertTrue(host == "okx.com" or host.endswith(".okx.com"), url)


if __name__ == "__main__":
    unittest.main(verbosity=2)
