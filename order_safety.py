
"""AI Crypto Trader: Phase 2c order safety primitives.

This module performs no network requests and stores no credentials.
"""

from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import secrets
import string
import time


VALID_ORDER_STATES = {
    "live",
    "partially_filled",
    "filled",
    "canceled",
    "mmp_canceled",
}

SIMULATION_HEADER = "x-simulated-trading"


def decimal_value(value, field_name="value"):
    """Convert a value to a finite Decimal without float arithmetic."""
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{field_name} must be a valid decimal")

    if not result.is_finite():
        raise ValueError(f"{field_name} must be finite")

    return result


def round_to_step(value, step, direction="down"):
    """Round to a multiple of step using Decimal arithmetic."""
    value = decimal_value(value)
    step = decimal_value(step, "step")

    if step <= 0:
        raise ValueError("step must be positive")

    if direction == "down":
        rounding = ROUND_FLOOR
    elif direction == "up":
        rounding = ROUND_CEILING
    else:
        raise ValueError("direction must be 'down' or 'up'")

    units = (value / step).to_integral_value(rounding=rounding)
    result = units * step

    return format(result, "f")


def normalize_limit_buy(price, quantity, tick_sz, lot_sz, min_sz):
    """Round a limit-buy price and quantity down to exchange steps."""
    price = decimal_value(price, "price")
    quantity = decimal_value(quantity, "quantity")
    min_sz = decimal_value(min_sz, "min_sz")

    if price <= 0 or quantity <= 0 or min_sz <= 0:
        raise ValueError("price, quantity and min_sz must be positive")

    rounded_price = decimal_value(
        round_to_step(price, tick_sz, "down")
    )
    rounded_quantity = decimal_value(
        round_to_step(quantity, lot_sz, "down")
    )

    if rounded_quantity < min_sz:
        raise ValueError("quantity is below min_sz after rounding")

    return {
        "px": format(rounded_price, "f"),
        "sz": format(rounded_quantity, "f"),
    }


def validate_normalized_order(order, tick_sz, lot_sz, min_sz):
    """Reject invalid prices or quantities before any network call."""
    price = decimal_value(order.get("px"), "px")
    quantity = decimal_value(order.get("sz"), "sz")
    tick_sz = decimal_value(tick_sz, "tick_sz")
    lot_sz = decimal_value(lot_sz, "lot_sz")
    min_sz = decimal_value(min_sz, "min_sz")

    if min(price, quantity, tick_sz, lot_sz, min_sz) <= 0:
        raise ValueError("price, quantity and steps must be positive")

    if price % tick_sz != 0:
        raise ValueError("price is not a multiple of tick_sz")

    if quantity % lot_sz != 0:
        raise ValueError("quantity is not a multiple of lot_sz")

    if quantity < min_sz:
        raise ValueError("quantity is below min_sz")

    return True


def generate_cl_ord_id(existing_ids=None):
    """Generate an alphanumeric client order ID, at most 32 characters."""
    existing_ids = existing_ids if existing_ids is not None else set()
    alphabet = string.ascii_letters + string.digits

    for _ in range(100):
        candidate = "".join(
            secrets.choice(alphabet) for _ in range(24)
        )
        if candidate not in existing_ids:
            existing_ids.add(candidate)
            return candidate

    raise RuntimeError("Unable to generate a unique clOrdId")


def classify_order_response(payload):
    """Classify the initial order response.

    code=0 and sCode=0 means SENT, not confirmed live.
    Nonzero sCode is preserved without interpreting its meaning.
    """
    if not isinstance(payload, dict):
        return {
            "status": "UNKNOWN",
            "reason": "invalid_response",
        }

    if str(payload.get("code")) != "0":
        return {
            "status": "REQUEST_ERROR",
            "code": payload.get("code"),
            "msg": payload.get("msg"),
        }

    data = payload.get("data")
    if not isinstance(data, list) or not data:
        return {
            "status": "UNKNOWN",
            "reason": "missing_order_result",
        }

    result = data[0]
    if not isinstance(result, dict) or "sCode" not in result:
        return {
            "status": "UNKNOWN",
            "reason": "missing_sCode",
        }

    s_code = str(result["sCode"])
    s_msg = result.get("sMsg", "")

    if s_code != "0":
        return {
            "status": "REJECTED",
            "sCode": result["sCode"],
            "sMsg": s_msg,
        }

    return {
        "status": "SENT",
        "sCode": result["sCode"],
        "sMsg": s_msg,
        "ordId": result.get("ordId"),
        "clOrdId": result.get("clOrdId"),
    }


def classify_order_state(state):
    """Recognize only the five documented order states."""
    if state in VALID_ORDER_STATES:
        return state
    return "UNKNOWN"


def has_any_fill(acc_fill_sz):
    """Use cumulative filled quantity, never an individual fill field."""
    quantity = decimal_value(acc_fill_sz, "accFillSz")
    return quantity > 0


def classify_timeout():
    """A timeout does not establish whether the exchange accepted an order."""
    return "UNKNOWN"


def build_demo_headers(extra=None):
    """Build headers that always include the demo-trading marker."""
    headers = dict(extra or {})

    # Prevent callers from overriding the safety marker.
    headers[SIMULATION_HEADER] = "1"
    return headers


def guarded_send(headers, send_callable, *args, **kwargs):
    """Refuse to call a transport unless demo mode is explicitly marked."""
    marker = None

    for key, value in headers.items():
        if key.lower() == SIMULATION_HEADER:
            marker = value
            break

    if marker != "1":
        raise RuntimeError(
            "Missing required simulated-trading header; request blocked"
        )

    return send_callable(*args, **kwargs)


class OrderIdRegistry:
    """Track client IDs within this process."""

    def __init__(self):
        self._ids = set()

    def new_id(self):
        return generate_cl_ord_id(self._ids)

    def register(self, order_id):
        if not isinstance(order_id, str):
            raise ValueError("clOrdId must be a string")

        if not order_id or len(order_id) > 32:
            raise ValueError("clOrdId length must be 1 to 32")

        if not order_id.isalnum():
            raise ValueError("clOrdId must be alphanumeric")

        if order_id in self._ids:
            raise ValueError("duplicate clOrdId")

        self._ids.add(order_id)
        return order_id

