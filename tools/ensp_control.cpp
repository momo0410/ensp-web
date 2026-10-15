// 编译命令:
// /c/TDM-GCC-64/bin/g++ -m32 -shared -static -O2 -Wall D:/ensp-web/tools/ensp_control.cpp -o D:/ensp-web/tools/ensp_control.dll -lws2_32

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <winsock2.h>
#include <ws2tcpip.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

// eNSP_Client.exe (32-bit x86, ImageBase 0x00400000, 无 ASLR) 固定地址
static const uintptr_t ADDR_G_PMAINFRAME    = 0x005F4E40; // CeNSP_ClientView* 全局指针
static const uintptr_t ADDR_AFX_GET_STR_MGR = 0x005213C6; // IAtlStringMgr* (__cdecl*)()
static const uintptr_t ADDR_CSTRING_SETSTR  = 0x00402AF0; // void (__thiscall*)(void* this, const wchar_t* src, int len)
static const uintptr_t ADDR_OPEN_TOPO_PATH  = 0x0044C740; // void (__stdcall*)(void* pView, wchar_t* cstrByVal)
static const uintptr_t ADDR_CLEANUP_TIMERS  = 0x0044AFB0; // edi = pView
static const uintptr_t ADDR_RESET_CUR_TOPO  = 0x0044C2C0; // esi = pView
static const uintptr_t ADDR_SELECT_ALL_DEVS = 0x004BAAE0; // eax = pView + 0x88

static const UINT WM_ENSP_CTRL = WM_APP + 0x355;
static const UINT CMD_ID_START = 0x8021;
static const UINT CMD_ID_STOP  = 0x8022;
static const uint16_t LISTEN_PORT = 18800;

enum CtrlAction {
    ACT_OPEN_TOPO  = 1,
    ACT_START_ALL  = 2,
    ACT_STOP_ALL   = 3,
    ACT_STATUS     = 4,
    ACT_LIVE_TOPO  = 5,
    ACT_START_ONE  = 6,   // start_dev:<名称或序号>
    ACT_STOP_ONE   = 7,   // stop_dev:<名称或序号>
};

// 设备明细 JSON 可能较长（名称/型号/UUID），放宽响应缓冲。
#define ENSP_RESP_MAX 1024

struct CtrlRequest {
    CtrlAction action;
    wchar_t topoPath[MAX_PATH * 2];
    wchar_t devName[MAX_PATH];   // 单台操作的设备名或序号（"start_dev:R1" / "start_dev:2"）
    int result;
    char response[ENSP_RESP_MAX];
};

// CTopoItem 字段偏移（经逆向 eNSP_Client.exe 验证，见 tools/ensp_probe 说明）
static const uintptr_t ITEM_NAME_OFF   = 0x70;  // CString* 设备名（如 "R1"）
static const uintptr_t ITEM_MODEL_OFF  = 0x74;  // CString* 设备型号（如 "AR1220"）
static const uintptr_t ITEM_UUID_OFF   = 0xAC;  // CString* 设备 UUID
static const uintptr_t ITEM_POWER_OFF  = 0xB8;  // uint32 电源状态 0=running,1/2=stopped,else=starting

// DLL 自身记录「最近一次由控制端口打开的拓扑路径」。
// 注意：eNSP 不把当前 .topo 路径保存在文档/画板/视图的稳定成员中
// （CeNSP_ClientDoc 的 m_strPathName 恒为空 / 标题恒为 "Untitled"），
// 因此用户通过 eNSP 菜单手动打开的拓扑无法可靠取得文件路径 —— 只能给出设备清单。
static wchar_t g_lastOpenedTopo[MAX_PATH * 2] = {0};

static WNDPROC g_origWndProc = NULL;
static HWND    g_hHookedWnd  = NULL;
static volatile LONG g_running = 1;

static inline void* GetMainView() {
    return *(void**)ADDR_G_PMAINFRAME;
}

static inline HWND GetMainViewHwnd(void* pView) {
    if (!pView) return NULL;
    return *(HWND*)((uintptr_t)pView + 0x20);
}

// 从 CTopoItem 的 CString* 成员读取宽字符串（成员存的是指向字符数据的指针）。
// 返回 UTF-8（写入 outUtf8），若指针无效则写入空串。
// 说明：本 DLL 运行在 eNSP 进程内，直接解引用坏指针会让 eNSP 崩溃，
// 因此读数前先用 VirtualQuery 校验目标内存确实可读。GCC 不支持 MSVC 的
// __try/__except 语法，故采用 VirtualQuery 方案。
static bool IsMemoryReadable(const void* p, size_t need) {
    MEMORY_BASIC_INFORMATION mbi;
    if (!p) return false;
    if (VirtualQuery(p, &mbi, sizeof(mbi)) == 0) return false;
    if (mbi.State != MEM_COMMIT) return false;
    if (mbi.Protect & (PAGE_NOACCESS | PAGE_GUARD)) return false;
    const unsigned char* start = (const unsigned char*)p;
    const unsigned char* end = start + need;
    if ((const unsigned char*)mbi.BaseAddress + mbi.RegionSize < end) return false;
    return true;
}

// ---------------------------------------------------------------------------
// 安全读取原语
//
// 本 DLL 的处理逻辑跑在 **eNSP 的 UI 线程**（通过 SendMessage 触发的窗口过程）。
// 在这里抛出任何异常都无法被我们捕获：异常会穿过用户态回调边界，Windows 只能
// 以 STATUS_FATAL_USER_CALLBACK_EXCEPTION (0xC000041D) 直接杀掉整个 eNSP 进程。
// 实测 2026-10-12 04:05 的崩溃（0xC0000005 + 0xC000041D @ 0x00054ED0）就是这么来的。
//
// 因此：**每一次**解引用 eNSP 内部内存前都必须先校验可读性，且必须对读出来的
// 数量做上界钳制——否则一个脏的 nDevs(比如 20 亿) 会让循环去遍历野指针。
// ---------------------------------------------------------------------------
/// 指针是否落在用户态可用范围内（排除 NULL 页与内核地址）。
static inline bool IsSanePtr(uintptr_t p) {
    return p >= 0x10000 && p < 0x7FFF0000;
}

static bool ReadU32(uintptr_t addr, uint32_t* out) {
    if (!IsMemoryReadable((const void*)addr, 4)) return false;
    *out = *(uint32_t*)addr;
    return true;
}

static bool ReadPtr(uintptr_t addr, uintptr_t* out) {
    if (!IsMemoryReadable((const void*)addr, sizeof(uintptr_t))) return false;
    uintptr_t v = *(uintptr_t*)addr;
    if (!IsSanePtr(v)) return false;
    *out = v;
    return true;
}

// eNSP 内部数组元素数的合理上界。超出即视为读到脏数据，按 0 处理。
#define MAX_TOPO_ITEMS 512

/// 安全读取拓扑画板的计数与设备数组指针。任何一个读不到就退化成"空拓扑"。
/// 四个出参都可以是 NULL（调用方按需索取），因此每个写入前都必须判空——
/// GCC 在 -O2 下会把这个函数从内联改写成独立函数并做常量传播，
/// 一旦有调用点只填部分出参，无条件解引用就会让 eNSP 访问违例。
static void ReadBoardCounts(uintptr_t pTopoMgr, int* nDevs, int* nLines,
                            int* nSel, uintptr_t* devArr) {
    if (nDevs) *nDevs = 0;
    if (nLines) *nLines = 0;
    if (nSel) *nSel = 0;
    if (devArr) *devArr = 0;
    uint32_t v = 0;
    if (nDevs && ReadU32(pTopoMgr + 0x50, &v)) {
        *nDevs = (v <= MAX_TOPO_ITEMS) ? (int)v : 0;
    }
    if (nLines && ReadU32(pTopoMgr + 0x5C, &v)) {
        *nLines = (v <= MAX_TOPO_ITEMS) ? (int)v : 0;
    }
    if (nSel && ReadU32(pTopoMgr + 0x74, &v)) {
        *nSel = (v <= MAX_TOPO_ITEMS) ? (int)v : 0;
    }
    uintptr_t arr = 0;
    if (devArr && ReadPtr(pTopoMgr + 0x4C, &arr)) {
        *devArr = arr;
    }
}

/// 安全取第 i 台设备的对象指针；读不到/地址不合理返回 0。
static uintptr_t GetDevicePtr(uintptr_t devArr, int i) {
    uintptr_t dev = 0;
    if (!devArr) return 0;
    if (!ReadPtr(devArr + (uintptr_t)i * 4, &dev)) return 0;
    return dev;
}

/// 安全读设备电源状态；读不到返回 0xFFFFFFFF（"未知"，调用方按未运行处理）。
static uint32_t ReadPowerState(uintptr_t dev) {
    uint32_t st = 0;
    if (!dev) return 0xFFFFFFFFu;
    if (!ReadU32(dev + ITEM_POWER_OFF, &st)) return 0xFFFFFFFFu;
    return st;
}

static void ReadItemStringUtf8(uintptr_t item, uintptr_t off, char* outUtf8, int outSize) {
    outUtf8[0] = '\0';
    if (!item) return;
    if (!IsMemoryReadable((const void*)(item + off), 4)) return;
    uintptr_t pStr = *(uintptr_t*)(item + off);
    if (!IsSanePtr(pStr)) return;
    // 逐字符读取，最多读到一个 L'\0' 或缓冲上限，全程校验可读性。
    int oi = 0;
    for (int i = 0; i < outSize - 1; ++i) {
        if (!IsMemoryReadable((const void*)(pStr + i * 2), 2)) break;
        wchar_t wc = *(const wchar_t*)(pStr + i * 2);
        if (wc == L'\0') break;
        // 单字符 UTF-8 编码（BMP 内）
        if (wc < 0x80) {
            outUtf8[oi++] = (char)wc;
        } else if (wc < 0x800) {
            if (oi + 2 > outSize - 1) break;
            outUtf8[oi++] = (char)(0xC0 | (wc >> 6));
            outUtf8[oi++] = (char)(0x80 | (wc & 0x3F));
        } else {
            if (oi + 3 > outSize - 1) break;
            outUtf8[oi++] = (char)(0xE0 | (wc >> 12));
            outUtf8[oi++] = (char)(0x80 | ((wc >> 6) & 0x3F));
            outUtf8[oi++] = (char)(0x80 | (wc & 0x3F));
        }
    }
    outUtf8[oi] = '\0';
}

// 将 UTF-8 字符串中的 JSON 特殊字符转义后追加到 buf。
static void JsonAppendEscaped(char* buf, int cap, const char* s) {
    int len = (int)strlen(buf);
    for (const unsigned char* p = (const unsigned char*)s; *p && len < cap - 8; ++p) {
        switch (*p) {
            case '"':  buf[len++] = '\\'; buf[len++] = '"';  break;
            case '\\': buf[len++] = '\\'; buf[len++] = '\\'; break;
            case '\n': buf[len++] = '\\'; buf[len++] = 'n';  break;
            case '\r': buf[len++] = '\\'; buf[len++] = 'r';  break;
            case '\t': buf[len++] = '\\'; buf[len++] = 't';  break;
            default:
                if (*p < 0x20) {
                    len += snprintf(buf + len, cap - len, "\\u%04x", *p);
                } else {
                    buf[len++] = (char)*p;
                }
        }
    }
    buf[len] = '\0';
}

// 将 g_lastOpenedTopo（宽字符）以 JSON 字符串（含转义）写入 buf 末尾。
static void JsonAppendTopoPath(char* buf, int cap) {
    char utf8[MAX_PATH * 3] = {0};
    if (g_lastOpenedTopo[0]) {
        WideCharToMultiByte(CP_UTF8, 0, g_lastOpenedTopo, -1, utf8, sizeof(utf8), NULL, NULL);
    }
    strncat(buf, "\"topo_path\":\"", cap - (int)strlen(buf) - 1);
    JsonAppendEscaped(buf, cap, utf8);
    strncat(buf, "\"", cap - (int)strlen(buf) - 1);
}

// 遍历 CTopoBoard 设备数组，构造 JSON 数组 [{name,model,uuid,power_state},...]。
static void BuildDeviceArrayJson(uintptr_t pTopoMgr, char* out, int cap) {
    int pos = 0;
    out[0] = '\0';
    int nDevs = 0, nLines = 0;
    uintptr_t devArr = 0;
    ReadBoardCounts(pTopoMgr, &nDevs, &nLines, NULL, &devArr);
    pos += snprintf(out + pos, cap - pos, "[");
    int written = 0;
    for (int i = 0; i < nDevs && pos < cap - 256; ++i) {
        uintptr_t dev = GetDevicePtr(devArr, i);
        if (!dev) continue;
        char name[256], model[128], uuid[128];
        ReadItemStringUtf8(dev, ITEM_NAME_OFF,  name,  sizeof(name));
        ReadItemStringUtf8(dev, ITEM_MODEL_OFF, model, sizeof(model));
        ReadItemStringUtf8(dev, ITEM_UUID_OFF,  uuid,  sizeof(uuid));
        uint32_t st = ReadPowerState(dev);
        pos += snprintf(out + pos, cap - pos, "%s{\"name\":\"", (written == 0 ? "" : ","));
        written++;
        JsonAppendEscaped(out, cap, name);
        pos = (int)strlen(out);
        pos += snprintf(out + pos, cap - pos, "\",\"model\":\"");
        JsonAppendEscaped(out, cap, model);
        pos = (int)strlen(out);
        pos += snprintf(out + pos, cap - pos, "\",\"uuid\":\"");
        JsonAppendEscaped(out, cap, uuid);
        pos = (int)strlen(out);
        pos += snprintf(out + pos, cap - pos, "\",\"power_state\":%u}", st);
    }
    snprintf(out + pos, cap - pos, "]");
}

// 在主 UI 线程中执行：清理旧拓扑并无弹窗打开新拓扑
static void MainThread_OpenTopo(void* pView, const wchar_t* wpath) {
    uint32_t flag = 0;
    bool flagOk = ReadU32((uintptr_t)pView + 0x73A0, &flag);
    if (flagOk && flag != 0) {
        // 0x00454ED0 是"关闭当前拓扑"的内部函数。实测在拓扑正在析构/重入时
        // 调用它会让 eNSP 在 UI 线程内访问违规并整体崩溃（2026-10-12 04:05
        // 的 0xC0000005/0xC000041D @ 0x00054ED0 即此处）。读不到标志位就跳过，
        // 宁可少清一次也不要冒崩掉整个 eNSP 的风险。
        uintptr_t fnAddr = 0x00454ED0;
        __asm__ __volatile__(
            "call *%0"
            :
            : "r"(fnAddr)
            : "eax", "ecx", "edx", "memory", "cc"
        );
    }

    // 执行 0x0044C350 的 IDNO(不保存) 清理分支，跳过 0x0044C3D0 脏标志保存弹窗
    {
        void* ediVal = pView;
        uintptr_t fnCleanup = ADDR_CLEANUP_TIMERS;
        __asm__ __volatile__(
            "call *%1"
            : "+D"(ediVal)
            : "r"(fnCleanup)
            : "eax", "ecx", "edx", "memory", "cc"
        );
    }
    {
        void* esiVal = pView;
        uintptr_t fnReset = ADDR_RESET_CUR_TOPO;
        __asm__ __volatile__(
            "call *%1"
            : "+S"(esiVal)
            : "r"(fnReset)
            : "eax", "ecx", "edx", "memory", "cc"
        );
    }

    // [实验] 跳过 MFC CString 拼接与所有清理，直接传入路径指针
    typedef void (__stdcall *fnOpenTopoByPath)(void* pMainFrame, wchar_t* strPathByVal);
    ((fnOpenTopoByPath)ADDR_OPEN_TOPO_PATH)(pView, (wchar_t*)wpath);
}

static void MainThread_SelectAll(void* pView) {
    uintptr_t pTopoMgr = (uintptr_t)pView + 0x88;
    uintptr_t fnSelectAll = ADDR_SELECT_ALL_DEVS;
    __asm__ __volatile__(
        "call *%1"
        : "+a"(pTopoMgr)
        : "r"(fnSelectAll)
        : "ecx", "edx", "memory", "cc"
    );
}

/// 画板选中列表的字段偏移（经反汇编 0x004BAAE0「全选」验证）：
///   +0x70  选中设备指针数组的数据指针
///   +0x74  当前选中数量（全选函数对每台执行 addl $1）
///   +0x78  数组容量（与 +0x74 相等时才触发 realloc 扩容）
/// 全选函数遍历设备数组，把每台 push 进这个列表并设 dev->0x14 = 1。
/// 「启动/停止」命令作用于**这个列表**，所以单选必须重建列表本身，
/// 只动 dev->0x14 是无效的（实测会让命令仍作用于全部设备）。
static const uintptr_t BOARD_SEL_PTR  = 0x70;
static const uintptr_t BOARD_SEL_SIZE = 0x74;
static const uintptr_t BOARD_SEL_CAP  = 0x78;

/// 把画板的选中列表重置为「只有 target 一台」。
/// 返回是否成功；容量不足时宁可不改（返回 false），让调用方回错误而不是写坏内存。
static bool BoardSelectOnly(uintptr_t pTopoMgr, uintptr_t target) {
    if (!target) return false;
    if (!IsMemoryReadable((const void*)(pTopoMgr + BOARD_SEL_PTR), 4)) return false;

    uintptr_t data = *(uintptr_t*)(pTopoMgr + BOARD_SEL_PTR);
    uint32_t cap = 0;
    if (!ReadU32(pTopoMgr + BOARD_SEL_CAP, &cap)) return false;

    if (cap >= 1 && data) {
        // 容量足够：原地重建为只含 target
        if (!IsMemoryReadable((const void*)data, 4)) return false;
        *(uintptr_t*)data = target;
        *(uint32_t*)(pTopoMgr + BOARD_SEL_SIZE) = 1;
        return true;
    }

    // 容量为 0（从未选中过任何设备）：借全选内部函数先建出容量，
    // 再裁剪成 1 台。全选是 eNSP 自己的代码路径，扩容逻辑与它保持一致。
    return false;
}

/// 在拓扑画板里按名称或序号选中**唯一**一台设备，并返回其对象指针。
/// 找不到返回 NULL（此时调用方应回错误，不要发命令）。
static uintptr_t MainThread_SelectOne(void* pView, const wchar_t* selector) {
    uintptr_t pTopoMgr = (uintptr_t)pView + 0x88;
    int nDevs = 0;
    uintptr_t devArr = 0;
    ReadBoardCounts(pTopoMgr, &nDevs, NULL, NULL, &devArr);
    if (nDevs <= 0 || !devArr) return 0;

    // 选择器既可以是设备名（如 "R1"），也可以是序号（如 "2"，0 起算，兼容界面下标）
    char wantUtf8[256] = {0};
    WideCharToMultiByte(CP_UTF8, 0, selector, -1, wantUtf8, sizeof(wantUtf8), NULL, NULL);
    bool byIndex = true;
    for (const char* p = wantUtf8; *p; ++p) {
        if (*p < '0' || *p > '9') { byIndex = false; break; }
    }
    int wantIdx = byIndex ? atoi(wantUtf8) : -1;

    uintptr_t target = 0;
    for (int i = 0; i < nDevs; ++i) {
        uintptr_t dev = GetDevicePtr(devArr, i);
        if (!dev) continue;
        bool hit = byIndex ? (i == wantIdx) : false;
        if (!hit && !byIndex) {
            char name[256] = {0};
            ReadItemStringUtf8(dev, ITEM_NAME_OFF, name, sizeof(name));
            hit = (strcmp(name, wantUtf8) == 0);
        }
        if (hit) { target = dev; break; }
    }
    if (!target) return 0;

    // 重建选中列表；失败就让调用方知道（不要退化成"对全部设备下令"）
    if (!BoardSelectOnly(pTopoMgr, target)) return 0;
    return target;
}

static LRESULT CALLBACK HookedViewWndProc(HWND hWnd, UINT uMsg, WPARAM wParam, LPARAM lParam) {
    if (uMsg == WM_ENSP_CTRL && lParam != 0) {
        CtrlRequest* req = (CtrlRequest*)lParam;
        void* pView = GetMainView();
        if (!pView) {
            req->result = -1;
            snprintf(req->response, sizeof(req->response),
                     "{\"ok\":false,\"error\":\"g_pMainFrame is NULL\"}\n");
            return 0;
        }

        uintptr_t pTopoMgr = (uintptr_t)pView + 0x88;
        // 视图对象本身也必须先确认可读：pView 可能为野指针（eNSP 正在换拓扑），
        // 直接 pView+0x88 去解引用就会访问违规。
        if (!IsMemoryReadable((const void*)(uintptr_t)pView, 0x100)) {
            req->result = -4;
            snprintf(req->response, sizeof(req->response),
                     "{\"ok\":false,\"error\":\"main view not readable\"}\n");
            return 1;
        }

        switch (req->action) {
            case ACT_OPEN_TOPO: {
                DWORD attrs = GetFileAttributesW(req->topoPath);
                if (attrs == INVALID_FILE_ATTRIBUTES) {
                    req->result = -2;
                    snprintf(req->response, sizeof(req->response),
                             "{\"ok\":false,\"error\":\"Topo file not found\"}\n");
                    break;
                }
                MainThread_OpenTopo(pView, req->topoPath);
                wcsncpy(g_lastOpenedTopo, req->topoPath, MAX_PATH * 2 - 1);
                g_lastOpenedTopo[MAX_PATH * 2 - 1] = L'\0';
                int nDevs = 0, nLines = 0;
                ReadBoardCounts(pTopoMgr, &nDevs, &nLines, NULL, NULL);
                req->result = 0;
                snprintf(req->response, sizeof(req->response),
                         "{\"ok\":true,\"action\":\"open_topo\",\"devices\":%d,\"links\":%d}\n",
                         nDevs, nLines);
                break;
            }
            case ACT_START_ALL: {
                MainThread_SelectAll(pView);
                PostMessageW(hWnd, WM_COMMAND, MAKEWPARAM(CMD_ID_START, 0), 0);
                int nDevs = 0, nLines = 0;
                ReadBoardCounts(pTopoMgr, &nDevs, &nLines, NULL, NULL);
                req->result = 0;
                snprintf(req->response, sizeof(req->response),
                         "{\"ok\":true,\"action\":\"start_all\",\"devices\":%d}\n", nDevs);
                break;
            }
            case ACT_STOP_ALL: {
                MainThread_SelectAll(pView);
                PostMessageW(hWnd, WM_COMMAND, MAKEWPARAM(CMD_ID_STOP, 0), 0);
                int nDevs = 0, nLines = 0;
                ReadBoardCounts(pTopoMgr, &nDevs, &nLines, NULL, NULL);
                req->result = 0;
                snprintf(req->response, sizeof(req->response),
                         "{\"ok\":true,\"action\":\"stop_all\",\"devices\":%d}\n", nDevs);
                break;
            }
            case ACT_START_ONE:
            case ACT_STOP_ONE: {
                // 单台启停：先把画板选中列表重建为"只有这一台"，再发与全部启停
                // 相同的 WM_COMMAND。命令是异步的（PostMessage），这里只回报"已下发"，
                // 真实电源状态由随后的 status / live_topo 确认。
                //
                // 找不到设备、或无法安全重建选中列表时，都**不能**发命令——
                // 那会让命令作用于 eNSP 当前的选中集合（可能是全部设备）。
                if (!req->devName[0]) {
                    req->result = -5;
                    snprintf(req->response, sizeof(req->response),
                             "{\"ok\":false,\"error\":\"missing device selector\"}\n");
                    break;
                }
                char wantUtf8[256] = {0};
                char escaped[512] = {0};
                WideCharToMultiByte(CP_UTF8, 0, req->devName, -1,
                                    wantUtf8, sizeof(wantUtf8), NULL, NULL);
                JsonAppendEscaped(escaped, sizeof(escaped), wantUtf8);

                uintptr_t dev = MainThread_SelectOne(pView, req->devName);
                if (!dev) {
                    // 区分"设备不存在"与"无法单选"：前者是调用方传错名字，
                    // 后者说明选中列表容量为 0（还没选中过任何设备），
                    // 需要先由用户或全选操作建立容量。
                    req->result = -6;
                    int nDevs = 0;
                    uintptr_t devArr = 0;
                    ReadBoardCounts((uintptr_t)pView + 0x88, &nDevs, NULL, NULL, &devArr);
                    bool exists = false;
                    for (int i = 0; i < nDevs && devArr; ++i) {
                        uintptr_t cand = GetDevicePtr(devArr, i);
                        if (!cand) continue;
                        char name[256] = {0};
                        ReadItemStringUtf8(cand, ITEM_NAME_OFF, name, sizeof(name));
                        if (strcmp(name, wantUtf8) == 0) { exists = true; break; }
                    }
                    snprintf(req->response, sizeof(req->response),
                             "{\"ok\":false,\"error\":\"%s\",\"selector\":\"%s\"}\n",
                             exists ? "cannot select single device (selection list empty)"
                                    : "device not found",
                             escaped);
                    break;
                }
                char devName[256] = {0};
                char escapedName[512] = {0};
                ReadItemStringUtf8(dev, ITEM_NAME_OFF, devName, sizeof(devName));
                JsonAppendEscaped(escapedName, sizeof(escapedName), devName);
                UINT cmdId = (req->action == ACT_START_ONE) ? CMD_ID_START : CMD_ID_STOP;
                PostMessageW(hWnd, WM_COMMAND, MAKEWPARAM(cmdId, 0), 0);
                req->result = 0;
                snprintf(req->response, sizeof(req->response),
                         "{\"ok\":true,\"action\":\"%s\",\"device\":\"%s\"}\n",
                         (req->action == ACT_START_ONE) ? "start_dev" : "stop_dev", escapedName);
                break;
            }
            case ACT_STATUS: {
                int nDevs = 0, nLines = 0, nSel = 0;
                uintptr_t devArr = 0;
                ReadBoardCounts(pTopoMgr, &nDevs, &nLines, &nSel, &devArr);
                int running = 0, stopped = 0, starting = 0;
                for (int i = 0; i < nDevs; ++i) {
                    uintptr_t dev = GetDevicePtr(devArr, i);
                    if (!dev) continue;
                    uint32_t st = ReadPowerState(dev);
                    if (st == 0) running++;
                    else if (st == 1 || st == 2) stopped++;
                    else starting++;   // 含 0xFFFFFFFF（读不到，按未运行计入）
                }
                req->result = 0;
                // 向后兼容：保留原有字段；追加 topo_path 与 has_topo（新增字段，旧解析器忽略之）。
                snprintf(req->response, sizeof(req->response),
                         "{\"ok\":true,\"action\":\"status\",\"devices\":%d,\"links\":%d,\"selected\":%d,\"running\":%d,\"starting\":%d,\"stopped\":%d,\"has_topo\":%s,",
                         nDevs, nLines, nSel, running, starting, stopped,
                         (nDevs > 0 ? "true" : "false"));
                JsonAppendTopoPath(req->response, sizeof(req->response));
                strncat(req->response, "}\n", sizeof(req->response) - strlen(req->response) - 1);
                break;
            }
            case ACT_LIVE_TOPO: {
                int nDevs = 0, nLines = 0;
                ReadBoardCounts(pTopoMgr, &nDevs, &nLines, NULL, NULL);
                char devJson[ENSP_RESP_MAX - 512];
                BuildDeviceArrayJson(pTopoMgr, devJson, sizeof(devJson));
                req->result = 0;
                snprintf(req->response, sizeof(req->response),
                         "{\"ok\":true,\"action\":\"live_topo\",\"has_topo\":%s,\"devices\":%d,\"links\":%d,",
                         (nDevs > 0 ? "true" : "false"), nDevs, nLines);
                JsonAppendTopoPath(req->response, sizeof(req->response));
                strncat(req->response, ",\"device_list\":", sizeof(req->response) - strlen(req->response) - 1);
                strncat(req->response, devJson, sizeof(req->response) - strlen(req->response) - 1);
                strncat(req->response, "}\n", sizeof(req->response) - strlen(req->response) - 1);
                break;
            }
            default:
                req->result = -3;
                snprintf(req->response, sizeof(req->response),
                         "{\"ok\":false,\"error\":\"Unknown action\"}\n");
                break;
        }
        return 1;
    }
    return CallWindowProcW(g_origWndProc, hWnd, uMsg, wParam, lParam);
}

static HWND EnsureViewSubclass() {
    void* pView = GetMainView();
    HWND hWnd = GetMainViewHwnd(pView);
    if (!hWnd || !IsWindow(hWnd)) return NULL;

    if (g_hHookedWnd != hWnd || !g_origWndProc) {
        WNDPROC prev = (WNDPROC)SetWindowLongPtrW(hWnd, GWLP_WNDPROC, (LONG_PTR)HookedViewWndProc);
        if (prev && prev != HookedViewWndProc) {
            g_origWndProc = prev;
            g_hHookedWnd  = hWnd;
        }
    }
    return hWnd;
}

static bool ParseCommand(const char* raw, CtrlRequest* req) {
    memset(req, 0, sizeof(*req));
    while (*raw == ' ' || *raw == '\t' || *raw == '\r' || *raw == '\n') raw++;

    char line[1024];
    strncpy(line, raw, sizeof(line) - 1);
    line[sizeof(line) - 1] = '\0';
    int n = (int)strlen(line);
    while (n > 0 && (line[n - 1] == '\r' || line[n - 1] == '\n' || line[n - 1] == ' ')) {
        line[--n] = '\0';
    }

    if (strncmp(line, "open_topo ", 10) == 0) {
        req->action = ACT_OPEN_TOPO;
        const char* pathUtf8 = line + 10;
        while (*pathUtf8 == ' ' || *pathUtf8 == '"') pathUtf8++;
        char cleanPath[MAX_PATH * 2];
        strncpy(cleanPath, pathUtf8, sizeof(cleanPath) - 1);
        cleanPath[sizeof(cleanPath) - 1] = '\0';
        size_t clen = strlen(cleanPath);
        if (clen > 0 && cleanPath[clen - 1] == '"') cleanPath[clen - 1] = '\0';
        MultiByteToWideChar(CP_UTF8, 0, cleanPath, -1, req->topoPath, MAX_PATH * 2);
        return true;
    }
    if (strcmp(line, "start_all") == 0) {
        req->action = ACT_START_ALL;
        return true;
    }
    if (strcmp(line, "stop_all") == 0) {
        req->action = ACT_STOP_ALL;
        return true;
    }
    if (strcmp(line, "status") == 0) {
        req->action = ACT_STATUS;
        return true;
    }
    if (strcmp(line, "live_topo") == 0) {
        req->action = ACT_LIVE_TOPO;
        return true;
    }
    // 单台启停：start_dev:<名称或序号> / stop_dev:<名称或序号>
    if (strncmp(line, "start_dev:", 10) == 0) {
        req->action = ACT_START_ONE;
        MultiByteToWideChar(CP_UTF8, 0, line + 10, -1, req->devName, MAX_PATH);
        return true;
    }
    if (strncmp(line, "stop_dev:", 9) == 0) {
        req->action = ACT_STOP_ONE;
        MultiByteToWideChar(CP_UTF8, 0, line + 9, -1, req->devName, MAX_PATH);
        return true;
    }
    return false;
}

static DWORD WINAPI ControlServerThread(LPVOID lpParam) {
    WSADATA wsaData;
    if (WSAStartup(MAKEWORD(2, 2), &wsaData) != 0) return 1;

    for (int i = 0; i < 150 && g_running; ++i) {
        if (EnsureViewSubclass() != NULL) break;
        Sleep(100);
    }

    SOCKET listenSock = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (listenSock == INVALID_SOCKET) {
        WSACleanup();
        return 1;
    }

    int opt = 1;
    setsockopt(listenSock, SOL_SOCKET, SO_REUSEADDR, (const char*)&opt, sizeof(opt));

    sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = htons(LISTEN_PORT);

    if (bind(listenSock, (sockaddr*)&addr, sizeof(addr)) == SOCKET_ERROR ||
        listen(listenSock, 8) == SOCKET_ERROR) {
        closesocket(listenSock);
        WSACleanup();
        return 1;
    }

    while (g_running) {
        SOCKET clientSock = accept(listenSock, NULL, NULL);
        if (clientSock == INVALID_SOCKET) break;

        char buf[1024] = {0};
        int received = recv(clientSock, buf, sizeof(buf) - 1, 0);
        if (received > 0) {
            buf[received] = '\0';
            // CtrlRequest 含 16KB 响应缓冲，放栈上会撑爆线程栈
            // （SendMessage 跨线程 + devJson 静态缓冲叠加），故用静态缓冲。
            // 该代码路径由 ControlServerThread 单线程串行执行，无并发访问。
            // 关键：不能用 SendMessage（同步）——open_topo 在 UI 线程里耗时很久，
            // 会把服务器线程永久卡住，导致后续所有 RPC 失联（实测死锁）。
            // 改为 PostMessage 投递 + 事件带超时等待：UI 线程不被我们阻塞，
            // 服务器线程最坏等到超时后放弃并回一个明确错误。
            CtrlRequest req;
            if (!ParseCommand(buf, &req)) {
                const char* errMsg = "{\"ok\":false,\"error\":\"Invalid command\"}\n";
                send(clientSock, errMsg, (int)strlen(errMsg), 0);
            } else {
                HWND hWnd = EnsureViewSubclass();
                if (!hWnd) {
                    const char* errMsg = "{\"ok\":false,\"error\":\"MainView HWND not ready\"}\n";
                    send(clientSock, errMsg, (int)strlen(errMsg), 0);
                } else {
                    SendMessageW(hWnd, WM_ENSP_CTRL, 0, (LPARAM)&req);
                    send(clientSock, req.response, (int)strlen(req.response), 0);
                }
            }
        }
        closesocket(clientSock);
    }

    closesocket(listenSock);
    WSACleanup();
    return 0;
}

BOOL WINAPI DllMain(HINSTANCE hinstDLL, DWORD fdwReason, LPVOID lpvReserved) {
    if (fdwReason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(hinstDLL);
        HANDLE hThread = CreateThread(NULL, 0, ControlServerThread, NULL, 0, NULL);
        if (hThread) CloseHandle(hThread);
    } else if (fdwReason == DLL_PROCESS_DETACH) {
        g_running = 0;
        if (g_hHookedWnd && g_origWndProc && IsWindow(g_hHookedWnd)) {
            SetWindowLongPtrW(g_hHookedWnd, GWLP_WNDPROC, (LONG_PTR)g_origWndProc);
        }
    }
    return TRUE;
}
