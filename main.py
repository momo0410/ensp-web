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
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from services.capability_probe import probe_capabilities, probe_capabilities_parallel, probe_capabilities_from_cache
from services.role_detector import detect_protocol_roles
from services import ai_assistant
from services.ai_assistant import AIError
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
app = FastAPI(title="eNSP Web 管理系统", version="2.0.0")

# 允许从本地 HTML 直接打开页面时访问后端接口。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载静态文件
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

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

@app.get("/")
async def index():
    """返回前端页面。"""
    return FileResponse(str(STATIC_DIR / "index.html"))


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


@app.get("/api/interfaces")
async def get_interfaces():
    """读取本机 Windows 网卡信息。"""
    interfaces = []
    try:
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        metadata = _get_windows_adapter_metadata()
    except Exception as e:
        logger.error(f"psutil 读取网卡信息失败: {e}")
        # 尝试使用 ipconfig 作为备用方案
        interfaces = _finalize_interfaces(_fallback_get_interfaces())
        if not interfaces:
            raise HTTPException(
                status_code=500,
                detail=f"读取网卡信息失败: {e}。请检查是否有管理员权限，或尝试重启后端。",
            )
        logger.info(f"使用 ipconfig 备用方案读取到 {len(interfaces)} 张网卡")
        recommended = next((i for i in interfaces if i.get("is_recommended")), None)
        return {
            "success": True,
            "interfaces": interfaces,
            "recommended_interface": recommended,
            "fallback": True,
        }

    for name, addr_list in addrs.items():
        meta = metadata.get(name, {})
        info = {
            "name": name,
            "description": meta.get("description", ""),
            "if_index": meta.get("if_index"),
            "ipv4": "",
            "ipv4_addresses": [],
            "netmask": "",
            "mac": "",
            "status": "DOWN",
            "subnet": "",
            "is_ensp": False,
            "is_recommended": False,
            "recommendation_reason": "",
        }

        ipv4_candidates = []
        for addr in addr_list:
            try:
                if addr.family == socket.AF_INET:
                    candidate = {
                        "address": addr.address or "",
                        "netmask": addr.netmask or "",
                        "subnet": "",
                    }
                    if addr.netmask and addr.address:
                        try:
                            network = ipaddress.IPv4Network(
                                f"{addr.address}/{addr.netmask}", strict=False
                            )
                            candidate["subnet"] = str(network)
                        except ValueError:
                            pass
                    if candidate["address"]:
                        ipv4_candidates.append(candidate)
                elif addr.family == getattr(psutil, "AF_LINK", -1) or (
                    hasattr(addr, "family") and addr.family == -1
                ):
                    info["mac"] = addr.address or ""
            except Exception as ex:
                logger.debug(f"解析网卡 {name} 地址信息时异常: {ex}")

        best_ipv4 = _pick_best_ipv4(ipv4_candidates)
        if best_ipv4:
            info["ipv4"] = best_ipv4["address"]
            info["netmask"] = best_ipv4["netmask"]
            info["subnet"] = best_ipv4["subnet"]
            info["ipv4_addresses"] = ipv4_candidates

        # 网卡状态
        try:
            if name in stats:
                info["status"] = "UP" if stats[name].isup else "DOWN"
        except Exception as ex:
            logger.debug(f"读取网卡 {name} 状态时异常: {ex}")

        # 只保留有 IPv4 地址或名称匹配 eNSP 关键词的网卡，过滤无用的
        if info["ipv4"] or info["mac"]:
            interfaces.append(info)

    interfaces = _finalize_interfaces(interfaces)
    recommended = next((i for i in interfaces if i.get("is_recommended")), None)
    logger.info(f"读取到 {len(interfaces)} 张网卡")
    if recommended:
        logger.info(
            "推荐管理网卡: %s (%s), 原因: %s",
            recommended["name"],
            recommended["ipv4"],
            recommended.get("recommendation_reason") or "规则匹配",
        )
    return {"success": True, "interfaces": interfaces, "recommended_interface": recommended}


def _fallback_get_interfaces() -> List[dict]:
    """当 psutil 失败时，使用 ipconfig /all 作为备用方案读取网卡信息。"""
    interfaces = []
    try:
        result = subprocess.run(
            ["ipconfig", "/all"],
            capture_output=True,
            text=True,
            timeout=10,
            encoding="gbk",
            errors="replace",
        )
        output = result.stdout

        current_iface = None
        for line in output.split("\n"):
            line = line.strip()
            # 网卡名称行（不以空格开头且包含适配器）
            if "adapter" in line.lower() or "适配器" in line:
                if current_iface and current_iface.get("ipv4"):
                    interfaces.append(current_iface)
                iface_name = line.rstrip(":").strip()
                current_iface = {
                    "name": iface_name,
                    "description": "",
                    "if_index": None,
                    "ipv4": "",
                    "ipv4_addresses": [],
                    "netmask": "",
                    "mac": "",
                    "status": "UP",
                    "subnet": "",
                    "is_ensp": False,
                    "is_recommended": False,
                    "recommendation_reason": "",
                }
            elif current_iface:
                if "IPv4" in line or "IPv4 地址" in line:
                    parts = line.split(":")
                    if len(parts) >= 2:
                        ip = parts[-1].strip().rstrip("(首选)")
                        current_iface["ipv4"] = ip
                        current_iface["ipv4_addresses"] = [{
                            "address": ip,
                            "netmask": current_iface.get("netmask", ""),
                            "subnet": "",
                        }]
                elif "子网掩码" in line or "Subnet Mask" in line:
                    parts = line.split(":")
                    if len(parts) >= 2:
                        current_iface["netmask"] = parts[-1].strip()
                        for item in current_iface.get("ipv4_addresses", []):
                            item["netmask"] = current_iface["netmask"]
                elif "物理地址" in line or "Physical Address" in line:
                    parts = line.split(":")
                    if len(parts) >= 2:
                        current_iface["mac"] = parts[-1].strip()

        # 别忘了最后一个
        if current_iface and current_iface.get("ipv4"):
            interfaces.append(current_iface)

        # 补充子网信息和 eNSP 判断
        for iface in interfaces:
            if iface["ipv4"] and iface["netmask"]:
                try:
                    network = ipaddress.IPv4Network(
                        f"{iface['ipv4']}/{iface['netmask']}", strict=False
                    )
                    iface["subnet"] = str(network)
                except ValueError:
                    pass
                for item in iface.get("ipv4_addresses", []):
                    if item.get("address") == iface["ipv4"]:
                        item["subnet"] = iface["subnet"]

        logger.info(f"ipconfig 备用方案解析到 {len(interfaces)} 张网卡")
    except Exception as e:
        logger.error(f"ipconfig 备用方案也失败: {e}")

    return interfaces


@app.post("/api/interfaces/select")
async def select_interface(req: InterfaceSelect):
    """选择某张网卡作为 eNSP 管理网卡。"""
    state["selected_interface"] = {
        "name": req.name,
        "ip": req.ip,
        "netmask": req.netmask,
    }
    try:
        network = ipaddress.IPv4Network(f"{req.ip}/{req.netmask}", strict=False)
        state["subnet"] = str(network)
    except ValueError:
        state["subnet"] = None

    logger.info(f"选择管理网卡: {req.name} ({req.ip}), 网段: {state['subnet']}")
    return {
        "success": True,
        "selected": state["selected_interface"],
        "subnet": state["subnet"],
    }


@app.post("/api/scan-by-interface")
async def scan_by_interface():
    """根据已选网卡扫描对应网段。"""
    if not state["subnet"]:
        raise HTTPException(status_code=400, detail="请先选择管理网卡")
    if not state["selected_interface"]:
        raise HTTPException(status_code=400, detail="请先选择管理网卡")

    local_ip = state["selected_interface"]["ip"]
    online_ips = scan_subnet(state["subnet"], exclude_ip=local_ip)
    return {
        "success": True,
        "subnet": state["subnet"],
        "local_ip": local_ip,
        "online_ips": online_ips,
    }


@app.post("/api/scan")
async def scan(req: ScanRequest):
    """扫描指定网段。"""
    state["subnet"] = req.subnet
    local_ip = state["selected_interface"]["ip"] if state["selected_interface"] else None
    online_ips = scan_subnet(req.subnet, exclude_ip=local_ip)
    return {
        "success": True,
        "subnet": req.subnet,
        "online_ips": online_ips,
    }


@app.post("/api/discover-devices")
async def discover_devices(req: Optional[DeviceDiscoverRequest] = None):
    """扫描管理网段，自动发现 eNSP 中实际在线的设备并同步设备列表。"""
    req = req or DeviceDiscoverRequest()
    discovery = discover_devices_from_subnet(req)
    logger.info(
        "自动发现完成: 网段 %s, 在线 %d 台, 同步设备 %d 台",
        discovery["subnet"],
        len(discovery["online_ips"]),
        len(discovery["discovered"]),
    )
    return {"success": True, **discovery}


@app.get("/api/ensp-console-diagnostics")
async def ensp_console_diagnostics():
    """通过本地 eNSP console 端口诊断设备 SSH 配置与 Cloud 桥接状态。"""
    return {"success": True, **diagnose_ensp_console_ports()}


@app.get("/api/devices")
async def get_devices():
    """获取设备列表。"""
    devices = load_devices()
    return {"success": True, "devices": devices}


@app.post("/api/devices")
async def add_device(device: DeviceAdd):
    """手动添加设备。"""
    devices = load_devices()
    for d in devices:
        if d["ip"] == device.ip:
            raise HTTPException(status_code=400, detail=f"设备 {device.ip} 已存在")
    new_device = device.model_dump()
    devices.append(new_device)
    save_devices(devices)
    logger.info(f"添加设备: {device.name} ({device.ip})")
    return {"success": True, "device": new_device, "message": f"设备 {device.name}({device.ip}) 已添加"}


@app.put("/api/devices/{ip}")
async def update_device(ip: str, device: DeviceAdd):
    """更新设备连接配置。"""
    devices = load_devices()
    target_index = next((idx for idx, d in enumerate(devices) if d["ip"] == ip), None)
    if target_index is None:
        raise HTTPException(status_code=404, detail=f"未找到 IP 为 {ip} 的设备")

    if device.ip != ip and any(d["ip"] == device.ip for d in devices):
        raise HTTPException(status_code=400, detail=f"设备 {device.ip} 已存在")

    updated_device = device.model_dump()
    devices[target_index] = updated_device
    save_devices(devices)

    if device.ip != ip and ip in state["collection_results"]:
        state["collection_results"][device.ip] = state["collection_results"].pop(ip)
        state["collection_results"][device.ip]["ip"] = device.ip
        state["collection_results"][device.ip]["name"] = device.name

    logger.info(f"更新设备配置: {ip} -> {device.name} ({device.ip})")
    return {"success": True, "device": updated_device, "message": f"设备 {device.name} 配置已保存"}


@app.delete("/api/devices/{ip}")
async def delete_device(ip: str):
    """删除指定 IP 的设备。"""
    devices = load_devices()
    new_devices = [d for d in devices if d["ip"] != ip]
    if len(new_devices) == len(devices):
        raise HTTPException(status_code=404, detail=f"未找到 IP 为 {ip} 的设备")
    save_devices(new_devices)
    state["collection_results"].pop(ip, None)
    logger.info(f"删除设备: {ip}")
    return {"success": True, "message": f"设备 {ip} 已删除"}


def _has_valid_full_config(result: dict) -> bool:
    command = result.get("commands", {}).get("display current-configuration", {})
    return bool(command.get("success") and command.get("output"))


def _ensure_protocol_collection(ip: str) -> dict:
    """确保协议读取使用完整配置；实时失败时回退到最新完整备份。"""
    result = state["collection_results"].get(ip)
    if result and _has_valid_full_config(result):
        return result

    device = next((d for d in load_devices() if d["ip"] == ip), None)
    if not device:
        raise HTTPException(status_code=404, detail=f"未找到 IP 为 {ip} 的设备")

    loaded = _load_config_for_clone(device, refresh=True)
    if not loaded.get("success"):
        raise HTTPException(
            status_code=502,
            detail=f"获取设备配置失败: {'; '.join(loaded.get('errors', [])) or '没有可用的完整配置'}",
        )

    result = dict(result or {
        "ip": ip,
        "name": device.get("name", ip),
        "online": loaded.get("source") == "live",
        "ssh_ok": loaded.get("source") == "live",
        "ssh_error": "",
        "commands": {},
        "info": {},
    })
    result["commands"] = dict(result.get("commands", {}))
    result["commands"]["display current-configuration"] = {
        "success": True,
        "output": loaded["config"],
        "error": None,
    }
    result["info"] = dict(result.get("info", {}))
    _parse_config_sections(result["info"], loaded["config"])
    result["config_source"] = loaded.get("source", "cache")
    result["config_backup_file"] = loaded.get("backup_file", "")
    result["config_warnings"] = loaded.get("errors", [])
    if loaded.get("source") == "live":
        result["online"] = True
        result["ssh_ok"] = True
        result["ssh_error"] = ""
    state["collection_results"][ip] = result
    return result


@app.get("/api/devices/{ip}/protocols")
async def get_device_protocols(ip: str):
    """获取设备已配置的协议列表和完整协议配置。"""
    result = _ensure_protocol_collection(ip)

    info = result.get("info", {})
    protocols = info.get("protocols", [])
    config_sections = info.get("config_sections", {})

    return {
        "success": True,
        "ip": ip,
        "protocols": protocols,
        "config_sections": config_sections,
        "config_source": result.get("config_source", "cache"),
        "config_backup_file": result.get("config_backup_file", ""),
        "warnings": result.get("config_warnings", []),
    }


@app.get("/api/devices/{ip}/protocols/{protocol}")
async def get_device_protocol_config(ip: str, protocol: str):
    """获取指定协议的当前配置。"""
    result = _ensure_protocol_collection(ip)

    config_sections = result.get("info", {}).get("config_sections", {})
    protocol_config = config_sections.get(protocol, {})

    return {
        "success": True,
        "ip": ip,
        "protocol": protocol,
        "config": protocol_config,
        "config_source": result.get("config_source", "cache"),
        "config_backup_file": result.get("config_backup_file", ""),
        "warnings": result.get("config_warnings", []),
    }


@app.get("/api/devices/{ip}/current-config")
async def get_device_current_config(ip: str):
    """获取完整当前配置；实时不可用时返回最新完整备份并标明来源。"""
    result = _ensure_protocol_collection(ip)
    command = result["commands"]["display current-configuration"]
    return {
        "success": True,
        "ip": ip,
        "config": command["output"],
        "source": result.get("config_source", "cache"),
        "backup_file": result.get("config_backup_file", ""),
        "warnings": result.get("config_warnings", []),
    }


@app.get("/api/config-templates")
async def list_config_templates(role: Optional[str] = None):
    """返回所有配置模板（表单定义），供前端协议配置模态框统一渲染（消除前端硬编码）。"""
    templates = get_templates_for_role(role) if role else get_all_templates()
    return {"success": True, "templates": templates, "categories": get_categories()}


@app.post("/api/devices/{ip}/protocols/{protocol}")
async def apply_device_protocol_config(ip: str, protocol: str, req: ProtocolConfigRequest):
    """应用协议配置到设备"""
    devices = load_devices()
    device = next((d for d in devices if d["ip"] == ip), None)
    if not device:
        raise HTTPException(status_code=404, detail=f"未找到 IP 为 {ip} 的设备")

    # 生成配置命令（统一走声明式模板渲染器，消除双数据源）
    if req.mode == "command":
        commands = req.commands
    else:
        commands = build_config_commands(protocol, req.params)

    if not commands:
        raise HTTPException(status_code=400, detail="未生成任何配置命令")

    # 推送配置到设备
    result = push_config_to_device(device, commands)
    if not result.get("success"):
        raise HTTPException(status_code=500, detail=f"配置推送失败: {result.get('error', '未知错误')}")

    # 重新采集设备信息以更新协议状态
    collect_result = ssh_collect_device(device)
    state["collection_results"][ip] = collect_result

    return {
        "success": True,
        "ip": ip,
        "protocol": protocol,
        "commands": commands,
        "output": result.get("output", ""),
    }


@app.delete("/api/devices/{ip}/protocols/{protocol}")
async def delete_device_protocol_config(ip: str, protocol: str):
    """删除协议配置"""
    devices = load_devices()
    device = next((d for d in devices if d["ip"] == ip), None)
    if not device:
        raise HTTPException(status_code=404, detail=f"未找到 IP 为 {ip} 的设备")

    # 生成删除命令
    delete_commands = generate_delete_commands(protocol, device)
    if not delete_commands:
        raise HTTPException(status_code=400, detail=f"不支持删除协议: {protocol}")

    # 推送删除命令到设备
    result = push_config_to_device(device, delete_commands)
    if not result.get("success"):
        raise HTTPException(status_code=500, detail=f"删除配置失败: {result.get('error', '未知错误')}")

    # 重新采集
    collect_result = ssh_collect_device(device)
    state["collection_results"][ip] = collect_result

    return {
        "success": True,
        "ip": ip,
        "protocol": protocol,
        "message": f"已删除 {protocol} 配置",
    }


@app.post("/api/ping")
async def ping_device(req: PingRequest):
    """测试设备连通性。"""
    online = ping_host(req.ip)
    return {
        "success": True,
        "ip": req.ip,
        "online": online,
        "message": "在线" if online else "离线",
    }


@app.post("/api/collect/{ip}")
async def collect_device(ip: str, fast_mode: bool = True, force: bool = True):
    """
    连接指定设备并采集信息。
    fast_mode: 使用快速采集（并行 SSH）
    force: True 强制采集，False 仅在状态变化时采集（增量模式）
    """
    devices = load_devices()
    device = next((d for d in devices if d["ip"] == ip), None)
    if not device:
        raise HTTPException(status_code=404, detail=f"未找到 IP 为 {ip} 的设备")

    # 增量模式：如果上次在线且有数据，直接返回缓存
    if not force:
        dev_status = state["device_status"].get(ip, {})
        if dev_status.get("last_online") and ip in state["collection_results"]:
            return {"success": True, "result": state["collection_results"][ip], "cached": True}

    # ping 检测（2包，~1.5s）
    online = ping_host(ip)
    state["device_status"][ip] = {
        "last_online": online,
        "last_collect_ts": time.time(),
    }

    if not online:
        result = {
            "ip": ip,
            "name": device.get("name", ip),
            "online": False,
            "ssh_ok": False,
            "ssh_error": "设备不可达（Ping 超时）",
            "commands": {},
            "info": {},
        }
        state["collection_results"][ip] = result
        return {"success": True, "result": result}

    result = ssh_collect_device_parallel(device, fast_mode=fast_mode)
    state["collection_results"][ip] = result
    return {"success": True, "result": result}


@app.post("/api/collect-all")
async def collect_all(fast_mode: bool = True, force: bool = True):
    """
    批量采集所有设备信息。

    优化策略：
    - 并行 ping（2 包）+ 并行 SSH 采集
    - 每台设备内部 3 个 SSH 会话并行执行不同命令组
    - 增量模式（force=False）：仅对上次离线→在线的设备重新采集

    Args:
        fast_mode: True（默认）使用快速模式
        force: True（默认）强制全部采集；False 增量模式
    """
    devices = load_devices()
    if not devices:
        return {"success": True, "results": [], "message": "设备列表为空"}

    results: List[dict] = []

    def _collect_single(device: dict) -> dict:
        ip = device["ip"]

        # 增量模式：上次在线且有数据，跳过 SSH 采集
        if not force:
            dev_status = state["device_status"].get(ip, {})
            if dev_status.get("last_online") and ip in state["collection_results"]:
                cached = state["collection_results"][ip]
                cached["cached"] = True
                return cached

        # 并行 ping（2包，~1.5s）
        online = ping_host(ip)
        state["device_status"][ip] = {
            "last_online": online,
            "last_collect_ts": time.time(),
        }

        if not online:
            return {
                "ip": ip,
                "name": device.get("name", ip),
                "online": False,
                "ssh_ok": False,
                "ssh_error": "设备不可达",
                "commands": {},
                "info": {},
            }

        # 并行 SSH 采集（3 个会话同时执行）
        return ssh_collect_device_parallel(device, fast_mode=fast_mode)

    # 并行采集所有设备（每台设备一个线程，内含 3 个 SSH 会话）
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(devices), 10)) as executor:
        future_map = {executor.submit(_collect_single, d): d for d in devices}
        for future in concurrent.futures.as_completed(future_map):
            try:
                result = future.result()
                results.append(result)
                state["collection_results"][result["ip"]] = result
            except Exception as e:
                device = future_map[future]
                logger.error(f"采集 {device['ip']} 时发生未预期异常: {e}")
                err_result = {
                    "ip": device["ip"],
                    "name": device.get("name", device["ip"]),
                    "online": False,
                    "ssh_ok": False,
                    "ssh_error": f"采集异常: {e}",
                    "commands": {},
                    "info": {},
                }
                results.append(err_result)
                state["collection_results"][device["ip"]] = err_result

    lldp_added = _sync_lldp_devices()
    if lldp_added:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(lldp_added), 5)) as executor:
            future_map = {executor.submit(_collect_single, d): d for d in lldp_added}
            for future in concurrent.futures.as_completed(future_map):
                device = future_map[future]
                try:
                    result = future.result()
                    results.append(result)
                    state["collection_results"][result["ip"]] = result
                except Exception as e:
                    logger.error(f"采集 LLDP 发现设备 {device['ip']} 时发生异常: {e}")
                    err_result = {
                        "ip": device["ip"],
                        "name": device.get("name", device["ip"]),
                        "online": False,
                        "ssh_ok": False,
                        "ssh_error": f"采集异常: {e}",
                        "commands": {},
                        "info": {"hostname": device.get("name", device["ip"])},
                    }
                    results.append(err_result)
                    state["collection_results"][device["ip"]] = err_result

    # 按 IP 排序返回
    results.sort(key=lambda r: int(r["ip"].rsplit(".", 1)[1]))
    logger.info(f"批量采集完成: {len(results)} 台设备")
    return {
        "success": True,
        "results": results,
        "devices": load_devices(),
        "lldp_added": lldp_added,
    }


@app.get("/api/topology")
async def get_topology():
    """根据 LLDP 信息返回拓扑关系。"""
    devices = load_devices()
    topology = generate_topology(devices, state["collection_results"])
    return {"success": True, "topology": topology}


@app.get("/api/collection-results")
async def get_collection_results():
    """获取所有采集结果。"""
    return {"success": True, "results": state["collection_results"]}


@app.get("/api/capability-results")
async def get_capability_results():
    """获取所有能力识别结果（缓存）。"""
    results = state.get("capability_results", {})
    return {"success": True, "results": results}


# ---------------------------------------------------------------------------
# 设备能力识别 API
# ---------------------------------------------------------------------------

@app.get("/api/devices/{ip}/capabilities")
async def get_device_capabilities(ip: str):
    """识别指定设备支持的协议和功能。"""
    devices = load_devices()
    device = next((d for d in devices if d["ip"] == ip), None)
    if not device:
        raise HTTPException(status_code=404, detail=f"未找到 IP 为 {ip} 的设备")

    source_ip = _management_source_ip_for(ip)
    result = probe_capabilities(
        device,
        source_ip=source_ip,
        management_socket_factory=_create_management_socket,
    )
    # Cache in state
    state["capability_results"] = state.get("capability_results", {})
    state["capability_results"][ip] = result
    return {"success": True, **result}


@app.post("/api/devices/capabilities-all")
async def probe_all_capabilities():
    """批量识别所有设备能力（并行：3个SSH会话/台，设备间也并行）。"""
    devices = load_devices()
    if not devices:
        return {"success": True, "results": [], "message": "设备列表为空"}

    results = []

    def _probe_one(device: dict) -> dict:
        src = _management_source_ip_for(device["ip"])
        # 传入采集缓存，避免重复 SSH
        coll_result = state["collection_results"].get(device["ip"])
        return probe_capabilities_parallel(
            device,
            source_ip=src,
            management_socket_factory=_create_management_socket,
            collection_result=coll_result,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(devices), 10)) as executor:
        future_map = {executor.submit(_probe_one, d): d for d in devices}
        for future in concurrent.futures.as_completed(future_map):
            device = future_map[future]
            try:
                result = future.result()
                results.append(result)
                state.setdefault("capability_results", {})[device["ip"]] = result
            except Exception as e:
                logger.error("能力识别 %s 失败: %s", device["ip"], e)
                results.append({
                    "ip": device["ip"],
                    "hostname": device.get("name", device["ip"]),
                    "device_role": "unknown",
                    "ssh_status": False,
                    "capabilities": {},
                    "raw_outputs": {},
                    "errors": [str(e)],
                })

    results.sort(key=lambda r: _ip_sort_key(r["ip"]))
    return {"success": True, "results": results}


@app.get("/api/devices/capability-templates")
async def get_capability_templates(device_role: Optional[str] = None):
    """获取可用的配置模板。"""
    if device_role:
        templates = get_templates_for_role(device_role)
    else:
        templates = get_all_templates()
    return {"success": True, "templates": templates}


@app.get("/api/devices/{ip}/protocol-roles")
async def get_device_protocol_roles(ip: str):
    """
    解析设备在每个协议中扮演的角色（VRRP Master/Backup、STP 根桥、
    DHCP Server/中继、NAT 类型……）。

    纯解析已有能力识别结果，零 SSH。未识别能力的设备返回 404 提示。
    """
    cap = state.get("capability_results", {}).get(ip)
    if not cap:
        raise HTTPException(status_code=404,
                            detail="暂无该设备的能力识别结果，请先执行能力识别")
    return {"success": True, **detect_protocol_roles(cap)}


@app.post("/api/protocol-roles-all")
async def analyze_protocol_roles_all():
    """批量解析所有已识别设备的协议角色（纯内存解析，零 SSH）。"""
    caps = state.get("capability_results", {})
    results = [detect_protocol_roles(cap) for cap in caps.values()]
    results.sort(key=lambda r: _ip_sort_key(r["ip"]))
    return {"success": True, "results": results, "count": len(results)}


@app.get("/api/devices/{ip}/protocol-tree")
async def discover_device_protocol_tree(
    ip: str,
    mode: Optional[str] = None,
    port: Optional[int] = None,
    interface: Optional[str] = None,
):
    """
    零副作用 ? 命令树协议发现。

    采集 display ? / 系统视图 ? / 接口视图 ? 三层关键字，与协议签名比对，
    再结合 eNSP 镜像能力矩阵，给出设备支持的完整协议清单与证据来源。
    mode=console 时走本地 Console 端口（零 IP 设备），默认 SSH。
    """
    if mode == "console":
        if not port:
            raise HTTPException(status_code=400, detail="console 模式需要提供 port 参数")
        from services.help_tree import collect_views_console, compose_discovery
        from services.command_probe import probe_via_console
        try:
            version, views = probe_via_console(
                {"mode": "console", "port": port, "interface": interface})
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Console 连接失败: {e}")
        result = compose_discovery(version, views)
        result.update({"ip": ip, "mode": "console", "console_port": port,
                       "channel_ok": True})
        return {"success": True, **result}

    devices = load_devices()
    device = next((d for d in devices if d["ip"] == ip), None)
    if not device:
        raise HTTPException(status_code=404, detail=f"未找到 IP 为 {ip} 的设备")
    if interface:
        device = dict(device)
        device["probe_interface"] = interface

    source_ip = _management_source_ip_for(ip)
    result = discover_via_ssh(
        device,
        source_ip=source_ip,
        management_socket_factory=_create_management_socket,
    )
    result["mode"] = "ssh"
    return {"success": result.get("ssh_status", False), **result}


# ---------------------------------------------------------------------------
# AI 助教 API（OpenAI 兼容协议，模型服务自行配置）
# ---------------------------------------------------------------------------

class AiConfigRequest(BaseModel):
    api_base: Optional[str] = None
    api_key: Optional[str] = None   # 传空表示保留原 Key
    model: Optional[str] = None
    mode: Optional[str] = None      # tutor / engineer
    temperature: Optional[float] = None


class AiChatRequest(BaseModel):
    message: str
    task: str = "free"              # free/explain_capabilities/explain_roles/explain_config/diagnose/lab_hint
    device_ip: Optional[str] = None
    history: List[dict] = []
    stream: bool = True


class AiDiagnoseRequest(BaseModel):
    ip: str
    error_text: str
    command_outputs: List[str] = []


class AiLabStartRequest(BaseModel):
    experiment_id: str
    device_ips: List[str] = []      # 空则取全部设备


class AiLabHintRequest(BaseModel):
    question: str = ""
    stream: bool = True


_lab_sessions: Dict[str, dict] = {}


def _device_context_for(ip: Optional[str], config_full: bool = False) -> str:
    """把指定设备的实测数据渲染成提示词上下文。"""
    if not ip:
        return ""
    device = next((d for d in load_devices() if d.get("ip") == ip), None)
    cap = state.get("capability_results", {}).get(ip)
    return ai_assistant.render_device_context(
        device or {"name": ip, "ip": ip}, cap, config_full=config_full)


def _sse(generator):
    """把 AI 流包装成 SSE 事件流。"""
    try:
        for delta in generator:
            yield f"data: {json.dumps({'delta': delta}, ensure_ascii=False)}\n\n"
        yield "data: {\"done\": true}\n\n"
    except AIError as e:
        yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
    except Exception as e:
        logger.exception("AI 流式输出异常")
        yield f"data: {json.dumps({'error': f'AI 输出中断: {e}'}, ensure_ascii=False)}\n\n"


@app.get("/api/ai/status")
async def ai_status():
    """AI 配置状态（Key 打码返回，不回传明文）。"""
    cfg = ai_assistant.load_config()
    return {
        "success": True,
        "configured": ai_assistant.is_configured(cfg),
        "api_base": cfg.get("api_base", ""),
        "model": cfg.get("model", ""),
        "mode": cfg.get("mode", "tutor"),
        "key_masked": ai_assistant.mask_key(cfg.get("api_key", "")),
    }


@app.post("/api/ai/config")
async def ai_update_config(req: AiConfigRequest):
    """保存 AI 配置。api_key 留空表示保留原值。"""
    try:
        cfg = ai_assistant.save_config(req.model_dump())
    except AIError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "success": True,
        "configured": ai_assistant.is_configured(cfg),
        "api_base": cfg.get("api_base", ""),
        "model": cfg.get("model", ""),
        "mode": cfg.get("mode", "tutor"),
        "key_masked": ai_assistant.mask_key(cfg.get("api_key", "")),
    }


@app.post("/api/ai/test")
async def ai_test_connection():
    """测试模型服务连通性（一次最小请求）。"""
    result = ai_assistant.test_connection()
    return {"success": result["success"], "message": result.get("message", result.get("error", ""))}


@app.post("/api/ai/chat")
async def ai_chat(req: AiChatRequest):
    """
    AI 对话。携带 device_ip 时自动注入该设备实测上下文；
    task 决定助教的角色侧重。stream=true 时返回 SSE 事件流。
    """
    device_context = _device_context_for(
        req.device_ip, config_full=(req.task == "explain_config"))
    if req.task == "free":
        catalog = ai_assistant.render_template_catalog()
        if catalog:
            device_context = (device_context + "\n\n" + catalog).strip()
    messages = ai_assistant.build_messages(
        req.message, req.history, device_context, req.task)

    if not req.stream:
        try:
            reply = ai_assistant.chat_once(messages)
        except AIError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"success": True, "reply": reply}

    return StreamingResponse(
        _sse(ai_assistant.chat_stream(messages)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/ai/diagnose")
async def ai_diagnose(req: AiDiagnoseRequest):
    """下发失败/连接问题的报错诊断（非流式，供结果区直调用）。"""
    device_context = _device_context_for(req.ip)
    error_text = req.error_text[:2500]
    if req.command_outputs:
        error_text += "\n执行回显:\n" + "\n".join(o[:500] for o in req.command_outputs[:8])
    messages = ai_assistant.build_messages(
        f"请诊断这次配置下发失败的原因并给排查步骤。报错信息：\n{error_text}",
        [], device_context, task="diagnose")
    try:
        reply = ai_assistant.chat_once(messages)
    except AIError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "reply": reply}


@app.get("/api/ai/lab/experiments")
async def ai_lab_experiments():
    """可选择的实验任务目录。"""
    return {
        "success": True,
        "experiments": [
            {"id": eid, "name": exp["name"], "goal": exp["goal"],
             "applicable_roles": exp["applicable_roles"],
             "check_count": len(exp["checks"])}
            for eid, exp in ai_assistant.LAB_EXPERIMENTS.items()
        ],
    }


@app.post("/api/ai/lab/start")
async def ai_lab_start(req: AiLabStartRequest):
    """创建实验会话（进度校验基于能力识别缓存，零 SSH）。"""
    if req.experiment_id not in ai_assistant.LAB_EXPERIMENTS:
        raise HTTPException(status_code=404, detail=f"未知实验: {req.experiment_id}")
    device_ips = req.device_ips or [d["ip"] for d in load_devices()]
    if not device_ips:
        raise HTTPException(status_code=400, detail="设备列表为空，请先添加设备")
    session_id = uuid.uuid4().hex
    _lab_sessions[session_id] = {
        "id": session_id,
        "experiment_id": req.experiment_id,
        "device_ips": device_ips,
        "created_at": time.time(),
    }
    progress = ai_assistant.evaluate_lab_progress(
        req.experiment_id, device_ips, state.get("capability_results", {}))
    return {"success": True, "session": _lab_sessions[session_id], "progress": progress}


@app.get("/api/ai/lab/{session_id}/progress")
async def ai_lab_progress(session_id: str):
    """重新校验实验进度。"""
    session = _lab_sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="实验会话不存在或已失效（重启服务后需重新开始）")
    progress = ai_assistant.evaluate_lab_progress(
        session["experiment_id"], session["device_ips"],
        state.get("capability_results", {}))
    return {"success": True, "progress": progress}


@app.post("/api/ai/lab/{session_id}/hint")
async def ai_lab_hint(session_id: str, req: AiLabHintRequest):
    """向 AI 助教要下一步实验提示（强制引导式教学）。"""
    session = _lab_sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="实验会话不存在或已失效（重启服务后需重新开始）")
    try:
        progress = ai_assistant.evaluate_lab_progress(
            session["experiment_id"], session["device_ips"],
            state.get("capability_results", {}))
    except AIError as e:
        raise HTTPException(status_code=404, detail=str(e))
    exp = ai_assistant.LAB_EXPERIMENTS[session["experiment_id"]]
    extra = (ai_assistant.render_lab_context(progress)
             + "\n【实验参考要点】" + "; ".join(exp.get("hints", [])))
    message = req.question.strip() or "请根据当前进度，指出我下一步该做什么、可能的卡点在哪里。"
    messages = ai_assistant.build_messages(message, [], "", task="lab_hint",
                                           extra_context=extra)
    # 实验指导强制引导模式，避免直接给整段答案
    saved_mode = ai_assistant.load_config().get("mode")
    if not req.stream:
        try:
            reply = ai_assistant.chat_once(messages)
        except AIError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"success": True, "reply": reply, "progress": progress}
    return StreamingResponse(
        _sse(ai_assistant.chat_stream(messages)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "X-Lab-Mode": saved_mode or "tutor"},
    )


# ---------------------------------------------------------------------------
# 设备备份 API
# ---------------------------------------------------------------------------

@app.post("/api/devices/{ip}/backup")
async def create_device_backup(ip: str):
    """备份指定设备当前配置。"""
    devices = load_devices()
    device = next((d for d in devices if d["ip"] == ip), None)
    if not device:
        raise HTTPException(status_code=404, detail=f"未找到 IP 为 {ip} 的设备")

    source_ip = _management_source_ip_for(ip)
    result = backup_device_config(
        device,
        source_ip=source_ip,
        management_socket_factory=_create_management_socket,
    )
    if not result["success"]:
        raise HTTPException(status_code=500, detail=f"备份失败: {result.get('error', '未知错误')}")
    return {"success": True, **result}


@app.get("/api/devices/{ip}/backups")
async def get_device_backups(ip: str):
    """查看指定设备历史备份列表。"""
    backups = list_backups(ip)
    return {"success": True, "ip": ip, "backups": backups}


# ---------------------------------------------------------------------------
# 配置下发 API
# ---------------------------------------------------------------------------

def _cached_full_config(ip: str) -> str:
    """读取缓存中的完整 current-configuration；过滤版输出不作为复制依据。"""
    result = state.get("collection_results", {}).get(ip, {})
    command = result.get("commands", {}).get("display current-configuration", {})
    if command.get("success") and command.get("output"):
        return command["output"]
    return ""


def _load_config_for_clone(device: dict, refresh: bool = True) -> dict:
    """
    获取配置复制所需的完整配置。

    refresh=True 时实时读取并落一份备份；失败后回退到完整采集缓存或最新备份。
    """
    ip = device["ip"]
    errors = []

    if refresh:
        source_ip = _management_source_ip_for(ip)
        backup = backup_device_config(
            device,
            source_ip=source_ip,
            management_socket_factory=_create_management_socket,
        )
        if backup.get("success") and backup.get("filepath"):
            try:
                return {
                    "success": True,
                    "config": Path(backup["filepath"]).read_text(encoding="utf-8"),
                    "backup_file": backup.get("filename", ""),
                    "source": "live",
                    "errors": [],
                }
            except OSError as e:
                errors.append(f"读取实时备份失败: {e}")
        else:
            errors.append(f"实时获取失败: {backup.get('error', '未知错误')}")

    cached = _cached_full_config(ip)
    if cached:
        return {
            "success": True,
            "config": cached,
            "backup_file": "",
            "source": "cache",
            "errors": errors,
        }

    backups = list_backups(ip)
    if backups:
        content = get_backup_content(backups[0]["filename"])
        if content:
            return {
                "success": True,
                "config": content,
                "backup_file": backups[0]["filename"],
                "source": "backup",
                "errors": errors,
            }

    return {
        "success": False,
        "config": "",
        "backup_file": "",
        "source": "",
        "errors": errors or ["没有可用的完整配置"],
    }


def _clone_role_warnings(source_ip: str, target_ips: List[str]) -> List[str]:
    warnings = []
    capabilities = state.get("capability_results", {})
    source_role = capabilities.get(source_ip, {}).get("device_role")
    for target_ip in target_ips:
        target_role = capabilities.get(target_ip, {}).get("device_role")
        if source_role and target_role and source_role != target_role:
            warnings.append(
                f"源设备角色为 {source_role}，目标设备 {target_ip} 角色为 {target_role}；"
                "不兼容接口或功能会被跳过，但仍需检查业务语义"
            )
    return warnings


def _reserved_ips_from_collection() -> set:
    """从所有已采集设备中提取地址，供配置复制自动分配时避让。"""
    reserved = set()
    for result in state.get("collection_results", {}).values():
        for line in result.get("info", {}).get("interfaces", []):
            parts = line.split() if isinstance(line, str) else []
            if len(parts) >= 2:
                try:
                    ipaddress.IPv4Address(parts[1])
                    reserved.add(parts[1])
                except ValueError:
                    pass
    return reserved


@app.post("/api/devices/{ip}/config/preview")
async def preview_config(ip: str, req: ConfigPreviewRequest):
    """生成配置预览（不连接设备）。"""
    preview = build_preview(req.config_type, req.params)
    if not preview["commands"]:
        raise HTTPException(status_code=400, detail=preview.get("error", "参数不完整"))
    return {"success": True, **preview}


@app.post("/api/devices/{ip}/config/deploy")
async def deploy_device_config(ip: str, req: ConfigDeployRequest):
    """下发配置命令到设备。"""
    devices = load_devices()
    device = next((d for d in devices if d["ip"] == ip), None)
    if not device:
        raise HTTPException(status_code=404, detail=f"未找到 IP 为 {ip} 的设备")

    commands = build_config_commands(req.config_type, req.params)
    if not commands:
        raise HTTPException(status_code=400, detail="参数不完整，无法生成配置命令")

    verify_cmds = get_verify_commands(req.config_type, req.params)

    source_ip = _management_source_ip_for(ip)
    result = deploy_config(
        device=device,
        commands=commands,
        verify_commands=verify_cmds,
        save_config=req.save_config,
        source_ip=source_ip,
        management_socket_factory=_create_management_socket,
    )

    if not result["success"] and result.get("error"):
        raise HTTPException(status_code=500, detail=f"配置下发失败: {result['error']}")

    return {"success": True, **result}


@app.get("/api/config/clone/domains")
async def get_clone_domains():
    """返回配置复制可选择的功能域。"""
    return {
        "success": True,
        "domains": [
            {"id": domain, "name": CLONE_DOMAIN_NAMES[domain], "default": domain in CLONE_DEFAULT_DOMAINS}
            for domain in CLONE_DOMAIN_NAMES
        ],
    }


@app.post("/api/config/clone/preview")
async def preview_clone_config(req: ClonePreviewRequest):
    """读取源/目标设备配置并生成自动解决冲突后的复制方案。"""
    devices = load_devices()
    device_map = {device["ip"]: device for device in devices}
    source = device_map.get(req.source_device)
    if not source:
        raise HTTPException(status_code=404, detail=f"未找到源设备 {req.source_device}")

    target_ips = list(dict.fromkeys(req.target_devices))
    if not target_ips:
        raise HTTPException(status_code=400, detail="请至少选择一台目标设备")
    if req.source_device in target_ips:
        raise HTTPException(status_code=400, detail="源设备不能同时作为目标设备")
    missing = [ip for ip in target_ips if ip not in device_map]
    if missing:
        raise HTTPException(status_code=404, detail=f"未找到目标设备: {', '.join(missing)}")

    selected_domains = [domain for domain in req.domains if domain in CLONE_DOMAIN_NAMES]
    if not selected_domains:
        raise HTTPException(status_code=400, detail="请至少选择一个复制功能域")

    fetch_ips = [req.source_device] + target_ips
    config_results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(fetch_ips), 5)) as executor:
        future_map = {
            executor.submit(_load_config_for_clone, device_map[ip], req.refresh_config): ip
            for ip in fetch_ips
        }
        for future in concurrent.futures.as_completed(future_map):
            ip = future_map[future]
            try:
                config_results[ip] = future.result()
            except Exception as e:
                config_results[ip] = {
                    "success": False,
                    "config": "",
                    "backup_file": "",
                    "source": "",
                    "errors": [str(e)],
                }

    unavailable = [
        f"{ip}: {'; '.join(info.get('errors', []))}"
        for ip, info in config_results.items()
        if not info.get("success")
    ]
    if unavailable:
        raise HTTPException(status_code=502, detail="无法获取完整设备配置: " + " | ".join(unavailable))

    clone_result = build_clone_preview(
        source_device=source,
        source_config=config_results[req.source_device]["config"],
        target_devices=[device_map[ip] for ip in target_ips],
        target_configs={ip: config_results[ip]["config"] for ip in target_ips},
        domains=selected_domains,
        auto_resolve=req.auto_resolve,
        include_sensitive=req.include_sensitive,
        reserved_ips=_reserved_ips_from_collection(),
    )

    rendered_commands = {
        ip: target["commands"]
        for ip, target in clone_result["targets"].items()
        if target.get("commands")
    }
    conflicts = check_cross_device_conflicts(
        targets=list(rendered_commands),
        rendered_commands=rendered_commands,
        collection_results=state.get("collection_results", {}),
    )
    clone_result["conflicts"] = conflicts
    clone_result["warnings"].extend(_clone_role_warnings(req.source_device, target_ips))
    if conflicts.get("errors"):
        clone_result["success"] = False

    preview_id = uuid.uuid4().hex
    state["clone_previews"][preview_id] = {
        "created_at": time.time(),
        "source_device": req.source_device,
        "target_devices": target_ips,
        "domains": selected_domains,
        "targets": clone_result["targets"],
        "conflicts": conflicts,
    }
    clone_result["preview_id"] = preview_id
    clone_result["config_sources"] = {
        ip: {
            "source": info.get("source", ""),
            "backup_file": info.get("backup_file", ""),
            "warnings": info.get("errors", []),
        }
        for ip, info in config_results.items()
    }
    return clone_result


@app.post("/api/config/clone/deploy")
async def deploy_clone_config(req: CloneDeployRequest):
    """下发已预览的设备配置复制方案。"""
    if not req.confirm:
        raise HTTPException(status_code=400, detail="请确认配置复制操作")

    preview = state.get("clone_previews", {}).get(req.preview_id)
    if not preview:
        raise HTTPException(status_code=404, detail="复制预览不存在或服务已重启，请重新生成预览")
    if time.time() - preview.get("created_at", 0) > 900:
        state["clone_previews"].pop(req.preview_id, None)
        raise HTTPException(status_code=410, detail="复制预览已超过 15 分钟，请重新读取设备配置并生成方案")
    if preview.get("conflicts", {}).get("errors"):
        raise HTTPException(status_code=409, detail="复制方案仍存在未解决冲突，请重新生成预览")

    active_targets = [
        ip for ip in preview["target_devices"]
        if preview["targets"].get(ip, {}).get("commands")
    ]
    if not active_targets:
        raise HTTPException(status_code=400, detail="没有需要下发的配置命令")

    task = {
        "task_name": f"复制 {preview['source_device']} 配置",
        "target_devices": active_targets,
        "verify_commands": [
            "display ip interface brief",
            "display current-configuration | include sysname|router-id|network-entity|ospf|bgp|isis|vlan",
        ],
        "save_after_success": req.save_config,
        "stop_on_error": True,
    }
    rendered_preview = {
        ip: {
            "device_name": preview["targets"][ip].get("device_name", ip),
            "commands": preview["targets"][ip]["commands"],
        }
        for ip in active_targets
    }
    exec_result = execute_free_deploy(
        task=task,
        preview=rendered_preview,
        devices=load_devices(),
        source_ip_func=_management_source_ip_for,
        socket_factory=_create_management_socket,
    )

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    task_id = generate_task_id()
    save_task_record({
        "task_id": task_id,
        "task_name": task["task_name"],
        "target_devices": active_targets,
        "command_template": "由设备配置复制生成",
        "global_vars": {},
        "device_vars": {},
        "verify_commands": task["verify_commands"],
        "rendered_commands": {ip: info["commands"] for ip, info in rendered_preview.items()},
        "results": exec_result["results"],
        "summary": exec_result["summary"],
        "status": "success" if exec_result["success"] else "partial" if exec_result["summary"]["success"] else "failed",
        "errors": [err for result in exec_result["results"].values() for err in result.get("errors", [])],
        "start_time": now,
        "end_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_device": preview["source_device"],
        "clone_preview_id": req.preview_id,
    })

    return {
        "success": exec_result["success"],
        "task_id": task_id,
        "source_device": preview["source_device"],
        "results": exec_result["results"],
        "summary": exec_result["summary"],
    }


@app.post("/api/devices/{ip}/config/raw-preview")
async def preview_raw_config(ip: str, req: RawPreviewRequest):
    """高级模式：预览原始命令（不执行）。"""
    if not req.commands:
        raise HTTPException(status_code=400, detail="请输入命令")
    return {
        "success": True,
        "commands": req.commands,
        "preview_commands": req.commands,
        "warning": "高级命令模式：请确认命令正确性，下发前系统会自动备份当前配置",
    }


@app.post("/api/devices/{ip}/config/raw-deploy")
async def deploy_raw_config(ip: str, req: RawDeployRequest):
    """高级模式：下发原始命令。"""
    if not req.commands:
        raise HTTPException(status_code=400, detail="请输入命令")
    if not req.confirm:
        raise HTTPException(status_code=400, detail="请确认二次确认标记")

    devices = load_devices()
    device = next((d for d in devices if d["ip"] == ip), None)
    if not device:
        raise HTTPException(status_code=404, detail=f"未找到 IP 为 {ip} 的设备")

    source_ip = _management_source_ip_for(ip)
    result = deploy_raw_commands(
        device=device,
        raw_commands=req.commands,
        save_config=req.save_config,
        source_ip=source_ip,
        management_socket_factory=_create_management_socket,
    )

    if not result["success"] and result.get("error"):
        raise HTTPException(status_code=500, detail=f"原始命令下发失败: {result['error']}")

    return {"success": True, **result}


# ---------------------------------------------------------------------------
# Console 端口直连配置 API（无需设备 IP，零配置设备初始化）
# ---------------------------------------------------------------------------

class ConsoleConfigRequest(BaseModel):
    """通过 Console 端口下发模板配置（零 IP）。"""
    console_port: int = Field(..., description="eNSP 本地 Console 端口号")
    config_type: str = Field(..., description="配置模板类型标识")
    params: dict = Field(default={}, description="表单参数")
    save_config: bool = False


class ConsoleRawRequest(BaseModel):
    """通过 Console 端口下发原始命令（零 IP，高级模式）。"""
    console_port: int = Field(..., description="eNSP 本地 Console 端口号")
    commands: List[str] = Field(default=[], description="原始命令列表（每行一条）")
    save_config: bool = False


@app.post("/api/console-config/preview")
async def preview_console_config(req: ConsoleRawRequest):
    """预览 Console 直连配置命令（不连接设备）。"""
    return {"success": True, "commands": req.commands}


@app.post("/api/console-config/deploy")
async def deploy_console_config(req: ConsoleConfigRequest):
    """通过 Console 端口下发模板配置（无需设备 IP）。"""
    commands = build_config_commands(req.config_type, req.params)
    if not commands:
        raise HTTPException(status_code=400, detail="参数不完整，无法生成配置命令")
    verify_cmds = get_verify_commands(req.config_type, req.params)
    result = console_deploy(
        console_port=req.console_port,
        commands=commands,
        verify_commands=verify_cmds,
        save_config=req.save_config,
    )
    if not result["success"] and result.get("error"):
        raise HTTPException(status_code=500, detail=f"Console 配置下发失败: {result['error']}")
    return {"success": True, **result}


@app.post("/api/console-config/raw-deploy")
async def deploy_console_raw(req: ConsoleRawRequest):
    """通过 Console 端口下发原始命令（高级模式，无需设备 IP）。"""
    if not req.commands:
        raise HTTPException(status_code=400, detail="没有命令可下发")
    result = console_deploy(
        console_port=req.console_port,
        commands=req.commands,
        verify_commands=[],
        save_config=req.save_config,
    )
    result["raw_mode"] = True
    if not result["success"] and result.get("error"):
        raise HTTPException(status_code=500, detail=f"Console 配置下发失败: {result['error']}")
    return {"success": True, **result}


# ---------------------------------------------------------------------------
# 命令目录 API（声明式表单化配置单一数据源）
# ---------------------------------------------------------------------------

class CatalogRenderRequest(BaseModel):
    key: str = Field(..., description="命令目录条目 key")
    params: dict = Field(default={}, description="表单参数")


class CatalogDeployRequest(BaseModel):
    key: str = Field(..., description="命令目录条目 key")
    params: dict = Field(default={}, description="表单参数")
    target_ip: Optional[str] = Field(None, description="目标设备 IP（SSH 通道）")
    console_port: Optional[int] = Field(None, description="Console 端口（零 IP 通道）")
    save_config: bool = False


@app.get("/api/command-catalog")
async def list_command_catalog(role: Optional[str] = None, device_type: Optional[str] = None):
    """列出命令目录（可选按设备角色 / eNSP 设备类型过滤）。"""
    return {
        "success": True,
        "categories": get_categories(),
        "entries": get_catalog_entries(role, device_type),
    }


@app.post("/api/command-catalog/render")
async def render_command_catalog(req: CatalogRenderRequest):
    """渲染目录条目为命令列表（不连接设备）。"""
    commands, warnings, errors = render_catalog_commands(req.key, req.params)
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    return {
        "success": True,
        "key": req.key,
        "commands": commands,
        "warnings": warnings,
    }


@app.post("/api/command-catalog/deploy")
async def deploy_command_catalog(req: CatalogDeployRequest):
    """通过命令目录下发配置：渲染后走 SSH 或 Console 通道。"""
    commands, warnings, errors = render_catalog_commands(req.key, req.params)
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))

    verify_cmds = render_catalog_verify(req.key, req.params)

    # 零 IP 通道优先：给了 console_port 就走 Console
    if req.console_port is not None:
        result = console_deploy(
            console_port=req.console_port,
            commands=commands,
            verify_commands=verify_cmds,
            save_config=req.save_config,
        )
        result["channel"] = "console"
        if not result["success"] and result.get("error"):
            raise HTTPException(status_code=500, detail=f"命令目录下发失败: {result['error']}")
        return {"success": True, **result}

    # 否则走 SSH 通道
    if not req.target_ip:
        raise HTTPException(status_code=400, detail="请指定 target_ip 或 console_port")
    devices = load_devices()
    device = next((d for d in devices if d["ip"] == req.target_ip), None)
    if not device:
        raise HTTPException(status_code=404, detail=f"未找到 IP 为 {req.target_ip} 的设备")

    source_ip = _management_source_ip_for(req.target_ip)
    result = deploy_config(
        device=device,
        commands=commands,
        verify_commands=verify_cmds,
        save_config=req.save_config,
        source_ip=source_ip,
        management_socket_factory=_create_management_socket,
    )
    result["channel"] = "ssh"
    if not result["success"] and result.get("error"):
        raise HTTPException(status_code=500, detail=f"命令目录下发失败: {result['error']}")
    return {"success": True, **result}


# ---------------------------------------------------------------------------
# 命令目录 ? 探测引导 API（后台执行 + 缓存，做到无感知）
# ---------------------------------------------------------------------------
_catalog_probe_jobs: Dict[str, dict] = {}  # job_id -> {"status","result","error"}


class CatalogProbeRequest(BaseModel):
    target: dict = Field(..., description="探测目标：SSH={mode:'ssh',ip,...} / Console={mode:'console',port,...}")
    force: bool = Field(False, description="强制重新探测（忽略缓存）")


@app.post("/api/command-catalog/probe")
async def probe_catalog(req: CatalogProbeRequest, background_tasks: BackgroundTasks):
    """
    触发 ? 探测：识别设备支持的命令并校验目录。

    - 命中缓存（5min 内）→ 立即返回 done（无感知）。
    - 未命中 → 后台执行探测，立即返回 job_id；前端轮询 /api/command-catalog/probe/{job_id}。
    """
    key = cache_key(req.target)
    cached = get_cached_probe(key)
    if cached and not req.force:
        return {"success": True, "status": "done", "from_cache": True,
                "device_type": cached.get("device_type"), "result": cached}

    # SSH 目标若只给 IP，自动从设备清单补全凭据，避免前端传递密码
    target = dict(req.target)
    if target.get("mode") == "ssh" and target.get("ip") and not (
        target.get("username") and target.get("password")
    ):
        dev = next((d for d in load_devices() if d.get("ip") == target.get("ip")), None)
        if dev:
            target.setdefault("username", dev.get("username"))
            target.setdefault("password", dev.get("password"))
            target.setdefault("device_type", dev.get("device_type", "huawei"))

    job_id = uuid.uuid4().hex
    _catalog_probe_jobs[job_id] = {"status": "probing", "result": None, "error": None}

    def _run():
        try:
            res = probe_device_commands(req.target, force=True)
            _catalog_probe_jobs[job_id] = {"status": "done", "result": res, "error": None}
        except Exception as e:  # pragma: no cover
            logger.exception("命令目录 ? 探测失败")
            _catalog_probe_jobs[job_id] = {"status": "error", "result": None, "error": str(e)}

    background_tasks.add_task(_run)
    return {"success": True, "status": "probing", "job_id": job_id, "from_cache": False}


@app.get("/api/command-catalog/probe/{job_id}")
async def probe_catalog_result(job_id: str):
    """轮询 ? 探测结果。"""
    job = _catalog_probe_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="探测任务不存在")
    return {
        "success": True,
        "status": job["status"],
        "result": job.get("result"),
        "error": job.get("error"),
    }


@app.get("/api/health")
async def health_check():
    """健康检查接口。"""
    return {"status": "ok", "version": "3.0.0"}


# ---------------------------------------------------------------------------
# 自由配置工作台 API
# ---------------------------------------------------------------------------

class FreePreviewRequest(BaseModel):
    task_name: str = ""
    target_devices: List[str]
    command_template: Any  # str or list
    global_vars: Dict[str, Any] = {}
    device_vars: Dict[str, Dict[str, Any]] = {}
    verify_commands: List[str] = []


class FreeDeployRequest(FreePreviewRequest):
    save_after_success: bool = False
    stop_on_error: bool = True
    force: bool = False


class ScriptSaveRequest(BaseModel):
    script_name: str
    description: str = ""
    command_template: Any = []
    global_vars: Dict[str, Any] = {}
    sample_device_vars: Dict[str, Any] = {}
    verify_commands: List[str] = []


@app.post("/api/config/free-preview")
async def free_config_preview(req: FreePreviewRequest):
    """自由配置预览：变量替换 + 批量冲突检测，不连接设备。"""
    task = {
        "task_name": req.task_name,
        "target_devices": req.target_devices,
        "command_template": req.command_template,
        "global_vars": req.global_vars,
        "device_vars": req.device_vars,
        "verify_commands": req.verify_commands,
    }

    is_valid, errors = validate_task(task)
    if not is_valid:
        return {"success": False, "preview": {}, "errors": errors, "warnings": []}

    result = render_preview(task)

    # 添加批量冲突检测
    if result["success"] and req.target_devices and len(req.target_devices) > 1:
        rendered_commands = {
            ip: info["commands"]
            for ip, info in result["preview"].items()
        }
        conflicts = check_cross_device_conflicts(
            targets=req.target_devices,
            rendered_commands=rendered_commands,
            collection_results=collection_results,
        )
        result["conflicts"] = conflicts

        # 将冲突中的 warnings 合并到主 warnings
        if conflicts.get("warnings"):
            for w in conflicts["warnings"]:
                result["warnings"].append(w.get("message", str(w)))
    else:
        result["conflicts"] = {
            "success": True,
            "errors": [],
            "warnings": [],
            "suggestions": [],
        }

    return result


@app.post("/api/config/free-deploy")
async def free_config_deploy(req: FreeDeployRequest):
    """自由配置下发：备份 + 下发 + 验证 + 可选保存。"""
    from datetime import datetime

    task = {
        "task_name": req.task_name,
        "target_devices": req.target_devices,
        "command_template": req.command_template,
        "global_vars": req.global_vars,
        "device_vars": req.device_vars,
        "verify_commands": req.verify_commands,
        "save_after_success": req.save_after_success,
        "stop_on_error": req.stop_on_error,
    }

    # 验证任务
    is_valid, errors = validate_task(task)
    if not is_valid:
        return {"success": False, "errors": errors}

    # 先生成预览
    preview_result = render_preview(task)
    if not preview_result["success"]:
        return {"success": False, "errors": preview_result["errors"], "warnings": preview_result["warnings"]}

    # 检查危险命令（除非 force=true）
    if not req.force:
        from services.free_config_renderer import check_dangerous_commands, clean_commands
        raw = req.command_template if isinstance(req.command_template, list) else req.command_template.split("\n")
        cleaned, _, _ = clean_commands(raw)
        is_safe, dangerous = check_dangerous_commands(cleaned)
        if not is_safe:
            return {
                "success": False,
                "errors": [f"检测到危险命令: {d['command']} - {d['reason']}" for d in dangerous],
                "hint": "如需强制执行，请设置 force=true",
            }

    # 批量冲突检测（除非 force=true）
    if not req.force and len(req.target_devices) > 1:
        rendered_cmds = {
            ip: info["commands"]
            for ip, info in preview_result["preview"].items()
        }
        cross_conflicts = check_cross_device_conflicts(
            targets=req.target_devices,
            rendered_commands=rendered_cmds,
            collection_results=collection_results,
        )
        if not cross_conflicts["success"]:
            return {
                "success": False,
                "errors": [e["message"] for e in cross_conflicts["errors"]],
                "warnings": [w["message"] for w in cross_conflicts.get("warnings", [])],
                "suggestions": [s["message"] for s in cross_conflicts.get("suggestions", [])],
                "hint": "如需强制执行，请设置 force=true",
            }

    # 获取设备列表
    devices = load_devices()
    device_ips = {d["ip"] for d in devices}
    for ip in req.target_devices:
        if ip not in device_ips:
            return {"success": False, "errors": [f"设备 {ip} 不在设备列表中"]}

    # 执行下发
    start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    task_id = generate_task_id()

    exec_result = execute_free_deploy(
        task=task,
        preview=preview_result["preview"],
        devices=devices,
        source_ip_func=_management_source_ip_for,
        socket_factory=_create_management_socket,
    )
    end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 保存任务记录
    record = {
        "task_id": task_id,
        "task_name": req.task_name or "未命名任务",
        "target_devices": req.target_devices,
        "command_template": req.command_template,
        "global_vars": req.global_vars,
        "device_vars": req.device_vars,
        "verify_commands": req.verify_commands,
        "rendered_commands": {ip: info["commands"] for ip, info in preview_result["preview"].items()},
        "results": exec_result["results"],
        "summary": exec_result["summary"],
        "status": "success" if exec_result["success"] else "partial" if exec_result["summary"]["success"] > 0 else "failed",
        "errors": [err for r in exec_result["results"].values() for err in r.get("errors", [])],
        "start_time": start_time,
        "end_time": end_time,
    }
    save_task_record(record)

    return {
        "success": exec_result["success"],
        "task_id": task_id,
        "task_name": req.task_name,
        "results": exec_result["results"],
        "summary": exec_result["summary"],
        "warnings": preview_result.get("warnings", []),
    }


# ---------------------------------------------------------------------------
# 智能默认值 & 批量冲突检测
# ---------------------------------------------------------------------------

class SmartDefaultsRequest(BaseModel):
    target_devices: List[str] = []
    config_type: str = ""


class NetworkWildcardRequest(BaseModel):
    network: str = ""


class BatchConflictRequest(BaseModel):
    target_devices: List[str] = []
    command_template: Any = []
    global_vars: Dict[str, Any] = {}
    device_vars: Dict[str, Dict[str, Any]] = {}


@app.post("/api/config/smart-defaults")
async def get_smart_defaults(req: SmartDefaultsRequest):
    """
    为选定设备生成智能默认值（Router ID = 管理 IP 等）。
    """
    if not req.target_devices:
        return {"success": False, "defaults": {}, "errors": ["未选择设备"]}

    defaults = generate_batch_smart_defaults(
        target_devices=req.target_devices,
        config_type=req.config_type,
        collection_results=collection_results,
    )

    return {"success": True, "defaults": defaults}


@app.post("/api/utils/network-to-wildcard")
async def api_network_to_wildcard(req: NetworkWildcardRequest):
    """
    将网络地址转换为反掩码。支持 CIDR、纯 IP、带掩码格式。
    """
    if not req.network:
        return {"success": False, "wildcard": None, "error": "网络地址不能为空"}

    wildcard = infer_wildcard_from_input(req.network)
    if wildcard:
        return {"success": True, "wildcard": wildcard}
    else:
        return {"success": False, "wildcard": None, "error": f"无法解析网络地址: {req.network}"}


@app.post("/api/config/check-batch-conflicts")
async def api_check_batch_conflicts(req: BatchConflictRequest):
    """
    批量冲突检测：跨设备检测 Router ID 重复、IP 冲突等。
    """
    if not req.target_devices:
        return {"success": True, "conflicts": {"success": True, "errors": [], "warnings": [], "suggestions": []}}

    # 生成预览以获取渲染后的命令
    task = {
        "task_name": "冲突检测",
        "target_devices": req.target_devices,
        "command_template": req.command_template,
        "global_vars": req.global_vars,
        "device_vars": req.device_vars,
    }

    preview_result = render_preview(task)
    if not preview_result["success"]:
        return {
            "success": False,
            "conflicts": None,
            "errors": preview_result["errors"],
        }

    rendered_commands = {
        ip: info["commands"]
        for ip, info in preview_result["preview"].items()
    }

    # 执行跨设备冲突检测
    conflicts = check_cross_device_conflicts(
        targets=req.target_devices,
        rendered_commands=rendered_commands,
        collection_results=collection_results,
    )

    return {"success": True, "conflicts": conflicts}
async def api_list_scripts():
    """获取脚本列表。"""
    scripts = list_scripts()
    return {"success": True, "scripts": scripts}


@app.post("/api/scripts")
async def api_save_script(req: ScriptSaveRequest):
    """保存脚本。"""
    data = {
        "script_name": req.script_name,
        "description": req.description,
        "command_template": req.command_template,
        "global_vars": req.global_vars,
        "sample_device_vars": req.sample_device_vars,
        "verify_commands": req.verify_commands,
    }
    result = save_script(data)
    return result


@app.get("/api/scripts/{name}")
async def api_get_script(name: str):
    """读取脚本。"""
    script = get_script(name)
    if not script:
        return {"success": False, "message": f"脚本 '{name}' 不存在"}
    return {"success": True, "script": script}


@app.delete("/api/scripts/{name}")
async def api_delete_script(name: str):
    """删除脚本。"""
    return delete_script(name)


@app.get("/api/tasks")
async def api_list_tasks(limit: int = 50):
    """获取历史任务列表。"""
    tasks = list_tasks(limit=limit)
    return {"success": True, "tasks": tasks}


@app.get("/api/tasks/{task_id}")
async def api_get_task(task_id: str):
    """获取任务详情。"""
    task = get_task(task_id)
    if not task:
        return {"success": False, "message": f"任务 '{task_id}' 不存在"}
    return {"success": True, "task": task}


# ---------------------------------------------------------------------------
# 服务器启动自动初始化
# ---------------------------------------------------------------------------

# 初始化状态
init_state = {
    "initialized": False,
    "in_progress": False,
    "message": "",
    "selected_interface": None,
    "subnet": None,
}


def _auto_init():
    """服务器启动时自动执行：选择网卡 → 发现设备 → 并行采集 → 并行能力识别"""
    global init_state
    if init_state["initialized"] or init_state["in_progress"]:
        return

    init_state["in_progress"] = True
    init_state["message"] = "正在自动识别网卡..."

    try:
        # 1. 自动选择管理网卡
        interfaces = _get_all_interfaces()
        recommended = None
        for iface in interfaces:
            if iface.get("is_recommended"):
                recommended = iface
                break

        if not recommended:
            for iface in interfaces:
                if iface.get("ipv4") and iface.get("status") == "UP":
                    recommended = iface
                    break

        if not recommended:
            init_state["message"] = "未找到可用的管理网卡"
            init_state["initialized"] = True
            init_state["in_progress"] = False
            return

        state["selected_interface"] = {
            "name": recommended["name"],
            "ip": recommended["ipv4"],
            "netmask": recommended["netmask"],
        }
        try:
            network = ipaddress.IPv4Network(f"{recommended['ipv4']}/{recommended['netmask']}", strict=False)
            state["subnet"] = str(network)
        except ValueError:
            state["subnet"] = None

        init_state["selected_interface"] = state["selected_interface"]
        init_state["subnet"] = state["subnet"]
        init_state["message"] = f"已选择网卡: {recommended['name']} ({recommended['ipv4']})"

        # 2. 发现设备
        if state["subnet"]:
            init_state["message"] = f"正在扫描网段 {state['subnet']}..."
            local_ip = state["selected_interface"]["ip"]
            probes = probe_subnet(state["subnet"], exclude_ip=local_ip, max_workers=50)

            # SSH 识别设备
            discovered = []
            for probe in probes:
                device = {
                    "name": _default_discovered_name(probe["ip"]),
                    "ip": probe["ip"],
                    "username": DEFAULT_SSH_USERNAME,
                    "password": DEFAULT_SSH_PASSWORD,
                    "device_type": DEFAULT_DEVICE_TYPE,
                }
                identity = ssh_identify_device(device)
                if identity.get("ssh_ok"):
                    device["name"] = identity.get("name", device["name"])
                    discovered.append(device)

            if discovered:
                save_devices(discovered)
                logger.info("自动发现 %d 台设备", len(discovered))

                # 3. 并行批量采集（3个SSH会话/台，设备间也并行）
                init_state["message"] = f"正在并行采集 {len(discovered)} 台设备信息..."

                def _parallel_collect(device):
                    ip = device["ip"]
                    online = ping_host(ip)
                    if online:
                        return ssh_collect_device_parallel(device, fast_mode=True)
                    return {"ip": ip, "name": device.get("name", ip), "online": False,
                            "ssh_ok": False, "ssh_error": "设备不可达", "commands": {}, "info": {}}

                with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(discovered), 10)) as executor:
                    future_map = {executor.submit(_parallel_collect, d): d for d in discovered}
                    for future in concurrent.futures.as_completed(future_map):
                        try:
                            result = future.result()
                            state["collection_results"][result["ip"]] = result
                        except Exception as e:
                            device = future_map[future]
                            logger.error("采集 %s 失败: %s", device["ip"], e)

                # 4. LLDP 同步 + 并行采集新发现设备
                lldp_added = _sync_lldp_devices()
                if lldp_added:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(lldp_added), 5)) as executor:
                        future_map = {executor.submit(_parallel_collect, d): d for d in lldp_added}
                        for future in concurrent.futures.as_completed(future_map):
                            try:
                                result = future.result()
                                state["collection_results"][result["ip"]] = result
                            except Exception as e:
                                device = future_map[future]
                                logger.error("LLDP 采集 %s 失败: %s", device["ip"], e)

                # 5. 并行能力识别（3个SSH会话/台，设备间也并行）
                init_state["message"] = "正在并行识别设备能力..."
                all_devices = load_devices()

                def _parallel_probe(device):
                    ip = device["ip"]
                    src = _management_source_ip_for(ip)
                    # 传入采集缓存，避免重复 SSH
                    coll_result = state["collection_results"].get(ip)
                    try:
                        return probe_capabilities_parallel(
                            device,
                            source_ip=src,
                            management_socket_factory=_create_management_socket,
                            collection_result=coll_result,
                        )
                    except Exception as e:
                        logger.error("能力识别 %s 失败: %s", ip, e)
                        return {"ip": ip, "device_name": device.get("name", ip),
                                "ssh_status": False, "capabilities": {}, "errors": [str(e)]}

                state["capability_results"] = {}
                with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(all_devices), 10)) as executor:
                    future_map = {executor.submit(_parallel_probe, d): d for d in all_devices}
                    for future in concurrent.futures.as_completed(future_map):
                        try:
                            cap_result = future.result()
                            state["capability_results"][cap_result["ip"]] = cap_result
                        except Exception as e:
                            device = future_map[future]
                            logger.error("能力识别 %s 失败: %s", device["ip"], e)

                init_state["initialized"] = True
                init_state["message"] = f"初始化完成: {len(discovered)} 台设备"
                logger.info("自动初始化完成")
            else:
                init_state["initialized"] = True
                init_state["message"] = "未发现可管理的设备"
        else:
            init_state["initialized"] = True
            init_state["message"] = "未获取到网段信息"

    except Exception as e:
        init_state["initialized"] = True
        init_state["message"] = f"初始化失败: {e}"
        logger.error("自动初始化失败: %s", e)
    finally:
        init_state["in_progress"] = False


def _get_all_interfaces() -> List[dict]:
    """获取所有网卡信息"""
    interfaces = []
    try:
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        metadata = _get_windows_adapter_metadata()
    except Exception:
        return interfaces

    for name, addr_list in addrs.items():
        meta = metadata.get(name, {})
        info = {
            "name": name,
            "description": meta.get("description", ""),
            "ipv4": "",
            "netmask": "",
            "mac": "",
            "status": "DOWN",
            "is_ensp": False,
            "is_recommended": False,
        }

        ipv4_candidates = []
        for addr in addr_list:
            try:
                if addr.family == socket.AF_INET:
                    candidate = {
                        "address": addr.address or "",
                        "netmask": addr.netmask or "",
                    }
                    if candidate["address"]:
                        ipv4_candidates.append(candidate)
                elif addr.family == getattr(psutil, "AF_LINK", -1):
                    info["mac"] = addr.address or ""
            except Exception:
                pass

        best_ipv4 = _pick_best_ipv4(ipv4_candidates)
        if best_ipv4:
            info["ipv4"] = best_ipv4["address"]
            info["netmask"] = best_ipv4["netmask"]

        try:
            if name in stats:
                info["status"] = "UP" if stats[name].isup else "DOWN"
        except Exception:
            pass

        if info["ipv4"] or info["mac"]:
            score, _ = _rank_interface(info)
            info["is_ensp"] = score >= 40
            info["is_recommended"] = score >= 40
            interfaces.append(info)

    interfaces.sort(key=lambda x: (
        not x.get("is_recommended", False),
        not x.get("is_ensp", False),
        x.get("status") != "UP",
    ))

    return interfaces


@app.get("/api/init-status")
async def get_init_status():
    """获取初始化状态"""
    return {
        "success": True,
        "initialized": init_state["initialized"],
        "in_progress": init_state["in_progress"],
        "message": init_state["message"],
        "selected_interface": init_state.get("selected_interface"),
        "subnet": init_state.get("subnet"),
    }


@app.post("/api/trigger-init")
async def trigger_init():
    """手动触发初始化"""
    if init_state["in_progress"]:
        return {"success": False, "message": "初始化正在进行中"}
    if init_state["initialized"]:
        return {"success": True, "message": "已完成初始化"}

    # 在后台线程执行初始化
    import threading
    thread = threading.Thread(target=_auto_init, daemon=True)
    thread.start()
    return {"success": True, "message": "初始化已启动"}


@app.get("/api/server-data")
async def get_server_data():
    """获取服务器上所有缓存数据（浏览器刷新时使用）"""
    devices = load_devices()
    return {
        "success": True,
        "initialized": init_state["initialized"],
        "selected_interface": state.get("selected_interface"),
        "subnet": state.get("subnet"),
        "devices": devices,
        "collection_results": state.get("collection_results", {}),
        "capability_results": state.get("capability_results", {}),
    }


# ---------------------------------------------------------------------------
# 启动入口
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup_event():
    """服务器启动时自动初始化"""
    logger.info("服务器启动，开始自动初始化...")
    import threading
    thread = threading.Thread(target=_auto_init, daemon=True)
    thread.start()


if __name__ == "__main__":
    import uvicorn
    logger.info("=" * 50)
    logger.info("eNSP Web 管理系统 启动中...")
    logger.info(f"数据文件: {DEVICES_FILE}")
    logger.info("访问地址: http://127.0.0.1:8000")
    logger.info("=" * 50)
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
