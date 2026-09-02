"""
live_check.py - 设备侧连通性验证
================================
在设备上执行 VRP ping 并解析回显，回答"真的通了吗"——
管理面缓存（能力/角色/配置关键字）只能说明"配了没有"，
学生实验的最终验收（如 PC1 ping 通 PC3 的网关）需要数据面证据。

用法：
    ping_from_device(device, target, count=4, source="192.168.1.1")
返回丢包率/平均时延/原始回显，供前端展示与 AI 助教引用。
"""

import re
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("ensp-web.live_check")

# 目标白名单：IPv4 / 主机名，防止把任意字符注入命令行
_TARGET_RE = re.compile(r"^[A-Za-z0-9.\-]{1,64}$")

# VRP ping 统计行：
#   4 packet(s) transmitted
#   4 packet(s) received
#   0.00% packet loss
# round-trip min/avg/max = 1/1/2 ms
_RX_TRANSMITTED = re.compile(r"(\d+)\s+packet\(s\)\s+transmitted")
_RX_RECEIVED = re.compile(r"(\d+)\s+packet\(s\)\s+received")
_RX_LOSS = re.compile(r"([\d.]+)%\s+packet\s+loss")
_RX_RTT = re.compile(r"round-trip\s+min/avg/max\s*=\s*([\d.]+)/([\d.]+)/([\d.]+)")


def parse_ping_output(output: str) -> Dict[str, Any]:
    """从 VRP ping 回显解析统计结果。"""
    transmitted = received = 0
    loss = None
    rtt_min = rtt_avg = rtt_max = None
    if output:
        m = _RX_TRANSMITTED.search(output)
        if m:
            transmitted = int(m.group(1))
        m = _RX_RECEIVED.search(output)
        if m:
            received = int(m.group(1))
        m = _RX_LOSS.search(output)
        if m:
            loss = float(m.group(1))
        m = _RX_RTT.search(output)
        if m:
            rtt_min, rtt_avg, rtt_max = (float(m.group(i)) for i in (1, 2, 3))
    return {
        "transmitted": transmitted,
        "received": received,
        "loss_rate": loss,
        "min_ms": rtt_min,
        "avg_ms": rtt_avg,
        "max_ms": rtt_max,
    }


def ping_from_device(
    device: dict,
    target: str,
    count: int = 4,
    source: Optional[str] = None,
    source_bind: Optional[str] = None,
    management_socket_factory=None,
    timeout: int = 12,
    read_timeout: int = 30,
) -> Dict[str, Any]:
    """
    通过 SSH 在设备上执行 ping。

    Args:
        device: 设备字典（ip/username/password/device_type）
        target: 目标 IPv4 或主机名
        count: 回显次数（1-20）
        source: VRP ping 的 -a 源地址（用于从指定网段发起，路由实验常用）
        source_bind: 本机管理源 IP（SSH 连接绑定用）
        management_socket_factory: 管理面 socket 工厂
    """
    target = (target or "").strip()
    if not _TARGET_RE.match(target):
        return {"success": False, "error": f"非法目标: {target!r}",
                "target": target, "raw_output": ""}
    try:
        count = max(1, min(int(count), 20))
    except (TypeError, ValueError):
        count = 4

    parts = ["ping"]
    if source:
        if not _TARGET_RE.match(source):
            return {"success": False, "error": f"非法源地址: {source!r}",
                    "target": target, "raw_output": ""}
        parts += ["-a", source]
    parts += ["-c", str(count), target]
    command = " ".join(parts)

    result: Dict[str, Any] = {
        "target": target,
        "source": source or "",
        "command": command,
        "success": False,
        "error": None,
        "raw_output": "",
    }
    result.update(parse_ping_output(""))
    result["transmitted"] = count

    from .device_connector import (
        import_netmiko, build_conn_params, safe_disconnect, strip_ansi,
    )

    conn = None
    ssh_sock = None
    try:
        ConnectHandler, _, _ = import_netmiko()
        params = build_conn_params(device, timeout=timeout)
        if source_bind and management_socket_factory:
            ssh_sock = management_socket_factory(device["ip"], 22, timeout=timeout)
            params["sock"] = ssh_sock
        conn = ConnectHandler(**params)
        ssh_sock = None
        conn.send_command_timing("screen-length 0 temporary", read_timeout=5)
        # ping 逐包回显间隔最长 2s/包，timing 模式按空闲读取
        output = conn.send_command_timing(command, read_timeout=read_timeout,
                                          delay_factor=2)
        output = strip_ansi(output or "")
        result["raw_output"] = output[:4000]
        stats = parse_ping_output(output)
        if stats["received"] or stats["loss_rate"] is not None:
            result.update(stats)
            if stats["transmitted"] == 0:
                stats["transmitted"] = count
            result["success"] = stats["received"] > 0
        else:
            result["error"] = "无法解析 ping 回显（可能超时或设备无响应）"
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        logger.warning("[%s] 设备侧 ping %s 失败: %s", device.get("ip"), target, e)
    finally:
        safe_disconnect(conn)
        if ssh_sock:
            try:
                ssh_sock.close()
            except OSError:
                pass
    return result
