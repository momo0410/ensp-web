"""
Windows 网卡枚举与筛选。

负责读取网卡元数据、给网卡打分以自动推荐 eNSP 管理网卡，
以及把同一网卡的多个 IPv4 地址收敛为最适合管理的一个。
"""

import ipaddress
import json
import os
import subprocess
from typing import Dict, List, Optional

from app.config import (
    EXCLUDED_INTERFACE_KEYWORDS,
    EXPECTED_MANAGEMENT_IP,
    logger,
)


def _get_windows_adapter_metadata() -> Dict[str, dict]:
    """读取 Windows 网卡描述信息，失败时返回空字典。"""
    if os.name != "nt":
        return {}

    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "Get-NetAdapter | "
                    "Select-Object Name,InterfaceDescription,ifIndex | "
                    "ConvertTo-Json -Compress"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=5,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0 or not result.stdout.strip():
            return {}

        parsed = json.loads(result.stdout)
        if isinstance(parsed, dict):
            parsed = [parsed]

        metadata = {}
        for item in parsed:
            name = item.get("Name")
            if name:
                metadata[name] = {
                    "description": item.get("InterfaceDescription", "") or "",
                    "if_index": item.get("ifIndex"),
                }
        return metadata
    except Exception as e:
        logger.debug(f"读取 Windows 网卡描述失败: {e}")
        return {}


def _is_link_local_ipv4(ip: str) -> bool:
    try:
        return ipaddress.IPv4Address(ip).is_link_local
    except ValueError:
        return False


def _is_loopback_ipv4(ip: str) -> bool:
    try:
        return ipaddress.IPv4Address(ip).is_loopback
    except ValueError:
        return False


def _pick_best_ipv4(candidates: List[dict]) -> Optional[dict]:
    """同一网卡多个 IPv4 时，优先选择可用于管理网段的地址。"""
    if not candidates:
        return None

    def _score(candidate: dict) -> int:
        ip = candidate.get("address", "")
        score = 0
        if ip == EXPECTED_MANAGEMENT_IP:
            score += 100
        if not _is_link_local_ipv4(ip):
            score += 20
        if not _is_loopback_ipv4(ip):
            score += 20
        try:
            addr = ipaddress.IPv4Address(ip)
            if addr.is_private:
                score += 10
        except ValueError:
            pass
        return score

    return max(candidates, key=_score)


def _rank_interface(info: dict) -> tuple[int, List[str]]:
    """给网卡打分，用于自动推荐 eNSP 管理网卡。"""
    name = info.get("name", "")
    description = info.get("description", "")
    text = f"{name} {description}".lower()
    ip = info.get("ipv4", "")

    score = 0
    reasons: List[str] = []

    if info.get("status") == "UP":
        score += 20
    else:
        score -= 15

    if ip:
        score += 10
        if _is_link_local_ipv4(ip):
            score -= 35
        if _is_loopback_ipv4(ip):
            score -= 60

    if ip == EXPECTED_MANAGEMENT_IP:
        score += 100
        reasons.append(f"匹配默认管理 IP {EXPECTED_MANAGEMENT_IP}")

    if "virtualbox" in text or "host-only" in text:
        score += 80
        reasons.append("VirtualBox Host-Only 网卡")

    if "vmware" in text or "vmnet" in text:
        score += 45
        reasons.append("VMware 虚拟网卡")

    if "npcap" in text or "km-test" in text:
        score += 35
        reasons.append("虚拟/抓包适配器")

    if "loopback" in text and "pseudo-interface" not in text:
        score += 25
        reasons.append("Loopback 网卡")

    if any(kw.lower() in text for kw in EXCLUDED_INTERFACE_KEYWORDS):
        score -= 80
        reasons = []

    if ip and not _is_link_local_ipv4(ip) and not _is_loopback_ipv4(ip):
        try:
            host = ipaddress.IPv4Address(ip)
            if host.is_private and str(host).endswith(".1"):
                score += 8
                reasons.append("私有网段网关地址")
        except ValueError:
            pass

    return score, reasons


def _finalize_interfaces(interfaces: List[dict]) -> List[dict]:
    """补充识别字段并按推荐程度排序。"""
    best_index = None
    best_score = -10_000

    for idx, iface in enumerate(interfaces):
        score, reasons = _rank_interface(iface)
        iface["_score"] = score
        iface["is_ensp"] = score >= 40
        iface["is_recommended"] = False
        iface["recommendation_reason"] = " / ".join(dict.fromkeys(reasons))

        if iface.get("ipv4") and score > best_score:
            best_score = score
            best_index = idx

    if best_index is not None and best_score >= 40:
        interfaces[best_index]["is_recommended"] = True

    interfaces.sort(
        key=lambda iface: (
            not iface.get("is_recommended", False),
            not iface.get("is_ensp", False),
            iface.get("status") != "UP",
            -iface.get("_score", 0),
            iface.get("name", ""),
        )
    )

    for iface in interfaces:
        iface.pop("_score", None)

    return interfaces
