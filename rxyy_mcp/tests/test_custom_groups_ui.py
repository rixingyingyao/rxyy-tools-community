# -*- coding: utf-8 -*-
"""侧栏自定义分组：源码里必须真有 localStorage 分组、抽出渲染、拖入拖出。"""
import unittest
from pathlib import Path

UI_PATH = Path(__file__).resolve().parents[1] / "ui.html"


class CustomGroupsUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ui = UI_PATH.read_text(encoding="utf-8")
        start = cls.ui.index("function renderTabs()")
        cls.render = cls.ui[start:cls.ui.index("function buildPickBar()")]
        drag = cls.ui.index("目标已入组则改组内顺序")
        cls.drag = cls.ui[drag:cls.ui.index("function startRename(")]

    def test_groups_persist_in_local_storage_key(self):
        self.assertIn('localStorage.getItem("cj_custom_groups")', self.ui)
        self.assertIn("function loadGroups()", self.ui)
        self.assertIn("function saveGroups(", self.ui)
        self.assertIn("function addToGroup(", self.ui)
        self.assertIn("function leaveGroup(", self.ui)
        self.assertIn("function createGroup(", self.ui)
        self.assertIn("function dissolveGroup(", self.ui)
        self.assertIn("function buildCustomGroupHead(", self.ui)

    def test_context_menu_has_group_actions(self):
        self.assertIn('data-action="group-new"', self.ui)
        self.assertIn('id="tabMenuGroups"', self.ui)
        self.assertIn('data-action="group-leave"', self.ui)
        self.assertIn("新建分组并移入", self.ui)
        self.assertIn("移出分组", self.ui)
        self.assertIn("action === \"group-new\"", self.ui)
        self.assertIn("action === \"group-add\"", self.ui)
        self.assertIn("action === \"group-leave\"", self.ui)

    def test_render_tabs_pulls_grouped_out_of_default_buckets(self):
        self.assertIn("const taken = groupedIdSet()", self.render)
        self.assertIn("const liveRest = notTaken(live)", self.render)
        self.assertIn("const resumeRest = notTaken(resume)", self.render)
        self.assertIn("const endedRest = notTaken(ended)", self.render)
        self.assertIn("buildCustomGroupHead(g, members.length, activeInside)", self.render)
        self.assertIn('out.id = "groupDropOut"', self.render)
        self.assertIn("拖到这里移出分组", self.render)
        self.assertIn("const keepEmptyGroups", self.render)
        self.assertIn("tgroup custom", self.ui)

    def test_tabs_signature_includes_groups(self):
        self.assertIn('"#g" + JSON.stringify(loadGroups()', self.ui)

    def test_drop_into_group_or_leave_group(self):
        self.assertIn("if (groupOf(s.id))", self.drag)
        self.assertIn("addToGroup(tg.id, src, s.id)", self.drag)
        self.assertIn("if (groupOf(src)) leaveGroup(src)", self.drag)
        self.assertIn('classList.add("dragging-grouped")', self.drag)
        self.assertIn("#tabs.dragging-grouped #groupDropOut", self.ui)

    def test_group_header_rename_collapse_dissolve(self):
        self.assertIn("function startGroupRename(", self.ui)
        self.assertIn("cur.collapsed = !cur.collapsed", self.ui)
        self.assertIn("dissolveGroup(g.id)", self.ui)
        self.assertIn("addToGroup(g.id, dragSid)", self.ui)
        # 09-07 接手真机走查：有成员的组点 × 直接散掉、名字也没了，补一句确认；空组仍直接收
        self.assertIn('if (n > 0 && !window.confirm("解散分组「"', self.ui)
