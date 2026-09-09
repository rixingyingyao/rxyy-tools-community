"""Desktop/mobile controls expose one quota-explicit, lockable resume action."""
import unittest
import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UI = (ROOT / "ui.html").read_text(encoding="utf-8")
SHARE = (ROOT / "share.html").read_text(encoding="utf-8")
GATEWAY = (ROOT / "gateway.py").read_text(encoding="utf-8")


class NativeResumeUiTests(unittest.TestCase):
    def test_desktop_action_is_native_only_quota_explicit_and_server_guarded(self):
        self.assertIn('data-action="native-resume"', UI)
        self.assertIn('>▶ 继续原任务</button>', UI)
        self.assertIn("适用于已中断任务；点击会唤醒模型并消耗额度", UI)
        self.assertIn('runtimeKindOf(ms) === "codex" && ms.native_thread_id', UI)
        self.assertIn("resumeBtn.disabled = !!(ms && ms.native_resume_disabled_reason)", UI)
        self.assertIn("api().resume_native_task(sessionId, menuSession.native_thread_id)", UI)

    def test_mobile_action_uses_same_session_and_unknown_lock(self):
        self.assertIn('sheetButton("继续原任务"', SHARE)
        self.assertIn('api("/api/native_resume", {', SHARE)
        self.assertIn("sid, thread_id: s.native_thread_id", SHARE)
        self.assertIn("resume.disabled = !!s.native_resume_disabled_reason", SHARE)
        self.assertIn("适用于已中断任务；点击会唤醒模型并消耗额度", SHARE)

    def test_gateway_allows_owner_wait_budget(self):
        self.assertIn("resume_native_task: 30000", GATEWAY)

    @unittest.skipUnless(shutil.which("node"), "Node is required for actual JS handler checks")
    def test_actual_handlers_block_double_click_and_unknown_resend(self):
        desktop = UI.split('    if (action === "native-resume") {', 1)[1]
        desktop = 'if (action === "native-resume") {' + desktop.split('    if (action === "ext-reopen")', 1)[0]
        mobile = SHARE.split('  if (runtimeKindOf(s) === "codex" && s.native_thread_id) {', 1)[1]
        mobile = 'if (runtimeKindOf(s) === "codex" && s.native_thread_id) {' + mobile.split('  if (s.conv_key)', 1)[0]
        script = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const snippets = JSON.parse(process.argv[1]);
(async () => {
  for (const platform of ['desktop', 'mobile']) {
    for (const unknown of [false, true]) {
      let calls = 0, release;
      const operation = () => {
        calls++;
        return new Promise((resolve, reject) => {
          release = () => unknown ? reject(Error('network lost')) : resolve({ok:true});
        });
      };
      const c = {
        Set, action: 'native-resume', sessionId: 'S', sid: 'S', nick: 'test',
        menuSession: {native_thread_id:'T'}, s: {native_thread_id:'T'}, button:{},
        nativeResumeInFlight: new Set(), nativeResumeUnknown: new Set(),
        toast: () => {}, tick: async () => {}, closeSheet: () => {},
        runtimeKindOf: () => 'codex',
        api: platform === 'desktop' ? () => ({resume_native_task:operation}) : operation,
        sheetButton: (title, detail, handler) => ({title, detail, handler}),
        box: {appendChild: button => { c.mobileButton = button; }},
      };
      vm.createContext(c);
      if (platform === 'desktop') {
        vm.runInContext('run = async () => {' + snippets.desktop + '}', c);
      } else {
        vm.runInContext('render = () => {' + snippets.mobile + '}; render()', c);
        c.run = () => c.mobileButton.handler();
      }
      const first = c.run();
      await c.run();
      assert.equal(calls, 1, platform + ': concurrent double click');
      release();
      await first;
      assert.equal(c.nativeResumeInFlight.size, 0);
      if (unknown) {
        await c.run();
        assert.equal(calls, 1, platform + ': retry after unknown');
        assert.equal(c.nativeResumeUnknown.has('S'), true);
        if (platform === 'mobile') {
          c.render();
          assert.equal(c.mobileButton.disabled, true);
        }
      }
    }
  }
  console.log('RESUME_HANDLERS_OK');
})().catch(e => { console.error(e); process.exitCode = 1; });
'''
        result = subprocess.run([shutil.which("node"), "-e", script,
                                 json.dumps({"desktop": desktop, "mobile": mobile})],
                                capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("RESUME_HANDLERS_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
