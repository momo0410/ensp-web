"""
网段设备发现：扫描 → 识别 → 同步设备列表。

从管理网段扫描出 Ping+SSH 可达的候选设备，SSH 识别名称后写入 devices.json，
并对异常偏多的结果做保护性拦截（虚拟网卡假阳性）。
"""

import concurrent.futures
import ipaddress
from typing import Dict, List

from fastapi import HTTPException

from app.config import logger
from app.models import DeviceDiscoverRequest
from app.state import state
from domain.device_store import load_devices, save_devices
from domain.netprobe import _ip_sort_key, probe_subnet
from domain.ssh_collect import _default_discovered_name, ssh_identify_device
from domain.lldp import _lldp_neighbor_inventory

def discover_devices_from_subnet(req: DeviceDiscoverRequest) -> dict:
    """扫描管理网段并把在线设备同步为设备列表。"""
    subnet = (req.subnet or state.get("subnet") or "").strip()
    if not subnet:
        raise HTTPException(status_code=400, detail="请先选择管理网卡，或输入要扫描的管理网段")

    try:
        network = ipaddress.IPv4Network(subnet, strict=False)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"无效的网段: {subnet}")

    if network.num_addresses > 4096:
        raise HTTPException(status_code=400, detail="扫描网段过大，请使用 /24 或更小的管理网段")

    local_ip = state["selected_interface"]["ip"] if state.get("selected_interface") else None
    probes = probe_subnet(str(network), exclude_ip=local_ip, max_workers=req.max_workers)
    lldp_inventory = _lldp_neighbor_inventory(req.username, req.password, req.device_type)
    candidates: List[dict] = []
    candidate_results: Dict[str, dict] = {}

    for probe in probes:
        ip = probe["ip"]
        device = {
            "name": lldp_inventory.get(ip, {}).get("name") or _default_discovered_name(ip),
            "ip": ip,
            "username": req.username,
            "password": req.password,
            "device_type": req.device_type,
        }
        candidates.append(device)
        candidate_results[ip] = {
            "ip": ip,
            "name": device["name"],
            "online": True,
            "ssh_ok": False,
            "ssh_error": "SSH 端口已开放，尚未完成登录识别",
            "commands": {},
            "info": {},
            "probe": probe,
        }

    if req.ssh_identify and candidates:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(candidates), 10)) as executor:
            future_map = {executor.submit(ssh_identify_device, d): d for d in candidates}
            for future in concurrent.futures.as_completed(future_map):
                device = future_map[future]
                ip = device["ip"]
                try:
                    identity = future.result()
                    if identity.get("name"):
                        device["name"] = identity["name"]
                    result = candidate_results[ip]
                    result["name"] = device["name"]
                    result["ssh_ok"] = identity.get("ssh_ok", False)
                    result["ssh_error"] = identity.get("ssh_error", "")
                    result["info"] = {
                        "hostname": device["name"] if identity.get("ssh_ok") else "",
                        "model": identity.get("model", ""),
                    }
                except Exception as e:
                    candidate_results[ip]["ssh_error"] = f"识别异常: {e}"

    discovered: List[dict] = []
    discovery_results: Dict[str, dict] = {}
    ignored: List[dict] = []

    for device in candidates:
        ip = device["ip"]
        result = candidate_results[ip]
        probe = result.get("probe", {})
        accepted = bool(probe.get("ping")) and bool(result.get("ssh_ok"))
        if accepted:
            discovered.append(device)
            discovery_results[ip] = result
        else:
            ignored.append({
                "ip": ip,
                "reason": "未同时通过 Ping 和 SSH 登录检测",
                "ssh_error": result.get("ssh_error", ""),
                "probe": probe,
            })

    discovered.sort(key=lambda item: _ip_sort_key(item["ip"]))
    suspicious_limit = 64
    suspicious_scan = len(candidates) > suspicious_limit

    if suspicious_scan:
        logger.warning(
            "自动发现结果异常偏多: 网段 %s 发现 %d 台，已阻止写入设备列表",
            network,
            len(candidates),
        )

    if req.replace and not req.dry_run and not suspicious_scan:
        devices = discovered
        save_devices(devices)
    elif discovered and not req.dry_run and not suspicious_scan:
        devices_by_ip = {d["ip"]: d for d in load_devices()}
        for device in discovered:
            devices_by_ip[device["ip"]] = device
        devices = sorted(devices_by_ip.values(), key=lambda item: _ip_sort_key(item["ip"]))
        save_devices(devices)
    elif discovered:
        devices = discovered
    else:
        devices = load_devices()

    for ip, result in discovery_results.items():
        state["collection_results"][ip] = result

    return {
        "subnet": str(network),
        "online_ips": [probe["ip"] for probe in probes],
        "candidate_count": len(candidates),
        "discovered": discovered,
        "ignored": ignored,
        "devices": devices,
        "discovery_results": discovery_results,
        "replaced": bool(req.replace and not req.dry_run and not suspicious_scan),
        "dry_run": req.dry_run,
        "suspicious": suspicious_scan,
        "message": (
            f"发现结果异常偏多（{len(candidates)} 台），疑似虚拟网卡假阳性，已保留原设备列表"
            if suspicious_scan else ""
        ),
    }
