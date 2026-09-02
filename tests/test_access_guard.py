"""
test_access_guard.py - 访问令牌单元测试
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from services import access_guard


@pytest.fixture
def client():
    app = FastAPI(dependencies=[])  # 手动挂 guard，模拟 main.py
    from fastapi import Depends

    app_with_guard = FastAPI(dependencies=[Depends(access_guard.access_guard)])

    @app_with_guard.get("/api/secret")
    async def secret():
        return {"ok": True}

    @app_with_guard.get("/api/health")
    async def health():
        return {"ok": True}

    return TestClient(app_with_guard)


class TestAccessGuard:
    def test_wrong_token_401(self, client):
        assert client.get("/api/secret", headers={"X-Access-Token": "wrong"}).status_code == 401

    def test_missing_token_401(self, client):
        assert client.get("/api/secret").status_code == 401

    def test_correct_token_200(self, client):
        r = client.get("/api/secret", headers={"X-Access-Token": access_guard.ACCESS_TOKEN})
        assert r.status_code == 200
        assert r.json() == {"ok": True}

    def test_health_exempt(self, client):
        assert client.get("/api/health").status_code == 200

    def test_disabled_flag_bypasses(self, monkeypatch):
        monkeypatch.setattr(access_guard, "ACCESS_ENABLED", False)
        assert client().get("/api/secret").status_code == 200

    def test_generated_token_is_hex(self):
        assert len(access_guard.ACCESS_TOKEN) == 16
        int(access_guard.ACCESS_TOKEN, 16)  # 不抛错即为合法 hex
