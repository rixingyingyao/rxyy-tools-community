/* rxyy MCP workbench 桥客户端。外链进 workbench.html，软失败。
   服务由主包注入的 IIFE 挂到 globalThis.__chijiuComposer / __chijiuChat。 */
(function () {
  const HUB = "http://127.0.0.1:__RXYY_MCP_HUB__";
  const TO = { headers: { "Content-Type": "application/json" } };

  function data() {
    try { return globalThis.__chijiuComposer || null; } catch (e) { return null; }
  }
  function chat() {
    try { return globalThis.__chijiuChat || null; } catch (e) { return null; }
  }

  function handleOf(svc, id) {
    if (!svc || !id) return null;
    try {
      if (typeof svc.getHandleIfLoaded === "function") {
        const h = svc.getHandleIfLoaded(id);
        if (h && typeof h.then !== "function") return h;
      }
    } catch (e) { /* ignore */ }
    try {
      if (typeof svc.getComposerHandleById === "function") {
        const h = svc.getComposerHandleById(id);
        if (h && typeof h.then !== "function") return h;
      }
    } catch (e) { /* ignore */ }
    try {
      const loaded = svc.loadedComposers;
      if (loaded && typeof loaded.getById === "function") {
        const h = loaded.getById(id);
        if (h && typeof h.then !== "function") return h;
      }
    } catch (e) { /* ignore */ }
    return null;
  }

  function rename(composerId, name) {
    const svc = data();
    if (!svc) return { ok: false, error: "桥未挂上：还没等到 composerDataService" };
    const id = String(composerId || "").trim();
    const nm = String(name || "").trim();
    if (!id || !nm) return { ok: false, error: "缺 composerId 或名字" };
    const h = handleOf(svc, id);
    if (!h) return { ok: false, error: "这个对话没在内存里（可能没打开）" };
    try {
      if (typeof svc.updateComposerData === "function") {
        svc.updateComposerData(h, { name: nm });
        return { ok: true, via: "updateComposerData" };
      }
      if (typeof svc.updateComposerDataSetStore === "function") {
        svc.updateComposerDataSetStore(h, function (set) { set("name", nm); });
        return { ok: true, via: "updateComposerDataSetStore" };
      }
    } catch (e) {
      return { ok: false, error: String(e && e.message || e) };
    }
    return { ok: false, error: "服务上没有改名方法" };
  }

  function parseArgs(x) {
    if (!x) return {};
    if (typeof x === "object") return x;
    try { return JSON.parse(x); } catch (e) { return {}; }
  }

  function toolStatus(tfd) {
    const st = String((tfd && tfd.status) || "").toLowerCase();
    const add = (tfd && tfd.additionalData) || {};
    if (st === "error") return "error";
    if (st === "loading" || st === "running" || st === "pending" || st === "in_progress" || !st) return "running";
    if (add.status === "error") return "error";
    if (add.status === "cancelled") return "cancelled";
    if (st === "completed" || st === "success" || st === "done") return "done";
    return st || "running";
  }

  function whyOf(raw, params) {
    const mcpArgs = raw && raw.args && typeof raw.args === "object" ? raw.args : {};
    const mcpDetails = mcpArgs.mcpDetails && typeof mcpArgs.mcpDetails === "object" ? mcpArgs.mcpDetails : {};
    const srcs = [raw, params, mcpDetails, mcpArgs];
    const keys = ["explanation", "commandDescription", "description", "instructions", "reason", "purpose"];
    for (let i = 0; i < srcs.length; i++) {
      const src = srcs[i];
      if (!src || typeof src !== "object") continue;
      for (let k = 0; k < keys.length; k++) {
        const v = src[keys[k]];
        if (typeof v === "string" && v.trim()) return v.trim().slice(0, 240);
      }
    }
    return "";
  }

  function resultPreview(tfd) {
    if (!tfd) return "";
    if (tfd.error) return String(tfd.error).slice(0, 600);
    const res = parseArgs(tfd.result);
    if (res && typeof res === "object") {
      if (res.output != null) return String(res.output).slice(0, 600);
      if (res.stdout != null) return String(res.stdout).slice(0, 600);
    }
    return "";
  }

  function bubblesOf(svc, h, payload) {
    if (typeof svc.getLastAiBubbles === "function") {
      try {
        const rows = svc.getLastAiBubbles(h) || [];
        if (rows && rows.length) return rows;
      } catch (e) { /* ignore */ }
    }
    if (typeof svc.getLoadedConversation === "function") {
      try {
        const rows = svc.getLoadedConversation(h) || [];
        if (rows && rows.length) return rows;
      } catch (e) { /* ignore */ }
    }
    const map = payload.conversationMap || {};
    const headers = payload.fullConversationHeadersOnly || [];
    const out = [];
    let lastHuman = -1;
    for (let i = 0; i < headers.length; i++) {
      if (headers[i] && (headers[i].type === 1 || headers[i].type === "1")) lastHuman = i;
    }
    const slice = headers.slice(lastHuman + 1);
    for (let i = 0; i < slice.length; i++) {
      const id = slice[i] && slice[i].bubbleId;
      const b = id && map[id];
      if (b) out.push(b);
    }
    return out;
  }

  function stepFromBubble(b) {
    if (!b || b.type === 1 || b.type === "1") return null;
    const bid = String(b.bubbleId || "");
    const tfd = b.toolFormerData;
    if (tfd && (tfd.name || tfd.tool != null)) {
      const raw = parseArgs(tfd.rawArgs);
      const params = parseArgs(tfd.params);
      return {
        kind: "tool",
        id: bid,
        bubbleId: bid,
        name: String(tfd.name || ("tool#" + tfd.tool)),
        status: toolStatus(tfd),
        why: whyOf(raw, params),
        summary: String(raw.command || params.command || raw.path || raw.file_path || raw.search_term || raw.name || "").slice(0, 200),
        result: resultPreview(tfd),
      };
    }
    let th = b.thinking;
    if (typeof th === "string" && th.trim().charAt(0) === "{") {
      try { th = JSON.parse(th); } catch (e) { /* keep */ }
    }
    let thinkText = "";
    if (th && typeof th === "object" && th.text) thinkText = String(th.text);
    else if (typeof th === "string") thinkText = th;
    if (thinkText || b.capabilityType === 30 || b.thinkingStyle != null) {
      return { kind: "thinking", id: bid, bubbleId: bid, text: String(thinkText || "").slice(0, 3000) };
    }
    if (typeof b.text === "string" && b.text.trim() && (b.type === 2 || b.type === "2")) {
      return { kind: "text", id: bid, bubbleId: bid, text: b.text.slice(0, 4000) };
    }
    return null;
  }

  function turnSnap(composerId) {
    const svc = data();
    if (!svc) return { ok: false, error: "桥未挂上" };
    const id = String(composerId || "").trim();
    const h = handleOf(svc, id);
    if (!h) return { ok: false, error: "对话不在内存" };
    let payload = {};
    try { payload = (typeof svc.getComposerData === "function" ? svc.getComposerData(h) : (h.data || {})) || {}; }
    catch (e) { payload = h.data || {}; }
    const headers = payload.fullConversationHeadersOnly || [];
    let reply = "";
    let thinking = "";
    const steps = [];
    try {
      const conv = bubblesOf(svc, h, payload);
      for (let i = 0; i < conv.length; i++) {
        const st = stepFromBubble(conv[i]);
        if (!st) continue;
        steps.push(st);
        if (st.kind === "thinking" && st.text) thinking = st.text;
        if (st.kind === "text" && st.text) reply = st.text;
      }
    } catch (e) { /* ignore */ }
    const gen = payload.generatingBubbleIds;
    return {
      ok: true,
      live: !!(gen && (Array.isArray(gen) ? gen.length : Object.keys(gen).length)),
      name: payload.name || "",
      steps: steps.slice(-80),
      stepCount: headers.length,
      reply: String(reply || "").slice(0, 12000),
      thinking: String(thinking || "").slice(0, 4000),
      at: Date.now(),
    };
  }

  function answerAsk(composerId, answers) {
    const c = chat();
    const svc = data();
    if (!c && !svc) return { ok: false, error: "桥未挂上" };
    const id = String(composerId || "").trim();
    const names = [
      "submitAskQuestion", "answerAskQuestion", "submitComposerAsk",
      "submitToolResult", "acceptAskQuestion",
    ];
    for (const n of names) {
      try {
        if (c && typeof c[n] === "function") {
          const r = c[n](id, answers);
          return { ok: true, via: "chat." + n, result: r == null ? null : String(r) };
        }
        if (svc && typeof svc[n] === "function") {
          const r = svc[n](id, answers);
          return { ok: true, via: "data." + n, result: r == null ? null : String(r) };
        }
      } catch (e) {
        return { ok: false, error: n + ": " + (e && e.message || e) };
      }
    }
    return { ok: false, error: "这个版本没有作答方法，请回 Cursor 里点" };
  }

  function post(path, body) {
    return fetch(HUB + path, Object.assign({ method: "POST", body: JSON.stringify(body || []) }, TO))
      .then(function (r) { return r.json().catch(function () { return {}; }); })
      .catch(function () { return null; });
  }

  let lastBeat = 0;
  function beat() {
    const now = Date.now();
    if (now - lastBeat < 2000) return;
    lastBeat = now;
    post("/api/wbhook_beat", [{
      ready: !!data(),
      chat: !!chat(),
      at: now,
    }]);
  }

  function ingestWatched(ids) {
    const snaps = [];
    (ids || []).forEach(function (id) {
      const s = turnSnap(id);
      if (s && s.ok) snaps.push(Object.assign({ composerId: id }, s));
    });
    if (snaps.length) post("/api/wbhook_ingest", [snaps]);
  }

  function commandSvc() {
    try {
      if (globalThis.__chijiuCommand) return globalThis.__chijiuCommand;
      const s = data();
      return (s && s._commandService) || null;
    } catch (e) { return null; }
  }

  function reloadWindow() {
    const c = commandSvc();
    if (c && typeof c.executeCommand === "function") {
      try {
        c.executeCommand("workbench.action.reloadWindow");
        return { ok: true, via: "commandService" };
      } catch (e) {
        return { ok: false, error: String(e && e.message || e) };
      }
    }
    return { ok: false, error: "没有 commandService" };
  }

  function runCmd(cmd) {
    const a = String(cmd.action || "");
    if (a === "rename") return rename(cmd.composerId, cmd.name);
    if (a === "turn") return turnSnap(cmd.composerId);
    if (a === "answer") return answerAsk(cmd.composerId, cmd.answers || {});
    if (a === "reload") return reloadWindow();
    return { ok: false, error: "未知命令 " + a };
  }

  function poll() {
    beat();
    post("/api/wbhook_poll", []).then(function (r) {
      if (!r) return;
      ingestWatched(r.watch || []);
      const cmds = r.cmds || [];
      cmds.forEach(function (cmd) {
        const result = runCmd(cmd);
        post("/api/wbhook_ack", [cmd.id, result]);
      });
    });
  }

  globalThis.__chijiuBridge = { rename, turnSnap, answerAsk, ready: function () { return !!data(); } };
  post("/api/wbhook_beat", [{ ready: !!data(), chat: !!chat(), boot: true }]);
  setInterval(poll, 250);
  setTimeout(poll, 800);
})();
