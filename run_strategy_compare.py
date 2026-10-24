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

# =========================
# 事先固定的配置（不要根据回测结果来调整）
# =========================
SMA_WINDOW = 50

# 数据分段（按时间顺序）：
#   dev  前 50%：开发期，可以反复查看
#   val  中 25%：验证期，策略定型后再看
#   test 后 25%：最终测试，只在所有决定都锁定后看一次
# 默认只看开发期。查看其他分段需要手动修改下面这一行。
SEGMENT = "dev"


def split_segments(n):
    """把 n 根 K 线按时间顺序分成 dev / val / test 三段，返回 [start, end) 下标。"""
    a = int(n * 0.50)
    b = int(n * 0.75)
    return {"dev": (0, a), "val": (a, b), "test": (b, n)}


def summarize(name, result):
    ann = result["annualized_return"]
    return [
        name,
        f"{result['final_equity']:.2f}",
        f"{result['total_return'] * 100:.2f}%",
        "不足一年" if ann is None else f"{ann * 100:.2f}%",
        f"{result['max_drawdown'] * 100:.2f}%",
        str(result["trade_count"]),
        f"{result['total_fees']:.2f}",
    ]


def main():
    print("=" * 60)
    print("AI Crypto Trader - 策略对比（均线策略 vs 买入并持有）")
    print("=" * 60)

    if not DATA_FILE.exists():
        print("错误：找不到数据文件：", DATA_FILE)
        return

    try:
        dates, opens, closes = load_price_data(DATA_FILE)
    except ValueError as exc:
        print("数据校验未通过，停止。")
        print("-", exc)
        return

    segments = split_segments(len(closes))
    if SEGMENT not in segments:
        print("错误：SEGMENT 必须是 dev / val / test 之一。")
        return
    if SEGMENT == "test":
        print("警告：这是最终测试段，只应在所有决定锁定后查看一次。")

    start, end = segments[SEGMENT]

    # 信号用【全部历史】生成（每天只看得到截至当天的数据），再截取该分段
    sma_signals = build_signals(
        make_sma_strategy(SMA_WINDOW), dates, opens, closes
    )
    hold_signals = [1] * len(closes)

    seg = (dates[start:end], opens[start:end], closes[start:end])
    sma = run_signal_backtest(
        *seg, sma_signals[start:end],
        initial_cash=INITIAL_CASH, fee_rate=FEE_RATE,
    )
    hold = run_signal_backtest(
        *seg, hold_signals[start:end],
        initial_cash=INITIAL_CASH, fee_rate=FEE_RATE,
    )

    print("分段：", SEGMENT, f"（第 {start} 至 {end - 1} 根，共 {end - start} 根）")
    print(
        "区间：",
        format_utc_date(dates[start]), "至", format_utc_date(dates[end - 1]),
    )
    print(f"均线窗口：{SMA_WINDOW}（事先固定）  手续费：{FEE_RATE * 100}%")
    print("成交：信号次日开盘；两个策略使用同一引擎和同一成本")
    print("-" * 60)
    header = ["策略", "期末资产", "累计收益", "年化", "最大回撤", "交易数", "手续费"]
    rows = [
        header,
        summarize(f"SMA{SMA_WINDOW}", sma),
        summarize("买入持有", hold),
    ]
    for row in rows:
        print(" | ".join(row))
    print("-" * 60)
    print("说明：仅用于验证流程与口径，不构成策略有效性结论。")
    print("历史表现不代表未来收益。当前不进行真实交易。")
    print("=" * 60)


if __name__ == "__main__":
    main()

