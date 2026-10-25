
"""Local-only tests for AI Crypto Trader order safety.

No HTTP clients, API keys, or exchange requests are used.
"""

import unittest
from decimal import Decimal

from pathlib import Path
import sys

PROJECT_DIR = Path(__file__).resolve().parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from order_safety import (
    OrderIdRegistry,
    build_demo_headers,
    classify_order_response,
    classify_order_state,
    classify_timeout,
    has_any_fill,
    normalize_limit_buy,
    validate_normalized_order,
    guarded_send,
)


class TestOrderSafety(unittest.TestCase):

    def test_limit_buy_rounding(self):
        order = normalize_limit_buy(
            price="100000.09",
            quantity="0.000029999",
            tick_sz="0.1",
            lot_sz="0.00000001",
            min_sz="0.00002",
        )

        self.assertEqual(order["px"], "100000.0")
        self.assertEqual(order["sz"], "0.00002999")

        self.assertTrue(
            validate_normalized_order(
                order,
                tick_sz="0.1",
                lot_sz="0.00000001",
                min_sz="0.00002",
            )
        )

    def test_quantity_below_minimum_is_rejected(self):
        with self.assertRaises(ValueError):
            normalize_limit_buy(
                price="100000",
                quantity="0.00001999",
                tick_sz="0.1",
                lot_sz="0.00000001",
                min_sz="0.00002",
            )

    def test_invalid_price_tick_is_rejected(self):
        order = {
            "px": "100000.05",
            "sz": "0.00002",
        }

        with self.assertRaises(ValueError):
            validate_normalized_order(
                order,
                tick_sz="0.1",
                lot_sz="0.00000001",
                min_sz="0.00002",
            )

    def test_successful_ack_is_sent_not_live(self):
        payload = {
            "code": "0",
            "msg": "",
            "data": [{
                "sCode": "0",
                "sMsg": "",
                "ordId": "12345",
            }],
        }

        result = classify_order_response(payload)

        self.assertEqual(result["status"], "SENT")
        self.assertNotEqual(result["status"], "live")

    def test_nonzero_scode_is_preserved(self):
        payload = {
            "code": "0",
            "data": [{
                "sCode": "51008",
                "sMsg": "Example exchange message",
            }],
        }

        result = classify_order_response(payload)

        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(result["sCode"], "51008")
        self.assertEqual(
            result["sMsg"],
            "Example exchange message",
        )

    def test_missing_scode_is_unknown(self):
        payload = {
            "code": "0",
            "data": [{"ordId": "12345"}],
        }

        self.assertEqual(
            classify_order_response(payload)["status"],
            "UNKNOWN",
        )

    def test_timeout_is_unknown_not_failure(self):
        self.assertEqual(classify_timeout(), "UNKNOWN")

    def test_cumulative_fill_is_used(self):
        # Even if another field were zero, cumulative quantity decides.
        self.assertTrue(has_any_fill("0.00002"))
        self.assertFalse(has_any_fill("0"))

    def test_canceled_order_can_have_fills(self):
        state = classify_order_state("canceled")
        cumulative_fill = "0.00002"

        self.assertEqual(state, "canceled")
        self.assertTrue(has_any_fill(cumulative_fill))

    def test_unknown_order_state_is_not_guessed(self):
        self.assertEqual(
            classify_order_state("some_new_state"),
            "UNKNOWN",
        )

    def test_all_five_known_states(self):
        expected = {
            "live",
            "partially_filled",
            "filled",
            "canceled",
            "mmp_canceled",
        }

        for state in expected:
            with self.subTest(state=state):
                self.assertEqual(classify_order_state(state), state)

    def test_simulation_header_is_always_added(self):
        headers = build_demo_headers({
            "Accept": "application/json",
        })

        self.assertEqual(
            headers["x-simulated-trading"],
            "1",
        )

    def test_missing_simulation_header_blocks_transport(self):
        called = []

        def fake_send():
            called.append(True)
            return "sent"

        with self.assertRaises(RuntimeError):
            guarded_send({}, fake_send)

        self.assertEqual(called, [])

    def test_wrong_simulation_header_blocks_transport(self):
        called = []

        def fake_send():
            called.append(True)

        with self.assertRaises(RuntimeError):
            guarded_send(
                {"x-simulated-trading": "0"},
                fake_send,
            )

        self.assertEqual(called, [])

    def test_valid_simulation_header_allows_fake_transport(self):
        called = []

        def fake_send():
            called.append(True)
            return "fake response"

        result = guarded_send(
            {"x-simulated-trading": "1"},
            fake_send,
        )

        self.assertEqual(result, "fake response")
        self.assertEqual(called, [True])

    def test_client_order_ids_are_unique(self):
        registry = OrderIdRegistry()

        first = registry.new_id()
        second = registry.new_id()

        self.assertNotEqual(first, second)

        for order_id in (first, second):
            self.assertTrue(order_id.isalnum())
            self.assertLessEqual(len(order_id), 32)

    def test_duplicate_client_order_id_is_rejected(self):
        registry = OrderIdRegistry()
        registry.register("DemoOrder123")

        with self.assertRaises(ValueError):
            registry.register("DemoOrder123")

    def test_invalid_client_order_id_is_rejected(self):
        registry = OrderIdRegistry()

        with self.assertRaises(ValueError):
            registry.register("invalid-id-with-dashes")

        with self.assertRaises(ValueError):
            registry.register("A" * 33)


if __name__ == "__main__":
    unittest.main(verbosity=2)

