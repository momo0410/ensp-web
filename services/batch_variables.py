"""
batch_variables.py - 批量变量序列展开（需求⑧）
================================================
把「同一模板 + 变量序列 → 一次下发 N 台」表达出来，例如 OSPF 区域 0 中 5 台设备
IP 依次 192.168.1.1 ~ 192.168.1.5，不必手填 5 行变量表。

序列 spec 形如：

    {
        "base":    {"router_id": "192.168.1.{i}", "ospf_network": "10.0.{i}.0"},
        "start":   1,          # 序号起始值，默认 1
        "step":    1,          # 序号步长，默认 1
        "width":   0,          # 补零宽度，0 表示不补零（{i:02d} 显式补零优先）
        "per_device_overrides": {          # 可选：个别设备偏离序列
            "127.0.0.1:2003": {"router_id": "1.1.1.1"}
        }
    }

模板字符串支持的占位符（Python str.format 语义）：

    {i}        序号本身
    {i:02d}    补零到 2 位（格式符任意，如 {i:03d}）
    {ip_last}  设备 IP 的最后一段（无 IP 时为空串）
    {ip}       设备 IP 原值

base 之外的变量（模板里出现但 base 未声明的）**不做序列展开**，由调用方
（前端手填的 device_vars / smart-defaults）提供，本模块只在缺省时补空串，
并把这些名字放进 missing_vars 让上层提示用户。

本模块为纯函数，不触碰设备、不触碰 eNSP，便于离线单测。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

MAX_DEVICES = 200          # 单次展开的设备数上限，防止误操作一次改几百台
MAX_WIDTH = 10             # 补零宽度上限

_VAR_PATTERN = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_PLACEHOLDER_PATTERN = re.compile(r"\{([^{}]*)\}")


class SeriesError(ValueError):
    """序列 spec 不合法。"""


def _normalize_ip(device: Any) -> str:
    """设备可以是 str（IP）或 dict（设备对象），统一取出 IP 标识。"""
    if isinstance(device, dict):
        return str(device.get("ip") or device.get("name") or "")
    return str(device or "")


def _last_octet(ip: str) -> str:
    """取 IP 的最后一段；端口形式（127.0.0.1:2000）取冒号前的最后一段。"""
    if not ip:
        return ""
    host = ip.rsplit(":", 1)[0] if ":" in ip and ip.count(".") == 3 else ip
    parts = host.split(".")
    return parts[-1] if parts else ""


def _render_series_value(template: str, index: int, ip: str) -> str:
    """
    把一个序列模板字符串渲染成具体值。

    逐段扫描 {...} 占位符并替换；模板里不含占位符时原样返回，
    这样 base 里既可以是 "192.168.1.{i}" 也可以是固定值 "0.0.0.0"。
    """
    if "{" not in template:
        return template

    ctx = {
        "i": index,
        "ip": ip or "",
        "ip_last": _last_octet(ip),
    }

    def _sub(match: "re.Match[str]") -> str:
        expr = match.group(1).strip()
        if not expr:
            return match.group(0)
        # 只认 i / ip / ip_last 三个名字，可带格式符（如 i:03d）；其余原样留给用户
        name, _, spec = expr.partition(":")
        name = name.split("!", 1)[0]
        if name not in ctx:
            return match.group(0)
        try:
            return format(ctx[name], spec) if spec else str(ctx[name])
        except (ValueError, TypeError):
            # 格式符非法（如 {i:x}）时退回无格式值
            return str(ctx[name])

    return _PLACEHOLDER_PATTERN.sub(_sub, template)


def _validate_spec(spec: Dict[str, Any]) -> Tuple[Dict[str, str], int, int, int, Dict[str, Dict[str, str]]]:
    """校验并归一化序列 spec，返回 (base, start, step, width, overrides)。"""
    if not isinstance(spec, dict):
        raise SeriesError("series 必须是对象")

    base = spec.get("base") or {}
    if not isinstance(base, dict):
        raise SeriesError("series.base 必须是对象（变量名 → 模板字符串）")
    clean_base: Dict[str, str] = {}
    for key, value in base.items():
        if not isinstance(key, str) or not key.strip():
            raise SeriesError(f"series.base 的变量名不合法: {key!r}")
        if not isinstance(value, (str, int, float)):
            raise SeriesError(f"变量 {key} 的模板必须是字符串或数字，得到 {type(value).__name__}")
        clean_base[key.strip()] = str(value)
    if not clean_base:
        raise SeriesError("series.base 不能为空（至少要有一个按序号变化的变量）")

    def _int_field(name: str, default: int, minimum: int, maximum: int) -> int:
        raw = spec.get(name, default)
        if raw is None:
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise SeriesError(f"series.{name} 必须是整数，得到 {raw!r}") from None
        if not (minimum <= value <= maximum):
            raise SeriesError(f"series.{name} 超出范围 [{minimum}, {maximum}]: {value}")
        return value

    start = _int_field("start", 1, 1, 100000)
    step = _int_field("step", 1, 1, 1000)
    width = _int_field("width", 0, 0, MAX_WIDTH)

    overrides_raw = spec.get("per_device_overrides") or {}
    if not isinstance(overrides_raw, dict):
        raise SeriesError("series.per_device_overrides 必须是对象")
    overrides: Dict[str, Dict[str, str]] = {}
    for ip, values in overrides_raw.items():
        if not isinstance(values, dict):
            raise SeriesError(f"设备 {ip} 的覆盖值必须是对象")
        overrides[str(ip)] = {str(k): str(v) for k, v in values.items()}

    return clean_base, start, step, width, overrides


def expand_variable_series(
    spec: Dict[str, Any],
    devices: List[Any],
) -> Dict[str, Any]:
    """
    把序列 spec 展开成逐台设备的变量表。

    Args:
        spec: 序列定义（见模块 docstring）
        devices: 目标设备列表，元素为 IP 字符串或含 ip 字段的 dict；
                 顺序即序号分配顺序（第 1 台拿 start，第 2 台拿 start+step ...）

    Returns:
        {
            "ok": True,
            "device_vars": {ip: {var: value}},   # 直接喂给 render_preview 的 device_vars
            "series_values": [{ip, index, vars}],  # 供前端展示"第几台 → 哪些值"
            "resolved": [变量名...],              # 实际参与展开的变量名
        }
    """
    base, start, step, width, overrides = _validate_spec(spec)

    if not devices:
        return {"ok": True, "device_vars": {}, "series_values": [], "resolved": sorted(base)}
    if len(devices) > MAX_DEVICES:
        raise SeriesError(f"一次最多展开 {MAX_DEVICES} 台设备，当前 {len(devices)} 台")

    ips = [_normalize_ip(d) for d in devices]
    if len(set(ips)) != len(ips):
        dup = sorted({ip for ip in ips if ips.count(ip) > 1})
        raise SeriesError(f"目标设备存在重复 IP: {', '.join(dup)}")

    device_vars: Dict[str, Dict[str, str]] = {}
    series_values: List[dict] = []

    for offset, ip in enumerate(ips):
        index = start + offset * step
        rendered: Dict[str, str] = {}
        for name, template in base.items():
            value = _render_series_value(template, index, ip)
            # width 是"整型序号补零"的简写：{i} 且模板里没写格式符时生效
            if "{i}" in template and ":" not in template.split("}", 1)[0] and width > 0:
                value = value.replace(str(index), str(index).zfill(width), 1) \
                    if str(index) in value else value
            rendered[name] = value
        if ip in overrides:
            rendered.update(overrides[ip])
        device_vars[ip] = rendered
        series_values.append({"ip": ip, "index": index, "vars": dict(rendered)})

    return {
        "ok": True,
        "device_vars": device_vars,
        "series_values": series_values,
        "resolved": sorted(base),
    }


def merge_series_into_device_vars(
    expanded: Dict[str, Dict[str, str]],
    manual_device_vars: Optional[Dict[str, Dict[str, Any]]],
) -> Dict[str, Dict[str, Any]]:
    """
    把序列展开结果与手填的 device_vars 合并。

    优先级：手填（用户在界面上逐台改的）> 序列展开。
    这样"先按序列生成，再单独微调某一台"是可行的。
    """
    merged: Dict[str, Dict[str, Any]] = {ip: dict(v) for ip, v in (expanded or {}).items()}
    for ip, values in (manual_device_vars or {}).items():
        if ip not in merged:
            merged[ip] = {}
        merged[ip].update(values or {})
    return merged


def series_vars_in_templates(command_template: Any) -> List[str]:
    """模板里出现的 {{var}} 名字（用于前端提示哪些变量可序列化）。"""
    if isinstance(command_template, str):
        lines: List[str] = command_template.split("\n")
    elif isinstance(command_template, list):
        lines = [str(x) for x in command_template]
    else:
        return []
    names = set()
    for line in lines:
        names.update(_VAR_PATTERN.findall(line))
    return sorted(names)
