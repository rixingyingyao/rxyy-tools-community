# -*- coding: utf-8 -*-
"""报到重发去重：同会话未答复期间重复投递同一句报到只留一张卡——第七节复现。

现场（方案书第七节）：08-12 23:39-23:45 接手 agent 报到连发 5 次客户端报
`Failed to start MCP session reinitialization`，第 6 次才成。全程消息零丢失，
代价是同一 tab 堆出 6 张一模一样的报到卡片——每次 reinit 重试都走
_handle_client_msg 的 zhi_request 分支 add_message 新开一张气泡。

根治：同 conversation_id 的 zhi，在前一条未答复且「正文 + 选项完全相同」时，
把旧 waiter 以 superseded 收掉、绿灯与那张卡原地保留，只把等待方换成新请求
——不再 add_message。正文/选项不同的新提问照旧开新卡（不误伤真·换问题）。
"""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub  # noqa: E402

WS = r"d:\桌面\working\cursor工作流"


class _Client:
    def __init__(self):
        self.closed_convs, self.sessions, self.cwd, self.pid = {}, {}, WS, 111
        self.last_heartbeat = 0
        self.sent = []

    def send(self, obj):
        self.sent.append(obj)


def _sess(sid, conv, name):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = sid, conv, name
    s.cwd = s.task_root = WS
    s.connected = True
    s.pending = None
    s.processing_since = None
    s.detached = False
    s.detached_since = None
    s.buffered_reply = None
    s.last_reply_probe = None
    s.agent_status = ""
    s.agent_activity = ""
    s.last_heartbeat = 0
    s.last_zhi_ts = 0
    s.queued = []
    s.messages = []
    s.msg_seq = 0
    s.machine_seq = 0
    s.real_seq = 0
    s.rev = 0
    s.file_path = None
    s.lock = threading.Lock()
    return s


def _bubbles(s):
    return [m for m in s.messages if m.get("role") == "ai"]


class _ImmediateThread:
    """zhi 的 _alert 是 daemon 线程；单测里同步跑，避免 stopall 后落到真推送。"""

    def __init__(self, target=None, daemon=None, **kwargs):
        self._target = target

    def start(self):
        if self._target:
            self._target()


class CheckinRedeliveryDedup(unittest.TestCase):
    def setUp(self):
        self.client = _Client()
        self.s = _sess("t1", "c1", "待命·cursor工作流")
        self.s.client = self.client   # s.send 经由它把 superseded 送回客户端
        self.client.sessions["c1"] = self.s
        self._patches = [
            patch.object(hub.HUB, "sessions", {"t1": self.s}),
            patch.object(hub.HUB, "order", ["t1"]),
            patch.object(hub.HUB, "cfg", {"max_messages": 200}),
            patch.object(hub.HUB, "resolve_session",
                         lambda cli, ck, tn: self.s),
            patch.object(hub.HUB, "_verify_identity_by_generating", MagicMock()),
            patch.object(hub.HUB, "_reap_takeover_shell", MagicMock()),
            patch.object(hub.HUB, "log_ai", MagicMock()),
            patch.object(hub.HUB, "notify", MagicMock()),
            patch.object(hub.HUB, "wake_window", MagicMock()),
            patch.object(hub.HUB, "flash_taskbar", MagicMock()),
            patch.object(hub.HUB, "push_phone", MagicMock()),
            patch.object(hub.Api, "_nudge_rename_if_standby", MagicMock()),
            patch.object(hub, "log_event", lambda *a, **k: None),
            patch.object(hub.threading, "Thread", _ImmediateThread),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(patch.stopall)

    def _report(self, rpc_id, message="📍 报到就位", options=("开始任务", "结束")):
        hub.HUB._handle_client_msg(self.client, None, {
            "type": "zhi_request", "id": rpc_id, "conversation_id": "c1",
            "message": message, "predefined_options": list(options),
            "is_markdown": True,
        })

    def test_three_retries_leave_one_card(self):
        # 第七节复现：同壳连发 3 次同一句报到，只该留 1 张卡
        self._report("rpc-1")
        self._report("rpc-2")
        self._report("rpc-3")
        self.assertEqual(1, len(_bubbles(self.s)), "重复报到必须合并成一张卡")
        self.assertEqual("rpc-3", self.s.pending["id"], "等待方换成最新一次请求")

    def test_old_waiters_get_superseded_so_they_unblock(self):
        # 消息零丢失的另一半：旧 waiter 必须收到 superseded 应答，不能永远阻塞
        self._report("rpc-1")
        self._report("rpc-2")
        superseded = [m for m in self.client.sent
                      if m.get("source") == "superseded" and m.get("id") == "rpc-1"]
        self.assertEqual(1, len(superseded))

    def test_a_genuinely_different_question_still_opens_a_new_card(self):
        # 不误伤真·换问题：正文变了就该开新卡（并把旧的 superseded 收掉）
        self._report("rpc-1", message="📍 报到就位")
        self._report("rpc-2", message="第八节做完了，要发布吗？")
        self.assertEqual(2, len(_bubbles(self.s)))
        self.assertEqual("rpc-2", self.s.pending["id"])

    def test_different_options_same_text_still_opens_a_new_card(self):
        self._report("rpc-1", options=("开始任务", "结束"))
        self._report("rpc-2", options=("A", "B", "C"))
        self.assertEqual(2, len(_bubbles(self.s)))

    def test_dedup_clears_detach_state(self):
        # 重发合并时把脱离态一并归位（等待方已换新，绿灯重新有主）
        self._report("rpc-1")
        self.s.detached = True
        self.s.detached_since = time.time()
        self._report("rpc-2")
        self.assertFalse(self.s.detached)
        self.assertIsNone(self.s.detached_since)

    def test_checkin_zhi_does_not_push_phone(self):
        # 08-14：待命报到不是真提问，不应 push_phone
        self._report("rpc-1")
        self.assertEqual(0, hub.HUB.push_phone.call_count)

    def test_real_question_still_pushes_phone(self):
        self._report("rpc-1", message="三处改完要重启，怎么干？",
                     options=("就这么干", "先问我"))
        self.assertGreaterEqual(hub.HUB.push_phone.call_count, 1)


if __name__ == "__main__":
    unittest.main()
