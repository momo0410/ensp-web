"""路由模块（由 main.py 机械拆分，逻辑未改动）。"""
from fastapi import APIRouter

from core import *  # noqa: F401,F403

router = APIRouter()

class ScriptSaveRequest(BaseModel):
    script_name: str
    description: str = ""
    command_template: Any = []
    global_vars: Dict[str, Any] = {}
    sample_device_vars: Dict[str, Any] = {}
    verify_commands: List[str] = []


@router.post("/api/scripts")
async def api_save_script(req: ScriptSaveRequest):
    """保存脚本。"""
    data = {
        "script_name": req.script_name,
        "description": req.description,
        "command_template": req.command_template,
        "global_vars": req.global_vars,
        "sample_device_vars": req.sample_device_vars,
        "verify_commands": req.verify_commands,
    }
    result = save_script(data)
    return result


@router.get("/api/scripts/{name}")
async def api_get_script(name: str):
    """读取脚本。"""
    script = get_script(name)
    if not script:
        return {"success": False, "message": f"脚本 '{name}' 不存在"}
    return {"success": True, "script": script}


@router.delete("/api/scripts/{name}")
async def api_delete_script(name: str):
    """删除脚本。"""
    return delete_script(name)


@router.get("/api/tasks")
async def api_list_tasks(limit: int = 50):
    """获取历史任务列表。"""
    tasks = list_tasks(limit=limit)
    return {"success": True, "tasks": tasks}


@router.get("/api/tasks/{task_id}")
async def api_get_task(task_id: str):
    """获取任务详情。"""
    task = get_task(task_id)
    if not task:
        return {"success": False, "message": f"任务 '{task_id}' 不存在"}
    return {"success": True, "task": task}


# ---------------------------------------------------------------------------
# 服务器启动自动初始化
# ---------------------------------------------------------------------------

# 初始化状态
init_state = {
    "initialized": False,
    "in_progress": False,
    "message": "",
    "selected_interface": None,
    "subnet": None,
}


def _auto_init():
    """服务器启动时自动执行：选择网卡 → 发现设备 → 并行采集 → 并行能力识别"""
    global init_state
    if init_state["initialized"] or init_state["in_progress"]:
        return

    init_state["in_progress"] = True
    init_state["message"] = "正在自动识别网卡..."

    try:
        # 1. 自动选择管理网卡
        interfaces = _get_all_interfaces()
        recommended = None
        for iface in interfaces:
            if iface.get("is_recommended"):
                recommended = iface
                break

        if not recommended:
            for iface in interfaces:
                if iface.get("ipv4") and iface.get("status") == "UP":
                    recommended = iface
                    break

        if not recommended:
            init_state["message"] = "未找到可用的管理网卡"
            init_state["initialized"] = True
            init_state["in_progress"] = False
            return

        state["selected_interface"] = {
            "name": recommended["name"],
            "ip": recommended["ipv4"],
            "netmask": recommended["netmask"],
        }
        try:
            network = ipaddress.IPv4Network(f"{recommended['ipv4']}/{recommended['netmask']}", strict=False)
            state["subnet"] = str(network)
        except ValueError:
            state["subnet"] = None

        init_state["selected_interface"] = state["selected_interface"]
        init_state["subnet"] = state["subnet"]
        init_state["message"] = f"已选择网卡: {recommended['name']} ({recommended['ipv4']})"

        # 2. 发现设备
        if state["subnet"]:
            init_state["message"] = f"正在扫描网段 {state['subnet']}..."
            local_ip = state["selected_interface"]["ip"]
            probes = probe_subnet(state["subnet"], exclude_ip=local_ip, max_workers=50)

            # SSH 识别设备
            discovered = []
            for probe in probes:
                device = {
                    "name": _default_discovered_name(probe["ip"]),
                    "ip": probe["ip"],
                    "username": DEFAULT_SSH_USERNAME,
                    "password": DEFAULT_SSH_PASSWORD,
                    "device_type": DEFAULT_DEVICE_TYPE,
                }
                identity = ssh_identify_device(device)
                if identity.get("ssh_ok"):
                    device["name"] = identity.get("name", device["name"])
                    discovered.append(device)

            if discovered:
                save_devices(discovered)
                logger.info("自动发现 %d 台设备", len(discovered))

                # 3. 并行批量采集（3个SSH会话/台，设备间也并行）
                init_state["message"] = f"正在并行采集 {len(discovered)} 台设备信息..."

                def _parallel_collect(device):
                    ip = device["ip"]
                    online = ping_host(ip)
                    if online:
                        return ssh_collect_device_parallel(device, fast_mode=True)
                    return {"ip": ip, "name": device.get("name", ip), "online": False,
                            "ssh_ok": False, "ssh_error": "设备不可达", "commands": {}, "info": {}}

                with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(discovered), 10)) as executor:
                    future_map = {executor.submit(_parallel_collect, d): d for d in discovered}
                    for future in concurrent.futures.as_completed(future_map):
                        try:
                            result = future.result()
                            state["collection_results"][result["ip"]] = result
                        except Exception as e:
                            device = future_map[future]
                            logger.error("采集 %s 失败: %s", device["ip"], e)

                # 4. LLDP 同步 + 并行采集新发现设备
                lldp_added = _sync_lldp_devices()
                if lldp_added:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(lldp_added), 5)) as executor:
                        future_map = {executor.submit(_parallel_collect, d): d for d in lldp_added}
                        for future in concurrent.futures.as_completed(future_map):
                            try:
                                result = future.result()
                                state["collection_results"][result["ip"]] = result
                            except Exception as e:
                                device = future_map[future]
                                logger.error("LLDP 采集 %s 失败: %s", device["ip"], e)

                # 5. 并行能力识别（3个SSH会话/台，设备间也并行）
                init_state["message"] = "正在并行识别设备能力..."
                all_devices = load_devices()

                def _parallel_probe(device):
                    ip = device["ip"]
                    src = _management_source_ip_for(ip)
                    # 传入采集缓存，避免重复 SSH
                    coll_result = state["collection_results"].get(ip)
                    try:
                        return probe_capabilities_parallel(
                            device,
                            source_ip=src,
                            management_socket_factory=_create_management_socket,
                            collection_result=coll_result,
                        )
                    except Exception as e:
                        logger.error("能力识别 %s 失败: %s", ip, e)
                        return {"ip": ip, "device_name": device.get("name", ip),
                                "ssh_status": False, "capabilities": {}, "errors": [str(e)]}

                state["capability_results"] = {}
                with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(all_devices), 10)) as executor:
                    future_map = {executor.submit(_parallel_probe, d): d for d in all_devices}
                    for future in concurrent.futures.as_completed(future_map):
                        try:
                            cap_result = future.result()
                            state["capability_results"][cap_result["ip"]] = cap_result
                            state_persist.save_capability_cache(
                                state["capability_results"])
                        except Exception as e:
                            device = future_map[future]
                            logger.error("能力识别 %s 失败: %s", device["ip"], e)

                init_state["initialized"] = True
                init_state["message"] = f"初始化完成: {len(discovered)} 台设备"
                logger.info("自动初始化完成")
            else:
                init_state["initialized"] = True
                init_state["message"] = "未发现可管理的设备"
        else:
            init_state["initialized"] = True
            init_state["message"] = "未获取到网段信息"

    except Exception as e:
        init_state["initialized"] = True
        init_state["message"] = f"初始化失败: {e}"
        logger.error("自动初始化失败: %s", e)
    finally:
        init_state["in_progress"] = False


def _get_all_interfaces() -> List[dict]:
    """获取所有网卡信息"""
    interfaces = []
    try:
        addrs = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        metadata = _get_windows_adapter_metadata()
    except Exception:
        return interfaces

    for name, addr_list in addrs.items():
        meta = metadata.get(name, {})
        info = {
            "name": name,
            "description": meta.get("description", ""),
            "ipv4": "",
            "netmask": "",
            "mac": "",
            "status": "DOWN",
            "is_ensp": False,
            "is_recommended": False,
        }

        ipv4_candidates = []
        for addr in addr_list:
            try:
                if addr.family == socket.AF_INET:
                    candidate = {
                        "address": addr.address or "",
                        "netmask": addr.netmask or "",
                    }
                    if candidate["address"]:
                        ipv4_candidates.append(candidate)
                elif addr.family == getattr(psutil, "AF_LINK", -1):
                    info["mac"] = addr.address or ""
            except Exception:
                pass

        best_ipv4 = _pick_best_ipv4(ipv4_candidates)
        if best_ipv4:
            info["ipv4"] = best_ipv4["address"]
            info["netmask"] = best_ipv4["netmask"]

        try:
            if name in stats:
                info["status"] = "UP" if stats[name].isup else "DOWN"
        except Exception:
            pass

        if info["ipv4"] or info["mac"]:
            score, _ = _rank_interface(info)
            info["is_ensp"] = score >= 40
            info["is_recommended"] = score >= 40
            interfaces.append(info)

    interfaces.sort(key=lambda x: (
        not x.get("is_recommended", False),
        not x.get("is_ensp", False),
        x.get("status") != "UP",
    ))

    return interfaces


