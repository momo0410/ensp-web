"""
ai_assistant.py - AI 助教模块
=============================
为实训教学提供 AI 辅助：结果讲解、报错诊断、配置翻译、意图转配置、
实验任务指导。通过 OpenAI 兼容协议接入任意模型服务
（GLM / DeepSeek / Qwen / OpenAI / 本地 Ollama），API 地址、模型、
Key 全部由用户自行配置，存于项目根目录 ai_config.json（不入 git）。

设计要点：
- 上下文注入：回答基于这台设备的**实测数据**（能力矩阵 / 协议角色 /
  ? 命令树 / current-config 摘要），而不是凭空回忆 VRP 语法——
  这是教学准确率的关键。
- 两种模式：tutor（助教，引导式，默认）/ engineer（工程师，直接给命令）。
- 安全红线：AI 输出只进预览/工作台，永不直接下发。
- 实验指导（三期）：预置实验目录，用已有的能力/角色数据自动校验学生
  进度，AI 按进度给下一步提示。
"""

import json
import time
import logging
from typing import Dict, Any, List, Optional, Callable

import httpx

from .role_detector import detect_protocol_roles

logger = logging.getLogger("ensp-web.ai")

AI_CONFIG_FILE = "ai_config.json"
DEFAULT_CONFIG: Dict[str, Any] = {
    "api_base": "",           # 如 https://open.bigmodel.cn/api/paas/v4
    "api_key": "",
    "model": "",              # 如 glm-4-flash / deepseek-chat
    "mode": "tutor",          # tutor=助教引导 | engineer=工程师直给
    "temperature": 0.6,
}
# 传给模型的最大上下文（字符），控制 token 成本
MAX_HISTORY_MESSAGES = 12
CONFIG_EXCERPT_FULL = 6000
CONFIG_EXCERPT_SHORT = 2200


class AIError(Exception):
    """AI 模块可预期错误（未配置/网络/上游报错）。"""


# ---------------------------------------------------------------------------
# 配置管理
# ---------------------------------------------------------------------------
def load_config() -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(AI_CONFIG_FILE, "r", encoding="utf-8") as f:
            stored = json.load(f)
        if isinstance(stored, dict):
            cfg.update({k: v for k, v in stored.items() if k in DEFAULT_CONFIG})
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("读取 ai_config.json 失败: %s", e)
    return cfg


def save_config(update: Dict[str, Any]) -> Dict[str, Any]:
    """合并保存配置；api_key 传空/缺省表示保留原值。"""
    cfg = load_config()
    for key in ("api_base", "model"):
        if update.get(key) is not None and str(update[key]).strip() != "":
            cfg[key] = str(update[key]).strip()
    if update.get("mode") in ("tutor", "engineer"):
        cfg["mode"] = update["mode"]
    key = update.get("api_key")
    if key is not None and str(key).strip() != "":
        cfg["api_key"] = str(key).strip()
    try:
        t = float(update.get("temperature", cfg["temperature"]))
        cfg["temperature"] = min(max(t, 0.0), 1.5)
    except (TypeError, ValueError):
        pass
    try:
        with open(AI_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError as e:
        raise AIError(f"保存配置失败: {e}")
    return cfg


def is_configured(cfg: Optional[Dict[str, Any]] = None) -> bool:
    cfg = cfg or load_config()
    return bool(cfg.get("api_base") and cfg.get("api_key") and cfg.get("model"))


def mask_key(key: str) -> str:
    if not key:
        return ""
    return key[:4] + "****" if len(key) <= 8 else key[:4] + "****" + key[-4:]


def _headers(cfg: Dict[str, Any]) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }


def _endpoint(cfg: Dict[str, Any]) -> str:
    base = (cfg.get("api_base") or "").rstrip("/")
    if not base:
        raise AIError("未配置 API 地址")
    if not base.endswith("/chat/completions"):
        base += "/chat/completions"
    return base


def test_connection(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """用一次最小请求验证连通性。"""
    cfg = cfg or load_config()
    if not is_configured(cfg):
        return {"success": False, "error": "API 地址 / Key / 模型 尚未配置完整"}
    payload = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": False,
    }
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(_endpoint(cfg), headers=_headers(cfg),
                               json=payload)
        if resp.status_code == 200:
            return {"success": True, "message": f"连接成功（{cfg['model']}）"}
        detail = resp.text[:300]
        try:
            err = resp.json().get("error", {})
            detail = err.get("message", detail) if isinstance(err, dict) else detail
        except Exception:
            pass
        return {"success": False, "error": f"HTTP {resp.status_code}: {detail}"}
    except Exception as e:
        return {"success": False, "error": f"连接失败: {e}"}


# ---------------------------------------------------------------------------
# 对话核心（OpenAI 兼容 /chat/completions）
# ---------------------------------------------------------------------------
def chat_once(messages: List[dict], cfg: Optional[Dict[str, Any]] = None,
              max_tokens: int = 2048) -> str:
    cfg = cfg or load_config()
    if not is_configured(cfg):
        raise AIError("AI 尚未配置：请先在设置里填写 API 地址、Key 和模型名")
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "stream": False,
        "temperature": cfg.get("temperature", 0.6),
        "max_tokens": max_tokens,
    }
    try:
        with httpx.Client(timeout=90) as client:
            resp = client.post(_endpoint(cfg), headers=_headers(cfg), json=payload)
    except httpx.HTTPError as e:
        raise AIError(f"请求模型服务失败: {e}")
    if resp.status_code != 200:
        detail = resp.text[:300]
        try:
            err = resp.json().get("error", {})
            detail = err.get("message", detail) if isinstance(err, dict) else detail
        except Exception:
            pass
        raise AIError(f"模型服务返回 HTTP {resp.status_code}: {detail}")
    try:
        return resp.json()["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, ValueError) as e:
        raise AIError(f"模型响应格式异常: {e}")


def chat_message(messages: List[dict], cfg: Optional[Dict[str, Any]] = None,
                 tools: Optional[List[dict]] = None,
                 max_tokens: int = 2048) -> Dict[str, Any]:
    """取回模型响应的**完整 message 对象**（含 tool_calls），供 Agent 循环使用。

    与 chat_once 的区别：chat_once 只返回 content 文本，丢掉 function calling
    的 tool_calls 字段。Agent 需要拿到 tool_calls 才能决定下一步调什么工具。

    返回 ``{"content": str, "tool_calls": [...]}``。
    """
    cfg = cfg or load_config()
    if not is_configured(cfg):
        raise AIError("AI 尚未配置：请先在设置里填写 API 地址、Key 和模型名")
    payload: Dict[str, Any] = {
        "model": cfg["model"],
        "messages": messages,
        "stream": False,
        "temperature": cfg.get("temperature", 0.6),
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    try:
        with httpx.Client(timeout=90) as client:
            resp = client.post(_endpoint(cfg), headers=_headers(cfg), json=payload)
    except httpx.HTTPError as e:
        raise AIError(f"请求模型服务失败: {e}")
    if resp.status_code != 200:
        detail = resp.text[:300]
        try:
            err = resp.json().get("error", {})
            detail = err.get("message", detail) if isinstance(err, dict) else detail
        except Exception:
            pass
        raise AIError(f"模型服务返回 HTTP {resp.status_code}: {detail}")
    try:
        message = resp.json()["choices"][0]["message"]
    except (KeyError, IndexError, ValueError) as e:
        raise AIError(f"模型响应格式异常: {e}")
    if not isinstance(message, dict):
        raise AIError(f"模型响应格式异常: message 不是对象（{type(message).__name__}）")
    return {
        "content": message.get("content") or "",
        "tool_calls": message.get("tool_calls") or [],
    }


def chat_stream(messages: List[dict], cfg: Optional[Dict[str, Any]] = None,
                max_tokens: int = 2048):
    """流式对话，逐段 yield 文本增量。异常统一抛 AIError。"""
    cfg = cfg or load_config()
    if not is_configured(cfg):
        raise AIError("AI 尚未配置：请先在设置里填写 API 地址、Key 和模型名")
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "stream": True,
        "temperature": cfg.get("temperature", 0.6),
        "max_tokens": max_tokens,
    }
    try:
        with httpx.stream("POST", _endpoint(cfg), headers=_headers(cfg),
                          json=payload, timeout=httpx.Timeout(90, connect=15)) as resp:
            if resp.status_code != 200:
                body = resp.read().decode("utf-8", errors="replace")[:300]
                raise AIError(f"模型服务返回 HTTP {resp.status_code}: {body}")
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                choices = obj.get("choices") or []
                if not choices:
                    # 部分服务把错误放在流里
                    if obj.get("error"):
                        err = obj["error"]
                        raise AIError(err.get("message", str(err)) if isinstance(err, dict) else str(err))
                    continue
                delta = (choices[0].get("delta") or {}).get("content")
                if delta:
                    yield delta
    except httpx.HTTPError as e:
        raise AIError(f"请求模型服务失败: {e}")


# ---------------------------------------------------------------------------
# 上下文组装：把设备实测数据变成模型能读懂的文本
# ---------------------------------------------------------------------------
_STATUS_LABEL = {"running": "运行中", "configured": "已配置",
                 "supported": "支持", "unknown": "未知"}


def render_device_context(device: dict, cap_result: Optional[dict],
                          config_full: bool = False) -> str:
    """设备上下文 → 文本。cap_result 为空时给出明确提示。"""
    if not cap_result:
        return f"【设备】{device.get('name', '')} ({device.get('ip', '')})\n（尚未执行能力识别，请提示学生先完成能力识别再提问）"

    roles = detect_protocol_roles(cap_result)
    lines = []
    lines.append(
        f"【设备】{cap_result.get('hostname') or device.get('name', '')} "
        f"({cap_result.get('ip', device.get('ip', ''))}) "
        f"角色: {cap_result.get('device_role', 'unknown')}")
    matrix = cap_result.get("matrix") or {}
    if matrix.get("family"):
        lines.append(f"【镜像】{matrix.get('image') or ''} "
                     f"家族 {matrix.get('family_label') or matrix['family']}"
                     f"（矩阵置信 {matrix.get('confidence', '')}）")

    buckets: Dict[str, List[str]] = {"running": [], "configured": [],
                                     "supported": [], "unsupported": []}
    for key, cap in (cap_result.get("capabilities") or {}).items():
        state = ("running" if cap.get("running") else
                 "configured" if cap.get("configured") else
                 "supported" if cap.get("supported") else "unsupported")
        buckets[state].append(key)
    parts = []
    label_map = {"running": "运行中", "configured": "已配置",
                 "supported": "支持未配置", "unsupported": "不支持"}
    for state, label in label_map.items():
        if buckets[state]:
            parts.append(f"{label}: {', '.join(sorted(buckets[state]))}")
    lines.append("【协议状态】" + ("; ".join(parts) or "无数据"))

    role_parts = []
    for key, proto in roles.get("protocols", {}).items():
        if proto.get("roles"):
            joined = "; ".join(
                f"{r['role']}" + (f"({r['detail']})" if r.get("detail") else "")
                + (f"@{r['interface']}" if r.get("interface") else "")
                for r in proto["roles"][:6])
            role_parts.append(f"{proto['name']}: {joined}")
    if role_parts:
        lines.append("【协议角色】" + " | ".join(role_parts))

    intfs = cap_result.get("interfaces") or []
    if intfs:
        lines.append("【接口】" + "; ".join(
            f"{i['name']}" + (f"={i['ip']}" if i.get("ip") else "")
            for i in intfs[:12]))

    cfg_text = ((cap_result.get("raw_outputs") or {})
                .get("display current-configuration", "") or "")
    if cfg_text:
        limit = CONFIG_EXCERPT_FULL if config_full else CONFIG_EXCERPT_SHORT
        lines.append(f"【当前配置摘录】\n{cfg_text[:limit]}"
                     + ("\n（超长已截断）" if len(cfg_text) > limit else ""))

    errors = cap_result.get("errors") or []
    if errors:
        lines.append("【探测异常】" + "; ".join(errors[:5]))
    return "\n".join(lines)


def render_template_catalog() -> str:
    """列出系统可用的填空模板，供 AI 引导学生走填空配置。"""
    try:
        from .config_builder import CONFIG_TEMPLATES
    except Exception:
        return ""
    items = [f"- {key}: {tpl.get('name', key)}"
             for key, tpl in CONFIG_TEMPLATES.items()]
    return "【系统可用填空模板】（在建议中引用时于行尾标注 【模板:模板键】）\n" + "\n".join(items)


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------
_BASE_SYSTEM = """你是 eNSP 网络实训课堂里的 AI 助教，精通华为 VRP 系统与 eNSP 模拟器。

硬性事实（必须遵守）：
- eNSP 镜像很旧且功能是子集：S5700 是 V200R001C00（无 BGP/NAT/MPLS），AR 系列是 V200R003C00。设备不支持某命令会返回 "Error: Unrecognized command"。
- 学生设备的真实状态（能力/角色/配置）已在上下文中给出，回答必须与之一致，不要凭空假设。
- 命令语法用 VRP（system-view 视图结构），绝不要输出 Cisco IOS 语法。
- 你只能建议，不能执行：所有配置命令最终要由学生在系统中预览确认后下发。

输出格式要求：
- 用简体中文，简洁分点；命令放在 ```代码块``` 里，每行一条。
- 当给出配置建议且系统已有对应填空模板时，在该建议段末尾另起一行标注 【模板:模板键】（键名从可用模板清单中选），学生可一键进入填空表单。
- 涉及危险操作（删除配置、修改 VTY/AAA、erase）时必须显式警告。

"""

_TUTOR_RULES = """当前为【助教模式】：学生是初学者。
- 用引导式教学：先解释原理和思路，给出方向性提示，再让学生自己尝试；
  不要一次性给出完整可抄的配置。学生答对时给予确认。
- 可以用反问帮助学生定位问题（如"你的接口 IP 和邻居在同一网段吗？"）。
"""

_ENGINEER_RULES = """当前为【工程师模式】：
- 直接给出准确、完整、可执行的命令序列（含视图切换），并说明每步作用。
- 建议优先引用系统填空模板（标注 【模板:模板键】）让学生走预填表单。
"""

# 常见故障速查（注入诊断场景）
_TROUBLESHOT_HINTS = """常见故障速查：
- ping 不通：接口 IP/掩码、接口未 up（undo shutdown）、缺少路由、ACL 拦截、代理/TUN 劫持管理流量。
- SSH 连不上：未配 Vlanif/接口管理 IP、VTY 未放行、AAA local-user 缺失、stelnet server enable 未开、防火墙。
- OSPF 邻居卡 Init/2-Way：hello/dead 不一致、区域号不一致、network 反掩码错误、接口网络类型（DRothers 2-Way 属正常）。
- VRRP 不主备：VRID/虚 IP 不一致、优先级、抢占、接口 down。
- DHCP 不发地址：全局 dhcp enable 未开、ip pool 网段与网关不匹配、接口未 dhcp select interface/global。
- Telnet/SSH 认证失败：user-interface 认证模式（password/aaa）与 local-user 权限级（0-15，需 3 级）。
"""


def build_messages(message: str, history: List[dict], device_context: str = "",
                   task: str = "free", extra_context: str = "") -> List[dict]:
    """组装最终 messages：system + 历史 + 用户消息。"""
    cfg = load_config()
    mode_rule = _ENGINEER_RULES if cfg.get("mode") == "engineer" else _TUTOR_RULES
    system = _BASE_SYSTEM + mode_rule
    if task == "diagnose":
        system += "\n本次任务是【报错诊断】：结合设备上下文和报错原文，先给最可能的原因（按概率排序），再给排查步骤。" + _TROUBLESHOT_HINTS
    elif task == "explain_config":
        system += "\n本次任务是【配置讲解】：像老师一样逐段讲解学生设备的当前配置，说明每一段的作用和协议状态。"
    elif task == "explain_roles":
        system += "\n本次任务是【角色讲解】：解释这台设备在各协议中扮演的角色及其含义（如为什么是 Master、根桥意味着什么）。"
    elif task == "explain_capabilities":
        system += "\n本次任务是【能力解读】：解释这台设备为什么支持/不支持这些协议（结合镜像版本事实），以及各状态（运行中/已配置/支持）的区别。"
    elif task == "lab_hint":
        system += "\n本次任务是【实验指导】：根据实验目标、当前进度校验结果和学生问题，指出下一步该做什么、卡点在哪。只提示方向和关键命令，不直接给整段答案。"
    if device_context:
        system += "\n\n===== 学生设备实测上下文 =====\n" + device_context
    if extra_context:
        system += "\n\n" + extra_context

    msgs: List[dict] = [{"role": "system", "content": system}]
    for m in (history or [])[-MAX_HISTORY_MESSAGES:]:
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") and m.get("content"):
            msgs.append({"role": m["role"], "content": str(m["content"])[:4000]})
    msgs.append({"role": "user", "content": message})
    return msgs


# ---------------------------------------------------------------------------
# 三期：实验任务指导
# ---------------------------------------------------------------------------
LAB_EXPERIMENTS: Dict[str, Dict[str, Any]] = {
    "static_route": {
        "name": "静态路由打通跨网段",
        "goal": "为两台以上路由器配置静态路由，使不同网段可以互通。",
        "applicable_roles": ["router", "l3_switch"],
        "checks": [
            {"type": "config_keyword", "keyword": "ip route-static",
             "min_devices": 2, "desc": "至少两台设备配置了静态路由"},
        ],
        "hints": ["ip route-static 目标网段 反掩码 下一跳地址"],
    },
    "ospf_full": {
        "name": "OSPF 全网互通",
        "goal": "所有路由器启用 OSPF，邻居达到 Full，跨网段学习到路由。",
        "applicable_roles": ["router", "l3_switch"],
        "checks": [
            {"type": "capability", "key": "ospf", "state": "configured",
             "min_devices": 2, "desc": "至少两台设备已配置 OSPF"},
            {"type": "capability", "key": "ospf", "state": "running",
             "min_devices": 2, "desc": "至少两台设备出现 Full 邻居"},
        ],
        "hints": ["ospf 1 → area 0.0.0.0 → network 网段 反掩码"],
    },
    "vlan_trunk": {
        "name": "VLAN 划分与 Trunk 互联",
        "goal": "交换机上划分 VLAN，Access 接入终端，交换机间 Trunk 放行。",
        "applicable_roles": ["switch", "l3_switch"],
        "checks": [
            {"type": "capability", "key": "vlan", "state": "configured",
             "min_devices": 1, "desc": "交换机已创建 VLAN"},
            {"type": "config_keyword", "keyword": "port link-type access",
             "min_devices": 1, "desc": "存在 Access 接入口"},
            {"type": "config_keyword", "keyword": "port link-type trunk",
             "min_devices": 1, "desc": "存在 Trunk 互联口"},
        ],
        "hints": ["vlan batch 10 20 → 接口下 port link-type trunk / port trunk allow-pass vlan"],
    },
    "vlanif_dhcp": {
        "name": "Vlanif 网关 + DHCP 服务器",
        "goal": "三层交换机配置 Vlanif 网关，并开启 DHCP 地址池为终端发地址。",
        "applicable_roles": ["l3_switch", "router"],
        "checks": [
            {"type": "capability", "key": "dhcp", "state": "configured",
             "min_devices": 1, "desc": "DHCP 已配置"},
            {"type": "role", "key": "dhcp", "role_keyword": "Server",
             "min_devices": 1, "desc": "设备扮演 DHCP Server（有地址池）"},
        ],
        "hints": ["dhcp enable → ip pool → gateway-list / network → 接口 dhcp select global"],
    },
    "vrrp_gateway": {
        "name": "VRRP 网关冗余",
        "goal": "两台设备在同一网段配置 VRRP，出现 Master/Backup 冗余网关。",
        "applicable_roles": ["l3_switch", "router"],
        "checks": [
            {"type": "capability", "key": "vrrp", "state": "configured",
             "min_devices": 2, "desc": "至少两台设备配置了 VRRP"},
            {"type": "role", "key": "vrrp", "role_keyword": "Master",
             "min_devices": 1, "desc": "出现 Master"},
        ],
        "hints": ["接口下 vrrp vrid 10 virtual-ip x.x.x.254，优先级区分主备"],
    },
    "nat_internet": {
        "name": "NAT 出口上网",
        "goal": "出口路由器配置 Easy-IP/NAT，使内网地址转换后访问外网。",
        "applicable_roles": ["router"],
        "checks": [
            {"type": "config_keyword", "keyword": "nat outbound",
             "min_devices": 1, "desc": "配置了 nat outbound（Easy-IP/地址池）"},
        ],
        "hints": ["acl 2000 permit 内网段 → 出接口 nat outbound 2000"],
    },
    "acl_filter": {
        "name": "ACL 流量过滤",
        "goal": "配置 ACL 并在接口引用，限制特定网段访问。",
        "applicable_roles": ["router", "l3_switch", "switch"],
        "checks": [
            {"type": "capability", "key": "acl", "state": "configured",
             "min_devices": 1, "desc": "ACL 已配置"},
            {"type": "config_keyword", "keyword": "traffic-filter",
             "min_devices": 1, "desc": "接口下引用了 traffic-filter"},
        ],
        "hints": ["acl 3000 → rule permit/deny → 接口 traffic-filter inbound acl 3000"],
    },
    "gre_tunnel": {
        "name": "GRE 隧道互联",
        "goal": "两台路由器建立 GRE 隧道， Tunnel 接口互通。",
        "applicable_roles": ["router"],
        "checks": [
            {"type": "config_keyword", "keyword": "tunnel-protocol gre",
             "min_devices": 2, "desc": "两台设备创建 GRE Tunnel 接口"},
        ],
        "hints": ["interface Tunnel0/0/0 → tunnel-protocol gre → tunnel source/destination"],
    },
}


def _eval_check(check: dict, session_device_ips: List[str],
                cap_results: Dict[str, dict]) -> dict:
    """对单个校验项在会话设备上求值。"""
    passed_devices: List[str] = []
    required = int(check.get("min_devices", 1))
    for ip in session_device_ips:
        cap = cap_results.get(ip)
        if not cap:
            continue
        ok = False
        ctype = check.get("type")
        if ctype == "capability":
            entry = (cap.get("capabilities") or {}).get(check.get("key"), {})
            ok = bool(entry.get(check.get("state", "configured")))
        elif ctype == "config_keyword":
            cfg_text = ((cap.get("raw_outputs") or {})
                        .get("display current-configuration", "") or "")
            ok = check.get("keyword", "").lower() in cfg_text.lower()
        elif ctype == "role":
            roles = detect_protocol_roles(cap).get("protocols", {})
            proto = roles.get(check.get("key"), {})
            kw = (check.get("role_keyword") or "").lower()
            ok = any(kw in (r.get("role", "").lower())
                     for r in proto.get("roles", [])) if kw else bool(proto.get("roles"))
        if ok:
            passed_devices.append(ip)
    return {
        "desc": check.get("desc", ""),
        "type": check.get("type"),
        "required": required,
        "passed_devices": passed_devices,
        "passed": len(passed_devices) >= required,
    }


def evaluate_lab_progress(experiment_id: str, session_device_ips: List[str],
                          cap_results: Dict[str, dict]) -> dict:
    """评估实验进度（基于最近一次能力识别的缓存数据，零连接）。"""
    exp = LAB_EXPERIMENTS.get(experiment_id)
    if not exp:
        raise AIError(f"未知实验: {experiment_id}")
    checks = [_eval_check(c, session_device_ips, cap_results) for c in exp["checks"]]
    done = sum(1 for c in checks if c["passed"])
    return {
        "experiment_id": experiment_id,
        "experiment_name": exp["name"],
        "goal": exp["goal"],
        "progress": round(done * 100 / len(checks)) if checks else 0,
        "checks": checks,
        "checked_devices": list(session_device_ips),
        "evaluated_at": time.time(),
        "stale_hint": None if cap_results else "尚无能力识别数据，请先执行能力识别",
    }


def render_lab_context(progress: dict) -> str:
    """把实验进度渲染进提示词。"""
    lines = [f"【实验】{progress['experiment_name']} — 目标: {progress['goal']}",
             f"【进度】{progress['progress']}%"]
    for c in progress["checks"]:
        mark = "✓" if c["passed"] else "✗"
        devs = ",".join(c["passed_devices"]) or "无"
        lines.append(f"  {mark} {c['desc']}（{len(c['passed_devices'])}/{c['required']} 台通过: {devs}）")
    return "\n".join(lines)
