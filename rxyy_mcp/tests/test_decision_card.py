# -*- coding: utf-8 -*-
"""决策卡片：zhi 的 card 参数（对标 BajieAsk wait_message 的 card）。

predefined_options 是一维字符串、单选、≤4 条；09-01 rxyy 拍板把 BajieAsk 的决策卡
搬进rxyy MCP：多问 + 每项 detail + recommended + 自由输入 + 倒计时代决。
这里锁：规范化（随手传也收）、气泡/记录文件的静态呈现、答复文本（人读一行 +
机器标签）、hub 侧存进 pending / get_state 外露、answer_card 走 send_reply 老路、
服务端倒计时到点代决、server 侧把 card 传进 BRIDGE.ask。
"""
import json
import re
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import decision_card as dc  # noqa: E402
import hub  # noqa: E402
import server  # noqa: E402

WS = r"d:\桌面\working\cursor工作流"

FULL = {
    "id": "deploy-now",
    "title": "模型牌修复怎么上线",
    "questions": [
        {"id": "when", "prompt": "何时重启 hub",
         "options": [
             {"id": "now", "label": "现在重启", "detail": "10 秒断一次；3 个 agent 在飞 zhi 原参重发", "recommended": True},
             {"id": "later", "label": "等他们收工", "detail": "牌继续错到晚上"}],
         "multiple": False, "allowFreeText": True},
        {"id": "extra", "prompt": "顺带做哪些",
         "options": [{"id": "title", "label": "read_cursor_title 也改 ro"},
                     {"id": "activity", "label": "activity 也改 ro"}],
         "multiple": True, "allowFreeText": False},
    ],
    "autoDecide": {"enabled": True, "onResolve": "adopt", "timeoutSec": 120},
}


class NormalizeTests(unittest.TestCase):
    def test_full_card_round_trips(self):
        c = dc.normalize_card(FULL)
        self.assertEqual("deploy-now", c["id"])
        self.assertEqual(2, len(c["questions"]))
        q1 = c["questions"][0]
        self.assertEqual(("when", "何时重启 hub", False, True),
                         (q1["id"], q1["prompt"], q1["multiple"], q1["allowFreeText"]))
        self.assertTrue(q1["options"][0]["recommended"])
        self.assertIn("原参重发", q1["options"][0]["detail"])
        self.assertTrue(c["questions"][1]["multiple"])
        self.assertEqual({"enabled": True, "onResolve": "adopt", "timeoutSec": 120}, c["autoDecide"])

    def test_the_old_flat_format_becomes_a_single_question(self):
        c = dc.normalize_card({"id": "x", "title": "先做哪个", "multiple": True,
                               "options": [{"label": "A"}, {"label": "B", "recommended": True}]})
        self.assertEqual(1, len(c["questions"]))
        self.assertEqual("先做哪个", c["questions"][0]["prompt"])
        self.assertTrue(c["questions"][0]["multiple"])
        self.assertEqual(["A", "B"], [o["label"] for o in c["questions"][0]["options"]])

    def test_plain_string_options_and_json_string_input_are_accepted(self):
        c = dc.normalize_card(json.dumps({"title": "t", "questions": [
            {"prompt": "p", "options": ["甲", "乙"]}]}, ensure_ascii=False))
        self.assertEqual(["opt1", "opt2"], [o["id"] for o in c["questions"][0]["options"]])
        self.assertEqual(["甲", "乙"], [o["label"] for o in c["questions"][0]["options"]])
        self.assertEqual("q1", c["questions"][0]["id"])

    def test_garbage_is_none_not_an_exception(self):
        for bad in (None, "", "not json", 42, [], {}, {"questions": []},
                    {"questions": [{"prompt": "p", "options": [], "allowFreeText": False}]}):
            self.assertIsNone(dc.normalize_card(bad), repr(bad))

    def test_a_question_with_no_options_but_free_text_is_still_a_question(self):
        c = dc.normalize_card({"questions": [{"prompt": "叫什么名字", "options": []}]})
        self.assertEqual([], c["questions"][0]["options"])
        self.assertTrue(c["questions"][0]["allowFreeText"])

    def test_missing_id_gets_a_stable_one(self):
        a = dc.normalize_card({"title": "t", "questions": [{"prompt": "p", "options": ["甲"]}]})
        b = dc.normalize_card({"title": "t", "questions": [{"prompt": "p", "options": ["甲"]}]})
        self.assertEqual(a["id"], b["id"])
        self.assertTrue(a["id"].startswith("card-"))

    def test_limits_are_clamped(self):
        raw = {"title": "T" * 500, "questions": [
            {"prompt": "p", "options": [{"label": "o%d" % i} for i in range(20)]}
            for _ in range(10)]}
        c = dc.normalize_card(raw)
        self.assertEqual(dc.MAX_QUESTIONS, len(c["questions"]))
        self.assertEqual(dc.MAX_OPTIONS, len(c["questions"][0]["options"]))
        self.assertEqual(dc.TITLE_MAX, len(c["title"]))

    def test_duplicate_option_ids_are_disambiguated(self):
        c = dc.normalize_card({"questions": [{"prompt": "p", "options": [
            {"id": "a", "label": "甲"}, {"id": "a", "label": "乙"}]}]})
        ids = [o["id"] for o in c["questions"][0]["options"]]
        self.assertEqual(2, len(set(ids)))

    def test_auto_decide_needs_a_timeout_and_a_recommended_option(self):
        no_timeout = dict(FULL, autoDecide={"enabled": True, "onResolve": "adopt"})
        self.assertFalse(dc.normalize_card(no_timeout)["autoDecide"]["enabled"])
        no_rec = json.loads(json.dumps(FULL))
        for q in no_rec["questions"]:
            for o in q["options"]:
                o["recommended"] = False
        self.assertFalse(dc.normalize_card(no_rec)["autoDecide"]["enabled"],
                         "一个推荐项都没有还开倒计时 = 到点空答")
        delegate = dict(no_rec, autoDecide={"enabled": True, "onResolve": "delegate", "timeoutSec": 30})
        self.assertTrue(dc.normalize_card(delegate)["autoDecide"]["enabled"],
                        "交回 AI 不需要推荐项")

    def test_unknown_on_resolve_falls_back_to_adopt_and_timeout_is_capped(self):
        c = dc.normalize_card(dict(FULL, autoDecide={"enabled": True, "onResolve": "whatever",
                                                     "timeoutSec": 99999}))
        self.assertEqual("adopt", c["autoDecide"]["onResolve"])
        self.assertEqual(dc.AUTO_TIMEOUT_MAX, c["autoDecide"]["timeoutSec"])


class RenderTests(unittest.TestCase):
    def test_text_lists_every_question_option_and_detail(self):
        t = dc.card_to_text(dc.normalize_card(FULL))
        self.assertIn("【决策卡片】模型牌修复怎么上线", t)
        self.assertIn("1. 何时重启 hub", t)
        self.assertIn("- 现在重启（推荐）：10 秒断一次", t)
        self.assertIn("2. 顺带做哪些（可多选）", t)
        self.assertIn("其他…（自由输入）", t)
        self.assertIn("120 秒无人作答则采纳推荐项", t)

    def test_html_escapes_and_marks_recommended(self):
        c = dc.normalize_card({"title": "<b>x</b>", "questions": [
            {"prompt": "p<script>", "options": [{"label": "a&b", "detail": "<i>d</i>", "recommended": True}]}]})
        h = dc.card_to_html(c)
        self.assertNotIn("<script>", h)
        self.assertIn("&lt;script&gt;", h)
        self.assertIn("a&amp;b", h)
        self.assertIn("&lt;i&gt;d&lt;/i&gt;", h)
        self.assertIn('class="dcard-rec"', h)

    def test_empty_card_renders_nothing(self):
        self.assertEqual("", dc.card_to_text(None))
        self.assertEqual("", dc.card_to_html(None))


class ReplyTests(unittest.TestCase):
    def setUp(self):
        self.card = dc.normalize_card(FULL)

    def _tag(self, text):
        m = re.search(r"<chijiu-decision answer='(.*?)'/>", text, re.S)
        self.assertIsNotNone(m, text)
        return json.loads(m.group(1).replace("&#39;", "'"))

    def test_manual_answer_has_a_human_line_and_a_machine_tag(self):
        text, labels = dc.decision_reply(self.card, {
            "when": {"selected": ["later"], "text": "等到 18:00"},
            "extra": {"selected": ["title", "activity"]}})
        self.assertTrue(text.startswith("[决策答复] 何时重启 hub：等他们收工、等到 18:00；顺带做哪些："))
        self.assertIn("read_cursor_title 也改 ro、activity 也改 ro", text)
        self.assertEqual(["等他们收工", "read_cursor_title 也改 ro", "activity 也改 ro"], labels)
        tag = self._tag(text)
        self.assertEqual(("deploy-now", "manual"), (tag["requestId"], tag["mode"]))
        self.assertEqual({"questionId": "when", "optionIds": ["later"], "labels": ["等他们收工"],
                          "freeText": "等到 18:00"}, tag["items"][0])

    def test_reply_renders_as_a_card_and_hides_the_machine_tag(self):
        # 09-07 rxyy 截图：<chijiu-decision answer='{…}'/> 一行 JSON 原样糊在用户气泡里
        text, _ = dc.decision_reply(self.card, {
            "when": {"selected": ["later"], "text": "等到 18:00"},
            "extra": {"selected": ["title", "activity"]}})
        h = dc.reply_to_html(text)
        self.assertIsNotNone(h)
        self.assertNotIn("chijiu-decision", h, "机器标签只给 agent，人看卡")
        self.assertIn('<span class="dc-reply-tag">决策答复</span>', h)
        self.assertIn('<span class="dc-reply-mode">手动回答</span>', h)
        self.assertIn('<span class="dc-reply-n">提问 1</span>何时重启 hub', h)
        self.assertIn('<span class="dc-reply-pick">✓ 等他们收工</span>', h)
        self.assertIn('<span class="dc-reply-pick">✓ 等到 18:00</span>', h, "自由输入也算一个回答")
        self.assertIn('<span class="dc-reply-n">提问 2</span>顺带做哪些', h)
        self.assertIn("✓ read_cursor_title 也改 ro", h)
        self.assertIn("✓ activity 也改 ro", h)
        # 预览 / 记录里用的纯文本：摘掉标签、保留人话
        plain = dc.strip_machine_tag(text)
        self.assertTrue(plain.startswith("[决策答复] 何时重启 hub："))
        self.assertNotIn("<chijiu-decision", plain)
        # 不是决策答复的普通消息不碰
        self.assertIsNone(dc.reply_to_html("你好，继续"))
        # 答案里自带「：」「；」不会切错：答案从机器标签取
        text2, _ = dc.decision_reply(self.card, {"when": {"selected": [], "text": "改成：先 A；再 B"}})
        h2 = dc.reply_to_html(text2)
        self.assertIn("✓ 改成：先 A；再 B", h2)
        self.assertNotIn("&lt;", dc.reply_to_html("[决策答复] 问：答"), "退化路径也能渲")

    def test_adopt_recommended_fills_unanswered_questions_with_the_recommended_option(self):
        text, labels = dc.decision_reply(self.card, {}, "adopt_recommended")
        self.assertIn("何时重启 hub：现在重启", text)
        self.assertIn("顺带做哪些：（未选）", text, "没有推荐项的问题就是没选")
        self.assertEqual(["现在重启"], labels)
        self.assertEqual("adopt_recommended", self._tag(text)["mode"])

    def test_delegate_says_so_instead_of_leaving_blanks(self):
        text, labels = dc.decision_reply(self.card, {}, "delegate_system")
        self.assertIn("（交给 AI 自定）", text)
        self.assertEqual([], labels)

    def test_unknown_option_ids_and_junk_answers_are_ignored(self):
        text, labels = dc.decision_reply(self.card, {"when": {"selected": ["nope"]}, "extra": "junk"})
        self.assertIn("何时重启 hub：（未选）", text)
        self.assertEqual([], labels)

    def test_a_quote_in_free_text_does_not_break_the_tag(self):
        text, _ = dc.decision_reply(self.card, {"when": {"selected": [], "text": "it's fine"}})
        self.assertEqual("it's fine", self._tag(text)["items"][0]["freeText"])


class _Client:
    def __init__(self):
        self.closed_convs, self.sessions, self.cwd, self.pid = {}, {}, WS, 111
        self.last_heartbeat = 0
        self.sent = []

    def send(self, obj):
        self.sent.append(obj)


def _sess(sid, conv, name):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = sid, conv, name
    s.cwd = s.task_root = WS
    s.connected = True
    s.archived = False
    s.pending = None
    s.processing_since = None
    s.detached = False
    s.detached_since = None
    s.buffered_reply = None
    s.last_reply_probe = None
    s.agent_status = ""
    s.agent_activity = ""
    s.agent_status_ts = 0
    s.last_reply_ts = 0
    s.last_heartbeat = 0
    s.last_zhi_ts = 0
    s.queued = []
    s.messages = []
    s.msg_seq = 0
    s.machine_seq = 0
    s.real_seq = 0
    s.rev = 0
    s.file_path = None
    s.lock = threading.Lock()
    return s


class _ImmediateThread:
    def __init__(self, target=None, daemon=None, **kwargs):
        self._target = target

    def start(self):
        if self._target:
            self._target()


class HubCardTests(unittest.TestCase):
    def setUp(self):
        self.client = _Client()
        self.s = _sess("t1", "c1", "rxyy MCP·决策卡")
        self.s.client = self.client
        self.client.sessions["c1"] = self.s
        self.logged = []
        ps = [
            patch.object(hub.HUB, "sessions", {"t1": self.s}),
            patch.object(hub.HUB, "order", ["t1"]),
            patch.object(hub.HUB, "cfg", {"max_messages": 200}),
            patch.object(hub.HUB, "resolve_session", lambda cli, ck, tn: self.s),
            patch.object(hub.HUB, "_verify_identity_by_generating", MagicMock()),
            patch.object(hub.HUB, "_reap_takeover_shell", MagicMock()),
            # 实例属性上打补丁 = 不绑定 self，形参从 session 起
            patch.object(hub.HUB, "log_ai", lambda s, m, o, a=None: self.logged.append(m)),
            patch.object(hub.HUB, "log_user", MagicMock()),
            patch.object(hub.HUB, "notify", MagicMock()),
            patch.object(hub.HUB, "wake_window", MagicMock()),
            patch.object(hub.HUB, "flash_taskbar", MagicMock()),
            patch.object(hub.HUB, "push_phone", MagicMock()),
            patch.object(hub.Api, "_nudge_rename_if_standby", MagicMock()),
            patch.object(hub.Api, "_with_library_digest",
                         lambda api, s, ui, selected=None: (ui, False)),
            patch.object(hub.Api, "_with_session_memory",
                         lambda api, s, ui, selected=None: ui),
            patch.object(hub, "log_event", lambda *a, **k: None),
            patch.object(hub.threading, "Thread", _ImmediateThread),
        ]
        for p in ps:
            p.start()
        self.addCleanup(patch.stopall)

    def _ask(self, card=FULL, rpc_id="rpc-1"):
        hub.HUB._handle_client_msg(self.client, None, {
            "type": "zhi_request", "id": rpc_id, "conversation_id": "c1",
            "message": "改完了，怎么上线？", "predefined_options": [],
            "is_markdown": True, "card": card,
        })

    def _responses(self):
        return [m for m in self.client.sent if m.get("type") == "zhi_response"]

    def test_the_card_lands_on_pending_the_bubble_and_the_record_file(self):
        self._ask()
        self.assertEqual("deploy-now", self.s.pending["card"]["id"])
        ai = [m for m in self.s.messages if m.get("role") == "ai"][-1]
        self.assertIn('class="dcard"', ai["html"])
        self.assertIn("现在重启", ai["html"])
        self.assertEqual("deploy-now", ai["card"]["id"])
        self.assertIn("【决策卡片】模型牌修复怎么上线", self.logged[-1], "记录文件也得看得到卡")

    def test_the_deadline_for_the_ui_is_created_plus_timeout(self):
        # get_state 把它连同 card 一起外露；界面只画倒计时，到点由 hub 代答
        self._ask()
        self.assertAlmostEqual(self.s.pending["created"] + 120,
                               hub.Api._card_deadline(self.s.pending), delta=0.5)

    def test_a_card_without_auto_decide_has_no_deadline(self):
        raw = json.loads(json.dumps(FULL))
        raw["autoDecide"] = {"enabled": False}
        self._ask(raw)
        self.assertIsNone(hub.Api._card_deadline(self.s.pending))
        self.assertIsNone(hub.Api._card_deadline(None))
        self.assertIsNone(hub.Api._card_deadline({"message": "无卡"}))

    def test_garbage_card_is_just_no_card(self):
        self._ask(card="not a card")
        self.assertIsNone(self.s.pending["card"])
        self.assertNotIn("dcard", [m for m in self.s.messages if m.get("role") == "ai"][-1]["html"])

    def test_answer_card_sends_the_decision_text_and_labels_back(self):
        self._ask()
        r = hub.Api().answer_card("t1", {"when": {"selected": ["later"], "text": "18:00 再说"},
                                         "extra": {"selected": ["title"]}}, "manual")
        self.assertTrue(r.get("ok"), r)
        resp = self._responses()[-1]
        self.assertEqual("rpc-1", resp["id"])
        self.assertIn("[决策答复] 何时重启 hub：等他们收工、18:00 再说", resp["user_input"])
        self.assertIn("<chijiu-decision", resp["user_input"])
        self.assertEqual(["等他们收工", "read_cursor_title 也改 ro"], resp["selected_options"])
        self.assertIsNone(self.s.pending, "答完卡就收")

    def test_who_from_the_phone_rides_along_to_send_reply(self):
        # 手机分享页答卡带答题人，与 /api/reply 同口径；控制台自己答的 who 为 None
        self._ask()
        with patch.object(hub.Api, "send_reply", return_value={"ok": True}) as sr:
            hub.Api().answer_card("t1", {"when": {"selected": ["now"]}}, "manual", who="rxyy")
        self.assertEqual("rxyy", sr.call_args[0][5])
        self.assertEqual(["现在重启"], sr.call_args[0][2])

    def test_answer_card_without_a_card_refuses(self):
        hub.HUB._handle_client_msg(self.client, None, {
            "type": "zhi_request", "id": "rpc-9", "conversation_id": "c1",
            "message": "普通提问", "predefined_options": ["A", "B"], "is_markdown": True})
        r = hub.Api().answer_card("t1", {}, "manual")
        self.assertFalse(r["ok"])
        self.assertIsNotNone(self.s.pending, "不能把普通提问的 pending 吃掉")

    def test_the_tick_adopts_the_recommended_option_when_the_clock_runs_out(self):
        self._ask()
        created = self.s.pending["created"]
        hub.HUB._tick_card_auto_decide(self.s, created + 119)
        self.assertEqual([], self._responses(), "没到点不许替用户答")
        hub.HUB._tick_card_auto_decide(self.s, created + 121)
        resp = self._responses()[-1]
        self.assertIn("何时重启 hub：现在重启", resp["user_input"])
        self.assertIn("adopt_recommended", resp["user_input"])
        self.assertIsNone(self.s.pending)

    def test_the_tick_delegates_when_asked_to(self):
        raw = json.loads(json.dumps(FULL))
        raw["autoDecide"] = {"enabled": True, "onResolve": "delegate", "timeoutSec": 10}
        self._ask(raw)
        hub.HUB._tick_card_auto_decide(self.s, self.s.pending["created"] + 11)
        self.assertIn("（交给 AI 自定）", self._responses()[-1]["user_input"])

    def test_the_tick_leaves_a_manual_card_alone(self):
        raw = json.loads(json.dumps(FULL))
        raw["autoDecide"] = {"enabled": False}
        self._ask(raw)
        hub.HUB._tick_card_auto_decide(self.s, self.s.pending["created"] + 99999)
        self.assertEqual([], self._responses())
        self.assertIsNotNone(self.s.pending)

    def test_a_failed_auto_answer_disarms_instead_of_retrying_every_second(self):
        self._ask()
        created = self.s.pending["created"]
        with patch.object(hub.Api, "answer_card", lambda *a, **k: {"ok": False, "error": "断了"}):
            hub.HUB._tick_card_auto_decide(self.s, created + 121)
        self.assertFalse(self.s.pending["card"]["autoDecide"]["enabled"])


class ServerCardTests(unittest.TestCase):
    """server 侧：card 规范化后随 zhi 请求进 BRIDGE.ask；传垃圾等于没传。"""

    def _call(self, card):
        asked = []

        def fake_ask(*a, **kw):
            asked.append(kw)
            return {}

        with patch.object(server.BRIDGE, "ask", fake_ask), \
                patch.object(server.BRIDGE, "_schedule_idle_clear", lambda *_a: None), \
                patch.object(server, "wait_if_frozen", lambda: None), \
                patch.object(server, "DISABLED", False):
            server.tool_zhi({"message": "怎么上线？", "conversation_id": "b4eff2ee", "card": card})
        return asked[0].get("card")

    def test_a_card_rides_along_normalized(self):
        c = self._call(FULL)
        self.assertEqual("deploy-now", c["id"])
        self.assertEqual(2, len(c["questions"]))

    def test_a_json_string_card_is_parsed(self):
        self.assertEqual("deploy-now", self._call(json.dumps(FULL))["id"])

    def test_garbage_means_no_card(self):
        self.assertIsNone(self._call("nope"))
        self.assertIsNone(self._call({"questions": []}))

    def test_the_schema_advertises_the_card(self):
        zhi = next(t for t in server.TOOLS if t["name"] == "zhi")
        self.assertIn("card", zhi["inputSchema"]["properties"])
        self.assertIn("chijiu-decision", zhi["inputSchema"]["properties"]["card"]["description"])


class PhoneCardRouteTests(unittest.TestCase):
    """手机分享页：/api/card 把 answers / mode / 答题人原样交给 Api.answer_card。

    以前手机上只有 predefined_options 那排按钮；卡片提问的 options 是空的，不开这条路
    手机端就只剩自由输入，agent 拿不到结构化答复。
    """
    PORT = 39189
    TOKEN = "t0k3n"
    srv = None

    @classmethod
    def setUpClass(cls):
        import tempfile
        import share_server as ss
        cls.tmp = tempfile.TemporaryDirectory()
        (Path(cls.tmp.name) / "图片").mkdir(parents=True, exist_ok=True)
        cls.calls = calls = []

        class FakeHub:
            cfg = {"share_port": cls.PORT, "share_token": cls.TOKEN, "share_enabled": True,
                   "lan_first": False, "history_dir": cls.tmp.name}

        class FakeApi:
            def get_state(self):
                return {"sessions": [{"id": "s1", "name": "x", "connected": True,
                                      "pending": True, "options": [], "rev": 1}]}

            def get_messages(self, sid):
                return {"rev": 1, "messages": [], "draft": ""}

            def answer_card(self, sid, answers, mode="manual", who=None):
                calls.append((sid, answers, mode, who))
                return {"ok": True}

        cls.srv = ss.start_share_server(FakeHub(), FakeApi())
        if cls.srv is None:
            cls.tmp.cleanup()
            raise unittest.SkipTest("端口 %d 被占用" % cls.PORT)
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        if cls.srv is not None:
            cls.srv.shutdown()
            cls.srv.server_close()
        cls.tmp.cleanup()

    def _post(self, path, payload):
        import urllib.request
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s?t=%s" % (self.PORT, path, self.TOKEN),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())

    def test_the_phone_answers_a_card_through_its_own_route(self):
        r = self._post("/api/card", {"sid": "s1", "answers": {"when": {"selected": ["later"]}},
                                     "mode": "adopt_recommended", "who": "rxyy"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(("s1", {"when": {"selected": ["later"]}}, "adopt_recommended", "rxyy"),
                         self.calls[-1])

    def test_missing_fields_fall_back_to_manual_and_the_default_name(self):
        self._post("/api/card", {"sid": "s1"})
        self.assertEqual(("s1", {}, "manual", "同事"), self.calls[-1])


class AdoptButtonWithoutCountdownTests(unittest.TestCase):
    """Bajie 同款：有推荐项就给「采纳推荐」，倒计时只在 autoDecide.enabled 时走。"""

    def test_both_pages_show_adopt_even_when_autodecide_is_off(self):
        for name in ("ui.html", "share.html"):
            src = (MODULE_DIR / name).read_text(encoding="utf-8")
            self.assertIn("ad.enabled || hasRec", src, name)
            self.assertIn("采纳推荐", src, name)


if __name__ == "__main__":
    unittest.main()
