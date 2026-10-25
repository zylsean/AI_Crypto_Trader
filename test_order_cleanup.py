"""order_cleanup 的假网络测试：没有真实请求，没有真实凭证。"""

import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from exchange_base import ExchangeTimeout
from order_cleanup import cleanup_orders
from test_okx_demo_exchange import make_exchange


class Recorder:
    def __init__(self):
        self.items = []

    def add(self, step, **info):
        self.items.append({"step": step, **info})

    def steps(self):
        return [i["step"] for i in self.items]

    def of(self, step):
        return [i for i in self.items if i["step"] == step]


class Base(unittest.TestCase):
    def setUp(self):
        self.exchange, self.server = make_exchange()
        self.evidence = Recorder()
        self.lines = []
        self.sleeps = []

    def place(self, cid="CLN0001"):
        self.exchange.place_order(cid, "buy", 0.00002, "limit", 99000.0)
        return cid

    def clean(self, cids, **kwargs):
        return cleanup_orders(
            self.exchange, cids, self.evidence, say=self.lines.append,
            sleep=self.sleeps.append, **kwargs)

    def cancel_posts(self):
        return self.server.posts("/api/v5/trade/cancel-order")

    def order_posts(self):
        return self.server.posts("/api/v5/trade/order")

    def order_queries(self, cid=None):
        found = [r for r in self.server.requests
                 if r["method"] == "GET" and r["path"] == "/api/v5/trade/order"]
        if cid:
            found = [r for r in found if r["query"]["clOrdId"] == [cid]]
        return found


class TestVerifiedCleanup(Base):
    def test_open_order_is_canceled_and_verified(self):
        cid = self.place()
        result = self.clean([cid])
        self.assertTrue(result["ok"])
        self.assertEqual(result["verified"], [cid])
        self.assertEqual(self.server.orders[cid]["state"], "canceled")
        self.assertIn("cleanup_cancel_sent", self.evidence.steps())
        verified = self.evidence.of("cleanup_verified")[0]
        self.assertEqual(verified["state"], "canceled")
        self.assertFalse(verified["had_fill"])
        self.assertTrue(self.evidence.of("cleanup_summary")[0]["ok"])

    def test_already_terminal_order_is_verified_without_cancel(self):
        cid = self.place()
        self.server.orders[cid]["state"] = "canceled"
        result = self.clean([cid])
        self.assertTrue(result["ok"])
        self.assertEqual(self.cancel_posts(), [])

    def test_empty_list_is_ok(self):
        result = self.clean([])
        self.assertTrue(result["ok"])

    def test_cleanup_twice_does_not_cancel_twice(self):
        cid = self.place()
        self.clean([cid])
        count = len(self.cancel_posts())
        again = self.clean([cid])
        self.assertTrue(again["ok"])
        self.assertEqual(len(self.cancel_posts()), count)

    def test_cleanup_never_places_orders(self):
        cid = self.place()
        before = len(self.order_posts())
        self.server.hide_orders = True
        self.clean([cid])
        self.server.cancel_mode = "ignore"
        self.clean([cid])
        self.assertEqual(len(self.order_posts()), before)


class TestQueryCannotProveAbsence(Base):
    def test_not_found_does_not_skip_cancel(self):
        cid = self.place()
        self.server.hide_orders = True      # 订单真实存在，但查询一直说不存在
        result = self.clean([cid])
        self.assertGreaterEqual(len(self.cancel_posts()), 1)
        self.assertEqual(self.server.orders[cid]["state"], "canceled")   # 按 clOrdId 撤到了
        # 但我们始终没看到终态，所以不能宣称清理成功。
        self.assertFalse(result["ok"])
        self.assertEqual(result["verified"], [])
        self.assertIn("一直查不到", result["unverified"][0]["reason"])
        self.assertTrue(any("警告" in line and cid in line for line in self.lines))
        self.assertEqual(self.evidence.of("cleanup_summary")[0]["unverified"], [cid])

    def test_query_error_does_not_skip_cancel_or_resend(self):
        cid = self.place()
        before = len(self.order_posts())
        self.server.raise_always[("GET", "/api/v5/trade/order")] = ExchangeTimeout("net")
        result = self.clean([cid])
        self.assertGreaterEqual(len(self.cancel_posts()), 1)
        self.assertEqual(self.server.orders[cid]["state"], "canceled")
        self.assertFalse(result["ok"])
        self.assertIn("查询持续失败", result["unverified"][0]["reason"])
        self.assertEqual(len(self.order_posts()), before)                 # 没有重发
        self.assertTrue(any(i.get("result") == "error"
                            for i in self.evidence.of("cleanup_query")))

    def test_not_found_for_the_first_queries_then_visible_is_verified(self):
        cid = self.place()
        not_found = (200, '{"code":"51603","msg":"x","data":[]}')
        self.server.overrides[("GET", "/api/v5/trade/order")] = [not_found] * 3
        result = self.clean([cid])
        self.assertTrue(result["ok"])
        self.assertEqual(self.server.orders[cid]["state"], "canceled")

    def test_queries_are_bounded_and_paced(self):
        cid = self.place()
        self.server.hide_orders = True
        self.clean([cid], poll_tries=5, poll_interval=1.0, max_cancel_attempts=2)
        self.assertLessEqual(len(self.order_queries(cid)), 2 * (1 + 5))
        self.assertLessEqual(len(self.cancel_posts()), 2)
        self.assertTrue(self.sleeps)
        self.assertTrue(all(s == 1.0 for s in self.sleeps))


class TestCancelDoesNotMeanCanceled(Base):
    def test_cancel_accepted_but_order_stays_live_is_a_failure(self):
        cid = self.place()
        self.server.cancel_mode = "ignore"
        result = self.clean([cid])
        self.assertFalse(result["ok"])
        self.assertIn("仍为 live", result["unverified"][0]["reason"])
        self.assertEqual(self.server.orders[cid]["state"], "live")
        self.assertEqual(len(self.cancel_posts()), 2)         # 有限次数的重试
        self.assertTrue(any("警告" in line for line in self.lines))

    def test_cancel_then_filled_is_reported_with_fill_not_as_clean_cancel(self):
        cid = self.place()
        self.server.cancel_mode = "fill"
        result = self.clean([cid])
        self.assertFalse(result["ok"])
        self.assertEqual(result["verified"], [])
        self.assertEqual(result["filled"][0]["cid"], cid)
        self.assertGreater(result["filled"][0]["filled"], 0)
        verified = self.evidence.of("cleanup_verified")[0]
        self.assertTrue(verified["had_fill"])
        self.assertEqual(verified["state"], "filled")
        self.assertTrue(any("已有成交" in line for line in self.lines))

    def test_partial_fill_then_canceled_records_cumulative_fill(self):
        cid = self.place()
        self.server.orders[cid].update(state="partially_filled", accFillSz="0.000005",
                                       fee="-0.000000005", feeCcy="BTC", avgPx="99000")
        result = self.clean([cid])
        self.assertFalse(result["ok"])                      # 有成交，需要人看
        self.assertEqual(result["filled"][0]["state"], "canceled")
        self.assertAlmostEqual(result["filled"][0]["filled"], 0.000005)
        self.assertEqual(self.evidence.of("cleanup_verified")[0]["accFillSz"], 0.000005)

    def test_partially_filled_that_stays_open_keeps_polling_then_alarms(self):
        cid = self.place()
        self.server.orders[cid].update(state="partially_filled", accFillSz="0.000005",
                                       fee="-0.000000005", feeCcy="BTC", avgPx="99000")
        self.server.cancel_mode = "ignore"
        result = self.clean([cid])
        self.assertFalse(result["ok"])
        self.assertIn("partially_filled", result["unverified"][0]["reason"])


class TestCancelBusinessErrors(Base):
    def test_cancel_rejected_once_then_retry_succeeds(self):
        cid = self.place()
        self.server.cancel_rejects = 1
        result = self.clean([cid])
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.cancel_posts()), 2)
        failed = self.evidence.of("cleanup_cancel_failed")
        self.assertEqual(failed[0]["s_code"], "99003")

    def test_cancel_always_rejected_and_order_live_is_a_failure(self):
        cid = self.place()
        self.server.cancel_rejects = 99
        result = self.clean([cid])
        self.assertFalse(result["ok"])
        self.assertEqual(self.server.orders[cid]["state"], "live")
        self.assertIn("仍为 live", result["unverified"][0]["reason"])
        self.assertEqual(len(self.evidence.of("cleanup_cancel_failed")), 2)

    def test_cancel_rejected_because_order_already_canceled_elsewhere(self):
        cid = self.place()
        self.server.orders[cid]["state"] = "canceled"
        self.server.cancel_rejects = 99                     # 即使撤单会被拒也不该去撤
        result = self.clean([cid])
        self.assertTrue(result["ok"])
        self.assertEqual(self.cancel_posts(), [])

    def test_cancel_timeout_is_unknown_so_keep_verifying(self):
        cid = self.place()
        self.server.raise_once[("POST", "/api/v5/trade/cancel-order")] = ExchangeTimeout("net")
        self.server.execute_then_raise = False
        result = self.clean([cid])
        # 第一次撤单结果不明（订单没撤掉），第二轮重试成功并核实。
        self.assertTrue(result["ok"])
        self.assertEqual(self.server.orders[cid]["state"], "canceled")
        self.assertEqual(len(self.evidence.of("cleanup_cancel_failed")), 1)


class TestUnknownStatesAndExceptions(Base):
    def test_unknown_state_is_not_auto_handled(self):
        cid = self.place()
        self.server.orders[cid]["state"] = "some_new_state"
        result = self.clean([cid])
        self.assertFalse(result["ok"])
        self.assertIn("some_new_state", result["unverified"][0]["reason"])
        self.assertEqual(self.cancel_posts(), [])           # 看不懂的状态不动手
        self.assertEqual(self.server.orders[cid]["state"], "some_new_state")

    def test_unexpected_exception_is_recorded_and_other_orders_still_cleaned(self):
        first = self.place("CLN0001")
        second = self.place("CLN0002")
        original = self.exchange.get_order

        def flaky(cid):
            if cid == first:
                raise RuntimeError("boom")
            return original(cid)

        self.exchange.get_order = flaky
        result = self.clean([first, second])
        self.assertFalse(result["ok"])
        self.assertEqual(result["verified"], [second])
        self.assertEqual(result["unverified"][0]["cid"], first)
        self.assertEqual(self.evidence.of("cleanup_exception")[0]["error_type"], "RuntimeError")
        self.assertEqual(self.server.orders[second]["state"], "canceled")

    def test_multiple_orders_are_each_verified(self):
        ids = [self.place(f"CLN{i:04d}") for i in range(3)]
        result = self.clean(ids)
        self.assertTrue(result["ok"])
        self.assertEqual(result["verified"], ids)
        self.assertTrue(all(self.server.orders[c]["state"] == "canceled" for c in ids))


if __name__ == "__main__":
    unittest.main(verbosity=2)

