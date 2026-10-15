"""
ensp_guard.py - eNSP 看护（需求①反向：eNSP 启动 → 连带拉起项目）
==================================================================
eNSP 是闭源 GUI，没有插件/回调机制，做不到事件级 hook。可行替代是
**轮询进程是否存在**：发现 eNSP_Client.exe 起来了，就确认本后端仍在跑
（后端当然在跑——看护线程就在后端里），并把控制模块注入进去，
让"用户在 eNSP 侧开机/开拓扑"这条链路立刻可用。

> 说明：反向联动的另一半「eNSP 启动 → 拉起后端进程」无法在后端内部实现
> （后端已经是被拉起的一方）。那需要外部手段：启动器快捷方式（先起后端
> 再起 eNSP）或计划任务看护。tools/make_launcher.py 提供前者的生成入口。

设计约束：
- 看护线程是 daemon，绝不阻塞启动；异常只记日志，不影响服务其余能力。
- 受设置项 `watch_ensp_topology` 控制（默认开）。
- 只在**状态变化时**动作（没注入 → 注入一次），不做无谓的重复注入。
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from app.config import logger

POLL_INTERVAL = 5.0            # 秒；eNSP 启动通常要十几秒，5s 足够灵敏
INJECT_GRACE = 3.0             # 发现进程后等一会再注入，等主视图就绪


class EnspGuard:
    """单例看护器：后台轮询 eNSP 进程，出现则注入控制模块。"""

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._injected_pid = 0
        self._on_ensp_up: Optional[Callable[[], None]] = None

    # -- 生命周期 ---------------------------------------------------------
    def start(self, on_ensp_up: Optional[Callable[[], None]] = None) -> bool:
        """启动看护线程；已在跑则返回 False。"""
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._on_ensp_up = on_ensp_up
        self._thread = threading.Thread(target=self._loop, name="ensp-guard", daemon=True)
        self._thread.start()
        logger.info("eNSP 看护已启动（每 %.0fs 检查一次）", POLL_INTERVAL)
        return True

    def stop(self) -> None:
        self._stop.set()

    # -- 内部 -------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:  # 看护崩了不能拖垮服务
                logger.warning("eNSP 看护检查异常: %s", e)
            self._stop.wait(POLL_INTERVAL)

    def _tick(self) -> None:
        from domain import workspace as ws
        if not ws.get_setting("watch_ensp_topology", True):
            return

        from domain.ensp_runtime import ensure_ensp_injected, find_process_pid, is_dll_loaded

        pid = find_process_pid("eNSP_Client.exe")
        if not pid:
            # eNSP 被关掉了：清掉记忆，下次起来重新注入
            if self._injected_pid:
                logger.info("eNSP 已退出，看护待下次启动")
                self._injected_pid = 0
            return

        if self._injected_pid == pid:
            return

        # 进程在但没注入控制模块 → 等主视图就绪后注入一次
        if is_dll_loaded(pid, "ensp_control.dll"):
            self._injected_pid = pid
            logger.info("检测到 eNSP (pid=%d) 且控制模块已加载", pid)
            self._notify_up()
            return

        logger.info("检测到 eNSP 启动 (pid=%d)，准备注入控制模块", pid)
        time.sleep(INJECT_GRACE)
        res = ensure_ensp_injected(launch_if_missing=False)
        if res.get("ok"):
            self._injected_pid = pid
            logger.info("eNSP 控制模块注入成功 (pid=%d)，联动已就绪", pid)
            self._notify_up()
        else:
            # 注入失败不反复重试到死：等下一轮（进程可能还在初始化）
            logger.warning("eNSP 控制模块注入失败: %s", res.get("error"))

    def _notify_up(self) -> None:
        if self._on_ensp_up:
            try:
                self._on_ensp_up()
            except Exception as e:
                logger.warning("eNSP 就绪回调异常: %s", e)


# 进程内唯一实例（与 app.state 的角色一致：全进程一份真身）
ensp_guard = EnspGuard()
