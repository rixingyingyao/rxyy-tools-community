"""Regression tests for native desktop streaming and exact question replies."""
import copy
import json
import sys
import threading
import unittest
import uuid
from unittest.mock import Mock, patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_desktop as desktop

THREAD = "01a00000-0000-7000-8000-000000000001"
TURN = "01a00000-0000-7000-8000-000000000002"
CALL = "call_question"
QUESTION = json.dumps(["request_user_input_async", CALL, 0], separators=(",", ":"))


def fixture():
    turn = {"turnId": TURN, "status": "inProgress", "items": [
        {"type": "agentMessage", "id": CALL, "delivery": "async", "text": "",
         "questions": [{"title": "选择输出格式", "options": ["Markdown", "JSON"]}]}]}
    return {"id": THREAD, "cwd": "D:/project", "requests": [], "turnHistory": {
        "kind": "canonical", "history": {"entitiesByKey": {"last": turn},
        "islands": [{"entries": [{"value": "last"}]}]}}}


class DesktopStateTests(unittest.TestCase):
    def test_canonical_history_and_incremental_text_updates(self):
        state = fixture()
        path = ["turnHistory", "history", "entitiesByKey", "last", "items"]
        desktop.apply_patches(state, [
            {"op": "add", "path": path + [1], "value": {"type": "agentMessage", "text": "progress"}},
            {"op": "replace", "path": path + [1, "text"], "value": "progress complete"}])
        self.assertEqual("progress complete", desktop.current_turn(state)["items"][-1]["text"])
        desktop.apply_patches(state, [{"op": "remove", "path": path + [1]}])
        self.assertEqual(1, len(desktop.current_turn(state)["items"]))

    def test_array_gap_and_non_patch_are_rejected(self):
        for patch in ({"op": "add", "path": ["items", 10], "value": 1},
                      {"op": "move", "path": []}):
            with self.assertRaises(ValueError):
                desktop.apply_patches({"items": []}, [patch])

    def test_stream_checks_owner_thread_version_and_revision(self):
        bridge = desktop.DesktopThread.__new__(desktop.DesktopThread)
        bridge.thread_id, bridge.owner = THREAD, "owner"
        bridge.state, bridge.revision = None, None
        bridge.lock = threading.RLock()
        bridge.connected = False
        event = {"type": "broadcast", "method": "thread-stream-state-changed", "sourceClientId": "owner",
                 "version": 11, "params": {"conversationId": THREAD, "hostId": "local",
                 "change": {"type": "snapshot", "revision": 2, "conversationState": fixture()}}}
        wrong = copy.deepcopy(event); wrong["sourceClientId"] = "another-owner"
        bridge.ingest(wrong)
        self.assertIsNone(bridge.state)
        bridge.ingest(event)
        self.assertTrue(bridge.connected)
        self.assertEqual(2, bridge.revision)
        wrong = copy.deepcopy(event); wrong["version"] = 12
        with self.assertRaises(ValueError): bridge.ingest(wrong)
        event["params"]["change"] = {"type": "patches", "revision": 4, "baseRevision": 3, "patches": []}
        with self.assertRaises(ValueError): bridge.ingest(event)
        event["params"]["change"] = {"type": "patches", "revision": 3, "baseRevision": 2,
            "patches": [{"op": "replace", "path": ["id"], "value": "other-thread"}]}
        with self.assertRaises(ValueError): bridge.ingest(event)


class QuestionTests(unittest.TestCase):
    def test_receipt_and_unknown_delivery_prevent_second_send_before_native_patch(self):
        for response in ({"result": {"result": {"turnId": TURN}}}, TimeoutError("timed out")):
            bridge = desktop.DesktopThread.__new__(desktop.DesktopThread)
            bridge.thread_id, bridge.state = THREAD, fixture()
            bridge.lock, bridge.connected, bridge.deliveries = threading.RLock(), True, {}
            bridge.request = Mock(side_effect=response if isinstance(response, Exception) else None,
                                  return_value=response)
            command = (TURN, CALL, {QUESTION: "JSON"})
            first = bridge._answer(command)
            self.assertEqual(not isinstance(response, Exception), first["ok"])
            second = bridge._answer(command)
            self.assertFalse(second["ok"])
            self.assertTrue(second["delivery_unknown"])
            self.assertEqual(1, bridge.request.call_count)

    def test_async_answer_uses_exact_native_question_envelope(self):
        method, params = desktop.answer_request(fixture(), THREAD, TURN, CALL, {QUESTION: "JSON"})
        self.assertEqual("thread-follower-steer-turn", method)
        self.assertEqual(THREAD, params["conversationId"])
        content = params["input"][0]["text"]
        data = json.loads(content.splitlines()[1])
        self.assertEqual([{"questionItemId": QUESTION, "question": "选择输出格式", "answer": "JSON"}], data)
        self.assertEqual("send_user_message_async_question", params["restoreMessage"]["context"]["turnTrigger"])

    def test_namespaced_custom_tool_answer_uses_same_native_question_id(self):
        state = fixture()
        state["turnHistory"]["history"]["entitiesByKey"]["last"]["items"] = [{
            "type": "customToolCall", "id": "tool-item", "callId": CALL,
            "name": "functions.request_user_input_async",
            "input": {"questions": [{"title": "选择输出格式", "options": ["JSON"]}]},
        }]
        method, params = desktop.answer_request(state, THREAD, TURN, CALL, {QUESTION: "JSON"})

        self.assertEqual("thread-follower-steer-turn", method)
        self.assertEqual(QUESTION, json.loads(params["input"][0]["text"].splitlines()[1])[0]["questionItemId"])
        self.assertEqual("send_user_message_async_question", params["restoreMessage"]["context"]["turnTrigger"])

    def test_stale_cross_thread_and_unknown_question_are_never_plain_messages(self):
        cases = [("wrong-thread", TURN, CALL, {QUESTION: "JSON"}),
                 (THREAD, "old-turn", CALL, {QUESTION: "JSON"}),
                 (THREAD, TURN, "wrong-call", {QUESTION: "JSON"}),
                 (THREAD, TURN, CALL, {"another-question": "JSON"}),
                 (THREAD, TURN, CALL, {QUESTION: ""})]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(ValueError):
                desktop.answer_request(fixture(), *args)
        state = fixture(); desktop.current_turn(state)["status"] = "completed"
        with self.assertRaises(ValueError): desktop.answer_request(state, THREAD, TURN, CALL, {QUESTION: "JSON"})

    def test_accepted_reply_prevents_duplicate_but_pending_does_not_imply_answered(self):
        state = fixture()
        _, params = desktop.answer_request(state, THREAD, TURN, CALL, {QUESTION: "JSON"})
        item = {"type": "steeringUserMessage", "status": "pending", "input": params["input"]}
        desktop.current_turn(state)["items"].append(item)
        self.assertEqual({}, desktop._accepted_answers(desktop.current_turn(state)))
        item["status"] = "accepted"
        with self.assertRaises(ValueError): desktop.answer_request(state, THREAD, TURN, CALL, {QUESTION: "JSON"})

    def test_sync_question_uses_pending_native_request_and_preserves_numeric_id(self):
        state = fixture()
        state["requests"] = [{"id": 42, "method": "item/tool/requestUserInput",
            "params": {"turnId": TURN, "questions": [{"id": "format", "question": "Format?"}]}}]
        method, params = desktop.answer_request(state, THREAD, TURN, "42", {"format": "Markdown"})
        self.assertEqual("thread-follower-submit-user-input", method)
        self.assertEqual(42, params["requestId"])
        self.assertEqual({"answers": {"format": {"answers": ["Markdown"]}}}, params["response"])
        state["requests"][0]["method"] = "item/permissions/requestApproval"
        with self.assertRaises(ValueError): desktop.answer_request(state, THREAD, TURN, "42", {"format": "yes"})


class NativeTextTests(unittest.TestCase):
    def test_running_turn_uses_exact_steer_envelope(self):
        delivery_id = "01a00000-0000-7000-8000-000000000003"
        method, params, version = desktop.text_request(
            fixture(), THREAD, TURN, "继续排查", delivery_id)
        self.assertEqual(("thread-follower-steer-turn", 1), (method, version))
        self.assertEqual(THREAD, params["conversationId"])
        self.assertEqual(delivery_id, params["clientUserMessageId"])
        self.assertEqual("继续排查", params["input"][0]["text"])
        self.assertEqual("composer", params["restoreMessage"]["context"]["turnTrigger"])
        self.assertEqual([], params["attachments"])

    def test_completed_turn_starts_one_new_turn_with_v2_payload(self):
        state = fixture()
        desktop.current_turn(state)["status"] = "completed"
        delivery_id = "01a00000-0000-7000-8000-000000000003"
        method, params, version = desktop.text_request(
            state, THREAD, TURN, "继续下一轮", delivery_id)
        self.assertEqual(("thread-follower-start-turn", 2), (method, version))
        start = params["turnStart"]
        self.assertEqual(THREAD, start["request"]["threadId"])
        self.assertEqual("composer", start["request"]["turnTrigger"])
        self.assertEqual([], start["context"]["attachments"])
        self.assertEqual([], start["context"]["responseItems"])

    def test_binding_status_and_text_are_strict(self):
        delivery_id = "01a00000-0000-7000-8000-000000000003"
        for args in ((fixture(), "wrong", TURN, "x", delivery_id),
                     (fixture(), THREAD, "old", "x", delivery_id),
                     (fixture(), THREAD, TURN, "", delivery_id),
                     (fixture(), THREAD, TURN, "x", "bad-id")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                desktop.text_request(*args)
        state = fixture(); desktop.current_turn(state)["status"] = "pending-owner"
        with self.assertRaises(ValueError):
            desktop.text_request(state, THREAD, TURN, "x", delivery_id)

    def test_delivery_ledger_prevents_duplicate_and_unknown_retry(self):
        delivery_id = "01a00000-0000-7000-8000-000000000003"
        for response in (
            {"result": {"result": {"turnId": TURN}}},
            TimeoutError("timed out"),
        ):
            bridge = desktop.DesktopThread.__new__(desktop.DesktopThread)
            bridge.thread_id, bridge.state = THREAD, fixture()
            bridge.lock, bridge.connected, bridge.deliveries = threading.RLock(), True, {}
            bridge.request = Mock(side_effect=response if isinstance(response, Exception) else None,
                                  return_value=response)
            command = (TURN, "继续", delivery_id)
            first = bridge._send_text(command)
            self.assertEqual(not isinstance(response, Exception), first["ok"])
            second = bridge._send_text(command)
            self.assertFalse(second["ok"])
            self.assertTrue(second["delivery_unknown"])
            self.assertEqual(1, bridge.request.call_count)

    def test_start_receipt_returns_the_new_turn_id(self):
        state = fixture(); desktop.current_turn(state)["status"] = "completed"
        bridge = desktop.DesktopThread.__new__(desktop.DesktopThread)
        bridge.thread_id, bridge.state = THREAD, state
        bridge.lock, bridge.connected, bridge.deliveries = threading.RLock(), True, {}
        new_turn = "01a00000-0000-7000-8000-000000000004"
        bridge.request = Mock(return_value={"result": {"result": {"turn": {"id": new_turn}}}})
        result = bridge._send_text((TURN, "下一轮", "01a00000-0000-7000-8000-000000000003"))
        self.assertTrue(result["ok"])
        self.assertEqual("native_started", result["delivery"])
        self.assertEqual(new_turn, result["turn_id"])
        self.assertEqual(2, bridge.request.call_args.kwargs["version"])

    def test_empty_thread_first_turn_keeps_selected_model_and_effort(self):
        delivery_id = "01a00000-0000-7000-8000-000000000003"
        state = {"id": THREAD, "cwd": "D:/project", "turns": []}
        method, params, version = desktop.initial_text_request(
            state, THREAD, "D:/project", "开始任务", "gpt-6-astra", "max", delivery_id)
        self.assertEqual(("thread-follower-start-turn", 2), (method, version))
        request = params["turnStart"]["request"]
        self.assertEqual(("gpt-6-astra", "max"), (request["model"], request["effort"]))
        self.assertEqual("开始任务", request["input"][0]["text"])
        self.assertTrue(params["turnStart"]["context"]["inheritThreadSettings"])

    def test_first_turn_delivery_is_sent_once_after_desktop_owns_empty_thread(self):
        bridge = desktop.DesktopThread.__new__(desktop.DesktopThread)
        bridge.thread_id = THREAD
        bridge.state = {"id": THREAD, "cwd": "D:/project", "turns": []}
        bridge.lock, bridge.connected, bridge.deliveries = threading.RLock(), True, {}
        new_turn = "01a00000-0000-7000-8000-000000000004"
        bridge.request = Mock(return_value={"result": {"result": {"turn": {"id": new_turn}}}})
        command = ("D:/project", "开始", "gpt-6-astra", "max",
                   "01a00000-0000-7000-8000-000000000003")
        first = bridge._start_initial(command)
        second = bridge._start_initial(command)
        self.assertTrue(first["ok"])
        self.assertEqual(new_turn, first["turn_id"])
        self.assertFalse(second["ok"])
        self.assertTrue(second["delivery_unknown"])
        self.assertEqual(1, bridge.request.call_count)


class NativeResumeTests(unittest.TestCase):
    def terminal(self):
        state = fixture()
        turn = desktop.current_turn(state)
        turn["status"] = "interrupted"
        turn["items"] = []
        return state

    def test_resume_uses_inherited_settings_and_fixed_continuation_prompt(self):
        delivery_id = "01a00000-0000-7000-8000-000000000003"
        method, params, version = desktop.resume_request(
            self.terminal(), THREAD, TURN, delivery_id)
        self.assertEqual(("thread-follower-start-turn", 2), (method, version))
        request = params["turnStart"]["request"]
        self.assertEqual(desktop.RESUME_PROMPT, request["input"][0]["text"])
        self.assertIsNone(request["model"])
        self.assertIsNone(request["effort"])
        self.assertTrue(params["turnStart"]["context"]["inheritThreadSettings"])
        self.assertIn("不要虚构新目标", desktop.RESUME_PROMPT)

    def test_running_question_approval_and_unknown_state_are_rejected(self):
        state = self.terminal()
        desktop.current_turn(state)["status"] = "inProgress"
        with self.assertRaisesRegex(ValueError, "仍在运行"):
            desktop.resume_request(state, THREAD, TURN, str(uuid.uuid4()))
        for method in ("item/tool/requestUserInput", "item/permissions/requestApproval"):
            state = self.terminal()
            state["requests"] = [{"method": method, "params": {"turnId": TURN}}]
            with self.subTest(method=method), self.assertRaisesRegex(ValueError, "问题或审批"):
                desktop.resume_request(state, THREAD, TURN, str(uuid.uuid4()))
        state = self.terminal()
        desktop.current_turn(state)["status"] = "pending-owner"
        with self.assertRaisesRegex(ValueError, "状态尚未确认"):
            desktop.resume_request(state, THREAD, TURN, str(uuid.uuid4()))

    def test_async_question_blocks_terminal_resume(self):
        state = self.terminal()
        desktop.current_turn(state)["items"] = [{
            "type": "agentMessage", "id": CALL, "delivery": "async",
            "questions": [{"title": "是否继续"}]}]
        with self.assertRaisesRegex(ValueError, "待回答问题"):
            desktop.resume_request(state, THREAD, TURN, str(uuid.uuid4()))

    def test_resume_persists_reservation_before_ipc_and_settles_unknown(self):
        bridge = desktop.DesktopThread.__new__(desktop.DesktopThread)
        bridge.thread_id, bridge.state = THREAD, self.terminal()
        bridge.lock, bridge.connected, bridge.deliveries = threading.RLock(), True, {}
        events = []
        delivery_id = "01a00000-0000-7000-8000-000000000003"

        def reserve(turn_id, msg_id):
            events.append(("reserve", turn_id, msg_id))
            return {"ok": True}

        def settle(turn_id, msg_id, state, new_turn):
            events.append(("settle", turn_id, msg_id, state, new_turn))

        def request(*_args, **_kwargs):
            events.append(("request",))
            raise TimeoutError("unknown")

        bridge.request = Mock(side_effect=request)
        result = bridge._resume(("", delivery_id, reserve, settle))
        self.assertFalse(result["ok"])
        self.assertTrue(result["delivery_unknown"])
        self.assertEqual("reserve", events[0][0])
        self.assertEqual("request", events[1][0])
        self.assertEqual(("settle", TURN, delivery_id, "unknown", ""), events[2])
        self.assertEqual(1, bridge.request.call_count)


class NewTaskAppServerTests(unittest.TestCase):
    class FakeServer:
        def __init__(self, *, read_model="gpt-6-astra", read_effort="max",
                     delete_fails=False):
            self.calls = []
            self.read_model, self.read_effort = read_model, read_effort
            self.delete_fails = delete_fails

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def request(self, method, params=None):
            self.calls.append((method, params))
            if method == "model/list":
                return {"data": [{"id": "gpt-6-astra", "model": "gpt-6-astra",
                    "displayName": "GPT-6-Astra", "isDefault": True,
                    "defaultReasoningEffort": "medium",
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": "medium"}, {"reasoningEffort": "max"}]}]}
            if method == "config/read":
                return {"config": {"model": "gpt-6-astra", "model_reasoning_effort": "max"}}
            if method == "thread/start":
                return {"thread": {"id": THREAD}}
            if method == "thread/read":
                return {"thread": {"id": THREAD, "model": self.read_model,
                                    "reasoningEffort": self.read_effort, "turns": []}}
            if method == "thread/delete" and self.delete_fails:
                raise TimeoutError("delete unknown")
            return {}

    def test_initialize_explicitly_opts_into_thread_settings_protocol(self):
        params = desktop._app_server_initialize_params()
        self.assertEqual(True, params["capabilities"]["experimentalApi"])
        self.assertEqual("rxyy-tools-community", params["clientInfo"]["name"])

    def test_catalog_uses_effective_supported_config_default(self):
        fake = self.FakeServer()
        with patch.object(desktop, "_AppServer", return_value=fake):
            result = desktop.list_new_task_models("D:/project")
        self.assertTrue(result["ok"])
        self.assertEqual(("gpt-6-astra", "max"),
                         (result["default_model"], result["default_effort"]))
        self.assertIn(("config/read", {"cwd": "D:/project", "includeLayers": False}),
                      fake.calls)

    def test_create_injects_truthful_marker_and_reads_settings_without_turn(self):
        fake = self.FakeServer()
        with patch.object(desktop, "_AppServer", return_value=fake):
            result = desktop.create_configured_thread("D:/project", "gpt-6-astra", "max")
        self.assertEqual(THREAD, result["thread_id"])
        self.assertIn(("thread/settings/update", {"threadId": THREAD,
            "model": "gpt-6-astra", "effort": "max"}), fake.calls)
        injected = next(params for method, params in fake.calls
                        if method == "thread/inject_items")
        self.assertEqual(THREAD, injected["threadId"])
        self.assertEqual("developer", injected["items"][0]["role"])
        self.assertEqual("Task created by rxyy tools desktop integration.",
                         injected["items"][0]["content"][0]["text"])
        self.assertNotIn(injected["items"][0]["role"], ("user", "assistant"))
        self.assertIn(("thread/read", {"threadId": THREAD, "includeTurns": True}),
                      fake.calls)
        self.assertNotIn("turn/start", [method for method, _ in fake.calls])

    def test_failed_settings_readback_deletes_only_the_new_thread(self):
        fake = self.FakeServer(read_effort="medium")
        with patch.object(desktop, "_AppServer", return_value=fake), \
             self.assertRaisesRegex(ValueError, "未保存"):
            desktop.create_configured_thread("D:/project", "gpt-6-astra", "max")
        self.assertEqual(("thread/delete", {"threadId": THREAD}), fake.calls[-1])

    def test_unknown_cleanup_preserves_thread_id_for_manual_verification(self):
        fake = self.FakeServer(read_effort="medium", delete_fails=True)
        with patch.object(desktop, "_AppServer", return_value=fake), \
             self.assertRaises(desktop.NewTaskSetupError) as caught:
            desktop.create_configured_thread("D:/project", "gpt-6-astra", "max")
        self.assertEqual(THREAD, caught.exception.thread_id)


if __name__ == "__main__":
    unittest.main()
