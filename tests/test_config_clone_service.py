from services.config_clone_service import (
    block_to_commands,
    build_clone_preview,
    split_config_blocks,
)


SOURCE_CONFIG = """
[V200R003C00]
#
 sysname AR1
#
 lldp enable
#
aaa
 local-user admin password cipher SECRET
 local-user admin service-type ssh
#
interface GigabitEthernet0/0/0
 ip address 192.168.56.11 255.255.255.0
#
interface GigabitEthernet0/0/1
 description LAN
 ip address 10.0.0.1 255.255.255.0
#
ospf 1 router-id 1.1.1.1
 area 0.0.0.0
  network 10.0.0.0 0.0.0.255
 silent-interface GigabitEthernet0/0/0
#
isis 1
 network-entity 49.0001.0010.0000.0001.00
#
return
"""


TARGET_CONFIG = """
#
 sysname AR2
#
interface GigabitEthernet0/0/0
 ip address 192.168.56.12 255.255.255.0
#
interface GigabitEthernet0/0/1
 ip address 10.0.0.2 255.255.255.0
#
return
"""


def test_block_to_commands_closes_nested_ospf_views():
    ospf = next(
        block for block in split_config_blocks(SOURCE_CONFIG)
        if block[0][1].startswith("ospf ")
    )

    assert block_to_commands(ospf) == [
        "ospf 1 router-id 1.1.1.1",
        "area 0.0.0.0",
        "network 10.0.0.0 0.0.0.255",
        "quit",
        "silent-interface GigabitEthernet0/0/0",
        "quit",
    ]


def test_block_to_commands_closes_single_line_view():
    assert block_to_commands([(0, "ospf 1 router-id 1.1.1.1")]) == [
        "ospf 1 router-id 1.1.1.1",
        "quit",
    ]
    assert block_to_commands([(0, "lldp enable")]) == ["lldp enable"]


def test_clone_preview_adapts_identity_router_ids_and_interface_ips():
    result = build_clone_preview(
        source_device={"ip": "192.168.56.11", "name": "AR1"},
        source_config=SOURCE_CONFIG,
        target_devices=[{"ip": "192.168.56.12", "name": "AR2"}],
        target_configs={"192.168.56.12": TARGET_CONFIG},
    )

    target = result["targets"]["192.168.56.12"]
    commands = target["commands"]

    assert result["success"] is True
    assert "sysname AR2" not in commands  # already exists on the target
    assert "ip address 192.168.56.12 255.255.255.0" not in commands
    assert "ip address 10.0.0.2 255.255.255.0" in commands
    assert "ospf 1 router-id 192.168.56.12" in commands
    assert any(cmd.startswith("network-entity 49.") for cmd in commands)
    assert not any("password" in cmd for cmd in commands)
    assert any(item["type"] == "router_id" for item in target["resolutions"])
    assert any(item["type"] == "sensitive_domain" for item in target["skipped"])


def test_clone_preview_allocates_free_ip_when_target_interface_has_no_ip():
    target_config = """
#
 sysname AR3
#
interface GigabitEthernet0/0/0
 ip address 192.168.56.13 255.255.255.0
#
interface GigabitEthernet0/0/1
#
return
"""
    result = build_clone_preview(
        source_device={"ip": "192.168.56.11", "name": "AR1"},
        source_config=SOURCE_CONFIG,
        target_devices=[{"ip": "192.168.56.13", "name": "AR3"}],
        target_configs={"192.168.56.13": target_config},
    )

    target = result["targets"]["192.168.56.13"]
    assert "ip address 10.0.0.13 255.255.255.0" in target["commands"]
    assert any(
        item["type"] == "interface_ip" and item["to"].startswith("ip address 10.0.0.13")
        for item in target["resolutions"]
    )


def test_clone_preview_skips_missing_physical_interface():
    target_config = """
#
 sysname AR4
#
interface GigabitEthernet0/0/0
 ip address 192.168.56.14 255.255.255.0
#
return
"""
    result = build_clone_preview(
        source_device={"ip": "192.168.56.11", "name": "AR1"},
        source_config=SOURCE_CONFIG,
        target_devices=[{"ip": "192.168.56.14", "name": "AR4"}],
        target_configs={"192.168.56.14": target_config},
    )

    target = result["targets"]["192.168.56.14"]
    assert "interface GigabitEthernet0/0/1" not in target["commands"]
    assert any(item["type"] == "missing_interface" for item in target["skipped"])


def test_clone_preview_avoids_ips_reserved_by_other_devices():
    target_config = """
#
 sysname AR3
#
interface GigabitEthernet0/0/0
 ip address 192.168.56.13 255.255.255.0
#
interface GigabitEthernet0/0/1
#
return
"""
    result = build_clone_preview(
        source_device={"ip": "192.168.56.11", "name": "AR1"},
        source_config=SOURCE_CONFIG,
        target_devices=[{"ip": "192.168.56.13", "name": "AR3"}],
        target_configs={"192.168.56.13": target_config},
        reserved_ips={"10.0.0.13"},
    )

    commands = result["targets"]["192.168.56.13"]["commands"]
    assert "ip address 10.0.0.13 255.255.255.0" not in commands
    assert "ip address 10.0.0.2 255.255.255.0" in commands
