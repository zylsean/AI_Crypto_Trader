"""
AI Crypto Trader - 阶段 2c 第 3 步：限价卖单成交样本。

目的：验证卖出路径。重点核对
  1. 卖出手续费的币种和方向（按账户设置，预期从收到的 USDT 里扣）；
  2. BTC 余额变化 == -成交量（手续费不在 BTC 上）；
  3. USDT 变化 == 成交量 x 成交均价 - 手续费。

做法：通过 OrderManager 下一笔价格略低于买一价的最小限价卖单（会立刻成交，
成交价按对手盘），然后模拟重启、对账、再对账。
逻辑和 run_2c_fill_check.py 共用（run_fill_check(side="sell")），安全设计相同：
- 只用模拟盘 Key；只在输入 D15-OK 之后继续；
- 默认只做检查，必须输入 SELL 才会下单；
- 单笔名义金额上限 5 USDT，数量取最小下单量；BTC 持仓不足就不下单；
- 没有立刻成交就撤销并如实报告，不重试；
- 结束时查询并撤销本次运行产生的挂单，并核实最终状态；
- 证据文件没有凭证和余额绝对数字；订单记录里有 BTC 基线余额，不要上传到公开仓库。
"""

import getpass
import sys
from datetime import datetime, timezone
from pathlib import Path

# Pyto 里脚本所在的文件夹不一定在模块搜索路径里，这里显式加上。
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from okx_demo_exchange import OKXDemoExchange
from run_2c_demo import ALLOWED_BASES, DEFAULT_BASE, Evidence, prepare_credentials
from run_2c_fill_check import run_fill_check


def main():
    print("AI Crypto Trader - 阶段 2c 第 3 步：限价卖单样本")
    print("=" * 48)
    print("只用模拟盘 Key。会卖出最小数量的 BTC（约 1~2 USDT 模拟资金），立刻成交。")
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

    sell = input("输入 SELL 才会下单卖出，其他输入只做检查：").strip() == "SELL"

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    folder = Path(__file__).resolve().parent
    store_path = folder / f"sell_check_{stamp}.jsonl"
    evidence_path = folder / f"evidence_2c_sell_{stamp}.jsonl"
    secrets = list(credentials.values())
    evidence = Evidence(evidence_path, secrets=secrets)
    try:
        summary = run_fill_check(exchange, store_path, evidence, folder, secrets,
                                 fill_orders=sell, side="sell")
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

