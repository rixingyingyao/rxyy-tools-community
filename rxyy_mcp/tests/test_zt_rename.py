# -*- coding: utf-8 -*-
"""agent 在 zt 里报的名字要算数。

08-07 用户在团队面板上看到的还是一屏「我自己第一句话的前 24 个字」，可命名那套
机制其实是齐的（maybe_apply_task_name / agent_named / _drop_auto_assign 都在）
——断在一根线上：**zt 这条路根本不带 task_name**。

- MCP 常驻纪律写的是「接到真活后第一次 zhi/zt 就把 task_name 换成 项目·功能」；
- 但按同一份纪律，zhi 是**收尾**才调的，中间几十分钟只有 zt；
- 而 zt 的 inputSchema 压根没声明 task_name（agent 想传也没处传），
  tool_zt 不读它，report_status 不发它，hub 的 agent_status 分支不认它。

于是 agent 老老实实每步都报名字，面板上一个字没变，控制台还回头拿
_nudge_rename_if_standby 催它改名——催了也白催，这是个死循环。

本测试锁死这条线的四段：schema → tool_zt → report_status 报文 → hub 落到 tab 上。
"""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub  # noqa: E402
import server  # noqa: E402

WS = r"d:\Desktop\cursor工作流"


def _sess(sid="s1", conv="495a0f4c", name="待命·cursor工作流"):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = sid, conv, name
    s.cwd = s.task_root = WS
    s.name_locked = False
    s.shell_born = True
    s.agent_named = False
    s.agent_project = ""
    s.agent_status = ""
    s.agent_activity = ""
    s.agent_status_ts = 0
    s.connected = True
    s.pending = None
    s.queued = []
    s.messages = []
    s.msg_seq = 9
    s.rev = 0
    s.client = None
    s.file_path = None
    s.id_history = []
    s.name_history = []
    s.last_heartbeat = time.time()
    s.lock = threading.RLock()
    s.reported_model = ""
    s.model_info = None
    s.model_probe_at = 0.0
    s.model_probe_uid = None
    s.cursor_uuid = None
    return s


class _FakeClient:
    def __init__(self, sessions):
        self.sessions = {x.conv_key: x for x in sessions}
        self.closed_convs = {}
        self.last_heartbeat = 0.0
        self.cwd = WS


class ZtSchemaTests(unittest.TestCase):
    """schema 不声明 = agent 传不了。这一条是整条线的入口。"""

    def _tool(self, name):
        return next(t for t in server.TOOLS if t["name"] == name)

    def test_zt_accepts_a_task_name(self):
        props = self._tool("zt")["inputSchema"]["properties"]
        self.assertIn("task_name", props)
        self.assertIn("项目·功能", props["task_name"]["description"])

    def test_zhi_and_ji_still_accept_it_too(self):
        for name in ("zhi", "ji"):
            self.assertIn("task_name",
                          self._tool(name)["inputSchema"]["properties"], name)

    def test_zt_and_zhi_accept_a_model(self):
        for name in ("zt", "zhi"):
            props = self._tool(name)["inputSchema"]["properties"]
            self.assertIn("model", props, name)
            self.assertIn("Codex", props["model"]["description"])


class ZtWireTests(unittest.TestCase):
    """tool_zt / tool_ji(状态) → report_status → agent_status 报文。"""

    def setUp(self):
        self.calls = []
        self._p = patch.object(
            server.BRIDGE, "report_status",
            lambda cid, st, act, tn=None, **kw: self.calls.append(
                (cid, st, act, tn)) or [])
        self._p.start()
        self.addCleanup(self._p.stop)

    def test_zt_hands_the_name_over(self):
        server.tool_zt({"conversation_id": "495a0f4c", "status": "developing",
                        "activity": "改顶栏", "task_name": "rxyy MCP·团队面板"})
        self.assertEqual([("495a0f4c", "developing", "改顶栏", "rxyy MCP·团队面板")],
                         self.calls)

    def test_ji_borrowing_zts_door_hands_it_over_too(self):
        server.tool_ji({"action": "状态", "conversation_id": "495a0f4c",
                        "content": "developing:改顶栏", "task_name": "rxyy MCP·团队面板"})
        self.assertEqual("rxyy MCP·团队面板", self.calls[0][3])

    def test_no_name_is_still_a_perfectly_good_report(self):
        server.tool_zt({"conversation_id": "495a0f4c", "status": "testing"})
        self.assertEqual((None,), self.calls[0][3:])


class ReportStatusPayloadTests(unittest.TestCase):
    """报文里真带上了才算数——上面那层全绿也可能断在这儿。"""

    def _bridge(self):
        b = server.HubBridge.__new__(server.HubBridge)
        b.send_lock = threading.Lock()
        b._active_convs = set()
        b._conv_activity = {}
        return b

    def _send(self, **kw):
        sent = []
        with (patch.object(server.HubBridge, "_ensure_conversation_id",
                           lambda self, cid: cid),
              patch.object(server.HubBridge, "_ensure_heartbeat", lambda self: None),
              patch.object(server.HubBridge, "_reconnect_if_hub_up",
                           lambda self: object()),
              patch.object(server.HubBridge, "fetch_mail",
                           lambda self, cid, sock: []),
              patch.object(server, "send_msg",
                           lambda sock, payload: sent.append(payload))):
            self._bridge().report_status("495a0f4c", "developing", "改顶栏", **kw)
        return sent[0] if sent else None

    def test_the_name_rides_along(self):
        self.assertEqual("rxyy MCP·团队面板",
                         self._send(task_name="rxyy MCP·团队面板")["task_name"])

    def test_the_model_rides_along(self):
        self.assertEqual("gpt-5.6-sol",
                         self._send(model="gpt-5.6-sol")["model"])

    def test_blank_models_are_left_out_entirely(self):
        self.assertNotIn("model", self._send(model="   "))
        self.assertNotIn("model", self._send())

    def test_blank_names_are_left_out_entirely(self):
        # 带个空字符串过去会被 maybe_apply_task_name 当无事发生，但报文里多一个
        # 空字段只会让人以为「报了没生效」，干脆不发
        self.assertNotIn("task_name", self._send(task_name="   "))
        self.assertNotIn("task_name", self._send())

    def test_hub_not_up_is_silent_not_fatal(self):
        with (patch.object(server.HubBridge, "_ensure_conversation_id",
                           lambda self, cid: cid),
              patch.object(server.HubBridge, "_ensure_heartbeat", lambda self: None),
              patch.object(server.HubBridge, "_reconnect_if_hub_up",
                           lambda self: None)):
            self.assertEqual([], self._bridge().report_status(
                "c", "developing", "x", "甲·乙"))


class HubAppliesZtNameTests(unittest.TestCase):
    """最后一段：hub 收到带名字的 agent_status，tab 真的改名了。"""

    def setUp(self):
        self.s = _sess()
        self.nudged = []
        d = {self.s.id: self.s}
        patches = [
            patch.object(hub.HUB, "sessions", d),
            patch.object(hub.HUB, "order", list(d)),
            patch.object(hub.HUB, "cfg", {"max_messages": 200}),
            patch.object(hub.HUB, "name_tombstones", {}),
            patch.object(hub.Hub, "save_state", lambda self: None),
            patch.object(hub.Hub, "_verify_identity_by_generating", lambda self, s: None),
            patch.object(hub.Hub, "_reap_takeover_shell", lambda self, s: None),
            patch.object(hub.Hub, "notify_status_only", lambda self: None),
            patch.object(hub.Api, "_drop_auto_assign", lambda api, s: None),
            patch.object(hub.Api, "_nudge_rename_if_standby",
                         lambda api, s: self.nudged.append(s.name)),
            patch.object(hub, "log_event", lambda *a, **k: None),
            patch.object(hub, "save_config", lambda cfg: None),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

    def _zt(self, **extra):
        msg = {"type": "agent_status", "conversation_id": self.s.conv_key,
               "status": "developing", "activity": "改顶栏溢出"}
        msg.update(extra)
        hub.HUB._handle_client_msg(_FakeClient([self.s]), None, msg)

    def test_a_reported_name_renames_the_tab(self):
        self._zt(task_name="rxyy MCP·团队面板")
        self.assertEqual("rxyy MCP·团队面板", self.s.name)
        self.assertTrue(self.s.agent_named)

    def test_a_reported_model_is_kept(self):
        self._zt(model="gpt-5.6-sol")
        self.assertEqual("gpt-5.6-sol", self.s.reported_model)

    def test_the_project_half_is_picked_out_for_grouping(self):
        # 项目那半是团队分组与「只发本组」广播的依据，不能只当字符串存着
        self._zt(task_name="rxyy MCP·团队面板")
        self.assertEqual("rxyy MCP", self.s.agent_project)

    def test_the_status_itself_still_lands(self):
        self._zt(task_name="rxyy MCP·团队面板")
        self.assertEqual("developing", self.s.agent_status)
        self.assertEqual("改顶栏溢出", self.s.agent_activity)

    def test_a_report_without_a_name_leaves_the_tab_alone(self):
        self._zt()
        self.assertEqual("待命·cursor工作流", self.s.name)
        self.assertFalse(self.s.agent_named)

    def test_the_users_own_rename_still_wins(self):
        self.s.name_locked = True
        self.s.name = "我自己起的名"
        self._zt(task_name="rxyy MCP·团队面板")
        self.assertEqual("我自己起的名", self.s.name)

    def test_it_renames_before_the_activity_placeholder_can_grab_the_name(self):
        # 顺序错了就白改：_auto_label_on_dispatch 会拿这句 zt 活动去填名字/分工
        self._zt(task_name="rxyy MCP·团队面板", activity="正在排查顶栏横向溢出")
        self.assertEqual("rxyy MCP·团队面板", self.s.name)

    def test_it_renames_before_the_console_nags_it_to_rename(self):
        # 催改名的那条硬提醒看的是 agent_named：改名必须排在它前面，
        # 否则 agent 刚照做就又挨一句「你还没改名」
        self._zt(task_name="rxyy MCP·团队面板")
        self.assertEqual(["rxyy MCP·团队面板"], self.nudged)
        self.assertTrue(self.s.agent_named)


class ZtTrailFootprintTests(unittest.TestCase):
    """每条 zt 在会话上留一行轨迹，接手词靠它还原「前任断线前在做什么」。

    08-28 f9c313ed 实测：前任提交完就断、没走 zhi，agent_status 又只有
    最后一条且重启即清——聊天记录里零线索，接手方翻了七八处才对上进度。
    轨迹有界（5 条）、同内容去重（zt 很频繁，刷屏没信息量）、随快照持久化
    （持久化那半见 test_snapshot_resilience）。
    """

    def setUp(self):
        self.s = _sess()
        d = {self.s.id: self.s}
        patches = [
            patch.object(hub.HUB, "sessions", d),
            patch.object(hub.HUB, "order", list(d)),
            patch.object(hub.HUB, "cfg", {"max_messages": 200}),
            patch.object(hub.HUB, "name_tombstones", {}),
            patch.object(hub.Hub, "save_state", lambda self: None),
            patch.object(hub.Hub, "_verify_identity_by_generating", lambda self, s: None),
            patch.object(hub.Hub, "_reap_takeover_shell", lambda self, s: None),
            patch.object(hub.Hub, "notify_status_only", lambda self: None),
            patch.object(hub.Api, "_drop_auto_assign", lambda api, s: None),
            patch.object(hub.Api, "_nudge_rename_if_standby", lambda api, s: None),
            patch.object(hub, "log_event", lambda *a, **k: None),
            patch.object(hub, "save_config", lambda cfg: None),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

    def _zt(self, status="developing", activity="改顶栏溢出"):
        hub.HUB._handle_client_msg(_FakeClient([self.s]), None, {
            "type": "agent_status", "conversation_id": self.s.conv_key,
            "status": status, "activity": activity})

    def test_each_distinct_report_leaves_one_line(self):
        # _sess 用 __new__ 造，没有 zt_trail 属性——老快照恢复的会话同款，
        # 第一条 zt 就得把轨迹惰性建出来
        self.assertFalse(hasattr(self.s, "zt_trail"))
        self._zt(activity="读代码")
        self._zt(activity="改 hub.py")
        self.assertEqual(2, len(self.s.zt_trail))
        self.assertTrue(self.s.zt_trail[-1].endswith("developing · 改 hub.py"),
                        self.s.zt_trail)
        self.assertRegex(self.s.zt_trail[0], r"^\d{2}-\d{2} \d{2}:\d{2} ",
                         "没有时刻，接手方看不出前任断在哪一步")

    def test_the_same_report_repeated_lands_once(self):
        self._zt()
        self._zt()
        self._zt()
        self.assertEqual(1, len(self.s.zt_trail))

    def test_the_trail_never_grows_past_five(self):
        for i in range(7):
            self._zt(activity="第%d件事" % i)
        self.assertEqual(5, len(self.s.zt_trail))
        self.assertIn("第6件事", self.s.zt_trail[-1], "留的得是最新的五条")
        self.assertIn("第2件事", self.s.zt_trail[0])

    def test_a_status_only_report_still_leaves_a_line(self):
        self._zt(status="testing", activity="")
        self.assertEqual(1, len(self.s.zt_trail))
        self.assertTrue(self.s.zt_trail[0].endswith("testing"), self.s.zt_trail)


class RetireRoleOnProjectMoveTests(unittest.TestCase):
    """08-12 实测回归：上午在 rxyy tools 组当负责人，下午改名去 index-tts2，
    「负责人」徽章一路粘着——角色按对话 ID 记、归属跟 task_name 前缀走，两边
    没打通。修复契约：改名导致**生效项目归属**变化 = 离开原团队，角色/分工/
    业务线就地卸下；同项目改功能名、壳首次立名（面板预设的角色是给新团队的）、
    intake 显式钉住归属的成员统统不受影响。"""

    def _cfg(self, conv, extra=None):
        cfg = {"max_messages": 200,
               "team_roles": {conv: "owner"},
               "team_assign": {conv: "统筹团队功能"},
               "team_assign_auto": {},
               "team_tracks": {conv: "团队线"},
               "team_seats": {}, "team_projects": {},
               "team_project_members": {}}
        cfg.update(extra or {})
        return cfg

    def _run(self, s, new_name, cfg):
        with (patch.object(hub.HUB, "sessions", {s.id: s}),
              patch.object(hub.HUB, "cfg", cfg),
              patch.object(hub, "save_config", lambda c: None),
              patch.object(hub, "log_event", lambda *a, **k: None)):
            hub.HUB.maybe_apply_task_name(s, new_name)

    def test_moving_to_another_project_retires_the_role(self):
        s = _sess(name="rxyy tools·团队优化")
        s.agent_named, s.agent_project = True, "rxyy tools"
        cfg = self._cfg(s.conv_key)
        self._run(s, "index-tts2·2.5部署", cfg)
        self.assertNotIn(s.conv_key, cfg["team_roles"])
        self.assertNotIn(s.conv_key, cfg["team_assign"])
        self.assertNotIn(s.conv_key, cfg["team_tracks"])
        self.assertIn("卸下", s.messages[-1]["html"])

    def test_same_project_rename_keeps_the_role(self):
        s = _sess(name="rxyy tools·团队测试")
        s.agent_named, s.agent_project = True, "rxyy tools"
        cfg = self._cfg(s.conv_key)
        self._run(s, "rxyy tools·团队优化", cfg)
        self.assertEqual("owner", cfg["team_roles"].get(s.conv_key))
        self.assertEqual("统筹团队功能", cfg["team_assign"].get(s.conv_key))

    def test_first_naming_from_shell_keeps_a_preset_role(self):
        s = _sess(name="待命·cursor工作流")  # agent_named=False：还没进过团队
        cfg = self._cfg(s.conv_key)
        self._run(s, "rxyy tools·新活", cfg)
        self.assertEqual("owner", cfg["team_roles"].get(s.conv_key))

    def test_intake_pinned_member_keeps_the_role(self):
        # intake 显式登记的归属优先级高于自报项目：改名不改变生效归属，不卸任
        s = _sess(name="rxyy tools·团队优化")
        s.agent_named, s.agent_project = True, "rxyy tools"
        cfg = self._cfg(s.conv_key, extra={
            "team_project_members": {s.conv_key: {
                "root": hub.norm_root(WS), "project": "rxyy tools",
                "name": "rxyy tools"}}})
        self._run(s, "index-tts2·2.5部署", cfg)
        self.assertEqual("owner", cfg["team_roles"].get(s.conv_key))


if __name__ == "__main__":
    unittest.main()
