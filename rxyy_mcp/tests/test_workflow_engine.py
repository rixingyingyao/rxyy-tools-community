# -*- coding: utf-8 -*-
"""工作流编排引擎：派单、黑板信号推进、卡住、超时、重派、跨重启接续。

信号协议（与接入纪律共生）：步骤完成 = 黑板「收工/提交」，失败 = 「卡住/事故」。
带「工作流#<id>」标记的谁写都认；不带标记的只认被派的那个 agent 本人。
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

from workflow import WorkflowEngine  # noqa: E402


class Hooks:
    """可检查的假钩子：记录派话/拉起/通知，模拟在线名册。"""

    def __init__(self, agents=None, spawn_ok=True):
        self.agents = list(agents or [])
        self.sent = []       # (conv, prompt)
        self.spawned = []    # (root, task_name, prompt)
        self.notices = []    # (title, body)
        self.spawn_ok = spawn_ok
        self.send_fail_convs = set()
        self.builtin = {}    # runner key -> {"status":…, "note":…}（内置巡检假注册表）

    def as_dict(self):
        return {
            "team_agents": lambda root: [dict(a) for a in self.agents],
            "find_conv": self._find,
            "send_to_conv": self._send,
            "spawn_builtin": self._spawn,
            "builtin_status": lambda key: self.builtin.get(key),
            "notify_user": lambda t, b: self.notices.append((t, b)),
            "log": lambda t: None,
        }

    def _find(self, prefix):
        for a in self.agents:
            if a["conv"].startswith(prefix):
                return {"conv": a["conv"], "label": a.get("label") or a["conv"]}
        return None

    def _send(self, conv, text):
        if conv in self.send_fail_convs:
            return {"ok": False, "error": "离线"}
        self.sent.append((conv, text))
        return {"ok": True, "label": "标签-" + conv[:4]}

    def _spawn(self, root, task_name, prompt):
        self.spawned.append((root, task_name, prompt))
        if not self.spawn_ok:
            return {"ok": False, "error": "Cursor CLI 未安装"}
        return {"ok": True, "conversation_id": "spawned0", "key": "bikey001"}


def agent(conv, label="", role="", waiting=False, shell=False, seq=0):
    return {"conv": conv, "label": label or conv, "role": role,
            "waiting": waiting, "shell": shell, "seq": seq}


class WorkflowEngineTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.path = Path(self.td.name) / "workflows.json"

    def tearDown(self):
        self.td.cleanup()

    def _engine(self, hooks):
        return WorkflowEngine(self.path, hooks.as_dict())

    def _two_step(self, eng, executor1="auto", executor2="auto", timeout_min=45):
        r = eng.create("登录功能", r"d:\proj", [
            {"title": "实现", "detail": "写代码", "executor": executor1},
            {"title": "审查", "detail": "只审不改", "executor": executor2},
        ], timeout_min=timeout_min)
        self.assertTrue(r["ok"], r)
        return r["id"]

    # ---------- 派单 ----------
    def test_start_dispatches_to_waiting_agent_first(self):
        h = Hooks(agents=[agent("aaaa1111", seq=9),
                          agent("bbbb2222", waiting=True, seq=50),
                          agent("cccc3333", shell=True, seq=0)])
        eng = self._engine(h)
        wid = self._two_step(eng)
        r = eng.start(wid)
        self.assertTrue(r["ok"], r)
        # 正等回复的优先于待命壳和低 seq
        self.assertEqual("bbbb2222", h.sent[0][0])
        self.assertIn("工作流#" + wid, h.sent[0][1], "派单提示词必须带收工标记写法")
        self.assertIn("第 1/2 步", h.sent[0][1])

    def test_no_candidate_marks_stuck_with_readable_reason(self):
        h = Hooks(agents=[])
        eng = self._engine(h)
        wid = self._two_step(eng)
        r = eng.start(wid)
        self.assertFalse(r["ok"])
        wf = eng.snapshot()[0]
        self.assertEqual("stuck", wf["status"])
        self.assertTrue(h.notices, "卡住必须提醒用户")

    def test_role_executor_filters_by_role(self):
        h = Hooks(agents=[agent("aaaa1111", role="impl", waiting=True),
                          agent("bbbb2222", role="review")])
        eng = self._engine(h)
        wid = self._two_step(eng, executor1="role:review")
        eng.start(wid)
        self.assertEqual("bbbb2222", h.sent[0][0])

    def test_conv_executor_uses_find_conv(self):
        h = Hooks(agents=[agent("aaaa1111"), agent("bbbb2222")])
        eng = self._engine(h)
        wid = self._two_step(eng, executor1="conv:bbbb")
        eng.start(wid)
        self.assertEqual("bbbb2222", h.sent[0][0])

    def test_builtin_executor_spawns_local_agent(self):
        h = Hooks(agents=[])
        eng = self._engine(h)
        wid = self._two_step(eng, executor1="builtin")
        r = eng.start(wid)
        self.assertTrue(r["ok"], r)
        self.assertEqual(1, len(h.spawned))
        root, task_name, prompt = h.spawned[0]
        self.assertEqual(r"d:\proj", root)
        self.assertIn("工作流#" + wid, prompt)
        wf = eng.snapshot()[0]
        self.assertEqual("spawned0", wf["steps"][0]["assigned_conv"])

    def test_auto_skips_agents_busy_with_other_workflows(self):
        h = Hooks(agents=[agent("aaaa1111", waiting=True),
                          agent("bbbb2222", waiting=True, seq=99)])
        eng = self._engine(h)
        w1 = self._two_step(eng)
        eng.start(w1)                      # aaaa1111 被 w1 占用
        w2 = self._two_step(eng)
        eng.start(w2)
        self.assertEqual("bbbb2222", h.sent[1][0], "第二条链不该压给同一个执行人")

    # ---------- 信号推进 ----------
    def test_marked_shouGong_completes_and_dispatches_next(self):
        h = Hooks(agents=[agent("aaaa1111", waiting=True), agent("bbbb2222")])
        eng = self._engine(h)
        wid = self._two_step(eng)
        eng.start(wid)
        note = eng.on_board_post("aaaa1111", r"d:\proj", "收工",
                                 "工作流#{} 第1步完成：登录接口已实现".format(wid))
        self.assertIn("推进到第2步", note or "")
        wf = eng.snapshot()[0]
        self.assertEqual("done", wf["steps"][0]["status"])
        self.assertIn("登录接口已实现", wf["steps"][0]["result"])
        self.assertEqual("running", wf["steps"][1]["status"])
        self.assertEqual(2, len(h.sent))
        self.assertIn("上一步交付", h.sent[1][1], "下一步提示词要带上一步结果")

    def test_marker_lets_anyone_report_for_the_step(self):
        # 负责人代执行人收工：带标记就认
        h = Hooks(agents=[agent("aaaa1111", waiting=True), agent("bbbb2222")])
        eng = self._engine(h)
        wid = self._two_step(eng)
        eng.start(wid)
        note = eng.on_board_post("zzzz9999", r"d:\elsewhere", "提交",
                                 "工作流#{} 第1步完成".format(wid))
        self.assertIn("推进", note or "")

    def test_unmarked_shouGong_from_assigned_agent_still_counts(self):
        h = Hooks(agents=[agent("aaaa1111", waiting=True), agent("bbbb2222")])
        eng = self._engine(h)
        wid = self._two_step(eng)
        eng.start(wid)
        note = eng.on_board_post("aaaa1111", r"d:\proj", "收工", "第一步做完了（忘了带标记）")
        self.assertIn("推进到第2步", note or "")

    def test_unmarked_post_from_stranger_is_ignored(self):
        h = Hooks(agents=[agent("aaaa1111", waiting=True)])
        eng = self._engine(h)
        wid = self._two_step(eng)
        eng.start(wid)
        note = eng.on_board_post("bbbb2222", r"d:\proj", "收工", "我干完别的活了")
        self.assertIsNone(note)
        self.assertEqual("running", eng.snapshot()[0]["steps"][0]["status"])

    def test_irrelevant_kinds_do_not_advance(self):
        h = Hooks(agents=[agent("aaaa1111", waiting=True)])
        eng = self._engine(h)
        wid = self._two_step(eng)
        eng.start(wid)
        note = eng.on_board_post("aaaa1111", r"d:\proj", "大改",
                                 "工作流#{} 准备动 hub.py".format(wid))
        self.assertIsNone(note)
        self.assertEqual("running", eng.snapshot()[0]["steps"][0]["status"])

    def test_chain_completion_notifies_user(self):
        h = Hooks(agents=[agent("aaaa1111", waiting=True), agent("bbbb2222")])
        eng = self._engine(h)
        wid = self._two_step(eng)
        eng.start(wid)
        eng.on_board_post("aaaa1111", r"d:\proj", "收工", "工作流#{} 第1步完成".format(wid))
        eng.on_board_post("", "", "收工", "工作流#{} 第2步完成：审查通过".format(wid))
        wf = eng.snapshot()[0]
        self.assertEqual("done", wf["status"])
        self.assertTrue(any("全链完成" in t for t, _ in h.notices))

    def test_kaZhu_marks_stuck_and_notifies(self):
        h = Hooks(agents=[agent("aaaa1111", waiting=True)])
        eng = self._engine(h)
        wid = self._two_step(eng)
        eng.start(wid)
        note = eng.on_board_post("aaaa1111", r"d:\proj", "卡住",
                                 "工作流#{} 依赖装不上".format(wid))
        self.assertIn("卡住", note or "")
        wf = eng.snapshot()[0]
        self.assertEqual("stuck", wf["status"])
        self.assertTrue(any("卡住" in t for t, _ in h.notices))

    # ---------- 暂停 / 手动推进 / 重派 ----------
    def test_paused_records_completion_but_waits_for_user(self):
        h = Hooks(agents=[agent("aaaa1111", waiting=True), agent("bbbb2222")])
        eng = self._engine(h)
        wid = self._two_step(eng)
        eng.start(wid)
        eng.pause(wid)
        eng.on_board_post("aaaa1111", r"d:\proj", "收工", "工作流#{} 完成".format(wid))
        wf = eng.snapshot()[0]
        self.assertEqual("done", wf["steps"][0]["status"], "暂停期间完成也要记账")
        self.assertEqual(1, len(h.sent), "暂停期间不许派下一步")
        eng.start(wid)
        self.assertEqual(2, len(h.sent), "继续后补派下一步")

    def test_force_done_advances_without_signal(self):
        h = Hooks(agents=[agent("aaaa1111", waiting=True), agent("bbbb2222")])
        eng = self._engine(h)
        wid = self._two_step(eng)
        eng.start(wid)
        r = eng.force_done(wid, note="我看着已经好了")
        self.assertTrue(r["ok"])
        wf = eng.snapshot()[0]
        self.assertIn("用户手动确认完成", wf["steps"][0]["result"])
        self.assertEqual("running", wf["steps"][1]["status"])

    def test_redispatch_with_new_executor(self):
        h = Hooks(agents=[agent("aaaa1111", waiting=True), agent("bbbb2222")])
        eng = self._engine(h)
        wid = self._two_step(eng)
        eng.start(wid)
        h.send_fail_convs.add("aaaa1111")
        r = eng.redispatch(wid, executor="conv:bbbb")
        self.assertTrue(r["ok"], r)
        self.assertEqual("bbbb2222", h.sent[-1][0])
        self.assertEqual("running", eng.snapshot()[0]["status"])

    # ---------- 超时 / 持久化 ----------
    def test_timeout_tick_marks_stuck_once(self):
        h = Hooks(agents=[agent("aaaa1111", waiting=True)])
        eng = self._engine(h)
        wid = self._two_step(eng, timeout_min=1)
        eng.start(wid)
        eng.tick(now=time.time() + 120)
        wf = eng.snapshot()[0]
        self.assertEqual("stuck", wf["status"])
        n = len(h.notices)
        eng.tick(now=time.time() + 240)
        self.assertEqual(n, len(h.notices), "标卡后不该反复轰炸提醒")
        self.assertEqual(wid, wf["id"])

    def test_zero_timeout_means_never_stuck_by_time(self):
        h = Hooks(agents=[agent("aaaa1111", waiting=True)])
        eng = self._engine(h)
        wid = self._two_step(eng, timeout_min=0)
        eng.start(wid)
        eng.tick(now=time.time() + 86400)
        self.assertEqual("running", eng.snapshot()[0]["status"])

    def test_state_survives_restart(self):
        h = Hooks(agents=[agent("aaaa1111", waiting=True), agent("bbbb2222")])
        eng = self._engine(h)
        wid = self._two_step(eng)
        eng.start(wid)
        # 新引擎实例 = hub 重启；执行人交付的信号照样被认、照样推进
        eng2 = self._engine(h)
        note = eng2.on_board_post("aaaa1111", r"d:\proj", "收工",
                                  "工作流#{} 第1步完成".format(wid))
        self.assertIn("推进到第2步", note or "")
        wf = eng2.snapshot()[0]
        self.assertEqual("done", wf["steps"][0]["status"])

    # ---------- 执行人死亡联动 ----------
    def test_executor_death_marks_stuck_immediately(self):
        h = Hooks(agents=[agent("aaaa1111", waiting=True)])
        eng = self._engine(h)
        wid = self._two_step(eng)
        eng.start(wid)
        note = eng.on_executor_dead("aaaa1111", "账号欠费（今天第 6 次）")
        self.assertIn("已标卡", note or "")
        wf = eng.snapshot()[0]
        self.assertEqual("stuck", wf["status"])
        self.assertEqual("stuck", wf["steps"][0]["status"])
        self.assertTrue(any("账号欠费" in b for _, b in h.notices),
                        "提醒里要带死因，用户才知道是付钱还是换人")

    def test_executor_death_of_stranger_is_ignored(self):
        h = Hooks(agents=[agent("aaaa1111", waiting=True)])
        eng = self._engine(h)
        wid = self._two_step(eng)
        eng.start(wid)
        self.assertIsNone(eng.on_executor_dead("zzzz9999", "账号欠费"))
        self.assertEqual("running", eng.snapshot()[0]["status"])

    def test_executor_death_while_paused_still_marks_stuck(self):
        # 暂停只是不派下一步；当前步执行人死了照样要让用户知道
        h = Hooks(agents=[agent("aaaa1111", waiting=True)])
        eng = self._engine(h)
        wid = self._two_step(eng)
        eng.start(wid)
        eng.pause(wid)
        note = eng.on_executor_dead("aaaa1111", "对话已从 Cursor 里消失")
        self.assertIn("已标卡", note or "")
        self.assertEqual("stuck", eng.snapshot()[0]["status"])

    def test_executor_death_after_step_done_is_ignored(self):
        # 干完活才死的不算：链已推进，别把下一步执行人的活标卡
        h = Hooks(agents=[agent("aaaa1111", waiting=True), agent("bbbb2222")])
        eng = self._engine(h)
        wid = self._two_step(eng, executor2="conv:bbbb")
        eng.start(wid)
        eng.on_board_post("aaaa1111", r"d:\proj", "收工",
                          "工作流#{} 第1步完成".format(wid))
        self.assertIsNone(eng.on_executor_dead("aaaa1111", "账号欠费"))
        self.assertEqual("running", eng.snapshot()[0]["status"])

    # ---------- 内置 runner 巡检 ----------
    def _builtin_running(self, h=None):
        h = h or Hooks(agents=[])
        eng = self._engine(h)
        wid = self._two_step(eng, executor1="builtin", timeout_min=0)
        r = eng.start(wid)
        self.assertTrue(r["ok"], r)
        return h, eng, wid

    def test_builtin_runner_error_marks_stuck(self):
        h, eng, wid = self._builtin_running()
        h.builtin["bikey001"] = {"status": "error", "note": "Cursor CLI 未登录"}
        eng.tick(now=time.time() + 30)
        wf = eng.snapshot()[0]
        self.assertEqual("stuck", wf["status"])
        self.assertTrue(any("Cursor CLI 未登录" in b for _, b in h.notices))

    def test_builtin_finished_without_signal_marks_stuck_after_grace(self):
        h, eng, wid = self._builtin_running()
        h.builtin["bikey001"] = {"status": "finished", "note": ""}
        t0 = time.time()
        eng.tick(now=t0 + 30)     # 首见 finished：只记时刻，进入宽限
        self.assertEqual("running", eng.snapshot()[0]["status"])
        eng.tick(now=t0 + 60)     # 宽限内（30s < 90s）
        self.assertEqual("running", eng.snapshot()[0]["status"])
        eng.tick(now=t0 + 300)    # 宽限已过还没收工信号
        wf = eng.snapshot()[0]
        self.assertEqual("stuck", wf["status"])
        self.assertTrue(any("没写黑板收工" in b for _, b in h.notices))

    def test_builtin_running_healthy_not_disturbed(self):
        h, eng, wid = self._builtin_running()
        h.builtin["bikey001"] = {"status": "running", "note": ""}
        eng.tick(now=time.time() + 3600)
        self.assertEqual("running", eng.snapshot()[0]["status"],
                         "runner 活着且 timeout_min=0 时不许打扰")

    def test_redispatch_clears_stale_builtin_key(self):
        # 内置卡住后换成在线 agent 重派：旧 runner 的死活不该再牵连新执行人
        h, eng, wid = self._builtin_running(
            Hooks(agents=[agent("cccc3333", waiting=True)]))
        h.builtin["bikey001"] = {"status": "error", "note": "CLI 崩了"}
        eng.tick(now=time.time() + 30)
        self.assertEqual("stuck", eng.snapshot()[0]["status"])
        r = eng.redispatch(wid, executor="conv:cccc")
        self.assertTrue(r["ok"], r)
        eng.tick(now=time.time() + 90)
        wf = eng.snapshot()[0]
        self.assertEqual("running", wf["status"])
        self.assertNotIn("builtin_key", wf["steps"][0])


if __name__ == "__main__":
    unittest.main()
