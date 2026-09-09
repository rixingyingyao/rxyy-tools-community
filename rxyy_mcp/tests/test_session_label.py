# -*- coding: utf-8 -*-
"""tab 显示名：哪个工作区的 agent、在改哪个项目的哪块功能"""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub

WS1 = r"d:\Desktop\cursor工作流"
WS2 = r"c:\Users\Administrator\AICodebrain"


def _s(conv, name, root=WS1, cwd=WS1):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = "sid", conv, name
    s.task_root, s.cwd = root, cwd
    s.lock = threading.Lock()
    return s


class SessionLabelTests(unittest.TestCase):
    def test_untasked_tab_keeps_its_own_name(self):
        with patch.dict(hub.HUB.cfg, {"team_assign": {}}):
            self.assertEqual("待命·cursor工作流1",
                             hub.Api().session_label(_s("c1", "待命·cursor工作流1")))

    def test_shows_project_and_feature_once_assigned(self):
        with patch.dict(hub.HUB.cfg, {"team_assign": {"c1": "日报生成"}}):
            self.assertEqual("cursor工作流·日报生成",
                             hub.Api().session_label(_s("c1", "待命·cursor工作流1")))

    def test_marks_where_the_agent_actually_lives_when_borrowed(self):
        # AICodebrain 那个闲下来的 agent 被叫来做 rxyy tools 的账号库
        with patch.dict(hub.HUB.cfg, {"team_assign": {"c1": "账号库"}}):
            self.assertEqual("AICodebrain›cursor工作流·账号库",
                             hub.Api().session_label(_s("c1", "待命·AICodebrain1",
                                                        root=WS1, cwd=WS2)))

    def test_label_follows_reassignment(self):
        s = _s("c1", "待命·cursor工作流1")
        with patch.dict(hub.HUB.cfg, {"team_assign": {"c1": "日报"}}):
            first = hub.Api().session_label(s)
        with patch.dict(hub.HUB.cfg, {"team_assign": {"c1": "周报阶段成果"}}):
            second = hub.Api().session_label(s)
        self.assertNotEqual(first, second)
        self.assertIn("周报阶段成果", second)

    def test_survives_a_session_without_directories(self):
        with patch.dict(hub.HUB.cfg, {"team_assign": {"c1": "打杂"}}):
            self.assertEqual("打杂", hub.Api().session_label(_s("c1", "x", root="", cwd="")))


if __name__ == "__main__":
    unittest.main()
