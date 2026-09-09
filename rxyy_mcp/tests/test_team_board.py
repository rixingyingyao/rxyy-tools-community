# -*- coding: utf-8 -*-
"""团队黑板（重大情况主动读写）+「对话从 Cursor 里消失」判死。

设计约定：六类（部署/大改/提交/卡住/事故/收工）都落盘上面板；部署/事故/大改
额外提醒**同一条业务线**的在线队友（08-03 曾整个去掉，因为同一工作区常挂着互不
相干的项目、无差别推送只会打扰，现在按业务线收窄）；读黑板只回本项目那一组；
黑板跨重启持久化。
对话被删/被政策回收时 composerData 键整个消失——验过身份的 uid 查无此键即判死。
"""
import json
import sys
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub

WS1 = r"d:\Desktop\cursor工作流"
WS2 = r"c:\Users\Administrator\AICodebrain"


def _s(sid, conv, name, root, connected=True, track=""):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = sid, conv, name
    s.cwd = s.task_root = root
    s.connected = connected
    s.pending = None
    s.queued = []
    s._track = track
    s.agent_project = ""
    s.lock = threading.Lock()
    return s


class _BoardHarness(unittest.TestCase):
    def _post(self, sender, kind, text, sessions, notify_ts=None):
        delivered = []

        def fake_queue(api_self, sid, txt, imgs, who=None, files=None, force=False):
            delivered.append({"sid": sid, "text": txt, "who": who})
            return {"ok": True, "qid": "q"}

        d = {x.id: x for x in sessions}
        self.bulletin = {}
        with (patch.object(hub.HUB, "sessions", d),
              patch.object(hub.HUB, "order", list(d)),
              patch.object(hub.HUB, "bulletin", self.bulletin),
              patch.object(hub.HUB, "_board_notify_ts",
                           notify_ts if notify_ts is not None else {}, create=True),
              patch.object(hub.Hub, "_save_bulletin", lambda self: None),
              patch.object(hub.Api, "queue_message", fake_queue),
              patch.object(hub.Api, "session_label", lambda self, s: s.name),
              patch.object(hub.Api, "_team_role", lambda self, s: ""),
              patch.object(hub.Api, "_team_assign", lambda self, s: ""),
              patch.object(hub.Api, "_team_track",
                           lambda self, s: getattr(s, "_track", ""))):
            r = hub.Api().board_from_agent(sender, kind, text)
        return r, delivered


class BoardTests(_BoardHarness):
    def test_entry_lands_on_project_board(self):
        a = _s("s1", "aaaa1111", "甲", WS1)
        r, _ = self._post(a, "收工", "周报链路修完可验收", [a])
        self.assertTrue(r["ok"])
        entries = list(self.bulletin.values())[0]
        self.assertEqual(1, len(entries))
        e = entries[0]
        self.assertEqual("收工", e["kind"])
        self.assertEqual("甲", e["from_label"])
        self.assertIn("周报链路", e["text"])

    def test_urgent_kind_reminds_the_same_track_only(self):
        # 08-03 曾把提醒整个去掉，因为分组键是工作区根目录，而同一工作区常挂着
        # 互不相干的项目（实测 ctest 事故推给了全部 agent）。现在按业务线收窄
        a = _s("s1", "aaaa1111", "甲", WS1, track="rxyy MCP")
        mate = _s("s2", "bbbb2222", "乙", WS1, track="rxyy MCP")
        other_track = _s("s3", "cccc3333", "丙", WS1, track="直播")
        outsider = _s("s4", "dddd4444", "丁", WS2, track="rxyy MCP")
        r, sent = self._post(a, "部署", "要换装了，断10-30秒",
                             [a, mate, other_track, outsider])
        self.assertTrue(r["ok"])
        self.assertEqual(["s2"], [x["sid"] for x in sent])  # 不推自己/别的线/外项目
        self.assertIn("【黑板·部署】", sent[0]["text"])
        self.assertEqual("黑板", sent[0]["who"])   # 机器代递，不占用户的回复位
        self.assertEqual(["乙"], r["pushed"])

    def test_no_track_reminds_the_others_without_a_track(self):
        # 都没分线时照常互相知会；但别去打断已经分好线的队伍
        a = _s("s1", "aaaa1111", "甲", WS1)
        plain = _s("s2", "bbbb2222", "乙", WS1)
        tracked = _s("s3", "cccc3333", "丙", WS1, track="直播")
        r, sent = self._post(a, "事故", "38999 没人监听了", [a, plain, tracked])
        self.assertEqual(["s2"], [x["sid"] for x in sent])
        self.assertEqual(["乙"], r["pushed"])

    def test_offline_teammate_is_not_pushed(self):
        a = _s("s1", "aaaa1111", "甲", WS1)
        gone = _s("s2", "bbbb2222", "乙", WS1, connected=False)
        r, sent = self._post(a, "大改", "要动 hub.py 了", [a, gone])
        self.assertEqual([], sent)
        self.assertEqual([], r["pushed"])

    def test_quiet_kind_does_not_push(self):
        a = _s("s1", "aaaa1111", "甲", WS1)
        b = _s("s2", "bbbb2222", "乙", WS1)
        r, sent = self._post(a, "提交", "合入 e1752e7", [a, b])
        self.assertTrue(r["ok"])
        self.assertEqual([], sent)

    def test_unknown_kind_normalized(self):
        a = _s("s1", "aaaa1111", "甲", WS1)
        r, _ = self._post(a, "", "动 app.js 周报段", [a])
        self.assertEqual("大改", r["kind"])

    def test_empty_text_rejected(self):
        a = _s("s1", "aaaa1111", "甲", WS1)
        r, _ = self._post(a, "部署", "   ", [a])
        self.assertFalse(r["ok"])


class BoardNotifyCooldownTests(_BoardHarness):
    """08-12 实测：一轮优化连发 6 条「部署」黑板，队友每次 zt 取信都被塞一条
    提醒，连环打断。同人同类（部署/大改）冷却窗内只推第一条、后续只落盘；
    「事故」不降噪（连发往往是事态升级，宁吵勿漏）。"""

    def setUp(self):
        self.a = _s("s1", "aaaa1111", "甲", WS1)
        self.mate = _s("s2", "bbbb2222", "乙", WS1)

    def test_repeat_deploy_notice_is_quiet(self):
        ts = {}
        r1, sent1 = self._post(self.a, "部署", "第一波要重启 hub",
                               [self.a, self.mate], notify_ts=ts)
        r2, sent2 = self._post(self.a, "部署", "第二波又要重启",
                               [self.a, self.mate], notify_ts=ts)
        self.assertEqual(["s2"], [x["sid"] for x in sent1])
        self.assertEqual([], sent2)      # 冷却中：不再打扰队友
        self.assertTrue(r2["ok"])        # 黑板本身照写、面板照见
        self.assertEqual([], r2["pushed"])

    def test_incident_never_quieted(self):
        ts = {}
        _, s1 = self._post(self.a, "事故", "39222 挂了",
                           [self.a, self.mate], notify_ts=ts)
        _, s2 = self._post(self.a, "事故", "39222 还在挂",
                           [self.a, self.mate], notify_ts=ts)
        self.assertEqual(1, len(s1))
        self.assertEqual(1, len(s2))

    def test_cooldown_expires(self):
        ts = {}
        self._post(self.a, "部署", "第一波", [self.a, self.mate], notify_ts=ts)
        for k in ts:
            ts[k] -= hub.Api.BOARD_NOTIFY_COOLDOWN_SECS + 1
        _, s2 = self._post(self.a, "部署", "半小时后的下一波",
                           [self.a, self.mate], notify_ts=ts)
        self.assertEqual(["s2"], [x["sid"] for x in s2])

    def test_unpushed_notice_does_not_burn_the_window(self):
        # 第一条发出时队友都不在线：窗口留给下一条，别让空推吃掉唯一一次提醒
        ts = {}
        _, s1 = self._post(self.a, "部署", "没人在线那波", [self.a], notify_ts=ts)
        self.assertEqual([], s1)
        _, s2 = self._post(self.a, "部署", "现在有人了",
                           [self.a, self.mate], notify_ts=ts)
        self.assertEqual(["s2"], [x["sid"] for x in s2])

    def test_another_sender_not_affected(self):
        ts = {}
        self._post(self.a, "部署", "甲要重启", [self.a, self.mate], notify_ts=ts)
        _, s2 = self._post(self.mate, "部署", "乙也要动",
                           [self.a, self.mate], notify_ts=ts)
        self.assertEqual(["s1"], [x["sid"] for x in s2])


class BoardReadScopeTests(unittest.TestCase):
    """读黑板只回本项目那一组。

    写入端按项目根分组，读取端原先却把所有项目揉在一起取最近 15 条——一个工作区
    忙起来就能把别人的黑板挤没，读到的还全是不相干项目的事（08-04 用户实测）。
    """

    def _read(self, board, project_path, task_name="", conversation_id="",
              sessions=None, config=None):
        import tempfile
        import server
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "board.json").write_text(
                json.dumps(board, ensure_ascii=False), encoding="utf-8")
            if sessions is not None:
                (Path(td) / ".sessions.json").write_text(
                    json.dumps(sessions, ensure_ascii=False), encoding="utf-8")
            if config is not None:
                (Path(td) / "config.json").write_text(
                    json.dumps(config, ensure_ascii=False), encoding="utf-8")
            with patch.object(server, "DATA_DIR", Path(td)):
                return server._board_read_text(project_path, task_name, conversation_id)

    BOARD = {
        WS1: [{"ts": 2.0, "day": "08-04", "hms": "10:00", "kind": "部署",
               "from_label": "甲", "text": "cursor工作流要换装"}],
        WS2: [{"ts": 3.0, "day": "08-04", "hms": "10:05", "kind": "事故",
               "from_label": "乙", "text": "AICodebrain 的活跟你无关"}],
    }

    def test_only_my_projects_entries_come_back(self):
        out = self._read(self.BOARD, WS1)
        self.assertIn("cursor工作流要换装", out)
        self.assertNotIn("跟你无关", out)   # 别的项目的事哪怕更新也不该挤进来
        self.assertIn("本项目", out)

    def test_path_case_and_slashes_still_match(self):
        out = self._read(self.BOARD, WS1.upper().replace("\\", "/") + "\\")
        self.assertIn("cursor工作流要换装", out)

    def test_0831_no_group_no_longer_dumps_every_project(self):
        # 0831 用户拍板「黑板按项目来」：报了工作区但查无分组，不再「宁可多给」
        # 把全库倒给它——跨工作区接手早已靠 conversation_id 快照解析真实归属
        # （见 test_cross_workspace_session_snapshot_resolves_real_named_scope），
        # 全量兜底只留给连工作区都没报的旧 agent（见下条）。
        out = self._read(self.BOARD, r"e:\somewhere-else")
        self.assertNotIn("cursor工作流要换装", out)
        self.assertNotIn("跟你无关", out)
        self.assertIn("团队黑板还是空的", out)

    def test_agent_reporting_nothing_still_gets_legacy_history(self):
        # 什么都没报的旧 agent 无从判定归属，保留全量兜底并如实标注来源
        out = self._read(self.BOARD, "")
        self.assertIn("cursor工作流要换装", out)
        self.assertIn("工作区历史", out)

    def test_empty_board_says_how_to_write_one(self):
        self.assertIn("ji(action=", self._read({}, WS1))

    def test_named_project_reads_only_its_exact_scope(self):
        board = {
            "_scopes": {
                hub.norm_root(WS1): {
                    "alpha": [{"ts": 3.0, "day": "08-07", "hms": "10:00",
                               "kind": "大改", "from_label": "甲", "text": "alpha 条目"}],
                    "beta": [{"ts": 4.0, "day": "08-07", "hms": "10:01",
                              "kind": "事故", "from_label": "乙", "text": "beta 条目"}],
                }
            },
            WS1: [{"ts": 5.0, "day": "08-07", "hms": "10:02",
                   "kind": "提交", "from_label": "旧", "text": "旧根级条目"}],
        }
        out = self._read(board, WS1, "Alpha·后端隔离")
        self.assertIn("alpha 条目", out)
        self.assertNotIn("beta 条目", out)
        self.assertNotIn("旧根级条目", out)

    def test_cross_workspace_session_snapshot_resolves_real_named_scope(self):
        board = {"_scopes": {hub.norm_root(WS1): {
            "alpha": [{"ts": 3.0, "day": "08-07", "hms": "10:00",
                       "kind": "大改", "from_label": "甲", "text": "alpha 条目"}],
            "beta": [{"ts": 4.0, "day": "08-07", "hms": "10:01",
                      "kind": "事故", "from_label": "乙", "text": "beta 条目"}],
        }}}
        sessions = [{"id": "sid-a", "conv_key": "conv-a", "cwd": WS2,
                     "task_root": WS1, "agent_project": "Alpha"}]
        out = self._read(board, WS2, "Alpha·隔离", "conv-a", sessions, {})
        self.assertIn("alpha 条目", out)
        self.assertNotIn("beta 条目", out)

    def test_member_project_wins_and_track_never_becomes_a_project(self):
        board = {"_scopes": {hub.norm_root(WS1): {
            "alpha": [{"ts": 3.0, "day": "08-07", "hms": "10:00",
                       "kind": "大改", "from_label": "甲", "text": "alpha 条目"}],
            "data": [{"ts": 4.0, "day": "08-07", "hms": "10:01",
                      "kind": "事故", "from_label": "乙", "text": "业务线误分组"}],
        }}}
        sessions = [{"id": "sid-a", "conv_key": "conv-a", "cwd": WS2,
                     "task_root": WS1, "agent_project": "Beta"}]
        config = {
            "team_tracks": {"conv-a": "data"},
            "team_project_members": {"conv-a": {
                "root": hub.norm_root(WS1), "project": "alpha", "name": "Alpha",
            }},
        }
        out = self._read(board, WS2, "Beta·数据线", "conv-a", sessions, config)
        self.assertIn("alpha 条目", out)
        self.assertNotIn("业务线误分组", out)

    def test_member_registration_root_wins_before_any_session_snapshot(self):
        board = {"_scopes": {hub.norm_root(WS1): {
            "alpha": [{"ts": 3.0, "day": "08-07", "hms": "10:00",
                       "kind": "大改", "from_label": "甲", "text": "alpha 条目"}],
        }}}
        config = {"team_project_members": {"new-alpha": {
            "root": hub.norm_root(WS1), "project": "alpha", "name": "Alpha",
        }}}
        out = self._read(board, WS2, "临时名字", "new-alpha", [], config)
        self.assertIn("alpha 条目", out)

    def test_seat_registration_root_wins_before_any_session_snapshot(self):
        board = {"_scopes": {hub.norm_root(WS1): {
            "beta": [{"ts": 3.0, "day": "08-07", "hms": "10:00",
                      "kind": "大改", "from_label": "乙", "text": "beta 条目"}],
        }}}
        config = {"team_projects": {hub.norm_root(WS1): {
            "beta": {"name": "Beta", "seats": [{"id": "seat-beta"}]},
        }}}
        out = self._read(board, WS2, "临时名字", "seat-beta", [], config)
        self.assertIn("beta 条目", out)


class TeamScopeTests(unittest.TestCase):
    """同一 task_root 下的命名项目不能共用可写团队资源。"""

    def _cfg(self):
        return {"team_projects": {}, "team_seats": {}, "team_boards": {},
                "team_project_members": {}, "team_tracks": {}, "team_roles": {},
                "team_assign": {}}

    def _agent(self, sid, conv, project):
        s = _s(sid, conv, sid, WS1)
        s.agent_project = project
        s.pid = 0
        return s

    def test_named_project_seats_and_boards_do_not_touch_legacy_root_data(self):
        cfg = self._cfg()
        api = hub.Api()
        with (patch.object(hub.HUB, "cfg", cfg),
              patch.object(hub, "save_config", lambda c: None)):
            alpha = api.team_seat_add(WS1, name="Alpha 实现", project="Alpha")
            api.team_set_board(WS1, "只给 Alpha", project="Alpha")
            api.team_set_board(WS1, "旧根级公告")
            self.assertTrue(alpha["ok"])
            self.assertEqual({}, cfg["team_seats"])
            self.assertEqual("只给 Alpha", api._team_board(WS1, "alpha")["text"])
            self.assertEqual("", api._team_board(WS1, "Beta")["text"])
            self.assertEqual("旧根级公告", api._team_board(WS1)["text"])
            self.assertEqual(1, len(api._seats(WS1, "Alpha")))
            self.assertEqual([], api._seats(WS1, "Beta"))

    def test_scope_prefers_member_then_named_project_then_seat(self):
        cfg = self._cfg()
        api = hub.Api()
        with (patch.object(hub.HUB, "cfg", cfg),
              patch.object(hub, "save_config", lambda c: None)):
            seat = api.team_seat_add(WS1, name="Alpha 实现", project="Alpha")["seat"]
            seated = self._agent("s1", seat["id"], "Beta")
            member = self._agent("s2", "cmember", "Beta")
            fallback = self._agent("s3", "cfallback", "Beta")
            nameless = self._agent("s4", seat["id"], "")
            cfg["team_project_members"] = {
                "cmember": {"root": hub.norm_root(WS1), "project": "alpha",
                            "name": "Alpha 固定组"},
            }
            cfg["team_tracks"] = {seat["id"]: "发布线", "cmember": "数据线"}
            self.assertEqual("beta", api._team_scope(seated)["project"],
                             "自报项目优先于旧席位")
            self.assertEqual("alpha", api._team_scope(member)["project"], "成员登记钉住")
            self.assertEqual("Alpha 固定组", api._team_scope(member)["name"])
            self.assertEqual("beta", api._team_scope(fallback)["project"])
            self.assertEqual("alpha", api._team_scope(nameless)["project"],
                             "没自报时席位仍认领")
            self.assertEqual("发布线", api._team_track(seated))

    def test_intake_registers_a_project_member_before_the_conversation_connects(self):
        cfg = self._cfg()
        api = hub.Api()
        prompt = {"ok": True, "prompt": "报到", "conversation_id": "new-alpha"}
        with (patch.object(hub.HUB, "cfg", cfg),
              patch.object(hub.Api, "new_chat_prompt", lambda self, *a, **k: dict(prompt)),
              patch.object(hub, "save_config", lambda c: None)):
            result = api.team_intake_prompt(
                WS1, project="Alpha", role="review", assign="隔离审查", track="数据线")
        self.assertTrue(result["ok"])
        self.assertEqual("alpha", result["project"])
        self.assertEqual({"root": hub.norm_root(WS1), "project": "alpha", "name": "Alpha"},
                         cfg["team_project_members"]["new-alpha"])
        self.assertEqual("review", cfg["team_roles"]["new-alpha"])
        self.assertEqual("隔离审查", cfg["team_assign"]["new-alpha"])
        self.assertEqual("数据线", cfg["team_tracks"]["new-alpha"])

    def test_first_report_binds_intake_and_seat_to_target_root(self):
        cfg = self._cfg()
        cfg["team_project_members"] = {
            "new-alpha": {"root": hub.norm_root(WS1), "project": "alpha",
                          "name": "Alpha"},
        }
        cfg["team_projects"] = {
            hub.norm_root(WS1): {
                "beta": {"name": "Beta", "board": {}, "seats": [
                    {"id": "seat-beta", "name": "Beta 审查", "role": "review"},
                ]},
            },
        }

        def client():
            return hub.Client(None, WS2, 123)

        with (patch.object(hub.HUB, "cfg", cfg),
              patch.object(hub.HUB, "sessions", {}),
              patch.object(hub.HUB, "order", []),
              patch.object(hub.Hub, "_init_file", lambda self, s: None),
              patch.object(hub.Hub, "_window_tag", lambda *args: ""),
              patch.object(hub.Hub, "yield_zhi", lambda *args, **kwargs: None),
              patch.object(hub.Hub, "start_yield_burst", lambda *args, **kwargs: None),
              patch.object(hub.Hub, "save_state", lambda self: None),
              patch.object(hub, "log_event", lambda *args, **kwargs: None)):
            intake = hub.HUB.create_session(client(), "new-alpha", "Alpha·实现")
            seated = hub.HUB.create_session(client(), "seat-beta", "临时名字")
            intake_scope = hub.Api()._team_scope(intake)
            seat_scope = hub.Api()._team_scope(seated)

        self.assertEqual(WS2, intake.cwd)
        self.assertEqual(hub.norm_root(WS1), hub.norm_root(intake.task_root))
        self.assertEqual("alpha", intake_scope["project"])
        self.assertEqual(WS2, seated.cwd)
        self.assertEqual(hub.norm_root(WS1), hub.norm_root(seated.task_root))
        self.assertEqual("beta", seat_scope["project"])

    def test_team_sessions_filters_by_registered_root_and_project(self):
        cfg = self._cfg()
        api = hub.Api()
        alpha = self._agent("a1", "ca1", "Beta")
        beta = self._agent("b1", "cb1", "Alpha")
        other_root = self._agent("o1", "co1", "Alpha")
        other_root.task_root = other_root.cwd = WS2
        cfg["team_project_members"] = {
            "ca1": {"root": hub.norm_root(WS1), "project": "alpha", "name": "Alpha"},
            "cb1": {"root": hub.norm_root(WS1), "project": "beta", "name": "Beta"},
            "co1": {"root": hub.norm_root(WS2), "project": "alpha", "name": "Alpha"},
        }
        sessions = {x.id: x for x in (alpha, beta, other_root)}
        with (patch.object(hub.HUB, "cfg", cfg),
              patch.object(hub.HUB, "sessions", sessions),
              patch.object(hub.HUB, "order", list(sessions))):
            # 08-21：命名项目按项目名找队友，跨工作区也是一队；Beta 仍被隔开。
            self.assertEqual({"a1", "o1"},
                             {s.id for s in api._team_sessions(WS1, project="Alpha")})
            self.assertEqual(["b1"], [s.id for s in api._team_sessions(WS1, project="Beta")])

    def test_moving_task_root_keeps_an_explicit_member_in_the_same_project(self):
        cfg = self._cfg()
        api = hub.Api()
        member = self._agent("a1", "ca1", "Beta")
        member.rev = 0
        cfg["team_project_members"] = {
            "ca1": {"root": hub.norm_root(WS1), "project": "alpha", "name": "Alpha"},
        }
        with (patch.object(hub.HUB, "cfg", cfg),
              patch.object(hub.HUB, "sessions", {member.id: member}),
              patch.object(hub, "save_config", lambda c: None)):
            result = api.set_task_root(member.id, WS2)
            scope = api._team_scope(member)
        self.assertTrue(result["ok"])
        self.assertEqual(hub.norm_root(WS2), cfg["team_project_members"]["ca1"]["root"])
        self.assertEqual("alpha", scope["project"])

    def test_explicit_reanchoring_moves_a_fixed_seat_without_moving_board_history(self):
        cfg = self._cfg()
        api = hub.Api()
        seated = self._agent("a1", "seat-alpha", "Beta")
        seated.cwd = WS2
        seated.rev = 0
        old_root, new_root = hub.norm_root(WS1), hub.norm_root(WS2)
        cfg["team_project_members"] = {
            "seat-alpha": {"root": old_root, "project": "alpha", "name": "Alpha"},
        }
        cfg["team_projects"] = {old_root: {
            "alpha": {"name": "Alpha", "board": {"text": "旧公告", "updated_at": 1},
                      "seats": [{"id": "seat-alpha", "name": "Alpha 实现"}]},
        }}
        bulletin = {"_scopes": {old_root: {"alpha": [{"text": "旧黑板历史"}]}}}
        with (patch.object(hub.HUB, "cfg", cfg),
              patch.object(hub.HUB, "sessions", {seated.id: seated}),
              patch.object(hub.HUB, "bulletin", bulletin),
              patch.object(hub.HUB, "save_state", lambda: None),
              patch.object(hub.Hub, "_save_bulletin", lambda self: None),
              patch.object(hub, "save_config", lambda c: None),
              patch.object(hub, "log_event", lambda *args, **kwargs: None)):
            result = api.set_task_root(seated.id, WS2)
            scope = api._team_scope(seated)
            posted = api.board_from_agent(seated, "提交", "新根黑板条目")
            merged = api._bulletin_entries(WS2, "alpha")

        self.assertTrue(result["ok"])
        self.assertEqual(new_root, cfg["team_project_members"]["seat-alpha"]["root"])
        self.assertEqual([], cfg["team_projects"][old_root]["alpha"]["seats"])
        self.assertEqual(["seat-alpha"],
                         [seat["id"] for seat in cfg["team_projects"][new_root]["alpha"]["seats"]])
        self.assertEqual("旧公告", cfg["team_projects"][old_root]["alpha"]["board"]["text"])
        self.assertEqual({}, cfg["team_projects"][new_root]["alpha"]["board"])
        self.assertEqual(new_root, hub.norm_root(scope["root"]))
        self.assertEqual("alpha", scope["project"])
        import server
        self.assertEqual((new_root, "alpha"),
                         server._registered_board_scope(cfg, "seat-alpha"))
        self.assertTrue(posted["ok"])
        # 08-31 起命名项目写进跨工作区共用的项目流（_projects），旧根下的历史原样
        # 保留；项目视角新旧同览——迁根不再把黑板拦腰截成两段
        self.assertEqual(["旧黑板历史"],
                         [entry["text"] for entry in bulletin["_scopes"][old_root]["alpha"]])
        self.assertEqual(["新根黑板条目"],
                         [entry["text"] for entry in bulletin["_projects"]["alpha"]])
        self.assertEqual(["旧黑板历史", "新根黑板条目"],
                         [entry["text"] for entry in merged])

    def test_concurrent_first_bucket_creation_keeps_every_project(self):
        cfg = self._cfg()
        api = hub.Api()
        gate = threading.Barrier(3)
        errors = []

        def create(project):
            try:
                gate.wait()
                api._project_bucket(WS1, project, create=True, name=project)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        with patch.object(hub.HUB, "cfg", cfg):
            threads = [threading.Thread(target=create, args=(project,))
                       for project in ("Alpha", "Beta")]
            for t in threads:
                t.start()
            gate.wait()
            for t in threads:
                t.join()
        self.assertEqual([], errors)
        self.assertEqual({"alpha", "beta"},
                         set(cfg["team_projects"][hub.norm_root(WS1)]))

    def test_concurrent_seat_add_keeps_both_rows(self):
        cfg = self._cfg()
        api = hub.Api()
        start = threading.Barrier(3)
        first_uuid = threading.Event()
        second_uuid = threading.Event()
        uuid_lock = threading.Lock()
        calls = {"n": 0}
        errors = []

        class FakeUuid:
            def __init__(self, value):
                self.hex = "{:08x}".format(value)

        def gated_uuid():
            with uuid_lock:
                calls["n"] += 1
                value = calls["n"]
            if value == 1:
                first_uuid.set()
                # 修复前第二个请求能在这里并发进入，确保两边都读到了旧 seats。
                # 修复后外层 RLock 会让它等到第一笔完整写完，超时后再继续即可。
                second_uuid.wait(0.3)
            else:
                second_uuid.set()
            return FakeUuid(value)

        def add(name):
            try:
                start.wait()
                api.team_seat_add(WS1, name=name, project="Alpha")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        with (patch.object(hub.HUB, "cfg", cfg),
              patch.object(hub.uuid, "uuid4", gated_uuid),
              patch.object(hub, "save_config", lambda c: None)):
            threads = [threading.Thread(target=add, args=(name,))
                       for name in ("实现一", "实现二")]
            for thread in threads:
                thread.start()
            start.wait()
            for thread in threads:
                thread.join(2)
            seat_names = {x["name"] for x in api._seats(WS1, "Alpha")}

        self.assertTrue(first_uuid.is_set())
        self.assertTrue(second_uuid.is_set())
        self.assertEqual([], errors)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual({"实现一", "实现二"}, seat_names)

    def test_concurrent_seat_add_is_not_lost_by_remove_copy_on_write(self):
        cfg = self._cfg()
        api = hub.Api()
        remove_read = threading.Event()
        add_done = threading.Event()
        errors = []
        added = []

        class GatedSeats(list):
            def __iter__(self):
                if (threading.current_thread().name == "seat-remove"
                        and not remove_read.is_set()):
                    remove_read.set()
                    # 修复前 add 能越过无锁的 remove，在这里完成；修复后它会等同一把
                    # RLock，remove 超时后先落盘，再由 add 基于新状态追加。
                    add_done.wait(0.3)
                return super().__iter__()

        cfg["team_projects"] = {hub.norm_root(WS1): {
            "alpha": {"name": "Alpha", "board": {},
                      "seats": GatedSeats([{"id": "old-seat", "name": "旧席位"}])},
        }}

        def remove_old():
            try:
                api.team_seat_remove("old-seat")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def add_new():
            try:
                added.append(api.team_seat_add(WS1, name="新席位", project="Alpha"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                add_done.set()

        with (patch.object(hub.HUB, "cfg", cfg),
              patch.object(hub, "save_config", lambda c: None)):
            remover = threading.Thread(target=remove_old, name="seat-remove")
            remover.start()
            self.assertTrue(remove_read.wait(1), "remove 未进入旧席位列表读取点")
            adder = threading.Thread(target=add_new, name="seat-add")
            adder.start()
            remover.join(2)
            adder.join(2)
            rows = api._seats(WS1, "Alpha")

        self.assertEqual([], errors)
        self.assertFalse(remover.is_alive())
        self.assertFalse(adder.is_alive())
        self.assertEqual(1, len(added))
        self.assertTrue(added[0]["ok"])
        self.assertEqual(["新席位"], [row["name"] for row in rows])

    def test_concurrent_board_save_is_not_rolled_back_by_seat_remove(self):
        cfg = self._cfg()
        api = hub.Api()
        remove_read = threading.Event()
        board_done = threading.Event()
        errors = []

        class GatedSeats(list):
            def __iter__(self):
                if (threading.current_thread().name == "seat-remove"
                        and not remove_read.is_set()):
                    remove_read.set()
                    board_done.wait(0.3)
                return super().__iter__()

        cfg["team_projects"] = {hub.norm_root(WS1): {
            "alpha": {"name": "Alpha", "board": {"text": "旧公告", "updated_at": 1},
                      "seats": GatedSeats([{"id": "old-seat", "name": "旧席位"}])},
        }}

        def remove_old():
            try:
                api.team_seat_remove("old-seat")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def save_board():
            try:
                api.team_set_board(WS1, "新公告", project="Alpha")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                board_done.set()

        with (patch.object(hub.HUB, "cfg", cfg),
              patch.object(hub, "save_config", lambda c: None)):
            remover = threading.Thread(target=remove_old, name="seat-remove")
            remover.start()
            self.assertTrue(remove_read.wait(1), "remove 未进入旧项目桶读取点")
            writer = threading.Thread(target=save_board, name="board-save")
            writer.start()
            remover.join(2)
            writer.join(2)
            board = api._team_board(WS1, "Alpha")

        self.assertEqual([], errors)
        self.assertFalse(remover.is_alive())
        self.assertFalse(writer.is_alive())
        self.assertEqual("新公告", board["text"])

    def test_seated_agent_role_assign_and_track_update_the_seat_prompt_data(self):
        cfg = self._cfg()
        api = hub.Api()
        with (patch.object(hub.HUB, "cfg", cfg),
              patch.object(hub, "save_config", lambda c: None)):
            seat = api.team_seat_add(
                WS1, name="实现位", role="impl", assign="旧分工",
                track="旧业务线", project="Alpha")["seat"]
            agent = self._agent("s1", seat["id"], "Alpha")
            with patch.object(hub.HUB, "sessions", {agent.id: agent}):
                self.assertTrue(api.team_set_role(agent.id, "review")["ok"])
                self.assertTrue(api.team_set_assign(agent.id, "终审")["ok"])
                self.assertTrue(api.team_set_track(agent.id, "发布线")["ok"])

        self.assertEqual("review", seat["role"])
        self.assertEqual("终审", seat["assign"])
        self.assertEqual("发布线", seat["track"])
        self.assertEqual("review", cfg["team_roles"][seat["id"]])
        self.assertEqual("终审", cfg["team_assign"][seat["id"]])
        self.assertEqual("发布线", cfg["team_tracks"][seat["id"]])

    def test_automatic_assignment_is_inherited_then_cleared_when_agent_self_names(self):
        cfg = self._cfg()
        api = hub.Api()
        with (patch.object(hub.HUB, "cfg", cfg),
              patch.object(hub, "save_config", lambda c: None),
              patch.object(hub, "log_event", lambda *a, **k: None)):
            seat = api.team_seat_add(WS1, name="实现位", project="Alpha")["seat"]
            agent = self._agent("s1", seat["id"], "Alpha")
            agent.name = "待命·cursor工作流"
            agent.name_locked = False
            agent.agent_named = False
            agent.name_history = []
            agent.rev = 0
            api._auto_label_on_dispatch(agent, "修 TeamScope 自动分工继承")
            inherited = seat["assign"]
            api._drop_auto_assign(agent)

        self.assertEqual("修 TeamScope 自动分工继承", inherited)
        self.assertEqual("", seat["assign"])
        self.assertNotIn(seat["id"], cfg["team_assign"])
        self.assertNotIn(seat["id"], cfg.get("team_assign_auto") or {})

    def test_sessions_broadcast_and_blackboard_are_scoped_by_project(self):
        cfg = self._cfg()
        alpha_sender = self._agent("a1", "ca1", "Alpha")
        alpha_mate = self._agent("a2", "ca2", "Alpha")
        beta = self._agent("b1", "cb1", "Beta")
        sent = []

        def fake_queue(_api, sid, text, imgs, **_kw):
            sent.append(sid)
            return {"ok": True}

        sessions = {x.id: x for x in (alpha_sender, alpha_mate, beta)}
        bulletin = {}
        with (patch.object(hub.HUB, "cfg", cfg),
              patch.object(hub.HUB, "sessions", sessions),
              patch.object(hub.HUB, "order", list(sessions)),
              patch.object(hub.HUB, "bulletin", bulletin),
              patch.object(hub.Hub, "_save_bulletin", lambda self: None),
              patch.object(hub.Api, "queue_message", fake_queue),
              patch.object(hub.Api, "session_label", lambda self, s: s.name),
              patch.object(hub, "log_event", lambda *args, **kwargs: None)):
            self.assertEqual({"a1", "a2"},
                             {s.id for s in hub.Api()._team_sessions(WS1, project="alpha")})
            result = hub.Api().team_broadcast(WS1, "Alpha 停手", project="Alpha",
                                               exclude_session_id="a1")
            self.assertTrue(result["ok"])
            self.assertEqual(["a2"], sent)
            post = hub.Api().board_from_agent(alpha_sender, "收工", "Alpha 已完成")
            self.assertTrue(post["ok"])
            alpha_rows = hub.Api()._bulletin_entries(WS1, "Alpha")
            beta_rows = hub.Api()._bulletin_entries(WS1, "Beta")
            self.assertEqual(["Alpha 已完成"], [x["text"] for x in alpha_rows])
            self.assertEqual([], beta_rows)

    def test_team_state_exposes_distinct_stable_scope_keys(self):
        alpha = self._agent("a1", "ca1", "Alpha")
        beta = self._agent("b1", "cb1", "Beta")
        cfg = self._cfg()
        live = {"state": "idle", "label": "", "sure": True, "evidence": [], "death": None}
        with (patch.object(hub.HUB, "cfg", cfg),
              patch.object(hub.HUB, "sessions", {"a1": alpha, "b1": beta}),
              patch.object(hub.HUB, "order", ["a1", "b1"]),
              patch.object(hub.HUB, "bulletin", {}),
              patch.object(hub.HUB, "relay_log", [
                  {"scope": hub.Api()._scope_key(WS1, "alpha"), "text": "alpha relay"},
                  {"scope": hub.Api()._scope_key(WS1, "beta"), "text": "beta relay"},
              ]),
              patch.object(hub.Api, "agentboard", lambda self: {"items": []}),
              patch.object(hub.Api, "_agent_liveness", lambda self, s, now: dict(live)),
              patch.object(hub.Api, "session_label", lambda self, s: s.name),
              patch.object(hub.Api, "_agent_desc", lambda self, s, hb: ""),
              patch.object(hub.HUB, "_window_tag", lambda *args: "")):
            rows = hub.Api().team_state()["projects"]
        by_project = {row["project"]: row for row in rows}
        self.assertEqual({"alpha", "beta"}, set(by_project))
        self.assertNotEqual(by_project["alpha"]["key"], by_project["beta"]["key"])
        self.assertEqual(by_project["alpha"]["key"], hub.Api()._scope_key(WS1, "alpha"))
        self.assertEqual(WS1, by_project["alpha"]["root"])
        self.assertEqual(["alpha relay"], [x["text"] for x in by_project["alpha"]["relays"]])
        self.assertEqual(["beta relay"], [x["text"] for x in by_project["beta"]["relays"]])

    def test_team_state_separates_legacy_relays_from_named_projects(self):
        alpha = self._agent("a1", "ca1", "Alpha")
        cfg = self._cfg()
        live = {"state": "idle", "label": "", "sure": True,
                "evidence": [], "death": None}
        with (patch.object(hub.HUB, "cfg", cfg),
              patch.object(hub.HUB, "sessions", {"a1": alpha}),
              patch.object(hub.HUB, "order", ["a1"]),
              patch.object(hub.HUB, "bulletin", {}),
              patch.object(hub.HUB, "relay_log", [
                  {"text": "升级前传话"},
                  {"scope": hub.Api()._scope_key(WS1, "alpha"),
                   "text": "alpha relay"},
              ]),
              patch.object(hub.Api, "agentboard", lambda self: {"items": []}),
              patch.object(hub.Api, "_agent_liveness", lambda self, s, now: dict(live)),
              patch.object(hub.Api, "session_label", lambda self, s: s.name),
              patch.object(hub.Api, "_agent_desc", lambda self, s, hb: ""),
              patch.object(hub.HUB, "_window_tag", lambda *args: "")):
            state = hub.Api().team_state()
        self.assertEqual(["升级前传话"], [x["text"] for x in state["legacy_relays"]])
        self.assertEqual(["alpha relay"],
                         [x["text"] for x in state["projects"][0]["relays"]])

    def test_legacy_board_without_sessions_still_has_a_default_project(self):
        cfg = self._cfg()
        cfg["team_boards"] = {hub.norm_root(WS1): {"text": "旧公告", "updated_at": 1}}
        with (patch.object(hub.HUB, "cfg", cfg),
              patch.object(hub.HUB, "sessions", {}),
              patch.object(hub.HUB, "order", []),
              patch.object(hub.HUB, "bulletin", {}),
              patch.object(hub.Api, "agentboard", lambda self: {"items": []})):
            rows = hub.Api().team_state()["projects"]
        self.assertEqual(1, len(rows))
        self.assertEqual(hub.Api().TEAM_DEFAULT_PROJECT, rows[0]["project"])
        self.assertEqual("旧公告", rows[0]["board"])
        self.assertFalse(rows[0].get("history"), "有公告的空组不能折进历史")


class ProjectFirstBulletinTests(unittest.TestCase):
    """0831 事故：一个项目横跨多个文件夹/工作区（agent 都按绝对路径干活），黑板
    却嵌在工作区根下——A 根写「部署」提醒得到 B 根的同项目队友，对方来读却读
    不到那条；未分项目读侧还会在自家桶空时吞下全部工作区历史（用户原话「写黑板
    会给所有该工作区的报告」）。命名项目自此项目优先共流；未分项目只看自己根。"""

    _CFG = {"team_projects": {}, "team_seats": {}, "team_boards": {},
            "team_project_members": {}, "team_tracks": {}, "team_roles": {},
            "team_assign": {}}

    def _ctx(self, sessions, bulletin, delivered):
        def fake_queue(api_self, sid, txt, imgs, who=None, files=None, force=False):
            delivered.append(sid)
            return {"ok": True, "qid": "q"}

        d = {x.id: x for x in sessions}
        return (patch.object(hub.HUB, "cfg", dict(self._CFG)),
                patch.object(hub.HUB, "sessions", d),
                patch.object(hub.HUB, "order", list(d)),
                patch.object(hub.HUB, "bulletin", bulletin),
                patch.object(hub.HUB, "_board_notify_ts", {}, create=True),
                patch.object(hub.Hub, "_save_bulletin", lambda self: None),
                patch.object(hub.Api, "queue_message", fake_queue),
                patch.object(hub.Api, "session_label", lambda self, s: s.name),
                patch.object(hub.Api, "_team_role", lambda self, s: ""),
                patch.object(hub.Api, "_team_assign", lambda self, s: ""),
                patch.object(hub.Api, "_team_track",
                             lambda self, s: getattr(s, "_track", "")),
                patch.object(hub, "log_event", lambda *args, **kwargs: None),
                patch.object(hub, "WORKFLOW", None))

    def test_0831_same_project_across_roots_shares_one_stream(self):
        a = _s("s1", "aaaa1111", "甲", WS1)
        b = _s("s2", "bbbb2222", "乙", WS2)
        a.agent_project = b.agent_project = "Alpha"
        delivered, bulletin = [], {}
        with ExitStack() as stack:
            for p in self._ctx([a, b], bulletin, delivered):
                stack.enter_context(p)
            r = hub.Api().board_from_agent(a, "部署", "Alpha 网关要重启")
            rows_b = hub.Api()._bulletin_entries(WS2, "Alpha")
            rows_a = hub.Api()._bulletin_entries(WS1, "Alpha")
        self.assertTrue(r["ok"])
        self.assertEqual(["s2"], delivered, "同项目跨工作区的队友要收到提醒")
        self.assertEqual(["Alpha 网关要重启"], [x["text"] for x in rows_b],
                         "被提醒的人读黑板必须读得到那条（0831 前嵌在根下读不到）")
        self.assertEqual([x["text"] for x in rows_a], [x["text"] for x in rows_b],
                         "两个工作区看到的是同一条项目事件流")
        self.assertNotIn(hub.norm_root(WS1), bulletin, "命名项目不再落工作区桶")
        entry = bulletin["_projects"]["alpha"][0]
        self.assertEqual(hub.norm_root(WS1), entry["root"],
                         "共流后每条要自带来源仓，读的人才分得清在哪写的")

    def test_0831_legacy_scoped_history_still_visible_in_project_stream(self):
        a = _s("s1", "aaaa1111", "甲", WS2)
        a.agent_project = "Alpha"
        delivered = []
        bulletin = {"_scopes": {hub.norm_root(WS1): {
            "alpha": [{"ts": 1, "text": "迁根前的旧事件", "from8": "bbbb2222"}]}}}
        with ExitStack() as stack:
            for p in self._ctx([a], bulletin, delivered):
                stack.enter_context(p)
            r = hub.Api().board_from_agent(a, "提交", "迁根后的新事件")
            rows = hub.Api()._bulletin_entries(WS2, "Alpha")
        self.assertTrue(r["ok"])
        self.assertEqual(["迁根前的旧事件", "迁根后的新事件"],
                         [x["text"] for x in rows],
                         "旧根下的历史条目要并进项目流，新旧同览")

    def test_0831_workspace_bucket_stays_isolated_per_root(self):
        a = _s("s1", "aaaa1111", "甲", WS1)   # 未分项目
        delivered, bulletin = [], {}
        with ExitStack() as stack:
            for p in self._ctx([a], bulletin, delivered):
                stack.enter_context(p)
            r = hub.Api().board_from_agent(a, "提交", "未分项目的一条")
            rows_own = hub.Api()._bulletin_entries(
                WS1, hub.Api().TEAM_DEFAULT_PROJECT)
            rows_other = hub.Api()._bulletin_entries(
                WS2, hub.Api().TEAM_DEFAULT_PROJECT)
        self.assertTrue(r["ok"])
        self.assertEqual(["未分项目的一条"], [x["text"] for x in rows_own])
        self.assertEqual([], rows_other, "未分项目仍按工作区根隔离，不跨根混流")


class ServerBoardReadProjectFirstTests(unittest.TestCase):
    """0831 事故（agent 直读侧）：ji(action=黑板) 空读走 server._board_read_text，
    命名项目要按项目取流（含嵌在各根下的历史），未分项目不得再兜底吞全库。"""

    def _write_board(self, data_dir, board):
        (data_dir / "board.json").write_text(
            json.dumps(board, ensure_ascii=False), encoding="utf-8")

    def test_0831_server_read_follows_project_stream_across_roots(self):
        import server
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            self._write_board(data_dir, {
                "_projects": {"alpha": [
                    {"ts": 2, "text": "项目流新条目", "from8": "aaaa1111",
                     "kind": "部署", "day": "08-31", "hms": "12:00:00",
                     "root": hub.norm_root(WS1)},
                ]},
                "_scopes": {hub.norm_root(WS1): {"alpha": [
                    {"ts": 1, "text": "嵌在别的根下的旧条目", "from8": "bbbb2222",
                     "kind": "大改", "day": "08-30", "hms": "09:00:00"},
                ]}},
                hub.norm_root(WS1): [
                    {"ts": 3, "text": "工作区桶别的摊子", "from8": "cccc3333",
                     "kind": "提交", "day": "08-31", "hms": "13:00:00"},
                ],
            })
            with patch.object(server, "DATA_DIR", data_dir):
                text = server._board_read_text(project_path=WS2,
                                               task_name="Alpha·收尾")
        self.assertIn("项目流新条目", text)
        self.assertIn("嵌在别的根下的旧条目", text,
                      "历史上嵌在各根 _scopes 里的同项目条目也要并进来")
        self.assertNotIn("工作区桶别的摊子", text, "命名项目不吃工作区桶")

    def test_0831_workspace_read_no_longer_swallows_everything(self):
        import server
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            self._write_board(data_dir, {
                hub.norm_root(WS1): [
                    {"ts": 1, "text": "别的工作区的事", "from8": "x",
                     "kind": "提交", "day": "08-30", "hms": "10:00:00"},
                ],
            })
            with patch.object(server, "DATA_DIR", data_dir):
                text = server._board_read_text(project_path=WS2, task_name="待命")
        self.assertNotIn("别的工作区的事", text,
                         "0831 前自家桶一空就兜底吞下全库历史，正是全工作区混流的读侧")
        self.assertIn("团队黑板还是空的", text)


class TeamWorkspaceProjectTests(unittest.TestCase):
    """08-21：团队按业务项目划分；工作区看 cwd；空死组折进历史。"""

    def _cfg(self):
        return {"team_projects": {}, "team_seats": {}, "team_boards": {},
                "team_project_members": {}, "team_tracks": {}, "team_roles": {},
                "team_assign": {}}

    def _agent(self, sid, conv, name, cwd, task_root=None, project=""):
        s = _s(sid, conv, name, cwd)
        s.cwd = cwd
        s.task_root = task_root if task_root is not None else cwd
        s.agent_project = project
        s.pid = 0
        return s

    def _state(self, sessions, cfg=None, live=None):
        cfg = cfg or self._cfg()
        live = live or {"state": "working", "label": "干活中", "sure": True,
                        "evidence": [], "death": None}
        with (patch.object(hub.HUB, "cfg", cfg),
              patch.object(hub.HUB, "sessions", {x.id: x for x in sessions}),
              patch.object(hub.HUB, "order", [x.id for x in sessions]),
              patch.object(hub.HUB, "bulletin", {}),
              patch.object(hub.HUB, "relay_log", []),
              patch.object(hub.Api, "agentboard", lambda self: {"items": []}),
              patch.object(hub.Api, "_agent_liveness", lambda self, s, now: dict(live)),
              patch.object(hub.Api, "session_label", lambda self, s: s.name),
              patch.object(hub.HUB, "_window_tag", lambda *args: "")):
            return hub.Api().team_state()

    def test_infer_role_from_psychology_tab_names(self):
        api = hub.Api()
        self.assertEqual("review", api._infer_role_from_name("心理·平台审页"))
        self.assertEqual("impl", api._infer_role_from_name("心理·平台剩余页"))
        self.assertEqual("", api._infer_role_from_name("待命·cursor工作流"))
        self.assertEqual("", api._infer_role_from_name("音频快编·TTS配额404"))

    def test_workspace_is_cwd_not_task_root(self):
        a = self._agent("a1", "31f6843c", "心理·平台剩余页", WS2, task_root=WS1,
                        project="心理")
        row = next(p for p in self._state([a])["projects"] if p["project"] == "心理")
        agent = row["agents"][0]
        self.assertEqual(Path(WS2).name, agent["workspace"])
        self.assertEqual(WS2, agent["workspace_path"])
        self.assertEqual(WS2, agent["affiliation"])
        self.assertFalse(agent["crossed"])
        self.assertFalse(agent["task_root_locked"])
        self.assertEqual([Path(WS2).name], [w["name"] for w in row["workspaces"]])
        self.assertEqual(Path(WS2).name, row["workspace_name"])

    def test_locked_affiliation_stays_on_task_root(self):
        a = self._agent("a1", "31f6843c", "心理·平台剩余页", WS2, task_root=WS1,
                        project="心理")
        a.task_root_locked = True
        agent = next(p for p in self._state([a])["projects"]
                     if p["project"] == "心理")["agents"][0]
        self.assertEqual(WS1, agent["affiliation"])
        self.assertTrue(agent["crossed"])
        self.assertTrue(agent["task_root_locked"])

    def test_same_named_project_across_roots_is_one_team(self):
        impl = self._agent("a1", "31f6843c", "心理·平台剩余页", WS2, task_root=WS1,
                           project="心理")
        review = self._agent("b1", "26554481", "心理·平台审页", WS2, task_root=WS2,
                             project="心理")
        cfg = self._cfg()
        live = {"state": "working", "label": "干活中", "sure": True,
                "evidence": [], "death": None}
        with (patch.object(hub.HUB, "cfg", cfg),
              patch.object(hub.HUB, "sessions", {"a1": impl, "b1": review}),
              patch.object(hub.HUB, "order", ["a1", "b1"]),
              patch.object(hub.HUB, "bulletin", {}),
              patch.object(hub.HUB, "relay_log", []),
              patch.object(hub.Api, "agentboard", lambda self: {"items": []}),
              patch.object(hub.Api, "_agent_liveness", lambda self, s, now: dict(live)),
              patch.object(hub.Api, "session_label", lambda self, s: s.name),
              patch.object(hub.HUB, "_window_tag", lambda *args: "")):
            rows = [p for p in hub.Api().team_state()["projects"] if p["project"] == "心理"]
            mates = hub.Api()._team_sessions(WS1, project="心理")
        self.assertEqual(1, len(rows), "同名项目跨工作区必须合成一队")
        self.assertEqual(2, len(rows[0]["agents"]))
        by_name = {a["name"]: a for a in rows[0]["agents"]}
        self.assertEqual("impl", by_name["心理·平台剩余页"]["role"])
        self.assertEqual("review", by_name["心理·平台审页"]["role"])
        self.assertEqual({"a1", "b1"}, {s.id for s in mates})

    def test_psychology_aliases_share_one_team(self):
        a = self._agent("a1", "c1", "心理评测·任务统筹", WS1, project="心理评测")
        b = self._agent("b1", "c2", "心健·回执后端", WS2, project="心健")
        c = self._agent("c1", "c3", "心评·专业服务端", WS2, project="心评")
        d = self._agent("d1", "c4", "心理·平台剩余页", WS2, project="心理")
        e = self._agent("e1", "c5", "心理引擎·推理", WS1, project="心理引擎")
        f = self._agent("f1", "c6", "心理后端·接口", WS2, project="心理后端")
        api = hub.Api()
        self.assertEqual(api._project_key("心理评测"), api._project_key("心理"))
        self.assertEqual(api._project_key("心理引擎"), api._project_key("心理"))
        self.assertEqual(api._project_key("心理后端"), api._project_key("心理"))
        self.assertEqual("心理", api._display_project_name("心健"))
        self.assertEqual("心理", api._display_project_name("心理健康管理平台"))
        rows = [p for p in self._state([a, b, c, d, e, f])["projects"]
                if p["project"] == api._project_key("心理")]
        self.assertEqual(1, len(rows))
        self.assertEqual(6, len(rows[0]["agents"]))
        self.assertEqual("心理", rows[0]["name"])

    def test_named_psychology_beats_stale_broadcast_seat(self):
        a = self._agent("d1", "050f09d2", "心理评测·任务统筹", WS2,
                        task_root=WS1, project="心理评测")
        cfg = self._cfg()
        cfg["team_projects"] = {
            hub.norm_root(WS1): {
                "智慧云广播": {"name": "智慧云广播", "seats": [
                    {"id": "050f09d2", "name": "负责人", "role": "owner"},
                ], "board": {}},
            },
        }
        row = next(p for p in self._state([a], cfg=cfg)["projects"]
                   if p["project"] == "心理")
        self.assertEqual(1, len(row["agents"]))
        self.assertEqual("心理评测·任务统筹", row["agents"][0]["name"])

    def test_runtime_live_root_never_becomes_a_history_group(self):
        live = r"C:\Users\example\AppData\Local\rxyy-tools-community\live\rxyy_mcp"
        cfg = self._cfg()
        cfg["team_seats"] = {hub.norm_root(live): []}
        cfg["team_boards"] = {hub.norm_root(live): {"text": "", "updated_at": 0}}
        cfg["team_projects"] = {hub.norm_root(live): {
            "__workspace__": {"name": "rxyy MCP", "seats": [], "board": {}},
        }}
        with patch.object(hub, "save_config", lambda cfg: None):
            rows = self._state([], cfg=cfg)["projects"]
        self.assertFalse(any(hub.is_runtime_ws_path(p.get("root")) for p in rows))
        self.assertNotIn(hub.norm_root(live), cfg.get("team_seats") or {})

    def test_stale_recon_does_not_occupy_a_live_row(self):
        dead = self._agent("d1", "dead0001", "旧线·旧会话", WS1, project="旧线")
        dead.connected = False
        live = {"state": "recon", "label": "重连中·多半已终止", "sure": False,
                "evidence": [], "death": None}
        row = next(p for p in self._state([dead], live=live)["projects"]
                   if p["project"] == "旧线")
        self.assertTrue(row["history"])
        self.assertEqual([], row["agents"])
        self.assertEqual(1, row["ended"])

    def test_empty_history_shell_is_dropped(self):
        cfg = self._cfg()
        cfg["team_projects"] = {
            hub.norm_root(WS1): {
                "空壳": {"name": "空壳", "seats": [], "board": {}},
            },
        }
        rows = self._state([], cfg=cfg)["projects"]
        self.assertFalse(any(p.get("name") == "空壳" for p in rows))

    def test_empty_ended_project_is_marked_history(self):
        dead = self._agent("d1", "dead0001", "旧线·旧会话", WS1, project="旧线")
        dead.connected = False
        live = {"state": "dead", "label": "已终止", "sure": True,
                "evidence": [], "death": None}
        row = next(p for p in self._state([dead], live=live)["projects"]
                   if p["project"] == "旧线")
        self.assertTrue(row["history"])
        self.assertEqual([], row["agents"])
        self.assertEqual(1, row["ended"])


class ConversationGoneTests(unittest.TestCase):
    """对话被删/被政策回收：composerData 没了、一条报错不留，也要判得出来。"""

    def _probe(self, verified=True, exists=False, err=None):
        s = _s("s1", "aaaa1111", "甲", WS1, connected=False)
        s.cursor_uuid = "u-123"
        s.uuid_verified = verified
        s.death_probe_at = 0
        s.death_info = None
        s.agent_status_ts = 0
        s.processing_since = None
        alerts = []
        with (patch.object(hub, "read_cursor_error", lambda uid: err),
              patch.object(hub, "cursor_conversation_exists", lambda uid: exists),
              patch.object(hub.Hub, "alert_session_death",
                           lambda self, x, dead: alerts.append(dead))):
            hub.HUB._tick_death_probe(s, time.time())
        return s, alerts

    def test_vanished_conversation_is_declared_dead(self):
        s, alerts = self._probe(verified=True, exists=False)
        self.assertIsNotNone(s.death_info)
        self.assertEqual("gone", s.death_info["code"])
        self.assertIn("消失", s.death_info["reason"])
        self.assertEqual(1, len(alerts))

    def test_unverified_uuid_never_judged_gone(self):
        # 猜来的 uid 查不到键不能算数——宁可不说话也不误判
        s, alerts = self._probe(verified=False, exists=False)
        self.assertIsNone(s.death_info)
        self.assertEqual([], alerts)

    def test_existing_conversation_stays_alive(self):
        s, alerts = self._probe(verified=True, exists=True)
        self.assertIsNone(s.death_info)
        self.assertEqual([], alerts)

    def test_death_pokes_workflow_engine(self):
        # 死的是某条工作流当前步的执行人 → 判死那一刻就通知引擎标卡，
        # 别让链干等 45 分钟超时
        calls = []

        class FakeWf:
            def on_executor_dead(self, conv, reason):
                calls.append((conv, reason))
                return "工作流「x」第1步执行人挂了，已标卡"

        with patch.object(hub, "WORKFLOW", FakeWf()):
            s, alerts = self._probe(verified=True, exists=False)
        self.assertEqual(1, len(alerts))
        self.assertEqual(1, len(calls))
        self.assertEqual("aaaa1111", calls[0][0])
        self.assertIn("消失", calls[0][1])

    def test_workflow_poke_failure_never_blocks_death_alert(self):
        class BoomWf:
            def on_executor_dead(self, conv, reason):
                raise RuntimeError("引擎坏了")

        with patch.object(hub, "WORKFLOW", BoomWf()):
            s, alerts = self._probe(verified=True, exists=False)
        self.assertEqual(1, len(alerts), "联动炸了也不能吞掉死亡提醒本身")
        self.assertIsNotNone(s.death_info)


if __name__ == "__main__":
    unittest.main()
