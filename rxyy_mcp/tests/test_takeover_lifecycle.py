# -*- coding: utf-8 -*-
"""接手生命周期收口（四账合并方案 B 步，2026-08-26）。

A 步收了「写账」，B 步收「销账」：handed_off_to 只表示【交接在途】——
落地那一刻（takeover_landed）连同 ⏳ 牌一起清，不再终身挂在活 tab 身上。
08-26 拆分手术的实景：b4eff2ee 明明活着干活，身上还挂着旧交接标记，
按它旧 ID/旧名的转告会被 _follow_handoff 转走、每次有人拿原 ID 报到它都
进 _reap_handed_off_shell 的候选名单——账不销，事故就换个入口重演。

配套：_shell_takeover_target 不再读壳身上的 handed_off_to 残留字段，
证据只认「贴给它的接手提示词正文」（队列+消息，派单那一刻写下的事实）
与「接手台账」（A 步落盘，跨重启）。归档 tab 的 handed_off_to 是
_handover_out 留下的史实指针（转告跟随、交接误判自愈都靠它），落地不碰。
"""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub  # noqa: E402


def _s(sid, conv, name, handed="", archived=False, dispatched=None):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = sid, conv, name
    s.handed_off_to = handed
    s.archived = archived
    s.takeover_dispatched = dispatched
    s.rev = 0
    s.queued = []
    s.messages = []
    s.pending = None
    s.connected = True
    s.client = None
    s.lock = threading.Lock()
    return s


class LandingClearsInFlightMarkers(unittest.TestCase):
    """takeover_landed = 销账时刻：⏳ 牌与指着本会话的在途标记一起摘。"""

    def _land(self, target, sessions):
        d = {x.id: x for x in sessions}
        with patch.object(hub.HUB, "sessions", d):
            hub.HUB.takeover_landed(target)

    def test_landing_clears_stale_marker_on_live_session(self):
        """b4eff2ee 实景：活着干活的 tab 挂着指向本会话的旧标记——落地即清。"""
        orig = _s("d", "b4eff2ee", "rxyy tools·全面体检",
                  dispatched={"to_name": "待命·乙", "to_conv": "c2",
                              "ts": 0.0, "warned": True})
        worker = _s("w", "c0ffee00", "干活中的 tab", handed="b4eff2ee")
        self._land(orig, [orig, worker])
        self.assertEqual("", worker.handed_off_to,
                         "落地后标记就该销账，留着会让按旧 ID 的转告被错误转走")
        self.assertGreater(worker.rev, 0, "面板「已交给 X」得当拍消失")

    def test_landing_without_badge_still_clears_markers(self):
        """手工贴词没有 ⏳ 牌（takeover_dispatched=None），销账不能因此跳过。"""
        orig = _s("d", "b4eff2ee", "rxyy tools·全面体检")
        worker = _s("w", "c0ffee00", "干活中的 tab", handed="b4eff2ee")
        self._land(orig, [orig, worker])
        self.assertEqual("", worker.handed_off_to)

    def test_landing_keeps_marker_on_archived_handover_tab(self):
        """归档 tab 的 handed_off_to 是史实指针：按旧名转告跟随、误判自愈都靠它。"""
        orig = _s("d", "627f39b7", "直播项目")
        old = _s("o", "fcd1ff60", "团队面板修复", handed="627f39b7",
                 archived=True)
        self._land(orig, [orig, old])
        self.assertEqual("627f39b7", old.handed_off_to)

    def test_landing_still_takes_the_badge_down(self):
        orig = _s("d", "cd", "挂了的",
                  dispatched={"to_name": "待命·乙", "to_conv": "c2",
                              "ts": 0.0, "warned": False})
        self._land(orig, [orig])
        self.assertIsNone(orig.takeover_dispatched)
        self.assertGreater(orig.rev, 0)


class ShellTargetReadsEvidenceNotResidue(unittest.TestCase):
    """_shell_takeover_target：证据 = 提示词正文 + 台账；单字段残留不算数。"""

    PROMPT = "<pre>%s\nconversation_id=「76a1cf4d」</pre>" % hub.TAKEOVER_PROMPT_HEAD

    def test_prompt_beats_residual_marker(self):
        """标记指错了（快照残留）：提示词正文点名的才是它真被派去接的活。"""
        x = _s("b", "34892f47", "待命·cursor工作流·2f47", handed="zzzz9999")
        x.messages = [{"role": "user", "html": self.PROMPT}]
        self.assertEqual("76a1cf4d", hub.HUB._shell_takeover_target(x))

    def test_marker_alone_is_not_evidence(self):
        """没有提示词、台账也没登记——光一个残留字段不构成「它被派去接谁」。"""
        x = _s("b", "34892f47", "待命·cursor工作流·2f47", handed="cd")
        with patch.object(hub.HUB, "takeover_ledger", {}, create=True):
            self.assertEqual("", hub.HUB._shell_takeover_target(x))

    def test_ledger_backs_the_target_when_prompt_is_gone(self):
        """崩溃恢复/消息被裁后提示词没了：台账（落盘）里的退休登记还能指路。"""
        x = _s("b", "34892f47", "待命·cursor工作流·2f47")
        ledger = {"34892f47": {"succ": "76a1cf4d", "old_name": "",
                               "succ_label": "", "ts": 1.0,
                               "why": "派接手即归并壳"}}
        with patch.object(hub.HUB, "takeover_ledger", ledger, create=True):
            self.assertEqual("76a1cf4d", hub.HUB._shell_takeover_target(x))


if __name__ == "__main__":
    unittest.main()
