"""
command_catalog.py - 声明式命令目录（表单化配置单一数据源）
============================================================
把「每条指令 = 一段 Python 拼接代码 + 一份硬编码 JS 表单」收敛为：

    一条目录记录 = fields(表单 schema) + command_template(含 {{变量}} 的命令模板) + 元信息

由此：
- 前端用通用渲染器（renderFormField）从 fields 自动出表单，零 JS；
- 后端用 free_config_renderer 把 {{变量}} 替换为参数，零 Python builder；
- 加任意一条指令 = 往 COMMAND_CATALOG 加一条数据，不改代码、不改前端。

覆盖 eNSP 全设备类型（device_types）：
    router   - 路由器（AR 系列）
    l3_switch- 三层交换机（S5700 等）
    switch   - 二层交换机（S3700 等）
    firewall - 防火墙（USG6000V / USG5500）
    ac       - 无线控制器（AC）
    cloud    - 云交换机（CloudEngine CE）

每条记录带 trigger（system-view 下 ? 输出的顶层关键字列表），供「? 探测引导」
逐条校验该设备是否真的支持此指令，实现「设备支持什么」的自动发现。

支持两种下发通道（均复用既有实现）：
- 设备通道：domain.console.console_deploy
- Console（零 IP）：main.console_deploy
"""

from typing import Dict, Any, List, Optional, Tuple

from .free_config_renderer import clean_commands, render_commands


# eNSP 设备分类（与前端 DEVICE_TYPE_LABELS 对应）
DEVICE_TYPES: List[str] = ["router", "l3_switch", "switch", "firewall", "ac", "cloud"]


# ---------------------------------------------------------------------------
# 命令目录（声明式，单一数据源）
# field schema 与 CONFIG_TEMPLATES / PROTOCOL_FORMS 完全兼容：
#   name, label, type(text/number/select/password/checkbox), required,
#   default, placeholder, options[], optional(可选, 空值时整行丢弃)
# command_template 为系统视图下的命令（不含 system-view / return / save，由下发通道自动包裹）
# device_types: 该指令适用的 eNSP 设备类型
# trigger: 在系统视图下敲 ? 时，输出里需要出现的顶层关键字（用于 ? 探测校验）
# ---------------------------------------------------------------------------
COMMAND_CATALOG: Dict[str, Dict[str, Any]] = {
    # ===================== 基础 =====================
    "sysname": {
        "name": "修改设备名",
        "icon": "&#x1F4E4;",
        "category": "基础",
        "roles": ["router", "l3_switch", "switch"],
        "device_types": ["router", "l3_switch", "switch", "firewall", "ac", "cloud"],
        "trigger": ["sysname"],
        "fields": [
            {"name": "name", "label": "设备名", "type": "text", "required": True,
             "placeholder": "Core-R1"},
        ],
        "command_template": [
            "sysname {{name}}",
        ],
        "verify": ["display current-configuration | include sysname"],
    },

    # ===================== 接口 =====================
    "interface_ip": {
        "name": "配置接口 IP",
        "icon": "&#x1F310;",
        "category": "接口",
        "roles": ["router", "l3_switch", "switch"],
        "device_types": ["router", "l3_switch", "firewall", "ac"],
        "trigger": ["interface"],
        "fields": [
            {"name": "interface", "label": "接口", "type": "text", "required": True,
             "placeholder": "GigabitEthernet0/0/0"},
            {"name": "ip_address", "label": "IP 地址", "type": "text", "required": True,
             "placeholder": "192.168.1.1"},
            {"name": "subnet_mask", "label": "子网掩码", "type": "text", "required": True,
             "placeholder": "255.255.255.0"},
            {"name": "description", "label": "接口描述", "type": "text", "optional": True,
             "placeholder": "可选"},
        ],
        "command_template": [
            "interface {{interface}}",
            "ip address {{ip_address}} {{subnet_mask}}",
            "description {{description}}",
        ],
        "verify": ["display ip interface brief"],
    },
    "interface_desc": {
        "name": "配置接口描述",
        "icon": "&#x1F4DD;",
        "category": "接口",
        "roles": ["router", "l3_switch", "switch"],
        "device_types": ["router", "l3_switch", "switch", "firewall", "ac", "cloud"],
        "trigger": ["interface"],
        "fields": [
            {"name": "interface", "label": "接口", "type": "text", "required": True,
             "placeholder": "GigabitEthernet0/0/1"},
            {"name": "description", "label": "描述", "type": "text", "required": True,
             "placeholder": "TO-CORE"},
        ],
        "command_template": [
            "interface {{interface}}",
            "description {{description}}",
        ],
        "verify": ["display interface {{interface}} | include Description"],
    },
    "loopback": {
        "name": "创建 Loopback 接口",
        "icon": "&#x1F300;",
        "category": "接口",
        "roles": ["router", "l3_switch"],
        "device_types": ["router", "l3_switch"],
        "trigger": ["interface"],
        "fields": [
            {"name": "id", "label": "LoopBack 编号", "type": "number", "required": True,
             "placeholder": "0"},
            {"name": "ip_address", "label": "IP 地址", "type": "text", "required": True,
             "placeholder": "1.1.1.1"},
            {"name": "mask", "label": "掩码位数", "type": "number", "default": "32",
             "placeholder": "32"},
        ],
        "command_template": [
            "interface LoopBack {{id}}",
            "ip address {{ip_address}} {{mask}}",
        ],
        "verify": ["display ip interface brief"],
    },

    # ===================== 路由 =====================
    "static_route": {
        "name": "配置静态路由",
        "icon": "&#x1F6E4;",
        "category": "路由",
        "roles": ["router", "l3_switch"],
        "device_types": ["router", "l3_switch"],
        "trigger": ["ip"],
        "fields": [
            {"name": "destination", "label": "目标网络", "type": "text", "required": True,
             "placeholder": "10.0.0.0"},
            {"name": "mask", "label": "子网掩码", "type": "text", "required": True,
             "placeholder": "255.255.255.0"},
            {"name": "next_hop", "label": "下一跳", "type": "text", "required": True,
             "placeholder": "192.168.1.1"},
        ],
        "command_template": [
            "ip route-static {{destination}} {{mask}} {{next_hop}}",
        ],
        "verify": ["display ip routing-table"],
    },
    "ospf": {
        "name": "配置 OSPF（基础）",
        "icon": "&#x1F310;",
        "category": "路由",
        "roles": ["router", "l3_switch"],
        "device_types": ["router", "l3_switch"],
        "trigger": ["ospf"],
        "fields": [
            {"name": "process_id", "label": "进程号", "type": "number", "default": "1",
             "required": True, "placeholder": "1"},
            {"name": "router_id", "label": "Router ID", "type": "text", "required": True,
             "placeholder": "1.1.1.1"},
            {"name": "area", "label": "区域 ID", "type": "text", "default": "0",
             "required": True, "placeholder": "0"},
            {"name": "network", "label": "宣告网络", "type": "text", "required": True,
             "placeholder": "192.168.1.0"},
            {"name": "wildcard", "label": "反掩码", "type": "text", "default": "0.0.0.255",
             "required": True, "placeholder": "0.0.0.255"},
        ],
        "command_template": [
            "ospf {{process_id}} router-id {{router_id}}",
            "area {{area}}",
            "network {{network}} {{wildcard}}",
        ],
        "verify": ["display ospf peer"],
    },
    "bgp": {
        "name": "配置 BGP（基础）",
        "icon": "&#x1F6E4;",
        "category": "路由",
        "roles": ["router", "l3_switch"],
        "device_types": ["router", "l3_switch"],
        "trigger": ["bgp"],
        "fields": [
            {"name": "local_as", "label": "本地 AS 号", "type": "number", "required": True,
             "placeholder": "65001"},
            {"name": "peer_ip", "label": "对等体 IP", "type": "text", "required": True,
             "placeholder": "192.168.1.2"},
            {"name": "peer_as", "label": "对等体 AS 号", "type": "number", "required": True,
             "placeholder": "65002"},
        ],
        "command_template": [
            "bgp {{local_as}}",
            "peer {{peer_ip}} as-number {{peer_as}}",
        ],
        "verify": ["display bgp peer"],
    },
    "isis": {
        "name": "配置 ISIS（基础）",
        "icon": "&#x1F6E4;",
        "category": "路由",
        "roles": ["router", "l3_switch"],
        "device_types": ["router", "l3_switch"],
        "trigger": ["isis"],
        "fields": [
            {"name": "process_id", "label": "进程号", "type": "number", "default": "1",
             "required": True, "placeholder": "1"},
            {"name": "net", "label": "NET 地址", "type": "text", "required": True,
             "placeholder": "49.0001.0010.0100.1001.00"},
        ],
        "command_template": [
            "isis {{process_id}}",
            "network-entity {{net}}",
        ],
        "verify": ["display isis peer"],
    },
    "rip": {
        "name": "配置 RIP（基础）",
        "icon": "&#x1F6E4;",
        "category": "路由",
        "roles": ["router", "l3_switch"],
        "device_types": ["router", "l3_switch"],
        "trigger": ["rip"],
        "fields": [
            {"name": "process_id", "label": "进程号", "type": "number", "default": "1",
             "required": True, "placeholder": "1"},
            {"name": "network", "label": "宣告网络", "type": "text", "required": True,
             "placeholder": "192.168.1.0"},
        ],
        "command_template": [
            "rip {{process_id}}",
            "version 2",
            "network {{network}}",
        ],
        "verify": ["display rip"],
    },

    # ===================== 二层 =====================
    "vlan_create": {
        "name": "创建 VLAN",
        "icon": "&#x1F5CE;",
        "category": "二层",
        "roles": ["l3_switch", "switch"],
        "device_types": ["l3_switch", "switch", "firewall", "ac"],
        "trigger": ["vlan"],
        "fields": [
            {"name": "vlan_id", "label": "VLAN ID", "type": "number", "required": True,
             "placeholder": "10"},
            {"name": "description", "label": "VLAN 描述", "type": "text", "optional": True,
             "placeholder": "可选"},
        ],
        "command_template": [
            "vlan {{vlan_id}}",
            "description {{description}}",
        ],
        "verify": ["display vlan"],
    },
    "vlanif_ip": {
        "name": "配置 VLANIF 接口 IP",
        "icon": "&#x1F517;",
        "category": "二层",
        "roles": ["l3_switch"],
        "device_types": ["l3_switch", "switch", "firewall", "ac"],
        "trigger": ["interface"],
        "fields": [
            {"name": "vlan_id", "label": "VLAN ID", "type": "number", "required": True,
             "placeholder": "10"},
            {"name": "ip_address", "label": "IP 地址", "type": "text", "required": True,
             "placeholder": "192.168.10.1"},
            {"name": "subnet_mask", "label": "子网掩码", "type": "text", "required": True,
             "placeholder": "255.255.255.0"},
        ],
        "command_template": [
            "interface Vlanif {{vlan_id}}",
            "ip address {{ip_address}} {{subnet_mask}}",
        ],
        "verify": ["display ip interface brief"],
    },
    "trunk_port": {
        "name": "配置 Trunk 干道",
        "icon": "&#x1F517;",
        "category": "二层",
        "roles": ["l3_switch", "switch"],
        "device_types": ["l3_switch", "switch"],
        "trigger": ["interface"],
        "fields": [
            {"name": "interface", "label": "接口", "type": "text", "required": True,
             "placeholder": "GigabitEthernet0/0/1"},
            {"name": "vlans", "label": "放行 VLAN（如 10 20 或 all）", "type": "text",
             "required": True, "placeholder": "10 20"},
        ],
        "command_template": [
            "interface {{interface}}",
            "port link-type trunk",
            "port trunk allow-pass vlan {{vlans}}",
        ],
        "verify": ["display port vlan"],
    },
    "access_port": {
        "name": "配置 Access 接入",
        "icon": "&#x1F517;",
        "category": "二层",
        "roles": ["l3_switch", "switch"],
        "device_types": ["l3_switch", "switch"],
        "trigger": ["interface"],
        "fields": [
            {"name": "interface", "label": "接口", "type": "text", "required": True,
             "placeholder": "GigabitEthernet0/0/2"},
            {"name": "vlan_id", "label": "所属 VLAN", "type": "number", "required": True,
             "placeholder": "10"},
        ],
        "command_template": [
            "interface {{interface}}",
            "port link-type access",
            "port default vlan {{vlan_id}}",
        ],
        "verify": ["display port vlan"],
    },
    "stp": {
        "name": "配置 STP 模式",
        "icon": "&#x1F503;",
        "category": "二层",
        "roles": ["l3_switch", "switch"],
        "device_types": ["l3_switch", "switch"],
        "trigger": ["stp"],
        "fields": [
            {"name": "mode", "label": "STP 模式", "type": "select", "default": "mstp",
             "options": [
                 {"value": "mstp", "label": "MSTP"},
                 {"value": "rstp", "label": "RSTP"},
                 {"value": "stp", "label": "STP"},
             ]},
            {"name": "priority", "label": "桥优先级(可选)", "type": "number", "optional": True,
             "placeholder": "4096"},
        ],
        "command_template": [
            "stp mode {{mode}}",
            "stp priority {{priority}}",
        ],
        "verify": ["display stp brief"],
    },
    "eth_trunk": {
        "name": "创建 Eth-Trunk 链路聚合",
        "icon": "&#x1F4D0;",
        "category": "二层",
        "roles": ["l3_switch", "switch"],
        "device_types": ["l3_switch", "switch"],
        "trigger": ["interface"],
        "fields": [
            {"name": "trunk_id", "label": "Eth-Trunk ID", "type": "number", "required": True,
             "placeholder": "1"},
            {"name": "member_interface", "label": "成员接口", "type": "text", "required": True,
             "placeholder": "GigabitEthernet0/0/1"},
            {"name": "vlans", "label": "放行 VLAN", "type": "text", "optional": True,
             "placeholder": "可选，如 10 20"},
        ],
        "command_template": [
            "interface Eth-Trunk {{trunk_id}}",
            "trunkport {{member_interface}}",
            "port link-type trunk",
            "port trunk allow-pass vlan {{vlans}}",
        ],
        "verify": ["display eth-trunk"],
    },
    "vrrp": {
        "name": "配置 VRRP 冗余网关",
        "icon": "&#x1F504;",
        "category": "二层",
        "roles": ["l3_switch"],
        "device_types": ["l3_switch"],
        "trigger": ["interface"],
        "fields": [
            {"name": "interface", "label": "接口（通常为 VLANIF）", "type": "text", "required": True,
             "placeholder": "Vlanif10"},
            {"name": "vrid", "label": "VRRP 组 ID", "type": "number", "default": "1",
             "required": True, "placeholder": "1"},
            {"name": "virtual_ip", "label": "虚拟 IP", "type": "text", "required": True,
             "placeholder": "192.168.10.254"},
        ],
        "command_template": [
            "interface {{interface}}",
            "vrrp vrid {{vrid}} virtual-ip {{virtual_ip}}",
        ],
        "verify": ["display vrrp brief"],
    },

    # ===================== 安全 =====================
    "acl_rule": {
        "name": "添加 ACL 规则",
        "icon": "&#x1F6D1;",
        "category": "安全",
        "roles": ["router", "l3_switch"],
        "device_types": ["router", "l3_switch", "firewall"],
        "trigger": ["acl"],
        "fields": [
            {"name": "acl_number", "label": "ACL 编号", "type": "number", "default": "2000",
             "required": True, "placeholder": "2000"},
            {"name": "rule_id", "label": "规则序号", "type": "number", "default": "5",
             "placeholder": "5"},
            {"name": "action", "label": "动作", "type": "select", "default": "permit",
             "options": [
                 {"value": "permit", "label": "permit 允许"},
                 {"value": "deny", "label": "deny 拒绝"},
             ]},
            {"name": "source", "label": "源网络", "type": "text", "required": True,
             "placeholder": "192.168.10.0"},
            {"name": "wildcard", "label": "源反掩码", "type": "text", "default": "0.0.0.255",
             "required": True, "placeholder": "0.0.0.255"},
        ],
        "command_template": [
            "acl {{acl_number}}",
            "rule {{rule_id}} {{action}} source {{source}} {{wildcard}}",
            "quit",
        ],
        "verify": ["display acl {{acl_number}}"],
    },
    "ssh_user": {
        "name": "创建 SSH 用户",
        "icon": "&#x1F510;",
        "category": "安全",
        "roles": ["router", "l3_switch", "switch"],
        "device_types": ["router", "l3_switch", "switch", "firewall", "ac", "cloud"],
        "trigger": ["aaa"],
        "fields": [
            {"name": "username", "label": "用户名", "type": "text", "required": True,
             "placeholder": "admin"},
            {"name": "password", "label": "密码", "type": "password", "required": True,
             "placeholder": "请输入密码"},
            {"name": "level", "label": "权限级别", "type": "number", "default": "3",
             "placeholder": "3"},
        ],
        "command_template": [
            "aaa",
            "local-user {{username}} password cipher {{password}}",
            "local-user {{username}} service-type ssh",
            "local-user {{username}} level {{level}}",
            "quit",
        ],
        "verify": ["display local-user"],
    },
    "telnet_server": {
        "name": "开启 Telnet 服务",
        "icon": "&#x1F5A5;",
        "category": "安全",
        "roles": ["router", "l3_switch", "switch"],
        "device_types": ["router", "l3_switch", "switch", "firewall", "ac"],
        "trigger": ["telnet"],
        "fields": [
            {"name": "vty_level", "label": "VTY 用户级别", "type": "number", "default": "3",
             "placeholder": "3"},
        ],
        "command_template": [
            "telnet server enable",
            "user-interface vty 0 4",
            "authentication-mode aaa",
            "user privilege level {{vty_level}}",
            "protocol inbound telnet",
            "quit",
        ],
        "verify": ["display telnet server status"],
    },
    "firewall_zone": {
        "name": "配置安全域",
        "icon": "&#x1F6E1;",
        "category": "安全",
        "roles": [],
        "device_types": ["firewall"],
        "trigger": ["firewall"],
        "fields": [
            {"name": "zone_name", "label": "安全域", "type": "select", "default": "trust",
             "options": [
                 {"value": "trust", "label": "trust 信任域"},
                 {"value": "untrust", "label": "untrust 非信任域"},
                 {"value": "dmz", "label": "dmz"},
             ]},
            {"name": "priority", "label": "优先级", "type": "number", "default": "85",
             "placeholder": "85"},
            {"name": "interface", "label": "加入的接口", "type": "text", "required": True,
             "placeholder": "GigabitEthernet0/0/1"},
        ],
        "command_template": [
            "firewall zone {{zone_name}}",
            "set priority {{priority}}",
            "add interface {{interface}}",
            "quit",
        ],
        "verify": ["display zone"],
    },
    "firewall_policy": {
        "name": "配置安全策略",
        "icon": "&#x1F6E1;",
        "category": "安全",
        "roles": [],
        "device_types": ["firewall"],
        "trigger": ["security-policy"],
        "fields": [
            {"name": "rule_name", "label": "规则名", "type": "text", "required": True,
             "placeholder": "allow_trust_to_untrust"},
            {"name": "source_zone", "label": "源安全域", "type": "text", "required": True,
             "placeholder": "trust"},
            {"name": "destination_zone", "label": "目的安全域", "type": "text", "required": True,
             "placeholder": "untrust"},
            {"name": "source_ip", "label": "源地址", "type": "text", "required": True,
             "placeholder": "192.168.10.0"},
            {"name": "source_mask", "label": "源掩码", "type": "text", "required": True,
             "placeholder": "255.255.255.0"},
            {"name": "destination_ip", "label": "目的地址", "type": "text", "required": True,
             "placeholder": "0.0.0.0"},
            {"name": "destination_mask", "label": "目的掩码", "type": "text", "required": True,
             "placeholder": "0.0.0.0"},
            {"name": "action", "label": "动作", "type": "select", "default": "permit",
             "options": [
                 {"value": "permit", "label": "permit 允许"},
                 {"value": "deny", "label": "deny 拒绝"},
             ]},
        ],
        "command_template": [
            "security-policy",
            "rule name {{rule_name}}",
            "source-zone {{source_zone}}",
            "destination-zone {{destination_zone}}",
            "source-address {{source_ip}} {{source_mask}}",
            "destination-address {{destination_ip}} {{destination_mask}}",
            "action {{action}}",
            "quit",
        ],
        "verify": ["display security-policy"],
    },
    "firewall_nat": {
        "name": "配置 NAT Server 映射",
        "icon": "&#x1F310;",
        "category": "安全",
        "roles": [],
        "device_types": ["firewall"],
        "trigger": ["nat"],
        "fields": [
            {"name": "global_ip", "label": "公网 IP", "type": "text", "required": True,
             "placeholder": "202.100.1.10"},
            {"name": "global_port", "label": "公网端口", "type": "number", "default": "80",
             "placeholder": "80"},
            {"name": "inside_ip", "label": "内网 IP", "type": "text", "required": True,
             "placeholder": "192.168.10.20"},
            {"name": "inside_port", "label": "内网端口", "type": "number", "default": "80",
             "placeholder": "80"},
        ],
        "command_template": [
            "nat server protocol tcp global {{global_ip}} {{global_port}} inside {{inside_ip}} {{inside_port}}",
        ],
        "verify": ["display nat server"],
    },

    # ===================== 服务 =====================
    "dhcp_server": {
        "name": "开启 DHCP 地址池",
        "icon": "&#x1F4E6;",
        "category": "服务",
        "roles": ["router", "l3_switch"],
        "device_types": ["router", "l3_switch"],
        "trigger": ["dhcp"],
        "fields": [
            {"name": "pool_name", "label": "地址池名", "type": "text", "required": True,
             "placeholder": "vlan10_pool"},
            {"name": "gateway", "label": "网关", "type": "text", "required": True,
             "placeholder": "192.168.10.1"},
            {"name": "network", "label": "网络号", "type": "text", "required": True,
             "placeholder": "192.168.10.0"},
            {"name": "mask", "label": "掩码", "type": "text", "required": True,
             "placeholder": "255.255.255.0"},
            {"name": "dns", "label": "DNS", "type": "text", "optional": True,
             "placeholder": "可选，如 8.8.8.8"},
        ],
        "command_template": [
            "dhcp enable",
            "ip pool {{pool_name}}",
            "gateway-list {{gateway}}",
            "network {{network}} mask {{mask}}",
            "dns-list {{dns}}",
        ],
        "verify": ["display ip pool"],
    },
    "snmp": {
        "name": "开启 SNMP（v2c）",
        "icon": "&#x1F4E1;",
        "category": "监控",
        "roles": ["router", "l3_switch", "switch"],
        "device_types": ["router", "l3_switch", "switch", "firewall", "ac", "cloud"],
        "trigger": ["snmp-agent"],
        "fields": [
            {"name": "community", "label": "只读团体字", "type": "text", "required": True,
             "placeholder": "public"},
        ],
        "command_template": [
            "snmp-agent",
            "snmp-agent sys-info version v2c",
            "snmp-agent community read {{community}}",
        ],
        "verify": ["display snmp-agent community"],
    },
    "ntp": {
        "name": "配置 NTP 时钟同步",
        "icon": "&#x1F550;",
        "category": "监控",
        "roles": ["router", "l3_switch", "switch"],
        "device_types": ["router", "l3_switch", "switch", "firewall", "ac", "cloud"],
        "trigger": ["ntp-service"],
        "fields": [
            {"name": "server_ip", "label": "NTP 服务器", "type": "text", "required": True,
             "placeholder": "192.168.1.100"},
        ],
        "command_template": [
            "ntp-service unicast-server {{server_ip}}",
        ],
        "verify": ["display ntp-service sessions"],
    },

    # ===================== 无线（AC） =====================
    "wlan_ac": {
        "name": "配置 WLAN（SSID/安全/VAP）",
        "icon": "&#x1F4F6;",
        "category": "无线",
        "roles": [],
        "device_types": ["ac"],
        "trigger": ["wlan"],
        "fields": [
            {"name": "ssid_profile", "label": "SSID 模板名", "type": "text", "required": True,
             "placeholder": "ssid-corp"},
            {"name": "ssid", "label": "无线 SSID", "type": "text", "required": True,
             "placeholder": "Corp-WiFi"},
            {"name": "security_profile", "label": "安全模板名", "type": "text", "required": True,
             "placeholder": "sec-corp"},
            {"name": "psk", "label": "WPA2 预共享密钥", "type": "password", "required": True,
             "placeholder": "请输入密码"},
            {"name": "vap_profile", "label": "VAP 模板名", "type": "text", "required": True,
             "placeholder": "vap-corp"},
            {"name": "vlan_id", "label": "业务 VLAN", "type": "number", "required": True,
             "placeholder": "10"},
            {"name": "ap_group", "label": "AP 组名", "type": "text", "required": True,
             "placeholder": "ap-group1"},
        ],
        "command_template": [
            "wlan",
            "ssid-profile name {{ssid_profile}}",
            "ssid {{ssid}}",
            "quit",
            "security-profile name {{security_profile}}",
            "security wpa2 psk pass-phrase {{psk}} aes",
            "quit",
            "vap-profile name {{vap_profile}}",
            "service-vlan vlan-id {{vlan_id}}",
            "ssid-profile {{ssid_profile}}",
            "security-profile {{security_profile}}",
            "quit",
            "ap-group name {{ap_group}}",
            "vap-profile {{vap_profile}} wlan 1",
            "quit",
        ],
        "verify": ["display vap ssid"],
    },
}


# ---------------------------------------------------------------------------
# 查询接口
# ---------------------------------------------------------------------------
def get_catalog_entries(role: Optional[str] = None,
                        device_type: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    返回目录条目列表（可选按设备角色 / 设备类型过滤）。
    device_type 过滤使用条目的 device_types 列表；role 过滤兼容旧字段 roles。
    """
    entries = []
    for key, entry in COMMAND_CATALOG.items():
        if role and role not in entry.get("roles", []):
            continue
        if device_type and device_type not in entry.get("device_types", []):
            continue
        entries.append({
            "key": key,
            "name": entry["name"],
            "icon": entry.get("icon", ""),
            "category": entry.get("category", ""),
            "roles": entry.get("roles", []),
            "device_types": entry.get("device_types", []),
            "trigger": entry.get("trigger", []),
            "fields": entry.get("fields", []),
        })
    # 按分类再按名称排序，方便前端分组展示
    entries.sort(key=lambda e: (e.get("category", ""), e.get("name", "")))
    return entries


def get_catalog_entry(key: str) -> Optional[Dict[str, Any]]:
    return COMMAND_CATALOG.get(key)


def get_categories() -> List[str]:
    """返回所有分类名（去重，保持首次出现顺序）。"""
    seen: List[str] = []
    for entry in COMMAND_CATALOG.values():
        cat = entry.get("category", "")
        if cat and cat not in seen:
            seen.append(cat)
    return seen


# ---------------------------------------------------------------------------
# 渲染（通用，零 Python builder）
# ---------------------------------------------------------------------------
def render_catalog_commands(key: str, params: Dict[str, Any]) -> Tuple[List[str], List[str], List[str]]:
    """
    把目录条目的 command_template 用 params 渲染为命令列表。

    Returns:
        (commands, warnings, errors)
    """
    entry = get_catalog_entry(key)
    if not entry:
        return [], [], [f"未找到命令目录条目: {key}"]

    fields = entry.get("fields", [])
    optional_names = {f["name"] for f in fields if f.get("optional")}

    # 组装变量：可选字段留空则跳过（其所在行渲染后会被丢弃）；
    # 必填字段留空且无默认值则报错。
    vars_dict: Dict[str, str] = {}
    errors: List[str] = []
    for field in fields:
        name = field["name"]
        value = params.get(name, "")
        if value in (None, ""):
            if field.get("optional"):
                continue
            if field.get("default"):
                value = field["default"]
            else:
                errors.append(f"必填项「{field.get('label', name)}」未填写")
                continue
        vars_dict[name] = str(value)

    if errors:
        return [], [], errors

    raw_lines = entry.get("command_template", [])
    cleaned, warnings, _ = clean_commands(raw_lines)
    rendered, undefined = render_commands(cleaned, {}, vars_dict)

    # 丢弃仍含未定义占位符的行（来自可选字段留空，属正常）
    commands = [c for c in rendered if "{{" not in c]

    # 仅当未定义变量不属于「有意留空的可选字段」时才报错
    undefined = [u for u in undefined if u not in optional_names]
    if undefined:
        errors.append(f"存在未定义变量: {', '.join(undefined)}")
    if not commands:
        errors.append("渲染后没有可用命令（请检查必填项）")

    return commands, warnings, errors


def render_catalog_verify(key: str, params: Dict[str, Any]) -> List[str]:
    """渲染验证命令（同样做变量替换，但不做严格校验）。"""
    entry = get_catalog_entry(key)
    if not entry:
        return []
    vars_dict = {k: str(v) for k, v in (params or {}).items() if v not in (None, "")}
    out = []
    for cmd in entry.get("verify", []):
        rendered, _ = render_commands([cmd], {}, vars_dict)
        if rendered and "{{" not in rendered[0]:
            out.append(rendered[0])
    return out
