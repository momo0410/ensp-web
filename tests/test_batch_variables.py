"""services/batch_variables.py 的离线单测（需求⑧：批量变量化下发）。"""
import pytest

from services.batch_variables import (
    SeriesError,
    expand_variable_series,
    merge_series_into_device_vars,
    series_vars_in_templates,
)


def _spec(**kw):
    base = kw.pop("base", {"router_id": "192.168.1.{i}"})
    out = {"base": base}
    out.update(kw)
    return out


class TestBasicExpansion:
    def test_五台设备顺序递增(self):
        ips = [f"127.0.0.1:{2000 + n}" for n in range(5)]
        res = expand_variable_series(_spec(), ips)
        assert res["ok"] is True
        assert [res["device_vars"][ip]["router_id"] for ip in ips] == [
            "192.168.1.1", "192.168.1.2", "192.168.1.3", "192.168.1.4", "192.168.1.5",
        ]

    def test_多个变量各自展开(self):
        ips = ["127.0.0.1:2000", "127.0.0.1:2001"]
        res = expand_variable_series(
            _spec(base={"router_id": "192.168.1.{i}", "area0_net": "10.0.{i}.0"}), ips
        )
        assert res["device_vars"][ips[0]] == {"router_id": "192.168.1.1", "area0_net": "10.0.1.0"}
        assert res["device_vars"][ips[1]] == {"router_id": "192.168.1.2", "area0_net": "10.0.2.0"}

    def test_start_与_step(self):
        ips = ["a", "b", "c"]
        res = expand_variable_series(_spec(start=10, step=5), ips)
        assert [res["device_vars"][ip]["router_id"] for ip in ips] == [
            "192.168.1.10", "192.168.1.15", "192.168.1.20",
        ]

    def test_series_values_带序号便于前端展示(self):
        ips = ["a", "b"]
        res = expand_variable_series(_spec(start=1, step=1), ips)
        assert [s["index"] for s in res["series_values"]] == [1, 2]
        assert res["series_values"][0]["ip"] == "a"
        assert res["series_values"][0]["vars"] == {"router_id": "192.168.1.1"}


class TestPlaceholders:
    def test_显式补零格式符(self):
        ips = ["a", "b"]
        res = expand_variable_series(_spec(base={"vlan": "vlan{i:03d}"}), ips)
        assert res["device_vars"]["a"]["vlan"] == "vlan001"
        assert res["device_vars"]["b"]["vlan"] == "vlan002"

    def test_width_简写补零(self):
        ips = ["a", "b"]
        res = expand_variable_series(_spec(base={"vlan": "vlan{i}"}, width=3), ips)
        assert res["device_vars"]["a"]["vlan"] == "vlan001"
        assert res["device_vars"]["b"]["vlan"] == "vlan002"

    def test_ip_last_取末段(self):
        ips = ["127.0.0.1:2000", "127.0.0.1:2001"]
        res = expand_variable_series(_spec(base={"host": "10.1.1.{ip_last}"}), ips)
        # ip_last 取的是 IP 的末段而非端口末段
        assert res["device_vars"][ips[0]]["host"] == "10.1.1.1"

    def test_固定值不做展开(self):
        ips = ["a", "b"]
        res = expand_variable_series(_spec(base={"area": "0", "rid": "1.1.1.{i}"}), ips)
        assert res["device_vars"]["a"] == {"area": "0", "rid": "1.1.1.1"}
        assert res["device_vars"]["b"] == {"area": "0", "rid": "1.1.1.2"}

    def test_未知占位符原样保留(self):
        res = expand_variable_series(_spec(base={"x": "{unknown}"}), ["a"])
        assert res["device_vars"]["a"]["x"] == "{unknown}"

    def test_数字模板转为字符串(self):
        res = expand_variable_series(_spec(base={"n": 7}), ["a"])
        assert res["device_vars"]["a"]["n"] == "7"


class TestOverrides:
    def test_单台偏离序列(self):
        ips = ["a", "b", "c"]
        res = expand_variable_series(
            _spec(per_device_overrides={"b": {"router_id": "9.9.9.9"}}), ips
        )
        assert res["device_vars"]["b"]["router_id"] == "9.9.9.9"
        assert res["device_vars"]["a"]["router_id"] == "192.168.1.1"
        assert res["device_vars"]["c"]["router_id"] == "192.168.1.3"


class TestDeviceObjects:
    def test_接受设备dict(self):
        devs = [{"ip": "127.0.0.1:2000", "name": "R1"}, {"ip": "127.0.0.1:2001", "name": "R2"}]
        res = expand_variable_series(_spec(), devs)
        assert res["device_vars"]["127.0.0.1:2000"]["router_id"] == "192.168.1.1"
        assert res["device_vars"]["127.0.0.1:2001"]["router_id"] == "192.168.1.2"


class TestMerge:
    def test_手填优先于序列(self):
        expanded = {"a": {"router_id": "192.168.1.1"}}
        merged = merge_series_into_device_vars(expanded, {"a": {"router_id": "手工改的"}})
        assert merged["a"]["router_id"] == "手工改的"

    def test_手填可新增变量(self):
        expanded = {"a": {"router_id": "192.168.1.1"}}
        merged = merge_series_into_device_vars(expanded, {"a": {"extra": "x"}})
        assert merged["a"] == {"router_id": "192.168.1.1", "extra": "x"}

    def test_无手填时原样(self):
        expanded = {"a": {"v": "1"}}
        assert merge_series_into_device_vars(expanded, None) == {"a": {"v": "1"}}


class TestValidation:
    def test_base_为空报错(self):
        with pytest.raises(SeriesError):
            expand_variable_series({"base": {}}, ["a"])

    def test_base_非对象报错(self):
        with pytest.raises(SeriesError):
            expand_variable_series({"base": "x"}, ["a"])

    def test_变量模板类型错误(self):
        with pytest.raises(SeriesError):
            expand_variable_series({"base": {"v": ["list"]}}, ["a"])

    def test_start_非整数(self):
        with pytest.raises(SeriesError):
            expand_variable_series(_spec(start="abc"), ["a"])

    def test_step_为零(self):
        with pytest.raises(SeriesError):
            expand_variable_series(_spec(step=0), ["a"])

    def test_width_超上限(self):
        with pytest.raises(SeriesError):
            expand_variable_series(_spec(width=99), ["a"])

    def test_设备数超上限(self):
        with pytest.raises(SeriesError):
            expand_variable_series(_spec(), [str(n) for n in range(201)])

    def test_重复_IP_报错(self):
        with pytest.raises(SeriesError):
            expand_variable_series(_spec(), ["a", "a"])

    def test_空设备列表(self):
        res = expand_variable_series(_spec(), [])
        assert res["device_vars"] == {} and res["series_values"] == []


class TestTemplateVarScan:
    def test_提取模板变量(self):
        assert series_vars_in_templates(["ospf 1 router-id {{router_id}}", "area {{area}}"]) == [
            "area", "router_id",
        ]

    def test_字符串模板(self):
        assert series_vars_in_templates("router-id {{rid}}\narea 0") == ["rid"]
