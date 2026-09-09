# -*- coding: utf-8 -*-
"""uuid 绑定 / 收壳全流程沙箱：假 USERPROFILE + 假 composer 库，不碰真 Cursor。

覆盖现场几种用法：CallMcpTool / zhi·zt·ji、接手词正文、Shell 字符串、
过期前任 jsonl、composer 只有提示词、同窗切 ID、新窗真切到原 ID、派单壳。
"""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub
import session_locator

CWD = r"d:\sandbox-uuidbind"
CID = "0d07cce5"
SHELL_CID = "e190ed54"
OTHER_CID = "76a1cf4d"
REAL_UID = "aaaaaaaa-1111-4000-8000-aaaaaaaaaaaa"
SHELL_UID = "bbbbbbbb-2222-4000-8000-bbbbbbbbbbbb"
PROMPT_UID = "cccccccc-3333-4000-8000-cccccccccccc"
STALE_UID = "dddddddd-4444-4000-8000-dddddddddddd"


def _callmcp(cid, tool="zt"):
    return {
        "role": "assistant",
        "message": {"content": [{
            "type": "tool_use",
            "name": "CallMcpTool",
            "input": {
                "server": "user-rxyy MCP",
                "toolName": tool,
                "arguments": {"conversation_id": cid, "status": "ready"},
            },
        }]},
    }


def _prompt(cid):
    return {
        "role": "user",
        "message": {"content": [{"type": "text", "text": (
            "现在立刻用 conversation_id=「%s」调一次 zt\n"
            "conversation_id 必须改用「%s」" % (cid, cid)
        )}]},
    }


def _shell_echo(cid):
    return {
        "role": "assistant",
        "message": {"content": [{
            "type": "tool_use",
            "name": "Shell",
            "input": {"command": 'python -c "print({\\"conversation_id\\": \\"%s\\"})"' % cid},
        }]},
    }


def _getmcp():
    return {
        "role": "assistant",
        "message": {"content": [{
            "type": "tool_use",
            "name": "GetMcpTools",
            "input": {"server": "user-rxyy MCP"},
        }]},
    }


def _zhi_flat(cid):
    return {"name": "zhi", "conversation_id": cid, "message": "已就位"}


def _write_jsonl(projects, slug, uid, events, mtime=None):
    p = projects / slug / "agent-transcripts" / uid / (uid + ".jsonl")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events),
                 encoding="utf-8")
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


def _composer(cid_prompt=None, cid_json=None, ts=None, generating=0):
    now_ms = int((ts if ts is not None else time.time()) * 1000)
    blob = {
        "name": "sandbox",
        "lastUpdatedAt": now_ms,
        "conversationCheckpointLastUpdatedAt": now_ms,
        "generatingBubbleIds": ["b1"] * int(generating),
    }
    if cid_prompt:
        blob["subtitle"] = "conversation_id=「%s」必须改用「%s」" % (
            cid_prompt, cid_prompt)
    if cid_json:
        blob["tool"] = {"conversation_id": cid_json}
    return blob


def _init_db(appdata, composers):
    db = Path(appdata) / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    for uid, data in composers.items():
        con.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            ("composerData:" + uid, json.dumps(data, ensure_ascii=False)))
    con.commit()
    con.close()
    return db


def _sess(sid, conv, name, uuid, msg_seq, cwd=CWD, **extra):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = sid, conv, name
    s.cursor_uuid = uuid
    s.transcript_path = None
    s.msg_seq = msg_seq
    s.pending = extra.get("pending")
    s.queued = extra.get("queued") or []
    s.messages = extra.get("messages") or []
    s.connected = True
    s.client = None
    s.lock = threading.Lock()
    s.cwd = cwd
    s.uuid_verified = extra.get("uuid_verified", False)
    s.shell_born = extra.get("shell_born", False)
    s.claimed_task_ts = extra.get("claimed_task_ts", 0)
    s.agent_named = extra.get("agent_named", False)
    s.agent_status = extra.get("agent_status", "")
    s.agent_status_ts = extra.get("agent_status_ts", 0)
    s._reap_ts = 0
    return s


class SandboxJsonlMatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.projects = self.root / "projects"
        self.slug = session_locator.cursor_project_slug(CWD)
        session_locator._TOOL_ARG_CACHE.clear()

    def test_formats_that_count_and_noise_that_must_not(self):
        p = _write_jsonl(self.projects, self.slug, REAL_UID, [
            _prompt(CID), _shell_echo(CID), _getmcp(),
        ])
        self.assertFalse(session_locator.transcript_has_tool_arg(str(p), CID))

        for extra in (
            [_callmcp(CID, "zt")],
            [_callmcp(CID, "zhi")],
            [_callmcp(CID, "ji")],
            [_zhi_flat(CID)],
        ):
            session_locator._TOOL_ARG_CACHE.clear()
            q = _write_jsonl(self.projects, self.slug, REAL_UID,
                             [_prompt(CID), _shell_echo(CID)] + extra)
            self.assertTrue(session_locator.transcript_has_tool_arg(str(q), CID), extra)
            self.assertFalse(session_locator.transcript_has_tool_arg(str(q), SHELL_CID))

    def test_stale_jsonl_without_composer_is_skipped(self):
        past = time.time() - 2500
        _write_jsonl(self.projects, self.slug, REAL_UID, [_callmcp(CID)], mtime=past)
        hit = session_locator.jsonl_tool_arg_session_for_conv(
            CWD, CID, projects_root=self.projects, within_secs=1800)
        self.assertIsNone(hit, "没有 composer 活性时，过期 jsonl 仍排除")

    def test_recent_jsonl_beats_stale_predecessor(self):
        past = time.time() - 4000
        _write_jsonl(self.projects, self.slug, STALE_UID, [_callmcp(CID)], mtime=past)
        _write_jsonl(self.projects, self.slug, REAL_UID, [_callmcp(CID)])
        hit = session_locator.jsonl_tool_arg_session_for_conv(
            CWD, CID, projects_root=self.projects, within_secs=1800)
        self.assertIsNotNone(hit)
        self.assertEqual(REAL_UID, hit[0])

    def test_subagent_jsonl_is_ignored(self):
        d = self.projects / self.slug / "agent-transcripts" / REAL_UID
        d.mkdir(parents=True)
        sub = d / "child.jsonl"
        sub.write_text(json.dumps(_callmcp(CID)) + "\n", encoding="utf-8")
        hit = session_locator.jsonl_tool_arg_session_for_conv(
            CWD, CID, projects_root=self.projects)
        self.assertIsNone(hit)

    def test_prompt_matcher_vs_tool_arg_matcher(self):
        prompt = "现在立刻用 conversation_id=「%s」调一次 zt" % CID
        param = session_locator._conv_matcher(CID, True)
        tool = session_locator._conv_matcher(CID, "tool_arg")
        self.assertTrue(param(prompt))
        self.assertFalse(tool(prompt))
        self.assertTrue(tool(json.dumps({"conversation_id": CID})))


class SandboxComposerAndRelocateTests(unittest.TestCase):
    """假 APPDATA 的 sqlite + 假 jsonl，走 hub 真函数，不 mock 定位器。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.home = base / "home"
        self.appdata = base / "appdata"
        self.home.mkdir()
        self.appdata.mkdir()
        self.projects = self.home / ".cursor" / "projects"
        self.slug = session_locator.cursor_project_slug(CWD)
        session_locator._TOOL_ARG_CACHE.clear()
        self._env = patch.dict(os.environ, {
            "USERPROFILE": str(self.home),
            "HOME": str(self.home),
            "APPDATA": str(self.appdata),
        })
        self._env.start()
        self.addCleanup(self._env.stop)
        self._home = patch.object(Path, "home", return_value=self.home)
        self._home.start()
        self.addCleanup(self._home.stop)

    def _hub(self, sessions):
        d = {x.id: x for x in sessions}
        patches = [
            patch.object(hub.HUB, "sessions", d),
            patch.object(hub.HUB, "order", list(d)),
            patch.object(hub.HUB, "log_end", lambda s, why: None),
            patch.object(hub, "log_event", lambda *a, **k: None),
            # 接手账本全隔离：本类的收壳走真 _retire_conv_into/_tombstone_shell，
            # 不打桩的话「测试 conv → 测试 succ」会灌进真 HUB 的台账并落盘，
            # 还会污染同进程后跑的用例（_shell_takeover_target 的台账兜底）
            patch.object(hub.HUB, "takeover_aliases", {}),
            patch.object(hub.HUB, "takeover_ledger", {}, create=True),
            patch.object(hub.HUB, "name_tombstones", {}),
            patch.object(hub.Hub, "_save_takeover_aliases", lambda self: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return d

    def test_relocate_follows_jsonl_not_prompt_only_composer(self):
        _write_jsonl(self.projects, self.slug, REAL_UID, [_callmcp(CID)])
        _write_jsonl(self.projects, self.slug, PROMPT_UID, [_prompt(CID)])
        now = time.time()
        _init_db(self.appdata, {
            REAL_UID: _composer(ts=now),
            PROMPT_UID: _composer(cid_prompt=CID, ts=now + 10),
        })
        revived = _sess("b", CID, "rxyy tools·MCP长连", STALE_UID, 45,
                        uuid_verified=True)
        shell = _sess("a", SHELL_CID, "待命·cursor工作流", PROMPT_UID, 2,
                      uuid_verified=True, shell_born=True)
        self._hub([revived, shell])
        hub.HUB._relocate_takeover_uuid(revived)
        self.assertEqual(REAL_UID, revived.cursor_uuid)
        self.assertTrue(revived.uuid_verified)
        self.assertNotEqual(PROMPT_UID, revived.cursor_uuid)

    def test_relocate_uses_hot_composer_when_jsonl_mtime_is_stale(self):
        past = time.time() - 2500
        _write_jsonl(self.projects, self.slug, REAL_UID, [_callmcp(CID)], mtime=past)
        _init_db(self.appdata, {REAL_UID: _composer(ts=time.time())})
        revived = _sess("b", CID, "rxyy tools·MCP长连", STALE_UID, 45)
        self._hub([revived])
        hub.HUB._relocate_takeover_uuid(revived)
        self.assertEqual(REAL_UID, revived.cursor_uuid)

    def test_relocate_ignores_prompt_only_when_jsonl_empty(self):
        _write_jsonl(self.projects, self.slug, PROMPT_UID, [_prompt(CID)])
        _init_db(self.appdata, {
            PROMPT_UID: _composer(cid_prompt=CID, ts=time.time()),
        })
        revived = _sess("b", CID, "rxyy tools·MCP长连", STALE_UID, 45)
        self._hub([revived])
        hub.HUB._relocate_takeover_uuid(revived)
        self.assertEqual(STALE_UID, revived.cursor_uuid)

    def test_relocate_composer_tool_arg_fallback_when_no_jsonl_call(self):
        _write_jsonl(self.projects, self.slug, REAL_UID, [_prompt(CID)])
        _init_db(self.appdata, {
            REAL_UID: _composer(cid_json=CID, ts=time.time()),
        })
        revived = _sess("b", CID, "rxyy tools·MCP长连", STALE_UID, 45)
        self._hub([revived])
        hub.HUB._relocate_takeover_uuid(revived)
        self.assertEqual(REAL_UID, revived.cursor_uuid)

    def test_reap_new_window_when_shell_jsonl_has_callmcp(self):
        now = time.time()
        _write_jsonl(self.projects, self.slug, REAL_UID, [_callmcp(CID)],
                     mtime=now)
        _write_jsonl(self.projects, self.slug, SHELL_UID,
                     [_prompt(CID), _callmcp(CID)], mtime=now - 20)
        _init_db(self.appdata, {
            REAL_UID: _composer(ts=now),
            SHELL_UID: _composer(ts=now - 20),
        })
        revived = _sess("b", CID, "rxyy tools·MCP长连", REAL_UID, 45,
                        uuid_verified=True)
        # claimed_task_ts 必须为 0：08-26 起认领了活的会话不再是壳（73004179
        # 事故），这条测试盯的是 jsonl 同窗判定，壳就得是纯壳
        shell = _sess("a", SHELL_CID, "待命·cursor工作流", SHELL_UID, 3,
                      shell_born=True, agent_named=False)
        d = self._hub([revived, shell])
        hub.HUB._reap_takeover_shell(revived)
        self.assertNotIn("a", d)
        self.assertIn("b", d)

    def test_reap_skips_pasted_prompt_without_tool_call(self):
        now = time.time()
        _write_jsonl(self.projects, self.slug, REAL_UID, [_callmcp(CID)],
                     mtime=now)
        _write_jsonl(self.projects, self.slug, SHELL_UID, [_prompt(CID)],
                     mtime=now - 5)
        _init_db(self.appdata, {
            REAL_UID: _composer(ts=now),
            SHELL_UID: _composer(cid_prompt=CID, ts=now - 5),
        })
        revived = _sess("b", CID, "rxyy tools·MCP长连", REAL_UID, 45,
                        uuid_verified=True)
        shell = _sess("a", SHELL_CID, "待命·cursor工作流", SHELL_UID, 3,
                      shell_born=True, agent_named=False)
        d = self._hub([revived, shell])
        hub.HUB._reap_takeover_shell(revived)
        self.assertIn("a", d)

    def test_reap_dispatched_prompt_head_even_without_jsonl(self):
        text = (hub.TAKEOVER_PROMPT_HEAD
                + "\n现在立刻用 conversation_id=「%s」调一次 zt" % CID)
        revived = _sess("b", CID, "rxyy tools·MCP长连", REAL_UID, 45)
        shell = _sess("a", SHELL_CID, "待命·cursor工作流", SHELL_UID, 2,
                      shell_born=True,
                      messages=[{"role": "user", "html": "<pre>%s</pre>" % text}])
        _write_jsonl(self.projects, self.slug, REAL_UID, [_callmcp(CID)])
        _write_jsonl(self.projects, self.slug, SHELL_UID, [_prompt(OTHER_CID)])
        _init_db(self.appdata, {
            REAL_UID: _composer(ts=time.time()),
            SHELL_UID: _composer(ts=time.time() - 10),
        })
        d = self._hub([revived, shell])
        hub.HUB._reap_takeover_shell(revived)
        self.assertNotIn("a", d)

    def test_same_window_uuid_still_reaps(self):
        revived = _sess("b", CID, "rxyy tools·MCP长连", REAL_UID, 45)
        shell = _sess("a", SHELL_CID, "待命·cursor工作流", REAL_UID, 2)
        _write_jsonl(self.projects, self.slug, REAL_UID, [_callmcp(CID)])
        _init_db(self.appdata, {REAL_UID: _composer(ts=time.time())})
        d = self._hub([revived, shell])
        hub.HUB._reap_takeover_shell(revived)
        self.assertNotIn("a", d)

    def test_other_conversation_shell_stays(self):
        revived = _sess("b", CID, "rxyy tools·MCP长连", REAL_UID, 45)
        other = _sess("c", OTHER_CID, "录播·紧急插播", PROMPT_UID, 20)
        _write_jsonl(self.projects, self.slug, REAL_UID, [_callmcp(CID)])
        _init_db(self.appdata, {REAL_UID: _composer(ts=time.time())})
        d = self._hub([revived, other])
        hub.HUB._reap_takeover_shell(revived)
        self.assertIn("c", d)


class BriefAndPromptTests(unittest.TestCase):
    def test_brief_and_new_chat_forbid_getmcptools(self):
        import server
        self.assertIn("禁止 GetMcpTools", server.MCP_INSTRUCTIONS_BRIEF)
        self.assertIn("CallMcpTool", server.MCP_INSTRUCTIONS_BRIEF)
        self.assertIn("rename_chat", server.MCP_INSTRUCTIONS_BRIEF)
        self.assertIn("GetMcpTools", hub.DEFAULTS["new_chat_prompt"])


if __name__ == "__main__":
    unittest.main()
