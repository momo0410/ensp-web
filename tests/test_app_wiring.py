"""main.py 装配一致性：版本号、shutdown 处理器、CORS 来源白名单。

背景：main.py 曾有两个同名的 `@app.on_event("shutdown")` 处理器，
后定义的把先定义的覆盖掉，导致 Agent sidecar 清理或单实例锁释放只有一处生效。
"""

import asyncio
import sys
from pathlib import Path

from fastapi.middleware.cors import CORSMiddleware

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _cors_kwargs() -> dict:
    import main

    for middleware in main.app.user_middleware:
        if getattr(middleware, "cls", None) is CORSMiddleware:
            return middleware.kwargs
    raise AssertionError("未挂载 CORSMiddleware")


def test_app_version_取自_app_config():
    """FastAPI 的 version 必须来自 APP_VERSION 单一来源，不得硬编码。"""
    import main
    from app.config import APP_VERSION

    assert main.app.version == APP_VERSION


def test_shutdown_处理器只有一个():
    """同名处理器会被后者覆盖，所以必须只注册一个。"""
    import main

    assert len(main.app.router.on_shutdown) == 1


def test_shutdown_先停_sidecar_再释放锁(monkeypatch):
    import main
    from services import agent_sidecar

    calls = []
    monkeypatch.setattr(agent_sidecar.sidecar, "stop", lambda: calls.append("sidecar"))
    monkeypatch.setattr(main, "release_single_instance_lock", lambda: calls.append("lock"))

    asyncio.run(main.app.router.on_shutdown[0]())
    assert calls == ["sidecar", "lock"]


def test_sidecar_清理失败不阻止释放锁(monkeypatch):
    import main
    from services import agent_sidecar

    released = []

    def _boom():
        raise RuntimeError("sidecar 清理炸了")

    monkeypatch.setattr(agent_sidecar.sidecar, "stop", _boom)
    monkeypatch.setattr(main, "release_single_instance_lock", lambda: released.append(True))

    asyncio.run(main.app.router.on_shutdown[0]())
    assert released == [True]


def test_释放锁失败不抛出(monkeypatch):
    import main
    from services import agent_sidecar

    stopped = []
    monkeypatch.setattr(agent_sidecar.sidecar, "stop", lambda: stopped.append(True))

    def _boom():
        raise RuntimeError("释放锁炸了")

    monkeypatch.setattr(main, "release_single_instance_lock", _boom)

    asyncio.run(main.app.router.on_shutdown[0]())
    assert stopped == [True]


def test_cors_不放通配来源():
    origins = _cors_kwargs()["allow_origins"]
    assert "*" not in origins


def test_cors_放行本机来源():
    origins = _cors_kwargs()["allow_origins"]
    assert "http://127.0.0.1:8000" in origins
    assert "http://localhost:8000" in origins
    assert "http://127.0.0.1:8160" in origins
    assert "http://localhost:8160" in origins


def test_cors_方法与头部仍全放行():
    kwargs = _cors_kwargs()
    assert kwargs["allow_methods"] == ["*"]
    assert kwargs["allow_headers"] == ["*"]
