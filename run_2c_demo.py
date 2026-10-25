"""
AI Crypto Trader - 阶段 2c：OKX 模拟盘下单最小实测。

安全设计：
- 默认只检查，不发订单；必须手动输入 SEND 才会真正向模拟盘发订单。
- 只在你输入 D15-OK（表示你已自行确认可以使用）后才继续。
- 只用模拟盘 Key（每个请求都带 x-simulated-trading: 1，由交易客户端保证）。
- 只下一笔低于最新价约 2% 的极小限价买单，下单后立刻撤销。
- 无论中途出什么问题，结束时都会尝试查询并撤销本次运行产生的所有挂单。
- 不重发任何订单；结果不明时只查询。
- 遇到地区或权限类错误：记录原始返回并停下，不绕过。
- 凭证只在内存里，不写文件；证据文件会把凭证替换成 ***，且不含余额数字。
"""

import getpass
import json
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

# Pyto 里脚本所在的文件夹不一定在模块搜索路径里，这里显式加上。
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from exchange_base import ExchangeError, ExchangeRejected, ExchangeTimeout
from okx_demo_exchange import OKXDemoExchange
from order_cleanup import cleanup_orders, describe_error
from order_safety import OrderIdRegistry, decimal_value, normalize_limit_buy, round_to_step
from secrets_util import redact_text

DEFAULT_BASE = "https://openapi.okx.com"
ALLOWED_BASES = ["https://openapi.okx.com", "https://www.okx.com"]


class Evidence:
    """把每一步的结果写成一行 JSON。写入前把凭证替换成 ***。"""

    def __init__(self, path, secrets, now=None):
        self.path = Path(path)
        self.secrets = list(secrets)
        self.now = now or (lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self.items = []

    def add(self, step, **info):
        record = {"time": self.now(), "step": step, **info}
        text = redact_text(json.dumps(record, ensure_ascii=False, default=str), self.secrets)
        self.items.append(json.loads(text))
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
        return record


def prepare_credentials(api_key, secret, passphrase):
    """
    清理手机上粘贴凭证时常见的问题，并给出提示。
    提示里绝不包含凭证内容，只说长度和空白问题。
    """
    notes = []
    cleaned = {}
    for label, name, value in (
        ("API Key", "api_key", api_key),
        ("Secret Key", "secret", secret),
        ("Passphrase", "passphrase", passphrase),
    ):
        stripped = value.strip()
        if stripped != value:
            notes.append(f"{label} 前后有空格或换行，已自动去掉")
        if any(ch.isspace() for ch in stripped):
            notes.append(f"{label} 中间含有空白字符，很可能抄错")
        cleaned[name] = stripped
    if len(cleaned["api_key"]) != 36:
        notes.append(f"API Key 长度是 {len(cleaned['api_key'])}，常见长度是 36，请核对")
    if len(cleaned["secret"]) != 32:
        notes.append(f"Secret Key 长度是 {len(cleaned['secret'])}，常见长度是 32，请核对")
    return cleaned, notes


def run_checks(exchange, evidence, send_orders=False, include_reverse=False,
               sleep=time.sleep, say=print, id_registry=None):
    """
    执行实测。返回 {"completed": bool, "stopped": 原因或 None, "placed": [...]}。
    """
    registry = id_registry or OrderIdRegistry()
    placed = []          # 本次运行发出过请求的 clOrdId
    summary = {"completed": False, "stopped": None, "placed": placed}

    def stop(reason):
        summary["stopped"] = reason
        evidence.add("stopped", reason=reason)
        say(f"停止：{reason}")
        return summary

    def fetch(cid):
        """查订单；返回 (订单字典或 None, 错误信息或 None)。"""
        try:
            return exchange.get_order(cid), None
        except ExchangeError as exc:
            return None, describe_error(exc)

    def wait_for_state(cid, wanted, tries=5):
        order = None
        for _ in range(tries):
            order, error = fetch(cid)
            if order is not None and order["state"] in wanted:
                return order, None
            sleep(1.0)
        return order, error

    try:
        # 1. 交易对规则
        say("1/6 读取交易对规则（公共接口）")
        try:
            rules = exchange.load_rules()
        except ExchangeError as exc:
            evidence.add("rules", ok=False, **describe_error(exc))
            return stop("读取交易对规则失败")
        evidence.add("rules", ok=True, **rules)

        # 2. 时钟
        say("2/6 检查本机时钟")
        try:
            offset, within = exchange.check_clock()
        except ExchangeError as exc:
            evidence.add("clock", ok=False, **describe_error(exc))
            return stop("无法读取服务器时间")
        evidence.add("clock", ok=within, offset_seconds=round(offset, 3))
        if not within:
            return stop(f"本机时间与服务器相差 {offset:.1f} 秒，请先校准时间")

        # 3. 余额（不记录数字）
        say("3/6 只读查询模拟盘余额")
        try:
            balance = exchange.get_balance()
        except ExchangeError as exc:
            evidence.add("balance", ok=False, **describe_error(exc))
            return stop("余额查询失败（地区、权限或 Key 问题请把原始错误码发给客服/我）")
        has_usdt = balance.get("USDT", 0.0) > 0
        evidence.add("balance", ok=True, usdt_positive=has_usdt)
        if not has_usdt:
            return stop("模拟盘 USDT 余额不大于 0")

        # 4. 计算订单
        say("4/6 计算一笔低于最新价约 2% 的极小限价买单")
        try:
            last = exchange.get_last_price()
            raw_price = Decimal(last) * Decimal("0.98")
            order = normalize_limit_buy(
                raw_price, rules["minSz"], rules["tickSz"], rules["lotSz"], rules["minSz"])
        except (ExchangeError, ValueError) as exc:
            evidence.add("plan", ok=False, **describe_error(exc))
            return stop("无法计算订单")
        px, sz = order["px"], order["sz"]
        evidence.add("plan", ok=True, last=last, px=px, sz=sz, send_orders=send_orders)
        say(f"   价格 {px}，数量 {sz}（最小下单量）")

        if not send_orders:
            say("只做检查，没有发送订单。需要真正下单请重新运行并输入 SEND。")
            summary["completed"] = True
            return summary

        # 5. 下单 -> 确认 -> 重复 clOrdId -> 撤单
        cid = registry.new_id()
        say("5/6 下单（只发一次，不重发）")
        placed.append(cid)
        try:
            ack = exchange.place_order(cid, "buy", float(sz), "limit", float(px))
        except ExchangeRejected as exc:
            placed.remove(cid)          # 交易所明确拒绝：订单不存在，不需要清理
            evidence.add("place", ok=False, outcome="rejected", cid=cid, **describe_error(exc))
            return stop("下单被拒绝：已记录原始 sCode/sMsg，不绕过")
        except ExchangeTimeout as exc:
            evidence.add("place", ok=False, outcome="unknown", cid=cid, **describe_error(exc))
            return stop("下单结果不明：不会重发，将只查询并清理")
        evidence.add("place", ok=True, outcome="accepted_not_confirmed", cid=cid,
                     order_id=ack.get("order_id"))

        order_info, error = wait_for_state(cid, ("live", "partially_filled", "filled"))
        if order_info is None:
            evidence.add("confirm", ok=False, cid=cid, **(error or {"message": "not found"}))
            return stop("下单后查不到订单：不确认为已挂单，将尝试清理")
        evidence.add("confirm", ok=order_info["state"] == "live", cid=cid,
                     state=order_info["state"], accFillSz=order_info["filled_size"],
                     fee=order_info["fee"], fee_ccy=order_info["fee_ccy"])
        if order_info["state"] != "live" or order_info["filled_size"] > 0:
            return stop(f"订单没有停在 live（实际 {order_info['state']}），请检查原始记录")

        say("   用同一个 clOrdId 再下一次（预期被拒绝，不是重发）")
        try:
            exchange.place_order(cid, "buy", float(sz), "limit", float(px))
            evidence.add("duplicate_clordid", outcome="accepted_unexpectedly", cid=cid)
        except ExchangeRejected as exc:
            evidence.add("duplicate_clordid", outcome="rejected", cid=cid, **describe_error(exc))
        except ExchangeTimeout as exc:
            evidence.add("duplicate_clordid", outcome="unknown", cid=cid, **describe_error(exc))

        say("   撤单")
        try:
            exchange.cancel_order(cid)
            evidence.add("cancel", ok=True, cid=cid)
        except ExchangeError as exc:
            evidence.add("cancel", ok=False, cid=cid, **describe_error(exc))
        final, error = wait_for_state(cid, ("canceled", "filled"))
        evidence.add("after_cancel", cid=cid, state=(final or {}).get("state"),
                     accFillSz=(final or {}).get("filled_size"),
                     fee=(final or {}).get("fee"), fee_ccy=(final or {}).get("fee_ccy"))

        # 6. 故意非法的订单（可选）
        if include_reverse:
            say("6/6 发送故意非法的订单，记录交易所真实返回（预期被拒）")
            tick = decimal_value(rules["tickSz"])
            lot = decimal_value(rules["lotSz"])
            below_min = round_to_step(decimal_value(rules["minSz"]) / 10, lot, "down")
            off_tick = format(Decimal(px) + tick / 10, "f")
            for name, size_text, price_text in (
                ("below_minsz", below_min, px),
                ("off_tick", sz, off_tick),
            ):
                rid = registry.new_id()
                placed.append(rid)
                try:
                    exchange.place_order(rid, "buy", float(size_text), "limit",
                                         float(price_text), validate_locally=False)
                    evidence.add(f"reverse_{name}", outcome="accepted_unexpectedly",
                                 cid=rid, px=price_text, sz=size_text)
                except ExchangeRejected as exc:
                    placed.remove(rid)  # 交易所明确拒绝：订单不存在，不需要清理
                    evidence.add(f"reverse_{name}", outcome="rejected", cid=rid,
                                 px=price_text, sz=size_text, **describe_error(exc))
                except ExchangeTimeout as exc:
                    evidence.add(f"reverse_{name}", outcome="unknown", cid=rid,
                                 px=price_text, sz=size_text, **describe_error(exc))
        else:
            say("6/6 跳过故意非法的订单")

        summary["completed"] = True
        return summary
    finally:
        if placed:
            say("清理：查询并撤销本次运行产生的挂单，并核实最终状态")
            result = cleanup_orders(exchange, placed, evidence, say=say, sleep=sleep)
            summary["cleanup_ok"] = result["ok"]
            if not result["ok"]:
                summary["completed"] = False
                if not summary["stopped"]:
                    summary["stopped"] = "清理未能核实或发现成交，请按提示手动检查"
            try:
                leftovers = [o for o in exchange.get_open_orders()
                             if o["client_order_id"] in placed]
                evidence.add("final_check", our_open_orders=len(leftovers))
                if leftovers:
                    say(f"警告：仍有 {len(leftovers)} 笔本次运行的挂单，请到模拟盘页面手动撤销")
                    summary["completed"] = False
            except ExchangeError as exc:
                evidence.add("final_check", ok=False, **describe_error(exc))
                say("警告：无法确认是否还有挂单，请到模拟盘页面检查")
                summary["completed"] = False


def main():
    print("AI Crypto Trader - 阶段 2c 模拟盘下单最小实测")
    print("=" * 48)
    print("只用模拟盘 Key。遇到地区或权限类错误会记录并停止，不会绕过。")
    print()
    if input("你已自行确认可以使用并且只用模拟盘？输入 D15-OK 继续：").strip() != "D15-OK":
        print("未确认，已退出。")
        return

    base = input(f"API 基址，直接回车使用默认 [{DEFAULT_BASE}]：").strip() or DEFAULT_BASE
    try:
        api_key = getpass.getpass("模拟盘 API Key：")
        secret = getpass.getpass("模拟盘 Secret Key：")
        passphrase = getpass.getpass("模拟盘 Passphrase：")
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。")
        return
    if not all((api_key.strip(), secret.strip(), passphrase.strip())):
        print("凭证不完整，已退出。")
        return
    credentials, notes = prepare_credentials(api_key, secret, passphrase)
    for note in notes:
        print("提示：", note)

    try:
        exchange = OKXDemoExchange(base, ALLOWED_BASES, credentials)
    except ValueError as exc:
        print("无法创建客户端：", exc)
        return

    send = input("输入 SEND 才会真正向模拟盘发送订单，其他输入只做检查：").strip() == "SEND"
    reverse = False
    if send:
        reverse = input("是否追加 2 个故意非法的订单（预期被拒）？输入 YES 追加：").strip() == "YES"

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = Path(__file__).resolve().parent / f"evidence_2c_{stamp}.jsonl"
    evidence = Evidence(path, secrets=list(credentials.values()))
    try:
        summary = run_checks(exchange, evidence, send_orders=send, include_reverse=reverse)
    except KeyboardInterrupt:
        print("\n已中断。")
        return
    finally:
        credentials = api_key = secret = passphrase = None

    print()
    print("完成" if summary["completed"] else "未完成：" + str(summary["stopped"]))
    print("证据文件：", path.name)
    print("请把证据文件内容发给我（里面没有凭证和余额数字）。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断。")
        sys.exit(1)
