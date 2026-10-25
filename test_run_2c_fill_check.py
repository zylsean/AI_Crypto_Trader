"""run_2c_fill_check 的假网络测试：没有真实请求，没有真实凭证。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from order_store import OrderStore
from run_2c_demo import Evidence
from run_2c_fill_check import run_fill_check
from run_2c_manager_check import run_manager_check
from test_okx_demo_exchange import CREDS, make_exchange


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.history = self.dir / "history"
        self.history.mkdir()
        self.exchange, self.server = make_exchange()
        self.server.min_sz = "0.00002"                # 和真实模拟盘一致
        self.server.balance_details = [
            {"ccy": "BTC", "cashBal": "1.01016007812"},
            {"ccy": "USDT", "cashBal": "123456.789"}]
        self.secrets = list(CREDS.values())
        self.lines = []
        self.counter = 0

    def fresh(self):
        """每次运行用独立的订单记录和证据文件（证据文件放进历史目录，模拟真实目录）。"""
        self.counter += 1
        store = self.dir / f"store_{self.counter}.jsonl"
        evidence = Evidence(self.history / f"evidence_2c_fill_{self.counter}.jsonl",
                            self.secrets)
        return store, evidence

    def run_it(self, fill=True, **kwargs):
        store, evidence = self.fresh()
        self.store_path, self.evidence = store, evidence
        return run_fill_check(self.exchange, store, evidence, self.history, self.secrets,
                              fill_orders=fill, sleep=lambda s: None,
                              say=self.lines.append, **kwargs)

    def item(self, step):
        return [i for i in self.evidence.items if i["step"] == step][-1]

    def steps(self):
        return [i["step"] for i in self.evidence.items]

    def live_orders(self):
        return [o for o in self.server.orders.values()
                if o["state"] in ("live", "partially_filled")]


class TestDryRun(Base):
    def test_dry_run_places_nothing_and_plans_a_marketable_limit(self):
        summary = self.run_it(fill=False)
        self.assertTrue(summary["completed"], self.lines)
        self.assertEqual(self.server.posts(), [])
        plan = self.item("plan")
        self.assertEqual(plan["px"], "100101.0")       # 卖一 100001.0 x 1.001 向下取整到 0.1
        self.assertEqual(plan["sz"], "0.00002")
        self.assertTrue(plan["ok"])
        self.assertTrue(self.item("reconcile_1")["ok"])

    def test_notional_cap_blocks_before_any_order(self):
        self.server.min_sz = "1"                       # 名义金额约 10 万 USDT
        summary = self.run_it(fill=True)
        self.assertFalse(summary["completed"])
        self.assertIn("上限", summary["stopped"])
        self.assertFalse(self.item("plan")["ok"])
        self.assertEqual(self.server.posts(), [])


class TestFillFlow(Base):
    def setUp(self):
        super().setUp()
        self.server.fill_on_place = True

    def test_filled_order_fee_balance_and_usdt_all_reconcile(self):
        summary = self.run_it()
        self.assertTrue(summary["completed"], self.lines)
        self.assertTrue(summary["cleanup_ok"])
        self.assertEqual(len(self.server.posts()), 1)
        self.assertEqual(self.server.posts("/api/v5/trade/cancel-order"), [])
        place = self.item("place")
        self.assertEqual(place["state"], "FILLED")
        self.assertAlmostEqual(place["accFillSz"], 0.00002)
        self.assertAlmostEqual(place["fee"], 0.00000002)       # 扣费统一成正数
        self.assertEqual(place["fee_ccy"], "BTC")
        self.assertAlmostEqual(place["avg_price"], 100001.0)
        after = self.item("reconcile_after_fill")
        self.assertTrue(after["ok"])
        self.assertLess(abs(after["btc_diff"]), 1e-9)
        self.assertTrue(self.item("usdt_check")["ok"])
        self.assertTrue(self.item("reconcile_final")["ok"])

    def test_order_record_keeps_fill_and_fee(self):
        self.run_it()
        orders, baseline, halts = OrderStore(self.store_path).load()
        record = [o for o in orders.values()][0]
        self.assertEqual(record["state"], "FILLED")
        self.assertAlmostEqual(record["filled_size"], 0.00002)
        self.assertAlmostEqual(record["fee"], 0.00000002)
        self.assertEqual(record["fee_ccy"], "BTC")
        self.assertEqual(halts, [])

    def test_fee_missing_from_balance_is_detected_by_reconcile(self):
        self.server.skip_fee_in_balance = True         # 交易所没扣手续费，但订单里写了
        summary = self.run_it()
        self.assertFalse(summary["completed"])
        self.assertIn("成交后对账未通过", summary["stopped"])
        after = self.item("reconcile_after_fill")
        self.assertFalse(after["ok"])
        self.assertTrue(any("BALANCE_MISMATCH" in i for i in after["issues"]))

    def test_usdt_not_moving_is_detected(self):
        self.server.skip_usdt_in_balance = True
        summary = self.run_it()
        self.assertFalse(summary["completed"])
        self.assertIn("USDT", summary["stopped"])
        self.assertFalse(self.item("usdt_check")["ok"])

    def test_second_run_after_a_fill_still_reconciles(self):
        first = self.run_it()
        self.assertTrue(first["completed"], self.lines)
        # 同一个目录里再跑一次（证据文件还在）：已成交的旧订单要能被登记，对账一致。
        self.server.fill_on_place = False
        second = self.run_it(fill=False)
        self.assertTrue(second["completed"], self.lines)
        self.assertEqual(self.item("seed")["seeded"], 1)
        self.assertTrue(self.item("reconcile_1")["ok"])

    def test_manager_check_still_works_after_a_fill(self):
        self.run_it()
        self.server.fill_on_place = False
        store = self.dir / "mgr.jsonl"
        evidence = Evidence(self.history / "evidence_2c_manager_x.jsonl", self.secrets)
        summary = run_manager_check(self.exchange, store, evidence, self.history,
                                    self.secrets, send_orders=True,
                                    sleep=lambda s: None, say=self.lines.append)
        self.assertTrue(summary["completed"], self.lines)
        self.assertTrue(summary["cleanup_ok"])


class TestNotFilled(Base):
    def test_not_filled_order_is_canceled_and_reported_without_retry(self):
        self.server.fill_on_place = False              # 限价单挂在盘口，没有成交
        summary = self.run_it()
        self.assertFalse(summary["completed"])
        self.assertIn("没有立刻成交", summary["stopped"])
        self.assertEqual(len(self.server.posts()), 1)  # 不会反复下单
        self.assertEqual(self.live_orders(), [])
        self.assertTrue(summary["cleanup_ok"])
        self.assertIn("cleanup_cancel_sent", self.steps())


class TestGuards(Base):
    def test_failed_cleanup_overrides_completed(self):
        import run_2c_fill_check as module
        original = module._cleanup
        module._cleanup = lambda *a, **k: {
            "ok": False, "verified": [], "filled": [],
            "unverified": [{"cid": "X", "reason": "test"}]}
        self.addCleanup(setattr, module, "_cleanup", original)
        self.server.fill_on_place = True
        summary = self.run_it()
        self.assertFalse(summary["completed"])
        self.assertFalse(summary["cleanup_ok"])

    def test_every_request_is_marked_simulated(self):
        self.server.fill_on_place = True
        self.run_it()
        for request in self.server.requests:
            self.assertEqual(request["headers"].get("x-simulated-trading"), "1")

    def test_no_absolute_balances_or_secrets_in_files(self):
        self.server.fill_on_place = True
        self.run_it()
        for path in (self.evidence.path, self.store_path):
            text = path.read_text(encoding="utf-8")
            for value in CREDS.values():
                self.assertNotIn(value, text)
            for line in text.splitlines():
                json.loads(line)
        evidence_text = self.evidence.path.read_text(encoding="utf-8")
        self.assertNotIn("123456", evidence_text)        # USDT 余额绝对数字
        self.assertNotIn("1.0101600", evidence_text)     # BTC 余额绝对数字


if __name__ == "__main__":
    unittest.main(verbosity=2)

