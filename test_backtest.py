
import sys
import unittest
from pathlib import Path

# 兼容 Pyto：确保当前项目目录可以被 Python 找到
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from run_backtest import run_buy_and_hold


class TestBuyAndHoldBacktest(unittest.TestCase):

    def test_flat_price(self):
        dates = [0, 86_400_000, 2 * 86_400_000]
        closes = [100.0, 100.0, 100.0]

        result = run_buy_and_hold(
            dates, closes, initial_cash=10000.0, fee_rate=0.001
        )

        expected_quantity = 10000.0 / (100.0 * 1.001)
        self.assertAlmostEqual(result["quantity"], expected_quantity, places=8)
        self.assertAlmostEqual(
            result["final_equity"], expected_quantity * 100.0, places=6
        )
        self.assertAlmostEqual(
            result["total_return"], expected_quantity * 100.0 / 10000.0 - 1,
            places=8
        )

    def test_price_doubles(self):
        dates = [0, 86_400_000]
        closes = [100.0, 200.0]

        result = run_buy_and_hold(
            dates, closes, initial_cash=10000.0, fee_rate=0.001
        )

        expected_quantity = 10000.0 / (100.0 * 1.001)
        self.assertAlmostEqual(result["quantity"], expected_quantity, places=8)
        self.assertAlmostEqual(
            result["final_equity"], expected_quantity * 200.0, places=6
        )

    def test_max_drawdown(self):
        dates = [0, 86_400_000, 2 * 86_400_000, 3 * 86_400_000]
        closes = [100.0, 200.0, 100.0, 150.0]

        result = run_buy_and_hold(
            dates, closes, initial_cash=10000.0, fee_rate=0.0
        )

        self.assertAlmostEqual(result["max_drawdown"], -0.5, places=8)

    def test_invalid_initial_cash(self):
        with self.assertRaises(ValueError):
            run_buy_and_hold(
                [0, 86_400_000], [100.0, 110.0],
                initial_cash=0.0, fee_rate=0.001
            )

    def test_invalid_fee(self):
        with self.assertRaises(ValueError):
            run_buy_and_hold(
                [0, 86_400_000], [100.0, 110.0],
                initial_cash=10000.0, fee_rate=1.0
            )

    def test_prices_must_be_positive(self):
        with self.assertRaises(ValueError):
            run_buy_and_hold(
                [0, 86_400_000], [100.0, 0.0],
                initial_cash=10000.0, fee_rate=0.001
            )

    def test_dates_must_be_increasing(self):
        with self.assertRaises(ValueError):
            run_buy_and_hold(
                [86_400_000, 0], [100.0, 110.0],
                initial_cash=10000.0, fee_rate=0.001
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)

