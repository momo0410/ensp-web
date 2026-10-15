"""
进程自提权（UAC）。

为什么需要：eNSP 在多数机房环境里是以**管理员权限**运行的。本项目要向它注入
控制模块（OpenProcess + CreateRemoteThread + ReadProcessMemory），调用方进程
必须具备**不低于 eNSP 的完整性级别**，否则 Windows 直接返回 ERROR_ACCESS_DENIED(5)，
注入、开机、加载拓扑全部失效。

因此启动早期检测自身权限：非管理员时用 ShellExecute 的 `runas` 动词重新拉起
自身（弹一次 UAC），原进程随即退出。已经是管理员则什么都不做。

降级策略：用户在 UAC 弹窗上点「否」时**不重试、不退出**，仅记录警告，
项目继续以普通权限运行——除 eNSP 自动注入外的功能均不受影响。
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from typing import List, Optional

# ShellExecuteW 返回值 > 32 表示成功
_SHELL_OK = 32
# 用户取消 UAC 弹窗
ERROR_CANCELLED = 1223


def is_admin() -> bool:
    """当前进程是否具备管理员令牌。非 Windows 平台一律视为已满足。"""
    if os.name != "nt":
        return True
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # pragma: no cover - 极端环境下 shell32 不可用
        return False


def _quote(arg: str) -> str:
    return '"%s"' % arg.replace('"', '\\"')


def relaunch_as_admin(argv: Optional[List[str]] = None) -> bool:
    """
    以 runas 动词重新拉起当前解释器，参数保持一致。

    返回 True 表示已成功发起提权请求（调用方应当立即退出当前进程，避免重复提权）。
    返回 False 表示未能发起（用户取消、平台不支持等）。
    """
    if os.name != "nt":
        return False

    args = list(sys.argv if argv is None else argv)
    exe = sys.executable or "python"
    # pythonw.exe 无控制台，提权后看不到日志，统一换成同目录的 python.exe
    if exe.lower().endswith("pythonw.exe"):
        candidate = exe[: -len("pythonw.exe")] + "python.exe"
        if os.path.exists(candidate):
            exe = candidate

    params = " ".join(_quote(a) for a in args)
    cwd = os.getcwd()

    try:
        shell32 = ctypes.windll.shell32
        shell32.ShellExecuteW.restype = wintypes.HINSTANCE
        shell32.ShellExecuteW.argtypes = [
            wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
            wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int,
        ]
        rc = shell32.ShellExecuteW(None, "runas", exe, params, cwd, 1)
    except Exception:
        return False

    return int(rc) > _SHELL_OK


def ensure_admin(enabled: bool = True) -> bool:
    """
    确保当前进程具备管理员权限。

    - 已是管理员：直接返回 True（无副作用）。
    - 非管理员且 enabled：发起 UAC 提权请求并**退出当前进程**（新进程接管）。
    - 提权请求失败（多数是用户取消）：返回 False，由调用方降级继续运行。
    """
    if not enabled:
        return is_admin()
    if is_admin():
        return True
    if relaunch_as_admin():
        # 新进程已由 UAC 拉起，当前进程必须让位，否则会出现两个实例
        sys.exit(0)
    return False
