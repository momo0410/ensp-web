"""
state_persist.py - 运行状态持久化
==================================
能力识别结果、实验会话以前只存在内存里，重启即丢（要重新跑一遍
SSH 探测）。本模块把它们落盘为 JSON（filelock 防并发写坏），
启动时回灌，让协议角色页 / AI 上下文 / 实验校验开箱即用。

文件（均已加入 .gitignore）：
- capability_cache.json  {ip: 能力识别结果}
- lab_sessions.json      {session_id: 实验会话}
"""

import json
import logging
import os
from filelock import FileLock

logger = logging.getLogger("ensp-web.persist")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAPABILITY_CACHE_FILE = os.path.join(BASE_DIR, "capability_cache.json")
LAB_SESSIONS_FILE = os.path.join(BASE_DIR, "lab_sessions.json")
_LOCK_TIMEOUT = 5


def _read_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("读取 %s 失败（将重建）: %s", path, e)
        return {}


def _write_json(path: str, data: dict) -> None:
    lock = FileLock(path + ".lock")
    try:
        with lock.acquire(timeout=_LOCK_TIMEOUT):
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
    except Exception as e:
        logger.warning("写入 %s 失败: %s", path, e)


# ---------------------------------------------------------------------------
# 能力识别结果
# ---------------------------------------------------------------------------
def save_capability_cache(results: dict) -> None:
    """results: {ip: capability_result}，只保留每台设备最新一份。"""
    if not isinstance(results, dict):
        return
    _write_json(CAPABILITY_CACHE_FILE, results)
    logger.debug("能力缓存已保存（%d 台）", len(results))


def load_capability_cache() -> dict:
    return _read_json(CAPABILITY_CACHE_FILE)


# ---------------------------------------------------------------------------
# 实验会话
# ---------------------------------------------------------------------------
def save_lab_sessions(sessions: dict) -> None:
    _write_json(LAB_SESSIONS_FILE, sessions)


def load_lab_sessions() -> dict:
    return _read_json(LAB_SESSIONS_FILE)
