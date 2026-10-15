"""
agent_sidecar.py - Agent sidecar（Node / pi）生命周期与 RPC
=========================================================
Agent 循环跑在 Node 的 pi-agent-core 上（`agent_node/`），但**工具的实现在
Python**（设备表、Console、能力缓存、下发链路都在这边）。本模块负责：

1. 拉起 sidecar 进程，分配一个空闲回环端口与一次性共享密钥；
2. 健康探测与优雅关闭（随主进程退出）；
3. 暴露工具清单与工具执行两条内部 RPC，供 sidecar 回调。

安全模型（AGENTS.md 第六节）：
- sidecar 只监听 127.0.0.1，且每个请求都要带 `X-Agent-Token`
  （进程启动时随机生成，只在本进程内存与子进程环境里存在）。
- sidecar **不持有任何下发能力**：工具清单由这里给出，而这里只有
  read / prepare 两类；真正下发仍走既有确认闸门。
- 拉起失败一律降级为可读错误，不拖垮主服务。
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("ensp-web.agent.sidecar")

BASE_DIR = Path(__file__).resolve().parent.parent
SIDECAR_DIR = BASE_DIR / "agent_node"
ENTRY = SIDECAR_DIR / "server.mjs"


class SidecarError(Exception):
    """sidecar 相关可预期错误。"""


def _free_loopback_port() -> int:
    """要一个空闲回环端口（先绑 0 再释放，交给 Node 监听）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def node_available() -> Optional[str]:
    """返回 node 可执行路径；不可用返回 None。"""
    exe = shutil.which("node")
    return exe


def sidecar_ready() -> Dict[str, Any]:
    """检查 sidecar 是否具备运行条件（不启动进程）。"""
    node = node_available()
    deps = (SIDECAR_DIR / "node_modules" / "@earendil-works" / "pi-agent-core").exists()
    return {
        "node": node,
        "node_ok": bool(node),
        "entry_ok": ENTRY.exists(),
        "deps_ok": deps,
        "ready": bool(node) and ENTRY.exists() and deps,
    }


class AgentSidecar:
    """单例 sidecar 管理器（懒启动、可复用、进程退出时清理）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._port: Optional[int] = None
        self._token: str = secrets.token_urlsafe(32)
        self._started_at: float = 0.0
        self._last_error: str = ""

    # -- 状态 ---------------------------------------------------------------
    @property
    def token(self) -> str:
        return self._token

    @property
    def port(self) -> Optional[int]:
        return self._port

    def base_url(self) -> Optional[str]:
        return f"http://127.0.0.1:{self._port}" if self._port else None

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def status(self) -> Dict[str, Any]:
        ready = sidecar_ready()
        return {
            "running": self.is_running(),
            "port": self._port,
            "pid": self._proc.pid if self._proc else None,
            "uptime_s": int(time.time() - self._started_at) if self._started_at else 0,
            "last_error": self._last_error,
            **ready,
        }

    # -- 生命周期 -----------------------------------------------------------
    def ensure_started(self, python_base: str = "http://127.0.0.1:8000",
                       timeout: float = 20.0) -> Dict[str, Any]:
        """确保 sidecar 在跑；返回状态。失败抛 SidecarError。"""
        with self._lock:
            if self.is_running():
                return self.status()

            ready = sidecar_ready()
            if not ready["ready"]:
                missing = []
                if not ready["node_ok"]:
                    missing.append("未找到 node 可执行文件")
                if not ready["entry_ok"]:
                    missing.append(f"缺少入口 {ENTRY}")
                if not ready["deps_ok"]:
                    missing.append("未安装 pi 依赖（在 agent_node/ 下执行 npm install）")
                raise SidecarError("Agent sidecar 不可用：" + "；".join(missing))

            port = _free_loopback_port()
            env = dict(os.environ)
            env.update({
                "AGENT_PORT": str(port),
                "AGENT_TOKEN": self._token,
                "AGENT_PYTHON_BASE": python_base,
                # 父进程看护：Python 被强杀时 shutdown 钩子不执行，
                # sidecar 靠轮询这个 pid 判断是否该自行退出，避免孤儿进程。
                "AGENT_PARENT_PID": str(os.getpid()),
            })
            creationflags = 0
            if os.name == "nt":
                # 不弹窗；且不让 sidecar 随父控制台信号一起死
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

            logger.info("启动 Agent sidecar（port=%s）", port)
            try:
                proc = subprocess.Popen(
                    [ready["node"], str(ENTRY)],
                    cwd=str(SIDECAR_DIR),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=creationflags,
                )
            except OSError as e:
                self._last_error = f"启动失败: {e}"
                raise SidecarError(self._last_error) from e

            self._proc = proc
            self._port = port
            self._started_at = time.time()

            # 等健康探测通过
            deadline = time.time() + timeout
            last = ""
            while time.time() < deadline:
                if proc.poll() is not None:
                    out = ""
                    try:
                        out = (proc.stdout.read() if proc.stdout else "") or ""
                    except Exception:
                        pass
                    self._proc = None
                    self._last_error = f"sidecar 提前退出（code={proc.returncode}）：{out[:500]}"
                    raise SidecarError(self._last_error)
                try:
                    r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2.0)
                    if r.status_code == 200:
                        logger.info("Agent sidecar 就绪（port=%s pid=%s）", port, proc.pid)
                        return self.status()
                except Exception as e:
                    last = str(e)
                time.sleep(0.3)

            self._last_error = f"sidecar 健康探测超时：{last}"
            self.stop()
            raise SidecarError(self._last_error)

    def stop(self, timeout: float = 5.0) -> None:
        """优雅停止 sidecar。"""
        with self._lock:
            proc, self._proc = self._proc, None
            self._port = None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
        except Exception as e:  # 清理失败不该影响主流程
            logger.warning("停止 sidecar 失败: %s", e)
        finally:
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass


# 进程级单例
sidecar = AgentSidecar()
