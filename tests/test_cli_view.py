"""domain/console_connection.py 的视图识别与守卫逻辑（离线单测）。

背景：VRP 的 `return` 会一路退回**用户视图**，不会停在系统视图；
而在用户视图下配置命令被**静默忽略**（不报错也不执行）。
domain 层必须自己识别"现在在哪一层"，否则空转的命令会被当成成功。
"""
import pytest

from domain.console_connection import CliView, parse_view, parse_prompt


class TestParseView:
    @pytest.mark.parametrize("prompt,kind,context", [
        ("<Huawei>", "user", ""),
        ("<R1>", "user", ""),
        ("[Huawei]", "system", ""),
        ("[R1]", "system", ""),
        ("[Huawei-GigabitEthernet0/0/1]", "interface", "GigabitEthernet0/0/1"),
        ("[Huawei-GE0/0/0]", "interface", "GE0/0/0"),
        ("[Huawei-Ethernet0/0/1]", "interface", "Ethernet0/0/1"),
        ("[Huawei-Serial0/0/0]", "interface", "Serial0/0/0"),
        ("[Huawei-Vlanif10]", "interface", "Vlanif10"),
        ("[Huawei-ospf-1]", "protocol", "ospf-1"),
        ("[Huawei-vlan10]", "vlan", "vlan10"),
        ("[AC6005-GigabitEthernet0/0/1]", "interface", "GigabitEthernet0/0/1"),
    ])
    def test_视图识别(self, prompt, kind, context):
        view = parse_view(prompt)
        assert view is not None
        assert view.kind == kind
        assert view.context == context

    def test_用户视图不可下发配置(self):
        assert parse_view("<Huawei>").is_config is False

    def test_系统及以下视图均可下发配置(self):
        for prompt in ("[Huawei]", "[Huawei-ospf-1]", "[Huawei-GigabitEthernet0/0/1]"):
            assert parse_view(prompt).is_config is True

    def test_多行输出取最后一个提示符(self):
        raw = "some output\n[Huawei]\nmore\n[Huawei-GigabitEthernet0/0/1]\n"
        assert parse_view(raw).kind == "interface"

    def test_空输入返回_None(self):
        assert parse_view("") is None
        assert parse_view(None) is None

    def test_没有提示符返回_None(self):
        assert parse_view("just some text without prompt") is None

    def test_设备名带连字符也能正确分层(self):
        """[USG-5500] 这种名字里的连字符不能干扰视图判断。"""
        view = parse_view("[USG-5500]")
        assert view.kind == "protocol"  # 无法识别成接口时按协议视图兜底


class TestLegacyParsePromptStillWorks:
    def test_仍能取到名字(self):
        assert parse_prompt("[Huawei-GigabitEthernet0/0/1]") == "Huawei-GigabitEthernet0/0/1"
        assert parse_prompt("<Huawei>") == "Huawei"

    def test_空输入(self):
        assert parse_prompt("") == ""


class TestCliView:
    def test_冻结数据类(self):
        view = CliView(name="R1", kind="system", is_config=True, context="")
        with pytest.raises(Exception):
            view.kind = "user"
