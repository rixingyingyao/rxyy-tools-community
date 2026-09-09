# -*- coding: utf-8 -*-
"""cursor_uuid 退出身份决策——08-12 多 agent 同工作区 uuid 互抢事故复现。

现场（根治方案书第一节·路径4 + 三节表）：一个 agent 的对话里叙述了一句
「OA对接 的 conversation_id是1d07989b」，正中「参数形态」正则；OA对接 下一次
zt 自校准就把这个活人的 uuid 抢走，并把原主 tab 走 _handover_out——closed_convs
封 ID、connected 翻 False、队列搬空。两个活 tab 身份来回互抢，面板上 tab 说
「已被接手」而 agent 明明还在干自己的活。当天的活性闸（HANDOVER_QUIET_SECS）只
挡住「15 分钟内说过话」的原主；埋头长活不 zt 的窗口期照样被抢。

根治（方案书第二节）：cursor_uuid 降级为纯显示/transcript 定位辅助，退出身份
决策。认领了活的会话对某个 uid 的持有是权威的——命中的 uid 被这样的会话持有
时，据 uuid 的推断整轮拒绝、一个字节不动（不封 ID、不翻 connected、不搬队列、
连 uuid 线索都不夺）。真交接由 agent 自己退出或用户显式处置。
"""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub  # noqa: E402

WS = r"d:\桌面\working\cursor工作流"
UID = "cursor-dialog-D"


class _FakeClient:
    def __init__(self):
        self.closed_convs, self.sessions, self.cwd = {}, {}, WS


def _sess(sid, conv, name, **kw):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = sid, conv, name
    s.cwd = s.task_root = WS
    s.connected = kw.get("connected", True)
    s.pending = kw.get("pending")
    s.queued = kw.get("queued") or []
    s.messages = []
    s.msg_seq = kw.get("msg_seq", 2)
    s.machine_seq = 0
    s.real_seq = kw.get("real_seq", 2)
    s.shell_born = kw.get("shell_born", False)
    s.agent_named = kw.get("agent_named", False)
    s.agent_status_ts = kw.get("agent_status_ts", 0)
    s.claimed_task_ts = kw.get("claimed_task_ts", 0.0)
    s.cursor_uuid = kw.get("uuid")
    s.uuid_verified = kw.get("verified", False)
    s.transcript_path = kw.get("transcript")
    s.last_zhi_ts = kw.get("last_zhi_ts", time.time() - 3600)
    s.recon_deadline = None
    s.archived = False
    s.handed_off_to = ""
    s.client = kw.get("client")
    s.file_path = None
    s.rev = 0
    s.lock = threading.Lock()
    return s


def _calibrate(s, sessions):
    """驱动 s 的身份自校准：路径1-3 全部落空，命中第4条「接手交接兜底」
    ——正是 08-12 互抢事故走的那条（无排他集的全库参数匹配）。"""
    d = {x.id: x for x in sessions}
    with patch.object(hub.HUB, "sessions", d), \
         patch.object(hub.HUB, "order", list(d)), \
         patch.object(hub.HUB, "cfg", {"max_messages": 20}), \
         patch.object(hub, "generating_now_session", lambda cwd, exclude=None: None), \
         patch.object(hub, "generating_session_for_conv",
                      lambda cwd, conv, exclude=None: None), \
         patch.object(hub, "cursor_db_session_for_conv",
                      lambda cwd, conv, exclude=None:
                      None if exclude else (UID, "transcript-D.jsonl")), \
         patch.object(hub, "composer_mentions_conv",
                      lambda uid, conv, param_context=False: False), \
         patch.object(hub.Hub, "save_state", MagicMock()), \
         patch.object(hub, "log_event", lambda *a, **k: None):
        hub.HUB._verify_identity_by_generating(s)


class UuidEvidenceNeverArchivesClaimedOwner(unittest.TestCase):
    def test_quiet_claimed_owner_survives_uuid_takeover(self):
        # 修前红：埋头干长活（>15min 没 zt）的认领会话被 _handover_out——
        # closed_convs 封 ID、connected 翻 False，下一次 zhi 直接被拦
        cli = _FakeClient()
        owner = _sess("x", "c-owner", "OA对接", uuid=UID, verified=True,
                      claimed_task_ts=time.time() - 7200, client=cli,
                      queued=[{"id": "q1", "who": "", "text": "用户排的话"}])
        cli.sessions["c-owner"] = owner
        thief = _sess("s", "c-thief", "排查串台", agent_named=True,
                      claimed_task_ts=time.time() - 60)
        _calibrate(thief, [owner, thief])
        self.assertFalse(cli.closed_convs.get("c-owner"),
                         "认领了活的原主绝不因 uuid 推断被封 ID")
        self.assertTrue(owner.connected, "connected 不许被 uuid 推断翻掉")
        self.assertEqual("", owner.handed_off_to, "不许被记成「已交接」")
        self.assertEqual(1, len(owner.queued), "队列不许被搬空")
        self.assertFalse(owner.archived)
        # 整轮拒绝：连 uuid 线索都不夺，活人原主的显示/transcript 保持正确
        self.assertEqual(UID, owner.cursor_uuid, "认领方对 uid 的持有是权威的")
        self.assertIsNone(thief.cursor_uuid, "据 uuid 的推断整轮拒绝，thief 不改绑")

    def test_recently_talking_owner_keeps_uuid_entirely(self):
        # 活性闸回归：刚说过话的 verified 原主，连 uuid 都不让渡
        cli = _FakeClient()
        owner = _sess("x", "c-owner", "OA对接", uuid=UID, verified=True,
                      agent_status_ts=time.time() - 60, client=cli)
        thief = _sess("s", "c-thief", "排查串台", agent_named=True)
        _calibrate(thief, [owner, thief])
        self.assertEqual(UID, owner.cursor_uuid)
        self.assertIsNone(thief.cursor_uuid)

    def test_unclaimed_quiet_owner_still_hands_over(self):
        # 老交接路径保留：verified、有出生真名但从没认领过活、也不说话的 tab，
        # 其 Cursor 对话被证实改去驱动别人时照旧归档交接（08-04 修复不回退）
        cli = _FakeClient()
        owner = _sess("x", "c-owner", "老任务", uuid=UID, verified=True, client=cli)
        cli.sessions["c-owner"] = owner
        thief = _sess("s", "c-thief", "接手方", agent_named=True)
        with patch.object(hub.Api, "queue_message",
                          lambda a, sid, t, im, who=None, files=None, force=False:
                          {"ok": True}):
            _calibrate(thief, [owner, thief])
        self.assertTrue(cli.closed_convs.get("c-owner"))
        self.assertFalse(owner.connected)
        self.assertEqual("c-thief", owner.handed_off_to)


if __name__ == "__main__":
    unittest.main()
