"""
agent_tools.py - Agent 工具注册表（只读观测 + 待确认意见书）
============================================================
把项目已有的能力包装成 LLM 可调用的**工具**。

Agent 循环跑在 Node 的 pi-agent-core 上（`agent_node/`），而**工具的真源
在这里**：sidecar 启动时通过 `GET /api/agent/internal/tools` 拉取本模块的
schema，每次工具调用再回 `POST /api/agent/internal/tools/execute` 执行。
Node 侧不硬编码任何工具——所以"Agent 能做什么"只有这一处定义。

安全模型（对应 AGENTS.md 第六节「AI 只建议不执行」）：
工具分两类，**注册表里不存在任何"直接下发"的工具**——

- ``read``    只读观测：查设备表、读配置、ping、路径追踪、查 eNSP 状态…
              对设备只做 display 类操作，不改变任何配置。
- ``prepare`` 只产出**待确认意见书**：调既有 ai_deploy_service.prepare
              （生成 → 预览 → 危险命令拦截 → 备份），返回一个 plan_id。
              真正下发仍必须由人在既有确认闸门（/api/ai/oneclick/deploy）
              上点确认，Agent 无从绕过。

因此「Agent 会不会自己把配置刷进去」这个问题在**结构上**就不成立：
它拿不到那样的工具。

本模块只依赖既有服务，不新增下发路径；所有 handler 均为纯函数式调用，
ctx 注入设备表与缓存，便于离线单测。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("ensp-web.agent-tools")

MAX_TOOL_OUTPUT = 6000      # 单个工具结果回灌模型的最大字符数
CONFIG_EXCERPT = 3000


class ToolError(Exception):
    """工具调用失败（参数非法 / 依赖缺失 / 设备不可达）。"""


# ---------------------------------------------------------------------------
# 运行上下文：把设备表与缓存注入 handler，避免 handler 自己去摸全局 state
# ---------------------------------------------------------------------------
@dataclass
class AgentContext:
    """一次 Agent 运行期间的只读视图 + 意见书登记口。"""

    devices: List[dict] = field(default_factory=list)
    capability_results: Dict[str, dict] = field(default_factory=dict)
    collection_results: Dict[str, dict] = field(default_factory=dict)
    # 允许连设备（单测里置 False，避免碰真机）
    allow_device_io: bool = True
    # 意见书登记：把 prepare 结果存起来并返回 plan_id。
    # 由路由层注入（写 state["ai_oneclick_plans"]），单测里注入 stub。
    register_proposal: Optional[Callable[[dict], str]] = None

    def find_device(self, ip: str) -> dict:
        for d in self.devices:
            if d.get("ip") == ip:
                return d
        raise ToolError(f"设备表中没有 {ip}；可用设备见 list_devices")


# ---------------------------------------------------------------------------
# 工具定义
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: Dict[str, Any]
    kind: str                       # read | prepare
    handler: Callable[[Dict[str, Any], AgentContext], Dict[str, Any]]

    def to_openai_schema(self) -> Dict[str, Any]:
        """转成 OpenAI /chat/completions 的 tools 数组元素。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


TOOLS: Dict[str, Tool] = {}


def _register(name: str, description: str, parameters: Dict[str, Any],
              kind: str = "read"):
    def deco(fn):
        TOOLS[name] = Tool(name=name, description=description,
                           parameters=parameters, kind=kind, handler=fn)
        return fn
    return deco


def _obj(props: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required or [],
        "additionalProperties": False,
    }


def _str(desc: str) -> Dict[str, Any]:
    return {"type": "string", "description": desc}


def _int(desc: str, default: Optional[int] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"type": "integer", "description": desc}
    if default is not None:
        out["default"] = default
    return out


def _bool(desc: str, default: bool = False) -> Dict[str, Any]:
    return {"type": "boolean", "description": desc, "default": default}


# ---------------------------------------------------------------------------
# 只读工具 · 设备与拓扑
# ---------------------------------------------------------------------------
@_register(
    "list_devices",
    "列出当前项目设备表（名称、IP、型号、Console 端口）。做任何按设备的操作前先调它拿真实 IP。",
    _obj({}),
)
def _t_list_devices(args: Dict[str, Any], ctx: AgentContext) -> Dict[str, Any]:
    if not ctx.devices:
        return {"devices": [], "note": "当前项目没有设备，请先在设备管理页添加或加载内置案例"}
    return {
        "devices": [
            {
                "name": d.get("name") or d.get("ip"),
                "ip": d.get("ip"),
                "model": d.get("model") or d.get("device_type") or "未知",
                "console_port": d.get("console_port"),
            }
            for d in ctx.devices
        ]
    }


@_register(
    "get_device_detail",
    "读取一台设备的实测上下文：协议能力状态、协议角色、接口、当前配置摘录、探测异常。"
    "回答任何关于这台设备支持什么、处于什么状态的问题前必须先调它。",
    _obj({"ip": _str("设备 IP（来自 list_devices）")}, ["ip"]),
)
def _t_get_device_detail(args: Dict[str, Any], ctx: AgentContext) -> Dict[str, Any]:
    from services import ai_assistant

    ip = str(args.get("ip") or "").strip()
    dev = ctx.find_device(ip)
    cap = ctx.capability_results.get(ip)
    if not cap:
        return {
            "ip": ip,
            "name": dev.get("name"),
            "has_capability_data": False,
            "note": "该设备尚无能力识别数据。如需真实状态，请先调 probe_capabilities。",
        }
    return {
        "ip": ip,
        "has_capability_data": True,
        "context": ai_assistant.render_device_context(dev, cap),
    }


@_register(
    "get_current_config",
    "读取一台设备的 display current-configuration 摘录（需要该设备已做过能力识别或采集）。",
    _obj({"ip": _str("设备 IP")}, ["ip"]),
)
def _t_get_current_config(args: Dict[str, Any], ctx: AgentContext) -> Dict[str, Any]:
    ip = str(args.get("ip") or "").strip()
    ctx.find_device(ip)
    cap = ctx.capability_results.get(ip) or {}
    text = (cap.get("raw_outputs") or {}).get("display current-configuration", "")
    if not text:
        coll = ctx.collection_results.get(ip) or {}
        commands = coll.get("commands") if isinstance(coll, dict) else {}
        if isinstance(commands, dict):
            entry = commands.get("display current-configuration") or {}
            text = entry.get("output", "") if isinstance(entry, dict) else str(entry or "")
    if not text:
        return {"ip": ip, "config": "", "note": "没有配置副本，请先对该设备执行能力识别或采集"}
    truncated = len(text) > CONFIG_EXCERPT
    return {"ip": ip, "config": text[:CONFIG_EXCERPT], "truncated": truncated}


@_register(
    "get_ensp_status",
    "查询 eNSP 运行状态与当前拓扑：进程是否存活、DLL 是否注入、设备数与电源状态。",
    _obj({}),
)
def _t_get_ensp_status(args: Dict[str, Any], ctx: AgentContext) -> Dict[str, Any]:
    from domain import ensp_runtime

    status = ensp_runtime.get_ensp_runtime_status()
    live = ensp_runtime.probe_ensp_live()
    return {"status": status, "live": live}


@_register(
    "list_topologies",
    "列出项目里的全部拓扑（名称、设备数、是否当前激活）。",
    _obj({}),
)
def _t_list_topologies(args: Dict[str, Any], ctx: AgentContext) -> Dict[str, Any]:
    from domain import workspace

    try:
        items = workspace.list_topologies()
    except Exception as e:
        raise ToolError(f"读取拓扑库失败: {e}") from None
    return {"topologies": items, "active": workspace.active_topology()}


@_register(
    "search_commands",
    "在项目内置的 VRP 命令目录里按关键词检索标准写法（返回命令模板与用途）。"
    "不确定某功能对应哪条命令时用它，不要凭空记忆语法。",
    _obj({"keyword": _str("关键词，如 ospf / vlan / nat / dhcp")}, ["keyword"]),
)
def _t_search_commands(args: Dict[str, Any], ctx: AgentContext) -> Dict[str, Any]:
    from services.command_catalog import COMMAND_CATALOG

    kw = str(args.get("keyword") or "").strip().lower()
    if not kw:
        raise ToolError("keyword 不能为空")
    hits = []
    for key, entry in COMMAND_CATALOG.items():
        blob = " ".join([
            key,
            str(entry.get("name") or ""),
            str(entry.get("desc") or entry.get("description") or ""),
            " ".join(entry.get("trigger") or []),
            " ".join(entry.get("command_template") or []),
        ]).lower()
        if kw in blob:
            hits.append({
                "key": key,
                "name": entry.get("name") or key,
                "commands": (entry.get("command_template") or [])[:12],
                "verify": (entry.get("verify") or [])[:6],
            })
    return {"keyword": kw, "count": len(hits), "matches": hits[:8]}


# ---------------------------------------------------------------------------
# 只读工具 · 联通性与路径（会连设备，但不改配置）
# ---------------------------------------------------------------------------
def _require_io(ctx: AgentContext) -> None:
    if not ctx.allow_device_io:
        raise ToolError("当前运行禁用了设备连接（allow_device_io=False）")


@_register(
    "ping",
    "从一台 eNSP 设备上 ping 目标地址，返回真实回显统计（通/不通、丢包率、时延）。"
    "验证连通性时用它，不要凭猜测断言通不通。",
    _obj({
        "ip": _str("发起 ping 的设备 IP"),
        "target": _str("目标 IPv4 地址"),
        "count": _int("ping 次数（1-20）", 4),
        "source": _str("可选：-a 源地址，用于从指定网段发起"),
    }, ["ip", "target"]),
)
def _t_ping(args: Dict[str, Any], ctx: AgentContext) -> Dict[str, Any]:
    from services.live_check import ping_from_device

    _require_io(ctx)
    dev = ctx.find_device(str(args.get("ip") or "").strip())
    result = ping_from_device(
        dev,
        str(args.get("target") or "").strip(),
        count=int(args.get("count") or 4),
        source=(str(args.get("source")).strip() or None) if args.get("source") else None,
    )
    return result


@_register(
    "trace_path",
    "追踪源→目的的转发路径：逐跳给出设备、出接口、下一跳与选路依据，并在不通时定位断点。"
    "基于已有采集数据推导，零新依赖。需要相关设备已执行过采集。",
    _obj({
        "source_ip": _str("起始地址"),
        "dest_ip": _str("目的地址"),
    }, ["source_ip", "dest_ip"]),
)
def _t_trace_path(args: Dict[str, Any], ctx: AgentContext) -> Dict[str, Any]:
    from services.path_trace import build_snapshots, format_trace_for_ai, trace_path

    snapshots = build_snapshots(ctx.devices, ctx.collection_results)
    if not any(s.get("has_data") for s in snapshots):
        return {
            "success": False,
            "note": "没有任何设备的采集数据，路径追踪无法进行。请先对相关设备执行采集。",
        }
    result = trace_path(
        str(args.get("source_ip") or "").strip(),
        str(args.get("dest_ip") or "").strip(),
        snapshots,
    )
    result["rendered"] = format_trace_for_ai(result)
    return result


@_register(
    "complete_command",
    "给 CLI 前缀补全候选（设备实测 ? 树 + 常用清单 + 内置目录三层）。"
    "写命令不确定下一段怎么写时用它。",
    _obj({
        "ip": _str("设备 IP"),
        "prefix": _str("已输入的命令前缀，如 disp 或 ip rou"),
        "view": _str("视图：display / system / interface，默认 display"),
    }, ["ip", "prefix"]),
)
def _t_complete_command(args: Dict[str, Any], ctx: AgentContext) -> Dict[str, Any]:
    from services.command_complete import complete_commands

    dev = ctx.find_device(str(args.get("ip") or "").strip())
    return complete_commands(
        dev,
        str(args.get("prefix") or "").strip(),
        view=(str(args["view"]).strip() if args.get("view") else None),
    )


@_register(
    "collect_device",
    "对一台设备执行信息采集（display 系列只读命令），结果是后续路径追踪与讲解的数据基础。",
    _obj({"ip": _str("设备 IP")}, ["ip"]),
)
def _t_collect_device(args: Dict[str, Any], ctx: AgentContext) -> Dict[str, Any]:
    from domain.collect import collect_device

    _require_io(ctx)
    dev = ctx.find_device(str(args.get("ip") or "").strip())
    result = collect_device(dev, fast_mode=True)
    ok = bool(result.get("ok", result.get("success")))
    return {
        "ip": dev.get("ip"),
        "ok": ok,
        "command_count": len(result.get("commands") or {}),
        "info": result.get("info") or {},
        "error": result.get("error"),
    }


@_register(
    "probe_capabilities",
    "对一台设备执行协议能力识别（会连设备跑 ? 树与 display 探测，较慢）。"
    "要判断这台设备到底支不支持某协议时，必须用它拿实测结果，不要只看镜像矩阵。",
    _obj({"ip": _str("设备 IP")}, ["ip"]),
)
def _t_probe_capabilities(args: Dict[str, Any], ctx: AgentContext) -> Dict[str, Any]:
    from services.capability_probe import probe_capabilities

    _require_io(ctx)
    dev = ctx.find_device(str(args.get("ip") or "").strip())
    result = probe_capabilities(dev)
    caps = result.get("capabilities") or {}
    unsupported = [k for k, v in caps.items() if not v.get("supported")]
    return {
        "ip": dev.get("ip"),
        "ok": result.get("ok"),
        "model": result.get("model"),
        "version": result.get("version"),
        "device_role": result.get("device_role"),
        "supported_count": len(caps) - len(unsupported),
        "unsupported": unsupported,
        "errors": result.get("errors") or [],
    }


# ---------------------------------------------------------------------------
# 意见书工具：只产出待确认方案，绝不直接下发
# ---------------------------------------------------------------------------
@_register(
    "propose_config",
    "根据自然语言要求生成一份**待确认的配置方案**（会调模型产出结构化 plan，"
    "然后校验、渲染逐台命令、拦截危险命令、并对目标设备备份）。"
    "返回一个 plan_id，**不会下发任何配置**——用户需要在界面上核对并点确认后才会生效。"
    "当用户要求给设备配置什么时用它，然后明确告诉用户去确认。",
    _obj({
        "instruction": _str("要下发的配置要求，如「三台路由器起 OSPF 区域 0」"),
        "device_ip": _str("可选：注入哪台设备的实测上下文"),
        "task_name": _str("可选：任务名"),
    }, ["instruction"]),
    kind="prepare",
)
def _t_propose_config(args: Dict[str, Any], ctx: AgentContext) -> Dict[str, Any]:
    from services import ai_deploy_service

    instruction = str(args.get("instruction") or "").strip()
    if not instruction:
        raise ToolError("instruction 不能为空")

    result = ai_deploy_service.prepare(
        instruction=instruction,
        devices=ctx.devices,
        capability_results=ctx.capability_results,
        device_ip=(str(args["device_ip"]).strip() if args.get("device_ip") else None),
        task_name=str(args.get("task_name") or ""),
    )
    if not result.get("success"):
        return {"ok": False, "error": result.get("error") or "方案生成失败",
                "errors": result.get("errors") or [],
                "dangerous": result.get("dangerous") or []}

    plan_id = None
    if ctx.register_proposal is not None:
        plan_id = ctx.register_proposal({
            "plan": result["plan"],
            "task": result["task"],
            "preview": result["preview"],
            "targets": result["targets"],
        })

    return {
        "ok": True,
        "stage": "prepared",
        "plan_id": plan_id,
        "targets": result["targets"],
        "denied": False,
        "preview_keys": list((result.get("preview") or {}).keys()),
        "warnings": result.get("warnings") or [],
        "dangerous": result.get("dangerous") or [],
        "backups": result.get("backups") or {},
        "backup_failures": result.get("backup_failures") or [],
        "series_notes": result.get("series_notes") or [],
        "summary": result.get("summary") or {},
        "note": result.get("note") or "这是待确认方案，尚未下发。",
    }


@_register(
    "propose_topology",
    "根据自然语言要求生成一份**待确认的拓扑搭建方案**（设备与连线），只做校验与摘要，"
    "不会真的在 eNSP 里建拓扑。用户确认后再走既有拓扑下发接口。",
    _obj({"instruction": _str("拓扑要求，如「搭一个两台路由器互联的拓扑」")}, ["instruction"]),
    kind="prepare",
)
def _t_propose_topology(args: Dict[str, Any], ctx: AgentContext) -> Dict[str, Any]:
    from services import ai_assistant
    from services.ai_plan import (
        PLAN_SYSTEM,
        PlanError,
        extract_plan,
        render_plan_summary,
        validate_plan,
    )

    instruction = str(args.get("instruction") or "").strip()
    if not instruction:
        raise ToolError("instruction 不能为空")
    if not ai_assistant.is_configured():
        return {"ok": False, "error": "AI 未配置，无法生成拓扑方案"}

    devices = ctx.devices
    facts = "\n".join(
        f"- {d.get('name')}（{d.get('ip')}，型号 {d.get('model') or '未知'}）"
        for d in devices) or "（当前项目没有任何设备）"
    prompt = (f"请设计一个拓扑。要求：{instruction}\n\n"
              f"## 当前已有设备（新拓扑不要与之重名）\n{facts}")
    messages = ai_assistant.build_messages(
        prompt, [], device_context="", task="free", extra_context=_PLAN_SYSTEM)
    reply = ai_assistant.chat_once(messages)

    plan, err = extract_plan(reply)
    if plan is None:
        return {"ok": False, "error": f"AI 未输出合法 plan: {err}", "reply": reply}

    known_ips = [d.get("ip") for d in devices if d.get("ip")]
    try:
        validated = validate_plan(plan, known_ips)
    except PlanError as e:
        return {"ok": False, "error": f"plan 校验未通过: {e}", "plan": plan}

    if validated.get("kind") != "topology":
        return {"ok": False,
                "error": f"期望 topology 类型，收到 {validated.get('kind')!r}"}

    return {
        "ok": True,
        "stage": "proposed",
        "kind": "topology",
        "plan": validated,
        "summary": render_plan_summary(validated),
        "note": "这是拓扑建议，尚未创建。用户确认后才走 /api/ensp/topo/apply。",
    }


# ---------------------------------------------------------------------------
# 调用入口
# ---------------------------------------------------------------------------
def tool_schemas(kind: Optional[str] = None) -> List[Dict[str, Any]]:
    """导出 OpenAI tools 数组。kind 过滤 read/prepare。"""
    return [t.to_openai_schema() for t in TOOLS.values()
            if kind is None or t.kind == kind]


def tool_names() -> List[str]:
    return sorted(TOOLS)


def sidecar_schemas() -> List[Dict[str, Any]]:
    """给 Node sidecar 的工具清单：OpenAI function schema + kind。

    sidecar 靠 kind 决定执行模式（prepare 类必须串行）。工具集由这里给出，
    所以"Agent 能不能下发配置"仍然只在本模块一处说了算。
    """
    out: List[Dict[str, Any]] = []
    for t in TOOLS.values():
        item = t.to_openai_schema()
        item["kind"] = t.kind
        out.append(item)
    return out


def describe_tools() -> List[Dict[str, Any]]:
    """给界面用的工具清单（透明展示 Agent 能做什么、不能做什么）。"""
    return [
        {"name": t.name, "description": t.description, "kind": t.kind}
        for t in TOOLS.values()
    ]


def _truncate(value: Any) -> Any:
    """工具结果回灌模型前截断，防止一个巨大回显吃光上下文。"""
    import json

    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) <= MAX_TOOL_OUTPUT:
        return value
    return {"truncated": True,
            "preview": text[:MAX_TOOL_OUTPUT],
            "note": f"结果过长已截断（原 {len(text)} 字符）"}


def call_tool(name: str, args: Optional[Dict[str, Any]],
              ctx: AgentContext) -> Dict[str, Any]:
    """执行一个工具。任何异常都转成结构化失败，绝不抛出打断 Agent 循环。"""
    tool = TOOLS.get(name)
    if tool is None:
        return {"ok": False,
                "error": f"未知工具 {name!r}；可用: {', '.join(tool_names())}"}
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return {"ok": False, "error": f"args 必须是对象，得到 {type(args).__name__}"}
    try:
        result = tool.handler(args, ctx)
    except ToolError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        logger.exception("工具 %s 执行失败", name)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if not isinstance(result, dict):
        result = {"ok": True, "result": result}
    result.setdefault("ok", True)
    return _truncate(result)


def render_tool_catalog() -> str:
    """把工具清单渲染进提示词（供无原生 function calling 的模型走 JSON 协议）。"""
    lines = []
    for t in TOOLS.values():
        required = t.parameters.get("required") or []
        params = ", ".join(
            f"{k}{'' if k in required else '?'}"
            for k in (t.parameters.get("properties") or {})
        )
        tag = "【只读】" if t.kind == "read" else "【只出待确认方案】"
        lines.append(f"- {t.name}({params}) {tag} {t.description}")
    return "\n".join(lines)
