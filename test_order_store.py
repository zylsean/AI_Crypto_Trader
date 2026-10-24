import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from order_store import OrderStore, StoreCorruptError

NAN = float("nan")


class TestOrderStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "run" / "orders.jsonl"

    def test_append_and_load_merges_patches(self):
        store = OrderStore(self.path)
        store.append({"type": "order", "cid": "A1", "state": "INTENT", "size": 1.0})
        store.append({"type": "order", "cid": "A1", "state": "SENT"})
        store.append({"type": "baseline", "base": 0.5})
        store.append({"type": "halt", "reason": "x"})
        orders, baseline, halts = OrderStore(self.path).load()
        self.assertEqual(orders["A1"]["state"], "SENT")
        self.assertEqual(orders["A1"]["size"], 1.0)
        self.assertEqual(baseline["base"], 0.5)
        self.assertEqual(len(halts), 1)

    def test_recovers_after_restart(self):
        OrderStore(self.path).append({"type": "order", "cid": "A1", "state": "LIVE"})
        again = OrderStore(self.path)
        again.append({"type": "order", "cid": "A2", "state": "INTENT"})
        orders, _, _ = again.load()
        self.assertEqual(set(orders), {"A1", "A2"})

    def test_truncated_last_line_is_dropped(self):
        store = OrderStore(self.path)
        store.append({"type": "order", "cid": "A1", "state": "SENT"})
        with open(self.path, "a", encoding="utf-8") as f:
            f.write('{"type": "order", "cid": "A1", "sta')   # 写到一半被杀
        repaired = OrderStore(self.path)
        self.assertGreater(repaired.repaired_bytes, 0)
        orders, _, _ = repaired.load()
        self.assertEqual(orders["A1"]["state"], "SENT")
        repaired.append({"type": "order", "cid": "A1", "state": "UNKNOWN"})
        orders, _, _ = OrderStore(self.path).load()
        self.assertEqual(orders["A1"]["state"], "UNKNOWN")

    def test_corrupt_middle_line_raises(self):
        store = OrderStore(self.path)
        store.append({"type": "order", "cid": "A1", "state": "SENT"})
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("this is not json\n")
        store.append({"type": "order", "cid": "A2", "state": "SENT"})
        with self.assertRaises(StoreCorruptError):
            OrderStore(self.path).load()

    def test_nan_is_rejected(self):
        store = OrderStore(self.path)
        with self.assertRaises(ValueError):
            store.append({"type": "order", "cid": "A1", "size": NAN})
        self.assertEqual(OrderStore(self.path).events(), [])

    def test_each_line_is_valid_json(self):
        store = OrderStore(self.path)
        store.append({"type": "order", "cid": "A1", "reason": "中文 and \"quotes\""})
        for line in self.path.read_text(encoding="utf-8").splitlines():
            json.loads(line)


if __name__ == "__main__":
    unittest.main(verbosity=2)

