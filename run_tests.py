import sys
import unittest
from pathlib import Path

# 一条命令运行全部测试（CSV 校验器 9 项 + 回测相关全部）
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import csv_quality_check
import test_backtest

loader = unittest.defaultTestLoader
suite = unittest.TestSuite([
    loader.loadTestsFromTestCase(csv_quality_check.TestCSVQuality),
    loader.loadTestsFromModule(test_backtest),
])

result = unittest.TextTestRunner(verbosity=1).run(suite)
passed = result.testsRun - len(result.failures) - len(result.errors)
print()
print("总计：", passed, "/", result.testsRun, "通过")
print("结果：", "全部通过" if result.wasSuccessful() else "存在失败")

