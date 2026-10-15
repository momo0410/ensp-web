"""
eNSP 拓扑文件生成器 —— 由「拓扑描述」生成可直接被 eNSP 打开并自动启动的 .topo 项目。

核心规范（经逆向 eNSP_Client.exe 与 eNSP_VBoxServer.exe 验证）：
1. .topo 文件虽然 XML 头声明 `encoding="UNICODE"`，但物理文件必须为 **无 BOM 的 GBK/ANSI 编码（CRLF 换行）**。
2. 每台网络设备必须拥有合法的华为 OUI `system_mac`（路由器/防火墙/无线 `00-E0-FC-XX-XX-XX`，交换机 `4C-1F-CC-XX-XX-XX`）与大写 UUID。
3. AP 设备（如 AP6050）必须在 `settings` 属性中包含 `" -apMac <MAC> -apSN <20位SN>"`。
4. 接口连线的 `srcIndex`/`tarIndex` 是 `<slot>` 内所有 `<interface>` 的全局从 0 累加索引；
   交换机（S5700/S3700）与无线控制器（AC6005）的逻辑端口号从 1 开始（如 GE0/0/1），路由器/防火墙/AP 从 0 开始（如 GE0/0/0）。
5. 路由器若带预置 `startup_config`，其 `<uuid>/statemach.efs` 必须为 2263 字节引导结构（含 `flash:/vrpcfg.zip` 指针）；
   若走 Console 端口免 SSH 动态配置，无需生成 `<uuid>` 子目录，eNSP 会自动用干净镜像启动并在 `127.0.0.1:<com_port>` 开启 Console 监听。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
import zipfile
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 设备型号 → 板卡接口布局（与 eNSP_Client.exe 0x0041f200..0x00422740 严格一致）
# ---------------------------------------------------------------------------
DEVICE_MODELS: dict[str, dict[str, Any]] = {
    "AR1220": {
        "kind": "router",
        "base_no": 0,
        "oui": "00-E0-FC",
        "interfaces": [("Ethernet", "GE", 2), ("Ethernet", "Ethernet", 8)],
        "slots": [],
        "serial_ifaces": ["Serial"],
    },
    "AR2220": {
        "kind": "router",
        "base_no": 0,
        "oui": "00-E0-FC",
        "interfaces": [("Ethernet", "GE", 1), ("Ethernet", "GE", 2)],
        "slots": [],
        "serial_ifaces": ["Serial"],
    },
    "Router": {
        "kind": "router",
        "base_no": 0,
        "oui": "00-E0-FC",
        "interfaces": [
            ("Ethernet", "Ethernet", 2),
            ("Ethernet", "GE", 4),
            ("Serial", "Serial", 4),
        ],
        "slots": [],
        "serial_ifaces": ["Serial"],
    },
    "S5700": {
        "kind": "l3_switch",
        "base_no": 1,
        "oui": "4C-1F-CC",
        "interfaces": [("Ethernet", "GE", 24)],
        "slots": [],
        "serial_ifaces": [],
    },
    "S3700": {
        "kind": "switch",
        "base_no": 1,
        "oui": "4C-1F-CC",
        "interfaces": [("Ethernet", "Ethernet", 22), ("Ethernet", "GE", 2)],
        "slots": [],
        "serial_ifaces": [],
    },
    "USG5500": {
        "kind": "firewall",
        "base_no": 0,
        "oui": "00-E0-FC",
        "interfaces": [("Ethernet", "GE", 9)],
        "slots": [],
        "serial_ifaces": [],
    },
    "USG6000V": {
        "kind": "firewall",
        "base_no": 0,
        "oui": "00-E0-FC",
        "interfaces": [("Ethernet", "GE", 4)],
        "slots": [],
        "serial_ifaces": [],
    },
    "AC6005": {
        "kind": "wlan_ac",
        "base_no": 1,
        "oui": "00-E0-FC",
        "interfaces": [("Ethernet", "GE", 8)],
        "slots": [],
        "serial_ifaces": [],
    },
    "AP6050": {
        "kind": "wlan_ap",
        "base_no": 0,
        "oui": "00-E0-FC",
        "interfaces": [("Ethernet", "GE", 2)],
        "slots": [],
        "serial_ifaces": [],
    },
    # ---- 路由器（可开机镜像族：vboxserver/devices/AR）----
    "AR201": {
        "kind": "router",
        "base_no": 0,
        "oui": "00-E0-FC",
        "interfaces": [("Ethernet", "Ethernet", 8), ("Ethernet", "Ethernet", 1)],
        "slots": [],
        "serial_ifaces": ["Serial"],
    },
    "AR2240": {
        "kind": "router",
        "base_no": 0,
        "oui": "00-E0-FC",
        "interfaces": [("Ethernet", "GE", 3)],
        "slots": [],
        "serial_ifaces": ["Serial"],
    },
    "AR3260": {
        "kind": "router",
        "base_no": 0,
        "oui": "00-E0-FC",
        "interfaces": [("Ethernet", "GE", 3)],
        "slots": [],
        "serial_ifaces": ["Serial"],
    },
    # ---- 交换机 ----
    "S5700-28C-HI": {
        "kind": "l3_switch",
        "base_no": 1,
        "oui": "4C-1F-CC",
        "interfaces": [("Ethernet", "GE", 24)],
        "slots": [],
        "serial_ifaces": [],
    },
    # ---- 无线 ----
    "AC6605": {
        "kind": "wlan_ac",
        "base_no": 1,
        "oui": "00-E0-FC",
        "interfaces": [("Ethernet", "GE", 8)],
        "slots": [],
        "serial_ifaces": [],
    },
    # ---- 终端设备（无 CLI，通过 settings 配置 IP/网关）----
    "PC": {
        "kind": "terminal",
        "base_no": 0,
        "oui": "54-89-98",
        "interfaces": [("Ethernet", "GE", 1)],
        "slots": [],
        "serial_ifaces": [],
        "settings_prefix": "-simpc",
    },
    "Laptop": {
        "kind": "terminal",
        "base_no": 0,
        "oui": "54-89-98",
        "interfaces": [("Ethernet", "Ethernet", 1)],
        "slots": [],
        "serial_ifaces": [],
        "settings_prefix": "-simpc",
    },
    "MCS": {
        "kind": "terminal",
        "base_no": 0,
        "oui": "54-89-98",
        "interfaces": [("Ethernet", "Ethernet", 1)],
        "slots": [],
        "serial_ifaces": [],
        "settings_prefix": "-mcs",
    },
    "STA": {
        "kind": "terminal",
        "base_no": 0,
        "oui": "54-89-98",
        "interfaces": [],
        "slots": [],
        "serial_ifaces": [],
        "settings_prefix": "-simsta",
    },
}

DEFAULT_COM_PORT_BASE = 2000


class TopologyError(ValueError):
    """拓扑描述不合法。"""


def _generate_mac(oui: str, seed_str: str, index: int) -> str:
    if not oui:
        return ""
    digest = hashlib.md5(f"{seed_str}-{index}".encode("utf-8")).digest()
    b3 = digest[0]
    b4 = digest[1]
    # 末字节对齐到 0x10 步长，避免多端口 MAC 派生进位冲突
    b5 = (index * 16 + 1) & 0xFF
    return f"{oui}-{b3:02X}-{b4:02X}-{b5:02X}"


def _generate_ap_sn(seed_str: str, index: int) -> str:
    digest = hashlib.md5(f"apsn-{seed_str}-{index}".encode("utf-8")).hexdigest().upper()
    return f"210235448310{digest[:8]}"


def _terminal_settings(spec: dict, dev: dict, sys_mac: str) -> str:
    """
    终端设备（PC/Laptop/MCS/STA）的 settings 字符串。

    格式取自 eNSP 官方示例拓扑：以空格分隔的 `-key value` 序列。
    STA 是无线终端，字段名与有线终端不同（-simsta_*）。
    """
    prefix = spec.get("settings_prefix", "-simpc")
    ip = dev.get("ip_address", "0.0.0.0")
    mask = dev.get("netmask", "0.0.0.0")
    gateway = dev.get("gateway", "0.0.0.0")
    parts = [
        f"{prefix}_ip {ip}",
        f"{prefix}_mask {mask}",
        f"{prefix}_gateway {gateway}",
        f"{prefix}_mac {sys_mac}",
    ]
    if prefix == "-simsta":
        parts.append(f"{prefix}_check 0")
    return " " + " ".join(parts) + " "


def build_statemach_efs() -> bytes:
    """构建 eNSP 路由器标准 2263 字节 statemach.efs（包含 flash:/vrpcfg.zip 指针）。"""
    buf = bytearray(2263)
    buf[4:6] = b"\x47\x06"
    buf[72 : 72 + 17] = b"flash:/vrpcfg.zip"
    buf[911 : 911 + 17] = b"flash:/vrpcfg.zip"
    return bytes(buf)


# ---------------------------------------------------------------------------
# 接口编号映射
# ---------------------------------------------------------------------------
def interface_index_map(model: str) -> dict[str, list[int]]:
    """返回 {逻辑接口名: [全局索引]}，支持 GE0/0/x 与 GigabitEthernet0/0/x 别名。"""
    spec = DEVICE_MODELS.get(model)
    if spec is None:
        raise TopologyError(
            f"未知设备型号 {model!r}；可用型号：{', '.join(sorted(DEVICE_MODELS))}"
        )
    base_no = spec.get("base_no", 0)
    mapping: dict[str, list[int]] = {}
    per_type: dict[str, int] = {}
    global_idx = 0
    for _sztype, iface_name, count in spec["interfaces"]:
        if count <= 0:
            continue
        start = per_type.get(iface_name, base_no)
        for n in range(count):
            port_no = start + n
            mapping[f"{iface_name}0/0/{port_no}"] = [global_idx]
            if iface_name == "GE":
                mapping[f"GigabitEthernet0/0/{port_no}"] = [global_idx]
            global_idx += 1
        per_type[iface_name] = start + count
    return mapping


def resolve_interface(model: str, iface: str) -> int:
    """把 'GE0/0/0' / 'GigabitEthernet0/0/1' 解析成 eNSP 全局接口索引。"""
    mapping = interface_index_map(model)
    key = iface.replace(" ", "")
    if key in mapping:
        return mapping[key][0]
    available = ", ".join(sorted(k for k in mapping if not k.startswith("Gigabit")))
    raise TopologyError(f"{model} 上没有接口 {iface!r}；可用：{available}")


def _is_serial(model: str, iface: str) -> bool:
    spec = DEVICE_MODELS.get(model, {})
    head = iface.replace(" ", "")[:6].lower()
    return any(head.startswith(s.lower()) for s in spec.get("serial_ifaces", []))


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def validate_topology(topo: dict) -> None:
    if not isinstance(topo, dict):
        raise TopologyError("拓扑描述必须是 JSON 对象")
    devices = topo.get("devices")
    if not isinstance(devices, list) or not devices:
        raise TopologyError("拓扑描述缺少非空的 devices 数组")

    names: set[str] = set()
    for i, d in enumerate(devices):
        if not isinstance(d, dict):
            raise TopologyError(f"devices[{i}] 必须是对象")
        name = d.get("name")
        if not name:
            raise TopologyError(f"devices[{i}] 缺少 name")
        if name in names:
            raise TopologyError(f"设备名重复：{name!r}")
        names.add(name)
        model = d.get("model")
        if model not in DEVICE_MODELS:
            raise TopologyError(
                f"设备 {name!r} 的 model={model!r} 不支持；"
                f"可用：{', '.join(sorted(DEVICE_MODELS))}"
            )
        cfg = d.get("startup_config", [])
        if not isinstance(cfg, list) or not all(isinstance(x, str) for x in cfg):
            raise TopologyError(f"设备 {name!r} 的 startup_config 必须是字符串数组")

    name_set = {d["name"] for d in devices if isinstance(d, dict) and "name" in d}
    by_name = {d["name"]: d for d in devices if isinstance(d, dict) and "name" in d}

    for i, link in enumerate(topo.get("links", [])):
        if not isinstance(link, dict):
            raise TopologyError(f"links[{i}] 必须是对象")
        for end in ("a", "b"):
            ep = link.get(end)
            if not isinstance(ep, dict) or "device" not in ep or "interface" not in ep:
                raise TopologyError(f"links[{i}].{end} 需要 device 与 interface")
            if ep["device"] not in name_set:
                raise TopologyError(
                    f"links[{i}].{end}.device={ep['device']!r} 不在 devices 列表中"
                )
        src_model = by_name[link["a"]["device"]]["model"]
        dst_model = by_name[link["b"]["device"]]["model"]
        sa = _is_serial(src_model, link["a"]["interface"])
        sb = _is_serial(dst_model, link["b"]["interface"])
        if sa != sb:
            raise TopologyError(
                f"links[{i}] 两端接口类型不一致："
                f"{link['a']['device']}/{link['a']['interface']} 与 "
                f"{link['b']['device']}/{link['b']['interface']}（串口须对串口）"
            )
        resolve_interface(src_model, link["a"]["interface"])
        resolve_interface(dst_model, link["b"]["interface"])


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------
def _build_config_text(model: str, sysname: str, extra: list[str]) -> str:
    """组装路由器 startup 配置文本（UTF-8 + CRLF，带版本头）。"""
    lines: list[str] = ["", "[V200R003C00]", "#", f" sysname {sysname}", "#"]
    for line in extra:
        lines.append(line.rstrip("\r\n"))
    body = "\r\n".join(lines) + "\r\n"
    body += "#\r\nreturn\r\n"
    return body


def generate(
    topo: dict,
    out_dir: Path,
    name: str | None = None,
    com_port_base: int = DEFAULT_COM_PORT_BASE,
    configs: "dict[str, str] | None" = None,
) -> Path:
    """
    只生成 .topo（设备 + 连线）。设备配置目录由 eNSP 自己维护。

    configs 参数保留以兼容调用方，当前不再用于生成 <UUID>/ 配置包 ——
    配置改为在设备开机后经 Console 下发，由 eNSP 在保存时落盘。
    """
    """生成 .topo 项目，返回 .topo 文件路径。"""
    validate_topology(topo)
    name = name or topo.get("name") or "topology"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 保持 JSON 中声明的设备顺序，便于分配确定性的 com_port
    ordered = list(topo["devices"])
    # 设备自带 id（项目持久化的 UUID）时复用，保证 <UUID>/ 配置目录跨次生成稳定
    ids: dict[str, str] = {
        d["name"]: (d.get("id") or str(uuid.uuid4())).upper() for d in ordered
    }
    by_name = {d["name"]: d for d in ordered}

    com_port = com_port_base
    dev_xml: list[str] = []
    for idx, d in enumerate(ordered):
        model = d["model"]
        spec = DEVICE_MODELS[model]
        did = ids[d["name"]]
        cx, cy = float(d.get("cx", 300.0)), float(d.get("cy", 300.0))
        edit_left = int(d.get("edit_left", round(cx + 27)))
        edit_top = int(d.get("edit_top", round(cy + 54)))

        if spec["kind"] == "terminal":
            # 终端设备不占 Console 端口，也不参与能力识别
            port_attr = 'com_port="0"'
            sys_mac = d.get("system_mac") or _generate_mac(spec["oui"], d["name"], idx + 1)
            settings = d.get("settings", "") or _terminal_settings(spec, d, sys_mac)
        else:
            assigned_port = d.get("com_port", com_port)
            port_attr = f'com_port="{assigned_port}"'
            com_port = max(com_port, int(assigned_port) + 1)
            sys_mac = d.get("system_mac") or _generate_mac(spec["oui"], d["name"], idx + 1)
            settings = d.get("settings", "")
            if spec["kind"] == "wlan_ap" and "-apMac" not in settings:
                ap_sn = d.get("ap_sn") or _generate_ap_sn(d["name"], idx + 1)
                settings = f" -apMac {sys_mac} -apSN {ap_sn}"

        ifaces = "".join(
            f'                <interface sztype="{sz}" interfacename="{nm}" count="{c}" />\n'
            for sz, nm, c in spec["interfaces"]
        )
        extra_slots = "".join(
            f'            <slot isMainBoard="0" id="{sid}" type="{stype}" />\n'
            for sid, stype in spec["slots"]
        )
        slot = (
            f'            <slot number="slot17" isMainBoard="1">\n'
            f"{ifaces}"
            f'            </slot>\n'
            f"{extra_slots}".rstrip("\n")
        )

        dev_xml.append(
            f'        <dev id="{did}" name="{d["name"]}" poe="0" model="{model}" '
            f'settings="{settings}" system_mac="{sys_mac}" {port_attr} bootmode="0" '
            f'cx="{cx:.6f}" cy="{cy:.6f}" edit_left="{edit_left}" edit_top="{edit_top}">\n'
            f"{slot}\n"
            f"        </dev>\n"
        )

    line_xml: list[str] = []
    for link in topo.get("links", []):
        a, b = link["a"], link["b"]
        src_dev, dst_dev = by_name[a["device"]], by_name[b["device"]]
        src_model, dst_model = src_dev["model"], dst_dev["model"]
        src_idx = resolve_interface(src_model, a["interface"])
        dst_idx = resolve_interface(dst_model, b["interface"])
        line_name = "Serial" if _is_serial(src_model, a["interface"]) else "Copper"
        src_x = float(src_dev.get("cx", 300.0)) + 27.0
        src_y = float(src_dev.get("cy", 300.0)) + 27.0
        dst_x = float(dst_dev.get("cx", 300.0)) + 27.0
        dst_y = float(dst_dev.get("cy", 300.0)) + 27.0
        line_xml.append(
            f'        <line srcDeviceID="{ids[a["device"]]}" destDeviceID="{ids[b["device"]]}">\n'
            f'            <interfacePair lineName="{line_name}" '
            f'srcIndex="{src_idx}" srcBoundRectIsMoved="0" '
            f'srcBoundRect_X="{src_x:.6f}" srcBoundRect_Y="{src_y:.6f}" '
            f'srcOffset_X="0.000000" srcOffset_Y="0.000000" '
            f'tarIndex="{dst_idx}" tarBoundRectIsMoved="0" '
            f'tarBoundRect_X="{dst_x:.6f}" tarBoundRect_Y="{dst_y:.6f}" '
            f'tarOffset_X="0.000000" tarOffset_Y="0.000000" />\n'
            f"        </line>\n"
        )

    xml = (
        '<?xml version="1.0" encoding="UNICODE" ?>\n'
        '<topo version="1.2.00.510">\n'
        "    <devices>\n"
        f"{''.join(dev_xml)}"
        "    </devices>\n"
        "    <lines>\n"
        f"{''.join(line_xml)}"
        "    </lines>\n"
        "    <shapes />\n"
        "    <txttips />\n"
        "</topo>\n"
    )

    topo_path = out_dir / f"{name}.topo"
    # 关键：eNSP 使用单字节 TiXmlDocument，必须保存为无 BOM 的 GBK/ANSI + CRLF
    xml_crlf = xml.replace("\r\n", "\n").replace("\n", "\r\n")
    topo_path.write_bytes(xml_crlf.encode("gbk"))

    # 设备配置目录（<UUID>/statemach.efs + vrpcfg.zip）刻意【不由本项目生成】。
    # 实测结论：eNSP 保存拓扑时会用自己当前的格式重写 .topo（版本号随版本变化），
    # 并在设备配置真正变化时写 <dev id>/ 目录 —— 交给 eNSP 生成，格式永远匹配，
    # 也不必维护逆向出来的 2263 字节 statemach.efs 模板。
    # 本项目只负责：在默认目录生成 .topo、通过 Console 下发配置。
    # 若拓扑描述里显式给了 startup_config，仍按老方式预置（供离线/测试场景使用）。
    for d in ordered:
        startup_cfg = list(d.get("startup_config", []))
        if not startup_cfg:
            continue
        if DEVICE_MODELS[d["model"]]["kind"] != "router":
            continue
        did = ids[d["name"]]
        dev_dir = out_dir / did
        dev_dir.mkdir(parents=True, exist_ok=True)
        (dev_dir / "statemach.efs").write_bytes(build_statemach_efs())
        cfg = _build_config_text(d["model"], d["name"], startup_cfg)
        with zipfile.ZipFile(dev_dir / "vrpcfg.zip", "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("vrpcfg.cfg", cfg.encode("utf-8"))

    return topo_path


# ---------------------------------------------------------------------------
# 内置综合案例拓扑（覆盖路由器、交换机、防火墙、无线 AC + AP）
# ---------------------------------------------------------------------------
COMPREHENSIVE_DEMO = {
    "name": "comprehensive_campus",
    "devices": [
        {
            "name": "R1",
            "model": "Router",
            "com_port": 2000,
            "cx": 150.0,
            "cy": 220.0,
        },
        {
            "name": "R2",
            "model": "Router",
            "com_port": 2001,
            "cx": 360.0,
            "cy": 220.0,
        },
        {
            "name": "SW1",
            "model": "S5700",
            "com_port": 2002,
            "cx": 580.0,
            "cy": 220.0,
        },
        {
            "name": "AC1",
            "model": "Router",
            "com_port": 2003,
            "cx": 800.0,
            "cy": 120.0,
        },
        {
            "name": "SW2",
            "model": "S3700",
            "com_port": 2004,
            "cx": 800.0,
            "cy": 340.0,
        },
        {
            "name": "PC1",
            "model": "PC",
            "cx": 1040.0,
            "cy": 260.0,
            "ip_address": "192.168.20.10",
            "netmask": "255.255.255.0",
            "gateway": "192.168.20.1",
        },
        {
            "name": "PC2",
            "model": "PC",
            "cx": 1040.0,
            "cy": 420.0,
            "ip_address": "192.168.20.11",
            "netmask": "255.255.255.0",
            "gateway": "192.168.20.1",
        },
    ],
    "links": [
        {
            "a": {"device": "R1", "interface": "GE0/0/0"},
            "b": {"device": "R2", "interface": "GE0/0/1"},
        },
        {
            "a": {"device": "R2", "interface": "GE0/0/2"},
            "b": {"device": "SW1", "interface": "GE0/0/1"},
        },
        {
            "a": {"device": "SW1", "interface": "GE0/0/2"},
            "b": {"device": "AC1", "interface": "GE0/0/0"},
        },
        {
            "a": {"device": "SW1", "interface": "GE0/0/3"},
            "b": {"device": "SW2", "interface": "GE0/0/1"},
        },
        {
            "a": {"device": "SW2", "interface": "Ethernet0/0/1"},
            "b": {"device": "PC1", "interface": "GE0/0/0"},
        },
        {
            "a": {"device": "SW2", "interface": "Ethernet0/0/2"},
            "b": {"device": "PC2", "interface": "GE0/0/0"},
        },
    ],
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成 eNSP .topo 拓扑文件")
    ap.add_argument("spec", nargs="?", help="拓扑描述 JSON 文件路径")
    ap.add_argument("-o", "--out", default=".", help="输出目录（默认当前目录）")
    ap.add_argument("-n", "--name", help="拓扑名（默认取 JSON 的 name 或文件名）")
    ap.add_argument("--demo", action="store_true", help="生成综合园区网示例拓扑（R1+FW1+SW1+AC1+AP1）")
    ap.add_argument("--list-models", action="store_true", help="列出支持的设备型号")
    args = ap.parse_args(argv)

    if args.list_models:
        for m, spec in sorted(DEVICE_MODELS.items()):
            base = spec.get("base_no", 0)
            ifaces = ", ".join(
                f"{nm}0/0/{base}..{base + count - 1}"
                for _sz, nm, count in spec["interfaces"]
                if count > 0
            )
            print(f"{m:10s} ({spec['kind']}): {ifaces}")
        return 0

    if args.demo:
        topo, name = COMPREHENSIVE_DEMO, args.name or COMPREHENSIVE_DEMO["name"]
    elif args.spec:
        path = Path(args.spec)
        if not path.exists():
            print(f"找不到拓扑描述文件：{path}", file=sys.stderr)
            return 2
        topo = json.loads(path.read_text(encoding="utf-8"))
        name = args.name or topo.get("name") or path.stem
    else:
        ap.print_help()
        return 1

    try:
        out = generate(topo, Path(args.out), name=name)
    except TopologyError as e:
        print(f"拓扑描述有误：{e}", file=sys.stderr)
        return 2
    print(f"已生成：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
