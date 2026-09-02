"""
config_builder.py - 配置模板命令生成器（声明式）
=================================================
单一数据源：每个模板 = fields（表单定义） + command_template（命令模板）。
命令生成由通用模板渲染器完成，无需为每条命令编写 Python 函数。

模板语法（command_template 每行一条 VRP 命令）：
  * {{var}}            变量替换；变量为空（None/""/未传）则整行丢弃
  * ?cond: TEXT        条件行：仅当 cond 为真时输出 TEXT（TEXT 内可含 {{var}}）
  * {{?cond: text}}    内联条件：cond 为真替换为 text，否则替换为空（text 内可含 {{var}}，支持嵌套）
  * cond 语法：var / !var / var==value / var!=value / var>value / var<value / var>=value / var<=value
  * 必填由模板的 required 列表 + fields 的 required 字段共同决定（渲染前校验，缺失则返回空）
"""

import re
from typing import List, Dict, Any, Optional


# ---------------------------------------------------------------------------
# 模板渲染引擎（通用命令生成器）
# ---------------------------------------------------------------------------

class _DropLine(Exception):
    """内部信号：某变量为空，需要丢弃当前行。"""
    pass


_TRUTHY_FALSE = {"false", "0", "off", "no", "none", "null", ""}


def _truthy(val) -> bool:
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return val != 0
    s = str(val).strip().lower()
    return s not in _TRUTHY_FALSE


def _eval_cond(cond: str, params: dict) -> bool:
    """计算条件表达式：var / !var / var==v / var!=v / var>v / var<v / var>=v / var<=v。"""
    cond = cond.strip()
    if cond.startswith("!"):
        return not _truthy(params.get(cond[1:].strip()))
    for op in ("==", "!=", ">=", "<=", ">", "<"):
        if op in cond:
            name, _, val = cond.partition(op)
            name = name.strip()
            val = val.strip()
            left = params.get(name, "")
            try:
                if op in ("==", "!="):
                    return (str(left) == val) if op == "==" else (str(left) != val)
                lv, rv = float(left), float(val)
                return {">": lv > rv, "<": lv < rv, ">=": lv >= rv, "<=": lv <= rv}[op]
            except (ValueError, TypeError):
                return False
    return _truthy(params.get(cond))


def _match_brace(text: str, start: int) -> Optional[int]:
    """从 text[start:start+2]=='{{' 开始，找到匹配的 '}}' 结束下标。"""
    if not text[start:start + 2] == "{{":
        return None
    depth = 1
    i = start + 2
    n = len(text)
    while i < n and depth > 0:
        if text[i:i + 2] == "{{":
            depth += 1
            i += 2
        elif text[i:i + 2] == "}}":
            depth -= 1
            i += 2
        else:
            i += 1
    return i if depth == 0 else None


def _expand(text: str, params: dict) -> str:
    """展开一行文本中的所有 {{...}}（变量 / 内联条件），变量为空抛 _DropLine 由上层决定丢行。"""
    out = []
    i, n = 0, len(text)
    while i < n:
        if text[i:i + 2] == "{{":
            j = _match_brace(text, i)
            if j is None:
                out.append(text[i:])
                break
            inner = text[i + 2:j - 2]
            seg = _expand_inner(inner, params)
            if seg is None:
                raise _DropLine()
            out.append(seg)
            i = j
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _expand_inner(inner: str, params: dict) -> Optional[str]:
    """处理单段 inner（已去掉外层 {{ }}）。"""
    if inner.startswith("?"):
        rest = inner[1:]
        idx = rest.find(":")
        if idx == -1:
            return ""
        cond = rest[:idx].strip()
        body = rest[idx + 1:]
        if _eval_cond(cond, params):
            try:
                return _expand(body, params)
            except _DropLine:
                return ""  # 内联内的变量为空，仅丢弃该内联段，不影响外层行
        return ""
    name = inner.strip()
    val = params.get(name, "")
    if val in (None, ""):
        return None
    return str(val)


def _render_line(line: str, params: dict) -> Optional[str]:
    s = line.strip()
    if not s:
        return None
    if s.startswith("?"):
        m = re.match(r"^\?(.+?):\s*(.*)$", s, re.S)
        if m:
            cond, text = m.group(1).strip(), m.group(2)
            if not _eval_cond(cond, params):
                return None
            try:
                return _expand(text, params)
            except _DropLine:
                return None
    try:
        result = _expand(s, params)
    except _DropLine:
        return None
    # 整行内联条件（如 {{?cond: text}}）在条件为假时展开为空串，应整行丢弃
    if result == "" and s.startswith("{{?"):
        return None
    # 整行内联条件展开后剥掉前导空格：条件体里 ": text" 的那个空格是语法
    # 分隔符，行中内联（如 ospf {{id}}{{?rid: router-id ...}}）依赖它做词间
    # 分隔，但整行展开后它是多余的前导空白
    if s.startswith("{{?"):
        result = result.strip()
    return result


def render_template_commands(template: List[str], params: dict) -> List[str]:
    """将命令模板渲染为 VRP 命令列表。"""
    out = []
    for line in template:
        r = _render_line(line, params or {})
        if r is not None:
            out.append(r)
    return out


# ---------------------------------------------------------------------------
# 配置模板定义（单一数据源：fields + command_template + required）
# ---------------------------------------------------------------------------

CONFIG_TEMPLATES: Dict[str, Dict[str, Any]] = {
    # ========== 组合业务方案 ==========
    "branch_ospf": {
        "name": "分支接口 + OSPF",
        "icon": "&#x1F5A7;",
        "roles": ["router", "l3_switch"],
        "category": "solution",
        "required": ["interface", "ip_address", "subnet_mask", "process_id", "network", "wildcard"],
        "fields": [
            {"name": "interface", "label": "业务接口", "type": "text", "required": True,
             "placeholder": "GigabitEthernet0/0/1"},
            {"name": "ip_address", "label": "接口 IP", "type": "text", "required": True,
             "placeholder": "10.0.12.1"},
            {"name": "subnet_mask", "label": "子网掩码", "type": "text", "required": True,
             "placeholder": "255.255.255.0"},
            {"name": "process_id", "label": "OSPF 进程号", "type": "number", "default": "1",
             "required": True, "placeholder": "1"},
            {"name": "router_id", "label": "Router ID", "type": "text",
             "placeholder": "1.1.1.1"},
            {"name": "area", "label": "区域 ID", "type": "text", "default": "0",
             "required": True, "placeholder": "0"},
            {"name": "network", "label": "宣告网络", "type": "text", "required": True,
             "placeholder": "10.0.12.0", "auto_fill_target": "wildcard",
             "auto_fill_type": "network_to_wildcard"},
            {"name": "wildcard", "label": "反掩码", "type": "text", "default": "0.0.0.255",
             "required": True, "placeholder": "0.0.0.255"},
        ],
        "command_template": [
            "interface {{interface}}",
            "ip address {{ip_address}} {{subnet_mask}}",
            "quit",
            "ospf {{process_id}}{{?router_id: router-id {{router_id}}}}",
            "area {{area}}",
            "network {{network}} {{wildcard}}",
            "quit",
            "quit",
        ],
    },
    "vlan_gateway_dhcp": {
        "name": "VLAN 网关 + DHCP",
        "icon": "&#x1F3E2;",
        "roles": ["l3_switch"],
        "category": "solution",
        "required": ["vlan_id", "access_interface", "gateway", "subnet_mask", "pool_name", "network"],
        "fields": [
            {"name": "vlan_id", "label": "VLAN ID", "type": "number", "required": True,
             "placeholder": "10"},
            {"name": "access_interface", "label": "Access 接口", "type": "text", "required": True,
             "placeholder": "GigabitEthernet0/0/1"},
            {"name": "gateway", "label": "网关地址", "type": "text", "required": True,
             "placeholder": "192.168.10.1"},
            {"name": "subnet_mask", "label": "子网掩码", "type": "text", "required": True,
             "placeholder": "255.255.255.0"},
            {"name": "pool_name", "label": "地址池名称", "type": "text", "required": True,
             "placeholder": "vlan10"},
            {"name": "network", "label": "地址池网络", "type": "text", "required": True,
             "placeholder": "192.168.10.0"},
            {"name": "dns", "label": "DNS 服务器", "type": "text", "default": "8.8.8.8",
             "placeholder": "8.8.8.8"},
        ],
        "command_template": [
            "vlan {{vlan_id}}",
            "quit",
            "interface {{access_interface}}",
            "port link-type access",
            "port default vlan {{vlan_id}}",
            "quit",
            "dhcp enable",
            "ip pool {{pool_name}}",
            "network {{network}} mask {{subnet_mask}}",
            "gateway-list {{gateway}}",
            "{{?dns: dns-list {{dns}}}}",
            "quit",
            "interface Vlanif {{vlan_id}}",
            "ip address {{gateway}} {{subnet_mask}}",
            "dhcp select global",
            "quit",
        ],
    },
    "edge_easy_ip_nat": {
        "name": "出口 ACL + Easy-IP NAT",
        "icon": "&#x1F310;",
        "roles": ["router"],
        "category": "solution",
        "required": ["acl_number", "inside_network", "inside_wildcard", "outside_interface"],
        "fields": [
            {"name": "acl_number", "label": "ACL 编号", "type": "number", "default": "2000",
             "required": True, "placeholder": "2000"},
            {"name": "inside_network", "label": "内网地址", "type": "text", "required": True,
             "placeholder": "192.168.10.0", "auto_fill_target": "inside_wildcard",
             "auto_fill_type": "network_to_wildcard"},
            {"name": "inside_wildcard", "label": "内网反掩码", "type": "text", "default": "0.0.0.255",
             "required": True, "placeholder": "0.0.0.255"},
            {"name": "outside_interface", "label": "出口接口", "type": "text", "required": True,
             "placeholder": "GigabitEthernet0/0/0"},
        ],
        "command_template": [
            "acl number {{acl_number}}",
            "rule permit source {{inside_network}} {{inside_wildcard}}",
            "quit",
            "interface {{outside_interface}}",
            "nat outbound {{acl_number}}",
            "quit",
        ],
    },
    # ========== 路由相关 ==========
    "interface_ip": {
        "name": "配置接口 IP",
        "icon": "&#x1F310;",
        "roles": ["router", "l3_switch", "switch"],
        "category": "routing",
        "required": ["interface", "ip_address", "subnet_mask"],
        "fields": [
            {"name": "interface", "label": "接口", "type": "text", "required": True,
             "placeholder": "GigabitEthernet0/0/0"},
            {"name": "ip_address", "label": "IP 地址", "type": "text", "required": True,
             "placeholder": "192.168.1.1"},
            {"name": "subnet_mask", "label": "子网掩码", "type": "text", "required": True,
             "placeholder": "255.255.255.0"},
        ],
        "command_template": [
            "interface {{interface}}",
            "ip address {{ip_address}} {{subnet_mask}}",
            "quit",
        ],
    },
    "static_route": {
        "name": "配置静态路由",
        "icon": "&#x1F6E4;",
        "roles": ["router", "l3_switch"],
        "category": "routing",
        "required": ["destination", "mask", "next_hop"],
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
    },
    "ospf": {
        "name": "配置 OSPF",
        "icon": "&#x1F310;",
        "field_groups": [
            {"id": "basic", "name": "基础配置", "icon": "&#x1F4CB;"},
            {"id": "network", "name": "网络宣告", "icon": "&#x1F310;"},
            {"id": "interface", "name": "接口参数", "icon": "&#x1F517;"},
            {"id": "auth", "name": "认证配置", "icon": "&#x1F512;"},
            {"id": "advanced", "name": "高级配置", "icon": "&#x2699;"},
        ],
        "roles": ["router", "l3_switch"],
        "category": "routing",
        "required": ["process_id", "network", "wildcard"],
        "fields": [
            {"name": "process_id", "label": "进程号", "type": "number", "default": "1",
             "required": True, "placeholder": "1", "group": "basic"},
            {"name": "router_id", "label": "Router ID", "type": "text",
             "placeholder": "1.1.1.1", "group": "basic"},
            {"name": "area", "label": "区域 ID", "type": "text", "default": "0",
             "required": True, "placeholder": "0", "group": "basic"},
            {"name": "area_type", "label": "区域类型", "type": "select", "default": "normal",
             "options": [
                 {"value": "normal", "label": "普通区域"},
                 {"value": "stub", "label": "Stub 区域"},
                 {"value": "stub_no_summary", "label": "Totally Stub"},
                 {"value": "nssa", "label": "NSSA 区域"},
                 {"value": "nssa_no_summary", "label": "Totally NSSA"},
             ], "group": "basic"},
            {"name": "network", "label": "网络地址", "type": "text", "required": True,
             "placeholder": "192.168.56.0", "group": "network",
             "auto_fill_target": "wildcard", "auto_fill_type": "network_to_wildcard"},
            {"name": "wildcard", "label": "反掩码", "type": "text", "required": True,
             "placeholder": "0.0.0.255", "group": "network"},
            {"name": "network2", "label": "网络地址 2", "type": "text",
             "placeholder": "10.0.0.0", "group": "network",
             "auto_fill_target": "wildcard2", "auto_fill_type": "network_to_wildcard"},
            {"name": "wildcard2", "label": "反掩码 2", "type": "text",
             "placeholder": "0.0.0.255", "group": "network"},
            {"name": "network3", "label": "网络地址 3", "type": "text",
             "placeholder": "172.16.0.0", "group": "network",
             "auto_fill_target": "wildcard3", "auto_fill_type": "network_to_wildcard"},
            {"name": "wildcard3", "label": "反掩码 3", "type": "text",
             "placeholder": "0.0.0.255", "group": "network"},
            {"name": "interface", "label": "接口名", "type": "text",
             "placeholder": "GigabitEthernet0/0/0", "group": "interface"},
            {"name": "cost", "label": "接口开销", "type": "number",
             "placeholder": "1", "group": "interface"},
            {"name": "hello_interval", "label": "Hello 间隔 (秒)", "type": "number",
             "placeholder": "10", "group": "interface"},
            {"name": "dead_interval", "label": "Dead 间隔 (秒)", "type": "number",
             "placeholder": "40", "group": "interface"},
            {"name": "dr_priority", "label": "DR 优先级", "type": "number",
             "placeholder": "1", "group": "interface"},
            {"name": "network_type", "label": "网络类型", "type": "select", "default": "default",
             "options": [
                 {"value": "default", "label": "默认"},
                 {"value": "broadcast", "label": "广播"},
                 {"value": "nbma", "label": "NBMA"},
                 {"value": "p2p", "label": "点对点"},
                 {"value": "p2mp", "label": "点对多点"},
             ], "group": "interface"},
            {"name": "auth_type", "label": "认证类型", "type": "select", "default": "none",
             "options": [
                 {"value": "none", "label": "无认证"},
                {"value": "simple", "label": "简单认证"},
                 {"value": "md5", "label": "MD5 认证"},
             ], "group": "auth"},
            {"name": "auth_key", "label": "认证密码", "type": "password",
             "placeholder": "Huawei@123", "group": "auth"},
            {"name": "auth_key_id", "label": "MD5 密钥 ID", "type": "number", "default": "1",
             "placeholder": "1", "group": "auth"},
            {"name": "silent_interface", "label": "静默接口", "type": "text",
             "placeholder": "GigabitEthernet0/0/1", "group": "advanced"},
            {"name": "default_route_advertise", "label": "通告默认路由", "type": "checkbox",
             "default": False, "group": "advanced"},
            {"name": "default_route_cost", "label": "默认路由开销", "type": "number",
             "placeholder": "1", "group": "advanced"},
            {"name": "reference_bandwidth", "label": "参考带宽 (Mbps)", "type": "number",
             "placeholder": "100", "group": "advanced"},
            {"name": "route_aggregation", "label": "路由聚合", "type": "text",
             "placeholder": "192.168.0.0 255.255.0.0", "group": "advanced"},
            {"name": "import_static", "label": "引入静态路由", "type": "checkbox",
             "default": False, "group": "advanced"},
            {"name": "import_connected", "label": "引入直连路由", "type": "checkbox",
             "default": False, "group": "advanced"},
            {"name": "import_bgp", "label": "引入 BGP", "type": "checkbox",
             "default": False, "group": "advanced"},
            {"name": "bfd_enable", "label": "启用 BFD", "type": "checkbox",
             "default": False, "group": "advanced"},
        ],
        "command_template": [
            "{{?bfd_enable: bfd}}",
            "{{?bfd_enable: quit}}",
            "ospf {{process_id}}{{?router_id: router-id {{router_id}}}}",
            "area {{area}}",
            "{{?area_type==stub: stub}}",
            "{{?area_type==stub_no_summary: stub no-summary}}",
            "{{?area_type==nssa: nssa}}",
            "{{?area_type==nssa_no_summary: nssa no-summary}}",
            "{{?auth_type==simple: authentication-mode simple cipher {{auth_key}}}}",
            "{{?auth_type==md5: authentication-mode md5 {{auth_key_id}} cipher {{auth_key}}}}",
            "network {{network}} {{wildcard}}",
            "{{?network2: network {{network2}} {{wildcard2}}}}",
            "{{?network3: network {{network3}} {{wildcard3}}}}",
            "{{?route_aggregation: abr-summary {{route_aggregation}}}}",
            "quit",
            "{{?silent_interface: silent-interface {{silent_interface}}}}",
            "{{?default_route_advertise: default-route-advertise}}{{?default_route_cost: cost {{default_route_cost}}}}",
            "{{?import_static: import-route static}}",
            "{{?import_connected: import-route direct}}",
            "{{?import_bgp: import-route bgp}}",
            "{{?reference_bandwidth: bandwidth-reference {{reference_bandwidth}}}}",
            "{{?bfd_enable: bfd all-interfaces enable}}",
            "quit",
            "{{?interface: interface {{interface}}}}",
            "{{?cost: ospf cost {{cost}}}}",
            "{{?hello_interval: ospf timer hello {{hello_interval}}}}",
            "{{?dead_interval: ospf timer dead {{dead_interval}}}}",
            "{{?dr_priority: ospf dr-priority {{dr_priority}}}}",
            "{{?network_type!=default: ospf network-type {{network_type}}}}",
            "{{?interface: quit}}",
        ],
    },
    "isis": {
        "name": "配置 IS-IS",
        "icon": "&#x1F310;",
        "roles": ["router", "l3_switch"],
        "category": "routing",
        "required": ["net_entity", "interface"],
        "fields": [
            {"name": "process_id", "label": "进程号", "type": "number", "default": "1",
             "required": True, "placeholder": "1"},
            {"name": "net_entity", "label": "NET 实体", "type": "text", "required": True,
             "placeholder": "49.0001.0000.0000.0001.00"},
            {"name": "is_level", "label": "级别", "type": "select", "default": "level-2",
             "options": [
                 {"value": "level-1", "label": "Level-1"},
                 {"value": "level-2", "label": "Level-1-2"},
                 {"value": "level-1-2", "label": "Level-1-2"},
             ]},
            {"name": "interface", "label": "接口", "type": "text", "required": True,
             "placeholder": "GigabitEthernet0/0/0"},
        ],
        "command_template": [
            "isis {{process_id}}",
            "network-entity {{net_entity}}",
            "is-level {{is_level}}",
            "quit",
            "interface {{interface}}",
            "isis enable {{process_id}}",
            "quit",
        ],
    },
    "bgp": {
        "name": "配置 BGP",
        "icon": "&#x1F310;",
        "roles": ["router", "l3_switch"],
        "category": "routing",
        "required": ["local_as", "peer_ip", "peer_as"],
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
        "command_template": [
            "bgp {{local_as}}",
            "{{?router_id: router-id {{router_id}}}}",
            "peer {{peer_ip}} as-number {{peer_as}}",
            "quit",
        ],
    },
    "rip": {
        "name": "配置 RIP",
        "icon": "&#x1F310;",
        "roles": ["router", "l3_switch"],
        "category": "routing",
        "required": ["network"],
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
        "command_template": [
            "rip {{process_id}}",
            "{{?version==2: version 2}}",
            "network {{network}}",
            "quit",
        ],
    },
    # ========== 交换相关 ==========
    "vlan": {
        "name": "配置 VLAN",
        "icon": "&#x1F4E6;",
        "roles": ["switch", "l3_switch"],
        "category": "switching",
        "required": ["vlan_id"],
        "fields": [
            {"name": "vlan_id", "label": "VLAN ID", "type": "number", "required": True,
             "placeholder": "10"},
            {"name": "description", "label": "描述", "type": "text",
             "placeholder": "Office"},
        ],
        "command_template": [
            "vlan {{vlan_id}}",
            "{{?description: description {{description}}}}",
            "quit",
        ],
    },
    "access_port": {
        "name": "配置 Access 接口",
        "icon": "&#x1F50C;",
        "roles": ["switch", "l3_switch"],
        "category": "switching",
        "required": ["interface", "vlan_id"],
        "fields": [
            {"name": "interface", "label": "接口", "type": "text", "required": True,
             "placeholder": "GigabitEthernet0/0/1"},
            {"name": "vlan_id", "label": "VLAN ID", "type": "number", "required": True,
             "placeholder": "10"},
        ],
        "command_template": [
            "interface {{interface}}",
            "port link-type access",
            "port default vlan {{vlan_id}}",
            "quit",
        ],
    },
    "trunk_port": {
        "name": "配置 Trunk 接口",
        "icon": "&#x1F517;",
        "roles": ["switch", "l3_switch"],
        "category": "switching",
        "required": ["interface", "allowed_vlans"],
        "fields": [
            {"name": "interface", "label": "接口", "type": "text", "required": True,
             "placeholder": "GigabitEthernet0/0/1"},
            {"name": "allowed_vlans", "label": "允许的 VLAN", "type": "text", "required": True,
             "placeholder": "10 20 30"},
        ],
        "command_template": [
            "interface {{interface}}",
            "port link-type trunk",
            "port trunk allow-pass vlan {{allowed_vlans}}",
            "quit",
        ],
    },
    "vlanif": {
        "name": "配置 VLANIF",
        "icon": "&#x1F310;",
        "roles": ["switch", "l3_switch"],
        "category": "switching",
        "required": ["vlan_id", "ip_address", "subnet_mask"],
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
            "quit",
        ],
    },
    "stp": {
        "name": "配置 STP",
        "icon": "&#x1F333;",
        "roles": ["switch", "l3_switch"],
        "category": "switching",
        "required": [],
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
        "command_template": [
            "{{?enable: stp mode {{mode}}}}",
            "{{?enable: stp priority {{priority}}}}",
            "{{?enable: stp enable}}",
            "{{?!enable: undo stp enable}}",
        ],
    },
    "eth_trunk": {
        "name": "配置 Eth-Trunk",
        "icon": "&#x1F517;",
        "roles": ["switch", "l3_switch", "router"],
        "category": "switching",
        "required": ["trunk_id", "member_interface"],
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
        "command_template": [
            "interface Eth-Trunk {{trunk_id}}",
            "{{?mode==lacp: mode lacp}}",
            "quit",
            "interface {{member_interface}}",
            "eth-trunk {{trunk_id}}",
            "quit",
        ],
    },
    # ========== 基础网络功能 ==========
    "lldp": {
        "name": "配置 LLDP",
        "icon": "&#x1F517;",
        "roles": ["router", "switch", "l3_switch"],
        "category": "network",
        "required": [],
        "fields": [
            {"name": "enable", "label": "启用 LLDP", "type": "checkbox", "default": True},
        ],
        "command_template": [
            "{{?enable: lldp enable}}",
            "{{?!enable: undo lldp enable}}",
        ],
    },
    "dhcp": {
        "name": "配置 DHCP",
        "icon": "&#x1F4E1;",
        "roles": ["router", "l3_switch"],
        "category": "network",
        "required": ["pool_name", "network", "mask"],
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
        "command_template": [
            "{{?enable: dhcp enable}}",
            "{{?enable: ip pool {{pool_name}}}}",
            "{{?enable: network {{network}} mask {{mask}}}}",
            "{{?enable: gateway-list {{gateway}}}}",
            "{{?enable: dns-list {{dns}}}}",
            "{{?enable: lease day {{lease_days}}}}",
            "{{?enable: quit}}",
            "{{?!enable: undo dhcp enable}}",
        ],
    },
    "nat": {
        "name": "配置 NAT",
        "icon": "&#x1F504;",
        "roles": ["router"],
        "category": "network",
        "required": ["nat_type", "acl_number", "interface", "global_ip", "inside_ip"],
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
        "command_template": [
            "{{?nat_type==outbound: interface {{interface}}}}",
            "{{?nat_type==outbound: nat outbound {{acl_number}}}}",
            "{{?nat_type==outbound: quit}}",
            "{{?nat_type==static: nat static global {{global_ip}} inside {{inside_ip}}}}",
            "{{?nat_type==server: interface {{interface}}}}",
            "{{?nat_type==server: nat server protocol tcp global {{global_ip}} inside {{inside_ip}}}}",
            "{{?nat_type==server: quit}}",
        ],
    },
    "vrrp": {
        "name": "配置 VRRP",
        "icon": "&#x1F6E1;",
        "roles": ["router", "l3_switch"],
        "category": "network",
        "required": ["interface", "virtual_ip"],
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
        "command_template": [
            "interface {{interface}}",
            "vrrp vrid {{vrid}} virtual-ip {{virtual_ip}}",
            "vrrp vrid {{vrid}} priority {{priority}}",
            "quit",
        ],
    },
    # ========== 安全/管理功能 ==========
    "acl": {
        "name": "配置 ACL",
        "icon": "&#x1F6E1;",
        "roles": ["router", "switch", "l3_switch"],
        "category": "security",
        "required": ["acl_number", "source"],
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
        "command_template": [
            "acl number {{acl_number}}",
            "rule {{rule_type}} {{protocol}} source {{source}}{{?destination!=any: destination {{destination}}}}",
            "quit",
        ],
    },
    "ssh_user": {
        "name": "配置 SSH 用户",
        "icon": "&#x1F512;",
        "roles": ["router", "switch", "l3_switch"],
        "category": "security",
        "required": ["username", "password"],
        "fields": [
            {"name": "username", "label": "用户名", "type": "text", "default": "admin",
             "required": True, "placeholder": "admin"},
            {"name": "password", "label": "密码", "type": "password", "required": True,
             "placeholder": "Huawei@123"},
            {"name": "privilege_level", "label": "权限等级", "type": "number", "default": "15",
             "required": True, "placeholder": "15"},
        ],
        "command_template": [
            "aaa",
            "local-user {{username}} password cipher {{password}}",
            "local-user {{username}} privilege level {{privilege_level}}",
            "local-user {{username}} service-type ssh",
            "quit",
            "stelnet server enable",
            "ssh user {{username}} authentication-type password",
            "user-interface vty 0 4",
            "authentication-mode aaa",
            "protocol inbound ssh",
            "quit",
        ],
    },
    "telnet": {
        "name": "配置 Telnet",
        "icon": "&#x1F4BB;",
        "roles": ["router", "switch", "l3_switch"],
        "category": "security",
        "required": [],
        "fields": [
            {"name": "enable", "label": "启用 Telnet 服务器", "type": "checkbox", "default": True},
            {"name": "username", "label": "用户名", "type": "text", "default": "admin",
             "placeholder": "admin"},
            {"name": "password", "label": "密码", "type": "password",
             "placeholder": "Huawei@123"},
            {"name": "privilege_level", "label": "权限等级", "type": "number", "default": "15",
             "placeholder": "15"},
        ],
        "command_template": [
            "{{?enable: telnet server enable}}",
            "{{?username: aaa}}",
            "{{?username: local-user {{username}} password cipher {{password}}}}",
            "{{?username: local-user {{username}} privilege level {{privilege_level}}}}",
            "{{?username: local-user {{username}} service-type telnet}}",
            "{{?username: quit}}",
            "{{?username: user-interface vty 0 4}}",
            "{{?username: authentication-mode aaa}}",
            "{{?username: protocol inbound telnet}}",
            "{{?username: quit}}",
            "{{?!enable: undo telnet server enable}}",
        ],
    },
    "snmp": {
        "name": "配置 SNMP",
        "icon": "&#x1F4CA;",
        "roles": ["router", "switch", "l3_switch"],
        "category": "security",
        "required": ["community"],
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
        "command_template": [
            "snmp-agent community {{permission}} {{community}}",
            "snmp-agent sys-info version {{version}}",
            "{{?sys_contact: snmp-agent sys-info contact {{sys_contact}}}}",
            "{{?sys_location: snmp-agent sys-info location {{sys_location}}}}",
        ],
    },
    # ========== 安全/VPN 功能 ==========
    "ipsec": {
        "name": "配置 IPsec",
        "icon": "&#x1F512;",
        "roles": ["router"],
        "category": "vpn",
        "required": ["proposal_name", "policy_name", "peer_name", "acl_number"],
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
            {"name": "peer_name", "label": "IKE Peer 名称", "type": "text", "required": True,
             "placeholder": "peer1"},
            {"name": "acl_number", "label": "ACL 编号 (匹配流量)", "type": "number",
             "required": True, "placeholder": "3000"},
        ],
        "command_template": [
            "ipsec proposal {{proposal_name}}",
            "esp authentication-algorithm {{auth_algorithm}}",
            "esp encryption-algorithm {{enc_algorithm}}",
            "quit",
            "ipsec policy {{policy_name}} 10 isakmp",
            "security acl {{acl_number}}",
            "ike-peer {{peer_name}}",
            "proposal {{proposal_name}}",
            "quit",
        ],
    },
    "ike": {
        "name": "配置 IKE",
        "icon": "&#x1F511;",
        "roles": ["router"],
        "category": "vpn",
        "required": ["proposal_name", "peer_name", "peer_address", "pre_shared_key"],
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
        "command_template": [
            "ike proposal {{proposal_name}}",
            "authentication-method {{auth_method}}",
            "encryption-algorithm {{enc_algorithm}}",
            "authentication-algorithm {{auth_algorithm}}",
            "dh {{dh_group}}",
            "quit",
            "ike peer {{peer_name}}",
            "ike-proposal {{proposal_name}}",
            "pre-shared-key simple {{pre_shared_key}}",
            "remote-address {{peer_address}}",
            "{{?local_address: local-address {{local_address}}}}",
            "quit",
        ],
    },
    "gre": {
        "name": "配置 GRE 隧道",
        "icon": "&#x1F6A7;",
        "roles": ["router"],
        "category": "vpn",
        "required": ["source", "destination", "tunnel_ip"],
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
        "command_template": [
            "interface Tunnel {{tunnel_id}}",
            "ip address {{tunnel_ip}} {{tunnel_mask}}",
            "tunnel-protocol {{protocol}}",
            "source {{source}}",
            "destination {{destination}}",
            "quit",
        ],
    },
    "vpn_instance": {
        "name": "配置 VPN 实例",
        "icon": "&#x1F3E2;",
        "roles": ["router"],
        "category": "vpn",
        "required": ["name", "rd"],
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
        "command_template": [
            "ip vpn-instance {{name}}",
            "route-distinguisher {{rd}}",
            "{{?rt_export: vpn-target {{rt_export}} export-extcommunity}}",
            "{{?rt_import: vpn-target {{rt_import}} import-extcommunity}}",
            "quit",
            "{{?interface: interface {{interface}}}}",
            "{{?interface: ip binding vpn-instance {{name}}}}",
            "{{?interface: quit}}",
        ],
    },
}

# Verification commands per config type
VERIFY_COMMANDS: Dict[str, List[str]] = {
    "branch_ospf":    ["display ip interface brief", "display ospf peer"],
    "vlan_gateway_dhcp": ["display vlan", "display ip interface brief", "display ip pool"],
    "edge_easy_ip_nat": ["display acl all", "display current-configuration | include nat"],
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
    "solution": "组合业务方案",
    "routing": "路由协议",
    "switching": "交换功能",
    "network": "基础网络",
    "security": "安全管理",
    "vpn": "VPN/隧道",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_templates_for_role(device_role: str) -> Dict[str, dict]:
    """返回适用于指定设备角色的配置模板。"""
    result = {}
    for key, tpl in CONFIG_TEMPLATES.items():
        if device_role in tpl.get("roles", []):
            result[key] = _public_entry(tpl)
    return result


def get_all_templates() -> Dict[str, dict]:
    """返回所有配置模板。"""
    return {key: _public_entry(tpl) for key, tpl in CONFIG_TEMPLATES.items()}


def _public_entry(tpl: dict) -> dict:
    entry = {
        "name": tpl["name"],
        "icon": tpl["icon"],
        "fields": tpl["fields"],
        "category": tpl.get("category", ""),
    }
    if "field_groups" in tpl:
        entry["field_groups"] = tpl["field_groups"]
    return entry


def get_categories() -> Dict[str, str]:
    """返回所有分类名称。"""
    return dict(CATEGORY_NAMES)


def build_config_commands(config_type: str, params: dict) -> List[str]:
    """
    根据配置类型和参数生成配置命令列表（声明式模板渲染）。

    Args:
        config_type: 配置类型标识
        params: 表单参数字典

    Returns:
        配置命令列表（不含 system-view / return）
    """
    tpl = CONFIG_TEMPLATES.get(config_type)
    if not tpl:
        return []
    tmpl = tpl.get("command_template")
    if not tmpl:
        return []
    required = list(tpl.get("required", []))
    required += [f["name"] for f in tpl.get("fields", []) if f.get("required")]
    missing = [n for n in required if not params.get(n)]
    if missing:
        return []
    return render_template_commands(tmpl, params or {})


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
