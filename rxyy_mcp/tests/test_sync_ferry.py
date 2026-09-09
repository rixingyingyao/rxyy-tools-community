# -*- coding: utf-8 -*-
"""双活 P1 运输线验收：SSH 摆渡的落地原子性、失败纪律与自伤防护。

scp 全程打桩（单测不碰网络不碰真 ssh），只验证 sync_ferry 自己的责任：
- 拉到新内容 → tmp+原子换落地，旧内容相同 → 不动本地文件
- 远端还没有 outbox 不算故障；真故障连败到阈值举手一次、恢复报一声
- peer_machine 撞本机名拒绝启动（拉回来会盖掉本机 outbox）
"""
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import sync_ferry  # noqa: E402


def _fake_scp(body: bytes = None, rc: int = 0, stderr: bytes = b""):
    """打桩 subprocess.run：rc=0 时把 body 写进 scp 的目标路径（cmd 末位）。"""
    def run(cmd, **kw):
        assert cmd[0] == "scp" and "-q" in cmd and "BatchMode=yes" in cmd
        if rc == 0 and body is not None:
            Path(cmd[-1]).write_bytes(body)
        return types.SimpleNamespace(returncode=rc, stderr=stderr)
    return run


class PullTests(unittest.TestCase):
    def _mk(self, td, alerts=None):
        return sync_ferry.Ferry(Path(td) / "root", "peerhost", r"C:\sync",
                                "home", secs=60,
                                alert=(alerts.append if alerts is not None
                                       else None))

    def test_remote_path_is_forward_slashed(self):
        with tempfile.TemporaryDirectory() as td:
            f = self._mk(td)
            self.assertEqual("peerhost:C:/sync/events/home.jsonl", f.remote)

    def test_new_content_lands_atomically(self):
        with tempfile.TemporaryDirectory() as td:
            f = self._mk(td)
            with patch.object(sync_ferry.subprocess, "run",
                              _fake_scp(b'{"seq":1}\n')):
                self.assertTrue(f.pull_once())
            self.assertEqual(b'{"seq":1}\n', f.local_path.read_bytes())
            self.assertFalse(f.local_path.with_name(
                f.local_path.name + ".ferrytmp").exists(), "tmp 必须收走")
            self.assertEqual((1, 0), (f.changed, f.consecutive_fails))

    def test_identical_content_does_not_touch_local_copy(self):
        with tempfile.TemporaryDirectory() as td:
            f = self._mk(td)
            f.local_path.write_bytes(b'{"seq":1}\n')
            before = f.local_path.stat().st_mtime_ns
            with patch.object(sync_ferry.subprocess, "run",
                              _fake_scp(b'{"seq":1}\n')):
                self.assertFalse(f.pull_once())
            self.assertEqual(before, f.local_path.stat().st_mtime_ns,
                             "同内容不许重写文件（replayer 不用白跑）")
            self.assertEqual(0, f.changed)

    def test_missing_remote_outbox_is_not_a_failure(self):
        with tempfile.TemporaryDirectory() as td:
            alerts = []
            f = self._mk(td, alerts)
            err = b"scp: C:/sync/events/home.jsonl: No such file or directory"
            with patch.object(sync_ferry.subprocess, "run",
                              _fake_scp(rc=1, stderr=err)):
                self.assertFalse(f.pull_once())
            self.assertTrue(f.remote_missing)
            self.assertEqual(0, f.consecutive_fails, "对端没话说≠运输故障")
            self.assertEqual([], alerts)

    def test_failures_alert_once_then_recovery_reports_back(self):
        with tempfile.TemporaryDirectory() as td:
            alerts = []
            f = self._mk(td, alerts)
            dead = _fake_scp(rc=255, stderr=b"Connection timed out")
            with patch.object(sync_ferry.subprocess, "run", dead):
                for _ in range(sync_ferry.FAIL_ALERT_AFTER + 5):
                    f.pull_once()
            self.assertEqual(1, len(alerts), "连败只举手一次，不刷屏")
            self.assertIn("拉不到", alerts[0])
            with patch.object(sync_ferry.subprocess, "run",
                              _fake_scp(b"x\n")):
                self.assertTrue(f.pull_once())
            self.assertEqual(2, len(alerts))
            self.assertIn("恢复", alerts[1])
            self.assertEqual(0, f.consecutive_fails)

    def test_scp_spawn_error_counts_as_failure_and_cleans_tmp(self):
        with tempfile.TemporaryDirectory() as td:
            f = self._mk(td)

            def boom(cmd, **kw):
                raise OSError("scp 不在 PATH")

            with patch.object(sync_ferry.subprocess, "run", boom):
                self.assertFalse(f.pull_once())
            self.assertEqual(1, f.consecutive_fails)
            self.assertIn("scp", f.last_error)


class ConfigTests(unittest.TestCase):
    _FULL = {"sync_root": r"C:\me\sync", "sync_peer_ssh": "sunrise",
             "sync_peer_root": "C:/peer/sync", "sync_peer_machine": "home",
             "sync_ferry_secs": 30}

    def test_requires_all_four_keys(self):
        with patch.object(sync_ferry.Ferry, "start", lambda self: self):
            for missing in ("sync_root", "sync_peer_ssh", "sync_peer_root",
                            "sync_peer_machine"):
                cfg = dict(self._FULL)
                cfg[missing] = ""
                self.assertIsNone(sync_ferry.start_from_config(cfg),
                                  "缺 {} 不该开".format(missing))

    def test_full_config_starts_with_configured_interval(self):
        with tempfile.TemporaryDirectory() as td, \
                patch.object(sync_ferry.Ferry, "start", lambda self: self):
            cfg = dict(self._FULL, sync_root=str(Path(td) / "sync"))
            f = sync_ferry.start_from_config(cfg, machine="company")
            self.assertIsNotNone(f)
            self.assertEqual(30.0, f.secs)
            self.assertEqual("sunrise:C:/peer/sync/events/home.jsonl", f.remote)

    def test_rejects_pulling_own_machine_name(self):
        # peer_machine 撞本机名：拉回来的副本会盖掉本机 outbox（单写者铁律）
        alerts = []
        with tempfile.TemporaryDirectory() as td, \
                patch.object(sync_ferry.Ferry, "start", lambda self: self):
            cfg = dict(self._FULL, sync_root=str(Path(td) / "sync"),
                       sync_peer_machine="company")
            f = sync_ferry.start_from_config(cfg, alert=alerts.append,
                                             machine="company")
            self.assertIsNone(f)
            self.assertTrue(any("盖掉本机 outbox" in a for a in alerts))


if __name__ == "__main__":
    unittest.main(verbosity=2)
