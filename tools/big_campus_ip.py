"""
big_campus_ip.py - 15 台分层园区网的 IP 规划与批量配置
=====================================================
给 tools/big_campus.py 的拓扑按链路配置接口 IP，使数据面真的通。
规划原则：每条链路一个 /30 点分网段，网段号随链路序号递增，便于对照与排障。

    核心三角互联（成环，OSPF 骨干）：
      R1.GE0/0/0 ↔ R2.GE0/0/0    10.0.12.0/30   R1=.1  R2=.2
      R1.GE0/0/1 ↔ R3.GE0/0/1    10.0.13.0/30   R1=.1  R3=.2
      R2.GE0/0/3 ↔ R3.GE0/0/3    10.0.23.0/30   R2=.1  R3=.2
    出口防火墙：
      FW1.GE0/0/0 ↔ R1.GE0/0/2   10.0.14.0/30   FW1=.1 R1=.2
      FW1.GE0/0/1 ↔ R3.GE0/0/2   10.0.34.0/30   FW1=.1 R3=.2
    核心 → 汇聚（各一条）：
      R1.GE0/0/3 ↔ SW1.GE0/0/1   10.0.15.0/30   R1=.1  SW1=.2
      R2.GE0/0/1 ↔ SW2.GE0/0/1   10.0.25.0/30   R2=.1  SW2=.2
      R3.GE0/0/0 ↔ SW3.GE0/0/1   10.0.35.0/30   R3=.1  SW3=.2
    无线：
      R2.GE0/0/2 ↔ AC1.GE0/0/1   10.0.26.0/30   R2=.1  AC1=.2
      AC1.GE0/0/2 ↔ AP1.GE0/0/0  10.0.27.0/30   AC1=.1 AP1=.2
    汇聚 → 接入（接入层 S3700 是**二层交换机**，物理接口不能配 IP，
    所以这一段走 VLAN trunk，用 Vlanif 做三层网关）：
      SW1.GE0/0/2 ↔ SW4.GE0/0/1   trunk(vlan10/20/30)
      SW2.GE0/0/3 ↔ SW5.GE0/0/1   trunk
      SW3.GE0/0/2 ↔ SW6.GE0/0/1   trunk
      网关在汇聚层交换机的 Vlanif 上：
        SW1 Vlanif10 = 192.168.10.1/24（PC1 的网关）
        SW2 Vlanif20 = 192.168.20.1/24（PC2 的网关）
        SW3 Vlanif30 = 192.168.30.1/24（PC3 的网关）
    接入层 VLAN 成员（终端接的口设为 access）：
      SW4.Ethernet0/0/1 → vlan10 → PC1 192.168.10.10
      SW5.Ethernet0/0/1 → vlan20 → PC2 192.168.20.10
      SW6.Ethernet0/0/1 → vlan30 → PC3 192.168.30.10

OSPF：5 台三层设备（R1/R2/R3/SW1/SW2/SW3）进程 1，区域 0，
router-id 用 192.168.1.{i} 递增（正好是批量变量化下发的用例）。
"""

from __future__ import annotations

from typing import Any, Dict, List

# 链路 IP 规划：(设备名, 接口名, IP/掩码)
# 点分网段的第二段即链路编号（10.0.AB.0/30 中 AB 是两端设备编号组合）
_LINK_IPS: List[Dict[str, str]] = [
    # 核心三角互联
    {"dev": "R1", "iface": "GigabitEthernet0/0/0", "ip": "10.0.12.1", "mask": "30"},
    {"dev": "R2", "iface": "GigabitEthernet0/0/0", "ip": "10.0.12.2", "mask": "30"},
    {"dev": "R1", "iface": "GigabitEthernet0/0/1", "ip": "10.0.13.1", "mask": "30"},
    {"dev": "R3", "iface": "GigabitEthernet0/0/1", "ip": "10.0.13.2", "mask": "30"},
    {"dev": "R2", "iface": "GigabitEthernet0/0/3", "ip": "10.0.23.1", "mask": "30"},
    {"dev": "R3", "iface": "GigabitEthernet0/0/3", "ip": "10.0.23.2", "mask": "30"},
    # 出口
    {"dev": "FW1", "iface": "GigabitEthernet0/0/0", "ip": "10.0.14.1", "mask": "30"},
    {"dev": "R1", "iface": "GigabitEthernet0/0/2", "ip": "10.0.14.2", "mask": "30"},
    {"dev": "FW1", "iface": "GigabitEthernet0/0/1", "ip": "10.0.34.1", "mask": "30"},
    {"dev": "R3", "iface": "GigabitEthernet0/0/2", "ip": "10.0.34.2", "mask": "30"},
    # 核心 → 汇聚
    {"dev": "R1", "iface": "GigabitEthernet0/0/3", "ip": "10.0.15.1", "mask": "30"},
    {"dev": "SW1", "iface": "GigabitEthernet0/0/1", "ip": "10.0.15.2", "mask": "30"},
    {"dev": "R2", "iface": "GigabitEthernet0/0/1", "ip": "10.0.25.1", "mask": "30"},
    {"dev": "SW2", "iface": "GigabitEthernet0/0/1", "ip": "10.0.25.2", "mask": "30"},
    {"dev": "R3", "iface": "GigabitEthernet0/0/0", "ip": "10.0.35.1", "mask": "30"},
    {"dev": "SW3", "iface": "GigabitEthernet0/0/1", "ip": "10.0.35.2", "mask": "30"},
    # 无线（AC1/AP1）：AC6005 与 AP6050 的 GE 口均**不支持 ip address**
    #（实测 `Error: Unrecognized command`），无线设备靠 AC 统一下发管理，
    # 不在此处配接口 IP。
    # 汇聚 → 接入（S3700 是二层交换机，物理口不配 IP，只做 trunk）
]

_MASKS = {"30": "255.255.255.252", "24": "255.255.255.0"}

# 三层设备的 OSPF 进程（router-id 用序列生成，正好当批量下发的用例）
_OSPF_DEVICES = ["R1", "R2", "R3", "SW1", "SW2", "SW3"]

# 汇聚 → 接入的 trunk 链路：(汇聚设备, 汇聚接口, 接入设备, 接入设备接口)
# 接口名必须与 tools/big_campus.py 的连线完全一致——否则 trunk 会配到
# 一个没接线的口上（表现为配置成功但 VLAN 不通）。
_TRUNK_LINKS = [
    ("SW1", "GigabitEthernet0/0/2", "SW4", "GigabitEthernet0/0/1"),
    ("SW2", "GigabitEthernet0/0/2", "SW5", "GigabitEthernet0/0/1"),
    ("SW3", "GigabitEthernet0/0/2", "SW6", "GigabitEthernet0/0/1"),
]

# 接入层 VLAN：网关在汇聚层交换机的 Vlanif 上（S3700 物理口不能配 IP）
#   vlan_id, 终端网段, 网关设备, 网关 Vlanif, 接入交换机, 接入交换机的 access 口
_VLANS = [
    {"vlan": 10, "net": "192.168.10.0", "mask": "24", "gw_dev": "SW1",
     "gw_iface": "Vlanif10", "gw_ip": "192.168.10.1", "access_sw": "SW4",
     "access_port": "Ethernet0/0/1", "pc_ip": "192.168.10.10"},
    {"vlan": 20, "net": "192.168.20.0", "mask": "24", "gw_dev": "SW2",
     "gw_iface": "Vlanif20", "gw_ip": "192.168.20.1", "access_sw": "SW5",
     "access_port": "Ethernet0/0/1", "pc_ip": "192.168.20.10"},
    {"vlan": 30, "net": "192.168.30.0", "mask": "24", "gw_dev": "SW3",
     "gw_iface": "Vlanif30", "gw_ip": "192.168.30.1", "access_sw": "SW6",
     "access_port": "Ethernet0/0/1", "pc_ip": "192.168.30.10"},
]


# 需要从二层口转成三层口才能在物理接口上配 IP 的型号。
# 实测结论（eNSP VRP 5.110）：S5700 的 GE 口默认是二层口，
# 直接 `ip address` 会报 `Error: Unrecognized command`；
# 必须先 `undo portswitch`。路由器（Router/AR 系列）的接口本来就是三层，无需此步。
# 无线设备（AC6005/AP6050）的 GE 口实测既不支持 portswitch 也不支持 ip address。
_SWITCH_MODELS_NEEDING_PORTSWITCH = {"S5700", "S3700", "S5720", "S6700"}

# 设备名 → eNSP 型号（与 tools/big_campus.py 保持一致）
_MODEL_OF: Dict[str, str] = {
    "FW1": "USG5500",
    "R1": "Router", "R2": "Router", "R3": "Router",
    "SW1": "S5700", "SW2": "S5700", "SW3": "S5700",
    "SW4": "S3700", "SW5": "S3700", "SW6": "S3700",
    "AC1": "AC6005", "AP1": "AP6050",
}


def build_ip_plan() -> Dict[str, Any]:
    """
    按设备聚合出每台设备的接口配置命令。

    VRP 5.110 上交换机物理口配 IP 有两个坑，都靠命令顺序规避：

    1. **二层口不能配 IP**：S5700 的 GE 口默认 L2，直接 `ip address` 报
       `Error: Unrecognized command`，必须先 `undo portswitch` 转 L3。
    2. **反复下发的中间态**：接口已是 L3 但没有地址时，`ip address` 同样报
       Unrecognized；而此时再 `undo ip address`（没有地址可撤销）会雪上加霜。
       所以**不要**在转 L3 之前发 `undo ip address`——只发 `undo portswitch`
       （ tolerate 它的 "already a L3" 报错），然后直接配地址。

    由此得到的顺序是：`undo portswitch` → `ip address` → `undo shutdown`。
    想让方案可重复执行，靠的是 portswitch + ip address 都是幂等的
    （portswitch 已转过会报错但状态不变，地址相同再配一次也报错但状态不变）。
    """
    per_dev: Dict[str, List[str]] = {}
    for entry in _LINK_IPS:
        cmds = per_dev.setdefault(entry["dev"], [])
        model = _MODEL_OF.get(entry["dev"], "")
        cmds.append(f"interface {entry['iface']}")
        if model in _SWITCH_MODELS_NEEDING_PORTSWITCH:
            cmds.append(" undo portswitch")
        cmds.append(f" ip address {entry['ip']} {_MASKS[entry['mask']]}")
        cmds.append(" undo shutdown")
        cmds.append(" quit")

    # OSPF（进程 1，区域 0），每条链路网络都要宣告
    for dev in _OSPF_DEVICES:
        cmds = per_dev.setdefault(dev, [])
        cmds.append("ospf 1")
        for entry in _LINK_IPS:
            if entry["dev"] != dev:
                continue
            net = _network_of(entry["ip"], entry["mask"])
            wildcard = _wildcard_of(entry["mask"])
            if net:
                cmds.append(f" area 0")
                cmds.append(f"  network {net} {wildcard}")
        cmds.append(" quit")

    # 汇聚 → 接入的 trunk，以及接入层的 VLAN/access 口。
    # 注入前先让每个 VLAN 在三层设备上都存在，Vlanif 才能起来。
    vlan_ids = [spec["vlan"] for spec in _VLANS]
    for spec in _VLANS:
        # 三层设备：建 VLAN + Vlanif 网关 + 接入 trunk 口
        cmds = per_dev.setdefault(spec["gw_dev"], [])
        for vid in vlan_ids:
            cmds.append(f"vlan {vid}")
            cmds.append(" quit")
        cmds.append(f"interface {spec['gw_iface']}")
        cmds.append(f" ip address {spec['gw_ip']} {_MASKS[spec['mask']]}")
        cmds.append(" undo shutdown")
        cmds.append(" quit")
        # OSPF 宣告终端网段
        cmds.append("ospf 1")
        cmds.append(" area 0")
        cmds.append(f"  network {spec['net']} {_wildcard_of(spec['mask'])}")
        cmds.append(" quit")

    for agg_dev, agg_if, acc_dev, acc_if in _TRUNK_LINKS:
        # 汇聚侧：设为 trunk 并放行全部 VLAN
        cmds = per_dev.setdefault(agg_dev, [])
        cmds.append(f"interface {agg_if}")
        cmds.append(" port link-type trunk")
        cmds.append(" port trunk allow-pass vlan " + " ".join(str(v) for v in vlan_ids))
        cmds.append(" undo shutdown")
        cmds.append(" quit")
        # 接入侧：建 VLAN + trunk
        cmds = per_dev.setdefault(acc_dev, [])
        for vid in vlan_ids:
            cmds.append(f"vlan {vid}")
            cmds.append(" quit")
        cmds.append(f"interface {acc_if}")
        cmds.append(" port link-type trunk")
        cmds.append(" port trunk allow-pass vlan " + " ".join(str(v) for v in vlan_ids))
        cmds.append(" undo shutdown")
        cmds.append(" quit")

    # 接入交换机：接终端的口设为 access
    for spec in _VLANS:
        cmds = per_dev.setdefault(spec["access_sw"], [])
        cmds.append(f"interface {spec['access_port']}")
        cmds.append(" port link-type access")
        cmds.append(f" port default vlan {spec['vlan']}")
        cmds.append(" undo shutdown")
        cmds.append(" quit")

    return per_dev


def _network_of(ip: str, mask: str) -> str:
    """由 IP 与掩码位数算网络地址。"""
    try:
        import ipaddress
        return str(ipaddress.ip_network(f"{ip}/{mask}", strict=False).network_address)
    except ValueError:
        return ""


def _wildcard_of(mask: str) -> str:
    """掩码位数 → OSPF 反掩码（/30 → 0.0.0.3）。"""
    try:
        import ipaddress
        net = ipaddress.ip_network(f"0.0.0.0/{mask}")
        return str(ipaddress.IPv4Address(int(net.netmask) ^ 0xFFFFFFFF))
    except ValueError:
        return "0.0.0.0"


def ospf_series_spec() -> Dict[str, Any]:
    """OSPF router-id 的序列定义（需求⑧：批量变量化下发）。"""
    return {"base": {"router_id": "192.168.1.{i}"}, "start": 1, "step": 1, "width": 0}


def ospf_router_id_commands() -> Dict[str, List[str]]:
    """每台三层设备的 router-id 配置（序列展开后的结果）。"""
    out = {}
    for idx, dev in enumerate(_OSPF_DEVICES, start=1):
        out[dev] = [f"ospf 1 router-id 192.168.1.{idx}"]
    return out


def summary() -> Dict[str, Any]:
    """统计信息，供脚本与测试使用。"""
    per_dev = build_ip_plan()
    return {
        "devices_with_config": sorted(per_dev),
        "commands_per_device": {k: len(v) for k, v in per_dev.items()},
        "link_count": len(_LINK_IPS) // 2,
        "trunk_links": len(_TRUNK_LINKS),
        "ospf_devices": _OSPF_DEVICES,
        "vlans": _VLANS,
    }


if __name__ == "__main__":
    import json
    print(json.dumps(summary(), ensure_ascii=False, indent=2))
    print("\n=== R1 的命令 ===")
    for line in build_ip_plan().get("R1", []):
        print(" ", line)
