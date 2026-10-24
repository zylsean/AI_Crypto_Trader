import csv
import time
import sys
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

DATA_FILE = Path(__file__).parent / "BTC_USDT_1d.csv"


def load_close_prices(path):
    """读取并返回 BTC 日线日期与收盘价。"""

    dates = []
    closes = []

    with Path(path).open(
        "r", encoding="utf-8-sig", newline=""
    ) as file:
        reader = csv.DictReader(file)

        for row in reader:
            dates.append(int(row["timestamp"]))
            closes.append(float(row["close"]))

    if len(closes) < 2:
        raise ValueError("至少需要两根日线才能进行回测。")

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
    1. 在第一根 K 线的收盘价买入。
    2. 买入时支付单边手续费。
    3. 买入后一直持有，不再交易。
    4. 按每日收盘价计算持仓市值。
    5. 暂不考虑滑点、税费和其他交易成本。
    """

    if len(dates) != len(closes):
        raise ValueError("日期数量与收盘价数量不一致。")

    if len(closes) < 2:
        raise ValueError("至少需要两根日线。")

    if initial_cash <= 0:
        raise ValueError("初始资金必须大于 0。")

    if not 0 <= fee_rate < 1:
        raise ValueError("手续费率必须在 [0, 1) 范围内。")

    if any(price <= 0 for price in closes):
        raise ValueError("收盘价必须大于 0。")

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

    total_return = final_equity / initial_cash - 1.0

    elapsed_days = (
        dates[-1] - dates[0]
    ) / 86_400_000

    if elapsed_days > 0 and final_equity > 0:
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

    # 回测前先重新校验真实数据
    now_ms = int(time.time() * 1000)
    errors, summary = validate_csv(DATA_FILE, now_ms=now_ms)

    if errors:
        print("数据校验未通过，停止回测。")
        print("问题数量：", len(errors))

        for error in errors[:10]:
            print("-", error)

        return

    dates, closes = load_close_prices(DATA_FILE)

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
    print("期末资产：", round(result["final_equity"], 2), "USDT")
    print("累计收益率：", round(result["total_return"] * 100, 2), "%")

    if result["annualized_return"] is not None:
        print(
            "年化收益率：",
            round(result["annualized_return"] * 100, 2),
            "%",
        )

    print(
        "最大回撤：",
        round(result["max_drawdown"] * 100, 2),
        "%",
    )

    print("-" * 55)
    print("说明：这是历史基准回测，不代表未来收益。")
    print("当前不进行真实交易。")
    print("=" * 55)


if __name__ == "__main__":
    main()
