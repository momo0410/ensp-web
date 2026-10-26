"""services/free_config_renderer.py 的离线单测。

这是下发链路上"清洗 → 危险命令拦截 → 变量渲染 → 生成预览"的最后一环，
任何一处放松都会让危险命令或未定义变量漏到设备上。
"""

from services.free_config_renderer import (
    DANGEROUS_COMMANDS,
    check_dangerous_commands,
    clean_commands,
    extract_variables,
    merge_variables,
    render_commands,
    render_preview,
    validate_task,
)


class TestCleanCommands:
    def test_过滤空行_注释_save_return_system_view(self):
        cleaned, warnings, skipped = clean_commands(
            ["", "   ", "# 这是注释", "save", "return", "system-view", "sys", "vlan 10"])
        assert cleaned == ["vlan 10"]
        assert len(warnings) == 4          # save / return / system-view / sys
        assert len(skipped) == 7           # 2 空行 + 注释 + 上述 4 条

    def test_保留正常命令与缩进内容(self):
        cleaned, _, _ = clean_commands(["ospf 1", " area 0", "  network 10.0.0.0 0.0.0.255"])
        assert cleaned == ["ospf 1", "area 0", "network 10.0.0.0 0.0.0.255"]

    def test_大小写不敏感(self):
        cleaned, warnings, _ = clean_commands(["SAVE", "System-View", "Return"])
        assert cleaned == []
        assert len(warnings) == 3


class TestDangerousCommands:
    def test_命中危险关键字即判不安全(self):
        safe, found = check_dangerous_commands(["reboot"])
        assert safe is False
        assert found[0]["command"] == "reboot"
        assert "reboot" in found[0]["reason"]

    def test_每个危险关键字都能命中(self):
        for keyword in DANGEROUS_COMMANDS:
            safe, found = check_dangerous_commands([f"  {keyword.upper()}  "])
            assert safe is False, f"{keyword} 应被拦截"
            assert found, f"{keyword} 应给出原因"

    def test_force_放行但仍报告(self):
        safe, found = check_dangerous_commands(["reboot"], force=True)
        assert safe is True
        assert found

    def test_安全命令(self):
        assert check_dangerous_commands(["vlan 10", "display version"]) == (True, [])


class TestVariables:
    def test_提取变量去重排序(self):
        assert extract_variables(
            ["ip address {{ip}} {{mask}}", "sysname {{name}}", "{{ip}}"]) == ["ip", "mask", "name"]

    def test_设备变量覆盖全局变量(self):
        merged = merge_variables({"ip": "1.1.1.1", "mask": "24"}, {"ip": "2.2.2.2"})
        assert merged == {"ip": "2.2.2.2", "mask": "24"}

    def test_渲染替换(self):
        rendered, undefined = render_commands(
            ["ip address {{ip}} {{mask}}"], {"ip": "10.0.0.1", "mask": "255.255.255.0"}, {})
        assert rendered == ["ip address 10.0.0.1 255.255.255.0"]
        assert undefined == []

    def test_未定义变量保留原样并报告(self):
        rendered, undefined = render_commands(["sysname {{name}}"], {}, {})
        assert rendered == ["sysname {{name}}"]
        assert undefined == ["name"]


class TestRenderPreview:
    def _task(self, **kw):
        task = {
            "command_template": ["sysname {{name}}", "vlan {{vlan}}"],
            "global_vars": {"vlan": "10"},
            "device_vars": {"127.0.0.1:2000": {"name": "SW1"}},
            "target_devices": ["127.0.0.1:2000"],
        }
        task.update(kw)
        return task

    def test_逐台渲染并使用设备名(self):
        result = render_preview(self._task())
        assert result["success"] is True
        assert result["errors"] == []
        preview = result["preview"]["127.0.0.1:2000"]
        assert preview["device_name"] == "SW1"
        assert preview["commands"] == ["sysname SW1", "vlan 10"]
        assert result["variables_used"] == ["name", "vlan"]

    def test_字符串模板也能拆行(self):
        result = render_preview(self._task(command_template="sysname {{name}}\nvlan {{vlan}}"))
        assert result["preview"]["127.0.0.1:2000"]["commands"] == ["sysname SW1", "vlan 10"]

    def test_危险命令阻止生成预览(self):
        result = render_preview(self._task(command_template=["reboot"]))
        assert result["success"] is False
        assert "危险命令" in result["errors"][0]
        assert result["preview"] == {}

    def test_模板全被过滤判失败(self):
        result = render_preview(self._task(command_template=["# 只有注释", ""]))
        assert result["success"] is False
        assert "命令模板为空或全部被过滤" in result["errors"]

    def test_缺变量判失败且不产出该设备预览(self):
        task = self._task(device_vars={})
        result = render_preview(task)
        assert result["success"] is False
        assert "未定义变量" in result["errors"][0]
        assert result["preview"] == {}

    def test_无目标设备不炸(self):
        result = render_preview(self._task(target_devices=[]))
        assert result["success"] is True
        assert result["preview"] == {}


class TestValidateTask:
    def test_缺设备或模板都报错(self):
        ok, errors = validate_task({})
        assert ok is False
        assert len(errors) == 2

    def test_完整任务通过(self):
        ok, errors = validate_task(
            {"target_devices": ["127.0.0.1:2000"], "command_template": ["vlan 10"]})
        assert ok is True
        assert errors == []
