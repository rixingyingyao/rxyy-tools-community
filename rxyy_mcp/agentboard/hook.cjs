"use strict";
/*
 * chijiu agentboard —— 同工作区多 agent 文件互斥 / 协商看板
 *
 * 事件接线（~/.cursor/hooks.json）：
 *   preToolUse    写类工具前：占用 / 排队静默等待 / 超时才拒绝
 *   afterFileEdit 编辑后：续期锁 + 记编辑次数
 *   stop          本轮收尾：释放该 agent 全部锁
 *
 * 铁律：任何异常一律 fail-open（放行）。这个钩子只负责错开撞车，
 * 绝不能成为「谁都改不了文件」的新故障源。
 */
const fs = require("fs");
const path = require("path");

const T0 = Date.now();
let RAW_BYTES = Buffer.alloc(0);
const HERE = __dirname;
const CFG_PATH = path.join(HERE, "chijiu-agentboard.config.json");
const LOG_PATH = path.join(HERE, "chijiu-agentboard.log");

const DEFAULTS = {
  mode: "observe", // observe=只记录不拦截 | enforce=真互斥
  waitMs: 90000, // 撞车后静默等待上限，等到就无感放行
  pollMs: 300,
  lockTtlMs: 600000, // 锁多久没续期就可被抢
  agentTtlMs: 1800000, // agent 多久没动静就判定退场、其锁全废
  logPayload: true,
  sessionsJson: [], // rxyy MCP .sessions.json 候选路径（用于把 cursor uuid 翻成 tab 名）
  ignoreRe: "[\\\\/](\\.chijiu-tmp|\\.git|node_modules|__pycache__|dist|build)[\\\\/]",
  // Cursor 实测 tool_name：Write / StrReplace / Delete / EditNotebook / MultiEdit，
  // MCP 写盘工具形如 MCP:write_file、MCP:edit_file
  writeToolRe: "^(Write|StrReplace|Edit|MultiEdit|SearchReplace|ApplyPatch|Delete|EditNotebook|NotebookEdit)$|^MCP:(write_file|edit_file|move_file)$",
};

let cfg = DEFAULTS;
try {
  cfg = Object.assign({}, DEFAULTS, JSON.parse(fs.readFileSync(CFG_PATH, "utf8")));
} catch (_) {}

function log(line) {
  try {
    try {
      if (fs.statSync(LOG_PATH).size > 2 * 1024 * 1024) {
        try { fs.unlinkSync(LOG_PATH + ".1"); } catch (_) {}
        fs.renameSync(LOG_PATH, LOG_PATH + ".1");
      }
    } catch (_) {}
    fs.appendFileSync(LOG_PATH, new Date().toISOString() + " " + line + "\n");
  } catch (_) {}
}

function sleepSync(ms) {
  try {
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
  } catch (_) {
    const end = Date.now() + ms;
    while (Date.now() < end) {}
  }
}

function out(obj) {
  try { process.stdout.write(JSON.stringify(obj)); } catch (_) {}
  const ms = Date.now() - T0;
  if (ms > 1000) log(`   慢: ${ms}ms ${obj.permission || ""}`);
  process.exit(0);
}

const allow = () => out({ permission: "allow" });
const noop = () => out({});

// ---------- payload 取值：字段名各版本不一，全部宽松匹配 ----------

function pick(obj, names) {
  if (!obj || typeof obj !== "object") return null;
  for (const n of names) {
    const v = obj[n];
    if (typeof v === "string" && v.trim()) return v.trim();
  }
  return null;
}

function agentKey(p) {
  return (
    pick(p, ["conversation_id", "conversationId", "thread_id", "threadId",
             "chat_id", "chatId", "composer_id", "composerId", "session_id",
             "sessionId"]) || "pid-" + (process.ppid || process.pid)
  );
}

// Cursor 给的路径混着 file:// URI、/d:/x 这种前导斜杠的盘符形式和正斜杠，
// 不归一化会 path.resolve 成 C:\d:\x（实测踩过）
function normPath(s) {
  if (!s) return "";
  let v = String(s).trim().replace(/^file:\/\/\/?/i, "");
  if (/%[0-9A-Fa-f]{2}/.test(v)) { try { v = decodeURIComponent(v); } catch (_) {} }
  v = v.replace(/^[\\/]+([A-Za-z]:)/, "$1");
  return v.replace(/\//g, path.sep);
}

function collectPaths(node, acc, depth) {
  if (!node || depth > 4) return acc;
  if (typeof node === "string") {
    if (/[\\/]/.test(node) && /\.[A-Za-z0-9]{1,8}$/.test(node)) acc.push(node);
    return acc;
  }
  if (Array.isArray(node)) {
    for (const v of node) collectPaths(v, acc, depth + 1);
    return acc;
  }
  if (typeof node === "object") {
    for (const [k, v] of Object.entries(node)) {
      if (/(path|file|target|uri|filename)/i.test(k) || Array.isArray(v) || (v && typeof v === "object")) {
        collectPaths(v, acc, depth + 1);
      }
    }
  }
  return acc;
}

function targetPaths(p) {
  const input = p.tool_input || p.toolInput || p.input || p.arguments || {};
  // afterFileEdit 的 file_path 在顶层，preToolUse 的在 tool_input 里
  const direct = pick(input, ["path", "file_path", "filePath", "target_file",
                              "absolute_path", "absolutePath", "uri", "filename"])
              || pick(p, ["file_path", "filePath", "path"]);
  const acc = direct ? [direct] : [];
  collectPaths(input, acc, 0);
  const seen = new Set();
  return acc
    .map(normPath)
    .filter((s) => {
      const k = s.toLowerCase();
      if (seen.has(k)) return false;
      seen.add(k);
      return true;
    });
}

function rootList(p) {
  const roots = p.workspace_roots || p.workspaceRoots || p.roots || [];
  return (Array.isArray(roots) ? roots : [roots])
    .map((r) => (typeof r === "string" ? r : (r && (r.path || r.uri)) || ""))
    .map(normPath)
    .filter(Boolean);
}

function workspaceRoot(p, filePath) {
  const list = rootList(p);
  if (filePath) {
    const lower = path.resolve(filePath).toLowerCase();
    for (const r of list) {
      if (lower.startsWith(path.resolve(r).toLowerCase() + path.sep)) return path.resolve(r);
    }
  }
  if (list.length) return path.resolve(list[0]);
  // 兜底：从文件往上找 .git
  let dir = filePath ? path.dirname(path.resolve(filePath)) : process.cwd();
  for (let i = 0; i < 12; i++) {
    if (fs.existsSync(path.join(dir, ".git"))) return dir;
    const up = path.dirname(dir);
    if (up === dir) break;
    dir = up;
  }
  return null;
}

// ---------- 看板读写 ----------

function boardPath(root) {
  return path.join(root, ".chijiu-tmp", "agentboard.json");
}

function loadBoard(bp) {
  try {
    const b = JSON.parse(fs.readFileSync(bp, "utf8"));
    b.agents = b.agents || {};
    b.locks = b.locks || {};
    b.queue = b.queue || {};
    b.notes = b.notes || [];
    return b;
  } catch (_) {
    return { version: 1, agents: {}, locks: {}, queue: {}, notes: [] };
  }
}

function saveBoard(bp, b) {
  b.updated = Date.now();
  const tmp = bp + ".tmp" + process.pid;
  fs.mkdirSync(path.dirname(bp), { recursive: true });
  fs.writeFileSync(tmp, JSON.stringify(b, null, 2));
  fs.renameSync(tmp, bp);
}

function withBoardLock(bp, fn) {
  const lp = bp + ".lock";
  const deadline = Date.now() + 3000;
  let fd = null;
  try { fs.mkdirSync(path.dirname(bp), { recursive: true }); } catch (_) {}
  while (Date.now() < deadline) {
    try { fd = fs.openSync(lp, "wx"); break; } catch (_) {}
    try {
      if (Date.now() - fs.statSync(lp).mtimeMs > 5000) { fs.unlinkSync(lp); continue; }
    } catch (_) {}
    sleepSync(40);
  }
  try {
    return fn();
  } finally {
    if (fd !== null) {
      try { fs.closeSync(fd); } catch (_) {}
      try { fs.unlinkSync(lp); } catch (_) {}
    }
  }
}

function gc(b, now) {
  for (const [k, l] of Object.entries(b.locks)) {
    const a = b.agents[l.owner];
    const ownerGone = !a || now - (a.lastSeen || 0) > cfg.agentTtlMs;
    if (ownerGone || now - (l.renewed || l.since || 0) > cfg.lockTtlMs) delete b.locks[k];
  }
  for (const [k, q] of Object.entries(b.queue)) {
    const kept = (q || []).filter((w) => now - (w.since || 0) < cfg.waitMs * 2);
    if (kept.length) b.queue[k] = kept; else delete b.queue[k];
  }
  for (const [k, a] of Object.entries(b.agents)) {
    if (now - (a.lastSeen || 0) > cfg.agentTtlMs * 2) delete b.agents[k];
  }
}

function relKey(root, file) {
  const rel = path.relative(root, path.resolve(file)) || path.resolve(file);
  return rel.split(path.sep).join("/").toLowerCase();
}

// ---------- 占用者身份：尽量翻成控制台 tab 名 ----------

function describeOwner(b, key, root) {
  const a = b.agents[key] || {};
  const tab = a.tab || lookupTab(a.uuid || key, root);
  const tail = a.model ? `${key.slice(0, 8)} · ${a.model}` : key.slice(0, 8);
  return tab ? `「${tab}」(${tail})` : `会话 ${tail}`;
}

function lookupTab(cursorUuid, root) {
  const cands = (cfg.sessionsJson || []).slice();
  if (root) {
    cands.push(path.join(root, "rxyy_mcp", ".sessions.json"));
    cands.push(path.join(root, "持久plus", ".sessions.json"));
  }
  for (const p of cands) {
    try {
      const arr = JSON.parse(fs.readFileSync(p, "utf8"));
      for (const s of Array.isArray(arr) ? arr : []) {
        if (s && s.cursor_uuid && String(s.cursor_uuid) === cursorUuid) {
          return s.name || s.cursor_title || null;
        }
      }
    } catch (_) {}
  }
  return null;
}

// ---------- payload 坏了之后的抢救：按 ASCII 骨架反推真实路径 ----------
//
// Cursor 交给钩子的 payload 会把非 ASCII 双重转码（桌面 → 妗岄潰），转码时还会把
// 紧邻的收尾引号一起吞成 ?，于是整份 JSON 解析不了、钩子静默空跑。工作区路径本身
// 带中文，所以这台机器上每一条 payload 都坏——看板从来没记下过一条锁。
// 好在坏掉的只有非 ASCII：ID、事件名是干净的，路径也还留着 ASCII 骨架，
// 拿骨架去真实目录里对一遍就能把路认回来。

function asciiSkeleton(s) {
  return String(s).replace(/[^A-Za-z0-9._+-]+/g, "").toLowerCase();
}

function pickRaw(raw, key) {
  // 值里可能带被吞掉的引号，所以到引号、逗号、右括号任一处为止；
  // 抓到的还是 JSON 字面量，反斜杠是转义过的（C:\\Users），得还原成真路径
  const m = new RegExp('"' + key + '"\\s*:\\s*"([^"\\],]*)').exec(raw);
  return m ? m[1].replace(/\\\\/g, "\\").replace(/\\\//g, "/") : "";
}

function knownRoots() {
  const out = [];
  for (const p of cfg.sessionsJson || []) {
    try {
      const arr = JSON.parse(fs.readFileSync(p, "utf8"));
      for (const s of Array.isArray(arr) ? arr : []) {
        for (const r of [s && s.cwd, s && s.task_root]) {
          if (r && !out.includes(r)) out.push(r);
        }
      }
    } catch (_) {}
  }
  return out;
}

function resolveUnderRoot(root, segs) {
  // 逐段拿骨架去真实目录里认：认得出就用真名，认不出而本段全 ASCII 就照用
  let cur = root;
  for (let i = 0; i < segs.length; i++) {
    const seg = segs[i];
    if (!seg || seg === ".") continue;
    const direct = path.join(cur, seg);
    if (fs.existsSync(direct)) { cur = direct; continue; }
    let entries = [];
    try { entries = fs.readdirSync(cur); } catch (_) { entries = []; }
    const want = asciiSkeleton(seg);
    const hit = want ? entries.filter((e) => asciiSkeleton(e) === want) : [];
    if (hit.length === 1) { cur = path.join(cur, hit[0]); continue; }
    // 新建的文件还不在盘上，最后一段认不出很正常：名字本身没坏就直接用
    if (i === segs.length - 1 && !/[^\x20-\x7E]/.test(seg)) {
      cur = path.join(cur, seg);
      continue;
    }
    return null;
  }
  return cur;
}

function recoverPayload(raw) {
  const event = pickRaw(raw, "hook_event_name");
  if (!event) return null;
  const badRootRaw = (raw.match(/"workspace_roots"\s*:\s*\[\s*"([^"\],]*)/) || [])[1] || "";
  const badRoot = normPath(badRootRaw.replace(/\\\\/g, "\\"));
  const badFile = pickRaw(raw, "file_path");
  const roots = knownRoots();
  const flat = (s) => asciiSkeleton(String(s).replace(/[\\/]/g, ""));

  const root = roots.find((r) => flat(r) === flat(badRoot)) || null;
  if (!root) return null;

  // 拿「第几段」而不是「第几个字符」去切：转码只啃非 ASCII，反斜杠是 ASCII、
  // 一根不少，所以段数是可信的；按长度切必错位——同一个汉字挨着引号和挨着
  // 反斜杠时被啃成的样子并不一样。切出相对路径后再逐段认回真名。
  let realFile = null;
  if (badFile) {
    const segs = normPath(badFile).split(/[\\/]/).filter(Boolean);
    const depth = root.split(/[\\/]/).filter(Boolean).length;
    if (segs.length > depth) realFile = resolveUnderRoot(root, segs.slice(depth));
  }

  const p = {
    hook_event_name: event,
    tool_name: pickRaw(raw, "tool_name"),
    conversation_id: pickRaw(raw, "conversation_id"),
    session_id: pickRaw(raw, "session_id"),
    transcript_path: pickRaw(raw, "transcript_path"),
    workspace_roots: [root],
  };
  if (realFile) p.tool_input = { file_path: realFile };
  log(`   已按 ASCII 骨架救回: root=${root}` +
      (realFile ? ` file=${realFile}` : " （文件名没认出来）"));
  return p;
}

function humanAge(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 60) return s + " 秒";
  const m = Math.floor(s / 60);
  if (m < 60) return m + " 分钟";
  return Math.floor(m / 60) + " 小时" + (m % 60) + " 分";
}

// ---------- 主流程 ----------

// Windows 上 fs.readFileSync(0) 常直接返回空（管道 EAGAIN），必须自己轮询读到 EOF
function readStdin() {
  const chunks = [];
  const buf = Buffer.alloc(65536);
  const deadline = Date.now() + 3000;
  let idle = 0;
  let fresh = false;
  // 收到的是不是一份完整 JSON。只按「空转几次就当读完」收工会把大 payload 腰斩：
  // Cursor 分多次把 tool_input（含整份文件正文）写进管道，中途 EAGAIN 是常态，
  // 20ms 就罢手拿到的是半截 JSON，解析失败后整个钩子静默空跑 —— 08-04 实测，
  // 家机上文件占用看板因此一条锁都没记过（rawsample 截在 position 1158，
  // 正好断在工作区路径的一个汉字中间）。
  const complete = () => {
    try {
      JSON.parse(Buffer.concat(chunks).toString("utf8").replace(/^\uFEFF/, ""));
      return true;
    } catch (_) {
      return false;
    }
  };
  while (Date.now() < deadline) {
    let n = 0;
    try {
      n = fs.readSync(0, buf, 0, buf.length, null);
    } catch (e) {
      const code = e && e.code;
      if (code === "EAGAIN") {
        // 已经读到过内容还 EAGAIN，多半是写端没关管道：解析得通就收工，
        // 解析不通说明还没写完，接着等（每来一批新数据才重试一次解析）
        if (chunks.length) {
          if (fresh) {
            fresh = false;
            if (complete()) break;
          }
          if (++idle > 400) break;
        }
        sleepSync(5);
        continue;
      }
      break; // EOF / EPIPE / 其它一律当读完
    }
    if (n <= 0) break;
    idle = 0;
    fresh = true;
    chunks.push(Buffer.from(buf.subarray(0, n)));
  }
  RAW_BYTES = Buffer.concat(chunks);
  return RAW_BYTES.toString("utf8");
}

function main() {
  const raw = readStdin();
  let p = {};
  try {
    p = JSON.parse(raw.replace(/^\uFEFF/, "") || "{}");
  } catch (e) {
    if (cfg.logPayload) {
      try {
        fs.writeFileSync(
          path.join(HERE, "chijiu-agentboard.rawsample.txt"),
          "parse error: " + (e && e.message) + "\n---\n" + raw.slice(0, 4000)
        );
        // 同时留一份原始字节：解析失败十有八九是编码/截断，光看解码后的文本
        // 分不清是「谁把字啃了」还是「本来就没收全」（08-04 查看板空白时踩的坑）
        fs.writeFileSync(path.join(HERE, "chijiu-agentboard.rawsample.bin"),
                         RAW_BYTES.subarray(0, 8192));
      } catch (_) {}
    }
    try {
      p = recoverPayload(raw) || {};
    } catch (err) {
      log("   抢救也失败了（放行）: " + (err && err.message));
    }
  }

  const event = pick(p, ["hook_event_name", "hookEventName", "event"]) || "";
  const tool = pick(p, ["tool_name", "toolName", "tool"]) || "";
  const me = agentKey(p);

  if (cfg.logPayload) {
    log(`[${event}] tool=${tool} agent=${me.slice(0, 12)} raw=${raw.length}B keys=${Object.keys(p).join(",")}`);
    if (/pretooluse|aftertooluse|afterfileedit/i.test(event)) {
      log("   input=" + JSON.stringify(p.tool_input || p.toolInput || p.input || {}).slice(0, 600));
    }
  }

  if (/^stop$|sessionend/i.test(event)) return releaseAll(p, me);
  if (/afterfileedit/i.test(event)) return renew(p, me);
  if (!/pretooluse/i.test(event)) return noop();

  if (!tool || !new RegExp(cfg.writeToolRe).test(tool)) return allow();

  const files = targetPaths(p).filter(
    (f) => !new RegExp(cfg.ignoreRe, "i").test(f)
  );
  if (!files.length) return allow();

  const file = files[0];
  const root = workspaceRoot(p, file);
  if (!root) return allow();
  const bp = boardPath(root);
  const key = relKey(root, file);

  const claimed = tryClaim(bp, key, me, p, tool);
  if (claimed.ok) return allow();

  if (cfg.mode !== "enforce") {
    log(`   OBSERVE 冲突: ${key} 被 ${claimed.owner} 占着，观察模式放行`);
    return allow();
  }

  // 静默等待：绝大多数撞车在这里自愈，两边都能改完
  const deadline = Date.now() + cfg.waitMs;
  while (Date.now() < deadline) {
    sleepSync(cfg.pollMs);
    const again = tryClaim(bp, key, me, p, tool);
    if (again.ok) {
      log(`   等到锁: ${key} 等了 ${humanAge(cfg.waitMs - (deadline - Date.now()))}`);
      return allow();
    }
  }

  const b = loadBoard(bp);
  const lock = b.locks[key] || {};
  const q = b.queue[key] || [];
  const pos = Math.max(1, q.findIndex((w) => w.agent === me) + 1);
  const owner = describeOwner(b, lock.owner || "", root);
  const held = humanAge(Date.now() - (lock.since || Date.now()));
  log(`   DENY ${key} owner=${lock.owner} held=${held}`);
  // 实测 Cursor 把 user_message 当成工具报错回灌给 agent、agent_message 未必露出，
  // 所以把「怎么办」压进 user_message，agent_message 留完整版
  const intent = lock.intent ? `，意图：${lock.intent}` : "";
  out({
    permission: "deny",
    user_message:
      `🔒 文件互斥：${key} 正被${owner}占用（已 ${held}${intent}）。` +
      `等了 ${humanAge(cfg.waitMs)} 仍未释放，你排第 ${pos}。` +
      `请先改别的文件或用 zhi 请示用户，别重试本次编辑——对方一释放会自动放行。`,
    agent_message:
      `【同工作区文件互斥】\`${key}\` 正被${owner}占用（已持有 ${held}${intent}）。` +
      `我已等了 ${humanAge(cfg.waitMs)} 仍未释放，你排在第 ${pos} 位。\n` +
      `别硬改，选一个：\n` +
      `1) 先去改别的文件/干别的活，过会儿再回来（对方一释放，下次编辑会自动放行）；\n` +
      `2) 用 zhi 把冲突报给用户，让他决定谁先改；\n` +
      `3) 在 \`${path.join(root, ".chijiu-tmp", "agentboard.json")}\` 的 notes 里留言协商分工。\n` +
      `注意：这是本机多 agent 协作的硬约束，重试同一次编辑没用。`,
  });
}

function sessionUuid(p) {
  const tp = pick(p, ["transcript_path", "transcriptPath"]);
  if (!tp) return null;
  return path.basename(tp).replace(/\.jsonl$/i, "") || null;
}

function tryClaim(bp, key, me, p, tool) {
  let res = { ok: false, owner: "" };
  try {
    withBoardLock(bp, () => {
      const now = Date.now();
      const b = loadBoard(bp);
      gc(b, now);
      const a = (b.agents[me] = b.agents[me] || { since: now });
      a.lastSeen = now;
      a.tool = tool;
      a.model = pick(p, ["model"]) || a.model || "";
      a.uuid = sessionUuid(p) || a.uuid || "";
      const lock = b.locks[key];
      if (!lock || lock.owner === me) {
        b.locks[key] = {
          owner: me,
          since: lock ? lock.since : now,
          renewed: now,
          edits: lock ? lock.edits || 0 : 0,
          intent: (lock && lock.intent) || "",
        };
        b.queue[key] = (b.queue[key] || []).filter((w) => w.agent !== me);
        if (!b.queue[key].length) delete b.queue[key];
        res = { ok: true, owner: me };
      } else {
        const q = (b.queue[key] = b.queue[key] || []);
        if (!q.some((w) => w.agent === me)) q.push({ agent: me, since: now });
        res = { ok: false, owner: lock.owner };
      }
      saveBoard(bp, b);
    });
  } catch (e) {
    log("tryClaim 异常（放行）: " + (e && e.message));
    res = { ok: true, owner: me };
  }
  return res;
}

function renew(p, me) {
  try {
    // 跟 preToolUse 用同一套忽略规则：否则 .chijiu-tmp 之类的临时文件
    // 会在这里被建锁，白白污染看板（拦截侧压根不看它们）
    const files = targetPaths(p).filter(
      (f) => !new RegExp(cfg.ignoreRe, "i").test(f)
    );
    if (!files.length) return noop();
    const root = workspaceRoot(p, files[0]);
    if (!root) return noop();
    const bp = boardPath(root);
    withBoardLock(bp, () => {
      const now = Date.now();
      const b = loadBoard(bp);
      const a = (b.agents[me] = b.agents[me] || { since: now });
      a.lastSeen = now;
      for (const f of files) {
        const key = relKey(root, f);
        const l = (b.locks[key] = b.locks[key] || { owner: me, since: now, edits: 0 });
        if (l.owner === me) {
          l.renewed = now;
          l.edits = (l.edits || 0) + 1;
        }
      }
      saveBoard(bp, b);
    });
  } catch (_) {}
  return noop();
}

function releaseAll(p, me) {
  try {
    for (const root of rootList(p)) {
      const bp = boardPath(path.resolve(root));
      if (!fs.existsSync(bp)) continue;
      withBoardLock(bp, () => {
        const b = loadBoard(bp);
        let n = 0;
        for (const [k, l] of Object.entries(b.locks)) {
          if (l.owner === me) { delete b.locks[k]; n++; }
        }
        for (const [k, q] of Object.entries(b.queue)) {
          const kept = (q || []).filter((w) => w.agent !== me);
          if (kept.length) b.queue[k] = kept; else delete b.queue[k];
        }
        if (b.agents[me]) b.agents[me].lastSeen = Date.now();
        if (n) log(`[stop] ${me.slice(0, 12)} 释放 ${n} 个锁 @ ${root}`);
        saveBoard(bp, b);
      });
    }
  } catch (_) {}
  return noop();
}

try {
  main();
} catch (e) {
  log("顶层异常（放行）: " + (e && e.stack));
  allow();
}
