# -*- coding: utf-8 -*-
"""扩展宿主插件契约（第一版 manifest）的单测。

锁的是从 parkgate/billgate 反推出来的共享骨架里那几条安全铁律——尤其
「多插件共存时摘一个绝不误伤另一个」和「只升不降 / 留底须是原件」。
本模块目前不接线到运行时（第三刀 A 案），这些测试就是它的验收。
"""
import sys
import tempfile
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import ext_host_plugin as ehp  # noqa: E402


def _park(home: Path) -> ehp.ExtHostPlugin:
    return ehp.ExtHostPlugin(
        name="parkgate",
        home=home / ".salak",
        hook_src=home / "src" / "parkgate_hook.js",
        hook_dst=home / ".salak" / "hook.js",
        stub_mark="chijiu-parkgate-esm",
        stub_line=('try{(function(){var m=require("module");'
                   'm.createRequire(process.execPath)(require("os").homedir()'
                   '+"/.salak/hook.js")})()}catch(e){/* chijiu-parkgate-esm */}\n'),
        own_line_groups=(("/.salak/hook.js",),),
        identity_markers=("/.salak/hook.js", "chijiu-parkgate-esm"),
    )


def _bill(home: Path) -> ehp.ExtHostPlugin:
    return ehp.ExtHostPlugin(
        name="billgate",
        home=home / ".rxyy-billgate",
        hook_src=home / "src" / "billgate_hook.js",
        hook_dst=home / ".rxyy-billgate" / "billhook.js",
        stub_mark="chijiu-billgate-esm-v2",
        stub_line=('try{(function(){var m=require("module");'
                   'm.createRequire(process.execPath)(require("os").homedir()'
                   '+"/.rxyy-billgate/billhook.js")})()}catch(e){/* chijiu-billgate-esm-v2 */}\n'),
        own_line_groups=(("chijiu-billgate", "billhook.js"),),
        identity_markers=("chijiu-billgate", "billhook.js"),
    )


class InstallHookUpgradeOnlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.p = _park(self.home)
        self.p.hook_src.parent.mkdir(parents=True, exist_ok=True)
        self.p.hook_src.write_text('// HOOK_VERSION = "v4-park"\nconsole.log(1)\n', encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_installs_when_absent(self):
        ok, note = ehp.install_hook(self.p)
        self.assertTrue(ok)
        self.assertIn("v4", note)
        self.assertTrue(self.p.hook_dst.is_file())

    def test_noop_when_identical(self):
        ehp.install_hook(self.p)
        ok, note = ehp.install_hook(self.p)
        self.assertTrue(ok)
        self.assertIn("已是最新", note)

    def test_refuses_to_downgrade(self):
        self.p.hook_dst.parent.mkdir(parents=True, exist_ok=True)
        self.p.hook_dst.write_text('// HOOK_VERSION = "v5-newer"\n', encoding="utf-8")
        ok, note = ehp.install_hook(self.p)
        self.assertFalse(ok)
        self.assertIn("不覆盖", note)
        self.assertIn("v5", self.p.hook_dst.read_text(encoding="utf-8"))

    def test_overwrites_older(self):
        self.p.hook_dst.parent.mkdir(parents=True, exist_ok=True)
        self.p.hook_dst.write_text('// HOOK_VERSION = "v3-old"\n', encoding="utf-8")
        ok, _ = ehp.install_hook(self.p)
        self.assertTrue(ok)
        self.assertIn("v4-park", self.p.hook_dst.read_text(encoding="utf-8"))

    def test_missing_source_is_reported(self):
        self.p.hook_src.unlink()
        ok, note = ehp.install_hook(self.p)
        self.assertFalse(ok)
        self.assertIn("找不到", note)


class StubDetectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.p, self.b = _park(self.home), _bill(self.home)
        self.entry = self.home / "extensionHostProcess.js"

    def tearDown(self):
        self.tmp.cleanup()

    def test_current_stub_detected(self):
        self.entry.write_bytes(b"code\n" + self.p.stub_line.encode("utf-8"))
        self.assertTrue(ehp.has_stub(self.p, self.entry))
        self.assertFalse(ehp.has_legacy_stub(self.p, self.entry))

    def test_legacy_stub_detected_and_distinguished(self):
        legacy = (b"code\n"
                  b"try{require(require('os').homedir()+'/.salak/hook.js')}catch(e){}\n")
        self.entry.write_bytes(legacy)
        self.assertFalse(ehp.has_stub(self.p, self.entry))
        self.assertTrue(ehp.has_legacy_stub(self.p, self.entry))

    def test_one_plugin_does_not_see_the_others_stub(self):
        self.entry.write_bytes(b"code\n" + self.b.stub_line.encode("utf-8"))
        self.assertFalse(ehp.has_stub(self.p, self.entry))
        self.assertFalse(ehp.has_legacy_stub(self.p, self.entry))

    def test_pristine_backup_rejects_any_plugin_marker(self):
        self.assertTrue(ehp.is_pristine_backup(b"just some cursor code\n"))
        self.assertFalse(ehp.is_pristine_backup(self.p.stub_line.encode("utf-8")))
        self.assertFalse(ehp.is_pristine_backup(self.b.stub_line.encode("utf-8")))


class CoexistenceTests(unittest.TestCase):
    """两个插件同时注入同一入口：摘一个绝不能碰另一个（8月多插件共存铁律）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.p, self.b = _park(self.home), _bill(self.home)
        self.entry = self.home / "extensionHostProcess.js"
        self.entry.write_bytes(b"realcode\n"
                               + self.p.stub_line.encode("utf-8")
                               + self.b.stub_line.encode("utf-8"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_strip_park_keeps_bill(self):
        self.assertTrue(ehp.strip_own_stub(self.p, self.entry))
        raw = self.entry.read_bytes()
        self.assertNotIn(self.p.stub_mark.encode("utf-8"), raw)
        self.assertIn(self.b.stub_mark.encode("utf-8"), raw)
        self.assertIn(b"realcode", raw)

    def test_strip_bill_keeps_park(self):
        self.assertTrue(ehp.strip_own_stub(self.b, self.entry))
        raw = self.entry.read_bytes()
        self.assertNotIn(self.b.stub_mark.encode("utf-8"), raw)
        self.assertIn(self.p.stub_mark.encode("utf-8"), raw)

    def test_strip_is_byte_exact_for_the_rest(self):
        # 只少了自己那一整行，其余字节一字不差（Windows CRLF 陷阱的回归锁）
        before = self.entry.read_bytes()
        ehp.strip_own_stub(self.p, self.entry)
        after = self.entry.read_bytes()
        expected = before.replace(self.p.stub_line.encode("utf-8"), b"")
        self.assertEqual(expected, after)
        self.assertEqual(b"realcode\n" + self.b.stub_line.encode("utf-8"), after)


class EnsureAndRestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.p = _park(self.home)
        self.entry = self.home / "app" / "extensionHostProcess.js"
        self.entry.parent.mkdir(parents=True, exist_ok=True)
        self.entry.write_bytes(b"AAA\nBBB\n")
        # 让 ext_host_entries 直接命中我们这份入口（不起 powershell）
        self.p.home.mkdir(parents=True, exist_ok=True)
        self.p.entry_cache.write_text('["%s"]' % str(self.entry).replace("\\", "\\\\"),
                                      encoding="utf-8")
        self._orig = ehp.ext_host_entries
        ehp.ext_host_entries = lambda plugin, probe=True: [self.entry]

    def tearDown(self):
        ehp.ext_host_entries = self._orig
        self.tmp.cleanup()

    def test_ensure_injects_and_backs_up_pristine_original(self):
        n, _ = ehp.ensure_stub(self.p)
        self.assertEqual(1, n)
        self.assertTrue(ehp.has_stub(self.p, self.entry))
        bak = ehp.backup_path(self.p, self.entry)
        self.assertTrue(bak.is_file())
        self.assertEqual(b"AAA\nBBB\n", bak.read_bytes())

    def test_ensure_is_idempotent(self):
        ehp.ensure_stub(self.p)
        n, _ = ehp.ensure_stub(self.p)
        self.assertEqual(0, n)
        body = self.entry.read_bytes()
        self.assertEqual(1, body.count(self.p.stub_mark.encode("utf-8")))

    def test_ensure_uses_cached_entries_before_powershell(self):
        calls = []

        def fake(plugin, probe=True):
            calls.append(probe)
            return [self.entry] if not probe else []

        ehp.ext_host_entries = fake
        n, _ = ehp.ensure_stub(self.p)
        self.assertEqual([False], calls)
        self.assertEqual(1, n)

    def test_restore_from_clean_backup_is_byte_exact(self):
        ehp.ensure_stub(self.p)
        restored, from_backup, _ = ehp.restore(self.p)
        self.assertEqual(1, restored)
        self.assertEqual(1, from_backup)
        self.assertEqual(b"AAA\nBBB\n", self.entry.read_bytes())

    def test_restore_strips_when_no_backup(self):
        # 无留底时退回摘行，也要摘干净
        ehp.atomic_write_bytes(self.entry, b"AAA\nBBB\n" + self.p.stub_line.encode("utf-8"))
        restored, from_backup, _ = ehp.restore(self.p)
        self.assertEqual(1, restored)
        self.assertEqual(0, from_backup)
        self.assertFalse(ehp.has_stub(self.p, self.entry))

    def test_ensure_replaces_legacy_stub(self):
        ehp.atomic_write_bytes(
            self.entry,
            b"AAA\n" + b"try{require(require('os').homedir()+'/.salak/hook.js')}catch(e){}\n")
        n, note = ehp.ensure_stub(self.p)
        self.assertEqual(1, n)
        self.assertIn("旧版", note)
        raw = self.entry.read_bytes()
        self.assertTrue(ehp.has_stub(self.p, self.entry))
        self.assertEqual(1, len([ln for ln in raw.splitlines() if ehp.is_own_line(self.p, ln)]))


class UninstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.b = _bill(self.home)
        self.entry = self.home / "extensionHostProcess.js"
        self.entry.write_bytes(b"code\n" + self.b.stub_line.encode("utf-8"))
        self.b.hook_dst.parent.mkdir(parents=True, exist_ok=True)
        self.b.hook_dst.write_text("hook", encoding="utf-8")
        self.signal = self.b.home / "bill.json"
        self.signal.write_text("{}", encoding="utf-8")
        self._orig = ehp.ext_host_entries
        ehp.ext_host_entries = lambda plugin, probe=True: [self.entry]

    def tearDown(self):
        ehp.ext_host_entries = self._orig
        self.tmp.cleanup()

    def test_uninstall_strips_stub_and_deletes_owned_files(self):
        removed, files, note = ehp.uninstall(self.b, owned_files=[self.b.hook_dst, self.signal])
        self.assertEqual(1, removed)
        self.assertEqual(2, files)
        self.assertFalse(ehp.has_stub(self.b, self.entry))
        self.assertFalse(self.b.hook_dst.exists())
        self.assertFalse(self.signal.exists())
        self.assertIn("需 Reload Window", note)


if __name__ == "__main__":
    unittest.main()
