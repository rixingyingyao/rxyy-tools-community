# -*- coding: utf-8 -*-
"""持久页轮询必须单飞，并在 iframe 恢复可见时错峰唤醒。"""
import re
import subprocess
import unittest
from pathlib import Path


UI_PATH = Path(__file__).resolve().parents[1] / "ui.html"
SHARE_PATH = Path(__file__).resolve().parents[1] / "share.html"


BODY_TAIL = "})().catch(err => { console.error(err.stack || err); process.exit(1); });"


class UiPollingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = UI_PATH.read_text(encoding="utf-8")
        cls.share_html = SHARE_PATH.read_text(encoding="utf-8")

    def _run_node(self, script):
        """跑一段抠出来的真代码，并确认测试体真跑到了最后一行。

        node 碰上「事件循环空了但 await 还挂着」是静默 exit 0——桩里少个变量、
        promise 永不落地，一句断言都没执行也照样绿。只看 returncode 挡不住。
        """
        self.assertIn(BODY_TAIL, script, "测试体没有用标准收尾，跑到底的记号插不进去")
        script = script.replace(BODY_TAIL, "  console.log('__DONE__');\n" + BODY_TAIL)
        proc = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            check=True, text=True, capture_output=True,
        )
        self.assertIn("__DONE__", proc.stdout or "",
                      "测试体没跑到最后就退出了（await 挂住了，node 不会报错）")

    def test_every_tick_entry_shares_one_inflight_chain(self):
        start = self.html.index("let tickFlight = null;")
        end = self.html.index("/* ---------- 轮询节奏", start)
        tick_code = self.html[start:end]
        script = r"""
let calls = 0, active = 0, maxActive = 0;
const forces = [], waits = [];
async function tickOnce(force) {
  calls++; active++; maxActive = Math.max(maxActive, active); forces.push(!!force);
  await new Promise(resolve => waits.push(resolve));
  active--;
}
%s
function turn() { return new Promise(resolve => setImmediate(resolve)); }
(async () => {
  const first = tick(false);
  await turn();
  const second = tick(true);
  await turn();
  if (calls !== 1 || maxActive !== 1) throw new Error("请求发生叠加");
  waits.shift()();
  while (calls < 2) await turn();
  if (maxActive !== 1) throw new Error("补刷与旧请求叠加");
  waits.shift()();
  await Promise.all([first, second]);
  if (calls !== 2) throw new Error("飞行中的刷新没有合并为一次补刷");
  if (JSON.stringify(forces) !== "[false,true]") throw new Error("force 标记丢失");
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""" % tick_code
        self._run_node(script)

    def test_visibility_wake_is_staggered_and_pending_wake_is_kept(self):
        self.assertIn("let timer = null, running = false, pendingWake = false", self.html)
        self.assertIn("if (running) pendingWake = true", self.html)
        self.assertIn("pollers.forEach(p => p.wake(p.primary))", self.html)
        self.assertIn("startPoll(tick, 700, 2500, 30000, true)", self.html)
        self.assertIn("if (!window.ResizeObserver) return", self.html)

    def test_share_tick_entries_share_one_inflight_chain_without_force_recursion(self):
        start = self.share_html.index("let tickFlight = null;")
        end = self.share_html.index("/* 轮询节奏自适应", start)
        tick_code = self.share_html[start:end]
        once_start = self.share_html.index("async function tickOnce(force)")
        once_end = self.share_html.index("let tickFlight = null;", once_start)
        self.assertNotIn("return tick(true);", self.share_html[once_start:once_end])

        script = r"""
let calls = 0, active = 0, maxActive = 0;
const forces = [], waits = [];
async function tickOnce(force) {
  calls++; active++; maxActive = Math.max(maxActive, active); forces.push(!!force);
  await new Promise(resolve => waits.push(resolve));
  active--;
}
%s
function turn() { return new Promise(resolve => setImmediate(resolve)); }
(async () => {
  const initial = tick(false);
  await turn();
  const resumed = tick(true);
  if (calls !== 1 || maxActive !== 1) throw new Error("分享页状态请求发生叠加");
  waits.shift()();
  while (calls < 2) await turn();
  if (maxActive !== 1) throw new Error("补刷与旧请求叠加");
  waits.shift()();
  await Promise.all([initial, resumed]);
  if (JSON.stringify(forces) !== "[false,true]") throw new Error("force 补刷被丢弃");
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""" % tick_code
        self._run_node(script)

    def test_secondary_refreshes_share_one_flight_queue_one_tail_and_reopen(self):
        start = self.html.index("function singleFlight(fn) {")
        end = self.html.index("/* ---------- 轮询节奏", start)
        single_flight = self.html[start:end]
        names = ("billRefresh", "parkRefresh", "lockRefresh", "teamRefresh")
        wrappers = []
        for name in names:
            self.assertIn("async function %sOnce()" % name, self.html)
            match = re.search(r"const %s = singleFlight\(%sOnce\);" % (name, name), self.html)
            self.assertIsNotNone(match)
            wrappers.append(match.group())

        script = r"""
%s
const names = ["billRefresh", "parkRefresh", "lockRefresh", "teamRefresh"];
const calls = Object.fromEntries(names.map(name => [name, 0]));
const active = Object.fromEntries(names.map(name => [name, 0]));
const maxActive = Object.fromEntries(names.map(name => [name, 0]));
const waits = Object.fromEntries(names.map(name => [name, []]));
function makeRefresh(name) {
  return async () => {
    calls[name]++;
    active[name]++;
    maxActive[name] = Math.max(maxActive[name], active[name]);
    await new Promise(resolve => waits[name].push(resolve));
    active[name]--;
  };
}
const billRefreshOnce = makeRefresh("billRefresh");
const parkRefreshOnce = makeRefresh("parkRefresh");
const lockRefreshOnce = makeRefresh("lockRefresh");
const teamRefreshOnce = makeRefresh("teamRefresh");
%s
const refreshers = { billRefresh, parkRefresh, lockRefresh, teamRefresh };
function turn() { return new Promise(resolve => setImmediate(resolve)); }
(async () => {
  for (const [name, refresh] of Object.entries(refreshers)) {
    const scheduled = refresh();
    await turn();
    const manual = refresh();
    if (scheduled !== manual) throw new Error(name + " did not return its shared in-flight chain");
    await turn();
    if (calls[name] !== 1 || maxActive[name] !== 1) {
      throw new Error(name + " overlapped the scheduled and manual requests");
    }
    waits[name].shift()();
    while (calls[name] < 2) await turn();
    if (maxActive[name] !== 1) throw new Error(name + " overlapped its trailing refresh");
    waits[name].shift()();
    await Promise.all([scheduled, manual]);
    const afterCompletion = refresh();
    await turn();
    if (calls[name] !== 3 || maxActive[name] !== 1) {
      throw new Error(name + " did not accept a fresh request after completion");
    }
    waits[name].shift()();
    await afterCompletion;
  }
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""" % (single_flight, "\n".join(wrappers))
        self._run_node(script)

    def test_legacy_intervals_are_gone(self):
        for old in (
            "setInterval(tick, 700)",
            "setInterval(billRefresh, 3000)",
            "setInterval(parkRefresh, 1200)",
            "setInterval(lockRefresh, 3000)",
        ):
            self.assertNotIn(old, self.html)


if __name__ == "__main__":
    unittest.main()
