
"""实验方案预登记校验器。

只允许检查开发期实验方案，不负责运行回测。
正式实验结果应单独记录，不能混入预登记方案。
"""

import math
from datetime import datetime


REQUIRED_FIELDS = (
    "experiment_id",
    "hypothesis",
    "strategy_name",
    "parameters",
    "symbol",
    "timeframe",
    "dev_start",
    "dev_end",
    "signal_rule",
    "execution_rule",
    "fee_rate",
    "initial_cash",
    "evaluation_segment",
)

FORBIDDEN_RESULT_FIELDS = {
    "results",
    "total_return",
    "final_equity",
    "max_drawdown",
    "total_fees",
    "d11_decision",
}


def _parse_date(value, field_name):
    """严格检查 YYYY-MM-DD 日期。"""
    if not isinstance(value, str):
        raise ValueError(f"{field_name} 必须是 YYYY-MM-DD 格式")

    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ValueError(
            f"{field_name} 必须是有效的 YYYY-MM-DD 日期"
        )

    if parsed.strftime("%Y-%m-%d") != value:
        raise ValueError(f"{field_name} 必须是 YYYY-MM-DD 格式")

    return parsed.date()


def _finite_number(value, field_name):
    """检查数值有限，并拒绝布尔值。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} 必须是数值")

    if not math.isfinite(value):
        raise ValueError(f"{field_name} 必须是有限数值")

    return value


def validate_experiment_plan(plan):
    """校验预登记方案；有效时返回 True，否则抛出 ValueError。"""
    if not isinstance(plan, dict):
        raise ValueError("实验方案必须是字典")

    missing = [
        field for field in REQUIRED_FIELDS
        if field not in plan
    ]
    if missing:
        raise ValueError(
            "缺少必填字段：" + ", ".join(missing)
        )

    forbidden = FORBIDDEN_RESULT_FIELDS.intersection(plan)
    if forbidden:
        raise ValueError(
            "预登记方案不能包含实验结果字段："
            + ", ".join(sorted(forbidden))
        )

    text_fields = (
        "experiment_id",
        "hypothesis",
        "strategy_name",
        "symbol",
        "timeframe",
        "signal_rule",
        "execution_rule",
    )

    for field in text_fields:
        value = plan[field]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} 不能为空")

    if not isinstance(plan["parameters"], dict):
        raise ValueError("parameters 必须是字典")

    # 当前研究纪律：候选策略先只在开发期评估。
    if plan["evaluation_segment"] != "dev":
        raise ValueError(
            "当前只允许登记开发期实验，不能使用验证期或最终测试期"
        )

    start = _parse_date(plan["dev_start"], "dev_start")
    end = _parse_date(plan["dev_end"], "dev_end")

    if start > end:
        raise ValueError("dev_start 不能晚于 dev_end")

    fee_rate = _finite_number(plan["fee_rate"], "fee_rate")
    if fee_rate < 0:
        raise ValueError("fee_rate 不能小于零")

    initial_cash = _finite_number(
        plan["initial_cash"], "initial_cash"
    )
    if initial_cash <= 0:
        raise ValueError("initial_cash 必须大于零")

    return True

