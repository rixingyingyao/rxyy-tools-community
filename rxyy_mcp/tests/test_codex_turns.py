# -*- coding: utf-8 -*-
"""Codex rollout 适配层的离线 fixtures。"""

import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch


MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import codex_turns as ct  # noqa: E402


THREAD = "01a07f10-6837-71d1-82d4-f249ff85b01f"
OTHER = "01a07f11-d60a-77e1-8167-eae2baad0961"


def event(kind, payload, timestamp="2026-09-08T03:00:00.000Z"):
    return {"timestamp": timestamp, "type": kind, "payload": payload}


def session_meta(thread=THREAD):
    return event("session_meta", {"id": thread, "session_id": thread,
                                   "base_instructions": {"text": "do not expose"}})


def task_started(turn="turn-1"):
    return event("event_msg", {"type": "task_started", "turn_id": turn,
                                "started_at": 1788836400000})


def user(text):
    return event("event_msg", {"type": "user_message", "message": text})


def assistant(text, ident="msg-1", phase=""):
    payload = {"type": "message", "id": ident, "role": "assistant",
               "content": [{"type": "output_text", "text": text}]}
    if phase:
        payload["phase"] = phase
    return event("response_item", payload)


def reasoning(text, ident="reason-1"):
    return event("response_item", {"type": "reasoning", "id": ident,
                                    "summary": [{"type": "summary_text", "text": text}],
                                    "encrypted_content": "DO_NOT_READ"})


def tool_call(name="functions.exec", call_id="call-1", ident="tool-1", code="print('ok')",
              status=None):
    payload = {"type": "function_call", "id": ident, "name": name, "call_id": call_id,
               "arguments": json.dumps({"code": code}, ensure_ascii=False)}
    if status is not None:
        payload["status"] = status
    return event("response_item", payload)


def tool_output(call_id="call-1", text="ok", ident="out-1"):
    return event("response_item", {"type": "function_call_output", "id": ident,
                                    "call_id": call_id,
                                    "output": [{"type": "text", "text": text}]})


def request_user_input(call_id="question-call", ident="question-request", turn="turn-1", questions=None):
    questions = questions or [{"title": "请选择范围", "options": ["只修读取", "读取与展示"]}]
    return event("response_item", {
        "type": "function_call", "id": ident, "name": "request_user_input_async",
        "call_id": call_id,
        "arguments": json.dumps({"questions": questions}, ensure_ascii=False),
        "internal_chat_message_metadata_passthrough": {"turn_id": turn},
    })


def question_item(call_id="question-call", turn="turn-1", questions=None):
    questions = questions or [{"title": "请选择范围", "options": ["只修读取", "读取与展示"]}]
    return event("event_msg", {
        "type": "item_completed", "thread_id": THREAD, "turn_id": turn,
        "item": {"type": "AgentMessage", "id": call_id, "content": [],
                 "phase": "final_answer", "delivery": "async", "questions": questions},
    })


def compacted(window=1):
    return event("compacted", {
        "window_number": window, "first_window_id": "window-first",
        "previous_window_id": "window-prev", "window_id": "window-new",
        "compaction_response_id": "resp-compact",
        "latest_token_usage_record": {},
    })


def token_count():
    return event("event_msg", {"type": "token_count", "info": {
        "total_token_usage": {"input_tokens": 650000, "output_tokens": 1200,
                               "reasoning_output_tokens": 300, "total_tokens": 651200},
        "last_token_usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        "model_context_window": 1000000,
    }})


def complete(turn="turn-1"):
    return event("event_msg", {"type": "task_complete", "turn_id": turn},
                 timestamp="2026-09-08T03:00:10.000Z")


def write_rollout(root, thread=THREAD, rows=None, name_prefix="2026-09-08T03-00-00"):
    path = Path(root) / "sessions" / "2026" / "09" / "08"
    path.mkdir(parents=True, exist_ok=True)
    file_path = path / ("rollout-{}-{}.jsonl".format(name_prefix, thread))
    with file_path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows or []:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    return file_path


class CodexTurnTests(unittest.TestCase):
    def test_phased_messages_native_question_compaction_and_title_are_structured(self):
        call_id = "question-call"
        rows = [session_meta(), task_started(), user("任务"),
                assistant("正在核对", "commentary-1", "commentary"),
                request_user_input(call_id), question_item(call_id),
                tool_output(call_id, '{"accepted":true}', "question-accepted"),
                compacted(), assistant("处理完成", "final-1", "final_answer")]
        with tempfile.TemporaryDirectory() as tmp:
            write_rollout(tmp, rows=rows)
            index = Path(tmp) / "session_index.jsonl"
            index.write_text(json.dumps({
                "id": THREAD, "thread_name": "原生任务标题", "updated_at": "2026-09-08T03:00:00Z"
            }, ensure_ascii=False) + "\n", encoding="utf-8")
            turn = ct.read_turn(THREAD, codex_home=tmp, now=1788836410)

        texts = [step for step in turn["steps"] if step["kind"] == "text"]
        self.assertEqual(["commentary", "final_answer"], [step["phase"] for step in texts])
        self.assertEqual("final-1", turn["reply"]["step_id"])
        self.assertEqual("final_answer", turn["reply"]["phase"])
        self.assertEqual("原生任务标题", turn["thread_title"])
        self.assertEqual("codex_session_index", turn["title_source"])
        question = turn["native_questions"][0]
        self.assertEqual(call_id, question["call_id"])
        self.assertEqual(THREAD, question["thread_id"])
        self.assertEqual("turn-1", question["turn_id"])
        self.assertEqual("pending", question["status"], "accepted 只表示请求已登记，不是已回答")
        self.assertEqual(
            '["request_user_input_async","question-call",0]', question["questions"][0]["id"])
        self.assertEqual("读取与展示", question["questions"][0]["options"][1]["label"])
        self.assertEqual(1, turn["compaction"]["count"])
        self.assertEqual("window-new", turn["compaction"]["window_id"])
        self.assertTrue(any(step.get("event") == "context_compacted" for step in turn["steps"]))

    def test_native_question_resolves_only_from_exact_question_reply_envelope(self):
        question_id = '["request_user_input_async","question-call",0]'
        answer = "读取与展示"
        envelope = "<send_user_message_question_reply>\n{}\n</send_user_message_question_reply>".format(
            json.dumps([{"questionItemId": question_id, "question": "请选择范围",
                         "answer": answer}], ensure_ascii=False))
        rows = [session_meta(), task_started(), user("任务"), request_user_input(), question_item(),
                tool_output("question-call", '{"accepted":true}'),
                event("response_item", {"type": "message", "id": "answer-1", "role": "user",
                                         "content": [{"type": "input_text", "text": envelope}]})]
        with tempfile.TemporaryDirectory() as tmp:
            write_rollout(tmp, rows=rows)
            turn = ct.read_turn(THREAD, codex_home=tmp)

        question = turn["native_questions"][0]
        self.assertEqual("resolved", question["status"])
        self.assertEqual(answer, question["answers"][question_id])

    def test_multi_question_card_stays_pending_until_every_question_is_answered(self):
        questions = [{"title": "范围", "options": ["读取"]},
                     {"title": "格式", "options": ["JSON"]}]
        first_id = '["request_user_input_async","question-call",0]'
        envelope = "<send_user_message_question_reply>\n{}\n</send_user_message_question_reply>".format(
            json.dumps([{"questionItemId": first_id, "question": "范围", "answer": "读取"}],
                       ensure_ascii=False))
        rows = [session_meta(), task_started(), user("任务"),
                request_user_input(questions=questions), question_item(questions=questions),
                event("response_item", {"type": "message", "id": "partial-answer", "role": "user",
                                         "content": [{"type": "input_text", "text": envelope}]})]
        with tempfile.TemporaryDirectory() as tmp:
            write_rollout(tmp, rows=rows)
            turn = ct.read_turn(THREAD, codex_home=tmp)

        question = turn["native_questions"][0]
        self.assertEqual("pending", question["status"])
        self.assertEqual("读取", question["answers"][first_id])

    def test_project_desktop_turn_whitelists_visible_items(self):
        question_id = '["request_user_input_async","question-item",0]'
        state = {
            "id": THREAD, "title": "桌面实时任务", "latestModel": "gpt-6-astra",
            "latestReasoningEffort": None,
            "latestTokenUsageInfo": {"last": {"totalTokens": 120},
                                     "total": {"totalTokens": 300},
                                     "modelContextWindow": 1000},
            "threadRuntimeStatus": {"type": "loaded", "activeFlags": ["hasActiveTurn"]},
            "turnHistory": {"kind": "canonical", "history": {
                "islands": [{"entries": [{"value": "turn-key"}]}],
                "entitiesByKey": {"turn-key": {
                "turnId": "native-turn", "status": "inProgress", "turnStartedAtMs": 1788836400000,
                "params": {"collaborationMode": {"mode": "default", "settings": {
                    "model": "gpt-6-astra", "reasoning_effort": "max",
                    "developer_instructions": "PRIVATE_PARAMS"}}},
                "durationMs": 5000, "items": [
                    {"type": "reasoning", "id": "reason-1", "summary": "核对结构",
                     "content": "PRIVATE_REASONING"},
                    {"type": "agentMessage", "id": "comment-1", "text": "处理中",
                     "phase": "commentary"},
                    {"type": "agentMessage", "id": "question-item", "text": "请选择范围",
                     "phase": "final_answer", "delivery": "async",
                     "questions": [{"title": "请选择范围", "options": ["只修读取", "读取与展示"]}]},
                    {"type": "commandExecution", "id": "cmd-1", "command": "echo ok",
                     "cwd": "C:/private", "status": "completed", "aggregatedOutput": "ok",
                     "exitCode": 0, "completedAtMs": 1788836404000},
                    {"type": "privateState", "secret": "DO_NOT_EXPOSE"},
                ]}}}},
            "requests": {},
        }

        turn = ct.project_desktop_turn(state, now=1788836406)

        self.assertEqual("codex_desktop", turn["source"])
        self.assertEqual("桌面实时任务", turn["thread_title"])
        self.assertTrue(turn["live"])
        self.assertEqual("native-turn", turn["turn_id"])
        self.assertEqual(12.0, turn["context"]["pct"])
        self.assertEqual("max", turn["effort"])
        self.assertEqual("comment-1", turn["reply"]["step_id"])
        steps = {step["id"]: step for step in turn["steps"]}
        self.assertEqual(0, steps["comment-1"]["at"], "缺逐项时间时不冒用整轮时间")
        self.assertEqual(1788836404, steps["cmd-1"]["at"])
        question = turn["native_questions"][0]
        self.assertEqual(question_id, question["questions"][0]["id"])
        serialized = json.dumps(turn, ensure_ascii=False)
        self.assertNotIn("PRIVATE_REASONING", serialized)
        self.assertNotIn("DO_NOT_EXPOSE", serialized)
        self.assertNotIn("C:/private", serialized)
        self.assertNotIn("PRIVATE_PARAMS", serialized)

    def test_desktop_blocking_question_and_pending_async_reply_stay_pending(self):
        question_id = '["request_user_input_async","async-question",0]'
        envelope = "<send_user_message_question_reply>\n{}\n</send_user_message_question_reply>".format(
            json.dumps([{"questionItemId": question_id, "question": "异步问题",
                         "answer": "已选择"}], ensure_ascii=False))
        turn = {"turnId": "native-turn", "status": "inProgress", "items": [
            {"type": "agentMessage", "id": "async-question", "delivery": "async",
             "questions": [{"title": "异步问题", "options": ["已选择"]}]},
            {"type": "steeringUserMessage", "status": "pending",
             "input": [{"type": "text", "text": envelope}]},
        ]}
        state = {"id": THREAD, "turnHistory": {"kind": "canonical", "history": {
            "entitiesByKey": {"turn": turn}, "islands": [{"entries": [{"value": "turn"}]}]}},
            "requests": [{"id": 42, "method": "item/tool/requestUserInput", "params": {
                "turnId": "native-turn", "questions": [{"id": "format", "question": "输出格式？",
                                                          "options": [{"label": "JSON"}]}]}}]}

        projected = ct.project_desktop_turn(state)

        self.assertEqual(THREAD, projected["uuid"])
        self.assertEqual(2, projected["native_question_pending"])
        blocking = next(q for q in projected["native_questions"] if q["delivery"] == "blocking")
        self.assertEqual(42, blocking["request_id"])
        self.assertEqual("format", blocking["questions"][0]["id"])
        state["requests"][0]["response"] = {"answers": {"format": {"answers": ["JSON"]}}}
        projected = ct.project_desktop_turn(state)
        blocking = next(q for q in projected["native_questions"] if q["delivery"] == "blocking")
        self.assertEqual("resolved", blocking["status"])
        self.assertEqual(["JSON"], blocking["answers"]["format"])
        turn["items"][1]["status"] = "accepted"
        projected = ct.project_desktop_turn(state)
        async_question = next(q for q in projected["native_questions"] if q["delivery"] == "async")
        self.assertEqual("resolved", async_question["status"])

    def test_desktop_async_text_without_questions_is_a_native_question(self):
        turn = {"turnId": "native-turn", "status": "inProgress", "items": [
            {"type": "agentMessage", "id": "plain-question", "delivery": "async",
             "text": "请补充验收范围"}]}
        state = {"id": THREAD, "turnHistory": {"kind": "canonical", "history": {
            "entitiesByKey": {"turn": turn}, "islands": [{"entries": [{"value": "turn"}]}]}}}

        projected = ct.project_desktop_turn(state)

        question = projected["native_questions"][0]
        self.assertEqual("plain-question", question["questions"][0]["id"])
        self.assertEqual("请补充验收范围", question["questions"][0]["question"])
        self.assertIsNone(projected["reply"])

    def test_desktop_plain_user_messages_keep_native_order_without_becoming_reply(self):
        turn = {"turnId": "native-turn", "status": "inProgress", "items": [
            {"type": "userMessage", "id": "user-1",
             "content": [{"type": "text", "text": "开始处理"}]},
            {"type": "reasoning", "id": "reason", "summary": "先检查"},
            {"type": "steeringUserMessage", "id": "steer-pending", "status": "pending",
             "input": [{"type": "text", "text": "尚未接收"}]},
            {"type": "steeringUserMessage", "id": "steer-ok", "status": "accepted",
             "input": [{"type": "text", "text": "继续处理"}]},
            {"type": "agentMessage", "id": "answer", "text": "处理完成"},
        ]}
        state = {"id": THREAD, "threadRuntimeStatus": {"type": "active"},
                 "turnHistory": {"kind": "canonical", "history": {
                     "entitiesByKey": {"turn": turn},
                     "islands": [{"entries": [{"value": "turn"}]}]}}}

        projected = ct.project_desktop_turn(state)

        self.assertEqual(["user-1", "reason", "steer-ok", "answer"],
                         [step["id"] for step in projected["steps"]])
        self.assertEqual("user", projected["steps"][0]["role"])
        self.assertEqual("assistant", projected["steps"][-1]["role"])
        self.assertEqual("处理完成", projected["reply"]["text"])

    def test_question_reply_requires_the_whole_exact_envelope(self):
        question_id = '["request_user_input_async","question-call",0]'
        payload = json.dumps([{"questionItemId": question_id, "question": "范围",
                               "answer": "读取"}], ensure_ascii=False)
        envelope = "<send_user_message_question_reply>" + payload + "</send_user_message_question_reply>"
        self.assertEqual(1, len(ct._question_answers_from_text(envelope)))
        self.assertEqual([], ct._question_answers_from_text("普通消息\n" + envelope))
        self.assertEqual([], ct._question_answers_from_text(envelope + "\n普通消息"))

    def test_cold_long_turn_keeps_real_count_and_model_outside_tail_window(self):
        rows = [session_meta(), task_started(), event("turn_context", {
            "turn_id": "turn-1", "model": "actual-model", "effort": "high"}), user("长任务")]
        rows.extend(assistant("x" * 6000, "message-%d" % i) for i in range(500))
        with tempfile.TemporaryDirectory() as tmp:
            path = write_rollout(tmp, rows=rows)
            self.assertGreater(path.stat().st_size, ct.TAIL_BYTES)
            cache = {}
            turn = ct.read_turn(THREAD, max_steps=10, codex_home=tmp, cache=cache)
            self.assertIsNotNone(turn)
            self.assertEqual(501, turn["total_steps"])
            self.assertEqual(491, turn["omitted"])
            self.assertEqual("actual-model", turn["model"])
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(assistant("新增", "new"), ensure_ascii=False) + "\n")
            with patch.object(ct, "_load_json_line", wraps=ct._load_json_line) as decode:
                newer = ct.read_turn(THREAD, max_steps=10, codex_home=tmp, cache=cache)
            self.assertLess(decode.call_count, 10, "追加时不应重新解析整个长任务")
            self.assertEqual(502, newer["total_steps"])
            self.assertEqual("新增", newer["reply"]["text"])

    def test_user_steering_inside_turn_does_not_erase_earlier_steps(self):
        rows = [session_meta(), task_started(), user("原始目标"), assistant("先完成部分", "first"),
                user("补充要求"), assistant("继续处理", "second")]
        with tempfile.TemporaryDirectory() as tmp:
            write_rollout(tmp, rows=rows)
            turn = ct.read_turn(THREAD, codex_home=tmp)
        self.assertIn("先完成部分", json.dumps(turn["steps"], ensure_ascii=False))
        self.assertEqual(3, turn["total_steps"])

    def test_large_turn_cache_is_bounded_without_losing_total_count(self):
        rows = [session_meta(), task_started(), user("长任务")]
        rows.extend(assistant("part", "a%d" % i) for i in range(2200))
        with tempfile.TemporaryDirectory() as tmp:
            write_rollout(tmp, rows=rows)
            cache = {}
            turn = ct.read_turn(THREAD, max_steps=9999, codex_home=tmp, cache=cache)
            self.assertEqual(2201, turn["total_steps"])
            self.assertEqual(2000, len(turn["steps"]))
            self.assertEqual(201, turn["omitted"])
            stream = cache[ct._CACHE_KEY]["threads"][THREAD]["stream"]
            self.assertEqual(2000, len(stream["steps"]))

    def test_late_terminal_of_previous_turn_does_not_end_new_turn(self):
        rows = [session_meta(), task_started("new"), user("新问题"),
                assistant("处理中"), complete("old")]
        with tempfile.TemporaryDirectory() as tmp:
            write_rollout(tmp, rows=rows)
            turn = ct.read_turn(THREAD, codex_home=tmp)
        self.assertFalse(turn["terminal"])

    def test_visible_turn_has_cursor_shape_and_native_metadata(self):
        rows = [session_meta(), task_started(), event("turn_context", {
            "turn_id": "turn-1", "model": "gpt-5.6-luna", "effort": "max",
            "cwd": "C:/secret/workspace", "summary": "ignored",
        }), user("新的一轮问题"), reasoning("先核对数据"),
                tool_call(code="nodeRepl.write('probe')"), tool_output(text="命令输出"),
                assistant("最新正文"), token_count(), complete()]
        with tempfile.TemporaryDirectory() as tmp:
            write_rollout(tmp, rows=rows)
            turn = ct.read_turn(THREAD, codex_home=tmp, now=1788836410)
        self.assertIsNotNone(turn)
        self.assertEqual(THREAD, turn["uuid"])
        self.assertEqual("codex", turn["runtime_kind"])
        self.assertEqual("codex_rollout", turn["source"])
        self.assertEqual(("gpt-5.6-luna", "max"), (turn["model"], turn["effort"]))
        self.assertEqual("新的一轮问题", turn["user_text"])
        self.assertTrue(turn["terminal"])
        self.assertEqual("completed", turn["status"])
        self.assertFalse(turn["generating"])
        self.assertEqual("text", turn["reply"]["kind"])
        self.assertEqual("最新正文", turn["reply"]["text"])
        self.assertNotIn("DO_NOT_READ", json.dumps(turn, ensure_ascii=False))
        self.assertTrue(any(step["kind"] == "thinking" for step in turn["steps"]))
        tool = next(step for step in turn["steps"] if step["kind"] == "tool")
        self.assertEqual("call-1", tool["call_id"])
        self.assertIn("nodeRepl.write", tool["command"])
        self.assertIn("命令输出", tool["result"])
        self.assertEqual(120, turn["context"]["used"])
        self.assertEqual(len(turn["sig"]), 12)

    def test_previous_turn_is_not_attached_to_new_user(self):
        rows = [session_meta(), task_started("old"), user("旧问题"), assistant("旧回答", "old-a"),
                complete("old"), task_started("new"), user("新问题"), assistant("新回答", "new-a"),
                complete("new")]
        with tempfile.TemporaryDirectory() as tmp:
            write_rollout(tmp, rows=rows)
            turn = ct.read_turn(THREAD, codex_home=tmp, now=1788836410)
        self.assertEqual("新问题", turn["user_text"])
        self.assertNotIn("旧回答", json.dumps(turn["steps"], ensure_ascii=False))
        self.assertEqual("新回答", turn["reply"]["text"])

    def test_same_thread_file_is_reused_but_other_thread_is_rejected(self):
        rows = [session_meta(), task_started(), user("问题"), assistant("回答")]
        with tempfile.TemporaryDirectory() as tmp:
            write_rollout(tmp, rows=rows)
            cache = {}
            self.assertIsNotNone(ct.read_turn(THREAD, codex_home=tmp, cache=cache))
            self.assertIsNone(ct.read_turn(OTHER, codex_home=tmp, cache=cache))
            self.assertIn("__codex_turns__", cache)
            self.assertLessEqual(len(cache["__codex_turns__"]["threads"]), 32)

    def test_concurrent_readers_share_a_fixture_without_cross_thread_data(self):
        rows = [session_meta(), task_started(), user("并发问题"), assistant("并发回答")]
        with tempfile.TemporaryDirectory() as tmp:
            write_rollout(tmp, rows=rows)
            with ThreadPoolExecutor(max_workers=6) as pool:
                turns = list(pool.map(lambda _: ct.read_turn(THREAD, codex_home=tmp), range(18)))
        self.assertEqual({THREAD}, {turn["uuid"] for turn in turns})
        self.assertEqual({"并发回答"}, {turn["reply"]["text"] for turn in turns})

    def test_appended_partial_line_is_pending_then_becomes_visible(self):
        rows = [session_meta(), task_started(), user("问题"), assistant("第一段", "a1")]
        with tempfile.TemporaryDirectory() as tmp:
            path = write_rollout(tmp, rows=rows)
            cache = {}
            first = ct.read_turn(THREAD, codex_home=tmp, cache=cache, now=1788836405)
            with path.open("ab") as stream:
                stream.write(json.dumps(assistant("第二段", "a2"), ensure_ascii=False).encode("utf-8")[:20])
            partial = ct.read_turn(THREAD, codex_home=tmp, cache=cache, now=1788836405)
            self.assertEqual(1, partial["pending_bubbles"])
            self.assertEqual("第一段", partial["reply"]["text"])
            with path.open("ab") as stream:
                stream.write(json.dumps(assistant("第二段", "a2"), ensure_ascii=False).encode("utf-8")[20:])
                stream.write(b"\n")
            final = ct.read_turn(THREAD, codex_home=tmp, cache=cache, now=1788836405)
        self.assertEqual(0, final["pending_bubbles"])
        self.assertEqual("第二段", final["reply"]["text"])
        self.assertNotEqual(first["sig"], final["sig"])

    def test_truncated_rollout_rebuilds_the_cached_turn(self):
        old_rows = [session_meta(), task_started("old"), user("旧问题"), assistant("旧回答"), complete("old")]
        new_rows = [session_meta(), task_started("new"), user("新问题"), assistant("新回答")]
        with tempfile.TemporaryDirectory() as tmp:
            path = write_rollout(tmp, rows=old_rows)
            cache = {}
            self.assertEqual("旧回答", ct.read_turn(THREAD, codex_home=tmp, cache=cache)["reply"]["text"])
            with path.open("w", encoding="utf-8", newline="\n") as stream:
                for row in new_rows:
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            rebuilt = ct.read_turn(THREAD, codex_home=tmp, cache=cache)
        self.assertEqual("新问题", rebuilt["user_text"])
        self.assertEqual("新回答", rebuilt["reply"]["text"])
        self.assertFalse(rebuilt["terminal"])

    def test_same_message_id_stays_stable_while_body_changes_sig(self):
        first_rows = [session_meta(), task_started(), user("问题"), assistant("甲", "stable")]
        second_rows = [session_meta(), task_started(), user("问题"), assistant("乙", "stable")]
        with tempfile.TemporaryDirectory() as tmp:
            path = write_rollout(tmp, rows=first_rows)
            cache = {}
            first = ct.read_turn(THREAD, codex_home=tmp, cache=cache)
            with path.open("w", encoding="utf-8", newline="\n") as stream:
                for row in second_rows:
                    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            second = ct.read_turn(THREAD, codex_home=tmp, cache=cache)
        first_text = next(step for step in first["steps"] if step["kind"] == "text")
        second_text = next(step for step in second["steps"] if step["kind"] == "text")
        self.assertEqual("stable", first_text["id"])
        self.assertEqual(first_text["id"], second_text["id"])
        self.assertNotEqual(first["sig"], second["sig"])

    def test_tool_completion_is_linked_by_call_id_and_custom_tool_is_supported(self):
        call = event("response_item", {"type": "custom_tool_call", "id": "c1",
                                         "call_id": "custom-1", "name": "lookup",
                                         "input": json.dumps({"query": "x"}), "status": "completed"})
        output = event("response_item", {"type": "custom_tool_call_output", "id": "co1",
                                           "call_id": "custom-1", "output": "done"})
        rows = [session_meta(), task_started(), user("问题"), call, output, complete()]
        with tempfile.TemporaryDirectory() as tmp:
            write_rollout(tmp, rows=rows)
            turn = ct.read_turn(THREAD, codex_home=tmp)
        step = next(item for item in turn["steps"] if item.get("call_id") == "custom-1")
        self.assertEqual("done", step["status"])
        self.assertIn("done", step["result"])

    def test_memory_bound_keeps_tail_steps_and_clips_large_visible_text(self):
        # 大量旧轮次落在尾部窗口之外；当前轮仍应只保留 max_steps 个步骤。
        rows = [session_meta(), task_started("old"), user("旧问题")]
        rows.extend(assistant("x" * 1000, "old-m-{}".format(index)) for index in range(6000))
        rows.extend([complete("old"), task_started("new"), user("问题"),
                     assistant("x" * 1000, "new-m-1"), assistant("最新", "new-m-2")])
        with tempfile.TemporaryDirectory() as tmp:
            write_rollout(tmp, rows=rows)
            turn = ct.read_turn(THREAD, max_steps=7, codex_home=tmp)
        self.assertLessEqual(len(turn["steps"]), 7)
        self.assertLessEqual(len(turn["reply"]["text"]), ct.MAX_TEXT_CHARS)
        self.assertLessEqual(len(turn["sig"]), 12)

    def test_aborted_is_terminal_but_silent_is_not(self):
        rows = [session_meta(), task_started(), user("问题"), reasoning("还在处理")]
        with tempfile.TemporaryDirectory() as tmp:
            write_rollout(tmp, rows=rows)
            silent = ct.read_turn(THREAD, codex_home=tmp, now=1788836401)
            write_rollout(tmp, rows=rows + [event("event_msg", {"type": "turn_aborted",
                                                                  "turn_id": "turn-1"})])
            aborted = ct.read_turn(THREAD, codex_home=tmp, now=1788836401)
        self.assertFalse(silent["terminal"])
        self.assertEqual("running", silent["status"])
        self.assertTrue(aborted["terminal"])
        self.assertEqual("aborted", aborted["status"])
        self.assertFalse(aborted["live"])

    def test_invalid_meta_id_and_symlink_escape_do_not_get_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_rollout(tmp, thread=OTHER,
                          rows=[session_meta(OTHER), task_started(), user("别的"), assistant("别读")])
            self.assertIsNone(ct.read_turn(THREAD, codex_home=tmp))
            with tempfile.TemporaryDirectory() as outside:
                outside_file = write_rollout(outside, rows=[session_meta(THREAD), task_started(),
                                                             user("越界"), assistant("越界")])
                link = Path(tmp) / "sessions" / "2026" / "09" / "08" / outside_file.name
                try:
                    link.symlink_to(outside_file)
                except (OSError, NotImplementedError):
                    return
                self.assertIsNone(ct.read_turn(THREAD, codex_home=tmp))

    def test_invalid_thread_does_not_scan_or_guess_from_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(ct.read_turn("../../auth.json", codex_home=tmp))
            self.assertIsNone(ct.read_turn("", codex_home=tmp))


class CodexContextTests(unittest.TestCase):
    def test_cumulative_usage_never_sets_context_percentage(self):
        context = ct._context_from_info({
            "total_token_usage": {"input_tokens": 31600000},
            "last_token_usage": {"input_tokens": 64000, "output_tokens": 4000, "total_tokens": 68000},
            "model_context_window": 256000})
        self.assertEqual(68000, context["used"])
        self.assertEqual(26.6, context["pct"])
        self.assertEqual(31600000, context["total"]["input_tokens"])

    def test_missing_last_usage_does_not_invent_current_occupancy(self):
        context = ct._context_from_info({
            "total_token_usage": {"input_tokens": 31600000}, "model_context_window": 256000})
        self.assertNotIn("used", context)
        self.assertNotIn("pct", context)

    def test_cached_input_and_reasoning_are_not_double_counted(self):
        context = ct._context_from_info({"last_token_usage": {
            "input_tokens": 64000, "cached_input_tokens": 60000,
            "output_tokens": 4000, "reasoning_output_tokens": 1000},
            "model_context_window": 256000})
        self.assertEqual(68000, context["used"])

    def test_compaction_lowers_occupancy_while_cumulative_usage_increases(self):
        def usage(cumulative, current):
            return event("event_msg", {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": cumulative},
                "last_token_usage": {"total_tokens": current}, "model_context_window": 256000}})
        rows = [session_meta(), task_started(), user("长任务"), usage(1000000, 180000)]
        with tempfile.TemporaryDirectory() as tmp:
            path = write_rollout(tmp, rows=rows)
            cache = {}
            before = ct.read_turn(THREAD, codex_home=tmp, cache=cache)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(usage(1050000, 50000)) + "\n")
            after = ct.read_turn(THREAD, codex_home=tmp, cache=cache)
        self.assertGreater(after["context"]["total"]["input_tokens"], before["context"]["total"]["input_tokens"])
        self.assertLess(after["context"]["pct"], before["context"]["pct"])
        self.assertEqual(50000, after["context"]["used"])
        self.assertNotEqual(before["sig"], after["sig"])


if __name__ == "__main__":
    unittest.main()
