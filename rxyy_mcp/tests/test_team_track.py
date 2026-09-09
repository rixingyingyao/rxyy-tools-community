# -*- coding: utf-8 -*-
"""业务线分组 + tab 自动命名 +「在干嘛」兜底 + 旧壳 ID 顺链转投。

08-04 用户实测的三件事：① 接手后队友手上那个报到 ID 已随空壳被收走，按它转告
扑空；② 同一个工作区（cursor工作流）下一个 agent 在做视频编辑、一个在做直播，
只按路径分组就混成一堆；③ 一排 tab 全叫「待命·cursor工作流N」，卡片上也看不出
谁在干什么（4 个 tab 只有 1 个说得出）。
"""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub

WS1 = r"d:\Desktop\cursor工作流"


def _s(sid="s1", conv="c1", name="待命·cursor工作流7", root=WS1, **kw):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = sid, conv, name
    s.cwd = s.task_root = root
    s.connected = kw.get("connected", True)
    s.pending = kw.get("pending")
    s.queued = []
    s.messages = kw.get("messages", [])
    s.agent_activity = kw.get("activity", "")
    s.id_history = kw.get("id_history", [])
    s.name_locked = kw.get("name_locked", False)
    s.recon_deadline = None
    s.rev = 0
    s.lock = threading.Lock()
    return s


class TracksOfTests(unittest.TestCase):
    def test_busier_track_comes_first(self):
        # 回归护栏：曾经拿 order.index 当排序键，而 list.sort 期间 CPython 会把列表
        # 临时清空 → ValueError，只要有人设过业务线，团队面板就整个打不开
        agents = [{"track": "直播"}, {"track": "视频编辑"}, {"track": "视频编辑"}]
        self.assertEqual([{"name": "视频编辑", "count": 2}, {"name": "直播", "count": 1}],
                         hub.Api._tracks_of(agents, []))

    def test_seat_reserved_track_is_listed_with_nobody_on_it(self):
        out = hub.Api._tracks_of([{"track": "直播"}], [{"track": "账号库"}])
        self.assertEqual(["直播", "账号库"], [x["name"] for x in out])
        self.assertEqual(0, out[-1]["count"])

    def test_agents_without_a_track_do_not_make_one(self):
        self.assertEqual([], hub.Api._tracks_of([{"track": ""}, {}], []))


class AgentDescTests(unittest.TestCase):
    """卡片上那行「这人此刻在干嘛」：自报 › 正等你回的提问 › 你最后派的活。"""

    def test_self_report_wins_while_the_heartbeat_is_alive(self):
        s = _s(activity="在改 hub.py 的业务线分组")
        self.assertEqual("在改 hub.py 的业务线分组", hub.Api()._agent_desc(s, True))

    def test_falls_back_to_the_question_it_is_waiting_on(self):
        s = _s(pending={"message": "三块改完都要重启 hub 生效，怎么干？"})
        d = hub.Api()._agent_desc(s, False)
        self.assertTrue(d.startswith("在等你回："), d)
        self.assertIn("重启 hub", d)

    def test_falls_back_to_the_last_task_you_dispatched(self):
        s = _s(messages=[{"role": "user", "html": "把<b>团队面板</b>三个问题一起改了"}])
        d = hub.Api()._agent_desc(s, False)
        self.assertTrue(d.startswith("最后派的活："), d)
        self.assertIn("团队面板", d)
        self.assertNotIn("<b>", d)

    def test_stale_self_report_is_not_passed_off_as_current(self):
        # 心跳都没了还拿它半小时前那句「在跑测试」当现状，是骗人
        s = _s(activity="在跑测试",
               messages=[{"role": "user", "html": "查一下账号库退款"}])
        self.assertEqual("最后派的活：查一下账号库退款", hub.Api()._agent_desc(s, False))

    def test_nothing_to_say_stays_empty(self):
        self.assertEqual("", hub.Api()._agent_desc(_s(), True))

    def test_a_teammates_relay_is_not_your_work(self):
        # 转告/广播也是 role=user 的气泡，混进来的话卡片会把别人递的话当成这人的活
        # （08-04 截图：团队面板修复那张卡写着「最后派的活：【agent 转告 · 来自…」）
        s = _s(messages=[
            {"role": "user", "html": "查一下账号库退款"},
            {"role": "user", "html": "【agent 转告 · 来自 甲】hub.py 我要动了",
             "who": "agent·甲"},
        ])
        self.assertEqual("最后派的活：查一下账号库退款", hub.Api()._agent_desc(s, False))

    def test_console_receipt_is_not_your_work(self):
        s = _s(messages=[
            {"role": "user", "html": "查一下账号库退款"},
            {"role": "user", "html": "【转告寄存】团队面板修复 目前已终止", "who": "控制台"},
        ])
        self.assertEqual("最后派的活：查一下账号库退款", hub.Api()._agent_desc(s, False))


class AutoLabelTests(unittest.TestCase):
    """派活/首次 zt 落地时，给「待命·xxx」这种壳名补上真任务名和分工。"""

    def _dispatch(self, s, text, assign="", who=None):
        cfg = {"team_assign": ({s.conv_key: assign} if assign else {}),
               "team_tracks": {}, "team_seats": {}}
        with (patch.object(hub.HUB, "sessions", {s.id: s}),
              patch.object(hub.HUB, "cfg", cfg),
              patch.object(hub, "save_config", lambda c: None),
              patch.object(hub, "log_event", lambda *a, **k: None)):
            hub.Api()._auto_label_on_dispatch(s, text, who=who)
        return (cfg.get("team_assign") or {}).get(s.conv_key, "")

    def test_shell_tab_is_named_after_the_work(self):
        s = _s(name="待命·cursor工作流7")
        assign = self._dispatch(s, "团队面板三个问题一起改：转告ID/业务线/命名")
        self.assertTrue(assign)
        self.assertNotIn("待命", s.name)
        self.assertEqual(assign, s.name)
        self.assertIn("待命·cursor工作流7", s.name_history)  # 旧名字留档，按旧名转告仍找得到

    def test_tab_you_renamed_yourself_is_left_alone(self):
        s = _s(name="团队面板修复", name_locked=True)
        self.assertEqual("", self._dispatch(s, "接着改 ui.html"))
        self.assertEqual("团队面板修复", s.name)

    def test_existing_assignment_is_never_overwritten(self):
        s = _s(name="待命·cursor工作流7")
        self.assertEqual("rxyy MCP 团队面板",
                         self._dispatch(s, "顺手看下这个报错", assign="rxyy MCP 团队面板"))
        self.assertEqual("待命·cursor工作流7", s.name)

    def test_a_name_that_already_says_something_is_kept(self):
        s = _s(name="视频编辑·转码档复用")
        self.assertEqual("", self._dispatch(s, "再看下另一个报错"))
        self.assertEqual("视频编辑·转码档复用", s.name)

    def test_a_teammates_relay_never_renames_the_target(self):
        # 转告投给「正等用户回话」的 tab 走的是 send_reply 而不是 queue_message，
        # 闸门只设在后者时，同一条转告就把人家的 tab 名和分工改成了转告正文
        s = _s(name="待命·cursor工作流7")
        self.assertEqual("", self._dispatch(
            s, "【agent 转告 · 来自 甲】hub.py 我要动了", who="agent·甲"))
        self.assertEqual("待命·cursor工作流7", s.name)

    def test_console_notice_never_renames_the_target(self):
        s = _s(name="待命·cursor工作流7")
        self.assertEqual("", self._dispatch(
            s, "【转告失败】找不到「27322a27」", who="控制台"))
        self.assertEqual("待命·cursor工作流7", s.name)


class _RelayHarness(unittest.TestCase):
    def _run(self, sender, to, msg, sessions, tracks=None):
        delivered = []

        def fake_queue(api_self, sid, text, imgs, who=None, files=None, force=False):
            delivered.append({"sid": sid, "text": text})
            return {"ok": True, "qid": "q"}

        d = {x.id: x for x in sessions}
        with (patch.object(hub.HUB, "sessions", d),
              patch.object(hub.HUB, "order", list(d)),
              patch.object(hub.HUB, "relay_log", []),
              patch.object(hub.HUB, "name_tombstones", {}),
              patch.dict(hub.HUB.cfg, {"team_tracks": tracks or {}}),
              patch.object(hub.Hub, "_save_relays", lambda self: None),
              patch.object(hub.Api, "queue_message", fake_queue),
              patch.object(hub.Api, "session_label", lambda self, s: s.name),
              patch.object(hub.Api, "_team_role", lambda self, s: ""),
              patch.object(hub.Api, "_team_assign", lambda self, s: "")):
            r = hub.Api().relay_from_agent(sender, to, msg)
        return r, delivered


class RelayOldIdTests(_RelayHarness):
    """接手落地后，队友手上多半还是接手方报到时那个已被收走的临时 ID。"""

    def test_old_shell_id_follows_the_chain_to_the_successor(self):
        a = _s(sid="s1", conv="aaaa1111", name="甲")
        succ = _s(sid="s2", conv="bbbb2222", name="乙", id_history=["cccc3333"])
        r, sent = self._run(a, "cccc3333", "styles.css 我要动了", [a, succ])
        self.assertTrue(r["ok"])
        self.assertIn("s2", [x["sid"] for x in sent])
        self.assertIn("已被接手", r["note"])
        back = [x["text"] for x in sent if x["sid"] == "s1"]
        # 发送方得知道现在该叫谁，否则它下次还用旧 ID
        self.assertTrue(back and "现在的 ID 是 bbbb2222" in back[0], back)

    def test_unknown_id_still_fails_out_loud(self):
        a = _s(sid="s1", conv="aaaa1111", name="甲")
        b = _s(sid="s2", conv="bbbb2222", name="乙")
        r, _ = self._run(a, "ffff9999", "在吗", [a, b])
        self.assertFalse(r["ok"])


class TrackBroadcastTests(_RelayHarness):
    """同工作区里另一摊活跟这事没关系，广播不该无差别打扰。"""

    def test_agent_broadcast_can_stay_inside_its_own_track(self):
        a = _s(sid="s1", conv="c1", name="视频甲")
        b = _s(sid="s2", conv="c2", name="视频乙")
        c = _s(sid="s3", conv="c3", name="直播丙")
        r, sent = self._run(a, "本组", "转码那块我要动 ffmpeg 参数", [a, b, c],
                            tracks={"c1": "视频编辑", "c2": "视频编辑", "c3": "直播"})
        self.assertTrue(r["ok"])
        self.assertEqual(["s2"], [x["sid"] for x in sent])   # 直播那位不该被打扰
        self.assertIn("ffmpeg 参数", sent[0]["text"])

    def test_broadcast_without_a_track_still_goes_to_everyone(self):
        a = _s(sid="s1", conv="c1", name="甲")
        b = _s(sid="s2", conv="c2", name="乙")
        r, sent = self._run(a, "本组", "都停手，我要 rebase", [a, b])
        self.assertTrue(r["ok"])
        self.assertEqual(["s2"], [x["sid"] for x in sent if x["sid"] != "s1"])
        # 没设业务线就按全项目发了，得回执告诉发送方一声，别以为只喊了自己组
        self.assertIn("你没设业务线", "".join(x["text"] for x in sent if x["sid"] == "s1"))


class PanelBroadcastTests(unittest.TestCase):
    """面板上那个「只发一条业务线」的下拉。"""

    def _cast(self, sessions, track, tracks):
        delivered = []

        def fake_queue(api_self, sid, text, imgs, who=None, files=None, force=False):
            delivered.append({"sid": sid, "text": text})
            return {"ok": True, "qid": "q"}

        d = {x.id: x for x in sessions}
        with (patch.object(hub.HUB, "sessions", d),
              patch.object(hub.HUB, "order", list(d)),
              patch.dict(hub.HUB.cfg, {"team_tracks": tracks}),
              patch.object(hub.Api, "queue_message", fake_queue),
              patch.object(hub.Api, "_team_role", lambda self, s: "")):
            r = hub.Api().team_broadcast(WS1, "都先停手", "", track)
        return r, delivered

    def test_only_that_track_gets_it(self):
        a = _s(sid="s1", conv="c1", name="视频甲")
        b = _s(sid="s2", conv="c2", name="直播乙")
        r, sent = self._cast([a, b], "视频编辑", {"c1": "视频编辑", "c2": "直播"})
        self.assertTrue(r["ok"])
        self.assertEqual(["s1"], [x["sid"] for x in sent])
        self.assertIn("【视频编辑广播】", sent[0]["text"])

    def test_empty_track_means_the_whole_project(self):
        a = _s(sid="s1", conv="c1", name="视频甲")
        b = _s(sid="s2", conv="c2", name="直播乙")
        r, sent = self._cast([a, b], "", {"c1": "视频编辑", "c2": "直播"})
        self.assertEqual({"s1", "s2"}, {x["sid"] for x in sent})
        self.assertIn("【项目广播】", sent[0]["text"])

    def test_nobody_on_that_track_says_so(self):
        a = _s(sid="s1", conv="c1", name="视频甲")
        r, sent = self._cast([a], "直播", {"c1": "视频编辑"})
        self.assertFalse(r["ok"])
        self.assertIn("直播", r["error"])
        self.assertEqual([], sent)


if __name__ == "__main__":
    unittest.main()
