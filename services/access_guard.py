"""
access_guard.py - Web 访问令牌
==============================
系统控制的是真实/模拟网络设备，任何能打开页面的人都能下发配置，
因此默认启用访问令牌：所有 /api/* 请求必须携带 X-Access-Token 头，
静态页面本身不设防（页面加载后前端会弹出令牌输入框）。

令牌来源（优先级从高到低）：
1. 环境变量 WEB_ACCESS_TOKEN（设为 disabled 表示关闭校验）
2. 项目根目录 web_access.json（{"token": "...", "enabled": true}）
3. 首次启动自动生成随机令牌并写入 web_access.json，同时在服务日志打印

把 web_access.json 的 enabled 改为 false 或环境变量设为 disabled 可关闭
校验——仅建议在纯本机回环使用时这么做。
"""

import os
import json
import hmac
import secrets
import logging

from fastapi import HTTPException, Request

logger = logging.getLogger("ensp-web.access")

TOKEN_FILE = "web_access.json"

# 静态页面/健康检查不设防；其余全部要求令牌
EXEMPT_PATHS = {"/", "/legacy", "/api/health"}


def _load_or_create() -> dict:
    env = os.environ.get("WEB_ACCESS_TOKEN", "").strip()
    if env:
        return {"token": env, "enabled": env.lower() != "disabled"}
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("token"):
            return data
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("读取 %s 失败: %s", TOKEN_FILE, e)

    data = {"token": secrets.token_hex(8), "enabled": True}
    try:
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info("已生成访问令牌并写入 %s", TOKEN_FILE)
    except OSError as e:
        logger.warning("无法写入 %s: %s", TOKEN_FILE, e)
    return data


_WEB_ACCESS = _load_or_create()
ACCESS_TOKEN = str(_WEB_ACCESS.get("token", ""))
ACCESS_ENABLED = bool(_WEB_ACCESS.get("enabled", True)) and bool(ACCESS_TOKEN)


async def access_guard(request: Request):
    """FastAPI 全局依赖：校验 X-Access-Token。

    注意：FastAPI 依赖返回 Response 不会短路请求，必须抛 HTTPException。
    """
    if not ACCESS_ENABLED:
        return
    if request.url.path in EXEMPT_PATHS:
        return
    supplied = request.headers.get("X-Access-Token", "")
    if supplied and hmac.compare_digest(supplied, ACCESS_TOKEN):
        return
    raise HTTPException(
        status_code=401,
        detail="需要访问令牌：请在页面弹窗中输入服务端打印的令牌",
    )
