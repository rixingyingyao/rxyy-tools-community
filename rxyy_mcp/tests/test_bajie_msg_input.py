# -*- coding: utf-8 -*-
"""消息区 + 输入框对齐 Bajie 0.7.45（晨雾配色）。

前任侧栏两行已经上线。这一刀要把气泡从「左右 92% 小胶囊」收成 Bajie Vue 的全宽
18px 卡片、连续同角色圆角合并，输入区 Enter 发送 / Shift+Enter 换行，hover 工具条
走复制/引用/收藏/编辑/删除。编辑删除只改控制台历史。
"""
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub  # noqa: E402

UI = (MODULE_DIR / "ui.html").read_text(encoding="utf-8")


def _session():
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = "s1", "c1", "消息皮"
    s.cwd = s.task_root = r"C:\example\cursor工作流"
    s.connected = True
    s.queued = []
    s.messages = []
    s.rev = 0
    s.lock = threading.Lock()
    s.draft_text = ""
    s.draft_images = []
    s.draft_files = []
    return s


class UiSkinTests(unittest.TestCase):
    def test_bubbles_are_full_width_bajie_cards(self):
        for anchor in (
            "max-width: 1080px",
            "border-radius: 18px",
            "function markMsgRuns",
            "run-first",
            "run-last",
            "border-left: 2px solid var(--primary)",
        ):
            self.assertIn(anchor, UI, anchor)

    def test_composer_enter_sends_shift_enter_breaks(self):
        self.assertIn('e.key === "Enter" && !e.shiftKey && !e.ctrlKey && !e.altKey', UI)
        self.assertIn("e.isComposing || e.keyCode === 229", UI)
        self.assertIn("Enter 发送 · Shift+Enter 换行", UI)
        self.assertFalse("Ctrl+Enter 发送" in UI)

    def test_hover_tools_and_jump_bottom_exist(self):
        for anchor in (
            "function attachMsgTools",
            'add("复制"',
            'add("引用"',
            "function startMsgEdit",
            'id="jumpBottom"',
            "function syncJumpBottom",
            'id="composerBox"',
            'id="composerTools"',
            "c-round",
        ):
            self.assertIn(anchor, UI, anchor)

    def test_script_parses(self):
        start = UI.index("<script>")
        end = UI.rindex("</script>")
        js = UI[start + 8:end]
        r = subprocess.run(
            ["node", "--check"], input=js.encode("utf-8"),
            capture_output=True, timeout=20)
        self.assertEqual(0, r.returncode, r.stderr.decode("utf-8", "replace"))


class MarkMsgRunsTests(unittest.TestCase):
    def test_consecutive_same_role_merges_corners(self):
        start = UI.index("function markMsgRuns")
        end = UI.index("function msgStarred")
        js = UI[start:end] + r"""
function make(cls) {
  const s = new Set(cls.split(/\s+/).filter(Boolean));
  return {
    classList: {
      contains: (x) => s.has(x),
      add: (...xs) => xs.forEach(x => s.add(x)),
      remove: (...xs) => xs.forEach(x => s.delete(x)),
    },
    get className() { return [...s].join(' '); },
  };
}
const kids = [make('msg user'), make('msg user'), make('msg ai'), make('msg sys'), make('msg ai')];
markMsgRuns({ children: kids });
const cls = kids.map(e => e.className);
if (cls[0] !== 'msg user run-first') throw new Error(cls[0]);
if (cls[1] !== 'msg user run-last') throw new Error(cls[1]);
if (cls[2] !== 'msg ai run-first run-last') throw new Error(cls[2]);
if (cls[3] !== 'msg sys') throw new Error(cls[3]);
if (cls[4] !== 'msg ai run-first run-last') throw new Error(cls[4]);
console.log('ok');
"""
        r = subprocess.run(["node", "-e", js], capture_output=True, timeout=20)
        self.assertEqual(0, r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace"))
        self.assertIn(b"ok", r.stdout)


class EditDeleteApiTests(unittest.TestCase):
    def setUp(self):
        self.s = _session()
        self.s.messages = [
            {"mid": "m1", "role": "user", "text": "旧句", "html": "旧句", "queued": False},
            {"mid": "m2", "role": "ai", "text": "**hi**", "html": "<p><strong>hi</strong></p>"},
            {"mid": "m3", "role": "sys", "text": "系统", "html": "系统"},
            {"mid": "mq", "role": "user", "text": "排队", "html": "排队",
             "queued": True, "qid": "q1"},
        ]
        self.s.queued = [{"id": "q1", "text": "排队"}]
        patches = [
            patch.object(hub.HUB, "sessions", {self.s.id: self.s}),
            patch.object(hub.HUB, "hydrate_messages_from_file", lambda *a, **k: None),
        ]
        for p in patches:
            p.start()
        self.addCleanup(patch.stopall)

    def test_edit_rewrites_html_and_marks_edited(self):
        r = hub.Api().edit_message(self.s.id, "m1", "新句")
        self.assertTrue(r["ok"], r)
        self.assertEqual("新句", self.s.messages[0]["text"])
        self.assertTrue(self.s.messages[0]["edited"])
        self.assertEqual(1, self.s.rev)

    def test_edit_rejects_queued_and_sys_and_blank(self):
        self.assertFalse(hub.Api().edit_message(self.s.id, "mq", "x")["ok"])
        self.assertFalse(hub.Api().edit_message(self.s.id, "m3", "x")["ok"])
        self.assertFalse(hub.Api().edit_message(self.s.id, "m1", "   ")["ok"])
        self.assertFalse(hub.Api().edit_message("nope", "m1", "x")["ok"])

    def test_delete_drops_row_and_queue_entry(self):
        r = hub.Api().delete_message(self.s.id, "mq")
        self.assertTrue(r["ok"], r)
        self.assertEqual(["m1", "m2", "m3"], [m["mid"] for m in self.s.messages])
        self.assertEqual([], self.s.queued)

    def test_get_messages_fills_missing_mid(self):
        self.s.messages.append({"role": "user", "text": "无 id", "html": "无 id"})
        data = hub.Api().get_messages(self.s.id)
        self.assertTrue(data["messages"][-1]["mid"])


if __name__ == "__main__":
    unittest.main()
