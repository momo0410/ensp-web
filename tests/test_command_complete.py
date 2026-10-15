"""
命令上下文补全（services.command_complete）的离线单测。

覆盖：前缀过滤大小写不敏感、目录候选生成、views 过滤、
缓存未命中时的降级（monkeypatch get_cached_probe）。不连真机。
"""

from services import command_complete as cc


# ---------------------------------------------------------------------------
# 目录候选
# ---------------------------------------------------------------------------
def test_catalog_candidates_first_token():
    cands = cc.catalog_candidates("")
    texts = {c["text"] for c in cands}
    assert "interface" in texts
    assert "sysname" in texts
    assert "ospf" in texts
    for c in cands:
        assert c["source"] == "catalog"
        assert c["view"] == "system"
        assert isinstance(c["desc"], str) and c["desc"]


def test_catalog_prefix_filter_is_case_insensitive():
    upper = cc.catalog_candidates("SYS")
    lower = cc.catalog_candidates("sys")
    assert [c["text"] for c in upper] == [c["text"] for c in lower]
    assert "sysname" in {c["text"] for c in upper}


def test_catalog_device_type_filter():
    router = {c["text"] for c in cc.catalog_candidates("", device_type="router")}
    switch_only = cc.catalog_candidates("", device_type="switch")
    assert "ospf" in router
    assert all(c["text"] != "ospf" for c in switch_only)


def test_catalog_first_token_from_template():
    entry = next(v for v in cc.COMMAND_CATALOG.values())
    token = cc._first_token(entry)
    assert token == str(entry["command_template"][0]).split()[0]


# ---------------------------------------------------------------------------
# views 过滤
# ---------------------------------------------------------------------------
def test_filter_view_candidates_matches_prefix():
    views = {"display": ["display", "version", "vlan"], "system": ["ospf"]}
    out = cc.filter_view_candidates(views, "dis", view="display")
    assert [c["text"] for c in out] == ["display"]
    assert out[0]["source"] == "?"
    assert out[0]["view"] == "display"


def test_filter_view_candidates_case_insensitive():
    views = {"system": ["OSPF", "ospf"]}
    out = cc.filter_view_candidates(views, "os", view="system")
    assert [c["text"] for c in out] == ["OSPF", "ospf"]


def test_filter_view_all_merges_views():
    views = {"display": ["version"], "system": ["ospf"]}
    out = {c["text"] for c in cc.filter_view_candidates(views, "", view="all")}
    assert out == {"version", "ospf"}


def test_empty_prefix_truncates():
    views = {"display": [f"cmd{i}" for i in range(300)]}
    out = cc.filter_view_candidates(views, "", view="display")
    assert len(out) == cc.MAX_CANDIDATES


# ---------------------------------------------------------------------------
# complete_commands：缓存命中 / 降级
# ---------------------------------------------------------------------------
def test_complete_commands_cache_hit(monkeypatch):
    monkeypatch.setattr(cc, "get_cached_probe", lambda key: {
        "views": {"system": ["ospf", "interface"], "display": ["display"]},
        "device_type": "router",
    })
    res = cc.complete_commands({"ip": "1.1.1.1", "console_port": 2000}, "os")
    assert res["from_cache"] is True
    texts = [c["text"] for c in res["candidates"]]
    assert "ospf" in texts
    ospf = next(c for c in res["candidates"] if c["text"] == "ospf")
    assert ospf["source"] == "?"


def test_complete_commands_cache_miss_falls_back(monkeypatch):
    monkeypatch.setattr(cc, "get_cached_probe", lambda key: None)
    res = cc.complete_commands({"ip": "1.1.1.1"}, "sys")
    assert res["from_cache"] is False
    assert res["hint"]
    texts = [c["text"] for c in res["candidates"]]
    assert "sysname" in texts
    assert all(c["source"] in {"base", "catalog"} for c in res["candidates"])


def test_complete_commands_hit_merges_catalog(monkeypatch):
    monkeypatch.setattr(cc, "get_cached_probe", lambda key: {
        "views": {"system": ["ospf"]},
        "device_type": "router",
    })
    res = cc.complete_commands({"ip": "1.1.1.1"}, "")
    sources = {c["source"] for c in res["candidates"]}
    assert sources == {"?", "base", "catalog"}


# ---------------------------------------------------------------------------
# 基础清单（BASE_COMMANDS）与三层合并
# ---------------------------------------------------------------------------
def test_base_candidates_covers_display_and_user_view():
    out = cc.base_candidates("disp")
    texts = {c["text"] for c in out}
    assert "display version" in texts
    assert "display ip interface brief" in texts
    assert "display current-configuration" in texts
    for c in out:
        assert c["source"] == "base"
        assert c["desc"]
    user_texts = {c["text"] for c in out if c["view"] == "user"}
    assert "display version" in user_texts
    assert "display ip interface brief" in user_texts


def test_base_candidates_multiword_prefix():
    out = cc.base_candidates("display ip")
    texts = {c["text"] for c in out}
    assert "display ip interface brief" in texts
    assert "display ip routing-table" in texts
    assert all(t.startswith("display ip") for t in texts)


def test_base_candidates_case_insensitive():
    upper = {c["text"] for c in cc.base_candidates("DISPLAY VER")}
    lower = {c["text"] for c in cc.base_candidates("display ver")}
    assert upper == lower
    assert "display version" in upper


def test_complete_commands_cache_miss_display_nonempty(monkeypatch):
    monkeypatch.setattr(cc, "get_cached_probe", lambda key: None)
    res = cc.complete_commands({"ip": "1.1.1.1"}, "disp")
    assert res["from_cache"] is False
    texts = {c["text"] for c in res["candidates"]}
    assert "display version" in texts
    base = next(c for c in res["candidates"] if c["text"] == "display version")
    assert base["source"] == "base"


def test_complete_commands_base_ignores_view(monkeypatch):
    monkeypatch.setattr(cc, "get_cached_probe", lambda key: None)
    res = cc.complete_commands({"ip": "1.1.1.1"}, "display ip", view="system")
    texts = {c["text"] for c in res["candidates"]}
    assert "display ip interface brief" in texts


def test_complete_commands_cache_hit_measured_first_no_dup(monkeypatch):
    monkeypatch.setattr(cc, "get_cached_probe", lambda key: {
        "views": {"system": ["ospf", "display version"]},
        "device_type": "router",
    })
    res = cc.complete_commands({"ip": "1.1.1.1"}, "display", view="system")
    assert res["from_cache"] is True
    first = res["candidates"][0]
    assert first["text"] == "display version"
    assert first["source"] == "?"
    texts = [c["text"] for c in res["candidates"]]
    assert texts.count("display version") == 1

