# -*- coding: utf-8 -*-
import importlib.util
import inspect
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import session_locator
import hub


class SessionLocatorModuleTests(unittest.TestCase):
    def test_session_locator_module_is_available(self):
        self.assertIsNotNone(importlib.util.find_spec("session_locator"))

    def test_public_locator_api_is_available(self):
        self.assertTrue(callable(getattr(session_locator, "cursor_project_slug", None)))
        self.assertTrue(callable(getattr(session_locator, "find_cursor_transcript", None)))
        self.assertTrue(callable(getattr(session_locator, "build_locator_prompt", None)))

    def test_hub_exposes_session_locator_api(self):
        self.assertTrue(callable(getattr(hub.Api, "get_session_locator", None)))

    def test_hub_locator_can_skip_disk_scan_for_prompt_copy(self):
        parameters = inspect.signature(hub.Api.get_session_locator).parameters
        self.assertIn("locate_transcript", parameters)

        session = SimpleNamespace(
            cwd=r"D:\projects\video_editor",
            name="看我看我",
            conv_key="b7e42a9c",
        )
        with (
            patch.object(hub.HUB, "sessions", {"tab-id": session}),
            patch.object(
                hub,
                "find_cursor_transcript",
                side_effect=AssertionError("prompt copy must not scan transcripts"),
            ),
        ):
            result = hub.Api().get_session_locator("tab-id", False)

        self.assertTrue(result["ok"])
        self.assertIn(r"D:\projects\video_editor", result["prompt"])

    def test_hub_locator_returns_cursor_uuid_path_and_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "real-uuid" / "real-uuid.jsonl"
            transcript.parent.mkdir()
            transcript.write_text("{}", encoding="utf-8")
            session = SimpleNamespace(
                cwd=r"D:\projects\video_editor",
                name="看我看我",
                conv_key="b7e42a9c",
            )
            with (
                patch.object(hub.HUB, "sessions", {"tab-id": session}),
                patch.object(hub, "find_cursor_transcript", return_value=transcript),
            ):
                result = hub.Api().get_session_locator("tab-id")

        self.assertTrue(result["ok"])
        self.assertEqual("real-uuid", result["cursor_session_id"])
        self.assertEqual(str(transcript), result["cursor_transcript_path"])
        self.assertIn(r"D:\projects\video_editor", result["prompt"])
        self.assertEqual("b7e42a9c", result["conversation_id"])

    def test_cursor_project_slug_matches_cursor_folder_rules(self):
        self.assertEqual(
            "d-projects-video-editor",
            session_locator.cursor_project_slug(r"D:\projects\video_editor"),
        )
        self.assertEqual(
            "d-projects-mcp",
            session_locator.cursor_project_slug(r"D:\projects\mcp持久化"),
        )

    def test_find_cursor_transcript_requires_exact_conversation_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp)
            transcripts = projects / "d-projects-video-editor" / "agent-transcripts"
            wrong = transcripts / "wrong-id" / "wrong-id.jsonl"
            right = transcripts / "real-uuid" / "real-uuid.jsonl"
            subagent = transcripts / "parent-id" / "subagents" / "child.jsonl"
            for path in (wrong, right, subagent):
                path.parent.mkdir(parents=True, exist_ok=True)
            wrong.write_text(
                json.dumps({"conversation_id": "deadbeef"}),
                encoding="utf-8",
            )
            right.write_text(
                json.dumps({
                    "role": "assistant",
                    "message": {"content": [{
                        "type": "tool_use",
                        "input": {"conversation_id": "a7c3e91b"},
                    }]},
                }),
                encoding="utf-8",
            )
            subagent.write_text(
                json.dumps({"conversation_id": "a7c3e91b"}),
                encoding="utf-8",
            )

            result = session_locator.find_cursor_transcript(
                r"D:\projects\video_editor",
                "a7c3e91b",
                projects_root=projects,
            )

            self.assertEqual(right, result)

    def test_find_cursor_transcript_rejects_default_or_missing_id(self):
        self.assertIsNone(session_locator.find_cursor_transcript(
            r"D:\projects\video_editor",
            "__default__",
        ))
        self.assertIsNone(session_locator.find_cursor_transcript(
            r"D:\projects\video_editor",
            "",
        ))

    def test_find_cursor_transcript_falls_back_across_project_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp)
            transcript = (
                projects
                / "d-projects-mcp"
                / "agent-transcripts"
                / "cross-project-uuid"
                / "cross-project-uuid.jsonl"
            )
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                json.dumps({"conversation_id": "a7c3e91b"}),
                encoding="utf-8",
            )

            result = session_locator.find_cursor_transcript(
                r"D:\projects\video_editor",
                "a7c3e91b",
                projects_root=projects,
            )

            self.assertEqual(transcript, result)

    def test_prompt_contains_project_and_search_locations(self):
        prompt = session_locator.build_locator_prompt(
            r"D:\projects\video_editor",
            "看我看我",
            "b7e42a9c",
            user_home=Path(r"C:\Users\Administrator"),
            appdata=Path(r"C:\Users\Administrator\AppData\Roaming"),
        )

        self.assertIn(r"D:\projects\video_editor", prompt)
        self.assertIn("看我看我", prompt)
        self.assertIn("b7e42a9c", prompt)
        self.assertIn("agent-transcripts", prompt)
        self.assertIn("workspaceStorage", prompt)
        self.assertIn("不要仅按最新修改时间猜测", prompt)


class ConvMatcherTests(unittest.TestCase):
    def test_tool_arg_ignores_takeover_prompt_text(self):
        prompt = (
            "现在立刻用 conversation_id=「050f09d2」调一次 zt\n"
            "conversation_id 必须改用「050f09d2」"
        )
        json_arg = '{"conversation_id": "050f09d2", "task_name": "心理评测·进度梳理"}'
        param = session_locator._conv_matcher("050f09d2", True)
        tool = session_locator._conv_matcher("050f09d2", "tool_arg")
        self.assertTrue(param(prompt), "旧档仍要认接手提示词（身份校准用）")
        self.assertFalse(tool(prompt), "收壳反查不得把提示词正文当成已切换 ID")
        self.assertTrue(tool(json_arg))
        self.assertFalse(tool('{"conversation_id": "28172cf0"}'))


class JsonlToolArgTests(unittest.TestCase):
    def test_callmcptool_arguments_count_and_prompt_or_shell_do_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "u" / "u.jsonl"
            p.parent.mkdir()
            prompt = {
                "role": "user",
                "message": {"content": [{"type": "text", "text":
                    "conversation_id=「0d07cce5」必须改用「0d07cce5」"}]},
            }
            shell = {
                "role": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "Shell",
                    "input": {"command":
                        'print("conversation_id": "0d07cce5")'}}]},
            }
            call = {
                "role": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "CallMcpTool",
                    "input": {"server": "user-rxyy MCP", "toolName": "zt",
                              "arguments": {"conversation_id": "0d07cce5",
                                            "status": "接令"}}}]},
            }
            p.write_text(
                json.dumps(prompt, ensure_ascii=False) + "\n"
                + json.dumps(shell, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            self.assertFalse(session_locator.transcript_has_tool_arg(str(p), "0d07cce5"))
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(call, ensure_ascii=False) + "\n")
            session_locator._TOOL_ARG_CACHE.clear()
            self.assertTrue(session_locator.transcript_has_tool_arg(str(p), "0d07cce5"))
            self.assertFalse(session_locator.transcript_has_tool_arg(str(p), "e190ed54"))

    def test_jsonl_tool_arg_session_picks_recent_not_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp)
            d = projects / "d-proj" / "agent-transcripts"
            old = d / "old-uuid" / "old-uuid.jsonl"
            new = d / "new-uuid" / "new-uuid.jsonl"
            for path in (old, new):
                path.parent.mkdir(parents=True)
            payload = json.dumps({
                "role": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "CallMcpTool",
                    "input": {"arguments": {"conversation_id": "0d07cce5"}}}]},
            })
            old.write_text(payload + "\n", encoding="utf-8")
            new.write_text(payload + "\n", encoding="utf-8")
            import os, time
            past = time.time() - 4000
            os.utime(old, (past, past))
            hit = session_locator.jsonl_tool_arg_session_for_conv(
                r"d:\proj", "0d07cce5", projects_root=projects, within_secs=1800)
            self.assertIsNotNone(hit)
            self.assertEqual("new-uuid", hit[0])


class WriteCursorTitleTests(unittest.TestCase):
    UID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    def _db(self, tmp, name="Persistent plus zhi report"):
        appdata = Path(tmp)
        db = appdata / "Cursor" / "User" / "globalStorage" / "state.vscdb"
        db.parent.mkdir(parents=True)
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
        con.execute(
            "CREATE TABLE composerHeaders (composerId TEXT PRIMARY KEY, "
            "workspaceId TEXT, createdAt INTEGER, lastUpdatedAt INTEGER, "
            "isArchived INTEGER, isSubagent INTEGER, recency INTEGER, "
            "checkpointAt INTEGER, value TEXT)")
        blob = json.dumps({"name": name, "subtitle": "报到提示词"}, ensure_ascii=False)
        con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)",
                    ("composerData:" + self.UID, blob))
        con.execute(
            "INSERT INTO composerHeaders VALUES (?,?,?,?,?,?,?,?,?)",
            (self.UID, "ws", 1, 1, 0, 0, 1, 1, blob))
        con.commit()
        con.close()
        return appdata

    def test_fake_uuid_is_not_written(self):
        self.assertFalse(session_locator.write_cursor_title("cursorchat-uuid-1", "控制台·开链报错"))

    def test_task_name_overwrites_boilerplate_in_both_stores(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata = self._db(tmp)
            self.assertTrue(session_locator.write_cursor_title(
                self.UID, "控制台·开链报错", appdata=str(appdata)))
            db = Path(tmp) / "Cursor" / "User" / "globalStorage" / "state.vscdb"
            con = sqlite3.connect(str(db))
            kv = json.loads(con.execute(
                "SELECT value FROM cursorDiskKV WHERE key=?",
                ("composerData:" + self.UID,)).fetchone()[0])
            hdr = json.loads(con.execute(
                "SELECT value FROM composerHeaders WHERE composerId=?",
                (self.UID,)).fetchone()[0])
            con.close()
            self.assertEqual("控制台·开链报错", kv["name"])
            self.assertEqual("控制台·开链报错", hdr["name"])
            self.assertEqual("报到提示词", kv["subtitle"])


class ListComposerHeadersTests(unittest.TestCase):
    """绑定菜单的名字必须来自 composerHeaders，不许扫 cursorDiskKV。"""
    A = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    B = "bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee"
    C = "cccccccc-bbbb-cccc-dddd-eeeeeeeeeeee"

    def _db(self, tmp):
        appdata = Path(tmp)
        db = appdata / "Cursor" / "User" / "globalStorage" / "state.vscdb"
        db.parent.mkdir(parents=True)
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
        con.execute(
            "CREATE TABLE composerHeaders (composerId TEXT PRIMARY KEY, "
            "workspaceId TEXT, createdAt INTEGER, lastUpdatedAt INTEGER, "
            "isArchived INTEGER, isSubagent INTEGER, recency INTEGER, "
            "checkpointAt INTEGER, value TEXT)")
        rows = [
            (self.A, 30, 0, 0, {"name": "活着的", "subtitle": "正在干"}),
            (self.B, 20, 1, 0, {"name": "已归档"}),
            (self.C, 40, 0, 1, {"name": "子代理"}),
        ]
        for uid, ts, archived, sub, data in rows:
            blob = json.dumps(data, ensure_ascii=False)
            con.execute("INSERT INTO cursorDiskKV VALUES (?, ?)",
                        ("composerData:" + uid, blob))
            con.execute(
                "INSERT INTO composerHeaders VALUES (?,?,?,?,?,?,?,?,?)",
                (uid, "ws", 1, ts, archived, sub, 1, 1, blob))
        con.commit()
        con.close()
        return str(appdata)

    def test_skips_archived_and_subagent_and_returns_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            items = session_locator.list_composer_headers(appdata=self._db(tmp))
        self.assertEqual(1, len(items))
        self.assertEqual(self.A, items[0]["id"])
        self.assertEqual("活着的", items[0]["name"])
        self.assertEqual("正在干", items[0]["subtitle"])

    def test_impl_never_scans_cursordiskkv(self):
        src = Path(__file__).resolve().parents[1].joinpath("session_locator.py")
        text = src.read_text(encoding="utf-8")
        start = text.index("def list_composer_headers")
        end = text.index("def cursor_conversation_exists")
        chunk = text[start:end]
        self.assertIn("FROM composerHeaders", chunk)
        self.assertNotIn("FROM cursorDiskKV", chunk)


if __name__ == "__main__":
    unittest.main()
