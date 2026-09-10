"""Cross-client regressions: a Codex task must never inherit a Cursor window."""
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runtime_adapter as runtime

THREAD = "11111111-1111-4111-8111-111111111111"


def session(**kw):
    data = dict(runtime_kind="codex", native_thread_id=THREAD, cursor_uuid=None,
                connected=True, last_heartbeat=1000, agent_status_ts=900,
                last_zhi_ts=0, processing_since=None, agent_status="developing",
                pending=None, buffered_reply=None, wait_deferred=False)
    data.update(kw)
    return SimpleNamespace(**data)


class RuntimeIdentityTests(unittest.TestCase):
    def test_codex_takeover_discards_old_cursor_binding(self):
        s = session(runtime_kind="cursor", cursor_uuid="cursor-id", ext_instance="window-a",
                    transcript_path="cursor.jsonl", uuid_verified=True, death_info={"reason": "old"})
        runtime.apply_metadata(s, {"runtime_kind": "codex", "native_thread_id": THREAD})
        self.assertEqual("codex", s.runtime_kind)
        self.assertEqual(THREAD, s.native_thread_id)
        self.assertIsNone(s.cursor_uuid)
        self.assertFalse(s.uuid_verified)
        self.assertIsNone(s.transcript_path)
        self.assertEqual("", s.ext_instance)
        self.assertIsNone(s.death_info)

    def test_other_clients_dont_get_assigned_cursor_capabilities(self):
        for kind in ("codex", "chatgpt", "unknown"):
            caps = runtime.capabilities(session(runtime_kind=kind))
            self.assertFalse(caps["cursor_window"])
        self.assertTrue(runtime.capabilities(session(runtime_kind="cursor"))["cursor_window"])

    def test_invalid_native_thread_id_is_not_a_file_path(self):
        s = session(native_thread_id="")
        runtime.apply_metadata(s, {"runtime_kind": "codex", "native_thread_id": "../../auth.json"})
        self.assertEqual("", s.native_thread_id)

    def test_legacy_messages_do_not_erase_native_identity(self):
        s = session()
        runtime.apply_metadata(s, {"status": "testing"})
        self.assertEqual("codex", s.runtime_kind)
        self.assertEqual(THREAD, s.native_thread_id)

    def test_native_rebind_drops_previous_tasks_model(self):
        s = session(reported_model="old-model", model_info={"label": "old-model"})
        runtime.apply_metadata(s, {"runtime_kind": "codex",
            "native_thread_id": "01a07f11-d60a-77e1-8167-eae2baad0961"})
        self.assertEqual("", s.reported_model)
        self.assertIsNone(s.model_info)

    def test_rebinding_during_log_read_cannot_cache_previous_thread(self):
        s = session()
        def rebind(*args, **kwargs):
            runtime.apply_metadata(s, {"runtime_kind": "codex",
                "native_thread_id": "01a07f11-d60a-77e1-8167-eae2baad0961"})
            return {"uuid": THREAD}
        with patch.object(runtime, "_read_codex_turn", side_effect=rebind):
            self.assertIsNone(runtime.read_native_turn(s, now=1000))
        self.assertIsNone(s.native_turn_view)


class NativeLivenessTests(unittest.TestCase):
    def test_shared_daemon_heartbeat_cannot_keep_codex_online(self):
        s = session(agent_status_ts=1, last_zhi_ts=1)
        with patch.object(runtime, "read_native_turn", return_value=None):
            view = runtime.native_liveness(s, 1000)
        self.assertNotIn(view["state"], ("online", "working", "waiting"))
        self.assertFalse(view["sure"])

    def test_last_task_completed_beats_frozen_generating_signal(self):
        s = session(agent_status_ts=800)
        turn = dict(terminal=True, generating=False, updated_at=990, status="completed")
        with patch.object(runtime, "read_native_turn", return_value=turn):
            view = runtime.native_liveness(s, 1000)
        self.assertEqual("turn_done", view["state"])
        self.assertTrue(view["sure"])

    def test_fresh_report_after_completed_turn_is_new_activity(self):
        s = session(agent_status_ts=999)
        turn = dict(terminal=True, generating=False, updated_at=990, status="completed")
        with patch.object(runtime, "read_native_turn", return_value=turn):
            view = runtime.native_liveness(s, 1000)
        self.assertEqual("online", view["state"])
        self.assertFalse(view["sure"])

    def test_silent_long_native_turn_is_unknown_not_dead_or_stalled(self):
        s = session(agent_status_ts=1)
        with patch.object(runtime, "read_native_turn", return_value={
                "terminal": False, "generating": True, "updated_at": 500}):
            view = runtime.native_liveness(s, 1000)
        self.assertEqual("unknown", view["state"])
        self.assertFalse(view["sure"])

    def test_recent_rollout_is_activity_evidence_not_live_process_confirmation(self):
        s = session(agent_status_ts=1)
        with patch.object(runtime, "read_native_turn", return_value={
                "terminal": False, "generating": True, "updated_at": 995}):
            view = runtime.native_liveness(s, 1000)
        self.assertEqual("working", view["state"])
        self.assertFalse(view["sure"])

    def test_old_deferred_question_is_not_proof_of_online(self):
        s = session(agent_status_ts=1, pending={"options": ["继续"]}, wait_deferred=True)
        with patch.object(runtime, "read_native_turn", return_value=None):
            view = runtime.native_liveness(s, 1000)
        self.assertNotEqual("waiting", view["state"])
        self.assertTrue(any("待回复" in v for v in view["evidence"]))

    def test_cloud_chatgpt_never_reads_local_codex_rollouts(self):
        s = session(runtime_kind="chatgpt", native_thread_id="remote-thread")
        with patch.object(runtime, "_read_codex_turn") as read:
            self.assertIsNone(runtime.read_native_turn(s))
        read.assert_not_called()


class HubNativeIntegrationTests(unittest.TestCase):
    def test_codex_link_opener_rejects_other_protocols(self):
        import hub
        import hub_api
        with patch.object(hub_api.os, "startfile", create=True) as launch:
            for url in ("file:///C:/Windows/System32/cmd.exe", "https://example.com", "codex://threads/../../bad"):
                self.assertFalse(hub.Api().open_codex_link(url)["ok"])
            launch.assert_not_called()
            result = hub.Api().open_codex_link("codex://threads/" + THREAD)
            self.assertTrue(result["ok"])
            self.assertFalse(result["dispatched"])
            launch.assert_called_once_with("codex://threads/" + THREAD)

    def test_native_identity_survives_snapshot_without_cursor_binding_or_cache(self):
        import hub
        import session_core
        snapshot = dict(id="native", name="任务", cwd="", conv_key="original",
                        runtime_kind="codex", native_thread_id=THREAD,
                        cursor_uuid="stale-cursor", uuid_verified=True,
                        transcript_path="stale.jsonl", messages=[])
        with patch.object(hub.HUB, "_bind_registered_team_root"), \
             patch.object(hub, "heal_session_paths"), \
             patch.object(hub, "heal_named_agent_root"):
            s = session_core.session_from_snapshot(hub.HUB, snapshot)
        self.assertEqual("codex", runtime.kind(s))
        self.assertEqual(THREAD, s.native_thread_id)
        self.assertIsNone(s.cursor_uuid)
        self.assertFalse(s.uuid_verified)
        s.native_turn_cache = {"private_large_cache": "not persisted"}
        saved = session_core.session_to_snapshot(hub.HUB, s)
        self.assertEqual("codex", saved["runtime_kind"])
        self.assertEqual(THREAD, saved["native_thread_id"])
        self.assertNotIn("native_turn_cache", saved)

    def test_native_resume_ledger_survives_snapshot_and_is_bounded(self):
        import hub
        import session_core
        snapshot = dict(id="native", name="任务", cwd="", conv_key="original",
                        runtime_kind="codex", native_thread_id=THREAD, messages=[],
                        native_resume_ledger=[{"source_turn_id": str(i)} for i in range(12)])
        with patch.object(hub.HUB, "_bind_registered_team_root"), \
             patch.object(hub, "heal_session_paths"), \
             patch.object(hub, "heal_named_agent_root"):
            restored = session_core.session_from_snapshot(hub.HUB, snapshot)
        self.assertEqual(8, len(restored.native_resume_ledger))
        saved = session_core.session_to_snapshot(hub.HUB, restored)
        self.assertEqual([str(i) for i in range(4, 12)],
                         [item["source_turn_id"] for item in saved["native_resume_ledger"]])

    def test_old_snapshot_still_uses_cursor_identity(self):
        import hub
        import session_core
        with patch.object(hub.HUB, "_bind_registered_team_root"), \
             patch.object(hub, "heal_session_paths"), \
             patch.object(hub, "heal_named_agent_root"):
            s = session_core.session_from_snapshot(hub.HUB, {
                "id": "legacy", "cursor_uuid": "original-cursor", "messages": []})
        self.assertEqual("cursor", runtime.kind(s))
        self.assertEqual("original-cursor", s.cursor_uuid)

    def test_codex_is_never_bound_to_unique_cursor_window_in_same_project(self):
        import hub
        s = session(id="native", cwd="C:/project", ext_instance="", archived=False)
        with patch.object(hub.HUB, "order", [s.id]), \
             patch.object(hub.HUB, "sessions", {s.id: s}), \
             patch.object(hub.HUB, "_ext_bind_ts", 0, create=True), \
             patch("ext_bus.live_instances", return_value=[{"id": "cursor", "workspace": s.cwd}]):
            hub.HUB._tick_window_bind(1000)
        self.assertEqual("", s.ext_instance)

    def test_codex_timeline_does_not_read_cursor_database_or_hook(self):
        import hub
        s = session(id="native")
        with patch.object(hub.HUB, "sessions", {s.id: s}), \
             patch.object(runtime, "read_native_turn", return_value={"runtime_kind": "codex"}), \
             patch.object(hub.cursor_turns, "read_turn") as cursor:
            view = hub.Api().get_live_turn(s.id)
        self.assertTrue(view["ok"])
        self.assertEqual("codex", view["turn"]["runtime_kind"])
        cursor.assert_not_called()

    def test_native_answer_checks_current_binding_and_never_falls_back_to_queue(self):
        import hub
        import codex_desktop
        s = session(id="native", lock=threading.RLock())
        with patch.object(hub.HUB, "sessions", {s.id: s}), \
             patch.object(codex_desktop, "answer", return_value={"ok": True}) as answer:
            api = hub.Api()
            self.assertFalse(api.answer_native_question(s.id, "other-thread", "turn", "call", {})["ok"])
            answer.assert_not_called()
            self.assertTrue(api.answer_native_question(s.id, THREAD, "turn", "call", {"q": "text"})["ok"])
            answer.assert_called_once_with(THREAD, "turn", "call", {"q": "text"})

    def test_native_text_checks_binding_rejects_attachments_and_uses_desktop_only(self):
        import hub
        import codex_desktop
        s = session(id="native", lock=threading.RLock())
        with patch.object(hub.HUB, "sessions", {s.id: s}), \
             patch.object(codex_desktop, "send_text", return_value={"ok": True}) as send:
            api = hub.Api()
            self.assertFalse(api.send_native_text(
                s.id, "other", "turn", "hello", "delivery")["ok"])
            self.assertFalse(api.send_native_text(
                s.id, THREAD, "turn", "hello", "delivery", files=[{"name": "x"}])["ok"])
            malformed = api.send_native_text(
                s.id, THREAD, "turn", ["not", "text"], "delivery")
            self.assertFalse(malformed["ok"])
            self.assertIn("文字", malformed["error"])
            send.assert_not_called()
            self.assertTrue(api.send_native_text(
                s.id, THREAD, "turn", "hello", "delivery", who="手机")["ok"])
            send.assert_called_once_with(THREAD, "turn", "[手机] hello", "delivery")

    def test_native_resume_keeps_binding_and_persists_before_one_dispatch(self):
        import hub
        import codex_desktop
        source_turn = "22222222-2222-4222-8222-222222222222"
        new_turn = "33333333-3333-4333-8333-333333333333"
        s = session(id="native", conv_key="native-conv", lock=threading.RLock(),
                    pending=None, native_resume_ledger=[], rev=2)
        events = []

        def fake_resume(thread_id, turn_id, delivery_id, reserve, settle):
            events.append("reserve")
            reserved = reserve(source_turn, delivery_id)
            if not reserved.get("ok"):
                return reserved
            events.append("dispatch")
            settle(source_turn, delivery_id, "native_started", new_turn)
            return {"ok": True, "delivery": "native_started",
                    "source_turn_id": source_turn, "turn_id": new_turn}

        with patch.object(hub.HUB, "sessions", {s.id: s}), \
             patch.object(hub.HUB, "save_state", side_effect=lambda: events.append("save")), \
             patch.object(hub.HUB, "notify"), patch.object(hub, "log_event"), \
             patch.object(hub.Api, "_persisted_resume_entry", return_value=True), \
             patch.object(hub.Api, "open_codex_link", return_value={"ok": True}) as opened, \
             patch.object(codex_desktop, "resume_thread", side_effect=fake_resume) as resume:
            first = hub.Api().resume_native_task(s.id, THREAD)
            second = hub.Api().resume_native_task(s.id, THREAD)
        self.assertTrue(first["ok"], first)
        self.assertFalse(second["ok"])
        self.assertTrue(second["resume_locked"])
        self.assertEqual(1, events.count("dispatch"))
        self.assertLess(events.index("save"), events.index("dispatch"))
        self.assertEqual("native_started", s.native_resume_ledger[0]["state"])
        self.assertEqual(new_turn, s.native_resume_ledger[0]["new_turn_id"])
        self.assertEqual(2, opened.call_count)
        self.assertEqual(2, resume.call_count)

    def test_native_resume_unknown_is_locked_across_snapshot_restore(self):
        import hub
        import codex_desktop
        import session_core
        source_turn = "22222222-2222-4222-8222-222222222222"
        s = session(id="native", conv_key="native-conv", name="任务", cwd="",
                    lock=threading.RLock(), pending=None, native_resume_ledger=[],
                    rev=2, messages=[])

        def fake_resume(_thread, _turn, delivery_id, reserve, settle):
            reserved = reserve(source_turn, delivery_id)
            if not reserved.get("ok"):
                return reserved
            settle(source_turn, delivery_id, "unknown", "")
            return {"ok": False, "delivery_unknown": True,
                    "source_turn_id": source_turn, "error": "结果未知"}

        with patch.object(hub.HUB, "sessions", {s.id: s}), \
             patch.object(hub.HUB, "save_state"), patch.object(hub.HUB, "notify"), \
             patch.object(hub.Api, "_persisted_resume_entry", return_value=True), \
             patch.object(hub.Api, "open_codex_link", return_value={"ok": True}), \
             patch.object(codex_desktop, "resume_thread", side_effect=fake_resume):
            first = hub.Api().resume_native_task(s.id, THREAD)
        self.assertTrue(first["delivery_unknown"])
        snapshot = dict(id="native", name="任务", cwd="", conv_key="native-conv",
                        runtime_kind="codex", native_thread_id=THREAD, messages=[],
                        native_resume_ledger=[dict(item) for item in s.native_resume_ledger])
        with patch.object(hub.HUB, "_bind_registered_team_root"), \
             patch.object(hub, "heal_session_paths"), \
             patch.object(hub, "heal_named_agent_root"):
            restored = session_core.session_from_snapshot(hub.HUB, snapshot)
        restored.lock = threading.RLock()
        with patch.object(hub.HUB, "sessions", {restored.id: restored}), \
             patch.object(hub.HUB, "save_state"), \
             patch.object(hub.Api, "_persisted_resume_entry", return_value=True), \
             patch.object(hub.Api, "open_codex_link", return_value={"ok": True}), \
             patch.object(codex_desktop, "resume_thread", side_effect=fake_resume):
            second = hub.Api().resume_native_task(restored.id, THREAD)
        self.assertFalse(second["ok"])
        self.assertTrue(second["resume_locked"])
        self.assertTrue(second["delivery_unknown"])

    def test_native_resume_rejects_pending_and_changed_binding_before_desktop(self):
        import hub
        import codex_desktop
        s = session(id="native", lock=threading.RLock(), pending={"id": "q"},
                    native_resume_ledger=[], rev=0)
        with patch.object(hub.HUB, "sessions", {s.id: s}), \
             patch.object(hub.Api, "open_codex_link") as opened, \
             patch.object(codex_desktop, "resume_thread") as resume:
            self.assertFalse(hub.Api().resume_native_task(s.id, THREAD)["ok"])
            self.assertFalse(hub.Api().resume_native_task(s.id, "other")["ok"])
        opened.assert_not_called()
        resume.assert_not_called()

    def test_native_resume_completed_answer_does_not_disable_button(self):
        import hub
        s = session(native_resume_ledger=[])
        view = {"turn_id": "t", "terminal": True,
                "native_questions": [{"status": "answered"}]}
        self.assertEqual("", hub.Api._native_resume_reason(s, view))
        view["native_questions"][0]["status"] = "pending"
        self.assertIn("待回答", hub.Api._native_resume_reason(s, view))

    def test_native_resume_detached_report_can_resume_but_real_decisions_cannot(self):
        import hub
        s = session(pending={"id": "report", "message": "上轮交付"},
                    detached=True, native_resume_ledger=[])
        self.assertEqual("", hub.Api._native_resume_pending_reason(s))
        s.pending["options"] = ["A", "B"]
        self.assertIn("待回答", hub.Api._native_resume_pending_reason(s))
        s.pending.pop("options")
        s.buffered_reply = {"user_input": "用户新要求"}
        self.assertIn("等待领取", hub.Api._native_resume_pending_reason(s))

    def test_native_resume_retires_detached_report_only_after_native_accepts(self):
        import hub
        import codex_desktop
        for outcome in ("native_started", "unknown"):
            with self.subTest(outcome=outcome):
                pending = {"id": "report", "message": "已完成阶段"}
                s = session(id="native", conv_key="native-conv", lock=threading.RLock(),
                            pending=pending, detached=True, wait_deferred=True,
                            native_resume_ledger=[], rev=0)

                def fake_resume(_thread, _turn, delivery, reserve, settle):
                    reserved = reserve("old-turn", delivery)
                    self.assertTrue(reserved["ok"], reserved)
                    self.assertIs(s.pending, pending)
                    settle("old-turn", delivery, outcome,
                           "new-turn" if outcome == "native_started" else "")
                    return {"ok": outcome == "native_started"}

                with patch.object(hub.HUB, "sessions", {s.id: s}), \
                     patch.object(hub.HUB, "save_state"), patch.object(hub.HUB, "notify"), \
                     patch.object(hub, "log_event"), \
                     patch.object(hub.Api, "_persisted_resume_entry", return_value=True), \
                     patch.object(hub.Api, "open_codex_link", return_value={"ok": True}), \
                     patch.object(codex_desktop, "resume_thread", side_effect=fake_resume):
                    hub.Api().resume_native_task(s.id, THREAD)
                if outcome == "native_started":
                    self.assertIsNone(s.pending)
                    self.assertFalse(s.wait_deferred)
                else:
                    self.assertIs(s.pending, pending)

    def test_native_text_retires_a_detached_wait_only_after_success(self):
        import hub
        import codex_desktop
        pending = {"id": "q1", "message": "进度"}
        buffered = {"user_input": "先前回复", "selected_options": ["继续"]}
        s = session(id="native", conv_key="native-conv", lock=threading.RLock(),
                    pending=pending, buffered_reply=buffered, wait_deferred=True,
                    detached=True, detached_since=10, processing_since=None,
                    last_reply_ts=0, rev=7)
        with patch.object(hub.HUB, "sessions", {s.id: s}), \
             patch.object(hub.HUB, "notify") as notify, \
             patch.object(hub, "log_event"), \
             patch.object(codex_desktop, "send_text", return_value={
                 "ok": True, "delivery": "native_started"}) as send:
            result = hub.Api().send_native_text(
                s.id, THREAD, "turn", "补充说明", "delivery")
        self.assertTrue(result["resolved_deferred_wait"])
        send.assert_called_once_with(
            THREAD, "turn", "先前回复\n选择的选项: 继续\n\n[补充] 补充说明", "delivery")
        self.assertIsNone(s.pending)
        self.assertIsNone(s.buffered_reply)
        self.assertFalse(s.wait_deferred)
        self.assertFalse(s.detached)
        self.assertIsNone(s.detached_since)
        self.assertIsNotNone(s.processing_since)
        self.assertEqual(8, s.rev)
        notify.assert_called_once()

    def test_native_text_serializes_a_concurrent_detached_reply(self):
        import hub
        import codex_desktop
        entered = threading.Event()
        release = threading.Event()
        pending = {"id": "q1", "message": "进度"}
        s = session(id="native", conv_key="native-conv", lock=threading.Lock(),
                    pending=pending,
                    buffered_reply={"user_input": "先前回复", "selected_options": []},
                    wait_deferred=True, detached=True, detached_since=10,
                    processing_since=None, last_reply_ts=0, rev=7)
        native_result = {}
        concurrent_result = {}

        def fake_send(*_args):
            entered.set()
            self.assertTrue(release.wait(2), "测试未释放原生发送")
            return {"ok": True, "delivery": "native_steered"}

        api = hub.Api()
        with patch.object(hub.HUB, "sessions", {s.id: s}), \
             patch.object(hub.HUB, "notify"), \
             patch.object(hub, "log_event"), \
             patch.object(codex_desktop, "send_text", side_effect=fake_send):
            native = threading.Thread(target=lambda: native_result.update(
                api.send_native_text(s.id, THREAD, "turn", "本次补充", "delivery")))
            native.start()
            self.assertTrue(entered.wait(1), "原生发送没有进入 IPC")
            concurrent = threading.Thread(target=lambda: concurrent_result.update(
                api.send_reply(s.id, "并发新回复", [], [], False)))
            concurrent.start()
            concurrent.join(0.05)
            self.assertTrue(concurrent.is_alive(), "并发回复不得越过原生发送的会话锁")
            release.set()
            native.join(2)
            concurrent.join(2)

        self.assertTrue(native_result["ok"])
        self.assertTrue(native_result["resolved_deferred_wait"])
        self.assertFalse(concurrent_result["ok"])
        self.assertIn("当前没有等待回复", concurrent_result["error"])
        self.assertIsNone(s.pending)
        self.assertIsNone(s.buffered_reply)

    def test_native_text_does_not_retire_an_active_or_failed_wait(self):
        import hub
        import codex_desktop
        for detached, result in ((False, {"ok": True}),
                                 (True, {"ok": False, "uncertain": True})):
            with self.subTest(detached=detached, result=result):
                pending = {"id": "q1", "message": "进度"}
                s = session(id="native", conv_key="native-conv", lock=threading.RLock(),
                            pending=pending, buffered_reply=None,
                            wait_deferred=detached, detached=detached,
                            detached_since=10 if detached else None,
                            processing_since=None, last_reply_ts=0, rev=3)
                with patch.object(hub.HUB, "sessions", {s.id: s}), \
                     patch.object(hub.HUB, "notify") as notify, \
                     patch.object(codex_desktop, "send_text", return_value=result):
                    got = hub.Api().send_native_text(
                        s.id, THREAD, "turn", "hello", "delivery")
                self.assertEqual(result["ok"] if detached else False, got["ok"])
                self.assertIs(pending, s.pending)
                self.assertEqual(detached, s.wait_deferred)
                self.assertEqual(detached, s.detached)
                self.assertEqual(3, s.rev)
                notify.assert_not_called()

    def test_sidebar_reads_only_the_bound_native_turn_cache(self):
        import hub
        s = session(messages=[], native_turn_view_id=THREAD,
                    native_turn_view={"steps": [{"kind": "text", "text": "原生最新进展"}]})
        self.assertEqual(("agent", "原生最新进展"), hub.Api()._sidebar_last_chat(s))
        s.native_thread_id = "another"
        self.assertEqual(("", ""), hub.Api()._sidebar_last_chat(s))

    def test_native_transfer_prepares_full_context_without_retiring_source(self):
        from urllib.parse import parse_qs, urlparse
        import hub
        with tempfile.TemporaryDirectory() as directory:
            s = session(id="native", conv_key="original", name="测试·接续", cwd=directory,
                        task_root=directory, lock=threading.Lock(), file_path="",
                        messages=[{"role": "user", "html": "保留完整上下文"}], archived=False)
            with patch.object(hub.HUB, "sessions", {s.id: s}), \
                 patch.object(hub, "DATA_DIR", Path(directory)):
                view = hub.Api().prepare_native_transfer(s.id)
            self.assertTrue(view["ok"], view)
            self.assertFalse(view["dispatched"])
            self.assertFalse(s.archived)
            self.assertEqual("native", view["model_selection"])
            parsed = urlparse(view["url"])
            self.assertEqual("codex", parsed.scheme)
            self.assertEqual(directory, parse_qs(parsed.query)["path"][0])
            self.assertIn("保留完整上下文", Path(view["path"]).read_text(encoding="utf-8"))
            self.assertIn("来源原生任务 ID", view["prompt"])
            self.assertNotIn("必须先调用 `zhi`", view["prompt"])

    def test_new_native_draft_does_not_reuse_or_retire_existing_sessions(self):
        from urllib.parse import parse_qs, urlparse
        import hub
        with tempfile.TemporaryDirectory() as directory:
            original = {"existing": session(archived=False)}
            with patch.object(hub.HUB, "sessions", original), patch.object(hub, "DATA_DIR", Path(directory)):
                view = hub.Api().prepare_native_new(directory, "独立任务")
                query = parse_qs(urlparse(view["url"]).query)
                self.assertEqual("独立任务", query["prompt"][0])
                self.assertFalse(view["dispatched"])
                self.assertEqual({"path", "prompt"}, set(query))
                self.assertEqual(["existing"], list(hub.HUB.sessions))
                self.assertFalse(original["existing"].archived)
                long_view = hub.Api().prepare_native_new(directory, "长任务" * 2000)
                self.assertEqual("长任务" * 2000, Path(long_view["path"]).read_text(encoding="utf-8"))
                self.assertLess(len(long_view["url"]), 1800)
                self.assertFalse(hub.Api().prepare_native_new("relative", "test")["ok"])

    def test_new_native_task_opens_saved_settings_and_empty_prompt_starts_no_turn(self):
        import hub
        shell = SimpleNamespace(id="native-session", runtime_kind="cursor",
                                native_thread_id="", recon_deadline=1, end_reason="")
        with tempfile.TemporaryDirectory() as directory, \
             patch("codex_desktop.create_configured_thread", return_value={
                 "thread_id": THREAD, "model": "gpt-6-astra", "effort": "max"}) as create, \
             patch("codex_desktop.start_initial_text") as start, \
             patch.object(hub.HUB, "create_spawn_shell", return_value=shell) as spawn, \
             patch.object(hub.HUB, "save_state"), \
             patch.object(hub.Api, "open_codex_link",
                          return_value={"ok": True, "dispatched": False}) as opened:
            result = hub.Api().create_native_task(
                directory, "", "gpt-6-astra", "max")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["created"])
        self.assertFalse(result["dispatched"])
        self.assertEqual("native-session", result["session_id"])
        self.assertEqual("codex", shell.runtime_kind)
        self.assertEqual(THREAD, shell.native_thread_id)
        self.assertIsNone(shell.recon_deadline)
        create.assert_called_once_with(directory, "gpt-6-astra", "max")
        self.assertEqual("codex-native-" + THREAD, spawn.call_args.args[0])
        opened.assert_called_once_with("codex://threads/" + THREAD)
        start.assert_not_called()

    def test_new_native_task_reports_exact_unsent_state_without_retry(self):
        import hub
        shell = SimpleNamespace(id="native-session", runtime_kind="cursor",
                                native_thread_id="", recon_deadline=1, end_reason="")
        with tempfile.TemporaryDirectory() as directory, \
             patch("codex_desktop.create_configured_thread", return_value={
                 "thread_id": THREAD, "model": "gpt-6-astra", "effort": "max"}), \
             patch("codex_desktop.start_initial_text", return_value={
                 "ok": False, "delivery_unknown": True,
                 "error": "结果尚未确认；不会自动重发"}) as start, \
             patch.object(hub.HUB, "create_spawn_shell", return_value=shell), \
             patch.object(hub.HUB, "save_state"), \
             patch.object(hub.Api, "open_codex_link", return_value={"ok": True}):
            result = hub.Api().create_native_task(
                directory, "执行任务", "gpt-6-astra", "max")
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["created"])
        self.assertFalse(result["dispatched"])
        self.assertTrue(result["delivery_unknown"])
        self.assertEqual("native-session", result["session_id"])
        self.assertIn("不会自动重发", result["warning"])
        self.assertEqual(1, start.call_count)

    def test_new_native_task_keeps_unknown_setup_thread_id_and_does_not_open(self):
        import hub
        import codex_desktop
        error = codex_desktop.NewTaskSetupError("设置未知", THREAD)
        with tempfile.TemporaryDirectory() as directory, \
             patch("codex_desktop.create_configured_thread", side_effect=error), \
             patch.object(hub.Api, "open_codex_link") as opened:
            result = hub.Api().create_native_task(
                directory, "", "gpt-6-astra", "max")
        self.assertFalse(result["ok"])
        self.assertTrue(result["created"])
        self.assertTrue(result["config_unknown"])
        self.assertEqual(THREAD, result["thread_id"])
        opened.assert_not_called()

    def test_transfer_model_reaches_cursor_batch_open(self):
        import hub
        source = session(id="source", name="任务", cwd="C:/project", task_root="C:/project", rev=0)
        chosen = {"model": "grok-test", "parameters": {"effort": "high"}}
        response = {"ok": True, "opened": [{"name": "new", "session_id": "new",
                    "composerId": "composer", "conversation_id": "cid"}]}
        with patch.object(hub.HUB, "sessions", {source.id: source}), \
             patch.object(hub.Api, "_ext_pick_instance", return_value=({"id": "window", "label": "win"}, "")), \
             patch.object(hub.Api, "ext_batch_open", return_value=response) as open_batch, \
             patch.object(hub.HUB, "add_message"), patch.object(hub, "log_event"), \
             patch("hub_api.threading.Thread"):
            result = hub.Api().ext_takeover_new(source.id, "window", model=chosen)
        self.assertTrue(result["ok"], result)
        self.assertEqual([chosen], open_batch.call_args.kwargs["models"])

    def test_cursor_source_uses_native_prompt_for_codex_recipient(self):
        import hub
        source = session(id="source", runtime_kind="cursor", conv_key="original",
                         name="测试", lock=threading.Lock(), messages=[], file_path="")
        with patch.object(hub.HUB, "sessions", {source.id: source}):
            result = hub.Api().get_takeover_prompt(source.id, target_runtime="codex")
        self.assertIn("按当前宿主", result["prompt"])
        self.assertNotIn("CallMcpTool", result["prompt"])
        self.assertNotIn("KEEPALIVE", result["prompt"])

    def test_native_batch_prompt_preserves_recipient_conversation_id(self):
        text = runtime.takeover_prompt(session(conv_key="original", name="测试"), "记录", stay_put=True)
        self.assertIn("全程沿用你当前会话的 conversation_id", text)
        self.assertNotIn("接手后继续使用此 ID", text)

    def test_native_batch_deferred_reply_is_queued_until_next_collection(self):
        import hub
        for deferred in (True, False):
            with self.subTest(deferred=deferred):
                target = session(id="target", name="接手方", cwd="C:/project",
                                 pending={"message": "进度"}, wait_deferred=deferred)
                prepared = {"ok": True, "prompt": "合并接续", "items": [
                    {"sid": "a", "name": "任务一"}, {"sid": "b", "name": "任务二"}]}
                with patch.object(hub.HUB, "sessions", {target.id: target}), \
                     patch.object(hub.Api, "_digest_already_sent", return_value=False), \
                     patch.object(hub.Api, "get_takeover_prompt_many", return_value=prepared) as prepare, \
                     patch.object(hub.Api, "send_reply", return_value={"ok": True}) as send, \
                     patch.object(hub.Api, "_commit_multi_takeover"), \
                     patch.object(hub, "log_event") as log:
                    result = hub.Api().share_takeover_many(["a", "b"], target.id)
                self.assertTrue(result["ok"])
                self.assertEqual(deferred, result["queued"])
                self.assertEqual("codex", prepare.call_args.kwargs["target_runtime"])
                send.assert_called_once()
                self.assertIn("排队" if deferred else "当场答复", log.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
