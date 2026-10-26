"""services/conflict_detector.py 的离线单测。

覆盖两条安全相关路径：
  1. 危险命令拦截（下发前必须挡住 reboot/delete/format 等）
  2. 冲突检测（Router ID 重复、同网段 IP 重复、与既有接口 IP 冲突）
"""

from services.conflict_detector import (
    check_conflicts,
    check_cross_device_conflicts,
    check_dangerous_commands,
    check_interface_has_ip,
    extract_interface_from_command,
    extract_ip_from_command,
    extract_vlan_id,
)

CURRENT_CONFIG = """#
sysname SW1
#
interface GigabitEthernet0/0/1
 ip address 10.0.0.1 255.255.255.0
#
interface Vlanif10
 ip address 192.168.10.1 255.255.255.0
#
return
"""


def _collection(**kw):
    """构造一份最小可用的采集结果（check_* 只读 info.interfaces 与 current-configuration）。"""
    data = {
        "info": {"interfaces": ["GigabitEthernet0/0/1  10.0.0.1  up  up"]},
        "commands": {
            "display current-configuration": {"output": CURRENT_CONFIG},
        },
    }
    data.update(kw)
    return {"127.0.0.1:2000": data}


class TestDangerousCommands:
    def test_单独的危险命令被拦下(self):
        safe, found = check_dangerous_commands(["reboot"])
        assert safe is False
        assert found[0]["command"] == "reboot"
        assert "重启" in found[0]["reason"]

    def test_每条危险关键字都有对应拦截(self):
        cases = [
            "reset saved-configuration",
            "delete flash:/vrpcfg.zip",
            "format flash:",
            "startup saved-configuration vrpcfg.zip",
            "undo ssh server enable",
            "undo local-user admin",
            "undo interface GigabitEthernet0/0/1",
            "undo ip route-static all",
            "undo acl all",
        ]
        for cmd in cases:
            safe, found = check_dangerous_commands([cmd])
            assert safe is False, f"{cmd} 应被拦截"
            assert found, f"{cmd} 应给出原因"

    def test_大小写与前导空白不绕过(self):
        safe, _ = check_dangerous_commands(["   REBOOT "])
        assert safe is False

    def test_普通命令判为安全(self):
        safe, found = check_dangerous_commands(
            ["system-view", "ospf 1 router-id 1.1.1.1", "display version"])
        assert safe is True
        assert found == []

    def test_空列表安全(self):
        assert check_dangerous_commands([]) == (True, [])


class TestExtractors:
    def test_接口名必须是完整名(self):
        # 回归：早先的正则只捕获前缀，返回 "GigabitEthernet"，导致
        # check_interface_has_ip 永远匹配不上、"接口已有 IP" 检查静默失效。
        assert extract_interface_from_command(
            "interface GigabitEthernet0/0/1") == "GigabitEthernet0/0/1"
        assert extract_interface_from_command("interface GE0/0/1") == "GE0/0/1"
        assert extract_interface_from_command("interface Vlanif10") == "Vlanif10"
        assert extract_interface_from_command("interface Eth-Trunk1") == "Eth-Trunk1"

    def test_非接口命令取不到接口名(self):
        assert extract_interface_from_command("port link-type trunk") == ""
        assert extract_interface_from_command("interface ") == ""

    def test_提取_ip(self):
        assert extract_ip_from_command("ip address 192.168.1.1 255.255.255.0") == "192.168.1.1"
        assert extract_ip_from_command("ospf 1") == ""

    def test_提取_vlan(self):
        assert extract_vlan_id("vlan 10") == "10"
        assert extract_vlan_id("VLAN 4094") == "4094"
        assert extract_vlan_id("port link-type trunk") == ""


class TestInterfaceHasIp:
    def test_已在配置中的接口能查到现有_ip(self):
        has_ip, current_ip = check_interface_has_ip(
            "127.0.0.1:2000", "GigabitEthernet0/0/1", _collection())
        assert has_ip is True
        assert current_ip == "10.0.0.1"

    def test_没有ip的接口判为无(self):
        has_ip, current_ip = check_interface_has_ip(
            "127.0.0.1:2000", "GigabitEthernet0/0/9", _collection())
        assert has_ip is False
        assert current_ip == ""

    def test_无采集结果时不误报(self):
        assert check_interface_has_ip("127.0.0.1:2000", "GE0/0/1", {}) == (False, "")


class TestCheckConflicts:
    def test_危险命令进_errors_并判失败(self):
        result = check_conflicts(["127.0.0.1:2000"], ["reboot"], {}, {})
        assert result["success"] is False
        assert result["errors"][0]["type"] == "dangerous_command"

    def test_未定义变量进_errors(self):
        result = check_conflicts(
            ["127.0.0.1:2000"], ["ip address {{addr}} 255.255.255.0"], {}, {})
        assert result["success"] is False
        assert result["errors"][0]["type"] == "undefined_variable"

    def test_覆盖已有_ip_进_suggestions(self):
        # 这条同时守住了接口名提取的回归：接口名截断时这里永远不会有 suggestions
        result = check_conflicts(
            ["127.0.0.1:2000"],
            ["interface GigabitEthernet0/0/1", "ip address 10.0.0.9 255.255.255.0"],
            {},
            _collection(),
        )
        assert result["errors"] == []
        types = [s["type"] for s in result["suggestions"]]
        assert "interface_has_ip" in types
        hit = next(s for s in result["suggestions"] if s["type"] == "interface_has_ip")
        assert hit["current_ip"] == "10.0.0.1"
        assert hit["interface"] == "GigabitEthernet0/0/1"

    def test_同设备重复配置同一_ip_进_warnings(self):
        result = check_conflicts(
            ["127.0.0.1:2000"],
            ["ip address 10.0.0.1 255.255.255.0"],
            {},
            _collection(),
        )
        assert [w["type"] for w in result["warnings"]] == ["ip_conflict"]

    def test_vlan_命令给确认提示(self):
        result = check_conflicts(["127.0.0.1:2000"], ["vlan 10"], {}, {})
        assert [s["type"] for s in result["suggestions"]] == ["vlan_check"]

    def test_干净配置无冲突(self):
        result = check_conflicts(
            ["127.0.0.1:2000"], ["vlan 20", "interface GE0/0/2"], {}, {})
        assert result["success"] is True
        assert result["errors"] == []
        assert result["warnings"] == []


class TestCrossDeviceConflicts:
    def test_router_id_重复判为错误(self):
        result = check_cross_device_conflicts(
            ["10.0.0.1", "10.0.0.2"],
            {
                "10.0.0.1": ["ospf 1", "router-id 1.1.1.1"],
                "10.0.0.2": ["ospf 1", "router-id 1.1.1.1"],
            },
        )
        assert result["success"] is False
        assert result["errors"][0]["type"] == "duplicate_router_id"

    def test_router_id_不同不报错(self):
        result = check_cross_device_conflicts(
            ["10.0.0.1", "10.0.0.2"],
            {
                "10.0.0.1": ["ospf 1", "router-id 1.1.1.1"],
                "10.0.0.2": ["ospf 1", "router-id 2.2.2.2"],
            },
        )
        assert result["success"] is True

    def test_跨设备重复_ip_判为错误(self):
        result = check_cross_device_conflicts(
            ["10.0.0.1", "10.0.0.2"],
            {
                "10.0.0.1": ["interface GE0/0/1", "ip address 192.168.1.1 255.255.255.0"],
                "10.0.0.2": ["interface GE0/0/1", "ip address 192.168.1.1 255.255.255.0"],
            },
        )
        assert result["success"] is False
        assert result["errors"][0]["type"] == "duplicate_ip"

    def test_与既有设备_ip_冲突给警告(self):
        collection = {
            "10.0.0.2": {"info": {"interfaces": ["GE0/0/1  192.168.1.1  up  up"]}},
        }
        result = check_cross_device_conflicts(
            ["10.0.0.1"],
            {"10.0.0.1": ["interface GE0/0/1", "ip address 192.168.1.1 255.255.255.0"]},
            collection,
        )
        assert result["success"] is True
        assert result["warnings"][0]["type"] == "ip_conflict_with_existing"

    def test_同一设备自己已用的_ip_不算跨设备冲突(self):
        collection = {
            "10.0.0.1": {"info": {"interfaces": ["GE0/0/1  192.168.1.1  up  up"]}},
        }
        result = check_cross_device_conflicts(
            ["10.0.0.1"],
            {"10.0.0.1": ["interface GE0/0/2", "ip address 192.168.1.1 255.255.255.0"]},
            collection,
        )
        assert result["warnings"] == []

    def test_vlan_跨设备出现给建议(self):
        result = check_cross_device_conflicts(
            ["10.0.0.1", "10.0.0.2"],
            {"10.0.0.1": ["vlan 10"], "10.0.0.2": ["vlan 10"]},
        )
        assert [s["type"] for s in result["suggestions"]] == ["vlan_across_devices"]

    def test_ospf_网段重叠给建议(self):
        result = check_cross_device_conflicts(
            ["10.0.0.1", "10.0.0.2"],
            {
                "10.0.0.1": ["ospf 1", "network 192.168.1.0 0.0.0.255", "quit"],
                "10.0.0.2": ["ospf 1", "network 192.168.1.0 0.0.0.255"],
            },
        )
        assert [s["type"] for s in result["suggestions"]] == ["ospf_network_overlap"]

    def test_空输入不炸(self):
        result = check_cross_device_conflicts([], {})
        assert result["success"] is True
