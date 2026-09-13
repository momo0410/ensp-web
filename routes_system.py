"""路由模块（由 main.py 机械拆分，逻辑未改动）。"""
from fastapi import APIRouter

from fastapi import HTTPException
from typing import List
from typing import Optional
import ipaddress
import psutil
import socket
import subprocess
import threading

from app.config import logger
from app.models import DeviceDiscoverRequest, InterfaceSelect, ScanRequest
from app.state import state
from domain.console import diagnose_ensp_console_ports
from domain.device_store import load_devices
from domain.discovery import discover_devices_from_subnet
from domain.interfaces import _finalize_interfaces, _get_windows_adapter_metadata, _pick_best_ipv4
from domain.netprobe import scan_subnet
from domain.topology import generate_topology

router = APIRouter()

from routes_scripts import _auto_init, init_state  # noqa: E402
@router.get("/api/interfaces")
async def get_interfaces():
    """读取本机 Windows 网卡信息。"""
    interfaces = []
    try:
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        metadata = _get_windows_adapter_metadata()
    except Exception as e:
        logger.error(f"psutil 读取网卡信息失败: {e}")
        # 尝试使用 ipconfig 作为备用方案
        interfaces = _finalize_interfaces(_fallback_get_interfaces())
        if not interfaces:
            raise HTTPException(
                status_code=500,
                detail=f"读取网卡信息失败: {e}。请检查是否有管理员权限，或尝试重启后端。",
            )
        logger.info(f"使用 ipconfig 备用方案读取到 {len(interfaces)} 张网卡")
        recommended = next((i for i in interfaces if i.get("is_recommended")), None)
        return {
            "success": True,
            "interfaces": interfaces,
            "recommended_interface": recommended,
            "fallback": True,
        }

    for name, addr_list in addrs.items():
        meta = metadata.get(name, {})
        info = {
            "name": name,
            "description": meta.get("description", ""),
            "if_index": meta.get("if_index"),
            "ipv4": "",
            "ipv4_addresses": [],
            "netmask": "",
            "mac": "",
            "status": "DOWN",
            "subnet": "",
            "is_ensp": False,
            "is_recommended": False,
            "recommendation_reason": "",
        }

        ipv4_candidates = []
        for addr in addr_list:
            try:
                if addr.family == socket.AF_INET:
                    candidate = {
                        "address": addr.address or "",
                        "netmask": addr.netmask or "",
                        "subnet": "",
                    }
                    if addr.netmask and addr.address:
                        try:
                            network = ipaddress.IPv4Network(
                                f"{addr.address}/{addr.netmask}", strict=False
                            )
                            candidate["subnet"] = str(network)
                        except ValueError:
                            pass
                    if candidate["address"]:
                        ipv4_candidates.append(candidate)
                elif addr.family == getattr(psutil, "AF_LINK", -1) or (
                    hasattr(addr, "family") and addr.family == -1
                ):
                    info["mac"] = addr.address or ""
            except Exception as ex:
                logger.debug(f"解析网卡 {name} 地址信息时异常: {ex}")

        best_ipv4 = _pick_best_ipv4(ipv4_candidates)
        if best_ipv4:
            info["ipv4"] = best_ipv4["address"]
            info["netmask"] = best_ipv4["netmask"]
            info["subnet"] = best_ipv4["subnet"]
            info["ipv4_addresses"] = ipv4_candidates

        # 网卡状态
        try:
            if name in stats:
                info["status"] = "UP" if stats[name].isup else "DOWN"
        except Exception as ex:
            logger.debug(f"读取网卡 {name} 状态时异常: {ex}")

        # 只保留有 IPv4 地址或名称匹配 eNSP 关键词的网卡，过滤无用的
        if info["ipv4"] or info["mac"]:
            interfaces.append(info)

    interfaces = _finalize_interfaces(interfaces)
    recommended = next((i for i in interfaces if i.get("is_recommended")), None)
    logger.info(f"读取到 {len(interfaces)} 张网卡")
    if recommended:
        logger.info(
            "推荐管理网卡: %s (%s), 原因: %s",
            recommended["name"],
            recommended["ipv4"],
            recommended.get("recommendation_reason") or "规则匹配",
        )
    return {"success": True, "interfaces": interfaces, "recommended_interface": recommended}


def _fallback_get_interfaces() -> List[dict]:
    """当 psutil 失败时，使用 ipconfig /all 作为备用方案读取网卡信息。"""
    interfaces = []
    try:
        result = subprocess.run(
            ["ipconfig", "/all"],
            capture_output=True,
            text=True,
            timeout=10,
            encoding="gbk",
            errors="replace",
        )
        output = result.stdout

        current_iface = None
        for line in output.split("\n"):
            line = line.strip()
            # 网卡名称行（不以空格开头且包含适配器）
            if "adapter" in line.lower() or "适配器" in line:
                if current_iface and current_iface.get("ipv4"):
                    interfaces.append(current_iface)
                iface_name = line.rstrip(":").strip()
                current_iface = {
                    "name": iface_name,
                    "description": "",
                    "if_index": None,
                    "ipv4": "",
                    "ipv4_addresses": [],
                    "netmask": "",
                    "mac": "",
                    "status": "UP",
                    "subnet": "",
                    "is_ensp": False,
                    "is_recommended": False,
                    "recommendation_reason": "",
                }
            elif current_iface:
                if "IPv4" in line or "IPv4 地址" in line:
                    parts = line.split(":")
                    if len(parts) >= 2:
                        ip = parts[-1].strip().rstrip("(首选)")
                        current_iface["ipv4"] = ip
                        current_iface["ipv4_addresses"] = [{
                            "address": ip,
                            "netmask": current_iface.get("netmask", ""),
                            "subnet": "",
                        }]
                elif "子网掩码" in line or "Subnet Mask" in line:
                    parts = line.split(":")
                    if len(parts) >= 2:
                        current_iface["netmask"] = parts[-1].strip()
                        for item in current_iface.get("ipv4_addresses", []):
                            item["netmask"] = current_iface["netmask"]
                elif "物理地址" in line or "Physical Address" in line:
                    parts = line.split(":")
                    if len(parts) >= 2:
                        current_iface["mac"] = parts[-1].strip()

        # 别忘了最后一个
        if current_iface and current_iface.get("ipv4"):
            interfaces.append(current_iface)

        # 补充子网信息和 eNSP 判断
        for iface in interfaces:
            if iface["ipv4"] and iface["netmask"]:
                try:
                    network = ipaddress.IPv4Network(
                        f"{iface['ipv4']}/{iface['netmask']}", strict=False
                    )
                    iface["subnet"] = str(network)
                except ValueError:
                    pass
                for item in iface.get("ipv4_addresses", []):
                    if item.get("address") == iface["ipv4"]:
                        item["subnet"] = iface["subnet"]

        logger.info(f"ipconfig 备用方案解析到 {len(interfaces)} 张网卡")
    except Exception as e:
        logger.error(f"ipconfig 备用方案也失败: {e}")

    return interfaces


@router.post("/api/interfaces/select")
async def select_interface(req: InterfaceSelect):
    """选择某张网卡作为 eNSP 管理网卡。"""
    state["selected_interface"] = {
        "name": req.name,
        "ip": req.ip,
        "netmask": req.netmask,
    }
    try:
        network = ipaddress.IPv4Network(f"{req.ip}/{req.netmask}", strict=False)
        state["subnet"] = str(network)
    except ValueError:
        state["subnet"] = None

    logger.info(f"选择管理网卡: {req.name} ({req.ip}), 网段: {state['subnet']}")
    return {
        "success": True,
        "selected": state["selected_interface"],
        "subnet": state["subnet"],
    }


@router.post("/api/scan-by-interface")
async def scan_by_interface():
    """根据已选网卡扫描对应网段。"""
    if not state["subnet"]:
        raise HTTPException(status_code=400, detail="请先选择管理网卡")
    if not state["selected_interface"]:
        raise HTTPException(status_code=400, detail="请先选择管理网卡")

    local_ip = state["selected_interface"]["ip"]
    online_ips = scan_subnet(state["subnet"], exclude_ip=local_ip)
    return {
        "success": True,
        "subnet": state["subnet"],
        "local_ip": local_ip,
        "online_ips": online_ips,
    }


@router.post("/api/scan")
async def scan(req: ScanRequest):
    """扫描指定网段。"""
    state["subnet"] = req.subnet
    local_ip = state["selected_interface"]["ip"] if state["selected_interface"] else None
    online_ips = scan_subnet(req.subnet, exclude_ip=local_ip)
    return {
        "success": True,
        "subnet": req.subnet,
        "online_ips": online_ips,
    }


@router.post("/api/discover-devices")
async def discover_devices(req: Optional[DeviceDiscoverRequest] = None):
    """扫描管理网段，自动发现 eNSP 中实际在线的设备并同步设备列表。"""
    req = req or DeviceDiscoverRequest()
    discovery = discover_devices_from_subnet(req)
    logger.info(
        "自动发现完成: 网段 %s, 在线 %d 台, 同步设备 %d 台",
        discovery["subnet"],
        len(discovery["online_ips"]),
        len(discovery["discovered"]),
    )
    return {"success": True, **discovery}


@router.get("/api/ensp-console-diagnostics")
async def ensp_console_diagnostics():
    """通过本地 eNSP console 端口诊断设备 SSH 配置与 Cloud 桥接状态。"""
    return {"success": True, **diagnose_ensp_console_ports()}


@router.get("/api/topology")
async def get_topology():
    """根据 LLDP 信息返回拓扑关系。"""
    devices = load_devices()
    topology = generate_topology(devices, state["collection_results"])
    return {"success": True, "topology": topology}


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
        "selected_interface": init_state.get("selected_interface"),
        "subnet": init_state.get("subnet"),
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
        "selected_interface": state.get("selected_interface"),
        "subnet": state.get("subnet"),
        "devices": devices,
        "collection_results": state.get("collection_results", {}),
        "capability_results": state.get("capability_results", {}),
    }


# ---------------------------------------------------------------------------
# 启动入口
# ---------------------------------------------------------------------------

