"""
eNSP Web 网络设备管理系统
=========================
通过 SSH 连接 eNSP 中的华为路由器/交换机，采集设备信息并展示在 Web 页面。

架构说明：
  - FastAPI 后端提供 RESTful API
  - psutil 读取本机网卡信息
  - Netmiko 执行 SSH 设备采集
  - JSON 文件持久化设备列表（带 filelock 并发保护）
  - 采集结果常驻内存，重启后需重新采集

注意事项：
  - 必须运行在安装 eNSP 的 Windows 主机上
  - 后端通过 SSH 连接设备管理 IP，不是连接 eNSP 软件
  - Web 前端无法直接读取网卡，由 Python 后端完成
"""

import json
import logging
import os
import re
import socket
import subprocess
import sys
import ipaddress
import concurrent.futures
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

import psutil
from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from services.capability_probe import probe_capabilities, probe_capabilities_parallel, probe_capabilities_from_cache
from services.role_detector import detect_protocol_roles
from services import ai_assistant
from services.ai_assistant import AIError
from services.access_guard import access_guard, ACCESS_ENABLED, ACCESS_TOKEN
from services.live_check import ping_from_device
from services import state_persist
from services.config_builder import (
    build_preview,
    build_config_commands,
    get_verify_commands,
    get_templates_for_role,
    get_all_templates,
    CONFIG_TEMPLATES,
)
from services.command_catalog import (
    get_catalog_entries,
    get_catalog_entry,
    render_catalog_commands,
    render_catalog_verify,
    get_categories,
)
from services.command_probe import probe_device_commands, get_cached_probe, cache_key
from services.help_tree import discover_via_ssh
from services.backup_service import (
    backup_device_config,
    list_backups,
    get_backup_content,
)
from services.deploy_service import deploy_config, deploy_raw_commands
from services.free_config_renderer import render_preview, validate_task
from services.free_config_executor import execute_free_deploy
from services.script_service import list_scripts, get_script, save_script, delete_script
from services.task_history_service import save_task_record, list_tasks, get_task, generate_task_id
from services.smart_defaults import (
    network_to_wildcard,
    generate_batch_smart_defaults,
    infer_wildcard_from_input,
)
from services.conflict_detector import check_cross_device_conflicts
from services.config_clone_service import (
    DEFAULT_DOMAINS as CLONE_DEFAULT_DOMAINS,
    DOMAIN_NAMES as CLONE_DOMAIN_NAMES,
    build_clone_preview,
)
from services.device_connector import command_result_from_output

# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ensp-web")

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
DEVICES_FILE = BASE_DIR / "devices.json"
STATIC_DIR = BASE_DIR / "static"
FRONTEND_DIST = BASE_DIR / "frontend" / "dist"  # React + Mantine 新版前端构建产物

# ---------------------------------------------------------------------------
# 华为设备默认 SSH 配置
# ---------------------------------------------------------------------------
HUAWEI_COMMANDS = [
    "display version",
    "display ip interface brief",
    "display current-configuration",
    "display vlan",
    "display ip routing-table",
    "display ospf peer",
    "display lldp neighbor brief",
    "display device",
    "display cpu-usage",
    "display memory-usage",
]

# 快速采集命令（跳过路由表、CPU、内存等非必要信息）。
# 必须获取完整配置：旧版多管道 section 语法不被 VRP 支持，而且会遗漏接口
# 子视图中的 OSPF/NAT/DHCP 等配置。
HUAWEI_COMMANDS_FAST = [
    "display version",
    "display ip interface brief",
    "display current-configuration",
    "display vlan",
    "display ospf peer",
    "display lldp neighbor brief",
    "display device",
]

# 大输出命令 — 使用 send_command_timing 代替 send_command，避免 prompt 等待超时
# 这些命令在 eNSP 模拟器上输出量大，send_command 反复读 prompt 效率很低
HEAVY_OUTPUT_COMMANDS = {
    "display current-configuration",
    "display current-configuration | section",
    "display ip routing-table",
}

# 并行采集命令分组 — 按预期耗时均衡分配到 3 个 SSH 会话
# 会话 A: 快命令（设备信息）
PARALLEL_CMD_GROUP_A = [
    "display version",
    "display device",
    "display cpu-usage",
    "display memory-usage",
]
# 会话 B: 重命令（配置和路由表，大输出）
PARALLEL_CMD_GROUP_B = [
    "display current-configuration",
    "display ip routing-table",
]
# 会话 C: 中速命令（接口/邻居表）
PARALLEL_CMD_GROUP_C = [
    "display ip interface brief",
    "display vlan",
    "display ospf peer",
    "display lldp neighbor brief",
]

# 快速模式并行命令分组（无路由表、CPU/内存）
PARALLEL_CMD_GROUP_A_FAST = [
    "display version",
    "display device",
]
PARALLEL_CMD_GROUP_B_FAST = [
    "display current-configuration",
]
PARALLEL_CMD_GROUP_C_FAST = [
    "display ip interface brief",
    "display vlan",
    "display ospf peer",
    "display lldp neighbor brief",
]

DEFAULT_SSH_USERNAME = "admin"
DEFAULT_SSH_PASSWORD = "Huawei@123"
DEFAULT_DEVICE_TYPE = "huawei"
SSH_TIMEOUT = 10
SSH_READ_TIMEOUT = 15
SSH_RETRY_COUNT = 2

# eNSP 管理网卡识别关键词
ENSP_KEYWORDS = ["Loopback", "VirtualBox", "Host-Only", "VMware", "KM-TEST", "NPCAP"]
EXCLUDED_INTERFACE_KEYWORDS = ["Loopback Pseudo-Interface", "Mihomo", "Meta Tunnel"]
EXPECTED_MANAGEMENT_IP = "192.168.100.1"

# ---------------------------------------------------------------------------
# FastAPI 应用
# ---------------------------------------------------------------------------
# 全局状态（单进程内存存储，不支持多 worker）
state: Dict[str, Any] = {
    "selected_interface": None,  # 当前选中的管理网卡
    "subnet": None,              # 当前扫描网段
    "collection_results": {},    # 采集结果 {ip: {...}}
    "device_status": {},         # 增量采集状态 {ip: {"last_online": bool, "last_collect_ts": float}}
    "clone_previews": {},        # 配置复制预览缓存 {preview_id: {...}}
}

# ---------------------------------------------------------------------------
# 数据模型（Pydantic）
# ---------------------------------------------------------------------------

class DeviceAdd(BaseModel):
    """手动添加设备的请求体"""
    name: str = Field(..., min_length=1, description="设备名称")
    ip: str = Field(..., description="管理 IP 地址")
    username: str = Field(default=DEFAULT_SSH_USERNAME)
    password: str = Field(default=DEFAULT_SSH_PASSWORD)
    device_type: str = Field(default=DEFAULT_DEVICE_TYPE)
    console_port: Optional[int] = Field(default=None, description="eNSP 本地 Console 端口号（可选，用于零 IP 直连配置）")


class InterfaceSelect(BaseModel):
    """选择管理网卡的请求体"""
    name: str
    ip: str
    netmask: str


class ScanRequest(BaseModel):
    """扫描网段的请求体"""
    subnet: str


class PingRequest(BaseModel):
    """Ping 测试的请求体"""
    ip: str


class DeviceDiscoverRequest(BaseModel):
    """自动发现设备的请求体"""
    subnet: Optional[str] = None
    username: str = Field(default=DEFAULT_SSH_USERNAME)
    password: str = Field(default=DEFAULT_SSH_PASSWORD)
    device_type: str = Field(default=DEFAULT_DEVICE_TYPE)
    replace: bool = True
    ssh_identify: bool = True
    dry_run: bool = False
    max_workers: int = Field(default=50, ge=1, le=200)


class ProtocolConfigRequest(BaseModel):
    """协议配置请求体"""
    mode: str = Field(default="form", description="配置模式: form 或 command")
    params: dict = Field(default={}, description="表单模式下的参数")
    commands: List[str] = Field(default=[], description="命令行模式下的命令列表")


class ConfigPreviewRequest(BaseModel):
    """配置预览请求体"""
    config_type: str = Field(..., description="配置类型标识")
    params: dict = Field(default={}, description="表单参数")


class ConfigDeployRequest(BaseModel):
    """配置下发请求体"""
    config_type: str = Field(..., description="配置类型标识")
    params: dict = Field(default={}, description="表单参数")
    save_config: bool = Field(default=False, description="下发后是否保存配置")


class RawPreviewRequest(BaseModel):
    """原始命令预览请求体"""
    commands: List[str] = Field(default=[], description="原始命令列表")


class RawDeployRequest(BaseModel):
    """原始命令下发请求体"""
    commands: List[str] = Field(default=[], description="原始命令列表")
    save_config: bool = Field(default=False, description="下发后是否保存配置")
    confirm: bool = Field(default=False, description="二次确认标志")


class ClonePreviewRequest(BaseModel):
    """设备配置复制预览请求体"""
    source_device: str
    target_devices: List[str] = []
    domains: List[str] = Field(default_factory=lambda: list(CLONE_DEFAULT_DOMAINS))
    auto_resolve: bool = True
    include_sensitive: bool = False
    refresh_config: bool = True


class CloneDeployRequest(BaseModel):
    """设备配置复制下发请求体"""
    preview_id: str
    confirm: bool = False
    save_config: bool = False


# ---------------------------------------------------------------------------
# 设备列表持久化（带文件锁）
# ---------------------------------------------------------------------------

def load_devices() -> List[dict]:
    """从 devices.json 加载设备列表。文件不存在或损坏时自动恢复。"""
    try:
        # 尝试使用 filelock（如未安装则回退到无锁版本）
        _lock = _acquire_lock()
    except ImportError:
        _lock = None

    if _lock:
        with _lock:
            return _do_load_devices()
    return _do_load_devices()


def _do_load_devices() -> List[dict]:
    if not DEVICES_FILE.exists():
        logger.info("devices.json 不存在，初始化为空列表")
        _do_save_devices([])
        return []
    try:
        with open(DEVICES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError("格式错误：期望 JSON 数组")
        return data
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"devices.json 解析失败 ({e})，已重置为空列表")
        _do_save_devices([])
        return []


def save_devices(devices: List[dict]) -> None:
    """保存设备列表到 devices.json（带文件锁）。"""
    try:
        _lock = _acquire_lock()
    except ImportError:
        _lock = None

    if _lock:
        with _lock:
            _do_save_devices(devices)
    else:
        _do_save_devices(devices)


def _do_save_devices(devices: List[dict]) -> None:
    with open(DEVICES_FILE, "w", encoding="utf-8") as f:
        json.dump(devices, f, indent=2, ensure_ascii=False)


def _acquire_lock():
    """尝试获取 filelock 实例。如未安装则抛出 ImportError。"""
    from filelock import FileLock
    lock_path = DEVICES_FILE.with_suffix(".json.lock")
    return FileLock(str(lock_path), timeout=3)


# ---------------------------------------------------------------------------
# 网络工具函数
# ---------------------------------------------------------------------------

def ping_host(ip: str, timeout: float = 2.0) -> bool:
    """
    使用系统 ping 命令检测主机是否在线。
    发送 2 个包，总超时紧凑，快速判断设备是否在线。
    """
    try:
        is_windows = sys.platform == "win32"
        if is_windows:
            # -n 2: 2个包, -w 超时(ms)
            cmd = ["ping", "-n", "2", "-w", str(int(timeout * 500)), ip]
        else:
            cmd = ["ping", "-c", "2", "-W", str(int(timeout / 2)), ip]

        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout + 1,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        logger.debug(f"Ping {ip} 超时")
        return False
    except Exception as e:
        logger.warning(f"Ping {ip} 异常: {e}")
        return False


def tcp_port_open(ip: str, port: int = 22, timeout: float = 0.8) -> bool:
    """检测 TCP 端口是否开放，用于补充 ping 扫描。"""
    try:
        with _create_management_socket(ip, port, timeout=timeout):
            return True
    except OSError:
        return False


def ssh_service_open(ip: str, timeout: float = 1.0) -> bool:
    """确认目标 22 端口是 SSH 服务，避免虚拟网卡环境里的 TCP 假阳性。"""
    sock = None
    try:
        sock = _create_management_socket(ip, 22, timeout=timeout)
        sock.settimeout(timeout)
        banner = sock.recv(64)
        return banner.startswith(b"SSH-")
    except OSError:
        return False
    finally:
        if sock:
            try:
                sock.close()
            except OSError:
                pass


def _ip_sort_key(ip: str) -> tuple:
    try:
        return tuple(int(part) for part in ip.split("."))
    except ValueError:
        return (999, 999, 999, 999)


def _local_ipv4_addresses() -> set[str]:
    addresses = set()
    try:
        for addr_list in psutil.net_if_addrs().values():
            for addr in addr_list:
                if addr.family == socket.AF_INET and addr.address:
                    addresses.add(addr.address)
    except Exception as e:
        logger.debug(f"读取本机 IPv4 地址失败: {e}")
    return addresses


def _same_ipv4_network(source_ip: str, netmask: str, target_ip: str) -> bool:
    try:
        network = ipaddress.IPv4Network(f"{source_ip}/{netmask}", strict=False)
        return ipaddress.IPv4Address(target_ip) in network
    except ValueError:
        return False


def _management_source_ip_for(target_ip: str) -> Optional[str]:
    """选择连接目标设备时应绑定的本机管理网卡 IP，避免走代理/TUN 默认路由。"""
    selected = state.get("selected_interface") or {}
    selected_ip = selected.get("ip")
    selected_netmask = selected.get("netmask")
    if selected_ip and selected_netmask and _same_ipv4_network(selected_ip, selected_netmask, target_ip):
        return selected_ip

    try:
        for addr_list in psutil.net_if_addrs().values():
            for addr in addr_list:
                if addr.family != socket.AF_INET or not addr.address or not addr.netmask:
                    continue
                if _same_ipv4_network(addr.address, addr.netmask, target_ip):
                    return addr.address
    except Exception as e:
        logger.debug(f"选择管理源地址失败: {e}")

    return None


def _create_management_socket(ip: str, port: int = 22, timeout: float = 1.0) -> socket.socket:
    source_ip = _management_source_ip_for(ip)
    if source_ip:
        return socket.create_connection((ip, port), timeout=timeout, source_address=(source_ip, 0))
    return socket.create_connection((ip, port), timeout=timeout)


def _console_recv_all(sock: socket.socket, wait: float = 0.25, max_wait: float = 3.0) -> str:
    output = b""
    end_time = time.time() + max_wait
    # 关键：把 recv 超时设为「空闲等待时间」，使无数据时在 wait 秒后快速返回，
    # 否则会沿用调用方设置的长超时（如 console_deploy 的 8s），导致每条命令多等 8s。
    sock.settimeout(wait)
    while time.time() < end_time:
        try:
            chunk = sock.recv(8192)
        except socket.timeout:
            break
        except OSError:
            break
        if not chunk:
            break
        output += chunk
        end_time = time.time() + wait
    return output.decode("gbk", errors="replace").replace("\x00", "")


def _console_send_command(sock: socket.socket, command: str, max_wait: float = 5.0) -> str:
    sock.sendall((command + "\r").encode("ascii", errors="ignore"))
    time.sleep(0.25)
    output = _console_recv_all(sock, max_wait=max_wait)
    more_count = 0
    while "---- More ----" in output and more_count < 20:
        sock.sendall(b" ")
        time.sleep(0.15)
        output += _console_recv_all(sock, max_wait=2.0)
        more_count += 1
    return output


def _parse_console_prompt(text: str) -> str:
    return _regex_find(r"[<\[]([^<>\[\]\r\n]+)[>\]]", text or "")


def _strip_command_echo(text: str) -> str:
    cleaned = (text or "").replace("\x00", "")
    cleaned = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", cleaned)
    cleaned = re.sub(r"[\b\r]+", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def _console_probe_port(port: int) -> Optional[dict]:
    sock = None
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=0.6)
        sock.settimeout(0.8)
        sock.sendall(b"\r")
        time.sleep(0.2)
        prompt_text = _console_recv_all(sock, max_wait=1.0)
        name = _parse_console_prompt(prompt_text)
        if not name:
            return None

        outputs = {
            "display ip interface brief": _console_send_command(sock, "display ip interface brief", max_wait=5.0),
            "display current-configuration | include sysname|stelnet|ssh|local-user|user-interface|authentication-mode|protocol inbound|ip address": _console_send_command(
                sock,
                "display current-configuration | include sysname|stelnet|ssh|local-user|user-interface|authentication-mode|protocol inbound|ip address",
                max_wait=5.0,
            ),
            "display ssh server status": _console_send_command(sock, "display ssh server status", max_wait=4.0),
            "display arp": _console_send_command(sock, "display arp", max_wait=5.0),
        }

        ip_brief = outputs["display ip interface brief"]
        mgmt_ips = re.findall(r"\b(\d+\.\d+\.\d+\.\d+)/\d+\b", ip_brief)
        config_text = outputs[
            "display current-configuration | include sysname|stelnet|ssh|local-user|user-interface|authentication-mode|protocol inbound|ip address"
        ]
        ssh_status = outputs["display ssh server status"]
        ssh_configured = all(
            marker in (config_text + "\n" + ssh_status)
            for marker in [
                "stelnet server enable",
                "local-user admin service-type ssh",
                "authentication-mode aaa",
                "protocol inbound ssh",
            ]
        )

        selected_ip = (state.get("selected_interface") or {}).get("ip")
        can_ping_host = None
        ping_output = ""
        if selected_ip and mgmt_ips:
            ping_output = _console_send_command(sock, f"ping -c 1 {selected_ip}", max_wait=5.0)
            can_ping_host = " 0.00% packet loss" in ping_output or " 0% packet loss" in ping_output
            outputs[f"ping -c 1 {selected_ip}"] = ping_output

        host_checks = []
        for ip in mgmt_ips:
            host_checks.append({
                "ip": ip,
                "ping": ping_host(ip, timeout=1.5),
                "ssh_service": ssh_service_open(ip, timeout=1.5),
            })

        return {
            "port": port,
            "name": name,
            "mgmt_ips": mgmt_ips,
            "ssh_configured": ssh_configured,
            "can_ping_selected_interface": can_ping_host,
            "host_checks": host_checks,
            "summary": {
                "ssh_server_enabled": "Stelnet server                      :Enable" in ssh_status or "stelnet server enable" in config_text,
                "admin_ssh_enabled": "local-user admin service-type ssh" in config_text,
                "vty_ssh_only": "protocol inbound ssh" in config_text,
                "host_ping_output": _strip_command_echo(ping_output)[:600] if ping_output else "",
            },
            "outputs": {cmd: _strip_command_echo(out)[-2000:] for cmd, out in outputs.items()},
        }
    except OSError:
        return None
    except Exception as e:
        logger.debug("探测 console 端口 %s 失败: %s", port, e)
        return None
    finally:
        if sock:
            try:
                sock.close()
            except OSError:
                pass


def _list_listening_console_ports(start_port: int = 2000, end_port: int = 2050) -> List[int]:
    ports: set[int] = set()
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.status != psutil.CONN_LISTEN or not conn.laddr:
                continue
            port = conn.laddr.port
            if start_port <= port <= end_port:
                ports.add(port)
    except Exception as e:
        logger.debug("读取本地监听端口失败，回退到范围扫描: %s", e)

    if ports:
        return sorted(ports)
    return list(range(start_port, end_port + 1))


def diagnose_ensp_console_ports(start_port: int = 2000, end_port: int = 2050) -> dict:
    """通过 eNSP 本地 console 端口诊断已打开设备与管理网卡连通性。"""
    ports = _list_listening_console_ports(start_port, end_port)
    devices = []
    if ports:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(ports), 10)) as executor:
            future_map = {executor.submit(_console_probe_port, port): port for port in ports}
            for future in concurrent.futures.as_completed(future_map):
                try:
                    device = future.result()
                    if device:
                        devices.append(device)
                except Exception as e:
                    logger.debug("console 端口 %s 诊断失败: %s", future_map[future], e)
    devices.sort(key=lambda item: item["port"])

    selected = state.get("selected_interface")
    subnet = state.get("subnet")
    cloud_issue_devices = []
    if subnet:
        try:
            network = ipaddress.IPv4Network(subnet, strict=False)
        except ValueError:
            network = None
        if network:
            for device in devices:
                in_subnet = [
                    ip for ip in device["mgmt_ips"]
                    if ipaddress.IPv4Address(ip) in network
                ]
                if not in_subnet:
                    continue
                host_unreachable = all(
                    not check["ping"] and not check["ssh_service"]
                    for check in device["host_checks"]
                    if check["ip"] in in_subnet
                )
                device_cannot_ping_host = device.get("can_ping_selected_interface") is False
                if device["ssh_configured"] and (host_unreachable or device_cannot_ping_host):
                    cloud_issue_devices.append({
                        "name": device["name"],
                        "port": device["port"],
                        "mgmt_ips": in_subnet,
                    })

    return {
        "selected_interface": selected,
        "subnet": subnet,
        "console_devices": devices,
        "cloud_bridge_issue": bool(cloud_issue_devices),
        "cloud_issue_devices": cloud_issue_devices,
        "recommendation": (
            "Console 可见设备且 SSH 已配置，但宿主机与设备管理 IP 不互通；请检查 eNSP Cloud 的 UDP/网卡映射，"
            "确保 Cloud 绑定 VirtualBox Host-Only Network，并建立 UDP 端口到网卡的双向映射。"
            if cloud_issue_devices else ""
        ),
    }


def console_deploy(
    console_port: int,
    commands: List[str],
    verify_commands: Optional[List[str]] = None,
    save_config: bool = False,
    timeout: float = 8.0,
) -> dict:
    """通过 eNSP 本地 Console 端口（telnet localhost）下发配置，无需设备 IP。"""
    from services.device_connector import get_cli_error

    result = {
        "success": False,
        "console_port": console_port,
        "commands": commands,
        "command_outputs": [],
        "verify_commands": verify_commands or [],
        "verify_outputs": [],
        "save_result": "",
        "error": None,
    }
    sock = None
    try:
        sock = socket.create_connection(("127.0.0.1", int(console_port)), timeout=2.0)
        sock.settimeout(timeout)
        # 唤醒提示符：eNSP 控制台刚连上时通常要先发一个空行才出稳定提示符，
        # 否则首条命令（含 system-view）会被吞掉，导致后续命令在用户视图下失败。
        try:
            sock.sendall(b"\r")
            time.sleep(0.2)
        except Exception:
            pass
        _console_recv_all(sock, max_wait=0.4)

        # 稳健进入系统视图：VRP 首条命令偶发被吞，循环重试直到提示符变为 [xxx]
        for _ in range(3):
            out = _console_send_command(sock, "system-view", max_wait=3.0)
            if "[" in out:
                break
            time.sleep(0.3)

        all_ok = True
        for cmd in commands:
            out = _console_send_command(sock, cmd, max_wait=timeout)
            err = get_cli_error(out)
            result["command_outputs"].append({
                "command": cmd,
                "output": _strip_command_echo(out),
                "success": err is None,
                "error": err,
            })
            if err:
                all_ok = False

        _console_send_command(sock, "return", max_wait=3.0)

        for vcmd in (verify_commands or []):
            vout = _console_send_command(sock, vcmd, max_wait=timeout)
            result["verify_outputs"].append({
                "command": vcmd,
                "output": _strip_command_echo(vout),
                "success": True,
            })

        if save_config and all_ok:
            result["save_result"] = _console_save(sock, timeout)
        result["success"] = all_ok
    except OSError as e:
        result["error"] = f"无法连接 Console 端口 {console_port}: {e}"
    except Exception as e:
        result["error"] = str(e)
    finally:
        if sock:
            try:
                sock.close()
            except OSError:
                pass
    return result


def _console_save(sock, timeout: float = 8.0) -> str:
    """在用户视图执行 save 并处理 y/n 确认提示。"""
    try:
        sock.sendall(b"save\r")
        time.sleep(0.4)
        out = _console_recv_all(sock, max_wait=timeout)
        if "y/n" in out.lower() or "yes/no" in out.lower() or "[y]" in out.lower():
            sock.sendall(b"y\r")
            time.sleep(0.6)
            out += _console_recv_all(sock, max_wait=timeout)
        return _strip_command_echo(out)[:800]
    except Exception as e:
        return f"save_failed: {e}"


def probe_subnet(subnet: str, exclude_ip: Optional[str] = None, max_workers: int = 50) -> List[dict]:
    """
    并发探测网段中的可管理设备候选。
    只有 Ping 和 SSH 服务探测都通过的地址才进入候选列表。
    """
    try:
        network = ipaddress.IPv4Network(subnet, strict=False)
    except ValueError as e:
        logger.error(f"无效的网段格式: {subnet} - {e}")
        return []

    excluded_ips = _local_ipv4_addresses()
    if exclude_ip:
        excluded_ips.add(exclude_ip)

    hosts = [str(h) for h in network.hosts()]
    probes: List[dict] = []

    def _check(ip: str) -> Optional[dict]:
        if ip in excluded_ips:
            return None
        ping_ok = ping_host(ip, timeout=1.2)
        if not ping_ok:
            return None
        ssh_open = ssh_service_open(ip, timeout=1.0)
        if not ssh_open:
            return None
        return {
            "ip": ip,
            "ping": True,
            "ssh_port_open": True,
            "online": True,
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_check, ip): ip for ip in hosts}
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
                if result:
                    probes.append(result)
            except Exception as e:
                logger.warning(f"探测线程异常: {e}")

    probes.sort(key=lambda item: _ip_sort_key(item["ip"]))
    logger.info(f"网段 {subnet} 探测完成，发现 {len(probes)} 台 Ping+SSH 候选设备")
    return probes


def scan_subnet(subnet: str, exclude_ip: Optional[str] = None, max_workers: int = 50) -> List[str]:
    """
    并发扫描网段中的 Ping+SSH 候选设备。
    自动跳过网络地址、广播地址和指定的排除 IP。

    Args:
        subnet: 网段，如 "192.168.100.0/24"
        exclude_ip: 需要跳过的 IP（通常是本机 IP）
        max_workers: 并发线程数

    Returns:
        Ping 和 SSH 服务探测均通过的 IP 列表（已排序）
    """
    online_ips = [probe["ip"] for probe in probe_subnet(subnet, exclude_ip, max_workers)]
    logger.info(f"网段 {subnet} 扫描完成，发现 {len(online_ips)} 台 Ping+SSH 候选设备")
    return online_ips


# ---------------------------------------------------------------------------
# SSH 设备采集
# ---------------------------------------------------------------------------

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


def _valid_remote_mgmt_ip(ip: str) -> bool:
    try:
        addr = ipaddress.IPv4Address(ip)
        return not (addr.is_loopback or addr.is_link_local or addr.is_multicast)
    except ValueError:
        return False


def _lldp_neighbor_inventory(
    username: str = DEFAULT_SSH_USERNAME,
    password: str = DEFAULT_SSH_PASSWORD,
    device_type: str = DEFAULT_DEVICE_TYPE,
) -> Dict[str, dict]:
    """从已采集设备的 LLDP 信息中提取邻居管理地址。"""
    inventory: Dict[str, dict] = {}

    for source_ip, result in state.get("collection_results", {}).items():
        commands = result.get("commands", {})
        info = result.get("info") or {}

        # 旧结果可能是在 LLDP 详细解析修复前生成的；这里用原始命令重新解析一次。
        if commands:
            parsed_info = _parse_device_info(commands)
            if parsed_info.get("lldp_neighbors"):
                info = parsed_info
                result["info"] = parsed_info

        for neighbor in info.get("lldp_neighbors", []):
            remote_ip = (neighbor.get("remote_mgmt_ip") or "").strip()
            if not remote_ip or not _valid_remote_mgmt_ip(remote_ip):
                continue

            name = neighbor.get("remote_device") or _default_discovered_name(remote_ip)
            inventory[remote_ip] = {
                "name": name,
                "ip": remote_ip,
                "username": username,
                "password": password,
                "device_type": device_type,
                "discovered_by": "lldp",
                "source_ip": source_ip,
            }

    return inventory


def _sync_lldp_devices(
    username: str = DEFAULT_SSH_USERNAME,
    password: str = DEFAULT_SSH_PASSWORD,
    device_type: str = DEFAULT_DEVICE_TYPE,
) -> List[dict]:
    """把 LLDP 中带管理 IP 且 Ping+SSH 通过的邻居同步到 devices.json。"""
    inventory = _lldp_neighbor_inventory(username, password, device_type)
    if not inventory:
        return []

    devices = load_devices()
    existing = {d["ip"]: d for d in devices}
    added: List[dict] = []

    for ip, device in inventory.items():
        if ip in existing:
            if existing[ip].get("name", "").startswith("Host-") and device.get("name"):
                existing[ip]["name"] = device["name"]
            continue

        online = ping_host(ip, timeout=1.5)
        if not online:
            state["collection_results"][ip] = {
                "ip": ip,
                "name": device["name"],
                "online": False,
                "ssh_ok": False,
                "ssh_error": f"通过 LLDP 从 {device.get('source_ip', '未知设备')} 发现，但 Ping 不可达，未写入设备列表",
                "commands": {},
                "info": {"hostname": device["name"]},
            }
            continue

        identity = ssh_identify_device(device)
        if not identity.get("ssh_ok"):
            state["collection_results"][ip] = {
                "ip": ip,
                "name": device["name"],
                "online": True,
                "ssh_ok": False,
                "ssh_error": f"通过 LLDP 发现，但 SSH 登录检测失败，未写入设备列表: {identity.get('ssh_error', '未知错误')}",
                "commands": {},
                "info": {"hostname": device["name"]},
            }
            logger.info("LLDP 发现 %s(%s)，但 SSH 未通过，未写入设备列表", device["name"], ip)
            continue

        new_device = {
            "name": identity.get("name") or device["name"],
            "ip": ip,
            "username": device.get("username", username),
            "password": device.get("password", password),
            "device_type": device.get("device_type", device_type),
        }
        devices.append(new_device)
        existing[ip] = new_device
        added.append(new_device)

        state["collection_results"].setdefault(ip, {
            "ip": ip,
            "name": new_device["name"],
            "online": True,
            "ssh_ok": True,
            "ssh_error": "",
            "commands": {},
            "info": {
                "hostname": new_device["name"],
                "model": identity.get("model", ""),
            },
        })

    if added:
        devices.sort(key=lambda item: _ip_sort_key(item["ip"]))
        save_devices(devices)
        logger.info("通过 LLDP 同步新增设备: %s", ", ".join(f"{d['name']}({d['ip']})" for d in added))

    return added


def discover_devices_from_subnet(req: DeviceDiscoverRequest) -> dict:
    """扫描管理网段并把在线设备同步为设备列表。"""
    subnet = (req.subnet or state.get("subnet") or "").strip()
    if not subnet:
        raise HTTPException(status_code=400, detail="请先选择管理网卡，或输入要扫描的管理网段")

    try:
        network = ipaddress.IPv4Network(subnet, strict=False)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"无效的网段: {subnet}")

    if network.num_addresses > 4096:
        raise HTTPException(status_code=400, detail="扫描网段过大，请使用 /24 或更小的管理网段")

    local_ip = state["selected_interface"]["ip"] if state.get("selected_interface") else None
    probes = probe_subnet(str(network), exclude_ip=local_ip, max_workers=req.max_workers)
    lldp_inventory = _lldp_neighbor_inventory(req.username, req.password, req.device_type)
    candidates: List[dict] = []
    candidate_results: Dict[str, dict] = {}

    for probe in probes:
        ip = probe["ip"]
        device = {
            "name": lldp_inventory.get(ip, {}).get("name") or _default_discovered_name(ip),
            "ip": ip,
            "username": req.username,
            "password": req.password,
            "device_type": req.device_type,
        }
        candidates.append(device)
        candidate_results[ip] = {
            "ip": ip,
            "name": device["name"],
            "online": True,
            "ssh_ok": False,
            "ssh_error": "SSH 端口已开放，尚未完成登录识别",
            "commands": {},
            "info": {},
            "probe": probe,
        }

    if req.ssh_identify and candidates:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(candidates), 10)) as executor:
            future_map = {executor.submit(ssh_identify_device, d): d for d in candidates}
            for future in concurrent.futures.as_completed(future_map):
                device = future_map[future]
                ip = device["ip"]
                try:
                    identity = future.result()
                    if identity.get("name"):
                        device["name"] = identity["name"]
                    result = candidate_results[ip]
                    result["name"] = device["name"]
                    result["ssh_ok"] = identity.get("ssh_ok", False)
                    result["ssh_error"] = identity.get("ssh_error", "")
                    result["info"] = {
                        "hostname": device["name"] if identity.get("ssh_ok") else "",
                        "model": identity.get("model", ""),
                    }
                except Exception as e:
                    candidate_results[ip]["ssh_error"] = f"识别异常: {e}"

    discovered: List[dict] = []
    discovery_results: Dict[str, dict] = {}
    ignored: List[dict] = []

    for device in candidates:
        ip = device["ip"]
        result = candidate_results[ip]
        probe = result.get("probe", {})
        accepted = bool(probe.get("ping")) and bool(result.get("ssh_ok"))
        if accepted:
            discovered.append(device)
            discovery_results[ip] = result
        else:
            ignored.append({
                "ip": ip,
                "reason": "未同时通过 Ping 和 SSH 登录检测",
                "ssh_error": result.get("ssh_error", ""),
                "probe": probe,
            })

    discovered.sort(key=lambda item: _ip_sort_key(item["ip"]))
    suspicious_limit = 64
    suspicious_scan = len(candidates) > suspicious_limit

    if suspicious_scan:
        logger.warning(
            "自动发现结果异常偏多: 网段 %s 发现 %d 台，已阻止写入设备列表",
            network,
            len(candidates),
        )

    if req.replace and not req.dry_run and not suspicious_scan:
        devices = discovered
        save_devices(devices)
    elif discovered and not req.dry_run and not suspicious_scan:
        devices_by_ip = {d["ip"]: d for d in load_devices()}
        for device in discovered:
            devices_by_ip[device["ip"]] = device
        devices = sorted(devices_by_ip.values(), key=lambda item: _ip_sort_key(item["ip"]))
        save_devices(devices)
    elif discovered:
        devices = discovered
    else:
        devices = load_devices()

    for ip, result in discovery_results.items():
        state["collection_results"][ip] = result

    return {
        "subnet": str(network),
        "online_ips": [probe["ip"] for probe in probes],
        "candidate_count": len(candidates),
        "discovered": discovered,
        "ignored": ignored,
        "devices": devices,
        "discovery_results": discovery_results,
        "replaced": bool(req.replace and not req.dry_run and not suspicious_scan),
        "dry_run": req.dry_run,
        "suspicious": suspicious_scan,
        "message": (
            f"发现结果异常偏多（{len(candidates)} 台），疑似虚拟网卡假阳性，已保留原设备列表"
            if suspicious_scan else ""
        ),
    }





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


# ---------------------------------------------------------------------------
# 命令输出解析
# ---------------------------------------------------------------------------

def _parse_device_info(commands: dict) -> dict:
    """
    从采集到的命令输出中解析结构化信息。
    所有解析失败的情况均返回空字符串，不抛出异常。
    """
    info = {
        "version": "",
        "model": "",
        "hostname": "",
        "interfaces": [],
        "vlans": [],
        "routes": [],
        "ospf_peers": [],
        "lldp_neighbors": [],
        "cpu_usage": "",
        "memory_usage": "",
        "protocols": [],
        "config_sections": {},
    }

    # --- display version ---
    _try_extract(commands, "display version", lambda out: (
        _update(info, "model", _regex_find(r"(AR\d+|S\d+|NE\d+|CE\d+|USG\d+|Router|Switch)\S*", out)),
        _update(info, "version", _regex_find(r"Version\s+([\d.]+\w*)", out)),
        _update(info, "hostname", _regex_find(r"<(\S+)>", out)),
    ))

    # --- display ip interface brief ---
    _try_extract(commands, "display ip interface brief", lambda out: (
        _update(info, "interfaces", _extract_table(out, r"^(\S+)\s+(\S+)\s+(\S+)\s+(\S+)", 4)),
    ))

    # --- display vlan ---
    _try_extract(commands, "display vlan", lambda out: (
        _update(info, "vlans", _extract_lines_containing(out, ["VLAN ID", "VLAN ID"])),
    ))

    # --- display ip routing-table ---
    _try_extract(commands, "display ip routing-table", lambda out: (
        _update(info, "routes", _extract_lines_containing(out, ["/", "Static", "RIP", "OSPF", "Direct"])),
    ))

    # --- display ospf peer ---
    _try_extract(commands, "display ospf peer", lambda out: (
        _update(info, "ospf_peers", _extract_table(out, r"^(\S+)\s+(\d+\.\d+\.\d+\.\d+)\s+(\d+)", 3)),
    ))

    # --- display lldp neighbor brief ---
    _try_extract(commands, "display lldp neighbor brief", lambda out: (
        _update(info, "lldp_neighbors", _parse_lldp_neighbors(out)),
    ))

    # --- display lldp neighbor (详细): 补充管理 IP ---
    _try_extract(commands, "display lldp neighbor", lambda out: (
        _merge_lldp_detail(info, _parse_lldp_neighbor_detail(out)),
    ))

    # --- display cpu-usage ---
    _try_extract(commands, "display cpu-usage", lambda out: (
        _update(info, "cpu_usage",
               _regex_find(r"CPU Usage\s*:\s*(\d+)%", out)
               or _regex_find(r"(\d+)%", out) + "%"),
    ))

    # --- display memory-usage ---
    _try_extract(commands, "display memory-usage", lambda out: (
        _update(info, "memory_usage", _regex_find(r"(\d+)%", out) + "%"),
    ))

    # --- display current-configuration: 解析协议配置段 ---
    _try_extract(commands, "display current-configuration", lambda out: (
        _parse_config_sections(info, out),
    ))

    return info


def _try_extract(commands: dict, cmd: str, fn) -> None:
    """安全地执行一组解析操作，忽略异常。"""
    cmd_data = commands.get(cmd, {})
    if cmd_data.get("success"):
        try:
            fn(cmd_data["output"])
        except Exception as e:
            logger.debug(f"解析 '{cmd}' 输出时异常: {e}")


def _update(info: dict, key: str, value) -> None:
    if value:
        info[key] = value


def _regex_find(pattern: str, text: str) -> str:
    """正则查找，返回第一个匹配组或空字符串。"""
    m = re.search(pattern, text)
    return m.group(1) if m else ""


def _extract_table(output: str, pattern: str, min_groups: int) -> List[str]:
    """从命令输出中提取表格行。"""
    lines = []
    for line in output.strip().split("\n"):
        m = re.match(pattern, line.strip())
        if m and len(m.groups()) >= min_groups:
            lines.append(line.strip())
    return lines


def _extract_lines_containing(output: str, keywords: List[str]) -> List[str]:
    """提取包含指定关键词的文本行。"""
    lines = []
    for line in output.strip().split("\n"):
        if any(kw in line for kw in keywords):
            lines.append(line.strip())
    return lines


def _parse_lldp_neighbors(output: str) -> List[dict]:
    """
    解析 display lldp neighbor brief 输出，提取邻居信息。
    兼容华为设备的各种表头格式（全称/缩写）。

    典型输出格式：
        Local Intf     Neighbor Dev     Neighbor Intf     Expiry
        GE0/0/0        SW1              GE0/0/1           120s
    """
    if "no neighbor" in output.lower() or "no lldp neighbor" in output.lower():
        return []
    if "not enabled" in output.lower():
        return []

    neighbors = []
    for line in output.strip().split("\n"):
        line = line.strip()
        # 跳过空行
        if not line:
            continue
        # 跳过分隔线
        if re.match(r'^[\-=]+$', line):
            continue
        lower = line.lower()
        # 跳过表头行：包含 intf/neighbor/expiry 等关键词
        if any(kw in lower for kw in ("local intf", "loc intf", "neigh dev", "neigh intf", "expiry")):
            continue
        # 按 2+ 空格分割
        parts = re.split(r"\s{2,}", line)
        if len(parts) >= 3:
            neighbors.append({
                "local_interface": parts[0].strip(),
                "remote_device": parts[1].strip(),
                "remote_interface": parts[2].strip() if len(parts) > 2 else "",
                "remote_mgmt_ip": "",
            })
    return neighbors


def _parse_lldp_neighbor_detail(output: str) -> List[dict]:
    """
    解析 display lldp neighbor 详细输出，提取邻居名称和管理 IP。

    典型输出格式：
        AR1's neighbor(s) information:
        -------------------------------------------------------
        Local Port : GigabitEthernet0/0/0
        Neighbor device name : AR2
        Neighbor port ID : GigabitEthernet0/0/0
        Management address : 192.168.100.12
    """
    if "not enabled" in output.lower():
        return []

    neighbors: List[dict] = []
    current: Optional[dict] = None

    for line in output.split("\n"):
        stripped = line.strip()

        # 常见详细格式: GigabitEthernet0/0/0 has 1 neighbors:
        m = re.match(r"^(\S+)\s+has\s+\d+\s+neighbors?\s*[:：]", stripped, re.IGNORECASE)
        if m:
            if current and current.get("remote_device"):
                neighbors.append(current)
            current = {
                "local_interface": m.group(1),
                "remote_device": "",
                "remote_interface": "",
                "remote_mgmt_ip": "",
            }
            continue

        # 新的本地接口块
        m = re.match(r"Local\s+(?:Port|Interface|Intf)\s*[:\uff1a]\s*(\S+)", stripped, re.IGNORECASE)
        if m:
            if current and current.get("remote_device"):
                neighbors.append(current)
            current = {"local_interface": m.group(1), "remote_device": "", "remote_interface": "", "remote_mgmt_ip": ""}
            continue

        if not current:
            continue

        # 邻居设备名称
        m = re.match(r"Neighbor(?:\s+device)?\s+name\s*[:\uff1a]\s*(\S+)", stripped, re.IGNORECASE)
        if m:
            current["remote_device"] = m.group(1)
            continue

        # S5700 等设备常见字段
        m = re.match(r"System\s+name\s*[:\uff1a]\s*(\S+)", stripped, re.IGNORECASE)
        if m:
            current["remote_device"] = m.group(1)
            continue

        # 邻居接口
        m = re.match(r"Neighbor\s+port\s+ID\s*[:\uff1a]\s*(\S+)", stripped, re.IGNORECASE)
        if m:
            current["remote_interface"] = m.group(1)
            continue

        m = re.match(r"Port\s+ID\s*[:\uff1a]\s*(\S+)", stripped, re.IGNORECASE)
        if m:
            current["remote_interface"] = m.group(1)
            continue

        # 管理 IP
        m = re.match(r"Management\s+address\s*[:\uff1a]\s*(\S+)", stripped, re.IGNORECASE)
        if m:
            current["remote_mgmt_ip"] = m.group(1)

    if current and current.get("remote_device"):
        neighbors.append(current)

    return neighbors


# 华为接口名缩写归一化映射
_INTF_ABBREV = {
    "ge": "gigabitethernet",
    "xge": "tengigabitethernet",
    "fe": "fastethernet",
    "eth": "ethernet",
    "me": "meth",
    "se": "serial",
    "loop": "loopback",
    "vlanif": "vlanif",
}


def _normalize_intf(name: str) -> str:
    """归一化华为接口名，如 GE0/0/0 -> gigabitethernet0/0/0"""
    lower = name.lower().strip()
    for abbrev, full in _INTF_ABBREV.items():
        if lower.startswith(abbrev) and (len(lower) == len(abbrev) or not lower[len(abbrev)].isalpha()):
            return full + lower[len(abbrev):]
    return lower


def _merge_lldp_detail(info: dict, detail_list: List[dict]) -> None:
    """
    将详细 LLDP 信息（管理 IP）合并到 info["lldp_neighbors"]。
    以 (归一化后的 local_interface, remote_device) 为键匹配。
    """
    if not detail_list:
        return

    # 构建查找表: (normalized_local_intf, remote_dev_lower) -> detail
    lookup = {}
    for d in detail_list:
        key = (_normalize_intf(d.get("local_interface", "")), d.get("remote_device", "").lower())
        lookup[key] = d

    neighbors = info.get("lldp_neighbors", [])
    for n in neighbors:
        key = (_normalize_intf(n.get("local_interface", "")), n.get("remote_device", "").lower())
        detail = lookup.get(key)
        if detail:
            n["remote_mgmt_ip"] = detail.get("remote_mgmt_ip", "")
            if not n.get("remote_interface") and detail.get("remote_interface"):
                n["remote_interface"] = detail["remote_interface"]

    # 如果 brief 解析到 0 条但详细解析到了，直接用详细结果
    if not neighbors and detail_list:
        info["lldp_neighbors"] = detail_list


# ---------------------------------------------------------------------------
# 配置段解析（从 display current-configuration 提取协议配置）
# ---------------------------------------------------------------------------

# 每项规则会检查完整配置块内的所有行。这样不仅能获取协议进程块，也能获取
# interface 子视图内的 OSPF、NAT、DHCP、IS-IS 等相关配置。
_CONFIG_SECTION_RULES = [
    ("ospf",   "OSPF",    (r"^ospf(?:\s|$)",)),
    ("rip",    "RIP",     (r"^rip(?:\s|$)",)),
    ("bgp",    "BGP",     (r"^bgp(?:\s|$)",)),
    ("isis",   "IS-IS",   (r"^isis(?:\s|$)",)),
    ("static", "静态路由", (r"^ip route-static(?:\s|$)",)),
    ("nat",    "NAT",     (r"^nat(?:\s|$)",)),
    ("dhcp",   "DHCP",    (r"^dhcp(?:\s|$)", r"^ip pool(?:\s|$)")),
    ("acl",    "ACL",     (r"^acl(?:\s|$)",)),
    ("ntp",    "NTP",     (r"^ntp-service(?:\s|$)",)),
    ("snmp",   "SNMP",    (r"^snmp-agent(?:\s|$)",)),
    ("ipsec",  "IPSec",   (r"^ipsec(?:\s|$)", r"^ike(?:\s|$)")),
]


def _split_current_config_blocks(config_output: str) -> List[List[str]]:
    """按 VRP 的 # 分隔符切分配置，同时保留原始缩进和父视图。"""
    blocks: List[List[str]] = []
    current: List[str] = []

    def flush() -> None:
        if current and any(
            line.strip()
            and not re.match(r"^\[V\d", line.strip(), re.IGNORECASE)
            and line.strip().lower() != "return"
            for line in current
        ):
            blocks.append(list(current))
        current.clear()

    for raw_line in config_output.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped == "#":
            flush()
            continue
        if not stripped or stripped.lower() == "return":
            continue
        if re.match(r"^\[V\d", stripped, re.IGNORECASE):
            continue
        current.append(line)

    flush()
    return blocks


def _config_block_matches(block: List[str], patterns: tuple) -> bool:
    return any(
        re.match(pattern, line.strip(), re.IGNORECASE)
        for line in block
        for pattern in patterns
    )


def _parse_config_sections(info: dict, config_output: str) -> None:
    """
    从 display current-configuration 输出中解析各协议/功能配置段。
    结果写入 info['protocols'] 和 info['config_sections']。
    """
    if not config_output:
        return

    protocols: List[str] = []
    config_sections: Dict[str, dict] = {}
    blocks = _split_current_config_blocks(config_output)

    for section_id, display_name, patterns in _CONFIG_SECTION_RULES:
        section_blocks = [
            block for block in blocks
            if _config_block_matches(block, patterns)
        ]
        if section_blocks:
            protocols.append(section_id)
            config_sections[section_id] = {
                "name": display_name,
                "configured": True,
                "config": "\n#\n".join("\n".join(block) for block in section_blocks),
            }

    info["protocols"] = protocols
    info["config_sections"] = config_sections


# ---------------------------------------------------------------------------
# 协议配置命令生成器
# ---------------------------------------------------------------------------
def generate_delete_commands(protocol: str, device: dict) -> List[str]:
    """生成删除协议配置的命令"""
    commands = []
    if protocol == "ospf":
        commands.append("undo ospf 1")
    elif protocol == "bgp":
        result = state["collection_results"].get(device["ip"], {})
        config = result.get("info", {}).get("config_sections", {}).get("bgp", {}).get("config", "")
        import re
        m = re.search(r"bgp\s+(\d+)", config)
        if m:
            commands.append(f"undo bgp {m.group(1)}")
    elif protocol == "rip":
        commands.append("undo rip")
    elif protocol == "isis":
        commands.append("undo isis")
    elif protocol == "static_route":
        commands.append("undo ip route-static all")
    elif protocol == "dhcp":
        commands.append("undo ip pool all")
    elif protocol == "acl":
        commands.append("undo acl all")
    elif protocol == "ntp":
        commands.append("undo ntp-service unicast-server all")
    elif protocol == "snmp":
        commands.append("undo snmp-agent community all")
    return commands


def push_config_to_device(device: dict, commands: List[str]) -> dict:
    """推送配置到设备"""
    conn = None
    ssh_sock = None
    try:
        ConnectHandler, _, _ = _import_netmiko()

        conn_params = {
            "device_type": device.get("device_type", DEFAULT_DEVICE_TYPE),
            "host": device["ip"],
            "username": device.get("username", DEFAULT_SSH_USERNAME),
            "password": device.get("password", DEFAULT_SSH_PASSWORD),
            "timeout": SSH_TIMEOUT,
        }
        source_ip = _management_source_ip_for(device["ip"])
        if source_ip:
            ssh_sock = _create_management_socket(device["ip"], 22, timeout=SSH_TIMEOUT)
            conn_params["sock"] = ssh_sock

        conn = ConnectHandler(
            **conn_params,
        )
        ssh_sock = None

        # 进入系统视图并执行配置
        conn.send_command_timing("system-view")
        output = conn.send_config_set(commands, exit_config_mode=False)
        conn.send_command_timing("return")

        error_markers = (
            "unrecognized command",
            "wrong parameter",
            "incomplete command",
            "error:",
        )
        if any(marker in output.lower() for marker in error_markers):
            logger.error("设备 %s 拒绝配置命令: %s", device["ip"], output)
            return {
                "success": False,
                "output": output,
                "error": "设备拒绝了一个或多个配置命令",
            }

        conn.save_config()

        logger.info(f"配置已推送到 {device['ip']}: {commands}")
        return {"success": True, "output": output}
    except Exception as e:
        logger.error(f"推送配置到 {device['ip']} 失败: {e}")
        return {"success": False, "error": str(e)}
    finally:
        if conn:
            try:
                conn.disconnect()
            except Exception:
                pass
        elif ssh_sock:
            try:
                ssh_sock.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 拓扑生成
# ---------------------------------------------------------------------------

def generate_topology(devices: List[dict], collection_results: dict) -> dict:
    """
    根据所有设备的 LLDP 邻居信息生成拓扑关系。
    自动去重，并尝试匹配对端设备 IP。
    """
    links = []
    seen_links = set()

    for device in devices:
        ip = device["ip"]
        result = collection_results.get(ip, {})
        info = result.get("info", {})
        lldp_neighbors = info.get("lldp_neighbors", [])
        hostname = info.get("hostname") or device.get("name", ip)

        # 建立设备名称到 IP 的映射
        name_to_ip = {}
        for d in devices:
            d_result = collection_results.get(d["ip"], {})
            d_info = d_result.get("info", {})
            d_name = d_info.get("hostname") or d.get("name", "")
            if d_name:
                name_to_ip[d_name] = d["ip"]
            name_to_ip[d.get("name", "")] = d["ip"]

        for neighbor in lldp_neighbors:
            remote_dev = neighbor.get("remote_device", "")
            local_intf = neighbor.get("local_interface", "")
            remote_intf = neighbor.get("remote_interface", "")
            remote_mgmt_ip = neighbor.get("remote_mgmt_ip", "")
            if not remote_dev:
                continue

            # 去重
            link_key = tuple(sorted([hostname, remote_dev])) + (local_intf,)
            if link_key in seen_links:
                continue
            seen_links.add(link_key)

            # 优先用名称映射的 IP，其次用 LLDP 管理 IP
            target_ip = name_to_ip.get(remote_dev, "") or remote_mgmt_ip

            links.append({
                "source": hostname,
                "source_ip": ip,
                "target": remote_dev,
                "target_ip": target_ip,
                "local_interface": local_intf,
                "remote_interface": remote_intf,
            })

    return {"links": links}


# ---------------------------------------------------------------------------
# API 路由
# ---------------------------------------------------------------------------

def _get_windows_adapter_metadata() -> Dict[str, dict]:
    """读取 Windows 网卡描述信息，失败时返回空字典。"""
    if os.name != "nt":
        return {}

    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "Get-NetAdapter | "
                    "Select-Object Name,InterfaceDescription,ifIndex | "
                    "ConvertTo-Json -Compress"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=5,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0 or not result.stdout.strip():
            return {}

        parsed = json.loads(result.stdout)
        if isinstance(parsed, dict):
            parsed = [parsed]

        metadata = {}
        for item in parsed:
            name = item.get("Name")
            if name:
                metadata[name] = {
                    "description": item.get("InterfaceDescription", "") or "",
                    "if_index": item.get("ifIndex"),
                }
        return metadata
    except Exception as e:
        logger.debug(f"读取 Windows 网卡描述失败: {e}")
        return {}


def _is_link_local_ipv4(ip: str) -> bool:
    try:
        return ipaddress.IPv4Address(ip).is_link_local
    except ValueError:
        return False


def _is_loopback_ipv4(ip: str) -> bool:
    try:
        return ipaddress.IPv4Address(ip).is_loopback
    except ValueError:
        return False


def _pick_best_ipv4(candidates: List[dict]) -> Optional[dict]:
    """同一网卡多个 IPv4 时，优先选择可用于管理网段的地址。"""
    if not candidates:
        return None

    def _score(candidate: dict) -> int:
        ip = candidate.get("address", "")
        score = 0
        if ip == EXPECTED_MANAGEMENT_IP:
            score += 100
        if not _is_link_local_ipv4(ip):
            score += 20
        if not _is_loopback_ipv4(ip):
            score += 20
        try:
            addr = ipaddress.IPv4Address(ip)
            if addr.is_private:
                score += 10
        except ValueError:
            pass
        return score

    return max(candidates, key=_score)


def _rank_interface(info: dict) -> tuple[int, List[str]]:
    """给网卡打分，用于自动推荐 eNSP 管理网卡。"""
    name = info.get("name", "")
    description = info.get("description", "")
    text = f"{name} {description}".lower()
    ip = info.get("ipv4", "")

    score = 0
    reasons: List[str] = []

    if info.get("status") == "UP":
        score += 20
    else:
        score -= 15

    if ip:
        score += 10
        if _is_link_local_ipv4(ip):
            score -= 35
        if _is_loopback_ipv4(ip):
            score -= 60

    if ip == EXPECTED_MANAGEMENT_IP:
        score += 100
        reasons.append(f"匹配默认管理 IP {EXPECTED_MANAGEMENT_IP}")

    if "virtualbox" in text or "host-only" in text:
        score += 80
        reasons.append("VirtualBox Host-Only 网卡")

    if "vmware" in text or "vmnet" in text:
        score += 45
        reasons.append("VMware 虚拟网卡")

    if "npcap" in text or "km-test" in text:
        score += 35
        reasons.append("虚拟/抓包适配器")

    if "loopback" in text and "pseudo-interface" not in text:
        score += 25
        reasons.append("Loopback 网卡")

    if any(kw.lower() in text for kw in EXCLUDED_INTERFACE_KEYWORDS):
        score -= 80
        reasons = []

    if ip and not _is_link_local_ipv4(ip) and not _is_loopback_ipv4(ip):
        try:
            host = ipaddress.IPv4Address(ip)
            if host.is_private and str(host).endswith(".1"):
                score += 8
                reasons.append("私有网段网关地址")
        except ValueError:
            pass

    return score, reasons


def _finalize_interfaces(interfaces: List[dict]) -> List[dict]:
    """补充识别字段并按推荐程度排序。"""
    best_index = None
    best_score = -10_000

    for idx, iface in enumerate(interfaces):
        score, reasons = _rank_interface(iface)
        iface["_score"] = score
        iface["is_ensp"] = score >= 40
        iface["is_recommended"] = False
        iface["recommendation_reason"] = " / ".join(dict.fromkeys(reasons))

        if iface.get("ipv4") and score > best_score:
            best_score = score
            best_index = idx

    if best_index is not None and best_score >= 40:
        interfaces[best_index]["is_recommended"] = True

    interfaces.sort(
        key=lambda iface: (
            not iface.get("is_recommended", False),
            not iface.get("is_ensp", False),
            iface.get("status") != "UP",
            -iface.get("_score", 0),
            iface.get("name", ""),
        )
    )

    for iface in interfaces:
        iface.pop("_score", None)

    return interfaces


_lab_sessions: Dict[str, dict] = {}


_lab_sessions: Dict[str, dict] = {}  # 实验会话（AI 模块与启动逻辑共享）

# 供路由模块 from core import * 一次性拿到全部符号（含下划线私有助手）
__all__ = [n for n in globals() if not n.startswith("__")]
