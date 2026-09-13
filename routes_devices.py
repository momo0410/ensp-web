"""路由模块（由 main.py 机械拆分，逻辑未改动）。"""
from fastapi import APIRouter

from fastapi import HTTPException
from typing import List
import concurrent.futures
import time

from app.config import logger
from app.models import DeviceAdd, PingRequest, ProtocolConfigRequest
from app.state import state
from domain.config_push import generate_delete_commands, push_config_to_device
from domain.device_store import load_devices, save_devices
from domain.lldp import _sync_lldp_devices
from domain.netprobe import ping_host
from domain.parse import _parse_config_sections, _parse_device_info  # noqa: F401  # 供测试引用
from domain.ssh_collect import ssh_collect_device, ssh_collect_device_parallel
from services.config_builder import build_config_commands

router = APIRouter()

import routes_config  # noqa: E402
@router.get("/api/devices")
async def get_devices():
    """获取设备列表。"""
    devices = load_devices()
    return {"success": True, "devices": devices}


@router.post("/api/devices")
async def add_device(device: DeviceAdd):
    """手动添加设备。"""
    devices = load_devices()
    for d in devices:
        if d["ip"] == device.ip:
            raise HTTPException(status_code=400, detail=f"设备 {device.ip} 已存在")
    new_device = device.model_dump()
    devices.append(new_device)
    save_devices(devices)
    logger.info(f"添加设备: {device.name} ({device.ip})")
    return {"success": True, "device": new_device, "message": f"设备 {device.name}({device.ip}) 已添加"}


@router.put("/api/devices/{ip}")
async def update_device(ip: str, device: DeviceAdd):
    """更新设备连接配置。"""
    devices = load_devices()
    target_index = next((idx for idx, d in enumerate(devices) if d["ip"] == ip), None)
    if target_index is None:
        raise HTTPException(status_code=404, detail=f"未找到 IP 为 {ip} 的设备")

    if device.ip != ip and any(d["ip"] == device.ip for d in devices):
        raise HTTPException(status_code=400, detail=f"设备 {device.ip} 已存在")

    updated_device = device.model_dump()
    devices[target_index] = updated_device
    save_devices(devices)

    if device.ip != ip and ip in state["collection_results"]:
        state["collection_results"][device.ip] = state["collection_results"].pop(ip)
        state["collection_results"][device.ip]["ip"] = device.ip
        state["collection_results"][device.ip]["name"] = device.name

    logger.info(f"更新设备配置: {ip} -> {device.name} ({device.ip})")
    return {"success": True, "device": updated_device, "message": f"设备 {device.name} 配置已保存"}


@router.delete("/api/devices/{ip}")
async def delete_device(ip: str):
    """删除指定 IP 的设备。"""
    devices = load_devices()
    new_devices = [d for d in devices if d["ip"] != ip]
    if len(new_devices) == len(devices):
        raise HTTPException(status_code=404, detail=f"未找到 IP 为 {ip} 的设备")
    save_devices(new_devices)
    state["collection_results"].pop(ip, None)
    logger.info(f"删除设备: {ip}")
    return {"success": True, "message": f"设备 {ip} 已删除"}


def _has_valid_full_config(result: dict) -> bool:
    command = result.get("commands", {}).get("display current-configuration", {})
    return bool(command.get("success") and command.get("output"))


def _ensure_protocol_collection(ip: str) -> dict:
    """确保协议读取使用完整配置；实时失败时回退到最新完整备份。"""
    result = state["collection_results"].get(ip)
    if result and _has_valid_full_config(result):
        return result

    device = next((d for d in load_devices() if d["ip"] == ip), None)
    if not device:
        raise HTTPException(status_code=404, detail=f"未找到 IP 为 {ip} 的设备")

    loaded = routes_config._load_config_for_clone(device, refresh=True)
    if not loaded.get("success"):
        raise HTTPException(
            status_code=502,
            detail=f"获取设备配置失败: {'; '.join(loaded.get('errors', [])) or '没有可用的完整配置'}",
        )

    result = dict(result or {
        "ip": ip,
        "name": device.get("name", ip),
        "online": loaded.get("source") == "live",
        "ssh_ok": loaded.get("source") == "live",
        "ssh_error": "",
        "commands": {},
        "info": {},
    })
    result["commands"] = dict(result.get("commands", {}))
    result["commands"]["display current-configuration"] = {
        "success": True,
        "output": loaded["config"],
        "error": None,
    }
    result["info"] = dict(result.get("info", {}))
    _parse_config_sections(result["info"], loaded["config"])
    result["config_source"] = loaded.get("source", "cache")
    result["config_backup_file"] = loaded.get("backup_file", "")
    result["config_warnings"] = loaded.get("errors", [])
    if loaded.get("source") == "live":
        result["online"] = True
        result["ssh_ok"] = True
        result["ssh_error"] = ""
    state["collection_results"][ip] = result
    return result


@router.get("/api/devices/{ip}/protocols")
async def get_device_protocols(ip: str):
    """获取设备已配置的协议列表和完整协议配置。"""
    result = _ensure_protocol_collection(ip)

    info = result.get("info", {})
    protocols = info.get("protocols", [])
    config_sections = info.get("config_sections", {})

    return {
        "success": True,
        "ip": ip,
        "protocols": protocols,
        "config_sections": config_sections,
        "config_source": result.get("config_source", "cache"),
        "config_backup_file": result.get("config_backup_file", ""),
        "warnings": result.get("config_warnings", []),
    }


@router.get("/api/devices/{ip}/protocols/{protocol}")
async def get_device_protocol_config(ip: str, protocol: str):
    """获取指定协议的当前配置。"""
    result = _ensure_protocol_collection(ip)

    config_sections = result.get("info", {}).get("config_sections", {})
    protocol_config = config_sections.get(protocol, {})

    return {
        "success": True,
        "ip": ip,
        "protocol": protocol,
        "config": protocol_config,
        "config_source": result.get("config_source", "cache"),
        "config_backup_file": result.get("config_backup_file", ""),
        "warnings": result.get("config_warnings", []),
    }


@router.get("/api/devices/{ip}/current-config")
async def get_device_current_config(ip: str):
    """获取完整当前配置；实时不可用时返回最新完整备份并标明来源。"""
    result = _ensure_protocol_collection(ip)
    command = result["commands"]["display current-configuration"]
    return {
        "success": True,
        "ip": ip,
        "config": command["output"],
        "source": result.get("config_source", "cache"),
        "backup_file": result.get("config_backup_file", ""),
        "warnings": result.get("config_warnings", []),
    }


@router.post("/api/devices/{ip}/protocols/{protocol}")
async def apply_device_protocol_config(ip: str, protocol: str, req: ProtocolConfigRequest):
    """应用协议配置到设备"""
    devices = load_devices()
    device = next((d for d in devices if d["ip"] == ip), None)
    if not device:
        raise HTTPException(status_code=404, detail=f"未找到 IP 为 {ip} 的设备")

    # 生成配置命令（统一走声明式模板渲染器，消除双数据源）
    if req.mode == "command":
        commands = req.commands
    else:
        commands = build_config_commands(protocol, req.params)

    if not commands:
        raise HTTPException(status_code=400, detail="未生成任何配置命令")

    # 推送配置到设备
    result = push_config_to_device(device, commands)
    if not result.get("success"):
        raise HTTPException(status_code=500, detail=f"配置推送失败: {result.get('error', '未知错误')}")

    # 重新采集设备信息以更新协议状态
    collect_result = ssh_collect_device(device)
    state["collection_results"][ip] = collect_result

    return {
        "success": True,
        "ip": ip,
        "protocol": protocol,
        "commands": commands,
        "output": result.get("output", ""),
    }


@router.delete("/api/devices/{ip}/protocols/{protocol}")
async def delete_device_protocol_config(ip: str, protocol: str):
    """删除协议配置"""
    devices = load_devices()
    device = next((d for d in devices if d["ip"] == ip), None)
    if not device:
        raise HTTPException(status_code=404, detail=f"未找到 IP 为 {ip} 的设备")

    # 生成删除命令
    delete_commands = generate_delete_commands(protocol, device)
    if not delete_commands:
        raise HTTPException(status_code=400, detail=f"不支持删除协议: {protocol}")

    # 推送删除命令到设备
    result = push_config_to_device(device, delete_commands)
    if not result.get("success"):
        raise HTTPException(status_code=500, detail=f"删除配置失败: {result.get('error', '未知错误')}")

    # 重新采集
    collect_result = ssh_collect_device(device)
    state["collection_results"][ip] = collect_result

    return {
        "success": True,
        "ip": ip,
        "protocol": protocol,
        "message": f"已删除 {protocol} 配置",
    }


@router.post("/api/ping")
async def ping_device(req: PingRequest):
    """测试设备连通性。"""
    online = ping_host(req.ip)
    return {
        "success": True,
        "ip": req.ip,
        "online": online,
        "message": "在线" if online else "离线",
    }


@router.post("/api/collect/{ip}")
async def collect_device(ip: str, fast_mode: bool = True, force: bool = True):
    """
    连接指定设备并采集信息。
    fast_mode: 使用快速采集（并行 SSH）
    force: True 强制采集，False 仅在状态变化时采集（增量模式）
    """
    devices = load_devices()
    device = next((d for d in devices if d["ip"] == ip), None)
    if not device:
        raise HTTPException(status_code=404, detail=f"未找到 IP 为 {ip} 的设备")

    # 增量模式：如果上次在线且有数据，直接返回缓存
    if not force:
        dev_status = state["device_status"].get(ip, {})
        if dev_status.get("last_online") and ip in state["collection_results"]:
            return {"success": True, "result": state["collection_results"][ip], "cached": True}

    # ping 检测（2包，~1.5s）
    online = ping_host(ip)
    state["device_status"][ip] = {
        "last_online": online,
        "last_collect_ts": time.time(),
    }

    if not online:
        result = {
            "ip": ip,
            "name": device.get("name", ip),
            "online": False,
            "ssh_ok": False,
            "ssh_error": "设备不可达（Ping 超时）",
            "commands": {},
            "info": {},
        }
        state["collection_results"][ip] = result
        return {"success": True, "result": result}

    result = ssh_collect_device_parallel(device, fast_mode=fast_mode)
    state["collection_results"][ip] = result
    return {"success": True, "result": result}


@router.post("/api/collect-all")
async def collect_all(fast_mode: bool = True, force: bool = True):
    """
    批量采集所有设备信息。

    优化策略：
    - 并行 ping（2 包）+ 并行 SSH 采集
    - 每台设备内部 3 个 SSH 会话并行执行不同命令组
    - 增量模式（force=False）：仅对上次离线→在线的设备重新采集

    Args:
        fast_mode: True（默认）使用快速模式
        force: True（默认）强制全部采集；False 增量模式
    """
    devices = load_devices()
    if not devices:
        return {"success": True, "results": [], "message": "设备列表为空"}

    results: List[dict] = []

    def _collect_single(device: dict) -> dict:
        ip = device["ip"]

        # 增量模式：上次在线且有数据，跳过 SSH 采集
        if not force:
            dev_status = state["device_status"].get(ip, {})
            if dev_status.get("last_online") and ip in state["collection_results"]:
                cached = state["collection_results"][ip]
                cached["cached"] = True
                return cached

        # 并行 ping（2包，~1.5s）
        online = ping_host(ip)
        state["device_status"][ip] = {
            "last_online": online,
            "last_collect_ts": time.time(),
        }

        if not online:
            return {
                "ip": ip,
                "name": device.get("name", ip),
                "online": False,
                "ssh_ok": False,
                "ssh_error": "设备不可达",
                "commands": {},
                "info": {},
            }

        # 并行 SSH 采集（3 个会话同时执行）
        return ssh_collect_device_parallel(device, fast_mode=fast_mode)

    # 并行采集所有设备（每台设备一个线程，内含 3 个 SSH 会话）
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(devices), 10)) as executor:
        future_map = {executor.submit(_collect_single, d): d for d in devices}
        for future in concurrent.futures.as_completed(future_map):
            try:
                result = future.result()
                results.append(result)
                state["collection_results"][result["ip"]] = result
            except Exception as e:
                device = future_map[future]
                logger.error(f"采集 {device['ip']} 时发生未预期异常: {e}")
                err_result = {
                    "ip": device["ip"],
                    "name": device.get("name", device["ip"]),
                    "online": False,
                    "ssh_ok": False,
                    "ssh_error": f"采集异常: {e}",
                    "commands": {},
                    "info": {},
                }
                results.append(err_result)
                state["collection_results"][device["ip"]] = err_result

    lldp_added = _sync_lldp_devices()
    if lldp_added:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(lldp_added), 5)) as executor:
            future_map = {executor.submit(_collect_single, d): d for d in lldp_added}
            for future in concurrent.futures.as_completed(future_map):
                device = future_map[future]
                try:
                    result = future.result()
                    results.append(result)
                    state["collection_results"][result["ip"]] = result
                except Exception as e:
                    logger.error(f"采集 LLDP 发现设备 {device['ip']} 时发生异常: {e}")
                    err_result = {
                        "ip": device["ip"],
                        "name": device.get("name", device["ip"]),
                        "online": False,
                        "ssh_ok": False,
                        "ssh_error": f"采集异常: {e}",
                        "commands": {},
                        "info": {"hostname": device.get("name", device["ip"])},
                    }
                    results.append(err_result)
                    state["collection_results"][device["ip"]] = err_result

    # 按 IP 排序返回
    results.sort(key=lambda r: int(r["ip"].rsplit(".", 1)[1]))
    logger.info(f"批量采集完成: {len(results)} 台设备")
    return {
        "success": True,
        "results": results,
        "devices": load_devices(),
        "lldp_added": lldp_added,
    }


