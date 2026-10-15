"""
应用共享状态。

全部为进程内可变状态（单进程内存存储，不支持多 worker）。
集中在此处定义，确保整个进程只有一份真身——任何模块都必须
从这里 import 同一个对象，不得在别处重新定义。
"""

from typing import Any, Dict

from app.config import logger

# ---------------------------------------------------------------------------
# 主状态字典
# ---------------------------------------------------------------------------
# 全局状态（单进程内存存储，不支持多 worker）
state: Dict[str, Any] = {
    "collection_results": {},    # 采集结果 {ip: {...}}
    "device_status": {},         # 增量采集状态 {ip: {"last_online": bool, "last_collect_ts": float}}
    "clone_previews": {},        # 配置复制预览缓存 {preview_id: {...}}
    "capability_results": {},    # 能力识别结果缓存 {ip: {...}}
}

# 采集结果落盘开关：置 False 可完全退回"只在内存"的旧行为
_PERSIST_COLLECTION = True


def record_collection(ip: str, result: Dict[str, Any], persist: bool = True) -> None:
    """
    记录一台设备的采集结果，并按拓扑落盘。

    所有写入点都必须走这里（不要直接 `state["collection_results"][ip] = ...`），
    否则重启后协议角色页会因为丢数据而退化成一堆空壳卡片。
    """
    state["collection_results"][ip] = result
    if persist and _PERSIST_COLLECTION:
        try:
            from services import state_persist
            state_persist.save_collection_cache(state["collection_results"])
        except Exception as e:  # 落盘失败不能影响采集本身
            logger.warning("采集结果落盘失败 (%s): %s", ip, e)


def drop_collection(ip: str, persist: bool = True) -> None:
    """删除一台设备的采集结果（设备被删/改 IP 时用）。"""
    state["collection_results"].pop(ip, None)
    if persist and _PERSIST_COLLECTION:
        try:
            from services import state_persist
            state_persist.save_collection_cache(state["collection_results"])
        except Exception as e:
            logger.warning("采集结果删除后落盘失败 (%s): %s", ip, e)

# ---------------------------------------------------------------------------
# 启动初始化状态
# ---------------------------------------------------------------------------
init_state = {
    "initialized": False,
    "in_progress": False,
    "message": "",
}

# ---------------------------------------------------------------------------
# AI 实验会话
# ---------------------------------------------------------------------------
# 实验会话（AI 模块与启动逻辑共享）。
# 注意：main.py 启动时用 .update() 就地回灌持久化会话，路由侧用 [key]= 就地写入，
# 二者必须操作同一个 dict 对象——因此全进程只允许此处这一份定义。
_lab_sessions: Dict[str, dict] = {}
