# -*- coding: utf-8 -*-
"""消息渲染管线：Cursor 发来的各类 markdown 都要在控制台/手机端正确显示"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub


class RenderMarkdownTests(unittest.TestCase):
    def test_plain_mode_escapes_html(self):
        out = hub.render_markdown("<b>x</b> **y**", is_markdown=False)
        self.assertIn("&lt;b&gt;", out)
        self.assertIn("pre class='plain'", out)

    def test_basic_markdown_still_works(self):
        out = hub.render_markdown("# 标题\n**粗** *斜* `行内`")
        self.assertIn("<h1>标题</h1>", out)
        self.assertIn("<strong>粗</strong>", out)
        self.assertIn("<code>行内</code>", out)

    def test_strikethrough(self):
        out = hub.render_markdown("~~已废弃~~ 保留")
        self.assertIn("<del>已废弃</del>", out)

    def test_strikethrough_not_applied_inside_code(self):
        out = hub.render_markdown("`~~a~~`")
        self.assertNotIn("<del>", out)

    def test_bare_url_becomes_link(self):
        out = hub.render_markdown("详见 https://github.com/a/b/pull/12 。")
        self.assertIn('href="https://github.com/a/b/pull/12"', out)
        self.assertIn('target="_blank"', out)
        self.assertIn("noopener", out)

    def test_bare_url_trailing_punctuation_trimmed(self):
        out = hub.render_markdown("（https://example.com/x）已发。")
        self.assertIn('href="https://example.com/x"', out)
        self.assertIn('target="_blank"', out)

    def test_figma_markdown_link_opens_outside_iframe(self):
        """事故：控制台嵌在 rxyy tools iframe 里点 Figma 链接，整页变成
        「www.figma.com 拒绝了我们的连接请求」。外链必须新标签，不能顶掉 iframe。"""
        out = hub.render_markdown("[设计稿](https://www.figma.com/design/abc)")
        self.assertIn('href="https://www.figma.com/design/abc"', out)
        self.assertIn('target="_blank"', out)
        self.assertIn("noopener", out)

    def test_existing_target_blank_is_kept(self):
        out = hub._force_http_links_new_tab(
            '<a href="https://example.com" target="_blank" rel="noopener">x</a>')
        self.assertEqual(1, out.count('target="_blank"'))
        self.assertEqual(1, out.count("noopener"))

    def test_non_http_anchor_is_untouched(self):
        raw = '<a href="#top">回顶</a><a href="/ui">站内</a>'
        self.assertEqual(raw, hub._force_http_links_new_tab(raw))

    def test_url_inside_code_untouched(self):
        out = hub.render_markdown("```\ncurl https://example.com/api\n```")
        self.assertNotIn("<a ", out)

    def test_task_list_checkboxes(self):
        out = hub.render_markdown("- [x] 已完成\n- [ ] 待办")
        self.assertIn('<li class="task"><input type="checkbox" disabled checked> 已完成', out)
        self.assertIn('<li class="task"><input type="checkbox" disabled> 待办', out)

    def test_table_wrapped_for_horizontal_scroll(self):
        out = hub.render_markdown("| a | b |\n|---|---|\n| 1 | 2 |")
        self.assertIn('<div class="tblwrap"><table>', out)
        self.assertIn("</table></div>", out)

    def test_fenced_code_language_class_kept(self):
        out = hub.render_markdown("```python\nprint(1)\n```")
        self.assertIn('class="language-python"', out)

    def test_cursor_code_citation_block(self):
        out = hub.render_markdown("看这段：\n```12:14:app/components/Todo.tsx\nreturn <div/>;\n```")
        self.assertIn('class="codecite"', out)
        self.assertIn("app/components/Todo.tsx", out)
        self.assertIn("第 12–14 行", out)
        self.assertIn('class="language-typescript"', out)

    def test_citation_like_line_inside_fence_untouched(self):
        out = hub.render_markdown("````\n```12:14:a/b.py\n````")
        self.assertNotIn("codecite", out)

    def test_local_image_copied_and_rewritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "截图.png"
            src.write_bytes(b"\x89PNG\r\n\x1a\n0000")
            hist = Path(tmp) / "hist"
            with patch.dict(hub.HUB.cfg, {"history_dir": str(hist)}):
                out = hub.render_markdown("![shot]({})".format(src))
            copies = list((hist / "图片").rglob("md_*.png"))
            self.assertEqual(1, len(copies))
            self.assertIn(str(copies[0]).replace("\\", "\\"), out)

    def test_windows_path_with_escapable_char_survives(self):
        """08-07 实锤：`\\.` `\\_` `\\-` 都是 markdown 转义序列，反斜杠会被吃掉，
        路径当场失效 → 图搬不动 → 前端 onerror 一藏，用户那头一张图也看不到"""
        for seg in (".chijiu-tmp", "_internal", "-tmp", "#1", "+新", "[备份]"):
            src = "D:\\桌面\\proj\\{}\\shot.png".format(seg)
            out = hub.render_markdown("![s]({})".format(src))
            self.assertIn('src="{}"'.format(src), out,
                          "路径里的 \\{} 被 markdown 吃掉了".format(seg[0]))

    def test_escapable_path_image_actually_gets_copied(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / ".chijiu-tmp" / "演示"
            d.mkdir(parents=True)
            src = d / "截图.png"
            src.write_bytes(b"\x89PNG\r\n\x1a\n0000")
            hist = Path(tmp) / "hist"
            with patch.dict(hub.HUB.cfg, {"history_dir": str(hist)}):
                out = hub.render_markdown("![s]({})".format(src))
            copies = list((hist / "图片").rglob("md_*.png"))
            self.assertEqual(1, len(copies))
            self.assertIn(str(copies[0]), out)

    def test_path_with_parens_not_truncated(self):
        """C:\\Program Files (x86)\\… 是最常见的 Windows 路径，
        按第一个右括号截断的话，图同样搬不动"""
        src = r"C:\Program Files (x86)\app\shot.png"
        out = hub.render_markdown("![s]({})".format(src))
        self.assertIn('src="{}"'.format(src), out)

    def test_local_image_alt_text_kept(self):
        out = hub.render_markdown(r"![改后的设置页](D:\a\.x\shot.png)")
        self.assertIn('alt="改后的设置页"', out)

    def test_quotes_in_local_image_are_escaped(self):
        out = hub.render_markdown(r'![a"b](D:\a\.x\s"1.png)')
        self.assertNotIn('src="D:\\a\\.x\\s"1.png"', out)
        self.assertIn("&quot;", out)

    def test_local_image_path_inside_fence_untouched(self):
        out = hub.render_markdown("```\n![s](D:\\a\\.x\\shot.png)\n```")
        self.assertNotIn("<img", out)
        self.assertIn(r"![s](D:\a\.x\shot.png)", out)

    def test_unc_path_image_protected(self):
        out = hub.render_markdown(r"![s](\\nas\share\.d\shot.png)")
        self.assertIn(r'src="\\nas\share\.d\shot.png"', out)

    def test_web_image_untouched(self):
        out = hub.render_markdown("![x](https://e.com/a.png)")
        self.assertIn('src="https://e.com/a.png"', out)

    def test_missing_local_image_left_as_is(self):
        out = hub.render_markdown(r"![x](D:\不存在\nope.png)")
        self.assertIn("nope.png", out)

    def test_two_space_nested_list_renders_nested(self):
        out = hub.render_markdown("- 父项\n  - 子项A\n  - 子项B")
        self.assertIn("<ul>\n<li>父项<ul>", out.replace("</li>", ""))
        self.assertIn("子项A", out)

    def test_four_space_style_untouched(self):
        src = "- 父项\n    - 子项"
        self.assertEqual(hub._normalize_list_indent(src), src)

    def test_list_lines_inside_fence_not_reindented(self):
        src = "```\n  - 不是列表\n```\n- 真列表\n  - 子项"
        out = hub.render_markdown(src)
        self.assertIn("  - 不是列表", out)


class PushSummaryTests(unittest.TestCase):
    def test_strips_markdown_noise(self):
        s = hub.push_summary("## 标题\n**重点**内容 `code` [链接](https://e.com)\n```py\nx=1\n```")
        self.assertNotIn("#", s)
        self.assertNotIn("**", s)
        self.assertIn("重点内容", s)
        self.assertIn("链接", s)
        self.assertIn("〔代码〕", s)

    def test_truncates_to_limit(self):
        s = hub.push_summary("很长" * 200, limit=50)
        self.assertLessEqual(len(s), 51)
        self.assertTrue(s.endswith("…"))

    def test_empty_input(self):
        self.assertEqual(hub.push_summary(""), "")


if __name__ == "__main__":
    unittest.main()
