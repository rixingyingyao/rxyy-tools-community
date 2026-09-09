# -*- coding: utf-8 -*-
"""过期补送不许顶掉真回复（死会话处置③，08-13 两起实证）。

实证一（09:47）：用户回复已实际送达，agent 收到后继续 zt 干活了几个小时；
10:55 hub 发布重启断连触发救援，把那条早已送达的回复又补送入队——16:23 agent
下一次 zhi 刚问出口就被这条旧补送「答」掉，用户的真回复没了着落。
实证二（14:08）：同机制，旧图片重放顶掉了在飞提问。

两层修：① 救援侧——回复送出后 agent 仍有 zt 上报 = 它已带着回复从阻塞的 zhi
里出来了（没送到的话按重试纪律它会重挂 zhi，不可能在干活），不补送；
② 交付侧——补送入队之后用户又真实回复过 = 对话已进新轮次，过期补送剔除，
pending 保持挂着等真回复。
"""
import sys
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub  # noqa: E402


def _sess(**kw):
    s = hub.Session.__new__(hub.Session)
    s.id, s.name, s.conv_key = "s1", "rxyy MCP·根治筹备", "1a49a6ec"
    s.connected, s.pending, s.queued, s.rev = True, None, [], 0
    s.lock = threading.Lock()
    s.messages = []
    s.agent_status_ts = 0
    s.processing_since = None
    s.last_reply_probe = None
    s.last_reply_ts = 0
    for k, v in kw.items():
        setattr(s, k, v)
    return s


class RescueSkipsDeliveredReplyTests(unittest.TestCase):
    """救援侧：zt 闭环判据。"""

    def test_zt_after_reply_means_delivered_no_redelivery(self):
        # 实证一：09:47 回复送出，其后 agent 一直 zt——救援不该补送
        now = time.time()
        s = _sess(agent_status_ts=now - 300)  # 回复送出之后 5 分钟还在 zt
        s.last_reply_probe = {"ts": now - 600, "text": "点了，你盯着",
                              "images": [], "files": [], "who": None,
                              "msg_ref": {"role": "user"}}
        with patch.object(hub, "log_event"):
            hub.HUB._rescue_swallowed_reply(s, why="hub 发布重启断连")
        self.assertEqual([], s.queued, "已送达的回复不许再补送")
        self.assertIsNone(s.last_reply_probe)

    def test_no_zt_after_reply_still_rescues(self):
        # 回复送出后 agent 再无任何动静——老规矩照常补送，且带过期判据字段
        now = time.time()
        s = _sess(agent_status_ts=now - 700)  # 只有回复**之前**的 zt
        s.last_reply_probe = {"ts": now - 600, "text": "改成蓝色",
                              "images": [], "files": [], "who": None,
                              "msg_ref": {"role": "user"}}
        with (patch.object(hub, "log_event"),
              patch.object(hub.Hub, "add_message",
                           lambda self, sess, m: sess.messages.append(m))):
            hub.HUB._rescue_swallowed_reply(s, why="通道断开")
        self.assertEqual(1, len(s.queued))
        self.assertIn("[补送", s.queued[0]["text"])
        self.assertTrue(s.queued[0].get("redelivery"))
        self.assertGreater(float(s.queued[0].get("ts") or 0), now - 5)


class FlushSkipsStaleRedeliveryTests(unittest.TestCase):
    """交付侧：过期补送剔除，pending 留给真回复。"""

    def _flush(self, s):
        with (patch.object(hub, "log_event"),
              patch.object(hub.Hub, "add_message",
                           lambda self, sess, m: sess.messages.append(m))):
            return hub.HUB._flush_queue(s)

    def test_stale_redelivery_is_dropped_and_pending_kept(self):
        # 实证一下半场：10:55 入队的补送，16:07 用户又真实回复过——16:23 的
        # zhi 到达时它必须被剔除，pending 保持挂着
        now = time.time()
        s = _sess(last_reply_ts=now - 60)
        s.pending = {"id": "rpc-9"}
        s.queued = [{"id": "q1", "text": "[补送|此回复此前可能因 IDE 端中断未送达，"
                                         "若已收到请忽略重复] 点了，你盯着",
                     "images": [], "files": [], "who": None,
                     "redelivery": True, "ts": now - 3600}]
        s.messages = [{"qid": "q1", "queued": True}]
        sent = []
        s.send = sent.append
        self.assertFalse(self._flush(s))
        self.assertEqual([], s.queued, "过期补送必须剔除")
        self.assertIsNotNone(s.pending, "pending 必须留给真回复")
        self.assertEqual([], sent, "不许消费这个 zhi")
        self.assertFalse(s.messages[0]["queued"], "气泡不再显示排队中")
        self.assertTrue(any("过期补送" in (m.get("html") or "") for m in s.messages),
                        "要给用户留一句为什么跳过")

    def test_fresh_redelivery_still_delivers(self):
        # 补送之后用户没再说过话——它就是最新的话，照常交付
        now = time.time()
        s = _sess(last_reply_ts=now - 7200)  # 最近真实回复早于补送入队
        s.pending = {"id": "rpc-9"}
        s.queued = [{"id": "q1", "text": "[补送|…] 改成蓝色", "images": [],
                     "files": [], "who": None, "redelivery": True,
                     "ts": now - 3600}]
        s.messages = [{"qid": "q1", "queued": True}]
        sent = []
        s.send = sent.append
        with (patch.object(hub.Api, "_with_library_digest",
                           lambda self, sess, ui: (ui, None)),
              patch.object(hub.Hub, "log_user", lambda self, *a, **k: None)):
            self.assertTrue(self._flush(s))
        self.assertEqual(1, len(sent))
        self.assertIn("改成蓝色", sent[0]["user_input"])
        self.assertIsNone(s.pending)

    def test_machine_only_queue_still_waits(self):
        # 原有行为回归锁：队列里只有机器转告时不消费 zhi（08-04 老规矩）
        s = _sess()
        s.pending = {"id": "rpc-9"}
        s.queued = [{"id": "q2", "text": "转告内容", "who": "agent·某壳",
                     "images": [], "files": []}]
        self.assertFalse(self._flush(s))
        self.assertIsNotNone(s.pending)
        self.assertEqual(1, len(s.queued), "机器信照旧等 zt 顺路捎带")


if __name__ == "__main__":
    unittest.main(verbosity=2)
