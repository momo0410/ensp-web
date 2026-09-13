from fastapi import Depends
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import threading

from app.config import (
    APP_VERSION,
    DEVICES_FILE,
    FRONTEND_DIST,
    HUAWEI_COMMANDS,  # noqa: F401  # 采集命令常量，供测试与外部引用
    HUAWEI_COMMANDS_FAST,  # noqa: F401
    PARALLEL_CMD_GROUP_B,  # noqa: F401
    PARALLEL_CMD_GROUP_B_FAST,  # noqa: F401
    STATIC_DIR,
    logger,
)
from app.state import _lab_sessions, state
from services.access_guard import ACCESS_ENABLED, ACCESS_TOKEN, access_guard
from services import state_persist

app = FastAPI(title="eNSP Web 管理系统", version="2.0.0",
              dependencies=[Depends(access_guard)])

# 允许从本地 HTML 直接打开页面时访问后端接口。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载静态文件
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# 新版前端构建产物（frontend/dist 由 Vite 生成，未构建时自动跳过）
if (FRONTEND_DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="frontend_assets")


# 按功能域挂载路由（原 main.py 的路由实现已机械拆分到 routes_*.py / core.py）
from routes_system import router as system_router
from routes_devices import router as devices_router
from routes_capabilities import router as capabilities_router
from routes_ai import router as ai_router
from routes_config import router as config_router
from routes_scripts import router as scripts_router

app.include_router(system_router)
app.include_router(devices_router)
app.include_router(capabilities_router)
app.include_router(ai_router)
app.include_router(config_router)
app.include_router(scripts_router)

@app.get("/")
async def index():
    """返回前端页面（优先新版 React 前端，未构建时回退旧版）。"""
    new_index = FRONTEND_DIST / "index.html"
    if new_index.exists():
        return FileResponse(str(new_index))
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/legacy")
async def legacy_index():
    """旧版（原生 JS）前端入口。"""
    return FileResponse(str(STATIC_DIR / "index.html"))




from routes_scripts import _auto_init  # noqa: E402

@app.on_event("startup")
async def startup_event():
    """服务器启动时自动初始化"""
    logger.info("服务器启动，开始自动初始化...")
    cached_caps = state_persist.load_capability_cache()
    if cached_caps:
        state.setdefault("capability_results", {}).update(cached_caps)
        logger.info("已回灌 %d 台设备的能力识别缓存", len(cached_caps))
    _lab_sessions.update(state_persist.load_lab_sessions())
    if _lab_sessions:
        logger.info("已恢复 %d 个实验会话", len(_lab_sessions))
    if ACCESS_ENABLED:
        logger.info(
            "访问令牌已启用: %s （客户端首次打开页面时需输入；"
            "可通过 web_access.json 修改或禁用，环境变量 WEB_ACCESS_TOKEN 优先）",
            ACCESS_TOKEN)
    else:
        logger.warning(
            "访问令牌未启用——任何能访问本端口的人都可以控制设备；"
            "仅建议纯本机回环使用")
    import threading
    thread = threading.Thread(target=_auto_init, daemon=True)
    thread.start()


if __name__ == "__main__":
    import uvicorn
    logger.info("=" * 50)
    logger.info("eNSP Web 管理系统 启动中...")
    logger.info(f"数据文件: {DEVICES_FILE}")
    logger.info("访问地址: http://127.0.0.1:8000")
    logger.info("=" * 50)
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
