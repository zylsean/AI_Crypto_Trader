"""
AI Crypto Trader - 阶段 2c 第 2 步：成交样本。

目的：拿到一笔真实成交的样本，核对
  1. 手续费的方向和币种（fee、feeCcy）；
  2. 累计成交量 accFillSz 的真实样子；
  3. BTC 余额变化 == 成交量 - 手续费（OrderManager 对账会自动核对）；
  4. USDT 变化 == 成交量 x 成交均价。

做法：通过 OrderManager 下一笔价格略高于卖一价的最小限价买单（会立刻成交，
成交价按对手盘，不会按你的限价），然后模拟重启、对账、再对账。

安全设计：
- 只用模拟盘 Key；只在输入 D15-OK 之后继续。
- 默认只做检查（预检、登记历史、对账、算出订单），必须输入 FILL 才会下单。
- 单笔名义金额上限 5 USDT，超过就拒绝；数量取最小下单量，约 1~2 USDT。
- 只买不卖：成交后模拟盘里会多出这一点 BTC，这是预期的结果。
- 如果没有立刻成交，订单会被撤销并如实报告，不会反复下单。
- 无论中途出什么问题，结束时都会查询并撤销本次运行产生的挂单，并核实最终状态。
- 证据文件只记录差额（对账差、USDT 差），不记录余额绝对数字，也没有凭证。
  订单记录文件里有 BTC 基线余额（模拟盘数字），所以不要上传到公开仓库。
"""

import getpass
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
from order_cleanup import describe_error
from order_manager import FILLED, TradingHalted
from order_safety import normalize_limit_buy
from order_store import OrderStore
from risk_guard import RiskLimits, RiskRejected
from run_2c_demo import ALLOWED_BASES, DEFAULT_BASE, Evidence, prepare_credentials
from run_2c_manager_check import (
    _cleanup, build_manager, collect_known_cids, seed_known_orders,
)

MAX_NOTIONAL_USDT = Decimal("5")
ASK_MARGIN = Decimal("1.001")          # 限价 = 卖一价 x 1.001，保证能立刻成交
USDT_TOLERANCE = 1e-6


def run_fill_check(exchange, store_path, evidence, evidence_dir, secrets,
                   fill_orders=False, sleep=time.sleep, say=print):
    summary = {"completed": False, "stopped": None}

    def stop(reason):
        summary["stopped"] = reason
        evidence.add("stopped", reason=reason)
        say(f"停止：{reason}")
        return summary

    store_path = Path(store_path)
    seeded = []
    try:
        # 1. 预检
        say("1/6 读取规则、检查时钟、只读查询余额和行情")
        try:
            rules = exchange.load_rules()
            offset, within = exchange.check_clock()
            balance = exchange.get_balance()
            prices = exchange.get_best_prices()
        except ExchangeError as exc:
            evidence.add("preflight", ok=False, **describe_error(exc))
            return stop("基础检查失败（规则、时钟、余额或行情）")
        evidence.add("preflight", ok=within, offset_seconds=round(offset, 3),
                     usdt_positive=balance.get("USDT", 0.0) > 0, **rules)
        if not within:
            return stop(f"本机时间与服务器相差 {offset:.1f} 秒，请先校准时间")
        if balance.get("USDT", 0.0) <= 0:
            return stop("模拟盘 USDT 余额不大于 0")
        usdt_before = balance["USDT"]
        btc_before = float(balance.get("BTC", 0.0))

        # 2. 计算订单
        say("2/6 计算一笔会立刻成交的最小限价买单")
        try:
            plan = normalize_limit_buy(
                Decimal(prices["ask"]) * ASK_MARGIN, rules["minSz"],
                rules["tickSz"], rules["lotSz"], rules["minSz"])
        except (ValueError, ArithmeticError) as exc:
            evidence.add("plan", ok=False, message=str(exc)[:200])
            return stop("无法计算订单")
        px, sz = plan["px"], plan["sz"]
        notional = Decimal(px) * Decimal(sz)
        evidence.add("plan", ok=notional <= MAX_NOTIONAL_USDT, ask=prices["ask"],
                     px=px, sz=sz, notional=format(notional, "f"), fill_orders=fill_orders)
        say(f"   计划：限价 {px}，数量 {sz}，名义金额约 {notional:.4f} USDT")
        if notional > MAX_NOTIONAL_USDT:
            return stop(f"名义金额超过 {MAX_NOTIONAL_USDT} USDT 上限，拒绝下单")

        size_f, ask_f, px_f = float(sz), float(prices["ask"]), float(px)
        limits = RiskLimits(
            max_order_notional=float(notional) * 2,
            max_position_notional=(btc_before + size_f * 10) * ask_f,
            max_daily_loss=1.0,
        )

        # 3. 登记历史订单、第一次对账
        say("3/6 登记之前测试留下的订单并对账")
        now_ms = int(time.time() * 1000)
        seeded_now, problems = seed_known_orders(
            OrderStore(store_path), exchange, collect_known_cids(evidence_dir),
            btc_before, now_ms)
        seeded.extend(seeded_now)
        evidence.add("seed", ok=not problems, seeded=len(seeded_now), problems=problems)
        if problems:
            for problem in problems:
                say("   " + problem)
            return stop("历史订单登记失败")
        manager = build_manager(exchange, store_path, secrets, limits, sleep)
        report = manager.reconcile()
        evidence.add("reconcile_1", ok=report["ok"], issues=report["issues"])
        if not report["ok"]:
            for issue in report["issues"]:
                say("   " + issue)
            return stop("对账未通过：发现不认识的订单或不一致，不绕过")

        if not fill_orders:
            say("只做检查，没有下单。需要成交样本请重新运行并输入 FILL。")
            summary["completed"] = True
            return summary

        # 4. 下单（应当立刻成交）
        say("4/6 通过 OrderManager 下单（价格略高于卖一价，应立刻成交）")
        try:
            record = manager.place_order(
                "buy", size_f, ref_price=ask_f, order_type="limit", price=px_f)
        except (TradingHalted, RiskRejected, ValueError) as exc:
            evidence.add("place", ok=False, **describe_error(exc))
            return stop("下单前被拦截：" + type(exc).__name__)
        evidence.add(
            "place", ok=record["state"] == FILLED, cid=record["cid"], state=record["state"],
            order_id=record.get("order_id"), accFillSz=record.get("filled_size"),
            avg_price=record.get("avg_price"), fee=record.get("fee"),
            fee_ccy=record.get("fee_ccy"), halted=manager.halted)
        if record["state"] != FILLED:
            return stop(f"订单没有立刻成交（状态 {record['state']}），将撤销并清理，不再重试")
        cid = record["cid"]
        filled = record["filled_size"]
        avg_price = record["avg_price"]
        fee, fee_ccy = record["fee"], record["fee_ccy"]
        say(f"   成交 {filled}，均价 {avg_price}，手续费 {fee} {fee_ccy}")

        # 5. 模拟重启并对账：BTC 余额是否等于 基线 + 成交量 - 手续费
        say("5/6 模拟重启：新建管理器并对账，核对 BTC 和 USDT 变化")
        restarted = build_manager(exchange, store_path, secrets, limits, sleep)
        report = restarted.reconcile()
        try:
            after = exchange.get_balance()
        except ExchangeError as exc:
            evidence.add("balance_after", ok=False, **describe_error(exc))
            return stop("成交后无法读取余额")
        expected_btc = restarted._expected_base()
        btc_diff = float(after.get("BTC", 0.0)) - expected_btc
        evidence.add("reconcile_after_fill", ok=report["ok"], issues=report["issues"],
                     btc_diff=round(btc_diff, 12), fee=fee, fee_ccy=fee_ccy,
                     accFillSz=filled, state=restarted.orders.get(cid, {}).get("state"))
        if not report["ok"]:
            for issue in report["issues"]:
                say("   " + issue)
            return stop("成交后对账未通过：手续费或成交量的记账与余额不符")

        usdt_spent = usdt_before - after.get("USDT", 0.0)
        expected_spent = filled * avg_price + (fee if fee_ccy == "USDT" else 0.0)
        usdt_diff = usdt_spent - expected_spent
        usdt_ok = abs(usdt_diff) <= USDT_TOLERANCE
        evidence.add("usdt_check", ok=usdt_ok, spent_minus_expected=round(usdt_diff, 10))
        if not usdt_ok:
            return stop("USDT 变化与成交不符：对手续费或成交均价的理解可能有误，请检查证据")

        # 6. 再对账一次
        say("6/6 再模拟重启对账一次")
        final = build_manager(exchange, store_path, secrets, limits, sleep)
        report = final.reconcile()
        evidence.add("reconcile_final", ok=report["ok"], issues=report["issues"])
        if not report["ok"]:
            for issue in report["issues"]:
                say("   " + issue)
            return stop("最终对账未通过")

        summary["completed"] = True
        return summary
    finally:
        result = _cleanup(exchange, store_path, evidence, say, seeded, sleep)
        summary["cleanup_ok"] = result["ok"]
        if not result["ok"]:
            summary["completed"] = False
            if not summary["stopped"]:
                summary["stopped"] = "清理未能核实或发现成交，请按提示手动检查"


def main():
    print("AI Crypto Trader - 阶段 2c 第 2 步：成交样本")
    print("=" * 48)
    print("只用模拟盘 Key。会下一笔立刻成交的最小买单（约 1~2 USDT 模拟资金）。")
    print("成交后模拟盘里会多出这一点 BTC，不会卖回。")
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

    fill = input("输入 FILL 才会下单并成交，其他输入只做检查：").strip() == "FILL"

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    folder = Path(__file__).resolve().parent
    store_path = folder / f"fill_check_{stamp}.jsonl"
    evidence_path = folder / f"evidence_2c_fill_{stamp}.jsonl"
    secrets = list(credentials.values())
    evidence = Evidence(evidence_path, secrets=secrets)
    try:
        summary = run_fill_check(exchange, store_path, evidence, folder, secrets,
                                 fill_orders=fill)
    except KeyboardInterrupt:
        print("\n已中断。")
        return
    finally:
        credentials = api_key = secret = passphrase = secrets = None

    print()
    print("完成" if summary["completed"] else "未完成：" + str(summary["stopped"]))
    print("证据文件：", evidence_path.name)
    print("本次订单记录：", store_path.name)
    print("证据文件里没有凭证和余额绝对数字，可以发给我，也可以上传仓库。")
    print("订单记录里含模拟盘的 BTC 基线余额：可以发给我，但不要上传到公开仓库。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断。")
        sys.exit(1)
