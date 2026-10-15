"""
command_probe.py - 设备 ? 探测引导（自动发现设备支持的命令）
=========================================================
连上设备后，逐视图抓取 VRP 的帮助输出（display ? / 系统视图 ? /
接口视图 ?），与命令目录 COMMAND_CATALOG 中每条的 trigger 关键字比对，
逐条标注该设备「是否支持此指令」。解决「如何知道设备支持什么」的问题。

设计要点（延迟调到无感知）：
- 通道：本地 Console（raw socket 连 127.0.0.1:<console_port>）。
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
CONN_TIMEOUT = 6
CONN_READ_TIMEOUT = 6
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
# target 归一化
# ---------------------------------------------------------------------------
def _normalize_target(target: dict) -> dict:
    """
    统一 target 的键名。

    历史遗留问题：路由层与设备表用的是 console_port，而本模块按 port 读取，
    int(None) 直接抛 TypeError，被上层 catch 成"连接失败"，表现为 ? 探测静默
    返回 0 个关键字。这里双向补齐，两种写法都能用。
    """
    t = dict(target or {})
    port = t.get("port") or t.get("console_port")
    if port not in (None, "", 0):
        try:
            port_i = int(port)
        except (TypeError, ValueError):
            port_i = 0
        t["port"] = port_i
        t["console_port"] = port_i
        t.setdefault("mode", "console")
    elif t.get("ip"):
        t.setdefault("mode", "ssh")
    return t


# ---------------------------------------------------------------------------
# Console 通道探测（零 IP，连 127.0.0.1:port）
# ---------------------------------------------------------------------------
def open_console_session(port: int):
    """
    打开一个**已就绪**的 Console 会话。

    必须复用 domain.console_connection.ConsoleConnection：它处理了 eNSP 控制台的
    唤醒（\\x03）、用户名/密码认证、退回用户视图、关闭分屏与提示符识别。
    裸 socket 直连会停在登录提示上，首条命令被吞，? 探测拿不到任何输出。
    """
    from domain.console_connection import ConsoleConnection

    return ConsoleConnection(int(port), timeout=CONSOLE_TIMEOUT)


def _console_connect(port: int, timeout: float = 2.0) -> socket.socket:
    """连接 eNSP 本地 Console 端口（保留供兼容调用）。"""
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    sock.settimeout(timeout)
    # 唤醒：eNSP 控制台刚连上时需要一个空行才吐出稳定提示符
    try:
        sock.sendall(b"\x03\r")
        time.sleep(0.2)
        _console_recv_all(sock, max_wait=0.8)
    except OSError:
        pass
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
    """发送命令并读回输出；带回显校验，回显被 ANSI 码吃掉时自动重发一次。

    注意：VRP/eNSP 控制台只认裸 \\r。发 \\r\\n 会让首条命令被吞，
    后续命令全部落到错误视图里返回 "Unrecognized command"。
    """
    sock.sendall((command + "\r").encode("ascii", errors="ignore"))
    time.sleep(0.2)
    out = _console_recv_all(sock, max_wait=read_timeout)

    # 回显校验：清理 ANSI 后应能看到所发命令（VRP 会回显输入行）。
    # 校验失败说明回显被光标控制码截断（如 "isplay vlan"），重发一次。
    cleaned = strip_ansi(out)
    if command not in cleaned.replace("\r", ""):
        time.sleep(0.3)
        sock.sendall((command + "\r").encode("ascii", errors="ignore"))
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


_VRP_PROMPT_RE = re.compile(r"^[<\[][^\s<>\[\]]+[>\]]\s*$")


def read_until_prompt(conn, command: str, read_timeout: float = 6.0, rounds: int = 4) -> str:
    """
    发送命令并读到提示符出现为止。

    eNSP 控制台的输出经常分成多段、间隔超过 `send_command` 内部 0.25s 的空闲
    判定，导致长输出被截断——`display version` 实测只回来前三行（138 字符），
    恰好丢掉带型号的那一行，于是设备类型永远判成 unknown。
    这里用"没看到提示符就再读一段"的方式补齐。
    """
    out = conn.send_command_timing(command, read_timeout=read_timeout)
    for _ in range(rounds):
        lines = [ln for ln in (out or "").splitlines() if ln.strip()]
        if lines and _VRP_PROMPT_RE.match(lines[-1].strip()):
            break
        more = conn.send_command_timing("", read_timeout=2.5)
        if not more or more.strip() == (out or "").strip():
            break
        out = (out or "") + "\n" + more
    return out or ""


def _enter_system_view(conn, read_timeout: float, attempts: int = 3) -> bool:
    """进入系统视图。

    eNSP 控制台刚连上时首条命令偶发被吞（连 `system-view` 也会被吞），
    表现为后续命令全落在用户视图、返回 Unrecognized command。
    这里按提示符判断是否真的进了系统视图，没进就重试。
    """
    for _ in range(attempts):
        out = conn.send_command_timing("system-view", read_timeout=read_timeout)
        tail = [ln for ln in strip_ansi(out).splitlines() if ln.strip()]
        if tail and tail[-1].strip().startswith("["):
            return True
        time.sleep(0.3)
    return False


def probe_via_console(target: dict, timeout: int = CONSOLE_TIMEOUT,
                      read_timeout: float = CONSOLE_READ_TIMEOUT
                      ) -> Tuple[str, Dict[str, Set[str]]]:
    """通过 eNSP 本地 Console 端口连接设备，返回 (version 输出, 各视图关键字集)。"""
    t = _normalize_target(target)
    port = t.get("port")
    if not port:
        raise ValueError("target 缺少 console_port / port，无法走 Console 通道")

    conn = open_console_session(port)
    version = ""
    views: Dict[str, Set[str]] = {}
    try:
        conn.send_command("screen-length 0 temporary", read_timeout=read_timeout)

        out = conn.send_command("display ?", read_timeout=read_timeout)
        views["display"] = parse_help_keywords(strip_ansi(out))

        if _enter_system_view(conn, read_timeout):
            out = conn.send_command("?", read_timeout=read_timeout)
            views["system"] = parse_help_keywords(strip_ansi(out))

            interface_name = (t.get("interface") or "").strip()
            if not interface_name and views.get("system"):
                brief = conn.send_command("display interface brief", read_timeout=read_timeout)
                interface_name = find_first_physical_interface(brief)
            if interface_name:
                conn.send_command(f"interface {interface_name}", read_timeout=read_timeout)
                out = conn.send_command("?", read_timeout=read_timeout)
                views["interface"] = parse_help_keywords(strip_ansi(out))
                conn.send_command("quit", read_timeout=read_timeout)
            conn.send_command("return", read_timeout=read_timeout)
        else:
            logger.warning("端口 %s 未能进入系统视图，系统/接口视图关键字将缺失", port)

        version = read_until_prompt(conn, "display version", read_timeout=read_timeout)
        # display version 输出长，eNSP 上容易被分段截断而丢掉带型号的那一行；
        # display device 输出短，作为型号来源的回退。
        if not _extract_model(version):
            device_out = read_until_prompt(conn, "display device", read_timeout=read_timeout)
            if device_out:
                version = version + "\n" + device_out
    finally:
        try:
            conn.disconnect()
        except Exception:
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
    target = _normalize_target(target)
    key = cache_key(target)
    cached = get_cached_probe(key)
    if cached and not force:
        cached = dict(cached)
        cached["from_cache"] = True
        return cached

    version = ""
    views: Dict[str, Set[str]] = {}
    errors: List[str] = []
    try:
        version, views = probe_via_console(target)
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
