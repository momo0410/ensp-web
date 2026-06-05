"""
device_connector.py - SSH 设备连接工具模块
==========================================
提供统一的 Netmiko SSH 连接管理，供其他服务模块复用。
"""

import logging
import socket
from typing import Optional, Tuple, Any

logger = logging.getLogger("ensp-web.connector")

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


def run_display_command(connection: Any, command: str, read_timeout: int = 15) -> dict:
    """
    执行单条 display 命令并返回结果字典。

    Returns:
        {"success": bool, "output": str, "error": Optional[str]}
    """
    try:
        output = connection.send_command(command, read_timeout=read_timeout)
        return {"success": True, "output": output, "error": None}
    except Exception as e:
        error_msg = str(e)
        return {"success": False, "output": "", "error": error_msg}


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
    if not output:
        return False
    lower = output.lower()
    return any(kw in lower for kw in [
        "unrecognized command",
        "wrong parameter",
        "too many parameters",
        "invalid input",
        "unknown command",
        "ambiguous command",
    ])
