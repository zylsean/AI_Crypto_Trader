
import csv
import tempfile
import unittest

from decimal import Decimal, InvalidOperation
from pathlib import Path


EXPECTED_HEADER = [
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
]

DAY_MS = 86_400_000


def validate_csv(path, now_ms=None):
    """检查日线 CSV 结构和基本数据质量，返回错误列表。"""
    errors = []
    path = Path(path)

    if not path.is_file():
        return ["文件不存在"]

    try:
        with path.open(
            "r", encoding="utf-8-sig", newline=""
        ) as f:
            reader = csv.DictReader(f)

            if reader.fieldnames != EXPECTED_HEADER:
                return [
                    "表头不正确，应为："
                    + ",".join(EXPECTED_HEADER)
                ]

            rows = list(reader)

    except (OSError, UnicodeError, csv.Error) as exc:
        return [f"文件读取失败：{exc}"]

    if not rows:
        return ["CSV 没有数据行"]

    timestamps = []

    for row_number, row in enumerate(rows, start=2):
        prefix = f"第 {row_number} 行"

        if None in row or any(
            row.get(name) is None
            or row.get(name).strip() == ""
            for name in EXPECTED_HEADER
        ):
            errors.append(f"{prefix}：字段缺失或为空")
            continue

        try:
            timestamp_text = row["timestamp"].strip()
            timestamp = int(timestamp_text)

            if str(timestamp) != timestamp_text:
                raise ValueError

            if timestamp < 0:
                raise ValueError

            if timestamp % DAY_MS != 0:
                raise ValueError

            timestamps.append((row_number, timestamp))

        except (ValueError, OverflowError):
            errors.append(f"{prefix}：时间戳无效")
            continue

        try:
            values = {
                name: Decimal(row[name].strip())
                for name in (
                    "open", "high", "low", "close", "volume"
                )
            }

            if not all(v.is_finite() for v in values.values()):
                raise InvalidOperation

            o = values["open"]
            h = values["high"]
            low = values["low"]
            c = values["close"]
            volume = values["volume"]

            if min(o, h, low, c) <= 0:
                errors.append(f"{prefix}：价格必须为正数")

            if volume < 0:
                errors.append(f"{prefix}：成交量不能为负数")

            if h < max(o, low, c):
                errors.append(f"{prefix}：最高价不符合 OHLC 关系")

            if low > min(o, h, c):
                errors.append(f"{prefix}：最低价不符合 OHLC 关系")

        except (InvalidOperation, ValueError):
            errors.append(f"{prefix}：价格或成交量不是有效数字")

    # 先判断时间戳是否重复、是否严格递增。
    seen = set()
    duplicate_found = False
    order_error_found = False

    for row_number, timestamp in timestamps:
        if timestamp in seen:
            errors.append(f"第 {row_number} 行：重复时间戳")
            duplicate_found = True
        seen.add(timestamp)

    for i in range(1, len(timestamps)):
        row_number, timestamp = timestamps[i]
        previous_timestamp = timestamps[i - 1][1]

        if timestamp < previous_timestamp:
            errors.append(f"第 {row_number} 行：时间顺序错误")
            order_error_found = True

    # 只有没有重复且严格递增时，才判断缺失日线。
    if not duplicate_found and not order_error_found:
        for i in range(1, len(timestamps)):
            row_number, timestamp = timestamps[i]
            previous_timestamp = timestamps[i - 1][1]

            if timestamp - previous_timestamp > DAY_MS:
                missing = (
                    timestamp - previous_timestamp
                ) // DAY_MS - 1

                errors.append(
                    f"第 {row_number} 行：发现 {missing} 根缺失日线"
                )

    # 每根日线必须经过完整的 24 小时才能视为收盘。
    if now_ms is not None:
        for row_number, timestamp in timestamps:
            if timestamp + DAY_MS > now_ms:
                errors.append(
                    f"第 {row_number} 行：K 线尚未完整收盘"
                )

    return errors


class TestCSVQualityCheck(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "test.csv"

        self.rows = [
            {
                "timestamp": "1704067200000",
                "open": "100", "high": "110",
                "low": "95", "close": "105",
                "volume": "1000",
            },
            {
                "timestamp": "1704153600000",
                "open": "105", "high": "115",
                "low": "100", "close": "110",
                "volume": "1200",
            },
            {
                "timestamp": "1704240000000",
                "open": "110", "high": "120",
                "low": "105", "close": "115",
                "volume": "900",
            },
        ]

    def tearDown(self):
        self.temp_dir.cleanup()

    def write_rows(self, rows):
        with self.path.open(
            "w", encoding="utf-8", newline=""
        ) as f:
            writer = csv.DictWriter(
                f, fieldnames=EXPECTED_HEADER
            )
            writer.writeheader()
            writer.writerows(rows)

    def test_valid_data(self):
        self.write_rows(self.rows)
        self.assertEqual(validate_csv(self.path), [])

    def test_duplicate_timestamp(self):
        rows = [dict(r) for r in self.rows]
        rows[1]["timestamp"] = rows[0]["timestamp"]
        self.write_rows(rows)
        errors = validate_csv(self.path)
        self.assertTrue(any("重复时间戳" in e for e in errors))

    def test_wrong_order_without_false_missing(self):
        rows = [self.rows[1], self.rows[0], self.rows[2]]
        self.write_rows(rows)
        errors = validate_csv(self.path)

        self.assertTrue(
            any("时间顺序错误" in e for e in errors)
        )
        self.assertFalse(
            any("缺失日线" in e for e in errors)
        )

    def test_missing_daily_candle(self):
        rows = [self.rows[0], self.rows[2]]
        self.write_rows(rows)
        errors = validate_csv(self.path)
        self.assertTrue(any("缺失日线" in e for e in errors))

    def test_invalid_ohlc(self):
        rows = [dict(r) for r in self.rows]
        rows[0]["high"] = "99"
        self.write_rows(rows)
        errors = validate_csv(self.path)
        self.assertTrue(
            any("最高价不符合" in e for e in errors)
        )

    def test_negative_volume(self):
        rows = [dict(r) for r in self.rows]
        rows[0]["volume"] = "-1"
        self.write_rows(rows)
        errors = validate_csv(self.path)
        self.assertTrue(
            any("成交量不能为负数" in e for e in errors)
        )

    def test_incomplete_candle(self):
        self.write_rows(self.rows)
        errors = validate_csv(
            self.path,
            now_ms=1704067200000 + DAY_MS - 1,
        )
        self.assertTrue(
            any("尚未完整收盘" in e for e in errors)
        )

    def test_wrong_header(self):
        self.path.write_text(
            "date,open,high,low,close,volume\n",
            encoding="utf-8",
        )
        errors = validate_csv(self.path)
        self.assertTrue(
            any("表头不正确" in e for e in errors)
        )

    def test_empty_csv(self):
        with self.path.open(
            "w", encoding="utf-8", newline=""
        ) as f:
            writer = csv.DictWriter(
                f, fieldnames=EXPECTED_HEADER
            )
            writer.writeheader()

        errors = validate_csv(self.path)
        self.assertTrue(any("没有数据行" in e for e in errors))


if __name__ == "__main__":
    print("AI Crypto Trader 数据检查器")
    print("版本：phase1-data-validator-v0.2")
    print("测试数据：临时目录中的人工构造数据")
    print()

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(
        TestCSVQualityCheck
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)

    print()
    print("测试总数：", result.testsRun)
    print("失败数量：", len(result.failures))
    print("错误数量：", len(result.errors))

    if result.wasSuccessful():
        print("结果：PASS")
    else:
        print("结果：FAIL")
        raise SystemExit(1)
