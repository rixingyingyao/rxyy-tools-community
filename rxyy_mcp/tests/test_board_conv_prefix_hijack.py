# -*- coding: utf-8 -*-
"""事故：两位长的 conversation_id 前缀命中了别人名下的卡。

2026-08-25 全量测试从绿转红（3 条），红的原因不在被测代码里，而在 rxyy 当天
真实建的一张卡：

    a8eb47dfe878  assignee.conversation_id = "c1ef737c"
                  status = in_review  project = "360photovedio_api"

而 test_team_track / test_task_root 里造的假会话 conv_key 就叫 "c1"。
team_facts._same_conv 按 ``min(len(a), len(b), 8)`` 位比较，两位一比就相等，
于是那个假会话被判成这张真卡的主人：

* _card_project_of 把它归进「360photovedio_api」；
* _team_sessions 按项目找人，同工作区那位队友不在这个项目里；
* relay_from_agent 广播当场回「没有队友」。

两个独立的洞，各修各的，缺一条都还会再犯：

1. **前缀劫持**：ID 越短命中越多。两位十六进制平均能套住 1/256 张卡；一位
   就是 1/16。这不是测试专属——agent 报到时 conversation_id 是它自己填的，
   填短了就会在团队面板上顶着别人的卡和别人的项目。
2. **测试读生产库**：rxyy MCP 这套测试从来没设过 RXYY_BOARD_STORE，
   board_store_path() 于是落到 %LOCALAPPDATA%\\rxyy-tools-community\\board\\.board.json——
   rxyy 的真面板。测试绿不绿取决于他今天建了什么卡。console 那边早有
   `_refuse_writing_the_real_board`（08-13 被 6 张垃圾卡教训过），hub 这边
   读的那一侧一直没有对应的闸。
"""
import os
import sys
import unittest
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent.parent
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

import team_facts  # noqa: E402


def _card(conv, project="360photovedio_api", status="in_review", cid="a8eb47dfe878"):
    return {"id": cid, "title": "修复多模态检索结果随时间漂移", "project": project,
            "status": status, "archived": False,
            "assignee": {"conversation_id": conv, "agent_type": "codex"}}


class SameConvTests(unittest.TestCase):
    """比 ID 这件事本身。"""

    def test_a_two_char_id_does_not_own_another_agents_conversation(self):
        # 事故原样：假会话 "c1" 撞上真卡 "c1ef737c"
        self.assertFalse(team_facts._same_conv("c1", "c1ef737c"))
        self.assertFalse(team_facts._same_conv("c1ef737c", "c1"))

    def test_a_single_char_id_matches_nothing_but_itself(self):
        self.assertFalse(team_facts._same_conv("b", "b4eff2ee"))
        self.assertTrue(team_facts._same_conv("b", "b"))

    def test_the_eight_char_short_form_still_matches_the_full_id(self):
        # 这才是这个函数存在的理由，别修没了
        self.assertTrue(team_facts._same_conv("b4eff2ee", "b4eff2ee9c1d4f70"))
        self.assertTrue(team_facts._same_conv("b4eff2ee9c1d4f70", "b4eff2ee"))

    def test_identical_ids_match_whatever_their_length(self):
        # 看板上真有非 8 位的记号：控制台自己是 "console"，走查 agent 是
        # "walkthru-agent"
        self.assertTrue(team_facts._same_conv("console", "console"))
        self.assertTrue(team_facts._same_conv("walkthru-agent", "walkthru-agent"))
        self.assertTrue(team_facts._same_conv("B4EFF2EE", "b4eff2ee"))

    def test_two_long_names_sharing_eight_letters_are_not_the_same_agent(self):
        # 非十六进制的记号按前 8 位比是错的："walkthru-" 开头的两个不是一个人
        self.assertFalse(team_facts._same_conv("walkthru-agent", "walkthru-bot"))

    def test_empty_never_matches(self):
        self.assertFalse(team_facts._same_conv("", ""))
        self.assertFalse(team_facts._same_conv("", "c1ef737c"))


class ActiveCardsOfTests(unittest.TestCase):
    """事故现场：短 ID 会不会捡走别人的卡。"""

    def test_a_short_id_picks_up_no_cards(self):
        self.assertEqual([], team_facts.active_cards_of("c1", [_card("c1ef737c")]))

    def test_the_real_owner_still_gets_its_card(self):
        got = team_facts.active_cards_of("c1ef737c", [_card("c1ef737c")])
        self.assertEqual(["a8eb47dfe878"], [c["id"] for c in got])

    def test_the_full_id_still_matches_a_card_recorded_in_short_form(self):
        got = team_facts.active_cards_of("b4eff2ee9c1d4f70", [_card("b4eff2ee")])
        self.assertEqual(1, len(got))


class BoardStoreIsolationTests(unittest.TestCase):
    """测试不许读 rxyy 的真面板。"""

    def test_under_pytest_the_store_is_not_the_real_board(self):
        real = team_facts.default_board_store_path()
        self.assertNotEqual(real, team_facts.board_store_path(),
                            "测试读到了真面板：绿不绿取决于 rxyy 今天建了什么卡")

    def test_an_explicit_override_still_wins(self):
        # 想喂数据的测试仍然能指定自己的库
        old = os.environ.get("RXYY_BOARD_STORE")
        os.environ["RXYY_BOARD_STORE"] = str(MODULE_DIR / "tests" / "_fake_board.json")
        try:
            self.assertEqual(MODULE_DIR / "tests" / "_fake_board.json",
                             team_facts.board_store_path())
        finally:
            if old is None:
                os.environ.pop("RXYY_BOARD_STORE", None)
            else:
                os.environ["RXYY_BOARD_STORE"] = old

    def test_outside_pytest_it_is_still_the_real_board(self):
        # 生产里必须照旧读真库，别把闸修成「hub 也读不到卡」
        old = os.environ.pop("PYTEST_CURRENT_TEST", None)
        old_override = os.environ.pop("RXYY_BOARD_STORE", None)
        old_under_test = os.environ.pop("CHIJIU_UNDER_TEST", None)
        old_argv = sys.argv
        sys.argv = ["hub.py", "--daemon"]
        try:
            self.assertEqual(team_facts.default_board_store_path(),
                             team_facts.board_store_path())
        finally:
            sys.argv = old_argv
            if old_under_test is not None:
                os.environ["CHIJIU_UNDER_TEST"] = old_under_test
            if old is not None:
                os.environ["PYTEST_CURRENT_TEST"] = old
            if old_override is not None:
                os.environ["RXYY_BOARD_STORE"] = old_override


if __name__ == "__main__":
    unittest.main()
