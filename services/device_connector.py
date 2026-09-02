"""
device_connector.py - SSH 设备连接工具模块
==========================================
提供统一的 Netmiko SSH 连接管理，供其他服务模块复用。
"""

import logging
import re
import socket
from typing import Optional, Tuple, Any

logger = logging.getLogger("ensp-web.connector")

# ANSI 转义序列：ESC[...字母（光标移动/清屏等）
_ANSI_ESC_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# ESC 字节丢失后残留的光标移动片段，如 "[42D"（console 通道实测会出现，
# 会把回显吃成 "isplay vlan" 这类残句，导致误判 Unrecognized command）
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


DEFAULT_SSH_USERNAME = "admin"
DEFAULT_SSH_PASSWORD = "Huawei@123"
DEFAULT_DEVICE_TYPE = "huawei"
SSH_TIMEOUT = 10


def import_netmiko() -> Tuple[Any, Any, Any]:
    """兼容不同 Netmiko 版本的导入路径。"""
    from netmiko import ConnectHandler
    try:
        from netmiko.exceptions import NetMikoTimeoutException, NetMikoAuthenticationException
    except ImportError:
        from netmiko.ssh_exception import NetMikoTimeoutException, NetMikoAuthenticationException
    return ConnectHandler, NetMikoTimeoutException, NetMikoAuthenticationException


def build_conn_params(
    device: dict,
    timeout: int = SSH_TIMEOUT,
    banner_timeout: Optional[int] = None,
) -> dict:
    """构建 Netmiko 连接参数字典。"""
    return {
        "device_type": device.get("device_type", DEFAULT_DEVICE_TYPE),
        "host": device["ip"],
        "username": device.get("username", DEFAULT_SSH_USERNAME),
        "password": device.get("password", DEFAULT_SSH_PASSWORD),
        "timeout": timeout,
        "conn_timeout": timeout,
        "banner_timeout": banner_timeout or timeout,
        "fast_cli": False,
    }


def create_management_socket(
    ip: str,
    port: int = 22,
    timeout: float = 1.0,
    source_ip: Optional[str] = None,
) -> socket.socket:
    """创建绑定到管理网卡源地址的 TCP socket。"""
    if source_ip:
        return socket.create_connection((ip, port), timeout=timeout, source_address=(source_ip, 0))
    return socket.create_connection((ip, port), timeout=timeout)


def connect_device(
    device: dict,
    timeout: int = SSH_TIMEOUT,
    source_ip: Optional[str] = None,
    management_socket_factory=None,
) -> Any:
    """
    建立 Netmiko SSH 连接并返回 connection 对象。

    Args:
        device: 设备字典（含 ip, username, password, device_type）
        timeout: SSH 超时秒数
        source_ip: 绑定管理源 IP（可选）
        management_socket_factory: 可替换的 socket 创建函数

    Returns:
        Netmiko ConnectHandler 实例
    """
    ConnectHandler, _, _ = import_netmiko()
    params = build_conn_params(device, timeout=timeout)

    ssh_sock = None
    if source_ip and management_socket_factory:
        ssh_sock = management_socket_factory(device["ip"], 22, timeout=timeout)
        params["sock"] = ssh_sock

    try:
        connection = ConnectHandler(**params)
        return connection
    except Exception:
        if ssh_sock:
            try:
                ssh_sock.close()
            except OSError:
                pass
        raise


def safe_disconnect(connection: Any) -> None:
    """安全断开 Netmiko 连接，忽略异常。"""
    if connection:
        try:
            connection.disconnect()
        except Exception:
            pass


def ask_help_timing(connection: Any, command: str, read_timeout: int = 4) -> str:
    """发送单独成行的 `?` 类命令并按 timing 模式读回帮助输出。

    netmiko 对 `?` 的 send_command 路径依赖提示符匹配、不稳定，统一走
    timing 模式（读到空闲即返回）；旧版 netmiko 无 read_timeout 参数时降级。
    """
    try:
        return connection.send_command_timing(command, read_timeout=read_timeout,
                                              delay_factor=1)
    except TypeError:
        return connection.send_command_timing(command, delay_factor=2)


def run_display_command(connection: Any, command: str, read_timeout: int = 15) -> dict:
    """
    执行单条 display 命令并返回结果字典。

    Returns:
        {"success": bool, "output": str, "error": Optional[str]}
    """
    try:
        output = connection.send_command(command, read_timeout=read_timeout)
        return command_result_from_output(output)
    except Exception as e:
        error_msg = str(e)
        return {"success": False, "output": "", "error": error_msg}


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
