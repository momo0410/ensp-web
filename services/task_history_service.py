"""
task_history_service.py - 任务历史记录服务
==========================================
负责保存和查询配置下发任务的历史记录。
记录保存在 tasks/ 目录下，JSON 格式。
"""

import os
import json
import logging
import uuid
from typing import Dict, Any, List, Optional
from datetime import datetime

logger = logging.getLogger("ensp-web.tasks")

# 任务目录
TASKS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tasks")


def _ensure_dir():
    """确保任务目录存在。"""
    os.makedirs(TASKS_DIR, exist_ok=True)


def generate_task_id() -> str:
    """生成唯一任务 ID。"""
    now = datetime.now()
    return f"task_{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"


def save_task_record(record: Dict[str, Any]) -> str:
    """
    保存任务记录。

    Args:
        record: 任务记录字典

    Returns:
        task_id
    """
    _ensure_dir()

    task_id = record.get("task_id") or generate_task_id()
    record["task_id"] = task_id

    filepath = os.path.join(TASKS_DIR, f"{task_id}.json")

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        logger.info("任务记录已保存: %s", task_id)
    except Exception as e:
        logger.error("保存任务记录失败: %s", e)

    return task_id


def list_tasks(limit: int = 50) -> List[Dict[str, Any]]:
    """
    列出历史任务（摘要信息）。

    Args:
        limit: 最大返回数量

    Returns:
        任务列表（按时间倒序）
    """
    _ensure_dir()
    tasks = []

    for filename in os.listdir(TASKS_DIR):
        if not filename.endswith(".json"):
            continue
        filepath = os.path.join(TASKS_DIR, filename)
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 只返回摘要
            tasks.append({
                "task_id": data.get("task_id", filename.replace(".json", "")),
                "task_name": data.get("task_name", "未命名"),
                "status": data.get("status", "unknown"),
                "target_devices": data.get("target_devices", []),
                "summary": data.get("summary", {}),
                "start_time": data.get("start_time", ""),
                "end_time": data.get("end_time", ""),
            })
        except Exception as e:
            logger.warning("读取任务 %s 失败: %s", filename, e)

    # 按开始时间倒序
    tasks.sort(key=lambda t: t.get("start_time", ""), reverse=True)
    return tasks[:limit]


def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    """
    获取任务详情。

    Args:
        task_id: 任务 ID

    Returns:
        完整任务记录，不存在返回 None
    """
    _ensure_dir()

    # 安全检查
    safe_id = "".join(c for c in task_id if c.isalnum() or c in "-_")
    filepath = os.path.join(TASKS_DIR, f"{safe_id}.json")

    if not os.path.exists(filepath):
        return None

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("读取任务 %s 失败: %s", task_id, e)
        return None


def delete_task(task_id: str) -> Dict[str, Any]:
    """
    删除任务记录。

    Args:
        task_id: 任务 ID

    Returns:
        {"success": bool, "message": str}
    """
    _ensure_dir()
    safe_id = "".join(c for c in task_id if c.isalnum() or c in "-_")
    filepath = os.path.join(TASKS_DIR, f"{safe_id}.json")

    if not os.path.exists(filepath):
        return {"success": False, "message": f"任务 '{task_id}' 不存在"}

    try:
        os.remove(filepath)
        logger.info("任务记录已删除: %s", task_id)
        return {"success": True, "message": f"任务 '{task_id}' 已删除"}
    except Exception as e:
        logger.error("删除任务 %s 失败: %s", task_id, e)
        return {"success": False, "message": f"删除失败: {e}"}

