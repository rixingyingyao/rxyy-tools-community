# -*- coding: utf-8 -*-
"""Cursor 侧栏顶着自动标题、写盘又被盖回来时，催这个 tab 自己改名。

08-28 用户看着一屏「Persistent plus task report / reporting」问：
「侧栏，你想想办法，你要是现在给侧栏改名要怎么做？不能重启 cursor」。
把 Cursor 主包拆开之后，能改内存的口只剩一个，三条证据锁死：

1. `renameComposer(id,name)` 内部只有一句
   `updateComposerDataSetStore(h, s=>s("name",name))`——**内存是真身，磁盘只是
   它刷出去的影子**。对话开着时我们写进库的名字下一次刷盘就被盖掉。
2. `handleRenameChat` 的目标完全取自调用方自己的 conversationId，拿不到就回
   「could not identify the calling conversation」——**没有任何 agent 能替别的
   tab 改名**，一个对话只改得动自己那一个。
3. 在跑的 Cursor 命令行只有 exe 一项，没开 `--remote-debugging-port`，
   从外面注入 JS 要重启才行，不能用。

所以唯一不重启 Cursor 的自愈路径是：控制台发现「写了又被盖」，就把改名指令
塞进这个 tab 的队列，它下一次 zt 顺路取信时自己调 rename_chat。
本测试锁死这条链的触发条件、只催一次、以及子代理那半句交回主对话的话术。
"""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub  # noqa: E402

WS = r"d:\Desktop\cursor工作流"
AUTO = "Persistent plus zhi report"


def _s(name="控制台·侧栏改名", uid="c6c4d53a-1111-2222-3333-444455556666"):
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
    s.lock = threading.Lock()
    return s


class ClobberDetectionTests(unittest.TestCase):
    """什么时候才算「磁盘这条路输了」。"""

    def setUp(self):
        self.s = _s()
        self.nudged = []
        ps = [patch.object(hub.HUB, "sessions", {self.s.id: self.s}),
              patch.object(hub.HUB, "cfg", {"max_messages": 200}),
              patch.object(hub, "log_event", lambda *a, **k: None),
              patch.object(hub, "write_cursor_title", lambda *a, **k: True),
              patch.object(hub.Api, "_nudge_cursor_rename",
                           lambda api, s, stale="": self.nudged.append(stale))]
        for p in ps:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in ps])

    def _push(self, title_in_db):
        with patch.object(hub, "read_cursor_title", lambda *a, **k: title_in_db):
            hub.HUB._push_name_to_cursor(self.s)

    def test_the_very_first_write_never_nudges(self):
        # 还没写过，库里是自动标题很正常——先写一次再说
        self._push(AUTO)
        self.assertEqual([], self.nudged)
        self.assertEqual(self.s.name, self.s.cursor_title)

    def test_a_write_that_stuck_never_nudges(self):
        self._push(AUTO)
        self._push(self.s.name)
        self.assertEqual([], self.nudged)

    def test_a_write_that_got_clobbered_nudges(self):
        """写完下一拍库里又变回自动标题 = Cursor 把内存那份刷回来盖掉了，
        也就是这个对话还开着，磁盘永远赢不了。

        0902 起催单只在控制台自己改不了时才发（sidebar_auto_rename 关着 / 没有
        cursor_live_rename 驱动）：能自己按 composer.renameChat 就不占 agent 的对话。
        自动改那条链在 test_sidebar_auto_rename 里锁。"""
        hub.HUB.cfg["sidebar_auto_rename"] = False
        self._push(AUTO)
        self._push(AUTO)
        self.assertEqual([AUTO], self.nudged)

    def test_with_the_console_able_to_rename_itself_the_agent_is_not_nagged(self):
        # 这台 Cursor 3.17 的 agent 没有 rename_chat，催一次就换回一句「本环境没有」
        with patch.object(hub, "cursor_live_rename", object()):  # 驱动在（非 Windows 的 CI 上也算在）
            self._push(AUTO)
            self._push(AUTO)
        self.assertEqual([], self.nudged)
        self.assertEqual(AUTO, self.s.sidebar_stale, "信号得留给 _auto_fix_sidebar")

    def test_a_standby_shell_never_gets_here(self):
        # 壳的 uuid 是猜的，往外写就可能改到别人干活的对话上
        self.s.name = "待命·cursor工作流·f9c3"
        self._push(AUTO)
        self._push(AUTO)
        self.assertEqual([], self.nudged)

    def test_a_tab_with_no_composer_bound_is_skipped(self):
        self.s.cursor_uuid = None
        with patch.object(hub, "locate_cursor_session", lambda *a, **k: None):
            self._push(AUTO)
        self.assertEqual([], self.nudged)


class NudgeContentTests(unittest.TestCase):
    """催单本身：走队列、只催一次、别把自己变成 tab 的新名字。"""

    def setUp(self):
        self.s = _s()
        ps = [patch.object(hub.HUB, "sessions", {self.s.id: self.s}),
              patch.object(hub.HUB, "cfg", {"max_messages": 200}),
              patch.object(hub, "log_event", lambda *a, **k: None),
              patch.object(hub.Hub, "save_msg_images", lambda h, imgs: [])]
        for p in ps:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in ps])

    def _nudge(self):
        hub.Api()._nudge_cursor_rename(self.s, AUTO)

    def test_it_spells_out_the_literal_call_with_the_real_title(self):
        self._nudge()
        self.assertEqual(1, len(self.s.queued))
        text = self.s.queued[0]["text"]
        self.assertIn("rename_chat", text)
        self.assertIn("cursor-app-control", text)
        self.assertIn('"title":"控制台·侧栏改名"', text)
        self.assertIn(AUTO, text, "得告诉它现在侧栏上写的是什么，否则无从判断")

    def test_a_subagent_is_told_to_hand_it_back_up_instead_of_skipping(self):
        """这是历次没治好的根子：规则里写着「工具表里没有就跳过」，而本机的活
        全是主对话整体委派给子代理干的，于是每个 agent 都合规地静默跳过，
        一次都没改过。子代理这一档必须给它别的动作，不能只说「跳过」。"""
        self._nudge()
        text = self.s.queued[0]["text"]
        self.assertIn("子代理", text)
        self.assertIn("主对话", text)
        self.assertIn("不要静默跳过", text)

    def test_it_goes_to_the_queue_as_a_console_message(self):
        # who=控制台 才不会占用户的回复位，也才不会被拿去当 tab 的新名字
        self._nudge()
        self.assertEqual("控制台", self.s.queued[0]["who"])
        self.assertEqual("控制台·侧栏改名", self.s.name)

    def test_the_same_name_is_only_nudged_once(self):
        for _ in range(5):
            self._nudge()
        self.assertEqual(1, len(self.s.queued))

    def test_renaming_the_tab_arms_the_nudge_again(self):
        self._nudge()
        self.s.name = "控制台·接入提速"
        self._nudge()
        self.assertEqual(2, len(self.s.queued))

    def test_it_never_takes_the_users_reply_slot(self):
        self.s.pending = {"id": "q1"}
        answered = []
        with patch.object(hub.Api, "send_reply",
                          lambda *a, **k: answered.append(a) or {"ok": True}):
            self._nudge()
        self.assertEqual([], answered)
        self.assertEqual(1, len(self.s.queued))
        self.assertIsNotNone(self.s.pending)


if __name__ == "__main__":
    unittest.main()
