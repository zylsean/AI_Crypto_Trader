
import sys
import unittest
from pathlib import Path

# 兼容 Pyto：先把当前项目目录加入模块搜索路径
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from run_sma200_experiment import evaluate_d11


def result(total_return, max_drawdown, total_fees):
    """构造只供规则测试使用的模拟回测结果。"""
    return {
        "total_return": total_return,
        "max_drawdown": max_drawdown,
        "total_fees": total_fees,
    }


class TestD11Evaluation(unittest.TestCase):

    def test_all_three_conditions_pass(self):
        strategy = result(0.40, -0.15, 500.00)
        baseline = result(0.50, -0.20, 10.00)
        decision = evaluate_d11(strategy, baseline)

        self.assertTrue(decision["condition_1"])
        self.assertTrue(decision["condition_2"])
        self.assertTrue(decision["condition_3"])
        self.assertTrue(decision["passed"])

    def test_equal_five_percentage_point_improvement_passes(self):
        strategy = result(0.40, -0.15, 100.00)
        baseline = result(0.50, -0.20, 10.00)
        decision = evaluate_d11(strategy, baseline)

        self.assertTrue(decision["condition_2"])

    def test_fee_budget_exactly_500_passes(self):
        strategy = result(0.40, -0.15, 500.00)
        baseline = result(0.50, -0.20, 10.00)
        decision = evaluate_d11(strategy, baseline)

        self.assertTrue(decision["condition_3"])

    def test_fee_budget_over_500_fails(self):
        strategy = result(0.40, -0.15, 500.01)
        baseline = result(0.50, -0.20, 10.00)
        decision = evaluate_d11(strategy, baseline)

        self.assertFalse(decision["condition_3"])
        self.assertFalse(decision["passed"])

    def test_non_positive_strategy_return_fails(self):
        strategy = result(0.00, -0.15, 100.00)
        baseline = result(0.50, -0.20, 10.00)
        decision = evaluate_d11(strategy, baseline)

        self.assertFalse(decision["condition_1"])
        self.assertFalse(decision["passed"])

    def test_strategy_drawdown_not_improved_enough(self):
        strategy = result(0.40, -0.16, 100.00)
        baseline = result(0.50, -0.20, 10.00)
        decision = evaluate_d11(strategy, baseline)

        self.assertFalse(decision["condition_2"])
        self.assertFalse(decision["passed"])

    def test_ratio_must_be_strictly_higher(self):
        # 两者收益回撤比都等于 2.5，不满足“严格高于”。
        strategy = result(0.375, -0.15, 100.00)
        baseline = result(0.50, -0.20, 10.00)
        decision = evaluate_d11(strategy, baseline)

        self.assertFalse(decision["condition_1"])
        self.assertFalse(decision["passed"])

    def test_zero_strategy_drawdown_ratio_is_undefined(self):
        strategy = result(0.40, 0.00, 100.00)
        baseline = result(0.50, -0.20, 10.00)
        decision = evaluate_d11(strategy, baseline)

        self.assertIsNone(decision["strategy_ratio"])
        self.assertFalse(decision["condition_1"])
        self.assertFalse(decision["passed"])

    def test_zero_baseline_drawdown_does_not_auto_pass(self):
        strategy = result(0.40, -0.10, 100.00)
        baseline = result(0.50, 0.00, 10.00)
        decision = evaluate_d11(strategy, baseline)

        self.assertIsNone(decision["baseline_ratio"])
        self.assertFalse(decision["condition_1"])
        self.assertFalse(decision["condition_2"])
        self.assertFalse(decision["passed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
