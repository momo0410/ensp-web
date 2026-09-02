"""
command_probe.py - 设备 ? 探测引导（自动发现设备支持的命令）
=========================================================
连上设备后，逐视图抓取 VRP 的帮助输出（display ? / 系统视图 ? /
接口视图 ?），与命令目录 COMMAND_CATALOG 中每条的 trigger 关键字比对，
逐条标注该设备「是否支持此指令」。解决「如何知道设备支持什么」的问题。

设计要点（延迟调到无感知）：
- 双通道：SSH（netmiko）/ Console（零 IP，raw socket 连 127.0.0.1:port）。
- 单次探测：screen-length 0 temporary → display ? → system-view → ? →
  接口视图 ?，全部 `?` 单独成行（部分输入+回车会真执行命令，产生副作用）。
- 通道健壮性：ANSI 转义清理（console 回显会被光标控制码吃掉首字符）、
  回显校验（对不上自动重发一次）、`?` 走 timing 模式读取。
- 缓存：按设备（IP / Console 端口）缓存探测结果，TTL 5 分钟；命中秒回。
- 后台执行（由 main.py 用 BackgroundTasks 调度），API 立即返回 job_id，
  前端轮询，UI 不阻塞 → 用户无感知。
"""

import re
import time
import socket
import logging
from typing import Dict, Any, List, Optional, Set, Tuple

from .command_catalog import COMMAND_CATALOG
from .help_tree import parse_help_keywords, find_first_physical_interface
from .device_connector import strip_ansi, ask_help_timing

logger = logging.getLogger("ensp-web.command_probe")

# 探测结果缓存（按设备）：key -> (timestamp, result)
# eNSP 设备支持的命令集合由镜像决定（重启/重配都不变），缓存 30 分钟足够稳
PROBE_CACHE_TTL = 1800  # 30 分钟
_probe_cache: Dict[str, Tuple[float, dict]] = {}

# 连接超时（秒）—— 短超时保证无感知
SSH_TIMEOUT = 6
SSH_READ_TIMEOUT = 6
CONSOLE_TIMEOUT = 8
CONSOLE_READ_TIMEOUT = 5


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------
def cache_key(target: dict) -> str:
    mode = target.get("mode")
    if mode == "console":
        return f"console:{target.get('port')}"
    return f"ssh:{target.get('ip')}"


def get_cached_probe(key: str) -> Optional[dict]:
    item = _probe_cache.get(key)
    if not item:
        return None
    ts, result = item
    if time.time() - ts > PROBE_CACHE_TTL:
        _probe_cache.pop(key, None)
        return None
    return result


def set_cached_probe(key: str, result: dict) -> None:
    _probe_cache[key] = (time.time(), result)


# ---------------------------------------------------------------------------
# 设备类型识别（基于 display version 的型号）
# ---------------------------------------------------------------------------
_MODEL_RE = re.compile(
    r"(AR\d+|S\d+|NE\d+|CE\d+|USG\d+|LSW\d+|AC\d+|Router|Switch|CloudEngine)",
    re.IGNORECASE,
)


def detect_device_type(version_output: str) -> str:
    """从 display version 输出推断 eNSP 设备类型。"""
    if not version_output:
        return "unknown"
    m = _MODEL_RE.search(version_output)
    raw = (m.group(0) if m else "").upper()
    if raw.startswith("AR") or raw.startswith("NE") or raw == "ROUTER":
        return "router"
    if raw.startswith("USG"):
        return "firewall"
    if raw.startswith("AC"):
        return "ac"
    if raw.startswith("CE") or raw.startswith("CLOUDENGINE"):
        return "cloud"
    if raw.startswith("S"):
        return "l3_switch"
    if raw.startswith("LSW") or raw == "SWITCH":
        return "switch"
    return "unknown"


def _extract_model(version_output: str) -> str:
    if not version_output:
        return ""
    m = _MODEL_RE.search(version_output)
    return m.group(0) if m else ""


# ---------------------------------------------------------------------------
# SSH 通道探测
# ---------------------------------------------------------------------------
def probe_via_ssh(target: dict, timeout: int = SSH_TIMEOUT,
                  read_timeout: int = SSH_READ_TIMEOUT
                  ) -> Tuple[str, Dict[str, Set[str]]]:
    """通过 SSH 连接设备，返回 (display version 输出, 各视图关键字集)。"""
    from .device_connector import connect_device, safe_disconnect

    device = {
        "ip": target.get("ip"),
        "username": target.get("username"),
        "password": target.get("password"),
        "device_type": target.get("device_type", "huawei"),
    }
    conn = None
    version = ""
    views: Dict[str, Set[str]] = {}
    try:
        conn = connect_device(device, timeout=timeout)
        try:
            version = conn.send_command("display version", read_timeout=read_timeout)
        except Exception as e:
            logger.warning("probe ssh display version 失败: %s", e)
        try:
            conn.send_command_timing("screen-length 0 temporary",
                                     read_timeout=read_timeout)
        except Exception:
            pass

        # display ? —— display 子命令全集
        try:
            views["display"] = parse_help_keywords(
                strip_ansi(ask_help_timing(conn, "display ?", read_timeout)))
        except Exception as e:
            logger.warning("probe ssh display ? 失败: %s", e)

        # 系统视图 ?
        try:
            conn.send_command_timing("system-view", read_timeout=read_timeout)
            views["system"] = parse_help_keywords(
                strip_ansi(ask_help_timing(conn, "?", read_timeout)))
        except Exception as e:
            logger.warning("probe ssh system-view ? 失败: %s", e)

        # 接口视图 ?（进入真实存在的物理接口，无副作用）
        if views.get("system"):
            try:
                brief = ask_help_timing(conn, "display interface brief", read_timeout)
            except Exception:
                brief = ""
            interface_name = find_first_physical_interface(
                brief, strip_ansi(brief))
            if interface_name:
                try:
                    conn.send_command_timing(f"interface {interface_name}",
                                             read_timeout=read_timeout)
                    views["interface"] = parse_help_keywords(
                        strip_ansi(ask_help_timing(conn, "?", read_timeout)))
                except Exception as e:
                    logger.warning("probe ssh interface ? 失败: %s", e)
                try:
                    conn.send_command_timing("quit", read_timeout=read_timeout)
                except Exception:
                    pass

        try:
            conn.send_command_timing("return", read_timeout=read_timeout)
        except Exception:
            pass
    finally:
        safe_disconnect(conn)
    return version, views


# ---------------------------------------------------------------------------
# Console 通道探测（零 IP，raw socket 连 127.0.0.1:port）
# ---------------------------------------------------------------------------
def _console_connect(port: int, timeout: float = 2.0) -> socket.socket:
    """连接 eNSP 本地 Console 端口并清掉登录提示。"""
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    sock.settimeout(timeout)
    _console_recv_all(sock, max_wait=0.5)
    return sock


def _console_recv_all(sock: socket.socket, wait: float = 0.25, max_wait: float = 3.0) -> str:
    output = b""
    end = time.time() + max_wait
    sock.settimeout(wait)
    while time.time() < end:
        try:
            chunk = sock.recv(8192)
        except socket.timeout:
            break
        except OSError:
            break
        if not chunk:
            break
        output += chunk
        end = time.time() + wait
    return output.decode("gbk", errors="replace").replace("\x00", "")


def _console_send(sock: socket.socket, command: str, read_timeout: float = 5.0) -> str:
    """发送命令并读回输出；带回显校验，回显被 ANSI 码吃掉时自动重发一次。"""
    sock.sendall((command + "\r\n").encode("ascii", errors="ignore"))
    time.sleep(0.2)
    out = _console_recv_all(sock, max_wait=read_timeout)

    # 回显校验：清理 ANSI 后应能看到所发命令（VRP 会回显输入行）。
    # 校验失败说明回显被光标控制码截断（如 "isplay vlan"），重发一次。
    cleaned = strip_ansi(out)
    if command not in cleaned.replace("\r", ""):
        time.sleep(0.3)
        sock.sendall((command + "\r\n").encode("ascii", errors="ignore"))
        time.sleep(0.2)
        out = _console_recv_all(sock, max_wait=read_timeout)
        cleaned = strip_ansi(out)

    # 处理分页 -- More --
    more = 0
    while "---- More ----" in cleaned and more < 20:
        sock.sendall(b" ")
        time.sleep(0.1)
        cleaned += strip_ansi(_console_recv_all(sock, max_wait=2.0))
        more += 1
    return cleaned


def probe_via_console(target: dict, timeout: int = CONSOLE_TIMEOUT,
                      read_timeout: float = CONSOLE_READ_TIMEOUT
                      ) -> Tuple[str, Dict[str, Set[str]]]:
    """通过 eNSP 本地 Console 端口连接设备，返回 (version 输出, 各视图关键字集)。"""
    port = int(target.get("port"))
    sock = None
    version = ""
    views: Dict[str, Set[str]] = {}
    try:
        sock = _console_connect(port)
        _console_send(sock, "screen-length 0 temporary", read_timeout)

        out = _console_send(sock, "display ?", read_timeout)
        views["display"] = parse_help_keywords(out)

        _console_send(sock, "system-view", read_timeout)
        out = _console_send(sock, "?", read_timeout)
        views["system"] = parse_help_keywords(out)

        interface_name = (target.get("interface") or "").strip()
        if not interface_name and views.get("system"):
            brief = _console_send(sock, "display interface brief", read_timeout)
            interface_name = find_first_physical_interface(brief)
        if interface_name:
            _console_send(sock, f"interface {interface_name}", read_timeout)
            out = _console_send(sock, "?", read_timeout)
            views["interface"] = parse_help_keywords(out)
            _console_send(sock, "quit", read_timeout)

        version = _console_send(sock, "display version", read_timeout)
        _console_send(sock, "return", read_timeout)
        _console_send(sock, "quit", read_timeout)
    finally:
        if sock:
            try:
                sock.close()
            except OSError:
                pass
    return version, views


# ---------------------------------------------------------------------------
# 编排：探测 + 解析 + 逐条校验 + 缓存
# ---------------------------------------------------------------------------
def probe_device_commands(target: dict, force: bool = False) -> dict:
    """
    探测某设备支持的命令，返回校验结果。

    trigger 校验对全部视图（display / system / interface）的关键字并集做
    匹配——接口视图下的 vrrp/nat 等协议命令在旧版单次顶层 `?` 中看不到，
    是漏判的主因。

    返回结构：
        {
          "target": {...}, "device_type": str, "model": str,
          "views": {"display": [...], "system": [...], "interface": [...]},
          "keywords": [str], "keyword_count": int,
          "supported": [key...], "unsupported": [key...],
          "errors": [str], "from_cache": bool, "probed_at": float
        }
    """
    key = cache_key(target)
    cached = get_cached_probe(key)
    if cached and not force:
        cached = dict(cached)
        cached["from_cache"] = True
        return cached

    mode = target.get("mode", "ssh")
    version = ""
    views: Dict[str, Set[str]] = {}
    errors: List[str] = []
    try:
        if mode == "console":
            version, views = probe_via_console(target)
        else:
            version, views = probe_via_ssh(target)
    except Exception as e:
        errors.append(f"连接设备失败: {e}")
        logger.warning("命令探测连接失败: %s", e)

    keywords: Set[str] = set()
    for kw_set in views.values():
        keywords |= kw_set
    device_type = detect_device_type(version)

    supported: List[str] = []
    unsupported: List[str] = []
    for ck, entry in COMMAND_CATALOG.items():
        triggers = entry.get("trigger", [])
        trigger_ok = True
        if keywords:  # 仅在成功抓到 ? 输出时才做关键字校验
            trigger_ok = all(t.lower() in keywords for t in triggers)
        type_ok = True
        if device_type != "unknown" and entry.get("device_types"):
            type_ok = device_type in entry.get("device_types", [])
        ok = trigger_ok and type_ok
        (supported if ok else unsupported).append(ck)

    result = {
        "target": target,
        "device_type": device_type,
        "model": _extract_model(version),
        "views": {k: sorted(v) for k, v in views.items()},
        "keywords": sorted(keywords),
        "keyword_count": len(keywords),
        "supported": supported,
        "unsupported": unsupported,
        "errors": errors,
        "from_cache": False,
        "probed_at": time.time(),
    }
    set_cached_probe(key, result)
    return result
