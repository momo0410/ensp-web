"""pytest 全局配置：把仓库根加入 sys.path，使测试可以 import 项目模块。

同时通过 pytest_plugins 暴露共享样例构造器，避免测试模块互相 import 私有助手。

**工作区隔离（autouse）**：所有测试一律把设备表 / 拓扑 / 应用设置重定向到
临时目录。历史上缺这一层，跑一次 pytest 就把真实工作区的 devices.json 和
.topo 覆盖成测试里造出来的假设备（曾把 5 台设备的拓扑刷成单台 R_TEST）。
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# 测试目录自身也在 sys.path 上（提供 _cap_result 等共享样例）
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path, monkeypatch):
    """把工作目录、应用设置、旧设备表迁移源全部指向临时目录。

    领域层一律通过 `from domain import workspace` 访问，所以替换模块属性即可生效。
    """
    from domain import workspace as ws
    from domain import device_store as ds

    # 建在 tmp_path 的**同级**，不要占用 tmp_path 本身：
    # 有测试会断言自己的 tmp_path 为空目录（如备份失败不落盘）。
    root = tmp_path.parent / (tmp_path.name + "_ws")
    root.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(ws, "get_workspace_root", lambda: root)
    monkeypatch.setattr(ws, "APP_SETTINGS_FILE", tmp_path / "app_settings.json")
    monkeypatch.setattr(ds, "LEGACY_DEVICES_FILE", tmp_path / "legacy_devices.json")
    monkeypatch.setattr(ds, "_migrated", False)

    yield root
