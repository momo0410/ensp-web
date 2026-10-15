"""
state_persist.py - 运行状态持久化
==================================
能力识别结果、实验会话以前只存在内存里，重启即丢（要重新跑一遍
探测）。本模块把它们落盘为 JSON（filelock 防并发写坏），
启动时回灌，让协议角色页 / AI 上下文 / 实验校验开箱即用。

文件（均已加入 .gitignore）：
- capability_cache.json  {ip: 能力识别结果}
- lab_sessions.json      {session_id: 实验会话}
- collection_cache.json  {ip: 采集结果}，按当前激活拓扑隔离
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


def collection_cache_file() -> str:
    """采集结果的落盘路径，按激活拓扑隔离。

    配置跟着拓扑走：A 拓扑采到的设备输出不该污染 B 拓扑的同名设备。
    拿不到激活拓扑时退回全局文件（此时只有一套设备，不会串）。
    """
    try:
        from domain import workspace
        name = workspace.active_topology()
    except Exception as e:  # 工作目录不可用时不该阻断持久化
        logger.warning("取激活拓扑失败，采集缓存退回全局文件: %s", e)
        name = None
    if not name:
        return os.path.join(BASE_DIR, "collection_cache.json")
    return os.path.join(
        BASE_DIR, "workspaces", name, "collection_cache.json"
    )


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


# ---------------------------------------------------------------------------
# 采集结果（协议角色解析的数据源，以前只在内存里，重启即丢）
# ---------------------------------------------------------------------------
# 采集输出很大（current-configuration 动辄几十 KB），整份落盘会让
# collection_cache.json 膨胀到数 MB。因此只保留角色解析真正用到的字段：
# 各命令的 output 原文、info 摘要、ok/online 标志。
_KEEP_COMMANDS = (
    "display current-configuration",
    "display ip interface brief",
    "display ip routing-table",
    "display arp all",
    "display mac-address",
    "display ospf peer",
    "display vlan",
    "display version",
    "display device",
)


def _slim_collection(result: dict) -> dict:
    """
    裁剪单台采集结果，只留协议角色/冲突检测要用的部分。

    **必须保留 commands[key] 的原始 dict 结构**（{success, output, error}）：
    落盘后再回灌的数据要和内存态完全一致，否则 role_detector / conflict_detector
    这些按 `commands[k]["output"]` 取值的地方会静默拿到空值——
    表现为"重启后协议角色全空了"，而界面看不出任何异常。
    """
    if not isinstance(result, dict):
        return {}
    commands = result.get("commands") or {}
    slim_commands = {}
    for key in _KEEP_COMMANDS:
        value = commands.get(key)
        if isinstance(value, dict):
            output = value.get("output")
            if isinstance(output, str):
                slim_commands[key] = {
                    "success": bool(value.get("success", True)),
                    "output": output,
                    "error": value.get("error"),
                }
        elif isinstance(value, str):
            slim_commands[key] = {"success": True, "output": value, "error": None}
    return {
        "ip": result.get("ip"),
        "name": result.get("name"),
        "ok": bool(result.get("ok")),
        "online": bool(result.get("online")),
        "commands": slim_commands,
        "info": result.get("info") or {},
    }


def save_collection_cache(results: dict) -> None:
    """results: {ip: 采集结果}，落盘前裁剪掉无用的大字段。"""
    if not isinstance(results, dict):
        return
    path = collection_cache_file()
    try:
        path_obj_dir = os.path.dirname(path)
        if path_obj_dir:
            os.makedirs(path_obj_dir, exist_ok=True)
    except OSError as e:
        logger.warning("创建采集缓存目录失败: %s", e)
        return
    slim = {ip: _slim_collection(v) for ip, v in results.items() if _slim_collection(v)}
    _write_json(path, slim)
    logger.debug("采集缓存已保存（%d 台）", len(slim))


def load_collection_cache() -> dict:
    path = collection_cache_file()
    if not os.path.exists(path):
        return {}
    return _read_json(path)
