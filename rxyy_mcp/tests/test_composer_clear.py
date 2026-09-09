# -*- coding: utf-8 -*-
"""发出去的话不许再回到输入框，以及图片要能复制（rxyy 2026-08-24 两笔）。

输入框：消息明明发出去了，输入框里还留着上一轮那句话，连粘贴的图片缩略图一起。
根因不在「清空」——doSend 一直都在清——而在清空没等落库：`save_draft(id, "")` 是
fire-and-forget，紧接着 `tick(true)` 就去拉 get_messages，hub 那边草稿还没清干净，
于是原样回给前端，renderActive 认为「本地没草稿、框也是空的」，就把它填了回来。
防抖那次存稿还会在 send_reply 的 await 中间开火，跟清空抢先后，抢赢了同样回填。

复制：这个 WebView 里 navigator.clipboard 整个不存在（share.html 顶上那条 08-12 的
实测注释就是为它写的），execCommand 又只搬得动文字，所以图片只能由后端去放。
"""
import base64
import re
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

# hub 的清空/图片复制契约不需要真实 WebView。开发机未安装 pywebview 时，
# 给 hub 一个只覆盖 import/窗口启动边界的桩，仍然运行下面所有真实 Api.copy_image
# 和 Node 发送测试；不要因为缺少桌面依赖而跳过整组测试。
try:
    import webview  # type: ignore  # noqa: F401
except ModuleNotFoundError as exc:
    if exc.name != "webview":
        raise
    webview_stub = types.ModuleType("webview")
    webview_stub.screens = []
    webview_stub.create_window = lambda *args, **kwargs: None
    webview_stub.start = lambda *args, **kwargs: None
    sys.modules["webview"] = webview_stub

import hub  # noqa: E402

UI_PATH = MODULE_DIR / "ui.html"

# 桩：把 doSend 依赖的那一圈全部换成可观测的假件，只留草稿与刷新的先后关系
HARNESS = r"""
let activeId = "s1";
const drafts = {};
let selected = [], images = [], files = [];
const MAX_TOTAL_MB = 20;
const lastRev = {};
const hubSide = { draft: "上一轮就在的旧稿", images: [], files: [] };
const saveLog = [];         // 落库顺序（发起顺序）
const landedLog = [];       // 真正写进 hub 的顺序
const toasts = [];
const ticks = [];
const ta = { value: "" };
let releaseSend = null;
let sendResult = { ok: true };
let slowSave = false;       // 非空存稿走慢路：模拟 IPC 排队
let slowClear = false;      // 清空走慢路：看刷新有没有等它
let staleDraftAtTick = null;  // 在飞的旧 get_messages 快照，不等于此刻 hubSide.draft
const sending = new Set();  // doSend 的在途闸（见 test_send_inflight）
function renderInput() {}   // 在途闸灰按钮时会回调它，这里不关心渲染
function armPinLastUser() {} // doSend 成功后的真实 UI 行为与本组草稿断言无关
function pushInputHist() {}  // doSend 发送成功会记输入历史，本套测试不关心历史

function $(id) { if (id === "ta") return ta; throw new Error("未预料的元素 " + id); }
function toast(m, ok) { toasts.push([m, !!ok]); }
function activeSession() {
  return { id: "s1", connected: true, pending: {}, reconnecting: false, ide_active: true };
}
async function tick(force) { ticks.push({ force: !!force, draftAtTick: hubSide.draft });
  // 模拟 refreshActive：框空就把 hub 草稿灌回来（含「在飞的旧 get_messages 快照」）
  const snap = (staleDraftAtTick != null) ? staleDraftAtTick : hubSide.draft;
  staleDraftAtTick = null;
  const fake = { draft: snap, draft_images: [], draft_files: [] };
  if (typeof shouldSkipHubDraftRestore === "function" && shouldSkipHubDraftRestore(activeId, fake)) {
    /* 刚发走的回声，不灌 */
  } else if (!ta.value.trim() && snap) {
    ta.value = snap;
  }
}
function api() {
  return {
    save_draft(sid, text, imgs, fls) {
      saveLog.push(text);
      const delay = text === "" ? (slowClear ? 20 : 0) : (slowSave ? 20 : 0);
      return new Promise(res => setTimeout(() => {
        hubSide.draft = text;
        hubSide.images = imgs || [];
        hubSide.files = fls || [];
        landedLog.push(text);
        res({ ok: true });
      }, delay));
    },
    send_reply() { return new Promise(res => { releaseSend = () => res(sendResult); }); },
    queue_message() { return new Promise(res => { releaseSend = () => res(sendResult); }); },
  };
}
function turn() { return new Promise(resolve => setImmediate(resolve)); }
function fail(msg) { throw new Error(msg); }
"""


def _ui_source():
    return UI_PATH.read_text(encoding="utf-8")


def _slice(html, start_marker, end_marker):
    start = html.index(start_marker)
    return html[start:html.index(end_marker, start)]


BODY_TAIL = "})().catch(err => { console.error(err.stack || err); process.exit(1); });"


def _run_js(body):
    """把 ui.html 里草稿与发送那两段真代码抠出来，配上桩在 node 里跑一遍。

    收尾要求测试体打个记号：node 碰上「事件循环空了但 await 还挂着」是静默
    exit 0，一句断言都没跑到也照样绿。只看 returncode 挡不住这种假通过。
    """
    if BODY_TAIL not in body:
        raise AssertionError("测试体没有用标准收尾，跑到底的记号插不进去")
    html = _ui_source()
    script = "\n".join([
        HARNESS,
        _slice(html, "let draftChain = Promise.resolve();", "function loadDraft(sid)"),
        _slice(html, "async function doSend(isContinue)", "/* ---------- footer ---------- */"),
        body.replace(BODY_TAIL, "  console.log('__DONE__');\n" + BODY_TAIL),
    ])
    proc = subprocess.run(["node", "--input-type=module", "--eval", script],
                          check=False, text=True, capture_output=True)
    if proc.returncode:
        detail = (proc.stderr or "") + (proc.stdout or "")
        if len(detail) > 4000:
            detail = detail[:4000] + "\n...[Node 输出已截断]"
        raise AssertionError(detail)
    if "__DONE__" not in (proc.stdout or ""):
        raise AssertionError("测试体没跑到最后就退出了（await 挂住了，node 不会报错）")


class ComposerClearTests(unittest.TestCase):
    def test_the_refresh_waits_until_the_cleared_draft_has_landed(self):
        """清空没落库就去 tick，get_messages 会把刚发走那句原样递回来。"""
        _run_js(r"""
(async () => {
  ta.value = "刚发出去的这一句";
  slowClear = true;
  const p = doSend(false);
  await turn();
  releaseSend();
  await p;
  if (ticks.length !== 1) fail("刷新次数不对：" + ticks.length);
  if (ticks[0].draftAtTick !== "")
    fail("刷新时 hub 上的草稿还是「" + ticks[0].draftAtTick + "」，会被读回输入框");
  if (ta.value !== "") fail("输入框没清空");
  if (images.length || files.length) fail("附件没清空");
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")

    def test_a_stale_inflight_get_messages_does_not_put_the_sent_line_back(self):
        """发送后那一拍 tick 若带着发出之前的 get_messages 快照，旧稿不能灌回框。"""
        _run_js(r"""
(async () => {
  ta.value = "刚发出去的这一句";
  staleDraftAtTick = "刚发出去的这一句";
  const p = doSend(false);
  await turn();
  releaseSend();
  await p;
  if (ta.value !== "") fail("旧快照把已发的话灌回输入框了：" + JSON.stringify(ta.value));
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")

    def test_a_debounced_save_in_flight_cannot_land_after_the_clear(self):
        """存稿已经在飞时清空必须排在它后面，否则旧稿最后一个落库、赢了。"""
        _run_js(r"""
(async () => {
  ta.value = "刚发出去的这一句";
  slowSave = true;
  saveDraft();                 // 防抖开过火了，这次存稿正在路上
  const p = doSend(false);
  releaseSend();
  await p;
  if (hubSide.draft !== "")
    fail("hub 上最后落的是「" + hubSide.draft + "」");
  if (landedLog.join("|") !== "刚发出去的这一句|")
    fail("落库顺序反了：" + landedLog.join("|"));
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")

    def test_sending_cancels_the_pending_debounce_instead_of_racing_it(self):
        """防抖若不掐，它会在 send_reply 的 await 中间拿还没清的输入框存一次。"""
        _run_js(r"""
(async () => {
  ta.value = "刚发出去的这一句";
  draftTimer = setTimeout(saveDraft, 5);      // 跟 input 事件里排的那次一样
  const p = doSend(false);
  setTimeout(() => releaseSend(), 30);        // 发送 30ms 才回，防抖本会在中途开火
  await p;
  const stale = saveLog.filter(t => t !== "");
  if (stale.length) fail("防抖存稿在发送中途开了火：" + stale.join("|"));
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")

    def test_a_failed_send_keeps_the_text_and_still_persists_it(self):
        """掐了防抖又没发出去，字不能就这么只剩在内存里。"""
        _run_js(r"""
(async () => {
  ta.value = "没发出去的这一句";
  draftTimer = setTimeout(saveDraft, 5);
  sendResult = { ok: false, error: "hub 掉线了" };
  const p = doSend(false);
  releaseSend();
  await p;
  if (ta.value !== "没发出去的这一句") fail("发送失败却把字清了");
  if (saveLog[saveLog.length - 1] !== "没发出去的这一句") fail("失败后没把草稿补存下来");
  if (ticks.length) fail("没发出去还去刷新了");
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")

    def test_the_clear_is_awaited_in_source_not_fire_and_forget(self):
        # 落库的内容后来从写死的 "" 改成了「框里还剩什么」（往返期间新打的字
        # 不能被清掉，见 test_send_inflight），发给谁也从 s.id 改成了出发时就
        # 钉死的 sid（往返期间人可能切了 tab，同上），但「必须 await 到真落库
        # 再刷新」这条契约没变，本测试锁的就是它
        html = _ui_source()
        self.assertIn('await pushDraft(sid, $("ta").value, images, files);', html)
        self.assertNotIn('api().save_draft(s.id, "").catch', html)


def _boom(*a, **kw):
    raise AssertionError("这一档不该真去开进程")


class CopyImageTests(unittest.TestCase):
    def test_a_message_image_is_put_on_the_clipboard_by_a_short_lived_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "图片").mkdir()
            png = root / "图片" / "截图.png"
            png.write_bytes(b"\x89PNG\r\n")
            seen = []

            def fake_run(cmd, **kw):
                seen.append((cmd, kw))
                return subprocess.CompletedProcess(cmd, 0, b"", b"")

            with patch.object(hub.HUB, "cfg", {"history_dir": str(root)}):
                with patch.object(subprocess, "run", fake_run):
                    r = hub.Api().copy_image(str(png))
        self.assertTrue(r["ok"], r)
        cmd, kw = seen[0]
        self.assertIn("-STA", cmd)          # Clipboard 只在 STA 线程上放得进去
        # $false 的话剪贴板只拿到一个指向 PowerShell 的引用，进程一退粘出来是空的
        self.assertIn("SetDataObject($img,$true)", cmd[-1])
        # 比对用 resolve 后的形态：%TEMP% 常给 8.3 短名（ADMINI~1），
        # 而 copy_image 会把路径解析成长名（Administrator），字面量对不上是
        # 环境噪音不是缺陷（08-27 公司机实测）
        self.assertIn(str(png.resolve()), cmd[-1])
        # 07-27 僵尸事故：工作线程里建 Tk 根窗口会连 GIL 一起锁死整个进程
        self.assertNotIn("tkinter", " ".join(str(x) for x in cmd))
        self.assertEqual(kw.get("creationflags"), 0x08000000)

    def test_a_pasted_preview_is_spilled_to_disk_and_the_temp_file_is_removed(self):
        seen = {}

        def fake_run(cmd, **kw):
            path = re.search(r"FromFile\('([^']+)'\)", cmd[-1]).group(1)
            seen["path"] = path
            # PowerShell 还没跑完就删掉的话，它打开的是个不存在的文件
            seen["during"] = Path(path).is_file()
            seen["bytes"] = Path(path).read_bytes()
            return subprocess.CompletedProcess(cmd, 0, b"", b"")

        blob = b"\x89PNG\r\n" + "粘贴来的图".encode("utf-8")
        with patch.object(subprocess, "run", fake_run):
            r = hub.Api().copy_image(
                "data:image/png;base64," + base64.b64encode(blob).decode())
        self.assertTrue(r["ok"], r)
        self.assertTrue(seen["during"])
        self.assertEqual(seen["bytes"], blob)
        self.assertFalse(Path(seen["path"]).exists())   # 复制完不留垃圾

    def test_a_path_outside_the_image_folder_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "图片").mkdir()
            outsider = root / "别处的东西.png"
            outsider.write_bytes(b"\x89PNG\r\n")
            with patch.object(hub.HUB, "cfg", {"history_dir": str(root)}):
                with patch.object(subprocess, "run", _boom):
                    r = hub.Api().copy_image(str(outsider))
        self.assertFalse(r["ok"])
        self.assertIn("图片目录", r["error"])

    def test_an_oversized_preview_is_refused_before_anything_hits_the_disk(self):
        huge = "data:image/png;base64," + "A" * (hub.Api.COPY_IMAGE_CAP + 4)
        with patch.object(subprocess, "run", _boom):
            r = hub.Api().copy_image(huge)
        self.assertFalse(r["ok"])
        self.assertIn("太大", r["error"])

    def test_a_failing_clipboard_call_says_so_instead_of_claiming_success(self):
        def fake_run(cmd, **kw):
            return subprocess.CompletedProcess(
                cmd, 1, b"", "剪贴板被别的进程占着".encode("utf-8"))

        with patch.object(subprocess, "run", fake_run):
            r = hub.Api().copy_image(
                "data:image/png;base64," + base64.b64encode(b"x").decode())
        self.assertFalse(r["ok"])
        self.assertIn("剪贴板", r["error"])

    def test_a_missing_file_is_reported_rather_than_handed_to_powershell(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "图片").mkdir()
            with patch.object(hub.HUB, "cfg", {"history_dir": str(root)}):
                with patch.object(subprocess, "run", _boom):
                    r = hub.Api().copy_image(str(root / "图片" / "早被清掉了.png"))
        self.assertFalse(r["ok"])
        self.assertIn("不在了", r["error"])


class LightboxCopyUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = _ui_source()

    def test_the_lightbox_offers_copy_and_the_button_itself_does_not_close_it(self):
        self.assertIn('<button id="lightboxCopy">', self.html)
        self.assertIn('e.target.id !== "lightboxOpen" && e.target.id !== "lightboxCopy"',
                      self.html)
        self.assertIn("api().copy_image(lightboxCopySrc)", self.html)

    def test_the_proxy_url_is_never_handed_to_the_backend_as_the_copy_source(self):
        # 消息图的 src 是 /img 代理地址，后端照着它取不到内容，只能递磁盘路径
        self.assertIn(
            'lightboxCopySrc = lightboxPath || (/^data:image\\//.test(src) ? src : null)',
            self.html)
        self.assertIn('$("lightboxCopy").style.display = lightboxCopySrc ? "" : "none"',
                      self.html)


class CompactResult(unittest.TextTestResult):
    """Node 失败可能带整段 HTML/堆栈；保留首段上下文，避免淹没断言。"""

    def _exc_info_to_string(self, err, test):
        text = super()._exc_info_to_string(err, test)
        return text if len(text) <= 5000 else text[:5000] + "\n...[测试输出已截断]\n"


if __name__ == "__main__":
    unittest.main(testRunner=unittest.TextTestRunner(resultclass=CompactResult))
