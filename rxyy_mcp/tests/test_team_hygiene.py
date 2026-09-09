# -*- coding: utf-8 -*-
"""团队功能的三处卫生：命名归 agent 自己、广播别吵待命壳、自报状态别忽然消失。

08-07 用户拍的三条（控制台截图为证：一屏 tab 全是他自己第一句话的前 24 字，
底下排着五个收不掉的「待命·cursor工作流-xxxx」）：
1. 会话命名要用「项目·功能」，而且得让 agent 自己说了算；
2. 接手之后那个待命壳该关掉，有的没关；
3. 转告/广播/黑板/状态整体不准。

这三条在代码里咬成一环——一条广播同时把三件事弄坏，所以放在同一份测试里：
广播投给待命壳 → 壳的队列非空 → 收壳判定的「无排队」永远不成立 → 壳收不掉。
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

WS = r"d:\桌面\working\cursor工作流"
UID = "cursorchat-uuid-1"


def _s(sid, conv, name, **kw):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = sid, conv, name
    s.cwd = s.task_root = kw.get("root", WS)
    s.cursor_uuid = kw.get("uuid", UID)
    s.msg_seq = kw.get("msg_seq", 2)
    s.pending = kw.get("pending")
    s.queued = kw.get("queued") or []
    s.messages = []
    s.connected = kw.get("connected", True)
    s.client = None
    s.rev = 0
    s.name_locked = False
    s.cursor_title = kw.get("cursor_title")
    s.agent_named = kw.get("agent_named", False)
    s.agent_project = kw.get("agent_project", "")
    s.agent_status = kw.get("agent_status", "")
    s.agent_activity = kw.get("agent_activity", "")
    s.agent_status_ts = kw.get("agent_status_ts", 0)
    s.file_path = None
    s.recon_deadline = None
    s.lock = threading.Lock()
    return s


def _mail(who, text="队友说了句话"):
    """机器替别人递的话：转告 / 黑板提醒 / 控制台回执。"""
    return {"id": "q1", "who": who, "text": text, "images": [], "files": []}


def _user_msg(text="顺手把这个也改了"):
    return {"id": "q2", "who": "", "text": text, "images": [], "files": []}


class ShellReapNotWedgedByTeamChatter(unittest.TestCase):
    """收壳判定不该被团队功能自己顶死。"""

    def _reap(self, revived, sessions):
        d = {x.id: x for x in sessions}
        with (patch.object(hub.HUB, "sessions", d),
              patch.object(hub.HUB, "order", [x.id for x in sessions]),
              patch.object(hub.HUB, "log_end", lambda s, why: None),
              patch.object(hub, "log_event", lambda *a, **k: None)):
            hub.HUB._reap_takeover_shell(revived)
        return d

    def test_broadcast_in_the_queue_no_longer_wedges_the_shell(self):
        # 这正是 08-07 现场：一条广播同时投给 4 个待命壳，它们从此永远收不掉
        shell = _s("s1", "newid", "待命·cursor工作流", queued=[_mail("agent·某某")])
        revived = _s("s2", "origid", "rxyy tools·换装收尾", msg_seq=45)
        with patch.object(hub.Api, "queue_message",
                          lambda self, sid, text, imgs=None, **kw: {"ok": True}):
            left = self._reap(revived, [shell, revived])
        self.assertNotIn("s1", left, "队列里只有队友转告，仍该判成空壳收掉")

    def test_board_reminder_and_console_receipt_also_do_not_wedge(self):
        for who in ("黑板", "控制台"):
            with self.subTest(who=who):
                shell = _s("s1", "newid", "待命·cursor工作流", queued=[_mail(who)])
                revived = _s("s2", "origid", "rxyy tools·换装收尾", msg_seq=45)
                with patch.object(hub.Api, "queue_message",
                                  lambda self, sid, text, imgs=None, **kw: {"ok": True}):
                    left = self._reap(revived, [shell, revived])
                self.assertNotIn("s1", left)

    def test_a_real_task_from_the_user_still_keeps_the_shell(self):
        # 安全边界：用户后来真在这个壳里派了活，收掉就丢消息
        shell = _s("s1", "newid", "待命·cursor工作流", queued=[_user_msg()])
        revived = _s("s2", "origid", "rxyy tools·换装收尾", msg_seq=45)
        left = self._reap(revived, [shell, revived])
        self.assertIn("s1", left)

    def test_mail_in_a_reaped_shell_is_handed_to_the_successor(self):
        # 宁可迟到不可蒸发：壳收掉了，压在里面的话得跟到现任身上
        shell = _s("s1", "newid", "待命·cursor工作流", queued=[_mail("agent·某某")])
        revived = _s("s2", "origid", "rxyy tools·换装收尾", msg_seq=45)
        forwarded = []

        def _q(self, sid, text, imgs=None, **kw):
            forwarded.append((sid, text))
            return {"ok": True}

        with patch.object(hub.Api, "queue_message", _q):
            self._reap(revived, [shell, revived])
        self.assertEqual([("s2", "队友说了句话")], forwarded)


class MachineChatterIsNotRealWork(unittest.TestCase):
    """队友的话不是这个 tab 干的活——阈值被广播推过头是 21 个死壳的真因。"""

    def test_relayed_messages_do_not_count_as_real_work(self):
        s = _s("s1", "c1", "待命·cursor工作流")
        s.msg_seq = s.machine_seq = 0
        with patch.object(hub.HUB, "cfg", {"max_messages": 20}):
            for who in ("agent·\u67d0\u67d0", "黑板", "控制台", "agent·别人"):
                hub.HUB.add_message(s, {"role": "user", "who": who, "html": "x"})
        self.assertEqual(4, s.msg_seq, "未读角标照数——用户确实有新东西要看")
        self.assertEqual(0, hub.HUB._real_seq(s), "但一句真活都没干")
        self.assertTrue(hub.HUB._is_checkin_shellish(s),
                        "挨了四条广播之后，它仍该被认作待命空壳")

    def test_the_users_own_messages_do_count(self):
        s = _s("s1", "c1", "待命·cursor工作流")
        s.msg_seq = s.machine_seq = 0
        with patch.object(hub.HUB, "cfg", {"max_messages": 20}):
            for _ in range(4):
                hub.HUB.add_message(s, {"role": "user", "who": "", "html": "把这个改了"})
        self.assertEqual(4, hub.HUB._real_seq(s))
        self.assertFalse(hub.HUB._is_checkin_shellish(s), "用户真派过活，不是空壳")

    def test_status_reports_count_for_neither(self):
        s = _s("s1", "c1", "待命·cursor工作流")
        s.msg_seq = s.machine_seq = 0
        with patch.object(hub.HUB, "cfg", {"max_messages": 20}):
            hub.HUB.add_message(s, {"role": "sys", "kind": "status", "html": "⚙"})
        self.assertEqual(0, s.msg_seq)
        self.assertEqual(0, hub.HUB._real_seq(s))

    def test_system_events_without_who_do_not_count_as_real_work(self):
        s = _s("s1", "c1", "待命·cursor工作流")
        s.msg_seq = s.machine_seq = s.real_seq = 0
        with patch.object(hub.HUB, "cfg", {"max_messages": 20}):
            for _ in range(4):
                hub.HUB.add_message(s, {"role": "sys", "html": "连接事件"})
        self.assertEqual(4, s.msg_seq)
        self.assertEqual(4, s.machine_seq)
        self.assertEqual(0, hub.HUB._real_seq(s))
        self.assertFalse(hub.HUB._did_real_work(s))

    def test_real_seq_survives_a_truncated_snapshot(self):
        """真实工作量不能随 messages 裁短回到 0。"""
        import json
        import tempfile

        client = type("Client", (), {"cwd": WS, "pid": 1, "peer_ip": None})()
        s = hub.Session(client, "c1", "待命·cursor工作流")
        with patch.object(hub.HUB, "cfg", {"max_messages": 40}):
            for _ in range(4):
                hub.HUB.add_message(s, {"role": "user", "html": "用户派活"})
            for _ in range(80):
                hub.HUB.add_message(s, {"role": "user", "who": "团队面板", "html": "广播"})
            self.assertEqual(4, hub.HUB._real_seq(s))
            self.assertEqual(40, len(s.messages), "真实消息已从可见数组里裁掉")
            with tempfile.TemporaryDirectory() as td:
                snap = Path(td) / "snap.json"
                with (patch.object(hub.Hub, "STATE_PATH", snap),
                      patch.object(hub.HUB, "sessions", {s.id: s}),
                      patch.object(hub.HUB, "order", [s.id])):
                    hub.HUB.save_state()
                raw = json.loads(snap.read_text(encoding="utf-8"))[0]
                self.assertEqual(4, raw["real_seq"])
                self.assertEqual(80, raw["machine_seq"])
                self.assertTrue(raw["messages_truncated"])
                restored, order = {}, []
                with (patch.object(hub.Hub, "STATE_PATH", snap),
                      patch.object(hub.HUB, "sessions", restored),
                      patch.object(hub.HUB, "order", order)):
                    hub.HUB.load_state()
        r = restored[order[0]]
        self.assertEqual(4, hub.HUB._real_seq(r))
        self.assertTrue(hub.HUB._did_real_work(r))

    def test_old_truncated_snapshot_with_machine_total_is_kept_conservatively(self):
        """旧快照没有 real_seq 时，机器累计值超过可见值说明历史已被裁短。"""
        import json
        import tempfile

        messages = [{"role": "user", "who": "团队面板", "html": "广播"}
                    for _ in range(40)]
        legacy = [{"id": "s1", "conv_key": "c1", "name": "待命·cursor工作流",
                   "cwd": WS, "created_ts": time.time(), "messages": messages,
                   # 73 是整个会话累计，40 只是快照里还看得见的最后一截。
                   "machine_seq": 73}]
        with tempfile.TemporaryDirectory() as td:
            snap = Path(td) / "snap.json"
            snap.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
            restored, order = {}, []
            with (patch.object(hub.Hub, "STATE_PATH", snap),
                  patch.object(hub.HUB, "sessions", restored),
                  patch.object(hub.HUB, "order", order)):
                hub.HUB.load_state()
        r = restored[order[0]]
        self.assertEqual(73, r.machine_seq)
        self.assertGreater(r.real_seq, hub.HUB.REAL_WORK_SEQ)
        self.assertTrue(hub.HUB._did_real_work(r), "证据不全时不能把可能干过活的 tab 收掉")

    def test_old_complete_machine_only_snapshot_stays_an_empty_shell(self):
        """兼容旧快照不等于把所有机器消息都保守算成真实工作。"""
        import json
        import tempfile

        legacy = [{"id": "s1", "conv_key": "c1", "name": "待命·cursor工作流",
                   "cwd": WS, "created_ts": time.time(),
                   "messages": [{"role": "user", "who": "黑板", "html": "提醒"}],
                   "machine_seq": 1}]
        with tempfile.TemporaryDirectory() as td:
            snap = Path(td) / "snap.json"
            snap.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
            restored, order = {}, []
            with (patch.object(hub.Hub, "STATE_PATH", snap),
                  patch.object(hub.HUB, "sessions", restored),
                  patch.object(hub.HUB, "order", order)):
                hub.HUB.load_state()
        self.assertEqual(0, hub.HUB._real_seq(restored[order[0]]))


class DeadShellsGetSweptUp(unittest.TestCase):
    """收壳只在接手落地那一刻触发，关窗走人的壳等不到那一刻，得有人扫。"""

    def _sweep(self, sessions):
        d = {x.id: x for x in sessions}
        with (patch.object(hub.HUB, "sessions", d),
              patch.object(hub.HUB, "order", [x.id for x in sessions]),
              patch.object(hub.HUB, "lock", threading.Lock()),
              patch.object(hub.HUB, "save_state", lambda: None),
              patch.object(hub.HUB, "_shell_sweep_ts", 0, create=True),
              patch.object(hub, "log_event", lambda *a, **k: None)):
            hub.HUB.sweep_dead_shells()
        return d

    def _shell(self, sid, **kw):
        s = _s(sid, "c" + sid, "待命·cursor工作流", connected=False, **kw)
        s.archived = kw.get("archived", True)
        s.machine_seq = kw.get("machine_seq", 0)
        s.msg_seq = kw.get("msg_seq", 2)
        s.created_ts = s.last_heartbeat = time.time() - 7200
        return s

    def test_an_archived_offline_empty_shell_is_swept(self):
        self.assertNotIn("s1", self._sweep([self._shell("s1")]))

    def test_one_that_only_ever_got_broadcasts_is_swept_too(self):
        # 正是控制台上那 21 个：msg_seq 11 全是广播堆的
        self.assertNotIn("s1", self._sweep([self._shell("s1", msg_seq=11, machine_seq=11)]))

    def test_a_shell_that_did_real_work_is_kept(self):
        self.assertIn("s1", self._sweep([self._shell("s1", msg_seq=11, machine_seq=2)]))

    def test_a_fresh_one_is_kept(self):
        s = self._shell("s1")
        s.created_ts = s.last_heartbeat = time.time()
        self.assertIn("s1", self._sweep([s]))

    def test_an_unarchived_one_is_kept(self):
        self.assertIn("s1", self._sweep([self._shell("s1", archived=False)]))

    def test_one_that_is_still_connected_is_kept(self):
        s = self._shell("s1")
        s.connected = True
        self.assertIn("s1", self._sweep([s]))

    def test_one_still_holding_a_users_message_is_kept(self):
        self.assertIn("s1", self._sweep([self._shell("s1", queued=[_user_msg()])]))


class BroadcastSkipsStandbyShells(unittest.TestCase):
    """待命壳手上没活，广播给它是纯噪音——还顺带把壳钉死在列表里。"""

    def _broadcast(self, sender, team, to="团队"):
        # 发送方自己也会收到「回执」（跳过了几个壳之类），那不是广播受众，
        # 单独记一路，别跟真正的收件人混在一起
        got = []

        def _q(self, sid, text, imgs=None, **kw):
            got.append(sid)
            return {"ok": True}

        with (patch.object(hub.Api, "_team_sessions", lambda self, root, **kw: team),
              patch.object(hub.Api, "queue_message", _q),
              patch.object(hub.HUB, "relay_log", []),
              patch.object(hub.HUB, "_save_relays", lambda: None),
              patch.object(hub, "log_event", lambda *a, **k: None)):
            r = hub.Api().relay_from_agent(sender, to, "我要重打包了")
        return r, [x for x in got if x != sender.id]

    def test_standby_shells_are_not_in_the_audience(self):
        sender = _s("s0", "a1b2c3d4", "rxyy tools·换装收尾", msg_seq=40)
        worker = _s("s1", "b2c3d4e5", "rxyy tools·远程访问", msg_seq=40)
        shell = _s("s2", "c3d4e5f6", "待命·cursor工作流·9275")
        r, got = self._broadcast(sender, [sender, worker, shell])
        self.assertTrue(r["ok"])
        self.assertEqual(["s1"], got)
        self.assertIn("跳过 1 个待命壳", r["note"])

    def test_naming_a_standby_shell_still_reaches_it(self):
        # 收窄的只有广播：明确点名找它，照送
        sender = _s("s0", "a1b2c3d4", "rxyy tools·换装收尾", msg_seq=40)
        shell = _s("s2", "c3d4e5f6", "待命·cursor工作流·9275")
        with (patch.object(hub.HUB, "sessions", {"s0": sender, "s2": shell}),
              patch.object(hub.HUB, "lock", threading.Lock())):
            r, got = self._broadcast(sender, [sender, shell], to="c3d4e5f6")
        self.assertTrue(r["ok"])
        self.assertEqual(["s2"], got)

    def test_board_reminder_skips_standby_shells_too(self):
        sender = _s("s0", "a1b2c3d4", "rxyy tools·换装收尾", msg_seq=40)
        worker = _s("s1", "b2c3d4e5", "rxyy tools·远程访问", msg_seq=40)
        shell = _s("s2", "c3d4e5f6", "待命·cursor工作流·9275")
        got = []

        def _q(self, sid, text, imgs=None, **kw):
            got.append(sid)
            return {"ok": True}

        with (patch.dict(hub.HUB.cfg, {"team_tracks": {}}, clear=False),
              patch.object(hub.Api, "_team_sessions",
                           lambda self, root, **kw: [sender, worker, shell]),
              patch.object(hub.Api, "queue_message", _q),
              patch.object(hub, "log_event", lambda *a, **k: None)):
            hub.Api()._board_notify(sender, "部署", {"text": "要换装了",
                                                     "from_label": "x", "from8": "aaa"}, WS)
        self.assertEqual(["s1"], got)


class AgentNamesItself(unittest.TestCase):
    """名字归 agent 自己说了算：它是唯一知道自己在做哪个项目哪块功能的人。"""

    def test_project_is_parsed_from_the_two_part_name(self):
        self.assertEqual("rxyy tools", hub._project_of_task_name("rxyy tools·换装收尾"))
        self.assertEqual("智慧云广播", hub._project_of_task_name("智慧云广播：日报取数"))
        self.assertEqual("", hub._project_of_task_name("换装收尾"))
        self.assertEqual("", hub._project_of_task_name(""))

    def test_self_reported_name_supersedes_the_24_char_placeholder(self):
        s = _s("s1", "c1", "待命·cursor工作流")
        cfg = {"team_assign": {"c1": "你先看下目前当前项目最新的开发进度、剩余任务。（…"},
               "team_assign_auto": {"c1": True}, "team_tracks": {}}
        with (patch.dict(hub.HUB.cfg, cfg, clear=False),
              patch.object(hub, "save_config", lambda c: None),
              patch.object(hub, "log_event", lambda *a, **k: None),
              patch.object(hub.HUB, "sessions", {"s1": s}),
              patch.object(hub.HUB, "lock", threading.Lock())):
            # 改名前：显示的是用户那句话的前 24 字
            self.assertIn("你先看下目前", hub.Api().session_label(s))
            hub.HUB.maybe_apply_task_name(s, "rxyy tools·换装收尾")
            self.assertTrue(s.agent_named)
            self.assertEqual("rxyy tools", s.agent_project)
            self.assertEqual("rxyy tools·换装收尾", hub.Api().session_label(s))

    def test_a_hand_written_assignment_is_never_stolen(self):
        # 用户在面板上手填的分工是人的意思，agent 自报名字不许顶掉它
        s = _s("s1", "c1", "待命·cursor工作流")
        cfg = {"team_assign": {"c1": "日报取数"}, "team_assign_auto": {}, "team_tracks": {}}
        with (patch.dict(hub.HUB.cfg, cfg, clear=False),
              patch.object(hub, "save_config", lambda c: None),
              patch.object(hub, "log_event", lambda *a, **k: None),
              patch.object(hub.HUB, "sessions", {"s1": s}),
              patch.object(hub.HUB, "lock", threading.Lock())):
            hub.HUB.maybe_apply_task_name(s, "rxyy tools·换装收尾")
            self.assertEqual("日报取数", hub.Api()._team_assign(s))
            self.assertIn("日报取数", hub.Api().session_label(s))

    def test_check_in_name_does_not_count_as_self_naming(self):
        s = _s("s1", "c1", "会话")
        with (patch.object(hub.HUB, "sessions", {"s1": s}),
              patch.object(hub.HUB, "lock", threading.Lock())):
            hub.HUB.maybe_apply_task_name(s, "待命·cursor工作流")
        self.assertFalse(s.agent_named)

    def test_cursor_auto_title_no_longer_overwrites_a_self_named_tab(self):
        # 08-07 实测：一个自报过名字的 tab 被 Cursor 自动标题冲成了
        # 英文的「Persistent task reporting」，从此再也认不出它在干嘛
        s = _s("s1", "c1", "rxyy tools·换装收尾", agent_named=True)
        with (patch.object(hub.HUB, "sessions", {"s1": s}),
              patch.object(hub.HUB, "order", ["s1"]),
              patch.object(hub.HUB, "lock", threading.Lock()),
              patch.object(hub.HUB, "_relocate_if_stale", lambda self_s, cl: None),
              patch.object(hub, "read_cursor_title",
                           lambda uid: "Persistent task reporting")):
            hub.HUB.sync_cursor_titles()
        self.assertEqual("rxyy tools·换装收尾", s.name)

    def test_self_reported_name_is_pushed_to_cursor_chat_title(self):
        """Cursor 列表不能还停在 Persistent plus zhi report，要跟着 task_name 变。"""
        s = _s("s1", "c1", "待命·cursor工作流",
               uuid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        pushed = []
        with (patch.dict(hub.HUB.cfg, {"team_assign": {}, "team_assign_auto": {}},
                         clear=False),
              patch.object(hub, "save_config", lambda c: None),
              patch.object(hub, "log_event", lambda *a, **k: None),
              patch.object(hub, "write_cursor_title",
                           lambda uid, name, **kw: pushed.append((uid, name)) or True),
              patch.object(hub.HUB, "sessions", {"s1": s}),
              patch.object(hub.HUB, "lock", threading.Lock())):
            hub.HUB.maybe_apply_task_name(s, "控制台·开链报错")
        self.assertEqual([("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "控制台·开链报错")],
                         pushed)
        self.assertEqual("控制台·开链报错", s.cursor_title)

    def test_checkin_shell_name_is_not_pushed_to_cursor(self):
        s = _s("s1", "c1", "会话", uuid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        pushed = []
        with (patch.object(hub.HUB, "sessions", {"s1": s}),
              patch.object(hub.HUB, "lock", threading.Lock()),
              patch.object(hub, "write_cursor_title",
                           lambda *a, **k: pushed.append(a) or True)):
            hub.HUB.maybe_apply_task_name(s, "待命·cursor工作流")
        self.assertEqual([], pushed)

    def test_agent_cannot_report_cursor_auto_title_as_its_task_name(self):
        """08-28 09:07 实测 d7044dd1：agent 把 Cursor 自动标题原样当 task_name 报上来。

        收下的话三重损失——tab 名变成全场同款英文模板句、agent_named 立起来让
        Cursor→控制台 那条同步彻底失效、_push_name_to_cursor 还把它写回 Cursor 库。
        用户 08-27「一排 Persistent plus，你看这就有点混乱了」有一半是这么来的。
        """
        pushed = []
        for bad in ("Persistent plus task report", "Persistent plus zhi report",
                    "Persistent task reporting"):
            s = _s("s1", "c1", "控制台·侧栏改名",
                   uuid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
            with (patch.object(hub, "log_event", lambda *a, **k: None),
                  patch.object(hub, "write_cursor_title",
                               lambda *a, **k: pushed.append(a) or True),
                  patch.object(hub.HUB, "sessions", {"s1": s}),
                  patch.object(hub.HUB, "lock", threading.Lock())):
                hub.HUB.maybe_apply_task_name(s, bad)
            self.assertEqual("控制台·侧栏改名", s.name, bad)
            self.assertFalse(s.agent_named, bad)
        self.assertEqual([], pushed)

    def test_normal_task_name_still_applies(self):
        s = _s("s1", "c1", "待命·cursor工作流",
               uuid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        with (patch.dict(hub.HUB.cfg, {"team_assign": {}, "team_assign_auto": {}},
                         clear=False),
              patch.object(hub, "save_config", lambda c: None),
              patch.object(hub, "log_event", lambda *a, **k: None),
              patch.object(hub, "write_cursor_title", lambda *a, **k: True),
              patch.object(hub.HUB, "sessions", {"s1": s}),
              patch.object(hub.HUB, "lock", threading.Lock())):
            hub.HUB.maybe_apply_task_name(s, "控制台·侧栏改名")
        self.assertEqual("控制台·侧栏改名", s.name)
        self.assertTrue(s.agent_named)

    def test_self_named_tab_can_still_be_renamed_after_a_cursor_title_landed(self):
        # 旧逻辑：cursor_title 一有值 maybe_apply_task_name 就 return，
        # agent 此后永远改不动自己的名字
        s = _s("s1", "c1", "Persistent task reporting", cursor_title="Persistent task reporting")
        with (patch.dict(hub.HUB.cfg, {"team_assign": {}, "team_assign_auto": {}},
                         clear=False),
              patch.object(hub, "save_config", lambda c: None),
              patch.object(hub, "log_event", lambda *a, **k: None),
              patch.object(hub.HUB, "sessions", {"s1": s}),
              patch.object(hub.HUB, "lock", threading.Lock())):
            hub.HUB.maybe_apply_task_name(s, "rxyy tools·换装收尾")
        self.assertEqual("rxyy tools·换装收尾", s.name)

    def test_track_does_not_replace_the_self_reported_project(self):
        # 业务线只属于项目内二级筛选；它不能再把 TeamScope 的项目边界改成「直播线」。
        s = _s("s1", "c1", "x", agent_project="rxyy tools")
        with patch.dict(hub.HUB.cfg, {"team_tracks": {}}, clear=False):
            self.assertEqual("rxyy tools", hub.Api()._group_key(s))
            self.assertEqual("", hub.Api()._team_track(s))
        with patch.dict(hub.HUB.cfg, {"team_tracks": {"c1": "直播线"}}, clear=False):
            self.assertEqual("rxyy tools", hub.Api()._group_key(s))
            self.assertEqual("直播线", hub.Api()._team_track(s))

    def test_panel_second_level_nodes_only_come_from_tracks(self):
        rows = [{"track": "直播"}, {"track": "直播"}, {"track": "视频编辑"}]
        self.assertEqual([{"name": "直播", "count": 2},
                          {"name": "视频编辑", "count": 1}],
                         hub.Api._tracks_of(rows, []))


class SelfReportedStatusStaysHonest(unittest.TestCase):
    """状态既不该忽然消失，也不该把十分钟前的话装成此刻正在干。"""

    def test_fresh_report_is_shown_as_is(self):
        now = time.time()
        s = _s("s1", "c1", "x", agent_status="developing",
               agent_activity="改 hub.py", agent_status_ts=now - 5)
        self.assertEqual(("developing", "改 hub.py"),
                         hub.Api()._self_report(s, now)[:2])

    def test_a_stale_report_carries_its_age(self):
        now = time.time()
        s = _s("s1", "c1", "x", agent_status="developing",
               agent_activity="改 hub.py", agent_status_ts=now - 900)
        status, activity, age = hub.Api()._self_report(s, now)
        self.assertEqual("developing", status)
        self.assertEqual("改 hub.py（15分钟前）", activity)
        self.assertEqual(900, age)

    def test_heartbeat_gap_no_longer_blanks_it(self):
        # 旧闸门是「15 秒内有心跳」，MCP 每次重启都踩中，那行就凭空消失
        now = time.time()
        s = _s("s1", "c1", "x", agent_status="developing",
               agent_activity="改 hub.py", agent_status_ts=now - 20)
        self.assertEqual("developing", hub.Api()._self_report(s, now)[0])

    def test_a_disconnected_tab_reports_nothing(self):
        now = time.time()
        s = _s("s1", "c1", "x", connected=False, agent_status="developing",
               agent_activity="改 hub.py", agent_status_ts=now - 5)
        self.assertEqual(("", ""), hub.Api()._self_report(s, now)[:2])


if __name__ == "__main__":
    unittest.main()
