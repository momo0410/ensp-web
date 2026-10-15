"""
command_complete.py - 命令上下文补全（实测/基础/目录三层候选）
==============================================================
给 CLI 输入框提供前缀补全候选，数据源三类：
- 设备实测：get_cached_probe(cache_key(target)) 的 views（? 树单层关键字）。
- 内置基础清单：BASE_COMMANDS（常用查询/操作命令，含用户视图 display 系列）。
- 内置目录：COMMAND_CATALOG 的 command_template 首 token（标准写法）。

候选结构：{"text", "desc", "source": "?" | "base" | "catalog", "view"}。
本模块为纯函数，便于离线单测；不新增采集、不触碰 eNSP。
"""

from typing import Any, Dict, List, Optional, Tuple

from .command_probe import cache_key, get_cached_probe
from .command_catalog import COMMAND_CATALOG

DEFAULT_VIEW = "system"
MAX_CANDIDATES = 100
CATALOG_DESC = "内置命令目录"
DEVICE_DESC = "设备实测关键字"

BASE_COMMANDS: Dict[str, List[Tuple[str, str]]] = {
    "user": [
        ("display version", "查看版本信息"),
        ("display current-configuration", "查看当前配置"),
        ("display ip interface brief", "查看接口 IP 简要信息"),
        ("display ip routing-table", "查看 IP 路由表"),
        ("display interface brief", "查看接口简要信息"),
        ("display vlan", "查看 VLAN 信息"),
        ("display ospf peer", "查看 OSPF 邻居"),
        ("display ospf routing", "查看 OSPF 路由"),
        ("display ospf brief", "查看 OSPF 概要"),
        ("display arp", "查看 ARP 表"),
        ("display mac-address", "查看 MAC 地址表"),
        ("display stp brief", "查看 STP 简要信息"),
        ("display vrrp brief", "查看 VRRP 简要信息"),
        ("display dhcp server pool", "查看 DHCP 地址池"),
        ("display bgp peer", "查看 BGP 邻居"),
        ("display ip pool", "查看 IP 地址池"),
        ("display acl all", "查看所有 ACL"),
        ("display nat session all", "查看 NAT 会话"),
        ("display saved-configuration", "查看已保存配置"),
        ("display clock", "查看系统时间"),
        ("display users", "查看登录用户"),
        ("display logbuffer", "查看日志缓冲区"),
        ("display device", "查看设备信息"),
        ("display elabel", "查看电子标签"),
        ("display power", "查看电源信息"),
        ("display fan", "查看风扇信息"),
        ("display temperature", "查看温度信息"),
        ("display cpu-usage", "查看 CPU 使用率"),
        ("display memory-usage", "查看内存使用率"),
        ("save", "保存配置"),
        ("reset", "清除统计/复位"),
        ("reboot", "重启设备"),
        ("ping", "连通性测试"),
        ("tracert", "路由追踪"),
        ("telnet", "Telnet 登录"),
        ("quit", "退出当前视图"),
        ("system-view", "进入系统视图"),
        ("undo", "取消配置"),
        ("terminal", "终端设置"),
    ],
    "system": [
        ("display this", "查看当前视图配置"),
        ("undo", "取消配置"),
        ("quit", "退出当前视图"),
        ("return", "返回用户视图"),
        ("save", "保存配置"),
    ],
    "interface": [
        ("display this", "查看当前接口配置"),
        ("undo", "取消配置"),
        ("quit", "退出当前视图"),
        ("return", "返回用户视图"),
    ],
}

BASE_DESC = "常用命令"


def _first_token(entry: Dict[str, Any]) -> str:
    template = entry.get("command_template") or []
    if not template:
        return ""
    return str(template[0]).strip().split()[0] if str(template[0]).strip() else ""


def _matches(text: str, prefix: str) -> bool:
    if not prefix:
        return True
    return text.lower().startswith(prefix.lower())


def base_candidates(prefix: str) -> List[Dict[str, str]]:
    """从内置基础清单生成候选；不按 view 过滤，保证 display 等查询命令始终可命中。"""
    out: List[Dict[str, str]] = []
    seen = set()
    for view, items in BASE_COMMANDS.items():
        for text, desc in items:
            if text in seen or not _matches(text, prefix):
                continue
            seen.add(text)
            out.append({
                "text": text,
                "desc": desc,
                "source": "base",
                "view": view,
            })
    out.sort(key=lambda c: c["text"])
    return out[:MAX_CANDIDATES]


def catalog_candidates(prefix: str, view: Optional[str] = None,
                       device_type: Optional[str] = None) -> List[Dict[str, str]]:
    """从内置命令目录生成候选（命令名取 command_template 首 token）。"""
    view = view or DEFAULT_VIEW
    out: List[Dict[str, str]] = []
    seen = set()
    for entry in COMMAND_CATALOG.values():
        if device_type:
            types = entry.get("device_types") or []
            if types and device_type not in types:
                continue
        text = _first_token(entry)
        if not text or not _matches(text, prefix):
            continue
        if text in seen:
            continue
        seen.add(text)
        out.append({
            "text": text,
            "desc": entry.get("name", ""),
            "source": "catalog",
            "view": view,
        })
    out.sort(key=lambda c: c["text"])
    return out[:MAX_CANDIDATES]


def filter_view_candidates(views: dict, prefix: str,
                           view: Optional[str] = None) -> List[Dict[str, str]]:
    """从设备实测 ? 树 views 生成候选。"""
    view = view or DEFAULT_VIEW
    if view == "all":
        tokens = set()
        for kw_set in (views or {}).values():
            tokens |= set(kw_set or [])
    else:
        tokens = set((views or {}).get(view, []) or [])
    out = [
        {"text": t, "desc": DEVICE_DESC, "source": "?", "view": view}
        for t in tokens
        if _matches(t, prefix)
    ]
    out.sort(key=lambda c: c["text"])
    return out[:MAX_CANDIDATES]


def complete_commands(target: dict, prefix: str, view: Optional[str] = None,
                      device_type: Optional[str] = None) -> Dict[str, Any]:
    """合并设备实测、基础清单与目录候选；实测优先，其次基础清单，最后目录。"""
    prefix = (prefix or "").strip()
    view = view or DEFAULT_VIEW
    base = base_candidates(prefix)
    catalog = catalog_candidates(prefix, view, device_type)

    def _merge(layers: List[List[Dict[str, str]]]) -> List[Dict[str, str]]:
        merged: List[Dict[str, str]] = []
        seen = set()
        for layer in layers:
            for c in layer:
                if c["text"] in seen:
                    continue
                seen.add(c["text"])
                merged.append(c)
        return merged[:MAX_CANDIDATES]

    cached = get_cached_probe(cache_key(target or {}))
    if cached:
        views = cached.get("views") or {}
        device_type = device_type or cached.get("device_type")
        measured = filter_view_candidates(views, prefix, view)
        return {
            "candidates": _merge([measured, base, catalog]),
            "from_cache": True,
            "hint": "候选来自设备实测 ? 树、常用命令清单与内置目录",
        }

    return {
        "candidates": _merge([base, catalog]),
        "from_cache": False,
        "hint": "缓存未命中，候选为常用命令清单与内置目录；可先触发 ? 探测以获取设备实测关键字",
    }
