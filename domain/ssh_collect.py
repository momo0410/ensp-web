"""
SSH 设备采集（Netmiko）与命令执行。

包含设备识别、并行采集、以及绕过 Netmiko 轮询 sleep 的快速命令发送。
fast_send_command 是纯 SSH channel 工具函数，被 services/capability_probe.py
直接依赖——因此定义在此，由 domain 层拥有，services 反向导入。
"""

import re
import threading
import time
from typing import Any, Dict, List

from app.config import (
    DEFAULT_DEVICE_TYPE,
    DEFAULT_SSH_PASSWORD,
    DEFAULT_SSH_USERNAME,
    HUAWEI_COMMANDS,
    HUAWEI_COMMANDS_FAST,
    PARALLEL_CMD_GROUP_A,
    PARALLEL_CMD_GROUP_A_FAST,
    PARALLEL_CMD_GROUP_B,
    PARALLEL_CMD_GROUP_B_FAST,
    PARALLEL_CMD_GROUP_C,
    PARALLEL_CMD_GROUP_C_FAST,
    SSH_READ_TIMEOUT,
    SSH_RETRY_COUNT,
    SSH_TIMEOUT,
    logger,
)
from domain.device_store import load_devices, save_devices
from services.device_connector import command_result_from_output
from domain.netprobe import _create_management_socket, _management_source_ip_for
from domain.parse import _parse_device_info, _regex_find

def _import_netmiko():
    """兼容不同 Netmiko 版本的导入路径。"""
    from netmiko import ConnectHandler

    try:
        from netmiko.exceptions import NetMikoTimeoutException, NetMikoAuthenticationException
    except ImportError:
        from netmiko.ssh_exception import NetMikoTimeoutException, NetMikoAuthenticationException

    return ConnectHandler, NetMikoTimeoutException, NetMikoAuthenticationException

def _parse_prompt_name(prompt: str) -> str:
    """从华为设备提示符中提取名称，如 <AR1> 或 [AR1]。"""
    return _regex_find(r"[<\[]([^>\]]+)[>\]]", prompt or "")

def _default_discovered_name(ip: str) -> str:
    last_octet = ip.rsplit(".", 1)[-1]
    return f"Host-{last_octet}"

def ssh_identify_device(device: dict) -> dict:
    """轻量 SSH 识别设备名称，不执行完整采集命令。"""
    identify = {
        "ip": device["ip"],
        "name": device.get("name") or _default_discovered_name(device["ip"]),
        "ssh_ok": False,
        "ssh_error": "",
        "model": "",
    }

    base_conn_params = {
        "device_type": device.get("device_type", DEFAULT_DEVICE_TYPE),
        "host": device["ip"],
        "username": device.get("username", DEFAULT_SSH_USERNAME),
        "password": device.get("password", DEFAULT_SSH_PASSWORD),
        "timeout": 5,
        "conn_timeout": 5,
        "banner_timeout": 5,
        "fast_cli": False,
    }

    connection = None
    ssh_sock = None
    try:
        ConnectHandler, NetMikoTimeoutException, NetMikoAuthenticationException = _import_netmiko()

        conn_params = dict(base_conn_params)
        source_ip = _management_source_ip_for(device["ip"])
        if source_ip:
            ssh_sock = _create_management_socket(device["ip"], 22, timeout=5)
            conn_params["sock"] = ssh_sock

        connection = ConnectHandler(**conn_params)
        ssh_sock = None
        identify["ssh_ok"] = True

        prompt_name = _parse_prompt_name(connection.find_prompt())
        sysname_output = connection.send_command(
            "display current-configuration | include sysname",
            read_timeout=5,
        )
        sysname = _regex_find(r"(?m)^\s*sysname\s+(\S+)", sysname_output)
        identify["name"] = sysname or prompt_name or identify["name"]

        version_output = connection.send_command("display version", read_timeout=5)
        identify["model"] = _regex_find(
            r"(AR\d+|S\d+|LSW\d+|NE\d+|CE\d+|USG\d+|Router|Switch)\S*",
            version_output,
        )
    except ImportError:
        identify["ssh_error"] = "Netmiko 未安装"
    except NetMikoTimeoutException as e:
        identify["ssh_error"] = f"SSH 连接超时: {e}"
    except NetMikoAuthenticationException as e:
        identify["ssh_error"] = f"SSH 认证失败: {e}"
    except Exception as e:
        identify["ssh_error"] = str(e)
    finally:
        if connection:
            try:
                connection.disconnect()
            except Exception:
                pass
        elif ssh_sock:
            try:
                ssh_sock.close()
            except OSError:
                pass

    return identify

def fast_send_command(connection, command: str, timeout: float = 10.0, idle_timeout: float = 1.5) -> str:
    """
    快速命令执行 — 绕过 Netmiko 的轮询 sleep，直接从 SSH channel 读取。

    原理：
      Netmiko send_command() 每次循环 sleep(0.1s) 再读 channel，
      大输出场景下 30+ 次循环 = 3s+ 白等。
      本函数用 channel.recv_ready() 检测数据，有数据立刻读，
      没数据仅 sleep(0.01s)，比 Netmiko 的 0.1s 快 10 倍。

    Args:
        connection: Netmiko ConnectHandler 实例
        command: 要执行的命令
        timeout: 总超时秒数
        idle_timeout: 输出空闲超时（连续 N 秒无新数据则认为命令完成）

    Returns:
        命令输出字符串
    """
    # 获取底层 Paramiko channel
    channel = connection.remote_conn
    if channel is None:
        raise RuntimeError("SSH channel 未建立")

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
            # 没数据时仅等 10ms（Netmiko 等 100ms）
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

def ssh_collect_device(device: dict, fast_mode: bool = False) -> dict:
    """
    通过 Netmiko SSH 连接华为设备并采集信息。

    优化策略：
    - fast_mode=True 时使用精简命令列表，但保留完整 current-configuration
    - 大输出命令（display current-configuration / routing-table）使用
      send_command_timing 代替 send_command，避免反复轮询 prompt 的开销
    - 移除重复的 display lldp neighbor（brief 已包含核心信息）
    - LLDP 开启改为仅在首次连接时执行

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
        "ssh_ok": False,
        "ssh_error": "",
        "commands": {},
        "info": {},
    }

    base_conn_params = {
        "device_type": device.get("device_type", DEFAULT_DEVICE_TYPE),
        "host": device["ip"],
        "username": device.get("username", DEFAULT_SSH_USERNAME),
        "password": device.get("password", DEFAULT_SSH_PASSWORD),
        "timeout": SSH_TIMEOUT,
        "conn_timeout": SSH_TIMEOUT,
        "banner_timeout": SSH_TIMEOUT,
        "fast_cli": False,  # 华为设备推荐 False，可减少登录问题
    }

    # 选择命令列表
    commands_list = HUAWEI_COMMANDS_FAST if fast_mode else HUAWEI_COMMANDS

    last_error = None
    for attempt in range(1, SSH_RETRY_COUNT + 1):
        connection = None
        ssh_sock = None
        try:
            ConnectHandler, NetMikoTimeoutException, NetMikoAuthenticationException = _import_netmiko()

            conn_params = dict(base_conn_params)
            source_ip = _management_source_ip_for(device["ip"])
            if source_ip:
                ssh_sock = _create_management_socket(device["ip"], 22, timeout=SSH_TIMEOUT)
                conn_params["sock"] = ssh_sock

            connection = ConnectHandler(**conn_params)
            ssh_sock = None
            result["ssh_ok"] = True

            # 确保 LLDP 已开启（eNSP 设备默认未开启）
            try:
                fast_send_command(connection, "system-view", timeout=5, idle_timeout=1.0)
                fast_send_command(connection, "lldp enable", timeout=5, idle_timeout=1.0)
                fast_send_command(connection, "return", timeout=5, idle_timeout=1.0)
                logger.debug(f"[{device['ip']}] LLDP 已启用")
            except Exception as e:
                logger.debug(f"[{device['ip']}] 启用 LLDP 时异常（可忽略）: {e}")

            # 执行命令（使用零等待快速读取）
            for cmd in commands_list:
                try:
                    output = fast_send_command(connection, cmd, timeout=SSH_READ_TIMEOUT)
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

        except ImportError:
            result["ssh_error"] = "Netmiko 未安装，请执行: pip install netmiko"
            return result
        except NetMikoTimeoutException as e:
            last_error = f"SSH 连接超时: {e}"
            logger.warning(f"[{device['ip']}] 第 {attempt} 次尝试超时")
        except NetMikoAuthenticationException as e:
            last_error = f"SSH 认证失败: {e}"
            break  # 认证失败无需重试
        except Exception as e:
            last_error = str(e)
            logger.warning(f"[{device['ip']}] 第 {attempt} 次尝试失败: {e}")

        if connection:
            try:
                connection.disconnect()
            except Exception:
                pass
        elif ssh_sock:
            try:
                ssh_sock.close()
            except OSError:
                pass

        if attempt < SSH_RETRY_COUNT:
            # 指数退避等待后重试
            time.sleep(attempt * 2)

    result["ssh_error"] = last_error or "SSH 连接失败"
    return result

def _ssh_run_command_group(
    device: dict,
    commands: List[str],
    conn_params: dict,
    result_dict: dict,
    lock: threading.Lock,
) -> None:
    """
    在独立 SSH 会话中执行一组命令，结果写入共享 result_dict。
    使用 fast_send_command 直接读 channel，无轮询 sleep。
    """
    ip = device["ip"]
    connection = None
    try:
        ConnectHandler, _, _ = _import_netmiko()
        connection = ConnectHandler(**conn_params)

        for cmd in commands:
            try:
                output = fast_send_command(connection, cmd, timeout=SSH_READ_TIMEOUT)
                with lock:
                    result_dict[cmd] = command_result_from_output(output)
            except Exception as e:
                logger.warning("[%s] 命令 '%s' 失败: %s", ip, cmd, e)
                with lock:
                    result_dict[cmd] = {"success": False, "output": "", "error": str(e)}

        connection.disconnect()
    except Exception as e:
        logger.warning("[%s] SSH 会话失败（命令组 %s）: %s", ip, commands[:1], e)
        for cmd in commands:
            with lock:
                if cmd not in result_dict:
                    result_dict[cmd] = {"success": False, "output": "", "error": str(e)}
    finally:
        if connection:
            try:
                connection.disconnect()
            except Exception:
                pass

def ssh_collect_device_parallel(device: dict, fast_mode: bool = False) -> dict:
    """
    并行采集：3 个 SSH 会话同时执行不同命令组。

    分组策略（按预期耗时均衡）：
      会话 A: display version / device / cpu / memory      (快命令 ~1-2s)
      会话 B: display current-configuration                (大输出 ~2-3s)
      会话 C: display ip interface brief / vlan / ospf / lldp (中速 ~2s)

    总耗时 ≈ max(会话A, 会话B, 会话C) ≈ 3s，相比串行 8-10s 提速 ~60%。
    """
    ip = device["ip"]
    result: Dict[str, Any] = {
        "ip": ip,
        "name": device.get("name", ip),
        "online": True,
        "ssh_ok": False,
        "ssh_error": "",
        "commands": {},
        "info": {},
    }

    # 构建连接参数
    base_conn_params = {
        "device_type": device.get("device_type", DEFAULT_DEVICE_TYPE),
        "host": ip,
        "username": device.get("username", DEFAULT_SSH_USERNAME),
        "password": device.get("password", DEFAULT_SSH_PASSWORD),
        "timeout": SSH_TIMEOUT,
        "conn_timeout": SSH_TIMEOUT,
        "banner_timeout": SSH_TIMEOUT,
        "fast_cli": False,
    }

    source_ip = _management_source_ip_for(ip)
    socket_factory = _create_management_socket if source_ip else None

    # 选择命令分组
    if fast_mode:
        groups = [
            ("A-fast", PARALLEL_CMD_GROUP_A_FAST),
            ("B-fast", PARALLEL_CMD_GROUP_B_FAST),
            ("C-fast", PARALLEL_CMD_GROUP_C_FAST),
        ]
    else:
        groups = [
            ("A", PARALLEL_CMD_GROUP_A),
            ("B", PARALLEL_CMD_GROUP_B),
            ("C", PARALLEL_CMD_GROUP_C),
        ]

    # 先做一次 LLDP 启用（用第一个会话顺便做）
    try:
        ConnectHandler, _, _ = _import_netmiko()
        conn_params = dict(base_conn_params)
        if socket_factory:
            ssh_sock = socket_factory(ip, 22, timeout=SSH_TIMEOUT)
            conn_params["sock"] = ssh_sock

        lldp_conn = ConnectHandler(**conn_params)
        fast_send_command(lldp_conn, "system-view", timeout=5, idle_timeout=1.0)
        fast_send_command(lldp_conn, "lldp enable", timeout=5, idle_timeout=1.0)
        fast_send_command(lldp_conn, "return", timeout=5, idle_timeout=1.0)
        lldp_conn.disconnect()
        result["ssh_ok"] = True
    except Exception as e:
        result["ssh_error"] = f"SSH 连接失败: {e}"
        return result

    # 并行执行 3 个命令组
    shared_commands: Dict[str, Any] = {}
    cmd_lock = threading.Lock()
    threads = []

    for group_name, cmd_list in groups:
        # 每个线程独立构建连接参数（避免 socket 共享）
        thread_conn_params = dict(base_conn_params)
        if socket_factory:
            try:
                thread_conn_params["sock"] = socket_factory(ip, 22, timeout=SSH_TIMEOUT)
            except Exception:
                pass  # 无 socket 工厂则直连

        t = threading.Thread(
            target=_ssh_run_command_group,
            args=(device, cmd_list, thread_conn_params, shared_commands, cmd_lock),
            name=f"collect-{ip}-{group_name}",
            daemon=True,
        )
        threads.append(t)

    for t in threads:
        t.start()

    # 等待所有线程完成（设置超时防止无限阻塞）
    for t in threads:
        t.join(timeout=SSH_READ_TIMEOUT + 10)

    result["commands"] = shared_commands

    # 判断是否有命令成功
    if any(v.get("success") for v in shared_commands.values()):
        result["ssh_ok"] = True

    # 解析结构化信息
    result["info"] = _parse_device_info(shared_commands)
    return result
