"""free-preview / free-deploy 的 series 字段端到端测试（需求⑧：批量变量化下发）。"""
import pytest
from fastapi.testclient import TestClient

import main
from domain import device_store as ds
from domain import workspace as ws

SERIES_BODY = {
    "task_name": "OSPF 区域0 批量",
    "target_devices": ["127.0.0.1:2000", "127.0.0.1:2001", "127.0.0.1:2002"],
    "command_template": [
        "ospf 1 router-id {{router_id}}",
        " area 0",
        "  network {{net}} area 0",
    ],
    "verify_commands": ["display ospf peer"],
    "series": {
        "base": {"router_id": "192.168.1.{i}", "net": "10.0.{i}.0"},
        "start": 1,
        "step": 1,
    },
}


@pytest.fixture
def three_devices(monkeypatch):
    devs = [
        {"name": f"R{n}", "ip": f"127.0.0.1:{2000 + n}", "console_port": 2000 + n,
         "model": "Router", "device_type": "huawei"}
        for n in range(3)
    ]
    monkeypatch.setattr(ds, "load_devices", lambda: devs)
    monkeypatch.setattr("routes_config.load_devices", lambda: devs)
    return devs


def _client():
    return TestClient(main.app)


class TestPreviewWithSeries:
    def test_序号只按目标设备顺序分配(self, three_devices):
        """只勾选后两台时，序号应从 1 起算，而不是跟随它们在设备表里的位置。"""
        body = {
            "task_name": "子集",
            "target_devices": ["127.0.0.1:2001", "127.0.0.1:2002"],
            "command_template": ["ospf 1 router-id {{router_id}}"],
            "series": {"base": {"router_id": "192.168.1.{i}"}, "start": 1, "step": 1},
        }
        resp = _client().post("/api/config/free-preview", json=body).json()
        assert resp["preview"]["127.0.0.1:2001"]["commands"][0] == "ospf 1 router-id 192.168.1.1"
        assert resp["preview"]["127.0.0.1:2002"]["commands"][0] == "ospf 1 router-id 192.168.1.2"

    def test_五台式展开(self, three_devices):
        resp = _client().post("/api/config/free-preview", json=SERIES_BODY)
        body = resp.json()
        assert body["success"] is True, body.get("errors")
        preview = body["preview"]
        assert preview["127.0.0.1:2000"]["commands"][0] == "ospf 1 router-id 192.168.1.1"
        assert preview["127.0.0.1:2001"]["commands"][0] == "ospf 1 router-id 192.168.1.2"
        assert preview["127.0.0.1:2002"]["commands"][0] == "ospf 1 router-id 192.168.1.3"
        # 网络号同样各自不同，证明不是同一条灌三遍
        assert preview["127.0.0.1:2002"]["commands"][2] == "network 10.0.3.0 area 0"

    def test_预览覆盖每台目标设备(self, three_devices):
        body = _client().post("/api/config/free-preview", json=SERIES_BODY).json()
        assert set(body["preview"]) == {"127.0.0.1:2000", "127.0.0.1:2001", "127.0.0.1:2002"}

    def test_补零(self, three_devices):
        body = dict(SERIES_BODY)
        body["series"] = {"base": {"router_id": "1.1.1.{i:02d}", "net": "10.0.0.0"}, "start": 1}
        resp = _client().post("/api/config/free-preview", json=body).json()
        assert resp["preview"]["127.0.0.1:2000"]["commands"][0] == "ospf 1 router-id 1.1.1.01"
        assert resp["preview"]["127.0.0.1:2002"]["commands"][0] == "ospf 1 router-id 1.1.1.03"

    def test_手填覆盖序列(self, three_devices):
        body = dict(SERIES_BODY)
        body["device_vars"] = {"127.0.0.1:2001": {"router_id": "9.9.9.9"}}
        resp = _client().post("/api/config/free-preview", json=body).json()
        assert resp["preview"]["127.0.0.1:2001"]["commands"][0] == "ospf 1 router-id 9.9.9.9"
        assert resp["preview"]["127.0.0.1:2002"]["commands"][0] == "ospf 1 router-id 192.168.1.3"

    def test_序列非法时报错不崩(self, three_devices):
        body = dict(SERIES_BODY)
        body["series"] = {"base": {}, "start": 1}
        resp = _client().post("/api/config/free-preview", json=body)
        assert resp.status_code == 200
        assert resp.json()["success"] is False
        assert "series.base" in resp.json()["errors"][0]

    def test_不给_series_时行为不变(self, three_devices):
        body = {
            "task_name": "普通",
            "target_devices": ["127.0.0.1:2000"],
            "command_template": ["ospf 1 router-id {{rid}}"],
            "global_vars": {"rid": "5.5.5.5"},
        }
        resp = _client().post("/api/config/free-preview", json=body).json()
        assert resp["preview"]["127.0.0.1:2000"]["commands"][0] == "ospf 1 router-id 5.5.5.5"


class TestDeployWithSeries:
    def test_序列非法时拒绝下发(self, three_devices, monkeypatch):
        """非法序列绝不能带着一半展开结果去下发。"""
        called = []
        monkeypatch.setattr("routes_config.execute_free_deploy",
                            lambda **kw: called.append(kw) or {"success": True})
        body = dict(SERIES_BODY)
        body["series"] = {"base": {}, "start": 1}
        resp = _client().post("/api/config/free-deploy", json=body)
        assert resp.status_code == 200
        assert resp.json()["success"] is False
        assert called == []
