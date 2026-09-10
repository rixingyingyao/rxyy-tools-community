"""Community package installation and isolated service lifecycle regression tests."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request


ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class CommunityCliIntegrationTests(unittest.TestCase):
    """Start the exported package without touching a user's installed instance."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="rxyy-community-test-")
        self.addCleanup(self.temporary.cleanup)
        self.temp_root = Path(self.temporary.name)
        self.data_dir = self.temp_root / "data"
        self.data_dir.mkdir()
        self.ports = {"port": _free_port(), "gateway_port": _free_port(),
                      "mcp_http_port": _free_port()}
        self.env = os.environ.copy()
        self.env.update({
            "RXYY_MCP_DATA_DIR": str(self.data_dir),
            "LOCALAPPDATA": str(self.temp_root / "localappdata"),
            "PYTHONPATH": str(ROOT) + os.pathsep + self.env.get("PYTHONPATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        (self.data_dir / "config.json").write_text(json.dumps({
            **self.ports,
            "host": "127.0.0.1",
            "bind_host": "127.0.0.1",
            "history_dir": str(self.temp_root / "history"),
            "share_enabled": False,
            "push_enabled": False,
            "sidebar_auto_rename": False,
            "sync_root": "",
        }), encoding="utf-8")
        self.service = None

    def tearDown(self) -> None:
        try:
            self._stop_service()
        finally:
            self.temporary.cleanup()

    def _cli(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "rxyy_mcp", *args],
            cwd=ROOT,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=75,
            check=check,
        )

    def _request(self, url: str, payload: dict | list | None = None,
                 headers: dict[str, str] | None = None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=headers or {})
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, dict(response.headers.items()), response.read()

    def _wait_for(self, predicate, message: str) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return
            except (OSError, ValueError, urllib.error.URLError):
                pass
            time.sleep(0.2)
        log = self.data_dir / "service.log"
        detail = log.read_text("utf-8", errors="replace") if log.exists() else ""
        self.fail(f"{message}\n{detail[-4000:]}")

    def _owned_process(self):
        state_path = self.data_dir / "community-service.json"
        if not state_path.exists():
            return None
        try:
            record = json.loads(state_path.read_text("utf-8"))
            import psutil
            process = psutil.Process(int(record["pid"]))
            cmdline = process.cmdline()
            same_process = abs(process.create_time() - float(record["started_at"])) < 0.1
            is_community_service = (
                "rxyy_mcp" in cmdline
                and "serve" in cmdline
                and Path(process.cwd()).resolve() == (ROOT / "rxyy_mcp").resolve()
                and record.get("url") == f"http://127.0.0.1:{self.ports['gateway_port']}"
            )
            return process if same_process and is_community_service else None
        except (OSError, ValueError, KeyError, json.JSONDecodeError, psutil.Error):
            return None

    def _stop_service(self) -> None:
        process = self._owned_process()
        if process is None:
            return
        self._cli("stop", check=False)
        self._wait_for(lambda: not process.is_running(), "community service did not stop")
        if process.is_running():
            # This branch remains constrained by the recorded PID + creation time check above.
            process.terminate()
            try:
                process.wait(timeout=5)
            except Exception:
                process.kill()
                process.wait(timeout=5)

    def _mcp(self, route: str, request: dict, session_id: str = ""):
        headers = {"Content-Type": "application/json"}
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        status, response_headers, body = self._request(
            f"http://127.0.0.1:{self.ports['mcp_http_port']}{route}",
            request, headers)
        self.assertEqual(status, 200)
        return json.loads(body), response_headers.get("Mcp-Session-Id", "")

    @unittest.skipUnless(sys.platform.startswith("win"), "community CLI service test is Windows-specific")
    def test_start_ui_api_mcp_routes_and_stop_in_isolated_data_dir(self) -> None:
        started = self._cli("start", "--no-browser")
        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertIn(f"http://127.0.0.1:{self.ports['gateway_port']}/ui", started.stdout)
        self._wait_for(lambda: self._owned_process() is not None, "service record was not created")

        ui_status, _headers, ui_body = self._request(
            f"http://127.0.0.1:{self.ports['gateway_port']}/ui")
        self.assertEqual(ui_status, 200)
        self.assertIn(b"rxyy", ui_body.lower())

        state_status, _headers, state_body = self._request(
            f"http://127.0.0.1:{self.ports['gateway_port']}/api/get_state", [])
        self.assertEqual(state_status, 200)
        self.assertIsInstance(json.loads(state_body), dict)

        for runtime in ("codex", "cursor"):
            initialized, session_id = self._mcp(f"/mcp/{runtime}", {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": runtime, "version": "test"},
                },
            })
            self.assertEqual(initialized["jsonrpc"], "2.0")
            self.assertIn("result", initialized)

            listed, _ = self._mcp(f"/mcp/{runtime}", {
                "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
            }, session_id)
            names = {tool["name"] for tool in listed["result"]["tools"]}
            self.assertTrue({"zt", "zhi", "ji"}.issubset(names), listed)

        self._stop_service()
        self.assertIsNone(self._owned_process())


if __name__ == "__main__":
    unittest.main()
