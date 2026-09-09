# -*- coding: utf-8 -*-
"""账单 hook 的 Hub/UI 错误契约。"""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
import hub  # noqa: E402
import billgate  # noqa: E402


class BillgateApiTests(unittest.TestCase):
    def test_billgate_bootstrap_isolated_from_parkgate_failure(self):
        bill_bootstrap = Mock(return_value={"enabled": False, "note": "安全关闭"})
        fake_park = SimpleNamespace(bootstrap=Mock(side_effect=RuntimeError("park failed")))
        fake_bill = SimpleNamespace(bootstrap=bill_bootstrap)
        with (patch.dict(sys.modules, {"parkgate": fake_park, "billgate": fake_bill}),
              patch.object(hub, "log_event")):
            hub._bootstrap_local_hooks()
        bill_bootstrap.assert_called_once_with()

    def test_failed_hook_copy_never_injects_a_stub(self):
        ensure = Mock(return_value=(1, "不该执行"))
        fake = SimpleNamespace(
            install_hook=lambda: (False, "找不到账单 hook 源文件"),
            ensure_stub=ensure,
        )
        with patch.dict(sys.modules, {"billgate": fake}):
            result = hub.Api().bill_install_hook()
        self.assertFalse(result["ok"])
        self.assertIn("找不到", result["error"])
        ensure.assert_not_called()

    def test_remove_api_keeps_legacy_count_and_full_uninstall_note(self):
        fake = SimpleNamespace(remove_stub=lambda: (2, "已完整卸载"))
        with patch.dict(sys.modules, {"billgate": fake}):
            result = hub.Api().bill_remove_hook()
        self.assertTrue(result["ok"])
        self.assertEqual(2, result["restored"])
        self.assertEqual(2, result["removed"])
        self.assertEqual("已完整卸载", result["note"])


class BillgateUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (APP_DIR / "ui.html").read_text(encoding="utf-8")

    def test_enable_disable_install_and_remove_all_check_ok(self):
        self.assertIn('function billError(r, action)', self.html)
        for action in ("开启", "停止", "安装", "卸载"):
            self.assertIn('billError(r, "{}")'.format(action), self.html)

    def test_remove_copy_describes_full_cleanup(self):
        self.assertIn("完整卸载账单hook", self.html)
        self.assertIn("脚本、状态、观测和日志", self.html)


class BillgatePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.paths = {
            "BILLGATE_DIR": billgate.BILLGATE_DIR,
            "BILL_JSON": billgate.BILL_JSON,
            "BILL_TMP": billgate.BILL_TMP,
            "HOOK_DST": billgate.HOOK_DST,
            "BILL_LOG": billgate.BILL_LOG,
            "BILL_LOG_ROTATED": billgate.BILL_LOG_ROTATED,
            "SEEN_DIR": billgate.SEEN_DIR,
            "ENTRY_CACHE": billgate.ENTRY_CACHE,
            "BACKUP_DIR": billgate.BACKUP_DIR,
        }
        self.old_state = dict(billgate._state)
        self.old_wire = dict(billgate._WIRE_CACHE)
        billgate.BILLGATE_DIR = self.root
        billgate.BILL_JSON = self.root / "bill.json"
        billgate.BILL_TMP = self.root / "bill.json.tmp"
        billgate.HOOK_DST = self.root / "billhook.js"
        billgate.BILL_LOG = self.root / "billhook.log"
        billgate.BILL_LOG_ROTATED = self.root / "billhook.log.1"
        billgate.SEEN_DIR = self.root / "bill-seen"
        billgate.ENTRY_CACHE = self.root / ".ext-hosts.json"
        billgate.BACKUP_DIR = self.root / "ext-host-backups"
        billgate._state.clear()
        billgate._state.update({"enabled": False, "mode": "observe", "seeded": True})
        billgate._WIRE_CACHE.update({"ts": 0.0, "wired": False, "note": ""})
        self._wiring = patch.object(billgate, "wiring_state", return_value=(True, "已接通"))
        self._wiring.start()

    def tearDown(self):
        self._wiring.stop()
        for name, value in self.paths.items():
            setattr(billgate, name, value)
        billgate._state.clear()
        billgate._state.update(self.old_state)
        billgate._WIRE_CACHE.clear()
        billgate._WIRE_CACHE.update(self.old_wire)
        self.tempdir.cleanup()

    def test_billgate_has_no_runtime_parkgate_dependency(self):
        self.assertNotIn("parkgate", billgate.__dict__)
        self.assertNotIn(".salak", str(billgate.BILLGATE_DIR).lower())
        source = (APP_DIR / "billgate.py").read_text(encoding="utf-8").lower()
        self.assertNotIn(".salak", source)
        self.assertNotIn("import parkgate", source)

    def test_billgate_has_no_slack_plugin_dependency(self):
        """账单拦截是独立的 HTTP/2 hook，不得随 Slack 插件启停或加载。"""
        production_files = (
            APP_DIR / "billgate.py",
            APP_DIR / "billgate_hook.js",
            APP_DIR / "hub.py",
            APP_DIR / "hub_api.py",
            APP_DIR / "server.py",
            APP_DIR / "live_runtime.py",
            APP_DIR / "ui.html",
        )
        for path in production_files:
            text = path.read_text(encoding="utf-8").lower()
            self.assertNotIn("slack", text, path.name)

    def test_legacy_billgate_marker_is_detected_without_plugin_path_knowledge(self):
        entry = self.root / "extensionHostProcess.js"
        legacy = (
            b"entry\n"
            b"try{require(require('os').homedir()+'/.old-billgate/billhook.js')}"
            b"catch(e){/* chijiu-billgate-esm */}\n"
        )
        entry.write_bytes(legacy)
        self.assertTrue(billgate._has_legacy_stub(entry))
        self.assertFalse(billgate._has_stub(entry))
        self.assertTrue(billgate._strip_own_stub(entry))
        self.assertNotIn(b"billhook.js", entry.read_bytes())

    def test_enable_refuses_when_not_wired(self):
        with patch.object(billgate, "wiring_state", return_value=(False, "入口未注入")):
            result = billgate.enable("observe")
        self.assertFalse(result["ok"])
        self.assertFalse(result["enabled"])
        self.assertIn("尚未接通", result["error"])
        self.assertFalse(billgate.BILL_JSON.exists())

    def test_enable_write_failure_restores_existing_enabled_state(self):
        billgate._state.update({"enabled": False, "mode": "observe"})
        with patch.object(billgate, "_write_bill", return_value=False):
            result = billgate.enable("block")
        self.assertFalse(result["ok"])
        self.assertFalse(result["enabled"])
        self.assertEqual("observe", result["mode"])
        self.assertFalse(billgate.status()["enabled"])
        self.assertEqual("observe", billgate.status()["mode"])

    def test_disable_write_failure_never_reports_a_stale_disabled_state(self):
        billgate._state.update({"enabled": True, "mode": "block"})
        with patch.object(billgate, "_write_bill", return_value=False):
            result = billgate.disable()
        self.assertFalse(result["ok"])
        self.assertTrue(result["enabled"])
        self.assertEqual("block", result["mode"])
        self.assertTrue(billgate.status()["enabled"])

    def test_set_mode_write_failure_restores_previous_mode(self):
        billgate._state.update({"enabled": True, "mode": "observe"})
        with patch.object(billgate, "_write_bill", return_value=False):
            result = billgate.set_mode("block")
        self.assertFalse(result["ok"])
        self.assertEqual("observe", result["mode"])
        self.assertEqual("observe", billgate.status()["mode"])

    def test_bootstrap_reports_when_existing_enabled_signal_cannot_be_closed(self):
        billgate.BILL_JSON.write_text('{"enabled": true, "mode": "block"}\n', encoding="utf-8")
        billgate._state.update({"enabled": True, "mode": "block"})
        with patch.object(billgate, "_write_bill", return_value=False):
            result = billgate.bootstrap()
        self.assertFalse(result["ok"])
        self.assertTrue(result["enabled"])
        self.assertIn("安全关闭失败", result["note"])
        self.assertTrue(billgate.status()["enabled"])

    def test_remove_stub_raises_if_a_stub_cannot_be_removed(self):
        entry = self.root / "extensionHostProcess.js"
        entry.write_bytes(b"entry\n" + billgate.STUB_LINE.encode("utf-8"))
        billgate.BILL_JSON.write_text('{"enabled": false, "mode": "observe"}\n', encoding="utf-8")
        with (patch.object(billgate, "_cursor_ext_host_entries", return_value=[entry]),
              patch.object(billgate, "_strip_own_stub", side_effect=billgate.BillgateCleanupError("locked"))):
            with self.assertRaisesRegex(billgate.BillgateCleanupError, "locked"):
                billgate.remove_stub()

    def test_remove_stub_raises_if_an_owned_file_cannot_be_deleted(self):
        billgate.BILL_JSON.write_text('{"enabled": false, "mode": "observe"}\n', encoding="utf-8")
        billgate.HOOK_DST.write_text("hook", encoding="utf-8")
        with (patch.object(billgate, "_cursor_ext_host_entries", return_value=[]),
              patch.object(billgate, "_unlink_own", side_effect=billgate.BillgateCleanupError("locked"))):
            with self.assertRaisesRegex(billgate.BillgateCleanupError, "locked"):
                billgate.remove_stub()

    def test_remove_stub_raises_if_observation_cleanup_fails(self):
        billgate.BILL_JSON.write_text('{"enabled": false, "mode": "observe"}\n', encoding="utf-8")
        billgate.SEEN_DIR.mkdir()
        seen = billgate.SEEN_DIR / "123.json"
        seen.write_text("{}", encoding="utf-8")
        unlink_own = billgate._unlink_own

        def fail_only_for_seen(path):
            if path == seen:
                raise billgate.BillgateCleanupError("locked")
            return unlink_own(path)

        with (patch.object(billgate, "_cursor_ext_host_entries", return_value=[]),
              patch.object(billgate, "_unlink_own", side_effect=fail_only_for_seen)):
            with self.assertRaisesRegex(billgate.BillgateCleanupError, "locked"):
                billgate.remove_stub()


if __name__ == "__main__":
    unittest.main()
