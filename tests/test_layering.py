"""分层方向守卫：`app/ ← domain/ ← routes_*.py`，services 不得反向依赖路由层。

历史上 services 曾 `from routes_ai import _PLAN_SYSTEM`，把系统提示词定义在路由层，
导致服务层反向依赖 HTTP 层、也埋下了循环 import 的隐患。这里用 AST 静态扫描
把约定钉死，避免以后再漂回去。
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOWER_LAYERS = ("app", "domain", "services")


def _reverse_imports() -> list:
    """找出下层模块里对 routes_*/main 的导入。"""
    hits = []
    for layer in LOWER_LAYERS:
        for path in sorted((ROOT / layer).glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""] if node.level == 0 else []
                else:
                    continue
                for name in names:
                    top = name.split(".")[0]
                    if top.startswith("routes_") or top == "main":
                        hits.append(f"{path.relative_to(ROOT)}:{node.lineno} -> {name}")
    return hits


def test_下层模块不导入路由层():
    hits = _reverse_imports()
    assert hits == [], "services/domain/app 不得 import routes_* 或 main：\n" + "\n".join(hits)


def test_plan_系统提示词定义在服务层():
    """prompt 与校验逻辑同处一处，三处消费者（路由 / Agent 工具 / 一键刷配置）共用。"""
    from services import ai_plan

    assert isinstance(ai_plan.PLAN_SYSTEM, str)
    assert "JSON plan" in ai_plan.PLAN_SYSTEM
    assert "VRP" in ai_plan.PLAN_SYSTEM


def test_路由层不再自带_plan_system():
    import routes_ai

    assert not hasattr(routes_ai, "_PLAN_SYSTEM"), (
        "提示词应只在 services/ai_plan.py 定义一处，路由层不要重新复制一份")
