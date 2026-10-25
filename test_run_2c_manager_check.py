"""run_2c_manager_check 的假网络测试：没有真实请求，没有真实凭证。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from order_manager import CANCELED
from order_store import OrderStore
from risk_guard import RiskLimits
from run_2c_demo import Evidence
from run_2c_manager_check import (
    build_manager, collect_known_cids, run_manager_check, seed_known_orders,
)
from exchange_base import ExchangeTimeout as ExchangeTimeoutForTest
from test_okx_demo_exchange import CREDS, FakeOKX, make_exchange, order_item, ok, top_error


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.store_path = self.dir / "manager_check.jsonl"
        self.evidence_path = self.dir / "evidence_2c_manager_test.jsonl"
        self.exchange, self.server = make_exchange()
        self.secrets = list(CREDS.values())
        self.evidence = Evidence(self.evidence_path, self.secrets)
        self.lines = []

    def run_it(self, **kwargs):
        # 证据文件放在单独的子目录，避免被"历史证据收集"读到。
        return run_manager_check(
            self.exchange, self.store_path, self.evidence, self.evidence_dir,
            self.secrets, sleep=lambda s: None, say=self.lines.append, **kwargs)

    @property
    def evidence_dir(self):
        folder = self.dir / "history"
        folder.mkdir(exist_ok=True)
        return folder

    def write_history(self, *cids):
        path = self.evidence_dir / "evidence_2c_20260101_000000.jsonl"
        path.write_text("\n".join(json.dumps({"step": "x", "cid": c}) for c in cids),
                        encoding="utf-8")

    def add_canceled(self, cid, **overrides):
        item = order_item(clOrdId=cid, ordId="H" + cid, state="canceled", sz="0.00002")
        item.update(overrides)
        self.server.orders[cid] = item

    def item(self, step):
        return [i for i in self.evidence.items if i["step"] == step][-1]

    def steps(self):
        return [i["step"] for i in self.evidence.items]

    def live_orders(self):
        return [o for o in self.server.orders.values()
                if o["state"] in ("live", "partially_filled")]


class TestCollectKnownCids(Base):
    def test_collects_unique_valid_cids_and_ignores_garbage(self):
        path = self.evidence_dir / "evidence_2c_a.jsonl"
        path.write_text("\n".join([
            json.dumps({"cid": "AAA111"}), json.dumps({"cid": "AAA111"}),
            json.dumps({"cid": "bad id!"}), json.dumps({"cid": 5}),
            "not json", json.dumps(["list"]), json.dumps({"other": 1}),
        ]), encoding="utf-8")
        (self.evidence_dir / "unrelated.jsonl").write_text(
            json.dumps({"cid": "SHOULDNOTAPPEAR"}), encoding="utf-8")
        self.assertEqual(collect_known_cids(self.evidence_dir), ["AAA111"])


class TestSeeding(Base):
    def test_canceled_zero_fill_orders_are_seeded_with_baseline(self):
        self.add_canceled("OLD001")
        store = OrderStore(self.store_path)
        seeded, problems = seed_known_orders(store, self.exchange, ["OLD001", "GONE01"],
                                             0.5, 1_700_000_000_000)
        self.assertEqual((seeded, problems), (["OLD001"], []))
        orders, baseline, _ = store.load()
        self.assertEqual(orders["OLD001"]["state"], "CANCELED")
        self.assertEqual(baseline["base"], 0.5)
        self.assertNotIn("GONE01", orders)

    def test_filled_and_partially_filled_then_canceled_history_is_seeded_as_in_baseline(self):
        self.add_canceled("OLD001")
        self.add_canceled("OLD002", state="filled", accFillSz="0.00002",
                          fee="-0.00000002", feeCcy="BTC", avgPx="99000")
        self.add_canceled("OLD004", accFillSz="0.00001", fee="-0.00000001",
                          feeCcy="BTC", avgPx="99000")      # 撤销前部分成交
        store = OrderStore(self.store_path)
        seeded, problems = seed_known_orders(
            store, self.exchange, ["OLD001", "OLD002", "OLD004"], 1.5, 1)
        self.assertEqual((seeded, problems), (["OLD001", "OLD002", "OLD004"], []))
        orders, baseline, _ = store.load()
        self.assertEqual(baseline["base"], 1.5)
        old2 = orders["OLD002"]
        self.assertEqual(old2["state"], "FILLED")
        self.assertEqual(old2["filled_size"], 0.0)          # 已计入基线，不重复计账
        self.assertEqual(old2["fee"], 0.0)
        self.assertAlmostEqual(old2["historic_filled_size"], 0.00002)
        self.assertAlmostEqual(old2["historic_fee"], 0.00000002)
        self.assertEqual(old2["historic_fee_ccy"], "BTC")
        self.assertEqual(orders["OLD004"]["state"], "CANCELED")
        self.assertAlmostEqual(orders["OLD004"]["historic_filled_size"], 0.00001)

    def test_open_or_unknown_history_is_refused_and_nothing_written(self):
        self.add_canceled("OLD003", state="live")
        self.add_canceled("OLD005", state="partially_filled", accFillSz="0.000005",
                          fee="-0.000000005", feeCcy="BTC", avgPx="99000")
        self.add_canceled("OLD006", state="mmp_canceled")
        self.add_canceled("OLD007", state="some_new_state")
        store = OrderStore(self.store_path)
        seeded, problems = seed_known_orders(
            store, self.exchange, ["OLD003", "OLD005", "OLD006", "OLD007"], 0.0, 1)
        self.assertEqual(seeded, [])
        self.assertEqual(len(problems), 4)
        self.assertFalse(self.store_path.exists())

    def test_query_failure_is_a_problem(self):
        self.server.overrides[("GET", "/api/v5/trade/order")] = [top_error("50119", "x")]
        seeded, problems = seed_known_orders(
            OrderStore(self.store_path), self.exchange, ["OLD001"], 0.0, 1)
        self.assertEqual(seeded, [])
        self.assertEqual(len(problems), 1)


class TestDryRun(Base):
    def test_dry_run_reconciles_and_places_nothing(self):
        summary = self.run_it(send_orders=False)
        self.assertTrue(summary["completed"])
        self.assertEqual(self.server.posts(), [])
        self.assertTrue(self.item("reconcile_1")["ok"])

    def test_dry_run_with_seeded_history_reconciles(self):
        self.add_canceled("OLD001")
        self.write_history("OLD001")
        summary = self.run_it(send_orders=False)
        self.assertTrue(summary["completed"], self.lines)
        self.assertEqual(self.item("seed")["seeded"], 1)

    def test_unknown_order_in_history_stops_before_any_order(self):
        self.add_canceled("MANUAL1")                  # 不在任何证据文件里
        summary = self.run_it(send_orders=True)
        self.assertFalse(summary["completed"])
        self.assertIn("对账未通过", summary["stopped"])
        self.assertTrue(any("FOREIGN_ORDER:MANUAL1" in i
                            for i in self.item("reconcile_1")["issues"]))
        self.assertEqual(self.server.posts(), [])

    def test_seeded_filled_history_reconciles_even_though_balance_includes_the_fill(self):
        # 旧订单已成交：余额里已经含有它的成交和手续费，基线按此刻余额取，对账应当一致。
        self.server.balance_details = [
            {"ccy": "BTC", "cashBal": "1.00001998"}, {"ccy": "USDT", "cashBal": "100000"}]
        self.add_canceled("OLD002", state="filled", accFillSz="0.00002",
                          fee="-0.00000002", feeCcy="BTC", avgPx="99000")
        self.write_history("OLD002")
        summary = self.run_it(send_orders=False)
        self.assertTrue(summary["completed"], self.lines)
        self.assertTrue(self.item("reconcile_1")["ok"])

    def test_unseedable_history_stops_before_reconcile(self):
        self.add_canceled("OLD002", state="live")
        self.write_history("OLD002")
        summary = self.run_it(send_orders=True)
        self.assertFalse(summary["completed"])
        self.assertNotIn("reconcile_1", self.steps())
        self.assertEqual(self.server.posts(), [])


class TestFullFlow(Base):
    def test_place_restart_cancel_restart(self):
        self.add_canceled("OLD001")
        self.write_history("OLD001")
        summary = self.run_it(send_orders=True)
        self.assertTrue(summary["completed"], self.lines)
        self.assertEqual(len(self.server.posts()), 1)
        self.assertEqual(self.item("place")["state"], "LIVE")
        self.assertTrue(self.item("reconcile_after_restart")["ok"])
        self.assertEqual(self.item("reconcile_after_restart")["state"], "LIVE")
        self.assertEqual(self.item("cancel")["state"], "CANCELED")
        self.assertTrue(self.item("reconcile_final")["ok"])
        self.assertEqual(self.live_orders(), [])

    def test_order_uses_planned_price_and_size(self):
        self.run_it(send_orders=True)
        body = json.loads(self.server.posts()[0]["body"])
        self.assertEqual(body["px"], "98000.4")        # 100000.5 * 0.98 向下取整到 0.1
        self.assertEqual(body["sz"], "0.00001")
        self.assertEqual(body["ordType"], "limit")
        self.assertEqual(body["tdMode"], "cash")

    def test_existing_btc_holdings_do_not_block_the_test_order(self):
        self.server.balance_details = [
            {"ccy": "BTC", "cashBal": "1.5"}, {"ccy": "USDT", "cashBal": "100000"}]
        summary = self.run_it(send_orders=True)
        self.assertTrue(summary["completed"], self.lines)
        self.assertEqual(len(self.server.posts()), 1)

    def test_every_request_is_marked_simulated(self):
        self.run_it(send_orders=True)
        for request in self.server.requests:
            self.assertEqual(request["headers"].get("x-simulated-trading"), "1")

    def test_failure_after_place_cleans_up_our_live_order(self):
        original = self.server._handle
        state = {"added": False}

        def sabotage(method, path, record):
            result = original(method, path, record)
            if method == "POST" and path == "/api/v5/trade/order" and not state["added"]:
                state["added"] = True
                self.server.orders["MANUAL9"] = order_item(clOrdId="MANUAL9", ordId="777")
            return result

        self.server._handle = sabotage
        summary = self.run_it(send_orders=True)
        self.assertFalse(summary["completed"])
        self.assertIn("重启后对账未通过", summary["stopped"])
        ours = [o for cid, o in self.server.orders.items() if cid != "MANUAL9"]
        self.assertTrue(all(o["state"] == "canceled" for o in ours))
        self.assertIn("cleanup_cancel_sent", self.steps())
        self.assertEqual(self.server.orders["MANUAL9"]["state"], "live")   # 不碰别人的单

    def test_ack_not_confirmed_live_stops_and_cleans_up(self):
        not_found = (200, json.dumps({"code": "51603", "msg": "x", "data": []}))
        self.server.overrides[("GET", "/api/v5/trade/order")] = [not_found] * 6
        summary = self.run_it(send_orders=True)
        self.assertFalse(summary["completed"])
        self.assertEqual(len(self.server.posts()), 1)
        self.assertEqual(self.live_orders(), [])

    def test_rejected_order_stops_without_retry(self):
        self.server.overrides[("POST", "/api/v5/trade/order")] = [
            top_error("1", "failed", [{"sCode": "66001", "sMsg": "example"}])]
        summary = self.run_it(send_orders=True)
        self.assertFalse(summary["completed"])
        self.assertEqual(len(self.server.posts()), 1)
        self.assertEqual(self.item("place")["state"], "REJECTED")


class TestCleanupIntegration(Base):
    def hide_after_place(self, **flags):
        """下单受理之后，让订单详情查询出问题。"""
        original = self.server._handle

        def sabotage(method, path, record):
            result = original(method, path, record)
            if method == "POST" and path == "/api/v5/trade/order":
                for name, value in flags.items():
                    setattr(self.server, name, value)
            return result

        self.server._handle = sabotage

    def test_clean_run_reports_cleanup_ok(self):
        summary = self.run_it(send_orders=True)
        self.assertTrue(summary["completed"])
        self.assertTrue(summary["cleanup_ok"])

    def test_orders_invisible_to_queries_are_still_canceled_but_not_reported_clean(self):
        self.hide_after_place(hide_orders=True)
        summary = self.run_it(send_orders=True)
        self.assertFalse(summary["completed"])
        self.assertFalse(summary["cleanup_ok"])
        self.assertEqual(len(self.server.posts()), 1)               # 没有重复下单
        self.assertEqual(self.live_orders(), [])                    # 按 clOrdId 撤掉了
        self.assertIn("cleanup_unverified", self.steps())
        self.assertTrue(any("警告" in line for line in self.lines))

    def test_query_network_failure_after_place_cancels_without_resend(self):
        self.hide_after_place(raise_always={
            ("GET", "/api/v5/trade/order"): ExchangeTimeoutForTest("net")})
        summary = self.run_it(send_orders=True)
        self.assertFalse(summary["completed"])
        self.assertFalse(summary["cleanup_ok"])
        self.assertEqual(len(self.server.posts()), 1)
        self.assertEqual(self.live_orders(), [])
        self.assertIn("cleanup_cancel_sent", self.steps())

    def test_cancel_that_does_not_take_effect_is_an_alarm_not_a_pass(self):
        not_found = (200, json.dumps({"code": "51603", "msg": "x", "data": []}))
        self.server.overrides[("GET", "/api/v5/trade/order")] = [not_found] * 6
        self.server.cancel_mode = "ignore"
        summary = self.run_it(send_orders=True)
        self.assertFalse(summary["completed"])
        self.assertFalse(summary["cleanup_ok"])
        self.assertEqual(len(self.live_orders()), 1)                # 如实报告：仍有挂单
        self.assertTrue(any("警告" in line and "手动撤销" in line for line in self.lines))

    def test_seeded_history_is_never_queried_again_or_canceled(self):
        self.add_canceled("OLD001")
        self.write_history("OLD001")
        self.hide_after_place(hide_orders=False)
        summary = self.run_it(send_orders=True)
        self.assertTrue(summary["completed"])
        old_queries = [r for r in self.server.requests
                       if r["method"] == "GET" and r["path"] == "/api/v5/trade/order"
                       and r["query"]["clOrdId"] == ["OLD001"]]
        self.assertEqual(len(old_queries), 1)                       # 只有登记时查过一次
        for request in self.server.posts("/api/v5/trade/cancel-order"):
            self.assertNotIn("OLD001", request["body"])

    def test_after_cleanup_operator_clear_and_reconcile_match_exchange(self):
        not_found = (200, json.dumps({"code": "51603", "msg": "x", "data": []}))
        self.server.overrides[("GET", "/api/v5/trade/order")] = [not_found] * 6
        summary = self.run_it(send_orders=True)
        self.assertFalse(summary["completed"])
        self.assertTrue(summary["cleanup_ok"])
        limits = RiskLimits(max_order_notional=1e9, max_position_notional=1e12,
                            max_daily_loss=1.0)
        manager = build_manager(self.exchange, self.store_path, self.secrets, limits,
                                lambda s: None)
        # 本次运行留下了停机记录，必须人工解除；重跑对账不会自动解除。
        self.assertFalse(manager.reconcile()["ok"])
        manager.operator_clear_halt("verified on exchange: order canceled")
        report = manager.reconcile()
        self.assertTrue(report["ok"], report)
        cids = [c for c in manager.orders if not c.startswith("OLD")]
        self.assertEqual(len(cids), 1)
        self.assertEqual(manager.orders[cids[0]]["state"], CANCELED)
        self.assertEqual(self.server.orders[cids[0]]["state"], "canceled")
        self.assertIsNone(manager.can_trade_reason())


class TestCleanupGuards(Base):
    def test_failed_cleanup_overrides_an_otherwise_completed_run(self):
        import run_2c_manager_check as module
        original = module._cleanup
        module._cleanup = lambda *a, **k: {
            "ok": False, "verified": [], "filled": [],
            "unverified": [{"cid": "X", "reason": "test"}]}
        self.addCleanup(setattr, module, "_cleanup", original)
        summary = self.run_it(send_orders=True)
        self.assertFalse(summary["completed"])
        self.assertFalse(summary["cleanup_ok"])
        self.assertIn("清理", summary["stopped"])

    def test_seeded_orders_are_excluded_even_if_the_store_says_open(self):
        import run_2c_manager_check as module
        store = OrderStore(self.store_path)
        store.append({"type": "order", "cid": "OLD001", "state": "LIVE", "side": "buy",
                      "size": 0.00001, "filled_size": 0.0, "updated_ms": 1})
        store.append({"type": "order", "cid": "NEW001", "state": "SENT", "side": "buy",
                      "size": 0.00001, "filled_size": 0.0, "updated_ms": 2})
        self.add_canceled("OLD001", state="live")
        self.server.orders["NEW001"] = order_item(clOrdId="NEW001", ordId="N1")
        module._cleanup(self.exchange, self.store_path, self.evidence, lambda *_: None,
                        ["OLD001"], lambda s: None)
        self.assertEqual(self.server.orders["OLD001"]["state"], "live")      # 没动历史订单
        self.assertEqual(self.server.orders["NEW001"]["state"], "canceled")  # 清理了本次订单

    def test_intent_only_orders_are_not_canceled(self):
        import run_2c_manager_check as module
        OrderStore(self.store_path).append({
            "type": "order", "cid": "NEVER1", "state": "INTENT", "side": "buy",
            "size": 0.00001, "filled_size": 0.0, "updated_ms": 1})
        result = module._cleanup(self.exchange, self.store_path, self.evidence,
                                 lambda *_: None, [], lambda s: None)
        self.assertTrue(result["ok"])
        self.assertEqual(self.server.posts("/api/v5/trade/cancel-order"), [])

    def test_unreadable_store_is_reported_not_ignored(self):
        import run_2c_manager_check as module
        self.store_path.write_text("this is not json\n", encoding="utf-8")
        result = module._cleanup(self.exchange, self.store_path, self.evidence,
                                 lambda *_: None, [], lambda s: None)
        self.assertFalse(result["ok"])
        self.assertIn("cleanup_exception", [i["step"] for i in self.evidence.items])


class TestEvidenceHygiene(Base):
    def test_no_secrets_or_balances_in_evidence_or_store(self):
        leak = " ".join(CREDS.values())
        self.server.overrides[("POST", "/api/v5/trade/order")] = [
            top_error("1", "failed", [{"sCode": "66002", "sMsg": leak}])]
        self.run_it(send_orders=True)
        for path in (self.evidence_path, self.store_path):
            text = path.read_text(encoding="utf-8")
            for value in CREDS.values():
                self.assertNotIn(value, text)
        text = self.evidence_path.read_text(encoding="utf-8")
        self.assertNotIn("100000\"", text)
        for line in text.splitlines():
            json.loads(line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
