# -*- coding: utf-8 -*-
"""zhi 取消按 conversation_id 分桶隔离——跨窗口 rpc_id 撞号 -32800 事故的根治层。

现场（2026-08-12 傍晚，根治方案书第一节·路径1）：39222 单端点服务本机所有 Cursor
窗口，JSON-RPC rpc_id 是每个客户端各自从小整数递增的。任一窗口的 zhi 被取消，
号码进了全进程共享的 cancelled_rpc_ids；新窗口首个 zhi 从同一小号起步 → 撞号 →
pre_cancelled 秒挂 -32800。当天用 15s TTL 止血（1044c1c，权宜）；本文件锁的是
根治：取消记录与等待方匹配都按 (conversation_id, rpc_id) 分桶，跨会话同号从机制
上互不可见，TTL 降级为同会话内的兜底。

取消通知本身只带 requestId，conversation_id 旁证由 HTTP 层的 _active_sse_calls
登记表反查（SSE 一开始就登记，先于 waiter 注册，竞态窗口内一定查得到）。
"""
import sys
import threading
import time
import unittest
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import server  # noqa: E402


def _waiter(rpc_id, conv):
    return {
        "event": threading.Event(), "resp": None, "sock": None,
        "rpc_id": rpc_id, "conversation_id": conv, "cancelled": False,
    }


class CrossConvPreCancelIsolation(unittest.TestCase):
    """兜底记录（取消先于 tools/call 到达）的分桶隔离。"""

    def setUp(self):
        self.bridge = server.HubBridge()

    def test_other_conversations_cancel_never_hits_me(self):
        # 08-12 事故主场景：窗口A 取消了 rpc_id=3，窗口B 新对话首个 zhi 也是 3
        self.bridge.cancel_request(3, conv="conv-aaaa")
        with self.bridge.waiters_lock:
            self.assertFalse(self.bridge._consume_pre_cancel("3", "conv-bbbb"),
                             "别的会话留下的取消号不得误杀本会话")

    def test_own_cancel_still_hits_within_ttl(self):
        # 真实竞态（同一会话取消抢先到达）仍要兜住
        self.bridge.cancel_request(3, conv="conv-aaaa")
        with self.bridge.waiters_lock:
            self.assertTrue(self.bridge._consume_pre_cancel("3", "conv-aaaa"))
            self.assertFalse(self.bridge._consume_pre_cancel("3", "conv-aaaa"),
                             "命中即消费，不能反复误杀")

    def test_legacy_cancel_without_conv_still_hits_any_asker(self):
        # stdio 路径 / SSE 登记前的极端竞态：无旁证记录进 (None, key) 桶，
        # 对同号请求兜底命中（TTL 内），行为与 1044c1c 权宜版一致
        self.bridge.cancel_request(3)
        with self.bridge.waiters_lock:
            self.assertTrue(self.bridge._consume_pre_cancel("3", "conv-any"))


class LiveWaiterCancelIsolation(unittest.TestCase):
    """活等待方（zhi 已在挂）的取消匹配也必须按会话隔离。"""

    def setUp(self):
        self.bridge = server.HubBridge()

    def test_conv_scoped_cancel_kills_only_its_own_waiter(self):
        wa = _waiter(7, "conv-aaaa")
        wb = _waiter(7, "conv-bbbb")
        self.bridge.waiters = {"ra": wa, "rb": wb}
        self.bridge.cancel_request(7, conv="conv-bbbb")
        self.assertFalse(wa["cancelled"], "别的窗口的取消不得错杀本窗口的在飞 zhi")
        self.assertFalse(wa["event"].is_set())
        self.assertTrue(wb["cancelled"])
        self.assertTrue(wb["event"].is_set())

    def test_ambiguous_cancel_without_conv_kills_nobody(self):
        # 无旁证 + 多个同号等待方（只有共享端点跨窗口才可能）：宁可不杀
        wa = _waiter(7, "conv-aaaa")
        wb = _waiter(7, "conv-bbbb")
        self.bridge.waiters = {"ra": wa, "rb": wb}
        self.bridge.cancel_request(7)
        self.assertFalse(wa["cancelled"])
        self.assertFalse(wb["cancelled"])

    def test_single_waiter_without_conv_is_still_cancelled(self):
        # stdio（一窗一进程）语义不变：唯一同号等待方照样作废
        wa = _waiter(7, "conv-aaaa")
        self.bridge.waiters = {"ra": wa}
        self.bridge.cancel_request(7)
        self.assertTrue(wa["cancelled"])
        self.assertTrue(wa["event"].is_set())

    def test_live_kill_leaves_no_pre_cancel_residue(self):
        # 杀到活等待方 = 取消已兑现，不得再留兜底记录毒到同号的后续请求
        wa = _waiter(7, "conv-aaaa")
        self.bridge.waiters = {"ra": wa}
        self.bridge.cancel_request(7, conv="conv-aaaa")
        self.assertTrue(wa["cancelled"])
        with self.bridge.waiters_lock:
            self.assertFalse(self.bridge._consume_pre_cancel("7", "conv-aaaa"),
                             "已兑现的取消不该再挂兜底记录")


class SseRegistryCarriesConv(unittest.TestCase):
    """_active_sse_calls 登记表必须带 conv 字段——取消通知反查旁证的唯一来源。"""

    def test_ask_consume_prefers_own_bucket_then_legacy(self):
        b = server.HubBridge()
        b.cancelled_rpc_ids[("conv-aaaa", "9")] = time.time()
        b.cancelled_rpc_ids[(None, "9")] = time.time()
        with b.waiters_lock:
            self.assertTrue(b._consume_pre_cancel("9", "conv-aaaa"))
        self.assertNotIn(("conv-aaaa", "9"), b.cancelled_rpc_ids,
                         "优先消费本会话桶")
        self.assertIn((None, "9"), b.cancelled_rpc_ids)


if __name__ == "__main__":
    unittest.main()
