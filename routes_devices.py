"""路由模块（由 main.py 机械拆分，逻辑未改动）。"""
from fastapi import APIRouter

from fastapi import HTTPException
from typing import List, Optional
import concurrent.futures
import time

from app.config import logger
from app.models import (
    DeviceAdd,
    EnspAttachRequest,
    EnspTopoApplyRequest,
    LinkAdd,
    PathTraceRequest,
    PingRequest,
    ProtocolConfigRequest,
    TopologyActivate,
    WorkspaceSet,
)
from app.state import record_collection, state
from domain.config_push import generate_delete_commands, push_config_to_device
from domain.device_store import (
    ConflictError,
    check_revision,
    current_revision,
    load_devices,
    save_devices,
)
from domain.ensp_runtime import (
    allocate_console_port,
    ensure_ensp_injected,
    get_ensp_runtime_status,
    probe_ensp_live,
    load_saved_links,
    read_ensp_topology,
    save_links,
    send_ensp_ctrl_cmd,
    start_stop_device,
    sync_and_start_ensp_topo,
)
from domain.reachability import ping_host, port_sort_key
from domain.parse import _parse_config_sections, _parse_device_info  # noqa: F401  # 供测试引用
from domain.collect import collect_device as collect_device_fn
from services.config_builder import build_config_commands
from tools.gen_topology import COMPREHENSIVE_DEMO, DEVICE_MODELS, interface_index_map, TopologyError

router = APIRouter()

import routes_config  # noqa: E402
@router.get("/api/devices")
async def get_devices():
    """获取设备列表。"""
    devices = load_devices()
    return {"success": True, "devices": devices, "revision": current_revision()}


@router.post("/api/devices")
async def add_device(device: DeviceAdd):
    """添加设备，自动分配 Console 端口，并可选自动在 eNSP 中生成拓扑与开机。"""
    try:
        check_revision(device.revision)
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    devices = load_devices()
    port = device.console_port or allocate_console_port(devices)
    ip_val = (device.ip or "").strip() or f"127.0.0.1:{port}"

    for d in devices:
        if d["ip"] == ip_val or d.get("name") == device.name:
            raise HTTPException(status_code=400, detail=f"设备 {device.name} ({ip_val}) 已存在")

    new_device = device.model_dump()
    new_device["console_port"] = int(port)
    new_device["ip"] = ip_val
    new_device["model"] = device.model or "Router"
    devices.append(new_device)
    save_devices(devices)
    logger.info("添加设备: %s (%s, model=%s, console=%s)", device.name, ip_val, new_device["model"], port)
    _rev = current_revision()

    ensp_result = None
    if device.auto_start:
        ensp_result = sync_and_start_ensp_topo(devices, auto_start=True)

    return {
        "success": True,
        "device": new_device,
        "revision": _rev,
        "ensp": ensp_result,
        "message": f"设备 {device.name} (Console 127.0.0.1:{port}) 已添加"
        + ("并触发自动开机" if device.auto_start else ""),
    }


@router.get("/api/ensp/status")
async def ensp_status():
    """获取 eNSP 运行状态及支持的设备型号列表。"""
    st = get_ensp_runtime_status()
    return {
        "success": True,
        "ensp": st,
        "models": [
            {
                "model": m,
                "kind": spec["kind"],
                # 终端设备（PC/Laptop/MCS/STA）没有 CLI，不占 Console 端口
                "terminal": spec["kind"] == "terminal",
                "interfaces": sum(c for _, _, c in spec["interfaces"]),
            }
            for m, spec in sorted(DEVICE_MODELS.items())
        ],
        "links": load_saved_links(),
    }


@router.get("/api/ensp/live")
async def ensp_live():
    """
    轻量实时探测：前端轮询用。

    绝不触发注入、绝不拉起 eNSP，快速返回 eNSP 的实时状态，
    用于让界面反映"用户在 eNSP 里做了什么"，而不是只显示本地 devices.json。
    """
    return {"success": True, **probe_ensp_live()}


# ---------------------------------------------------------------------------
# 拓扑连线（CRUD）
# ---------------------------------------------------------------------------
def _interface_names(model: str) -> List[str]:
    """某型号可用的逻辑接口名（GE 只保留短名，避免下拉里重复）。"""
    try:
        keys = interface_index_map(model).keys()
    except TopologyError:
        return []
    return sorted(k for k in keys if not k.startswith("Gigabit"))


@router.get("/api/topology/interfaces/{device_name}")
async def topology_device_interfaces(device_name: str):
    """某设备可用接口列表，供连线编辑器下拉使用。"""
    device = next((d for d in load_devices() if d.get("name") == device_name), None)
    if not device:
        raise HTTPException(status_code=404, detail=f"未找到设备 {device_name}")
    model = device.get("model") or "Router"
    if DEVICE_MODELS.get(model, {}).get("kind") == "terminal":
        return {"success": True, "device": device_name, "model": model,
                "interfaces": _interface_names(model), "terminal": True}
    return {"success": True, "device": device_name, "model": model,
            "interfaces": _interface_names(model), "terminal": False}


@router.get("/api/topology/links")
async def topology_links():
    """当前激活拓扑的连线列表。"""
    return {"success": True, "links": load_saved_links()}


@router.post("/api/topology/links")
async def topology_add_link(req: LinkAdd):
    """新增一条连线（端点接口须在该型号上真实存在）。"""
    devices = load_devices()
    by_name = {d.get("name"): d for d in devices}
    for endpoint, label in ((req.a, "a"), (req.b, "b")):
        dev = by_name.get(endpoint.device)
        if not dev:
            raise HTTPException(status_code=400, detail=f"{label} 端设备不存在: {endpoint.device}")
        model = dev.get("model") or "Router"
        if endpoint.interface not in _interface_names(model):
            raise HTTPException(
                status_code=400,
                detail=f"{endpoint.device}({model}) 上没有接口 {endpoint.interface}",
            )
    if req.a.device == req.b.device and req.a.interface == req.b.interface:
        raise HTTPException(status_code=400, detail="连线两端不能是同一个接口")

    links = load_saved_links()
    candidate = {"a": req.a.model_dump(), "b": req.b.model_dump()}
    # 重复检测（两端顺序无关）
    def _norm(link):
        ends = sorted([
            (link["a"]["device"], link["a"]["interface"]),
            (link["b"]["device"], link["b"]["interface"]),
        ])
        return tuple(ends)
    if _norm(candidate) in {_norm(x) for x in links}:
        raise HTTPException(status_code=400, detail="该连线已存在")
    # 接口占用检测
    used = set()
    for link in links:
        for end in ("a", "b"):
            used.add((link[end]["device"], link[end]["interface"]))
    for end in ("a", "b"):
        key = (candidate[end]["device"], candidate[end]["interface"])
        if key in used:
            raise HTTPException(status_code=400,
                                detail=f"{key[0]} 的 {key[1]} 已被占用")

    links.append(candidate)
    save_links(links)
    logger.info("新增连线: %s/%s <-> %s/%s",
                req.a.device, req.a.interface, req.b.device, req.b.interface)
    return {"success": True, "links": links}


@router.delete("/api/topology/links")
async def topology_delete_link(
    a_device: str, a_interface: str, b_device: str, b_interface: str
):
    """删除一条连线（端点顺序无关）。"""
    links = load_saved_links()
    target = {tuple(sorted([(a_device, a_interface), (b_device, b_interface)]))}
    kept = [
        lnk for lnk in links
        if tuple(sorted([
            (lnk["a"]["device"], lnk["a"]["interface"]),
            (lnk["b"]["device"], lnk["b"]["interface"]),
        ])) not in target
    ]
    if len(kept) == len(links):
        raise HTTPException(status_code=404, detail="未找到该连线")
    save_links(kept)
    logger.info("删除连线: %s/%s <-> %s/%s", a_device, a_interface, b_device, b_interface)
    return {"success": True, "links": kept}


# ---------------------------------------------------------------------------
# 拓扑库（工作目录下的多个拓扑）
# ---------------------------------------------------------------------------
@router.post("/api/ensp/attach-active")
async def ensp_attach_active(req: EnspAttachRequest):
    """把 eNSP 当前拓扑对齐到项目（需求②反向）。

    两种模式：
    - match_by_devices：在项目已有拓扑里找设备名集合一致的，直接切过去；
    - import_as：把 eNSP 当前设备清单导入为一个新的项目拓扑。

    连线无法反向取得（eNSP 内存只暴露设备名/型号/电源），
    导入的拓扑 links 置空，由前端明示需手动补。
    """
    from domain import workspace as ws
    from domain.ensp_runtime import DEVICE_MODELS

    live = read_ensp_topology()
    if not live.get("ensp_running"):
        return {"success": False, "error": "eNSP 未运行，无法对齐"}
    live_devices = live.get("live_devices") or []
    if not live_devices:
        return {"success": False, "error": "eNSP 中未检测到任何设备（拓扑未加载）"}

    actual_names = [d.get("name") for d in live_devices if d.get("name")]

    if req.mode == "match_by_devices":
        for item in ws.list_topologies():
            expected = [d.get("name") for d in ws.load_topology(item["name"]).get("devices", [])]
            if expected and set(expected) == set(actual_names):
                ws.set_active(item["name"])
                return {
                    "success": True,
                    "mode": "match_by_devices",
                    "active": item["name"],
                    "matched": len(expected),
                    "message": f"已在项目中切换到拓扑 {item['name']}（{len(expected)} 台设备）",
                }
        return {
            "success": False,
            "error": "项目中没有与 eNSP 当前设备集合一致的拓扑",
            "hint": "可改用 import_as 模式把 eNSP 设备清单导入为新拓扑",
            "actual": actual_names,
            "available": [t["name"] for t in ws.list_topologies()],
        }

    if req.mode == "import_as":
        name = (req.name or "").strip() or "ensp_imported"
        try:
            ws.create_topology(name)
        except ws.WorkspaceError as e:
            return {"success": False, "error": str(e)}

        devices = []
        next_port = allocate_console_port(devices)
        for dev in live_devices:
            model = (dev.get("model") or "Router").strip() or "Router"
            if model not in DEVICE_MODELS:
                model = "Router"
            devices.append({
                "name": dev.get("name") or f"DEV{next_port}",
                "model": model,
                # 项目里设备用 ip 作为主键，Console 接入点是 127.0.0.1:<port>，
                # 两者必须一起补全，否则采集/下发都找不到这台设备。
                "ip": f"127.0.0.1:{next_port}",
                "console_port": next_port,
                "device_type": "huawei",
                "auto_start": False,
            })
            next_port += 1

        ws.save_topology(name, devices, links=[])
        ws.set_active(name)
        logger.info("已从 eNSP 导入拓扑 %s（%d 台设备，连线需手动补）", name, len(devices))
        return {
            "success": True,
            "mode": "import_as",
            "active": name,
            "imported": len(devices),
            "links_imported": 0,
            "message": (
                f"已导入 {len(devices)} 台设备为新拓扑 {name}；"
                "eNSP 不暴露端口级连线，请在项目里手动补连线"
            ),
            "topology": ws.load_topology(name),
        }

    return {"success": False, "error": f"未知模式: {req.mode}（可用 match_by_devices / import_as）"}


@router.get("/api/topology/library")
async def topology_library():
    """列出工作目录下的所有拓扑及其激活状态。"""
    from domain import workspace as ws
    return {
        "success": True,
        "workspace_root": str(ws.get_workspace_root()),
        "active": ws.active_topology(),
        "topologies": ws.list_topologies(),
    }


def _open_in_ensp_if_possible(name: str) -> dict:
    """把指定拓扑同步到 eNSP 窗口（需求②：项目建/开拓扑 → eNSP 立即打开）。

    失败只返回原因、不抛异常——eNSP 不可用时项目其余能力仍要能用。
    """
    from domain import workspace as ws
    if not ws.get_setting("auto_sync_ensp_topo", True):
        return {"attempted": False, "reason": "已关闭自动同步（auto_sync_ensp_topo=false）"}
    topo = ws.topo_path_for(name)
    if not topo.exists():
        return {"attempted": False, "reason": "该拓扑还没有 .topo 文件（添加设备后自动生成）"}
    try:
        from domain.ensp_runtime import open_topology_in_ensp
        res = open_topology_in_ensp(topo)
    except Exception as e:
        return {"attempted": True, "ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"attempted": True, **res}


@router.post("/api/topology/library/activate")
async def topology_activate(req: TopologyActivate):
    """切换激活拓扑，并返回其设备与连线。切换后同步在 eNSP 中打开该拓扑。"""
    from domain import workspace as ws
    try:
        ws.set_active(req.name)
    except ws.WorkspaceError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    data = ws.load_topology(req.name)
    return {"success": True, **data, "ensp": _open_in_ensp_if_possible(req.name)}


@router.post("/api/topology/library/create")
async def topology_create(req: TopologyActivate):
    """新建一个空拓扑。新建后若已有 .topo 则同步在 eNSP 中打开。"""
    from domain import workspace as ws
    try:
        res = ws.create_topology(req.name)
    except ws.WorkspaceError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"success": True, **res, "ensp": _open_in_ensp_if_possible(req.name)}


@router.delete("/api/topology/library/{name}")
async def topology_delete(name: str):
    """删除一个拓扑及其配置目录。"""
    from domain import workspace as ws
    try:
        res = ws.delete_topology(name)
    except ws.WorkspaceError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"success": True, **res}


@router.post("/api/workspace/root")
async def workspace_set_root(req: WorkspaceSet):
    """设置工作目录。"""
    from domain import workspace as ws
    try:
        res = ws.set_workspace_root(req.path)
    except ws.WorkspaceError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"success": True, **res}


@router.post("/api/ensp/topo/apply")
async def ensp_apply_topo(req: EnspTopoApplyRequest):
    """
    把拓扑下发到 eNSP。

    两种意图，必须分清（此前混在一起，会误覆盖设备表）：
    - **加载预设**：`preset` 命中内置案例 → 用预设**替换**当前拓扑的设备与连线。
    - **下发当前拓扑**：不带 preset → **只读**当前设备表/连线并下发，**不写回**。
      想改设备请用 /api/devices；想改连线请用 /api/topology/links。
    """
    overwrite = False

    if req.preset == "comprehensive_campus":
        raw_devices = COMPREHENSIVE_DEMO["devices"]
        links = COMPREHENSIVE_DEMO["links"]
        topo_name = req.name or COMPREHENSIVE_DEMO["name"]
        overwrite = True
    elif req.preset == "big_campus":
        # 15 台设备分层园区网（核心/汇聚/接入 + 出口 + 无线 + 终端），
        # 用于验证批量与分层场景；接口分配见 tools/big_campus.py 的注释。
        from tools.big_campus import build_big_campus
        big = build_big_campus()
        raw_devices = big["devices"]
        links = big["links"]
        topo_name = req.name or big["name"]
        overwrite = True
    elif req.preset:
        raise HTTPException(status_code=400, detail=f"未知的拓扑预设: {req.preset}")
    else:
        # 不带 preset：用现有拓扑下发，绝不覆盖设备表
        raw_devices = load_devices()
        links = load_saved_links()
        from domain import workspace as _ws
        topo_name = req.name or _ws.active_topology() or "active_workspace"

    if not raw_devices:
        raise HTTPException(
            status_code=400,
            detail="当前拓扑没有设备。请先添加设备，或用「加载综合园区网案例」导入预设。",
        )

    # 预设路径：带版本校验地整体替换，避免覆盖掉用户在别处刚做的修改
    if overwrite:
        try:
            check_revision(req.revision)
        except ConflictError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        normalized = _normalize_devices(raw_devices)
        save_devices(normalized)
        save_links(links)
        devices_for_deploy = normalized
    else:
        devices_for_deploy = raw_devices

    ensp_res = sync_and_start_ensp_topo(
        devices_for_deploy, links=links, topo_name=topo_name, auto_start=req.auto_start
    )
    ok = bool(ensp_res.get("ok"))
    if ok:
        msg = f"已向 eNSP 下发 {len(devices_for_deploy)} 台设备" + (
            "（并替换了当前拓扑）" if overwrite else ""
        )
    else:
        err = (ensp_res.get("error")
               or (ensp_res.get("open_result") or {}).get("error")
               or "eNSP 控制通道不可用")
        msg = f"下发失败：{err}"
    return {
        "success": ok,
        "devices": devices_for_deploy,
        "links": links,
        "ensp": ensp_res,
        "message": msg,
    }


def _normalize_devices(raw_devices: list) -> list:
    """把拓扑描述里的设备补全成设备表记录（坐标、端口、终端参数等）。"""
    out = []
    for idx, d in enumerate(raw_devices):
        name = d.get("name") or f"DEV{idx + 1}"
        model = d.get("model") or "Router"
        spec = DEVICE_MODELS.get(model)
        entry = {
            "name": name,
            "model": model,
            "cx": d.get("cx", 160.0 + (idx % 4) * 210.0),
            "cy": d.get("cy", 200.0 + (idx // 4) * 180.0),
        }
        if d.get("uuid"):
            entry["uuid"] = d["uuid"]
        if spec and spec["kind"] == "terminal":
            entry.update({
                "ip": d.get("ip") or name,
                "console_port": None,
                "device_type": "terminal",
                "ip_address": d.get("ip_address") or "0.0.0.0",
                "netmask": d.get("netmask") or "255.255.255.0",
                "gateway": d.get("gateway") or "0.0.0.0",
            })
        else:
            port = int(d.get("console_port") or d.get("com_port") or (2000 + idx))
            entry.update({
                "ip": d.get("ip") or f"127.0.0.1:{port}",
                "console_port": port,
                "device_type": "huawei",
                "username": "admin",
                "password": "Huawei@123",
            })
        out.append(entry)
    return out


@router.post("/api/ensp/start-all")
async def ensp_start_all():
    """一键在 eNSP 中同步当前设备并启动全部设备。"""
    devices = load_devices()
    if not devices:
        raise HTTPException(status_code=400, detail="设备列表为空，请先添加设备")
    ensp_res = sync_and_start_ensp_topo(devices, auto_start=True)
    ok = bool(ensp_res.get("ok"))
    if ok:
        msg = f"已向 eNSP 发送 {len(devices)} 台设备的启动指令"
    else:
        err = (ensp_res.get("error")
               or (ensp_res.get("open_result") or {}).get("error")
               or "eNSP 控制通道不可用")
        msg = f"启动失败：{err}"
    return {"success": ok, "ensp": ensp_res, "message": msg}


@router.post("/api/ensp/stop-all")
async def ensp_stop_all():
    """一键停止 eNSP 中的全部设备。"""
    inj = ensure_ensp_injected(launch_if_missing=False)
    if not inj.get("ok"):
        raise HTTPException(status_code=400, detail=inj.get("error", "eNSP 未运行"))
    stop_res = send_ensp_ctrl_cmd("stop_all")
    ok = bool(stop_res.get("ok"))
    return {
        "success": ok,
        "ensp": stop_res,
        "message": ("已向 eNSP 发送全部设备停止指令" if ok
                    else f"停止失败：{stop_res.get('error', 'eNSP 控制通道不可用')}"),
    }


@router.post("/api/ensp/devices/{ip}/start")
async def ensp_start_device(ip: str):
    """启动 eNSP 中的单台设备（需求③）。"""
    res = start_stop_device(ip, action="start")
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "启动失败"))
    return {"success": True, "ensp": res, "message": res.get("message")}


@router.post("/api/ensp/devices/{ip}/stop")
async def ensp_stop_device(ip: str):
    """停止 eNSP 中的单台设备（需求③）。"""
    res = start_stop_device(ip, action="stop")
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "停止失败"))
    return {"success": True, "ensp": res, "message": res.get("message")}


@router.put("/api/devices/{ip}")
async def update_device(ip: str, device: DeviceAdd):
    """更新设备连接配置。"""
    try:
        check_revision(device.revision)
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    devices = load_devices()
    target_index = next((idx for idx, d in enumerate(devices) if d["ip"] == ip), None)
    if target_index is None:
        raise HTTPException(status_code=404, detail=f"未找到标识为 {ip} 的设备")

    port = device.console_port or devices[target_index].get("console_port") or allocate_console_port(devices)
    new_ip = (device.ip or "").strip() or f"127.0.0.1:{port}"

    if new_ip != ip and any(d["ip"] == new_ip for d in devices):
        raise HTTPException(status_code=400, detail=f"设备 {new_ip} 已存在")

    old = devices[target_index]
    updated_device = device.model_dump()
    updated_device["console_port"] = int(port)
    updated_device["ip"] = new_ip
    # 表单里没有的字段从原记录保留，否则编辑一次就会丢掉坐标/终端参数/设备身份
    for key in ("cx", "cy", "uuid", "ip_address", "netmask", "gateway"):
        if updated_device.get(key) is None and old.get(key) is not None:
            updated_device[key] = old[key]
    if not updated_device.get("cx"):
        updated_device["cx"] = old.get("cx", 160.0)
    if not updated_device.get("cy"):
        updated_device["cy"] = old.get("cy", 200.0)
    devices[target_index] = updated_device
    save_devices(devices)

    if new_ip != ip and ip in state["collection_results"]:
        moved = state["collection_results"].pop(ip)
        if moved:
            moved["ip"] = new_ip
            moved["name"] = device.name
            record_collection(new_ip, moved)

    logger.info(f"更新设备配置: {ip} -> {device.name} ({new_ip})")
    return {"success": True, "device": updated_device, "message": f"设备 {device.name} 配置已保存"}


@router.delete("/api/devices/{ip}")
async def delete_device(ip: str, revision: Optional[int] = None):
    """删除指定 IP 的设备。"""
    try:
        check_revision(revision)
    except ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    devices = load_devices()
    new_devices = [d for d in devices if d["ip"] != ip]
    if len(new_devices) == len(devices):
        raise HTTPException(status_code=404, detail=f"未找到 IP 为 {ip} 的设备")
    removed_name = next((d.get("name") for d in devices if d["ip"] == ip), None)
    save_devices(new_devices)
    state["collection_results"].pop(ip, None)

    # 清理该设备相关的连线，避免留下悬空引用
    dropped = 0
    if removed_name:
        links = load_saved_links()
        kept = [
            lnk for lnk in links
            if lnk.get("a", {}).get("device") != removed_name
            and lnk.get("b", {}).get("device") != removed_name
        ]
        dropped = len(links) - len(kept)
        if dropped:
            save_links(kept)

    logger.info("删除设备: %s（同时清理 %d 条连线）", ip, dropped)
    return {"success": True, "message": f"设备 {ip} 已删除",
            "removed_links": dropped}


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
        "ok": loaded.get("source") == "live",
        "error": "",
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
        result["ok"] = True
        result["error"] = ""
    record_collection(ip, result)
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
    collect_result = collect_device_fn(device)
    record_collection(ip, collect_result)

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
    collect_result = collect_device_fn(device)
    record_collection(ip, collect_result)

    return {
        "success": True,
        "ip": ip,
        "protocol": protocol,
        "message": f"已删除 {protocol} 配置",
    }


@router.post("/api/path-trace")
async def path_trace(req: PathTraceRequest):
    """逻辑路径追踪（需求④）：推导源→目的经过哪些设备、在哪断，并给出断点原因。

    零连接：只用已采集的命令原文；`live=true` 时先对缺数据的设备补采集。
    """
    from services.path_trace import (
        TraceError,
        build_snapshots,
        format_trace_for_ai,
        trace_path,
    )

    devices = load_devices()
    if not devices:
        raise HTTPException(status_code=400, detail="设备列表为空，请先添加设备")
    if req.device_ips:
        wanted = set(req.device_ips)
        devices = [d for d in devices if d.get("ip") in wanted]
        if not devices:
            raise HTTPException(status_code=400, detail="指定的设备都不在设备列表中")

    snapshots = build_snapshots(devices, state.get("collection_results", {}))

    if req.live:
        # 补采集：只对一张表都没有的设备动手，避免重复劳动
        missing = [d for d, snap in zip(devices, snapshots) if not snap.get("has_data")]
        for dev in missing:
            try:
                record_collection(dev["ip"], collect_device_fn(dev))
            except Exception as e:  # 单台采集失败不应让整次追踪失败
                logger.warning("补采集 %s 失败: %s", dev.get("ip"), e)
        snapshots = build_snapshots(devices, state.get("collection_results", {}))

    try:
        result = trace_path(req.source_ip, req.dest_ip, snapshots)
    except TraceError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    result["ai_summary"] = format_trace_for_ai(result)
    result["devices_used"] = [s["device"] for s in snapshots]
    result["devices_without_data"] = [s["device"] for s in snapshots if not s.get("has_data")]
    return {"success": True, "trace": result}


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
    fast_mode: 使用快速采集
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
            "ok": False,
            "error": "设备不可达（Ping 超时）",
            "commands": {},
            "info": {},
        }
        record_collection(ip, result)
        return {"success": True, "result": result}

    result = collect_device_fn(device, fast_mode=fast_mode)
    record_collection(ip, result)
    return {"success": True, "result": result}


@router.post("/api/collect-all")
async def collect_all(fast_mode: bool = True, force: bool = True):
    """
    批量采集所有设备信息。

    优化策略：
    - 并行 ping（2 包）+ 并行采集
    - Console 为独占串口，顺序执行
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

        # 增量模式：上次在线且有数据，跳过采集
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
                "ok": False,
                "error": "设备不可达",
                "commands": {},
                "info": {},
            }

        # 并行采集（设备间并行）
        return collect_device_fn(device, fast_mode=fast_mode)

    # 并行采集所有设备（每台设备一个线程）
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(devices), 10)) as executor:
        future_map = {executor.submit(_collect_single, d): d for d in devices}
        for future in concurrent.futures.as_completed(future_map):
            try:
                result = future.result()
                results.append(result)
                record_collection(result["ip"], result)
            except Exception as e:
                device = future_map[future]
                logger.error(f"采集 {device['ip']} 时发生未预期异常: {e}")
                err_result = {
                    "ip": device["ip"],
                    "name": device.get("name", device["ip"]),
                    "online": False,
                    "ok": False,
                    "error": f"采集异常: {e}",
                    "commands": {},
                    "info": {},
                }
                results.append(err_result)
                record_collection(device["ip"], err_result)

    # 按 Console 端口排序返回
    results.sort(key=lambda r: port_sort_key(r["ip"]))
    logger.info(f"批量采集完成: {len(results)} 台设备")
    return {
        "success": True,
        "results": results,
        "devices": load_devices(),
    }


