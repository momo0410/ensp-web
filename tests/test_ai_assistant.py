"""
test_ai_assistant.py - AI 助教模块单元测试（全部离线，不访问网络）
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import pytest

from services import ai_assistant
from services.ai_assistant import (
    load_config, save_config, is_configured, mask_key,
    build_messages, render_device_context, render_template_catalog,
    evaluate_lab_progress, LAB_EXPERIMENTS,
    AIError,
)
from test_role_detector import _cap_result  # 复用 realistic fixture


@pytest.fixture
def tmp_config(monkeypatch, tmp_path):
    """把 AI 配置文件隔离到临时目录。"""
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestConfig:
    def test_default_unconfigured(self, tmp_config):
        cfg = load_config()
        assert is_configured(cfg) is False

    def test_save_and_keep_key(self, tmp_config):
        save_config({"api_base": "https://api.example.com/v1", "api_key": "sk-abc12345678",
                     "model": "test-model"})
        assert is_configured() is True
        # api_key 留空 → 保留原值
        cfg = save_config({"model": "new-model", "api_key": ""})
        assert cfg["api_key"] == "sk-abc12345678"
        assert cfg["model"] == "new-model"
        assert cfg["api_base"] == "https://api.example.com/v1"

    def test_mode_validation(self, tmp_config):
        cfg = save_config({"mode": "engineer"})
        assert cfg["mode"] == "engineer"
        cfg = save_config({"mode": "bogus"})
        assert cfg["mode"] == "engineer"  # 非法值不生效

    def test_temperature_clamp(self, tmp_config):
        cfg = save_config({"temperature": 99})
        assert cfg["temperature"] <= 1.5

    def test_mask_key(self):
        assert mask_key("") == ""
        assert mask_key("sk-1234567890").startswith("sk-1")
        assert "****" in mask_key("sk-1234567890")

    def test_chat_once_raises_when_unconfigured(self, tmp_config):
        with pytest.raises(AIError):
            ai_assistant.chat_once([{"role": "user", "content": "hi"}])

    def test_chat_stream_raises_when_unconfigured(self, tmp_config):
        with pytest.raises(AIError):
            list(ai_assistant.chat_stream([{"role": "user", "content": "hi"}]))


class TestContext:
    def test_context_without_cap(self):
        text = render_device_context({"name": "AR1", "ip": "1.2.3.4"}, None)
        assert "AR1" in text and "能力识别" in text

    def test_context_with_cap(self):
        cap = _cap_result()
        text = render_device_context({"name": "Core-SW", "ip": cap["ip"]}, cap)
        assert "Core-SW" in text
        assert "l3_switch" in text
        assert "Master" in text            # VRRP 角色
        assert "Vlanif10" in text          # 接口
        assert "ip pool vlan10_pool" in text  # 配置摘录

    def test_context_full_config_flag(self):
        cap = _cap_result()
        short = render_device_context({"name": "x", "ip": "1"}, cap)
        full = render_device_context({"name": "x", "ip": "1"}, cap, config_full=True)
        assert len(full) >= len(short)

    def test_template_catalog_lists_keys(self):
        catalog = render_template_catalog()
        assert "vrrp" in catalog and "ospf" in catalog


class TestMessages:
    def test_tutor_mode_rule(self, tmp_config):
        save_config({"mode": "tutor"})
        msgs = build_messages("hello", [], "", task="free")
        assert msgs[0]["role"] == "system"
        assert "助教模式" in msgs[0]["content"]

    def test_engineer_mode_rule(self, tmp_config):
        save_config({"mode": "engineer"})
        msgs = build_messages("hello", [], "", task="free")
        assert "工程师模式" in msgs[0]["content"]

    def test_task_instruction_injected(self):
        msgs = build_messages("报错了", [], "", task="diagnose")
        assert "报错诊断" in msgs[0]["content"]

    def test_device_context_and_history(self):
        history = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"},
                   {"role": "system", "content": "should be dropped"}]
        msgs = build_messages("q2", history, "【设备】test", task="free")
        assert "【设备】test" in msgs[0]["content"]
        assert msgs[-1]["content"] == "q2"
        assert len(msgs) == 4  # system + 2 history + user
        assert all(m["role"] != "system" for m in msgs[1:])

    def test_history_window_truncated(self):
        history = [{"role": "user", "content": f"m{i}"} for i in range(30)]
        msgs = build_messages("latest", history, "", task="free")
        assert len(msgs) <= 1 + ai_assistant.MAX_HISTORY_MESSAGES + 1


_OSPF_ON = {"ospf": {"supported": True, "configured": True, "running": True}}


class TestLab:
    def test_experiment_catalog_complete(self):
        for eid, exp in LAB_EXPERIMENTS.items():
            assert exp["name"] and exp["goal"] and exp["checks"]

    def test_ospf_progress_all_pass(self):
        # 两台设备 OSPF 均已配置且邻居 Full
        caps = {"10.0.0.1": _cap_result(capabilities=_OSPF_ON),
                "10.0.0.2": _cap_result(capabilities=_OSPF_ON)}
        progress = evaluate_lab_progress("ospf_full", ["10.0.0.1", "10.0.0.2"], caps)
        assert progress["progress"] == 100
        assert all(c["passed"] for c in progress["checks"])

    def test_nat_role_check(self):
        caps = {"10.0.0.1": _cap_result(
            capabilities={"nat": {"supported": True, "configured": True, "running": True}},
            raw_outputs={"display current-configuration": _ROUTER_CFG})}
        progress = evaluate_lab_progress("nat_internet", ["10.0.0.1"], caps)
        assert progress["checks"][0]["passed"] is True

    def test_progress_partial(self):
        # 两台都已配置（✓），但只有一台邻居 Full（✗，需 2 台）
        caps = {
            "10.0.0.1": _cap_result(capabilities=_OSPF_ON),
            "10.0.0.2": _cap_result(capabilities={
                "ospf": {"supported": True, "configured": True, "running": False}}),
        }
        progress = evaluate_lab_progress("ospf_full", ["10.0.0.1", "10.0.0.2"], caps)
        configured_check = next(c for c in progress["checks"] if "已配置" in c["desc"])
        running_check = next(c for c in progress["checks"] if "Full" in c["desc"])
        assert configured_check["passed"] is True
        assert running_check["passed"] is False
        assert 0 < progress["progress"] < 100

    def test_no_cap_results(self):
        progress = evaluate_lab_progress("ospf_full", ["10.0.0.1"], {})
        assert progress["progress"] == 0

    def test_unknown_experiment(self):
        with pytest.raises(AIError):
            evaluate_lab_progress("nope", ["1.1.1.1"], {})

    def test_lab_context_render(self):
        caps = {
            "10.0.0.1": _cap_result(capabilities=_OSPF_ON),
            "10.0.0.2": _cap_result(capabilities={
                "ospf": {"supported": True, "configured": True, "running": False}}),
        }
        progress = evaluate_lab_progress("ospf_full", ["10.0.0.1", "10.0.0.2"], caps)
        text = ai_assistant.render_lab_context(progress)
        assert "【实验】" in text and "✓" in text and "✗" in text


# 路由器配置（与 test_role_detector 的 AR_CONFIG 一致的子集）
_ROUTER_CFG = """
#
sysname AR1
#
acl number 2000
 rule 5 permit source 192.168.100.0 0.0.0.255
#
interface GigabitEthernet0/0/1
 ip address 200.1.1.1 255.255.255.0
 nat outbound 2000
#
return
"""


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
