import math
import re
import secrets as _secrets
import time
from pathlib import Path

from exchange_base import ExchangeError, ExchangeRejected
from secrets_util import redact_text

INTENT = "INTENT"
SENT = "SENT"
LIVE = "LIVE"
PARTIAL = "PARTIAL"
FILLED = "FILLED"
CANCELED = "CANCELED"
REJECTED = "REJECTED"
UNKNOWN = "UNKNOWN"

TERMINAL = {FILLED, CANCELED, REJECTED}
NON_TERMINAL = {INTENT, SENT, LIVE, PARTIAL, UNKNOWN}

# 允许的状态转换。
# INTENT -> REJECTED 只用于重启后确认"请求从未发出"（发请求总是在 SENT 落盘之后）。
ALLOWED = {
    INTENT: {SENT, REJECTED},
    SENT: {LIVE, PARTIAL, FILLED, REJECTED, UNKNOWN},
    LIVE: {PARTIAL, FILLED, CANCELED},
    PARTIAL: {PARTIAL, FILLED, CANCELED},
    UNKNOWN: {LIVE, PARTIAL, FILLED, CANCELED, REJECTED},
    FILLED: set(),
    CANCELED: set(),
    REJECTED: set(),
}

# 交易所状态 -> 我们的状态。没出现在这里的状态一律停止交易（不猜测）。
EXCHANGE_STATE_MAP = {
    "live": LIVE,
    "partially_filled": PARTIAL,
    "filled": FILLED,
    "canceled": CANCELED,
}

ID_PATTERN = re.compile(r"^[A-Za-z0-9]{1,32}$")
EPS = 1e-9

# 停机记录的"已解除"标记：写入后，之后启动的管理器不再继承该停机。
HALT_CLEARED_PREFIX = "CLEARED:"


class TradingHalted(Exception):
    """当前禁止交易。"""


class IllegalTransition(Exception):
    """非法的订单状态转换。"""


def _is_positive_finite(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def write_halt_cleared_marker(store, note, now_ms=None):
    """
    人工确认问题已处理后，写入停机解除标记。
    不需要交易所连接，可以单独在一个小脚本里调用。
    """
    if not isinstance(note, str) or not note.strip():
        raise ValueError("必须提供人工处理说明。")
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    store.append({
        "type": "halt",
        "reason": HALT_CLEARED_PREFIX + "OPERATOR:" + note.strip()[:200],
        "updated_ms": now_ms,
    })


class OrderManager:
    """
    订单管理：状态机、下单流程、启动对账。

    关键规则：
    1. 下单前必须先对账成功，且没有任何非终态订单、没有停止标志、没有 halt。
    2. 先把 INTENT、SENT 写盘，再发请求。
    3. 超时或结果不明 -> UNKNOWN，只按客户端订单 ID 查询，绝不重发。
    4. 下单接口返回成功只表示"已受理"，必须再查订单详情才能确认状态。
    5. 已成交数量以交易所返回的累计值为准。
    6. 任何不一致都停止交易；上次运行留下的停机必须人工解除。
    """

    def __init__(self, exchange, store, risk, *, base_ccy="BTC", quote_ccy="USDT",
                 clock=None, id_factory=None, sleep=None, stop_flag_path=None,
                 secrets=(), max_polls=3, poll_interval=1.0,
                 max_consecutive_errors=3, balance_tolerance=1e-8):
        self.exchange = exchange
        self.store = store
        self.risk = risk
        self.base_ccy = base_ccy
        self.quote_ccy = quote_ccy
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.id_factory = id_factory or self._default_id
        self.sleep = sleep or time.sleep
        self.stop_flag_path = Path(stop_flag_path) if stop_flag_path else None
        self.secrets = list(secrets)
        self.max_polls = max_polls
        self.poll_interval = poll_interval
        self.max_consecutive_errors = max_consecutive_errors
        self.balance_tolerance = balance_tolerance

        self.orders, self.baseline, halts = store.load()

        # 停机来源："persisted"=上次运行遗留，"runtime"=本次运行中产生，
        # "reconcile"=本次对账自己产生。只有最后一种允许重跑对账后自动解除。
        self.halted = None
        self._halt_source = None
        self._in_reconcile = False
        self._load_persisted_halt(halts)

        self.reconciled = False
        self._errors = 0

    # ---------- 基础工具 ----------
    @staticmethod
    def _halt_reason(entry):
        if isinstance(entry, dict):
            return str(entry.get("reason") or "PERSISTED_HALT")
        return str(entry) or "PERSISTED_HALT"

    def _load_persisted_halt(self, halts):
        """历史停机记录不能被启动流程静默忽略。"""
        if not halts:
            return
        if not isinstance(halts, (list, tuple)):
            self.halted = "UNREADABLE_HALT_RECORDS"
            self._halt_source = "persisted"
            return
        reason = self._halt_reason(halts[-1])
        if reason.startswith(HALT_CLEARED_PREFIX):
            return
        self.halted = reason
        self._halt_source = "persisted"

    def _default_id(self):
        return "ACT" + str(self.clock()) + _secrets.token_hex(3)

    def _safe(self, value):
        return redact_text(value, self.secrets)[:300]

    def _persist(self, cid, **fields):
        fields["updated_ms"] = self.clock()
        record = self.orders.setdefault(cid, {"cid": cid})
        record.update(fields)
        self.store.append({"type": "order", "cid": cid, **fields})

    def halt(self, reason):
        reason = self._safe(reason)
        if self.halted is None:
            self.halted = reason
            self._halt_source = "reconcile" if self._in_reconcile else "runtime"
        self.store.append(
            {"type": "halt", "reason": reason, "updated_ms": self.clock()}
        )

    def operator_clear_halt(self, note):
        """
        人工确认问题已处理后解除停机。
        解除后 reconciled 回到 False，必须重新对账成功才能交易。
        """
        if not isinstance(note, str) or not note.strip():
            raise ValueError("必须提供人工处理说明。")
        write_halt_cleared_marker(self.store, self._safe(note), self.clock())
        self.halted = None
        self._halt_source = None
        self.reconciled = False

    def _call(self, function, *args):
        """调用交易所并统计连续错误。明确拒绝说明交易所有回应，不算错误。"""
        try:
            result = function(*args)
        except ExchangeRejected:
            self._errors = 0
            raise
        except Exception:
            self._errors += 1
            if self._errors >= self.max_consecutive_errors:
                self.halt("CONSECUTIVE_ERRORS")
            raise
        self._errors = 0
        return result

    def can_trade_reason(self):
        """返回禁止交易的原因；可以交易则返回 None。"""
        if self.halted:
            return f"HALTED:{self.halted}"
        if self.stop_flag_path is not None and self.stop_flag_path.exists():
            return "STOP_FLAG"
        if not self.reconciled:
            return "NOT_RECONCILED"
        active = [c for c, r in self.orders.items() if r["state"] in NON_TERMINAL]
        if active:
            return "OPEN_ORDERS:" + ",".join(sorted(active))
        return None

    # ---------- 状态机 ----------
    def transition(self, cid, new_state, **fields):
        old = self.orders[cid]["state"]
        if new_state not in ALLOWED.get(old, set()):
            raise IllegalTransition(f"{cid}: {old} -> {new_state}")
        self._persist(cid, state=new_state, **fields)

    def _apply_exchange_state(self, cid, ex):
        """用交易所返回的订单信息更新本地记录。有任何异常就停止交易。"""
        record = self.orders[cid]
        new_state = EXCHANGE_STATE_MAP.get(ex.get("state"))
        if new_state is None:
            self.halt(f"UNKNOWN_EXCHANGE_STATE:{cid}:{ex.get('state')}")
            return
        filled = ex.get("filled_size", 0.0)
        if not isinstance(filled, (int, float)) or isinstance(filled, bool) \
                or not math.isfinite(filled) or filled < 0:
            self.halt(f"BAD_FILLED_SIZE:{cid}")
            return
        previous = record.get("filled_size", 0.0) or 0.0
        if filled < previous - EPS:
            self.halt(f"FILL_DECREASED:{cid}")
            return
        if filled > record["size"] + EPS:
            self.halt(f"OVERFILLED:{cid}")
            return

        fields = {
            "filled_size": filled,
            "avg_price": ex.get("avg_price", 0.0),
            "fee": ex.get("fee", 0.0),
            "fee_ccy": ex.get("fee_ccy", ""),
            "order_id": ex.get("order_id", record.get("order_id")),
        }
        unchanged = all(record.get(k) == v for k, v in fields.items())
        if new_state == record["state"] and new_state != PARTIAL:
            if not unchanged:
                self._persist(cid, **fields)
            return
        if new_state == record["state"] and unchanged:
            return
        try:
            self.transition(cid, new_state, **fields)
        except IllegalTransition as exc:
            self.halt(f"ILLEGAL_TRANSITION:{exc}")

    # ---------- 下单 ----------
    def _base_position(self):
        balance = self._call(self.exchange.get_balance)
        return float(balance.get(self.base_ccy, 0.0))

    def place_order(self, side, size, ref_price, order_type=None, price=None):
        if side not in ("buy", "sell"):
            raise ValueError("side 必须是 buy 或 sell。")
        if not _is_positive_finite(size):
            raise ValueError("size 必须是大于 0 的有限数字。")
        if not _is_positive_finite(ref_price):
            raise ValueError("ref_price 必须是大于 0 的有限数字。")
        if order_type is None:
            raise ValueError("必须显式指定 order_type，禁止使用默认订单类型。")
        if order_type not in ("market", "limit"):
            raise ValueError("order_type 必须是 market 或 limit。")
        if order_type == "limit" and not _is_positive_finite(price):
            raise ValueError("限价单必须提供有效的 price。")

        reason = self.can_trade_reason()
        if reason:
            raise TradingHalted(reason)

        self.risk.check(side, size, ref_price, self._base_position())

        cid = self.id_factory()
        if not isinstance(cid, str) or not ID_PATTERN.match(cid):
            raise ValueError("客户端订单 ID 只能含字母和数字，长度 1 到 32。")
        if cid in self.orders:
            raise ValueError("客户端订单 ID 重复。")

        self._persist(
            cid, state=INTENT, side=side, size=size, order_type=order_type,
            price=price, ref_price=ref_price, filled_size=0.0,
            created_ms=self.clock(),
        )
        self.transition(cid, SENT)
        try:
            result = self._call(
                self.exchange.place_order, cid, side, size, order_type, price
            )
        except ExchangeRejected as exc:
            self.transition(cid, REJECTED, reason=self._safe(exc))
            return dict(self.orders[cid])
        except Exception as exc:
            # 超时、断网或无法解析：不知道结果。只查询，不重发。
            self.transition(cid, UNKNOWN, reason=self._safe(exc))
            self.resolve_unknown(cid)
            return dict(self.orders[cid])

        # 下单接口成功响应只表示请求已被受理，
        # 不能据此认定订单已经挂单。状态保持 SENT，只记录交易所订单号。
        order_id = result.get("order_id") if isinstance(result, dict) else None
        self._persist(cid, order_id=order_id)

        # 必须再查订单详情才能确认。查询失败或查不到，结果仍不确定：
        # 转 UNKNOWN，由 resolve_unknown 只按客户端订单 ID 重试查询，
        # 仍然确认不了就停止交易。绝不重发订单。
        try:
            ex = self._call(self.exchange.get_order, cid)
        except Exception as exc:
            self.transition(cid, UNKNOWN, reason=self._safe(exc))
            self.resolve_unknown(cid)
            return dict(self.orders[cid])

        if ex is None:
            self.transition(cid, UNKNOWN, reason="order_confirmation_missing")
            self.resolve_unknown(cid)
            return dict(self.orders[cid])

        self._apply_exchange_state(cid, ex)
        return dict(self.orders[cid])

    # ---------- 查询与更新 ----------
    def resolve_unknown(self, cid):
        """按客户端订单 ID 查询以解除 UNKNOWN。查不到就保持 UNKNOWN 并停止交易。"""
        for _ in range(self.max_polls):
            try:
                ex = self._call(self.exchange.get_order, cid)
            except Exception:
                self.sleep(self.poll_interval)
                continue
            if ex is not None:
                self._apply_exchange_state(cid, ex)
                return True
            self.sleep(self.poll_interval)
        self.halt(f"UNRESOLVED_UNKNOWN:{cid}")
        return False

    def refresh(self, cid):
        record = self.orders[cid]
        if record["state"] in TERMINAL:
            return dict(record)
        try:
            ex = self._call(self.exchange.get_order, cid)
        except Exception:
            return dict(record)
        if ex is None:
            if record["state"] in (LIVE, PARTIAL):
                self.halt(f"ORDER_MISSING:{cid}")
            return dict(record)
        self._apply_exchange_state(cid, ex)
        return dict(self.orders[cid])

    def refresh_open_orders(self):
        """更新所有非终态订单，返回处理的数量。"""
        count = 0
        for cid in [c for c, r in self.orders.items() if r["state"] in NON_TERMINAL]:
            if self.orders[cid]["state"] == UNKNOWN:
                self.resolve_unknown(cid)
            else:
                self.refresh(cid)
            count += 1
        return count

    def cancel_order(self, cid):
        record = self.orders[cid]
        if record["state"] not in (LIVE, PARTIAL):
            raise ValueError("只能撤销 LIVE 或 PARTIAL 状态的订单。")
        try:
            self._call(self.exchange.cancel_order, cid)
        except ExchangeError:
            pass
        return self.refresh(cid)

    # ---------- 启动对账 ----------
    def _expected_base(self):
        total = self.baseline["base"]
        for record in self.orders.values():
            filled = record.get("filled_size", 0.0) or 0.0
            total += filled if record["side"] == "buy" else -filled
            if record.get("fee_ccy") == self.base_ccy:
                total -= record.get("fee", 0.0) or 0.0
        return total

    def reconcile(self):
        """
        启动对账。返回 {"ok": bool, "issues": [...]}。
        只有对账完全一致才允许交易；有任何问题就 halt。
        只核对基础币余额，计价币余额的核对留给 2b/2c。

        停机规则：
        - 上次运行遗留的停机（persisted）和本次运行中产生的停机（runtime），
          重跑对账不会解除，必须调用 operator_clear_halt 人工解除。
        - 只有"本次对账自己产生的停机"允许在问题修复后重跑对账来解除。
        """
        self.reconciled = False

        if self.halted:
            if self._halt_source != "reconcile":
                tag = "PERSISTED_HALT" if self._halt_source == "persisted" \
                    else "ACTIVE_HALT"
                return {"ok": False, "issues": [f"{tag}:{self.halted}"]}
            # 上一次对账自己造成的停机，允许重新检查。
            self.halted = None
            self._halt_source = None
            cleared_previous = True
        else:
            cleared_previous = False

        self._in_reconcile = True
        try:
            report = self._reconcile_inner()
        finally:
            self._in_reconcile = False

        if report["ok"] and cleared_previous:
            # 让之后启动的管理器不再继承上一次对账失败留下的停机。
            self.store.append({
                "type": "halt",
                "reason": HALT_CLEARED_PREFIX + "RECONCILE_RERUN_OK",
                "updated_ms": self.clock(),
            })
        return report

    def _reconcile_inner(self):
        issues = []

        try:
            open_orders = self._call(self.exchange.get_open_orders)
            recent_orders = self._call(self.exchange.get_recent_orders)
            balance = self._call(self.exchange.get_balance)
        except Exception as exc:
            self.halt(f"RECONCILE_FETCH_FAILED:{exc}")
            return {"ok": False,
                    "issues": [f"RECONCILE_FETCH_FAILED:{self._safe(exc)}"]}

        exchange_orders = {}
        for order in list(recent_orders) + list(open_orders):
            cid = order.get("client_order_id")
            if not cid:
                issues.append(f"FOREIGN_NO_CLIENT_ID:{order.get('order_id')}")
            else:
                exchange_orders[cid] = order

        for cid in exchange_orders:
            if cid not in self.orders:
                issues.append(f"FOREIGN_ORDER:{cid}")

        for cid, record in list(self.orders.items()):
            ex = exchange_orders.get(cid)
            state = record["state"]
            if state in TERMINAL:
                mapped = EXCHANGE_STATE_MAP.get(ex.get("state")) if ex else None
                if ex is not None and mapped != state:
                    issues.append(f"STATE_MISMATCH:{cid}:{state}:{ex.get('state')}")
                continue
            if state == INTENT:
                if ex is None:
                    self.transition(cid, REJECTED, reason="never_sent")
                else:
                    issues.append(f"INTENT_BUT_ON_EXCHANGE:{cid}")
                continue
            if state == SENT:
                self.transition(cid, UNKNOWN, reason="recovered_after_restart")
                state = UNKNOWN
            if ex is None:
                tag = "UNRESOLVED_UNKNOWN" if state == UNKNOWN else "ORDER_MISSING"
                issues.append(f"{tag}:{cid}")
                continue
            self._apply_exchange_state(cid, ex)

        if self.halted:
            issues.append(f"HALTED_DURING_RECONCILE:{self.halted}")

        if self.baseline is None:
            if self.orders:
                issues.append("NO_BASELINE")
            else:
                self.baseline = {
                    "type": "baseline", "base_ccy": self.base_ccy,
                    "base": float(balance.get(self.base_ccy, 0.0)),
                    "updated_ms": self.clock(),
                }
                self.store.append(self.baseline)

        if self.baseline is not None:
            expected = self._expected_base()
            actual = float(balance.get(self.base_ccy, 0.0))
            if abs(actual - expected) > self.balance_tolerance:
                issues.append(
                    f"BALANCE_MISMATCH:expected={expected:.10f}:actual={actual:.10f}"
                )

        if issues:
            self.halt("RECONCILE_FAILED:" + ";".join(issues[:3]))
            return {"ok": False, "issues": issues}

        self.reconciled = True
        return {"ok": True, "issues": []}
