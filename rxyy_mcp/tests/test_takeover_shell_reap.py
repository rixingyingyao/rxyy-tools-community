# -*- coding: utf-8 -*-
"""手动接手落地自动收待命空壳：同 Cursor 窗口切到原对话 → 空壳收掉。

用户实操（07-31）：新 Cursor 对话先自动报到成「待命·xxx」（空壳 A，新 ID），
再贴接手提示词切到原对话 B（同一窗口 = 同 cursor_uuid）→ A 该自动收起。
关键安全边界：单独发新任务（没有别的有历史对话复活）绝不能误收空壳。
"""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub

UID = "cursorchat-uuid-1"


def _s(sid, conv, name, uuid, msg_seq, pending=None, queued=None):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = sid, conv, name
    s.cursor_uuid = uuid
    s.msg_seq = msg_seq
    s.pending = pending
    s.queued = queued or []
    s.connected = True
    s.client = None
    s.lock = threading.Lock()
    return s


class TakeoverShellReapTests(unittest.TestCase):
    def _reap(self, revived, sessions):
        d = {x.id: x for x in sessions}
        order = [x.id for x in sessions]
        # 账本全隔离：收壳走真 _retire_conv_into/_tombstone_shell，不打桩会把
        # 「newid→origid」这类测试数据灌进真 HUB 的台账并落盘，还会污染同进程
        # 后跑的用例（_shell_takeover_target 的台账兜底）
        with (patch.object(hub.HUB, "sessions", d),
              patch.object(hub.HUB, "order", order),
              patch.object(hub.HUB, "log_end", lambda s, why: None),
              patch.object(hub.HUB, "takeover_aliases", {}),
              patch.object(hub.HUB, "takeover_ledger", {}, create=True),
              patch.object(hub.HUB, "name_tombstones", {}),
              patch.object(hub.Hub, "_save_takeover_aliases", lambda self: None),
              patch.object(hub, "log_event", lambda *a, **k: None)):
            hub.HUB._reap_takeover_shell(revived)
        return d

    def test_empty_shell_on_same_window_is_reaped(self):
        shell = _s("s1", "newid", "待命·cursor工作流", UID, msg_seq=2)
        revived = _s("s2", "origid", "cursor工作流1", UID, msg_seq=45)
        left = self._reap(revived, [shell, revived])
        self.assertNotIn("s1", left)   # 空壳收掉
        self.assertIn("s2", left)      # 原对话留着

    def test_standalone_new_task_does_not_reap_anything(self):
        # 单独新任务：报到的会话自己就没什么历史（msg_seq 小）→ 压根不进收壳逻辑
        fresh = _s("s2", "newid2", "待命·cursor工作流", UID, msg_seq=2)
        other_shell = _s("s1", "newid", "待命·cursor工作流", UID, msg_seq=2)
        left = self._reap(fresh, [other_shell, fresh])
        self.assertIn("s1", left)
        self.assertIn("s2", left)

    def test_shell_with_real_work_is_kept(self):
        # 用户后来在这个待命 tab 里另派了活（有历史）→ 不是空壳，留着
        busy = _s("s1", "newid", "待命·cursor工作流", UID, msg_seq=20)
        revived = _s("s2", "origid", "cursor工作流1", UID, msg_seq=45)
        left = self._reap(revived, [busy, revived])
        self.assertIn("s1", left)

    def test_shell_in_another_window_is_untouched(self):
        # 不同 Cursor 窗口（不同 uuid）的空壳绝不能被牵连
        other = _s("s1", "newid", "待命·别的", "other-uuid", msg_seq=2)
        revived = _s("s2", "origid", "cursor工作流1", UID, msg_seq=45)
        left = self._reap(revived, [other, revived])
        self.assertIn("s1", left)

    def test_shell_with_pending_question_is_kept(self):
        shell = _s("s1", "newid", "待命·x", UID, msg_seq=2, pending={"id": "q"})
        revived = _s("s2", "origid", "cursor工作流1", UID, msg_seq=45)
        left = self._reap(revived, [shell, revived])
        self.assertIn("s1", left)

    def test_checkin_pending_shell_on_same_window_is_reaped(self):
        # 08-14：活着的待命永远卡在「已就位 / 开始任务|结束」上。这不是真提问，
        # 同窗口接手落地必须把壳收掉，否则 rxyy tools 复活后待命 tab 一直留着。
        shell = _s("s1", "newid", "待命·cursor工作流", UID, msg_seq=2, pending={
            "id": "q-checkin",
            "message": "📍 cursor工作流（d:\\x）· 对话 abcd1234 已就位",
            "options": ["开始任务", "结束"],
        })
        revived = _s("s2", "origid", "rxyy tools·主题", UID, msg_seq=45)
        left = self._reap(revived, [shell, revived])
        self.assertNotIn("s1", left)
        self.assertIn("s2", left)
        self.assertIsNone(shell.pending)

    def test_start_task_clicked_shell_same_window_is_reaped(self):
        # 点过「开始任务」的壳同窗口接手仍该收。信封闸（is_checkin_envelope）
        # 生效后点「开始任务」不再写 claimed_ts——claimed_ts=0 就是它现在的
        # 真实形状；claimed>0 的会话按 08-26 规则（73004179 事故）绝不收。
        shell = _s("s1", "newid", "Persistent Plus check-in", UID, msg_seq=3)
        shell.shell_born = True
        shell.claimed_task_ts = 0.0
        shell.agent_named = False
        revived = _s("s2", "origid", "rxyy tools·MCP长连", UID, msg_seq=45)
        left = self._reap(revived, [shell, revived])
        self.assertNotIn("s1", left)

    def test_claimed_shell_same_window_survives_reap(self):
        # 08-26（73004179）：同窗口 + 出生是壳，但用户派过真活（claimed_ts）
        # ——绝不收。收了就是把正在干活的会话归并进别人 tab。
        shell = _s("s1", "73004179", "切换到这个项目：playthread-go…", UID,
                   msg_seq=3)
        shell.shell_born = True
        shell.claimed_task_ts = 1.0
        shell.agent_named = False
        revived = _s("s2", "b4eff2ee", "rxyy tools·全面体检", UID, msg_seq=45)
        left = self._reap(revived, [shell, revived])
        self.assertIn("s1", left)

    def test_new_window_shell_reaped_when_its_composer_has_orig_id(self):
        # 复制接手词贴进新窗口且 agent 已用原 ID 调了 zhi/zt：
        # 现任 uuid 停在旧对话，壳 uuid 才是新窗口。matcher 由 tool_arg 判定。
        shell = _s("s1", "f099d23b", "Persistent Plus check-in·d23b",
                   "new-uuid", msg_seq=3)
        shell.shell_born = True
        shell.claimed_task_ts = 0.0
        shell.agent_named = False
        revived = _s("s2", "0d07cce5", "rxyy tools·MCP长连", "old-uuid",
                     msg_seq=45)
        with patch.object(hub, "composer_mentions_conv",
                          lambda uid, conv, **k: (
                              uid == "new-uuid" and conv == "0d07cce5"
                              and k.get("param_context") == "tool_arg")):
            left = self._reap(revived, [shell, revived])
        self.assertNotIn("s1", left)

    def test_pasted_takeover_prompt_without_tool_call_does_not_reap(self):
        # 把接手提示词贴进壳当上下文、agent 仍用壳 ID 说话：composer 里只有
        # 提示词正文的 conversation_id=「原ID」，tool_arg 不匹配，不得收壳。
        shell = _s("s1", "28172cf0", "待命·cursor工作流·2cf0",
                   "new-uuid", msg_seq=3)
        shell.shell_born = True
        shell.claimed_task_ts = 0.0
        shell.agent_named = False
        revived = _s("s2", "050f09d2", "心理评测·任务统筹", "old-uuid",
                     msg_seq=45)
        with patch.object(hub, "composer_mentions_conv",
                          lambda uid, conv, **k: False), \
             patch.object(hub, "transcript_has_tool_arg", return_value=False), \
             patch.object(hub, "jsonl_path_for_uuid", return_value=None):
            left = self._reap(revived, [shell, revived])
        self.assertIn("s1", left)

    def test_new_window_shell_reaped_when_jsonl_has_tool_arg(self):
        # 08-15：composer 气泡没有 MCP JSON，但壳窗口 jsonl 里有
        # CallMcpTool arguments.conversation_id = 原 ID → 该收。
        shell = _s("s1", "e190ed54", "待命·cursor工作流·ed54",
                   "new-uuid", msg_seq=3)
        shell.shell_born = True
        shell.cwd = r"d:\proj"
        shell.claimed_task_ts = 0.0
        shell.agent_named = False
        revived = _s("s2", "0d07cce5", "rxyy tools·MCP长连", "old-uuid",
                     msg_seq=45)
        with patch.object(hub, "composer_mentions_conv",
                          lambda uid, conv, **k: False), \
             patch.object(hub, "transcript_has_tool_arg",
                          lambda tp, cid, **k: cid == "0d07cce5"), \
             patch.object(hub, "jsonl_path_for_uuid",
                          return_value=r"C:\t\new-uuid.jsonl"):
            left = self._reap(revived, [shell, revived])
        self.assertNotIn("s1", left)

    def test_queued_redelivery_does_not_block_reap(self):
        # 08-15 现网：壳队列卡着 who=None 的「用户级规则」补送，
        # _may_take_the_reply_slot(None) 为真，四个派接手壳收不掉。
        prompt = (
            hub.TAKEOVER_PROMPT_HEAD + "\n"
            "conversation_id=「0d07cce5」\n"
        )
        shell = _s("s1", "e190ed54", "待命·cursor工作流·ed54", "new-uuid", msg_seq=2)
        shell.shell_born = True
        shell.queued = [{
            "id": "q1",
            "text": "[补送|未送达] 【用户级规则 · 必读必守 · 本对话只发这一次】先 Read v19",
            "who": None,
            "redelivery": True,
        }]
        shell.messages = [{"role": "user", "html": "<pre>%s</pre>" % prompt}]
        revived = _s("s2", "0d07cce5", "rxyy tools·MCP长连", "old-uuid", msg_seq=45)
        with patch.object(hub, "composer_mentions_conv", return_value=False), \
             patch.object(hub, "transcript_has_tool_arg", return_value=False):
            left = self._reap(revived, [shell, revived])
        self.assertNotIn("s1", left)
        self.assertIn("s2", left)

    def test_queued_fresh_user_task_still_blocks_reap(self):
        shell = _s("s1", "e190ed54", "待命·cursor工作流·ed54", "new-uuid", msg_seq=2)
        shell.shell_born = True
        shell.queued = [{"id": "q1", "text": "另外帮我开个新功能", "who": None}]
        revived = _s("s2", "0d07cce5", "rxyy tools·MCP长连", "old-uuid", msg_seq=45)
        with patch.object(hub, "composer_mentions_conv", return_value=False), \
             patch.object(hub, "transcript_has_tool_arg", return_value=False):
            left = self._reap(revived, [shell, revived])
        self.assertIn("s1", left)

    def test_no_uuid_no_reap(self):
        shell = _s("s1", "newid", "待命·x", None, msg_seq=2)
        revived = _s("s2", "origid", "cursor工作流1", None, msg_seq=45)
        left = self._reap(revived, [shell, revived])
        self.assertIn("s1", left)

    def test_dispatched_shell_reaped_when_orig_revives_without_uuid(self):
        # 08-15：四个待命壳被派去接手，原会话自己复活。不同窗口，uuid 对不上，
        # 但提示词点名了原 ID → 必须收壳。
        prompt = (
            hub.TAKEOVER_PROMPT_HEAD + "，把它没做完的工作继续完成。\n\n"
            "现在立刻用 conversation_id=「76a1cf4d」调一次 zt\n"
            "- conversation_id：76a1cf4d\n"
        )
        shell = _s("s1", "34892f47", "待命·cursor工作流·2f47", "new-uuid", msg_seq=2)
        shell.shell_born = True
        shell.messages = [{"role": "user", "html": "<pre class='plain'>%s</pre>" % prompt}]
        revived = _s("s2", "76a1cf4d", "录播·紧急插播", "old-uuid", msg_seq=45)
        with patch.object(hub, "composer_mentions_conv", return_value=False), \
             patch.object(hub, "transcript_has_tool_arg", return_value=False):
            left = self._reap(revived, [shell, revived])
        self.assertNotIn("s1", left)
        self.assertIn("s2", left)

    def test_dispatched_shell_for_other_id_is_kept(self):
        prompt = (
            hub.TAKEOVER_PROMPT_HEAD + "\n"
            "conversation_id=「aaaaaaaa」\n"
        )
        shell = _s("s1", "34892f47", "待命·cursor工作流·2f47", "new-uuid", msg_seq=2)
        shell.shell_born = True
        shell.messages = [{"role": "user", "html": "<pre>%s</pre>" % prompt}]
        revived = _s("s2", "76a1cf4d", "录播·紧急插播", "old-uuid", msg_seq=45)
        with patch.object(hub, "composer_mentions_conv", return_value=False), \
             patch.object(hub, "transcript_has_tool_arg", return_value=False):
            left = self._reap(revived, [shell, revived])
        self.assertIn("s1", left)


class TakeoverPromptTargetTests(unittest.TestCase):
    def test_plain_and_html(self):
        text = hub.TAKEOVER_PROMPT_HEAD + "\nconversation_id=「76a1cf4d」"
        self.assertEqual("76a1cf4d", hub.takeover_prompt_target(text))
        html = "<pre class='plain'>%s</pre>" % text
        self.assertEqual("76a1cf4d", hub.takeover_prompt_target(html))
        self.assertIsNone(hub.takeover_prompt_target("普通派活 conversation_id=「76a1cf4d」"))


class SweepRevivedTakeoverShellsTests(unittest.TestCase):
    def test_sweep_closes_dispatched_shells_when_orig_is_alive(self):
        prompt = hub.TAKEOVER_PROMPT_HEAD + "\nconversation_id=「76a1cf4d」"
        shell = _s("s1", "34892f47", "待命·cursor工作流·2f47", "new-uuid", msg_seq=2)
        shell.shell_born = True
        shell.real_seq = 2
        shell.messages = [{"role": "user", "html": "<pre>%s</pre>" % prompt}]
        live = _s("s2", "76a1cf4d", "录播·紧急插播", "old-uuid", msg_seq=45)
        live.real_seq = 9
        d = {"s1": shell, "s2": live}
        with (patch.object(hub.HUB, "sessions", d),
              patch.object(hub.HUB, "order", ["s1", "s2"]),
              patch.object(hub.HUB, "log_end", lambda s, why: None),
              patch.object(hub.HUB, "save_state", lambda: None),
              patch.object(hub.HUB, "takeover_aliases", {}),
              patch.object(hub.HUB, "takeover_ledger", {}, create=True),
              patch.object(hub.HUB, "name_tombstones", {}),
              patch.object(hub.Hub, "_save_takeover_aliases", lambda self: None),
              patch.object(hub, "log_event", lambda *a, **k: None)):
            hub.HUB._takeover_sweep_ts = 0
            hub.HUB.sweep_revived_takeover_shells()
        self.assertNotIn("s1", d)
        self.assertIn("s2", d)


if __name__ == "__main__":
    unittest.main()
