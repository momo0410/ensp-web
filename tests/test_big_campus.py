"""tools/big_campus.py 与 big_campus 预设接入的离线测试。"""
import pytest
from fastapi.testclient import TestClient

import main
from tools.big_campus import (
    BIG_CAMPUS_NAME,
    build_big_campus,
    expected_router_ids,
    ospf_series_targets,
    validate_layout,
)


class TestLayout:
    def test_设备数超过十台(self):
        topo = build_big_campus()
        assert len(topo["devices"]) >= 10

    def test_自检通过(self):
        assert validate_layout() == []

    def test_设备名唯一(self):
        names = [d["name"] for d in build_big_campus()["devices"]]
        assert len(set(names)) == len(names)

    def test_console_端口唯一且从_2000_起(self):
        ports = [d["com_port"] for d in build_big_campus()["devices"] if d.get("com_port")]
        assert len(set(ports)) == len(ports)
        assert min(ports) == 2000

    def test_终端设备无_console_端口但有网络参数(self):
        for dev in build_big_campus()["devices"]:
            if dev["model"] in ("PC", "Laptop"):
                assert dev.get("com_port") is None
                assert dev.get("ip_address") and dev.get("netmask") and dev.get("gateway")

    def test_接口在各型号上真实存在(self):
        from tools.gen_topology import DEVICE_MODELS, TopologyError, interface_index_map
        topo = build_big_campus()
        models = {d["name"]: d["model"] for d in topo["devices"]}
        for link in topo["links"]:
            for side in ("a", "b"):
                dev, iface = link[side]["device"], link[side]["interface"]
                try:
                    keys = set(interface_index_map(models[dev]).keys())
                except TopologyError as e:
                    pytest.fail(f"{dev} 取接口表失败: {e}")
                short = {k for k in keys if not k.startswith("Gigabit")}
                assert iface in short or iface in keys, f"{dev}({models[dev]}) 没有接口 {iface}"

    def test_接口无重复占用(self):
        used = set()
        for link in build_big_campus()["links"]:
            for side in ("a", "b"):
                key = (link[side]["device"], link[side]["interface"])
                assert key not in used, f"接口重复占用: {key}"
                used.add(key)

    def test_终端网段不重叠(self):
        nets = [d["ip_address"] for d in build_big_campus()["devices"] if d.get("ip_address")]
        assert len(set(nets)) == len(nets)


class TestOspfSeriesPlan:
    def test_序列目标正好五台(self):
        assert len(ospf_series_targets()) == 5

    def test_目标端口与案例设备对应(self):
        topo = build_big_campus()
        ports = {d.get("com_port") for d in topo["devices"] if d.get("com_port")}
        for ip in ospf_series_targets():
            assert int(ip.split(":")[1]) in ports

    def test_预期_router_id_依次递增(self):
        expected = expected_router_ids()
        assert list(expected.values()) == [f"192.168.1.{i}" for i in range(1, 6)]

    def test_与_batch_variables_展开结果一致(self):
        from services.batch_variables import expand_variable_series
        result = expand_variable_series(
            {"base": {"router_id": "192.168.1.{i}"}, "start": 1, "step": 1},
            ospf_series_targets(),
        )
        assert {ip: v["router_id"] for ip, v in result["device_vars"].items()} == expected_router_ids()


class TestPresetRoute:
    def test_big_campus_预设被接受(self, monkeypatch):
        """不连 eNSP，只验证 preset 分支能构造出 15 台设备并写进设备表。"""
        import routes_devices
        from domain import device_store as ds
        monkeypatch.setattr(routes_devices, "sync_and_start_ensp_topo",
                            lambda devices, links=None, topo_name=None, auto_start=True:
                            {"ok": True, "devices": len(devices), "topo_name": topo_name})
        saved = {}
        monkeypatch.setattr(ds, "save_devices", lambda d: saved.setdefault("devices", d))
        monkeypatch.setattr(routes_devices, "save_devices", lambda d: saved.setdefault("devices", d))

        resp = TestClient(main.app).post("/api/ensp/topo/apply", json={
            "preset": "big_campus",
            "auto_start": False,
        })
        body = resp.json()
        assert body["success"] is True, body
        assert len(saved["devices"]) == 15

    def test_未知预设仍报错(self):
        resp = TestClient(main.app).post("/api/ensp/topo/apply", json={"preset": "nope"})
        assert resp.status_code == 400
        assert "未知的拓扑预设" in resp.json()["detail"]
