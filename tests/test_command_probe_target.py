"""
命令探测（? 树）的回归测试。

覆盖两个真机暴露过、且都会让 ? 探测"静默失败"的缺陷：
1. target 键名不一致：路由层传 console_port，探测层读 port → int(None) 抛错被吞，
   表现为 keywords=0，没有任何报错。
2. Console 命令用 \\r\\n 而非裸 \\r：VRP 控制台只认 \\r，\\r\\n 会让首条命令被吞。
"""

import pytest

from services import command_probe as cp


# ---------------------------------------------------------------------------
# 1. target 归一化
# ---------------------------------------------------------------------------
def test_normalize_target_accepts_console_port():
    t = cp._normalize_target({"ip": "127.0.0.1:2000", "console_port": 2000})
    assert t["port"] == 2000
    assert t["console_port"] == 2000
    assert t["mode"] == "console"


def test_normalize_target_accepts_legacy_port():
    t = cp._normalize_target({"mode": "console", "port": 2010})
    assert t["port"] == 2010
    assert t["console_port"] == 2010


def test_normalize_target_ssh_only():
    t = cp._normalize_target({"ip": "10.0.0.1"})
    assert t.get("port") is None
    assert t["mode"] == "ssh"


def test_probe_device_commands_uses_console_port_without_crashing(monkeypatch):
    """路由层传的是 console_port；探测不得因键名不匹配而静默失败。"""
    seen = {}

    def fake_probe(target, timeout=None, read_timeout=None):
        seen.update(target)
        return "display version: AR1220-S", {"display": {"ospf", "bgp"}, "system": {"ospf"}}

    monkeypatch.setattr(cp, "probe_via_console", fake_probe)
    res = cp.probe_device_commands({"ip": "1.2.3.4", "console_port": 2000}, force=True)

    assert seen.get("port") == 2000, "归一化后必须能拿到 port"
    assert res["keyword_count"] > 0
    assert not res["errors"]


def test_probe_via_console_falls_back_to_display_device(monkeypatch):
    """display version 被截断（无型号）时，应回退到 display device 取型号。"""

    class FakeConn:
        def send_command(self, cmd, read_timeout=5.0):
            return ""

        def send_command_timing(self, cmd, read_timeout=5.0):
            if cmd == "display version":
                # 模拟 eNSP 分段截断：只有前三行，没有型号
                return ("Huawei Versatile Routing Platform Software\n"
                        "VRP (R) software, Version 5.110\n"
                        "Copyright (c) 2011 HUAWEI TECH CO., LTD\n<Huawei>")
            if cmd == "display device":
                return "1  AR1220-S  Present\n<Huawei>"
            return "<Huawei>"

        def disconnect(self):
            pass

    monkeypatch.setattr(cp, "open_console_session", lambda port: FakeConn())
    version, _views = cp.probe_via_console({"console_port": 2000})

    assert "AR1220-S" in version, "型号应来自 display device 回退"
    assert cp.detect_device_type(version) == "router"


def test_read_until_prompt_stops_at_prompt():
    class FakeConn:
        def __init__(self):
            self.calls = 0

        def send_command_timing(self, cmd, read_timeout=5.0):
            self.calls += 1
            if cmd == "":
                return "<Huawei>"
            return "line1\nline2\n<Huawei>"

    conn = FakeConn()
    out = cp.read_until_prompt(conn, "display version")
    assert "line1" in out and "line2" in out
    assert conn.calls == 1, "首段已带提示符，不应再读"


def test_probe_via_console_requires_port():
    with pytest.raises(ValueError):
        cp.probe_via_console({"ip": "1.2.3.4"})


def test_probe_via_console_opens_session_on_console_port(monkeypatch):
    """验证拿到的是 console_port 指向的端口。"""
    opened = {}

    class FakeConn:
        def send_command(self, cmd, read_timeout=5.0):
            if cmd == "display ?":
                return "  ospf   OSPF information\n  bgp    BGP information"
            if cmd == "display interface brief":
                return "GigabitEthernet0/0/0  up"
            if cmd == "display version":
                return "AR1220-S  Version 5.110"
            return ""

        def send_command_timing(self, cmd, read_timeout=5.0):
            """保留提示符（真实连接里 prompt 不会被 strip）。"""
            body = self.send_command(cmd, read_timeout=read_timeout)
            prompt = "[Huawei]" if cmd == "system-view" else "<Huawei>"
            return f"{body}\n{prompt}" if body else prompt

        def disconnect(self):
            opened["closed"] = True

    def fake_open(port):
        opened["port"] = port
        return FakeConn()

    monkeypatch.setattr(cp, "open_console_session", fake_open)
    version, views = cp.probe_via_console({"console_port": 2007})

    assert opened["port"] == 2007
    assert "ospf" in views["display"]
    assert "AR1220-S" in version


# ---------------------------------------------------------------------------
# 2. 命令结尾必须是裸 \r
# ---------------------------------------------------------------------------
class _FakeSock:
    def __init__(self):
        self.sent = []
        self.payload = b"display ?   \r\n  ospf   OSPF information\r\n<Huawei>"

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, n):
        if self.payload:
            out, self.payload = self.payload, b""
            return out
        raise TimeoutError

    def settimeout(self, t):
        pass


def test_console_send_uses_bare_cr():
    """\\r\\n 会让 VRP 吞掉首条命令——这是真机踩过的坑。"""
    sock = _FakeSock()
    cp._console_send(sock, "display ?", read_timeout=0.5)
    assert sock.sent, "应当真的发出命令"
    assert sock.sent[0].endswith(b"\r"), f"必须以裸 \\r 结尾，实际: {sock.sent[0]!r}"
    assert b"\r\n" not in sock.sent[0], "不得出现 \\r\\n"


def test_console_connect_wakes_device():
    """连上后必须发唤醒字节，否则停在登录提示上，首条命令被吞。"""
    sock = _FakeSock()
    sock.payload = b"\r\n<Huawei>"
    import socket as _socket
    created = {}

    def fake_create(addr, timeout=None):
        created["addr"] = addr
        return sock

    orig = _socket.create_connection
    _socket.create_connection = fake_create
    try:
        cp._console_connect(2000)
    finally:
        _socket.create_connection = orig

    assert created["addr"] == ("127.0.0.1", 2000)
    assert any(b"\x03" in s or s == b"\r" or s.endswith(b"\r") for s in sock.sent), \
        "必须发送唤醒字节"
