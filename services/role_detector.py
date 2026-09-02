"""
role_detector.py - 设备协议角色解析
====================================
回答"这台设备在每个协议里扮演什么角色"：

    VRRP   → Master / Backup（哪个接口哪个 VRID）
    STP    → 根桥 / 有根端口 / 模式
    DHCP   → Server / 中继 / Snooping
    OSPF   → 进程号 / 区域归属 / Full 邻居数
    NAT    → Easy-IP / 静态 / Server（应用在哪个接口）
    ACL    → 基础/高级，应用在哪个接口哪个方向
    ...

输入是**已有的能力探测结果**（capability_probe 的输出 dict，含
raw_outputs / current-configuration / interfaces），纯字符串解析，
零 SSH、零延迟。每个协议同时给出「填空配置」入口：

    config_key - config_builder 模板键（前端弹窗表单直接打开）
    presets    - 可自动推导的字段预填值（接口/IP/网段等）
    preset_hints - 给用户看的预填说明
"""

import re
import time
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger("ensp-web.role_detector")

PROTOCOL_ROLE_NAMES: Dict[str, str] = {
    "vrrp": "VRRP", "ospf": "OSPF", "bgp": "BGP", "isis": "IS-IS",
    "rip": "RIP", "static_route": "静态路由", "routing_table": "路由表",
    "vlan": "VLAN", "access_port": "Access 接口", "trunk_port": "Trunk 接口",
    "vlanif": "VLANIF", "stp": "STP", "eth_trunk": "Eth-Trunk",
    "lldp": "LLDP", "interface_ip": "接口 IP", "acl": "ACL",
    "dhcp": "DHCP", "nat": "NAT", "snmp": "SNMP", "ssh": "SSH",
    "telnet": "Telnet", "ntp": "NTP", "igmp": "IGMP", "pim": "PIM",
    "ipsec": "IPsec", "ike": "IKE", "gre": "GRE", "vpn_instance": "VPN 实例",
    "mpls": "MPLS", "mpls_ldp": "MPLS LDP", "l2tp": "L2TP", "pppoe": "PPPoE",
}

# 协议 -> config_builder 模板键（填空弹窗直接打开；None 表示暂无模板）
PROTOCOL_CONFIG_KEYS: Dict[str, Optional[str]] = {
    "vrrp": "vrrp", "ospf": "ospf", "bgp": "bgp", "isis": "isis",
    "rip": "rip", "static_route": "static_route", "vlan": "vlan",
    "access_port": "access_port", "trunk_port": "trunk_port",
    "vlanif": "vlanif", "stp": "stp", "eth_trunk": "eth_trunk",
    "lldp": "lldp", "interface_ip": "interface_ip", "acl": "acl",
    "dhcp": "dhcp", "nat": "nat", "snmp": "snmp", "ssh": "ssh_user",
    "telnet": "telnet", "ipsec": "ipsec", "ike": "ike", "gre": "gre",
    "vpn_instance": "vpn_instance",
    "routing_table": None, "ntp": None, "igmp": None, "pim": None,
    "mpls": None, "mpls_ldp": None, "l2tp": None, "pppoe": None,
}

_IP_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


# ---------------------------------------------------------------------------
# 基础解析工具
# ---------------------------------------------------------------------------
def _interface_blocks(current_config: str) -> List[Dict[str, str]]:
    """把 current-config 按 interface 段落切开：[{name, body}]。"""
    blocks: List[Dict[str, str]] = []
    name, body = None, []
    for line in (current_config or "").splitlines():
        m = re.match(r"^\s*interface\s+(\S+)", line)
        if m:
            if name:
                blocks.append({"name": name, "body": "\n".join(body)})
            name, body = m.group(1), []
        elif name is not None:
            if re.match(r"^\s*#\s*$", line) or re.match(r"^\s*return\s*$", line):
                blocks.append({"name": name, "body": "\n".join(body)})
                name, body = None, []
            else:
                body.append(line)
    if name:
        blocks.append({"name": name, "body": "\n".join(body)})
    return blocks


def _find_intfs(interfaces: Optional[List[dict]], *prefixes: str) -> List[dict]:
    out = []
    for intf in interfaces or []:
        if intf.get("name", "").lower().startswith(tuple(p.lower() for p in prefixes)):
            out.append(intf)
    return out


def _best_router_id(interfaces: Optional[List[dict]]) -> str:
    """LoopBack 优先，否则取最大的接口 IP 作为建议 Router ID。"""
    with_ip = [i for i in (interfaces or []) if i.get("ip")]
    loopbacks = [i for i in with_ip if i["name"].lower().startswith("loopback")]
    pool = loopbacks or with_ip
    if not pool:
        return ""
    try:
        best = max(pool, key=lambda i: tuple(int(x) for x in i["ip"].split(".")[:4]))
        return best["ip"]
    except (ValueError, KeyError):
        return pool[0]["ip"]


def _suggest_virtual_ip(ip: str) -> str:
    """同网段 .254 作为建议虚拟 IP。"""
    if ip and ip.count(".") == 3:
        return ip.rsplit(".", 1)[0] + ".254"
    return ""


def _roles(cap_entry: Optional[dict]) -> str:
    """从能力三态归纳展示状态。"""
    if not cap_entry:
        return "unknown"
    if cap_entry.get("running"):
        return "running"
    if cap_entry.get("configured"):
        return "configured"
    if cap_entry.get("supported"):
        return "supported"
    return "unsupported"


def _role(role: str, detail: str = "", interface: str = "") -> dict:
    return {"role": role, "detail": detail, "interface": interface}


# ---------------------------------------------------------------------------
# 各协议角色解析（输入均为已有探测输出）
# ---------------------------------------------------------------------------
def _detect_vrrp(cfg: str, raw: Dict[str, str], interfaces: List[dict]) -> List[dict]:
    roles = []
    brief = raw.get("display vrrp brief", "") or ""
    # Interface VRID Virtual-IP State ...
    for m in re.finditer(r"^(\S+)\s+(\d+)\s+(\d+\.\d+\.\d+\.\d+)\s+(\w+)", brief, re.M):
        roles.append(_role(m.group(4), f"VRID {m.group(2)} 虚IP {m.group(3)}",
                           m.group(1)))
    if not roles:
        for blk in _interface_blocks(cfg):
            for m in re.finditer(r"vrrp vrid (\d+) virtual-ip (\S+)", blk["body"]):
                roles.append(_role("已配置", f"VRID {m.group(1)} 虚IP {m.group(2)}",
                                   blk["name"]))
    return roles


def _detect_stp(cfg: str, raw: Dict[str, str]) -> List[dict]:
    roles = []
    m = re.search(r"stp mode (\w+)", cfg, re.I)
    if m:
        roles.append(_role(f"{m.group(1).upper()} 模式", "stp mode " + m.group(1)))
    brief = raw.get("display stp brief", "") or ""
    root = alte = desi = 0
    for line in brief.splitlines():
        cols = line.split()
        if len(cols) >= 3:
            for c in cols:
                if c == "ROOT":
                    root += 1
                elif c == "ALTE":
                    alte += 1
                elif c == "DESI":
                    desi += 1
    if root:
        roles.append(_role("非根桥", f"{root} 个根端口，转发到根桥"))
    elif desi:
        roles.append(_role("疑似根桥", f"全部 {desi} 个端口为指定端口（DESI）"))
    if alte:
        roles.append(_role("有阻塞链路", f"{alte} 个备用端口（ALTE）处于阻塞"))
    return roles


def _detect_dhcp(cfg: str) -> List[dict]:
    roles = []
    if re.search(r"^dhcp enable", cfg, re.M):
        pools = re.findall(r"^ip pool (\S+)", cfg, re.M)
        if pools:
            roles.append(_role("Server", f"地址池: {', '.join(pools)}"))
        if re.search(r"dhcp select relay", cfg, re.I):
            roles.append(_role("中继", "接口下 dhcp select relay"))
        if re.search(r"dhcp snooping enable", cfg, re.I):
            roles.append(_role("Snooping", "防私接 DHCP Server"))
        if not pools and not re.search(r"dhcp select", cfg, re.I):
            roles.append(_role("已启用", "全局 dhcp enable，未建地址池"))
    return roles


def _detect_nat(cfg: str) -> List[dict]:
    roles = []
    for blk in _interface_blocks(cfg):
        if re.search(r"nat outbound", blk["body"]):
            roles.append(_role("Easy-IP / 出方向", "nat outbound", blk["name"]))
        if re.search(r"nat server ", blk["body"]):
            roles.append(_role("NAT Server", "对外发布内网服务", blk["name"]))
    if re.search(r"^nat static ", cfg, re.M):
        roles.append(_role("静态 NAT", "nat static 一对一转换"))
    return roles


def _detect_acl(cfg: str) -> List[dict]:
    roles = []
    for m in re.finditer(r"^acl number (\d+)", cfg, re.M):
        num = int(m.group(1))
        kind = "基础 ACL" if num < 3000 else "高级 ACL"
        roles.append(_role(kind, f"acl number {num}"))
    for blk in _interface_blocks(cfg):
        for m in re.finditer(r"traffic-filter (inbound|outbound) acl (\d+)", blk["body"]):
            roles.append(_role("流量过滤", f"{m.group(1)} 方向引用 acl {m.group(2)}",
                               blk["name"]))
    return roles


def _detect_ospf(cfg: str, raw: Dict[str, str]) -> List[dict]:
    roles = []
    m = re.search(r"^ospf (\d+)", cfg, re.M)
    if m:
        roles.append(_role("进程 " + m.group(1), "ospf " + m.group(1)))
        rid = re.search(r"router-id (\d+\.\d+\.\d+\.\d+)", cfg)
        if rid:
            roles.append(_role("Router ID", rid.group(1)))
        areas = sorted(set(re.findall(r"^\s+area ([\d.]+)", cfg, re.M)))
        if areas:
            label = "骨干区域" if "0.0.0.0" in areas else "普通区域"
            roles.append(_role(label, "Area: " + ", ".join(areas)))
    peers = raw.get("display ospf peer", "") or ""
    full = len(re.findall(r"Full", peers))
    if full:
        roles.append(_role("邻居正常", f"{full} 个 Full 邻居"))
    elif m and peers and "neighbor" in peers.lower():
        roles.append(_role("无邻居", "进程运行但尚无 Full 邻居"))
    return roles


def _detect_bgp(cfg: str, raw: Dict[str, str]) -> List[dict]:
    roles = []
    m = re.search(r"^bgp (\d+)", cfg, re.M)
    if m:
        roles.append(_role(f"AS {m.group(1)}", "bgp " + m.group(1)))
        peers = raw.get("display bgp peer", "") or ""
        est = len(re.findall(r"Established", peers))
        roles.append(_role("邻居状态", f"{est} 个 Established 邻居" if est
                           else "无 Established 邻居"))
    return roles


def _detect_static_route(cfg: str) -> List[dict]:
    routes = re.findall(r"^ip route-static \S+", cfg, re.M)
    if routes:
        return [_role("静态出口", f"{len(routes)} 条静态路由")]
    return []


def _detect_vlan(cfg: str) -> List[dict]:
    roles = []
    m = re.search(r"vlan batch (.+)", cfg)
    if m:
        roles.append(_role("批量创建", "vlan batch " + m.group(1).strip()))
    vlans = set(re.findall(r"^vlan (\d+)", cfg, re.M))
    if vlans:
        roles.append(_role("VLAN 划分", f"{len(vlans)} 个 VLAN: "
                           + ", ".join(sorted(vlans, key=int)[:8])))
    acc = len(re.findall(r"port link-type access", cfg))
    trk = len(re.findall(r"port link-type trunk", cfg))
    if acc:
        roles.append(_role("Access 端口", f"{acc} 个"))
    if trk:
        roles.append(_role("Trunk 端口", f"{trk} 个"))
    return roles


def _detect_eth_trunk(cfg: str) -> List[dict]:
    roles = []
    for blk in _interface_blocks(cfg):
        if blk["name"].lower().startswith("eth-trunk"):
            mode = re.search(r"mode (\S+)", blk["body"])
            roles.append(_role("LACP 模式" if mode and "lacp" in mode.group(1)
                               else "手工负载", blk["name"], blk["name"]))
    return roles


def _detect_snmp(cfg: str) -> List[dict]:
    roles = []
    comms = re.findall(r"snmp-agent community (read|write) (\S+)", cfg)
    if comms:
        roles.append(_role("v2c", f"{', '.join(c for _, c in comms)}"))
    if re.search(r"snmp-agent usm-user", cfg):
        roles.append(_role("v3", "USM 用户"))
    return roles


def _detect_ntp(cfg: str) -> List[dict]:
    roles = []
    servers = re.findall(r"ntp-service unicast-server (\d+\.\d+\.\d+\.\d+)", cfg)
    if servers:
        roles.append(_role("客户端", "同步源: " + ", ".join(servers)))
    if re.search(r"ntp-service refclock-master", cfg):
        roles.append(_role("服务端", "本机为时钟源"))
    return roles


def _detect_ssh(cfg: str) -> List[dict]:
    roles = []
    if re.search(r"stelnet server enable", cfg):
        roles.append(_role("SSH 服务端", "stelnet server enable"))
    users = re.findall(r"^ssh user (\S+)", cfg, re.M)
    if users:
        roles.append(_role("认证用户", ", ".join(users)))
    return roles


def _detect_telnet(cfg: str) -> List[dict]:
    if re.search(r"telnet server enable", cfg):
        vty = re.search(r"user-interface vty (\d+ \d+)", cfg)
        return [_role("Telnet 服务端",
                      f"VTY {vty.group(1)}" if vty else "telnet server enable")]
    return []


def _detect_gre(cfg: str) -> List[dict]:
    roles = []
    for blk in _interface_blocks(cfg):
        if re.match(r"Tunnel\d+", blk["name"]) and re.search(
                r"tunnel-protocol gre", blk["body"], re.I):
            src = re.search(r"tunnel source (\S+)", blk["body"])
            dst = re.search(r"tunnel destination (\S+)", blk["body"])
            roles.append(_role("隧道端点",
                               f"source {src.group(1) if src else '?'} → "
                               f"{dst.group(1) if dst else '?'}",
                               blk["name"]))
    return roles


def _detect_ipsec(cfg: str) -> List[dict]:
    roles = []
    for m in re.finditer(r"^ipsec policy (\S+)", cfg, re.M):
        roles.append(_role("安全策略", f"ipsec policy {m.group(1)}"))
    for blk in _interface_blocks(cfg):
        if re.search(r"ipsec policy ", blk["body"]):
            roles.append(_role("保护接口", "接口引用 ipsec policy", blk["name"]))
    return roles


def _detect_vpn_instance(cfg: str) -> List[dict]:
    names = re.findall(r"^ip vpn-instance (\S+)", cfg, re.M)
    if names:
        return [_role("VRF", f"{len(names)} 个实例: {', '.join(names)}")]
    return []


def _detect_mpls_ldp(cfg: str) -> List[dict]:
    roles = []
    if re.search(r"^mpls lsr-id", cfg, re.M):
        roles.append(_role("LSR ID", re.search(r"mpls lsr-id (\S+)", cfg).group(1)))
    ldp_intfs = [b["name"] for b in _interface_blocks(cfg)
                 if re.search(r"mpls ldp", b["body"])]
    if ldp_intfs:
        roles.append(_role("LDP 使能", f"{len(ldp_intfs)} 个接口",
                           ldp_intfs[0]))
    return roles


# ---------------------------------------------------------------------------
# 预填值推导（填空配置的"傻瓜"部分）
# ---------------------------------------------------------------------------
def _build_presets(key: str, cfg: str, interfaces: List[dict]) -> (Dict[str, str], List[str]):
    """从设备上下文推导模板字段预填值。只填有把握的，其余留给用户。"""
    presets: Dict[str, str] = {}
    hints: List[str] = []

    def _ip_intf():
        pool = [i for i in interfaces if i.get("ip")] or _find_intfs(
            interfaces, "Vlanif", "GigabitEthernet", "Ethernet")
        return pool[0] if pool else None

    if key == "vrrp":
        intf = _ip_intf()
        if intf:
            presets["interface"] = intf["name"]
            vip = _suggest_virtual_ip(intf["ip"])
            if vip:
                presets["virtual_ip"] = vip
            hints.append(f"已选接口 {intf['name']}"
                         + (f"（当前 IP {intf['ip']}，虚拟 IP 建议 {vip}）" if vip else ""))
    elif key == "ospf":
        presets["process_id"] = (re.search(r"^ospf (\d+)", cfg, re.M).group(1)
                                 if re.search(r"^ospf (\d+)", cfg, re.M) else "1")
        rid = _best_router_id(interfaces)
        if rid:
            presets["router_id"] = rid
            hints.append(f"Router ID 建议 {rid}（取自现有接口 IP）")
    elif key == "dhcp":
        intf = _ip_intf()
        if intf and intf.get("ip"):
            presets["gateway"] = intf["ip"]
            presets["network"] = intf["ip"].rsplit(".", 1)[0] + ".0"
            presets["mask"] = "255.255.255.0"
            hints.append(f"按 {intf['name']}（{intf['ip']}）所在网段预填网关/网段")
    elif key == "nat":
        for blk in _interface_blocks(cfg):
            m = re.search(r"nat outbound (\d+)", blk["body"])
            if m:
                presets.update({"nat_type": "outbound", "acl_number": m.group(1),
                                "interface": blk["name"]})
                hints.append(f"沿用现有出接口 {blk['name']} 与 ACL {m.group(1)}")
                break
        else:
            out = _find_intfs(interfaces, "GigabitEthernet", "Ethernet")
            out = [i for i in out if not i.get("ip")]
            if out:
                presets["interface"] = out[0]["name"]
                hints.append(f"出接口建议 {out[0]['name']}（当前未配 IP，通常为公网口）")
    elif key == "acl":
        m = re.search(r"^acl number (\d{4})", cfg, re.M)
        presets["acl_number"] = str(int(m.group(1)) + 1 if m else 3000)
        if m:
            hints.append(f"ACL 编号建议 {presets['acl_number']}（现有 {m.group(1)} 的下一个）")
    elif key == "interface_ip":
        free = [i for i in interfaces if not i.get("ip")
                and not i["name"].lower().startswith(("null", "inloop", "vlanif"))]
        if free:
            presets["interface"] = free[0]["name"]
            hints.append(f"已选未配 IP 的接口 {free[0]['name']}")
    elif key == "stp":
        m = re.search(r"stp mode (\w+)", cfg, re.I)
        if m:
            presets["mode"] = m.group(1)
    elif key == "vlanif":
        m = re.search(r"^vlan (\d+)", cfg, re.M)
        if m:
            presets["interface"] = f"Vlanif{m.group(1)}"
            hints.append(f"已选现有 VLAN {m.group(1)} 对应的 Vlanif")
    return presets, hints


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def detect_protocol_roles(cap_result: dict) -> dict:
    """
    从能力探测结果解析每协议角色。

    Args:
        cap_result: probe_capabilities* 的返回（含 raw_outputs/capabilities/
            interfaces），或 main.py state["capability_results"][ip]。

    Returns:
        {"ip", "hostname", "device_role", "protocols": {key: {...}}, ...}
    """
    raw = cap_result.get("raw_outputs") or {}
    cfg = raw.get("display current-configuration", "") or ""
    interfaces = cap_result.get("interfaces") or []
    caps = cap_result.get("capabilities") or {}

    parsers = {
        "vrrp": lambda: _detect_vrrp(cfg, raw, interfaces),
        "stp": lambda: _detect_stp(cfg, raw),
        "dhcp": lambda: _detect_dhcp(cfg),
        "nat": lambda: _detect_nat(cfg),
        "acl": lambda: _detect_acl(cfg),
        "ospf": lambda: _detect_ospf(cfg, raw),
        "bgp": lambda: _detect_bgp(cfg, raw),
        "static_route": lambda: _detect_static_route(cfg),
        "vlan": lambda: _detect_vlan(cfg),
        "eth_trunk": lambda: _detect_eth_trunk(cfg),
        "snmp": lambda: _detect_snmp(cfg),
        "ntp": lambda: _detect_ntp(cfg),
        "ssh": lambda: _detect_ssh(cfg),
        "telnet": lambda: _detect_telnet(cfg),
        "gre": lambda: _detect_gre(cfg),
        "ipsec": lambda: _detect_ipsec(cfg),
        "vpn_instance": lambda: _detect_vpn_instance(cfg),
        "mpls_ldp": lambda: _detect_mpls_ldp(cfg),
    }

    protocols: Dict[str, dict] = {}
    for key, name in PROTOCOL_ROLE_NAMES.items():
        cap_entry = caps.get(key)
        status = _roles(cap_entry)
        if status == "unsupported":
            continue  # 矩阵/探测确定不支持：角色视图直接不显示

        roles = parsers[key]() if key in parsers else []
        config_key = PROTOCOL_CONFIG_KEYS.get(key)
        presets, hints = ({}, [])
        if config_key:
            presets, hints = _build_presets(key, cfg, interfaces)

        protocols[key] = {
            "name": name,
            "status": status,
            "roles": roles,
            "config_key": config_key,
            "presets": presets,
            "preset_hints": hints,
        }

    return {
        "ip": cap_result.get("ip", ""),
        "hostname": cap_result.get("hostname") or cap_result.get("device_name", ""),
        "device_role": cap_result.get("device_role", "unknown"),
        "protocols": protocols,
        "generated_at": time.time(),
    }
