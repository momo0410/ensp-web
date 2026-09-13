"""路由模块（由 main.py 机械拆分，逻辑未改动）。"""
from fastapi import APIRouter

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
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
    """创建实验会话（进度校验基于能力识别缓存，零 SSH）。"""
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
# 设备备份 API
# ---------------------------------------------------------------------------

