"""路由模块（由 main.py 机械拆分，逻辑未改动）。"""
from fastapi import APIRouter

import threading

from app.config import logger
from app.state import state
from domain.device_store import load_devices

router = APIRouter()

from routes_scripts import _auto_init, init_state  # noqa: E402

# ---------------------------------------------------------------------------
# 用户设置（开关面板）
# ---------------------------------------------------------------------------
from services.app_settings import SettingsError, list_settings, update_settings  # noqa: E402


@router.get("/api/settings")
async def get_settings():
    """列出全部设置项的当前值与 schema（供前端设置面板渲染）。"""
    return list_settings()


@router.post("/api/settings")
async def save_settings(patch: dict):
    """更新设置项；标记 need_restart 的项要重启后端才生效。"""
    try:
        return update_settings(patch or {})
    except SettingsError as e:
        return {"success": False, "error": str(e)}


@router.get("/api/collection-results")
async def get_collection_results():
    """获取所有采集结果。"""
    return {"success": True, "results": state["collection_results"]}


@router.get("/api/capability-results")
async def get_capability_results():
    """获取所有能力识别结果（缓存）。"""
    results = state.get("capability_results", {})
    return {"success": True, "results": results}


# ---------------------------------------------------------------------------
# 设备能力识别 API
# ---------------------------------------------------------------------------

@router.get("/api/health")
async def health_check():
    """健康检查接口。"""
    return {"status": "ok", "version": "3.0.0"}


# ---------------------------------------------------------------------------
# 自由配置工作台 API
# ---------------------------------------------------------------------------

@router.get("/api/init-status")
async def get_init_status():
    """获取初始化状态"""
    return {
        "success": True,
        "initialized": init_state["initialized"],
        "in_progress": init_state["in_progress"],
        "message": init_state["message"],
    }


@router.post("/api/trigger-init")
async def trigger_init():
    """手动触发初始化"""
    if init_state["in_progress"]:
        return {"success": False, "message": "初始化正在进行中"}
    if init_state["initialized"]:
        return {"success": True, "message": "已完成初始化"}

    # 在后台线程执行初始化
    import threading
    thread = threading.Thread(target=_auto_init, daemon=True)
    thread.start()
    return {"success": True, "message": "初始化已启动"}


@router.get("/api/server-data")
async def get_server_data():
    """获取服务器上所有缓存数据（浏览器刷新时使用）"""
    devices = load_devices()
    return {
        "success": True,
        "initialized": init_state["initialized"],
        "devices": devices,
        "collection_results": state.get("collection_results", {}),
        "capability_results": state.get("capability_results", {}),
    }


# ---------------------------------------------------------------------------
# 启动入口
# ---------------------------------------------------------------------------

