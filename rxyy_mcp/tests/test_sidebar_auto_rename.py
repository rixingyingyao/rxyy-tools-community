# -*- coding: utf-8 -*-
"""侧栏名字自动跟随：控制台趁用户空闲自己按 composer.renameChat，不再催 agent。

0902 rxyy 截图：一排 tab 顶着自动标题，agent 收到「并行调 cursor-app-control.rename_chat」
只能回一句「本环境没有」——这台 Cursor 3.17 的工具表里根本没那个工具（IDE 主对话也没有）。
能改内存的只剩 cursor_live_rename 那条抢键盘的路，它以前只做成手动按钮。现在挂进标题
同步循环的末尾，但按键只在「用户空闲 ≥ N 秒 且 Cursor 在前台」时发，同一个名字 10 分钟
只按一次；开着它就不再往 agent 队列里塞催单。
"""
import os
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub  # noqa: E402
import cursor_live_rename as live  # noqa: E402

WS = r"d:\Desktop\cursor工作流"
AUTO = "Persistent plus zhi report"
UID = "c6c4d53a-1111-2222-3333-444455556666"


def _s(name="控制台·侧栏改名", uid=UID, stale=AUTO):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = "s1", "f9c313ed", name
    s.cwd = s.task_root = WS
    s.agent_named = True
    s.name_locked = False
    s.cursor_uuid = uid
    s.cursor_title = None
    s.transcript_path = None
    s.created_ts = None
    s.connected = True
    s.pending = None
    s.queued = []
    s.messages = []
    s.msg_seq = 9
    s.rev = 0
    s.recon_deadline = None
    s.ide_active_cache = False
    s.sidebar_stale = stale
    s.lock = threading.Lock()
    return s


class _FakeDriver:
    """替身 cursor_live_rename：空闲秒数、前台、按键结果都可控。"""

    def __init__(self, idle=120.0, in_front=True, done=None):
        self.idle, self.in_front, self.done = idle, in_front, done
        self.calls = []

    def user_idle_secs(self):
        return self.idle

    def foreground_is_cursor(self):
        return self.in_front

    def rename_chats_live(self, pairs, **kw):
        self.calls.append((list(pairs), kw))
        return self.done if self.done is not None else {c for c, _ in pairs}


class AutoFixTickTests(unittest.TestCase):
    def setUp(self):
        self.s = _s()
        self.driver = _FakeDriver()
        self.logs = []
        ps = [patch.object(hub.HUB, "lock", threading.Lock()),
              patch.object(hub.HUB, "sessions", {self.s.id: self.s}),
              patch.object(hub.HUB, "cfg", {"max_messages": 200}),
              patch.object(hub, "log_event", lambda msg, *a, **k: self.logs.append(msg)),
              patch.object(hub, "cursor_live_rename", self.driver)]
        for p in ps:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in ps])
        hub.HUB._sidebar_tried = {}
        self.addCleanup(lambda: setattr(hub.HUB, "_sidebar_tried", {}))

    def tick(self):
        return hub.HUB._auto_fix_sidebar()

    def test_a_clobbered_tab_is_renamed_when_the_user_is_idle_and_cursor_is_in_front(self):
        done = self.tick()
        self.assertEqual({UID}, done)
        pairs, kw = self.driver.calls[0]
        self.assertEqual([(UID, "控制台·侧栏改名")], pairs)
        self.assertEqual("cursor工作流", kw["workspace_hint"])
        self.assertEqual("", self.s.sidebar_stale, "按下去了就清掉标记，下一拍重判")
        self.assertTrue(any("侧栏自动改名" in m for m in self.logs))

    def test_no_keypress_while_the_user_is_typing(self):
        # 按键会敲进用户正打的字里，手在键盘上就等下一拍
        self.driver.idle = 3.0
        self.assertEqual(set(), self.tick())
        self.assertEqual([], self.driver.calls)
        self.assertEqual(AUTO, self.s.sidebar_stale, "没按就别清标记")

    def test_idle_threshold_comes_from_config(self):
        hub.HUB.cfg["sidebar_auto_rename_idle_secs"] = 300
        self.driver.idle = 120.0
        self.assertEqual(set(), self.tick())
        hub.HUB.cfg["sidebar_auto_rename_idle_secs"] = 60
        self.assertEqual({UID}, self.tick())

    def test_no_keypress_when_cursor_is_not_the_foreground_window(self):
        # 按键落到别的程序里就是事故
        self.driver.in_front = False
        self.assertEqual(set(), self.tick())
        self.assertEqual([], self.driver.calls)

    def test_unknown_idle_means_hands_off(self):
        self.driver.idle = None
        self.assertEqual(set(), self.tick())

    def test_the_switch_off_means_hands_off(self):
        hub.HUB.cfg["sidebar_auto_rename"] = False
        self.assertEqual(set(), self.tick())
        self.assertEqual([], self.driver.calls)

    def test_a_machine_without_the_driver_does_nothing(self):
        with patch.object(hub, "cursor_live_rename", None):
            self.assertEqual(set(), self.tick())

    def test_the_same_name_is_pressed_at_most_once_per_retry_window(self):
        # 按下去 ≠ Cursor 认了；但每拍都按 = 每 30 秒抢一次键盘
        self.tick()
        self.s.sidebar_stale = AUTO  # 下一拍库里还是旧名（renameComposer 异步、或真没成）
        self.assertEqual(set(), self.tick())
        self.assertEqual(1, len(self.driver.calls))
        hub.HUB._sidebar_tried[(UID, "控制台·侧栏改名")] -= hub.Hub.SIDEBAR_RETRY_SECS + 1
        self.assertEqual({UID}, self.tick())
        self.assertEqual(2, len(self.driver.calls))

    def test_a_renamed_tab_is_a_new_key_for_the_retry_window(self):
        self.tick()
        self.s.name = "控制台·接入提速"
        self.s.sidebar_stale = AUTO
        self.assertEqual({UID}, self.tick())

    def test_a_standby_shell_is_never_pushed(self):
        # 壳的 uuid 是猜的，改到别人干活的对话上就是事故（08-25 铁律）
        self.s.name = "待命·cursor工作流·f9c3"
        self.assertEqual(set(), self.tick())
        self.assertEqual([], self.driver.calls)

    def test_a_tab_that_was_not_clobbered_is_left_alone(self):
        self.s.sidebar_stale = ""
        self.assertEqual(set(), self.tick())
        self.assertEqual([], self.driver.calls)

    def test_a_partial_batch_only_clears_what_was_pressed(self):
        other = _s(name="B站412修复", uid="u2")
        other.id = "s2"
        hub.HUB.sessions["s2"] = other
        self.driver.done = {UID}
        self.assertEqual({UID}, self.tick())
        self.assertEqual("", self.s.sidebar_stale)
        self.assertEqual(AUTO, other.sidebar_stale)

    def test_a_driver_that_blows_up_does_not_sink_the_title_sync_loop(self):
        self.driver.rename_chats_live = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        with patch.object(hub.HUB, "_relocate_if_stale", lambda *a, **k: None), \
             patch.object(hub, "locate_cursor_session", lambda *a, **k: None):
            hub.HUB.sync_cursor_titles()  # 不抛
        self.assertTrue(any("侧栏自动改名异常" in m for m in self.logs))


class PushMarksStaleTests(unittest.TestCase):
    """信号从哪来：_push_name_to_cursor 写之前那一读发现「又被盖回自动标题」。"""

    def setUp(self):
        self.s = _s(stale="")
        self.driver = _FakeDriver()
        self.nudged = []
        ps = [patch.object(hub.HUB, "lock", threading.Lock()),
              patch.object(hub.HUB, "sessions", {self.s.id: self.s}),
              patch.object(hub.HUB, "cfg", {"max_messages": 200}),
              patch.object(hub, "log_event", lambda *a, **k: None),
              patch.object(hub, "write_cursor_title", lambda *a, **k: True),
              patch.object(hub, "cursor_live_rename", self.driver),
              patch.object(hub.Api, "_nudge_cursor_rename",
                           lambda api, s, stale="": self.nudged.append(stale))]
        for p in ps:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in ps])
        hub.HUB._sidebar_tried = {}
        self.addCleanup(lambda: setattr(hub.HUB, "_sidebar_tried", {}))

    def _push(self, title_in_db):
        with patch.object(hub, "read_cursor_title", lambda *a, **k: title_in_db):
            hub.HUB._push_name_to_cursor(self.s)

    def test_a_write_that_got_clobbered_marks_the_session_and_the_tick_presses(self):
        self._push(AUTO)
        self.assertEqual("", self.s.sidebar_stale, "第一次写还谈不上被盖")
        self._push(AUTO)
        self.assertEqual(AUTO, self.s.sidebar_stale)
        self.assertEqual({UID}, hub.HUB._auto_fix_sidebar())

    def test_a_write_that_stuck_clears_the_mark(self):
        self._push(AUTO)
        self._push(AUTO)
        self._push(self.s.name)
        self.assertEqual("", self.s.sidebar_stale)

    def test_the_console_handles_it_instead_of_nagging_the_agent(self):
        # 催了 agent 也只换回「本环境没有 rename_chat」——控制台能自己改就别催
        self._push(AUTO)
        self._push(AUTO)
        self.assertEqual([], self.nudged)

    def test_with_auto_rename_off_the_old_nudge_still_fires(self):
        hub.HUB.cfg["sidebar_auto_rename"] = False
        self._push(AUTO)
        self._push(AUTO)
        self.assertEqual([AUTO], self.nudged)

    def test_without_the_driver_the_old_nudge_still_fires(self):
        with patch.object(hub, "cursor_live_rename", None):
            self._push(AUTO)
            self._push(AUTO)
        self.assertEqual([AUTO], self.nudged)


class IdleProbeTests(unittest.TestCase):
    def test_idle_seconds_is_a_non_negative_float_on_windows(self):
        idle = live.user_idle_secs()
        if os.name != "nt":
            self.assertIsNone(idle)
            return
        self.assertIsInstance(idle, float)
        self.assertGreaterEqual(idle, 0.0)
        self.assertLess(idle, 49.7 * 24 * 3600, "回绕处理坏了才会算出几十天")


if __name__ == "__main__":
    unittest.main()
