# -*- coding: utf-8 -*-
"""事故回归：控制台消息操作条要对齐 Bajie 的编辑/删除。

排队中的条不能走编辑（会跟 5 秒撤回抢同一条）；删除排队条要连队列一起拿掉，
不能只把气泡藏了、到期还冲进 zhi。
"""
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub  # noqa: E402


def _session():
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = "s1", "c1", "消息操作"
    s.cwd = s.task_root = r"C:\example\cursor工作流"
    s.connected = True
    s.pending = None
    s.queued = []
    s.messages = []
    s.msg_seq = s.machine_seq = s.real_seq = s.rev = 0
    s.lock = threading.Lock()
    s.draft_text = ""
    s.draft_images = []
    s.draft_files = []
    s.file_path = ""
    return s


class EditDeleteMessageTests(unittest.TestCase):
    def setUp(self):
        self.s = _session()
        patch.object(hub.HUB, "sessions", {self.s.id: self.s}).start()
        patch.object(hub.HUB, "cfg", {"max_messages": 200}).start()
        self.addCleanup(patch.stopall)

    def test_add_message_stamps_a_stable_mid(self):
        hub.HUB.add_message(self.s, {"role": "user", "html": "<p>hi</p>"})
        self.assertTrue(self.s.messages[0]["mid"])
        self.assertEqual(12, len(self.s.messages[0]["mid"]))

    def test_get_messages_backfills_mid_on_old_bubbles(self):
        self.s.messages.append({"role": "ai", "html": "<p>旧</p>"})
        out = hub.Api().get_messages(self.s.id)
        self.assertTrue(out["messages"][0]["mid"])
        self.assertEqual(out["messages"][0]["mid"], self.s.messages[0]["mid"])

    def test_edit_rewrites_html_and_marks_edited_without_touching_socket(self):
        hub.HUB.add_message(self.s, {
            "role": "user", "text": "原句", "html": "<pre class='plain'>原句</pre>",
        })
        mid = self.s.messages[0]["mid"]
        rev = self.s.rev
        r = hub.Api().edit_message(self.s.id, mid, "改过了")
        self.assertTrue(r["ok"], r)
        self.assertTrue(self.s.messages[0]["edited"])
        self.assertEqual("改过了", self.s.messages[0]["text"])
        self.assertIn("改过了", self.s.messages[0]["html"])
        self.assertNotIn("原句", self.s.messages[0]["html"])
        self.assertGreater(self.s.rev, rev)

    def test_edit_rejects_queued_live_bubble(self):
        hub.HUB.add_message(self.s, {
            "role": "user", "html": "<p>排队</p>", "queued": True, "qid": "q1",
        })
        mid = self.s.messages[0]["mid"]
        r = hub.Api().edit_message(self.s.id, mid, "改")
        self.assertFalse(r["ok"])
        self.assertIn("撤回", r["error"])

    def test_delete_drops_queued_entry_so_flush_cannot_deliver_it(self):
        hub.HUB.add_message(self.s, {
            "role": "user", "html": "<p>删我</p>", "queued": True, "qid": "q9",
        })
        self.s.queued.append({"id": "q9", "text": "删我", "msg": self.s.messages[0]})
        mid = self.s.messages[0]["mid"]
        r = hub.Api().delete_message(self.s.id, mid)
        self.assertTrue(r["ok"], r)
        self.assertEqual([], self.s.messages)
        self.assertEqual([], self.s.queued)

    def test_delete_missing_mid_does_not_wipe_the_thread(self):
        hub.HUB.add_message(self.s, {"role": "user", "html": "<p>留着</p>"})
        r = hub.Api().delete_message(self.s.id, "no-such")
        self.assertFalse(r["ok"])
        self.assertEqual(1, len(self.s.messages))
