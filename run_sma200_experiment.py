
import sys
from pathlib import Path

# 兼容 Pyto：确保当前项目目录可以被 Python 找到
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from backtest_engine import build_signals, run_signal_backtest
from csv_quality_check import format_utc_date
from run_backtest import DATA_FILE, load_price_data, INITIAL_CASH, FEE_RATE
from strategy_sma import make_sma_strategy
from run_strategy_compare import split_segments


# E2：预先锁定的参数，不根据结果调整
SMA_WINDOW = 200
SEGMENT = "dev"
MAX_FEE_BUDGET = INITIAL_CASH * 0.05
MIN_DRAWDOWN_IMPROVEMENT = 0.05


def return_drawdown_ratio(result):
    """累计收益 / 最大回撤绝对值；回撤为零时不定义。"""
    drawdown = abs(result["max_drawdown"])
    if drawdown == 0:
        return None
    return result["total_return"] / drawdown


def evaluate_d11(strategy, baseline):
    """
    按 DECISIONS.md 中已锁定的 D11 判定。
    返回每项判定、收益回撤比和最终结论。
    """
    strategy_ratio = return_drawdown_ratio(strategy)
    baseline_ratio = return_drawdown_ratio(baseline)

    # 条件 1：收益为正，且收益回撤比严格高于基准。
    # 任一比值未定义，都不自动通过。
    condition_1 = (
        strategy["total_return"] > 0
        and strategy_ratio is not None
        and baseline_ratio is not None
        and strategy_ratio > baseline_ratio
    )

    # 条件 2：策略回撤绝对值至少比基准低 5 个百分点。
    strategy_dd = abs(strategy["max_drawdown"])
    baseline_dd = abs(baseline["max_drawdown"])
    condition_2 = (
        strategy_dd <= baseline_dd - MIN_DRAWDOWN_IMPROVEMENT
    )

    # 条件 3：策略自身累计手续费不得超过初始资金的 5%。
    condition_3 = strategy["total_fees"] <= MAX_FEE_BUDGET

    return {
        "strategy_ratio": strategy_ratio,
        "baseline_ratio": baseline_ratio,
        "condition_1": condition_1,
        "condition_2": condition_2,
        "condition_3": condition_3,
        "passed": condition_1 and condition_2 and condition_3,
    }


def format_ratio(value):
    if value is None:
        return "未定义（回撤为零）"
    return f"{value:.4f}"


def print_result(name, result):
    annualized = result["annualized_return"]
    annualized_text = (
        "不足一年"
        if annualized is None
        else f"{annualized * 100:.2f}%"
    )

    print(f"\n{name}")
    print(f"  期末资产：{result['final_equity']:.2f} USDT")
    print(f"  累计收益：{result['total_return'] * 100:.2f}%")
    print(f"  年化收益：{annualized_text}")
    print(f"  最大回撤：{result['max_drawdown'] * 100:.2f}%")
    print(f"  收益回撤比：{format_ratio(return_drawdown_ratio(result))}")
    print(f"  交易数：{result['trade_count']}")
    print(f"  累计手续费：{result['total_fees']:.2f} USDT")


def main():
    print("=" * 60)
    print("AI Crypto Trader - E2 SMA200 开发期实验")
    print("=" * 60)
    print("固定窗口：200 日")
    print("单边手续费：", FEE_RATE * 100, "%")
    print("初始资金：", INITIAL_CASH, "USDT")
    print("评估分段：仅开发期")
    print("执行规则：收盘产生信号，次日开盘成交")

    if not DATA_FILE.exists():
        print("错误：找不到数据文件：", DATA_FILE)
        return

    try:
        dates, opens, closes = load_price_data(DATA_FILE)
    except ValueError as exc:
        print("数据校验未通过，停止实验。")
        print("-", exc)
        return

    # 只确定开发期的结束位置；不计算或输出 val/test 的策略结果。
    segments = split_segments(len(closes))
    start, end = segments[SEGMENT]

    dev_dates = dates[start:end]
    dev_opens = opens[start:end]
    dev_closes = closes[start:end]

    if len(dev_closes) < SMA_WINDOW + 2:
        print("开发期数据不足以进行 SMA200 回测，停止。")
        return

    signals = build_signals(
        make_sma_strategy(SMA_WINDOW),
        dev_dates,
        dev_opens,
        dev_closes,
    )

    # 同一回测引擎、同一分段、同一手续费口径。
    strategy_result = run_signal_backtest(
        dev_dates,
        dev_opens,
        dev_closes,
        signals,
        initial_cash=INITIAL_CASH,
        fee_rate=FEE_RATE,
    )

    # 信号恒为 1 的买入持有基准。
    baseline_result = run_signal_backtest(
        dev_dates,
        dev_opens,
        dev_closes,
        [1] * len(dev_closes),
        initial_cash=INITIAL_CASH,
        fee_rate=FEE_RATE,
    )

    print("-" * 60)
    print("开发期：", format_utc_date(dev_dates[0]),
          "至", format_utc_date(dev_dates[-1]))
    print("开发期 K 线数：", len(dev_closes))
    print("注意：SMA200 预热期按既定规则保持空仓。")
    print_result("E2：SMA200", strategy_result)
    print_result("基准：买入持有", baseline_result)

    decision = evaluate_d11(strategy_result, baseline_result)

    print("\n" + "=" * 60)
    print("D11 自动判定")
    print("=" * 60)
    print(
        "条件 1（收益为正且收益回撤比严格高于基准）：",
        "通过" if decision["condition_1"] else "未通过",
    )
    print(
        "条件 2（最大回撤至少改善 5 个百分点）：",
        "通过" if decision["condition_2"] else "未通过",
    )
    print(
        "条件 3（策略累计手续费不超过 500 USDT）：",
        "通过" if decision["condition_3"] else "未通过",
    )
    print("手续费预算：", f"{MAX_FEE_BUDGET:.2f} USDT")

    if decision["passed"]:
        print("E2 判定：开发期通过，可按流程进入下一阶段审查。")
    else:
        print("E2 判定：开发期未通过，不调整窗口，不放宽 D11。")

    print("=" * 60)
    print("研究边界：本程序只输出开发期结果。")
    print("验证期与最终测试期尚未评估；当前不进行真实交易。")
    print("历史回测结果不代表未来收益。")
    print("=" * 60)


if __name__ == "__main__":
    main()

