# -*- coding: utf-8 -*-
"""zt 顺路取信 + 「待命」tab 改名硬提醒。

递话此前只有「用户下次在这个 tab 回话」时才发得出去（server.py:1418 的队列只在
zhi 里取），于是 agent 埋头干活的那半小时里，队友递过去的话它一个字看不见——
08-07 实测：10:31 发出的转告，对方 10:35 还在改文件，压根没收到。zt 本来就
每完成一个动作调一次，让它顺路把信带走，延迟从几十分钟降到几十秒。

用户的回复位一寸都不能动：那个位置永远留给 zhi（见 _may_take_the_reply_slot
的事故记录）。所以这里取的只是「排在用户后面」的那一批。
"""
import sys
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub  # noqa: E402
import server  # noqa: E402

WS1 = r"d:\Desktop\cursor工作流"


def _s(sid="s1", conv="aaaa1111", name="甲", root=WS1):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = sid, conv, name
    s.cwd = s.task_root = root
    s.connected = True
    s.pending = None
    s.queued = []
    s.messages = []
    s.msg_seq = 0
    s.rev = 0
    s.recon_deadline = None
    s.ide_active_cache = False
    s.lock = threading.Lock()
    return s


def _entry(qid, text, who):
    return {"id": qid, "text": text, "images": [], "files": [],
            "who": who, "msg": {}}


class TakeAgentMailTests(unittest.TestCase):
    """hub 侧：哪些信能被 zt 顺走，哪些一寸都不许动。"""

    def setUp(self):
        self.s = _s()

    def test_a_teammates_relay_rides_back_on_zt(self):
        self.s.queued = [_entry("q1", "hub.py 我要动了", "agent·乙")]
        texts, taken = hub.Api().take_agent_mail(self.s)
        self.assertEqual(["[agent·乙] hub.py 我要动了"], texts)
        self.assertEqual(1, len(taken))
        self.assertEqual([], self.s.queued)

    def test_console_receipts_and_board_alerts_ride_along_too(self):
        self.s.queued = [_entry("q1", "转告已送达", "控制台"),
                         _entry("q2", "【黑板·部署】要换装了", "黑板")]
        texts, _ = hub.Api().take_agent_mail(self.s)
        self.assertEqual(2, len(texts))
        self.assertEqual([], self.s.queued)

    def test_the_users_own_message_is_never_taken(self):
        # 用户那条只能由 zhi 取走。被 zt 顺走 = 用户在 tab 里发的话石沉大海，
        # agent 那头还当没人理它
        self.s.queued = [_entry("q1", "先测试再改", None),
                         _entry("q2", "hub.py 我要动了", "agent·乙")]
        texts, _ = hub.Api().take_agent_mail(self.s)
        self.assertEqual(["[agent·乙] hub.py 我要动了"], texts)
        self.assertEqual(["q1"], [e["id"] for e in self.s.queued])

    def test_the_panel_and_the_phone_are_the_user_talking_too(self):
        self.s.queued = [_entry("q1", "全员停一下", "团队面板"),
                         _entry("q2", "我在手机上看到了", "张垒")]
        texts, _ = hub.Api().take_agent_mail(self.s)
        self.assertEqual([], texts)
        self.assertEqual(2, len(self.s.queued))

    def test_an_empty_queue_costs_nothing(self):
        texts, taken = hub.Api().take_agent_mail(self.s)
        self.assertEqual(([], []), (texts, taken))
        self.assertEqual(0, self.s.rev)   # 没信就别惊动 UI 刷新

    def test_the_taken_bubble_stops_saying_queued(self):
        self.s.queued = [_entry("q1", "hub.py 我要动了", "agent·乙")]
        self.s.messages = [{"role": "user", "qid": "q1", "queued": True}]
        hub.Api().take_agent_mail(self.s)
        self.assertFalse(self.s.messages[0]["queued"])


class MailPutBackTests(unittest.TestCase):
    """取走了却没送到 = 凭空销毁一条转告，比迟到严重得多。

    hub 是先把信从队列里摘下来、再往 socket 上写的。写失败（控制台正在重启、
    MCP 进程被回收）时若就地吞掉，这条转告在两头都不存在了，而发送方那头
    收到的是「✓ 已提交转告」。所以送不出去必须原样放回队列，等下一趟。
    """

    def test_mail_goes_back_into_the_queue(self):
        s = _s()
        s.queued = [_entry("q1", "hub.py 我要动了", "agent·乙")]
        s.messages = [{"role": "user", "qid": "q1", "queued": True}]
        _, taken = hub.Api().take_agent_mail(s)
        self.assertEqual([], s.queued)

        hub.Api().put_back_agent_mail(s, taken)
        self.assertEqual(["q1"], [e["id"] for e in s.queued])
        self.assertTrue(s.messages[0]["queued"])   # 气泡改回「排队中」

    def test_it_does_not_jump_ahead_of_the_user(self):
        # 放回时用户那条已经排上了：机器消息仍旧排在用户后面
        s = _s()
        s.queued = [_entry("q1", "hub.py 我要动了", "agent·乙")]
        _, taken = hub.Api().take_agent_mail(s)
        s.queued = [_entry("q9", "先测试再改", None)]
        hub.Api().put_back_agent_mail(s, taken)
        self.assertEqual(["q9", "q1"], [e["id"] for e in s.queued])

    def test_putting_back_nothing_is_a_no_op(self):
        s = _s()
        hub.Api().put_back_agent_mail(s, [])
        self.assertEqual([], s.queued)
        self.assertEqual(0, s.rev)


class SendReplyFailurePutsMailBackTests(unittest.TestCase):
    """用户的回答没送出去时，捎带的队友转告不许跟着蒸发。

    send_reply 是先把「排在用户后面」的转告从队列摘下来拼进 user_input、
    再往 socket 上写的。08-25 体检发现：写失败只把 pending 还了回去，
    转告没还——用户重试时重新组稿，根本不会再带上它们，这些话就两头都
    不存在了（转告方那头收到的还是「✓ 已提交」）。
    """

    def setUp(self):
        self.s = _s()
        self.s.pending = {"id": "req1", "message": "选哪个方案？"}
        self.s.detached = False
        self.s.queued = [_entry("q1", "hub.py 我要动了", "agent·乙")]
        self.s.messages = [{"role": "user", "qid": "q1", "queued": True}]

        def boom(msg):
            raise OSError("连接断了")

        self.s.send = boom
        ps = [patch.object(hub.HUB, "sessions", {self.s.id: self.s}),
              patch.object(hub.HUB, "cfg", {"library_autosend": False,
                                            "max_messages": 200}),
              patch.object(hub.Api, "_with_session_memory",
                           lambda api, s, ui, selected=None: ui)]
        for p in ps:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in ps])

    def test_mail_rides_back_into_the_queue_when_send_fails(self):
        r = hub.Api().send_reply(self.s.id, "就按方案A", [], [], False)
        self.assertFalse(r["ok"])
        self.assertEqual(["q1"], [e["id"] for e in self.s.queued])
        self.assertTrue(self.s.messages[0]["queued"])   # 气泡改回「排队中」
        self.assertIsNotNone(self.s.pending)            # 提问还回去，允许重试

    def test_a_clean_send_still_takes_the_mail_along(self):
        # 修完不能把正常路径改坏：发送成功时信照旧被捎走、不再排队
        sent = []
        self.s.send = lambda msg: sent.append(msg)
        with patch.object(hub.Hub, "add_message", lambda h, s, m: None), \
             patch.object(hub.Hub, "save_msg_images", lambda h, imgs: []), \
             patch.object(hub.Hub, "log_user", lambda h, *a, **k: None), \
             patch.object(hub.Hub, "_mark_claimed", lambda h, *a, **k: None):
            r = hub.Api().send_reply(self.s.id, "就按方案A", [], [], False)
        self.assertTrue(r["ok"])
        self.assertEqual([], self.s.queued)
        self.assertIn("hub.py 我要动了", sent[0]["user_input"])
        self.assertFalse(self.s.messages[0]["queued"])


class RenameNudgeTests(unittest.TestCase):
    """tab 还叫「待命·xxx」却已经在干活了 → 往它队列里塞一句硬提醒。

    纪律里写了「接到真活后第一次 zhi/zt 必须换 task_name」，但没有任何东西拦得住
    不换：08-07 控制台上并排三个「待命·cursor工作流」，用户认不出谁在干嘛，
    队友转告也点不准名。
    """

    def setUp(self):
        self.s = _s(name="待命·cursor工作流")
        self.s.msg_seq = 8
        # 这里特意不 patch _auto_label_on_dispatch：那道「机器消息不改名」的闸门
        # 就在它函数体第一行，patch 掉等于把要验的东西拆了
        ps = [patch.object(hub.HUB, "sessions", {self.s.id: self.s}),
              patch.object(hub.HUB, "cfg", {"max_messages": 200}),
              patch.object(hub.Hub, "save_msg_images", lambda h, imgs: [])]
        for p in ps:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in ps])

    def _queued_texts(self):
        return [e["text"] for e in self.s.queued]

    def test_a_working_tab_still_called_standby_gets_nudged(self):
        hub.Api()._nudge_rename_if_standby(self.s)
        self.assertEqual(1, len(self.s.queued))
        self.assertIn("task_name", self._queued_texts()[0])
        self.assertEqual("控制台", self.s.queued[0]["who"])

    def test_a_tab_that_just_checked_in_is_left_alone(self):
        # 报到那一两句就催改名是冤枉它——它真的还在待命等派活
        self.s.msg_seq = 2
        hub.Api()._nudge_rename_if_standby(self.s)
        self.assertEqual([], self.s.queued)

    def test_a_properly_named_tab_is_never_nudged(self):
        self.s.name = "换装收尾"
        hub.Api()._nudge_rename_if_standby(self.s)
        self.assertEqual([], self.s.queued)

    def test_the_same_name_is_only_nudged_once(self):
        for _ in range(5):
            hub.Api()._nudge_rename_if_standby(self.s)
        self.assertEqual(1, len(self.s.queued))

    def test_renaming_and_falling_back_to_standby_nudges_again(self):
        hub.Api()._nudge_rename_if_standby(self.s)
        self.s.name = "待命·cursor工作流2"
        hub.Api()._nudge_rename_if_standby(self.s)
        self.assertEqual(2, len(self.s.queued))

    def test_the_nudge_must_not_become_the_tabs_new_name(self):
        # queue_message 末尾会拿正文去自动命名。这条以 who=控制台 入队，正是
        # 为了走 _is_machine_who 那道闸——否则 tab 会被改名成「⚠ 控制台纪律…」，
        # 一条催人改名的提醒反倒把名字改成了它自己
        hub.Api()._nudge_rename_if_standby(self.s)
        self.assertEqual("待命·cursor工作流", self.s.name)

    def test_the_nudge_never_takes_the_users_reply_slot(self):
        # tab 正阻塞在 zhi 等用户拍板时，这条提醒不许抢答
        self.s.pending = {"id": "req1", "message": "两个方案选哪个？"}
        answered = []
        with patch.object(hub.Api, "send_reply",
                          lambda *a, **k: answered.append(a) or {"ok": True}):
            hub.Api()._nudge_rename_if_standby(self.s)
        self.assertEqual([], answered)
        self.assertEqual(1, len(self.s.queued))
        self.assertIsNotNone(self.s.pending)


class DedupeNameTests(unittest.TestCase):
    """重名不再加 1/2：拼对话 ID 后 4 位。

    「待命·cursor工作流1 / 2」在列表里认不出谁是谁，而对话 ID 是用户在报到消息里
    看见的、转告点名用的同一个东西，拿它的后 4 位当后缀，一眼能对上人。
    """

    def _hub_with(self, *names):
        sessions = {}
        for i, (name, conv) in enumerate(names):
            s = _s(sid="s{}".format(i), conv=conv, name=name)
            sessions[s.id] = s
        return patch.object(hub.HUB, "sessions", sessions)

    def test_a_unique_name_is_untouched(self):
        with self._hub_with(("甲", "aaaa1111")):
            self.assertEqual("换装收尾",
                             hub.HUB._dedupe_name("换装收尾", conv_key="266bdbe4"))

    def test_a_clash_takes_the_conversation_id_not_a_number(self):
        with self._hub_with(("待命·cursor工作流", "aaaa1111")):
            self.assertEqual(
                "待命·cursor工作流·dbe4",
                hub.HUB._dedupe_name("待命·cursor工作流", conv_key="266bdbe4"))

    def test_without_an_id_it_still_falls_back_to_a_number(self):
        with self._hub_with(("待命·cursor工作流", "aaaa1111")):
            self.assertEqual("待命·cursor工作流1",
                             hub.HUB._dedupe_name("待命·cursor工作流"))

    def test_the_no_id_placeholder_never_becomes_a_suffix(self):
        # 不带 conversation_id 的连接在 hub 里一律记成 "__default__"，
        # 截末 4 位得到的「ault」比编号还难认
        with self._hub_with(("待命·cursor工作流", "aaaa1111")):
            self.assertEqual(
                "待命·cursor工作流1",
                hub.HUB._dedupe_name("待命·cursor工作流", conv_key="__default__"))

    def test_renaming_to_your_own_name_is_not_a_clash(self):
        # 同名 task_name 反复上报会把 tab 改成 新任务1→新任务3（实测事故）：
        # 加了 conv_key 之后这条护栏仍要在
        with self._hub_with(("换装收尾", "266bdbe4")):
            self.assertEqual(
                "换装收尾",
                hub.HUB._dedupe_name("换装收尾", exclude_id="s0",
                                     conv_key="266bdbe4"))


class FetchMailTests(unittest.TestCase):
    """MCP 侧：zt 发起取信，拿不到就当没有，绝不卡住 agent。"""

    def setUp(self):
        self.bridge = server.HubBridge()
        self.sent = []

    def _patch_send(self):
        return patch.object(server, "send_msg",
                            lambda sock, m: self.sent.append(m))

    def test_no_connection_means_no_mail_and_no_crash(self):
        self.assertEqual([], self.bridge.fetch_mail("aaaa1111", sock=None))

    def test_it_asks_the_hub_and_hands_back_what_came(self):
        def answer():
            for _ in range(200):
                with self.bridge.waiters_lock:
                    w = next(iter(self.bridge.waiters.values()), None)
                if w is not None:
                    w["resp"] = {"items": ["[agent·乙] hub.py 我要动了"]}
                    w["event"].set()
                    return
                threading.Event().wait(0.01)

        with self._patch_send():
            t = threading.Thread(target=answer, daemon=True)
            t.start()
            got = self.bridge.fetch_mail("aaaa1111", sock=object(), timeout=3.0)
            t.join(timeout=3)
        self.assertEqual(["[agent·乙] hub.py 我要动了"], got)
        self.assertEqual("agent_mail_fetch", self.sent[0]["type"])
        self.assertEqual("aaaa1111", self.sent[0]["conversation_id"])

    def test_a_silent_hub_just_means_no_mail_this_round(self):
        with self._patch_send():
            self.assertEqual(
                [], self.bridge.fetch_mail("aaaa1111", sock=object(), timeout=0.2))
        self.assertEqual({}, self.bridge.waiters)   # 等待方不许泄漏

    def test_a_late_answer_is_kept_for_the_next_round(self):
        # 超时那一刻 hub 那头已经把信从队列里摘走了，应答只是路上慢了。
        # 这里再丢掉，这条转告就两头都不存在了
        with self._patch_send():
            self.bridge.fetch_mail("aaaa1111", sock=object(), timeout=0.2)
        self.bridge._reader_dispatch({
            "type": "mail_response", "id": "已经没人等了",
            "conversation_id": "aaaa1111",
            "items": ["[agent·乙] hub.py 我要动了"]})
        with self._patch_send():
            got = self.bridge.fetch_mail("aaaa1111", sock=object(), timeout=0.2)
        self.assertEqual(["[agent·乙] hub.py 我要动了"], got)

    def test_an_old_hub_that_never_answers_stops_costing_two_seconds(self):
        # 源码版 server 配打包版 hub：老 hub 没有 agent_mail_fetch 这个分支，一声
        # 不吭。zt 是每完成一个动作就调的，白等的超时会加在每一步上，所以连丢几次
        # 之后就别再问了
        sock = object()
        with self._patch_send():
            for _ in range(server.HubBridge.MAIL_MISS_LIMIT):
                self.bridge.fetch_mail("aaaa1111", sock=sock, timeout=0.2)
            asked = len(self.sent)
            self.bridge.fetch_mail("aaaa1111", sock=sock, timeout=0.2)
        self.assertEqual(asked, len(self.sent))   # 这一趟根本没发问

    def test_a_new_connection_gets_another_chance(self):
        # 换装/重启后 hub 就是新版了，不能因为旧连接上判过死刑就永远不问
        with self._patch_send():
            for _ in range(server.HubBridge.MAIL_MISS_LIMIT):
                self.bridge.fetch_mail("aaaa1111", sock=object(), timeout=0.2)
            asked = len(self.sent)
            self.bridge.fetch_mail("aaaa1111", sock=object(), timeout=0.2)
        self.assertEqual(asked + 1, len(self.sent))

    def test_late_mail_never_lands_in_someone_elses_tab(self):
        # 一个 MCP 进程服务本机所有 Cursor 窗口：认不出归属宁可不收。
        # 把别人的转告塞给这个 agent 比丢一条严重得多（见 own_conv_id_hint 那次改绑）
        self.bridge._reader_dispatch({
            "type": "mail_response", "id": "x",
            "conversation_id": "bbbb2222", "items": ["别人的话"]})
        self.bridge._reader_dispatch({
            "type": "mail_response", "id": "y", "items": ["没写归属的话"]})
        with self._patch_send():
            got = self.bridge.fetch_mail("aaaa1111", sock=object(), timeout=0.2)
        self.assertEqual([], got)


class MailSuffixTests(unittest.TestCase):
    """取回来的信要拼进 zt 的回执里，否则 agent 根本看不到。"""

    def test_no_mail_adds_nothing(self):
        self.assertEqual("", server._mail_suffix([]))
        self.assertEqual("", server._mail_suffix(None))

    def test_mail_is_spelled_out_in_the_receipt(self):
        out = server._mail_suffix(["[agent·乙] hub.py 我要动了"])
        self.assertIn("hub.py 我要动了", out)
        self.assertIn("1", out)


if __name__ == "__main__":
    unittest.main()
