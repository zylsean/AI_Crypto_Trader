"""
AI Crypto Trader - 阶段 2c 第 1 步：OrderManager 接真实模拟盘（不成交）。

流程：对账 -> 下一笔低于最新价约 2% 的最小限价买单 -> 确认 LIVE
      -> 模拟重启并对账 -> 撤单 -> 再对账。

安全设计（和 run_2c_demo.py 一致）：
- 默认只做对账检查，不发订单；必须输入 SEND 才会下单。
- 只在输入 D15-OK 之后继续；只用模拟盘 Key。
- 每次运行使用独立的本地订单记录文件，不碰你的正式订单记录。
- 对账发现不认识的订单就停下，不绕过。
- 无论中途出什么问题，结束时都会查询并撤销本次运行产生的挂单。
- 证据文件不含凭证和余额数字。

关于"历史订单登记"：
OrderManager 会把交易所里它不认识的订单当成外来订单并停机。
之前 run_2c_demo.py 留下的测试单还在最近 7 天的历史里，所以这里会先读取
evidence_2c_*.jsonl 里记录过的 clOrdId，只把"已撤销且零成交"的订单登记为历史订单；
有成交或仍在挂单的，一律停下，不当作无影响的历史。
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

from exchange_base import ExchangeError
from okx_demo_exchange import OKXDemoExchange
from order_cleanup import cleanup_orders, describe_error
from order_manager import (
    CANCELED, ID_PATTERN, INTENT, LIVE, NON_TERMINAL, OrderManager, TradingHalted,
)
from order_safety import normalize_limit_buy
from order_store import OrderStore
from risk_guard import RiskGuard, RiskLimits, RiskRejected
from run_2c_demo import (
    ALLOWED_BASES, DEFAULT_BASE, Evidence, prepare_credentials,
)

# 需要清理的本地状态：已经（或可能已经）发给交易所、还没到终态的订单。
# INTENT 表示请求从未发出，不需要撤单。
NEEDS_CLEANUP = NON_TERMINAL - {INTENT}


def collect_known_cids(directory):
    """从之前的证据文件里收集记录过的 clOrdId。"""
    cids = []
    for path in sorted(Path(directory).glob("evidence_2c_*.jsonl")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            cid = item.get("cid") if isinstance(item, dict) else None
            if isinstance(cid, str) and ID_PATTERN.match(cid) and cid not in cids:
                cids.append(cid)
    return cids


def seed_known_orders(store, exchange, cids, base_balance, now_ms):
    """
    把已知的、已撤销且零成交的历史订单登记进本地记录（并写入基线）。
    返回 (登记的 clOrdId 列表, 问题列表)。有任何问题就什么都不写。
    """
    records = []
    problems = []
    for cid in cids:
        try:
            order = exchange.get_order(cid)
        except ExchangeError as exc:
            problems.append(f"查询历史订单 {cid} 失败：{type(exc).__name__}")
            continue
        if order is None:
            continue                      # 交易所里没有（比如当时就被拒了），无需登记
        if order["state"] == "canceled" and order["filled_size"] == 0:
            records.append({
                "type": "order", "cid": cid, "state": CANCELED, "side": order["side"],
                "size": order["size"], "order_type": "limit", "price": None,
                "filled_size": 0.0, "fee": 0.0, "fee_ccy": "",
                "note": "seeded_from_previous_2c_evidence", "updated_ms": now_ms,
            })
        else:
            problems.append(
                f"历史订单 {cid} 状态为 {order['state']}、累计成交 {order['filled_size']}，"
                "不能当作无影响的历史订单，请先到模拟盘页面处理"
            )
    if problems:
        return [], problems
    if records:
        store.append({"type": "baseline", "base_ccy": "BTC",
                      "base": float(base_balance), "updated_ms": now_ms})
        for record in records:
            store.append(record)
    return [record["cid"] for record in records], []


def build_manager(exchange, store_path, secrets, risk_limits, sleep):
    return OrderManager(
        exchange, OrderStore(store_path), RiskGuard(risk_limits),
        base_ccy="BTC", quote_ccy="USDT", sleep=sleep, secrets=list(secrets),
        max_polls=3, poll_interval=1.0,
    )


def run_manager_check(exchange, store_path, evidence, evidence_dir, secrets,
                      send_orders=False, sleep=time.sleep, say=print):
    summary = {"completed": False, "stopped": None}

    def stop(reason):
        summary["stopped"] = reason
        evidence.add("stopped", reason=reason)
        say(f"停止：{reason}")
        return summary

    store_path = Path(store_path)
    seeded = []
    try:
        # 1. 规则、时钟、余额、行情
        say("1/6 读取规则、检查时钟、只读查询余额和行情")
        try:
            rules = exchange.load_rules()
            offset, within = exchange.check_clock()
            balance = exchange.get_balance()
            last = exchange.get_last_price()
        except ExchangeError as exc:
            evidence.add("preflight", ok=False, **describe_error(exc))
            return stop("基础检查失败（规则、时钟、余额或行情）")
        evidence.add("preflight", ok=within, offset_seconds=round(offset, 3),
                     usdt_positive=balance.get("USDT", 0.0) > 0, **rules)
        if not within:
            return stop(f"本机时间与服务器相差 {offset:.1f} 秒，请先校准时间")
        if balance.get("USDT", 0.0) <= 0:
            return stop("模拟盘 USDT 余额不大于 0")

        try:
            plan = normalize_limit_buy(
                Decimal(last) * Decimal("0.98"), rules["minSz"],
                rules["tickSz"], rules["lotSz"], rules["minSz"])
        except (ValueError, ArithmeticError) as exc:
            evidence.add("plan", ok=False, message=str(exc)[:200])
            return stop("无法计算订单")
        px, sz = plan["px"], plan["sz"]
        evidence.add("plan", ok=True, last=last, px=px, sz=sz, send_orders=send_orders)
        say(f"   计划：价格 {px}，数量 {sz}（最小下单量）")

        # 风控额度：只为这次小订单留余量。模拟盘账户里可能本来就有 BTC，
        # 所以持仓上限按"现有持仓 + 这一笔的 10 倍"计算，目的是测订单流程，不是测风控。
        btc = float(balance.get("BTC", 0.0))
        size_f, last_f = float(sz), float(last)
        limits = RiskLimits(
            max_order_notional=size_f * last_f * 2,
            max_position_notional=(btc + size_f * 10) * last_f,
            max_daily_loss=1.0,
        )

        # 2. 登记历史测试订单
        say("2/6 登记之前测试留下的已撤销订单")
        now_ms = int(time.time() * 1000)
        known = collect_known_cids(evidence_dir)
        seeded, problems = seed_known_orders(
            OrderStore(store_path), exchange, known, btc, now_ms)
        evidence.add("seed", ok=not problems, seeded=len(seeded), problems=problems)
        if problems:
            for problem in problems:
                say("   " + problem)
            return stop("历史订单登记失败")

        # 3. 第一次对账
        say("3/6 对账（读取挂单列表和近 7 天订单历史）")
        manager = build_manager(exchange, store_path, secrets, limits, sleep)
        report = manager.reconcile()
        evidence.add("reconcile_1", ok=report["ok"], issues=report["issues"])
        if not report["ok"]:
            for issue in report["issues"]:
                say("   " + issue)
            return stop("对账未通过：发现不认识的订单或不一致，不绕过")

        if not send_orders:
            say("只做对账检查，没有发送订单。需要下单请重新运行并输入 SEND。")
            summary["completed"] = True
            return summary

        # 4. 下单
        say("4/6 通过 OrderManager 下单")
        try:
            record = manager.place_order(
                "buy", size_f, ref_price=last_f, order_type="limit", price=float(px))
        except (TradingHalted, RiskRejected, ValueError) as exc:
            evidence.add("place", ok=False, **describe_error(exc))
            return stop("下单前被拦截：" + type(exc).__name__)
        evidence.add("place", ok=record["state"] == LIVE, cid=record["cid"],
                     state=record["state"], order_id=record.get("order_id"),
                     halted=manager.halted)
        if record["state"] != LIVE:
            return stop(f"订单状态是 {record['state']}，不是 LIVE，请检查证据和记录文件")
        cid = record["cid"]

        # 5. 模拟重启并对账
        say("5/6 模拟重启：新建管理器并对账，订单应仍是 LIVE")
        restarted = build_manager(exchange, store_path, secrets, limits, sleep)
        report = restarted.reconcile()
        state_after = restarted.orders.get(cid, {}).get("state")
        evidence.add("reconcile_after_restart", ok=report["ok"], issues=report["issues"],
                     cid=cid, state=state_after)
        if not report["ok"] or state_after != LIVE:
            for issue in report["issues"]:
                say("   " + issue)
            return stop("重启后对账未通过")

        # 6. 撤单并再次对账
        say("6/6 撤单，再模拟重启对账一次")
        done = restarted.cancel_order(cid)
        evidence.add("cancel", ok=done["state"] == CANCELED, cid=cid,
                     state=done["state"], filled=done.get("filled_size"))
        if done["state"] != CANCELED:
            return stop(f"撤单后状态是 {done['state']}")
        final = build_manager(exchange, store_path, secrets, limits, sleep)
        report = final.reconcile()
        evidence.add("reconcile_final", ok=report["ok"], issues=report["issues"])
        if not report["ok"]:
            for issue in report["issues"]:
                say("   " + issue)
            return stop("撤单后对账未通过")

        summary["completed"] = True
        return summary
    finally:
        result = _cleanup(exchange, store_path, evidence, say, seeded, sleep)
        summary["cleanup_ok"] = result["ok"]
        if not result["ok"]:
            # 清理没能核实或发现成交：无论前面的流程怎样，都不能算"完成"。
            summary["completed"] = False
            if not summary["stopped"]:
                summary["stopped"] = "清理未能核实或发现成交，请按提示手动检查"


def _cleanup(exchange, store_path, evidence, say, seeded_cids, sleep):
    """
    清理本次运行产生的订单（记录文件里除登记的历史订单之外的订单）。
    只处理仍可能在交易所存在的订单；返回 cleanup_orders 的结果字典。
    """
    try:
        orders, _, _ = OrderStore(store_path).load()
    except FileNotFoundError:
        return {"ok": True, "verified": [], "filled": [], "unverified": []}
    except Exception as exc:                   # 记录文件读不了：不能假装清理过
        evidence.add("cleanup_exception", cid=None, error_type=type(exc).__name__)
        say("警告：无法读取本次订单记录，不知道有哪些订单需要清理。"
            "请到 OKX 模拟交易的“当前委托”里检查并手动撤销。")
        return {"ok": False, "verified": [], "filled": [],
                "unverified": [{"cid": None, "reason": "无法读取本次订单记录"}]}

    mine = [cid for cid in orders if cid not in seeded_cids]
    needs = [cid for cid in mine if orders[cid].get("state") in NEEDS_CLEANUP]
    for cid in mine:
        if cid not in needs:
            evidence.add("cleanup_skipped", cid=cid, local_state=orders[cid].get("state"))
    if not needs:
        return {"ok": True, "verified": [], "filled": [], "unverified": []}
    say("清理：查询并撤销本次运行产生的挂单，并核实最终状态")
    return cleanup_orders(exchange, needs, evidence, say=say, sleep=sleep)


def main():
    print("AI Crypto Trader - 阶段 2c 第 1 步：OrderManager 接模拟盘")
    print("=" * 52)
    print("只用模拟盘 Key。对账发现不认识的订单会停下，不会绕过。")
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

    send = input("输入 SEND 才会真正向模拟盘下单，其他输入只做对账检查：").strip() == "SEND"

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    folder = Path(__file__).resolve().parent
    store_path = folder / f"manager_check_{stamp}.jsonl"
    evidence_path = folder / f"evidence_2c_manager_{stamp}.jsonl"
    secrets = list(credentials.values())
    evidence = Evidence(evidence_path, secrets=secrets)
    try:
        summary = run_manager_check(exchange, store_path, evidence, folder, secrets,
                                    send_orders=send)
    except KeyboardInterrupt:
        print("\n已中断。")
        return
    finally:
        credentials = api_key = secret = passphrase = secrets = None

    print()
    print("完成" if summary["completed"] else "未完成：" + str(summary["stopped"]))
    print("证据文件：", evidence_path.name)
    print("本次订单记录：", store_path.name)
    print("请把证据文件内容发给我（里面没有凭证和余额数字）。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断。")
        sys.exit(1)

