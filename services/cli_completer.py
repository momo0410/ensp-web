"""
cli_completer.py - 命令补全服务
===============================
连接华为 VRP 设备，执行帮助命令并解析输出。
"""

import re
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger("ensp-web.cli-completer")


def parse_huawei_help(raw_output: str) -> List[Dict[str, str]]:
    """
    解析华为 VRP 命令帮助输出。

    华为 VRP 帮助输出格式示例：
    ```
      <cr>           
      abnormity      Enable the abnormity statistics function
      binding        Enable binding of an interface with a VPN instance
      fast-forward   Enable fast-forward
      nat            Specify NAT(Network Address Translation) configuration
      verify         Enable verify
    ```

    Args:
        raw_output: 设备原始帮助输出

    Returns:
        解析后的建议列表 [{"keyword": str, "description": str}]
    """
    suggestions = []

    if not raw_output:
        return suggestions

    lines = raw_output.strip().split("\n")

    for line in lines:
        line = line.strip()

        # 跳过空行和提示行
        if not line or line.startswith("<cr>") or line.startswith("^"):
            continue

        # 跳过错误信息
        if any(kw in line.lower() for kw in ["error", "unrecognized", "invalid", "%"]):
            continue

        # 解析格式：keyword      description
        # 匹配第一个空格分隔的关键字和后面的描述
        match = re.match(r'^(\S+)\s{2,}(.+)$', line)
        if match:
            keyword = match.group(1).strip()
            description = match.group(2).strip()

            # 过滤掉一些无用的条目
            if keyword in ("<cr>", "...", "---"):
                continue

            suggestions.append({
                "keyword": keyword,
                "description": description,
            })
            continue

        # 尝试匹配单个关键字（无描述）
        match = re.match(r'^(\S+)\s*$', line)
        if match:
            keyword = match.group(1).strip()
            if keyword not in ("<cr>", "...", "---"):
                suggestions.append({
                    "keyword": keyword,
                    "description": "",
                })

    return suggestions


def execute_cli_complete(
    device: dict,
    view_commands: List[str],
    prefix: str,
    source_ip: Optional[str] = None,
    socket_factory=None,
) -> Dict[str, Any]:
    """
    连接设备执行命令补全。

    Args:
        device: 设备信息字典
        view_commands: 视图切换命令列表（如 ["system-view", "interface GE0/0/1"]）
        prefix: 要补全的命令前缀（如 "ip"）
        source_ip: 本机管理源 IP
        socket_factory: socket 工厂函数

    Returns:
        {
            "success": bool,
            "raw": str,           # 设备原始输出
            "suggestions": [...], # 解析后的建议
            "error": str,         # 错误信息
        }
    """
    from .device_connector import import_netmiko, build_conn_params, safe_disconnect

    result = {
        "success": False,
        "raw": "",
        "suggestions": [],
        "error": "",
    }

    connection = None

    try:
        ConnectHandler, NetMikoTimeoutException, NetMikoAuthenticationException = import_netmiko()

        # 构建连接参数
        params = build_conn_params(device, timeout=10)

        ssh_sock = None
        if source_ip and socket_factory:
            try:
                ssh_sock = socket_factory(device["ip"], 22, timeout=10)
                params["sock"] = ssh_sock
            except Exception as e:
                logger.warning("[%s] 创建 socket 失败: %s", device["ip"], e)

        # 连接设备
        connection = ConnectHandler(**params)

        # 执行视图切换命令
        for view_cmd in view_commands:
            view_cmd = view_cmd.strip()
            if not view_cmd:
                continue

            # 判断是否需要处理确认提示
            output = connection.send_command_timing(view_cmd, strip_prompt=False, strip_command=False)

            # 处理可能的确认提示
            if "[Y/N]" in output or "[y/n]" in output or "Are you sure" in output:
                output += connection.send_command_timing("Y", strip_prompt=False, strip_command=False)

        # 执行帮助命令（在当前视图下）
        help_command = f"{prefix} ?"
        logger.info("[%s] 执行命令补全: %s", device["ip"], help_command)

        raw_output = connection.send_command_timing(
            help_command,
            strip_prompt=False,
            strip_command=False,
            read_timeout=10,
        )

        result["raw"] = raw_output

        # 解析帮助输出
        suggestions = parse_huawei_help(raw_output)
        result["suggestions"] = suggestions
        result["success"] = True

        # 断开连接
        safe_disconnect(connection)
        connection = None

    except Exception as e:
        error_msg = str(e)
        if "Timeout" in type(e).__name__:
            result["error"] = f"SSH 连接超时: {error_msg}"
        elif "Authentication" in type(e).__name__:
            result["error"] = f"SSH 认证失败: {error_msg}"
        else:
            result["error"] = f"命令补全失败: {error_msg}"
        logger.error("[%s] 命令补全失败: %s", device["ip"], e)
    finally:
        if connection:
            try:
                connection.disconnect()
            except Exception:
                pass

    return result


def get_common_commands(view_context: str = "") -> List[Dict[str, str]]:
    """
    获取常用命令建议（离线，不连接设备）。

    Args:
        view_context: 当前视图上下文（如 "system-view", "interface"）

    Returns:
        命令建议列表
    """
    # 系统视图常用命令
    system_view_commands = [
        {"keyword": "interface", "description": "进入接口视图"},
        {"keyword": "ip", "description": "IP 配置"},
        {"keyword": "ospf", "description": "OSPF 配置"},
        {"keyword": "vlan", "description": "VLAN 配置"},
        {"keyword": "acl", "description": "ACL 配置"},
        {"keyword": "dhcp", "description": "DHCP 配置"},
        {"keyword": "nat", "description": "NAT 配置"},
        {"keyword": "route-static", "description": "静态路由"},
        {"keyword": "lldp", "description": "LLDP 配置"},
        {"keyword": "snmp-agent", "description": "SNMP 配置"},
        {"keyword": "ntp-service", "description": "NTP 配置"},
        {"keyword": "stelnet", "description": "STelnet 配置"},
        {"keyword": "user-interface", "description": "用户界面配置"},
        {"keyword": "aaa", "description": "AAA 配置"},
        {"keyword": "firewall", "description": "防火墙配置"},
        {"keyword": "ipsec", "description": "IPSec 配置"},
        {"keyword": "ike", "description": "IKE 配置"},
    ]

    # 接口视图常用命令
    interface_commands = [
        {"keyword": "ip address", "description": "配置 IP 地址"},
        {"keyword": "shutdown", "description": "关闭接口"},
        {"keyword": "undo shutdown", "description": "开启接口"},
        {"keyword": "description", "description": "接口描述"},
        {"keyword": "mtu", "description": "MTU 配置"},
        {"keyword": "nat", "description": "NAT 配置"},
        {"keyword": "vrrp", "description": "VRRP 配置"},
        {"keyword": "dhcp", "description": "DHCP 配置"},
        {"keyword": "ospf", "description": "OSPF 接口配置"},
        {"keyword": "lldp", "description": "LLDP 配置"},
    ]

    if "interface" in view_context.lower():
        return interface_commands
    return system_view_commands
