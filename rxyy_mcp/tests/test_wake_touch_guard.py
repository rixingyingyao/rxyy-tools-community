# -*- coding: utf-8 -*-
"""touch-wake 护栏：有在飞 zhi 时不做全局唤醒触碰——08-13 reinit 风暴事故复现。

现场（根治方案书第七节）：23:42-23:44 hub 为追一个 23:46 自己就回连的掉队会话
（4fb76917），2 分钟内三次触碰 mcp.json 全局唤醒——每次强拆本机所有窗口的 MCP
会话，把在飞长挂 zhi 撕断，客户端打进 initialize 风暴（server 日志 initialize
成对出现的时刻与三次触碰一一对应）。

根治：唤醒掉队会话的全局触碰（重启后唤醒 / 绿灯会话通道死的紧急自愈）走 _wake_touch
护栏——有别人的在飞 zhi 时绝不触碰（掉队会话本会按重试纪律自愈回连），且两次触碰
至少隔 WAKE_TOUCH_DEBOUNCE_SECS。端点自身离线→上线的触碰不受限（那时全员 zhi
已随端点一起断，无可误伤）。
"""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub  # noqa: E402

WS = r"d:\桌面\working\cursor工作流"


def _sess(sid, conv, name, *, connected=True, pending=False, detached=False,
          archived=False, last_hb=None, auto_reconnect_blocked=False):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = sid, conv, name
    s.cwd = s.task_root = WS
    s.connected = connected
    s.pending = {"id": "q1"} if pending else None
    s.detached = detached
    s.archived = archived
    s.auto_reconnect_blocked = auto_reconnect_blocked
    s.last_heartbeat = time.time() if last_hb is None else last_hb
    s.lock = threading.Lock()
    return s


def _with(sessions, **cfg):
    d = {s.id: s for s in sessions}
    base = {"token_freeze": False}
    base.update(cfg)
    return [
        patch.object(hub.HUB, "sessions", d),
        patch.object(hub.HUB, "order", list(d)),
        patch.object(hub.HUB, "cfg", base),
        patch.object(hub, "log_event", lambda *a, **k: None),
    ]


class HasInflightZhi(unittest.TestCase):
    def test_connected_pending_is_inflight(self):
        s = _sess("a", "c1", "在等回复", connected=True, pending=True)
        for p in _with([s]):
            p.start()
        self.addCleanup(patch.stopall)
        self.assertIsNotNone(hub.HUB._has_inflight_zhi())

    def test_detached_pending_is_not_inflight(self):
        # 脱离期：MCP 已提前返回，此刻没有挂着的请求可被撕断
        s = _sess("a", "c1", "脱离中", connected=True, pending=True, detached=True)
        for p in _with([s]):
            p.start()
        self.addCleanup(patch.stopall)
        self.assertIsNone(hub.HUB._has_inflight_zhi())

    def test_disconnected_pending_is_not_inflight(self):
        s = _sess("a", "c1", "断了", connected=False, pending=True)
        for p in _with([s]):
            p.start()
        self.addCleanup(patch.stopall)
        self.assertIsNone(hub.HUB._has_inflight_zhi())

    def test_exclude_id_is_skipped(self):
        s = _sess("a", "c1", "自己", connected=True, pending=True)
        for p in _with([s]):
            p.start()
        self.addCleanup(patch.stopall)
        self.assertIsNone(hub.HUB._has_inflight_zhi(exclude_id="a"))


class WakeTouchGuard(unittest.TestCase):
    def test_inflight_zhi_blocks_the_global_touch(self):
        # 核心复现：追掉队会话时，别人正有在飞 zhi → 绝不全局触碰
        busy = _sess("a", "c1", "在飞zhi", connected=True, pending=True)
        touch = MagicMock(return_value=True)
        for p in _with([busy]):
            p.start()
        self.addCleanup(patch.stopall)
        with patch.object(hub, "touch_mcp_json", touch):
            got = hub.HUB._wake_touch("追掉队会话")
        self.assertFalse(got)
        touch.assert_not_called()

    def test_touches_when_all_quiet(self):
        idle = _sess("a", "c1", "待机", connected=True, pending=False)
        touch = MagicMock(return_value=True)
        for p in _with([idle]):
            p.start()
        self.addCleanup(patch.stopall)
        hub.HUB._wake_touch_last = 0
        with patch.object(hub, "touch_mcp_json", touch):
            got = hub.HUB._wake_touch("全静默可唤醒")
        self.assertTrue(got)
        touch.assert_called_once()

    def test_debounce_blocks_rapid_second_touch(self):
        idle = _sess("a", "c1", "待机", connected=True, pending=False)
        touch = MagicMock(return_value=True)
        for p in _with([idle]):
            p.start()
        self.addCleanup(patch.stopall)
        hub.HUB._wake_touch_last = time.time()  # 刚触碰过
        with patch.object(hub, "touch_mcp_json", touch):
            got = hub.HUB._wake_touch("紧接着又追一次")
        self.assertFalse(got)
        touch.assert_not_called()

    def test_token_freeze_blocks(self):
        idle = _sess("a", "c1", "待机", connected=True, pending=False)
        touch = MagicMock(return_value=True)
        for p in _with([idle], token_freeze=True):
            p.start()
        self.addCleanup(patch.stopall)
        hub.HUB._wake_touch_last = 0
        with patch.object(hub, "touch_mcp_json", touch):
            self.assertFalse(hub.HUB._wake_touch("冻结期不动"))
        touch.assert_not_called()


class RestartWakeRespectsInflight(unittest.TestCase):
    def test_restart_wake_with_inflight_zhi_does_not_touch(self):
        # 方案书第七节复现：有在飞 zhi 时重启 hub，唤醒掉队会话不触发全局 touch
        busy = _sess("a", "c1", "在飞zhi", connected=True, pending=True)
        straggler = _sess("b", "c2", "掉队的", connected=False,
                          last_hb=time.time() - 60)  # 近 15 分钟有心跳=该被唤醒
        touch = MagicMock(return_value=True)
        for p in _with([busy, straggler]):
            p.start()
        self.addCleanup(patch.stopall)
        hub.HUB._wake_touch_last = 0
        with patch.object(hub, "touch_mcp_json", touch):
            hub.HUB.wake_stragglers_after_restart()
        touch.assert_not_called()

    def test_restart_wake_touches_when_no_inflight(self):
        straggler = _sess("b", "c2", "掉队的", connected=False,
                          last_hb=time.time() - 60)
        touch = MagicMock(return_value=True)
        for p in _with([straggler]):
            p.start()
        self.addCleanup(patch.stopall)
        hub.HUB._wake_touch_last = 0
        with patch.object(hub, "touch_mcp_json", touch):
            hub.HUB.wake_stragglers_after_restart()
        touch.assert_called_once()

    def test_no_straggler_no_touch(self):
        live = _sess("a", "c1", "在线", connected=True, pending=False)
        touch = MagicMock(return_value=True)
        for p in _with([live]):
            p.start()
        self.addCleanup(patch.stopall)
        hub.HUB._wake_touch_last = 0
        with patch.object(hub, "touch_mcp_json", touch):
            hub.HUB.wake_stragglers_after_restart()
        touch.assert_not_called()

    def test_confirmed_offline_session_is_not_woken_after_restart(self):
        ended = _sess("b", "c2", "已确认离线", connected=False,
                      last_hb=time.time() - 60, auto_reconnect_blocked=True)
        touch = MagicMock(return_value=True)
        for p in _with([ended]):
            p.start()
        self.addCleanup(patch.stopall)
        hub.HUB._wake_touch_last = 0
        with patch.object(hub, "touch_mcp_json", touch):
            hub.HUB.wake_stragglers_after_restart()
        touch.assert_not_called()


class EmergencyReloadRespectsInflight(unittest.TestCase):
    def test_emergency_reload_yields_to_other_inflight_zhi(self):
        # 绿灯会话 s 通道死了想触碰救它，但别的窗口正有在飞 zhi → 让路，s 靠重试自愈
        dead = _sess("s", "cs", "刚掉线", connected=False, pending=False)
        other_busy = _sess("a", "c1", "在飞zhi", connected=True, pending=True)
        touch = MagicMock(return_value=True)
        for p in _with([dead, other_busy]):
            p.start()
        self.addCleanup(patch.stopall)
        hub.HUB._wake_touch_last = 0
        hub.HUB._emg_reload_last = 0
        with patch.object(hub, "touch_mcp_json", touch), \
             patch.object(time, "sleep", lambda *_: None):
            hub.HUB._emergency_reload(dead)
        touch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
