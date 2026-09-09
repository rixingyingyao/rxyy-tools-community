# -*- coding: utf-8 -*-
"""接手落地之后，继任 agent 不许再把自己当成待命壳。

08-25 现场（hub-run.log + 聊天记录 .md 双证）：

    11:42:04  派接手即归并 壳 tab=待命·cursor工作流·f7d6 conv=5cbbf7d6 → b4eff2ee
    12:26:22  标签改名: rxyy tools·全面体检 → 待命·cursor工作流·f2ee
    13:31:35  AI：📍 已就位待命（对话 5cbbf7d6）。点「开始任务」我就开工。
    13:33:26  用户：接手功能还出问题了？…你这是什么情况？失忆了还是什么？

链条是这样接上的：接手把壳 ID 别名并进原 tab（这一步是对的，壳 tab 当场消失
= 用户看到的「待命报道壳自动关了」）。可继任 agent 手上那份报到提示词还在，
它每次 zhi 仍带着报到时的 task_name「待命·<工作区>」——maybe_apply_task_name
照单全收，把一个干了半个月活的 tab 改名成「待命·cursor工作流·f2ee」。

改名不是化妆问题。常驻协议第 0 条写着「task_name 以待命打头 = 报到壳，用户点
开始任务前只调 zhi，禁止 Read/Grep」。于是下一个读到这个 tab 的 agent（包括
它自己下一轮）一律判定「我在待命」，什么都不干，只回一句「已就位」——用户
看到的就是「接手完就失忆」。

修复契约（本测试锁死）：
1. 待命壳名不许给「已认领活」的 tab 改名——壳名不含任何信息，它只会把干活
   tab 降级成壳；
2. 报到那句 zhi 落到一个已认领活的 tab 上时，hub 当场答复它「接手已生效、
   你带的是 X、先读聊天记录接着干」，不再把这句报到挂成一张等用户点的卡；
3. 防死锁：同一个 tab 只当场答一次，agent 若坚持再报，照旧放行给用户，
   绝不能因为这道闸让人够不着 agent。
"""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub
import session_core

WS = r"d:\桌面\working\cursor工作流"


def _s(sid, conv, name, **kw):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = sid, conv, name
    s.cwd = s.task_root = kw.get("root", WS)
    s.cursor_uuid = kw.get("uuid")
    s.msg_seq = kw.get("msg_seq", 2)
    s.machine_seq = 0
    s.real_seq = kw.get("real_seq", kw.get("msg_seq", 2))
    s.pending = kw.get("pending")
    s.queued = []
    s.messages = []
    s.connected = True
    s.client = None
    s.rev = 0
    s.name_locked = False
    s.cursor_title = None
    s.shell_born = kw.get("shell_born",
                          hub.CHECKIN_SHELL_NAME_RE.match(name) is not None)
    s.agent_named = kw.get("agent_named", False)
    s.agent_project = kw.get("agent_project", "")
    s.agent_status = ""
    s.agent_activity = ""
    s.agent_status_ts = kw.get("agent_status_ts", 0)
    s.claimed_task_ts = kw.get("claimed_task_ts", 0)
    s.file_path = kw.get("file_path")
    s.recon_deadline = None
    s.lock = threading.Lock()
    return s


def _rename(s, new_name, others=()):
    sessions = {s.id: s}
    sessions.update({x.id: x for x in others})
    with (patch.dict(hub.HUB.cfg, {"team_assign": {}, "team_assign_auto": {},
                                   "team_tracks": {}}, clear=False),
          patch.object(hub, "save_config", lambda c: None),
          patch.object(hub, "log_event", lambda *a, **k: None),
          patch.object(hub.HUB, "_push_name_to_cursor", lambda self_s: None),
          patch.object(hub.HUB, "sessions", sessions),
          patch.object(hub.HUB, "lock", threading.Lock())):
        hub.HUB.maybe_apply_task_name(s, new_name)
    return s.name


class ShellNameNeverDemotesAWorkingTab(unittest.TestCase):
    """壳名不许把干活的 tab 改名成待命（本次事故的直接复现）。"""

    def test_the_real_crash_08_25(self):
        # 12:26:22 现场：继任 agent 带着报到时的 task_name 又调了一次 zhi
        s = _s("tab", "b4eff2ee", "rxyy tools·全面体检",
               agent_named=True, msg_seq=90)
        self.assertEqual("rxyy tools·全面体检",
                         _rename(s, "待命·cursor工作流"))

    def test_dedupe_suffix_variant_is_blocked_too(self):
        # 当时真正落下来的名字带去重后缀（·f2ee）：拦的是「壳名」这一类，
        # 不是某一个字符串
        s = _s("tab", "b4eff2ee", "rxyy tools·全面体检",
               agent_named=True, msg_seq=90)
        other = _s("shell", "5cbbf7d6", "待命·cursor工作流")
        self.assertEqual("rxyy tools·全面体检",
                         _rename(s, "待命·cursor工作流", others=[other]))

    def test_english_checkin_name_is_blocked_as_well(self):
        s = _s("tab", "c1", "rxyy tools·全面体检", agent_named=True, msg_seq=90)
        self.assertEqual("rxyy tools·全面体检",
                         _rename(s, "Persistent Plus check-in"))

    def test_a_tab_that_only_reported_zt_is_protected_too(self):
        # 还没自报真名、但已经 zt 上报过进度 = 已认领活（08-12 铁律）
        s = _s("tab", "c1", "接手·待定", agent_status_ts=time.time())
        self.assertEqual("接手·待定", _rename(s, "待命·cursor工作流"))

    def test_a_genuine_shell_can_still_be_renamed_shell_to_shell(self):
        # 同一个壳换工作区重报到：它什么活都没认领，改名照旧
        s = _s("shell", "c1", "待命·A")
        self.assertEqual("待命·B", _rename(s, "待命·B"))

    def test_a_shell_becoming_a_real_tab_still_works(self):
        s = _s("shell", "c1", "待命·cursor工作流")
        self.assertEqual("rxyy tools·接手交接",
                         _rename(s, "rxyy tools·接手交接"))
        self.assertTrue(s.agent_named)

    def test_a_working_tab_can_still_rename_itself_to_another_real_name(self):
        s = _s("tab", "c1", "rxyy tools·全面体检", agent_named=True, msg_seq=90)
        self.assertEqual("rxyy tools·接手交接",
                         _rename(s, "rxyy tools·接手交接"))

    def test_the_default_session_name_is_not_treated_as_claimed(self):
        # 没带 task_name 的连接落到兜底名「会话」，它没认领过活，别误伤
        s = _s("s", "c1", "会话")
        self.assertEqual("待命·cursor工作流", _rename(s, "待命·cursor工作流"))
        self.assertFalse(s.agent_named)


class CheckinOnAWorkingTabIsAnsweredNotParked(unittest.TestCase):
    """报到闸：报到落到干活 tab = 接手已生效，当场把人叫醒，别挂给用户。"""

    def _gate(self, s, message, options):
        return session_core.checkin_takeover_notice(hub.HUB, s, message, options)

    def test_the_standby_checkin_is_bounced_with_the_real_task(self):
        s = _s("tab", "b4eff2ee", "rxyy tools·全面体检",
               agent_named=True, msg_seq=90,
               file_path=r"D:\持久plus聊天记录\rxyy tools·全面体检.md")
        out = self._gate(s, "📍 cursor工作流（d:\\…）· 对话 5cbbf7d6 已就位",
                         ["开始任务", "结束"])
        self.assertTrue(out)
        self.assertIn("rxyy tools·全面体检", out)          # 你带的是这个活
        self.assertIn("rxyy MCP聊天记录", out)              # 去哪儿补记忆
        self.assertIn("b4eff2ee", out)                     # 原对话 ID

    def test_options_alone_are_enough_to_recognise_a_checkin(self):
        s = _s("tab", "c1", "rxyy tools·全面体检", agent_named=True, msg_seq=90)
        self.assertTrue(self._gate(s, "报到", ["开始任务", "结束"]))

    def test_message_alone_is_enough_too(self):
        s = _s("tab", "c1", "rxyy tools·全面体检", agent_named=True, msg_seq=90)
        self.assertTrue(self._gate(s, "📍 cursor工作流 · 对话 5cbbf7d6 已就位", []))

    def test_a_genuine_shell_checkin_is_left_alone(self):
        # 真待命壳照旧挂给用户，「开始任务 / 结束」那张卡不能没了
        s = _s("shell", "c1", "待命·cursor工作流")
        self.assertEqual("", self._gate(s, "📍 · 对话 c1 已就位",
                                        ["开始任务", "结束"]))

    def test_a_real_question_on_a_working_tab_is_never_bounced(self):
        s = _s("tab", "c1", "rxyy tools·全面体检", agent_named=True, msg_seq=90)
        self.assertEqual("", self._gate(
            s, "两个洞都堵上了，要不要我顺手把常驻区也换了？",
            ["换", "先不换"]))

    def test_a_finished_report_on_a_working_tab_is_never_bounced(self):
        s = _s("tab", "c1", "rxyy tools·全面体检", agent_named=True, msg_seq=90)
        self.assertEqual("", self._gate(s, "都改完了，验收看这里", ["结束"]))

    def test_the_gate_fires_once_then_lets_the_agent_through(self):
        # 防死锁：闸只叫醒一次。它再报第二次说明叫不醒，那就放行给用户，
        # 绝不能因为这道闸把 agent 卡成谁也够不着
        s = _s("tab", "c1", "rxyy tools·全面体检", agent_named=True, msg_seq=90)
        self.assertTrue(self._gate(s, "📍 已就位", ["开始任务", "结束"]))
        self.assertEqual("", self._gate(s, "📍 已就位", ["开始任务", "结束"]))

    def test_keepalive_resume_with_an_empty_message_is_not_a_checkin(self):
        s = _s("tab", "c1", "rxyy tools·全面体检", agent_named=True, msg_seq=90)
        self.assertEqual("", self._gate(s, "", ["开始任务", "结束"]))


class TheGateRearmsForEachNewAmnesia(unittest.TestCase):
    """「只叫醒一次」得是「每次失忆各叫醒一次」，不是「这个 tab 一辈子一次」。

    08-26 现场（控制台截图 + takeover-aliases.json 双证）：

        08:27:11  用户派接手，闸把继任叫醒了一次——它照着叫醒词去读了聊天记录，
                  接上前任没做完的活
        09:32:38  它把这段活干完，交了完整报告（一条实打实的 zhi）
        09:37:17  Cursor 压缩上下文，它照着**还留在第一条用户消息里**的报到提示词
                  又报了一次到。这一次闸门是空的：报到穿过去，在一个干了一早上活的
                  tab 上挂出一张「📍 已就位 / 开始任务 / 结束」的卡
        09:37:44  用户：「又出现这种情况了？是因为压缩上下文导致的吗？」
                  紧接着：「卧槽你串台了？」——卡里报的是壳 ID 087f1836，
                  而这个 tab 是 b4eff2ee（别名表里 087f1836→b4eff2ee，路由其实没错，
                  错的是一张不该存在的卡在报另一个 ID）

    防死锁那一条本身没错：叫不醒就得放行给用户，绝不能因为这道闸把 agent 卡成
    谁也够不着。错在把「叫不醒」和「一小时后又失忆一次」当成同一件事——后者中间
    干完了一整段活。用「这中间它有没有真说过话」把两者分开。
    """

    def _gate(self, s, message="📍 已就位", options=("开始任务", "结束")):
        return session_core.checkin_takeover_notice(
            hub.HUB, s, message, list(options))

    def _tab(self, **kw):
        kw.setdefault("real_seq", 90)
        return _s("tab", "b4eff2ee", "rxyy tools·全面体检",
                  agent_named=True, msg_seq=90, **kw)

    def test_the_real_crash_08_26(self):
        s = self._tab()
        self.assertTrue(self._gate(s))          # 08:27 接手：叫醒第一次
        s.checkin_bounced_ts -= 3600            # 一小时过去了
        s.real_seq += 12                        # 这中间它说过话（09:32 那份报告）
        self.assertTrue(self._gate(s),          # 09:37 压缩后又失忆 → 还得叫醒
                        "干完一整段活之后的失忆是新的一次，不是「叫不醒」")

    def test_two_checkins_in_a_row_still_fall_through(self):
        # 防死锁不能被这次修复削掉：中间一个字没说 = 真叫不醒，放行给用户
        s = self._tab()
        self.assertTrue(self._gate(s))
        s.checkin_bounced_ts -= 3600            # 哪怕隔得再久
        self.assertEqual("", self._gate(s))

    def test_a_zt_only_worker_re_arms_too(self):
        # 长任务常常整段只报 zt 不调 zhi，它一样是醒着的
        s = self._tab()
        self.assertTrue(self._gate(s))
        s.checkin_bounced_ts -= 3600
        s.agent_status_ts = time.time()
        self.assertTrue(self._gate(s))

    def test_a_zt_in_the_same_breath_does_not_re_arm(self):
        # zt 极便宜：一个懵住的 agent 完全可能「zt 一声、马上再报一次到」。
        # 没有这道时间下限，闸就成了无限叫醒，用户永远看不见它卡住了
        s = self._tab()
        self.assertTrue(self._gate(s))
        s.agent_status_ts = time.time()
        self.assertEqual("", self._gate(s))

    def test_real_work_in_the_same_breath_does_not_re_arm(self):
        s = self._tab()
        self.assertTrue(self._gate(s))
        s.real_seq += 5
        self.assertEqual("", self._gate(s))

    def test_a_third_amnesia_is_bounced_as_well(self):
        # 不是「放宽到两次」：每一次「真干过活 + 又失忆」都该被接住
        s = self._tab()
        for _ in range(3):
            self.assertTrue(self._gate(s))
            s.checkin_bounced_ts -= 3600
            s.real_seq += 12

    def test_a_session_that_never_bounced_is_unaffected(self):
        # 老对象/旧快照没有这两个新字段，getattr 兜底不能把第一次叫醒也吃掉
        s = self._tab()
        self.assertFalse(hasattr(s, "checkin_bounced_seq"))
        self.assertTrue(self._gate(s))


class CheckinGateWiredIntoTheHub(unittest.TestCase):
    """闸要真的接在 zhi 的入站路径上，光有函数不算修好。"""

    class _Sock:
        pass

    def _deliver(self, s, message, options, steps=None, boom=False):
        sent = []
        client = type("C", (), {})()
        client.sessions = {s.conv_key: s}
        client.closed_convs = {}
        client.cwd = WS
        client.pid = 4242
        client.peer_ip = None
        client.last_heartbeat = 0

        def _send(payload):
            sent.append(payload)
            if steps is not None:
                steps.append("answer")

        client.send = _send
        s.client = client
        s.send = client.send

        def _step(name):
            def run(_s):
                if steps is not None:
                    steps.append(name)
                if boom:
                    raise RuntimeError("Cursor 库这一拍被独占")
            return run

        with (patch.object(hub.HUB, "sessions", {s.id: s}),
              patch.object(hub.HUB, "order", [s.id]),
              patch.object(hub.HUB, "takeover_aliases", {"5cbbf7d6": s.conv_key}),
              patch.object(hub.HUB, "resolve_session",
                           lambda cl, ck, tn: s),
              patch.object(hub.HUB, "_verify_identity_by_generating",
                           _step("verify")),
              patch.object(hub.HUB, "_reap_takeover_shell", _step("reap")),
              patch.object(hub, "log_event", lambda *a, **k: None),
              patch.object(hub, "maybe_ensure_project_mcp", lambda cwd: None)):
            hub.HUB._handle_client_msg(client, self._Sock(), {
                "type": "zhi_request",
                "id": "rpc-1",
                "conversation_id": "5cbbf7d6",
                "task_name": "待命·cursor工作流",
                "message": message,
                "predefined_options": options,
                "cwd": WS,
            })
        return sent

    def test_checkin_is_answered_immediately_and_never_becomes_a_card(self):
        s = _s("tab", "b4eff2ee", "rxyy tools·全面体检",
               agent_named=True, msg_seq=90,
               file_path=r"D:\持久plus聊天记录\rxyy tools·全面体检.md")
        sent = self._deliver(s, "📍 cursor工作流 · 对话 5cbbf7d6 已就位",
                             ["开始任务", "结束"])
        self.assertIsNone(s.pending)          # 没变成一张等用户点的卡
        self.assertEqual(1, len(sent))
        self.assertEqual("zhi_response", sent[0]["type"])
        self.assertEqual("rpc-1", sent[0]["id"])
        self.assertIn("rxyy tools·全面体检", sent[0]["user_input"])
        self.assertEqual([], sent[0]["selected_options"])

    def test_a_real_question_still_becomes_a_card(self):
        s = _s("tab", "b4eff2ee", "rxyy tools·全面体检",
               agent_named=True, msg_seq=90)
        sent = self._deliver(s, "常驻区要不要现在换？", ["换", "先不换"])
        self.assertIsNotNone(s.pending)
        self.assertEqual("常驻区要不要现在换？", s.pending["message"])
        self.assertEqual([], sent)

    def test_the_previous_agents_card_is_not_left_behind_as_a_dead_card(self):
        """前任挂着的那张卡，waiter 已被 superseded 收掉，卡不能留在界面上。

        留着的后果不是难看：用户去答它，回复会被送去 stale 那个 id，而那头
        早就没人等了——这条回复就此蒸发。
        """
        s = _s("tab", "b4eff2ee", "rxyy tools·全面体检",
               agent_named=True, msg_seq=90,
               pending={"id": "old-q", "message": "前任问的那句", "options": []})
        sent = self._deliver(s, "📍 · 对话 5cbbf7d6 已就位", ["开始任务", "结束"])
        self.assertIsNone(s.pending)
        sources = [x.get("source") for x in sent]
        self.assertIn("superseded", sources)       # 旧 waiter 已经被收掉了
        self.assertIn("takeover_wake", sources)

    def test_the_tab_goes_back_to_processing_not_idle(self):
        """叫醒之后 agent 就在干活，tab 不能显示成待机。

        processing_since 是「话已交给 AI、等它再提问」的唯一信号：留成 None，
        点是灰的、面板说它闲着，跟真相正相反。
        """
        s = _s("tab", "b4eff2ee", "rxyy tools·全面体检",
               agent_named=True, msg_seq=90)
        s.detached = True
        s.detached_since = time.time() - 600
        self._deliver(s, "📍 · 对话 5cbbf7d6 已就位", ["开始任务", "结束"])
        self.assertIsNotNone(s.processing_since)
        self.assertFalse(s.detached)               # 它刚说过话，脱离态该结束
        self.assertIsNone(s.detached_since)

    def test_identity_is_recalibrated_before_the_agent_is_let_go(self):
        """闸开了也得走身份自校准，而且要赶在答复之前。

        这一瞬是「叫话的 Cursor 会话必在生成中」的唯一铁证窗口，答复一发出去
        agent 就不生成了。_reap_takeover_shell 里的 _relocate_takeover_uuid 更是
        把 cursor_uuid 从被接手的旧对话挪到接手方新对话的那一步——跳过它，
        模型牌和死因牌就一直挂着前任对话的结论（rxyy 08-25 实测「牌变得慢」）。
        """
        steps = []
        s = _s("tab", "b4eff2ee", "rxyy tools·全面体检",
               agent_named=True, msg_seq=90)
        self._deliver(s, "📍 · 对话 5cbbf7d6 已就位", ["开始任务", "结束"],
                      steps=steps)
        self.assertEqual(["verify", "reap", "answer"], steps)

    def test_a_failing_probe_still_lets_the_agent_go(self):
        # Cursor 库正被独占之类：校准失败也必须把话答出去，
        # 否则 agent 就卡死在这次 zhi 上，谁也够不着
        s = _s("tab", "b4eff2ee", "rxyy tools·全面体检",
               agent_named=True, msg_seq=90)
        sent = self._deliver(s, "📍 · 对话 5cbbf7d6 已就位",
                             ["开始任务", "结束"], boom=True)
        self.assertEqual(1, len(sent))
        self.assertEqual("takeover_wake", sent[0]["source"])

    def test_the_user_can_see_that_the_takeover_agent_was_woken(self):
        # 当场答复不等于把用户蒙在鼓里：闸开过一次，tab 里要留一行说明
        s = _s("tab", "b4eff2ee", "rxyy tools·全面体检",
               agent_named=True, msg_seq=90)
        self._deliver(s, "📍 · 对话 5cbbf7d6 已就位", ["开始任务", "结束"])
        says = [m for m in s.messages
                if m.get("role") == "sys" and "接手" in (m.get("html") or "")]
        self.assertEqual(1, len(says), s.messages)


if __name__ == "__main__":
    unittest.main()
