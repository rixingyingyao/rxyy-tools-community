"use strict";
/*
 * 安装 chijiu agentboard 钩子到 ~/.cursor/（用户级，所有工作区生效）
 *   node install.cjs                 观察模式（只记录不拦截）
 *   node install.cjs enforce         真互斥
 *   node install.cjs uninstall       从 hooks.json 摘除（脚本与看板保留）
 */
const fs = require("fs");
const os = require("os");
const path = require("path");

const arg = (process.argv[2] || "observe").toLowerCase();
const HERE = __dirname;
const CURSOR_DIR = path.join(os.homedir(), ".cursor");
const HOOKS_DIR = path.join(CURSOR_DIR, "hooks");
const DST = path.join(HOOKS_DIR, "chijiu-agentboard.cjs");
const CFG = path.join(HOOKS_DIR, "chijiu-agentboard.config.json");
const HOOKS_JSON = path.join(CURSOR_DIR, "hooks.json");
// matcher 只让写类工具触发钩子：否则每个 agent 的每次 Read/Shell/MCP 调用
// 都要多 spawn 一个 node 进程（实测全机一分钟几十次），纯属白烧
const EVENTS = {
  preToolUse: "Write|StrReplace|Edit|MultiEdit|Delete|Notebook|write_file|edit_file|move_file",
  afterFileEdit: null,
  stop: null,
};

function readJson(p, dflt) {
  try { return JSON.parse(fs.readFileSync(p, "utf8")); } catch (_) { return dflt; }
}
function writeJson(p, obj) {
  fs.mkdirSync(path.dirname(p), { recursive: true });
  fs.writeFileSync(p, JSON.stringify(obj, null, 2), "utf8");
}
function stripMine(list) {
  return (list || []).filter((h) => !/chijiu-agentboard/i.test(String(h && h.command)));
}

if (arg === "uninstall") {
  const j = readJson(HOOKS_JSON, null);
  if (j && j.hooks) {
    for (const evt of Object.keys(j.hooks)) {
      const kept = stripMine(j.hooks[evt]);
      if (kept.length) j.hooks[evt] = kept; else delete j.hooks[evt];
    }
    writeJson(HOOKS_JSON, j);
  }
  console.log("已摘除 chijiu-agentboard 钩子");
  process.exit(0);
}

const mode = arg === "enforce" ? "enforce" : "observe";

fs.mkdirSync(HOOKS_DIR, { recursive: true });
fs.copyFileSync(path.join(HERE, "hook.cjs"), DST);

// .sessions.json 每次装都重新找：钩子靠它把 uuid 翻成 tab 名，payload 被 Cursor
// 转码坏掉时还靠里面的 cwd 反推真实工作区。旧配置里那份常常指着早已退役的安装目录
// （08-04：还指着 %USERPROFILE%\持久plus，而数据早搬进了打包版的 data\）。
const REPO = path.resolve(HERE, "..", "..");
const sessions = [
  path.join(REPO, "dist", "rxyy-tools-community", "data", "rxyy_mcp", ".sessions.json"),
  path.resolve(HERE, "..", ".sessions.json"),
  path.join(os.homedir(), "rxyy_mcp", ".sessions.json"),
  path.join(REPO, "dist", "rxyy-tools-community", "data", "持久plus", ".sessions.json"),
  path.join(os.homedir(), "持久plus", ".sessions.json"),
].filter((p) => fs.existsSync(p));
const cfg = Object.assign(
  {
    waitMs: 90000,
    pollMs: 300,
    lockTtlMs: 600000,
    agentTtlMs: 1800000,
    logPayload: true,
  },
  readJson(CFG, {}),
  { mode, sessionsJson: sessions }
);
writeJson(CFG, cfg);

const j = readJson(HOOKS_JSON, null) || { version: 1, hooks: {} };
j.version = j.version || 1;
j.hooks = j.hooks || {};
const command = `"${process.execPath}" "${DST}"`;
for (const [evt, matcher] of Object.entries(EVENTS)) {
  const entry = { command, timeout: 120 };
  if (matcher) entry.matcher = matcher;
  j.hooks[evt] = stripMine(j.hooks[evt]).concat([entry]);
}
writeJson(HOOKS_JSON, j);

console.log(`已安装 chijiu-agentboard（${mode} 模式）`);
console.log("  钩子脚本: " + DST);
console.log("  配置    : " + CFG);
console.log("  hooks   : " + HOOKS_JSON);
console.log("  日志    : " + path.join(HOOKS_DIR, "chijiu-agentboard.log"));
