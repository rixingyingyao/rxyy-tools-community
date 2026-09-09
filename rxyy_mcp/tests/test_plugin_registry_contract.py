# -*- coding: utf-8 -*-
"""插件注册表契约（日报插件方案批①）：注册/重名拒绝/状态聚合/异常不连坐。

设计判断的护栏：通用层只有 PluginInfo 描述 + 注册表三问（有谁/什么状态/入口
在哪），ext_host 与 workflow-data 两族的个性生命周期不进通用签名。异常不连坐
与 hub._bootstrap_local_hooks 同纪律——一个插件坏了不能把面板整个拖黑。
"""
import sys
import unittest
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import plugin_registry as pr  # noqa: E402


def _info(name, status=None, kind="workflow-data"):
    return pr.PluginInfo(name=name, kind=kind, title=name, version="1.0",
                         provides=("x:测试",), status=status)


class RegistryContract(unittest.TestCase):
    def setUp(self):
        pr._reset_for_tests()
        self.addCleanup(pr._reset_for_tests)

    def test_register_and_list_keeps_order(self):
        pr.register(_info("a"))
        pr.register(_info("b"))
        self.assertEqual(["a", "b"], [p.name for p in pr.plugins()])

    def test_duplicate_name_is_rejected(self):
        pr.register(_info("a"))
        with self.assertRaises(ValueError):
            pr.register(_info("a"))

    def test_nameless_plugin_is_rejected(self):
        with self.assertRaises(ValueError):
            pr.register(_info(""))

    def test_states_aggregates_status(self):
        pr.register(_info("a", status=lambda: {"armed": True}))
        pr.register(_info("b", status=None))
        st = pr.states()
        self.assertEqual({"armed": True}, st["a"]["status"])
        self.assertIsNone(st["b"]["status"])
        self.assertEqual("workflow-data", st["a"]["kind"])
        self.assertEqual(["x:测试"], st["a"]["provides"])

    def test_one_broken_status_does_not_take_others_down(self):
        # 异常不连坐：a 坏了记 error，b 与注册表本身照常
        def boom():
            raise RuntimeError("信号文件损坏")
        pr.register(_info("a", status=boom))
        pr.register(_info("b", status=lambda: {"ok": True}))
        st = pr.states()
        self.assertIn("状态查询失败", st["a"]["status"]["error"])
        self.assertEqual({"ok": True}, st["b"]["status"])


class BuiltinPluginsContract(unittest.TestCase):
    """内置描述接入：park/bill 两个 ext_host 活样例——零行为变化地被描述。"""

    def setUp(self):
        pr._reset_for_tests()
        self.addCleanup(pr._reset_for_tests)

    def test_builtin_registers_both_families(self):
        added = pr.ensure_builtin_plugins()
        names = {p.name for p in added}
        self.assertEqual({"parkgate", "billgate", "daily_report"}, names)
        kinds = {p.name: p.kind for p in pr.plugins()}
        self.assertEqual("ext_host", kinds["parkgate"])
        self.assertEqual("ext_host", kinds["billgate"])
        self.assertEqual("workflow-data", kinds["daily_report"])
        for p in pr.plugins():
            self.assertTrue(callable(p.status), p.name)

    def test_builtin_is_idempotent(self):
        pr.ensure_builtin_plugins()
        again = pr.ensure_builtin_plugins()
        self.assertEqual([], again, "重复调用不得二次注册")
        self.assertEqual(3, len(pr.plugins()))

    def test_builtin_states_answer_without_env(self):
        # 契约：干净环境（无信号文件/无 Cursor 安装）下 states() 也要能应答——
        # 各 gate 的 status() 本就设计为缺环境时回默认态 dict，不炸
        pr.ensure_builtin_plugins()
        st = pr.states()
        for name in ("parkgate", "billgate"):
            self.assertIsInstance(st[name]["status"], dict, name)


class HubApiPluginsState(unittest.TestCase):
    """批③：hub_api.plugins_state() 只读聚合——面板/UI 的插件生态数据源。"""

    def setUp(self):
        pr._reset_for_tests()
        self.addCleanup(pr._reset_for_tests)

    def test_plugins_state_aggregates_builtin(self):
        import json
        import hub
        r = hub.Api().plugins_state()
        self.assertTrue(r["ok"])
        self.assertEqual({"parkgate", "billgate", "daily_report"},
                         set(r["plugins"].keys()))
        json.dumps(r, ensure_ascii=False)   # 面板直接下发，必须可序列化

    def test_registry_blowup_does_not_take_ui_down(self):
        import hub
        from unittest.mock import patch
        with patch.object(pr, "ensure_builtin_plugins",
                          side_effect=RuntimeError("坏了")):
            r = hub.Api().plugins_state()
        self.assertFalse(r["ok"])
        self.assertIn("插件状态不可用", r["error"])


if __name__ == "__main__":
    unittest.main()
