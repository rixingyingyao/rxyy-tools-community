# -*- coding: utf-8 -*-
"""消息区 / 输入框对齐 Bajie 的皮：源码里必须真有这些结构和文案。"""
import unittest
from pathlib import Path

UI_PATH = Path(__file__).resolve().parents[1] / "ui.html"


class MessageComposerUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ui = UI_PATH.read_text(encoding="utf-8")

    def test_message_hover_toolbar_and_star_filter_exist(self):
        self.assertIn('className = "msg-tools"', self.ui)
        self.assertIn("function attachMsgTools", self.ui)
        self.assertIn("只看收藏", self.ui)
        self.assertIn("edit_message", self.ui)
        self.assertIn("delete_message", self.ui)
        self.assertIn("(已编辑)", self.ui)

    def test_code_fold_defaults_at_thirty_lines(self):
        self.assertIn("function foldLongCode", self.ui)
        self.assertIn("code-folded", self.ui)
        self.assertIn("n <= 30", self.ui)
        self.assertIn("展开全部 ", self.ui)
        self.assertIn("function openCodeFs", self.ui, "Bajie 代码块全屏")
        self.assertIn('id="codeFs"', self.ui)
        self.assertIn("code-fsbtn", self.ui)
        self.assertIn("closeCodeFs(); return;", self.ui)

    def test_composer_enter_to_send_and_slash_menu(self):
        self.assertIn('id="composerBox"', self.ui)
        self.assertIn("Enter 发送 · Shift+Enter 换行", self.ui)
        self.assertIn("e.key === \"Enter\" && !e.shiftKey && !e.ctrlKey && !e.altKey", self.ui)
        self.assertIn("function renderSlashMenu", self.ui)
        self.assertIn('id="jumpBottom"', self.ui)
        self.assertIn("function syncJumpBottom", self.ui)
        self.assertIn("function markMsgRuns", self.ui)
        self.assertIn("border-radius: 18px", self.ui)
        self.assertIn('id="composerTools"', self.ui)
        self.assertIn('id="btnQuick"', self.ui)
        self.assertIn('id="btnPreview"', self.ui)
        self.assertIn('id="btnComposerFs"', self.ui)
        self.assertIn("class=\"c-round\"", self.ui)
        self.assertIn("composer-fs", self.ui)
        self.assertIn("setComposerFs", self.ui)
        self.assertIn("function mentionItems", self.ui)
        self.assertIn("function openCmdPal", self.ui)
        self.assertIn('id="cmdPal"', self.ui)
        self.assertIn('id="btnAuto"', self.ui)
        self.assertIn('id="trashList"', self.ui)
        self.assertIn("function trashPush", self.ui)
        self.assertIn("wbhook_answer", self.ui)
        self.assertIn("btn_wbhook_install", self.ui)

    def test_msgs_live_in_a_pane_so_jump_button_survives_repaint(self):
        self.assertIn('id="msgsPane"', self.ui)
        self.assertIn("attachMsgTools(div, s, m)", self.ui)
        # innerHTML 清空不能把跳转按钮一起干掉
        start = self.ui.index("function paintMessages")
        chunk = self.ui[start:self.ui.index("function msgStarred")]
        self.assertIn('msgsBox.innerHTML = ""', chunk)
        self.assertIn("attachMsgTools(div, s, m)", chunk)
        self.assertIn("markMsgRuns(msgsBox)", chunk)
        self.assertIn("syncJumpBottom()", chunk)
