# -*- coding: utf-8 -*-
"""转世对话自动换绑——08-28 音视频编辑·env审计·30d6 实案。

现场：Cursor 窗口 16:35 重载后，正在跑的云端 agent 对话「转世」——同一个对话换了
新 composer uuid 继续干活，旧 uuid 的 composerData 被 Cursor 整个回收。判死探针
只看旧 uuid：「对话已从 Cursor 里消失」，tab 在待续组躺了 5 小时，而 agent 一直
在写文件（AI 改档流水 22:05 还有记录）。hub 原本只在 zhi/zt 到达时校准身份，
从不调 MCP 的长跑 agent 永远等不来那一刻。

契约：
* gone 判死前先查转世——存根首消息里有本会话 conversation_id、uuid 无人认领、
  近 2h 有动静（改档流水/存根落盘）→ 当拍换绑复活，不落死因不响铃；
* 证据脏（同一存根能对上两个会话）或凉透（超 2h 没动静）→ 不绑，gone 照判；
* AI 改档流水是判活第五路信号：融合让路认它，断开侧 liveness 也认它。
"""
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub  # noqa: E402
import session_core  # noqa: E402
import session_locator  # noqa: E402

CHECKIN = ("立即调用rxyy MCP的zhi报到：conversation_id=「4ccbbe49」全程沿用；"
           "message=「📍 workspace · 对话 4ccbbe49 已就位」")


class _Host:
    def __init__(self, every=0.0):
        self.DEATH_PROBE_EVERY = every
        self.alerted = []

    def _fusion_recent_activity(self, s, now):
        return ""

    def _rescue_swallowed_reply(self, s, why="", note=""):
        pass

    def alert_session_death(self, s, dead):
        self.alerted.append(dead)


def _sess(conv="22a030d6", uid="u-old", history=("4ccbbe49",)):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = "s-" + conv, conv, "音视频编辑·env审计·30d6"
    s.cursor_uuid = uid
    s.uuid_verified = True
    s.death_probe_uid = uid
    s.death_probe_at = 0.0
    s.death_info = None
    s.death_alerts = {}
    s.agent_status_ts = 0
    s.processing_since = None
    s.last_zhi_ts = 0
    s.pending = None
    s.id_history = list(history)
    s.transcript_path = None
    return s


class _FakeHub:
    def __init__(self, sessions):
        self.sessions = {x.id: x for x in sessions}


def _stub(convs, path="stub.jsonl", age=60.0, now=None):
    now = now or time.time()
    return {"convs": set(convs), "path": path, "mtime": now - age}


class StubParsingTests(unittest.TestCase):
    def test_checkin_prompt_yields_the_conv_id(self):
        self.assertIn("4ccbbe49", session_locator.stub_conv_ids(CHECKIN))

    def test_uuid_fragments_do_not_count(self):
        # 存根里满地都是 composer uuid（a1708989-2bd0-…），前 8 位 hex 不能被
        # 当成 conversation_id 认亲
        text = "transcript a1708989-2bd0-4c2c-a7fd-f891b7130414 里有 uuid"
        self.assertEqual(set(), session_locator.stub_conv_ids(text))

    def test_scan_reads_the_first_message(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "d-x" / "agent-transcripts" / "11077f63-aa"
            d.mkdir(parents=True)
            p = d / "11077f63-aa.jsonl"
            p.write_text(
                '{"role":"user","message":{"content":[{"type":"text",'
                '"text":"conversation_id=「4ccbbe49」全程沿用"}]}}\n',
                encoding="utf-8")
            out = session_locator.scan_agent_stub_convs(projects_root=td)
        self.assertIn("11077f63-aa", out)
        self.assertIn("4ccbbe49", out["11077f63-aa"]["convs"])


class EditStreamTests(unittest.TestCase):
    def test_edit_map_reads_the_tracking_db(self):
        with tempfile.TemporaryDirectory() as td:
            db_dir = Path(td) / ".cursor" / "ai-tracking"
            db_dir.mkdir(parents=True)
            con = sqlite3.connect(str(db_dir / "ai-code-tracking.db"))
            con.execute("CREATE TABLE ai_code_hashes (hash TEXT PRIMARY KEY,"
                        " conversationId TEXT, timestamp INTEGER)")
            now_ms = int(time.time() * 1000)
            con.execute("INSERT INTO ai_code_hashes VALUES ('h1','u-new',?)",
                        (now_ms - 5000,))
            con.execute("INSERT INTO ai_code_hashes VALUES ('h2','u-new',?)",
                        (now_ms - 90000,))
            con.commit()
            con.close()
            out = session_locator.agent_edit_ts_map(home=td)
        self.assertAlmostEqual((now_ms - 5000) / 1000.0, out["u-new"], places=2)

    def test_missing_db_means_empty_map(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual({}, session_locator.agent_edit_ts_map(home=td))


class RelocateTests(unittest.TestCase):
    def setUp(self):
        p = patch.object(hub, "log_event", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)

    def _relocate(self, s, stubs, edits, others=()):
        fake = _FakeHub([s] + list(others))
        with patch.object(hub, "HUB", fake), \
             patch.object(hub, "scan_agent_stub_convs", lambda **k: stubs), \
             patch.object(hub, "agent_edit_ts_map", lambda **k: edits):
            return session_core.relocate_reincarnated(_Host(), s, time.time())

    def test_reincarnated_dialog_is_rebound(self):
        # 30d6 主形态：旧 uuid 被回收，新 uuid 的存根写着本会话的曾用 ID、
        # 改档流水几分钟前还有记录 → 换绑复活
        s = _sess()
        s.death_info = {"code": "gone", "bubble_id": "gone:u-old"}
        now = time.time()
        got = self._relocate(s, {"u-new": _stub({"4ccbbe49"}, path="p.jsonl")},
                             {"u-new": now - 120})
        self.assertEqual("u-new", got)
        self.assertEqual("u-new", s.cursor_uuid)
        self.assertTrue(s.uuid_verified)
        self.assertIsNone(s.death_info)
        self.assertEqual("p.jsonl", s.transcript_path)

    def test_conv_key_itself_also_matches(self):
        s = _sess(history=())
        got = self._relocate(s, {"u-new": _stub({"22a030d6"})},
                             {"u-new": time.time() - 60})
        self.assertEqual("u-new", got)

    def test_a_cold_corpse_is_not_bound(self):
        # 超过采信窗口没动静的候选是尸体：绑上只会 10 秒一换来回抖
        s = _sess()
        got = self._relocate(
            s, {"u-new": _stub({"4ccbbe49"}, age=3 * 3600)}, {})
        self.assertIsNone(got)
        self.assertEqual("u-old", s.cursor_uuid)

    def test_a_claimed_uuid_is_not_stolen(self):
        s = _sess()
        other = _sess(conv="ffff0001", uid="u-new", history=())
        got = self._relocate(s, {"u-new": _stub({"4ccbbe49"})},
                             {"u-new": time.time()}, others=[other])
        self.assertIsNone(got)

    def test_an_ambiguous_stub_binds_nobody(self):
        # 存根同时对得上两个会话（接手词里新旧 ID 都在）= 证据脏，宁可不绑
        s = _sess()
        other = _sess(conv="4ccbbe49", uid="u-x", history=())
        got = self._relocate(s, {"u-new": _stub({"4ccbbe49"})},
                             {"u-new": time.time()}, others=[other])
        self.assertIsNone(got)

    def test_freshest_candidate_wins(self):
        s = _sess()
        now = time.time()
        got = self._relocate(
            s,
            {"u-a": _stub({"4ccbbe49"}, path="a.jsonl", age=1800),
             "u-b": _stub({"4ccbbe49"}, path="b.jsonl", age=1800)},
            {"u-b": now - 30})
        self.assertEqual("u-b", got)


class GoneProbeRelocatesTests(unittest.TestCase):
    """判死探针端到端：exists=False 的 gone 死法先走转世换绑，绑上就不落死因。"""

    def setUp(self):
        self.patches = [
            patch.object(hub, "log_event", lambda *a, **k: None),
            patch.object(hub, "WORKFLOW", None),
            patch.object(hub, "read_cursor_error", lambda uid: None),
            patch.object(hub, "cursor_conversation_exists", lambda uid: False),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])
        import board_hooks
        rel = patch.object(board_hooks, "release_session", lambda *a, **k: None)
        rel.start()
        self.addCleanup(rel.stop)

    def test_gone_with_a_live_successor_revives_in_place(self):
        s = _sess()
        host = _Host()
        with patch.object(hub, "HUB", _FakeHub([s])), \
             patch.object(hub, "scan_agent_stub_convs",
                          lambda **k: {"u-new": _stub({"4ccbbe49"})}), \
             patch.object(hub, "agent_edit_ts_map",
                          lambda **k: {"u-new": time.time() - 60}):
            session_core.tick_death_probe(host, s, time.time())
        self.assertIsNone(s.death_info)
        self.assertEqual("u-new", s.cursor_uuid)
        self.assertEqual([], host.alerted, "转世复活不许推「挂了」")

    def test_gone_without_a_successor_still_dies(self):
        s = _sess()
        host = _Host()
        with patch.object(hub, "HUB", _FakeHub([s])), \
             patch.object(hub, "scan_agent_stub_convs", lambda **k: {}), \
             patch.object(hub, "agent_edit_ts_map", lambda **k: {}):
            session_core.tick_death_probe(host, s, time.time())
        self.assertIsNotNone(s.death_info)
        self.assertEqual("gone", s.death_info["code"])
        self.assertEqual(1, len(host.alerted))


class EmptyAndStaleBindTests(unittest.TestCase):
    """08-31 rxyy MCP·任务重开实案：接手会话明明在干活，侧栏点却是灰的。

    现场两个形态，都是「换绑只挂在 gone 分支」的死角：
    ① 接手/合并出来的会话 cursor_uuid 为空——判死探针一进门就退出，Cursor
      三路信号永远黑着；agent 埋头跑 18 分钟全量回归，zt 自报窗口一过，
      侧栏点退成灰「在线·没在输出」，用户以为它待机了；
    ② 标题治愈旁路把会话绑回转世前的旧壳——composerData 还在库里（治愈刚
      写过标题），判不出 gone，可真身早在新壳里写文件，信号全是死水。
    """

    def setUp(self):
        p = patch.object(hub, "log_event", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)

    def _tick(self, s, stubs, edits, exists=True):
        host = _Host()
        with patch.object(hub, "HUB", _FakeHub([s])), \
             patch.object(hub, "read_cursor_error", lambda uid: None), \
             patch.object(hub, "cursor_conversation_exists",
                          lambda uid: exists), \
             patch.object(hub, "scan_agent_stub_convs", lambda **k: stubs), \
             patch.object(hub, "agent_edit_ts_map", lambda **k: edits):
            session_core.tick_death_probe(host, s, time.time())
        return host

    def test_0831_an_unbound_takeover_session_gets_a_shell(self):
        # 形态①：uuid 为空的会话也要每拍参与转世扫描，而不是直接免检
        s = _sess(uid="")
        host = self._tick(s, {"u-new": _stub({"4ccbbe49"}, path="p.jsonl")},
                          {"u-new": time.time() - 60})
        self.assertEqual("u-new", s.cursor_uuid)
        self.assertTrue(s.uuid_verified)
        self.assertEqual("p.jsonl", s.transcript_path)
        self.assertEqual([], host.alerted)

    def test_0831_a_stale_shell_is_upgraded_to_the_live_one(self):
        # 形态②：绑着的旧壳还在库里（exists=True 判不出 gone），但新壳的
        # 改档流水新鲜得多 → 升级换绑
        now = time.time()
        s = _sess(uid="u-old")
        self._tick(s,
                   {"u-old": _stub({"4ccbbe49"}, age=3600),
                    "u-new": _stub({"4ccbbe49"}, path="n.jsonl", age=30)},
                   {"u-new": now - 30})
        self.assertEqual("u-new", s.cursor_uuid)

    def test_0831_a_live_shell_is_not_flapped_away(self):
        # 反面闸：现任信号新鲜时，比它只新几秒的挑战者不许换——没有 margin
        # 就是 10 秒一换的抖动
        now = time.time()
        s = _sess(uid="u-old")
        self._tick(s,
                   {"u-old": _stub({"4ccbbe49"}, age=20),
                    "u-new": _stub({"4ccbbe49"}, age=5)},
                   {"u-old": now - 20, "u-new": now - 5})
        self.assertEqual("u-old", s.cursor_uuid)


class EditStreamLivenessTests(unittest.TestCase):
    def _sess(self, **kw):
        s = hub.Session.__new__(hub.Session)
        s.id = "s1"
        s.name = "测试tab"
        s.connected = kw.pop("connected", False)
        s.pending = None
        s.recon_deadline = None
        s.queued = []
        s.lock = threading.Lock()
        s.agent_status = ""
        s.agent_status_ts = 0
        s.last_heartbeat = kw.pop("last_heartbeat", 0)
        s.cursor_uuid = kw.pop("cursor_uuid", "u-new")
        s.transcript_path = None
        s.conv_key = "22a030d6"
        s.last_zhi_ts = 0
        s.processing_since = None
        s.death_info = None
        for k, v in kw.items():
            setattr(s, k, v)
        return s

    def test_disconnected_but_editing_shows_alive(self):
        # 30d6 复活后的形态：通道没接（agent 从不调 MCP）、composer 读不出活动，
        # 但改档流水 2 分钟前有记录 → 「Cursor 里还活着」，不进待续组
        now = time.time()
        s = self._sess()
        api = hub.Api()
        with patch.object(hub, "read_cursor_activity", lambda uuid: {}), \
             patch.object(hub.Hub, "board_write_ages",
                          lambda h, x, t: (None, None)), \
             patch.object(hub, "agent_last_edit_ts",
                          lambda uid, home=None: now - 120):
            r = api._agent_liveness(s, now)
        self.assertEqual("ide", r["state"])
        self.assertTrue(any("AI 改档" in e for e in r["evidence"]))

    def test_fusion_lane_vetoes_death(self):
        now = time.time()
        s = self._sess()
        with patch.object(hub.Hub, "board_write_ages",
                          lambda h, x, t: (None, None)), \
             patch.object(hub, "agent_last_edit_ts",
                          lambda uid, home=None: now - 45):
            sig = session_core.fusion_recent_activity(hub.Hub.__new__(hub.Hub),
                                                      s, now)
        self.assertIn("AI 改档流水", sig or "")

    def test_no_edits_no_signal(self):
        now = time.time()
        s = self._sess()
        with patch.object(hub.Hub, "board_write_ages",
                          lambda h, x, t: (None, None)), \
             patch.object(hub, "agent_last_edit_ts", lambda uid, home=None: 0):
            sig = session_core.fusion_recent_activity(hub.Hub.__new__(hub.Hub),
                                                      s, now)
        self.assertIsNone(sig)


if __name__ == "__main__":
    unittest.main()
