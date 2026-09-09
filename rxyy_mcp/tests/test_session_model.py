# -*- coding: utf-8 -*-
"""tab 上要看得出自己是哪个模型 + 存量自动分工顶掉 agent 真名（08-24 两笔）。

模型：一屏十几个 agent，名字里没有任何模型痕迹，出问题时不知道该换谁。没有一条
通道会主动送来这个事实——MCP 是独立进程，hook payload 里 model 字段实测恒空——
只有 Cursor 自己的 `composerData.modelConfig` 有。

分工：`team_assign_auto` 标记是 08-07 才加的，此前落库的自动占位一条标记都没有，
`_assign_is_auto` 于是把它们全判成「用户手填」，agent 报了真名也顶不掉；面板上
永远显示用户第一句话的前 24 字。
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub  # noqa: E402
import session_core  # noqa: E402
import session_locator  # noqa: E402

UI_PATH = MODULE_DIR / "ui.html"


def _fake_cursor_db(tmp, entries):
    """造一个跟 Cursor 同构的 state.vscdb，返回可当 appdata 传的根目录。"""
    root = Path(tmp)
    gs = root / "Cursor" / "User" / "globalStorage"
    gs.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(gs / "state.vscdb"))
    con.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")
    for key, value in entries.items():
        con.execute("INSERT INTO cursorDiskKV VALUES (?,?)", (key, value))
    con.commit()
    con.close()
    return str(root)


def _composer(model_name, max_mode=False, effort=None, fast=None, context=None,
              think_id="effort"):
    cfg = {"modelName": model_name, "maxMode": max_mode}
    params = []
    if effort is not None:
        params.append({"id": think_id, "value": effort})
    if fast is not None:
        params.append({"id": "fast", "value": "true" if fast else "false"})
    if context is not None:
        params.append({"id": "context", "value": context})
    if params:
        cfg["selectedModels"] = [{"modelId": model_name, "parameters": params}]
    return json.dumps({"modelConfig": cfg})


class ModelLabelTests(unittest.TestCase):
    def test_vendor_prefix_is_dropped(self):
        # 同屏全是 claude-，前缀占掉一半宽度却零区分度
        self.assertEqual("opus-5", session_locator.model_label("claude-opus-5"))
        self.assertEqual("gpt-5.6", session_locator.model_label("openai/gpt-5.6"))

    def test_max_mode_is_appended(self):
        self.assertEqual("opus-5 max",
                         session_locator.model_label("claude-opus-5", True))

    def test_short_names_pass_through(self):
        self.assertEqual("composer-1", session_locator.model_label("composer-1"))

    def test_auto_slot_says_auto_not_default(self):
        # Cursor 选 Auto 时落的是字面量 default，直接显示会让人以为是某个模型
        self.assertEqual("Auto", session_locator.model_label("default"))

    def test_blank_stays_blank(self):
        self.assertEqual("", session_locator.model_label(None))


class PrettyModelLabelTests(unittest.TestCase):
    def test_uses_catalog_display_without_max_suffix(self):
        schema = {"display": "Claude Opus 5"}
        with patch.object(session_locator, "catalog_schema_for", return_value=schema):
            self.assertEqual(
                "Claude Opus 5",
                session_locator.pretty_model_label("claude-opus-5", True))

    def test_auto_and_blank(self):
        self.assertEqual("Auto", session_locator.pretty_model_label("default"))
        self.assertEqual("", session_locator.pretty_model_label(""))

    def test_fallback_short_name_skips_max_suffix(self):
        with patch.object(session_locator, "catalog_schema_for", return_value=None):
            self.assertEqual(
                "opus-5",
                session_locator.pretty_model_label("claude-opus-5", True))


class SidebarModelChipTests(unittest.TestCase):
    def test_display_plus_context_effort_fast(self):
        schema = {"display": "Cursor Grok 4.6"}
        info = {"model": "grok-4.6", "max": True, "effort": "xhigh",
                "fast": True, "context": ""}
        with patch.object(session_locator, "catalog_schema_for", return_value=schema):
            self.assertEqual(
                "Cursor Grok 4.6 Extra High Fast",
                session_locator.sidebar_model_chip(info))

    def test_fable_shows_1m_high_without_fast(self):
        schema = {"display": "Claude Fable 5.1"}
        info = {"model": "claude-fable-5-1", "effort": "high",
                "fast": False, "context": "1m"}
        with patch.object(session_locator, "catalog_schema_for", return_value=schema):
            self.assertEqual(
                "Claude Fable 5.1 1M High",
                session_locator.sidebar_model_chip(info))

    def test_max_mode_does_not_append_max_word(self):
        schema = {"display": "Claude Opus 5"}
        info = {"model": "claude-opus-5", "max": True, "effort": "high",
                "fast": False}
        with patch.object(session_locator, "catalog_schema_for", return_value=schema):
            self.assertEqual(
                "Claude Opus 5 High",
                session_locator.sidebar_model_chip(info))


class OpenModelNormalizeTests(unittest.TestCase):
    def test_string_max_suffix_and_auto(self):
        spec = session_locator.normalize_open_model("grok-4.6 max")
        self.assertEqual("grok-4.6", spec["model"])
        self.assertTrue(spec["max"])
        self.assertEqual("grok-4.6", spec["config"]["modelName"])
        self.assertTrue(spec["config"]["maxMode"])
        self.assertEqual("Auto", session_locator.normalize_open_model("auto")["label"])

    def test_dict_uses_preset_parameters(self):
        spec = session_locator.normalize_open_model(
            {"model": "claude-opus-5", "max": True})
        ids = [p["id"] for p in spec["parameters"]]
        self.assertIn("thinking", ids)
        self.assertIn("effort", ids)

    def test_presets_include_the_models_on_the_sidebar(self):
        keys = {s["key"] for s in session_locator.iter_model_presets()}
        self.assertIn("grok-4.6|max", keys)
        self.assertIn("claude-fable-5|max", keys)
        self.assertIn("default|std", keys)

    def test_effort_and_fast_override_the_preset(self):
        spec = session_locator.normalize_open_model({
            "model": "grok-4.6", "max": True, "effort": "high", "fast": False})
        params = {p["id"]: p["value"] for p in spec["parameters"]}
        self.assertEqual("high", params["effort"])
        self.assertEqual("false", params["fast"])
        self.assertEqual("high", spec["effort"])
        self.assertFalse(spec["fast"])
        self.assertEqual("High", spec["thinking"])
        cfg_params = spec["config"]["selectedModels"][0]["parameters"]
        self.assertEqual(params, {p["id"]: p["value"] for p in cfg_params})

    def test_override_keeps_claude_thinking_and_context(self):
        spec = session_locator.normalize_open_model({
            "model": "claude-opus-5", "max": True, "effort": "xhigh", "fast": True})
        ids = [p["id"] for p in spec["parameters"]]
        self.assertIn("thinking", ids)
        self.assertIn("context", ids)
        self.assertEqual("xhigh", spec["effort"])
        self.assertTrue(spec["fast"])
        self.assertEqual("Extra High Fast", spec["thinking"])

    def test_schema_strips_fast_and_keeps_reasoning(self):
        schema = {"think_id": "reasoning",
                  "think_values": [{"id": "xhigh", "label": "Extra High"}],
                  "has_fast": False}
        spec = session_locator.normalize_open_model({
            "model": "gpt-5.6-sol", "max": False,
            "effort": "xhigh", "fast": True,
        }, schema=schema)
        params = {p["id"]: p["value"] for p in spec["parameters"]}
        self.assertEqual("xhigh", params["reasoning"])
        self.assertNotIn("fast", params)
        self.assertNotIn("effort", params)

    def test_schema_writes_context_and_thinking(self):
        schema = {
            "think_id": "effort",
            "think_values": [{"id": "high", "label": "High"}],
            "has_fast": False,
            "params": [
                {"id": "effort", "kind": "enum",
                 "values": [{"id": "high"}]},
                {"id": "context", "kind": "enum",
                 "values": [{"id": "300k"}, {"id": "1m"}]},
                {"id": "thinking", "kind": "boolean",
                 "values": [{"id": "true"}]},
            ],
        }
        spec = session_locator.normalize_open_model({
            "model": "claude-opus-5", "max": True,
            "effort": "high", "fast": True,
            "context": "1m", "thinking": True,
        }, schema=schema)
        params = {p["id"]: p["value"] for p in spec["parameters"]}
        self.assertEqual("1m", params["context"])
        self.assertEqual("true", params["thinking"])
        self.assertEqual("high", params["effort"])
        self.assertNotIn("fast", params)

    def test_schema_drops_context_when_model_has_none(self):
        schema = {
            "think_id": "effort",
            "think_values": [{"id": "high"}],
            "has_fast": True,
            "params": [
                {"id": "effort", "kind": "enum", "values": [{"id": "high"}]},
                {"id": "fast", "kind": "boolean", "values": [{"id": "true"}]},
            ],
        }
        spec = session_locator.normalize_open_model({
            "model": "grok-4.6", "max": True,
            "parameters": [{"id": "context", "value": "1m"},
                           {"id": "effort", "value": "high"}],
            "effort": "high", "fast": False, "context": "1m",
        }, schema=schema)
        self.assertNotIn("context", {p["id"] for p in spec["parameters"]})


class ReadCursorModelTests(unittest.TestCase):
    def _read(self, entries, uid):
        with tempfile.TemporaryDirectory() as tmp:
            appdata = _fake_cursor_db(tmp, entries)
            return session_locator.read_cursor_model(uid, appdata=appdata)

    def test_reads_name_max_and_effort(self):
        info = self._read(
            {"composerData:u1": _composer("claude-opus-5", True, "max")}, "u1")
        self.assertEqual({
            "model": "claude-opus-5", "label": "opus-5 max",
            "max": True, "effort": "max", "fast": None, "context": "",
        }, info)

    def test_reads_fast_context_and_reasoning(self):
        info = self._read({
            "composerData:u1": _composer(
                "gpt-5.6-sol", False, "medium", fast=False, context="272k",
                think_id="reasoning"),
        }, "u1")
        self.assertEqual("medium", info["effort"])
        self.assertIs(info["fast"], False)
        self.assertEqual("272k", info["context"])

    def test_effort_of_other_models_is_not_borrowed(self):
        # selectedModels 里躺着历史选过的别的模型，别把它们的 effort 安到当前档上
        cfg = json.dumps({"modelConfig": {
            "modelName": "composer-1", "maxMode": False,
            "selectedModels": [
                {"modelId": "claude-opus-5",
                 "parameters": [{"id": "effort", "value": "max"}]},
                {"modelId": "composer-1",
                 "parameters": [{"id": "effort", "value": "low"}]}]}})
        self.assertEqual("low", self._read({"composerData:u1": cfg}, "u1")["effort"])

    def test_missing_conversation_returns_none(self):
        self.assertIsNone(self._read({"composerData:u1": _composer("x")}, "nope"))

    def test_corrupt_payload_returns_none_instead_of_raising(self):
        self.assertIsNone(self._read({"composerData:u1": "{not json"}, "u1"))

    def test_blank_uuid_never_touches_the_db(self):
        self.assertIsNone(session_locator.read_cursor_model(""))

    def test_missing_db_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(session_locator.read_cursor_model("u1", appdata=tmp))

    def test_0902_a_model_switch_still_sitting_in_the_wal_is_seen(self):
        """事故 0902：rxyy 新开对话，档位从选择器上继承的 grok-4.6 切成 claude-fable-5-1，
        控制台模型牌却挂着 grok-4.6 一分多钟（截图里五个待命壳全是 grok-4.6）。
        Cursor 的库是 WAL 模式：切档那笔先进 -wal，要等 checkpoint 才落主文件；
        旧读法 immutable=1 只看主文件，读到的永远是上一次 checkpoint 的旧档。
        这里让切档停在 WAL 里（写连接不关、关掉自动 checkpoint），读到的必须是新档。
        """
        with tempfile.TemporaryDirectory() as tmp:
            appdata = _fake_cursor_db(tmp, {"composerData:u1": _composer("grok-4.6")})
            db = Path(appdata) / "Cursor" / "User" / "globalStorage" / "state.vscdb"
            writer = sqlite3.connect(str(db))
            try:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute("UPDATE cursorDiskKV SET value=? WHERE key=?",
                               (_composer("claude-fable-5-1", True, "max"), "composerData:u1"))
                writer.commit()
                wal = db.with_name("state.vscdb-wal")
                self.assertGreater(wal.stat().st_size, 0, "切档没停在 WAL 里，测试前提不成立")
                info = session_locator.read_cursor_model("u1", appdata=appdata)
            finally:
                writer.close()
        self.assertEqual("claude-fable-5-1", info["model"], "还在读 checkpoint 前的旧档")
        self.assertEqual("fable-5-1 max", info["label"])

    def test_ro_open_failure_falls_back_to_the_immutable_snapshot(self):
        # Cursor 写入间隙偶发 database is locked：宁可读到略旧的快照，也别让牌空掉
        real_connect = sqlite3.connect

        def flaky(dsn, *a, **k):
            if "mode=ro" in dsn:
                raise sqlite3.OperationalError("database is locked")
            return real_connect(dsn, *a, **k)

        with tempfile.TemporaryDirectory() as tmp:
            appdata = _fake_cursor_db(tmp, {"composerData:u1": _composer("claude-opus-5")})
            with patch.object(session_locator.sqlite3, "connect", flaky):
                info = session_locator.read_cursor_model("u1", appdata=appdata)
        self.assertEqual("claude-opus-5", info["model"])


class ReadCodexModelTests(unittest.TestCase):
    def test_reads_top_level_model_and_effort(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.toml"
            p.write_text('model = "gpt-5.6-sol"\n'
                         'model_reasoning_effort = "xhigh"\n'
                         '\n[agents.worker]\n'
                         'model = "gpt-4.1"\n', encoding="utf-8")
            info = session_locator.read_codex_model(config_path=p)
        self.assertEqual("gpt-5.6-sol", info["model"])
        self.assertEqual("gpt-5.6-sol", info["label"])
        self.assertEqual("xhigh", info["effort"])

    def test_section_model_is_not_the_tab_chip(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "config.toml"
            p.write_text('[agents.worker]\nmodel = "gpt-4.1"\n', encoding="utf-8")
            self.assertIsNone(session_locator.read_codex_model(config_path=p))

    def test_missing_file_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(session_locator.read_codex_model(
                config_path=Path(tmp) / "nope.toml"))

    def test_from_name_is_none_when_blank(self):
        self.assertIsNone(session_locator.model_info_from_name(""))
        self.assertIsNone(session_locator.model_info_from_name(None))


def _sess(uid="u1"):
    s = hub.Session.__new__(hub.Session)
    s.cursor_uuid = uid
    s.model_info = None
    s.model_probe_at = 0.0
    s.model_probe_uid = uid  # 「牌就是从这个对话读的」——换绑测试再显式改 uuid
    s.reported_model = ""
    return s


class ModelProbeTickTests(unittest.TestCase):
    def test_probe_caches_label_on_the_session(self):
        s = _sess()
        info = {"model": "claude-opus-5", "label": "opus-5 max",
                "max": True, "effort": "max"}
        with patch.object(hub, "read_cursor_model", return_value=info):
            session_core.tick_model_probe(hub.HUB, s, 100.0)
        self.assertEqual("opus-5 max", s.model_info["label"])

    def test_probe_is_throttled(self):
        s = _sess()
        calls = []

        def counted(uid):
            calls.append(uid)
            return {"model": "m", "label": "m", "max": False, "effort": ""}

        with patch.object(hub, "read_cursor_model", counted):
            session_core.tick_model_probe(hub.HUB, s, 100.0)
            session_core.tick_model_probe(hub.HUB, s, 100.0 + hub.HUB.MODEL_PROBE_EVERY - 1)
            session_core.tick_model_probe(hub.HUB, s, 100.0 + hub.HUB.MODEL_PROBE_EVERY + 1)
        self.assertEqual(2, len(calls))

    def test_failed_read_keeps_the_last_known_model(self):
        # Cursor 独占写库时这一拍会读空；抹掉的话面板上的模型牌会一闪一闪
        s = _sess()
        s.model_info = {"model": "claude-opus-5", "label": "opus-5", "max": False,
                        "effort": ""}
        with patch.object(hub, "read_cursor_model", return_value=None):
            session_core.tick_model_probe(hub.HUB, s, 1e9)
        self.assertEqual("opus-5", s.model_info["label"])

    def test_probe_survives_a_raising_reader(self):
        s = _sess()
        with patch.object(hub, "read_cursor_model", side_effect=sqlite3.DatabaseError("x")):
            session_core.tick_model_probe(hub.HUB, s, 1e9)  # 不该把状态循环带崩
        self.assertIsNone(s.model_info)

    def test_non_cursor_agents_do_not_hit_the_cursor_db(self):
        s = _sess(uid=None)
        with patch.object(hub, "read_cursor_model", side_effect=AssertionError("不该查")), \
             patch.object(hub, "read_codex_model", return_value=None):
            session_core.tick_model_probe(hub.HUB, s, 1e9)
        self.assertIsNone(s.model_info)

    def test_codex_tab_uses_config_default_when_no_cursor_uuid(self):
        s = _sess(uid=None)
        info = {"model": "gpt-5.6-sol", "label": "gpt-5.6-sol",
                "max": False, "effort": "xhigh"}
        with patch.object(hub, "read_cursor_model", side_effect=AssertionError("不该查")), \
             patch.object(hub, "read_codex_model", return_value=info):
            session_core.tick_model_probe(hub.HUB, s, 1e9)
        self.assertEqual("gpt-5.6-sol", s.model_info["label"])
        self.assertEqual("xhigh", s.model_info["effort"])

    def test_reported_model_beats_codex_default(self):
        s = _sess(uid=None)
        s.reported_model = "gpt-5.6-terra"
        with patch.object(hub, "read_codex_model",
                          side_effect=AssertionError("不该读默认")):
            session_core.tick_model_probe(hub.HUB, s, 1e9)
        self.assertEqual("gpt-5.6-terra", s.model_info["label"])

    def test_snapshot_does_not_carry_a_stale_model(self):
        # 模型是此刻的实况，重启后重新拍一次即可，别把上次的结论当现在的
        s = session_core.session_from_snapshot(
            hub.HUB, {"id": "s1", "name": "x", "cwd": str(MODULE_DIR),
                      "model_info": {"label": "opus-4"}, "model_probe_at": 123.0})
        self.assertIsNone(s.model_info)
        self.assertEqual(0.0, s.model_probe_at)
        self.assertIsNone(s.model_probe_uid)

    def test_snapshot_keeps_reported_model_for_codex(self):
        s = session_core.session_from_snapshot(
            hub.HUB, {"id": "s1", "name": "x", "cwd": str(MODULE_DIR),
                      "reported_model": "gpt-5.6-sol"})
        self.assertEqual("gpt-5.6-sol", s.reported_model)
        self.assertIsNone(s.model_info)


class ModelChipRebindTests(unittest.TestCase):
    """08-25 用户实测：fable-5 新对话接手 opus-5 的会话，面板模型牌迟迟不换。

    根因：cursor_uuid 换绑（接手重定位/身份自校准/认领被收走）时模型牌缓存
    与 8 秒限频戳原样留着——旧牌属于前任对话，还要再挂最多一个限频周期。
    契约：换绑当拍立即改读新对话的牌；uuid 被收走的 tab 牌跟着消失，不猜。
    """

    OPUS = {"model": "claude-opus-5", "label": "opus-5 max",
            "max": True, "effort": "max"}
    FABLE = {"model": "claude-fable-5", "label": "fable-5 max",
             "max": True, "effort": "max"}

    def _worn(self):
        # 一个刚读过牌的会话：opus 牌新鲜、限频戳还压着整整一个周期
        s = _sess(uid="u-old")
        s.model_info = dict(self.OPUS)
        s.model_probe_at = 100.0
        return s

    def test_rebind_reads_the_new_dialog_this_tick(self):
        # 接手落地：uuid 换成新对话，下一拍就该挂 fable，不等限频周期
        s = self._worn()
        s.cursor_uuid = "u-new"
        reads = []

        def reader(uid):
            reads.append(uid)
            return dict(self.FABLE)

        with patch.object(hub, "read_cursor_model", reader):
            session_core.tick_model_probe(hub.HUB, s, 101.0)
        self.assertEqual(["u-new"], reads)
        self.assertEqual("fable-5 max", s.model_info["label"])

    def test_rebind_drops_the_old_chip_even_if_the_read_fails(self):
        # 换绑那拍库正被 Cursor 独占：宁可空牌等下一拍，也不挂前任的模型
        s = self._worn()
        s.cursor_uuid = "u-new"
        with patch.object(hub, "read_cursor_model", return_value=None):
            session_core.tick_model_probe(hub.HUB, s, 101.0)
        self.assertIsNone(s.model_info)

    def test_dispossessed_tab_loses_the_chip(self):
        # 认领被收走（cursor_uuid=None）：旧对话的牌必须掉。没有自报、也没有
        # Codex 默认档时保持空白，别把前任的 opus 继续挂着。
        s = self._worn()
        s.cursor_uuid = None
        with patch.object(hub, "read_cursor_model",
                          side_effect=AssertionError("没对话不该查")), \
             patch.object(hub, "read_codex_model", return_value=None):
            session_core.tick_model_probe(hub.HUB, s, 101.0)
        self.assertIsNone(s.model_info)

    def test_same_dialog_keeps_the_throttle(self):
        # 没换绑就维持限频一拍：别把限频修没了
        s = self._worn()
        with patch.object(hub, "read_cursor_model",
                          side_effect=AssertionError("限频期内不该查")):
            session_core.tick_model_probe(
                hub.HUB, s, 100.0 + hub.HUB.MODEL_PROBE_EVERY - 1)
        self.assertEqual("opus-5 max", s.model_info["label"])


class LegacyAutoAssignTests(unittest.TestCase):
    """存量自动分工没有 team_assign_auto 标记，按形状认出来才顶得掉。"""

    def test_the_actual_stuck_label_is_recognised(self):
        # 就是 b4eff2ee 面板上顶了半天的那串（派活第一句截 24 字 + 省略号）
        stuck = "你先看下目前当前项目最新的开发进度、剩余任务。（…"
        self.assertEqual(hub.Api.AUTO_ASSIGN_LEN + 1, len(stuck))
        self.assertTrue(hub.Api._looks_auto_cut(stuck))

    def test_auto_cut_shape_matches_what_one_line_produces(self):
        raw = "把飞鸽跨机收发和任务安排站的收发都补完，工单页也要显示附件图片"
        self.assertTrue(hub.Api._looks_auto_cut(
            hub.Api()._one_line(raw, hub.Api.AUTO_ASSIGN_LEN)))

    def test_hand_typed_assign_is_not_mistaken_for_auto(self):
        for typed in ("飞鸽跨机收发", "工单对接", "", "短的…",
                      "用户自己敲的一串刚好也很长的分工说明文字排布"):
            self.assertFalse(hub.Api._looks_auto_cut(typed), typed)

    def test_explicit_flag_still_wins_regardless_of_shape(self):
        s = hub.Session.__new__(hub.Session)
        s.conv_key = "cafe1234"
        cfg = {"team_assign": {"cafe1234": "飞鸽跨机收发"},
               "team_assign_auto": {"cafe1234": True}}
        with patch.object(hub.HUB, "cfg", cfg):
            self.assertTrue(hub.Api()._assign_is_auto(s))

    def test_legacy_entry_without_flag_is_auto_by_shape(self):
        stuck = "你先看下目前当前项目最新的开发进度、剩余任务。（…"
        s = hub.Session.__new__(hub.Session)
        s.conv_key = "b4eff2ee"
        with patch.object(hub.HUB, "cfg", {"team_assign": {"b4eff2ee": stuck}}):
            self.assertTrue(hub.Api()._assign_is_auto(s))

    def test_legacy_hand_typed_entry_without_flag_is_kept(self):
        s = hub.Session.__new__(hub.Session)
        s.conv_key = "b4eff2ee"
        with patch.object(hub.HUB, "cfg", {"team_assign": {"b4eff2ee": "团队功能整改"}}):
            self.assertFalse(hub.Api()._assign_is_auto(s))


class ModelInUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = UI_PATH.read_text(encoding="utf-8")

    def test_sidebar_tab_renders_the_model_chip(self):
        self.assertIn('m.className = "mchip"', self.html)
        self.assertIn(".tab .mchip, .tm-model {", self.html)

    def test_open_session_header_renders_the_model_chip(self):
        head = self.html[self.html.index("function renderSessHeader"):]
        head = head[:head.index("\nfunction ")]
        self.assertIn("s.model_full", head)

    def test_team_card_renders_the_model_chip(self):
        card = self.html[self.html.index("function agentCard"):]
        card = card[:card.index("\n  function ", 10)]
        self.assertIn('class="tm-model"', card)

    def test_tabs_repaint_when_the_model_changes(self):
        sig = self.html[self.html.index("function tabsSignature"):]
        sig = sig[:sig.index("\nfunction ")]
        self.assertIn("s.model", sig)

    def test_chip_title_promises_seconds_not_a_minute(self):
        self.assertIn("几秒内跟上", self.html)
        self.assertNotIn("一分钟内跟上", self.html)
        self.assertLessEqual(hub.HUB.MODEL_PROBE_EVERY, 10.0)


class SidebarBajieUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = UI_PATH.read_text(encoding="utf-8")

    def test_session_item_is_two_line_like_bajie(self):
        for a in ('className = "s-content"', 'className = "spreview"',
                  'className = "stchip "', "function tabPreviewText",
                  "function tabRelTime", 'id="sideSearchQ"', "你: "):
            self.assertIn(a, self.html, a)

    def test_search_empty_copy_matches_bajie(self):
        self.assertIn("无匹配会话", self.html)
        self.assertIn("暂无会话", self.html)


class SidebarPreviewTests(unittest.TestCase):
    def test_last_human_line_wins_and_strips_tags(self):
        api = hub.Api()
        s = SimpleNamespace(messages=[
            {"role": "sys", "html": "忽略"},
            {"role": "user", "html": "<p>你好世界</p>"},
            {"role": "ai", "html": "<p>收到了</p>"},
        ])
        role, text = api._sidebar_last_chat(s)
        self.assertEqual("agent", role)
        self.assertEqual("收到了", text)

    def test_relay_from_another_agent_is_not_your_line(self):
        api = hub.Api()
        s = SimpleNamespace(messages=[
            {"role": "user", "html": "真问题"},
            {"role": "user", "html": "【agent 转告", "who": "agent·foo"},
        ])
        role, text = api._sidebar_last_chat(s)
        self.assertEqual("user", role)
        self.assertEqual("真问题", text)


if __name__ == "__main__":
    unittest.main()
