# -*- coding: utf-8 -*-
"""rxyy MCP 跨客户端适配的事故复现测试。

这些测试锁住四条曾经会互相污染的路径：运行时说明/schema、HTTP 初始化身份、
SSE 取消键，以及 Codex/ChatGPT 的默认等待策略。测试故意只触碰 server 的
适配层；hub 的字段消费由主会话负责。
"""
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import server  # noqa: E402
from client_runtime import RUNTIME_KINDS  # noqa: E402


def _initialize(name, version="1"):
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {"roots": {}},
            "clientInfo": {"name": name, "version": version},
        },
    }


class RuntimeSchemaRegressionTests(unittest.TestCase):
    def test_every_tool_has_optional_runtime_and_thread_id_metadata(self):
        for tool in server.TOOLS:
            props = tool["inputSchema"]["properties"]
            self.assertEqual(RUNTIME_KINDS, tuple(props["runtime"]["enum"]), tool["name"])
            self.assertEqual("string", props["thread_id"]["type"])
            self.assertIs(False, tool["annotations"]["readOnlyHint"])

    def test_zhi_accepts_english_artifacts_without_removing_chinese_key(self):
        props = next(t for t in server.TOOLS if t["name"] == "zhi")["inputSchema"]["properties"]
        self.assertIn("成果", props)
        self.assertIn("artifacts", props)
        self.assertEqual(props["成果"]["items"], props["artifacts"]["items"])


class NativeInstructionRegressionTests(unittest.TestCase):
    def test_codex_instructions_are_self_contained_and_do_not_force_cursor(self):
        resp = server.dispatch_request(_initialize("codex-cli", "0.1"))
        instructions = resp["result"]["instructions"]
        self.assertLessEqual(len(instructions), 512)
        self.assertIn("zhi", instructions)
        self.assertIn("wait=false", instructions)
        for cursor_only in ("CallMcpTool", "GetMcpTools", "rename_chat", "Reload Window"):
            self.assertNotIn(cursor_only, instructions)

    def test_chatgpt_profile_gets_native_tool_descriptions(self):
        registry = server.HTTP_RUNTIME_REGISTRY
        ctx, _ = registry.initialize("chatgpt-connection", _initialize("unknown")["params"],
                                      profile="chatgpt")
        tools = server.dispatch_request({
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        }, client_context=ctx)["result"]["tools"]
        zhi = next(t for t in tools if t["name"] == "zhi")
        self.assertNotIn("cursor-app-control", zhi["inputSchema"]["properties"]["task_name"]["description"])
        self.assertIn("默认 false", zhi["inputSchema"]["properties"]["wait"]["description"])
        self.assertNotIn("必须先调它收尾", zhi["description"])


class HttpRuntimeIsolationRegressionTests(unittest.TestCase):
    def test_reinitialize_on_same_connection_does_not_mutate_inflight_identity(self):
        registry = server.HttpRuntimeRegistry()
        old, _ = registry.initialize("same", _initialize("codex-cli")["params"])
        new, _ = registry.initialize("same", _initialize("chatgpt-web")["params"])
        self.assertEqual("codex", old.runtime_kind)
        self.assertEqual("chatgpt", new.runtime_kind)
        self.assertNotEqual(old.session_id, new.session_id)

    def test_abandoned_native_sessions_have_a_memory_bound(self):
        registry = server.HttpRuntimeRegistry()
        for i in range(600):
            registry.initialize(str(i), _initialize("codex-cli")["params"])
            registry.release_connection(str(i))
        self.assertLessEqual(len(registry._sessions), 512)
        self.assertEqual(0, len(registry._connections))

    def test_last_initialize_does_not_overwrite_another_connection(self):
        registry = server.HTTP_RUNTIME_REGISTRY
        a, _ = registry.initialize("connection-a", _initialize("codex-cli")["params"])
        b, _ = registry.initialize("connection-b", _initialize("chatgpt-web")["params"])
        self.assertEqual("codex", registry.resolve("connection-a").runtime_kind)
        self.assertEqual("chatgpt", registry.resolve("connection-b").runtime_kind)
        self.assertEqual("codex", a.runtime_kind)
        self.assertEqual("chatgpt", b.runtime_kind)

    def test_explicit_profile_is_a_fallback_for_clients_without_identifying_name(self):
        registry = server.HTTP_RUNTIME_REGISTRY
        ctx, issued = registry.initialize(
            "connection-c", _initialize("mystery-client")["params"], profile="codex")
        self.assertEqual("codex", ctx.runtime_kind)
        self.assertTrue(issued)
        self.assertTrue(ctx.session_id)

    def test_releasing_connection_keeps_session_for_cross_connection_resume(self):
        registry = server.HttpRuntimeRegistry()
        ctx, issued = registry.initialize(
            "connection-d", _initialize("chatgpt-web")["params"])
        self.assertTrue(issued)
        registry.release_connection("connection-d")
        resumed = registry.resolve(
            "connection-e", {server.SESSION_HEADER: ctx.session_id})
        self.assertIs(ctx, resumed)
        self.assertEqual("chatgpt", resumed.runtime_kind)
        self.assertEqual("", registry.resolve("connection-d").session_id)


class RuntimeMetadataAndWaitRegressionTests(unittest.TestCase):
    def test_bridge_payload_carries_native_metadata(self):
        bridge = server.HubBridge()
        bridge._hb_started = True
        sent = []
        bridge._connect_locked = lambda **_kwargs: object()
        bridge._send_detach = lambda *_args: None
        bridge._deferred_peek_secs = lambda _cfg: 0.01
        with patch.object(server, "send_msg",
                          lambda _sock, payload: sent.append(payload)):
            result = bridge.ask(
                "进展", [], True, conversation_id="native-conv", cwd="D:\\project",
                wait=False, runtime_kind="chatgpt", native_thread_id="thread-456")
        request = next(p for p in sent if p.get("type") == "zhi_request")
        self.assertTrue(result["deferred"])
        self.assertEqual("chatgpt", request["runtime_kind"])
        self.assertEqual("thread-456", request["native_thread_id"])
        self.assertEqual("D:\\project", request["cwd"])

    def test_status_payload_carries_native_metadata_and_cwd(self):
        bridge = server.HubBridge()
        bridge._hb_started = True
        sent = []
        bridge._reconnect_if_hub_up = lambda: object()
        bridge.fetch_mail = lambda *_args, **_kwargs: []
        with patch.object(server, "send_msg",
                          lambda _sock, payload: sent.append(payload)):
            bridge.report_status(
                "native-conv", "developing", "适配", cwd="D:\\project",
                runtime_kind="codex", native_thread_id="thread-789")
        payload = next(p for p in sent if p.get("type") == "agent_status")
        self.assertEqual("codex", payload["runtime_kind"])
        self.assertEqual("thread-789", payload["native_thread_id"])
        self.assertEqual("D:\\project", payload["cwd"])

    def test_codex_defaults_to_non_blocking_and_forwards_native_metadata(self):
        calls = []

        class Bridge:
            def ask(self, *args, **kwargs):
                calls.append((args, kwargs))
                return {"user_input": "收到", "selected_options": []}

            def _schedule_idle_clear(self, *_args):
                pass

            def own_conv_id_hint(self, conversation_id):
                return conversation_id

        with patch.object(server, "BRIDGE", Bridge()), \
                patch.object(server, "DISABLED", False), \
                patch.object(server, "wait_if_frozen", lambda: None):
            server.tool_zhi({
                "message": "进展已完成",
                "conversation_id": "codex-conv",
                "runtime": "codex",
                "thread_id": "thread-123",
            })
        self.assertFalse(calls[0][1]["wait"])
        self.assertEqual("codex", calls[0][1]["runtime_kind"])
        self.assertEqual("thread-123", calls[0][1]["native_thread_id"])

    def test_zt_uses_a_valid_project_path_as_cwd_and_forwards_metadata(self):
        calls = []

        class Bridge:
            def report_status(self, *args, **kwargs):
                calls.append((args, kwargs))
                return []

            def own_conv_id_hint(self, conversation_id):
                return conversation_id

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(server, "BRIDGE", Bridge()), \
                patch.object(server, "DISABLED", False), \
                patch.object(server, "wait_if_frozen", lambda: None):
            server.tool_zt({
                "status": "developing",
                "activity": "适配客户端",
                "project_path": tmp,
                "runtime": "chatgpt",
                "thread_id": "chat-thread",
            })
        self.assertEqual(str(Path(calls[0][1]["cwd"])), str(Path(tmp)))
        self.assertEqual("chatgpt", calls[0][1]["runtime_kind"])
        self.assertEqual("chat-thread", calls[0][1]["native_thread_id"])

    def test_explicit_native_wait_true_returns_deferred_after_one_bounded_wait(self):
        bridge = server.HubBridge()
        bridge._hb_started = True
        bridge._connect_locked = lambda **_kwargs: object()
        bridge._send_detach = lambda *_args: None
        with patch.object(server, "NATIVE_WAIT_MAX_SECS", 0.01), \
                patch.object(server, "send_msg", lambda *_args: None):
            result = bridge.ask(
                "等待用户确认", [], True, conversation_id="native-wait",
                runtime_kind="codex", native_thread_id="thread-123", wait=True)
        self.assertTrue(result["deferred"])
        self.assertTrue(result["wait_timeout"])
        self.assertNotIn("keepalive", result)


class SseCancellationIsolationRegressionTests(unittest.TestCase):
    def test_cancel_scope_only_reaches_the_matching_waiter(self):
        bridge = server.HubBridge()
        waiters = {}
        for key, scope in (("a", "http-session:a"), ("b", "http-session:b")):
            waiters[key] = {
                "event": threading.Event(), "resp": None,
                "sock": None, "rpc_id": 7, "conversation_id": "same-conv",
                "rpc_scope": scope, "cancelled": False,
            }
        bridge.waiters = waiters
        bridge.cancel_request(7, conv="same-conv", rpc_scope="http-session:b")
        self.assertFalse(waiters["a"]["cancelled"])
        self.assertTrue(waiters["b"]["cancelled"])

    def test_same_rpc_id_in_two_http_sessions_has_two_activity_keys(self):
        a = server._sse_activity_key("session-a", 7)
        b = server._sse_activity_key("session-b", 7)
        self.assertNotEqual(a, b)
        old = dict(server._active_sse_calls)
        try:
            server._active_sse_calls.clear()
            server._active_sse_calls[a] = {"conv": "conv-a"}
            server._active_sse_calls[b] = {"conv": "conv-b"}
            self.assertEqual("conv-a", server._get_active_sse_call("session-a", 7)["conv"])
            self.assertEqual("conv-b", server._get_active_sse_call("session-b", 7)["conv"])
            self.assertIsNone(server._get_active_sse_call("session-c", 7))
        finally:
            server._active_sse_calls.clear()
            server._active_sse_calls.update(old)


if __name__ == "__main__":
    unittest.main(verbosity=2)
