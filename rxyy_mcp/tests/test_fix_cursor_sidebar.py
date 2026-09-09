# -*- coding: utf-8 -*-
"""设置面板「Cursor 侧栏名字·把跑偏的改回来」。

写盘赢不了内存，所以侧栏一直顶着「Persistent plus …」。这个按钮改走 Cursor
自己的 composer.renameChat，当场改内存。它会抢键盘，所以只认控制台有把握的
那批名字，闸门跟 _push_name_to_cursor 一模一样。
"""
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hub  # noqa: E402


def _s(sid, uid, name, **kw):
    d = dict(id=sid, cursor_uuid=uid, name=name, cwd=r"D:\Desktop\cursor工作流",
             agent_named=True, name_locked=False)
    d.update(kw)
    return SimpleNamespace(**d)


class DriftTests(unittest.TestCase):
    def setUp(self):
        self.shown = {}
        ps = [patch.object(hub.HUB, "lock", threading.Lock()),
              patch.object(hub, "read_cursor_title",
                           lambda uid: self.shown.get(uid))]
        for p in ps:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in ps])

    def drift(self, sessions):
        with patch.object(hub.HUB, "sessions", {s.id: s for s in sessions}):
            return hub.Api().cursor_sidebar_drift()

    def test_a_tab_stuck_on_the_auto_title_is_drift(self):
        self.shown["u1"] = "Persistent plus zhi report"
        rows = self.drift([_s("s1", "u1", "控制台·侧栏改名")])
        self.assertEqual(1, len(rows))
        self.assertEqual({"shown": "Persistent plus zhi report",
                          "want": "控制台·侧栏改名"},
                         {k: rows[0][k] for k in ("shown", "want")})
        self.assertEqual("u1", rows[0]["composer_id"])

    def test_a_tab_already_showing_the_right_name_is_not_drift(self):
        self.shown["u1"] = "控制台·侧栏改名"
        self.assertEqual([], self.drift([_s("s1", "u1", "控制台·侧栏改名")]))

    def test_a_name_the_console_is_not_sure_about_is_left_alone(self):
        # agent 没自报过、用户也没锁过 = 控制台自己也只是猜的，别推上去
        self.shown["u1"] = "Persistent plus zhi report"
        self.assertEqual([], self.drift(
            [_s("s1", "u1", "控制台·侧栏改名", agent_named=False)]))

    def test_a_locked_name_counts_even_without_the_agent_saying_so(self):
        self.shown["u1"] = "Persistent plus zhi report"
        rows = self.drift([_s("s1", "u1", "机构管理端",
                              agent_named=False, name_locked=True)])
        self.assertEqual(1, len(rows))

    def test_a_standby_shell_name_is_never_pushed(self):
        # 08-25 事故铁律：别把干活 tab 降级成待命
        self.shown["u1"] = "Persistent plus zhi report"
        self.assertEqual([], self.drift([_s("s1", "u1", "待命·cursor工作流")]))

    def test_a_session_with_no_composer_yet_is_skipped(self):
        self.assertEqual([], self.drift([_s("s1", "", "控制台·侧栏改名")]))

    def test_a_tab_cursor_never_wrote_to_disk_is_skipped(self):
        # 读不到就是不知道，不知道就别抢键盘
        self.assertEqual([], self.drift([_s("s1", "u1", "控制台·侧栏改名")]))

    def test_a_db_read_that_blows_up_does_not_sink_the_whole_scan(self):
        def flaky(uid):
            if uid == "u1":
                raise RuntimeError("db locked")
            return "Persistent plus zhi report"
        with patch.object(hub, "read_cursor_title", flaky):
            with patch.object(hub.HUB, "sessions", {}):
                pass
            rows = self.drift([_s("s1", "u1", "甲"), _s("s2", "u2", "乙")])
        self.assertEqual(["u2"], [r["composer_id"] for r in rows])

    def test_names_are_clipped_the_same_40_chars_as_everywhere_else(self):
        self.shown["u1"] = "Persistent plus zhi report"
        rows = self.drift([_s("s1", "u1", "名" * 60)])
        self.assertEqual("名" * 40, rows[0]["want"])


class FixTests(unittest.TestCase):
    def setUp(self):
        self.shown = {"u1": "Persistent plus zhi report",
                      "u2": "Persistent plus task report"}
        self.sessions = [_s("s1", "u1", "控制台·侧栏改名"),
                         _s("s2", "u2", "B站412修复")]
        self.calls = []
        ps = [patch.object(hub.HUB, "lock", threading.Lock()),
              patch.object(hub.HUB, "sessions",
                           {s.id: s for s in self.sessions}),
              patch.object(hub, "log_event", lambda *a, **k: None),
              patch.object(hub, "read_cursor_title",
                           lambda uid: self.shown.get(uid))]
        for p in ps:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in ps])

    def fix(self, dry_run=False, done=None, module=True):
        fake = SimpleNamespace(rename_chats_live=lambda pairs, **kw: (
            self.calls.append((pairs, kw)) or (done if done is not None
                                               else {c for c, _ in pairs})))
        with patch.object(hub, "cursor_live_rename", fake if module else None):
            return hub.Api().fix_cursor_sidebar(dry_run)

    def test_dry_run_shows_the_list_and_touches_nothing(self):
        r = self.fix(dry_run=True)
        self.assertTrue(r["ok"])
        self.assertEqual(2, len(r["drift"]))
        self.assertEqual([], r["renamed"])
        self.assertEqual([], self.calls)

    def test_it_renames_every_drifted_tab(self):
        r = self.fix()
        self.assertTrue(r["ok"])
        self.assertEqual(2, len(r["renamed"]))
        pairs, kw = self.calls[0]
        self.assertEqual([("u1", "控制台·侧栏改名"), ("u2", "B站412修复")], pairs)

    def test_the_workspace_hint_is_the_folder_name_not_the_full_path(self):
        self.fix()
        self.assertEqual("cursor工作流", self.calls[0][1]["workspace_hint"])

    def test_a_partial_batch_reports_what_is_left(self):
        r = self.fix(done={"u1"})
        self.assertEqual(["u1"], [x["composer_id"] for x in r["renamed"]])
        self.assertEqual(1, r["left"])

    def test_nothing_drifted_means_no_keyboard_grab(self):
        self.shown = {"u1": "控制台·侧栏改名", "u2": "B站412修复"}
        r = self.fix()
        self.assertTrue(r["ok"])
        self.assertEqual([], r["drift"])
        self.assertEqual([], self.calls)

    def test_a_machine_without_the_module_says_so_instead_of_crashing(self):
        r = self.fix(module=False)
        self.assertFalse(r["ok"])
        self.assertIn("cursor_live_rename", r["error"])
        self.assertEqual(2, len(r["drift"]))


if __name__ == "__main__":
    unittest.main()
