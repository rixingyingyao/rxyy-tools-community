"""Explicit service lifecycle and setup; no login, model call, or editor edit on install."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import runpy
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))
from datadir import CONFIG_PATH, DATA_DIR


def config() -> dict:
    if CONFIG_PATH.exists():
        value = json.loads(CONFIG_PATH.read_text("utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("config.json must contain an object")
        return value
    return {"host": "127.0.0.1", "port": 38999, "gateway_port": 38777,
            "mcp_http_port": 39222, "share_enabled": False, "push_enabled": False,
            "sidebar_auto_rename": False, "sync_root": ""}


def url(cfg: dict) -> str:
    return f"http://127.0.0.1:{int(cfg.get('gateway_port', 38777))}"


def api(cfg: dict, method: str, args: list):
    request = urllib.request.Request(url(cfg) + "/api/" + method,
        data=json.dumps(args).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


def setup() -> dict:
    cfg = config()
    if not CONFIG_PATH.exists():
        with CONFIG_PATH.open("x", encoding="utf-8") as stream:
            json.dump(cfg, stream, ensure_ascii=False, indent=2)
    return cfg


def owned_service(cfg: dict) -> bool:
    try:
        import psutil
        saved = json.loads((DATA_DIR / "community-service.json").read_text("utf-8"))
        process = psutil.Process(saved["pid"])
        return (abs(process.create_time() - saved["started_at"]) < .1
                and "rxyy_mcp" in process.cmdline() and "serve" in process.cmdline()
                and saved["url"] == url(cfg))
    except (OSError, ValueError, KeyError, psutil.Error):
        return False


def start(cfg: dict, open_browser: bool) -> None:
    if owned_service(cfg):
        api(cfg, "get_state", [])
    else:
        for key, default in (("port", 38999), ("gateway_port", 38777), ("mcp_http_port", 39222)):
            port = int(cfg.get(key, default))
            if not port:
                continue
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    raise RuntimeError(f"Port {port} is occupied. Stop that installation or change {key} in {CONFIG_PATH}.")
        executable = Path(sys.executable)
        if os.name == "nt" and executable.with_name("pythonw.exe").exists():
            executable = executable.with_name("pythonw.exe")
        log_path = DATA_DIR / "service.log"
        with log_path.open("ab") as log:
            kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
            process = subprocess.Popen([str(executable), "-m", "rxyy_mcp", "serve"],
                cwd=APP_DIR.parent, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **kwargs)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"Service exited; see {log_path}")
            if owned_service(cfg):
                try:
                    api(cfg, "get_state", [])
                    break
                except OSError:
                    pass
            time.sleep(.4)
        else:
            raise RuntimeError(f"Service not ready within 60 seconds; see {log_path}")
    print(url(cfg) + "/ui")
    if open_browser:
        webbrowser.open(url(cfg) + "/ui")


def serve() -> None:
    import psutil
    cfg = setup()
    saved = {"pid": os.getpid(), "started_at": psutil.Process().create_time(), "url": url(cfg)}
    (DATA_DIR / "community-service.json").write_text(json.dumps(saved), encoding="utf-8")
    sys.argv = [str(APP_DIR / "hub.py"), "--daemon"]
    runpy.run_path(str(APP_DIR / "hub.py"), run_name="__main__")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="rxyy tools community · Cursor / Codex console")
    parser.add_argument("command", choices=("start", "serve", "stop", "status", "setup", "server", "cursor-extension"), nargs="?", default="start")
    parser.add_argument("--no-browser", action="store_true")
    args, extra = parser.parse_known_args(argv)
    try:
        cfg = setup()
        if args.command == "start":
            start(cfg, not args.no_browser)
        elif args.command == "serve":
            serve()
        elif args.command == "stop":
            if not owned_service(cfg):
                raise RuntimeError("No service owned by this community data directory is running")
            reply = api(cfg, "shutdown_core", [])
            print(json.dumps(reply, ensure_ascii=False))
        elif args.command == "status":
            running = owned_service(cfg)
            if running:
                api(cfg, "get_state", [])
            print(json.dumps({"running": running, "url": url(cfg) + "/ui", "data_dir": str(DATA_DIR)}))
        elif args.command == "setup":
            port = int(cfg.get("mcp_http_port", 39222))
            print(f"Config: {CONFIG_PATH}\nCodex: http://127.0.0.1:{port}/mcp/codex\nCursor: http://127.0.0.1:{port}/mcp/cursor")
        else:
            name = "extbus_build.py" if args.command == "cursor-extension" else "server.py"
            sys.argv = [str(APP_DIR / name), *extra]
            runpy.run_path(str(APP_DIR / name), run_name="__main__")
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f"rxyy tools: {error}\n")
