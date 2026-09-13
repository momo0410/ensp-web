"""
LLDP 邻居清单与设备同步。

从已采集设备的结果中提取 LLDP 邻居管理地址，
并把 Ping+SSH 均可通过的邻居同步进 devices.json。
"""

import ipaddress
from typing import Dict, List

from app.config import (
    DEFAULT_DEVICE_TYPE,
    DEFAULT_SSH_PASSWORD,
    DEFAULT_SSH_USERNAME,
    logger,
)
from app.state import state
from domain.device_store import load_devices, save_devices
from domain.netprobe import _ip_sort_key, ping_host
from domain.parse import _parse_device_info
from domain.ssh_collect import _default_discovered_name, ssh_identify_device

def _valid_remote_mgmt_ip(ip: str) -> bool:
    try:
        addr = ipaddress.IPv4Address(ip)
        return not (addr.is_loopback or addr.is_link_local or addr.is_multicast)
    except ValueError:
        return False

def _lldp_neighbor_inventory(
    username: str = DEFAULT_SSH_USERNAME,
    password: str = DEFAULT_SSH_PASSWORD,
    device_type: str = DEFAULT_DEVICE_TYPE,
) -> Dict[str, dict]:
    """从已采集设备的 LLDP 信息中提取邻居管理地址。"""
    inventory: Dict[str, dict] = {}

    for source_ip, result in state.get("collection_results", {}).items():
        commands = result.get("commands", {})
        info = result.get("info") or {}

        # 旧结果可能是在 LLDP 详细解析修复前生成的；这里用原始命令重新解析一次。
        if commands:
            parsed_info = _parse_device_info(commands)
            if parsed_info.get("lldp_neighbors"):
                info = parsed_info
                result["info"] = parsed_info

        for neighbor in info.get("lldp_neighbors", []):
            remote_ip = (neighbor.get("remote_mgmt_ip") or "").strip()
            if not remote_ip or not _valid_remote_mgmt_ip(remote_ip):
                continue

            name = neighbor.get("remote_device") or _default_discovered_name(remote_ip)
            inventory[remote_ip] = {
                "name": name,
                "ip": remote_ip,
                "username": username,
                "password": password,
                "device_type": device_type,
                "discovered_by": "lldp",
                "source_ip": source_ip,
            }

    return inventory

def _sync_lldp_devices(
    username: str = DEFAULT_SSH_USERNAME,
    password: str = DEFAULT_SSH_PASSWORD,
    device_type: str = DEFAULT_DEVICE_TYPE,
) -> List[dict]:
    """把 LLDP 中带管理 IP 且 Ping+SSH 通过的邻居同步到 devices.json。"""
    inventory = _lldp_neighbor_inventory(username, password, device_type)
    if not inventory:
        return []

    devices = load_devices()
    existing = {d["ip"]: d for d in devices}
    added: List[dict] = []

    for ip, device in inventory.items():
        if ip in existing:
            if existing[ip].get("name", "").startswith("Host-") and device.get("name"):
                existing[ip]["name"] = device["name"]
            continue

        online = ping_host(ip, timeout=1.5)
        if not online:
            state["collection_results"][ip] = {
                "ip": ip,
                "name": device["name"],
                "online": False,
                "ssh_ok": False,
                "ssh_error": f"通过 LLDP 从 {device.get('source_ip', '未知设备')} 发现，但 Ping 不可达，未写入设备列表",
                "commands": {},
                "info": {"hostname": device["name"]},
            }
            continue

        identity = ssh_identify_device(device)
        if not identity.get("ssh_ok"):
            state["collection_results"][ip] = {
                "ip": ip,
                "name": device["name"],
                "online": True,
                "ssh_ok": False,
                "ssh_error": f"通过 LLDP 发现，但 SSH 登录检测失败，未写入设备列表: {identity.get('ssh_error', '未知错误')}",
                "commands": {},
                "info": {"hostname": device["name"]},
            }
            logger.info("LLDP 发现 %s(%s)，但 SSH 未通过，未写入设备列表", device["name"], ip)
            continue

        new_device = {
            "name": identity.get("name") or device["name"],
            "ip": ip,
            "username": device.get("username", username),
            "password": device.get("password", password),
            "device_type": device.get("device_type", device_type),
        }
        devices.append(new_device)
        existing[ip] = new_device
        added.append(new_device)

        state["collection_results"].setdefault(ip, {
            "ip": ip,
            "name": new_device["name"],
            "online": True,
            "ssh_ok": True,
            "ssh_error": "",
            "commands": {},
            "info": {
                "hostname": new_device["name"],
                "model": identity.get("model", ""),
            },
        })

    if added:
        devices.sort(key=lambda item: _ip_sort_key(item["ip"]))
        save_devices(devices)
        logger.info("通过 LLDP 同步新增设备: %s", ", ".join(f"{d['name']}({d['ip']})" for d in added))

    return added
