"""
conflict_detector.py - 配置冲突检测器
=====================================
检测配置命令与现有配置的冲突，以及危险命令拦截。
"""

import re
import logging
from typing import List, Dict, Any, Tuple

logger = logging.getLogger("ensp-web.conflict")

# ---------------------------------------------------------------------------
# 危险命令列表（禁止执行）
# ---------------------------------------------------------------------------
DANGEROUS_COMMANDS = [
    {"pattern": r"^reboot\b", "reason": "设备重启命令"},
    {"pattern": r"^reset\s+saved-configuration", "reason": "清除保存的配置"},
    {"pattern": r"^delete\s+", "reason": "删除文件命令"},
    {"pattern": r"^format\s+", "reason": "格式化存储命令"},
    {"pattern": r"^startup\s+saved-configuration", "reason": "修改启动配置"},
    {"pattern": r"^undo\s+ssh\s+server\s+enable", "reason": "禁用 SSH 服务"},
    {"pattern": r"^undo\s+stelnet\s+server\s+enable", "reason": "禁用 STelnet 服务"},
    {"pattern": r"^undo\s+local-user", "reason": "删除本地用户"},
    {"pattern": r"^undo\s+interface\s+", "reason": "删除接口"},
    {"pattern": r"^undo\s+ip\s+route-static\s+all", "reason": "删除所有静态路由"},
    {"pattern": r"^undo\s+acl\s+all", "reason": "删除所有 ACL"},
    {"pattern": r"^undo\s+firewall\s+", "reason": "关闭防火墙"},
]


def check_dangerous_commands(commands: List[str]) -> Tuple[bool, List[dict]]:
    """
    检测命令列表中是否包含危险命令。

    Args:
        commands: 命令列表

    Returns:
        (is_safe, dangerous_list)
        - is_safe: True 表示安全，False 表示有危险命令
        - dangerous_list: 危险命令详情列表
    """
    dangerous_found = []

    for cmd in commands:
        cmd_stripped = cmd.strip()
        if not cmd_stripped:
            continue

        for danger in DANGEROUS_COMMANDS:
            if re.match(danger["pattern"], cmd_stripped, re.IGNORECASE):
                dangerous_found.append({
                    "command": cmd_stripped,
                    "reason": danger["reason"],
                })

    return len(dangerous_found) == 0, dangerous_found


def extract_ip_from_command(cmd: str) -> str:
    """从命令中提取 IP 地址。"""
    match = re.search(r'\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b', cmd)
    return match.group(1) if match else ""


def extract_interface_from_command(cmd: str) -> str:
    """从命令中提取完整接口名（如 GE0/0/1、GigabitEthernet0/0/1、Vlanif10）。

    注意必须返回**完整**接口名：`check_interface_has_ip` 是按接口名整体比对
    配置段的，只取前缀（早先的写法会把 GigabitEthernet0/0/1 截成
    GigabitEthernet）会让"接口已有 IP"的检查永远匹配不上、静默失效。
    """
    match = re.search(r"\binterface\s+([A-Za-z][\w\-/.:]*)", cmd, re.IGNORECASE)
    return match.group(1) if match else ""


def extract_vlan_id(cmd: str) -> str:
    """从命令中提取 VLAN ID。"""
    match = re.search(r'vlan\s+(\d+)', cmd, re.IGNORECASE)
    return match.group(1) if match else ""


def check_ip_conflict(
    target_ip: str,
    new_ip: str,
    collection_results: dict,
) -> Tuple[bool, str]:
    """
    检查新 IP 是否与现有接口 IP 冲突。

    Args:
        target_ip: 目标设备 IP
        new_ip: 要配置的新 IP
        collection_results: 设备采集结果

    Returns:
        (has_conflict, message)
    """
    if not new_ip:
        return False, ""

    result = collection_results.get(target_ip, {})
    info = result.get("info", {})
    interfaces = info.get("interfaces", [])

    for iface_line in interfaces:
        # 解析接口 IP（格式：Interface IP-Address Physical Protocol）
        parts = iface_line.split()
        if len(parts) >= 2:
            existing_ip = parts[1]
            if existing_ip == new_ip and existing_ip != "---":
                return True, f"IP {new_ip} 已被接口 {' '.join(parts[:1])} 使用"

    return False, ""


def check_interface_has_ip(
    target_ip: str,
    interface_name: str,
    collection_results: dict,
) -> Tuple[bool, str]:
    """
    检查指定接口是否已有 IP 配置。

    Args:
        target_ip: 目标设备 IP
        interface_name: 接口名
        collection_results: 设备采集结果

    Returns:
        (has_ip, current_ip)
    """
    if not interface_name:
        return False, ""

    result = collection_results.get(target_ip, {})
    info = result.get("info", {})
    config_sections = info.get("config_sections", {})
    config_output = result.get("commands", {}).get("display current-configuration", {}).get("output", "")

    if not config_output:
        return False, ""

    # 在配置中查找接口段
    in_interface = False
    interface_normalized = interface_name.lower().replace(" ", "")

    for line in config_output.split("\n"):
        line_stripped = line.strip()

        # 检测接口配置开始
        if line_stripped.lower().startswith("interface "):
            iface = line_stripped.split()[1] if len(line_stripped.split()) > 1 else ""
            if iface.lower().replace(" ", "") == interface_normalized:
                in_interface = True
                continue

        # 检测接口配置结束
        if in_interface and (line_stripped == "#" or line_stripped.lower().startswith("interface ")):
            break

        # 查找 IP 配置
        if in_interface and line_stripped.lower().startswith("ip address "):
            parts = line_stripped.split()
            if len(parts) >= 3:
                current_ip = parts[2]
                return True, current_ip

    return False, ""


def check_conflicts(
    targets: List[str],
    template: List[str],
    variables: Dict[str, Dict[str, Any]],
    collection_results: dict,
) -> Dict[str, Any]:
    """
    综合冲突检测。

    Args:
        targets: 目标设备 IP 列表
        template: 配置模板命令
        variables: 每台设备的变量
        collection_results: 设备采集结果

    Returns:
        {
            "success": bool,  # True 表示无冲突
            "errors": [...],   # 严重错误，阻止执行
            "warnings": [...], # 警告信息，可选择继续
            "suggestions": [...], # 建议信息
        }
    """
    result = {
        "success": True,
        "errors": [],
        "warnings": [],
        "suggestions": [],
    }

    # 1. 检测危险命令
    is_safe, dangerous = check_dangerous_commands(template)
    if not is_safe:
        result["success"] = False
        for d in dangerous:
            result["errors"].append({
                "type": "dangerous_command",
                "command": d["command"],
                "message": f"危险命令: {d['command']} - {d['reason']}",
            })

    # 2. 渲染模板并检测每台设备的冲突
    from .free_config_renderer import render_commands, merge_variables

    for target_ip in targets:
        device_vars = variables.get(target_ip, {})
        rendered, undefined = render_commands(template, {}, device_vars)

        if undefined:
            result["success"] = False
            result["errors"].append({
                "type": "undefined_variable",
                "device": target_ip,
                "message": f"设备 {target_ip} 缺少变量: {', '.join(undefined)}",
            })
            continue

        # 检测每条命令的冲突
        for cmd in rendered:
            cmd_lower = cmd.strip().lower()

            # 检测 IP 地址配置冲突
            if cmd_lower.startswith("ip address "):
                new_ip = extract_ip_from_command(cmd)
                if new_ip:
                    has_conflict, msg = check_ip_conflict(target_ip, new_ip, collection_results)
                    if has_conflict:
                        result["warnings"].append({
                            "type": "ip_conflict",
                            "device": target_ip,
                            "command": cmd,
                            "message": f"设备 {target_ip}: {msg}",
                        })

            # 检测接口是否已有 IP
            if cmd_lower.startswith("interface "):
                iface = extract_interface_from_command(cmd)
                if iface:
                    has_ip, current_ip = check_interface_has_ip(target_ip, iface, collection_results)
                    if has_ip:
                        result["suggestions"].append({
                            "type": "interface_has_ip",
                            "device": target_ip,
                            "interface": iface,
                            "current_ip": current_ip,
                            "message": f"设备 {target_ip} 接口 {iface} 已有 IP {current_ip}，新配置将覆盖",
                        })

            # 检测 VLAN 冲突
            if cmd_lower.startswith("vlan "):
                vlan_id = extract_vlan_id(cmd)
                if vlan_id:
                    result["suggestions"].append({
                        "type": "vlan_check",
                        "device": target_ip,
                        "vlan_id": vlan_id,
                        "message": f"设备 {target_ip}: 请确认 VLAN {vlan_id} 是否已存在",
                    })

    return result


# ---------------------------------------------------------------------------
# 跨设备批量冲突检测
# ---------------------------------------------------------------------------

def _extract_router_ids(rendered_commands: Dict[str, List[str]]) -> Dict[str, Dict[str, str]]:
    """
    从渲染后的命令中提取所有 Router ID。

    Returns:
        {ip: {"ospf": "1.1.1.1", "bgp": "2.2.2.2"}}
    """
    router_id_pattern = re.compile(r'router-id\s+(\S+)', re.IGNORECASE)
    result = {}

    for ip, commands in rendered_commands.items():
        ids = {}
        current_context = None

        for cmd in commands:
            cmd_stripped = cmd.strip()
            # 检测上下文（ospf/bgp/isis 进程）
            ctx_match = re.match(r'^(ospf|bgp|isis)\s+', cmd_stripped, re.IGNORECASE)
            if ctx_match:
                current_context = ctx_match.group(1).lower()

            # 提取 router-id
            rid_match = router_id_pattern.search(cmd_stripped)
            if rid_match:
                ctx = current_context or "unknown"
                ids[ctx] = rid_match.group(1)

        if ids:
            result[ip] = ids

    return result


def _extract_configured_ips(rendered_commands: Dict[str, List[str]]) -> Dict[str, List[Dict[str, str]]]:
    """
    从渲染后的命令中提取所有配置的 IP 地址。

    Returns:
        {ip: [{"address": "192.168.1.1", "mask": "255.255.255.0", "interface": "GE0/0/0"}]}
    """
    ip_pattern = re.compile(r'ip\s+address\s+(\S+)\s+(\S+)', re.IGNORECASE)
    iface_pattern = re.compile(r'^interface\s+(\S+)', re.IGNORECASE)
    result = {}

    for device_ip, commands in rendered_commands.items():
        ips = []
        current_iface = ""

        for cmd in commands:
            cmd_stripped = cmd.strip()

            iface_match = iface_pattern.match(cmd_stripped)
            if iface_match:
                current_iface = iface_match.group(1)
                continue

            ip_match = ip_pattern.search(cmd_stripped)
            if ip_match:
                ips.append({
                    "address": ip_match.group(1),
                    "mask": ip_match.group(2),
                    "interface": current_iface,
                })

        if ips:
            result[device_ip] = ips

    return result


def _extract_vlan_ids(rendered_commands: Dict[str, List[str]]) -> Dict[str, set]:
    """
    从渲染后的命令中提取 VLAN ID。

    Returns:
        {ip: {10, 20, 30}}
    """
    vlan_pattern = re.compile(r'vlan\s+(\d+)', re.IGNORECASE)
    result = {}

    for device_ip, commands in rendered_commands.items():
        vlans = set()
        for cmd in commands:
            match = vlan_pattern.search(cmd.strip())
            if match:
                vlans.add(int(match.group(1)))
        if vlans:
            result[device_ip] = vlans

    return result


def _get_existing_ips_from_collection(collection_results: dict) -> Dict[str, set]:
    """
    从采集结果中提取每台设备的已有 IP 地址。

    Returns:
        {device_ip: {"192.168.56.11", "10.0.0.1"}}
    """
    result = {}

    for device_ip, data in collection_results.items():
        if not isinstance(data, dict):
            continue

        ips = set()
        info = data.get("info", {})
        if isinstance(info, dict):
            interfaces = info.get("interfaces", [])
            for iface_line in interfaces:
                parts = iface_line.split() if isinstance(iface_line, str) else []
                if len(parts) >= 2:
                    ip_addr = parts[1]
                    if ip_addr and ip_addr != "---" and ip_addr != "unassigned":
                        # 验证是否是合法 IP
                        if re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', ip_addr):
                            ips.add(ip_addr)

        if ips:
            result[device_ip] = ips

    return result


def check_cross_device_conflicts(
    targets: List[str],
    rendered_commands: Dict[str, List[str]],
    collection_results: dict = None,
) -> Dict[str, Any]:
    """
    跨设备批量冲突检测。

    检测项目：
      1. Router ID 重复（OSPF/BGP/IS-IS）
      2. 同网段 IP 地址重复
      3. 与已有接口 IP 冲突（跨设备）
      4. VLAN ID 可能的冲突警告

    Args:
        targets: 目标设备 IP 列表
        rendered_commands: {ip: [渲染后的命令列表]}
        collection_results: 设备采集结果（可选）

    Returns:
        {
            "success": bool,
            "errors": [...],     # 严重错误，阻止执行
            "warnings": [...],   # 警告信息
            "suggestions": [...], # 建议信息
        }
    """
    result = {
        "success": True,
        "errors": [],
        "warnings": [],
        "suggestions": [],
    }

    # --- 1. Router ID 冲突检测 ---
    router_ids = _extract_router_ids(rendered_commands)
    rid_map = {}  # rid -> [ip1, ip2, ...]

    for device_ip, ids in router_ids.items():
        for ctx, rid in ids.items():
            if rid not in rid_map:
                rid_map[rid] = []
            rid_map[rid].append(f"{device_ip}({ctx})")

    for rid, devices in rid_map.items():
        if len(devices) > 1:
            result["success"] = False
            result["errors"].append({
                "type": "duplicate_router_id",
                "value": rid,
                "devices": devices,
                "message": f"Router ID '{rid}' 被多台设备使用: {', '.join(devices)}",
            })

    # --- 2. 配置 IP 地址重复检测 ---
    configured_ips = _extract_configured_ips(rendered_commands)
    ip_registry = {}  # address -> [{device, mask, interface}]

    for device_ip, ip_list in configured_ips.items():
        for entry in ip_list:
            addr = entry["address"]
            if addr not in ip_registry:
                ip_registry[addr] = []
            ip_registry[addr].append({
                "device": device_ip,
                "mask": entry["mask"],
                "interface": entry["interface"],
            })

    for addr, entries in ip_registry.items():
        if len(entries) > 1:
            devices_str = ", ".join(
                f"{e['device']}({e['interface']})" for e in entries
            )
            result["success"] = False
            result["errors"].append({
                "type": "duplicate_ip",
                "value": addr,
                "devices": [e["device"] for e in entries],
                "message": f"IP 地址 '{addr}' 被配置到多处: {devices_str}",
            })

    # --- 3. 与已有接口 IP 跨设备冲突检测 ---
    if collection_results:
        existing_ips = _get_existing_ips_from_collection(collection_results)

        for device_ip, ip_list in configured_ips.items():
            for entry in ip_list:
                addr = entry["address"]
                # 检查其他设备是否已使用该 IP
                for other_ip, other_ips in existing_ips.items():
                    if other_ip == device_ip:
                        continue
                    if addr in other_ips:
                        # 检查这是否是同一批配置中的设备（如果是，
                        # 已在上面的重复检测中报告）
                        if other_ip not in configured_ips:
                            result["warnings"].append({
                                "type": "ip_conflict_with_existing",
                                "value": addr,
                                "new_device": device_ip,
                                "existing_device": other_ip,
                                "message": (
                                    f"设备 {device_ip} 配置的 IP '{addr}' "
                                    f"已被设备 {other_ip} 使用"
                                ),
                            })

    # --- 4. VLAN 冲突警告 ---
    vlan_ids = _extract_vlan_ids(rendered_commands)
    all_vlans = {}  # vlan_id -> [ip1, ip2, ...]

    for device_ip, vlans in vlan_ids.items():
        for vid in vlans:
            if vid not in all_vlans:
                all_vlans[vid] = []
            all_vlans[vid].append(device_ip)

    for vid, devices in all_vlans.items():
        if len(devices) > 1:
            result["suggestions"].append({
                "type": "vlan_across_devices",
                "vlan_id": vid,
                "devices": devices,
                "message": (
                    f"VLAN {vid} 出现在多台设备上: {', '.join(devices)}，"
                    f"请确认 Trunk 链路和互联配置正确"
                ),
            })

    # --- 5. OSPF 网段重叠检测 ---
    ospf_networks = _extract_ospf_networks(rendered_commands)
    network_registry = {}  # network -> [ip1, ip2, ...]

    for device_ip, networks in ospf_networks.items():
        for net_str in networks:
            if net_str not in network_registry:
                network_registry[net_str] = []
            network_registry[net_str].append(device_ip)

    for net_str, devices in network_registry.items():
        if len(devices) > 1:
            result["suggestions"].append({
                "type": "ospf_network_overlap",
                "network": net_str,
                "devices": devices,
                "message": (
                    f"OSPF 网段 '{net_str}' 被多台设备宣告: {', '.join(devices)}，"
                    f"请确认 Area 配置正确"
                ),
            })

    return result


def _extract_ospf_networks(rendered_commands: Dict[str, List[str]]) -> Dict[str, List[str]]:
    """
    提取 OSPF network 宣告的网段。

    Returns:
        {device_ip: ["192.168.1.0", "10.0.0.0"]}
    """
    network_pattern = re.compile(
        r'^network\s+(\S+)\s+(\S+)', re.IGNORECASE
    )
    result = {}

    for device_ip, commands in rendered_commands.items():
        networks = []
        in_ospf = False

        for cmd in commands:
            cmd_stripped = cmd.strip()

            if re.match(r'^ospf\s+', cmd_stripped, re.IGNORECASE):
                in_ospf = True
                continue

            if in_ospf and cmd_stripped.lower() == "quit":
                in_ospf = False
                continue

            if in_ospf:
                match = network_pattern.match(cmd_stripped)
                if match:
                    networks.append(match.group(1))

        if networks:
            result[device_ip] = networks

    return result

