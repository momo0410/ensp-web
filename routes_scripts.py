"""路由模块（由 main.py 机械拆分，逻辑未改动）。"""
from fastapi import APIRouter

from pydantic import BaseModel
from typing import Any
from typing import Dict
from typing import List
import concurrent.futures

from app.config import logger
from app.state import record_collection, state
from domain.device_store import load_devices
from domain.reachability import ping_host
from domain.collect import collect_device
from services.capability_probe import probe_capabilities_parallel
from services.script_service import delete_script, get_script, save_script
from services import state_persist
from services.task_history_service import get_task, list_tasks

router = APIRouter()

# 能力识别结果的有效期。eNSP 镜像的支持集合不会变，真正会变的是
# configured/running 两态；启动时复用较新的缓存能省掉一次全量探测
# （15 台实测 ~100s，期间前台请求被拖到十几秒）。
_PROBE_CACHE_TTL_SECONDS = 30 * 60


def _is_probe_fresh(cap: Any, now: float = None) -> bool:
    """判断一条缓存的能力识别结果是否还新到可以复用。"""
    if not isinstance(cap, dict) or not cap.get("ok"):
        return False
    raw = cap.get("raw_outputs") or {}
    if not any(isinstance(v, str) and v.strip() for v in raw.values()):
        return False
    import time as _time
    ts = cap.get("probed_at") or cap.get("generated_at")
    if not ts:
        return False
    try:
        age = (now or _time.time()) - float(ts)
    except (TypeError, ValueError):
        return False
    return 0 <= age <= _PROBE_CACHE_TTL_SECONDS


class ScriptSaveRequest(BaseModel):
    script_name: str
    description: str = ""
    command_template: Any = []
    global_vars: Dict[str, Any] = {}
    sample_device_vars: Dict[str, Any] = {}
    verify_commands: List[str] = []


@router.post("/api/scripts")
async def api_save_script(req: ScriptSaveRequest):
    """保存脚本。"""
    data = {
        "script_name": req.script_name,
        "description": req.description,
        "command_template": req.command_template,
        "global_vars": req.global_vars,
        "sample_device_vars": req.sample_device_vars,
        "verify_commands": req.verify_commands,
    }
    result = save_script(data)
    return result


@router.get("/api/scripts/{name}")
async def api_get_script(name: str):
    """读取脚本。"""
    script = get_script(name)
    if not script:
        return {"success": False, "message": f"脚本 '{name}' 不存在"}
    return {"success": True, "script": script}


@router.delete("/api/scripts/{name}")
async def api_delete_script(name: str):
    """删除脚本。"""
    return delete_script(name)


@router.get("/api/tasks")
async def api_list_tasks(limit: int = 50):
    """获取历史任务列表。"""
    tasks = list_tasks(limit=limit)
    return {"success": True, "tasks": tasks}


@router.get("/api/tasks/{task_id}")
async def api_get_task(task_id: str):
    """获取任务详情。"""
    task = get_task(task_id)
    if not task:
        return {"success": False, "message": f"任务 '{task_id}' 不存在"}
    return {"success": True, "task": task}


# ---------------------------------------------------------------------------
# 服务器启动自动初始化
# ---------------------------------------------------------------------------

# 初始化状态
init_state = {
    "initialized": False,
    "in_progress": False,
    "message": "",
}


def _auto_init():
    """服务器启动时自动执行：就绪检查 → 并行采集已配置设备 → 能力识别。

    设备来源是 devices.json（由添加设备 / 导入拓扑预设写入），
    不再扫描网段。
    """
    global init_state
    if init_state["initialized"] or init_state["in_progress"]:
        return

    init_state["in_progress"] = True
    init_state["message"] = "正在检查已配置设备..."

    try:
        devices = load_devices()
        if not devices:
            init_state["initialized"] = True
            init_state["message"] = "尚未添加设备，请先添加设备或导入拓扑预设"
            return

        # 1. 并行采集
        init_state["message"] = f"正在采集 {len(devices)} 台设备信息..."

        def _collect_one(device):
            ip = device["ip"]
            if ping_host(ip):
                return collect_device(device, fast_mode=True)
            return {"ip": ip, "name": device.get("name", ip), "online": False,
                    "ok": False, "error": "Console 端口不可达", "commands": {}, "info": {}}

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(devices), 10)) as executor:
            future_map = {executor.submit(_collect_one, d): d for d in devices}
            for future in concurrent.futures.as_completed(future_map):
                try:
                    result = future.result()
                    record_collection(result["ip"], result)
                except Exception as e:
                    device = future_map[future]
                    logger.error("采集 %s 失败: %s", device["ip"], e)

        # 2. 并行能力识别（传入采集缓存，命中时零连接）
        init_state["message"] = "正在并行识别设备能力..."

        def _probe_one(device):
            ip = device["ip"]
            coll_result = state["collection_results"].get(ip)
            try:
                return probe_capabilities_parallel(device, collection_result=coll_result)
            except Exception as e:
                logger.error("能力识别 %s 失败: %s", ip, e)
                return {"ip": ip, "device_name": device.get("name", ip),
                        "ok": False, "capabilities": {}, "errors": [str(e)]}

        # 缓存里已有的设备不必重新探测：启动时它们已经有角色数据，
        # 重探一遍只是把首次启动拖长（实测 15 台全探要 ~100s，期间请求被拖到 17s）。
        cached_caps = state_persist.load_capability_cache()
        fresh_targets = [d for d in devices
                         if d.get("ip") and not _is_probe_fresh(cached_caps.get(d["ip"]))]
        reused = len(devices) - len(fresh_targets)
        state["capability_results"] = dict(cached_caps)
        if reused:
            logger.info("复用 %d 台设备的能力识别缓存，实际探测 %d 台", reused, len(fresh_targets))

        if fresh_targets:
            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(fresh_targets), 10)) as executor:
                future_map = {executor.submit(_probe_one, d): d for d in fresh_targets}
                for future in concurrent.futures.as_completed(future_map):
                    try:
                        cap_result = future.result()
                        state["capability_results"][cap_result["ip"]] = cap_result
                    except Exception as e:
                        device = future_map[future]
                        logger.error("能力识别 %s 失败: %s", device["ip"], e)

            # 全部探测完再落盘一次：早先每完成一台就写一遍全量 JSON，
            # 15 台就是 15 次全量写入，把事件循环堵得更久。
            try:
                state_persist.save_capability_cache(state["capability_results"])
            except Exception as e:
                logger.warning("能力缓存落盘失败: %s", e)

        init_state["initialized"] = True
        init_state["message"] = f"初始化完成: {len(devices)} 台设备"
        logger.info("自动初始化完成")

    except Exception as e:
        init_state["initialized"] = True
        init_state["message"] = f"初始化失败: {e}"
        logger.error("自动初始化失败: %s", e)
    finally:
        init_state["in_progress"] = False


