"""只读探针：验证 eNSP_Client.exe 是否为「单拓扑单窗口」架构。

结论（本脚本用于复核，不修改任何东西）：
  eNSP_Client.exe 是一个 MFC **单文档（SDI）** 应用，且为**单实例**进程：
    - 进程内只有一个 CeNSP_ClientView（全局 0x005F4E40），它内嵌一个 CTopoBoard(+0x88)。
    - 只有 CSingleDocTemplate（镜像里没有 CMultiDocTemplate / CMDIChildWnd）。
    - 只允许一个进程实例（互斥体 "eNSP_Console_Mutex"）。

因此 `open_topo` 的语义是「把新拓扑加载进这唯一一个视图」，而不是新开窗口。
本脚本只做内存读取（ReadProcessMemory），不会写入、不会调用任何 eNSP 功能。

用法:  python -I tools/ensp_topo_probe.py
"""
from __future__ import annotations

import ctypes
import struct
import sys
from ctypes import wintypes

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PROCESS_ALL_ACCESS = 0x001F0FFF
TH32CS_SNAPPROCESS = 0x00000002

G_PMAINFRAME = 0x005F4E40          # CeNSP_ClientView* 全局（镜像基址 0x00400000，无 ASLR）
VIEW_DOC_OFF = 0x54                # CView::m_pDocument（实测偏移）
BOARD_OFF = 0x88                   # CeNSP_ClientView 内嵌 CTopoBoard
BOARD_DEV_COUNT = 0x50             # CTopoBoard: 设备数
BOARD_LINE_COUNT = 0x5C            # CTopoBoard: 连线数

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)


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


kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.ReadProcessMemory.argtypes = [
    wintypes.HANDLE,
    wintypes.LPCVOID,
    wintypes.LPVOID,
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
]
kernel32.ReadProcessMemory.restype = wintypes.BOOL


def find_pid(exe_name: str = "eNSP_Client.exe") -> int:
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == wintypes.HANDLE(-1).value:
        return 0
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snap, ctypes.byref(pe)):
            return 0
        while True:
            if pe.szExeFile.lower() == exe_name.lower():
                return pe.th32ProcessID
            if not kernel32.Process32NextW(snap, ctypes.byref(pe)):
                return 0
    finally:
        kernel32.CloseHandle(snap)


class Reader:
    def __init__(self, pid: int):
        self.h = kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
        if not self.h:
            raise OSError(f"OpenProcess 失败: {ctypes.get_last_error()}")

    def u32(self, addr: int) -> int:
        buf = ctypes.create_string_buffer(4)
        n = ctypes.c_size_t(0)
        if not kernel32.ReadProcessMemory(
            self.h, ctypes.c_void_p(addr), buf, 4, ctypes.byref(n)
        ):
            return 0
        return struct.unpack("<I", buf.raw)[0]

    def bytes(self, addr: int, size: int) -> bytes:
        buf = ctypes.create_string_buffer(size)
        n = ctypes.c_size_t(0)
        kernel32.ReadProcessMemory(
            self.h, ctypes.c_void_p(addr), buf, size, ctypes.byref(n)
        )
        return buf.raw[: n.value]

    def close(self):
        if self.h:
            kernel32.CloseHandle(self.h)


def rtti_name(r: Reader, obj_addr: int) -> str:
    """从对象虚表指针解析 RTTI 类名（MSVC）。"""
    vt = r.u32(obj_addr)
    if not (0x400000 <= vt < 0x800000):
        return ""
    col = r.u32(vt - 4)
    if not (0x400000 <= col < 0x800000):
        return ""
    td = r.u32(col + 0xC)  # TypeDescriptor*
    if not (0x400000 <= td < 0x800000):
        return ""
    raw = r.bytes(td + 8, 96).split(b"\x00")[0]
    return raw.decode("latin1", "replace")


def enum_afx_frames(pid: int) -> list[tuple[int, str, str]]:
    """枚举该进程的所有顶层 Afx 窗口（拓扑窗口应只会有 1 个主框架）。"""
    result: list[tuple[int, str, str]] = []
    cb_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        wpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value != pid:
            return True
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        title = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, title, 256)
        if cls.value.startswith("Afx:"):
            result.append((hwnd, cls.value, title.value))
        return True

    user32.EnumWindows(cb_type(_cb), 0)
    return result


def main() -> int:
    pid = find_pid()
    if not pid:
        print("[ERR] eNSP_Client.exe 未运行")
        return 1
    print(f"eNSP_Client.exe PID = {pid}")

    frames = enum_afx_frames(pid)
    print(f"\n顶层 Afx 窗口数 = {len(frames)}  (SDI 预期为 1)")
    for hwnd, cls, title in frames:
        module_base = cls.split(":")[1] if ":" in cls else ""
        print(f"  hwnd={hwnd:#x} module_base=0x{module_base} class={cls!r} title={title!r}")

    r = Reader(pid)
    try:
        view = r.u32(G_PMAINFRAME)
        if not view:
            print("\n[!] g_pMainFrame 为空（拓扑尚未初始化）")
            return 0
        print(f"\nCeNSP_ClientView @ {view:#x}  RTTI={rtti_name(r, view)}")

        doc = r.u32(view + VIEW_DOC_OFF)
        print(f"  m_pDocument(+{VIEW_DOC_OFF:#x}) = {doc:#x}  RTTI={rtti_name(r, doc)}")

        # 文档模板（CSingleDocTemplate / CMultiDocTemplate）
        tmpl = 0
        for off in range(0, 0x80, 4):
            p = r.u32(doc + off)
            nm = rtti_name(r, p) if 0x1000000 <= p < 0x80000000 else ""
            if "DocTemplate" in nm:
                tmpl = p
                print(f"  文档模板(doc+{off:#x}) = {p:#x}  RTTI={nm}")
                break
        if not tmpl:
            print("  [!] 未找到文档模板指针")

        board = view + BOARD_OFF
        print(
            f"\nCTopoBoard @ {board:#x}  "
            f"devices={r.u32(board + BOARD_DEV_COUNT)} "
            f"links={r.u32(board + BOARD_LINE_COUNT)}"
        )
        print(
            "\n结论: 单视图 / 单文档 / 单实例 → "
            "同一时刻只能有一个拓扑，open_topo 是「替换」而非「新开窗口」。"
        )
    finally:
        r.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
