"""
backup_service.py - 设备配置备份模块
=====================================
通过 SSH 获取设备当前配置并保存为 .cfg 文件。
"""

import logging
import re
from pathlib import Path
from datetime import datetime
from typing import Optional

from .device_connector import (
    import_netmiko,
    build_conn_params,
    safe_disconnect,
)

logger = logging.getLogger("ensp-web.backup")

BACKUP_DIR = Path(__file__).parent.parent / "backups"


def ensure_backup_dir() -> Path:
    """确保备份目录存在。"""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    return BACKUP_DIR


def _sanitize_filename(name: str) -> str:
    """清理文件名中的非法字符。"""
    return re.sub(r'[<>:"/\\|?*]', '_', name)


def backup_device_config(
    device: dict,
    source_ip: Optional[str] = None,
    management_socket_factory=None,
    timeout: int = 10,
    read_timeout: int = 30,
) -> dict:
    """
    备份指定设备的当前配置。

    Returns:
        {
            "success": bool,
            "filename": str,
            "filepath": str,
            "config_length": int,
            "error": Optional[str],
        }
    """
    ip = device["ip"]
    hostname = device.get("name", ip)
    result = {
        "success": False,
        "filename": "",
        "filepath": "",
        "config_length": 0,
        "error": None,
    }

    connection = None
    ssh_sock = None

    try:
        ConnectHandler, _, _ = import_netmiko()
        params = build_conn_params(device, timeout=timeout)

        if source_ip and management_socket_factory:
            ssh_sock = management_socket_factory(ip, 22, timeout=timeout)
            params["sock"] = ssh_sock

        connection = ConnectHandler(**params)
        ssh_sock = None

        # Get current configuration
        config_output = connection.send_command(
            "display current-configuration",
            read_timeout=read_timeout,
        )

        safe_disconnect(connection)

        if not config_output or len(config_output) < 10:
            result["error"] = "获取到的配置为空或过短"
            return result

        # Generate filename
        now = datetime.now()
        safe_name = _sanitize_filename(hostname)
        filename = f"{safe_name}_{ip}_{now.strftime('%Y%m%d_%H%M%S')}.cfg"
        filepath = ensure_backup_dir() / filename

        filepath.write_text(config_output, encoding="utf-8")

        result["success"] = True
        result["filename"] = filename
        result["filepath"] = str(filepath)
        result["config_length"] = len(config_output)
        logger.info("[%s] backup saved: %s (%d bytes)", ip, filename, len(config_output))

    except Exception as e:
        result["error"] = str(e)
        logger.error("[%s] backup failed: %s", ip, e)
    finally:
        safe_disconnect(connection)
        if ssh_sock:
            try:
                ssh_sock.close()
            except OSError:
                pass

    return result


def list_backups(device_ip: str) -> list:
    """
    列出指定设备的所有备份文件。

    Returns:
        [{"filename": str, "filepath": str, "size": int, "created": str}, ...]
    """
    ensure_backup_dir()
    backups = []
    safe_ip = device_ip.replace(".", "_")

    for f in sorted(BACKUP_DIR.glob("*.cfg"), reverse=True):
        # Match pattern: <name>_<ip>_<datetime>.cfg
        if safe_ip in f.name or device_ip in f.name:
            stat = f.stat()
            backups.append({
                "filename": f.name,
                "filepath": str(f),
                "size": stat.st_size,
                "created": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            })

    return backups


def get_backup_content(filename: str) -> Optional[str]:
    """读取备份文件内容。"""
    filepath = ensure_backup_dir() / filename
    if filepath.exists():
        return filepath.read_text(encoding="utf-8")
    return None
