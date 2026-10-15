from services.device_connector import command_result_from_output, get_cli_error
from services import backup_service

import main
import routes_config
import routes_devices


FULL_CONFIG = """[V200R003C00]
#
 sysname R1
#
interface GigabitEthernet0/0/0
 ip address 192.168.56.11 255.255.255.0
 ospf cost 10
 nat outbound 2000
 dhcp select global
 isis enable 1
#
ospf 1 router-id 1.1.1.1
 area 0.0.0.0
  network 192.168.56.0 0.0.0.255
#
rip 1
 version 2
 network 192.168.56.0
#
bgp 65000
 peer 192.168.56.12 as-number 65001
#
isis 1
 network-entity 49.0001.0010.0000.0001.00
#
ip route-static 10.0.0.0 255.255.255.0 192.168.56.1
#
dhcp enable
#
ip pool users
 network 192.168.10.0 mask 255.255.255.0
#
acl number 2000
 rule 5 permit source 192.168.10.0 0.0.0.255
#
ntp-service unicast-server 192.0.2.10
#
snmp-agent
 snmp-agent sys-info version v2c
#
ike peer peer1 v1
 pre-shared-key cipher Secret123
#
ipsec policy map1 1 isakmp
 security acl 2000
 ike-peer peer1
#
return
"""


def test_fast_collection_uses_portable_full_current_configuration():
    unsupported = "display current-configuration | section ^interface"

    assert "display current-configuration" in main.HUAWEI_COMMANDS
    assert "display current-configuration" in main.HUAWEI_COMMANDS_FAST
    assert all(unsupported not in command for command in main.HUAWEI_COMMANDS)
    assert all(unsupported not in command for command in main.HUAWEI_COMMANDS_FAST)


def test_device_cli_rejection_is_not_reported_as_success():
    output = "                                     ^\r\nError: Unrecognized command found at '^' position.\r\n"

    result = command_result_from_output(output)

    assert result["success"] is False
    assert result["output"] == output
    assert "Unrecognized command" in result["error"]
    assert get_cli_error("OSPF Process 1 with Router ID 1.1.1.1") is None


def test_backup_rejects_cli_error_instead_of_saving_it(monkeypatch, tmp_path):
    class FakeConnection:
        def send_command(self, command, read_timeout):
            return "Error: Unrecognized command found at '^' position."

        def disconnect(self):
            pass

    monkeypatch.setattr(
        backup_service,
        "import_console",
        lambda: (lambda **params: FakeConnection(), Exception, Exception),
    )
    # 备份目录现在由 ensure_backup_dir() 按当前拓扑解析，打桩该函数即可
    monkeypatch.setattr(backup_service, "ensure_backup_dir", lambda: tmp_path)

    result = backup_service.backup_device_config({"ip": "192.0.2.10", "name": "R1"})

    assert result["success"] is False
    assert "设备拒绝获取当前配置" in result["error"]
    assert list(tmp_path.iterdir()) == []


def test_parse_config_sections_covers_all_supported_protocol_forms():
    info = {"protocols": [], "config_sections": {}}

    routes_devices._parse_config_sections(info, FULL_CONFIG)

    expected = {
        "ospf", "rip", "bgp", "isis", "static", "nat", "dhcp",
        "acl", "ntp", "snmp", "ipsec",
    }
    assert set(info["protocols"]) == expected
    assert set(info["config_sections"]) == expected

    ospf = info["config_sections"]["ospf"]["config"]
    assert "ospf 1 router-id 1.1.1.1" in ospf
    assert " area 0.0.0.0" in ospf
    assert "interface GigabitEthernet0/0/0" in ospf
    assert " ospf cost 10" in ospf

    assert "interface GigabitEthernet0/0/0" in info["config_sections"]["nat"]["config"]
    assert " nat outbound 2000" in info["config_sections"]["nat"]["config"]
    assert "ip pool users" in info["config_sections"]["dhcp"]["config"]
    assert "ike peer peer1 v1" in info["config_sections"]["ipsec"]["config"]
    assert "ipsec policy map1 1 isakmp" in info["config_sections"]["ipsec"]["config"]


def test_parse_device_info_uses_full_configuration_result():
    info = routes_devices._parse_device_info({
        "display current-configuration": {
            "success": True,
            "output": FULL_CONFIG,
            "error": None,
        }
    })

    assert "ospf" in info["protocols"]
    assert "nat" in info["protocols"]
    assert info["config_sections"]["ospf"]["configured"] is True


def test_protocol_collection_refreshes_stale_invalid_cache(monkeypatch):
    ip = "192.0.2.1"
    stale = {
        "ok": True,
        "commands": {
            "display current-configuration | section ^ospf": {
                "success": False,
                "output": "Error: Unrecognized command",
                "error": "Error: Unrecognized command",
            }
        },
        "info": {"protocols": [], "config_sections": {}},
    }
    monkeypatch.setitem(main.state["collection_results"], ip, stale)
    monkeypatch.setattr(routes_devices, "load_devices", lambda: [{"ip": ip, "name": "R1"}])
    monkeypatch.setattr(
        routes_config,
        "_load_config_for_clone",
        lambda device, refresh: {
            "success": True,
            "config": FULL_CONFIG,
            "source": "live",
            "backup_file": "R1.cfg",
            "errors": [],
        },
    )

    result = routes_devices._ensure_protocol_collection(ip)

    assert result["commands"]["display current-configuration"]["output"] == FULL_CONFIG
    assert result["config_source"] == "live"
    assert "ospf" in result["info"]["protocols"]
    assert main.state["collection_results"][ip] is result


def test_protocol_collection_falls_back_to_labeled_backup(monkeypatch):
    ip = "192.0.2.2"
    monkeypatch.delitem(main.state["collection_results"], ip, raising=False)
    monkeypatch.setattr(routes_devices, "load_devices", lambda: [{"ip": ip, "name": "R2"}])
    monkeypatch.setattr(
        routes_config,
        "_load_config_for_clone",
        lambda device, refresh: {
            "success": True,
            "config": FULL_CONFIG,
            "source": "backup",
            "backup_file": "R2_latest.cfg",
            "errors": ["实时获取失败: timed out"],
        },
    )

    result = routes_devices._ensure_protocol_collection(ip)

    assert result["config_source"] == "backup"
    assert result["config_backup_file"] == "R2_latest.cfg"
    assert result["config_warnings"] == ["实时获取失败: timed out"]
    assert result["ok"] is False
