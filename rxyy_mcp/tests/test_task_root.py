# -*- coding: utf-8 -*-
"""任务归属项目 vs agent 实际工作区（跨工作区接手不许串台）"""
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub

WS1 = r"d:\Desktop\cursor工作流"
WS2 = r"c:\Users\Administrator\AICodebrain"


def _session(sid, conv, task_root, cwd, name="tab"):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = sid, conv, name
    s.task_root, s.cwd = task_root, cwd
    s.task_root_locked = False
    s.connected, s.lock = True, threading.Lock()
    s.agent_activity = ""
    return s


class TaskRootTests(unittest.TestCase):
    def test_falls_back_to_cwd_when_never_set(self):
        self.assertEqual(WS1, hub.task_root_of(SimpleNamespace(cwd=WS1)))
        self.assertEqual("", hub.task_root_of(SimpleNamespace()))

    def test_case_and_slash_differences_are_not_a_cross_workspace(self):
        s = _session("a", "c1", WS1, WS1.upper().replace("\\", "/"))
        self.assertFalse(hub.cross_workspace(s))

    def test_takeover_from_another_workspace_is_flagged(self):
        s = _session("a", "c1", WS1, WS2)
        s.task_root_locked = True
        self.assertTrue(hub.cross_workspace(s))
        self.assertEqual(WS1, hub.affiliation_of(s))

    def test_unlocked_cwd_mismatch_follows_cwd_and_is_not_flagged(self):
        s = _session("a", "c1", WS1, WS2)
        self.assertFalse(hub.cross_workspace(s))
        self.assertEqual(WS2, hub.affiliation_of(s))

    def test_unknown_cwd_is_not_flagged(self):
        self.assertFalse(hub.cross_workspace(_session("a", "c1", WS1, "")))


class TeamGroupingTests(unittest.TestCase):
    def test_grouping_follows_the_task_not_the_takeover_agent(self):
        # 工作区2 的闲置 agent 接手了工作区1 的活：它仍要算工作区1 的队员
        crossed = _session("s1", "c1", WS1, WS2)
        native = _session("s2", "c2", WS1, WS1)
        stranger = _session("s3", "c3", WS2, WS2)
        with (patch.object(hub.HUB, "sessions",
                           {"s1": crossed, "s2": native, "s3": stranger}),
              patch.object(hub.HUB, "order", ["s1", "s2", "s3"])):
            mates = hub.Api()._team_sessions(WS1)
            others = hub.Api()._team_sessions(WS2)
        self.assertEqual({"s1", "s2"}, {x.id for x in mates})
        self.assertEqual({"s3"}, {x.id for x in others})

    def test_review_targets_are_the_task_project_not_the_agent_workspace(self):
        crossed = _session("s1", "c1", WS1, WS2, name="实现")
        reviewer = _session("s2", "c2", WS1, WS1, name="审查")
        api = hub.Api()
        with (patch.object(hub.HUB, "sessions", {"s1": crossed, "s2": reviewer}),
              patch.object(hub.HUB, "order", ["s1", "s2"]),
              patch.dict(hub.HUB.cfg, {"team_roles": {"c1": "impl", "c2": "review"}}),
              patch.object(hub.Api, "_git_change_digest", lambda self, root: "改了 a.py"),
              patch.object(hub.Api, "queue_message",
                           lambda self, sid, body, imgs, who="": {"ok": True, "body": body})):
            r = api.team_review_send("s1")
        self.assertTrue(r["ok"])
        self.assertEqual(["审查"], r["sent"])


class SetTaskRootTests(unittest.TestCase):
    def test_takeover_create_session_keeps_the_existing_task_root(self):
        original = hub.Client(None, WS1, 1)
        crossed = hub.Client(None, WS2, 2)
        s = hub.Session(original, "seat-a", "固定席位")
        s.peer_ip = crossed.peer_ip
        cfg = {"team_projects": {hub.norm_root(WS1): {
            "alpha": {"name": "Alpha", "board": {}, "seats": [{"id": "seat-a"}]},
        }}}
        with (patch.object(hub.HUB, "cfg", cfg),
              patch.object(hub.HUB, "sessions", {s.id: s}),
              patch.object(hub.HUB, "order", [s.id]),
              patch.object(hub.Hub, "_reap_handed_off_shell", lambda *args: None),
              patch.object(hub.Hub, "_migrate_window_tags", lambda *args: None),
              patch.object(hub.Hub, "maybe_apply_task_name", lambda *args: None),
              patch.object(hub.Hub, "yield_zhi", lambda *args, **kwargs: None),
              patch.object(hub, "log_event", lambda *args, **kwargs: None)):
            restored = hub.HUB.create_session(crossed, "seat-a", "接手")
        self.assertIs(s, restored)
        self.assertEqual(WS2, s.cwd)
        self.assertEqual(hub.norm_root(WS1), hub.norm_root(s.task_root))
        self.assertEqual(hub.norm_root(WS1),
                         next(iter(cfg["team_projects"])))

    def test_reanchoring_moves_the_session_to_its_current_workspace(self):
        s = _session("s1", "c1", WS1, WS2)
        s.rev = 1
        persisted_roots = []
        with (patch.object(hub.HUB, "sessions", {"s1": s}),
              patch.object(hub.HUB, "save_state",
                           side_effect=lambda: persisted_roots.append(s.task_root)) as save_state):
            r = hub.Api().set_task_root("s1", "")
        self.assertTrue(r["ok"])
        self.assertEqual(WS2, s.task_root)
        self.assertTrue(s.task_root_locked)
        self.assertFalse(hub.cross_workspace(s))
        save_state.assert_called_once_with()
        self.assertEqual([WS2], persisted_roots)

    def test_unknown_session_is_reported_not_crashed(self):
        with patch.object(hub.HUB, "sessions", {}):
            self.assertFalse(hub.Api().set_task_root("nope")["ok"])


class SnapshotTests(unittest.TestCase):
    def test_old_snapshots_without_task_root_do_not_look_crossed(self):
        s = hub.Session.__new__(hub.Session)
        d = {"conv_key": "c1", "cwd": WS1}
        s.conv_key = d.get("conv_key", "__default__")
        s.cwd = d.get("cwd", "")
        s.task_root = d.get("task_root") or s.cwd
        s.task_root_locked = bool(d.get("task_root_locked"))
        self.assertEqual(WS1, hub.task_root_of(s))
        self.assertFalse(hub.cross_workspace(s))
        self.assertEqual(WS1, hub.affiliation_of(s))


if __name__ == "__main__":
    unittest.main()
