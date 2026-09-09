# -*- coding: utf-8 -*-
"""发送要有在途闸，而且不能把往返期间新打的字吞掉。

08-25 全面体检查出。两个页面的发送按钮 disabled 只由「有没有内容 + 会话状态」
算，一次发送在飞的时候它照样是可点的：

* 桌面页（ui.html）Ctrl+Enter 或点两下按钮 = 两次 send_reply/queue_message，
  同一条消息连人带附件发两遍；
* 手机页（share.html）更容易撞——tapBind 只吞掉 600ms 内的重复点，而带附件时
  那次 await 的超时上限是 60 秒起步，人等一两秒没反应必然再点一下。

第二件事同源：往返回来后无脑 `$("ta").value = ""`，人在这一两秒里接着打的下
一句会被一起清掉（rxyy 08-24 抱怨过输入框状态不对，那次修的是草稿回灌，这条
是另一半）。契约：只收走真正发出去的那一段，剩下的留在框里。

测法沿用 test_ui_polling.py 的路子：把真实函数体抠出来丢进 node 跑，不是看
源码里有没有那行字。
"""
import subprocess
import unittest
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
UI_PATH = MODULE_DIR / "ui.html"
SHARE_PATH = MODULE_DIR / "share.html"

TURN = """
function turn() { return new Promise(resolve => setImmediate(resolve)); }
"""

# 每个测试体都用这一行收尾，_node 靠它把「跑到底了」的记号插进去
BODY_TAIL = "})().catch(err => { console.error(err.stack || err); process.exit(1); });"

# --- 桌面页 doSend 的替身环境 ---------------------------------------------
UI_STUBS = """
const MAX_TOTAL_MB = 50;
let images = [], files = [], selected = [], drafts = {}, lastRev = {};
let draftTimer = null;
const sending = new Set();
const ta = { value: "" };
function $(id) { return id === "ta" ? ta : { value: "", disabled: false }; }
function toast() {}
function armPinLastUser() {} // 真实页面发送成功后会钉住最后一条用户消息
// 每次重画记一笔「此刻闸是关着还是开着」：按钮的 disabled 只在 renderInput 里
// 算，闸放开后不再画一次，按钮就一直灰到下一拍轮询
const renders = [];
function renderInput() { renders.push(sending.has("s1")); }
let saveDraftCalls = 0;
function saveDraft() { saveDraftCalls++; }
const drafted = [];
function pushDraft(sid, text, imgs, fls) {
  drafted.push([sid, text, imgs.slice(), fls.slice()]);
  return Promise.resolve();
}
async function tick() {}
function pushInputHist() {}
let session = { id: "s1", connected: true, pending: { id: 1 } };
function activeSession() { return session; }
const liveTurns = Object.create(null);
function runtimeKindOf(s) { return String((s && s.runtime_kind) || "cursor").toLowerCase(); }
function nativeTextTarget(s) {
  const turn = liveTurns[s.id] && liveTurns[s.id].turn;
  return runtimeKindOf(s) === "codex" && s.native_desktop_connected
      && turn && turn.turn_id && s.native_thread_id
    ? { threadId: s.native_thread_id, turnId: turn.turn_id, live: !!turn.live } : null;
}
const calls = [], waits = [];
function api() {
  const rec = name => (...a) => {
    calls.push([name, ...a]);
    return new Promise(resolve => waits.push(resolve));
  };
  return {
    send_reply: rec("send_reply"), queue_message: rec("queue_message"),
    send_native_text: rec("send_native_text")
  };
}
"""

# --- 手机页 doSend 的替身环境 ---------------------------------------------
SHARE_STUBS = """
const MAX_TOTAL_MB = 50;
let images = [], files = [], selected = [];
const sending = new Set();
const myQids = new Set();
const views = new Map();
const nick = "rxyy";
const ta = { value: "" };
function $(id) { return id === "ta" ? ta : { value: "", disabled: false }; }
function toast() {}
function armPinLastUser() {} // 真实页面发送成功后会钉住最后一条用户消息
// 见 UI_STUBS 里同名桩的说明
const renders = [];
function renderInput() { renders.push(sending.has("s1")); }
function autoGrow() {}
function bumpMedia() {}
async function tick() {}
let session = { id: "s1", name: "张三", connected: true, pending: { id: 1 } };
function activeSession() { return session; }
const liveTurns = Object.create(null);
function runtimeKindOf(s) { return String((s && s.runtime_kind) || "cursor").toLowerCase(); }
function nativeTextTarget(s) {
  const turn = liveTurns[s.id] && liveTurns[s.id].turn;
  return runtimeKindOf(s) === "codex" && s.native_desktop_connected
      && turn && turn.turn_id && s.native_thread_id
    ? { threadId: s.native_thread_id, turnId: turn.turn_id, live: !!turn.live } : null;
}
// 人在手机上切会话：框清空、选项/图片清空（跟 selectSession 一个样）
function switchTo(next) { session = next; ta.value = ""; selected = []; images = []; files = []; }
const calls = [], waits = [];
let boom = false;            // 置真 = 这次 api 直接抛（模拟超时/断网）
function api(path, body, wait) {
  calls.push([path, body, wait]);
  if (path === "/api/draft") return Promise.resolve({ ok: true });
  if (boom) return Promise.reject(new Error("timeout"));
  return new Promise(resolve => waits.push(resolve));
}
"""

# --- 手机页草稿防抖 + 切会话的替身环境 -------------------------------------
DRAFT_STUBS = """
const ta = { value: "" };
const els = {};
function $(id) {
  if (id === "ta") return ta;
  return els[id] || (els[id] = { style: {}, value: "",
                                 classList: { add() {}, remove() {} } });
}
const sessions = [{ id: "s1", name: "张三" }, { id: "s2", name: "李四" }];
let activeId = "s1";
function activeSession() { return sessions.find(s => s.id === activeId) || null; }
let selected = [], images = [], files = [];
function autoGrow() {}
function bumpMedia() {}
function ensureView() { return {}; }
function showView() {}
function renderSessions() {}
function updateJump() {}
function closeDrawer() {}
function renderInput() {}
async function refreshActive() {}
const calls = [];
function api(path, body) { calls.push([path, body]); return Promise.resolve({ ok: true }); }
"""


def _slice(html, start_marker, end_marker):
    start = html.index(start_marker)
    return html[start:html.index(end_marker, start)]


class NodeCase(unittest.TestCase):
    """把两个页面里的真函数抠出来丢进 node 跑的共用底座。"""

    @classmethod
    def setUpClass(cls):
        ui = UI_PATH.read_text(encoding="utf-8")
        share = SHARE_PATH.read_text(encoding="utf-8")
        cls.ui_send = _slice(ui, "async function doSend(isContinue) {",
                             "/* ---------- footer ---------- */")
        cls.ui_send = _slice(ui, "function nativeDeliveryId() {",
                             "function renderInput(s)") + "\n" + cls.ui_send
        # 草稿落库的三个小函数就挨在 doSend 前面，一起切出来：doSend 收尾要用
        # postDraft，测草稿防抖那组也要用它们
        cls.share_send = _slice(share, "/* 草稿落库：",
                                "/* 上一拍状态包的摘要")
        cls.share_send = _slice(share, "function nativeDeliveryId() {",
                                "function renderInput(s)") + "\n" + cls.share_send
        cls.share_select = _slice(share, "function selectSession(id) {",
                                  "function neighborSession(dir)")

    def _node(self, *parts):
        script = "\n".join(parts)
        # node 对「事件循环空了但 await 还挂着」是静默 exit 0：测试体一句断言都
        # 没跑到，returncode 照样是 0。只看 returncode 的写法会把这种挂起当通过
        # ——本文件自己就中过一次（桩里少个变量，整段没跑还是绿的）。所以要求
        # 测试体跑到最后一行打个记号，收不到记号就算红。
        self.assertIn(BODY_TAIL, script, "测试体没有用标准收尾，记号插不进去")
        script = script.replace(BODY_TAIL, "  console.log('__DONE__');\n" + BODY_TAIL)
        proc = subprocess.run(["node", "--input-type=module", "--eval", script],
                              text=True, capture_output=True)
        self.assertEqual(0, proc.returncode,
                         (proc.stderr or "") + (proc.stdout or ""))
        self.assertIn("__DONE__", proc.stdout or "",
                      "测试体没跑到最后就退出了（await 挂住了，node 不会报错）")


class SendInFlightTests(NodeCase):
    def test_ui_native_running_turn_uses_exact_target_and_delivery_id(self):
        self._node(UI_STUBS, self.ui_send, TURN, r"""
(async () => {
  session = { id: "s1", runtime_kind: "codex", native_desktop_connected: true,
              native_thread_id: "thread-1", connected: true, pending: null };
  liveTurns.s1 = { turn: { turn_id: "turn-1", live: true } };
  ta.value = "追加这一句";
  const pending = doSend(false);
  await turn();
  if (calls.length !== 1 || calls[0][0] !== "send_native_text") {
    throw new Error("原生连接仍走了旧发送路径：" + JSON.stringify(calls));
  }
  const call = calls[0];
  if (call[1] !== "s1" || call[2] !== "thread-1" || call[3] !== "turn-1"
      || call[4] !== "追加这一句") throw new Error("原生目标漂移：" + JSON.stringify(call));
  if (!/^[0-9a-f-]{36}$/i.test(call[5])) throw new Error("delivery id 非客户端 UUID：" + call[5]);
  if (call[6].length || call[7].length || call[8].length || call[9] !== null) {
    throw new Error("原生文字请求夹带旧 MCP 载荷：" + JSON.stringify(call));
  }
  waits.shift()({ ok: true, delivery: "native_steered" });
  await pending;
  if (ta.value !== "") throw new Error("原生 steer 成功后没清草稿");
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")

    def test_native_send_builds_secure_v4_uuid_without_random_uuid(self):
        body = r"""
Object.defineProperty(globalThis, "crypto", { configurable: true, value: {
  getRandomValues(bytes) { for (let i = 0; i < bytes.length; i++) bytes[i] = i; return bytes; }
}});
(async () => {
  session = { id: "s1", name: "张三", runtime_kind: "codex", native_desktop_connected: true,
              native_thread_id: "thread-1", connected: true, pending: null };
  liveTurns.s1 = { turn: { turn_id: "turn-1", live: true } };
  ta.value = "HTTP 页面也要能发";
  const pending = doSend(false);
  await turn();
  const call = calls[0];
  const delivery = Array.isArray(call) && call[0] === "send_native_text"
    ? call[5] : call && call[1] && call[1].delivery_id;
  if (delivery !== "00010203-0405-4607-8809-0a0b0c0d0e0f") {
    throw new Error("fallback UUID 不是合法 v4：" + delivery);
  }
  waits.shift()({ ok: true, delivery: "native_steered" });
  await pending;
})().catch(err => { console.error(err.stack || err); process.exit(1); });
"""
        self._node(UI_STUBS, self.ui_send, TURN, body)
        self._node(SHARE_STUBS, self.share_send, TURN, body)

    def test_share_native_ended_turn_success_clears_the_draft(self):
        self._node(SHARE_STUBS, self.share_send, TURN, r"""
(async () => {
  session = { id: "s1", name: "张三", runtime_kind: "codex", native_desktop_connected: true,
              native_thread_id: "thread-2", connected: true, pending: null };
  liveTurns.s1 = { turn: { turn_id: "turn-ended", live: false } };
  ta.value = "开始下一轮";
  const pending = doSend(false);
  await turn();
  const post = calls.find(c => c[0] === "/api/native_send");
  if (!post) throw new Error("手机页没有调用原生发送：" + JSON.stringify(calls));
  if (post[1].sid !== "s1" || post[1].thread_id !== "thread-2"
      || post[1].turn_id !== "turn-ended" || post[1].text !== "开始下一轮") {
    throw new Error("手机原生目标漂移：" + JSON.stringify(post));
  }
  if (!/^[0-9a-f-]{36}$/i.test(post[1].delivery_id) || post[2] !== 12000) {
    throw new Error("手机 delivery id/请求结构异常：" + JSON.stringify(post));
  }
  waits.shift()({ ok: true, delivery: "native_started" });
  await pending;
  if (ta.value !== "") throw new Error("start 成功后没清草稿");
  if (!calls.some(c => c[0] === "/api/draft" && c[1].text === "")) {
    throw new Error("start 成功后未同步清空草稿：" + JSON.stringify(calls));
  }
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")

    def test_native_attachments_are_rejected_without_any_send(self):
        body = r"""
(async () => {
  session = { id: "s1", runtime_kind: "codex", native_desktop_connected: true,
              native_thread_id: "thread-1", connected: true, pending: null };
  liveTurns.s1 = { turn: { turn_id: "turn-1", live: true } };
  ta.value = "带附件";
  images.push({ data: "image" });
  await doSend(false);
  if (calls.length) throw new Error("附件被发进某条路径：" + JSON.stringify(calls));
  if (ta.value !== "带附件" || images.length !== 1) throw new Error("拒绝时动了草稿或附件");
})().catch(err => { console.error(err.stack || err); process.exit(1); });
"""
        self._node(UI_STUBS, self.ui_send, TURN, body)
        self._node(SHARE_STUBS, self.share_send, TURN, body)

    def test_native_connected_without_synced_turn_never_falls_back_to_mcp(self):
        body = r"""
(async () => {
  session = { id: "s1", runtime_kind: "codex", native_desktop_connected: true,
              native_thread_id: "thread-1", connected: true, pending: null };
  ta.value = "轮次尚未同步";
  await doSend(false);
  if (calls.length) throw new Error("轮次未同步却派发了消息：" + JSON.stringify(calls));
  if (ta.value !== "轮次尚未同步") throw new Error("未派发却清了草稿");
})().catch(err => { console.error(err.stack || err); process.exit(1); });
"""
        self._node(UI_STUBS, self.ui_send, TURN, body)
        self._node(SHARE_STUBS, self.share_send, TURN, body)

    def test_delivery_unknown_keeps_text_and_never_falls_back(self):
        ui_body = r"""
(async () => {
  session = { id: "s1", runtime_kind: "codex", native_desktop_connected: true,
              native_thread_id: "thread-1", connected: true, pending: null };
  liveTurns.s1 = { turn: { turn_id: "turn-1", live: true } };
  ta.value = "回执未知不要重发";
  const pending = doSend(false);
  await turn();
  waits.shift()({ ok: false, delivery_unknown: true, error: "发送结果未知，请勿重发" });
  await pending;
  if (ta.value !== "回执未知不要重发") throw new Error("未知回执清了输入框");
  if (calls.length !== 1 || calls[0][0] !== "send_native_text") {
    throw new Error("未知回执触发了降级/重发：" + JSON.stringify(calls));
  }
})().catch(err => { console.error(err.stack || err); process.exit(1); });
"""
        share_body = r"""
(async () => {
  session = { id: "s1", name: "张三", runtime_kind: "codex", native_desktop_connected: true,
              native_thread_id: "thread-1", connected: true, pending: null };
  liveTurns.s1 = { turn: { turn_id: "turn-1", live: true } };
  ta.value = "手机回执未知不要重发";
  const pending = doSend(false);
  await turn();
  waits.shift()({ ok: false, delivery_unknown: true, error: "发送结果未知，请勿重发" });
  await pending;
  if (ta.value !== "手机回执未知不要重发") throw new Error("手机未知回执清了输入框");
  const sends = calls.filter(c => c[0] !== "/api/draft");
  if (sends.length !== 1 || sends[0][0] !== "/api/native_send") {
    throw new Error("手机未知回执触发了降级/重发：" + JSON.stringify(calls));
  }
})().catch(err => { console.error(err.stack || err); process.exit(1); });
"""
        self._node(UI_STUBS, self.ui_send, TURN, ui_body)
        self._node(SHARE_STUBS, self.share_send, TURN, share_body)

    def test_codex_desktop_disconnect_uses_the_existing_mcp_queue(self):
        self._node(UI_STUBS, self.ui_send, TURN, r"""
(async () => {
  session = { id: "s1", runtime_kind: "codex", native_desktop_connected: false,
              connected: true, pending: null };
  ta.value = "桌面断开后排队";
  const pending = doSend(false);
  await turn();
  if (calls.length !== 1 || calls[0][0] !== "queue_message") {
    throw new Error("桌面断开后的去向不明确：" + JSON.stringify(calls));
  }
  waits.shift()({ ok: true, qid: "fallback-q" });
  await pending;
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")

    def test_ui_normal_reply_requests_the_recall_window(self):
        self._node(UI_STUBS, self.ui_send, TURN, r"""
(async () => {
  ta.value = "桌面普通回复";
  const pending = doSend(false);
  await turn();
  const call = calls[0];
  if (call[0] !== "send_reply" || call[8] !== true) {
    throw new Error("桌面普通回复没请求撤回窗口：" + JSON.stringify(call));
  }
  waits.shift()({ ok: true, qid: "desktop-q" });
  await pending;
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")

    def test_ui_processing_queue_requests_the_recall_window_and_keeps_options(self):
        self._node(UI_STUBS, self.ui_send, TURN, r"""
(async () => {
  session.pending = null;
  selected.push("方案A");
  ta.value = "桌面提前排队";
  const pending = doSend(false);
  await turn();
  const call = calls[0];
  if (call[0] !== "queue_message" || call[7] !== true) {
    throw new Error("桌面排队没请求撤回窗口：" + JSON.stringify(call));
  }
  if (!Array.isArray(call[8]) || call[8][0] !== "方案A") {
    throw new Error("桌面排队丢了选项：" + JSON.stringify(call[8]));
  }
  waits.shift()({ ok: true, qid: "desktop-q" });
  await pending;
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")

    def test_share_direct_reply_keeps_qid_for_recall(self):
        self._node(SHARE_STUBS, self.share_send, TURN, r"""
(async () => {
  ta.value = "手机普通回复";
  const pending = doSend(false);
  await turn();
  const post = calls.find(c => c[0] === "/api/reply");
  if (!post || post[1].recallable !== true) {
    throw new Error("手机回复没请求撤回窗口：" + JSON.stringify(post));
  }
  waits.shift()({ ok: true, qid: "phone-reply-q" });
  await pending;
  if (!myQids.has("phone-reply-q")) throw new Error("手机直接回复的 qid 没留下");
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")

    def test_share_processing_queue_keeps_qid_for_recall(self):
        self._node(SHARE_STUBS, self.share_send, TURN, r"""
(async () => {
  session.pending = null;
  ta.value = "手机提前排队";
  const pending = doSend(false);
  await turn();
  const post = calls.find(c => c[0] === "/api/queue");
  if (!post || post[1].recallable !== true) {
    throw new Error("手机排队没请求撤回窗口：" + JSON.stringify(post));
  }
  waits.shift()({ ok: true, qid: "phone-queue-q" });
  await pending;
  if (!myQids.has("phone-queue-q")) throw new Error("手机排队的 qid 没留下");
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")

    # ---- 桌面页 ----------------------------------------------------------

    def test_ui_second_click_while_in_flight_is_swallowed(self):
        self._node(UI_STUBS, self.ui_send, TURN, r"""
(async () => {
  ta.value = "把这句发出去";
  const first = doSend(false);
  await turn();
  const second = doSend(false);   // 人以为没点上，又点了一下
  await turn();
  if (calls.length !== 1) throw new Error("同一条消息发了 " + calls.length + " 遍");
  waits.shift()({ ok: true });
  await Promise.all([first, second]);
  if (calls.length !== 1) throw new Error("第二次点击最终还是漏出去了");
  if (ta.value !== "") throw new Error("发完输入框没清：" + JSON.stringify(ta.value));
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")

    def test_ui_gate_reopens_after_the_flight_lands(self):
        self._node(UI_STUBS, self.ui_send, TURN, r"""
(async () => {
  ta.value = "第一句";
  const first = doSend(false);
  await turn();
  waits.shift()({ ok: true });
  await first;
  ta.value = "第二句";
  const second = doSend(false);
  await turn();
  if (calls.length !== 2) throw new Error("闸没放开，第二句发不出去");
  waits.shift()({ ok: true });
  await second;
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")

    def test_ui_gate_reopens_when_the_send_fails(self):
        # 失败也得放闸，否则一次网络抖动会把这个 tab 永远锁成发不出去
        self._node(UI_STUBS, self.ui_send, TURN, r"""
(async () => {
  ta.value = "会失败的一句";
  const first = doSend(false);
  await turn();
  waits.shift()({ ok: false, error: "boom" });
  await first;
  if (ta.value !== "会失败的一句") throw new Error("发失败了却把字清了");
  if (saveDraftCalls !== 1) throw new Error("发失败没补存草稿");
  const second = doSend(false);
  await turn();
  if (calls.length !== 2) throw new Error("发失败后这个 tab 被锁死了");
  waits.shift()({ ok: true });
  await second;
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")

    def test_ui_redraws_the_button_once_the_gate_reopens(self):
        # 闸关上时画过一次（按钮灰掉），放开后必须再画一次，否则按钮要一直灰到
        # 下一拍轮询才亮回来——人看到的就是「发失败了，还点不动」
        self._node(UI_STUBS, self.ui_send, TURN, r"""
(async () => {
  ta.value = "会失败的一句";
  const first = doSend(false);
  await turn();
  if (renders[0] !== true) throw new Error("发送时按钮没当场灰掉");
  waits.shift()({ ok: false, error: "boom" });
  await first;
  if (renders[renders.length - 1] !== false) {
    throw new Error("闸放开后没重画，按钮会一直灰着：" + JSON.stringify(renders));
  }
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")

    def test_ui_keeps_what_was_typed_during_the_round_trip(self):
        self._node(UI_STUBS, self.ui_send, TURN, r"""
(async () => {
  ta.value = "先发这句";
  const first = doSend(false);
  await turn();
  ta.value = "先发这句" + "接着打的下一句";   // 往返期间人没停手
  waits.shift()({ ok: true });
  await first;
  if (ta.value !== "接着打的下一句") {
    throw new Error("往返期间打的字被吞了：" + JSON.stringify(ta.value));
  }
  const last = drafted[drafted.length - 1];
  if (last[1] !== "接着打的下一句") {
    throw new Error("落库的草稿把新打的字冲掉了：" + JSON.stringify(last[1]));
  }
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")

    def test_ui_only_the_attachments_that_went_out_are_removed(self):
        self._node(UI_STUBS, self.ui_send, TURN, r"""
(async () => {
  const sent = { data: "d1", filename: "发出去的.png" };
  const pastedLater = { data: "d2", filename: "途中粘的.png" };
  images.push(sent);
  ta.value = "带图";
  const first = doSend(false);
  await turn();
  images.push(pastedLater);      // 往返期间又粘了一张
  waits.shift()({ ok: true });
  await first;
  if (images.length !== 1 || images[0] !== pastedLater) {
    throw new Error("途中粘的图被一起清掉了");
  }
  // calls 记的是 [名字, ...实参]，所以 send_reply(sid, text, selected, images, …)
  // 里的 images 落在下标 4
  const payload = calls[0][4];
  if (payload.length !== 1 || payload[0] !== sent) {
    throw new Error("发出去的那次带上了还没决定要发的图");
  }
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")

    # ---- 手机页 ----------------------------------------------------------

    def test_share_second_tap_while_in_flight_is_swallowed(self):
        self._node(SHARE_STUBS, self.share_send, TURN, r"""
(async () => {
  ta.value = "手机上发的一句";
  const first = doSend(false);
  await turn();
  const second = doSend(false);   // 公网慢，人又点了一下
  await turn();
  const posts = calls.filter(c => c[0] === "/api/reply");
  if (posts.length !== 1) throw new Error("手机端发了 " + posts.length + " 遍");
  waits.shift()({ ok: true });
  await Promise.all([first, second]);
  if (ta.value !== "") throw new Error("发完输入框没清");
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")

    def test_share_gate_reopens_and_keeps_new_typing(self):
        self._node(SHARE_STUBS, self.share_send, TURN, r"""
(async () => {
  ta.value = "第一句";
  const first = doSend(false);
  await turn();
  ta.value = "第一句" + "路上打的";
  waits.shift()({ ok: true });
  await first;
  if (ta.value !== "路上打的") {
    throw new Error("往返期间打的字被吞了：" + JSON.stringify(ta.value));
  }
  const draft = calls.filter(c => c[0] === "/api/draft").pop();
  if (draft[1].text !== "路上打的") {
    throw new Error("同步给 hub 的草稿把新打的字冲掉了");
  }
  const second = doSend(false);
  await turn();
  if (calls.filter(c => c[0] === "/api/reply").length !== 2) {
    throw new Error("闸没放开");
  }
  waits.shift()({ ok: true });
  await second;
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")

    def test_share_redraws_the_button_once_the_gate_reopens(self):
        self._node(SHARE_STUBS, self.share_send, TURN, r"""
(async () => {
  ta.value = "手机上会失败的一句";
  const first = doSend(false);
  await turn();
  if (renders[0] !== true) throw new Error("发送时按钮没当场灰掉");
  waits.shift()({ ok: false, error: "boom" });
  await first;
  if (renders[renders.length - 1] !== false) {
    throw new Error("闸放开后没重画，按钮会一直灰着：" + JSON.stringify(renders));
  }
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")

    # ---- 往返期间人切了会话 ------------------------------------------------
    #
    # 08-25 体检第二段查出。发一趟带附件的要几十秒，人早切到别的会话去了。
    # 两个页面回来都还照着「出发时那个会话」收拾**共用的**那个输入框，而框里
    # 此刻装的是新会话的草稿：
    #   · $("ta").value 被当成本会话的余量推回 hub → 新会话的字写进了旧会话的
    #     草稿。旧会话那条链接是发给同事的，等于把没写完的话递了出去；
    #   · 桌面页更狠，pushDraft 连 images/files 一起推，粘的图会串到别的 tab；
    #   · selected 被清空、renderInput 拿旧会话重画，眼前会话的选项当场乱掉。

    def test_share_switching_away_does_not_touch_the_other_session(self):
        self._node(SHARE_STUBS, self.share_send, TURN, r"""
(async () => {
  ta.value = "发给张三的";
  const first = doSend(false);
  await turn();
  switchTo({ id: "s2", name: "李四", connected: true, pending: { id: 9 } });
  ta.value = "李四这边刚打了一半";        // 切过去以后人接着打字
  waits.shift()({ ok: true });
  await first;
  if (ta.value !== "李四这边刚打了一半") {
    throw new Error("把李四框里的字动了：" + JSON.stringify(ta.value));
  }
  const drafts = calls.filter(c => c[0] === "/api/draft");
  if (drafts.some(c => c[1].sid === "s2")) {
    throw new Error("往李四的草稿上写了东西：" + JSON.stringify(drafts));
  }
  const mine = drafts.filter(c => c[1].sid === "s1");
  if (!mine.length) throw new Error("张三那边发完了却没清草稿");
  if (mine[mine.length - 1][1].text !== "") {
    throw new Error("张三的草稿被写成了李四的字：" + JSON.stringify(mine));
  }
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")

    def test_share_switching_away_keeps_what_was_typed_before_leaving(self):
        """切走前在原会话又打了半句：那半句仍是原会话的草稿，不能连坐清掉。"""
        self._node(SHARE_STUBS, self.share_send, TURN, r"""
(async () => {
  ta.value = "先发这句";
  const first = doSend(false);
  await turn();
  ta.value = "先发这句" + "还没发的下半句";
  // 切走那一刻页面会把整段补存给张三（selectSession 里那一枪），
  // 落地时要从这一段里减掉已经发出去的，剩下的还归张三
  postDraft("s1", ta.value);
  switchTo({ id: "s2", name: "李四", connected: true, pending: { id: 9 } });
  waits.shift()({ ok: true });
  await first;
  const mine = calls.filter(c => c[0] === "/api/draft" && c[1].sid === "s1");
  if (mine[mine.length - 1][1].text !== "还没发的下半句") {
    throw new Error("切走前打的半句被吞了：" + JSON.stringify(mine[mine.length - 1]));
  }
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")

    def test_ui_switching_tabs_does_not_push_the_other_tabs_draft(self):
        self._node(UI_STUBS, self.ui_send, TURN, r"""
(async () => {
  const mine = { data: "d1", filename: "张三的图.png" };
  images.push(mine);
  ta.value = "发给张三的";
  const first = doSend(false);
  await turn();
  // 切 tab：switchTab 先把当前框存进 drafts[s1]，再把 s2 的草稿灌进来
  drafts["s1"] = { text: "发给张三的", selected: [], images: [mine], files: [] };
  session = { id: "s2", connected: true, pending: { id: 9 } };
  ta.value = "李四这边的草稿";
  images = [{ data: "d2", filename: "李四的图.png" }];
  waits.shift()({ ok: true });
  await first;
  if (ta.value !== "李四这边的草稿") throw new Error("动了李四框里的字");
  if (images.length !== 1 || images[0].filename !== "李四的图.png") {
    throw new Error("李四的图被当成张三的清掉了");
  }
  const last = drafted[drafted.length - 1];
  if (last[0] !== "s1") throw new Error("最后一枪打给了 " + last[0]);
  if (last[1] !== "" || last[2].length || last[3].length) {
    throw new Error("把李四的草稿写进了张三：" + JSON.stringify(last));
  }
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")

    def test_ui_switching_tabs_keeps_the_unsent_remainder(self):
        self._node(UI_STUBS, self.ui_send, TURN, r"""
(async () => {
  ta.value = "先发这句";
  const first = doSend(false);
  await turn();
  drafts["s1"] = { text: "先发这句还没发的下半句", selected: [], images: [], files: [] };
  session = { id: "s2", connected: true, pending: { id: 9 } };
  ta.value = "李四这边的草稿";
  waits.shift()({ ok: true });
  await first;
  const last = drafted[drafted.length - 1];
  if (last[0] !== "s1" || last[1] !== "还没发的下半句") {
    throw new Error("张三没发完的半句丢了：" + JSON.stringify(last));
  }
  if (!drafts["s1"] || drafts["s1"].text !== "还没发的下半句") {
    throw new Error("本地草稿也丢了：" + JSON.stringify(drafts["s1"]));
  }
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")

    def test_share_gate_reopens_when_the_send_throws(self):
        # api() 直接抛（超时/断网）也必须放闸。boom 开关在桩里 —— doSend 那段
        # 源码里没有 api 的定义，往它身上做替换是空转，整段测试会一句不跑就绿
        self._node(SHARE_STUBS, self.share_send, TURN, r"""
(async () => {
  boom = true;
  ta.value = "断网时发的";
  const first = doSend(false);
  await turn();
  await first;
  if (ta.value !== "断网时发的") throw new Error("抛异常还把字清了");
  boom = false;
  const second = doSend(false);
  await turn();
  if (calls.filter(c => c[0] === "/api/reply").length !== 2) {
    throw new Error("一次超时就把这个会话锁死了");
  }
  waits.shift()({ ok: true });
  await second;
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")


class PhoneDraftDebounceTests(NodeCase):
    """手机页的草稿防抖：0.8 秒后那一枪必须打在「当时打字的那个会话」身上。

    08-25 体检第二段查出。原写法是点火时才去问 activeSession()：

        draftTimer = setTimeout(() => {
          const cur = activeSession();
          if (cur) api("/api/draft", { sid: cur.id, text: $("ta").value });
        }, 800);

    而 selectSession 换会话时会把输入框清空。于是在张三那儿打完字、0.8 秒内切到
    李四，这一枪打的是**李四**、内容是**空字符串**——李四存在 hub 里的草稿当场
    被清掉（手机上刷一下就再也回不来了），张三刚打的那半句也一个字没存下。
    切会话本来就是最常见的动作，0.8 秒又是打字停顿的常态间隔。
    """

    def _drafts(self):
        return DRAFT_STUBS, self.share_select, self.share_send

    def test_a_pending_save_lands_on_the_session_it_was_typed_in(self):
        self._node(*self._drafts(), TURN, r"""
(async () => {
  ta.value = "张三这边打了一半";
  armDraftSave();
  selectSession("s2");                       // 0.8 秒还没到就切走了
  await new Promise(r => setTimeout(r, 30));
  const drafts = calls.filter(c => c[0] === "/api/draft");
  if (drafts.some(c => c[1].sid === "s2")) {
    throw new Error("把李四的草稿冲了：" + JSON.stringify(drafts));
  }
  const mine = drafts.filter(c => c[1].sid === "s1");
  if (!mine.length || mine[mine.length - 1][1].text !== "张三这边打了一半") {
    throw new Error("张三打的那半句没存下：" + JSON.stringify(drafts));
  }
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")

    def test_the_timer_does_not_fire_again_after_the_flush(self):
        """切走时补的那一枪要把定时器一起拆掉，否则它照样会打在新会话身上。"""
        self._node(*self._drafts(), TURN, r"""
(async () => {
  ta.value = "张三这边打了一半";
  armDraftSave();
  selectSession("s2");
  ta.value = "";                              // 切过去后框是空的
  await new Promise(r => setTimeout(r, 900)); // 等过原来那 800ms
  const drafts = calls.filter(c => c[0] === "/api/draft");
  if (drafts.length !== 1 || drafts[0][1].sid !== "s1") {
    throw new Error("补枪之后定时器又开了一枪：" + JSON.stringify(drafts));
  }
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")

    def test_typing_and_pausing_still_saves_normally(self):
        """护栏：没切会话时，照旧是「停 0.8 秒存一次」。"""
        self._node(*self._drafts(), TURN, r"""
(async () => {
  ta.value = "慢慢打";
  armDraftSave();
  await new Promise(r => setTimeout(r, 900));
  const drafts = calls.filter(c => c[0] === "/api/draft");
  if (drafts.length !== 1 || drafts[0][1].sid !== "s1"
      || drafts[0][1].text !== "慢慢打") {
    throw new Error("正常存稿被改坏了：" + JSON.stringify(drafts));
  }
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")

    def test_keystrokes_within_the_window_only_save_once(self):
        """护栏：连着打字只在停下来之后存一次，不是每个键一趟公网。"""
        self._node(*self._drafts(), TURN, r"""
(async () => {
  for (const t of ["一", "一二", "一二三"]) { ta.value = t; armDraftSave(); }
  await new Promise(r => setTimeout(r, 900));
  const drafts = calls.filter(c => c[0] === "/api/draft");
  if (drafts.length !== 1 || drafts[0][1].text !== "一二三") {
    throw new Error("防抖坏了：" + JSON.stringify(drafts));
  }
})().catch(err => { console.error(err.stack || err); process.exit(1); });
""")


class CompactResult(unittest.TextTestResult):
    """Node 错误里可能包含很长的 JS/HTML；截断展示但不改变断言。"""

    def _exc_info_to_string(self, err, test):
        text = super()._exc_info_to_string(err, test)
        return text if len(text) <= 5000 else text[:5000] + "\n...[测试输出已截断]\n"


if __name__ == "__main__":
    unittest.main(testRunner=unittest.TextTestRunner(resultclass=CompactResult))
