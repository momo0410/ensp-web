"""pytest 全局配置：把仓库根加入 sys.path，使测试可以 import 项目模块。

同时通过 pytest_plugins 暴露共享样例构造器，避免测试模块互相 import 私有助手。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# 测试目录自身也在 sys.path 上（提供 _cap_result 等共享样例）
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
