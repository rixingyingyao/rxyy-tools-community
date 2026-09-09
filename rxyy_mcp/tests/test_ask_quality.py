# -*- coding: utf-8 -*-
"""选项闸的复现测试 —— 语料全部是 08-07 / 08-24 真实发给 rxyy 的提问。

事故本身：rxyy 08-24 说「有时候 agent 会提问：一堆选项，有时候我都不知道怎么选，
后果是什么……给一堆雷同的或者给一堆其实那些都需要做的选项，就很容易让我摸不着
头脑」。当天最后一次提问是七条，里面还带一条「都干：落常驻区重启 + 清 18 份冲突
副本」——有这条就等于承认前面那些根本不互斥。

所以这里不造样本，直接把当天聊天记录里的选项集抄进来分成两组：
- BAD：确实该拦的（伞形 / 超量 / 雷同）
- GOOD：形状本来就对的，一条都不许误伤——闸门误伤比放过更贵，因为 agent 会
  被卡在一个它改不动的判据上。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import ask_quality  # noqa: E402
import server  # noqa: E402


# ---------- 真实语料 ----------

# 08-07 09:35 进度盘点
BAD_STOCKTAKE = [
    "先换装（跑 _swap.ps1 -SwapOnly）",
    "先把两笔改动提交了",
    "验雷神那 60 秒",
    "更新 progress.md / task_plan.md",
    "都干：换装 + 提交 + 更新文档",
    "结束",
]

# 08-07 10:10 换装收尾
BAD_AFTER_SWAP = [
    "删掉那 34 份冲突副本",
    "先处理 AICodebrain 落后 4 笔",
    "清 dist_export / backup / scripts 临时物",
    "验雷神那 60 秒",
    "build.ps1 加固（0 字节 app.log 放行）",
    "都干",
    "结束",
]

# 08-07 10:38 团队功能三个洞
BAD_TEAM_FIXES = [
    "先做修法 1：转告搭 zt 的回程车（最划算，改动最小）",
    "修法 1+3 都做（递话提速 + 命名强制）",
    "修法 2：看板补上终端改动那半",
    "团队功能先放着，等 d66f1bf0 提交完你来重打包+换装",
    "先把今天这些结论写进 task_plan.md",
    "结束",
]

# 08-07 17:01 面板按项目分之后
BAD_CLEAN_BUILD = [
    "开打干净包（我先喊 83d106bf 让出 39090）",
    "先把那 21 个存量死壳清了，并把「机器消息不算真实消息」根治掉",
    "顶层也改成按项目分（锁/席位/黑板一起搬，单开一轮）",
    "推送 a343536 + e157b02",
    "先看一会控制台效果再说",
    "还有别的活派给你",
    "结束",
]

# 08-24 14:22 孤儿 WIP
BAD_ORPHAN_WIP = [
    "把孤儿 WIP 分批提交（先提 08-21 打包自启+ssh cwd 那批，它已在生产跑 3 天）",
    "全部 5 批都认领清理，一批一个 commit",
    "修团队功能：分组加事实校验（看它真在改哪个仓/领的哪张卡）",
    "修团队功能：脏文件标出「最后编辑会话 + 已离线」，孤儿 WIP 自动告警",
    "两个洞都堵，孤儿 WIP 也一并提",
    "先别动，我自己看一眼",
]

# 08-24 16:20 —— rxyy 就是看到这一屏才提的意见
BAD_LAST_STRAW = [
    "外科式落常驻区 + 重启一次 hub，我现在就要看面板",
    "先别重启，等其他 agent 歇下来再说",
    "删掉那 2 份搞坏 pytest 的冲突副本",
    "18 份在 git 历史里的冲突副本一并清掉",
    "两个外项目文件帮我挪到它们自己仓去",
    "外项目文件先放着，我自己处理",
    "都干：落常驻区重启 + 清 18 份冲突副本",
]

# 08-24 09:33 接手盘点：两条真选项 + 结束
GOOD_HANDOVER = [
    "好，结束这个会话",
    "代我验收那张团队面板卡（393ee05）",
    "还有别的活派给你",
]

# 08-24 12:14 三件交付后
GOOD_AFTER_DELIVERY = [
    "结束",
    "打个新包换装，我要在界面上看",
    "工单页还缺功能，我说给你",
    "飞鸽/任务站还想再补",
]

# 08-24 12:53 常驻区怎么落 —— 四条真互斥，这组是正面样板
GOOD_LIVE_AREA = [
    "把我那三笔外科式打进常驻区，重启一次 hub（队友未提交的改动保留）",
    "只热补输入框那一处（零重启，刷新页面即生效）",
    "等队友把rxyy MCP 提交完再统一刷",
    "先别管常驻区，我说工单/飞鸽还缺什么",
]

# 08-07 15:13 推送 + BOM —— 措辞高度重叠但确是三条不同的路，不许误判成雷同
GOOD_BOM = [
    "推送 23960fe，并顺手把 BOM 那个也收了（推荐）",
    "只推送 23960fe，BOM 那个不管",
    "先别推，只把 BOM 那个修了一起提",
    "还有别的活派给你",
    "结束",
]

# 报到壳每次都发的那一组，被拦了整个控制台就报不了到
GOOD_CHECKIN = ["开始任务", "结束"]


class UmbrellaOptionMeansItWasNeverAChoice(unittest.TestCase):
    """形状 1：有「都干」就说明其余各条不互斥。"""

    def test_the_last_straw_question_is_caught(self):
        problems = ask_quality.review(BAD_LAST_STRAW)
        self.assertTrue(any("伞形" in p for p in problems), problems)

    def test_bare_all_option_counts_even_without_a_list(self):
        self.assertTrue(ask_quality.is_umbrella("都干"))

    def test_all_five_batches_phrasing(self):
        self.assertTrue(ask_quality.is_umbrella("全部 5 批都认领清理，一批一个 commit"))

    def test_two_holes_both_plugged_phrasing(self):
        self.assertTrue(ask_quality.is_umbrella("两个洞都堵，孤儿 WIP 也一并提"))

    def test_plus_sign_alone_is_not_umbrella(self):
        # 「上线 + 紧接着打包」是一条连贯动作，不是把别人几条并起来
        self.assertFalse(ask_quality.is_umbrella("上线 + 紧接着就打那个干净包（一次断完）"))

    def test_waiting_for_others_to_commit_is_not_umbrella(self):
        # 「等他们都提交完」里的「都提」说的是别人，不是把选项并起来
        self.assertFalse(ask_quality.is_umbrella("先别打，等他们都提交完我再打一个干净的"))

    def test_one_batch_cleanup_is_not_umbrella(self):
        self.assertFalse(ask_quality.is_umbrella("18 份在 git 历史里的冲突副本一并清掉"))

    def test_terminal_option_never_umbrella(self):
        self.assertFalse(ask_quality.is_umbrella("结束"))


class TooManyThingsToWeigh(unittest.TestCase):
    """形状 2：要动手的选项超过 4 条。"""

    def test_clean_build_question_had_six(self):
        problems = ask_quality.review(BAD_CLEAN_BUILD)
        self.assertTrue(any("超过" in p for p in problems), problems)

    def test_terminal_options_do_not_eat_the_budget(self):
        opts = ["改 A（会重启一次）", "改 B（零重启）", "先不改，我再想想", "结束"]
        self.assertEqual([], ask_quality.review(opts))

    def test_exactly_four_actionable_passes(self):
        self.assertEqual([], ask_quality.review(GOOD_LIVE_AREA))


class NearIdenticalOptions(unittest.TestCase):
    """形状 3：雷同。阈值必须高到不误伤真正互斥的三条路。"""

    def test_bom_trio_is_not_duplicates(self):
        self.assertEqual([], ask_quality.find_duplicates(GOOD_BOM))

    def test_one_option_swallowed_by_another(self):
        opts = ["推送 a343536（只推我这笔）", "推送 a343536（只推我这笔），顺手清临时物"]
        self.assertTrue(ask_quality.find_duplicates(opts))

    def test_short_options_are_never_compared(self):
        # 「推送」「重启」这类短句天然像，比了必错
        self.assertEqual([], ask_quality.find_duplicates(["推送", "重启", "结束"]))


class GoodQuestionsMustGetThrough(unittest.TestCase):
    """误伤比放过贵：这几组一条都不许拦。"""

    def test_checkin_shell_options(self):
        self.assertEqual([], ask_quality.review(GOOD_CHECKIN))

    def test_handover_stocktake(self):
        self.assertEqual([], ask_quality.review(GOOD_HANDOVER))

    def test_after_delivery(self):
        self.assertEqual([], ask_quality.review(GOOD_AFTER_DELIVERY))

    def test_live_area_choice(self):
        self.assertEqual([], ask_quality.review(GOOD_LIVE_AREA))

    def test_push_plus_bom(self):
        self.assertEqual([], ask_quality.review(GOOD_BOM))

    def test_free_form_question_without_options(self):
        self.assertEqual([], ask_quality.review([]))

    def test_single_option(self):
        self.assertEqual([], ask_quality.review(["结束"]))


class EveryBadQuestionFromThatDayIsCaught(unittest.TestCase):
    def test_all_six_real_bad_sets(self):
        corpus = {
            "08-07 进度盘点": BAD_STOCKTAKE,
            "08-07 换装收尾": BAD_AFTER_SWAP,
            "08-07 团队三洞": BAD_TEAM_FIXES,
            "08-07 干净包": BAD_CLEAN_BUILD,
            "08-24 孤儿 WIP": BAD_ORPHAN_WIP,
            "08-24 压垮那次": BAD_LAST_STRAW,
        }
        for name, opts in corpus.items():
            with self.subTest(name):
                self.assertTrue(ask_quality.review(opts), name)


class BounceNeverDeadlocksTheConversation(unittest.TestCase):
    """安全阀：拦一次就够，第二次必须放行——问不到人比问得难看严重得多。"""

    def setUp(self):
        ask_quality._last_bounce.clear()

    def tearDown(self):
        ask_quality._last_bounce.clear()

    def test_first_call_is_bounced(self):
        self.assertIsNotNone(
            ask_quality.bounce_text(BAD_LAST_STRAW, "b4eff2ee", now=1000.0))

    def test_second_call_goes_through_even_if_still_bad(self):
        ask_quality.bounce_text(BAD_LAST_STRAW, "b4eff2ee", now=1000.0)
        self.assertIsNone(
            ask_quality.bounce_text(BAD_LAST_STRAW, "b4eff2ee", now=1005.0))

    def test_other_conversations_are_not_muted_by_mine(self):
        ask_quality.bounce_text(BAD_LAST_STRAW, "b4eff2ee", now=1000.0)
        self.assertIsNotNone(
            ask_quality.bounce_text(BAD_LAST_STRAW, "be748934", now=1001.0))

    def test_cooldown_expires(self):
        ask_quality.bounce_text(BAD_LAST_STRAW, "b4eff2ee", now=1000.0)
        later = 1000.0 + ask_quality.BOUNCE_COOLDOWN + 1
        self.assertIsNotNone(
            ask_quality.bounce_text(BAD_LAST_STRAW, "b4eff2ee", now=later))

    def test_good_options_are_never_bounced(self):
        self.assertIsNone(ask_quality.bounce_text(GOOD_LIVE_AREA, "b4eff2ee", now=1000.0))

    def test_bounce_text_says_what_to_do_next(self):
        text = ask_quality.bounce_text(BAD_LAST_STRAW, "b4eff2ee", now=1000.0)
        self.assertIn("直接做完", text)
        self.assertIn("互相排斥", text)

    def test_table_does_not_grow_without_bound(self):
        for i in range(400):
            ask_quality.bounce_text(BAD_LAST_STRAW, "conv%d" % i, now=1000.0 + i)
        self.assertLessEqual(len(ask_quality._last_bounce), 400)


class ZhiActuallyStopsTheBadQuestion(unittest.TestCase):
    """闸门真接在 zhi 上——只测模块不接线的话，rxyy 那头一点变化都没有。

    这几条是本次事故的复现测试：接线之前它们全红（坏选项照样落到 BRIDGE.ask，
    也就是照样弹到 rxyy 眼前）。
    """

    def setUp(self):
        ask_quality._last_bounce.clear()
        self.asked = []

    def tearDown(self):
        ask_quality._last_bounce.clear()

    def _call(self, message, options, conv="b4eff2ee"):
        def fake_ask(*a, **kw):
            self.asked.append((a, kw))
            return {}

        with patch.object(server.BRIDGE, "ask", fake_ask), \
                patch.object(server.BRIDGE, "_schedule_idle_clear", lambda *_a: None), \
                patch.object(server, "wait_if_frozen", lambda: None), \
                patch.object(server, "DISABLED", False):
            return server.tool_zhi({"message": message,
                                    "predefined_options": options,
                                    "conversation_id": conv})

    def test_bad_question_never_reaches_the_user(self):
        out = self._call("这几件先干哪个？", BAD_LAST_STRAW)
        self.assertEqual([], self.asked)
        self.assertIn("选项闸", out[0]["text"])

    def test_good_question_is_delivered_untouched(self):
        self._call("常驻区那半怎么落？", GOOD_LIVE_AREA)
        self.assertEqual(1, len(self.asked))
        self.assertEqual(GOOD_LIVE_AREA, self.asked[0][0][1])

    def test_checkin_shell_is_never_blocked(self):
        self._call("📍 已就位", GOOD_CHECKIN)
        self.assertEqual(1, len(self.asked))

    def test_question_without_options_is_never_blocked(self):
        self._call("这条报错你见过吗？", [])
        self.assertEqual(1, len(self.asked))

    def test_keepalive_resume_is_not_re_screened(self):
        # 续期重呼 message 为空、选项原样带着，再拦一次就把保活链打断了
        self._call("", BAD_LAST_STRAW)
        self.assertEqual(1, len(self.asked))

    def test_resend_gets_through_so_the_user_is_never_cut_off(self):
        self._call("这几件先干哪个？", BAD_LAST_STRAW)
        self.assertEqual([], self.asked)
        self._call("这几件先干哪个？", BAD_LAST_STRAW)
        self.assertEqual(1, len(self.asked))


if __name__ == "__main__":
    unittest.main()
