"""services/ai_plan.py 的离线单测（需求⑦：AI 结构化 plan）。

重点验证安全红线：AI 的自由文本、缺字段、越界、设备不存在等情况
一律被拒，绝不静默通过。
"""
import pytest

from services.ai_plan import (
    PlanError,
    extract_plan,
    render_plan_summary,
    validate_plan,
    validate_config_plan,
    validate_topology_plan,
)

KNOWN_IPS = ["127.0.0.1:2000", "127.0.0.1:2001"]


def _config_plan(**kw):
    plan = {
        "kind": "config",
        "targets": ["127.0.0.1:2000"],
        "commands": ["ospf 1 router-id 1.1.1.1", " area 0"],
        "verify": ["display ospf peer"],
        "save": False,
    }
    plan.update(kw)
    return plan


def _topo_plan(**kw):
    plan = {
        "kind": "topology",
        "name": "ai_topo",
        "devices": [{"name": "R1", "model": "Router"}, {"name": "R2", "model": "S5700"}],
        "links": [{"a": {"device": "R1", "interface": "GE0/0/0"},
                   "b": {"device": "R2", "interface": "GE0/0/0"}}],
    }
    plan.update(kw)
    return plan


class TestExtractPlan:
    def test_json_代码块(self):
        reply = '好的，方案如下：\n```json\n{"kind": "config"}\n```\n请确认。'
        plan, err = extract_plan(reply)
        assert plan == {"kind": "config"}
        assert err is None

    def test_无代码块的裸_json(self):
        plan, err = extract_plan('我先说明一下 {"kind": "topology", "name": "x"} 就这样')
        assert plan["kind"] == "topology"

    def test_前后有废话(self):
        reply = '我建议\n```json\n{"kind":"config","targets":["a"],"commands":["x"]}\n```\n注意备份'
        plan, _ = extract_plan(reply)
        assert plan["targets"] == ["a"]

    def test_空回复(self):
        plan, err = extract_plan("")
        assert plan is None
        assert "没有返回任何内容" in err

    def test_没有_json(self):
        plan, err = extract_plan("我觉得你应该配 OSPF，先进入系统视图。")
        assert plan is None
        assert "找不到合法的 JSON plan" in err

    def test_非法_json(self):
        plan, err = extract_plan('```json\n{"kind": "config", 坏了}\n```')
        assert plan is None

    def test_json_数组不算_plan(self):
        plan, err = extract_plan('```json\n[1, 2, 3]\n```')
        assert plan is None


class TestConfigPlanValidation:
    def test_合法(self):
        result = validate_plan(_config_plan(), KNOWN_IPS)
        assert result["kind"] == "config"
        assert result["target_devices"] == ["127.0.0.1:2000"]
        assert result["command_template"] == ["ospf 1 router-id 1.1.1.1", "area 0"]

    def test_命令可为换行字符串(self):
        result = validate_plan(_config_plan(commands="ospf 1\n area 0\n"), KNOWN_IPS)
        assert result["command_template"] == ["ospf 1", "area 0"]

    def test_目标设备不在表中(self):
        with pytest.raises(PlanError) as e:
            validate_plan(_config_plan(targets=["10.9.9.9"]), KNOWN_IPS)
        assert "不在设备表中" in str(e.value)

    def test_不提供_known_ips_时跳过存在性检查(self):
        result = validate_plan(_config_plan(targets=["任意"]), None)
        assert result["target_devices"] == ["任意"]

    def test_targets_为空(self):
        with pytest.raises(PlanError):
            validate_plan(_config_plan(targets=[]), KNOWN_IPS)

    def test_commands_为空(self):
        with pytest.raises(PlanError):
            validate_plan(_config_plan(commands=["", "   "]), KNOWN_IPS)

    def test_commands_不是数组(self):
        with pytest.raises(PlanError):
            validate_plan(_config_plan(commands={"a": 1}), KNOWN_IPS)

    def test_命令里混入非字符串(self):
        with pytest.raises(PlanError):
            validate_plan(_config_plan(commands=["ok", 42]), KNOWN_IPS)

    def test_目标数超上限(self):
        with pytest.raises(PlanError) as e:
            validate_plan(_config_plan(targets=[f"ip{n}" for n in range(51)]), None)
        assert "超过上限" in str(e.value)

    def test_series_合法(self):
        result = validate_plan(
            _config_plan(series={"base": {"rid": "1.1.1.{i}"}, "start": 1}), KNOWN_IPS
        )
        assert result["series"]["base"] == {"rid": "1.1.1.{i}"}
        assert result["series"]["start"] == 1

    def test_series_缺_base(self):
        with pytest.raises(PlanError):
            validate_plan(_config_plan(series={"start": 1}), KNOWN_IPS)

    def test_verify_可为空(self):
        result = validate_plan(_config_plan(verify=[]), KNOWN_IPS)
        assert result["verify_commands"] == []

    def test_save_非布尔(self):
        with pytest.raises(PlanError):
            validate_plan(_config_plan(save="yes"), KNOWN_IPS)


class TestTopologyPlanValidation:
    def test_合法(self):
        result = validate_plan(_topo_plan())
        assert result["kind"] == "topology"
        assert len(result["devices"]) == 2
        assert len(result["links"]) == 1
        assert result["auto_start"] is False

    def test_型号不支持(self):
        with pytest.raises(PlanError) as e:
            validate_plan(_topo_plan(devices=[{"name": "R1", "model": "不存在的型号"}]))
        assert "不受支持" in str(e.value)

    def test_设备名重复(self):
        with pytest.raises(PlanError):
            validate_plan(_topo_plan(devices=[{"name": "R1"}, {"name": "R1"}]))

    def test_设备缺_name(self):
        with pytest.raises(PlanError):
            validate_plan(_topo_plan(devices=[{"model": "Router"}]))

    def test_devices_为空(self):
        with pytest.raises(PlanError):
            validate_plan(_topo_plan(devices=[]))

    def test_连线指向不存在的设备(self):
        bad = [{"a": {"device": "R1", "interface": "GE0/0/0"},
                "b": {"device": "R9", "interface": "GE0/0/0"}}]
        with pytest.raises(PlanError) as e:
            validate_plan(_topo_plan(links=bad))
        assert "不在设备列表里" in str(e.value)

    def test_接口被重复占用(self):
        links = [
            {"a": {"device": "R1", "interface": "GE0/0/0"},
             "b": {"device": "R2", "interface": "GE0/0/0"}},
            {"a": {"device": "R1", "interface": "GE0/0/0"},
             "b": {"device": "R2", "interface": "GE0/0/1"}},
        ]
        with pytest.raises(PlanError) as e:
            validate_plan(_topo_plan(links=links))
        assert "重复占用" in str(e.value)

    def test_接口名不合法(self):
        links = [{"a": {"device": "R1", "interface": "随便写"},
                  "b": {"device": "R2", "interface": "GE0/0/0"}}]
        with pytest.raises(PlanError):
            validate_plan(_topo_plan(links=links))

    def test_连线两端相同(self):
        links = [{"a": {"device": "R1", "interface": "GE0/0/0"},
                  "b": {"device": "R1", "interface": "GE0/0/0"}}]
        with pytest.raises(PlanError):
            validate_plan(_topo_plan(links=links))

    def test_设备数超上限(self):
        with pytest.raises(PlanError):
            validate_plan(_topo_plan(devices=[{"name": f"R{n}"} for n in range(61)]))

    def test_终端设备网络参数透传(self):
        result = validate_plan(_topo_plan(
            devices=[
                {"name": "PC1", "model": "PC",
                 "ip_address": "192.168.1.10", "netmask": "255.255.255.0", "gateway": "192.168.1.1"},
                {"name": "R1", "model": "Router"},
            ],
            links=[],
        ))
        assert result["devices"][0]["ip_address"] == "192.168.1.10"
        assert result["devices"][1].get("ip_address") is None


class TestKindDispatch:
    def test_未知_kind(self):
        with pytest.raises(PlanError) as e:
            validate_plan({"kind": "delete_everything"})
        assert "plan.kind" in str(e.value)

    def test_缺_kind(self):
        with pytest.raises(PlanError):
            validate_plan({"targets": ["a"], "commands": ["x"]})

    def test_非对象(self):
        with pytest.raises(PlanError):
            validate_plan(["not", "a", "dict"])


class TestSummary:
    def test_配置摘要(self):
        result = validate_plan(_config_plan(targets=KNOWN_IPS), KNOWN_IPS)
        text = render_plan_summary(result)
        assert "2 台设备" in text
        assert "2 条命令" in text

    def test_拓扑摘要(self):
        result = validate_plan(_topo_plan())
        assert "ai_topo" in render_plan_summary(result)
        assert "2 台设备" in render_plan_summary(result)

    def test_多目标摘要省略(self):
        result = validate_plan(_config_plan(targets=[f"ip{n}" for n in range(8)]), None)
        assert "等" in render_plan_summary(result)
