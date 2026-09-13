"""
设备列表持久化（devices.json，带 filelock 并发保护）。
"""

import json
from typing import List

from app.config import DEVICES_FILE, logger


def load_devices() -> List[dict]:
    """从 devices.json 加载设备列表。文件不存在或损坏时自动恢复。"""
    try:
        # 尝试使用 filelock（如未安装则回退到无锁版本）
        _lock = _acquire_lock()
    except ImportError:
        _lock = None

    if _lock:
        with _lock:
            return _do_load_devices()
    return _do_load_devices()


def _do_load_devices() -> List[dict]:
    if not DEVICES_FILE.exists():
        logger.info("devices.json 不存在，初始化为空列表")
        _do_save_devices([])
        return []
    try:
        with open(DEVICES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError("格式错误：期望 JSON 数组")
        return data
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"devices.json 解析失败 ({e})，已重置为空列表")
        _do_save_devices([])
        return []


def save_devices(devices: List[dict]) -> None:
    """保存设备列表到 devices.json（带文件锁）。"""
    try:
        _lock = _acquire_lock()
    except ImportError:
        _lock = None

    if _lock:
        with _lock:
            _do_save_devices(devices)
    else:
        _do_save_devices(devices)


def _do_save_devices(devices: List[dict]) -> None:
    with open(DEVICES_FILE, "w", encoding="utf-8") as f:
        json.dump(devices, f, indent=2, ensure_ascii=False)


def _acquire_lock():
    """尝试获取 filelock 实例。如未安装则抛出 ImportError。"""
    from filelock import FileLock
    lock_path = DEVICES_FILE.with_suffix(".json.lock")
    return FileLock(str(lock_path), timeout=3)
