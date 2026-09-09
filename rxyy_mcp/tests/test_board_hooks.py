# -*- coding: utf-8 -*-
"""任务面板的三个钩子：hub 只发事实，且**绝不能因此拖累任何人**。

看板事件是锦上添花：控制台没开、答不上来、甚至钩子自己崩了，hub 的消息主循环和
判死巡检都得照样走完。这份测试里最要紧的不是「钩子发出去了」，而是最后那两条
回归锁——把出站函数换成一抛就炸的，zt 照样落到 tab 上、判死照样记上。

其余四条锁住语义：进度带上活动内容、十几秒内的重复进度吃掉、只有「收工」触发
送验收、判死让路时不退卡。
"""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import board_hooks  # noqa: E402
import hub  # noqa: E402
import session_core  # noqa: E402

WS = r"d:\Desktop\cursor工作流"


def _sess(sid="s1", conv="7edf659a", name="rxyy tools·任务面板"):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = sid, conv, name
    s.cursor_uuid = "uid-1"  # 判死只认有 Cursor 对话 uid 的 tab
    s.death_probe_at = 0
    s.uuid_verified = False
    s.cwd = s.task_root = WS
    s.name_locked = False
    s.shell_born = True
    s.agent_named = True
    s.agent_project = "rxyy tools"
    s.agent_status = ""
    s.agent_activity = ""
    s.agent_status_ts = 0
    s.connected = True
    s.pending = None
    s.queued = []
    s.messages = []
    s.msg_seq = 9
    s.rev = 0
    s.client = None
    s.file_path = None
    s.id_history = []
    s.name_history = []
    s.last_heartbeat = time.time()
    s.lock = threading.RLock()
    return s


class _FakeClient:
    def __init__(self, sessions):
        self.sessions = {x.conv_key: x for x in sessions}
        self.closed_convs = {}
        self.last_heartbeat = 0.0
        self.cwd = WS


class BoardHookSubmitTests(unittest.TestCase):
    """三个动词各自送出什么（拦在队列入口，不真的发 HTTP）。"""

    def setUp(self):
        self.sent = []
        p = patch.object(board_hooks._Sender, "submit",
                         lambda _self, method, args: self.sent.append((method, args)))
        p.start()
        self.addCleanup(p.stop)
        board_hooks._SENDER._last_note.clear()

    def test_progress_carries_what_it_is_doing(self):
        board_hooks.note_progress("7edf659a", "developing · 接看板钩子")
        self.assertEqual([("board_note_activity",
                           ["7edf659a", "developing · 接看板钩子"])], self.sent)

    def test_the_same_progress_twice_in_a_row_is_swallowed(self):
        board_hooks.note_progress("7edf659a", "developing · 同一句")
        board_hooks.note_progress("7edf659a", "developing · 同一句")
        self.assertEqual(1, len(self.sent))
        # 换了内容就该报，别把真进展也吃掉
        board_hooks.note_progress("7edf659a", "developing · 换了一句")
        self.assertEqual(2, len(self.sent))
        # 别人的会话不受我的节流影响
        board_hooks.note_progress("bc18e44d", "developing · 同一句")
        self.assertEqual(3, len(self.sent))

    def test_empty_things_are_not_worth_a_round_trip(self):
        board_hooks.note_progress("", "有内容没会话")
        board_hooks.note_progress("7edf659a", "")
        board_hooks.finish_session("")
        board_hooks.release_session("")
        self.assertEqual([], self.sent)


class BoardHookWiringTests(unittest.TestCase):
    """挂点接对了没有：zt / 黑板收工 / 判死。"""

    def setUp(self):
        self.sent = []
        self.s = _sess()
        d = {self.s.id: self.s}
        self.patches = [
            patch.object(hub.HUB, "sessions", d),
            patch.object(hub.HUB, "order", list(d)),
            patch.object(hub.HUB, "cfg", {"max_messages": 200}),
            patch.object(hub.HUB, "name_tombstones", {}),
            patch.object(hub.Hub, "save_state", lambda self: None),
            patch.object(hub.Hub, "_verify_identity_by_generating", lambda self, s: None),
            patch.object(hub.Hub, "_reap_takeover_shell", lambda self, s: None),
            patch.object(hub.Hub, "notify_status_only", lambda self: None),
            patch.object(hub.Api, "_drop_auto_assign", lambda api, s: None),
            patch.object(hub.Api, "_nudge_rename_if_standby", lambda api, s: None),
            patch.object(hub, "log_event", lambda *a, **k: None),
            patch.object(board_hooks._Sender, "submit",
                         lambda _self, m, a: self.sent.append((m, a))),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])
        board_hooks._SENDER._last_note.clear()

    def _zt(self, **extra):
        msg = {"type": "agent_status", "conversation_id": self.s.conv_key,
               "status": "developing", "activity": "接看板钩子"}
        msg.update(extra)
        hub.HUB._handle_client_msg(_FakeClient([self.s]), None, msg)

    def test_a_zt_becomes_progress_on_the_card(self):
        self._zt()
        self.assertEqual([("board_note_activity",
                           ["7edf659a", "developing · 接看板钩子"])], self.sent)

    def test_a_zt_without_activity_still_reports_the_status(self):
        self._zt(activity="")
        self.assertEqual([("board_note_activity", ["7edf659a", "developing"])], self.sent)


class BoardPostFinishTests(unittest.TestCase):
    """黑板「收工」送验收，别的类别不动看板。"""

    def setUp(self):
        self.sent = []
        self.s = _sess()
        self.patches = [
            patch.object(hub.HUB, "bulletin", {}),
            patch.object(hub.Hub, "_save_bulletin", lambda self: None),
            patch.object(hub, "log_event", lambda *a, **k: None),
            patch.object(hub, "WORKFLOW", None),
            patch.object(hub.Api, "_team_scope",
                         lambda api, sender: {"root": WS, "project": "rxyy tools"}),
            patch.object(hub.Api, "session_label", lambda api, s: s.name),
            patch.object(hub.Api, "_board_notify",
                         lambda api, sender, kind, entry, root, project=None: []),
            patch.object(board_hooks._Sender, "submit",
                         lambda _self, m, a: self.sent.append((m, a))),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def test_knocking_off_sends_the_cards_for_review(self):
        hub.Api().board_from_agent(self.s, "收工", "看板钩子接完了")
        self.assertEqual([("board_finish_session", ["7edf659a", "看板钩子接完了"])], self.sent)

    def test_other_kinds_leave_the_board_alone(self):
        for kind in ("部署", "大改", "提交", "卡住", "事故"):
            hub.Api().board_from_agent(self.s, kind, "一条 " + kind)
        self.assertEqual([], self.sent)


class DeathReleasesTheCardTests(unittest.TestCase):
    """人没了就退卡；但判死让路的时候一张也不许退。"""

    def setUp(self):
        self.sent = []
        self.s = _sess()
        self.s.death_info = None
        self.s.death_alerts = {}
        self.patches = [
            patch.object(hub, "log_event", lambda *a, **k: None),
            patch.object(hub, "WORKFLOW", None),
            patch.object(board_hooks._Sender, "submit",
                         lambda _self, m, a: self.sent.append((m, a))),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def _probe(self, dead):
        hub_self = _DeathHost()
        with patch.object(hub, "read_cursor_error", lambda uid: dict(dead, is_last=True)):
            self.s.death_probe_at = 0
            session_core.tick_death_probe(hub_self, self.s, time.time())
        return hub_self

    def test_a_fresh_death_hands_the_card_back(self):
        self._probe({"bubble_id": "b1", "reason": "额度用尽", "code": "quota", "at_ts": 1})
        self.assertEqual([("board_release_session",
                           ["7edf659a", "会话判死：额度用尽"])], self.sent)

    def test_the_same_death_seen_twice_only_releases_once(self):
        dead = {"bubble_id": "b1", "reason": "额度用尽", "code": "quota", "at_ts": 1}
        self._probe(dead)
        self._probe(dict(dead))
        self.assertEqual(1, len(self.sent))

    def test_a_network_hiccup_does_not_take_the_card_away(self):
        # provider 不可达、连接中断这类几十秒就缓过来，退了卡它回头交付会被顶回来
        for reason in ("Unable to reach the model provider", "连接中断", "请求超时"):
            self.sent.clear()
            self.s.death_info = None
            self._probe({"bubble_id": "b-" + reason, "reason": reason,
                         "code": "41", "at_ts": 1})
            self.assertEqual([], self.sent, reason)
        # 但回不来的死法照退不误
        self.s.death_info = None
        self._probe({"bubble_id": "b-gone", "at_ts": 1, "code": "gone",
                     "reason": "对话已从 Cursor 里消失（被删或被政策回收）"})
        self.assertEqual(1, len(self.sent))

    def test_when_the_agent_spoke_after_the_error_nothing_is_released(self):
        # session_core 那道「报错后又说过话就不算死」的闸：卡不能被退掉
        self.s.agent_status_ts = time.time()
        self._probe({"bubble_id": "b2", "reason": "看着像死了", "code": "x", "at_ts": 1})
        self.assertEqual([], self.sent)


class _DeathHost:
    """tick_death_probe 需要的那点宿主行为，别的一律不提供。"""

    DEATH_PROBE_EVERY = 0  # 测试里每次调用都真的探一遍

    def __init__(self):
        self.rescued = []
        self.alerted = []

    def _fusion_recent_activity(self, s, now):
        return ""  # 四路信号都不新鲜 = 不让路，让判死走到底

    def _rescue_swallowed_reply(self, s, why="", note=""):
        self.rescued.append(why)

    def alert_session_death(self, s, dead):
        self.alerted.append(dead)


class HooksMustNeverBreakTheHostTests(unittest.TestCase):
    """回归锁：钩子一抛就炸时，hub 的两条主路照样走完。

    这是整份测试的重点。前面那些断言只是「功能对不对」，这两条断的是
    「出事会不会连累全队」——一条 zt 卡住的是所有 agent 的消息主循环。
    """

    def setUp(self):
        self.s = _sess()
        self.s.death_info = None
        self.s.death_alerts = {}

        def boom(*a, **k):
            raise RuntimeError("看板钩子故意炸给你看")

        d = {self.s.id: self.s}
        self.patches = [
            patch.object(hub.HUB, "sessions", d),
            patch.object(hub.HUB, "order", list(d)),
            patch.object(hub.HUB, "cfg", {"max_messages": 200}),
            patch.object(hub.HUB, "name_tombstones", {}),
            patch.object(hub.Hub, "save_state", lambda self: None),
            patch.object(hub.Hub, "_verify_identity_by_generating", lambda self, s: None),
            patch.object(hub.Hub, "_reap_takeover_shell", lambda self, s: None),
            patch.object(hub.Hub, "notify_status_only", lambda self: None),
            patch.object(hub.Api, "_drop_auto_assign", lambda api, s: None),
            patch.object(hub.Api, "_nudge_rename_if_standby", lambda api, s: None),
            patch.object(hub, "log_event", lambda *a, **k: None),
            patch.object(hub, "WORKFLOW", None),
            patch.object(board_hooks, "note_progress", boom),
            patch.object(board_hooks, "release_session", boom),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def test_a_broken_hook_does_not_stop_a_zt_from_landing(self):
        hub.HUB._handle_client_msg(_FakeClient([self.s]), None, {
            "type": "agent_status", "conversation_id": self.s.conv_key,
            "status": "developing", "activity": "钩子已经炸了"})
        self.assertEqual("developing", self.s.agent_status)
        self.assertEqual("钩子已经炸了", self.s.agent_activity)
        self.assertTrue(self.s.messages, "状态气泡照样要落到 tab 上")

    def test_a_broken_hook_does_not_stop_a_death_from_being_recorded(self):
        host = _DeathHost()
        dead = {"bubble_id": "b9", "reason": "额度用尽", "code": "quota",
                "at_ts": 1, "is_last": True}
        with patch.object(hub, "read_cursor_error", lambda uid: dead):
            session_core.tick_death_probe(host, self.s, time.time())
        self.assertEqual("额度用尽", (self.s.death_info or {}).get("reason"))
        self.assertEqual(1, len(host.alerted), "判死提醒照样要发")
        self.assertEqual(1, len(host.rescued), "被吞的回复照样要救")


if __name__ == "__main__":
    unittest.main()
