# -*- coding: utf-8 -*-
"""接手交接：agent 被派去接手别的活之后，老 tab 该归档、被接手的 tab 该复活。

08-04 实测三处一起犯错——「团队面板修复」的 agent 被派去接直播项目的活，结果：
1) 被接手的 tab 一直不复活：身份自校准的两条路都拿 claimed 排除了「已验证的
   非空壳认领」（08-03 加排他是为了防错绑），正当换绑于是永远校准不过来；
2) 老 tab 一直挂着「疑似 Cursor 里还活着·通道未接」，用户以为接手没生效——
   它有真实历史，不符合「待命空壳」的收壳条件，没人管得着它；
3) 接手方落地后第一件事常是 ji（黑板/转告），而 ji 那两条路径只刷心跳、不做
   身份校准也不收壳，面板就一直停在交接前的样子。

修复契约：排他挡住时再查一次不带排除的「conversation_id 参数形态 + 30 分钟活跃」
硬证据；换绑坐实后把老 tab 归档（不是删除，历史/右键接手都还在）并把它没送出去的
排队消息转给现任；ji 两条路径与 zt 同权做校准+收壳。
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
U_OLD = "fcd1ff60-uuid"   # 「团队面板修复」那个 Cursor 对话（现已改驱动直播项目）


def _sess(sid, conv, name, msg_seq=0, uuid=None, verified=False,
          pending=None, queued=None, archived=False):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = sid, conv, name
    s.cwd = s.task_root = WS1
    s.connected = True
    s.archived = archived
    s.pending = pending
    s.queued = queued or []
    s.messages = []
    s.msg_seq = msg_seq
    s.rev = 0
    s.client = None
    s.file_path = None
    s.id_history = []
    s.name_history = []
    s.handed_off_to = ""
    s.cursor_uuid = uuid
    s.uuid_verified = verified
    s.transcript_path = ("C:\\t\\" + uuid + ".jsonl") if uuid else None
    s.lock = threading.Lock()
    return s


class _HubHarness(unittest.TestCase):
    """把 Hub 的落盘/日志换成哑实现，只留被测逻辑。"""

    def _start(self, patches):
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

    def _patch_hub(self, sessions):
        d = {x.id: x for x in sessions}
        self._start([
            patch.object(hub.HUB, "sessions", d),
            patch.object(hub.HUB, "order", list(d)),
            patch.object(hub.HUB, "cfg", {"max_messages": 200}),
            patch.object(hub.HUB, "name_tombstones", {}),
            # A步后 _tombstone_shell 会把台账落盘，测试不许碰真文件
            patch.object(hub.Hub, "_save_takeover_aliases", lambda self: None),
            patch.object(hub.Hub, "log_end", lambda self, s, why: None),
            patch.object(hub.Hub, "save_state", lambda self: None),
            patch.object(hub, "log_event", lambda *a, **k: None),
        ])
        return d

    def _patch_queue(self):
        """记下每一次入队，返回 [(session_id, text)]。"""
        moved = []
        self._start([patch.object(
            hub.Api, "queue_message",
            lambda api, sid, text, imgs, who=None, files=None, force=False:
                (moved.append((sid, text)), {"ok": True})[1])])
        return moved


class HandoverCalibrationTests(_HubHarness):
    """身份自校准 + 身份根治④（2026-08-12）：cursor_uuid 退出身份决策。

    08-04 曾放开「据 uuid 推断把 verified 原主 _handover_out 归档」以修「被接手 tab
    不复活」；08-12 该路径反噬——参数形态正则被对话叙述误中，活人 tab 被当成
    「已交接」互抢身份（方案书第一节·路径4）。根治铁律压过 08-04：认领了活的
    会话对某个 uid 的持有是权威的，据 uuid 的推断整轮拒绝。"""

    def setUp(self):
        # old = 团队面板修复（有真实历史 = 认领了活、身份验过），succ = 直播项目 tab
        self.old = _sess("t-old", "fcd1ff60", "团队面板修复", msg_seq=8,
                         uuid=U_OLD, verified=True)
        self.succ = _sess("t-succ", "627f39b7", "直播项目", msg_seq=12)
        self._patch_hub([self.old, self.succ])
        self._patch_queue()

    def _verify(self, db_hits):
        """db_hits = cursor_db_session_for_conv 按序的返回值；返回每次的排除集合。"""
        calls = []

        def fake_db(cwd, cid, exclude=(), **kw):
            calls.append(set(exclude))
            return db_hits[min(len(calls) - 1, len(db_hits) - 1)]

        with (patch.object(hub, "generating_now_session", return_value=None),
              patch.object(hub, "generating_session_for_conv", return_value=None),
              patch.object(hub, "cursor_db_session_for_conv", side_effect=fake_db)):
            hub.HUB._verify_identity_by_generating(self.succ)
        return calls

    def test_claimed_holder_is_spared_from_uuid_handover(self):
        # 根治④：命中的 uid 被「认领了活」的会话持有 → 据 uuid 的推断整轮拒绝。
        # 探测路径不变（仍会为找证据不带排除再查一次），但结论是「一个字节不动」
        calls = self._verify([None, (U_OLD, "path-old", {"updated_ts": 1.0})])
        self.assertEqual([{U_OLD}, set()], calls)   # 探测逻辑照旧：带排除→不带排除
        self.assertIsNone(self.succ.cursor_uuid, "据 uuid 推断不再改绑")
        self.assertFalse(self.succ.uuid_verified)
        self.assertFalse(self.old.archived, "认领了活的原主绝不被 uuid 推断归档")
        self.assertEqual("", self.old.handed_off_to)
        self.assertEqual(U_OLD, self.old.cursor_uuid, "认领方对 uid 的持有是权威的")

    def test_unclaimed_misbound_shell_still_yields_its_uuid(self):
        # 守卫不扩大化：持有者是没认领过活的报到空壳（08-03 错绑场景）时，
        # 照旧把本尊线索拿回来——空壳收壳/校准机制不受影响
        self.old.name, self.old.msg_seq = "待命·cursor工作流", 1
        calls = self._verify([None])   # 空壳不进 claimed 排除集，一次逐字节查即可
        self.assertEqual([set()], calls)

    def test_fallback_still_requires_param_evidence(self):
        # 不带排除也查不到「本会话 ID 以参数形态出现」的活跃对话 → 一个字节都不动
        self._verify([None])
        self.assertIsNone(self.succ.cursor_uuid)
        self.assertFalse(self.old.archived)
        self.assertEqual(U_OLD, self.old.cursor_uuid)


class HandoverOutTests(_HubHarness):
    """_handover_out 本身：归档而不是删除，排队的话跟着人走。"""

    def setUp(self):
        self.old = _sess("t-old", "fcd1ff60", "团队面板修复", msg_seq=8,
                         uuid=U_OLD, verified=True)
        self.succ = _sess("t-succ", "627f39b7", "直播项目", msg_seq=12)
        self.left = self._patch_hub([self.old, self.succ])
        self.moved = self._patch_queue()

    def test_old_tab_is_archived_not_dropped(self):
        hub.HUB._handover_out(self.old, self.succ)
        self.assertIn("t-old", self.left)        # 历史/记录文件/右键接手都还在
        self.assertTrue(self.old.archived)
        self.assertFalse(self.old.connected)
        self.assertIn("直播项目", self.old.messages[-1]["html"])
        self.assertIn("fcd1ff60", self.succ.id_history)  # 按老 ID 转告能找到现任

    def test_queued_messages_follow_the_agent(self):
        self.old.queued = [{"text": "媒资那块的结论给我一下", "who": "agent·甲"}]
        self.old.messages = [{"role": "user", "queued": True, "html": "x"}]
        hub.HUB._handover_out(self.old, self.succ)
        self.assertEqual([("t-succ", "媒资那块的结论给我一下")], self.moved)
        self.assertEqual([], self.old.queued)
        self.assertFalse(self.old.messages[0]["queued"])
        self.assertIn("1 条", self.old.messages[-1]["html"])

    def test_tab_with_unanswered_question_is_left_alone(self):
        # 还等着用户回话的不能埋进「已结束」组——那等于把人家的提问吞了
        self.old.pending = {"id": "q1"}
        hub.HUB._handover_out(self.old, self.succ)
        self.assertFalse(self.old.archived)
        self.assertTrue(self.old.connected)


class HandoverRelayTests(_HubHarness):
    """交接之后队友按老 ID 转告，话要跟到现任身上，不能进这个没人看的 tab。"""

    def setUp(self):
        self.mate = _sess("t-mate", "aaaa1111", "甲")
        self.old = _sess("t-old", "fcd1ff60", "团队面板修复", msg_seq=8,
                         archived=True)
        self.old.connected = False
        self.old.handed_off_to = "627f39b7"
        self.succ = _sess("t-succ", "627f39b7", "直播项目", msg_seq=12)
        self._patch_hub([self.mate, self.old, self.succ])
        self.sent = self._patch_queue()
        self._start([
            patch.object(hub.HUB, "relay_log", []),
            patch.object(hub.Hub, "_save_relays", lambda self: None),
            patch.object(hub.Api, "session_label", lambda api, s: s.name),
            patch.object(hub.Api, "_team_role", lambda api, s: ""),
            patch.object(hub.Api, "_team_assign", lambda api, s: ""),
        ])

    def test_relay_by_old_id_reaches_the_successor(self):
        r = hub.Api().relay_from_agent(self.mate, "fcd1ff60", "面板那块我要动了")
        self.assertTrue(r["ok"])
        self.assertEqual(["t-succ"], [sid for sid, _ in self.sent
                                      if sid != "t-mate"])

    def test_plain_archived_tab_still_parks_its_own_messages(self):
        # 只是被用户关掉的 tab（没交接过）维持原样：话寄存在它自己那儿等接手
        self.old.handed_off_to = ""
        r = hub.Api().relay_from_agent(self.mate, "fcd1ff60", "面板那块我要动了")
        self.assertTrue(r["ok"])
        self.assertIn("t-old", [sid for sid, _ in self.sent])


class TakeoverDispatchLabelTests(_HubHarness):
    """派活自动命名（08-04 新增）必须放过接手提示词。

    实测：一次给 4 个待命 agent 派接手，4 个壳全被改名成「你的任务：接手一个此前
    在…」——名字不再以「待命」打头，_is_checkin_shellish 就认不出它们，于是壳的
    uuid 认领变成排他的，被接手的原 tab 反而永远校准不回身份、复活不了。"""

    PROMPT = (hub.TAKEOVER_PROMPT_HEAD + "，把它没做完的工作继续完成。\n\n"
              "【原会话信息】\n- conversation_id：2425c5d0\n")

    def setUp(self):
        self.shell = _sess("t-shell", "6d75c6a4", "待命·cursor工作流", msg_seq=1)
        self._patch_hub([self.shell])
        self.cfg = hub.HUB.cfg
        self._start([
            patch.object(hub, "save_config", lambda cfg: None),
            patch.object(hub.Api, "_team_assign", lambda api, s: ""),
            patch.object(hub.Api, "_remember_label", lambda api, s: None),
        ])

    def test_takeover_prompt_leaves_the_shell_alone(self):
        hub.Api()._auto_label_on_dispatch(self.shell, self.PROMPT)
        self.assertEqual("待命·cursor工作流", self.shell.name)
        self.assertEqual({}, self.cfg.get("team_assign") or {})

    def test_takeover_prompt_with_sender_prefix_also_skipped(self):
        # 面板派单会在正文前加「[手机] 」这类来源前缀
        hub.Api()._auto_label_on_dispatch(self.shell, "[手机] " + self.PROMPT)
        self.assertEqual("待命·cursor工作流", self.shell.name)

    def test_a_real_task_still_renames_the_shell(self):
        hub.Api()._auto_label_on_dispatch(self.shell, "继续完善团队面板功能")
        self.assertEqual("继续完善团队面板功能", self.shell.name)
        self.assertEqual("继续完善团队面板功能",
                         (self.cfg.get("team_assign") or {}).get("6d75c6a4"))


class _FakeClient:
    def __init__(self, sessions):
        self.sessions = {x.conv_key: x for x in sessions}
        self.closed_convs = {}
        self.last_heartbeat = 0.0
        self.cwd = WS1
        self.peer_ip = ""
        self.pid = 111


class JiSignalCalibrationTests(_HubHarness):
    """ji（转告/黑板）到达与 zt 同权：也要做身份校准 + 收壳，否则接手方落地后
    先发黑板的那种（08-04 实测）面板永远停在交接前。"""

    def setUp(self):
        self.succ = _sess("t-succ", "627f39b7", "直播项目", msg_seq=12)
        self._patch_hub([self.succ])
        seen = self.seen = []
        self._start([
            patch.object(hub.Hub, "_verify_identity_by_generating",
                         lambda h, s: seen.append(("verify", s.id))),
            patch.object(hub.Hub, "_reap_takeover_shell",
                         lambda h, s: seen.append(("reap", s.id))),
            patch.object(hub.Api, "relay_from_agent", lambda api, s, to, m: None),
            patch.object(hub.Api, "board_from_agent", lambda api, s, kind, t: None),
        ])

    def _send(self, msg):
        hub.HUB._handle_client_msg(_FakeClient([self.succ]), None, msg)

    def test_relay_arrival_calibrates_and_reaps(self):
        self._send({"type": "agent_relay", "conversation_id": "627f39b7",
                    "to": "团队", "message": "接手了"})
        self.assertEqual([("verify", "t-succ"), ("reap", "t-succ")], self.seen)

    def test_board_arrival_calibrates_and_reaps(self):
        self._send({"type": "agent_board", "conversation_id": "627f39b7",
                    "kind": "提交", "text": "全链路已推"})
        self.assertEqual([("verify", "t-succ"), ("reap", "t-succ")], self.seen)


class ReviveHandedOffTests(_HubHarness):
    """08-12 回归：被「接手交接」误归档的 tab，其 agent 还带着自己的 conv_key
    来 zt/ji = 交接是误判，就地撤销归档并清 closed_convs（不清的话它下次 zhi
    会被拦截、被告知「立即结束任务」）。当天实测：OA对接 被叙述污染误判交接后，
    zt 全被丢弃，tab 一直躺在「已结束」组，用户以为会话死了。
    用户手动 × 的（无 handed_off_to）与进程级心跳维持原样，永不自动复活。"""

    def _mk(self, handed_to="627f39b7"):
        s = _sess("t-old", "1d07989b", "OA对接", msg_seq=12, archived=True)
        s.peer_ip = ""
        s.handed_off_to = handed_to
        s.end_reason = "活已交接给 直播项目"
        s.connected = False
        s.pid = 111
        s.created_at = "2026-08-12 08:37:50"
        return s

    def test_zt_revives_wrongly_handed_off_tab(self):
        s = self._mk()
        self._patch_hub([s])
        c = _FakeClient([])
        c.closed_convs["1d07989b"] = True
        got = hub.HUB._resolve_for_signal(c, "1d07989b", revive_handed=True)
        self.assertIs(got, s)
        self.assertFalse(s.archived)
        self.assertEqual("", s.handed_off_to)
        self.assertNotIn("1d07989b", c.closed_convs)
        self.assertIn("接回来", s.messages[-1]["html"])

    def test_heartbeat_never_revives_archived(self):
        s = self._mk()
        self._patch_hub([s])
        got = hub.HUB._resolve_for_signal(_FakeClient([]), "1d07989b")
        self.assertIsNone(got)
        self.assertTrue(s.archived)

    def test_manual_close_not_revived(self):
        s = self._mk(handed_to="")
        self._patch_hub([s])
        got = hub.HUB._resolve_for_signal(_FakeClient([]), "1d07989b",
                                          revive_handed=True)
        self.assertIsNone(got)
        self.assertTrue(s.archived)

    def test_already_bound_archived_session_also_revives(self):
        # 交接归档时 client.sessions 里的映射可能还在（同一条连接没断过）
        s = self._mk()
        self._patch_hub([s])
        c = _FakeClient([s])
        c.closed_convs["1d07989b"] = True
        got = hub.HUB._resolve_for_signal(c, "1d07989b", revive_handed=True)
        self.assertIs(got, s)
        self.assertFalse(s.archived)
        self.assertNotIn("1d07989b", c.closed_convs)


if __name__ == "__main__":
    unittest.main()
