"""
path_trace.py - 逻辑路径追踪（抓包的零依赖替代）
==============================================
真抓包要装 npcap 内核驱动，机房部署门槛高。本模块用 eNSP 设备**本来就能查**
的几张表，回答教学场景真正关心的问题：「这个包从哪走到哪、在哪断的」：

    display arp all              → 某一跳上，目的 IP 对应哪台设备的哪个接口
    display mac-address          → 二层下一跳是哪个物理设备
    display ip routing-table     → 这一跳选路选给了谁（含默认路由与协议优先级）
    display ip interface brief   → 这一跳自己有哪些接口 IP（判断包是否已到达）

追踪结果是一串「跳」，每跳带证据来源；任一环推导不下去，就标记为
**断点**（breakpoint）并给出可能原因，交给 AI 助教分析。

设计约束：
- **零连接**：所有输入都是已采集的文本（collection_results / 临时执行结果），
  本模块不主动连设备，便于离线单测。
- **只读推导**：绝不臆造。推不出下一跳就如实说断，不猜。
- 真实抓包仍留作后续（npcap 可行性验证后再定），本模块是长期可用的轻量替代。
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# VRP 输出解析
#
# VRP 这几张表都是「空白分隔的定宽列」，且列内容本身不含空格，
# 所以统一按空白切分再按位置/形状取字段，比长正则稳得多
# （不同镜像版本的列宽、对齐、空列处理并不一致）。
#
# display ip routing-table（Proto 在前，Flags 列可能为空）：
#   Proto   Destination/Mask    Pre  Cost  Flags  NextHop          Interface
#   Direct  10.0.13.0/24        0    0     D      10.0.13.1        GigabitEthernet0/0/0
#   OSPF    172.16.0.0/24       10   2           10.0.13.2        GigabitEthernet0/0/0
# display ip interface brief：
#   *down: GigabitEthernet0/0/0        手动关闭   down    down
#   GigabitEthernet0/0/1               192.168.1.1/24   up    up
# display arp all：
#   IP ADDRESS      MAC ADDRESS     EXPIRE(M) TYPE  INTERFACE
#   192.168.1.2     00e0-fc12-3456  20        D-0   GE0/0/1
# display mac-address：
#   MAC Address    VLAN  Learned-From  Type      AGE
#   00e0-fc12-3458  1     GE0/0/2       dynamic   20
# ---------------------------------------------------------------------------
_CIDR_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+/\d+$")
_IPV4_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+$")
_MAC_RE = re.compile(r"^[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}$")
# 路由来源优先级（数值越小越优先），用于同前缀多条路由时挑"实际生效"的那条
_PROTO_PREFERENCE = {
    "Direct": 0, "Static": 60, "OSPF": 10, "OSPF-ASE": 150, "ISIS": 15,
    "RIP": 100, "BGP": 255, "IBGP": 255, "EBGP": 255,
}


class TraceError(ValueError):
    """路径追踪的输入不合法。"""


def _split_row(line: str) -> List[str]:
    return [tok for tok in (line or "").split() if tok]


def parse_routing_table(output: str) -> List[dict]:
    """
    解析 display ip routing-table。

    Flags 列可能为空（如 OSPF 路由），所以不按固定列号取，
    而是按「第二列是 CIDR」定位行，再用 IPv4 形状认出下一跳。
    """
    routes = []
    for line in (output or "").splitlines():
        tokens = _split_row(line)
        # 真实 VRP 输出是「Destination/Mask  Proto  Pre  Cost  Flags  NextHop  Interface」，
        #  Destination 在第一列。靠 CIDR 形状定位它，兼容个别镜像把 Proto 提前的排列。
        if len(tokens) < 5:
            continue
        # 两种列序都支持：
        #   A. Destination/Mask  Proto  Pre  Cost  Flags  NextHop  Interface（VRP 真实格式）
        #   B. Proto  Destination/Mask  Pre  Cost  Flags  NextHop  Interface（少数镜像）
        if _CIDR_RE.match(tokens[0]):
            dest, proto, rest = tokens[0], tokens[1], tokens[2:]
        elif _CIDR_RE.match(tokens[1]):
            dest, proto, rest = tokens[1], tokens[0], tokens[2:]
        else:
            continue

        # rest = [Pre, Cost, (Flags), NextHop, Interface]，Flags 列可能为空。
        # 前两个是整数（Pre/Cost），用 IPv4 形状认出 NextHop，其余即接口名。
        try:
            pre = int(rest[0])
            cost = int(rest[1])
        except (ValueError, IndexError):
            continue
        nexthop, iface = "", ""
        for idx, tok in enumerate(rest[2:], start=2):
            if _IPV4_RE.match(tok):
                nexthop = tok
                remainder = rest[idx + 1:]
                iface = remainder[0] if remainder else ""
                break
        try:
            network = ipaddress.ip_network(dest, strict=False)
        except ValueError:
            continue
        routes.append({
            "destination": dest,
            "network": network,
            "proto": proto,
            "preference": pre,
            "cost": cost,
            "nexthop": nexthop,
            "interface": iface,
        })
    # 每个目的网络只保留 preference 最小的一条（VRP 的真实选路行为）
    best: Dict[str, dict] = {}
    for r in routes:
        key = str(r["network"])
        cur = best.get(key)
        if cur is None or r["preference"] < cur["preference"]:
            best[key] = r
    return list(best.values())


_IFACE_NAME_RE = re.compile(r"^\*?[A-Za-z][A-Za-z0-9/\-]*$")
_PROTO_WORDS = {"up", "down", "up(s)", "down(s)", "*down", "up(d)", "down(d)"}


def parse_interfaces(output: str) -> List[dict]:
    """
    解析 display ip interface brief。

    真实输出前有若干图例行（`*down: administratively down`、
    `The number of interface that is UP ...`）和表头，
    靠「首列是合法接口名 + 末两列是 up/down」把数据行认出来。
    IP 列带掩码（192.168.1.1/24），这里只留 IP 本体，
    掩码由 _annotate_masks() 从路由表 Direct 条目补齐。
    """
    out = []
    for line in (output or "").splitlines():
        tokens = _split_row(line)
        if len(tokens) < 3:
            continue
        # 管理关闭的接口行首是 "*down:" 与接口名连在一起，先剥掉再认接口名
        admin_down = False
        if tokens[0].startswith("*down:"):
            head, _, tail = tokens[0].partition(":")
            if tail:
                tokens = [tail] + tokens[1:]
                admin_down = True
            else:
                tokens = [head] + tokens[1:]
                admin_down = True
        if not _IFACE_NAME_RE.match(tokens[0]):
            continue
        line_proto, link_proto = tokens[-2], tokens[-1]
        if line_proto not in _PROTO_WORDS or link_proto not in _PROTO_WORDS:
            continue
        ip = None
        if _CIDR_RE.match(tokens[1]):
            ip = tokens[1].split("/", 1)[0]
        elif _IPV4_RE.match(tokens[1]):
            ip = tokens[1]
        out.append({
            "interface": tokens[0],
            "ip": ip,
            "admin_down": admin_down or line_proto == "*down",
            "line_protocol": line_proto,
            "link_protocol": link_proto,
            "up": line_proto.startswith("up") and link_proto.startswith("up"),
        })
    return out


def _annotate_masks(interfaces: List[dict], routes: List[dict]) -> None:
    """用路由表的直连条目给接口补上掩码（brief 本身没有掩码列）。"""
    direct = [r for r in routes if r["proto"] == "Direct"]
    for itf in interfaces:
        ip = itf.get("ip")
        if not ip:
            continue
        for route in direct:
            if ipaddress.ip_address(ip) in route["network"]:
                itf["netmask"] = str(route["network"].netmask)
                itf["subnet"] = str(route["network"])
                break


def parse_arp(output: str) -> List[dict]:
    """解析 display arp all（首列是 IPv4、次列是 MAC 的行才算）。"""
    out = []
    for line in (output or "").splitlines():
        tokens = _split_row(line)
        if len(tokens) < 2 or not _IPV4_RE.match(tokens[0]) or not _MAC_RE.match(tokens[1]):
            continue
        out.append({
            "ip": tokens[0],
            "mac": tokens[1],
            "expire": tokens[2] if len(tokens) > 2 else "",
            "type": tokens[3] if len(tokens) > 3 else "",
            "interface": tokens[4] if len(tokens) > 4 else "",
        })
    return out


def parse_mac_table(output: str) -> List[dict]:
    """解析 display mac-address（首列是 MAC 的行才算）。"""
    out = []
    for line in (output or "").splitlines():
        tokens = _split_row(line)
        if not tokens or not _MAC_RE.match(tokens[0]):
            continue
        out.append({
            "mac": tokens[0],
            "vlan": tokens[1] if len(tokens) > 1 else "",
            "learned_from": tokens[2] if len(tokens) > 2 else "",
            "type": tokens[3] if len(tokens) > 3 else "",
            "age": tokens[4] if len(tokens) > 4 else "",
        })
    return out


# ---------------------------------------------------------------------------
# 选路
# ---------------------------------------------------------------------------
def longest_prefix_match(dest_ip: str, routes: List[dict]) -> Optional[dict]:
    """对 dest_ip 做最长前缀匹配，返回命中的路由（无命中返回 None）。"""
    try:
        addr = ipaddress.ip_address(dest_ip)
    except ValueError:
        return None
    best: Optional[dict] = None
    for r in routes:
        if addr in r["network"]:
            if best is None or r["network"].prefixlen > best["network"].prefixlen:
                best = r
    return best


def _next_hop_for(route: dict, device_name: str) -> dict:
    """从一条路由推导出"下一跳是谁"。"""
    nexthop = route.get("nexthop") or ""
    iface = route.get("interface") or ""
    # 直连路由（Direct）的下一跳就是接口所在网段里的对端，设备名未知
    return {
        "device": device_name,
        "via": "nexthop" if nexthop and nexthop != "0.0.0.0" else "direct",
        "nexthop": nexthop,
        "interface": iface,
        "proto": route.get("proto"),
        "preference": route.get("preference"),
        "destination": route.get("destination"),
    }


# ---------------------------------------------------------------------------
# 断点归因
# ---------------------------------------------------------------------------
def diagnose_break(hop: dict, interfaces: List[dict], arps: List[dict]) -> List[str]:
    """给一个断点跳列出可能原因（供 AI 助教与界面展示）。"""
    reasons: List[str] = []
    iface_name = hop.get("interface")
    if iface_name:
        for itf in interfaces:
            if itf["interface"] == iface_name:
                if itf["admin_down"]:
                    reasons.append(f"接口 {iface_name} 被手动关闭（undo shutdown）")
                elif not itf["up"]:
                    reasons.append(f"接口 {iface_name} 线路/链路协议未 up（物理连线或对端未开机）")
                break
        else:
            reasons.append(f"设备上没有接口 {iface_name}（型号不符或接口名写错）")

    nexthop = hop.get("nexthop")
    if nexthop and nexthop != "0.0.0.0":
        if not any(a["ip"] == nexthop for a in arps):
            reasons.append(f"ARP 表里没有 {nexthop} 的条目（对端未配置 IP 或链路不通，无法解析 MAC）")

    if hop.get("via") == "no_route":
        reasons.append("没有匹配任何路由条目（缺少静态/动态路由，或目的网段不在路由表中）")

    if not reasons:
        reasons.append("该跳无法继续推导：缺少对应设备的采集数据（先做一次全量采集）")
    return reasons


# ---------------------------------------------------------------------------
# 从采集结果构造设备快照
# ---------------------------------------------------------------------------
# 采集结果里命令原文的键名（domain/collect.py 的采集字典）
_KEY_IFACE = "ip_interface_brief"
_KEY_ROUTE = "routing_table"
_KEY_ARP = "arp_all"
_KEY_MAC = "mac_address"


def _pick(commands: dict, *keys: str) -> str:
    """按候选键名依次取第一条命令原文（不同镜像/采集版本的键名略有差异）。

    键名归一化时**空格和下划线都要去掉**，否则 "display arp all"
    与 "displayarpall" 永远匹配不上。
    """
    def _norm(s: str) -> str:
        return s.replace(" ", "").replace("_", "").lower()

    for key in keys:
        for actual, value in commands.items():
            if _norm(actual) != _norm(key):
                continue
            if isinstance(value, str):
                return value
            if isinstance(value, dict):
                for sub in ("output", "raw", "text"):
                    if isinstance(value.get(sub), str):
                        return value[sub]
    return ""


def build_snapshots(devices: List[dict], collection_results: Dict[str, dict]) -> List[dict]:
    """
    把设备表与采集结果组装成 trace_path 吃的快照列表。

    采集结果的结构由 domain/collect.py 决定；这里只做"尽量认出表在哪"，
    认不出的表留空字符串（该设备会在追踪中被标为数据不足）。
    """
    snapshots = []
    for dev in devices:
        ip = dev.get("ip") or ""
        result = (collection_results or {}).get(ip) or {}
        commands = result.get("commands") if isinstance(result, dict) else None
        if not isinstance(commands, dict):
            commands = result if isinstance(result, dict) else {}
        snapshots.append({
            "device": dev.get("name") or ip,
            "ip": ip,
            "interfaces": _pick(commands, "display ip interface brief", "ip_interface_brief"),
            "routing": _pick(commands, "display ip routing-table", "routing_table"),
            "arp": _pick(commands, "display arp all", "arp_all"),
            "mac": _pick(commands, "display mac-address", "mac_address"),
            "has_data": bool(commands),
        })
    return snapshots


def format_trace_for_ai(result: Dict[str, Any]) -> str:
    """把追踪结果渲染成一段给 AI 助教的纯文本（含断点与可能原因）。"""
    if not result.get("success"):
        bp = result.get("breakpoint") or {}
        lines = [f"路径追踪 {result.get('source')} → {result.get('destination')} 未完成。"]
        if result.get("hops"):
            lines.append("已确认的跳：" + " → ".join(h["device"] for h in result["hops"]))
        if bp.get("device"):
            lines.append(f"中断在设备 {bp['device']}。")
        for reason in bp.get("reasons") or []:
            lines.append(f"- {reason}")
        return "\n".join(lines)

    lines = [f"路径追踪 {result['source']} → {result['destination']}："]
    for idx, hop in enumerate(result.get("hops") or [], start=1):
        lines.append(
            f"{idx}. {hop['device']} 经 {hop.get('interface') or '?'} 发往 "
            f"{hop.get('nexthop') or '直连'}（{hop.get('proto') or '?'}）"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def trace_path(
    source_ip: str,
    dest_ip: str,
    snapshots: List[dict],
    max_hops: int = 10,
) -> Dict[str, Any]:
    """
    推导 source_ip → dest_ip 的转发路径。

    Args:
        source_ip: 起始地址（用于在 snapshots 里定位起始设备）
        dest_ip:  目的地址
        snapshots: 每台设备的只读快照，形如
            [{"device": "R1", "ip": "127.0.0.1:2000",
              "interfaces": "display ip interface brief 原文",
              "routing":   "display ip routing-table 原文",
              "arp":       "display arp all 原文",
              "mac":       "display mac-address 原文"}, ...]
        max_hops: 最多追踪多少跳（防环/防呆）

    Returns:
        {success, source, destination, hops, breakpoint, summary}
        hops 每项含 device / via / nexthop / interface / proto / evidence；
        断点时 breakpoint = {device, reasons, hop_index}。
    """
    try:
        src = ipaddress.ip_address(source_ip.strip())
        dst = ipaddress.ip_address(dest_ip.strip())
    except (ValueError, AttributeError) as e:
        raise TraceError(f"IP 地址不合法: {e}") from None

    parsed = []
    for snap in snapshots:
        interfaces = parse_interfaces(snap.get("interfaces", ""))
        routes = parse_routing_table(snap.get("routing", ""))
        _annotate_masks(interfaces, routes)
        parsed.append({
            "device": snap.get("device") or snap.get("name") or "?",
            "interfaces": interfaces,
            "routes": routes,
            "arps": parse_arp(snap.get("arp", "")),
            "macs": parse_mac_table(snap.get("mac", "")),
        })

    if not parsed:
        return {"success": False, "source": str(src), "destination": str(dst),
                "hops": [], "breakpoint": None,
                "summary": "没有任何设备快照，无法追踪"}

    # 起始设备：接口 IP 与 source 同网段的那台（取第一个命中的）
    start = None
    for node in parsed:
        for itf in node["interfaces"]:
            if itf.get("ip") and _same_subnet(str(src), itf["ip"], itf.get("netmask")):
                start = node
                break
        if start:
            break

    hops: List[dict] = []
    dest_target = str(dst)
    visited: set = set()

    if start is None:
        return {
            "success": False, "source": str(src), "destination": str(dst),
            "hops": [],
            "breakpoint": {
                "device": None, "hop_index": 0,
                "reasons": [f"找不到接口 IP 与 {src} 同网段的设备（先确认源地址归属并做全量采集）"],
            },
            "summary": f"无法定位源地址 {src} 所属的设备",
        }

    node = start
    for index in range(max_hops):
        key = (node["device"], dest_target)
        if key in visited:
            return {
                "success": False, "source": str(src), "destination": str(dst),
                "hops": hops,
                "breakpoint": {"device": node["device"], "hop_index": index,
                               "reasons": [f"检测到环路：{node['device']} 再次为 {dest_target} 选路"]},
                "summary": "路径出现环路，追踪中止",
            }
        visited.add(key)

        route = longest_prefix_match(dest_target, node["routes"])
        if route is None:
            hop = {"device": node["device"], "via": "no_route", "interface": "",
                   "nexthop": "", "proto": None, "evidence": "routing-table"}
            hops.append(hop)
            return {
                "success": False, "source": str(src), "destination": str(dst),
                "hops": hops,
                "breakpoint": {
                    "device": node["device"], "hop_index": index,
                    "reasons": diagnose_break(hop, node["interfaces"], node["arps"]),
                },
                "summary": f"{node['device']} 上没有到 {dest_target} 的路由，路径在此中断",
            }

        hop = _next_hop_for(route, node["device"])
        hop["evidence"] = f"{route['proto']} 路由 {route['destination']} → {hop['interface']}"
        hops.append(hop)

        # 直连路由意味着目的就在本设备某个网段里 —— 到达终点
        if route["proto"] == "Direct":
            return _finish(hops, str(src), str(dst), True)

        nexthop = route["nexthop"]
        if not nexthop or nexthop == "0.0.0.0":
            return {
                "success": False, "source": str(src), "destination": str(dst),
                "hops": hops,
                "breakpoint": {
                    "device": node["device"], "hop_index": index,
                    "reasons": [f"路由 {route['destination']} 没有可用下一跳（接口未 up 或配置不完整）"],
                },
                "summary": f"{node['device']} 到 {dst} 的路由缺少下一跳",
            }

        # 找拥有 nexthop 的那台设备，继续为**目的地址**选路
        nxt = _find_node_by_ip(parsed, nexthop)
        if nxt is None:
            return {
                "success": False, "source": str(src), "destination": str(dst),
                "hops": hops,
                "breakpoint": {
                    "device": node["device"], "hop_index": index,
                    "reasons": diagnose_break(hop, node["interfaces"], node["arps"]),
                },
                "summary": f"找不到接口 IP 为 {nexthop} 的设备，无法继续追踪下一跳",
            }
        node = nxt

    return {
        "success": False, "source": str(src), "destination": str(dst),
        "hops": hops,
        "breakpoint": {"device": node["device"], "hop_index": len(hops),
                       "reasons": [f"超过 {max_hops} 跳仍未到达，可能存在环路或路径过长"]},
        "summary": f"超过 {max_hops} 跳仍未到达 {dst}",
    }


def _finish(hops, source, destination, ok):
    return {
        "success": ok, "source": source, "destination": destination,
        "hops": hops, "breakpoint": None,
        "summary": " → ".join(h["device"] for h in hops) + f"（{len(hops)} 跳到达 {destination}）",
    }


def _mask_of(cidr: str) -> Optional[str]:
    try:
        return str(ipaddress.ip_network(cidr, strict=False).netmask)
    except ValueError:
        return None


def _same_subnet(a: str, b: str, netmask: Optional[str]) -> bool:
    """判断两个地址是否同网段；netmask 为空时按两边各自掩码的较粗者比较。"""
    try:
        addr_a = ipaddress.ip_address(a)
        addr_b = ipaddress.ip_address(b)
    except ValueError:
        return False
    if netmask:
        try:
            mask = ipaddress.ip_address(netmask)
        except ValueError:
            return False
        return (int(addr_a) & int(mask)) == (int(addr_b) & int(mask))
    return addr_a == addr_b


def _find_node_by_ip(parsed: List[dict], ip: str) -> Optional[dict]:
    for node in parsed:
        for itf in node["interfaces"]:
            if itf.get("ip") == ip:
                return node
    return None
