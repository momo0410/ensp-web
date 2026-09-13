"""D 验证：零配置设备经 Console 通道表单初始化（端到端）"""
import socket, threading, time, sys, asyncio
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

# ---- Mock VRP Console (telnet localhost) ----
received = []

def vrp_respond(cmd):
    cmd = cmd.strip()
    if cmd == "system-view":
        return "Enter system view, return user view with Ctrl+Z.\r\n[Huawei]"
    if cmd == "return":
        return "\r\n<Huawei>"
    if cmd == "save":
        return "\r\nThe current configuration will be saved to the device.\r\nAre you sure? (y/n)[n]: "
    if cmd == "y":
        return "\r\nNow saving the current configuration to the device.\r\nSaved the current configuration successfully.\r\n<Huawei>"
    if cmd.startswith("display") or cmd.startswith("screen-length"):
        return f"\r\n{cmd}\r\n[Huawei]"
    return f"\r\n{cmd}\r\n[Huawei]"

def handle(conn):
    try:
        conn.sendall(b"<Huawei>\r\n")
        buf = b""
        while True:
            try:
                data = conn.recv(4096)
            except Exception:
                break
            if not data:
                break
            buf += data
            while b"\r" in buf:
                line, buf = buf.split(b"\r", 1)
                cmd = line.decode("gbk", "replace").replace(b"\n".decode(), "").strip()
                if cmd == "":
                    continue
                received.append(cmd)
                resp = vrp_respond(cmd).encode("gbk", "replace")
                time.sleep(0.04)
                try:
                    conn.sendall(resp)
                except Exception:
                    break
    finally:
        try:
            conn.close()
        except Exception:
            pass

def start_mock(port=2001):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(5)
    def accept():
        while True:
            try:
                conn, _ = srv.accept()
            except Exception:
                break
            threading.Thread(target=handle, args=(conn,), daemon=True).start()
    threading.Thread(target=accept, daemon=True).start()
    time.sleep(0.3)
    return srv

import httpx
import main

start_mock(7777)

async def run():
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        print("=== D: 零配置设备经 Console 通道表单初始化 ===")
        # 1) 渲染预览
        r = await client.post("/api/command-catalog/render", json={
            "key": "interface_ip",
            "params": {"interface": "GigabitEthernet0/0/0", "ip_address": "192.168.1.1", "subnet_mask": "255.255.255.0"},
        })
        print("render:", r.json())

        # 2) 经 Console 通道下发（无 IP，仅端口）
        r2 = await client.post("/api/command-catalog/deploy", json={
            "key": "interface_ip",
            "params": {"interface": "GigabitEthernet0/0/0", "ip_address": "192.168.1.1", "subnet_mask": "255.255.255.0"},
            "console_port": 7777,
            "save_config": True,
        })
        j2 = r2.json()
        print("deploy status:", r2.status_code)
        print("deploy success:", j2.get("success"), "| channel:", j2.get("channel"))
        print("console 收到命令条数:", len([c for c in j2.get("command_outputs", []) if c.get("success")]))
        print("save_result 非空:", bool(j2.get("save_result")))

        # 3) 验证零 IP 设备初始化的另一条：ssh_user
        r3 = await client.post("/api/command-catalog/deploy", json={
            "key": "ssh_user",
            "params": {"username": "admin", "password": "Huawei@123", "privilege_level": "15"},
            "console_port": 7777,
            "save_config": False,
        })
        j3 = r3.json()
        print("\nssh_user deploy success:", j3.get("success"), "| channel:", j3.get("channel"))

    print("\nMock Console 收到的命令序列（节选前 12 条）:")
    for c in received[:12]:
        print("   >", c)
    print("\n[DONE] Console 通道零配置设备表单初始化验证完成。")

asyncio.run(run())

if __name__ == "__main__":
    asyncio.run(run())
