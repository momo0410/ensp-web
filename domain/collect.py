"""
设备采集与命令执行（本地 Console 通道）。

包含设备识别、信息采集，以及绕过轮询 sleep 的快速命令发送。
fast_send_command 被 services/capability_probe.py 直接依赖——
因此定义在此，由 domain 层拥有，services 反向导入。
"""

import re
import time
from typing import Any, Dict

from app.config import (
    CONN_READ_TIMEOUT,
    CONN_RETRY_COUNT,
    CONN_TIMEOUT,
    HUAWEI_COMMANDS,
    HUAWEI_COMMANDS_FAST,
    logger,
)
from services.device_connector import command_result_from_output
from domain.parse import _parse_device_info, _regex_find


def _parse_prompt_name(prompt: str) -> str:
    """从华为设备提示符中提取名称，如 <AR1> 或 [AR1]。"""
    return _regex_find(r"[<\[]([^>\]]+)[>\]]", prompt or "")


def identify_device(device: dict) -> dict:
    """轻量识别设备名称，不执行完整采集命令。"""
    identify = {
        "ip": device["ip"],
        "name": device.get("name") or device["ip"],
        "ok": False,
        "error": "",
        "model": "",
    }

    from services.device_connector import build_conn_params, import_console

    connection = None
    try:
        ConnectHandler, ConsoleTimeoutError, ConsoleAuthError = import_console()
        connection = ConnectHandler(**build_conn_params(device, timeout=5))
        identify["ok"] = True

        prompt_name = _parse_prompt_name(connection.find_prompt())
        sysname_output = connection.send_command(
            "display current-configuration | include sysname"
        )
        sysname = _regex_find(r"sysname\s+(\S+)", sysname_output or "")
        identify["name"] = sysname or prompt_name or identify["name"]

        version_output = connection.send_command("display version")
        identify["model"] = _regex_find(
            r"(AR\d+|S\d+|NE\d+|CE\d+|USG\d+|Router|Switch)\S*", version_output or ""
        )
    except (ConsoleTimeoutError, ConsoleAuthError) as e:
        identify["error"] = str(e)
    except Exception as e:
        identify["error"] = str(e)
    finally:
        if connection:
            try:
                connection.disconnect()
            except Exception:
                pass

    return identify


def fast_send_command(connection, command: str, timeout: float = 10.0, idle_timeout: float = 1.5) -> str:
    """
    快速命令执行：优先走 ConsoleConnection 直读本地串口 Socket；
    若传入带 remote_conn 的 mock 对象则兼容读取 channel。
    """
    if getattr(connection, "remote_conn", None) is None and hasattr(connection, "send_command"):
        return connection.send_command(command, read_timeout=timeout)

    # 兼容单元测试中的 mock remote_conn channel
    channel = connection.remote_conn
    if channel is None:
        raise RuntimeError("Console channel 未建立")

    # 清空接收缓冲区（丢弃之前的残留数据）
    while channel.recv_ready():
        channel.recv(65536)

    # 发送命令
    channel.send(command + "\n")

    # 等待一小会让设备开始输出（避免读到命令回显）
    time.sleep(0.05)

    output = b""
    start_time = time.time()
    last_data_time = time.time()

    while True:
        elapsed = time.time() - start_time
        if elapsed > timeout:
            break

        # channel.recv_ready() 检测缓冲区是否有数据，无阻塞，耗时 <0.01ms
        if channel.recv_ready():
            try:
                chunk = channel.recv(65536)
                if chunk:
                    output += chunk
                    last_data_time = time.time()
            except Exception:
                break
        else:
            # 没数据时仅等 10ms
            time.sleep(0.01)

        # 空闲检测：连续 idle_timeout 秒无新数据 → 命令输出完成
        idle = time.time() - last_data_time
        if len(output) > 0 and idle > idle_timeout:
            break

    # 清理：去除命令回显和尾部 prompt
    text = output.decode("utf-8", errors="replace")
    lines = text.split("\n")

    # 去掉第一行（命令回显，如 "<Huawei>display version"）
    if lines and command.strip() in lines[0]:
        lines = lines[1:]

    # 去掉最后一行（prompt，如 "<Huawei>" 或 "[Huawei]"）
    if lines and not lines[-1].strip():
        lines = lines[:-1]
    # 最后一行如果看起来像 prompt（<...> 或 [...]），去掉
    if lines:
        last = lines[-1].strip()
        if re.match(r'^[<\[].*[>\]]$', last):
            lines = lines[:-1]

    return "\n".join(lines)


def collect_device(device: dict, fast_mode: bool = False) -> dict:
    """
    通过本地 Console 连接华为设备并采集信息。

    Console 为独占串口，故单会话顺序采集。

    Args:
        device: 设备信息字典
        fast_mode: True 时使用快速采集（跳过路由表、CPU/内存等）

    Returns:
        采集结果字典
    """
    result: Dict[str, Any] = {
        "ip": device["ip"],
        "name": device.get("name", device["ip"]),
        "online": True,
        "ok": False,
        "error": "",
        "commands": {},
        "info": {},
    }

    from services.device_connector import build_conn_params, import_console

    base_conn_params = build_conn_params(device, timeout=CONN_TIMEOUT)

    # 选择命令列表
    commands_list = HUAWEI_COMMANDS_FAST if fast_mode else HUAWEI_COMMANDS

    last_error = None
    for attempt in range(1, CONN_RETRY_COUNT + 1):
        connection = None
        try:
            ConnectHandler, ConsoleTimeoutError, ConsoleAuthError = import_console()
            connection = ConnectHandler(**base_conn_params)
            result["ok"] = True

            # 执行命令（使用零等待快速读取）
            for cmd in commands_list:
                try:
                    output = fast_send_command(connection, cmd, timeout=CONN_READ_TIMEOUT)
                    result["commands"][cmd] = command_result_from_output(output)
                except Exception as e:
                    logger.warning(f"[{device['ip']}] 命令 '{cmd}' 执行失败: {e}")
                    result["commands"][cmd] = {
                        "success": False,
                        "output": "",
                        "error": str(e),
                    }

            connection.disconnect()

            # 解析结构化信息
            result["info"] = _parse_device_info(result["commands"])
            return result  # 成功，直接返回

        except ConsoleAuthError as e:
            last_error = f"Console 认证失败: {e}"
            break  # 认证失败无需重试
        except ConsoleTimeoutError as e:
            last_error = f"Console 连接超时: {e}"
            logger.warning(f"[{device['ip']}] 第 {attempt} 次尝试超时")
        except Exception as e:
            last_error = str(e)
            logger.warning(f"[{device['ip']}] 第 {attempt} 次尝试失败: {e}")

        if connection:
            try:
                connection.disconnect()
            except Exception:
                pass

        if attempt < CONN_RETRY_COUNT:
            # 指数退避等待后重试
            time.sleep(attempt * 2)

    result["error"] = last_error or "Console 连接失败"
    return result
