import csv
import math
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from csv_quality_check import validate_csv, format_utc_date

# =========================
# 回测配置
# =========================
INITIAL_CASH = 10000.0
# 单边手续费：0.1%
FEE_RATE = 0.001
DATA_FILE = SCRIPT_DIR / "BTC_USDT_1d.csv"

DAY_MS = 86_400_000
# 不满一年的区间，年化收益没有参考意义，不显示
MIN_DAYS_FOR_ANNUALIZED = 365


def _is_finite_number(value):
    """是真正的有限数字（排除 NaN、正负无穷、字符串、布尔值）。"""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def load_price_data(path, now_ms=None):
    """
    读取并返回 (日期, 开盘价, 收盘价) 三个列表。

    加载前会先调用 validate_csv 校验，校验不通过就抛出 ValueError，
    因此任何调用路径都不会绕过数据校验。
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    errors, _ = validate_csv(path, now_ms=now_ms)
    if errors:
        preview = "；".join(errors[:5])
        raise ValueError(
            f"CSV 数据校验未通过（共 {len(errors)} 个问题）：{preview}"
        )

    dates = []
    opens = []
    closes = []
    with Path(path).open(
        "r", encoding="utf-8-sig", newline=""
    ) as file:
        reader = csv.DictReader(file)
        for row in reader:
            dates.append(int(row["timestamp"]))
            opens.append(float(row["open"]))
            closes.append(float(row["close"]))

    if len(closes) < 2:
        raise ValueError("至少需要两根日线才能进行回测。")

    return dates, opens, closes


def load_close_prices(path, now_ms=None):
    """读取并返回 BTC 日线日期与收盘价（基准回测使用）。"""
    dates, _, closes = load_price_data(path, now_ms=now_ms)
    return dates, closes


def run_buy_and_hold(
    dates,
    closes,
    initial_cash=INITIAL_CASH,
    fee_rate=FEE_RATE,
):
    """
    买入并持有基准。

    假设：
    1. 在第一根 K 线的收盘价买入（基准没有信号，不存在未来数据问题）。
    2. 买入时支付单边手续费，手续费加在名义金额之上。
    3. 买入后一直持有，不再交易。
    4. 按每日收盘价计算持仓市值。
    5. 暂不考虑滑点、税费和其他交易成本。

    口径：
    - 最大回撤是"账户级、含手续费"：峰值从初始资金起算，
      所以买入手续费会体现在首日回撤里（首日回撤 = -fee/(1+fee)）。
    - final_equity 是期末按市值，不扣卖出手续费；
      final_equity_after_exit_fee 是假设期末全部卖出后的净值。
    """
    # ---- 输入验证：先查数值类型和有限性，再查其他 ----
    if not _is_finite_number(initial_cash) or initial_cash <= 0:
        raise ValueError("初始资金必须是大于 0 的有限数字。")
    if not _is_finite_number(fee_rate) or not 0 <= fee_rate < 1:
        raise ValueError("手续费率必须是 [0, 1) 范围内的有限数字。")
    if len(dates) != len(closes):
        raise ValueError("日期数量与收盘价数量不一致。")
    if len(closes) < 2:
        raise ValueError("至少需要两根日线。")
    if not all(_is_finite_number(d) for d in dates):
        raise ValueError("日期必须是有限数字。")
    if not all(_is_finite_number(p) and p > 0 for p in closes):
        raise ValueError("收盘价必须是大于 0 的有限数字。")
    if any(
        dates[i] <= dates[i - 1]
        for i in range(1, len(dates))
    ):
        raise ValueError("日期必须严格递增。")

    # 买入费用包含在初始资金中
    first_price = closes[0]
    quantity = initial_cash / (first_price * (1 + fee_rate))

    equity_curve = []
    peak_equity = initial_cash
    max_drawdown = 0.0

    for timestamp, price in zip(dates, closes):
        equity = quantity * price
        equity_curve.append({
            "timestamp": timestamp,
            "close": price,
            "equity": equity,
        })
        peak_equity = max(peak_equity, equity)
        drawdown = equity / peak_equity - 1.0
        max_drawdown = min(max_drawdown, drawdown)

    final_equity = equity_curve[-1]["equity"]
    final_equity_after_exit_fee = final_equity * (1 - fee_rate)
    total_return = final_equity / initial_cash - 1.0

    elapsed_days = (dates[-1] - dates[0]) / DAY_MS

    if elapsed_days >= MIN_DAYS_FOR_ANNUALIZED:
        years = elapsed_days / 365.25
        annualized_return = (
            (final_equity / initial_cash) ** (1 / years) - 1
        )
    else:
        annualized_return = None

    return {
        "initial_cash": initial_cash,
        "fee_rate": fee_rate,
        "first_date": dates[0],
        "last_date": dates[-1],
        "first_price": first_price,
        "last_price": closes[-1],
        "quantity": quantity,
        "final_equity": final_equity,
        "final_equity_after_exit_fee": final_equity_after_exit_fee,
        "total_return": total_return,
        "annualized_return": annualized_return,
        "max_drawdown": max_drawdown,
        "equity_curve": equity_curve,
    }


def main():
    print("=" * 55)
    print("AI Crypto Trader - BTC 买入并持有基准回测")
    print("=" * 55)

    if not DATA_FILE.exists():
        print("错误：找不到数据文件：", DATA_FILE)
        return

    # 加载函数内部会先校验数据，校验不通过会抛出 ValueError
    try:
        dates, closes = load_close_prices(DATA_FILE)
    except ValueError as exc:
        print("数据校验未通过，停止回测。")
        print("-", exc)
        return

    result = run_buy_and_hold(dates, closes)

    print("数据来源：OKX BTC-USDT 日线")
    print("数据条数：", len(closes))
    print(
        "数据区间：",
        format_utc_date(result["first_date"]),
        "至",
        format_utc_date(result["last_date"]),
    )
    print("初始资金：", round(result["initial_cash"], 2), "USDT")
    print("单边手续费：", result["fee_rate"] * 100, "%")
    print("首日收盘价：", result["first_price"])
    print("末日收盘价：", result["last_price"])
    print("持有 BTC 数量：", round(result["quantity"], 8))
    print("期末资产（按市值）：", round(result["final_equity"], 2), "USDT")
    print(
        "期末资产（扣卖出手续费）：",
        round(result["final_equity_after_exit_fee"], 2),
        "USDT",
    )
    print("累计收益率：", round(result["total_return"] * 100, 2), "%")
    if result["annualized_return"] is not None:
        print(
            "年化收益率：",
            round(result["annualized_return"] * 100, 2),
            "%",
        )
    else:
        print("年化收益率：区间不足一年，不显示")
    print(
        "最大回撤（账户级，含手续费）：",
        round(result["max_drawdown"] * 100, 2),
        "%",
    )
    print("-" * 55)
    print("说明：这是历史基准回测，不代表未来收益。")
    print("当前不进行真实交易。")
    print("=" * 55)


if __name__ == "__main__":
    main()
