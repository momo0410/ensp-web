"""POST /api/ensp/attach-active 的离线单测（需求②反向联动）。

用 monkeypatch 假造 read_ensp_topology 的返回值，不触碰真机。
"""
import pytest
from fastapi.testclient import TestClient

import main
import routes_devices
from domain import workspace as ws


def _client():
    return TestClient(main.app)


LIVE = {
    "ok": True,
    "ensp_running": True,
    "pid": 1234,
    "devices": 3,
    "links": 3,
    "live_devices": [
        {"name": "R1", "model": "AR2220", "power_state": "running"},
        {"name": "SW1", "model": "S5700", "power_state": "stopped"},
        {"name": "PC1", "model": "PC", "power_state": "running"},
    ],
}


@pytest.fixture
def fake_ensp(monkeypatch):
    """让 routes_devices 以为 eNSP 里开着 LIVE 那套拓扑。"""
    monkeypatch.setattr(routes_devices, "read_ensp_topology", lambda: dict(LIVE))


def _make_topology(name, device_names):
    ws.create_topology(name)
    devices = [
        {"name": n, "model": "Router", "console_port": 2000 + i, "device_type": "huawei"}
        for i, n in enumerate(device_names)
    ]
    ws.save_topology(name, devices, links=[])


class TestNotRunning:
    def test_ensp_未运行(self, monkeypatch):
        monkeypatch.setattr(routes_devices, "read_ensp_topology",
                            lambda: {"ok": True, "ensp_running": False, "live_devices": []})
        resp = _client().post("/api/ensp/attach-active", json={"mode": "match_by_devices"})
        assert resp.json()["success"] is False
        assert "eNSP 未运行" in resp.json()["error"]

    def test_ensp_里没有设备(self, monkeypatch):
        monkeypatch.setattr(routes_devices, "read_ensp_topology",
                            lambda: {"ok": True, "ensp_running": True, "live_devices": []})
        resp = _client().post("/api/ensp/attach-active", json={"mode": "match_by_devices"})
        assert resp.json()["success"] is False
        assert "未检测到任何设备" in resp.json()["error"]


class TestMatchByDevices:
    def test_匹配到就切换(self, fake_ensp):
        _make_topology("other", ["X1", "X2"])
        _make_topology("target", ["R1", "SW1", "PC1"])
        ws.set_active("other")

        resp = _client().post("/api/ensp/attach-active", json={"mode": "match_by_devices"})
        body = resp.json()
        assert body["success"] is True
        assert body["active"] == "target"
        assert body["matched"] == 3
        assert ws.active_topology() == "target"

    def test_集合相同但顺序不同也算匹配(self, fake_ensp):
        _make_topology("target", ["PC1", "R1", "SW1"])
        resp = _client().post("/api/ensp/attach-active", json={"mode": "match_by_devices"})
        assert resp.json()["success"] is True
        assert resp.json()["active"] == "target"

    def test_没有匹配项时给出可选拓扑(self, fake_ensp):
        _make_topology("target", ["R1", "SW1"])  # 少一台 PC1
        resp = _client().post("/api/ensp/attach-active", json={"mode": "match_by_devices"})
        body = resp.json()
        assert body["success"] is False
        assert body["actual"] == ["R1", "SW1", "PC1"]
        assert "target" in body["available"]
        assert "import_as" in body["hint"]

    def test_空拓扑不参与匹配(self, fake_ensp):
        ws.create_topology("empty")  # 0 台设备
        resp = _client().post("/api/ensp/attach-active", json={"mode": "match_by_devices"})
        assert resp.json()["success"] is False


class TestImportAs:
    def test_导入为新拓扑(self, fake_ensp):
        resp = _client().post("/api/ensp/attach-active",
                              json={"mode": "import_as", "name": "from_ensp"})
        body = resp.json()
        assert body["success"] is True
        assert body["active"] == "from_ensp"
        assert body["imported"] == 3
        assert body["links_imported"] == 0
        assert "手动补" in body["message"]

        topo = ws.load_topology("from_ensp")
        assert [d["name"] for d in topo["devices"]] == ["R1", "SW1", "PC1"]
        assert topo["links"] == []
        assert ws.active_topology() == "from_ensp"

    def test_导入的设备带_ip_主键(self, fake_ensp):
        """ip 是项目里的设备主键，导入时必须与 console_port 一起补全。"""
        _client().post("/api/ensp/attach-active", json={"mode": "import_as", "name": "t"})
        devices = ws.load_topology("t")["devices"]
        for dev in devices:
            assert dev.get("ip") == f"127.0.0.1:{dev['console_port']}"

    def test_console_端口顺序分配(self, fake_ensp):
        _client().post("/api/ensp/attach-active", json={"mode": "import_as", "name": "t"})
        devices = ws.load_topology("t")["devices"]
        assert [d["console_port"] for d in devices] == [2000, 2001, 2002]

    def test_默认拓扑名(self, fake_ensp):
        resp = _client().post("/api/ensp/attach-active", json={"mode": "import_as"})
        assert resp.json()["active"] == "ensp_imported"

    def test_拓扑已存在则拒绝(self, fake_ensp):
        _client().post("/api/ensp/attach-active", json={"mode": "import_as", "name": "dup"})
        resp = _client().post("/api/ensp/attach-active", json={"mode": "import_as", "name": "dup"})
        assert resp.json()["success"] is False
        assert "已存在" in resp.json()["error"]

    def test_未知型号回退_Router(self, monkeypatch, fake_ensp):
        live = dict(LIVE)
        live["live_devices"] = [{"name": "W1", "model": "不存在的型号", "power_state": "running"}]
        monkeypatch.setattr(routes_devices, "read_ensp_topology", lambda: live)
        _client().post("/api/ensp/attach-active", json={"mode": "import_as", "name": "t"})
        assert ws.load_topology("t")["devices"][0]["model"] == "Router"


class TestBadMode:
    def test_未知模式(self, fake_ensp):
        resp = _client().post("/api/ensp/attach-active", json={"mode": "nonsense"})
        assert resp.json()["success"] is False
        assert "未知模式" in resp.json()["error"]
