"""
API 请求体数据模型（Pydantic）。

全部为路由层共用的请求模型。仅依赖 app/config.py（设备默认参数）
与 services/config_clone_service（配置复制的默认功能域）。
"""

from typing import List, Optional

from pydantic import BaseModel, Field

from app.config import (
    DEFAULT_DEVICE_TYPE,
    DEFAULT_DEVICE_PASSWORD,
    DEFAULT_DEVICE_USERNAME,
)
from services.config_clone_service import DEFAULT_DOMAINS as CLONE_DEFAULT_DOMAINS


class DeviceAdd(BaseModel):
    """添加设备的请求体（支持自动分配 Console 端口并在 eNSP 中自动开机）"""
    name: str = Field(..., min_length=1, description="设备名称")
    ip: str = Field(default="", description="设备标识或本地地址（留空自动生成 127.0.0.1:<console_port>）")
    model: str = Field(default="Router", description="eNSP 设备型号（Router/S5700/S3700/USG5500/AR2220/AR1220/AC6005/AP6050）")
    username: str = Field(default=DEFAULT_DEVICE_USERNAME)
    password: str = Field(default=DEFAULT_DEVICE_PASSWORD)
    device_type: str = Field(default=DEFAULT_DEVICE_TYPE)
    console_port: Optional[int] = Field(default=None, description="eNSP 本地 Console 端口号（留空自动从 2000 起分配）")
    auto_start: bool = Field(default=True, description="添加后是否自动同步 .topo 并在 eNSP 中自动开机")
    cx: Optional[float] = Field(default=None, description="拓扑中横坐标（留空自动排布）")
    cy: Optional[float] = Field(default=None, description="拓扑中纵坐标（留空自动排布）")
    # 终端设备（PC/Laptop/MCS/STA）的网络参数；网络设备留空
    ip_address: Optional[str] = Field(default=None, description="终端设备 IP 地址")
    netmask: Optional[str] = Field(default=None, description="终端设备子网掩码")
    gateway: Optional[str] = Field(default=None, description="终端设备网关")
    revision: Optional[int] = Field(
        default=None,
        description="客户端持有的设备表版本号（乐观锁）；不匹配则拒绝写入，避免覆盖他人改动",
    )


class LinkEndpoint(BaseModel):
    """连线的一个端点。"""
    device: str = Field(..., min_length=1, description="设备名称")
    interface: str = Field(..., min_length=1, description="接口名，如 GE0/0/0")


class LinkAdd(BaseModel):
    """新增连线的请求体。"""
    a: LinkEndpoint
    b: LinkEndpoint


class TopologyActivate(BaseModel):
    """切换/新建拓扑的请求体。"""
    name: str = Field(..., min_length=1, description="拓扑名称")


class WorkspaceSet(BaseModel):
    """设置工作目录的请求体。"""
    path: str = Field(..., min_length=1, description="工作目录绝对路径")


class EnspTopoApplyRequest(BaseModel):
    """一键构建/加载拓扑并自动开机的请求体"""
    preset: Optional[str] = Field(default=None, description="内置拓扑预设名，如 comprehensive_campus")
    revision: Optional[int] = Field(
        default=None,
        description="客户端持有的设备表版本号（乐观锁）；仅加载预设时校验，不匹配则拒绝覆盖",
    )
    name: str = Field(default="active_workspace", description="拓扑名称")
    devices: List[dict] = Field(default_factory=list, description="设备列表")
    links: Optional[List[dict]] = Field(default=None, description="连线列表")
    auto_start: bool = Field(default=True, description="是否自动启动全部设备")


class PathTraceRequest(BaseModel):
    """逻辑路径追踪请求体（需求④：数据包走向）。"""
    source_ip: str = Field(..., description="源 IPv4（用于定位起始设备）")
    dest_ip: str = Field(..., description="目的 IPv4")
    device_ips: List[str] = Field(default_factory=list, description="参与追踪的设备 IP；留空=全部")
    live: bool = Field(default=False, description="是否临时采集对应设备的表（缺采集数据时用）")


class PingRequest(BaseModel):
    """Ping 测试的请求体"""
    ip: str


class EnspAttachRequest(BaseModel):
    """把 eNSP 当前拓扑对齐到项目的请求体（需求②反向）。"""
    mode: str = Field(
        default="match_by_devices",
        description="match_by_devices=按设备名匹配已有拓扑并切换；import_as=导入为新拓扑",
    )
    name: Optional[str] = Field(default=None, description="import_as 模式下新拓扑的名称（留空自动生成）")


class ProtocolConfigRequest(BaseModel):
    """协议配置请求体"""
    mode: str = Field(default="form", description="配置模式: form 或 command")
    params: dict = Field(default={}, description="表单模式下的参数")
    commands: List[str] = Field(default=[], description="命令行模式下的命令列表")


class ConfigPreviewRequest(BaseModel):
    """配置预览请求体"""
    config_type: str = Field(..., description="配置类型标识")
    params: dict = Field(default={}, description="表单参数")


class ConfigDeployRequest(BaseModel):
    """配置下发请求体"""
    config_type: str = Field(..., description="配置类型标识")
    params: dict = Field(default={}, description="表单参数")
    save_config: bool = Field(default=False, description="下发后是否保存配置")


class RawPreviewRequest(BaseModel):
    """原始命令预览请求体"""
    commands: List[str] = Field(default=[], description="原始命令列表")


class RawDeployRequest(BaseModel):
    """原始命令下发请求体"""
    commands: List[str] = Field(default=[], description="原始命令列表")
    save_config: bool = Field(default=False, description="下发后是否保存配置")
    confirm: bool = Field(default=False, description="二次确认标志")


class ClonePreviewRequest(BaseModel):
    """设备配置复制预览请求体"""
    source_device: str
    target_devices: List[str] = []
    domains: List[str] = Field(default_factory=lambda: list(CLONE_DEFAULT_DOMAINS))
    auto_resolve: bool = True
    include_sensitive: bool = False
    refresh_config: bool = True


class CloneDeployRequest(BaseModel):
    """设备配置复制下发请求体"""
    preview_id: str
    confirm: bool = False
    save_config: bool = False
