import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from risk_guard import RiskGuard, RiskLimits, RiskRejected

NAN = float("nan")


def make():
    return RiskGuard(RiskLimits(
        max_order_notional=1000.0,
        max_position_notional=3000.0,
        max_daily_loss=500.0,
    ))


class TestRiskGuard(unittest.TestCase):
    def test_normal_buy_passes(self):
        make().check("buy", 5.0, 100.0, 0.0)

    def test_order_notional_limit(self):
        with self.assertRaises(RiskRejected):
            make().check("buy", 11.0, 100.0, 0.0)

    def test_exactly_at_limit_passes(self):
        make().check("buy", 10.0, 100.0, 0.0)

    def test_position_limit(self):
        guard = make()
        guard.check("buy", 5.0, 100.0, 25.0)          # 持仓到 30 -> 3000，刚好
        with self.assertRaises(RiskRejected):
            guard.check("buy", 5.0, 100.0, 26.0)

    def test_no_short_selling(self):
        guard = make()
        guard.check("sell", 2.0, 100.0, 2.0)
        with self.assertRaises(RiskRejected):
            guard.check("sell", 2.1, 100.0, 2.0)

    def test_daily_loss_blocks_buy_but_allows_sell(self):
        guard = make()
        guard.record_pnl(-500.0)
        with self.assertRaises(RiskRejected):
            guard.check("buy", 1.0, 100.0, 0.0)
        guard.check("sell", 1.0, 100.0, 5.0)
        guard.reset_day()
        guard.check("buy", 1.0, 100.0, 0.0)

    def test_limits_cannot_be_changed(self):
        guard = make()
        with self.assertRaises(FrozenInstanceError):
            guard.limits.max_order_notional = 10 ** 9
        with self.assertRaises(AttributeError):
            guard.limits = RiskLimits(1, 1, 1)

    def test_invalid_inputs_rejected(self):
        guard = make()
        for args in (
            ("hold", 1.0, 100.0, 0.0), ("buy", NAN, 100.0, 0.0),
            ("buy", 1.0, float("inf"), 0.0), ("buy", -1.0, 100.0, 0.0),
            ("buy", True, 100.0, 0.0), ("buy", 1.0, 100.0, NAN),
        ):
            with self.assertRaises(RiskRejected):
                guard.check(*args)

    def test_invalid_limits_rejected(self):
        for bad in (0, -1, NAN, float("inf"), True):
            with self.assertRaises(ValueError):
                RiskLimits(bad, 1.0, 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

