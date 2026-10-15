"""
app_settings.py - 用户设置项的集中定义与读写
=============================================
把散落在代码里的开关收拢到一处，供 HTTP 路由暴露给前端设置面板。

设置项分两类：
- **即时生效**：后端每次调用前现读，改完立刻影响下一次操作；
- **需重启/仅启动时读一次**：如 auto_launch_ensp 只在进程启动时判断，
  改完要重启进程才生效（前端会标注）。

新增开关必须在这里登记（含默认值、说明、是否需重启），
否则前端设置面板看不到它。
"""

from __future__ import annotations

from typing import Any, Dict, List

from app.config import logger
from domain import workspace as ws


class SettingsError(ValueError):
    """设置项不存在或取值不合法。"""


# 全部可配置项。顺序即前端展示顺序。
#   restart: True 表示改动需要重启后端进程才生效
SETTINGS_SCHEMA: List[dict] = [
    {
        "key": "auto_launch_ensp",
        "type": "bool",
        "default": True,
        "restart": True,
        "label": "项目启动时自动拉起 eNSP",
        "desc": "后端启动后自动启动 eNSP 客户端并注入控制模块；关闭后需手动开 eNSP",
    },
    {
        "key": "auto_sync_ensp_topo",
        "type": "bool",
        "default": True,
        "restart": False,
        "label": "切换/新建拓扑时自动在 eNSP 中打开",
        "desc": "在项目里切换或新建拓扑后，立即让 eNSP 加载对应的 .topo 文件",
    },
    {
        "key": "watch_ensp_topology",
        "type": "bool",
        "default": True,
        "restart": False,
        "label": "监测 eNSP 侧拓扑变化",
        "desc": "前端轮询 eNSP，检测到用户在 eNSP 里换了拓扑时提示对齐或导入",
    },
    {
        "key": "ensp_poll_interval_ms",
        "type": "int",
        "default": 3000,
        "min": 1000,
        "max": 30000,
        "restart": False,
        "label": "eNSP 轮询间隔（毫秒）",
        "desc": "前端多久问一次 eNSP 的实时状态；越小越灵敏，占用越高",
    },
    {
        "key": "auto_start_devices",
        "type": "bool",
        "default": True,
        "restart": False,
        "label": "应用拓扑后自动开机",
        "desc": "一键应用拓扑时，生成 .topo 后立即启动全部设备",
    },
]

_BY_KEY = {item["key"]: item for item in SETTINGS_SCHEMA}


def _coerce(item: dict, value: Any) -> Any:
    """按 schema 校验并转换单个设置值。"""
    kind = item["type"]
    if kind == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("1", "true", "yes", "on"):
                return True
            if low in ("0", "false", "no", "off"):
                return False
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        raise SettingsError(f"{item['key']} 只接受 true/false，得到 {value!r}")

    if kind == "int":
        try:
            num = int(value)
        except (TypeError, ValueError):
            raise SettingsError(f"{item['key']} 只接受整数，得到 {value!r}") from None
        minimum, maximum = item.get("min"), item.get("max")
        if minimum is not None and num < minimum:
            raise SettingsError(f"{item['key']} 不能小于 {minimum}（当前 {num}）")
        if maximum is not None and num > maximum:
            raise SettingsError(f"{item['key']} 不能大于 {maximum}（当前 {num}）")
        return num

    raise SettingsError(f"{item['key']} 的类型 {kind} 暂不支持")


def list_settings() -> Dict[str, Any]:
    """返回全部设置项的当前值（附 schema 供前端渲染表单）。"""
    current = {}
    for item in SETTINGS_SCHEMA:
        raw = ws.get_setting(item["key"], item["default"])
        try:
            current[item["key"]] = _coerce(item, raw)
        except SettingsError as e:
            # 配置里存了非法值：退回默认值并告知，不让整个面板打不开
            logger.warning("设置 %s 取值非法 (%s)，使用默认值 %r", item["key"], e, item["default"])
            current[item["key"]] = item["default"]
    return {
        "success": True,
        "settings": current,
        "schema": SETTINGS_SCHEMA,
    }


def update_settings(patch: Dict[str, Any]) -> Dict[str, Any]:
    """批量更新设置项（只更新 patch 里出现的键，其余不动）。"""
    if not isinstance(patch, dict) or not patch:
        raise SettingsError("没有要更新的设置项")

    unknown = [k for k in patch if k not in _BY_KEY]
    if unknown:
        raise SettingsError(f"未知设置项: {', '.join(sorted(unknown))}")

    coerced = {}
    for key, value in patch.items():
        coerced[key] = _coerce(_BY_KEY[key], value)

    for key, value in coerced.items():
        ws.set_setting(key, value)

    # 需要重启的项改完后明确告知，避免用户以为没生效
    need_restart = [k for k in coerced if _BY_KEY[k].get("restart")]
    result = list_settings()
    result["updated"] = coerced
    result["need_restart"] = need_restart
    result["message"] = (
        f"以下设置需重启后端进程后生效: {', '.join(need_restart)}" if need_restart else "设置已保存"
    )
    return result
