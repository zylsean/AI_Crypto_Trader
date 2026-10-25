"""
AI Crypto Trader - Phase 2b/2c access precheck

Purpose:
- Test public API connectivity.
- Test read-only access to the demo trading account.
- Never place, cancel, or modify orders.
- Never persist API credentials.
- Never print API credentials or signatures.
"""

import base64
import getpass
import hashlib
import hmac
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

from datetime import datetime, timezone


DEFAULT_BASE_URL = "https://openapi.okx.com"
SIMULATION_HEADER = "x-simulated-trading"
TIMEOUT_SECONDS = 15


def utc_timestamp():
    """Return an OKX-compatible UTC timestamp."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def safe_message(message):
    """Avoid printing likely credential values."""
    if not isinstance(message, str):
        return ""

    message = re.sub(
        r"(?i)(api.?key|secret.?key|passphrase|signature)"
        r"\s*[:=]\s*\S+",
        r"\1=[REDACTED]",
        message,
    )

    return message[:200]


def request_json(base_url, path, credentials=None):
    """
    Send one HTTP request.

    Every request includes x-simulated-trading: 1.
    credentials=None means a public request.
    """

    url = base_url.rstrip("/") + path
    method = "GET"
    body = ""

    headers = {
        SIMULATION_HEADER: "1",
        "Accept": "application/json",
        "User-Agent": "AI-Crypto-Trader-ReadOnly-Test/0.1",
    }

    if credentials is not None:
        api_key, secret_key, passphrase = credentials

        timestamp = utc_timestamp()
        prehash = timestamp + method + path + body

        signature = base64.b64encode(
            hmac.new(
                secret_key.encode("utf-8"),
                prehash.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("utf-8")

        headers.update({
            "OK-ACCESS-KEY": api_key,
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": passphrase,
        })

    request = urllib.request.Request(
        url=url,
        data=None,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=TIMEOUT_SECONDS,
        ) as response:
            http_status = response.status
            raw = response.read().decode("utf-8")

    except urllib.error.HTTPError as exc:
        http_status = exc.code

        try:
            raw = exc.read().decode("utf-8")
        except Exception:
            raw = ""

    except Exception as exc:
        # Do not print exception details that could contain
        # request or environment information.
        print("网络请求异常：", type(exc).__name__)
        return None, None

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        print("HTTP 状态码：", http_status)
        print("响应不是有效 JSON。")
        return http_status, None

    return http_status, payload


def print_response_summary(label, http_status, payload):
    """Print a minimal response summary without credentials."""

    print("\n---", label, "---")

    if http_status is None:
        print("结果：未取得 HTTP 响应")
        return

    print("HTTP 状态码：", http_status)

    if payload is None:
        print("结果：无法解析 JSON")
        return

    code = payload.get("code")
    msg = safe_message(payload.get("msg", ""))

    print("code：", code)
    print("msg：", msg)

    if code == "0":
        print("接口层结果：成功")
    else:
        print("接口层结果：失败或需要进一步诊断")


def main():
    print("AI Crypto Trader - 模拟盘只读访问测试")
    print("=" * 48)
    print("本脚本不会下单、撤单或修改账户设置。")
    print("凭证只在本次运行期间使用，不写入文件。")
    print()

    default_base = os.environ.get(
        "OKX_BASE_URL",
        DEFAULT_BASE_URL,
    )

    base_url = input(
        "API 基址，直接回车使用默认值 "
        f"[{default_base}]："
    ).strip()

    if not base_url:
        base_url = default_base

    parsed = urllib.parse.urlparse(base_url)

    # Require HTTPS and reject unexpected URL components.
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        print("基址格式不符合安全要求，停止。")
        return

    base_url = base_url.rstrip("/")

    print("\n第一项：测试公共服务器时间")

    http_status, payload = request_json(
        base_url,
        "/api/v5/public/time",
    )

    print_response_summary(
        "公共时间接口",
        http_status,
        payload,
    )

    if payload is None or payload.get("code") != "0":
        print("公共接口检查未通过，停止后续测试。")
        return

    print("\n第二项：准备只读查询模拟盘账户余额")
    print("请输入模拟盘 API Key 的凭证。")
    print("请勿将凭证发送给 ChatGPT 或其他人。")
    print()

    try:
        api_key = getpass.getpass("API Key：")
        secret_key = getpass.getpass("Secret Key：")
        passphrase = getpass.getpass("Passphrase：")
    except (EOFError, KeyboardInterrupt):
        print("\n用户取消，未发送私有请求。")
        return

    if not all((api_key, secret_key, passphrase)):
        print("凭证不完整，停止。")
        return

    credentials = (api_key, secret_key, passphrase)

    try:
        path = "/api/v5/account/balance?ccy=USDT"

        http_status, payload = request_json(
            base_url,
            path,
            credentials=credentials,
        )

        print_response_summary(
            "模拟盘账户余额接口",
            http_status,
            payload,
        )

        if payload is None:
            print("结果：未取得可验证的账户响应。")
            return

        if payload.get("code") != "0":
            print("结果：账户查询未通过。")
            print("请保存脱敏后的错误码，暂不重试。")
            return

        data = payload.get("data")

        if not isinstance(data, list) or not data:
            print("结果：响应中没有可用的账户数据。")
            return

        details = data[0].get("details", [])

        usdt = next(
            (
                item for item in details
                if item.get("ccy") == "USDT"
            ),
            None,
        )

        if usdt is None:
            print("结果：响应中未找到 USDT 余额记录。")
            return

        avail_balance = usdt.get("availBal")

        print("\nUSDT 余额检查：")

        if avail_balance is None or avail_balance == "":
            print("可用余额字段缺失，不能判定通过。")
            return

        try:
            available = float(avail_balance)
        except (TypeError, ValueError):
            print("可用余额不是有效数字，不能判定通过。")
            return

        print("USDT 可用余额：", avail_balance)

        if available > 0:
            print("只读账户查询：PASS")
            print("USDT 可用余额大于 0：PASS")
        else:
            print("只读账户查询：PASS")
            print("USDT 可用余额大于 0：FAIL")

        print("\n本脚本已结束。没有发送任何交易订单。")

    finally:
        # Drop local references; this is not a guarantee of
        # secure memory erasure.
        credentials = None
        api_key = None
        secret_key = None
        passphrase = None


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n测试已中断。")
        sys.exit(1)
