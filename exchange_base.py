class ExchangeError(Exception):
    """交易所相关错误的基类。"""


class ExchangeTimeout(ExchangeError):
    """请求超时、断网或响应无法解析：不知道交易所有没有处理这个请求。"""


class ExchangeRejected(ExchangeError):
    """交易所明确拒绝了请求（交易所做出了回应，订单没有被接受）。"""


class Exchange:
    """
    交易所接口约定。所有实现（FakeExchange、将来的 OKX 模拟盘）都必须遵守：

    订单字典格式（get_order / get_open_orders / get_recent_orders 返回）：
        client_order_id  客户端订单 ID；不是我们下的单则为空字符串
        order_id         交易所订单 ID
        state            交易所原始状态字符串（live / partially_filled / filled / canceled ...）
        side             "buy" 或 "sell"
        size             下单数量（基础币）
        filled_size      累计成交数量（基础币），以交易所为准
        avg_price        成交均价
        fee              累计手续费，【正数】表示扣费的数量
        fee_ccy          手续费币种

    异常约定：
        交易所明确拒绝       -> ExchangeRejected
        超时 / 断网 / 无法解析 -> ExchangeTimeout
    """

    def place_order(self, client_order_id, side, size, order_type="market", price=None):
        """下单。成功返回 {"order_id": ..., "client_order_id": ...}，只表示被接受，不表示成交。"""
        raise NotImplementedError

    def get_order(self, client_order_id):
        """按客户端订单 ID 查询；交易所不认识这个 ID 时返回 None。"""
        raise NotImplementedError

    def cancel_order(self, client_order_id):
        raise NotImplementedError

    def get_open_orders(self):
        raise NotImplementedError

    def get_recent_orders(self):
        """近期全部订单（含已终结），用于启动对账。"""
        raise NotImplementedError

    def get_balance(self):
        """返回 {币种: 总余额}，例如 {"BTC": 0.5, "USDT": 1000.0}。"""
        raise NotImplementedError

