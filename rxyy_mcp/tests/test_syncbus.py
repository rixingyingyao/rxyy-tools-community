# -*- coding: utf-8 -*-
"""双活 P0（方案A 事件互灌）骨架验收：单写 outbox + 幂等重放 + 台账 + 停机纪律。

设计稿 §4.4 P0 验收换算成单测（家机挂着，两机 Syncthing 实拍等它复活）：
- A 机 emit 演示事件，B 机一拍内重放落地（真机上 5s 内，poll_secs=2）
- 「断网 10 分钟恢复后无重复无丢失」→ 重放两遍/重启重放/文件回滚重读，
  applied 台账（uuid 幂等）都必须挡住重复
- 坏行跳过举手、对端高版本事件停机举手、LWW 定序
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import syncbus  # noqa: E402


class _Rig:
    """两台机（A=company、B=home）共用一个「Syncthing 夹」，各有私有 datadir。"""

    def __init__(self, td):
        self.root = Path(td) / "syncfolder"
        self.data_a = Path(td) / "data-a"
        self.data_b = Path(td) / "data-b"
        for p in (self.data_a, self.data_b):
            p.mkdir(parents=True)
        self.alerts = []
        self.a = syncbus.SyncBus(self.root, "company", self.data_a,
                                 alert=self.alerts.append)
        self.b = syncbus.SyncBus(self.root, "home", self.data_b,
                                 alert=self.alerts.append)

    def demo_on_b(self):
        try:
            return json.loads((self.data_b / ".sync-demo.json")
                              .read_text(encoding="utf-8"))
        except OSError:
            return {}


class OutboxTests(unittest.TestCase):
    def test_emit_writes_full_event_and_seq_increments(self):
        with tempfile.TemporaryDirectory() as td:
            box = syncbus.Outbox(Path(td), "company")
            e1 = box.emit("demo", "update", "demo", payload={"text": "一"})
            e2 = box.emit("demo", "update", "demo", payload={"text": "二"})
            self.assertEqual((1, 2), (e1["seq"], e2["seq"]))
            lines = (Path(td) / "events" / "company.jsonl").read_text(
                encoding="utf-8").splitlines()
            self.assertEqual(2, len(lines))
            ev = json.loads(lines[0])
            for key in ("v", "seq", "uuid", "ts", "machine", "store", "op", "entity"):
                self.assertIn(key, ev)
            self.assertEqual("company", ev["machine"])

    def test_seq_survives_restart(self):
        # 重启（重建 Outbox）后 seq 接着数，不回卷——seq 回卷会毁掉「单调」承诺
        with tempfile.TemporaryDirectory() as td:
            syncbus.Outbox(Path(td), "company").emit("demo", "update", "d")
            box2 = syncbus.Outbox(Path(td), "company")
            self.assertEqual(2, box2.emit("demo", "update", "d")["seq"])


class ReplayTests(unittest.TestCase):
    def test_a_emits_b_applies_within_one_poll(self):
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            rig.a.demo_emit("你好家机")
            self.assertEqual(1, rig.b.replayer.poll_once())
            self.assertEqual("你好家机",
                             rig.demo_on_b()["demo"]["payload"]["text"])

    def test_replaying_twice_applies_once(self):
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            rig.a.demo_emit("只此一次")
            self.assertEqual(1, rig.b.replayer.poll_once())
            self.assertEqual(0, rig.b.replayer.poll_once(), "offset 没推进")

    def test_restart_does_not_reapply(self):
        # 台账落盘：B 机重启（重建 SyncBus）后不重复应用
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            rig.a.demo_emit("重启前")
            rig.b.replayer.poll_once()
            b2 = syncbus.SyncBus(rig.root, "home", rig.data_b,
                                 alert=rig.alerts.append)
            self.assertEqual(0, b2.replayer.poll_once())

    def test_rewritten_peer_file_is_deduped_by_uuid(self):
        # 对端文件被重建（offset > size）：offset 作废重读，uuid 挡重复
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            rig.a.demo_emit("回滚我")
            rig.a.demo_emit("我也在")
            self.assertEqual(2, rig.b.replayer.poll_once())
            f = rig.root / "events" / "company.jsonl"
            first_line = f.read_text(encoding="utf-8").splitlines()[0]
            f.write_text(first_line + "\n", encoding="utf-8")  # 文件变小
            self.assertEqual(0, rig.b.replayer.poll_once(), "全是旧事件，一条都不许重放")
            self.assertTrue(any("变小" in a for a in rig.alerts))

    def test_own_outbox_is_never_replayed(self):
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            rig.b.demo_emit("自己的话自己不吃")
            self.assertEqual(0, rig.b.replayer.poll_once())
            self.assertEqual({}, rig.demo_on_b())


class DisciplineTests(unittest.TestCase):
    """坏行跳过、半行等待、高版本停机、未知 store 跳过举手、handler 失败停位。"""

    def test_bad_line_is_skipped_and_flagged_once(self):
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            f = rig.root / "events" / "company.jsonl"
            f.parent.mkdir(parents=True, exist_ok=True)
            with open(f, "a", encoding="utf-8") as fh:
                fh.write("这不是JSON{{{\n")
            rig.a.demo_emit("坏行后面的好事件")
            self.assertEqual(1, rig.b.replayer.poll_once(), "坏行不许堵死后面的事件")
            led = rig.b.replayer._ledger("company")
            self.assertEqual(1, led.bad_lines)
            self.assertTrue(any("坏行" in a for a in rig.alerts))

    def test_partial_tail_line_waits_for_completion(self):
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            ev = rig.a.demo_emit("半行")
            f = rig.root / "events" / "company.jsonl"
            whole = f.read_text(encoding="utf-8")
            f.write_text(whole.rstrip("\n"), encoding="utf-8")  # 掐掉换行=写了一半
            self.assertEqual(0, rig.b.replayer.poll_once(), "半行不许吃")
            f.write_text(whole, encoding="utf-8")  # 对端写完了
            self.assertEqual(1, rig.b.replayer.poll_once())
            self.assertTrue(rig.b.replayer._ledger("company").seen(ev["uuid"]))

    def test_higher_schema_version_holds_that_peer(self):
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            f = rig.root / "events" / "company.jsonl"
            f.parent.mkdir(parents=True, exist_ok=True)
            future = {"v": syncbus.SCHEMA_V + 1, "seq": 1, "uuid": "f" * 32,
                      "ts": time.time(), "machine": "company",
                      "store": "demo", "op": "update", "entity": "demo",
                      "payload": {"text": "来自未来"}}
            with open(f, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(future, ensure_ascii=False) + "\n")
            rig.a.demo_emit("跟在未来事件后面")
            self.assertEqual(0, rig.b.replayer.poll_once(), "高版本必须停机，不许瞎猜")
            led = rig.b.replayer._ledger("company")
            self.assertIn("先升级", led.held)
            self.assertEqual(0, rig.b.replayer.poll_once(), "停机期间一直不吃")
            self.assertEqual({}, rig.demo_on_b())
            self.assertTrue(any("停机" in a for a in rig.alerts))

    def test_unknown_store_skips_but_keeps_lane_open(self):
        # P1 的 store 先于本机代码出现（灰度窗口）：跳过举手，demo 不能被堵死
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            rig.a.outbox.emit("board", "create", "card-1", payload={"t": "x"})
            rig.a.demo_emit("后面的 demo 要到")
            self.assertEqual(1, rig.b.replayer.poll_once())
            self.assertEqual("后面的 demo 要到",
                             rig.demo_on_b()["demo"]["payload"]["text"])
            self.assertTrue(any("未注册" in a for a in rig.alerts))

    def test_handler_failure_holds_position_and_retries(self):
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            state = {"fail": True, "applied": []}

            def flaky(ev):
                if state["fail"]:
                    raise RuntimeError("本地故障一次")
                state["applied"].append(ev["uuid"])

            rig.b.replayer.register("flaky", flaky)
            rig.a.outbox.emit("flaky", "update", "e1")
            self.assertEqual(0, rig.b.replayer.poll_once(), "失败要停位，不许跳过丢事件")
            state["fail"] = False
            self.assertEqual(1, rig.b.replayer.poll_once(), "下一拍从停点重试")
            self.assertEqual(1, len(state["applied"]))


class LwwTests(unittest.TestCase):
    def test_older_event_arriving_later_does_not_overwrite(self):
        # LWW 按 (ts, machine)：晚到的旧值不许覆盖新值（§4.2 定序）
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            now = time.time()
            newer = {"v": 1, "uuid": "n" * 32, "ts": now, "machine": "company",
                     "store": "demo", "op": "update", "entity": "demo",
                     "payload": {"text": "新值"}}
            older = {"v": 1, "uuid": "o" * 32, "ts": now - 60, "machine": "company",
                     "store": "demo", "op": "update", "entity": "demo",
                     "payload": {"text": "旧值"}}
            f = rig.root / "events" / "company.jsonl"
            f.parent.mkdir(parents=True, exist_ok=True)
            with open(f, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(newer, ensure_ascii=False) + "\n")
                fh.write(json.dumps(older, ensure_ascii=False) + "\n")
            self.assertEqual(2, rig.b.replayer.poll_once())
            self.assertEqual("新值", rig.demo_on_b()["demo"]["payload"]["text"])

    def test_lww_tiebreak_is_stable_across_machines(self):
        # 时间戳完全相同时按机器名决胜：两边裁决必须一致（谁裁都一个结果）
        self.assertTrue(syncbus.lww_wins(100.0, "home", 100.0, "company"))
        self.assertFalse(syncbus.lww_wins(100.0, "company", 100.0, "home"))
        self.assertTrue(syncbus.lww_wins(101.0, "company", 100.0, "home"))


class ClockAndConfigTests(unittest.TestCase):
    def test_sntp_unreachable_returns_none_not_crash(self):
        # DNS 打桩：公司网慢解析曾让这条测试跑了一分多钟，单测不碰真网络
        def _boom(*a, **k):
            raise OSError("DNS 不可达")

        with patch.object(syncbus.socket, "getaddrinfo", _boom):
            self.assertIsNone(syncbus.sntp_skew(timeout=0.3))

    def test_start_from_config_disabled_when_root_empty(self):
        self.assertIsNone(syncbus.start_from_config({"sync_root": ""}, "."))
        self.assertIsNone(syncbus.start_from_config({}, "."))

    def test_start_from_config_uses_configured_machine_name(self):
        # start 打桩：单测不起线程不打网络（真启动那条路由 hub main 接线）
        with tempfile.TemporaryDirectory() as td, \
                patch.object(syncbus.SyncBus, "start", lambda self: self):
            bus = syncbus.start_from_config(
                {"sync_root": str(Path(td) / "sf"), "sync_machine": "Company"},
                Path(td) / "data")
            self.assertEqual("company", bus.machine)


if __name__ == "__main__":
    unittest.main(verbosity=2)
