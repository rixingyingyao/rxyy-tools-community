# -*- coding: utf-8 -*-
"""发件底账：agent 经「发给」送出去的每一笔在自家任务库留档。

用户 08-27 的原话：「要能看到派发出去给别人的任务有哪些，然后要找个机制来
确认其完成否」。此前 ji(action="发给") 只在名录里刷个 last_sent_at，发出去
就断线。现在：

- 派单样内容 → 任务安排站「处理中」挂卡（session_name=同事·谁，飞鸽标注），
  回执消息里带底账编号，对方确认完成后 ji(action="完成任务", content=编号)
  销账，或用户在站里一键「标记为已处理」；
- 回执样内容（✓/已完成…开头）→ 直接落「已处理」留档，不攒永远不会关的卡；
- 底账记不上（任务库不可用/写失败）绝不拦投递——消息本身已经送达。
"""
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
CONSOLE_DIR = MODULE_DIR.parent / "console"
sys.path.insert(0, str(MODULE_DIR))
sys.path.insert(0, str(CONSOLE_DIR))

import server  # noqa: E402
from api.taskstage.contacts import ContactBook  # noqa: E402
from api.taskstage.storage import TaskStageStorage  # noqa: E402


class OutboundKindTests(unittest.TestCase):
    def test_task_like_content_is_a_task(self):
        for text in ("帮忙看看转播页白屏", "转播延时已经改完",  # 「已经改完」不在行首标记里
                     "麻烦部署一下", "在吗"):
            self.assertEqual(server._outbound_kind(text), "task", text)

    def test_receipt_heads_are_receipts(self):
        for text in ("✓ 已通过", "√ 收到", "【回执】编号 abc 已完成",
                     "已完成：转播延时", "已修复，见提交 1234", "回复：收到"):
            self.assertEqual(server._outbound_kind(text), "receipt", text)

    def test_bias_is_toward_task_when_ambiguous(self):
        # 判成 task 顶多让用户多点一次销账；判成 receipt 会让真派单
        # 从待确认名单里消失——含糊的必须归 task
        self.assertEqual(server._outbound_kind("这事已完成一半，剩下的你来"), "task")


class _Inbox(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    mode = "ok"

    def log_message(self, *_args):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if _Inbox.mode == "http500":
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = json.dumps({"ok": True, "saved": 0, "dir": "x"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _LedgerSandbox(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = HTTPServer(("127.0.0.1", 0), _Inbox)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.inbox_url = "http://127.0.0.1:{}/".format(cls.httpd.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        _Inbox.mode = "ok"
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.storage = TaskStageStorage(root)
        self.book = ContactBook(root)
        self.book.save({"name": "阿龟", "url": self.inbox_url})
        self.feige = []
        for item in (
            patch("share_server._task_storage", lambda: self.storage),
            patch.object(server, "_task_contact_book", lambda: self.book),
            patch.object(server, "_feige_send",
                         lambda name, text: (self.feige.append((name, text)),
                                             {"ok": True})[1]),
        ):
            item.start()
            self.addCleanup(item.stop)

    def _ji(self, **kwargs):
        return server.tool_ji(kwargs)[0]["text"]

    def _ledger(self):
        return [t for t in self.storage.list_tasks()
                if str(t.get("requester")) == "发件底账"]


class DispatchLeavesOpenCardTests(_LedgerSandbox):
    def test_full_loop_dispatch_then_settle_by_id(self):
        text = self._ji(action="发给", category="阿龟", content="帮忙查一下转播页白屏")
        self.assertIn("✓", text)
        cards = self._ledger()
        self.assertEqual(1, len(cards), "派出去的活必须在自家任务库挂卡")
        card = cards[0]
        self.assertEqual("dispatched", card["status"], "派单样内容落「处理中」等确认")
        self.assertEqual("发给阿龟：帮忙查一下转播页白屏", card["title"])
        self.assertEqual("同事·阿龟", card["session_name"])
        self.assertIn(card["id"], text, "回执消息必须带底账编号，agent 才有销账凭据")
        self.assertIn("完成任务", text)

        settle = self._ji(action="完成任务", content=card["id"])
        self.assertIn("已处理", settle)
        self.assertEqual("done", self.storage.find(card["id"])["status"])

    def test_receipt_is_archived_done_not_left_open(self):
        text = self._ji(action="发给", category="阿龟", content="✓ 转播延时已改完并部署")
        self.assertIn("回执已留档", text)
        cards = self._ledger()
        self.assertEqual(1, len(cards))
        self.assertEqual("done", cards[0]["status"],
                         "回执落「已处理」留档，不在处理中攒永远不会关的卡")

    def test_feige_fallback_is_visible_on_the_card(self):
        _Inbox.mode = "http500"
        text = self._ji(action="发给", category="阿龟", content="帮忙重启下转播服务")
        self.assertIn("飞鸽", text)
        cards = self._ledger()
        self.assertEqual(1, len(cards))
        self.assertEqual("同事·阿龟（飞鸽）", cards[0]["session_name"],
                         "走了备用通道要标在卡上，回头对账看得出来")
        self.assertEqual("dispatched", cards[0]["status"])

    def test_ledger_failure_never_blocks_delivery(self):
        with patch("share_server._task_storage", lambda: None):
            text = self._ji(action="发给", category="阿龟", content="帮忙看看")
        self.assertIn("✓", text, "任务库不可用时消息照发，只是没有底账尾巴")
        self.assertNotIn("发件底账", text)

    def test_failed_delivery_records_nothing(self):
        _Inbox.mode = "http500"
        with patch.object(server, "_feige_send",
                          lambda name, text: {"ok": False, "error": "飞鸽也挂了"}):
            text = self._ji(action="发给", category="阿龟", content="帮忙看看")
        self.assertNotIn("✓", text)
        self.assertEqual([], self._ledger(), "没送到的不许记账——账上有=对方收到了")


if __name__ == "__main__":
    unittest.main()
