"""扫描本机 eNSP 设备 Console 端口（只读，不改配置）。

eNSP 每个设备启动后会监听 127.0.0.1 的一个 telnet 端口（默认从 2000 起）。
本脚本只做 TCP 连接 + 读取欢迎 banner，确认可达性与设备类型，不发送任何命令。
"""
import socket, sys

PORTS = range(2000, 2031)  # eNSP 常用 Console 端口区间，可按需扩大
TIMEOUT = 0.6

def probe(port):
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=TIMEOUT)
        s.settimeout(0.8)
        banner = b""
        try:
            while len(banner) < 200:
                chunk = s.recv(4096)
                if not chunk:
                    break
                banner += chunk
                # 常见欢迎符：<Huawei> / <设备名> / Username: / 空等
                if b">" in banner or b"login" in banner.lower() or b"Password" in banner:
                    break
        except socket.timeout:
            pass
        s.close()
        return banner
    except (OSError, ConnectionRefusedError):
        return None

def main():
    print("=== eNSP Console 端口扫描（127.0.0.1, 2000-2030）===")
    found = []
    for p in PORTS:
        b = probe(p)
        if b is not None:
            txt = b.decode("gbk", "replace").replace("\r", "").replace("\n", " ")
            found.append((p, txt.strip()[:80]))
            print(f"  [OPEN] port {p}: {txt.strip()[:80]}")
    if not found:
        print("  未发现任何开放端口。请确认：eNSP 已启动 / 至少一台设备已开机（右键设备->启动）")
        print("  若设备已启动仍无端口，可能是非默认区间，请告诉我具体端口号。")
    else:
        print(f"\n发现 {len(found)} 个在线 Console 端口。下一步可做只读 ? 探测确认设备类型。")


if __name__ == "__main__":
    main()
