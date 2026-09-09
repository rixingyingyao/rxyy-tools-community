"""Real HTTP/stdio transports with isolated state and a fake bridge."""
import http.client
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE))
import server


def request(method, params=None, rid=1):
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}


def initialize(name):
    return request("initialize", {"protocolVersion": "2025-11-25",
        "capabilities": {}, "clientInfo": {"name": name, "version": "test"}})


class FakeBridge:
    def __init__(self):
        self.calls = []
        self._keepalive_active = set()
        self._deferred_convs = set()
        self._minted_conv_id = "test"

    def ask(self, *args, **kwargs):
        self.calls.append(kwargs)
        return {"user_input": "protocol-ok", "selected_options": []}

    def own_conv_id_hint(self, cid):
        return cid or "test"

    def _schedule_idle_clear(self, *_):
        pass


class HttpProtocolSmoke(unittest.TestCase):
    def setUp(self):
        self.bridge = FakeBridge()
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(server, "BRIDGE", self.bridge),
            patch.object(server, "DATA_DIR", Path(self.tmp.name)),
            patch.object(server, "HTTP_RUNTIME_REGISTRY", server.HttpRuntimeRegistry()),
            patch.object(server, "DISABLED", False),
            patch.object(server, "log", lambda *_: None),
            patch.object(server, "_is_frozen", return_value=False)]
        for p in self.patches:
            p.start()
        # Materialize the production Handler without starting watchdogs or
        # touching live config. Only this test's ephemeral listener is started.
        factory = server.ExclusiveThreadingHTTPServer
        with patch.object(server.instance_owner, "should_stand_down", return_value=(False, "")), \
                patch.object(server, "ExclusiveThreadingHTTPServer", side_effect=RuntimeError("handler-ready")):
            with self.assertRaisesRegex(RuntimeError, "handler-ready"):
                server.serve_http(0, in_hub=True)
        self.http = factory(("127.0.0.1", 0), server.McpHttpHandler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(3)
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def post(self, path, payload, sid=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.http.server_address[1], timeout=5)
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if sid:
            headers[server.SESSION_HEADER] = sid
        conn.request("POST", path, json.dumps(payload), headers)
        response = conn.getresponse()
        sid = response.getheader(server.SESSION_HEADER)
        if "text/event-stream" in (response.getheader("Content-Type") or ""):
            while True:
                line = response.readline().decode("utf-8").strip()
                if line.startswith("data:"):
                    result = json.loads(line[5:])
                    if result.get("id") == payload["id"]:
                        break
        else:
            result = json.loads(response.read())
        code = response.status
        conn.close()
        self.assertEqual(200, code)
        return result, sid

    def test_codex_and_chatgpt_sessions_keep_their_metadata_on_new_connections(self):
        a, aid = self.post("/mcp/codex", initialize("unknown"))
        b, bid = self.post("/mcp/chatgpt", initialize("unknown"))
        self.assertTrue(aid)
        self.assertNotEqual(aid, bid)
        self.assertLessEqual(len(a["result"]["instructions"].encode("utf-8")), 512)
        for sid, runtime in ((aid, "codex"), (bid, "chatgpt")):
            listing, _ = self.post("/mcp", request("tools/list", rid=2), sid)
            zhi = next(t for t in listing["result"]["tools"] if t["name"] == "zhi")
            self.assertIn("artifacts", zhi["inputSchema"]["properties"])
            result, _ = self.post("/mcp", request("tools/call", {"name": "zhi", "arguments": {
                "message": "test", "conversation_id": "test-" + runtime}}, rid=3), sid)
            self.assertIn("protocol-ok", str(result))
            self.assertEqual(runtime, self.bridge.calls[-1]["runtime_kind"])
            self.assertFalse(self.bridge.calls[-1]["wait"])

    def test_legacy_cursor_initialize_still_returns_cursor_contract(self):
        result, sid = self.post("/mcp", initialize("Cursor"))
        self.assertIsNone(sid)
        self.assertIn("zhi", result["result"]["instructions"])


class StdioProtocolSmoke(unittest.TestCase):
    def test_real_stdio_initialize_list_and_disabled_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = "\n".join([
                "import sys", "from pathlib import Path", "sys.path.insert(0, " + repr(str(MODULE)) + ")",
                "import server", "server.DATA_DIR = Path(" + repr(tmp) + ")",
                "server.log = lambda *args: None", "server.DISABLED = True",
                "server._install_crash_forensics = lambda: None", "server.main()"])
            proc = subprocess.Popen([sys.executable, "-u", "-c", script],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=dict(os.environ, CHIJIU_UNDER_TEST="1"))
            lines = queue.Queue()
            reader = threading.Thread(target=lambda: [lines.put(line) for line in proc.stdout], daemon=True)
            reader.start()
            try:
                requests = [initialize("codex-cli"), request("tools/list", rid=2),
                    request("tools/call", {"name": "zhi", "arguments": {"message": "test"}}, rid=3)]
                results = []
                for payload in requests:
                    proc.stdin.write((json.dumps(payload) + "\n").encode())
                    proc.stdin.flush()
                    response = json.loads(lines.get(timeout=15))
                    self.assertEqual(payload["id"], response["id"])
                    results.append(response)
                self.assertEqual(3, len(results[1]["result"]["tools"]))
                self.assertNotIn("CallMcpTool", str(results))
                self.assertIn("禁用模式", str(results[2]))
            finally:
                proc.stdin.close()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait(timeout=5)
                proc.stdout.close()
                proc.stderr.close()


if __name__ == "__main__":
    unittest.main()
