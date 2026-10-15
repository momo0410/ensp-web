"""设备表乐观锁：版本号随写入递增，过期版本被拒绝。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """把 device_store 指向临时目录。"""
    from domain import device_store as ds
    from domain import workspace as ws

    monkeypatch.setattr(ws, "get_workspace_root", lambda: tmp_path)
    ds._migrated = True  # 跳过迁移
    monkeypatch.setattr(ds, "_DEFAULT_TOPOLOGY", "t")
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "devices.json").write_text("[]", encoding="utf-8")
    (tmp_path / ".current").write_text("t", encoding="utf-8")
    return ds


def test_revision_increases_on_save(store):
    assert store.current_revision() == 0
    store.save_devices([{"name": "R1", "ip": "127.0.0.1:2000"}])
    assert store.current_revision() == 1
    store.save_devices([{"name": "R2", "ip": "127.0.0.1:2001"}])
    assert store.current_revision() == 2


def test_check_revision_accepts_none(store):
    store.save_devices([])
    store.check_revision(None)  # 不校验 → 不抛


def test_check_revision_rejects_stale(store):
    store.save_devices([])  # rev = 1
    with pytest.raises(store.ConflictError):
        store.check_revision(999)


def test_check_revision_accepts_current(store):
    store.save_devices([])  # rev = 1
    store.check_revision(store.current_revision())  # 应通过
