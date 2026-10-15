"""路由模块（由 main.py 机械拆分，逻辑未改动）。"""
from fastapi import APIRouter

from fastapi import HTTPException
from pydantic import BaseModel
from typing import Optional
import concurrent.futures

from app.config import logger
from app.state import state
from domain.device_store import load_devices
from domain.reachability import port_sort_key
from services.capability_probe import probe_capabilities, probe_capabilities_parallel
from services.config_builder import get_all_templates, get_templates_for_role
from services.help_tree import discover_via_connection
from services.live_check import ping_from_device
from services.role_detector import detect_protocol_roles
from services import state_persist

router = APIRouter()


def _is_effective_probe(cap: dict) -> bool:
    """
    判断一条 capability_results 是否真的探测过、且字段结构可供角色解析。

    空壳条目（设备表写入或缓存回灌时产生）raw_outputs 全空、
    capabilities 也为空，直接拿去做角色解析只会得到一堆"未配置"。

    有些条目（如终端设备 PC）只有 capabilities、raw_outputs 里的值不是
    字符串；这类数据喂给 role_detector 的正则会直接 TypeError，
    因此必须要求"至少一个值是字符串"才算有效。
    """
    raw = cap.get("raw_outputs") or {}
    has_string_output = any(isinstance(v, str) and v.strip() for v in raw.values())
    if has_string_output:
        return True
    # 没有可用原文时，只有带 capabilities 才勉强算探测过；
    # 但若 capabilities 也不是 dict，视为脏数据。
    caps = cap.get("capabilities")
    return isinstance(caps, dict) and bool(caps) and not raw


@router.get("/api/devices/{ip}/capabilities")
async def get_device_capabilities(ip: str):
    """识别指定设备支持的协议和功能。"""
    devices = load_devices()
    device = next((d for d in devices if d["ip"] == ip), None)
    if not device:
        raise HTTPException(status_code=404, detail=f"未找到 IP 为 {ip} 的设备")

    result = probe_capabilities(
        device,
    )
    # Cache in state
    state["capability_results"] = state.get("capability_results", {})
    state["capability_results"][ip] = result
    state_persist.save_capability_cache(state["capability_results"])
    return {"success": True, **result}

@router.post("/api/devices/capabilities-all")
async def probe_all_capabilities():
    """批量识别所有设备能力（设备间并行）。"""
    devices = load_devices()
    if not devices:
        return {"success": True, "results": [], "message": "设备列表为空"}

    results = []

    def _probe_one(device: dict) -> dict:
        # 传入采集缓存，避免重复连接
        coll_result = state["collection_results"].get(device["ip"])
        return probe_capabilities_parallel(
            device,
            collection_result=coll_result,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(devices), 10)) as executor:
        future_map = {executor.submit(_probe_one, d): d for d in devices}
        for future in concurrent.futures.as_completed(future_map):
            device = future_map[future]
            try:
                result = future.result()
                results.append(result)
                state.setdefault("capability_results", {})[device["ip"]] = result
                state_persist.save_capability_cache(state["capability_results"])
            except Exception as e:
                logger.error("能力识别 %s 失败: %s", device["ip"], e)
                results.append({
                    "ip": device["ip"],
                    "hostname": device.get("name", device["ip"]),
                    "device_role": "unknown",
                    "ok": False,
                    "capabilities": {},
                    "raw_outputs": {},
                    "errors": [str(e)],
                })

    results.sort(key=lambda r: port_sort_key(r["ip"]))
    return {"success": True, "results": results}

@router.get("/api/devices/capability-templates")
async def get_capability_templates(device_role: Optional[str] = None):
    """获取可用的配置模板。"""
    if device_role:
        templates = get_templates_for_role(device_role)
    else:
        templates = get_all_templates()
    return {"success": True, "templates": templates}

class DevicePingRequest(BaseModel):
    target: str
    count: int = 4
    source: Optional[str] = None   # 源地址（VRP ping -a），路由实验常用

@router.post("/api/devices/{ip}/device-ping")
async def device_ping(ip: str, req: DevicePingRequest):
    """
    在指定设备上执行 ping（数据面验证）。

    管理面的能力/角色/配置只能说明"配了没有"，学生实验的最终验收
    （如跨网段互通）需要在设备上真实 ping 一发。
    """
    device = next((d for d in load_devices() if d["ip"] == ip), None)
    if not device:
        raise HTTPException(status_code=404, detail=f"未找到 IP 为 {ip} 的设备")
    result = ping_from_device(
        device, req.target, count=req.count, source=req.source,
    )
    return result

@router.get("/api/devices/{ip}/protocol-roles")
async def get_device_protocol_roles(ip: str):
    """
    解析设备在每个协议中扮演的角色（VRRP Master/Backup、STP 根桥、
    DHCP Server/中继、NAT 类型……）。

    纯解析已有能力识别结果，零连接。未识别能力的设备返回 404 提示。
    """
    cap = state.get("capability_results", {}).get(ip)
    if not cap:
        raise HTTPException(status_code=404,
                            detail="暂无该设备的能力识别结果，请先执行能力识别")
    try:
        return {"success": True, **detect_protocol_roles(cap)}
    except Exception as e:
        # 采集结果的字段结构与解析器预期不符时，给一个可读错误，
        # 比把 500 裸抛给前端更容易定位（重新采集通常就能修好）。
        logger.warning("解析 %s 的协议角色失败: %s", ip, e)
        raise HTTPException(
            status_code=500,
            detail=f"解析 {ip} 的协议角色失败（建议重新采集该设备）: {type(e).__name__}: {e}",
        ) from e

@router.post("/api/protocol-roles-all")
async def analyze_protocol_roles_all():
    """批量解析所有已识别设备的协议角色（纯内存解析，零连接）。

    区分三种情况，否则界面会把"从未探测过的设备"渲染成"探测后没有任何角色"，
    用户完全无法判断该不该去点能力识别：

    - probed：有有效采集数据 → 正常给角色结果
    - empty：占位条目（raw_outputs 全空）→ 不算进 count，单独列在 not_probed
    - 设备表里还没进 capability_results 的 → 同样列在 not_probed
    """
    caps = state.get("capability_results", {})
    probed_ips = set()

    results = []
    failed = []
    for cap in caps.values():
        ip = cap.get("ip") or ""
        if not _is_effective_probe(cap):
            continue
        probed_ips.add(ip)
        try:
            results.append(detect_protocol_roles(cap))
        except Exception as e:
            # 一台设备的采集结果触发了解析异常，不该让整批都看不到角色。
            # 记下来并在响应里报出来，其余设备照常返回。
            logger.warning("解析 %s 的协议角色失败: %s", ip, e)
            failed.append({"ip": ip, "error": f"{type(e).__name__}: {e}"})
    results.sort(key=lambda r: port_sort_key(r["ip"]))

    not_probed = [
        {"ip": d.get("ip"), "name": d.get("name") or d.get("ip")}
        for d in load_devices()
        if (d.get("ip") not in probed_ips)
    ]
    return {
        "success": True,
        "results": results,
        "count": len(results),
        "not_probed": not_probed,
        "failed": failed,
    }

@router.get("/api/devices/{ip}/protocol-tree")
async def discover_device_protocol_tree(
    ip: str,
    mode: Optional[str] = None,
    port: Optional[int] = None,
    interface: Optional[str] = None,
):
    """
    零副作用 ? 命令树协议发现。

    采集 display ? / 系统视图 ? / 接口视图 ? 三层关键字，与协议签名比对，
    再结合 eNSP 镜像能力矩阵，给出设备支持的完整协议清单与证据来源。
    通过本地 Console 端口（127.0.0.1:<console_port>）采集。
    """
    if mode == "console":
        if not port:
            raise HTTPException(status_code=400, detail="console 模式需要提供 port 参数")
        from services.help_tree import collect_views_console, compose_discovery
        from services.command_probe import probe_via_console
        try:
            version, views = probe_via_console(
                {"mode": "console", "port": port, "interface": interface})
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Console 连接失败: {e}")
        result = compose_discovery(version, views)
        result.update({"ip": ip, "mode": "console", "console_port": port,
                       "channel_ok": True})
        return {"success": True, **result}

    devices = load_devices()
    device = next((d for d in devices if d["ip"] == ip), None)
    if not device:
        raise HTTPException(status_code=404, detail=f"未找到 IP 为 {ip} 的设备")
    if interface:
        device = dict(device)
        device["probe_interface"] = interface

    result = discover_via_connection(
        device,
    )
    result["mode"] = "ssh"
    return {"success": result.get("ok", False), **result}

# ---------------------------------------------------------------------------
# AI 助教 API（OpenAI 兼容协议，模型服务自行配置）
# ---------------------------------------------------------------------------

