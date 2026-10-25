"""
AI Crypto Trader - 阶段 2c：OKX 模拟盘交易客户端（实现 Exchange 接口）。

范围（2c 首轮，故意收紧）：
- 只支持现货限价买单（side=buy, ordType=limit, tdMode=cash）。
- 市价单、卖单、不合规的价格或数量一律在本地拒绝（LocalOrderRejected），不发请求。
- 复用 2b 的 OKXDemoClient：基址白名单、https、每个请求都带 x-simulated-trading: 1。

判定原则：
- code=0 且 sCode=0 只表示"已受理"，不表示挂单或成交，必须再查订单详情。
- 非零 sCode 原样保存 sCode 和 sMsg，不解释含义。
- 超时、5xx、50004、无法解析 -> ExchangeTimeout（结果不明，绝不重发）。
- 看不懂的响应一律按"结果不明"处理，不猜。
"""

import sys
from pathlib import Path

# Pyto 里脚本所在的文件夹不一定在模块搜索路径里，这里显式加上。
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import re
import time

from exchange_base import Exchange, ExchangeRejected, ExchangeTimeout
from okx_demo import OKXApiError, OKXDemoClient
from order_safety import (
    classify_order_response,
    decimal_value,
    validate_normalized_order,
)
from secrets_util import redact_text

ID_PATTERN = re.compile(r"^[A-Za-z0-9]{1,32}$")

# 订单不存在的错误码。【待 2c 实测核实】：
# 如果实测返回的是别的错误码，get_order 会抛 OKXApiError（保守，不会误报"查不到"）。
NOT_FOUND_CODES = {"51603"}

# 订单列表单页上限；达到上限就无法确认列表是否完整，保守起见报错而不是漏单。
PAGE_LIMIT = 100


class OKXOrderRejected(ExchangeRejected):
    """交易所明确拒绝。保留原始 sCode/sMsg（或请求级 code/msg），不解释含义。"""

    def __init__(self, message, s_code=None, s_msg=None, code=None, msg=None):
        super().__init__(message)
        self.s_code = s_code
        self.s_msg = s_msg
        self.code = code
        self.msg = msg


class LocalOrderRejected(ExchangeRejected, ValueError):
    """
    发送前的本地检查没通过：请求根本没有发出去，交易所不知道这笔单。
    OrderManager 会把它记为 REJECTED（不会误判成结果不明而停机）。
    """


def _extract_data(text):
    """从原始响应文本里取出 data 数组；取不到就返回空列表。"""
    import json
    try:
        payload = json.loads(text)
    except ValueError:
        return []
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, list) else []


class _DataKeepingClient(OKXDemoClient):
    """
    2b 的 _parse 在顶层 code 非零时会丢掉 data，
    但下单失败时 sCode/sMsg 就在 data 里。这里只补一件事：把 data 挂到异常上。
    """

    def _parse(self, status, text):
        try:
            return super()._parse(status, text)
        except OKXApiError as exc:
            exc.data = _extract_data(text)
            raise


class OKXDemoExchange(Exchange):
    def __init__(self, base_url, allowed_base_urls, credentials,
                 inst_id="BTC-USDT", transport=None, clock=None, timeout=10.0,
                 min_interval=0.25, sleep=None):
        if not credentials:
            raise ValueError("交易客户端必须提供模拟盘凭证。")
        self._client = _DataKeepingClient(
            base_url, allowed_base_urls, credentials=credentials,
            transport=transport, clock=clock, timeout=timeout,
        )
        self._secret_values = list(credentials.values())
        self.inst_id = inst_id
        self._rules = None
        self._min_interval = min_interval
        self._sleep = sleep or time.sleep
        self._last_request = None

    def __repr__(self):
        return f"OKXDemoExchange(base_url={self._client.base_url!r}, inst_id={self.inst_id!r})"

    @property
    def base_url(self):
        return self._client.base_url

    # ---------- 内部工具 ----------
    def _safe(self, value):
        return redact_text(value, self._secret_values)[:300]

    def _throttle(self):
        if self._min_interval and self._last_request is not None:
            wait = self._min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                self._sleep(wait)
        self._last_request = time.monotonic()

    def _request(self, method, path, params=None, body=None, private=True):
        self._throttle()
        return self._client.request(
            method, path, params=params, body=body, private=private
        )

    @staticmethod
    def _number(value, field):
        try:
            return float(decimal_value(value, field))
        except ValueError:
            raise ExchangeTimeout(f"响应字段 {field} 不是有效数字") from None

    # ---------- 公共信息 ----------
    def check_clock(self, max_offset_seconds=5.0):
        return self._client.check_clock(max_offset_seconds)

    def load_rules(self, force=False):
        """读取并缓存 minSz、lotSz、tickSz。字段缺失或不是数字就报错。"""
        if self._rules is not None and not force:
            return dict(self._rules)
        data = self._request(
            "GET", "/api/v5/public/instruments",
            params={"instType": "SPOT", "instId": self.inst_id}, private=False,
        )
        if not data or not isinstance(data[0], dict):
            raise ExchangeTimeout("交易对规则响应结构不符合预期")
        item = data[0]
        if item.get("instId") != self.inst_id:
            raise ExchangeTimeout("返回的交易对与请求不一致")
        rules = {}
        for key in ("minSz", "lotSz", "tickSz"):
            value = item.get(key)
            if not isinstance(value, str) or not value:
                raise ExchangeTimeout(f"交易对规则缺少 {key}")
            try:
                decimal_value(value, key)
            except ValueError:
                raise ExchangeTimeout(f"交易对规则 {key} 不是有效数字") from None
            rules[key] = value
        self._rules = rules
        return dict(rules)

    def get_last_price(self):
        """公共行情接口，返回最新成交价字符串。"""
        data = self._request(
            "GET", "/api/v5/market/ticker",
            params={"instId": self.inst_id}, private=False,
        )
        if not data or not isinstance(data[0], dict):
            raise ExchangeTimeout("行情响应结构不符合预期")
        last = data[0].get("last")
        if not isinstance(last, str) or not last:
            raise ExchangeTimeout("行情响应缺少 last")
        try:
            decimal_value(last, "last")
        except ValueError:
            raise ExchangeTimeout("行情 last 不是有效数字") from None
        return last

    def get_best_prices(self):
        """公共行情接口，返回 {"last", "ask", "bid"} 字符串。任何字段缺失或不是正数都报错。"""
        data = self._request(
            "GET", "/api/v5/market/ticker",
            params={"instId": self.inst_id}, private=False,
        )
        if not data or not isinstance(data[0], dict):
            raise ExchangeTimeout("行情响应结构不符合预期")
        prices = {}
        for name, key in (("last", "last"), ("ask", "askPx"), ("bid", "bidPx")):
            value = data[0].get(key)
            if not isinstance(value, str) or not value:
                raise ExchangeTimeout(f"行情响应缺少 {key}")
            try:
                number = decimal_value(value, key)
            except ValueError:
                raise ExchangeTimeout(f"行情 {key} 不是有效数字") from None
            if number <= 0:
                raise ExchangeTimeout(f"行情 {key} 不是正数")
            prices[name] = value
        return prices

    # ---------- 错误分类 ----------
    def _classify_api_error(self, exc):
        data = getattr(exc, "data", None) or []
        first = data[0] if data and isinstance(data[0], dict) else {}
        s_code = first.get("sCode")
        s_msg = self._safe(first.get("sMsg", ""))

        if s_code not in (None, ""):
            if str(s_code) != "0":
                # 订单级拒绝：交易所明确回应，订单没有被接受。
                return OKXOrderRejected(
                    f"订单被拒绝 sCode={s_code} sMsg={s_msg}",
                    s_code=str(s_code), s_msg=s_msg,
                    code=exc.code, msg=exc.msg,
                )
            # 顶层失败但订单级显示成功：自相矛盾，按结果不明处理。
            return ExchangeTimeout(f"响应自相矛盾，结果不明：{exc}")

        if exc.unknown_outcome or exc.http_status != 200:
            return ExchangeTimeout(f"结果不明：{exc}")

        # HTTP 200 且是请求级错误（没有订单数据）：请求被明确拒绝。
        return OKXOrderRejected(
            f"请求被交易所拒绝：{exc}", code=exc.code, msg=exc.msg,
        )

    def _send_trade(self, path, body):
        """发送下单/撤单请求并分类结果。成功返回 data[0]（已受理，不是已成交）。"""
        try:
            data = self._request("POST", path, body=body)
        except OKXApiError as exc:
            raise self._classify_api_error(exc) from None

        outcome = classify_order_response({"code": "0", "data": data})
        status = outcome["status"]
        if status == "SENT":
            return data[0]
        if status == "REJECTED":
            s_msg = self._safe(outcome.get("sMsg", ""))
            raise OKXOrderRejected(
                f"订单被拒绝 sCode={outcome['sCode']} sMsg={s_msg}",
                s_code=str(outcome["sCode"]), s_msg=s_msg, code="0", msg="",
            )
        raise ExchangeTimeout(f"下单响应无法确认，结果不明：{outcome.get('reason')}")

    # ---------- Exchange 接口 ----------
    def place_order(self, client_order_id, side, size, order_type="market",
                    price=None, validate_locally=True):
        """
        限价买单。成功只表示"已受理"，返回 {"order_id", "client_order_id"}。
        validate_locally=False 只给"故意发非法订单看交易所怎么拒绝"的实验使用，
        OrderManager 不会传这个参数。
        """
        if side != "buy" or order_type != "limit":
            raise LocalOrderRejected("2c 首轮只支持现货限价买单。")
        if price is None:
            raise LocalOrderRejected("限价单必须提供 price。")
        if not isinstance(client_order_id, str) or not ID_PATTERN.match(client_order_id):
            raise LocalOrderRejected("clOrdId 只能含字母和数字，长度 1 到 32。")

        try:
            px = decimal_value(price, "price")
            sz = decimal_value(size, "size")
            if px <= 0 or sz <= 0:
                raise ValueError("price 和 size 必须大于 0。")
            px_text = format(px, "f")
            sz_text = format(sz, "f")
            if validate_locally:
                rules = self.load_rules()
                validate_normalized_order(
                    {"px": px_text, "sz": sz_text},
                    rules["tickSz"], rules["lotSz"], rules["minSz"],
                )
        except ValueError as exc:
            raise LocalOrderRejected(f"本地检查未通过，未发送：{exc}") from None

        body = {
            "instId": self.inst_id,
            "tdMode": "cash",
            "clOrdId": client_order_id,
            "side": "buy",
            "ordType": "limit",
            "px": px_text,
            "sz": sz_text,
        }
        result = self._send_trade("/api/v5/trade/order", body)
        return {
            "order_id": result.get("ordId"),
            "client_order_id": result.get("clOrdId") or client_order_id,
        }

    def cancel_order(self, client_order_id):
        if not isinstance(client_order_id, str) or not ID_PATTERN.match(client_order_id):
            raise ValueError("clOrdId 只能含字母和数字，长度 1 到 32。")
        body = {"instId": self.inst_id, "clOrdId": client_order_id}
        result = self._send_trade("/api/v5/trade/cancel-order", body)
        return {
            "order_id": result.get("ordId"),
            "client_order_id": result.get("clOrdId") or client_order_id,
        }

    def _normalize_order(self, item):
        """把 OKX 订单字段转成 Exchange 约定的格式。字段异常就报错，不猜。"""
        if not isinstance(item, dict):
            raise ExchangeTimeout("订单响应结构不符合预期")
        state = item.get("state")
        side = item.get("side")
        if not isinstance(state, str) or not state:
            raise ExchangeTimeout("订单响应缺少 state")
        if not isinstance(side, str) or not side:
            raise ExchangeTimeout("订单响应缺少 side")

        size = self._number(item.get("sz"), "sz")

        # 成交量只用累计值 accFillSz，不用 fillSz（最近一笔）。
        acc = item.get("accFillSz")
        if not isinstance(acc, str) or acc == "":
            raise ExchangeTimeout("订单响应缺少 accFillSz")
        filled = self._number(acc, "accFillSz")

        avg_text = item.get("avgPx")
        avg_price = 0.0 if avg_text in (None, "") else self._number(avg_text, "avgPx")

        fee_text = item.get("fee")
        if fee_text in (None, ""):
            if filled > 0:
                raise ExchangeTimeout("已有成交但订单响应缺少 fee")
            fee = 0.0
        else:
            raw_fee = self._number(fee_text, "fee")
            # OKX：扣费为负数；Exchange 约定扣费为正数。
            fee = 0.0 - raw_fee      # 不用 -raw_fee，避免产生 -0.0
            if fee < 0:
                raise ExchangeTimeout("fee 为正数（返利），2c 未支持，需人工确认")
        fee_ccy = item.get("feeCcy") or ""
        if filled > 0 and fee > 0 and not fee_ccy:
            raise ExchangeTimeout("有手续费但缺少 feeCcy")

        return {
            "client_order_id": item.get("clOrdId") or "",
            "order_id": item.get("ordId") or "",
            "state": state,
            "side": side,
            "size": size,
            "filled_size": filled,
            "avg_price": avg_price,
            "fee": fee,
            "fee_ccy": fee_ccy,
        }

    def get_order(self, client_order_id):
        """按客户端订单 ID 查询；交易所明确说订单不存在时返回 None。"""
        if not isinstance(client_order_id, str) or not ID_PATTERN.match(client_order_id):
            raise ValueError("clOrdId 只能含字母和数字，长度 1 到 32。")
        try:
            data = self._request(
                "GET", "/api/v5/trade/order",
                params={"instId": self.inst_id, "clOrdId": client_order_id},
            )
        except OKXApiError as exc:
            if exc.code in NOT_FOUND_CODES and exc.http_status == 200:
                return None
            raise
        if not data:
            raise ExchangeTimeout("订单详情响应为空，结果不明")
        order = self._normalize_order(data[0])
        if order["client_order_id"] != client_order_id:
            raise ExchangeTimeout("返回的订单与查询的 clOrdId 不一致")
        return order

    def _list_orders(self, path):
        data = self._request("GET", path, params={"instType": "SPOT"})
        if len(data) >= PAGE_LIMIT:
            raise ExchangeTimeout("订单数量达到单页上限，无法确认列表完整，需要实现分页")
        return [self._normalize_order(item) for item in data]

    def get_open_orders(self):
        return self._list_orders("/api/v5/trade/orders-pending")

    def get_recent_orders(self):
        return self._list_orders("/api/v5/trade/orders-history")

    def get_balance(self):
        """返回 {币种: 总余额}（取 cashBal）。字段缺失就报错，不猜。"""
        data = self._request("GET", "/api/v5/account/balance")
        if not data or not isinstance(data[0], dict):
            raise ExchangeTimeout("余额响应结构不符合预期")
        details = data[0].get("details")
        if not isinstance(details, list):
            raise ExchangeTimeout("余额响应缺少 details")
        result = {}
        for entry in details:
            ccy = entry.get("ccy") if isinstance(entry, dict) else None
            cash = entry.get("cashBal") if isinstance(entry, dict) else None
            if not ccy or not isinstance(cash, str) or cash == "":
                raise ExchangeTimeout("余额条目缺少 ccy 或 cashBal")
            result[ccy] = self._number(cash, "cashBal")
        return result
