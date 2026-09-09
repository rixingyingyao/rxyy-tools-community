# -*- coding: utf-8 -*-
"""事故回归：普通发送原来会立即取走 pending，现有撤回按钮来不及生效。"""
import inspect
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub  # noqa: E402


def _session():
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = "s1", "c1", "撤回窗口"
    s.cwd = s.task_root = r"C:\example\cursor工作流"
    s.connected = True
    s.recon_deadline = None
    s.ide_active_cache = False
    s.pending = {"id": "req1", "message": "下一步？", "options": ["A", "B"]}
    s.detached = False
    s.processing_since = None
    s.buffered_reply = None
    s.last_reply_probe = None
    s.last_reply_ts = 0
    s.library_sent = False
    s.memory_sent_ts = 0
    s.handed_off_to = ""
    s.queued = []
    s.messages = []
    s.msg_seq = 0
    s.machine_seq = 0
    s.real_seq = 0
    s.rev = 0
    s.lock = threading.Lock()
    return s


class SendRecallDelayTests(unittest.TestCase):
    def setUp(self):
        self.s = _session()
        self.sent = []
        self.s.send = self.sent.append
        patches = [
            patch.object(hub.HUB, "sessions", {self.s.id: self.s}),
            patch.object(hub.HUB, "cfg", {
                "max_messages": 200,
                "library_autosend": False,
                "continue_prompt": "请继续",
            }),
            patch.object(hub.HUB, "save_msg_images", return_value=[]),
            patch.object(hub.HUB, "_mark_claimed", MagicMock()),
            patch.object(hub.HUB, "log_user", MagicMock()),
            patch.object(hub.Api, "_auto_label_on_dispatch", MagicMock()),
            patch.object(
                hub.Api, "_with_library_digest",
                lambda api, s, text, selected=None: (text, False)),
        ]
        for p in patches:
            p.start()
        self.timers = []
        test = self

        class CapturingTimer:
            def __init__(self, delay, fn):
                self.delay = delay
                self.fn = fn
                self.started = False
                test.timers.append(self)

            def start(self):
                self.started = True

            def cancel(self):
                self.cancelled = True

        patch("hub_api.threading.Timer", CapturingTimer).start()
        self.addCleanup(patch.stopall)

    def _recallable(self, text="先做 A", selected=None, images=None, files=None):
        return hub.Api().send_reply(
            self.s.id, text, selected or [], images or [], False,
            files=files or [], recallable=True)

    def _mature(self, entry):
        entry["defer_until"] = time.time() - 0.1

    def test_waiting_reply_stays_pending_during_the_five_second_window(self):
        pending = self.s.pending
        r = self._recallable(selected=["A"])
        self.assertTrue(r["ok"], r)
        self.assertIn("qid", r)
        self.assertIs(pending, self.s.pending)
        self.assertEqual([], self.sent, "缓冲期内不得碰 socket")
        self.assertEqual(1, len(self.s.queued))
        entry = self.s.queued[0]
        self.assertGreater(entry["defer_until"], time.time())
        self.assertEqual("req1", entry["reply_to"])
        self.assertEqual(["A"], entry["selected"])
        self.assertFalse(hub.HUB._flush_queue(self.s))
        self.assertEqual([], self.sent)

    def test_matured_message_is_sent_with_its_selected_options(self):
        self._recallable(selected=["A"])
        self._mature(self.s.queued[0])
        self.assertTrue(hub.HUB._flush_queue(self.s))
        self.assertEqual(1, len(self.sent))
        self.assertEqual("req1", self.sent[0]["id"])
        self.assertEqual(["A"], self.sent[0]["selected_options"])
        self.assertIsNone(self.s.pending)
        self.assertEqual([], self.s.queued)

    def test_only_mature_entries_leave_the_queue(self):
        self._recallable("第一条", ["A"])
        self._recallable("第二条", ["B"])
        first, second = self.s.queued
        self._mature(first)
        self.assertTrue(hub.HUB._flush_queue(self.s))
        self.assertEqual("第一条", self.sent[0]["user_input"])
        self.assertEqual(["A"], self.sent[0]["selected_options"])
        self.assertEqual([second["id"]], [e["id"] for e in self.s.queued])
        self.assertTrue(second["msg"]["queued"])

    def test_recall_restores_text_attachments_and_options_and_never_sends(self):
        image = {"data": "data:image/png;base64,QUJD", "filename": "图.png"}
        file = {"name": "说明.txt", "data": "data:text/plain;base64,SEk="}
        r = self._recallable("撤回我", ["A"], [image], [file])
        out = hub.Api().unqueue_message(self.s.id, r["qid"])
        self.assertTrue(out["ok"], out)
        restore = out["restore"]
        self.assertEqual("撤回我", restore["text"])
        self.assertEqual(["A"], restore["selected"])
        self.assertEqual("图.png", restore["images"][0]["filename"])
        self.assertEqual("说明.txt", restore["files"][0]["name"])
        self.assertEqual([], self.s.queued)
        self.assertFalse(hub.HUB._flush_queue(self.s))
        self.assertEqual([], self.sent)

    def test_send_failure_puts_back_both_queue_and_pending(self):
        self._recallable("断线也别丢")
        entry = self.s.queued[0]
        self._mature(entry)
        pending = self.s.pending
        self.s.send = lambda _obj: (_ for _ in ()).throw(OSError("断线"))
        self.assertFalse(hub.HUB._flush_queue(self.s))
        self.assertIs(pending, self.s.pending)
        self.assertEqual([entry["id"]], [e["id"] for e in self.s.queued])
        self.assertIsNone(self.s.processing_since)
        self.assertTrue(entry["msg"]["queued"])

    def test_old_queue_cannot_answer_a_card_and_typed_reply_to_cannot_either(self):
        # 09-07：卡还挂着时，5 秒缓冲的发送条不能把卡秒关；答卡走 answer_card，
        # 排队文字答卡时再捎带。
        self.s.pending = {"id": "card2", "message": "选哪个？", "card": {"id": "c2"}}
        old = {
            "id": "old", "text": "卡出来之前说的", "selected": [],
            "images": [], "files": [], "who": None, "defer_until": 0,
            "msg": {"queued": True, "qid": "old"},
        }
        self.s.queued.append(old)
        self.assertFalse(hub.HUB._flush_queue(self.s))
        self.assertEqual([], self.sent)

        r = self._recallable("就选 A", ["A"])
        matching = next(e for e in self.s.queued if e["id"] == r["qid"])
        self._mature(matching)
        self.assertFalse(hub.HUB._flush_queue(self.s),
                         "修前：reply_to 对上就把决策卡当普通提问答掉")
        self.assertEqual([], self.sent)
        self.assertIsNotNone(self.s.pending.get("card"))
        self.assertEqual(2, len(self.s.queued))

    def test_recallable_send_schedules_a_flush_timer_that_delivers(self):
        r = self._recallable("先做 A", ["A"])
        self.assertTrue(r["ok"], r)
        self.assertEqual(1, len(self.timers))
        self.assertGreaterEqual(self.timers[0].delay, 5.0)
        self.assertTrue(self.timers[0].started)
        self.assertEqual([], self.sent)
        self._mature(self.s.queued[0])
        self.timers[0].fn()
        self.assertEqual(1, len(self.sent))
        self.assertEqual("先做 A", self.sent[0]["user_input"])
        self.assertEqual(["A"], self.sent[0]["selected_options"])
        self.assertIsNone(self.s.pending)
        self.assertEqual([], self.s.queued)
        self.assertFalse(self.s.messages[0]["queued"])

    def test_continue_takes_stranded_user_send_along(self):
        """09-04 事故：发送还压在缓冲里，点「继续」抢走 pending，发送条永远排队。"""
        self._recallable("我点的发送", ["重启控制台吃模型API"])
        self._mature(self.s.queued[0])
        r = hub.Api().send_reply(self.s.id, "", [], [], True)
        self.assertTrue(r["ok"], r)
        self.assertEqual("req1", self.sent[-1]["id"])
        self.assertIn("我点的发送", self.sent[-1]["user_input"])
        self.assertIn("请继续", self.sent[-1]["user_input"])
        self.assertEqual(["重启控制台吃模型API"], self.sent[-1]["selected_options"])
        self.assertEqual([], self.s.queued)
        self.assertFalse(self.s.messages[0]["queued"])

    def test_continue_during_recall_window_still_takes_the_send(self):
        self._recallable("不要排队", ["继续对齐其它Bajie项"])
        self.assertGreater(self.s.queued[0]["defer_until"], time.time())
        r = hub.Api().send_reply(self.s.id, "", [], [], True)
        self.assertTrue(r["ok"], r)
        self.assertIn("不要排队", self.sent[-1]["user_input"])
        self.assertEqual(["继续对齐其它Bajie项"], self.sent[-1]["selected_options"])
        self.assertEqual([], self.s.queued)
        self.assertFalse(self.s.messages[0]["queued"])

    def test_old_callers_and_continue_keep_immediate_delivery(self):
        r = hub.Api().send_reply(self.s.id, "旧调用", [], [], False)
        self.assertTrue(r["ok"], r)
        self.assertEqual("旧调用", self.sent[-1]["user_input"])
        self.assertEqual([], self.s.queued)

        self.s.pending = {"id": "req2", "message": "继续吗？"}
        r = hub.Api().send_reply(
            self.s.id, "", [], [], True, recallable=True)
        self.assertTrue(r["ok"], r)
        self.assertEqual("req2", self.sent[-1]["id"])
        self.assertEqual("请继续", self.sent[-1]["user_input"])
        self.assertEqual([], self.s.queued)

    def test_state_tick_contains_the_automatic_flush_path(self):
        source = inspect.getsource(hub.Hub._state_tick)
        self.assertIn("self._flush_queue(s)", source)


class FrontendRecallContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ui = (MODULE_DIR / "ui.html").read_text(encoding="utf-8")
        cls.share = (MODULE_DIR / "share.html").read_text(encoding="utf-8")
        cls.server = (MODULE_DIR / "share_server.py").read_text(encoding="utf-8")

    def test_desktop_normal_send_and_quick_phrase_request_recall(self):
        self.assertIn("payloadFiles, !isContinue", self.ui)
        self.assertIn("false, null, [], true", self.ui)
        self.assertIn("已发送 · 撤回 ", self.ui)
        self.assertIn("已发送 · AI 下次提问时送达", self.ui)
        self.assertIn("正在交给 AI…", self.ui)
        self.assertNotIn("排队中 · AI 就绪后自动发送", self.ui)

    def test_phone_reply_and_queue_forward_recall_and_keep_the_qid(self):
        self.assertGreaterEqual(self.share.count("recallable: true"), 3)
        self.assertIn("if (r && r.qid) myQids.add(r.qid);", self.share)
        self.assertIn('recallable=bool(payload.get("recallable"))', self.server)
        self.assertIn("已发送 · 撤回 ", self.share)
        self.assertIn("已发送 · AI 下次提问时送达", self.share)
        self.assertIn("正在交给 AI…", self.share)
        self.assertNotIn("排队中 · AI 就绪后自动发送", self.share)


if __name__ == "__main__":
    unittest.main()
