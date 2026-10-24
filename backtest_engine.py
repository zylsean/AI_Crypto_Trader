import math

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


def build_signals(strategy, dates, opens, closes):
    """
    用策略函数生成每日信号，并在结构上杜绝使用未来数据。

    strategy(opens_so_far, closes_so_far) -> 0 或 1
    第 t 天调用时，只会收到截至第 t 天收盘的数据（长度 t+1 的副本），
    策略拿不到第 t+1 天及以后的任何数据。
    返回的 signals[t] 表示：第 t 天收盘后决定的目标仓位。
    """
    n = len(closes)
    if not (len(dates) == len(opens) == n):
        raise ValueError("日期、开盘价、收盘价数量不一致。")
    signals = []
    for t in range(n):
        signals.append(strategy(opens[: t + 1], closes[: t + 1]))
    return signals


def run_signal_backtest(
    dates,
    opens,
    closes,
    signals,
    initial_cash=10000.0,
    fee_rate=0.001,
):
    """
    按信号回测（只做多、不做空、全仓或空仓）。

    成交规则（固定）：
    1. signals[t] 是第 t 天收盘后决定的目标仓位：1=持有，0=空仓。
    2. 它在第 t+1 天的【开盘价】成交。最后一天的信号没有下一个开盘，被忽略。
    3. 目标为 1 且当前空仓：用全部现金买入，手续费加在名义金额之上。
       目标为 0 且当前持仓：全部卖出，卖出手续费从所得中扣除。
    4. 每天按收盘价计算账户总市值 = 现金 + 持仓数量 × 收盘价。
    5. 最大回撤：账户级、含手续费，峰值从初始资金起算（与基准一致）。
    6. 不考虑滑点、税费和其他成本。
    """
    # ---- 输入验证 ----
    if not _is_finite_number(initial_cash) or initial_cash <= 0:
        raise ValueError("初始资金必须是大于 0 的有限数字。")
    if not _is_finite_number(fee_rate) or not 0 <= fee_rate < 1:
        raise ValueError("手续费率必须是 [0, 1) 范围内的有限数字。")
    n = len(closes)
    if not (len(dates) == len(opens) == len(signals) == n):
        raise ValueError("日期、开盘价、收盘价、信号数量不一致。")
    if n < 2:
        raise ValueError("至少需要两根日线。")
    if not all(_is_finite_number(d) for d in dates):
        raise ValueError("日期必须是有限数字。")
    if not all(_is_finite_number(p) and p > 0 for p in opens):
        raise ValueError("开盘价必须是大于 0 的有限数字。")
    if not all(_is_finite_number(p) and p > 0 for p in closes):
        raise ValueError("收盘价必须是大于 0 的有限数字。")
    if any(dates[i] <= dates[i - 1] for i in range(1, n)):
        raise ValueError("日期必须严格递增。")
    if not all(type(s) is int and s in (0, 1) for s in signals):
        raise ValueError("信号必须是整数 0 或 1。")

    cash = initial_cash
    quantity = 0.0
    equity_curve = []
    trades = []
    total_fees = 0.0
    peak_equity = initial_cash
    max_drawdown = 0.0

    for t in range(n):
        # 第 t 天开盘：执行第 t-1 天收盘后做出的决定
        if t >= 1:
            target = signals[t - 1]
            price = opens[t]
            if target == 1 and quantity == 0:
                quantity = cash / (price * (1 + fee_rate))
                fee = quantity * price * fee_rate
                cash = 0.0
                total_fees += fee
                trades.append({
                    "timestamp": dates[t], "side": "BUY",
                    "price": price, "quantity": quantity, "fee": fee,
                })
            elif target == 0 and quantity > 0:
                gross = quantity * price
                fee = gross * fee_rate
                cash = gross - fee
                total_fees += fee
                trades.append({
                    "timestamp": dates[t], "side": "SELL",
                    "price": price, "quantity": quantity, "fee": fee,
                })
                quantity = 0.0

        # 第 t 天收盘：按市值记账
        equity = cash + quantity * closes[t]
        equity_curve.append({
            "timestamp": dates[t],
            "close": closes[t],
            "cash": cash,
            "quantity": quantity,
            "equity": equity,
        })
        peak_equity = max(peak_equity, equity)
        max_drawdown = min(max_drawdown, equity / peak_equity - 1.0)

    final_equity = equity_curve[-1]["equity"]
    if quantity > 0:
        final_equity_after_exit_fee = cash + quantity * closes[-1] * (
            1 - fee_rate
        )
    else:
        final_equity_after_exit_fee = final_equity
    total_return = final_equity / initial_cash - 1.0

    elapsed_days = (dates[-1] - dates[0]) / DAY_MS
    if elapsed_days >= MIN_DAYS_FOR_ANNUALIZED and final_equity > 0:
        years = elapsed_days / 365.25
        annualized_return = (final_equity / initial_cash) ** (1 / years) - 1
    else:
        annualized_return = None

    return {
        "initial_cash": initial_cash,
        "fee_rate": fee_rate,
        "first_date": dates[0],
        "last_date": dates[-1],
        "final_equity": final_equity,
        "final_equity_after_exit_fee": final_equity_after_exit_fee,
        "total_return": total_return,
        "annualized_return": annualized_return,
        "max_drawdown": max_drawdown,
        "trade_count": len(trades),
        "total_fees": total_fees,
        "trades": trades,
        "equity_curve": equity_curve,
    }

