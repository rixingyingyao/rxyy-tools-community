# -*- coding: utf-8 -*-
"""身份自校准：zhi/zt 到达那一刻「唯一在生成」的 Cursor 会话就是叫话的本尊。
这是「tab ↔ Cursor 会话」映射的决定性证据（07-31 用户点名要直读真实状态：
直读早就有，错的是身份映射——一天里 turn_ended/干活中/死判全都根源于此）。

08-03 二修补充（下半部分三个测试类）：并发接手场景实测两处一起犯错——
1) 排除集合把「接手方自己的报到空壳」的认领挡在候选外 → 真身进不了候选，
   「唯一在生成」的幸存者只剩隔壁等派活的 check-in 对话，铁证误伤；
2) composer 匹配是裸包含 → 队友通知里满天飞的裸 ID 也算命中。
修复契约：空壳认领不排他 + 跨对话认领要求 conversation_id 参数上下文 +
多 agent 同工作区时「唯一在生成」必须再见到参数佐证（单 agent 工作区维持原行为）。
"""
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub
import session_locator

WS1 = r"d:\Desktop\cursor工作流"


def _s(sid, conv, name, uuid=None, verified=False):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = sid, conv, name
    s.cwd = s.task_root = WS1
    s.connected = True
    s.pending = None
    s.queued = []
    s.cursor_uuid = uuid
    s.uuid_verified = verified
    s.transcript_path = ("C:\\t\\" + uuid + ".jsonl") if uuid else None
    s.lock = threading.Lock()
    return s


class IdentityCalibrationTests(unittest.TestCase):
    """本类只验「唯一在生成」那一档的判定，会话的工作区一律换成空临时目录。

    原先直接拿真实的 d:\\Desktop\\cursor工作流 当 cwd：把 generating_now_session
    patch 成 None 之后，下面几档兜底（cursor_db_session_for_conv /
    generating_session_for_conv）会真的去翻 ~/.cursor/projects/d-desktop-cursor
    底下的活流水。08-28 抓到现行——本机只要有 agent 正在生成，而
    transcript_mentions 又是裸子串匹配（"c1" 这种短 ID 在任何一串 hex 里都找得
    到），测试就凭空绑到一个真 composer 上挂掉。结论随「IDE 里此刻有没有人在
    干活」翻转，这比稳定失败更糟。工作区隔空，兜底自然全空。
    """

    def setUp(self):
        self._iso = tempfile.TemporaryDirectory()
        self.addCleanup(self._iso.cleanup)

    def _verify(self, target, sessions, gen_hit, mentions=None):
        for x in sessions:
            x.cwd = x.task_root = self._iso.name
        d = {x.id: x for x in sessions}
        calls = []

        def fake_gen(cwd, exclude=(), **kw):
            calls.append(set(exclude))
            return gen_hit

        patches = [patch.object(hub.HUB, "sessions", d),
                   patch.object(hub, "generating_now_session", fake_gen),
                   patch.object(hub, "log_event", lambda *a, **k: None)]
        if mentions is not None:
            patches.append(patch.object(hub, "composer_mentions_conv",
                                        lambda *a, **k: mentions))
        for p in patches:
            p.start()
        try:
            hub.HUB._verify_identity_by_generating(target)
        finally:
            for p in patches:
                p.stop()
        return calls

    def test_claims_the_sole_generating_conversation(self):
        s = _s("s1", "c1", "甲")
        self._verify(s, [s], ("uuid-aaa", r"C:\t\uuid-aaa.jsonl", {"generating": 1}))
        self.assertEqual("uuid-aaa", s.cursor_uuid)
        self.assertTrue(s.uuid_verified)
        self.assertIn("uuid-aaa", s.transcript_path)

    def test_steals_back_from_a_guessed_claim(self):
        # 别的 tab 靠「谁最新算谁」猜走了本尊：真主到达时要拿回来。
        # 08-03 起多 agent 同工作区还要参数佐证（对话里有本会话 ID 的参数形态），
        # 这里 mock 佐证为真，锁死「佐证在场时夺回照常发生」
        thief = _s("s2", "c2", "乙", uuid="uuid-aaa", verified=False)
        owner = _s("s1", "c1", "甲")
        self._verify(owner, [owner, thief],
                     ("uuid-aaa", r"C:\t\uuid-aaa.jsonl", {"generating": 1}),
                     mentions=True)
        self.assertEqual("uuid-aaa", owner.cursor_uuid)
        self.assertTrue(owner.uuid_verified)
        self.assertIsNone(thief.cursor_uuid)

    def test_verified_claims_are_excluded_from_the_scan(self):
        holder = _s("s2", "c2", "乙", uuid="uuid-aaa", verified=True)
        newcomer = _s("s1", "c1", "甲")
        calls = self._verify(newcomer, [newcomer, holder], None)
        self.assertIn({"uuid-aaa"}, calls)   # 验过的认领要排除在扫描外
        self.assertIsNone(newcomer.cursor_uuid)

    def test_rate_limited_to_once_a_minute(self):
        s = _s("s1", "c1", "甲")
        s._ident_ts = time.time()  # 刚校准过
        calls = self._verify(s, [s], ("uuid-aaa", "p", {"generating": 1}))
        self.assertEqual([], calls)
        self.assertIsNone(s.cursor_uuid)

    def test_ambiguous_or_none_generating_changes_nothing(self):
        s = _s("s1", "c1", "甲", uuid="uuid-old")
        self._verify(s, [s], None)   # 0 个或多个在生成 → None
        self.assertEqual("uuid-old", s.cursor_uuid)
        self.assertFalse(s.uuid_verified)


# ---------------------------------------------------------------------------
# 08-03 二修回归：并发接手错绑实案
# 接手方 B（27322a27）的真对话 U_ME 被它自己的报到空壳 A（94829d87，verified）
# 认领；同工作区另一个 check-in 对话 U_OTHER 挂着等派活（也在「生成中」），且被
# 队友通知污染（正文含裸 '27322a27' 但没有 conversation_id 参数形态）。
# 旧逻辑把 B 错绑到 U_OTHER（实测 090b9fa3），空壳 94829d87 永远收不掉。
# ---------------------------------------------------------------------------

U_ME = "c29d26f0-me"        # 接手方真对话（被自己的报到空壳认领着）
U_OTHER = "090b9fa3-other"  # 无辜的隔壁 check-in 对话（被裸 ID 污染）
U_PRED = "2972e376-pred"    # 前任死对话


def _sess(sid, conv, name, msg_seq, uuid=None, verified=False,
          pending=None, queued=None, cwd=WS1):
    s = _s(sid, conv, name, uuid=uuid, verified=verified)
    s.msg_seq = msg_seq
    s.pending = pending
    s.queued = queued or []
    s.client = None
    s.cwd = cwd
    return s


class RelocateTakeoverUuidTests(unittest.TestCase):
    def setUp(self):
        self.B = _sess("tab-B", "27322a27", "待命·cursor工作流2", 20,
                       uuid=U_PRED, verified=True)
        self.A = _sess("tab-A", "94829d87", "待命·cursor工作流5", 1,
                       uuid=U_ME, verified=True)
        self.C = _sess("tab-C", "10a8b6f1", "Persistent Plus check-in1", 1,
                       uuid=U_OTHER, verified=False, pending=object())
        self._patches = [
            patch.object(hub.HUB, "sessions",
                         {"tab-B": self.B, "tab-A": self.A, "tab-C": self.C}),
            patch.object(hub, "log_event", MagicMock()),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    def test_own_shell_claim_must_not_block_relocate(self):
        """空壳 A 认领着真身 U_ME：exclude 不得含 U_ME，重绑成功并收走 A 的认领。"""
        seen = {}

        def fake_db_match(cwd, cid, exclude=(), **kw):
            seen["exclude"] = set(exclude)
            if U_ME in exclude:
                return None  # 旧逻辑的死局：真身被排除，永远绑不回来
            return (U_ME, "path-me", {"updated_ts": 1.0})

        with patch.object(hub, "jsonl_tool_arg_session_for_conv", return_value=None), \
             patch.object(hub, "cursor_db_session_for_conv", side_effect=fake_db_match):
            hub.HUB._relocate_takeover_uuid(self.B)
        self.assertNotIn(U_ME, seen["exclude"])
        self.assertEqual(self.B.cursor_uuid, U_ME)
        self.assertTrue(self.B.uuid_verified)
        self.assertIsNone(self.A.cursor_uuid)      # 原认领者交出 uuid，防双认领
        self.assertFalse(self.A.uuid_verified)
        self.assertEqual(self.C.cursor_uuid, U_OTHER)  # 无辜对话没被动

    def test_verified_nonshell_claim_still_excluded(self):
        """真在干活的 tab（非空壳）的已验证认领仍是排他的，不许被抢。"""
        self.C.name = "工作中的tab"
        self.C.msg_seq = 9
        self.C.uuid_verified = True
        seen = {}

        def fake_db_match(cwd, cid, exclude=(), **kw):
            seen["exclude"] = set(exclude)
            return None

        with patch.object(hub, "jsonl_tool_arg_session_for_conv", return_value=None), \
             patch.object(hub, "cursor_db_session_for_conv", side_effect=fake_db_match):
            hub.HUB._relocate_takeover_uuid(self.B)
        self.assertIn(U_OTHER, seen["exclude"])
        self.assertEqual(self.B.cursor_uuid, U_PRED)  # 没命中就不动，宁可晚收壳

    def test_jsonl_tool_arg_wins_over_prompt_only_composer(self):
        """08-15：composer 里只有接手词正文（param 档能命中），jsonl 才有
        CallMcpTool。重定位必须跟 jsonl，不能把只贴了提示词的窗口抢走。"""
        prompt_only = "prompt-only-uuid"
        real = U_ME

        def fake_db(cwd, cid, exclude=(), **kw):
            self.assertEqual(kw.get("param_context"), "tool_arg")
            return (prompt_only, "path-prompt", {"updated_ts": 9.0})

        with patch.object(hub, "jsonl_tool_arg_session_for_conv",
                          return_value=(real, "path-me", {"updated_ts": 2.0})), \
             patch.object(hub, "cursor_db_session_for_conv", side_effect=fake_db):
            hub.HUB._relocate_takeover_uuid(self.B)
        self.assertEqual(self.B.cursor_uuid, real)
        self.assertIsNone(self.A.cursor_uuid)


class VerifyIdentityConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.B = _sess("tab-B", "27322a27", "待命·cursor工作流2", 20,
                       uuid=U_PRED, verified=True)
        self.A = _sess("tab-A", "94829d87", "待命·cursor工作流5", 1,
                       uuid=U_ME, verified=True)
        self._patches = [
            patch.object(hub.HUB, "sessions", {"tab-B": self.B, "tab-A": self.A}),
            patch.object(hub, "log_event", MagicMock()),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    def test_unique_generating_shell_dialog_needs_conv_evidence(self):
        """「唯一在生成」命中的是空壳认领的对话，但对话里没出现过 s 的 ID：
        不能凭唯一硬认（那可能是隔壁还没派活的 check-in），交给参数上下文匹配。"""
        calls = {"db": 0}

        def fake_db_match(cwd, cid, exclude=()):
            calls["db"] += 1
            return None

        with patch.object(hub, "generating_now_session",
                          return_value=(U_ME, "path-me", {"updated_ts": 1.0})), \
             patch.object(hub, "composer_mentions_conv", return_value=False), \
             patch.object(hub, "cursor_db_session_for_conv", side_effect=fake_db_match), \
             patch.object(hub, "generating_session_for_conv", return_value=None):
            hub.HUB._verify_identity_by_generating(self.B)
        self.assertEqual(self.B.cursor_uuid, U_PRED)  # 证据不足，不动
        self.assertEqual(calls["db"], 1)              # 确实走了参数上下文兜底

    def test_unique_generating_shell_dialog_with_evidence_binds(self):
        """同对话接手的正主：空壳对话里有 s 的 ID → 凭唯一在生成直接绑上并夺回认领。"""
        with patch.object(hub, "generating_now_session",
                          return_value=(U_ME, "path-me", {"updated_ts": 1.0})), \
             patch.object(hub, "composer_mentions_conv", return_value=True):
            hub.HUB._verify_identity_by_generating(self.B)
        self.assertEqual(self.B.cursor_uuid, U_ME)
        self.assertTrue(self.B.uuid_verified)
        self.assertIsNone(self.A.cursor_uuid)

    def test_shell_claim_not_in_generating_exclude(self):
        """generating_now_session 的排除集合不含空壳认领（真身要能进候选）。"""
        seen = {}

        def fake_generating(cwd, exclude=()):
            seen["exclude"] = set(exclude)
            return None

        with patch.object(hub, "generating_now_session", side_effect=fake_generating), \
             patch.object(hub, "cursor_db_session_for_conv", return_value=None), \
             patch.object(hub, "generating_session_for_conv", return_value=None):
            hub.HUB._verify_identity_by_generating(self.B)
        self.assertNotIn(U_ME, seen["exclude"])

    def test_multi_agent_cwd_unique_generating_needs_param_evidence(self):
        """多 agent 同工作区：哪怕「唯一在生成」命中的不是空壳认领的对话，也必须
        有 conversation_id 参数佐证才许绑（hub 重启重连风暴实测会连环易主）。"""
        free_uuid = "b5b2e545-free"
        seen = {}

        def fake_mentions(uid, cid, **kw):
            seen["args"] = (uid, cid, kw.get("param_context"))
            return False

        with patch.object(hub, "generating_now_session",
                          return_value=(free_uuid, "path-free", {"updated_ts": 1.0})), \
             patch.object(hub, "composer_mentions_conv", side_effect=fake_mentions), \
             patch.object(hub, "cursor_db_session_for_conv", return_value=None), \
             patch.object(hub, "generating_session_for_conv", return_value=None):
            hub.HUB._verify_identity_by_generating(self.B)
        self.assertEqual(self.B.cursor_uuid, U_PRED)  # 没佐证，不动
        self.assertEqual(seen["args"], (free_uuid, "27322a27", True))

    def test_single_agent_cwd_unique_generating_binds_without_evidence(self):
        """单 agent 工作区维持 07-31 原行为：唯一在生成即铁证，不额外要佐证。"""
        self.B.cwd = r"e:\solo-proj"  # 只有 B 一个 tab 在这个工作区
        free_uuid = "f0f0f0f0-solo"
        with patch.object(hub, "generating_now_session",
                          return_value=(free_uuid, "path-solo", {"updated_ts": 1.0})), \
             patch.object(hub, "composer_mentions_conv",
                          side_effect=AssertionError("单agent工作区不该查佐证")):
            hub.HUB._verify_identity_by_generating(self.B)
        self.assertEqual(self.B.cursor_uuid, free_uuid)
        self.assertTrue(self.B.uuid_verified)


class CheckinWaveBindingTests(unittest.TestCase):
    """08-12 08:37 实测回归：两个对话同时报到（报到潮），「唯一在生成」的快照
    恰好只捕到对方，把甲绑到了乙的对话上——之后两小时的接手提示词都生成错了
    对象。修复契约：报到壳（待命名+没干过真活）优先用「报到词参数形态」精确
    认领（报到词里必带 conversation_id=「xxx」且用户一贴就进 composerData），
    「唯一在生成」退居二线；参数认领没查到时仍回落旧路径。"""

    U_MINE = "52c6cd05-mine"
    U_OTHER = "e4e8e283-other"

    def _shell(self):
        s = _sess("t-shell", "b8b4d35f", "待命·cursor工作流", 1)
        return s

    def _verify(self, shell, param_hit, generating_hit):
        def fake_db(cwd, cid, exclude=()):
            return param_hit

        with patch.object(hub.HUB, "sessions", {"t-shell": shell}), \
             patch.object(hub, "log_event", MagicMock()), \
             patch.object(hub, "generating_now_session",
                          return_value=generating_hit), \
             patch.object(hub, "cursor_db_session_for_conv", side_effect=fake_db), \
             patch.object(hub, "generating_session_for_conv", return_value=None):
            hub.HUB._verify_identity_by_generating(shell)

    def test_param_context_wins_over_sole_generating(self):
        shell = self._shell()
        self._verify(shell,
                     param_hit=(self.U_MINE, "path-mine", {"updated_ts": 1.0}),
                     generating_hit=(self.U_OTHER, "path-other", {"generating": 1}))
        self.assertEqual(self.U_MINE, shell.cursor_uuid)   # 认参数形态，不认猜测
        self.assertTrue(shell.uuid_verified)

    def test_falls_back_to_generating_when_param_missing(self):
        # composerData 还没写进来（极早期）：回落「唯一在生成」，行为如旧
        shell = self._shell()
        self._verify(shell, param_hit=None,
                     generating_hit=(self.U_OTHER, "path-other", {"generating": 1}))
        self.assertEqual(self.U_OTHER, shell.cursor_uuid)

    def test_worked_tab_keeps_the_old_path(self):
        # 干过真活的 tab 不算报到壳：仍走「唯一在生成」优先的原路径
        vet = _sess("t-vet", "4fb76917", "待命·cursor工作流2", 20)
        def fake_db(cwd, cid, exclude=()):
            raise AssertionError("非报到壳不该先扫参数上下文")
        with patch.object(hub.HUB, "sessions", {"t-vet": vet}), \
             patch.object(hub, "log_event", MagicMock()), \
             patch.object(hub, "generating_now_session",
                          return_value=(self.U_MINE, "p", {"generating": 1})), \
             patch.object(hub, "composer_mentions_conv", return_value=True), \
             patch.object(hub, "cursor_db_session_for_conv", side_effect=fake_db), \
             patch.object(hub, "generating_session_for_conv", return_value=None):
            hub.HUB._verify_identity_by_generating(vet)
        self.assertEqual(self.U_MINE, vet.cursor_uuid)


class TakeoverLivenessGateTests(unittest.TestCase):
    """08-12 实测回归：叙述污染 + 接手交接 = 两个活 tab 互抢身份。

    排查串台的 agent（团队优化，绑 U_MINE，verified）对话里写了一句
    「OA对接 的 conversation_id是1d07989b」，正中参数形态正则；OA对接 下一次
    zt 校准走到「接手交接」兜底，把这个正在说话的活人的 uuid 抢走，真主调 zhi
    又抢回去——两个 tab 身份来回拉锯，用户在面板上看到 tab 说「已被接手」而
    agent 明明还在干自己的活。修复契约：verified 原主通道在线且「近
    HANDOVER_QUIET_SECS 内 zhi/zt 过（或正阻塞等回复）」时，接手交接不动手；
    原主真安静了（被派走/断线）时照常交接。
    """
    U_MINE = "52c6cd05-mine"
    U_OWN = "e4e8e283-own"

    def _make(self, victim_alive=True, victim_spoke_ago=300):
        victim = _s("tab-V", "b8b4d35f", "团队优化", uuid=self.U_MINE, verified=True)
        victim.connected = victim_alive
        victim.last_zhi_ts = time.time() - victim_spoke_ago
        caller = _s("tab-C", "1d07989b", "OA对接", uuid=self.U_OWN, verified=True)
        return victim, caller

    def _calibrate(self, caller, victim):
        def fake_db(cwd, cid, exclude=()):
            # 路2 带 exclude（victim 的 verified 认领被排除）→ 落空；
            # 路3 接手交接兜底不带 exclude → 命中被污染的对话
            if exclude:
                return None
            return (self.U_MINE, "path-mine", {"updated_ts": time.time()})

        with patch.object(hub.HUB, "sessions", {"tab-V": victim, "tab-C": caller}), \
             patch.object(hub, "log_event", MagicMock()), \
             patch.object(hub, "generating_now_session", return_value=None), \
             patch.object(hub, "cursor_db_session_for_conv", side_effect=fake_db), \
             patch.object(hub, "generating_session_for_conv", return_value=None), \
             patch.object(hub.HUB, "_handover_out", MagicMock()):
            hub.HUB._verify_identity_by_generating(caller)

    def test_living_verified_owner_is_never_robbed(self):
        victim, caller = self._make(victim_alive=True, victim_spoke_ago=300)
        self._calibrate(caller, victim)
        self.assertEqual(victim.cursor_uuid, self.U_MINE)  # 活人原主不许被抢
        self.assertTrue(victim.uuid_verified)
        self.assertEqual(caller.cursor_uuid, self.U_OWN)   # 叫话方也不换绑

    def test_pending_owner_counts_as_talking(self):
        # 阻塞在 zhi 等用户回复的 tab 哪怕很久没「说话」也是活的（保活在续期）
        victim, caller = self._make(victim_alive=True, victim_spoke_ago=99999)
        victim.pending = object()
        self._calibrate(caller, victim)
        self.assertEqual(victim.cursor_uuid, self.U_MINE)
        self.assertEqual(caller.cursor_uuid, self.U_OWN)

    def test_quiet_owner_hands_over(self):
        # 原主超过安静窗没说话（agent 被派走的真接手）：交接照常落地
        victim, caller = self._make(victim_alive=True, victim_spoke_ago=99999)
        self._calibrate(caller, victim)
        self.assertEqual(caller.cursor_uuid, self.U_MINE)
        self.assertTrue(caller.uuid_verified)
        self.assertIsNone(victim.cursor_uuid)

    def test_disconnected_owner_hands_over(self):
        # 原主断线且过了重连宽限：几分钟前说过话也拦不住正当交接
        victim, caller = self._make(victim_alive=False, victim_spoke_ago=60)
        victim.recon_deadline = 0
        self._calibrate(caller, victim)
        self.assertEqual(caller.cursor_uuid, self.U_MINE)
        self.assertIsNone(victim.cursor_uuid)


class ParamContextMatcherTests(unittest.TestCase):
    """composer_mentions_conv 的 param_context 档：工具参数/接手提示词命中，
    队友通知的裸 ID 污染不命中。用内存 sqlite 模拟 Cursor 的 cursorDiskKV。"""

    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")

    def tearDown(self):
        self.con.close()

    def _put(self, uuid, text):
        blob = json.dumps({"name": "t", "text": text,
                           "fullConversationHeadersOnly": []}, ensure_ascii=False)
        self.con.execute("INSERT OR REPLACE INTO cursorDiskKV VALUES (?,?)",
                         ("composerData:" + uuid, blob))

    def test_tool_param_json_matches(self):
        self._put("u1", '工具调用参数 \\"conversation_id\\": \\"27322a27\\" 落进 bubble')
        self.assertTrue(session_locator.composer_mentions_conv(
            "u1", "27322a27", con=self.con, param_context=True))

    def test_takeover_prompt_matches(self):
        self._put("u2", "**conversation_id 必须改用「27322a27」**（这是被接手会话的原ID）")
        self.assertTrue(session_locator.composer_mentions_conv(
            "u2", "27322a27", con=self.con, param_context=True))

    def test_teammate_notification_pollution_rejected(self):
        self._put("u3", "【队友动态】待命·cursor工作流2（27322a27）：接手27322a27，读前任执行流水确认进度")
        self.assertFalse(session_locator.composer_mentions_conv(
            "u3", "27322a27", con=self.con, param_context=True))
        # 裸包含档维持旧语义（在已确认的对话里找空壳报到 ID 用）
        self.assertTrue(session_locator.composer_mentions_conv(
            "u3", "27322a27", con=self.con))

    def test_narrative_chinese_mention_rejected(self):
        # 08-12 实测污染源：排查串台的 agent 对话里叙述「…的 conversation_id是xxx」，
        # 一个「是」字就让它被认成了对方的接手方（身份互抢事故的第一环）
        self._put("u4", "OA对接 的 conversation_id是27322a27，正在排查串台")
        self.assertFalse(session_locator.composer_mentions_conv(
            "u4", "27322a27", con=self.con, param_context=True))

    def test_checkin_prompt_form_matches(self):
        # 报到提示词形态（conversation_id=「xxx」）必须继续命中
        self._put("u5", "立即调用rxyy MCP的zhi报到：conversation_id=「27322a27」全程沿用")
        self.assertTrue(session_locator.composer_mentions_conv(
            "u5", "27322a27", con=self.con, param_context=True))

    def test_takeover_info_line_matches(self):
        # 接手提示词「原会话信息」列表行（- conversation_id：xxx）必须继续命中
        self._put("u6", "【原会话信息】\n- conversation_id：27322a27\n- 原项目目录：d:\\x")
        self.assertTrue(session_locator.composer_mentions_conv(
            "u6", "27322a27", con=self.con, param_context=True))


if __name__ == "__main__":
    unittest.main()
