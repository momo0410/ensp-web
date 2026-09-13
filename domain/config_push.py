"""
配置下发：删除命令生成与配置推送。

generate_delete_commands 纯函数生成 undo 命令；
push_config_to_device 通过 SSH 推送命令并读取回显。
"""

from typing import List

from app.config import (
    DEFAULT_DEVICE_TYPE,
    DEFAULT_SSH_PASSWORD,
    DEFAULT_SSH_USERNAME,
    SSH_TIMEOUT,
    logger,
)
from app.state import state
from domain.netprobe import _create_management_socket, _management_source_ip_for
from domain.ssh_collect import _import_netmiko

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
