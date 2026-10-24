import json
import os
import re
from pathlib import Path


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


class MissingCredentials(Exception):
    """凭证缺失或格式不对。错误信息里只写字段名，不写任何值。"""


_ENV_NAMES = ("OKX_DEMO_API_KEY", "OKX_DEMO_SECRET", "OKX_DEMO_PASSPHRASE")
_FILE_FIELDS = ("api_key", "secret", "passphrase")


def _is_inside(path, root):
    path = Path(path).resolve()
    root = Path(root).resolve()
    return path == root or root in path.parents


def credentials_path_warnings(path):
    """凭证文件放在 iCloud 同步目录里时给出警告。"""
    text = str(Path(path).resolve()).lower()
    warnings = []
    if "mobile documents" in text or "icloud" in text:
        warnings.append("凭证文件似乎在 iCloud 同步目录内，请移到本地目录。")
    return warnings


def load_credentials(file_path=None, environ=None, project_root=None):
    """
    读取模拟盘凭证（API Key、Secret、Passphrase）。
    优先读环境变量，其次读 JSON 文件（字段 api_key、secret、passphrase）。
    凭证文件不得放在项目目录内。
    """
    environ = os.environ if environ is None else environ
    values = [environ.get(name) for name in _ENV_NAMES]
    if all(values):
        return dict(zip(_FILE_FIELDS, values))
    if file_path is None:
        raise MissingCredentials("未找到环境变量，也没有指定凭证文件。")
    path = Path(file_path)
    if project_root is not None and _is_inside(path, project_root):
        raise ValueError("凭证文件不得放在项目目录内。")
    if not path.exists():
        raise MissingCredentials(f"凭证文件不存在：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise MissingCredentials("凭证文件不是有效的 JSON。") from None
    if not isinstance(data, dict):
        raise MissingCredentials("凭证文件格式不对。")
    missing = [
        f for f in _FILE_FIELDS
        if not isinstance(data.get(f), str) or not data.get(f).strip()
    ]
    if missing:
        raise MissingCredentials("凭证文件缺少字段：" + ",".join(missing))
    return {f: data[f] for f in _FILE_FIELDS}
