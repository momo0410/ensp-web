"""
config_clone_service.py - 设备配置复制与自动适配
================================================
从源设备当前配置生成面向目标设备的安全迁移命令。

重点处理设备身份相关配置，并在不猜测业务语义的前提下自动解决冲突：
- sysname -> 目标设备名称
- OSPF/BGP Router ID -> 目标管理 IP
- IS-IS NET -> 基于目标管理 IP 生成唯一 System ID
- 接口 IP -> 保留目标接口已有 IP，或在原网段内分配未使用地址
- 不存在的物理接口、完全重复的配置块 -> 自动跳过
- AAA、VTY、SSH 密钥、预共享密钥等敏感配置 -> 默认隔离
"""

import ipaddress
import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


DEFAULT_DOMAINS = [
    "identity",
    "basic",
    "interfaces",
    "switching",
    "routing",
    "services",
]

DOMAIN_NAMES = {
    "identity": "设备身份",
    "basic": "基础功能",
    "interfaces": "接口配置",
    "switching": "交换配置",
    "routing": "路由协议",
    "services": "网络服务/VPN",
    "security": "安全与管理访问",
}

_SENSITIVE_LINE_PATTERNS = [
    re.compile(r"\bpassword\b", re.IGNORECASE),
    re.compile(r"\bpre-shared-key\b", re.IGNORECASE),
    re.compile(r"\bauthentication-key\b", re.IGNORECASE),
    re.compile(r"\bset authentication\b", re.IGNORECASE),
]

_VIEW_HEADERS = re.compile(
    r"^(?:"
    r"interface\s+|vlan\s+\d|aaa$|user-interface\s+|"
    r"ospf\s+|rip(?:\s+|$)|bgp\s+|isis\s+|area\s+|"
    r"acl\s+|ip pool\s+|ip vpn-instance\s+|"
    r"ike proposal\s+|ike peer\s+|ipsec proposal\s+|ipsec policy\s+|"
    r"traffic classifier\s+|traffic behavior\s+|traffic policy\s+|"
    r"firewall zone\s+|wlan ac$"
    r")",
    re.IGNORECASE,
)

_PHYSICAL_INTERFACE = re.compile(
    r"^(?:GigabitEthernet|Ethernet|FastEthernet|XGigabitEthernet|"
    r"Ten-GigabitEthernet|Serial|Eth-Trunk|GE|XGE|FE)\S+$",
    re.IGNORECASE,
)


def split_config_blocks(config: str) -> List[List[Tuple[int, str]]]:
    """将 VRP current-configuration 按 # 分割，并保留缩进层级。"""
    blocks: List[List[Tuple[int, str]]] = []
    current: List[Tuple[int, str]] = []

    def flush() -> None:
        nonlocal current
        if not current:
            return
        min_indent = min(indent for indent, _ in current)
        blocks.append([(indent - min_indent, cmd) for indent, cmd in current])
        current = []

    for raw_line in (config or "").replace("\r", "").split("\n"):
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped == "#":
            flush()
            continue
        if stripped.lower() == "return":
            flush()
            continue
        if re.match(r"^\[V\d", stripped):
            continue
        if re.match(r"^[<\[].+[>\]]$", stripped):
            continue
        indent = len(raw_line) - len(raw_line.lstrip())
        current.append((indent, stripped))

    flush()
    return blocks


def classify_block(block: List[Tuple[int, str]]) -> str:
    """识别配置块所属功能域。"""
    if not block:
        return "other"
    first = block[0][1].lower()
    all_text = "\n".join(cmd.lower() for _, cmd in block)

    if first.startswith("sysname "):
        return "identity"
    if first.startswith("interface "):
        return "interfaces"
    if first.startswith(("vlan ", "stp ", "eth-trunk ")):
        return "switching"
    if first.startswith(("ospf ", "rip", "bgp ", "isis ", "ip route-static ")):
        return "routing"
    if first.startswith(("aaa", "user-interface ", "stelnet ", "ssh ", "snmp-agent", "firewall ")):
        return "security"
    if first.startswith(("lldp ", "clock ", "info-center ", "header ")):
        return "basic"
    if first.startswith((
        "dhcp ", "ip pool ", "nat ", "acl ", "ike ", "ipsec ",
        "ip vpn-instance ", "traffic ", "ntp-service ", "vrrp ",
    )):
        return "services"
    if "local-user " in all_text or "protocol inbound " in all_text:
        return "security"
    return "other"


def block_to_commands(block: List[Tuple[int, str]]) -> List[str]:
    """将带缩进的 current-configuration 块转换为可顺序下发的命令。"""
    if not block:
        return []

    commands: List[str] = []
    for index, (indent, cmd) in enumerate(block):
        if index:
            previous_indent = block[index - 1][0]
            if indent < previous_indent:
                commands.extend(["quit"] * (previous_indent - indent))
        commands.append(cmd)

    final_indent = block[-1][0]
    # A view header changes context even when the block has no child lines,
    # for example a current config containing only "ospf 1 router-id ...".
    final_depth = max(final_indent, 1) if _VIEW_HEADERS.match(block[0][1]) else final_indent
    commands.extend(["quit"] * final_depth)
    return commands


def _normalize_block(block: List[Tuple[int, str]]) -> Tuple[Tuple[int, str], ...]:
    return tuple((indent, re.sub(r"\s+", " ", cmd.strip()).lower()) for indent, cmd in block)


def _extract_interface_names(config: str) -> Set[str]:
    names = set()
    for block in split_config_blocks(config):
        if block and block[0][1].lower().startswith("interface "):
            names.add(block[0][1].split(None, 1)[1].lower())
    return names


def _extract_interface_ips(config: str) -> Dict[str, Tuple[str, str]]:
    result: Dict[str, Tuple[str, str]] = {}
    for block in split_config_blocks(config):
        if not block or not block[0][1].lower().startswith("interface "):
            continue
        interface = block[0][1].split(None, 1)[1]
        for _, cmd in block[1:]:
            match = re.match(r"^ip address\s+(\S+)\s+(\S+)", cmd, re.IGNORECASE)
            if match and _is_ipv4(match.group(1)):
                result[interface.lower()] = (match.group(1), match.group(2))
                break
    return result


def _extract_all_interface_ips(configs: Iterable[str]) -> Set[str]:
    used: Set[str] = set()
    for config in configs:
        for address, _ in _extract_interface_ips(config).values():
            used.add(address)
    return used


def _is_ipv4(value: str) -> bool:
    try:
        ipaddress.IPv4Address(value)
        return True
    except ValueError:
        return False


def _mask_to_network(address: str, mask: str) -> Optional[ipaddress.IPv4Network]:
    try:
        return ipaddress.IPv4Network(f"{address}/{mask}", strict=False)
    except ValueError:
        return None


def _allocate_ip(
    source_address: str,
    mask: str,
    target_management_ip: str,
    used_ips: Set[str],
) -> Optional[str]:
    network = _mask_to_network(source_address, mask)
    if not network:
        return None

    try:
        last_octet = int(target_management_ip.split(".")[-1])
        candidate_int = int(network.network_address) + last_octet
        candidate = ipaddress.IPv4Address(candidate_int)
        if candidate in network and candidate not in (network.network_address, network.broadcast_address):
            candidate_text = str(candidate)
            if candidate_text not in used_ips:
                return candidate_text
    except (ValueError, IndexError):
        pass

    for candidate in network.hosts():
        candidate_text = str(candidate)
        if candidate_text not in used_ips:
            return candidate_text
    return None


def _target_isis_net(current_net: str, target_ip: str) -> str:
    """保留 Area ID，仅替换 IS-IS System ID。"""
    parts = current_net.split(".")
    if len(parts) < 3:
        return current_net
    try:
        digits = "".join(f"{int(part):03d}" for part in target_ip.split("."))
        system_id = ".".join(digits[i:i + 4] for i in range(0, 12, 4))
        return f"{parts[0]}.{system_id}.00"
    except ValueError:
        return current_net


def _adapt_block(
    block: List[Tuple[int, str]],
    source_ip: str,
    target: Dict[str, Any],
    target_interfaces: Set[str],
    target_interface_ips: Dict[str, Tuple[str, str]],
    used_ips: Set[str],
    include_sensitive: bool,
) -> Tuple[Optional[List[Tuple[int, str]]], List[dict], List[dict]]:
    adapted: List[Tuple[int, str]] = []
    resolutions: List[dict] = []
    skipped: List[dict] = []
    target_ip = target["ip"]
    target_name = target.get("name") or target_ip
    interface_name = ""

    if block and block[0][1].lower().startswith("interface "):
        interface_name = block[0][1].split(None, 1)[1]
        normalized_interface = interface_name.lower()
        if _PHYSICAL_INTERFACE.match(interface_name) and normalized_interface not in target_interfaces:
            skipped.append({
                "type": "missing_interface",
                "block": block[0][1],
                "message": f"目标设备不存在物理接口 {interface_name}，已跳过该接口配置",
            })
            return None, resolutions, skipped

    for indent, original in block:
        cmd = original
        lower = cmd.lower()

        if not include_sensitive and any(pattern.search(cmd) for pattern in _SENSITIVE_LINE_PATTERNS):
            skipped.append({
                "type": "sensitive_command",
                "command": cmd,
                "message": f"敏感命令已隔离，未复制: {cmd}",
            })
            continue

        if lower.startswith("sysname "):
            replacement = f"sysname {target_name}"
            if replacement != cmd:
                resolutions.append({
                    "type": "sysname",
                    "from": cmd,
                    "to": replacement,
                    "message": f"设备名自动改为 {target_name}",
                })
            cmd = replacement

        cmd, count = re.subn(
            r"(\brouter-id\s+)\d{1,3}(?:\.\d{1,3}){3}",
            rf"\g<1>{target_ip}",
            cmd,
            flags=re.IGNORECASE,
        )
        if count:
            resolutions.append({
                "type": "router_id",
                "from": original,
                "to": cmd,
                "message": f"Router ID 自动改为目标管理 IP {target_ip}",
            })

        net_match = re.match(r"^network-entity\s+(\S+)", cmd, re.IGNORECASE)
        if net_match:
            new_net = _target_isis_net(net_match.group(1), target_ip)
            if new_net != net_match.group(1):
                replacement = f"network-entity {new_net}"
                resolutions.append({
                    "type": "isis_net",
                    "from": cmd,
                    "to": replacement,
                    "message": f"IS-IS NET 自动生成唯一 System ID: {new_net}",
                })
                cmd = replacement

        ip_match = re.match(r"^ip address\s+(\S+)\s+(\S+)(.*)$", cmd, re.IGNORECASE)
        if ip_match and interface_name and _is_ipv4(ip_match.group(1)):
            old_ip, mask, suffix = ip_match.groups()
            existing = target_interface_ips.get(interface_name.lower())
            new_ip = old_ip

            if old_ip == source_ip:
                new_ip = target_ip
            elif existing:
                new_ip = existing[0]
                mask = existing[1]
            elif old_ip in used_ips:
                allocated = _allocate_ip(old_ip, mask, target_ip, used_ips)
                if allocated:
                    new_ip = allocated
                else:
                    skipped.append({
                        "type": "ip_allocation_failed",
                        "command": cmd,
                        "message": f"{interface_name} 的地址 {old_ip}/{mask} 无可用地址，已跳过",
                    })
                    continue

            if new_ip != old_ip:
                replacement = f"ip address {new_ip} {mask}{suffix}"
                resolutions.append({
                    "type": "interface_ip",
                    "interface": interface_name,
                    "from": cmd,
                    "to": replacement,
                    "message": f"{interface_name} 地址冲突已自动改为 {new_ip}",
                })
                cmd = replacement
            used_ips.add(new_ip)

        adapted.append((indent, cmd))

    if not adapted:
        return None, resolutions, skipped
    return adapted, resolutions, skipped


def build_clone_preview(
    source_device: Dict[str, Any],
    source_config: str,
    target_devices: List[Dict[str, Any]],
    target_configs: Dict[str, str],
    domains: Optional[List[str]] = None,
    auto_resolve: bool = True,
    include_sensitive: bool = False,
    reserved_ips: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """生成多目标设备配置复制预览。"""
    selected_domains = set(domains or DEFAULT_DOMAINS)
    source_blocks = split_config_blocks(source_config)
    all_configs = [source_config] + list(target_configs.values())
    globally_used_ips = _extract_all_interface_ips(all_configs)
    globally_used_ips.update(reserved_ips or set())
    result = {
        "success": True,
        "source_device": source_device.get("ip", ""),
        "domains": sorted(selected_domains),
        "targets": {},
        "errors": [],
        "warnings": [],
    }

    if not source_blocks:
        result["success"] = False
        result["errors"].append("源设备配置为空，无法生成复制方案")
        return result

    for target in target_devices:
        target_ip = target["ip"]
        target_config = target_configs.get(target_ip, "")
        target_blocks = split_config_blocks(target_config)
        target_normalized = {_normalize_block(block) for block in target_blocks}
        target_interfaces = _extract_interface_names(target_config)
        target_interface_ips = _extract_interface_ips(target_config)
        used_ips = set(globally_used_ips)
        commands: List[str] = []
        resolutions: List[dict] = []
        skipped: List[dict] = []
        included_blocks = 0

        for block in source_blocks:
            domain = classify_block(block)
            if domain == "security" and not include_sensitive:
                skipped.append({
                    "type": "sensitive_domain",
                    "block": block[0][1],
                    "message": f"安全与管理访问配置默认不复制: {block[0][1]}",
                })
                continue
            if domain not in selected_domains:
                skipped.append({
                    "type": "domain_not_selected",
                    "block": block[0][1],
                    "domain": domain,
                    "message": f"未选择“{DOMAIN_NAMES.get(domain, domain)}”，已跳过 {block[0][1]}",
                })
                continue

            if auto_resolve:
                adapted, block_resolutions, block_skipped = _adapt_block(
                    block,
                    source_device["ip"],
                    target,
                    target_interfaces,
                    target_interface_ips,
                    used_ips,
                    include_sensitive,
                )
            else:
                adapted, block_resolutions, block_skipped = block, [], []

            resolutions.extend(block_resolutions)
            skipped.extend(block_skipped)
            if not adapted:
                continue
            if _normalize_block(adapted) in target_normalized:
                skipped.append({
                    "type": "identical_block",
                    "block": adapted[0][1],
                    "message": f"目标设备已存在相同配置，已跳过: {adapted[0][1]}",
                })
                continue

            commands.extend(block_to_commands(adapted))
            included_blocks += 1

        result["targets"][target_ip] = {
            "device_name": target.get("name", target_ip),
            "commands": commands,
            "resolutions": resolutions,
            "skipped": skipped,
            "included_blocks": included_blocks,
        }
        if not commands:
            result["warnings"].append(f"目标设备 {target_ip} 没有需要下发的配置")

    return result
