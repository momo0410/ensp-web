"""
script_service.py - 脚本模板管理服务
====================================
负责脚本的保存、读取、删除、列表。
脚本保存在 scripts/ 目录下，JSON 格式。
"""

import os
import json
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime

logger = logging.getLogger("ensp-web.scripts")

# 脚本目录
SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")


def _ensure_dir():
    """确保脚本目录存在。"""
    os.makedirs(SCRIPTS_DIR, exist_ok=True)


def _safe_name(name: str) -> str:
    """清理文件名，防止路径遍历。"""
    return "".join(c for c in name if c.isalnum() or c in "-_. ").strip()


def list_scripts() -> List[Dict[str, Any]]:
    """
    列出所有脚本。

    Returns:
        脚本列表 [{"name": str, "description": str, "created_at": str, "updated_at": str}]
    """
    _ensure_dir()
    scripts = []

    for filename in os.listdir(SCRIPTS_DIR):
        if not filename.endswith(".json"):
            continue
        filepath = os.path.join(SCRIPTS_DIR, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            scripts.append({
                "name": data.get("script_name", filename.replace(".json", "")),
                "description": data.get("description", ""),
                "created_at": data.get("created_at", ""),
                "updated_at": data.get("updated_at", ""),
            })
        except Exception as e:
            logger.warning("读取脚本 %s 失败: %s", filename, e)

    # 按更新时间排序
    scripts.sort(key=lambda s: s.get("updated_at", ""), reverse=True)
    return scripts


def get_script(name: str) -> Optional[Dict[str, Any]]:
    """
    读取指定脚本。

    Args:
        name: 脚本名称

    Returns:
        脚本内容字典，不存在返回 None
    """
    _ensure_dir()
    safe = _safe_name(name)
    filepath = os.path.join(SCRIPTS_DIR, f"{safe}.json")

    if not os.path.exists(filepath):
        return None

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("读取脚本 %s 失败: %s", name, e)
        return None


def save_script(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    保存脚本。

    Args:
        data: 脚本数据，包含 script_name, description, command_template 等

    Returns:
        {"success": bool, "message": str, "name": str}
    """
    _ensure_dir()

    name = data.get("script_name", "").strip()
    if not name:
        return {"success": False, "message": "脚本名称不能为空", "name": ""}

    safe = _safe_name(name)
    if not safe:
        return {"success": False, "message": "脚本名称包含无效字符", "name": ""}

    filepath = os.path.join(SCRIPTS_DIR, f"{safe}.json")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 如果已存在，保留创建时间
    existing = get_script(safe)
    created_at = existing.get("created_at", now) if existing else now

    script_data = {
        "script_name": name,
        "description": data.get("description", ""),
        "command_template": data.get("command_template", []),
        "global_vars": data.get("global_vars", {}),
        "sample_device_vars": data.get("sample_device_vars", {}),
        "verify_commands": data.get("verify_commands", []),
        "created_at": created_at,
        "updated_at": now,
    }

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(script_data, f, ensure_ascii=False, indent=2)
        logger.info("脚本已保存: %s", safe)
        return {"success": True, "message": f"脚本 '{name}' 已保存", "name": safe}
    except Exception as e:
        logger.error("保存脚本 %s 失败: %s", name, e)
        return {"success": False, "message": f"保存失败: {e}", "name": ""}


def delete_script(name: str) -> Dict[str, Any]:
    """
    删除脚本。

    Args:
        name: 脚本名称

    Returns:
        {"success": bool, "message": str}
    """
    _ensure_dir()
    safe = _safe_name(name)
    filepath = os.path.join(SCRIPTS_DIR, f"{safe}.json")

    if not os.path.exists(filepath):
        return {"success": False, "message": f"脚本 '{name}' 不存在"}

    try:
        os.remove(filepath)
        logger.info("脚本已删除: %s", safe)
        return {"success": True, "message": f"脚本 '{name}' 已删除"}
    except Exception as e:
        logger.error("删除脚本 %s 失败: %s", name, e)
        return {"success": False, "message": f"删除失败: {e}"}

