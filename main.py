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

DEFAULT_SSH_USERNAME = "admin"
DEFAULT_SSH_PASSWORD = "Huawei@123"
DEFAULT_DEVICE_TYPE = "huawei"
SSH_TIMEOUT = 10
SSH_READ_TIMEOUT = 15
SSH_RETRY_COUNT = 2

# eNSP 管理网卡识别关键词
ENSP_KEYWORDS = ["Loopback", "VirtualBox", "Host-Only", "VMware", "KM-TEST", "NPCAP"]
EXCLUDED_INTERFACE_KEYWORDS = ["Loopback Pseudo-Interface", "Mihomo", "Meta Tunnel"]

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


def scan_subnet(subnet: str, exclude_ip: Optional[str] = None, max_workers: int = 50) -> List[str]:
    """
    并发扫描网段中的在线设备。
    自动跳过网络地址、广播地址和指定的排除 IP。

    Args:
        subnet: 网段，如 "192.168.100.0/24"
        exclude_ip: 需要跳过的 IP（通常是本机 IP）
        max_workers: 并发线程数

    Returns:
        在线 IP 列表（已排序）
    """
    try:
        network = ipaddress.IPv4Network(subnet, strict=False)
    except ValueError as e:
        logger.error(f"无效的网段格式: {subnet} - {e}")
        return []

    online_ips: List[str] = []
    hosts = [str(h) for h in network.hosts()]

    def _check(ip: str) -> Optional[str]:
        if ip == exclude_ip:
            return None
        return ip if ping_host(ip, timeout=1.5) else None

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_check, ip): ip for ip in hosts}
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
                if result:
                    online_ips.append(result)
            except Exception as e:
                logger.warning(f"扫描线程异常: {e}")

    online_ips.sort(key=lambda x: int(x.rsplit(".", 1)[1]))
    logger.info(f"网段 {subnet} 扫描完成，发现 {len(online_ips)} 台在线设备")
    return online_ips


# ---------------------------------------------------------------------------
# SSH 设备采集
# ---------------------------------------------------------------------------

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

    conn_params = {
        "device_type": device.get("device_type", DEFAULT_DEVICE_TYPE),
        "host": device["ip"],
        "username": device.get("username", DEFAULT_SSH_USERNAME),
        "password": device.get("password", DEFAULT_SSH_PASSWORD),
        "timeout": SSH_TIMEOUT,
        "conn_timeout": SSH_TIMEOUT,
        "fast_cli": False,  # 华为设备推荐 False，可减少登录问题
    }

    last_error = None
    for attempt in range(1, SSH_RETRY_COUNT + 1):
        try:
            from netmiko import ConnectHandler
            from netmiko.ssh_exception import NetMikoTimeoutException, NetMikoAuthenticationException

            connection = ConnectHandler(**conn_params)
            result["ssh_ok"] = True

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

    典型输出格式：
        Local Intf     Neighbor Dev     Neighbor Intf     Expiry
        GE0/0/0        SW1              GE0/0/1           120s
    """
    neighbors = []
    for line in output.strip().split("\n"):
        line = line.strip()
        # 跳过空行、表头、分隔线
        if not line or "Local Intf" in line or "---" in line or "Neighbor" in line:
            continue
        # 按 2+ 空格分割
        parts = re.split(r"\s{2,}", line)
        if len(parts) >= 3:
            neighbors.append({
                "local_interface": parts[0].strip(),
                "remote_device": parts[1].strip(),
                "remote_interface": parts[2].strip() if len(parts) > 2 else "",
            })
    return neighbors


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
            if not remote_dev:
                continue

            # 去重
            link_key = tuple(sorted([hostname, remote_dev])) + (local_intf,)
            if link_key in seen_links:
                continue
            seen_links.add(link_key)

            links.append({
                "source": hostname,
                "source_ip": ip,
                "target": remote_dev,
                "target_ip": name_to_ip.get(remote_dev, ""),
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

    # 按 IP 排序返回
    results.sort(key=lambda r: int(r["ip"].rsplit(".", 1)[1]))
    logger.info(f"批量采集完成: {len(results)} 台设备")
    return {"success": True, "results": results}


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


@app.get("/api/health")
async def health_check():
    """健康检查接口。"""
    return {"status": "ok", "version": "2.0.0"}


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
