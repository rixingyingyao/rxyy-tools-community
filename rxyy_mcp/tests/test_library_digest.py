# -*- coding: utf-8 -*-
"""开干后资料补发：报到省 token，开始任务后附规则全文（含 .off 指针）。"""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub


class LibraryDigestFullTextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".cursor" / "rules").mkdir(parents=True)
        (self.root / ".cursor" / "rules" / "00-codebrain-pointers.mdc.off").write_text(
            "# pointers\nadd_memory(group_id=\"rxyy_tools\")\n", encoding="utf-8")
        self.v19 = self.root / "v19.md"
        self.v19.write_text("# v19\n验证四件套\n", encoding="utf-8")
        self.idx = self.root / "index.json"
        self.idx.write_text(json.dumps({
            "user_prompt": {"path": str(self.v19), "desc": "v19"},
            "skills": [{"name": "manage-board", "desc": "看板", "path": "x"}],
            "rules": [],
        }), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_off_pointer_file_is_inlined_after_checkin(self):
        with patch.object(hub.Api, "_library_index_path", return_value=self.idx):
            digest = hub.Api.library_digest(str(self.root))
        self.assertIn("验证四件套", digest)
        self.assertIn("add_memory(group_id=\"rxyy_tools\")", digest)
        self.assertIn("00-codebrain-pointers.mdc.off", digest)
        self.assertIn("【本项目规则 · 全文", digest)
        self.assertIn("manage-board", digest)

    def test_digest_always_requires_listing_the_board(self):
        with patch.object(hub.Api, "_library_index_path", return_value=self.idx):
            digest = hub.Api.library_digest(str(self.root))
        self.assertIn("【开干先看板 · 续接文档】", digest)
        self.assertIn("boardctl.py list", digest)

    def test_digest_requires_live_cursor_title_via_rename_chat(self):
        with patch.object(hub.Api, "_library_index_path", return_value=self.idx):
            digest = hub.Api.library_digest(str(self.root))
        self.assertIn("【Cursor 侧栏标题 · 实时跟随】", digest)
        self.assertIn("rename_chat", digest)
        self.assertIn("cursor-app-control", digest)
        self.assertIn("Reload Window", digest)

    def test_digest_spells_out_the_literal_title_to_rename_to(self):
        """派活时控制台已经知道 tab 叫什么，别让 agent 自己去凑「当前 task_name」。"""
        with patch.object(hub.Api, "_library_index_path", return_value=self.idx):
            digest = hub.Api.library_digest(str(self.root), "控制台·侧栏改名")
        self.assertIn('arguments={"title":"控制台·侧栏改名"}', digest)
        self.assertNotIn("<当前 task_name>", digest)

    def test_digest_warns_subagents_cannot_see_rename_chat(self):
        """08-27 侧栏一屏 Persistent plus 的根因：活全委派给了看不到该工具的子代理。"""
        with patch.object(hub.Api, "_library_index_path", return_value=self.idx):
            digest = hub.Api.library_digest(str(self.root), "控制台·侧栏改名")
        self.assertIn("子代理", digest)
        self.assertIn("主对话", digest)

    def test_workspace_v19_copy_is_not_duplicated(self):
        (self.root / ".cursor" / "rules" / "30-v19-protocol.mdc.off").write_text(
            "DUPLICATE_V19_SHOULD_NOT_APPEAR", encoding="utf-8")
        with patch.object(hub.Api, "_library_index_path", return_value=self.idx):
            digest = hub.Api.library_digest(str(self.root))
        self.assertNotIn("DUPLICATE_V19_SHOULD_NOT_APPEAR", digest)

    def test_start_task_option_attaches_even_without_typed_text(self):
        s = hub.Session.__new__(hub.Session)
        s.lock = threading.Lock()
        s.library_sent = False
        s.cwd = str(self.root)
        old = hub.HUB.cfg.get("library_autosend", True)
        hub.HUB.cfg["library_autosend"] = True
        try:
            with patch.object(hub.Api, "library_digest", return_value="FULL\n"):
                out, claimed = hub.Api()._with_library_digest(
                    s, None, selected=["开始任务"])
        finally:
            hub.HUB.cfg["library_autosend"] = old
        self.assertTrue(claimed)
        self.assertEqual("FULL\n", out)

    def test_end_option_alone_does_not_attach(self):
        s = hub.Session.__new__(hub.Session)
        s.lock = threading.Lock()
        s.library_sent = False
        s.cwd = str(self.root)
        out, claimed = hub.Api()._with_library_digest(s, None, selected=["结束"])
        self.assertFalse(claimed)
        self.assertIsNone(out)
        self.assertFalse(s.library_sent)


class SessionMemoryDigestTests(unittest.TestCase):
    def _session(self):
        s = hub.Session.__new__(hub.Session)
        s.lock = threading.Lock()
        s.library_sent = True
        s.memory_sent_ts = 0
        s.cwd = r"d:\Desktop\cursor工作流"
        s.name = "团队·项目工作区"
        s.conv_key = "8f6423db"
        s.file_path = r"D:\持久plus聊天记录\8f6423db.md"
        s.messages = [
            {"role": "ai", "html": "<div>已就位</div>"},
            {"role": "user", "html": "<div>面板看着对，卡交待验收</div>"},
            {"role": "user", "html": "<div>归属下拉也改跟 cwd</div>"},
        ]
        return s

    def test_digest_points_at_chat_log_and_recent_user_lines(self):
        digest = hub.Api().session_memory_digest(self._session())
        self.assertIn("【会话要点 · 压缩后回灌】", digest)
        self.assertIn("8f6423db", digest)
        self.assertIn(r"D:\持久plus聊天记录\8f6423db.md", digest)
        self.assertIn("归属下拉也改跟 cwd", digest)
        self.assertIn("先 Read 上面这份补记忆", digest)

    def test_digest_keeps_clicked_options_not_just_typed_text(self):
        s = self._session()
        s.file_path = ""
        s.messages = [{
            "role": "user",
            "html": "<div>回灌会不会导致什么问题？不能自动收工</div>",
            "selected": [
                "1 修cwd被抖到live", "2 每次回复都回灌",
                "3 清幽灵tab", "4 席位跟项目走", "6 顶栏减负",
            ],
        }]
        digest = hub.Api().session_memory_digest(s)
        self.assertIn("1 修cwd被抖到live", digest)
        self.assertIn("6 顶栏减负", digest)
        self.assertIn("不能自动收工", digest)
        self.assertIn("硬约束", digest)
        pin_block = digest.split("硬约束", 1)[1].split("用户最近说", 1)[0]
        self.assertNotIn("1 修cwd被抖到live", pin_block)

    def test_digest_prefers_chat_log_selections_over_short_bubbles(self):
        # 气泡只有一句「继续」，md 里才有五项选择——回灌必须读 md
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        md = Path(tmp.name) / "23db.md"
        md.write_text(
            "# 记录\n\n"
            "## 🧑 用户 · 2026-08-21 11:01:21\n\n"
            "选择: 1 修cwd被抖到live、2 每次回复都回灌、3 清幽灵tab、"
            "4 席位跟项目走、6 顶栏减负\n\n"
            "不能自动收工，只有我手动打结束才能彻底结束。\n\n"
            "## 🧑 用户 · 2026-08-21 11:17:07\n\n"
            "继续\n\n李杰宇的任务中转 http://192.168.20.149:39000/ 加进去\n",
            encoding="utf-8",
        )
        s = self._session()
        s.file_path = str(md)
        s.messages = [{"role": "user", "html": "<div>继续</div>"}]
        digest = hub.Api().session_memory_digest(s)
        self.assertIn("1 修cwd被抖到live", digest)
        self.assertIn("192.168.20.149:39000", digest)
        self.assertIn("不能自动收工", digest)
        self.assertIn("硬约束", digest)

    def test_injects_after_library_sent(self):
        s = self._session()
        out = hub.Api()._with_session_memory(s, "继续改")
        self.assertTrue(out.startswith("【会话要点 · 压缩后回灌】"))
        self.assertIn("继续改", out)
        self.assertGreater(s.memory_sent_ts, 0)

    def test_injects_every_user_reply_after_library_sent(self):
        s = self._session()
        s.memory_sent_ts = time.time()
        api = hub.Api()
        out = api._with_session_memory(s, "普通回复")
        self.assertTrue(out.startswith("【会话要点 · 压缩后回灌】"))
        self.assertIn("普通回复", out)
        again = api._with_session_memory(s, "【会话要点 · 压缩后回灌】\n已灌过")
        self.assertEqual("【会话要点 · 压缩后回灌】\n已灌过", again)

    def test_skips_before_library_and_on_end(self):
        s = self._session()
        s.library_sent = False
        api = hub.Api()
        self.assertEqual("开干前", api._with_session_memory(s, "开干前"))
        s.library_sent = True
        self.assertEqual("再见", api._with_session_memory(s, "再见", selected=["结束"]))


if __name__ == "__main__":
    unittest.main()
