"""路由模块（由 main.py 机械拆分，逻辑未改动）。"""
from fastapi import APIRouter

from core import *  # noqa: F401,F403

router = APIRouter()

@router.get("/api/devices/{ip}/capabilities")
async def get_device_capabilities(ip: str):
    """识别指定设备支持的协议和功能。"""
    devices = load_devices()
    device = next((d for d in devices if d["ip"] == ip), None)
    if not device:
        raise HTTPException(status_code=404, detail=f"未找到 IP 为 {ip} 的设备")

    source_ip = _management_source_ip_for(ip)
    result = probe_capabilities(
        device,
        source_ip=source_ip,
        management_socket_factory=_create_management_socket,
    )
    # Cache in state
    state["capability_results"] = state.get("capability_results", {})
    state["capability_results"][ip] = result
    state_persist.save_capability_cache(state["capability_results"])
    return {"success": True, **result}


@router.post("/api/devices/capabilities-all")
async def probe_all_capabilities():
    """批量识别所有设备能力（并行：3个SSH会话/台，设备间也并行）。"""
    devices = load_devices()
    if not devices:
        return {"success": True, "results": [], "message": "设备列表为空"}

    results = []

    def _probe_one(device: dict) -> dict:
        src = _management_source_ip_for(device["ip"])
        # 传入采集缓存，避免重复 SSH
        coll_result = state["collection_results"].get(device["ip"])
        return probe_capabilities_parallel(
            device,
            source_ip=src,
            management_socket_factory=_create_management_socket,
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
                    "ssh_status": False,
                    "capabilities": {},
                    "raw_outputs": {},
                    "errors": [str(e)],
                })

    results.sort(key=lambda r: _ip_sort_key(r["ip"]))
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
        source_bind=_management_source_ip_for(ip),
        management_socket_factory=_create_management_socket,
    )
    return result


@router.get("/api/devices/{ip}/protocol-roles")
async def get_device_protocol_roles(ip: str):
    """
    解析设备在每个协议中扮演的角色（VRRP Master/Backup、STP 根桥、
    DHCP Server/中继、NAT 类型……）。

    纯解析已有能力识别结果，零 SSH。未识别能力的设备返回 404 提示。
    """
    cap = state.get("capability_results", {}).get(ip)
    if not cap:
        raise HTTPException(status_code=404,
                            detail="暂无该设备的能力识别结果，请先执行能力识别")
    return {"success": True, **detect_protocol_roles(cap)}


@router.post("/api/protocol-roles-all")
async def analyze_protocol_roles_all():
    """批量解析所有已识别设备的协议角色（纯内存解析，零 SSH）。"""
    caps = state.get("capability_results", {})
    results = [detect_protocol_roles(cap) for cap in caps.values()]
    results.sort(key=lambda r: _ip_sort_key(r["ip"]))
    return {"success": True, "results": results, "count": len(results)}


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
    mode=console 时走本地 Console 端口（零 IP 设备），默认 SSH。
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

    source_ip = _management_source_ip_for(ip)
    result = discover_via_ssh(
        device,
        source_ip=source_ip,
        management_socket_factory=_create_management_socket,
    )
    result["mode"] = "ssh"
    return {"success": result.get("ssh_status", False), **result}


# ---------------------------------------------------------------------------
# AI 助教 API（OpenAI 兼容协议，模型服务自行配置）
# ---------------------------------------------------------------------------

