# -*- coding: utf-8 -*-
"""会话快照的故障隔离：一个坏会话不废整份快照、写盘失败要留痕。

07-31 事故：重启后 4 个待命 tab 消失。save_state 原来一层 try 包全部——
任何一个会话坏了整份快照就静默不写，重启只能按旧快照恢复。
"""
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub


def _session(sid, conv, name):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = sid, conv, name
    s.name_locked = False
    s.cwd = r"d:\Desktop\cursor工作流"
    s.peer_ip = None
    s.task_root = s.cwd
    s.pid = 1
    s.created_at = "2026-07-31 14:00:00"
    s.file_path = None
    s.pending = None
    s.pending_lost = False
    s.cursor_uuid = None
    s.cursor_title = None
    s.transcript_path = None
    s.created_ts = time.time()
    s.rev = 0
    s.msg_seq = 0
    s.messages = [{"role": "sys", "ts": "14:00", "html": "对话已开始"}]
    s.queued = []
    s.lock = threading.Lock()
    return s


class SnapshotFaultIsolationTests(unittest.TestCase):
    def _save_and_load(self, sessions, tmp):
        order = [s.id for s in sessions]
        with (patch.object(hub.Hub, "STATE_PATH", tmp),
              patch.object(hub.HUB, "sessions", {s.id: s for s in sessions}),
              patch.object(hub.HUB, "order", order)):
            hub.HUB.save_state()
        if not tmp.is_file():
            return None
        return json.loads(tmp.read_text(encoding="utf-8"))

    def test_one_broken_session_does_not_kill_the_whole_snapshot(self):
        import tempfile
        good1 = _session("s1", "c1", "好会话1")
        bad = _session("s2", "c2", "坏会话")
        del bad.messages          # 属性缺失 → 采集时抛 AttributeError
        good2 = _session("s3", "c3", "好会话2")
        with tempfile.TemporaryDirectory() as td:
            snap = self._save_and_load([good1, bad, good2], Path(td) / "snap.json")
        self.assertIsNotNone(snap, "快照必须写出来，不能因一个坏会话整份放弃")
        self.assertEqual(["c1", "c3"], [d["conv_key"] for d in snap])

    def test_unserializable_field_degrades_to_string_instead_of_losing_all(self):
        import tempfile
        s1 = _session("s1", "c1", "带怪字段")
        s1.cursor_title = object()   # json 序列化不了 → default=str 兜底
        s2 = _session("s2", "c2", "正常")
        with tempfile.TemporaryDirectory() as td:
            snap = self._save_and_load([s1, s2], Path(td) / "snap.json")
        self.assertIsNotNone(snap)
        self.assertEqual({"c1", "c2"}, {d["conv_key"] for d in snap})

    def test_queued_messages_survive_a_restart(self):
        # 07-31 实测：用户 14:10 排队的长消息因重启丢失，agent 压根没收到
        import tempfile
        s = _session("s1", "c1", "带排队")
        s.queued = [{"id": "q1", "text": "重启前排队的话", "selected": ["A"],
                     "images": [], "files": [], "msg": {"role": "user"},
                     "who": None, "defer_until": 1999999999.0,
                     "reply_to": "req-before-restart"}]
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "snap.json"
            with (patch.object(hub.Hub, "STATE_PATH", tmp),
                  patch.object(hub.HUB, "sessions", {"s1": s}),
                  patch.object(hub.HUB, "order", ["s1"])):
                hub.HUB.save_state()
            restored_sessions, restored_order = {}, []
            with (patch.object(hub.Hub, "STATE_PATH", tmp),
                  patch.object(hub.HUB, "sessions", restored_sessions),
                  patch.object(hub.HUB, "order", restored_order)):
                hub.HUB.load_state()
            self.assertEqual(1, len(restored_order))
            q = restored_sessions[restored_order[0]].queued
            self.assertEqual(1, len(q))
            self.assertEqual("重启前排队的话", q[0]["text"])
            self.assertEqual(["A"], q[0]["selected"])
            self.assertEqual(1999999999.0, q[0]["defer_until"])
            self.assertEqual("req-before-restart", q[0]["reply_to"])

    def test_zt_trail_survives_a_restart_for_the_next_takeover(self):
        # 08-28：接手几乎总发生在 hub 重启之后（前任断了才要人接）；
        # zt 轨迹只活在内存里就等于没有——接手词靠它告诉接手方
        # 「前任断线前正在做什么」。当前状态清零、轨迹保留，且有界（5 条）。
        import tempfile
        s = _session("s1", "c1", "带轨迹")
        s.zt_trail = ["08-28 10:0%d 步骤%d · x" % (i, i) for i in range(7)]
        s.agent_status = "developing"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "snap.json"
            with (patch.object(hub.Hub, "STATE_PATH", tmp),
                  patch.object(hub.HUB, "sessions", {"s1": s}),
                  patch.object(hub.HUB, "order", ["s1"])):
                hub.HUB.save_state()
            restored_sessions, restored_order = {}, []
            with (patch.object(hub.Hub, "STATE_PATH", tmp),
                  patch.object(hub.HUB, "sessions", restored_sessions),
                  patch.object(hub.HUB, "order", restored_order)):
                hub.HUB.load_state()
        restored = restored_sessions[restored_order[0]]
        self.assertEqual(s.zt_trail[-5:], restored.zt_trail)
        self.assertEqual("", restored.agent_status,
                         "当前状态是「此刻」的事，重启后不该冒充还活着")

    def test_a_session_without_a_trail_still_snapshots(self):
        # 老快照/老会话对象没有 zt_trail 属性，采集时不能因此炸掉
        import tempfile
        s = _session("s1", "c1", "无轨迹")
        self.assertFalse(hasattr(s, "zt_trail"))
        with tempfile.TemporaryDirectory() as td:
            snap = self._save_and_load([s], Path(td) / "snap.json")
        self.assertIsNotNone(snap)
        self.assertEqual([], snap[0]["zt_trail"])

    def test_flush_after_restart_clears_queued_badge_on_the_bubble(self):
        # 07-31 22:48 用户实测：排队消息重启后送达了 AI，但用户侧气泡仍显示
        # 「排队中」。根因：JSON 落盘把队列条目里的 msg 引用和 messages 里的
        # 气泡拆成两个对象，送达时只改了脱钩副本。
        import tempfile
        s = _session("s1", "c1", "带排队")
        bubble = {"role": "user", "ts": "14:10", "html": "排队的话",
                  "queued": True, "qid": "q1"}
        s.messages.append(bubble)
        s.queued = [{"id": "q1", "text": "排队的话", "images": [],
                     "files": [], "msg": bubble, "who": None}]
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "snap.json"
            with (patch.object(hub.Hub, "STATE_PATH", tmp),
                  patch.object(hub.HUB, "sessions", {"s1": s}),
                  patch.object(hub.HUB, "order", ["s1"])):
                hub.HUB.save_state()
            restored_sessions, restored_order = {}, []
            with (patch.object(hub.Hub, "STATE_PATH", tmp),
                  patch.object(hub.HUB, "sessions", restored_sessions),
                  patch.object(hub.HUB, "order", restored_order)):
                hub.HUB.load_state()
        r = restored_sessions[restored_order[0]]
        r.pending = {"id": "req1"}
        r.send = lambda obj: None   # 通道正常，直接送达
        self.assertTrue(hub.HUB._flush_queue(r))
        stuck = [m for m in r.messages if m.get("qid") == "q1" and m.get("queued")]
        self.assertEqual([], stuck, "送达后气泡必须不再显示「排队中」")

    def test_first_queued_dispatch_receives_library_digest_once(self):
        s = _session("s1", "c1", "首发索引")
        bubble = {"role": "user", "ts": "14:10", "html": "首个任务",
                  "queued": True, "qid": "q1"}
        s.messages.append(bubble)
        s.queued = [{"id": "q1", "text": "首个任务", "images": [],
                     "files": [], "msg": bubble, "who": None}]
        s.pending = {"id": "req1"}
        sent = []
        s.send = sent.append
        old = hub.HUB.cfg.get("library_autosend", True)
        hub.HUB.cfg["library_autosend"] = True
        try:
            with patch.object(hub.Api, "library_digest", return_value="[INDEX]\n"):
                self.assertTrue(hub.HUB._flush_queue(s))
                self.assertTrue(s.library_sent)
                self.assertEqual("[INDEX]\n首个任务", sent[0]["user_input"])
        finally:
            hub.HUB.cfg["library_autosend"] = old

    def test_library_claim_is_released_when_queued_dispatch_fails(self):
        s = _session("s1", "c1", "索引失败")
        bubble = {"role": "user", "ts": "14:10", "html": "别丢我",
                  "queued": True, "qid": "q1"}
        s.messages.append(bubble)
        s.queued = [{"id": "q1", "text": "别丢我", "images": [],
                     "files": [], "msg": bubble, "who": None}]
        s.pending = {"id": "req1"}
        s.send = lambda obj: (_ for _ in ()).throw(OSError("connection lost"))
        old = hub.HUB.cfg.get("library_autosend", True)
        hub.HUB.cfg["library_autosend"] = True
        try:
            with patch.object(hub.Api, "library_digest", return_value="[INDEX]\n"):
                self.assertFalse(hub.HUB._flush_queue(s))
                self.assertFalse(getattr(s, "library_sent", False))
        finally:
            hub.HUB.cfg["library_autosend"] = old

    def test_restore_heals_stale_queued_badges(self):
        # 修复上线前已经送达、但标记被脱钩固化的历史气泡：恢复快照时一次清掉
        # （队列里已经没有它了 = 它早就送达/撤回过）；还在队列里的不许动
        import tempfile
        s = _session("s1", "c1", "带遗留")
        s.messages.append({"role": "user", "ts": "22:18", "html": "早送达了",
                           "queued": True, "qid": "old1"})
        s.messages.append({"role": "user", "ts": "23:00", "html": "还在排队",
                           "queued": True, "qid": "new1"})
        s.queued = [{"id": "new1", "text": "还在排队", "images": [],
                     "files": [], "msg": s.messages[-1], "who": None}]
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "snap.json"
            with (patch.object(hub.Hub, "STATE_PATH", tmp),
                  patch.object(hub.HUB, "sessions", {"s1": s}),
                  patch.object(hub.HUB, "order", ["s1"])):
                hub.HUB.save_state()
            restored_sessions, restored_order = {}, []
            with (patch.object(hub.Hub, "STATE_PATH", tmp),
                  patch.object(hub.HUB, "sessions", restored_sessions),
                  patch.object(hub.HUB, "order", restored_order)):
                hub.HUB.load_state()
        r = restored_sessions[restored_order[0]]
        flags = {m.get("qid"): bool(m.get("queued"))
                 for m in r.messages if m.get("qid")}
        self.assertFalse(flags["old1"], "已不在队列里的历史气泡必须清掉排队标记")
        self.assertTrue(flags["new1"], "真还在排队的不许误清")

    def test_flush_send_failure_puts_entries_back_in_queue(self):
        # 送达那一刻通道恰好断了：排队消息必须放回队列（agent 重试 zhi 时
        # 还能送达），不能弹出后静默蒸发；气泡也保持排队中不许误标已送达
        s = _session("s1", "c1", "断通道")
        bubble = {"role": "user", "ts": "14:10", "html": "别丢我",
                  "queued": True, "qid": "q1"}
        s.messages.append(bubble)
        s.queued = [{"id": "q1", "text": "别丢我", "images": [],
                     "files": [], "msg": bubble, "who": None}]
        s.pending = {"id": "req1"}
        def boom(obj):
            raise OSError("connection lost")
        s.send = boom
        self.assertFalse(hub.HUB._flush_queue(s))
        self.assertEqual(["q1"], [e["id"] for e in s.queued],
                         "发送失败的排队消息必须放回队列")
        self.assertTrue(bubble.get("queued"), "没送出去的气泡得继续显示排队中")

    def test_window_tag_groups_by_workspace_dir_not_pid(self):
        # 共享 MCP 守护进程后所有会话同一个 pid：按 (pid,项目名) 编号会把
        # 不同窗口编成一个号（07-31 用户指正）。按工作区编才对得上窗口。
        h = hub.Hub.__new__(hub.Hub)
        t1 = h._window_tag(100, r"d:\Desktop\cursor工作流")
        t2 = h._window_tag(100, r"D:/Desktop/CURSOR工作流")   # 同目录不同写法
        t3 = h._window_tag(100, r"c:\Users\Administrator\AICodebrain")
        t4 = h._window_tag(999, r"d:\Desktop\cursor工作流")   # pid 变了不影响
        self.assertEqual(t1, t2)
        self.assertEqual(t1, t4)
        self.assertNotEqual(t1, t3)

    def test_window_tag_trusts_transcript_slug_over_wrong_cwd(self):
        # 报到没带 project_path 时 cwd 是错的（回落成 MCP 进程目录被折回本仓）；
        # 但流水一定写在它自己窗口的工作区槽位下——按流水 slug 归队
        h = hub.Hub.__new__(hub.Hub)
        right = h._window_tag(100, r"c:\Users\Administrator\AICodebrain")
        fixed = h._window_tag(
            100, r"d:\Desktop\cursor工作流",   # cwd 落错了
            r"C:\Users\Administrator\.cursor\projects\c-users-administrator-aicodebrain"
            r"\agent-transcripts\ab\ab.jsonl")
        other = h._window_tag(100, r"d:\Desktop\cursor工作流")
        self.assertEqual(right, fixed)
        self.assertNotEqual(right, other)

    def test_write_failure_is_logged_not_swallowed(self):
        events = []
        with (patch.object(hub, "log_event", events.append),
              patch.object(hub.Hub, "STATE_PATH",
                           Path(r"Z:\不存在的盘\snap.json")),
              patch.object(hub.HUB, "sessions", {"s1": _session("s1", "c1", "会话")}),
              patch.object(hub.HUB, "order", ["s1"])):
            hub.HUB._save_warn = (0, "")
            hub.HUB.save_state()
        self.assertTrue(any("快照" in e for e in events),
                        "写盘失败必须在日志里喊出来，不能 except: pass")


if __name__ == "__main__":
    unittest.main()
