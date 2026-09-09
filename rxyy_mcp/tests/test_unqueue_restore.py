# -*- coding: utf-8 -*-
"""撤回一条排队消息，原文和附件必须回到**它自己那个会话**。

08-25 全面体检第二段查出，两个页面各错一半：

* 手机页 share.html 把服务端交还的 restore 整包扔了。`unqueue_message` 那边写得
  很清楚「撤回时把文本/图片/文件原样交还前端，恢复到输入框（而不是凭空丢失）」
  ——手机上撤回一条写了半天的长消息，字和图当场就没了，找都没处找。
* 桌面页 ui.html 收了 restore，却往「此刻开着的那个框」里灌：unqueue 那一趟回来
  之前人完全可能切了 tab，于是这条话连同附件被塞进别人的输入框，下一次发送就发
  给了错的 AI 会话。撤回本身是「我不想让这句发出去」，结果它改嫁了。

测法沿用 test_send_inflight.py：把两个页面里的真函数抠出来丢进 node 跑，
不是看源码里有没有那行字。
"""
import subprocess
import unittest
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
UI_PATH = MODULE_DIR / "ui.html"
SHARE_PATH = MODULE_DIR / "share.html"

BODY_TAIL = "})().catch(err => { console.error(err.stack || err); process.exit(1); });"

# --- 桌面页：草稿是按会话存在 drafts 里的，切回去 loadDraft 就能填回框 --------
UI_STUBS = """
let images = [], files = [], selected = [], drafts = {};
const ta = { value: "" };
function $(id) { return id === "ta" ? ta : { value: "", disabled: false }; }
let activeId = "s1";
function activeSession() { return activeId ? { id: activeId } : null; }
let saveDraftCalls = 0;
function saveDraft() { saveDraftCalls++; }
const drafted = [];
function pushDraft(sid, text, imgs, fls) {
  drafted.push([sid, text, imgs.slice(), fls.slice()]);
  return Promise.resolve();
}
"""

# --- 手机页：没有本地按会话的草稿盘，人不在就先寄存，回来那一拍再还 ----------
SHARE_STUBS = """
let images = [], files = [], selected = [];
const ta = { value: "" };
function $(id) { return id === "ta" ? ta : { value: "", disabled: false }; }
let activeId = "s1";
function activeSession() { return activeId ? { id: activeId } : null; }
function autoGrow() {}
function bumpMedia() {}
function renderInput() {}
let armed = 0;
function armDraftSave() { armed++; }
const toasts = [];
function toast(t) { toasts.push(t); }
"""


def _slice(html, start_marker, end_marker):
    start = html.index(start_marker)
    return html[start:html.index(end_marker, start)]


class NodeCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ui = UI_PATH.read_text(encoding="utf-8")
        share = SHARE_PATH.read_text(encoding="utf-8")
        cls.ui_restore = _slice(ui, "function restoreUnqueued(sid, rs) {",
                                "function switchTab(sid) {")
        cls.share_restore = _slice(share, "const parkedRestore = {};",
                                   "/* 在途闸：")

    def _node(self, *parts):
        script = "\n".join(parts)
        # 见 test_send_inflight._node：await 挂住时 node 静默 exit 0，只看
        # returncode 会把「一句断言都没跑」当成通过，所以要求跑到最后打个记号
        self.assertIn(BODY_TAIL, script, "测试体没有用标准收尾，记号插不进去")
        script = script.replace(BODY_TAIL, "  console.log('__DONE__');\n" + BODY_TAIL)
        proc = subprocess.run(["node", "--input-type=module", "--eval", script],
                              text=True, capture_output=True)
        self.assertEqual(0, proc.returncode,
                         (proc.stderr or "") + (proc.stdout or ""))
        self.assertIn("__DONE__", proc.stdout or "",
                      "测试体没跑到最后就退出了（await 挂住了，node 不会报错）")


RESTORE = """
const rs = {
  text: "撤回的这一句",
  selected: ["方案A"],
  images: [{ data: "data:image/png;base64,AA", filename: "图.png" }],
  files: [{ name: "稿子.docx", data: "data:application/octet-stream;base64,BB", size: 7 }],
};
"""


class DesktopRestoreTests(NodeCase):
    def test_restoring_while_still_on_the_session_fills_the_box(self):
        """护栏：人没走开时就是老样子——合并进框，不覆盖已经打了的字。"""
        self._node(UI_STUBS, self.ui_restore, RESTORE, r"""
(async () => {
  ta.value = "已经打了的";
  restoreUnqueued("s1", rs);
  if (ta.value !== "已经打了的\n撤回的这一句") {
    throw new Error("没合并进框：" + JSON.stringify(ta.value));
  }
  if (images.length !== 1 || files.length !== 1) throw new Error("附件没回来");
  if (selected.join("|") !== "方案A") throw new Error("选项没回来");
  if (!saveDraftCalls) throw new Error("撤回回来的字没落库，刷一下又白撤了");
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")

    def test_restoring_after_switching_tabs_does_not_touch_the_open_box(self):
        self._node(UI_STUBS, self.ui_restore, RESTORE, r"""
(async () => {
  activeId = "s2";                      // unqueue 在飞的时候人切走了
  ta.value = "李四这边正写着的";
  restoreUnqueued("s1", rs);
  if (ta.value !== "李四这边正写着的") {
    throw new Error("把撤回的话灌进了李四的框：" + JSON.stringify(ta.value));
  }
  if (images.length || files.length) throw new Error("附件串到李四那儿去了");
  const d = drafts["s1"];
  if (!d || d.text !== "撤回的这一句") {
    throw new Error("没落回张三的草稿：" + JSON.stringify(d));
  }
  if (d.images.length !== 1 || d.files.length !== 1) {
    throw new Error("附件没落回张三的草稿：" + JSON.stringify(d));
  }
  if (d.selected.join("|") !== "方案A") throw new Error("选项没落回张三的草稿");
  const last = drafted[drafted.length - 1];
  if (!last || last[0] !== "s1" || last[1] !== "撤回的这一句") {
    throw new Error("推给 hub 的那一枪打错人了：" + JSON.stringify(last));
  }
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")

    def test_restoring_into_a_session_that_already_had_a_draft_merges(self):
        """那个会话本来就存着草稿：撤回的话接在后面，不能把它顶掉。"""
        self._node(UI_STUBS, self.ui_restore, RESTORE, r"""
(async () => {
  activeId = "s2";
  drafts["s1"] = { text: "张三原本存着的", selected: ["旧选项"], images: [],
                   files: [{ name: "旧.pdf", data: "d", size: 1 }] };
  restoreUnqueued("s1", rs);
  const d = drafts["s1"];
  if (d.text !== "张三原本存着的\n撤回的这一句") {
    throw new Error("把原来的草稿顶掉了：" + JSON.stringify(d.text));
  }
  if (d.files.length !== 2) throw new Error("原来的附件被冲了：" + JSON.stringify(d.files));
  if (d.selected.join("|") !== "旧选项|方案A") throw new Error("原来的选项被冲了");
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")

    def test_an_empty_restore_payload_is_a_no_op(self):
        """撤回一条只有选项的空消息：别凭空造出一条空草稿。"""
        self._node(UI_STUBS, self.ui_restore, r"""
(async () => {
  activeId = "s2";
  restoreUnqueued("s1", { text: "", images: [], files: [] });
  if (drafts["s1"]) throw new Error("凭空造了条空草稿：" + JSON.stringify(drafts["s1"]));
  if (drafted.length) throw new Error("还往 hub 推了一枪：" + JSON.stringify(drafted));
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")


class PhoneRestoreTests(NodeCase):
    def test_the_recalled_message_is_no_longer_thrown_away(self):
        self._node(SHARE_STUBS, self.share_restore, RESTORE, r"""
(async () => {
  restoreUnqueued("s1", rs);
  if (ta.value !== "撤回的这一句") {
    throw new Error("撤回的原文没回到框里：" + JSON.stringify(ta.value));
  }
  if (images.length !== 1 || files.length !== 1) {
    throw new Error("撤回的图片/附件被丢了");
  }
  if (selected.join("|") !== "方案A") throw new Error("撤回的选项被丢了");
  if (!armed) throw new Error("撤回回来的字没落库，刷一下又白撤了");
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")

    def test_switching_away_parks_it_instead_of_hitting_the_open_box(self):
        self._node(SHARE_STUBS, self.share_restore, RESTORE, r"""
(async () => {
  activeId = "s2";
  ta.value = "李四这边正写着的";
  restoreUnqueued("s1", rs);
  if (ta.value !== "李四这边正写着的") {
    throw new Error("把撤回的话灌进了李四的框：" + JSON.stringify(ta.value));
  }
  if (images.length || files.length) throw new Error("附件串到李四那儿去了");
  if (!toasts.length) throw new Error("没告诉人东西去哪了");
  // 切回张三：寄存的那份现在还给他
  activeId = "s1";
  ta.value = "";
  drainParkedRestore("s1");
  if (ta.value !== "撤回的这一句") {
    throw new Error("切回来没还给他：" + JSON.stringify(ta.value));
  }
  if (images.length !== 1 || files.length !== 1) throw new Error("附件没还回来");
  if (selected.join("|") !== "方案A") throw new Error("选项没还回来");
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")

    def test_draining_twice_does_not_duplicate(self):
        """还过一次就不能再还第二次，否则每拍轮询都往框里加一遍。"""
        self._node(SHARE_STUBS, self.share_restore, RESTORE, r"""
(async () => {
  activeId = "s2";
  restoreUnqueued("s1", rs);
  activeId = "s1";
  drainParkedRestore("s1");
  drainParkedRestore("s1");
  if (ta.value !== "撤回的这一句") {
    throw new Error("还了两遍：" + JSON.stringify(ta.value));
  }
  if (images.length !== 1) throw new Error("图片加了两遍");
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")

    def test_draining_a_session_with_nothing_parked_is_a_no_op(self):
        self._node(SHARE_STUBS, self.share_restore, r"""
(async () => {
  ta.value = "正写着的";
  drainParkedRestore("s1");
  if (ta.value !== "正写着的") throw new Error("凭空动了框里的字");
  if (armed) throw new Error("没东西可还却还是落了一次库");
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")


if __name__ == "__main__":
    unittest.main()
