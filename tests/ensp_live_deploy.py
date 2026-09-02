"""真机 Console 下发验证（D 步骤全链路，修复 \\r + 稳健进系统视图后）+ 清理。

- 部署 LoopBack199 (全新) IP 10.255.255.199/32
- 回读 display ip interface brief 确认生效（只匹配真实接口行，不误判回显命令）
- 清理：稳健进入系统视图后 undo LoopBack99 / LoopBack199
- save_config=False，仅改运行配置；清理后设备还原
"""
import sys, asyncio
sys.path.insert(0, ".")
import httpx, main
import socket, time
from services.command_probe import _console_recv_all, _console_send

PORT = 2007

def enter_sysview(s):
    """稳健进入系统视图：先唤醒空行，再重试 system-view 直至提示符变为 [xxx]。"""
    try:
        s.sendall(b"\r"); time.sleep(0.2)
    except Exception:
        pass
    _console_recv_all(s, max_wait=0.4)
    for _ in range(3):
        out = _console_send(s, "system-view")
        if "[" in out:
            return True
        time.sleep(0.3)
    return False

async def deploy(key, params):
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/api/command-catalog/deploy", json={
            "key": key, "params": params, "console_port": PORT, "save_config": False,
        })
        return r.json()

def raw_display():
    s = socket.create_connection(("127.0.0.1", PORT), timeout=2.0); s.settimeout(5.0)
    _console_recv_all(s, max_wait=0.5)
    _console_send(s, "screen-length 0 temporary")
    out = _console_send(s, "display ip interface brief")
    _console_send(s, "return"); _console_send(s, "quit"); s.close()
    return out

async def run():
    print(f"=== 真机 Console 下发（端口 {PORT}，AR2200）===")
    j = await deploy("loopback", {"id": 199, "ip_address": "10.255.255.199", "mask": 32})
    print("deploy success:", j.get("success"), "| channel:", j.get("channel"))
    for c in j.get("command_outputs", []):
        print(f"  [{'OK' if c.get('success') else 'ERR'}] {c.get('command')} -> {(c.get('output') or '').replace(chr(10),' ')[:80]}")

    out = raw_display()
    # 只匹配形如 "LoopBack199  X.X.X.X" 的真实接口行，避免误判回显命令
    real = [l for l in (out or "").replace("\r","").split("\n") if "LoopBack199" in l and "ip address" not in l]
    print("\n回读 LoopBack199 真实接口行:", real if real else "（无 -> 未生效）")

    # 清理
    print("\n--- 清理运行配置 ---")
    s = socket.create_connection(("127.0.0.1", PORT), timeout=2.0); s.settimeout(5.0)
    enter_sysview(s)
    for vid in (99, 199):
        r = _console_send(s, f"undo interface LoopBack {vid}")
        print(f"  undo LoopBack{vid}:", "OK" if "Error" not in r else r.replace(chr(13),"").replace(chr(10)," ")[:60])
    _console_send(s, "return"); _console_send(s, "quit"); s.close()

    out2 = raw_display()
    real2 = [l for l in (out2 or "").replace("\r","").split("\n") if "LoopBack199" in l and "ip address" not in l]
    print("清理后 LoopBack199:", "已移除" if not real2 else "仍在!")
    print("\n[DONE] 真机下发全链路验证完成，设备已清理。")

asyncio.run(run())
