# -*- coding: utf-8 -*-
"""zhi SSE 登记表不许只涨不落（08-25 体检发现的两条泄漏路径）。

hub/MCP 是常驻进程，`_zhi_event_index` 与 `_active_sse_calls` 里滞留的条目
永远没有第二次机会被清：

① 事件 id 无上限：心跳 15s 造一个 id，全部进索引。挂几小时的待命 zhi 被
   用户打断（notifications/cancelled）后，旧实现只弹 _active_sse_calls，
   几百个心跳 id 原地滞留——续流只认「客户端收到的最后一个 id」，老 id
   本来就没资格再被用到。
② 流断了又没人续：agent 进程没了，work 线程照常算完、done 置位，但
   result_sent 永远等不到 True，pump 的 finally 只在送达后清登记，
   这条流连着它的事件 id 一起永久滞留。

治法：索引每流只留最近 _ZHI_IDS_KEEP 个 id；结果已出却超过
_ZHI_ORPHAN_TTL 没送出去的流，随下一条 zhi 到达顺手回收。
"""
import sys
import threading
import time
import types
import unittest
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import server  # noqa: E402


def _stream(rid="r1", conv="aaaa1111"):
    st = types.SimpleNamespace()
    st.rid, st.conv, st.tok = rid, conv, None
    st.ids, st.n = [], 0
    st.box, st.done = {}, threading.Event()
    st.write_lock = threading.Lock()
    st.result_sent = False
    return st


class EventIndexTrimTests(unittest.TestCase):
    """索引按流修剪：只留最近几个 id，够 Last-Event-ID 续流即可。"""

    def setUp(self):
        self._saved = dict(server._zhi_event_index)
        server._zhi_event_index.clear()
        self.addCleanup(lambda: (server._zhi_event_index.clear(),
                                 server._zhi_event_index.update(self._saved)))

    def test_old_heartbeat_ids_fall_off_the_index(self):
        st = _stream()
        for i in range(server._ZHI_IDS_KEEP * 4):
            server._index_zhi_event("zhi-t{}".format(i), st)
        self.assertEqual(server._ZHI_IDS_KEEP, len(server._zhi_event_index))
        self.assertEqual(server._ZHI_IDS_KEEP, len(st.ids))
        # 最新的查得到（这才是客户端会拿来续流的那个），最老的已经放掉
        last = "zhi-t{}".format(server._ZHI_IDS_KEEP * 4 - 1)
        self.assertIs(st, server._lookup_zhi_stream(last))
        self.assertIsNone(server._lookup_zhi_stream("zhi-t0"))

    def test_two_streams_do_not_trim_each_other(self):
        a, b = _stream("ra"), _stream("rb")
        for i in range(server._ZHI_IDS_KEEP):
            server._index_zhi_event("a{}".format(i), a)
            server._index_zhi_event("b{}".format(i), b)
        self.assertIs(a, server._lookup_zhi_stream("a0"))
        self.assertIs(b, server._lookup_zhi_stream("b0"))

    def test_drop_removes_every_id_of_that_stream(self):
        st = _stream()
        for i in range(3):
            server._index_zhi_event("d{}".format(i), st)
        server._drop_zhi_stream(st)
        for i in range(3):
            self.assertIsNone(server._lookup_zhi_stream("d{}".format(i)))


class OrphanSweepTests(unittest.TestCase):
    """结果已出却送不出去的流：TTL 一到就连登记带事件 id 一起回收。"""

    def setUp(self):
        self._idx = dict(server._zhi_event_index)
        self._calls = dict(server._active_sse_calls)
        server._zhi_event_index.clear()
        server._active_sse_calls.clear()

        def restore():
            server._zhi_event_index.clear()
            server._zhi_event_index.update(self._idx)
            server._active_sse_calls.clear()
            server._active_sse_calls.update(self._calls)

        self.addCleanup(restore)

    def _register(self, st):
        server._active_sse_calls[st.rid] = {
            "t0": time.time(), "token": None, "probe": False,
            "conv": st.conv, "stream": st}

    def test_a_long_dead_stream_is_reaped(self):
        st = _stream("dead1")
        server._index_zhi_event("hb1", st)
        st.done_at = time.time() - server._ZHI_ORPHAN_TTL - 5
        st.done.set()
        self._register(st)
        server._sweep_zhi_orphans()
        self.assertNotIn("dead1", server._active_sse_calls)
        self.assertIsNone(server._lookup_zhi_stream("hb1"))

    def test_a_stream_still_waiting_for_the_user_is_untouched(self):
        # done 没置位 = zhi 还在等用户回复，挂几小时都是正常业态
        st = _stream("wait1")
        server._index_zhi_event("hb2", st)
        self._register(st)
        server._sweep_zhi_orphans()
        self.assertIn("wait1", server._active_sse_calls)
        self.assertIs(st, server._lookup_zhi_stream("hb2"))

    def test_a_freshly_done_stream_gets_its_grace_period(self):
        # 刚算完结果、pump 正要写出去：不许抢在送达前回收
        st = _stream("fresh1")
        st.done_at = time.time()
        st.done.set()
        self._register(st)
        server._sweep_zhi_orphans()
        self.assertIn("fresh1", server._active_sse_calls)

    def test_a_delivered_stream_is_not_double_dropped(self):
        # 已送达的流由 pump 的 finally 清，即便晚点没清也不该在这里炸
        st = _stream("sent1")
        st.done_at = time.time() - server._ZHI_ORPHAN_TTL - 5
        st.done.set()
        st.result_sent = True
        self._register(st)
        server._sweep_zhi_orphans()
        self.assertIn("sent1", server._active_sse_calls)


if __name__ == "__main__":
    unittest.main()
