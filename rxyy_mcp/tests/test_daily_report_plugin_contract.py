# -*- coding: utf-8 -*-
"""日报插件契约（日报插件方案批②）：manifest 声明与实盘一致 + 状态跨域只读。

workflow-data 族第一例。锁三件事：
1. manifest 声明的载体真实存在（CLI 在盘、skills 在盘）——provides 不许说空话；
2. status 跨域只读 workflow.db：素材计数/OA 就绪/开关都从真 sqlite 读出；
3. 缺环境不炸：db 不在/表没建时如实报告（「不知道」也是一种状态），
   绝不把注册表面板拖黑（与 states 异常不连坐配合）。
"""
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = MODULE_DIR.parent
sys.path.insert(0, str(MODULE_DIR))

import hub  # noqa: E402
import plugin_registry as pr  # noqa: E402


def _make_db(td, notes=(), kv=()):
    db = Path(td) / "workflow.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE report_notes (id INTEGER PRIMARY KEY, "
                "note_date TEXT, project TEXT, content TEXT)")
    con.execute("CREATE TABLE kv_config (k TEXT PRIMARY KEY, v TEXT, "
                "updated_at TEXT)")
    for date, project in notes:
        con.execute("INSERT INTO report_notes (note_date, project, content) "
                    "VALUES (?,?,?)", (date, project, "做了点什么"))
    for k, v in kv:
        con.execute("INSERT INTO kv_config (k, v, updated_at) VALUES (?,?,?)",
                    (k, v, "now"))
    con.commit()
    con.close()
    return db


class ManifestMatchesReality(unittest.TestCase):
    """provides 声明的载体必须真实在盘——插件自述不许说空话。"""

    def setUp(self):
        pr._reset_for_tests()
        self.addCleanup(pr._reset_for_tests)
        pr.ensure_builtin_plugins()
        self.info = next(p for p in pr.plugins() if p.name == "daily_report")

    def test_kind_and_status_shape(self):
        self.assertEqual("workflow-data", self.info.kind)
        self.assertTrue(callable(self.info.status))

    def test_declared_cli_exists(self):
        if not (REPO_DIR / "console").is_dir():
            # console 域不在场（AgentDeck 独立仓/干净 checkout）——日报 CLI 是
            # console 的家当，跨域存在性断言只在全仓开发机上有意义
            self.skipTest("console 域不在场，跨域 CLI 断言跳过")
        self.assertTrue((REPO_DIR / "console" / "tools" / "report_note.py").is_file(),
                        "manifest 声明的 CLI 不在盘上")

    def test_declared_skills_exist(self):
        skills = REPO_DIR / "dist" / "rxyy-tools-community" / "data" / "library" / "skills"
        if not skills.is_dir():
            # dist/ 整体不入 git（蓝图卫生审计），CI/干净 checkout 没有这棵树——
            # 本断言只在带数据的开发机上有意义（CI 首跑实翻，跳过而非假红）
            self.skipTest("dist/ 不入库，此环境无 skills 数据目录")
        for name in ("daily-note", "daily-report", "weekly-report"):
            self.assertTrue((skills / name / "SKILL.md").is_file(),
                            "manifest 声明的 skill 缺失: " + name)


class StatusReadsRealDb(unittest.TestCase):
    def setUp(self):
        pr._reset_for_tests()
        self.addCleanup(pr._reset_for_tests)

    def _status_with(self, db_dir):
        with patch.object(hub, "rxyy_data_dirs",
                          lambda: [Path(db_dir)] if db_dir else []):
            return pr.daily_report_status()

    def test_counts_recent_notes_and_reads_kv(self):
        today = time.strftime("%Y-%m-%d")
        with tempfile.TemporaryDirectory() as td:
            _make_db(td,
                     notes=[(today, "rxyy-tools-community"), ("2020-01-01", "老项目")],
                     kv=[("oa_user_account", "u"), ("oa_password", "p")])
            st = self._status_with(td)
        self.assertEqual(1, st["notes_7d"], "只数近 7 天素材")
        self.assertTrue(st["oa_ready"])
        self.assertTrue(st["enabled"], "开关缺省=开（向后兼容）")

    def test_disabled_switch_is_read(self):
        with tempfile.TemporaryDirectory() as td:
            _make_db(td, kv=[(pr.DAILY_REPORT_ENABLED_KEY, "0")])
            st = self._status_with(td)
        self.assertFalse(st["enabled"])

    def test_missing_db_reports_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as td:
            st = self._status_with(td)
        self.assertIn("未找到 workflow.db", st["db"])
        self.assertIsNone(st["notes_7d"])

    def test_states_aggregation_carries_daily_report(self):
        # 注册表聚合口径：json 可序列化（面板/hub_api 直接下发）
        pr.ensure_builtin_plugins()
        with tempfile.TemporaryDirectory() as td:
            _make_db(td)
            with patch.object(hub, "rxyy_data_dirs", lambda: [Path(td)]):
                st = pr.states()
        self.assertIn("daily_report", st)
        json.dumps(st["daily_report"], ensure_ascii=False)
        self.assertEqual("workflow-data", st["daily_report"]["kind"])


if __name__ == "__main__":
    unittest.main()
