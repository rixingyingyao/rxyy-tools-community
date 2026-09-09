"""Native tool calls must return without entering Cursor's keepalive loops."""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


class NativeDeadlineTests(unittest.TestCase):
    def test_missing_conversation_ids_cannot_merge_two_native_tasks(self):
        a = server._native_conversation_id(None, "codex", "task-a")
        self.assertEqual(a, server._native_conversation_id(None, "codex", "task-a"))
        self.assertNotEqual(a, server._native_conversation_id(None, "codex", "task-b"))
        self.assertNotEqual(server._native_conversation_id(None, "codex", None),
                            server._native_conversation_id(None, "codex", None))
        self.assertEqual("old-id", server._native_conversation_id("old-id", "codex", "task-a"))

    def bridge(self):
        bridge = server.HubBridge()
        bridge._hb_started = True
        bridge._send_detach = lambda *_: None
        return bridge

    def test_nonblocking_native_connect_has_a_deadline(self):
        bridge = self.bridge()
        connections = []
        bridge._connect_locked = lambda **kw: connections.append(kw) or object()
        with patch.object(server, "send_msg", lambda *_: None), \
                patch.object(bridge, "_deferred_peek_secs", return_value=0):
            result = bridge.ask("progress", [], True, conversation_id="native-nowait",
                                runtime_kind="codex", wait=False)
        self.assertTrue(result["deferred"])
        self.assertIn("deadline", connections[0])

    def test_forced_yield_preserves_question_without_cursor_keepalive(self):
        bridge = self.bridge()
        bridge._connect_locked = lambda **_: object()

        def send(_sock, msg):
            if msg.get("type") == "zhi_request":
                waiter = bridge.waiters[msg["id"]]
                waiter["force_ka"] = True
                waiter["event"].set()

        with patch.object(server, "send_msg", send):
            result = bridge.ask("question", [], True, conversation_id="native-yield",
                                runtime_kind="codex", wait=True)
        self.assertTrue(result["deferred"])
        self.assertIn("native-yield", bridge._deferred_convs)

    def test_native_freeze_does_not_block_host_tool_call(self):
        with patch.object(server, "DISABLED", False), \
                patch.object(server, "_is_frozen", return_value=True), \
                patch.object(server, "wait_if_frozen", side_effect=AssertionError("must not wait")):
            result = server.tool_zhi({"message": "progress", "runtime": "codex"})
        self.assertIn("冻结", str(result))


if __name__ == "__main__":
    unittest.main()
