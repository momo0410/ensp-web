// 与旧版 app.js 对齐的常量表
export const PROTOCOL_NAMES = {
  ospf: "OSPF", rip: "RIP", bgp: "BGP", isis: "IS-IS",
  dhcp: "DHCP", nat: "NAT", ipsec: "IPSec",
  ntp: "NTP", snmp: "SNMP", acl: "ACL", static: "静态路由",
}

export const ROLE_NAMES = {
  router: "路由器", switch: "交换机", l3_switch: "三层交换机", unknown: "未知设备",
}

export const CAPABILITY_NAMES = {
  static_route: "静态路由", ospf: "OSPF", isis: "IS-IS", bgp: "BGP", rip: "RIP",
  routing_table: "路由表", vlan: "VLAN", access_port: "Access 接口",
  trunk_port: "Trunk 接口", vlanif: "VLANIF", stp: "STP/RSTP/MSTP",
  eth_trunk: "Eth-Trunk", lldp: "LLDP", interface_ip: "接口 IP",
  acl: "ACL", dhcp: "DHCP", nat: "NAT", vrrp: "VRRP",
  snmp: "SNMP", ssh: "SSH/STelnet", telnet: "Telnet",
  ipsec: "IPsec VPN", ike: "IKE/IKEv2", gre: "GRE 隧道", vpn_instance: "VPN 实例/VRF",
  ntp: "NTP", l2tp: "L2TP VPN", pppoe: "PPPoE", ipv6: "IPv6 基础",
  mpls: "MPLS", mpls_ldp: "MPLS LDP", igmp: "IGMP", pim: "PIM",
  igmp_snooping: "IGMP Snooping",
}

export const DEVICE_TYPE_LABELS = {
  router: "路由器", l3_switch: "三层交换机", switch: "二层交换机",
  firewall: "防火墙", ac: "无线AC", unknown: "未知",
}

export const TEMPLATE_CATEGORY_NAMES = {
  solution: "组合业务方案",
  routing: "路由协议",
  switching: "交换功能",
  network: "基础网络",
  security: "安全管理",
  vpn: "VPN/隧道",
}

export const TEMPLATE_CATEGORY_ORDER = ["solution", "routing", "switching", "network", "security", "vpn", "other"]

export const AI_QUICK_TASKS = {
  explain_capabilities: "请解读这台设备的能力识别结果：为什么支持/不支持这些协议？运行中、已配置、支持三种状态分别是什么意思？",
  explain_roles: "请讲解这台设备在各协议中扮演的角色（如 Master、根桥、DHCP Server）以及这些角色意味着什么。",
  explain_config: "请像老师一样逐段讲解这台设备的当前配置，说明每一段的作用。",
  diagnose: "",
}

// network 变量名 → 自动推导反掩码到这些变量
export const NETWORK_VAR_NAMES = ["network", "network_address", "network_addr", "net_addr"]
export const WILDCARD_VAR_NAMES = ["wildcard", "wildcard_mask", "wc"]
