"""
桌面窗口启动器（系统 WebView）。

把后端的 127.0.0.1 页面装进一个原生窗口，用户双击即用，不必打开浏览器。

与浏览器模式的分工（由 main.py 的命令行参数决定）：
- 默认：WebView 窗口（本模块）—— 日常使用
- `--web`：只跑 HTTP 服务，用浏览器访问 —— 调试用（有 DevTools / React DevTools）

WebView 内的调试：pywebview 支持 `debug=True`，会用 WebView2 的开发模式启动，
右键可打开"检查"（Inspect）。默认关闭，用 `--debug` 打开。
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from typing import Optional

from app.config import logger

# 后端服务监听的地址（WebView 只访问本机回环）
HOST = "127.0.0.1"
PORT = 8000

_started_server: Optional[object] = None


def _wait_for_port(host: str, port: int, timeout: float = 30.0) -> bool:
    """等后端端口真正可连（uvicorn 起来需要一点时间）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def run_desktop(app, debug: bool = False) -> int:
    """
    启动后端（后台线程）+ 打开 WebView 窗口；窗口关闭后进程退出。

    app: FastAPI 应用对象。
    debug: 传给 pywebview，启用 WebView2 的开发模式（可右键检查元素）。
    """
    try:
        import webview
    except ImportError:
        logger.error(
            "未安装 pywebview，无法启动桌面窗口。\n"
            "  安装：pip install pywebview\n"
            "  或改用浏览器模式：python main.py --web"
        )
        return 1

    import uvicorn

    url = f"http://{HOST}:{PORT}/"

    def _serve() -> None:
        uvicorn.run(app, host=HOST, port=PORT, log_level="info")

    server_thread = threading.Thread(target=_serve, daemon=True)
    server_thread.start()

    if not _wait_for_port(HOST, PORT):
        logger.error("后端未能在 30 秒内就绪，桌面窗口未启动")
        return 1

    logger.info("桌面窗口启动中：%s", url)

    window = webview.create_window(
        "eNSP 网络设备管理系统",
        url,
        width=1440,
        height=900,
        min_size=(1024, 640),
        # 用 WebView 自带的确认框会与页面的确认弹窗冲突，交给前端处理
        confirm_close=False,
    )
    # create_window 的返回值保留引用，避免被 GC 回收导致窗口关闭
    _ = window

    # 阻塞直到窗口关闭
    webview.start(debug=debug)

    logger.info("桌面窗口已关闭，进程退出")
    return 0


def run_browser(app) -> int:
    """浏览器模式：只跑 HTTP 服务（调试前端用）。"""
    import uvicorn

    logger.info("=" * 50)
    logger.info("eNSP Web 管理系统（浏览器模式）")
    logger.info("访问地址: http://%s:%s", HOST, PORT)
    logger.info("=" * 50)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
    return 0
