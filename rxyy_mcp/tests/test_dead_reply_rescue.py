# -*- coding: utf-8 -*-
"""agent 被 Cursor 在 API 层杀掉（欠费/额度）时，刚发出去的回复不能人间蒸发。

08-03 同事机实测：通道一直连着、tab 转黄「处理中」、气泡跟送到了的一模一样，
实际那条回复躺在一个已死的请求里，人和 AI 各等了一小时。
此前只有「TCP 通道断开」会触发救援，这类死法一声不吭。
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


def _session_with_unanswered_reply(sent_at):
    s = hub.Session.__new__(hub.Session)
    s.id, s.name, s.conv_key = "s1", "待命·jsjb", "b6e1007f"
    s.connected, s.pending, s.queued, s.rev = True, None, [], 0
    s.lock = threading.Lock()
    s.cursor_uuid, s.uuid_verified = "uuid-1", True
    s.death_probe_at, s.death_info, s.death_alerts = 0, None, {}
    s.agent_status_ts = 0
    s.processing_since = sent_at          # 回复送出后一直没等到 AI 再提问
    s.messages = []
    s.msg_ref = {"role": "user", "html": "你是什么模型？"}
    s.last_reply_probe = {
        "ts": sent_at, "text": "你是什么模型？", "images": [], "files": [],
        "who": None, "msg_ref": s.msg_ref,
    }
    return s


def _fatal(at_ts):
    return {"code": 50, "reason": "账号欠费", "advice": "去 dashboard 付清账单",
            "is_last": True, "bubble_id": "bubble-1", "at_ts": at_ts}


class DeadReplyRescueTests(unittest.TestCase):
    def _probe(self, s, err):
        with (patch.object(hub, "read_cursor_error", lambda uid: err),
              patch.object(hub.Hub, "add_message",
                           lambda self, sess, m: sess.messages.append(m)),
              patch.object(hub.Hub, "alert_session_death", lambda self, sess, d: None),
              patch.object(hub, "WORKFLOW", None)):
            hub.HUB._tick_death_probe(s, time.time())

    def test_death_requeues_the_reply_nobody_received(self):
        now = time.time()
        s = _session_with_unanswered_reply(now - 300)
        self._probe(s, _fatal(now - 240))

        self.assertEqual(1, len(s.queued), "回复必须转进队列等补送，不能就这么没了")
        self.assertIn("[补送", s.queued[0]["text"])
        self.assertIn("你是什么模型？", s.queued[0]["text"])
        self.assertTrue(s.msg_ref.get("undelivered"),
                        "原气泡要标「未送达」，否则跟送到了的长得一样")
        self.assertIsNone(s.last_reply_probe, "探针用过即清，别重复补送")
        self.assertTrue(any("账号欠费" in (m.get("html") or "") for m in s.messages),
                        "系统提示里要写清死因，用户才知道是去付钱还是换号")

    def test_no_pending_reply_means_nothing_to_rescue(self):
        now = time.time()
        s = _session_with_unanswered_reply(now - 300)
        s.last_reply_probe = None
        self._probe(s, _fatal(now - 240))
        self.assertEqual([], s.queued)

    def test_alive_again_after_error_is_not_a_death(self):
        # 报错之后 agent 又跟控制台说过话 = 它缓过来了，不能判死、更不能把回复抽走
        now = time.time()
        s = _session_with_unanswered_reply(now - 10)
        s.agent_status_ts = now          # 报错之后还上报过 zt
        self._probe(s, _fatal(now - 240))
        self.assertEqual([], s.queued)
        self.assertIsNotNone(s.last_reply_probe)


class DefaultsTests(unittest.TestCase):
    def test_detach_grace_outlives_keepalive(self):
        """脱离宽限必须大于保活周期，否则 agent 每次正常续期都可能被判失联，
        绿灯灭掉、提问清空，用户之后发的话只能排队等下一次提问。"""
        self.assertGreater(hub.DEFAULTS["detach_grace_secs"],
                           hub.DEFAULTS["keepalive_secs"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
