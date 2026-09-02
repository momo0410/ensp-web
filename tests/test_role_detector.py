"""
test_role_detector.py - 协议角色解析单元测试
============================================
全部使用真实 eNSP 输出格式构造样例，纯函数测试，零 SSH。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.role_detector import (
    detect_protocol_roles, _interface_blocks, _best_router_id,
    _suggest_virtual_ip,
)


S5700_CONFIG = """
#
sysname Core-SW
#
vlan batch 10 20 30
#
stp mode mstp
#
dhcp enable
#
ip pool vlan10_pool
 gateway-list 192.168.10.1
 network 192.168.10.0 mask 255.255.255.0
#
interface Vlanif10
 ip address 192.168.10.1 255.255.255.0
 vrrp vrid 10 virtual-ip 192.168.10.254
 dhcp select relay
#
interface Vlanif20
 ip address 192.168.20.1 255.255.255.0
#
interface GigabitEthernet0/0/1
 port link-type access
 port default vlan 10
#
interface GigabitEthernet0/0/24
 port link-type trunk
 port trunk allow-pass vlan 10 20
#
snmp-agent community read public
#
return
"""

VRRP_BRIEF = """Interface                 VRID  Virtual IP      State
Vlanif10                  10    192.168.10.254  Master
Vlanif20                  20    192.168.20.254  Backup
"""

STP_BRIEF = """ MSTID  Port                        Role  STP State     Protection
    0   GigabitEthernet0/0/1        DESI  FORWARDING      NONE
    0   GigabitEthernet0/0/2        ALTE  DISCARDING      NONE
    0   GigabitEthernet0/0/24       ROOT  FORWARDING      NONE
"""


def _cap_result(**overrides):
    base = {
        "ip": "192.168.56.12",
        "hostname": "Core-SW",
        "device_role": "l3_switch",
        "interfaces": [
            {"name": "Vlanif10", "ip": "192.168.10.1", "status": "up"},
            {"name": "Vlanif20", "ip": "192.168.20.1", "status": "up"},
            {"name": "GigabitEthernet0/0/1", "ip": "", "status": "down"},
            {"name": "GigabitEthernet0/0/2", "ip": "", "status": "down"},
            {"name": "GigabitEthernet0/0/3", "ip": "", "status": "down"},
        ],
        "capabilities": {
            "vrrp": {"supported": True, "configured": True, "running": True},
            "stp": {"supported": True, "configured": True, "running": True},
            "dhcp": {"supported": True, "configured": True, "running": True},
            "vlan": {"supported": True, "configured": True, "running": True},
            "nat": {"supported": False, "configured": False, "running": False},
            "ospf": {"supported": True, "configured": False, "running": False},
            "snmp": {"supported": True, "configured": True, "running": False},
            "acl": {"supported": True, "configured": False, "running": False},
            "ssh": {"supported": True, "configured": False, "running": False},
        },
        "raw_outputs": {
            "display current-configuration": S5700_CONFIG,
            "display vrrp brief": VRRP_BRIEF,
            "display stp brief": STP_BRIEF,
        },
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
class TestParsers:
    def test_interface_blocks(self):
        blocks = _interface_blocks(S5700_CONFIG)
        names = [b["name"] for b in blocks]
        assert "Vlanif10" in names and "GigabitEthernet0/0/1" in names
        vlanif10 = next(b for b in blocks if b["name"] == "Vlanif10")
        assert "vrrp vrid 10" in vlanif10["body"]
        assert "sysname" not in vlanif10["body"]  # 全局配置不混入接口段

    def test_best_router_id_prefers_loopback(self):
        intfs = [
            {"name": "Vlanif10", "ip": "192.168.10.1"},
            {"name": "LoopBack0", "ip": "10.0.0.1"},
            {"name": "GigabitEthernet0/0/0", "ip": "192.168.56.12"},
        ]
        assert _best_router_id(intfs) == "10.0.0.1"

    def test_best_router_id_falls_back_to_max_ip(self):
        intfs = [
            {"name": "Vlanif10", "ip": "192.168.10.1"},
            {"name": "Vlanif20", "ip": "192.168.20.1"},
        ]
        assert _best_router_id(intfs) == "192.168.20.1"

    def test_suggest_virtual_ip(self):
        assert _suggest_virtual_ip("192.168.10.1") == "192.168.10.254"
        assert _suggest_virtual_ip("") == ""


# ---------------------------------------------------------------------------
# 角色判定
# ---------------------------------------------------------------------------
class TestRoleDetection:
    def _detect(self, **kw):
        return detect_protocol_roles(_cap_result(**kw))

    def test_vrrp_roles_from_brief(self):
        result = self._detect()
        vrrp = result["protocols"]["vrrp"]
        assert vrrp["status"] == "running"
        roles = {r["role"] for r in vrrp["roles"]}
        assert roles == {"Master", "Backup"}
        master = next(r for r in vrrp["roles"] if r["role"] == "Master")
        assert master["interface"] == "Vlanif10"
        assert "192.168.10.254" in master["detail"]

    def test_stp_roles(self):
        result = self._detect()
        stp = result["protocols"]["stp"]
        roles = [r["role"] for r in stp["roles"]]
        assert any("MSTP" in r for r in roles)          # 模式
        assert any("非根桥" in r for r in roles)         # 有 ROOT 端口
        assert any("阻塞" in r for r in roles)          # 有 ALTE 端口

    def test_dhcp_relay(self):
        result = self._detect()
        dhcp = result["protocols"]["dhcp"]
        roles = {r["role"]: r for r in dhcp["roles"]}
        assert "Server" in roles and "vlan10_pool" in roles["Server"]["detail"]
        assert "中继" in roles  # Vlanif10 下 dhcp select relay

    def test_vlan_roles(self):
        result = self._detect()
        vlan = result["protocols"]["vlan"]
        detail = " ".join(r["detail"] for r in vlan["roles"])
        assert "batch 10 20 30" in detail
        assert any("Access" in r["role"] for r in vlan["roles"])
        assert any("Trunk" in r["role"] for r in vlan["roles"])

    def test_snmp_v2c(self):
        result = self._detect()
        snmp = result["protocols"]["snmp"]
        assert snmp["roles"][0]["role"] == "v2c"
        assert snmp["roles"][0]["detail"] == "public"

    def test_unsupported_protocol_hidden(self):
        result = self._detect()
        assert "nat" not in result["protocols"]  # 矩阵判定交换机不支持 → 不显示

    def test_supported_but_unconfigured_shown(self):
        result = self._detect()
        ospf = result["protocols"]["ospf"]
        assert ospf["status"] == "supported"
        assert ospf["roles"] == []
        assert ospf["config_key"] == "ospf"  # 仍可一键填空配置

    def test_presets_for_ospf(self):
        result = self._detect()
        ospf = result["protocols"]["ospf"]
        assert ospf["presets"]["process_id"] == "1"
        assert ospf["presets"]["router_id"] == "192.168.20.1"  # 最大接口 IP
        assert ospf["preset_hints"]

    def test_presets_for_vrrp(self):
        result = self._detect()
        vrrp = result["protocols"]["vrrp"]
        assert vrrp["presets"]["interface"] == "Vlanif10"
        assert vrrp["presets"]["virtual_ip"] == "192.168.10.254"

    def test_presets_for_acl_number_increments(self):
        result = self._detect()
        acl = result["protocols"]["acl"]
        assert acl["presets"]["acl_number"] == "3000"

    def test_no_capability_results(self):
        result = self._detect(capabilities={}, raw_outputs={})
        # 无能力信息时保留协议行（unknown 状态），角色为空
        assert "vrrp" in result["protocols"]
        assert result["protocols"]["vrrp"]["status"] == "unknown"


# ---------------------------------------------------------------------------
# 路由器场景：NAT / OSPF 进程
# ---------------------------------------------------------------------------
AR_CONFIG = """
#
sysname AR1
#
acl number 2000
 rule 5 permit source 192.168.100.0 0.0.0.255
#
ospf 1 router-id 1.1.1.1
 area 0.0.0.0
  network 192.168.100.0 0.0.0.255
#
interface GigabitEthernet0/0/0
 ip address 192.168.100.1 255.255.255.0
#
interface GigabitEthernet0/0/1
 ip address 200.1.1.1 255.255.255.0
 nat outbound 2000
 traffic-filter inbound acl 2000
#
return
"""

OSPF_PEER = """
 Neighbor ID     Pri   State           Dead Time  Address         Interface
 2.2.2.2          1    Full/ -         00:00:37   192.168.100.2   GigabitEthernet0/0/0
"""


class TestRouterRoles:
    def _detect(self):
        return detect_protocol_roles({
            "ip": "192.168.56.11",
            "hostname": "AR1",
            "device_role": "router",
            "interfaces": [
                {"name": "GigabitEthernet0/0/0", "ip": "192.168.100.1", "status": "up"},
                {"name": "GigabitEthernet0/0/1", "ip": "200.1.1.1", "status": "up"},
            ],
            "capabilities": {
                "ospf": {"supported": True, "configured": True, "running": True},
                "nat": {"supported": True, "configured": True, "running": True},
                "acl": {"supported": True, "configured": True, "running": True},
            },
            "raw_outputs": {
                "display current-configuration": AR_CONFIG,
                "display ospf peer": OSPF_PEER,
            },
        })

    def test_nat_outbound_role(self):
        result = self._detect()
        nat = result["protocols"]["nat"]
        roles = nat["roles"]
        assert any("Easy-IP" in r["role"] for r in roles)
        outbound = next(r for r in roles if r["interface"] == "GigabitEthernet0/0/1")
        assert outbound
        # NAT 预填：沿用现有出接口与 ACL
        assert nat["presets"]["interface"] == "GigabitEthernet0/0/1"
        assert nat["presets"]["acl_number"] == "2000"
        assert nat["presets"]["nat_type"] == "outbound"

    def test_ospf_process_area_and_neighbor(self):
        result = self._detect()
        ospf = result["protocols"]["ospf"]
        roles = {r["role"]: r for r in ospf["roles"]}
        assert "进程 1" in roles
        assert "骨干区域" in roles
        assert "邻居正常" in roles and "Full" in roles["邻居正常"]["detail"]
        assert roles["Router ID"]["detail"] == "1.1.1.1"

    def test_acl_basic_and_interface_binding(self):
        result = self._detect()
        acl = result["protocols"]["acl"]
        assert any("基础 ACL" in r["role"] for r in acl["roles"])
        assert any("流量过滤" in r["role"] and r["interface"] == "GigabitEthernet0/0/1"
                   for r in acl["roles"])
        # 已有 acl 2000 → 建议下一个编号
        assert acl["presets"]["acl_number"] == "2001"

    def test_interface_ip_preset_picks_free_intf(self):
        # 路由器两个接口都有 IP → 不预填 interface
        result = self._detect()
        # 构造一个没配 IP 的接口再测
        cap = {
            "ip": "x", "hostname": "R", "device_role": "router",
            "interfaces": [
                {"name": "GigabitEthernet0/0/0", "ip": "10.0.0.1", "status": "up"},
                {"name": "GigabitEthernet0/0/1", "ip": "", "status": "down"},
            ],
            "capabilities": {"interface_ip": {"supported": True, "configured": False, "running": False}},
            "raw_outputs": {"display current-configuration": "#\nreturn\n"},
        }
        r = detect_protocol_roles(cap)
        assert r["protocols"]["interface_ip"]["presets"]["interface"] == "GigabitEthernet0/0/1"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
