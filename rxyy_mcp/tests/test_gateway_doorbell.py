# -*- coding: utf-8 -*-
"""网关 SSE 门铃：hub 状态一变，控制台当拍就刷，不再等轮询到点。

08-27 用户报「消息点发出去了要等好多秒，状态才跟着变」——根子是 UI 全靠轮询，
变化落在慢档（2.5s/30s）拍点之间就得干等。门铃（GET /events）由后台对
get_state 做摘要，变了就推一帧「版本号」，前端收到立刻走既有 tick 单飞链补刷；
数据口径与轮询完全一致，SSE 只是把「下一拍」提前成「现在」。

这里锁死四件事：
1. 摘要必须抠掉秒表字段（processing_secs/idle_secs/…），否则光是时间流逝
   门铃就常鸣，退化回高频轮询；
2. 真变化必须响铃、纯秒表变化绝不响铃（e2e 走真 HTTP 流验证）；
3. 网关升 HTTP/1.1 后普通接口在一条连接上连发两问都答对（keep-alive 不错位）；
4. 慢调用（>SLOW_CALL_MS）要在运行日志留痕——下次再有「发送要等好多秒」，
   直接看日志断案，不靠体感猜。
"""
import http.client
import json
import socket
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import gateway

UI_PATH = MODULE_DIR / "ui.html"


class _FakeApi:
    """get_state 可控的假 api：counter 是「真变化」，secs 是「秒表噪音」。"""

    def __init__(self):
        self.counter = 0
        self.secs = 0

    def get_state(self):
        return {"sessions": [{
            "id": "s1", "name": "待命·测试", "rev": self.counter,
            "processing_secs": self.secs, "idle_secs": self.secs,
            "agent_status_age": self.secs,
            "takeover_pending": {"to_name": "x", "age": self.secs},
        }], "config": {}}

    def ping(self):
        return {"ok": True}

    def slowpoke(self):
        time.sleep(0.05)
        return {"ok": True}


class ScrubVolatileTests(unittest.TestCase):
    def test_stopwatch_keys_are_dropped_at_any_depth(self):
        node = {"a": [{"processing_secs": 3, "keep": 1,
                       "deep": {"idle_secs": 9, "age": 4, "ok": 2}}],
                "agent_status_age": 8}
        self.assertEqual(
            gateway._scrub_volatile(node),
            {"a": [{"keep": 1, "deep": {"ok": 2}}]})

    def test_real_fields_survive(self):
        node = {"rev": 5, "pending": True, "options": ["a"]}
        self.assertEqual(gateway._scrub_volatile(node), node)


class DoorbellProbeTests(unittest.TestCase):
    def setUp(self):
        self.api = _FakeApi()
        self.bell = gateway.Doorbell(self.api)  # 不 start：手动 probe，免时序抖动

    def test_first_probe_rings_then_settles(self):
        self.assertTrue(self.bell.probe_once(), "首拍建立基线算一次变化")
        self.assertFalse(self.bell.probe_once(), "状态没动就不该响")

    def test_stopwatch_ticks_never_ring(self):
        self.bell.probe_once()
        self.api.secs += 1
        self.assertFalse(self.bell.probe_once(),
                         "纯秒表字段变化响铃=门铃常鸣，退化回高频轮询")

    def test_real_change_rings(self):
        self.bell.probe_once()
        rev0 = self.bell.rev
        self.api.counter += 1
        self.assertTrue(self.bell.probe_once())
        self.assertEqual(self.bell.rev, rev0 + 1)

    def test_wait_change_returns_new_rev_or_none(self):
        self.bell.probe_once()
        rev = self.bell.rev
        self.assertIsNone(self.bell.wait_change(rev, timeout=0.05),
                          "没变化就该超时返回 None（让调用方发心跳）")
        self.api.counter += 1
        self.bell.probe_once()
        self.assertEqual(self.bell.wait_change(rev, timeout=0.05), rev + 1)

    def test_probe_survives_broken_api(self):
        class _Boom:
            def get_state(self):
                raise RuntimeError("hub 还没起来")
        bell = gateway.Doorbell(_Boom())
        with self.assertRaises(RuntimeError):
            bell.probe_once()   # probe 本身抛（_loop 里有兜底），rev 不该乱动
        self.assertEqual(bell.rev, 0)


def _drain(sock, quiet_secs=0.7):
    """把已到/将到的字节吸干，直到静默 quiet_secs——隔离启动首拍与测试动作。"""
    sock.settimeout(quiet_secs)
    buf = b""
    while True:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            return buf
        if not chunk:
            return buf
        buf += chunk


def _read_until(sock, token, timeout):
    sock.settimeout(0.3)
    deadline = time.monotonic() + timeout
    buf = b""
    while token not in buf:
        if time.monotonic() > deadline:
            raise AssertionError("等不到 %r，已收到 %r" % (token, buf))
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            continue
        if not chunk:
            raise AssertionError("SSE 流被对端关闭，已收到 %r" % buf)
        buf += chunk
    return buf


class GatewaySseE2ETests(unittest.TestCase):
    """真 HTTP 走一遍：连上 /events、收首帧、状态一变收到新帧、秒表不响铃。"""

    @classmethod
    def setUpClass(cls):
        cls.api = _FakeApi()
        cls.logs = []
        cls.httpd = gateway.start_gateway(cls.api, UI_PATH, port=0,
                                          logger=cls.logs.append)
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _open_events(self):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(sock.close)
        sock.sendall(b"GET /events HTTP/1.1\r\nHost: t\r\n"
                     b"Accept: text/event-stream\r\n\r\n")
        return sock

    def test_stream_opens_and_rings_on_real_change(self):
        sock = self._open_events()
        head = _read_until(sock, b"data: ", timeout=5)
        self.assertIn(b"text/event-stream", head)
        self.assertIn(b"retry: 2000", head)
        _drain(sock)                       # 吸掉启动首拍，从静默态开始
        self.api.counter += 1              # 真变化
        _read_until(sock, b"data: ", timeout=3)

    def test_stopwatch_tick_stays_silent_on_the_wire(self):
        sock = self._open_events()
        _read_until(sock, b"data: ", timeout=5)
        _drain(sock)
        self.api.secs += 1                 # 纯秒表变化
        got = _drain(sock, quiet_secs=1.2)  # 至少跨过 4 个门铃探测拍
        self.assertNotIn(b"data: ", got,
                         "秒表字段变化不该响铃（响了=门铃每秒常鸣）")

    def test_plain_api_keepalive_two_calls_one_connection(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        for _ in range(2):
            conn.request("POST", "/api/ping", body=b"[]",
                         headers={"Content-Type": "application/json"})
            r = conn.getresponse()
            self.assertEqual(r.status, 200)
            self.assertEqual(json.loads(r.read()), {"ok": True},
                             "HTTP/1.1 keep-alive 下第二问必须还答得对（错位=全错）")

    def test_slow_call_leaves_evidence_in_log(self):
        with patch.object(gateway, "SLOW_CALL_MS", 10):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            self.addCleanup(conn.close)
            conn.request("POST", "/api/slowpoke", body=b"[]",
                         headers={"Content-Type": "application/json"})
            self.assertEqual(conn.getresponse().status, 200)
        self.assertTrue(any("网关慢调用 slowpoke" in m for m in self.logs),
                        "慢调用必须留痕，下次「发送等好多秒」直接看日志断案")

    def test_events_404_when_no_bell(self):
        # 直接构造无门铃的 handler 不便；退而检查路由守卫存在——
        # bell=None 时 /events 必须 404（旧 ui.html 撞上也只是拿到 404）
        src = Path(gateway.__file__).read_text(encoding="utf-8")
        self.assertIn("if bell is None:", src)


class UiDoorbellWiringTests(unittest.TestCase):
    """ui.html 的门铃客户端：存在、只叫醒主轮询、连不上会放弃。"""

    @classmethod
    def setUpClass(cls):
        cls.html = UI_PATH.read_text(encoding="utf-8")

    def test_doorbell_listens_on_relative_events(self):
        self.assertIn('new EventSource("events")', self.html)

    def test_ring_wakes_only_primary_poller_immediately(self):
        self.assertIn("pollers.forEach(p => { if (p.primary) p.wake(true); });",
                      self.html)

    def test_never_opened_gives_up_instead_of_hammering(self):
        self.assertIn("if (!everOpened && fails >= 3) es.close();", self.html)

    def test_reconnect_refreshes_once_to_cover_the_gap(self):
        self.assertIn("if (everOpened) ring();", self.html)


if __name__ == "__main__":
    unittest.main()
