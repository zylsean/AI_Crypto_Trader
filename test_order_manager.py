import itertools
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from exchange_base import ExchangeRejected
from fake_exchange import FakeExchange, SimulatedCrash
from order_manager import (
    ALLOWED, CANCELED, FILLED, INTENT, LIVE, PARTIAL, REJECTED, SENT,
    TERMINAL, UNKNOWN, IllegalTransition, OrderManager, TradingHalted,
    write_halt_cleared_marker,
)
from order_store import OrderStore
from risk_guard import RiskGuard, RiskLimits, RiskRejected

NAN = float("nan")
API_KEY = "KEYVALUE-abc-123456"
SECRET = "SECRETVALUE-xyz-987654"
PASSPHRASE = "PassPhrase#2026"

MKT = "market"
LMT = "limit"


def new_risk():
    return RiskGuard(RiskLimits(
        max_order_notional=5000.0, max_position_notional=20000.0,
        max_daily_loss=1000.0,
    ))


class ConfirmFlakyExchange(FakeExchange):
    """下单本身成功，但随后的订单详情查询会失败或返回 None。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.get_order_errors = 0   # 接下来这么多次 get_order 抛异常
        self.get_order_nones = 0    # 再接下来这么多次 get_order 返回 None
        self.get_order_calls = 0

    def get_order(self, cid):
        self.get_order_calls += 1
        if self.get_order_errors > 0:
            self.get_order_errors -= 1
            raise ConnectionError("simulated get_order failure")
        if self.get_order_nones > 0:
            self.get_order_nones -= 1
            return None
        return super().get_order(cid)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.store_path = self.dir / "orders.jsonl"
        self.stop_path = self.dir / "STOP"
        self.exchange = FakeExchange(
            balances={"BTC": 0.0, "USDT": 100000.0}, market_price=100.0
        )
        self._clock = itertools.count(1_700_000_000_000)

    def manager(self, prefix="T", exchange=None, risk=None, **kwargs):
        ids = (f"{prefix}{n:04d}" for n in itertools.count(1))
        return OrderManager(
            exchange or self.exchange, OrderStore(self.store_path),
            risk or new_risk(),
            clock=lambda: next(self._clock),
            id_factory=lambda: next(ids),
            sleep=lambda s: None,
            stop_flag_path=self.stop_path,
            **kwargs,
        )

    def ready(self, **kwargs):
        mgr = self.manager(**kwargs)
        report = mgr.reconcile()
        self.assertTrue(report["ok"], report)
        return mgr


class TestNormalFlow(Base):
    def test_01_market_buy_fills(self):
        mgr = self.ready()
        rec = mgr.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)
        self.assertEqual(rec["state"], FILLED)
        self.assertEqual(rec["filled_size"], 1.0)
        self.assertEqual(self.exchange.place_calls, 1)
        orders, baseline, _ = OrderStore(self.store_path).load()
        self.assertEqual(orders[rec["cid"]]["state"], FILLED)
        self.assertEqual(baseline["base"], 0.0)

    def test_02_partial_fills_then_filled(self):
        mgr = self.ready()
        rec = mgr.place_order("buy", 1.0, ref_price=100.0,
                              order_type=LMT, price=100.0)
        cid = rec["cid"]
        self.assertEqual(rec["state"], LIVE)
        self.exchange.fill(cid, 0.4)
        self.assertEqual(mgr.refresh(cid)["state"], PARTIAL)
        self.assertAlmostEqual(mgr.orders[cid]["filled_size"], 0.4)
        self.exchange.fill(cid, 0.3)
        mgr.refresh(cid)
        self.assertAlmostEqual(mgr.orders[cid]["filled_size"], 0.7)
        self.exchange.fill(cid, 0.3)
        self.assertEqual(mgr.refresh(cid)["state"], FILLED)
        self.assertAlmostEqual(mgr.orders[cid]["filled_size"], 1.0)

    def test_03_explicit_reject_not_resent(self):
        mgr = self.ready()
        self.exchange.set_next_place("reject")
        rec = mgr.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)
        self.assertEqual(rec["state"], REJECTED)
        self.assertEqual(self.exchange.place_calls, 1)
        self.assertIsNone(mgr.can_trade_reason())

    def test_buy_then_sell_round_trip_reconciles(self):
        mgr = self.ready()
        mgr.place_order("buy", 2.0, ref_price=100.0, order_type=MKT)
        mgr.place_order("sell", 1.5, ref_price=100.0, order_type=MKT)
        again = self.manager(prefix="R")
        self.assertTrue(again.reconcile()["ok"])

    def test_cancel_live_and_partial(self):
        mgr = self.ready()
        rec = mgr.place_order("buy", 1.0, 100.0, order_type=LMT, price=100.0)
        self.assertEqual(mgr.cancel_order(rec["cid"])["state"], CANCELED)
        rec2 = mgr.place_order("buy", 1.0, 100.0, order_type=LMT, price=100.0)
        self.exchange.fill(rec2["cid"], 0.25)
        mgr.refresh(rec2["cid"])
        done = mgr.cancel_order(rec2["cid"])
        self.assertEqual(done["state"], CANCELED)
        self.assertAlmostEqual(done["filled_size"], 0.25)
        self.assertTrue(self.manager(prefix="R").reconcile()["ok"])

    def test_open_order_blocks_new_orders(self):
        mgr = self.ready()
        rec = mgr.place_order("buy", 1.0, 100.0, order_type=LMT, price=100.0)
        with self.assertRaises(TradingHalted):
            mgr.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)
        mgr.cancel_order(rec["cid"])
        mgr.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)

    def test_not_reconciled_blocks_trading(self):
        mgr = self.manager()
        with self.assertRaises(TradingHalted):
            mgr.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)
        self.assertEqual(self.exchange.place_calls, 0)

    def test_invalid_inputs_raise_value_error(self):
        mgr = self.ready()
        for kwargs in (
            dict(side="hold", size=1.0, ref_price=100.0, order_type=MKT),
            dict(side="buy", size=NAN, ref_price=100.0, order_type=MKT),
            dict(side="buy", size=-1.0, ref_price=100.0, order_type=MKT),
            dict(side="buy", size=1.0, ref_price=float("inf"), order_type=MKT),
            dict(side="buy", size=1.0, ref_price=100.0, order_type=LMT),
            dict(side="buy", size=1.0, ref_price=100.0, order_type="stop"),
        ):
            with self.assertRaises(ValueError):
                mgr.place_order(**kwargs)
        self.assertEqual(self.exchange.place_calls, 0)


class TestExplicitOrderType(Base):
    """修改 1、2：必须显式指定订单类型，禁止默认市价单。"""

    def test_missing_order_type_rejected_before_any_request(self):
        mgr = self.ready()
        with self.assertRaises(ValueError) as ctx:
            mgr.place_order("buy", 1.0, ref_price=100.0)
        self.assertIn("order_type", str(ctx.exception))
        self.assertEqual(self.exchange.place_calls, 0)
        self.assertEqual(mgr.orders, {})

    def test_explicit_none_order_type_rejected(self):
        mgr = self.ready()
        with self.assertRaises(ValueError):
            mgr.place_order("buy", 1.0, ref_price=100.0, order_type=None)
        self.assertEqual(self.exchange.place_calls, 0)

    def test_empty_string_order_type_rejected(self):
        mgr = self.ready()
        with self.assertRaises(ValueError):
            mgr.place_order("buy", 1.0, ref_price=100.0, order_type="")
        self.assertEqual(self.exchange.place_calls, 0)

    def test_missing_order_type_checked_before_halt_state(self):
        # 输入错误应当先报参数问题；这里确认它不会被当成成功下单。
        mgr = self.manager()   # 未对账
        with self.assertRaises(ValueError):
            mgr.place_order("buy", 1.0, ref_price=100.0)
        self.assertEqual(self.exchange.place_calls, 0)


class TestOrderAckIsNotConfirmation(Base):
    """修改 3：下单接口成功只表示受理，必须再查订单详情确认。"""

    def test_confirmation_query_is_made_after_place(self):
        ex = ConfirmFlakyExchange(
            balances={"BTC": 0.0, "USDT": 100000.0}, market_price=100.0)
        mgr = self.ready(exchange=ex)
        before = ex.get_order_calls
        rec = mgr.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)
        self.assertEqual(rec["state"], FILLED)
        self.assertGreater(ex.get_order_calls, before)
        self.assertEqual(ex.place_calls, 1)

    def test_ack_without_confirmation_is_not_live(self):
        ex = ConfirmFlakyExchange(
            balances={"BTC": 0.0, "USDT": 100000.0}, market_price=100.0)
        mgr = self.ready(exchange=ex)
        ex.get_order_nones = 99          # 详情一直查不到
        rec = mgr.place_order("buy", 1.0, 100.0, order_type=LMT, price=100.0)
        self.assertEqual(rec["state"], UNKNOWN)
        self.assertNotEqual(rec["state"], LIVE)
        self.assertIn("UNRESOLVED_UNKNOWN", mgr.halted)
        self.assertEqual(ex.place_calls, 1)          # 没有重发
        with self.assertRaises(TradingHalted):
            mgr.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)
        self.assertEqual(ex.place_calls, 1)

    def test_confirmation_error_then_recovery_without_resend(self):
        ex = ConfirmFlakyExchange(
            balances={"BTC": 0.0, "USDT": 100000.0}, market_price=100.0)
        mgr = self.ready(exchange=ex)
        ex.get_order_errors = 2          # 确认查询失败 2 次，第 3 次成功
        rec = mgr.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)
        self.assertEqual(rec["state"], FILLED)
        self.assertIsNone(mgr.halted)
        self.assertEqual(ex.place_calls, 1)

    def test_confirmation_error_all_attempts_halts(self):
        ex = ConfirmFlakyExchange(
            balances={"BTC": 0.0, "USDT": 100000.0}, market_price=100.0)
        mgr = self.ready(exchange=ex, max_consecutive_errors=99)
        ex.get_order_errors = 99
        rec = mgr.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)
        self.assertEqual(rec["state"], UNKNOWN)
        self.assertIn("UNRESOLVED_UNKNOWN", mgr.halted)
        self.assertEqual(ex.place_calls, 1)

    def test_unknown_after_ack_survives_restart_and_blocks(self):
        ex = ConfirmFlakyExchange(
            balances={"BTC": 0.0, "USDT": 100000.0}, market_price=100.0)
        mgr = self.ready(exchange=ex)
        ex.get_order_nones = 99
        mgr.place_order("buy", 1.0, 100.0, order_type=LMT, price=100.0)
        again = self.manager(prefix="R", exchange=ex)
        self.assertIsNotNone(again.halted)
        with self.assertRaises(TradingHalted):
            again.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)
        self.assertEqual(ex.place_calls, 1)


class TestUncertainOutcomes(Base):
    def test_04_timeout_but_executed_resolves_without_duplicate(self):
        mgr = self.ready()
        self.exchange.set_next_place("timeout_executed")
        rec = mgr.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)
        self.assertEqual(rec["state"], FILLED)
        self.assertEqual(self.exchange.place_calls, 1)
        self.assertEqual(len(self.exchange.orders), 1)
        self.assertIsNone(mgr.halted)

    def test_05_timeout_not_executed_stays_unknown_and_halts(self):
        mgr = self.ready()
        self.exchange.set_next_place("timeout_not_executed")
        rec = mgr.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)
        self.assertEqual(rec["state"], UNKNOWN)
        self.assertEqual(self.exchange.place_calls, 1)       # 没有重发
        self.assertIn("UNRESOLVED_UNKNOWN", mgr.halted)
        with self.assertRaises(TradingHalted):
            mgr.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)
        self.assertEqual(self.exchange.place_calls, 1)

    def test_06_duplicate_client_id_single_order(self):
        # 交易所端（假设拒绝重复）
        self.exchange.place_order("DUP1", "buy", 1.0)
        with self.assertRaises(ExchangeRejected):
            self.exchange.place_order("DUP1", "buy", 1.0)
        self.assertEqual(len(self.exchange.orders), 1)
        # 管理器端：ID 重复直接拒绝，不发请求
        other = FakeExchange()
        mgr = OrderManager(
            other, OrderStore(self.dir / "o2.jsonl"), new_risk(),
            clock=lambda: next(self._clock), id_factory=lambda: "SAME1",
            sleep=lambda s: None,
        )
        self.assertTrue(mgr.reconcile()["ok"])
        mgr.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)
        calls = other.place_calls
        with self.assertRaises(ValueError):
            mgr.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)
        self.assertEqual(other.place_calls, calls)

    def test_bad_client_id_rejected(self):
        mgr = OrderManager(
            self.exchange, OrderStore(self.dir / "o3.jsonl"), new_risk(),
            clock=lambda: next(self._clock), id_factory=lambda: "bad id!",
            sleep=lambda s: None,
        )
        self.assertTrue(mgr.reconcile()["ok"])
        with self.assertRaises(ValueError) as ctx:
            mgr.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)
        self.assertIn("客户端订单 ID", str(ctx.exception))
        self.assertEqual(self.exchange.place_calls, 0)

    def test_07a_restart_after_crash_order_was_executed(self):
        mgr = self.ready()
        self.exchange.set_next_place("crash_executed")
        with self.assertRaises(SimulatedCrash):
            mgr.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)
        again = self.manager(prefix="R")
        self.assertEqual(list(again.orders.values())[0]["state"], SENT)
        self.assertTrue(again.reconcile()["ok"])
        self.assertEqual(list(again.orders.values())[0]["state"], FILLED)
        self.assertEqual(len(self.exchange.orders), 1)
        self.assertIsNone(again.can_trade_reason())

    def test_07b_restart_after_crash_order_not_executed(self):
        mgr = self.ready()
        self.exchange.set_next_place("crash_not_executed")
        with self.assertRaises(SimulatedCrash):
            mgr.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)
        again = self.manager(prefix="R")
        report = again.reconcile()
        self.assertFalse(report["ok"])
        self.assertEqual(list(again.orders.values())[0]["state"], UNKNOWN)
        with self.assertRaises(TradingHalted):
            again.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)
        self.assertEqual(self.exchange.place_calls, 1)

    def test_07c_intent_only_means_never_sent(self):
        self.ready()
        OrderStore(self.store_path).append({
            "type": "order", "cid": "LOST1", "state": INTENT, "side": "buy",
            "size": 1.0, "order_type": "market", "price": None,
            "ref_price": 100.0, "filled_size": 0.0, "updated_ms": 1,
        })
        again = self.manager(prefix="R")
        self.assertTrue(again.reconcile()["ok"])
        self.assertEqual(again.orders["LOST1"]["state"], REJECTED)

    def test_07d_partial_state_survives_restart(self):
        mgr = self.ready()
        rec = mgr.place_order("buy", 1.0, 100.0, order_type=LMT, price=100.0)
        self.exchange.fill(rec["cid"], 0.5)
        again = self.manager(prefix="R")
        self.assertTrue(again.reconcile()["ok"])
        self.assertEqual(again.orders[rec["cid"]]["state"], PARTIAL)
        self.assertAlmostEqual(again.orders[rec["cid"]]["filled_size"], 0.5)


class TestReconcileProblems(Base):
    def test_08_foreign_order_halts(self):
        self.ready()
        self.exchange.add_foreign_order("MANUAL1")
        again = self.manager(prefix="R")
        report = again.reconcile()
        self.assertFalse(report["ok"])
        self.assertTrue(any("FOREIGN_ORDER:MANUAL1" in i for i in report["issues"]))
        with self.assertRaises(TradingHalted):
            again.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)

    def test_08b_foreign_order_without_client_id_halts(self):
        self.ready()
        self.exchange.add_foreign_order("")
        report = self.manager(prefix="R").reconcile()
        self.assertFalse(report["ok"])
        self.assertTrue(any("FOREIGN_NO_CLIENT_ID" in i for i in report["issues"]))

    def test_09_balance_mismatch_halts(self):
        mgr = self.ready()
        mgr.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)
        self.assertTrue(self.manager(prefix="R").reconcile()["ok"])   # 含手续费，一致
        self.exchange.balances["BTC"] += 0.5
        again = self.manager(prefix="S")
        report = again.reconcile()
        self.assertFalse(report["ok"])
        self.assertTrue(any("BALANCE_MISMATCH" in i for i in report["issues"]))

    def test_no_baseline_with_existing_orders_halts(self):
        OrderStore(self.store_path).append({
            "type": "order", "cid": "OLD1", "state": FILLED, "side": "buy",
            "size": 1.0, "filled_size": 1.0, "updated_ms": 1,
        })
        report = self.manager().reconcile()
        self.assertFalse(report["ok"])
        self.assertIn("NO_BASELINE", report["issues"])

    def test_terminal_state_mismatch_halts(self):
        mgr = self.ready()
        rec = mgr.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)
        self.exchange.orders[rec["cid"]]["state"] = "canceled"
        report = self.manager(prefix="R").reconcile()
        self.assertFalse(report["ok"])
        self.assertTrue(any("STATE_MISMATCH" in i for i in report["issues"]))

    def test_live_order_missing_on_exchange_halts(self):
        mgr = self.ready()
        rec = mgr.place_order("buy", 1.0, 100.0, order_type=LMT, price=100.0)
        del self.exchange.orders[rec["cid"]]
        report = self.manager(prefix="R").reconcile()
        self.assertFalse(report["ok"])
        self.assertTrue(any("ORDER_MISSING" in i for i in report["issues"]))

    def test_fetch_failure_during_reconcile_halts(self):
        self.exchange.set_failures("get_open_orders", 1)
        mgr = self.manager()
        report = mgr.reconcile()
        self.assertFalse(report["ok"])
        self.assertIsNotNone(mgr.halted)

    def test_reconcile_can_be_rerun_after_problem_is_fixed(self):
        # 本次对账自己造成的停机：问题修复后，同一个管理器重跑对账可以解除。
        self.ready()
        self.exchange.add_foreign_order("MANUAL1")
        again = self.manager(prefix="R")
        self.assertFalse(again.reconcile()["ok"])
        del self.exchange.orders["MANUAL1"]
        self.assertTrue(again.reconcile()["ok"])
        self.assertIsNone(again.halted)
        # 解除记录已落盘：之后新启动的管理器不再继承这次停机。
        later = self.manager(prefix="S")
        self.assertIsNone(later.halted)
        self.assertTrue(later.reconcile()["ok"])

    def test_failed_rerun_keeps_trading_blocked(self):
        self.ready()
        self.exchange.add_foreign_order("MANUAL1")
        again = self.manager(prefix="R")
        self.assertFalse(again.reconcile()["ok"])
        self.assertFalse(again.reconcile()["ok"])     # 问题没修复，仍然失败
        with self.assertRaises(TradingHalted):
            again.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)
        self.assertEqual(self.exchange.place_calls, 0)


class TestPersistedHalt(Base):
    """修改 4、5：历史停机不能被启动或重新对账静默解除。"""

    def test_halt_from_previous_run_is_loaded(self):
        mgr = self.ready()
        mgr.halt("TEST_HALT_REASON")
        again = self.manager(prefix="R")
        self.assertIsNotNone(again.halted)
        self.assertIn("TEST_HALT_REASON", again.halted)
        self.assertFalse(again.reconciled)

    def test_persisted_halt_blocks_trading_even_after_reconcile_attempt(self):
        mgr = self.ready()
        mgr.halt("TEST_HALT_REASON")
        again = self.manager(prefix="R")
        report = again.reconcile()
        self.assertFalse(report["ok"])
        self.assertTrue(report["issues"][0].startswith("PERSISTED_HALT"))
        self.assertFalse(again.reconciled)
        with self.assertRaises(TradingHalted):
            again.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)
        self.assertEqual(self.exchange.place_calls, 0)

    def test_reconcile_does_not_clear_persisted_halt_repeatedly(self):
        mgr = self.ready()
        mgr.halt("TEST_HALT_REASON")
        again = self.manager(prefix="R")
        for _ in range(3):
            self.assertFalse(again.reconcile()["ok"])
        self.assertIn("TEST_HALT_REASON", again.halted)
        # 再启动一次，仍然带着停机。
        third = self.manager(prefix="S")
        self.assertIn("TEST_HALT_REASON", third.halted)

    def test_runtime_halt_not_cleared_by_reconcile(self):
        mgr = self.ready()
        self.exchange.set_failures("get_balance", 3)
        for _ in range(3):
            with self.assertRaises(Exception):
                mgr.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)
        self.assertEqual(mgr.halted, "CONSECUTIVE_ERRORS")
        report = mgr.reconcile()
        self.assertFalse(report["ok"])
        self.assertTrue(report["issues"][0].startswith("ACTIVE_HALT"))
        self.assertEqual(mgr.halted, "CONSECUTIVE_ERRORS")

    def test_operator_clear_allows_reconcile_and_trading(self):
        mgr = self.ready()
        mgr.halt("TEST_HALT_REASON")
        again = self.manager(prefix="R")
        again.operator_clear_halt("checked exchange manually")
        self.assertIsNone(again.halted)
        self.assertFalse(again.reconciled)            # 仍需重新对账
        with self.assertRaises(TradingHalted):
            again.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)
        self.assertTrue(again.reconcile()["ok"])
        rec = again.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)
        self.assertEqual(rec["state"], FILLED)
        # 解除记录已落盘，再次启动不会重新带上停机。
        later = self.manager(prefix="S")
        self.assertIsNone(later.halted)
        self.assertTrue(later.reconcile()["ok"])

    def test_operator_clear_requires_note(self):
        mgr = self.ready()
        mgr.halt("TEST_HALT_REASON")
        for bad in ("", "   ", None):
            with self.assertRaises(ValueError):
                mgr.operator_clear_halt(bad)
        self.assertIn("TEST_HALT_REASON", mgr.halted)

    def test_clear_marker_can_be_written_without_manager(self):
        mgr = self.ready()
        mgr.halt("TEST_HALT_REASON")
        write_halt_cleared_marker(OrderStore(self.store_path), "fixed by hand")
        again = self.manager(prefix="R")
        self.assertIsNone(again.halted)
        self.assertTrue(again.reconcile()["ok"])

    def test_new_halt_after_clear_is_loaded(self):
        mgr = self.ready()
        mgr.halt("FIRST_HALT")
        mgr.operator_clear_halt("handled first")
        again = self.manager(prefix="R")
        self.assertTrue(again.reconcile()["ok"])
        again.halt("SECOND_HALT")
        third = self.manager(prefix="S")
        self.assertIn("SECOND_HALT", third.halted)


class TestStateMachine(Base):
    def test_10_no_transition_out_of_terminal_states(self):
        for terminal in TERMINAL:
            self.assertEqual(ALLOWED[terminal], set())
        mgr = self.ready()
        rec = mgr.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)
        self.assertEqual(rec["state"], FILLED)
        for target in (LIVE, PARTIAL, CANCELED, REJECTED, UNKNOWN, SENT, INTENT):
            with self.assertRaises(IllegalTransition):
                mgr.transition(rec["cid"], target)

    def test_10b_allowed_table_matches_design(self):
        self.assertEqual(ALLOWED[INTENT], {SENT, REJECTED})
        self.assertEqual(ALLOWED[SENT], {LIVE, PARTIAL, FILLED, REJECTED, UNKNOWN})
        self.assertEqual(ALLOWED[LIVE], {PARTIAL, FILLED, CANCELED})
        self.assertEqual(ALLOWED[PARTIAL], {PARTIAL, FILLED, CANCELED})
        self.assertEqual(ALLOWED[UNKNOWN], {LIVE, PARTIAL, FILLED, CANCELED, REJECTED})

    def test_live_to_filled_in_one_step(self):
        mgr = self.ready()
        rec = mgr.place_order("buy", 1.0, 100.0, order_type=LMT, price=100.0)
        self.exchange.fill(rec["cid"], 1.0)
        mgr.refresh(rec["cid"])
        self.assertEqual(mgr.orders[rec["cid"]]["state"], FILLED)

    def test_fill_decrease_halts(self):
        mgr = self.ready()
        rec = mgr.place_order("buy", 1.0, 100.0, order_type=LMT, price=100.0)
        self.exchange.fill(rec["cid"], 0.6)
        mgr.refresh(rec["cid"])
        self.exchange.orders[rec["cid"]]["filled_size"] = 0.2
        mgr.refresh(rec["cid"])
        self.assertIn("FILL_DECREASED", mgr.halted)

    def test_overfill_halts(self):
        mgr = self.ready()
        rec = mgr.place_order("buy", 1.0, 100.0, order_type=LMT, price=100.0)
        self.exchange.orders[rec["cid"]]["filled_size"] = 1.5
        self.exchange.orders[rec["cid"]]["state"] = "filled"
        mgr.refresh(rec["cid"])
        self.assertIn("OVERFILLED", mgr.halted)

    def test_unknown_exchange_state_halts(self):
        mgr = self.ready()
        rec = mgr.place_order("buy", 1.0, 100.0, order_type=LMT, price=100.0)
        self.exchange.orders[rec["cid"]]["state"] = "some_new_state"
        mgr.refresh(rec["cid"])
        self.assertIn("UNKNOWN_EXCHANGE_STATE", mgr.halted)
        self.assertEqual(mgr.orders[rec["cid"]]["state"], LIVE)


class TestRiskAndStop(Base):
    def test_11a_risk_rejects_before_any_record_or_request(self):
        mgr = self.ready()
        with self.assertRaises(RiskRejected):
            mgr.place_order("buy", 100.0, ref_price=100.0, order_type=MKT)  # 名义 10000 > 5000
        self.assertEqual(self.exchange.place_calls, 0)
        self.assertEqual(mgr.orders, {})

    def test_11b_position_limit(self):
        mgr = self.ready()
        for _ in range(4):
            mgr.place_order("buy", 50.0, ref_price=100.0, order_type=MKT)   # 每笔 5000，累计 200 个
        with self.assertRaises(RiskRejected):
            mgr.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)

    def test_11c_no_short(self):
        mgr = self.ready()
        with self.assertRaises(RiskRejected):
            mgr.place_order("sell", 1.0, ref_price=100.0, order_type=MKT)

    def test_11d_stop_flag(self):
        mgr = self.ready()
        self.stop_path.write_text("stop")
        with self.assertRaises(TradingHalted):
            mgr.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)
        self.assertEqual(self.exchange.place_calls, 0)
        self.stop_path.unlink()
        mgr.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)

    def test_11e_daily_loss(self):
        risk = new_risk()
        mgr = self.ready(risk=risk)
        risk.record_pnl(-1000.0)
        with self.assertRaises(RiskRejected):
            mgr.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)

    def test_13_consecutive_errors_halt(self):
        mgr = self.ready()
        self.exchange.set_failures("get_balance", 3)
        for _ in range(3):
            with self.assertRaises(Exception):
                mgr.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)
        self.assertEqual(mgr.halted, "CONSECUTIVE_ERRORS")
        with self.assertRaises(TradingHalted):
            mgr.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)

    def test_13b_success_resets_error_counter(self):
        mgr = self.ready()
        self.exchange.set_failures("get_balance", 2)
        for _ in range(2):
            with self.assertRaises(Exception):
                mgr.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)
        mgr.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)   # 成功，计数清零
        self.exchange.set_failures("get_balance", 2)
        for _ in range(2):
            with self.assertRaises(Exception):
                mgr.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)
        self.assertIsNone(mgr.halted)


class TestNoSecretsInRecords(Base):
    def test_12_secrets_never_written_to_store(self):
        secrets = [API_KEY, SECRET, PASSPHRASE]
        mgr = self.ready(secrets=secrets)
        self.exchange.reject_message = (
            f"bad request OK-ACCESS-KEY: {API_KEY} sign={SECRET} "
            f"OK-ACCESS-PASSPHRASE: {PASSPHRASE}"
        )
        self.exchange.set_next_place("reject")
        rec = mgr.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)
        self.assertEqual(rec["state"], REJECTED)
        mgr.halt(f"manual halt with {SECRET}")
        text = self.store_path.read_text(encoding="utf-8")
        for value in secrets:
            self.assertNotIn(value, text)
        self.assertIn("***", text)

    def test_12b_unknown_header_values_also_removed(self):
        mgr = self.ready()
        self.exchange.reject_message = "OK-ACCESS-SIGN: SOMESIG123 OK-ACCESS-KEY=SOMEKEY999"
        self.exchange.set_next_place("reject")
        mgr.place_order("buy", 1.0, ref_price=100.0, order_type=MKT)
        text = self.store_path.read_text(encoding="utf-8")
        self.assertNotIn("SOMESIG123", text)
        self.assertNotIn("SOMEKEY999", text)

    def test_12c_operator_note_is_redacted(self):
        secrets = [SECRET]
        mgr = self.ready(secrets=secrets)
        mgr.halt("x")
        mgr.operator_clear_halt(f"note mentions {SECRET}")
        text = self.store_path.read_text(encoding="utf-8")
        self.assertNotIn(SECRET, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)

