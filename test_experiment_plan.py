
import unittest

from experiment_plan import validate_experiment_plan


def make_plan():
    return {
        "experiment_id": "E3",
        "hypothesis": "待验证的明确假设",
        "strategy_name": "ExampleStrategy",
        "parameters": {"window": 50},
        "symbol": "BTC-USDT",
        "timeframe": "1d",
        "dev_start": "2023-07-13",
        "dev_end": "2025-03-02",
        "signal_rule": "按预先固定的规则产生信号",
        "execution_rule": "当日收盘产生信号，次日开盘执行",
        "fee_rate": 0.001,
        "initial_cash": 10000.0,
        "evaluation_segment": "dev",
    }


class TestExperimentPlan(unittest.TestCase):

    def test_valid_plan_passes(self):
        self.assertTrue(validate_experiment_plan(make_plan()))

    def test_missing_field_fails(self):
        plan = make_plan()
        del plan["hypothesis"]

        with self.assertRaisesRegex(ValueError, "缺少必填字段"):
            validate_experiment_plan(plan)

    def test_empty_hypothesis_fails(self):
        plan = make_plan()
        plan["hypothesis"] = "  "

        with self.assertRaisesRegex(ValueError, "不能为空"):
            validate_experiment_plan(plan)

    def test_validation_segment_is_rejected(self):
        plan = make_plan()
        plan["evaluation_segment"] = "validation"

        with self.assertRaisesRegex(ValueError, "开发期"):
            validate_experiment_plan(plan)

    def test_invalid_date_fails(self):
        plan = make_plan()
        plan["dev_start"] = "2025-02-30"

        with self.assertRaises(ValueError):
            validate_experiment_plan(plan)

    def test_reversed_dates_fail(self):
        plan = make_plan()
        plan["dev_start"] = "2025-03-03"

        with self.assertRaisesRegex(ValueError, "不能晚于"):
            validate_experiment_plan(plan)

    def test_negative_fee_fails(self):
        plan = make_plan()
        plan["fee_rate"] = -0.001

        with self.assertRaisesRegex(ValueError, "不能小于零"):
            validate_experiment_plan(plan)

    def test_non_positive_initial_cash_fails(self):
        plan = make_plan()
        plan["initial_cash"] = 0

        with self.assertRaisesRegex(ValueError, "必须大于零"):
            validate_experiment_plan(plan)

    def test_result_fields_are_rejected(self):
        plan = make_plan()
        plan["total_return"] = 1.25

        with self.assertRaisesRegex(ValueError, "实验结果字段"):
            validate_experiment_plan(plan)


if __name__ == "__main__":
    unittest.main(verbosity=2)

