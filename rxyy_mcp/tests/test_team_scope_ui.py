# -*- coding: utf-8 -*-
"""同一工作区的多个团队项目必须用 scope key 和项目数据独立操作。"""
import unittest
from pathlib import Path


UI_PATH = Path(__file__).resolve().parents[1] / "ui.html"
SHARE_PATH = Path(__file__).resolve().parents[1] / "share.html"


class TeamScopeUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ui_html = UI_PATH.read_text(encoding="utf-8")
        cls.share_html = SHARE_PATH.read_text(encoding="utf-8")

    def test_selection_uses_scope_key_instead_of_workspace_root(self):
        self.assertIn("find(p => p.key === team.sel)", self.ui_html)
        self.assertIn('data-scope="', self.ui_html)
        self.assertIn("team.sel = el.dataset.scope", self.ui_html)
        self.assertIn("some(p => p.key === team.sel)", self.ui_html)
        self.assertNotIn("p.root === team.sel", self.ui_html)

    def test_project_mutations_send_root_and_project(self):
        self.assertIn('$("tmSeatTrack").value,\n          p.project)', self.ui_html)
        self.assertIn('team_set_board(p.root, $("tmBoard").value, p.project)', self.ui_html)
        self.assertIn('team_broadcast(p.root, text, "", tk, p.project)', self.ui_html)
        self.assertIn('$("tmNewTrack").value, p.project)', self.ui_html)

    def test_share_relay_panel_uses_project_scoped_rows(self):
        start = self.share_html.index("async function renderTeam()")
        end = self.share_html.index("/* 批量派单", start)
        render_team = self.share_html[start:end]
        self.assertIn("const relays = (p.relays || []).slice().reverse();", render_team)
        self.assertNotIn("r.relays", render_team)
        relay_start = render_team.index("const relays = (p.relays || []).slice().reverse();")
        self.assertLess(relay_start, render_team.index("body.appendChild(box);", relay_start))

    def test_legacy_relays_are_explicitly_separate_on_both_surfaces(self):
        self.assertIn("team.legacyRelays = r.legacy_relays || []", self.ui_html)
        self.assertIn("升级前未分项目历史", self.ui_html)
        self.assertIn("const legacyRelays = (r.legacy_relays || []).slice().reverse();",
                      self.share_html)
        self.assertIn("升级前未分项目历史", self.share_html)

    def test_dead_tabs_are_not_pinned_live_by_queued_mail(self):
        self.assertIn("已死会话的排队送不出去", self.ui_html)
        self.assertNotIn("if (unseen > 0 || s.queued > 0) return null;", self.ui_html)

    def test_idle_hooks_fold_out_of_the_topbar(self):
        self.assertIn("pk-dormant", self.ui_html)
        self.assertIn('parkbox").classList.toggle("pk-dormant"', self.ui_html)
        self.assertIn('lockbox").classList.toggle("pk-dormant"', self.ui_html)
        self.assertIn("pk-broken", self.ui_html)
        self.assertIn('billbox").classList.toggle("pk-broken"', self.ui_html)

    def test_history_sidebar_caps_at_eight(self):
        self.assertIn("hist.slice(0, 8)", self.ui_html)
        self.assertIn("historyTotal", self.ui_html)
        self.assertIn("其余 ", self.ui_html)
        self.assertIn("histProjects.slice(0, 8)", self.share_html)

    def test_root_select_follows_affiliation_or_cwd(self):
        self.assertIn("a.affiliation || a.workspace_path || a.agent_cwd || a.task_root",
                      self.ui_html)
        self.assertIn("a.affiliation || a.workspace_path || a.agent_cwd || a.task_root",
                      self.share_html)

    def test_sidebar_shows_workspace_and_folds_history(self):
        self.assertIn("工作区 · ", self.ui_html)
        self.assertIn('class="tm-ws"', self.ui_html)
        self.assertIn("历史项目（无人在跑", self.ui_html)
        self.assertIn("function matesLine", self.ui_html)
        self.assertIn("p.workspaces", self.ui_html)

    def test_share_shows_workspace_and_hides_empty_history(self):
        self.assertIn("工作区 · ", self.share_html)
        self.assertIn("p.workspaces", self.share_html)
        self.assertIn("历史项目（无人在跑", self.share_html)
        self.assertIn("liveProjects", self.share_html)

    def test_0831_dead_groups_sort_newest_first(self):
        """0831 用户：待续（未完成）/已结束 两组要按时间倒序，最近的放上面。"""
        self.assertIn("const lastAge = (s) => s.idle_secs", self.ui_html)
        self.assertIn("resumeRest.sort((a, b) => lastAge(a) - lastAge(b))", self.ui_html)
        self.assertIn("endedRest.sort((a, b) => lastAge(a) - lastAge(b))", self.ui_html)

    def test_0831_cleanup_and_team_grouping_copy_is_explicit(self):
        """0831 用户：「（每周清理）」「团队（按项目划分）」含义不够明确——
        文案必须写清：按天清理只碰已删除标签的旧记录/图片；团队按项目划分、
        同名项目跨文件夹/工作区算一队。"""
        self.assertIn("只清「已从列表删除的会话」留下的旧记录", self.ui_html)
        self.assertIn("标签本身更不会被自动删", self.ui_html)
        self.assertIn("团队面板 · 按项目划分（同名项目跨文件夹/工作区算一队", self.ui_html)
        self.assertIn("已删除标签的旧记录保留最近 ", self.ui_html)
        self.assertIn("这组标签不会被定期清理", self.ui_html)

    def test_desktop_legacy_relays_render_outside_selected_project(self):
        modal_start = self.ui_html.index('<div id="teamModal"')
        tm_wrap = self.ui_html.index('<div class="tm-wrap">', modal_start)
        tm_legacy = self.ui_html.index('<div class="tm-legacy" id="tmLegacy"></div>', tm_wrap)
        self.assertGreater(tm_legacy, tm_wrap)

        main_start = self.ui_html.index("function renderTeamMain()")
        main_end = self.ui_html.index("async function teamRefreshOnce()", main_start)
        self.assertNotIn("legacyRelaySection()", self.ui_html[main_start:main_end])

        refresh_start = self.ui_html.index("async function teamRefreshOnce()")
        refresh_end = self.ui_html.index("const teamRefresh = singleFlight", refresh_start)
        refresh = self.ui_html[refresh_start:refresh_end]
        self.assertIn("function renderLegacyRelays()", self.ui_html)
        self.assertLess(refresh.index("renderLegacyRelays();"), refresh.index("if (!team.open) return;"))


if __name__ == "__main__":
    unittest.main()
