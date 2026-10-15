"""
eNSP 本地 Console 端口（127.0.0.1:<com_port>）通用连接适配器。

提供设备 CLI 会话接口（find_prompt / send_command / send_command_timing /
send_config_set / save_config），支持：
1. 自动等待设备度过 '###' 引导阶段
2. 自动应答 USG5500 防火墙默认 Console 登录（admin / Admin@123）及密码修改询问（Y/N -> n）
3. 自动关闭分页（screen-length 0 temporary）与清理 ANSI 控制序列
4. 端口级线程互斥锁（避免多线程并发读写同一串口导致回显串扰）
"""

from __future__ import annotations

import re
import socket
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from services.device_connector import get_cli_error, strip_ansi

_PORT_LOCKS: Dict[int, threading.Lock] = {}
_GLOBAL_LOCK = threading.Lock()

PROMPT_RE = re.compile(r"[<\[]([^<>\[\]\r\n]+)[>\]]")

# VRP 提示符的视图层级（括号形状 + 后缀共同决定）：
#   <Huawei>                          用户视图
#   [Huawei]                          系统视图
#   [Huawei-GigabitEthernet0/0/1]     接口视图
#   [Huawei-ospf-1]                   协议进程视图
#   [Huawei-vlan10]                   VLAN 视图
# 只取方括号内的文本（旧 parse_prompt 的做法）会把接口名/进程名当成主机名，
# 视图层级就丢了——而"现在到底在哪一层"直接决定一条命令会不会被执行。
@dataclass(frozen=True)
class CliView:
    name: str                       # 提示符里的原始名字，如 Huawei-GigabitEthernet0/0/1
    kind: str                       # user / system / interface / protocol / vlan / unknown
    is_config: bool                 # 是否处于可下发配置的视图（系统及以下）
    context: str                    # 接口名 / 进程名 / VLAN 号；用户与系统视图为空


def parse_view(text: str) -> Optional[CliView]:
    """从一段输出里识别**最后**一个提示符所处的视图。"""
    if not text:
        return None
    last = None
    for m in re.finditer(r"([<\[])([^<>\[\]\r\n]+)([>\]])", text):
        last = m
    if last is None:
        return None
    opener, name, closer = last.group(1), last.group(2).strip(), last.group(3)

    if opener == "<" and closer == ">":
        return CliView(name=name, kind="user", is_config=False, context="")

    # 方括号：按 "-" 后缀判断进入了哪一层。
    # 注意判断顺序：Vlanif 也是 "vlan" 开头，但它是三层接口而非 VLAN 视图。
    base, sep, suffix = name.partition("-")
    if not sep:
        return CliView(name=name, kind="system", is_config=True, context="")
    lowered = suffix.lower()
    if lowered.startswith(("vlanif", "gigabit", "ethernet", "serial", "eth", "ge", "meht",
                           "loopback", "null")):
        return CliView(name=name, kind="interface", is_config=True, context=suffix)
    if lowered.startswith("vlan"):
        return CliView(name=name, kind="vlan", is_config=True, context=suffix)
    return CliView(name=name, kind="protocol", is_config=True, context=suffix)


def get_port_lock(port: int) -> threading.Lock:
    with _GLOBAL_LOCK:
        if port not in _PORT_LOCKS:
            _PORT_LOCKS[port] = threading.Lock()
        return _PORT_LOCKS[port]


def parse_prompt(text: str) -> str:
    matches = PROMPT_RE.findall(text or "")
    return matches[-1].strip() if matches else ""


def wait_for_console_ready(
    port: int,
    timeout: float = 150.0,
    poll_interval: float = 2.0,
    default_user: str = "admin",
    default_pass: str = "Admin@123",
) -> dict:
    """
    等待指定 Console 端口完成系统引导并进入可交互的 VRP 提示符状态。
    自动处理 USG5500 防火墙的 Username/Password 登录及密码修改提示。
    """
    deadline = time.time() + timeout
    last_text = ""
    lock = get_port_lock(int(port))

    while time.time() < deadline:
        sock = None
        with lock:
            try:
                sock = socket.create_connection(("127.0.0.1", int(port)), timeout=1.5)
                sock.settimeout(0.6)
                sock.sendall(b"\r\n")
                time.sleep(0.25)
                raw = _recv_raw(sock, wait=0.25, max_wait=1.2)
                last_text = raw

                low = raw.lower()
                # 处理 USG5500 / 防火墙默认 Console 认证
                if "username:" in low or "login:" in low:
                    sock.sendall((default_user + "\r\n").encode("ascii"))
                    time.sleep(0.3)
                    raw += _recv_raw(sock, wait=0.25, max_wait=1.0)
                    low = raw.lower()

                if "password:" in low:
                    sock.sendall((default_pass + "\r\n").encode("ascii"))
                    time.sleep(0.4)
                    raw += _recv_raw(sock, wait=0.25, max_wait=1.2)
                    low = raw.lower()

                # 处理首次登录密码修改询问 (Y/N)
                if ("modify" in low or "change" in low or "are you sure" in low) and (
                    "y/n" in low or "yes/no" in low
                ):
                    sock.sendall(b"n\r\n")
                    time.sleep(0.3)
                    raw += _recv_raw(sock, wait=0.25, max_wait=1.0)

                prompt = parse_prompt(raw)
                # 排除引导阶段的文本，确认出现合法提示符且不再连续刷 ###
                if prompt and not raw.rstrip().endswith("###"):
                    return {
                        "ready": True,
                        "port": int(port),
                        "prompt": prompt,
                        "banner": last_text[-300:],
                    }
            except OSError:
                pass
            finally:
                if sock:
                    try:
                        sock.close()
                    except OSError:
                        pass
        time.sleep(poll_interval)

    return {
        "ready": False,
        "port": int(port),
        "prompt": "",
        "banner": last_text[-300:],
        "error": f"端口 {port} 在 {timeout:.0f}s 内未就绪",
    }


def _recv_raw(
    sock: socket.socket,
    wait: float = 0.2,
    max_wait: float = 3.0,
    wait_for_prompt: bool = False,
) -> str:
    output = b""
    end_time = time.time() + max_wait
    sock.settimeout(wait)
    more_count = 0
    while time.time() < end_time:
        try:
            chunk = sock.recv(8192)
        except socket.timeout:
            if not wait_for_prompt:
                break
            text_so_far = strip_ansi(output.decode("gbk", errors="replace"))
            lines = [ln.strip() for ln in text_so_far.splitlines() if ln.strip()]
            if lines and PROMPT_RE.search(lines[-1]):
                break
            continue
        except OSError:
            break
        if not chunk:
            break
        output += chunk
        if not wait_for_prompt:
            end_time = time.time() + wait
        if b"---- More ----" in chunk and more_count < 25:
            more_count += 1
            try:
                sock.sendall(b" ")
                time.sleep(0.05)
            except OSError:
                break
    text = output.decode("gbk", errors="replace").replace("\x00", "")
    return strip_ansi(text)


class ConsoleConnection:
    """本地 Console 连接器。"""

    def __init__(
        self,
        port: int,
        host: str = "127.0.0.1",
        timeout: float = 8.0,
        default_user: str = "admin",
        default_pass: str = "Admin@123",
    ):
        self.host = host
        self.port = int(port)
        self.timeout = timeout
        self.default_user = default_user
        self.default_pass = default_pass
        self.sock: Optional[socket.socket] = None
        self._lock = get_port_lock(self.port)
        self._locked = False
        self._prompt = ""
        self.connect()

    def connect(self) -> None:
        self._lock.acquire()
        self._locked = True
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self.sock.settimeout(0.5)
            self._wake_and_authenticate()
            # 退回用户视图并关闭分屏
            self._send_line("return")
            _recv_raw(self.sock, wait=0.15, max_wait=0.6)
            self._send_line("screen-length 0 temporary")
            _recv_raw(self.sock, wait=0.15, max_wait=0.6)
            self.find_prompt()
        except Exception:
            self.disconnect()
            raise

    def _wake_and_authenticate(self) -> None:
        assert self.sock is not None
        self.sock.sendall(b"\x03\r\n")
        time.sleep(0.2)
        raw = _recv_raw(self.sock, wait=0.2, max_wait=1.0)
        low = raw.lower()
        if "username:" in low or "login:" in low:
            self._send_line(self.default_user)
            time.sleep(0.25)
            raw += _recv_raw(self.sock, wait=0.2, max_wait=1.0)
            low = raw.lower()
        if "password:" in low:
            self._send_line(self.default_pass)
            time.sleep(0.35)
            raw += _recv_raw(self.sock, wait=0.2, max_wait=1.0)
            low = raw.lower()
        if ("modify" in low or "change" in low) and ("y/n" in low or "yes/no" in low):
            self._send_line("n")
            time.sleep(0.25)
            _recv_raw(self.sock, wait=0.2, max_wait=0.8)

    def _send_line(self, cmd: str) -> None:
        if not self.sock:
            raise RuntimeError(f"Console 端口 {self.port} 未连接")
        self.sock.sendall((cmd + "\r").encode("ascii", errors="ignore"))

    def disconnect(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        if self._locked:
            self._lock.release()
            self._locked = False

    def __enter__(self) -> "ConsoleConnection":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.disconnect()

    def find_prompt(self) -> str:
        assert self.sock is not None
        self._send_line("")
        time.sleep(0.15)
        raw = _recv_raw(self.sock, wait=0.15, max_wait=1.0)
        p = parse_prompt(raw)
        if p:
            self._prompt = f"<{p}>"
        return self._prompt or "<Huawei>"

    def send_command(
        self,
        command: str,
        read_timeout: float = 8.0,
        strip_prompt: bool = True,
        strip_command: bool = True,
        **kwargs,
    ) -> str:
        assert self.sock is not None
        _recv_raw(self.sock, wait=0.05, max_wait=0.15)
        self._send_line(command)
        time.sleep(0.15)
        is_slow = command.strip().lower().startswith(("ping ", "tracert "))
        raw = _recv_raw(
            self.sock,
            wait=0.25,
            max_wait=read_timeout,
            wait_for_prompt=is_slow,
        )
        # 若碰上交互确认 (Y/N)，自动确认 y
        if re.search(r"\[y/n\]|\(y/n\)|yes/no", raw, re.IGNORECASE):
            self._send_line("y")
            time.sleep(0.2)
            raw += _recv_raw(self.sock, wait=0.25, max_wait=2.0)

        lines = [line.rstrip("\r") for line in raw.splitlines()]
        if strip_command and lines and command.strip() and command.strip() in lines[0]:
            lines = lines[1:]
        if strip_prompt and lines and PROMPT_RE.search(lines[-1]):
            lines = lines[:-1]
        return "\n".join(lines).strip()

    def send_command_timing(
        self,
        command: str,
        read_timeout: float = 5.0,
        delay_factor: float = 1.0,
        **kwargs,
    ) -> str:
        return self.send_command(
            command,
            read_timeout=read_timeout * delay_factor,
            strip_prompt=False,
            strip_command=True,
        )

    def _enter_system_view(self) -> CliView:
        """
        稳健地回到系统视图。

        VRP 的 `return` 语义是"一路退回用户视图"——从接口视图发一次 return
        会直接到 `<Huawei>`，中途不会停在 `[Huawei]`。所以要**连发两次 return**
        确保干净，再进 system-view，并用提示符确认真到了 `[xxx]`。
        """
        for _ in range(2):
            self._send_line("return")
            time.sleep(0.15)
            _recv_raw(self.sock, wait=0.1, max_wait=0.6)

        for _ in range(3):
            self._send_line("system-view")
            time.sleep(0.2)
            raw = _recv_raw(self.sock, wait=0.2, max_wait=1.5)
            view = parse_view(raw)
            if view and view.kind == "system":
                return view
        view = parse_view(self.find_prompt())
        if view is None or view.kind != "system":
            raise RuntimeError(f"无法进入系统视图（当前: {view.name if view else '未知'}）")
        return view

    def send_config_set(
        self,
        config_commands: List[str],
        exit_config_mode: bool = True,
        read_timeout: float = 15.0,
        **kwargs,
    ) -> str:
        """
        进入系统视图批量执行配置命令。

        每发一条命令后都校验视图：一旦掉回用户视图（VRP 在用户视图会**静默忽略**
        配置命令，不报错也不执行），立即补一次 system-view 重新进入，
        而不是让后续命令全部空转。
        """
        assert self.sock is not None
        outputs: List[str] = []
        self._enter_system_view()

        for cmd in config_commands or []:
            cmd_str = (cmd or "").strip()
            if not cmd_str:
                continue
            res = self.send_command(cmd_str, read_timeout=read_timeout, strip_prompt=False)
            outputs.append(f"{cmd_str}\n{res}")

            # 视图守卫：模板里的 return / quit 会把设备打回用户视图，
            # 此后所有命令都会被静默吞掉，必须在发现时立刻恢复。
            view = parse_view(res)
            if view and view.kind == "user":
                self._enter_system_view()

        if exit_config_mode:
            self._send_line("return")
            time.sleep(0.15)
            _recv_raw(self.sock, wait=0.1, max_wait=0.6)

        return "\n".join(outputs)

    def deploy_commands(
        self,
        commands: List[str],
        verify_commands: Optional[List[str]] = None,
        save_config: bool = False,
    ) -> dict:
        """进入系统视图批量下发配置命令，并执行验证命令与保存。"""
        assert self.sock is not None
        result = {
            "success": False,
            "console_port": self.port,
            "commands": commands,
            "command_outputs": [],
            "verify_commands": verify_commands or [],
            "verify_outputs": [],
            "save_result": "",
            "error": None,
        }

        # 确保先退回用户视图，再稳健进入系统视图
        self._send_line("return")
        time.sleep(0.15)
        _recv_raw(self.sock, wait=0.15, max_wait=0.5)

        in_sys = False
        for _ in range(4):
            self._send_line("system-view")
            time.sleep(0.25)
            out = _recv_raw(self.sock, wait=0.2, max_wait=2.0)
            if "[" in out:
                in_sys = True
                break

        if not in_sys:
            result["error"] = f"端口 {self.port} 未能进入 system-view"
            return result

        all_ok = True
        for cmd in commands:
            out = self.send_command(cmd, read_timeout=self.timeout, strip_prompt=False)
            err = get_cli_error(out)
            result["command_outputs"].append(
                {
                    "command": cmd,
                    "output": out,
                    "success": err is None,
                    "error": err,
                }
            )
            if err:
                all_ok = False

        self._send_line("return")
        time.sleep(0.2)
        _recv_raw(self.sock, wait=0.15, max_wait=1.0)

        for vcmd in verify_commands or []:
            vout = self.send_command(vcmd, read_timeout=self.timeout)
            result["verify_outputs"].append(
                {
                    "command": vcmd,
                    "output": vout,
                    "success": True,
                }
            )

        if save_config:
            result["save_result"] = self.save_config()

        result["success"] = all_ok
        return result

    def save_config(self) -> str:
        assert self.sock is not None
        self._send_line("return")
        time.sleep(0.15)
        _recv_raw(self.sock, wait=0.1, max_wait=0.5)
        self._send_line("save")
        time.sleep(0.3)
        out = _recv_raw(self.sock, wait=0.25, max_wait=4.0)
        if any(k in out.lower() for k in ("y/n", "yes/no", "[y]", "[n]")):
            self._send_line("y")
            time.sleep(0.4)
            out += _recv_raw(self.sock, wait=0.25, max_wait=4.0)
        # 某些设备还会提示输入保存文件名，直接回车确认
        if "file name" in out.lower() or ".cfg" in out.lower() or ".zip" in out.lower():
            self._send_line("")
            time.sleep(0.4)
            out += _recv_raw(self.sock, wait=0.25, max_wait=4.0)
        return out.strip()[:800]
