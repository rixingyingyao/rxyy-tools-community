# -*- coding: utf-8 -*-
"""detach 失联看门狗不清「认领了活」会话的 pending——08-12 用户回复漏读事故复现。

现场（根治方案书第一节·路径2）：keepalive detach 后 agent 干活/未及时续期，
detach_grace 到点被判「本轮失联」，pending 被无条件清掉——用户稍后在控制台的
回复打在「当前没有等待回复的请求」上悬空，或送进已死请求静默丢失。当天用
keepalive_secs=0（纯 SSE 长挂不 detach）缓解，但 detach 路径还在：配置一旦回退
或旧版 agent 接入，同一坑再踩。

根治（方案书第三节·路径2）：认领了活的会话 detach 超宽限只挂起、不清 pending/
身份——提问与绿灯保留，用户回复走 buffered_reply 缓存，agent 续期重呼时原样
交付；失联误判的代价（绿灯多亮一会儿）远小于丢用户的话。
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


def _sess(sid, conv, name, claimed=False, claimed_ts=0.0):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = sid, conv, name
    s.name_locked = False
    s.cwd = s.task_root = WS
    s.peer_ip = None
    s.pid = 111
    s.client = None
    s.connected = True
    s.archived = False
    s.end_reason = ""
    s.pending = {"id": "req1", "message": "报到", "options": [], "created": time.time()}
    s.pending_lost = False
    s.recon_deadline = None
    s.lost_pending_on_drop = False
    s.processing_since = None
    s.detached = True
    s.detached_since = time.time() - 3600  # 远超宽限
    s.buffered_reply = None
    s.disconnected_at = None
    s.last_reply_probe = None
    s.last_heartbeat = time.time()
    s.agent_status = ""
    s.agent_activity = ""
    s.agent_status_ts = time.time() if claimed else 0
    s.claimed_task_ts = claimed_ts
    s.agent_named = False
    s.shell_born = True
    s.death_info = None
    s.death_probe_at = time.time()  # 限频窗口内，跳过真探测
    s.death_alerts = {}
    s.name_history = []
    s.cursor_uuid = None
    s.uuid_verified = False
    s.cursor_title = None
    s.transcript_path = None
    s.takeover_dispatched = None
    s.created_ts = time.time()
    s.created_at = "2026-08-12 20:00:00"
    s.file_path = None
    s.library_sent = False
    s.handed_off_to = ""
    s.msg_seq = 2
    s.machine_seq = 0
    s.real_seq = 2
    s.messages = []
    s.queued = []
    s.draft_text = ""
    s.draft_images = []
    s.draft_files = []
    s.rev = 0
    s.lock = threading.Lock()
    return s


def _tick_with(s):
    with patch.object(hub.HUB, "sessions", {s.id: s}), \
         patch.object(hub.HUB, "order", [s.id]), \
         patch.object(hub.HUB, "cfg", {"detach_grace_secs": 30, "ide_active_secs": 0,
                                       "max_messages": 20}), \
         patch.object(hub.Hub, "_tick_death_probe", MagicMock()), \
         patch.object(hub.Api, "_agent_liveness", lambda a, x, now: {}), \
         patch.object(hub, "log_event", lambda *a, **k: None), \
         patch.object(hub, "WORKFLOW", None):
        hub.HUB._state_tick()


class DetachWatchdogSparesClaimedSessions(unittest.TestCase):
    def test_zt_claimed_session_keeps_pending_after_grace(self):
        # 修前红：报过 zt 的会话照样被清 pending（漏读事故主形态）
        s = _sess("t1", "c1", "待命·cursor工作流", claimed=True)
        _tick_with(s)
        self.assertIsNotNone(s.pending, "认领了活的会话不许被失联看门狗清 pending")
        self.assertTrue(s.detached, "只挂起：脱离态保留，等续期重呼")

    def test_persisted_claim_mark_also_spares(self):
        s = _sess("t1", "c1", "待命·cursor工作流", claimed_ts=time.time() - 60)
        _tick_with(s)
        self.assertIsNotNone(s.pending)

    def test_virgin_shell_is_still_cleared_as_before(self):
        # 守卫不扩大化：没认领过活的报到壳，超宽限照旧切回待机
        s = _sess("t1", "c1", "待命·cursor工作流")
        _tick_with(s)
        self.assertIsNone(s.pending)
        self.assertFalse(s.detached)
        self.assertTrue(any("失联" in (m.get("html") or "") for m in s.messages))

    def test_user_reply_during_overdue_detach_is_buffered_not_lost(self):
        # 漏读事故的完整链路：超宽限后用户才回话——必须进 buffered_reply 等续期交付
        s = _sess("t1", "c1", "rxyy MCP·根治筹备", claimed=True)
        _tick_with(s)
        with patch.object(hub.HUB, "sessions", {"t1": s}), \
             patch.object(hub.HUB, "cfg", {"max_messages": 20, "history_dir": "."}), \
             patch.object(hub.Hub, "log_user", MagicMock()), \
             patch.object(hub.Hub, "save_msg_images", lambda h, p: []), \
             patch.object(hub.Api, "_with_library_digest",
                          lambda a, x, t, selected=None: (t, False)), \
             patch.object(hub.Api, "_auto_label_on_dispatch",
                          lambda a, x, t, who=None: None), \
             patch.object(hub, "log_event", lambda *a, **k: None):
            r = hub.Api().send_reply("t1", "继续把第八节做完", [], [], False)
        self.assertTrue(r["ok"], "pending 还在，回复就收得进")
        self.assertIsNotNone(s.buffered_reply, "脱离期回复必须缓存等续期交付")
        self.assertIsNotNone(s.pending, "提问保留到交付为止")

    def test_overdue_log_fires_once_per_detach_round(self):
        s = _sess("t1", "c1", "待命·cursor工作流", claimed=True)
        logs = []
        with patch.object(hub.HUB, "sessions", {s.id: s}), \
             patch.object(hub.HUB, "order", [s.id]), \
             patch.object(hub.HUB, "cfg", {"detach_grace_secs": 30, "ide_active_secs": 0,
                                           "max_messages": 20}), \
             patch.object(hub.Hub, "_tick_death_probe", MagicMock()), \
             patch.object(hub.Api, "_agent_liveness", lambda a, x, now: {}), \
             patch.object(hub, "log_event", lambda m, *a, **k: logs.append(m)), \
             patch.object(hub, "WORKFLOW", None):
            hub.HUB._state_tick()
            hub.HUB._state_tick()
        overdue = [m for m in logs if "只挂起不清" in m]
        self.assertEqual(1, len(overdue), "15s 一拍的循环里不许每拍刷一条")


if __name__ == "__main__":
    unittest.main()
