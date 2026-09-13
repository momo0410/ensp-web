# -*- coding: utf-8 -*-
"""全量只读探测（健壮版）：识别 127.0.0.1:2000-2030 上所有在线 eNSP 设备。
- 处理 ---- More ---- 分页（自动补空格翻页）
- 过滤 ANSI 转义序列
- 关闭 terminal monitor 噪声
仅只读，不改动任何配置。每端口结果写入 tests/inventory/port_NNN.txt，清单写入 tests/inventory/SUMMARY.txt
"""
import socket, time, os, re

OUT_DIR = str(__import__("pathlib").Path(__file__).resolve().parent / "inventory")
os.makedirs(OUT_DIR, exist_ok=True)

PROBE_CMDS = [
    "display version",
    "display current-configuration | include sysname",
    "display ip interface brief",
    "display interface brief",
    "display vlan",
    "display port vlan",
    "display ap all",
    "display device",
]

ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[a-zA-Z]')
CR_RE = re.compile(r'\r')

def strip_ansi(txt):
    txt = ANSI_RE.sub('', txt)
    txt = txt.replace('\x1b', '')
    return txt

def recv_chunk(sock, idle=0.3, total=12):
    sock.settimeout(0.2)
    buf = b""
    last = time.time()
    while True:
        try:
            data = sock.recv(8192)
        except socket.timeout:
            if time.time() - last >= idle:
                break
            continue
        if not data:
            break
        buf += data
        last = time.time()
        if time.time() - last > total:
            break
    return buf

def run_cmd(sock, cmd, idle=0.6):
    try:
        sock.sendall((cmd + "\r").encode("ascii", "ignore"))
    except Exception as e:
        return f"__send_err__{e}"
    time.sleep(0.08)
    out = b""
    while True:
        buf = recv_chunk(sock, idle=idle)
        out += buf
        if b"---- More ----" in out:
            # 翻页：发空格继续
            try:
                sock.sendall(b" ")
            except Exception:
                pass
            time.sleep(0.08)
            continue
        break
    return strip_ansi(out.decode("utf-8", "ignore"))

def probe_port(port):
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=2.0)
    except Exception as e:
        return None, f"connect_fail: {e}"
    try:
        sock.settimeout(3.0)
        time.sleep(0.3)
        recv_chunk(sock, idle=0.6)
        sock.sendall(b"\r")
        recv_chunk(sock, idle=0.4)
        run_cmd(sock, "screen-length 0 temporary", idle=0.4)
        run_cmd(sock, "undo terminal monitor", idle=0.4)
        parts = []
        for c in PROBE_CMDS:
            out = run_cmd(sock, c, idle=0.8)
            parts.append(f"\n===== [{c}] =====\n{out}")
        try:
            sock.sendall(b"quit\r")
        except Exception:
            pass
        return "".join(parts), "ok"
    except Exception as e:
        return None, f"probe_err: {e}"
    finally:
        try:
            sock.close()
        except Exception:
            pass

def main():
    summary = []
    count = 0
    for port in range(2000, 2031):
        txt, status = probe_port(port)
        if txt is None:
            summary.append(f"port {port}: {status}")
            continue
        low = txt.lower()
        is_dev = ("huawei" in low) or ("vrp" in low) or ("<" in txt) or ("version" in low)
        if not is_dev:
            summary.append(f"port {port}: no_device")
            with open(os.path.join(OUT_DIR, f"port_{port}_none.txt"), "w", encoding="utf-8") as f:
                f.write(txt)
            continue
        # 解析关键信息
        sysname = "?"
        m = re.search(r"sysname\s+(\S+)", txt)
        if m:
            sysname = m.group(1)
        model = "?"
        m = re.search(r"(S\d{4}[^\r\n]*|AR\d{3,4}[^\r\n]*|USG\d{3,4}[^\r\n]*|AC\d{3,4}[^\r\n]*)", txt)
        if m:
            model = m.group(1).strip()
        dtype = "?"
        if "Routing Switch" in txt or "S5700" in txt or "S3700" in txt or "S2700" in txt:
            dtype = "Switch"
        elif "AC " in txt or "Access Controller" in txt or re.search(r"\bAC\d", txt):
            dtype = "AC"
        elif "USG" in txt or "Firewall" in txt or "Secospace" in txt:
            dtype = "Firewall"
        else:
            dtype = "Router"
        count += 1
        summary.append(f"port {port}: {dtype:9s} | sysname={sysname:12s} | model={model}")
        with open(os.path.join(OUT_DIR, f"port_{port}.txt"), "w", encoding="utf-8") as f:
            f.write(txt)
    with open(os.path.join(OUT_DIR, "SUMMARY.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(summary) + f"\n\n=== 共 {count} 台在线设备 ===\n")
    # 同时打印（无管道缓冲时可见）
    print("\n".join(summary))
    print(f"\n=== 共 {count} 台在线设备 ===")

if __name__ == "__main__":
    main()
