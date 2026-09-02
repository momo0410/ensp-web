"""
deploy_service.py - 配置下发模块
=================================
通过 SSH 向设备下发配置命令，支持：
- 模板配置下发
- 原始命令下发
- 可选保存配置
- 自动备份 + 验证
"""

import logging
from typing import List, Optional, Any

from .device_connector import (
    import_netmiko,
    build_conn_params,
    command_result_from_output,
    get_cli_error,
    safe_disconnect,
)
from .backup_service import backup_device_config

logger = logging.getLogger("ensp-web.deploy")


def _deploy_commands(
    device: dict,
    commands: List[str],
    verify_commands: List[str],
    save_config: bool = False,
    source_ip: Optional[str] = None,
    management_socket_factory=None,
    timeout: int = 10,
    read_timeout: int = 15,
) -> dict:
    """
    内部配置下发核心函数。

    Args:
        device: 设备字典
        commands: 要下发的配置命令列表
        verify_commands: 下发后执行的验证命令
        save_config: 是否保存配置
        source_ip: 管理源 IP
        management_socket_factory: socket 工厂
        timeout: SSH 超时
        read_timeout: 命令读取超时

    Returns:
        下发结果字典
    """
    ip = device["ip"]
    result = {
        "success": False,
        "ip": ip,
        "commands": commands,
        "command_outputs": [],
        "verify_commands": verify_commands,
        "verify_outputs": [],
        "backup_file": "",
        "save_result": "",
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

        # Auto backup before deploy
        try:
            backup_result = _inline_backup(connection, device)
            result["backup_file"] = backup_result.get("filename", "")
        except Exception as e:
            logger.warning("[%s] pre-deploy backup failed: %s", ip, e)
            result["backup_file"] = f"backup_failed: {e}"

        # Enter system-view
        connection.send_command_timing("system-view")

        # Deploy commands one by one
        all_ok = True
        for cmd in commands:
            try:
                output = connection.send_command_timing(cmd, read_timeout=read_timeout)
                result["command_outputs"].append({
                    "command": cmd,
                    "output": output,
                    "success": True,
                })
                # Check for error indicators in output
                if output and any(kw in output.lower() for kw in [
                    "unrecognized command", "wrong parameter",
                    "incomplete command", "error:",
                ]):
                    result["command_outputs"][-1]["success"] = False
                    result["command_outputs"][-1]["error"] = output.strip()[:500]
                    all_ok = False
            except Exception as e:
                result["command_outputs"].append({
                    "command": cmd,
                    "output": "",
                    "success": False,
                    "error": str(e),
                })
                all_ok = False

        # Return to user view
        connection.send_command_timing("return")

        # Run verification commands
        for vcmd in verify_commands:
            try:
                vout = connection.send_command(vcmd, read_timeout=read_timeout)
                verify_result = command_result_from_output(vout)
                result["verify_outputs"].append({"command": vcmd, **verify_result})
            except Exception as e:
                result["verify_outputs"].append({
                    "command": vcmd,
                    "output": "",
                    "success": False,
                    "error": str(e),
                })

        # Save config if requested
        if save_config and all_ok:
            save_result = _save_device_config(connection)
            result["save_result"] = save_result

        result["success"] = all_ok
        safe_disconnect(connection)
        logger.info("[%s] deploy done: success=%s, commands=%d", ip, all_ok, len(commands))

    except Exception as e:
        result["error"] = str(e)
        logger.error("[%s] deploy failed: %s", ip, e)
    finally:
        safe_disconnect(connection)
        if ssh_sock:
            try:
                ssh_sock.close()
            except OSError:
                pass

    return result


def _inline_backup(connection: Any, device: dict) -> dict:
    """使用已有连接执行备份。"""
    config_output = connection.send_command(
        "display current-configuration",
        read_timeout=30,
    )
    cli_error = get_cli_error(config_output)
    if cli_error:
        raise RuntimeError(f"设备拒绝获取当前配置: {cli_error}")
    if not config_output or len(config_output) < 10:
        raise RuntimeError("获取到的配置为空或过短")
    from .backup_service import ensure_backup_dir, _sanitize_filename
    from datetime import datetime

    hostname = device.get("name", device["ip"])
    ip = device["ip"]
    now = datetime.now()
    safe_name = _sanitize_filename(hostname)
    filename = f"{safe_name}_{ip}_{now.strftime('%Y%m%d_%H%M%S')}.cfg"
    filepath = ensure_backup_dir() / filename
    filepath.write_text(config_output, encoding="utf-8")

    logger.info("[%s] pre-deploy backup: %s", ip, filename)
    return {"filename": filename, "filepath": str(filepath)}


def _save_device_config(connection: Any) -> str:
    """
    保存设备配置。
    尝试 conn.save_config()，失败则用 send_command_timing。
    """
    try:
        result = connection.save_config()
        return f"save_config: {result}"
    except Exception as e:
        logger.debug("save_config() failed, trying manual save: %s", e)

    # Fallback: manual save via timing
    try:
        output = connection.send_command_timing("save")
        if "y/n" in output.lower() or "yes/no" in output.lower() or "[y]" in output.lower():
            output += connection.send_command_timing("y")
        return f"manual_save: {output.strip()[:500]}"
    except Exception as e:
        return f"save_failed: {e}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def deploy_config(
    device: dict,
    commands: List[str],
    verify_commands: List[str] = None,
    save_config: bool = False,
    source_ip: Optional[str] = None,
    management_socket_factory=None,
    timeout: int = 10,
) -> dict:
    """
    下发模板配置到设备。

    Args:
        device: 设备字典
        commands: 配置命令列表
        verify_commands: 验证命令列表
        save_config: 是否保存
        source_ip: 管理源 IP
        management_socket_factory: socket 工厂
        timeout: 超时

    Returns:
        下发结果
    """
    if not commands:
        return {"success": False, "error": "没有配置命令可下发"}

    return _deploy_commands(
        device=device,
        commands=commands,
        verify_commands=verify_commands or [],
        save_config=save_config,
        source_ip=source_ip,
        management_socket_factory=management_socket_factory,
        timeout=timeout,
    )


def deploy_raw_commands(
    device: dict,
    raw_commands: List[str],
    save_config: bool = False,
    source_ip: Optional[str] = None,
    management_socket_factory=None,
    timeout: int = 10,
) -> dict:
    """
    下发原始命令到设备（高级模式）。

    与模板模式的区别：
    - 不自动添加 system-view / return
    - 命令按原样下发
    - 标记为危险模式

    Args:
        device: 设备字典
        raw_commands: 原始命令列表
        save_config: 是否保存
        source_ip: 管理源 IP
        management_socket_factory: socket 工厂
        timeout: 超时

    Returns:
        下发结果
    """
    if not raw_commands:
        return {"success": False, "error": "没有命令可下发"}

    result = _deploy_commands(
        device=device,
        commands=raw_commands,
        verify_commands=[],
        save_config=save_config,
        source_ip=source_ip,
        management_socket_factory=management_socket_factory,
        timeout=timeout,
    )
    result["raw_mode"] = True
    result["warning"] = "高级命令模式：命令已原样下发，请确认设备状态正常"
    return result
