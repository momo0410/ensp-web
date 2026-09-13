"""
test_live_check.py - 设备侧 ping 解析单元测试（真实 VRP 回显样例，零网络）
"""

import sys
from pathlib import Path


from services.live_check import parse_ping_output, ping_from_device

VRP_PING_OK = """PING 192.168.56.13: 56  data bytes, press CTRL_C to break
Reply from 192.168.56.13: bytes=56 Sequence=1 ttl=255 time=1 ms
Reply from 192.168.56.13: bytes=56 Sequence=2 ttl=255 time=1 ms
Reply from 192.168.56.13: bytes=56 Sequence=3 ttl=255 time=2 ms
Reply from 192.168.56.13: bytes=56 Sequence=4 ttl=255 time=1 ms

--- 192.168.56.13 ping statistics ---
  4 packet(s) transmitted
  4 packet(s) received
  0.00% packet loss
round-trip min/avg/max = 1/1/2 ms
"""

VRP_PING_FAIL = """PING 10.99.99.99: 56  data bytes, press CTRL_C to break
Request time out
Request time out

--- 10.99.99.99 ping statistics ---
  2 packet(s) transmitted
  0 packet(s) received
  100.00% packet loss
"""


class TestParse:
    def test_full_success(self):
        s = parse_ping_output(VRP_PING_OK)
        assert s["transmitted"] == 4 and s["received"] == 4
        assert s["loss_rate"] == 0.0
        assert s["avg_ms"] == 1 and s["max_ms"] == 2

    def test_timeout(self):
        s = parse_ping_output(VRP_PING_FAIL)
        assert s["transmitted"] == 2 and s["received"] == 0
        assert s["loss_rate"] == 100.0
        assert s["avg_ms"] is None

    def test_empty(self):
        s = parse_ping_output("")
        assert s["received"] == 0 and s["loss_rate"] is None


class TestTargetValidation:
    def test_reject_injection(self):
        r = ping_from_device({"ip": "1.1.1.1"}, "10.0.0.1; reboot")
        assert r["success"] is False and "非法目标" in r["error"]

    def test_reject_bad_source(self):
        r = ping_from_device({"ip": "1.1.1.1"}, "10.0.0.1", source="a b")
        assert r["success"] is False and "非法源地址" in r["error"]

    def test_count_clamped(self, monkeypatch):
        captured = {}

        class FakeConn:
            def send_command_timing(self, cmd, **kw):
                captured["cmd"] = cmd
                return VRP_PING_OK

            def __enter__(self):
                return self

        from services import live_check
        import netmiko

        class FakeHandler(FakeConn):
            def __init__(self, **kw):
                pass

        monkeypatch.setattr(live_check, "_connect", None, raising=False)
        # 直接打桩 import_netmiko
        monkeypatch.setattr(
            "services.device_connector.import_netmiko",
            lambda: (FakeHandler, Exception, Exception),
        )
        r = ping_from_device({"ip": "1.1.1.1"}, "10.0.0.1", count=999)
        assert "-c 20" in captured["cmd"]  # 999 → 钳位到 20
        assert r["success"] is True
