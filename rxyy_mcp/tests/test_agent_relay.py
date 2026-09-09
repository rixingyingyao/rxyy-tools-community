# -*- coding: utf-8 -*-
"""agent → agent 转告/广播（ji 借道）：目标解析与投递路由。

设计约定：对话ID前缀（≥6位）明确指名跨项目也认；名字/分工包含匹配先队友后全局；
「团队/广播」发同项目除自己外全部；找不到目标时把人话失败说明排队回发送方。
"""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub

WS1 = r"d:\Desktop\cursor工作流"
WS2 = r"c:\Users\Administrator\AICodebrain"


def _s(sid, conv, name, root, assign=""):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = sid, conv, name
    s.cwd = s.task_root = root
    s.connected = True
    s.pending = None
    s.queued = []
    s.lock = threading.Lock()
    s._assign = assign
    return s


class _RelayHarness(unittest.TestCase):
    def _run(self, sender, to, msg, sessions, tombstones=None, aliases=None):
        delivered = []

        def fake_queue(api_self, sid, text, imgs, who=None, files=None, force=False):
            delivered.append({"sid": sid, "text": text, "who": who, "force": force})
            return {"ok": True, "qid": "q"}

        d = {x.id: x for x in sessions}
        self.relay_log = []
        with (patch.object(hub.HUB, "sessions", d),
              patch.object(hub.HUB, "order", list(d)),
              patch.object(hub.HUB, "relay_log", self.relay_log),
              patch.object(hub.HUB, "name_tombstones", tombstones or {}),
              # 隔离真机的接手别名表：不打桩的话本机 takeover-aliases.json
              # 里的真实映射会参与解析，测试在别人机器上跑出另一个结果
              patch.object(hub.HUB, "takeover_aliases", aliases or {}),
              patch.object(hub.Hub, "_save_relays", lambda self: None),
              patch.object(hub.Api, "queue_message", fake_queue),
              patch.object(hub.Api, "session_label", lambda self, s: s.name),
              patch.object(hub.Api, "_team_role", lambda self, s: ""),
              patch.object(hub.Api, "_team_assign",
                           lambda self, s: getattr(s, "_assign", ""))):
            r = hub.Api().relay_from_agent(sender, to, msg)
        return r, delivered


class RelayTests(_RelayHarness):
    def test_resolve_by_conv_id_prefix(self):
        a = _s("s1", "aaaa1111", "甲", WS1)
        b = _s("s2", "bbbb2222", "乙", WS2)   # 跨项目，用 ID 指名也能送到
        r, sent = self._run(a, "bbbb22", "对接好了", [a, b])
        self.assertTrue(r["ok"])
        self.assertEqual(["s2"], [x["sid"] for x in sent])
        self.assertIn("对接好了", sent[0]["text"])
        self.assertIn("aaaa1111"[:8], sent[0]["text"])  # 回话线索带发送方 ID

    def test_resolve_by_name_within_team_first(self):
        a = _s("s1", "aaaa1111", "甲", WS1)
        mate = _s("s2", "bbbb2222", "账号库", WS1)
        stranger = _s("s3", "cccc3333", "账号库", WS2)  # 别的项目同名，不该抢
        r, sent = self._run(a, "账号库", "styles.css 我要动了", [a, mate, stranger])
        self.assertTrue(r["ok"])
        self.assertEqual(["s2"], [x["sid"] for x in sent])

    def test_resolve_by_assign_prefix(self):
        a = _s("s1", "aaaa1111", "甲", WS1)
        b = _s("s2", "bbbb2222", "乙", WS1, assign="手机页/分隔线样式")
        r, sent = self._run(a, "手机页", "分隔线样式我改了", [a, b])
        self.assertTrue(r["ok"])
        self.assertEqual(["s2"], [x["sid"] for x in sent])

    def test_assign_keyword_buried_in_the_middle_asks_back(self):
        # 前缀不算数的那半：分工写成「rxyy MCP 团队/手机页」时按「手机页」找人
        # 不再直接投——这几个字段现在常是派活自动写进去的一整句话
        a = _s("s1", "aaaa1111", "甲", WS1)
        b = _s("s2", "bbbb2222", "乙", WS1, assign="rxyy MCP 团队/手机页")
        r, sent = self._run(a, "手机页", "分隔线样式我改了", [a, b])
        self.assertFalse(r["ok"])
        self.assertIn("不替你拿主意", sent[0]["text"])
        self.assertIn("乙", sent[0]["text"])

    def test_broadcast_goes_to_all_teammates_except_sender(self):
        a = _s("s1", "aaaa1111", "甲", WS1)
        b = _s("s2", "bbbb2222", "乙", WS1)
        c = _s("s3", "cccc3333", "丙", WS1)
        outsider = _s("s4", "dddd4444", "丁", WS2)
        r, sent = self._run(a, "团队", "全量重打包要开始了", [a, b, c, outsider])
        self.assertTrue(r["ok"])
        self.assertEqual({"s2", "s3"}, {x["sid"] for x in sent})

    def test_unknown_target_notifies_sender_with_roster(self):
        a = _s("s1", "aaaa1111", "甲", WS1)
        b = _s("s2", "bbbb2222", "乙", WS1)
        r, sent = self._run(a, "不存在的人", "喂", [a, b])
        self.assertFalse(r["ok"])
        # 失败说明排队回发送方自己，且附同项目名册
        self.assertEqual(["s1"], [x["sid"] for x in sent])
        self.assertIn("找不到", sent[0]["text"])
        self.assertIn("乙", sent[0]["text"])

    def test_a_name_that_is_merely_contained_asks_back(self):
        # 派活自动命名之后 tab 名常是一整句话：发给「团队面板」不能就这么投给
        # 「继续完善团队面板功能，我让这个会话的agent去…」（08-04 用户报乱转发）
        a = _s("s1", "aaaa1111", "甲", WS1)
        b = _s("s2", "bbbb2222", "继续完善团队面板功能，我让这个会话的agent去…", WS1)
        r, sent = self._run(a, "团队面板", "面板那块我要动了", [a, b])
        self.assertFalse(r["ok"])
        self.assertEqual(["s1"], [x["sid"] for x in sent])   # 回问发送方
        self.assertIn("不替你拿主意", sent[0]["text"])
        self.assertIn("继续完善团队面板功能", sent[0]["text"])

    def test_the_same_sentence_in_every_field_does_not_reopen_the_loophole(self):
        # 派活自动命名把同一句话同时写进 tab 名和分工，改名时还把带项目前缀的
        # 整句塞进曾用名——三处都得一样严（08-04 实测：先漏在分工，再漏在曾用名）
        a = _s("s1", "aaaa1111", "甲", WS1)
        long_name = "不能退出雷神哈，我cursor在使用雷神。"
        b = _s("s2", "bbbb2222", long_name, WS1, assign=long_name)
        b.name_history = ["待命·cursor工作流", "cursor工作流·" + long_name]
        r, sent = self._run(a, "雷神", "我要重启 hub 了", [a, b])
        self.assertFalse(r["ok"])
        self.assertEqual(["s1"], [x["sid"] for x in sent])
        self.assertIn("不替你拿主意", sent[0]["text"])

    def test_an_exact_name_still_goes_straight_through(self):
        a = _s("s1", "aaaa1111", "甲", WS1)
        b = _s("s2", "bbbb2222", "团队面板修复", WS1)
        c = _s("s3", "cccc3333", "继续完善团队面板功能，我让这个会话的agent去…", WS1)
        r, sent = self._run(a, "团队面板修复", "面板那块我要动了", [a, b, c])
        self.assertTrue(r["ok"])
        self.assertEqual(["s2"], [x["sid"] for x in sent])

    def test_ambiguous_name_lists_candidates(self):
        a = _s("s1", "aaaa1111", "甲", WS1)
        b = _s("s2", "bbbb2222", "待命·乙", WS1)
        c = _s("s3", "cccc3333", "待命·丙", WS1)
        r, sent = self._run(a, "待命", "谁有空", [a, b, c])
        self.assertFalse(r["ok"])
        self.assertEqual(["s1"], [x["sid"] for x in sent])
        self.assertIn("对上了 2 个", sent[0]["text"])


class SelfTargetTests(_RelayHarness):
    """目标解析出来是发送方自己时要说真话——08-26 归并事故的第二半。

    现场：73004179 被误当空壳并进 b4eff2ee 后，10:09:59 它发现撞车、发转告想
    警告「b4eff2ee」，可它的调用已被别名路由成 b4eff2ee 本人——四步查找全带
    s.id != sender.id，回执落到兜底：「找不到 b4eff2ee。同项目在线的有：
    （没有队友）」。这句话把它往「那个 tab 不存在」上带，撞车警告就此蒸发。
    真话应当是：这个 ID 就是你所在的会话；若你以为它是别人，说明你们已被并进
    同一个 tab，上报用户。"""

    def test_relaying_to_your_own_conv_id_says_so(self):
        a = _s("s1", "b4eff2ee", "rxyy tools·全面体检", WS1)
        b = _s("s2", "bbbb2222", "乙", WS1)
        r, sent = self._run(a, "b4eff2ee", "别动 hub.py，我在改", [a, b])
        self.assertFalse(r["ok"])
        self.assertEqual(["s1"], [x["sid"] for x in sent])
        self.assertIn("你自己", sent[0]["text"])
        self.assertIn("并进同一个 tab", sent[0]["text"])
        self.assertNotIn("找不到", sent[0]["text"],
                         "「找不到」会把发送方往「目标不存在」上带——事故原话")

    def test_relaying_to_your_own_former_id_says_so(self):
        # 它想找的人是「自己挂着的那个壳 ID」：归并后 73004179 进了曾用 ID
        a = _s("s1", "b4eff2ee", "rxyy tools·全面体检", WS1)
        a.id_history = ["73004179"]
        b = _s("s2", "bbbb2222", "乙", WS1)
        r, sent = self._run(a, "73004179", "拦一下，我要动 hub.py", [a, b])
        self.assertFalse(r["ok"])
        self.assertEqual(["s1"], [x["sid"] for x in sent])
        self.assertIn("你自己", sent[0]["text"])
        self.assertIn("并进同一个 tab", sent[0]["text"])

    def test_relaying_to_your_own_tab_name_says_so(self):
        a = _s("s1", "b4eff2ee", "rxyy tools·全面体检", WS1)
        b = _s("s2", "bbbb2222", "乙", WS1)
        r, sent = self._run(a, "rxyy tools·全面体检", "在吗", [a, b])
        self.assertFalse(r["ok"])
        self.assertEqual(["s1"], [x["sid"] for x in sent])
        self.assertIn("你自己", sent[0]["text"])

    def test_someone_elses_former_id_still_gets_forwarded(self):
        # 护栏：别人的曾用 ID 照旧代投，不许被自指判定劫走
        a = _s("s1", "aaaa1111", "甲", WS1)
        b = _s("s2", "bbbb2222", "乙", WS1)
        b.id_history = ["73004179"]
        r, sent = self._run(a, "73004179", "对接好了", [a, b])
        self.assertTrue(r["ok"])
        # 第一条投给现任；第二条是回发送方的「已代投·以后用新 ID」附言
        self.assertEqual("s2", sent[0]["sid"])
        self.assertIn("对接好了", sent[0]["text"])


class AliasRoutingTests(_RelayHarness):
    """退休 ID 的转告路由要跟权威的接手别名表走，不能跟误归并留下的影子走。

    08-26 拆分手术的机制半边：73004179 被误归并进 b4eff2ee（id_history 留下
    影子），用户把那条活改派到新会话 9e528993 后，把别名改指 9e528993 必须
    让全部路由跟着走——否则队友按旧 ID 递话仍落进 b4eff2ee，串台复发。"""

    def test_retired_id_follows_takeover_alias_over_id_history(self):
        a = _s("s1", "aaaa1111", "甲", WS1)
        wrong = _s("s2", "b4eff2ee", "rxyy tools·全面体检", WS1)
        wrong.id_history = ["73004179"]          # 误归并留下的影子
        right = _s("s3", "9e528993", "录播播出端·切换器联动·8993", WS1)
        r, sent = self._run(a, "73004179", "切换器那边好了", [a, wrong, right],
                            aliases={"73004179": "9e528993"})
        self.assertTrue(r["ok"])
        self.assertEqual("s3", sent[0]["sid"],
                         "别名表已改指现任，不能再按 id_history 投给误归并的 tab")
        self.assertIn("切换器那边好了", sent[0]["text"])

    def test_alias_pointing_at_sender_says_self(self):
        # 现任自己按旧 ID 找人：别名解析到自己 → 说真话
        me = _s("s1", "9e528993", "录播播出端·切换器联动·8993", WS1)
        b = _s("s2", "bbbb2222", "乙", WS1)
        r, sent = self._run(me, "73004179", "在吗", [me, b],
                            aliases={"73004179": "9e528993"})
        self.assertFalse(r["ok"])
        self.assertEqual(["s1"], [x["sid"] for x in sent])
        self.assertIn("你自己", sent[0]["text"])


class DeadTargetTests(_RelayHarness):
    """已终止但还在列表里的 tab 也收话（07-31 用户实测「cursor工作流1 明明在
    列表里却说找不到」）：排队寄存等接手者，且给发送方一条寄存说明。"""

    def test_dead_listed_target_gets_parked_message(self):
        a = _s("s1", "aaaa1111", "甲", WS1)
        dead = _s("s2", "bbbb2222", "cursor工作流1", WS1)
        dead.connected = False
        dead.recon_deadline = 0
        r, sent = self._run(a, "cursor工作流1", "退款那块我要动了", [a, dead])
        self.assertTrue(r["ok"])
        self.assertEqual(["cursor工作流1"], r.get("parked"))
        by_sid = {x["sid"]: x for x in sent}
        self.assertTrue(by_sid["s2"]["force"])          # 死 tab 强制入队
        self.assertIn("退款那块", by_sid["s2"]["text"])
        self.assertIn("转告寄存", by_sid["s1"]["text"])  # 发送方知道话被寄存了

    def test_parked_wording_follows_fusion_liveness(self):
        # 08-13 rxyy 实测被误导：回执说「已终止」，面板蓝点却说 IDE 里还活着。
        # 措辞跟判活融合同一口径：断开 ≠ 死
        a = _s("s1", "aaaa1111", "甲", WS1)
        napping = _s("s2", "bbbb2222", "根治筹备", WS1)
        napping.connected = False
        napping.recon_deadline = 0
        napping.live_cache = {"state": "ide", "label": "Cursor 里还活着·通道未接"}
        r, sent = self._run(a, "根治筹备", "审查请求", [a, napping])
        self.assertTrue(r["ok"])
        receipt = {x["sid"]: x for x in sent}["s1"]["text"]
        self.assertIn("IDE 里可能还活着", receipt)
        self.assertNotIn("已终止", receipt)

    def test_parked_wording_still_says_dead_when_fusion_agrees(self):
        a = _s("s1", "aaaa1111", "甲", WS1)
        corpse = _s("s2", "bbbb2222", "凉透的", WS1)
        corpse.connected = False
        corpse.recon_deadline = 0
        corpse.live_cache = {"state": "dead", "label": "已终止"}
        r, sent = self._run(a, "凉透的", "喂", [a, corpse])
        self.assertTrue(r["ok"])
        receipt = {x["sid"]: x for x in sent}["s1"]["text"]
        self.assertIn("已终止", receipt)

    def test_tombstone_points_to_successor(self):
        a = _s("s1", "aaaa1111", "甲", WS1)
        b = _s("s2", "bbbb2222", "渲染升级", WS1)
        tomb = {"待命·105628d8": {"ts": 1.0, "succ_label": "渲染升级",
                                  "succ8": "bbbb2222"[:8]}}
        r, sent = self._run(a, "待命·105628d8", "喂", [a, b], tombstones=tomb)
        self.assertFalse(r["ok"])
        self.assertEqual(["s1"], [x["sid"] for x in sent])
        self.assertIn("壳已被收起", sent[0]["text"])
        self.assertIn("渲染升级", sent[0]["text"])

    def test_former_name_still_reaches_renamed_agent(self):
        # 分工一改标签就变（渲染升级→推送排障，07-31 实测扑空）：曾用名也要认
        a = _s("s1", "aaaa1111", "甲", WS1)
        b = _s("s2", "bbbb2222", "推送排障", WS1)
        b.name_history = ["待命·105628d8", "渲染升级"]
        r, sent = self._run(a, "渲染升级", "hub 我重启了", [a, b])
        self.assertTrue(r["ok"])
        self.assertEqual(["s2"], [x["sid"] for x in sent])


class ReplySlotTests(unittest.TestCase):
    """tab 正阻塞在 zhi 等用户回话时，谁有资格占掉那个「回复位」。

    只有用户本人够格。队友的转告和控制台回执一抢答，tab 明明在等用户拍板，
    agent 收到的却是队友的话，用户那条反而被挤掉（08-04 实测两次，agent 只好
    把汇报重发一遍）。不抢答也不会丢：队列在对方下一次 zhi 时照常送达。
    """

    def setUp(self):
        s = self.s = _s("s1", "aaaa1111", "甲", WS1)
        s.pending = {"id": "q1", "message": "三处改完要重启，怎么干？"}
        s.messages, s.msg_seq, s.rev = [], 0, 0
        s.recon_deadline = None
        s.ide_active_cache = False
        self.replied = []
        ps = [patch.object(hub.HUB, "sessions", {s.id: s}),
              patch.object(hub.HUB, "cfg", {"max_messages": 200}),
              patch.object(hub.Hub, "save_msg_images", lambda h, imgs: []),
              patch.object(hub.Api, "_auto_label_on_dispatch",
                           lambda api, sess, text, who=None: None),
              patch.object(hub.Api, "send_reply",
                           lambda api, sid, text, sel, imgs, cont, who=None, files=None:
                               (self.replied.append((sid, who)), {"ok": True})[1])]
        for p in ps:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in ps])

    def _queue(self, who):
        return hub.Api().queue_message("s1", "hub.py 我要动了", [], who=who)

    def test_a_teammates_relay_waits_its_turn(self):
        r = self._queue("agent·乙")
        self.assertTrue(r["ok"])
        self.assertEqual([], self.replied)          # 没占掉用户的回复位
        self.assertEqual(1, len(self.s.queued))     # 但也没丢，排着
        self.assertIsNotNone(self.s.pending)        # 用户那条提问还等着

    def test_console_receipt_waits_its_turn(self):
        self._queue("控制台")
        self.assertEqual([], self.replied)
        self.assertEqual(1, len(self.s.queued))

    def test_the_user_still_answers_right_away(self):
        self._queue(None)
        self.assertEqual([("s1", None)], self.replied)

    def test_a_colleague_on_the_phone_still_answers_right_away(self):
        self._queue("张垒")
        self.assertEqual([("s1", "张垒")], self.replied)

    def test_panel_broadcast_is_the_user_talking_too(self):
        # 面板广播/送审是用户本人在操作，照旧当场送达
        self._queue("团队面板")
        self.assertEqual([("s1", "团队面板")], self.replied)


class QueuedRelayDeliveryTests(unittest.TestCase):
    """排着的转告什么时候才出去：等用户真回话，被那一答捎带出去。

    只堵「转告到达时不抢答」是堵了一半——agent 下一次 zhi 一提问，_flush_queue
    立刻拿队列里的转告把这次提问「答」了，用户照样没机会看见（08-04 实测一轮连中
    两次）。所以提问那头也要认：队列里全是转告/回执时不算数。
    """

    def setUp(self):
        s = self.s = _s("s1", "aaaa1111", "甲", WS1)
        s.messages, s.msg_seq, s.rev = [], 0, 0
        s.detached = False
        s.library_sent = True
        s.processing_since = None
        s.last_reply_probe = None
        self.sent = []
        s.send = lambda payload: self.sent.append(payload)
        ps = [patch.object(hub.HUB, "sessions", {s.id: s}),
              patch.object(hub.HUB, "cfg", {"max_messages": 200}),
              patch.object(hub.Hub, "save_msg_images", lambda h, imgs: []),
              patch.object(hub.Hub, "log_user",
                           lambda h, *a, **k: None),
              patch.object(hub.Api, "_auto_label_on_dispatch",
                           lambda api, sess, text, who=None: None)]
        for p in ps:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in ps])

    def _relay(self, text="hub.py 我要动了"):
        return {"id": "q1", "text": text, "images": [], "files": [],
                "who": "agent·乙", "msg": {}}

    def test_a_new_question_is_not_answered_by_a_queued_relay(self):
        self.s.pending = {"id": "req1", "message": "三处改完要重启，怎么干？"}
        self.s.queued = [self._relay()]
        self.assertFalse(hub.HUB._flush_queue(self.s))
        self.assertIsNotNone(self.s.pending)        # 提问还挂着，等用户
        self.assertEqual(1, len(self.s.queued))

    def test_a_queued_user_message_still_answers_and_takes_the_relay_along(self):
        self.s.pending = {"id": "req1", "message": "?"}
        self.s.queued = [self._relay(), {"id": "q2", "text": "先测再改", "images": [],
                                         "files": [], "who": None, "msg": {}}]
        self.assertTrue(hub.HUB._flush_queue(self.s))
        self.assertEqual(1, len(self.sent))
        body = self.sent[0]["user_input"]
        self.assertIn("先测再改", body)
        self.assertIn("hub.py 我要动了", body)      # 用户开口了，转告顺路一起走
        self.assertEqual([], self.s.queued)

    def test_the_users_answer_carries_the_queued_relay_along(self):
        self.s.pending = {"id": "req1", "message": "?"}
        self.s.queued = [self._relay()]
        self.s.messages = [{"role": "user", "qid": "q1", "queued": True, "html": "x"}]
        r = hub.Api().send_reply("s1", "按方案二来", [], [], False)
        self.assertTrue(r["ok"])
        body = self.sent[0]["user_input"]
        self.assertIn("按方案二来", body)
        self.assertIn("[agent·乙] hub.py 我要动了", body)
        self.assertEqual([], self.s.queued)
        self.assertFalse(self.s.messages[0]["queued"])  # 气泡不再显示「排队中」


class RelayLogTests(_RelayHarness):
    """每笔转告（成/败）都进 HUB.relay_log，团队面板「队内传话」由它供数。"""

    def test_success_is_logged_with_target_labels(self):
        a = _s("s1", "aaaa1111", "甲", WS1)
        b = _s("s2", "bbbb2222", "乙", WS1)
        self._run(a, "乙", "对接好了", [a, b])
        self.assertEqual(1, len(self.relay_log))
        e = self.relay_log[0]
        self.assertTrue(e["ok"])
        self.assertEqual("甲", e["from_label"])
        self.assertEqual(["乙"], e["to_labels"])
        self.assertIn("对接好了", e["text"])

    def test_failure_is_logged_with_reason(self):
        a = _s("s1", "aaaa1111", "甲", WS1)
        b = _s("s2", "bbbb2222", "乙", WS1)
        self._run(a, "不存在的人", "喂", [a, b])
        self.assertEqual(1, len(self.relay_log))
        e = self.relay_log[0]
        self.assertFalse(e["ok"])
        self.assertIn("找不到", e["note"])

    def test_broadcast_logged_as_one_entry(self):
        a = _s("s1", "aaaa1111", "甲", WS1)
        b = _s("s2", "bbbb2222", "乙", WS1)
        c = _s("s3", "cccc3333", "丙", WS1)
        self._run(a, "广播", "都停一下", [a, b, c])
        self.assertEqual(1, len(self.relay_log))
        e = self.relay_log[0]
        self.assertTrue(e["ok"])
        self.assertEqual("广播", e["kind"])
        self.assertEqual({"乙", "丙"}, set(e["to_labels"]))

    def test_relay_log_survives_restart(self):
        # 传话记录落盘：hub 重启后团队面板「队内传话」区不再清空（07-31 用户刚
        # 看到传话区就被十几次重启清了个干净）
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "relays.json"
            entries = [{"ts": 1.0, "hms": "22:17:00", "kind": "转告",
                        "from8": "aaaa1111", "from_label": "甲", "to": "乙",
                        "to_labels": ["乙"], "text": "部署通知", "ok": True,
                        "note": ""}]
            with (patch.object(hub.Hub, "RELAYS_PATH", p),
                  patch.object(hub.HUB, "relay_log", entries)):
                hub.HUB._save_relays()
                loaded = hub.HUB._load_relays()
        self.assertEqual(1, len(loaded))
        self.assertEqual("部署通知", loaded[0]["text"])
        self.assertTrue(loaded[0]["ok"])


if __name__ == "__main__":
    unittest.main()
