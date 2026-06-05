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
import time
from pathlib import Path
from typing import Optional, List, Dict, Any

import psutil
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from services.capability_probe import probe_capabilities
from services.config_builder import (
    build_preview,
    build_config_commands,
    get_verify_commands,
    get_templates_for_role,
    get_all_templates,
    CONFIG_TEMPLATES,
)
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
    "display lldp neighbor",
    "display device",
    "display cpu-usage",
    "display memory-usage",
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
    Windows 使用 `ping -n 1 -w timeout`，类 Unix 使用 `ping -c 1 -W timeout`。
    """
    try:
        is_windows = sys.platform == "win32"
        if is_windows:
            cmd = ["ping", "-n", "1", "-w", str(int(timeout * 1000)), ip]
        else:
            cmd = ["ping", "-c", "1", "-W", str(int(timeout)), ip]

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
    while time.time() < end_time:
        try:
            chunk = sock.recv(8192)
            if not chunk:
                break
            output += chunk
            end_time = time.time() + wait
        except socket.timeout:
            break
    return output.decode("gbk", errors="replace").replace("\x00", "")


def _console_send_command(sock: socket.socket, command: str, max_wait: float = 5.0) -> str:
    sock.sendall((command + "\r\n").encode("ascii", errors="ignore"))
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
        sock.sendall(b"\r\n")
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


def ssh_collect_device(device: dict) -> dict:
    """
    通过 Netmiko SSH 连接华为设备并采集信息。

    特性：
    - 每条命令独立捕获异常，单条失败不影响其他
    - 支持自动重试（SSH_RETRY_COUNT）
    - 返回结构化的 JSON 结果

    Args:
        device: 设备信息字典（需包含 ip, name, username, password, device_type）

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
                connection.send_command_timing("system-view")
                connection.send_command_timing("lldp enable")
                connection.send_command_timing("return")
                logger.debug(f"[{device['ip']}] LLDP 已启用")
            except Exception as e:
                logger.debug(f"[{device['ip']}] 启用 LLDP 时异常（可忽略）: {e}")

            # 执行每条命令
            for cmd in HUAWEI_COMMANDS:
                try:
                    output = connection.send_command(cmd, read_timeout=SSH_READ_TIMEOUT)
                    result["commands"][cmd] = {"success": True, "output": output}
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

# 要检测的协议/功能段及其匹配规则
# key: 协议标识, value: (显示名称, 段起始正则, 段结束正则)
_CONFIG_SECTIONS = [
    ("ospf",     "OSPF",     r"^ospf\b",              r"^(?:ospf|rip|bgp|isis|dhcp|nat|acl|ipsec|ike|ntp|snmp|#)"),
    ("rip",      "RIP",      r"^rip\b",               r"^(?:ospf|rip|bgp|isis|dhcp|nat|acl|ipsec|ike|ntp|snmp|#)"),
    ("bgp",      "BGP",      r"^bgp\b",               r"^(?:ospf|rip|bgp|isis|dhcp|nat|acl|ipsec|ike|ntp|snmp|#)"),
    ("isis",     "IS-IS",    r"^isis\b",              r"^(?:ospf|rip|bgp|isis|dhcp|nat|acl|ipsec|ike|ntp|snmp|#)"),
    ("dhcp",     "DHCP",     r"^dhcp\b",              r"^(?:ospf|rip|bgp|isis|dhcp|nat|acl|ipsec|ike|ntp|snmp|#)"),
    ("nat",      "NAT",      r"^nat\b",               r"^(?:ospf|rip|bgp|isis|dhcp|nat|acl|ipsec|ike|ntp|snmp|#)"),
    ("ipsec",    "IPSec",    r"^ipsec\b",             r"^(?:ospf|rip|bgp|isis|dhcp|nat|acl|ipsec|ike|ntp|snmp|#)"),
    ("ntp",      "NTP",      r"^ntp\b",               r"^(?:ospf|rip|bgp|isis|dhcp|nat|acl|ipsec|ike|ntp|snmp|#)"),
    ("snmp",     "SNMP",     r"^snmp-agent\b",        r"^(?:ospf|rip|bgp|isis|dhcp|nat|acl|ipsec|ike|ntp|snmp|#)"),
    ("static",   "静态路由",  r"^ip route-static\b",    None),
]

# ACL 段单独处理（可能有多条）
_ACL_SECTION_RE = re.compile(r"^acl\s+(?:number\s+)?(\d+)", re.IGNORECASE)


def _parse_config_sections(info: dict, config_output: str) -> None:
    """
    从 display current-configuration 输出中解析各协议/功能配置段。
    结果写入 info['protocols'] 和 info['config_sections']。
    """
    if not config_output:
        return

    lines = config_output.split("\n")
    protocols: List[str] = []
    config_sections: Dict[str, dict] = {}

    # --- 解析常规协议段 ---
    for section_id, display_name, start_re, end_re in _CONFIG_SECTIONS:
        section_lines: List[str] = []
        in_section = False
        start_pattern = re.compile(start_re, re.IGNORECASE)
        end_pattern = re.compile(end_re, re.IGNORECASE) if end_re else None

        for line in lines:
            stripped = line.strip()
            if not in_section:
                if start_pattern.match(stripped):
                    in_section = True
                    section_lines.append(line.rstrip())
            else:
                # 遇到同级配置段开头、#分隔符或返回标记则结束
                if stripped == "#":
                    break
                if end_pattern and end_pattern.match(stripped) and not start_pattern.match(stripped):
                    break
                if stripped.startswith("return"):
                    break
                section_lines.append(line.rstrip())

        if section_lines:
            protocols.append(section_id)
            config_sections[section_id] = {
                "name": display_name,
                "configured": True,
                "config": "\n".join(section_lines),
            }

    # --- 解析 ACL 段（可能有多条） ---
    acl_blocks: List[dict] = []
    current_acl: Optional[List[str]] = None
    current_acl_id = ""

    for line in lines:
        stripped = line.strip()
        if _ACL_SECTION_RE.match(stripped):
            if current_acl:
                acl_blocks.append({"acl_id": current_acl_id, "config": "\n".join(current_acl)})
            current_acl = [line.rstrip()]
            m = _ACL_SECTION_RE.match(stripped)
            current_acl_id = m.group(1) if m else ""
        elif current_acl is not None:
            if stripped.startswith("#"):
                acl_blocks.append({"acl_id": current_acl_id, "config": "\n".join(current_acl)})
                current_acl = None
            elif stripped.startswith("return"):
                acl_blocks.append({"acl_id": current_acl_id, "config": "\n".join(current_acl)})
                current_acl = None
            else:
                current_acl.append(line.rstrip())

    if current_acl:
        acl_blocks.append({"acl_id": current_acl_id, "config": "\n".join(current_acl)})

    if acl_blocks:
        protocols.append("acl")
        config_sections["acl"] = {
            "name": "ACL",
            "configured": True,
            "config": "\n\n".join(
                f"# ACL {b['acl_id']}\n{b['config']}" for b in acl_blocks
            ),
        }

    info["protocols"] = protocols
    info["config_sections"] = config_sections


# ---------------------------------------------------------------------------
# 协议配置命令生成器
# ---------------------------------------------------------------------------

def generate_ospf_commands(params: dict) -> List[str]:
    """生成 OSPF 配置命令"""
    commands = []
    process_id = params.get("process_id", "1")
    area_id = params.get("area_id", "0.0.0.0")
    network = params.get("network", "")
    wildcard = params.get("wildcard", "")
    auth_type = params.get("auth_type", "none")

    commands.append(f"ospf {process_id}")
    commands.append(f"area {area_id}")
    if network and wildcard:
        commands.append(f"network {network} {wildcard}")
    if auth_type == "simple":
        commands.append("authentication-mode simple")
    elif auth_type == "md5":
        commands.append("authentication-mode md5")
    return commands


def generate_bgp_commands(params: dict) -> List[str]:
    """生成 BGP 配置命令"""
    commands = []
    local_as = params.get("local_as", "")
    router_id = params.get("router_id", "")
    neighbor_ip = params.get("neighbor_ip", "")
    neighbor_as = params.get("neighbor_as", "")
    network = params.get("network", "")

    if not local_as:
        return []
    commands.append(f"bgp {local_as}")
    if router_id:
        commands.append(f"router-id {router_id}")
    if neighbor_ip and neighbor_as:
        commands.append(f"peer {neighbor_ip} as-number {neighbor_as}")
    if network:
        commands.append(f"network {network}")
    return commands


def generate_rip_commands(params: dict) -> List[str]:
    """生成 RIP 配置命令"""
    commands = []
    version = params.get("version", "2")
    network = params.get("network", "")

    if not network:
        return []
    commands.append("rip")
    commands.append(f"version {version}")
    commands.append(f"network {network}")
    return commands


def generate_isis_commands(params: dict) -> List[str]:
    """生成 IS-IS 配置命令"""
    commands = []
    process_id = params.get("process_id", "1")
    net_entity = params.get("net_entity", "")
    level = params.get("level", "level-2")

    if not net_entity:
        return []
    commands.append(f"isis {process_id}")
    commands.append(f"network-entity {net_entity}")
    commands.append(f"is-level {level}")
    return commands


def generate_static_route_commands(params: dict) -> List[str]:
    """生成静态路由配置命令"""
    commands = []
    dest_network = params.get("dest_network", "")
    mask = params.get("mask", "")
    next_hop = params.get("next_hop", "")
    interface = params.get("interface", "")

    if not dest_network or not mask:
        return []
    if next_hop:
        commands.append(f"ip route-static {dest_network} {mask} {next_hop}")
    elif interface:
        commands.append(f"ip route-static {dest_network} {mask} {interface}")
    return commands


def generate_nat_commands(params: dict) -> List[str]:
    """生成 NAT 配置命令"""
    commands = []
    nat_type = params.get("nat_type", "easyip")
    inside_interface = params.get("inside_interface", "")
    outside_interface = params.get("outside_interface", "")
    acl_number = params.get("acl_number", "")

    if not inside_interface or not outside_interface:
        return []
    commands.append(f"interface {inside_interface}")
    commands.append("nat outbound")
    if nat_type == "easyip":
        commands.append("nat outbound 3000")
    return commands


def generate_dhcp_commands(params: dict) -> List[str]:
    """生成 DHCP 配置命令"""
    commands = []
    pool_name = params.get("pool_name", "")
    network = params.get("network", "")
    mask = params.get("mask", "")
    gateway = params.get("gateway", "")
    dns = params.get("dns", "")
    lease_days = params.get("lease_days", "1")

    if not pool_name or not network:
        return []
    commands.append(f"ip pool {pool_name}")
    commands.append(f"network {network} mask {mask}")
    if gateway:
        commands.append(f"gateway-list {gateway}")
    if dns:
        commands.append(f"dns-list {dns}")
    commands.append(f"lease day {lease_days}")
    return commands


def generate_acl_commands(params: dict) -> List[str]:
    """生成 ACL 配置命令"""
    commands = []
    acl_number = params.get("acl_number", "")
    action = params.get("action", "permit")
    source = params.get("source", "")
    source_wildcard = params.get("source_wildcard", "")
    dest = params.get("dest", "")

    if not acl_number or not source:
        return []
    commands.append(f"acl number {acl_number}")
    if dest:
        commands.append(f"rule 5 {action} source {source} {source_wildcard} destination {dest}")
    else:
        commands.append(f"rule 5 {action} source {source} {source_wildcard}")
    return commands


def generate_ntp_commands(params: dict) -> List[str]:
    """生成 NTP 配置命令"""
    commands = []
    server_ip = params.get("server_ip", "")

    if not server_ip:
        return []
    commands.append(f"ntp-service unicast-server {server_ip}")
    return commands


def generate_snmp_commands(params: dict) -> List[str]:
    """生成 SNMP 配置命令"""
    commands = []
    version = params.get("version", "v2c")
    community = params.get("community", "")
    trap_target = params.get("trap_target", "")

    if not community:
        return []
    if version == "v2c":
        commands.append(f"snmp-agent community read {community}")
    if trap_target:
        commands.append(f"snmp-agent target-host trap address udp-domain {trap_target} params securityname {community}")
    return commands


def generate_ipsec_commands(params: dict) -> List[str]:
    """生成 IPSec VPN 配置命令"""
    commands = []
    ike_proposal = params.get("ike_proposal", "1")
    encryption = params.get("encryption", "aes-128")
    dh_group = params.get("dh_group", "2")
    pre_share_key = params.get("pre_share_key", "")
    acl_number = params.get("acl_number", "3000")
    local_subnet = params.get("local_subnet", "")
    remote_subnet = params.get("remote_subnet", "")
    remote_ip = params.get("remote_ip", "")

    if not pre_share_key or not remote_ip:
        return []

    # IKE 提议
    commands.append(f"ike proposal {ike_proposal}")
    commands.append(f"encryption-algorithm {encryption}")
    commands.append(f"dh group{dh_group}")

    # IKE 对等体
    commands.append(f"ike peer-peer v1")
    commands.append(f"pre-shared-key cipher {pre_share_key}")
    commands.append(f"remote-address {remote_ip}")

    # ACL
    commands.append(f"acl number {acl_number}")
    commands.append(f"rule 5 permit ip source {local_subnet} destination {remote_subnet}")

    # IPSec 提议
    commands.append("ipsec proposal default")
    commands.append(f"transform {encryption}")

    # IPSec 策略
    commands.append("ipsec policy map1 1 isakmp")
    commands.append(f"security acl {acl_number}")
    commands.append(f"ike-peer v1")

    return commands


# 协议配置命令生成器映射
PROTOCOL_COMMAND_GENERATORS = {
    "ospf": generate_ospf_commands,
    "bgp": generate_bgp_commands,
    "rip": generate_rip_commands,
    "isis": generate_isis_commands,
    "static": generate_static_route_commands,
    "nat": generate_nat_commands,
    "dhcp": generate_dhcp_commands,
    "acl": generate_acl_commands,
    "ntp": generate_ntp_commands,
    "snmp": generate_snmp_commands,
    "ipsec": generate_ipsec_commands,
}


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
    elif protocol == "static":
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
        output = conn.send_config_set(commands)
        conn.send_command_timing("return")
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


@app.get("/api/devices/{ip}/protocols")
async def get_device_protocols(ip: str):
    """获取设备支持的协议列表"""
    result = state["collection_results"].get(ip)
    if not result:
        raise HTTPException(status_code=404, detail="未找到设备采集结果，请先采集设备")

    info = result.get("info", {})
    protocols = info.get("protocols", [])
    config_sections = info.get("config_sections", {})

    return {
        "success": True,
        "ip": ip,
        "protocols": protocols,
        "config_sections": config_sections,
    }


@app.get("/api/devices/{ip}/protocols/{protocol}")
async def get_device_protocol_config(ip: str, protocol: str):
    """获取指定协议的当前配置"""
    result = state["collection_results"].get(ip)
    if not result:
        raise HTTPException(status_code=404, detail="未找到设备采集结果")

    config_sections = result.get("info", {}).get("config_sections", {})
    protocol_config = config_sections.get(protocol, {})

    return {
        "success": True,
        "ip": ip,
        "protocol": protocol,
        "config": protocol_config,
    }


@app.post("/api/devices/{ip}/protocols/{protocol}")
async def apply_device_protocol_config(ip: str, protocol: str, req: ProtocolConfigRequest):
    """应用协议配置到设备"""
    devices = load_devices()
    device = next((d for d in devices if d["ip"] == ip), None)
    if not device:
        raise HTTPException(status_code=404, detail=f"未找到 IP 为 {ip} 的设备")

    # 生成配置命令
    if req.mode == "command":
        commands = req.commands
    else:
        generator = PROTOCOL_COMMAND_GENERATORS.get(protocol)
        if not generator:
            raise HTTPException(status_code=400, detail=f"不支持配置协议: {protocol}")
        commands = generator(req.params)

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
async def collect_device(ip: str):
    """连接指定设备并采集信息。"""
    devices = load_devices()
    device = next((d for d in devices if d["ip"] == ip), None)
    if not device:
        raise HTTPException(status_code=404, detail=f"未找到 IP 为 {ip} 的设备")

    # 先 ping 检测，离线则直接返回
    online = ping_host(ip)
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

    result = ssh_collect_device(device)
    state["collection_results"][ip] = result
    return {"success": True, "result": result}


@app.post("/api/collect-all")
async def collect_all():
    """批量采集所有设备信息（并行执行）。"""
    devices = load_devices()
    if not devices:
        return {"success": True, "results": [], "message": "设备列表为空"}

    results: List[dict] = []

    def _collect_single(device: dict) -> dict:
        ip = device["ip"]
        online = ping_host(ip)
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
        return ssh_collect_device(device)

    # 并行采集所有设备（每台设备一个线程）
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
    """批量识别所有设备能力。"""
    devices = load_devices()
    if not devices:
        return {"success": True, "results": [], "message": "设备列表为空"}

    results = []

    def _probe_one(device: dict) -> dict:
        src = _management_source_ip_for(device["ip"])
        return probe_capabilities(
            device,
            source_ip=src,
            management_socket_factory=_create_management_socket,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(devices), 5)) as executor:
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
    """自由配置预览：变量替换，不连接设备。"""
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


@app.get("/api/scripts")
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
# 启动入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    logger.info("=" * 50)
    logger.info("eNSP Web 管理系统 启动中...")
    logger.info(f"数据文件: {DEVICES_FILE}")
    logger.info("访问地址: http://127.0.0.1:8000")
    logger.info("=" * 50)
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
