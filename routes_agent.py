"""
routes_agent.py - Agent 的 HTTP 层（pi sidecar 版）
==================================================
Agent 循环跑在 Node 的 pi-agent-core 上（见 `agent_node/`），本层负责：

    GET  /api/agent/tools                     工具清单（能力透明展示）
    GET  /api/agent/status                    sidecar 与依赖健康状况
    POST /api/agent/chat                      跑一轮 Agent，SSE 透传事件
    POST /api/agent/chat/sync                 同上，非流式（脚本与测试用）

    ——— 以下两条仅供 Node sidecar 回调，需 X-Agent-Token ———
    GET  /api/agent/internal/tools            工具 schema 清单
    POST /api/agent/internal/tools/execute    执行一次工具

安全模型（AGENTS.md 第六节，未因换引擎而放松）：
- 工具清单与执行**只在 Python 定义**。sidecar 不硬编码任何工具，
  因此「Agent 没有任何直接下发的工具」这件事仍然是一处定义、全局成立。
- 真正下发仍必须走既有确认闸门 `/api/ai/oneclick/deploy`（需 confirm=true）。
- 内部端点校验共享密钥；密钥只存内存与子进程环境，不落盘、不入日志。
"""

from __future__ import annotations

import json
import time
import uuid

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional

from app.config import logger
from app.state import state
from domain.device_store import load_devices
from services import agent_sidecar, ai_assistant
from services.agent_tools import (
    AgentContext,
    call_tool,
    describe_tools,
    sidecar_schemas,
)

router = APIRouter()

PROPOSAL_TTL_SECONDS = 900


class AgentChatRequest(BaseModel):
    message: str = Field(..., description="用户要求，如「R1 为什么 ping 不通 R2」")
    device_ip: Optional[str] = Field(
        default=None, description="可选：注入哪台设备的实测上下文作为起点")
    history: List[dict] = Field(default_factory=list, description="历史对话")
    mode: Optional[str] = Field(
        default=None, description="tutor / engineer；缺省用 ai_config.json 里的设置")
    allow_device_io: bool = Field(
        default=True, description="是否允许 Agent 连设备（ping/采集/能力识别）")
    max_steps: int = Field(default=8, ge=1, le=16, description="保留字段（步数由引擎控制）")


class ToolExecuteRequest(BaseModel):
    name: str
    args: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 工具执行（Python 是真源）
# ---------------------------------------------------------------------------
def _register_proposal(payload: Dict[str, Any]) -> str:
    """把 Agent 产出的待确认方案登记进既有确认闸门所用的同一份存储。"""
    plan_id = uuid.uuid4().hex
    state.setdefault("ai_oneclick_plans", {})[plan_id] = {
        "created_at": time.time(),
        "plan": payload.get("plan"),
        "task": payload.get("task"),
        "preview": payload.get("preview"),
        "targets": payload.get("targets") or [],
    }
    return plan_id


def _build_context(allow_device_io: bool) -> AgentContext:
    return AgentContext(
        devices=load_devices(),
        capability_results=state.get("capability_results", {}),
        collection_results=state.get("collection_results", {}),
        allow_device_io=allow_device_io,
        register_proposal=_register_proposal,
    )


# allow_device_io 由 /chat 决定；内部端点沿用最近一次的设定（默认 True）
_INTERNAL_CTX_FLAG = {"allow_device_io": True}


def _check_internal_token(request: Request) -> None:
    got = request.headers.get("X-Agent-Token", "")
    expected = agent_sidecar.sidecar.token
    # 常量时间比较，避免时序侧信道
    import hmac
    if not hmac.compare_digest(got, expected):
        raise HTTPException(status_code=401, detail="内部端点令牌不匹配")


@router.get("/api/agent/internal/tools")
async def agent_internal_tools(request: Request):
    """供 sidecar 拉取工具清单（含 kind）。"""
    _check_internal_token(request)
    return {"tools": sidecar_schemas()}


@router.post("/api/agent/internal/tools/execute")
def agent_internal_execute(req: ToolExecuteRequest, request: Request):
    """供 sidecar 执行一次工具。任何异常都转成结构化失败，不抛 500。

    刻意用同步 def：工具会连设备（阻塞 socket），FastAPI 会把同步端点丢进
    线程池执行，不会卡住事件循环。若写成 async def，工具一阻塞就会把
    sidecar 的回调也一起卡死——那是死锁。
    """
    _check_internal_token(request)
    ctx = _build_context(_INTERNAL_CTX_FLAG["allow_device_io"])
    result = call_tool(req.name, req.args, ctx)
    return result


# ---------------------------------------------------------------------------
# 对外：工具清单与状态
# ---------------------------------------------------------------------------
@router.get("/api/agent/tools")
async def agent_list_tools():
    """列出 Agent 可用的工具。kind=read 只读观测；kind=prepare 只产出待确认方案。"""
    return {
        "success": True,
        "engine": "pi-agent-core (Node sidecar)",
        "tools": describe_tools(),
        "note": ("Agent 没有任何直接下发配置的工具：最重的动作是 propose_config，"
                 "它只产出待确认方案，实际下发仍需在界面上点确认。"),
    }


@router.get("/api/agent/status")
async def agent_status():
    """sidecar 与依赖健康状况（便于排查 node/依赖缺失）。"""
    return {"success": True, "sidecar": agent_sidecar.sidecar.status()}


# ---------------------------------------------------------------------------
# 对外：跑 Agent
# ---------------------------------------------------------------------------
def _resolve_mode(req: AgentChatRequest) -> str:
    return req.mode or ai_assistant.load_config().get("mode") or "tutor"


def _device_context(req: AgentChatRequest) -> str:
    if not req.device_ip:
        return ""
    devices = load_devices()
    dev = next((d for d in devices if d.get("ip") == req.device_ip), None)
    if not dev:
        return ""
    return ai_assistant.render_device_context(
        dev, state.get("capability_results", {}).get(req.device_ip))


def _payload(req: AgentChatRequest, mode: str) -> Dict[str, Any]:
    cfg = ai_assistant.load_config()
    return {
        "message": req.message,
        "history": req.history,
        "mode": mode,
        "device_context": _device_context(req),
        "cfg": {
            "api_base": cfg.get("api_base", ""),
            "api_key": cfg.get("api_key", ""),
            "model": cfg.get("model", ""),
        },
    }


def _ensure_sidecar() -> str:
    """确保 pi sidecar 在跑；返回其 base url。

    sidecar 不可用（机器上没装 Node、或 pi 依赖未安装）时抛 503 并给出
    可操作的原因——不做静默降级，避免用户以为在用 Agent 而实际用的是别的引擎。
    """
    if not ai_assistant.is_configured():
        raise HTTPException(status_code=400,
                            detail="AI 未配置：请先在设置里填写 API 地址、Key 和模型名")
    base = agent_sidecar.sidecar.base_url() if agent_sidecar.sidecar.is_running() else None
    if base:
        return base
    try:
        agent_sidecar.sidecar.ensure_started()
    except agent_sidecar.SidecarError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    base = agent_sidecar.sidecar.base_url()
    if not base:
        raise HTTPException(status_code=503, detail="Agent sidecar 启动后未就绪")
    return base


@router.post("/api/agent/chat")
async def agent_chat(req: AgentChatRequest):
    """跑一轮 Agent（pi 引擎），SSE 透传事件。"""
    base = _ensure_sidecar()
    mode = _resolve_mode(req)
    _INTERNAL_CTX_FLAG["allow_device_io"] = bool(req.allow_device_io)

    body = _payload(req, mode)
    token = agent_sidecar.sidecar.token

    def _proxy():
        try:
            with httpx.stream(
                "POST", f"{base}/run",
                json=body,
                headers={"X-Agent-Token": token},
                timeout=httpx.Timeout(600.0, connect=15.0),
            ) as resp:
                if resp.status_code != 200:
                    detail = resp.read().decode("utf-8", errors="replace")[:500]
                    yield f"data: {json.dumps({'type': 'error', 'error': f'sidecar HTTP {resp.status_code}: {detail}'}, ensure_ascii=False)}\n\n"
                    return
                for chunk in resp.iter_raw():
                    if chunk:
                        yield chunk
        except Exception as e:
            logger.exception("Agent sidecar 流式代理异常")
            yield f"data: {json.dumps({'type': 'error', 'error': f'Agent 执行中断: {e}'}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done', 'failed': True}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        _proxy(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/agent/chat/sync")
def agent_chat_sync(req: AgentChatRequest):
    """非流式：把 sidecar 的 SSE 收全后一次性返回。便于脚本与测试。

    刻意用同步 def（跑在线程池）而非 async def：本函数会**阻塞等待** sidecar
    把整轮跑完，而 sidecar 中途还要回调本进程执行工具。若占着事件循环等待，
    回调就永远进不来 —— 死锁。
    """
    base = _ensure_sidecar()
    mode = _resolve_mode(req)
    _INTERNAL_CTX_FLAG["allow_device_io"] = bool(req.allow_device_io)
    started = time.time()

    body = _payload(req, mode)
    token = agent_sidecar.sidecar.token

    events: List[Dict[str, Any]] = []
    answer = ""
    tool_calls: List[Dict[str, Any]] = []
    proposals: List[Dict[str, Any]] = []
    error: Optional[str] = None

    try:
        with httpx.stream(
            "POST", f"{base}/run",
            json=body,
            headers={"X-Agent-Token": token},
            timeout=httpx.Timeout(600.0, connect=15.0),
        ) as resp:
            if resp.status_code != 200:
                detail = resp.read().decode("utf-8", errors="replace")[:500]
                raise HTTPException(status_code=502,
                                    detail=f"sidecar HTTP {resp.status_code}: {detail}")
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except ValueError:
                    continue
                events.append(ev)
                t = ev.get("type")
                if t == "tool_call":
                    tool_calls.append({"tool": ev.get("tool"), "args": ev.get("args") or {}})
                elif t == "answer":
                    answer = ev.get("answer") or answer
                elif t == "error":
                    error = ev.get("error") or error
                elif t == "tool_result":
                    result = ev.get("result") or {}
                    if ev.get("tool") == "propose_config" and isinstance(result, dict) and result.get("plan_id"):
                        proposals.append({
                            "plan_id": result.get("plan_id"),
                            "targets": result.get("targets") or [],
                            "summary": result.get("summary") or {},
                        })
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Agent sidecar 非流式代理异常")
        raise HTTPException(status_code=502, detail=f"Agent 执行失败: {e}") from e

    return {
        "success": error is None and bool(answer),
        "engine": "pi-agent-core",
        "mode": mode,
        "answer": answer,
        "tool_calls": tool_calls,
        "proposals": proposals,
        "error": error,
        "events": events,
        "elapsed_ms": int((time.time() - started) * 1000),
    }
