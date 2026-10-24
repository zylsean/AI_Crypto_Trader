import base64
import hashlib
import hmac
import json
import re
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlencode

from exchange_base import ExchangeError, ExchangeTimeout
from secrets_util import redact_text


class OKXApiError(ExchangeError):
    """OKX 返回了明确的错误（HTTP 非 200，或顶层 code 不是 0）。"""

    def __init__(self, message, code=None, msg=None, http_status=None):
        super().__init__(message)
        self.code = code
        self.msg = msg
        self.http_status = http_status

    @property
    def unknown_outcome(self):
        """50004 = 接口超时：官方说明它不代表请求成功或失败，必须再查询确认。"""
        return self.code == "50004"


# 错误码线索：只是排查方向，不是结论
HINTS = {
    "50119": "API Key 不存在。官方 FAQ 的首要线索：API 基址与账户所属地区不一致；"
             "同时确认用的是模拟盘 Key，而不是实盘 Key。",
    "50101": "Key 与请求环境不匹配。我们的请求总是带模拟标志，所以最常见的原因是："
             "这是实盘 Key，而不是在"模拟交易"里创建的模拟 Key。",
    "50102": "时间戳超出允许误差：核对手机时间是否准确。",
    "50113": "签名可能无效：核对 Secret 和 Passphrase 是否抄对。",
    "50011": "请求过于频繁：稍后重试。",
    "50004": "接口超时：不代表请求成功或失败，必须再查询确认。",
}


# 如实标明客户端身份（不伪装成浏览器）
USER_AGENT = "AI-Crypto-Trader/0.1 (read-only demo client)"


def _snippet(text, limit=160):
    """把响应开头整理成一小段可读文字：去掉 HTML 标签、压缩空白、截断。"""
    text = re.sub(r"<[^>]*>", " ", str(text))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] if text else "（空）"


def sign(secret, timestamp, method, request_path, body=""):
    """签名 = Base64(HMAC-SHA256(Secret, 时间戳 + 大写方法 + 请求路径(含查询参数) + 请求体))。"""
    prehash = f"{timestamp}{method.upper()}{request_path}{body}"
    digest = hmac.new(
        secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("ascii")


def default_transport(method, url, headers, body, timeout):
    """用标准库发请求，返回 (HTTP 状态码, 响应文本)。网络层失败抛 ExchangeTimeout。"""
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read().decode("utf-8", "replace")
        finally:
            exc.close()
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        raise ExchangeTimeout(f"network error: {type(exc).__name__}") from exc


def _normalize(url):
    return str(url).strip().lower().rstrip("/")


class OKXDemoClient:
    """
    OKX 模拟盘客户端（2b：只读）。

    - 基址必须在白名单内且是 https，否则无法创建
    - 每个请求都带 x-simulated-trading: 1，且不提供关闭它的参数
    - 不记录请求头；错误信息里的 Key、Secret、Passphrase 会被替换为 ***
    - 本类 2b 只提供读取接口，没有下单方法
    """

    def __init__(self, base_url, allowed_base_urls, credentials=None,
                 transport=None, clock=None, timeout=10.0):
        allowed = [_normalize(u) for u in (allowed_base_urls or [])]
        if not allowed or _normalize(base_url) not in allowed:
            raise ValueError("基址不在白名单内。")
        if not _normalize(base_url).startswith("https://"):
            raise ValueError("基址必须使用 https。")
        self._base = _normalize(base_url)
        self._credentials = dict(credentials) if credentials else None
        self._transport = transport or default_transport
        self._clock = clock or time.time
        self._timeout = timeout

    def __repr__(self):
        has = "set" if self._credentials else "none"
        return f"OKXDemoClient(base_url={self._base!r}, credentials={has})"

    @property
    def base_url(self):
        return self._base

    def _secrets(self):
        return list(self._credentials.values()) if self._credentials else []

    def _timestamp(self):
        moment = datetime.fromtimestamp(self._clock(), tz=timezone.utc)
        return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def request(self, method, path, params=None, body=None, private=False):
        query = ("?" + urlencode(params)) if params else ""
        request_path = path + query
        body_text = (
            json.dumps(body, separators=(",", ":")) if body is not None else ""
        )
        headers = {
            "Content-Type": "application/json",
            "x-simulated-trading": "1",
            "User-Agent": USER_AGENT,
        }
        if private:
            if not self._credentials:
                raise ValueError("私有接口需要凭证。")
            timestamp = self._timestamp()
            headers.update({
                "OK-ACCESS-KEY": self._credentials["api_key"],
                "OK-ACCESS-SIGN": sign(
                    self._credentials["secret"], timestamp, method,
                    request_path, body_text,
                ),
                "OK-ACCESS-TIMESTAMP": timestamp,
                "OK-ACCESS-PASSPHRASE": self._credentials["passphrase"],
            })
        status, text = self._transport(
            method.upper(), self._base + request_path, headers,
            body_text.encode("utf-8") if body_text else None, self._timeout,
        )
        return self._parse(status, text)

    def _parse(self, status, text):
        try:
            payload = json.loads(text)
        except ValueError:
            preview = redact_text(_snippet(text), self._secrets())
            raise ExchangeTimeout(
                f"HTTP {status}: 响应不是 JSON；开头内容：{preview}"
            ) from None
        if not isinstance(payload, dict) or "code" not in payload:
            raise ExchangeTimeout(f"HTTP {status}: 响应结构不符合预期")
        code = str(payload.get("code"))
        msg = redact_text(payload.get("msg", ""), self._secrets())[:200]
        if status != 200 or code != "0":
            raise OKXApiError(
                f"HTTP {status} code={code} msg={msg}",
                code=code, msg=msg, http_status=status,
            )
        data = payload.get("data")
        return data if isinstance(data, list) else []

    # ---------- 公共接口（不需要密钥） ----------
    def get_server_time(self):
        data = self.request("GET", "/api/v5/public/time")
        try:
            return int(data[0]["ts"])
        except (IndexError, KeyError, TypeError, ValueError):
            raise ExchangeTimeout("服务器时间响应结构不符合预期") from None

    def check_clock(self, max_offset_seconds=5.0):
        """返回 (本机减服务器的秒数, 是否在允许误差内)。"""
        server_ms = self.get_server_time()
        offset = self._clock() - server_ms / 1000.0
        return offset, abs(offset) <= max_offset_seconds

    def get_instruments(self, inst_type="SPOT", inst_id=None):
        params = {"instType": inst_type}
        if inst_id:
            params["instId"] = inst_id
        return self.request("GET", "/api/v5/public/instruments", params=params)

    # ---------- 私有只读接口 ----------
    def get_account_config(self):
        data = self.request("GET", "/api/v5/account/config", private=True)
        if not data or not isinstance(data[0], dict):
            raise ExchangeTimeout("账户配置响应结构不符合预期")
        return data[0]

    def get_balance(self):
        data = self.request("GET", "/api/v5/account/balance", private=True)
        if not data or not isinstance(data[0], dict):
            raise ExchangeTimeout("余额响应结构不符合预期")
        return data[0]
