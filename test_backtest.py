import csv
import sys
import tempfile
import unittest
from pathlib import Path

# 兼容 Pyto：确保当前项目目录可以被 Python 找到
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from run_backtest import run_buy_and_hold, load_close_prices

DAY = 86_400_000
NAN = float("nan")
INF = float("inf")


class TestBuyAndHoldBacktest(unittest.TestCase):
    # ---------- 原有 7 项 ----------
    def test_flat_price(self):
        dates = [0, DAY, 2 * DAY]
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
            result["total_return"],
            expected_quantity * 100.0 / 10000.0 - 1,
            places=8,
        )

    def test_price_doubles(self):
        result = run_buy_and_hold(
            [0, DAY], [100.0, 200.0], initial_cash=10000.0, fee_rate=0.001
        )
        expected_quantity = 10000.0 / (100.0 * 1.001)
        self.assertAlmostEqual(result["quantity"], expected_quantity, places=8)
        self.assertAlmostEqual(
            result["final_equity"], expected_quantity * 200.0, places=6
        )

    def test_max_drawdown(self):
        result = run_buy_and_hold(
            [0, DAY, 2 * DAY, 3 * DAY],
            [100.0, 200.0, 100.0, 150.0],
            initial_cash=10000.0,
            fee_rate=0.0,
        )
        self.assertAlmostEqual(result["max_drawdown"], -0.5, places=8)

    def test_invalid_initial_cash(self):
        with self.assertRaises(ValueError):
            run_buy_and_hold([0, DAY], [100.0, 110.0],
                             initial_cash=0.0, fee_rate=0.001)

    def test_invalid_fee(self):
        with self.assertRaises(ValueError):
            run_buy_and_hold([0, DAY], [100.0, 110.0],
                             initial_cash=10000.0, fee_rate=1.0)

    def test_prices_must_be_positive(self):
        with self.assertRaises(ValueError):
            run_buy_and_hold([0, DAY], [100.0, 0.0],
                             initial_cash=10000.0, fee_rate=0.001)

    def test_dates_must_be_increasing(self):
        with self.assertRaises(ValueError):
            run_buy_and_hold([DAY, 0], [100.0, 110.0],
                             initial_cash=10000.0, fee_rate=0.001)

    # ---------- 新增：NaN / 无穷大 / 长度 ----------
    def test_nan_price_rejected(self):
        with self.assertRaises(ValueError):
            run_buy_and_hold([0, DAY, 2 * DAY], [100.0, NAN, 110.0])

    def test_inf_price_rejected(self):
        with self.assertRaises(ValueError):
            run_buy_and_hold([0, DAY], [100.0, INF])

    def test_nan_and_inf_cash_rejected(self):
        for bad in (NAN, INF, -INF):
            with self.assertRaises(ValueError):
                run_buy_and_hold([0, DAY], [100.0, 110.0], initial_cash=bad)

    def test_nan_and_inf_fee_rejected(self):
        for bad in (NAN, INF, -0.001):
            with self.assertRaises(ValueError):
                run_buy_and_hold([0, DAY], [100.0, 110.0], fee_rate=bad)

    def test_non_numeric_price_rejected(self):
        with self.assertRaises(ValueError):
            run_buy_and_hold([0, DAY], [100.0, "110"])

    def test_length_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            run_buy_and_hold([0, DAY, 2 * DAY], [100.0, 110.0])

    def test_empty_and_single_rejected(self):
        with self.assertRaises(ValueError):
            run_buy_and_hold([], [])
        with self.assertRaises(ValueError):
            run_buy_and_hold([0], [100.0])

    def test_duplicate_dates_rejected(self):
        with self.assertRaises(ValueError):
            run_buy_and_hold([0, 0], [100.0, 110.0])

    # ---------- 新增：口径与手算对照 ----------
    def test_first_day_drawdown_includes_fee(self):
        fee = 0.001
        result = run_buy_and_hold(
            [0, DAY], [100.0, 100.0], initial_cash=10000.0, fee_rate=fee
        )
        self.assertAlmostEqual(
            result["max_drawdown"], -fee / (1 + fee), places=10
        )

    def test_hand_calculated_three_candles(self):
        # 100 -> 200 -> 100，手续费 0.1%
        # 数量 = 10000 / 100.1 = 99.900099...
        # 期末资产 = 99.900099... * 100 = 9990.00999...
        # 峰值 = 99.900099... * 200；期末回撤恰好 -50%
        result = run_buy_and_hold(
            [0, DAY, 2 * DAY], [100.0, 200.0, 100.0],
            initial_cash=10000.0, fee_rate=0.001,
        )
        self.assertAlmostEqual(result["final_equity"], 9990.00999001, places=6)
        self.assertAlmostEqual(result["total_return"], -0.000999000999, places=9)
        self.assertAlmostEqual(result["max_drawdown"], -0.5, places=9)
        self.assertAlmostEqual(
            result["final_equity_after_exit_fee"],
            9990.00999001 * 0.999, places=6,
        )

    def test_zero_fee_flat_price_zero_return(self):
        result = run_buy_and_hold(
            [0, DAY, 2 * DAY], [100.0, 100.0, 100.0], fee_rate=0.0
        )
        self.assertAlmostEqual(result["total_return"], 0.0, places=12)
        self.assertAlmostEqual(result["max_drawdown"], 0.0, places=12)

    def test_deterministic(self):
        args = ([0, DAY, 2 * DAY], [100.0, 150.0, 120.0])
        self.assertEqual(
            run_buy_and_hold(*args), run_buy_and_hold(*args)
        )

    # ---------- 新增：年化 ----------
    def test_annualized_hidden_when_under_one_year(self):
        result = run_buy_and_hold([0, DAY], [100.0, 200.0])
        self.assertIsNone(result["annualized_return"])

    def test_annualized_one_year_doubling(self):
        one_year_ms = 31_557_600_000  # 365.25 天
        result = run_buy_and_hold(
            [0, one_year_ms], [100.0, 200.0], fee_rate=0.0
        )
        self.assertAlmostEqual(result["annualized_return"], 1.0, places=9)


class TestLoadClosePrices(unittest.TestCase):
    def write_csv(self, rows, header=None):
        header = header or ["timestamp", "open", "high", "low", "close", "volume"]
        temp = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="",
            suffix=".csv", delete=False,
        )
        self.addCleanup(lambda: Path(temp.name).unlink(missing_ok=True))
        writer = csv.writer(temp)
        writer.writerow(header)
        writer.writerows(rows)
        temp.close()
        return temp.name

    def row(self, day, close="105"):
        return [str(day * DAY), "100", "110", "90", close, "1000"]

    def test_valid_file_loads(self):
        path = self.write_csv([self.row(1), self.row(2, "106")])
        dates, closes = load_close_prices(path, now_ms=10 * DAY)
        self.assertEqual(dates, [DAY, 2 * DAY])
        self.assertEqual(closes, [105.0, 106.0])

    def test_nan_close_rejected(self):
        path = self.write_csv([self.row(1), self.row(2, "nan")])
        with self.assertRaises(ValueError):
            load_close_prices(path, now_ms=10 * DAY)

    def test_missing_column_rejected(self):
        path = self.write_csv(
            [["1", "100", "110", "90", "105"]],
            header=["timestamp", "open", "high", "low", "close"],
        )
        with self.assertRaises(ValueError):
            load_close_prices(path, now_ms=10 * DAY)

    def test_missing_day_rejected(self):
        path = self.write_csv([self.row(1), self.row(3)])
        with self.assertRaises(ValueError):
            load_close_prices(path, now_ms=10 * DAY)

    def test_single_row_rejected(self):
        path = self.write_csv([self.row(1)])
        with self.assertRaises(ValueError):
            load_close_prices(path, now_ms=10 * DAY)


if __name__ == "__main__":
    unittest.main(verbosity=2)
