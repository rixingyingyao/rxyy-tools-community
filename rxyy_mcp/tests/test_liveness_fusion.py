# -*- coding: utf-8 -*-
"""判活多信号融合——08-12 23:28 c00f0d0a「活跃写文件却被判收工」实案复现。

现场（方案书第六节）：23:28 c00f0d0a 正活跃写文件（agentboard 钩子 lastSeen 实时
刷新），它的 Cursor transcript 文件时间却停在 22:25——滞后 63 分钟。旧判活以
transcript/cursor_activity 为主信号：静默即降档（recent→idle「已收工」），漏读 /
detach 失联误判都由此而来。

根治（方案书第九节）：四路信号——看板写文件（agentboard lastSeen）/ Cursor 实时
generating（直读状态库）/ zhi·zt 到达 / 文件锁续期（locks[].renewed）——任一活跃
即判活；判「死/收工/卡住」必须全信号静默 + 超时阈值；transcript mtime 降级为辅助
（新鲜可佐证在跑，停滞不再单独支撑死亡/收工结论）。
"""
import json
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


def _sess(**kw):
    s = hub.Session.__new__(hub.Session)
    s.id = kw.pop("id", "s1")
    s.name = kw.pop("name", "测试tab")
    s.connected = kw.pop("connected", True)
    s.pending = None
    s.recon_deadline = None
    s.queued = []
    s.lock = threading.Lock()
    s.agent_status = kw.pop("agent_status", "")
    s.agent_status_ts = kw.pop("agent_status_ts", 0)
    s.last_heartbeat = kw.pop("last_heartbeat", time.time())
    s.cursor_uuid = kw.pop("cursor_uuid", "uuid-c00f0d0a")
    s.transcript_path = kw.pop("transcript_path", None)
    s.conv_key = kw.pop("conv_key", "c00f0d0a")
    s.last_zhi_ts = kw.pop("last_zhi_ts", 0)
    s.processing_since = kw.pop("processing_since", None)
    s.death_info = kw.pop("death_info", None)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _liveness(s, *, act=None, board=(None, None), edit_ts=0, now=None):
    api = hub.Api()
    with patch.object(hub, "read_cursor_activity", lambda uuid: act or {}), \
         patch.object(hub, "agent_last_edit_ts", return_value=edit_ts), \
         patch.object(hub.Hub, "board_write_ages",
                      lambda h, x, t: board):
        return api._agent_liveness(s, now or time.time())


class WritingAgentIsAliveDespiteStaleTranscript(unittest.TestCase):
    def test_c00f0d0a_writing_files_with_transcript_stalled_63min(self):
        # 实案主形态：transcript 停 63 分钟，看板 lastSeen 30 秒前——必须判活
        now = time.time()
        s = _sess()
        r = _liveness(s, act={"updated_ts": now - 63 * 60},
                      board=(30.0, None), now=now)
        self.assertEqual("working", r["state"],
                         "在写文件的 agent 不许因 transcript 停滞被判收工")
        self.assertIn("看板写文件", r["label"])
        self.assertEqual("active", r["tier"])

    def test_lock_renewal_alone_proves_alive(self):
        now = time.time()
        s = _sess()
        r = _liveness(s, act={"updated_ts": now - 40 * 60},
                      board=(None, 90.0), now=now)
        self.assertEqual("working", r["state"])
        self.assertIn("文件锁续期", r["label"])

    def test_death_bubble_yields_to_fresh_write_signal(self):
        # 钩子只在真实工具调用时刷新——报错气泡刷不了它。写文件信号新鲜时，
        # 读到的死亡气泡只可能是历史残影或 uuid 错配，不判死
        now = time.time()
        s = _sess(death_info={"reason": "账号欠费", "at_ts": now - 60,
                              "bubble_id": "b1", "code": 50})
        r = _liveness(s, act={}, board=(45.0, None), now=now)
        self.assertEqual("working", r["state"], "写文件的活人不许被判死")

    def test_stalled_needs_all_signals_quiet(self):
        # 已把话交给它 + transcript 静默——旧判定喊「疑似卡住」；
        # 但它正埋头写文件（不 zt、不输出对话），融合后不算卡
        now = time.time()
        s = _sess(processing_since=now - 600)
        r = _liveness(s, act={"updated_ts": now - 600},
                      board=(60.0, None), now=now)
        self.assertEqual("working", r["state"])

    def test_turn_done_not_judged_while_writing(self):
        # 流水尾部 turn_ended 但看板显示在写文件：流水多半错配到了别人的旧文件
        now = time.time()
        with tempfile.TemporaryDirectory() as td:
            tp = Path(td) / "t.jsonl"
            rows = [
                {"role": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "zhi",
                     "input": {"conversation_id": "c00f0d0a", "message": "报到"}}]}},
                {"type": "turn_ended", "status": "completed"},
            ]
            tp.write_text("\n".join(json.dumps(x) for x in rows) + "\n",
                          encoding="utf-8")
            # transcript mtime 做旧，别让「文件刚写过」抢在 turn_done 判定前面
            old = now - 3600
            import os as _os
            _os.utime(tp, (old, old))
            s = _sess(transcript_path=str(tp), last_zhi_ts=now - 11 * 60)
            r = _liveness(s, act={}, board=(50.0, None), now=now)
        self.assertNotEqual("turn_done", r["state"])
        self.assertEqual("working", r["state"])

    def test_disconnected_but_writing_counts_as_ide_alive(self):
        # 通道断了（MCP 被回收）但看板显示 5 分钟前还在写文件——
        # 旧判定 transcript 定位不到直接滑向 dead；融合后算「IDE 里还活着」
        now = time.time()
        s = _sess(connected=False)
        with patch.dict(hub.HUB.cfg, {"ide_active_secs": 900}):
            r = _liveness(s, act={}, board=(300.0, None), now=now)
        self.assertEqual("ide", r["state"])
        self.assertEqual("suspect", r["tier"])

    def test_full_silence_still_reads_as_idle(self):
        # 融合不放水：四路信号全部凉透（写文件/锁无记录、zt 无、transcript 20 分钟、
        # 交互 20 分钟）照旧判收工——该判静默时要敢判
        now = time.time()
        s = _sess(last_zhi_ts=now - 1200)
        r = _liveness(s, act={"updated_ts": now - 1200}, board=(None, None), now=now)
        self.assertEqual("idle", r["state"])
        self.assertEqual("quiet", r["tier"])

    def test_recent_uses_freshest_of_all_signals(self):
        now = time.time()
        s = _sess()
        r = _liveness(s, act={"updated_ts": now - 1200}, board=(400.0, None), now=now)
        self.assertEqual("recent", r["state"])
        self.assertIn("6分钟前", r["label"])


class StalePersistedGeneratingTests(unittest.TestCase):
    """持久化 composerData 的 generating 残留不能无限制造确定活跃。"""

    def test_fresh_generating_remains_certain_working(self):
        now = time.time()
        r = _liveness(
            _sess(),
            act={"generating": True, "updated_ts": now - 20},
            now=now,
        )

        self.assertEqual("working", r["state"])
        self.assertTrue(r["sure"])
        self.assertTrue(r["generating"])

    def test_stale_generating_without_independent_activity_is_unknown(self):
        now = time.time()
        r = _liveness(
            _sess(),
            act={"generating": True, "updated_ts": now - 3600},
            board=(None, None),
            now=now,
        )

        self.assertEqual("unknown", r["state"])
        self.assertFalse(r["sure"])
        self.assertFalse(r["generating"])
        self.assertIn("生成标记陈旧", " ".join(r["evidence"]))

    def test_recent_board_write_beats_stale_generating(self):
        now = time.time()
        r = _liveness(
            _sess(),
            act={"generating": True, "updated_ts": now - 3600},
            board=(25.0, None),
            now=now,
        )

        self.assertEqual("working", r["state"])
        self.assertTrue(r["sure"])
        self.assertFalse(r["generating"])
        self.assertIn("看板写文件", r["label"])

    def test_recent_background_edit_beats_stale_generating(self):
        now = time.time()
        r = _liveness(
            _sess(),
            act={"generating": True, "updated_ts": now - 3600},
            edit_ts=now - 20,
            now=now,
        )

        self.assertEqual("working", r["state"])
        self.assertTrue(r["sure"])
        self.assertFalse(r["generating"])
        self.assertIn("AI 改档", " ".join(r["evidence"]))

class NonCursorClientTests(unittest.TestCase):
    """不是 Cursor 的客户端（Codex / CLI / ACP）也得判得对。

    这类会话没有 Cursor 状态库可读（cursor_uuid 为空 → read_cursor_activity 空、
    没有 transcript 文件），也不挂 agentboard 钩子，所以 ide_age 和 write_age
    **永远**是 None。四路信号里它只剩 zhi·zt 到达这一路。

    而「疑似卡住」那条判定当初只写了 ide 和 write 两路，把 zt 漏了。于是用户在
    控制台回一句话（processing_since 落下）之后满 3 分钟，这个 tab 必定翻成
    「疑似卡住」——哪怕 agent 上一秒才 zt 报过进度。08-25 用户在 codex 里实测：
    那边 Codex 窗口明明正跑着「第 3/5 步 · 8 个文件已更改」，控制台这边灰着。
    Cursor 侧因为 ide_age 常年新鲜，这个洞一直没露出来。

    本模块开头写的规矩就是「判死/收工/卡住必须全信号静默」——漏一路即违例。
    """

    def _codex(self, **kw):
        kw.setdefault("cursor_uuid", None)
        kw.setdefault("transcript_path", None)
        return _sess(**kw)

    def test_a_fresh_zt_means_it_is_not_stuck(self):
        now = time.time()
        s = self._codex(processing_since=now - 600,
                        agent_status="developing", agent_status_ts=now - 20)
        r = _liveness(s, act={}, board=(None, None), now=now)
        self.assertEqual("working", r["state"],
                         "20 秒前刚报过进度的 agent 不是卡住")
        self.assertTrue(r["sure"], "有实打实的 zt 佐证，界面不该再加「疑似」")

    def test_zt_going_quiet_still_reads_as_stuck(self):
        """不放水：zt 也凉透了，「疑似卡住」照旧要敢报。"""
        now = time.time()
        s = self._codex(processing_since=now - 600,
                        agent_status="developing", agent_status_ts=now - 900)
        r = _liveness(s, act={}, board=(None, None), now=now)
        self.assertEqual("stalled", r["state"])

    def test_never_reported_anything_still_reads_as_stuck(self):
        now = time.time()
        s = self._codex(processing_since=now - 600)
        r = _liveness(s, act={}, board=(None, None), now=now)
        self.assertEqual("stalled", r["state"])


class ToolPhaseGeneratingZeroTests(unittest.TestCase):
    """08-15 现网：工具阶段 composer 的 generatingBubbleIds 全空、status=aborted，
    但 checkpoint 还在刷。这不是死——灯必须继续干活中；挂着 zhi 的仍是等你回复。
    """

    def test_aborted_hot_composer_is_working_not_dead(self):
        now = time.time()
        s = _sess(agent_status="testing", agent_status_ts=now - 8,
                  last_heartbeat=now, processing_since=now - 110)
        r = _liveness(s, act={
            "updated_ts": now - 15,
            "generating": 0,
            "status": "aborted",
        }, now=now)
        self.assertEqual("working", r["state"])
        self.assertEqual("active", r["tier"])
        self.assertIn("工具阶段", r["label"])
        self.assertNotIn("挂", r["label"])

    def test_waiting_zhi_beats_aborted_composer(self):
        now = time.time()
        s = _sess(last_heartbeat=now)
        s.pending = {"id": "q", "message": "问你一句", "created": now - 30}
        r = _liveness(s, act={
            "updated_ts": now - 80,
            "generating": 0,
            "status": "aborted",
        }, now=now)
        self.assertEqual("waiting", r["state"])

    def test_board_write_keeps_working_when_composer_aborted(self):
        now = time.time()
        s = _sess(last_heartbeat=now)
        r = _liveness(s, act={
            "updated_ts": now - 400,
            "generating": 0,
            "status": "aborted",
        }, board=(11.0, None), now=now)
        self.assertEqual("working", r["state"])
        self.assertIn("看板写文件", r["label"])


class DeathVerdictYieldsToFusion(unittest.TestCase):
    """判死提醒收口（第九节③）：读到死亡气泡但四路信号还热着 → 不落死因不推「挂了」。

    反面教材（08-03/08-12 谱系）：uuid 错绑到隔壁死对话时读到别人的报错气泡，
    活人 tab 被推「挂了：账号欠费」+ skull 响铃。融合后：真死的 agent 刷不动
    看板/锁/zhi/zt 信号，最多晚 3 分钟照样判死；活人永不被错杀。"""

    def _probe(self, s, *, board=(None, None), err=None):
        pushed = []
        with patch.object(hub, "read_cursor_error", lambda uid: err), \
             patch.object(hub.Hub, "board_write_ages", lambda h, x, t: board), \
             patch.object(hub.Hub, "alert_session_death",
                          lambda h, x, d: pushed.append(d)), \
             patch.object(hub, "WORKFLOW", None), \
             patch.object(hub, "log_event", lambda *a, **k: None):
            hub.HUB._tick_death_probe(s, time.time())
        return pushed

    def _dead_sess(self, **kw):
        s = _sess(**kw)
        s.death_probe_at = 0
        s.death_alerts = {}
        s.last_reply_probe = None
        return s

    def _fatal(self, at_ts):
        return {"code": 50, "reason": "账号欠费", "advice": "去付账单",
                "is_last": True, "bubble_id": "b1", "at_ts": at_ts}

    def test_death_push_yields_to_fresh_write_signal(self):
        now = time.time()
        s = self._dead_sess()
        pushed = self._probe(s, board=(40.0, None), err=self._fatal(now - 60))
        self.assertIsNone(s.death_info, "在写文件的活人不落死因")
        self.assertEqual([], pushed, "更不许推「挂了」")

    def test_death_push_fires_when_all_signals_quiet(self):
        # 融合不放水：全静默时照旧判死、照旧提醒（真欠费的要及时喊人）
        now = time.time()
        s = self._dead_sess()
        pushed = self._probe(s, board=(None, None), err=self._fatal(now - 60))
        self.assertIsNotNone(s.death_info)
        self.assertEqual(1, len(pushed))

    def test_recent_zhi_arrival_blocks_death_verdict(self):
        # 报错气泡之后 agent 还调过 zhi = 缓过来了（last_zhi_ts 补进豁免闸）
        now = time.time()
        s = self._dead_sess(last_zhi_ts=now - 30)
        pushed = self._probe(s, board=(None, None), err=self._fatal(now - 300))
        self.assertIsNone(s.death_info)
        self.assertEqual([], pushed)


class DetachWatchdogYieldsToFusion(unittest.TestCase):
    """失联清理收口（第九节③）：没认领过活、但四路信号还热着的会话，
    detach 超宽限同样只挂起不清 pending——人在写文件，只是没来得及续期。"""

    def _tick(self, s, board):
        from unittest.mock import MagicMock
        with patch.object(hub.HUB, "sessions", {s.id: s}), \
             patch.object(hub.HUB, "order", [s.id]), \
             patch.object(hub.HUB, "cfg", {"detach_grace_secs": 30,
                                           "ide_active_secs": 0,
                                           "max_messages": 20}), \
             patch.object(hub.Hub, "_tick_death_probe", MagicMock()), \
             patch.object(hub.Hub, "board_write_ages", lambda h, x, t: board), \
             patch.object(hub.Api, "_agent_liveness", lambda a, x, now: {}), \
             patch.object(hub, "log_event", lambda *a, **k: None), \
             patch.object(hub, "WORKFLOW", None):
            hub.HUB._state_tick()

    def _detached_shell(self):
        s = _sess(conv_key="c-shell", cursor_uuid=None)
        s.pending = {"id": "req1", "message": "报到", "options": [],
                     "created": time.time()}
        s.detached = True
        s.detached_since = time.time() - 3600
        s.archived = False
        s.pending_lost = False
        s.lost_pending_on_drop = False
        s.buffered_reply = None
        s.disconnected_at = None
        s.agent_named = False
        s.shell_born = True
        s.claimed_task_ts = 0.0
        s.msg_seq = 2
        s.machine_seq = 0
        s.real_seq = 2
        s.messages = []
        s.death_probe_at = time.time()
        s.death_alerts = {}
        s.takeover_dispatched = None
        s.file_path = None
        s.rev = 0
        s.draft_text = ""
        s.draft_images = []
        s.draft_files = []
        return s

    def test_writing_unclaimed_session_keeps_pending(self):
        s = self._detached_shell()
        self._tick(s, (50.0, None))
        self.assertIsNotNone(s.pending, "在写文件的会话不许被失联看门狗清 pending")
        self.assertTrue(s.detached)

    def test_fully_quiet_unclaimed_session_is_still_cleared(self):
        s = self._detached_shell()
        self._tick(s, (None, None))
        self.assertIsNone(s.pending, "全静默的未认领壳照旧切回待机")
        self.assertFalse(s.detached)


class BoardWriteAgesMatching(unittest.TestCase):
    """board_write_ages 的归属匹配：conv 键 / uuid 键 / 条目.uuid 三条路 + 毫秒换算。"""

    def _board(self, td, agents=None, locks=None):
        d = Path(td) / ".chijiu-tmp"
        d.mkdir(parents=True, exist_ok=True)
        (d / "agentboard.json").write_text(json.dumps({
            "version": 1, "agents": agents or {}, "locks": locks or {},
            "queue": {}, "notes": []}), encoding="utf-8")

    def _ages(self, td, s, now):
        hub._AGENTBOARD_SIG_CACHE.clear()
        return hub.HUB.board_write_ages(s, now)

    def test_matches_by_conversation_id_key(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as td:
            self._board(td, agents={"c00f0d0a": {"lastSeen": int((now - 30) * 1000)}})
            s = _sess(cwd=td, task_root=td, cursor_uuid=None)
            seen, renew = self._ages(td, s, now)
        self.assertAlmostEqual(30, seen, delta=3)
        self.assertIsNone(renew)

    def test_matches_by_transcript_uuid_field(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as td:
            self._board(
                td,
                agents={"f6f68dd6-uuid": {"lastSeen": int((now - 45) * 1000),
                                          "uuid": "uuid-c00f0d0a"}},
                locks={"src/a.py": {"owner": "f6f68dd6-uuid",
                                    "renewed": int((now - 12) * 1000)}})
            s = _sess(cwd=td, task_root=td, conv_key="whatever")
            seen, renew = self._ages(td, s, now)
        self.assertAlmostEqual(45, seen, delta=3)
        self.assertAlmostEqual(12, renew, delta=3)

    def test_unrelated_and_pid_fallback_keys_are_ignored(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as td:
            self._board(td, agents={
                "pid-4444": {"lastSeen": int(now * 1000)},
                "someone-else": {"lastSeen": int(now * 1000), "uuid": "other"}})
            s = _sess(cwd=td, task_root=td)
            seen, renew = self._ages(td, s, now)
        self.assertIsNone(seen)
        self.assertIsNone(renew)

    def test_missing_board_reads_as_no_signal(self):
        now = time.time()
        with tempfile.TemporaryDirectory() as td:
            s = _sess(cwd=td, task_root=td)
            hub._AGENTBOARD_SIG_CACHE.clear()
            self.assertEqual((None, None), hub.HUB.board_write_ages(s, now))


if __name__ == "__main__":
    unittest.main()
