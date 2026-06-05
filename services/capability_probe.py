"""
capability_probe.py - 设备能力识别模块
=======================================
通过 SSH 登录华为设备，执行 display 命令，识别：
- 设备类型（router / switch / l3_switch / unknown）
- 支持的协议和功能（supported / configured / running）
"""

import re
import logging
from typing import Dict, Any, Optional

from .device_connector import (
    import_netmiko,
    build_conn_params,
    safe_disconnect,
    run_display_command,
    is_unsupported_error,
    is_output_unsupported,
)

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
    }


def probe_capabilities(
    device: dict,
    source_ip: Optional[str] = None,
    management_socket_factory=None,
    timeout: int = 10,
    read_timeout: int = 15,
) -> dict:
    """
    通过 SSH 连接设备并识别所有能力。

    Args:
        device: 设备字典
        source_ip: 管理源 IP（可选）
        management_socket_factory: socket 工厂函数
        timeout: SSH 超时
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
        "ssh_status": False,
        "capabilities": {},
        "raw_outputs": {},
        "errors": [],
    }

    connection = None
    ssh_sock = None

    try:
        ConnectHandler, NetMikoTimeoutException, NetMikoAuthenticationException = import_netmiko()
        params = build_conn_params(device, timeout=timeout)

        if source_ip and management_socket_factory:
            ssh_sock = management_socket_factory(ip, 22, timeout=timeout)
            params["sock"] = ssh_sock

        connection = ConnectHandler(**params)
        ssh_sock = None
        result["ssh_status"] = True

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

        # ---- Probe each capability ----
        # Group capabilities by their display command to avoid running same command twice
        cmd_to_caps: Dict[str, list] = {}
        for cap_key, cmd in CAPABILITY_COMMANDS.items():
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
                cmd_results[cmd] = {"success": True, "output": out, "error": None}
                result["raw_outputs"][cmd] = out
            except Exception as e:
                cmd_results[cmd] = {"success": False, "output": "", "error": str(e)}
                result["errors"].append(f"{cmd}: {e}")
                logger.warning("[%s] capability command '%s' failed: %s", ip, cmd, e)

        # Evaluate each capability
        for cap_key in CAPABILITY_COMMANDS:
            cmd = CAPABILITY_COMMANDS[cap_key]
            display_result = cmd_results.get(cmd, {"success": False, "output": "", "error": "not executed"})
            result["capabilities"][cap_key] = _check_capability(cap_key, display_result, current_config)

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

        safe_disconnect(connection)
        logger.info("[%s] capability probe done: role=%s, capabilities=%d",
                    ip, result["device_role"], len(result["capabilities"]))

    except ImportError:
        result["errors"].append("Netmiko 未安装")
    except Exception as e:
        err_name = type(e).__name__
        if "Timeout" in err_name:
            result["errors"].append(f"SSH 连接超时: {e}")
        elif "Authentication" in err_name:
            result["errors"].append(f"SSH 认证失败: {e}")
        else:
            result["errors"].append(str(e))
        logger.error("[%s] capability probe failed: %s", ip, e)
    finally:
        safe_disconnect(connection)
        if ssh_sock:
            try:
                ssh_sock.close()
            except OSError:
                pass

    return result
