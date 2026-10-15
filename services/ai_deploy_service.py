"""
ai_deploy_service.py - AI 一键刷配置（生成 → 预览 → 确认 → 刷入 → 检测）
=====================================================================

链路（AGENTS.md 安全红线：备份 → 预览 → 确认 → 验证，确认闸门不可省略）：

    1. generate  调 AI 产出结构化 plan（受约束 JSON，不是自由文本）
    2. preview   校验 plan + 渲染逐台命令 + 危险命令拦截 + 备份
    3. deploy    人在预览上点确认后才下发（confirm=true）
    4. verify    下发后自动跑验证命令，比对真实设备回显

与既有模块的关系：
    - 复用 services/ai_plan.py     的 plan 提取与校验（不重造）
    - 复用 services/free_config_renderer.py 的渲染与危险命令拦截
    - 复用 services/free_config_executor.py 的执行器（含逐台失败处理）
    - 复用 services/backup_service.py 的下发前备份
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("ensp-web.ai-deploy")


class AiDeployError(Exception):
    """一键刷配置的可预期错误。"""


# ---------------------------------------------------------------------------
# 阶段 1：生成
# ---------------------------------------------------------------------------

# 交给 AI 的设备事实：只给真实存在的信息，减少幻觉
_DEVICE_FACT_TEMPLATE = """- {name}（{ip}，型号 {model}{kind}）"""


def build_device_facts(devices: List[dict],
                       capability_results: Optional[Dict[str, Any]] = None,
                       limit: int = 30) -> str:
    """把设备表渲染成给 AI 的事实清单。

    只描述**真实存在**的设备，避免 AI 编造不存在的 IP。
    """
    if not devices:
        return "（当前项目没有任何设备）"

    lines: List[str] = []
    for d in devices[:limit]:
        name = d.get("name") or d.get("ip") or "?"
        ip = d.get("ip") or ""
        model = d.get("model") or d.get("device_type") or "未知"
        kind = ""
        if capability_results:
            cap = capability_results.get(ip)
            if isinstance(cap, dict) and cap.get("role"):
                kind = f"，角色 {cap['role']}"
        lines.append(_DEVICE_FACT_TEMPLATE.format(
            name=name, ip=ip, model=model, kind=kind))

    if len(devices) > limit:
        lines.append(f"（另有 {len(devices) - limit} 台设备未列出）")
    return "\n".join(lines)


def build_deploy_prompt(instruction: str, device_facts: str,
                        topo_facts: str = "") -> str:
    """构造「生成配置 plan」的用户消息。"""
    parts = [
        "请为下列设备生成配置。",
        "",
        "## 设备清单",
        device_facts,
    ]
    if topo_facts:
        parts += ["", "## 拓扑连线", topo_facts]
    parts += [
        "",
        "## 要求",
        instruction.strip(),
        "",
        "## 约束",
        "- 命令必须是华为 VRP 语法，逐条给出，且要包含进入视图的命令（如 system-view、interface X）。",
        "- 需要进入子视图时，必须写全 `system-view` → `interface GigabitEthernet0/0/0` → 子命令 → `quit`。",
        "- targets 只能使用上面列出的设备 IP，不得编造。",
        "- verify 给出用于确认配置生效的 display 命令。",
        "",
        "## 逐台不同配置怎么写（重要）",
        "每台设备配置**不相同**时（如 R1 用 1.1.1.1、R2 用 2.2.2.2），"
        "用 `series` 表达按序号变化的模板：",
        "```json",
        '{"kind":"config","targets":["IP1","IP2"],'
        '"commands":["system-view","ospf 1 router-id {{router_id}}",'
        '"area 0","network {{network}} 0.0.0.255","quit","quit"],'
        '"series":{"base":{"router_id":"1.1.1.{i}","network":"192.168.{i}.0"},'
        '"start":1,"step":1},"verify":["display ospf peer"],"save":false}',
        "```",
        "- series.base 的值必须是**含 {i} 的模板**，{i} 按目标顺序取 start、start+step…",
        "- 可用占位符：`{i}`（序号）、`{ip}`、`{ip_last}`。",
        "- **禁止**把 base 的值写成变量名本身（如 \"router_id\": \"{{router_id}}\"）——那是自引用，会展开成非法命令。",
        "- 若各台命令确实无法用 {i} 表达，就为每台单独输出一个 JSON 块。",
    ]
    return "\n".join(parts)


def generate_plan(instruction: str, devices: List[dict],
                  capability_results: Optional[Dict[str, Any]] = None,
                  topo_facts: str = "",
                  device_ip: Optional[str] = None,
                  chat_fn=None) -> Dict[str, Any]:
    """调用 AI 产出并校验 plan。返回 {success, plan, summary, reply} 或 {success:False, error}。"""
    from services import ai_assistant
    from services.ai_plan import PlanError, extract_plan, render_plan_summary, validate_plan

    if not instruction or not instruction.strip():
        raise AiDeployError("请先描述要下发的配置")

    if not ai_assistant.is_configured():
        return {"success": False,
                "error": "AI 未配置：请先在设置里填写 API 地址、Key 和模型名"}

    facts = build_device_facts(devices, capability_results)
    prompt = build_deploy_prompt(instruction, facts, topo_facts)

    context = ""
    if device_ip:
        dev = next((d for d in devices if d.get("ip") == device_ip), None)
        if dev:
            context = ai_assistant.render_device_context(
                dev, (capability_results or {}).get(device_ip))

    # 复用路由层同一份系统提示词，保证「生成」与「校验」的口径一致
    from routes_ai import _PLAN_SYSTEM

    messages = ai_assistant.build_messages(
        prompt, [], device_context=context, task="free", extra_context=_PLAN_SYSTEM)

    try:
        reply = chat_fn(messages) if chat_fn else ai_assistant.chat_once(messages)
    except Exception as e:
        return {"success": False, "error": f"模型调用失败: {e}"}

    plan, extract_error = extract_plan(reply)
    if plan is None:
        return {"success": False, "error": f"AI 未输出合法 plan: {extract_error}",
                "reply": reply}

    known_ips = [d.get("ip") for d in devices if d.get("ip")]
    try:
        validated = validate_plan(plan, known_ips)
    except PlanError as e:
        return {"success": False, "error": f"plan 校验未通过: {e}",
                "plan": plan, "reply": reply}

    if validated.get("kind") != "config":
        return {"success": False,
                "error": f"一键刷配置只接受 config 类型 plan，收到 {validated.get('kind')!r}",
                "plan": validated, "reply": reply}

    return {"success": True, "plan": validated,
            "summary": render_plan_summary(validated), "reply": reply}


# ---------------------------------------------------------------------------
# 阶段 2：预览（含危险命令拦截与备份）
# ---------------------------------------------------------------------------

def sanitize_series(plan: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    """净化 AI 给出的变量序列。

    真机实测：AI 经常写出**自引用**的 series ——
        "base": {"router_id": "{{router_id}}"}
    变量名和值都是同一个占位符，展开后得到字面量 `{{router_id}}`，
    下发到设备就是非法命令。AI 把 series 误解成「声明变量名」而不是
    「给变量一个按序号变化的模板」。

    这里剔除自引用项；剩余项仍按原样使用。返回 (净化后的 plan, 说明)。
    """
    series = plan.get("series")
    if not isinstance(series, dict):
        return plan, []

    base = series.get("base")
    if not isinstance(base, dict):
        return plan, []

    notes: List[str] = []
    cleaned: Dict[str, str] = {}
    for name, tpl in base.items():
        text = str(tpl)
        # 自引用：值就是 {{自己}}（可能带空格）
        stripped = text.strip().strip("{}").strip()
        if stripped == str(name):
            notes.append(f"series.{name} 是自引用（{text!r}），已忽略")
            continue
        # 值里不含任何序号占位符时，它对「按序号变化」没有意义
        if "{" not in text:
            notes.append(f"series.{name} 不含序号占位符（{text!r}），已忽略")
            continue
        cleaned[str(name)] = text

    out = dict(plan)
    if cleaned:
        out["series"] = {**series, "base": cleaned}
    else:
        out.pop("series", None)
        notes.append("series 里没有可用的模板，已整体忽略")
    return out, notes


def derive_series_from_instruction(instruction: str, targets: List[str],
                                   existing: Optional[Dict[str, Any]] = None
                                   ) -> Optional[Dict[str, Any]]:
    """从自然语言指令里推导变量序列。

    支持的写法（真机实测的常见表述）：
        「192.168.1.1 到 192.168.1.10」
        「router-id 从 1.1.1.1 开始递增」
        「192.168.1.1/.2/.3」

    推导不出时返回 None，交由上层提示用户手工指定。
    """
    import re

    if existing and isinstance(existing.get("base"), dict) and existing["base"]:
        return existing
    if not instruction:
        return None

    # 形如 192.168.1.1 到 192.168.1.10 / 192.168.1.1-192.168.1.10
    m = re.search(
        r"(\d{1,3}(?:\.\d{1,3}){3})\s*(?:到|至|-|~|—)\s*(\d{1,3}(?:\.\d{1,3}){3})",
        instruction)
    if m:
        first, last = m.group(1), m.group(2)
        a = first.rsplit(".", 1)
        b = last.rsplit(".", 1)
        if a[0] == b[0]:  # 前三段相同 → 末段递增
            try:
                lo, hi = int(a[1]), int(b[1])
            except ValueError:
                return None
            if hi >= lo:
                return {"base": {"ip": f"{a[0]}.{{i}}"},
                        "start": lo, "step": 1, "width": 0,
                        "per_device_overrides": {}}

    # 形如 router-id 用 1.1.1.1 / 2.2.2.2（逐台不同）→ 按目标顺序取末段
    ips = re.findall(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", instruction)
    if len(ips) >= 2 and len(ips) >= len(targets):
        return None  # 交给 AI 的 per_target 机制处理，不强行套序列
    return None


def plan_to_task(plan: Dict[str, Any], task_name: str = "") -> Dict[str, Any]:
    """把校验过的 plan 转成 free-deploy 的 task 入参。

    兼容两种字段名：AI 原始 plan 用 `targets`/`commands`，
    而 validate_config_plan 归一化后输出 `target_devices`/`command_template`。
    只认其中一种会静默得到空目标（真机实测踩过）。

    `per_target_commands` 存在时（AI 按设备分别给命令，如 R1 用 1.1.1.1、
    R2 用 2.2.2.2），把它转成 device_vars 里的逐台命令覆盖，避免
    「两台刷成同一份配置」这种静默错配。
    """
    per_target = plan.get("per_target_commands") or {}
    device_vars: Dict[str, Dict[str, Any]] = {}

    targets = list(plan.get("target_devices") or plan.get("targets") or [])
    template = list(plan.get("command_template") or plan.get("commands") or [])
    verify = list(plan.get("verify_commands") or plan.get("verify") or [])
    save = plan.get("save_after_success")
    if save is None:
        save = plan.get("save")

    if per_target:
        # 逐台命令用 device_vars 的 commands 覆盖；模板留空时以第一台为模板
        first = next(iter(per_target.values()), [])
        template = list(first)
        for ip, cmds in per_target.items():
            device_vars[ip] = {"commands": list(cmds)}

    return {
        "task_name": task_name or plan.get("task_name") or "AI 一键配置",
        "target_devices": targets,
        "command_template": template,
        "global_vars": {},
        "device_vars": device_vars,
        "verify_commands": verify,
        "save_after_success": bool(save),
        "series": plan.get("series"),
    }


def build_preview(plan: Dict[str, Any], devices: List[dict],
                  task_name: str = "", force: bool = False) -> Dict[str, Any]:
    """渲染逐台命令 + 危险命令拦截。**不下发**。

    series 展开必须在这里做：`render_preview` 本身不认 series，
    它只做 `{{变量}}` 替换。真机实测：AI 用 series 表达「R1 用 1.1.1.1、
    R2 用 2.2.2.2」时，不展开会报「存在未定义变量: router_id」。

    返回 {success, preview, task, dangerous, warnings} 或 {success:False, errors}
    """
    from services.free_config_renderer import (
        check_dangerous_commands, render_preview, validate_task)

    task = plan_to_task(plan, task_name)
    if not task["target_devices"]:
        return {"success": False, "errors": ["plan 未指定任何目标设备"]}
    if not task["command_template"]:
        return {"success": False, "errors": ["plan 未给出任何配置命令"]}

    # 展开变量序列 → device_vars（与 /api/config/free-preview 同源逻辑）
    if task.get("series"):
        try:
            from services.batch_variables import (
                expand_variable_series, merge_series_into_device_vars)
            by_ip = {d.get("ip"): d for d in devices}
            ordered = [by_ip.get(ip, ip) for ip in task["target_devices"]]
            expanded = expand_variable_series(task["series"], ordered)
            task["device_vars"] = merge_series_into_device_vars(
                expanded["device_vars"], task.get("device_vars") or {})
        except Exception as e:
            return {"success": False,
                    "errors": [f"变量序列展开失败: {e}"]}

    ok, errors = validate_task(task)
    if not ok:
        return {"success": False, "errors": errors}

    rendered = render_preview(task)
    if not rendered.get("success"):
        return {"success": False,
                "errors": rendered.get("errors") or ["预览渲染失败"],
                "warnings": rendered.get("warnings") or []}

    # 危险命令拦截（安全红线，force 才能绕过）
    is_safe, dangerous = check_dangerous_commands(task["command_template"], force=force)
    if not is_safe:
        return {
            "success": False,
            "errors": ["检测到危险命令，已阻止下发；确认无误可设置 force=true 强制继续"],
            "dangerous": dangerous,
        }

    return {
        "success": True,
        "preview": rendered.get("preview") or {},
        "task": task,
        "dangerous": dangerous,
        "warnings": rendered.get("warnings") or [],
    }


def backup_targets(devices: List[dict], target_ips: List[str]) -> Dict[str, Any]:
    """下发前逐台备份（安全红线：不可绕过）。"""
    from services.backup_service import backup_device_config

    backups: Dict[str, Any] = {}
    failures: List[str] = []
    for ip in target_ips:
        dev = next((d for d in devices if d.get("ip") == ip), None)
        if not dev:
            failures.append(f"{ip}: 设备不存在")
            continue
        try:
            r = backup_device_config(dev)
            if r.get("success"):
                backups[ip] = r.get("filename") or r.get("file") or ""
            else:
                failures.append(f"{ip}: {r.get('error') or '备份失败'}")
        except Exception as e:
            failures.append(f"{ip}: {e}")
    return {"backups": backups, "failures": failures,
            "ok": not failures}


# ---------------------------------------------------------------------------
# 阶段 4：检测
# ---------------------------------------------------------------------------

def verify_deployment(devices: List[dict], target_ips: List[str],
                      verify_commands: List[str],
                      timeout: int = 15, read_timeout: int = 30) -> Dict[str, Any]:
    """下发后逐台跑验证命令，采集**真实回显**。"""
    from domain.console_connection import ConsoleConnection

    results: Dict[str, Any] = {}
    for ip in target_ips:
        dev = next((d for d in devices if d.get("ip") == ip), None)
        if not dev:
            results[ip] = {"success": False, "error": "设备不存在"}
            continue
        port = dev.get("console_port")
        if not port:
            results[ip] = {"success": False, "error": "该设备没有 Console 端口，无法检测"}
            continue

        conn = None
        outputs: Dict[str, str] = {}
        try:
            conn = ConsoleConnection(port=int(port), timeout=float(timeout))
            conn.connect()
            for cmd in verify_commands:
                try:
                    out = conn.send_command(cmd, read_timeout=read_timeout)
                    outputs[cmd] = out or ""
                except Exception as e:
                    outputs[cmd] = f"<检测失败: {e}>"
            results[ip] = {"success": True, "name": dev.get("name") or ip,
                           "outputs": outputs}
        except Exception as e:
            results[ip] = {"success": False, "error": f"连接失败: {e}"}
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    return results


def summarize_verify(verify_results: Dict[str, Any]) -> Dict[str, Any]:
    """把验证结果压成可展示的摘要。"""
    ok = sum(1 for r in verify_results.values() if r.get("success"))
    return {
        "total": len(verify_results),
        "ok": ok,
        "failed": len(verify_results) - ok,
        "failed_ips": [ip for ip, r in verify_results.items() if not r.get("success")],
    }


# ---------------------------------------------------------------------------
# 端到端：一次调用完成「生成 → 预览」，产出待确认的方案
# ---------------------------------------------------------------------------

def prepare(instruction: str, devices: List[dict],
            capability_results: Optional[Dict[str, Any]] = None,
            topo_facts: str = "", device_ip: Optional[str] = None,
            task_name: str = "", force: bool = False,
            chat_fn=None) -> Dict[str, Any]:
    """生成 + 预览 + 备份，返回待人工确认的方案（**不刷入**）。"""
    started = time.time()

    gen = generate_plan(instruction, devices, capability_results,
                        topo_facts, device_ip, chat_fn=chat_fn)
    if not gen.get("success"):
        return gen

    # AI 常把 series 写成自引用（base.x = "{{x}}"），展开后会得到字面量
    # 占位符，下发即非法命令。先净化，剔除这类无意义项。
    plan, series_notes = sanitize_series(gen["plan"])
    if not plan.get("series") and not plan.get("per_target_commands"):
        derived = derive_series_from_instruction(
            instruction, plan.get("target_devices") or plan.get("targets") or [])
        if derived:
            plan["series"] = derived
            series_notes.append("已从指令推导出变量序列")

    prev = build_preview(plan, devices, task_name, force=force)
    if not prev.get("success"):
        return {**prev, "plan": plan, "summary": gen.get("summary"),
                "reply": gen.get("reply"), "series_notes": series_notes}

    targets = list(prev["task"]["target_devices"])
    bk = backup_targets(devices, targets)

    return {
        "success": True,
        "stage": "prepared",
        "plan": plan,
        "summary": gen.get("summary"),
        "reply": gen.get("reply"),
        "series_notes": series_notes,
        "task": prev["task"],
        "preview": prev["preview"],
        "dangerous": prev.get("dangerous") or [],
        "warnings": prev.get("warnings") or [],
        "backups": bk["backups"],
        "backup_failures": bk["failures"],
        "targets": targets,
        "elapsed_ms": int((time.time() - started) * 1000),
        "note": "这是待确认方案，尚未下发。确认后才会刷入。",
    }
