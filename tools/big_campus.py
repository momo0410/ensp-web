"""
big_campus.py - 14 台设备分层园区网综合案例（验证用）
====================================================
比内置的 comprehensive_campus（7 台）更大，用于验证：
- 拓扑一次性生成/注入/开机（需求①②）
- 单台启停（需求③）
- 能力识别与协议角色（需求⑤）
- 批量变量化下发：OSPF 区域 0 中 5 台设备 IP 依次递增（需求⑧）
- 逻辑路径追踪：PC → 跨三层 → 对端 PC（需求④）

拓扑（三层 + 出口 + 无线 + 终端）：

    出口:  FW1 (USG5500)
    核心:  R1 ─ R2 ─ R3         ← OSPF 区域 0，三者两两互联
    汇聚:  SW1 ─ SW2 ─ SW3      ← S5700 三层，各双上行到核心
    接入:  SW4 ─ SW5 ─ SW6      ← S3700 二层，各单上行到汇聚
    无线:  AC1 ─ AP1
    终端:  PC1 ─ PC2 ─ PC3

共 14 台。Console 端口从 2000 顺序分配，与 IP 末段一致（便于对照）。
"""

from __future__ import annotations

import ipaddress
from typing import Any, Dict, List

# 各层设备的横向排布步长（eNSP 拓扑画布坐标）
_X_STEP = 260.0
_Y_CORE = 260.0
_Y_AGG = 420.0
_Y_ACCESS = 580.0

BIG_CAMPUS_NAME = "big_campus"

# 三层 + 出口 + 无线 + 终端的完整设备清单
_DEVICE_DEFS: List[Dict[str, Any]] = [
    # 出口防火墙
    {"name": "FW1", "model": "USG5500", "cx": 130, "cy": 120},
    # 核心层：三台路由器，OSPF 区域 0 骨干
    {"name": "R1", "model": "Router", "cx": 130, "cy": _Y_CORE},
    {"name": "R2", "model": "Router", "cx": 130 + _X_STEP, "cy": _Y_CORE},
    {"name": "R3", "model": "Router", "cx": 130 + _X_STEP * 2, "cy": _Y_CORE},
    # 汇聚层：三层交换机
    {"name": "SW1", "model": "S5700", "cx": 130, "cy": _Y_AGG},
    {"name": "SW2", "model": "S5700", "cx": 130 + _X_STEP, "cy": _Y_AGG},
    {"name": "SW3", "model": "S5700", "cx": 130 + _X_STEP * 2, "cy": _Y_AGG},
    # 接入层：二层交换机
    {"name": "SW4", "model": "S3700", "cx": 130, "cy": _Y_ACCESS},
    {"name": "SW5", "model": "S3700", "cx": 130 + _X_STEP, "cy": _Y_ACCESS},
    {"name": "SW6", "model": "S3700", "cx": 130 + _X_STEP * 2, "cy": _Y_ACCESS},
    # 无线
    {"name": "AC1", "model": "AC6005", "cx": 130 + _X_STEP * 3, "cy": _Y_AGG},
    {"name": "AP1", "model": "AP6050", "cx": 130 + _X_STEP * 3, "cy": _Y_ACCESS},
    # 终端（无 Console）
    {"name": "PC1", "model": "PC", "cx": 130 + _X_STEP * 3, "cy": _Y_CORE,
     "ip_address": "192.168.30.10", "netmask": "255.255.255.0", "gateway": "192.168.30.1"},
    {"name": "PC2", "model": "PC", "cx": 130 + _X_STEP * 4, "cy": _Y_CORE,
     "ip_address": "192.168.40.10", "netmask": "255.255.255.0", "gateway": "192.168.40.1"},
    {"name": "PC3", "model": "PC", "cx": 130 + _X_STEP * 4, "cy": _Y_ACCESS,
     "ip_address": "192.168.50.10", "netmask": "255.255.255.0", "gateway": "192.168.50.1"},
]


def _link(a_dev: str, a_if: str, b_dev: str, b_if: str) -> dict:
    return {"a": {"device": a_dev, "interface": a_if},
            "b": {"device": b_dev, "interface": b_if}}


# 各型号可用接口（与 tools/gen_topology.py 的 DEVICE_MODELS 对齐）：
#   Router    GE0/0/0~3 + Serial0/0/0~1
#   S5700     GE0/0/1~24
#   S3700     Ethernet0/0/1~24
#   USG5500   GE0/0/0~5
#   AC6005    GE0/0/1~6
#   AP6050    GE0/0/0~1
#   PC        仅 GE0/0/0
# 核心路由器只有 4 个 GE，因此核心三角互联 + 出口 + 汇聚的分配要精打细算。


def build_big_campus() -> Dict[str, Any]:
    """构造 15 台设备的拓扑描述（可直接喂 /api/ensp/topo/apply）。"""
    devices: List[dict] = []
    for idx, spec in enumerate(_DEVICE_DEFS):
        entry: Dict[str, Any] = {
            "name": spec["name"],
            "model": spec["model"],
            "cx": spec["cx"],
            "cy": spec["cy"],
        }
        if spec["model"] in ("PC", "Laptop", "MCS", "STA"):
            entry.update({
                "ip_address": spec["ip_address"],
                "netmask": spec["netmask"],
                "gateway": spec["gateway"],
            })
        else:
            entry["com_port"] = 2000 + idx
        devices.append(entry)

    # 核心路由器 GE0/0/0~3 的分配（每台 4 个口）：
    #   R1: GE0/0/0→R2   GE0/0/1→R3   GE0/0/2→FW1   GE0/0/3→SW1
    #   R2: GE0/0/0→R1   GE0/0/1→SW2  GE0/0/2→AC1   GE0/0/3→R3
    #   R3: GE0/0/0→SW3  GE0/0/1→R1   GE0/0/2→FW1   GE0/0/3→R2
    # 汇聚层 GE 口充裕，接入层用 Ethernet 口。
    links = [
        # 出口防火墙 ↔ 核心（双挂）
        _link("FW1", "GE0/0/0", "R1", "GE0/0/2"),
        _link("FW1", "GE0/0/1", "R3", "GE0/0/2"),
        # 核心三角互联（OSPF 骨干，形成环以验证路由收敛）
        _link("R1", "GE0/0/0", "R2", "GE0/0/0"),
        _link("R1", "GE0/0/1", "R3", "GE0/0/1"),
        _link("R2", "GE0/0/3", "R3", "GE0/0/3"),
        # 核心 → 汇聚（单上行，核心口不够）
        _link("R1", "GE0/0/3", "SW1", "GE0/0/1"),
        _link("R2", "GE0/0/1", "SW2", "GE0/0/1"),
        _link("R3", "GE0/0/0", "SW3", "GE0/0/1"),
        # 无线
        _link("R2", "GE0/0/2", "AC1", "GE0/0/1"),
        _link("AC1", "GE0/0/2", "AP1", "GE0/0/0"),
        # 汇聚 → 接入
        _link("SW1", "GE0/0/2", "SW4", "GE0/0/1"),
        _link("SW2", "GE0/0/2", "SW5", "GE0/0/1"),
        _link("SW3", "GE0/0/2", "SW6", "GE0/0/1"),
        # 接入 → 终端（各占一个 VLAN 网段）
        _link("SW4", "Ethernet0/0/1", "PC1", "GE0/0/0"),
        _link("SW5", "Ethernet0/0/1", "PC2", "GE0/0/0"),
        _link("SW6", "Ethernet0/0/1", "PC3", "GE0/0/0"),
    ]

    return {"name": BIG_CAMPUS_NAME, "devices": devices, "links": links}


# ---------------------------------------------------------------------------
# 供批量序列化下发验证用的网段规划（需求⑧）
# ---------------------------------------------------------------------------
# OSPF 骨干：5 台设备（R1/R2/R3/SW1/SW2）的 router-id 依次递增，
# 正好是"同一模板、5 台设备、IP 依次 192.168.1.1~5"的场景。
OSPF_SERIES_SPEC = {
    "base": {"router_id": "192.168.1.{i}"},
    "start": 1,
    "step": 1,
    "width": 0,
}


def ospf_series_targets() -> List[str]:
    """参与 OSPF 序列下发的 5 台设备（按序号分配顺序）。"""
    return ["127.0.0.1:2001", "127.0.0.1:2002", "127.0.0.1:2003",
            "127.0.0.1:2004", "127.0.0.1:2005"]


def expected_router_ids() -> Dict[str, str]:
    """预期每台设备拿到的 router-id，供验证断言用。"""
    return {ip: f"192.168.1.{i}" for i, ip in enumerate(ospf_series_targets(), start=1)}


def validate_layout() -> List[str]:
    """自检：接口不重复占用、设备名不重复、网段不重叠。返回问题列表（空=通过）。"""
    problems: List[str] = []
    topo = build_big_campus()

    names = [d["name"] for d in topo["devices"]]
    if len(set(names)) != len(names):
        problems.append("设备名重复")

    used = {}
    for link in topo["links"]:
        for side in ("a", "b"):
            key = (link[side]["device"], link[side]["interface"])
            if key in used:
                problems.append(f"接口被重复占用: {key[0]} 的 {key[1]}")
            used[key] = True
        for side in ("a", "b"):
            if link[side]["device"] not in names:
                problems.append(f"连线引用了不存在的设备: {link[side]['device']}")

    # 终端网段不得重叠
    nets = [d["ip_address"] for d in topo["devices"] if d.get("ip_address")]
    for i, a in enumerate(nets):
        for b in nets[i + 1:]:
            na = ipaddress.ip_interface(f"{a}/24").network
            nb = ipaddress.ip_interface(f"{b}/24").network
            if na == nb:
                problems.append(f"终端网段重叠: {a} 与 {b}")
    return problems


if __name__ == "__main__":
    import json
    topo = build_big_campus()
    print(f"设备 {len(topo['devices'])} 台，连线 {len(topo['links'])} 条")
    issues = validate_layout()
    print("自检:", "通过" if not issues else "\n".join(issues))
    print(json.dumps(topo, ensure_ascii=False, indent=2)[:400])
