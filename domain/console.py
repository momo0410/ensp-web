"""
eNSP 本地 Console（telnet 127.0.0.1）通道。

本系统唯一的设备接入方式：设备没有管理 IP，直接通过
127.0.0.1:<console_port> 与设备串口会话交互。
"""

import re
import socket
import time
from typing import List, Optional

from app.config import logger
from domain.parse import _regex_find


def _console_recv_all(sock: socket.socket, wait: float = 0.25, max_wait: float = 3.0) -> str:
    output = b""
    end_time = time.time() + max_wait
    # 关键：把 recv 超时设为「空闲等待时间」，使无数据时在 wait 秒后快速返回，
    # 否则会沿用调用方设置的长超时（如 console_deploy 的 8s），导致每条命令多等 8s。
    sock.settimeout(wait)
    while time.time() < end_time:
        try:
            chunk = sock.recv(8192)
        except socket.timeout:
            break
        except OSError:
            break
        if not chunk:
            break
        output += chunk
        end_time = time.time() + wait
    return output.decode("gbk", errors="replace").replace("\x00", "")


def _console_send_command(sock: socket.socket, command: str, max_wait: float = 5.0) -> str:
    sock.sendall((command + "\r").encode("ascii", errors="ignore"))
    time.sleep(0.25)
    output = _console_recv_all(sock, max_wait=max_wait)
    more_count = 0
    while "---- More ----" in output and more_count < 20:
        sock.sendall(b" ")
        time.sleep(0.15)
        output += _console_recv_all(sock, max_wait=2.0)
        more_count += 1
    return output


def _parse_console_prompt(text: str) -> str:
    return _regex_find(r"[<\[]([^<>\[\]\r\n]+)[>\]]", text or "")


def _strip_command_echo(text: str) -> str:
    cleaned = (text or "").replace("\x00", "")
    cleaned = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", cleaned)
    cleaned = re.sub(r"[\b\r]+", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def console_deploy(
    console_port: int,
    commands: List[str],
    verify_commands: Optional[List[str]] = None,
    save_config: bool = False,
    timeout: float = 8.0,
) -> dict:
    """通过 eNSP 本地 Console 端口（telnet localhost）下发配置，无需设备 IP。"""
    from services.device_connector import get_cli_error

    result = {
        "success": False,
        "console_port": console_port,
        "commands": commands,
        "command_outputs": [],
        "verify_commands": verify_commands or [],
        "verify_outputs": [],
        "save_result": "",
        "error": None,
    }
    sock = None
    try:
        sock = socket.create_connection(("127.0.0.1", int(console_port)), timeout=2.0)
        sock.settimeout(timeout)
        # 唤醒提示符：eNSP 控制台刚连上时通常要先发一个空行才出稳定提示符，
        # 否则首条命令（含 system-view）会被吞掉，导致后续命令在用户视图下失败。
        try:
            sock.sendall(b"\r")
            time.sleep(0.2)
        except Exception:
            pass
        _console_recv_all(sock, max_wait=0.4)

        # 稳健进入系统视图：VRP 首条命令偶发被吞，循环重试直到提示符变为 [xxx]
        for _ in range(3):
            out = _console_send_command(sock, "system-view", max_wait=3.0)
            if "[" in out:
                break
            time.sleep(0.3)

        all_ok = True
        for cmd in commands:
            out = _console_send_command(sock, cmd, max_wait=timeout)
            err = get_cli_error(out)
            result["command_outputs"].append({
                "command": cmd,
                "output": _strip_command_echo(out),
                "success": err is None,
                "error": err,
            })
            if err:
                all_ok = False

        _console_send_command(sock, "return", max_wait=3.0)

        for vcmd in (verify_commands or []):
            vout = _console_send_command(sock, vcmd, max_wait=timeout)
            result["verify_outputs"].append({
                "command": vcmd,
                "output": _strip_command_echo(vout),
                "success": True,
            })

        if save_config and all_ok:
            result["save_result"] = _console_save(sock, timeout)
        result["success"] = all_ok
    except OSError as e:
        result["error"] = f"无法连接 Console 端口 {console_port}: {e}"
    except Exception as e:
        result["error"] = str(e)
    finally:
        if sock:
            try:
                sock.close()
            except OSError:
                pass
    return result


def _console_save(sock, timeout: float = 8.0) -> str:
    """在用户视图执行 save 并处理 y/n 确认提示。"""
    try:
        sock.sendall(b"save\r")
        time.sleep(0.4)
        out = _console_recv_all(sock, max_wait=timeout)
        if "y/n" in out.lower() or "yes/no" in out.lower() or "[y]" in out.lower():
            sock.sendall(b"y\r")
            time.sleep(0.6)
            out += _console_recv_all(sock, max_wait=timeout)
        return _strip_command_echo(out)[:800]
    except Exception as e:
        return f"save_failed: {e}"
