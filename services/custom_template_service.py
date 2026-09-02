"""
custom_template_service.py - 自定义配置模板服务
===============================================
负责保存、加载、管理用户自定义配置模板。
模板存储在 custom_templates/ 目录下，JSON 格式。
"""

import os
import json
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime

logger = logging.getLogger("ensp-web.custom-templates")

# 模板目录
TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "custom_templates")


def _ensure_dir():
    """确保模板目录存在。"""
    os.makedirs(TEMPLATES_DIR, exist_ok=True)


def list_templates() -> List[Dict[str, Any]]:
    """
    列出所有自定义模板（摘要信息）。

    Returns:
        模板列表
    """
    _ensure_dir()
    templates = []

    for filename in os.listdir(TEMPLATES_DIR):
        if not filename.endswith(".json"):
            continue
        filepath = os.path.join(TEMPLATES_DIR, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            templates.append({
                "name": data.get("name", filename.replace(".json", "")),
                "description": data.get("description", ""),
                "category": data.get("category", "custom"),
                "created_at": data.get("created_at", ""),
                "updated_at": data.get("updated_at", ""),
            })
        except Exception as e:
            logger.warning("读取模板 %s 失败: %s", filename, e)

    # 按名称排序
    templates.sort(key=lambda t: t.get("name", ""))
    return templates


def get_template(name: str) -> Optional[Dict[str, Any]]:
    """
    获取模板详情。

    Args:
        name: 模板名称

    Returns:
        模板详情字典，不存在返回 None
    """
    _ensure_dir()

    # 安全检查
    safe_name = "".join(c for c in name if c.isalnum() or c in "-_ ")
    filepath = os.path.join(TEMPLATES_DIR, f"{safe_name}.json")

    if not os.path.exists(filepath):
        return None

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("读取模板 %s 失败: %s", name, e)
        return None


def save_template(template_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    保存自定义模板。

    Args:
        template_data: 模板数据
            {
                "name": str,           # 模板名称
                "description": str,    # 模板描述
                "category": str,       # 分类
                "template": list,      # 命令模板
                "variables": dict,     # 变量定义
                "precheck": list,      # 预检命令
                "verify": list,        # 验证命令
                "rollback": list,      # 回滚命令
            }

    Returns:
        {"success": bool, "message": str}
    """
    _ensure_dir()

    name = template_data.get("name", "").strip()
    if not name:
        return {"success": False, "message": "模板名称不能为空"}

    # 安全检查
    safe_name = "".join(c for c in name if c.isalnum() or c in "-_ ")

    # 检查是否已存在
    filepath = os.path.join(TEMPLATES_DIR, f"{safe_name}.json")
    is_new = not os.path.exists(filepath)

    # 更新时间
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if is_new:
        template_data["created_at"] = now
    template_data["updated_at"] = now
    template_data["name"] = name

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(template_data, f, ensure_ascii=False, indent=2)
        action = "创建" if is_new else "更新"
        logger.info("模板已%s: %s", action, name)
        return {"success": True, "message": f"模板 '{name}' 已{action}"}
    except Exception as e:
        logger.error("保存模板 %s 失败: %s", name, e)
        return {"success": False, "message": f"保存失败: {e}"}


def delete_template(name: str) -> Dict[str, Any]:
    """
    删除自定义模板。

    Args:
        name: 模板名称

    Returns:
        {"success": bool, "message": str}
    """
    _ensure_dir()

    safe_name = "".join(c for c in name if c.isalnum() or c in "-_ ")
    filepath = os.path.join(TEMPLATES_DIR, f"{safe_name}.json")

    if not os.path.exists(filepath):
        return {"success": False, "message": f"模板 '{name}' 不存在"}

    try:
        os.remove(filepath)
        logger.info("模板已删除: %s", name)
        return {"success": True, "message": f"模板 '{name}' 已删除"}
    except Exception as e:
        logger.error("删除模板 %s 失败: %s", name, e)
        return {"success": False, "message": f"删除失败: {e}"}


# ---------------------------------------------------------------------------
# 预置常用命令库
# ---------------------------------------------------------------------------

COMMON_COMMANDS_LIBRARY: Dict[str, Dict[str, Any]] = {
    "基础配置": {
        "icon": "&#x1F4E6;",
        "commands": [
            {"cmd": "sysname {{hostname}}", "desc": "设置设备名称"},
            {"cmd": "clock timezone BJ add 08:00:00", "desc": "设置时区"},
            {"cmd": "header shell information \"Welcome to {{hostname}}\"", "desc": "设置登录欢迎信息"},
            {"cmd": "info-center enable", "desc": "开启信息中心"},
            {"cmd": "info-center loghost {{syslog_ip}}", "desc": "配置日志服务器"},
        ]
    },
    "接口配置": {
        "icon": "&#x1F310;",
        "commands": [
            {"cmd": "interface {{interface}}", "desc": "进入接口"},
            {"cmd": "ip address {{ip}} {{mask}}", "desc": "配置 IP 地址"},
            {"cmd": "description {{desc}}", "desc": "接口描述"},
            {"cmd": "undo shutdown", "desc": "开启接口"},
            {"cmd": "shutdown", "desc": "关闭接口"},
            {"cmd": "mtu {{mtu}}", "desc": "设置 MTU"},
            {"cmd": "bandwidth {{bandwidth}}", "desc": "设置带宽"},
            {"cmd": "undo ip address", "desc": "删除 IP 地址"},
            {"cmd": "ip address {{ip}} {{mask}} sub", "desc": "配置从 IP"},
        ]
    },
    "VLAN 配置": {
        "icon": "&#x1F4CB;",
        "commands": [
            {"cmd": "vlan {{vlan_id}}", "desc": "创建 VLAN"},
            {"cmd": "vlan batch {{vlan_start}} to {{vlan_end}}", "desc": "批量创建 VLAN"},
            {"cmd": "description {{vlan_desc}}", "desc": "VLAN 描述"},
            {"cmd": "port link-type access", "desc": "设置 Access 口"},
            {"cmd": "port default vlan {{vlan_id}}", "desc": "加入 VLAN"},
            {"cmd": "port link-type trunk", "desc": "设置 Trunk 口"},
            {"cmd": "port trunk allow-pass vlan {{vlan_list}}", "desc": "允许 VLAN 通过"},
            {"cmd": "port trunk allow-pass vlan all", "desc": "允许所有 VLAN"},
            {"cmd": "port hybrid pvid vlan {{vlan_id}}", "desc": "设置 PVID"},
        ]
    },
    "路由配置": {
        "icon": "&#x1F6E4;",
        "commands": [
            {"cmd": "ip route-static {{dest}} {{mask}} {{next_hop}}", "desc": "静态路由"},
            {"cmd": "ip route-static {{dest}} {{mask}} {{interface}}", "desc": "接口静态路由"},
            {"cmd": "ip route-static 0.0.0.0 0.0.0.0 {{next_hop}}", "desc": "默认路由"},
            {"cmd": "undo ip route-static {{dest}} {{mask}} {{next_hop}}", "desc": "删除静态路由"},
        ]
    },
    "OSPF 配置": {
        "icon": "&#x1F310;",
        "commands": [
            {"cmd": "ospf {{process_id}}", "desc": "进入 OSPF 进程"},
            {"cmd": "router-id {{router_id}}", "desc": "设置 Router ID"},
            {"cmd": "area {{area_id}}", "desc": "进入 OSPF 区域"},
            {"cmd": "network {{network}} {{wildcard}}", "desc": "宣告网络"},
            {"cmd": "silent-interface {{interface}}", "desc": "静默接口"},
            {"cmd": "default-route-advertise", "desc": "通告默认路由"},
            {"cmd": "ospf cost {{cost}}", "desc": "设置 OSPF 开销"},
            {"cmd": "ospf timer hello {{seconds}}", "desc": "Hello 定时器"},
            {"cmd": "ospf timer dead {{seconds}}", "desc": "Dead 定时器"},
            {"cmd": "ospf authentication-mode md5 {{key_id}} cipher {{password}}", "desc": "MD5 认证"},
        ]
    },
    "BGP 配置": {
        "icon": "&#x1F30D;",
        "commands": [
            {"cmd": "bgp {{local_as}}", "desc": "进入 BGP 进程"},
            {"cmd": "router-id {{router_id}}", "desc": "设置 Router ID"},
            {"cmd": "peer {{peer_ip}} as-number {{peer_as}}", "desc": "配置 BGP 邻居"},
            {"cmd": "peer {{peer_ip}} connect-interface Loopback{{loopback_id}}", "desc": "指定更新源"},
            {"cmd": "ipv4-family unicast", "desc": "进入 IPv4 单播地址族"},
            {"cmd": "network {{network}} {{mask}}", "desc": "宣告网络"},
            {"cmd": "peer {{peer_ip}} enable", "desc": "启用邻居"},
            {"cmd": "peer {{peer_ip}} next-hop-local", "desc": "设置下一跳为本地"},
        ]
    },
    "RIP 配置": {
        "icon": "&#x1F4CD;",
        "commands": [
            {"cmd": "rip {{process_id}}", "desc": "进入 RIP 进程"},
            {"cmd": "version 2", "desc": "设置版本 2"},
            {"cmd": "network {{network}}", "desc": "宣告网络"},
            {"cmd": "undo summary", "desc": "关闭自动汇总"},
            {"cmd": "rip metricin {{metric}}", "desc": "设置入向度量值"},
        ]
    },
    "IS-IS 配置": {
        "icon": "&#x1F517;",
        "commands": [
            {"cmd": "isis {{process_id}}", "desc": "进入 IS-IS 进程"},
            {"cmd": "network-entity {{net_entity}}", "desc": "设置 NET 实体"},
            {"cmd": "is-level {{level}}", "desc": "设置级别"},
            {"cmd": "is-name {{name}}", "desc": "设置系统名称"},
            {"cmd": "interface {{interface}}", "desc": "进入接口"},
            {"cmd": "isis enable {{process_id}}", "desc": "接口启用 IS-IS"},
            {"cmd": "isis circuit-level {{level}}", "desc": "接口电路级别"},
        ]
    },
    "DHCP 配置": {
        "icon": "&#x1F4CA;",
        "commands": [
            {"cmd": "dhcp enable", "desc": "开启 DHCP"},
            {"cmd": "ip pool {{pool_name}}", "desc": "创建地址池"},
            {"cmd": "network {{network}} mask {{mask}}", "desc": "配置地址池网段"},
            {"cmd": "gateway-list {{gateway}}", "desc": "配置网关"},
            {"cmd": "dns-list {{dns}}", "desc": "配置 DNS"},
            {"cmd": "lease day {{days}}", "desc": "配置租期"},
            {"cmd": "excluded-ip-address {{start_ip}} {{end_ip}}", "desc": "排除 IP 范围"},
            {"cmd": "static-bind ip-address {{ip}} mac-address {{mac}}", "desc": "静态绑定"},
            {"cmd": "interface {{interface}}", "desc": "进入接口"},
            {"cmd": "dhcp select global", "desc": "接口使用全局 DHCP"},
            {"cmd": "dhcp select interface", "desc": "接口 DHCP"},
        ]
    },
    "NAT 配置": {
        "icon": "&#x1F310;",
        "commands": [
            {"cmd": "nat address-group {{group_id}} {{start_ip}} {{end_ip}}", "desc": "NAT 地址池"},
            {"cmd": "acl number {{acl_id}}", "desc": "创建 ACL"},
            {"cmd": "rule permit source {{source}} {{wildcard}}", "desc": "ACL 规则"},
            {"cmd": "interface {{interface}}", "desc": "进入接口"},
            {"cmd": "nat outbound {{acl_id}} address-group {{group_id}}", "desc": "动态 NAT"},
            {"cmd": "nat outbound {{acl_id}}", "desc": "Easy IP NAT"},
            {"cmd": "nat server protocol tcp global {{global_ip}} {{global_port}} inside {{inside_ip}} {{inside_port}}", "desc": "NAT 服务器"},
        ]
    },
    "ACL 配置": {
        "icon": "&#x1F4CB;",
        "commands": [
            {"cmd": "acl number {{acl_id}}", "desc": "基本 ACL"},
            {"cmd": "acl number {{acl_id}}", "desc": "高级 ACL"},
            {"cmd": "rule {{rule_id}} permit source {{source}} {{wildcard}}", "desc": "允许源地址"},
            {"cmd": "rule {{rule_id}} deny source {{source}} {{wildcard}}", "desc": "拒绝源地址"},
            {"cmd": "rule {{rule_id}} permit ip source {{src}} {{src_wild}} destination {{dst}} {{dst_wild}}", "desc": "高级 ACL 规则"},
            {"cmd": "rule {{rule_id}} permit tcp destination-port eq {{port}}", "desc": "TCP 端口规则"},
            {"cmd": "rule {{rule_id}} permit icmp", "desc": "允许 ICMP"},
            {"cmd": "traffic classifier {{name}} operator or", "desc": "创建流分类"},
            {"cmd": "if-match acl {{acl_id}}", "desc": "匹配 ACL"},
            {"cmd": "traffic behavior {{name}}", "desc": "创建流行为"},
            {"cmd": "traffic policy {{name}}", "desc": "创建流策略"},
            {"cmd": "classifier {{classifier}} behavior {{behavior}}", "desc": "绑定分类和行为"},
            {"cmd": "interface {{interface}}", "desc": "进入接口"},
            {"cmd": "traffic-policy {{policy}} inbound", "desc": "应用流策略"},
        ]
    },
    "STP/RSTP 配置": {
        "icon": "&#x1F500;",
        "commands": [
            {"cmd": "stp mode {{mode}}", "desc": "设置 STP 模式"},
            {"cmd": "stp enable", "desc": "开启 STP"},
            {"cmd": "stp root primary", "desc": "设置根桥"},
            {"cmd": "stp root secondary", "desc": "设置备份根桥"},
            {"cmd": "stp priority {{priority}}", "desc": "设置优先级"},
            {"cmd": "interface {{interface}}", "desc": "进入接口"},
            {"cmd": "stp edged-port enable", "desc": "设置边缘端口"},
            {"cmd": "stp bpdu-protection", "desc": "BPDU 保护"},
            {"cmd": "stp cost {{cost}}", "desc": "设置路径开销"},
        ]
    },
    "链路聚合配置": {
        "icon": "&#x1F517;",
        "commands": [
            {"cmd": "interface Eth-Trunk {{trunk_id}}", "desc": "创建 Eth-Trunk"},
            {"cmd": "mode lacp-static", "desc": "设置 LACP 模式"},
            {"cmd": "trunkport {{interface}}", "desc": "加入成员接口"},
            {"cmd": "max active-linknumber {{number}}", "desc": "最大活动链路数"},
            {"cmd": "least active-linknumber {{number}}", "desc": "最小活动链路数"},
            {"cmd": "interface {{interface}}", "desc": "进入成员接口"},
            {"cmd": "eth-trunk {{trunk_id}}", "desc": "加入 Eth-Trunk"},
        ]
    },
    "VRRP 配置": {
        "icon": "&#x1F6E4;",
        "commands": [
            {"cmd": "interface {{interface}}", "desc": "进入接口"},
            {"cmd": "vrrp vrid {{vrid}} virtual-ip {{virtual_ip}}", "desc": "创建 VRRP"},
            {"cmd": "vrrp vrid {{vrid}} priority {{priority}}", "desc": "设置优先级"},
            {"cmd": "vrrp vrid {{vrid}} preempt-mode timer delay {{delay}}", "desc": "抢占延时"},
            {"cmd": "vrrp vrid {{vrid}} track interface {{track_interface}} reduced {{reduced}}", "desc": "接口跟踪"},
        ]
    },
    "LLDP 配置": {
        "icon": "&#x1F9F0;",
        "commands": [
            {"cmd": "lldp enable", "desc": "全局开启 LLDP"},
            {"cmd": "lldp management-address {{ip}}", "desc": "设置管理地址"},
            {"cmd": "interface {{interface}}", "desc": "进入接口"},
            {"cmd": "lldp enable", "desc": "接口开启 LLDP"},
            {"cmd": "lldp tlv-enable basic-tlv all", "desc": "启用所有 TLV"},
        ]
    },
    "SNMP 配置": {
        "icon": "&#x1F4E1;",
        "commands": [
            {"cmd": "snmp-agent", "desc": "开启 SNMP"},
            {"cmd": "snmp-agent community read {{community}}", "desc": "只读团体名"},
            {"cmd": "snmp-agent community write {{community}}", "desc": "读写团体名"},
            {"cmd": "snmp-agent sys-info version v2c", "desc": "设置 SNMP 版本"},
            {"cmd": "snmp-agent target-host trap address udp-domain {{ip}} params securityname {{community}}", "desc": "Trap 目标"},
            {"cmd": "snmp-agent trap enable", "desc": "开启 Trap"},
        ]
    },
    "NTP 配置": {
        "icon": "&#x1F570;",
        "commands": [
            {"cmd": "ntp-service unicast-server {{server_ip}}", "desc": "NTP 服务器"},
            {"cmd": "ntp-service authentication enable", "desc": "开启 NTP 认证"},
            {"cmd": "ntp-service authentication-keyid {{key_id}} authentication-mode md5 {{password}}", "desc": "NTP 认证密钥"},
            {"cmd": "ntp-service reliable authentication-keyid {{key_id}}", "desc": "可信密钥"},
        ]
    },
    "SSH 配置": {
        "icon": "&#x1F512;",
        "commands": [
            {"cmd": "stelnet server enable", "desc": "开启 SSH 服务"},
            {"cmd": "ssh user {{username}}", "desc": "创建 SSH 用户"},
            {"cmd": "ssh user {{username}} authentication-type password", "desc": "密码认证"},
            {"cmd": "ssh user {{username}} service-type stelnet", "desc": "服务类型"},
            {"cmd": "aaa", "desc": "进入 AAA"},
            {"cmd": "local-user {{username}} password cipher {{password}}", "desc": "设置用户密码"},
            {"cmd": "local-user {{username}} privilege level {{level}}", "desc": "设置用户级别"},
            {"cmd": "local-user {{username}} service-type ssh", "desc": "服务类型"},
            {"cmd": "user-interface vty 0 4", "desc": "进入 VTY"},
            {"cmd": "authentication-mode aaa", "desc": "AAA 认证"},
            {"cmd": "protocol inbound ssh", "desc": "只允许 SSH"},
        ]
    },
    "Telnet 配置": {
        "icon": "&#x1F4DF;",
        "commands": [
            {"cmd": "telnet server enable", "desc": "开启 Telnet"},
            {"cmd": "user-interface vty 0 4", "desc": "进入 VTY"},
            {"cmd": "authentication-mode aaa", "desc": "AAA 认证"},
            {"cmd": "protocol inbound telnet", "desc": "允许 Telnet"},
            {"cmd": "user privilege level {{level}}", "desc": "用户级别"},
            {"cmd": "aaa", "desc": "进入 AAA"},
            {"cmd": "local-user {{username}} password cipher {{password}}", "desc": "设置用户"},
            {"cmd": "local-user {{username}} service-type telnet", "desc": "服务类型"},
        ]
    },
    "安全配置": {
        "icon": "&#x1F6E1;",
        "commands": [
            {"cmd": "firewall enable", "desc": "开启防火墙"},
            {"cmd": "firewall default deny", "desc": "默认拒绝"},
            {"cmd": "firewall default permit", "desc": "默认允许"},
            {"cmd": "login-blocker block-period {{minutes}}", "desc": "登录锁定"},
            {"cmd": "password complexity-check", "desc": "密码复杂度检查"},
            {"cmd": "set authentication password cipher {{password}}", "desc": "设置认证密码"},
        ]
    },
    "GRE 隧道配置": {
        "icon": "&#x1F573;",
        "commands": [
            {"cmd": "interface Tunnel {{tunnel_id}}", "desc": "创建隧道接口"},
            {"cmd": "ip address {{ip}} {{mask}}", "desc": "隧道 IP"},
            {"cmd": "tunnel-protocol gre", "desc": "GRE 协议"},
            {"cmd": "source {{source_ip}}", "desc": "源地址"},
            {"cmd": "destination {{dest_ip}}", "desc": "目的地址"},
            {"cmd": "keepalive", "desc": "开启 Keepalive"},
        ]
    },
    "IPsec VPN 配置": {
        "icon": "&#x1F510;",
        "commands": [
            {"cmd": "ike proposal {{proposal_id}}", "desc": "IKE 提议"},
            {"cmd": "encryption-algorithm {{algorithm}}", "desc": "加密算法"},
            {"cmd": "dh group{{group_id}}", "desc": "DH 组"},
            {"cmd": "authentication-algorithm sha2-256", "desc": "认证算法"},
            {"cmd": "ike peer {{peer_name}} v1", "desc": "IKE 对等体"},
            {"cmd": "pre-shared-key cipher {{key}}", "desc": "预共享密钥"},
            {"cmd": "ike-proposal {{proposal_id}}", "desc": "引用提议"},
            {"cmd": "remote-address {{remote_ip}}", "desc": "远端地址"},
            {"cmd": "ipsec proposal {{proposal_name}}", "desc": "IPsec 提议"},
            {"cmd": "transform esp", "desc": "ESP 变换"},
            {"cmd": "esp authentication-algorithm sha2-256", "desc": "ESP 认证"},
            {"cmd": "esp encryption-algorithm aes-128", "desc": "ESP 加密"},
            {"cmd": "ipsec policy {{policy_name}} {{seq}} isakmp", "desc": "IPsec 策略"},
            {"cmd": "security acl {{acl_id}}", "desc": "安全 ACL"},
            {"cmd": "ike-peer {{peer_name}}", "desc": "引用 IKE 对等体"},
            {"cmd": "ipsec proposal {{proposal_name}}", "desc": "引用 IPsec 提议"},
            {"cmd": "interface {{interface}}", "desc": "进入接口"},
            {"cmd": "ipsec policy {{policy_name}}", "desc": "应用策略"},
        ]
    },
    "QoS 配置": {
        "icon": "&#x1F4CB;",
        "commands": [
            {"cmd": "traffic classifier {{name}} operator or", "desc": "创建流分类"},
            {"cmd": "if-match acl {{acl_id}}", "desc": "匹配 ACL"},
            {"cmd": "if-match dscp {{dscp}}", "desc": "匹配 DSCP"},
            {"cmd": "traffic behavior {{name}}", "desc": "创建流行为"},
            {"cmd": "remark dscp {{dscp}}", "desc": "重标记 DSCP"},
            {"cmd": "car cir {{cir}} cbs {{cbs}}", "desc": "流量监管"},
            {"cmd": "traffic policy {{name}}", "desc": "创建流策略"},
            {"cmd": "classifier {{classifier}} behavior {{behavior}}", "desc": "绑定"},
            {"cmd": "interface {{interface}}", "desc": "进入接口"},
            {"cmd": "traffic-policy {{policy}} inbound", "desc": "应用策略"},
        ]
    },
}


def get_common_commands_library() -> Dict[str, Dict[str, Any]]:
    """获取常用命令库。"""
    return COMMON_COMMANDS_LIBRARY


def get_category_commands(category: str) -> List[Dict[str, str]]:
    """获取指定分类的命令列表。"""
    cat_data = COMMON_COMMANDS_LIBRARY.get(category, {})
    return cat_data.get("commands", [])
