"""路由模块（由 main.py 机械拆分，逻辑未改动）。"""
from fastapi import APIRouter
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
import json
import time
import uuid

from app.config import logger
from app.state import state, _lab_sessions
from domain.device_store import load_devices
from services import ai_assistant
from services.ai_assistant import AIError
from services import state_persist

class AiPlanRequest(BaseModel):
    """让 AI 产出一份受约束的结构化 plan（需求⑦）。"""
    message: str = Field(..., description="给 AI 的指令，如「给这 3 台配 OSPF」")
    device_ip: Optional[str] = Field(default=None, description="注入哪台设备的上下文")
    reply: Optional[str] = Field(
        default=None, description="已有模型回复；给出则不再调用模型，直接校验（便于测试与重放）"
    )


router = APIRouter()

class AiConfigRequest(BaseModel):
    api_base: Optional[str] = None
    api_key: Optional[str] = None   # 传空表示保留原 Key
    model: Optional[str] = None
    mode: Optional[str] = None      # tutor / engineer
    temperature: Optional[float] = None


class AiChatRequest(BaseModel):
    message: str
    task: str = "free"              # free/explain_capabilities/explain_roles/explain_config/diagnose/lab_hint
    device_ip: Optional[str] = None
    history: List[dict] = []
    stream: bool = True


class AiDiagnoseRequest(BaseModel):
    ip: str
    error_text: str
    command_outputs: List[str] = []


class AiLabStartRequest(BaseModel):
    experiment_id: str
    device_ips: List[str] = []      # 空则取全部设备


class AiLabHintRequest(BaseModel):
    question: str = ""
    stream: bool = True


class OpenAIChatCompletionRequest(BaseModel):
    """标准 OpenAI Chat Completions 兼容请求体"""
    model: Optional[str] = None
    messages: List[Dict[str, Any]]
    stream: bool = False
    temperature: Optional[float] = None
    max_tokens: int = 2048
    api_base: Optional[str] = None
    api_key: Optional[str] = None
    device_ip: Optional[str] = None


def _device_context_for(ip: Optional[str], config_full: bool = False) -> str:
    """把指定设备的实测数据渲染成提示词上下文。"""
    if not ip:
        return ""
    device = next((d for d in load_devices() if d.get("ip") == ip), None)
    cap = state.get("capability_results", {}).get(ip)
    return ai_assistant.render_device_context(
        device or {"name": ip, "ip": ip}, cap, config_full=config_full)


def _sse(generator):
    """把 AI 流包装成 SSE 事件流。"""
    try:
        for delta in generator:
            yield f"data: {json.dumps({'delta': delta}, ensure_ascii=False)}\n\n"
        yield "data: {\"done\": true}\n\n"
    except AIError as e:
        yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
    except Exception as e:
        logger.exception("AI 流式输出异常")
        yield f"data: {json.dumps({'error': f'AI 输出中断: {e}'}, ensure_ascii=False)}\n\n"


@router.get("/api/ai/status")
async def ai_status():
    """AI 配置状态（Key 打码返回，不回传明文）。"""
    cfg = ai_assistant.load_config()
    return {
        "success": True,
        "configured": ai_assistant.is_configured(cfg),
        "api_base": cfg.get("api_base", ""),
        "model": cfg.get("model", ""),
        "mode": cfg.get("mode", "tutor"),
        "key_masked": ai_assistant.mask_key(cfg.get("api_key", "")),
    }


@router.post("/api/ai/config")
async def ai_update_config(req: AiConfigRequest):
    """保存 AI 配置。api_key 留空表示保留原值。"""
    try:
        cfg = ai_assistant.save_config(req.model_dump())
    except AIError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "success": True,
        "configured": ai_assistant.is_configured(cfg),
        "api_base": cfg.get("api_base", ""),
        "model": cfg.get("model", ""),
        "mode": cfg.get("mode", "tutor"),
        "key_masked": ai_assistant.mask_key(cfg.get("api_key", "")),
    }


@router.post("/api/ai/test")
async def ai_test_connection():
    """测试模型服务连通性（一次最小请求）。"""
    result = ai_assistant.test_connection()
    return {"success": result["success"], "message": result.get("message", result.get("error", ""))}


@router.post("/api/ai/chat")
async def ai_chat(req: AiChatRequest):
    """
    AI 对话。携带 device_ip 时自动注入该设备实测上下文；
    task 决定助教的角色侧重。stream=true 时返回 SSE 事件流。
    """
    device_context = _device_context_for(
        req.device_ip, config_full=(req.task == "explain_config"))
    if req.task == "free":
        catalog = ai_assistant.render_template_catalog()
        if catalog:
            device_context = (device_context + "\n\n" + catalog).strip()
    messages = ai_assistant.build_messages(
        req.message, req.history, device_context, req.task)

    if not req.stream:
        try:
            reply = ai_assistant.chat_once(messages)
        except AIError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"success": True, "reply": reply}

    return StreamingResponse(
        _sse(ai_assistant.chat_stream(messages)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _resolve_openai_cfg(req: OpenAIChatCompletionRequest, request: Request) -> Dict[str, Any]:
    """合并 ai_config.json 与请求体/Authorization 头中的模型配置。"""
    cfg = ai_assistant.load_config()
    if req.api_base and req.api_base.strip():
        cfg["api_base"] = req.api_base.strip()
    if req.model and req.model.strip():
        cfg["model"] = req.model.strip()
    if req.temperature is not None:
        cfg["temperature"] = float(req.temperature)

    if req.api_key and req.api_key.strip():
        cfg["api_key"] = req.api_key.strip()
    else:
        auth = request.headers.get("Authorization", "").strip()
        if auth.lower().startswith("bearer "):
            bearer_token = auth[7:].strip()
            if bearer_token and not cfg.get("api_key"):
                cfg["api_key"] = bearer_token
    return cfg


def _openai_sse(generator, completion_id: str, created: int, model_name: str):
    """包装为标准 OpenAI chat.completion.chunk SSE 流。"""
    first = True
    try:
        for delta in generator:
            delta_obj = {"role": "assistant", "content": delta} if first else {"content": delta}
            first = False
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model_name,
                "choices": [
                    {
                        "index": 0,
                        "delta": delta_obj,
                        "finish_reason": None,
                    }
                ],
            }
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        stop_chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_name,
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                }
            ],
        }
        yield f"data: {json.dumps(stop_chunk, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
    except AIError as e:
        err_payload = {"error": {"message": str(e), "type": "ai_error"}}
        yield f"data: {json.dumps(err_payload, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"


@router.post("/v1/chat/completions")
@router.post("/api/v1/chat/completions")
@router.post("/api/ai/v1/chat/completions")
async def openai_chat_completions(req: OpenAIChatCompletionRequest, request: Request):
    """
    标准 OpenAI Chat Completions 格式模型调用接口。
    支持传入 messages / model / stream / temperature / max_tokens，
    并可选通过 device_ip 自动注入指定 eNSP 设备的实测上下文。
    """
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages 不能为空")

    cfg = _resolve_openai_cfg(req, request)
    messages = [dict(m) for m in req.messages]

    if req.device_ip:
        dev_ctx = _device_context_for(req.device_ip, config_full=False)
        if dev_ctx:
            messages = [{"role": "system", "content": f"【当前设备上下文】\n{dev_ctx}"}] + messages

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    model_name = cfg.get("model") or req.model or "default"

    if req.stream:
        return StreamingResponse(
            _openai_sse(
                ai_assistant.chat_stream(messages, cfg=cfg, max_tokens=req.max_tokens),
                completion_id=completion_id,
                created=created,
                model_name=model_name,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        reply = ai_assistant.chat_once(messages, cfg=cfg, max_tokens=req.max_tokens)
    except AIError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model_name,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": reply,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


@router.post("/api/ai/diagnose")
async def ai_diagnose(req: AiDiagnoseRequest):
    """下发失败/连接问题的报错诊断（非流式，供结果区直调用）。"""
    device_context = _device_context_for(req.ip)
    error_text = req.error_text[:2500]
    if req.command_outputs:
        error_text += "\n执行回显:\n" + "\n".join(o[:500] for o in req.command_outputs[:8])
    messages = ai_assistant.build_messages(
        f"请诊断这次配置下发失败的原因并给排查步骤。报错信息：\n{error_text}",
        [], device_context, task="diagnose")
    try:
        reply = ai_assistant.chat_once(messages)
    except AIError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "reply": reply}


@router.get("/api/ai/lab/experiments")
async def ai_lab_experiments():
    """可选择的实验任务目录。"""
    return {
        "success": True,
        "experiments": [
            {"id": eid, "name": exp["name"], "goal": exp["goal"],
             "applicable_roles": exp["applicable_roles"],
             "check_count": len(exp["checks"])}
            for eid, exp in ai_assistant.LAB_EXPERIMENTS.items()
        ],
    }


@router.post("/api/ai/lab/start")
async def ai_lab_start(req: AiLabStartRequest):
    """创建实验会话（进度校验基于能力识别缓存，零连接）。"""
    if req.experiment_id not in ai_assistant.LAB_EXPERIMENTS:
        raise HTTPException(status_code=404, detail=f"未知实验: {req.experiment_id}")
    device_ips = req.device_ips or [d["ip"] for d in load_devices()]
    if not device_ips:
        raise HTTPException(status_code=400, detail="设备列表为空，请先添加设备")
    session_id = uuid.uuid4().hex
    _lab_sessions[session_id] = {
        "id": session_id,
        "experiment_id": req.experiment_id,
        "device_ips": device_ips,
        "created_at": time.time(),
    }
    state_persist.save_lab_sessions(_lab_sessions)
    progress = ai_assistant.evaluate_lab_progress(
        req.experiment_id, device_ips, state.get("capability_results", {}))
    return {"success": True, "session": _lab_sessions[session_id], "progress": progress}


@router.get("/api/ai/lab/{session_id}/progress")
async def ai_lab_progress(session_id: str):
    """重新校验实验进度。"""
    session = _lab_sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="实验会话不存在或已失效（重启服务后需重新开始）")
    progress = ai_assistant.evaluate_lab_progress(
        session["experiment_id"], session["device_ips"],
        state.get("capability_results", {}))
    return {"success": True, "progress": progress}


@router.post("/api/ai/lab/{session_id}/hint")
async def ai_lab_hint(session_id: str, req: AiLabHintRequest):
    """向 AI 助教要下一步实验提示（强制引导式教学）。"""
    session = _lab_sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="实验会话不存在或已失效（重启服务后需重新开始）")
    try:
        progress = ai_assistant.evaluate_lab_progress(
            session["experiment_id"], session["device_ips"],
            state.get("capability_results", {}))
    except AIError as e:
        raise HTTPException(status_code=404, detail=str(e))
    exp = ai_assistant.LAB_EXPERIMENTS[session["experiment_id"]]
    extra = (ai_assistant.render_lab_context(progress)
             + "\n【实验参考要点】" + "; ".join(exp.get("hints", [])))
    message = req.question.strip() or "请根据当前进度，指出我下一步该做什么、可能的卡点在哪里。"
    messages = ai_assistant.build_messages(message, [], "", task="lab_hint",
                                           extra_context=extra)
    # 实验指导强制引导模式，避免直接给整段答案
    saved_mode = ai_assistant.load_config().get("mode")
    if not req.stream:
        try:
            reply = ai_assistant.chat_once(messages)
        except AIError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"success": True, "reply": reply, "progress": progress}
    return StreamingResponse(
        _sse(ai_assistant.chat_stream(messages)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "X-Lab-Mode": saved_mode or "tutor"},
    )


# ---------------------------------------------------------------------------
# AI 结构化 plan（需求⑦：AI 快速配置设备 / 搭建拓扑）
#
# 安全红线：AI 绝不直接下发。它只能产出受 schema 约束的 JSON plan，
# 这里校验通过后交给前端预览，由人点确认后才走既有下发链路。
# ---------------------------------------------------------------------------
_PLAN_SYSTEM = """你是一个网络配置助手。根据用户要求和设备上下文，输出一份**JSON plan**，
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


@router.post("/api/ai/plan")
async def ai_create_plan(req: AiPlanRequest):
    """让 AI 产出结构化 plan 并校验；不执行任何下发。"""
    from services.ai_plan import PlanError, extract_plan, render_plan_summary, validate_plan

    devices = load_devices()
    known_ips = [d.get("ip") for d in devices if d.get("ip")]

    reply = req.reply
    if not reply:
        if not ai_assistant.is_configured():
            return {"success": False, "error": "AI 未配置（在 AI 助教设置里接入模型服务后才能用）"}
        context = ""
        if req.device_ip:
            dev = next((d for d in devices if d.get("ip") == req.device_ip), None)
            if dev:
                context = ai_assistant.render_device_context(dev, state.get("capability_results", {}).get(req.device_ip))
        messages = ai_assistant.build_messages(
            req.message, [], device_context=context, task="free",
            extra_context=_PLAN_SYSTEM,
        )
        try:
            reply = ai_assistant.chat_once(messages)
        except AIError as e:
            return {"success": False, "error": str(e)}

    plan, extract_error = extract_plan(reply)
    if plan is None:
        return {"success": False, "error": extract_error, "reply": reply}

    try:
        validated = validate_plan(plan, known_ips)
    except PlanError as e:
        # 校验失败要把原因回给用户/AI 让它改，而不是静默丢弃
        return {"success": False, "error": f"plan 校验未通过: {e}",
                "plan": plan, "reply": reply}

    return {
        "success": True,
        "kind": validated["kind"],
        "plan": validated,
        "summary": render_plan_summary(validated),
        "reply": reply,
        "note": "这是 AI 的建议方案，尚未下发。请核对后自行确认。",
    }


# ---------------------------------------------------------------------------
# AI 一键刷配置（生成 → 预览 → 确认 → 刷入 → 检测）
#
# 安全红线仍然成立：本组端点把「生成」和「刷入」拆成两次调用，
# /prepare 只生成待确认方案（含备份），/deploy 必须带 confirm=true 才刷。
# 确认闸门在服务端强制，前端无法绕过。
# ---------------------------------------------------------------------------

class AiOneClickPrepareRequest(BaseModel):
    """一键刷配置 · 第一步：生成待确认方案。"""
    instruction: str = Field(..., description="要下发的配置，如「三台路由器起 OSPF 区域 0」")
    device_ip: Optional[str] = Field(default=None, description="注入哪台设备的上下文")
    task_name: str = Field(default="", description="任务名")
    force: bool = Field(default=False, description="危险命令是否强制放行")


class AiOneClickDeployRequest(BaseModel):
    """一键刷配置 · 第二步：确认后刷入并检测。"""
    plan_id: str = Field(..., description="prepare 返回的 plan_id")
    confirm: bool = Field(default=False, description="必须显式确认才下发")


@router.post("/api/ai/oneclick/prepare")
async def ai_oneclick_prepare(req: AiOneClickPrepareRequest):
    """生成 + 预览 + 备份，返回**待确认**方案；不刷入。"""
    from services import ai_deploy_service

    devices = load_devices()
    result = ai_deploy_service.prepare(
        instruction=req.instruction,
        devices=devices,
        capability_results=state.get("capability_results", {}),
        device_ip=req.device_ip,
        task_name=req.task_name,
        force=req.force,
    )
    if not result.get("success"):
        return result

    plan_id = uuid.uuid4().hex
    state.setdefault("ai_oneclick_plans", {})[plan_id] = {
        "created_at": time.time(),
        "plan": result["plan"],
        "task": result["task"],
        "preview": result["preview"],
        "targets": result["targets"],
    }
    result["plan_id"] = plan_id
    return result


@router.post("/api/ai/oneclick/deploy")
async def ai_oneclick_deploy(req: AiOneClickDeployRequest):
    """确认后刷入，随后自动跑验证命令采集真实回显。"""
    from services import ai_deploy_service
    from services.free_config_executor import execute_free_deploy

    if not req.confirm:
        raise HTTPException(status_code=400, detail="请确认配置下发操作（confirm=true）")

    saved = state.get("ai_oneclick_plans", {}).get(req.plan_id)
    if not saved:
        raise HTTPException(status_code=404,
                            detail="方案不存在或服务已重启，请重新生成")
    if time.time() - saved.get("created_at", 0) > 900:
        state["ai_oneclick_plans"].pop(req.plan_id, None)
        raise HTTPException(status_code=410, detail="方案已超过 15 分钟，请重新生成")

    devices = load_devices()
    task = saved["task"]

    exec_result = execute_free_deploy(
        task=task,
        preview=saved["preview"],
        devices=devices,
        timeout=15,
        read_timeout=30,
    )

    verify_results = {}
    verify_summary = {}
    verify_commands = task.get("verify_commands") or []
    if verify_commands:
        verify_results = ai_deploy_service.verify_deployment(
            devices, saved["targets"], verify_commands)
        verify_summary = ai_deploy_service.summarize_verify(verify_results)

    state["ai_oneclick_plans"].pop(req.plan_id, None)

    return {
        "success": exec_result.get("success", False),
        "task_name": exec_result.get("task_name"),
        "summary": exec_result.get("summary"),
        "results": exec_result.get("results"),
        "verify": verify_results,
        "verify_summary": verify_summary,
        "verify_commands": verify_commands,
    }


# ---------------------------------------------------------------------------
# 设备备份 API
# ---------------------------------------------------------------------------

