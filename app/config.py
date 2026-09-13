"""
应用配置：日志、路径常量、eNSP/华为设备默认参数。

本模块是依赖树的叶子节点——不依赖项目内任何其他模块，
其余所有模块都可以安全地从这里导入。
"""

import logging
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ensp-web")

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
DEVICES_FILE = BASE_DIR / "devices.json"
STATIC_DIR = BASE_DIR / "static"
FRONTEND_DIST = BASE_DIR / "frontend" / "dist"  # React + Mantine 新版前端构建产物

# ---------------------------------------------------------------------------
# 华为设备默认 SSH 配置
# ---------------------------------------------------------------------------
HUAWEI_COMMANDS = [
    "display version",
    "display ip interface brief",
    "display current-configuration",
    "display vlan",
    "display ip routing-table",
    "display ospf peer",
    "display lldp neighbor brief",
    "display device",
    "display cpu-usage",
    "display memory-usage",
]

# 快速采集命令（跳过路由表、CPU、内存等非必要信息）。
# 必须获取完整配置：旧版多管道 section 语法不被 VRP 支持，而且会遗漏接口
# 子视图中的 OSPF/NAT/DHCP 等配置。
HUAWEI_COMMANDS_FAST = [
    "display version",
    "display ip interface brief",
    "display current-configuration",
    "display vlan",
    "display ospf peer",
    "display lldp neighbor brief",
    "display device",
]

# 大输出命令 — 使用 send_command_timing 代替 send_command，避免 prompt 等待超时
# 这些命令在 eNSP 模拟器上输出量大，send_command 反复读 prompt 效率很低
HEAVY_OUTPUT_COMMANDS = {
    "display current-configuration",
    "display current-configuration | section",
    "display ip routing-table",
}

# 并行采集命令分组 — 按预期耗时均衡分配到 3 个 SSH 会话
# 会话 A: 快命令（设备信息）
PARALLEL_CMD_GROUP_A = [
    "display version",
    "display device",
    "display cpu-usage",
    "display memory-usage",
]
# 会话 B: 重命令（配置和路由表，大输出）
PARALLEL_CMD_GROUP_B = [
    "display current-configuration",
    "display ip routing-table",
]
# 会话 C: 中速命令（接口/邻居表）
PARALLEL_CMD_GROUP_C = [
    "display ip interface brief",
    "display vlan",
    "display ospf peer",
    "display lldp neighbor brief",
]

# 快速模式并行命令分组（无路由表、CPU/内存）
PARALLEL_CMD_GROUP_A_FAST = [
    "display version",
    "display device",
]
PARALLEL_CMD_GROUP_B_FAST = [
    "display current-configuration",
]
PARALLEL_CMD_GROUP_C_FAST = [
    "display ip interface brief",
    "display vlan",
    "display ospf peer",
    "display lldp neighbor brief",
]

DEFAULT_SSH_USERNAME = "admin"
DEFAULT_SSH_PASSWORD = "Huawei@123"
DEFAULT_DEVICE_TYPE = "huawei"
SSH_TIMEOUT = 10
SSH_READ_TIMEOUT = 15
SSH_RETRY_COUNT = 2

# eNSP 管理网卡识别关键词
ENSP_KEYWORDS = ["Loopback", "VirtualBox", "Host-Only", "VMware", "KM-TEST", "NPCAP"]
EXCLUDED_INTERFACE_KEYWORDS = ["Loopback Pseudo-Interface", "Mihomo", "Meta Tunnel"]
EXPECTED_MANAGEMENT_IP = "192.168.100.1"

# 应用版本号（单一来源，供 FastAPI 与 /api/health 共用）
APP_VERSION = "3.0.0"
