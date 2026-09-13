"""
test_protocol_discovery.py - 协议发现（矩阵 / ? 命令树 / 通道修复）单元测试
==========================================================================
不依赖真实设备：矩阵查表、帮助输出解析、ANSI 清理、证据合并逻辑全部纯函数测试。
"""

import sys
from pathlib import Path


import pytest

from services.capability_matrix import (
    lookup_matrix, matrix_support, matrix_note, parse_version_info, PROTOCOL_NAMES,
)
from services.help_tree import (
    parse_help_keywords, detect_protocols, find_first_physical_interface,
    compose_discovery,
)
from services.device_connector import strip_ansi
from services import capability_probe


# ---------------------------------------------------------------------------
# 测试数据：来自真实 eNSP 镜像输出（tests/inventory/port_2000.txt 为 S5700 实采）
# ---------------------------------------------------------------------------
S5700_VERSION = """display version
Huawei Versatile Routing Platform Software
VRP (R) software, Version 5.110 (S5700 V200R001C00)
Copyright (c) 2000-2011 HUAWEI TECH CO., LTD

Quidway S5700-28C-HI Routing Switch uptime is 0 week, 0 day, 0 hour, 18 minutes
<Huawei>"""

AR_VERSION = """Huawei Versatile Routing Platform Software
VRP (R) software, Version 5.130 (AR2220 V200R003C00)
Copyright (c) 2000-2011 HUAWEI TECH CO., LTD
Huawei AR2220 Router uptime is 0 week, 0 day, 1 hour, 3 minutes
<AR1>"""

UNKNOWN_VERSION = """Some Weird Device
VRP (R) software, Version 9.99 (FOOBAR V999R999C999)
<Foo>"""

DISPLAY_HELP = """display ?
  aaa                             AAA
  acl                             Display ACL information
  bgp                             Display BGP information
  ip                              <group>
  ospf                            Display OSPF information
  vlan                            Display VLAN information
  <cr>"""

SYSTEM_HELP = """[Huawei]?
aaa                                          AAA authentication mode
bgp                                          Enable BGP
interface                                    Specify interface configuration view
ospf                                         Open Shortest Path First
sysname                                      Specify the host name
vlan                                         Virtual LAN
<cr>"""

INTERFACE_HELP = """[Huawei-GigabitEthernet0/0/1]?
description                                  Configure the interface description
dhcp                                         DHCP
nat                                          Network Address Translation
vrrp                                         VRRP configuration
<cr>"""


# ---------------------------------------------------------------------------
# 方案A：能力矩阵
# ---------------------------------------------------------------------------
class TestCapabilityMatrix:
    def test_parse_s5700(self):
        info = parse_version_info(S5700_VERSION)
        assert info["model"] == "S5700"
        assert info["vrp_version"] == "5.110"
        assert info["image"] == "S5700 V200R001C00"

    def test_parse_ar(self):
        info = parse_version_info(AR_VERSION)
        assert info["model"] == "AR2220"
        assert info["image"] == "AR2220 V200R003C00"

    def test_lookup_s5700(self):
        matrix = lookup_matrix(S5700_VERSION)
        assert matrix["family"] == "s5700"
        assert matrix["confidence"] == "high"
        p = matrix["protocols"]
        assert p["vlan"]["supported"] is True
        assert p["stp"]["supported"] is True
        assert p["nat"]["supported"] is False
        assert p["mpls"]["supported"] is False
        assert p["ospf"]["supported"] is True
        assert p["isis"]["supported"] is None  # 无把握 → 实探

    def test_lookup_ar(self):
        matrix = lookup_matrix(AR_VERSION)
        assert matrix["family"] == "ar_router"
        p = matrix["protocols"]
        assert p["nat"]["supported"] is True
        assert p["mpls"]["supported"] is True
        assert p["bgp"]["supported"] is True
        assert p["vlan"]["supported"] is None  # 交换卡型号不定 → 实探
        assert p["stp"]["supported"] is False

    def test_lookup_unknown(self):
        matrix = lookup_matrix(UNKNOWN_VERSION)
        assert matrix["family"] is None
        assert matrix["confidence"] == "low"
        assert all(v["supported"] is None for v in matrix["protocols"].values())

    def test_matrix_support_helper(self):
        matrix = lookup_matrix(S5700_VERSION)
        assert matrix_support(matrix, "vlan") is True
        assert matrix_support(matrix, "nat") is False
        assert matrix_support(matrix, "isis") is None
        assert matrix_note(matrix, "nat") != ""

    def test_registry_has_groups_covered(self):
        """PROTOCOL_GROUPS 引用的协议 key 必须都在注册表里（防手误）。"""
        all_keys = set(PROTOCOL_NAMES)
        for keys in __import__("services.capability_matrix", fromlist=["PROTOCOL_GROUPS"]).PROTOCOL_GROUPS.values():
            assert set(keys) <= all_keys


# ---------------------------------------------------------------------------
# 方案B：? 命令树解析与签名
# ---------------------------------------------------------------------------
class TestHelpTree:
    def test_parse_display_help(self):
        kws = parse_help_keywords(DISPLAY_HELP)
        assert {"aaa", "acl", "bgp", "ip", "ospf", "vlan"} <= kws
        assert "<group>" not in kws

    def test_parse_interface_help(self):
        kws = parse_help_keywords(INTERFACE_HELP)
        assert {"dhcp", "nat", "vrrp", "description"} <= kws

    def test_detect_protocols_views(self):
        views = {
            "display": parse_help_keywords(DISPLAY_HELP),
            "system": parse_help_keywords(SYSTEM_HELP),
            "interface": parse_help_keywords(INTERFACE_HELP),
        }
        detected = detect_protocols(views)
        assert detected["ospf"] == "display:ospf"
        assert detected["vrrp"] == "interface:vrrp"
        assert detected["nat"] == "interface:nat"
        assert detected["vlan"] == "display:vlan"
        assert "isis" not in detected  # 视图里没有 isis

    def test_detect_protocols_empty(self):
        assert detect_protocols({}) == {}

    def test_find_first_physical_interface(self):
        brief = """Interface                   PHY   Protocol InUti OutUti
NULL0                       up    up(s)      0%     0%
GigabitEthernet0/0/1        down  down        0%     0%
GigabitEthernet0/0/2        down  down        0%     0%"""
        assert find_first_physical_interface(brief) == "GigabitEthernet0/0/1"

    def test_compose_discovery(self):
        views = {
            "display": parse_help_keywords(DISPLAY_HELP),
            "interface": parse_help_keywords(INTERFACE_HELP),
        }
        result = compose_discovery(S5700_VERSION, views)
        assert result["model"] == "S5700"
        assert result["matrix"]["family"] == "s5700"
        assert result["protocols"]["ospf"]["supported"] is True
        assert "?树" in result["protocols"]["ospf"]["evidence"]
        # 矩阵判定不支持的 nat：? 树接口视图里实测到了 → 实测优先
        assert result["protocols"]["nat"]["supported"] is True
        # 矩阵没把握的 isis：视图也没有 → None
        assert result["protocols"]["isis"]["supported"] is None


# ---------------------------------------------------------------------------
# 方案C：通道修复
# ---------------------------------------------------------------------------
class TestChannelFixes:
    def test_strip_ansi_esc(self):
        assert strip_ansi("\x1b[42Dhello\x1b[0K world") == "hello world"

    def test_strip_ansi_residue(self):
        # console 通道实测：ESC 字节丢失后残留 "[42D"，回显被吃成 isplay vlan
        raw = "[42D                                          [42D\r\n<Huawei>isplay vlan"
        cleaned = strip_ansi(raw)
        assert "[42D" not in cleaned
        assert "<Huawei>" in cleaned

    def test_strip_ansi_backspace(self):
        assert strip_ansi("ab\x08c") == "abc"


# ---------------------------------------------------------------------------
# 证据合并：矩阵覆盖 / 候选命令 / ? 树升级
# ---------------------------------------------------------------------------
class TestEvidenceMerge:
    def _s5700_matrix(self):
        return lookup_matrix(S5700_VERSION)

    def test_matrix_override_probe_false_negative(self):
        """镜像确定支持，但探测命令不被识别 → 不得误判为不支持。"""
        cap = {"supported": False, "configured": False, "running": False,
               "error": "Error: Unrecognized command", "evidence": "probe"}
        capability_probe._apply_matrix_override("ospf", cap, self._s5700_matrix())
        assert cap["supported"] is True
        assert cap["error"] is None
        assert cap["evidence"] == "matrix"

    def test_matrix_override_probe_false_positive(self):
        """矩阵确定不支持，探测却误报支持 → 以矩阵为准。"""
        cap = {"supported": True, "configured": False, "running": True,
               "error": None, "evidence": "probe"}
        capability_probe._apply_matrix_override("nat", cap, self._s5700_matrix())
        assert cap["supported"] is False
        assert cap["running"] is False
        assert cap["evidence"] == "matrix"

    def test_matrix_none_keeps_probe(self):
        """矩阵无把握（isis=None）→ 探测结果保留。"""
        cap = {"supported": True, "configured": True, "running": False,
               "error": None, "evidence": "probe"}
        capability_probe._apply_matrix_override("isis", cap, self._s5700_matrix())
        assert cap["supported"] is True
        assert cap["evidence"] == "probe"

    def test_alt_command_rescue(self):
        """主命令不支持时，候选命令成功 → 恢复支持。"""
        cap = {"supported": False, "error": "Unrecognized", "evidence": "probe",
               "display_cmd": "display nat session"}
        results = {"display nat statistics": {"success": True, "output": "NAT OK", "error": None}}
        capability_probe._try_alt_commands("nat", cap, lambda c: results.get(c))
        assert cap["supported"] is True
        assert cap["display_cmd"] == "display nat statistics"
        assert "alt" in cap["evidence"]

    def test_alt_command_all_fail(self):
        cap = {"supported": False, "error": "Unrecognized", "evidence": "probe"}
        capability_probe._try_alt_commands("nat", cap, lambda c: None)
        assert cap["supported"] is False

    def test_tree_detection_upgrades(self):
        """? 树实测到 → 支持且只升不降。"""
        result = {"capabilities": {
            "vlan": {"supported": False, "configured": False, "running": False,
                     "error": "Unrecognized", "evidence": "probe"},
            "ospf": {"supported": True, "configured": True, "running": False,
                     "error": None, "evidence": "probe"},
        }}
        views = {"display": {"vlan", "ospf", "acl"}}
        capability_probe._apply_tree_detection(result, views)
        assert result["capabilities"]["vlan"]["supported"] is True
        assert "helptree" in result["capabilities"]["vlan"]["evidence"]
        assert result["capabilities"]["ospf"]["evidence"].endswith("+helptree")
        # 树里有、但结果里还没有的协议 → 新增条目
        assert result["capabilities"]["acl"]["supported"] is True

    def test_tree_detection_never_downgrades(self):
        """矩阵判定不支持、树里也没看到 → 保持不支持（不因树采集失败翻转）。"""
        result = {"capabilities": {
            "nat": {"supported": False, "configured": False, "running": False,
                    "error": None, "evidence": "matrix"},
        }}
        capability_probe._apply_tree_detection(result, {"display": {"ospf"}})
        assert result["capabilities"]["nat"]["supported"] is False


# ---------------------------------------------------------------------------
# 缓存路径集成：probe_capabilities_from_cache（纯 dict，零 SSH）
# ---------------------------------------------------------------------------
class TestFromCacheIntegration:
    def _collection_result(self):
        return {
            "ip": "192.168.56.12",
            "name": "S5700",
            "ssh_ok": True,
            "commands": {
                "display version": {"success": True, "output": S5700_VERSION},
                "display current-configuration": {"success": True, "output": (
                    "sysname S5700\n"
                    "vlan batch 10 20\n"
                    "stp mode mstp\n"
                    "interface Vlanif10\n"
                    " ip address 192.168.56.12 255.255.255.0\n"
                )},
                "display ip interface brief": {"success": True, "output": (
                    "Interface                         IP Address/Mask      Physical   Protocol\n"
                    "Vlanif10                          192.168.56.12/24     up         up\n"
                    "GigabitEthernet0/0/1              unassigned           down       down\n"
                )},
                "display ospf peer": {"success": True, "output": ""},
            },
        }

    def test_matrix_settles_unsupported_without_ssh(self):
        result = capability_probe.probe_capabilities_from_cache(self._collection_result())
        caps = result["capabilities"]
        # NAT：矩阵判定交换机不支持 → 直接 False，且无需补跑
        assert caps["nat"]["supported"] is False
        assert caps["nat"]["evidence"] == "matrix"
        assert "nat" not in result["unknown_capabilities"]
        # MPLS：矩阵 False
        assert caps["mpls"]["supported"] is False
        # OSPF：镜像支持（HI），探测输出为空 → 缓存路径假定支持
        assert caps["ospf"]["supported"] is True
        # VLAN：矩阵 True + current-config 命中关键字 → 已配置
        assert caps["vlan"]["configured"] is True
        assert caps["vlan"]["supported"] is True
        # 接口列表正常解析
        assert any(i["name"] == "GigabitEthernet0/0/1" for i in result["interfaces"])
        assert result["matrix"]["family"] == "s5700"

    def test_unknown_only_contains_matrix_unsettled(self):
        """无输出且矩阵没把握的协议才进入 unknown（等待补跑）。"""
        result = capability_probe.probe_capabilities_from_cache(self._collection_result())
        unknown = set(result["unknown_capabilities"])
        # 矩阵 True/False 的协议不允许出现在 unknown
        matrix = lookup_matrix(S5700_VERSION)
        for key in unknown:
            assert matrix_support(matrix, key) is None, f"{key} 不应在 unknown 中"


# ---------------------------------------------------------------------------
# command_probe：多视图 trigger 校验
# ---------------------------------------------------------------------------
class TestCommandProbeViews:
    def test_trigger_matches_interface_view(self, monkeypatch):
        """trigger 只在接口视图 ? 里出现的目录条目不再漏判。"""
        from services import command_probe

        fake_views = {
            "display": parse_help_keywords(DISPLAY_HELP),
            "system": parse_help_keywords(SYSTEM_HELP),
            "interface": parse_help_keywords(INTERFACE_HELP),
        }

        def fake_probe_via_ssh(target, timeout=6, read_timeout=6):
            return S5700_VERSION, fake_views

        monkeypatch.setattr(command_probe, "probe_via_ssh", fake_probe_via_ssh)
        result = command_probe.probe_device_commands({"mode": "ssh", "ip": "1.2.3.4"}, force=True)

        assert result["device_type"] == "l3_switch"
        assert result["keyword_count"] > 0
        # vrrp 条目的 trigger=["interface"]，interface/system 视图并集中必命中
        assert "vrrp" in result["supported"]
        assert result["views"]["interface"]  # 接口视图关键字已带回

    def test_no_keywords_marks_all_supported(self, monkeypatch):
        """抓不到 ? 输出（连接半残）时不做关键字否决，退化为按型号过滤。"""
        from services import command_probe

        monkeypatch.setattr(command_probe, "probe_via_ssh",
                            lambda target, timeout=6, read_timeout=6: ("", {}))
        result = command_probe.probe_device_commands({"mode": "ssh", "ip": "1.2.3.4"}, force=True)
        assert result["keyword_count"] == 0
        assert "vrrp" in result["supported"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
