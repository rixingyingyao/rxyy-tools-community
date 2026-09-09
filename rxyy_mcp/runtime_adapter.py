"""Session identity and evidence for native Codex and hosted ChatGPT clients.

The MCP daemon is shared by clients. Its PID, heartbeat and global model config
cannot identify a task, or establish whether that task is still running.
"""
import time
import uuid


KINDS = ("cursor", "codex", "chatgpt", "unknown")
SNAPSHOT_FIELDS = ("runtime_kind", "native_thread_id")


def kind(session):
    # Snapshots from before client detection belong to the legacy Cursor path.
    value = str(getattr(session, "runtime_kind", "") or "cursor").lower()
    return value if value in KINDS else "unknown"


def is_native(session):
    return kind(session) != "cursor"


def valid_thread_id(value):
    try:
        parsed = uuid.UUID(str(value))
        return str(parsed) if str(parsed) == str(value).lower() else ""
    except (ValueError, TypeError, AttributeError):
        return ""


def apply_metadata(session, message):
    value = str(message.get("runtime_kind") or "").strip().lower()
    if value not in KINDS:
        return
    changed = kind(session) != value
    session.runtime_kind = value
    if changed:
        session.native_thread_id = ""
        session.native_turn_cache = {}
        session.native_turn_view = None
        session.model_info = None
        session.model_probe_at = 0
        session.reported_model = ""
        session.live_step_cache = None
        session.live_cache = None
    if value != "cursor":
        # A takeover can reuse a persistent-console ID across different apps.
        # Do not let an old verified Cursor binding survive that transition.
        session.cursor_uuid = None
        session.uuid_verified = False
        session.transcript_path = None
        session.cursor_title = None
        session.ext_instance = ""
        session.death_info = None
        session.death_probe_uid = None
    thread_id = str(message.get("native_thread_id") or "").strip()
    if value == "codex":
        thread_id = valid_thread_id(thread_id)
    elif len(thread_id) > 512 or any(ord(c) < 32 for c in thread_id):
        thread_id = ""
    if thread_id and thread_id != getattr(session, "native_thread_id", ""):
        session.native_thread_id = thread_id
        session.native_turn_cache = {}
        session.native_turn_view = None
        session.native_turn_read_at = 0
        session.reported_model = ""
        session.model_info = None
        session.model_probe_at = 0
        session.live_step_cache = None


def capabilities(session):
    client = kind(session)
    return {
        "runtime": client,
        "cursor_window": client == "cursor",
        "timeline": bool(getattr(session, "cursor_uuid", None)) if client == "cursor"
                    else client == "codex" and bool(valid_thread_id(
                        getattr(session, "native_thread_id", ""))),
        "context_transfer": True,
        "local_thread": client == "codex",
        "native_text": client == "codex" and bool(valid_thread_id(
            getattr(session, "native_thread_id", ""))),
    }


def _read_codex_turn(thread_id, **kwargs):
    try:
        import codex_turns
    except ImportError:
        return None
    return codex_turns.read_turn(thread_id, **kwargs)


def _read_desktop_turn(thread_id, max_steps):
    try:
        import codex_desktop
        return codex_desktop.read_turn(thread_id, max_steps=max_steps)
    except (ImportError, ValueError, TypeError, KeyError):
        return None


def read_native_turn(session, max_steps=100, now=None):
    if kind(session) != "codex":
        return None
    thread_id = valid_thread_id(getattr(session, "native_thread_id", ""))
    if not thread_id:
        return None
    now = time.time() if now is None else now
    max_steps = max(1, min(int(max_steps or 100), 2000))
    desktop = _read_desktop_turn(thread_id, max_steps)
    if desktop is not None:
        if valid_thread_id(getattr(session, "native_thread_id", "")) != thread_id:
            return None
        session.native_turn_view = desktop
        session.native_turn_view_id = thread_id
        return desktop
    last = float(getattr(session, "native_turn_read_at", 0) or 0)
    if (0 <= now - last < 0.5 and getattr(session, "native_turn_steps", None) == max_steps
            and getattr(session, "native_turn_view_id", None) == thread_id):
        return getattr(session, "native_turn_view", None)
    cache = getattr(session, "native_turn_cache", None)
    if cache is None:
        cache = session.native_turn_cache = {}
    view = _read_codex_turn(thread_id, max_steps=max_steps, cache=cache, now=now)
    if valid_thread_id(getattr(session, "native_thread_id", "")) != thread_id:
        return None  # 读取期间接手换绑了，旧任务结果不能污染新任务缓存。
    session.native_turn_read_at = now
    session.native_turn_steps = max_steps
    session.native_turn_view = view
    session.native_turn_view_id = thread_id
    return view


def native_liveness(session, now):
    client = kind(session)
    label = {"codex": "Codex", "chatgpt": "ChatGPT"}.get(client, "客户端")
    zt = float(getattr(session, "agent_status_ts", 0) or 0)
    zhi = float(getattr(session, "last_zhi_ts", 0) or 0)
    seen = max(zt, zhi)
    seen_age = max(0, now - seen) if seen else None
    turn = read_native_turn(session, now=now) or {}
    updated = float(turn.get("updated_at") or 0)
    age = max(0, now - updated) if updated else None
    status = str(getattr(session, "agent_status", "") or "")
    pending = getattr(session, "pending", None)
    evidence = [label + " 会话；MCP 服务心跳不代表任务在线"]
    if seen_age is not None:
        evidence.append("本任务上次工具交互 {} 秒前".format(int(seen_age)))
    if age is not None:
        evidence.append("Codex 任务记录 {} 秒前".format(int(age)))
    if pending is not None:
        evidence.append("控制台有待回复内容，保留到领取")
    if client == "chatgpt":
        evidence.append("云端会话无法读取本机窗口或任务日志")
    generating = bool(turn.get("generating")) and age is not None and age < 180

    def out(state, text, sure=False):
        tier = "active" if state in ("working", "waiting", "online") else (
            "suspect" if state in ("stalled", "lost", "recon") else "quiet")
        return {"state": state, "label": text, "sure": sure, "tier": tier,
                "evidence": evidence, "generating": generating,
                "hb_age": None, "ide_age": None if age is None else int(age),
                "zt_age": None if not zt else int(max(0, now - zt)),
                "write_age": None, "proc_age": None, "death": None}

    if turn.get("desktop_connected"):
        evidence.append("已连接此任务所属的 Codex 桌面进程")
        if turn.get("terminal"):
            generating = False
            return out("turn_done", "Codex 本轮已结束", True)
        if (turn.get("compaction") or {}).get("status") in ("running", "inProgress", "compacting"):
            return out("working", "Codex 正在压缩上下文", True)
        questions = [q for q in turn.get("native_questions", []) if q.get("status") == "pending"]
        if questions:
            return out("waiting", "Codex 有待回答问题", True)
        if turn.get("generating"):
            generating = True
            return out("working", "Codex 正在执行", True)
        generating = False
        return out("online", "Codex 已连接", True)

    if turn.get("terminal") and updated >= seen:
        generating = False
        return out("turn_done", label + " 本轮已结束", True)
    if status in ("complete", "completed", "task_complete", "dev_complete",
                  "ready", "session_ended") and zt and zt >= updated:
        generating = False
        return out("turn_done", label + " 已完成" if status != "ready" else label + " 待机", True)
    if status in ("blocked", "failed", "error") and zt >= updated:
        return out("stalled", label + " 需要处理", True)
    if pending is not None and seen_age is not None and seen_age < 60:
        if (pending.get("options") or pending.get("card")) and not getattr(session, "buffered_reply", None):
            return out("waiting", "有待回复问题", True)
    if generating:
        evidence.append("近期 rollout 有执行活动；不是宿主进程的实时确认")
        return out("working", "Codex 近期有执行活动")
    if seen_age is not None and seen_age < 180:
        return out("online", label + " 刚有任务交互，执行状态待确认")
    if (turn.get("generating") and not turn.get("terminal")) or getattr(session, "processing_since", None):
        evidence.append("未见本轮结束；长思考、工具等待和客户端断开均可能暂无新记录")
        return out("unknown", label + " 等待新的活动信号")
    return out("unknown", label + " 暂无活动信号")


def update_native_model(session, now):
    if now - float(getattr(session, "model_probe_at", 0) or 0) < 3:
        return
    turn = read_native_turn(session, now=now) or {}
    model = turn.get("model") or getattr(session, "reported_model", "")
    if model:
        from session_locator import model_info_from_name
        session.model_info = model_info_from_name(str(model), str(turn.get("effort") or ""))
    session.model_probe_at = now


def takeover_prompt(session, recent_text, target="codex", stay_put=False):
    cid = str(getattr(session, "conv_key", "") or "")
    lines = [
        "继续完成这个任务：{}。".format(getattr(session, "name", "") or "未完成任务"),
        "项目目录：{}".format(getattr(session, "task_root", "") or getattr(session, "cwd", "")),
        ("来源rxyy MCP 会话 ID：{}，仅作读记录；全程沿用你当前会话的 conversation_id。".format(cid)
         if stay_put else "rxyy MCP 会话 ID：{}；接手后继续使用此 ID。".format(cid)),
        "先读项目 AGENTS.md、README 和下面的会话记录，核对现有改动及剩余要求后继续。",
        "保留其他会话的未提交改动；已有测试与完成记录需核实，不要从头重做。",
        "按当前宿主和用户的指令使用工具；只有已安装且真实可用的 MCP 才能调用。",
        "接手时向rxyy MCP上报 zt，带 conversation_id、project_path、task_name；runtime 按当前宿主填写 cursor、codex 或 chatgpt；"
        "thread_id 填本次新任务自己的原生 ID，不要填下面的来源任务 ID。",
        "进度可用 zt，成果可用 zhi(wait=false) 同步到控制台；需要反馈时优先使用宿主的原生交互。",
    ]
    source = getattr(session, "native_thread_id", "")
    if source:
        lines.append("来源原生任务 ID（只作历史来源）：{}".format(source))
    history = str(getattr(session, "file_path", "") or "")
    if history:
        lines.append("完整会话记录：{}".format(history))
    trail = getattr(session, "zt_trail", None) or []
    if trail:
        lines.extend(["最近上报：", *[str(x) for x in trail[-5:]]])
    lines.extend(["最近对话摘要（完整要求以会话记录和用户的新指令为准）：", recent_text])
    return "\n\n".join(lines)
