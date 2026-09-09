# -*- coding: utf-8 -*-
"""ji(action="摘要")：某个会话干到哪了，一段话回给 agent（对标 BajieAsk get_session_summary）。

09-01 六单接手实测：想知道「那个 tab 干到哪了」只有两条路——让用户复制几千行
接手提示词，或者自己去翻对方的记录文件。这里由 hub 按内存实况压一段 ≤ N 字的话：
在线与否、自报状态、zt 轨迹、正等谁回话、最近几句对话、记录文件在哪。
目标解析沿用转告那套（对话 ID 前缀 › 队友名字），留空 = 自己。
"""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub  # noqa: E402
import server  # noqa: E402

WS = r"d:\Desktop\cursor工作流"


def _s(sid, conv, name, **kw):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = sid, conv, name
    s.cwd = s.task_root = WS
    s.connected = True
    s.archived = False
    s.pending = None
    s.queued = []
    s.messages = []
    s.msg_seq = 0
    s.rev = 0
    s.recon_deadline = None
    s.ide_active_cache = False
    s.live_cache = {"state": "working", "label": "干活中"}
    s.model_info = None
    s.agent_status = ""
    s.agent_activity = ""
    s.agent_status_ts = 0
    s.zt_trail = []
    s.file_path = ""
    s.id_history = []
    s.name_history = []
    s.agent_named = True
    s.lock = threading.Lock()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


class SummaryTextTests(unittest.TestCase):
    def setUp(self):
        self.me = _s("s1", "aaaa1111", "rxyy MCP·模型名纠错")
        self.peer = _s(
            "s2", "bbbb2222", "录播客户端·工单接管",
            agent_status="developing", agent_activity="改 deploy.sh", agent_status_ts=1e12,
            zt_trail=["09-02 10:20 developing · 核 11779", "09-02 10:40 testing · 跑回归"],
            pending={"id": "q1", "message": "<p>staging 要不要<b>现在</b>发？</p>",
                     "options": ["发", "等一下"]},
            queued=[{"id": "m1", "text": "hub.py 我要动了", "who": "agent·甲"}],
            file_path=r"D:\持久plus聊天记录\录播客户端·工单接管.md",
            model_info={"model": "grok-4.6", "label": "grok-4.6", "max": False, "effort": ""},
            messages=[
                {"role": "sys", "ts": "10:07", "html": "对话已开始"},
                {"role": "user", "ts": "10:08", "html": "先把 <code>SCB-113</code> 收掉"},
                {"role": "ai", "ts": "10:40", "html": "<p>11779 已在跑新二进制，<br>现场 UUID 待核</p>"},
            ])
        ps = [patch.object(hub.HUB, "lock", threading.Lock()),
              patch.object(hub.HUB, "sessions", {"s1": self.me, "s2": self.peer}),
              patch.object(hub.HUB, "order", ["s1", "s2"]),
              patch.object(hub.HUB, "cfg", {"max_messages": 200}),
              patch.object(hub.HUB, "takeover_aliases", {}),
              patch.object(hub, "log_event", lambda *a, **k: None)]
        for p in ps:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in ps])

    def summary(self, target, requester="me", max_chars=2000):
        req = {"me": self.me, "peer": self.peer, None: None}[requester]
        return hub.Api().session_summary_text(target, requester=req, max_chars=max_chars)

    def test_a_peer_by_conversation_id_prefix_gets_the_full_picture(self):
        text = self.summary("bbbb22")
        self.assertIn("录播客户端·工单接管（bbbb2222）", text)
        self.assertIn("在线", text)
        self.assertIn("模型 grok-4.6", text)
        self.assertIn("developing · 改 deploy.sh", text)
        self.assertIn("09-02 10:40 testing · 跑回归", text)
        self.assertIn("正等用户回话：「staging 要不要现在发？」", text, "html 得去掉")
        self.assertIn("选项：发 / 等一下", text)
        self.assertIn("排队还没送到它手上的消息：1 条", text)
        self.assertIn("🧑 10:08 先把 SCB-113 收掉", text)
        self.assertIn("🤖 10:40 11779 已在跑新二进制，现场 UUID 待核", text)
        self.assertNotIn("对话已开始", text, "系统气泡不算对话")
        self.assertIn(r"D:\持久plus聊天记录\录播客户端·工单接管.md", text)

    def test_a_peer_by_tab_name(self):
        self.assertIn("（bbbb2222）", self.summary("录播客户端·工单接管"))

    def test_an_empty_target_means_myself(self):
        text = self.summary("")
        self.assertIn("rxyy MCP·模型名纠错（aaaa1111）", text)
        self.assertNotIn("bbbb2222", text)

    def test_the_self_words_mean_myself_too(self):
        for w in ("自己", "me", "self"):
            self.assertIn("（aaaa1111）", self.summary(w), w)

    def test_my_own_id_prefix_is_myself_not_a_failure(self):
        # 转告那套把「发给自己」当失败；摘要自己是正当需求
        self.assertIn("（aaaa1111）", self.summary("aaaa11"))

    def test_an_unknown_target_says_so_in_summary_words(self):
        text = self.summary("不存在的人")
        self.assertIn("【摘要失败】", text)
        self.assertNotIn("转告", text)

    def test_no_requester_and_no_target_explains_the_missing_id(self):
        text = self.summary("", requester=None)
        self.assertIn("conversation_id", text)

    def test_no_requester_can_still_ask_about_someone_by_id(self):
        self.assertIn("（bbbb2222）", self.summary("bbbb2222", requester=None))

    def test_the_budget_is_respected_and_the_newest_line_survives(self):
        self.peer.messages = [
            {"role": "user", "ts": "10:0%d" % i, "html": "第%d句 " % i + "很长" * 200}
            for i in range(6)]
        text = self.summary("bbbb2222", max_chars=700)
        self.assertLessEqual(len(text), 700)
        self.assertIn("第5句", text, "最新那句必须在，哪怕截得狠")
        self.assertNotIn("第0句", text)

    def test_messages_are_shown_oldest_to_newest(self):
        text = self.summary("bbbb2222")
        self.assertLess(text.index("🧑 10:08"), text.index("🤖 10:40"))

    def test_an_archived_disconnected_tab_is_labelled(self):
        self.peer.connected = False
        self.peer.archived = True
        self.peer.live_cache = {}
        with patch.object(hub.Api, "_agent_liveness", lambda self, s, now: {"label": "已终止"}):
            text = self.summary("bbbb2222")
        self.assertIn("已归档", text)
        self.assertIn("已终止", text)

    def test_a_weird_max_chars_falls_back_sanely(self):
        self.assertTrue(self.summary("bbbb2222", max_chars="abc"))
        self.assertLessEqual(len(self.summary("bbbb2222", max_chars=10)), 200)


class ServerDispatchTests(unittest.TestCase):
    """MCP 侧：ji(action=摘要) 怎么落到 BRIDGE.fetch_summary，hub 不应答时说人话。"""

    def _ji(self, fake_fetch, **args):
        base = {"action": "摘要", "conversation_id": "aaaa1111"}
        base.update(args)
        with patch.object(server, "DISABLED", False), \
             patch.object(server.BRIDGE, "fetch_summary", fake_fetch):
            return server.tool_ji(base)[0]["text"]

    def test_category_is_the_target_and_content_is_the_budget(self):
        calls = []

        def fake(cid, target, max_chars):
            calls.append((cid, target, max_chars))
            return "📄 会话摘要 · 乙"

        self.assertEqual("📄 会话摘要 · 乙", self._ji(fake, category="bbbb2222", content="500"))
        self.assertEqual([("aaaa1111", "bbbb2222", 500)], calls)

    def test_the_memory_category_default_is_not_a_target(self):
        calls = []
        self._ji(lambda cid, t, n: calls.append(t) or "x", category="context")
        self.assertEqual([""], calls)

    def test_the_budget_is_clamped(self):
        calls = []
        self._ji(lambda cid, t, n: calls.append(n) or "x", content="999999")
        self._ji(lambda cid, t, n: calls.append(n) or "x", content="1")
        self._ji(lambda cid, t, n: calls.append(n) or "x", content="不是数")
        self.assertEqual([8000, 200, 2000], calls)

    def test_a_silent_hub_gets_a_human_fallback(self):
        text = self._ji(lambda *a: None)
        self.assertIn("没有应答", text)
        self.assertIn("聊天记录", text)

    def test_english_aliases_work(self):
        for action in ("summary", "get_session_summary", "会话摘要"):
            self.assertEqual("ok", self._ji(lambda *a: "ok", action=action), action)


class BridgeRoundTripTests(unittest.TestCase):
    """HubBridge.fetch_summary：发 agent_summary_fetch，等 summary_response，超时回 None。"""

    def setUp(self):
        self.bridge = server.HubBridge()
        self.bridge._hb_started = True  # 别起心跳线程
        self.sent = []

    def _patch_send(self):
        return patch.object(server, "send_msg", lambda sock, m: self.sent.append(m))

    def test_it_asks_the_hub_and_hands_back_the_text(self):
        def answer():
            for _ in range(300):
                with self.bridge.waiters_lock:
                    w = next((w for w in self.bridge.waiters.values()
                              if w.get("kind") == "summary"), None)
                if w is not None:
                    self.bridge._reader_dispatch({
                        "type": "summary_response", "id": self.sent[0]["id"],
                        "conversation_id": "aaaa1111", "text": "📄 会话摘要 · 乙"})
                    return
                threading.Event().wait(0.01)

        with self._patch_send():
            t = threading.Thread(target=answer, daemon=True)
            t.start()
            got = self.bridge.fetch_summary("aaaa1111", "bbbb2222", 500,
                                            sock=object(), timeout=3.0)
            t.join(timeout=3)
        self.assertEqual("📄 会话摘要 · 乙", got)
        req = self.sent[0]
        self.assertEqual("agent_summary_fetch", req["type"])
        self.assertEqual(("aaaa1111", "bbbb2222", 500),
                         (req["conversation_id"], req["target"], req["max_chars"]))
        self.assertEqual({}, self.bridge.waiters, "等待方不许泄漏")

    def test_a_silent_hub_means_none_not_a_hang(self):
        with self._patch_send():
            self.assertIsNone(self.bridge.fetch_summary(
                "aaaa1111", "bbbb2222", 500, sock=object(), timeout=0.5))
        self.assertEqual({}, self.bridge.waiters)

    def test_a_late_answer_is_dropped_quietly(self):
        # 摘要是即时问答，迟到的不像转告那样值得寄存
        self.bridge._reader_dispatch({"type": "summary_response", "id": "没人等",
                                      "conversation_id": "aaaa1111", "text": "x"})
        self.assertEqual({}, getattr(self.bridge, "_late_mail", {}))


if __name__ == "__main__":
    unittest.main()
