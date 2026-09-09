# -*- coding: utf-8 -*-
"""派活 agent 调 ji(action=完成任务) 按编号标已处理；发给走安排站再飞鸽。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
CONSOLE_DIR = MODULE_DIR.parent / "console"
sys.path.insert(0, str(MODULE_DIR))
sys.path.insert(0, str(CONSOLE_DIR))

import server  # noqa: E402
from api.taskstage.contacts import ContactBook  # noqa: E402
from api.taskstage.storage import TaskStageStorage  # noqa: E402


class JiCompleteTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = TaskStageStorage(Path(self.tmp.name))
        self.task = self.storage.add_task({"title": "修延迟"})
        self.storage.update_task(self.task["id"], {"status": "dispatched"})
        self._s = patch("share_server._task_storage", lambda: self.storage)
        self._s.start()
        self.addCleanup(self._s.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ji_marks_done_by_the_id_in_content(self):
        out = server.tool_ji({
            "action": "完成任务",
            "content": self.task["id"],
            "conversation_id": "someone-elses-tab",
        })
        self.assertIn("已处理", out[0]["text"])
        self.assertEqual("done", self.storage.find(self.task["id"])["status"])

    def test_missing_id_does_not_touch_the_store(self):
        out = server.tool_ji({"action": "完成任务", "conversation_id": "0d07cce5"})
        self.assertIn("编号", out[0]["text"])
        self.assertEqual("dispatched", self.storage.find(self.task["id"])["status"])

    def test_brief_mentions_complete_task_and_deliver(self):
        self.assertIn("完成任务", server.MCP_INSTRUCTIONS_BRIEF)
        self.assertIn("发给", server.MCP_INSTRUCTIONS_BRIEF)
        desc = [t for t in server.TOOLS if t["name"] == "ji"][0]["description"]
        self.assertIn("完成任务", desc)
        self.assertIn("发给", desc)


class ConsolePathTests(unittest.TestCase):
    def test_live_layout_uses_recorded_console_root(self):
        fake_live = Path(tempfile.mkdtemp()) / "rxyy_mcp"
        fake_live.mkdir(parents=True)
        repo = MODULE_DIR.parent
        console = str(repo / "console")
        saved = list(sys.path)
        try:
            while console in sys.path:
                sys.path.remove(console)
            with patch.object(server, "APP_DIR", fake_live):
                with patch("share_server._console_root", lambda: repo):
                    server._ensure_console_on_path()
            self.assertEqual(console, sys.path[0])
        finally:
            sys.path[:] = saved


class JiDeliverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = TaskStageStorage(Path(self.tmp.name))
        self.book = ContactBook(Path(self.tmp.name))
        self.book.save({"name": "肖宇轩", "url": "https://xyx.example/?k=tok"})
        self._s = patch("share_server._task_storage", lambda: self.storage)
        self._s.start()
        self.addCleanup(self._s.stop)
        self._b = patch.object(server, "_task_contact_book", lambda: self.book)
        self._b.start()
        self.addCleanup(self._b.stop)
        self.feige = []

        def fake_feige(name, text):
            self.feige.append((name, text))
            return {"ok": True}

        self._f = patch.object(server, "_feige_send", fake_feige)
        self._f.start()
        self.addCleanup(self._f.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_inbox_is_tried_before_feige(self):
        with patch("api.taskstage.contacts.send_inbox",
                   return_value={"saved": 0, "dir": ""}) as inbox:
            out = server.tool_ji({
                "action": "发给",
                "category": "肖宇轩",
                "content": "转播延时按这个改",
            })
        self.assertIn("任务安排站", out[0]["text"])
        inbox.assert_called_once()
        self.assertEqual([], self.feige)

    def test_feige_is_the_fallback_when_inbox_is_down(self):
        with patch("api.taskstage.contacts.send_inbox",
                   side_effect=ConnectionError("连不上对方的投递站")):
            out = server.tool_ji({
                "action": "发给",
                "category": "肖宇轩",
                "content": "转播延时按这个改",
            })
        self.assertIn("飞鸽", out[0]["text"])
        self.assertEqual([("肖宇轩", "转播延时按这个改")], self.feige)


if __name__ == "__main__":
    unittest.main()
