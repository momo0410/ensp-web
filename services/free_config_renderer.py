"""
free_config_renderer.py - 自由配置命令渲染器
=============================================
负责变量替换、命令清洗、危险命令检查。
不连接设备，只做文本处理。
"""

import re
from typing import List, Dict, Any, Tuple, Optional


# 危险命令关键字（阻止执行）
DANGEROUS_COMMANDS = [
    "reboot",
    "reset saved-configuration",
    "delete",
    "format",
    "startup saved-configuration",
    "undo interface",
    "undo ip route-static all",
]

# 需要在预览阶段警告的命令
WARNING_COMMANDS = [
    "save",
    "return",
]


def clean_commands(raw_lines: List[str]) -> Tuple[List[str], List[str], List[str]]:
    """
    清洗命令列表。

    Args:
        raw_lines: 原始命令行列表

    Returns:
        (cleaned_commands, warnings, skipped_lines)
        - cleaned_commands: 清洗后的命令列表
        - warnings: 警告信息列表
        - skipped_lines: 被跳过的行
    """
    cleaned = []
    warnings = []
    skipped = []

    for line in raw_lines:
        # 去除首尾空格
        line = line.strip()

        # 跳过空行
        if not line:
            skipped.append("(空行)")
            continue

        # 跳过注释行（以 # 开头）
        if line.startswith("#"):
            skipped.append(line)
            continue

        # 检查 save 命令
        if line.lower() == "save":
            warnings.append(f"检测到 'save' 命令，请使用 save_after_success 选项自动保存")
            skipped.append(line)
            continue

        # 检查 system-view（不自动添加，如果用户写了则警告）
        if line.lower() in ("system-view", "sys", "sys-view"):
            warnings.append(f"检测到 '{line}'，系统会自动进入系统视图，已跳过")
            skipped.append(line)
            continue

        # 检查 return 命令
        if line.lower() == "return":
            warnings.append(f"检测到 'return' 命令，已自动过滤")
            skipped.append(line)
            continue

        cleaned.append(line)

    return cleaned, warnings, skipped


def check_dangerous_commands(commands: List[str], force: bool = False) -> Tuple[bool, List[str]]:
    """
    检查危险命令。

    Args:
        commands: 命令列表
        force: 是否强制允许

    Returns:
        (is_safe, dangerous_list)
        - is_safe: True 表示安全，False 表示有危险命令
        - dangerous_list: 检测到的危险命令列表
    """
    dangerous_found = []

    for cmd in commands:
        cmd_lower = cmd.lower().strip()
        for danger in DANGEROUS_COMMANDS:
            if danger.lower() in cmd_lower:
                dangerous_found.append({
                    "command": cmd,
                    "reason": f"包含危险关键字: {danger}"
                })

    if dangerous_found and not force:
        return False, dangerous_found
    return True, dangerous_found


def extract_variables(commands: List[str]) -> List[str]:
    """
    从命令模板中提取变量名。

    变量格式: {{variable_name}}

    Args:
        commands: 命令列表

    Returns:
        变量名列表（去重）
    """
    var_pattern = re.compile(r"\{\{(\w+)\}\}")
    variables = set()

    for cmd in commands:
        matches = var_pattern.findall(cmd)
        variables.update(matches)

    return sorted(list(variables))


def merge_variables(global_vars: Dict[str, str], device_vars: Dict[str, str]) -> Dict[str, str]:
    """
    合并全局变量和设备变量。
    设备变量优先级高于全局变量。

    Args:
        global_vars: 全局变量
        device_vars: 设备变量

    Returns:
        合并后的变量字典
    """
    merged = dict(global_vars or {})
    if device_vars:
        merged.update(device_vars)
    return merged


def render_commands(
    commands: List[str],
    global_vars: Dict[str, str],
    device_vars: Dict[str, str],
) -> Tuple[List[str], List[str]]:
    """
    渲染命令模板，替换变量。

    Args:
        commands: 命令模板列表（已清洗）
        global_vars: 全局变量
        device_vars: 设备特定变量

    Returns:
        (rendered_commands, undefined_vars)
        - rendered_commands: 替换变量后的命令
        - undefined_vars: 未定义的变量列表
    """
    merged = merge_variables(global_vars, device_vars)
    var_pattern = re.compile(r"\{\{(\w+)\}\}")
    rendered = []
    undefined = set()

    for cmd in commands:
        # 查找所有变量
        vars_in_cmd = var_pattern.findall(cmd)

        # 检查未定义变量
        for var in vars_in_cmd:
            if var not in merged:
                undefined.add(var)

        # 替换变量
        def replace_var(match):
            var_name = match.group(1)
            if var_name in merged:
                return str(merged[var_name])
            return match.group(0)  # 保持原样

        rendered_cmd = var_pattern.sub(replace_var, cmd)
        rendered.append(rendered_cmd)

    return rendered, sorted(list(undefined))


def render_preview(
    task: Dict[str, Any],
) -> Dict[str, Any]:
    """
    生成完整预览。

    Args:
        task: 配置任务字典

    Returns:
        {
            "success": bool,
            "preview": {ip: {"device_name": str, "commands": [...]}},
            "errors": [...],
            "warnings": [...],
            "variables_used": [...],
        }
    """
    command_template = task.get("command_template", [])
    global_vars = task.get("global_vars", {})
    device_vars = task.get("device_vars", {})
    target_devices = task.get("target_devices", [])

    result = {
        "success": True,
        "preview": {},
        "errors": [],
        "warnings": [],
        "variables_used": [],
    }

    # 清洗命令
    if isinstance(command_template, str):
        raw_lines = command_template.split("\n")
    else:
        raw_lines = command_template

    cleaned, warnings, skipped = clean_commands(raw_lines)
    result["warnings"].extend(warnings)

    if not cleaned:
        result["success"] = False
        result["errors"].append("命令模板为空或全部被过滤")
        return result

    # 检查危险命令
    is_safe, dangerous = check_dangerous_commands(cleaned)
    if not is_safe:
        result["success"] = False
        for d in dangerous:
            result["errors"].append(f"危险命令 '{d['command']}': {d['reason']}")
        return result

    # 提取变量
    all_vars = extract_variables(cleaned)
    result["variables_used"] = all_vars

    # 为每台设备渲染
    for ip in target_devices:
        dev_vars = device_vars.get(ip, {})
        rendered, undefined = render_commands(cleaned, global_vars, dev_vars)

        if undefined:
            result["success"] = False
            result["errors"].append(
                f"设备 {ip} 存在未定义变量: {', '.join(undefined)}"
            )
            continue

        # 获取设备名
        device_name = dev_vars.get("hostname", dev_vars.get("name", ip))

        result["preview"][ip] = {
            "device_name": device_name,
            "commands": rendered,
        }

    if not result["preview"] and target_devices:
        result["success"] = False
        if not result["errors"]:
            result["errors"].append("无法生成任何设备的预览")

    return result


def validate_task(task: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    验证配置任务结构。

    Returns:
        (is_valid, errors)
    """
    errors = []

    if not task.get("target_devices"):
        errors.append("未选择目标设备")

    if not task.get("command_template"):
        errors.append("命令模板为空")

    return len(errors) == 0, errors
