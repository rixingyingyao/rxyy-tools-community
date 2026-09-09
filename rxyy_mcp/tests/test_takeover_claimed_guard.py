# -*- coding: utf-8 -*-
"""接手落地守卫：已开工（认领了活）的会话绝不被自动收起/归并——c00f0d0a 事故复现。

现场（2026-08-12，根治方案书第一节·路径3）：c00f0d0a 报到成待命壳后被派了自己的
活，已改名、报过 zt，但真实消息数还没过 REAL_WORK_SEQ；一次派接手（4f3cb97 落地
机制）把它当「待命空壳」判中——壳 ID 被登记为 1a49a6ec 的接手别名、tab 当场归并
消失，此后它的每一次 zhi/zt 都串进别人的 tab（消息串台、面板失踪）。

根治铁律（方案书第二节）：会话一旦「认领了活」（收过真实用户任务 / 报过 zt 进度 /
被 agent 改过名 / 干过真活），就永不被任何自动逻辑收起、归并、改身份、清状态。
判定收口在 Hub._claimed_task，_is_checkin_shellish 从此认它。
"""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub  # noqa: E402

WS = r"d:\桌面\working\cursor工作流"


def _s(sid, conv, name, **kw):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = sid, conv, name
    s.cwd = s.task_root = kw.get("root", WS)
    s.connected = kw.get("connected", True)
    s.pending = {"id": "q"} if kw.get("pending") else None
    s.queued = kw.get("queued") or []
    s.messages = []
    s.msg_seq = kw.get("msg_seq", 2)
    s.machine_seq = 0
    s.real_seq = kw.get("real_seq", kw.get("msg_seq", 2))
    s.shell_born = kw.get("shell_born", str(name).startswith("待命"))
    s.agent_named = kw.get("agent_named", False)
    s.agent_status_ts = kw.get("agent_status_ts", 0)
    s.claimed_task_ts = kw.get("claimed_task_ts", 0.0)
    s.handed_off_to = kw.get("handed_off_to", "")
    s.client = None
    s.lock = threading.Lock()
    s.file_path = None
    s.rev = 0
    return s


class ClaimedTaskJudgement(unittest.TestCase):
    """「认领了活」四条腿，缺一条都会重演 c00f0d0a：判空壳只看聊天句数。"""

    def test_virgin_checkin_shell_is_still_a_shell(self):
        x = _s("s1", "c1", "待命·cursor工作流", real_seq=1)
        self.assertFalse(hub.HUB._claimed_task(x))
        self.assertTrue(hub.HUB._is_checkin_shellish(x))

    def test_zt_report_claims_the_task(self):
        # c00f0d0a 修前红的核心：报过 zt 的壳仍被判成空壳
        x = _s("s1", "c1", "待命·cursor工作流", real_seq=1,
               agent_status_ts=time.time())
        self.assertTrue(hub.HUB._claimed_task(x))
        self.assertFalse(hub.HUB._is_checkin_shellish(x))

    def test_idle_zt_with_checkin_zhi_is_still_a_shell(self):
        # 08-14：待命还挂着报到 zhi 时报了 ready，不算开工。派接手必须仍能归并它。
        x = _s("s1", "c1", "待命·cursor工作流", real_seq=1,
               agent_status_ts=time.time())
        x.agent_status = "ready"
        x.pending = {"id": "q", "message": "📍 x · 对话 abcd 已就位",
                     "options": ["开始任务", "结束"]}
        self.assertTrue(hub.HUB._claimed_task(x), "zt 时间戳仍算认领（其它路径要用）")
        self.assertTrue(hub.HUB._is_checkin_shellish(x))
        self.assertTrue(hub.HUB._is_takeover_shell(x))

    def test_developing_zt_with_checkin_zhi_is_not_a_shell(self):
        # Cursor 里已经开干、控制台还挂着报到 zhi：不能当空壳收（c00f0d0a 变体）
        x = _s("s1", "c1", "待命·cursor工作流", real_seq=1,
               agent_status_ts=time.time())
        x.agent_status = "developing"
        x.pending = {"id": "q", "message": "已就位",
                     "options": ["开始任务", "结束"]}
        self.assertFalse(hub.HUB._is_checkin_shellish(x))
        self.assertFalse(hub.HUB._is_takeover_shell(x))

    def test_bare_pending_without_checkin_shape_is_not_takeover_shell(self):
        x = _s("s1", "c1", "待命·cursor工作流", real_seq=1)
        x.pending = {"id": "q"}
        self.assertTrue(hub.HUB._is_checkin_shellish(x))
        self.assertFalse(hub.HUB._is_takeover_shell(x))

    def test_agent_rename_claims_the_task(self):
        x = _s("s1", "c1", "rxyy MCP·根治筹备", shell_born=True, real_seq=1,
               agent_named=True)
        self.assertFalse(hub.HUB._is_checkin_shellish(x))

    def test_persisted_claim_mark_survives_restart_semantics(self):
        # agent_status_ts 重启即清零。真开工靠 agent_named（落盘）挡住误收。
        x = _s("s1", "c1", "待命·cursor工作流", real_seq=1,
               claimed_task_ts=time.time() - 3600, agent_named=True)
        self.assertFalse(hub.HUB._is_checkin_shellish(x))

    def test_claimed_without_pending_is_not_a_shell_anymore(self):
        """08-26 反转 08-14 的旧例外：claimed 单独出现也不再算壳。

        08-14 当时「点开始任务/贴接手词」会写 claimed_ts，只好把 claimed 单独
        出现仍判成壳。但拆信封两条路早已在上游闸掉（send_reply 的
        is_checkin_envelope、queue_message 的 TAKEOVER_PROMPT_HEAD），如今
        claimed_task_ts 只可能来自真实开工信号——73004179 事故（见下面的
        Incident0826 类）就是这行旧例外把收过真活的会话归并掉的。"""
        x = _s("s1", "c1", "Persistent Plus check-in", real_seq=1,
               claimed_task_ts=time.time(), shell_born=True)
        x.pending = None
        self.assertFalse(hub.HUB._is_checkin_shellish(x))
        self.assertFalse(hub.HUB._is_takeover_shell(x))

    def test_checkin_envelope_detects_start_and_takeover(self):
        pending = {"id": "q", "message": "已就位", "options": ["开始任务", "结束"]}
        self.assertTrue(hub.session_core.is_checkin_envelope(
            pending, ["开始任务"], ""))
        self.assertTrue(hub.session_core.is_checkin_envelope(
            pending, [], hub.TAKEOVER_PROMPT_HEAD + "……"))
        self.assertFalse(hub.session_core.is_checkin_envelope(
            pending, [], "帮我修这个 bug"))

    def test_machine_chatter_alone_does_not_claim(self):
        # 08-07 教训的反向保护：光挨广播/黑板的壳还得收得掉
        x = _s("s1", "c1", "待命·cursor工作流", real_seq=0, msg_seq=6)
        x.queued = [{"id": "q1", "who": "agent·某某", "text": "进度"}]
        self.assertFalse(hub.HUB._claimed_task(x))
        self.assertTrue(hub.HUB._is_checkin_shellish(x))

    def test_mark_claimed_is_idempotent_and_persists_a_timestamp(self):
        x = _s("s1", "c1", "待命·cursor工作流", real_seq=1)
        with patch.object(hub, "log_event", lambda *a, **k: None):
            hub.HUB._mark_claimed(x, "测试")
            first = x.claimed_task_ts
            hub.HUB._mark_claimed(x, "测试")
        self.assertGreater(first, 0)
        self.assertEqual(first, x.claimed_task_ts)


def _ledger_hygiene(case):
    """接手账本全隔离（A步起 _retire_conv_into 一笔写台账+平表并落盘）：
    不打桩会把「c2→cd」这类测试数据灌进真 HUB 的账本，污染同进程后跑的
    用例（_shell_takeover_target 的台账兜底）。用例自己的同名 patch 照常嵌套。"""
    for p in (patch.object(hub.HUB, "takeover_aliases", {}),
              patch.object(hub.HUB, "takeover_ledger", {}, create=True),
              patch.object(hub.HUB, "name_tombstones", {}),
              patch.object(hub.Hub, "_save_takeover_aliases",
                           lambda self: None)):
        p.start()
        case.addCleanup(p.stop)


class DispatchDoesNotMergeClaimedSession(unittest.TestCase):
    """事故主场景：派接手给「已开工的壳」，不许别名归并、tab 必须原地活着。"""

    def setUp(self):
        _ledger_hygiene(self)

    def _ctx(self, sessions, aliases):
        d = {x.id: x for x in sessions}
        return d, [
            patch.object(hub.HUB, "sessions", d),
            patch.object(hub.HUB, "order", list(d)),
            patch.object(hub.HUB, "takeover_aliases", aliases),
            patch.object(hub.HUB, "cfg", {"max_messages": 200}),
            patch.object(hub.Hub, "_save_takeover_aliases", lambda self: None),
            patch.object(hub.Hub, "log_end", lambda self, s, why: None),
            patch.object(hub.Hub, "_tombstone_shell", lambda self, x, succ: None),
            patch.object(hub.Hub, "save_state", lambda self: None),
            patch.object(hub, "log_event", lambda *a, **k: None),
            patch.object(hub.Api, "get_takeover_prompt",
                         lambda self, sid, digest_repeat=False: {"ok": True, "prompt": "接手吧",
                                            "conversation_id": "cd", "name": "挂了的"}),
            patch.object(hub.Api, "send_reply",
                         lambda self, sid, t, sel, im, cont, who="", files=None: {"ok": True}),
            patch.object(hub.Api, "queue_message",
                         lambda self, sid, t, im, who="", files=None: {"ok": True}),
        ]

    def test_c00f0d0a_working_shell_is_not_aliased_away(self):
        dead = _s("d", "cd", "挂了的", connected=False)
        # c00f0d0a 出事时的样子：出生是壳、真实消息少，但已报 zt（在干自己的活）
        worker = _s("b", "c00f0d0a", "待命·cursor工作流", real_seq=2, msg_seq=2,
                    agent_status_ts=time.time())
        aliases = {}
        d, ps = self._ctx([dead, worker], aliases)
        with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9], ps[10], ps[11]:
            r = hub.Api().share_takeover("d", "b", who="手机")
            self.assertTrue(r["ok"])
            self.assertEqual({}, aliases, "已开工的会话绝不能成为别名")
            self.assertIn("b", d, "它的 tab 必须原地活着，不许归并消失")
            self.assertEqual("cd", worker.handed_off_to,
                             "只按「正在干活的 agent」记后任关系")

    def test_virgin_shell_still_merges_as_designed(self):
        # 守卫不能把正常接手流程一起打死：真空壳照旧当场别名归并
        dead = _s("d", "cd", "挂了的", connected=False)
        shell = _s("b", "c2", "待命·乙", real_seq=1, msg_seq=1)
        aliases = {}
        d, ps = self._ctx([dead, shell], aliases)
        with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9], ps[10], ps[11]:
            r = hub.Api().share_takeover("d", "b", who="手机")
            self.assertTrue(r["ok"])
            self.assertEqual("cd", aliases.get("c2"))
            self.assertNotIn("b", d)

    def test_idle_standby_waiting_on_checkin_zhi_still_merges(self):
        # 08-14 主场景：活着的待命挂着报到 zhi，还报过 ready。派给它接手时
        # 必须当场归并，不能因为 claimed_task/pending 把壳留下。
        dead = _s("d", "cd", "rxyy tools·主题", connected=False)
        shell = _s("b", "c2", "待命·cursor工作流", real_seq=1, msg_seq=1,
                   agent_status_ts=time.time(), pending=True)
        shell.agent_status = "ready"
        shell.pending = {"id": "q", "message": "📍 cursor工作流 · 对话 c2 已就位",
                         "options": ["开始任务", "结束"]}
        aliases = {}
        d, ps = self._ctx([dead, shell], aliases)
        with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9], ps[10], ps[11]:
            r = hub.Api().share_takeover("d", "b", who="手机")
            self.assertTrue(r["ok"])
            self.assertEqual("cd", aliases.get("c2"))
            self.assertNotIn("b", d, "报到 zhi 还挂着的待命壳必须归并消失")
            self.assertIsNone(shell.pending)

    def test_shell_judgement_is_taken_before_prompt_delivery(self):
        """接手提示词本身是用户消息，送达即认领——判壳必须用派单前的状态。

        不预先取值的话：真队列路径（queue_message 会 _mark_claimed）跑完，
        壳永远判不成壳，接手落地机制整个失效。
        """
        dead = _s("d", "cd", "挂了的", connected=False)
        shell = _s("b", "c2", "待命·乙", real_seq=1, msg_seq=1)
        aliases = {}
        d = {x.id: x for x in [dead, shell]}

        def _queue_and_claim(api, sid, t, im, who="", files=None):
            hub.HUB._mark_claimed(d[sid], "接手提示词送达")
            return {"ok": True}

        with patch.object(hub.HUB, "sessions", d), \
             patch.object(hub.HUB, "order", list(d)), \
             patch.object(hub.HUB, "takeover_aliases", aliases), \
             patch.object(hub.HUB, "cfg", {"max_messages": 200}), \
             patch.object(hub.Hub, "_save_takeover_aliases", lambda self: None), \
             patch.object(hub.Hub, "log_end", lambda self, s, why: None), \
             patch.object(hub.Hub, "_tombstone_shell", lambda self, x, succ: None), \
             patch.object(hub.Hub, "save_state", lambda self: None), \
             patch.object(hub, "log_event", lambda *a, **k: None), \
             patch.object(hub.Api, "get_takeover_prompt",
                          lambda self, sid, digest_repeat=False: {"ok": True, "prompt": "接手吧",
                                             "conversation_id": "cd", "name": "挂了的"}), \
             patch.object(hub.Api, "queue_message", _queue_and_claim):
            r = hub.Api().share_takeover("d", "b", who="手机")
            self.assertTrue(r["ok"])
            self.assertEqual("cd", aliases.get("c2"),
                             "判壳晚于提示词送达的话，真空壳也会被当成开工会话")


class ReapRespectsClaimedSessions(unittest.TestCase):
    """事后收壳的两条路（报到收壳/zt 收壳）同样要认「认领了活」。"""

    class _Client:
        def __init__(self):
            self.closed_convs, self.sessions, self.cwd = {}, {}, WS

    def setUp(self):
        _ledger_hygiene(self)

    def test_handed_off_reap_leaves_claimed_session_alone(self):
        # 修前红：内联判定只看 real_seq，报过 zt 的照收（closed_convs 封 ID）
        x = _s("b", "c2", "待命·乙", real_seq=2, msg_seq=2,
               agent_status_ts=time.time(), handed_off_to="cd")
        with patch.object(hub.HUB, "sessions", {"b": x}), \
             patch.object(hub.HUB, "order", ["b"]), \
             patch.object(hub.Hub, "log_end", lambda self, s, why: None), \
             patch.object(hub, "log_event", lambda *a, **k: None):
            hub.HUB._reap_handed_off_shell(self._Client(), "cd")
            self.assertIn("b", hub.HUB.sessions)

    def test_handed_off_reap_still_collapses_virgin_shell(self):
        x = _s("b", "c2", "待命·乙", real_seq=1, msg_seq=1, handed_off_to="cd")
        with patch.object(hub.HUB, "sessions", {"b": x}), \
             patch.object(hub.HUB, "order", ["b"]), \
             patch.object(hub.Hub, "log_end", lambda self, s, why: None), \
             patch.object(hub.Hub, "_tombstone_shell", lambda self, x, succ: None), \
             patch.object(hub, "log_event", lambda *a, **k: None):
            hub.HUB._reap_handed_off_shell(self._Client(), "cd")
            self.assertNotIn("b", hub.HUB.sessions)

    def test_takeover_shell_judgement_excludes_claimed(self):
        # _reap_takeover_shell 的候选筛子是 _is_takeover_shell → 认领了活的进不了
        x = _s("b", "c2", "待命·乙", real_seq=2, msg_seq=2,
               agent_status_ts=time.time())
        self.assertFalse(hub.HUB._is_takeover_shell(x))


class Incident0826WorkingSessionMergedAsShell(unittest.TestCase):
    """2026-08-26 09:32:42 事故复现：73004179 被当「待命空壳」并进 b4eff2ee。

    现场：73004179 报到成待命壳（08:26），用户 08:31 给它派了真活（「收过真实
    用户回复」→ claimed_task_ts 落盘、tab 改名 playthread-go），此后它一直在干
    自己的活。09:32:38 同一个 Cursor 窗口里的 b4eff2ee 交接手报告，
    _reap_takeover_shell 按 uuid 对上窗口，_is_takeover_shell 里的旧例外
    「claimed 单独出现仍算壳」放行——一个正在干活的会话被归并，此后它的每句话
    都串进 b4eff2ee 的 tab（hub-run.log 09:39:38「收到提问 conv=b4eff2ee
    tab=录播播出端·切换器联动」）。c00f0d0a（08-12）原地重演。"""

    def setUp(self):
        _ledger_hygiene(self)

    def test_dispatched_real_task_alone_is_not_a_shell(self):
        # 73004179 出事时的样子：出生是壳、真实消息少、没报过 zt、没自报名，
        # 唯一的认领信号是用户派的真活（claimed_task_ts），报到 zhi 已答掉。
        x = _s("s1", "73004179", "切换到这个项目：playthread-go…",
               shell_born=True, real_seq=2, msg_seq=2,
               claimed_task_ts=time.time() - 3660)
        x.pending = None
        self.assertTrue(hub.HUB._claimed_task(x))
        self.assertFalse(hub.HUB._is_checkin_shellish(x),
                         "收过真活的会话绝不是报到壳")
        self.assertFalse(hub.HUB._is_takeover_shell(x),
                         "收过真活的会话绝不能被当待命空壳收掉")

    def test_reap_takeover_shell_leaves_claimed_session_alone(self):
        # 同窗口（uuid 相同）也不许收：窗口归属是接手设计的常态，
        # 「认领了活」才是生死线（08-12 铁律）。
        succ = _s("b", "b4eff2ee", "rxyy tools·全面体检", real_seq=45, msg_seq=45)
        succ.cursor_uuid = "U1"
        succ._reap_ts = 0
        worker = _s("a", "73004179", "切换到这个项目：playthread-go…",
                    shell_born=True, real_seq=2, msg_seq=2,
                    claimed_task_ts=time.time() - 3660)
        worker.pending = None
        worker.cursor_uuid = "U1"
        d = {x.id: x for x in [succ, worker]}
        aliases = {}
        with patch.object(hub.HUB, "sessions", d), \
             patch.object(hub.HUB, "order", list(d)), \
             patch.object(hub.HUB, "takeover_aliases", aliases), \
             patch.object(hub.Hub, "_relocate_takeover_uuid", lambda h, s: None), \
             patch.object(hub.Hub, "_save_takeover_aliases", lambda h: None), \
             patch.object(hub.Hub, "log_end", lambda h, s, why: None), \
             patch.object(hub.Hub, "_tombstone_shell", lambda h, x, succ: None), \
             patch.object(hub.Hub, "save_state", lambda h: None), \
             patch.object(hub.Hub, "takeover_landed", lambda h, s: None), \
             patch.object(hub, "log_event", lambda *a, **k: None):
            hub.HUB._reap_takeover_shell(succ)
        self.assertIn("a", d, "认领了活的会话绝不因接手落地被归并消失")
        self.assertEqual({}, aliases, "它的 ID 绝不能变成别人的接手别名")


class ClaimMarkSources(unittest.TestCase):
    """三个打标入口：zt 上报、agent 自报真名、用户真实输入。"""

    def test_zt_arrival_persists_the_claim(self):
        x = _s("b", "c2", "待命·乙", real_seq=1)
        client = ReapRespectsClaimedSessions._Client()
        client.sessions["c2"] = x
        client.last_heartbeat = 0
        x.agent_status = ""
        x.agent_activity = ""
        x.archived = False
        x.recon_deadline = None
        x.peer_ip = ""
        with patch.object(hub.HUB, "sessions", {"b": x}), \
             patch.object(hub.HUB, "takeover_aliases", {}), \
             patch.object(hub.HUB, "cfg", {"max_messages": 200}), \
             patch.object(hub.Hub, "_verify_identity_by_generating", lambda h, s: None), \
             patch.object(hub.Hub, "_reap_takeover_shell", lambda h, s: None), \
             patch.object(hub.Hub, "notify_status_only", lambda h: None), \
             patch.object(hub.Hub, "maybe_apply_task_name", lambda h, s, n: None), \
             patch.object(hub.Api, "_auto_label_on_dispatch", lambda a, s, t: None), \
             patch.object(hub.Api, "_nudge_rename_if_standby", lambda a, s: None), \
             patch.object(hub, "log_event", lambda *a, **k: None):
            hub.HUB._handle_client_msg(client, None, {
                "type": "agent_status", "conversation_id": "c2",
                "status": "developing", "activity": "接令干活"})
        self.assertGreater(x.claimed_task_ts, 0, "zt 到达必须落认领标记（可持久化）")

    def test_user_queue_marks_but_machine_mail_does_not(self):
        x = _s("b", "c2", "待命·乙", real_seq=1)
        with patch.object(hub.HUB, "sessions", {"b": x}), \
             patch.object(hub.HUB, "cfg", {"max_messages": 200, "history_dir": "."}), \
             patch.object(hub.Hub, "save_msg_images", lambda h, p: []), \
             patch.object(hub.Api, "_auto_label_on_dispatch",
                          lambda a, s, t, who=None: None), \
             patch.object(hub, "log_event", lambda *a, **k: None):
            hub.Api().queue_message("b", "队友的进度", [], who="agent·某某")
            self.assertEqual(0, x.claimed_task_ts, "机器代递不算认领")
            hub.Api().queue_message("b", "把这个项目做了", [], who=None)
            self.assertGreater(x.claimed_task_ts, 0, "用户排话=派活")


if __name__ == "__main__":
    unittest.main()
