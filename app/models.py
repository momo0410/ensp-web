"""
API 请求体数据模型（Pydantic）。

全部为路由层共用的请求模型。仅依赖 app/config.py（SSH 默认值）
与 services/config_clone_service（配置复制的默认功能域）。
"""

from typing import List, Optional

from pydantic import BaseModel, Field

from app.config import (
    DEFAULT_DEVICE_TYPE,
    DEFAULT_SSH_PASSWORD,
    DEFAULT_SSH_USERNAME,
)
from services.config_clone_service import DEFAULT_DOMAINS as CLONE_DEFAULT_DOMAINS


class DeviceAdd(BaseModel):
    """手动添加设备的请求体"""
    name: str = Field(..., min_length=1, description="设备名称")
    ip: str = Field(..., description="管理 IP 地址")
    username: str = Field(default=DEFAULT_SSH_USERNAME)
    password: str = Field(default=DEFAULT_SSH_PASSWORD)
    device_type: str = Field(default=DEFAULT_DEVICE_TYPE)
    console_port: Optional[int] = Field(default=None, description="eNSP 本地 Console 端口号（可选，用于零 IP 直连配置）")


class InterfaceSelect(BaseModel):
    """选择管理网卡的请求体"""
    name: str
    ip: str
    netmask: str


class ScanRequest(BaseModel):
    """扫描网段的请求体"""
    subnet: str


class PingRequest(BaseModel):
    """Ping 测试的请求体"""
    ip: str


class DeviceDiscoverRequest(BaseModel):
    """自动发现设备的请求体"""
    subnet: Optional[str] = None
    username: str = Field(default=DEFAULT_SSH_USERNAME)
    password: str = Field(default=DEFAULT_SSH_PASSWORD)
    device_type: str = Field(default=DEFAULT_DEVICE_TYPE)
    replace: bool = True
    ssh_identify: bool = True
    dry_run: bool = False
    max_workers: int = Field(default=50, ge=1, le=200)


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
