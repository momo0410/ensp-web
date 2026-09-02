"""
smart_defaults.py - 智能默认值生成器
====================================
根据设备管理 IP 自动生成不冲突的配置默认值：
- Router ID = 管理 IP（天然唯一）
- 反掩码从网络地址自动推导
- 子网掩码常用默认值
"""

import ipaddress
import re
import logging
from typing import Dict, Any, Optional, List

logger = logging.getLogger("ensp-web.smart-defaults")


def network_to_wildcard(network_address: str) -> Optional[str]:
    """
    将网络地址转换为反掩码（Wildcard Mask）。

    支持的输入格式：
      - CIDR:    "192.168.1.0/24"       → "0.0.0.255"
      - 纯地址:  "192.168.1.0"          → "0.0.0.255"（按类推断 /24）
      - 带掩码:  "10.0.0.0 255.255.0.0" → "0.0.255.255"

    Args:
        network_address: 网络地址字符串

    Returns:
        反掩码字符串，无法解析时返回 None
    """
    if not network_address or not network_address.strip():
        return None

    network_address = network_address.strip()

    # 尝试 CIDR 格式 (192.168.1.0/24)
    if "/" in network_address:
        try:
            net = ipaddress.ip_network(network_address, strict=False)
            wildcard_int = ~int(net.netmask) & 0xFFFFFFFF
            return str(ipaddress.IPv4Address(wildcard_int))
        except ValueError:
            pass

    # 尝试 "IP 掩码" 格式 (10.0.0.0 255.255.0.0)
    parts = network_address.split()
    if len(parts) == 2:
        try:
            mask = ipaddress.IPv4Address(parts[1])
            wildcard_int = ~int(mask) & 0xFFFFFFFF
            return str(ipaddress.IPv4Address(wildcard_int))
        except ValueError:
            pass

    # 尝试纯 IP 格式，根据类推断掩码
    octets = network_address.split(".")
    if len(octets) == 4:
        try:
            first = int(octets[0])
            if first == 0:
                return None

            # 按类推断
            if first <= 127:
                # A 类: /8
                net = ipaddress.ip_network(f"{network_address}/8", strict=False)
            elif first <= 191:
                # B 类: /16
                net = ipaddress.ip_network(f"{network_address}/16", strict=False)
            else:
                # C 类: /24
                net = ipaddress.ip_network(f"{network_address}/24", strict=False)

            wildcard_int = ~int(net.netmask) & 0xFFFFFFFF
            return str(ipaddress.IPv4Address(wildcard_int))
        except (ValueError, IndexError):
            pass

    return None


def subnet_to_wildcard(subnet_mask: str) -> Optional[str]:
    """
    将子网掩码转换为反掩码。

    Args:
        subnet_mask: 子网掩码，如 "255.255.255.0"

    Returns:
        反掩码字符串，无效掩码返回 None
    """
    if not subnet_mask or not subnet_mask.strip():
        return None

    try:
        mask = ipaddress.IPv4Address(subnet_mask.strip())
        wildcard_int = ~int(mask) & 0xFFFFFFFF
        return str(ipaddress.IPv4Address(wildcard_int))
    except (ValueError, TypeError):
        return None


def wildcard_to_subnet(wildcard: str) -> Optional[str]:
    """
    将反掩码转换为子网掩码。

    Args:
        wildcard: 反掩码，如 "0.0.0.255"

    Returns:
        子网掩码字符串，无效反掩码返回 None
    """
    if not wildcard or not wildcard.strip():
        return None

    try:
        wc = ipaddress.IPv4Address(wildcard.strip())
        mask_int = ~int(wc) & 0xFFFFFFFF
        return str(ipaddress.IPv4Address(mask_int))
    except (ValueError, TypeError):
        return None


def generate_router_id(management_ip: str) -> str:
    """
    根据管理 IP 生成 Router ID。

    直接使用管理 IP 作为 Router ID（业界通用做法，天然唯一）。

    Args:
        management_ip: 设备管理 IP

    Returns:
        Router ID 字符串
    """
    return management_ip


def get_last_octet(ip: str) -> Optional[str]:
    """
    获取 IP 地址的最后一段（末位字节）。

    Args:
        ip: IP 地址

    Returns:
        末位字符串，如 "11"
    """
    if not ip:
        return None
    parts = ip.split(".")
    if len(parts) == 4:
        return parts[3]
    return None


def generate_smart_defaults(
    device_ip: str,
    config_type: str = "",
    existing_interfaces: List[Dict] = None,
) -> Dict[str, Any]:
    """
    为单台设备生成智能默认值。

    Args:
        device_ip: 设备管理 IP
        config_type: 配置类型（可选，如 "ospf"、"bgp"）
        existing_interfaces: 设备已有接口列表（可选）

    Returns:
        默认值字典
    """
    defaults = {
        "router_id": generate_router_id(device_ip),
        "last_octet": get_last_octet(device_ip) or "",
    }

    # OSPF 相关默认值
    if config_type in ("ospf", ""):
        defaults["router_id"] = generate_router_id(device_ip)

    # BGP 相关默认值
    if config_type in ("bgp", ""):
        defaults["router_id"] = generate_router_id(device_ip)

    return defaults


def generate_batch_smart_defaults(
    target_devices: List[str],
    config_type: str = "",
    collection_results: Dict = None,
) -> Dict[str, Dict[str, Any]]:
    """
    为批量设备生成智能默认值。

    Args:
        target_devices: 目标设备 IP 列表
        config_type: 配置类型
        collection_results: 设备采集结果（可选，用于获取接口信息）

    Returns:
        {设备IP: 默认值字典}
    """
    defaults = {}

    for ip in target_devices:
        device_interfaces = []
        if collection_results and ip in collection_results:
            result = collection_results[ip]
            if isinstance(result, dict):
                info = result.get("info", {})
                if isinstance(info, dict):
                    device_interfaces = info.get("interfaces", [])

        defaults[ip] = generate_smart_defaults(
            device_ip=ip,
            config_type=config_type,
            existing_interfaces=device_interfaces,
        )

    return defaults


def infer_wildcard_from_input(text: str) -> Optional[str]:
    """
    从用户输入中推断反掩码。

    智能识别用户输入的格式并返回对应的反掩码。
    支持：CIDR、纯网络地址、带掩码的地址。

    Args:
        text: 用户输入的文本

    Returns:
        反掩码字符串，无法推断返回 None
    """
    if not text:
        return None

    text = text.strip()

    # 如果已经是反掩码格式（0.x.x.x），直接返回
    if text.startswith("0."):
        try:
            parts = [int(x) for x in text.split(".")]
            if len(parts) == 4 and all(0 <= x <= 255 for x in parts):
                return text
        except ValueError:
            pass

    # 尝试作为网络地址解析
    return network_to_wildcard(text)
