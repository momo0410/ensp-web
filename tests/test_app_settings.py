"""services/app_settings.py 与 GET/POST /api/settings 的离线单测。

工作区隔离由 conftest.py 的 autouse fixture 负责，这里不重复。
"""
import pytest
from fastapi.testclient import TestClient

import main

from services.app_settings import SettingsError, list_settings, update_settings


def _client():
    return TestClient(main.app)


class TestList:
    def test_默认值(self):
        data = list_settings()
        assert data["success"] is True
        assert data["settings"]["auto_launch_ensp"] is True
        assert data["settings"]["auto_sync_ensp_topo"] is True
        assert data["settings"]["ensp_poll_interval_ms"] == 3000

    def test_schema_覆盖每个键(self):
        data = list_settings()
        keys = {item["key"] for item in data["schema"]}
        assert keys == set(data["settings"])

    def test_非法持久值退回默认(self, monkeypatch):
        from domain import workspace as ws
        ws.set_setting("ensp_poll_interval_ms", "abc")
        assert list_settings()["settings"]["ensp_poll_interval_ms"] == 3000


class TestUpdate:
    def test_布尔开关(self):
        res = update_settings({"auto_launch_ensp": False})
        assert res["settings"]["auto_launch_ensp"] is False
        assert res["updated"]["auto_launch_ensp"] is False

    def test_需重启的项被标记(self):
        res = update_settings({"auto_launch_ensp": False})
        assert res["need_restart"] == ["auto_launch_ensp"]
        assert "重启" in res["message"]

    def test_即时生效项不标记重启(self):
        res = update_settings({"auto_sync_ensp_topo": False})
        assert res["need_restart"] == []
        assert res["message"] == "设置已保存"

    def test_只更新传入的键(self):
        update_settings({"auto_launch_ensp": False})
        res = update_settings({"ensp_poll_interval_ms": 5000})
        assert res["settings"]["ensp_poll_interval_ms"] == 5000
        assert res["settings"]["auto_launch_ensp"] is False

    def test_字符串布尔(self):
        assert update_settings({"auto_launch_ensp": "false"})["settings"]["auto_launch_ensp"] is False
        assert update_settings({"auto_launch_ensp": "on"})["settings"]["auto_launch_ensp"] is True

    def test_整数越界(self):
        with pytest.raises(SettingsError):
            update_settings({"ensp_poll_interval_ms": 999999})
        with pytest.raises(SettingsError):
            update_settings({"ensp_poll_interval_ms": 100})

    def test_未知键(self):
        with pytest.raises(SettingsError):
            update_settings({"不存在的开关": True})

    def test_空补丁(self):
        with pytest.raises(SettingsError):
            update_settings({})


class TestHttp:
    def test_get(self):
        resp = _client().get("/api/settings")
        assert resp.status_code == 200
        assert resp.json()["settings"]["auto_launch_ensp"] is True

    def test_post(self):
        resp = _client().post("/api/settings", json={"auto_sync_ensp_topo": False})
        assert resp.status_code == 200
        assert resp.json()["settings"]["auto_sync_ensp_topo"] is False

    def test_post_非法键不崩(self):
        resp = _client().post("/api/settings", json={"没有这项": 1})
        assert resp.status_code == 200
        assert resp.json()["success"] is False
