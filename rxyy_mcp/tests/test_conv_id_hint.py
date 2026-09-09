# -*- coding: utf-8 -*-
"""漏传 conversation_id 的调用绝不能沾上别人的 ID——回执和消息本体两半都是。

一个 MCP 进程服务本机所有 Cursor 窗口。08-04 现场：没传 ID 的调用方拿「最后一个
报上来的人」的 ID 填回执，agent 被要求「后续必须沿用」，于是老实改绑到别人的 tab
（ba71f55d → d17f7c53 → … 一路漂）——那次只修了回执这半边（own_conv_id_hint）。
08-12 09:03 又栽在消息本体这半边：OA对接 的 zhi 漏带 ID，5 秒前 defbeed7 刚 zt
过，_ensure_conversation_id 把 defbeed7 补给了它，整段探查汇报连提问带选项落进
video-editor 的 tab，用户当场抓到串台。现在两半共用同一条铁律：显式 ID 原样用、
互不影响；漏传只补「本进程自己 mint 的」那一个，绝不复用别的对话报过的。
"""
import sys
import unittest
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import server  # noqa: E402


class ConvIdHintTests(unittest.TestCase):
    def setUp(self):
        self.bridge = server.HubBridge()

    def test_explicit_id_is_echoed_back(self):
        # 自己传来的，原样回给它：这是它自己的 ID，提醒沿用没问题
        self.assertEqual("aaaa1111", self.bridge.own_conv_id_hint("aaaa1111"))

    def test_minted_id_goes_to_the_caller_it_was_minted_for(self):
        minted = self.bridge._ensure_conversation_id(None)
        self.assertEqual(minted, self.bridge.own_conv_id_hint(None))

    def test_someone_elses_id_is_never_offered(self):
        # 队友带着自己的 ID 来过一趟——它的 ID 不能在进程里留下任何可被
        # 漏传者复用的痕迹
        self.bridge._ensure_conversation_id("bbbb2222")
        self.assertIsNone(self.bridge._minted_conv_id)
        # 此时另一个没传 ID 的调用方，绝不能被告知「你是 bbbb2222」
        self.assertIsNone(self.bridge.own_conv_id_hint(None))

    def test_minted_id_survives_other_agents_traffic(self):
        minted = self.bridge._ensure_conversation_id(None)
        for other in ("cccc3333", "dddd4444", "eeee5555"):
            self.bridge._ensure_conversation_id(other)
        self.assertEqual(minted, self.bridge.own_conv_id_hint(None))

    def test_missing_id_never_reuses_someone_elses(self):
        # 08-12 09:03 串台事故的最小复现：defbeed7 刚带着自己的 ID zt 过，
        # 紧接着一个漏带 ID 的 zhi 进来——旧实现把 defbeed7 补给它，整段
        # 汇报落进 video-editor 的 tab。新语义：漏传者只能拿到本进程
        # mint 的 ID，且稳定复用。
        self.bridge._ensure_conversation_id("defbeed7")
        got = self.bridge._ensure_conversation_id(None)
        self.assertNotEqual("defbeed7", got)
        self.assertEqual(got, self.bridge._ensure_conversation_id(""))


if __name__ == "__main__":
    unittest.main()
