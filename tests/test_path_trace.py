"""services/path_trace.py 的离线单测（需求④：数据包走向 / 抓包的零依赖替代）。

所有输入都是 VRP 输出的真实格式文本，不连任何设备。
"""
import pytest

from services.path_trace import (
    TraceError,
    diagnose_break,
    longest_prefix_match,
    parse_arp,
    parse_interfaces,
    parse_mac_table,
    parse_routing_table,
    trace_path,
)


# ---------------------------------------------------------------------------
# 真实格式的 VRP 输出样例
# ---------------------------------------------------------------------------
IFACE_R1 = """
Interface                   IP Address/Mask    Physical   Protocol
GigabitEthernet0/0/0        10.0.13.1/24      up         up
GigabitEthernet0/0/1        192.168.1.1/24    up         up
LoopBack0                   1.1.1.1/32        up         up(s)
"""

# brief 不带掩码，掩码靠路由表 Direct 条目补
# brief 的 IP 列带掩码（192.168.1.1/24）
IFACE_BRIEF_R1 = """
Interface                     IP Address/Mask    PHY     Protocol
GigabitEthernet0/0/0         10.0.13.1/24       up      up
GigabitEthernet0/0/1         192.168.1.1/24     up      up
"""

IFACE_BRIEF_R2 = """
Interface                     IP Address/Mask    PHY     Protocol
GigabitEthernet0/0/0         10.0.13.2/24       up      up
GigabitEthernet0/0/1         172.16.0.1/24      up      up
"""

IFACE_BRIEF_SW1 = """
Interface                     IP Address/Mask    PHY     Protocol
GigabitEthernet0/0/1         192.168.1.10/24    up      up
Vlanif10                     unassign           down    down
"""

ROUTES_R1 = """
Proto      Destination/Mask    Pre  Cost        Flags NextHop         Interface
10.0.13.0/24   Direct             0    0           D     10.0.13.1       GigabitEthernet0/0/0
192.168.1.0/24   Direct           0    0           D     192.168.1.1      GigabitEthernet0/0/1
172.16.0.0/24   OSPF              10   2           D     10.0.13.2       GigabitEthernet0/0/0
"""

ROUTES_R2 = """
Proto      Destination/Mask    Pre  Cost        Flags NextHop         Interface
10.0.13.0/24   Direct             0    0           D     10.0.13.2       GigabitEthernet0/0/0
172.16.0.0/24   Direct            0    0           D     172.16.0.1      GigabitEthernet0/0/1
192.168.1.0/24   OSPF             10   2           D     10.0.13.1       GigabitEthernet0/0/1
"""

ROUTES_NO_ROUTE = """
Proto      Destination/Mask    Pre  Cost        Flags NextHop         Interface
10.0.13.0/24   Direct             0    0           D     10.0.13.2       GigabitEthernet0/0/0
192.168.1.0/24   Direct           0    0           D     192.168.1.1      GigabitEthernet0/0/1
"""

ROUTES_DOWN_IF = """
Proto      Destination/Mask    Pre  Cost        Flags NextHop         Interface
192.168.1.0/24   Direct           0    0           D     192.168.1.1      GigabitEthernet0/0/1
172.16.0.0/24   Static            60   0           RD    10.0.13.2       GigabitEthernet0/0/0
"""

ARP_R1 = """
IP ADDRESS      MAC ADDRESS     EXPIRE(M) TYPE        INTERFACE   VPN-INSTANCE
192.168.1.10    00e0-fc12-3456  20        D-0         GE0/0/1
10.0.13.2       00e0-fc12-3457  18        D-0         GE0/0/0
"""

MAC_SW1 = """
MAC Address    VLAN/VSI/BD   Learned-From   Type        AGE
00e0-fc12-3458  1             GE0/0/2        dynamic     20
"""


class TestParsers:
    def test_路由表(self):
        routes = parse_routing_table(ROUTES_R1)
        assert len(routes) == 3
        ospf = next(r for r in routes if r["proto"] == "OSPF")
        assert ospf["nexthop"] == "10.0.13.2"
        assert ospf["interface"] == "GigabitEthernet0/0/0"
        assert ospf["preference"] == 10

    def test_同前缀多条只留最优(self):
        out = """
10.0.0.0/24   Direct         0   0   D   10.0.0.1   GE0/0/0
10.0.0.0/24   Static         60  0   RD  10.0.0.9   GE0/0/1
"""
        routes = parse_routing_table(out)
        assert len(routes) == 1
        assert routes[0]["proto"] == "Direct"

    def test_接口_brief(self):
        ifaces = parse_interfaces(IFACE_BRIEF_SW1)
        assert ifaces[0]["interface"] == "GigabitEthernet0/0/1"
        assert ifaces[0]["up"] is True
        assert ifaces[1]["interface"] == "Vlanif10"
        assert ifaces[1]["up"] is False

    def test_接口_down_标记(self):
        ifaces = parse_interfaces("*down: GigabitEthernet0/0/0   unassign  down  down")
        assert ifaces[0]["admin_down"] is True

    def test_arp(self):
        arps = parse_arp(ARP_R1)
        assert arps[0]["ip"] == "192.168.1.10"
        assert arps[1]["ip"] == "10.0.13.2"
        assert arps[1]["interface"] == "GE0/0/0"

    def test_mac(self):
        macs = parse_mac_table(MAC_SW1)
        assert macs[0]["mac"] == "00e0-fc12-3458"
        assert macs[0]["learned_from"] == "GE0/0/2"

    def test_空输入(self):
        assert parse_routing_table("") == []
        assert parse_interfaces("") == []
        assert parse_arp(None) == []


class TestLongestPrefix:
    def test_最长前缀优先(self):
        routes = parse_routing_table(ROUTES_R1)
        # 172.16.0.5 只命中 OSPF 那条
        route = longest_prefix_match("172.16.0.5", routes)
        assert route["proto"] == "OSPF"

    def test_无命中(self):
        routes = parse_routing_table(ROUTES_R1)
        assert longest_prefix_match("8.8.8.8", routes) is None

    def test_非法地址(self):
        assert longest_prefix_match("不是IP", parse_routing_table(ROUTES_R1)) is None


def _snap(device, iface, routing, arp=""):
    return {"device": device, "interfaces": iface, "routing": routing, "arp": arp}


class TestTracePath:
    def test_两跳到达(self):
        result = trace_path("192.168.1.10", "172.16.0.5", [
            _snap("R1", IFACE_BRIEF_R1, ROUTES_R1, ARP_R1),
            _snap("R2", IFACE_BRIEF_R2, ROUTES_R2),
        ])
        assert result["success"] is True
        assert [h["device"] for h in result["hops"]] == ["R1", "R2"]
        assert result["breakpoint"] is None
        assert "2 跳" in result["summary"]

    def test_起始设备是接口同网段那台(self):
        result = trace_path("192.168.1.10", "172.16.0.5", [
            _snap("R1", IFACE_BRIEF_R1, ROUTES_R1, ARP_R1),
            _snap("R2", IFACE_BRIEF_R2, ROUTES_R2),
        ])
        # R1 的 192.168.1.1/24 与源同网段，所以从 R1 出发
        assert result["hops"][0]["device"] == "R1"

    def test_每跳带证据(self):
        result = trace_path("192.168.1.10", "172.16.0.5", [
            _snap("R1", IFACE_BRIEF_R1, ROUTES_R1, ARP_R1),
            _snap("R2", IFACE_BRIEF_R2, ROUTES_R2),
        ])
        first = result["hops"][0]
        assert first["nexthop"] == "10.0.13.2"
        assert first["proto"] == "OSPF"
        assert "OSPF" in first["evidence"]

    def test_无路由时报断点(self):
        result = trace_path("192.168.1.10", "172.16.0.5", [
            _snap("R1", IFACE_BRIEF_R1, ROUTES_NO_ROUTE, ARP_R1),
        ])
        assert result["success"] is False
        assert result["breakpoint"]["device"] == "R1"
        assert any("没有匹配任何路由" in r for r in result["breakpoint"]["reasons"])
        assert "没有到 172.16.0.5 的路由" in result["summary"]

    def test_找不到下一跳设备(self):
        result = trace_path("192.168.1.10", "172.16.0.5", [
            _snap("R1", IFACE_BRIEF_R1, ROUTES_R1, ARP_R1),
            # 没有 10.0.13.2 这台设备的快照
        ])
        assert result["success"] is False
        assert "10.0.13.2" in result["summary"]
        assert result["breakpoint"]["device"] == "R1"

    def test_定位不到源设备(self):
        result = trace_path("203.0.113.9", "172.16.0.5", [
            _snap("R1", IFACE_BRIEF_R1, ROUTES_R1, ARP_R1),
        ])
        assert result["success"] is False
        assert result["breakpoint"]["device"] is None
        assert "无法定位源地址" in result["summary"]

    def test_空快照列表(self):
        result = trace_path("192.168.1.10", "172.16.0.5", [])
        assert result["success"] is False
        assert "没有任何设备快照" in result["summary"]

    def test_非法_IP(self):
        with pytest.raises(TraceError):
            trace_path("不是IP", "1.1.1.1", [_snap("R1", IFACE_BRIEF_R1, ROUTES_R1)])

    def test_环路(self):
        # R1 与 R2 都把 172.16.0.0/24 指向对方 → 成环
        r1 = """
10.0.13.0/24   Direct            0    0   D   10.0.13.1   GE0/0/0
192.168.1.0/24   Direct          0    0   D   192.168.1.1  GE0/0/1
172.16.0.0/24   Static           60   0   RD  10.0.13.2   GE0/0/0
"""
        r2 = """
10.0.13.0/24   Direct            0    0   D   10.0.13.2   GE0/0/0
192.168.1.0/24   Direct          0    0   D   10.0.13.1   GE0/0/1
172.16.0.0/24   Static           60   0   RD  10.0.13.1   GE0/0/0
"""
        result = trace_path("192.168.1.10", "172.16.0.5", [
            _snap("R1", IFACE_BRIEF_R1, r1, ARP_R1),
            _snap("R2", IFACE_BRIEF_R2, r2),
        ], max_hops=10)
        assert result["success"] is False
        assert "环路" in result["summary"]

    def test_跳数上限(self):
        # R1→R2→R3→R4 一条长链，max_hops=2 时走不完
        chain = []
        for i in range(1, 5):
            prev = i - 1
            nxt = i + 1
            routes = f"""
Direct     10.0.{prev}.0/24    0   0   D   10.0.{prev}.{prev}   GE0/0/0
Direct     10.0.{i}.0/24      0   0   D   10.0.{i}.{i}         GE0/0/0
172.16.0.0/24   OSPF             10  2       10.0.{nxt}.{nxt}     GE0/0/1
"""
            ifaces = f"""
Interface              IP Address/Mask       PHY  Protocol
GigabitEthernet0/0/0   10.0.{prev}.{prev}/24  up   up
GigabitEthernet0/0/1   10.0.{i}.{i}/24       up   up
"""
            chain.append(_snap(f"R{i}", ifaces, routes))

        result = trace_path("10.0.0.1", "172.16.0.5", chain, max_hops=2)
        assert result["success"] is False
        assert "超过 2 跳" in result["summary"]
        assert len(result["hops"]) == 2


class TestDiagnoseBreak:
    def test_接口被手动关闭(self):
        hop = {"interface": "GE0/0/1", "nexthop": ""}
        ifaces = [{"interface": "GE0/0/1", "admin_down": True, "up": False, "ip": "1.1.1.1"}]
        reasons = diagnose_break(hop, ifaces, [])
        assert any("手动关闭" in r for r in reasons)

    def test_接口未_up(self):
        hop = {"interface": "Vlanif10", "nexthop": ""}
        ifaces = [{"interface": "Vlanif10", "admin_down": False, "up": False, "ip": None}]
        reasons = diagnose_break(hop, ifaces, [])
        assert any("未 up" in r for r in reasons)

    def test_ARP_缺失(self):
        hop = {"interface": "GE0/0/0", "nexthop": "10.0.13.2"}
        reasons = diagnose_break(hop, [], [])
        assert any("ARP" in r for r in reasons)

    def test_接口不存在(self):
        hop = {"interface": "不存在的口", "nexthop": ""}
        reasons = diagnose_break(hop, [], [])
        assert any("没有接口" in r for r in reasons)

    def test_信息不足时兜底(self):
        reasons = diagnose_break({"interface": "", "nexthop": ""}, [], [])
        assert any("缺少对应设备的采集数据" in r for r in reasons)
