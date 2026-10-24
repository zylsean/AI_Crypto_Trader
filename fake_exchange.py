import math

from exchange_base import (
    Exchange,
    ExchangeRejected,
    ExchangeTimeout,
)


class SimulatedCrash(BaseException):
    """模拟程序在请求途中被杀。继承 BaseException，普通的异常处理抓不到它。"""


class FakeExchange(Exchange):
    """
    测试用假交易所，在内存里模拟订单和余额，可注入各种故障。

    注意：重复的客户端订单 ID 被拒绝，是我们【假设】的行为，
    真实交易所的行为要在 2c 用模拟盘实测后再校正。

    手续费模型（对应账户 feeType=0）：买入从收到的基础币里扣，卖出从收到的计价币里扣。
    """

    def __init__(self, balances=None, fee_rate=0.001, market_price=100.0,
                 base_ccy="BTC", quote_ccy="USDT"):
        self.balances = dict(balances or {base_ccy: 0.0, quote_ccy: 100000.0})
        self.fee_rate = fee_rate
        self.market_price = market_price
        self.base_ccy = base_ccy
        self.quote_ccy = quote_ccy
        self.orders = {}
        self.place_calls = 0
        self.reject_message = "simulated reject"
        self._next_place = []
        self._failures = {}
        self._counter = 0

    # ---------- 故障注入 ----------
    def set_next_place(self, *behaviors):
        """依次指定接下来每次下单的行为：fill / live / reject /
        timeout_executed / timeout_not_executed / crash_executed / crash_not_executed"""
        self._next_place = list(behaviors)

    def set_failures(self, method, count):
        """让某个方法接下来 count 次调用都抛出 ExchangeTimeout。"""
        self._failures[method] = count

    def _maybe_fail(self, method):
        if self._failures.get(method, 0) > 0:
            self._failures[method] -= 1
            raise ExchangeTimeout(f"simulated timeout in {method}")

    # ---------- 测试辅助 ----------
    def add_foreign_order(self, client_order_id="MANUAL1"):
        """加入一笔不是我们下的订单（例如网页上手动下的）。"""
        self._counter += 1
        key = client_order_id or f"foreign-{self._counter}"
        self.orders[key] = {
            "client_order_id": client_order_id,
            "order_id": f"X{self._counter}",
            "state": "live", "side": "buy", "size": 1.0,
            "filled_size": 0.0, "avg_price": 0.0, "fee": 0.0,
            "fee_ccy": self.base_ccy, "price": self.market_price,
        }

    def fill(self, client_order_id, qty):
        """让交易所对某笔订单成交 qty（累计成交量、余额、手续费同步更新）。"""
        order = self.orders[client_order_id]
        remaining = order["size"] - order["filled_size"]
        if qty <= 0 or qty > remaining + 1e-12:
            raise ValueError("成交数量无效。")
        price = order["price"]
        if order["side"] == "buy":
            fee = qty * self.fee_rate
            self.balances[self.quote_ccy] -= qty * price
            self.balances[self.base_ccy] += qty - fee
            order["fee_ccy"] = self.base_ccy
        else:
            fee = qty * price * self.fee_rate
            self.balances[self.base_ccy] -= qty
            self.balances[self.quote_ccy] += qty * price - fee
            order["fee_ccy"] = self.quote_ccy
        total = order["filled_size"] + qty
        order["avg_price"] = price
        order["fee"] += fee
        order["filled_size"] = total
        order["state"] = (
            "filled" if total >= order["size"] - 1e-12 else "partially_filled"
        )

    # ---------- 接口实现 ----------
    def place_order(self, client_order_id, side, size, order_type="market", price=None):
        self.place_calls += 1
        self._maybe_fail("place_order")
        if client_order_id in self.orders:
            raise ExchangeRejected("duplicate client order id")
        behavior = (
            self._next_place.pop(0) if self._next_place
            else ("fill" if order_type == "market" else "live")
        )
        if behavior == "reject":
            raise ExchangeRejected(self.reject_message)
        if behavior == "timeout_not_executed":
            raise ExchangeTimeout("simulated timeout")
        if behavior == "crash_not_executed":
            raise SimulatedCrash()

        px = price if order_type == "limit" else self.market_price
        if px is None or not math.isfinite(px) or px <= 0:
            raise ExchangeRejected("invalid price")
        if side == "buy" and self.balances[self.quote_ccy] < size * px:
            raise ExchangeRejected("insufficient balance")
        if side == "sell" and self.balances[self.base_ccy] < size:
            raise ExchangeRejected("insufficient balance")

        self._counter += 1
        self.orders[client_order_id] = {
            "client_order_id": client_order_id,
            "order_id": f"X{self._counter}",
            "state": "live", "side": side, "size": size,
            "filled_size": 0.0, "avg_price": 0.0, "fee": 0.0,
            "fee_ccy": "", "price": px,
        }
        if behavior in ("fill", "timeout_executed", "crash_executed"):
            self.fill(client_order_id, size)
        if behavior == "timeout_executed":
            raise ExchangeTimeout("simulated timeout (order was executed)")
        if behavior == "crash_executed":
            raise SimulatedCrash()
        return {
            "order_id": self.orders[client_order_id]["order_id"],
            "client_order_id": client_order_id,
        }

    def get_order(self, client_order_id):
        self._maybe_fail("get_order")
        order = self.orders.get(client_order_id)
        return dict(order) if order else None

    def cancel_order(self, client_order_id):
        self._maybe_fail("cancel_order")
        order = self.orders.get(client_order_id)
        if order is None:
            raise ExchangeRejected("order not found")
        if order["state"] in ("live", "partially_filled"):
            order["state"] = "canceled"
        else:
            raise ExchangeRejected("order already finished")

    def get_open_orders(self):
        self._maybe_fail("get_open_orders")
        return [
            dict(o) for o in self.orders.values()
            if o["state"] in ("live", "partially_filled")
        ]

    def get_recent_orders(self):
        self._maybe_fail("get_recent_orders")
        return [dict(o) for o in self.orders.values()]

    def get_balance(self):
        self._maybe_fail("get_balance")
        return dict(self.balances)

