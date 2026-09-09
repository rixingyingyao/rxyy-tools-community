# -*- coding: utf-8 -*-
"""叫醒 Cursor 客户端：改对字段才算数。

触碰 mcp.json 是卡死自愈 / 紧急自愈 / 控制台「重连MCP」/ 守护进程上线自报共用的
唯一叫醒手段。而此前它改的是 env._reload——env 是 stdio 传输才用的字段，HTTP 型
条目（本机就是 http://127.0.0.1:39222/mcp）改它 Cursor 根本不看。

08-07 有日志为证：12:36:08 与 12:39:54 两次只改 env，Cursor 侧一行 createClient
都没有，全队 agent 在 Not connected 里干挂十分钟，最后是用户手点设置里的「重启
MCP」救回来的；12:47:11 改 headers 的那一秒，两个窗口同时 createClient success=true。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import mcp_touch  # noqa: E402


class TouchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "mcp.json"
        p = patch.object(mcp_touch, "mcp_json_path", lambda: self.path)
        p.start()
        self.addCleanup(p.stop)

    def _write(self, cfg):
        self.path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    def _read(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def test_an_http_entry_gets_a_header_nonce(self):
        # 这条就是本机的装法，也是十分钟全队失联的那次
        self._write({"mcpServers": {"rxyy MCP": {
            "url": "http://127.0.0.1:39222/mcp"}}})
        ok, why = mcp_touch.touch_mcp_json()
        self.assertTrue(ok, why)
        self.assertIn("X-Rxyy-Mcp-Reload",
                      self._read()["mcpServers"]["rxyy MCP"]["headers"])

    def test_the_nonce_actually_changes(self):
        # 值不变就等于没改，Cursor 一样不会重连
        self._write({"mcpServers": {"rxyy MCP": {
            "url": "http://127.0.0.1:39222/mcp",
            "headers": {"X-Chijiu-Reload": "1"}}}})
        with patch.object(mcp_touch.time, "time", lambda: 1786078031.0):
            mcp_touch.touch_mcp_json()
        self.assertEqual("1786078031",
                         self._read()["mcpServers"]["rxyy MCP"]["headers"]["X-Rxyy-Mcp-Reload"])
        self.assertNotIn("X-Chijiu-Reload",
                         self._read()["mcpServers"]["rxyy MCP"]["headers"])

    def test_other_headers_survive(self):
        self._write({"mcpServers": {"rxyy MCP": {
            "url": "http://127.0.0.1:39222/mcp",
            "headers": {"Authorization": "Bearer 别动我"}}}})
        mcp_touch.touch_mcp_json()
        h = self._read()["mcpServers"]["rxyy MCP"]["headers"]
        self.assertEqual("Bearer 别动我", h["Authorization"])

    def test_a_stdio_entry_is_not_given_headers(self):
        # 装成 stdio 的机器（command+args）没有 headers 一说，凭空塞一个只会招误会；
        # 它本来就靠 env 生效
        self._write({"mcpServers": {"rxyy MCP": {
            "command": "python", "args": ["server.py"]}}})
        ok, _ = mcp_touch.touch_mcp_json()
        self.assertTrue(ok)
        entry = self._read()["mcpServers"]["rxyy MCP"]
        self.assertNotIn("headers", entry)
        self.assertIn("_reload", entry["env"])

    def test_the_env_nonce_is_still_bumped_for_http_too(self):
        # 老装法/老版本 Cursor 可能仍认它，留着不吃亏
        self._write({"mcpServers": {"rxyy MCP": {
            "url": "http://127.0.0.1:39222/mcp"}}})
        mcp_touch.touch_mcp_json()
        self.assertIn("_reload",
                      self._read()["mcpServers"]["rxyy MCP"]["env"])

    def test_nobody_elses_server_entry_is_touched(self):
        self._write({"mcpServers": {
            "rxyy MCP": {"url": "http://127.0.0.1:39222/mcp"},
            "figma": {"url": "https://figma.example/mcp",
                      "headers": {"X-Api-Key": "k"}}}})
        mcp_touch.touch_mcp_json()
        self.assertEqual({"X-Api-Key": "k"},
                         self._read()["mcpServers"]["figma"]["headers"])

    def test_a_missing_file_says_so_instead_of_raising(self):
        ok, why = mcp_touch.touch_mcp_json(retries=1)
        self.assertFalse(ok)
        self.assertIn("找不到", why)

    def test_a_config_without_our_entry_says_so(self):
        self._write({"mcpServers": {"figma": {"url": "https://x/mcp"}}})
        ok, why = mcp_touch.touch_mcp_json(retries=1)
        self.assertFalse(ok)
        self.assertIn("没有 rxyy MCP 条目", why)

    def test_broken_json_is_retried_then_reported(self):
        # Cursor 自己也在写这个文件，撞上半截内容是常事
        self.path.write_text("{半截", encoding="utf-8")
        ok, why = mcp_touch.touch_mcp_json(retries=2, delay=0)
        self.assertFalse(ok)
        self.assertIn("解析失败", why)

    def test_a_bom_does_not_look_like_broken_json(self):
        # 人手用 PowerShell 改过 mcp.json 就会带 BOM（PS 5.1 的 -Encoding utf8 写 BOM），
        # 按 utf-8 读会当场 ValueError，叫醒彻底失灵却报「可能正被 Cursor 改写」
        self.path.write_text(json.dumps({"mcpServers": {"rxyy MCP": {
            "url": "http://127.0.0.1:39222/mcp"}}}, ensure_ascii=False),
            encoding="utf-8-sig")
        self.assertTrue(self.path.read_bytes().startswith(b"\xef\xbb\xbf"))
        ok, why = mcp_touch.touch_mcp_json(retries=1)
        self.assertTrue(ok, why)
        self.assertIn("X-Rxyy-Mcp-Reload",
                      self._read()["mcpServers"]["rxyy MCP"]["headers"])
        # 写回的是不带 BOM 的 utf-8，顺手把它去掉
        self.assertFalse(self.path.read_bytes().startswith(b"\xef\xbb\xbf"))


class EnsureWorkspaceMcpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "cursor工作流"
        self.root.mkdir()

    def test_writes_project_url_and_keeps_other_servers(self):
        cursor = self.root / ".cursor"
        cursor.mkdir()
        (cursor / "mcp.json").write_text(json.dumps({
            "mcpServers": {
                "figma": {"url": "https://mcp.figma.com/mcp"},
            }
        }, ensure_ascii=False), encoding="utf-8")
        ok, why = mcp_touch.ensure_workspace_mcp(self.root, port=39222)
        self.assertTrue(ok, why)
        self.assertEqual("wrote", why)
        cfg = json.loads((cursor / "mcp.json").read_text(encoding="utf-8"))
        self.assertEqual("https://mcp.figma.com/mcp",
                         cfg["mcpServers"]["figma"]["url"])
        url = cfg["mcpServers"]["rxyy MCP"]["url"]
        self.assertTrue(url.startswith("http://127.0.0.1:39222/mcp/"))
        self.assertEqual(url, mcp_touch.project_mcp_url(self.root, 39222))

    def test_second_call_does_not_rewrite(self):
        mcp_touch.ensure_workspace_mcp(self.root, port=39222)
        mcp_path = self.root / ".cursor" / "mcp.json"
        before = mcp_path.read_bytes()
        ok, why = mcp_touch.ensure_workspace_mcp(self.root, port=39222)
        self.assertTrue(ok)
        self.assertEqual("unchanged", why)
        self.assertEqual(before, mcp_path.read_bytes())

    def test_missing_dir_is_reported(self):
        ok, why = mcp_touch.ensure_workspace_mcp(self.root / "nope")
        self.assertFalse(ok)
        self.assertIn("不存在", why)

    def test_slug_is_url_safe(self):
        slug = mcp_touch.project_mcp_slug(self.root)
        self.assertNotIn(" ", slug)
        self.assertNotIn("\\", slug)
        self.assertTrue(slug)

    def test_inspect_missing_ready_mismatch(self):
        ok, st = mcp_touch.inspect_workspace_mcp(self.root, port=39222)
        self.assertTrue(ok)
        self.assertEqual("missing", st)
        mcp_touch.ensure_workspace_mcp(self.root, port=39222)
        ok, st = mcp_touch.inspect_workspace_mcp(self.root, port=39222)
        self.assertTrue(ok)
        self.assertEqual("ready", st)
        mcp_path = self.root / ".cursor" / "mcp.json"
        mcp_path.write_text(json.dumps({
            "mcpServers": {"rxyy MCP": {"url": "http://127.0.0.1:39222/mcp"}}
        }, ensure_ascii=False), encoding="utf-8")
        ok, st = mcp_touch.inspect_workspace_mcp(self.root, port=39222)
        self.assertTrue(ok)
        self.assertEqual("mismatch", st)


class NewChatPromptMcpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "playthread-go"
        self.root.mkdir()
        import hub
        self.hub = hub
        self._burst = patch.object(hub.HUB, "start_yield_burst")
        self._yield = patch.object(hub.HUB, "yield_zhi_all_local")
        self._burst.start()
        self._yield.start()
        self.addCleanup(self._burst.stop)
        self.addCleanup(self._yield.stop)

    def test_plain_copy_does_not_write(self):
        r = self.hub.Api().new_chat_prompt(str(self.root), False)
        self.assertTrue(r["ok"])
        self.assertFalse(r.get("mcp"))
        self.assertFalse((self.root / ".cursor" / "mcp.json").exists())

    def test_codex_manual_copy_does_not_interrupt_waiters_or_write_cursor_config(self):
        r = self.hub.Api().new_chat_prompt(str(self.root), False, "codex")
        self.assertTrue(r["ok"])
        self.assertEqual("codex", r["runtime"])
        self.assertIn("runtime=codex", r["prompt"])
        self.assertIn(r["conversation_id"], r["prompt"])
        self.assertIn(str(self.root), r["prompt"])
        self.assertIn("thread_id", r["prompt"])
        self.assertNotIn("CallMcpTool", r["prompt"])
        self.assertNotIn("Cursor", r["prompt"])
        self.hub.HUB.yield_zhi_all_local.assert_not_called()
        self.hub.HUB.start_yield_burst.assert_not_called()
        self.assertFalse((self.root / ".cursor").exists())

    def test_write_mcp_writes_once_then_unchanged(self):
        api = self.hub.Api()
        r = api.new_chat_prompt(str(self.root), True)
        self.assertTrue(r["ok"], r)
        self.assertEqual("wrote", r["mcp"])
        self.assertIn("/mcp/", r["mcp_url"])
        r2 = api.new_chat_prompt(str(self.root), True)
        self.assertEqual("unchanged", r2["mcp"])

    def test_write_mcp_requires_dir(self):
        r = self.hub.Api().new_chat_prompt("", True)
        self.assertFalse(r.get("ok"))
        self.assertIn("工作目录", r.get("error") or "")

    def test_list_known_roots_dedupes_and_reports_status(self):
        mcp_touch.ensure_workspace_mcp(self.root, port=39222)
        other = Path(self.tmp.name) / "new-proj"
        other.mkdir()
        s1 = self.hub.Session.__new__(self.hub.Session)
        s1.cwd = s1.task_root = str(self.root)
        s1.archived = False
        s2 = self.hub.Session.__new__(self.hub.Session)
        s2.cwd = s2.task_root = str(self.root)
        s2.archived = False
        s3 = self.hub.Session.__new__(self.hub.Session)
        s3.cwd = str(other)
        s3.task_root = str(other)
        s3.archived = False
        with patch.object(self.hub.HUB, "sessions", {"a": s1, "b": s2, "c": s3}), \
             patch.object(self.hub.HUB, "cfg", {"mcp_http_port": 39222, "team_project_members": {}}):
            r = self.hub.Api().list_known_project_roots()
        self.assertTrue(r["ok"])
        by_name = {x["name"]: x for x in r["items"]}
        self.assertEqual("ready", by_name["playthread-go"]["mcp"])
        self.assertEqual("missing", by_name["new-proj"]["mcp"])


class MaybeEnsureProjectMcpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "fresh-proj"
        self.root.mkdir()
        import hub
        self.hub = hub

    def test_under_test_does_not_write(self):
        self.assertTrue(self.hub.running_under_test())
        ok, why = self.hub.maybe_ensure_project_mcp(str(self.root))
        self.assertFalse(ok)
        self.assertEqual("under_test", why)
        self.assertFalse((self.root / ".cursor" / "mcp.json").exists())

    def test_live_path_writes_without_touching_global(self):
        with patch.object(self.hub, "running_under_test", lambda: False), \
             patch.object(self.hub, "touch_mcp_json") as touch:
            ok, why = self.hub.maybe_ensure_project_mcp(str(self.root))
        self.assertTrue(ok, why)
        self.assertEqual("wrote", why)
        touch.assert_not_called()
        cfg = json.loads((self.root / ".cursor" / "mcp.json").read_text(encoding="utf-8"))
        self.assertTrue(cfg["mcpServers"]["rxyy MCP"]["url"].startswith(
            "http://127.0.0.1:39222/mcp/"))

    def test_install_dir_is_skipped(self):
        with patch.object(self.hub, "running_under_test", lambda: False):
            ok, why = self.hub.maybe_ensure_project_mcp(str(self.hub.APP_DIR))
        self.assertFalse(ok)
        self.assertEqual("install_dir", why)

    def test_zhi_request_triggers_maybe_ensure(self):
        called = []

        def _fake(root):
            called.append(root)
            return True, "wrote"

        client = type("C", (), {
            "closed_convs": {}, "sessions": {}, "cwd": str(self.root),
            "pid": 1, "last_heartbeat": 0, "peer_ip": None,
        })()
        s = self.hub.Session.__new__(self.hub.Session)
        s.id, s.conv_key, s.name = "sid", "c1", "待命"
        s.cwd = s.task_root = str(self.root)
        s.connected = True
        s.pending = None
        s.processing_since = None
        s.detached = False
        s.buffered_reply = None
        s.last_heartbeat = 0
        s.last_zhi_ts = 0
        with patch.object(self.hub, "maybe_ensure_project_mcp", _fake), \
             patch.object(self.hub.HUB, "resolve_session", lambda *a, **k: s), \
             patch.object(self.hub.HUB, "add_message",
                          side_effect=RuntimeError("stop-after-ensure")):
            try:
                self.hub.HUB._handle_client_msg(client, None, {
                    "type": "zhi_request", "id": "rpc-1",
                    "conversation_id": "c1",
                    "message": "已就位",
                    "predefined_options": ["开始任务", "结束"],
                })
            except RuntimeError as e:
                self.assertIn("stop-after-ensure", str(e))
        self.assertEqual([str(self.root)], called)

    def test_resume_zhi_does_not_rewrite(self):
        called = []
        client = type("C", (), {
            "closed_convs": {}, "sessions": {}, "cwd": str(self.root),
            "pid": 1, "last_heartbeat": 0, "peer_ip": None,
            "send": lambda self, obj: None,
        })()
        s = self.hub.Session.__new__(self.hub.Session)
        s.id, s.conv_key, s.name = "sid", "c1", "待命"
        s.cwd = s.task_root = str(self.root)
        s.connected = True
        s.pending = {"id": "old"}
        s.processing_since = None
        s.detached = False
        s.buffered_reply = None
        s.last_heartbeat = 0
        s.last_zhi_ts = 0
        s.queued = []
        s.messages = []
        s.lock = __import__("threading").Lock()
        with patch.object(self.hub, "maybe_ensure_project_mcp",
                          lambda root: called.append(root) or (True, "unchanged")), \
             patch.object(self.hub.HUB, "resolve_session", lambda *a, **k: s):
            self.hub.HUB._handle_client_msg(client, None, {
                "type": "zhi_request", "id": "rpc-2",
                "conversation_id": "c1", "resume": True,
            })
        self.assertEqual([], called)


class CheckinPromptText(unittest.TestCase):
    def test_default_prompt_forbids_v19_before_start(self):
        import hub
        p = hub.DEFAULTS["new_chat_prompt"]
        self.assertIn("不读文件", p)
        self.assertIn("v19", p)
        self.assertIn("开始任务", p)
        self.assertIn("GetMcpTools", p)
        # CallMcpTool 08-31 起不再占模板本体的字节（1024B 硬顶逼的），由改名行
        # 携带——组装后的报到词照样点名调用机制
        with patch.object(hub, "registered_mcp_name",
                          lambda port=None: "user-rxyy MCP"):
            assembled = hub.fill_checkin_prompt(p, "abcd1234", "")
        self.assertIn("CallMcpTool", assembled)


class CheckinPromptCarriesTheWorkspace(unittest.TestCase):
    """报到提示词必须自带工作区路径和名字，不能让 agent 自己去问 shell。

    08-26 用户实测：「为什么现在第一次报道（发接入提示词）要思考这么久？」
    截图里那一段是这么走的——提示词发下去，里面写着 task_name=「待命·<工作区
    目录名>」、project_path=工作区完整路径、message=「📍 <项目名>（<完整路径>）」，
    全是占位符。而同一句话又写着「不思考、不读文件、不做他事」「点开始任务前
    禁止 Read/Grep」，于是 agent 唯一能用的工具只剩 shell：

        pwd && basename "$(pwd)"        → PowerShell 不认 &&，InvalidEndOfLine
        (Get-Location).Path             → 中文路径吐成乱码
        再来一次，这次带上 UTF8 强制     → 终于拿到

    三趟 shell、每趟几十秒，全花在打听「我在哪个目录」上。而控制台**生成这句
    提示词的时候本来就知道 cwd**——它是 new_chat_prompt(cwd=...) 的入参，
    此前只拿去写 mcp.json，从没填进提示词里。「+ → 直接拉起本机 agent」更彻底：
    agent 就是被拉起在那个 cwd 里的，还是得自己去问一遍。
    """

    def setUp(self):
        import hub
        self.hub = hub
        self._burst = patch.object(hub.HUB, "start_yield_burst")
        self._yield = patch.object(hub.HUB, "yield_zhi_all_local")
        self._burst.start()
        self._yield.start()
        self.addCleanup(self._burst.stop)
        self.addCleanup(self._yield.stop)
        # 真名探测读的是真实 %USERPROFILE%/.cursor/mcp.json，换台机器变体就漂：
        # 探不到真名 → 全量逃生口更长 → 字节闸先砍改名行 → 本类 rename 断言全红。
        # 钉死成正常态「已探到真名」，别让测试结果取决于跑在谁的机器上。
        self._real = patch.object(hub, "registered_mcp_name",
                                  lambda port=None: "user-rxyy MCP")
        self._real.start()
        self.addCleanup(self._real.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "cursor工作流"
        self.root.mkdir()

    def _prompt(self, cwd=None):
        r = self.hub.Api().new_chat_prompt(
            str(self.root) if cwd is None else cwd, False)
        self.assertTrue(r["ok"], r)
        return r["prompt"], r["conversation_id"]

    def test_the_agent_never_has_to_go_ask_the_shell_where_it_is(self):
        prompt, cid = self._prompt()
        self.assertIn(str(self.root), prompt)      # 完整路径
        self.assertIn("待命·cursor工作流", prompt)  # 目录名
        self.assertIn(cid, prompt)
        self.assertNotIn("<", prompt, "占位符一个都不许留下，留一个就得跑一趟 shell")

    def test_the_check_in_line_names_the_project_and_the_path(self):
        # 报到那句话本身就是用户在控制台里看到的第一行字，别让它印着尖括号
        prompt, cid = self._prompt()
        self.assertIn("📍 cursor工作流（{}）· 对话 {} 已就位".format(self.root, cid),
                      prompt)

    def test_plain_copy_without_a_directory_still_reads_sensibly(self):
        # 「+ → 复制」没选目录时填不出来，那就退回原来的说明式占位符——
        # 宁可让 agent 多跑一趟 shell，也不能给它一句 project_path=「」
        prompt, _ = self._prompt(cwd="")
        self.assertIn("工作区", prompt)
        self.assertNotIn("（）", prompt)
        self.assertNotIn("「」", prompt)

    def test_plain_copy_fallback_says_use_context_not_shell(self):
        """「+ → 复制」不选目录恰恰是最常用的一条路（08-26 两份现场报到词
        9d27b55c/a0fe767c 都长这样）。占位符可以留，但「怎么填」必须写死：
        工作区路径 Cursor 开场就写在上下文里，照抄即可；绝不许跑终端查——
        中文路径在 PowerShell 里吐乱码，一趟几十秒还得再来两趟，
        这就是「第一次报到思考这么久」的全部成分。"""
        prompt, _ = self._prompt(cwd="")
        self.assertIn("上下文", prompt, "得告诉 agent 值就在它上下文里")
        self.assertIn("禁止", prompt)
        self.assertIn("终端", prompt, "不点名终端，它下次还去 pwd")

    def test_the_path_ban_covers_every_discovery_tool_not_just_the_terminal(self):
        """08-27 13:35 壳 22a030d6（用户截图）：禁令只点名「终端」、理由只写
        「乱码」，agent 就有缝可钻——先列目录核实路径（listing 超时白等 21 秒），
        再 printf|base64 专治乱码接着查，前后两分多钟才报到。要禁的是「查路径」
        这个动作本身：列目录、搜索都得点名，别留绕的缝。"""
        prompt, _ = self._prompt(cwd="")
        self.assertIn("列目录", prompt)
        self.assertIn("搜索", prompt)
        self.assertIn("拿不准", prompt, "没有合法出路，点名再全它也会另辟蹊径")

    def test_plain_copy_fallback_gives_no_context_agents_a_legal_exit(self):
        """08-27 实测（壳 f9c313ed）：hh-wait 子代理的上下文里根本没有
        workspace 路径，旧兜底只说「上下文里有，照抄」，对它是死路——
        它只好连跑两趟慢终端（pwd 空输出 + echo 才拿到，白耗四十多秒才报到）。
        兜底必须给这类壳一条合法出路：占位符填「workspace」、project_path
        不传，控制台经 MCP 归属探测认领（11:44 壳 8653eee8 实证这条路通）。"""
        prompt, _ = self._prompt(cwd="")
        self.assertIn("填「workspace」", prompt)
        self.assertIn("project_path 不传", prompt)
        self.assertIn("探测", prompt)

    def test_an_old_hand_written_template_gets_filled_too(self):
        # 用户在设置面板里改过模板（config.json 覆盖 DEFAULTS）时，
        # 老写法里的尖括号占位符照样要被填上，否则这个修复对他等于没有
        old = ("conversation_id=「{conversation_id}」；task_name=「待命·<工作区目录名>」；"
               "project_path=工作区完整路径；message=「📍 <项目名>（<完整路径>）· 已就位」")
        with patch.dict(self.hub.HUB.cfg, {"new_chat_prompt": old}, clear=False):
            prompt, _ = self._prompt()
        self.assertIn(str(self.root), prompt)
        self.assertIn("待命·cursor工作流", prompt)
        self.assertNotIn("<", prompt)
        self.assertNotIn("project_path=工作区完整路径", prompt)

    def test_checkin_names_the_cursor_tab_before_cursor_can_auto_name_it(self):
        """08-28 用户截图：编辑器顶栏四个 tab 全叫「Persistent plus task re...」，
        追问「这个真名，不能一开始就给他设置对吗」。能——Cursor 的
        shouldRenameComposer 最后一档是 `!n.name`，**只给还没有名字的对话自动起名**。
        报到第一轮就落下名字，那句 Persistent plus zhi report 根本不会产生。"""
        prompt, cid = self._prompt()
        self.assertIn("rename_chat", prompt)
        self.assertIn("cursor-app-control", prompt)
        self.assertIn('"title":"待命·cursor工作流·%s"' % cid[:4], prompt)
        self.assertIn("子代理", prompt)  # 只有主对话有这个工具，转手前得自己调

    def test_checkin_tab_name_is_unique_even_without_a_workspace(self):
        # 四个壳同时开、cwd 又都是空的时候，光靠「待命·workspace」还是一排同名
        prompt, cid = self._prompt(cwd="")
        self.assertIn("·%s\"" % cid[:4], prompt)

    def test_rename_title_uses_the_same_placeholder_as_the_rest(self):
        """控制台不知道目录时，改名那句里的工作区也必须是占位符。

        写死「workspace」会让两边分家：报到词其余部分发的是占位符，手上有真
        路径的主对话把 task_name 填成「待命·cursor工作流」，改名那句却拿字面
        「待命·workspace·d704」去改 Cursor 侧栏——08-28 用户截图里控制台是真名、
        侧栏还是 workspace，就是这么来的。占位符一致，一次替换三处同答案。
        """
        prompt, cid = self._prompt(cwd="")
        self.assertIn('"title":"待命·<工作区目录名>·%s"' % cid[:4], prompt)
        self.assertNotIn("待命·workspace", prompt,
                         "控制台不该替 agent 决定它在哪个工作区")
        # 上下文里真没有路径的壳（子代理）仍有合法出路：占位符一律填 workspace
        self.assertIn("填「workspace」", prompt)

    def test_rename_line_survives_a_hand_edited_template(self):
        old = "conversation_id=「{conversation_id}」；随便写点什么"
        with patch.dict(self.hub.HUB.cfg, {"new_chat_prompt": old}, clear=False):
            prompt, cid = self._prompt()
        self.assertIn('"title":"待命·cursor工作流·%s"' % cid[:4], prompt)

    def test_spawning_a_local_agent_fills_it_in_as_well(self):
        # 这条路最不该漏：runner 就是在 cwd 里把 agent 拉起来的，它还得自己问一遍
        import sdk_runner as reg
        captured = {}
        prompt_dir = Path(self.tmp.name) / "prompts"
        with (patch.object(reg, "PROMPT_DIR", prompt_dir),
              patch.object(reg, "upsert_entry", lambda *a, **k: None),
              patch.object(self.hub.Api, "_ensure_one_project_mcp",
                           lambda self_a, c: (True, "wrote")),
              patch.object(self.hub.Api, "_backend_ready_error",
                           lambda self_a, b: None),
              patch.object(self.hub.Api, "_spawn_sdk_runner",
                           lambda self_a, key, cwd, pf, *a, **k:
                               captured.update(prompt=pf.read_text(encoding="utf-8")))):
            r = self.hub.Api().sdk_spawn(str(self.root))
        self.assertTrue(r.get("ok"), r)
        self.assertIn(str(self.root), captured["prompt"])
        self.assertNotIn("<", captured["prompt"])

    def test_mcp_brief_forbids_reading_during_checkin(self):
        import server
        self.assertIn("报到壳", server.MCP_INSTRUCTIONS_BRIEF)
        self.assertIn("v19", server.MCP_INSTRUCTIONS_BRIEF)
        self.assertIn("GetMcpTools", server.MCP_INSTRUCTIONS_BRIEF)
        self.assertIn("get_mcp_tools", server.MCP_INSTRUCTIONS_BRIEF)
        self.assertIn("rename_chat", server.MCP_INSTRUCTIONS_BRIEF)
        self.assertIn("Reload Window", server.MCP_INSTRUCTIONS_BRIEF)
        # 「禁止 GetMcpTools」必须留例外口：真名带前缀时准搜一次，否则云端壳锁死
        self.assertIn("does not exist", server.MCP_INSTRUCTIONS_BRIEF)

    def test_prompt_carries_the_mcp_name_escape_hatch(self):
        """08-27 用户：「有的 agent 首次接入说找不到rxyy MCP mcp，耗时很久」。
        云端/子代理运行时把项目级 MCP 展名成 project-0-<目录>-rxyy MCP，
        直呼「rxyy MCP」必报 does not exist；报到词又禁 GetMcpTools，
        agent 就地锁死（壳 f9c313ed 实测：白撞一次才违规去搜真名）。
        逃生口必须写在报到词本身——连不上服务器的 agent 看不到服务器侧
        任何 instructions。填好路径和占位兜底两条路都得带。"""
        filled, _ = self._prompt()
        fallback, _ = self._prompt(cwd="")
        # 这个类把真名钉死了，filled / 占位兜底都走短句：失败只重试，不准再搜工具表
        for prompt in (filled, fallback):
            self.assertIn("失败只重试禁搜", prompt)
            self.assertNotIn("搜「rxyy MCP」", prompt)


class RegisteredMcpNameTests(unittest.TestCase):
    """控制台自己就能算出 Cursor 工具表里的真名，报到词首调即中。

    08-28 用户追问「一开始找不到真名的问题还存在啊？」——壳 8828b74d 直呼
    「rxyy MCP」报 does not exist，又被报到词禁着 GetMcpTools，白烧一整轮
    才违规搜到「user-rxyy MCP」。Cursor 的展名规则是固定的：用户级
    %USERPROFILE%/.cursor/mcp.json 里的键加 user- 前缀。控制台读同一份
    文件就能提前算出来，没必要让每个壳都现场撞一次。
    """

    def setUp(self):
        import hub
        self.hub = hub
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        p = patch.object(hub.Path, "home", staticmethod(lambda: self.home))
        p.start()
        self.addCleanup(p.stop)

    def _seed(self, servers):
        d = self.home / ".cursor"
        d.mkdir(parents=True, exist_ok=True)
        (d / "mcp.json").write_text(
            json.dumps({"mcpServers": servers}, ensure_ascii=False),
            encoding="utf-8")

    def test_the_user_level_entry_gets_the_user_prefix(self):
        self._seed({"rxyy MCP": {"url": "http://127.0.0.1:39222/mcp"}})
        self.assertEqual("user-rxyy MCP", self.hub.registered_mcp_name(39222))

    def test_a_key_the_user_renamed_is_respected(self):
        # 真名跟着键名走：用户把条目改叫别的，报到词也得跟着叫别的
        self._seed({"我的控制台": {"url": "http://127.0.0.1:39222/mcp"}})
        self.assertEqual("user-我的控制台", self.hub.registered_mcp_name(39222))

    def test_disabled_entries_do_not_count(self):
        self._seed({"rxyy MCP": {"url": "http://127.0.0.1:39222/mcp",
                                "disabled": True}})
        self.assertEqual("", self.hub.registered_mcp_name(39222))

    def test_someone_elses_port_does_not_count(self):
        self._seed({"别家": {"url": "http://127.0.0.1:1234/mcp"}})
        self.assertEqual("", self.hub.registered_mcp_name(39222))

    def test_a_missing_file_returns_empty_instead_of_raising(self):
        self.assertEqual("", self.hub.registered_mcp_name(39222))

    def test_broken_json_returns_empty_instead_of_raising(self):
        d = self.home / ".cursor"
        d.mkdir(parents=True, exist_ok=True)
        (d / "mcp.json").write_text("{嗯这不是json", encoding="utf-8")
        self.assertEqual("", self.hub.registered_mcp_name(39222))


class CheckinPromptFirstCallHitsTheRealName(unittest.TestCase):
    """真名探测的结果要真落进报到词，探测不到也不能把词搞崩。"""

    def _fill(self, real, cwd=r"d:\Desktop\cursor工作流"):
        import hub
        with patch.object(hub, "registered_mcp_name", lambda port=None: real):
            return hub.fill_checkin_prompt(
                hub.DEFAULTS["new_chat_prompt"], "abcd1234", cwd)

    def test_the_detected_name_lands_in_the_first_line(self):
        import hub
        p = self._fill("user-rxyy MCP")
        self.assertIn("MCP「user-rxyy MCP」", p)
        # 名字大概率是对的，兜底换更短那句——报到词每个字都占四个壳的上下文
        self.assertIn(hub.MCP_NAME_ESCAPE_KNOWN, p)
        self.assertNotIn(hub.MCP_NAME_ESCAPE, p)

    def test_detection_failure_falls_back_to_the_plain_name(self):
        import hub
        p = self._fill("")
        self.assertIn("MCP「rxyy MCP」", p)
        self.assertIn(hub.MCP_NAME_ESCAPE, p, "算不出真名时全量逃生口必须在")

    def test_a_hand_edited_template_without_the_slot_keeps_the_full_escape(self):
        # 用户改过模板、里面没有 {mcp_name} 槽位：真名塞不进去，
        # 兜底就不能换短版——agent 手里仍只有「rxyy MCP」这个假名
        import hub
        old = "conversation_id=「{conversation_id}」；随便写点什么"
        with patch.object(hub, "registered_mcp_name", lambda port=None: "user-rxyy MCP"):
            p = hub.fill_checkin_prompt(old, "abcd1234", r"d:\x")
        self.assertIn(hub.MCP_NAME_ESCAPE, p)


class CheckinPromptFitsCursorsFirstMessageCap(unittest.TestCase):
    """报到词全文必须压在 Cursor 首条消息的字节硬顶之下。

    上限口径变过两次：08-28「提示词过长 780/768 错误码 1003」按字符计；
    08-31 用户截图「failed_precondition 输入过长，请精简后重试（1.38K/1.00K）」
    按 UTF-8 字节计——1.38K 恰是当时兜底版报到词的 1410 字节（1410/1024=1.377，
    中文一字 3B），上限 1024B；同晨一条 763B 的旧版报到词发送成功，两点互证。
    老的 768 字符闸对中文最多放行 2304B 完全失防，且兜底版还免检——全变体
    （1089~1479B）一起被拒，+号根本接不进来。发得出去的短版永远好过发不出去
    的全版。
    """

    def _fill(self, cwd, real="user-rxyy MCP"):
        import hub
        with patch.object(hub, "registered_mcp_name", lambda port=None: real):
            return hub.fill_checkin_prompt(
                hub.DEFAULTS["new_chat_prompt"], "abcd1234", cwd)

    @staticmethod
    def _bytes(s):
        return len(s.encode("utf-8"))

    def test_the_0831_rejected_placeholder_variant_now_fits(self):
        # 事故复现：截图里被拒的正是「+ → 复制（没选目录）+ 已探到真名」这个变体
        import hub
        p = self._fill("", real="user-rxyy MCP")
        self.assertLessEqual(self._bytes(p), hub.CURSOR_FIRST_MSG_MAX_UTF8,
                             (len(p), self._bytes(p)))
        # 预算内改名行保得住——08-28 一排同名 tab 的教训不能因省字节回潮
        self.assertIn("rename_chat", p)
        self.assertIn(hub.MCP_NAME_ESCAPE_KNOWN, p)

    def test_every_variant_fits_in_bytes(self):
        import hub
        for real in ("user-rxyy MCP", ""):
            for cwd in ("", r"D:\Desktop\cursor工作流"):
                p = self._fill(cwd, real=real)
                self.assertLessEqual(self._bytes(p), hub.CURSOR_FIRST_MSG_MAX_UTF8,
                                     (real, cwd, self._bytes(p)))

    def test_a_typical_path_fits_and_keeps_the_real_path(self):
        import hub
        p = self._fill(r"D:\Desktop\cursor工作流")
        self.assertLessEqual(self._bytes(p), hub.CURSOR_FIRST_MSG_MAX_UTF8)
        self.assertIn(r"D:\Desktop\cursor工作流", p, "合规时真路径必须保留")

    def test_a_monster_path_falls_back_to_placeholders_not_rejection(self):
        import hub
        monster = "D:\\" + "\\".join(["超长中文目录名第%d层" % i for i in range(24)])
        p = self._fill(monster)
        self.assertLessEqual(self._bytes(p), hub.CURSOR_FIRST_MSG_MAX_UTF8)
        self.assertNotIn(monster, p, "塞不下就整体退回占位符版，不能超顶硬发")
        self.assertIn("<工作区完整路径>", p)
        self.assertIn("abcd1234", p, "退回占位符版也不能丢对话 ID")

    def test_over_budget_sheds_rename_before_escape(self):
        # 逐段让位的顺序：改名行（锦上添花）先走，逃生口（失败时唯一活路）殿后
        import hub
        squeezed = self._bytes(self._fill("", real="user-rxyy MCP")) - 1
        with patch.object(hub, "CURSOR_FIRST_MSG_MAX_UTF8", squeezed):
            p = self._fill("", real="user-rxyy MCP")
        self.assertNotIn("rename_chat", p)
        self.assertIn(hub.MCP_NAME_ESCAPE_KNOWN, p)

    def test_the_floor_always_ships_even_when_it_cannot_fit(self):
        # 用户钉过超长自定义模板时地板也会超——那也得发（旧行为不倒退）：
        # 起码别再往上摞改名行/逃生口，更不能空手而归
        import hub
        with patch.object(hub, "CURSOR_FIRST_MSG_MAX_UTF8", 10):
            p = self._fill("", real="user-rxyy MCP")
        self.assertIn("abcd1234", p)
        self.assertNotIn("rename_chat", p)


class SpawnExtrasShareTheFirstMessageBudget(unittest.TestCase):
    """「+ 直拉本机 agent」闸后追加的 task_name/首任务两行不能把报到词重新顶超。

    08-31 同类路径排查：sdk_spawn 原来在长度闸之后裸拼这两行，first_task 一长，
    刚过 1024B 字节闸的报到词照样被拒、agent 根本拉不起来。首任务全文另有
    预建壳队列兜底，提示词里被截不等于任务丢失。
    """

    def test_short_extras_are_appended_verbatim(self):
        import hub
        out = hub.append_spawn_extras("报到词", "视频线·剪辑修复", "把导出崩溃修了")
        self.assertIn("task_name 改用「视频线·剪辑修复」", out)
        self.assertIn("立即开始执行以下任务：把导出崩溃修了", out)

    def test_a_long_first_task_is_trimmed_to_fit_not_rejected(self):
        import hub
        out = hub.append_spawn_extras("报到词", "视频线", "修复导出崩溃。" * 300)
        self.assertLessEqual(len(out.encode("utf-8")), hub.CURSOR_FIRST_MSG_MAX_UTF8)
        self.assertIn("立即开始执行以下任务：", out)
        self.assertTrue(out.endswith("…"), "截过要带省略号，别让 agent 以为任务就这半句")

    def test_task_name_wins_and_first_task_yields_when_space_is_tight(self):
        import hub
        near_cap = "字" * 320  # 960B，只够塞 task_name 行
        out = hub.append_spawn_extras(near_cap, "视频线", "修复导出崩溃" * 50)
        self.assertLessEqual(len(out.encode("utf-8")), hub.CURSOR_FIRST_MSG_MAX_UTF8)
        self.assertIn("task_name 改用「视频线」", out)
        self.assertNotIn("立即开始执行以下任务", out, "塞不下就全靠队列，别硬挤")


class RunAsScriptTests(unittest.TestCase):
    """手动入口 `rxyy-tools-community.exe --run mcp_touch.py`：它得真干活，也得真退出来。

    打包 exe 的 stdout 是 gbk/surrogateescape（它不认 PYTHONUTF8），编不出的字符
    抛 UnicodeEncodeError；而窗口化的 PyInstaller 程序遇未捕获异常弹的是一个看不见
    的模态框，进程从此挂死。08-07 实测：加了 __main__ 块的头一版正文带「✓」，
    nonce 写进去了却再也不退出，比原来「exit=0 却什么都没干」更难查。
    这里用 PYTHONIOENCODING=gbk 把那条窄通道复现出来。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def _seed(self, cfg):
        (self.home / ".cursor").mkdir(parents=True, exist_ok=True)
        (self.home / ".cursor" / "mcp.json").write_text(
            json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    def _run(self):
        env = dict(os.environ)
        env["USERPROFILE"] = str(self.home)
        env["PYTHONIOENCODING"] = "gbk"
        env.pop("PYTHONUTF8", None)
        return subprocess.run([sys.executable, str(MODULE_DIR / "mcp_touch.py")],
                              env=env, capture_output=True, timeout=60)

    def test_a_gbk_stdout_does_not_blow_up_the_script(self):
        self._seed({"mcpServers": {"rxyy MCP": {"url": "http://127.0.0.1:39222/mcp"}}})
        r = self._run()
        self.assertEqual(0, r.returncode, r.stderr[-400:])
        self.assertNotIn(b"UnicodeEncodeError", r.stderr)
        entry = json.loads((self.home / ".cursor" / "mcp.json").read_text(
            encoding="utf-8"))["mcpServers"]["rxyy MCP"]
        self.assertIn("X-Rxyy-Mcp-Reload", entry["headers"])

    def test_a_failure_exits_nonzero_and_still_says_why(self):
        # 没有 .cursor/mcp.json：失败路径的那句话也得编得出去
        r = self._run()
        self.assertEqual(1, r.returncode)
        self.assertNotIn(b"UnicodeEncodeError", r.stderr)
        self.assertIn("触碰失败", r.stdout.decode("gbk", "replace"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
