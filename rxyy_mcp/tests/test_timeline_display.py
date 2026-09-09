# -*- coding: utf-8 -*-
"""控制台时间线展示对齐 Bajie（bd5e0430，09-05 派单①）。

三件套（ltMarkdown / findLiveTurnHost / 30s 久未响应）之上再补三样：
- 工具结果轻排版：等宽保留换行、超 LT_RESULT_FOLD 行折叠 + 展开、长结果顶部摘要
- Cursor 原生 AskQuestion 渲成只读选项卡（题目 / 选项 / 已选 / 补充；不作答）
- ltMarkdown 补 GFM 表格；XSS / 未闭合围栏不崩

ltMarkdown 一族是纯字符串函数，抠出来在 node 里跑真代码；painter 用最小假 DOM 跑。
"""
import subprocess
import sys
import unittest
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

UI = (MODULE_DIR / "ui.html").read_text(encoding="utf-8")


def _slice(start_marker, end_marker):
    start = UI.index(start_marker)
    end = UI.index(end_marker, start)
    return UI[start:end]


def _node(js):
    r = subprocess.run(["node", "-"], input=js.encode("utf-8"), capture_output=True, timeout=30)
    out = (r.stdout + r.stderr).decode("utf-8", "replace")
    return r.returncode, out


# 最小假 DOM：只实现 painter 用到的 createElement / append / textContent / className / classList
FAKE_DOM = r"""
class El {
  constructor(tag) { this.tag = tag; this.children = []; this._text = ""; this.className = ""; this.style = {}; this.dataset = {}; this.onclick = null; this.title = ""; }
  get classList() {
    const self = this;
    return {
      add: (...xs) => { const s = new Set(self.className.split(/\s+/).filter(Boolean)); xs.forEach(x => s.add(x)); self.className = [...s].join(" "); },
      contains: (x) => self.className.split(/\s+/).includes(x),
      remove: (...xs) => { const s = new Set(self.className.split(/\s+/).filter(Boolean)); xs.forEach(x => s.delete(x)); self.className = [...s].join(" "); },
    };
  }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(""); }
  append(...xs) { xs.forEach(x => this.children.push(x)); }
  appendChild(x) { this.children.push(x); return x; }
  find(cls) { const out = []; const walk = (n) => { if (n.classList.contains(cls)) out.push(n); n.children.forEach(walk); }; this.children.forEach(walk); return out; }
}
globalThis.document = { createElement: (t) => new El(t) };
globalThis.$ = () => ({ dataset: {} });
globalThis.activeSession = () => null;
globalThis.renderLiveTurn = () => {};
"""


class MarkdownTests(unittest.TestCase):
    JS = _slice("function ltEsc(", "function ltFillRich(")

    def test_markdown_escapes_html_and_survives_unclosed_fence(self):
        code, out = _node(self.JS + r"""
const xss = ltMarkdown('<img src=x onerror="alert(1)"> **b** <script>evil()</script>');
if (xss.includes("<img") || xss.includes("<script")) throw new Error("XSS leaked: " + xss);
if (!xss.includes("<strong>b</strong>")) throw new Error("bold lost: " + xss);
const link = ltMarkdown('[x](javascript:alert(1)) [ok](https://a.b/c)');
if (link.includes('href="javascript')) throw new Error("js link leaked: " + link);
if (!link.includes('href="https://a.b/c"')) throw new Error("https link lost: " + link);
const open = ltMarkdown('前文\n```py\nprint(1)\n还没闭合');
if (open.includes("<pre>")) throw new Error("unclosed fence must stay plain: " + open);
if (!open.includes("print(1)")) throw new Error("text lost: " + open);
const closed = ltMarkdown('```js\nlet a = "<b>";\n```\n后文');
if (!closed.includes('<pre><code class="language-js">')) throw new Error("fence lost: " + closed);
if (closed.includes("<b>")) throw new Error("html inside fence leaked: " + closed);
if (!closed.includes("&lt;b&gt;")) throw new Error("fence body must be escaped: " + closed);
console.log("ok");
""")
        self.assertEqual(0, code, out)
        self.assertIn("ok", out)

    def test_markdown_renders_gfm_table_and_no_extra_br_after_blocks(self):
        code, out = _node(self.JS + r"""
const src = '## 结果\n| 项 | 值 |\n|---|---|\n| a | **1** |\n| b | `2` |\n尾巴';
const html = ltMarkdown(src);
if (!html.includes("<table>")) throw new Error("no table: " + html);
if (!html.includes("<th>项</th><th>值</th>")) throw new Error("header cells: " + html);
if (!html.includes("<td>a</td><td><strong>1</strong></td>")) throw new Error("inline md inside cell: " + html);
if (!html.includes("<td>b</td><td><code>2</code></td>")) throw new Error("code inside cell: " + html);
if (html.includes("</h2><br>")) throw new Error("extra <br> after heading: " + html);
if (html.includes("</table><br>")) throw new Error("extra <br> after table: " + html);
if (!html.endsWith("尾巴")) throw new Error("tail lost: " + html);
const notTable = ltMarkdown('| 只有一行 |\n没有分隔行');
if (notTable.includes("<table>")) throw new Error("lonely pipe row must not be a table: " + notTable);
console.log("ok");
""")
        self.assertEqual(0, code, out)
        self.assertIn("ok", out)


class ThinkTextTests(unittest.TestCase):
    """09-07 真机：思考落库成 {"text":…,"isLastThinkingChunk":true} JSON 字符串，前端兜底解开（含流式未闭合）。"""
    def test_unwraps_json_thinking_in_both_pages(self):
        for name, src in (("ui", UI), ("share", (MODULE_DIR / "share.html").read_text(encoding="utf-8"))):
            i = src.index("function ltThinkText(")
            js = src[i:src.index("\n}\n", i) + 3] + r"""
const eq = (a, b, why) => { if (a !== b) throw new Error(why + ": " + JSON.stringify(a)); };
eq(ltThinkText('{"text":"x.ai/bot 直接被拒绝","isLastThinkingChunk":true}'), "x.ai/bot 直接被拒绝", "closed json");
eq(ltThinkText('{"text":"还在写\\n第二行'), "还在写\n第二行", "streaming unclosed json");
eq(ltThinkText("普通思考"), "普通思考", "plain");
eq(ltThinkText("{a: 1} 伪代码"), "{a: 1} 伪代码", "brace but not json");
eq(ltThinkText(""), "", "empty");
console.log("ok");
"""
            code, out = _node(js)
            self.assertEqual(0, code, name + ": " + out)
            self.assertIn("ok", out, name)
        for src in (UI, (MODULE_DIR / "share.html").read_text(encoding="utf-8")):
            self.assertIn("ltThinkText(st.text)", src)
            self.assertIn('r.kind === "thinking" ? ltThinkText(r.text) : r.text', src)


class PainterTests(unittest.TestCase):
    JS = FAKE_DOM + _slice("function ltEsc(", "function ltFillRich(") \
        + _slice("function ltUi(sid)", "function ltWantsFetch(") \
        + _slice("// 默认摊开的步", "// 工具结果：等宽") \
        + _slice("// 工具结果：等宽", "function renderLiveTurn(")

    def test_result_folds_beyond_threshold_with_summary_head(self):
        code, out = _node(self.JS + r"""
const u = { open: new Set() };
const st = { id: "t1", label: "终端", result: "退出码 0 · " + Array.from({length: 30}, (_, i) => "line" + i).join("\n") + "…" };
const d = document.createElement("div");
ltPaintResult(d, st, u);
if (!d.classList.contains("result")) throw new Error("class result");
const head = d.find("lt-res-head")[0];
if (!head) throw new Error("no summary head");
if (!head.textContent.includes("退出码 0")) throw new Error("exit code: " + head.textContent);
if (!head.textContent.includes("30 行")) throw new Error("line count: " + head.textContent);
if (!head.textContent.includes("hub 已截断")) throw new Error("clip mark: " + head.textContent);
const body = d.find("lt-res-body")[0];
if (body.textContent.split("\n").length !== LT_RESULT_FOLD) throw new Error("folded lines: " + body.textContent.split("\n").length);
const more = d.find("lt-res-more")[0];
if (!more.textContent.includes("展开全部 30 行")) throw new Error("more label: " + more.textContent);
more.onclick({ stopPropagation() {} });
if (!u.open.has("full:t1")) throw new Error("expand must record full:id");
const d2 = document.createElement("div");
ltPaintResult(d2, st, u);
if (d2.find("lt-res-body")[0].textContent.split("\n").length !== 30) throw new Error("not expanded");
if (!d2.find("lt-res-more")[0].textContent.includes("收起")) throw new Error("collapse label");
// 短结果：不折、不给摘要头
const d3 = document.createElement("div");
ltPaintResult(d3, { id: "t2", result: "全文 12 行" }, { open: new Set() });
if (d3.find("lt-res-head").length) throw new Error("short result must not get a head");
if (d3.find("lt-res-more").length) throw new Error("short result must not get a fold toggle");
console.log("ok");
""")
        self.assertEqual(0, code, out)
        self.assertIn("ok", out)

    def test_ask_card_is_readonly_with_state_options_and_picks(self):
        code, out = _node(self.JS + r"""
const ask = { title: "派单路线", state: "waiting", questions: [
  { id: "q1", prompt: "走哪条？", multiple: false, free_ok: true,
    options: [{ id: "a", label: "读库", picked: true }, { id: "b", label: "注入", picked: false }], answer: ["读库"], freeform: "" },
  { id: "q2", prompt: "要不要撤回？", multiple: true, free_ok: true,
    options: [{ id: "y", label: "要", picked: false }], answer: [], freeform: "顺手做" },
]};
const d = document.createElement("div");
ltPaintAsk(d, ask);
if (!d.classList.contains("ask")) throw new Error("class ask");
const st = d.find("lt-ask-state")[0];
if (!st.classList.contains("waiting")) throw new Error("state class: " + st.className);
if (!st.textContent.includes("等你点选")) throw new Error("state text: " + st.textContent);
const qs = d.find("lt-ask-q");
if (qs.length !== 2) throw new Error("questions: " + qs.length);
if (!qs[0].textContent.startsWith("① ")) throw new Error("numbering: " + qs[0].textContent);
const opts = d.find("lt-ask-opt");
if (opts.length !== 3) throw new Error("options: " + opts.length);
if (!opts[0].classList.contains("picked")) throw new Error("picked option must be marked");
if (opts[1].classList.contains("picked")) throw new Error("unpicked option marked");
if (!opts[2].classList.contains("multiple")) throw new Error("multi-select question renders checkbox style");
const free = d.find("lt-ask-free")[0];
if (!free || !free.textContent.includes("顺手做")) throw new Error("freeform text");
const foot = d.find("lt-ask-foot")[0];
if (!foot.textContent.includes("提交")) throw new Error("submit hint: " + foot.textContent);
if (!opts[0].onclick) throw new Error("waiting options should be clickable for the hook");
const d2 = document.createElement("div");
ltPaintAsk(d2, Object.assign({}, ask, { state: "answered" }));
if (!d2.find("lt-ask-state")[0].classList.contains("answered")) throw new Error("answered state");
if (!d2.find("lt-ask-foot")[0].textContent.includes("已在 Cursor 里答过")) throw new Error("answered foot");
console.log("ok");
""")
        self.assertEqual(0, code, out)
        self.assertIn("ok", out)


class StaleTests(unittest.TestCase):
    JS = _slice("const LT_STALE_SEC", "function ltBubblePlain(")

    def test_wait_for_user_tools_are_never_stale(self):
        code, out = _node(self.JS + r"""
const now = 10000;
const shell = { kind: "tool", status: "running", name: "run_terminal_command_v2", started: now - 31 };
if (!ltToolStale(shell, now)) throw new Error("31s running shell must be stale");
if (ltToolStale(Object.assign({}, shell, { started: now - 29 }), now)) throw new Error("29s is not stale");
for (const name of ["mcp-rxyy MCP-zhi", "mcp-BajieAsk-wait_message", "AskQuestion", "user-rxyy MCP.zhi"]) {
  const st = { kind: "tool", status: "running", name, started: now - 3600 };
  if (ltToolStale(st, now)) throw new Error(name + " waits for the user, must not be stale");
  if (!ltToolWaitsUser(st)) throw new Error(name + " must count as waiting for user");
}
if (!ltToolWaitsUser({ kind: "tool", status: "running", name: "whatever", blocking: true })) throw new Error("blocking flag");
if (ltToolWaitsUser({ kind: "tool", status: "running", name: "mcp-rxyy MCP-zt" })) throw new Error("zt is fire-and-forget, not a wait");
console.log("ok");
""")
        self.assertEqual(0, code, out)
        self.assertIn("ok", out)

    def test_timeline_children_do_not_flex_shrink(self):
        # 09-07 真机：步骤多时 .lt-detail（overflow:auto）被 flex 压成 13~17px 的小滚动条
        self.assertIn("#liveTurn .lt-body > * { flex-shrink: 0; }", UI)
        self.assertIn('"正在回复 · 等你回复"', UI)


class BadgeAndFreshnessTests(unittest.TestCase):
    """读库路做透三样：行上结果徽标 / 真实滞后 / 读库中断（Bajie 工具卡 + rtStale）。"""
    JS = _slice("function ltSpan(", "function paintLtReply(") \
        + _slice("// 工具行上的结果徽标", "// 工具结果：等宽")

    def test_badge_derived_from_result_and_diff(self):
        code, out = _node(self.JS + r"""
const eq = (a, b, why) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(why + ": " + JSON.stringify(a)); };
eq(ltResultBadge({ kind: "tool", status: "done", diff: { plus: 12, minus: 3, preview: "" }, result: "+12 / −3" }), { text: "+12 / −3", cls: "diff" }, "edit");
eq(ltResultBadge({ kind: "tool", status: "done", result: "退出码 0 · ok" }), { text: "退出码 0", cls: "ok" }, "shell ok");
eq(ltResultBadge({ kind: "tool", status: "error", result: "退出码 1 · boom" }), { text: "退出码 1", cls: "bad" }, "shell bad");
eq(ltResultBadge({ kind: "tool", status: "done", result: "7 处匹配 · 3 个文件" }), { text: "7 处匹配", cls: "" }, "search");
eq(ltResultBadge({ kind: "tool", status: "done", result: "全文 120 行" }), { text: "120 行", cls: "" }, "read");
eq(ltResultBadge({ kind: "tool", status: "done", result: "3/5 完成；进行中：x" }), { text: "3/5 完成", cls: "" }, "todo");
if (ltResultBadge({ kind: "tool", status: "running", result: "退出码 0" })) throw new Error("running has no badge yet");
if (ltResultBadge({ kind: "tool", status: "done", result: "⏳ 等你在 Cursor 对话里点选" })) throw new Error("ask text is not a badge");
if (ltResultBadge({ kind: "thinking", text: "退出码 0" })) throw new Error("only tools");
console.log("ok");
""")
        self.assertEqual(0, code, out)
        self.assertIn("ok", out)

    def test_consecutive_thinking_steps_coalesce_like_bajie(self):
        code, out = _node(self.JS + r"""
const steps = [
  { kind: "thinking", id: "a", text: "先" },
  { kind: "thinking", id: "b", text: "先对照再改" },
  { kind: "tool", id: "t", name: "Shell", status: "done" },
  { kind: "thinking", id: "c", text: "工具后又想" },
];
const got = coalesceTimelineSteps(steps);
if (got.length !== 3) throw new Error("len " + got.length);
if (got[0].text !== "先对照再改") throw new Error("merged text " + got[0].text);
if (got[0].id !== "b") throw new Error("keep latest id");
if (got[1].kind !== "tool") throw new Error("tool stays");
if (got[2].text !== "工具后又想") throw new Error("new think phase");
console.log("ok");
""")
        self.assertEqual(0, code, out)

    def test_native_tool_group_id_stays_stable_while_stream_grows(self):
        code, out = _node(self.JS + r"""
const a = { kind: "tool", id: "first", status: "done" };
const b = { kind: "tool", id: "second", status: "done" };
const c = { kind: "tool", id: "third", status: "running" };
const pair = coalesceTimelineSteps([a, b], true)[0];
const triple = coalesceTimelineSteps([a, b, c], true)[0];
if (pair.id !== "tools:first" || triple.id !== pair.id) {
  throw new Error("工具组新增子项后 id 漂移：" + pair.id + " -> " + triple.id);
}
console.log("ok");
""")
        self.assertEqual(0, code, out)

    def test_send_pins_to_user_and_blocks_live_follow(self):
        js = _slice("function armPinLastUser(", "function attachMsgTools(")
        code, out = _node(js + r"""
pinLastUser = true;
pinSawAway = false;
if (shouldFollowLiveTurn(true)) throw new Error("must not follow while pinned");
if (shouldFollowLiveTurn(false)) throw new Error("never follow when not at bottom");
msgsAfterPaint = "pin-user";
let pinned = false;
pinMsgsToLastUser = () => { pinned = true; };
const box = { scrollTop: 0, scrollHeight: 500, clientHeight: 400 };
applyMsgsScroll(box, true, true);
if (!pinned) throw new Error("pin-user must win over force-to-bottom");
if (box.scrollTop !== 0) throw new Error("must not jump to bottom when pinning");
if (msgsAfterPaint !== "") throw new Error("flag must clear");
applyMsgsScroll(box, true, true);
if (box.scrollTop !== 0) throw new Error("force must not break pin: " + box.scrollTop);
if (shouldFollowLiveTurn(true)) throw new Error("still pinned after force paint");
updatePinByNear(true);
if (!pinLastUser) throw new Error("still-at-bottom after send must stay pinned");
updatePinByNear(false);
if (!pinSawAway || !pinLastUser) throw new Error("leaving bottom notes away, keeps pin");
updatePinByNear(true);
if (pinLastUser) throw new Error("user return to bottom releases pin");
if (!shouldFollowLiveTurn(true)) throw new Error("follow after user unpins");
box.scrollTop = 0;
applyMsgsScroll(box, true, false);
if (box.scrollTop !== 500) throw new Error("after unpin, near-bottom still follows: " + box.scrollTop);
console.log("ok");
""")
        self.assertEqual(0, code, out)

    def test_live_turn_hangs_on_ai_after_last_user(self):
        js = "function ltNorm(s){return String(s||'').replace(/\\s+/g,' ').trim();}\n" \
             + _slice("function ltBubblePlain(", "function placeLiveTurnInMsgs(")
        code, out = _node("globalThis.Node={DOCUMENT_POSITION_FOLLOWING:4};\n" + js + r"""
function msg(role, text, extra) {
  const cls = "msg " + role + (extra ? " " + extra : "");
  const el = {
    cls, after: [],
    classList: { contains: (c) => cls.split(/\s+/).includes(c) },
    querySelector: () => ({ childNodes: [{ innerText: text || "" }] }),
    compareDocumentPosition(other) { return this.after.includes(other) ? 4 : 0; },
  };
  return el;
}
const oldAi = msg("ai", "上一轮 zhi");
const user = msg("user", "新回复");
const newAi = msg("ai", "本轮回答");
oldAi.after = [user, newAi];
user.after = [newAi];
const msgs = {
  querySelectorAll: (sel) => sel.includes(".ai") ? [oldAi, newAi] : [user],
};
const host = findLiveTurnHost(msgs, { reply: { kind: "text", text: "对不上" } });
if (host !== newAi) throw new Error("must hang on AI after last user, got " + (host && host.cls));
const onlyOld = {
  querySelectorAll: (sel) => sel.includes(".ai") ? [oldAi] : [user],
};
if (findLiveTurnHost(onlyOld, { reply: { kind: "text", text: "上一轮 zhi" } }) !== null)
  throw new Error("must not hang on previous zhi while user is last");
console.log("ok");
""")
        self.assertEqual(0, code, out)

    def test_freshness_reports_real_lag_and_read_outage(self):
        code, out = _node(self.JS + r"""
const now = 2000;
if (ltFreshness({}, { live: true, updated_at: now - 4 }, now) !== "上一步 4s 前") throw new Error("lag: " + ltFreshness({}, { live: true, updated_at: now - 4 }, now));
if (ltFreshness({}, { live: false, updated_at: now - 4 }, now) !== "") throw new Error("ended turn says nothing");
if (ltFreshness({}, { via: "wbhook", hook_age: 0.3, live: true }, now) !== "桥 实时") throw new Error("hook live: " + ltFreshness({}, { via: "wbhook", hook_age: 0.3, live: true }, now));
if (ltFreshness({}, { via: "wbhook", hook_age: 1.4, live: true }, now) !== "桥 1.4s 前") throw new Error("hook age: " + ltFreshness({}, { via: "wbhook", hook_age: 1.4, live: true }, now));
const s = ltFreshness({ failSince: Date.now() - 65000 }, { live: true, updated_at: now }, now);
if (!/^读库中断 1m05s，等待恢复…$/.test(s)) throw new Error("outage: " + s);
console.log("ok");
""")
        self.assertEqual(0, code, out)
        self.assertIn("ok", out)

    def test_tick_keeps_live_turn_on_read_failure_and_ui_marks_it(self):
        tick = _slice("async function liveTurnTick()", "function toggleLiveTurn()")
        self.assertIn("failSince: prev.failSince || Date.now()", tick, "读库失败时保留正在看的 live 时间线并计时")
        self.assertIn('"正在回复 · 读库中断"', UI)
        self.assertIn('classList.toggle("broken", readBroken)', UI)
        self.assertIn("ltFreshness(cur, turn, nowSec)", UI)
        self.assertNotIn('"读库 ≈8s 延迟"', UI, "写死的 8s 是 immutable 时代的数")
        self.assertIn('b.className = "lt-badge " + bit.cls', UI, "结果摘要行的徽标")


class ChoiceOnlyMessageTests(unittest.TestCase):
    """只点选项没打字：选项就是正文（Bajie 同款），不再「上面一排芯片 + 下面 (仅选项/图片) 空泡」。"""
    def test_both_pages_render_choices_as_body(self):
        for name, src in (("ui", UI), ("share", (MODULE_DIR / "share.html").read_text(encoding="utf-8"))):
            self.assertNotIn("(仅选项/图片)", src, name)
            self.assertIn('bubble.classList.add("choice-only")', src, name)
            self.assertIn('line.className = "choice-line"', src, name)
            self.assertIn('bubble.classList.add("empty-hidden")', src, name)
            self.assertIn("&& !choiceOnly) {", src, name + "：有正文才保留芯片行")
            self.assertIn(".bubble.empty-hidden { display: none; }", src, name)


class FooterMoreTests(unittest.TestCase):
    """底栏 13 个开关平铺太挤（rxyy 09-07 拍）：低频的收进「更多」弹层，id 不变、原绑定照用。"""
    def test_low_frequency_switches_live_in_more_popover(self):
        pop_start = UI.index('<div id="footMorePop"')
        pop_end = UI.index("</div>", pop_start)
        pop = UI[pop_start:pop_end]
        for anchor in ('id="swShare"', 'id="swFreeze"', 'id="swBoot"', 'id="btnReloadMcp"', 'id="btnRestart"'):
            self.assertIn(anchor, pop, anchor + " 应在「更多」弹层里")
        foot_start = UI.index('<div id="footer">')
        foot = UI[foot_start:pop_start]
        for anchor in ('id="histList"', 'id="shareLink"', 'id="swTop"', 'id="swAudio"', 'id="swQuiet"', 'id="btnSettings"', 'id="footMore"'):
            self.assertIn(anchor, foot, anchor + " 应留在底栏一眼可见")
        self.assertIn('footMore.onclick = (e) => { e.stopPropagation(); setFootPop(!footPopOpen()); };', UI)
        self.assertIn('if (e.key === "Escape" && footPopOpen()) setFootPop(false);', UI)
        self.assertIn('footMore.classList.toggle("hot", !!footPop.querySelector(".switch.on"))', UI, "里面有开关亮着要在「更多」上打点")
        self.assertIn("点右下角「更多」里的「❄冻结额度」关闭即恢复", UI, "冻结横幅的指路文案要跟着搬")
        # 原有绑定一个都不能丢
        for h in ('$("swShare").onclick', '$("swFreeze").onclick', '$("swBoot").onclick', '$("btnReloadMcp").onclick', '$("btnRestart").onclick'):
            self.assertIn(h, UI, h)


class DiffBarTests(unittest.TestCase):
    """改文件卡：复制 diff / 复制路径（Bajie DiffView 同款动作），两页都有。"""
    def test_diff_bar_in_both_pages(self):
        share = (MODULE_DIR / "share.html").read_text(encoding="utf-8")
        for name, src in (("ui", UI), ("share", share)):
            self.assertIn("function ltDiffBar(st)", src, name)
            self.assertIn("ltDiffBar(st)", src, name)
            self.assertIn('const path = st.path || st.summary || "";', src, name + "：老 hub 没 path 就退文件名")
            self.assertIn('mk("复制 diff", st.diff.preview,', src, name)
            self.assertIn('mk("复制路径", path,', src, name)
            self.assertIn(".lt-diff-bar", src, name)

    def test_ui_diff_bar_builds_with_fake_dom(self):
        js = FAKE_DOM + "globalThis.copyText = async (t) => { globalThis.__copied = t; return true; }; globalThis.toast = () => {};\n" \
            + _slice("function ltDiffBar(st)", "// Cursor 原生提问") + r"""
(async () => {
  const st = { kind: "tool", tool: "edit", summary: "c.py", path: "D:\\a\\b\\c.py", diff: { plus: 1, minus: 0, preview: "@@ -1 +1 @@\n+x" } };
  const bar = ltDiffBar(st);
  if (!bar.classList.contains("lt-diff-bar")) throw new Error("class");
  const btns = bar.find("lt-diff-btn");
  if (btns.length !== 2) throw new Error("two buttons, got " + btns.length);
  if (bar.find("lt-diff-path")[0].textContent !== "D:\\a\\b\\c.py") throw new Error("path label");
  await btns[0].onclick({ stopPropagation() {} });
  if (globalThis.__copied !== st.diff.preview) throw new Error("copy diff: " + globalThis.__copied);
  await btns[1].onclick({ stopPropagation() {} });
  if (globalThis.__copied !== "D:\\a\\b\\c.py") throw new Error("copy path: " + globalThis.__copied);
  const old = ltDiffBar({ kind: "tool", summary: "c.py", diff: { preview: "+y" } });
  if (old.find("lt-diff-path")[0].textContent !== "c.py") throw new Error("fallback to summary");
  console.log("ok");
})().catch(e => { console.error(e); process.exit(1); });
"""
        code, out = _node(js)
        self.assertEqual(0, code, out)
        self.assertIn("ok", out)


class StepFilterTests(unittest.TestCase):
    """长 turn 步骤过滤：全部 / 工具 / 思考 / 报错 芯片，两页同款。"""
    def test_filter_helpers_in_node(self):
        code, out = _node(_slice("// 步骤过滤：全部", "function paintLtFilter(") + r"""
const steps = [
  { kind: "tool", status: "done" }, { kind: "thinking" }, { kind: "tool", status: "error" },
  { kind: "error" }, { kind: "text" }, { kind: "thinking" }, { kind: "tool", status: "running" },
];
const c = ltFilterCounts(steps);
if (c.all !== 7 || c.tool !== 3 || c.thinking !== 2 || c.error !== 2) throw new Error(JSON.stringify(c));
if (steps.filter(s => ltStepMatches(s, "error")).length !== 2) throw new Error("error filter must include failed tools");
if (steps.filter(s => ltStepMatches(s, "tool")).length !== 3) throw new Error("tool filter");
if (steps.filter(s => ltStepMatches(s, "all")).length !== 7 || steps.filter(s => ltStepMatches(s, "")).length !== 7) throw new Error("all");
if (LT_FILTER_MIN !== 6) throw new Error("min");
console.log("ok");
""")
        self.assertEqual(0, code, out)
        self.assertIn("ok", out)

    def test_wired_in_both_pages(self):
        share = (MODULE_DIR / "share.html").read_text(encoding="utf-8")
        for name, src in (("ui", UI), ("share", share)):
            self.assertIn('<div class="lt-filter" id="ltFilter" style="display:none"></div>', src, name)
            self.assertIn("paintLtFilter(s, Object.assign({}, turn, { steps: steps }), u, !collapsed);", src, name)
            self.assertIn("paintLtFilter(s, null, null, false);", src, name + "：没步骤时要藏掉芯片")
            self.assertIn("if (!ltStepMatches(st, u.filter)) return;", src, name)
            self.assertIn('":f:" + u.filter', src, name + "：过滤进 paintSig，切换才重画")
            self.assertIn('这一段里没有「', src, name)
            self.assertIn(".lt-fchip.on", src, name)


class SysRowAndNickTests(unittest.TestCase):
    def test_sys_rows_are_hairline_dividers_in_both_pages(self):
        share = (MODULE_DIR / "share.html").read_text(encoding="utf-8")
        for name, src in (("ui", UI), ("share", share)):
            self.assertIn(".msg.sys .bubble::before, .msg.sys .bubble::after { content: \"\"; flex: 1;", src, name)
            self.assertIn(".msg.sys + .msg.sys { margin-top: -12px; }", src, name + "：连着几条系统行要收紧")

    def test_share_asks_nickname_only_when_sending(self):
        share = (MODULE_DIR / "share.html").read_text(encoding="utf-8")
        self.assertNotIn("if (!nick) askNick(true);", share, "首访不再进页就弹昵称框")
        self.assertIn("if (!nick) { askNick(true, () => doSend(isContinue)); return; }", share)
        self.assertIn("function askNick(firstTime, then)", share)
        self.assertIn('if (nick && typeof then === "function") then();', share, "填完昵称要把刚才那条接着发出去")
        self.assertIn("function paintNickBtn()", share)
        self.assertIn('"👤 起个名字"', share)
        self.assertIn("#nickBtn.nick-missing", share)


class WiringTests(unittest.TestCase):
    def test_render_live_turn_uses_painters_and_auto_opens_blocking_ask(self):
        body = _slice("function renderLiveTurn(", "async function refreshActive(")
        self.assertIn("ltPaintAsk(d, st.ask)", body)
        self.assertIn("ltPaintResult(d, st, u)", body)
        self.assertNotIn("d.textContent = st.result;", body, "工具结果不再裸 textContent")
        self.assertIn("const autoDetail = !native && (isLast || !!(st.ask && st.blocking)", body,
                      "Cursor 提问保持默认摊开，Codex 工具默认折叠")
        self.assertIn("ltSetStepOpen(sid, st.id, !detailOpen)", body, "默认摊开的步也能手动收起")

    def test_css_anchors_present(self):
        for anchor in (".lt-detail.result", ".lt-res-head", ".lt-res-more",
                       ".lt-detail.ask", ".lt-ask-state.waiting", ".lt-ask-opt.picked",
                       ".lt-md table", "LT_RESULT_FOLD = 14"):
            self.assertIn(anchor, UI, anchor)

    def test_script_parses(self):
        start = UI.index("<script>")
        end = UI.rindex("</script>")
        js = UI[start + 8:end]
        r = subprocess.run(["node", "--check"], input=js.encode("utf-8"),
                           capture_output=True, timeout=20)
        self.assertEqual(0, r.returncode, r.stderr.decode("utf-8", "replace"))

    def test_native_text_uses_one_delivery_path_in_both_pages(self):
        share = (MODULE_DIR / "share.html").read_text(encoding="utf-8")
        for name, src in (("ui", UI), ("share", share)):
            for anchor in (
                    "const nativeMcpWait = nativeMode && s.connected && s.pending && !s.pending_deferred;",
                    "const nativeDirect = nativeMode && !nativeMcpWait;",
                    "if (nativeDirect)",
                    "发送消息即可继续当前 Codex 任务",
                    "已发送，Codex 将继续当前任务"):
                self.assertIn(anchor, src, name + ":" + anchor)
        self.assertIn("r = await api().send_native_text(", UI)
        self.assertIn("r = await api().send_reply(", UI)
        self.assertIn('r = await api("/api/native_send"', share)
        self.assertIn('r = await api("/api/reply"', share)


class CardFlowTests(unittest.TestCase):
    """09-07 rxyy 拍板「A 直接换卡片流」：每步一张卡（工具卡三行 / 思考卡正体全文 / 改文件卡默认摊开带行号 diff）。"""

    JS = FAKE_DOM + r"""
El.prototype.insertBefore = function (x, ref) { const i = ref ? this.children.indexOf(ref) : -1; if (i < 0) this.children.push(x); else this.children.splice(i, 0, x); return x; };
globalThis.$ = (id) => ({ dataset: {} });
globalThis.copyText = async () => true;
globalThis.toast = () => {};
globalThis.enhanceBubble = () => {};
const liveTurnUi = {};
const LT_STALE_SEC = 30;
""" + _slice("function ltEsc(", "function ltFillRich(") \
        + _slice("function ltFillRich(el, text)", "// 这些工具「跑着」就是在等人") \
        + _slice("const LT_WAIT_TOOL_RE", "function ltBubblePlain(") \
        + _slice("function ltUi(sid)", "const LT_FILTER_MIN") \
        + _slice("// 默认摊开的步", "// 工具结果：等宽") \
        + _slice("// 工具结果：等宽", "// Cursor 原生提问") \
        + _slice("const LT_ASK_STATE", "function paintLtReply(") \
        + _slice("/* ---------- 卡片流的积木 ---------- */", "async function refreshActive(")

    def _run(self, js):
        return _node(self.JS + js)

    def test_result_line_summarises_like_bajie(self):
        code, out = self._run(r"""
const L = (st) => (ltResultLine(st) || []).map(b => b.text).join(" | ");
console.log("shell=" + L({ kind: "tool", status: "done", result: "退出码 0 · a\nb\nc" }));
console.log("shellbad=" + L({ kind: "tool", status: "done", result: "退出码 1" }));
console.log("edit=" + L({ kind: "tool", status: "done", diff: { plus: 12, minus: 3 } }));
console.log("grep=" + L({ kind: "tool", status: "done", result: "7 处匹配\nx" }));
console.log("read=" + L({ kind: "tool", status: "done", result: "全文 384 行" }));
console.log("running=" + L({ kind: "tool", status: "running", result: "x" }));
console.log("err=" + L({ kind: "tool", status: "error", result: "boom\nmore" }));
console.log("mcp=" + L({ kind: "tool", status: "done", result: "ok" }));
""")
        self.assertEqual(0, code, out)
        self.assertIn("shell=退出码 0 | 3 行输出", out)
        self.assertIn("shellbad=退出码 1 | 无输出", out)
        self.assertIn("edit=+12 / −3", out)
        self.assertIn("grep=7 处匹配", out)
        self.assertIn("read=384 行", out)
        self.assertIn("running=\n", out, "跑着的不给结果行")
        self.assertIn("err=失败 | boom", out)
        self.assertIn("mcp=ok", out)

    def test_diff_view_numbers_both_sides_from_hunk_header(self):
        code, out = self._run(r"""
const u = { open: new Set() };
const st = { id: "e1", kind: "tool", tool: "edit", status: "done", summary: "a.py", path: "D:\\x\\a.py",
             diff: { plus: 2, minus: 1, preview: "@@ -10,3 +10,4 @@\n ctx\n-old\n+new1\n+new2\n ctx2\n… 还有 5 行改动" } };
const v = ltDiffView(st, u);
const rows = v.find("dl").map(r => r.className.replace("dl ", "") + ":" + r.children.map(c => c.textContent).join("|"));
console.log(rows.join("\n"));
console.log("btns=" + v.find("lt-diff-btn").map(b => b.textContent).join(","));
console.log("badges=" + v.find("lt-badge").map(b => b.textContent).join(","));
""")
        self.assertEqual(0, code, out)
        self.assertIn("dl-hunk:||@@ -10,3 +10,4 @@", out)
        self.assertIn("dl-ctx:10|10| ctx", out)
        self.assertIn("dl-del:11||-old", out, "删行只有旧行号")
        self.assertIn("dl-add:|11|+new1", out, "增行只有新行号")
        self.assertIn("dl-add:|12|+new2", out)
        self.assertIn("dl-ctx:12|13| ctx2", out)
        self.assertIn("dl-note:||… 还有 5 行改动", out)
        self.assertIn("btns=复制 diff,复制路径,统一,并排,全屏", out)
        self.assertIn("badges=+2,−1", out)

    def test_split_view_pairs_del_with_add(self):
        code, out = self._run(r"""
const u = { open: new Set(["split:e1"]) };
const st = { id: "e1", kind: "tool", tool: "edit", status: "done", summary: "a.py", path: "D:\\x\\a.py",
             diff: { plus: 2, minus: 1, preview: "@@ -10,3 +10,4 @@\n ctx\n-old\n+new1\n+new2\n ctx2" } };
const v = ltDiffView(st, u);
if (!/\bsplit\b/.test(v.className)) throw new Error("wrap not split: " + v.className);
const rows = v.find("lt-split-row").map(r => r.children.map(c => c.className + ":" + c.children.map(x => x.textContent).join("|")).join(" || "));
console.log(rows.join("\n"));
console.log("btns=" + v.find("lt-diff-btn").map(b => b.textContent + (b.className.includes("on") ? "*" : "")).join(","));
""")
        self.assertEqual(0, code, out)
        self.assertIn("col left dl-hunk:|@@ -10,3 +10,4 @@ || col right dl-hunk:|@@ -10,3 +10,4 @@", out)
        self.assertIn("col left dl-ctx:10| ctx || col right dl-ctx:10| ctx", out)
        self.assertIn("col left dl-del:11|-old || col right dl-add:11|+new1", out, "删行和第一增行配对")
        self.assertIn("col left empty:| || col right dl-add:12|+new2", out, "多出来的增行右侧单独占")
        self.assertIn("col left dl-ctx:12| ctx2 || col right dl-ctx:13| ctx2", out)
        self.assertIn("btns=复制 diff,复制路径,统一,并排*,全屏", out)

    def test_tool_card_has_head_why_result_and_stale_note(self):
        code, out = self._run(r"""
const u = ltUi("s1");
const now = 1000;
const st = { id: "t1", kind: "tool", tool: "shell", label: "运行命令", name: "Shell", summary: "ls -la", at: 900, started: 900,
             status: "running", why: "看看目录里有什么", result: "" };
const c1 = ltCardTool(st, { sid: "s1", u, turn: { live: true }, nowSec: now, isLast: true });
console.log("cls=" + c1.className);
console.log("why=" + c1.find("lt-why").map(x => x.textContent).join(""));
console.log("stale=" + c1.find("lt-stale-note").length);
const done = Object.assign({}, st, { status: "done", result: "退出码 0 · a\nb" });
const c2 = ltCardTool(done, { sid: "s1", u, turn: { live: false }, nowSec: now, isLast: false });
console.log("res=" + c2.find("lt-badge").map(x => x.textContent).join("|"));
console.log("detail-closed=" + c2.find("lt-detail").length);
u.open.add("t1");
const c3 = ltCardTool(done, { sid: "s1", u, turn: { live: false }, nowSec: now, isLast: false });
console.log("detail-open=" + c3.find("lt-detail").length + " chev=" + c3.find("lt-chev")[0].textContent);
// 改文件默认摊开 diff
const ed = { id: "e1", kind: "tool", tool: "edit", label: "改文件", status: "done", summary: "a.py", at: 1, diff: { plus: 1, minus: 0, preview: "@@ -1 +1 @@\n+x" } };
const c4 = ltCardTool(ed, { sid: "s1", u, turn: { live: false }, nowSec: now, isLast: false });
console.log("edit-open=" + c4.find("lt-diffv").length);
""")
        self.assertEqual(0, code, out)
        self.assertIn("cls=lt-card tool running stale", out, "跑了 100s 的命令标 stale")
        self.assertIn("why=看看目录里有什么", out, "模型给的说明单列一行")
        self.assertIn("stale=1", out, "「30 秒无进展」落在具体那张卡上")
        self.assertIn("res=退出码 0|2 行输出", out)
        self.assertIn("detail-closed=0", out)
        self.assertIn("detail-open=1 chev=▾", out)
        self.assertIn("edit-open=1", out, "改文件卡默认摊开 diff")

    def test_thinking_card_is_open_by_default_and_collapsible(self):
        code, out = self._run(r"""
const u = ltUi("s2");
const st = { id: "k1", kind: "thinking", text: "第一行\n第二行", ms: 1500, at: 5 };
const c1 = ltCardThinking(st, { sid: "s2", u, turn: { live: false }, isLast: false });
console.log("open=" + c1.find("lt-think-body").length + " cls=" + c1.className + " sum=" + JSON.stringify(c1.find("lt-sum")[0].textContent));
u.open.add("!k1");
const c2 = ltCardThinking(st, { sid: "s2", u, turn: { live: false }, isLast: false });
console.log("closed=" + c2.find("lt-think-body").length + " sum=" + JSON.stringify(c2.find("lt-sum")[0].textContent));
const c3 = ltCardThinking({ id: "k2", kind: "thinking", text: "", at: 6 }, { sid: "s2", u, turn: { live: true }, isLast: true });
console.log("live=" + c3.className + " lbl=" + c3.find("lt-lbl")[0].textContent);
""")
        self.assertEqual(0, code, out)
        self.assertIn('open=1 cls=lt-card thinking sum=""', out, "摊开时头部不重复第一行")
        self.assertIn('closed=0 sum="第一行"', out, "收起后头部给第一行当摘要")
        self.assertIn("live=lt-card thinking running think-live lbl=思考中", out)

    def test_native_cards_keep_chat_prominent_and_details_closed(self):
        code, out = self._run(r'''
const u = { open: new Set() };
const thought = ltCardThinking({id:"r1",kind:"thinking",text:"summary only",at:0},
  {sid:"s",u,turn:{live:false},isLast:true,native:true});
if (thought.find("lt-think-body").length) throw new Error("native reasoning opened");
const tool = ltCardTool({id:"t1",kind:"tool",tool:"shell",label:"运行命令",summary:"pytest",result:"large json"},
  {sid:"s",u,turn:{live:false},nowSec:0,isLast:true,native:true});
if (tool.find("lt-detail").length) throw new Error("native tool opened");
const text = ltCardText({id:"m1",kind:"text",role:"user",text:"继续"},
  {sid:"s",u,isLast:false,native:true});
if (!String(text.className).includes("lt-native-message user")) throw new Error("native user style");
if (text.find("lt-card-head").length) throw new Error("native body has card heading");
const group = ltCardToolGroup({id:"g",kind:"tool_group",status:"done",tools:[
  {id:"a",kind:"tool",label:"工具",name:"read"},{id:"b",kind:"tool",label:"工具",name:"search"}
]}, {sid:"s",u,turn:{live:false},nowSec:0,native:true});
if (group.find("lt-tool-group-body").length) throw new Error("native tool group opened");
console.log("ok");
''')
        self.assertEqual(0, code, out)
        self.assertIn("ok", out)

    def test_css_and_placement_anchors_in_both_pages(self):
        share = (MODULE_DIR / "share.html").read_text(encoding="utf-8")
        for name, src in (("ui", UI), ("share", share)):
            for anchor in ("#liveTurn .lt-body::before", ".lt-card::before", ".lt-card.thinking::before",
                           ".lt-diffv .dl .no", ".lt-why", ".lt-res-line", ".lt-stale-note",
                           ".lt-diff-split", ".lt-diffv.fs", "function ltParseDiffRows", "function ltPairSplitRows",
                           "insertBefore(box, match)",
                           'querySelectorAll(".msg.user")',
                           "#liveTurn.in-stream",
                           "#liveTurn.in-stream .lt-toggle { display: none; }",
                           "function coalesceTimelineSteps",
                           "const hideThink = !!(inStream && r && r.kind === \"thinking\");",
                           "const streaming = !!(turn.live && r.kind === \"text\");",
                           "#ltReply.stream-live .lt-reply-body::after",
                           "不贴上一轮 zhi",
                           ".lt-card.edit .lt-ico",
                           ".lt-st.done { color: #fff; background: #22c55e; }",
                           "#nfx",
                           "@keyframes nfxfloat",
                           "body.nfx-live #nfx .orb",
                           ".lt-think-body {",
                           "border-left: 2px solid",
                           "function ltCardTool", "function ltCardThinking", "function ltDiffView", "function ltResultLine",
                           "function shouldFollowLiveTurn",
                           "function armPinLastUser",
                           "function updatePinByNear",
                           "function releasePinLastUser",
                           "if (msgsEl && (turn.live || runtimeKindOf(s) === \"codex\") && shouldFollowLiveTurn(msgsNearBottom)) msgsEl.scrollTop = msgsEl.scrollHeight;"):
                self.assertIn(anchor, src, name + "：" + anchor)
            # 时间线盒子不再限高自滚（随整页滚）
            i = src.index("#liveTurn .lt-body {")
            rule = src[i:src.index("}", i)]
            self.assertIn("max-height: none", rule, name)
            self.assertNotIn(".msg.lt-host #liveTurn .lt-body { max-height", src, name)
            self.assertNotIn("#liveTurn.in-stream #ltReply { display: none !important; }", src, name)
            self.assertNotIn("if (turn && turn.live) return ais[ais.length - 1];", src, name)


class SharePageParityTests(unittest.TestCase):
    """手机分享页 share.html 同一套：挂对应气泡 / 结果排版 / 提问只读卡 / 表格 / 久未响应 / 不被 flex 压扁。"""
    SHARE = (MODULE_DIR / "share.html").read_text(encoding="utf-8")

    def test_share_carries_the_same_timeline_pieces(self):
        for anchor in ("function findLiveTurnHost", "lt-host-only", "function ltTables",
                       "function ltPaintResult", "function ltPaintAsk", "function ltSetStepOpen",
                       "function ltToolStale", "function ltToolWaitsUser", "LT_STALE_SEC = 30",
                       "LT_RESULT_FOLD = 14", "久未响应", "#liveTurn .lt-body > * { flex-shrink: 0; }",
                       ".lt-detail.ask", ".lt-md table", "ltPaintAsk(d, st.ask)",
                        "const autoDetail = !native && (isLast || !!(st.ask && st.blocking)",
                       "function ltResultBadge", "function ltFreshness", "failSince: prev.failSince || Date.now()",
                       '"正在回复 · 读库中断"', 'classList.toggle("broken", readBroken)', ".lt-badge",
                       "function openCodeFs", 'id="codeFs"', "code-fsbtn",
                       "function armPinLastUser", "function shouldFollowLiveTurn",
                       "function updatePinByNear", "function releasePinLastUser",
                       "本轮用户之后的第一条 AI"):
            self.assertIn(anchor, self.SHARE, anchor)
        self.assertNotIn('"≈8s 延迟"', self.SHARE)
        self.assertNotIn('d.textContent = st.result || st.text || "";\n  }\n  return d;\n}\n\nfunction paintLtReply',
                         self.SHARE, "工具结果必须走 ltPaintResult")

    def test_share_markdown_and_painters_run_in_node(self):
        sh = self.SHARE
        def sl(a, b):
            i = sh.index(a); return sh[i:sh.index(b, i)]
        js = FAKE_DOM + "globalThis.tapBind = (el, fn) => { el.onclick = fn; };\n" \
            + sl("function ltEsc(", "function ltFillRich(") \
            + sl("const LT_STALE_SEC", "function paintLtReply(") + r"""
const html = ltMarkdown('| a | b |\n|---|---|\n| 1 | 2 |');
if (!html.includes("<table>")) throw new Error("share table: " + html);
if (ltMarkdown('<img src=x onerror=alert(1)>').includes("<img")) throw new Error("share xss");
const now = 1000;
if (ltToolStale({kind:"tool", status:"running", name:"mcp-rxyy MCP-zhi", started: now - 999}, now)) throw new Error("zhi not stale");
if (!ltToolStale({kind:"tool", status:"running", name:"run_terminal_command_v2", started: now - 31}, now)) throw new Error("shell stale");
const u = { open: new Set() };
const d = document.createElement("div");
ltPaintResult(d, { id: "x", label: "终端", result: Array.from({length: 20}, (_, i) => "l" + i).join("\n") }, u);
if (!d.find("lt-res-more").length) throw new Error("share fold toggle");
if (d.find("lt-res-body")[0].textContent.split("\n").length !== LT_RESULT_FOLD) throw new Error("share folded");
const a = document.createElement("div");
ltPaintAsk(a, { title: "t", state: "waiting", questions: [{ id: "q", prompt: "p", options: [{ id: "o", label: "L", picked: true }], answer: ["L"], freeform: "" }] });
if (!a.find("lt-ask-opt")[0].classList.contains("picked")) throw new Error("share ask picked");
console.log("ok");
"""
        code, out = _node(js)
        self.assertEqual(0, code, out)
        self.assertIn("ok", out)

    def test_share_script_parses(self):
        sh = self.SHARE
        js = sh[sh.index("<script>") + 8:sh.rindex("</script>")]
        r = subprocess.run(["node", "--check"], input=js.encode("utf-8"),
                           capture_output=True, timeout=20)
        self.assertEqual(0, r.returncode, r.stderr.decode("utf-8", "replace"))


class StreamReplyTests(unittest.TestCase):
    """流失气泡：生成中露变长正文，对上正式气泡再藏，不贴上一轮 zhi。"""

    def test_paint_lt_reply_shows_live_text_until_host_matches(self):
        js = FAKE_DOM + r"""
Object.defineProperty(El.prototype, "innerHTML", {
  set() { this.children = []; this._text = ""; },
  get() { return this._text; },
});
const reply = new El("div");
const live = { classList: { contains: () => true } };
globalThis.liveTurnHostMatched = false;
globalThis.$ = (id) => id === "ltReply" ? reply : id === "liveTurn" ? live : null;
globalThis.ltFillRich = (el, text) => { el.textContent = text; };
globalThis.ltThinkText = (t) => t;
""" + _slice("function paintLtReply(", "function renderLiveTurn(") + r"""
paintLtReply({ live: true, reply: { kind: "text", text: "正在写第一句" } });
if (reply.style.display !== "flex") throw new Error("live text hidden: " + reply.style.display);
if (!String(reply.className).includes("stream-live")) throw new Error("no caret: " + reply.className);
if (reply.find("lt-reply-lab")[0].textContent !== "正在输出") throw new Error("lab=" + reply.find("lt-reply-lab")[0].textContent);
globalThis.liveTurnHostMatched = true;
paintLtReply({ live: true, reply: { kind: "text", text: "正在写第一句" } });
if (reply.style.display !== "none") throw new Error("dup not hidden");
globalThis.liveTurnHostMatched = false;
paintLtReply({ live: true, reply: { kind: "thinking", text: "先想一下" } });
if (reply.style.display !== "none") throw new Error("in-stream thinking should hide");
console.log("ok");
"""
        code, out = _node(js)
        self.assertEqual(0, code, out)
        self.assertIn("ok", out)


class TimelineWindowTests(unittest.TestCase):
    """长时间线按100步翻页，保留真实总数并能请求未加载历史。"""

    @staticmethod
    def _window_source(src, suffix):
        start = src.index("function coalesceTimelineSteps")
        paint = src.index("function paintLtReply")
        end = paint if paint > start else src.index("function renderLiveTurn", start)
        chunk = src[start:end]
        return (chunk
                .replace("coalesceTimelineSteps", "coalesceTimelineSteps_" + suffix)
                .replace("timelineRenderWindow", "timelineRenderWindow_" + suffix)
                .replace("LT_TIMELINE_RENDER_LIMIT", "LT_TIMELINE_RENDER_LIMIT_" + suffix))

    def test_ui_and_share_have_bounded_pages_and_preserve_real_counts(self):
        share = (MODULE_DIR / "share.html").read_text(encoding="utf-8")
        js = self._window_source(UI, "ui") + self._window_source(share, "share") + r'''
const steps = Array.from({length: 460}, (_, i) => ({kind:"tool", id:"t"+i}));
const turn = {steps,total_steps:460};
const a = timelineRenderWindow_ui(turn, null);
const b = timelineRenderWindow_share(turn, null);
if (JSON.stringify(a)!==JSON.stringify(b)) throw new Error("ui/share differs");
if(a.steps.length!==100 || a.steps[0].id!=="t360" || a.totalSteps!==460) throw new Error("latest page");
let page=a, seen=[];
while (true) {
  if(page.steps.length>100) throw new Error("unbounded nodes");
  seen.push(...page.steps.map(x=>x.id));
  if(page.start===1) break;
  page=timelineRenderWindow_ui(turn,page.start-1);
}
if(new Set(seen).size!==460) throw new Error("history lost or duplicated");
const partial=timelineRenderWindow_share({steps:steps.slice(-40),total_steps:460},null);
if(partial.backendOmitted!==420 || !partial.canExpand) throw new Error("cannot request unloaded history");
const old=timelineRenderWindow_ui({...turn,steps:[...steps,{id:"new",kind:"tool"}],total_steps:461},360);
if(old.steps[0].id!=="t260" || old.steps.at(-1).id!=="t359") throw new Error("live append moved history");
const native=timelineRenderWindow_ui({...turn,runtime_kind:"codex"},null);
if(native.steps.length!==1 || native.steps[0].kind!=="tool_group" || native.steps[0].tools.length!==100)
  throw new Error("native tools not grouped");
console.log("ok");
'''
        code, out = _node(js)
        self.assertEqual(0, code, out)
        self.assertIn("ok", out)

    def test_runtime_kind_and_timeline_capability_contract_is_compatible(self):
        share = (MODULE_DIR / "share.html").read_text(encoding="utf-8")
        for name, src in (("ui", UI), ("share", share)):
            self.assertIn("if (s.has_timeline != null)", src, name)
            self.assertIn("s.has_cursor", src, name)
            start = src.index("const RUNTIME_LABELS")
            end = src.index("const $", start)
            js = "const state = {};\n" + src[start:end] + r'''
const eq = (a, b, why) => { if (a !== b) throw new Error(why + ": " + a); };
eq(runtimeKindOf({runtime_kind: "codex"}), "codex", "codex must stay codex");
eq(runtimeLabelOf({runtime_kind: "chatgpt"}), "ChatGPT", "chatgpt label");
eq(runtimeLabelOf({runtime_kind: "unknown"}), "未知宿主", "unknown label");
eq(sessionHasTimeline({has_timeline: true, has_cursor: false}), true, "new timeline flag");
eq(sessionHasTimeline({has_timeline: false, has_cursor: true}), false, "new false flag wins");
eq(sessionHasTimeline({has_cursor: true}), true, "old cursor fallback");
eq(sessionHasTimeline({capabilities: {timeline: true}}), true, "capability fallback");
console.log("ok");
'''
            code, out = _node(js)
            self.assertEqual(0, code, name + ": " + out)
            self.assertIn("ok", out, name)


if __name__ == "__main__":
    unittest.main()
