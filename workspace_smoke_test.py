
from pathlib import Path
import sys

PROJECT_DIR = Path(__file__).resolve().parent
TEST_FILE = PROJECT_DIR / "workspace_smoke_test.txt"

EXPECTED_TEXT = (
    "AI Crypto Trader workspace test\n"
    "phase1-workspace-v0.1\n"
)


def main():
    print("版本标识：phase1-workspace-v0.1")
    print("Python 版本：", sys.version.split()[0])
    print("脚本位置：", Path(__file__).resolve())
    print("项目目录：", PROJECT_DIR)

    try:
        TEST_FILE.write_text(
            EXPECTED_TEXT,
            encoding="utf-8"
        )
        actual_text = TEST_FILE.read_text(
            encoding="utf-8"
        )
    except Exception as exc:
        print("结果：FAIL")
        print("错误类型：", type(exc).__name__)
        print("错误详情：", str(exc))
        raise

    if actual_text != EXPECTED_TEXT:
        print("结果：FAIL")
        print("预期内容：", repr(EXPECTED_TEXT))
        print("实际内容：", repr(actual_text))
        raise SystemExit(1)

    print("测试文件：", TEST_FILE)
    print("写入并读取：正常")
    print("内容一致性：通过")
    print("结果：PASS")


if __name__ == "__main__":
    main()

