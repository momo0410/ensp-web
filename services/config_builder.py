"""
config_builder.py - 配置模板命令生成器
======================================
根据配置类型和表单参数生成华为 VRP 配置命令。
只生成预览，不连接设备。
支持所有可配置的设备能力。
"""

from typing import List, Dict, Any, Optional


# ---------------------------------------------------------------------------
# 配置模板定义
# ---------------------------------------------------------------------------

CONFIG_TEMPLATES: Dict[str, Dict[str, Any]] = {
    # ========== 路由相关 ==========
    "interface_ip": {
        "name": "配置接口 IP",
        "icon": "&#x1F310;",
        "fields": [
            {"name": "interface", "label": "接口", "type": "text", "required": True,
             "placeholder": "GigabitEthernet0/0/0"},
            {"name": "ip_address", "label": "IP 地址", "type": "text", "required": True,
             "placeholder": "192.168.1.1"},
            {"name": "subnet_mask", "label": "子网掩码", "type": "text", "required": True,
             "placeholder": "255.255.255.0"},
        ],
        "roles": ["router", "l3_switch", "switch"],
        "category": "routing",
    },
    "static_route": {
        "name": "配置静态路由",
        "icon": "&#x1F6E4;",
        "fields": [
            {"name": "destination", "label": "目标网络", "type": "text", "required": True,
             "placeholder": "10.0.0.0"},
            {"name": "mask", "label": "子网掩码", "type": "text", "required": True,
             "placeholder": "255.255.255.0"},
            {"name": "next_hop", "label": "下一跳", "type": "text", "required": True,
             "placeholder": "192.168.1.1"},
        ],
        "roles": ["router", "l3_switch"],
        "category": "routing",
    },
    "ospf": {
        "name": "配置 OSPF",
        "icon": "&#x1F310;",
        "fields": [
            {"name": "process_id", "label": "进程号", "type": "number", "default": "1",
             "required": True, "placeholder": "1"},
            {"name": "router_id", "label": "Router ID", "type": "text",
             "placeholder": "1.1.1.1"},
            {"name": "area", "label": "区域", "type": "text", "default": "0",
             "required": True, "placeholder": "0"},
            {"name": "network", "label": "网络地址", "type": "text", "required": True,
             "placeholder": "192.168.56.0"},
            {"name": "wildcard", "label": "反掩码", "type": "text", "required": True,
             "placeholder": "0.0.0.255"},
        ],
        "roles": ["router", "l3_switch"],
        "category": "routing",
    },
    "isis": {
        "name": "配置 IS-IS",
        "icon": "&#x1F310;",
        "fields": [
            {"name": "process_id", "label": "进程号", "type": "number", "default": "1",
             "required": True, "placeholder": "1"},
            {"name": "net_entity", "label": "NET 实体", "type": "text", "required": True,
             "placeholder": "49.0001.0000.0000.0001.00"},
            {"name": "is_level", "label": "级别", "type": "select", "default": "level-2",
             "options": [
                 {"value": "level-1", "label": "Level-1"},
                 {"value": "level-2", "label": "Level-2"},
                 {"value": "level-1-2", "label": "Level-1-2"},
             ]},
            {"name": "interface", "label": "接口", "type": "text", "required": True,
             "placeholder": "GigabitEthernet0/0/0"},
        ],
        "roles": ["router", "l3_switch"],
        "category": "routing",
    },
    "bgp": {
        "name": "配置 BGP",
        "icon": "&#x1F310;",
        "fields": [
            {"name": "local_as", "label": "本地 AS 号", "type": "number", "required": True,
             "placeholder": "65000"},
            {"name": "router_id", "label": "Router ID", "type": "text",
             "placeholder": "1.1.1.1"},
            {"name": "peer_ip", "label": "对端 IP", "type": "text", "required": True,
             "placeholder": "192.168.56.12"},
            {"name": "peer_as", "label": "对端 AS 号", "type": "number", "required": True,
             "placeholder": "65001"},
        ],
        "roles": ["router", "l3_switch"],
        "category": "routing",
    },
    "rip": {
        "name": "配置 RIP",
        "icon": "&#x1F310;",
        "fields": [
            {"name": "process_id", "label": "进程号", "type": "number", "default": "1",
             "required": True, "placeholder": "1"},
            {"name": "version", "label": "版本", "type": "select", "default": "2",
             "options": [
                 {"value": "1", "label": "RIP-1"},
                 {"value": "2", "label": "RIP-2"},
             ]},
            {"name": "network", "label": "网络地址", "type": "text", "required": True,
             "placeholder": "192.168.56.0"},
        ],
        "roles": ["router", "l3_switch"],
        "category": "routing",
    },
    # ========== 交换相关 ==========
    "vlan": {
        "name": "配置 VLAN",
        "icon": "&#x1F4E6;",
        "fields": [
            {"name": "vlan_id", "label": "VLAN ID", "type": "number", "required": True,
             "placeholder": "10"},
            {"name": "description", "label": "描述", "type": "text",
             "placeholder": "Office"},
        ],
        "roles": ["switch", "l3_switch"],
        "category": "switching",
    },
    "access_port": {
        "name": "配置 Access 接口",
        "icon": "&#x1F50C;",
        "fields": [
            {"name": "interface", "label": "接口", "type": "text", "required": True,
             "placeholder": "GigabitEthernet0/0/1"},
            {"name": "vlan_id", "label": "VLAN ID", "type": "number", "required": True,
             "placeholder": "10"},
        ],
        "roles": ["switch", "l3_switch"],
        "category": "switching",
    },
    "trunk_port": {
        "name": "配置 Trunk 接口",
        "icon": "&#x1F517;",
        "fields": [
            {"name": "interface", "label": "接口", "type": "text", "required": True,
             "placeholder": "GigabitEthernet0/0/1"},
            {"name": "allowed_vlans", "label": "允许的 VLAN", "type": "text", "required": True,
             "placeholder": "10 20 30"},
        ],
        "roles": ["switch", "l3_switch"],
        "category": "switching",
    },
    "vlanif": {
        "name": "配置 VLANIF",
        "icon": "&#x1F310;",
        "fields": [
            {"name": "vlan_id", "label": "VLAN ID", "type": "number", "required": True,
             "placeholder": "10"},
            {"name": "ip_address", "label": "IP 地址", "type": "text", "required": True,
             "placeholder": "192.168.10.1"},
            {"name": "subnet_mask", "label": "子网掩码", "type": "text", "required": True,
             "placeholder": "255.255.255.0"},
        ],
        "roles": ["switch", "l3_switch"],
        "category": "switching",
    },
    "stp": {
        "name": "配置 STP",
        "icon": "&#x1F333;",
        "fields": [
            {"name": "enable", "label": "启用 STP", "type": "checkbox", "default": True},
            {"name": "mode", "label": "模式", "type": "select", "default": "mstp",
             "options": [
                 {"value": "stp", "label": "STP"},
                 {"value": "rstp", "label": "RSTP"},
                 {"value": "mstp", "label": "MSTP"},
             ]},
            {"name": "priority", "label": "优先级", "type": "number", "default": "32768",
             "placeholder": "32768"},
        ],
        "roles": ["switch", "l3_switch"],
        "category": "switching",
    },
    "eth_trunk": {
        "name": "配置 Eth-Trunk",
        "icon": "&#x1F517;",
        "fields": [
            {"name": "trunk_id", "label": "Eth-Trunk 编号", "type": "number", "required": True,
             "placeholder": "1"},
            {"name": "mode", "label": "模式", "type": "select", "default": "lacp",
             "options": [
                 {"value": "manual", "label": "手工负载分担"},
                 {"value": "lacp", "label": "LACP"},
             ]},
            {"name": "member_interface", "label": "成员接口", "type": "text", "required": True,
             "placeholder": "GigabitEthernet0/0/1"},
        ],
        "roles": ["switch", "l3_switch", "router"],
        "category": "switching",
    },
    # ========== 基础网络功能 ==========
    "lldp": {
        "name": "配置 LLDP",
        "icon": "&#x1F517;",
        "fields": [
            {"name": "enable", "label": "启用 LLDP", "type": "checkbox", "default": True},
        ],
        "roles": ["router", "switch", "l3_switch"],
        "category": "network",
    },
    "dhcp": {
        "name": "配置 DHCP",
        "icon": "&#x1F4E1;",
        "fields": [
            {"name": "enable", "label": "启用 DHCP", "type": "checkbox", "default": True},
            {"name": "pool_name", "label": "地址池名称", "type": "text", "required": True,
             "placeholder": "pool1"},
            {"name": "network", "label": "网络地址", "type": "text", "required": True,
             "placeholder": "192.168.1.0"},
            {"name": "mask", "label": "子网掩码", "type": "text", "required": True,
             "placeholder": "255.255.255.0"},
            {"name": "gateway", "label": "网关", "type": "text",
             "placeholder": "192.168.1.1"},
            {"name": "dns", "label": "DNS 服务器", "type": "text",
             "placeholder": "8.8.8.8"},
            {"name": "lease_days", "label": "租约天数", "type": "number", "default": "1",
             "placeholder": "1"},
        ],
        "roles": ["router", "l3_switch"],
        "category": "network",
    },
    "nat": {
        "name": "配置 NAT",
        "icon": "&#x1F504;",
        "fields": [
            {"name": "nat_type", "label": "NAT 类型", "type": "select", "default": "outbound",
             "options": [
                 {"value": "outbound", "label": "Easy IP (出方向)"},
                 {"value": "static", "label": "静态 NAT"},
                 {"value": "server", "label": "NAT Server"},
             ]},
            {"name": "acl_number", "label": "ACL 编号", "type": "number",
             "placeholder": "2000"},
            {"name": "interface", "label": "出接口", "type": "text",
             "placeholder": "GigabitEthernet0/0/0"},
            {"name": "global_ip", "label": "公网 IP", "type": "text",
             "placeholder": "200.1.1.1"},
            {"name": "inside_ip", "label": "内网 IP", "type": "text",
             "placeholder": "192.168.1.100"},
        ],
        "roles": ["router"],
        "category": "network",
    },
    "vrrp": {
        "name": "配置 VRRP",
        "icon": "&#x1F6E1;",
        "fields": [
            {"name": "interface", "label": "接口", "type": "text", "required": True,
             "placeholder": "GigabitEthernet0/0/0"},
            {"name": "vrid", "label": "VRID", "type": "number", "default": "1",
             "required": True, "placeholder": "1"},
            {"name": "virtual_ip", "label": "虚拟 IP", "type": "text", "required": True,
             "placeholder": "192.168.1.1"},
            {"name": "priority", "label": "优先级", "type": "number", "default": "100",
             "placeholder": "100"},
        ],
        "roles": ["router", "l3_switch"],
        "category": "network",
    },
    # ========== 安全/管理功能 ==========
    "acl": {
        "name": "配置 ACL",
        "icon": "&#x1F6E1;",
        "fields": [
            {"name": "acl_number", "label": "ACL 编号", "type": "number", "required": True,
             "placeholder": "3000"},
            {"name": "rule_type", "label": "规则", "type": "select", "default": "permit",
             "options": [
                 {"value": "permit", "label": "允许"},
                 {"value": "deny", "label": "拒绝"},
             ]},
            {"name": "protocol", "label": "协议", "type": "select", "default": "ip",
             "options": [
                 {"value": "ip", "label": "IP"},
                 {"value": "tcp", "label": "TCP"},
                 {"value": "udp", "label": "UDP"},
                 {"value": "icmp", "label": "ICMP"},
             ]},
            {"name": "source", "label": "源地址", "type": "text", "required": True,
             "placeholder": "192.168.1.0 0.0.0.255"},
            {"name": "destination", "label": "目的地址", "type": "text",
             "placeholder": "any"},
        ],
        "roles": ["router", "switch", "l3_switch"],
        "category": "security",
    },
    "ssh_user": {
        "name": "配置 SSH 用户",
        "icon": "&#x1F512;",
        "fields": [
            {"name": "username", "label": "用户名", "type": "text", "default": "admin",
             "required": True, "placeholder": "admin"},
            {"name": "password", "label": "密码", "type": "password", "required": True,
             "placeholder": "Huawei@123"},
            {"name": "privilege_level", "label": "权限等级", "type": "number", "default": "15",
             "required": True, "placeholder": "15"},
        ],
        "roles": ["router", "switch", "l3_switch"],
        "category": "security",
    },
    "telnet": {
        "name": "配置 Telnet",
        "icon": "&#x1F4BB;",
        "fields": [
            {"name": "enable", "label": "启用 Telnet 服务器", "type": "checkbox", "default": True},
            {"name": "username", "label": "用户名", "type": "text", "default": "admin",
             "placeholder": "admin"},
            {"name": "password", "label": "密码", "type": "password",
             "placeholder": "Huawei@123"},
            {"name": "privilege_level", "label": "权限等级", "type": "number", "default": "15",
             "placeholder": "15"},
        ],
        "roles": ["router", "switch", "l3_switch"],
        "category": "security",
    },
    "snmp": {
        "name": "配置 SNMP",
        "icon": "&#x1F4CA;",
        "fields": [
            {"name": "community", "label": "团体名", "type": "text", "default": "public",
             "required": True, "placeholder": "public"},
            {"name": "permission", "label": "权限", "type": "select", "default": "read",
             "options": [
                 {"value": "read", "label": "只读"},
                 {"value": "write", "label": "读写"},
             ]},
            {"name": "version", "label": "版本", "type": "select", "default": "v2c",
             "options": [
                 {"value": "v1", "label": "SNMPv1"},
                 {"value": "v2c", "label": "SNMPv2c"},
                 {"value": "all", "label": "全部版本"},
             ]},
            {"name": "sys_contact", "label": "系统联系人", "type": "text",
             "placeholder": "admin@example.com"},
            {"name": "sys_location", "label": "系统位置", "type": "text",
             "placeholder": "Beijing"},
        ],
        "roles": ["router", "switch", "l3_switch"],
        "category": "security",
    },
    # ========== 安全/VPN 功能 ==========
    "ipsec": {
        "name": "配置 IPsec",
        "icon": "&#x1F512;",
        "fields": [
            {"name": "proposal_name", "label": "Proposal 名称", "type": "text", "required": True,
             "placeholder": "prop1"},
            {"name": "enc_algorithm", "label": "加密算法", "type": "select", "default": "aes-256",
             "options": [
                 {"value": "aes-128", "label": "AES-128"},
                 {"value": "aes-256", "label": "AES-256"},
                 {"value": "3des", "label": "3DES"},
                 {"value": "des", "label": "DES"},
             ]},
            {"name": "auth_algorithm", "label": "认证算法", "type": "select", "default": "sha2-256",
             "options": [
                 {"value": "md5", "label": "MD5"},
                 {"value": "sha1", "label": "SHA1"},
                 {"value": "sha2-256", "label": "SHA2-256"},
             ]},
            {"name": "policy_name", "label": "Policy 名称", "type": "text", "required": True,
             "placeholder": "policy1"},
            {"name": "peer_address", "label": "对端地址", "type": "text", "required": True,
             "placeholder": "200.1.1.2"},
            {"name": "acl_number", "label": "ACL 编号 (匹配流量)", "type": "number",
             "required": True, "placeholder": "3000"},
        ],
        "roles": ["router"],
        "category": "vpn",
    },
    "ike": {
        "name": "配置 IKE",
        "icon": "&#x1F511;",
        "fields": [
            {"name": "proposal_name", "label": "Proposal 名称", "type": "text", "required": True,
             "placeholder": "ike_prop1"},
            {"name": "auth_method", "label": "认证方法", "type": "select", "default": "pre-share",
             "options": [
                 {"value": "pre-share", "label": "预共享密钥"},
                 {"value": "rsa-sig", "label": "RSA 签名"},
             ]},
            {"name": "enc_algorithm", "label": "加密算法", "type": "select", "default": "aes-256",
             "options": [
                 {"value": "aes-128", "label": "AES-128"},
                 {"value": "aes-256", "label": "AES-256"},
                 {"value": "3des", "label": "3DES"},
             ]},
            {"name": "auth_algorithm", "label": "认证算法", "type": "select", "default": "sha2-256",
             "options": [
                 {"value": "md5", "label": "MD5"},
                 {"value": "sha1", "label": "SHA1"},
                 {"value": "sha2-256", "label": "SHA2-256"},
             ]},
            {"name": "dh_group", "label": "DH 组", "type": "select", "default": "group14",
             "options": [
                 {"value": "group1", "label": "Group 1 (MODP 768)"},
                 {"value": "group2", "label": "Group 2 (MODP 1024)"},
                 {"value": "group5", "label": "Group 5 (MODP 1536)"},
                 {"value": "group14", "label": "Group 14 (MODP 2048)"},
             ]},
            {"name": "peer_name", "label": "Peer 名称", "type": "text", "required": True,
             "placeholder": "peer1"},
            {"name": "peer_address", "label": "对端地址", "type": "text", "required": True,
             "placeholder": "200.1.1.2"},
            {"name": "pre_shared_key", "label": "预共享密钥", "type": "password", "required": True,
             "placeholder": "Huawei@123"},
            {"name": "local_address", "label": "本端地址", "type": "text",
             "placeholder": "200.1.1.1"},
        ],
        "roles": ["router"],
        "category": "vpn",
    },
    "gre": {
        "name": "配置 GRE 隧道",
        "icon": "&#x1F6A7;",
        "fields": [
            {"name": "tunnel_id", "label": "Tunnel 编号", "type": "number", "default": "0",
             "required": True, "placeholder": "0"},
            {"name": "source", "label": "隧道源 (接口/IP)", "type": "text", "required": True,
             "placeholder": "200.1.1.1"},
            {"name": "destination", "label": "隧道目的 (IP)", "type": "text", "required": True,
             "placeholder": "200.1.1.2"},
            {"name": "tunnel_ip", "label": "隧道接口 IP", "type": "text", "required": True,
             "placeholder": "10.0.0.1"},
            {"name": "tunnel_mask", "label": "隧道接口掩码", "type": "text", "default": "255.255.255.0",
             "placeholder": "255.255.255.0"},
            {"name": "protocol", "label": "隧道协议", "type": "select", "default": "gre",
             "options": [
                 {"value": "gre", "label": "GRE"},
                 {"value": "ip", "label": "IP (IP封装)"},
             ]},
        ],
        "roles": ["router"],
        "category": "vpn",
    },
    "vpn_instance": {
        "name": "配置 VPN 实例",
        "icon": "&#x1F3E2;",
        "fields": [
            {"name": "name", "label": "VPN 实例名称", "type": "text", "required": True,
             "placeholder": "vpn1"},
            {"name": "rd", "label": "Route Distinguisher", "type": "text", "required": True,
             "placeholder": "100:1"},
            {"name": "rt_export", "label": "Export RT", "type": "text",
             "placeholder": "100:1"},
            {"name": "rt_import", "label": "Import RT", "type": "text",
             "placeholder": "100:1"},
            {"name": "interface", "label": "绑定接口", "type": "text",
             "placeholder": "GigabitEthernet0/0/1"},
        ],
        "roles": ["router"],
        "category": "vpn",
    },
}

# Verification commands per config type
VERIFY_COMMANDS: Dict[str, List[str]] = {
    "interface_ip":   ["display ip interface brief"],
    "static_route":   ["display ip routing-table"],
    "ospf":           ["display ospf peer", "display current-configuration | include ospf"],
    "isis":           ["display isis peer", "display current-configuration | include isis"],
    "bgp":            ["display bgp peer", "display current-configuration | include bgp"],
    "rip":            ["display rip", "display current-configuration | include rip"],
    "vlan":           ["display vlan"],
    "access_port":    [],  # dynamic: display current-configuration interface <intf>
    "trunk_port":     [],  # dynamic
    "vlanif":         ["display ip interface brief"],
    "stp":            ["display stp brief"],
    "eth_trunk":      ["display eth-trunk"],
    "lldp":           ["display lldp neighbor brief"],
    "dhcp":           ["display dhcp server statistics", "display ip pool"],
    "nat":            ["display nat session", "display current-configuration | include nat"],
    "vrrp":           ["display vrrp brief"],
    "acl":            ["display acl all"],
    "ssh_user":       ["display ssh server status", "display current-configuration | include ssh"],
    "telnet":         ["display telnet server status", "display current-configuration | include telnet"],
    "snmp":           ["display snmp-agent sys-info"],
    "ipsec":          ["display ipsec sa", "display ipsec policy"],
    "ike":            ["display ike sa", "display ike peer"],
    "gre":            ["display interface Tunnel"],
    "vpn_instance":   ["display ip vpn-instance"],
}

# Category names for frontend grouping
CATEGORY_NAMES = {
    "routing": "路由协议",
    "switching": "交换功能",
    "network": "基础网络",
    "security": "安全管理",
    "vpn": "VPN/隧道",
}


# ---------------------------------------------------------------------------
# 命令生成函数
# ---------------------------------------------------------------------------

def _build_interface_ip(params: dict) -> List[str]:
    intf = params.get("interface", "").strip()
    ip_addr = params.get("ip_address", "").strip()
    mask = params.get("subnet_mask", "").strip()
    if not intf or not ip_addr or not mask:
        return []
    return [
        f"interface {intf}",
        f"ip address {ip_addr} {mask}",
        "quit",
    ]


def _build_static_route(params: dict) -> List[str]:
    dest = params.get("destination", "").strip()
    mask = params.get("mask", "").strip()
    next_hop = params.get("next_hop", "").strip()
    if not dest or not mask or not next_hop:
        return []
    return [f"ip route-static {dest} {mask} {next_hop}"]


def _build_ospf(params: dict) -> List[str]:
    pid = params.get("process_id", "1").strip()
    rid = params.get("router_id", "").strip()
    area = params.get("area", "0").strip()
    network = params.get("network", "").strip()
    wildcard = params.get("wildcard", "").strip()
    if not network or not wildcard:
        return []
    cmds = [f"ospf {pid}" + (f" router-id {rid}" if rid else "")]
    cmds.append(f"area {area}")
    cmds.append(f"network {network} {wildcard}")
    cmds.append("quit")
    cmds.append("quit")
    return cmds


def _build_isis(params: dict) -> List[str]:
    pid = params.get("process_id", "1").strip()
    net = params.get("net_entity", "").strip()
    level = params.get("is_level", "level-2").strip()
    intf = params.get("interface", "").strip()
    if not net or not intf:
        return []
    cmds = [
        f"isis {pid}",
        f"network-entity {net}",
        f"is-level {level}",
        "quit",
        f"interface {intf}",
        f"isis enable {pid}",
        "quit",
    ]
    return cmds


def _build_bgp(params: dict) -> List[str]:
    local_as = params.get("local_as", "").strip()
    rid = params.get("router_id", "").strip()
    peer_ip = params.get("peer_ip", "").strip()
    peer_as = params.get("peer_as", "").strip()
    if not local_as or not peer_ip or not peer_as:
        return []
    cmds = [f"bgp {local_as}"]
    if rid:
        cmds.append(f"router-id {rid}")
    cmds.append(f"peer {peer_ip} as-number {peer_as}")
    cmds.append("quit")
    return cmds


def _build_rip(params: dict) -> List[str]:
    pid = params.get("process_id", "1").strip()
    ver = params.get("version", "2").strip()
    network = params.get("network", "").strip()
    if not network:
        return []
    cmds = [f"rip {pid}"]
    if ver == "2":
        cmds.append("version 2")
    cmds.append(f"network {network}")
    cmds.append("quit")
    return cmds


def _build_vlan(params: dict) -> List[str]:
    vid = params.get("vlan_id", "").strip()
    desc = params.get("description", "").strip()
    if not vid:
        return []
    cmds = [f"vlan {vid}"]
    if desc:
        cmds.append(f"description {desc}")
    cmds.append("quit")
    return cmds


def _build_access_port(params: dict) -> List[str]:
    intf = params.get("interface", "").strip()
    vid = params.get("vlan_id", "").strip()
    if not intf or not vid:
        return []
    return [
        f"interface {intf}",
        "port link-type access",
        f"port default vlan {vid}",
        "quit",
    ]


def _build_trunk_port(params: dict) -> List[str]:
    intf = params.get("interface", "").strip()
    vlans = params.get("allowed_vlans", "").strip()
    if not intf or not vlans:
        return []
    return [
        f"interface {intf}",
        "port link-type trunk",
        f"port trunk allow-pass vlan {vlans}",
        "quit",
    ]


def _build_vlanif(params: dict) -> List[str]:
    vid = params.get("vlan_id", "").strip()
    ip_addr = params.get("ip_address", "").strip()
    mask = params.get("subnet_mask", "").strip()
    if not vid or not ip_addr or not mask:
        return []
    return [
        f"interface Vlanif {vid}",
        f"ip address {ip_addr} {mask}",
        "quit",
    ]


def _build_stp(params: dict) -> List[str]:
    enable = params.get("enable", True)
    if isinstance(enable, str):
        enable = enable.lower() in ("true", "1", "yes", "on")
    mode = params.get("mode", "mstp").strip()
    priority = params.get("priority", "32768").strip()
    cmds = []
    if enable:
        cmds.append(f"stp mode {mode}")
        cmds.append(f"stp priority {priority}")
        cmds.append("stp enable")
    else:
        cmds.append("undo stp enable")
    return cmds


def _build_eth_trunk(params: dict) -> List[str]:
    tid = params.get("trunk_id", "").strip()
    mode = params.get("mode", "lacp").strip()
    member = params.get("member_interface", "").strip()
    if not tid or not member:
        return []
    cmds = [
        f"interface Eth-Trunk {tid}",
    ]
    if mode == "lacp":
        cmds.append("mode lacp")
    cmds.append("quit")
    cmds.append(f"interface {member}")
    cmds.append(f"eth-trunk {tid}")
    cmds.append("quit")
    return cmds


def _build_lldp(params: dict) -> List[str]:
    enable = params.get("enable", True)
    if isinstance(enable, str):
        enable = enable.lower() in ("true", "1", "yes", "on")
    return ["lldp enable"] if enable else ["undo lldp enable"]


def _build_dhcp(params: dict) -> List[str]:
    enable = params.get("enable", True)
    if isinstance(enable, str):
        enable = enable.lower() in ("true", "1", "yes", "on")
    if not enable:
        return ["undo dhcp enable"]
    pool = params.get("pool_name", "").strip()
    network = params.get("network", "").strip()
    mask = params.get("mask", "").strip()
    gateway = params.get("gateway", "").strip()
    dns = params.get("dns", "").strip()
    lease = params.get("lease_days", "1").strip()
    if not pool or not network or not mask:
        return []
    cmds = ["dhcp enable"]
    cmds.append(f"ip pool {pool}")
    cmds.append(f"network {network} mask {mask}")
    if gateway:
        cmds.append(f"gateway-list {gateway}")
    if dns:
        cmds.append(f"dns-list {dns}")
    cmds.append(f"lease day {lease}")
    cmds.append("quit")
    return cmds


def _build_nat(params: dict) -> List[str]:
    nat_type = params.get("nat_type", "outbound").strip()
    acl = params.get("acl_number", "").strip()
    intf = params.get("interface", "").strip()
    global_ip = params.get("global_ip", "").strip()
    inside_ip = params.get("inside_ip", "").strip()
    if nat_type == "outbound":
        if not acl or not intf:
            return []
        return [
            f"interface {intf}",
            f"nat outbound {acl}",
            "quit",
        ]
    elif nat_type == "static":
        if not global_ip or not inside_ip:
            return []
        return [f"nat static global {global_ip} inside {inside_ip}"]
    elif nat_type == "server":
        if not global_ip or not inside_ip or not intf:
            return []
        return [
            f"interface {intf}",
            f"nat server protocol tcp global {global_ip} inside {inside_ip}",
            "quit",
        ]
    return []


def _build_vrrp(params: dict) -> List[str]:
    intf = params.get("interface", "").strip()
    vrid = params.get("vrid", "1").strip()
    vip = params.get("virtual_ip", "").strip()
    priority = params.get("priority", "100").strip()
    if not intf or not vip:
        return []
    return [
        f"interface {intf}",
        f"vrrp vrid {vrid} virtual-ip {vip}",
        f"vrrp vrid {vrid} priority {priority}",
        "quit",
    ]


def _build_acl(params: dict) -> List[str]:
    acl_num = params.get("acl_number", "").strip()
    rule_type = params.get("rule_type", "permit").strip()
    protocol = params.get("protocol", "ip").strip()
    source = params.get("source", "").strip()
    dest = params.get("destination", "").strip()
    if not acl_num or not source:
        return []
    cmds = [f"acl number {acl_num}"]
    rule = f"rule {rule_type} {protocol} source {source}"
    if dest and dest.lower() != "any":
        rule += f" destination {dest}"
    cmds.append(rule)
    cmds.append("quit")
    return cmds


def _build_ssh_user(params: dict) -> List[str]:
    username = params.get("username", "admin").strip()
    password = params.get("password", "").strip()
    priv = params.get("privilege_level", "15").strip()
    if not username or not password:
        return []
    return [
        "aaa",
        f"local-user {username} password cipher {password}",
        f"local-user {username} privilege level {priv}",
        f"local-user {username} service-type ssh",
        "quit",
        "stelnet server enable",
        f"ssh user {username} authentication-type password",
        "user-interface vty 0 4",
        "authentication-mode aaa",
        "protocol inbound ssh",
        "quit",
    ]


def _build_telnet(params: dict) -> List[str]:
    enable = params.get("enable", True)
    if isinstance(enable, str):
        enable = enable.lower() in ("true", "1", "yes", "on")
    username = params.get("username", "").strip()
    password = params.get("password", "").strip()
    priv = params.get("privilege_level", "15").strip()
    cmds = []
    if enable:
        cmds.append("telnet server enable")
    else:
        cmds.append("undo telnet server enable")
        return cmds
    if username and password:
        cmds.append("aaa")
        cmds.append(f"local-user {username} password cipher {password}")
        cmds.append(f"local-user {username} privilege level {priv}")
        cmds.append(f"local-user {username} service-type telnet")
        cmds.append("quit")
        cmds.append("user-interface vty 0 4")
        cmds.append("authentication-mode aaa")
        cmds.append("protocol inbound telnet")
        cmds.append("quit")
    return cmds


def _build_snmp(params: dict) -> List[str]:
    community = params.get("community", "public").strip()
    permission = params.get("permission", "read").strip()
    version = params.get("version", "v2c").strip()
    contact = params.get("sys_contact", "").strip()
    location = params.get("sys_location", "").strip()
    if not community:
        return []
    cmds = []
    if version == "all":
        cmds.append(f"snmp-agent community {permission} {community}")
    else:
        cmds.append(f"snmp-agent community {permission} {community} acl 2000")
        cmds.append(f"snmp-agent sys-info version {version}")
    if contact:
        cmds.append(f"snmp-agent sys-info contact {contact}")
    if location:
        cmds.append(f"snmp-agent sys-info location {location}")
    return cmds


def _build_ipsec(params: dict) -> List[str]:
    prop_name = params.get("proposal_name", "").strip()
    enc = params.get("enc_algorithm", "aes-256").strip()
    auth = params.get("auth_algorithm", "sha2-256").strip()
    policy_name = params.get("policy_name", "").strip()
    peer = params.get("peer_address", "").strip()
    acl = params.get("acl_number", "").strip()
    if not prop_name or not policy_name or not peer or not acl:
        return []
    cmds = [
        f"ipsec proposal {prop_name}",
        f"esp authentication-algorithm {auth}",
        f"esp encryption-algorithm {enc}",
        "quit",
        f"ipsec policy {policy_name} 10 isakmp",
        f"security acl {acl}",
        f"ike peer {peer}",
        f"proposal {prop_name}",
        "quit",
        "quit",
    ]
    return cmds


def _build_ike(params: dict) -> List[str]:
    prop_name = params.get("proposal_name", "").strip()
    auth_method = params.get("auth_method", "pre-share").strip()
    enc = params.get("enc_algorithm", "aes-256").strip()
    auth = params.get("auth_algorithm", "sha2-256").strip()
    dh = params.get("dh_group", "group14").strip()
    peer_name = params.get("peer_name", "").strip()
    peer_addr = params.get("peer_address", "").strip()
    psk = params.get("pre_shared_key", "").strip()
    local_addr = params.get("local_address", "").strip()
    if not prop_name or not peer_name or not peer_addr or not psk:
        return []
    cmds = [
        f"ike proposal {prop_name}",
        f"authentication-method {auth_method}",
        f"encryption-algorithm {enc}",
        f"authentication-algorithm {auth}",
        f"dh {dh}",
        "quit",
        f"ike peer {peer_name}",
        f"ike-proposal {prop_name}",
        f"pre-shared-key simple {psk}",
        f"remote-address {peer_addr}",
    ]
    if local_addr:
        cmds.append(f"local-address {local_addr}")
    cmds.append("quit")
    return cmds


def _build_gre(params: dict) -> List[str]:
    tid = params.get("tunnel_id", "0").strip()
    source = params.get("source", "").strip()
    dest = params.get("destination", "").strip()
    tip = params.get("tunnel_ip", "").strip()
    tmask = params.get("tunnel_mask", "255.255.255.0").strip()
    proto = params.get("protocol", "gre").strip()
    if not source or not dest or not tip:
        return []
    return [
        f"interface Tunnel {tid}",
        f"ip address {tip} {tmask}",
        f"tunnel-protocol {proto}",
        f"source {source}",
        f"destination {dest}",
        "quit",
    ]


def _build_vpn_instance(params: dict) -> List[str]:
    name = params.get("name", "").strip()
    rd = params.get("rd", "").strip()
    rt_export = params.get("rt_export", "").strip()
    rt_import = params.get("rt_import", "").strip()
    intf = params.get("interface", "").strip()
    if not name or not rd:
        return []
    cmds = [
        f"ip vpn-instance {name}",
        f"route-distinguisher {rd}",
    ]
    if rt_export:
        cmds.append(f"vpn-target {rt_export} export-extcommunity")
    if rt_import:
        cmds.append(f"vpn-target {rt_import} import-extcommunity")
    cmds.append("quit")
    if intf:
        cmds.append(f"interface {intf}")
        cmds.append(f"ip binding vpn-instance {name}")
        cmds.append("quit")
    return cmds


# Build dispatch map
_BUILDERS = {
    "interface_ip":  _build_interface_ip,
    "static_route":  _build_static_route,
    "ospf":          _build_ospf,
    "isis":          _build_isis,
    "bgp":           _build_bgp,
    "rip":           _build_rip,
    "vlan":          _build_vlan,
    "access_port":   _build_access_port,
    "trunk_port":    _build_trunk_port,
    "vlanif":        _build_vlanif,
    "stp":           _build_stp,
    "eth_trunk":     _build_eth_trunk,
    "lldp":          _build_lldp,
    "dhcp":          _build_dhcp,
    "nat":           _build_nat,
    "vrrp":          _build_vrrp,
    "acl":           _build_acl,
    "ssh_user":      _build_ssh_user,
    "telnet":        _build_telnet,
    "snmp":          _build_snmp,
    "ipsec":         _build_ipsec,
    "ike":           _build_ike,
    "gre":           _build_gre,
    "vpn_instance":  _build_vpn_instance,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_templates_for_role(device_role: str) -> Dict[str, dict]:
    """返回适用于指定设备角色的配置模板。"""
    result = {}
    for key, tpl in CONFIG_TEMPLATES.items():
        if device_role in tpl.get("roles", []):
            result[key] = {
                "name": tpl["name"],
                "icon": tpl["icon"],
                "fields": tpl["fields"],
                "category": tpl.get("category", ""),
            }
    return result


def get_all_templates() -> Dict[str, dict]:
    """返回所有配置模板。"""
    return {
        key: {
            "name": tpl["name"],
            "icon": tpl["icon"],
            "fields": tpl["fields"],
            "category": tpl.get("category", ""),
        }
        for key, tpl in CONFIG_TEMPLATES.items()
    }


def get_categories() -> Dict[str, str]:
    """返回所有分类名称。"""
    return dict(CATEGORY_NAMES)


def build_config_commands(config_type: str, params: dict) -> List[str]:
    """
    根据配置类型和参数生成配置命令列表。

    Args:
        config_type: 配置类型标识
        params: 表单参数字典

    Returns:
        配置命令列表（不含 system-view / return）
    """
    builder = _BUILDERS.get(config_type)
    if not builder:
        return []
    return builder(params)


def get_verify_commands(config_type: str, params: dict) -> List[str]:
    """
    获取配置类型对应的验证命令。

    对于 access_port / trunk_port，验证命令需要接口名。
    """
    if config_type == "access_port" or config_type == "trunk_port":
        intf = params.get("interface", "").strip()
        if intf:
            return [f"display current-configuration interface {intf}"]
        return []
    return VERIFY_COMMANDS.get(config_type, [])


def build_preview(config_type: str, params: dict) -> dict:
    """
    生成配置预览（包含 system-view 包裹的完整命令）。

    Returns:
        {
            "config_type": str,
            "commands": [...],       # 实际下发命令（不含 system-view）
            "preview_commands": [...],  # 预览显示命令（含 system-view）
            "verify_commands": [...],
        }
    """
    commands = build_config_commands(config_type, params)
    if not commands:
        return {
            "config_type": config_type,
            "commands": [],
            "preview_commands": [],
            "verify_commands": [],
            "error": "参数不完整，无法生成配置命令",
        }

    preview = ["system-view"] + commands + ["return"]
    verify = get_verify_commands(config_type, params)

    return {
        "config_type": config_type,
        "commands": commands,
        "preview_commands": preview,
        "verify_commands": verify,
    }
