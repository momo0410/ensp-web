"""
单实例保护。

项目的工作目录（拓扑、设备表、连线、设备配置）是**共享可变状态**：
同时跑两个后端进程，两边都会读写同一批 JSON，互相覆盖——实测出现过设备串到
另一个拓扑里的情况。因此本进程在启动时就要拿到一个排他锁，拿不到就退出。

实现方式：在项目根目录放一个 `.instance.lock`，用 filelock 的**非阻塞**加锁。
filelock 是跨进程的（Windows 下基于 msvcrt 文件锁），所以 python main.py
和 uvicorn main:app 两条启动路径都会被拦住。

锁文件里写入当前进程的 pid 与启动时间，便于用户排查"是谁占着"。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from app.config import BASE_DIR, logger

LOCK_FILE = BASE_DIR / ".instance.lock"          # filelock 的锁文件
OWNER_FILE = BASE_DIR / ".instance.owner"         # 记录占用者，便于排查

# 进程存活期间必须一直持有的锁对象（模块级，防止被 GC 释放）
_lock_handle = None


class AlreadyRunningError(RuntimeError):
    """已有另一个实例在运行。"""


def acquire_single_instance_lock() -> None:
    """
    获取单实例锁；已被占用则抛 AlreadyRunningError。

    timeout=0 → 非阻塞：立刻判断，不等待。
    """
    global _lock_handle
    try:
        from filelock import FileLock, Timeout
    except ImportError:
        logger.warning("未安装 filelock，跳过单实例检查（多开会损坏工作目录数据）")
        return

    lock = FileLock(str(LOCK_FILE), timeout=0)
    try:
        lock.acquire()
    except Timeout as e:
        owner = _read_owner()
        raise AlreadyRunningError(
            f"已有一个实例在运行（{owner}）；"
            "同一工作目录同时只能有一个后端进程，否则拓扑/设备数据会互相覆盖；"
            f"请先关闭另一个窗口，或删除 {LOCK_FILE} 与 {OWNER_FILE}（确认没有实例在跑时）"
        ) from e

    _lock_handle = lock
    # filelock 的锁文件由它自己管理，占用者信息另写一个文件便于排查
    try:
        from datetime import datetime
        OWNER_FILE.write_text(
            f"pid={os.getpid()}  started={datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            encoding="utf-8",
        )
    except OSError:
        pass
    logger.info("已获取单实例锁 (pid=%s)", os.getpid())


def release_single_instance_lock() -> None:
    """释放锁并删除锁文件（进程正常退出时调用）。"""
    global _lock_handle
    if _lock_handle is None:
        return
    try:
        _lock_handle.release()
    except Exception:
        pass
    _lock_handle = None
    for f in (LOCK_FILE, OWNER_FILE):
        try:
            f.unlink(missing_ok=True)
        except OSError:
            pass


def _read_owner() -> str:
    """读出占用者描述；若记录的 pid 已不存在，标注为残留记录。

    锁本身由 filelock 的 OS 级锁保证，进程被强杀后会自动释放；
    但 `.instance.owner` 是普通文本文件，会留在磁盘上。启动时若恰好
    撞上另一进程正在抢锁的窗口，读到的可能是上一轮异常退出的残留，
    这时候如果照原样报"已有一个实例在运行（pid=13372）"，用户会去追一个
    根本不存在的进程。这里做一次存活校验，把话说清。
    """
    try:
        raw = OWNER_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "另一进程（占用者信息不可读）"
    pid = _parse_pid(raw)
    if pid is not None and not _pid_alive(pid):
        return f"{raw}（该 pid 已退出，属上次异常退出的残留记录）"
    return raw


def _parse_pid(text: str):
    """从 `pid=123  started=...` 里取出 pid；取不到返回 None。"""
    import re

    match = re.search(r"pid=(\d+)", text)
    return int(match.group(1)) if match else None


def _pid_alive(pid: int) -> bool:
    """pid 是否仍然存在。优先用 psutil（已是依赖），缺失时保守认为存活。"""
    try:
        import psutil
    except ImportError:
        return True
    try:
        return psutil.pid_exists(pid)
    except Exception:
        return True


def fail_fast_if_already_running() -> None:
    """
    供 `python main.py` 直接启动时使用：拿不到锁就打印提示并退出。

    用 uvicorn 启动时由 FastAPI 的 startup 钩子调用另一条路径（见 acquire 的调用方），
    因为那时才拿得到应用生命周期。
    """
    try:
        acquire_single_instance_lock()
    except AlreadyRunningError as e:
        print(f"\n[启动失败] {e}\n", file=sys.stderr)
        raise SystemExit(1) from e
