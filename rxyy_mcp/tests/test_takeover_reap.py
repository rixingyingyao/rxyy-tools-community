# -*- coding: utf-8 -*-
"""接手落地自动收起「待命空壳」（_reap_takeover_shell）。

复现 08-03 根因场景：接手者在同一个 Cursor 对话里先报到 NEW_ID（空壳 A），再切
原对话 ID（B 复活并干活）。B 的 cursor_uuid 被 _verify 认领后，A 的 uuid 会被清空
（同一对话 uuid 归 B）。收壳不能再靠「同 uuid」，改靠「A 的报到 ID 落在 B 当前
对话的 composerData 里」——本测试锁死这条链路，防回归。"""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub


def _sess(sid, conv, name, msg_seq, uuid=None, pending=None, queued=None):
    s = hub.Session.__new__(hub.Session)
    s.id = sid
    s.conv_key = conv
    s.name = name
    s.msg_seq = msg_seq
    s.cursor_uuid = uuid
    s.pending = pending
    s.queued = queued or []
    s.client = None
    s.connected = True
    s.lock = threading.Lock()
    return s


class ReapTakeoverShellTests(unittest.TestCase):
    def setUp(self):
        # B：被接手的原会话，有真实历史，uuid 已校准到接手者当前对话 U
        self.B = _sess("tab-B", "27322a27", "待命·cursor工作流2", 20, uuid="U-dialog")
        # A：接手前身空壳，报到用 0e525653，uuid 已被 _verify 清空（None）
        self.A = _sess("tab-A", "0e525653", "待命·cursor工作流", 1, uuid=None)
        self._patches = [
            patch.object(hub.HUB, "sessions", {"tab-B": self.B, "tab-A": self.A}),
            patch.object(hub.HUB, "order", ["tab-B", "tab-A"]),
            patch.object(hub.HUB, "_relocate_takeover_uuid", MagicMock()),
            patch.object(hub.HUB, "_tombstone_shell", MagicMock()),
            patch.object(hub.HUB, "log_end", MagicMock()),
            patch.object(hub, "log_event", MagicMock()),
            # 账本隔离：收壳的 _retire_conv_into 写台账，不隔离会把测试数据
            # 灌进真 HUB 的账并污染同进程后跑的用例（台账兜底指路）
            patch.object(hub.HUB, "takeover_aliases", {}),
            patch.object(hub.HUB, "takeover_ledger", {}, create=True),
            patch.object(hub.Hub, "_save_takeover_aliases", lambda self: None),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    def test_reaps_shell_whose_report_id_is_in_takeover_dialog(self):
        # A 的 uuid 被清成 None，但它的报到 ID（以 conversation_id 参数形态）
        # 出现在 B 当前对话 U 里 → 认出并收起；且必须用参数上下文档查（防裸 ID 污染）
        seen = {}

        def fake_mentions(uid, cid, **kw):
            seen["param_context"] = kw.get("param_context")
            return uid == "U-dialog" and cid == "0e525653"

        with patch.object(hub, "composer_mentions_conv", side_effect=fake_mentions):
            hub.HUB._reap_takeover_shell(self.B)
        self.assertTrue(seen.get("param_context"))
        self.assertNotIn("tab-A", hub.HUB.sessions)   # 空壳已收
        self.assertIn("tab-B", hub.HUB.sessions)      # 原会话还在
        self.assertNotIn("tab-A", hub.HUB.order)

    def test_reaps_shell_by_same_uuid_without_db(self):
        # A 的 uuid 还等于 U（_verify 尚未清）→ 直接同 uuid 命中，不必查库
        self.A.cursor_uuid = "U-dialog"
        with patch.object(hub, "composer_mentions_conv",
                          side_effect=AssertionError("同 uuid 就不该再查库")):
            hub.HUB._reap_takeover_shell(self.B)
        self.assertNotIn("tab-A", hub.HUB.sessions)

    def test_keeps_shell_of_unrelated_dialog(self):
        # 另一个不相干的待命空壳（报到 ID 不在 B 对话里）绝不能被误收
        with patch.object(hub, "composer_mentions_conv", return_value=False):
            hub.HUB._reap_takeover_shell(self.B)
        self.assertIn("tab-A", hub.HUB.sessions)

    def test_no_reap_when_session_has_no_history(self):
        # s 自己没历史（msg_seq<=3）= 不是被接手的原会话，不触发收壳
        self.B.msg_seq = 2
        with patch.object(hub, "composer_mentions_conv", return_value=True):
            hub.HUB._reap_takeover_shell(self.B)
        self.assertIn("tab-A", hub.HUB.sessions)

    def test_does_not_reap_a_tab_with_real_work(self):
        # A 上后来被派了真活（有排队消息）→ 不是空壳，保留
        self.A.queued = [{"message": "另派的活"}]
        with patch.object(hub, "composer_mentions_conv", return_value=True):
            hub.HUB._reap_takeover_shell(self.B)
        self.assertIn("tab-A", hub.HUB.sessions)

    def test_reaps_shell_still_waiting_on_checkin_zhi(self):
        # 08-14：壳还挂着报到 zhi，同对话接手落地仍应收掉
        self.A.pending = {
            "id": "q-checkin",
            "message": "📍 cursor工作流 · 对话 0e525653 已就位",
            "options": ["开始任务", "结束"],
        }
        self.A.cursor_uuid = "U-dialog"
        with patch.object(hub, "composer_mentions_conv",
                          side_effect=AssertionError("同 uuid 就不该再查库")):
            hub.HUB._reap_takeover_shell(self.B)
        self.assertNotIn("tab-A", hub.HUB.sessions)
        self.assertIsNone(self.A.pending)

    def test_throttled_within_15s(self):
        # 15s 内重复调用只干一次（防 zt 高频刷库）：先手动占用时间戳
        import time as _t
        self.B._reap_ts = _t.time()
        with patch.object(hub, "composer_mentions_conv", return_value=True):
            hub.HUB._reap_takeover_shell(self.B)
        self.assertIn("tab-A", hub.HUB.sessions)  # 冷却内没动


if __name__ == "__main__":
    unittest.main()
