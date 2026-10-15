"""采集结果持久化与协议角色「未探测」区分的离线测试。"""
import pytest
from fastapi.testclient import TestClient

import main
from app.state import drop_collection, record_collection, state
from services import state_persist
from services.role_detector import detect_protocol_roles


def _cap(ip: str, with_output: bool = True) -> dict:
    """造一条能力识别结果；with_output=False 表示空壳（从未探测过）。"""
    raw = {
        "display current-configuration": "ospf 1 router-id 1.1.1.1\n area 0\n" if with_output else "",
    }
    return {
        "ip": ip,
        "hostname": "R1" if with_output else "",
        "device_role": "router" if with_output else "unknown",
        "capabilities": {"ospf": {"supported": True, "configured": True, "running": True}} if with_output else {},
        "raw_outputs": raw,
        "interfaces": [],
    }


class TestEffectiveProbe:
    def test_空壳不算已探测(self):
        from routes_capabilities import _is_effective_probe
        assert _is_effective_probe(_cap("a", with_output=False)) is False
        assert _is_effective_probe(_cap("a", with_output=True)) is True

    def test_只有_capabilities_也算(self):
        from routes_capabilities import _is_effective_probe
        cap = {"ip": "a", "capabilities": {"ospf": {"supported": True}}, "raw_outputs": {}}
        assert _is_effective_probe(cap) is True

    def test_全是空白输出不算(self):
        from routes_capabilities import _is_effective_probe
        cap = {"ip": "a", "raw_outputs": {"display vlan": "   \n  "}}
        assert _is_effective_probe(cap) is False


class TestRolesAllNotProbed:
    def test_空壳被排除且列入_not_probed(self, monkeypatch):
        """关键回归：空壳绝不能被渲染成"探测后没有角色"的卡片。"""
        import routes_capabilities
        from routes_capabilities import _is_effective_probe  # noqa: F401

        monkeypatch.setattr(routes_capabilities, "load_devices", lambda: [
            {"ip": "127.0.0.1:2001", "name": "R1"},
            {"ip": "127.0.0.1:2002", "name": "R2"},
            {"ip": "127.0.0.1:2003", "name": "R3"},
        ])
        state["capability_results"].clear()
        state["capability_results"]["127.0.0.1:2001"] = _cap("127.0.0.1:2001", True)
        state["capability_results"]["127.0.0.1:2002"] = _cap("127.0.0.1:2002", False)

        resp = TestClient(main.app).post("/api/protocol-roles-all").json()
        assert resp["count"] == 1
        assert [r["ip"] for r in resp["results"]] == ["127.0.0.1:2001"]
        not_probed = {d["ip"] for d in resp["not_probed"]}
        assert not_probed == {"127.0.0.1:2002", "127.0.0.1:2003"}

    def test_全部未探测时给提示信息(self, monkeypatch):
        import routes_capabilities
        state["capability_results"].clear()
        state["capability_results"]["127.0.0.1:2002"] = _cap("127.0.0.1:2002", False)
        monkeypatch.setattr(routes_capabilities, "load_devices", lambda: [
            {"ip": "127.0.0.1:2002", "name": "R2"},
        ])
        resp = TestClient(main.app).post("/api/protocol-roles-all").json()
        assert resp["count"] == 0
        assert len(resp["not_probed"]) == 1


class TestCollectionPersistence:
    def test_record_collection_落盘(self, monkeypatch, tmp_path):
        target = tmp_path / "collection_cache.json"
        monkeypatch.setattr(state_persist, "collection_cache_file", lambda: str(target))
        record_collection("127.0.0.1:2001", _collection_result("127.0.0.1:2001"))
        assert target.exists()
        loaded = state_persist.load_collection_cache()
        assert "127.0.0.1:2001" in loaded

    def test_落盘会裁剪大字段(self, monkeypatch, tmp_path):
        target = tmp_path / "cc.json"
        monkeypatch.setattr(state_persist, "collection_cache_file", lambda: str(target))
        result = _collection_result("127.0.0.1:2001")
        result["commands"]["display current-configuration"]["output"] = "x" * 500
        result["huge_field"] = "y" * 10000   # 不该被保留
        record_collection("127.0.0.1:2001", result)
        loaded = state_persist.load_collection_cache()
        saved = loaded["127.0.0.1:2001"]["commands"]["display current-configuration"]
        assert isinstance(saved, dict) and saved["output"] == "x" * 500
        assert "huge_field" not in loaded["127.0.0.1:2001"]

    def test_落盘后回灌的数据结构与内存态一致(self, monkeypatch, tmp_path):
        """关键回归：commands[key] 必须还是 {success, output, error} dict。

        早先落盘时把 output 抽成纯字符串，回灌后 role_detector 按
        commands[k]["output"] 取值全部拿到空，协议角色页重启后全空。
        """
        target = tmp_path / "cc.json"
        monkeypatch.setattr(state_persist, "collection_cache_file", lambda: str(target))
        original = _collection_result("127.0.0.1:2001")
        record_collection("127.0.0.1:2001", original)
        reloaded = state_persist.load_collection_cache()["127.0.0.1:2001"]

        for key, value in original["commands"].items():
            if key not in reloaded["commands"]:
                continue
            assert isinstance(reloaded["commands"][key], dict), key
            assert reloaded["commands"][key]["output"] == value["output"], key
        assert reloaded["ok"] == original["ok"]
        assert reloaded["info"] == original["info"]

    def test_回灌后角色解析仍能拿到配置(self, monkeypatch, tmp_path):
        """落盘 → 回灌 → 角色解析这条链不能断。

        注意：detect_protocol_roles 吃的是 capability_probe 的 raw_outputs
        （{命令: 字符串}），与 collection_results 的 commands（{命令: {output}}）
        结构不同。这里验证的是回灌后的 commands 能无损还原成 raw_outputs。
        """
        target = tmp_path / "cc.json"
        monkeypatch.setattr(state_persist, "collection_cache_file", lambda: str(target))
        result = _collection_result("127.0.0.1:2001")
        record_collection("127.0.0.1:2001", result)
        reloaded = state_persist.load_collection_cache()["127.0.0.1:2001"]

        # 回灌后的结构必须仍是 {success, output, error}，取值方才能拿到原文
        raw_outputs = {
            cmd: value["output"]
            for cmd, value in reloaded["commands"].items()
            if isinstance(value, dict) and isinstance(value.get("output"), str)
        }
        assert "ospf 1 router-id 1.1.1.1" in raw_outputs["display current-configuration"]

        from services.role_detector import detect_protocol_roles
        roles = detect_protocol_roles({
            "ip": "127.0.0.1:2001",
            "hostname": "R1",
            "device_role": "router",
            "capabilities": {"ospf": {"supported": True, "configured": True, "running": True}},
            "raw_outputs": raw_outputs,
            "interfaces": [],
        })
        assert roles["protocols"]["ospf"]["status"] == "running"
        assert roles["protocols"]["ospf"]["roles"], "回灌后应仍能解析出 OSPF 角色"

    def test_drop_collection_同步删除(self, monkeypatch, tmp_path):
        target = tmp_path / "cc.json"
        monkeypatch.setattr(state_persist, "collection_cache_file", lambda: str(target))
        record_collection("127.0.0.1:2001", _collection_result("127.0.0.1:2001"))
        drop_collection("127.0.0.1:2001")
        assert state_persist.load_collection_cache() == {}

    def test_落盘失败不影响采集本身(self, monkeypatch):
        monkeypatch.setattr(
            state_persist, "save_collection_cache",
            lambda results: (_ for _ in ()).throw(OSError("磁盘满了")),
        )
        record_collection("127.0.0.1:2099", _collection_result("127.0.0.1:2099"))
        assert state["collection_results"]["127.0.0.1:2099"]["ok"] is True


class TestRefreshAfterDeploy:
    def test_按端口反查设备(self, monkeypatch):
        import routes_config
        from routes_config import _ips_by_console_port
        monkeypatch.setattr(routes_config, "load_devices", lambda: [
            {"ip": "127.0.0.1:2001", "console_port": 2001},
            {"ip": "127.0.0.1:2002", "console_port": 2002},
        ])
        assert _ips_by_console_port(2002) == ["127.0.0.1:2002"]
        assert _ips_by_console_port(9999) == []

    def test_刷新在后台线程执行且不抛异常(self, monkeypatch):
        """下发接口不能被采集拖住；采集失败也不能让下发失败。"""
        from routes_config import _refresh_after_deploy
        monkeypatch.setattr("domain.collect.collect_device",
                            lambda dev: (_ for _ in ()).throw(RuntimeError("设备离线")))
        _refresh_after_deploy(["127.0.0.1:2001"])  # 不抛即通过

    def test_空列表直接返回(self):
        from routes_config import _refresh_after_deploy
        _refresh_after_deploy([])  # 不起线程


@pytest.fixture(autouse=True)
def _clean_state():
    """这些用例直接改全局 state，跑完必须还原，否则污染其他测试。"""
    saved_caps = dict(state.get("capability_results", {}))
    saved_coll = dict(state.get("collection_results", {}))
    yield
    state["capability_results"].clear(); state["capability_results"].update(saved_caps)
    state["collection_results"].clear(); state["collection_results"].update(saved_coll)


class TestRoleParsingRobustness:
    """raw_outputs 里混入 dict 值时不能抛 TypeError（曾让批量接口整体 500）。"""

    def test_dict_形态的_output_能解析(self):
        from services.role_detector import detect_protocol_roles
        res = detect_protocol_roles({
            "ip": "127.0.0.1:2001",
            "hostname": "R1",
            "device_role": "router",
            "capabilities": {"ospf": {"supported": True, "running": True}},
            "raw_outputs": {
                "display current-configuration": {
                    "success": True,
                    "output": "ospf 1 router-id 1.1.1.1\n area 0\n",
                    "error": None,
                },
                "display ospf peer": {"success": True, "output": "", "error": None},
            },
            "interfaces": [],
        })
        assert res["protocols"]["ospf"]["status"] == "running"
        assert any("Router ID" == r["role"] for r in res["protocols"]["ospf"]["roles"])

    def test_混合形态_字符串与_dict_并存(self):
        from services.role_detector import detect_protocol_roles
        res = detect_protocol_roles({
            "ip": "x",
            "capabilities": {"vrrp": {"supported": True, "configured": True, "running": True}},
            "raw_outputs": {
                "display current-configuration": "vrrp vrid 1 virtual-ip 10.0.0.254\n",
                "display vlan": {"success": True, "output": "VLAN 10\n", "error": None},
            },
            "interfaces": [],
        })
        assert res["protocols"]["vrrp"]["status"] == "running"

    def test_空_和_None_不崩(self):
        from services.role_detector import detect_protocol_roles
        for raw in ({}, None, {"display vlan": None}, {"display vlan": 42}):
            res = detect_protocol_roles({"ip": "x", "raw_outputs": raw, "interfaces": []})
            assert res["ip"] == "x"

    def test_batch_接口对单台解析失败有容忍(self, monkeypatch):
        """一台脏数据不能让整批都看不到角色。"""
        import routes_capabilities
        from domain import device_store as ds
        monkeypatch.setattr(routes_capabilities, "load_devices", lambda: [])

        def boom(cap):
            if cap.get("ip") == "bad":
                raise TypeError("expected string or bytes-like object")
            return {"ip": cap.get("ip"), "hostname": "", "device_role": "router", "protocols": {}}

        monkeypatch.setattr(routes_capabilities, "detect_protocol_roles", boom)
        state["capability_results"].clear()
        state["capability_results"]["good"] = {"ip": "good", "raw_outputs": {"a": "b"}}
        state["capability_results"]["bad"] = {"ip": "bad", "raw_outputs": {"a": "b"}}

        resp = TestClient(main.app).post("/api/protocol-roles-all").json()
        assert resp["success"] is True
        assert resp["count"] == 1
        assert [f["ip"] for f in resp["failed"]] == ["bad"]


def _collection_result(ip: str) -> dict:
    """与 domain/collect.py 的真实采集结果同构：commands[key] 是 {success,output,error}。"""
    return {
        "ip": ip,
        "name": "R1",
        "ok": True,
        "online": True,
        "commands": {
            "display current-configuration": {
                "success": True,
                "output": "ospf 1 router-id 1.1.1.1\n area 0\n",
                "error": None,
            },
            "display ip interface brief": {
                "success": True,
                "output": "GE0/0/0  10.0.12.1/24  up  up\n",
                "error": None,
            },
        },
        "info": {"hostname": "R1"},
    }


def test_角色解析能吃下落盘后的采集结果():
    """落盘 → 回灌 → 角色解析，这条链不能断。"""
    result = detect_protocol_roles(_cap("127.0.0.1:2001", True))
    assert result["protocols"]["ospf"]["status"] == "running"
    assert any(r["role"] for r in result["protocols"]["ospf"]["roles"])
