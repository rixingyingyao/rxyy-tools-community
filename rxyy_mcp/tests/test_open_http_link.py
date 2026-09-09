# -*- coding: utf-8 -*-
"""外链必须新标签打开，不能把 rxyy tools 里的rxyy MCP iframe 顶掉。"""
import re
import subprocess
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
UI_PATH = APP_DIR / "ui.html"
SHARE_PATH = APP_DIR / "share.html"
_FN_RE = re.compile(
    r"function isExternalHttpHref\(href\) \{.*?\nfunction openHttpLinkOutsideFrame\(e\) \{.*?\n\}",
    re.S,
)


class OpenHttpLinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ui = UI_PATH.read_text(encoding="utf-8")
        cls.share = SHARE_PATH.read_text(encoding="utf-8")

    def test_both_frontends_install_capture_click_guard(self):
        for html in (self.ui, self.share):
            self.assertIn("function openHttpLinkOutsideFrame(e)", html)
            self.assertIn("function openLinkViewer(url)", html)
            self.assertIn('id="linkview"', html)
            self.assertIn(
                'document.addEventListener("click", openHttpLinkOutsideFrame, true)',
                html,
            )

    def _fn(self, html):
        m = _FN_RE.search(html)
        if not m:
            raise AssertionError("找不到外链拦截函数")
        return m.group(0)

    def run_guard(self, html, scenario):
        script = r"""
const opened = [];
const copied = [];
const toasts = [];
const els = {
  linkview: { style: { display: "none" } },
  linkviewUrl: { textContent: "", title: "" },
  linkviewFrame: { src: "" },
};
function $(id) { return els[id]; }
const window = { open(url, target, feat) { opened.push([url, target, feat]); return { ok: 1 }; } };
function copyText(t) { copied.push(t); }
function toast(t) { toasts.push(t); }
%s
%s
console.log("__DONE__");
""" % (self._fn(html), scenario)
        proc = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            check=True, text=True, capture_output=True,
        )
        # 顶层 await 挂住时 node 是静默 exit 0，剧本一句没跑完也照样绿；
        # 收不到收尾记号就算红
        self.assertIn("__DONE__", proc.stdout or "",
                      "剧本没跑到最后就退出了（await 挂住了，node 不会报错）")

    def test_figma_left_click_opens_overlay_and_does_not_navigate(self):
        """事故：点 Figma 链接把 iframe 导航走，整页「拒绝了我们的连接请求」。
        现改为页内预览层，控制台本体不跳转。"""
        self.run_guard(self.ui, r"""
let prevented = false;
const ev = {
  target: { closest() { return { getAttribute() { return "https://www.figma.com/design/abc"; }, href: "https://www.figma.com/design/abc" }; } },
  defaultPrevented: false, button: 0,
  preventDefault() { prevented = true; },
};
if (!openHttpLinkOutsideFrame(ev) || !prevented) throw new Error("未拦截左键");
if (els.linkview.style.display !== "flex") throw new Error("未打开预览层");
if (els.linkviewFrame.src !== "https://www.figma.com/design/abc") throw new Error("预览层没挂上地址");
if (opened.length) throw new Error("左键不该直接 window.open 顶掉控制台");
""")

    def test_hash_and_modified_clicks_pass_through(self):
        self.run_guard(self.ui, r"""
const hashEv = {
  target: { closest() { return { getAttribute() { return "#top"; }, href: "http://127.0.0.1/ui#top" }; } },
  defaultPrevented: false, button: 0, preventDefault() { throw new Error("锚点不该拦"); },
};
if (openHttpLinkOutsideFrame(hashEv)) throw new Error("锚点被拦");
const ctrlEv = {
  target: { closest() { return { getAttribute() { return "https://example.com"; }, href: "https://example.com" }; } },
  defaultPrevented: false, button: 0, ctrlKey: true,
  preventDefault() { throw new Error("Ctrl 点击不该拦"); },
};
if (openHttpLinkOutsideFrame(ctrlEv)) throw new Error("Ctrl 点击被拦");
if (opened.length) throw new Error("放过的点击却 window.open 了");
""")

    def test_share_page_uses_the_same_guard(self):
        self.assertEqual(self._fn(self.ui), self._fn(self.share))


if __name__ == "__main__":
    unittest.main()
