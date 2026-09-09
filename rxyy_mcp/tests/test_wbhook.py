# -*- coding: utf-8 -*-
import tempfile
import time
import unittest
from pathlib import Path

import wbhook


def _fake_app(root: Path, bundle_js: str, html: str = None):
    html_dir = root / "out" / "vs" / "code" / "electron-sandbox" / "workbench"
    bun_dir = root / "out" / "vs" / "workbench"
    html_dir.mkdir(parents=True)
    bun_dir.mkdir(parents=True)
    (html_dir / "workbench.html").write_text(
        html or "<html><body></body>\n</html>\n", encoding="utf-8")
    (bun_dir / "workbench.desktop.main.js").write_text(bundle_js, encoding="utf-8")
    return root


class WbhookScoreTests(unittest.TestCase):
    def test_locked_viewdescriptor_pair_wins(self):
        pad = "var x=1;" * 40
        text = (
            "this.composerDataService=z,mockAgentStreamService=x," + pad
            + "this.viewDescriptorService=i,this.composerDataService=r,this.foo=1,"
            + pad + "this.composerDataService=q,backgroundComposerDataService=b,"
        )
        hits = wbhook.find_anchors(text, "data")
        self.assertTrue(hits)
        self.assertIn("composerDataService=r", hits[0][2])

    def test_test_mocks_are_skipped(self):
        text = "setupAIServiceMocking();this.composerDataService=e,this.getHandleIfLoaded=1,"
        self.assertEqual([], wbhook.find_anchors(text, "data"))

    def test_locked_wins_even_when_background_word_is_nearby(self):
        pad = "backgroundComposerDataService;" + "var x=1;" * 40
        text = (
            pad
            + "this.viewDescriptorService=i,this.composerDataService=r,this.foo=1,"
            + pad
            + "this.composerDataService=q,this.getHandleIfLoaded=1,"
            + "this.updateComposerData=1,this.composerChatService=a,"
        )
        hits = wbhook.find_anchors(text, "data")
        self.assertTrue(hits)
        self.assertIn("composerDataService=r", hits[0][2])


class WbhookInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_install_and_uninstall_on_a_fake_app(self):
        app = _fake_app(
            self.root,
            "prefix;this.viewDescriptorService=i,this.composerDataService=r,this.x=1;"
            "mid;this.composerChatService=a,submitChatMaybeAbortCurrent=1,updateComposerData=1;"
            "tail;",
        )
        r = wbhook.install(app, port=38777)
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["data_hooked"])
        st = wbhook.status(app)
        self.assertTrue(st["installed"])
        html = (app / "out/vs/code/electron-sandbox/workbench/workbench.html").read_text(encoding="utf-8")
        self.assertIn("chijiu-wbhook", html)
        js = app / "out/vs/code/electron-sandbox/workbench/chijiu-wbhook.js"
        self.assertTrue(js.is_file())
        self.assertIn("38777", js.read_text(encoding="utf-8"))
        bun = (app / "out/vs/workbench/workbench.desktop.main.js").read_text(encoding="utf-8")
        self.assertIn(wbhook.DATA_MARK, bun)
        self.assertIn("__chijiuComposer", bun)
        self.assertGreaterEqual(bun.count(wbhook.DATA_MARK), 1)
        u = wbhook.uninstall(app)
        self.assertTrue(u["ok"])
        bun2 = (app / "out/vs/workbench/workbench.desktop.main.js").read_text(encoding="utf-8")
        self.assertNotIn(wbhook.DATA_MARK, bun2)
        self.assertFalse(js.is_file())

    def test_reinstall_adds_more_data_sites(self):
        pad = "var x=1;" * 40
        js = (
            "this.viewDescriptorService=i,this.composerDataService=r,this.x=1;"
            + pad
            + "this.composerDataService=s,this.getHandleIfLoaded=1,this.updateComposerData=1;"
            + pad
            + "this.composerChatService=a,submitChatMaybeAbortCurrent=1,updateComposerData=1;"
        )
        app = _fake_app(self.root, js)
        r1 = wbhook.install(app)
        self.assertTrue(r1["ok"], r1)
        r2 = wbhook.install(app)
        self.assertTrue(r2["ok"], r2)
        bun = (app / "out/vs/workbench/workbench.desktop.main.js").read_text(encoding="utf-8")
        self.assertGreaterEqual(bun.count(wbhook.DATA_MARK), 2)

    def test_missing_anchor_is_soft_fail_not_crash(self):
        app = _fake_app(self.root, "no composer services here at all;")
        r = wbhook.install(app)
        self.assertTrue(r["ok"], r)
        self.assertFalse(r["data_hooked"])

    def test_self_injects_on_composer_data_ctor(self):
        js = (
            'qs=rn("composerDataService"),k4=class extends Ce{constructor(e){super(),'
            "this._storageService=e,this.x=1;}"
            "this.viewDescriptorService=i,this.composerDataService=r,this.foo=1;"
        )
        app = _fake_app(self.root, js)
        r = wbhook.install(app)
        self.assertTrue(r["ok"], r)
        bun = (app / "out/vs/workbench/workbench.desktop.main.js").read_text(encoding="utf-8")
        self.assertIn(wbhook.SELF_DATA_MARK, bun)
        self.assertIn("__chijiuComposer=this", bun)
        st = wbhook.status(app)
        self.assertTrue(st.get("self_data") or st.get("self_data_hooked"))


class WbhookRuntimeTests(unittest.TestCase):
    def setUp(self):
        wbhook.reset_runtime()
    def tearDown(self):
        wbhook.reset_runtime()

    def test_rename_many_is_empty_without_heartbeat(self):
        self.assertEqual({}, wbhook.rename_many([("abc", "名")]))

    def test_overlay_turn_uses_fresh_hook_steps(self):
        turn = {"steps": [], "live": False, "reply": None, "total_steps": 0}
        snap = {
            "ok": True, "recv_at": time.time(), "live": True, "at": 1,
            "thinking": "正在对照显示",
            "reply": "桥挂上了",
            "steps": [{"kind": "tool", "name": "Shell", "status": "loading",
                       "bubbleId": "b1", "summary": "py -3.11 x.py",
                       "why": "打印提交数"}],
        }
        out = wbhook.overlay_turn(turn, snap)
        self.assertEqual("wbhook", out["via"])
        self.assertTrue(out["live"])
        kinds = [s["kind"] for s in out["steps"]]
        self.assertIn("thinking", kinds)
        self.assertIn("tool", kinds)
        tool = next(s for s in out["steps"] if s["kind"] == "tool")
        self.assertEqual("running", tool["status"])
        self.assertEqual("运行命令", tool["label"])
        self.assertEqual("打印提交数", tool["why"])
        self.assertEqual("桥挂上了", out["reply"]["text"])

    def test_overlay_ignores_stale_snap(self):
        turn = {"steps": [{"kind": "text", "id": "t", "text": "old"}], "live": False}
        snap = {"ok": True, "recv_at": time.time() - 9, "live": True, "reply": "new"}
        out = wbhook.overlay_turn(turn, snap)
        self.assertNotEqual("wbhook", out.get("via"))
        self.assertEqual("old", out["steps"][0]["text"])

    def test_poll_ack_ingest_roundtrip(self):
        wbhook.beat({"ready": True})
        self.assertTrue(wbhook.hook_alive())
        rid = wbhook.enqueue("rename", composerId="u1", name="A")
        payload = wbhook.poll_payload()
        self.assertEqual(1, len(payload["cmds"]))
        self.assertEqual(rid, payload["cmds"][0]["id"])
        wbhook.ack(rid, {"ok": True, "via": "t"})
        done = wbhook.rename_many([("u2", "B")], timeout=0.5)
        # 没人 ack u2，所以空
        self.assertEqual({}, done)
        wbhook.ingest([{"composerId": "u1", "ok": True, "reply": "hello", "live": True}])
        snap = wbhook.latest_turn("u1")
        self.assertEqual("hello", snap["reply"])
        self.assertLess(time.time() - snap["recv_at"], 1)


if __name__ == "__main__":
    unittest.main()
