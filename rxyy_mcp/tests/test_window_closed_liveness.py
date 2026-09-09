# -*- coding: utf-8 -*-
"""Cursor 窗口关闭后仍显示在线的事故复现与回归。

事故形态：Cursor 扩展窗口已经停止登记，但共享 MCP 心跳仍让 session.connected
保持 True；旧 pending / generating 又排在窗口判断之前，于是面板继续显示等回复或
干活中。测试只打桩判活依赖，不读写真实 Cursor 状态库、agentboard 或 extbus 文件。
"""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import ext_bus  # noqa: E402
import hub  # noqa: E402


def _session(now, **overrides):
    values = {
        "id": "window-closed",
        "name": "Cursor 旧会话",
        "connected": True,
        "pending": None,
        "recon_deadline": None,
        "queued": [],
        "lock": threading.Lock(),
        "agent_status": "",
        "agent_status_ts": 0,
        "last_heartbeat": now,
        "cursor_uuid": "cursor-uuid",
        "transcript_path": None,
        "conv_key": "conv-window-closed",
        "last_zhi_ts": 0,
        "processing_since": None,
        "death_info": None,
        "pending_lost": False,
        "wait_deferred": False,
        "buffered_reply": None,
        "ext_instance": "win-closed",
        "runtime_kind": "cursor",
    }
    values.update(overrides)
    s = hub.Session.__new__(hub.Session)
    for key, value in values.items():
        setattr(s, key, value)
    return s


class CursorWindowClosedLivenessTests(unittest.TestCase):
    def test_report_before_window_timeout_does_not_delay_offline_for_fifteen_minutes(self):
        now = time.time()
        s = _session(now, last_zhi_ts=now - 100, agent_status="testing",
                     agent_status_ts=now - 100,
                     pending={"id": "q", "created": now - 100})
        result = self._liveness(
            s, now, act={"generating": True, "updated_ts": now - 100},
            registered=[{"id": "win-closed", "updatedAt": (now - 80) * 1000}])
        self.assertEqual("idle", result["state"])
        self.assertFalse(result["generating"])

    def _liveness(self, s, now, *, act=None, instances=(), registered=None,
                  board=(None, None)):
        if registered is None:
            registered = instances
        with patch.object(hub, "read_cursor_activity", return_value=act or {}), \
                patch.object(hub, "agent_last_edit_ts", return_value=0), \
                patch.object(hub.Hub, "board_write_ages",
                             return_value=board), \
                patch.object(hub.Api, "unclaimed_lock_owner", return_value=None), \
                patch.object(ext_bus, "read_instances",
                             return_value=list(registered)), \
                patch.object(ext_bus, "live_instances",
                             return_value=list(instances)):
            return hub.Api()._agent_liveness(s, now)

    def test_closed_cursor_window_overrides_old_pending_and_generating(self):
        """修前红：共享 MCP 心跳 + 旧 pending/generating 会伪装成在线。"""
        now = time.time()
        s = _session(
            now,
            pending={"id": "old-q", "message": "旧问题", "created": now - 1200},
        )
        result = self._liveness(
            s,
            now,
            act={"generating": True, "updated_ts": now - 1200},
            registered=[{"id": "win-closed", "updatedAt": now * 1000 - 120000}],
        )

        self.assertEqual("idle", result["state"])
        self.assertIn("Cursor 窗口已关闭", result["label"])
        self.assertIn("扩展心跳", " ".join(result["evidence"]))

    def test_closed_cursor_window_overrides_old_generating_without_pending(self):
        now = time.time()
        s = _session(now)
        result = self._liveness(
            s,
            now,
            act={"generating": True, "updated_ts": now - 1200},
            registered=[{"id": "win-closed", "updatedAt": now * 1000 - 120000}],
        )

        self.assertEqual("idle", result["state"])
        self.assertIn("Cursor 窗口已关闭", result["label"])

    def test_live_cursor_window_keeps_pending_waiting(self):
        now = time.time()
        s = _session(
            now,
            pending={"id": "q", "message": "请确认", "created": now - 1200},
        )
        result = self._liveness(
            s,
            now,
            act={"generating": True, "updated_ts": now - 1200},
            instances=[{"id": "win-closed", "updatedAt": now * 1000 - 1000}],
        )

        self.assertEqual("waiting", result["state"])

    def test_recent_real_zhi_and_zt_survive_closed_window_evidence(self):
        now = time.time()
        zhi = _session(now, last_zhi_ts=now - 20, processing_since=now - 20)
        zt = _session(now, agent_status="testing", agent_status_ts=now - 20)
        stale_window = [{"id": "win-closed", "updatedAt": now * 1000 - 120000}]

        zhi_result = self._liveness(zhi, now, registered=stale_window)
        zt_result = self._liveness(zt, now, registered=stale_window)

        self.assertEqual("working", zhi_result["state"])
        self.assertEqual("working", zt_result["state"])

    def test_recent_background_write_survives_closed_window_evidence(self):
        now = time.time()
        s = _session(now)
        result = self._liveness(
            s,
            now,
            registered=[{"id": "win-closed", "updatedAt": now * 1000 - 120000}],
            board=(10.0, None),
        )

        self.assertEqual("working", result["state"])
        self.assertIn("看板写文件", result["label"])

    def test_missing_window_registry_is_not_verified_as_closed(self):
        now = time.time()
        s = _session(
            now,
            pending={"id": "q", "message": "请确认", "created": now - 1200},
        )
        result = self._liveness(s, now, act={"generating": True})

        self.assertEqual("waiting", result["state"])
        self.assertNotIn("Cursor 窗口已关闭", result["label"])

    def test_missing_or_empty_runtime_kind_keeps_legacy_cursor_compatibility(self):
        now = time.time()
        stale_window = [{"id": "win-closed", "updatedAt": now * 1000 - 120000}]
        for runtime_kind in ("missing", ""):
            s = _session(
                now,
                pending={"id": "q", "message": "旧问题", "created": now - 1200},
            )
            if runtime_kind == "missing":
                del s.runtime_kind
            else:
                s.runtime_kind = runtime_kind
            result = self._liveness(s, now, registered=stale_window)

            self.assertEqual("idle", result["state"], runtime_kind)
            self.assertIn("Cursor 窗口已关闭", result["label"])

    def test_non_cursor_runtime_skips_cursor_window_probe(self):
        now = time.time()
        for runtime_kind in ("codex", "chatgpt", "unknown"):
            s = _session(
                now,
                runtime_kind=runtime_kind,
                pending={"id": "q", "message": "请确认", "created": now - 1200},
            )
            with patch.object(ext_bus, "live_instances",
                              side_effect=AssertionError("非 Cursor 不应探测窗口")):
                result = self._liveness_without_window_probe(s, now)
            self.assertNotEqual("idle", result["state"], runtime_kind)
            self.assertNotIn("Cursor 窗口已关闭", result["label"])

    def test_unbound_legacy_session_skips_cursor_window_probe(self):
        now = time.time()
        s = _session(
            now,
            ext_instance="",
            pending={"id": "q", "message": "请确认", "created": now - 1200},
        )
        with patch.object(ext_bus, "live_instances",
                          side_effect=AssertionError("未绑定旧会话不应探测窗口")):
            result = self._liveness_without_window_probe(s, now)
        self.assertNotEqual("idle", result["state"])
        self.assertNotIn("Cursor 窗口已关闭", result["label"])

    def _liveness_without_window_probe(self, s, now):
        with patch.object(hub, "read_cursor_activity", return_value={}), \
                patch.object(hub, "agent_last_edit_ts", return_value=0), \
                patch.object(hub.Hub, "board_write_ages",
                             return_value=(None, None)), \
                patch.object(hub.Api, "unclaimed_lock_owner", return_value=None):
            return hub.Api()._agent_liveness(s, now)


if __name__ == "__main__":
    unittest.main()
