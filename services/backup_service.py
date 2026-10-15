"""
backup_service.py - 设备配置备份模块
=====================================
通过本地 Console 获取设备当前配置并保存为 .cfg 文件。
"""

import logging
import re
from pathlib import Path
from datetime import datetime
from typing import Optional

from .device_connector import (
    import_console,
    build_conn_params,
    get_cli_error,
    safe_disconnect,
)

logger = logging.getLogger("ensp-web.backup")

LEGACY_BACKUP_DIR = Path(__file__).parent.parent / "backups"


def ensure_backup_dir() -> Path:
    """
    备份目录 = 当前拓扑目录下的 configs/。

    这样配置跟着拓扑走：切换拓扑就切换一整套设备配置。
    没有激活拓扑时回退到旧的全局 backups/。
    """
    from domain import workspace

    name = workspace.active_topology()
    if name is None:
        LEGACY_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        return LEGACY_BACKUP_DIR
    d = workspace.configs_dir_for(name)
    d.mkdir(parents=True, exist_ok=True)
    return d

def _sanitize_filename(name: str) -> str:
    """清理文件名中的非法字符。"""
    return re.sub(r'[<>:"/\\|?*]', '_', name)

def backup_device_config(
    device: dict,
    timeout: int = 15,
    read_timeout: int = 90,
) -> dict:
    """
    备份指定设备的当前配置。

    read_timeout 默认 90s：`display current-configuration` 在交换机/防火墙上
    输出量很大（24 个接口 + 各协议段），30s 经常读不完；
    eNSP 镜像的 Console 是逐字符回显的，速度比真机慢得多。
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

    try:
        ConnectHandler, _, _ = import_console()
        params = build_conn_params(device, timeout=timeout)

        connection = ConnectHandler(**params)

        # Get current configuration
        config_output = connection.send_command(
            "display current-configuration",
            read_timeout=read_timeout,
        )

        safe_disconnect(connection)

        if not config_output or len(config_output) < 10:
            result["error"] = "获取到的配置为空或过短"
            return result
        cli_error = get_cli_error(config_output)
        if cli_error:
            result["error"] = f"设备拒绝获取当前配置: {cli_error}"
            return result

        # Generate filename
        now = datetime.now()
        safe_name = _sanitize_filename(hostname)
        safe_ip = _sanitize_filename(ip)
        filename = f"{safe_name}_{safe_ip}_{now.strftime('%Y%m%d_%H%M%S')}.cfg"
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

    return result

def list_backups(device_ip: str) -> list:
    """
    列出指定设备的所有备份文件。

    Returns:
        [{"filename": str, "filepath": str, "size": int, "created": str}, ...]
    """
    backup_dir = ensure_backup_dir()
    backups = []
    safe_ip = _sanitize_filename(device_ip)

    for f in sorted(backup_dir.glob("*.cfg"), reverse=True):
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
