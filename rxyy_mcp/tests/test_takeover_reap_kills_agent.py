# -*- coding: utf-8 -*-
"""收壳 / 交接之后，控制台不许把还在干活的 agent 判死。

08-25 现场（rxyy 截图）：待命壳 6176cedf 报到、接活、干了 49 分 15 秒。壳被收起
的那一刻它这一侧的 conversation_id 被打进 closed_convs，下一次 zhi 拿回来的是

    [用户已在rxyy MCP控制台关闭此对话，请立即结束当前任务，不要再调用 zhi]

于是它当场收工——「对话 6176cedf 已在控制台侧被关闭，我按约定停手了，不再继续
调用」——手里那个被接手会话的原 ID 一次都没再试，整个rxyy MCP 被它判成掉线。
rxyy 的原话：「接手完之后，agent 认为连不上对话了，就自动断了rxyy MCP mcp」。

三处根因，本测试各锁一条：

1. 收壳（_reap_shell_into / _reap_handed_off_shell）把壳 ID 封进 closed_convs。
   壳 ID 是同一个 agent 手上还在用的 ID，正确做法是登记成接手别名让它继续可用。
   alias_shell_into 的 docstring 早写死了这条纪律（「绝不能设 closed_convs」），
   另外两条收壳路径没照做。
2. _handover_out 归档的老 tab 也封 conv。同样带旧 ID 回来，zt/ji 会自愈
   （_resolve_for_signal revive_handed，08-12），只有 zhi 回「请立即结束当前
   任务」——同一件事两种结论，而 zhi 恰恰是 agent 唯一会当真的那条。
3. 真被用户 × 掉时那句话本身。「结束当前任务」被读成「rxyy MCP 没了」，agent
   顺手把 MCP 也停了。语义应当是「这个 ID 到此为止」，不是「你别干了」。
"""
import re
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub  # noqa: E402
import server  # noqa: E402

WS = r"d:\桌面\working\cursor工作流"


def _sess(sid, conv, name, **kw):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = sid, conv, name
    s.cwd = s.task_root = kw.get("root", WS)
    s.peer_ip = kw.get("peer_ip", "127.0.0.1")
    s.pid = kw.get("pid", 4242)
    s.msg_seq = s.real_seq = kw.get("msg_seq", 2)
    s.machine_seq = 0
    s.pending = kw.get("pending")
    s.queued = []
    s.messages = []
    s.connected = True
    s.archived = kw.get("archived", False)
    s.handed_off_to = kw.get("handed_off_to", "")
    s.id_history = []
    s.client = None
    s.rev = 0
    s.file_path = None
    s.end_reason = ""
    s.lock = threading.Lock()
    return s


class _Client:
    def __init__(self, closed=None, sessions=None):
        self.closed_convs = dict(closed or {})
        self.sessions = dict(sessions or {})
        self.cwd = WS
        self.pid = 4242
        self.peer_ip = "127.0.0.1"
        self.last_heartbeat = 0
        self.sent = []

    def send(self, payload):
        self.sent.append(payload)


class _Landed(Exception):
    """resolve_session 一到就停：这几条只关心「落到哪个 conv、有没有被判死」。"""


class ReapedShellIdStaysUsable(unittest.TestCase):
    """收壳只是收 tab，不是封 ID——那个 ID 后面还有个大活人在用。"""

    def setUp(self):
        self.aliases = {}
        self.shell = _sess("tab-shell", "6176cedf", "待命·cursor工作流")
        self.orig = _sess("tab-orig", "b4eff2ee", "rxyy tools·全面体检", msg_seq=90)
        self.client = _Client(sessions={"6176cedf": self.shell})
        self.shell.client = self.client
        self.shell.send = self.client.send
        self._patches = [
            patch.object(hub.HUB, "sessions",
                         {"tab-shell": self.shell, "tab-orig": self.orig}),
            patch.object(hub.HUB, "order", ["tab-shell", "tab-orig"]),
            patch.object(hub.HUB, "takeover_aliases", self.aliases),
            # 台账同拍隔离（A步起 _retire_conv_into 一笔写两个视图）
            patch.object(hub.HUB, "takeover_ledger", {}, create=True),
            patch.object(hub.Hub, "_save_takeover_aliases", lambda self: None),
            patch.object(hub.HUB, "_tombstone_shell", MagicMock()),
            patch.object(hub.HUB, "log_end", MagicMock()),
            patch.object(hub, "log_event", MagicMock()),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    def test_reap_shell_into_aliases_instead_of_sealing(self):
        # 事后收壳（接手方已改用原 ID 说话）——08-25 那次走的就是这条
        hub.HUB._reap_shell_into(self.shell, self.orig)
        self.assertEqual("b4eff2ee", self.aliases.get("6176cedf"),
                         "壳 ID 必须登记成接手别名，迟到的调用照样有地方落")
        self.assertFalse(self.client.closed_convs.get("6176cedf"),
                         "封了这个 ID，下一次 zhi 就会被判死——这正是本次事故")
        self.assertNotIn("tab-shell", hub.HUB.sessions)  # tab 照旧收起

    def test_reap_handed_off_shell_aliases_instead_of_sealing(self):
        # 接手方拿原 ID 报到时顺手收掉自己那个壳，走的是另一条路径，同一条纪律
        self.shell.handed_off_to = "b4eff2ee"
        with patch.object(hub.HUB, "_is_takeover_shell", lambda x: True), \
             patch.object(hub.HUB, "_dismiss_shell_pending",
                          lambda x, succ_conv="": None), \
             patch.object(hub.HUB, "_forward_shell_mail", lambda x, succ: 0):
            hub.HUB._reap_handed_off_shell(self.client, "b4eff2ee")
        self.assertEqual("b4eff2ee", self.aliases.get("6176cedf"))
        self.assertFalse(self.client.closed_convs.get("6176cedf"))
        self.assertNotIn("tab-shell", hub.HUB.sessions)

    def test_alias_shell_into_registers_the_alias_itself(self):
        # 手工复制接手词贴过去这条路不经 share_takeover，没人替壳登记别名；
        # 登记必须落在 alias_shell_into 自己身上，别指望调用方
        with patch.object(hub.HUB, "_dismiss_shell_pending",
                          lambda x, succ_conv="": None), \
             patch.object(hub.HUB, "save_state", lambda: None), \
             patch.object(threading, "Thread", MagicMock()):
            hub.HUB.alias_shell_into(self.shell, self.orig)
        self.assertEqual("b4eff2ee", self.aliases.get("6176cedf"))

    def test_alias_chains_are_flattened_to_the_final_owner(self):
        # 连续接手两次：壳A→壳B→原对话。换名只做一跳，A 必须直接指向终点
        self.aliases["shellB"] = "b4eff2ee"
        hub.HUB._retire_conv_into("shellA", "shellB")
        self.assertEqual("b4eff2ee", self.aliases["shellA"])

    def test_a_conv_never_aliases_to_itself(self):
        hub.HUB._retire_conv_into("6176cedf", "6176cedf")
        self.assertNotIn("6176cedf", self.aliases)


class AClosedConvNeverKillsALiveAgent(unittest.TestCase):
    """入站闸：封条只对真被用户 × 掉的对话生效，且不许暗示 MCP 死了。"""

    def _deliver(self, client, conv_id, resolve=None):
        landed = {}

        def _resolve(cl, ck, tn):
            landed["conv"] = ck
            raise _Landed

        with patch.object(hub.HUB, "resolve_session", resolve or _resolve), \
             patch.object(hub, "log_event", MagicMock()):
            try:
                hub.HUB._handle_client_msg(client, object(), {
                    "type": "zhi_request",
                    "id": "rpc-1",
                    "conversation_id": conv_id,
                    "task_name": "rxyy tools·全面体检",
                    "message": "改完了，看一眼？",
                    "predefined_options": ["行", "再改"],
                    "cwd": WS,
                })
            except _Landed:
                pass
        return landed.get("conv"), client.sent

    def test_the_retired_shell_id_is_routed_not_killed(self):
        # 收壳登记了别名之后，接手方继续拿壳 ID 说话 = 照常落到原对话
        client = _Client()
        with patch.object(hub.HUB, "takeover_aliases", {"6176cedf": "b4eff2ee"}):
            conv, sent = self._deliver(client, "6176cedf")
        self.assertEqual("b4eff2ee", conv)
        self.assertEqual([], sent, "不许回那句判死的话")

    def test_a_handed_off_tab_that_still_asks_is_revived_not_killed(self):
        # _handover_out 归档并封了 conv，可这个 agent 正拿旧 ID 提问 = 交接判错了。
        # zt/ji 早就会自愈，zhi 是唯一还在回「请立即结束当前任务」的入口
        old = _sess("tab-old", "c-old", "OA对接", archived=True,
                    handed_off_to="c-new")
        client = _Client(closed={"c-old": True})
        with patch.object(hub.HUB, "takeover_aliases", {}), \
             patch.object(hub.HUB, "_resolve_for_signal",
                          lambda cl, ck, revive_handed=False:
                          old if revive_handed else None):
            conv, sent = self._deliver(client, "c-old")
        self.assertEqual("c-old", conv, "照常落回它自己的 tab，不许改投别人")
        self.assertEqual([], sent)
        self.assertNotIn("c-old", client.closed_convs, "封条要一并撤掉")

    def test_a_genuinely_closed_conv_is_still_stopped(self):
        # 用户真手动 × 掉的（归档但没有 handed_off_to）语义不变：这个 ID 到此为止
        client = _Client(closed={"c-dead": True})
        with patch.object(hub.HUB, "takeover_aliases", {}), \
             patch.object(hub.HUB, "_resolve_for_signal",
                          lambda cl, ck, revive_handed=False: None):
            conv, sent = self._deliver(client, "c-dead")
        self.assertIsNone(conv, "被 × 掉的对话不该再落进任何 tab")
        self.assertEqual(1, len(sent))
        self.assertEqual("popup_closed", sent[0]["source"])
        self.assertEqual("rpc-1", sent[0]["id"])

    def test_the_close_notice_never_reads_like_the_mcp_died(self):
        # 事故的最后一环：agent 把「结束当前任务」读成「rxyy MCP 没了」，
        # 顺手停用 MCP。这句话必须自己把话说死
        text = hub.CLOSED_CONV_REPLY
        self.assertIn("不是 MCP 故障", text)
        self.assertIn("conversation_id", text)
        self.assertNotIn("不要再调用 zhi]", text)
        self.assertTrue(
            re.search(r"不要(停用|断开)", text),
            "得明写「别把rxyy MCP 断了」，光说别用这个 ID 不够")

    def test_the_console_close_button_uses_the_same_wording(self):
        # 用户点 × 时当场答复 pending 的那句，和入站闸必须是同一句，
        # 否则改了一头另一头照旧把 agent 吓停
        src = (MODULE_DIR / "hub_api.py").read_text(encoding="utf-8")
        self.assertIn("hub.CLOSED_CONV_REPLY", src)
        self.assertNotIn("请立即结束当前任务，不要再调用 zhi", src)


class ReapingAShellNeverForgesAnEndClick(unittest.TestCase):
    """收壳答复报到 zhi 时，不许冒充 rxyy 点「结束」。

    08-25 第二份现场：一个待命壳报到后蹲了 52m24s，一被收壳就回
    「会话已在控制台关闭，我就此停下，不再继续调用」，全程没读过一个文件。
    报到卡的选项正好是「开始任务 / 结束」，而收壳答复回的是
    selected_options=["结束"]——在 agent 眼里这跟 rxyy 亲手点结束一模一样。
    接手提示词还在后任队列里躺着，没人去取。
    """

    def _dismiss(self, succ_conv="b4eff2ee", pending=None):
        shell = _sess("tab-shell", "e9358825", "待命·cursor工作流",
                      pending=pending if pending is not None else {
                          "id": "q-checkin",
                          "message": "📍 cursor工作流 · 对话 e9358825 已就位",
                          "options": ["开始任务", "结束"],
                      })
        client = _Client()
        shell.client = client
        shell.send = client.send
        hub.HUB._dismiss_shell_pending(shell, succ_conv)
        return shell, client.sent

    def test_the_reply_carries_no_selected_option_at_all(self):
        _, sent = self._dismiss()
        self.assertEqual(1, len(sent))
        self.assertEqual("reaped", sent[0]["source"])
        self.assertEqual([], sent[0]["selected_options"],
                         "回「结束」= 冒充用户点了结束按钮，agent 当场收工")

    def test_the_reply_says_where_the_work_went_and_to_keep_going(self):
        _, sent = self._dismiss()
        body = sent[0]["user_input"]
        self.assertIn("b4eff2ee", body)          # 活在哪
        self.assertIn("不是 MCP 故障", body)      # 别把rxyy MCP 判死
        self.assertIn("别停手", body)

    def test_the_waiter_is_still_answered_so_nothing_hangs(self):
        # 这个方法存在的本来目的：别让 MCP 那头永远堵在报到提问上
        shell, sent = self._dismiss()
        self.assertIsNone(shell.pending)
        self.assertEqual("q-checkin", sent[0]["id"])

    def test_without_a_successor_it_still_does_not_forge_an_end_click(self):
        _, sent = self._dismiss(succ_conv="")
        self.assertEqual([], sent[0]["selected_options"])
        self.assertIn("不是 MCP 故障", sent[0]["user_input"])

    def test_a_shell_with_nothing_pending_sends_nothing(self):
        shell, sent = self._dismiss(pending=None)
        # pending=None 走 _sess 的默认分支，这里显式再验一次空壳不发消息
        shell2 = _sess("t2", "c2", "待命·x")
        shell2.pending = None
        client = _Client()
        shell2.client = client
        shell2.send = client.send
        hub.HUB._dismiss_shell_pending(shell2, "b4eff2ee")
        self.assertEqual([], client.sent)

    def test_every_reap_path_tells_the_shell_where_its_work_went(self):
        # 四条收壳路都走 _reap 引擎，引擎里必须把后任传进 dismiss。
        # 漏了 agent 就收不到去处；再手拼一条路就是新事故。
        import inspect
        src = inspect.getsource(hub.Hub)
        self.assertIn("self._dismiss_shell_pending(shell, succ_conv)", src,
                      "引擎必须把后任传给 dismiss")
        self.assertNotIn("self._dismiss_shell_pending(x,", src,
                         "触发器不许再手拼 dismiss，一律走 _reap")
        for trigger in ("def alias_shell_into", "def _reap_shell_into",
                        "def _reap_handed_off_shell",
                        "def _reap_takeover_shell",
                        "def sweep_revived_takeover_shells"):
            self.assertIn(trigger, src)
        # 三条动手的触发器（派单 / 原ID报到 / 同窗+巡检的 wrapper）都调引擎
        self.assertIn("self._reap(", inspect.getsource(hub.Hub.alias_shell_into))
        self.assertIn("self._reap(", inspect.getsource(hub.Hub._reap_shell_into))
        self.assertIn("self._reap(", inspect.getsource(hub.Hub._reap_handed_off_shell))


class TakeoverReceiptAgreesWithTheTakeoverPrompt(unittest.TestCase):
    """回执结尾那句 ID 提示不许和接手正文打架。"""

    def test_a_takeover_dispatch_points_at_the_original_id(self):
        text = ("你的任务：接手一个此前在rxyy MCP控制台中断的会话。\n"
                "**conversation_id 必须改用「b4eff2ee」**（这是被接手会话的原ID）")
        self.assertEqual("b4eff2ee", server._takeover_switch_target(text))

    def test_a_plain_checkin_prompt_is_not_mistaken_for_a_switch(self):
        # 报到提示词里也有 conversation_id=「xxx」，那句说的是「全程沿用」；
        # 认宽了会把正常报到的回执也改写成「切 ID」
        text = ("立即调用rxyy MCP的zhi报到：conversation_id=「6176cedf」全程沿用；"
                "task_name=「待命·cursor工作流」")
        self.assertEqual("", server._takeover_switch_target(text))

    def test_an_ordinary_reply_has_no_switch_target(self):
        self.assertEqual("", server._takeover_switch_target("行，就这么改"))
        self.assertEqual("", server._takeover_switch_target(None))

    def test_the_wording_it_matches_is_still_the_one_the_console_sends(self):
        # 正则认的是「conversation_id 必须改用「X」」这个措辞，
        # 改了接手提示词却忘了改这里，回执就会重新和正文打架
        src = (MODULE_DIR / "hub_api.py").read_text(encoding="utf-8")
        self.assertIn("conversation_id 必须改用「{}」", src)


if __name__ == "__main__":
    unittest.main()
