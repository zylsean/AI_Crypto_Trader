import json
import os
from pathlib import Path


class StoreCorruptError(Exception):
    """订单记录文件中间有无法解析的行。"""


class OrderStore:
    """
    订单本地存储：只追加的 JSON Lines 文件，每次写入后立即落盘。

    - 事件类型：order（订单快照补丁）、baseline（基础币起始余额）、halt（停止原因）
    - 程序在写入中途被杀，文件末尾可能留下不完整的一行。启动时会把这一行截掉：
      它还没写完整，就不可能已经发出对应的请求（发请求总是在写入完成之后）。
    - 文件中间出现无法解析的行，视为损坏，直接抛错，不猜测。
    - 不允许写入 NaN / 无穷大。
    """

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.repaired_bytes = 0
        self._repair_tail()

    def _repair_tail(self):
        if not self.path.exists():
            return
        data = self.path.read_bytes()
        if not data or data.endswith(b"\n"):
            return
        cut = data.rfind(b"\n") + 1
        self.repaired_bytes = len(data) - cut
        with open(self.path, "r+b") as file:
            file.truncate(cut)
            file.flush()
            os.fsync(file.fileno())

    def append(self, event):
        line = json.dumps(
            event, ensure_ascii=False, sort_keys=True, allow_nan=False
        ) + "\n"
        with open(self.path, "a", encoding="utf-8") as file:
            file.write(line)
            file.flush()
            os.fsync(file.fileno())

    def events(self):
        if not self.path.exists():
            return []
        result = []
        with open(self.path, "r", encoding="utf-8") as file:
            for number, line in enumerate(file, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    result.append(json.loads(line))
                except ValueError as exc:
                    raise StoreCorruptError(
                        f"订单记录第 {number} 行无法解析"
                    ) from exc
        return result

    def load(self):
        """返回 (orders, baseline, halts)。orders 是 {cid: 合并后的订单字典}。"""
        orders = {}
        baseline = None
        halts = []
        for event in self.events():
            kind = event.get("type")
            if kind == "order":
                cid = event["cid"]
                record = orders.setdefault(cid, {"cid": cid})
                record.update(
                    {k: v for k, v in event.items() if k != "type"}
                )
            elif kind == "baseline":
                baseline = event
            elif kind == "halt":
                halts.append(event)
        return orders, baseline, halts

