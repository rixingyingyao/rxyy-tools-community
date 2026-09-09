# -*- coding: utf-8 -*-
"""成果展示：agent 改完东西后把产物挂在 zhi 上，用户在手机上直接验收。

渲染刻意放在服务端（hub），控制台 ui.html 与分享页 share.html 都只是
`bubble.innerHTML = m.html`，两端零改动就长一个样。图片写本机绝对路径，
交给既有的 _localize_imgs 搬进 图片/ 目录，前端各自改写成自己的图片代理。
"""
import html as html_mod
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub

PNG = b"\x89PNG\r\n\x1a\n0000"


class ArtifactKindTests(unittest.TestCase):
    def test_explicit_kinds(self):
        for k in ("截图", "对比", "diff", "链接"):
            self.assertEqual(k, hub._art_kind({"类型": k}))

    def test_english_and_alias_kinds(self):
        self.assertEqual("截图", hub._art_kind({"type": "screenshot"}))
        self.assertEqual("链接", hub._art_kind({"类型": "URL"}))
        self.assertEqual("diff", hub._art_kind({"类型": "patch"}))
        self.assertEqual("对比", hub._art_kind({"type": "compare"}))

    def test_kind_inferred_from_fields(self):
        """模型漏填类型是常事，靠已有字段猜出来，别整条丢掉"""
        self.assertEqual("对比", hub._art_kind({"前": "a.png", "后": "b.png"}))
        self.assertEqual("链接", hub._art_kind({"地址": "http://x"}))
        self.assertEqual("diff", hub._art_kind({"内容": "--- a"}))
        self.assertEqual("截图", hub._art_kind({"路径": "a.png"}))

    def test_unrecognizable_item_has_no_kind(self):
        self.assertEqual("", hub._art_kind({"说明": "只有说明"}))


class RenderArtifactsTests(unittest.TestCase):
    def test_empty_input_renders_nothing(self):
        self.assertEqual("", hub.render_artifacts(None))
        self.assertEqual("", hub.render_artifacts([]))

    def test_items_without_content_render_nothing(self):
        self.assertEqual("", hub.render_artifacts([{"说明": "空的"}, "不是字典"]))

    def test_screenshot_copied_and_shown(self):
        """截图走 _localize_imgs：原图拷进 图片/，src 改写成拷贝后的路径"""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "after.png"
            src.write_bytes(PNG)
            hist = Path(tmp) / "hist"
            with patch.dict(hub.HUB.cfg, {"history_dir": str(hist)}):
                out = hub.render_artifacts(
                    [{"类型": "截图", "路径": str(src), "说明": "改后的设置页"}])
            copies = list((hist / "图片").rglob("md_*.png"))
            self.assertEqual(1, len(copies))
            self.assertIn(str(copies[0]), out)
            self.assertIn("改后的设置页", out)
            self.assertIn("🎁 本次成果", out)

    def test_before_after_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "b.png", Path(tmp) / "a.png"
            a.write_bytes(PNG)
            b.write_bytes(PNG + b"x")
            hist = Path(tmp) / "hist"
            with patch.dict(hub.HUB.cfg, {"history_dir": str(hist)}):
                out = hub.render_artifacts(
                    [{"类型": "对比", "前": str(a), "后": str(b), "说明": "暗色模式"}])
            self.assertEqual(2, len(list((hist / "图片").rglob("md_*.png"))))
            self.assertIn("改前", out)
            self.assertIn("改后", out)
            self.assertIn("暗色模式", out)

    def test_0907_compare_with_sentences_is_text_not_broken_img(self):
        """09-07 cb2eefee 把两句话填进「对比」的 前/后：以前渲成 <img src="25s 墙 -> …">，
        控制台每次重绘都 404 两次、卡片里两张裂图。一句话就按文字画。"""
        before = "25s 墙 -> TIMEOUT -> 一律收壳 -> 绿灯整排消失，agent 仍堵 zhi"
        after = "TIMEOUT/已报到留下；超时加长；误收后下一声 zhi/zt 塞回列表"
        out = hub.render_artifacts([{"类型": "对比", "前": before, "后": after, "说明": "判死改法"}])
        self.assertNotIn("<img", out)
        self.assertIn(html_mod.escape(before), out)
        self.assertIn(html_mod.escape(after), out)
        self.assertIn("改前", out)
        self.assertIn("改后", out)
        # 截图给了一句话而不是路径，同样按文字画
        out2 = hub.render_artifacts([{"类型": "截图", "路径": "这里本该是路径但 agent 写了说明", "说明": "x"}])
        self.assertNotIn("<img", out2)
        self.assertIn("这里本该是路径但 agent 写了说明", out2)

    def test_image_ref_detection(self):
        for ok in (r"C:\Users\x\a.png", r"\\nas\share\b.jpg", "https://a.b/c.webp", "/api/image?x=1",
                   "shot.PNG", "D:/桌面/图.jpeg"):
            self.assertTrue(hub._art_is_image_ref(ok), ok)
        for bad in ("", "一句话", "25s 墙 -> TIMEOUT", "a\nb.png", "x" * 501 + ".png"):
            self.assertFalse(hub._art_is_image_ref(bad), bad)

    def test_diff_gets_highlight_class(self):
        """diff 标成 language-diff，两个前端的 highlight.js 才会上色"""
        out = hub.render_artifacts(
            [{"类型": "diff", "内容": "--- a\n+++ b\n-旧\n+新", "说明": "修了 1 个文件"}])
        self.assertIn("language-diff", out)
        self.assertIn("+新", out)
        self.assertIn("修了 1 个文件", out)

    def test_oversized_diff_truncated(self):
        out = hub.render_artifacts([{"类型": "diff", "内容": "+x\n" * 40000}])
        self.assertIn("已截断", out)
        self.assertLess(len(out), hub._DIFF_MAX + 2000)

    def test_link_rendered_as_anchor(self):
        out = hub.render_artifacts(
            [{"类型": "链接", "地址": "http://localhost:5173", "说明": "本地预览"}])
        self.assertIn('href="http://localhost:5173"', out)
        self.assertIn('target="_blank"', out)
        self.assertIn("本地预览", out)

    def test_dangerous_url_dropped(self):
        """成果由 agent 生成，仍按不可信内容对待：只放行 http(s) 与站内绝对路径"""
        for bad in ("javascript:alert(1)", "data:text/html,<script>", "file:///C:/x"):
            self.assertEqual("", hub.render_artifacts([{"类型": "链接", "地址": bad}]))

    def test_note_is_html_escaped(self):
        out = hub.render_artifacts(
            [{"类型": "链接", "地址": "http://x", "说明": "<img onerror=alert(1)>"}])
        self.assertNotIn("<img onerror", out)
        self.assertIn("&lt;img", out)

    def test_diff_body_is_html_escaped(self):
        out = hub.render_artifacts([{"类型": "diff", "内容": "-<script>x</script>"}])
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_item_count_capped(self):
        items = [{"类型": "链接", "地址": "http://x/%d" % i} for i in range(50)]
        out = hub.render_artifacts(items)
        self.assertEqual(hub._ARTIFACTS_MAX, out.count("<a href="))

    def test_broken_item_does_not_kill_the_rest(self):
        out = hub.render_artifacts([
            {"类型": "链接", "地址": "http://good"},
            {"类型": "截图"},                       # 缺路径
            {"类型": "diff", "内容": "+ok"},
        ])
        self.assertIn("http://good", out)
        self.assertIn("+ok", out)

    def test_uses_inline_styles_not_css_classes(self):
        """两个前端的样式表各改各的，成果块必须自带样式才能两端一致"""
        out = hub.render_artifacts([{"类型": "链接", "地址": "http://x"}])
        self.assertIn("style=", out)


class ArtifactsDigestTests(unittest.TestCase):
    def test_digest_lists_kind_and_note(self):
        d = hub.artifacts_digest([
            {"类型": "截图", "路径": "a.png", "说明": "设置页"},
            {"类型": "链接", "地址": "http://x"},
        ])
        self.assertEqual(["截图：设置页", "链接：http://x"], d)

    def test_digest_skips_junk(self):
        self.assertEqual([], hub.artifacts_digest([{"说明": "无类型"}, "字符串", None]))

    def test_digest_of_nothing(self):
        self.assertEqual([], hub.artifacts_digest(None))


class LogAiTests(unittest.TestCase):
    def test_artifacts_written_into_chat_log(self):
        """聊天记录 .md 是接手用的唯一真源，成果得在里面留痕"""
        written = []
        h = hub.Hub.__new__(hub.Hub)
        with patch.object(hub.Hub, "_append_file",
                          lambda self, s, block: written.append(block)):
            h.log_ai(None, "改完了", ["结束"],
                     [{"类型": "截图", "路径": "a.png", "说明": "设置页"}])
        self.assertIn("本次成果: 截图：设置页", "".join(written))

    def test_no_artifacts_keeps_old_format(self):
        written = []
        h = hub.Hub.__new__(hub.Hub)
        with patch.object(hub.Hub, "_append_file",
                          lambda self, s, block: written.append(block)):
            h.log_ai(None, "改完了", ["结束"])
        self.assertNotIn("本次成果", "".join(written))


if __name__ == "__main__":
    unittest.main()
