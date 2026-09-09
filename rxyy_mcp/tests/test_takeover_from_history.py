# -*- coding: utf-8 -*-
"""误关标签后从聊天记录 .md 找回会话（parse_history_md + get_takeover_prompt_from_file）"""
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub


SAMPLE = """# rxyy MCP 会话记录

- 会话: 待命·cursor工作流1 (ebc40cf8)
- 工作目录: d:\\Desktop\\cursor工作流
- 开始时间: 2026-07-30 16:48:22

---

## 🤖 AI · 2026-07-30 16:48:22

📍 cursor工作流（d:\\Desktop\\cursor工作流）· 对话 7c6a4e74 已就位

（提供选项: 开始任务 | 结束）

## 🧑 用户 · 2026-07-30 17:53:03

立即调用rxyy MCP的zhi报到：conversation_id=「dec3efdb」全程沿用

## 🤖 AI · 2026-07-30 18:00:11

雷神那条 8.0.0.0/5 路由把 OA 流量吸进隧道了

## 🧑 李小雨（局域网） · 2026-07-30 18:53:06

选择: OA 先放着，接着做日报/周报/团队
"""


def _write_sample(dirpath, text=SAMPLE, name="待命·cursor工作流1 (10).md"):
    p = Path(dirpath) / name
    p.write_text(text, encoding="utf-8")
    return p


class ParseHistoryMdTests(unittest.TestCase):
    def test_prefers_reported_id_over_the_one_user_pasted(self):
        # 用户粘的 dec3efdb 从未生效，认它就会把找回引到一个不存在的对话上
        self.assertEqual("7c6a4e74", hub.parse_history_md(SAMPLE)["conv"])

    def test_header_id_wins(self):
        text = SAMPLE.replace("- 会话: 待命·cursor工作流1 (ebc40cf8)",
                              "- 会话: 待命·cursor工作流1 (ebc40cf8)\n- 对话ID: aabbccdd")
        self.assertEqual("aabbccdd", hub.parse_history_md(text)["conv"])

    def test_reads_header_fields_without_internal_session_id(self):
        meta = hub.parse_history_md(SAMPLE)
        self.assertEqual("待命·cursor工作流1", meta["name"])
        self.assertEqual(r"d:\Desktop\cursor工作流", meta["cwd"])
        self.assertEqual("2026-07-30 16:48:22", meta["created_at"])

    def test_keeps_lan_replies(self):
        msgs = hub.parse_history_md(SAMPLE)["msgs"]
        self.assertEqual(["ai", "user", "ai", "user"], [m[0] for m in msgs])
        self.assertIn("OA 先放着", msgs[-1][2])
        self.assertNotIn("提供选项", msgs[0][2])

    def test_empty_text_is_survivable(self):
        meta = hub.parse_history_md("")
        self.assertEqual("", meta["conv"])
        self.assertEqual([], meta["msgs"])


class TakeoverFromFileTests(unittest.TestCase):
    def test_builds_prompt_with_original_conversation_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            md = _write_sample(tmp)
            with (patch.dict(hub.HUB.cfg, {"history_dir": tmp}),
                  patch.object(hub.HUB, "sessions", {}),
                  patch.object(hub.Api, "_locate_transcripts", staticmethod(lambda *a, **k: []))):
                r = hub.Api().get_takeover_prompt_from_file(str(md))
        self.assertTrue(r["ok"])
        self.assertEqual("7c6a4e74", r["conversation_id"])
        self.assertEqual("待命·cursor工作流1", r["name"])
        self.assertIn("7c6a4e74", r["prompt"])
        self.assertIn(str(md.resolve()), r["prompt"])
        self.assertIn("OA 先放着", r["prompt"])  # 局域网回复要进摘要

    def test_lists_every_transcript_of_that_conversation(self):
        with tempfile.TemporaryDirectory() as tmp:
            md = _write_sample(tmp)
            found = [r"C:\t\new.jsonl（27KB · 07-30 20:54）",
                     r"C:\t\main.jsonl（271KB · 07-30 19:04）"]
            with (patch.dict(hub.HUB.cfg, {"history_dir": tmp}),
                  patch.object(hub.HUB, "sessions", {}),
                  patch.object(hub.Api, "_locate_transcripts", staticmethod(lambda *a, **k: found))):
                r = hub.Api().get_takeover_prompt_from_file(str(md))
        for line in found:
            self.assertIn(line, r["prompt"])

    def test_rejects_paths_outside_history_dir(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
            md = _write_sample(other)
            with patch.dict(hub.HUB.cfg, {"history_dir": tmp}):
                r = hub.Api().get_takeover_prompt_from_file(str(md))
        self.assertFalse(r["ok"])

    def test_delegates_to_live_session_when_tab_still_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            md = _write_sample(tmp)
            s = hub.Session.__new__(hub.Session)
            s.id, s.conv_key, s.cwd = "tab-1", "7c6a4e74", r"d:\Desktop\cursor工作流"
            s.name, s.file_path, s.messages = "待命·cursor工作流1", str(md), []
            s.lock, s.transcript_path, s.cursor_uuid = threading.Lock(), None, None
            with (patch.dict(hub.HUB.cfg, {"history_dir": tmp}),
                  patch.object(hub.HUB, "sessions", {"tab-1": s}),
                  patch.object(hub, "find_cursor_transcript", return_value=None)):
                r = hub.Api().get_takeover_prompt_from_file(str(md))
        self.assertTrue(r["ok"])
        self.assertNotIn("from_file", r)  # 走的是会话那条路
        self.assertEqual("7c6a4e74", r["conversation_id"])


class SessionTakeoverTranscriptListTests(unittest.TestCase):
    """活会话的接手词也列「内容核对过」的流水清单；内存指针只垫底并注明未核对。

    08-28 f9c313ed 实测：接手词只给了单指针 transcript_path，指向的是报到
    失败空壳留下的 1.6KB 流水、与原对话毫无关系——接手方按它读了个寂寞，
    「新agent不能快速定位上一个agent做了什么」一半就栽在这。_locate_transcripts
    逐份验过内容确实提到这个对话，可信；内存指针没被清单覆盖时才垫底补进。
    """

    def _session(self, tmp, md):
        s = hub.Session.__new__(hub.Session)
        # cwd 用临时目录：不是 git 仓库，「git 现场」块保持静音，断言不受干扰
        s.id, s.conv_key, s.cwd = "tab-1", "7c6a4e74", tmp
        s.name, s.file_path, s.messages = "待命·cursor工作流1", str(md), []
        s.lock, s.transcript_path, s.cursor_uuid = threading.Lock(), None, None
        return s

    def _prompt(self, s, found):
        with (patch.object(hub.HUB, "sessions", {"tab-1": s}),
              patch.object(hub.Api, "_locate_transcripts",
                           staticmethod(lambda *a, **k: list(found)))):
            r = hub.Api().get_takeover_prompt("tab-1")
        self.assertTrue(r["ok"], r)
        return r["prompt"]

    def test_the_memory_pointer_is_labeled_unverified_and_listed_last(self):
        with tempfile.TemporaryDirectory() as tmp:
            md = _write_sample(tmp)
            stale = Path(tmp) / "stale.jsonl"
            stale.write_text("{}", encoding="utf-8")
            s = self._session(tmp, md)
            s.transcript_path = str(stale)
            verified = r"C:\t\main.jsonl（271KB · 07-30 19:04）"
            p = self._prompt(s, [verified])
        self.assertIn(verified, p)
        self.assertIn("内存指针，内容未核对", p)
        self.assertLess(p.index(verified), p.index(str(stale)),
                        "核对过的排前面，指针垫底")

    def test_a_pointer_already_on_the_list_is_not_repeated(self):
        with tempfile.TemporaryDirectory() as tmp:
            md = _write_sample(tmp)
            good = Path(tmp) / "good.jsonl"
            good.write_text("{}", encoding="utf-8")
            s = self._session(tmp, md)
            s.transcript_path = str(good)
            p = self._prompt(s, ["{}（27KB · 07-30 20:54）".format(good)])
        self.assertNotIn("内存指针", p)
        self.assertEqual(1, p.count(str(good)))

    def test_no_verified_hits_still_hands_over_the_pointer(self):
        # 老对话可能早于内容索引窗口：清单空着时，指针再不牢也比没有强
        with tempfile.TemporaryDirectory() as tmp:
            md = _write_sample(tmp)
            only = Path(tmp) / "only.jsonl"
            only.write_text("{}", encoding="utf-8")
            s = self._session(tmp, md)
            s.transcript_path = str(only)
            p = self._prompt(s, [])
        self.assertIn(str(only), p)
        self.assertIn("内存指针，内容未核对", p)


class HydrateTests(unittest.TestCase):
    def _session(self, md, messages):
        s = hub.Session.__new__(hub.Session)
        s.file_path, s.messages, s.lock, s.rev = str(md), messages, threading.Lock(), 1
        return s

    def test_backfilled_bubbles_include_lan_replies(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self._session(_write_sample(tmp), [])
            self.assertTrue(hub.HUB.hydrate_messages_from_file(s))
        self.assertEqual(["ai", "user", "ai", "user"], [m["role"] for m in s.messages])
        self.assertIn("OA 先放着", s.messages[-1]["html"])

    def test_only_prepends_and_never_replaces_rendered_bubbles(self):
        # 内存里的气泡是渲染过的；补历史绝不能把它们换成 .md 里的纯文本
        with tempfile.TemporaryDirectory() as tmp:
            live = [{"role": "sys", "ts": "1", "html": "连接已恢复"},
                    {"role": "ai", "ts": "2", "html": "<div class='md'><b>渲染过的</b></div>"},
                    {"role": "user", "ts": "3", "html": "<div class='md'>回复</div>"}]
            s = self._session(_write_sample(tmp), list(live))
            self.assertTrue(hub.HUB.hydrate_messages_from_file(s))
        # 文件里 4 条对话、内存里已有 2 条 → 只把最早的 2 条补在前面
        self.assertEqual(5, len(s.messages))
        self.assertEqual(live, s.messages[2:])
        self.assertTrue(all(m.get("from_file") for m in s.messages[:2]))
        self.assertIn("已就位", s.messages[0]["html"])

    def test_no_work_when_memory_already_has_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = self._session(_write_sample(tmp),
                              [{"role": "ai", "ts": str(i), "html": "x"} for i in range(4)])
            self.assertFalse(hub.HUB.hydrate_messages_from_file(s))
            self.assertEqual(4, len(s.messages))


if __name__ == "__main__":
    unittest.main()
