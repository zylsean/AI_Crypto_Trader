import re


class StartupRefused(Exception):
    """启动守卫未通过，拒绝启动。"""


_HEADER_NAMES = ("OK-ACCESS-KEY", "OK-ACCESS-SIGN", "OK-ACCESS-PASSPHRASE")
_HEADER_PATTERN = re.compile(
    r"(OK-ACCESS-(?:KEY|SIGN|PASSPHRASE))(\s*[:=]\s*)(\S+)", re.IGNORECASE
)


def redact_text(text, secrets=()):
    """把文本里出现的已知秘密值和敏感请求头的值替换为 ***。"""
    text = str(text)
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), "***")
    return _HEADER_PATTERN.sub(r"\1\2***", text)


def redact_headers(headers):
    """返回请求头的副本，敏感请求头的值替换为 ***（不区分大小写）。"""
    sensitive = {name.lower() for name in _HEADER_NAMES}
    return {
        key: ("***" if str(key).lower() in sensitive else value)
        for key, value in headers.items()
    }


def _normalize_url(url):
    return str(url).strip().lower().rstrip("/")


def check_startup_guard(base_url, allowed_base_urls, demo_flag, account_config):
    """
    启动守卫（纯函数）。返回问题列表，空列表表示通过。

    - 基址必须与白名单中的某一项完全一致（且白名单不能为空）
    - 模拟标志必须开启
    - account_config["perm"]（本 API Key 的权限）必须能读到，且不含 withdraw
    account_config 是 GET /api/v5/account/config 返回的 data[0]。
    """
    problems = []
    allowed = [_normalize_url(u) for u in (allowed_base_urls or [])]
    if not allowed:
        problems.append("ALLOWLIST_EMPTY")
    elif _normalize_url(base_url) not in allowed:
        problems.append("BASE_URL_NOT_ALLOWED")
    if demo_flag is not True:
        problems.append("DEMO_FLAG_OFF")
    perm = (account_config or {}).get("perm")
    if not isinstance(perm, str) or not perm.strip():
        problems.append("PERM_UNKNOWN")
    elif "withdraw" in [p.strip().lower() for p in perm.split(",")]:
        problems.append("WITHDRAW_PERMISSION")
    return problems


def enforce_startup_guard(base_url, allowed_base_urls, demo_flag, account_config):
    problems = check_startup_guard(
        base_url, allowed_base_urls, demo_flag, account_config
    )
    if problems:
        raise StartupRefused("启动守卫未通过：" + ",".join(problems))

