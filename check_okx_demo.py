import sys
from pathlib import Path

# 兼容 Pyto：确保当前项目目录可以被 Python 找到
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from exchange_base import ExchangeError
from okx_demo import HINTS, OKXApiError, OKXDemoClient
from secrets_util import (
    MissingCredentials, check_startup_guard, credentials_path_warnings,
    load_credentials, locate_credentials_file,
)

# =========================
# 配置（只读检查，不会下单）
# =========================
# 候选基址：官方开发者文档与帮助中心提到的 OKX 域名。
# 确认哪个能用之后，把它填到 BASE_URL，以后只用这一个。
CANDIDATE_BASE_URLS = [
    "https://openapi.okx.com",   # 开发者文档：全球
    "https://www.okx.com",       # 帮助中心 FAQ 提到的默认域名
    "https://us.okx.com",        # 美国、澳大利亚
    "https://eea.okx.com",       # 欧盟
]
BASE_URL = "https://openapi.okx.com"   # 2b 验证通过的基址（D16）

# 凭证文件必须放在项目目录之外、也不要放在 iCloud 同步目录内。
# 内容是 JSON：{"api_key": "...", "secret": "...", "passphrase": "..."}
# 推荐位置：和项目文件夹并排（即项目文件夹的上一层，在 Pyto 里通常就是 Documents）。
CREDENTIALS_FILE_NAME = "okx_demo_credentials.json"
CREDENTIALS_CANDIDATES = list(dict.fromkeys([
    PROJECT_ROOT.parent / CREDENTIALS_FILE_NAME,          # 项目文件夹的上一层（推荐）
    Path.home() / "Documents" / CREDENTIALS_FILE_NAME,
    Path.home() / CREDENTIALS_FILE_NAME,
    PROJECT_ROOT / CREDENTIALS_FILE_NAME,                 # 仅用于发现误放，不会被采用
]))


def run_diagnosis(candidates, credentials, transport=None, clock=None, out=print):
    """
    依次检查候选基址：公共时间接口 -> 私有账户配置 -> 余额，全部只读。
    输出里的 code/msg 来自 OKX，线索只是排查方向，不是结论。
    返回 {"ok": bool, "working_base_url": str|None, "problems": [...]}。
    """
    result = {"ok": False, "working_base_url": None, "problems": [],
              "key_recognized_at": []}
    public_errors = []
    for base in candidates:
        out(f"--- 尝试 {base}")
        client = OKXDemoClient(
            base, [base], credentials, transport=transport, clock=clock
        )
        try:
            offset, clock_ok = client.check_clock()
        except ExchangeError as exc:
            out(f"公共时间接口失败：{exc}")
            public_errors.append(str(exc))
            continue
        out(f"网络连通；本机与服务器时间差 {offset:+.1f} 秒")
        if not clock_ok:
            out("时间差超过 5 秒，请先校准手机时间再重试。")
            result["problems"].append("CLOCK_OFFSET")
            return result

        try:
            config = client.get_account_config()
        except OKXApiError as exc:
            out(f"私有接口失败：HTTP {exc.http_status} code={exc.code} msg={exc.msg}")
            if exc.code in HINTS:
                out(f"线索（不是结论）：{HINTS[exc.code]}")
            if exc.code == "50101":
                result["key_recognized_at"].append(base)
            continue
        except ExchangeError as exc:
            out(f"私有接口失败：{exc}")
            continue

        result["working_base_url"] = base
        out("私有只读接口成功。")
        out(
            f"账户权限 perm={config.get('perm')}  "
            f"acctLv={config.get('acctLv')}  feeType={config.get('feeType')}"
        )
        problems = check_startup_guard(base, [base], True, config)
        result["problems"] = problems
        if problems:
            out("启动守卫未通过：" + ",".join(problems))
        else:
            out("启动守卫通过（域名在白名单内、模拟标志开启、权限不含 withdraw）。")

        try:
            balance = client.get_balance()
            details = balance.get("details")
            if isinstance(details, list):
                shown = [f"{d.get('ccy')}={d.get('cashBal')}" for d in details[:10]]
                out("模拟账户余额：" + (", ".join(shown) if shown else "（空）"))
            else:
                out("余额接口成功（返回结构未识别，已忽略）。")
        except ExchangeError as exc:
            out(f"余额接口失败：{exc}")

        result["ok"] = not problems
        return result

    if public_errors and len(public_errors) == len(candidates):
        result["problems"].append("PUBLIC_ENDPOINT_FAILED")
        out("公共接口（不需要密钥）在所有域名上都失败了。")
        out("这说明问题出在网络访问层，不在 Key、域名或签名上：你的凭证还没有被用到。")
        if all("HTTP 403" in e for e in public_errors):
            out("HTTP 403 表示请求被服务器或它前面的防护层拒绝访问，"
                "常见原因是当前网络出口或客户端不被允许。")
        out("本项目不提供绕过访问限制的功能。请把上面的输出（不含密钥）发给 OKX 客服，")
        out("询问你当前的网络环境能否访问 OKX API。不要发 Key、Secret、Passphrase。")
        return result
    if result["key_recognized_at"]:
        out("推断（不是结论）：50101 说明这些基址的服务器认得这个 Key（否则会是 50119），")
        out("但 Key 与环境不匹配：" + "、".join(result["key_recognized_at"]))
        out("最常见原因：这是实盘 Key，不是在"模拟交易"里创建的模拟 Key。")
        out("请在 OKX 确认 Key 的创建位置；实盘 Key 不要用于本项目。")
    out("所有候选基址都失败了。请对照上面每个基址的 code/msg 和线索排查；")
    out("把输出（已脱敏，不含密钥）发给 OKX 客服或我，不要发 Key、Secret、Passphrase。")
    return result


def main():
    print("=" * 55)
    print("OKX 模拟盘只读检查（不会下单）")
    print("=" * 55)
    path, misplaced = locate_credentials_file(CREDENTIALS_CANDIDATES, PROJECT_ROOT)
    for bad in misplaced:
        print("错误：凭证文件在项目目录内，有被提交到 Git 的风险，请移到项目文件夹的上一层：", bad)
    if path is None:
        print("没有找到凭证文件。已查找这些位置：")
        for candidate in CREDENTIALS_CANDIDATES:
            print("  -", candidate)
        print("请把", CREDENTIALS_FILE_NAME, "放到项目文件夹的上一层（与项目文件夹并排）。")
        return
    print("使用凭证文件：", path)
    for warning in credentials_path_warnings(path):
        print("警告：", warning)
    try:
        credentials = load_credentials(path, project_root=PROJECT_ROOT)
    except (MissingCredentials, ValueError) as exc:
        print("无法读取凭证：", exc)
        return

    if BASE_URL is not None:
        if BASE_URL not in CANDIDATE_BASE_URLS:
            print("BASE_URL 不在候选列表内，停止。")
            return
        candidates = [BASE_URL]
    else:
        candidates = CANDIDATE_BASE_URLS

    result = run_diagnosis(candidates, credentials)
    print("-" * 55)
    if result["working_base_url"]:
        print("可用基址：", result["working_base_url"])
        if BASE_URL is None:
            print("下一步：把它填到本文件的 BASE_URL，以后只用这一个。")
    print("结果：", "通过" if result["ok"] else "未通过")
    print("=" * 55)


if __name__ == "__main__":
    main()
