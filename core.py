"""
eNSP Web 网络设备管理系统
=========================
通过 SSH 连接 eNSP 中的华为路由器/交换机，采集设备信息并展示在 Web 页面。

架构说明：
  - FastAPI 后端提供 RESTful API
  - psutil 读取本机网卡信息
  - Netmiko 执行 SSH 设备采集
  - JSON 文件持久化设备列表（带 filelock 并发保护）
  - 采集结果常驻内存，重启后需重新采集

注意事项：
  - 必须运行在安装 eNSP 的 Windows 主机上
  - 后端通过 SSH 连接设备管理 IP，不是连接 eNSP 软件
  - Web 前端无法直接读取网卡，由 Python 后端完成
"""

import json
import logging
import os
import re
import socket
import subprocess
import sys
import ipaddress
import concurrent.futures
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

import psutil
from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from services.capability_probe import probe_capabilities, probe_capabilities_parallel, probe_capabilities_from_cache
from services.role_detector import detect_protocol_roles
from services import ai_assistant
from services.ai_assistant import AIError
from services.access_guard import access_guard, ACCESS_ENABLED, ACCESS_TOKEN
from services.live_check import ping_from_device
from services import state_persist
from services.config_builder import (
    build_preview,
    build_config_commands,
    get_verify_commands,
    get_templates_for_role,
    get_all_templates,
    CONFIG_TEMPLATES,
)
from services.command_catalog import (
    get_catalog_entries,
    get_catalog_entry,
    render_catalog_commands,
    render_catalog_verify,
    get_categories,
)
from services.command_probe import probe_device_commands, get_cached_probe, cache_key
from services.help_tree import discover_via_ssh
from services.backup_service import (
    backup_device_config,
    list_backups,
    get_backup_content,
)
from services.deploy_service import deploy_config, deploy_raw_commands
from services.free_config_renderer import render_preview, validate_task
from services.free_config_executor import execute_free_deploy
from services.script_service import list_scripts, get_script, save_script, delete_script
from services.task_history_service import save_task_record, list_tasks, get_task, generate_task_id
from services.smart_defaults import (
    network_to_wildcard,
    generate_batch_smart_defaults,
    infer_wildcard_from_input,
)
from services.conflict_detector import check_cross_device_conflicts
from services.config_clone_service import (
    DEFAULT_DOMAINS as CLONE_DEFAULT_DOMAINS,
    DOMAIN_NAMES as CLONE_DOMAIN_NAMES,
    build_clone_preview,
)
from services.device_connector import command_result_from_output

# ---------------------------------------------------------------------------
# 配置 / 共享状态 / 数据模型 —— 已迁移至 app/，此处仅转发保持兼容
# ---------------------------------------------------------------------------
from app.config import *  # noqa: F401,F403,E402
from app.config import logger, BASE_DIR, DEVICES_FILE, STATIC_DIR, FRONTEND_DIST  # noqa: F401,E402
from app.state import state, init_state, _lab_sessions  # noqa: F401,E402
from app.models import (  # noqa: F401,E402
    DeviceAdd,
    InterfaceSelect,
    ScanRequest,
    PingRequest,
    DeviceDiscoverRequest,
    ProtocolConfigRequest,
    ConfigPreviewRequest,
    ConfigDeployRequest,
    RawPreviewRequest,
    RawDeployRequest,
    ClonePreviewRequest,
    CloneDeployRequest,
)


# ---------------------------------------------------------------------------
# 设备列表持久化 —— 已迁移至 domain/device_store.py，此处仅转发保持兼容
# ---------------------------------------------------------------------------
from domain.device_store import (  # noqa: E402,F401
    load_devices,
    save_devices,
    _do_load_devices,
    _do_save_devices,
    _acquire_lock,
)


# ---------------------------------------------------------------------------
# 网络工具函数
# ---------------------------------------------------------------------------
# 网络工具函数 —— 已迁移至 domain/netprobe.py，此处仅转发保持兼容
from domain.netprobe import (  # noqa: E402,F401
    ping_host,
    tcp_port_open,
    ssh_service_open,
    probe_subnet,
    scan_subnet,
    _ip_sort_key,
    _local_ipv4_addresses,
    _same_ipv4_network,
    _management_source_ip_for,
    _create_management_socket,
)

# Console 通道 —— 已迁移至 domain/console.py，此处仅转发保持兼容
from domain.console import (  # noqa: E402,F401
    _console_recv_all,
    _console_send_command,
    _parse_console_prompt,
    _strip_command_echo,
    _console_probe_port,
    _list_listening_console_ports,
    diagnose_ensp_console_ports,
    console_deploy,
    _console_save,
)

# 网段扫描 —— 已迁移至 domain/netprobe.py，此处通过上方 netprobe 转发

# ---------------------------------------------------------------------------
# SSH 设备采集
# ---------------------------------------------------------------------------






# ---------------------------------------------------------------------------
# SSH 设备采集 / 设备发现 —— 已迁移至 domain/，此处仅转发保持兼容
# ---------------------------------------------------------------------------
from domain.ssh_collect import (  # noqa: E402,F401
    _import_netmiko,
    _parse_prompt_name,
    _default_discovered_name,
    ssh_identify_device,
    fast_send_command,
    ssh_collect_device,
    _ssh_run_command_group,
    ssh_collect_device_parallel,
)
from domain.lldp import (  # noqa: E402,F401
    _valid_remote_mgmt_ip,
    _lldp_neighbor_inventory,
    _sync_lldp_devices,
)
from domain.discovery import discover_devices_from_subnet  # noqa: E402,F401
from domain.config_push import (  # noqa: E402,F401
    generate_delete_commands,
    push_config_to_device,
)
from domain.topology import generate_topology  # noqa: E402,F401


# ---------------------------------------------------------------------------
# API 路由
# ---------------------------------------------------------------------------
# 网卡枚举 —— 已迁移至 domain/interfaces.py，此处仅转发保持兼容
# ---------------------------------------------------------------------------
from domain.interfaces import (  # noqa: E402,F401
    _get_windows_adapter_metadata,
    _is_link_local_ipv4,
    _is_loopback_ipv4,
    _pick_best_ipv4,
    _rank_interface,
    _finalize_interfaces,
)


# 供路由模块 from core import * 一次性拿到全部符号（含下划线私有助手）
__all__ = [n for n in globals() if not n.startswith("__")]
