"""
capability_matrix.py - eNSP 镜像版本 → 协议能力矩阵
====================================================
eNSP 使用固定且版本很老的镜像（S5700 是 V200R001C00，AR 系列是 V200R003C00），
每个镜像支持哪些协议是确定不变的。本模块从 display version 输出解析出
型号 + 镜像版本，查预置矩阵直接得到权威的协议支持清单。

矩阵取值：
    True  - 镜像确定支持该协议（权威判断，可覆盖运行时探测的误判）
    False - 镜像确定不支持该协议（直接跳过探测，避免 Unrecognized 误判噪音）
    None  - 不确定（交给运行时 display 探测 / ? 命令树确认）

判定优先级：help_tree 实测 > 矩阵 > display 命令探测。
矩阵只回答「支持与否」；configured / running 仍由配置和 display 输出判断。
"""

import re
from typing import Dict, Optional, Any

# ---------------------------------------------------------------------------
# 全量协议注册表：协议 key -> 中文名（矩阵 / ? 命令树 / display 探测共用）
# ---------------------------------------------------------------------------
PROTOCOL_NAMES: Dict[str, str] = {
    # 路由
    "static_route": "静态路由",
    "routing_table": "路由表",
    "rip": "RIP",
    "ospf": "OSPF",
    "isis": "IS-IS",
    "bgp": "BGP",
    "ipv6": "IPv6 基础",
    # 交换
    "vlan": "VLAN",
    "access_port": "Access 接口",
    "trunk_port": "Trunk 接口",
    "vlanif": "VLANIF",
    "stp": "STP/RSTP/MSTP",
    "eth_trunk": "Eth-Trunk",
    "lldp": "LLDP",
    "igmp_snooping": "IGMP Snooping",
    # 可靠性
    "vrrp": "VRRP",
    # 服务
    "interface_ip": "接口 IP",
    "acl": "ACL",
    "dhcp": "DHCP",
    "nat": "NAT",
    "snmp": "SNMP",
    "ssh": "SSH/STelnet",
    "telnet": "Telnet",
    "ntp": "NTP",
    # 组播
    "igmp": "IGMP",
    "pim": "PIM",
    # 安全 / VPN
    "ipsec": "IPsec VPN",
    "ike": "IKE",
    "gre": "GRE 隧道",
    "vpn_instance": "VPN 实例/VRF",
    "l2tp": "L2TP VPN",
    "pppoe": "PPPoE",
    "mpls": "MPLS",
    "mpls_ldp": "MPLS LDP",
}

# 前端分组展示顺序
PROTOCOL_GROUPS: Dict[str, list] = {
    "路由": ["static_route", "routing_table", "rip", "ospf", "isis", "bgp", "ipv6"],
    "交换": ["vlan", "access_port", "trunk_port", "vlanif", "stp", "eth_trunk",
             "lldp", "igmp_snooping"],
    "可靠性": ["vrrp"],
    "服务": ["interface_ip", "acl", "dhcp", "nat", "snmp", "ssh", "telnet", "ntp"],
    "组播": ["igmp", "pim"],
    "安全/VPN": ["ipsec", "ike", "gre", "vpn_instance", "l2tp", "pppoe", "mpls", "mpls_ldp"],
}

# 支持级别含义（写入 result 供前端提示）
_LEVEL_DOC = "True=支持 False=不支持 None=不确定(需实探)"


def _p(**kwargs) -> Dict[str, Any]:
    """矩阵协议条目简写：True/False/None → {supported, note}。"""
    return kwargs  # {key: (value, note)}


# ---------------------------------------------------------------------------
# 镜像矩阵：family -> 匹配规则 + 协议支持表
# note 说明为什么；None 表示该镜像上没有把握，留给运行时探测
# ---------------------------------------------------------------------------
# 简写：T=确定支持(附可选备注), F=确定不支持, None=不确定
def T(note: str = "") -> tuple:
    return (True, note)


def F(note: str = "") -> tuple:
    return (False, note)


def U(note: str = "") -> tuple:
    return (None, note)


_DEVICE_FAMILIES: Dict[str, Dict[str, Any]] = {
    # ---- AR 系列路由器（eNSP 镜像 VRP V200R003C00，部分旧包 V200R001/002 同档）----
    "ar_router": {
        "label": "AR 系列路由器",
        "match": re.compile(r"\bAR\d{2,4}\b", re.IGNORECASE),
        "protocols": {
            "static_route": T(), "routing_table": T(),
            "rip": T(), "ospf": T(), "isis": T(), "bgp": T(),
            "ipv6": T("VRP V200R003 支持基础 IPv6 / OSPFv3"),
            "acl": T(), "dhcp": T("支持 DHCP 服务器/中继"),
            "nat": T("Easy-IP / NAT Server / NAT Static"),
            "vrrp": T(), "snmp": T(), "ssh": T(), "telnet": T(), "ntp": T(),
            "ipsec": T(), "ike": T(), "gre": T(), "vpn_instance": T("MPLS L3VPN 可用"),
            "l2tp": T(), "pppoe": T("AR 支持 PPPoE 服务端/客户端"),
            "mpls": T("支持 MPLS 基础"), "mpls_ldp": T("支持 LDP，可用于 L3VPN 实验"),
            "igmp": T(), "pim": T("支持 PIM DM/SM"),
            "lldp": T(),
            # AR 基本无二层特性；AR201/AR1220 自带交换口，留给实探
            "vlan": U("AR201/AR1220 带交换口，其余型号不支持"),
            "access_port": U(), "trunk_port": U(), "vlanif": U(),
            "stp": F("无二层环路特性"), "eth_trunk": U("带交换口型号才有"),
            "igmp_snooping": U(),
            "interface_ip": T(),
        },
    },
    # ---- S5700（eNSP 镜像 V200R001C00，Routing Switch / HI）----
    "s5700": {
        "label": "S5700 三层交换机",
        "match": re.compile(r"\bS5700\b|\bS57\d0\b", re.IGNORECASE),
        "protocols": {
            "vlan": T(), "access_port": T(), "trunk_port": T(), "vlanif": T(),
            "stp": T(), "eth_trunk": T(), "lldp": T(), "igmp_snooping": T(),
            "interface_ip": T(), "acl": T(),
            "snmp": T(), "ssh": T(), "telnet": T(),
            "static_route": T("HI 版本支持静态路由"),
            "rip": T("HI 版本支持"),
            "ospf": T("HI 版本支持"),
            "vrrp": T("HI 版本支持"),
            "dhcp": T("支持 DHCP 服务器/中继/Snooping"),
            "routing_table": T(),
            "bgp": F("V200R001 镜像不含 BGP"),
            "isis": U("V200R001 HI 是否含 IS-IS 无把握，留实探"),
            "nat": F("交换机无 NAT"),
            "ipsec": F(), "ike": F(), "gre": F(), "vpn_instance": F(),
            "l2tp": F(), "pppoe": F(), "mpls": F(), "mpls_ldp": F(),
            "ipv6": U(), "ntp": U(),
            "igmp": U("V200R001 HI 三层组播无把握"), "pim": U(),
        },
    },
    # ---- S3700（eNSP 镜像 V100R006，纯二层）----
    "s3700": {
        "label": "S3700 二层交换机",
        "match": re.compile(r"\bS3700\b|\bS37\d0\b", re.IGNORECASE),
        "protocols": {
            "vlan": T(), "access_port": T(), "trunk_port": T(), "vlanif": T(),
            "stp": T(), "eth_trunk": T(), "lldp": T(), "igmp_snooping": T(),
            "interface_ip": T(), "acl": T(), "snmp": T(), "ssh": T(), "telnet": T(),
            "dhcp": U("V100R006 支持 Snooping/中继，服务器特性留实探"),
            "routing_table": U(),
            "static_route": F("纯二层，无路由协议"),
            "rip": F(), "ospf": F(), "isis": F(), "bgp": F(),
            "nat": F(), "vrrp": F(),
            "ipsec": F(), "ike": F(), "gre": F(), "vpn_instance": F(),
            "l2tp": F(), "pppoe": F(), "mpls": F(), "mpls_ldp": F(),
            "ipv6": U(), "ntp": U(), "igmp": F(), "pim": F(),
        },
    },
    # ---- USG 防火墙 / NE / CE（eNSP Pro），镜像差异大，只标确定项 ----
    "usg_fw": {
        "label": "USG 防火墙",
        "match": re.compile(r"\bUSG\d+\b", re.IGNORECASE),
        "protocols": {
            "static_route": T(), "routing_table": T(), "interface_ip": T(),
            "acl": T(), "nat": T(), "snmp": T(), "ssh": T(), "telnet": T(),
            "ospf": T(), "rip": T(), "isis": T(), "bgp": T(),
            "dhcp": T(), "vrrp": T(), "ipsec": T(), "ike": T(), "gre": T(),
            "lldp": U(), "vlan": U(), "vlanif": U(), "access_port": U(), "trunk_port": U(),
            "stp": U(), "eth_trunk": U(), "igmp_snooping": U(),
            "ipv6": U(), "ntp": U(), "l2tp": U(), "pppoe": U(),
            "mpls": U(), "mpls_ldp": U(), "igmp": U(), "pim": U(),
        },
    },
    "ne_ce": {
        "label": "NE/CE 路由器",
        "match": re.compile(r"\bNE\d+\b|\bCE\d+\b|CloudEngine", re.IGNORECASE),
        "protocols": {
            "static_route": T(), "routing_table": T(), "interface_ip": T(),
            "ospf": T(), "isis": T(), "bgp": T(), "rip": U(),
            "acl": T(), "dhcp": T(), "snmp": T(), "ssh": T(), "telnet": T(),
            "vrrp": T(), "mpls": T(), "mpls_ldp": T(), "vpn_instance": T(),
            "gre": T(), "ipsec": T(), "ike": T(), "nat": U(), "l2tp": U(),
            "vlan": T(), "vlanif": U(), "access_port": U(), "trunk_port": U(),
            "stp": U(), "eth_trunk": U(), "lldp": T(), "igmp_snooping": U(),
            "ipv6": T(), "ntp": T(), "igmp": U(), "pim": U(), "pppoe": F(),
        },
    },
}

# 未匹配到任何 family 时的兜底：全部 None（完全交给运行时探测）
_UNMATCHED_NOTE = "未知镜像，全部交给运行时探测确认"


# ---------------------------------------------------------------------------
# 版本解析
# ---------------------------------------------------------------------------
_MODEL_RE = re.compile(
    r"\b(AR\d+|S\d{4}|S\d{3}|NE\d+|CE\d+|USG\d+|LSW\d+|CloudEngine \d+)\b",
    re.IGNORECASE,
)
_VRP_PAREN_RE = re.compile(r"Version\s+[\d.]+\s*\(([^)]+)\)", re.IGNORECASE)
_VRP_PLAIN_RE = re.compile(r"Version\s+([\d.]+\w*)", re.IGNORECASE)


def parse_version_info(version_output: str) -> Dict[str, str]:
    """从 display version 输出解析 model 和镜像版本。

    eNSP 输出形如：
        VRP (R) software, Version 5.110 (S5700 V200R001C00)
        VRP (R) software, Version 5.130 (AR200 V200R003C00)
    括号内才是镜像版本（VxxxRxxxCxx）， plain "5.110" 是 VRP 内核号。
    """
    if not version_output:
        return {"model": "", "vrp_version": "", "image": ""}
    model = ""
    m = _MODEL_RE.search(version_output)
    if m:
        model = m.group(1).upper()

    vrp_version = ""
    image = ""
    m = _VRP_PAREN_RE.search(version_output)
    if m:
        image = m.group(1).strip()
    m = _VRP_PLAIN_RE.search(version_output)
    if m:
        vrp_version = m.group(1)
    return {"model": model, "vrp_version": vrp_version, "image": image}


def _match_family(model: str, version_output: str) -> Optional[str]:
    """根据型号匹配设备 family。"""
    if not model:
        return None
    for family, spec in _DEVICE_FAMILIES.items():
        if spec["match"].search(model):
            return family
    # 兜底：display version 原文再扫一遍
    for family, spec in _DEVICE_FAMILIES.items():
        if spec["match"].search(version_output or ""):
            return family
    return None


def lookup_matrix(version_output: str) -> Dict[str, Any]:
    """查矩阵：display version 输出 → 全量协议支持表。

    Returns:
        {
          "family": "s5700" | None,
          "family_label": "S5700 三层交换机" | "",
          "model": "S5700", "vrp_version": "5.110", "image": "S5700 V200R001C00",
          "confidence": "high" | "low",
          "protocols": {key: {"supported": True/False/None, "note": str}},
        }
    """
    info = parse_version_info(version_output)
    family = _match_family(info["model"], version_output)

    if family:
        spec = _DEVICE_FAMILIES[family]
        protocols = {
            key: {"supported": val[0], "note": val[1]}
            for key, val in spec["protocols"].items()
        }
        return {
            "family": family,
            "family_label": spec["label"],
            "model": info["model"],
            "vrp_version": info["vrp_version"],
            "image": info["image"],
            "confidence": "high",
            "protocols": protocols,
        }

    # 未匹配：全部 None
    protocols = {key: {"supported": None, "note": _UNMATCHED_NOTE}
                 for key in PROTOCOL_NAMES}
    return {
        "family": None,
        "family_label": "",
        "model": info["model"],
        "vrp_version": info["vrp_version"],
        "image": info["image"],
        "confidence": "low",
        "protocols": protocols,
    }


def matrix_support(matrix: Optional[dict], key: str) -> Optional[bool]:
    """取某协议的矩阵判定值（不在矩阵里的协议返回 None）。"""
    if not matrix:
        return None
    entry = matrix.get("protocols", {}).get(key)
    if not entry:
        return None
    return entry.get("supported")


def matrix_note(matrix: Optional[dict], key: str) -> str:
    if not matrix:
        return ""
    entry = matrix.get("protocols", {}).get(key)
    return entry.get("note", "") if entry else ""
