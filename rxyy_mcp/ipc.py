# -*- coding: utf-8 -*-
"""本地 TCP 消息协议：4 字节长度前缀 + UTF-8 JSON，可承载大图 base64。
另提供全项目共用的「独占绑定」HTTP server 工厂（Windows 防端口劫持），
以及 39222 并入 hub 后 MCP 桥用的进程内管道 InProcSock。"""
import json
import queue
import socket
import struct
from http.server import ThreadingHTTPServer


class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    """Windows 加固版 ThreadingHTTPServer（39222/38777/39080 共用）：

    1. 独占绑定：http.server 默认 allow_reuse_address=1，Windows 语义是
       SO_REUSEADDR=「允许别的 socket 再绑同一端口并劫持流量」——两个进程都
       "成功"监听同一端口、行为不可预测（07-27 半死进程疑似诱因之一）。
       改用 SO_EXCLUSIVEADDRUSE，第二个绑定者立刻 OSError，单例语义确定。
    2. backlog 调大：默认 request_queue_size=5，进程哪怕只是被磁盘/换页卡住
       几秒，5 个排队名额一满 Windows 就对新连接直接回 RST——Cursor 端表现为
       ERR_CONNECTION_REFUSED（明明进程还活着）。放大到 64 扛住重连风暴。
    """
    allow_reuse_address = False
    request_queue_size = 64
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class InProcSock:
    """进程内双向管道的一端，鸭子类型模拟 send_msg/recv_msg 所需的最小 socket 面。

    39222 MCP 端点并入 hub 进程后（2026-08-12 hub 拆分第二刀），HubBridge 与
    Hub 同进程，39222→38999 的本机 TCP 桥失去意义。用一对 InProcSock 替代那条
    TCP 连接：sendall 入对端队列、recv 出自己队列，4 字节长度前缀 + JSON 的
    字节协议原样走——send_msg/recv_msg、hub.handle_client（含 hello 握手）、
    server 侧 _reader 线程一行不用改；per-connection 读取线程的串行处理语义、
    close → 对端 recv 得空（EOF）的断连语义都与 TCP 完全一致。消除的只有
    网络栈本身：没有端口、没有半开连接、没有重连竞态。
    """

    def __init__(self):
        self._rx = queue.Queue()
        self._peer = None
        self._buf = b""
        self._timeout = None   # None=阻塞，与 socket.settimeout 同语义
        self._closed = False
        self._eof = False

    @classmethod
    def pair(cls):
        """像 socketpair 一样返回互联的两端。"""
        a, b = cls(), cls()
        a._peer, b._peer = b, a
        return a, b

    def sendall(self, data):
        peer = self._peer
        if self._closed or peer is None:
            raise OSError("inproc socket 已关闭")
        peer._rx.put(bytes(data))

    def recv(self, n):
        if self._closed or self._eof:
            return b""
        if not self._buf:
            try:
                chunk = self._rx.get(timeout=self._timeout)
            except queue.Empty:
                raise socket.timeout("inproc recv 超时")
            if chunk is None:  # 对端 close 的哨兵 = EOF
                self._eof = True
                return b""
            self._buf = chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def settimeout(self, t):
        self._timeout = t

    def setsockopt(self, *a, **kw):
        pass  # handle_client 会做 TCP keepalive 调优，进程内没有网络栈，直接吞掉

    def ioctl(self, *a, **kw):
        pass

    def close(self):
        if self._closed:
            return
        self._closed = True
        peer = self._peer
        if peer is not None:
            peer._rx.put(None)


def send_msg(sock, obj):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sock.sendall(struct.pack(">I", len(data)) + data)


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def recv_msg(sock):
    header = _recv_exact(sock, 4)
    if header is None:
        return None
    (length,) = struct.unpack(">I", header)
    if length > 256 * 1024 * 1024:
        return None
    data = _recv_exact(sock, length)
    if data is None:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except Exception:
        return None
