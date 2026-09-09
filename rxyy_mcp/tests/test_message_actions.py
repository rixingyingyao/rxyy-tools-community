# -*- coding: utf-8 -*-
"""普通消息操作栏：重发只回填编辑器，不偷偷再次调用发送接口。"""
import subprocess
import unittest
from pathlib import Path


MODULE_DIR = Path(__file__).resolve().parents[1]
UI_PATH = MODULE_DIR / "ui.html"
SHARE_PATH = MODULE_DIR / "share.html"


FAKE_DOM = r"""
class El {
  constructor(tag) {
    this.tag = tag; this.children = []; this._text = "";
    this.className = ""; this.title = ""; this.type = "";
    this.bubble = null;
  }
  appendChild(x) { this.children.push(x); return x; }
  append(...xs) { xs.forEach(x => this.children.push(x)); }
  querySelector(sel) {
    if (sel === ".bubble") return this.bubble;
    return null;
  }
  get classList() {
    const self = this;
    return {
      add: (...xs) => { const s = new Set(self.className.split(/\s+/).filter(Boolean)); xs.forEach(x => s.add(x)); self.className = [...s].join(" "); },
      toggle: (x, force) => {
        const on = force == null ? !self.className.split(/\s+/).includes(x) : !!force;
        const s = new Set(self.className.split(/\s+/).filter(Boolean));
        if (on) s.add(x); else s.delete(x);
        self.className = [...s].join(" ");
        return on;
      },
      contains: (x) => self.className.split(/\s+/).includes(x),
    };
  }
  set textContent(v) { this._text = String(v); }
  get textContent() { return this._text + this.children.map(x => x.textContent || "").join(""); }
}
globalThis.document = { createElement: (tag) => new El(tag) };
globalThis.localStorage = {
  data: Object.create(null),
  getItem(k) { return this.data[k] || null; },
  setItem(k, v) { this.data[k] = String(v); },
};
"""


def _action_source(src):
    start = src.index("function msgStarred")
    end_marker = "function startMsgEdit" if "function startMsgEdit" in src[start:] else "function buildMsg"
    return src[start:src.index(end_marker, start)]


def _node(js):
    return subprocess.run(["node", "--input-type=module", "--eval", js],
                          text=True, capture_output=True, timeout=30)


class MessageActionTests(unittest.TestCase):
    def test_both_pages_expose_resend_in_the_real_toolbar_and_only_refill(self):
        pages = {
            "ui": UI_PATH.read_text(encoding="utf-8"),
            "share": SHARE_PATH.read_text(encoding="utf-8"),
        }
        for name, src in pages.items():
            self.assertIn('className = "msg-tools"', src, name)
            self.assertIn('add("重发"', src, name)
            self.assertIn("function resendMessage", src, name)

            save = "function saveDraft() { saveDraftCalls++; }" if name == "ui" \
                else "function armDraftSave() { armDraftSaveCalls++; }"
            js = FAKE_DOM + r'''
let selected = ["旧选项"];
let saveDraftCalls = 0, armDraftSaveCalls = 0;
let apiCalls = [];
const ta = { value: "已有草稿", focus() {} };
function $(id) { if (id === "ta") return ta; return { value: "" }; }
function toast() {}
function renderInput() {}
function api() { apiCalls.push([...arguments]); throw new Error("重发不应调用 API"); }
''' + save + "\n" + _action_source(src) + r'''
const div = new El("div");
const bubble = new El("div");
bubble._text = "气泡正文";
div.bubble = bubble;
const msg = { role: "user", mid: "m1", text: "历史正文", selected: ["新选项"] };
const session = { id: "s1" };
attachMsgTools(div, session, msg);
const tools = div.children.find(x => x.className === "msg-tools");
if (!tools) throw new Error("没有消息操作栏");
const labels = tools.children.map(x => x.textContent);
if (!labels.includes("复制") || !labels.includes("引用") || !labels.includes("☆") || !labels.includes("重发"))
  throw new Error("操作栏不完整: " + JSON.stringify(labels));
const resend = tools.children.find(x => x.textContent === "重发");
resend.onclick({ stopPropagation() {} });
if (ta.value !== "已有草稿\n历史正文") throw new Error("重发没有回填编辑器: " + JSON.stringify(ta.value));
if (JSON.stringify(selected) !== JSON.stringify(["新选项"])) throw new Error("重发没有恢复选项");
if (apiCalls.length) throw new Error("重发偷偷调用发送接口: " + JSON.stringify(apiCalls));
if (saveDraftCalls + armDraftSaveCalls !== 1) throw new Error("回填后没有保存草稿");
console.log("ok");
'''
            proc = _node(js)
            self.assertEqual(0, proc.returncode, name + ": " + (proc.stderr or proc.stdout))
            self.assertIn("ok", proc.stdout, name)

    def test_resend_is_not_offered_for_queued_or_ai_messages(self):
        for name, src in (("ui", UI_PATH.read_text(encoding="utf-8")),
                          ("share", SHARE_PATH.read_text(encoding="utf-8"))):
            self.assertIn('if (m.role === "user" && !queued && !m.cancelled)', src, name)
            self.assertIn('m.role === "sys"', src, name)


if __name__ == "__main__":
    unittest.main()
