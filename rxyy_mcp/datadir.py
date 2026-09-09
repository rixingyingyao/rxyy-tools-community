"""Community state never reads a personal rxyy-tools install or source pointer."""
from __future__ import annotations

import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent


def resolve_data_dir() -> Path:
    configured = os.environ.get("RXYY_MCP_DATA_DIR", "").strip()
    if configured:
        result = Path(configured).expanduser().resolve()
    else:
        base = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local/state"))
        result = base / "rxyy-tools-community"
    result.mkdir(parents=True, exist_ok=True)
    return result


DATA_DIR = resolve_data_dir()
CONFIG_PATH = DATA_DIR / "config.json"
