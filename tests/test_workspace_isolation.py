"""回归：跑测试绝不能污染真实工作区。

背景：tests/test_ai_assistant.py 里添加设备的用例只 monkeypatch 了
routes_devices 上的 load/save_devices，但 add_device 之后会调用
sync_and_start_ensp_topo()，而它在函数体内部 `from domain.device_store import
save_devices` —— 拿到的是**真实**实现，绕过了 monkeypatch。加上
DeviceAdd 没有 uuid 字段，_ensure_device_uuids() 必然返回 True，
于是真实工作区的 devices.json 被覆盖成测试用的假设备（R_TEST），
.topo 也被 generate() 重写成单台设备。

修复两处：
1. sync_and_start_ensp_topo() 的 PYTEST_CURRENT_TEST 守卫前置到所有写操作之前
2. tests/conftest.py 增加 autouse 的临时工作区隔离
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _real_workspace_signature() -> dict:
    """真实工作区当前所有 devices.json 的内容指纹。"""
    from domain import workspace as ws

    root = ws.DEFAULT_WORKSPACE_ROOT
    if not root.exists():
        return {}
    return {
        str(p): (p.stat().st_size, p.read_bytes())
        for p in sorted(root.glob("*/devices.json"))
    }


def test_run_add_device_does_not_touch_real_workspace(monkeypatch):
    """添加设备的完整链路不得写入真实工作区。"""
    from app import config as cfg

    before = _real_workspace_signature()
    if not before:
        pytest.skip("真实工作区为空，无需校验（CI 环境）")

    from fastapi.testclient import TestClient
    import main
    import routes_devices

    # 故意只 patch 路由层的读写（复现当年出事时的写法），
    # 以此验证下游的 sync_and_start_ensp_topo 也不会漏出去写真实文件。
    store = []
    monkeypatch.setattr(routes_devices, "load_devices", lambda: list(store))
    monkeypatch.setattr(
        routes_devices, "save_devices", lambda devs: (store.clear(), store.extend(devs))
    )

    client = TestClient(main.app)
    resp = client.post(
        "/api/devices", json={"name": "R_TEST", "model": "Router", "auto_start": True}
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True

    assert _real_workspace_signature() == before, "真实工作区被测试污染了"


def test_sync_and_start_ensp_topo_is_dry_run_under_pytest(tmp_path, monkeypatch):
    """pytest 环境下 sync_and_start_ensp_topo 只做内存计算，不落任何文件。"""
    from domain import ensp_runtime

    out_dir = tmp_path / "should_stay_empty"
    out_dir.mkdir()
    monkeypatch.setattr(ensp_runtime, "_topo_dir", lambda: out_dir)

    res = ensp_runtime.sync_and_start_ensp_topo(
        [{"name": "R1", "model": "Router", "console_port": 2000}],
        links=[],
        topo_name="probe",
        auto_start=True,
    )
    assert res["ok"] is True
    assert res["mode"] == "pytest_dry_run"
    assert list(out_dir.iterdir()) == [], "pytest 环境下不该写出 .topo"
