"""
网络探测：主机可达性、TCP 端口、管理网卡源地址选择、网段扫描。

管理网卡源地址的选择会读取应用共享状态（用户选中的网卡），
目的是避免流量走代理/TUN 默认路由。
"""

import concurrent.futures
import ipaddress
import socket
import subprocess
import sys
from typing import List, Optional

import psutil

from app.config import logger
from app.state import state


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
