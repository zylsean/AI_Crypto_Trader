"""run_2c_demo 的假网络测试：没有真实请求，没有真实凭证。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from exchange_base import ExchangeTimeout
from run_2c_demo import Evidence, prepare_credentials, run_checks
from test_okx_demo_exchange import CREDS, FakeOKX, make_exchange, ok, top_error


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "evidence.jsonl"
        self.exchange, self.server = make_exchange()
        self.evidence = Evidence(self.path, list(CREDS.values()))
        self.lines = []

    def run_it(self, **kwargs):
        return run_checks(self.exchange, self.evidence, sleep=lambda s: None,
                          say=self.lines.append, **kwargs)

    def steps(self):
        return [item["step"] for item in self.evidence.items]

    def item(self, step):
        return [i for i in self.evidence.items if i["step"] == step][-1]

    def live_orders(self):
        return [o for o in self.server.orders.values()
                if o["state"] in ("live", "partially_filled")]


class TestDryRun(Base):
    def test_dry_run_sends_no_order_or_cancel(self):
        summary = self.run_it(send_orders=False)
        self.assertTrue(summary["completed"])
        self.assertEqual(self.server.posts(), [])
        self.assertEqual(self.server.posts("/api/v5/trade/cancel-order"), [])
        plan = self.item("plan")
        self.assertEqual(plan["px"], "98000.4")      # 100000.5 * 0.98 向下取整到 0.1
        self.assertEqual(plan["sz"], "0.00001")
        self.assertFalse(plan["send_orders"])

    def test_every_request_is_marked_simulated(self):
        self.run_it(send_orders=True, include_reverse=True)
        for request in self.server.requests:
            self.assertEqual(request["headers"].get("x-simulated-trading"), "1")


class TestStopConditions(Base):
    def test_bad_clock_stops_before_any_private_request(self):
        self.server.time_offset_ms = 60_000
        summary = self.run_it(send_orders=True)
        self.assertFalse(summary["completed"])
        self.assertIn("校准", summary["stopped"])
        self.assertFalse(any("OK-ACCESS-KEY" in r["headers"] for r in self.server.requests))

    def test_zero_usdt_stops_before_order(self):
        self.server.balance_details = [{"ccy": "USDT", "cashBal": "0"}]
        summary = self.run_it(send_orders=True)
        self.assertFalse(summary["completed"])
        self.assertEqual(self.server.posts(), [])

    def test_balance_error_is_recorded_and_stops(self):
        self.server.overrides[("GET", "/api/v5/account/balance")] = [
            top_error("50119", "API key doesn't exist")]
        summary = self.run_it(send_orders=True)
        self.assertFalse(summary["completed"])
        self.assertEqual(self.item("balance")["code"], "50119")
        self.assertEqual(self.server.posts(), [])


class TestSendFlow(Base):
    def test_full_flow_places_once_duplicate_rejected_and_cancels(self):
        summary = self.run_it(send_orders=True)
        self.assertTrue(summary["completed"])
        self.assertEqual(len(self.server.posts()), 2)      # 第 1 次受理 + 重复 ID 的那次
        self.assertEqual(self.item("place")["outcome"], "accepted_not_confirmed")
        self.assertEqual(self.item("confirm")["state"], "live")
        self.assertEqual(self.item("duplicate_clordid")["outcome"], "rejected")
        self.assertEqual(self.item("duplicate_clordid")["s_code"], "99001")
        self.assertEqual(self.item("after_cancel")["state"], "canceled")
        self.assertEqual(self.live_orders(), [])
        self.assertEqual(self.item("final_check")["our_open_orders"], 0)

    def test_reverse_orders_that_get_accepted_are_cleaned_up(self):
        # 假服务器不会校验最小量/价位，所以这两笔会被"意外接受"，必须被撤掉。
        summary = self.run_it(send_orders=True, include_reverse=True)
        self.assertTrue(summary["completed"])
        self.assertEqual(self.item("reverse_below_minsz")["outcome"], "accepted_unexpectedly")
        self.assertEqual(self.item("reverse_off_tick")["outcome"], "accepted_unexpectedly")
        self.assertEqual(self.live_orders(), [])
        self.assertEqual(len(self.server.orders), 3)

    def test_reverse_orders_rejected_are_recorded_with_scode(self):
        original = self.server._handle

        def strict(method, path, record):
            if method == "POST" and path == "/api/v5/trade/order":
                body = json.loads(record["body"])
                from decimal import Decimal
                if body["sz"] == "0.000001" or Decimal(body["px"]) % Decimal("0.1") != 0:
                    return top_error("1", "failed", [
                        {"clOrdId": body["clOrdId"], "sCode": "77001", "sMsg": "example"}])
            return original(method, path, record)

        self.server._handle = strict
        self.run_it(send_orders=True, include_reverse=True)
        self.assertEqual(self.item("reverse_below_minsz")["s_code"], "77001")
        self.assertEqual(self.item("reverse_off_tick")["s_code"], "77001")
        self.assertEqual(self.live_orders(), [])
        # 被明确拒绝的订单不存在于交易所，不应触发清理告警。
        self.assertNotIn("cleanup_unverified", self.steps())

    def test_unknown_place_outcome_never_resends_and_cleans_up(self):
        self.server.execute_then_raise = True
        self.server.raise_once[("POST", "/api/v5/trade/order")] = ExchangeTimeout("net")
        summary = self.run_it(send_orders=True)
        self.assertFalse(summary["completed"])
        self.assertEqual(self.item("place")["outcome"], "unknown")
        self.assertEqual(len(self.server.posts()), 1)               # 没有重发
        self.assertEqual(self.live_orders(), [])                    # 交易所里的那笔被清理
        self.assertIn("cleanup_cancel_sent", self.steps())

    def test_rejected_place_stops_and_does_not_retry(self):
        self.server.overrides[("POST", "/api/v5/trade/order")] = [
            top_error("1", "failed", [{"sCode": "66001", "sMsg": "region or permission"}])]
        summary = self.run_it(send_orders=True)
        self.assertFalse(summary["completed"])
        self.assertEqual(self.item("place")["s_code"], "66001")
        self.assertEqual(len(self.server.posts()), 1)

    def test_order_not_confirmed_live_stops_and_cleans_up(self):
        not_found = (200, json.dumps({"code": "51603", "msg": "x", "data": []}))
        self.server.overrides[("GET", "/api/v5/trade/order")] = [not_found] * 5
        summary = self.run_it(send_orders=True)
        self.assertFalse(summary["completed"])
        self.assertEqual(self.live_orders(), [])

    def test_unexpected_fill_stops_without_claiming_success(self):
        original = self.server._handle

        def filled(method, path, record):
            result = original(method, path, record)
            if method == "POST" and path == "/api/v5/trade/order":
                for order in self.server.orders.values():
                    order.update(state="filled", accFillSz=order["sz"], avgPx="98000.4",
                                 fee="-0.00000001", feeCcy="BTC")
            return result

        self.server._handle = filled
        summary = self.run_it(send_orders=True)
        self.assertFalse(summary["completed"])
        self.assertIn("live", summary["stopped"])


class TestCleanupHonesty(Base):
    def test_cancel_that_does_not_take_effect_is_not_reported_complete(self):
        self.server.cancel_mode = "ignore"
        summary = self.run_it(send_orders=True)
        self.assertFalse(summary["completed"])
        self.assertFalse(summary["cleanup_ok"])
        self.assertEqual(len(self.live_orders()), 1)
        self.assertEqual(self.item("final_check")["our_open_orders"], 1)

    def test_failed_cleanup_alone_makes_the_run_incomplete(self):
        self.server.cancel_mode = "ignore"
        self.exchange.get_open_orders = lambda: []      # 让"挂单列表复查"看不出问题
        summary = self.run_it(send_orders=True)
        self.assertFalse(summary["cleanup_ok"])
        self.assertFalse(summary["completed"])
        self.assertIn("清理", summary["stopped"])

    def test_rejected_main_order_triggers_no_cleanup_noise(self):
        self.server.overrides[("POST", "/api/v5/trade/order")] = [
            top_error("1", "failed", [{"sCode": "66003", "sMsg": "example"}])]
        summary = self.run_it(send_orders=True)
        self.assertFalse(summary["completed"])
        self.assertNotIn("cleanup_summary", self.steps())

    def test_clean_run_reports_cleanup_ok(self):
        summary = self.run_it(send_orders=True, include_reverse=True)
        self.assertTrue(summary["completed"])
        self.assertTrue(summary["cleanup_ok"])


class TestEvidenceFile(Base):
    def test_evidence_is_valid_jsonl_without_secrets_or_balances(self):
        leak = f"{CREDS['api_key']} {CREDS['secret']} {CREDS['passphrase']}"
        self.server.overrides[("POST", "/api/v5/trade/order")] = [
            top_error("1", "failed", [{"sCode": "66002", "sMsg": leak}])]
        self.run_it(send_orders=True)
        text = self.path.read_text(encoding="utf-8")
        for line in text.splitlines():
            json.loads(line)
        for value in CREDS.values():
            self.assertNotIn(value, text)
        self.assertNotIn("100000", text.replace("100000.5", ""))   # 余额数字不落盘
        self.assertIn("usdt_positive", text)


class TestEvidenceRedaction(unittest.TestCase):
    def test_evidence_redacts_secrets_even_if_a_caller_forgets(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "e.jsonl"
            evidence = Evidence(path, list(CREDS.values()))
            evidence.add("x", note=f"leak {CREDS['secret']} and {CREDS['api_key']}")
            text = path.read_text(encoding="utf-8")
            for value in CREDS.values():
                self.assertNotIn(value, text)
            self.assertIn("***", text)
            for item in evidence.items:
                self.assertNotIn(CREDS["secret"], json.dumps(item))


class TestPrepareCredentials(unittest.TestCase):
    KEY = "a" * 36
    SECRET = "B" * 32

    def test_clean_input_has_no_notes(self):
        creds, notes = prepare_credentials(self.KEY, self.SECRET, "Pass1234")
        self.assertEqual(notes, [])
        self.assertEqual(creds, {"api_key": self.KEY, "secret": self.SECRET,
                                 "passphrase": "Pass1234"})

    def test_surrounding_whitespace_is_removed_and_reported(self):
        creds, notes = prepare_credentials(self.KEY + " ", "\n" + self.SECRET, " Pass1234\n")
        self.assertEqual(creds["api_key"], self.KEY)
        self.assertEqual(creds["secret"], self.SECRET)
        self.assertEqual(creds["passphrase"], "Pass1234")
        self.assertEqual(len(notes), 3)

    def test_inner_whitespace_and_odd_lengths_are_flagged(self):
        creds, notes = prepare_credentials("abc def", "short", "Pass 1234")
        text = " ".join(notes)
        self.assertIn("中间含有空白", text)
        self.assertIn("API Key 长度", text)
        self.assertIn("Secret Key 长度", text)

    def test_notes_never_contain_the_credentials(self):
        creds, notes = prepare_credentials("SECRETKEYVALUE 1", "SECRETSECRET 2", "PASSVALUE 3")
        text = " ".join(notes)
        for value in ("SECRETKEYVALUE", "SECRETSECRET", "PASSVALUE"):
            self.assertNotIn(value, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
