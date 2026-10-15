"""
设备列表持久化（工作目录下当前拓扑的 devices.json，带 filelock 并发保护）。

存储位置由 domain.workspace 决定：每个拓扑目录下各自一份 devices.json。
首次运行时会把项目根目录的旧 devices.json 迁移进默认拓扑，避免丢数据。
"""

import json
from pathlib import Path
from typing import List, Optional

from app.config import DEVICES_FILE as LEGACY_DEVICES_FILE, logger

_DEFAULT_TOPOLOGY = "campus"
_migrated = False


class ConflictError(RuntimeError):
    """设备表在客户端读取之后被其他人改过（乐观锁冲突）。"""


def current_revision() -> int:
    """当前设备表的版本号（每次写入 +1）。用于并发写入的乐观锁校验。"""
    path = _devices_path()
    rev_file = path.with_suffix(".json.rev")
    try:
        return int(rev_file.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return 0


def check_revision(client_rev: Optional[int]) -> None:
    """
    校验客户端持有的版本号是否仍是当前版本。

    client_rev 为 None 表示调用方不参与校验（例如首次导入预设）。
    """
    if client_rev is None:
        return
    cur = current_revision()
    if int(client_rev) != cur:
        raise ConflictError(
            f"设备表已被其他操作修改（你的版本 {client_rev}，当前版本 {cur}）；"
            "请刷新页面后重试，以免覆盖他人的改动"
        )


def _devices_path() -> Path:
    """当前激活拓扑的 devices.json；无拓扑时先建一个默认的。"""
    from domain import workspace

    _ensure_migration()
    name = workspace.active_topology()
    if name is None:
        workspace.create_topology(_DEFAULT_TOPOLOGY)
        name = _DEFAULT_TOPOLOGY
        workspace.set_active(name)
    return workspace.devices_path_for(name)


def _ensure_migration() -> None:
    """把根目录的旧 devices.json 迁进默认拓扑（只做一次）。"""
    global _migrated
    if _migrated:
        return
    _migrated = True
    try:
        from domain import workspace

        root = workspace.get_workspace_root()
        if workspace.list_topologies():
            return  # 已有拓扑，无需迁移
        legacy = LEGACY_DEVICES_FILE
        if not legacy.exists():
            return
        data = json.loads(legacy.read_text(encoding="utf-8"))
        if not isinstance(data, list) or not data:
            return
        workspace.create_topology(_DEFAULT_TOPOLOGY)
        workspace.save_topology(_DEFAULT_TOPOLOGY, data, links=[])
        workspace.set_active(_DEFAULT_TOPOLOGY)
        legacy.rename(legacy.with_suffix(".json.migrated"))
        logger.info("已将旧 devices.json 迁移到拓扑 %s", _DEFAULT_TOPOLOGY)
    except Exception as e:  # 迁移失败不应阻断启动
        logger.warning("迁移旧 devices.json 失败: %s", e)


def load_devices() -> List[dict]:
    """加载当前拓扑的设备列表。文件不存在或损坏时自动恢复。"""
    path = _devices_path()
    try:
        _lock = _acquire_lock(path)
    except ImportError:
        _lock = None

    if _lock:
        with _lock:
            return _do_load_devices(path)
    return _do_load_devices(path)


def _do_load_devices(path: Path) -> List[dict]:
    if not path.exists():
        logger.info("%s 不存在，初始化为空列表", path.name)
        _do_save_devices(path, [])
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError("格式错误：期望 JSON 数组")
        return data
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("%s 解析失败 (%s)，已重置为空列表", path.name, e)
        _do_save_devices(path, [])
        return []


def save_devices(devices: List[dict]) -> None:
    """保存设备列表到当前拓扑的 devices.json（带文件锁）。"""
    path = _devices_path()
    try:
        _lock = _acquire_lock(path)
    except ImportError:
        _lock = None

    if _lock:
        with _lock:
            _do_save_devices(path, devices)
    else:
        _do_save_devices(path, devices)


def _do_save_devices(path: Path, devices: List[dict]) -> None:
    """先写临时文件再替换，避免写到一半崩溃导致文件损坏；同时递增版本号。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(devices, f, indent=2, ensure_ascii=False)
    tmp.replace(path)
    # 递增乐观锁版本号，供 check_revision 比对
    rev_file = path.with_suffix(".json.rev")
    try:
        cur = int(rev_file.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        cur = 0
    rev_file.write_text(str(cur + 1), encoding="utf-8")


def _acquire_lock(path: Path):
    """尝试获取 filelock 实例。如未安装则抛出 ImportError。"""
    from filelock import FileLock
    return FileLock(str(path) + ".lock", timeout=3)
