"""
设备可达性检测：本地 Console 端口连通性。

设备通过 eNSP 本地 Console 端口（127.0.0.1:<console_port>）接入，
因此"是否可达"等价于"该本地端口是否开放"。
"""

import socket
from typing import Optional

from app.config import logger


def resolve_port(ip: str) -> Optional[int]:
    """从设备标识（'127.0.0.1:2000'）解析 Console 端口号。"""
    if ":" in str(ip):
        try:
            return int(str(ip).rsplit(":", 1)[1])
        except ValueError:
            return None
    return None


def ping_host(ip: str, timeout: float = 2.0) -> bool:
    """
    检测设备是否在线：设备关联的本地 eNSP Console 端口是否开放。
    """
    port = resolve_port(ip)
    if not port:
        from domain.device_store import load_devices

        for d in load_devices():
            if d.get("ip") == ip or d.get("name") == ip:
                port = d.get("console_port") or d.get("com_port")
                break
    if not port:
        logger.debug(f"设备 {ip} 无 Console 端口，无法判定在线状态")
        return False

    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def port_sort_key(ip: str) -> int:
    """按 Console 端口排序；无端口者排在最后。"""
    port = resolve_port(ip)
    return port if port is not None else 999999
