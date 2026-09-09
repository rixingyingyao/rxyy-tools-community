# -*- coding: utf-8 -*-
"""rxyy MCP workbench 桥：往 Cursor 主包插软失败钩子（学 Bajie §4.2/4.3）。

两处：
  1. workbench.desktop.main.js —— 打分找到 composerDataService / composerChatService
     赋值，紧跟着偷到 globalThis（不抄 curvsix，不替换 extensionHostProcess）
  2. workbench.html —— 外链 chijiu-wbhook.js，轮询控制台改名/回合

装前各留 .chijiu.bak，卸按备份还原。现窗口要 Reload 才挂上；挂不上软失败。
测试请传 app= 假根，禁止拿真实 Cursor 当夹具。
"""
from __future__ import annotations

import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

MARK = "chijiu-wbhook"
SCRIPT_NAME = "chijiu-wbhook.js"
HTML_TAG = f'<!-- {MARK} --><script src="./{SCRIPT_NAME}"></script>'
DATA_MARK = "/*chijiu-wbhook-data*/"
CHAT_MARK = "/*chijiu-wbhook-chat*/"
SELF_DATA_MARK = "/*chijiu-wbhook-self-data*/"
# 消费者赋值处：只收像真服务的，且不让空壳盖掉已经偷到的 getHandleIfLoaded。
DATA_SNIP = (
    DATA_MARK
    + "(function(){try{var s=this.composerDataService||this._composerDataService;"
    + "if(!s)return;"
    + "var ok=typeof s.getHandleIfLoaded===\"function\"||typeof s.getComposerHandleById===\"function\""
    + "||typeof s.updateComposerData===\"function\"||typeof s.updateComposerDataSetStore===\"function\";"
    + "if(!ok)return;"
    + "var cur=globalThis.__chijiuComposer;"
    + "if(cur&&typeof cur.getHandleIfLoaded===\"function\"&&typeof s.getHandleIfLoaded!==\"function\")return;"
    + "globalThis.__chijiuComposer=s}"
    + "catch(_){}}).call(this),"
)
CHAT_SNIP = (
    CHAT_MARK
    + "(function(){try{var s=this.composerChatService;"
    + "if(s&&(typeof s.submitChatMaybeAbortCurrent===\"function\"||typeof s.submit===\"function\")){"
    + "var cur=globalThis.__chijiuChat;"
    + "if(!(cur&&typeof cur.submitChatMaybeAbortCurrent===\"function\"&&typeof s.submitChatMaybeAbortCurrent!==\"function\"))"
    + "globalThis.__chijiuChat=s}}"
    + "catch(_){}}).call(this),"
)
# 真服务自己的构造函数：this 就是 ComposerDataService（1.128 的 k4）。
SELF_DATA_SNIP = (
    SELF_DATA_MARK
    + "(function(){try{if(typeof this.getHandleIfLoaded===\"function\"){"
    + "globalThis.__chijiuComposer=this;"
    + "if(this._commandService)globalThis.__chijiuCommand=this._commandService}}"
    + "catch(_){}}).call(this),"
)
SELF_DATA_NEEDLE = 'rn("composerDataService")'
SRC = Path(__file__).resolve().parent / "wbhook_client.js"
MIN_SCORE = 20
_SKIP = re.compile(r"mockAgentStreamService|setupAIServiceMocking|installE2E")
_DATA_ASSIGN = re.compile(
    r"this\.(?:_?)composerDataService=([A-Za-z_$][\w$]*),"
)
_CHAT_ASSIGN = re.compile(
    r"this\.composerChatService=([A-Za-z_$][\w$]*),"
)
_LOCKED_DATA = "this.viewDescriptorService=i,this.composerDataService="


def _app_roots():
    env = (os.environ.get("CURSOR_APP_ROOT") or "").strip()
    cands = []
    if env:
        cands.append(Path(env))
    local = Path(os.environ.get("LOCALAPPDATA") or "")
    cands.extend([
        Path(r"E:\cursor"),
        Path(r"D:\cursor"),
        local / "Programs" / "cursor",
        local / "Programs" / "Cursor",
        Path(r"C:\Program Files\cursor"),
        Path(r"C:\Program Files\Cursor"),
    ])
    out = []
    for p in cands:
        app = p / "resources" / "app" if (p / "resources" / "app").is_dir() else p
        if app.is_dir() and app not in out:
            out.append(app)
    return out


def _html_path(app=None):
    roots = [Path(app)] if app else _app_roots()
    for root in roots:
        p = root / "out" / "vs" / "code" / "electron-sandbox" / "workbench" / "workbench.html"
        if p.is_file():
            return p
    return None


def _bundle_path(app=None):
    roots = [Path(app)] if app else _app_roots()
    for root in roots:
        p = root / "out" / "vs" / "workbench" / "workbench.desktop.main.js"
        if p.is_file():
            return p
    return None


def workbench_html(app=None):
    return _html_path(app)


def _score(text, pos, kind):
    near = text[max(0, pos - 160): pos + 160]
    if _SKIP.search(near):
        return -999
    ctx = text[max(0, pos - 2000): pos + 2000]
    score = 0
    locked = kind == "data" and _LOCKED_DATA in ctx
    if locked:
        score += 80
    if "viewDescriptorService" in ctx:
        score += 20
    if "composerChatService" in ctx:
        score += 18
    if "getHandleIfLoaded" in ctx:
        score += 25
    if "updateComposerData" in ctx:
        score += 20
    if "getLoadedConversation" in ctx:
        score += 12
    if "getComposerData" in ctx:
        score += 8
    if "getLastAiBubbles" in ctx:
        score += 16
    if "submitChatMaybeAbortCurrent" in ctx:
        score += 22
    if "renameComposer" in ctx:
        score += 16
    # 工作台面板构造函数上下文里常出现 background* 字样，不能把 Bajie 锁定锚点扣下去。
    if "backgroundComposerDataService" in ctx and not locked:
        score -= 25
    return score


def find_anchors(text, kind="data"):
    """打分找注入点。返回 [(score, start, matched_assign), ...] 降序。"""
    pat = _DATA_ASSIGN if kind == "data" else _CHAT_ASSIGN
    rows = []
    for m in pat.finditer(text):
        sc = _score(text, m.start(), kind)
        if sc >= MIN_SCORE:
            rows.append((sc, m.start(), m.group(0)))
    rows.sort(key=lambda r: (-r[0], r[1]))
    return rows


def _already_patched(text, pos, assign, mark):
    after = text[pos + len(assign): pos + len(assign) + 64]
    return mark in after


def _locked_data_assign_pos(text):
    i = text.find(_LOCKED_DATA)
    if i < 0:
        return None
    j = text.find("this.composerDataService=", i)
    if j < 0 or j - i > 80:
        return None
    return j


def _patch_self_data(text):
    """在 ComposerDataService 自己的 constructor 里挂 this。"""
    i = text.find(SELF_DATA_NEEDLE)
    if i < 0:
        return text, False
    j = text.find("{super(),", i)
    if j < 0 or j - i > 500:
        return text, False
    at = j + len("{super(),")
    if SELF_DATA_MARK in text[at:at + 96]:
        return text, True
    return text[:at] + SELF_DATA_SNIP + text[at:], True


def _patch_bundle(text, kind="data"):
    """同一服务可能挂在好几个构造函数上。多点各偷一次，懒加载的类也能碰到。"""
    mark = DATA_MARK if kind == "data" else CHAT_MARK
    snip = DATA_SNIP if kind == "data" else CHAT_SNIP
    hits = find_anchors(text, kind)
    chosen = []
    seen = set()
    if kind == "data":
        locked = _locked_data_assign_pos(text)
        if locked is not None:
            assign = "this.composerDataService="
            # 取出完整 assign（到逗号）
            m = _DATA_ASSIGN.match(text, locked)
            if m and not _already_patched(text, locked, m.group(0), mark):
                chosen.append((locked, m.group(0)))
                seen.add(locked)
    for _sc, pos, assign in hits:
        if pos in seen or _already_patched(text, pos, assign, mark):
            continue
        chosen.append((pos, assign))
        seen.add(pos)
        if len(chosen) >= 4:
            break
    if not chosen:
        return text, mark in text
    for pos, assign in sorted(chosen, reverse=True):
        text = text[:pos] + assign + snip + text[pos + len(assign):]
    return text, True


def _bak(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".chijiu.bak")


def _write_client(dest: Path, port=38777):
    raw = SRC.read_text(encoding="utf-8")
    port_text = str(int(port or 38777))
    dest.write_text(raw.replace("__RXYY_MCP_HUB__", port_text)
                    .replace("__CHIJIU_HUB__", port_text), encoding="utf-8")


def status(app=None):
    html = _html_path(app)
    bundle = _bundle_path(app)
    if html is None or bundle is None:
        return {"ok": False, "installed": False, "error": "找不到 Cursor workbench 文件"}
    ht = html.read_text(encoding="utf-8")
    bt = bundle.read_text(encoding="utf-8", errors="replace")
    js = html.with_name(SCRIPT_NAME)
    return {
        "ok": True,
        "installed": MARK in ht and DATA_MARK in bt and js.is_file(),
        "html": str(html),
        "bundle": str(bundle),
        "js": str(js),
        "html_hooked": MARK in ht,
        "data_hooked": DATA_MARK in bt,
        "self_data_hooked": SELF_DATA_MARK in bt,
        "chat_hooked": CHAT_MARK in bt,
        "has_bak": _bak(html).is_file() or _bak(bundle).is_file(),
        "need_reload": True,
        "data_sites": bt.count(DATA_MARK),
        "self_data": SELF_DATA_MARK in bt,
        "chat_sites": bt.count(CHAT_MARK),
        "beat_age": None if not _BEAT.get("at") else round(time.time() - float(_BEAT["at"]), 1),
        "beat_ready": bool(_BEAT.get("ready")),
        "beat_chat": bool(_BEAT.get("chat")),
    }


def install(app=None, port=38777):
    html = _html_path(app)
    bundle = _bundle_path(app)
    if html is None or bundle is None:
        return {"ok": False, "error": "找不到 Cursor workbench 文件"}
    if not SRC.is_file():
        return {"ok": False, "error": "缺 wbhook_client.js"}

    notes = []
    if not _bak(html).is_file():
        shutil.copy2(html, _bak(html))
    if not _bak(bundle).is_file():
        shutil.copy2(bundle, _bak(bundle))

    ht = html.read_text(encoding="utf-8")
    in_body = "</body>" in ht and HTML_TAG in ht.split("</body>")[0]
    if MARK not in ht or not in_body:
        if "</body>" not in ht:
            return {"ok": False, "error": "workbench.html 没有 </body>"}
        ht = ht.replace(HTML_TAG + "\n", "").replace(HTML_TAG, "")
        ht = ht.replace("</body>", "\t" + HTML_TAG + "\n\t</body>", 1)
        html.write_text(ht, encoding="utf-8")
        notes.append("html")
    _write_client(html.with_name(SCRIPT_NAME), port)

    bt = bundle.read_text(encoding="utf-8", errors="replace")
    bt2, data_ok = _patch_bundle(bt, "data")
    bt3, chat_ok = _patch_bundle(bt2, "chat")
    bt4, self_ok = _patch_self_data(bt3)
    if bt4 != bt:
        bundle.write_text(bt4, encoding="utf-8")
        notes.append("bundle")
    if not data_ok and not self_ok:
        notes.append("data桥未挂上（打分找不到 composerDataService，软失败）")
    return {
        "ok": True,
        "html": str(html),
        "bundle": str(bundle),
        "data_hooked": data_ok or DATA_MARK in bt4 or self_ok,
        "self_data_hooked": self_ok or SELF_DATA_MARK in bt4,
        "chat_hooked": chat_ok or CHAT_MARK in bt4,
        "note": "已注入 workbench 桥（{}）。现窗口要 Reload 才挂上；挂不上软失败，对话不受影响。".format(
            "+".join(notes) or "已在"),
    }


# ---------- 运行时：渲染进程心跳 / 命令队列 / 回合快照 ----------
_LOCK = threading.Lock()
_CMDS: list[dict] = []
_ACKS: dict[str, dict] = {}
_TURNS: dict[str, dict] = {}
_WATCH: set[str] = set()
_BEAT = {"at": 0.0, "ready": False, "chat": False}


def reset_runtime():
    with _LOCK:
        _CMDS.clear()
        _ACKS.clear()
        _TURNS.clear()
        _WATCH.clear()
        _BEAT.update(at=0.0, ready=False, chat=False)


def beat(info=None):
    info = info if isinstance(info, dict) else {}
    with _LOCK:
        _BEAT["at"] = time.time()
        _BEAT["ready"] = bool(info.get("ready"))
        _BEAT["chat"] = bool(info.get("chat"))
    return {"ok": True, "ready": _BEAT["ready"]}


def hook_alive(max_age=8.0):
    with _LOCK:
        return (time.time() - float(_BEAT.get("at") or 0)) < max_age and bool(_BEAT.get("ready"))


def watch(composer_id, on=True):
    cid = str(composer_id or "").strip()
    if not cid:
        return
    with _LOCK:
        if on:
            _WATCH.add(cid)
        else:
            _WATCH.discard(cid)


def enqueue(action, **payload):
    req = {"id": uuid.uuid4().hex[:12], "action": str(action), **payload}
    with _LOCK:
        _CMDS.append(req)
    return req["id"]


def poll_payload():
    with _LOCK:
        cmds = list(_CMDS)
        _CMDS.clear()
        watch_ids = list(_WATCH)
    return {"ok": True, "cmds": cmds, "watch": watch_ids, "ready": hook_alive()}


def ack(req_id, result=None):
    with _LOCK:
        _ACKS[str(req_id)] = result if isinstance(result, dict) else {"ok": False, "error": "空回执"}
    return {"ok": True}


def ingest(snaps):
    rows = snaps if isinstance(snaps, list) else [snaps]
    now = time.time()
    with _LOCK:
        for row in rows:
            if not isinstance(row, dict):
                continue
            cid = str(row.get("composerId") or "").strip()
            if not cid:
                continue
            item = dict(row)
            item["recv_at"] = now
            _TURNS[cid] = item
            if len(_TURNS) > 80:
                old = next(iter(_TURNS))
                _TURNS.pop(old, None)
    return {"ok": True, "n": len(rows)}


def latest_turn(composer_id):
    cid = str(composer_id or "").strip()
    with _LOCK:
        row = _TURNS.get(cid)
        return dict(row) if row else None


def _norm_status(st):
    s = str(st or "").lower()
    if s in ("loading", "running", "pending", "in_progress", ""):
        return "running"
    if s in ("completed", "success", "done"):
        return "done"
    if s in ("error", "cancelled"):
        return s
    return s or "running"


def _classify(name):
    try:
        import cursor_turns
        return cursor_turns.classify_tool(name)
    except Exception:
        return "tool", (name or "工具")


def steps_from_snap(snap):
    """把桥客户端的薄步骤收成控制台时间线用的 step dict。"""
    rows = snap.get("steps") if isinstance(snap, dict) else None
    out = []
    for raw in rows or []:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or "")
        bid = str(raw.get("id") or raw.get("bubbleId") or "")
        if kind == "thinking":
            out.append({"kind": "thinking", "id": bid or "hook-think",
                        "text": str(raw.get("text") or ""), "at": 0})
            continue
        if kind == "text":
            out.append({"kind": "text", "id": bid or "hook-text",
                        "text": str(raw.get("text") or ""), "at": 0})
            continue
        name = str(raw.get("name") or "")
        if not name and kind != "tool":
            continue
        tk, label = _classify(name)
        out.append({
            "kind": "tool", "id": bid or name, "tool": tk, "label": label,
            "name": name, "status": _norm_status(raw.get("status")),
            "summary": str(raw.get("summary") or ""),
            "why": str(raw.get("why") or ""),
            "result": str(raw.get("result") or ""),
        })
    thinking = str((snap or {}).get("thinking") or "").strip()
    if thinking:
        has = False
        for s in reversed(out):
            if s.get("kind") == "thinking":
                if len(thinking) >= len(s.get("text") or ""):
                    s["text"] = thinking[:3000]
                has = True
                break
        if not has:
            out.insert(0, {"kind": "thinking", "id": "hook-think",
                           "text": thinking[:3000], "at": 0})
    return out


def overlay_turn(turn, snap, now=None, max_age=4.0):
    """桥内存快照盖到读库回合上。我们自己的桥，不读 Bajie 任何文件。

    读库有 WAL 约 8s 天花板；桥 250ms 一拍。新鲜快照的步骤/思考/正文优先，
    读库已有的 diff / 完整结果按 bubbleId 补回去。
    """
    if not isinstance(snap, dict) or not snap.get("ok"):
        return turn
    now = time.time() if now is None else float(now)
    try:
        age = now - float(snap.get("recv_at") or 0)
    except (TypeError, ValueError):
        return turn
    if age >= float(max_age):
        return turn
    turn = dict(turn or {})
    hook_steps = steps_from_snap(snap)
    db_steps = [s for s in (turn.get("steps") or []) if isinstance(s, dict)]
    by_id = {str(s.get("id")): dict(s) for s in db_steps if s.get("id")}
    merged, seen = [], set()
    for hs in hook_steps:
        hid = str(hs.get("id") or "")
        db = by_id.get(hid)
        if db:
            item = dict(db)
            if hs.get("kind") == "thinking" and hs.get("text"):
                if len(hs["text"]) >= len(item.get("text") or ""):
                    item["text"] = hs["text"]
            if hs.get("kind") == "tool":
                if hs.get("status"):
                    item["status"] = hs["status"]
                if hs.get("why") and not item.get("why"):
                    item["why"] = hs["why"]
                if hs.get("summary") and not item.get("summary"):
                    item["summary"] = hs["summary"]
                if hs.get("result") and len(str(hs["result"])) > len(str(item.get("result") or "")):
                    item["result"] = hs["result"]
            merged.append(item)
        else:
            merged.append(hs)
        if hid:
            seen.add(hid)
    for ds in db_steps:
        did = str(ds.get("id") or "")
        if did and did not in seen:
            merged.append(ds)
    if merged:
        turn["steps"] = merged
        turn["total_steps"] = max(int(turn.get("total_steps") or 0), len(merged))
    reply = str(snap.get("reply") or "")
    if reply:
        cur = turn.get("reply")
        db_text = cur.get("text") if isinstance(cur, dict) else ""
        if (not db_text) or len(reply) >= len(str(db_text)):
            turn["reply"] = {"kind": "text", "text": reply}
    turn["live"] = bool(snap.get("live") or turn.get("live"))
    turn["via"] = "wbhook"
    turn["hook_age"] = round(age, 2)
    turn["sig"] = "hook:{}:{}:{}".format(
        len(merged), len(reply), int(snap.get("at") or 0))
    return turn


def rename_many(pairs, timeout=2.5):
    """桥活着就排队改名并等回执。没心跳立刻 {}，调用方走键盘兜底。"""
    pairs = [(str(a).strip(), str(b).strip()) for a, b in (pairs or []) if a and b]
    if not pairs or not hook_alive():
        return {}
    reqs = {enqueue("rename", composerId=cid, name=name): cid for cid, name in pairs}
    deadline = time.time() + max(0.4, float(timeout))
    done = {}
    pending = dict(reqs)
    while pending and time.time() < deadline:
        with _LOCK:
            for req, cid in list(pending.items()):
                if req in _ACKS:
                    r = _ACKS.pop(req)
                    if r and r.get("ok"):
                        done[cid] = r
                    pending.pop(req, None)
        if pending:
            time.sleep(0.05)
    return done


def uninstall(app=None):
    html = _html_path(app)
    bundle = _bundle_path(app)
    if html is None:
        return {"ok": False, "error": "找不到 Cursor workbench.html"}
    js = html.with_name(SCRIPT_NAME)
    for p in (html, bundle):
        if p is None or not p.is_file():
            continue
        bak = _bak(p)
        if bak.is_file():
            shutil.copy2(bak, p)
            try:
                bak.unlink()
            except OSError:
                pass
        elif p == html:
            text = p.read_text(encoding="utf-8")
            p.write_text(text.replace(HTML_TAG + "\n", "").replace(HTML_TAG, ""),
                         encoding="utf-8")
    if js.is_file():
        try:
            js.unlink()
        except OSError:
            pass
    return {"ok": True, "note": "已卸 workbench 桥"}
