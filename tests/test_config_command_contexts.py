from services.config_builder import build_config_commands

import main


def test_ospf_networks_are_generated_inside_area_view():
    commands = build_config_commands(
        "ospf",
        {
            "process_id": "1",
            "router_id": "1.1.1.1",
            "area": "0",
            "network": "192.168.56.0",
            "wildcard": "0.0.0.255",
            "network2": "10.0.0.0",
            "wildcard2": "0.0.0.255",
            "network3": "172.16.0.0",
            "wildcard3": "0.0.0.255",
            "interface": "GigabitEthernet0/0/0",
        },
    )

    assert commands == [
        "ospf 1 router-id 1.1.1.1",
        "area 0",
        "network 192.168.56.0 0.0.0.255",
        "network 10.0.0.0 0.0.0.255",
        "network 172.16.0.0 0.0.0.255",
        "quit",
        "quit",
        # 模板新增的接口参数组：提供了 interface 字段时进入接口视图配置后退出
        "interface GigabitEthernet0/0/0",
        "quit",
    ]


def test_ospf_area_and_process_options_stay_in_their_views():
    commands = build_config_commands(
        "ospf",
        {
            "process_id": "2",
            "router_id": "2.2.2.2",
            "area": "1",
            "area_type": "stub_no_summary",
            "auth_type": "md5",
            "auth_key_id": "7",
            "auth_key": "Secret123",
            "network": "10.0.0.0",
            "wildcard": "0.0.0.255",
            "route_aggregation": "10.0.0.0 255.255.0.0",
            "silent_interface": "GigabitEthernet0/0/1",
            "default_route_advertise": True,
            "default_route_cost": "5",
            "import_static": True,
            "import_connected": True,
            "import_bgp": True,
            "reference_bandwidth": "1000",
            "bfd_enable": True,
            "interface": "GigabitEthernet0/0/0",
            "cost": "10",
            "network_type": "p2p",
        },
    )

    assert commands == [
        "bfd",
        "quit",
        "ospf 2 router-id 2.2.2.2",
        "area 1",
        "stub no-summary",
        "authentication-mode md5 7 cipher Secret123",
        "network 10.0.0.0 0.0.0.255",
        "abr-summary 10.0.0.0 255.255.0.0",
        "quit",
        "silent-interface GigabitEthernet0/0/1",
        "default-route-advertise cost 5",
        "import-route static",
        "import-route direct",
        "import-route bgp",
        "bandwidth-reference 1000",
        "bfd all-interfaces enable",
        "quit",
        "interface GigabitEthernet0/0/0",
        "ospf cost 10",
        "ospf network-type p2p",
        "quit",
    ]


def test_ipsec_policy_references_ike_peer_and_returns_to_system_view():
    commands = build_config_commands(
        "ipsec",
        {
            "proposal_name": "prop1",
            "policy_name": "policy1",
            "peer_name": "peer1",
            "acl_number": "3000",
        },
    )

    assert "ike-peer peer1" in commands
    assert "ike peer peer1" not in commands
    assert commands[-1] == "quit"
    assert commands.count("quit") == 2


def test_snmp_does_not_reference_an_unconfigured_acl():
    commands = build_config_commands(
        "snmp",
        {"community": "public", "permission": "read", "version": "v2c"},
    )

    assert commands == [
        "snmp-agent community read public",
        "snmp-agent sys-info version v2c",
    ]


def test_protocol_templates_close_child_views():
    """声明式模板取代了旧的 generate_* 函数：每个模板都必须闭合自己的视图。"""
    cases = [
        build_config_commands(
            "ospf",
            {"process_id": "1", "area": "0",
             "network": "192.168.1.0", "wildcard": "0.0.0.255"},
        ),
        build_config_commands(
            "bgp",
            {"local_as": "65000", "peer_ip": "192.168.1.2", "peer_as": "65001"},
        ),
        build_config_commands(
            "rip",
            {"process_id": "1", "version": "2", "network": "192.168.1.0"},
        ),
        build_config_commands(
            "dhcp",
            {"enable": True, "pool_name": "pool1",
             "network": "192.168.1.0", "mask": "255.255.255.0",
             "gateway": "192.168.1.1"},
        ),
        build_config_commands(
            "acl",
            {"acl_number": "3000", "source": "192.168.1.0"},
        ),
    ]

    assert all(len(commands) > 0 for commands in cases), "模板渲染结果为空"
    assert all(commands[-1] == "quit" for commands in cases)
    # ospf：退出 area 后还要退出进程视图
    assert cases[0][-2:] == ["quit", "quit"]


def test_nat_outbound_uses_the_outside_interface_and_selected_acl():
    commands = build_config_commands(
        "nat",
        {
            "nat_type": "outbound",
            "interface": "GigabitEthernet0/0/0",
            "acl_number": "2999",
            # 仅 static/server 分支使用，outbound 分支不输出
            "global_ip": "200.1.1.1",
            "inside_ip": "192.168.1.100",
        },
    )

    assert commands == [
        "interface GigabitEthernet0/0/0",
        "nat outbound 2999",
        "quit",
    ]


def test_ipsec_and_ike_commands_have_balanced_views_and_valid_keywords():
    ipsec = build_config_commands(
        "ipsec",
        {
            "proposal_name": "p1",
            "policy_name": "pol1",
            "peer_name": "peer1",
            "acl_number": "3000",
            "local_subnet": "192.168.1.0 0.0.0.255",
            "remote_subnet": "192.168.2.0 0.0.0.255",
            "remote_ip": "203.0.113.1",
        },
    )

    assert ipsec == [
        "ipsec proposal p1",
        "quit",
        "ipsec policy pol1 10 isakmp",
        "security acl 3000",
        "ike-peer peer1",
        "proposal p1",
        "quit",
    ]
    assert ipsec[-1] == "quit"
    assert ipsec.count("quit") == 2

    ike = build_config_commands(
        "ike",
        {
            "proposal_name": "p1",
            "peer_name": "peer1",
            "peer_address": "203.0.113.1",
            "pre_shared_key": "Secret123",
        },
    )

    assert "ike proposal p1" in ike
    assert "ike peer peer1" in ike
    assert "remote-address 203.0.113.1" in ike
    assert ike[-1] == "quit"
    assert ike.count("quit") == 2


def test_branch_ospf_solution_builds_complete_multi_view_configuration():
    commands = build_config_commands(
        "branch_ospf",
        {
            "interface": "GigabitEthernet0/0/1",
            "ip_address": "10.0.12.1",
            "subnet_mask": "255.255.255.0",
            "process_id": "1",
            "router_id": "12.12.12.12",
            "area": "0",
            "network": "10.0.12.0",
            "wildcard": "0.0.0.255",
        },
    )
    assert commands == [
        "interface GigabitEthernet0/0/1",
        "ip address 10.0.12.1 255.255.255.0",
        "quit",
        "ospf 1 router-id 12.12.12.12",
        "area 0",
        "network 10.0.12.0 0.0.0.255",
        "quit",
        "quit",
    ]


def test_vlan_gateway_dhcp_solution_builds_complete_service():
    commands = build_config_commands(
        "vlan_gateway_dhcp",
        {
            "vlan_id": "10",
            "access_interface": "GigabitEthernet0/0/1",
            "gateway": "192.168.10.1",
            "subnet_mask": "255.255.255.0",
            "pool_name": "vlan10",
            "network": "192.168.10.0",
            "dns": "8.8.8.8",
        },
    )
    assert "interface Vlanif 10" in commands
    assert "dhcp select global" in commands
    assert "ip pool vlan10" in commands
    assert commands.index("dhcp enable") < commands.index("interface Vlanif 10")
    assert commands[-1] == "quit"


def test_edge_nat_solution_builds_acl_before_outbound_nat():
    commands = build_config_commands(
        "edge_easy_ip_nat",
        {
            "acl_number": "2000",
            "inside_network": "192.168.10.0",
            "inside_wildcard": "0.0.0.255",
            "outside_interface": "GigabitEthernet0/0/0",
        },
    )
    assert commands == [
        "acl number 2000",
        "rule permit source 192.168.10.0 0.0.0.255",
        "quit",
        "interface GigabitEthernet0/0/0",
        "nat outbound 2000",
        "quit",
    ]
