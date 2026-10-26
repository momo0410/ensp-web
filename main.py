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
    STATIC_DIR,
    logger,
)
from app.instance_lock import (
    AlreadyRunningError,
    acquire_single_instance_lock,
    release_single_instance_lock,
)
from app.state import _lab_sessions, state
from domain.device_store import load_devices
from services import state_persist

app = FastAPI(title="eNSP Web 管理系统", version=APP_VERSION)

# 前端由本进程同源托管（app/desktop.py:PORT=8000），开发期走 vite 代理（8160），
# 因此只放行本机来源即可，无需通配来源。
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:8000",
        "http://localhost:8000",
        "http://127.0.0.1:8160",
        "http://localhost:8160",
    ],
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
from routes_agent import router as agent_router
from routes_config import router as config_router
from routes_scripts import router as scripts_router

app.include_router(system_router)
app.include_router(devices_router)
app.include_router(capabilities_router)
app.include_router(ai_router)
app.include_router(agent_router)
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
    """服务器启动时先抢单实例锁，再做自动初始化。"""
    try:
        acquire_single_instance_lock()
    except AlreadyRunningError as e:
        logger.error("启动中止：%s", e)
        # 直接让进程退出，避免两个实例并发写同一批数据
        import os as _os
        _os._exit(2)
    logger.info("服务器启动，开始自动初始化...")
    cached_caps = state_persist.load_capability_cache()
    if cached_caps:
        state.setdefault("capability_results", {}).update(cached_caps)
        logger.info("已回灌 %d 台设备的能力识别缓存", len(cached_caps))
    _lab_sessions.update(state_persist.load_lab_sessions())
    if _lab_sessions:
        logger.info("已恢复 %d 个实验会话", len(_lab_sessions))
    # 采集结果是协议角色/冲突检测的数据源；以前只在内存里，重启即丢，
    # 导致协议角色页退化成一堆空壳。这里回灌落盘的那份。
    cached_collections = state_persist.load_collection_cache()
    if cached_collections:
        state.setdefault("collection_results", {}).update(cached_collections)
        logger.info("已回灌 %d 台设备的采集结果", len(cached_collections))
    import threading
    thread = threading.Thread(target=_auto_init, daemon=True)
    thread.start()
    # 后台静默补齐缺失的采集：不阻塞启动，失败只记日志
    thread2 = threading.Thread(target=_silent_collect_missing, daemon=True)
    thread2.start()


@app.on_event("shutdown")
async def shutdown_event():
    """退出时先清理 Agent sidecar 子进程，再释放单实例锁。

    两步各自兜异常：sidecar 清理失败不能连带把锁留在文件系统里。
    """
    try:
        from services.agent_sidecar import sidecar
        sidecar.stop()
    except Exception as e:
        logger.warning("清理 Agent sidecar 失败: %s", e)
    try:
        release_single_instance_lock()
    except Exception as e:
        logger.warning("释放单实例锁失败: %s", e)


def _silent_collect_missing() -> None:
    """
    启动后静默采集缺数据的设备（协议角色/冲突检测都依赖采集结果）。

    只采"设备表里有、但采集结果里没有或探测失败"的，已缓存的不重复采。

    两个压制前台延迟的设计：
    - **小批次**：每批最多 3 台，避免一次占满 15 个 Console 连接把线程池耗尽；
    - **批次间让出**：每批之后 sleep 一段时间，且在 sleep 前后主动释放 GIL，
      让 asyncio 事件循环能处理排队的 HTTP 请求。
      实测不这样做时，静默采集会把 /api/health 从 1.5ms 拖到 8s、
      /api/protocol-roles-all 从 15ms 拖到 17s。
    """
    import time
    time.sleep(20)  # 让 eNSP 注入/设备开机先跑一会，避免一启动就全失败
    try:
        devices = load_devices()
    except Exception as e:
        logger.warning("静默采集：读取设备表失败: %s", e)
        return

    from domain.collect import collect_device as _collect
    from app.state import record_collection

    existing = state.get("collection_results", {})
    targets = [d for d in devices
               if d.get("ip") and d.get("console_port")
               and not (existing.get(d["ip"]) or {}).get("ok")]
    if not targets:
        return

    logger.info("静默采集 %d 台缺数据的设备...", len(targets))
    ok = 0
    batch_size = 3
    for start in range(0, len(targets), batch_size):
        batch = targets[start:start + batch_size]
        for dev in batch:
            try:
                result = _collect(dev)
                if result:
                    record_collection(dev["ip"], result)
                    if result.get("ok"):
                        ok += 1
            except Exception as e:
                logger.warning("静默采集 %s 失败: %s", dev.get("ip"), e)
        # 让出 CPU 与 Console 通道，前台请求不至于排在本批后面干等
        time.sleep(1.5)
    logger.info("静默采集完成：%d/%d 台成功", ok, len(targets))


def _auto_launch_ensp() -> None:
    """后台拉起 eNSP 并注入控制模块。

    失败只记录日志：eNSP 自动联动是增强能力，不该拖垮服务启动。
    """
    try:
        from domain.ensp_runtime import ensure_ensp_injected
    except Exception as e:
        logger.warning("eNSP 运行时模块不可用，跳过自动拉起: %s", e)
        return
    try:
        res = ensure_ensp_injected(launch_if_missing=True)
        if res.get("ok"):
            logger.info("eNSP 已就绪（pid=%s）", res.get("pid"))
        else:
            logger.warning("eNSP 自动拉起/注入失败: %s", res.get("error"))
    except Exception as e:
        logger.warning("eNSP 自动拉起异常: %s", e)


if __name__ == "__main__":
    import argparse

    from app import desktop

    parser = argparse.ArgumentParser(description="eNSP Web 管理系统")
    parser.add_argument(
        "--web", action="store_true",
        help="浏览器模式：只跑 HTTP 服务，用浏览器打开（调试前端用，有 DevTools）",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="桌面窗口调试模式：启用 WebView2 开发模式，可右键检查元素",
    )
    parser.add_argument(
        "--no-elevate", action="store_true",
        help="不请求管理员权限（eNSP 若以管理员运行，自动注入/开机将不可用）",
    )
    parser.add_argument(
        "--no-ensp", action="store_true",
        help="启动时不自动拉起 eNSP",
    )
    args = parser.parse_args()

    # 提权必须在做任何事之前完成：eNSP 常以管理员权限运行，
    # 非管理员进程无法向其注入控制模块（OpenProcess 会返回拒绝访问）。
    from services.elevation import ensure_admin, is_admin
    if not args.no_elevate and not ensure_admin():
        logger.warning(
            "未获得管理员权限（可能是在 UAC 弹窗点了「否」）。"
            "eNSP 若同样以管理员运行，自动注入与自动开机将不可用，其余功能不受影响。"
        )

    logger.info("=" * 50)
    logger.info("eNSP Web 管理系统 启动中...")
    logger.info("数据文件: %s", DEVICES_FILE)
    logger.info("管理员权限: %s", "是" if is_admin() else "否")
    logger.info("=" * 50)

    # 启动即拉起 eNSP（可通过设置项 auto_launch_ensp 或 --no-ensp 关闭）
    if not args.no_ensp:
        from domain import workspace as _ws
        if _ws.get_setting("auto_launch_ensp", True):
            threading.Thread(target=_auto_launch_ensp, daemon=True).start()

    # 反向联动看护：eNSP 随后被用户启动时，自动把控制模块注入进去。
    # 受设置项 watch_ensp_topology 控制；--no-ensp 时也不需要看护。
    if not args.no_ensp:
        from domain.ensp_guard import ensp_guard
        ensp_guard.start()

    if args.web:
        raise SystemExit(desktop.run_browser(app))
    raise SystemExit(desktop.run_desktop(app, debug=args.debug))
