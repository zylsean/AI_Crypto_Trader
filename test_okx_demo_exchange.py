"""
OKXDemoExchange 的假网络测试。没有任何真实请求、没有真实凭证。
"""

import itertools
import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from exchange_base import ExchangeRejected, ExchangeTimeout
from okx_demo import OKXApiError, sign
from okx_demo_exchange import (
    LocalOrderRejected, OKXDemoExchange, OKXOrderRejected,
)
from order_manager import (
    CANCELED, FILLED, LIVE, REJECTED, UNKNOWN, OrderManager, TradingHalted,
)
from order_store import OrderStore
from risk_guard import RiskGuard, RiskLimits

BASE = "https://openapi.okx.com"
CREDS = {
    "api_key": "KEYVALUE-abc-123456",
    "secret": "SECRETVALUE-xyz-987654",
    "passphrase": "PassPhrase#2026",
}
NOW = 1_700_000_000.0


def ok(data):
    return 200, json.dumps({"code": "0", "msg": "", "data": data})


def top_error(code, msg, data=None, status=200):
    return status, json.dumps({"code": code, "msg": msg, "data": data or []})


def ack(cid, ord_id="9001"):
    return ok([{"clOrdId": cid, "ordId": ord_id, "tag": "", "sCode": "0", "sMsg": ""}])


class FakeOKX:
    """假的 OKX 服务器：记录每个请求，并按路径返回可编排的响应。"""

    def __init__(self):
        self.requests = []
        self.orders = {}
        self.balance_details = [
            {"ccy": "BTC", "cashBal": "0"},
            {"ccy": "USDT", "cashBal": "100000"},
        ]
        self.overrides = {}        # (方法, 路径) -> [(状态码, 文本), ...]，每次用掉一个
        self.raise_once = {}       # (方法, 路径) -> 异常
        self.execute_then_raise = False
        self.time_offset_ms = 0    # 假服务器时间比本机快多少毫秒
        self.hide_orders = False   # True：订单详情查询一律返回"订单不存在"，但订单真实存在
        self.raise_always = {}     # (方法, 路径) -> 异常，每次请求都抛
        self.cancel_mode = None    # None=正常撤单；"ignore"=返回受理但订单不变；"fill"=返回受理但订单变成已成交
        self.cancel_rejects = 0    # 接下来这么多次撤单请求返回业务错误（订单不变）
        self.ask = "100001.0"      # 行情卖一价
        self.bid = "100000.0"      # 行情买一价
        self.fill_on_place = False         # True：新下的限价买单立刻按卖一价成交
        self.skip_fee_in_balance = False   # True：成交了但余额里没扣手续费（用来测对账能否发现）
        self.skip_usdt_in_balance = False  # True：成交了但 USDT 余额没变化
        self.min_sz = "0.00001"            # 交易对最小下单量
        self._n = 0

    def posts(self, path="/api/v5/trade/order"):
        return [r for r in self.requests if r["method"] == "POST" and r["path"] == path]

    def __call__(self, method, url, headers, body, timeout):
        parsed = urlparse(url)
        record = {
            "method": method, "path": parsed.path, "query": parse_qs(parsed.query),
            "headers": dict(headers),
            "body": body.decode("utf-8") if body else "",
        }
        self.requests.append(record)
        key = (method, parsed.path)

        always = self.raise_always.get(key)
        if always is not None:
            raise always

        exc = self.raise_once.get(key)
        if exc is not None:
            if self.execute_then_raise and key == ("POST", "/api/v5/trade/order"):
                self._place(json.loads(record["body"]))
            del self.raise_once[key]
            raise exc

        queue = self.overrides.get(key)
        if queue:
            return queue.pop(0)
        return self._handle(method, parsed.path, record)

    def _place(self, body):
        self._n += 1
        self.orders[body["clOrdId"]] = {
            "instId": body["instId"], "clOrdId": body["clOrdId"],
            "ordId": f"EX{self._n}", "side": body["side"], "sz": body["sz"],
            "px": body["px"], "state": "live", "accFillSz": "0", "fillSz": "0",
            "avgPx": "", "fee": "", "feeCcy": "",
        }
        order = self.orders[body["clOrdId"]]
        if self.fill_on_place:
            from decimal import Decimal
            size, price = Decimal(body["sz"]), Decimal(self.ask)
            fee = size * Decimal("0.001")
            order.update(state="filled", accFillSz=body["sz"], fillSz=body["sz"],
                         avgPx=format(price, "f"), fee="-" + format(fee, "f"),
                         feeCcy="BTC")
            self._move_balance("BTC", size - (Decimal(0) if self.skip_fee_in_balance else fee))
            if not self.skip_usdt_in_balance:
                self._move_balance("USDT", -(size * price))
        return order

    def _move_balance(self, ccy, delta):
        from decimal import Decimal
        for entry in self.balance_details:
            if entry["ccy"] == ccy:
                entry["cashBal"] = format(Decimal(entry["cashBal"]) + delta, "f")
                return
        self.balance_details.append({"ccy": ccy, "cashBal": format(delta, "f")})

    def _handle(self, method, path, record):
        if path == "/api/v5/public/time":
            return ok([{"ts": str(int(NOW * 1000) + self.time_offset_ms)}])
        if path == "/api/v5/public/instruments":
            return ok([{"instId": "BTC-USDT", "minSz": self.min_sz,
                        "lotSz": "0.00000001", "tickSz": "0.1"}])
        if path == "/api/v5/market/ticker":
            return ok([{"instId": "BTC-USDT", "last": "100000.5",
                        "askPx": self.ask, "bidPx": self.bid}])
        if path == "/api/v5/account/balance":
            return ok([{"details": self.balance_details}])
        if method == "POST" and path == "/api/v5/trade/order":
            body = json.loads(record["body"])
            existing = self.orders.get(body["clOrdId"])
            if existing and existing["state"] in ("live", "partially_filled"):
                return top_error("1", "All operations failed", [
                    {"clOrdId": body["clOrdId"], "ordId": "", "sCode": "99001",
                     "sMsg": "duplicate (test)"}])
            order = self._place(body)
            return ack(body["clOrdId"], order["ordId"])
        if method == "POST" and path == "/api/v5/trade/cancel-order":
            body = json.loads(record["body"])
            order = self.orders.get(body["clOrdId"])
            if self.cancel_rejects > 0:
                self.cancel_rejects -= 1
                return top_error("1", "All operations failed", [
                    {"clOrdId": body["clOrdId"], "ordId": "", "sCode": "99003",
                     "sMsg": "cancel rejected (test)"}])
            if order and self.cancel_mode == "ignore":
                return ack(body["clOrdId"], order["ordId"])
            if order and self.cancel_mode == "fill":
                order.update(state="filled", accFillSz=order["sz"], fillSz=order["sz"],
                             avgPx=order["px"], fee="-0.00000001", feeCcy="BTC")
                return ack(body["clOrdId"], order["ordId"])
            if order and order["state"] in ("live", "partially_filled"):
                order["state"] = "canceled"
                return ack(body["clOrdId"], order["ordId"])
            return top_error("1", "All operations failed", [
                {"clOrdId": body["clOrdId"], "ordId": "", "sCode": "99002",
                 "sMsg": "cannot cancel (test)"}])
        if method == "GET" and path == "/api/v5/trade/order":
            cid = record["query"]["clOrdId"][0]
            order = self.orders.get(cid)
            if order is None or self.hide_orders:
                return 200, json.dumps(
                    {"code": "51603", "msg": "Order does not exist", "data": []})
            return ok([dict(order)])
        if method == "GET" and path == "/api/v5/trade/orders-pending":
            return ok([dict(o) for o in self.orders.values()
                       if o["state"] in ("live", "partially_filled")])
        if method == "GET" and path == "/api/v5/trade/orders-history":
            return ok([dict(o) for o in self.orders.values()])
        return top_error("99999", "unhandled in fake server")


def make_exchange(server=None, **kwargs):
    server = server or FakeOKX()
    exchange = OKXDemoExchange(
        BASE, [BASE], dict(CREDS), transport=server, clock=lambda: NOW,
        min_interval=0, **kwargs,
    )
    return exchange, server


class TestConstruction(unittest.TestCase):
    def test_credentials_required(self):
        with self.assertRaises(ValueError):
            OKXDemoExchange(BASE, [BASE], None)

    def test_base_url_must_be_whitelisted_https(self):
        with self.assertRaises(ValueError):
            OKXDemoExchange("https://evil.example.com", [BASE], dict(CREDS))
        with self.assertRaises(ValueError):
            OKXDemoExchange("http://openapi.okx.com", ["http://openapi.okx.com"],
                            dict(CREDS))

    def test_repr_has_no_credentials(self):
        exchange, _ = make_exchange()
        text = repr(exchange)
        for value in CREDS.values():
            self.assertNotIn(value, text)


class TestRequestSafety(unittest.TestCase):
    def test_every_request_has_simulation_header(self):
        exchange, server = make_exchange()
        exchange.load_rules()
        exchange.get_last_price()
        exchange.get_balance()
        exchange.get_open_orders()
        exchange.get_recent_orders()
        exchange.get_order("ABC123")
        exchange.place_order("ORD0001", "buy", 0.00002, "limit", 99000.0)
        exchange.cancel_order("ORD0001")
        self.assertGreaterEqual(len(server.requests), 8)
        for request in server.requests:
            self.assertEqual(request["headers"].get("x-simulated-trading"), "1")

    def test_private_requests_are_signed_over_the_sent_body(self):
        exchange, server = make_exchange()
        exchange.place_order("ORD0001", "buy", 0.00002, "limit", 99000.0)
        request = server.posts()[0]
        headers = request["headers"]
        expected = sign(CREDS["secret"], headers["OK-ACCESS-TIMESTAMP"], "POST",
                        "/api/v5/trade/order", request["body"])
        self.assertEqual(headers["OK-ACCESS-SIGN"], expected)
        self.assertEqual(headers["OK-ACCESS-KEY"], CREDS["api_key"])
        self.assertEqual(headers["OK-ACCESS-PASSPHRASE"], CREDS["passphrase"])
        self.assertNotIn("OK-ACCESS-SECRET", headers)

    def test_public_requests_carry_no_credentials(self):
        exchange, server = make_exchange()
        exchange.load_rules()
        headers = server.requests[0]["headers"]
        self.assertNotIn("OK-ACCESS-KEY", headers)
        self.assertNotIn("OK-ACCESS-SIGN", headers)

    def test_secrets_never_appear_in_error_text(self):
        exchange, server = make_exchange()
        leak = (f"bad key {CREDS['api_key']} secret={CREDS['secret']} "
                f"pass {CREDS['passphrase']}")
        server.overrides[("POST", "/api/v5/trade/order")] = [
            top_error("1", "x", [{"sCode": "99001", "sMsg": leak}])]
        with self.assertRaises(OKXOrderRejected) as ctx:
            exchange.place_order("ORD0001", "buy", 0.00002, "limit", 99000.0)
        text = str(ctx.exception) + str(ctx.exception.s_msg)
        for value in CREDS.values():
            self.assertNotIn(value, text)


class TestPlaceOrder(unittest.TestCase):
    def test_valid_limit_buy_body(self):
        exchange, server = make_exchange()
        result = exchange.place_order("ORD0001", "buy", 0.00002, "limit", 99000.0)
        body = json.loads(server.posts()[0]["body"])
        self.assertEqual(body, {
            "instId": "BTC-USDT", "tdMode": "cash", "clOrdId": "ORD0001",
            "side": "buy", "ordType": "limit", "px": "99000.0", "sz": "0.00002",
        })
        for value in body.values():
            self.assertIsInstance(value, str)
        self.assertEqual(result["client_order_id"], "ORD0001")
        self.assertTrue(result["order_id"])

    def test_ack_makes_no_state_claim(self):
        exchange, server = make_exchange()
        result = exchange.place_order("ORD0001", "buy", 0.00002, "limit", 99000.0)
        self.assertNotIn("state", result)
        # 下单本身不应该顺手去查订单；确认由调用方负责。
        self.assertEqual(
            [r for r in server.requests if r["path"] == "/api/v5/trade/order"
             and r["method"] == "GET"], [])

    def test_local_checks_block_before_any_order_request(self):
        exchange, server = make_exchange()
        cases = [
            ("ORD0001", "buy", 0.000001, "limit", 99000.0),     # 低于 minSz
            ("ORD0002", "buy", 0.00002, "limit", 99000.05),     # 价格不是 tickSz 整数倍
            ("ORD0003", "buy", 0.000020001, "limit", 99000.0),  # 数量不是 lotSz 整数倍
            ("ORD0004", "buy", 0.00002, "market", None),        # 市价单未开放
            ("ORD0004B", "buy", 0.00002, "market", 99000.0),    # 市价单带价格也不放行
            ("ORD0005", "sell", 0.00002, "limit", 99000.0),     # 卖单未开放
            ("ORD0006", "buy", 0.00002, "limit", None),         # 缺价格
            ("ORD0007", "buy", 0.00002, "limit", float("nan")),
            ("bad id!", "buy", 0.00002, "limit", 99000.0),      # 非法 clOrdId
        ]
        for args in cases:
            with self.subTest(args=args):
                with self.assertRaises(LocalOrderRejected):
                    exchange.place_order(*args)
        self.assertEqual(server.posts(), [])

    def test_local_rejection_is_both_rejected_and_value_error(self):
        exchange, _ = make_exchange()
        with self.assertRaises(ExchangeRejected):
            exchange.place_order("ORD0001", "buy", 0.00002, "market")
        with self.assertRaises(ValueError):
            exchange.place_order("ORD0001", "buy", 0.00002, "market")

    def test_validate_locally_false_sends_invalid_order(self):
        exchange, server = make_exchange()
        exchange.place_order("ORD0001", "buy", 0.000001, "limit", 99000.0,
                             validate_locally=False)
        self.assertEqual(len(server.posts()), 1)

    def test_non_positive_values_never_sent_even_without_local_validation(self):
        exchange, server = make_exchange()
        with self.assertRaises(LocalOrderRejected):
            exchange.place_order("ORD0001", "buy", 0, "limit", 99000.0,
                                 validate_locally=False)
        with self.assertRaises(LocalOrderRejected):
            exchange.place_order("ORD0001", "buy", 0.00002, "limit", -1.0,
                                 validate_locally=False)
        self.assertEqual(server.posts(), [])

    def test_order_level_rejection_in_successful_envelope(self):
        exchange, server = make_exchange()
        server.overrides[("POST", "/api/v5/trade/order")] = [
            ok([{"clOrdId": "ORD0001", "ordId": "", "sCode": "88001",
                 "sMsg": "example rejection"}])]
        with self.assertRaises(OKXOrderRejected) as ctx:
            exchange.place_order("ORD0001", "buy", 0.00002, "limit", 99000.0)
        self.assertEqual(ctx.exception.s_code, "88001")
        self.assertEqual(ctx.exception.s_msg, "example rejection")
        self.assertEqual(len(server.posts()), 1)

    def test_order_level_rejection_in_failed_envelope_keeps_scode(self):
        exchange, server = make_exchange()
        server.overrides[("POST", "/api/v5/trade/order")] = [
            top_error("1", "All operations failed", [
                {"clOrdId": "ORD0001", "ordId": "", "sCode": "88002",
                 "sMsg": "example"}])]
        with self.assertRaises(OKXOrderRejected) as ctx:
            exchange.place_order("ORD0001", "buy", 0.00002, "limit", 99000.0)
        self.assertEqual(ctx.exception.s_code, "88002")
        self.assertEqual(len(server.posts()), 1)

    def test_request_level_error_http_200_is_rejection(self):
        exchange, server = make_exchange()
        server.overrides[("POST", "/api/v5/trade/order")] = [
            top_error("50113", "Invalid sign")]
        with self.assertRaises(OKXOrderRejected) as ctx:
            exchange.place_order("ORD0001", "buy", 0.00002, "limit", 99000.0)
        self.assertEqual(ctx.exception.code, "50113")
        self.assertIsNone(ctx.exception.s_code)

    def test_unknown_outcomes_are_timeouts_not_rejections(self):
        scenarios = {
            "50004": top_error("50004", "timeout"),
            "http500": (500, "<html>Internal Server Error</html>"),
            "http502_json": top_error("1", "bad gateway", status=502),
            "not_json": (200, "gateway says hello"),
            "no_code": (200, json.dumps({"hello": "world"})),
            "empty_data": ok([]),
            "missing_scode": ok([{"clOrdId": "ORD0001", "ordId": "1"}]),
            "contradiction": top_error("1", "x", [{"sCode": "0", "sMsg": ""}]),
        }
        for name, response in scenarios.items():
            with self.subTest(name=name):
                exchange, server = make_exchange()
                server.overrides[("POST", "/api/v5/trade/order")] = [response]
                with self.assertRaises(ExchangeTimeout):
                    exchange.place_order("ORD0001", "buy", 0.00002, "limit", 99000.0)
                self.assertEqual(len(server.posts()), 1)    # 没有重发

    def test_transport_timeout_propagates_once(self):
        exchange, server = make_exchange()
        server.raise_once[("POST", "/api/v5/trade/order")] = ExchangeTimeout("net")
        with self.assertRaises(ExchangeTimeout):
            exchange.place_order("ORD0001", "buy", 0.00002, "limit", 99000.0)
        self.assertEqual(len(server.posts()), 1)

    def test_duplicate_live_client_id_is_rejected_with_scode(self):
        exchange, server = make_exchange()
        exchange.place_order("ORD0001", "buy", 0.00002, "limit", 99000.0)
        with self.assertRaises(OKXOrderRejected) as ctx:
            exchange.place_order("ORD0001", "buy", 0.00002, "limit", 99000.0)
        self.assertEqual(ctx.exception.s_code, "99001")
        self.assertEqual(len(server.orders), 1)


class TestCancel(unittest.TestCase):
    def test_cancel_body_and_ack(self):
        exchange, server = make_exchange()
        exchange.place_order("ORD0001", "buy", 0.00002, "limit", 99000.0)
        exchange.cancel_order("ORD0001")
        body = json.loads(server.posts("/api/v5/trade/cancel-order")[0]["body"])
        self.assertEqual(body, {"instId": "BTC-USDT", "clOrdId": "ORD0001"})
        self.assertEqual(server.orders["ORD0001"]["state"], "canceled")

    def test_cancel_failure_is_rejection(self):
        exchange, _ = make_exchange()
        with self.assertRaises(OKXOrderRejected) as ctx:
            exchange.cancel_order("NOSUCH1")
        self.assertEqual(ctx.exception.s_code, "99002")

    def test_cancel_timeout_is_unknown(self):
        exchange, server = make_exchange()
        server.overrides[("POST", "/api/v5/trade/cancel-order")] = [
            top_error("50004", "timeout")]
        with self.assertRaises(ExchangeTimeout):
            exchange.cancel_order("ORD0001")


def order_item(**overrides):
    item = {
        "clOrdId": "ORD0001", "ordId": "9001", "state": "live", "side": "buy",
        "sz": "0.00002", "accFillSz": "0", "fillSz": "0", "avgPx": "",
        "fee": "", "feeCcy": "",
    }
    item.update(overrides)
    return item


class TestGetOrder(unittest.TestCase):
    def fetch(self, **overrides):
        exchange, server = make_exchange()
        server.overrides[("GET", "/api/v5/trade/order")] = [ok([order_item(**overrides)])]
        return exchange.get_order("ORD0001")

    def test_live_order_mapping(self):
        order = self.fetch()
        self.assertEqual(order["client_order_id"], "ORD0001")
        self.assertEqual(order["order_id"], "9001")
        self.assertEqual(order["state"], "live")
        self.assertEqual(order["side"], "buy")
        self.assertAlmostEqual(order["size"], 0.00002)
        self.assertEqual(order["filled_size"], 0.0)
        self.assertEqual(order["avg_price"], 0.0)
        self.assertEqual(order["fee"], 0.0)

    def test_query_uses_inst_id_and_cl_ord_id(self):
        exchange, server = make_exchange()
        exchange.get_order("ORD0001")
        query = server.requests[0]["query"]
        self.assertEqual(query["instId"], ["BTC-USDT"])
        self.assertEqual(query["clOrdId"], ["ORD0001"])

    def test_filled_uses_cumulative_size_and_positive_fee(self):
        order = self.fetch(state="filled", accFillSz="0.00002", fillSz="0.000005",
                           avgPx="99000.1", fee="-0.00000002", feeCcy="BTC")
        self.assertAlmostEqual(order["filled_size"], 0.00002)   # accFillSz，不是 fillSz
        self.assertAlmostEqual(order["fee"], 0.00000002)        # 扣费统一成正数
        self.assertEqual(order["fee_ccy"], "BTC")
        self.assertAlmostEqual(order["avg_price"], 99000.1)

    def test_canceled_order_keeps_partial_fill(self):
        order = self.fetch(state="canceled", accFillSz="0.00001", avgPx="99000.0",
                           fee="-0.00000001", feeCcy="BTC")
        self.assertEqual(order["state"], "canceled")
        self.assertAlmostEqual(order["filled_size"], 0.00001)

    def test_zero_fee_is_positive_zero(self):
        import math
        for raw in ("0", "-0", "0.0", ""):
            with self.subTest(raw=raw):
                order = self.fetch(fee=raw)
                self.assertEqual(order["fee"], 0.0)
                self.assertEqual(math.copysign(1.0, order["fee"]), 1.0)

    def test_unknown_state_is_passed_through_raw(self):
        self.assertEqual(self.fetch(state="some_new_state")["state"], "some_new_state")

    def test_fill_without_fee_is_error_not_zero(self):
        for fee in ("", None):
            with self.subTest(fee=fee):
                with self.assertRaises(ExchangeTimeout):
                    self.fetch(state="filled", accFillSz="0.00002", avgPx="99000",
                               fee=fee, feeCcy="BTC")

    def test_positive_fee_rebate_is_error(self):
        with self.assertRaises(ExchangeTimeout):
            self.fetch(state="filled", accFillSz="0.00002", avgPx="99000",
                       fee="0.00000002", feeCcy="BTC")

    def test_fee_without_currency_is_error(self):
        with self.assertRaises(ExchangeTimeout):
            self.fetch(state="filled", accFillSz="0.00002", avgPx="99000",
                       fee="-0.00000002", feeCcy="")

    def test_missing_or_bad_fields_are_errors(self):
        for field, value in (("accFillSz", ""), ("accFillSz", None), ("sz", "abc"),
                             ("state", ""), ("side", ""), ("accFillSz", "NaN")):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ExchangeTimeout):
                    self.fetch(**{field: value})

    def test_not_found_code_returns_none(self):
        exchange, _ = make_exchange()
        self.assertIsNone(exchange.get_order("NOSUCH1"))

    def test_other_error_codes_are_not_treated_as_not_found(self):
        exchange, server = make_exchange()
        server.overrides[("GET", "/api/v5/trade/order")] = [top_error("50119", "x")]
        with self.assertRaises(OKXApiError):
            exchange.get_order("ORD0001")

    def test_not_found_code_with_http_error_is_not_none(self):
        exchange, server = make_exchange()
        server.overrides[("GET", "/api/v5/trade/order")] = [
            top_error("51603", "x", status=500)]
        with self.assertRaises(OKXApiError):
            exchange.get_order("ORD0001")

    def test_empty_data_is_unknown_not_none(self):
        exchange, server = make_exchange()
        server.overrides[("GET", "/api/v5/trade/order")] = [ok([])]
        with self.assertRaises(ExchangeTimeout):
            exchange.get_order("ORD0001")

    def test_mismatched_client_id_is_error(self):
        with self.assertRaises(ExchangeTimeout):
            self.fetch(clOrdId="OTHER99")

    def test_invalid_id_raises_value_error_without_request(self):
        exchange, server = make_exchange()
        with self.assertRaises(ValueError):
            exchange.get_order("bad id!")
        self.assertEqual(server.requests, [])


class TestListsAndBalance(unittest.TestCase):
    def test_lists_use_spot_and_normalize(self):
        exchange, server = make_exchange()
        exchange.place_order("ORD0001", "buy", 0.00002, "limit", 99000.0)
        server.orders["MANUAL1"] = order_item(clOrdId="", ordId="777")
        open_orders = exchange.get_open_orders()
        recent = exchange.get_recent_orders()
        self.assertEqual(len(open_orders), 2)
        self.assertEqual(len(recent), 2)
        self.assertIn("", [o["client_order_id"] for o in open_orders])
        for request in server.requests:
            if request["path"].endswith(("orders-pending", "orders-history")):
                self.assertEqual(request["query"]["instType"], ["SPOT"])

    def test_page_limit_is_error_not_silent_truncation(self):
        exchange, server = make_exchange()
        server.overrides[("GET", "/api/v5/trade/orders-pending")] = [
            ok([order_item(clOrdId=f"X{i}") for i in range(100)])]
        with self.assertRaises(ExchangeTimeout):
            exchange.get_open_orders()

    def test_balance_mapping(self):
        exchange, _ = make_exchange()
        self.assertEqual(exchange.get_balance(), {"BTC": 0.0, "USDT": 100000.0})

    def test_balance_missing_fields_are_errors(self):
        for details in ([{"ccy": "BTC"}], [{"cashBal": "1"}],
                        [{"ccy": "BTC", "cashBal": ""}], [{"ccy": "BTC", "cashBal": "x"}]):
            with self.subTest(details=details):
                exchange, server = make_exchange()
                server.balance_details = details
                with self.assertRaises(ExchangeTimeout):
                    exchange.get_balance()

    def test_balance_without_details_is_error(self):
        exchange, server = make_exchange()
        server.overrides[("GET", "/api/v5/account/balance")] = [ok([{"eq": "1"}])]
        with self.assertRaises(ExchangeTimeout):
            exchange.get_balance()


class TestRulesAndPrice(unittest.TestCase):
    def test_rules_loaded_once_and_cached(self):
        exchange, server = make_exchange()
        self.assertEqual(exchange.load_rules(),
                         {"minSz": "0.00001", "lotSz": "0.00000001", "tickSz": "0.1"})
        exchange.load_rules()
        count = [r for r in server.requests if r["path"] == "/api/v5/public/instruments"]
        self.assertEqual(len(count), 1)

    def test_bad_rules_are_errors(self):
        bad = [
            {"instId": "BTC-USDT", "minSz": "0.00001", "lotSz": "0.00000001"},
            {"instId": "BTC-USDT", "minSz": "x", "lotSz": "1", "tickSz": "0.1"},
            {"instId": "ETH-USDT", "minSz": "1", "lotSz": "1", "tickSz": "1"},
        ]
        for item in bad:
            with self.subTest(item=item):
                exchange, server = make_exchange()
                server.overrides[("GET", "/api/v5/public/instruments")] = [ok([item])]
                with self.assertRaises(ExchangeTimeout):
                    exchange.load_rules()

    def test_last_price(self):
        exchange, _ = make_exchange()
        self.assertEqual(exchange.get_last_price(), "100000.5")

    def test_best_prices(self):
        exchange, _ = make_exchange()
        self.assertEqual(exchange.get_best_prices(),
                         {"last": "100000.5", "ask": "100001.0", "bid": "100000.0"})

    def test_best_prices_missing_or_bad_fields_are_errors(self):
        base = {"instId": "BTC-USDT", "last": "1", "askPx": "2", "bidPx": "1"}
        for field, value in (("last", ""), ("askPx", ""), ("bidPx", None),
                             ("askPx", "abc"), ("askPx", "0"), ("bidPx", "-1")):
            with self.subTest(field=field, value=value):
                exchange, server = make_exchange()
                item = dict(base)
                item[field] = value
                server.overrides[("GET", "/api/v5/market/ticker")] = [ok([item])]
                with self.assertRaises(ExchangeTimeout):
                    exchange.get_best_prices()


class TestWithOrderManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store_path = Path(self.tmp.name) / "orders.jsonl"
        self._clock = itertools.count(1_700_000_000_000)
        self.exchange, self.server = make_exchange()

    def manager(self, prefix="T", **kwargs):
        ids = (f"{prefix}{n:04d}" for n in itertools.count(1))
        return OrderManager(
            self.exchange, OrderStore(self.store_path),
            RiskGuard(RiskLimits(max_order_notional=5000.0,
                                 max_position_notional=20000.0, max_daily_loss=1000.0)),
            clock=lambda: next(self._clock), id_factory=lambda: next(ids),
            sleep=lambda s: None, secrets=list(CREDS.values()), **kwargs,
        )

    def ready(self, **kwargs):
        mgr = self.manager(**kwargs)
        report = mgr.reconcile()
        self.assertTrue(report["ok"], report)
        return mgr

    def place(self, mgr, size=0.00002, price=99000.0):
        return mgr.place_order("buy", size, ref_price=100000.0,
                               order_type="limit", price=price)

    def test_limit_order_becomes_live_only_after_confirmation_query(self):
        mgr = self.ready()
        rec = self.place(mgr)
        self.assertEqual(rec["state"], LIVE)
        order_posts = len(self.server.posts())
        self.assertEqual(order_posts, 1)
        confirmations = [r for r in self.server.requests
                         if r["method"] == "GET" and r["path"] == "/api/v5/trade/order"]
        self.assertGreaterEqual(len(confirmations), 1)

    def test_cancel_flow_and_clean_restart(self):
        mgr = self.ready()
        rec = self.place(mgr)
        done = mgr.cancel_order(rec["cid"])
        self.assertEqual(done["state"], CANCELED)
        self.assertEqual(done["filled_size"], 0.0)
        again = self.manager(prefix="R")
        self.assertTrue(again.reconcile()["ok"])

    def test_live_order_survives_restart(self):
        mgr = self.ready()
        rec = self.place(mgr)
        again = self.manager(prefix="R")
        self.assertTrue(again.reconcile()["ok"])
        self.assertEqual(again.orders[rec["cid"]]["state"], LIVE)

    def test_fill_updates_fee_and_balance_reconcile(self):
        mgr = self.ready()
        rec = self.place(mgr)
        order = self.server.orders[rec["cid"]]
        order.update(state="filled", accFillSz="0.00002", fillSz="0.00002",
                     avgPx="99000.0", fee="-0.00000002", feeCcy="BTC")
        self.server.balance_details = [
            {"ccy": "BTC", "cashBal": "0.00001998"},
            {"ccy": "USDT", "cashBal": "99998.02"}]
        self.assertEqual(mgr.refresh(rec["cid"])["state"], FILLED)
        self.assertAlmostEqual(mgr.orders[rec["cid"]]["fee"], 0.00000002)
        self.assertTrue(self.manager(prefix="R").reconcile()["ok"])

    def test_explicit_rejection_marks_rejected_without_halt(self):
        mgr = self.ready()
        self.server.overrides[("POST", "/api/v5/trade/order")] = [
            top_error("1", "failed", [{"sCode": "88003", "sMsg": "example"}])]
        rec = self.place(mgr)
        self.assertEqual(rec["state"], REJECTED)
        self.assertIsNone(mgr.halted)
        self.assertEqual(len(self.server.posts()), 1)

    def test_local_validation_failure_marks_rejected_without_halt(self):
        mgr = self.ready()
        rec = self.place(mgr, size=0.000001)          # 低于 minSz
        self.assertEqual(rec["state"], REJECTED)
        self.assertIsNone(mgr.halted)
        self.assertEqual(self.server.posts(), [])

    def test_market_order_is_not_sent_in_round_one(self):
        mgr = self.ready()
        rec = mgr.place_order("buy", 0.00002, ref_price=100000.0, order_type="market")
        self.assertEqual(rec["state"], REJECTED)
        self.assertEqual(self.server.posts(), [])

    def test_timeout_but_executed_resolves_without_second_post(self):
        mgr = self.ready()
        self.server.execute_then_raise = True
        self.server.raise_once[("POST", "/api/v5/trade/order")] = ExchangeTimeout("net")
        rec = self.place(mgr)
        self.assertEqual(rec["state"], LIVE)
        self.assertEqual(len(self.server.posts()), 1)
        self.assertIsNone(mgr.halted)

    def test_timeout_not_executed_halts_without_second_post(self):
        mgr = self.ready()
        self.server.raise_once[("POST", "/api/v5/trade/order")] = ExchangeTimeout("net")
        rec = self.place(mgr)
        self.assertEqual(rec["state"], UNKNOWN)
        self.assertIn("UNRESOLVED_UNKNOWN", mgr.halted)
        self.assertEqual(len(self.server.posts()), 1)
        with self.assertRaises(TradingHalted):
            self.place(mgr)
        self.assertEqual(len(self.server.posts()), 1)

    def test_50004_after_post_is_unknown_then_resolved(self):
        mgr = self.ready()
        self.server.overrides[("POST", "/api/v5/trade/order")] = [
            top_error("50004", "timeout")]
        rec = self.place(mgr)
        self.assertEqual(rec["state"], UNKNOWN)       # 交易所里没有这笔单
        self.assertIn("UNRESOLVED_UNKNOWN", mgr.halted)
        self.assertEqual(len(self.server.posts()), 1)

    def test_ack_but_confirmation_not_found_halts(self):
        mgr = self.ready()
        not_found = (200, json.dumps({"code": "51603", "msg": "x", "data": []}))
        self.server.overrides[("GET", "/api/v5/trade/order")] = [not_found] * 6
        rec = self.place(mgr)
        self.assertEqual(rec["state"], UNKNOWN)
        self.assertNotEqual(rec["state"], LIVE)
        self.assertIn("UNRESOLVED_UNKNOWN", mgr.halted)
        self.assertEqual(len(self.server.posts()), 1)

    def test_foreign_order_blocks_reconcile(self):
        self.server.orders["MANUAL1"] = order_item(clOrdId="MANUAL1", ordId="777")
        report = self.manager().reconcile()
        self.assertFalse(report["ok"])
        self.assertTrue(any("FOREIGN_ORDER:MANUAL1" in i for i in report["issues"]))

    def test_secrets_never_reach_the_store(self):
        mgr = self.ready()
        leak = f"{CREDS['api_key']} {CREDS['secret']} {CREDS['passphrase']}"
        self.server.overrides[("POST", "/api/v5/trade/order")] = [
            top_error("1", "failed", [{"sCode": "88004", "sMsg": leak}])]
        self.place(mgr)
        text = self.store_path.read_text(encoding="utf-8")
        for value in CREDS.values():
            self.assertNotIn(value, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
