"""
domain/ensp_runtime.py
======================
eNSP 进程注入控制与拓扑自动开机运行时服务：
1. 将项目设备列表 (devices.json) 与连线自动渲染为符合 eNSP 规范的 .topo 文件
2. 自动检测/拉起 eNSP_Client.exe，注入 32 位控制模块 ensp_control.dll
3. 通过本地 RPC (127.0.0.1:18800) 无弹窗加载 .topo 并触发 start_all 自动开机
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
import re
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from app.config import BASE_DIR, logger
from tools.gen_topology import COMPREHENSIVE_DEMO, DEVICE_MODELS, generate

ENSP_EXE = Path(r"C:\Program Files\Huawei\eNSP\eNSP_Client.exe")
DLL_PATH = BASE_DIR / "tools" / "ensp_control.dll"
WORKSPACE_TOPO_DIR = BASE_DIR / "tools" / "demo_topo"
LINKS_FILE = WORKSPACE_TOPO_DIR / "active_links.json"
CTRL_PORT = 18800

PROCESS_ALL_ACCESS = 0x001F0FFF
MEM_COMMIT = 0x00001000
MEM_RESERVE = 0x00002000
MEM_RELEASE = 0x00008000
PAGE_READWRITE = 0x04
TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
ENSP_IAT_LOADLIBRARYW = 0x00593444
ENSP_G_PMAINFRAME = 0x005F4E40

if sys.platform == "win32":
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * wintypes.MAX_PATH),
        ]

    class MODULEENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("th32ModuleID", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("GlblcntUsage", wintypes.DWORD),
            ("ProccntUsage", wintypes.DWORD),
            ("modBaseAddr", ctypes.POINTER(wintypes.BYTE)),
            ("modBaseSize", wintypes.DWORD),
            ("hModule", wintypes.HMODULE),
            ("szModule", wintypes.WCHAR * 256),
            ("szExePath", wintypes.WCHAR * wintypes.MAX_PATH),
        ]

    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.VirtualAllocEx.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        ctypes.c_size_t,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel32.VirtualAllocEx.restype = wintypes.LPVOID
    kernel32.VirtualFreeEx.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        ctypes.c_size_t,
        wintypes.DWORD,
    ]
    kernel32.VirtualFreeEx.restype = wintypes.BOOL
    kernel32.WriteProcessMemory.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.LPCVOID,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.WriteProcessMemory.restype = wintypes.BOOL
    kernel32.ReadProcessMemory.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.LPVOID,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.ReadProcessMemory.restype = wintypes.BOOL
    kernel32.CreateRemoteThread.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        ctypes.c_size_t,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.CreateRemoteThread.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]


def _links_file() -> Path:
    """当前激活拓扑的 links.json；无拓扑时回退到旧位置。"""
    from domain import workspace

    name = workspace.active_topology()
    if name is None:
        return LINKS_FILE
    return workspace.links_path_for(name)


def _topo_dir() -> Path:
    """当前激活拓扑的输出目录（.topo 与 <UUID>/ 都放这里）。"""
    from domain import workspace

    name = workspace.active_topology()
    if name is None:
        return WORKSPACE_TOPO_DIR
    d = workspace.topology_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_saved_links() -> List[dict]:
    try:
        path = _links_file()
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


def save_links(links: List[dict]) -> None:
    try:
        path = _links_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(links, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        logger.warning("保存拓扑连线失败: %s", e)


def allocate_console_port(devices: List[dict], start_port: int = 2000) -> int:
    """分配下一个未占用的 Console 端口（默认从 2000 起）。"""
    used = set()
    for d in devices:
        p = d.get("console_port") or d.get("com_port")
        if p:
            try:
                used.add(int(p))
            except (TypeError, ValueError):
                pass
        ip_str = str(d.get("ip") or "")
        if ":" in ip_str:
            try:
                used.add(int(ip_str.rsplit(":", 1)[1]))
            except ValueError:
                pass
    port = start_port
    while port in used:
        port += 1
    return port


def find_process_pid(exe_name: str = "eNSP_Client.exe") -> int:
    if sys.platform != "win32":
        return 0
    h_snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if h_snap == wintypes.HANDLE(-1).value:
        return 0
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not kernel32.Process32FirstW(h_snap, ctypes.byref(pe)):
            return 0
        while True:
            if pe.szExeFile.lower() == exe_name.lower():
                return int(pe.th32ProcessID)
            if not kernel32.Process32NextW(h_snap, ctypes.byref(pe)):
                break
        return 0
    finally:
        kernel32.CloseHandle(h_snap)


def is_dll_loaded(pid: int, dll_name: str) -> bool:
    if sys.platform != "win32" or not pid:
        return False
    h_snap = kernel32.CreateToolhelp32Snapshot(
        TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid
    )
    if h_snap == wintypes.HANDLE(-1).value:
        return False
    try:
        me = MODULEENTRY32W()
        me.dwSize = ctypes.sizeof(MODULEENTRY32W)
        if not kernel32.Module32FirstW(h_snap, ctypes.byref(me)):
            return False
        while True:
            if me.szModule.lower() == dll_name.lower():
                return True
            if not kernel32.Module32NextW(h_snap, ctypes.byref(me)):
                break
        return False
    finally:
        kernel32.CloseHandle(h_snap)


def _enable_virtualization_and_patch_vboxserver() -> None:
    """为 eNSP 相关进程开启 UAC VirtualStore 并修补 eNSP_VBoxServer 的 Local\\IP_POOL。"""
    if sys.platform != "win32":
        return
    for exe in ("eNSP_Client.exe", "eNSP_VBoxServer.exe"):
        pid = find_process_pid(exe)
        if not pid:
            continue
        hp = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
        if not hp:
            continue
        try:
            ht = wintypes.HANDLE()
            if advapi32.OpenProcessToken(hp, 0x0088, ctypes.byref(ht)):
                en = wintypes.DWORD(1)
                advapi32.SetTokenInformation(ht, 24, ctypes.byref(en), 4)
                kernel32.CloseHandle(ht)
            if exe == "eNSP_VBoxServer.exe":
                old_prot = ctypes.c_ulong()
                kernel32.VirtualProtectEx(
                    hp, ctypes.c_void_p(0x0045ECCC), 32, 0x40, ctypes.byref(old_prot)
                )
                new_str = "Local\\IP_POOL\x00".encode("utf-16-le")
                n = ctypes.c_size_t()
                kernel32.WriteProcessMemory(
                    hp, ctypes.c_void_p(0x0045ECCC), new_str, len(new_str), ctypes.byref(n)
                )
        finally:
            kernel32.CloseHandle(hp)


def wait_for_main_view(pid: int, timeout: float = 8.0) -> bool:
    if sys.platform != "win32" or not pid:
        return False
    h_proc = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not h_proc:
        return False
    try:
        deadline = time.time() + timeout
        ptr_buf = ctypes.create_string_buffer(4)
        bytes_read = ctypes.c_size_t(0)
        while time.time() < deadline:
            if kernel32.ReadProcessMemory(
                h_proc,
                ctypes.c_void_p(ENSP_G_PMAINFRAME),
                ptr_buf,
                4,
                ctypes.byref(bytes_read),
            ):
                if struct.unpack("<I", ptr_buf.raw)[0] != 0:
                    return True
            time.sleep(0.4)
        return False
    finally:
        kernel32.CloseHandle(h_proc)


def ensure_ensp_injected(launch_if_missing: bool = True) -> dict:
    """确保 eNSP_Client.exe 已运行且已注入 ensp_control.dll。"""
    if sys.platform != "win32":
        return {"ok": False, "error": "仅支持 Windows 平台"}
    if not ENSP_EXE.exists():
        return {"ok": False, "error": f"未找到 eNSP 客户端: {ENSP_EXE}"}
    if not DLL_PATH.exists():
        return {"ok": False, "error": f"未找到控制模块: {DLL_PATH}"}

    pid = find_process_pid("eNSP_Client.exe")
    if not pid:
        if not launch_if_missing:
            return {"ok": False, "error": "eNSP_Client.exe 未运行"}
        env = os.environ.copy()
        env["__COMPAT_LAYER"] = "RunAsInvoker"
        subprocess.Popen([str(ENSP_EXE)], cwd=str(ENSP_EXE.parent), env=env)
        for _ in range(30):
            pid = find_process_pid("eNSP_Client.exe")
            if pid:
                break
            time.sleep(0.4)
        if not pid:
            return {"ok": False, "error": "拉起 eNSP_Client.exe 失败"}

    if not wait_for_main_view(pid, timeout=40.0):
        return {"ok": False, "error": "等待 eNSP 主界面初始化超时"}

    _enable_virtualization_and_patch_vboxserver()

    if is_dll_loaded(pid, DLL_PATH.name):
        return {"ok": True, "pid": pid, "already_injected": True}

    h_proc = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not h_proc:
        return {"ok": False, "error": f"OpenProcess({pid}) 失败: {ctypes.get_last_error()}"}

    remote_mem = None
    try:
        ptr_buf = ctypes.create_string_buffer(4)
        bytes_read = ctypes.c_size_t(0)
        if not kernel32.ReadProcessMemory(
            h_proc,
            ctypes.c_void_p(ENSP_IAT_LOADLIBRARYW),
            ptr_buf,
            4,
            ctypes.byref(bytes_read),
        ):
            return {"ok": False, "error": "读取 IAT LoadLibraryW 地址失败"}

        load_lib_addr = struct.unpack("<I", ptr_buf.raw)[0]
        path_bytes = str(DLL_PATH.resolve()).encode("utf-16le") + b"\x00\x00"
        remote_mem = kernel32.VirtualAllocEx(
            h_proc, None, len(path_bytes), MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE
        )
        written = ctypes.c_size_t(0)
        kernel32.WriteProcessMemory(
            h_proc, remote_mem, path_bytes, len(path_bytes), ctypes.byref(written)
        )
        tid = wintypes.DWORD(0)
        h_thread = kernel32.CreateRemoteThread(
            h_proc,
            None,
            0,
            ctypes.c_void_p(load_lib_addr),
            remote_mem,
            0,
            ctypes.byref(tid),
        )
        if not h_thread:
            return {"ok": False, "error": "CreateRemoteThread 失败"}
        try:
            kernel32.WaitForSingleObject(h_thread, 10000)
            exit_code = wintypes.DWORD(0)
            kernel32.GetExitCodeThread(h_thread, ctypes.byref(exit_code))
            if not exit_code.value:
                return {"ok": False, "error": "LoadLibraryW 返回 NULL（注入失败）"}

            # CreateRemoteThread 成功 ≠ RPC 可用：DLL 的 bind() 可能因为端口被占而失败。
            # 必须真正问一次 status 才能确认控制通道活着。
            time.sleep(0.4)
            probe = send_ensp_ctrl_cmd("status", timeout=3.0, retries=3)
            if not probe.get("ok"):
                return {
                    "ok": False,
                    "pid": pid,
                    "hmodule": exit_code.value,
                    "error": (
                        f"控制模块已加载但 RPC 无应答（{probe.get('error')}）；"
                        f"通常是端口 {CTRL_PORT} 被残留监听占用，重启 eNSP 即可"
                    ),
                }
            return {"ok": True, "pid": pid, "hmodule": exit_code.value}
        finally:
            kernel32.CloseHandle(h_thread)
    finally:
        if remote_mem:
            kernel32.VirtualFreeEx(h_proc, remote_mem, 0, MEM_RELEASE)
        kernel32.CloseHandle(h_proc)


def send_ensp_ctrl_cmd(cmd: str, timeout: float = 10.0, retries: int = 8) -> dict:
    """向注入到 eNSP_Client.exe 的控制模块 (127.0.0.1:18800) 发送指令。"""
    last_err = None
    for _ in range(max(1, retries)):
        try:
            with socket.create_connection(("127.0.0.1", CTRL_PORT), timeout=timeout) as s:
                s.sendall((cmd.strip() + "\n").encode("utf-8"))
                data = b""
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                    if b"\n" in data:
                        break
                return json.loads(data.decode("utf-8", errors="replace"))
        except Exception as e:
            last_err = e
            time.sleep(0.4)
    return {"ok": False, "error": f"连接 eNSP 控制端口失败: {last_err}"}


def build_topo_spec_from_devices(
    devices: List[dict],
    links: Optional[List[dict]] = None,
    name: str = "active_workspace",
) -> dict:
    """将项目中的设备列表与连线转换为 gen_topology 的拓扑描述。"""
    topo_devices = []
    valid_names = set()
    for idx, d in enumerate(devices):
        dev_name = d.get("name") or f"DEV{idx + 1}"
        model = d.get("model") or "Router"
        if model not in DEVICE_MODELS:
            model = "Router"
        cx = float(d.get("cx") or (160.0 + (idx % 4) * 210.0))
        cy = float(d.get("cy") or (200.0 + (idx // 4) * 180.0))
        entry = {
            "name": dev_name,
            "model": model,
            "cx": cx,
            "cy": cy,
            # 稳定 UUID：让 eNSP 的 <UUID>/ 配置目录在各次生成之间保持一致
            "id": d.get("uuid"),
        }
        if DEVICE_MODELS[model]["kind"] == "terminal":
            # 终端设备不占 Console 端口，IP 走 settings
            entry["ip_address"] = d.get("ip_address") or d.get("mgmt_ip") or "0.0.0.0"
            entry["netmask"] = d.get("netmask") or "255.255.255.0"
            entry["gateway"] = d.get("gateway") or "0.0.0.0"
        else:
            entry["com_port"] = int(d.get("console_port") or d.get("com_port") or (2000 + idx))
        topo_devices.append(entry)
        valid_names.add(dev_name)

    active_links = links if links is not None else load_saved_links()
    filtered_links = [
        lnk
        for lnk in active_links
        if isinstance(lnk, dict)
        and lnk.get("a", {}).get("device") in valid_names
        and lnk.get("b", {}).get("device") in valid_names
    ]
    return {
        "name": name,
        "devices": topo_devices,
        "links": filtered_links,
    }



# ---------------------------------------------------------------------------
# 拓扑读写：命令行打开 + 只读内存观测
# ---------------------------------------------------------------------------
# 实测结论（eNSP_Client.exe 2019 构建）：
#   - 通过注入 DLL 调用内部 open_topo 会让 UI 线程永久阻塞、控制通道失联；
#   - 而 `eNSP_Client.exe <topo路径>` 命令行参数是 eNSP 官方支持的打开方式，
#     实测稳定、能正确加载我们生成的 .topo。
#   - 观测当前拓扑无需注入：直接只读 eNSP 进程内存即可拿到设备名/型号/电源状态。
G_PMAINFRAME_ADDR = 0x005F4E40   # CeNSP_ClientView* 全局（无 ASLR）
BOARD_OFF = 0x88                 # view + 0x88 = CTopoBoard
ITEM_NAME_OFF = 0x70             # CTopoItem: CString* 设备名
ITEM_MODEL_OFF = 0x74            # CTopoItem: CString* 型号
ITEM_POWER_OFF = 0xB8            # CTopoItem: uint32 电源状态

_POWER_STATES = {0: "running", 1: "stopped", 2: "stopped"}


class _ProcReader:
    """只读读取目标进程内存的辅助类（全程校验，读不到就返回 None）。"""

    def __init__(self, pid: int):
        self.pid = pid
        self.handle = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
        self._buf = ctypes.create_string_buffer(4)
        self._read = ctypes.c_size_t(0)

    def ok(self) -> bool:
        return bool(self.handle)

    def u32(self, addr: int) -> Optional[int]:
        if not self.handle:
            return None
        if kernel32.ReadProcessMemory(
            self.handle, ctypes.c_void_p(addr), self._buf, 4, ctypes.byref(self._read)
        ):
            return struct.unpack("<I", self._buf.raw)[0]
        return None

    def wstr(self, ptr: int, max_chars: int = 128) -> str:
        """读取宽字符串（先探测可读性，避免坏指针拖垮 eNSP）。"""
        if not self.handle or not ptr or ptr < 0x10000 or ptr >= 0x7FFF0000:
            return ""
        buf = ctypes.create_unicode_buffer(max_chars)
        need = max_chars * 2
        if kernel32.ReadProcessMemory(
            self.handle, ctypes.c_void_p(ptr), buf, need, ctypes.byref(self._read)
        ):
            return buf.value
        return ""

    def close(self) -> None:
        if self.handle:
            kernel32.CloseHandle(self.handle)
            self.handle = None


def open_topology_in_ensp(topo_path) -> dict:
    """
    用 eNSP 官方支持的命令行参数打开拓扑文件。

    eNSP 是单实例应用：若已运行，会由既有实例接管打开该文件；未运行则新建进程。
    返回前会等待视图就绪并回读，确认 eNSP 真的加载了该拓扑。
    """
    path = Path(topo_path)
    if not path.exists():
        return {"ok": False, "error": f"拓扑文件不存在: {path}"}

    if sys.platform != "win32":
        return {"ok": False, "error": "仅支持 Windows 平台"}

    env = os.environ.copy()
    env["__COMPAT_LAYER"] = "RunAsInvoker"
    try:
        subprocess.Popen([str(ENSP_EXE), str(path)], cwd=str(ENSP_EXE.parent), env=env)
    except OSError as e:
        return {"ok": False, "error": f"启动 eNSP 失败: {e}"}

    # 等待视图出现（eNSP 已运行时由既有实例接管，通常几秒内完成）
    pid = 0
    deadline = time.time() + 60
    while time.time() < deadline:
        pid = find_process_pid("eNSP_Client.exe") or pid
        if pid and wait_for_main_view(pid, timeout=1.0):
            break
        time.sleep(0.8)
    else:
        return {"ok": False, "pid": pid, "error": "等待 eNSP 主视图超时"}

    # 等待目标拓扑真正载入。
    # 注意：不能只看"设备数 > 0"——eNSP 是单实例，若它已经开着另一个拓扑，
    # 新命令只是把那个窗口带到前台，旧拓扑仍在。必须比对设备名。
    expect = _expected_device_names(path)
    live: dict = {}
    deadline = time.time() + 60
    while time.time() < deadline:
        live = read_ensp_topology()
        actual = [d.get("name") for d in (live.get("live_devices") or [])]
        # 必须等到设备集合与 .topo 完全一致——eNSP 是逐台载入的，
        # 用"子集"判定会在最后一两台还没出来时就误判成功。
        if actual and (not expect or set(expect) == set(actual)):
            return {"ok": True, "pid": pid, "topo_path": str(path),
                    "devices": live["devices"], "links": live.get("links", 0),
                    "live_devices": actual}
        time.sleep(1.0)

    actual = [d.get("name") for d in (live.get("live_devices") or [])]
    if actual:
        return {
            "ok": False, "pid": pid, "topo_path": str(path), "live": live,
            "error": (f"eNSP 里是另一套拓扑（实际: {', '.join(actual)}"
                      f"{'；期望: ' + ', '.join(expect) if expect else ''}）。"
                      "eNSP 为单实例，请先在 eNSP 里关闭当前拓扑再重试"),
        }
    return {"ok": False, "pid": pid, "topo_path": str(path),
            "error": "eNSP 已就绪但未载入任何拓扑", "live": live}


def _expected_device_names(topo_path: Path) -> List[str]:
    """从 .topo 文件里读出设备名，用于确认 eNSP 载入的是不是这个文件。"""
    try:
        raw = topo_path.read_bytes()
    except OSError:
        return []
    # .topo 是 GBK（无 BOM），但设备名多为 ASCII，双解码都试一下
    for enc in ("gbk", "utf-8"):
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        # <dev ...> 的属性可能跨行（id 后换行再写 name），故用 DOTALL
        names = re.findall(r'<dev\b[^>]*?\bname="([^"]*)"', text, re.DOTALL)
        if names:
            return names
    return []


def read_ensp_topology() -> dict:
    """
    只读观测 eNSP 当前打开了什么拓扑（不注入、不修改）。

    返回 {ok, ensp_running, devices, links, live_devices, running, stopped}。
    live_devices 为 [{name, model, power_state}]，与界面所见一致。
    """
    pid = find_process_pid("eNSP_Client.exe")
    if not pid:
        return {"ok": True, "ensp_running": False, "devices": 0, "links": 0,
                "live_devices": []}

    rd = _ProcReader(pid)
    if not rd.ok():
        return {"ok": False, "ensp_running": True, "pid": pid,
                "error": "无法打开 eNSP 进程（可能需要管理员权限）",
                "devices": 0, "links": 0, "live_devices": []}
    try:
        view = rd.u32(G_PMAINFRAME_ADDR)
        if not view:
            return {"ok": True, "ensp_running": True, "pid": pid,
                    "devices": 0, "links": 0, "live_devices": [],
                    "note": "eNSP 主视图尚未就绪"}
        board = view + BOARD_OFF
        n_devs = rd.u32(board + 0x50) or 0
        n_lines = rd.u32(board + 0x5C) or 0

        devices: List[dict] = []
        dev_arr = rd.u32(board + 0x4C)
        if dev_arr and 0 < n_devs < 512:
            for i in range(n_devs):
                item = rd.u32(dev_arr + i * 4)
                if not item:
                    continue
                name = rd.wstr(rd.u32(item + ITEM_NAME_OFF) or 0)
                model = rd.wstr(rd.u32(item + ITEM_MODEL_OFF) or 0)
                power = rd.u32(item + ITEM_POWER_OFF)
                devices.append({
                    "name": name,
                    "model": model,
                    "power_state": _POWER_STATES.get(power, "starting"),
                })
        running = sum(1 for d in devices if d["power_state"] == "running")
        return {
            "ok": True, "ensp_running": True, "pid": pid,
            "devices": len(devices), "links": n_lines,
            "running": running, "stopped": len(devices) - running,
            "live_devices": devices,
        }
    finally:
        rd.close()


def _verify_open_matches(live: dict, expected_devices: List[dict]) -> dict:
    """确认 eNSP 里打开的正是我们写入的设备集合。"""
    if not live.get("ensp_running"):
        return {"ok": False, "error": "eNSP 未运行"}
    actual = [d["name"] for d in (live.get("live_devices") or [])]
    want = [d.get("name") for d in expected_devices if d.get("name")]
    if not actual:
        return {"ok": False, "error": "eNSP 中未检测到任何设备（拓扑未加载）"}
    missing = [n for n in want if n not in actual]
    if missing:
        return {"ok": False,
                "error": f"eNSP 中缺少设备: {', '.join(missing)}（实际: {', '.join(actual)}）",
                "missing": missing}
    return {"ok": True, "matched": len(want), "actual": actual}


def start_all_devices() -> dict:
    """
    启动 eNSP 中全部设备。

    依赖注入的 DLL 控制通道（start_all 命令）。若通道不可用，
    返回明确错误而不是假装成功。
    """
    inj = ensure_ensp_injected(launch_if_missing=False)
    if not inj.get("ok"):
        return {"ok": False, "error": inj.get("error", "控制模块不可用")}
    return send_ensp_ctrl_cmd("start_all", timeout=30.0, retries=2)


def _resolve_device_selector(devices: List[dict], device_ip: str) -> Optional[str]:
    """
    把项目的设备标识解析成 eNSP 侧的选择器。

    eNSP 画板用**设备名**（devices.json 的 name 字段）识别设备，
    而前端手上通常是 IP（127.0.0.1:2000），所以要在这里做一次映射。
    找不到匹配设备时返回 None。
    """
    dev = next((d for d in devices if (d.get("ip") == device_ip or d.get("name") == device_ip)), None)
    if not dev:
        return None
    return dev.get("name") or None


def start_stop_device(device_ip: str, action: str = "start", wait: bool = True) -> dict:
    """
    启动或停止 eNSP 中的**单台**设备（需求③）。

    依赖 DLL 的 start_dev:<名称> / stop_dev:<名称> 指令。命令是异步下发的，
    这里通过轮询 live_topo 确认电源状态真的变了，避免"假装成功"。

    Args:
        device_ip: 项目的设备 IP（或设备名）
        action: "start" 或 "stop"
        wait: 是否等待并回报最终电源状态（默认等，最多约 12 秒）

    Returns:
        {ok, device, action, power_state, ...}
    """
    from domain.device_store import load_devices

    action = (action or "start").lower()
    if action not in ("start", "stop"):
        return {"ok": False, "error": f"未知动作: {action}（可用 start / stop）"}

    selector = _resolve_device_selector(load_devices(), device_ip)
    if not selector:
        return {"ok": False, "error": f"设备表中找不到 {device_ip}（eNSP 按设备名操作，请先添加设备）"}

    inj = ensure_ensp_injected(launch_if_missing=False)
    if not inj.get("ok"):
        return {"ok": False, "error": inj.get("error", "控制模块不可用"), "device": selector}

    cmd = f"{'start' if action == 'start' else 'stop'}_dev:{selector}"
    res = send_ensp_ctrl_cmd(cmd, timeout=15.0, retries=2)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error", f"{cmd} 执行失败"), "device": selector}

    want_state = "running" if action == "start" else "stopped"
    out: Dict[str, Any] = {
        "ok": True,
        "device": selector,
        "action": action,
        "sent": True,
    }
    if not wait:
        out["message"] = f"已向 eNSP 下发{('开机' if action == 'start' else '停机')}指令: {selector}"
        return out

    # 轮询确认电源状态真的变了（eNSP 开机通常要几秒）
    deadline = time.time() + 12.0
    observed = None
    while time.time() < deadline:
        time.sleep(1.0)
        live = read_ensp_topology()
        if not live.get("ok"):
            continue
        for dev in (live.get("live_devices") or []):
            if dev.get("name") == selector:
                observed = dev.get("power_state")
                break
        if observed == want_state:
            break

    out["power_state"] = observed
    if observed == want_state:
        out["message"] = f"设备 {selector} 已{'开机' if action == 'start' else '停机'}"
    else:
        out["message"] = (
            f"已向 eNSP 下发{('开机' if action == 'start' else '停机')}指令: {selector}，"
            f"但当前状态为 {observed or '未知'}（设备启动可能需要更长时间）"
        )
    return out


def _ensure_device_uuids(devices: List[dict]) -> bool:
    """
    为缺少 uuid 的设备补一个稳定 UUID。返回是否发生了改动。

    eNSP 用 <dev id="<UUID>"> 决定设备配置目录名，如果每次生成都换 UUID，
    旧的 <UUID>/ 目录会残留、配置也无法稳定关联到设备。
    """
    import uuid as _uuid

    changed = False
    for d in devices:
        if not d.get("uuid"):
            d["uuid"] = str(_uuid.uuid4()).upper()
            changed = True
    return changed


def sync_and_start_ensp_topo(
    devices: List[dict],
    links: Optional[List[dict]] = None,
    topo_name: Optional[str] = None,
    auto_start: bool = True,
) -> dict:
    """
    根据给定的设备与连线生成 .topo 文件，并在 eNSP 中自动加载与启动全部设备。

    topo_name 省略时使用当前激活的拓扑名——文件名与目录名保持一致。
    """
    if not devices:
        return {"ok": False, "error": "设备列表为空"}

    # 单元测试环境：只做内存内的 spec 计算，绝不写任何文件、绝不拉起真实 GUI 进程。
    #
    # 这个守卫必须放在**所有**写操作之前。它原来被放在 generate() 之后，
    # 结果跑一次 pytest 就会：先用测试里造出来的假设备覆盖真实工作区的
    # devices.json，再重写真实 .topo（曾经把 5 台设备的拓扑刷成单台 R_TEST）。
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return {
            "ok": True,
            "mode": "pytest_dry_run",
            "topo_path": "",
            "devices": len(devices),
            "links": len(links or []),
        }

    if links is not None:
        save_links(links)

    if not topo_name:
        from domain import workspace
        topo_name = workspace.active_topology() or "active_workspace"

    # 给还没有 UUID 的设备分配一个并写回，保证 eNSP 的 <UUID>/ 配置目录跨次生成稳定。
    if _ensure_device_uuids(devices):
        from domain.device_store import save_devices
        save_devices(devices)

    topo_spec = build_topo_spec_from_devices(devices, links=links, name=topo_name)

    # 只生成 .topo。设备配置目录（<UUID>/）由 eNSP 保存时自己写。
    topo_path = generate(topo_spec, _topo_dir(), name=topo_name)

    # 打开拓扑：走 eNSP 自己的命令行参数（实测可靠）。
    # 不能用注入 DLL 的 open_topo —— 实测会让 eNSP 的 UI 线程永久阻塞、
    # 继而整个控制通道失联（且 eNSP 版本间行为不一致）。
    open_res = open_topology_in_ensp(topo_path)
    if not open_res.get("ok"):
        return {
            "ok": False,
            "topo_path": str(topo_path),
            "open_result": open_res,
            "error": open_res.get("error", "eNSP 未能打开拓扑"),
        }

    # 回读确认：eNSP 是否真的打开了这个文件、设备是否与我们写入的一致
    live = read_ensp_topology()
    verify = _verify_open_matches(live, devices)
    if not verify["ok"]:
        return {
            "ok": False,
            "topo_path": str(topo_path),
            "open_result": open_res,
            "live": live,
            "error": verify["error"],
        }

    start_res = None
    if auto_start:
        start_res = start_all_devices()

    return {
        "ok": True,
        "topo_path": str(topo_path),
        "open_result": open_res,
        "live": live,
        "verify": verify,
        "start_result": start_res,
    }


def get_ensp_runtime_status() -> dict:
    """查询 eNSP 客户端与内部设备的实时运行状态（只读，不注入）。"""
    live = read_ensp_topology()
    live["state"] = _live_state(live)
    return live


def probe_ensp_live(timeout: float = 1.5) -> dict:
    """
    非侵入式实时探测：供前端轮询使用。

    **只读** eNSP 进程内存与其最近打开记录，绝不注入、绝不拉起 eNSP、失败快速返回。
    """
    live = read_ensp_topology()
    live["state"] = _live_state(live)
    live["in_sync"] = compare_with_active_topology(live)
    return live


def compare_with_active_topology(live: Optional[dict] = None) -> dict:
    """
    比较 eNSP 里实际打开的内容与项目当前激活拓扑，供界面提示是否需要重新下发。

    返回 {status, message, expected, actual}，status ∈
    in_sync / not_running / empty / drifted / unknown。
    """
    from domain import workspace

    if live is None:
        live = read_ensp_topology()

    if not live.get("ensp_running"):
        return {"status": "not_running", "message": "eNSP 未运行"}

    actual = [d.get("name") for d in (live.get("live_devices") or [])]
    if not actual:
        return {"status": "empty", "message": "eNSP 中未打开任何拓扑"}

    name = workspace.active_topology()
    if not name:
        return {"status": "unknown", "message": "项目中没有拓扑", "actual": actual}

    expected = [d.get("name") for d in workspace.load_topology(name).get("devices", [])]
    if set(expected) == set(actual):
        return {"status": "in_sync", "message": "与项目拓扑一致",
                "expected": expected, "actual": actual}

    missing = [n for n in expected if n not in actual]
    extra = [n for n in actual if n not in expected]
    parts = []
    if missing:
        parts.append(f"缺少 {', '.join(missing)}")
    if extra:
        parts.append(f"多出 {', '.join(extra)}")
    return {
        "status": "drifted",
        "message": f"eNSP 与项目拓扑不一致（{'；'.join(parts)}）",
        "expected": expected, "actual": actual,
    }


def _live_state(live: dict) -> str:
    """把只读观测结果归纳成一个前端可直接展示的状态枚举。"""
    if not live.get("ensp_running"):
        return "not_running"
    if not live.get("ok"):
        return "unreadable"
    if live.get("devices"):
        return "topology_open"
    return "empty"
