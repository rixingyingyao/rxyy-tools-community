# -*- coding: utf-8 -*-
"""开机自启 / 拉起子进程不能弹黑框。"""
import ast
import os
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
CONSOLE_DIR = APP_DIR.parent / "console"
sys.path.insert(0, str(APP_DIR))

import frozen_boot  # noqa: E402
import watchdog  # noqa: E402


class FrozenBootWindowlessTests(unittest.TestCase):
    def test_quoted_argv_quotes_every_segment(self):
        s = frozen_boot.quoted_argv([r"D:\桌面\a.exe", "--run", r"D:\x\hub.py", "--daemon"])
        self.assertEqual(
            '"D:\\桌面\\a.exe" "--run" "D:\\x\\hub.py" "--daemon"', s)

    def test_hidden_popen_kwargs_hide_the_console(self):
        kw = frozen_boot.hidden_popen_kwargs()
        self.assertEqual(frozen_boot.CREATE_NO_WINDOW | frozen_boot.CREATE_NEW_PROCESS_GROUP,
                         kw.get("creationflags", 0) & (
                             frozen_boot.CREATE_NO_WINDOW | frozen_boot.CREATE_NEW_PROCESS_GROUP))
        si = kw.get("startupinfo")
        self.assertIsNotNone(si)
        self.assertTrue(si.dwFlags & 0x1)  # STARTF_USESHOWWINDOW
        self.assertEqual(0, si.wShowWindow)

    def test_script_dir_uses_the_py_not_system32(self):
        argv = [r"C:\Python\pythonw.exe", r"D:\live\rxyy_mcp\hub.py", "--daemon"]
        self.assertEqual(r"D:\live\rxyy_mcp", frozen_boot.script_dir(argv, r"C:\Windows\System32"))

    def test_py_exe_prefers_pythonw_when_not_frozen(self):
        exe = Path(frozen_boot.py_exe()).name.lower()
        if Path(sys.executable).with_name("pythonw.exe").is_file():
            self.assertEqual("pythonw.exe", exe)


class WiringTests(unittest.TestCase):
    def test_hub_refreshes_run_keys_from_live_too(self):
        src = (APP_DIR / "hub.py").read_text(encoding="utf-8")
        self.assertIn("refresh_run_keys_if_enabled", src)
        self.assertNotIn(
            'autostart_get() and getattr(sys, "frozen", False)', src)

    def test_login_autostart_opens_console_after_gateway(self):
        src = (APP_DIR / "hub.py").read_text(encoding="utf-8")
        self.assertIn("should_open_console_on_autostart", src)
        self.assertIn("open_console_window_if_absent", src)
        # 看门狗复活必须仍只传 --daemon，否则每次救援都弹窗抢焦点
        wd = (APP_DIR / "watchdog.py").read_text(encoding="utf-8")
        self.assertNotIn("--autostart", wd)

    def test_watchdog_no_longer_starts_powershell_to_list_hubs(self):
        tree = ast.parse((APP_DIR / "watchdog.py").read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "hub_py_pids":
                calls = [ast.unparse(n) for n in ast.walk(node) if isinstance(n, ast.Call)]
                self.assertFalse(any("powershell" in c.lower() for c in calls))
                self.assertTrue(any("_pid_command_line" in c for c in calls))
                return
        self.fail("找不到 hub_py_pids")

    def test_console_startup_uses_spawn_argv(self):
        src = (CONSOLE_DIR / "app.py").read_text(encoding="utf-8")
        self.assertIn("spawn_argv", src)
        self.assertIn("hidden_popen_kwargs", src)


class OpenConsoleOnAutostartTests(unittest.TestCase):
    """登录自启才亮窗口：看门狗 --daemon 不抢焦点；窗口已在不弹第二扇。"""

    def test_gate_only_login_flag(self):
        import hub
        self.assertFalse(hub.should_open_console_on_autostart(["hub.py", "--daemon"]))
        self.assertFalse(hub.should_open_console_on_autostart(["hub.py"]))
        self.assertTrue(hub.should_open_console_on_autostart(
            ["hub.py", "--daemon", "--autostart"]))

    def test_skips_spawn_when_window_already_open(self):
        import hub
        spawned = []
        self.assertEqual(
            "skipped",
            hub.open_console_window_if_absent(
                _find=lambda: 1, _spawn=lambda: spawned.append(1)))
        self.assertEqual([], spawned)

    def test_spawns_when_no_window(self):
        import hub
        spawned = []
        self.assertEqual(
            "opened",
            hub.open_console_window_if_absent(
                _find=lambda: 0, _spawn=lambda: spawned.append(1)))
        self.assertEqual([1], spawned)


class ProcessCommandLineTests(unittest.TestCase):
    def test_can_read_this_process_command_line(self):
        cmd = watchdog._pid_command_line(os.getpid())
        self.assertTrue(cmd, "读不到本进程命令行，清残骸会漏掉 exe --run")
        self.assertTrue("python" in cmd.lower() or "rxyy-tools-community" in cmd.lower())

    def test_this_process_age_is_non_negative(self):
        age = watchdog._pid_age_secs(os.getpid())
        self.assertIsNotNone(age)
        self.assertGreaterEqual(age, 0.0)


class PackedExeSubsystemTests(unittest.TestCase):
    def test_current_dist_exe_is_windows_gui_if_present(self):
        exe = APP_DIR.parent / "dist" / "rxyy-tools-community" / "rxyy-tools-community.exe"
        if not exe.is_file():
            self.skipTest("还没打过包")
        sys.path.insert(0, str(APP_DIR.parent / "scripts"))
        import verify_exe_windowed
        self.assertEqual(2, verify_exe_windowed.pe_subsystem(exe))


if __name__ == "__main__":
    unittest.main()
