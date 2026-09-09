# -*- coding: utf-8 -*-
"""把 hub 里的三件事实告诉 rxyy tools 的「任务面板」：谁在干什么、谁收工了、谁没了。

hub 只发事实，不碰看板业务：投什么卡、能不能挪、版本对不对，全在控制台那一个
进程里判（那边的锁才是有效的，见 console/api/board/storage.py 的说明）。所以这里
只有三个动词，且都不需要知道任何卡片 ID。

**这个模块绝不能拖累 hub。** 它挂在 MCP 消息主循环和判死巡检上，那两条路一卡，
全队的 agent 就一起卡住。所以：投递一律进队列由后台线程发；队列有界且丢最旧；
超时按秒以下算；连续失败就熔断歇一会儿；任何异常一律吞掉只留一行日志。看板事件
是锦上添花，宁可丢，也不能让 hub 慢一拍。

开关在 config.json 的 board_hooks_enabled（默认开），出事随手关掉即可。
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from collections import deque

QUEUE_MAX = 200
# 控制台就在本机，慢成这样只会是它假死或端口被别的东西占着——那就别等了
CONNECT_TIMEOUT = 0.5
READ_TIMEOUT = 1.5
FAIL_LIMIT = 5
COOLDOWN_SECS = 60.0
# agent 干活时 zt 报得很密，同一个会话十几秒内的重复进度没有信息量
THROTTLE_SECS = 12.0
# 控制台一天要重启好几次，令牌和端口在它起来之前根本不存在，隔一阵重探一次
PROBE_RETRY_SECS = 300.0


def _log(text):
    try:
        import hub
        hub.log_event(text)
    except Exception:  # noqa: BLE001  日志本身绝不能成为故障源
        pass


def _cfg_enabled():
    try:
        import hub
        return bool(hub.HUB.cfg.get("board_hooks_enabled", True))
    except Exception:  # noqa: BLE001
        return True


class _Client:
    """控制台远程网关的极简客户端：自己找门牌号和钥匙，找不到就整块歇着。"""

    def __init__(self):
        self._base = ""
        self._token = ""
        self._probed_at = 0.0

    def _probe(self):
        """从控制台的 workflow.db 里问出端口与令牌。

        路径解析必须抄 share_server 那两个现成函数：常驻区在
        %LOCALAPPDATA% 底下，从自己的位置推不出 rxyy tools 装在哪，
        07-29 和 08-07 两次事故都是自己推路径推错的。
        """
        import os
        import sqlite3

        self._probed_at = time.time()
        self._base, self._token = "", ""
        base = (os.getenv("RXYY_CONSOLE_BASE") or "").strip()
        try:
            from share_server import _console_data_dir
            db = _console_data_dir() / "workflow.db"
        except Exception as exc:  # noqa: BLE001
            _log("看板钩子：找不到控制台数据目录（{}），本轮禁用".format(exc))
            return
        if not db.is_file():
            return
        try:
            con = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True, timeout=2)
            try:
                rows = dict(con.execute(
                    "SELECT k, v FROM kv_config WHERE k IN "
                    "('console_remote_token','console_remote_port','console_remote_enabled')"
                ).fetchall())
            finally:
                con.close()
        except sqlite3.Error:
            return
        if str(rows.get("console_remote_enabled", "1")).strip().lower() in ("0", "false", "off"):
            return
        token = str(rows.get("console_remote_token") or "").strip()
        if not token:
            return  # 控制台从没起过远程网关，钩子静默歇着，别每条 zt 去撞一次 TCP
        port = str(rows.get("console_remote_port") or "").strip() or "39090"
        self._base = base or "http://127.0.0.1:%s" % port
        self._token = token

    def ready(self):
        if self._token and self._base:
            return True
        if time.time() - self._probed_at < PROBE_RETRY_SECS:
            return False
        self._probe()
        return bool(self._token and self._base)

    def post(self, method, args):
        request = urllib.request.Request(
            self._base.rstrip("/") + "/api/" + method,
            data=json.dumps(args, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "X-Requested-With": "rxyy",  # 网关的同源闸门要这个头
                     "X-Console-Token": self._token},
            method="POST")
        with urllib.request.urlopen(request, timeout=READ_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")

    def forget(self):
        """令牌可能因为控制台换装而失效，下次 ready() 重探。"""
        self._token = ""


class _Sender:
    def __init__(self):
        self._queue = deque(maxlen=QUEUE_MAX)  # 满了自动丢最旧，看板事件丢得起
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._worker = None
        self._client = _Client()
        self._fails = 0
        self._muted_until = 0.0
        self._last_note = {}  # conv -> (文本, 时刻)，zt 节流用

    def submit(self, method, args):
        if not _cfg_enabled():
            return
        with self._lock:
            self._queue.append((method, args))
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._run, name="board-hooks", daemon=True)
                self._worker.start()
        self._wake.set()

    def _run(self):
        while True:
            with self._lock:
                item = self._queue.popleft() if self._queue else None
            if item is None:
                # 空转一小会儿就让线程退掉，下次投递再拉起来：hub 是长命进程，
                # 没必要为一个偶尔用一次的通道常驻一根线程
                if not self._wake.wait(30):
                    with self._lock:
                        if not self._queue:
                            self._worker = None
                            return
                self._wake.clear()
                continue
            self._deliver(*item)

    def _deliver(self, method, args):
        if time.time() < self._muted_until:
            return
        try:
            if not self._client.ready():
                return
            reply = self._client.post(method, args)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                # 控制台比 hub 旧，还没有这个方法。重试没有意义，也不该把另外
                # 两个能用的钩子一起拖进熔断——记一行，等它下次换装就有了
                _log("看板钩子 {} 在当前控制台版本上不存在，跳过".format(method))
                return
            if exc.code == 403:
                self._client.forget()  # 换装后令牌变了，下次重探
            self._note_failure("{} HTTP {}".format(method, exc.code))
            return
        except Exception as exc:  # noqa: BLE001  控制台没开着是常态，不该刷屏
            self._note_failure("{} {}".format(method, exc))
            return
        self._fails = 0
        if isinstance(reply, dict) and not reply.get("ok"):
            # 业务侧的拒绝多半是正常的：卡不在它名下、重复收工、没有在做的卡
            code = reply.get("code") or ""
            if code not in ("", "INVALID_MOVE", "NOT_OWNER", "NOT_ACTIVE", "NOT_IN_REVIEW"):
                _log("看板钩子 {} 被拒：{}".format(method, reply.get("error")))

    def _note_failure(self, why):
        self._fails += 1
        if self._fails >= FAIL_LIMIT:
            self._muted_until = time.time() + COOLDOWN_SECS
            self._fails = 0
            _log("看板钩子连续失败，静默 {:.0f} 秒（{}）".format(COOLDOWN_SECS, why))

    # ---- 三个动词 --------------------------------------------------------
    def note_progress(self, conv_key, text):
        conv, text = str(conv_key or "").strip(), str(text or "").strip()
        if not conv or not text:
            return
        seen = self._last_note.get(conv)
        now = time.time()
        if seen and seen[0] == text and now - seen[1] < THROTTLE_SECS:
            return
        self._last_note[conv] = (text, now)
        self.submit("board_note_activity", [conv, text[:200]])

    def finish_session(self, conv_key, note=""):
        conv = str(conv_key or "").strip()
        if conv:
            self.submit("board_finish_session", [conv, str(note or "")[:400]])

    def release_session(self, conv_key, reason=""):
        conv = str(conv_key or "").strip()
        if conv:
            self.submit("board_release_session", [conv, str(reason or "")[:400]])


_SENDER = _Sender()


def note_progress(conv_key, text):
    """zt 上报：这个会话此刻在干什么（同会话十几秒内的重复会被吃掉）。"""
    try:
        _SENDER.note_progress(conv_key, text)
    except Exception:  # noqa: BLE001
        pass


def finish_session(conv_key, note=""):
    """黑板「收工」：这个会话名下执行中的卡该进待验收了。"""
    try:
        _SENDER.finish_session(conv_key, note)
    except Exception:  # noqa: BLE001
        pass


def release_session(conv_key, reason=""):
    """会话判死：它名下的卡别占着坑，退回去让别人接。"""
    try:
        _SENDER.release_session(conv_key, reason)
    except Exception:  # noqa: BLE001
        pass
