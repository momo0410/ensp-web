"""真机 ? 探测验证：直接调用项目自身的 probe_device_commands（Console 通道，只读）。"""
import sys, json
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from services.command_probe import probe_device_commands

PORTS = [2000, 2001, 2002, 2003, 2006, 2007]


def main():

    for p in PORTS:
        print(f"\n========== 端口 {p} ==========")
        try:
            r = probe_device_commands({"mode": "console", "port": p}, force=True)
        except Exception as e:
            print("  探测异常:", repr(e))
            continue
        print("  device_type:", r.get("device_type"))
        print("  model      :", r.get("model"))
        print("  keyword_count:", r.get("keyword_count"))
        print("  supported  :", r.get("supported"))
        print("  unsupported:", r.get("unsupported"))
        print("  errors     :", r.get("errors"))


if __name__ == "__main__":
    main()
