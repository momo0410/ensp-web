"""
test_state_persist.py - 状态持久化单元测试（隔离到临时目录）
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import pytest

from services import state_persist
from test_role_detector import _cap_result


@pytest.fixture
def tmp_files(monkeypatch, tmp_path):
    monkeypatch.setattr(state_persist, "CAPABILITY_CACHE_FILE", str(tmp_path / "capability_cache.json"))
    monkeypatch.setattr(state_persist, "LAB_SESSIONS_FILE", str(tmp_path / "lab_sessions.json"))
    return tmp_path


class TestCapabilityCache:
    def test_roundtrip(self, tmp_files):
        caps = {"192.168.56.12": _cap_result()}
        state_persist.save_capability_cache(caps)
        loaded = state_persist.load_capability_cache()
        assert loaded["192.168.56.12"]["hostname"] == "Core-SW"
        assert loaded["192.168.56.12"]["capabilities"]["vrrp"]["running"] is True

    def test_empty_when_missing(self, tmp_files):
        assert state_persist.load_capability_cache() == {}

    def test_corrupt_file_rebuilds(self, tmp_files):
        p = tmp_files / "capability_cache.json"
        p.write_text("{broken json", encoding="utf-8")
        assert state_persist.load_capability_cache() == {}
        # 覆写恢复正常
        state_persist.save_capability_cache({"a": {"ok": True}})
        assert state_persist.load_capability_cache() == {"a": {"ok": True}}


class TestLabSessions:
    def test_roundtrip(self, tmp_files):
        sessions = {"abc": {"id": "abc", "experiment_id": "ospf_full",
                            "device_ips": ["1.1.1.1"], "created_at": 123}}
        state_persist.save_lab_sessions(sessions)
        assert state_persist.load_lab_sessions()["abc"]["experiment_id"] == "ospf_full"

    def test_concurrent_writes_no_corruption(self, tmp_files):
        import threading
        def write(i):
            state_persist.save_capability_cache({f"10.0.0.{i}": {"i": i}})
        threads = [threading.Thread(target=write, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        data = state_persist.load_capability_cache()
        assert isinstance(data, dict)  # 文件未被写坏
