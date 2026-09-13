import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from services import config_builder as cb

def show(title, cmds):
    print(f"\n=== {title} ===")
    for c in cmds:
        print("  ", c)
def main():

    # 1) 基础
    show("interface_ip", cb.build_config_commands("interface_ip",
        {"interface": "GE0/0/0", "ip_address": "192.168.1.1", "subnet_mask": "255.255.255.0"}))
    # 2) 必填缺失 -> []
    show("interface_ip MISSING", cb.build_config_commands("interface_ip", {"interface": "GE0/0/0"}))

    # 3) OSPF 复杂
    ospf_full = {
        "process_id": "1", "area": "0", "network": "10.0.0.0", "wildcard": "0.0.0.255",
        "router_id": "1.1.1.1", "area_type": "stub", "network2": "172.16.0.0", "wildcard2": "0.0.0.255",
        "import_static": True, "default_route_advertise": True, "default_route_cost": "10",
        "interface": "GE0/0/1", "cost": "100", "auth_type": "md5", "auth_key": "Huawei@123", "auth_key_id": "1",
    }
    show("ospf FULL", cb.build_config_commands("ospf", ospf_full))
    # OSPF 最简（仅必填）
    show("ospf MIN", cb.build_config_commands("ospf",
        {"process_id": "1", "area": "0", "network": "10.0.0.0", "wildcard": "0.0.0.255"}))
    # OSPF 缺 network -> []
    show("ospf MISSING", cb.build_config_commands("ospf", {"process_id": "1", "area": "0"}))

    # 4) NAT 三分支
    show("nat OUTBOUND", cb.build_config_commands("nat",
        {"nat_type": "outbound", "acl_number": "2000", "interface": "GE0/0/0", "global_ip": "1.1.1.1", "inside_ip": "2.2.2.2"}))
    show("nat STATIC", cb.build_config_commands("nat",
        {"nat_type": "static", "acl_number": "2000", "interface": "GE0/0/0", "global_ip": "1.1.1.1", "inside_ip": "2.2.2.2"}))
    show("nat SERVER", cb.build_config_commands("nat",
        {"nat_type": "server", "acl_number": "2000", "interface": "GE0/0/0", "global_ip": "1.1.1.1", "inside_ip": "2.2.2.2"}))

    # 5) STP / LLDP checkbox
    show("stp ENABLE", cb.build_config_commands("stp", {"enable": True, "mode": "mstp", "priority": "32768"}))
    show("stp DISABLE", cb.build_config_commands("stp", {"enable": False}))
    show("lldp ENABLE", cb.build_config_commands("lldp", {"enable": True}))

    # 6) ACL destination any vs not
    show("acl ANY", cb.build_config_commands("acl",
        {"acl_number": "3000", "rule_type": "permit", "protocol": "ip", "source": "192.168.1.0 0.0.0.255", "destination": "any"}))
    show("acl DEST", cb.build_config_commands("acl",
        {"acl_number": "3000", "rule_type": "deny", "protocol": "tcp", "source": "192.168.1.0 0.0.0.255", "destination": "10.0.0.0 0.0.0.255"}))

    # 7) VPN instance 可选 interface
    show("vpn NO-INTF", cb.build_config_commands("vpn_instance", {"name": "vpn1", "rd": "100:1"}))
    show("vpn INTf", cb.build_config_commands("vpn_instance", {"name": "vpn1", "rd": "100:1", "interface": "GE0/0/2"}))

    # 8) 数量统计
    print("\n=== 模板总数:", len(cb.CONFIG_TEMPLATES), "===")
    print("=== get_all_templates keys:", len(cb.get_all_templates()), "===")



if __name__ == "__main__":
    main()
