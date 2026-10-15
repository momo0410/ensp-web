"""services/agent_tools.py 的离线单测。

重点验证**安全边界在结构上成立**：工具集里不存在任何直接下发配置的工具，
Agent 能做的最重动作是 propose_config（只产出待确认方案）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services import agent_tools as T
from services.agent_tools import AgentContext, ToolError, call_tool


def _ctx(**kw):
    base = dict(
        devices=[
            {"name": "R1", "ip": "127.0.0.1:2000", "model": "Router",
             "console_port": 2000},
            {"name": "R2", "ip": "127.0.0.1:2001", "model": "S5700",
             "console_port": 2001},
        ],
        capability_results={},
        collection_results={},
        allow_device_io=False,          # 单测绝不连真机
    )
    base.update(kw)
    return AgentContext(**base)


# ---------------------------------------------------------------------------
# 安全边界：这是最重要的一组断言
# ---------------------------------------------------------------------------
class TestSecurityBoundary:
    def test_没有直接下发的工具(self):
        """工具集里不得出现任何"直接刷入设备"语义的工具名。"""
        banned = ("deploy", "apply", "push", "execute", "run_config",
                  "write_config", "commit", "send_config")
        for name in T.tool_names():
            lowered = name.lower()
            for word in banned:
                assert word not in lowered, \
                    f"工具 {name} 名字含高危语义 {word}——Agent 不应拥有下发能力"

    def test_只有两种工具类型(self):
        kinds = {t.kind for t in T.TOOLS.values()}
        assert kinds == {"read", "prepare"}

    def test_prepare_类工具只有两个(self):
        prepares = [t.name for t in T.TOOLS.values() if t.kind == "prepare"]
        assert sorted(prepares) == ["propose_config", "propose_topology"]

    def test_每个工具都有描述与参数模式(self):
        for t in T.TOOLS.values():
            assert t.description.strip(), f"{t.name} 缺描述"
            assert t.parameters.get("type") == "object", f"{t.name} 参数模式非法"


# ---------------------------------------------------------------------------
# 工具 schema 导出
# ---------------------------------------------------------------------------
class TestSchemas:
    def test_openai_schema_形状(self):
        schemas = T.tool_schemas()
        assert len(schemas) == len(T.TOOLS)
        for s in schemas:
            assert s["type"] == "function"
            fn = s["function"]
            assert fn["name"] and fn["description"]
            assert fn["parameters"]["type"] == "object"

    def test_可按类型过滤(self):
        reads = T.tool_schemas("read")
        prepares = T.tool_schemas("prepare")
        assert len(reads) + len(prepares) == len(T.TOOLS)
        assert all("propose" not in s["function"]["name"] for s in reads)

    def test_工具目录可渲染进提示词(self):
        text = T.render_tool_catalog()
        assert "list_devices" in text
        assert "propose_config" in text
        assert "【只出待确认方案】" in text


# ---------------------------------------------------------------------------
# 调用纪律：任何错误都不得抛出打断 Agent 循环
# ---------------------------------------------------------------------------
class TestCallTool:
    def test_未知工具返回结构化失败(self):
        r = call_tool("no_such_tool", {}, _ctx())
        assert r["ok"] is False
        assert "未知工具" in r["error"]
        assert "list_devices" in r["error"]      # 报错里要给出可用工具

    def test_args_非对象被拒(self):
        r = call_tool("list_devices", ["x"], _ctx())
        assert r["ok"] is False

    def test_args_为空字典可接受(self):
        r = call_tool("list_devices", None, _ctx())
        assert r["ok"] is True
        assert len(r["devices"]) == 2

    def test_未知设备给出可读错误(self):
        r = call_tool("get_current_config", {"ip": "9.9.9.9"}, _ctx())
        assert r["ok"] is False
        assert "9.9.9.9" in r["error"]

    def test_禁连设备时_io_类工具被拒(self):
        for name, args in (("ping", {"ip": "127.0.0.1:2000", "target": "1.1.1.1"}),
                           ("collect_device", {"ip": "127.0.0.1:2000"}),
                           ("probe_capabilities", {"ip": "127.0.0.1:2000"})):
            r = call_tool(name, args, _ctx(allow_device_io=False))
            assert r["ok"] is False, f"{name} 不该在禁连设备时执行"
            assert "禁用" in r["error"]

    def test_超长结果被截断(self):
        ctx = _ctx(capability_results={
            "127.0.0.1:2000": {"raw_outputs": {
                "display current-configuration": "x" * 50000}}})
        r = call_tool("get_current_config", {"ip": "127.0.0.1:2000"}, ctx)
        assert r["ok"] is True
        assert len(r["config"]) <= T.CONFIG_EXCERPT


# ---------------------------------------------------------------------------
# 只读工具的实际行为（全部离线，不碰真机）
# ---------------------------------------------------------------------------
class TestReadTools:
    def test_list_devices_空设备表给出提示(self):
        r = call_tool("list_devices", {}, _ctx(devices=[]))
        assert r["ok"] is True
        assert r["devices"] == []
        assert "没有设备" in r["note"]

    def test_list_devices_回真实字段(self):
        r = call_tool("list_devices", {}, _ctx())
        names = [d["name"] for d in r["devices"]]
        assert names == ["R1", "R2"]
        assert r["devices"][0]["console_port"] == 2000

    def test_get_device_detail_无能力数据时不装懂(self):
        r = call_tool("get_device_detail", {"ip": "127.0.0.1:2000"}, _ctx())
        assert r["ok"] is True
        assert r["has_capability_data"] is False
        assert "probe_capabilities" in r["note"]

    def test_get_device_detail_有能力数据时渲染上下文(self):
        cap = {"ip": "127.0.0.1:2000", "hostname": "R1", "device_role": "router",
               "capabilities": {"ospf": {"supported": True, "configured": True}},
               "interfaces": [], "raw_outputs": {}, "errors": []}
        r = call_tool("get_device_detail", {"ip": "127.0.0.1:2000"},
                      _ctx(capability_results={"127.0.0.1:2000": cap}))
        assert r["has_capability_data"] is True
        assert "R1" in r["context"]

    def test_get_current_config_缺数据时解释原因(self):
        r = call_tool("get_current_config", {"ip": "127.0.0.1:2000"}, _ctx())
        assert r["ok"] is True
        assert r["config"] == ""
        assert "能力识别" in r["note"]

    def test_search_commands_空关键词报错(self):
        r = call_tool("search_commands", {"keyword": "  "}, _ctx())
        assert r["ok"] is False

    def test_search_commands_能检索到真实目录条目(self):
        r = call_tool("search_commands", {"keyword": "ospf"}, _ctx())
        assert r["ok"] is True
        assert r["count"] >= 1
        for m in r["matches"]:
            assert m["key"]

    def test_search_commands_无命中时返回空而不是报错(self):
        r = call_tool("search_commands", {"keyword": "zzz_no_such"}, _ctx())
        assert r["ok"] is True
        assert r["count"] == 0

    def test_trace_path_无采集数据时明确说明(self):
        r = call_tool("trace_path",
                      {"source_ip": "1.1.1.1", "dest_ip": "2.2.2.2"}, _ctx())
        assert r["ok"] is True
        assert r["success"] is False
        assert "采集" in r["note"]


# ---------------------------------------------------------------------------
# 意见书工具：必须产出 plan_id，且必须声明"尚未下发"
# ---------------------------------------------------------------------------
class TestPrepareTools:
    def test_propose_config_空指令报错(self):
        r = call_tool("propose_config", {"instruction": "  "}, _ctx())
        assert r["ok"] is False

    def test_propose_config_登记方案并回传_plan_id(self):
        captured = {}

        def fake_register(payload):
            captured.update(payload)
            return "PID_TEST"

        def fake_prepare(**kw):
            return {"success": True, "plan": {"kind": "config"},
                    "task": {"target_devices": ["127.0.0.1:2000"]},
                    "preview": {"127.0.0.1:2000": ["x"]},
                    "targets": ["127.0.0.1:2000"], "summary": {"targets": 1}}

        import services.ai_deploy_service as svc
        orig = svc.prepare
        svc.prepare = fake_prepare
        try:
            r = call_tool("propose_config", {"instruction": "起 OSPF"},
                          _ctx(register_proposal=fake_register))
        finally:
            svc.prepare = orig

        assert r["ok"] is True
        assert r["plan_id"] == "PID_TEST"
        assert r["stage"] == "prepared"
        assert r["targets"] == ["127.0.0.1:2000"]
        assert "尚未下发" in r["note"]
        assert captured["plan"] == {"kind": "config"}

    def test_propose_config_失败时不登记方案(self):
        called = {"n": 0}

        def fake_register(payload):
            called["n"] += 1
            return "X"

        def fake_prepare(**kw):
            return {"success": False, "error": "模型没输出 plan"}

        import services.ai_deploy_service as svc
        orig = svc.prepare
        svc.prepare = fake_prepare
        try:
            r = call_tool("propose_config", {"instruction": "x"},
                          _ctx(register_proposal=fake_register))
        finally:
            svc.prepare = orig

        assert r["ok"] is False
        assert called["n"] == 0, "生成失败不该登记方案"

    def test_propose_topology_空指令报错(self):
        r = call_tool("propose_topology", {"instruction": ""}, _ctx())
        assert r["ok"] is False

    def test_propose_topology_AI_未配置时明确失败(self):
        import services.ai_assistant as ai
        orig = ai.is_configured
        ai.is_configured = lambda cfg=None: False
        try:
            r = call_tool("propose_topology", {"instruction": "搭两台路由器"},
                          _ctx())
        finally:
            ai.is_configured = orig
        assert r["ok"] is False
        assert "未配置" in r["error"]
