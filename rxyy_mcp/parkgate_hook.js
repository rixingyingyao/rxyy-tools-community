// chijiu-parkgate hook v4 —— 部署目标: ~/.salak/hook.js（由 rxyy MCP parkgate.py 安装/更新）
// 相对 v3 关键修复：Cursor EH 用 process.getBuiltinModule("node:http2")，
// require("http2") 拿到的是另一份模块，包它等于没拦。v4 改包 BuiltinModule，
// 并同步补 ClientHttp2Session.prototype.request，覆盖 hook 加载前已建好的 Agent 长连。
// 协议不变：park.json {armed,launchEpoch,cancelEpoch} + parked/<pid>.json 计数。
(function () {
  "use strict";
  var http2, fs, os, path, C;
  try {
    http2 = process.getBuiltinModule("node:http2");
    fs = process.getBuiltinModule("node:fs");
    os = process.getBuiltinModule("node:os");
    path = process.getBuiltinModule("node:path");
    C = http2.constants || {};
  } catch (e) {
    try {
      http2 = require("http2"); fs = require("fs"); os = require("os"); path = require("path");
      C = http2.constants || {};
    } catch (e2) { return; }
  }

  var HOOK_VERSION = "v4-chijiu-2";
  var MARK = "__chijiu_parkgate";
  var HOST_TAG = "__chijiu_parkgate_host";
  var DIR = path.join(os.homedir(), ".salak");
  var PARK_JSON = path.join(DIR, "park.json");
  var PARKED_DIR = path.join(DIR, "parked");
  var LOG_FILE = path.join(DIR, "hook.log");
  var HOST_RE = /(^|\.)cursor\.(sh|com)$/i;
  var RUN_RE = /^\/agent\.v\d+\.AgentService\/Run$/;

  var pending = [];
  var pollTimer = null;
  var protoHooked = false;
  var parkCache = { mtimeMs: -1, val: null, ts: 0 };

  function log(msg) {
    try {
      try {
        var st = fs.statSync(LOG_FILE);
        if (st.size > 2 * 1024 * 1024) {
          try { fs.unlinkSync(LOG_FILE + ".1"); } catch (_) {}
          try { fs.renameSync(LOG_FILE, LOG_FILE + ".1"); } catch (_) {}
        }
      } catch (_) {
        try { fs.mkdirSync(DIR, { recursive: true }); } catch (_) {}
      }
      fs.appendFileSync(LOG_FILE,
        new Date().toISOString() + " [pid=" + process.pid + " role=" +
        (process.env.CURSOR_EXTENSION_HOST_ROLE || "") + "] " + msg + "\n");
    } catch (_) {}
  }

  function readPark() {
    try {
      var st = fs.statSync(PARK_JSON);
      var now = Date.now();
      if (parkCache.val && st.mtimeMs === parkCache.mtimeMs && now - parkCache.ts < 150) {
        return parkCache.val;
      }
      var j = JSON.parse(fs.readFileSync(PARK_JSON, "utf8").replace(/^\uFEFF/, ""));
      parkCache = { mtimeMs: st.mtimeMs, val: j, ts: now };
      return j;
    } catch (_) { return null; }
  }

  function updateCountFile() {
    try {
      fs.mkdirSync(PARKED_DIR, { recursive: true });
      var f = path.join(PARKED_DIR, process.pid + ".json");
      if (pending.length === 0) {
        try { fs.unlinkSync(f); } catch (_) {}
        return;
      }
      fs.writeFileSync(f, JSON.stringify({ pid: process.pid, count: pending.length, ts: Date.now() }));
    } catch (_) {}
  }

  function removePending(e) {
    var i = pending.indexOf(e);
    if (i >= 0) pending.splice(i, 1);
  }

  function stopPollIfIdle() {
    if (pending.length === 0 && pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function ensurePoll() {
    if (pollTimer) return;
    pollTimer = setInterval(pollSignals, 200);
    if (pollTimer.unref) pollTimer.unref();
  }

  function pollSignals() {
    try {
      if (pending.length === 0) { stopPollIfIdle(); return; }
      var park = readPark();
      if (!park) return;
      var le = park.launchEpoch || 0, ce = park.cancelEpoch || 0;
      var slice = pending.slice();
      for (var i = 0; i < slice.length; i++) {
        var e = slice[i];
        if (le > e.launchEpoch) { removePending(e); e.release(); }
        else if (ce > e.cancelEpoch) { removePending(e); e.drop(); }
      }
      updateCountFile();
      stopPollIfIdle();
    } catch (_) {}
  }

  function holdStream(stream, park) {
    var buf = [];
    var ended = false, released = false, dropping = false;
    var origWrite = stream.write.bind(stream);
    var origEnd = stream.end.bind(stream);

    stream.write = function (chunk, enc, cb) {
      if (dropping) { if (typeof cb === "function") { try { cb(); } catch (_) {} } return true; }
      if (released) return origWrite(chunk, enc, cb);
      if (typeof enc === "function") { cb = enc; enc = undefined; }
      if (chunk != null) buf.push({ c: chunk, e: enc });
      if (typeof cb === "function") { try { cb(); } catch (_) {} }
      return true;
    };
    stream.end = function (chunk, enc, cb) {
      // 取消途中要吞掉这一次：Node 的 stream.close() 内部会先替我们调一次 end()，
      // 那时 released 已置位，若放行就等于把一个空 body 的 /Run 当正常请求送了出去
      // （沙箱实测：服务端收到完整请求并回了 200，rst 根本没送到）。
      if (dropping) { if (typeof cb === "function") { try { cb(); } catch (_) {} } return stream; }
      if (released) return origEnd(chunk, enc, cb);
      if (typeof chunk === "function") { cb = chunk; chunk = undefined; enc = undefined; }
      else if (typeof enc === "function") { cb = enc; enc = undefined; }
      if (chunk != null) buf.push({ c: chunk, e: enc });
      ended = true;
      if (typeof cb === "function") { try { cb(); } catch (_) {} }
      return stream;
    };

    var entry = {
      launchEpoch: park.launchEpoch || 0,
      cancelEpoch: park.cancelEpoch || 0,
      release: function () {
        if (released) return;
        released = true;
        stream.write = origWrite;
        stream.end = origEnd;
        for (var i = 0; i < buf.length; i++) origWrite(buf[i].c, buf[i].e);
        if (ended) origEnd();
        buf.length = 0;
        log("release: body replayed");
      },
      drop: function () {
        if (released) return;
        released = true;
        dropping = true;
        try { stream.close(0x8); }
        catch (_) { try { stream.destroy(); } catch (_) {} }
        buf.length = 0;
        log("drop: stream cancelled");
      },
    };

    var cleanup = function () { removePending(entry); updateCountFile(); stopPollIfIdle(); };
    stream.on("close", cleanup);
    stream.on("error", cleanup);
    pending.push(entry);
    updateCountFile();
    ensurePoll();
  }

  function normalizePath(raw) {
    if (typeof raw === "string") return raw;
    if (raw == null) return "";
    if (typeof Buffer !== "undefined" && Buffer.isBuffer(raw)) return raw.toString("utf8");
    if (Array.isArray(raw) && raw.length) return normalizePath(raw[0]);
    return "";
  }

  function pathFromHeaders(headers) {
    if (!headers || typeof headers !== "object") return "";
    var raw = headers[C.HTTP2_HEADER_PATH || ":path"];
    if (raw == null) raw = headers[":path"];
    if (raw == null) raw = headers["path"];
    if (raw == null) {
      try {
        var keys = Object.keys(headers);
        for (var i = 0; i < keys.length; i++) {
          if (keys[i] === ":path" || /:path$/i.test(keys[i])) { raw = headers[keys[i]]; break; }
        }
      } catch (_) {}
    }
    return normalizePath(raw);
  }

  function isRunPath(p) {
    if (!p) return false;
    var bare = String(p).split("?")[0];
    return RUN_RE.test(bare) || bare.indexOf("/agent.v1.AgentService/Run") === 0;
  }

  function isHookedRequest(fn) {
    try { return !!(fn && fn[MARK]); } catch (_) { return false; }
  }

  function hostOf(a) {
    try {
      if (typeof a === "string") return new URL(a).hostname;
      if (a && a.hostname) return a.hostname;
      if (a && a.host) return new URL("https://" + a.host).hostname;
    } catch (_) {}
    return String(a || "");
  }

  function sessionHost(sess) {
    try {
      if (sess && sess[HOST_TAG]) return sess[HOST_TAG];
      if (sess && sess.authority) return new URL("https://" + sess.authority).hostname;
      if (sess && sess.origin) return new URL(sess.origin).hostname;
      if (sess) {
        var syms = Object.getOwnPropertySymbols(sess);
        for (var i = 0; i < syms.length; i++) {
          var key = String(syms[i]);
          if (!/authority|origin/i.test(key)) continue;
          var val = sess[syms[i]];
          if (typeof val === "string" && val) return hostOf(val.indexOf("://") >= 0 ? val : "https://" + val);
        }
      }
      var sock = sess && sess.socket;
      var sn = sock && (sock.servername || sock._host);
      if (sn) return String(sn);
    } catch (_) {}
    return "";
  }

  function parkRequest(protoOrig) {
    function hookedRequest(headers, reqOptions) {
      var self = this;
      try {
        var p = pathFromHeaders(headers);
        if (!isRunPath(p)) return protoOrig.apply(self, arguments);
        var sessHost = sessionHost(self);
        // Unknown hosts fail closed: do not infer Cursor traffic from path alone.
        if (!sessHost || !HOST_RE.test(sessHost)) return protoOrig.apply(self, arguments);
        var park = readPark();
        // 铁律：只有用户在控制台点了「开启」(armed=true) 才扣住；
        // 没点开启 = 完全透传，绝不能影响正常 Claude / 加速通道。
        if (!(park && park.armed)) return protoOrig.apply(self, arguments);
        var realStream = protoOrig.apply(self, arguments);
        try { holdStream(realStream, park); log("parked /Run host=" + sessHost); }
        catch (_) {}
        return realStream;
      } catch (_) {
        return protoOrig.apply(self, arguments);
      }
    }
    hookedRequest[MARK] = 1;
    return hookedRequest;
  }

  function clientSessionProto(sample) {
    if (sample) {
      try { return Object.getPrototypeOf(sample) || null; } catch (_) {}
    }
    var probe = null;
    try {
      probe = origConnect.call(http2, "http://127.0.0.1:1");
      probe.on("error", function () {});
      var proto = Object.getPrototypeOf(probe) || null;
      try { probe.destroy(); } catch (_) {}
      return proto;
    } catch (e) {
      try { if (probe) probe.destroy(); } catch (_) {}
      return null;
    }
  }

  function ensureProtoHook(sample) {
    if (protoHooked) return true;
    var proto = clientSessionProto(sample);
    if (!proto || typeof proto.request !== "function") return false;
    if (isHookedRequest(proto.request)) { protoHooked = true; return true; }
    proto.request = parkRequest(proto.request);
    protoHooked = true;
    log("原型补丁已挂上（新旧连接通吃）");
    return true;
  }

  var origConnect = http2.connect;
  function parkConnect(authority) {
    var session = origConnect.apply(this, arguments);
    try {
      session[HOST_TAG] = hostOf(authority);
    } catch (_) { return session; }
    return session;
  }
  parkConnect[MARK] = 1;

  if (!(http2.connect && http2.connect[MARK])) {
    http2.connect = parkConnect;
  }

  // 同步补原型，覆盖 hook 加载前已创建的 session；pending 自带轮询只负责信号。
  ensureProtoHook();

  log("installed chijiu-parkgate hook " + HOOK_VERSION + " (default OFF, arm manually)");
})();
