import csv
import sys
import tempfile
import unittest
from pathlib import Path

# 兼容 Pyto：确保当前项目目录可以被 Python 找到
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from backtest_engine import build_signals, run_signal_backtest
from run_backtest import run_buy_and_hold, load_price_data

DAY = 86_400_000
NAN = float("nan")
INF = float("inf")


def days(n):
    return [i * DAY for i in range(n)]


class TestHandCalculated(unittest.TestCase):
    """
    5 根 K 线，手续费 0.1%，初始资金 10000：
      开盘  = [100, 100, 110, 120, 90]
      收盘  = [100, 105, 115, 100, 95]
      信号  = [1, 1, 1, 0, 1]   （最后一个信号没有下一个开盘，应被忽略）

    第 1 天开盘 100 买入：数量 = 10000 / 100.1 = 99.9000999001
    第 4 天开盘 90 卖出：现金 = 99.9000999001 * 90 * 0.999 = 8982.01798202
    峰值（第 2 天收盘）= 99.9000999001 * 115 = 11488.5114885
    最大回撤 = 89.91 / 115 - 1 = -0.218173913
    手续费：买 9.99000999 + 卖 8.99100899 = 18.98101898
    """

    def setUp(self):
        self.result = run_signal_backtest(
            days(5),
            [100.0, 100.0, 110.0, 120.0, 90.0],
            [100.0, 105.0, 115.0, 100.0, 95.0],
            [1, 1, 1, 0, 1],
            initial_cash=10000.0,
            fee_rate=0.001,
        )

    def test_final_equity_and_return(self):
        self.assertAlmostEqual(self.result["final_equity"], 8982.01798202, places=6)
        self.assertAlmostEqual(self.result["total_return"], -0.1017982018, places=9)

    def test_max_drawdown(self):
        self.assertAlmostEqual(self.result["max_drawdown"], -0.218173913, places=8)

    def test_trades_use_next_open(self):
        trades = self.result["trades"]
        self.assertEqual(self.result["trade_count"], 2)
        self.assertEqual(trades[0]["side"], "BUY")
        self.assertEqual(trades[0]["timestamp"], DAY)        # 第 1 天
        self.assertEqual(trades[0]["price"], 100.0)           # 第 1 天开盘价
        self.assertEqual(trades[1]["side"], "SELL")
        self.assertEqual(trades[1]["timestamp"], 4 * DAY)    # 第 4 天
        self.assertEqual(trades[1]["price"], 90.0)            # 第 4 天开盘价

    def test_fees(self):
        self.assertAlmostEqual(self.result["total_fees"], 18.98101898, places=6)

    def test_day_zero_has_no_position(self):
        first = self.result["equity_curve"][0]
        self.assertEqual(first["quantity"], 0.0)
        self.assertEqual(first["equity"], 10000.0)

    def test_last_signal_ignored(self):
        # 最后一天信号为 1，但没有下一个开盘，不能产生交易
        self.assertEqual(self.result["trades"][-1]["side"], "SELL")
        self.assertEqual(self.result["equity_curve"][-1]["quantity"], 0.0)


class TestReconciliation(unittest.TestCase):
    def test_always_long_equals_buy_and_hold(self):
        # 连续市场：每天开盘价 = 前一天收盘价
        closes = [100.0, 110.0, 90.0, 120.0, 130.0]
        opens = [100.0] + closes[:-1]
        signals = [1] * 5
        engine = run_signal_backtest(days(5), opens, closes, signals)
        baseline = run_buy_and_hold(days(5), closes)
        self.assertAlmostEqual(
            engine["final_equity"], baseline["final_equity"], places=6
        )

    def test_never_trade_keeps_cash(self):
        result = run_signal_backtest(
            days(4), [100.0] * 4, [100.0, 120.0, 80.0, 100.0], [0, 0, 0, 0]
        )
        self.assertEqual(result["final_equity"], 10000.0)
        self.assertEqual(result["trade_count"], 0)
        self.assertEqual(result["max_drawdown"], 0.0)
        self.assertEqual(result["total_return"], 0.0)

    def test_round_trip_same_price_loses_fees(self):
        # 100 买入、100 卖出：期末 = 10000 / 1.001 * 0.999
        result = run_signal_backtest(
            days(4), [100.0] * 4, [100.0] * 4, [1, 0, 0, 0], fee_rate=0.001
        )
        self.assertAlmostEqual(
            result["final_equity"], 10000.0 / 1.001 * 0.999, places=8
        )

    def test_zero_fee_round_trip_no_loss(self):
        result = run_signal_backtest(
            days(4), [100.0] * 4, [100.0] * 4, [1, 0, 0, 0], fee_rate=0.0
        )
        self.assertAlmostEqual(result["final_equity"], 10000.0, places=8)


class TestInvariants(unittest.TestCase):
    def test_no_negative_cash_or_short(self):
        opens = [100, 101, 99, 105, 110, 95, 90, 100, 108, 112]
        closes = [101, 99, 105, 110, 95, 90, 100, 108, 112, 115]
        signals = [1, 0, 1, 1, 0, 0, 1, 0, 1, 1]
        result = run_signal_backtest(
            days(10),
            [float(x) for x in opens],
            [float(x) for x in closes],
            signals,
        )
        for row in result["equity_curve"]:
            self.assertGreaterEqual(row["cash"], 0.0)
            self.assertGreaterEqual(row["quantity"], 0.0)
            self.assertGreater(row["equity"], 0.0)
            self.assertAlmostEqual(
                row["equity"], row["cash"] + row["quantity"] * row["close"],
                places=8,
            )
        sides = [t["side"] for t in result["trades"]]
        for i, side in enumerate(sides):
            self.assertEqual(side, "BUY" if i % 2 == 0 else "SELL")

    def test_deterministic(self):
        args = (
            days(4), [100.0, 100.0, 110.0, 105.0],
            [100.0, 105.0, 108.0, 103.0], [1, 1, 0, 0],
        )
        self.assertEqual(run_signal_backtest(*args), run_signal_backtest(*args))

    def test_annualized_hidden_under_one_year(self):
        result = run_signal_backtest(
            days(3), [100.0] * 3, [100.0, 110.0, 120.0], [1, 1, 1]
        )
        self.assertIsNone(result["annualized_return"])


class TestNoLookAhead(unittest.TestCase):
    def test_strategy_only_sees_data_up_to_today(self):
        seen = []

        def spy(opens_so_far, closes_so_far):
            seen.append((len(opens_so_far), len(closes_so_far)))
            return 0

        build_signals(spy, days(5), [1.0] * 5, [1.0] * 5)
        self.assertEqual(seen, [(1, 1), (2, 2), (3, 3), (4, 4), (5, 5)])

    def test_strategy_cannot_modify_original_data(self):
        closes = [1.0, 2.0, 3.0]

        def vandal(opens_so_far, closes_so_far):
            closes_so_far[-1] = 999.0
            return 0

        build_signals(vandal, days(3), [1.0] * 3, closes)
        self.assertEqual(closes, [1.0, 2.0, 3.0])

    def test_truncation_gives_same_past_signals(self):
        def strategy(opens_so_far, closes_so_far):
            # 今天收盘高于过去所有收盘 -> 持有
            return 1 if closes_so_far[-1] >= max(closes_so_far) else 0

        closes = [100.0, 105.0, 103.0, 110.0, 108.0, 120.0, 90.0, 95.0]
        opens = [100.0] + closes[:-1]
        full = build_signals(strategy, days(8), opens, closes)
        for t in range(1, 8):
            part = build_signals(
                strategy, days(t + 1), opens[: t + 1], closes[: t + 1]
            )
            self.assertEqual(part, full[: t + 1])

    def test_changing_future_does_not_change_today_signal(self):
        def strategy(opens_so_far, closes_so_far):
            return 1 if closes_so_far[-1] > closes_so_far[0] else 0

        a = [100.0, 110.0, 120.0, 130.0]
        b = [100.0, 110.0, 1.0, 1.0]       # 只改了未来
        sa = build_signals(strategy, days(4), a, a)
        sb = build_signals(strategy, days(4), b, b)
        self.assertEqual(sa[:2], sb[:2])


class TestValidation(unittest.TestCase):
    def run_bad(self, **overrides):
        args = dict(
            dates=days(3), opens=[100.0, 100.0, 100.0],
            closes=[100.0, 100.0, 100.0], signals=[0, 0, 0],
            initial_cash=10000.0, fee_rate=0.001,
        )
        args.update(overrides)
        with self.assertRaises(ValueError):
            run_signal_backtest(**args)

    def test_nan_inf_prices(self):
        self.run_bad(opens=[100.0, NAN, 100.0])
        self.run_bad(closes=[100.0, INF, 100.0])

    def test_non_positive_prices(self):
        self.run_bad(opens=[100.0, 0.0, 100.0])
        self.run_bad(closes=[100.0, -1.0, 100.0])

    def test_length_mismatch(self):
        self.run_bad(signals=[0, 0])
        self.run_bad(opens=[100.0, 100.0])
        self.run_bad(dates=days(2))

    def test_too_few_candles(self):
        self.run_bad(dates=days(1), opens=[100.0], closes=[100.0], signals=[0])

    def test_dates_not_increasing(self):
        self.run_bad(dates=[0, DAY, DAY])

    def test_bad_signals(self):
        for bad in (2, -1, True, 1.0, None, "1"):
            self.run_bad(signals=[0, bad, 0])

    def test_bad_cash_and_fee(self):
        for bad in (0.0, -5.0, NAN, INF):
            self.run_bad(initial_cash=bad)
        for bad in (-0.001, 1.0, NAN, INF):
            self.run_bad(fee_rate=bad)

    def test_build_signals_length_mismatch(self):
        with self.assertRaises(ValueError):
            build_signals(lambda o, c: 0, days(3), [1.0] * 2, [1.0] * 3)


class TestLoadPriceData(unittest.TestCase):
    def write_csv(self, rows):
        temp = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="",
            suffix=".csv", delete=False,
        )
        self.addCleanup(lambda: Path(temp.name).unlink(missing_ok=True))
        writer = csv.writer(temp)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        writer.writerows(rows)
        temp.close()
        return temp.name

    def test_loads_open_and_close(self):
        path = self.write_csv([
            [str(1 * DAY), "100", "110", "90", "105", "1000"],
            [str(2 * DAY), "104", "112", "95", "108", "1000"],
        ])
        dates, opens, closes = load_price_data(path, now_ms=10 * DAY)
        self.assertEqual(dates, [DAY, 2 * DAY])
        self.assertEqual(opens, [100.0, 104.0])
        self.assertEqual(closes, [105.0, 108.0])

    def test_rejects_nan_open(self):
        path = self.write_csv([
            [str(1 * DAY), "nan", "110", "90", "105", "1000"],
            [str(2 * DAY), "104", "112", "95", "108", "1000"],
        ])
        with self.assertRaises(ValueError):
            load_price_data(path, now_ms=10 * DAY)


if __name__ == "__main__":
    unittest.main(verbosity=2)

