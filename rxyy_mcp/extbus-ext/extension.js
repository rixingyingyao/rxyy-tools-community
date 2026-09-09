'use strict';
/**
 * rxyy MCP · 窗口总线（Cursor 扩展这一头）。
 *
 * 只干三件事，全部是 hub 那个独立进程自己做不到、必须站在扩展宿主里才能做的：
 *   1. 把本窗口登记到 <root>/instances.json（每 15s 一次，60s 没登记就算下线）
 *   2. 每秒泵一次 <root>/cmds/<instanceId>/*.req.json，执行后把结果写回 .res.json
 *   3. 三个 action：ping / batchOpen（composer.createNew 无头开对话）/ openCursor（调到前台）
 *
 * 刻意不做：不注入 workbench、不碰 extensionHostProcess.js、不写工作区规则、不加在线闸。
 * composer.createNew / getOrderedSelectedComposerIds / openAgentById 都是 Cursor 的私有命令，
 * 不在公开 API 里——全部包在 try/catch 里，且命令名做成设置项，Cursor 升版改名时不用改代码。
 *
 * 协议与 bajie-chat 0.7.45 的跨窗口总线同构（见 bajie-chat扩展全解析-20260902.md §3.5），
 * hub 侧对应 rxyy_mcp/ext_bus.py。
 */
const vscode = require('vscode');
const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');

const INSTANCE_KEY = 'chijiu.extbus.instanceId';
const INSTANCE_FRESH_MS = 60 * 1000;
const REGISTER_EVERY_MS = 15 * 1000;
const CMD_TTL_MS = 5 * 60 * 1000;
const MOUNT_TRIES = 20;
const MOUNT_STEP_MS = 150;
// composer.createNew 带 autoSubmit 时，它返回的 promise 要等**这轮对话整个跑完**才 resolve。
// 开出来的 agent 一报到就堵在 zhi 上等人 → promise 永不 resolve → 老泵 await 在这里，后面所有
// 命令（连 ping）都进不来（09-07 13:35/13:37 两次单开 grok 全无回应，13:32 那批起的 agent 还在等人）。
// 能报的错（命令不存在 / partialState 炸）都在头几百毫秒就 reject，所以只等这么久拿同步错误。
const CREATE_ACK_MS = 2500;
// 单条命令的硬上限：到点先回一个失败结果放行，别让一条卡住的命令拖死整个收件箱
const CMD_HARD_MS = 90 * 1000;

// composer.createNew 的 partialState.context 必须结构完整：36 个键全给，列表类给 []，
// 其余给 undefined，再加一份同形的 mentions。少一个键就炸——这份键表照抄 bajie。
const CONTEXT_KEYS = [
  'notepads', 'composers', 'quotes', 'selectedCommits', 'selectedPullRequests', 'gitDiff',
  'gitDiffFromBranchToMain', 'selectedImages', 'selectedDocuments', 'selectedVideos',
  'useWeb', 'usePlaywrightMcp', 'folderSelections', 'fileSelections', 'terminalFiles',
  'selections', 'terminalSelections', 'selectedDocs', 'externalLinks', 'useLinterErrors',
  'useDiffReview', 'useGenerateRules', 'useContextPicking', 'useRememberThis',
  'diffHistory', 'cursorRules', 'cursorCommands', 'autoContext', 'uiElementSelections',
  'consoleLogs', 'ideEditorsState', 'gitPRDiffSelections', 'subagentSelections',
  'browserSelections', 'extraContext',
];
const LIST_KEYS = new Set([
  'notepads', 'composers', 'quotes', 'selectedCommits', 'selectedPullRequests',
  'selectedImages', 'selectedDocuments', 'selectedVideos', 'folderSelections',
  'fileSelections', 'terminalFiles', 'selections', 'terminalSelections', 'selectedDocs',
  'externalLinks', 'cursorRules', 'cursorCommands', 'uiElementSelections', 'consoleLogs',
  'gitPRDiffSelections', 'subagentSelections', 'browserSelections', 'extraContext',
]);

let output;
let instanceId = '';
let registerTimer;
let pumpTimer;
let stats = { handled: 0, failed: 0, lastAction: '', lastAt: 0, created: 0 };

function cfg() {
  return vscode.workspace.getConfiguration('chijiu.extbus');
}

function log(msg) {
  try { output && output.appendLine(`[${new Date().toLocaleTimeString()}] ${msg}`); } catch { /* ignore */ }
}

function busRoot() {
  const custom = String(cfg().get('root') || '').trim();
  if (custom) return custom;
  const env = String(process.env.CHIJIU_EXTBUS_ROOT || '').trim();
  if (env) return env;
  const local = String(process.env.LOCALAPPDATA || '').trim();
  if (local) return path.join(local, 'rxyy-tools-community', 'extbus');
  return path.join(os.homedir(), '.rxyy-tools-community', 'extbus');
}

function sanitize(name) {
  return String(name || '').replace(/[^A-Za-z0-9_-]/g, '').slice(0, 80) || '_';
}

function cmdDir() {
  return path.join(busRoot(), 'cmds', sanitize(instanceId));
}

function readJson(file, fallback) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return fallback; }
}

function writeJsonAtomic(file, obj) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = `${file}.${process.pid}.${Date.now()}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(obj), 'utf8');
  fs.renameSync(tmp, file);
}

function stableInstanceId(context) {
  const saved = String(context.workspaceState.get(INSTANCE_KEY) || '').trim();
  if (saved) return saved;
  const fresh = crypto.randomBytes(6).toString('hex');
  void context.workspaceState.update(INSTANCE_KEY, fresh);
  return fresh;
}

function instanceLabel() {
  const folders = vscode.workspace.workspaceFolders || [];
  return (folders[0] && folders[0].name) || vscode.workspace.name || 'Cursor 窗口';
}

function workspacePath() {
  const folders = vscode.workspace.workspaceFolders || [];
  return (folders[0] && folders[0].uri && folders[0].uri.fsPath) || '';
}

// ---------- 登记 ----------

function readInstances() {
  const raw = readJson(path.join(busRoot(), 'instances.json'), { instances: [] });
  const items = Array.isArray(raw && raw.instances) ? raw.instances : [];
  return items.filter(x => x && typeof x.id === 'string' && x.id);
}

function registerInstance() {
  try {
    const now = Date.now();
    const others = readInstances().filter(x => x.id !== instanceId && now - (x.updatedAt || 0) < INSTANCE_FRESH_MS);
    others.push({
      id: instanceId,
      label: instanceLabel(),
      workspace: workspacePath(),
      pid: process.pid,
      version: vscode.version,
      updatedAt: now,
    });
    writeJsonAtomic(path.join(busRoot(), 'instances.json'), { instances: others });
  } catch (e) {
    log(`登记失败：${e && e.message || e}`);
  }
}

function unregisterInstance() {
  try {
    const rest = readInstances().filter(x => x.id !== instanceId);
    writeJsonAtomic(path.join(busRoot(), 'instances.json'), { instances: rest });
  } catch { /* ignore */ }
}

// ---------- 命令收件箱 ----------

function takeCommands() {
  const dir = cmdDir();
  let names = [];
  try { names = fs.readdirSync(dir); } catch { return []; }
  const now = Date.now();
  const out = [];
  for (const name of names) {
    const file = path.join(dir, name);
    try {
      if (name.endsWith('.req.json')) {
        const req = readJson(file, null);
        fs.rmSync(file, { force: true });           // 取走即删：一条命令只执行一次
        if (req && req.id && now - (req.createdAt || 0) < CMD_TTL_MS) out.push(req);
      } else if (name.endsWith('.res.json') || name.endsWith('.tmp')) {
        if (now - fs.statSync(file).mtimeMs > CMD_TTL_MS) fs.rmSync(file, { force: true });
      }
    } catch { /* 单个文件坏了不影响别的 */ }
  }
  return out;
}

function writeResult(cmdId, result) {
  try {
    writeJsonAtomic(path.join(cmdDir(), `${sanitize(cmdId)}.res.json`), { id: cmdId, result });
  } catch (e) {
    log(`写结果失败 ${cmdId}：${e && e.message || e}`);
  }
}

// ---------- 三个 action ----------

function createCursorContext() {
  const ctx = {};
  const mentions = {};
  for (const key of CONTEXT_KEYS) {
    const isList = LIST_KEYS.has(key);
    ctx[key] = isList ? [] : undefined;
    mentions[key] = isList ? {} : [];
  }
  ctx.fileSelections = [];
  ctx.mentions = mentions;
  return ctx;
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function orderedComposerIds() {
  const cmd = String(cfg().get('orderedIdsCommand') || 'composer.getOrderedSelectedComposerIds');
  const ids = await vscode.commands.executeCommand(cmd);
  return Array.isArray(ids) ? ids : null;
}

async function waitMounted(composerId) {
  for (let i = 0; i < MOUNT_TRIES; i++) {
    try {
      const ids = await orderedComposerIds();
      if (ids && ids.includes(composerId)) return true;
    } catch {
      return false;                                 // 命令都没有，别白等 3 秒
    }
    await sleep(MOUNT_STEP_MS);
  }
  return false;
}

function asModelConfig(item) {
  const raw = item && item.modelConfig;
  if (raw && typeof raw === 'object' && raw.modelName) return raw;
  const model = String(item && (item.model || item.modelName) || '').trim();
  if (!model) return null;
  const maxMode = !!(item && (item.maxMode || item.max));
  const parameters = Array.isArray(item && item.parameters) ? item.parameters : [];
  const cfg = { modelName: model, maxMode };
  if (model !== 'default') cfg.selectedModels = [{ modelId: model, parameters }];
  return cfg;
}

async function createOne(item, autoSubmit) {
  const cmd = String(cfg().get('createCommand') || 'composer.createNew');
  const composerId = String(item.composerId || crypto.randomUUID());
  const name = String(item.name || '').replace(/\s+/g, ' ').trim().slice(0, 60);
  const prompt = String(item.prompt || '');
  const partialState = {
    composerId,
    name,
    text: prompt,
    richText: prompt,
    hasChangedContext: true,
    context: createCursorContext(),
  };
  const modelConfig = asModelConfig(item || {});
  if (modelConfig) partialState.modelConfig = modelConfig;
  const run = Promise.resolve().then(() => vscode.commands.executeCommand(cmd, {
    openInNewTab: true,
    autoSubmit: autoSubmit && !!prompt,
    dontRefreshReactiveContext: true,
    partialState,
  }));
  // 只等 CREATE_ACK_MS：拿得到的错误这段时间内一定 reject；没消息 = 对话已经开出去在跑，
  // 后面用 waitMounted（getOrderedSelectedComposerIds 里有没有这个 id）当真正的「开成了」。
  const settled = await Promise.race([
    run.then(() => 'done'),
    sleep(CREATE_ACK_MS).then(() => 'pending'),
  ]);
  if (settled === 'pending') {
    run.catch(e => log(`createNew ${composerId} 后台报错：${e && e.message || e}`));
  }
  return composerId;
}

async function batchOpen(payload) {
  const items = Array.isArray(payload && payload.items) ? payload.items : [];
  const autoSubmit = payload && payload.autoSubmit === false ? false : true;
  const skipMountWait = !!(payload && payload.skipMountWait);
  const delayMs = Math.max(0, Math.min(120, Number(payload && payload.launchDelayMs) || 0));
  if (!items.length) return { ok: false, error: 'items 为空' };
  if (items.length > 12) return { ok: false, error: '一次最多开 12 个' };

  const results = await Promise.all(items.map(async (item, i) => {
    if (i > 0 && delayMs > 0) await sleep(i * delayMs);
    try {
      const composerId = await createOne(item || {}, autoSubmit);
      return { ok: true, composerId, name: String(item && item.name || '') };
    } catch (e) {
      return { ok: false, composerId: String(item && item.composerId || ''), error: String(e && e.message || e) };
    }
  }));
  const created = results.filter(r => r.ok);
  let verified = 0;
  if (!skipMountWait && created.length) {
    const flags = await Promise.all(created.map(r => waitMounted(r.composerId)));
    flags.forEach((f, i) => { created[i].mounted = f; if (f) verified++; });
  } else {
    verified = created.length;
  }
  stats.created += created.length;
  log(`batchOpen ×${items.length}：创建 ${created.length} · 挂载确认 ${verified}`);
  return { ok: created.length > 0, created: created.length, verified, results,
           error: created.length ? undefined : (results[0] && results[0].error) || 'createNew 全部失败' };
}

async function openCursor(payload) {
  const composerId = String(payload && payload.composerId || '').trim();
  if (!composerId) return { ok: false, error: '缺 composerId' };
  const cmds = cfg().get('openCommands');
  const list = Array.isArray(cmds) && cmds.length ? cmds : ['aichat.openAgentById', 'composer.openComposer'];
  const errors = [];
  for (const cmd of list) {
    try {
      await vscode.commands.executeCommand(String(cmd), composerId);
      log(`openCursor ${composerId} via ${cmd}`);
      return { ok: true, via: String(cmd) };
    } catch (e) {
      errors.push(`${cmd}: ${e && e.message || e}`);
    }
  }
  return { ok: false, error: errors.join(' | ') };
}

// 控制台关会话 → 顺手把 Cursor 侧栏里那个对话 tab 也收掉（Cursor 的「Close Tab」，进历史不删）。
// Cursor 没有暴露删除/归档对话的命令（deleteComposer 只在 composerService 内部），
// closeComposerTab 是唯一能拿 composerId 当参数的公开命令。
async function closeComposer(payload) {
  const composerId = String(payload && payload.composerId || '').trim();
  if (!composerId) return { ok: false, error: '缺 composerId' };
  const cmds = cfg().get('closeCommands');
  const list = Array.isArray(cmds) && cmds.length ? cmds : ['composer.closeComposerTab'];
  const errors = [];
  for (const cmd of list) {
    try {
      await vscode.commands.executeCommand(String(cmd), composerId);
      log(`closeComposer ${composerId} via ${cmd}`);
      return { ok: true, via: String(cmd) };
    } catch (e) {
      errors.push(`${cmd}: ${e && e.message || e}`);
    }
  }
  return { ok: false, error: errors.join(' | ') };
}

async function ping() {
  let ids = null;
  let commandsOk = true;
  try { ids = await orderedComposerIds(); } catch { commandsOk = false; }
  return {
    ok: true,
    instanceId,
    label: instanceLabel(),
    workspace: workspacePath(),
    version: vscode.version,
    pid: process.pid,
    commandsOk,
    composerCount: Array.isArray(ids) ? ids.length : null,
    stats,
  };
}

async function handle(req) {
  const action = String(req.action || '');
  if (action === 'ping') return ping();
  if (action === 'batchOpen') return batchOpen(req.payload || {});
  if (action === 'openCursor') return openCursor(req.payload || {});
  if (action === 'closeComposer') return closeComposer(req.payload || {});
  if (action === 'listComposers') {
    try { return { ok: true, ids: await orderedComposerIds() }; } catch (e) { return { ok: false, error: String(e && e.message || e) }; }
  }
  return { ok: false, error: `未知命令：${action}` };
}

async function runOne(req) {
  let result;
  try {
    result = await Promise.race([
      handle(req),
      sleep(CMD_HARD_MS).then(() => ({
        ok: false, code: 'STUCK',
        error: `命令 ${req.action} ${CMD_HARD_MS / 1000}s 没做完，已放行`,
      })),
    ]);
    if (result && result.ok) stats.handled++; else stats.failed++;
  } catch (e) {
    result = { ok: false, error: String(e && e.message || e) };
    stats.failed++;
  }
  stats.lastAction = String(req.action || '');
  stats.lastAt = Date.now();
  writeResult(req.id, result);
  log(`受理 ${req.action}（来自 ${req.from || '?'}）→ ${result && result.ok ? 'ok' : ('失败：' + (result && result.error))}`);
}

function pump() {
  // 取件是同步的；每条命令各自异步跑，谁卡住都挡不住后面的 ping / 下一批 createNew。
  // 老写法是 for…await 串行 + pumping 互斥：一条 createNew 不 resolve，整个窗口就再也不接活。
  for (const req of takeCommands()) void runOne(req);
}

function restartPump() {
  if (pumpTimer) clearInterval(pumpTimer);
  const every = Math.max(200, Number(cfg().get('pumpIntervalMs')) || 1000);
  pumpTimer = setInterval(() => void pump(), every);
}

// ---------- 生命周期 ----------

function activate(context) {
  output = vscode.window.createOutputChannel('rxyy MCP 窗口总线');
  context.subscriptions.push(output);
  instanceId = stableInstanceId(context);
  registerInstance();
  registerTimer = setInterval(registerInstance, REGISTER_EVERY_MS);
  restartPump();
  void pump();
  context.subscriptions.push(
    vscode.commands.registerCommand('chijiu.extbus.status', async () => {
      const p = await ping();
      const msg = `窗口 ${p.label} · instanceId ${instanceId} · 总线 ${busRoot()} · Cursor 私有命令 ${p.commandsOk ? '可用' : '不可用'} · 已受理 ${stats.handled} · 开过 ${stats.created} 个对话`;
      log(msg);
      output.show(true);
      vscode.window.showInformationMessage(msg);
    }),
    vscode.commands.registerCommand('chijiu.extbus.pumpNow', () => void pump()),
    vscode.workspace.onDidChangeConfiguration(e => {
      if (e.affectsConfiguration('chijiu.extbus')) { restartPump(); registerInstance(); }
    }),
    { dispose: () => { clearInterval(registerTimer); clearInterval(pumpTimer); unregisterInstance(); } },
  );
  log(`已登记：instanceId=${instanceId} label=${instanceLabel()} root=${busRoot()}`);
}

function deactivate() {
  clearInterval(registerTimer);
  clearInterval(pumpTimer);
  unregisterInstance();
}

module.exports = { activate, deactivate };
