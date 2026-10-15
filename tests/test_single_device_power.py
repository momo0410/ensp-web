"""domain.ensp_runtime.start_stop_device 的离线单测（需求③：单台启停）。

不碰真机：monkeypatch 掉 ensure_ensp_injected / send_ensp_ctrl_cmd /
read_ensp_topology，验证选择器映射、命令拼装与状态确认逻辑。
"""
import pytest

from domain import ensp_runtime as er


@pytest.fixture
def devices(monkeypatch):
    devs = [
        {"name": "R1", "ip": "127.0.0.1:2000", "console_port": 2000},
        {"name": "SW1", "ip": "127.0.0.1:2001", "console_port": 2001},
    ]
    monkeypatch.setattr("domain.device_store.load_devices", lambda: devs)
    return devs


@pytest.fixture
def fake_channel(monkeypatch):
    """假造一条可用的 DLL 控制通道，并记录下发的命令。"""
    sent = []
    monkeypatch.setattr(er, "ensure_ensp_injected",
                        lambda launch_if_missing=True: {"ok": True, "pid": 1})
    monkeypatch.setattr(er, "send_ensp_ctrl_cmd",
                        lambda cmd, **kw: (sent.append(cmd), {"ok": True})[1])
    return sent


def _fake_live(monkeypatch, name, state):
    monkeypatch.setattr(er, "read_ensp_topology",
                        lambda: {"ok": True, "live_devices": [{"name": name, "power_state": state}]})


class TestSelector:
    def test_IP_映射为设备名(self, monkeypatch, devices, fake_channel):
        monkeypatch.setattr(er, "read_ensp_topology",
                            lambda: {"ok": True, "live_devices": []})
        res = er.start_stop_device("127.0.0.1:2000", "start")
        assert res["ok"] is True
        assert res["device"] == "R1"
        assert fake_channel == ["start_dev:R1"]

    def test_直接传设备名(self, monkeypatch, devices, fake_channel):
        monkeypatch.setattr(er, "read_ensp_topology",
                            lambda: {"ok": True, "live_devices": []})
        er.start_stop_device("SW1", "stop")
        assert fake_channel == ["stop_dev:SW1"]

    def test_设备表里没有则报错(self, monkeypatch, devices, fake_channel):
        res = er.start_stop_device("127.0.0.1:9999", "start")
        assert res["ok"] is False
        assert "找不到" in res["error"]
        assert fake_channel == []


class TestCommand:
    def test_stop_指令(self, monkeypatch, devices, fake_channel):
        monkeypatch.setattr(er, "read_ensp_topology",
                            lambda: {"ok": True, "live_devices": []})
        res = er.start_stop_device("R1", "stop")
        assert fake_channel == ["stop_dev:R1"]
        assert res["action"] == "stop"

    def test_未知动作(self, monkeypatch, devices, fake_channel):
        res = er.start_stop_device("R1", "reboot")
        assert res["ok"] is False
        assert fake_channel == []

    def test_通道不可用(self, monkeypatch, devices):
        monkeypatch.setattr(er, "ensure_ensp_injected",
                            lambda launch_if_missing=True: {"ok": False, "error": "eNSP 未运行"})
        res = er.start_stop_device("R1", "start")
        assert res["ok"] is False
        assert "eNSP 未运行" in res["error"]

    def test_DLL_报错透传(self, monkeypatch, devices):
        monkeypatch.setattr(er, "ensure_ensp_injected",
                            lambda launch_if_missing=True: {"ok": True})
        monkeypatch.setattr(er, "send_ensp_ctrl_cmd",
                            lambda cmd, **kw: {"ok": False, "error": "device not found"})
        res = er.start_stop_device("R1", "start")
        assert res["ok"] is False
        assert "device not found" in res["error"]


class TestStateConfirm:
    def test_等待到_running(self, monkeypatch, devices, fake_channel):
        _fake_live(monkeypatch, "R1", "running")
        res = er.start_stop_device("R1", "start", wait=True)
        assert res["power_state"] == "running"
        assert "已开机" in res["message"]

    def test_等待不到就如实说(self, monkeypatch, devices, fake_channel):
        _fake_live(monkeypatch, "R1", "starting")
        monkeypatch.setattr(er.time, "sleep", lambda s: None)
        res = er.start_stop_device("R1", "start", wait=True)
        assert res["ok"] is True          # 指令确实下发了
        assert res["power_state"] == "starting"
        assert "可能需要更长时间" in res["message"]

    def test_不等待时立即返回(self, monkeypatch, devices, fake_channel):
        _fake_live(monkeypatch, "R1", "starting")
        res = er.start_stop_device("R1", "start", wait=False)
        assert "power_state" not in res
        assert "已向 eNSP 下发开机指令" in res["message"]

    def test_确认的是同名设备(self, monkeypatch, devices, fake_channel):
        """live 里多台设备时，只认目标那台的状态。"""
        monkeypatch.setattr(er, "read_ensp_topology", lambda: {"ok": True, "live_devices": [
            {"name": "SW1", "power_state": "running"},
            {"name": "R1", "power_state": "running"},
        ]})
        res = er.start_stop_device("R1", "start")
        assert res["power_state"] == "running"
