"""
free_config_executor.py - 自由配置执行器
========================================
负责备份、连接设备、下发命令、验证、保存配置、记录结果。
"""

import logging
import time
from typing import Dict, Any, List, Optional
from datetime import datetime

from .device_connector import (
    import_netmiko,
    build_conn_params,
    safe_disconnect,
)
from .backup_service import backup_device_config

logger = logging.getLogger("ensp-web.free-executor")


def _save_device_config(conn, device_ip: str) -> Dict[str, Any]:
    """
    保存设备配置。

    Returns:
        {"success": bool, "message": str}
    """
    try:
        # 先尝试 Netmiko 内置方法
        conn.save_config()
        return {"success": True, "message": "配置已保存 (save_config)"}
    except Exception as e1:
        logger.debug("[%s] save_config() 失败: %s, 尝试 send_command_timing", device_ip, e1)
        try:
            # 回退到 send_command_timing
            output = conn.send_command_timing("save", strip_prompt=False, strip_command=False)
            if "Y/N" in output or "y/n" in output or "Are you sure" in output:
                output += conn.send_command_timing("y", strip_prompt=False, strip_command=False)
            return {"success": True, "message": f"配置已保存 (timing): {output[:200]}"}
        except Exception as e2:
            return {"success": False, "message": f"保存失败: {e2}"}


def execute_single_device(
    device: dict,
    commands: List[str],
    verify_commands: List[str],
    save_after: bool = False,
    stop_on_error: bool = True,
    source_ip: Optional[str] = None,
    socket_factory=None,
    timeout: int = 15,
    read_timeout: int = 30,
) -> Dict[str, Any]:
    """
    对单台设备执行配置下发。

    Returns:
        {
            "ip": str,
            "device_name": str,
            "success": bool,
            "backup_file": str,
            "commands_sent": [...],
            "command_output": str,
            "verify_commands": [...],
            "verify_outputs": {...},
            "save_result": {...},
            "errors": [...],
            "duration": float,
        }
    """
    ip = device["ip"]
    device_name = device.get("name", ip)
    start_time = time.time()

    result = {
        "ip": ip,
        "device_name": device_name,
        "success": False,
        "backup_file": None,
        "commands_sent": commands,
        "command_output": "",
        "verify_commands": verify_commands,
        "verify_outputs": {},
        "save_result": None,
        "errors": [],
        "duration": 0,
    }

    connection = None

    try:
        ConnectHandler, NetMikoTimeoutException, NetMikoAuthenticationException = import_netmiko()

        # Step 1: 备份
        logger.info("[%s] 开始备份配置", ip)
        try:
            backup_result = backup_device_config(
                device,
                source_ip=source_ip,
                management_socket_factory=socket_factory,
            )
            if backup_result.get("success"):
                result["backup_file"] = backup_result.get("filename")
                logger.info("[%s] 备份完成: %s", ip, result["backup_file"])
            else:
                result["errors"].append(f"备份失败: {backup_result.get('error', '未知错误')}")
                if stop_on_error:
                    result["duration"] = time.time() - start_time
                    return result
        except Exception as e:
            result["errors"].append(f"备份异常: {e}")
            if stop_on_error:
                result["duration"] = time.time() - start_time
                return result

        # Step 2: 连接设备
        logger.info("[%s] 连接设备", ip)
        params = build_conn_params(device, timeout=timeout)
        if source_ip and socket_factory:
            ssh_sock = socket_factory(ip, 22, timeout=timeout)
            params["sock"] = ssh_sock

        connection = ConnectHandler(**params)

        # Step 3: 进入系统视图并下发命令
        logger.info("[%s] 下发 %d 条配置命令", ip, len(commands))
        try:
            output = connection.send_config_set(commands, read_timeout=read_timeout)
            result["command_output"] = output

            # 检查输出中是否有错误标志
            error_keywords = ["Error:", "Unrecognized command", "Incomplete command",
                             "Wrong parameter", "Invalid input", "% Error"]
            for kw in error_keywords:
                if kw.lower() in output.lower():
                    result["errors"].append(f"命令执行返回错误: {kw}")
                    if stop_on_error:
                        break

        except Exception as e:
            result["errors"].append(f"命令下发异常: {e}")
            result["duration"] = time.time() - start_time
            safe_disconnect(connection)
            return result

        # Step 4: 执行验证命令
        if verify_commands:
            logger.info("[%s] 执行 %d 条验证命令", ip, len(verify_commands))
            for vcmd in verify_commands:
                try:
                    vout = connection.send_command(vcmd, read_timeout=read_timeout)
                    result["verify_outputs"][vcmd] = vout
                except Exception as e:
                    result["verify_outputs"][vcmd] = f"验证命令失败: {e}"

        # Step 5: 保存配置（如果请求）
        if save_after:
            # 先退出系统视图
            try:
                connection.send_command("return", read_timeout=5)
            except Exception:
                pass

            save_result = _save_device_config(connection, ip)
            result["save_result"] = save_result
            if not save_result["success"]:
                result["errors"].append(f"保存配置失败: {save_result['message']}")

        # 断开连接
        safe_disconnect(connection)

        # 判断是否成功
        if not result["errors"]:
            result["success"] = True
        elif not stop_on_error:
            # 非严格模式，有错误但仍标记为部分成功
            result["success"] = len(result["errors"]) == 0

    except ImportError:
        result["errors"].append("Netmiko 未安装")
    except Exception as e:
        err_name = type(e).__name__
        if "Timeout" in err_name:
            result["errors"].append(f"SSH 连接超时: {e}")
        elif "Authentication" in err_name:
            result["errors"].append(f"SSH 认证失败: {e}")
        else:
            result["errors"].append(str(e))
        logger.error("[%s] 执行失败: %s", ip, e)
    finally:
        safe_disconnect(connection)
        result["duration"] = time.time() - start_time

    return result


def execute_free_deploy(
    task: Dict[str, Any],
    preview: Dict[str, Any],
    devices: List[dict],
    source_ip_func=None,
    socket_factory=None,
    timeout: int = 15,
    read_timeout: int = 30,
) -> Dict[str, Any]:
    """
    批量执行配置下发。

    Args:
        task: 配置任务
        preview: 预览结果 {ip: {"commands": [...]}}
        devices: 设备列表
        source_ip_func: 获取源 IP 的函数
        socket_factory: socket 工厂
        timeout: SSH 超时
        read_timeout: 命令读取超时

    Returns:
        {
            "success": bool,
            "task_name": str,
            "results": {ip: {...}},
            "summary": {...},
        }
    """
    task_name = task.get("task_name", "未命名任务")
    target_devices = task.get("target_devices", [])
    verify_commands = task.get("verify_commands", [])
    save_after = task.get("save_after_success", False)
    stop_on_error = task.get("stop_on_error", True)

    # 构建设备映射
    device_map = {d["ip"]: d for d in devices}

    results = {}
    success_count = 0
    fail_count = 0

    for ip in target_devices:
        if ip not in preview:
            results[ip] = {
                "ip": ip,
                "success": False,
                "errors": ["无预览数据，跳过执行"],
                "duration": 0,
            }
            fail_count += 1
            continue

        device = device_map.get(ip)
        if not device:
            results[ip] = {
                "ip": ip,
                "success": False,
                "errors": ["设备不在设备列表中"],
                "duration": 0,
            }
            fail_count += 1
            continue

        commands = preview[ip]["commands"]
        source_ip = source_ip_func(ip) if source_ip_func else None

        result = execute_single_device(
            device=device,
            commands=commands,
            verify_commands=verify_commands,
            save_after=save_after,
            stop_on_error=stop_on_error,
            source_ip=source_ip,
            socket_factory=socket_factory,
            timeout=timeout,
            read_timeout=read_timeout,
        )
        results[ip] = result

        if result["success"]:
            success_count += 1
        else:
            fail_count += 1

    return {
        "success": fail_count == 0,
        "task_name": task_name,
        "results": results,
        "summary": {
            "total": len(target_devices),
            "success": success_count,
            "failed": fail_count,
        },
    }
