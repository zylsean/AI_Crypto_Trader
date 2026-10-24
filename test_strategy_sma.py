import sys
import unittest
from pathlib import Path

# 兼容 Pyto：确保当前项目目录可以被 Python 找到
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from backtest_engine import build_signals, run_signal_backtest
from run_strategy_compare import split_segments
from strategy_sma import make_sma_strategy

DAY = 86_400_000


def days(n):
    return [i * DAY for i in range(n)]


class TestSmaStrategy(unittest.TestCase):
    def test_warmup_is_flat(self):
        strategy = make_sma_strategy(3)
        self.assertEqual(strategy([1.0], [1.0]), 0)
        self.assertEqual(strategy([1.0, 1.0], [1.0, 5.0]), 0)

    def test_hand_calculated_signals(self):
        # 窗口 3，收盘 [10, 10, 10, 12]
        # t=2：平均 10，收盘 10，不严格高于 -> 0
        # t=3：平均 10.667，收盘 12 -> 1
        closes = [10.0, 10.0, 10.0, 12.0]
        signals = build_signals(
            make_sma_strategy(3), days(4), closes, closes
        )
        self.assertEqual(signals, [0, 0, 0, 1])

    def test_equal_to_average_is_flat(self):
        strategy = make_sma_strategy(2)
        self.assertEqual(strategy([1.0, 1.0], [10.0, 10.0]), 0)

    def test_below_average_is_flat(self):
        strategy = make_sma_strategy(2)
        self.assertEqual(strategy([1.0, 1.0], [12.0, 8.0]), 0)

    def test_invalid_window(self):
        for bad in (0, 1, -3, 2.5, "5", None, True):
            with self.assertRaises(ValueError):
                make_sma_strategy(bad)

    def test_no_look_ahead_truncation(self):
        closes = [100.0, 102.0, 101.0, 105.0, 107.0, 103.0, 99.0, 104.0, 110.0]
        opens = [100.0] + closes[:-1]
        strategy = make_sma_strategy(3)
        full = build_signals(strategy, days(9), opens, closes)
        for t in range(1, 9):
            part = build_signals(
                strategy, days(t + 1), opens[: t + 1], closes[: t + 1]
            )
            self.assertEqual(part, full[: t + 1])

    def test_future_change_does_not_change_past_signals(self):
        a = [100.0, 102.0, 101.0, 105.0, 107.0, 103.0]
        b = a[:4] + [1.0, 1.0]            # 只改了第 4 天以后
        strategy = make_sma_strategy(3)
        sa = build_signals(strategy, days(6), a, a)
        sb = build_signals(strategy, days(6), b, b)
        self.assertEqual(sa[:4], sb[:4])


class TestSmaWithEngine(unittest.TestCase):
    def test_end_to_end_hand_calculated(self):
        # 窗口 2，连续市场（开盘 = 前一日收盘）
        # 收盘 [100,100,110,120,100,90] -> 信号 [0,0,1,1,0,0]
        # 第 3 天开盘 110 买入；第 5 天开盘 100 卖出（手续费 0）
        # 期末 = 10000 / 110 * 100 = 9090.909...
        closes = [100.0, 100.0, 110.0, 120.0, 100.0, 90.0]
        opens = [100.0, 100.0, 100.0, 110.0, 120.0, 100.0]
        signals = build_signals(
            make_sma_strategy(2), days(6), opens, closes
        )
        self.assertEqual(signals, [0, 0, 1, 1, 0, 0])
        result = run_signal_backtest(
            days(6), opens, closes, signals, fee_rate=0.0
        )
        self.assertEqual(result["trade_count"], 2)
        self.assertEqual(result["trades"][0]["timestamp"], 3 * DAY)
        self.assertEqual(result["trades"][0]["price"], 110.0)
        self.assertEqual(result["trades"][1]["timestamp"], 5 * DAY)
        self.assertEqual(result["trades"][1]["price"], 100.0)
        self.assertAlmostEqual(
            result["final_equity"], 10000.0 / 110.0 * 100.0, places=6
        )


class TestSplitSegments(unittest.TestCase):
    def test_1199_candles(self):
        seg = split_segments(1199)
        self.assertEqual(seg["dev"], (0, 599))
        self.assertEqual(seg["val"], (599, 899))
        self.assertEqual(seg["test"], (899, 1199))

    def test_segments_cover_everything_without_overlap(self):
        for n in (10, 100, 1199):
            seg = split_segments(n)
            self.assertEqual(seg["dev"][0], 0)
            self.assertEqual(seg["dev"][1], seg["val"][0])
            self.assertEqual(seg["val"][1], seg["test"][0])
            self.assertEqual(seg["test"][1], n)


if __name__ == "__main__":
    unittest.main(verbosity=2)

