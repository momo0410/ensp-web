"""
工作目录与拓扑库。

一个**工作目录**（workspace root）是一个拓扑库，里面每个子目录代表一个拓扑：

    <workspace_root>/
        .current                # 当前激活的拓扑名（纯文本，一行）
        campus/                 # 一个拓扑一个子目录
            campus.topo         # eNSP 拓扑定义（生成物）
            devices.json        # 项目设备表
            links.json          # 项目连线表
            configs/            # 设备配置的可读副本
            <UUID>/             # eNSP 读取的设备启动配置
                statemach.efs
                vrpcfg.zip
        lab-ospf/
            ...

工作目录本身可被用户指定，配置存在项目根目录的 app_settings.json。
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional

from app.config import APP_SETTINGS_FILE, BASE_DIR, logger

# 默认工作目录（用户未指定时）
DEFAULT_WORKSPACE_ROOT = BASE_DIR / "workspaces"

# 当前激活拓扑的记录文件名（放在工作目录根下）
_CURRENT_MARKER = ".current"

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_一-鿿-]{1,64}$")


class WorkspaceError(ValueError):
    """工作目录或拓扑名称不合法。"""


# ---------------------------------------------------------------------------
# 工作目录根
# ---------------------------------------------------------------------------
def _read_settings() -> dict:
    if not APP_SETTINGS_FILE.exists():
        return {}
    try:
        data = json.loads(APP_SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("app_settings.json 读取失败 (%s)，使用默认配置", e)
        return {}


def _write_settings(data: dict) -> None:
    APP_SETTINGS_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# 通用开关型设置（工作目录以外的零散偏好）
# ---------------------------------------------------------------------------
def get_setting(key: str, default=None):
    """读取单个设置项；不存在或读取失败时返回 default。"""
    try:
        return _read_settings().get(key, default)
    except Exception as e:  # pragma: no cover - 设置损坏时不应让功能崩掉
        logger.warning("读取设置 %s 失败 (%s)，使用默认值", key, e)
        return default


def set_setting(key: str, value) -> dict:
    """写入单个设置项（保留其他键）。"""
    data = _read_settings()
    data[key] = value
    _write_settings(data)
    logger.info("设置已更新: %s = %r", key, value)
    return data


def get_workspace_root() -> Path:
    """当前工作目录；未配置或不可用时回退到默认值。"""
    raw = _read_settings().get("workspace_root")
    root = Path(raw) if raw else DEFAULT_WORKSPACE_ROOT
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.error("工作目录 %s 不可用 (%s)，回退到默认目录", root, e)
        root = DEFAULT_WORKSPACE_ROOT
        root.mkdir(parents=True, exist_ok=True)
    return root


def set_workspace_root(path: str) -> dict:
    """设置工作目录（必须可创建/可写）。"""
    if not path or not str(path).strip():
        raise WorkspaceError("工作目录不能为空")
    root = Path(str(path).strip())
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        raise WorkspaceError(f"目录不可写: {root} ({e})") from e

    settings = _read_settings()
    settings["workspace_root"] = str(root)
    _write_settings(settings)
    logger.info("工作目录已设置为 %s", root)
    return {"ok": True, "workspace_root": str(root)}


# ---------------------------------------------------------------------------
# 拓扑名与路径
# ---------------------------------------------------------------------------
def _validate_name(name: str) -> str:
    name = (name or "").strip()
    if not _SAFE_NAME_RE.match(name):
        raise WorkspaceError(
            "拓扑名只能包含字母、数字、下划线、连字符和中文，长度 1-64"
        )
    if name.startswith("."):
        raise WorkspaceError("拓扑名不能以点开头")
    return name


def topology_dir(name: str) -> Path:
    """某拓扑的目录（不保证存在）。"""
    return get_workspace_root() / _validate_name(name)


def topo_path_for(name: str) -> Path:
    """某拓扑的 .topo 文件路径。"""
    safe = _validate_name(name)
    return topology_dir(safe) / f"{safe}.topo"


def devices_path_for(name: str) -> Path:
    return topology_dir(name) / "devices.json"


def links_path_for(name: str) -> Path:
    return topology_dir(name) / "links.json"


def configs_dir_for(name: str) -> Path:
    """设备配置（可读副本）目录。"""
    return topology_dir(name) / "configs"


# ---------------------------------------------------------------------------
# 激活拓扑
# ---------------------------------------------------------------------------
def active_topology() -> Optional[str]:
    """当前激活的拓扑名；没有任何拓扑时返回 None。

    **只读语义**：标记缺失或损坏时按名字顺序回退，但**不写回**。
    曾经这里会 `set_active(tops[0])` 把回退结果持久化——配合 `set_active`
    的非原子写入（先截断再写），并发读只要撞上写入窗口就会读到空文件，
    于是「回退到字母序第一个拓扑」被永久钉死。实测中它把激活拓扑从
    15 台设备的 `capability_probe` 悄悄换成了空的 `campus`（字母序在前），
    表现为「设备表突然空了」。改为不写回后，标记损坏只是本次读不到。
    """
    root = get_workspace_root()
    marker = root / _CURRENT_MARKER
    if marker.exists():
        try:
            name = marker.read_text(encoding="utf-8").strip()
            if name and topology_dir(name).is_dir():
                return name
        except OSError:
            pass
    # 标记缺失或指向不存在的拓扑：只回退，不写盘
    tops = list_topologies()
    return tops[0]["name"] if tops else None


def set_active(name: str) -> dict:
    """把某拓扑设为激活（原子写入，避免并发读到半截文件）。"""
    safe = _validate_name(name)
    d = topology_dir(safe)
    if not d.is_dir():
        raise WorkspaceError(f"拓扑不存在: {safe}")
    marker = get_workspace_root() / _CURRENT_MARKER
    tmp = marker.with_suffix(".tmp")
    tmp.write_text(safe, encoding="utf-8")
    tmp.replace(marker)
    return {"ok": True, "active": safe}


# ---------------------------------------------------------------------------
# 拓扑库增删查
# ---------------------------------------------------------------------------
def list_topologies() -> List[dict]:
    """扫描工作目录，列出所有拓扑。"""
    root = get_workspace_root()
    out: List[dict] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if not child.is_dir() or child.name.startswith("."):
            continue
        devices = _read_json_list(child / "devices.json")
        links = _read_json_list(child / "links.json")
        topo = child / f"{child.name}.topo"
        try:
            mtime = max(
                (p.stat().st_mtime for p in (topo, child / "devices.json", child / "links.json")
                 if p.exists()),
                default=child.stat().st_mtime,
            )
        except OSError:
            mtime = 0
        out.append({
            "name": child.name,
            "path": str(child),
            "devices": len(devices),
            "links": len(links),
            "modified": mtime,
        })
    return out


def load_topology(name: str) -> dict:
    """读取某拓扑的设备与连线。"""
    safe = _validate_name(name)
    return {
        "name": safe,
        "devices": _read_json_list(devices_path_for(safe)),
        "links": _read_json_list(links_path_for(safe)),
    }


def save_topology(name: str, devices: List[dict], links: Optional[List[dict]] = None) -> dict:
    """写回某拓扑的设备与连线（原子写）。"""
    safe = _validate_name(name)
    d = topology_dir(safe)
    d.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(d / "devices.json", devices)
    if links is not None:
        _atomic_write_json(d / "links.json", links)
    return {"ok": True, "name": safe, "path": str(d)}


def create_topology(name: str) -> dict:
    """新建一个空拓扑目录。"""
    safe = _validate_name(name)
    d = topology_dir(safe)
    if d.exists():
        raise WorkspaceError(f"拓扑已存在: {safe}")
    d.mkdir(parents=True)
    (d / "configs").mkdir(exist_ok=True)
    _atomic_write_json(d / "devices.json", [])
    _atomic_write_json(d / "links.json", [])
    logger.info("已创建拓扑 %s", safe)
    if active_topology() is None:
        set_active(safe)
    return {"ok": True, "name": safe, "path": str(d)}


def delete_topology(name: str) -> dict:
    """删除拓扑目录（连同其配置）。"""
    safe = _validate_name(name)
    d = topology_dir(safe)
    if not d.is_dir():
        raise WorkspaceError(f"拓扑不存在: {safe}")
    shutil.rmtree(d)
    logger.info("已删除拓扑 %s", safe)
    # 若删的是激活拓扑，切换到剩下的第一个（并显式落盘，不留悬空标记）
    marker = get_workspace_root() / _CURRENT_MARKER
    try:
        current = marker.read_text(encoding="utf-8").strip() if marker.exists() else ""
    except OSError:
        current = ""
    if current == safe:
        marker.unlink(missing_ok=True)
        fallback = active_topology()
        if fallback:
            set_active(fallback)
    return {"ok": True, "name": safe}


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _read_json_list(path: Path) -> List[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("%s 读取失败: %s", path.name, e)
        return []


def _atomic_write_json(path: Path, data) -> None:
    """先写临时文件再替换，避免写到一半崩溃导致文件损坏。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
