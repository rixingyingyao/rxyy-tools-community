# -*- coding: utf-8 -*-
"""hub 重启不许把老死亡再推一遍手机（09-02 家机 08:41 一口气 46 条、公司机 12:38 26 条）。

死因牌不落快照（下一拍重查），于是 hub 每次重启，`tick_death_probe` 第一拍都把
所有僵尸会话的死亡当「新出现」重走一遍提醒分支；唯一挡着的是 24h 冷却。冷却一过
——通常就是次日开机那一次重启——几十个从没归档的死会话齐刷刷再推一轮。
09-02 实测：家机 4 次重启各重判 29~32 个死亡，08:41 那次冷却已过 → 46 条推送；
公司机 3 次重启各重判 13 个，09:48 / 11:22 全被冷却压掉，12:38 冷却到期 → 26 条。

契约：处理过（响过、或被冷却压掉）的死亡气泡随快照落盘，跨重启永不二次提醒；
真正的新气泡（僵尸窗口被戳出新报错 / 换绑后的新对话）照旧走 24h 冷却。
"""
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub  # noqa: E402
import session_core  # noqa: E402

QUOTA = {"bubble_id": "b-quota", "reason": "额度用尽", "code": "quota",
         "at_ts": 1, "is_last": True}


class _Host:
    def __init__(self):
        self.DEATH_PROBE_EVERY = 0
        self.alerted = []

    def _fusion_recent_activity(self, s, now):
        return ""

    def _rescue_swallowed_reply(self, s, why="", note=""):
        pass

    def alert_session_death(self, s, dead):
        self.alerted.append(dead)


def _sess(uid="u-old"):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = "s1", "b4eff2ee", "云端·日编排报错"
    s.cursor_uuid = uid
    s.death_probe_uid = uid
    s.death_probe_at = 0.0
    s.death_info = None
    s.death_alerts = {}
    s.death_alerted_bubbles = {}
    s.uuid_verified = False
    s.agent_status_ts = 0
    s.processing_since = None
    s.last_zhi_ts = 0
    s.pending = None
    return s


def _restart(s):
    """模拟 hub 重启：只留快照里的东西，死因牌清空（真实路径就是这样）。"""
    snap = {"id": s.id, "name": s.name, "cwd": str(MODULE_DIR),
            "cursor_uuid": s.cursor_uuid,
            "death_alerts": dict(s.death_alerts),
            "death_alerted_bubbles": dict(getattr(s, "death_alerted_bubbles", {}) or {})}
    r = session_core.session_from_snapshot(hub.HUB, snap)
    r.uuid_verified = False
    r.agent_status_ts = 0
    r.processing_since = None
    r.last_zhi_ts = 0
    r.pending = None
    r.death_probe_at = 0.0
    return r


class RestartStormTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch.object(hub, "log_event", lambda *a, **k: None),
            patch.object(hub, "WORKFLOW", None),
            patch.object(session_core, "relocate_reincarnated", lambda *a, **k: None),
            patch.object(hub, "read_cursor_error", lambda uid: dict(QUOTA)),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(patch.stopall)

    def test_the_same_death_is_not_pushed_again_after_a_restart_even_when_the_cooldown_expired(self):
        host = _Host()
        s = _sess()
        session_core.tick_death_probe(host, s, 100.0)
        self.assertEqual(1, len(host.alerted), "第一次判死当然要响")
        # 一天多以后开机：冷却早过了，死因牌也没了——修前这里就是第二遍推送
        s.death_alerts = {k: v - 30 * 3600 for k, v in s.death_alerts.items()}
        r = _restart(s)
        session_core.tick_death_probe(host, r, 200.0)
        self.assertEqual(1, len(host.alerted), "同一次死亡跨重启不许再推手机")
        self.assertEqual("额度用尽", r.death_info["reason"], "死因牌本身照常重查出来")

    def test_a_death_that_the_cooldown_swallowed_stays_swallowed_across_restarts(self):
        # 冷却期内出现的新气泡没响过；它也算处理过，次日重启不许补响
        host = _Host()
        s = _sess()
        bubbles = iter(["b1", "b2"])
        with patch.object(hub, "read_cursor_error",
                          lambda uid: dict(QUOTA, bubble_id=next(bubbles))):
            session_core.tick_death_probe(host, s, 100.0)
            session_core.tick_death_probe(host, s, 101.0)
        self.assertEqual(1, len(host.alerted))
        s.death_alerts = {k: v - 30 * 3600 for k, v in s.death_alerts.items()}
        r = _restart(s)
        with patch.object(hub, "read_cursor_error", lambda uid: dict(QUOTA, bubble_id="b2")):
            session_core.tick_death_probe(host, r, 200.0)
        self.assertEqual(1, len(host.alerted))

    def test_a_genuinely_new_bubble_after_the_cooldown_still_rings(self):
        host = _Host()
        s = _sess()
        session_core.tick_death_probe(host, s, 100.0)
        s.death_alerts = {k: v - 30 * 3600 for k, v in s.death_alerts.items()}
        r = _restart(s)
        with patch.object(hub, "read_cursor_error",
                          lambda uid: dict(QUOTA, bubble_id="b-fresh")):
            session_core.tick_death_probe(host, r, 200.0)
        self.assertEqual(2, len(host.alerted), "新气泡 = 新死亡，冷却已过就该响")

    def test_the_bubble_belongs_to_the_dialog_so_a_rebind_rings_again(self):
        # 换绑后的新对话哪怕撞了同一个气泡号也是新死亡（对话 uid 进键）
        host = _Host()
        s = _sess()
        session_core.tick_death_probe(host, s, 100.0)
        s.cursor_uuid = "u-new"
        session_core.tick_death_probe(host, s, 101.0)
        self.assertEqual(2, len(host.alerted))

    def test_seen_bubbles_are_trimmed_to_a_week(self):
        host = _Host()
        s = _sess()
        s.death_alerted_bubbles = {"u-ancient:b0": time.time() - 8 * 24 * 3600}
        session_core.tick_death_probe(host, s, 100.0)
        self.assertEqual(["u-old:b-quota"], sorted(s.death_alerted_bubbles))

    def test_snapshot_round_trips_the_seen_bubbles(self):
        s = session_core.session_from_snapshot(
            hub.HUB, {"id": "s1", "name": "x", "cwd": str(MODULE_DIR),
                      "cursor_uuid": "u-old",
                      "death_alerted_bubbles": {"u-old:b-quota": 123.0}})
        self.assertEqual({"u-old:b-quota": 123.0}, s.death_alerted_bubbles)
        with patch.object(hub.HUB, "cfg", {"max_messages": 200}):
            s.lock = __import__("threading").Lock()
            snap = session_core.session_to_snapshot(hub.HUB, s)
        self.assertEqual({"u-old:b-quota": 123.0}, snap["death_alerted_bubbles"])


if __name__ == "__main__":
    unittest.main()
