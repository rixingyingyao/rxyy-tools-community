# -*- coding: utf-8 -*-
"""绿灯不熔（09-02 17:09–17:57 事故，对话 351c6f9d）。

第六任 zhi(wait=false, card=三问) 后断线；rxyy 对那张卡回了三条（17:12 / 17:36 / 17:38，
后两条带图），tab 却绿着「等你回复」40 分钟；第七任接手一上来又发带卡的新提问，存着的
旧回复被拿去当场答掉，卡在 rxyy 眼前闪一下就关（16:56 / 17:09 / 17:49 三张卡全这么死）。
四个缺陷 + 一个接手盲区，逐条以事故命名复现（修前红 / 修后绿）：

① 绿灯语义（session_core.agent_liveness）：只发不等的提问，用户已回 / 纯进展 → 不判 waiting
② buffered_reply 单槽覆盖（send_reply 脱离分支 / _flush_queue buffer_only）→ 合并
③ 陈旧回复秒关新卡（_flush_queue）：新提问带 card 时排队消息扣住不答，只放行同一道题的
   补送件；用户答卡时随答复捎带（send_reply）
④ 接手盲区：断线时 buffered_reply 转队列不丢（_client_disconnect）；复活不清有 pending 的
   缓存（create_session）；hub 重启 buffered_reply 随快照走（session_to_snapshot）；空 message
   来收按续期（zhi_request）；接手单写明「有回复待收、先收再开工」（get_takeover_prompt）
顺手：share_takeover 的「当场答复 / 排队」取派单前快照
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
import session_core  # noqa: E402

WS = r"d:\桌面\working\cursor工作流"

CARD = {"title": "#6 怎么开工", "questions": [
    {"id": "q1", "prompt": "走哪条路？",
     "options": [{"id": "a", "label": "读库路", "detail": "不注入 Cursor，读 state.vscdb"},
                 {"id": "b", "label": "注入路", "detail": "改 workbench，实时"}]}]}

IMG = {"data": "data:image/png;base64,QUJD", "filename": "shot.png"}


class _Client:
    def __init__(self, peer_ip=None):
        self.closed_convs, self.sessions, self.cwd, self.pid = {}, {}, WS, 111
        self.peer_ip = peer_ip
        self.last_heartbeat = 0
        self.sent = []

    def send(self, obj):
        self.sent.append(obj)


class _Sock:
    def close(self):
        pass


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
    s.created_at = "2026-09-02 17:00:00"
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
    s.zt_trail = []
    s.rev = 0
    s.lock = threading.Lock()
    return s


class _ImmediateThread:
    def __init__(self, target=None, daemon=None, **kwargs):
        self._target = target

    def start(self):
        if self._target:
            self._target()


class _HubCase(unittest.TestCase):
    """与 test_zhi_deferred.HubDeferredTests 同款夹具：真 handler、假 socket。"""

    def setUp(self):
        self.client = _Client()
        self.s = _sess("t1", "351c6f9d", "rxyy MCP·绿灯不熔")
        self.s.client = self.client
        self.client.sessions["351c6f9d"] = self.s
        self.pushed = []
        ps = [
            patch.object(hub.HUB, "sessions", {"t1": self.s}),
            patch.object(hub.HUB, "order", ["t1"]),
            patch.object(hub.HUB, "cfg", {"max_messages": 200, "detach_grace_secs": 30,
                                          "ide_active_secs": 0, "reconnect_grace_secs": 120}),
            patch.object(hub.HUB, "resolve_session", lambda cli, ck, tn: self.s),
            patch.object(hub.HUB, "_verify_identity_by_generating", MagicMock()),
            patch.object(hub.HUB, "_reap_takeover_shell", MagicMock()),
            patch.object(hub.HUB, "_emergency_reload", MagicMock()),
            patch.object(hub.HUB, "log_ai", MagicMock()),
            patch.object(hub.HUB, "log_user", MagicMock()),
            patch.object(hub.HUB, "log_end", MagicMock()),
            patch.object(hub.HUB, "notify", MagicMock()),
            patch.object(hub.HUB, "wake_window", MagicMock()),
            patch.object(hub.HUB, "flash_taskbar", MagicMock()),
            patch.object(hub.HUB, "push_phone",
                         lambda *a, **k: self.pushed.append((a, k))),
            patch.object(hub.HUB, "_is_checkin_pending", lambda s: False),
            patch.object(hub.HUB, "_push_label", lambda s: s.name),
            patch.object(hub.HUB, "_mark_claimed", MagicMock()),
            patch.object(hub.Hub, "save_msg_images", lambda h, p: []),
            patch.object(hub.Api, "_nudge_rename_if_standby", MagicMock()),
            patch.object(hub.Api, "_auto_label_on_dispatch", lambda a, x, t, who=None: None),
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

    def _zhi(self, rid, message, options=(), deferred=False, resume=False, card=None):
        m = {"type": "zhi_request", "id": rid, "conversation_id": "351c6f9d",
             "message": message, "predefined_options": list(options),
             "is_markdown": True, "resume": resume}
        if deferred:
            m["deferred"] = True
        if card is not None:
            m["card"] = card
        hub.HUB._handle_client_msg(self.client, None, m)

    def _reply(self, text, selected=(), images=()):
        return hub.Api().send_reply("t1", text, list(selected), list(images), False)

    def _responses(self):
        return [m for m in self.client.sent if m.get("type") == "zhi_response"]

    def _ai_bubbles(self):
        return [m for m in self.s.messages if m.get("role") == "ai"]

    def _sys_texts(self):
        return [m.get("html") or "" for m in self.s.messages if m.get("role") == "sys"]


# ---------------- ① 绿灯语义 ----------------

class GreenLightLivenessTests(unittest.TestCase):
    """17:36 rxyy：「消息发出去了还是显示绿色的等待回复？」——回了三条，tab 绿了 40 分钟。"""

    def _liveness(self, s, generating=True):
        with patch.object(hub, "read_cursor_activity",
                          lambda uuid: {"generating": generating, "updated_ts": time.time()}), \
                patch.object(hub, "agent_last_edit_ts", lambda uuid: 0), \
                patch.object(hub.HUB, "board_write_ages", lambda s, now: (None, None)):
            return hub.Api()._agent_liveness(s, time.time())

    def _deferred(self, options=(), card=None, replied=False):
        s = _sess("t1", "351c6f9d", "rxyy MCP·绿灯不熔")
        s.pending = {"id": "r1", "message": "三问卡", "options": list(options),
                     "card": card, "created": time.time()}
        s.wait_deferred = True
        s.detached = True
        if replied:
            s.buffered_reply = {"user_input": "bajie.bajie-chat，参考这个扩展去做",
                                "selected_options": [], "images": [], "files": [],
                                "source": "popup"}
        return s

    def test_a_deferred_question_the_user_already_answered_is_no_longer_a_green_light(self):
        s = self._deferred(card=CARD, replied=True)
        r = self._liveness(s)
        self.assertNotEqual("waiting", r["state"],
                            "修前：pending 不为空一律「等你回复」，用户回了它还说等")
        self.assertEqual("working", r["state"], "落到真实判活（Cursor 正在生成）")
        self.assertIn("已回复·等 AI 来取", r["label"])
        self.assertTrue(any("你已回复" in e for e in r["evidence"]))

    def test_a_deferred_progress_note_with_nothing_to_answer_is_not_a_green_light(self):
        s = self._deferred()  # 无选项无卡：纯进展汇报
        r = self._liveness(s)
        self.assertNotEqual("waiting", r["state"])
        self.assertIn("有进展可回复", r["label"])

    def test_a_deferred_card_nobody_answered_still_waits_for_the_user(self):
        # 守卫：带卡且没人答，用户确实该拍板——绿灯保留，只是说明它只发不等
        s = self._deferred(card=CARD)
        r = self._liveness(s)
        self.assertEqual("waiting", r["state"])
        self.assertIn("只发不等", r["label"])

    def test_a_deferred_question_with_options_nobody_answered_still_waits(self):
        s = self._deferred(options=["重启", "等"])
        self.assertEqual("waiting", self._liveness(s)["state"])

    def test_an_ordinary_blocking_question_is_still_a_plain_green_light(self):
        # 守卫不扩大化：普通阻塞提问照旧
        s = self._deferred(card=CARD, replied=True)
        s.wait_deferred = False
        r = self._liveness(s)
        self.assertEqual(("waiting", "等你回复"), (r["state"], r["label"]))


# ---------------- ② buffered_reply 合并 ----------------

class BufferedReplyMergeTests(_HubCase):
    """17:12 / 17:36 / 17:38 三条回复，第八任来收时只剩最后那条「空正文 + 1 图」。"""

    def test_three_replies_while_detached_are_merged_not_overwritten(self):
        self._zhi("r1", "三问：#6 怎么开工 / curvsix 载荷 / BajieAsk.mdc",
                  deferred=True, card=CARD)
        self.assertTrue(self._reply("bajie.bajie-chat，参考这个扩展去做")["ok"])
        self.assertTrue(self._reply("rxyy MCP mcp 是不是出问题了？", images=[IMG])["ok"])
        self.assertTrue(self._reply("", images=[IMG])["ok"])
        buf = self.s.buffered_reply
        self.assertIsNotNone(buf)
        text = buf["user_input"]
        self.assertIn("bajie.bajie-chat", text, "修前：第二条一到，第一条就被整槽顶掉")
        self.assertIn("rxyy MCP mcp 是不是出问题了", text)
        self.assertLess(text.index("bajie.bajie-chat"), text.index("rxyy MCP mcp"),
                        "按到达顺序拼接")
        self.assertEqual(2, len(buf["images"]), "图片累加")
        self.assertEqual([], self._responses(), "脱离期没人等，仍是存着")
        self.assertIsNotNone(self.s.pending, "提问保留到 agent 来收")
        # agent 来收：三条一次拿齐
        self._zhi("r2", "", resume=True)
        got = self._responses()[-1]
        self.assertEqual("r2", got["id"])
        self.assertIn("bajie.bajie-chat", got["user_input"])
        self.assertEqual(2, len(got["images"]))
        self.assertIsNone(self.s.pending)
        self.assertIsNone(self.s.buffered_reply)

    def test_selected_options_accumulate_without_duplicates(self):
        self._zhi("r1", "重启还是等？", ["重启", "等"], deferred=True)
        self._reply("", selected=["重启"])
        self._reply("再补一句", selected=["重启"])
        self.assertEqual(["重启"], self.s.buffered_reply["selected_options"])
        self.assertIn("再补一句", self.s.buffered_reply["user_input"])

    def test_merge_keeps_only_the_newest_memory_digest(self):
        mark = session_core.MEMORY_DIGEST_MARK
        prev = {"user_input": mark + "\n- 要点甲\n\n第一条", "selected_options": [],
                "images": [], "files": [], "source": "popup"}
        new = {"user_input": mark + "\n- 要点乙\n\n第二条", "selected_options": [],
               "images": [{"data": "x"}], "files": [], "source": "popup_share"}
        out = session_core.merge_buffered_reply(prev, new)
        self.assertTrue(out["user_input"].startswith(mark))
        self.assertIn("- 要点乙", out["user_input"])
        self.assertNotIn("- 要点甲", out["user_input"], "摘要每条都会拼一份，只留最新")
        self.assertEqual(1, out["user_input"].count(mark))
        self.assertIn("第一条\n\n第二条", out["user_input"])
        self.assertEqual("popup_share", out["source"])
        self.assertEqual(1, len(out["images"]))

    def test_merge_with_nothing_stored_is_just_the_new_reply(self):
        new = {"user_input": "唯一一条", "selected_options": ["A"], "images": [], "files": []}
        out = session_core.merge_buffered_reply(None, new)
        self.assertEqual("唯一一条", out["user_input"])
        self.assertEqual(["A"], out["selected_options"])

    def test_queued_messages_flushed_into_an_existing_buffer_are_appended(self):
        # 用户先回了一条（存着），又提前排了一条；新提问只发不等落地 → _flush_queue
        # 走 buffer_only 分支：以前整槽覆盖，先回的那条丢
        self._zhi("r1", "进展 1", deferred=True)
        self._reply("先回的这条")
        self.s.queued.append({"id": "q1", "text": "排队的这条", "images": [], "files": [],
                              "msg": None, "who": None, "ts": time.time()})
        self.assertTrue(hub.HUB._flush_queue(self.s))
        buf = self.s.buffered_reply
        self.assertIn("先回的这条", buf["user_input"], "修前：buffer_only 分支整槽覆盖")
        self.assertIn("排队的这条", buf["user_input"])
        self.assertEqual([], self.s.queued)


# ---------------- ③ 陈旧回复不许秒关新卡 ----------------

class CardHoldsStaleQueuedRepliesTests(_HubCase):
    """16:56 / 17:09 / 17:49 三张卡：落地瞬间被一句陈旧「继续」当场答掉、闪一下就关。"""

    def _queue(self, text, **kw):
        e = {"id": kw.pop("id", "q1"), "text": text, "images": [], "files": [],
             "msg": None, "who": None, "ts": time.time()}
        e.update(kw)
        self.s.queued.append(e)
        return e

    def test_a_stale_queued_reply_does_not_close_a_freshly_opened_card(self):
        self._queue("继续")
        self._zhi("r1", "三问：#6 怎么开工", card=CARD)
        self.assertEqual([], self._responses(), "修前：「继续」当场答卡，卡秒关")
        self.assertIsNotNone(self.s.pending, "卡保持打开等用户答")
        self.assertIsNotNone(self.s.pending.get("card"))
        self.assertEqual(1, len(self.s.queued), "扣住，不丢")
        self.assertTrue(any("已扣住" in t for t in self._sys_texts()),
                        "得告诉用户那条为什么没送出去")
        self.assertEqual(1, len(self.pushed), "卡在等人答，照常推手机")

    def test_a_typed_send_while_the_card_is_up_does_not_auto_close_it(self):
        self._zhi("r1", "三问：#6 怎么开工", card=CARD)
        self._queue("补充一句现场情况", reply_to="r1")
        self.assertFalse(hub.HUB._flush_queue(self.s))
        self.assertIsNotNone(self.s.pending.get("card"))
        self.assertEqual(1, len(self.s.queued))
        r = self._reply("走读库路", selected=["读库路"])
        self.assertTrue(r["ok"], r)
        got = self._responses()[-1]
        self.assertIn("走读库路", got["user_input"])
        self.assertIn("补充一句现场情况", got["user_input"])
        self.assertIsNone(self.s.pending)

    def test_answering_the_card_carries_the_held_messages_along(self):
        self._queue("继续")
        self._zhi("r1", "三问：#6 怎么开工", card=CARD)
        r = self._reply("走读库路", selected=["读库路"])
        self.assertTrue(r["ok"], r)
        got = self._responses()[-1]
        self.assertEqual("r1", got["id"])
        self.assertIn("走读库路", got["user_input"])
        self.assertIn("继续", got["user_input"], "扣住的随答复一起送，不丢")
        self.assertEqual(["读库路"], got["selected_options"])
        self.assertEqual([], self.s.queued)
        self.assertIsNone(self.s.pending)

    def test_held_messages_are_not_dropped_when_the_agent_reasks_the_card(self):
        # 卡挂着没人答，agent 又把同一张卡原样重发（续期/重问）：扣住的照旧扣着，
        # 「已扣住」的系统气泡不重复刷
        self._queue("继续")
        self._zhi("r1", "三问：#6 怎么开工", card=CARD)
        self._zhi("r2", "三问：#6 怎么开工", card=CARD)
        self.assertEqual(1, len(self.s.queued))
        self.assertEqual(1, sum("已扣住" in t for t in self._sys_texts()))

    def test_a_redelivery_of_the_same_question_still_answers_the_card(self):
        # 重问补送闸的正路：用户答过这张卡、回复被吞、agent 原样重问 → 补送件当场答上
        self._queue("[补送|…] 读库路，先不注入", redelivery=True,
                    question="三问：#6 怎么开工", q_options=[])
        self._zhi("r1", "三问：#6 怎么开工", card=CARD)
        got = self._responses()
        self.assertEqual(1, len(got))
        self.assertIn("读库路，先不注入", got[0]["user_input"])
        self.assertIsNone(self.s.pending)

    def test_a_redelivery_of_a_different_question_is_held(self):
        self._queue("[补送|…] 要", redelivery=True, question="要不要重启？", q_options=["要", "不要"])
        self._zhi("r1", "三问：#6 怎么开工", card=CARD)
        self.assertEqual([], self._responses(), "答的是别的题，不许拿来关这张卡")
        self.assertIsNotNone(self.s.pending)

    def test_an_old_redelivery_without_a_recorded_question_is_held_too(self):
        # 老补送件没记问题：不知道答的是哪道题，宁可让用户多点一下也别把卡关掉
        self._queue("[补送|…] 继续", redelivery=True)
        self._zhi("r1", "三问：#6 怎么开工", card=CARD)
        self.assertEqual([], self._responses())

    def test_a_queued_message_still_answers_a_new_question_without_a_card(self):
        # 守卫不扩大化：没卡的普通提问照旧当场答
        self._queue("继续")
        self._zhi("r1", "下一步做哪个？", ["A", "B"])
        got = self._responses()
        self.assertEqual(1, len(got))
        self.assertIn("继续", got[0]["user_input"])
        self.assertIsNone(self.s.pending)

    def test_teammate_mail_never_answers_a_card_and_rides_along_with_the_user(self):
        # 队友转告本来就不占回复位（08-04 规则）；有卡时同样扣住、答卡时捎带
        self._queue("【agent 转告】我这边改完了", who="agent·乙")
        self._zhi("r1", "三问：#6 怎么开工", card=CARD)
        self.assertEqual([], self._responses())
        self._reply("走读库路")
        got = self._responses()[-1]
        self.assertIn("我这边改完了", got["user_input"])
        self.assertEqual([], self.s.queued)

    def test_rescue_records_which_question_the_swallowed_reply_answered(self):
        # 补送件要带着「答的是哪道题」，_answers_this_question 才认得
        self.s.agent_status_ts = 0
        self.s.last_reply_probe = {"ts": time.time(), "text": "读库路", "images": [],
                                   "files": [], "who": None, "msg_ref": {"role": "user"},
                                   "question": "三问：#6 怎么开工", "q_options": []}
        hub.HUB._rescue_swallowed_reply(self.s, why="通道断开")
        e = self.s.queued[0]
        self.assertTrue(e["redelivery"])
        self.assertEqual("三问：#6 怎么开工", e["question"])
        self.assertEqual([], e["q_options"])
        self.assertTrue(hub.Hub._answers_this_question(
            e, {"message": "三问：#6 怎么开工", "options": []}))
        self.assertFalse(hub.Hub._answers_this_question(
            e, {"message": "三问：#6 怎么开工", "options": ["A"]}), "选项不同 = 不是同一道题")
        self.assertFalse(hub.Hub._answers_this_question(
            e, {"message": "别的题", "options": []}))
        self.assertFalse(hub.Hub._answers_this_question(
            {"text": "继续", "redelivery": False, "question": "三问：#6 怎么开工"},
            {"message": "三问：#6 怎么开工", "options": []}), "非补送件不走这道放行口")


# ---------------- ④ 接手盲区 ----------------

class DisconnectKeepsBufferedReplyTests(_HubCase):
    """17:57:50 第七任判死 → _client_disconnect 清 pending；buffered_reply 没清也没转队列，
    下次复活分支直接置 None——三条回复蒸发。"""

    def _disconnect(self):
        with patch.object(hub.HUB, "lock", threading.Lock()):
            hub.HUB._client_disconnect(self.client, _Sock())

    def test_a_buffered_reply_survives_the_agent_dying_before_collecting(self):
        self._zhi("r1", "三问：#6 怎么开工", deferred=True, card=CARD)
        self._reply("bajie.bajie-chat，参考这个扩展去做")
        self._reply("rxyy MCP mcp 是不是出问题了？", images=[IMG])
        self._disconnect()
        self.assertFalse(self.s.connected)
        self.assertIsNone(self.s.pending)
        self.assertIsNone(self.s.buffered_reply, "转走了，不留在原槽等复活分支清掉")
        self.assertEqual(1, len(self.s.queued), "合并后的整条进队列一次，不拆、不重")
        e = self.s.queued[0]
        self.assertIn("bajie.bajie-chat", e["text"])
        self.assertIn("rxyy MCP mcp", e["text"])
        self.assertIn("没来取就断了", e["text"])
        self.assertEqual(1, len(e["images"]))
        self.assertFalse(e.get("redelivery"), "这是从未送出的回复，不是补送件")
        self.assertIsNone(self.s.last_reply_probe, "探针清掉，救援不再把最后一条再入一次队")
        self.assertTrue(any("没来取就断了链接" in t for t in self._sys_texts()))

    def test_the_successor_collects_it_with_an_empty_zhi_after_revival(self):
        self._zhi("r1", "三问：#6 怎么开工", deferred=True, card=CARD)
        self._reply("bajie.bajie-chat，参考这个扩展去做")
        self._disconnect()
        self.s.connected = True  # 接手方拿原 ID 报到 = 复活（create_session 另测）
        del self.client.sent[:]
        self._zhi("r2", "")  # 接手单：先 zhi(message 留空) 收；新进程没有 resume 记忆
        got = self._responses()
        self.assertEqual(1, len(got))
        self.assertEqual(("r2", "popup_queued"), (got[0]["id"], got[0]["source"]))
        self.assertIn("bajie.bajie-chat", got[0]["user_input"])
        self.assertIsNone(self.s.pending)
        bubble = self._ai_bubbles()[-1]
        self.assertIn("来收上一条提问的回复", bubble["html"], "别渲染一片空白")

    def test_a_reply_that_was_really_sent_is_still_rescued_the_old_way(self):
        # 守卫：普通阻塞提问 → 回复直发 → agent 无动静就断了 → 老救援照旧补送一条
        self._zhi("r1", "要不要重启？", ["要", "不要"])
        self._reply("要，现在就重启")
        self.assertIsNone(self.s.buffered_reply)
        self._disconnect()
        self.assertEqual(1, len(self.s.queued))
        self.assertTrue(self.s.queued[0]["redelivery"])
        self.assertIn("要，现在就重启", self.s.queued[0]["text"])

    def test_disconnect_with_nothing_buffered_queues_nothing(self):
        self._zhi("r1", "进展：改完 server", deferred=True)
        self._disconnect()
        self.assertEqual([], self.s.queued)


class RevivalKeepsBufferedReplyTests(unittest.TestCase):
    """create_session 复活分支：`buffered_reply = None` 一刀切——前任还连着、只发不等的
    提问挂着、用户已回，新进程拿同一 ID 来收的那一瞬把回复清了。"""

    def setUp(self):
        self.s = _sess("t1", "351c6f9d", "rxyy MCP·绿灯不熔")
        self.s.peer_ip = None
        ps = [
            patch.object(hub.HUB, "sessions", {"t1": self.s}),
            patch.object(hub.HUB, "order", ["t1"]),
            patch.object(hub.HUB, "cfg", {"max_messages": 200}),
            patch.object(hub.HUB, "save_state", MagicMock()),
            patch.object(hub.HUB, "add_message",
                         lambda s, m: s.messages.append(m)),
            patch.object(hub.HUB, "maybe_apply_task_name", MagicMock()),
            patch.object(hub.HUB, "yield_zhi", MagicMock()),
            patch.object(hub.HUB, "_reap_handed_off_shell", MagicMock()),
            patch.object(hub.HUB, "takeover_landed", MagicMock()),
            patch.object(hub.HUB, "heal_standby_tab_name", MagicMock()),
            patch.object(hub.HUB, "_bind_registered_team_root", MagicMock()),
            patch.object(hub, "heal_session_paths", lambda s: None),
            patch.object(hub, "apply_session_cwd", lambda s, cwd: False),
            patch.object(hub, "log_event", lambda *a, **k: None),
        ]
        for p in ps:
            p.start()
        self.addCleanup(patch.stopall)

    def test_revival_keeps_the_buffered_reply_while_the_question_is_still_pending(self):
        self.s.pending = {"id": "r1", "message": "三问", "options": [], "card": CARD,
                          "created": time.time()}
        self.s.wait_deferred = True
        self.s.detached = True
        self.s.buffered_reply = {"user_input": "bajie.bajie-chat", "selected_options": [],
                                 "images": [], "files": [], "source": "popup"}
        got = hub.HUB.create_session(_Client(), "351c6f9d", "接手方")
        self.assertIs(self.s, got)
        self.assertIsNotNone(self.s.buffered_reply, "修前：复活即清，用户回过的话蒸发")
        self.assertTrue(self.s.wait_deferred, "提问仍是只发不等，绿灯语义别跟着变")
        self.assertIsNotNone(self.s.pending)

    def test_revival_requeues_an_orphan_buffered_reply(self):
        # 提问已不在（断线清掉）却还存着回复（快照恢复等旁路）：转队列，别丢
        self.s.connected = False
        self.s.buffered_reply = {"user_input": "bajie.bajie-chat", "selected_options": [],
                                 "images": [], "files": [], "source": "popup"}
        hub.HUB.create_session(_Client(), "351c6f9d", "接手方")
        self.assertIsNone(self.s.buffered_reply)
        self.assertEqual(1, len(self.s.queued))
        self.assertIn("bajie.bajie-chat", self.s.queued[0]["text"])
        self.assertFalse(self.s.wait_deferred)


class SnapshotKeepsBufferedReplyTests(unittest.TestCase):
    """hub 重启是「断线」里最常见的一种（接力重启 / 看门狗回收 / 热更）：pending 不落盘
    是设计（活请求的一半），但 buffered_reply 此前也不落盘——09-03 14:50 家机热更重启前
    3 个 tab 正挂着只发不等的提问，用户若回过话，重启就全丢。落盘、恢复成孤儿缓存，
    复活分支（上面 RevivalKeepsBufferedReplyTests）接着把它转队列。"""

    def _snapshot(self, s):
        with patch.object(hub.HUB, "cfg", {"max_messages": 200}), \
                patch.object(hub.HUB, "_real_seq", lambda s: 0):
            return session_core.session_to_snapshot(hub.HUB, s)

    def _restore(self, snap):
        with patch.object(hub, "log_event", lambda *a, **k: None), \
                patch.object(hub.HUB, "_bind_registered_team_root", MagicMock()), \
                patch.object(hub, "heal_session_paths", lambda s: None), \
                patch.object(hub, "heal_named_agent_root", lambda s: None):
            return session_core.session_from_snapshot(hub.HUB, snap)

    def test_snapshot_round_trips_the_buffered_reply_as_an_orphan(self):
        s = _sess("t1", "351c6f9d", "rxyy MCP·绿灯不熔")
        s.pending = {"id": "r1", "message": "三问", "options": [], "card": CARD,
                     "created": time.time()}
        s.wait_deferred = True
        s.buffered_reply = {"user_input": "bajie.bajie-chat", "selected_options": ["A"],
                            "images": [dict(IMG)], "files": [], "source": "popup"}
        snap = self._snapshot(s)
        self.assertEqual("bajie.bajie-chat", snap["buffered_reply"]["user_input"],
                         "修前：快照没有这一项，重启即丢")
        self.assertEqual(1, len(snap["buffered_reply"]["images"]))
        r = self._restore(snap)
        self.assertIsNone(r.pending, "提问是活请求的一半，照旧不随快照走")
        self.assertFalse(r.wait_deferred)
        self.assertEqual(["A"], r.buffered_reply["selected_options"])
        self.assertEqual("bajie.bajie-chat", r.buffered_reply["user_input"])
        self.assertEqual(1, len(r.buffered_reply["images"]), "带图的回复图也要跟着回来")

    def test_snapshot_without_a_buffered_reply_restores_none(self):
        s = _sess("t1", "351c6f9d", "rxyy MCP·绿灯不熔")
        snap = self._snapshot(s)
        self.assertIsNone(snap["buffered_reply"])
        self.assertIsNone(self._restore(snap).buffered_reply)
        # 老快照（没有这一项）同样不炸
        snap.pop("buffered_reply")
        self.assertIsNone(self._restore(snap).buffered_reply)


class EmptyCollectIsAResumeTests(_HubCase):
    """接手方是新进程：按接手单「zhi(message 留空) 收回复」来收，server 侧 resume 标不上。
    以前 hub 把它当「不同的新提问」：旧 waiter superseded、渲染空气泡、存着的回复绕一圈。"""

    def test_a_new_process_collecting_with_an_empty_message_gets_the_buffered_reply(self):
        self._zhi("r1", "三问：#6 怎么开工", deferred=True, card=CARD)
        self._reply("走读库路", selected=["读库路"])
        self._zhi("r2", "")  # resume=False：新进程
        got = self._responses()
        self.assertEqual(["r2"], [m["id"] for m in got], "修前：r1 先收到 superseded")
        self.assertIn("走读库路", got[0]["user_input"])
        self.assertEqual(["读库路"], got[0]["selected_options"])
        self.assertIsNone(self.s.pending)
        self.assertFalse(self.s.wait_deferred)
        self.assertEqual(1, len(self._ai_bubbles()), "续期不重发气泡、不渲染空气泡")

    def test_an_empty_collect_with_nothing_buffered_just_takes_over_the_wait(self):
        self._zhi("r1", "重启还是等？", ["重启", "等"], deferred=True)
        self._zhi("r2", "")
        self.assertEqual([], self._responses())
        self.assertEqual("r2", self.s.pending["id"], "等待方换成新请求，提问原样保留")
        self.assertEqual(["重启", "等"], self.s.pending["options"])
        self.assertFalse(self.s.wait_deferred, "来收的在真等了，看门狗照常管")
        self.assertFalse(self.s.detached)
        self.assertEqual(1, len(self._ai_bubbles()))
        # 用户这时答，直发给新等待方
        self._reply("", selected=["重启"])
        self.assertEqual("r2", self._responses()[-1]["id"])

    def test_a_real_new_question_is_still_a_new_question(self):
        # 守卫：带正文的就是新提问，旧的照旧 superseded
        self._zhi("r1", "进展：改完 server", deferred=True)
        self._zhi("r2", "下一步做哪个？", ["A", "B"])
        got = self._responses()
        self.assertEqual([("r1", "superseded")], [(m["id"], m["source"]) for m in got])
        self.assertEqual("r2", self.s.pending["id"])
        self.assertEqual(2, len(self._ai_bubbles()))

    def test_an_empty_message_with_options_is_not_treated_as_a_collect(self):
        # 守卫：空正文但带选项——形状怪，但不是来收的，别吞
        self._zhi("r1", "进展", deferred=True)
        self._zhi("r2", "", ["A", "B"])
        self.assertEqual("r2", self.s.pending["id"])
        self.assertEqual(2, len(self._ai_bubbles()))


class TakeoverPromptUncollectedTests(unittest.TestCase):
    """接手单只有 400 字摘要，一个字没提「有回复存着」→ 第七任按纪律直接发带卡的新提问。"""

    def _s(self):
        return _sess("t1", "351c6f9d", "rxyy MCP·绿灯不熔")

    def test_prompt_tells_the_successor_a_reply_is_waiting_to_be_collected(self):
        s = self._s()
        s.pending = {"id": "r1", "message": "三问：#6 怎么开工 / curvsix / BajieAsk.mdc",
                     "options": [], "card": CARD, "created": time.time()}
        s.wait_deferred = True
        s.buffered_reply = {
            "user_input": session_core.MEMORY_DIGEST_MARK + "\n- 要点\n\nbajie.bajie-chat，参考这个扩展去做",
            "selected_options": ["读库路"], "images": [{"data": "a"}, {"data": "b"}],
            "files": [{"name": "截图.png", "data": "x"}], "source": "popup"}
        block = hub.Api._takeover_uncollected_block(s, "351c6f9d")
        self.assertIn("有用户回复待收", block)
        self.assertIn("只发不等", block)
        self.assertIn("带选项/决策卡", block)
        self.assertIn("用户已经回复了", block)
        self.assertIn("bajie.bajie-chat，参考这个扩展去做", block)
        self.assertNotIn("要点", block, "会话要点摘要是机器拼的，不进接手单")
        self.assertIn("读库路", block)
        self.assertIn("2 张图片", block)
        self.assertIn("截图.png", block)
        self.assertIn('zhi(conversation_id="351c6f9d"，message 留空)', block)
        self.assertLess(block.index("怎么收"), len(block))

    def test_prompt_lists_queued_user_messages_left_by_the_disconnect(self):
        s = self._s()  # 断线后：pending 已清，回复在队列里
        s.queued = [{"id": "q1", "text": "[上一条提问的回复|AI 只发不等、没来取就断了，用户已回话] "
                                        "rxyy MCP mcp 是不是出问题了？",
                     "images": [{"data": "a"}], "files": [], "who": None, "ts": time.time()},
                    {"id": "q2", "text": "【agent 转告】改完了", "images": [], "files": [],
                     "who": "agent·乙", "ts": time.time()}]
        block = hub.Api._takeover_uncollected_block(s, "351c6f9d")
        self.assertIn("1 条用户消息", block, "队友转告不算用户回复")
        self.assertIn("rxyy MCP mcp 是不是出问题了", block)
        self.assertIn("1 张图", block)
        self.assertNotIn("改完了", block)

    def test_prompt_mentions_an_unanswered_deferred_card_without_inventing_a_reply(self):
        s = self._s()
        s.pending = {"id": "r1", "message": "三问", "options": [], "card": CARD,
                     "created": time.time()}
        s.wait_deferred = True
        block = hub.Api._takeover_uncollected_block(s, "351c6f9d")
        self.assertIn("用户还没回复", block)
        self.assertNotIn("用户已经回复了", block)

    def test_prompt_stays_silent_when_nothing_is_waiting(self):
        s = self._s()
        self.assertEqual("", hub.Api._takeover_uncollected_block(s, "351c6f9d"))
        s.pending = {"id": "r1", "message": "要不要重启？", "options": ["要"], "card": None}
        self.assertEqual("", hub.Api._takeover_uncollected_block(s, "351c6f9d"),
                         "普通阻塞提问不是只发不等，没有「存着的回复」这回事")
        s.pending = None
        s.queued = [{"id": "q", "text": "转告", "who": "agent·乙", "images": [], "files": []}]
        self.assertEqual("", hub.Api._takeover_uncollected_block(s, "351c6f9d"))

    def test_the_block_rides_into_the_built_prompt_before_the_workspace_check(self):
        block = "【前任断线前 · 有用户回复待收 · 先收再开工】\n- 用户已经回复了\n"
        with patch.object(hub.Api, "library_digest", return_value=""):
            p = hub.Api()._build_takeover_prompt(
                "351c6f9d", WS, "rxyy MCP·绿灯不熔", "f.md", "摘要", None, "",
                uncollected=block)
            bare = hub.Api()._build_takeover_prompt(
                "351c6f9d", WS, "rxyy MCP·绿灯不熔", "f.md", "摘要", None, "")
        self.assertIn("有用户回复待收", p)
        self.assertLess(p.index("原会话信息"), p.index("有用户回复待收"))
        self.assertLess(p.index("有用户回复待收"), p.index("先对一眼工作区"))
        self.assertNotIn("有用户回复待收", bare)

    def test_get_takeover_prompt_wires_the_block_in(self):
        s = self._s()
        s.file_path = str(Path(__file__))  # 存在的文件，别触发记录重建
        s.pending = {"id": "r1", "message": "三问", "options": [], "card": CARD,
                     "created": time.time()}
        s.wait_deferred = True
        s.buffered_reply = {"user_input": "bajie.bajie-chat", "selected_options": [],
                            "images": [], "files": [], "source": "popup"}
        with patch.object(hub.HUB, "sessions", {"t1": s}), \
                patch.object(hub.Api, "library_digest", return_value=""), \
                patch.object(hub.Api, "_locate_transcripts", lambda a, cwd, ck, limit=3: []), \
                patch.object(hub.Api, "_locate_transcript", lambda a, cwd, ck: None), \
                patch.object(hub.Api, "_takeover_team_block", lambda a, s: ""), \
                patch.object(hub.Api, "_takeover_git_evidence", lambda a, cwd, limit=8: ""), \
                patch.object(hub, "log_event", lambda *a, **k: None):
            r = hub.Api().get_takeover_prompt("t1")
        self.assertTrue(r["ok"], r)
        self.assertIn("用户已经回复了", r["prompt"])
        self.assertIn("bajie.bajie-chat", r["prompt"])


class TakeoverLogSnapshotTests(unittest.TestCase):
    """share_takeover 的「当场答复 / 排队」在 send_reply 之后取 t.pending——送达即清空，
    永远打「排队」，tab 上那句「已排队，它下次提问时收到」也说反。"""

    def setUp(self):
        for p in (patch.object(hub.HUB, "takeover_aliases", {}),
                  patch.object(hub.HUB, "takeover_ledger", {}, create=True),
                  patch.object(hub.HUB, "name_tombstones", {}),
                  patch.object(hub.Hub, "_save_takeover_aliases", lambda self: None)):
            p.start()
            self.addCleanup(p.stop)

    def test_instant_delivery_is_reported_as_instant_not_queued(self):
        dead = _sess("d", "cd", "挂了的")
        dead.connected = False
        busy = _sess("b", "c2", "干活中的")
        busy.pending = {"id": "q", "message": "下一步？", "options": []}
        sessions = {"d": dead, "b": busy}

        def fake_send_reply(self, sid, t, sel, im, cont, who="", files=None):
            hub.HUB.sessions[sid].pending = None  # 真 send_reply 送达即清空
            return {"ok": True}

        with patch.object(hub.HUB, "sessions", sessions), \
                patch.object(hub.HUB, "order", list(sessions)), \
                patch.object(hub.HUB, "cfg", {"max_messages": 200}), \
                patch.object(hub.HUB, "_is_checkin_shellish", lambda s: False), \
                patch.object(hub.Api, "get_takeover_prompt",
                             lambda self, sid, digest_repeat=False: {
                                 "ok": True, "prompt": "接手吧", "conversation_id": "cd",
                                 "name": "挂了的"}), \
                patch.object(hub.Api, "_digest_already_sent", lambda self, tid, cwd: False), \
                patch.object(hub.Api, "send_reply", fake_send_reply), \
                patch.object(hub.Hub, "save_state", lambda self: None), \
                patch.object(hub, "log_event", lambda *a, **k: None):
            r = hub.Api().share_takeover("d", "b", who="手机")
        self.assertTrue(r["ok"], r)
        self.assertFalse(r["queued"], "修前：send_reply 之后再看 pending，永远「排队」")
        self.assertIn("它正等回复，会立刻看到", dead.messages[-1]["html"])
        self.assertNotIn("已排队", dead.messages[-1]["html"])


if __name__ == "__main__":
    unittest.main()
