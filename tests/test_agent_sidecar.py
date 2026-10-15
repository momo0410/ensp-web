"""Agent sidecar 链路与内部端点的离线单测。

覆盖三件事：
1. sidecar 就绪检查与状态（不启动真进程）
2. 内部端点的令牌校验（安全边界）
3. 工具执行端点返回结构化结果、且**没有任何下发工具**

不依赖 Node 进程与真机：只测 Python 侧契约。
"""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import agent_sidecar
from services.agent_sidecar import AgentSidecar, SidecarError, sidecar_ready


class TestSidecarReady:
    def test_就绪检查返回必备字段(self):
        r = sidecar_ready()
        for key in ("node", "node_ok", "entry_ok", "deps_ok", "ready"):
            assert key in r, f"缺少 {key}"

    def test_entry_存在(self):
        """agent_node/server.mjs 必须存在（否则 sidecar 起不来）。"""
        assert agent_sidecar.ENTRY.exists(), f"缺少入口 {agent_sidecar.ENTRY}"

    def test_依赖已安装(self):
        """pi 依赖必须已安装（CI 里需先 npm install）。"""
        r = sidecar_ready()
        assert r["deps_ok"], "未安装 pi 依赖：在 agent_node/ 下执行 npm install"


class TestSidecarState:
    def test_初始未运行(self):
        s = AgentSidecar()
        assert s.is_running() is False
        assert s.port is None
        assert s.base_url() is None

    def test_token_非空且随机(self):
        a, b = AgentSidecar(), AgentSidecar()
        assert a.token and b.token
        assert a.token != b.token, "两个实例的 token 不应相同"
        assert len(a.token) >= 20, "token 太短，容易被猜"

    def test_status_结构完整(self):
        s = AgentSidecar()
        st = s.status()
        for key in ("running", "port", "pid", "uptime_s", "last_error", "ready"):
            assert key in st

    def test_stop_在未启动时安全(self):
        """未启动时 stop 不应抛异常。"""
        AgentSidecar().stop()

    def test_缺少_node_时报可读错误(self, monkeypatch):
        s = AgentSidecar()
        monkeypatch.setattr(agent_sidecar, "node_available", lambda: None)
        with pytest.raises(SidecarError) as ei:
            s.ensure_started()
        assert "node" in str(ei.value)

    def test_缺少依赖_时报可读错误(self, monkeypatch, tmp_path):
        s = AgentSidecar()
        monkeypatch.setattr(agent_sidecar, "SIDECAR_DIR", tmp_path)
        monkeypatch.setattr(agent_sidecar, "ENTRY", tmp_path / "nope.mjs")
        with pytest.raises(SidecarError):
            s.ensure_started()


class TestInternalEndpoints:
    """内部端点只允许带正确令牌的 sidecar 调用。"""

    @pytest.fixture()
    def client(self):
        import main
        return TestClient(main.app)

    def test_工具清单无令牌被拒(self, client):
        r = client.get("/api/agent/internal/tools")
        assert r.status_code == 401

    def test_工具清单错令牌被拒(self, client):
        r = client.get("/api/agent/internal/tools",
                       headers={"X-Agent-Token": "wrong"})
        assert r.status_code == 401

    def test_工具清单正确令牌放行(self, client):
        r = client.get("/api/agent/internal/tools",
                       headers={"X-Agent-Token": agent_sidecar.sidecar.token})
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data["tools"], list) and data["tools"]
        # 每条都要能被 sidecar 直接包装成 pi 工具
        for t in data["tools"]:
            assert "kind" in t
            fn = t["function"]
            assert fn["name"] and fn["description"]
            assert fn["parameters"]["type"] == "object"

    def test_执行端点无令牌被拒(self, client):
        r = client.post("/api/agent/internal/tools/execute",
                        json={"name": "list_devices", "args": {}})
        assert r.status_code == 401

    def test_执行未知工具返回结构化失败而非_500(self, client):
        r = client.post("/api/agent/internal/tools/execute",
                        json={"name": "no_such_tool", "args": {}},
                        headers={"X-Agent-Token": agent_sidecar.sidecar.token})
        assert r.status_code == 200, "未知工具应返回结构化失败，不是 500"
        body = r.json()
        assert body["ok"] is False
        assert "未知工具" in body["error"]

    def test_执行_list_devices_成功(self, client, monkeypatch):
        import routes_agent
        monkeypatch.setattr(routes_agent, "load_devices",
                            lambda: [{"name": "R1", "ip": "127.0.0.1:2000",
                                      "model": "Router"}])
        r = client.post("/api/agent/internal/tools/execute",
                        json={"name": "list_devices", "args": {}},
                        headers={"X-Agent-Token": agent_sidecar.sidecar.token})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["devices"][0]["name"] == "R1"


class TestSecurityBoundaryUnchanged:
    """换引擎（自研循环 → pi）后，安全边界必须依然成立。"""

    def test_工具集里没有下发工具(self):
        from services.agent_tools import TOOLS, tool_names
        banned = ("deploy", "apply", "push", "run_config", "write_config", "commit")
        for n in tool_names():
            low = n.lower()
            assert not any(w in low for w in banned), f"{n} 含下发语义"

    def test_只有_read_与_prepare_两类(self):
        from services.agent_tools import TOOLS
        assert {t.kind for t in TOOLS.values()} == {"read", "prepare"}

    def test_sidecar_schema_与注册表一致(self):
        from services.agent_tools import TOOLS, sidecar_schemas
        sc = sidecar_schemas()
        assert len(sc) == len(TOOLS)
        assert {s["function"]["name"] for s in sc} == set(TOOLS)

    def test_agent_对外路由里没有下发端点(self):
        import main
        paths = [getattr(r, "path", "") or "" for r in main.app.routes]
        # 嵌套路由在本 FastAPI 版本里是 _IncludedRouter，用 openapi 校验更可靠
        spec = main.app.openapi()
        agent_paths = [p for p in spec["paths"] if p.startswith("/api/agent")]
        assert "/api/agent/chat" in agent_paths
        for p in agent_paths:
            if p.startswith("/api/agent/internal"):
                continue
            assert "deploy" not in p and "apply" not in p, f"{p} 像下发端点"


class TestNoSilentFallback:
    """只保留 pi 引擎：sidecar 不可用时必须明确报错，不得静默换成别的引擎。"""

    def test_sidecar_不可用时抛_503_并说明原因(self, monkeypatch):
        import routes_agent
        monkeypatch.setattr(agent_sidecar.sidecar, "is_running", lambda: False)
        monkeypatch.setattr(agent_sidecar.sidecar, "base_url", lambda: None)

        def _boom(*a, **kw):
            raise SidecarError("未找到 node 可执行文件")

        monkeypatch.setattr(agent_sidecar.sidecar, "ensure_started", _boom)
        import services.ai_assistant as ai
        monkeypatch.setattr(ai, "is_configured", lambda cfg=None: True)

        with pytest.raises(Exception) as ei:
            routes_agent._ensure_sidecar()
        msg = str(ei.value)
        assert "503" in msg or "node" in msg

    def test_AI_未配置时仍抛_400(self, monkeypatch):
        import routes_agent
        import services.ai_assistant as ai
        monkeypatch.setattr(ai, "is_configured", lambda cfg=None: False)
        with pytest.raises(Exception) as ei:
            routes_agent._ensure_sidecar()
        assert "400" in str(ei.value) or "未配置" in str(ei.value)

    def test_内置_python_引擎已移除(self):
        """agent_runtime 已删除，不得再被引用——只留 pi。"""
        import importlib
        with pytest.raises(ImportError):
            importlib.import_module("services.agent_runtime")

    def test_路由里不再有降级分支(self):
        import inspect
        import routes_agent
        src = inspect.getsource(routes_agent)
        assert "_stream_python_engine" not in src
        assert "builtin-python" not in src

    def test_事件契约齐全(self):
        """pi 事件映射到前端契约：start/tool_call/tool_result/answer/error/done。"""
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "agent_node" / "server.mjs").read_text(encoding="utf-8")
        for expected in ("tool_call", "tool_result", "answer", "error", "done", "start"):
            assert f'"{expected}"' in src, f"sidecar 缺少事件类型 {expected}"


class TestOrphanPrevention:
    """Python 被强杀（无 SIGTERM，shutdown 钩子不跑）时不得留下孤儿 sidecar。"""

    def test_sidecar_有父进程看护(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "agent_node" / "server.mjs").read_text(encoding="utf-8")
        assert "AGENT_PARENT_PID" in src, "sidecar 未实现父进程看护"
        assert "process.kill(pid, 0)" in src, "未做父进程存活探测"

    def test_启动时传入父进程_pid(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "services" / "agent_sidecar.py").read_text(encoding="utf-8")
        assert "AGENT_PARENT_PID" in src, "Python 未把父 pid 传给 sidecar"

    def test_信号处理仍保留(self):
        """正常退出路径（CTRL+C / 正常关闭）也要能停。"""
        from pathlib import Path
        src = (Path(__file__).resolve().parent.parent
               / "agent_node" / "server.mjs").read_text(encoding="utf-8")
        assert "SIGTERM" in src and "SIGINT" in src
