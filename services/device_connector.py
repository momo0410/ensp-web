"""
device_connector.py - 设备连接工具模块（Console 直连模式）
========================================================
通过 eNSP 本地 Console 端口（127.0.0.1:<console_port>）直连设备。
"""

import logging
import re
from typing import Any, Optional, Tuple

logger = logging.getLogger("ensp-web.connector")

# ANSI 转义序列：ESC[...字母（光标移动/清屏等）
_ANSI_ESC_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# ESC 字节丢失后残留的光标移动片段，如 "[42D"
_ANSI_RESIDUE_RE = re.compile(r"\[\d+[A-Za-z]")
_BACKSPACE_RE = re.compile(r"\x08")


def strip_ansi(text: str) -> str:
    """清理终端 ANSI 转义序列及其丢失 ESC 后的残留片段。"""
    if not text:
        return text
    text = _ANSI_ESC_RE.sub("", text)
    text = _ANSI_RESIDUE_RE.sub("", text)
    text = _BACKSPACE_RE.sub("", text)
    return text


CONN_TIMEOUT = 10


def resolve_console_port(device: dict) -> int:
    """从设备字典或 IP 标识（如 '127.0.0.1:2000'）解析本地 Console 端口号。"""
    if not isinstance(device, dict):
        return 2000
    port = device.get("console_port") or device.get("com_port") or device.get("port")
    if port:
        try:
            return int(port)
        except (TypeError, ValueError):
            pass

    ip_str = str(device.get("ip") or device.get("host") or "").strip()
    if ":" in ip_str:
        try:
            return int(ip_str.rsplit(":", 1)[1])
        except (TypeError, ValueError):
            pass

    # 从 devices.json 查找匹配的设备记录
    try:
        from domain.device_store import load_devices

        for d in load_devices():
            if d.get("ip") == ip_str or d.get("name") == device.get("name"):
                p = d.get("console_port") or d.get("com_port")
                if p:
                    return int(p)
    except Exception:
        pass

    return 2000


class ConsoleTimeoutError(TimeoutError):
    """Console 连接或命令超时异常。"""


class ConsoleAuthError(PermissionError):
    """Console 认证失败异常。"""


def ConsoleConnectHandler(**params) -> Any:
    """本地 Console 连接工厂。"""
    from domain.console_connection import ConsoleConnection

    port = params.get("console_port") or resolve_console_port(params)
    timeout = float(params.get("timeout") or CONN_TIMEOUT)
    try:
        return ConsoleConnection(port=int(port), timeout=timeout)
    except OSError as e:
        raise ConsoleTimeoutError(f"无法连接本地 Console 端口 127.0.0.1:{port}: {e}") from e


def import_console() -> Tuple[Any, Any, Any]:
    """返回 Console 连接器工厂及其超时/认证异常类型。"""
    return ConsoleConnectHandler, ConsoleTimeoutError, ConsoleAuthError


def build_conn_params(device: dict, timeout: int = CONN_TIMEOUT) -> dict:
    """构建设备连接参数字典（包含解析后的 console_port）。"""
    port = resolve_console_port(device)
    return {
        "console_port": port,
        "timeout": timeout,
        "conn_timeout": timeout,
    }


def connect_device(device: dict, timeout: int = CONN_TIMEOUT) -> Any:
    """建立本地 Console 连接并返回 ConsoleConnection 对象。"""
    ConnectHandler, _, _ = import_console()
    return ConnectHandler(**build_conn_params(device, timeout=timeout))


def safe_disconnect(connection: Any) -> None:
    """安全断开连接，忽略异常。"""
    if connection:
        try:
            connection.disconnect()
        except Exception:
            pass


def ask_help_timing(connection: Any, command: str, read_timeout: int = 4) -> str:
    """发送单独成行的 `?` 类命令并按 timing 模式读回帮助输出。"""
    try:
        return connection.send_command_timing(command, read_timeout=read_timeout, delay_factor=1)
    except TypeError:
        return connection.send_command_timing(command, delay_factor=2)


_CLI_ERROR_PATTERNS = (
    re.compile(r"error:\s*unrecognized command", re.IGNORECASE),
    re.compile(r"error:\s*wrong parameter", re.IGNORECASE),
    re.compile(r"error:\s*too many parameters", re.IGNORECASE),
    re.compile(r"error:\s*incomplete command", re.IGNORECASE),
    re.compile(r"\binvalid input\b", re.IGNORECASE),
    re.compile(r"\bunknown command\b", re.IGNORECASE),
    re.compile(r"\bambiguous command\b", re.IGNORECASE),
)


def get_cli_error(output: str) -> Optional[str]:
    """Return a concise device CLI error when command output contains one."""
    if not output:
        return None
    for line in output.splitlines():
        stripped = line.strip()
        if any(pattern.search(stripped) for pattern in _CLI_ERROR_PATTERNS):
            return stripped
    return None


def command_result_from_output(output: str) -> dict:
    """Build a command result without treating a device CLI rejection as success."""
    cli_error = get_cli_error(output)
    if cli_error:
        return {"success": False, "output": output, "error": cli_error}
    return {"success": True, "output": output, "error": None}


def is_unsupported_error(error_msg: str) -> bool:
    """判断命令输出是否表示设备不支持该功能。"""
    if not error_msg:
        return False
    lower = error_msg.lower()
    keywords = [
        "unrecognized command",
        "wrong parameter",
        "too many parameters",
        "error:",
        "invalid input",
        "unknown command",
        "ambiguous command",
    ]
    return any(kw in lower for kw in keywords)


def is_output_unsupported(output: str) -> bool:
    """判断命令输出文本是否包含不支持关键字。"""
    return get_cli_error(output) is not None
