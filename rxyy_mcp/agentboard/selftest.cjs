"use strict";
/*
 * agentboard 自测：不依赖 Cursor，直接喂真实形状的 payload 给钩子。
 *   node selftest.cjs [工作区路径]
 * 覆盖：空闲秒拿锁 / 撞车静默等待 / 等超时拒绝 / stop 释放 / 归一化怪路径
 */
const fs = require("fs");
const os = require("os");
const path = require("path");
const { execFileSync } = require("child_process");

const ROOT = path.resolve(process.argv[2] || path.resolve(__dirname, "..", ".."));
const HOOK = path.join(os.homedir(), ".cursor", "hooks", "chijiu-agentboard.cjs");
const CFG = path.join(os.homedir(), ".cursor", "hooks", "chijiu-agentboard.config.json");
const BOARD = path.join(ROOT, ".chijiu-tmp", "agentboard.json");
const PROBE = path.join(ROOT, "_agentboard_probe.txt");

function call(payload) {
  const t0 = Date.now();
  const out = execFileSync(process.execPath, [HOOK], {
    input: JSON.stringify(payload),
    encoding: "utf8",
  });
  return { ms: Date.now() - t0, out: out.trim() };
}

function pre(agent, file, extra) {
  return Object.assign({
    hook_event_name: "preToolUse",
    tool_name: "Write",
    conversation_id: agent,
    model: "selftest",
    // 故意用 Cursor 真实形状：前导斜杠 + 正斜杠盘符
    workspace_roots: ["/" + ROOT.replace(/\\/g, "/")],
    transcript_path: "x/" + agent + ".jsonl",
    tool_input: { file_path: file, content: "x" },
  }, extra || {});
}

function loadBoard() {
  try { return JSON.parse(fs.readFileSync(BOARD, "utf8")); } catch (_) { return {}; }
}

function setCfg(patch) {
  const c = JSON.parse(fs.readFileSync(CFG, "utf8"));
  Object.assign(c, patch);
  fs.writeFileSync(CFG, JSON.stringify(c, null, 2));
  return c;
}

function reset() {
  try { fs.unlinkSync(BOARD); } catch (_) {}
}

let failed = 0;
function check(name, cond, detail) {
  console.log((cond ? "  PASS " : "  FAIL ") + name + (detail ? "  " + detail : ""));
  if (!cond) failed++;
}

console.log("工作区:", ROOT);
console.log("看板  :", BOARD);

// 1) 空闲：秒拿锁
setCfg({ mode: "enforce", waitMs: 4000, pollMs: 200 });
reset();
let r = call(pre("agent-A", PROBE));
check("空闲文件放行", /"permission":"allow"/.test(r.out) && r.ms < 3000, `${r.ms}ms ${r.out}`);
let b = loadBoard();
check("锁已落到看板", !!(b.locks && b.locks["_agentboard_probe.txt"]),
  JSON.stringify(b.locks || {}));
check("路径归一化正确（不是 C:\\d:\\...）", fs.existsSync(BOARD));

// 2) 同一 agent 重入：不自锁
r = call(pre("agent-A", PROBE));
check("同 agent 重入放行", /"permission":"allow"/.test(r.out), r.out);

// 3) 撞车：B 等到超时被拒
r = call(pre("agent-B", PROBE));
check("撞车拒绝", /"permission":"deny"/.test(r.out), `${r.ms}ms`);
check("等待时长≈waitMs", r.ms >= 3500 && r.ms < 12000, `${r.ms}ms`);
check("拒绝文案带占用者", /agent-A|会话 agent/.test(r.out), r.out.slice(0, 200));

// 4) A 收尾释放后，B 立刻拿到
call({
  hook_event_name: "stop",
  conversation_id: "agent-A",
  workspace_roots: ["/" + ROOT.replace(/\\/g, "/")],
});
b = loadBoard();
check("stop 后锁已释放", !(b.locks || {})["_agentboard_probe.txt"], JSON.stringify(b.locks || {}));
r = call(pre("agent-B", PROBE));
check("B 随后秒拿锁", /"permission":"allow"/.test(r.out) && r.ms < 3000, `${r.ms}ms`);

// 5) observe 模式：撞车也放行
setCfg({ mode: "observe" });
r = call(pre("agent-C", PROBE));
check("observe 模式撞车放行", /"permission":"allow"/.test(r.out) && r.ms < 3000, `${r.ms}ms`);

// 6) 非写工具与忽略目录不进流程
setCfg({ mode: "enforce" });
r = call(pre("agent-D", PROBE, { tool_name: "Read" }));
check("Read 直接放行", /"permission":"allow"/.test(r.out) && r.ms < 3000, `${r.ms}ms`);
r = call(pre("agent-D", path.join(ROOT, ".chijiu-tmp", "whatever.json")));
check("忽略目录直接放行", /"permission":"allow"/.test(r.out) && r.ms < 3000, `${r.ms}ms`);

// 7) 兜底：看板文件损坏也不能挡住干活
fs.writeFileSync(BOARD, "{ not json");
r = call(pre("agent-E", PROBE));
check("看板损坏仍放行", /"permission":"allow"/.test(r.out), r.out);

// 8) payload 分批到达、中间 EAGAIN：不能读半截就当读完
//
// Cursor 给钩子的 stdin 是非阻塞管道，大 payload（Write 带着整份文件正文）会分多
// 批到达，中途 readSync 抛 EAGAIN 是常态。旧实现连空转 4 次就当读完，于是拿到半截
// JSON、解析失败、整个钩子静默空跑 —— 08-04 家机实测：钩子一直在跑，看板却一条锁
// 都没记过（rawsample 截在 position 1158，正好断在工作区路径的一个汉字中间）。
// 普通管道是阻塞的，execFileSync 喂再大的 payload 也复现不出来，只能把 EAGAIN 造出来。
reset();
const chunkPayload = pre("agent-F", PROBE);
chunkPayload.tool_input.content = "内容".repeat(2000);
const DRIVER = path.join(os.tmpdir(), "chijiu-agentboard-eagain-driver.cjs");
fs.writeFileSync(DRIVER, `const fs = require("fs");
const b = Buffer.from(${JSON.stringify(JSON.stringify(chunkPayload))}, "utf8");
const half = Math.floor(b.length / 2);
let stage = 0;
const orig = fs.readSync;
fs.readSync = function (fd, buf) {
  if (fd !== 0) return orig.apply(this, arguments);
  if (stage === 0) { stage = 1; b.copy(buf, 0, 0, half); return half; }
  if (stage < 10) { stage++; const e = new Error("EAGAIN"); e.code = "EAGAIN"; throw e; }
  if (stage === 10) { stage = 11; b.copy(buf, 0, half); return b.length - half; }
  return 0;
};
require(${JSON.stringify(HOOK)});
`, "utf8");
r = (() => {
  const t0 = Date.now();
  const out = execFileSync(process.execPath, [DRIVER], { encoding: "utf8" });
  return { ms: Date.now() - t0, out: out.trim() };
})();
check("分批到达仍解析出 preToolUse", /"permission":"allow"/.test(r.out), r.out.slice(0, 80));
b = loadBoard();
check("分批到达的锁也落到看板", !!(b.locks && b.locks["_agentboard_probe.txt"]),
  JSON.stringify(Object.keys(b.locks || {})));

reset();
setCfg({ mode: "observe", waitMs: 90000 });
console.log(failed ? `\n${failed} 项未通过` : "\n全部通过");
process.exit(failed ? 1 : 0);
