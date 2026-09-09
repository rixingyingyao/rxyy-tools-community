# -*- coding: utf-8 -*-
"""派接手：自动挑接手方、批量派单、接手落地后自动收空壳"""
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub

WS1 = r"d:\Desktop\cursor工作流"
WS2 = r"c:\Users\Administrator\AICodebrain"


def _s(sid, conv, name, *, root=WS1, connected=True, pending=False, msgs=1, queued=0):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = sid, conv, name
    s.task_root = s.cwd = root
    s.connected = connected
    s.pending = {"id": "q"} if pending else None
    s.queued = [1] * queued
    s.msg_seq = msgs
    s.handed_off_to = ""
    s.client = None
    s.lock = threading.Lock()
    s.file_path = None
    s.messages = []
    s.rev = 0
    return s


def _hub_with(*sessions):
    d = {s.id: s for s in sessions}
    return (patch.object(hub.HUB, "sessions", d),
            patch.object(hub.HUB, "order", list(d)))


class PickTargetTests(unittest.TestCase):
    def test_prefers_the_one_waiting_for_a_reply(self):
        dead = _s("d", "cd", "挂了的", connected=False)
        busy = _s("a", "c1", "待命·甲", pending=False)
        ready = _s("b", "c2", "待命·乙", pending=True)
        with _hub_with(dead, busy, ready)[0], _hub_with(dead, busy, ready)[1]:
            self.assertEqual("b", hub.Api().pick_takeover_target("d").id)

    def test_prefers_same_project_when_both_are_waiting(self):
        dead = _s("d", "cd", "挂了的", root=WS1, connected=False)
        far = _s("a", "c1", "待命·远", root=WS2, pending=True)
        near = _s("b", "c2", "待命·近", root=WS1, pending=True)
        with _hub_with(dead, far, near)[0], _hub_with(dead, far, near)[1]:
            self.assertEqual("b", hub.Api().pick_takeover_target("d").id)

    def test_offline_agents_are_never_picked(self):
        dead = _s("d", "cd", "挂了的", connected=False)
        off = _s("a", "c1", "待命·甲", connected=False)
        with _hub_with(dead, off)[0], _hub_with(dead, off)[1]:
            self.assertIsNone(hub.Api().pick_takeover_target("d"))


def _ledger_hygiene(case):
    """接手账本全隔离（A步起 _retire_conv_into 一笔写台账+平表并落盘）：
    不打桩的话，本文件的派单/收壳会把「c2→cd」这类测试数据灌进真 HUB 的
    账本，污染同进程后跑的用例（_shell_takeover_target 的台账兜底就被
    它坑过），还会写真数据文件。用例自己再 patch 同名属性照常嵌套生效。"""
    for p in (patch.object(hub.HUB, "takeover_aliases", {}),
              patch.object(hub.HUB, "takeover_ledger", {}, create=True),
              patch.object(hub.HUB, "name_tombstones", {}),
              patch.object(hub.Hub, "_save_takeover_aliases",
                           lambda self: None)):
        p.start()
        case.addCleanup(p.stop)


class ShareTakeoverTests(unittest.TestCase):
    def setUp(self):
        _ledger_hygiene(self)

    def _patched(self, *sessions, aliases=None):
        p1, p2 = _hub_with(*sessions)
        return (p1, p2,
                patch.object(hub.Api, "get_takeover_prompt",
                             lambda self, sid, digest_repeat=False: {"ok": True, "prompt": "接手吧",
                                                "conversation_id": "cd", "name": "挂了的"}),
                patch.object(hub.Api, "send_reply",
                             lambda self, sid, t, sel, im, cont, who="", files=None: {"ok": True}),
                patch.object(hub.Api, "queue_message",
                             lambda self, sid, t, im, who="", files=None: {"ok": True}),
                patch.object(hub.HUB, "takeover_aliases",
                             aliases if aliases is not None else {}),
                patch.object(hub.HUB, "cfg", {"max_messages": 200}),
                patch.object(hub.Hub, "_save_takeover_aliases", lambda self: None),
                patch.object(hub.Hub, "log_end", lambda self, s, why: None),
                patch.object(hub.Hub, "_tombstone_shell", lambda self, x, succ: None),
                patch.object(hub.Hub, "save_state", lambda self: None),
                patch.object(hub, "log_event", lambda *a, **k: None))

    def test_auto_target_picks_and_marks_the_handoff(self):
        dead = _s("d", "cd", "挂了的", connected=False)
        ready = _s("b", "c2", "待命·乙", pending=True)
        aliases = {}
        ps = self._patched(dead, ready, aliases=aliases)
        with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9], ps[10], ps[11]:
            r = hub.Api().share_takeover("d", "auto", who="手机")
            self.assertTrue(r["ok"])
            self.assertEqual("待命·乙", r["target"])
            self.assertFalse(r["queued"])          # 它正等着，当场就开工
            # 08-12 起：派给待命壳 = 壳 ID 当场成为被接手会话的别名（机制落地，
            # 不再靠 handed_off_to 等它自己改 ID）
            self.assertEqual("cd", aliases.get("c2"))
            self.assertEqual("待命·乙", dead.takeover_dispatched["to_name"])
            self.assertFalse(dead.takeover_dispatched["warned"])
            self.assertIn("无需改 ID", dead.messages[-1]["html"])

    def test_no_online_agent_says_so_instead_of_crashing(self):
        dead = _s("d", "cd", "挂了的", connected=False)
        ps = self._patched(dead)
        with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9], ps[10], ps[11]:
            r = hub.Api().share_takeover("d", "auto")
        self.assertFalse(r["ok"])
        self.assertIn("没有在线", r["error"])

    def test_batch_reports_each_pair(self):
        d1 = _s("d1", "cd", "挂了的一", connected=False)
        d2 = _s("d2", "cd2", "挂了的二", connected=False)
        ready = _s("b", "c2", "待命·乙", pending=True)
        ps = self._patched(d1, d2, ready)
        with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9], ps[10], ps[11]:
            r = hub.Api().share_takeover_batch(
                [{"sid": "d1", "target": "b"}, {"sid": "d2", "target": "nope"}])
        self.assertTrue(r["ok"])
        self.assertEqual(1, len(r["done"]))
        self.assertEqual(1, len(r["failed"]))
        self.assertIn("接手方会话不存在", r["failed"][0])


class TakeoverAliasTests(unittest.TestCase):
    """08-12 落地机制回归：接手落地不能寄托在「接手方肯改 ID」上（常驻协议要求
    它全程复用首个 ID，两条规矩打架它听先来的——实测 15 分钟超时提醒都响了它
    还在用自己的 ID 干活）。派给待命壳=壳 ID 当场登记为被接手会话的别名并归并
    壳（机制兜底）；派给正在干活的 agent=只记后任、不抢身份。"""

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

    def test_dispatch_to_shell_aliases_and_merges_it(self):
        dead = _s("d", "cd", "挂了的", connected=False)
        shell = _s("b", "c2", "待命·乙")
        shell.queued = [{"text": "接手吧", "who": "手机"}]  # 已排进壳队列的提示词
        aliases = {}
        d, ps = self._ctx([dead, shell], aliases)
        with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9], ps[10], ps[11]:
            r = hub.Api().share_takeover("d", "b", who="手机")
            self.assertTrue(r["ok"])
            self.assertEqual("cd", aliases.get("c2"))   # 壳 ID 成为别名
            self.assertNotIn("b", d)                    # 壳从列表消失
            self.assertEqual(1, len(dead.queued))       # 壳队列（含提示词）随迁
            self.assertIn("c2", dead.id_history)        # 老 ID 转告寻人可达
            self.assertEqual("c2", dead.takeover_dispatched["to_conv"])
            self.assertIn("无需改 ID", dead.messages[-1]["html"])

    def test_dispatch_to_busy_agent_keeps_its_identity(self):
        dead = _s("d", "cd", "挂了的", connected=False)
        busy = _s("b", "c2", "rxyy tools·干活中", msgs=25, pending=True)
        aliases = {}
        d, ps = self._ctx([dead, busy], aliases)
        with ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8], ps[9], ps[10], ps[11]:
            r = hub.Api().share_takeover("d", "b", who="手机")
            self.assertTrue(r["ok"])
            self.assertEqual({}, aliases)               # 不抢正在干活者的身份
            self.assertIn("b", d)                       # 它的 tab 原地不动
            self.assertEqual("cd", busy.handed_off_to)  # 只记后任关系
            self.assertEqual("c2", dead.takeover_dispatched["to_conv"])


class AliasRoutingTests(unittest.TestCase):
    """别名生效在消息入口统一换名：接手方带着壳 ID 来的 zt，直接落到被接手的
    原会话上——zhi/ji/心跳同一入口，全部受益。"""

    def test_shell_id_routes_to_original_session(self):
        orig = _s("d", "cd", "挂了的", connected=False)
        orig.agent_status = ""
        orig.agent_activity = ""
        orig.agent_status_ts = 0
        orig.archived = False
        orig.recon_deadline = None
        orig.messages = []
        orig.peer_ip = ""
        client = ReapShellTests._Client()
        client.sessions["cd"] = orig
        with patch.object(hub.HUB, "sessions", {"d": orig}), \
             patch.object(hub.HUB, "takeover_aliases", {"c2": "cd"}), \
             patch.object(hub.HUB, "cfg", {"max_messages": 200}), \
             patch.object(hub.Hub, "_verify_identity_by_generating",
                          lambda h, s: None), \
             patch.object(hub.Hub, "_reap_takeover_shell", lambda h, s: None), \
             patch.object(hub.Hub, "notify_status_only", lambda h: None), \
             patch.object(hub.Hub, "maybe_apply_task_name", lambda h, s, n: None), \
             patch.object(hub.Api, "_auto_label_on_dispatch", lambda a, s, t: None), \
             patch.object(hub.Api, "_nudge_rename_if_standby", lambda a, s: None), \
             patch.object(hub, "log_event", lambda *a, **k: None):
            hub.HUB._handle_client_msg(client, None, {
                "type": "agent_status", "conversation_id": "c2",
                "status": "developing", "activity": "接手后继续干"})
        self.assertEqual("developing", orig.agent_status)


class TakeoverLandedTests(unittest.TestCase):
    """接手方一开口，「⏳接手在途」就得摘掉。

    08-25 现场取证（`.sessions.json` 写盘 3 秒后读出来的）：

        name=rxyy tools·全面体检 conv=b4eff2ee
        takeover_dispatched to=待命·cursor工作流·f7d6 warned=True age=552min

    而同一时刻 takeover-aliases.json 里躺着 `5cbbf7d6 → b4eff2ee`、
    `fc4a1e07 → b4eff2ee`——接手 9 小时前就落地了，接手方（正是写这段话的我）
    一直在这个 tab 里干活。牌子却一直挂着，15 分钟那条「已等 N 分钟没落地，可
    重新派给别人」的告警＋手机推送照样发了。用户照它说的再派一次，才是真的把
    接手搞乱——这就是「接手老是出问题」的来源。

    根因：清牌子只写在 create_session 的复活分支里。而派给待命壳（alias_shell_into，
    如今的主路）之后，接手方带壳 ID 说的每一句都被别名换成本会话，
    resolve_session / _resolve_for_signal 在 client.sessions 里直接命中就返回了，
    那条复活分支根本不经过。于是「落地」这件事永远没人记账。
    """

    def setUp(self):
        _ledger_hygiene(self)

    def _orig(self):
        s = _s("d", "cd", "被接手的活", connected=False)
        s.agent_status = ""
        s.agent_activity = ""
        s.agent_status_ts = 0
        s.last_heartbeat = 0
        s.archived = False
        s.recon_deadline = None
        s.peer_ip = ""
        s.takeover_dispatched = {"to_name": "待命·乙", "to_conv": "c2",
                                 "ts": time.time() - 3600, "warned": False}
        return s

    def _ctx(self):
        return [
            patch.object(hub.HUB, "cfg", {"max_messages": 200}),
            patch.object(hub.Hub, "_verify_identity_by_generating", lambda h, s: None),
            patch.object(hub.Hub, "_reap_takeover_shell", lambda h, s: None),
            patch.object(hub.Hub, "_mark_claimed", lambda h, s, why: None),
            patch.object(hub.Hub, "notify_status_only", lambda h: None),
            patch.object(hub.Hub, "maybe_apply_task_name", lambda h, s, n: None),
            patch.object(hub.Api, "_auto_label_on_dispatch", lambda a, s, t: None),
            patch.object(hub.Api, "_nudge_rename_if_standby", lambda a, s: None),
            patch.object(hub, "log_event", lambda *a, **k: None),
        ]

    def test_a_zt_under_the_shell_alias_takes_the_badge_down(self):
        orig = self._orig()
        client = ReapShellTests._Client()
        client.sessions["cd"] = orig
        ps = self._ctx()
        with patch.object(hub.HUB, "sessions", {"d": orig}), \
             patch.object(hub.HUB, "takeover_aliases", {"c2": "cd"}), \
             ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8]:
            hub.HUB._handle_client_msg(client, None, {
                "type": "agent_status", "conversation_id": "c2",
                "status": "接令", "activity": "接手中"})
        self.assertEqual("接令", orig.agent_status, "zt 本身要照常生效")
        self.assertIsNone(orig.takeover_dispatched,
                          "接手方已经开口了，牌子还挂着 = 15 分钟后照发假告警")

    def test_the_original_owner_coming_back_also_takes_it_down(self):
        """原主自己回来了同样算落地——那时候「派出去的还没人接」已经没意义。"""
        orig = self._orig()
        client = ReapShellTests._Client()
        client.sessions["cd"] = orig
        ps = self._ctx()
        with patch.object(hub.HUB, "sessions", {"d": orig}), \
             patch.object(hub.HUB, "takeover_aliases", {}), \
             ps[0], ps[1], ps[2], ps[3], ps[4], ps[5], ps[6], ps[7], ps[8]:
            hub.HUB._handle_client_msg(client, None, {
                "type": "agent_status", "conversation_id": "cd",
                "status": "开工", "activity": "我回来了"})
        self.assertIsNone(orig.takeover_dispatched)

    def test_a_heartbeat_is_not_a_landing(self):
        """心跳是 MCP 进程级的自动信号，它服务过的死对话全都在名单里躺着。

        认它当落地，等于「派出去的接手永远不会超时」——那条提醒就废了。
        真落地必须是 agent 自己开口（zhi / zt / ji）。
        """
        orig = self._orig()
        client = ReapShellTests._Client()
        client.sessions["cd"] = orig
        with patch.object(hub.HUB, "sessions", {"d": orig}), \
             patch.object(hub.HUB, "takeover_aliases", {}), \
             patch.object(hub.HUB, "cfg", {"max_messages": 200}), \
             patch.object(hub, "log_event", lambda *a, **k: None):
            hub.HUB._handle_client_msg(client, None, {
                "type": "mcp_heartbeat", "conversations": ["cd"]})
        self.assertIsNotNone(orig.takeover_dispatched,
                             "心跳不能顶替落地，否则超时提醒永远不会响")

    def test_reaping_the_shell_takes_it_down_too(self):
        """收壳这条路本身就叫「接手落地」（日志里写的就是这四个字）。"""
        orig = self._orig()
        shell = _s("b", "c2", "待命·乙", msgs=2)
        shell.handed_off_to = "cd"
        with _hub_with(shell, orig)[0], _hub_with(shell, orig)[1], \
             patch.object(hub.Hub, "log_end", lambda self, s, why: None):
            hub.HUB._reap_handed_off_shell(ReapShellTests._Client(), "cd")
        self.assertNotIn("b", hub.HUB.sessions)
        self.assertIsNone(orig.takeover_dispatched)


class TakeoverBadgeExpiryTests(unittest.TestCase):
    """告警早发过、又挂了几个小时的「⏳接手在途」，牌子自己得下架。

    上面那条修复（接手方一开口就摘牌）只对**将来**的接手管用。08-25 22:03 重启
    之后从快照里恢复出来的旧牌子，接手方是在修复上线之前开的口，没人记过账，
    于是继续挂着：

        rxyy tools·全面体检   等了   650 分钟
        智慧云广播·仓库对齐   等了 19079 分钟（13 天）
        快编·AI配音不显      等了   297 分钟
        心理后端·接口补齐     等了   649 分钟

    四块牌子的接手方全是待命壳，早就不在会话表里了（其中三个在
    takeover-aliases.json 里白纸黑字并进了本会话）。用户看到的就是面板上一排
    「派出去几个小时了还没人接」，照着它重新派人，才是真把接手搞乱。

    牌子的两件正事在 15 分钟那一下就全做完了：面板留言 + 手机推送。此后它只剩
    「派给了谁」这一条信息，而这条已经写在 tab 的历史里。所以：告警发出去之后
    再挂满一小时，牌子到期下架——不再发第二次提醒，也不改任何别的状态。
    """

    EXPIRE = 3600

    def _sess(self, *, age_min, warned):
        s = _s("d", "cd", "被接手的活", connected=False)
        s.archived = False
        s.recon_deadline = None
        s.client = None
        s.transcript_path = None
        s.ide_active_cache = False
        s.live_cache = {}
        s.processing_since = None
        s.detached = False
        s.detached_since = None
        s.pending_lost = False
        s.lost_pending_on_drop = False
        s.buffered_reply = None
        s.disconnected_at = None
        s.claimed_task_ts = time.time() - 86400
        s.death_probe_at = time.time()
        s.death_alerts = {}
        s.end_reason = ""
        s.takeover_dispatched = {"to_name": "待命·乙", "to_conv": "c2",
                                 "ts": time.time() - age_min * 60, "warned": warned}
        return s

    def _tick(self, s):
        from unittest.mock import MagicMock
        pushed, msgs = [], []
        with patch.object(hub.HUB, "sessions", {s.id: s}), \
             patch.object(hub.HUB, "order", [s.id]), \
             patch.object(hub.HUB, "cfg", {"max_messages": 200, "ide_active_secs": 0,
                                           "share_enabled": False}), \
             patch.object(hub.Hub, "_tick_death_probe", MagicMock()), \
             patch.object(hub.Hub, "_tick_model_probe", MagicMock()), \
             patch.object(hub.Hub, "_tick_yield_burst", MagicMock()), \
             patch.object(hub.Hub, "_tick_parked_relay_reminders", MagicMock()), \
             patch.object(hub.Api, "_agent_liveness", lambda a, x, now: {}), \
             patch.object(hub.session_core, "detach_watchdog_tick",
                          lambda *a, **k: None), \
             patch.object(hub.Hub, "add_message",
                          lambda h, x, m: msgs.append(m)), \
             patch.object(hub.Hub, "notify", MagicMock()), \
             patch.object(hub.Hub, "push_phone",
                          lambda h, *a, **k: pushed.append(a)), \
             patch.object(hub, "log_event", lambda *a, **k: None), \
             patch.object(hub, "WORKFLOW", None):
            hub.HUB._state_tick()
        return pushed, msgs

    def test_a_badge_warned_about_hours_ago_comes_down(self):
        s = self._sess(age_min=650, warned=True)
        rev = s.rev
        pushed, msgs = self._tick(s)
        self.assertIsNone(s.takeover_dispatched,
                          "告警发过又挂了几小时，面板不该还说「接手在途」")
        self.assertGreater(s.rev, rev, "牌子当拍消失，不能等下一次全量刷新")
        self.assertEqual([], pushed, "下架是收尾，不许再推一次手机")
        self.assertEqual([], msgs, "也不许再往 tab 里写一条系统消息")

    def test_a_badge_warned_about_minutes_ago_stays_up(self):
        """刚报过警的还得留着：人正要照着它换个人派。"""
        s = self._sess(age_min=20, warned=True)
        self._tick(s)
        self.assertIsNotNone(s.takeover_dispatched)

    def test_a_fresh_dispatch_is_neither_warned_nor_expired(self):
        s = self._sess(age_min=5, warned=False)
        pushed, msgs = self._tick(s)
        self.assertIsNotNone(s.takeover_dispatched)
        self.assertEqual([], pushed)
        self.assertEqual([], msgs)

    def test_the_fifteen_minute_warning_still_fires(self):
        """下架不能把告警一起废掉——那才是这块牌子存在的理由。"""
        s = self._sess(age_min=16, warned=False)
        pushed, msgs = self._tick(s)
        self.assertEqual(1, len(pushed), "15 分钟那条提醒照发")
        self.assertEqual(1, len(msgs))
        self.assertTrue(s.takeover_dispatched["warned"])


class TakeoverPromptFirstActTests(unittest.TestCase):
    """提示词把 ID 切换写成「第一个动作」而不是第 5 条规则（手工粘贴场景仍靠
    文案；面板派单已有别名机制兜底）。"""

    def test_prompt_puts_id_switch_before_everything(self):
        with patch.object(hub.Api, "library_digest", return_value=""):
            p = hub.Api()._build_takeover_prompt(
                "cd12ef34", "d:\\x", "任务", "f.md", "摘要", None, "")
        self.assertIn("第一个动作", p)
        self.assertLess(p.index("第一个动作"), p.index("原会话信息"))
        self.assertIn("conversation_id=「cd12ef34」", p)

    def test_stay_put_never_asks_to_switch_to_the_old_id(self):
        with patch.object(hub.Api, "library_digest", return_value=""):
            p = hub.Api()._build_takeover_prompt(
                "cd12ef34", "d:\\x", "任务", "f.md", "摘要", None, "",
                stay_put=True)
        self.assertIn("第一个动作", p)
        self.assertIn("当前会话已经在用的", p)
        self.assertNotIn("必须改用", p)
        self.assertNotIn("conversation_id=「cd12ef34」", p)
        self.assertIn("原会话 ID（只读记录，不要改用）：cd12ef34", p)
        self.assertIsNone(hub.takeover_prompt_target(
            hub.TAKEOVER_PROMPT_HEAD + "\n" + p))

    def test_no_cid_means_no_first_act_block(self):
        with patch.object(hub.Api, "library_digest", return_value=""):
            p = hub.Api()._build_takeover_prompt(
                "", "d:\\x", "任务", "f.md", "摘要", None, "")
        self.assertNotIn("第一个动作", p)

    def test_embeds_library_digest_after_first_act(self):
        digest = ("【用户级规则 · 必读必守 · 本对话只发这一次】\n"
                  "先 Read 下面这份并完整遵守：\n· C:\\v19.md\n")
        with patch.object(hub.Api, "library_digest", return_value=digest):
            p = hub.Api()._build_takeover_prompt(
                "cd12ef34", "d:\\x", "任务", "f.md", "摘要", None, "")
        self.assertIn("【资料索引 · 接手后必读】", p)
        self.assertIn("C:\\v19.md", p)
        self.assertLess(p.index("第一个动作"), p.index("【资料索引 · 接手后必读】"))
        self.assertLess(p.index("【资料索引 · 接手后必读】"), p.index("原会话信息"))
        self.assertIn("先 zt，再按资料全文遵守", p)

    def test_digest_project_name_braces_do_not_keyerror(self):
        """复制接手提示词 KeyError('项目名')：资料索引原文含 10-servers 占位符。

        修前：整段 .format() 把 `{项目名}` 当成字段，当场 KeyError。
        修后：花括号原样进提示词，摘要/公告板里的同类写法也不炸。
        """
        digest = "部署根：`/data/www/{项目名}/{子目录}/`\n"
        with patch.object(hub.Api, "library_digest", return_value=digest):
            p = hub.Api()._build_takeover_prompt(
                "cd12ef34", "d:\\x", "心理评测", "f.md",
                "摘要含{foo}", None, "公告{项目名}")
        self.assertIn("/data/www/{项目名}/{子目录}/", p)
        self.assertIn("摘要含{foo}", p)
        self.assertIn("公告{项目名}", p)
        self.assertIn("conversation_id=「cd12ef34」", p)
        self.assertIn("task_name 用「心理评测」", p)

    def test_a_second_ticket_to_the_same_agent_drops_the_repeated_rulebook(self):
        """事故 0901：六张接手单叠给同一个 agent，共 5097 行，其中约 4700 行是
        用户级规则全文 / 项目规则全文 / 开干先看板 / 技能目录**逐字重复六遍**。
        接手方光读提示词就烧掉一大截上下文，真正的任务（一句话）埋在第 839 行。
        第二张起这几块换成一行指路，只留「侧栏改名」——它带本单标题，每单不同。
        """
        digest = ("【用户级规则 · 必读必守 · 本对话只发这一次】\n很长的协议全文\n"
                  "【本项目规则 · 全文 · 本对话只发这一次】\n很长的项目规则\n")
        with patch.object(hub.Api, "library_digest",
                          side_effect=lambda cwd, name, repeat=False:
                          "" if repeat else digest):
            first = hub.Api()._build_takeover_prompt(
                "cd12ef34", "d:\\x", "任务甲", "f.md", "摘要", None, "")
            second = hub.Api()._build_takeover_prompt(
                "cd12ef34", "d:\\x", "任务乙", "f.md", "摘要", None, "",
                digest_repeat=True)
        self.assertIn("很长的协议全文", first)
        self.assertNotIn("很长的协议全文", second, "第二张单还在重复规则全文")
        self.assertLess(len(second), len(first))
        # 砍掉的只是重复段，接手要用的东西一样不能少
        for needle in ("第一个动作", "原会话信息", "任务乙", "接手步骤"):
            self.assertIn(needle, second)

    def test_the_rulebook_ledger_is_per_agent_and_per_workspace(self):
        """记账要认「谁 + 哪个工作区」：同一个人第二次才算重复，换个人得照发。"""
        hub.Api._digest_sent_at.clear()
        self.assertFalse(hub.Api._digest_already_sent("agent-1", r"d:\x"))
        self.assertTrue(hub.Api._digest_already_sent("agent-1", r"d:\x"))
        self.assertFalse(hub.Api._digest_already_sent("agent-2", r"d:\x"),
                         "换了接手方还当成重复，新人就拿不到规则了")
        self.assertFalse(hub.Api._digest_already_sent("agent-1", r"d:\y"),
                         "换了工作区还当成重复，规则是按工作区取的")
        hub.Api._digest_sent_at.clear()

    def test_the_repeat_digest_keeps_the_per_ticket_sidebar_title(self):
        """侧栏改名那块带的是本单标题、每单都不一样，不能跟着一起砍。"""
        with patch.object(hub.Api, "_library_index_path", lambda: None):
            repeated = hub.Api.library_digest(r"d:\x", "任务乙", repeat=True)
        self.assertIn("任务乙", repeated)
        self.assertIn("rename_chat", repeated)
        self.assertIn("资料已在本轮前一张接手单里给过", repeated)
        # 指路那句会点名这几块，所以要按「块标题」判它们真没跟来
        self.assertNotIn("【开干先看板 · 续接文档】", repeated)
        self.assertNotIn("【技能目录", repeated)

    def test_no_digest_omits_library_block(self):
        with patch.object(hub.Api, "library_digest", return_value=""):
            p = hub.Api()._build_takeover_prompt(
                "cd12ef34", "d:\\x", "任务", "f.md", "摘要", None, "")
        self.assertNotIn("【资料索引 · 接手后必读】", p)

    def test_send_reply_does_not_double_attach_embedded_digest(self):
        s = _s("a", "cd", "任务")
        s.library_sent = False
        text = "【资料索引 · 接手后必读】\n" + hub.TAKEOVER_PROMPT_HEAD
        out, claimed = hub.Api()._with_library_digest(s, text)
        self.assertEqual(out, text)
        self.assertFalse(claimed)
        self.assertFalse(s.library_sent)

    def test_normal_first_dispatch_still_attaches_digest(self):
        s = _s("a", "cd", "任务")
        s.library_sent = False
        old = hub.HUB.cfg.get("library_autosend", True)
        hub.HUB.cfg["library_autosend"] = True
        try:
            with patch.object(hub.Api, "library_digest", return_value="[INDEX]\n"):
                out, claimed = hub.Api()._with_library_digest(s, "把日报生成一下")
        finally:
            hub.HUB.cfg["library_autosend"] = old
        self.assertTrue(claimed)
        self.assertEqual("[INDEX]\n把日报生成一下", out)
        self.assertTrue(s.library_sent)


class TakeoverPromptSceneEvidenceTests(unittest.TestCase):
    """接手词要自带「现场」：前任 zt 轨迹 + 中文乱码提示。

    08-28 用户：「接手时有中文问题，然后新agent不能快速定位上一个agent做了
    什么跟对话内容」。f9c313ed 实测：前任断线前在干嘛，聊天记录里一个字
    没有；接手方中途还被 GBK 乱码带偏、连跑几趟终端专修编码。
    """

    def _prompt(self, cwd=r"d:\x", **kw):
        with patch.object(hub.Api, "library_digest", return_value=""):
            return hub.Api()._build_takeover_prompt(
                "cd12ef34", cwd, "任务", "f.md", "摘要", None, "", **kw)

    def test_the_predecessors_last_reports_ride_along_newest_first(self):
        p = self._prompt(zt_trail=["08-28 10:00 分析中 · 读代码",
                                   "08-28 10:20 提交中 · push 四笔修复"])
        self.assertIn("前任最后上报", p)
        self.assertIn("push 四笔修复", p)
        self.assertLess(p.index("提交中"), p.index("分析中"), "得是新→旧")

    def test_a_long_trail_only_keeps_the_last_three(self):
        p = self._prompt(zt_trail=["08-28 10:0%d 步骤%d · x" % (i, i)
                                   for i in range(5)])
        self.assertIn("步骤4", p)
        self.assertIn("步骤2", p)
        self.assertNotIn("步骤1", p, "接手词是给人读的，轨迹最多三条")

    def test_no_trail_takes_no_space(self):
        # 老会话/从文件接手没有轨迹，不能留一行空标签占地方
        self.assertNotIn("前任最后上报", self._prompt())

    def test_the_charset_tip_rides_in_the_steps(self):
        p = self._prompt()
        self.assertIn("乱码", p)
        self.assertIn("命令失败", p, "得说清乱码≠失败，不然它还去修终端")


class TakeoverGitEvidenceTests(unittest.TestCase):
    """接手词里的「git 现场」：前任做到哪一步，最硬的证据在仓库里。

    08-28 f9c313ed 实测：前任把 4 笔修复全提交推送了，聊天记录里一个字
    没有（没走 zhi 就断了），接手方翻了七八处才从 git log 对出进度。
    git 不挑 agent 类型：Cursor 流水只有 Cursor agent 才留，Codex 工人
    没有，但谁干活都得提交。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)

    def _git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t",
                        "-c", "user.email=t@x"] + list(args),
                       capture_output=True, timeout=30, check=True)

    def _commit(self, name, msg):
        (self.repo / name).write_text("x", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-m", msg)

    def test_recent_commits_land_in_the_block_newest_first(self):
        self._git("init")
        self._commit("a.txt", "fix: 第一笔")
        self._commit("b.txt", "fix: 第二笔")
        block = hub.Api._takeover_git_evidence(str(self.repo))
        self.assertIn("【git 现场】", block)
        self.assertIn("fix: 第一笔", block)
        self.assertIn("fix: 第二笔", block)
        self.assertLess(block.index("第二笔"), block.index("第一笔"))

    def test_uncommitted_leftovers_are_flagged(self):
        # 半成品比提交更要紧：接手方不知道就会跟它撞车
        self._git("init")
        self._commit("a.txt", "fix: 有提交")
        (self.repo / "wip.txt").write_text("半成品", encoding="utf-8")
        self.assertIn("未提交改动",
                      hub.Api._takeover_git_evidence(str(self.repo)))

    def test_a_clean_tree_is_not_flagged(self):
        self._git("init")
        self._commit("a.txt", "fix: 干净")
        self.assertNotIn("未提交改动",
                         hub.Api._takeover_git_evidence(str(self.repo)))

    def test_a_plain_folder_stays_silent(self):
        # 不是仓库/git 跑不成：整段省略，一次 git 故障不许挡住接手词生成
        self.assertEqual("", hub.Api._takeover_git_evidence(str(self.repo)))

    def test_a_missing_dir_stays_silent(self):
        self.assertEqual(
            "", hub.Api._takeover_git_evidence(str(self.repo / "不存在")))

    def test_the_block_rides_into_the_prompt(self):
        self._git("init")
        self._commit("a.txt", "fix: 进了接手词")
        with patch.object(hub.Api, "library_digest", return_value=""):
            p = hub.Api()._build_takeover_prompt(
                "cd12ef34", str(self.repo), "任务", "f.md", "摘要", None, "")
        self.assertIn("fix: 进了接手词", p)


class ReapShellTests(unittest.TestCase):
    class _Client:
        def __init__(self):
            self.closed_convs, self.sessions, self.cwd = {}, {}, WS1

    def setUp(self):
        _ledger_hygiene(self)

    def test_empty_shell_is_closed_once_the_takeover_lands(self):
        shell = _s("b", "c2", "待命·乙", msgs=2)
        shell.handed_off_to = "cd"
        with _hub_with(shell)[0], _hub_with(shell)[1], \
             patch.object(hub.Hub, "log_end", lambda self, s, why: None):
            hub.HUB._reap_handed_off_shell(self._Client(), "cd")
            self.assertNotIn("b", hub.HUB.sessions)

    def test_shell_with_real_work_in_it_is_left_alone(self):
        busy = _s("b", "c2", "待命·乙", msgs=25)
        busy.handed_off_to = "cd"
        pend = _s("c", "c3", "待命·丙", pending=True)
        pend.handed_off_to = "cd"
        with _hub_with(busy, pend)[0], _hub_with(busy, pend)[1], \
             patch.object(hub.Hub, "log_end", lambda self, s, why: None):
            hub.HUB._reap_handed_off_shell(self._Client(), "cd")
            self.assertIn("b", hub.HUB.sessions)
            self.assertIn("c", hub.HUB.sessions)

    def test_checkin_pending_handed_off_shell_is_closed(self):
        # 08-14：handed_off 的待命还挂着报到 zhi → 接手落地仍应收
        shell = _s("b", "c2", "待命·乙", msgs=2, pending=True)
        shell.pending = {"id": "q", "message": "已就位",
                         "options": ["开始任务", "结束"]}
        shell.handed_off_to = "cd"
        with _hub_with(shell)[0], _hub_with(shell)[1], \
             patch.object(hub.Hub, "log_end", lambda self, s, why: None):
            hub.HUB._reap_handed_off_shell(self._Client(), "cd")
            self.assertNotIn("b", hub.HUB.sessions)
            self.assertIsNone(shell.pending)

    def test_unrelated_conversations_are_untouched(self):
        other = _s("b", "c2", "待命·乙")
        with _hub_with(other)[0], _hub_with(other)[1]:
            hub.HUB._reap_handed_off_shell(self._Client(), "cd")
            self.assertIn("b", hub.HUB.sessions)

    def test_prompt_target_is_enough_without_handed_off_to(self):
        # 08-15：复制接手词发出去，没走 share_takeover，handed_off_to 是空的
        shell = _s("b", "34892f47", "待命·cursor工作流·2f47", msgs=2)
        shell.messages = [{
            "role": "user",
            "html": "<pre>%s\nconversation_id=「76a1cf4d」</pre>" % hub.TAKEOVER_PROMPT_HEAD,
        }]
        live = _s("d", "76a1cf4d", "录播·紧急插播", msgs=20)
        with _hub_with(shell, live)[0], _hub_with(shell, live)[1], \
             patch.object(hub.Hub, "log_end", lambda self, s, why: None):
            hub.HUB._reap_handed_off_shell(self._Client(), "76a1cf4d")
            self.assertNotIn("b", hub.HUB.sessions)
            self.assertIn("d", hub.HUB.sessions)


class DeathPushActionTests(unittest.TestCase):
    def test_death_push_carries_a_one_tap_dispatch_button(self):
        with patch.dict(hub.HUB.cfg, {"share_enabled": True, "share_token": "tok",
                                      "remote_base_url": "http://100.64.0.1:39080"}):
            act = hub.HUB._takeover_action("sid-1")
        self.assertEqual("http", act["action"])
        self.assertIn("/api/takeover?t=tok", act["url"])
        self.assertIn("sid-1", act["body"])
        self.assertIn("auto", act["body"])

    def test_no_button_when_sharing_is_off(self):
        with patch.dict(hub.HUB.cfg, {"share_enabled": False}):
            self.assertIsNone(hub.HUB._takeover_action("sid-1"))


if __name__ == "__main__":
    unittest.main()
