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
)
from order_store import OrderStore
from risk_guard import RiskGuard, RiskLimits, RiskRejected

NAN = float("nan")
API_KEY = "KEYVALUE-abc-123456"
SECRET = "SECRETVALUE-xyz-987654"
PASSPHRASE = "PassPhrase#2026"


def new_risk():
    return RiskGuard(RiskLimits(
        max_order_notional=5000.0, max_position_notional=20000.0,
        max_daily_loss=1000.0,
    ))


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
        rec = mgr.place_order("buy", 1.0, ref_price=100.0)
        self.assertEqual(rec["state"], FILLED)
        self.assertEqual(rec["filled_size"], 1.0)
        self.assertEqual(self.exchange.place_calls, 1)
        orders, baseline, _ = OrderStore(self.store_path).load()
        self.assertEqual(orders[rec["cid"]]["state"], FILLED)
        self.assertEqual(baseline["base"], 0.0)

    def test_02_partial_fills_then_filled(self):
        mgr = self.ready()
        rec = mgr.place_order("buy", 1.0, ref_price=100.0,
                              order_type="limit", price=100.0)
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
        rec = mgr.place_order("buy", 1.0, ref_price=100.0)
        self.assertEqual(rec["state"], REJECTED)
        self.assertEqual(self.exchange.place_calls, 1)
        self.assertIsNone(mgr.can_trade_reason())

    def test_buy_then_sell_round_trip_reconciles(self):
        mgr = self.ready()
        mgr.place_order("buy", 2.0, ref_price=100.0)
        mgr.place_order("sell", 1.5, ref_price=100.0)
        again = self.manager(prefix="R")
        self.assertTrue(again.reconcile()["ok"])

    def test_cancel_live_and_partial(self):
        mgr = self.ready()
        rec = mgr.place_order("buy", 1.0, 100.0, order_type="limit", price=100.0)
        self.assertEqual(mgr.cancel_order(rec["cid"])["state"], CANCELED)
        rec2 = mgr.place_order("buy", 1.0, 100.0, order_type="limit", price=100.0)
        self.exchange.fill(rec2["cid"], 0.25)
        mgr.refresh(rec2["cid"])
        done = mgr.cancel_order(rec2["cid"])
        self.assertEqual(done["state"], CANCELED)
        self.assertAlmostEqual(done["filled_size"], 0.25)
        self.assertTrue(self.manager(prefix="R").reconcile()["ok"])

    def test_open_order_blocks_new_orders(self):
        mgr = self.ready()
        rec = mgr.place_order("buy", 1.0, 100.0, order_type="limit", price=100.0)
        with self.assertRaises(TradingHalted):
            mgr.place_order("buy", 1.0, ref_price=100.0)
        mgr.cancel_order(rec["cid"])
        mgr.place_order("buy", 1.0, ref_price=100.0)

    def test_not_reconciled_blocks_trading(self):
        mgr = self.manager()
        with self.assertRaises(TradingHalted):
            mgr.place_order("buy", 1.0, ref_price=100.0)
        self.assertEqual(self.exchange.place_calls, 0)

    def test_invalid_inputs_raise_value_error(self):
        mgr = self.ready()
        for kwargs in (
            dict(side="hold", size=1.0, ref_price=100.0),
            dict(side="buy", size=NAN, ref_price=100.0),
            dict(side="buy", size=-1.0, ref_price=100.0),
            dict(side="buy", size=1.0, ref_price=float("inf")),
            dict(side="buy", size=1.0, ref_price=100.0, order_type="limit"),
            dict(side="buy", size=1.0, ref_price=100.0, order_type="stop"),
        ):
            with self.assertRaises(ValueError):
                mgr.place_order(**kwargs)
        self.assertEqual(self.exchange.place_calls, 0)


class TestUncertainOutcomes(Base):
    def test_04_timeout_but_executed_resolves_without_duplicate(self):
        mgr = self.ready()
        self.exchange.set_next_place("timeout_executed")
        rec = mgr.place_order("buy", 1.0, ref_price=100.0)
        self.assertEqual(rec["state"], FILLED)
        self.assertEqual(self.exchange.place_calls, 1)
        self.assertEqual(len(self.exchange.orders), 1)
        self.assertIsNone(mgr.halted)

    def test_05_timeout_not_executed_stays_unknown_and_halts(self):
        mgr = self.ready()
        self.exchange.set_next_place("timeout_not_executed")
        rec = mgr.place_order("buy", 1.0, ref_price=100.0)
        self.assertEqual(rec["state"], UNKNOWN)
        self.assertEqual(self.exchange.place_calls, 1)       # 没有重发
        self.assertIn("UNRESOLVED_UNKNOWN", mgr.halted)
        with self.assertRaises(TradingHalted):
            mgr.place_order("buy", 1.0, ref_price=100.0)
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
        mgr.place_order("buy", 1.0, ref_price=100.0)
        calls = other.place_calls
        with self.assertRaises(ValueError):
            mgr.place_order("buy", 1.0, ref_price=100.0)
        self.assertEqual(other.place_calls, calls)

    def test_bad_client_id_rejected(self):
        mgr = OrderManager(
            self.exchange, OrderStore(self.dir / "o3.jsonl"), new_risk(),
            clock=lambda: next(self._clock), id_factory=lambda: "bad id!",
            sleep=lambda s: None,
        )
        self.assertTrue(mgr.reconcile()["ok"])
        with self.assertRaises(ValueError):
            mgr.place_order("buy", 1.0, ref_price=100.0)

    def test_07a_restart_after_crash_order_was_executed(self):
        mgr = self.ready()
        self.exchange.set_next_place("crash_executed")
        with self.assertRaises(SimulatedCrash):
            mgr.place_order("buy", 1.0, ref_price=100.0)
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
            mgr.place_order("buy", 1.0, ref_price=100.0)
        again = self.manager(prefix="R")
        report = again.reconcile()
        self.assertFalse(report["ok"])
        self.assertEqual(list(again.orders.values())[0]["state"], UNKNOWN)
        with self.assertRaises(TradingHalted):
            again.place_order("buy", 1.0, ref_price=100.0)
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
        rec = mgr.place_order("buy", 1.0, 100.0, order_type="limit", price=100.0)
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
            again.place_order("buy", 1.0, ref_price=100.0)

    def test_08b_foreign_order_without_client_id_halts(self):
        self.ready()
        self.exchange.add_foreign_order("")
        report = self.manager(prefix="R").reconcile()
        self.assertFalse(report["ok"])
        self.assertTrue(any("FOREIGN_NO_CLIENT_ID" in i for i in report["issues"]))

    def test_09_balance_mismatch_halts(self):
        mgr = self.ready()
        mgr.place_order("buy", 1.0, ref_price=100.0)
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
        rec = mgr.place_order("buy", 1.0, ref_price=100.0)
        self.exchange.orders[rec["cid"]]["state"] = "canceled"
        report = self.manager(prefix="R").reconcile()
        self.assertFalse(report["ok"])
        self.assertTrue(any("STATE_MISMATCH" in i for i in report["issues"]))

    def test_live_order_missing_on_exchange_halts(self):
        mgr = self.ready()
        rec = mgr.place_order("buy", 1.0, 100.0, order_type="limit", price=100.0)
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
        self.ready()
        self.exchange.add_foreign_order("MANUAL1")
        again = self.manager(prefix="R")
        self.assertFalse(again.reconcile()["ok"])
        del self.exchange.orders["MANUAL1"]
        self.assertTrue(again.reconcile()["ok"])
        self.assertIsNone(again.halted)


class TestStateMachine(Base):
    def test_10_no_transition_out_of_terminal_states(self):
        for terminal in TERMINAL:
            self.assertEqual(ALLOWED[terminal], set())
        mgr = self.ready()
        rec = mgr.place_order("buy", 1.0, ref_price=100.0)
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
        rec = mgr.place_order("buy", 1.0, 100.0, order_type="limit", price=100.0)
        self.exchange.fill(rec["cid"], 1.0)
        mgr.refresh(rec["cid"])
        self.assertEqual(mgr.orders[rec["cid"]]["state"], FILLED)

    def test_fill_decrease_halts(self):
        mgr = self.ready()
        rec = mgr.place_order("buy", 1.0, 100.0, order_type="limit", price=100.0)
        self.exchange.fill(rec["cid"], 0.6)
        mgr.refresh(rec["cid"])
        self.exchange.orders[rec["cid"]]["filled_size"] = 0.2
        mgr.refresh(rec["cid"])
        self.assertIn("FILL_DECREASED", mgr.halted)

    def test_overfill_halts(self):
        mgr = self.ready()
        rec = mgr.place_order("buy", 1.0, 100.0, order_type="limit", price=100.0)
        self.exchange.orders[rec["cid"]]["filled_size"] = 1.5
        self.exchange.orders[rec["cid"]]["state"] = "filled"
        mgr.refresh(rec["cid"])
        self.assertIn("OVERFILLED", mgr.halted)

    def test_unknown_exchange_state_halts(self):
        mgr = self.ready()
        rec = mgr.place_order("buy", 1.0, 100.0, order_type="limit", price=100.0)
        self.exchange.orders[rec["cid"]]["state"] = "some_new_state"
        mgr.refresh(rec["cid"])
        self.assertIn("UNKNOWN_EXCHANGE_STATE", mgr.halted)
        self.assertEqual(mgr.orders[rec["cid"]]["state"], LIVE)


class TestRiskAndStop(Base):
    def test_11a_risk_rejects_before_any_record_or_request(self):
        mgr = self.ready()
        with self.assertRaises(RiskRejected):
            mgr.place_order("buy", 100.0, ref_price=100.0)   # 名义 10000 > 5000
        self.assertEqual(self.exchange.place_calls, 0)
        self.assertEqual(mgr.orders, {})

    def test_11b_position_limit(self):
        mgr = self.ready()
        for _ in range(4):
            mgr.place_order("buy", 50.0, ref_price=100.0)    # 每笔 5000，累计 200 个
        with self.assertRaises(RiskRejected):
            mgr.place_order("buy", 1.0, ref_price=100.0)

    def test_11c_no_short(self):
        mgr = self.ready()
        with self.assertRaises(RiskRejected):
            mgr.place_order("sell", 1.0, ref_price=100.0)

    def test_11d_stop_flag(self):
        mgr = self.ready()
        self.stop_path.write_text("stop")
        with self.assertRaises(TradingHalted):
            mgr.place_order("buy", 1.0, ref_price=100.0)
        self.assertEqual(self.exchange.place_calls, 0)
        self.stop_path.unlink()
        mgr.place_order("buy", 1.0, ref_price=100.0)

    def test_11e_daily_loss(self):
        risk = new_risk()
        mgr = self.ready(risk=risk)
        risk.record_pnl(-1000.0)
        with self.assertRaises(RiskRejected):
            mgr.place_order("buy", 1.0, ref_price=100.0)

    def test_13_consecutive_errors_halt(self):
        mgr = self.ready()
        self.exchange.set_failures("get_balance", 3)
        for _ in range(3):
            with self.assertRaises(Exception):
                mgr.place_order("buy", 1.0, ref_price=100.0)
        self.assertEqual(mgr.halted, "CONSECUTIVE_ERRORS")
        with self.assertRaises(TradingHalted):
            mgr.place_order("buy", 1.0, ref_price=100.0)

    def test_13b_success_resets_error_counter(self):
        mgr = self.ready()
        self.exchange.set_failures("get_balance", 2)
        for _ in range(2):
            with self.assertRaises(Exception):
                mgr.place_order("buy", 1.0, ref_price=100.0)
        mgr.place_order("buy", 1.0, ref_price=100.0)          # 成功，计数清零
        self.exchange.set_failures("get_balance", 2)
        for _ in range(2):
            with self.assertRaises(Exception):
                mgr.place_order("buy", 1.0, ref_price=100.0)
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
        rec = mgr.place_order("buy", 1.0, ref_price=100.0)
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
        mgr.place_order("buy", 1.0, ref_price=100.0)
        text = self.store_path.read_text(encoding="utf-8")
        self.assertNotIn("SOMESIG123", text)
        self.assertNotIn("SOMEKEY999", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)

