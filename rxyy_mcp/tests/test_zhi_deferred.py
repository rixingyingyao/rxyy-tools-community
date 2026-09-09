# -*- coding: utf-8 -*-
"""zhi(wait=false) 只发不等——reply/wait 分离（对标 BajieAsk 的 reply_message + wait_message）。

09-01 rxyy 拍板 #7c。以前 zhi 只有一种形状：发出提问就阻塞到用户回话，agent 想「先汇报
一句、接着干活、回头再收答复」做不到，只能把汇报攒到收尾一次说。现在：
- server：wait=false 时提问照常落到控制台，只抓 deferred_peek_secs 的即时答复（排队补送 /
  脱离期缓存），没有就发 zhi_detach 立刻返回 {"deferred": True}；之后 message 留空再调
  走既有 resume 续期路径阻塞收回复
- hub：zhi_request 带 deferred → s.wait_deferred，失联看门狗对它只挂起不清 pending；
  agent 没来取就又发新提问时，脱离期缓存的用户回复转队列作为新提问的答复补送，不再丢
- 顺带补上一条老窄窗：等待到点 → 脱离电文发出之间恰好到达的回复，以前抛 KEEPALIVE
  后就永远送不出去（迟到的 zhi_response 没有等待方会被丢），现在照常交付
"""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub  # noqa: E402
import server  # noqa: E402
import session_core  # noqa: E402

WS = r"d:\桌面\working\cursor工作流"


# ---------------- server 侧 ----------------

class _Sock:
    pass


class DeferredAskTests(unittest.TestCase):
    """HubBridge.ask(wait=False)：发出去、脱离、当场返回；来收就是 resume。"""

    def setUp(self):
        self.bridge = server.HubBridge()
        self.bridge._hb_started = True  # 别起心跳线程
        self.sent = []
        self.sock = _Sock()
        ps = [
            patch.object(server, "send_msg", self._send),
            patch.object(self.bridge, "_connect_locked", lambda wait_secs=0: self.sock),
            patch.object(server.HubBridge, "_deferred_peek_secs", staticmethod(lambda cfg: 0.2)),
            patch.object(server, "DATA_DIR", Path(__file__).parent / "_no_such_cfg_dir"),
        ]
        for p in ps:
            p.start()
        self.addCleanup(patch.stopall)
        self.on_send = None  # (msg) -> None：模拟 hub 对某条电文的即时反应

    def _send(self, sock, msg):
        self.sent.append(msg)
        if self.on_send:
            self.on_send(msg)

    def _types(self):
        return [m["type"] for m in self.sent]

    def _reply(self, req_id, text):
        self.bridge._reader_dispatch({
            "type": "zhi_response", "id": req_id, "user_input": text,
            "selected_options": [], "images": [], "source": "popup"})

    def test_wait_false_posts_detaches_and_returns_at_once(self):
        t0 = time.time()
        resp = self.bridge.ask("进展：hub 改完", [], True, "c1", "rxyy MCP·x", wait=False)
        self.assertEqual({"deferred": True, "resumed": False}, resp)
        # 没人回话也返回了 = 没阻塞；上界放宽只防全量并跑时机器慢被误判
        self.assertLess(time.time() - t0, 10, "只发不等不许真等人")
        self.assertEqual(["zhi_request", "zhi_detach"], self._types())
        req, det = self.sent
        self.assertTrue(req["deferred"], "hub 得知道这条马上要脱离")
        self.assertFalse(req["resume"])
        self.assertEqual("进展：hub 改完", req["message"])
        self.assertEqual(req["id"], det["id"])
        self.assertIn("c1", self.bridge._keepalive_active, "下次空 message 来收要按续期走")
        self.assertIn("c1", self.bridge._deferred_convs)
        self.assertEqual({}, self.bridge.waiters, "等待方不许泄漏")

    def test_collecting_later_is_a_resume_that_rebuilds_the_question(self):
        self.bridge.ask("进展：hub 改完", ["好", "停"], True, "c1", wait=False)
        del self.sent[:]
        got = {}

        def collect():
            got["resp"] = self.bridge.ask("", [], True, "c1", wait=True)

        t = threading.Thread(target=collect, daemon=True)
        t.start()
        for _ in range(300):
            if self.sent:
                break
            time.sleep(0.01)
        req = self.sent[0]
        self.assertTrue(req["resume"], "来收 = 续期重呼，hub 沿用旧提问不重发气泡")
        self.assertNotIn("deferred", req)
        self.assertEqual("进展：hub 改完", req["message"], "hub 若重启过要能用原文重建")
        self.assertEqual(["好", "停"], req["predefined_options"])
        self._reply(req["id"], "好，继续")
        t.join(timeout=3)
        self.assertEqual("好，继续", got["resp"]["user_input"])
        self.assertNotIn("c1", self.bridge._keepalive_active)
        self.assertNotIn("c1", self.bridge._deferred_convs)

    def test_a_reply_the_hub_already_holds_comes_back_at_once_even_with_wait_false(self):
        # hub 落卡后 _flush_queue 立刻答（排队消息 / 脱离期缓存转队列）：peek 窗口内抓到就是正常回复
        def hub_answers_immediately(msg):
            if msg["type"] == "zhi_request":
                self._reply(msg["id"], "[上一条提问的回复] 先修 A")
        self.on_send = hub_answers_immediately
        resp = self.bridge.ask("进展 2", [], True, "c1", wait=False)
        self.assertEqual("[上一条提问的回复] 先修 A", resp["user_input"])
        self.assertEqual(["zhi_request"], self._types(), "已答复就不该再发脱离")
        self.assertNotIn("c1", self.bridge._deferred_convs)
        self.assertNotIn("c1", self.bridge._keepalive_active)

    def test_a_reply_racing_the_deferred_detach_is_delivered_not_dropped(self):
        def hub_answers_on_detach(msg):
            if msg["type"] == "zhi_detach":
                self._reply(msg["id"], "刚好这时回的")
        self.on_send = hub_answers_on_detach
        resp = self.bridge.ask("进展 3", [], True, "c1", wait=False)
        self.assertEqual("刚好这时回的", resp["user_input"], "脱离电文发出瞬间到的回复不许丢")
        self.assertNotIn("c1", self.bridge._deferred_convs)

    def test_a_reply_racing_the_keepalive_detach_is_delivered_not_keepalived(self):
        # 老路同款窄窗：让路机制强制续期（force_ka）→ 发脱离电文 → 回复恰在此刻到达
        def hub_answers_on_detach(msg):
            if msg["type"] == "zhi_detach":
                self._reply(msg["id"], "窄窗里的回复")
        self.on_send = hub_answers_on_detach

        def force_yield():
            for _ in range(300):
                with self.bridge.waiters_lock:
                    w = next(iter(self.bridge.waiters.values()), None)
                if w is not None:
                    w["force_ka"] = True
                    w["event"].set()
                    return
                time.sleep(0.01)
        threading.Thread(target=force_yield, daemon=True).start()
        resp = self.bridge.ask("要不要重启？", ["要", "不要"], True, "c2", wait=True)
        self.assertEqual("窄窗里的回复", resp["user_input"],
                         "修前：抛 KEEPALIVE，这条回复因无等待方被丢")
        self.assertNotIn("c2", self.bridge._keepalive_active)

    def test_a_second_post_before_collecting_is_a_fresh_question_not_a_resume(self):
        self.bridge.ask("进展 1", [], True, "c1", wait=False)
        del self.sent[:]
        resp = self.bridge.ask("进展 2", [], True, "c1", wait=False)
        self.assertEqual({"deferred": True, "resumed": False}, resp)
        req = self.sent[0]
        self.assertFalse(req["resume"], "续期会让 hub 沿用「进展 1」、把「进展 2」吞掉")
        self.assertEqual("进展 2", req["message"])
        self.assertTrue(req["deferred"])
        self.assertIn("c1", self.bridge._deferred_convs, "第二条同样是只发不等")

    def test_a_blocking_question_after_a_deferred_post_is_also_fresh(self):
        self.bridge.ask("进展 1", [], True, "c1", wait=False)
        del self.sent[:]

        def answer_when_asked(msg):
            if msg["type"] == "zhi_request":
                self._reply(msg["id"], "A")
        self.on_send = answer_when_asked
        resp = self.bridge.ask("A 还是 B？", ["A", "B"], True, "c1", wait=True)
        self.assertEqual("A", resp["user_input"])
        self.assertFalse(self.sent[0]["resume"])
        self.assertEqual("A 还是 B？", self.sent[0]["message"])
        self.assertNotIn("c1", self.bridge._deferred_convs)

    def test_a_peek_with_empty_message_reports_not_yet(self):
        self.bridge.ask("进展 1", [], True, "c1", wait=False)
        del self.sent[:]
        resp = self.bridge.ask("", [], True, "c1", wait=False)
        self.assertEqual({"deferred": True, "resumed": True}, resp)
        req = self.sent[0]
        self.assertTrue(req["resume"], "探一眼 = 续期形状，hub 只换等待方 id")
        self.assertTrue(req["deferred"], "探完还走，看门狗仍按只发不等对待")
        self.assertEqual("进展 1", req["message"])
        self.assertIn("c1", self.bridge._deferred_convs)

    def test_pruning_forgets_deferred_convs_too(self):
        self.bridge._deferred_convs.add("old1")
        self.bridge._active_convs.update("c%d" % i for i in range(70))
        self.bridge._active_convs.add("old1")
        self.bridge._conv_activity["old1"] = time.time() - 90000
        self.bridge._prune_conv_caches(max_convs=64, max_age=86400)
        self.assertNotIn("old1", self.bridge._deferred_convs)


class ToolZhiWaitTests(unittest.TestCase):
    """tool_zhi 层：wait 解析、透传、回执文案、空正文闸、schema。"""

    def _call(self, args, ask_ret=None):
        asked = []

        def fake_ask(*a, **kw):
            asked.append(kw)
            return ask_ret if ask_ret is not None else {"user_input": "ok", "selected_options": []}

        with patch.object(server.BRIDGE, "ask", fake_ask), \
                patch.object(server.BRIDGE, "_schedule_idle_clear", lambda *_a: None), \
                patch.object(server, "wait_if_frozen", lambda: None), \
                patch.object(server, "DISABLED", False):
            out = server.tool_zhi(args)
        return asked, "".join(c.get("text", "") for c in out if c.get("type") == "text")

    def test_wait_parsing(self):
        for v in (False, 0, "false", "False", "0", "no", "off", "否", "不等"):
            self.assertFalse(server._parse_wait(v), repr(v))
        for v in (None, True, 1, "true", "yes", "", "随便"):
            self.assertTrue(server._parse_wait(v), repr(v))

    def test_default_is_blocking(self):
        asked, _ = self._call({"message": "汇报", "conversation_id": "b4eff2ee"})
        self.assertTrue(asked[0]["wait"])

    def test_wait_false_rides_through_and_returns_the_receipt(self):
        asked, text = self._call({"message": "汇报", "conversation_id": "b4eff2ee", "wait": False},
                                 ask_ret={"deferred": True, "resumed": False})
        self.assertFalse(asked[0]["wait"])
        self.assertIn("未等回复", text)
        self.assertIn('conversation_id="b4eff2ee"', text)
        self.assertIn("message 留空", text)
        self.assertNotIn("KEEPALIVE", text, "回执不是保活信号，别让 agent 立刻空重呼")

    def test_wait_false_as_string_also_works(self):
        asked, _ = self._call({"message": "汇报", "conversation_id": "b4eff2ee", "wait": "false"},
                              ask_ret={"deferred": True})
        self.assertFalse(asked[0]["wait"])

    def test_a_peek_that_found_nothing_says_not_yet(self):
        with patch.object(server.BRIDGE, "_keepalive_active", {"b4eff2ee"}):
            _, text = self._call({"message": "", "conversation_id": "b4eff2ee", "wait": False},
                                 ask_ret={"deferred": True, "resumed": True})
        self.assertIn("尚无回复", text)

    def test_wait_false_without_message_and_nothing_pending_is_explained_without_a_hub_trip(self):
        with patch.object(server.BRIDGE, "_keepalive_active", set()):
            asked, text = self._call({"message": "", "conversation_id": "b4eff2ee", "wait": False})
        self.assertEqual([], asked, "没东西可发也没东西可收，不该去打扰 hub 渲染空气泡")
        self.assertIn("需要 message", text)
        self.assertIn("b4eff2ee", text)

    def test_the_schema_advertises_wait(self):
        zhi = next(t for t in server.TOOLS if t["name"] == "zhi")
        self.assertIn("wait", zhi["inputSchema"]["properties"])
        self.assertIn("只发不等", zhi["inputSchema"]["properties"]["wait"]["description"])
        self.assertIn("wait=false", server.MCP_INSTRUCTIONS_BRIEF)
        self.assertIn("wait=false", server.MCP_INSTRUCTIONS_FULL)


# ---------------- hub 侧 ----------------

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
    s.name_locked = False
    s.cwd = s.task_root = WS
    s.peer_ip = None
    s.pid = 111
    s.client = None
    s.connected = True
    s.archived = False
    s.end_reason = ""
    s.pending = None
    s.pending_lost = False
    s.recon_deadline = None
    s.lost_pending_on_drop = False
    s.processing_since = None
    s.detached = False
    s.detached_since = None
    s.buffered_reply = None
    s.wait_deferred = False
    s.disconnected_at = None
    s.last_reply_probe = None
    s.last_heartbeat = time.time()
    s.last_zhi_ts = 0
    s.last_reply_ts = 0
    s.agent_status = ""
    s.agent_activity = ""
    s.agent_status_ts = 0
    s.claimed_task_ts = 0
    s.agent_named = False
    s.shell_born = True
    s.death_info = None
    s.death_probe_at = time.time()
    s.death_alerts = {}
    s.death_alerted_bubbles = {}
    s.name_history = []
    s.cursor_uuid = None
    s.uuid_verified = False
    s.cursor_title = None
    s.transcript_path = None
    s.takeover_dispatched = None
    s.created_ts = time.time()
    s.created_at = "2026-09-02 15:00:00"
    s.file_path = None
    s.library_sent = False
    s.handed_off_to = ""
    s.msg_seq = 0
    s.machine_seq = 0
    s.real_seq = 0
    s.messages = []
    s.queued = []
    s.draft_text = ""
    s.draft_images = []
    s.draft_files = []
    s.rev = 0
    s.lock = threading.Lock()
    return s


class _ImmediateThread:
    def __init__(self, target=None, daemon=None, **kwargs):
        self._target = target

    def start(self):
        if self._target:
            self._target()


class HubDeferredTests(unittest.TestCase):
    def setUp(self):
        self.client = _Client()
        self.s = _sess("t1", "c1", "rxyy MCP·只发不等")
        self.s.client = self.client
        self.client.sessions["c1"] = self.s
        self.pushed = []
        ps = [
            patch.object(hub.HUB, "sessions", {"t1": self.s}),
            patch.object(hub.HUB, "order", ["t1"]),
            patch.object(hub.HUB, "cfg", {"max_messages": 200, "detach_grace_secs": 30,
                                          "ide_active_secs": 0}),
            patch.object(hub.HUB, "resolve_session", lambda cli, ck, tn: self.s),
            patch.object(hub.HUB, "_verify_identity_by_generating", MagicMock()),
            patch.object(hub.HUB, "_reap_takeover_shell", MagicMock()),
            patch.object(hub.HUB, "log_ai", MagicMock()),
            patch.object(hub.HUB, "log_user", MagicMock()),
            patch.object(hub.HUB, "notify", MagicMock()),
            patch.object(hub.HUB, "wake_window", MagicMock()),
            patch.object(hub.HUB, "flash_taskbar", MagicMock()),
            patch.object(hub.HUB, "push_phone",
                         lambda *a, **k: self.pushed.append((a, k))),
            patch.object(hub.HUB, "_is_checkin_pending", lambda s: False),
            patch.object(hub.HUB, "_push_label", lambda s: s.name),
            patch.object(hub.Api, "_nudge_rename_if_standby", MagicMock()),
            patch.object(hub.Api, "_with_library_digest",
                         lambda api, s, ui, selected=None: (ui, False)),
            patch.object(hub.Api, "_with_session_memory",
                         lambda api, s, ui, selected=None: ui),
            patch.object(hub, "maybe_ensure_project_mcp", lambda *a, **k: None),
            patch.object(hub, "owner_session_url", lambda *a, **k: None),
            patch.object(hub, "log_event", lambda *a, **k: None),
            patch.object(hub.threading, "Thread", _ImmediateThread),
        ]
        for p in ps:
            p.start()
        self.addCleanup(patch.stopall)

    def _zhi(self, rid, message, options=(), deferred=False, resume=False):
        m = {"type": "zhi_request", "id": rid, "conversation_id": "c1",
             "message": message, "predefined_options": list(options),
             "is_markdown": True, "resume": resume}
        if deferred:
            m["deferred"] = True
        hub.HUB._handle_client_msg(self.client, None, m)

    def _detach(self, rid):
        hub.HUB._handle_client_msg(self.client, None, {
            "type": "zhi_detach", "id": rid, "conversation_id": "c1"})

    def _responses(self):
        return [m for m in self.client.sent if m.get("type") == "zhi_response"]

    def _watchdog(self, secs_after_detach=3600):
        self.s.detached_since = time.time() - secs_after_detach
        with patch.object(hub.HUB, "_claimed_task", lambda s: False), \
                patch.object(hub.HUB, "_fusion_recent_activity", lambda s, now: None):
            session_core.detach_watchdog_tick(hub.HUB, self.s, time.time())

    def test_a_deferred_question_survives_the_detach_watchdog(self):
        self._zhi("r1", "进展：改完 server", deferred=True)
        self.assertTrue(self.s.wait_deferred)
        self._detach("r1")
        self.assertTrue(self.s.detached)
        self._watchdog()
        self.assertIsNotNone(self.s.pending, "修前：超宽限被当「本轮失联」清掉，用户回话打空")
        self.assertTrue(self.s.detached, "只挂起，等 agent 回来收")
        self.assertFalse(any("失联" in (m.get("html") or "") for m in self.s.messages))

    def test_an_ordinary_question_is_still_cleared_as_before(self):
        # 守卫不扩大化：普通提问脱离超宽限、没认领活、没融合信号 → 照旧切回待机
        self._zhi("r1", "要不要重启？", ["要", "不要"])
        self.assertFalse(self.s.wait_deferred)
        self._detach("r1")
        self._watchdog()
        self.assertIsNone(self.s.pending)
        self.assertTrue(any("失联" in (m.get("html") or "") for m in self.s.messages))

    def test_coming_back_to_collect_ends_the_deferred_state(self):
        self._zhi("r1", "进展：改完 server", deferred=True)
        self._detach("r1")
        self._zhi("r2", "", resume=True)  # 来收：不带 deferred
        self.assertEqual("r2", self.s.pending["id"], "等待方换成新请求，提问不重发")
        self.assertFalse(self.s.wait_deferred, "agent 真在等了，看门狗照常管")
        self.assertEqual(1, len([m for m in self.s.messages if m.get("role") == "ai"]),
                         "续期不重发气泡")

    def test_a_peek_keeps_the_deferred_state(self):
        self._zhi("r1", "进展：改完 server", deferred=True)
        self._detach("r1")
        self._zhi("r2", "", deferred=True, resume=True)
        self.assertTrue(self.s.wait_deferred)
        self._detach("r2")
        self._watchdog()
        self.assertIsNotNone(self.s.pending)

    def test_a_user_reply_while_deferred_is_buffered_and_handed_over_on_collect(self):
        self._zhi("r1", "进展：改完 server，下一步跑全量", deferred=True)
        self._detach("r1")
        with patch.object(hub.Hub, "save_msg_images", lambda h, p: []), \
                patch.object(hub.Api, "_auto_label_on_dispatch", lambda a, x, t, who=None: None), \
                patch.object(hub.HUB, "_mark_claimed", MagicMock()):
            r = hub.Api().send_reply("t1", "先别跑全量，先修 A", [], [], False)
        self.assertTrue(r["ok"], r)
        self.assertIsNotNone(self.s.buffered_reply)
        self.assertEqual([], self._responses(), "没人在等，先存着")
        self._zhi("r2", "", resume=True)
        resp = self._responses()[-1]
        self.assertEqual("r2", resp["id"])
        self.assertIn("先别跑全量，先修 A", resp["user_input"])
        self.assertIsNone(self.s.pending)
        self.assertFalse(self.s.wait_deferred)

    def test_a_deferred_question_is_born_detached_so_an_early_reply_is_buffered_not_dropped(self):
        # 16:06 e2e 实测：MCP 侧 peek 1.5s 就发 zhi_detach，但 hub 这边 handler 里的身份校准
        # 常比 1.5s 长——用户在 detach 到达前回话，回复走 socket 直发、那头没人等，被丢
        self._zhi("r1", "进展：改完 server", deferred=True)
        self.assertTrue(self.s.detached, "deferred 提问生来就是脱离态")
        self.assertIsNotNone(self.s.detached_since)
        with patch.object(hub.Hub, "save_msg_images", lambda h, p: []), \
                patch.object(hub.Api, "_auto_label_on_dispatch", lambda a, x, t, who=None: None), \
                patch.object(hub.HUB, "_mark_claimed", MagicMock()):
            r = hub.Api().send_reply("t1", "detach 还没到就回了", [], [], False)
        self.assertTrue(r["ok"], r)
        self.assertEqual([], self._responses(), "修前：直发给已无等待方的请求，回复蒸发")
        self.assertIn("detach 还没到就回了", self.s.buffered_reply["user_input"])
        self.assertIsNotNone(self.s.pending, "提问保留到 agent 来收")
        self._detach("r1")  # 迟到的脱离电文只是再确认一次，不许出乱子
        self.assertTrue(self.s.detached)
        self._zhi("r2", "", resume=True)
        self.assertIn("detach 还没到就回了", self._responses()[-1]["user_input"])

    def test_an_ordinary_question_is_not_born_detached(self):
        self._zhi("r1", "要不要重启？", ["要", "不要"])
        self.assertFalse(self.s.detached)
        self.assertIsNone(self.s.detached_since)

    def test_queued_messages_answer_a_deferred_post_into_the_buffer_not_the_dead_socket(self):
        # 用户提前排队的消息 + 只发不等的提问：_flush_queue 以前直发 socket；
        # 等待方已脱离时直发 = 丢。改存 buffered_reply、提问保留，来收时交付
        self.s.queued.append({"id": "q1", "text": "先把 A 修了", "images": [], "files": [],
                              "msg": None, "who": None, "ts": time.time()})
        self._zhi("r1", "进展：改完 server", deferred=True)
        self.assertEqual([], self._responses(), "脱离态不许直发")
        self.assertEqual([], self.s.queued)
        self.assertIn("先把 A 修了", self.s.buffered_reply["user_input"])
        self.assertEqual("r1", self.s.pending["id"], "提问保留到 agent 来收")
        self.assertTrue(any("来取时送达" in (m.get("html") or "") for m in self.s.messages))
        self.assertEqual([], self.pushed, "队列已把它答了，不该再推手机")
        self._zhi("r2", "", resume=True)
        resp = self._responses()[-1]
        self.assertEqual(("r2", "popup_queued"), (resp["id"], resp["source"]))
        self.assertIn("先把 A 修了", resp["user_input"])
        self.assertIsNone(self.s.pending)

    def test_queued_messages_still_go_straight_to_a_waiting_agent(self):
        # 守卫不扩大化：普通阻塞提问照旧当场直发
        self.s.queued.append({"id": "q1", "text": "先把 A 修了", "images": [], "files": [],
                              "msg": None, "who": None, "ts": time.time()})
        self._zhi("r1", "下一步？", ["A", "B"])
        resp = self._responses()[-1]
        self.assertEqual(("r1", "popup_queued"), (resp["id"], resp["source"]))
        self.assertIsNone(self.s.pending)
        self.assertIsNone(self.s.buffered_reply)

    def test_a_buffered_reply_is_not_lost_when_the_agent_posts_again_before_collecting(self):
        self._zhi("r1", "进展 1：改完 server", deferred=True)
        self.s.buffered_reply = {"user_input": "先修 A 再说", "selected_options": ["改方案"],
                                 "images": [], "files": [], "source": "popup"}
        self._zhi("r2", "进展 2：全量绿了", deferred=True)
        resp = self._responses()
        # 旧 r1 收到 superseded；用户对进展 1 的回话转成进展 2 的答复存着（新提问也是只发不等）
        self.assertEqual(["r1"], [m["id"] for m in resp])
        self.assertEqual("superseded", resp[0]["source"])
        buf = self.s.buffered_reply
        self.assertIsNotNone(buf, "修前：新提问落地时 buffered_reply 直接置 None，用户的话蒸发")
        self.assertIn("先修 A 再说", buf["user_input"])
        self.assertIn("选择的选项: 改方案", buf["user_input"])
        self.assertIn("上一条提问的回复", buf["user_input"])
        self.assertEqual("r2", self.s.pending["id"])
        self.assertTrue(any("还没来取" in (m.get("html") or "") for m in self.s.messages))
        self._zhi("r3", "", resume=True)  # agent 回来收
        got = self._responses()[-1]
        self.assertEqual("r3", got["id"])
        self.assertIn("先修 A 再说", got["user_input"])
        self.assertIsNone(self.s.pending)
        self.assertIsNone(self.s.buffered_reply)

    def test_a_buffered_reply_answers_a_following_blocking_question_at_once(self):
        # 只发不等后接一条阻塞提问：用户对前一条的回话当场直发给这条（agent 正在等）
        self._zhi("r1", "进展 1：改完 server", deferred=True)
        self.s.buffered_reply = {"user_input": "先修 A 再说", "selected_options": [],
                                 "images": [], "files": [], "source": "popup"}
        self._zhi("r2", "下一步做哪个？", ["A", "B"])
        resp = self._responses()
        self.assertEqual(["r1", "r2"], [m["id"] for m in resp])
        self.assertIn("先修 A 再说", resp[1]["user_input"])
        self.assertIsNone(self.s.pending)

    def test_a_progress_note_with_no_options_does_not_page_the_phone(self):
        self._zhi("r1", "进展：改完 server", deferred=True)
        self.assertEqual([], self.pushed, "纯进展汇报没在等人拍板，不推手机")
        hub.HUB.wake_window.assert_called()

    def test_a_deferred_question_with_options_still_pages_the_phone(self):
        self._zhi("r1", "重启还是等？", ["重启", "等"], deferred=True)
        self.assertEqual(1, len(self.pushed))

    def test_get_state_exposes_pending_deferred(self):
        self._zhi("r1", "进展：改完 server", deferred=True)
        with patch.object(hub.HUB, "lock", threading.Lock()), \
                patch.object(hub.Api, "session_label", lambda a, s: s.name), \
                patch.object(hub.Api, "_takeover_pending_view", lambda a, s, now: None), \
                patch.object(hub.Api, "_self_report", lambda a, s, now: ("", "", None)), \
                patch.object(hub.Api, "_team_role", lambda a, s: ""), \
                patch.object(hub.HUB, "_window_tag", lambda *a: ""), \
                patch.object(hub.HUB, "_did_real_work", lambda s: True), \
                patch.object(hub.HUB, "history_keep_days", lambda: 7), \
                patch.object(hub.HUB, "autostart_enabled", False, create=True), \
                patch.object(hub.HUB, "config_error", "", create=True), \
                patch.object(hub, "task_root_of", lambda s: s.cwd), \
                patch.object(hub, "affiliation_of", lambda s: ""), \
                patch.object(hub, "cross_workspace", lambda s: False):
            hub.HUB.cfg["history_dir"] = "."
            st = hub.Api().get_state()
        row = st["sessions"][0]
        self.assertTrue(row["pending"])
        self.assertTrue(row["pending_deferred"])
        self._zhi("r2", "", resume=True)
        self.assertFalse(self.s.wait_deferred)


if __name__ == "__main__":
    unittest.main()
