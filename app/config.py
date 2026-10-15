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
DEVICES_FILE = BASE_DIR / "devices.json"          # 旧路径（首次运行时迁移进工作目录）
APP_SETTINGS_FILE = BASE_DIR / "app_settings.json"  # 用户设置（工作目录等）
STATIC_DIR = BASE_DIR / "static"
FRONTEND_DIST = BASE_DIR / "frontend" / "dist"  # React + Mantine 新版前端构建产物

# ---------------------------------------------------------------------------
# 设备默认连接参数（本地 Console 通道）
# ---------------------------------------------------------------------------
HUAWEI_COMMANDS = [
    "display version",
    "display ip interface brief",
    "display current-configuration",
    "display vlan",
    "display ip routing-table",
    "display ospf peer",
    "display device",
    "display cpu-usage",
    "display memory-usage",
]

# 快速采集命令（跳过路由表、CPU、内存等非必要信息）。
# 必须获取完整配置：旧版多管道 section 语法不被 VRP 支持，而且会遗漏接口
# 子视图中的 OSPF/NAT/DHCP 等配置。
#
# 后三项（arp/mac-address/ip routing-table）是逻辑路径追踪（services/path_trace.py）
# 的数据来源：没有它们就无法回答"包从哪走到哪、在哪断"。
# 三条都是零副作用的只读查询，eNSP 镜像上执行开销可接受。
HUAWEI_COMMANDS_FAST = [
    "display version",
    "display ip interface brief",
    "display current-configuration",
    "display vlan",
    "display ospf peer",
    "display device",
    "display arp all",
    "display mac-address",
    "display ip routing-table",
]

# 大输出命令 — 使用 send_command_timing 代替 send_command，避免 prompt 等待超时
# 这些命令在 eNSP 模拟器上输出量大，send_command 反复读 prompt 效率很低
HEAVY_OUTPUT_COMMANDS = {
    "display current-configuration",
    "display current-configuration | section",
    "display ip routing-table",
}

DEFAULT_DEVICE_USERNAME = "admin"
DEFAULT_DEVICE_PASSWORD = "Huawei@123"
DEFAULT_DEVICE_TYPE = "huawei"
CONN_TIMEOUT = 10
CONN_READ_TIMEOUT = 15
CONN_RETRY_COUNT = 2

# 应用版本号（单一来源，供 FastAPI 与 /api/health 共用）
APP_VERSION = "3.0.0"
