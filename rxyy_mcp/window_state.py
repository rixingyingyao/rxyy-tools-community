# -*- coding: utf-8 -*-
"""rxyy MCP 窗口位置与显示状态的持久化逻辑。"""
import threading


VALID_STATES = {"normal", "minimized", "maximized"}
OFFSCREEN_SENTINEL = -30000


def _as_int(value, default=None):
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def normalize_window_state(value):
    return value if value in VALID_STATES else "normal"


def _is_visible_on_any_screen(x, y, width, height, screens, min_visible=80):
    required_width = min(min_visible, width)
    required_height = min(min_visible, height)
    for screen in screens or []:
        sx = _as_int(getattr(screen, "x", None))
        sy = _as_int(getattr(screen, "y", None))
        sw = _as_int(getattr(screen, "width", None))
        sh = _as_int(getattr(screen, "height", None))
        if None in (sx, sy, sw, sh) or sw <= 0 or sh <= 0:
            continue
        overlap_width = min(x + width, sx + sw) - max(x, sx)
        overlap_height = min(y + height, sy + sh) - max(y, sy)
        if overlap_width >= required_width and overlap_height >= required_height:
            return True
    return False


def build_window_options(
    config,
    screens,
    default_width=920,
    default_height=1040,
    min_width=520,
    min_height=640,
):
    width = max(min_width, _as_int(config.get("win_width"), default_width))
    height = max(min_height, _as_int(config.get("win_height"), default_height))
    x = _as_int(config.get("win_x"))
    y = _as_int(config.get("win_y"))
    if (
        x is None
        or y is None
        or x <= OFFSCREEN_SENTINEL
        or y <= OFFSCREEN_SENTINEL
        or not _is_visible_on_any_screen(x, y, width, height, screens)
    ):
        x = y = None
    state = normalize_window_state(config.get("win_state"))
    return {
        "width": width,
        "height": height,
        "x": x,
        "y": y,
        "minimized": state == "minimized",
        "maximized": state == "maximized",
    }


class WindowStateTracker:
    def __init__(
        self,
        config,
        window,
        save_callback,
        capture_delay=0.35,
        timer_factory=threading.Timer,
    ):
        self.config = config
        self.window = window
        self.save_callback = save_callback
        self.capture_delay = capture_delay
        self.timer_factory = timer_factory
        self.lock = threading.RLock()
        self.timer = None
        self.generation = 0
        self.state = normalize_window_state(config.get("win_state"))
        self.config["win_state"] = self.state

    def _cancel_capture_locked(self):
        self.generation += 1
        if self.timer:
            self.timer.cancel()
            self.timer = None

    def _schedule_capture_locked(self):
        if self.state != "normal":
            return
        self._cancel_capture_locked()
        generation = self.generation
        timer = self.timer_factory(
            self.capture_delay,
            lambda: self.capture(generation),
        )
        timer.daemon = True
        self.timer = timer
        timer.start()

    def geometry_changed(self, *_args):
        with self.lock:
            self._schedule_capture_locked()

    def _set_state(self, state):
        with self.lock:
            self.state = normalize_window_state(state)
            self.config["win_state"] = self.state
            if self.state == "normal":
                self._schedule_capture_locked()
            else:
                self._cancel_capture_locked()
        self.save_callback()

    def mark_minimized(self):
        self._set_state("minimized")

    def mark_maximized(self):
        self._set_state("maximized")

    def mark_restored(self):
        self._set_state("normal")

    def capture(self, generation=None):
        with self.lock:
            if generation is not None and generation != self.generation:
                return False
            if generation is not None:
                self.timer = None
            if self.state != "normal":
                return False
        try:
            x = _as_int(self.window.x)
            y = _as_int(self.window.y)
            width = _as_int(self.window.width)
            height = _as_int(self.window.height)
        except Exception:
            return False
        if (
            None in (x, y, width, height)
            or x <= OFFSCREEN_SENTINEL
            or y <= OFFSCREEN_SENTINEL
            or width < 520
            or height < 640
        ):
            return False
        with self.lock:
            if self.state != "normal":
                return False
            self.config.update({
                "win_x": x,
                "win_y": y,
                "win_width": width,
                "win_height": height,
                "win_state": "normal",
            })
        self.save_callback()
        return True

    def flush(self):
        with self.lock:
            self._cancel_capture_locked()
            normal = self.state == "normal"
        if normal:
            self.capture()
        else:
            self.save_callback()

    def stop(self):
        with self.lock:
            self._cancel_capture_locked()
