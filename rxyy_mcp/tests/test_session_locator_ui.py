# -*- coding: utf-8 -*-
import unittest
from pathlib import Path


UI_PATH = Path(__file__).resolve().parents[1] / "ui.html"


class SessionLocatorUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = UI_PATH.read_text(encoding="utf-8")

    def test_footer_contains_locator_prompt_action(self):
        self.assertIn('id="sessionPrompt"', self.html)

    def test_tab_context_menu_contains_all_copy_actions(self):
        self.assertIn('id="tabMenu"', self.html)
        self.assertIn('data-action="id"', self.html)
        self.assertIn('data-action="path"', self.html)
        self.assertIn('data-action="prompt"', self.html)

    def test_tabs_open_context_menu_and_call_locator_api(self):
        self.assertIn("tab.oncontextmenu", self.html)
        self.assertIn("get_session_locator", self.html)
        self.assertIn("copyLocatorValue", self.html)

    def test_narrow_toolbar_wraps_controls_inside_the_viewport(self):
        """窄窗口不能把顶栏后半组推到不可见的横向滚动区。"""
        start = self.html.index("@media (max-width: 760px)")
        end = self.html.index("</style>", start)
        mobile_css = self.html[start:end]
        self.assertIn("#topbar, #footer", mobile_css)
        self.assertIn("overflow-x: hidden", mobile_css)
        self.assertIn("flex-wrap: wrap", mobile_css)
        self.assertIn("#topbar > #teambox", mobile_css)


if __name__ == "__main__":
    unittest.main()
