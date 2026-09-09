# -*- coding: utf-8 -*-
"""报到时不知道工作区的壳，拿到真工作区后把 tab 名补正。

08-28 用户截图：一排新壳全叫「待命·workspace」，追问「接入我感觉还有问题」。
根子在「+ → 复制」——这条路（实测最常用）压根不选目录，控制台生成报到词时
真不知道这段话会被粘到哪个 Cursor 窗口，只能发占位符下去。

契约分两段，本测试各锁一段：

1. 报到词里那句 rename_chat 的标题也走占位符，不许写死 workspace——手上有真
   路径的主对话一次替换，控制台名字和 Cursor 侧栏名字必然同一个答案。
2. 工作区总会到（下一次调用带 project_path / 项目级 MCP 的 cwd / 它自己的
   Cursor 流水落在某个工作区槽位下），到了就把「待命·workspace」补正成
   「待命·<工作区>·<对话ID前4位>」。只补占位名：锁过名字的、自报过真名的、
   已经认领了活的一概不碰（把干活 tab 降级成待命，08-25 出过事故）。
"""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub
from session_locator import cursor_project_slug

WS = r"d:\Desktop\cursor工作流"
OTHER_WS = r"d:\Desktop\smart_cloud_broadcasting_live_api"


def _s(sid, conv, name, cwd="", **kw):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = sid, conv, name
    s.name_locked = kw.get("name_locked", False)
    s.shell_born = kw.get("shell_born", name.startswith("待命"))
    s.agent_named = kw.get("agent_named", False)
    s.claimed_task_ts = kw.get("claimed_task_ts", 0)
    s.agent_status_ts = kw.get("agent_status_ts", 0)
    s.agent_status = kw.get("agent_status", "")
    s.cwd = cwd
    s.task_root = kw.get("task_root", "")
    s.peer_ip = None
    s.pid = 1
    s.msg_seq = kw.get("msg_seq", 1)
    s.real_seq = kw.get("real_seq", 1)
    s.messages = []
    s.queued = []
    s.pending = None
    s.pending_lost = False
    s.cursor_uuid = None
    s.uuid_verified = False
    s.cursor_title = None
    s.transcript_path = kw.get("transcript_path", None)
    s.created_at = "2026-08-28 09:02:00"
    s.created_ts = time.time()
    s.file_path = None
    s.connected = True
    s.client = None
    s.archived = False
    s.rev = 0
    s.lock = threading.Lock()
    return s


def _transcript(root, slug):
    return str(Path(root) / ".cursor" / "projects" / slug
               / "agent-transcripts" / "u1" / "u1.jsonl")


class PlaceholderNameTests(unittest.TestCase):
    """哪些名字算「工作区那半还没填上」。"""

    def test_the_placeholder_spellings_are_all_recognised(self):
        for name in ("待命·workspace", "待命·workspace·d704", "待命·workspace2",
                     "待命·<工作区目录名>·d704", "待命·工作区目录名"):
            self.assertTrue(hub.standby_name_is_placeholder(name), name)

    def test_a_real_workspace_name_is_left_alone(self):
        for name in ("待命·cursor工作流", "待命·cursor工作流·d704", "换装收尾",
                     "控制台·侧栏改名", "Persistent plus zhi report"):
            self.assertFalse(hub.standby_name_is_placeholder(name), name)

    def test_a_workspace_actually_called_workspace_is_a_corner_we_accept(self):
        # 真有人把目录叫 workspace 时补正会变成空转（改成同名后返回 False），
        # 不会反复改名，也不会误伤——代价可以接受
        self.assertTrue(hub.standby_name_is_placeholder("待命·Workspace·d704"))


class HealFromKnownCwdTests(unittest.TestCase):
    """壳下一次调用带上了真目录：当场补正。"""

    def setUp(self):
        self.log = []
        p = patch.object(hub, "log_event", lambda *a, **k: self.log.append(a))
        p.start()
        self.addCleanup(p.stop)

    def _heal(self, s, others=()):
        pool = {x.id: x for x in (s,) + tuple(others)}
        with patch.object(hub.HUB, "sessions", pool):
            return hub.HUB.heal_standby_tab_name(s)

    def test_the_placeholder_tab_takes_the_real_workspace_name(self):
        s = _s("s1", "d7044dd1", "待命·workspace·d704", cwd=WS)
        self.assertTrue(self._heal(s))
        self.assertEqual("待命·cursor工作流·d704", s.name)

    def test_a_shell_that_pasted_the_placeholder_verbatim_is_healed_too(self):
        # 上下文里没路径的子代理有时把尖括号原样报上来
        s = _s("s1", "e01717aa", "待命·<工作区目录名>·e017", cwd=WS)
        self.assertTrue(self._heal(s))
        self.assertEqual("待命·cursor工作流·e017", s.name)

    def test_an_already_correct_shell_is_not_touched(self):
        s = _s("s1", "d7044dd1", "待命·cursor工作流·d704", cwd=WS)
        self.assertFalse(self._heal(s))
        self.assertEqual("待命·cursor工作流·d704", s.name)

    def test_no_workspace_yet_means_no_rename(self):
        s = _s("s1", "d7044dd1", "待命·workspace·d704", cwd="")
        self.assertFalse(self._heal(s))
        self.assertEqual("待命·workspace·d704", s.name)

    def test_the_runtime_directory_never_counts_as_a_workspace(self):
        # hello 落在常驻区时 cwd 是 live\rxyy_mcp，补正成「待命·rxyy MCP」纯属添乱
        s = _s("s1", "d7044dd1", "待命·workspace·d704",
               cwd=r"C:\Users\x\AppData\Local\rxyy-tools-community\live\rxyy_mcp")
        self.assertFalse(self._heal(s))
        self.assertEqual("待命·workspace·d704", s.name)

    def test_a_tab_that_claimed_work_is_never_renamed_back_to_standby(self):
        """08-25 事故的同一条铁律：认领过活的 tab 一律不许被降级成壳名。
        它现在叫「待命·workspace」只是还没自报真名，补正会把「待命」这层
        身份再钉一次，下一个读到它的 agent 又以为自己在待命。"""
        s = _s("s1", "d7044dd1", "待命·workspace·d704", cwd=WS,
               claimed_task_ts=time.time(), real_seq=9, msg_seq=9)
        self.assertFalse(self._heal(s))

    def test_a_locked_or_self_named_tab_is_never_renamed(self):
        locked = _s("s1", "c1", "待命·workspace", cwd=WS, name_locked=True)
        named = _s("s2", "c2", "待命·workspace", cwd=WS, agent_named=True)
        self.assertFalse(self._heal(locked))
        self.assertFalse(self._heal(named))

    def test_two_shells_in_one_workspace_still_get_distinct_names(self):
        a = _s("s1", "aaaa1111", "待命·workspace·aaaa", cwd=WS)
        b = _s("s2", "bbbb2222", "待命·workspace·bbbb", cwd=WS)
        self.assertTrue(self._heal(a, others=(b,)))
        self.assertTrue(self._heal(b, others=(a,)))
        self.assertNotEqual(a.name, b.name)

    def test_the_rename_is_logged_so_a_surprise_name_can_be_traced(self):
        s = _s("s1", "d7044dd1", "待命·workspace·d704", cwd=WS)
        self._heal(s)
        self.assertTrue(any("工作区补正" in str(a) for a in self.log), self.log)


class HealFromTranscriptTests(unittest.TestCase):
    """壳从没说过自己在哪：拿它 Cursor 流水所在的工作区槽位反查。"""

    def setUp(self):
        p = patch.object(hub, "log_event", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)

    def _heal(self, s, roots):
        # roots 模拟「控制台已知的工作区」：在线会话的 cwd / 团队登记的 root
        pool = {s.id: s}
        for i, r in enumerate(roots):
            x = _s("known%d" % i, "k%d" % i, "干活中%d" % i, cwd=r,
                   agent_named=True, real_seq=9, msg_seq=9)
            pool[x.id] = x
        with patch.object(hub.HUB, "sessions", pool):
            return hub.HUB.heal_standby_tab_name(s)

    def test_the_transcript_slot_identifies_the_workspace(self):
        """「+ → 复制」没选目录、壳也报不出 project_path 时，唯一的铁证是它
        一开口流水就落在自己窗口的 .cursor/projects/<slug> 槽位下。"""
        s = _s("s1", "d7044dd1", "待命·workspace·d704",
               transcript_path=_transcript(Path.home(), cursor_project_slug(WS)))
        self.assertTrue(self._heal(s, [WS, OTHER_WS]))
        self.assertEqual("待命·cursor工作流·d704", s.name)

    def test_an_unknown_slot_is_left_alone(self):
        s = _s("s1", "d7044dd1", "待命·workspace·d704",
               transcript_path=_transcript(Path.home(), "d-somewhere-else"))
        self.assertFalse(self._heal(s, [WS, OTHER_WS]))

    def test_two_workspaces_sharing_one_slug_are_refused(self):
        """slug 把非 ASCII 全压掉：D:\\Desktop\\视频快编 与 D:\\Desktop\\音频快编
        算出来是同一个 d-desktop。猜错就是把 tab 改成隔壁项目的名字，
        比继续叫 workspace 还糟——撞了就一个都不认。"""
        a, b = r"d:\Desktop\视频快编", r"d:\Desktop\音频快编"
        self.assertEqual(cursor_project_slug(a), cursor_project_slug(b))
        s = _s("s1", "d7044dd1", "待命·workspace·d704",
               transcript_path=_transcript(Path.home(), cursor_project_slug(a)))
        self.assertFalse(self._heal(s, [a, b]))

    def test_the_cwd_it_reported_wins_over_the_slug_guess(self):
        s = _s("s1", "d7044dd1", "待命·workspace·d704", cwd=OTHER_WS,
               transcript_path=_transcript(Path.home(), cursor_project_slug(WS)))
        self.assertTrue(self._heal(s, [WS, OTHER_WS]))
        self.assertEqual("待命·smart_cloud_broadcasting_live_api·d704", s.name)


class WiringTests(unittest.TestCase):
    """补正得真的挂在「工作区落地」的那两个时刻上，光有函数不算修好。"""

    def setUp(self):
        p = patch.object(hub, "log_event", lambda *a, **k: None)
        p.start()
        self.addCleanup(p.stop)

    def test_the_next_call_carrying_a_real_project_path_heals_the_tab(self):
        """壳报到时报不出目录，下一次 zhi/zt 带了 project_path（server 把它
        变成本次请求的 cwd）——这是最常见的一条「工作区到了」。"""
        s = _s("s1", "d7044dd1", "待命·workspace·d704")
        client = type("C", (), {})()
        client.sessions = {"d7044dd1": s}
        client.cwd = WS
        client.peer_ip = None
        client.pid = 1
        client.closed_convs = {}
        with (patch.object(hub.HUB, "sessions", {"s1": s}),
              patch.object(hub.Hub, "_reap_handed_off_shell",
                           lambda self_h, c, k: None),
              patch.object(hub.Hub, "takeover_landed", lambda self_h, x: None),
              patch.object(hub.Hub, "_bind_registered_team_root",
                           lambda self_h, x: None)):
            hub.HUB.resolve_session(client, "d7044dd1", "待命·workspace")
        self.assertEqual("待命·cursor工作流·d704", s.name)
        self.assertEqual(WS, s.cwd)

    def test_title_sync_heals_a_shell_the_moment_it_locates_the_transcript(self):
        """壳一句 project_path 都不肯说时，标题同步循环里那次定位就是唯一
        能拿到答案的地方——它刚把 transcript_path 绑上，紧接着就该补正。"""
        s = _s("s1", "d7044dd1", "待命·workspace·d704")
        seen = []
        located = ("u1", _transcript(Path.home(), cursor_project_slug(WS)))
        with (patch.object(hub.HUB, "sessions", {"s1": s}),
              patch.object(hub.HUB, "order", ["s1"]),
              patch.object(hub, "locate_cursor_session", lambda *a, **k: located),
              patch.object(hub.Hub, "heal_standby_tab_name",
                           lambda self_h, x: seen.append(x.name))):
            hub.HUB.sync_cursor_titles()
        self.assertEqual(["待命·workspace·d704"], seen)

    def test_title_sync_still_refuses_to_write_shell_names_into_cursor(self):
        # 壳的 uuid 是猜的，往外写就可能把别人干活的对话改名叫「待命·xxx」
        s = _s("s1", "d7044dd1", "待命·workspace·d704")
        known = _s("s2", "k1", "控制台·侧栏改名", cwd=WS, agent_named=True,
                   real_seq=9, msg_seq=9)
        wrote = []
        located = ("u1", _transcript(Path.home(), cursor_project_slug(WS)))
        with (patch.object(hub.HUB, "sessions", {"s1": s, "s2": known}),
              patch.object(hub.HUB, "order", ["s1"]),
              patch.object(hub, "locate_cursor_session", lambda *a, **k: located),
              patch.object(hub, "write_cursor_title",
                           lambda *a, **k: wrote.append(a) or True)):
            hub.HUB.sync_cursor_titles()
        self.assertEqual([], wrote)
        self.assertEqual("待命·cursor工作流·d704", s.name)


if __name__ == "__main__":
    unittest.main()
