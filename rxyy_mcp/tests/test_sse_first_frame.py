# -*- coding: utf-8 -*-
"""reinit 风暴缓解：zhi 的 SSE 首包即时化（身份根治⑦，方案书第七节）。

定位（第七节证据链 + 传输层走读）：本端点是【无会话】的 Streamable HTTP——
从不发 Mcp-Session-Id、从不因会话过期回 404，服务端绝不主动要求客户端 reinit。
`Failed to start MCP session reinitialization` 因此是 Cursor 客户端会话层内部的
竞态：新窗口首个长挂 zhi 把共享 per-URL 传输占住、期间 SSE 流 0~15s 静默，另一个
窗口并发 initialize/调用时客户端尝试 reinit 与被占用的传输相撞。服务端能做的、
零语义变更的对冲 = 首包即时化：headers 之后立刻发第一帧，消灭 0~15s 静默窗口。

本测试锁的是该缓解的载荷形状（_sse_progress_frame）：有 progressToken 发真
notifications/progress、progress 从 0 起；无 token 退回 SSE 注释心跳。首包发在
15s 轮询循环之前由代码结构保证（见 _serve_zhi_sse）。
"""
import json
import sys
import unittest
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import server  # noqa: E402


class SseFirstFrameShape(unittest.TestCase):
    def test_progress_frame_with_token_is_real_progress_zero(self):
        raw = server._sse_progress_frame("tok-1", 0, "已就绪，等待用户回复")
        self.assertTrue(raw.startswith(b"event: message\r\ndata: "))
        self.assertTrue(raw.endswith(b"\r\n\r\n"))
        body = raw.decode("utf-8").split("data: ", 1)[1].strip()
        obj = json.loads(body)
        self.assertEqual("notifications/progress", obj["method"])
        self.assertEqual("tok-1", obj["params"]["progressToken"])
        self.assertEqual(0, obj["params"]["progress"], "首包 progress 从 0 起")

    def test_progress_frame_without_token_falls_back_to_comment(self):
        raw = server._sse_progress_frame(None, 0, "已就绪")
        # SSE 注释帧（以 ':' 开头）不是事件，纯保活；不得是 JSON 事件
        self.assertTrue(raw.startswith(b": "))
        self.assertTrue(raw.endswith(b"\r\n\r\n"))
        self.assertNotIn(b"event: message", raw)

    def test_loop_frames_increment_from_one(self):
        # 循环内的续拍从 1 起（首包已占了 0），progress 单调递增
        f1 = server._sse_progress_frame("t", 1, "等待用户回复中")
        f2 = server._sse_progress_frame("t", 2, "等待用户回复中")
        p1 = json.loads(f1.decode("utf-8").split("data: ", 1)[1].strip())
        p2 = json.loads(f2.decode("utf-8").split("data: ", 1)[1].strip())
        self.assertEqual(1, p1["params"]["progress"])
        self.assertEqual(2, p2["params"]["progress"])


class EndpointStaysSessionless(unittest.TestCase):
    """会话过期策略（第七节结论）：initialize 应答绝不带 Mcp-Session-Id/sessionId,
    从而服务端永不触发客户端 reinit——这是根治的一半（另一半是首包即时化）。"""

    def test_initialize_result_has_no_session_id(self):
        resp = server.dispatch_request({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "1"}}})
        result = resp["result"]
        self.assertNotIn("sessionId", result)
        self.assertNotIn("Mcp-Session-Id", result)
        # 协议版本原样回显、能力声明存在——握手本身正常
        self.assertEqual("2025-06-18", result["protocolVersion"])
        self.assertIn("tools", result["capabilities"])

    def test_initialize_2025_11_25_still_has_no_session_id(self):
        resp = server.dispatch_request({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {
                "elicitation": {}, "roots": {}},
                       "clientInfo": {"name": "cursor-vscode", "version": "1.0.0"}}})
        result = resp["result"]
        self.assertEqual("2025-11-25", result["protocolVersion"])
        self.assertNotIn("sessionId", result)
        self.assertNotIn("Mcp-Session-Id", result)
        self.assertNotIn("MCP-Session-Id", result)


class StreamableHttp2025_11_25(unittest.TestCase):
    """Cursor 现网 initialize proto=2025-11-25：GET 必须能开 SSE 长连接；
    POST 断线不得当取消；priming 帧带 id。仍不发 session id。"""

    def test_prime_frame_has_id_retry_and_empty_data(self):
        raw = server._sse_prime_frame("zhi-1")
        text = raw.decode("utf-8")
        self.assertIn("id: zhi-1\r\n", text)
        self.assertIn("retry: 15000\r\n", text)
        self.assertIn("data: \r\n\r\n", text)
        self.assertNotIn("event: message", text)

    def test_protocol_at_least_from_header(self):
        self.assertTrue(server._protocol_at_least(
            {"MCP-Protocol-Version": "2025-11-25"}))
        self.assertFalse(server._protocol_at_least(
            {"MCP-Protocol-Version": "2025-06-18"}))
        self.assertFalse(server._protocol_at_least({}))

    def test_get_path_and_accept(self):
        self.assertTrue(server._mcp_path_ok("/mcp"))
        self.assertTrue(server._mcp_path_ok("/mcp/cursor工作流"))
        self.assertTrue(server._mcp_path_ok("/mcp/cursor%E5%B7%A5%E4%BD%9C%E6%B5%81"))
        self.assertFalse(server._mcp_path_ok("/ui"))
        self.assertTrue(server._accepts_sse("application/json, text/event-stream"))
        self.assertFalse(server._accepts_sse("application/json"))

    def test_disconnect_does_not_cancel_zhi(self):
        self.assertFalse(server._zhi_sse_disconnect_cancels())

    def test_event_index_roundtrip(self):
        stream = type("S", (), {"ids": []})()
        eid = server._alloc_sse_id("zhi")
        stream.ids.append(eid)
        server._index_zhi_event(eid, stream)
        self.assertIs(server._lookup_zhi_stream(eid), stream)
        server._drop_zhi_stream(stream)
        self.assertIsNone(server._lookup_zhi_stream(eid))


if __name__ == "__main__":
    unittest.main()
