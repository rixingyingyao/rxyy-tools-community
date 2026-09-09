# -*- coding: utf-8 -*-
"""双活 P1 配置线验收：workflow.db 白名单实体的差量 emit + 幂等应用。

对应 sync_wf.py 门头的四条规矩：
- 首跑只立基线不发事件（存量一致性靠 08-28 的人工合并，不重播存量）
- 差量才发、内容指纹判同（同值重写不产生事件）
- LWW 用行内 updated_at 定序；凭证键 emit/apply 双侧硬闸
- 项目只更新两边都有的（按 name 对齐），绝不凭空造幽灵项目
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import sync_wf  # noqa: E402
import syncbus  # noqa: E402

# 与 console/api/workflow_db.py 的 _SCHEMA 对齐（sync_wf 碰到的三张表）
_DDL = """
CREATE TABLE kv_config (
    k TEXT PRIMARY KEY, v TEXT DEFAULT '', updated_at TEXT NOT NULL);
CREATE TABLE projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
    enabled INTEGER DEFAULT 1, biz_name TEXT DEFAULT '',
    oa_project_id TEXT DEFAULT '', oa_project_text TEXT DEFAULT '',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE report_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_date TEXT NOT NULL, project TEXT NOT NULL,
    content TEXT DEFAULT '', source TEXT DEFAULT 'agent',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE(note_date, project));
"""


def _mkdb(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.executescript(_DDL)
    con.commit()
    con.close()


def _kv_set(db, k, v, at):
    con = sqlite3.connect(str(db))
    con.execute("INSERT INTO kv_config(k,v,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v, "
                "updated_at=excluded.updated_at", (k, v, at))
    con.commit()
    con.close()


def _kv_get(db, k):
    con = sqlite3.connect(str(db))
    row = con.execute("SELECT v, updated_at FROM kv_config WHERE k=?",
                      (k,)).fetchone()
    con.close()
    return row


def _proj_add(db, path, name, biz="", at="2026-08-28T09:00:00"):
    con = sqlite3.connect(str(db))
    con.execute("INSERT INTO projects(path,name,enabled,biz_name,created_at,"
                "updated_at) VALUES(?,?,1,?,?,?)", (path, name, biz, at, at))
    con.commit()
    con.close()


def _q1(db, sql, args=()):
    con = sqlite3.connect(str(db))
    row = con.execute(sql, args).fetchone()
    con.close()
    return row


class _Rig:
    """A=company、B=home 共用一个事件夹，各有私有 workflow.db 与 datadir。
    线程一律不起（poll_secs 只是摆设），全部手动打拍。"""

    def __init__(self, td):
        td = Path(td)
        self.root = td / "syncfolder"
        self.alerts = []
        self.db_a = td / "a" / "workflow.db"
        self.db_b = td / "b" / "workflow.db"
        _mkdb(self.db_a)
        _mkdb(self.db_b)
        self.bus_a = syncbus.SyncBus(self.root, "company", td / "a",
                                     alert=self.alerts.append)
        self.bus_b = syncbus.SyncBus(self.root, "home", td / "b",
                                     alert=self.alerts.append)
        self.wf_a = sync_wf.WfSync(self.bus_a, lambda: self.db_a, td / "a",
                                   alert=self.alerts.append)
        self.wf_b = sync_wf.WfSync(self.bus_b, lambda: self.db_b, td / "b",
                                   alert=self.alerts.append)

    def seed_both(self):
        assert self.wf_a.poll_once() == 0
        assert self.wf_b.poll_once() == 0


class SeedTests(unittest.TestCase):
    def test_first_poll_seeds_baseline_without_emitting(self):
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            _kv_set(rig.db_a, "report_total_hours", "8", "2026-08-28T10:00:00")
            self.assertEqual(0, rig.wf_a.poll_once(), "首跑只立基线，不重播存量")
            self.assertTrue(rig.wf_a._seeded)
            self.assertFalse((rig.root / "events" / "company.jsonl").exists(),
                             "一条事件都不该写")
            # 基线落盘且能认出实体
            d = json.loads(rig.wf_a.baseline_path.read_text(encoding="utf-8"))
            self.assertIn("kv:report_total_hours", d["fp"])

    def test_unchanged_rows_emit_nothing_after_seed(self):
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            _kv_set(rig.db_a, "report_total_hours", "8", "2026-08-28T10:00:00")
            rig.wf_a.poll_once()
            self.assertEqual(0, rig.wf_a.poll_once(), "没改动就没事件")


class FlowTests(unittest.TestCase):
    def test_kv_change_flows_to_peer_with_row_ts(self):
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            rig.seed_both()
            _kv_set(rig.db_a, "sync_canary", "canary-来自公司机",
                    "2026-08-29T01:00:00")
            self.assertEqual(1, rig.wf_a.poll_once())
            self.assertEqual(1, rig.bus_b.replayer.poll_once())
            row = _kv_get(rig.db_b, "sync_canary")
            self.assertEqual("canary-来自公司机", row[0])
            self.assertEqual("2026-08-29T01:00:00", row[1],
                             "对端落库要保留真实写入时刻，不许洗成应用时刻")

    def test_applied_event_is_not_echoed_back(self):
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            rig.seed_both()
            _kv_set(rig.db_a, "sync_canary", "只跑一圈", "2026-08-29T01:00:00")
            rig.wf_a.poll_once()
            rig.bus_b.replayer.poll_once()
            self.assertEqual(0, rig.wf_b.poll_once(),
                             "刚吃进来的值不许再发回去（回声抑制）")
            self.assertFalse((rig.root / "events" / "home.jsonl").exists())

    def test_lww_older_incoming_does_not_overwrite_newer_local(self):
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            _kv_set(rig.db_b, "report_total_hours", "9", "2026-08-29T12:00:00")
            rig.seed_both()
            # A 在更早的时刻写了旧值（比 B 的 12:00 早）
            _kv_set(rig.db_a, "report_total_hours", "8", "2026-08-29T08:00:00")
            self.assertEqual(1, rig.wf_a.poll_once())
            rig.bus_b.replayer.poll_once()
            self.assertEqual("9", _kv_get(rig.db_b, "report_total_hours")[0],
                             "晚到的旧改动不许盖掉本地更新的值")
            self.assertEqual(1, rig.wf_b.skipped_lww)

    def test_note_insert_then_update_lands_without_duplicates(self):
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            rig.seed_both()
            con = sqlite3.connect(str(rig.db_a))
            con.execute("INSERT INTO report_notes(note_date,project,content,"
                        "source,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                        ("2026-08-29", "playthread-go", "修复推流卡顿",
                         "agent", "2026-08-29T10:00:00", "2026-08-29T10:00:00"))
            con.commit()
            con.close()
            self.assertEqual(1, rig.wf_a.poll_once())
            self.assertEqual(1, rig.bus_b.replayer.poll_once())
            row = _q1(rig.db_b, "SELECT content FROM report_notes WHERE "
                                "note_date=? AND project=?",
                      ("2026-08-29", "playthread-go"))
            self.assertEqual("修复推流卡顿", row[0])
            # 同键再改内容 → 对端是更新而不是第二行
            con = sqlite3.connect(str(rig.db_a))
            con.execute("UPDATE report_notes SET content=?, updated_at=? "
                        "WHERE note_date=? AND project=?",
                        ("修复推流卡顿；补单测", "2026-08-29T11:00:00",
                         "2026-08-29", "playthread-go"))
            con.commit()
            con.close()
            rig.wf_a.poll_once()
            rig.bus_b.replayer.poll_once()
            n = _q1(rig.db_b, "SELECT COUNT(*), MAX(content) FROM report_notes")
            self.assertEqual((1, "修复推流卡顿；补单测"), (n[0], n[1]))


class GuardTests(unittest.TestCase):
    def test_snapshot_never_contains_non_whitelist_or_credential_keys(self):
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "workflow.db"
            _mkdb(db)
            _kv_set(db, "oa_password", "绝密", "2026-08-29T00:00:00")
            _kv_set(db, "oa_token", "绝密", "2026-08-29T00:00:00")
            _kv_set(db, "some_random_key", "闲杂", "2026-08-29T00:00:00")
            _kv_set(db, "report_total_hours", "8", "2026-08-29T00:00:00")
            snap = sync_wf.snapshot(db)
            self.assertEqual(["kv:report_total_hours"],
                             [k for k in snap if k.startswith("kv:")])

    def test_credential_event_from_peer_never_lands(self):
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            rig.seed_both()
            # 对端手滑（或旧代码白名单里有敏感键）直接发凭证事件
            rig.bus_a.outbox.emit("wf", "update", "kv:oa_token",
                                  payload={"v": "leaked",
                                           "row_ts": "2026-08-29T01:00:00"})
            self.assertEqual(1, rig.bus_b.replayer.poll_once(),
                             "事件要记账消化（不堵队），但不落库")
            self.assertIsNone(_kv_get(rig.db_b, "oa_token"))

    def test_project_updates_matched_name_but_never_creates_ghosts(self):
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            _proj_add(rig.db_a, r"D:\work\playthread-go", "playthread-go")
            _proj_add(rig.db_a, r"D:\work\only-on-a", "only-on-a")
            _proj_add(rig.db_b, r"E:\repos\playthread-go", "playthread-go")
            rig.seed_both()
            con = sqlite3.connect(str(rig.db_a))
            con.execute("UPDATE projects SET biz_name=?, updated_at=? "
                        "WHERE name=?",
                        ("智慧云广播录播播出端", "2026-08-29T09:30:00",
                         "playthread-go"))
            con.execute("UPDATE projects SET biz_name=?, updated_at=? "
                        "WHERE name=?",
                        ("A 机独有仓", "2026-08-29T09:30:00", "only-on-a"))
            con.commit()
            con.close()
            self.assertEqual(2, rig.wf_a.poll_once())
            self.assertEqual(2, rig.bus_b.replayer.poll_once())
            self.assertEqual("智慧云广播录播播出端",
                             _q1(rig.db_b, "SELECT biz_name FROM projects "
                                           "WHERE name=?",
                                 ("playthread-go",))[0])
            self.assertEqual(1, _q1(rig.db_b,
                                    "SELECT COUNT(*) FROM projects")[0],
                             "对端没有的项目不许凭空插行（路径在本机不存在）")

    def test_baseline_survives_restart(self):
        # WfSync 重建（hub 重启）后不把存量当新改动重播
        with tempfile.TemporaryDirectory() as td:
            rig = _Rig(td)
            _kv_set(rig.db_a, "report_total_hours", "8", "2026-08-28T10:00:00")
            rig.wf_a.poll_once()
            wf_a2 = sync_wf.WfSync(rig.bus_a, lambda: rig.db_a,
                                   Path(td) / "a", alert=rig.alerts.append)
            self.assertEqual(0, wf_a2.poll_once(), "重启不重播存量")


class HelperTests(unittest.TestCase):
    def test_iso_to_epoch_accepts_both_separators_and_garbage(self):
        t1 = sync_wf._iso_to_epoch("2026-08-28T10:00:00")
        t2 = sync_wf._iso_to_epoch("2026-08-28 10:00:00")
        self.assertEqual(t1, t2)
        self.assertGreater(t1, 0)
        self.assertEqual(0.0, sync_wf._iso_to_epoch(""))
        self.assertEqual(0.0, sync_wf._iso_to_epoch("不是时间"))

    def test_fingerprint_ignores_dict_order_but_not_content(self):
        a = sync_wf._fp({"v": "1", "x": "2"})
        self.assertEqual(a, sync_wf._fp({"x": "2", "v": "1"}))
        self.assertNotEqual(a, sync_wf._fp({"v": "1", "x": "3"}))

    def test_canary_key_is_whitelisted(self):
        self.assertIn("sync_canary", sync_wf.KV_WHITELIST)


if __name__ == "__main__":
    unittest.main(verbosity=2)
