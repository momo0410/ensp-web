"""
test_access_guard.py - 访问令牌单元测试
"""

import sys
from pathlib import Path


from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from services import access_guard


def _make_client():
    app = FastAPI(dependencies=[Depends(access_guard.access_guard)])

    @app.get("/api/secret")
    async def secret():
        return {"ok": True}

    @app.get("/api/health")
    async def health():
        return {"ok": True}

    return TestClient(app)


class TestAccessGuard:
    def test_wrong_token_401(self):
        assert _make_client().get(
            "/api/secret", headers={"X-Access-Token": "wrong"}).status_code == 401

    def test_missing_token_401(self):
        assert _make_client().get("/api/secret").status_code == 401

    def test_correct_token_200(self):
        r = _make_client().get(
            "/api/secret", headers={"X-Access-Token": access_guard.ACCESS_TOKEN})
        assert r.status_code == 200
        assert r.json() == {"ok": True}

    def test_health_exempt(self):
        assert _make_client().get("/api/health").status_code == 200

    def test_disabled_flag_bypasses(self, monkeypatch):
        monkeypatch.setattr(access_guard, "ACCESS_ENABLED", False)
        assert _make_client().get("/api/secret").status_code == 200

    def test_generated_token_is_hex(self):
        assert len(access_guard.ACCESS_TOKEN) == 16
        int(access_guard.ACCESS_TOKEN, 16)  # 不抛错即为合法 hex
