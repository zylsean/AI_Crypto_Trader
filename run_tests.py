
import sys
import unittest
from pathlib import Path

# 一条命令运行全部测试
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import csv_quality_check
import test_backtest
import test_engine
import test_strategy_sma
import test_sma200_experiment
import test_order_store
import test_risk_guard
import test_secrets_util
import test_order_manager
import test_okx_demo
import test_experiment_plan

loader = unittest.defaultTestLoader
suite = unittest.TestSuite([
    loader.loadTestsFromTestCase(csv_quality_check.TestCSVQuality),
    loader.loadTestsFromModule(test_backtest),
    loader.loadTestsFromModule(test_engine),
    loader.loadTestsFromModule(test_strategy_sma),
    loader.loadTestsFromModule(test_sma200_experiment),
    loader.loadTestsFromModule(test_experiment_plan),

loader.loadTestsFromModule(test_order_store),
    loader.loadTestsFromModule(test_risk_guard),
    loader.loadTestsFromModule(test_secrets_util),
    loader.loadTestsFromModule(test_order_manager),
    loader.loadTestsFromModule(test_okx_demo),
])

result = unittest.TextTestRunner(verbosity=1).run(suite)
passed = result.testsRun - len(result.failures) - len(result.errors)

print()
print("总计：", passed, "/", result.testsRun, "通过")
print("结果：", "全部通过" if result.wasSuccessful() else "存在失败")