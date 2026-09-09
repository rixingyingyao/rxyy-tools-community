# -*- coding: utf-8 -*-
"""zhi 的 pre_cancelled 兜底必须按 TTL 过期，杜绝跨窗口 rpc_id 撞号误杀。

现场（2026-08-12 傍晚）：一个 39222 MCP 端点服务本机所有 Cursor 窗口，rpc_id 是
每个客户端连接各自从 2、3、4… 递增的 JSON-RPC id。任一窗口的 zhi 挂到 120s 被
Cursor 硬超时取消，就把它的小号灌进全进程共享的 cancelled_rpc_ids；新开窗口 rpc_id
也从小号起步，撞上别人留下的号 → pre_cancelled 命中 → 连请求都不发直接回
-32800「请求已被客户端取消」。表现为「新会话报到时 zhi 连败几次、递增到干净号才
成功」，且服务端不打「已被取消」日志（走的是 pre_cancelled 那条静默路径）。

根治：兜底记录带时刻、按 TTL 过期且命中即消费——只保留真实竞态窗口内的取消。
（身份根治② 后记录按 (conversation_id|None, rpc_id) 分桶，本文件覆盖无会话旁证
的 (None, rpc_id) 兜底桶；跨会话隔离见 test_cancel_conv_isolation.py。）
"""
import sys
import time
import unittest
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import server  # noqa: E402


class PreCancelTtlTests(unittest.TestCase):
    def setUp(self):
        self.bridge = server.HubBridge()

    def _mark_cancelled(self, rpc_id):
        # 复用真实入口：waiter 未注册时 cancel_request 把号记进兜底集合
        self.bridge.cancel_request(rpc_id)

    def test_fresh_cancel_hits_within_ttl(self):
        # 合法竞态：cancelled 抢在 tools/call 前到达，随后同号的 zhi 应被作废
        self._mark_cancelled(7)
        with self.bridge.waiters_lock:
            self.assertTrue(self.bridge._consume_pre_cancel("7"))

    def test_hit_is_consumed_once(self):
        # 命中即消费：同一条兜底记录不能反复误杀后续请求
        self._mark_cancelled(7)
        with self.bridge.waiters_lock:
            self.assertTrue(self.bridge._consume_pre_cancel("7"))
            self.assertFalse(self.bridge._consume_pre_cancel("7"))

    def test_stale_cancel_does_not_hit_cross_window(self):
        # 核心回归：别的窗口很久前取消的同号，新窗口不得被误杀
        self._mark_cancelled(2)
        # 改成 TTL 之前（模拟一小时前别的窗口留下的记录）
        self.bridge.cancelled_rpc_ids[(None, "2")] = (
            time.time() - self.bridge.PRE_CANCEL_TTL - 1)
        with self.bridge.waiters_lock:
            self.assertFalse(self.bridge._consume_pre_cancel("2"))

    def test_expired_entries_are_purged(self):
        self.bridge.cancelled_rpc_ids[(None, "2")] = (
            time.time() - self.bridge.PRE_CANCEL_TTL - 1)
        self.bridge.cancelled_rpc_ids[(None, "3")] = time.time()
        with self.bridge.waiters_lock:
            self.bridge._purge_expired_cancels(time.time())
        self.assertNotIn((None, "2"), self.bridge.cancelled_rpc_ids)
        self.assertIn((None, "3"), self.bridge.cancelled_rpc_ids)

    def test_unknown_rpc_id_never_hits(self):
        with self.bridge.waiters_lock:
            self.assertFalse(self.bridge._consume_pre_cancel("999"))

    def test_cancel_request_records_timestamp(self):
        # 存的是时刻（float），不是老的纯 set 成员
        self._mark_cancelled(5)
        self.assertIn((None, "5"), self.bridge.cancelled_rpc_ids)
        self.assertIsInstance(self.bridge.cancelled_rpc_ids[(None, "5")], float)


if __name__ == "__main__":
    unittest.main()
