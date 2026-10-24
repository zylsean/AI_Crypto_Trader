
import csv
import math
import tempfile
import unittest
from datetime import datetime, timezone
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
DEFAULT_CSV = Path(__file__).parent / "BTC_USDT_1d.csv"


def validate_csv(path, now_ms=None):
    """
    校验 CSV 日线数据。
    返回 (errors, summary)。
    不修改原始文件。
    """
    errors = []
    summary = {
        "rows": 0,
        "valid_rows": 0,
        "duplicates": 0,
        "order_errors": 0,
        "missing_days": 0,
        "incomplete_candles": 0,
    }

    path = Path(path)

    if not path.exists():
        return [f"文件不存在：{path}"], summary

    timestamps = []
    seen_timestamps = set()
    previous_timestamp = None

    try:
        with path.open(
            "r", encoding="utf-8-sig", newline=""
        ) as file:
            reader = csv.DictReader(file)

            if reader.fieldnames != EXPECTED_HEADER:
                return [
                    "CSV 表头错误。",
                    f"期望：{EXPECTED_HEADER}",
                    f"实际：{reader.fieldnames}",
                ], summary

            for line_number, row in enumerate(reader, start=2):
                summary["rows"] += 1
                row_errors = []

                # 检查必需字段
                if any(
                    row.get(field) is None
                    or not row[field].strip()
                    for field in EXPECTED_HEADER
                ):
                    errors.append(
                        f"第 {line_number} 行：存在空字段"
                    )
                    continue

                # 检查时间戳
                try:
                    timestamp = int(row["timestamp"])
                except (ValueError, TypeError):
                    errors.append(
                        f"第 {line_number} 行：时间戳不是整数"
                    )
                    continue

                if timestamp < 0:
                    row_errors.append("时间戳为负数")

                if timestamp % DAY_MS != 0:
                    row_errors.append(
                        "时间戳不是 UTC 日线零点"
                    )

                if timestamp in seen_timestamps:
                    summary["duplicates"] += 1
                    row_errors.append("重复时间戳")
                else:
                    seen_timestamps.add(timestamp)

                if (
                    previous_timestamp is not None
                    and timestamp < previous_timestamp
                ):
                    summary["order_errors"] += 1
                    row_errors.append("时间戳顺序错误")

                previous_timestamp = timestamp
                timestamps.append(timestamp)

                # 检查价格和成交量
                values = {}

                for field in (
                    "open", "high", "low", "close", "volume"
                ):
                    try:
                        value = float(row[field])
                    except (ValueError, TypeError):
                        row_errors.append(
                            f"{field} 不是有效数字"
                        )
                        continue

                    if not math.isfinite(value):
                        row_errors.append(
                            f"{field} 不是有限数字"
                        )
                        continue

                    values[field] = value

                # 只有数值完整时才检查 OHLC
                if all(
                    field in values
                    for field in (
                        "open", "high", "low", "close", "volume"
                    )
                ):
                    o = values["open"]
                    h = values["high"]
                    low = values["low"]
                    c = values["close"]
                    volume = values["volume"]

                    if min(o, h, low, c) <= 0:
                        row_errors.append("价格必须大于 0")

                    if volume < 0:
                        row_errors.append("成交量不能为负数")

                    if h < max(o, low, c):
                        row_errors.append(
                            "最高价低于开盘价、最低价或收盘价"
                        )

                    if low > min(o, h, c):
                        row_errors.append(
                            "最低价高于开盘价、最高价或收盘价"
                        )

                # 检查未完成 K 线
                if now_ms is not None:
                    if timestamp + DAY_MS > now_ms:
                        summary["incomplete_candles"] += 1
                        row_errors.append("日线尚未完整结束")

                if row_errors:
                    for message in row_errors:
                        errors.append(
                            f"第 {line_number} 行：{message}"
                        )
                else:
                    summary["valid_rows"] += 1

    except (OSError, csv.Error) as exc:
        return [f"读取 CSV 失败：{exc}"], summary

    if summary["rows"] == 0:
        errors.append("CSV 没有数据行")
        return errors, summary

    # 仅在时间戳没有重复、没有乱序时统计缺失日线
    if (
        summary["duplicates"] == 0
        and summary["order_errors"] == 0
    ):
        for index in range(1, len(timestamps)):
            difference = timestamps[index] - timestamps[index - 1]

            if difference > DAY_MS and difference % DAY_MS == 0:
                missing = difference // DAY_MS - 1
                summary["missing_days"] += missing

        if summary["missing_days"] > 0:
            errors.append(
                f"缺失日线总数：{summary['missing_days']}"
            )

    return errors, summary


def format_utc_date(timestamp):
    return datetime.fromtimestamp(
        timestamp / 1000,
        tz=timezone.utc,
    ).strftime("%Y-%m-%d")


def inspect_real_csv():
    print("=" * 55)
    print("AI Crypto Trader - BTC 日线数据校验")
    print("=" * 55)
    print("文件：", DEFAULT_CSV.name)

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    errors, summary = validate_csv(DEFAULT_CSV, now_ms=now_ms)

    print("数据行数：", summary["rows"])
    print("完全通过的行数：", summary["valid_rows"])
    print("重复时间戳：", summary["duplicates"])
    print("时间顺序错误：", summary["order_errors"])
    print("缺失日线数量：", summary["missing_days"])
    print("未完成日线：", summary["incomplete_candles"])

    if DEFAULT_CSV.exists():
        try:
            with DEFAULT_CSV.open(
                "r", encoding="utf-8-sig", newline=""
            ) as file:
                reader = csv.DictReader(file)
                first = next(reader, None)
                last = first

                for row in reader:
                    last = row

            if first and last:
                print(
                    "CSV 首行日期：",
                    format_utc_date(int(first["timestamp"])),
                )
                print(
                    "CSV 末行日期：",
                    format_utc_date(int(last["timestamp"])),
                )
        except (OSError, ValueError, TypeError, KeyError):
            print("日期范围读取失败。")

    print("-" * 55)

    if errors:
        print("真实数据检查结果：发现问题")
        print("问题数量：", len(errors))
        print("最多显示前 20 条：")

        for error in errors[:20]:
            print("-", error)

        if len(errors) > 20:
            print("其余问题未显示。")
    else:
        print("真实数据检查结果：PASS")

    print("=" * 55)
    return not errors


# 以下是合成数据测试：不会修改真实行情 CSV
class TestCSVQuality(unittest.TestCase):

    def write_csv(self, rows, header=EXPECTED_HEADER):
        temp = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            suffix=".csv",
            delete=False,
        )
        self.addCleanup(
            lambda: Path(temp.name).unlink(missing_ok=True)
        )

        writer = csv.writer(temp)
        writer.writerow(header)
        writer.writerows(rows)
        temp.close()
        return temp.name

    def valid_row(self, timestamp=DAY_MS):
        return [
            str(timestamp),
            "100",
            "110",
            "90",
            "105",
            "1000",
        ]

    def test_valid_data(self):
        path = self.write_csv([self.valid_row()])
        errors, _ = validate_csv(path, now_ms=10 * DAY_MS)
        self.assertEqual(errors, [])

    def test_duplicate_timestamp(self):
        row = self.valid_row()
        path = self.write_csv([row, row])
        errors, summary = validate_csv(path, now_ms=10 * DAY_MS)
        self.assertGreater(summary["duplicates"], 0)
        self.assertTrue(errors)

    def test_wrong_order(self):
        path = self.write_csv([
            self.valid_row(2 * DAY_MS),
            self.valid_row(DAY_MS),
        ])
        errors, summary = validate_csv(path, now_ms=10 * DAY_MS)
        self.assertGreater(summary["order_errors"], 0)
        self.assertTrue(errors)

    def test_missing_daily_candle(self):
        path = self.write_csv([
            self.valid_row(DAY_MS),
            self.valid_row(3 * DAY_MS),
        ])
        errors, summary = validate_csv(path, now_ms=10 * DAY_MS)
        self.assertEqual(summary["missing_days"], 1)
        self.assertTrue(errors)

    def test_invalid_ohlc(self):
        row = self.valid_row()
        row[2] = "95"
        path = self.write_csv([row])
        errors, _ = validate_csv(path, now_ms=10 * DAY_MS)
        self.assertTrue(errors)

    def test_negative_volume(self):
        row = self.valid_row()
        row[5] = "-1"
        path = self.write_csv([row])
        errors, _ = validate_csv(path, now_ms=10 * DAY_MS)
        self.assertTrue(errors)

    def test_incomplete_candle(self):
        path =self.write_csv([self.valid_row(9      * DAY_MS)])
        errors, summary = validate_csv(
        path,
        now_ms=9 * DAY_MS + 1,
        )
        self.assertEqual(summary["incomplete_candles"], 1)
        self.assertTrue(errors)

    def test_wrong_header(self):
        path = self.write_csv(
            [self.valid_row()],
            header=["date", "open", "high", "low", "close", "volume"],
        )
        errors, _ = validate_csv(path, now_ms=10 * DAY_MS)
        self.assertTrue(errors)

    def test_empty_csv(self):
        path = self.write_csv([])
        errors, _ = validate_csv(path, now_ms=10 * DAY_MS)
        self.assertTrue(errors)


if __name__ == "__main__":
    print("运行合成数据测试...")
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(
            TestCSVQuality
        )
    )

    print()
    print("合成测试结果：", result.testsRun - len(result.failures) - len(result.errors),
          "/", result.testsRun, "通过")

    print()
    inspect_real_csv()
