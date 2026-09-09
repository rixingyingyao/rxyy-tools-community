# -*- coding: utf-8 -*-
"""死因分级：挂了之后「下一步干什么」必须有人话（死会话处置①，08-13 四起实证）。

当天四起死亡全靠人工盘点才发现、全靠人工决定怎么救：
01:25 ×2「Fable 5 hit a safety filter → Switched to Opus 4.8」（ERROR_CUSTOM_MESSAGE）、
05:00 code=None、10:20 failed_precondition code=29。其实除了「对话被删」，其余
死法 Cursor 原窗口上下文都在，点重试/回一句就能原地续——分级的意义就是把这句
话带到气泡、推送和寄存回执上。
"""
import sys
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub  # noqa: E402
import session_core  # noqa: E402


class ClassifyDeathTests(unittest.TestCase):
    def test_model_switch_is_revivable_in_place(self):
        # 01:25 实例：安全过滤器切模型——原窗口直接继续
        dead = {"code": '{"error":"ERROR_CUSTOM_MESSAGE","details":{"title":'
                        '"Switched to Opus 4.8","detail":"Fable 5 hit a safety '
                        'filter, and the conversation was automatically switched"}}',
                "reason": "报错中断"}
        revivable, advice = session_core.classify_death(dead)
        self.assertTrue(revivable)
        self.assertIn("安全过滤器", advice)
        self.assertIn("接手", advice)

    def test_failed_precondition_is_revivable(self):
        # 10:20 实例
        revivable, advice = session_core.classify_death(
            {"code": "failed_precondition 29", "reason": "报错中断"})
        self.assertTrue(revivable)
        self.assertIn("重试", advice)

    def test_unknown_code_none_is_revivable(self):
        # 05:00 实例：code=None 的无名死法也给出通用下一步
        revivable, advice = session_core.classify_death(
            {"code": None, "reason": "报错中断"})
        self.assertTrue(revivable)
        self.assertTrue(advice)

    def test_gone_conversation_is_not_revivable(self):
        dead = {"code": "gone", "reason": "对话已从 Cursor 里消失",
                "advice": "右键这个 tab「🤝 接手」"}
        revivable, advice = session_core.classify_death(dead)
        self.assertFalse(revivable)
        self.assertIn("接手", advice)

    def test_quota_death_says_recharge_then_retry(self):
        revivable, advice = session_core.classify_death(
            {"code": 50, "reason": "账号欠费"})
        self.assertTrue(revivable)
        self.assertIn("充值", advice)

    def test_transient_network_death_says_it_heals(self):
        revivable, advice = session_core.classify_death(
            {"code": "?", "reason": "unable to reach the model provider"})
        self.assertTrue(revivable)
        self.assertIn("缓过来", advice)

    def test_nameless_interrupt_is_transient_quota_is_not(self):
        self.assertTrue(session_core._is_transient_death(
            {"code": None, "reason": "报错中断"}))
        self.assertTrue(session_core._is_transient_death(
            {"code": "?", "reason": "unable to reach the model provider"}))
        self.assertFalse(session_core._is_transient_death(
            {"code": 50, "reason": "账号欠费"}))
        self.assertFalse(session_core._is_transient_death(
            {"code": "failed_precondition 29", "reason": "报错中断"}))


class ProbeFillsAdviceTests(unittest.TestCase):
    """tick_death_probe 集成：death_info 带上 revivable/advice，推送带下一步。"""

    def _sess(self):
        s = hub.Session.__new__(hub.Session)
        s.id, s.name, s.conv_key = "s1", "rxyy MCP·判活融合", "4b0a9d81"
        s.connected, s.pending, s.queued, s.rev = True, None, [], 0
        s.lock = threading.Lock()
        s.cursor_uuid, s.uuid_verified = "uuid-1", True
        s.death_probe_at, s.death_info, s.death_alerts = 0, None, {}
        s.agent_status_ts = 0
        s.processing_since = None
        s.last_reply_probe = None
        s.last_zhi_ts = 0
        s.messages = []
        return s

    def test_probe_attaches_grade_and_push_carries_advice(self):
        s = self._sess()
        err = {"code": "failed_precondition 29", "reason": "报错中断",
               "is_last": True, "bubble_id": "b1", "at_ts": time.time() - 60}
        pushed = []
        with (patch.object(hub, "read_cursor_error", lambda uid: err),
              patch.object(hub.Hub, "add_message",
                           lambda self, sess, m: sess.messages.append(m)),
              patch.object(hub.Hub, "_fusion_recent_activity",
                           lambda self, sess, now: None),
              patch.object(hub.Hub, "notify", lambda self: None),
              patch.object(hub.Hub, "_push_label", lambda self, sess: sess.name),
              patch.object(hub.Hub, "push_phone",
                           lambda self, title, body, **kw: pushed.append(body)),
              patch.object(hub.Hub, "_takeover_action", lambda self, sid: {}),
              patch.object(hub, "WORKFLOW", None)):
            hub.HUB._tick_death_probe(s, time.time())
        self.assertIsNotNone(s.death_info)
        self.assertTrue(s.death_info.get("revivable"))
        self.assertIn("重试", s.death_info.get("advice") or "")
        self.assertEqual(1, len(pushed), "判死必须推手机")
        self.assertIn("→", pushed[0])
        self.assertIn("重试", pushed[0], "推送要带下一步，人在外面才知道怎么救")

    def test_nameless_interrupt_does_not_push_phone(self):
        # 08-26 快编：对话被总结后留下 code=None「报错中断」，Bark 误报挂了
        s = self._sess()
        s.name = "快编·导出媒资"
        err = {"code": None, "reason": "报错中断",
               "is_last": True, "bubble_id": "b-none", "at_ts": time.time() - 60}
        pushed = []
        with (patch.object(hub, "read_cursor_error", lambda uid: err),
              patch.object(hub.Hub, "add_message",
                           lambda self, sess, m: sess.messages.append(m)),
              patch.object(hub.Hub, "_fusion_recent_activity",
                           lambda self, sess, now: None),
              patch.object(hub.Hub, "notify", lambda self: None),
              patch.object(hub.Hub, "push_phone",
                           lambda self, title, body, **kw: pushed.append(body)),
              patch.object(hub.Hub, "_takeover_action", lambda self, sid: {}),
              patch.object(hub, "WORKFLOW", None)):
            hub.HUB._tick_death_probe(s, time.time())
        self.assertIsNotNone(s.death_info)
        self.assertEqual([], pushed)


class ConcurrencyGateTests(unittest.TestCase):
    """并发闸=排队态，不是死（08-27 11:42-12:10 事故）。

    5+ tab 同时派活/接手，Cursor 平台连环拒新会话——hub 日志三种原话：
    「会话建立失败（错误码：1001）」「会话超过最大限制 4/4」「每分钟请求上限」。
    它们此前被当普通死因走判死：退卡（心理健康那张卡的「退回：会话判死」）、
    推「挂了」、工作流标卡重派；重派又开新会话，越派越堵——接手 c0cfa23a 的
    agent 连败 57 次、26 分钟后 tab 判死，用户看到的就是「消息发出去了没成功」。
    """

    def test_incident_payloads_classify_as_gate(self):
        import session_locator
        payloads = [
            {"error": "ERROR_CUSTOM_MESSAGE",
             "details": {"title": "failed_precondition",
                         "detail": "会话建立失败（错误码：1001）",
                         "isRetryable": False}},
            {"error": "ERROR_CUSTOM_MESSAGE",
             "details": {"title": "resource_exhausted",
                         "detail": "会话超过最大限制（4/4）"}},
            {"error": "ERROR_CUSTOM_MESSAGE",
             "details": {"title": "resource_exhausted",
                         "detail": "已达每分钟请求上限，请稍后再试"}},
        ]
        for p in payloads:
            info = session_locator._classify_error(p)
            self.assertTrue(info.get("gate"), p["details"]["detail"])
            self.assertIn("排队", info["reason"])

    def test_english_gate_wordings_also_match(self):
        import session_locator
        info = session_locator._classify_error(
            {"error": "ERROR_CUSTOM_MESSAGE",
             "details": {"title": "resource_exhausted",
                         "detail": "You have reached the maximum number of "
                                   "concurrent sessions"}})
        self.assertTrue(info.get("gate"))

    def test_ordinary_deaths_are_not_gate(self):
        import session_locator
        for err in (
            50,  # 欠费
            {"error": "ERROR_CUSTOM_MESSAGE",
             "details": {"title": "Switched to Opus 4.8",
                         "detail": "Fable 5 hit a safety filter"}},
        ):
            self.assertFalse(session_locator._classify_error(err).get("gate"),
                             "欠费/安全过滤器不是排队，误归会把真死瞒下来")

    def test_gate_is_transient_so_board_cards_stay(self):
        # 排队态退卡必误伤：agent 进来时卡已不在名下，交付被顶回（08-13 同款）
        self.assertTrue(session_core._is_transient_death(
            {"gate": True, "code": "ERROR_CUSTOM_MESSAGE",
             "reason": "Cursor 并发满/限频（排队等空位）"}))

    def test_gate_advice_beats_custom_message_revive_rule(self):
        # gate 的 code 也是 ERROR_CUSTOM_MESSAGE：不先拦一道会被「安全过滤器」
        # 规则劫走，建议「接手」——接手=再开会话，正是把事故越搅越堵的动作
        revivable, advice = session_core.classify_death(
            {"gate": True, "code": "ERROR_CUSTOM_MESSAGE",
             "reason": "Cursor 并发满/限频（排队等空位）",
             "detail": "会话建立失败（错误码：1001）"})
        self.assertTrue(revivable)
        self.assertIn("别再加派", advice)
        self.assertNotIn("安全过滤器", advice)


class GatewayTextGateTests(unittest.TestCase):
    """AI 网关（会员口令）的并发闸长得不一样：一条**正文**气泡，不是 errorDetails。

    09-03 17:25 无头开待命对话实测撞上：对话里唯一一条 AI 气泡是
    「并发窗口已满（2/2）：该口令同时打开的窗口已达上限，请关闭其他窗口后重试」，
    status 直接 completed。hub 只看 errorDetails，于是壳 tab 一直挂「重连中」到超时，
    没人知道那个对话根本没跑起来。且**它不会自动重试**，与 Cursor 自家闸口不同。
    """

    def test_short_plain_ai_bubble_with_gateway_wording_is_a_gate(self):
        import session_locator
        info = session_locator.gateway_text_rejection({
            "type": 2,
            "text": "并发窗口已满（2/2）：该口令同时打开的窗口已达上限，请关闭其他窗口后重试",
        })
        self.assertIsNotNone(info)
        self.assertTrue(info["gate"])
        self.assertEqual("gateway_text", info["gate_kind"])
        self.assertIn("网关", info["reason"])
        self.assertIn("不会自动重试", info["reason"])

    def test_user_bubbles_tool_calls_and_long_texts_are_not_gates(self):
        import session_locator
        wording = "并发窗口已满（2/2）：该口令同时打开的窗口已达上限"
        for bubble in (
            {"type": 1, "text": wording},                                  # 用户自己在说
            {"type": 2, "text": wording, "toolFormerData": {"name": "x"}},  # 工具调用步骤
            {"type": 2, "text": wording, "thinking": {"text": "…"}},        # 思考段
            {"type": 2, "text": wording + "。" + "我来分析一下这个限制。" * 30},  # 正常回复里引用
            {"type": 2, "text": "干完了"},                                   # 普通正文
            {"type": 2, "text": ""},
            None, "garbage",
        ):
            self.assertIsNone(session_locator.gateway_text_rejection(bubble), bubble)

    def test_advice_says_it_will_not_retry_itself(self):
        revivable, advice = session_core.classify_death(
            {"gate": True, "gate_kind": "gateway_text", "code": "GATEWAY_CONCURRENCY",
             "reason": "AI 网关并发满（口令名额用完，不会自动重试）",
             "detail": "并发窗口已满（2/2）：该口令同时打开的窗口已达上限"})
        self.assertTrue(revivable)
        self.assertIn("不会自动重试", advice)
        self.assertIn("重发", advice)
        self.assertNotIn("空位一出", advice)      # 那是 Cursor 自家闸口的话，这里会哄人

    def test_liveness_label_does_not_promise_auto_resume(self):
        s = hub.Session.__new__(hub.Session)
        s.id, s.name, s.conv_key = "s1", "待命·探针", "f9c313ed"
        s.connected, s.pending, s.queued, s.rev = False, None, [], 0
        s.lock = threading.Lock()
        s.cursor_uuid, s.uuid_verified = "uuid-gw", False
        s.death_info = {"gate": True, "gate_kind": "gateway_text",
                        "reason": "AI 网关并发满（口令名额用完，不会自动重试）",
                        "at_ts": time.time() - 30, "bubble_id": "b1"}
        s.agent_status, s.agent_status_ts = "", 0
        s.processing_since = None
        s.last_reply_probe = None
        s.last_zhi_ts = s.last_heartbeat = 0
        s.transcript_path, s.recon_deadline = None, None
        s.messages = []
        with (patch.object(hub, "read_cursor_activity", lambda uuid: {}),
              patch.object(hub.Hub, "board_write_ages", lambda h, x, t: (None, None))):
            r = hub.Api()._agent_liveness(s, time.time())
        self.assertEqual("recon", r["state"])
        self.assertIn("网关", r["label"])
        self.assertNotIn("空位自动续", r["label"])

    def test_read_cursor_error_picks_it_up_from_a_fake_db(self):
        import json as _json
        import sqlite3
        import tempfile
        import session_locator
        uid = "uuid-gw"
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "state.vscdb"
            con = sqlite3.connect(str(db))
            con.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
            con.execute("INSERT INTO cursorDiskKV VALUES (?,?)", (
                "composerData:" + uid,
                _json.dumps({"fullConversationHeadersOnly": [
                    {"bubbleId": "b1", "type": 1}, {"bubbleId": "b2", "type": 2}]})))
            con.execute("INSERT INTO cursorDiskKV VALUES (?,?)", (
                "bubbleId:{}:b1".format(uid), _json.dumps({"type": 1, "text": "报到词"})))
            con.execute("INSERT INTO cursorDiskKV VALUES (?,?)", (
                "bubbleId:{}:b2".format(uid),
                _json.dumps({"type": 2, "createdAt": "2026-09-03T09:25:27.283Z",
                             "text": "并发窗口已满（2/2）：该口令同时打开的窗口已达上限，请关闭其他窗口后重试"})))
            con.commit()
            con.close()
            with patch.object(session_locator, "_global_db", lambda appdata=None: db):
                info = session_locator.read_cursor_error(uid)
        self.assertIsNotNone(info)
        self.assertTrue(info["gate"])
        self.assertEqual("gateway_text", info["gate_kind"])
        self.assertTrue(info["is_last"])
        self.assertEqual("b2", info["bubble_id"])
        self.assertGreater(info["at_ts"], 0)

    def test_gateway_wording_not_in_last_bubble_is_ignored(self):
        # 只认最后一条：历史上撞过一次、后来缓过来接着干的对话不能被判成撞闸
        import json as _json
        import sqlite3
        import tempfile
        import session_locator
        uid = "uuid-gw2"
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "state.vscdb"
            con = sqlite3.connect(str(db))
            con.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
            con.execute("INSERT INTO cursorDiskKV VALUES (?,?)", (
                "composerData:" + uid,
                _json.dumps({"fullConversationHeadersOnly": [
                    {"bubbleId": "b1", "type": 2}, {"bubbleId": "b2", "type": 2}]})))
            con.execute("INSERT INTO cursorDiskKV VALUES (?,?)", (
                "bubbleId:{}:b1".format(uid),
                _json.dumps({"type": 2, "text": "并发窗口已满（2/2）：请关闭其他窗口后重试"})))
            con.execute("INSERT INTO cursorDiskKV VALUES (?,?)", (
                "bubbleId:{}:b2".format(uid), _json.dumps({"type": 2, "text": "好了，我接着干"})))
            con.commit()
            con.close()
            with patch.object(session_locator, "_global_db", lambda appdata=None: db):
                self.assertIsNone(session_locator.read_cursor_error(uid))


class GateProbeTests(unittest.TestCase):
    """tick_death_probe 撞并发闸：一次性明示「排队中」，其余死亡机关全不触发。"""

    def _sess(self):
        s = hub.Session.__new__(hub.Session)
        s.id, s.name, s.conv_key = "s1", "控制台·并发闸", "f9c313ed"
        s.connected, s.pending, s.queued, s.rev = True, None, [], 0
        s.lock = threading.Lock()
        s.cursor_uuid, s.uuid_verified = "uuid-gate", True
        s.death_probe_at, s.death_info, s.death_alerts = 0, None, {}
        s.agent_status, s.agent_status_ts = "", 0
        s.processing_since = None
        s.last_reply_probe = None
        s.last_zhi_ts = 0
        s.last_heartbeat = 0
        s.transcript_path, s.recon_deadline = None, None
        s.messages = []
        return s

    def _gate_err(self):
        return {"code": "ERROR_CUSTOM_MESSAGE", "gate": True,
                "reason": "Cursor 并发满/限频（排队等空位）",
                "title": "failed_precondition",
                "detail": "会话建立失败（错误码：1001）",
                "is_last": True, "bubble_id": "b-gate",
                "at_ts": time.time() - 60}

    def test_gate_probe_pushes_queue_note_and_skips_workflow(self):
        s = self._sess()
        pushed, kicked = [], []

        class _WF:
            def on_executor_dead(self, conv, reason):
                kicked.append(conv)
                return ""

        with (patch.object(hub, "read_cursor_error", lambda uid: self._gate_err()),
              patch.object(hub.Hub, "add_message",
                           lambda self, sess, m: sess.messages.append(m)),
              patch.object(hub.Hub, "_fusion_recent_activity",
                           lambda self, sess, now: None),
              patch.object(hub.Hub, "notify", lambda self: None),
              patch.object(hub.Hub, "_push_label", lambda self, sess: sess.name),
              patch.object(hub.Hub, "push_phone",
                           lambda self, title, body, **kw: pushed.append((body, kw))),
              patch.object(hub.Hub, "_takeover_action", lambda self, sid: {}),
              patch.object(hub, "WORKFLOW", _WF())):
            hub.HUB._tick_death_probe(s, time.time())
        self.assertTrue((s.death_info or {}).get("gate"))
        self.assertEqual([], kicked, "并发闸不能触发工作流重派——重派=再开会话加压")
        self.assertEqual(1, len(pushed), "要明示一次「排队中」，静默就是 08-27 的假失败")
        body, kw = pushed[0]
        self.assertIn("排队", body)
        self.assertNotIn("挂了", body)
        self.assertNotIn("skull", str(kw.get("tags") or ""))
        self.assertFalse(kw.get("extra_actions"),
                         "不给「一键派单」按钮：派单=再开会话，给闸口加压")

    def test_gate_liveness_shows_queueing_not_dead(self):
        s = self._sess()
        s.death_info = self._gate_err()
        api = hub.Api()
        with (patch.object(hub, "read_cursor_activity", lambda uuid: {}),
              patch.object(hub.Hub, "board_write_ages",
                           lambda h, x, t: (None, None))):
            r = api._agent_liveness(s, time.time())
        self.assertEqual("recon", r["state"], "报成 died 用户就去点接手，越接越堵")
        self.assertIn("排队中", r["label"])

    def test_real_death_still_reported_after_gate_change(self):
        # 回归护栏：欠费这类真死不许被排队改动误吞
        s = self._sess()
        s.death_info = {"code": 50, "reason": "账号欠费",
                        "at_ts": time.time() - 60, "bubble_id": "b50"}
        api = hub.Api()
        with (patch.object(hub, "read_cursor_activity", lambda uuid: {}),
              patch.object(hub.Hub, "board_write_ages",
                           lambda h, x, t: (None, None))):
            r = api._agent_liveness(s, time.time())
        self.assertEqual("died", r["state"])
        self.assertIn("已挂", r["label"])


class PushLabelTests(unittest.TestCase):
    def _sess(self, **kw):
        s = hub.Session.__new__(hub.Session)
        s.name = kw.get("name", "快编·导出媒资")
        s.cwd = kw.get("cwd", r"d:\桌面\working\cursor工作流")
        s.task_root = kw.get("task_root", s.cwd)
        s.task_root_locked = kw.get("task_root_locked", False)
        s.agent_project = kw.get("agent_project", "快编")
        s.conv_key = "be748934"
        return s

    def test_fast_edit_in_console_ws_does_not_prefix_cursor_workflow(self):
        s = self._sess()
        hub.HUB.cfg["team_assign"] = {}
        self.assertEqual("快编·导出媒资", hub.HUB._push_label(s))

    def test_unnamed_tab_still_shows_folder(self):
        s = self._sess(name="未命名", agent_project="")
        hub.HUB.cfg["team_assign"] = {}
        self.assertEqual("cursor工作流 · 未命名", hub.HUB._push_label(s))


if __name__ == "__main__":
    unittest.main(verbosity=2)
