# -*- coding: utf-8 -*-
"""Codex UUID is stable task identity when an MCP bridge changes conv IDs."""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub  # noqa: E402


THREAD = "11111111-1111-4111-8111-111111111111"
WORKSPACE = str(MODULE_DIR.parent)


class _Client:
    def __init__(self, pid, peer_ip=None):
        self.cwd = WORKSPACE
        self.pid = pid
        self.peer_ip = peer_ip
        self.sessions = {}
        self.closed_convs = {}
        self.last_heartbeat = 0.0


class CodexNativeIdentityTests(unittest.TestCase):
    def setUp(self):
        self.h = hub.Hub.__new__(hub.Hub)
        self.h.cfg = {
            "max_messages": 200,
            "team_projects": {}, "team_seats": {}, "team_project_members": {},
            "reconnect_grace_secs": 120,
        }
        self.h.sessions, self.h.order = {}, []
        self.h.lock = threading.RLock()
        self.h.name_tombstones = {}
        self.h.takeover_aliases = {}
        self.old_client = _Client(101)
        self.original = hub.Session(self.old_client, "original-conv", "保留的原标签")
        self.original.runtime_kind = "codex"
        self.original.native_thread_id = THREAD
        self.original.name_locked = True
        self.original.messages = [{"role": "user", "html": "既有历史"}]
        self.original.created_ts = 10.0
        self.h.sessions[self.original.id] = self.original
        self.h.order.append(self.original.id)
        self.old_client.sessions[self.original.conv_key] = self.original
        self.patches = [
            patch.object(hub.Hub, "maybe_apply_task_name", lambda *args: None),
            patch.object(hub.Hub, "heal_standby_tab_name", lambda *args: None),
            patch.object(hub.Hub, "yield_zhi", lambda *args, **kwargs: None),
            patch.object(hub.Hub, "_note_model", lambda *args, **kwargs: None),
            patch.object(hub.Hub, "_verify_identity_by_generating", lambda *args: None),
            patch.object(hub.Hub, "_reap_takeover_shell", lambda *args: None),
            patch.object(hub.Hub, "notify_status_only", lambda *args: None),
            patch.object(hub.Api, "_auto_label_on_dispatch", lambda *args: None),
            patch.object(hub.Api, "_nudge_rename_if_standby", lambda *args: None),
            patch.object(hub, "heal_session_paths", lambda *args: None),
            patch.object(hub, "log_event", lambda *args, **kwargs: None),
        ]
        for item in self.patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(self.patches)])

    def _metadata(self, conv="bridge-after-restart", **extra):
        data = {"conversation_id": conv, "runtime_kind": "codex",
                "native_thread_id": THREAD}
        data.update(extra)
        return data

    def _add_historical_duplicate(self, conv_key, created_ts, name):
        duplicate = hub.Session(self.old_client, conv_key, name)
        duplicate.runtime_kind = "codex"
        duplicate.native_thread_id = THREAD
        duplicate.created_ts = created_ts
        duplicate.messages = [{"role": "user", "html": name + " 的既有历史"}]
        self.h.sessions[duplicate.id] = duplicate
        self.h.order.append(duplicate.id)
        return duplicate

    def test_new_bridge_conversation_reuses_original_tab_and_history(self):
        client = _Client(202)
        session = self.h.resolve_session(
            client, "bridge-after-restart", "新的状态名", self._metadata())

        self.assertIs(self.original, session)
        self.assertEqual({self.original.id}, set(self.h.sessions))
        self.assertEqual("original-conv", self.original.conv_key)
        self.assertEqual("保留的原标签", self.original.name)
        self.assertEqual([{"role": "user", "html": "既有历史"}], self.original.messages)
        self.assertIs(self.original, client.sessions["bridge-after-restart"])

    def test_three_historical_duplicates_choose_canonical_without_recursion(self):
        second = self._add_historical_duplicate("duplicate-conv-2", 20.0, "第二个旧标签")
        third = self._add_historical_duplicate("duplicate-conv-3", 30.0, "第三个旧标签")

        restarted = _Client(202)
        result = self.h.resolve_session(
            restarted, "new-bridge-conv", "", self._metadata(conv="new-bridge-conv"))
        original_key = _Client(203)
        by_original_key = self.h.resolve_session(
            original_key, "original-conv", "", self._metadata(conv="original-conv"))

        self.assertIs(self.original, result)
        self.assertIs(self.original, by_original_key)
        self.assertEqual(3, len(self.h.sessions))
        self.assertEqual(["保留的原标签", "第二个旧标签", "第三个旧标签"],
                         [self.h.sessions[sid].name for sid in self.h.order])
        self.assertIs(self.original, restarted.sessions["new-bridge-conv"])
        self.assertIs(self.original, original_key.sessions["original-conv"])
        self.assertEqual([{"role": "user", "html": "第二个旧标签 的既有历史"}],
                         second.messages)
        self.assertEqual([{"role": "user", "html": "第三个旧标签 的既有历史"}],
                         third.messages)

    def test_session_id_mistaken_for_conversation_id_still_reuses_thread(self):
        client = _Client(202)
        session = self.h.resolve_session(
            client, self.original.id, "", self._metadata(conv=self.original.id))

        self.assertIs(self.original, session)
        self.assertEqual(1, len(self.h.sessions))
        self.assertIs(self.original, client.sessions[self.original.id])

    def test_agent_status_new_conversation_uses_the_existing_native_tab(self):
        client = _Client(202)
        self.h._handle_client_msg(client, None, {
            "type": "agent_status", "conversation_id": "zt-after-restart",
            "runtime_kind": "codex", "native_thread_id": THREAD,
            "status": "developing", "activity": "修复身份去重",
        })

        self.assertEqual(1, len(self.h.sessions))
        self.assertIs(self.original, client.sessions["zt-after-restart"])
        self.assertEqual("developing", self.original.agent_status)

    def test_direct_create_session_has_the_same_guard(self):
        client = _Client(202)
        session = self.h.create_session(
            client, "direct-native-entry", "", self._metadata(conv="direct-native-entry"))

        self.assertIs(self.original, session)
        self.assertEqual(1, len(self.h.sessions))

    def test_runtime_host_and_unbound_reports_are_not_merged(self):
        remote = _Client(202, peer_ip="10.0.0.2")
        self.assertIsNone(self.h._native_thread_reuse_target(
            remote, "different-host", self._metadata()))
        self.assertIsNone(self.h._native_thread_reuse_target(
            _Client(202), "chatgpt-conv", self._metadata(runtime_kind="chatgpt")))
        self.assertIsNone(self.h._native_thread_reuse_target(
            _Client(202), "invalid-thread", self._metadata(native_thread_id="not-a-uuid")))
        self.assertIsNone(self.h._native_thread_reuse_target(
            _Client(202), "__default__", self._metadata(conv="__default__")))

    def test_parallel_bad_conversation_ids_do_not_create_extra_tabs(self):
        clients = [_Client(202), _Client(203)]
        errors = []

        def reconnect(index):
            try:
                key = "bad-conv-{}".format(index)
                self.h.resolve_session(clients[index], key, "", self._metadata(conv=key))
            except Exception as exc:  # pragma: no cover - assertion below reports it
                errors.append(exc)

        workers = [threading.Thread(target=reconnect, args=(i,)) for i in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

        self.assertEqual([], errors)
        self.assertEqual(1, len(self.h.sessions))
        self.assertTrue(all(client.sessions for client in clients))
        self.assertTrue(all(next(iter(client.sessions.values())) is self.original
                            for client in clients))

    def test_old_client_disconnect_does_not_mark_adopted_tab_offline(self):
        current = _Client(202)
        self.h.resolve_session(current, "wrong-conv", "", self._metadata(conv="wrong-conv"))
        self.assertIs(self.original, current.sessions["wrong-conv"])
        self.assertIs(self.original.client, current)

        self.h._client_disconnect(self.old_client, type("Sock", (), {"close": lambda self: None})())

        self.assertTrue(self.original.connected)
        self.assertIs(self.original.client, current)


if __name__ == "__main__":
    unittest.main()
