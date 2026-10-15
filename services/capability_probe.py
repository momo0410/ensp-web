"""
capability_probe.py - 设备能力识别模块
=======================================
通过本地 Console 登录华为设备，执行 display 命令，识别：
- 设备类型（router / switch / l3_switch / unknown）
- 支持的协议和功能（supported / configured / running）
"""

import re
import logging
import time
from typing import Dict, Any, Optional, List

from .device_connector import (
    import_console,
    build_conn_params,
    safe_disconnect,
    command_result_from_output,
    is_unsupported_error,
    is_output_unsupported,
    strip_ansi,
)
from domain.collect import fast_send_command
from .capability_matrix import lookup_matrix, matrix_support, matrix_note
from .help_tree import collect_views_over_connection, detect_protocols, find_first_physical_interface, parse_help_keywords

logger = logging.getLogger("ensp-web.capability")

# ---------------------------------------------------------------------------
# 能力探测命令配置
# ---------------------------------------------------------------------------

# 基础信息命令
BASE_COMMANDS = [
    "display version",
    "display device",
    "display current-configuration",
    "display ip interface brief",
    "display interface brief",
]

# 能力探测命令映射: capability_key -> display command
CAPABILITY_COMMANDS = {
    "routing_table":  "display ip routing-table",
    "ospf":           "display ospf peer",
    "bgp":            "display bgp peer",
    "isis":           "display isis peer",
    "rip":            "display rip",
    "static_route":   "display ip routing-table",
    "vlan":           "display vlan",
    "stp":            "display stp brief",
    "lldp":           "display lldp neighbor brief",
    "eth_trunk":      "display eth-trunk",
    "vrrp":           "display vrrp brief",
    "acl":            "display acl all",
    "snmp":           "display snmp-agent sys-info",
    "dhcp":           "display dhcp server statistics",
    "nat":            "display nat session",
    "interface_ip":   "display ip interface brief",
    "access_port":    "display interface brief",
    "trunk_port":     "display interface brief",
    "vlanif":         "display ip interface brief",
    "ssh":            "display ssh server status",
    "telnet":         "display telnet server status",
    # 安全/VPN 功能
    "ipsec":          "display ipsec sa",
    "ike":            "display ike sa",
    "gre":            "display interface Tunnel",
    "vpn_instance":   "display ip vpn-instance",
    # 扩展协议（完整协议清单）
    "ntp":            "display ntp-service status",
    "l2tp":           "display l2tp session",
    "pppoe":          "display pppoe-server session all",
    "ipv6":           "display ipv6 interface brief",
    "mpls":           "display mpls interface",
    "mpls_ldp":       "display mpls ldp session",
    "igmp":           "display igmp group",
    "pim":            "display pim neighbor",
    "igmp_snooping":  "display igmp-snooping",
}

# 多候选探测命令: 主命令报"不支持"时依次尝试，任一成功即算支持。
# 防止单条命令在特定镜像上不存在导致整项协议被误判 unsupported。
ALT_COMMANDS = {
    "ospf":          ["display ospf brief"],
    "bgp":           ["display bgp routing-table"],
    "nat":           ["display nat statistics", "display nat session all"],
    "dhcp":          ["display dhcp statistics"],
    "vlan":          ["display port vlan"],
    "stp":           ["display stp"],
    "ntp":           ["display ntp status", "display ntp-service service-status"],
    "ipv6":          ["display ipv6 interface"],
    "mpls":          ["display mpls lsp"],
    "mpls_ldp":      ["display mpls ldp neighbor"],
    "igmp":          ["display igmp interface"],
    "pim":           ["display pim routing-table"],
    "igmp_snooping": ["display igmp-snooping configuration"],
    "l2tp":          ["display l2tp tunnel"],
    "pppoe":         ["display pppoe-client session summary"],
    "ipsec":         ["display ipsec policy"],
    "ike":           ["display ike proposal"],
}

# 补充探测命令: 某些能力需要多条命令综合判断
SUPPLEMENTARY_COMMANDS = {
    "ipsec": [
        "display ipsec policy",
        "display ipsec proposal",
    ],
}

# 配置关键字映射: capability_key -> list of keywords to search in current-config
CONFIG_KEYWORDS = {
    "static_route":  ["ip route-static"],
    "ospf":          ["ospf "],
    "isis":          ["isis "],
    "bgp":           ["bgp "],
    "rip":           ["rip"],
    "vlan":          ["vlan "],
    "access_port":   ["port link-type access", "port default vlan"],
    "trunk_port":    ["port link-type trunk", "port trunk allow-pass vlan"],
    "vlanif":        ["interface Vlanif"],
    "stp":           ["stp mode", "stp enable"],
    "eth_trunk":     ["eth-trunk", "mode lacp", "mode manual"],
    "lldp":          ["lldp enable"],
    "interface_ip":  ["ip address"],
    "acl":           ["acl number", "acl "],
    "dhcp":          ["dhcp enable", "ip pool"],
    "nat":           ["nat outbound", "nat static", "nat server"],
    "vrrp":          ["vrrp vrid"],
    "snmp":          ["snmp-agent"],
    "ssh":           ["stelnet server enable", "ssh user"],
    "telnet":        ["telnet server enable", "user-interface vty"],
    "routing_table": [],
    # 扩展协议
    "ntp":           ["ntp-service", "ntp "],
    "l2tp":          ["l2tp-group", "allow l2tp", "start l2tp"],
    "pppoe":         ["pppoe-server", "pppoe-client"],
    "ipv6":          ["ipv6"],
    "mpls":          ["mpls"],
    "mpls_ldp":      ["mpls ldp"],
    "igmp":          ["igmp enable", "igmp "],
    "pim":           ["pim dm", "pim sm", "pim ", "multicast routing-enable"],
    "igmp_snooping": ["igmp-snooping"],
    # 安全/VPN 功能
    "ipsec":         ["ipsec proposal", "ipsec policy", "ipsec sa", "ipsec transform"],
    "ike":           ["ike proposal", "ike peer", "ike sa", "ike key-exchange"],
    "gre":           ["interface Tunnel", "tunnel-protocol gre", "tunnel source", "tunnel destination"],
    "vpn_instance":  ["ip vpn-instance", "route-distinguisher", "vpn-target"],
}

# 运行状态关键字: capability_key -> keywords indicating active/running state
RUNNING_KEYWORDS = {
    "ospf":          ["Full", "2-Way", "ExStart", "Loading", "Exchange"],
    "bgp":           ["Established", "Active", "Connect", "OpenConfirm"],
    "isis":          ["Up", "adjacency"],
    "rip":           ["RIP Process", "neighbors"],
    "static_route":  ["Static", "/"],
    "vlan":          ["VLAN ID"],
    "stp":           ["CST", "MSTI", "Root", "Forwarding", "Discarding", "DESIGNATED", "ROOT"],
    "lldp":          ["neighbor", "GE", "XGE", "Eth", "FE"],
    "eth_trunk":     ["Eth-Trunk", "up", "LACP"],
    "vrrp":          ["Master", "Backup", "Initialize"],
    "dhcp":          ["pool", "lease", "used", "expired"],
    "nat":           ["session", "NAT"],
    "routing_table": ["Destination", "Direct", "Static", "OSPF", "RIP", "BGP", "ISIS"],
    "interface_ip":  ["up", "up", "down"],
    "ssh":           ["Enable", "STelnet"],
    "telnet":        ["Enable", "Telnet"],
    "acl":           ["rule", "match"],
    "snmp":          ["sys-info", "version", "community"],
    "access_port":   ["access"],
    "trunk_port":    ["trunk"],
    "vlanif":        ["Vlanif", "up"],
    # 安全/VPN 功能
    "ipsec":         ["sa", "active", "established", "phase", "inbound", "outbound", "esp", "ah"],
    "ike":           ["sa", "established", "phase", "v1", "v2", "conn-id"],
    "gre":           ["Tunnel", "up", "GRE", "protocol gre"],
    "vpn_instance":  ["vpn-instance", "rd", "rt", "export-extcommunity", "import-extcommunity"],
    # 扩展协议
    "ntp":           ["synchronized", "stratum", "clock status"],
    "l2tp":          ["tunnel", "session"],
    "pppoe":         ["session", "up"],
    "ipv6":          ["fe80", "link-local", "up"],
    "mpls":          ["lsp", "interface", "up"],
    "mpls_ldp":      ["session", "operational", "up"],
    "igmp":          ["group", "member"],
    "pim":           ["neighbor", "up"],
    "igmp_snooping": ["enable", "router port"],
}

def _classify_device_role(version_output: str, device_output: str, current_config: str, model: str) -> str:
    """
    根据命令输出判断设备角色。

    Returns:
        "router" / "switch" / "l3_switch" / "unknown"
    """
    text_all = f"{version_output}\n{device_output}\n{current_config}".lower()
    model_lower = (model or "").lower()

    has_vlan = bool(re.search(r"\bvlan\b", current_config, re.IGNORECASE))
    has_vlanif = bool(re.search(r"interface\s+vlanif", current_config, re.IGNORECASE))
    has_stp = bool(re.search(r"\bstp\b", current_config, re.IGNORECASE))
    has_routing = bool(re.search(r"\b(ospf|bgp|isis|rip|ip route-static)\b", current_config, re.IGNORECASE))
    has_nat = bool(re.search(r"\bnat\b", current_config, re.IGNORECASE))

    # Model-based hints
    if any(kw in model_lower for kw in ["ar1", "ar2", "ar3", "ar4", "ar12", "ar22", "ar32", "ne"]):
        return "router"
    if any(kw in model_lower for kw in ["s57", "s67", "s37", "s27", "ce"]):
        if has_vlanif and has_routing:
            return "l3_switch"
        return "switch"
    if "lsw" in model_lower or "switch" in model_lower:
        if has_vlanif and has_routing:
            return "l3_switch"
        return "switch"

    # Behavior-based
    if has_vlan and has_stp and not has_routing:
        return "switch"
    if has_vlan and has_vlanif and has_routing:
        return "l3_switch"
    if has_routing or has_nat:
        return "router"
    if has_vlan:
        return "switch"

    return "unknown"

def _parse_interface_list(ip_intf_output: str, current_config: str) -> List[Dict[str, str]]:
    """
    从 display ip interface brief 输出解析接口列表。

    返回格式: [{"name": "GigabitEthernet0/0/0", "ip": "192.168.1.1", "status": "up"}, ...]
    """
    interfaces = []
    if not ip_intf_output:
        return interfaces

    # 需要过滤的接口前缀
    skip_prefixes = ("null", "inloop", "tunnel", "vlanif", "dialer", "mp-group", "virtual-template")
    # LoopBack 只保留 LoopBack0
    allowed_loopbacks = {"loopback0"}

    seen = set()
    for line in ip_intf_output.split("\n"):
        line = line.strip()
        if not line or ("Interface" in line and "IP-Address" in line):
            continue
        # 匹配接口行: Interface IP-Address Physical Protocol
        parts = line.split()
        if len(parts) >= 4:
            intf_name = parts[0]
            ip_addr = parts[1] if parts[1] != "unassigned" else ""
            status = parts[2] if len(parts) > 2 else ""
            intf_lower = intf_name.lower().replace(" ", "")

            # 过滤不重要的接口
            if intf_lower.startswith(skip_prefixes):
                continue
            # LoopBack 只保留 LoopBack0
            if intf_lower.startswith("loopback") and intf_lower not in allowed_loopbacks:
                continue

            if intf_lower not in seen:
                seen.add(intf_lower)
                interfaces.append({
                    "name": intf_name,
                    "ip": ip_addr,
                    "status": status,
                })

    # 按接口名排序
    def _intf_sort_key(intf):
        name = intf["name"].lower()
        nums = re.findall(r'\d+', name)
        return (name.split('/')[0] if '/' in name else name, [int(n) for n in nums])

    interfaces.sort(key=_intf_sort_key)
    return interfaces

def _make_cap(cap_key: str, supported: bool, evidence: str, note: str = "",
              error: Optional[str] = None) -> dict:
    """构造一个不经过 display 探测的能力条目（矩阵/?树直接判定）。"""
    return {
        "supported": supported,
        "configured": False,
        "running": False,
        "display_cmd": CAPABILITY_COMMANDS.get(cap_key, ""),
        "config_keywords": CONFIG_KEYWORDS.get(cap_key, []),
        "output": "",
        "error": error,
        "evidence": evidence,
        "note": note,
    }

def _try_alt_commands(cap_key: str, cap: dict, get_result) -> None:
    """主命令不支持时依次尝试候选命令，任一成功即算支持（原地修改 cap）。"""
    if cap.get("supported"):
        return
    for alt in ALT_COMMANDS.get(cap_key, []):
        res = get_result(alt)
        if res and res.get("success") and not is_output_unsupported(res.get("output", "")):
            cap["supported"] = True
            cap["error"] = None
            cap["display_cmd"] = alt
            cap["evidence"] = f"{cap.get('evidence', 'probe')}+alt"
            return

def _apply_matrix_override(cap_key: str, cap: dict, matrix: Optional[dict]) -> None:
    """
    矩阵与探测结果合并：镜像确定支持/不支持的协议以矩阵为准。
    - 矩阵 True 但 display 命令不被该镜像识别 → 保留 supported=True
      （修复"命令不存在被误判为协议不支持"），证据标记 matrix。
    - 矩阵 False 但探测意外显示支持 → 以矩阵为准，避免噪音误报。
    """
    ms = matrix_support(matrix, cap_key)
    if ms is None:
        cap.setdefault("evidence", "probe")
        return
    if ms is True:
        if cap.get("supported"):
            ev = cap.get("evidence", "probe")
            cap["evidence"] = ev if "matrix" in ev else f"{ev}+matrix"
        else:
            # 探测命令在该镜像上不存在/失败，但镜像确定支持该协议
            cap["supported"] = True
            cap["error"] = None
            cap["evidence"] = "matrix"
            cap["note"] = matrix_note(matrix, cap_key) or "矩阵判定支持；探测命令在该镜像不可用"
    else:  # ms is False
        if cap.get("supported"):
            cap["supported"] = False
            cap["running"] = False
            cap["evidence"] = "matrix"
            cap["note"] = matrix_note(matrix, cap_key)
        else:
            cap["evidence"] = "matrix"
            cap["note"] = matrix_note(matrix, cap_key) or "矩阵判定不支持"

def _apply_tree_detection(result: dict, views: Dict[str, Any]) -> None:
    """
    用 ? 命令树给探测结果补证据：树里实测见到的协议至少 supported=True。
    命令树是实测证据，优先级最高（可推翻矩阵的保守判断），只升不降。
    """
    normalized = {}
    for scope, val in (views or {}).items():
        normalized[scope] = set(val) if not isinstance(val, set) else val
    detected = detect_protocols(normalized)
    if detected:
        result["tree_detected"] = detected
    for key, ev in detected.items():
        cap = result["capabilities"].get(key)
        if cap is None:
            cap = _make_cap(key, True, f"helptree:{ev}")
            result["capabilities"][key] = cap
            continue
        if not cap.get("supported"):
            cap["supported"] = True
            cap["error"] = None
            cap["evidence"] = f"helptree:{ev}"
        else:
            prev = cap.get("evidence", "probe")
            if "helptree" not in prev:
                cap["evidence"] = f"{prev}+helptree"

def _collect_views_from_result(result: dict, connection: Any = None,
                               shared_results: Optional[dict] = None,
                               read_timeout: int = 3,
                               deadline_s: float = 8.0) -> Dict[str, Any]:
    """
    尽力采集 ? 命令树：有连接时在连接上采集（受 deadline_s 硬性时间预算
    约束）；无连接时只用并行补跑带回的 display ? 输出。
    失败不致命——只影响证据丰富度，不影响原有 supported 判定。
    """
    views: Dict[str, Any] = {}

    if connection is None:
        res = (shared_results or {}).get("display ?") or {}
        if isinstance(res, dict) and res.get("success"):
            views["display"] = parse_help_keywords(strip_ansi(res.get("output", "")))
        return views

    interface_name = find_first_physical_interface(
        result.get("raw_outputs", {}).get("display interface brief", ""))
    tree_views, _errors = collect_views_over_connection(
        connection, read_timeout=read_timeout,
        interface_name=interface_name, deadline_s=deadline_s,
    )
    views.update(tree_views)
    return views

def _check_capability(
    cap_key: str,
    display_result: dict,
    current_config: str,
) -> dict:
    """
    判断单个能力的 supported/configured/running 状态。
    """
    supported = False
    configured = False
    running = False
    output = ""
    error = None

    cmd_output = display_result.get("output", "")
    cmd_error = display_result.get("error")
    cmd_success = display_result.get("success", False)

    # Determine supported
    if cmd_success and not is_output_unsupported(cmd_output):
        supported = True
    elif cmd_error and is_unsupported_error(cmd_error):
        supported = False
        error = cmd_error
    elif cmd_error:
        # Some other error (timeout etc.) - treat as probe failed, not unsupported
        supported = True  # probably supported but probe failed
        error = cmd_error
    elif cmd_success and is_output_unsupported(cmd_output):
        supported = False
        error = cmd_output.strip()[:200]

    # Determine configured (from current-config keywords)
    keywords = CONFIG_KEYWORDS.get(cap_key, [])
    if keywords and current_config:
        configured = any(kw.lower() in current_config.lower() for kw in keywords)

    # Determine running (from display output keywords)
    if supported and cmd_output:
        run_kws = RUNNING_KEYWORDS.get(cap_key, [])
        if run_kws:
            running = any(kw.lower() in cmd_output.lower() for kw in run_kws)
        elif configured:
            running = True  # if configured and display returned data, likely running

    # Special cases
    if cap_key == "static_route" and current_config:
        configured = bool(re.search(r"ip route-static\s+\d", current_config, re.IGNORECASE))
        if configured and cmd_output:
            running = "Static" in cmd_output or any(
                re.search(r"\d+\.\d+\.\d+\.\d+", line) for line in cmd_output.split("\n")
                if "Static" in line
            )

    if cap_key == "interface_ip" and cmd_output:
        running = bool(re.search(r"\d+\.\d+\.\d+\.\d+", cmd_output))
        configured = running

    if cap_key == "routing_table":
        configured = True  # routing table always exists
        running = bool(re.search(r"Destination", cmd_output, re.IGNORECASE))

    # IPsec: 综合判断 display ipsec sa 和 supplementary commands
    if cap_key == "ipsec":
        # configured: check for ipsec proposal/policy in current config
        configured = any(kw.lower() in current_config.lower() for kw in CONFIG_KEYWORDS.get("ipsec", []))
        # running: check for active SA entries
        if supported and cmd_output:
            running = any(kw.lower() in cmd_output.lower() for kw in RUNNING_KEYWORDS.get("ipsec", []))
            # "No SA" or "There is no" means not running
            if re.search(r"no\s+sa|there\s+is\s+no|no\s+record|0\s+sa", cmd_output, re.IGNORECASE):
                running = False

    # IKE: similar to IPsec
    if cap_key == "ike":
        configured = any(kw.lower() in current_config.lower() for kw in CONFIG_KEYWORDS.get("ike", []))
        if supported and cmd_output:
            running = any(kw.lower() in cmd_output.lower() for kw in RUNNING_KEYWORDS.get("ike", []))
            if re.search(r"no\s+sa|there\s+is\s+no|no\s+record|0\s+sa", cmd_output, re.IGNORECASE):
                running = False

    # GRE: check for tunnel interfaces
    if cap_key == "gre":
        if supported and cmd_output:
            running = bool(re.search(r"Tunnel\d+.*up", cmd_output, re.IGNORECASE))
            configured = bool(re.search(r"interface\s+Tunnel", current_config, re.IGNORECASE))

    # VPN Instance: check for vpn-instance config
    if cap_key == "vpn_instance":
        configured = bool(re.search(r"ip\s+vpn-instance\s+\S+", current_config, re.IGNORECASE))
        if supported and cmd_output:
            running = bool(re.search(r"vpn-instance\s+\S+", cmd_output, re.IGNORECASE))

    return {
        "supported": supported,
        "configured": configured,
        "running": running,
        "display_cmd": CAPABILITY_COMMANDS.get(cap_key, ""),
        "config_keywords": CONFIG_KEYWORDS.get(cap_key, []),
        "output": cmd_output[:3000] if cmd_output else "",
        "error": error,
        "evidence": "probe",
        "note": "",
    }

def probe_capabilities(
    device: dict,
    timeout: int = 10,
    read_timeout: int = 15,
) -> dict:
    """
    通过本地 Console 连接设备并识别所有能力。

    Args:
        device: 设备字典
        timeout: 连接超时
        read_timeout: 命令读取超时

    Returns:
        完整的能力识别结果字典
    """
    ip = device["ip"]
    hostname = device.get("name", ip)
    result: Dict[str, Any] = {
        "ip": ip,
        "hostname": hostname,
        "device_role": "unknown",
        "platform": "",
        "os": "VRP",
        "model": "",
        "version": "",
        "ok": False,
        "capabilities": {},
        "raw_outputs": {},
        "errors": [],
    }

    connection = None

    try:
        ConnectHandler, ConsoleTimeoutError, ConsoleAuthError = import_console()
        params = build_conn_params(device, timeout=timeout)

        connection = ConnectHandler(**params)
        result["ok"] = True

        # ---- Execute base commands ----
        current_config = ""
        version_output = ""
        device_output = ""

        for cmd in BASE_COMMANDS:
            try:
                out = connection.send_command(cmd, read_timeout=read_timeout)
                result["raw_outputs"][cmd] = out
                if cmd == "display current-configuration":
                    current_config = out
                elif cmd == "display version":
                    version_output = out
                elif cmd == "display device":
                    device_output = out
            except Exception as e:
                err_msg = f"{cmd}: {e}"
                result["errors"].append(err_msg)
                result["raw_outputs"][cmd] = ""
                logger.warning("[%s] base command '%s' failed: %s", ip, cmd, e)

        # Parse model/version from display version
        model_match = re.search(r"(AR\d+|S\d+|NE\d+|CE\d+|USG\d+|LSW\d+|Router|Switch)\S*", version_output)
        result["model"] = model_match.group(0) if model_match else ""
        result["platform"] = f"Huawei {result['model']}" if result["model"] else "Huawei"

        ver_match = re.search(r"Version\s+([\d.]+\w*)", version_output)
        result["version"] = ver_match.group(1) if ver_match else ""

        hostname_match = re.search(r"<(\S+)>", version_output) or re.search(r"sysname\s+(\S+)", current_config)
        if hostname_match:
            result["hostname"] = hostname_match.group(1)

        # Classify device role
        result["device_role"] = _classify_device_role(
            version_output, device_output, current_config, result["model"]
        )

        # ---- 能力矩阵：镜像确定不支持的协议直接定论，跳过探测 ----
        matrix = lookup_matrix(version_output)
        result["matrix"] = {k: v for k, v in matrix.items() if k != "protocols"}

        # ---- Probe each capability ----
        # Group capabilities by their display command to avoid running same command twice
        matrix_settled: Dict[str, dict] = {}
        cmd_to_caps: Dict[str, list] = {}
        for cap_key, cmd in CAPABILITY_COMMANDS.items():
            if matrix_support(matrix, cap_key) is False:
                # 矩阵确定不支持：不再发探测命令，避免 Unrecognized 噪音
                matrix_settled[cap_key] = _make_cap(cap_key, False, "matrix",
                                                    matrix_note(matrix, cap_key))
                continue
            cmd_to_caps.setdefault(cmd, []).append(cap_key)

        cmd_results: Dict[str, dict] = {}
        for cmd in cmd_to_caps:
            # Skip commands already run as base commands
            if cmd in result["raw_outputs"]:
                cmd_results[cmd] = {
                    "success": True,
                    "output": result["raw_outputs"][cmd],
                    "error": None,
                }
                continue
            try:
                out = connection.send_command(cmd, read_timeout=read_timeout)
                cmd_results[cmd] = command_result_from_output(out)
                result["raw_outputs"][cmd] = out
            except Exception as e:
                cmd_results[cmd] = {"success": False, "output": "", "error": str(e)}
                result["errors"].append(f"{cmd}: {e}")
                logger.warning("[%s] capability command '%s' failed: %s", ip, cmd, e)

        # Evaluate each capability
        for cap_key in CAPABILITY_COMMANDS:
            if cap_key in matrix_settled:
                result["capabilities"][cap_key] = matrix_settled[cap_key]
                continue
            cmd = CAPABILITY_COMMANDS[cap_key]
            display_result = cmd_results.get(cmd, {"success": False, "output": "", "error": "not executed"})
            cap = _check_capability(cap_key, display_result, current_config)
            _try_alt_commands(cap_key, cap,
                              lambda c: cmd_results.get(c))
            result["capabilities"][cap_key] = cap
            _apply_matrix_override(cap_key, cap, matrix)

        # Run supplementary commands for enhanced detection
        for cap_key, supp_cmds in SUPPLEMENTARY_COMMANDS.items():
            if cap_key not in result["capabilities"]:
                continue
            cap_result = result["capabilities"][cap_key]
            for supp_cmd in supp_cmds:
                if supp_cmd in result["raw_outputs"]:
                    continue  # already executed
                try:
                    out = connection.send_command(supp_cmd, read_timeout=read_timeout)
                    result["raw_outputs"][supp_cmd] = out
                    # If main probe failed but supplementary succeeded, mark as supported
                    if not cap_result["supported"]:
                        if not is_output_unsupported(out):
                            cap_result["supported"] = True
                            cap_result["error"] = None
                    # Check configured from supplementary output
                    if not cap_result["configured"]:
                        supp_kws = CONFIG_KEYWORDS.get(cap_key, [])
                        if any(kw.lower() in out.lower() for kw in supp_kws):
                            cap_result["configured"] = True
                except Exception as e:
                    result["raw_outputs"][supp_cmd] = ""
                    logger.debug("[%s] supplementary command '%s' failed: %s", ip, supp_cmd, e)

        # ---- ? 命令树：实测证据，可推翻矩阵/探测的保守误判（只升不降）----
        try:
            views = _collect_views_from_result(result, connection=connection,
                                               read_timeout=read_timeout)
            if views:
                result["help_views"] = {k: sorted(v) for k, v in views.items()}
                _apply_tree_detection(result, views)
        except Exception as e:
            logger.debug("[%s] ? 命令树采集失败（不影响探测结果）: %s", ip, e)

        # ---- Parse interface list from display ip interface brief ----
        ip_intf_output = result["raw_outputs"].get("display ip interface brief", "")
        result["interfaces"] = _parse_interface_list(ip_intf_output, current_config)

        safe_disconnect(connection)
        logger.info("[%s] capability probe done: role=%s, capabilities=%d, interfaces=%d",
                    ip, result["device_role"], len(result["capabilities"]), len(result["interfaces"]))

    except ImportError:
        result["errors"].append("Console 连接器不可用")
    except Exception as e:
        err_name = type(e).__name__
        if "Timeout" in err_name:
            result["errors"].append(f"Console 连接超时: {e}")
        elif "Authentication" in err_name:
            result["errors"].append(f"Console 认证失败: {e}")
        else:
            result["errors"].append(str(e))
        logger.error("[%s] capability probe failed: %s", ip, e)
    finally:
        safe_disconnect(connection)

    return result

def probe_capabilities_from_cache(
    collection_result: dict,
    device: dict = None,
) -> Dict[str, Any]:
    """
    从已有采集结果中提取能力信息（零连接开销）。

    采集阶段已执行的 display 命令可直接用于能力判断：
      - display version        → supported、model、role
      - display device         → supported、role
      - display current-config → configured（所有协议）
      - display ospf peer      → ospf running
      - display vlan           → vlan running
      - display lldp brief     → lldp running
      - display ip routing-table → routing_table/static_route running
      - display ip int brief   → interface_ip running

    Returns:
        与 probe_capabilities 相同结构的结果字典。
        unknown_capabilities: 无法从缓存判断的能力列表（需要补跑探测）。
    """
    ip = collection_result.get("ip", "")
    device = device or {"ip": ip, "name": collection_result.get("name", ip)}

    result = {
        "ip": ip,
        "device_name": device.get("name", ip),
        "ok": collection_result.get("ok", False),
        "device_role": "unknown",
        "model": "",
        "platform": "",
        "version": "",
        "hostname": "",
        "capabilities": {},
        "raw_outputs": {},
        "errors": [],
        "unknown_capabilities": [],  # 无法从缓存判断的能力
    }

    if not collection_result.get("ok"):
        return result

    commands = collection_result.get("commands", {})
    info = collection_result.get("info", {})

    # 构建 raw_outputs（模拟 probe_capabilities 的格式）
    for cmd_key, cmd_data in commands.items():
        if isinstance(cmd_data, dict) and cmd_data.get("success"):
            result["raw_outputs"][cmd_key] = cmd_data.get("output", "")

    # 提取关键输出
    # 采集阶段用的命令可能带 | section 后缀，需要模糊匹配
    def _find_output(prefix):
        for key, val in commands.items():
            if key.startswith(prefix) and isinstance(val, dict) and val.get("success"):
                return val.get("output", "")
        return ""

    version_output = _find_output("display version")
    device_output = _find_output("display device")
    current_config = _find_output("display current-configuration")
    ip_intf_output = _find_output("display ip interface brief")
    vlan_output = _find_output("display vlan")
    ospf_peer_output = _find_output("display ospf peer")
    lldp_output = _find_output("display lldp neighbor brief")
    routing_output = _find_output("display ip routing-table")

    # 解析基础信息
    if version_output:
        model_match = re.search(r"(AR\d+|S\d+|NE\d+|CE\d+|USG\d+|LSW\d+|Router|Switch)\S*", version_output)
        result["model"] = model_match.group(0) if model_match else ""
        result["platform"] = f"Huawei {result['model']}" if result["model"] else "Huawei"

        ver_match = re.search(r"Version\s+([\d.]+\w*)", version_output)
        result["version"] = ver_match.group(1) if ver_match else ""

        hostname_match = re.search(r"<(\S+)>", version_output) or re.search(r"sysname\s+(\S+)", current_config)
        if hostname_match:
            result["hostname"] = hostname_match.group(1)

    # 设备角色分类
    result["device_role"] = _classify_device_role(version_output, device_output, current_config, result["model"])

    # 用已有的 display 输出构造 display_result 供 _check_capability 使用
    def _make_display_result(output):
        if output:
            return {"success": True, "output": output, "error": None}
        return {"success": False, "output": "", "error": "缓存无数据"}

    # 已有输出的能力映射
    cached_outputs = {
        "routing_table":  routing_output,
        "ospf":           ospf_peer_output,
        "vlan":           vlan_output,
        "stp":            "",       # display stp brief — 未采集
        "lldp":           lldp_output,
        "eth_trunk":      "",       # display eth-trunk — 未采集
        "interface_ip":   ip_intf_output,
        "access_port":    "",       # display interface brief — 未采集（注意不同于 ip interface brief）
        "trunk_port":     "",
        "vlanif":         ip_intf_output,
        "bgp":            "",       # display bgp peer — 未采集
        "isis":           "",       # display isis peer — 未采集
        "rip":            "",       # display rip — 未采集
        "static_route":   routing_output,
        "vrrp":           "",       # 未采集
        "acl":            "",       # 未采集
        "snmp":           "",       # 未采集
        "dhcp":           "",       # 未采集
        "nat":            "",       # 未采集
        "ssh":            "",       # 未采集
        "telnet":         "",       # 未采集
        "ipsec":          "",       # 未采集
        "ike":            "",       # 未采集
        "gre":            "",       # 未采集
        "vpn_instance":   "",       # 未采集
    }

    # 能力矩阵：镜像确定不支持的能力直接定论，不再要求补跑
    matrix = lookup_matrix(version_output)
    result["matrix"] = {k: v for k, v in matrix.items() if k != "protocols"}

    # 逐个评估能力
    for cap_key in CAPABILITY_COMMANDS:
        output = cached_outputs.get(cap_key, "")
        display_result = _make_display_result(output)
        cap = _check_capability(cap_key, display_result, current_config)
        _apply_matrix_override(cap_key, cap, matrix)
        result["capabilities"][cap_key] = cap

        # 判断是否需要补跑：
        # - 没有任何 display 输出
        # - 且配置关键字也没匹配到（configured=False）
        # - 且该能力不是"总是存在"的（routing_table 总是 configured=True）
        # - 且矩阵没有确定判定（矩阵 True/False 已定论 supported，无需补跑确认支持性）
        has_output = bool(output)
        has_config = cap.get("configured", False)
        always_present = cap_key in ("routing_table",)
        matrix_settled = matrix_support(matrix, cap_key) is not None

        if not has_output and not has_config and not always_present and not matrix_settled:
            result["unknown_capabilities"].append(cap_key)

    # 接口列表
    if ip_intf_output:
        result["interfaces"] = _parse_interface_list(ip_intf_output, current_config)
    else:
        result["interfaces"] = info.get("interfaces", [])

    result["probed_at"] = time.time()
    return result

def probe_capabilities_parallel(
    device: dict,
    timeout: int = 10,
    read_timeout: int = 10,
    collection_result: dict = None,
) -> Dict[str, Any]:
    """
    并行能力识别：先从缓存提取，再补跑缺失命令。

    策略：
      1. 如果有采集缓存 → probe_capabilities_from_cache 零连接 提取
      2. 如果 unknown_capabilities 为空 → 直接返回（零连接）
      3. 如果有缺失 → 只跑缺失能力对应的 display 命令

    Args:
        collection_result: 采集阶段的结果（可选），有缓存时大幅减少连接次数。
    """
    import threading

    ip = device["ip"]

    # ---- Step 1: 从缓存提取 ----
    if collection_result and collection_result.get("ok"):
        cached = probe_capabilities_from_cache(collection_result, device)
        unknown = cached.get("unknown_capabilities", [])

        if not unknown:
            # 所有能力都能从缓存判断，零连接
            logger.info("[%s] 能力识别完全命中缓存，零连接", ip)
            cached.pop("unknown_capabilities", None)
            return cached

        # 有缺失能力，只补跑缺失对应的命令
        logger.info("[%s] 缓存命中 %d/%d 能力，补跑 %d 个: %s",
                    ip,
                    len(cached["capabilities"]) - len(unknown),
                    len(cached["capabilities"]),
                    len(unknown),
                    ", ".join(unknown))

        # 找出缺失能力需要的 display 命令
        missing_cmds = set()
        for cap_key in unknown:
            cmd = CAPABILITY_COMMANDS.get(cap_key)
            if cmd:
                missing_cmds.add(cmd)
            # 补充命令
            for supp_cmd in SUPPLEMENTARY_COMMANDS.get(cap_key, []):
                missing_cmds.add(supp_cmd)
            # 多候选命令：主命令在该镜像不存在时备胎
            for alt_cmd in ALT_COMMANDS.get(cap_key, []):
                missing_cmds.add(alt_cmd)
        # ? 命令树证据源（一条命令拿全部 display 子命令）
        missing_cmds.add("display ?")

        # 也需要 display interface brief（access_port / trunk_port 需要）
        need_interface_brief = any(k in unknown for k in ("access_port", "trunk_port"))
        if need_interface_brief:
            missing_cmds.add("display interface brief")

        # 如果 supported 状态不确定（没有 display 输出），需要连设备确认
        # 过滤掉已经有输出的命令
        cmds_to_run = []
        for cmd in missing_cmds:
            # 检查是否已有此命令的输出
            has_output = False
            for key, val in collection_result.get("commands", {}).items():
                if key.startswith(cmd) and isinstance(val, dict) and val.get("success"):
                    has_output = True
                    break
            if not has_output:
                cmds_to_run.append(cmd)

        if not cmds_to_run:
            # 虽然有 unknown 能力，但所有需要的命令都有了
            cached.pop("unknown_capabilities", None)
            return cached

        # ---- Step 2: 补跑缺失命令 ----
        # 将缺失命令分成最多 3 组
        groups = [[], [], []]
        for i, cmd in enumerate(cmds_to_run):
            groups[i % 3].append(cmd)
        groups = [(f"G{i+1}", g) for i, g in enumerate(groups) if g]

        shared_results = {}
        cmd_lock = threading.Lock()

        from .device_connector import build_conn_params, import_console
        base_params = build_conn_params(device, timeout=timeout)

        def _run_group(group_name, cmd_list):
            ConnectHandler, _, _ = import_console()
            conn = None
            try:
                conn = ConnectHandler(**dict(base_params))
                for cmd in cmd_list:
                    try:
                        out = fast_send_command(conn, cmd, timeout=read_timeout)
                        with cmd_lock:
                            shared_results[cmd] = command_result_from_output(out)
                    except Exception as e:
                        with cmd_lock:
                            shared_results[cmd] = {"success": False, "output": "", "error": str(e)}
            except Exception as e:
                logger.warning("[%s] 补跑会话 %s 失败: %s", ip, group_name, e)
            finally:
                if conn:
                    try:
                        conn.disconnect()
                    except Exception:
                        pass

        threads = []
        for group_name, cmd_list in groups:
            t = threading.Thread(target=_run_group, args=(group_name, cmd_list), daemon=True)
            threads.append(t)
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=read_timeout + 10)

        # ---- Step 3: 合并缓存结果和补跑结果 ----
        matrix = lookup_matrix(
            cached["raw_outputs"].get("display version", ""))
        if "matrix" not in cached:
            cached["matrix"] = {k: v for k, v in matrix.items() if k != "protocols"}
        current_config = cached["raw_outputs"].get("display current-configuration", "")

        for cap_key in unknown:
            cmd = CAPABILITY_COMMANDS.get(cap_key)
            display_result = shared_results.get(cmd, {"success": False, "output": "", "error": "未补跑"})
            cap = _check_capability(cap_key, display_result, current_config)
            _try_alt_commands(cap_key, cap, lambda c: shared_results.get(c))
            _apply_matrix_override(cap_key, cap, matrix)
            cached["capabilities"][cap_key] = cap

        # ? 命令树证据（display ? 输出 → display 视图关键字）
        display_help_res = shared_results.get("display ?")
        if display_help_res and display_help_res.get("success"):
            views = {"display": parse_help_keywords(strip_ansi(display_help_res.get("output", "")))}
            cached["help_views"] = {k: sorted(v) for k, v in views.items()}
            _apply_tree_detection(cached, views)

        # 补充探测结果
        for cap_key, supp_cmds in SUPPLEMENTARY_COMMANDS.items():
            if cap_key not in unknown:
                continue
            cap_result = cached["capabilities"][cap_key]
            for supp_cmd in supp_cmds:
                supp_result = shared_results.get(supp_cmd)
                if supp_result and supp_result["success"]:
                    if not cap_result["supported"]:
                        if not is_output_unsupported(supp_result["output"]):
                            cap_result["supported"] = True
                            cap_result["error"] = None

        cached.pop("unknown_capabilities", None)
        cached["probed_at"] = time.time()
        return cached

    # ---- 无缓存：走完整的并行探测 ----
    result = {
        "ip": ip,
        "device_name": device.get("name", ip),
        "ok": False,
        "device_role": "unknown",
        "model": "",
        "platform": "",
        "version": "",
        "hostname": "",
        "capabilities": {},
        "raw_outputs": {},
        "errors": [],
    }

    # 去重：合并所有需要执行的命令
    # 候选命令不进首轮 wave：只有主命令被拒时才在第二轮懒执行，避免无效往返
    all_base_cmds = list(BASE_COMMANDS)
    all_cap_cmds = list(set(CAPABILITY_COMMANDS.values()))
    all_supp_cmds = list(set(cmd for cmds in SUPPLEMENTARY_COMMANDS.values() for cmd in cmds))
    all_cmds = list(dict.fromkeys(
        all_base_cmds + all_cap_cmds + all_supp_cmds + ["display ?"]))

    # 按预期耗时分 3 组
    group_a_cmds = [c for c in all_cmds if c in BASE_COMMANDS or c in (
        "display interface brief", "display ip interface brief", "display ?",
    )]
    group_b_cmds = [c for c in all_cmds if c in (
        "display ospf peer", "display bgp peer", "display isis peer",
        "display rip", "display vlan", "display stp brief",
        "display eth-trunk", "display lldp neighbor brief",
        "display ip routing-table",
    )]
    group_c_cmds = [c for c in all_cmds if c not in group_a_cmds and c not in group_b_cmds]

    groups = [("A", group_a_cmds), ("B", group_b_cmds), ("C", group_c_cmds)]

    # 构建连接参数
    from .device_connector import build_conn_params
    base_params = build_conn_params(device, timeout=timeout)

    # 验证 Console 可连接
    try:
        from .device_connector import import_console
        ConnectHandler, _, _ = import_console()
        test_conn = ConnectHandler(**base_params)
        test_conn.disconnect()
        result["ok"] = True
    except Exception as e:
        result["errors"].append(f"Console 连接失败: {e}")
        return result

    # 并行执行 3 组命令
    shared_results = {}
    cmd_lock = threading.Lock()

    def _run_group(group_name, cmd_list):
        from .device_connector import import_console
        ConnectHandler, _, _ = import_console()
        conn_params = dict(base_params)
        conn = None
        try:
            conn = ConnectHandler(**conn_params)
            for cmd in cmd_list:
                try:
                    out = fast_send_command(conn, cmd, timeout=read_timeout)
                    with cmd_lock:
                        shared_results[cmd] = command_result_from_output(out)
                except Exception as e:
                    with cmd_lock:
                        shared_results[cmd] = {"success": False, "output": "", "error": str(e)}
            conn.disconnect()
        except Exception as e:
            logger.warning("[%s] 能力探测会话 %s 失败: %s", ip, group_name, e)
            for cmd in cmd_list:
                with cmd_lock:
                    if cmd not in shared_results:
                        shared_results[cmd] = {"success": False, "output": "", "error": str(e)}
        finally:
            if conn:
                try:
                    conn.disconnect()
                except Exception:
                    pass

    threads = []
    for group_name, cmd_list in groups:
        if not cmd_list:
            continue
        t = threading.Thread(
            target=_run_group,
            args=(group_name, cmd_list),
            name=f"cap-{ip}-{group_name}",
            daemon=True,
        )
        threads.append(t)

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=read_timeout + 15)

    # 合并结果
    result["raw_outputs"] = shared_results
    result["ok"] = True

    # 提取关键输出
    version_output = shared_results.get("display version", {}).get("output", "")
    device_output = shared_results.get("display device", {}).get("output", "")
    current_config = shared_results.get("display current-configuration", {}).get("output", "")

    # 解析基础信息
    model_match = re.search(r"(AR\d+|S\d+|NE\d+|CE\d+|USG\d+|LSW\d+|Router|Switch)\S*", version_output)
    result["model"] = model_match.group(0) if model_match else ""
    result["platform"] = f"Huawei {result['model']}" if result["model"] else "Huawei"

    ver_match = re.search(r"Version\s+([\d.]+\w*)", version_output)
    result["version"] = ver_match.group(1) if ver_match else ""

    hostname_match = re.search(r"<(\S+)>", version_output) or re.search(r"sysname\s+(\S+)", current_config)
    if hostname_match:
        result["hostname"] = hostname_match.group(1)

    # 设备角色分类
    result["device_role"] = _classify_device_role(version_output, device_output, current_config, result["model"])

    # 评估每个能力
    matrix = lookup_matrix(version_output)
    result["matrix"] = {k: v for k, v in matrix.items() if k != "protocols"}
    for cap_key in CAPABILITY_COMMANDS:
        cmd = CAPABILITY_COMMANDS[cap_key]
        display_result = shared_results.get(cmd, {"success": False, "output": "", "error": "未执行"})
        cap = _check_capability(cap_key, display_result, current_config)
        _apply_matrix_override(cap_key, cap, matrix)
        result["capabilities"][cap_key] = cap

    # 第二轮懒执行：只对"主命令被拒且矩阵没把握"的协议补发候选命令
    alt_cmds = set()
    for cap_key, cap in result["capabilities"].items():
        if not cap.get("supported") and matrix_support(matrix, cap_key) is None:
            alt_cmds.update(ALT_COMMANDS.get(cap_key, []))
    alt_cmds -= set(shared_results)
    if alt_cmds:
        alt_groups = [[], [], []]
        for i, cmd in enumerate(sorted(alt_cmds)):
            alt_groups[i % 3].append(cmd)
        threads2 = []
        for group_name, cmd_list in (("A2", alt_groups[0]), ("B2", alt_groups[1]), ("C2", alt_groups[2])):
            if not cmd_list:
                continue
            threads2.append(threading.Thread(
                target=_run_group, args=(group_name, cmd_list),
                name=f"cap-{ip}-{group_name}", daemon=True))
        for t in threads2:
            t.start()
        for t in threads2:
            t.join(timeout=read_timeout + 15)

    for cap_key in list(result["capabilities"]):
        if result["capabilities"][cap_key].get("supported"):
            continue
        _try_alt_commands(cap_key, result["capabilities"][cap_key],
                          lambda c: shared_results.get(c))

    # ? 命令树证据（display ? 输出 → display 视图关键字）
    display_help_res = shared_results.get("display ?")
    if display_help_res and display_help_res.get("success"):
        views = {"display": parse_help_keywords(strip_ansi(display_help_res.get("output", "")))}
        result["help_views"] = {k: sorted(v) for k, v in views.items()}
        _apply_tree_detection(result, views)

    # 补充探测
    for cap_key, supp_cmds in SUPPLEMENTARY_COMMANDS.items():
        if cap_key not in result["capabilities"]:
            continue
        cap_result = result["capabilities"][cap_key]
        for supp_cmd in supp_cmds:
            supp_result = shared_results.get(supp_cmd)
            if supp_result and supp_result["success"]:
                if not cap_result["supported"]:
                    if not is_output_unsupported(supp_result["output"]):
                        cap_result["supported"] = True
                        cap_result["error"] = None

    # 接口列表
    ip_intf_output = shared_results.get("display ip interface brief", {}).get("output", "")
    if ip_intf_output:
        result["interfaces"] = _parse_interface_list(ip_intf_output, current_config)

    return result
