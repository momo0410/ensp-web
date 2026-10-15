"""
help_tree.py - 递归 ? 视图遍历（零副作用协议发现）
==================================================
VRP 的 `?` 只是显示帮助、不执行命令；单独一行的 `?`（或 `display ?`）
完全无副作用。利用这一点逐层询问设备"这里能敲什么"：

  1. 用户视图 `display ?`   → 列出全部 display 子命令（信息量最大，
                               ospf/bgp/mpls/pim/ipsec... 一条命令全暴露）
  2. 系统视图 `?`           → 顶层配置命令（ospf/bgp/mpls/l2tp/ipsec...）
  3. 接口视图 `?`           → 接口下命令（vrrp/nat/dhcp/eth-trunk...
                               这些协议在顶层 ? 看不到，是旧探针漏判主因）

注意：**绝不发送 `ospf 1 ?` 这类部分输入**——帮助显示后紧随的回车
会把不完整的命令真正执行掉（如创建空的 OSPF 进程）。所有 `?` 必须单独成行。

解析出的命令树与协议签名比对，得到设备实际支持的协议清单，
作为能力矩阵之外的第二证据源。
"""

import re
import logging
from typing import Dict, Set, List, Tuple, Optional, Any

logger = logging.getLogger("ensp-web.help_tree")

# ---------------------------------------------------------------------------
# VRP 帮助输出解析
# ---------------------------------------------------------------------------
_HELP_TOKEN_RE = re.compile(r"^[A-Za-z][\w\-/]*$")

def parse_help_keywords(text: str) -> Set[str]:
    """
    把 VRP `?` 帮助输出解析为关键字集合。

    输出形如（每行：>=2 空格 + 关键字 + >=2 空格 + 描述）：
         ospf                        Specify the OSPF configuration
         ip                          <group>
         <cr>
    """
    keywords: Set[str] = set()
    if not text:
        return keywords
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("<cr>"):
            continue
        token = re.split(r"\s{2,}", s, maxsplit=1)[0].strip()
        if not token or "<" in token or ">" in token:
            continue
        if _HELP_TOKEN_RE.match(token):
            keywords.add(token.lower())
    return keywords

def find_first_physical_interface(*sources: str) -> str:
    """从 display interface brief / 当前配置等文本中找第一个物理接口名。"""
    pattern = re.compile(
        r"\b(GigabitEthernet\d+(?:/\d+)+|Ethernet\d+(?:/\d+)+|"
        r"XGigabitEthernet\d+(?:/\d+)+|FortyGigE\d+(?:/\d+)+|"
        r"100GE\d+(?:/\d+)+|MEth\d+(?:/\d+)+)",
        re.IGNORECASE,
    )
    for text in sources:
        if not text:
            continue
        m = pattern.search(text)
        if m:
            return m.group(1)
    return ""

# ---------------------------------------------------------------------------
# 协议签名：协议 key -> [(视图 scope, 关键字)]
# scope: "display"=display ? 子命令 | "system"=系统视图顶层 | "interface"=接口视图
# 任一命中即认为设备支持该协议；排列顺序即证据优先级。
# ---------------------------------------------------------------------------
PROTOCOL_SIGNATURES: Dict[str, List[Tuple[str, str]]] = {
    "ospf":          [("display", "ospf"), ("system", "ospf")],
    "bgp":           [("display", "bgp"), ("system", "bgp")],
    "isis":          [("display", "isis"), ("system", "isis")],
    "rip":           [("display", "rip"), ("system", "rip")],
    "ipv6":          [("display", "ipv6"), ("system", "ipv6")],
    "vlan":          [("display", "vlan"), ("system", "vlan")],
    "stp":           [("display", "stp"), ("system", "stp")],
    "eth_trunk":     [("display", "eth-trunk"), ("interface", "eth-trunk")],
    "lldp":          [("display", "lldp"), ("system", "lldp")],
    "igmp_snooping": [("display", "igmp-snooping"), ("system", "igmp-snooping")],
    "vrrp":          [("interface", "vrrp"), ("display", "vrrp")],
    "acl":           [("display", "acl"), ("system", "acl")],
    "dhcp":          [("display", "dhcp"), ("system", "dhcp"), ("interface", "dhcp")],
    "nat":           [("display", "nat"), ("interface", "nat")],
    "snmp":          [("display", "snmp-agent"), ("system", "snmp-agent")],
    "ssh":           [("display", "ssh"), ("system", "stelnet"), ("system", "ssh")],
    "telnet":        [("display", "telnet"), ("system", "telnet")],
    "ntp":           [("display", "ntp-service"), ("display", "ntp"),
                      ("system", "ntp-service"), ("system", "ntp")],
    "igmp":          [("display", "igmp"), ("system", "igmp")],
    "pim":           [("display", "pim"), ("system", "pim"),
                      ("system", "multicast")],
    "ipsec":         [("display", "ipsec"), ("system", "ipsec")],
    "ike":           [("display", "ike"), ("system", "ike")],
    "l2tp":          [("display", "l2tp"), ("system", "l2tp")],
    "pppoe":         [("display", "pppoe-server"), ("display", "pppoe-client"),
                      ("display", "pppoe"), ("system", "pppoe-server")],
    "mpls":          [("display", "mpls"), ("system", "mpls")],
    "mpls_ldp":      [("display", "mpls-ldp"), ("display", "ldp"),
                      ("system", "mpls-ldp")],
    "gre":           [("display", "tunnel-interface")],
    "vpn_instance":  [("display", "ip-vpn-instance")],
    "static_route":  [],
    "routing_table": [],
    "interface_ip":  [],
    "access_port":   [("interface", "port")],
    "trunk_port":    [],
    "vlanif":        [],
}

def detect_protocols(views: Dict[str, Set[str]]) -> Dict[str, str]:
    """
    用协议签名比对命令树，返回 {协议key: 证据描述}。

    证据描述形如 "display:ospf" / "interface:vrrp"。
    """
    detected: Dict[str, str] = {}
    for key, sigs in PROTOCOL_SIGNATURES.items():
        for scope, keyword in sigs:
            keywords = views.get(scope)
            if keywords and keyword in keywords:
                detected[key] = f"{scope}:{keyword}"
                break
    return detected

# ---------------------------------------------------------------------------
# 视图采集：基于已建立的设备连接
# ---------------------------------------------------------------------------
def collect_views_over_connection(
    connection: Any,
    read_timeout: int = 4,
    interface_name: str = "",
    deadline_s: float = 10.0,
) -> Tuple[Dict[str, Set[str]], List[str]]:
    """在已建立的设备连接上采集三层视图的关键字。"

    Args:
        deadline_s: 整体时间预算（秒）。每一步之前检查，超预算立即带已有
            结果返回，保证本函数对整次探测的延迟增量有硬上限。

    Returns:
        (views, errors) — views: {"display": set, "system": set,
        "interface": set}；errors: 采集过程中的非致命错误。
    """
    import time as _time

    from .device_connector import ask_help_timing, strip_ansi

    views: Dict[str, Set[str]] = {}
    errors: List[str] = []
    deadline = _time.monotonic() + deadline_s

    def _budget_left() -> bool:
        return _time.monotonic() < deadline

    def _ask(cmd: str, scope: str):
        try:
            out = ask_help_timing(connection, cmd, read_timeout=read_timeout)
            views[scope] = parse_help_keywords(strip_ansi(out or ""))
        except Exception as e:
            errors.append(f"{cmd}: {e}")
            logger.warning("help_tree 采集 '%s' 失败: %s", cmd, e)

    try:
        connection.send_command_timing("screen-length 0 temporary",
                                       read_timeout=read_timeout)
    except Exception:
        pass

    # 1) display ? —— display 子命令全集（信息量最大，最先采）
    if _budget_left():
        _ask("display ?", "display")

    # 2) 系统视图 ?
    if _budget_left():
        try:
            connection.send_command_timing("system-view", read_timeout=read_timeout)
            _ask("?", "system")
        except Exception as e:
            errors.append(f"system-view: {e}")

        # 3) 接口视图 ?（进入已有物理接口无副作用）
        if interface_name and _budget_left():
            try:
                connection.send_command_timing(f"interface {interface_name}",
                                               read_timeout=read_timeout)
                _ask("?", "interface")
            except Exception as e:
                errors.append(f"interface {interface_name}: {e}")
            try:
                connection.send_command_timing("quit", read_timeout=read_timeout)
            except Exception:
                pass

        try:
            connection.send_command_timing("return", read_timeout=read_timeout)
        except Exception:
            pass

    return views, errors

# ---------------------------------------------------------------------------
# 视图采集：Console（raw socket，复用 command_probe 的底层收发）
# ---------------------------------------------------------------------------
def collect_views_console(target: dict, read_timeout: float = 4.0) -> Tuple[Dict[str, Set[str]], List[str]]:
    """通过 eNSP 本地 Console 端口采集视图关键字（零 IP 设备用）。"""
    from .command_probe import _normalize_target, _enter_system_view, open_console_session
    from .device_connector import strip_ansi

    t = _normalize_target(target)
    port = t.get("port")
    views: Dict[str, Set[str]] = {}
    errors: List[str] = []
    if not port:
        return views, ["console: target 缺少 console_port / port"]
    conn = None
    try:
        conn = open_console_session(port)
        conn.send_command("screen-length 0 temporary", read_timeout=read_timeout)

        out = conn.send_command("display ?", read_timeout=read_timeout)
        views["display"] = parse_help_keywords(strip_ansi(out))

        if _enter_system_view(conn, read_timeout):
            out = conn.send_command("?", read_timeout=read_timeout)
            views["system"] = parse_help_keywords(strip_ansi(out))

            interface_name = (t.get("interface") or "").strip()
            if interface_name:
                conn.send_command(f"interface {interface_name}", read_timeout=read_timeout)
                out = conn.send_command("?", read_timeout=read_timeout)
                views["interface"] = parse_help_keywords(strip_ansi(out))
                conn.send_command("quit", read_timeout=read_timeout)
            conn.send_command("return", read_timeout=read_timeout)
        else:
            errors.append("console: 未能进入系统视图")
    except Exception as e:
        errors.append(f"console: {e}")
    finally:
        if conn:
            try:
                conn.disconnect()
            except Exception:
                pass
    return views, errors

# ---------------------------------------------------------------------------
# 结果组装：版本 → 矩阵 → 签名比对 → 协议判定（Console 通道）
# ---------------------------------------------------------------------------
def compose_discovery(version_output: str,
                      views: Dict[str, Set[str]],
                      errors: Optional[List[str]] = None,
                      interface_name: str = "") -> Dict[str, Any]:
    """把版本输出和 ? 视图关键字组装成完整的协议发现结果。"""
    from .capability_matrix import lookup_matrix, PROTOCOL_NAMES

    matrix = lookup_matrix(version_output)
    detected = detect_protocols(views)

    protocols = {}
    for key in PROTOCOL_NAMES:
        if key in detected:
            protocols[key] = {
                "name": PROTOCOL_NAMES[key],
                "supported": True,
                "evidence": f"?树 {detected[key]}",
            }
        else:
            ms = matrix.get("protocols", {}).get(key, {})
            protocols[key] = {
                "name": PROTOCOL_NAMES[key],
                "supported": ms.get("supported"),
                "evidence": "矩阵" if ms.get("supported") is not None else "未发现",
                "note": ms.get("note", ""),
            }

    return {
        "model": matrix.get("model", ""),
        "image": matrix.get("image", ""),
        "vrp_version": matrix.get("vrp_version", ""),
        "matrix": matrix,
        "views": {k: sorted(v) for k, v in views.items()},
        "view_sizes": {k: len(v) for k, v in views.items()},
        "protocols": protocols,
        "interface": interface_name,
        "errors": list(errors or []),
        "detected_count": len(detected),
    }

# ---------------------------------------------------------------------------
# 独立发现入口：自建设备连接，一次拿到 版本 + 命令树 + 矩阵 + 协议判定
# ---------------------------------------------------------------------------
def discover_via_connection(
    device: dict,
    timeout: int = 10,
    read_timeout: int = 6,
) -> dict:
    """连上设备做一次完整的协议发现（版本 → 矩阵 → ? 命令树 → 签名比对）。"""
    from .device_connector import safe_disconnect

    ip = device["ip"]
    result: Dict[str, Any] = {
        "ip": ip,
        "hostname": device.get("name", ip),
        "ok": False,
        "model": "",
        "image": "",
        "views": {},
        "view_sizes": {},
        "protocols": {},
        "matrix": None,
        "interface": "",
        "errors": [],
    }

    conn = None
    try:
        from .device_connector import import_console, build_conn_params
        ConnectHandler, _, _ = import_console()
        conn = ConnectHandler(**build_conn_params(device, timeout=timeout))
        result["ok"] = True

        try:
            version = conn.send_command("display version", read_timeout=read_timeout)
        except Exception as e:
            version = ""
            result["errors"].append(f"display version: {e}")

        # 接口名：优先调用方指定，否则从 display interface brief 找
        interface_name = (device.get("probe_interface") or "").strip()
        brief_output = ""
        if not interface_name:
            try:
                brief_output = conn.send_command_timing("display interface brief",
                                                        read_timeout=read_timeout)
            except Exception:
                brief_output = ""
            interface_name = find_first_physical_interface(brief_output)

        views, errors = collect_views_over_connection(
            conn, read_timeout=read_timeout,
            interface_name=interface_name,
        )
        result.update(compose_discovery(version, views, errors,
                                        interface_name=interface_name))
        logger.info("[%s] ? 树发现协议 %d 项，视图关键字 %s",
                    ip, result.get("detected_count", 0), result.get("view_sizes"))
    except Exception as e:
        result["errors"].append(f"{type(e).__name__}: {e}")
        logger.error("[%s] help_tree 发现失败: %s", ip, e)
    finally:
        safe_disconnect(conn)
    return result
