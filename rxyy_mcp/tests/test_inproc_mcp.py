# -*- coding: utf-8 -*-
"""hub 拆分第二刀：39222 并入 hub 后的进程内 MCP 桥。

39222→38999 的本机 TCP 桥换成 ipc.InProcSock 管道后，协议字节流、hello 握手、
per-connection 串行读取、断连语义必须与 TCP 一字不差——SSE/progress/KEEPALIVE/
让路逻辑全部压在这条桥上，桥的语义漂一点，上面全歪。

hub 侧 _handle_client_msg 的业务逻辑已有各自的测试锁着（test_agent_mail 等直接
喂消息），这里锁的是桥本身：管道的 socket 鸭子面、_try_connect 的分支选择、
ask/zt 全链路在管道上的往返。真 Hub 实例 + 内嵌 HTTP 的组合走隔离实例冒烟。
"""
import socket
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import server  # noqa: E402
from ipc import InProcSock, recv_msg, send_msg  # noqa: E402


class InProcSockTests(unittest.TestCase):
    """管道的 socket 鸭子面：send_msg/recv_msg 所依赖的每一条语义。"""

    def test_message_roundtrip_both_ways(self):
        a, b = InProcSock.pair()
        send_msg(a, {"type": "hello", "pid": 1})
        self.assertEqual({"type": "hello", "pid": 1}, recv_msg(b))
        send_msg(b, {"type": "hello_ack"})
        self.assertEqual({"type": "hello_ack"}, recv_msg(a))

    def test_large_payload_survives_chunked_recv(self):
        # recv(n) 每次最多给 n 字节，_recv_exact 靠循环凑齐——大图 base64 的路径
        a, b = InProcSock.pair()
        big = {"type": "zhi_response", "images": ["x" * (1024 * 1024)]}
        send_msg(a, big)
        self.assertEqual(big, recv_msg(b))

    def test_close_reads_as_eof_and_stays_eof(self):
        a, b = InProcSock.pair()
        a.close()
        self.assertIsNone(recv_msg(b))   # 对端 close = EOF，与 TCP 一致
        self.assertIsNone(recv_msg(b))   # EOF 是终态，不能又阻塞回去

    def test_recv_honors_timeout(self):
        a, b = InProcSock.pair()
        b.settimeout(0.05)
        with self.assertRaises(socket.timeout):
            recv_msg(b)

    def test_send_after_close_raises(self):
        a, b = InProcSock.pair()
        a.close()
        with self.assertRaises(OSError):
            send_msg(a, {"type": "ping"})

    def test_tcp_tuning_calls_are_noops(self):
        # hub.handle_client 进门就做 keepalive 调优，鸭子面必须接得住
        a, _ = InProcSock.pair()
        a.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        a.ioctl(None, (1, 10000, 2000))


class _StubHub:
    """只演 handle_client 这一幕的 hub 替身：hello 握手 + 按消息类型应答。

    与真 hub 同构：一条专属线程、recv_msg 循环、send_msg 应答——桥那头
    （_try_connect/_reader/ask）感知不到差别。
    """

    def __init__(self):
        self.hello = None
        self.got = []
        self.disconnected = threading.Event()

    def handle_client(self, sock, addr=("127.0.0.1", 0)):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self.hello = recv_msg(sock)
        if not self.hello or self.hello.get("type") != "hello":
            sock.close()
            return
        send_msg(sock, {"type": "hello_ack"})
        while True:
            msg = recv_msg(sock)
            if msg is None:
                break
            self.got.append(msg)
            if msg.get("type") == "zhi_request":
                send_msg(sock, {
                    "type": "zhi_response", "id": msg.get("id"),
                    "user_input": "收到：" + str(msg.get("message") or ""),
                    "selected_options": [], "images": [],
                })
            elif msg.get("type") == "agent_mail_fetch":
                send_msg(sock, {
                    "type": "mail_response", "id": msg.get("id"),
                    "conversation_id": msg.get("conversation_id"),
                    "items": ["[agent·乙] 管道通了"],
                })
        self.disconnected.set()
        sock.close()


class InProcBridgeTests(unittest.TestCase):
    """HubBridge 在管道上的全链路：接线、握手、zhi 往返、zt 顺路取信。"""

    def setUp(self):
        self.stub = _StubHub()
        self.bridge = server.HubBridge()
        self.bridge._hb_started = True  # 心跳线程别起：它会把测试对话写进真机名单
        self._patches = [
            patch.object(server, "_INPROC_HUB", self.stub),
            patch.object(server, "load_hub_target",
                         return_value=("127.0.0.1", 38999, "")),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        sock = self.bridge.sock
        if sock is not None:
            self.bridge._drop(sock)
        for p in self._patches:
            p.stop()

    def test_try_connect_returns_pipe_and_shakes_hands(self):
        s = self.bridge._try_connect()
        self.assertIsInstance(s, InProcSock)
        self.assertEqual("hello", (self.stub.hello or {}).get("type"))

    def test_ask_roundtrip_over_pipe(self):
        resp = self.bridge.ask("管道在吗", [], True,
                               conversation_id="inproctest", task_name="测试")
        self.assertEqual("收到：管道在吗", resp.get("user_input"))
        req = next(m for m in self.stub.got if m.get("type") == "zhi_request")
        self.assertEqual("inproctest", req.get("conversation_id"))

    def test_zt_status_and_mail_ride_the_pipe(self):
        mail = self.bridge.report_status("inproctest", "developing", "试管道")
        self.assertEqual(["[agent·乙] 管道通了"], mail)
        st = next(m for m in self.stub.got if m.get("type") == "agent_status")
        self.assertEqual("developing", st.get("status"))

    def test_bridge_close_reaches_hub_as_disconnect(self):
        # server 侧 _drop 后 hub 侧读到 EOF 退出循环 = TCP 断连语义原样保留，
        # hub 的重连宽限（_client_disconnect）才接得上
        s = self.bridge._try_connect()
        self.bridge._drop(s)
        self.assertTrue(self.stub.disconnected.wait(timeout=2))

    def test_remote_hub_still_goes_tcp(self):
        # hub_host 指向远程机器时（把会话接到对方控制台），管道不得劫持。
        # 0.0.0.1 是不可路由地址，connect 立即失败——失败即证明走了 TCP 而非管道
        with patch.object(server, "load_hub_target",
                          return_value=("0.0.0.1", 38999, "tok")):
            with self.assertRaises(OSError):
                self.bridge._try_connect()


class StdioModeUnchangedTests(unittest.TestCase):
    """stdio 独立进程形态（同事接入）一字不动：_INPROC_HUB 恒 None 走 TCP。"""

    def test_default_is_tcp(self):
        bridge = server.HubBridge()
        bridge._hb_started = True
        self.assertIsNone(server._INPROC_HUB)
        with patch.object(server, "load_hub_target",
                          return_value=("127.0.0.1", 1, "")):
            with self.assertRaises(OSError):
                bridge._try_connect()  # 端口 1 必拒 → 走的是真 TCP


if __name__ == "__main__":
    unittest.main()
