# -*- coding: utf-8 -*-
import importlib.util
import inspect
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import window_state


class ImmediateTimer:
    def __init__(self, _delay, callback):
        self.callback = callback
        self.daemon = False
        self.cancelled = False

    def start(self):
        if not self.cancelled:
            self.callback()

    def cancel(self):
        self.cancelled = True


class WindowStateModuleTests(unittest.TestCase):
    def test_window_state_module_is_available(self):
        self.assertIsNotNone(importlib.util.find_spec("window_state"))

    def test_window_state_public_api_is_available(self):
        self.assertTrue(callable(getattr(window_state, "build_window_options", None)))
        self.assertTrue(callable(getattr(window_state, "WindowStateTracker", None)))

    def test_tracker_accepts_window_and_save_dependencies(self):
        parameters = inspect.signature(window_state.WindowStateTracker).parameters

        self.assertIn("config", parameters)
        self.assertIn("window", parameters)
        self.assertIn("save_callback", parameters)
        self.assertIn("timer_factory", parameters)

    def test_maximized_geometry_does_not_replace_normal_geometry(self):
        cfg = {
            "win_x": 100,
            "win_y": 120,
            "win_width": 900,
            "win_height": 700,
            "win_state": "normal",
        }
        window = SimpleNamespace(x=100, y=120, width=900, height=700)
        tracker = window_state.WindowStateTracker(
            cfg,
            window,
            lambda: None,
            timer_factory=ImmediateTimer,
        )
        self.assertTrue(callable(getattr(tracker, "mark_maximized", None)))
        self.assertTrue(callable(getattr(tracker, "geometry_changed", None)))

        tracker.mark_maximized()
        window.x, window.y, window.width, window.height = 0, 0, 1920, 1080
        tracker.geometry_changed()

        self.assertEqual((100, 120, 900, 700), (
            cfg["win_x"], cfg["win_y"], cfg["win_width"], cfg["win_height"],
        ))
        self.assertEqual("maximized", cfg["win_state"])

    def test_restored_window_captures_stable_geometry(self):
        cfg = {"win_state": "maximized"}
        window = SimpleNamespace(x=240, y=180, width=1000, height=760)
        tracker = window_state.WindowStateTracker(
            cfg,
            window,
            lambda: None,
            timer_factory=ImmediateTimer,
        )

        tracker.mark_restored()

        self.assertTrue(
            {"win_x", "win_y", "win_width", "win_height"}.issubset(cfg),
        )
        self.assertEqual((240, 180, 1000, 760), (
            cfg["win_x"], cfg["win_y"], cfg["win_width"], cfg["win_height"],
        ))
        self.assertEqual("normal", cfg["win_state"])

    def test_build_options_restores_secondary_screen_and_maximized_state(self):
        screens = [
            SimpleNamespace(x=0, y=0, width=1920, height=1080),
            SimpleNamespace(x=-1920, y=0, width=1920, height=1080),
        ]
        cfg = {
            "win_x": -1700,
            "win_y": 120,
            "win_width": 900,
            "win_height": 700,
            "win_state": "maximized",
        }

        options = window_state.build_window_options(cfg, screens)

        self.assertEqual(
            {"width", "height", "x", "y", "minimized", "maximized"},
            set(options),
        )
        self.assertEqual((-1700, 120), (options["x"], options["y"]))
        self.assertEqual((900, 700), (options["width"], options["height"]))
        self.assertTrue(options["maximized"])
        self.assertFalse(options["minimized"])

    def test_build_options_discards_coordinates_outside_current_screens(self):
        screens = [SimpleNamespace(x=0, y=0, width=1920, height=1080)]
        cfg = {
            "win_x": 5000,
            "win_y": 5000,
            "win_width": 900,
            "win_height": 700,
            "win_state": "unexpected",
        }

        options = window_state.build_window_options(cfg, screens)

        self.assertIsNone(options["x"])
        self.assertIsNone(options["y"])
        self.assertFalse(options["maximized"])
        self.assertFalse(options["minimized"])

    def test_build_options_supports_legacy_config_without_placement(self):
        screens = [SimpleNamespace(x=0, y=0, width=1920, height=1080)]

        options = window_state.build_window_options(
            {"win_width": 400, "win_height": 500},
            screens,
        )

        self.assertEqual((520, 640), (options["width"], options["height"]))
        self.assertIsNone(options["x"])
        self.assertIsNone(options["y"])
        self.assertFalse(options["maximized"])
        self.assertFalse(options["minimized"])


if __name__ == "__main__":
    unittest.main()
