"""
AI Crypto Trader - 实测脚本共用的订单清理。

原则：
- "查不到订单" 不等于 "订单不存在"：下单已受理但查询失败或暂时看不到时，
  仍然按 clOrdId 尝试撤单（撤单不会创建新订单，不可能造成重复下单）。
- "撤单请求被受理" 不等于 "已撤销"：必须再查询，看到终态才算核实。
- 核实不了就明确报告，不宣称清理成功；报告里写明 clOrdId 和人工处理办法。
- 看不懂的订单状态一律不自动处理，只记录并报警。
- 每笔订单的清理相互独立，一笔出现意外异常不影响其他订单，并留下证据。
- 本模块只撤单、只查询，永远不下单。
"""

import time

from exchange_base import ExchangeError

# 交易所订单状态（以官方文档的五个状态为准）
OPEN_STATES = ("live", "partially_filled")
TERMINAL_STATES = ("canceled", "filled", "mmp_canceled")


def describe_error(exc):
    """把异常整理成可记录的字段，不解释含义。"""
    info = {"error_type": type(exc).__name__}
    for name in ("s_code", "s_msg", "code", "msg", "http_status"):
        value = getattr(exc, name, None)
        if value not in (None, ""):
            info[name] = value
    if len(info) == 1:
        info["message"] = str(exc)[:200]
    return info


def _query(exchange, cid):
    """返回 (种类, 内容)：order / none / error。"""
    try:
        order = exchange.get_order(cid)
    except ExchangeError as exc:
        return "error", describe_error(exc)
    if order is None:
        return "none", None
    return "order", order


def _cleanup_one(exchange, cid, evidence, sleep, poll_tries, poll_interval, max_cancel_attempts):
    """
    清理一笔订单。返回 {"cid", "status", ...}，status 为：
    verified（核实到终态且没有成交）、filled（核实到终态但有成交）、unverified（无法核实）。
    """
    last_kind = None
    last_state = None

    def verified(order):
        filled = order["filled_size"]
        evidence.add(
            "cleanup_verified", cid=cid, state=order["state"], accFillSz=filled,
            fee=order["fee"], fee_ccy=order["fee_ccy"], had_fill=filled > 0,
        )
        return {
            "cid": cid, "status": "filled" if filled > 0 else "verified",
            "state": order["state"], "filled": filled,
        }

    def unverified(reason):
        evidence.add("cleanup_unverified", cid=cid, reason=reason, last_state=last_state)
        return {"cid": cid, "status": "unverified", "reason": reason}

    for attempt in range(1, max_cancel_attempts + 1):
        # 先查一次；只有看到明确的终态才可以不撤单。
        kind, payload = _query(exchange, cid)
        last_kind = kind
        if kind == "order":
            last_state = payload["state"]
            evidence.add("cleanup_query", cid=cid, attempt=attempt, result="order",
                         state=payload["state"], accFillSz=payload["filled_size"])
            if payload["state"] in TERMINAL_STATES:
                return verified(payload)
            if payload["state"] not in OPEN_STATES:
                return unverified(f"未知订单状态 {payload['state']}，未自动处理")
        else:
            evidence.add("cleanup_query", cid=cid, attempt=attempt, result=kind,
                         **(payload or {}))

        # 仍在挂单、查不到、或查询失败：都按 clOrdId 撤单。
        try:
            exchange.cancel_order(cid)
            evidence.add("cleanup_cancel_sent", cid=cid, attempt=attempt)
        except ExchangeError as exc:
            # 业务错误或结果不明：都不能当作订单已结束，继续查询核实。
            evidence.add("cleanup_cancel_failed", cid=cid, attempt=attempt,
                         **describe_error(exc))

        # 撤单请求已发出 != 已撤销：反复查询直到看到终态。
        for _ in range(poll_tries):
            sleep(poll_interval)
            kind, payload = _query(exchange, cid)
            last_kind = kind
            if kind == "order":
                last_state = payload["state"]
                if payload["state"] in TERMINAL_STATES:
                    return verified(payload)
                if payload["state"] not in OPEN_STATES:
                    evidence.add("cleanup_query", cid=cid, attempt=attempt,
                                 result="order", state=payload["state"])
                    return unverified(f"未知订单状态 {payload['state']}，未自动处理")

    if last_kind == "order":
        reason = f"撤单后订单仍为 {last_state}，未能撤销"
    elif last_kind == "none":
        reason = "撤单后一直查不到订单：无法确认订单是否存在或是否已撤销"
    else:
        reason = "撤单后订单查询持续失败，无法核实"
    return unverified(reason)


def cleanup_orders(exchange, cids, evidence, say=print, sleep=time.sleep,
                   poll_tries=5, poll_interval=1.0, max_cancel_attempts=2):
    """
    清理给定的 clOrdId 列表。返回：
    {"ok": bool, "verified": [...], "filled": [...], "unverified": [...]}
    ok 为 True 当且仅当每一笔都核实到终态，并且没有任何成交。
    """
    results = {"ok": True, "verified": [], "filled": [], "unverified": []}
    for cid in cids:
        try:
            outcome = _cleanup_one(exchange, cid, evidence, sleep, poll_tries,
                                   poll_interval, max_cancel_attempts)
        except Exception as exc:                    # 清理自身的意外异常也要留下证据
            evidence.add("cleanup_exception", cid=cid, error_type=type(exc).__name__)
            outcome = {"cid": cid, "status": "unverified",
                       "reason": f"清理自身出现意外异常 {type(exc).__name__}"}
        if outcome["status"] == "verified":
            results["verified"].append(outcome["cid"])
        elif outcome["status"] == "filled":
            results["filled"].append(outcome)
            results["ok"] = False
            say(f"警告：{cid} 在清理时发现已有成交 {outcome['filled']}"
                f"（状态 {outcome['state']}），模拟盘余额已变化，请检查")
        else:
            results["unverified"].append(outcome)
            results["ok"] = False
            say(f"警告：{cid} 清理未能核实（{outcome['reason']}）。"
                f"请到 OKX 模拟交易的“当前委托”里检查并手动撤销，clOrdId={cid}")
    evidence.add(
        "cleanup_summary", ok=results["ok"], verified=len(results["verified"]),
        filled=[o["cid"] for o in results["filled"]],
        unverified=[o["cid"] for o in results["unverified"]],
    )
    return results

