import math
from dataclasses import dataclass


class RiskRejected(Exception):
    """风控拒绝了这个订单。"""


@dataclass(frozen=True)
class RiskLimits:
    """风控上限。frozen：创建后无法修改，策略或 AI 都改不了。"""
    max_order_notional: float      # 单笔最大名义金额（计价币）
    max_position_notional: float   # 买入后最大持仓名义金额（计价币）
    max_daily_loss: float          # 当日最大亏损（计价币，正数）

    def __post_init__(self):
        for name in ("max_order_notional", "max_position_notional", "max_daily_loss"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} 必须是大于 0 的有限数字。")


class RiskGuard:
    """
    独立风控：只做多、无杠杆。
    - 单笔名义金额不得超过上限
    - 买入后的持仓名义金额不得超过上限
    - 卖出数量不得超过当前持仓（不做空）
    - 当日亏损达到上限后，只拒绝买入，仍允许卖出以降低风险
    """

    def __init__(self, limits):
        if not isinstance(limits, RiskLimits):
            raise TypeError("limits 必须是 RiskLimits。")
        self._limits = limits
        self._daily_pnl = 0.0

    @property
    def limits(self):
        return self._limits

    @property
    def daily_pnl(self):
        return self._daily_pnl

    def record_pnl(self, pnl):
        if not isinstance(pnl, (int, float)) or isinstance(pnl, bool) or not math.isfinite(pnl):
            raise ValueError("pnl 必须是有限数字。")
        self._daily_pnl += pnl

    def reset_day(self):
        self._daily_pnl = 0.0

    def check(self, side, size, ref_price, position_size):
        if side not in ("buy", "sell"):
            raise RiskRejected("side 必须是 buy 或 sell。")
        for name, value in (("size", size), ("ref_price", ref_price)):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise RiskRejected(f"{name} 必须是大于 0 的有限数字。")
        if (
            isinstance(position_size, bool)
            or not isinstance(position_size, (int, float))
            or not math.isfinite(position_size)
            or position_size < 0
        ):
            raise RiskRejected("当前持仓无效。")

        limits = self._limits
        notional = size * ref_price
        if notional > limits.max_order_notional:
            raise RiskRejected("超过单笔名义金额上限。")

        if side == "buy":
            if self._daily_pnl <= -limits.max_daily_loss:
                raise RiskRejected("当日亏损已达上限，禁止买入。")
            if (position_size + size) * ref_price > limits.max_position_notional:
                raise RiskRejected("买入后将超过持仓名义金额上限。")
        else:
            if size > position_size:
                raise RiskRejected("卖出数量超过当前持仓（不允许做空）。")

