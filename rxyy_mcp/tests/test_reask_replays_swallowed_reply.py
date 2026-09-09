# -*- coding: utf-8 -*-
"""重问补送闸：agent 原样重问同一道题 = 上一条回复没送到，自动补送，别再让用户重打。

现场（08-26 截图实证）：用户回复送进了一个已被 Cursor 打断的 zhi——打断不发
cancelled 通知，socket 还活着，断线救援（_client_disconnect → _rescue_swallowed_reply）
永远不会醒。agent 拿不到回复，按重试纪律把同一道题原样再问一遍。老逻辑把
「新提问到达」一律当作上轮正常闭环，进门先清 last_reply_probe——用户那条回复
就此蒸发，界面上是同一个问题的第二张卡，人还得亲手把刚说过的话再发一遍。

契约：
* 同正文 + 同选项的重问，且回复送出后 agent 再无 zt 动静（没收到的铁证）
  → 探针转进队列，本次落卡后 _flush_queue 立刻自动答上去，用户零操作；
* 回复送出后 agent 还 zt 过（已送达铁证）→ 视为真·再问一遍，照旧开新卡等人答；
* 正文或选项不同 → 正常新提问，探针照老规矩清掉。

探针识别重问靠 send_reply 记下的 question/q_options 两个新字段——那是回复
出发时它所应答的那道题。
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
Q = "三处改完要重启 hub，现在就重启吗？"
OPTS = ("重启", "先不")
REPLY = "重启，顺手把缓存也清了"


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
    s.agent_status_ts = 0
    s.last_reply_ts = 0
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


class _ImmediateThread:
    """zhi 的 _alert 是 daemon 线程；单测里同步跑，避免 stopall 后落到真推送。"""

    def __init__(self, target=None, daemon=None, **kwargs):
        self._target = target

    def start(self):
        if self._target:
            self._target()


class ReaskReplayTests(unittest.TestCase):
    def setUp(self):
        self.client = _Client()
        self.s = _sess("t1", "c1", "全面体检")
        self.s.client = self.client
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
            patch.object(hub.HUB, "log_user", MagicMock()),
            patch.object(hub.HUB, "notify", MagicMock()),
            patch.object(hub.HUB, "wake_window", MagicMock()),
            patch.object(hub.HUB, "flash_taskbar", MagicMock()),
            patch.object(hub.HUB, "push_phone", MagicMock()),
            patch.object(hub.Api, "_nudge_rename_if_standby", MagicMock()),
            patch.object(hub.Api, "_with_library_digest",
                         lambda api, s, ui, selected=None: (ui, False)),
            patch.object(hub, "log_event", lambda *a, **k: None),
            patch.object(hub.threading, "Thread", _ImmediateThread),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(patch.stopall)

    def _ask(self, rpc_id, message=Q, options=OPTS):
        hub.HUB._handle_client_msg(self.client, None, {
            "type": "zhi_request", "id": rpc_id, "conversation_id": "c1",
            "message": message, "predefined_options": list(options),
            "is_markdown": True,
        })

    def _arm_probe(self, sent_ago=120, question=Q, options=OPTS):
        """布好现场：REPLY 已在 sent_ago 秒前送出（send_reply 会记探针）。"""
        ref = {"role": "user", "ts": "10:00:00", "html": REPLY}
        self.s.messages.append(ref)
        now = time.time()
        self.s.last_reply_probe = {
            "ts": now - sent_ago, "text": REPLY, "images": [], "files": [],
            "who": None, "msg_ref": ref,
            "question": question, "q_options": list(options),
        }
        self.s.last_reply_ts = now - sent_ago
        return ref

    def _auto_answers(self, rpc_id):
        return [m for m in self.client.sent
                if m.get("type") == "zhi_response" and m.get("id") == rpc_id
                and m.get("source") == "popup_queued"]

    def test_reask_same_question_redelivers_the_swallowed_reply(self):
        # 回复送出后 agent 再无 zt（agent_status_ts 停在回复之前）→ 没收到的铁证
        ref = self._arm_probe()
        self.s.agent_status_ts = time.time() - 600
        self._ask("rpc-2")
        answers = self._auto_answers("rpc-2")
        self.assertEqual(1, len(answers), "重问必须被探针里的回复当场答掉")
        self.assertIn(REPLY, answers[0].get("user_input") or "",
                      "补送的得是用户原话")
        self.assertIn("补送", answers[0].get("user_input") or "",
                      "要带补送标记，万一其实已送到 agent 才知道能忽略")
        self.assertIsNone(self.s.pending, "已自动答掉，不许再挂卡等用户")
        self.assertIsNone(self.s.last_reply_probe, "探针用过即清")
        self.assertTrue(ref.get("undelivered"),
                        "原气泡要标「未送达」，用户才知道那趟没人接")
        self.assertEqual(0, hub.HUB.push_phone.call_count,
                         "已自动答掉的重问不该再推手机")

    def test_reask_after_zt_closure_is_a_real_question_again(self):
        # 回复送出后 agent 还 zt 过 = 已送达；它再问同一道题是真的又想问一遍
        ref = self._arm_probe()
        self.s.agent_status_ts = time.time() - 60   # 回复(120s前)之后还有动静
        self._ask("rpc-2")
        self.assertEqual([], self._auto_answers("rpc-2"),
                         "已送达过的回复不许再补送——会答非所问")
        self.assertIsNotNone(self.s.pending, "真·再问一遍要照旧挂卡等人答")
        self.assertEqual("rpc-2", self.s.pending["id"])
        self.assertIsNone(self.s.last_reply_probe, "探针此时该按老规矩清掉")
        self.assertFalse(ref.get("undelivered"), "送达过的气泡不许倒打「未送达」")

    def test_a_different_question_never_triggers_the_replay(self):
        ref = self._arm_probe()
        self.s.agent_status_ts = time.time() - 600
        self._ask("rpc-2", message="下一步先做哪件？", options=("A", "B"))
        self.assertEqual([], self._auto_answers("rpc-2"))
        self.assertIsNotNone(self.s.pending, "换了问题就是正常新提问")
        self.assertIsNone(self.s.last_reply_probe)
        self.assertFalse(ref.get("undelivered"))

    def test_same_text_but_different_options_is_not_a_retry(self):
        self._arm_probe()
        self.s.agent_status_ts = time.time() - 600
        self._ask("rpc-2", options=("重启", "先不", "再想想"))
        self.assertEqual([], self._auto_answers("rpc-2"))
        self.assertIsNotNone(self.s.pending)


class ProbeRecordsTheQuestionTests(unittest.TestCase):
    """send_reply 记探针时必须带上它所应答的那道题——重问识别的唯一凭据。"""

    def setUp(self):
        self.s = _sess("t1", "c1", "全面体检")
        self.s.pending = {"id": "req1", "message": Q, "options": list(OPTS)}
        self.sent = []
        self.s.send = lambda payload: self.sent.append(payload)
        ps = [
            patch.object(hub.HUB, "sessions", {"t1": self.s}),
            patch.object(hub.HUB, "cfg", {"library_autosend": False,
                                          "max_messages": 200}),
            patch.object(hub.Api, "_with_session_memory",
                         lambda api, s, ui, selected=None: ui),
            patch.object(hub.Api, "_with_library_digest",
                         lambda api, s, ui, selected=None: (ui, False)),
            patch.object(hub.Api, "_auto_label_on_dispatch",
                         lambda api, s, text, who=None: None),
            patch.object(hub.Hub, "add_message", lambda h, s, m: None),
            patch.object(hub.Hub, "save_msg_images", lambda h, imgs: []),
            patch.object(hub.Hub, "log_user", lambda h, *a, **k: None),
            patch.object(hub.Hub, "_mark_claimed", lambda h, *a, **k: None),
            patch.object(hub, "log_event", lambda *a, **k: None),
        ]
        for p in ps:
            p.start()
        self.addCleanup(patch.stopall)

    def test_probe_carries_question_and_options(self):
        r = hub.Api().send_reply("t1", REPLY, [], [], False)
        self.assertTrue(r["ok"], r)
        probe = self.s.last_reply_probe
        self.assertIsNotNone(probe, "有实质内容的回复必须记探针")
        self.assertEqual(Q, probe.get("question"))
        self.assertEqual(list(OPTS), probe.get("q_options"))


if __name__ == "__main__":
    unittest.main()
