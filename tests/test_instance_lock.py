"""单实例锁：同一工作目录只允许一个后端进程。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_second_instance_is_rejected(monkeypatch, tmp_path):
    """已持锁时，第二次 acquire 必须抛 AlreadyRunningError。"""
    from app import instance_lock

    monkeypatch.setattr(instance_lock, "LOCK_FILE", tmp_path / ".instance.lock")
    monkeypatch.setattr(instance_lock, "OWNER_FILE", tmp_path / ".instance.owner")
    monkeypatch.setattr(instance_lock, "_lock_handle", None)

    instance_lock.acquire_single_instance_lock()  # 本进程拿锁
    try:
        # 同进程再拿一次：filelock 的 ReentrantLock 语义会放行，
        # 所以这里另起一个子进程来验证跨进程排斥。
        import subprocess
        code = (
            "import sys; sys.path.insert(0, r'%s');"
            "from app import instance_lock as il;"
            "from pathlib import Path;"
            "il.LOCK_FILE = Path(r'%s');"
            "il.OWNER_FILE = Path(r'%s');"
            "il._lock_handle = None;"
            "il.acquire_single_instance_lock();"
            "print('ACQUIRED')"
        ) % (str(Path(__file__).resolve().parent.parent),
             str(tmp_path / ".instance.lock"),
             str(tmp_path / ".instance.owner"))
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
        assert "ACQUIRED" not in r.stdout, "第二个进程不应拿到锁"
        assert "AlreadyRunningError" in r.stderr or "已有一个实例" in r.stderr
    finally:
        instance_lock.release_single_instance_lock()


def test_lock_released_allows_reacquire(monkeypatch, tmp_path):
    """释放后可以重新获取。"""
    from app import instance_lock

    monkeypatch.setattr(instance_lock, "LOCK_FILE", tmp_path / ".instance.lock")
    monkeypatch.setattr(instance_lock, "OWNER_FILE", tmp_path / ".instance.owner")
    monkeypatch.setattr(instance_lock, "_lock_handle", None)

    instance_lock.acquire_single_instance_lock()
    assert (tmp_path / ".instance.owner").exists()
    instance_lock.release_single_instance_lock()
    assert not (tmp_path / ".instance.owner").exists()

    # 再次获取应当成功
    instance_lock.acquire_single_instance_lock()
    instance_lock.release_single_instance_lock()
