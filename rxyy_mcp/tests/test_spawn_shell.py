# -*- coding: utf-8 -*-
"""「拉起本机 agent」预建会话壳 + 首任务排队（sdk_spawn 派单不丢件）。

设计约定：拉起瞬间预建断开态壳（tab 立即可见、宽限内显示重连中），首个任务
以 who=派单 排进队列；agent 按报到纪律用同 conv_key 首呼 zhi 时，复活的是
同一个壳（绝不另开 tab），排队任务随首个 zhi 送达——没有壳，无头 agent 会被
KEEPALIVE 圈在「待命等派任务」上永远开不了工。
"""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub

WS1 = r"d:\Desktop\cursor工作流"


class _FakeClient:
    def __init__(self, cwd=WS1, pid=4321, peer_ip=None):
        self.cwd, self.pid, self.peer_ip = cwd, pid, peer_ip
        self.sessions = {}


class SpawnShellTests(unittest.TestCase):
    def _patched(self, sessions):
        return (patch.object(hub.HUB, "sessions", sessions),
                patch.object(hub.HUB, "order", list(sessions)),
                patch.object(hub.Hub, "save_state", lambda self: None),
                patch.object(hub.Hub, "_init_file", lambda self, s: None),
                patch.object(hub.Hub, "save_msg_images", lambda self, p: []),
                patch.object(hub.Hub, "yield_zhi",
                             lambda self, client, exclude_conv=None, secs=90: None),
                patch.object(hub.Hub, "start_yield_burst",
                             lambda self, secs=None: None))

    def test_shell_created_disconnected_with_queued_task(self):
        sessions = {}
        with patch.multiple(hub.Hub, save_state=lambda self: None,
                            _init_file=lambda self, s: None,
                            save_msg_images=lambda self, p: []), \
                patch.object(hub.HUB, "sessions", sessions), \
                patch.object(hub.HUB, "order", []):
            s = hub.HUB.create_spawn_shell("abcd1234", WS1, "内置 agent")
            self.assertIn(s.id, sessions)
            self.assertFalse(s.connected)
            self.assertIsNone(s.client)
            self.assertIsNone(s.peer_ip)          # 本机 runner：复活匹配的关键
            self.assertGreater(s.recon_deadline, time.time())  # 宽限内显示重连中
            r = hub.Api().queue_message(s.id, "去把周报修了", [], who="派单",
                                        force=True)
            self.assertTrue(r["ok"])
            self.assertEqual(1, len(s.queued))
            self.assertEqual("派单", s.queued[0]["who"])
            self.assertEqual("去把周报修了", s.queued[0]["text"])

    def test_report_in_revives_shell_not_a_new_tab(self):
        sessions = {}
        patches = self._patched(sessions)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], patches[6]:
            shell = hub.HUB.create_spawn_shell("abcd1234", WS1, "内置 agent")
            client = _FakeClient()
            got = hub.HUB.create_session(client, "abcd1234", "待命·cursor工作流")
            self.assertIs(shell, got)             # 复活同壳，不另开 tab
            self.assertEqual(1, len(sessions))
            self.assertTrue(got.connected)
            self.assertIs(client, got.client)
            self.assertIsNone(got.recon_deadline)
            self.assertIs(got, client.sessions["abcd1234"])

    def test_misdropped_live_shell_returns_to_list_on_next_zhi(self):
        # 09-07：batchOpen 回执超时把已报到的壳从 sessions 摘掉，zhi 还堵在
        # 这个 Session 上。下一声 resolve_session 必须把它塞回侧栏。
        sessions = {}
        patches = self._patched(sessions)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], patches[6], \
                patch.object(hub.HUB, "_reap_handed_off_shell", lambda *a, **k: None), \
                patch.object(hub.HUB, "takeover_landed", lambda *a, **k: None), \
                patch.object(hub.HUB, "maybe_apply_task_name", lambda *a, **k: None), \
                patch.object(hub.HUB, "heal_standby_tab_name", lambda *a, **k: None), \
                patch.object(hub.HUB, "_bind_registered_team_root", lambda *a, **k: None), \
                patch.object(hub, "heal_session_paths", lambda *a, **k: None), \
                patch.object(hub, "heal_named_agent_root", lambda *a, **k: None), \
                patch.object(hub, "apply_session_cwd", lambda *a, **k: None), \
                patch.object(hub.Api, "_follow_seat_to_named_project",
                             lambda *a, **k: None):
            shell = hub.HUB.create_spawn_shell("abcd1234", WS1, "内置 agent")
            client = _FakeClient()
            client.sessions["abcd1234"] = shell
            shell.client = client
            shell.connected = True
            sid = shell.id
            sessions.pop(sid)
            hub.HUB.order.remove(sid)
            got = hub.HUB.resolve_session(client, "abcd1234", "rxyy MCP·批量消失")
            self.assertIs(shell, got)
            self.assertIn(sid, sessions)
            self.assertIn(sid, hub.HUB.order)

    def test_misdropped_live_shell_returns_to_list_on_zt_signal(self):
        sessions = {}
        patches = self._patched(sessions)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], patches[6], \
                patch.object(hub.HUB, "takeover_landed", lambda *a, **k: None):
            shell = hub.HUB.create_spawn_shell("abcd1234", WS1, "内置 agent")
            client = _FakeClient()
            client.sessions["abcd1234"] = shell
            shell.client = client
            sid = shell.id
            sessions.pop(sid)
            hub.HUB.order.remove(sid)
            got = hub.HUB._resolve_for_signal(client, "abcd1234", revive_handed=True)
            self.assertIs(shell, got)
            self.assertIn(sid, sessions)
            self.assertIn(sid, hub.HUB.order)

    def test_archived_shell_is_not_pulled_back_by_ensure_listed(self):
        sessions = {}
        patches = self._patched(sessions)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], patches[6]:
            shell = hub.HUB.create_spawn_shell("abcd1234", WS1, "内置 agent")
            sid = shell.id
            shell.archived = True
            sessions.pop(sid)
            hub.HUB.order.remove(sid)
            self.assertFalse(hub.HUB._ensure_listed(shell))
            self.assertNotIn(sid, sessions)

    def test_shell_survives_other_peer_reporting_same_conv(self):
        # 远端来源（peer_ip 非 None）用同 conv_key 报到不该抢走本机壳：
        # 复活条件 = 同对话ID + 同来源
        sessions = {}
        patches = self._patched(sessions)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], patches[6]:
            shell = hub.HUB.create_spawn_shell("abcd1234", WS1, "内置 agent")
            remote = _FakeClient(peer_ip="192.168.1.50")
            got = hub.HUB.create_session(remote, "abcd1234", "远端会话")
            self.assertIsNot(shell, got)
            self.assertEqual(2, len(sessions))
            self.assertFalse(shell.connected)     # 壳原样留着等本机 agent


if __name__ == "__main__":
    unittest.main()
