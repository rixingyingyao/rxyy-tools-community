# -*- coding: utf-8 -*-
"""转告寄存兜底（死会话处置②，08-13 实证）。

实证：日报壳 16:0x 终止后，1a49a6ec 的「README+安装方案」派活转告寄存一个多
小时无人收，发送方手里只有一条「✓ 已提交转告」——活就此断线，直到人工盘点
才发现。三件套：① 寄存条目记发送方与时刻（可溯源）② 死目标回执带死因与
在线候选（发送方有下一步）③ 滞留超时给发送方捎提醒（每条只提醒一次）。
"""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub  # noqa: E402

WS = r"d:\Desktop\cursor工作流"


def _s(sid, conv, name, connected=True, **kw):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = sid, conv, name
    s.cwd = s.task_root = WS
    s.connected = connected
    s.pending = None
    s.queued = []
    s.lock = threading.Lock()
    s.recon_deadline = 0
    s.archived = False
    s._assign = ""
    for k, v in kw.items():
        setattr(s, k, v)
    return s


class _Harness(unittest.TestCase):
    def _relay(self, sender, to, msg, sessions):
        delivered = []

        def fake_queue(api_self, sid, text, imgs, who=None, files=None, force=False):
            # 真入队到目标 queued（relay 靠 qid 找回条目补寄存档案），同时记流水
            t = next(x for x in sessions if x.id == sid)
            qid = "q" + sid
            with t.lock:
                t.queued.append({"id": qid, "text": text, "who": who,
                                 "images": [], "files": []})
            delivered.append({"sid": sid, "text": text, "who": who, "force": force})
            return {"ok": True, "qid": qid}

        d = {x.id: x for x in sessions}
        with (patch.object(hub.HUB, "sessions", d),
              patch.object(hub.HUB, "order", list(d)),
              patch.object(hub.HUB, "relay_log", []),
              patch.object(hub.HUB, "name_tombstones", {}),
              patch.object(hub.Hub, "_save_relays", lambda self: None),
              patch.object(hub.Hub, "_is_checkin_shellish", lambda self, x: False),
              patch.object(hub.Api, "queue_message", fake_queue),
              patch.object(hub.Api, "session_label", lambda self, s: s.name),
              patch.object(hub.Api, "_team_role", lambda self, s: ""),
              patch.object(hub.Api, "_team_assign",
                           lambda self, s: getattr(s, "_assign", ""))):
            r = hub.Api().relay_from_agent(sender, to, msg)
        return r, delivered


class ParkedArchiveTests(_Harness):
    def test_parked_entry_records_sender_and_time(self):
        a = _s("s1", "aaaa1111", "甲")
        t = _s("s2", "bbbb2222", "日报壳", connected=False,
               live_cache={"state": "died"}, death_info=None)
        before = time.time()
        r, _ = self._relay(a, "bbbb22", "README 派活", [a, t])
        self.assertTrue(r["ok"])
        entry = t.queued[0]
        self.assertEqual("aaaa1111", entry.get("from_conv"),
                         "寄存条目必须记下发送方，滞留了才找得到人提醒")
        self.assertGreaterEqual(float(entry.get("parked_ts") or 0), before)

    def test_dead_target_receipt_names_cause_and_candidates(self):
        a = _s("s1", "aaaa1111", "甲")
        t = _s("s2", "bbbb2222", "日报壳", connected=False,
               live_cache={"state": "died"},
               death_info={"reason": "Fable 5 hit a safety filter"},
               end_reason="")
        c = _s("s3", "cccc3333", "丙")  # 在线候选
        r, delivered = self._relay(a, "bbbb22", "README 派活", [a, t, c])
        self.assertTrue(r["ok"])
        receipt = next(x["text"] for x in delivered if x["sid"] == "s1")
        self.assertIn("safety filter", receipt, "回执必须带死因")
        self.assertIn("丙", receipt, "回执必须给出在线可转投候选")
        self.assertIn("cccc3333"[:8], receipt)


class StaleReminderTests(unittest.TestCase):
    def _tick(self, sessions, now):
        d = {x.id: x for x in sessions}
        hub.HUB._parked_scan_at = 0
        with (patch.object(hub.HUB, "sessions", d),
              patch.object(hub, "log_event")):
            hub.HUB._tick_parked_relay_reminders(now)

    def test_stale_parked_relay_reminds_sender_once(self):
        now = time.time()
        sender = _s("s1", "aaaa1111", "甲")
        t = _s("s2", "bbbb2222", "日报壳", connected=False)
        t.queued = [{"id": "q1", "text": "【agent 转告】README 派活",
                     "who": "agent·甲", "images": [], "files": [],
                     "from_conv": "aaaa1111", "parked_ts": now - 1900}]
        self._tick([sender, t], now)
        self.assertEqual(1, len(sender.queued), "滞留超时必须提醒发送方")
        self.assertIn("寄存滞留提醒", sender.queued[0]["text"])
        self.assertIn("日报壳", sender.queued[0]["text"])
        self.assertEqual("控制台", sender.queued[0]["who"],
                         "走机器信道，不许占用户回复位")
        self.assertTrue(t.queued[0].get("stale_reminded"))
        # 再扫一轮不重复提醒
        self._tick([sender, t], now + 61)
        self.assertEqual(1, len(sender.queued))

    def test_fresh_parked_relay_not_reminded_yet(self):
        now = time.time()
        sender = _s("s1", "aaaa1111", "甲")
        t = _s("s2", "bbbb2222", "日报壳", connected=False)
        t.queued = [{"id": "q1", "text": "x", "who": "agent·甲",
                     "images": [], "files": [],
                     "from_conv": "aaaa1111", "parked_ts": now - 60}]
        self._tick([sender, t], now)
        self.assertEqual([], sender.queued)

    def test_reconnect_grace_target_is_left_alone(self):
        now = time.time()
        sender = _s("s1", "aaaa1111", "甲")
        t = _s("s2", "bbbb2222", "日报壳", connected=False,
               recon_deadline=now + 60)  # 宽限内，多半马上自己回来
        t.queued = [{"id": "q1", "text": "x", "who": "agent·甲",
                     "images": [], "files": [],
                     "from_conv": "aaaa1111", "parked_ts": now - 1900}]
        self._tick([sender, t], now)
        self.assertEqual([], sender.queued)


if __name__ == "__main__":
    unittest.main(verbosity=2)
