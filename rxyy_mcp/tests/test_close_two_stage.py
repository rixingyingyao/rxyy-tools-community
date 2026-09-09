# -*- coding: utf-8 -*-
"""两段式关闭标签（08-04 用户要求）。

原来点一次 × 就把 tab 从列表里抹掉，误关只能去「🕘 历史」翻记录再接手。
现在：第一次 × = 收进「已结束」组（会话还在，能回看能接手），第二次 × 才真删。
本测试锁死两段的边界，外加三个坑：关掉的 tab 不许被心跳复活、重启后仍待在
「已结束」组、原对话ID重新报到要撤销归档。
"""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub


class _FakeClient:
    def __init__(self, peer_ip=None, cwd=r"d:\Desktop\cursor工作流", pid=111):
        self.peer_ip = peer_ip
        self.cwd = cwd
        self.pid = pid
        self.closed_convs = {}
        self.sessions = {}
        self.sent = []

    def send(self, obj):
        self.sent.append(obj)


def _sess(sid, conv, name, connected=False, client=None, pending=None):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = sid, conv, name
    s.name_locked = False
    s.cwd = r"d:\Desktop\cursor工作流"
    s.task_root = s.cwd
    s.peer_ip = None
    s.pid = 111
    s.client = client
    s.connected = connected
    s.archived = False
    s.end_reason = ""
    s.pending = pending
    s.pending_lost = False
    s.recon_deadline = None
    s.lost_pending_on_drop = False
    s.processing_since = None
    s.detached = False
    s.detached_since = None
    s.buffered_reply = None
    s.disconnected_at = None
    s.last_reply_probe = None
    s.last_heartbeat = time.time()
    s.agent_status = ""
    s.agent_activity = ""
    s.agent_status_ts = 0
    s.death_info = None
    s.death_probe_at = 0.0
    s.death_alerts = {}
    s.name_history = []
    s.cursor_uuid = None
    s.uuid_verified = False
    s.cursor_title = None
    s.transcript_path = None
    s.created_ts = time.time()
    s.created_at = "2026-08-04 08:00:00"
    s.file_path = None
    s.library_sent = False
    s.handed_off_to = ""
    s.msg_seq = 8
    s.messages = [{"role": "sys", "ts": "08:00", "html": "对话已开始"}]
    s.queued = []
    s.draft_text = ""
    s.draft_images = []
    s.draft_files = []
    s.rev = 0
    s.lock = threading.Lock()
    if client is not None:
        client.sessions[conv] = s
    return s


class TwoStageCloseTests(unittest.TestCase):
    def setUp(self):
        self.sessions, self.order = {}, []
        self._patches = [
            patch.object(hub.HUB, "sessions", self.sessions),
            patch.object(hub.HUB, "order", self.order),
            patch.object(hub.HUB, "save_state", MagicMock()),
            patch.object(hub.HUB, "log_end", MagicMock()),
            patch.object(hub.HUB, "add_message", MagicMock()),
            patch.object(hub, "log_event", MagicMock()),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])
        self.api = hub.Api()

    def _add(self, s):
        self.sessions[s.id] = s
        self.order.append(s.id)
        return s

    def test_first_x_archives_a_disconnected_tab_instead_of_deleting_it(self):
        s = self._add(_sess("t1", "c1", "待命·cursor工作流"))
        r = self.api.force_close("t1")
        self.assertTrue(r["archived"])
        self.assertIn("t1", self.sessions, "第一次 × 不许把 tab 从列表里删掉")
        self.assertIn("t1", self.order)
        self.assertTrue(s.archived)

    def test_second_x_removes_the_tab_from_the_list(self):
        self._add(_sess("t1", "c1", "待命·cursor工作流"))
        self.api.force_close("t1")
        r = self.api.force_close("t1")
        self.assertTrue(r["removed"])
        self.assertNotIn("t1", self.sessions)
        self.assertNotIn("t1", self.order)

    def test_first_x_on_a_live_tab_ends_the_conversation_but_keeps_the_tab(self):
        cli = _FakeClient()
        s = self._add(_sess("t1", "c1", "在跑的活", connected=True, client=cli,
                            pending={"id": "req1"}))
        r = self.api.force_close("t1")
        self.assertTrue(r["archived"])
        self.assertIn("t1", self.sessions)
        self.assertFalse(s.connected)
        self.assertTrue(s.archived)
        self.assertTrue(cli.closed_convs.get("c1"), "AI 侧必须收到「这对话关了」的标记")
        self.assertEqual("popup_closed", cli.sent[0]["source"])
        self.assertNotIn("c1", cli.sessions)

    def test_heartbeat_or_status_never_resurrects_a_closed_tab(self):
        # 关掉在跑的对话后，它的 MCP 进程还会心跳几轮（每 5s 报一次服务中的 conv）。
        # 认了就会把 tab 重新标成「连接中」弹回列表，第二次 × 还会被挡住。
        cli = _FakeClient()
        s = self._add(_sess("t1", "c1", "在跑的活", connected=True, client=cli))
        self.api.force_close("t1")
        again = _FakeClient()
        self.assertIsNone(hub.HUB._resolve_for_signal(again, "c1"))
        self.assertFalse(s.connected)
        self.assertTrue(self.api.force_close("t1")["removed"],
                        "第二次 × 必须删得掉，不能被假的「仍在连接中」挡住")

    def test_reporting_in_again_with_the_same_conv_id_unarchives(self):
        # 「🤝 接手」就是让别的 agent 用原对话ID报到：tab 得活过来，而不是躺在已结束组
        s = self._add(_sess("t1", "c1", "关掉的活"))
        self.api.force_close("t1")
        with (patch.object(hub.HUB, "maybe_apply_task_name", MagicMock()),
              patch.object(hub.HUB, "yield_zhi", MagicMock()),
              patch.object(hub.HUB, "_reap_handed_off_shell", MagicMock())):
            got = hub.HUB.create_session(_FakeClient(), "c1", "接手方")
        self.assertIs(s, got, "同对话ID不该另开 tab")
        self.assertFalse(s.archived)
        self.assertTrue(s.connected)

    def test_state_tick_leaves_archived_tabs_alone(self):
        # 用户亲手关掉的 tab 不该再被探活/判死：省下每秒读 Cursor 库，也不再响铃
        s = self._add(_sess("t1", "c1", "关掉的活"))
        self.api.force_close("t1")
        probe = MagicMock()
        with patch.object(hub.HUB, "_tick_death_probe", probe):
            hub.HUB._state_tick()
        probe.assert_not_called()
        self.assertTrue(s.archived)
        self.assertFalse(s.connected)

    def test_clean_all_archives_and_purge_empties_the_ended_group(self):
        live = self._add(_sess("t0", "c0", "在跑", connected=True, client=_FakeClient()))
        self._add(_sess("t1", "c1", "断开1"))
        self._add(_sess("t2", "c2", "断开2"))
        r = self.api.close_disconnected()
        self.assertEqual(2, r["archived"])
        self.assertEqual(["t0", "t1", "t2"], self.order, "批量关闭同样只收不删")
        r = self.api.purge_archived()
        self.assertEqual(2, r["removed"])
        self.assertEqual(["t0"], self.order)
        self.assertTrue(live.connected, "在跑的会话不许被顺手清掉")


class ArchivedSurvivesRestartTests(unittest.TestCase):
    def test_archived_tab_stays_in_the_ended_group_after_a_restart(self):
        s = _sess("t1", "c1", "关掉的活")
        s.archived = True
        with tempfile.TemporaryDirectory() as td:
            snap = Path(td) / "snap.json"
            with (patch.object(hub.Hub, "STATE_PATH", snap),
                  patch.object(hub.HUB, "sessions", {"t1": s}),
                  patch.object(hub.HUB, "order", ["t1"])):
                hub.HUB.save_state()
            self.assertTrue(json.loads(snap.read_text(encoding="utf-8"))[0]["archived"])
            restored, order = {}, []
            with (patch.object(hub.Hub, "STATE_PATH", snap),
                  patch.object(hub.HUB, "sessions", restored),
                  patch.object(hub.HUB, "order", order)):
                hub.HUB.load_state()
        r = restored[order[0]]
        self.assertTrue(r.archived)
        self.assertIsNone(r.recon_deadline,
                          "用户关掉的 tab 不该再亮「重连中」等它接回来")


if __name__ == "__main__":
    unittest.main()
