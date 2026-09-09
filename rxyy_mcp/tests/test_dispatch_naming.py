# -*- coding: utf-8 -*-
"""派活自动命名：任务安排站派发的 tab 名一次到位。

08-27 用户问「不能让这个名字一次就正确吗」——此前 _auto_label_on_dispatch 把
派发文案整句「【任务派发】修转播延时 | 优先级…」截 24 字当 tab 名/分工，要等
agent 首次 zt 自报真名才换正，中间满屏口头禅、四个 tab 分不清谁是谁。

修法：派发文案是结构化的，名字直接取人写的标题——
- 「【任务派发】<标题>」 → <标题>；
- 「【批量派发】共 N 个任务 …【标题】<第一条>」 → 「<第一条>（批量N件）」；
- 非派发文案（用户手打的一句话）维持旧口径：第一句截 24 字。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub


class _S:
    def __init__(self, name="待命·cursor工作流"):
        self.id = "s1"
        self.conv_key = "convA"
        self.name = name
        self.name_locked = False
        self.agent_named = False
        self.rev = 0


class DispatchNamingTests(unittest.TestCase):
    def _label(self, s, text):
        with (patch.object(hub.HUB, "cfg", {"max_messages": 200}),
              patch.object(hub.HUB, "sessions", {}),
              patch.object(hub, "save_config", lambda cfg: None),
              patch.object(hub, "log_event", lambda *a, **k: None),
              patch.object(hub.Api, "_remember_label", lambda a, x: None)):
            hub.Api()._auto_label_on_dispatch(s, text)
            return dict(hub.HUB.cfg.get("team_assign") or {})

    def test_single_dispatch_uses_the_human_written_title(self):
        s = _S()
        assign = self._label(s, "【任务派发】修复转播页白屏\n项目仓库：D:\\x（请只在该仓库内修改）\n"
                                "优先级：中 | 编号：abc123 | 需求人：阿龟\n\n【任务描述】\n白屏了")
        self.assertEqual("修复转播页白屏", s.name, "名字必须一次就是人写的标题")
        self.assertEqual("修复转播页白屏", assign.get("convA"), "分工同步用标题，不吃口头禅")

    def test_batch_dispatch_names_by_first_title_with_count(self):
        s = _S()
        text = ("【批量派发】共 3 个任务\n项目仓库：D:\\x（请只在该仓库内修改）\n"
                "先通读全部任务再动手：能合并的改动一起做。\n\n"
                "────── 任务 1/3 ──────\n【标题】修复转播页白屏\n编号：a1\n【描述】\n白屏\n"
                "────── 任务 2/3 ──────\n【标题】修延迟\n编号：a2\n【描述】\n延迟")
        self._label(s, text)
        self.assertEqual("修复转播页白屏（批量3件）", s.name)

    def test_plain_sentence_keeps_the_old_24_char_cut(self):
        s = _S()
        self._label(s, "帮我看看转播页为什么白屏，顺便把控制台报错也贴出来一起分析一下")
        self.assertEqual(hub.Api._one_line(
            "帮我看看转播页为什么白屏，顺便把控制台报错也贴出来一起分析一下",
            hub.Api.AUTO_ASSIGN_LEN), s.name)
        self.assertTrue(s.name.endswith("…"), "旧口径：真截断补省略号")

    def test_long_title_is_still_cut_to_assign_len(self):
        s = _S()
        title = "把转播控制台的所有页面全部重构成响应式并适配手机端布局"
        self._label(s, "【任务派发】{}\n优先级：中\n【任务描述】\n干".format(title))
        self.assertEqual(hub.Api._one_line(title, hub.Api.AUTO_ASSIGN_LEN), s.name)

    def test_user_named_tab_is_never_touched(self):
        s = _S(name="我自己起的名")   # 不匹配壳名 → 不动
        self._label(s, "【任务派发】修复转播页白屏\n【任务描述】\n白屏")
        self.assertEqual("我自己起的名", s.name)


if __name__ == "__main__":
    unittest.main()
