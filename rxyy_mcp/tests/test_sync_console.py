# -*- coding: utf-8 -*-
"""双活 P2 业务线验收：看板 / 任务安排站 / 会话控制台列表的差量 emit + 幂等应用。

对应 sync_console.py 门头的约定：
- 首跑重播存量（与 P1 配置线相反）：两机存量本来不同，应用侧是加法语义
- 看板：标量 LWW（行内 updated_at）+ 事件流并集——断连期间两边的评论都不丢
- 任务站：整体 LWW + 真删除事件；删除撞上更新的本地编辑时编辑赢
- 会话列表：单机单写整表投影，对端落只读副本；messages/草稿绝不进事件
"""
import base64
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import sync_console  # noqa: E402
import sync_wf  # noqa: E402
import syncbus  # noqa: E402

REPO_ROOT = MODULE_DIR.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BST = _load("_t_board_storage", "console/api/board/storage.py")
TST = _load("_t_task_storage", "console/api/taskstage/storage.py")


def _card(cid, title, updated_at, events=None, **extra):
    base = {"id": cid, "title": title, "desc": "", "project": "rxyy tools",
            "priority": "normal", "labels": [], "status": "todo",
            "assignee": None, "review": None, "archived": False,
            "blocked_by": [], "version": 1, "created_at": 100.0,
            "updated_at": updated_at, "last_activity": "",
            "events": events or [{"ts": 100.0, "kind": "create",
                                  "conversation_id": "c0", "text": "建卡"}]}
    base.update(extra)
    return base


def _task(tid, title, updated_at, **extra):
    base = {"id": tid, "title": title, "description": "", "project": "p",
            "repo": "", "requester": "同事", "priority": "normal",
            "status": "draft", "tags": [], "images": [], "files": [],
            "order": 0, "created_at": 100.0, "updated_at": updated_at,
            "dispatched_at": 0.0, "dispatch_note": "", "session_name": "",
            "dispatch_session_id": "", "dispatch_conv_key": "",
            "dispatch_qid": "", "dispatch_direct": False, "dispatch_batch": ""}
    base.update(extra)
    return base


class _Rig:
    """A=company、B=home 共用一个事件夹；线程一律不起，手动打拍。"""

    def __init__(self, td):
        td = Path(td)
        self.alerts = []
        self.root = td / "syncfolder"
        # 生产里 hub 的 DATA_DIR 一定存在；rig 里的两个「机器态目录」得自己建
        # （applied 台账是 P0 的代码，落盘不建目录，测试别逼它改行为）
        (td / "a").mkdir(parents=True, exist_ok=True)
        (td / "b").mkdir(parents=True, exist_ok=True)
        self.bus_a = syncbus.SyncBus(self.root, "company", td / "a",
                                     alert=self.alerts.append)
        self.bus_b = syncbus.SyncBus(self.root, "home", td / "b",
                                     alert=self.alerts.append)
        self.board_a = BST.BoardStorage(td / "a" / "board")
        self.board_b = BST.BoardStorage(td / "b" / "board")
        self.task_a = TST.TaskStageStorage(td / "a" / "taskstage")
        self.task_b = TST.TaskStageStorage(td / "b" / "taskstage")
        self.sess_a = td / "a" / ".sessions.json"
        self.sess_b = td / "b" / ".sessions.json"
        self.cs_a = sync_console.ConsoleSync(
            self.bus_a, {}, td / "a", self.sess_a, alert=self.alerts.append,
            board_getter=lambda: self.board_a, task_getter=lambda: self.task_a)
        self.cs_b = sync_console.ConsoleSync(
            self.bus_b, {}, td / "b", self.sess_b, alert=self.alerts.append,
            board_getter=lambda: self.board_b, task_getter=lambda: self.task_b)

    def pump(self, rounds=8):
        """双向回放直到一整圈无动静（并集合并要多跑一圈才收敛，见门头 2）。"""
        for _ in range(rounds):
            moved = self.cs_a.poll_once()
            moved += self.bus_b.replayer.poll_once()
            moved += self.cs_b.poll_once()
            moved += self.bus_a.replayer.poll_once()
            if not moved:
                return
        raise AssertionError("回放 {} 圈仍未收敛".format(rounds))


class StockTests(unittest.TestCase):
    def test_first_poll_replays_stock_and_peer_backfills(self):
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            rig.board_a.add_card(_card("aaa1", "A 机的存量卡", 200.0))
            rig.task_a.upsert_task_replica(_task("t-a1", "A 机的存量任务", 200.0))
            self.assertGreaterEqual(rig.cs_a.poll_once(), 2,
                                    "首跑要把存量重播出去（与配置线相反）")
            rig.bus_b.replayer.poll_once()
            self.assertIsNotNone(rig.board_b.find("aaa1"), "对端要补插缺的卡")
            got = rig.task_b.find("t-a1")
            self.assertEqual("A 机的存量任务", got["title"])
            self.assertEqual(200.0, got["updated_at"],
                             "副本要保留对端行内时间，不许洗成应用时刻")

    def test_baseline_survives_restart_without_reemitting(self):
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            rig.board_a.add_card(_card("aaa1", "卡", 200.0))
            rig.cs_a.poll_once()
            again = sync_console.ConsoleSync(
                rig.bus_a, {}, Path(td) / "a", rig.sess_a,
                board_getter=lambda: rig.board_a,
                task_getter=lambda: rig.task_a)
            self.assertEqual(0, again.poll_once(), "重启不重播存量")


class BoardTests(unittest.TestCase):
    def test_equal_workflow_clock_uses_stable_content_tie_break(self):
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            local = _card("aaa1", "卡", 100.0, status="in_progress",
                          assignee={"conversation_id": "left", "agent_type": "codex", "tab_name": ""},
                          workflow_updated_at=400.0)
            remote = _card("aaa1", "卡", 100.0, status="in_review",
                           assignee={"conversation_id": "right", "agent_type": "cursor", "tab_name": ""},
                           workflow_updated_at=400.0)
            rig.board_b.upsert_card_replica(local)
            payload = {"row_ts": 100.0, **remote}
            first = {"entity": "card:aaa1", "machine": "company", "ts": 100.0,
                     "payload": payload}
            second = {"entity": "card:aaa1", "machine": "home", "ts": 100.0,
                      "payload": payload}
            rig.cs_b.board.apply_event(first)
            once = rig.board_b.find("aaa1")
            rig.cs_b.board.apply_event(second)  # 回声换了 machine 也不得翻面
            twice = rig.board_b.find("aaa1")
            self.assertEqual(once["status"], twice["status"])
            self.assertEqual(once["assignee"], twice["assignee"])

    def test_newer_workflow_clock_is_saved_even_when_fields_match(self):
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            rig.board_b.upsert_card_replica(_card("aaa1", "卡", 100.0,
                                                   workflow_updated_at=200.0))
            remote = _card("aaa1", "卡", 100.0, workflow_updated_at=300.0)
            rig.cs_b.board.apply_event({"entity": "card:aaa1", "machine": "company",
                                        "ts": 100.0, "payload": {"row_ts": 100.0, **remote}})
            self.assertEqual(300.0, rig.board_b.find("aaa1")["workflow_updated_at"])

    def test_delivery_workflow_clock_beats_later_comment_and_title_edit(self):
        """旧 B 的评论/改标题不能把 A 已交付状态再盖回执行中。"""
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            rig.board_a.add_card(_card("aaa1", "初始", 100.0))
            rig.pump()
            delivered_events = [
                {"ts": 100.0, "kind": "create", "conversation_id": "c0", "text": "建卡"},
                {"ts": 200.0, "kind": "move", "conversation_id": "deliverer",
                 "text": "in_progress → in_review：已交付"},
            ]
            rig.board_a.upsert_card_replica(_card(
                "aaa1", "A 已交付", 200.0, events=delivered_events,
                status="in_review", assignee={"conversation_id": "deliverer",
                                                "agent_type": "codex", "tab_name": ""},
                workflow_updated_at=200.0))

            def old_b_comment(card):
                card["title"] = "B 的较晚标题编辑"
                card["events"].append({"ts": 300.0, "kind": "comment",
                                       "conversation_id": "old-b", "text": "旧端补评论"})

            rig.board_b.mutate("aaa1", None, old_b_comment)
            rig.pump()
            for store in (rig.board_a, rig.board_b):
                got = store.find("aaa1")
                self.assertEqual("in_review", got["status"])
                self.assertEqual("deliverer", got["assignee"]["conversation_id"])
                self.assertIn("旧端补评论", {e["text"] for e in got["events"]})

    def test_legacy_board_event_without_workflow_clock_is_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            legacy = _card("legacy", "旧客户端", 300.0)
            payload = dict(legacy)
            payload.pop("workflow_updated_at", None)
            rig.cs_b.board.apply_event({"entity": "card:legacy", "machine": "company",
                                        "ts": 300.0, "payload": {"row_ts": 300.0, **payload}})
            got = rig.board_b.find("legacy")
            self.assertEqual("旧客户端", got["title"])
            self.assertEqual(300.0, got["workflow_updated_at"])

    def test_comments_from_both_sides_merge_without_loss(self):
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            rig.board_a.add_card(_card("aaa1", "共同的卡", 200.0))
            rig.pump()
            # 断连期间两边各写一条评论（事件键刻意不同）
            rig.board_a.mutate("aaa1", None, lambda c: c["events"].append(
                {"ts": 300.0, "kind": "comment", "conversation_id": "ca",
                 "text": "公司机的评论"}))
            rig.board_b.mutate("aaa1", None, lambda c: c["events"].append(
                {"ts": 301.0, "kind": "comment", "conversation_id": "cb",
                 "text": "家机的评论"}))
            rig.pump()
            for store in (rig.board_a, rig.board_b):
                texts = {e["text"] for e in store.find("aaa1")["events"]}
                self.assertLessEqual({"公司机的评论", "家机的评论"}, texts,
                                     "并集合并两边的评论都不许丢")

    def test_scalar_conflict_converges_to_newer_side(self):
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            rig.board_a.add_card(_card("aaa1", "原名", 200.0))
            rig.pump()
            rig.board_a.upsert_card_replica(_card("aaa1", "公司机改的旧名", 400.0))
            rig.board_b.upsert_card_replica(_card("aaa1", "家机改的新名", 500.0))
            rig.pump()
            self.assertEqual("家机改的新名", rig.board_a.find("aaa1")["title"])
            self.assertEqual("家机改的新名", rig.board_b.find("aaa1")["title"])

    def test_applied_card_is_not_echoed_back(self):
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            rig.board_a.add_card(_card("aaa1", "卡", 200.0))
            rig.cs_a.poll_once()
            rig.cs_b.poll_once()          # B 先立自己的（空）基线
            rig.bus_b.replayer.poll_once()
            self.assertEqual(0, rig.cs_b.board.poll_once(),
                             "刚吃进来的卡不许再发回去（回声抑制）")

    def test_upsert_replica_replaces_not_duplicates(self):
        with tempfile.TemporaryDirectory() as td:
            store = BST.BoardStorage(Path(td) / "board")
            store.upsert_card_replica(_card("aaa1", "第一版", 200.0))
            store.upsert_card_replica(_card("aaa1", "第二版", 300.0))
            cards = store.list_cards()
            self.assertEqual(1, len(cards))
            self.assertEqual("第二版", cards[0]["title"])


class TaskTests(unittest.TestCase):
    def test_delete_propagates_to_peer(self):
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            rig.task_a.upsert_task_replica(_task("t1", "要删的任务", 200.0))
            rig.pump()
            self.assertIsNotNone(rig.task_b.find("t1"))
            rig.task_a.remove_task("t1")
            rig.pump()
            self.assertIsNone(rig.task_b.find("t1"), "删除要跟着事件走到对端")

    def test_delete_loses_to_newer_edit(self):
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            rig.task_a.upsert_task_replica(_task("t1", "任务", 200.0))
            rig.pump()
            # A 删除（emit 的 row_ts 是当下），B 在「更晚」编辑——把 B 行内
            # 时间拨到未来，保证 LWW 里编辑更新
            rig.task_a.remove_task("t1")
            rig.task_b.upsert_task_replica(_task("t1", "删除后还在改", 9.9e12))
            rig.pump()
            self.assertEqual("删除后还在改", rig.task_a.find("t1")["title"],
                             "后写的编辑要赢过先按的删除（两边都复活）")
            self.assertEqual("删除后还在改", rig.task_b.find("t1")["title"])

    def test_lww_older_incoming_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            rig.task_a.upsert_task_replica(_task("t1", "任务", 200.0))
            rig.pump()
            rig.task_b.upsert_task_replica(_task("t1", "家机新改", 500.0))
            rig.task_a.upsert_task_replica(_task("t1", "公司机旧改", 400.0))
            rig.pump()
            self.assertEqual("家机新改", rig.task_a.find("t1")["title"])
            self.assertEqual("家机新改", rig.task_b.find("t1")["title"])

    def test_dispatch_credentials_travel_verbatim(self):
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            rig.task_a.upsert_task_replica(_task(
                "t1", "已派的任务", 200.0, status="dispatched",
                dispatch_qid="q-123", dispatch_session_id="s-9",
                dispatch_conv_key="conv-1"))
            rig.pump()
            got = rig.task_b.find("t1")
            self.assertEqual("q-123", got["dispatch_qid"],
                             "凭据原样复制：剥掉它回声一圈会把原机的撤回凭据洗掉")
            self.assertEqual("s-9", got["dispatch_session_id"])


class SessTests(unittest.TestCase):
    def _write_sessions(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")

    def test_projection_lands_as_peer_file_without_messages(self):
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            self._write_sessions(rig.sess_a, [{
                "conv_key": "872e2b81", "name": "双活P2", "cwd": "d:\\x",
                "task_root": "d:\\x", "agent_project": "rxyy tools",
                "created_ts": 1.0, "shell_born": False, "agent_named": True,
                "cursor_title": "标题",
                "messages": [{"kind": "user", "text": "聊天记录不许出机"}],
                "draft_text": "草稿也不许"}])
            rig.cs_a.poll_once()
            rig.bus_b.replayer.poll_once()
            peers = rig.cs_b.sess.peers_snapshot()
            self.assertEqual(1, len(peers))
            self.assertEqual("company", peers[0]["machine"])
            row = peers[0]["sessions"][0]
            self.assertEqual("双活P2", row["name"])
            blob = json.dumps(peers, ensure_ascii=False)
            self.assertNotIn("聊天记录不许出机", blob)
            self.assertNotIn("草稿也不许", blob)

    def test_reemits_only_on_list_change(self):
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            self._write_sessions(rig.sess_a, [{
                "conv_key": "c1", "name": "名字", "created_ts": 1.0}])
            self.assertEqual(1, rig.cs_a.sess.poll_once(), "首跑发全量投影")
            self.assertEqual(0, rig.cs_a.sess.poll_once(), "没变化就不发")
            self._write_sessions(rig.sess_a, [{
                "conv_key": "c1", "name": "改名了", "created_ts": 1.0}])
            self.assertEqual(1, rig.cs_a.sess.poll_once())

    def test_forged_entity_from_wrong_machine_is_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            # 事件封装的 machine=company，却声称是 home 的列表：不落
            rig.bus_a.outbox.emit("sess", "update", "list:home",
                                  payload={"sessions": [{"name": "伪造"}]})
            rig.bus_b.replayer.poll_once()
            self.assertEqual([], rig.cs_b.sess.peers_snapshot())


class JiraWhitelistTests(unittest.TestCase):
    def test_dispatch_records_key_rides_the_kv_line(self):
        self.assertIn("jira_dispatch_records", sync_wf.KV_WHITELIST)
        self.assertIsNone(sync_wf.DENY_RE.search("jira_dispatch_records"),
                          "白名单键不许撞凭证硬闸")


def _fake_scp(body: bytes = None, rc: int = 0, stderr: bytes = b"",
              calls: list = None):
    """打桩 subprocess.run（照抄 test_sync_ferry 的织法）：rc=0 时把 body
    写进 scp 目标路径（cmd 末位）；calls 收集远端路径，验证「谁被拉过」。"""
    def run(cmd, **kw):
        assert cmd[0] == "scp" and "BatchMode=yes" in cmd
        if calls is not None:
            calls.append(cmd[-2])
        if rc == 0 and body is not None:
            Path(cmd[-1]).write_bytes(body)
        return types.SimpleNamespace(returncode=rc, stderr=stderr)
    return run


class AssetFerryTests(unittest.TestCase):
    """08-31 用户实拍：对端投的任务在本机任务站里缩略图全是破图——双活
    只摆元数据不摆二进制（_TASK_FIELDS 门头当时就交代过这条残留）。

    契约：出港区发布 + 按缺认领。文件名 = uuid+内容一次写死，所以「本地
    已有」即认领终点，无需 LWW；对端没出港/没开机只是退避，不是故障。
    """

    def _st(self, td, sub="ts"):
        return TST.TaskStageStorage(Path(td) / sub)

    def _ferry(self, st, td):
        return sync_console.TaskAssetFerry(
            lambda: st, str(Path(td) / "syncroot"), "company", "C:/peer-sync")

    def _task_with_ref(self, st, name, tid="t-img"):
        st.upsert_task_replica(_task(tid, "带图任务", 200.0,
                                     images=[{"id": "i1", "name": "截图.png",
                                              "file": name}]))

    def test_0831_a_referenced_image_sails_out_to_the_port(self):
        with tempfile.TemporaryDirectory() as td:
            st = self._st(td)
            meta = st.save_image_file(
                base64.b64encode(b"PNGDATA").decode(), "image/png")
            self._task_with_ref(st, meta["file"])
            f = self._ferry(st, td)
            with patch.object(sync_console.subprocess, "run",
                              _fake_scp()):        # 本地都齐，不许碰 scp
                f.poll_once()
            port = Path(td) / "syncroot" / "assets" / "taskstage" / meta["file"]
            self.assertEqual(b"PNGDATA", port.read_bytes())

    def test_0831_a_missing_image_is_claimed_from_the_peer(self):
        with tempfile.TemporaryDirectory() as td:
            st = self._st(td)
            name = "a" * 32 + ".png"
            self._task_with_ref(st, name)
            f = self._ferry(st, td)
            calls = []
            with patch.object(sync_console.subprocess, "run",
                              _fake_scp(b"IMGBYTES", calls=calls)):
                f.poll_once()
            self.assertEqual(b"IMGBYTES",
                             (Path(st.images_dir) / name).read_bytes())
            self.assertEqual(["company:C:/peer-sync/assets/taskstage/" + name],
                             calls)
            self.assertEqual(1, f.fetched)

    def test_0831_peer_not_yet_published_backs_off_per_image(self):
        with tempfile.TemporaryDirectory() as td:
            st = self._st(td)
            self._task_with_ref(st, "b" * 32 + ".png")
            f = self._ferry(st, td)
            calls = []
            stub = _fake_scp(rc=1, stderr=b"scp: no such file or directory",
                             calls=calls)
            with patch.object(sync_console.subprocess, "run", stub):
                f.poll_once()
                f.poll_once()   # 退避期内的第二拍不许再拉
            self.assertEqual(1, len(calls))
            self.assertEqual(1, f.status()["waiting"])
            self.assertEqual("", f.last_error, "对端还没出港不算故障")

    def test_0831_connection_failure_cools_the_whole_lane(self):
        with tempfile.TemporaryDirectory() as td:
            st = self._st(td)
            self._task_with_ref(st, "c" * 32 + ".png", tid="t1")
            self._task_with_ref(st, "d" * 32 + ".png", tid="t2")
            f = self._ferry(st, td)
            calls = []
            stub = _fake_scp(rc=1, stderr=b"ssh: connect to host company "
                                          b"port 22: Connection refused",
                             calls=calls)
            with patch.object(sync_console.subprocess, "run", stub):
                f.poll_once()
            self.assertEqual(1, len(calls), "连不上就整线退避，别 8s 超时乘图数")
            self.assertIn("Connection refused", f.last_error)

    def test_0831_an_oversize_pull_is_quarantined(self):
        with tempfile.TemporaryDirectory() as td:
            st = self._st(td)
            name = "e" * 32 + ".png"
            self._task_with_ref(st, name)
            f = self._ferry(st, td)
            big = b"x" * (sync_console.MAX_ASSET_BYTES + 1)
            with patch.object(sync_console.subprocess, "run", _fake_scp(big)):
                f.poll_once()
            self.assertFalse((Path(st.images_dir) / name).exists())
            self.assertIn("尺寸异常", f.last_error)

    def test_0831_junk_filenames_never_reach_the_filesystem(self):
        with tempfile.TemporaryDirectory() as td:
            st = self._st(td)
            self._task_with_ref(st, "..\\evil.png")
            f = self._ferry(st, td)
            calls = []
            with patch.object(sync_console.subprocess, "run",
                              _fake_scp(calls=calls)):
                f.poll_once()
            self.assertEqual([], calls, "对端来的名字是边界数据，不匹配不碰盘")

    def test_0831_dropped_tasks_are_pruned_from_the_port(self):
        with tempfile.TemporaryDirectory() as td:
            st = self._st(td)
            meta = st.save_image_file(
                base64.b64encode(b"PNGDATA").decode(), "image/png")
            self._task_with_ref(st, meta["file"])
            f = self._ferry(st, td)
            with patch.object(sync_console.subprocess, "run", _fake_scp()):
                f.poll_once()
                st.remove_task("t-img")
                f.poll_once()
            port = Path(td) / "syncroot" / "assets" / "taskstage" / meta["file"]
            self.assertFalse(port.exists(), "引用消失，出港副本顺手清掉")
            self.assertEqual(1, f.pruned)

    def test_0831_missing_peer_config_disables_the_lane(self):
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)     # cfg={}：单机跑，摆渡必须静默停用
            self.assertFalse(rig.cs_a.assets.enabled)
            self.assertEqual(0, rig.cs_a.assets.poll_once())
            cs = sync_console.ConsoleSync(
                rig.bus_a, {"sync_root": str(Path(td) / "r"),
                            "sync_peer_ssh": "company",
                            "sync_peer_root": "C:/peer"},
                Path(td) / "a", rig.sess_a,
                board_getter=lambda: rig.board_a,
                task_getter=lambda: rig.task_a)
            self.assertTrue(cs.assets.enabled)


if __name__ == "__main__":
    unittest.main(verbosity=2)
