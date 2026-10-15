"""
综合案例全链路验证脚本：
覆盖 路由器(R1) + 防火墙(FW1) + 核心交换机(SW1) + 无线控制器(AC1) + 无线AP(AP1)

执行链路：
1. 生成符合 eNSP 底层规范的 comprehensive_campus.topo
2. 若 eNSP_Client.exe 未运行则拉起，并将 32 位 ensp_control.dll 注入到 eNSP_Client.exe
3. 通过注入 DLL 的本地控制端口 (127.0.0.1:18800) 发送 `open_topo` 与 `start_all`
4. 并发等待各设备 Console 端口 (127.0.0.1:2000..2004) 完成系统引导
5. 直接通过 ConsoleConnection (免 SSH、免 Cloud、免宿主机网卡) 并发下发业务配置与验证
"""

from __future__ import annotations

import concurrent.futures
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from domain.console_connection import ConsoleConnection, wait_for_console_ready
from tools.gen_topology import COMPREHENSIVE_DEMO, generate

ENSP_EXE = Path(r"C:\Program Files\Huawei\eNSP\eNSP_Client.exe")
DLL_PATH = Path(__file__).resolve().parent / "ensp_control.dll"
OUT_DIR = Path(__file__).resolve().parent / "demo_topo"
CTRL_PORT = 18800

# Windows API 常量与 eNSP 固定 IAT 槽位
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

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


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


def find_process_pid(exe_name: str = "eNSP_Client.exe") -> int:
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
                return pe.th32ProcessID
            if not kernel32.Process32NextW(h_snap, ctypes.byref(pe)):
                break
        return 0
    finally:
        kernel32.CloseHandle(h_snap)


def is_dll_loaded(pid: int, dll_name: str) -> bool:
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


def wait_for_main_view(pid: int, timeout: float = 45.0) -> bool:
    """等待 eNSP_Client.exe 初始化 g_pMainFrame (0x005F4E40) 非空。"""
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
                p_view = struct.unpack("<I", ptr_buf.raw)[0]
                if p_view != 0:
                    return True
            time.sleep(0.5)
        return False
    finally:
        kernel32.CloseHandle(h_proc)


def inject_dll(pid: int, dll_path: Path) -> bool:
    abs_dll = str(dll_path.resolve())
    dll_name = dll_path.name
    if is_dll_loaded(pid, dll_name):
        print(f"  [OK] {dll_name} 已存在于进程 PID={pid}")
        return True

    h_proc = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not h_proc:
        raise OSError(f"OpenProcess({pid}) 失败: {ctypes.get_last_error()}")

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
            raise OSError(f"读取 IAT LoadLibraryW 失败: {ctypes.get_last_error()}")

        load_lib_addr = struct.unpack("<I", ptr_buf.raw)[0]
        path_bytes = abs_dll.encode("utf-16le") + b"\x00\x00"
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
            raise OSError(f"CreateRemoteThread 失败: {ctypes.get_last_error()}")
        try:
            kernel32.WaitForSingleObject(h_thread, 10000)
            exit_code = wintypes.DWORD(0)
            kernel32.GetExitCodeThread(h_thread, ctypes.byref(exit_code))
            print(
                f"  [OK] 已注入 {dll_name} -> PID={pid} (HMODULE=0x{exit_code.value:08X})"
            )
            return exit_code.value != 0
        finally:
            kernel32.CloseHandle(h_thread)
    finally:
        if remote_mem:
            kernel32.VirtualFreeEx(h_proc, remote_mem, 0, MEM_RELEASE)
        kernel32.CloseHandle(h_proc)


def send_ctrl_cmd(cmd: str, timeout: float = 15.0) -> dict:
    for attempt in range(10):
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
        except OSError:
            if attempt == 9:
                raise
            time.sleep(0.5)
    return {"ok": False}


# ---------------------------------------------------------------------------
# 各网元免 SSH 配置方案（覆盖路由器、防火墙、核心交换机、无线控制器、无线接入交换机）
# ---------------------------------------------------------------------------
DEVICE_CONFIG_PLAN = {
    "R1": {
        "port": 2000,
        "model": "Router",
        "role": "出口路由器",
        "commands": [
            "sysname R1",
            "interface GigabitEthernet0/0/0",
            "ip address 10.0.12.1 255.255.255.0",
            "ospf network-type p2p",
            "undo shutdown",
            "quit",
            "interface LoopBack0",
            "ip address 1.1.1.1 255.255.255.255",
            "quit",
            "ospf 1 router-id 1.1.1.1",
            "area 0",
            "network 10.0.12.0 0.0.0.255",
            "network 1.1.1.1 0.0.0.0",
            "quit",
            "quit",
        ],
        "verify": [
            "display ip interface brief",
            "display ospf peer brief",
        ],
    },
    "FW1": {
        "port": 2001,
        "model": "USG5500",
        "role": "边界防火墙",
        "commands": [
            "sysname FW1",
            "interface GigabitEthernet0/0/1",
            "ip address 10.0.12.2 255.255.255.0",
            "ospf network-type p2p",
            "quit",
            "interface GigabitEthernet0/0/2",
            "ip address 10.0.23.1 255.255.255.0",
            "ospf network-type p2p",
            "quit",
            "firewall zone untrust",
            "add interface GigabitEthernet0/0/1",
            "quit",
            "firewall zone trust",
            "add interface GigabitEthernet0/0/2",
            "quit",
            "firewall packet-filter default permit all",
            "ospf 1 router-id 2.2.2.2",
            "area 0",
            "network 10.0.12.0 0.0.0.255",
            "network 10.0.23.0 0.0.0.255",
            "quit",
            "quit",
        ],
        "verify": [
            "display ip interface brief",
            "display zone",
        ],
    },
    "SW1": {
        "port": 2002,
        "model": "S5700",
        "role": "核心三层交换机",
        "commands": [
            "sysname SW1",
            "vlan batch 10 100 200",
            "dhcp enable",
            "interface Vlanif10",
            "ip address 10.0.23.2 255.255.255.0",
            "ospf network-type p2p",
            "quit",
            "interface Vlanif100",
            "ip address 192.168.100.254 255.255.255.0",
            "ospf network-type p2p",
            "dhcp select interface",
            "quit",
            "interface Vlanif200",
            "ip address 192.168.200.254 255.255.255.0",
            "dhcp select interface",
            "quit",
            "interface GigabitEthernet0/0/1",
            "port link-type access",
            "port default vlan 10",
            "quit",
            "interface GigabitEthernet0/0/2",
            "port link-type access",
            "port default vlan 100",
            "quit",
            "interface GigabitEthernet0/0/3",
            "port link-type trunk",
            "port trunk allow-pass vlan 100 200",
            "quit",
            "ospf 1 router-id 3.3.3.3",
            "area 0",
            "network 10.0.23.0 0.0.0.255",
            "network 192.168.100.0 0.0.0.255",
            "network 192.168.200.0 0.0.0.255",
            "quit",
            "quit",
        ],
        "verify": [
            "display ip interface brief",
            "display vlan summary",
        ],
    },
    "AC1": {
        "port": 2003,
        "model": "Router",
        "role": "无线控制器 (WAC)",
        "commands": [
            "sysname AC1",
            "interface GigabitEthernet0/0/0",
            "ip address 192.168.100.1 255.255.255.0",
            "ospf network-type p2p",
            "undo shutdown",
            "quit",
            "ospf 1 router-id 4.4.4.4",
            "area 0",
            "network 192.168.100.0 0.0.0.255",
            "quit",
            "quit",
            "interface Wlan-Ess0",
            "quit",
            "wlan",
            "ap-auth-mode no-auth",
            "wmm-profile name campus-wmm",
            "quit",
            "traffic-profile name campus-traffic",
            "quit",
            "security-profile name campus-sec",
            "quit",
            "service-set name campus-ss",
            "ssid Campus-WiFi",
            "wlan-ess 0",
            "wmm-profile name campus-wmm",
            "traffic-profile name campus-traffic",
            "security-profile name campus-sec",
            "quit",
            "radio-profile name campus-radio",
            "wmm-profile name campus-wmm",
            "quit",
            "quit",
        ],
        "verify": [
            "display ip interface brief",
            "display service-set all",
        ],
    },
    "SW2": {
        "port": 2004,
        "model": "S3700",
        "role": "PoE 无线接入交换机",
        "commands": [
            "sysname SW2",
            "vlan batch 100 200",
            "interface GigabitEthernet0/0/1",
            "port link-type trunk",
            "port trunk allow-pass vlan 100 200",
            "quit",
            "interface Ethernet0/0/1",
            "port link-type trunk",
            "port trunk pvid vlan 100",
            "port trunk allow-pass vlan 100 200",
            "quit",
            "interface Vlanif100",
            "ip address 192.168.100.2 255.255.255.0",
            "quit",
            "ip route-static 0.0.0.0 0.0.0.0 192.168.100.254",
        ],
        "verify": [
            "display ip interface brief",
            "display vlan summary",
        ],
    },
}


def deploy_one_device(name: str, spec: dict) -> dict:
    port = spec["port"]
    t0 = time.time()
    wait_res = wait_for_console_ready(port, timeout=160.0)
    if not wait_res["ready"]:
        return {
            "device": name,
            "port": port,
            "role": spec["role"],
            "success": False,
            "error": wait_res.get("error"),
            "elapsed": round(time.time() - t0, 1),
        }

    with ConsoleConnection(port=port, timeout=8.0) as conn:
        deploy_res = conn.deploy_commands(
            commands=spec["commands"],
            verify_commands=spec["verify"],
            save_config=False,
        )
        prompt_after = conn.find_prompt()

    return {
        "device": name,
        "port": port,
        "role": spec["role"],
        "model": spec["model"],
        "boot_prompt": wait_res["prompt"],
        "final_prompt": prompt_after,
        "success": deploy_res["success"],
        "cmd_count": len(deploy_res["command_outputs"]),
        "cmd_ok": sum(1 for c in deploy_res["command_outputs"] if c["success"]),
        "verify_outputs": deploy_res["verify_outputs"],
        "elapsed": round(time.time() - t0, 1),
    }


def main() -> int:
    print("=" * 72)
    print("步骤 1: 生成综合园区网拓扑 (R1 + FW1 + SW1 + AC1 + AP1)")
    print("=" * 72)
    topo_path = generate(COMPREHENSIVE_DEMO, OUT_DIR)
    print(f"  [OK] 拓扑文件已生成: {topo_path} ({topo_path.stat().st_size} bytes)")

    print("\n" + "=" * 72)
    print("步骤 2: 检查/拉起 eNSP_Client.exe 并注入 32位控制模块 ensp_control.dll")
    print("=" * 72)
    pid = find_process_pid("eNSP_Client.exe")
    if not pid:
        print("  [*] eNSP_Client.exe 未运行，正在以 RunAsInvoker 模式启动 eNSP_Client.exe...")
        env = os.environ.copy()
        env["__COMPAT_LAYER"] = "RunAsInvoker"
        subprocess.Popen([str(ENSP_EXE)], cwd=str(ENSP_EXE.parent), env=env)
        for _ in range(30):
            pid = find_process_pid("eNSP_Client.exe")
            if pid:
                break
            time.sleep(0.5)
        if not pid:
            print("  [ERR] 无法启动 eNSP_Client.exe")
            return 1
        print(f"  [OK] eNSP_Client.exe 已启动 (PID={pid})，等待主视图与 VBoxServer 就绪...")
    else:
        print(f"  [OK] 检测到正在运行的 eNSP_Client.exe (PID={pid})")

    if not wait_for_main_view(pid, timeout=45.0):
        print("  [ERR] 等待 eNSP 主视图 (g_pMainFrame) 初始化超时")
        return 1

    inject_dll(pid, DLL_PATH)
    time.sleep(1.0)

    print("\n" + "=" * 72)
    print("步骤 3: 通过 DLL 进程内 RPC 无弹窗加载拓扑并一键启动全部设备")
    print("=" * 72)
    st0 = send_ctrl_cmd("status")
    print("  [RPC status (加载前)]:", st0)

    open_res = send_ctrl_cmd(f"open_topo {topo_path}")
    print("  [RPC open_topo]:      ", open_res)

    time.sleep(1.0)
    start_res = send_ctrl_cmd("start_all")
    print("  [RPC start_all]:      ", start_res)

    time.sleep(2.0)
    st1 = send_ctrl_cmd("status")
    print("  [RPC status (启动中)]:", st1)

    print("\n" + "=" * 72)
    print("步骤 4: 等待各设备 Console 端口 (2000..2004) 就绪并免 SSH 并发下发配置")
    print("=" * 72)
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        fmap = {
            pool.submit(deploy_one_device, name, spec): name
            for name, spec in DEVICE_CONFIG_PLAN.items()
        }
        for fut in concurrent.futures.as_completed(fmap):
            name = fmap[fut]
            res = fut.result()
            results[name] = res
            status_tag = "SUCCESS" if res.get("success") else "PARTIAL/FAIL"
            print(
                f"  [{status_tag}] {name:4s} ({res.get('role')} | port={res.get('port')}) "
                f"prompt={res.get('final_prompt')} cmds={res.get('cmd_ok')}/{res.get('cmd_count')} "
                f"耗时={res.get('elapsed')}s"
            )

    print("\n" + "=" * 72)
    print("步骤 5: 各设备 Console 实测回显验证")
    print("=" * 72)
    for name in ["R1", "FW1", "SW1", "AC1", "SW2"]:
        res = results.get(name, {})
        print(f"\n--- {name} ({res.get('model')} | Console 127.0.0.1:{res.get('port')}) ---")
        if res.get("error"):
            print("  ERROR:", res["error"])
            continue
        for v in res.get("verify_outputs", []):
            print(f"  $ {v['command']}")
            for line in (v["output"] or "").splitlines()[:15]:
                print(f"    {line}")

    print("\n" + "=" * 72)
    print("步骤 6: 等待 OSPF 全网收敛并执行跨防火墙/跨交换机端到端连通性验证")
    print("=" * 72)
    time.sleep(10.0)
    with ConsoleConnection(port=2000) as r1_conn:
        print("  [R1 $ display ospf peer brief]")
        for line in r1_conn.send_command("display ospf peer brief").splitlines():
            print("   ", line)
        for target_ip, desc in [
            ("10.0.12.2", "FW1 Untrust 接口"),
            ("10.0.23.2", "SW1 核心交换机 Vlanif10"),
            ("192.168.100.1", "AC1 无线控制器"),
            ("192.168.100.2", "SW2 无线接入交换机"),
        ]:
            print(f"\n  [R1 -> {desc} ({target_ip}) $ ping -c 2 {target_ip}]")
            ping_out = r1_conn.send_command(f"ping -c 2 {target_ip}", read_timeout=8.0)
            for line in ping_out.splitlines():
                if any(k in line for k in ("PING", "Reply", "packet loss", "round-trip")):
                    print("   ", line)

    with ConsoleConnection(port=2002) as sw1_conn:
        print("\n  [SW1 $ display ospf peer brief]")
        for line in sw1_conn.send_command("display ospf peer brief").splitlines():
            print("   ", line)

    st_final = send_ctrl_cmd("status")
    print("\n" + "=" * 72)
    print("最终 eNSP 内部状态:", st_final)
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
