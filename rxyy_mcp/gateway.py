"""rxyy MCP HTTP 网关（增量、非侵入）。

把 pywebview 的 js_api（Api 实例）通过本地 http 暴露，供 rxyy tools 的
「rxyy MCP」页用 iframe 内嵌本控制台 UI。

设计要点：
- 不改动 hub 现有任何逻辑，仅在 hub 进程内旁挂一个本地 http server。
- 仅监听 127.0.0.1（不对外），默认端口 38777。
- GET  /            → 返回 ui.html 原文并注入一段兼容 shim，把
                      window.pywebview.api.<m>(...) 映射成 fetch POST /api/<m>
- POST /api/<m>     → getattr(api, m)(*json_body_args)，结果转 json 返回

因为直接读 ui.html 原文动态注入，ui.html 后续更新会自动跟随，无需维护副本。
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from ipc import ExclusiveThreadingHTTPServer

# 注入到 ui.html 的兼容 shim：让基于 window.pywebview.api 的前端在纯 http 下也能跑
_SHIM = """
<script>
(function () {
  // 普通按钮 15s；装开车/账单 hook 要扫 Cursor 安装目录，powershell 自己就有预算；
  // 批量新建对话要等扩展 batchOpen 回执（hub 侧每批上限 25+10n 秒、10 个一批，
  // 50 个最坏 5×125s），15s 一到就 abort 会把已经开成的一批误报成「创建对话失败」（09-07）
  var SLOW = { park_install_hook: 60000, park_restore: 60000,
               bill_install_hook: 60000, bill_uninstall: 60000,
               ext_batch_open: 640000, ext_reopen_shell: 60000,
               wbhook_install: 60000, wbhook_uninstall: 60000,
               fix_cursor_sidebar: 30000,
               list_native_models: 40000, create_native_task: 120000,
               resume_native_task: 30000 };
  function call(method, args) {
    // 15s 超时：hub 重启/冻死期间按钮点击要快速失败弹 toast，而不是无限挂死
    // （07-27 17:12 用户「点半天没反应」的直接体感来源）
    var ctl = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    var ms = SLOW[method] || 15000;
    var timer = ctl ? setTimeout(function () { ctl.abort(); }, ms) : null;
    return fetch('/api/' + method, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(args || []),
      signal: ctl ? ctl.signal : undefined
    }).then(function (r) {
      if (timer) clearTimeout(timer);
      return r.text().then(function (t) {
        var d = {};
        try { d = JSON.parse(t || '{}'); } catch (err) { d = {error: t || ('HTTP ' + r.status)}; }
        if (!r.ok) throw new Error(d.error || d.__error || ('HTTP ' + r.status));
        return d;
      });
    }, function (e) { if (timer) clearTimeout(timer); throw e; });
  }
  var apiProxy = new Proxy({}, {
    get: function (_, m) {
      return function () { return call(m, Array.prototype.slice.call(arguments)); };
    }
  });
  window.pywebview = window.pywebview || {};
  window.pywebview.api = apiProxy;
  // 原生 pywebview 在桥就绪时派发 pywebviewready；这里在 DOM 就绪后补发
  function fireReady() { window.dispatchEvent(new Event('pywebviewready')); }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', fireReady);
  } else {
    setTimeout(fireReady, 0);
  }
})();
</script>
"""


_IMG_CTYPE = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".gif": "image/gif", ".webp": "image/webp"}


# get_state 里这些字段是「秒表」：每秒自己就变（处理中秒数/zt 上报年龄/空闲秒数/
# 接手在途等待秒数）。门铃摘要必须把它们抠掉，否则光是时间流逝就会让摘要每拍都变、
# 门铃常鸣——那就退化回高频轮询了。抠的只是摘要的输入，UI 拉到的数据原样不动。
_VOLATILE_KEYS = frozenset({"processing_secs", "agent_status_age", "idle_secs", "age"})

# API 调用超过这个毫秒数就记一笔运行日志（诊断「点发送要等好多秒」这类体感投诉）
SLOW_CALL_MS = 1000


def _scrub_volatile(node):
    if isinstance(node, dict):
        return {k: _scrub_volatile(v) for k, v in node.items()
                if k not in _VOLATILE_KEYS}
    if isinstance(node, list):
        return [_scrub_volatile(x) for x in node]
    return node


class Doorbell:
    """状态门铃：后台盯着 get_state 的摘要，一变就叫醒所有挂着的 SSE 长连接。

    为什么不去 hub 的几十处状态变更点埋钩子：那些 `s.rev += 1` 散布在会话生死/
    改名/排队/接手/转告的所有路径上，漏一处就是一类「界面不动」的暗病（用户
    08-27 报「发消息要等好多秒状态才变」正是轮询拍点撞不上变化的体感）。门铃盯的
    是 UI 轮询本来就在看的同一份快照（get_state 纯读零副作用、零 I/O），信号源与
    消费口径天然一致，永不漏报；代价是每拍一次内存快照 + sha1（20 会话约 1ms）。

    门铃只送「变了 + 版本号」，数据仍由前端照旧走 get_state/get_messages 拉取——
    SSE 只是把「等下一拍轮询」提前成「当拍就刷」，不开第二条数据口径。
    """

    TICK_SECS = 0.25

    def __init__(self, api):
        self._api = api
        self._cond = threading.Condition()
        self.rev = 0
        self._digest = None

    def start(self):
        threading.Thread(target=self._loop, name="gateway-doorbell",
                         daemon=True).start()
        return self

    def _loop(self):
        while True:
            time.sleep(self.TICK_SECS)
            try:
                self.probe_once()
            except Exception:
                pass  # hub 尚在启动/单拍异常都不该杀死门铃线程

    def probe_once(self):
        """做一次摘要比对，变了就 rev+1 并叫醒等待者。拆出来是为了可测。"""
        snap = json.dumps(_scrub_volatile(self._api.get_state()),
                          ensure_ascii=False, default=str)
        digest = hashlib.sha1(snap.encode("utf-8")).hexdigest()
        if digest == self._digest:
            return False
        self._digest = digest
        with self._cond:
            self.rev += 1
            self._cond.notify_all()
        return True

    def wait_change(self, seen_rev, timeout):
        """版本超过 seen_rev 就返回新版本号；超时仍没动静返回 None（该发心跳了）。"""
        with self._cond:
            if self.rev == seen_rev:
                self._cond.wait(timeout)
            return self.rev if self.rev != seen_rev else None


def img_path_from_url(raw):
    """从 /img?p=... 或 /img/<urlencoded> 取出本地路径。

    旧 webgw（已打进 rxyy-tools-community.exe）转发 /mcpgw 时丢掉 query，path 却原样带着。
    控制台 UI 改走后一种，手机不换装也能看到图。
    """
    from urllib.parse import parse_qs, unquote, urlparse
    u = urlparse(raw)
    p = (parse_qs(u.query).get("p") or [""])[0]
    if p:
        return p
    prefix = "/img/"
    if u.path.startswith(prefix):
        return unquote(u.path[len(prefix):])
    return ""


def _make_handler(api, ui_path: Path, bell: Doorbell | None = None, logger=None):
    class Handler(BaseHTTPRequestHandler):
        # HTTP/1.1：SSE 需要一条不关的连接；顺带普通接口得到 keep-alive（本页
        # 每 0.7-2.5s 一拍轮询，省去每拍握手）。所有普通响应都经 _send 精确写
        # Content-Length，复用连接不会错位。
        protocol_version = "HTTP/1.1"

        def _send(self, code, body, ctype="application/json; charset=utf-8", cache=False):
            data = body if isinstance(body, (bytes, bytearray)) else str(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Access-Control-Allow-Origin", "*")
            if cache:
                self.send_header("Cache-Control", "max-age=86400")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except Exception:
                pass

        def _serve_image(self):
            """GET /img?p=<绝对路径> 或 /img/<urlencoded>：代理消息图片。"""
            p = img_path_from_url(self.path)
            try:
                root = Path(str(api.image_root().get("root"))).resolve()
                fp = Path(p).resolve()
            except Exception:
                self._send(400, "{}")
                return
            ctype = _IMG_CTYPE.get(fp.suffix.lower())
            inside = root in fp.parents or fp == root
            if not (ctype and inside and fp.is_file()):
                self._send(404, "{}")
                return
            try:
                self._send(200, fp.read_bytes(), ctype, cache=True)
            except OSError:
                self._send(404, "{}")

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _serve_events(self):
            """GET /events：SSE 门铃。只报「状态有变+版本号」，正文数据仍由前端
            照旧拉取。心跳 15s 一拍：既让死连接尽快暴露（写失败即回收线程），
            也防中间层把空闲流掐断。"""
            if bell is None:
                self._send(404, "{}")
                return
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                # 断线后浏览器 2s 自动重连；随后立发当前版本号当「连通了」的首帧
                self.wfile.write(b"retry: 2000\n\n")
                seen = -1
                while True:
                    rev = bell.wait_change(seen, timeout=15.0)
                    if rev is None:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    seen = rev
                    self.wfile.write(("data: %d\n\n" % rev).encode("ascii"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionError, OSError):
                return

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/events":
                self._serve_events()
                return
            if path == "/img" or path.startswith("/img/"):
                self._serve_image()
                return
            if path in ("/model_picker.js", "/transfer_dialog.js", "/native_questions.js"):
                try:
                    self._send(200, ui_path.with_name(path.lstrip("/")).read_bytes(),
                               "application/javascript; charset=utf-8")
                except OSError:
                    self._send(404, "{}")
                return
            if path == "/highlight.min.js":
                # 代码高亮库本地化：与 ui.html 同目录，打包/源码两种形态都随包走
                try:
                    self._send(200, ui_path.with_name("highlight.min.js").read_bytes(),
                               "application/javascript; charset=utf-8", cache=True)
                except OSError:
                    self._send(404, "{}")
                return
            if path in ("/", "/ui", "/index.html"):
                try:
                    html = ui_path.read_text(encoding="utf-8")
                except Exception as exc:  # noqa: BLE001
                    self._send(500, "<h1>ui.html 读取失败: %s</h1>" % exc, "text/html; charset=utf-8")
                    return
                if "</head>" in html:
                    html = html.replace("</head>", _SHIM + "</head>", 1)
                else:
                    html = _SHIM + html
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            else:
                self._send(404, "{}")

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if not path.startswith("/api/"):
                self._send(404, json.dumps({"error": "not found"}))
                return
            method = path[len("/api/"):]
            if not method or method.startswith("_"):
                self._send(403, json.dumps({"error": "forbidden"}))
                return
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"[]"
            try:
                args = json.loads(raw.decode("utf-8") or "[]")
            except Exception:
                args = []
            if not isinstance(args, list):
                args = [args]
            fn = getattr(api, method, None)
            if not callable(fn):
                self._send(404, json.dumps({"error": "no such method: " + method}))
                return
            t0 = time.monotonic()
            try:
                result = fn(*args)
            except Exception as exc:  # noqa: BLE001
                self._send(500, json.dumps({"error": repr(exc)}))
                return
            finally:
                # 慢调用留痕：用户报「点发送要等好多秒」时，这里给出铁证——
                # 到底是哪个方法慢、慢多少，而不是靠体感猜（低频，只记超阈值的）。
                dt_ms = int((time.monotonic() - t0) * 1000)
                if dt_ms >= SLOW_CALL_MS and logger is not None:
                    try:
                        logger("网关慢调用 {} {}ms".format(method, dt_ms))
                    except Exception:
                        pass
            try:
                payload = json.dumps(result, ensure_ascii=False, default=str)
            except Exception:
                payload = json.dumps({"error": "result not json-serializable"})
            self._send(200, payload.encode("utf-8"))

        def log_message(self, *args):  # 静音，避免污染 hub 日志
            pass

    return Handler


class _QuietGatewayServer(ExclusiveThreadingHTTPServer):
    def handle_error(self, request, client_address):
        """keep-alive/SSE 连接被浏览器随手掐断是常态（刷新页面/关 tab），
        默认实现会往 stderr 打全栈——升 HTTP/1.1 后每次刷新都要刷一屏。
        连接层的断线（OSError 一族）静默吞掉；处理器内部真正的 bug
        （TypeError 之类）照旧打出来。"""
        import sys as _sys
        if isinstance(_sys.exc_info()[1], OSError):
            return
        super().handle_error(request, client_address)


def start_gateway(api, ui_path, host="127.0.0.1", port=38777, logger=None):
    """在后台线程起 http 网关，返回 httpd（失败会抛异常，调用方自行兜底）。
    独占绑定：网关是僵尸判定的核心探针，端口被第二个实例劫持=误判连环翻车。"""
    bell = Doorbell(api).start()
    handler = _make_handler(api, Path(ui_path), bell=bell, logger=logger)
    httpd = _QuietGatewayServer((host, port), handler)
    threading.Thread(target=httpd.serve_forever, name="persist-gateway", daemon=True).start()
    return httpd
