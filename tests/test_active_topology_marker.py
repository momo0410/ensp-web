"""激活拓扑标记（.current）的健壮性回归测试（离线）。

真实事故：激活拓扑在无人操作的情况下从 15 台设备的 `capability_probe`
变成了**空的** `campus`，前端表现为「设备表突然空了」。根因是两处叠加：

1. `set_active` 用非原子的 `write_text`（先截断再写），并发读只要撞上
   写入窗口就会读到空文件；
2. `active_topology` 在标记损坏时**把回退结果写回**（`set_active(tops[0])`），
   而 `list_topologies` 按名字排序 —— `campus` 字母序在前，于是「空拓扑」
   被永久钉死为激活项。

修法：写入改原子替换；读取只回退、不写盘。
"""

import json

import pytest

from domain import workspace as ws


@pytest.fixture
def ws_root(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr(ws, "get_workspace_root", lambda: root)
    return root


def make_topo(root, name, devices=None):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "configs").mkdir(exist_ok=True)
    (d / "devices.json").write_text(
        json.dumps(devices if devices is not None else []), encoding="utf-8"
    )
    (d / "links.json").write_text("[]", encoding="utf-8")
    return d


class TestActiveTopologyReadIsPure:
    def test_标记正常时按标记返回(self, ws_root):
        make_topo(ws_root, "capability_probe", [{"name": "R1"}])
        (ws_root / ".current").write_text("capability_probe", encoding="utf-8")
        assert ws.active_topology() == "capability_probe"

    def test_标记指向不存在的拓扑时只回退不写盘(self, ws_root):
        """回归：回退结果绝不能被持久化，否则会钉死错误的激活拓扑。"""
        make_topo(ws_root, "campus")            # 字母序在前，但它是空的
        make_topo(ws_root, "capability_probe", [{"name": "R1"}])
        (ws_root / ".current").write_text("deleted_topo", encoding="utf-8")

        result = ws.active_topology()
        assert result == "campus"               # 本次回退
        # 关键断言：标记**没有被改写**
        assert (ws_root / ".current").read_text(encoding="utf-8") == "deleted_topo"

    def test_标记为空文件时不会钉死空拓扑(self, ws_root):
        """回归：并发写入窗口里读到的空标记，不得被回退结果覆盖。"""
        make_topo(ws_root, "campus")
        make_topo(ws_root, "capability_probe", [{"name": "R1"}])
        (ws_root / ".current").write_text("", encoding="utf-8")
        ws.active_topology()
        assert (ws_root / ".current").read_text(encoding="utf-8") == ""

    def test_没有标记时不创建标记(self, ws_root):
        make_topo(ws_root, "only", [{"name": "R1"}])
        assert ws.active_topology() == "only"
        assert not (ws_root / ".current").exists()

    def test_无任何拓扑返回_none(self, ws_root):
        assert ws.active_topology() is None


class TestSetActiveIsAtomic:
    def test_写入后内容正确(self, ws_root):
        make_topo(ws_root, "t1")
        ws.set_active("t1")
        assert (ws_root / ".current").read_text(encoding="utf-8") == "t1"

    def test_原子替换不残留临时文件(self, ws_root):
        make_topo(ws_root, "t1")
        ws.set_active("t1")
        leftovers = [p.name for p in ws_root.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == [], f"不应残留临时文件: {leftovers}"

    def test_不存在的拓扑报错且不改标记(self, ws_root):
        make_topo(ws_root, "t1")
        ws.set_active("t1")
        with pytest.raises(ws.WorkspaceError):
            ws.set_active("nope")
        assert (ws_root / ".current").read_text(encoding="utf-8") == "t1"

    def test_反复切换内容始终完整(self, ws_root):
        """原子替换下，任意时刻读到的内容都是某个完整拓扑名。"""
        for name in ("alpha", "beta", "gamma"):
            make_topo(ws_root, name)
        allowed = {"alpha", "beta", "gamma"}
        for name in ("alpha", "beta", "gamma", "alpha"):
            ws.set_active(name)
            assert (ws_root / ".current").read_text(encoding="utf-8") in allowed


class TestDeleteTopologyActivatesRemainder:
    def test_删除激活拓扑后切到剩余项并落盘(self, ws_root):
        make_topo(ws_root, "a")
        make_topo(ws_root, "b")
        ws.set_active("b")
        ws.delete_topology("b")
        assert ws.active_topology() == "a"
        # 显式落盘，不留悬空标记
        assert (ws_root / ".current").read_text(encoding="utf-8") == "a"

    def test_删除非激活拓扑不影响标记(self, ws_root):
        make_topo(ws_root, "a")
        make_topo(ws_root, "b")
        ws.set_active("b")
        ws.delete_topology("a")
        assert (ws_root / ".current").read_text(encoding="utf-8") == "b"
