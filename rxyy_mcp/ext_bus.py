# -*- coding: utf-8 -*-
"""hub ↔ Cursor 扩展宿主之间的文件命令总线（hub 这一头）。

hub 是独立进程，进不了 Cursor 的扩展宿主，所以「无头开一个新对话」「把某个对话
调到前台」这类只有 `vscode.commands.executeCommand` 才做得到的事一直做不了——
以前只能靠 cursor_live_rename 那套敲键盘（抢焦点、提权就失败）。

解法照抄 bajie-chat 0.7.45 的跨窗口命令总线（`bajie-chat扩展全解析-20260902.md` §3.5）：

    <root>/instances.json                       每个装了扩展的 Cursor 窗口定时登记（60s 判活）
    <root>/cmds/<instanceId>/<uuid>.req.json    hub 想让某个窗口干活 → 写一个请求文件
    <root>/cmds/<instanceId>/<uuid>.res.json    窗口里的泵取走请求、执行、把结果写回来

并发安全的手法同样照抄：tmp+rename 原子写（读方永远看不到半个文件）、请求被泵
取走即删（一条命令只会被执行一次）、TTL 5 分钟（泵停了几小时再起来不会把过期
请求补执行一遍）。

根目录刻意**不跟 DATA_DIR 走**：机器态目录在源码态 / 打包版 / 常驻区三种住法下各不
相同（datadir.py），扩展那一头没法可靠地推断；固定在用户级
`%LOCALAPPDATA%\\rxyy-tools-community\\extbus\\`，hub 与扩展各自都算得出同一个路径，两机同口径。
一台机器只跑一个正主 hub，开发态 hub 与常驻区共用一条总线也不冲突——每条命令
自带 uuid、结果各回各的文件。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

INSTANCE_FRESH_MS = 60_000      # 窗口多久没登记就当它不在了（扩展每 15s 登记一次）
CMD_TTL_MS = 5 * 60 * 1000      # 请求 / 结果文件的寿命
DEFAULT_TIMEOUT = 25.0          # 等结果的默认上限（秒）；createNew + 挂载确认实测 1~4s

_ROOT_OVERRIDE: Path | None = None


def set_root(path) -> None:
    """测试 / 排查用：把总线根目录指到别处。传 None 恢复默认推断。"""
    global _ROOT_OVERRIDE
    _ROOT_OVERRIDE = Path(path) if path else None


def bus_root() -> Path:
    if _ROOT_OVERRIDE is not None:
        return _ROOT_OVERRIDE
    raw = (os.environ.get("CHIJIU_EXTBUS_ROOT") or "").strip()
    if raw:
        return Path(raw)
    local = (os.environ.get("LOCALAPPDATA") or "").strip()
    if local:
        return Path(local) / "rxyy-tools-community" / "extbus"
    return Path.home() / ".rxyy-tools-community" / "extbus"


def _sanitize(name) -> str:
    return "".join(ch for ch in str(name or "") if ch.isalnum() or ch in "-_")[:80] or "_"


def _cmd_dir(instance_id) -> Path:
    return bus_root() / "cmds" / _sanitize(instance_id)


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json_atomic(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("%s.%d.%d.tmp" % (path.name, os.getpid(), int(time.time() * 1000)))
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def read_instances() -> list[dict]:
    """instances.json 里登记过的全部窗口（含过期的），坏行丢弃。"""
    raw = _read_json(bus_root() / "instances.json", {}) or {}
    items = raw.get("instances") if isinstance(raw, dict) else None
    out = []
    for it in items or []:
        if isinstance(it, dict) and isinstance(it.get("id"), str) and it.get("id"):
            out.append(it)
    return out


def live_instances(fresh_ms: int = INSTANCE_FRESH_MS, now_ms: float | None = None) -> list[dict]:
    """此刻还活着的窗口：updatedAt 在 fresh_ms 之内。

    返回项：{id, label, pid, version, workspace, updatedAt, age_ms}，按 label 排序。
    """
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    out = []
    for it in read_instances():
        try:
            upd = float(it.get("updatedAt") or 0)
        except (TypeError, ValueError):
            upd = 0.0
        age = now_ms - upd
        if age > fresh_ms:
            continue
        out.append({
            "id": it["id"],
            "label": str(it.get("label") or "Cursor 窗口"),
            "pid": it.get("pid"),
            "version": str(it.get("version") or ""),
            "workspace": str(it.get("workspace") or ""),
            "updatedAt": upd,
            "age_ms": int(max(0.0, age)),
        })
    out.sort(key=lambda x: (x["label"], x["id"]))
    return out


def find_instance(instance_id) -> dict | None:
    want = str(instance_id or "").strip()
    for it in live_instances():
        if it["id"] == want:
            return it
    return None


def post_command(instance_id, action, payload=None, sender="hub") -> str:
    """往某个窗口的收件箱里丢一条请求，返回命令 id（结果文件按它取）。"""
    cmd_id = uuid.uuid4().hex
    _write_json_atomic(_cmd_dir(instance_id) / (cmd_id + ".req.json"), {
        "id": cmd_id,
        "action": str(action),
        "payload": payload if isinstance(payload, dict) else {},
        "from": str(sender or "hub"),
        "createdAt": int(time.time() * 1000),
    })
    return cmd_id


def await_result(instance_id, cmd_id, timeout: float = DEFAULT_TIMEOUT, poll: float = 0.15):
    """轮询结果文件；拿到即删并返回 result（dict）。超时返回 None。

    结果文件是窗口用 tmp+rename 写的，读到就是完整的；读坏（极小概率恰逢
    rename 之前的瞬间）就下一拍再读。
    """
    res = _cmd_dir(instance_id) / (_sanitize(cmd_id) + ".res.json")
    deadline = time.time() + max(0.2, float(timeout))
    while time.time() < deadline:
        if res.is_file():
            data = _read_json(res, None)
            if data is None:
                time.sleep(poll)
                continue
            try:
                res.unlink()
            except OSError:
                pass
            return data.get("result") if isinstance(data, dict) else None
        time.sleep(poll)
    # 超时：把请求文件也撤掉，别让泵晚几分钟醒来又替我们开一个对话
    try:
        (_cmd_dir(instance_id) / (_sanitize(cmd_id) + ".req.json")).unlink()
    except OSError:
        pass
    return None


def call(instance_id, action, payload=None, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """post + await 一步到位；统一成 {ok, ...} 形状，超时 / 窗口不在都不抛。"""
    inst = find_instance(instance_id)
    if inst is None:
        return {"ok": False, "error": "该 Cursor 窗口不在线（扩展没登记或已过期）",
                "code": "NO_INSTANCE"}
    cmd_id = post_command(instance_id, action, payload)
    result = await_result(instance_id, cmd_id, timeout=timeout)
    if result is None:
        return {"ok": False, "error": "Cursor 窗口 %.0f 秒内没有回应（扩展泵停了？）"
                % timeout, "code": "TIMEOUT", "cmd_id": cmd_id}
    if not isinstance(result, dict):
        return {"ok": False, "error": "窗口回了个看不懂的结果", "raw": result}
    result.setdefault("ok", True)
    return result


def sweep_expired(now_ms: float | None = None) -> int:
    """清掉 TTL 之外的 req/res 残留（窗口不在、命令永远没人取时会积）。返回删了几个。"""
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    n = 0
    root = bus_root() / "cmds"
    if not root.is_dir():
        return 0
    for d in root.iterdir():
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if not (f.name.endswith(".req.json") or f.name.endswith(".res.json")
                    or f.name.endswith(".tmp")):
                continue
            try:
                if now_ms - f.stat().st_mtime * 1000 > CMD_TTL_MS:
                    f.unlink()
                    n += 1
            except OSError:
                pass
    return n
