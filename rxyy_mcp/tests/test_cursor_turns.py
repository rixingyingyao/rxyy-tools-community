# -*- coding: utf-8 -*-
"""实时时间线（读库还原 agent 这一轮的每一步）——rxyy 09-03「看 bajie 是怎么做的，把rxyy MCP
也改成这样」。

夹具全部照 09-03 从本机 Cursor 库（composer cb6f455c / 8f482069）实测抄下来的形态：
思考气泡 thinking.text + thinkingDurationMs；工具气泡 toolFormerData{name,status,rawArgs,
params,result,additionalData}；正文气泡 text；生成报错 errorDetails。
"""
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import cursor_turns as ct  # noqa: E402
import hub  # noqa: E402

UID = "cb6f455c-d6c3-4abd-ad8c-8f18ee7bd603"


def _bubble(bid, **fields):
    b = {"_v": 3, "bubbleId": bid, "type": 2, "createdAt": "2026-09-03T06:30:26.159Z",
         "conversationState": "~", "unifiedMode": 2,
         "tokenCount": {"inputTokens": 0, "outputTokens": 0}}
    b.update(fields)
    return b


def _tool(bid, name, status="completed", raw=None, params=None, result=None, add=None,
          error=None, at="2026-09-03T06:31:00.000Z"):
    tfd = {"toolCallId": "toolu_" + bid, "toolIndex": 0, "modelCallId": "", "name": name,
           "status": status, "tool": 15, "toolCallBinary": ""}
    if raw is not None:
        tfd["rawArgs"] = json.dumps(raw, ensure_ascii=False) if not isinstance(raw, str) else raw
    if params is not None:
        tfd["params"] = json.dumps(params, ensure_ascii=False)
    if result is not None:
        tfd["result"] = json.dumps(result, ensure_ascii=False) if not isinstance(result, str) else result
    if add is not None:
        tfd["additionalData"] = add
    if error is not None:
        tfd["error"] = error
    return _bubble(bid, capabilityType=15, toolFormerData=tfd, createdAt=at)


def _think(bid, text, ms=3380, at="2026-09-03T06:30:40.000Z"):
    return _bubble(bid, capabilityType=30, thinking={"text": text}, thinkingDurationMs=ms,
                   thinkingStyle=1, createdAt=at)


def _text(bid, text, at="2026-09-03T06:32:00.000Z"):
    return _bubble(bid, text=text, createdAt=at)


def _user(bid, text, at="2026-09-03T06:30:26.159Z"):
    return {"_v": 3, "bubbleId": bid, "type": 1, "text": text, "richText": "{}",
            "createdAt": at, "requestId": "r", "tokenCount": {}}


class ClassifyTests(unittest.TestCase):
    def test_known_tools_map_to_kind_and_label(self):
        self.assertEqual(("shell", "运行命令"), ct.classify_tool("run_terminal_command_v2"))
        self.assertEqual(("read", "读文件"), ct.classify_tool("read_file_v2"))
        self.assertEqual(("edit", "改文件"), ct.classify_tool("edit_file_v2"))
        self.assertEqual(("search", "搜索"), ct.classify_tool("ripgrep_raw_search"))
        self.assertEqual(("todo", "待办"), ct.classify_tool("todo_write"))
        self.assertEqual(("mcp", "MCP"), ct.classify_tool("mcp-BajieAsk-wait_message"))
        self.assertEqual(("mcp", "MCP"), ct.classify_tool("user-rxyy MCP-zhi"))

    def test_ask_and_switch_mode_are_told_apart(self):
        self.assertEqual(("ask", "提问"), ct.classify_tool("ask_question"))
        self.assertEqual(("ask", "提问"), ct.classify_tool("AskQuestion"))
        self.assertEqual(("ask", "切换模式"), ct.classify_tool("switch_mode"))

    def test_unknown_tool_keeps_its_name(self):
        self.assertEqual(("tool", "frobnicate"), ct.classify_tool("frobnicate"))
        self.assertEqual(("tool", "工具"), ct.classify_tool(""))


class ParseBubbleTests(unittest.TestCase):
    def test_shell_tool_summary_is_the_command_and_result_is_the_output(self):
        b = _tool("b1", "run_terminal_command_v2",
                  params={"command": "py -3.11 -m pytest rxyy_mcp/tests -q", "cwd": "d:\\x",
                          "options": {"timeout": 30000}},
                  result={"output": "1694 passed in 98.02s\n"},
                  add={"status": "success", "startedAtMs": 1788417088963})
        s = ct.parse_bubble(b)
        self.assertEqual("tool", s["kind"])
        self.assertEqual(("shell", "运行命令", "done"), (s["tool"], s["label"], s["status"]))
        self.assertEqual("py -3.11 -m pytest rxyy_mcp/tests -q", s["summary"])
        self.assertIn("1694 passed", s["result"])
        self.assertAlmostEqual(1788417088.963, s["started"], places=2)

    def test_tool_step_carries_the_models_one_line_explanation(self):
        # Bajie 工具卡第二行「这一步为什么做」= Cursor 工具 schema 里的 explanation / description /
        # instructions；09-07 卡片流把它单列一行。rawArgs 优先，params 兜底，MCP 工具没有就没有
        b = _tool("b1e", "run_terminal_command_v2",
                  raw={"command": "ls", "explanation": "See what the walkthrough is polling on the API"},
                  params={"command": "ls"})
        self.assertEqual("See what the walkthrough is polling on the API", ct.parse_bubble(b)["why"])
        b2 = _tool("b2e", "Shell", params={"command": "git status", "description": "看工作树脏没脏"})
        self.assertEqual("看工作树脏没脏", ct.parse_bubble(b2)["why"])
        b3 = _tool("b3e", "edit_file_v2", raw={"target_file": "a.py", "instructions": "把超时改成 60s\n第二行"})
        self.assertEqual("把超时改成 60s 第二行", ct.parse_bubble(b3)["why"], "只留一行")
        b4 = _tool("b4e", "mcp-playwright-browser_click", raw={"element": "x", "ref": "e1"})
        self.assertNotIn("why", ct.parse_bubble(b4))
        # 09-07 真机（Cursor 1.128）：终端的说明在 params.commandDescription；MCP 调用在
        # rawArgs.args.mcpDetails.description（CallMcpTool 让模型写的那句人话）
        b5 = _tool("b5e", "run_terminal_command_v2",
                   params={"command": "git status", "commandDescription": "看工作树脏没脏", "cwd": "d:\\x"})
        self.assertEqual("看工作树脏没脏", ct.parse_bubble(b5)["why"])
        b6 = _tool("b6e", "mcp-rxyy MCP-zt",
                   raw={"name": "zt", "toolName": "zt", "serverIdentifier": "user-rxyy MCP",
                        "args": {"status": "testing", "mcpDetails": {"description": "报进度"}}},
                   params={"tools": []})
        self.assertEqual("报进度", ct.parse_bubble(b6)["why"])

    def test_a_running_command_is_running_even_when_additionalData_says_cancelled(self):
        # 09-03 实测：命令还在跑时 additionalData 就先写着 "cancelled"
        b = _tool("b2", "run_terminal_command_v2", status="loading",
                  params={"command": "py -3.11 x.py"}, add={"status": "cancelled"})
        self.assertEqual("running", ct.parse_bubble(b)["status"])

    def test_a_finished_command_that_was_cancelled_shows_cancelled(self):
        b = _tool("b3", "run_terminal_command_v2", status="completed",
                  params={"command": "sleep 100"}, add={"status": "cancelled"})
        self.assertEqual("cancelled", ct.parse_bubble(b)["status"])

    def test_read_file_shows_basename_and_range(self):
        b = _tool("b4", "read_file_v2",
                  raw={"path": "d:\\桌面\\working\\cursor工作流\\rxyy_mcp\\hub.py", "offset": 3187, "limit": 70},
                  params={"targetFile": "d:\\x\\hub.py"}, result={"totalLinesInFile": 6418})
        s = ct.parse_bubble(b)
        self.assertEqual(("read", "读文件"), (s["tool"], s["label"]))
        self.assertEqual("hub.py L3187~+70", s["summary"])
        self.assertEqual("全文 6418 行", s["result"])

    def test_edit_file_shows_basename_and_written(self):
        b = _tool("b5", "edit_file_v2",
                  params={"relativeWorkspacePath": "d:\\x\\rxyy_mcp\\tests\\test_green_light_stuck.py"},
                  result={"beforeContentId": "composer.content.aa", "afterContentId": "composer.content.bb"})
        s = ct.parse_bubble(b)
        self.assertEqual("test_green_light_stuck.py", s["summary"])
        self.assertEqual("已写入", s["result"])

    def test_search_shows_pattern_and_match_counts_from_additionalData(self):
        b = _tool("b6", "ripgrep_raw_search",
                  raw={"pattern": "def _flush_queue|def _rescue", "path": "d:\\x\\rxyy_mcp\\hub.py"},
                  add={"isPruned": True, "totalFiles": 1, "totalMatches": 4})
        s = ct.parse_bubble(b)
        self.assertEqual("def _flush_queue|def _rescue · hub.py", s["summary"])
        self.assertEqual("4 处匹配 · 1 个文件", s["result"])

    def test_mcp_tool_shows_inner_name_args_and_reply_text(self):
        inner = {"content": [{"type": "text", "text": "[OK] 已写入回复（会话 X）。"}]}
        b = _tool("b7", "mcp-BajieAsk-reply_message",
                  raw={"name": "user-BajieAsk-reply_message",
                       "args": {"sessionId": "X", "content": "# 标题"}, "providerIdentifier": "BajieAsk"},
                  params={"tools": [{"name": "reply_message", "parameters": "{}", "serverName": "BajieAsk"}]},
                  result={"result": json.dumps(inner, ensure_ascii=False)}, add={"status": "success"})
        s = ct.parse_bubble(b)
        self.assertEqual(("mcp", "MCP"), (s["tool"], s["label"]))
        self.assertTrue(s["summary"].startswith("BajieAsk-reply_message"), s["summary"])
        self.assertIn('"sessionId": "X"', s["summary"])
        self.assertEqual("[OK] 已写入回复（会话 X）。", s["result"])

    def test_mcp_tool_without_rawArgs_falls_back_to_params_tools(self):
        b = _tool("b8", "mcp-rxyy MCP-zhi",
                  params={"tools": [{"name": "zhi", "parameters": '{"message":"进展"}', "serverName": "rxyy MCP"}]})
        self.assertEqual("rxyy MCP/zhi {\"message\": \"进展\"}", ct.parse_bubble(b)["summary"])

    def test_todo_summarises_progress(self):
        b = _tool("b9", "todo_write", params={"merge": True},
                  result={"success": True, "finalTodos": [
                      {"content": "写模块", "status": "completed", "id": "a"},
                      {"content": "写测试", "status": "in_progress", "id": "b"},
                      {"content": "部署", "status": "pending", "id": "c"}]})
        s = ct.parse_bubble(b)
        self.assertEqual("", s["summary"])
        self.assertEqual("1/3 完成；进行中：写测试", s["result"])

    def test_errored_tool_carries_the_error_text(self):
        b = _tool("b10", "await", status="error", raw={}, error="Cannot await shell task")
        s = ct.parse_bubble(b)
        self.assertEqual("error", s["status"])
        self.assertEqual("等命令结束", s["label"])
        self.assertEqual("Cannot await shell task", s["result"])

    def test_thinking_bubble(self):
        s = ct.parse_bubble(_think("t1", "  I found a real bug in tag_along…  ", ms=2725))
        self.assertEqual({"kind": "thinking", "text": "I found a real bug in tag_along…", "ms": 2725},
                         {k: s[k] for k in ("kind", "text", "ms")})
        self.assertGreater(s["at"], 0)

    def test_empty_typed_thinking_is_kept_for_streaming(self):
        s = ct.parse_bubble(_think("t0", "   ", ms=0))
        self.assertEqual("thinking", s["kind"])
        self.assertEqual("", s["text"])
        self.assertIsNone(ct.parse_bubble(_bubble("z1")), "没标思考类型的空泡仍跳过")

    def test_edit_and_read_steps_carry_full_path_for_copy(self):
        """摘要只有文件名；前端「复制路径」要完整路径（09-07 读库做透清单）。"""
        b = _tool("e1", "edit_file", raw={"target_file": r"D:\a\b\c.py", "instructions": "x"})
        s = ct.parse_bubble(b)
        self.assertEqual(r"D:\a\b\c.py", s["path"])
        self.assertEqual("c.py", s["summary"])
        b2 = _tool("r1", "read_file", raw={"path": "src/x.py", "offset": 3, "limit": 20})
        self.assertEqual("src/x.py", ct.parse_bubble(b2)["path"])
        b3 = _tool("s1", "run_terminal_command_v2", raw={"command": "dir"})
        self.assertNotIn("path", ct.parse_bubble(b3))

    def test_0907_json_string_thinking_is_unwrapped(self):
        """09-07 真机 8adae4ba：部分模型的 thinking 落库是整段 JSON 字符串
        {"text": "...", "isLastThinkingChunk": true}，以前时间线上糊一坨花括号。"""
        payload = json.dumps({"text": "x.ai/bot 直接被拒绝", "isLastThinkingChunk": True}, ensure_ascii=False)
        s = ct.parse_bubble(_think("t7", payload, ms=3200))
        self.assertEqual("x.ai/bot 直接被拒绝", s["text"])
        # thinking 本身就是字符串（不是 dict）时同样解开
        s2 = ct.parse_bubble(_bubble("t8", thinking=payload, capabilityType=30))
        self.assertEqual("x.ai/bot 直接被拒绝", s2["text"])
        # 普通思考文字里碰巧以 { 开头但不是 JSON：原样保留
        s3 = ct.parse_bubble(_think("t9", "{a: 1} 这是伪代码", ms=1))
        self.assertEqual("{a: 1} 这是伪代码", s3["text"])

    def test_text_bubble(self):
        s = ct.parse_bubble(_text("x1", "Committed as `d08bc95`."))
        self.assertEqual(("text", "Committed as `d08bc95`."), (s["kind"], s["text"]))

    def test_error_details_bubble(self):
        b = _bubble("e1", errorDetails={"error": {
            "error": "ERROR_NOT_LOGGED_IN",
            "details": {"title": "Authentication error", "detail": "If you are logged in, try again.",
                        "isRetryable": False}}, "requestId": "r"})
        s = ct.parse_bubble(b)
        self.assertEqual("error", s["kind"])
        self.assertEqual("Authentication error：If you are logged in, try again.", s["text"])
        self.assertEqual("ERROR_NOT_LOGGED_IN", s["code"])

    def test_error_details_given_as_a_json_string_is_unpacked(self):
        # 09-03 线上实测形态：error 是一段 JSON 字符串
        raw = json.dumps({"error": "ERROR_CUSTOM_MESSAGE",
                          "details": {"title": "failed_precondition",
                                      "detail": "这条对话的驻留会话已结束，请新开一个对话", "isRetryable": False}},
                         ensure_ascii=False)
        s = ct.parse_bubble(_bubble("e2", errorDetails={"error": raw, "requestId": "r"}))
        self.assertEqual("failed_precondition：这条对话的驻留会话已结束，请新开一个对话", s["text"])
        self.assertEqual("ERROR_CUSTOM_MESSAGE", s["code"])

    def test_user_bubble_and_empty_bubble_are_skipped(self):
        self.assertIsNone(ct.parse_bubble(_user("u1", "hi")))
        self.assertIsNone(ct.parse_bubble(_bubble("z1")))
        self.assertIsNone(ct.parse_bubble("junk"))

    def test_long_texts_are_clipped(self):
        s = ct.parse_bubble(_text("x2", "A" * 5000))
        self.assertEqual(4001, len(s["text"]))
        self.assertTrue(s["text"].endswith("…"))


# Cursor 原生提问的真实形态：09-03 从本机库 composer 47435c3d 那条（08-31 08:31
# 「OpenMontage 派单路线」，两问）逐字抄下来的。要点：题目在 params、**没有 rawArgs**，
# 答案在 result.answers，进行中的选择在 additionalData.currentSelections。
ASK_PARAMS = {
    "title": "OpenMontage 派单路线",
    "questions": [
        {"id": "route", "prompt": "OpenMontage 已在线（http://192.168.71.157:30731/），这条派单接下来怎么走？",
         "options": [
             {"id": "as_is", "label": "按现状交付（推荐）：不动线上服务，把 URL 和能力边界回给潘剑"},
             {"id": "refresh_demo", "label": "只补演示内容：不升级，用现有 key 新跑 1 条真实生产"},
             {"id": "upgrade", "label": "只升级：备份 → pull 到最新 → 重装依赖 → 重启 → 复验"}]},
        {"id": "notify", "prompt": "要不要现在就通过任务安排站回潘剑？",
         "options": [{"id": "now", "label": "现在就回"}, {"id": "after", "label": "等上面选的活干完再一起回"}]},
    ],
}


def _ask(bid, status="completed", result=None, add=None):
    """提问气泡：params 里带题、rawArgs 一律缺（实测就是这样）。"""
    return _tool(bid, "ask_question", status=status, params=ASK_PARAMS, result=result, add=add)


class AskQuestionTests(unittest.TestCase):
    """AskQuestion 显示（09-01 §六 待办）：原生提问是**阻塞**的一步，问了什么、答没答
    必须在控制台/手机上看得见，否则用户只看到 agent 卡着不动，不知道它在等人点。"""

    def test_answered_question_shows_prompts_options_and_what_was_picked(self):
        s = ct.parse_bubble(_ask(
            "q1",
            result={"answers": [
                {"questionId": "route", "selectedOptionIds": ["refresh_demo"], "freeformText": ""},
                {"questionId": "notify", "selectedOptionIds": ["after"], "freeformText": "顺便把 URL 贴给他"}]},
            add={"status": "submitted", "currentSelections": {"route": ["refresh_demo"]},
                 "freeformTexts": {}, "completionDelivered": True}))
        self.assertEqual(("ask", "提问", "done"), (s["tool"], s["label"], s["status"]))
        # 摘要：标题 + 几问（老代码只读 rawArgs，这里恒为空串——那正是「看不见」的根）
        self.assertEqual("OpenMontage 派单路线 · 2 问", s["summary"])
        self.assertFalse(s["blocking"])
        self.assertEqual("answered", s["ask"]["state"])
        self.assertEqual(["refresh_demo"], [o["id"] for o in s["ask"]["questions"][0]["options"] if o["picked"]])
        self.assertEqual("顺便把 URL 贴给他", s["ask"]["questions"][1]["freeform"])
        body = s["result"]
        self.assertIn("✓ 已答（2 问）", body)
        self.assertIn("要不要现在就通过任务安排站回潘剑？", body)
        self.assertIn("● 只补演示内容", body)
        self.assertIn("○ 按现状交付", body)
        self.assertIn("→ 选了：等上面选的活干完再一起回", body)
        self.assertIn("→ 补充：顺便把 URL 贴给他", body)

    def test_a_question_still_waiting_says_it_is_blocking_and_tab_line_says_wait_for_you(self):
        s = ct.parse_bubble(_ask("q2", status="loading"))
        self.assertEqual("running", s["status"])
        self.assertTrue(s["blocking"])
        self.assertEqual("waiting", s["ask"]["state"])
        self.assertIn("⏳ 等你在 Cursor 对话里点选（2 问 · 5 个选项）", s["result"])
        view = ct.last_step_view({"total_steps": 9, "live": True, "updated_at": 5.0, "steps": [s]})
        self.assertEqual("提问·等你答", view["label"], "tab 上要说清是在等人，不是在跑")

    def test_partial_clicks_while_waiting_are_shown_as_already_selected(self):
        s = ct.parse_bubble(_ask("q3", status="loading",
                                 add={"currentSelections": {"route": ["upgrade"]},
                                      "freeformTexts": {"notify": "我自己跟他说"}}))
        self.assertEqual("waiting", s["ask"]["state"])
        self.assertEqual(["只升级：备份 → pull 到最新 → 重装依赖 → 重启 → 复验"],
                         s["ask"]["questions"][0]["answer"])
        self.assertIn("→ 补充：我自己跟他说", s["result"])

    def test_a_cancelled_question_says_it_was_never_answered(self):
        s = ct.parse_bubble(_ask("q4", status="completed", add={"status": "cancelled"}))
        self.assertEqual("cancelled", s["status"])
        self.assertEqual("cancelled", s["ask"]["state"])
        self.assertFalse(s["blocking"])
        self.assertIn("⊘ 这次提问没答成", s["result"])

    def test_a_question_shaped_tool_without_questions_falls_back_quietly(self):
        s = ct.parse_bubble(_tool("q5", "switch_mode", params={"target_mode_id": "plan"},
                                  result={"ok": True}))
        self.assertEqual(("ask", "切换模式"), (s["tool"], s["label"]))
        self.assertNotIn("ask", s)
        self.assertNotIn("blocking", s)
        self.assertIn("ok", s["result"])
        self.assertIsNone(ct.parse_ask({"params": json.dumps({"title": "空"})}))
        self.assertIsNone(ct.parse_ask("junk"))
        self.assertEqual("", ct.render_ask(None))

    def test_single_question_summary_has_no_count_and_falls_back_to_the_prompt(self):
        one = {"questions": [{"id": "a", "prompt": "要不要顺手部署两机？",
                              "options": [{"id": "y", "label": "部署"}, {"id": "n", "label": "先不"}]}]}
        s = ct.parse_bubble(_tool("q6", "ask_question", status="loading", params=one))
        self.assertEqual("要不要顺手部署两机？", s["summary"], "只有一问就不写「N 问」")

    def test_a_waiting_question_is_never_cached_so_the_answer_shows_up_next_tick(self):
        bubbles = [_user("u1", "开工"), _ask("q7", status="loading")]
        cache = {}
        with tempfile.TemporaryDirectory() as tmp:
            appdata, _ = _fake_db(tmp, _composer(bubbles), bubbles)
            t = ct.read_turn(UID, appdata=appdata, cache=cache, now=time.time())
        self.assertTrue(t["live"], "有人被问着 = 这一轮还活着，面板要继续拍")
        self.assertNotIn("q7", cache)
        self.assertTrue(t["steps"][-1]["blocking"])


def _fake_db(tmp, composer, bubbles):
    root = Path(tmp)
    gs = root / "Cursor" / "User" / "globalStorage"
    gs.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(gs / "state.vscdb"))
    con.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")
    con.execute("INSERT INTO cursorDiskKV VALUES (?,?)",
                ("composerData:" + UID, json.dumps(composer, ensure_ascii=False)))
    for b in bubbles:
        con.execute("INSERT INTO cursorDiskKV VALUES (?,?)",
                    ("bubbleId:{}:{}".format(UID, b["bubbleId"]), json.dumps(b, ensure_ascii=False)))
    con.commit()
    con.close()
    return str(root), gs / "state.vscdb"


def _composer(bubbles, generating=(), status="completed", extra_heads=(), ctx=None):
    heads = [{"bubbleId": b["bubbleId"], "type": b["type"]} for b in bubbles]
    heads += [{"bubbleId": bid, "type": 2} for bid in extra_heads]
    comp = {"composerId": UID, "name": "FRAP", "status": status,
            "generatingBubbleIds": list(generating),
            "fullConversationHeadersOnly": heads, "lastUpdatedAt": 1788415679971}
    # 09-03 实测：每个 composerData 上都有这三个字段（FRAP 那个对话正是
    # 484306/1000000 = 48.4306%）。默认给上，夹具才像真的
    comp.update(ctx if ctx is not None else
                {"contextTokensUsed": 484306, "contextTokenLimit": 1000000,
                 "contextUsagePercent": 48.4306})
    return comp


class ContextUsageTests(unittest.TestCase):
    """上下文过长提示（09-03 对比文档 §8 落地顺序③）：Cursor 自己就在 composerData 上
    记着精确用量，不必像 bajie 那样拿「≥120 条消息」猜——同样 120 条可能是 20K 也可能
    是 600K。字段与数值都是 09-03 从本机库四个对话实测抄下来的。"""

    def test_real_numbers_from_the_db_become_a_human_label(self):
        c = ct.context_usage({"contextTokensUsed": 484306, "contextTokenLimit": 1000000,
                              "contextUsagePercent": 48.4306})
        self.assertEqual((484306, 1000000, 48.4), (c["used"], c["limit"], c["pct"]))
        self.assertEqual("ok", c["level"])
        self.assertEqual("上下文 48%（484K / 1.00M）", c["label"])
        self.assertEqual("", c["hint"])

    def test_the_three_thresholds_each_say_what_to_do(self):
        def lvl(pct):
            return ct.context_usage({"contextTokensUsed": int(pct * 10000),
                                     "contextTokenLimit": 1000000,
                                     "contextUsagePercent": pct})
        self.assertEqual("ok", lvl(64.9)["level"])
        self.assertEqual("warn", lvl(65.0)["level"])
        self.assertIn("准备接力", lvl(70)["hint"])
        self.assertEqual("high", lvl(80.0)["level"])
        self.assertIn("自动摘要压缩", lvl(85)["hint"], "该交接的理由要写清，不能只报数字")
        self.assertEqual("critical", lvl(92.0)["level"])
        self.assertIn("立刻交接", lvl(99)["hint"])

    def test_percent_is_recomputed_when_cursor_did_not_write_it(self):
        c = ct.context_usage({"contextTokensUsed": 900000, "contextTokenLimit": 1000000})
        self.assertEqual(90.0, c["pct"])
        self.assertEqual("high", c["level"])

    def test_missing_or_broken_fields_are_reported_as_unknown_not_zero(self):
        self.assertIsNone(ct.context_usage({}))
        self.assertIsNone(ct.context_usage({"contextTokensUsed": 100}), "没有上限就量不出比例")
        self.assertIsNone(ct.context_usage({"contextTokenLimit": 0, "contextTokensUsed": 5}))
        self.assertIsNone(ct.context_usage({"contextTokenLimit": "x", "contextTokensUsed": 5}))
        self.assertIsNone(ct.context_usage("junk"))

    def test_read_turn_carries_the_usage_and_only_the_level_enters_the_repaint_sig(self):
        bubbles = [_user("u1", "开工"), _text("x1", "答")]
        with tempfile.TemporaryDirectory() as tmp:
            appdata, _ = _fake_db(tmp, _composer(bubbles), bubbles)
            t = ct.read_turn(UID, appdata=appdata)
        self.assertEqual(48.4, t["context"]["pct"])
        self.assertEqual("上下文 48%（484K / 1.00M）", t["context"]["label"])
        with tempfile.TemporaryDirectory() as tmp:
            # 同一档里 pct 涨了几个 token：sig 必须不变，否则 UI 每拍白重建整段 DOM
            appdata, _ = _fake_db(tmp, _composer(bubbles, ctx={
                "contextTokensUsed": 484999, "contextTokenLimit": 1000000,
                "contextUsagePercent": 48.4999}), bubbles)
            t2 = ct.read_turn(UID, appdata=appdata)
        self.assertEqual(t["sig"], t2["sig"])
        with tempfile.TemporaryDirectory() as tmp:
            appdata, _ = _fake_db(tmp, _composer(bubbles, ctx={
                "contextTokensUsed": 850000, "contextTokenLimit": 1000000,
                "contextUsagePercent": 85.0}), bubbles)
            t3 = ct.read_turn(UID, appdata=appdata)
        self.assertEqual("high", t3["context"]["level"])
        self.assertNotEqual(t["sig"], t3["sig"], "换档了就该重画")

    def test_a_composer_without_the_fields_just_has_no_badge(self):
        bubbles = [_user("u1", "开工"), _text("x1", "答")]
        with tempfile.TemporaryDirectory() as tmp:
            appdata, _ = _fake_db(tmp, _composer(bubbles, ctx={}), bubbles)
            t = ct.read_turn(UID, appdata=appdata)
        self.assertIsNone(t["context"])

    def test_read_context_usage_reads_one_row_without_touching_bubbles(self):
        bubbles = [_user("u1", "开工"), _text("x1", "答")]
        with tempfile.TemporaryDirectory() as tmp:
            appdata, _ = _fake_db(tmp, _composer(bubbles), bubbles)
            self.assertEqual(48.4, ct.read_context_usage(UID, appdata=appdata)["pct"])
            self.assertIsNone(ct.read_context_usage("no-such-uuid", appdata=appdata))
            self.assertIsNone(ct.read_context_usage("", appdata=appdata))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(ct.read_context_usage(UID, appdata=tmp), "库不在也不许抛")


class ReadTurnTests(unittest.TestCase):
    def test_turn_is_everything_after_the_last_user_bubble(self):
        bubbles = [
            _user("u0", "第一问"), _text("a0", "第一答"),
            _user("u1", "你就看你（bajie 这个 mcp）当前是怎么做的，把rxyy MCP 也改成这样"),
            _think("t1", "先探库里的气泡结构"),
            _tool("c1", "run_terminal_command_v2", params={"command": "py probe.py"},
                  result={"output": "composers=654"}, add={"status": "success"}),
            _text("x1", "数据源探明了。"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            appdata, _ = _fake_db(tmp, _composer(bubbles), bubbles)
            turn = ct.read_turn(UID, appdata=appdata, now=time.time())
        self.assertEqual(3, turn["total_steps"], "第一轮的一问一答不算进这一轮")
        self.assertEqual(["thinking", "tool", "text"], [s["kind"] for s in turn["steps"]])
        self.assertTrue(turn["user_text"].startswith("你就看你"))
        self.assertFalse(turn["generating"])
        self.assertEqual(0, turn["omitted"])
        self.assertEqual(0, turn["pending_bubbles"])
        self.assertEqual(12, len(turn["sig"]))
        self.assertEqual({"kind": "text", "text": "数据源探明了。"}, turn["reply"])


class ComposeReplyTests(unittest.TestCase):
    """前端实时输出 Cursor 详细回复（09-03 决策卡自由输入）。

    事故：时间线把正文混进最多 40 步、收起后只剩「Agent 正在回复…」，控制台看不到
    Cursor 里正在写的那段话。compose_reply 把 text 步拼成一段；还没正文时顶上最新思考。
    """

    def test_all_text_steps_are_joined_in_order(self):
        r = ct.compose_reply([
            {"kind": "thinking", "text": "先想"},
            {"kind": "tool", "text": ""},
            {"kind": "text", "text": "第一段"},
            {"kind": "text", "text": "第二段"},
        ])
        self.assertEqual({"kind": "text", "text": "第一段\n\n第二段"}, r)

    def test_without_text_the_latest_thinking_is_shown(self):
        r = ct.compose_reply([
            {"kind": "thinking", "text": "旧想法"},
            {"kind": "tool", "label": "读文件"},
            {"kind": "thinking", "text": "新想法：改 session_locator"},
        ])
        self.assertEqual({"kind": "thinking", "text": "新想法：改 session_locator"}, r)

    def test_tools_only_have_no_reply_pane(self):
        self.assertIsNone(ct.compose_reply([{"kind": "tool", "label": "运行命令"}]))
        self.assertIsNone(ct.compose_reply([]))
        self.assertIsNone(ct.compose_reply(None))

    def test_read_turn_exposes_reply_and_thinking_fallback(self):
        texts = [_user("u1", "开工"), _think("t1", "先探库"),
                 _tool("c1", "run_terminal_command_v2", params={"command": "py probe.py"},
                       result={"output": "ok"}, add={"status": "success"}),
                 _text("x1", "探明了。"), _text("x2", "下一步改 UI。")]
        with tempfile.TemporaryDirectory() as tmp:
            appdata, _ = _fake_db(tmp, _composer(texts), texts)
            t = ct.read_turn(UID, appdata=appdata, now=time.time())
        self.assertEqual("text", t["reply"]["kind"])
        self.assertEqual("探明了。\n\n下一步改 UI。", t["reply"]["text"])
        thinking_only = [_user("u1", "开工"), _think("t1", "还在想")]
        with tempfile.TemporaryDirectory() as tmp:
            appdata, _ = _fake_db(tmp, _composer(thinking_only, generating=["t1"]), thinking_only)
            t2 = ct.read_turn(UID, appdata=appdata, now=time.time())
        self.assertEqual({"kind": "thinking", "text": "还在想"}, t2["reply"])
        self.assertEqual("running", t2["steps"][-1]["status"])

    def test_reply_length_enters_the_repaint_sig(self):
        a = [_user("u1", "开工"), _text("x1", "短")]
        b = [_user("u1", "开工"), _text("x1", "短一些的正文会变长")]
        with tempfile.TemporaryDirectory() as tmp:
            appdata, _ = _fake_db(tmp, _composer(a), a)
            sa = ct.read_turn(UID, appdata=appdata)["sig"]
        with tempfile.TemporaryDirectory() as tmp:
            appdata, _ = _fake_db(tmp, _composer(b), b)
            sb = ct.read_turn(UID, appdata=appdata)["sig"]
        self.assertNotEqual(sa, sb, "正文变长了面板必须重画，否则用户一直看着旧的半句")


class ReadTurnRestTests(unittest.TestCase):
    def test_window_keeps_only_the_last_max_steps_and_reports_omitted(self):
        bubbles = [_user("u1", "开工")] + [_text("x%d" % i, "第 %d 步" % i) for i in range(12)]
        with tempfile.TemporaryDirectory() as tmp:
            appdata, _ = _fake_db(tmp, _composer(bubbles), bubbles)
            turn = ct.read_turn(UID, appdata=appdata, max_steps=5)
        self.assertEqual(12, turn["total_steps"])
        self.assertEqual(7, turn["omitted"])
        self.assertEqual(["第 7 步", "第 8 步", "第 9 步", "第 10 步", "第 11 步"],
                         [s["text"] for s in turn["steps"]])

    def test_generating_or_a_running_tool_or_an_unwritten_bubble_means_live(self):
        base = [_user("u1", "开工"), _think("t1", "想", at="2026-09-03T06:30:40.000Z")]
        long_ago = time.time()  # 夹具 createdAt 是 06:30Z，与 now 相差远 → 单靠时间不算 live
        with tempfile.TemporaryDirectory() as tmp:
            appdata, _ = _fake_db(tmp, _composer(base), base)
            self.assertFalse(ct.read_turn(UID, appdata=appdata, now=long_ago)["live"],
                             "早就停了的一轮不该算 live")
        with tempfile.TemporaryDirectory() as tmp:
            appdata, _ = _fake_db(tmp, _composer(base, generating=["t1"]), base)
            self.assertTrue(ct.read_turn(UID, appdata=appdata, now=long_ago)["live"])
        running = base + [_tool("c1", "run_terminal_command_v2", status="loading",
                                params={"command": "pytest"}, add={"status": "cancelled"})]
        with tempfile.TemporaryDirectory() as tmp:
            appdata, _ = _fake_db(tmp, _composer(running), running)
            t = ct.read_turn(UID, appdata=appdata, now=long_ago)
            self.assertTrue(t["live"])
            self.assertEqual("running", t["steps"][-1]["status"])
        with tempfile.TemporaryDirectory() as tmp:
            # 头已登记、正文还没落盘 = 正在写的那条
            appdata, _ = _fake_db(tmp, _composer(base, extra_heads=["ghost"]), base)
            t = ct.read_turn(UID, appdata=appdata, now=long_ago)
            self.assertTrue(t["live"])
            self.assertEqual(1, t["pending_bubbles"])
            self.assertEqual(2, t["total_steps"])
            self.assertEqual(1, len(t["steps"]))

    def test_recent_activity_alone_counts_as_live(self):
        now = time.time()
        iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now - 5))
        bubbles = [_user("u1", "开工", at=iso), _text("x1", "刚说的", at=iso)]
        with tempfile.TemporaryDirectory() as tmp:
            appdata, _ = _fake_db(tmp, _composer(bubbles), bubbles)
            self.assertTrue(ct.read_turn(UID, appdata=appdata, now=now)["live"])
            self.assertFalse(ct.read_turn(UID, appdata=appdata, now=now + 120)["live"])

    def test_finished_bubbles_are_cached_and_the_tail_is_reread(self):
        bubbles = [
            _user("u1", "开工"),
            _tool("c1", "read_file_v2", raw={"path": "a.py"}, result={"totalLinesInFile": 3}),
            _tool("c2", "run_terminal_command_v2", status="loading", params={"command": "pytest"}),
        ]
        cache = {}
        with tempfile.TemporaryDirectory() as tmp:
            appdata, db = _fake_db(tmp, _composer(bubbles), bubbles)
            t1 = ct.read_turn(UID, appdata=appdata, cache=cache)
            self.assertEqual(["done", "running"], [s["status"] for s in t1["steps"]])
            self.assertIn("c1", cache, "跑完的工具定型、进缓存")
            self.assertNotIn("c2", cache, "还在跑的不缓存，下拍要重读")
            # 库里：c1 被改掉（真实世界不会，这里用来证明它确实走的是缓存）；c2 跑完了
            con = sqlite3.connect(str(db))
            con.execute("UPDATE cursorDiskKV SET value=? WHERE key=?",
                        (json.dumps(_tool("c1", "read_file_v2", raw={"path": "CHANGED.py"},
                                          result={"totalLinesInFile": 99})),
                         "bubbleId:{}:c1".format(UID)))
            con.execute("UPDATE cursorDiskKV SET value=? WHERE key=?",
                        (json.dumps(_tool("c2", "run_terminal_command_v2", status="completed",
                                          params={"command": "pytest"},
                                          result={"output": "3 passed"}, add={"status": "success"})),
                         "bubbleId:{}:c2".format(UID)))
            con.commit()
            con.close()
            t2 = ct.read_turn(UID, appdata=appdata, cache=cache)
        self.assertEqual("a.py", t2["steps"][0]["summary"], "缓存命中：没重读 c1")
        self.assertEqual("done", t2["steps"][1]["status"])
        self.assertIn("3 passed", t2["steps"][1]["result"])
        self.assertIn("c2", cache)
        self.assertNotEqual(t1["sig"], t2["sig"], "状态变了签名就得变（前端靠它决定要不要重画）")

    def test_missing_composer_or_db_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(ct.read_turn(UID, appdata=tmp), "没有库")
            appdata, _ = _fake_db(tmp, _composer([]), [])
            self.assertIsNone(ct.read_turn("nope-" + UID, appdata=appdata), "没这个对话")
        self.assertIsNone(ct.read_turn("", appdata="x"))


class EditDiffTests(unittest.TestCase):
    """打磨 ②：改文件步骤接 diff——前后全文存在 composer.content.<sha> 键里。"""

    def test_compute_diff_counts_and_previews(self):
        before = "a\nb\nc\nd\n"
        after = "a\nB\nc\nd\ne\n"
        d = ct.compute_diff(before, after)
        self.assertEqual((2, 1), (d["plus"], d["minus"]))
        self.assertIn("-b", d["preview"])
        self.assertIn("+B", d["preview"])
        self.assertIn("+e", d["preview"])
        self.assertNotIn("---", d["preview"], "不带 ---/+++ 文件头")
        self.assertIn("@@", d["preview"], "@@ 行留着好定位")

    def test_compute_diff_preview_is_capped_but_counts_are_full(self):
        before = "\n".join("l%d" % i for i in range(300))
        after = "\n".join("L%d" % i for i in range(300))
        d = ct.compute_diff(before, after, preview_lines=10)
        self.assertEqual((300, 300), (d["plus"], d["minus"]))
        self.assertIn("还有", d["preview"])
        self.assertLess(d["preview"].count("\n"), 14)

    def _edit_db(self, tmp, before, after, bubble_status="completed", with_content=True,
                 big=False):
        bid_before = "composer.content.aaaa"
        bid_after = "composer.content.bbbb"
        bubbles = [_user("u1", "改一下"),
                   _tool("e1", "edit_file_v2", status=bubble_status,
                         params={"relativeWorkspacePath": "d:\\x\\hub.py"},
                         result={"beforeContentId": bid_before, "afterContentId": bid_after})]
        appdata, db = _fake_db(tmp, _composer(bubbles), bubbles)
        if with_content:
            con = sqlite3.connect(str(db))
            con.execute("INSERT INTO cursorDiskKV VALUES (?,?)", (bid_before, before))
            con.execute("INSERT INTO cursorDiskKV VALUES (?,?)",
                        (bid_after, ("x" * (ct.DIFF_MAX_BYTES + 10)) if big else after))
            con.commit()
            con.close()
        return appdata

    def test_edit_step_gets_plus_minus_and_a_diff_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata = self._edit_db(tmp, "a\nb\n", "a\nB\nc\n")
            cache = {}
            t = ct.read_turn(UID, appdata=appdata, cache=cache)
        st = t["steps"][-1]
        self.assertEqual("+2 / −1", st["result"])
        self.assertEqual((2, 1), (st["diff"]["plus"], st["diff"]["minus"]))
        self.assertIn("+B", st["diff"]["preview"])
        self.assertNotIn("content_ids", st, "算完就把键名摘掉")
        self.assertIn("e1", cache, "算完 diff 的改文件步骤定型、进缓存")

    def test_new_file_is_marked(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata = self._edit_db(tmp, "", "line1\nline2\n")
            st = ct.read_turn(UID, appdata=appdata)["steps"][-1]
        self.assertEqual("+2 / −0（新文件）", st["result"])

    def test_content_not_yet_written_is_retried_not_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata = self._edit_db(tmp, "a", "b", with_content=False)
            cache = {}
            st = ct.read_turn(UID, appdata=appdata, cache=cache)["steps"][-1]
            self.assertIn("还没落盘", st["result"])
            self.assertIn("content_ids", st)
            self.assertNotIn("e1", cache, "前后内容没到，下拍还得重读")

    def test_huge_files_skip_the_diff_but_still_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata = self._edit_db(tmp, "a", "b", big=True)
            cache = {}
            st = ct.read_turn(UID, appdata=appdata, cache=cache)["steps"][-1]
        self.assertIn("太大没算 diff", st["result"])
        self.assertNotIn("diff", st)
        self.assertIn("e1", cache)

    def test_running_edit_is_not_diffed_yet(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata = self._edit_db(tmp, "a", "b", bubble_status="loading")
            st = ct.read_turn(UID, appdata=appdata)["steps"][-1]
        self.assertEqual("running", st["status"])
        self.assertNotIn("diff", st)


class LastStepViewTests(unittest.TestCase):
    """打磨 ③：tab 列表上那一句「第 N 步 · 运行命令 · …」。"""

    def test_tool_step(self):
        v = ct.last_step_view({"total_steps": 12, "live": True, "updated_at": 5.0, "steps": [
            {"kind": "tool", "label": "运行命令", "name": "run_terminal_command_v2",
             "summary": "pytest -q", "status": "running"}]})
        self.assertEqual({"n": 12, "kind": "tool", "label": "运行命令", "summary": "pytest -q",
                          "status": "running", "live": True, "at": 5.0}, v)

    def test_thinking_text_and_error_steps(self):
        mk = lambda st: ct.last_step_view({"total_steps": 1, "live": False, "steps": [st]})
        self.assertEqual(("思考", "想想"), (mk({"kind": "thinking", "text": "想想"})["label"],
                                          mk({"kind": "thinking", "text": "想想"})["summary"]))
        self.assertEqual("输出", mk({"kind": "text", "text": "好了"})["label"])
        self.assertEqual("报错", mk({"kind": "error", "text": "x"})["label"])

    def test_empty(self):
        self.assertIsNone(ct.last_step_view(None))
        self.assertIsNone(ct.last_step_view({"steps": []}))


class TickLiveStepTests(unittest.TestCase):
    """hub._tick_live_step：只对 Cursor 正在生成的 tab 读库、3s 一拍、停了留 45s。"""

    def _sess(self, uid=UID, generating=True):
        s = hub.Session.__new__(hub.Session)
        s.id, s.conv_key, s.name = "t1", "351c6f9d", "x"
        s.cursor_uuid = uid
        s.live_cache = {"state": "working", "generating": generating}
        s.live_step_cache = None
        s.rev = 0
        return s

    def test_generating_session_gets_a_live_step(self):
        s = self._sess()
        turn = {"total_steps": 7, "live": True, "updated_at": 1.0, "steps": [
            {"kind": "tool", "label": "运行命令", "summary": "pytest", "status": "running"}]}
        with patch.object(hub.cursor_turns, "read_turn", lambda *a, **k: turn), \
                patch.dict(hub.Api._TURN_CACHE, {}, clear=True):
            hub.HUB._tick_live_step(s, 1000.0)
        self.assertEqual(7, s.live_step_cache["n"])
        self.assertEqual("运行命令", s.live_step_cache["label"])
        self.assertEqual(1000.0, s.live_step_cache["seen"])
        self.assertEqual(0, s.rev, "只是 tab 上一句话，不许动 rev（rev 一变 UI 重拉整段消息）")

    def test_probe_is_throttled_to_the_interval(self):
        s = self._sess()
        calls = []
        with patch.object(hub.cursor_turns, "read_turn",
                          lambda *a, **k: calls.append(1) or {"total_steps": 1, "live": True,
                                                              "steps": [{"kind": "text", "text": "a"}]}), \
                patch.dict(hub.Api._TURN_CACHE, {}, clear=True):
            hub.HUB._tick_live_step(s, 1000.0)
            hub.HUB._tick_live_step(s, 1001.0)
            hub.HUB._tick_live_step(s, 1002.0)
            hub.HUB._tick_live_step(s, 1003.5)
        self.assertEqual(2, len(calls))

    def test_not_generating_keeps_the_last_step_for_a_while_then_drops_it(self):
        s = self._sess(generating=False)
        s.live_step_cache = {"n": 3, "seen": 1000.0}
        with patch.object(hub.cursor_turns, "read_turn", lambda *a, **k: self.fail("没在生成不该读库")):
            hub.HUB._tick_live_step(s, 1010.0)
            self.assertIsNotNone(s.live_step_cache)
            hub.HUB._tick_live_step(s, 1000.0 + hub.HUB.LIVE_STEP_LINGER + 1)
            self.assertIsNone(s.live_step_cache)

    def test_without_cursor_uuid_nothing_is_read(self):
        s = self._sess(uid=None)
        s.live_step_cache = {"n": 1, "seen": 0}
        with patch.object(hub.cursor_turns, "read_turn", lambda *a, **k: self.fail("没绑对话不该读库")):
            hub.HUB._tick_live_step(s, 1000.0)
        self.assertIsNone(s.live_step_cache)


class ApiGetLiveTurnTests(unittest.TestCase):
    def _sess(self, uid):
        s = hub.Session.__new__(hub.Session)
        s.id, s.conv_key, s.name = "t1", "351c6f9d", "rxyy MCP·绿灯不熔"
        s.cursor_uuid = uid
        return s

    def test_without_a_bound_cursor_conversation_it_says_so(self):
        with patch.object(hub.HUB, "sessions", {"t1": self._sess(None)}):
            r = hub.Api().get_live_turn("t1")
        self.assertFalse(r["ok"])
        self.assertIn("Cursor 对话", r["why"])
        self.assertFalse(hub.Api().get_live_turn("nope")["ok"])

    def test_reads_the_turn_with_a_per_conversation_cache(self):
        calls = []

        def fake_read(uid, max_steps=40, cache=None):
            calls.append((uid, max_steps, cache))
            return {"uuid": uid, "steps": [{"kind": "text", "text": "hi"}], "sig": "abc", "live": True}

        with patch.object(hub.HUB, "sessions", {"t1": self._sess(UID)}), \
                patch.object(hub.cursor_turns, "read_turn", fake_read), \
                patch.dict(hub.Api._TURN_CACHE, {}, clear=True):
            r1 = hub.Api().get_live_turn("t1")
            r2 = hub.Api().get_live_turn("t1", max_steps=500)
            hub.Api().get_live_turn("t1", max_steps=5000)
        self.assertTrue(r1["ok"])
        self.assertEqual("abc", r1["turn"]["sig"])
        self.assertIn("now", r1)
        self.assertEqual(UID, calls[0][0])
        self.assertEqual(40, calls[0][1])
        self.assertEqual(500, calls[1][1], "历史查看允许读取 400+ 步")
        self.assertEqual(2000, calls[2][1], "历史读取仍有上限")
        self.assertIs(calls[0][2], calls[1][2], "同一个对话两拍用同一份缓存")

    def test_hook_snap_overlays_steps_not_just_reply(self):
        db = {"uuid": UID, "steps": [{"kind": "text", "id": "t", "text": "db"}],
              "sig": "db", "live": False, "reply": {"kind": "text", "text": "db"}}
        snap = {"ok": True, "recv_at": time.time(), "live": True, "at": 9,
                "thinking": "对照中", "reply": "正在输出…",
                "steps": [{"kind": "tool", "name": "Shell", "status": "running",
                           "bubbleId": "s1", "why": "跑脚本"}]}

        def fake_read(*a, **k):
            return dict(db)

        with patch.object(hub.HUB, "sessions", {"t1": self._sess(UID)}), \
                patch.object(hub.cursor_turns, "read_turn", fake_read), \
                patch.object(hub.wbhook, "latest_turn", lambda cid: snap), \
                patch.object(hub.wbhook, "watch", lambda *a, **k: None), \
                patch.dict(hub.Api._TURN_CACHE, {}, clear=True):
            r = hub.Api().get_live_turn("t1")
        self.assertTrue(r["ok"])
        self.assertEqual("wbhook", r["turn"]["via"])
        self.assertTrue(r["turn"]["live"])
        self.assertTrue(any(s.get("kind") == "thinking" for s in r["turn"]["steps"]))
        self.assertTrue(any(s.get("kind") == "tool" and s.get("why") == "跑脚本"
                            for s in r["turn"]["steps"]))
        self.assertEqual("正在输出…", r["turn"]["reply"]["text"])

    def test_unreadable_conversation_is_reported_not_raised(self):
        with patch.object(hub.HUB, "sessions", {"t1": self._sess(UID)}), \
                patch.object(hub.cursor_turns, "read_turn", lambda *a, **k: None), \
                patch.dict(hub.Api._TURN_CACHE, {}, clear=True):
            r = hub.Api().get_live_turn("t1")
        self.assertFalse(r["ok"])
        self.assertIn("读不到", r["why"])


class StateExposesGeneratingTests(unittest.TestCase):
    def test_agent_liveness_carries_generating_and_get_state_exposes_it(self):
        src = (MODULE_DIR / "session_core.py").read_text(encoding="utf-8")
        self.assertIn('"generating": bool(generating)', src)
        api_src = (MODULE_DIR / "hub_api.py").read_text(encoding="utf-8")
        self.assertIn('"live_generating": bool(live.get("generating"))', api_src)
        self.assertIn('"has_cursor": bool(getattr(s, "cursor_uuid", None))', api_src)

    def test_ui_has_the_timeline_panel_and_polls_get_live_turn(self):
        ui = (MODULE_DIR / "ui.html").read_text(encoding="utf-8")
        self.assertIn('id="liveTurn"', ui)
        self.assertIn("api().get_live_turn(", ui)
        self.assertIn("startPoll(liveTurnTick", ui)
        self.assertIn("renderLiveTurn(s);", ui)
        self.assertIn("s.live_step", ui, "tab 上那一句进度")
        self.assertIn("dl-add", ui, "diff 预览上色")
        self.assertIn('id="ltCtx"', ui, "上下文用量牌")
        self.assertIn("turn.context", ui)
        self.assertIn('id="ltReply"', ui, "Cursor 详细回复固定区，收起步骤也不藏")
        self.assertIn("function paintLtReply", ui)
        self.assertIn("展开步骤", ui)
        self.assertIn("Cursor 回复", ui)
        self.assertIn("placeLiveTurnInMsgs", ui, "时间线挂进消息气泡，对齐 Bajie MessageLog.ru")
        self.assertIn('id="ltPark"', ui)
        self.assertIn("执行过程", ui)
        self.assertIn("startPoll(liveTurnTick, 400", ui, "流失气泡忙时 400ms 一拍读库，对齐 Bajie 450ms 量级")
        self.assertIn("stream-live", ui, "生成中正文当流失气泡，带闪烁光标")
        self.assertIn("正在输出", ui)
        self.assertIn("不贴上一轮 zhi", ui, "生成中别把上一轮 zhi 当宿主")
        self.assertIn("本轮用户之后的第一条 AI", ui, "Bajie ru 顺序兜底：挂在这一轮的 AI 气泡上")
        self.assertIn("思考中", ui)
        self.assertIn("think-live", ui)
        self.assertIn("lt-think-body", ui)
        self.assertIn("pollLively();   // 闷头思考也钉在快档", ui)
        # Cursor 默认展开、Codex 默认折叠由 test_timeline_display 执行 JS 验证。
        self.assertIn("function ltMarkdown", ui, "思考卡/回复走前端 Markdown，不再 textContent 纯文本")
        self.assertIn("function findLiveTurnHost", ui, "Bajie MessageLog.ru：按 reply 挂到对应气泡")
        self.assertIn("lt-host-only", ui, "对不上才用独立宿主，别把真气泡 padding 清掉")
        self.assertIn("LT_STALE_SEC", ui)
        self.assertIn("久未响应", ui, "工具 running ≥30s 对齐 Bajie W_=30000")
        self.assertIn("ltFillRich", ui)
        self.assertNotIn('body.textContent = r.text', ui, "#ltReply 必须走 Markdown")
        self.assertNotIn('d.textContent = st.text || (thinkLive ? "…" : "")', ui, "思考正文必须走 Markdown")

    def test_get_state_exposes_live_step(self):
        api_src = (MODULE_DIR / "hub_api.py").read_text(encoding="utf-8")
        self.assertIn('"live_step": getattr(s, "live_step_cache", None)', api_src)

    def test_share_page_and_server_carry_the_timeline_too(self):
        share = (MODULE_DIR / "share.html").read_text(encoding="utf-8")
        self.assertIn('id="liveTurn"', share)
        self.assertIn('"/api/live_turn?sid="', share)
        self.assertIn("await liveTurnTick()", share)
        self.assertIn("s.live_step", share)
        self.assertIn('id="ltCtx"', share, "手机页同款上下文用量牌")
        self.assertIn('id="ltReply"', share, "手机页同样固定展示 Cursor 正文")
        self.assertIn("function paintLtReply", share)
        self.assertIn("展开步骤", share)
        srv = (MODULE_DIR / "share_server.py").read_text(encoding="utf-8")
        self.assertIn('path == "/api/live_turn"', srv)
        self.assertIn("api.get_live_turn(sid_q, max_steps=max_steps)", srv)


if __name__ == "__main__":
    unittest.main()
