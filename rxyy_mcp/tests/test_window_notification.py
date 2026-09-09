# -*- coding: utf-8 -*-
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub


class WindowNotificationTests(unittest.TestCase):
    def test_defaults_include_persistent_window_placement(self):
        self.assertIn("win_x", hub.DEFAULTS)
        self.assertIn("win_y", hub.DEFAULTS)
        self.assertEqual("normal", hub.DEFAULTS.get("win_state"))

    def test_hub_initializes_window_tracker_slot(self):
        with tempfile.TemporaryDirectory() as history_dir:
            # share_token 为空时 Hub.__init__ 会调 save_config，必须一并 patch，
            # 否则跑测试会把 DEFAULTS+临时目录写进真实 config.json（线上配置被污染）
            cfg = dict(hub.DEFAULTS, history_dir=history_dir)
            with patch.object(hub, "load_config", return_value=cfg), \
                    patch.object(hub, "save_config"):
                test_hub = hub.Hub()

        self.assertTrue(hasattr(test_hub, "window_tracker"))
        self.assertIsNone(test_hub.window_tracker)

    def test_hub_stops_window_tracker_before_saving_on_close(self):
        events = []

        class Tracker:
            def stop(self):
                events.append("stop")

        test_hub = hub.Hub.__new__(hub.Hub)
        test_hub.cfg = {}
        test_hub.window_tracker = Tracker()
        test_hub.lock = hub.threading.Lock()
        test_hub.sessions = {}
        with patch.object(hub, "save_config", side_effect=lambda _cfg: events.append("save")):
            hub.Hub.hub_closing(test_hub)

        self.assertEqual(["stop", "save"], events)

    def test_notify_never_restores_or_moves_window(self):
        class WindowThatMustRemainUntouched:
            restore_calls = 0

            def restore(self):
                self.restore_calls += 1

        test_hub = hub.Hub.__new__(hub.Hub)
        test_hub.cfg = {"audio_enabled": False}
        test_hub.window = WindowThatMustRemainUntouched()

        hub.Hub.notify(test_hub)

        self.assertEqual(0, test_hub.window.restore_calls)


if __name__ == "__main__":
    unittest.main()
