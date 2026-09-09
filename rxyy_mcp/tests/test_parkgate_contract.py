# -*- coding: utf-8 -*-
"""开车（parkgate）的 Hub 安装契约。

08-07 线上事故：park_install_hook 只调 ensure_stub，从不把 hook.js 落到 ~/.salak。
stub 每次 require 一个不存在的文件都抛 MODULE_NOT_FOUND 又被自己 catch 吞掉，
于是按钮报「已装，注入 1 个入口」、面板一直显示「未安装」、开车按了全放行。
两个沙箱自测都测不出来——它们验的是 parkgate 模块，漏的恰好是 Hub 这层。
"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
import hub  # noqa: E402


class ParkgateInstallApiTests(unittest.TestCase):
    def test_install_copies_the_hook_before_injecting_the_stub(self):
        calls = []
        fake = SimpleNamespace(
            install_hook=lambda: (calls.append("install_hook"), (True, "hook.js 已安装（v4）"))[1],
            ensure_stub=lambda: (calls.append("ensure_stub"), (1, "已注入 1 个扩展宿主入口"))[1],
        )
        with patch.dict(sys.modules, {"parkgate": fake}):
            result = hub.Api().park_install_hook()
        self.assertTrue(result["ok"])
        self.assertEqual(1, result["injected"])
        self.assertEqual(["install_hook", "ensure_stub"], calls)

    def test_failed_hook_copy_never_injects_a_stub(self):
        ensure = Mock(return_value=(1, "不该执行"))
        fake = SimpleNamespace(
            install_hook=lambda: (False, "找不到 parkgate_hook.js 源文件"),
            ensure_stub=ensure,
        )
        with patch.dict(sys.modules, {"parkgate": fake}):
            result = hub.Api().park_install_hook()
        self.assertFalse(result["ok"])
        self.assertIn("找不到", result["error"])
        ensure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
