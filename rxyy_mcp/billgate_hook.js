// chijiu-billgate hook v2 —— 部署目标: ~/.rxyy-billgate/billhook.js（由 rxyy MCP billgate.py 安装/更新）
//
// v1 → v2 的要害修复：v1 靠 process._getActiveHandles() 去捞「hook 加载前就已经建好」的
// Http2Session。实测（node v22.12）那张 handle 表里只有 Socket / TLSSocket，一个
// Http2Session 都捞不到，socket 上也不留 _http2Session 反向引用 —— 那条路恒返回 0，
// 从上线起就是空转。而账单/额度接口恰恰跑在开机就建好、几小时不换的长连上，于是
// 「已扣 0 次」：不是没拦住，是压根没挂上去。
//
// v2 改补 ClientHttp2Session.prototype.request：所有客户端 session（含热装前建好的）
// 共用同一个原型对象，补一次就等于补到每一条，新旧连接通吃。http2.connect 仍然包，
// 但只用来给 session 贴一个 authority 标签（原型补丁那边拿不到连接目标，Node 没把
// authority 暴露成公开属性），不再在 connect 层面替换 session.request。
//
// 协议：读 ~/.rxyy-billgate/bill.json {enabled, mode}；enabled=false 时完全透传，对 Cursor 零影响。
//   observe（默认）：看到就记一笔到 ~/.rxyy-billgate/bill-seen/<pid>.json，请求照常放行；
//   block        ：直接 RST 掉这些 Dashboard 账单请求（谨慎，会让相关面板/设置失败）；
//   debug        ：在 observe 的基础上，把本进程发出的每条 h2 路径都记进 billhook.log，
//                  带 count 和 delta_ms，用来看清账单接口到底走不走扩展宿主、频率是多少。
//
// Agent 的生成请求走 agent.v1.AgentService 的 Connect/WS/SSE 链路，服务端在推理过程中
// 计量；客户端没有另一条可单独阻断的「扣费 RPC」。这里故意不碰 Agent/Composer/Chat，
// 否则只能让会话失败，既不能绕过计费，也不能恢复已经用完的额度。
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

  var HOOK_VERSION = "v2-chijiu-4";
  var MARK = "__chijiu_billgate";
  var HOST_TAG = "__chijiu_billgate_host";
  var DIR = path.join(os.homedir(), ".rxyy-billgate");
  var BILL_JSON = path.join(DIR, "bill.json");
  var SEEN_DIR = path.join(DIR, "bill-seen");
  var LOG_FILE = path.join(DIR, "billhook.log");
  var HOST_RE = /(^|\.)cursor\.(sh|com)$/i;
  // Cursor 3.15.6 包内 protobuf 的真实服务名是 aiserver.v1.DashboardService。
  // 服务名和方法都收窄，避免把别的 Service 里同名的 Run/Create/Set 误当成账单。
  var BILL_RE = new RegExp("^/aiserver\\.v1\\.DashboardService/(" +
    "GetHardLimit|SetHardLimit|GetSpendLimitPolicy|SetSpendLimitPolicy|" +
    "GetOrgDailySpendByCategory|EnableOnDemandSpend|GetMonthlyInvoice|ListInvoiceCycles|" +
    "GetDailySpendByCategory|GetTeamHasValidPaymentMethod|GetUsageBasedPremiumRequests|" +
    "SetUsageBasedPremiumRequests|GetClientUsageData|GetCurrentPeriodUsage|" +
    "GetUsageLimitPolicyStatus|GetUsageLimitStatusAndActiveGrants|GetCreditGrantsBalance|" +
    "GetClientVisibleCreditGrants|GetTokenUsage|GetTeamSpend|GetCurrentBillingCycle|" +
    "GetMonthlyBillingCycle|GetFilteredUsageEvents|GetAggregatedUsageEvents|ListInvoices|" +
    "ListBlockingCheckoutInvoices|GetServiceAccountSpendLimit|SetServiceAccountSpendLimit|" +
    "SetUserHardLimit|SetUserMonthlyLimit|IsAllowedFreeTrialUsage|CheckUsageBasedPrice" +
    ")(\\?|$)", "i");

  var seen = 0;
  var billCache = { mtimeMs: -1, val: null, ts: 0 };
  var protoHooked = false;
  var debugStats = Object.create(null);
  var pollTimer = null;

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

  function readBill() {
    try {
      var st = fs.statSync(BILL_JSON);
      var now = Date.now();
      if (billCache.val && st.mtimeMs === billCache.mtimeMs && now - billCache.ts < 150) {
        return billCache.val;
      }
      var j = JSON.parse(fs.readFileSync(BILL_JSON, "utf8").replace(/^\uFEFF/, ""));
      billCache = { mtimeMs: st.mtimeMs, val: j, ts: now };
      return j;
    } catch (_) { return null; }
  }

  function bumpSeen() {
    seen++;
    try {
      fs.mkdirSync(SEEN_DIR, { recursive: true });
      fs.writeFileSync(path.join(SEEN_DIR, process.pid + ".json"),
        JSON.stringify({ pid: process.pid, count: seen, ts: Date.now() }));
    } catch (_) {}
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

  function isBillPath(p) {
    if (!p) return false;
    return BILL_RE.test(String(p));
  }

  function isHooked(fn) {
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

  /**
   * 认出一条 session 连的是谁。
   *
   * Node 不把连接目标暴露成公开属性（authority / origin 在 ClientHttp2Session 上通常是
   * undefined，只存在内部 Symbol 上），所以首选我们在 connect 时自己贴的标签；
   * 热装前就建好、没有标签的老连接，退到 socket 上取 TLS servername —— 真实 cursor 流量
   * 走的正是这条。都拿不到就返回空串，调用方按「认不出」处理（只靠路径匹配兜底）。
   */
  function sessionHost(sess) {
    try {
      if (sess && sess[HOST_TAG]) return sess[HOST_TAG];
      if (sess && sess.authority) return new URL("https://" + sess.authority).hostname;
      if (sess && sess.origin) return new URL(sess.origin).hostname;
      // Node stores authority/origin on private symbols; use the descriptive symbol
      // name so sessions created before this hook was loaded remain identifiable.
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

  function debugLog(bare, host) {
    var key = host + " " + bare;
    var now = Date.now();
    var st = debugStats[key] || { count: 0, last: 0 };
    var delta = st.last ? now - st.last : 0;
    st.count++;
    st.last = now;
    debugStats[key] = st;
    log("PATH " + (bare || "(空)") + " host=" + (host || "(认不出)") +
      " count=" + st.count + " delta_ms=" + delta);
  }

  /**
   * 拿 ClientHttp2Session.prototype。
   *
   * 优先从手上已有的 session 取；一个都没有时，用一次注定连不通的 h2c connect 换原型 ——
   * connect 同步返回 session 对象、不等握手，拿到原型立刻拆掉。http 与 https 的 session
   * 是同一个类（区别只在底下那个 socket 是 net 还是 tls），所以拿 h2c 的原型补上去，
   * 真实的 TLS 连接一样会走到。
   */
  function clientSessionProto(sample) {
    if (sample) {
      try { return Object.getPrototypeOf(sample) || null; } catch (_) {}
    }
    var probe = null;
    try {
      probe = origConnect.call(http2, "http://127.0.0.1:1");
      // 不挂 error handler 就是 unhandled error，会掀掉整个扩展宿主进程。
      probe.on("error", function () {});
      var proto = Object.getPrototypeOf(probe) || null;
      try { probe.destroy(); } catch (_) {}
      return proto;
    } catch (e) {
      try { if (probe) probe.destroy(); } catch (_) {}
      log("拿不到 session 原型: " + (e && e.message));
      return null;
    }
  }

  function billedRequest(protoOrig) {
    function hookedRequest(headers) {
      var self = this;
      try {
        var bill = readBill();
        // 铁律：只有 enabled=true 才动手；没开 = 完全透传，绝不影响正常使用。
        if (!(bill && bill.enabled)) return protoOrig.apply(self, arguments);
        var host = sessionHost(self);
        // 认得出且不是 cursor 的域，一概不碰；认不出的靠下面的路径匹配兜底。
        // Unknown hosts fail closed: never infer Cursor traffic from path alone.
        if (!host || !HOST_RE.test(host)) return protoOrig.apply(self, arguments);
        var p = pathFromHeaders(headers);
        var bare = p ? String(p).split("?")[0] : "";
        if (bill.mode === "debug") debugLog(bare, host);
        if (!isBillPath(p)) return protoOrig.apply(self, arguments);
        bumpSeen();
        if (bill.mode === "block") {
          var s = protoOrig.apply(self, arguments);
          try { s.close(0x8); } catch (_) { try { s.destroy(); } catch (_) {} }
          log("BLOCK " + bare + " host=" + host);
          return s;
        }
        log("SEE " + bare + " host=" + host);
        return protoOrig.apply(self, arguments);
      } catch (_) {
        return protoOrig.apply(self, arguments);
      }
    }
    hookedRequest[MARK] = 1;
    return hookedRequest;
  }

  function ensureProtoHook(sample) {
    if (protoHooked) return true;
    var proto = clientSessionProto(sample);
    if (!proto || typeof proto.request !== "function") return false;
    if (isHooked(proto.request)) { protoHooked = true; return true; }
    proto.request = billedRequest(proto.request);
    protoHooked = true;
    log("原型补丁已挂上（新旧连接通吃）");
    return true;
  }

  // connect 只做一件事：给 session 贴上它连的是谁。不换 session.request，
  // 拦截统一由原型补丁负责——两处都改会让同一条请求被记两笔。
  var origConnect = http2.connect;
  var lastSession = null;
  function billConnect(authority) {
    var session = origConnect.apply(this, arguments);
    try {
      session[HOST_TAG] = hostOf(authority);
      lastSession = session;
    } catch (_) {}
    return session;
  }
  billConnect[MARK] = 1;

  if (!(http2.connect && http2.connect[MARK])) {
    http2.connect = billConnect;
  }

  // Patch synchronously at load time. The first request can happen before the
  // polling timer gets its first tick, so delaying this loses that request.
  ensureProtoHook(lastSession);

  // 若运行时暂时拿不到原型，保留轻量重试；正常路径在加载时已同步完成。
  pollTimer = setInterval(function () {
    try {
      if (!protoHooked) ensureProtoHook(lastSession);
    } catch (_) {}
  }, 300);
  if (pollTimer.unref) pollTimer.unref();

  log("installed chijiu-billgate hook " + HOOK_VERSION + " (default OFF)");
})();
