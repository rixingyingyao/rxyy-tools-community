# -*- coding: utf-8 -*-
"""任务安排站沙箱全流程：真 HTTP 投递站替身 + 临时任务库。

不碰现网肖宇轩、不发真飞鸽。覆盖：
- 正常：派发 → 按编号完成 → 发给打进安排站
- 不正常：没编号、按会话猜、投递站挂、飞鸽也挂、人名/正文空
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
from api.taskstage.core import build_task_prompt  # noqa: E402
from api.taskstage.service import TaskStageService  # noqa: E402
from api.taskstage.storage import TaskStageStorage  # noqa: E402


class _Inbox(BaseHTTPRequestHandler):
    """同事投递站替身。mode: ok / http500 / http401 / reject。"""

    received = []
    mode = "ok"
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8")
        body = json.loads(raw) if raw else {}
        _Inbox.received.append({"path": self.path, "body": body})
        if _Inbox.mode == "http500":
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if _Inbox.mode == "http401":
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if _Inbox.mode == "reject":
            payload = json.dumps({"ok": False, "error": "口令不对"}).encode("utf-8")
        else:
            payload = json.dumps({
                "ok": True, "saved": 0, "dir": "sandbox",
            }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class FakeHub:
    def __init__(self):
        self.sessions = [{
            "id": "s1", "name": "沙箱·联调", "cwd": r"D:\work\repo-a",
            "connected": True, "pending": False, "queued": 0,
            "conv_key": "tab-that-will-take-over",
        }]
        self.sent = []

    def state(self):
        return {"sessions": self.sessions}

    def send(self, sid, text, images=None, files=None):
        self.sent.append({"sid": sid, "text": text,
                          "images": images or [], "files": files or []})
        return {"ok": True, "qid": "q1"}


class _Sandbox(unittest.TestCase):
    """每条用例独立临时库；投递站进程级共用，按用例切 mode。"""

    @classmethod
    def setUpClass(cls):
        cls.httpd = HTTPServer(("127.0.0.1", 0), _Inbox)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.httpd.server_port
        cls.inbox_url = "http://127.0.0.1:{}/".format(cls.port)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        _Inbox.received = []
        _Inbox.mode = "ok"
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.storage = TaskStageStorage(root)
        self.book = ContactBook(root)
        self.book.save({"name": "肖宇轩", "url": self.inbox_url, "note": "沙箱"})
        self.hub = FakeHub()
        self.service = TaskStageService(
            host=None, storage=self.storage, hub=self.hub, contacts=self.book)
        self.feige = []
        self.feige_ok = True
        self.feige_error = "飞鸽没发出去"

        def fake_feige(name, text):
            self.feige.append((name, text))
            if self.feige_ok:
                return {"ok": True}
            return {"ok": False, "error": self.feige_error}

        self._patches = [
            patch("share_server._task_storage", lambda: self.storage),
            patch.object(server, "_task_contact_book", lambda: self.book),
            patch.object(server, "_feige_send", fake_feige),
        ]
        for item in self._patches:
            item.start()
            self.addCleanup(item.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def _ji(self, **kwargs):
        return server.tool_ji(kwargs)[0]["text"]


class HappyFullFlowTests(_Sandbox):
    def test_dispatch_complete_by_id_then_inbox_not_feige(self):
        task = self.storage.add_task({
            "title": "转播延时",
            "description": "按这个改",
            "repo": r"D:\work\repo-a",
            "requester": "肖宇轩",
        })
        reply = self.service.dispatch(task["id"], "s1")
        self.assertTrue(reply["ok"], reply)
        prompt = self.hub.sent[0]["text"]
        self.assertIn('ji(action="完成任务", content="%s")' % task["id"], prompt)
        self.assertIn("不要按会话猜", prompt)
        self.assertNotIn('ji(action="发给"', prompt, "派发本身不授权向需求人发送消息")
        self.assertEqual("dispatched", self.storage.find(task["id"])["status"])
        self.assertEqual("tab-that-will-take-over",
                         self.storage.find(task["id"])["dispatch_conv_key"])

        text = self._ji(
            action="完成任务",
            content=task["id"],
            conversation_id="someone-elses-tab",
            task_name="别的窗口在干别的",
        )
        self.assertIn("已处理", text)
        self.assertEqual("done", self.storage.find(task["id"])["status"])

        text = self._ji(
            action="发给",
            category="肖宇轩",
            content="转播延时已经改完",
            conversation_id="someone-elses-tab",
        )
        self.assertIn("任务安排站", text)
        self.assertNotIn("飞鸽", text)
        self.assertEqual([], self.feige)
        self.assertEqual(1, len(_Inbox.received))
        sent = _Inbox.received[0]
        self.assertEqual("/api/inbox", sent["path"], "局域网无口令，路径不带 ?k=")
        self.assertEqual("转播延时已经改完", sent["body"]["content"])
        self.assertTrue(self.book.find_by_name("肖宇轩")["last_sent_at"])
        self.assertEqual("", self.book.find_by_name("肖宇轩")["last_error"])

    def test_prompt_id_is_what_complete_accepts(self):
        task = self.storage.add_task({"title": "修延迟", "requester": "肖宇轩"})
        self.storage.update_task(task["id"], {"status": "dispatched"})
        prompt = build_task_prompt(self.storage.find(task["id"]), "", "")
        self.assertIn(task["id"], prompt)
        text = self._ji(action="完成任务", content=task["id"])
        self.assertIn("已处理", text)


class UnhappyFullFlowTests(_Sandbox):
    def test_complete_without_id_does_not_touch_store_or_guess_session(self):
        mine = self.storage.add_task({"title": "修延迟"})
        other = self.storage.add_task({"title": "别人的活"})
        self.storage.update_task(mine["id"], {
            "status": "dispatched",
            "dispatch_conv_key": "0d07cce5",
        })
        self.storage.update_task(other["id"], {
            "status": "dispatched",
            "dispatch_conv_key": "deadbeef",
        })
        text = self._ji(action="完成任务", conversation_id="0d07cce5")
        self.assertIn("编号", text)
        self.assertEqual("dispatched", self.storage.find(mine["id"])["status"])
        self.assertEqual("dispatched", self.storage.find(other["id"])["status"])

    def test_complete_with_session_key_does_not_match_conv(self):
        task = self.storage.add_task({"title": "修延迟"})
        self.storage.update_task(task["id"], {
            "status": "dispatched",
            "dispatch_conv_key": "0d07cce5",
        })
        text = self._ji(action="完成任务", content="0d07cce5")
        self.assertIn("找不到", text)
        self.assertEqual("dispatched", self.storage.find(task["id"])["status"])

    def test_complete_wrong_id_leaves_the_real_one(self):
        task = self.storage.add_task({"title": "修延迟"})
        self.storage.update_task(task["id"], {"status": "dispatched"})
        text = self._ji(action="完成任务", content="ffffffffffffffffffffffffffffffff")
        self.assertIn("找不到", text)
        self.assertEqual("dispatched", self.storage.find(task["id"])["status"])

    def test_complete_draft_is_refused(self):
        task = self.storage.add_task({"title": "还没派"})
        text = self._ji(action="完成任务", content=task["id"])
        self.assertIn("处理中", text)
        self.assertEqual("draft", self.storage.find(task["id"])["status"])

    def test_complete_already_done_is_idempotent(self):
        task = self.storage.add_task({"title": "修延迟"})
        self.storage.update_task(task["id"], {"status": "dispatched"})
        self.storage.update_task(task["id"], {"status": "done"})
        text = self._ji(action="完成任务", content=task["id"])
        self.assertIn("已经是已处理", text)
        self.assertEqual("done", self.storage.find(task["id"])["status"])

    def test_ambiguous_title_does_not_pick_one(self):
        a = self.storage.add_task({"title": "修延迟"})
        b = self.storage.add_task({"title": "修延迟"})
        self.storage.update_task(a["id"], {"status": "dispatched"})
        self.storage.update_task(b["id"], {"status": "dispatched"})
        text = self._ji(action="完成任务", content="修延迟")
        self.assertIn("找不到", text)
        self.assertEqual("dispatched", self.storage.find(a["id"])["status"])
        self.assertEqual("dispatched", self.storage.find(b["id"])["status"])

    def test_substring_title_also_refuses_when_two_match(self):
        a = self.storage.add_task({"title": "A延迟"})
        b = self.storage.add_task({"title": "B延迟"})
        self.storage.update_task(a["id"], {"status": "dispatched"})
        self.storage.update_task(b["id"], {"status": "dispatched"})
        text = self._ji(action="完成任务", content="延迟")
        self.assertIn("找不到", text)
        self.assertEqual("dispatched", self.storage.find(a["id"])["status"])
        self.assertEqual("dispatched", self.storage.find(b["id"])["status"])

    def test_deliver_without_name(self):
        text = self._ji(action="发给", content="在吗")
        self.assertIn("发给谁", text)
        self.assertEqual([], _Inbox.received)
        self.assertEqual([], self.feige)

    def test_deliver_without_body(self):
        text = self._ji(action="发给", category="肖宇轩", content="  ")
        self.assertIn("内容", text)
        self.assertEqual([], _Inbox.received)
        self.assertEqual([], self.feige)

    def test_inbox_500_falls_back_to_feige(self):
        _Inbox.mode = "http500"
        text = self._ji(action="发给", category="肖宇轩", content="在吗")
        self.assertIn("飞鸽", text)
        self.assertEqual([("肖宇轩", "在吗")], self.feige)
        self.assertTrue(self.book.find_by_name("肖宇轩")["last_error"])

    def test_inbox_401_falls_back_to_feige(self):
        _Inbox.mode = "http401"
        text = self._ji(action="发给", category="肖宇轩", content="在吗")
        self.assertIn("飞鸽", text)
        self.assertEqual([("肖宇轩", "在吗")], self.feige)
        self.assertIn("口令", self.book.find_by_name("肖宇轩")["last_error"])

    def test_inbox_reject_falls_back_to_feige(self):
        _Inbox.mode = "reject"
        text = self._ji(action="发给", category="肖宇轩", content="在吗")
        self.assertIn("飞鸽", text)
        self.assertEqual([("肖宇轩", "在吗")], self.feige)

    def test_inbox_and_feige_both_fail(self):
        _Inbox.mode = "http500"
        self.feige_ok = False
        text = self._ji(action="发给", category="肖宇轩", content="在吗")
        self.assertIn("任务安排站", text)
        self.assertIn("飞鸽", text)
        self.assertNotIn("✓", text)
        self.assertEqual([("肖宇轩", "在吗")], self.feige)

    def test_unknown_person_goes_to_feige(self):
        text = self._ji(action="发给", category="路人甲", content="在吗")
        self.assertIn("飞鸽", text)
        self.assertEqual([("路人甲", "在吗")], self.feige)
        self.assertEqual([], _Inbox.received)

    def test_name_without_url_goes_to_feige(self):
        self.book.save({"name": "只飞鸽"})
        text = self._ji(action="发给", category="只飞鸽", content="在吗")
        self.assertIn("飞鸽", text)
        self.assertEqual([("只飞鸽", "在吗")], self.feige)
        self.assertEqual([], _Inbox.received)

    def test_dead_port_falls_back_to_feige_with_lan_wording(self):
        import api.taskstage.contacts as ct
        dead = self.book.save({
            "name": "死端口", "url": "http://127.0.0.1:1/",
        })
        with patch.object(ct, "SEND_TIMEOUT", 1.0):
            text = self._ji(action="发给", category="死端口", content="在吗")
        self.assertIn("飞鸽", text)
        self.assertEqual([("死端口", "在吗")], self.feige)
        err = self.book.find(dead["id"])["last_error"]
        self.assertIn("局域网", err)


if __name__ == "__main__":
    unittest.main()
