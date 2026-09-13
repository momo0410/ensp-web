"""
命令输出解析：把 eNSP 设备的 display 输出转成结构化信息。

包含采集结果的结构化解析、LLDP 邻居解析与合并、
以及从完整配置中提取协议配置段。

本模块只做纯文本解析，不发起任何网络请求。
"""

import re
from typing import Dict, List, Optional

from app.config import logger


def _parse_device_info(commands: dict) -> dict:
    """
    从采集到的命令输出中解析结构化信息。
    所有解析失败的情况均返回空字符串，不抛出异常。
    """
    info = {
        "version": "",
        "model": "",
        "hostname": "",
        "interfaces": [],
        "vlans": [],
        "routes": [],
        "ospf_peers": [],
        "lldp_neighbors": [],
        "cpu_usage": "",
        "memory_usage": "",
        "protocols": [],
        "config_sections": {},
    }

    # --- display version ---
    _try_extract(commands, "display version", lambda out: (
        _update(info, "model", _regex_find(r"(AR\d+|S\d+|NE\d+|CE\d+|USG\d+|Router|Switch)\S*", out)),
        _update(info, "version", _regex_find(r"Version\s+([\d.]+\w*)", out)),
        _update(info, "hostname", _regex_find(r"<(\S+)>", out)),
    ))

    # --- display ip interface brief ---
    _try_extract(commands, "display ip interface brief", lambda out: (
        _update(info, "interfaces", _extract_table(out, r"^(\S+)\s+(\S+)\s+(\S+)\s+(\S+)", 4)),
    ))

    # --- display vlan ---
    _try_extract(commands, "display vlan", lambda out: (
        _update(info, "vlans", _extract_lines_containing(out, ["VLAN ID", "VLAN ID"])),
    ))

    # --- display ip routing-table ---
    _try_extract(commands, "display ip routing-table", lambda out: (
        _update(info, "routes", _extract_lines_containing(out, ["/", "Static", "RIP", "OSPF", "Direct"])),
    ))

    # --- display ospf peer ---
    _try_extract(commands, "display ospf peer", lambda out: (
        _update(info, "ospf_peers", _extract_table(out, r"^(\S+)\s+(\d+\.\d+\.\d+\.\d+)\s+(\d+)", 3)),
    ))

    # --- display lldp neighbor brief ---
    _try_extract(commands, "display lldp neighbor brief", lambda out: (
        _update(info, "lldp_neighbors", _parse_lldp_neighbors(out)),
    ))

    # --- display lldp neighbor (详细): 补充管理 IP ---
    _try_extract(commands, "display lldp neighbor", lambda out: (
        _merge_lldp_detail(info, _parse_lldp_neighbor_detail(out)),
    ))

    # --- display cpu-usage ---
    _try_extract(commands, "display cpu-usage", lambda out: (
        _update(info, "cpu_usage",
               _regex_find(r"CPU Usage\s*:\s*(\d+)%", out)
               or _regex_find(r"(\d+)%", out) + "%"),
    ))

    # --- display memory-usage ---
    _try_extract(commands, "display memory-usage", lambda out: (
        _update(info, "memory_usage", _regex_find(r"(\d+)%", out) + "%"),
    ))

    # --- display current-configuration: 解析协议配置段 ---
    _try_extract(commands, "display current-configuration", lambda out: (
        _parse_config_sections(info, out),
    ))

    return info


def _try_extract(commands: dict, cmd: str, fn) -> None:
    """安全地执行一组解析操作，忽略异常。"""
    cmd_data = commands.get(cmd, {})
    if cmd_data.get("success"):
        try:
            fn(cmd_data["output"])
        except Exception as e:
            logger.debug(f"解析 '{cmd}' 输出时异常: {e}")


def _update(info: dict, key: str, value) -> None:
    if value:
        info[key] = value


def _regex_find(pattern: str, text: str) -> str:
    """正则查找，返回第一个匹配组或空字符串。"""
    m = re.search(pattern, text)
    return m.group(1) if m else ""


def _extract_table(output: str, pattern: str, min_groups: int) -> List[str]:
    """从命令输出中提取表格行。"""
    lines = []
    for line in output.strip().split("\n"):
        m = re.match(pattern, line.strip())
        if m and len(m.groups()) >= min_groups:
            lines.append(line.strip())
    return lines


def _extract_lines_containing(output: str, keywords: List[str]) -> List[str]:
    """提取包含指定关键词的文本行。"""
    lines = []
    for line in output.strip().split("\n"):
        if any(kw in line for kw in keywords):
            lines.append(line.strip())
    return lines


def _parse_lldp_neighbors(output: str) -> List[dict]:
    """
    解析 display lldp neighbor brief 输出，提取邻居信息。
    兼容华为设备的各种表头格式（全称/缩写）。

    典型输出格式：
        Local Intf     Neighbor Dev     Neighbor Intf     Expiry
        GE0/0/0        SW1              GE0/0/1           120s
    """
    if "no neighbor" in output.lower() or "no lldp neighbor" in output.lower():
        return []
    if "not enabled" in output.lower():
        return []

    neighbors = []
    for line in output.strip().split("\n"):
        line = line.strip()
        # 跳过空行
        if not line:
            continue
        # 跳过分隔线
        if re.match(r'^[\-=]+$', line):
            continue
        lower = line.lower()
        # 跳过表头行：包含 intf/neighbor/expiry 等关键词
        if any(kw in lower for kw in ("local intf", "loc intf", "neigh dev", "neigh intf", "expiry")):
            continue
        # 按 2+ 空格分割
        parts = re.split(r"\s{2,}", line)
        if len(parts) >= 3:
            neighbors.append({
                "local_interface": parts[0].strip(),
                "remote_device": parts[1].strip(),
                "remote_interface": parts[2].strip() if len(parts) > 2 else "",
                "remote_mgmt_ip": "",
            })
    return neighbors


def _parse_lldp_neighbor_detail(output: str) -> List[dict]:
    """
    解析 display lldp neighbor 详细输出，提取邻居名称和管理 IP。

    典型输出格式：
        AR1's neighbor(s) information:
        -------------------------------------------------------
        Local Port : GigabitEthernet0/0/0
        Neighbor device name : AR2
        Neighbor port ID : GigabitEthernet0/0/0
        Management address : 192.168.100.12
    """
    if "not enabled" in output.lower():
        return []

    neighbors: List[dict] = []
    current: Optional[dict] = None

    for line in output.split("\n"):
        stripped = line.strip()

        # 常见详细格式: GigabitEthernet0/0/0 has 1 neighbors:
        m = re.match(r"^(\S+)\s+has\s+\d+\s+neighbors?\s*[:：]", stripped, re.IGNORECASE)
        if m:
            if current and current.get("remote_device"):
                neighbors.append(current)
            current = {
                "local_interface": m.group(1),
                "remote_device": "",
                "remote_interface": "",
                "remote_mgmt_ip": "",
            }
            continue

        # 新的本地接口块
        m = re.match(r"Local\s+(?:Port|Interface|Intf)\s*[:：]\s*(\S+)", stripped, re.IGNORECASE)
        if m:
            if current and current.get("remote_device"):
                neighbors.append(current)
            current = {"local_interface": m.group(1), "remote_device": "", "remote_interface": "", "remote_mgmt_ip": ""}
            continue

        if not current:
            continue

        # 邻居设备名称
        m = re.match(r"Neighbor(?:\s+device)?\s+name\s*[:：]\s*(\S+)", stripped, re.IGNORECASE)
        if m:
            current["remote_device"] = m.group(1)
            continue

        # S5700 等设备常见字段
        m = re.match(r"System\s+name\s*[:：]\s*(\S+)", stripped, re.IGNORECASE)
        if m:
            current["remote_device"] = m.group(1)
            continue

        # 邻居接口
        m = re.match(r"Neighbor\s+port\s+ID\s*[:：]\s*(\S+)", stripped, re.IGNORECASE)
        if m:
            current["remote_interface"] = m.group(1)
            continue

        m = re.match(r"Port\s+ID\s*[:：]\s*(\S+)", stripped, re.IGNORECASE)
        if m:
            current["remote_interface"] = m.group(1)
            continue

        # 管理 IP
        m = re.match(r"Management\s+address\s*[:：]\s*(\S+)", stripped, re.IGNORECASE)
        if m:
            current["remote_mgmt_ip"] = m.group(1)

    if current and current.get("remote_device"):
        neighbors.append(current)

    return neighbors


# 华为接口名缩写归一化映射
_INTF_ABBREV = {
    "ge": "gigabitethernet",
    "xge": "tengigabitethernet",
    "fe": "fastethernet",
    "eth": "ethernet",
    "me": "meth",
    "se": "serial",
    "loop": "loopback",
    "vlanif": "vlanif",
}


def _normalize_intf(name: str) -> str:
    """归一化华为接口名，如 GE0/0/0 -> gigabitethernet0/0/0"""
    lower = name.lower().strip()
    for abbrev, full in _INTF_ABBREV.items():
        if lower.startswith(abbrev) and (len(lower) == len(abbrev) or not lower[len(abbrev)].isalpha()):
            return full + lower[len(abbrev):]
    return lower


def _merge_lldp_detail(info: dict, detail_list: List[dict]) -> None:
    """
    将详细 LLDP 信息（管理 IP）合并到 info["lldp_neighbors"]。
    以 (归一化后的 local_interface, remote_device) 为键匹配。
    """
    if not detail_list:
        return

    # 构建查找表: (normalized_local_intf, remote_dev_lower) -> detail
    lookup = {}
    for d in detail_list:
        key = (_normalize_intf(d.get("local_interface", "")), d.get("remote_device", "").lower())
        lookup[key] = d

    neighbors = info.get("lldp_neighbors", [])
    for n in neighbors:
        key = (_normalize_intf(n.get("local_interface", "")), n.get("remote_device", "").lower())
        detail = lookup.get(key)
        if detail:
            n["remote_mgmt_ip"] = detail.get("remote_mgmt_ip", "")
            if not n.get("remote_interface") and detail.get("remote_interface"):
                n["remote_interface"] = detail["remote_interface"]

    # 如果 brief 解析到 0 条但详细解析到了，直接用详细结果
    if not neighbors and detail_list:
        info["lldp_neighbors"] = detail_list


# ---------------------------------------------------------------------------
# 配置段解析（从 display current-configuration 提取协议配置）
# ---------------------------------------------------------------------------

# 每项规则会检查完整配置块内的所有行。这样不仅能获取协议进程块，也能获取
# interface 子视图内的 OSPF、NAT、DHCP、IS-IS 等相关配置。
_CONFIG_SECTION_RULES = [
    ("ospf",   "OSPF",    (r"^ospf(?:\s|$)",)),
    ("rip",    "RIP",     (r"^rip(?:\s|$)",)),
    ("bgp",    "BGP",     (r"^bgp(?:\s|$)",)),
    ("isis",   "IS-IS",   (r"^isis(?:\s|$)",)),
    ("static", "静态路由", (r"^ip route-static(?:\s|$)",)),
    ("nat",    "NAT",     (r"^nat(?:\s|$)",)),
    ("dhcp",   "DHCP",    (r"^dhcp(?:\s|$)", r"^ip pool(?:\s|$)")),
    ("acl",    "ACL",     (r"^acl(?:\s|$)",)),
    ("ntp",    "NTP",     (r"^ntp-service(?:\s|$)",)),
    ("snmp",   "SNMP",    (r"^snmp-agent(?:\s|$)",)),
    ("ipsec",  "IPSec",   (r"^ipsec(?:\s|$)", r"^ike(?:\s|$)")),
]


def _split_current_config_blocks(config_output: str) -> List[List[str]]:
    """按 VRP 的 # 分隔符切分配置，同时保留原始缩进和父视图。"""
    blocks: List[List[str]] = []
    current: List[str] = []

    def flush() -> None:
        if current and any(
            line.strip()
            and not re.match(r"^\[V\d", line.strip(), re.IGNORECASE)
            and line.strip().lower() != "return"
            for line in current
        ):
            blocks.append(list(current))
        current.clear()

    for raw_line in config_output.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped == "#":
            flush()
            continue
        if not stripped or stripped.lower() == "return":
            continue
        if re.match(r"^\[V\d", stripped, re.IGNORECASE):
            continue
        current.append(line)

    flush()
    return blocks


def _config_block_matches(block: List[str], patterns: tuple) -> bool:
    return any(
        re.match(pattern, line.strip(), re.IGNORECASE)
        for line in block
        for pattern in patterns
    )


def _parse_config_sections(info: dict, config_output: str) -> None:
    """
    从 display current-configuration 输出中解析各协议/功能配置段。
    结果写入 info['protocols'] 和 info['config_sections']。
    """
    if not config_output:
        return

    protocols: List[str] = []
    config_sections: Dict[str, dict] = {}
    blocks = _split_current_config_blocks(config_output)

    for section_id, display_name, patterns in _CONFIG_SECTION_RULES:
        section_blocks = [
            block for block in blocks
            if _config_block_matches(block, patterns)
        ]
        if section_blocks:
            protocols.append(section_id)
            config_sections[section_id] = {
                "name": display_name,
                "configured": True,
                "config": "\n#\n".join("\n".join(block) for block in section_blocks),
            }

    info["protocols"] = protocols
    info["config_sections"] = config_sections
