# -*- coding: utf-8 -*-
"""给正开着的 Cursor 侧栏改名——不重启 Cursor、不 Reload Window。

08-28 用户：「侧栏，你想想办法，你要是现在给侧栏改名要怎么做？不能重启cursor」。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cursor_live_rename as live  # noqa: E402


class ParseKeybindingsTests(unittest.TestCase):
    def test_cursor_own_file_starts_with_a_comment(self):
        # Cursor 生成的 keybindings.json 头一行就是 //，直接 json.loads 会炸
        text = '// 将键绑定放在此文件中以覆盖默认值\n[{"key":"ctrl+alt+s","command":"x"}]'
        self.assertEqual([{"key": "ctrl+alt+s", "command": "x"}],
                         live.parse_keybindings(text))

    def test_block_comments_and_trailing_commas(self):
        text = '[\n /* 注释 */ {"key":"a","command":"b"},\n]'
        self.assertEqual([{"key": "a", "command": "b"}],
                         live.parse_keybindings(text))

    def test_empty_file_is_an_empty_list(self):
        self.assertEqual([], live.parse_keybindings(""))
        self.assertEqual([], live.parse_keybindings("   \n"))

    def test_garbage_reads_as_empty_rather_than_raising(self):
        # 解析失败只影响「我们往里加什么」，原文由调用方原样还原，不会丢用户绑定
        self.assertEqual([], live.parse_keybindings("{{{not json"))

    def test_a_json_object_is_not_a_binding_list(self):
        self.assertEqual([], live.parse_keybindings('{"key":"a"}'))


class BindingShapeTests(unittest.TestCase):
    """这条绑定的形状是从 workbench.desktop.main.js 里读出来的，不是猜的。

        function IDb(e,t){ if(typeof t=="string") return t; ... }
        ... super({id:exi /* composer.renameChat */, ..., f1:!1})

    composerId 作为字符串参数直接传给命令；f1:!1 意味着命令面板里搜不到它，
    但快捷键照样绑得上——整个方案就架在这两条上。
    """

    def test_composer_id_rides_as_the_command_argument(self):
        b = live.binding_for("abc-123", "ctrl+alt+shift+f9")
        self.assertEqual("composer.renameChat", b["command"])
        self.assertEqual("abc-123", b["args"])
        self.assertEqual("ctrl+alt+shift+f9", b["key"])

    def test_our_bindings_are_marked_so_we_can_find_them_again(self):
        self.assertTrue(live.binding_for("abc", live.CHORD)[live._MARK])

    def test_no_when_clause(self):
        # 这条只活几百毫秒，且要在任何焦点下都按得动
        self.assertNotIn("when", live.binding_for("abc", live.CHORD))


class TempBindingTests(unittest.TestCase):
    USER = ('// 用户自己的\n[{"key":"ctrl+alt+s",'
            '"command":"workbench.action.toggleUnifiedSidebarFromKeyboard"}]')

    def test_user_bindings_survive(self):
        text, _ = live.with_temp_bindings(self.USER, ["c1"])
        got = json.loads(text)
        self.assertEqual("ctrl+alt+s", got[0]["key"])
        self.assertEqual("workbench.action.toggleUnifiedSidebarFromKeyboard",
                         got[0]["command"])

    def test_each_tab_gets_its_own_chord(self):
        text, pairs = live.with_temp_bindings(self.USER, ["c1", "c2", "c3"])
        chords = [c for _, c in pairs]
        self.assertEqual(3, len(set(chords)))
        self.assertEqual(["c1", "c2", "c3"], [cid for cid, _ in pairs])
        keys = [b["key"] for b in json.loads(text) if b.get(live._MARK)]
        self.assertEqual(chords, keys)

    def test_a_leftover_binding_from_last_time_is_swept_out(self):
        stale, _ = live.with_temp_bindings(self.USER, ["old"])
        text, _ = live.with_temp_bindings(stale, ["new"])
        ours = [b for b in json.loads(text) if b.get(live._MARK)]
        self.assertEqual(1, len(ours))
        self.assertEqual("new", ours[0]["args"])

    def test_more_tabs_than_chords_are_left_for_the_next_round(self):
        many = ["c{}".format(i) for i in range(live.BATCH_MAX + 5)]
        _, pairs = live.with_temp_bindings(self.USER, many)
        self.assertEqual(live.BATCH_MAX, len(pairs))

    def test_output_is_plain_json_cursor_can_reload(self):
        text, _ = live.with_temp_bindings("", ["c1"])
        self.assertTrue(text.endswith("\n"))
        json.loads(text)

    def test_chinese_titles_are_not_escaped_into_ascii(self):
        # 名字不进 keybindings.json，但 composerId 之外的中文（用户自己的绑定
        # 里可能有 when/comment）不该被写成 \uXXXX，否则用户回头看是一坨乱码
        text, _ = live.with_temp_bindings('[{"key":"a","command":"改名"}]', ["c1"])
        self.assertIn("改名", text)


class ChordTests(unittest.TestCase):
    def test_only_the_ctrl_alt_shift_f_family_is_pressable(self):
        for bad in ["ctrl+k", "alt+shift+f9", "ctrl+alt+shift+q",
                    "ctrl+alt+shift+f13"]:
            with self.assertRaises(ValueError):
                live.press_chord(bad)

    def test_every_chord_we_hand_out_is_pressable(self):
        _, pairs = live.with_temp_bindings("", ["c{}".format(i)
                                                for i in range(live.BATCH_MAX)])
        sent = []
        with patch.object(live, "_send", lambda seq: sent.append(seq)):
            for _, chord in pairs:
                live.press_chord(chord)
        self.assertEqual(live.BATCH_MAX, len(sent))


class Utf16Tests(unittest.TestCase):
    def test_ascii_is_one_unit_each(self):
        self.assertEqual([65, 66], live.utf16_units("AB"))

    def test_cjk_is_still_one_unit_each(self):
        self.assertEqual(2, len(live.utf16_units("待命")))

    def test_astral_chars_split_into_a_surrogate_pair(self):
        # KEYEVENTF_UNICODE 一次只吃一个码元
        self.assertEqual(2, len(live.utf16_units("😀")))


class PickWindowTests(unittest.TestCase):
    WINS = [(1, "Customize - smart_cloud - Cursor"),
            (2, "hub.py - cursor工作流 - Cursor")]

    def test_the_workspace_hint_wins(self):
        self.assertEqual(2, live.pick_window(self.WINS, "cursor工作流")[0])

    def test_no_hint_falls_back_to_the_first_window(self):
        self.assertEqual(1, live.pick_window(self.WINS, "")[0])

    def test_a_hint_that_matches_nothing_still_picks_something(self):
        self.assertEqual(1, live.pick_window(self.WINS, "不存在的目录")[0])

    def test_no_windows_at_all(self):
        self.assertEqual((None, ""), live.pick_window([], "x"))


class DriverGuardTests(unittest.TestCase):
    """驱动本身。真按键盘那几下全 patch 掉，这里只验闸门和还原。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.kb = live.keybindings_path(self.tmp.name)
        self.kb.parent.mkdir(parents=True, exist_ok=True)
        self.original = '// mine\n[{"key":"ctrl+alt+s","command":"x"}]'
        self.kb.write_text(self.original, encoding="utf-8")
        self.typed = []
        ps = [patch.object(live, "cursor_windows", lambda: [(1, "x - Cursor")]),
              patch.object(live, "focus_window", lambda h: True),
              patch.object(live, "foreground_is_cursor", lambda: True),
              patch.object(live, "press_focus_explorer", lambda: None),
              patch.object(live, "press_chord", lambda c=None: None),
              patch.object(live, "press_select_all", lambda: None),
              patch.object(live, "press_enter", lambda: None),
              patch.object(live, "type_text", self.typed.append),
              patch.object(live, "KEYBIND_RELOAD_WAIT", 0),
              patch.object(live, "INPUT_OPEN_WAIT", 0),
              patch.object(live, "FOCUS_SETTLE", 0),
              patch.object(live.time, "sleep", lambda *_: None),
              patch.object(live.os, "name", "nt")]
        for p in ps:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in ps])

    def run_it(self, renames):
        return live.rename_chats_live(renames, appdata=self.tmp.name)

    def test_it_types_each_new_name(self):
        done = self.run_it([("c1", "控制台·侧栏改名"), ("c2", "B站412修复")])
        self.assertEqual({"c1", "c2"}, done)
        self.assertEqual(["控制台·侧栏改名", "B站412修复"], self.typed)

    def test_keybindings_json_is_put_back_exactly(self):
        self.run_it([("c1", "名字")])
        self.assertEqual(self.original, self.kb.read_text(encoding="utf-8"))

    def test_keybindings_json_is_put_back_even_when_typing_blows_up(self):
        def boom(_):
            raise RuntimeError("nope")
        with patch.object(live, "type_text", boom):
            self.assertEqual(set(), self.run_it([("c1", "名字")]))
        self.assertEqual(self.original, self.kb.read_text(encoding="utf-8"))

    def test_a_file_that_did_not_exist_is_removed_again(self):
        self.kb.unlink()
        self.run_it([("c1", "名字")])
        self.assertFalse(self.kb.is_file())

    def test_nothing_to_do_touches_nothing(self):
        self.assertEqual(set(), self.run_it([]))
        self.assertEqual(set(), self.run_it([("c1", "   ")]))
        self.assertEqual(set(), self.run_it([("", "名字")]))
        self.assertEqual(self.original, self.kb.read_text(encoding="utf-8"))

    def test_names_are_clipped_to_the_same_40_chars_as_the_console(self):
        self.run_it([("c1", "名" * 60)])
        self.assertEqual("名" * 40, self.typed[0])

    def test_it_bails_when_the_foreground_window_is_not_cursor(self):
        # 别人的窗口在前台时敲 ctrl+a／打字／回车 = 往别人那儿乱敲
        with patch.object(live, "foreground_is_cursor", lambda: False):
            self.assertEqual(set(), self.run_it([("c1", "名字")]))
        self.assertEqual([], self.typed)

    def test_it_stops_midway_if_focus_escapes(self):
        seen = {"n": 0}

        def flaky():
            # 1=进门检查 2=c1 开头 3=c1 敲完前 4=c2 开头（这一下跑了）
            seen["n"] += 1
            return seen["n"] < 4
        with patch.object(live, "foreground_is_cursor", flaky):
            done = self.run_it([("c1", "一"), ("c2", "二"), ("c3", "三")])
        self.assertEqual({"c1"}, done)
        self.assertEqual(["一"], self.typed)

    def test_no_cursor_window_means_no_keystrokes_at_all(self):
        with patch.object(live, "cursor_windows", lambda: []):
            self.assertEqual(set(), self.run_it([("c1", "名字")]))
        self.assertEqual([], self.typed)
        self.assertEqual(self.original, self.kb.read_text(encoding="utf-8"))

    def test_an_elevated_cursor_we_cannot_raise_is_still_fine_if_it_is_up(self):
        """Cursor 以管理员身份跑、hub 不是：Windows 不让 hub 把它提到前台。

        10:50 现场就卡在这儿。但用户本来就是在 Cursor 里点的按钮（控制台 UI
        是 Cursor 里的 iframe），窗口已经在前台，按键送得进去；而且
        composer.renameChat 收 composerId、不挑窗口。所以提不上来不算失败，
        「前台压根不是 Cursor」才算。
        """
        with patch.object(live, "focus_window", lambda h: False):
            self.assertEqual({"c1"}, self.run_it([("c1", "名字")]))
        self.assertEqual(["名字"], self.typed)

    def test_cannot_raise_it_and_it_is_not_up_either_means_hands_off(self):
        with (patch.object(live, "focus_window", lambda h: False),
              patch.object(live, "foreground_is_cursor", lambda: False)):
            self.assertEqual(set(), self.run_it([("c1", "名字")]))
        self.assertEqual([], self.typed)

    def test_only_windows(self):
        with patch.object(live.os, "name", "posix"):
            self.assertEqual(set(), self.run_it([("c1", "名字")]))
        self.assertEqual([], self.typed)

    def test_an_over_long_batch_is_trimmed_not_dropped(self):
        many = [("c{}".format(i), "名{}".format(i))
                for i in range(live.BATCH_MAX + 3)]
        done = self.run_it(many)
        self.assertEqual(live.BATCH_MAX, len(done))

    def test_single_rename_helper_reports_per_tab(self):
        with patch.object(live, "rename_chats_live", lambda r, **k: {"c1"}):
            self.assertTrue(live.rename_chat_live("c1", "x"))
            self.assertFalse(live.rename_chat_live("c2", "x"))


if __name__ == "__main__":
    unittest.main()
