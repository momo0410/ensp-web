"""路由模块（由 main.py 机械拆分，逻辑未改动）。"""
from fastapi import APIRouter

from core import *  # noqa: F401,F403

router = APIRouter()

class FreePreviewRequest(BaseModel):
    task_name: str = ""
    target_devices: List[str]
    command_template: Any  # str or list
    global_vars: Dict[str, Any] = {}
    device_vars: Dict[str, Dict[str, Any]] = {}
    verify_commands: List[str] = []


class FreeDeployRequest(FreePreviewRequest):
    save_after_success: bool = False
    stop_on_error: bool = True
    force: bool = False


@router.get("/api/config-templates")
async def list_config_templates(role: Optional[str] = None):
    """返回所有配置模板（表单定义），供前端协议配置模态框统一渲染（消除前端硬编码）。"""
    templates = get_templates_for_role(role) if role else get_all_templates()
    return {"success": True, "templates": templates, "categories": get_categories()}


@router.post("/api/devices/{ip}/backup")
async def create_device_backup(ip: str):
    """备份指定设备当前配置。"""
    devices = load_devices()
    device = next((d for d in devices if d["ip"] == ip), None)
    if not device:
        raise HTTPException(status_code=404, detail=f"未找到 IP 为 {ip} 的设备")

    source_ip = _management_source_ip_for(ip)
    result = backup_device_config(
        device,
        source_ip=source_ip,
        management_socket_factory=_create_management_socket,
    )
    if not result["success"]:
        raise HTTPException(status_code=500, detail=f"备份失败: {result.get('error', '未知错误')}")
    return {"success": True, **result}


@router.get("/api/devices/{ip}/backups")
async def get_device_backups(ip: str):
    """查看指定设备历史备份列表。"""
    backups = list_backups(ip)
    return {"success": True, "ip": ip, "backups": backups}


# ---------------------------------------------------------------------------
# 配置下发 API
# ---------------------------------------------------------------------------

def _cached_full_config(ip: str) -> str:
    """读取缓存中的完整 current-configuration；过滤版输出不作为复制依据。"""
    result = state.get("collection_results", {}).get(ip, {})
    command = result.get("commands", {}).get("display current-configuration", {})
    if command.get("success") and command.get("output"):
        return command["output"]
    return ""


def _load_config_for_clone(device: dict, refresh: bool = True) -> dict:
    """
    获取配置复制所需的完整配置。

    refresh=True 时实时读取并落一份备份；失败后回退到完整采集缓存或最新备份。
    """
    ip = device["ip"]
    errors = []

    if refresh:
        source_ip = _management_source_ip_for(ip)
        backup = backup_device_config(
            device,
            source_ip=source_ip,
            management_socket_factory=_create_management_socket,
        )
        if backup.get("success") and backup.get("filepath"):
            try:
                return {
                    "success": True,
                    "config": Path(backup["filepath"]).read_text(encoding="utf-8"),
                    "backup_file": backup.get("filename", ""),
                    "source": "live",
                    "errors": [],
                }
            except OSError as e:
                errors.append(f"读取实时备份失败: {e}")
        else:
            errors.append(f"实时获取失败: {backup.get('error', '未知错误')}")

    cached = _cached_full_config(ip)
    if cached:
        return {
            "success": True,
            "config": cached,
            "backup_file": "",
            "source": "cache",
            "errors": errors,
        }

    backups = list_backups(ip)
    if backups:
        content = get_backup_content(backups[0]["filename"])
        if content:
            return {
                "success": True,
                "config": content,
                "backup_file": backups[0]["filename"],
                "source": "backup",
                "errors": errors,
            }

    return {
        "success": False,
        "config": "",
        "backup_file": "",
        "source": "",
        "errors": errors or ["没有可用的完整配置"],
    }


def _clone_role_warnings(source_ip: str, target_ips: List[str]) -> List[str]:
    warnings = []
    capabilities = state.get("capability_results", {})
    source_role = capabilities.get(source_ip, {}).get("device_role")
    for target_ip in target_ips:
        target_role = capabilities.get(target_ip, {}).get("device_role")
        if source_role and target_role and source_role != target_role:
            warnings.append(
                f"源设备角色为 {source_role}，目标设备 {target_ip} 角色为 {target_role}；"
                "不兼容接口或功能会被跳过，但仍需检查业务语义"
            )
    return warnings


def _reserved_ips_from_collection() -> set:
    """从所有已采集设备中提取地址，供配置复制自动分配时避让。"""
    reserved = set()
    for result in state.get("collection_results", {}).values():
        for line in result.get("info", {}).get("interfaces", []):
            parts = line.split() if isinstance(line, str) else []
            if len(parts) >= 2:
                try:
                    ipaddress.IPv4Address(parts[1])
                    reserved.add(parts[1])
                except ValueError:
                    pass
    return reserved


@router.post("/api/devices/{ip}/config/preview")
async def preview_config(ip: str, req: ConfigPreviewRequest):
    """生成配置预览（不连接设备）。"""
    preview = build_preview(req.config_type, req.params)
    if not preview["commands"]:
        raise HTTPException(status_code=400, detail=preview.get("error", "参数不完整"))
    return {"success": True, **preview}


@router.post("/api/devices/{ip}/config/deploy")
async def deploy_device_config(ip: str, req: ConfigDeployRequest):
    """下发配置命令到设备。"""
    devices = load_devices()
    device = next((d for d in devices if d["ip"] == ip), None)
    if not device:
        raise HTTPException(status_code=404, detail=f"未找到 IP 为 {ip} 的设备")

    commands = build_config_commands(req.config_type, req.params)
    if not commands:
        raise HTTPException(status_code=400, detail="参数不完整，无法生成配置命令")

    verify_cmds = get_verify_commands(req.config_type, req.params)

    source_ip = _management_source_ip_for(ip)
    result = deploy_config(
        device=device,
        commands=commands,
        verify_commands=verify_cmds,
        save_config=req.save_config,
        source_ip=source_ip,
        management_socket_factory=_create_management_socket,
    )

    if not result["success"] and result.get("error"):
        raise HTTPException(status_code=500, detail=f"配置下发失败: {result['error']}")

    return {"success": True, **result}


@router.get("/api/config/clone/domains")
async def get_clone_domains():
    """返回配置复制可选择的功能域。"""
    return {
        "success": True,
        "domains": [
            {"id": domain, "name": CLONE_DOMAIN_NAMES[domain], "default": domain in CLONE_DEFAULT_DOMAINS}
            for domain in CLONE_DOMAIN_NAMES
        ],
    }


@router.post("/api/config/clone/preview")
async def preview_clone_config(req: ClonePreviewRequest):
    """读取源/目标设备配置并生成自动解决冲突后的复制方案。"""
    devices = load_devices()
    device_map = {device["ip"]: device for device in devices}
    source = device_map.get(req.source_device)
    if not source:
        raise HTTPException(status_code=404, detail=f"未找到源设备 {req.source_device}")

    target_ips = list(dict.fromkeys(req.target_devices))
    if not target_ips:
        raise HTTPException(status_code=400, detail="请至少选择一台目标设备")
    if req.source_device in target_ips:
        raise HTTPException(status_code=400, detail="源设备不能同时作为目标设备")
    missing = [ip for ip in target_ips if ip not in device_map]
    if missing:
        raise HTTPException(status_code=404, detail=f"未找到目标设备: {', '.join(missing)}")

    selected_domains = [domain for domain in req.domains if domain in CLONE_DOMAIN_NAMES]
    if not selected_domains:
        raise HTTPException(status_code=400, detail="请至少选择一个复制功能域")

    fetch_ips = [req.source_device] + target_ips
    config_results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(fetch_ips), 5)) as executor:
        future_map = {
            executor.submit(_load_config_for_clone, device_map[ip], req.refresh_config): ip
            for ip in fetch_ips
        }
        for future in concurrent.futures.as_completed(future_map):
            ip = future_map[future]
            try:
                config_results[ip] = future.result()
            except Exception as e:
                config_results[ip] = {
                    "success": False,
                    "config": "",
                    "backup_file": "",
                    "source": "",
                    "errors": [str(e)],
                }

    unavailable = [
        f"{ip}: {'; '.join(info.get('errors', []))}"
        for ip, info in config_results.items()
        if not info.get("success")
    ]
    if unavailable:
        raise HTTPException(status_code=502, detail="无法获取完整设备配置: " + " | ".join(unavailable))

    clone_result = build_clone_preview(
        source_device=source,
        source_config=config_results[req.source_device]["config"],
        target_devices=[device_map[ip] for ip in target_ips],
        target_configs={ip: config_results[ip]["config"] for ip in target_ips},
        domains=selected_domains,
        auto_resolve=req.auto_resolve,
        include_sensitive=req.include_sensitive,
        reserved_ips=_reserved_ips_from_collection(),
    )

    rendered_commands = {
        ip: target["commands"]
        for ip, target in clone_result["targets"].items()
        if target.get("commands")
    }
    conflicts = check_cross_device_conflicts(
        targets=list(rendered_commands),
        rendered_commands=rendered_commands,
        collection_results=state.get("collection_results", {}),
    )
    clone_result["conflicts"] = conflicts
    clone_result["warnings"].extend(_clone_role_warnings(req.source_device, target_ips))
    if conflicts.get("errors"):
        clone_result["success"] = False

    preview_id = uuid.uuid4().hex
    state["clone_previews"][preview_id] = {
        "created_at": time.time(),
        "source_device": req.source_device,
        "target_devices": target_ips,
        "domains": selected_domains,
        "targets": clone_result["targets"],
        "conflicts": conflicts,
    }
    clone_result["preview_id"] = preview_id
    clone_result["config_sources"] = {
        ip: {
            "source": info.get("source", ""),
            "backup_file": info.get("backup_file", ""),
            "warnings": info.get("errors", []),
        }
        for ip, info in config_results.items()
    }
    return clone_result


@router.post("/api/config/clone/deploy")
async def deploy_clone_config(req: CloneDeployRequest):
    """下发已预览的设备配置复制方案。"""
    if not req.confirm:
        raise HTTPException(status_code=400, detail="请确认配置复制操作")

    preview = state.get("clone_previews", {}).get(req.preview_id)
    if not preview:
        raise HTTPException(status_code=404, detail="复制预览不存在或服务已重启，请重新生成预览")
    if time.time() - preview.get("created_at", 0) > 900:
        state["clone_previews"].pop(req.preview_id, None)
        raise HTTPException(status_code=410, detail="复制预览已超过 15 分钟，请重新读取设备配置并生成方案")
    if preview.get("conflicts", {}).get("errors"):
        raise HTTPException(status_code=409, detail="复制方案仍存在未解决冲突，请重新生成预览")

    active_targets = [
        ip for ip in preview["target_devices"]
        if preview["targets"].get(ip, {}).get("commands")
    ]
    if not active_targets:
        raise HTTPException(status_code=400, detail="没有需要下发的配置命令")

    task = {
        "task_name": f"复制 {preview['source_device']} 配置",
        "target_devices": active_targets,
        "verify_commands": [
            "display ip interface brief",
            "display current-configuration | include sysname|router-id|network-entity|ospf|bgp|isis|vlan",
        ],
        "save_after_success": req.save_config,
        "stop_on_error": True,
    }
    rendered_preview = {
        ip: {
            "device_name": preview["targets"][ip].get("device_name", ip),
            "commands": preview["targets"][ip]["commands"],
        }
        for ip in active_targets
    }
    exec_result = execute_free_deploy(
        task=task,
        preview=rendered_preview,
        devices=load_devices(),
        source_ip_func=_management_source_ip_for,
        socket_factory=_create_management_socket,
    )

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    task_id = generate_task_id()
    save_task_record({
        "task_id": task_id,
        "task_name": task["task_name"],
        "target_devices": active_targets,
        "command_template": "由设备配置复制生成",
        "global_vars": {},
        "device_vars": {},
        "verify_commands": task["verify_commands"],
        "rendered_commands": {ip: info["commands"] for ip, info in rendered_preview.items()},
        "results": exec_result["results"],
        "summary": exec_result["summary"],
        "status": "success" if exec_result["success"] else "partial" if exec_result["summary"]["success"] else "failed",
        "errors": [err for result in exec_result["results"].values() for err in result.get("errors", [])],
        "start_time": now,
        "end_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_device": preview["source_device"],
        "clone_preview_id": req.preview_id,
    })

    return {
        "success": exec_result["success"],
        "task_id": task_id,
        "source_device": preview["source_device"],
        "results": exec_result["results"],
        "summary": exec_result["summary"],
    }


@router.post("/api/devices/{ip}/config/raw-preview")
async def preview_raw_config(ip: str, req: RawPreviewRequest):
    """高级模式：预览原始命令（不执行）。"""
    if not req.commands:
        raise HTTPException(status_code=400, detail="请输入命令")
    return {
        "success": True,
        "commands": req.commands,
        "preview_commands": req.commands,
        "warning": "高级命令模式：请确认命令正确性，下发前系统会自动备份当前配置",
    }


@router.post("/api/devices/{ip}/config/raw-deploy")
async def deploy_raw_config(ip: str, req: RawDeployRequest):
    """高级模式：下发原始命令。"""
    if not req.commands:
        raise HTTPException(status_code=400, detail="请输入命令")
    if not req.confirm:
        raise HTTPException(status_code=400, detail="请确认二次确认标记")

    devices = load_devices()
    device = next((d for d in devices if d["ip"] == ip), None)
    if not device:
        raise HTTPException(status_code=404, detail=f"未找到 IP 为 {ip} 的设备")

    source_ip = _management_source_ip_for(ip)
    result = deploy_raw_commands(
        device=device,
        raw_commands=req.commands,
        save_config=req.save_config,
        source_ip=source_ip,
        management_socket_factory=_create_management_socket,
    )

    if not result["success"] and result.get("error"):
        raise HTTPException(status_code=500, detail=f"原始命令下发失败: {result['error']}")

    return {"success": True, **result}


# ---------------------------------------------------------------------------
# Console 端口直连配置 API（无需设备 IP，零配置设备初始化）
# ---------------------------------------------------------------------------

class ConsoleConfigRequest(BaseModel):
    """通过 Console 端口下发模板配置（零 IP）。"""
    console_port: int = Field(..., description="eNSP 本地 Console 端口号")
    config_type: str = Field(..., description="配置模板类型标识")
    params: dict = Field(default={}, description="表单参数")
    save_config: bool = False


class ConsoleRawRequest(BaseModel):
    """通过 Console 端口下发原始命令（零 IP，高级模式）。"""
    console_port: int = Field(..., description="eNSP 本地 Console 端口号")
    commands: List[str] = Field(default=[], description="原始命令列表（每行一条）")
    save_config: bool = False


@router.post("/api/console-config/preview")
async def preview_console_config(req: ConsoleRawRequest):
    """预览 Console 直连配置命令（不连接设备）。"""
    return {"success": True, "commands": req.commands}


@router.post("/api/console-config/deploy")
async def deploy_console_config(req: ConsoleConfigRequest):
    """通过 Console 端口下发模板配置（无需设备 IP）。"""
    commands = build_config_commands(req.config_type, req.params)
    if not commands:
        raise HTTPException(status_code=400, detail="参数不完整，无法生成配置命令")
    verify_cmds = get_verify_commands(req.config_type, req.params)
    result = console_deploy(
        console_port=req.console_port,
        commands=commands,
        verify_commands=verify_cmds,
        save_config=req.save_config,
    )
    if not result["success"] and result.get("error"):
        raise HTTPException(status_code=500, detail=f"Console 配置下发失败: {result['error']}")
    return {"success": True, **result}


@router.post("/api/console-config/raw-deploy")
async def deploy_console_raw(req: ConsoleRawRequest):
    """通过 Console 端口下发原始命令（高级模式，无需设备 IP）。"""
    if not req.commands:
        raise HTTPException(status_code=400, detail="没有命令可下发")
    result = console_deploy(
        console_port=req.console_port,
        commands=req.commands,
        verify_commands=[],
        save_config=req.save_config,
    )
    result["raw_mode"] = True
    if not result["success"] and result.get("error"):
        raise HTTPException(status_code=500, detail=f"Console 配置下发失败: {result['error']}")
    return {"success": True, **result}


# ---------------------------------------------------------------------------
# 命令目录 API（声明式表单化配置单一数据源）
# ---------------------------------------------------------------------------

class CatalogRenderRequest(BaseModel):
    key: str = Field(..., description="命令目录条目 key")
    params: dict = Field(default={}, description="表单参数")


class CatalogDeployRequest(BaseModel):
    key: str = Field(..., description="命令目录条目 key")
    params: dict = Field(default={}, description="表单参数")
    target_ip: Optional[str] = Field(None, description="目标设备 IP（SSH 通道）")
    console_port: Optional[int] = Field(None, description="Console 端口（零 IP 通道）")
    save_config: bool = False


@router.get("/api/command-catalog")
async def list_command_catalog(role: Optional[str] = None, device_type: Optional[str] = None):
    """列出命令目录（可选按设备角色 / eNSP 设备类型过滤）。"""
    return {
        "success": True,
        "categories": get_categories(),
        "entries": get_catalog_entries(role, device_type),
    }


@router.post("/api/command-catalog/render")
async def render_command_catalog(req: CatalogRenderRequest):
    """渲染目录条目为命令列表（不连接设备）。"""
    commands, warnings, errors = render_catalog_commands(req.key, req.params)
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    return {
        "success": True,
        "key": req.key,
        "commands": commands,
        "warnings": warnings,
    }


@router.post("/api/command-catalog/deploy")
async def deploy_command_catalog(req: CatalogDeployRequest):
    """通过命令目录下发配置：渲染后走 SSH 或 Console 通道。"""
    commands, warnings, errors = render_catalog_commands(req.key, req.params)
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(errors))

    verify_cmds = render_catalog_verify(req.key, req.params)

    # 零 IP 通道优先：给了 console_port 就走 Console
    if req.console_port is not None:
        result = console_deploy(
            console_port=req.console_port,
            commands=commands,
            verify_commands=verify_cmds,
            save_config=req.save_config,
        )
        result["channel"] = "console"
        if not result["success"] and result.get("error"):
            raise HTTPException(status_code=500, detail=f"命令目录下发失败: {result['error']}")
        return {"success": True, **result}

    # 否则走 SSH 通道
    if not req.target_ip:
        raise HTTPException(status_code=400, detail="请指定 target_ip 或 console_port")
    devices = load_devices()
    device = next((d for d in devices if d["ip"] == req.target_ip), None)
    if not device:
        raise HTTPException(status_code=404, detail=f"未找到 IP 为 {req.target_ip} 的设备")

    source_ip = _management_source_ip_for(req.target_ip)
    result = deploy_config(
        device=device,
        commands=commands,
        verify_commands=verify_cmds,
        save_config=req.save_config,
        source_ip=source_ip,
        management_socket_factory=_create_management_socket,
    )
    result["channel"] = "ssh"
    if not result["success"] and result.get("error"):
        raise HTTPException(status_code=500, detail=f"命令目录下发失败: {result['error']}")
    return {"success": True, **result}


# ---------------------------------------------------------------------------
# 命令目录 ? 探测引导 API（后台执行 + 缓存，做到无感知）
# ---------------------------------------------------------------------------
_catalog_probe_jobs: Dict[str, dict] = {}  # job_id -> {"status","result","error"}


class CatalogProbeRequest(BaseModel):
    target: dict = Field(..., description="探测目标：SSH={mode:'ssh',ip,...} / Console={mode:'console',port,...}")
    force: bool = Field(False, description="强制重新探测（忽略缓存）")


@router.post("/api/command-catalog/probe")
async def probe_catalog(req: CatalogProbeRequest, background_tasks: BackgroundTasks):
    """
    触发 ? 探测：识别设备支持的命令并校验目录。

    - 命中缓存（5min 内）→ 立即返回 done（无感知）。
    - 未命中 → 后台执行探测，立即返回 job_id；前端轮询 /api/command-catalog/probe/{job_id}。
    """
    key = cache_key(req.target)
    cached = get_cached_probe(key)
    if cached and not req.force:
        return {"success": True, "status": "done", "from_cache": True,
                "device_type": cached.get("device_type"), "result": cached}

    # SSH 目标若只给 IP，自动从设备清单补全凭据，避免前端传递密码
    target = dict(req.target)
    if target.get("mode") == "ssh" and target.get("ip") and not (
        target.get("username") and target.get("password")
    ):
        dev = next((d for d in load_devices() if d.get("ip") == target.get("ip")), None)
        if dev:
            target.setdefault("username", dev.get("username"))
            target.setdefault("password", dev.get("password"))
            target.setdefault("device_type", dev.get("device_type", "huawei"))

    job_id = uuid.uuid4().hex
    _catalog_probe_jobs[job_id] = {"status": "probing", "result": None, "error": None}

    def _run():
        try:
            res = probe_device_commands(req.target, force=True)
            _catalog_probe_jobs[job_id] = {"status": "done", "result": res, "error": None}
        except Exception as e:  # pragma: no cover
            logger.exception("命令目录 ? 探测失败")
            _catalog_probe_jobs[job_id] = {"status": "error", "result": None, "error": str(e)}

    background_tasks.add_task(_run)
    return {"success": True, "status": "probing", "job_id": job_id, "from_cache": False}


@router.get("/api/command-catalog/probe/{job_id}")
async def probe_catalog_result(job_id: str):
    """轮询 ? 探测结果。"""
    job = _catalog_probe_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="探测任务不存在")
    return {
        "success": True,
        "status": job["status"],
        "result": job.get("result"),
        "error": job.get("error"),
    }


@router.post("/api/config/free-preview")
async def free_config_preview(req: FreePreviewRequest):
    """自由配置预览：变量替换 + 批量冲突检测，不连接设备。"""
    task = {
        "task_name": req.task_name,
        "target_devices": req.target_devices,
        "command_template": req.command_template,
        "global_vars": req.global_vars,
        "device_vars": req.device_vars,
        "verify_commands": req.verify_commands,
    }

    is_valid, errors = validate_task(task)
    if not is_valid:
        return {"success": False, "preview": {}, "errors": errors, "warnings": []}

    result = render_preview(task)

    # 添加批量冲突检测
    if result["success"] and req.target_devices and len(req.target_devices) > 1:
        rendered_commands = {
            ip: info["commands"]
            for ip, info in result["preview"].items()
        }
        conflicts = check_cross_device_conflicts(
            targets=req.target_devices,
            rendered_commands=rendered_commands,
            collection_results=state.get("collection_results", {}),
        )
        result["conflicts"] = conflicts

        # 将冲突中的 warnings 合并到主 warnings
        if conflicts.get("warnings"):
            for w in conflicts["warnings"]:
                result["warnings"].append(w.get("message", str(w)))
    else:
        result["conflicts"] = {
            "success": True,
            "errors": [],
            "warnings": [],
            "suggestions": [],
        }

    return result


@router.post("/api/config/free-deploy")
async def free_config_deploy(req: FreeDeployRequest):
    """自由配置下发：备份 + 下发 + 验证 + 可选保存。"""
    from datetime import datetime

    task = {
        "task_name": req.task_name,
        "target_devices": req.target_devices,
        "command_template": req.command_template,
        "global_vars": req.global_vars,
        "device_vars": req.device_vars,
        "verify_commands": req.verify_commands,
        "save_after_success": req.save_after_success,
        "stop_on_error": req.stop_on_error,
    }

    # 验证任务
    is_valid, errors = validate_task(task)
    if not is_valid:
        return {"success": False, "errors": errors}

    # 先生成预览
    preview_result = render_preview(task)
    if not preview_result["success"]:
        return {"success": False, "errors": preview_result["errors"], "warnings": preview_result["warnings"]}

    # 检查危险命令（除非 force=true）
    if not req.force:
        from services.free_config_renderer import check_dangerous_commands, clean_commands
        raw = req.command_template if isinstance(req.command_template, list) else req.command_template.split("\n")
        cleaned, _, _ = clean_commands(raw)
        is_safe, dangerous = check_dangerous_commands(cleaned)
        if not is_safe:
            return {
                "success": False,
                "errors": [f"检测到危险命令: {d['command']} - {d['reason']}" for d in dangerous],
                "hint": "如需强制执行，请设置 force=true",
            }

    # 批量冲突检测（除非 force=true）
    if not req.force and len(req.target_devices) > 1:
        rendered_cmds = {
            ip: info["commands"]
            for ip, info in preview_result["preview"].items()
        }
        cross_conflicts = check_cross_device_conflicts(
            targets=req.target_devices,
            rendered_commands=rendered_cmds,
            collection_results=state.get("collection_results", {}),
        )
        if not cross_conflicts["success"]:
            return {
                "success": False,
                "errors": [e["message"] for e in cross_conflicts["errors"]],
                "warnings": [w["message"] for w in cross_conflicts.get("warnings", [])],
                "suggestions": [s["message"] for s in cross_conflicts.get("suggestions", [])],
                "hint": "如需强制执行，请设置 force=true",
            }

    # 获取设备列表
    devices = load_devices()
    device_ips = {d["ip"] for d in devices}
    for ip in req.target_devices:
        if ip not in device_ips:
            return {"success": False, "errors": [f"设备 {ip} 不在设备列表中"]}

    # 执行下发
    start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    task_id = generate_task_id()

    exec_result = execute_free_deploy(
        task=task,
        preview=preview_result["preview"],
        devices=devices,
        source_ip_func=_management_source_ip_for,
        socket_factory=_create_management_socket,
    )
    end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 保存任务记录
    record = {
        "task_id": task_id,
        "task_name": req.task_name or "未命名任务",
        "target_devices": req.target_devices,
        "command_template": req.command_template,
        "global_vars": req.global_vars,
        "device_vars": req.device_vars,
        "verify_commands": req.verify_commands,
        "rendered_commands": {ip: info["commands"] for ip, info in preview_result["preview"].items()},
        "results": exec_result["results"],
        "summary": exec_result["summary"],
        "status": "success" if exec_result["success"] else "partial" if exec_result["summary"]["success"] > 0 else "failed",
        "errors": [err for r in exec_result["results"].values() for err in r.get("errors", [])],
        "start_time": start_time,
        "end_time": end_time,
    }
    save_task_record(record)

    return {
        "success": exec_result["success"],
        "task_id": task_id,
        "task_name": req.task_name,
        "results": exec_result["results"],
        "summary": exec_result["summary"],
        "warnings": preview_result.get("warnings", []),
    }


# ---------------------------------------------------------------------------
# 智能默认值 & 批量冲突检测
# ---------------------------------------------------------------------------

class SmartDefaultsRequest(BaseModel):
    target_devices: List[str] = []
    config_type: str = ""


class NetworkWildcardRequest(BaseModel):
    network: str = ""


class BatchConflictRequest(BaseModel):
    target_devices: List[str] = []
    command_template: Any = []
    global_vars: Dict[str, Any] = {}
    device_vars: Dict[str, Dict[str, Any]] = {}


@router.post("/api/config/smart-defaults")
async def get_smart_defaults(req: SmartDefaultsRequest):
    """
    为选定设备生成智能默认值（Router ID = 管理 IP 等）。
    """
    if not req.target_devices:
        return {"success": False, "defaults": {}, "errors": ["未选择设备"]}

    defaults = generate_batch_smart_defaults(
        target_devices=req.target_devices,
        config_type=req.config_type,
        collection_results=state.get("collection_results", {}),
    )

    return {"success": True, "defaults": defaults}


@router.post("/api/utils/network-to-wildcard")
async def api_network_to_wildcard(req: NetworkWildcardRequest):
    """
    将网络地址转换为反掩码。支持 CIDR、纯 IP、带掩码格式。
    """
    if not req.network:
        return {"success": False, "wildcard": None, "error": "网络地址不能为空"}

    wildcard = infer_wildcard_from_input(req.network)
    if wildcard:
        return {"success": True, "wildcard": wildcard}
    else:
        return {"success": False, "wildcard": None, "error": f"无法解析网络地址: {req.network}"}


@router.post("/api/config/check-batch-conflicts")
async def api_check_batch_conflicts(req: BatchConflictRequest):
    """
    批量冲突检测：跨设备检测 Router ID 重复、IP 冲突等。
    """
    if not req.target_devices:
        return {"success": True, "conflicts": {"success": True, "errors": [], "warnings": [], "suggestions": []}}

    # 生成预览以获取渲染后的命令
    task = {
        "task_name": "冲突检测",
        "target_devices": req.target_devices,
        "command_template": req.command_template,
        "global_vars": req.global_vars,
        "device_vars": req.device_vars,
    }

    preview_result = render_preview(task)
    if not preview_result["success"]:
        return {
            "success": False,
            "conflicts": None,
            "errors": preview_result["errors"],
        }

    rendered_commands = {
        ip: info["commands"]
        for ip, info in preview_result["preview"].items()
    }

    # 执行跨设备冲突检测
    conflicts = check_cross_device_conflicts(
        targets=req.target_devices,
        rendered_commands=rendered_commands,
        collection_results=state.get("collection_results", {}),
    )

    return {"success": True, "conflicts": conflicts}


