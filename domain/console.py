"""
eNSP 本地 Console（telnet 127.0.0.1）通道。

用于设备还没有管理 IP 时直连 Console 端口完成初始化配置、
以及诊断 Cloud 桥接状态。与 SSH 通道相互独立。
"""

import concurrent.futures
import ipaddress
import re
import socket
import time
from typing import List, Optional

import psutil

from app.config import logger
from app.state import state
from domain.netprobe import ping_host, ssh_service_open
from domain.parse import _regex_find


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
