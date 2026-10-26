"""
ai_plan.py - AI 结构化 plan（需求⑦）
====================================
AI 要能"快速配置设备/搭建拓扑"，但项目安全红线是
**备份 → 预览 → 确认 → 验证**，确认闸门不可省略。

因此 AI 不允许输出自由文本直接下发：它必须吐出一个受约束的 JSON plan，
后端校验通过后转成现有下发链路的入参（仍是人点确认）。

plan 分两类：

1. 配置下发（config）
   {
     "kind": "config",
     "targets": ["127.0.0.1:2000", ...],   # 设备 IP
     "commands": ["ospf 1 router-id 1.1.1.1", " area 0", ...],
     "verify": ["display ospf peer"],
     "save": false
   }
   可带 series（变量序列），转成 free-deploy 的 series 字段。

2. 拓扑搭建（topology）
   {
     "kind": "topology",
     "name": "ai_topo",
     "devices": [{"name": "R1", "model": "Router", "x": 100, "y": 100}, ...],
     "links": [{"a": {"device": "R1", "interface": "GE0/0/0"},
                "b": {"device": "R2", "interface": "GE0/0/0"}}, ...],
     "auto_start": false
   }

校验失败一律拒绝并给出可读原因（回给 AI 让它改），绝不静默丢弃字段。
本模块为纯函数，不连设备、不调用模型，便于离线单测。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from tools.gen_topology import DEVICE_MODELS

MAX_TARGETS = 50
MAX_COMMANDS = 200
MAX_DEVICES = 60
MAX_LINKS = 200

_IFACE_RE = re.compile(r"^[A-Za-z]{1,12}\d+(/\d+){0,3}$")

PLAN_KINDS = ("config", "topology")

# 让模型产出受约束 plan 的系统提示词。定义在服务层（而非路由层），
# 因为「路由产出 plan」「Agent 工具产出 plan」「一键刷配置产出 plan」
# 三处必须共用同一份口径，services 不允许反向依赖 routes。
PLAN_SYSTEM = """你是一个网络配置助手。根据用户要求和设备上下文，输出一份**JSON plan**，
且只能输出一个 ```json 代码块，不要有多余文字。

两种 plan：
1. 配置下发：{"kind":"config","targets":["设备IP",...],"commands":["ospf 1 router-id 1.1.1.1"," area 0"],"verify":["display ospf brief"],"save":false}
   需要批量递增时加 series：{"kind":"config","targets":[...],"commands":["ospf 1 router-id {{router_id}}"],"series":{"base":{"router_id":"192.168.1.{i}"},"start":1,"step":1}}
2. 拓扑搭建：{"kind":"topology","name":"拓扑名","devices":[{"name":"R1","model":"Router"}],"links":[{"a":{"device":"R1","interface":"GE0/0/0"},"b":{"device":"R2","interface":"GE0/0/0"}}],"auto_start":false}

硬性要求：
- targets 必须是设备表中真实存在的 IP；型号必须在支持列表内。
- 命令逐条写成 VRP（华为）语法，不要 Cisco 语法。
- 不要输出自然语言解释，不要输出第二个 JSON。
"""


class PlanError(ValueError):
    """AI 输出的 plan 不合法。"""


# ---------------------------------------------------------------------------
# 提取：从模型回复里抠出 JSON
# ---------------------------------------------------------------------------
_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


def _iter_json_objects(text: str) -> List[str]:
    """从文本里切出所有**完整**的 JSON 对象（含嵌套）。

    不能用 `\\{.*?\\}` 非贪婪匹配：plan 里常带嵌套对象（如 series），
    非贪婪会在第一个 `}` 就截断，得到残缺 JSON。
    这里按花括号配平扫描，并跳过字符串内的括号。
    """
    out: List[str] = []
    depth = 0
    start = -1
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    out.append(text[start:i + 1])
                    start = -1
    return out


def _merge_config_plans(plans: List[dict]) -> Optional[dict]:
    """把多个 config plan 合并成一个。

    真机实测：让 AI「给 R1 和 R2 配 OSPF」时，它常按设备**分别输出两个
    JSON 块**（每台一个 targets），而不是一个含多目标的 plan。
    只取第一个会导致「只刷了一台」这种静默漏配 —— 必须合并。

    若各目标的命令**不相同**（如 R1 用 1.1.1.1、R2 用 2.2.2.2），
    合并成单一 commands 会让两台刷成同一份配置 —— 那是错的。
    此时保留 `per_target_commands`，由下发层逐台取用。
    """
    configs = [p for p in plans if isinstance(p, dict) and p.get("kind") == "config"]
    if not configs:
        return None

    targets: List[str] = []
    verify: List[str] = []
    save = False
    series = None
    per_target: Dict[str, List[str]] = {}
    all_commands: List[str] = []
    distinct: List[tuple] = []

    for p in configs:
        cmds = p.get("commands") or []
        if isinstance(cmds, str):
            cmds = [c for c in cmds.split("\n") if c.strip()]
        cmds = [c for c in cmds if isinstance(c, str) and c.strip()]
        key = tuple(cmds)
        if key and key not in distinct:
            distinct.append(key)

        p_targets = [t for t in (p.get("targets") or []) if t]
        for t in p_targets:
            if t not in targets:
                targets.append(t)
            per_target[t] = list(cmds)

        for c in cmds:
            if c not in all_commands:
                all_commands.append(c)
        for v in (p.get("verify") or []):
            if v not in verify:
                verify.append(v)
        save = save or bool(p.get("save"))
        if series is None:
            series = p.get("series")

    merged = {
        "kind": "config",
        "targets": targets,
        "commands": all_commands,
        "verify": verify,
        "save": save,
    }
    # 命令确实逐台不同时，明确标注，避免下发层误用统一模板
    if len(distinct) > 1 and len(per_target) > 1:
        merged["per_target_commands"] = per_target
    if series:
        merged["series"] = series
    return merged


def extract_plan(reply: str) -> Tuple[Optional[dict], Optional[str]]:
    """
    从模型回复文本中提取 plan JSON。

    依次尝试：```json 代码块 → 全文里的完整 JSON 对象。
    若模型输出了**多个** config plan（按设备分开写），合并成一个。
    返回 (plan, error)；解析不出来时 plan 为 None。
    """
    text = (reply or "").strip()
    if not text:
        return None, "模型没有返回任何内容"

    candidates: List[str] = []
    for m in _FENCE_RE.finditer(text):
        candidates.append(m.group(1))
    # 按花括号配平再扫一遍，覆盖嵌套对象与非代码块输出
    candidates.extend(_iter_json_objects(text))

    parsed: List[dict] = []
    seen: set = set()
    for raw in candidates:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        # 两条提取路径会重复捞到同一个代码块（fence 扫一遍、配平再扫一遍），
        # 不去重会让「单个 plan」被误判成「多个 plan」而走合并分支，
        # 凭空补出 targets/commands/verify/save 四个空字段。
        key = json.dumps(data, sort_keys=True, ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        parsed.append(data)

    if not parsed:
        return None, "模型回复里找不到合法的 JSON plan（应输出 ```json {...} ``` 代码块）"

    # 单目标/拓扑 plan：直接返回第一个
    if len(parsed) == 1:
        return parsed[0], None

    # 多个 plan：config 类型合并，避免「只刷了第一台」
    merged = _merge_config_plans(parsed)
    if merged is not None:
        return merged, None
    return parsed[0], None


# ---------------------------------------------------------------------------
# 校验：配置下发 plan
# ---------------------------------------------------------------------------
def _validate_commands(plan: dict) -> List[str]:
    commands = plan.get("commands")
    if isinstance(commands, str):
        commands = [c for c in commands.split("\n") if c.strip()]
    if not isinstance(commands, list) or not commands:
        raise PlanError("plan.commands 必须是非空数组（或换行分隔的字符串）")
    cleaned = []
    for cmd in commands:
        if not isinstance(cmd, str):
            raise PlanError(f"命令必须是字符串，得到 {type(cmd).__name__}")
        line = cmd.strip()
        if line:
            cleaned.append(line)
    if not cleaned:
        raise PlanError("plan.commands 里没有有效命令（全是空行）")
    if len(cleaned) > MAX_COMMANDS:
        raise PlanError(f"命令条数超过上限 {MAX_COMMANDS}（当前 {len(cleaned)}）")
    return cleaned


def _validate_targets(plan: dict, known_ips: Optional[List[str]]) -> List[str]:
    targets = plan.get("targets")
    if not isinstance(targets, list) or not targets:
        raise PlanError("plan.targets 必须是非空数组（设备 IP 列表）")
    out = []
    for t in targets:
        if not isinstance(t, str) or not t.strip():
            raise PlanError(f"目标设备必须是 IP 字符串，得到 {t!r}")
        out.append(t.strip())
    if len(out) > MAX_TARGETS:
        raise PlanError(f"目标设备数超过上限 {MAX_TARGETS}（当前 {len(out)}）")
    if known_ips is not None:
        unknown = [t for t in out if t not in known_ips]
        if unknown:
            raise PlanError(f"这些设备不在设备表中: {', '.join(unknown)}")
    return out


def validate_config_plan(plan: dict, known_ips: Optional[List[str]] = None) -> Dict[str, Any]:
    """校验配置下发 plan，返回可直接喂给 free-deploy 的请求体。"""
    targets = _validate_targets(plan, known_ips)
    commands = _validate_commands(plan)

    verify = plan.get("verify") or []
    if not isinstance(verify, list):
        raise PlanError("plan.verify 必须是数组")
    verify = [str(v).strip() for v in verify if str(v).strip()][:20]

    save = plan.get("save", False)
    if not isinstance(save, bool):
        raise PlanError("plan.save 必须是布尔值")

    series = plan.get("series")
    if series is not None:
        if not isinstance(series, dict) or not isinstance(series.get("base"), dict):
            raise PlanError("plan.series 必须是对象且含 base（变量序列定义）")
        series = {
            "base": {str(k): str(v) for k, v in series["base"].items()},
            "start": int(series.get("start", 1)),
            "step": int(series.get("step", 1)),
            "width": int(series.get("width", 0)),
            "per_device_overrides": series.get("per_device_overrides") or {},
        }

    per_target = plan.get("per_target_commands")
    if per_target is not None:
        if not isinstance(per_target, dict):
            raise PlanError("plan.per_target_commands 必须是 {设备IP: [命令]} 对象")
        cleaned_per: Dict[str, List[str]] = {}
        for ip, cmds in per_target.items():
            if isinstance(cmds, str):
                cmds = [c for c in cmds.split("\n") if c.strip()]
            if not isinstance(cmds, list) or not cmds:
                raise PlanError(f"per_target_commands[{ip}] 必须是非空命令数组")
            cleaned_per[str(ip)] = [
                str(c).strip() for c in cmds if str(c).strip()]
        per_target = cleaned_per

    result = {
        "task_name": str(plan.get("task_name") or "AI 生成的配置任务"),
        "target_devices": targets,
        "command_template": commands,
        "verify_commands": verify,
        "save_after_success": save,
        "series": series,
        # 供界面展示"AI 打算做什么"
        "rationale": str(plan.get("rationale") or "")[:500],
    }
    if per_target:
        result["per_target_commands"] = per_target
    return result


# ---------------------------------------------------------------------------
# 校验：拓扑搭建 plan
# ---------------------------------------------------------------------------
def validate_topology_plan(plan: dict) -> Dict[str, Any]:
    """校验拓扑 plan，返回可喂给 /api/ensp/topo/apply 的请求体。"""
    devices = plan.get("devices")
    if not isinstance(devices, list) or not devices:
        raise PlanError("plan.devices 必须是非空数组")
    if len(devices) > MAX_DEVICES:
        raise PlanError(f"设备数超过上限 {MAX_DEVICES}（当前 {len(devices)}）")

    seen_names = set()
    out_devices = []
    for idx, dev in enumerate(devices):
        if not isinstance(dev, dict):
            raise PlanError(f"第 {idx + 1} 个设备必须是对象")
        name = str(dev.get("name") or "").strip()
        if not name:
            raise PlanError(f"第 {idx + 1} 个设备缺少 name")
        if name in seen_names:
            raise PlanError(f"设备名重复: {name}")
        seen_names.add(name)

        model = str(dev.get("model") or "Router").strip()
        if model not in DEVICE_MODELS:
            raise PlanError(
                f"设备 {name} 的型号 {model} 不受支持；可用: {', '.join(sorted(DEVICE_MODELS)[:12])} ..."
            )

        entry: Dict[str, Any] = {"name": name, "model": model}
        for key in ("x", "y"):
            if key in dev and dev[key] is not None:
                try:
                    entry[key] = float(dev[key])
                except (TypeError, ValueError):
                    raise PlanError(f"设备 {name} 的 {key} 必须是数字") from None
        # 终端设备的网络参数（PC/Laptop 才有意义）
        for key in ("ip_address", "netmask", "gateway"):
            if dev.get(key):
                entry[key] = str(dev[key])
        out_devices.append(entry)

    links = plan.get("links") or []
    if not isinstance(links, list):
        raise PlanError("plan.links 必须是数组")
    if len(links) > MAX_LINKS:
        raise PlanError(f"连线数超过上限 {MAX_LINKS}")

    used_ifaces = set()
    out_links = []
    for idx, link in enumerate(links):
        if not isinstance(link, dict):
            raise PlanError(f"第 {idx + 1} 条连线必须是对象")
        ends = []
        for side in ("a", "b"):
            endpoint = link.get(side)
            if not isinstance(endpoint, dict):
                raise PlanError(f"第 {idx + 1} 条连线缺少 {side} 端")
            dev_name = str(endpoint.get("device") or "").strip()
            iface = str(endpoint.get("interface") or "").strip()
            if dev_name not in seen_names:
                raise PlanError(f"第 {idx + 1} 条连线的 {side} 端设备 {dev_name!r} 不在设备列表里")
            if not _IFACE_RE.match(iface):
                raise PlanError(f"第 {idx + 1} 条连线的 {side} 端接口名不合法: {iface!r}")
            key = (dev_name, iface)
            if key in used_ifaces:
                raise PlanError(f"接口被重复占用: {dev_name} 的 {iface}")
            used_ifaces.add(key)
            ends.append({"device": dev_name, "interface": iface})
        if ends[0] == ends[1]:
            raise PlanError(f"第 {idx + 1} 条连线两端相同")
        out_links.append({"a": ends[0], "b": ends[1]})

    return {
        "name": str(plan.get("name") or "ai_topology"),
        "devices": out_devices,
        "links": out_links,
        "auto_start": bool(plan.get("auto_start", False)),
        "rationale": str(plan.get("rationale") or "")[:500],
    }


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------
def validate_plan(plan: dict, known_ips: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    校验任意 plan，返回 {kind, ...可直接下发的字段, rationale}。

    这是 AI 与下发链路之间唯一的通道：过了这里才谈得上预览/确认。
    """
    if not isinstance(plan, dict):
        raise PlanError("plan 必须是 JSON 对象")
    kind = str(plan.get("kind") or "").strip().lower()
    if kind not in PLAN_KINDS:
        raise PlanError(f"plan.kind 必须是 {' 或 '.join(PLAN_KINDS)}，得到 {kind!r}")

    if kind == "config":
        result = validate_config_plan(plan, known_ips)
    else:
        result = validate_topology_plan(plan)
    result["kind"] = kind
    return result


def render_plan_summary(validated: Dict[str, Any]) -> str:
    """把校验通过的 plan 渲染成一句话摘要，供确认弹窗展示。"""
    if validated["kind"] == "config":
        targets = validated["target_devices"]
        series = validated.get("series")
        suffix = "（含变量序列展开）" if series else ""
        return (
            f"AI 建议向 {len(targets)} 台设备（{', '.join(targets[:5])}"
            f"{' 等' if len(targets) > 5 else ''}）下发 {len(validated['command_template'])} 条命令{suffix}。"
            "请核对下方命令后再确认。"
        )
    return (
        f"AI 建议创建拓扑「{validated['name']}」："
        f"{len(validated['devices'])} 台设备、{len(validated['links'])} 条连线"
        f"{'，并自动开机' if validated.get('auto_start') else ''}。"
    )
