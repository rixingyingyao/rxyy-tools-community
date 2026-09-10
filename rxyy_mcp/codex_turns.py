# -*- coding: utf-8 -*-
"""只读还原本地 Codex rollout 的当前时间线。

Codex 的原生记录不是 Cursor 的 sqlite 气泡：一个 thread 可以在同一个
``rollout-*.jsonl`` 里连续写多个 ``task_started``。本适配层只读取
``<codex_home>/sessions`` 下、文件名末尾精确匹配 thread UUID 的 rollout，
并只把已经完整落盘且属于最近一轮的可见文本、reasoning summary、工具调用/结果
和少量状态事件投影成 ``cursor_turns.read_turn`` 兼容的结构。

安全边界是这个模块的一部分：不读取 auth/config/cache，不猜工作目录，不把
``session_meta`` 的长规则或 ``encrypted_content`` 带入返回值。路径和文件偏移通过
调用方传入的 cache 复用；只保存最近 2000 步的可见摘要，不保存整份原始 rollout。
任务标题只读 Codex 自己的 ``session_index.jsonl``；桌面实时 state 也只投影白名单字段。
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import threading
import uuid as _uuid
from collections import deque
from pathlib import Path


DEFAULT_MAX_STEPS = 100
TAIL_BYTES = 2 * 1024 * 1024
MAX_META_BYTES = 2 * 1024 * 1024
MAX_TEXT_CHARS = 12000
MAX_RESULT_CHARS = 12000
MAX_SUMMARY_CHARS = 6000
MAX_CACHE_THREADS = 32
MAX_STORED_STEPS = 2000
_READ_LOCK = threading.RLock()
LIVE_WINDOW_SECS = 30.0
_CACHE_KEY = "__codex_turns__"
_SAFE_TOKEN_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)
_TERMINAL_EVENTS = {"task_complete": "completed", "turn_aborted": "aborted"}
_ASYNC_QUESTION_TOOL_NAMES = {
    "request_user_input_async",
    "functions.request_user_input_async",
}


def _clip(value, limit=MAX_TEXT_CHARS):
    text = value if isinstance(value, str) else str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _one_line(value, limit=400):
    return _clip(" ".join(str(value or "").split()), limit)


def _iso_ts(value):
    if not isinstance(value, str) or not value.strip():
        return 0.0
    text = value.strip()
    try:
        from datetime import datetime, timezone

        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).astimezone(timezone.utc).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _epoch(value):
    """把 rollout 中常见的 ISO、秒、毫秒时间转成 epoch 秒。"""
    if isinstance(value, str):
        iso = _iso_ts(value)
        if iso:
            return iso
        try:
            value = float(value)
        except (TypeError, ValueError):
            return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number > 100000000000.0:
        number /= 1000.0
    return number if number > 0 else 0.0


def _canonical_uuid(value):
    if isinstance(value, _uuid.UUID):
        return str(value)
    if not isinstance(value, str):
        return ""
    try:
        return str(_uuid.UUID(value.strip()))
    except (ValueError, AttributeError, TypeError):
        return ""


def _inside(path, root):
    try:
        path_name = os.path.normcase(os.path.abspath(os.fspath(path)))
        root_name = os.path.normcase(os.path.abspath(os.fspath(root)))
        return os.path.commonpath((path_name, root_name)) == root_name
    except (OSError, ValueError, TypeError):
        return False


def _reparse_or_symlink(path):
    """拒绝普通 symlink，也尽量拒绝 Windows reparse point/junction。"""
    try:
        if os.path.islink(path):
            return True
        info = os.stat(path, follow_symlinks=False)
        attributes = getattr(info, "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except OSError:
        return True


def _safe_sessions_root(codex_home):
    if codex_home is None:
        codex_home = os.environ.get("CODEX_HOME") or (Path.home() / ".codex")
    try:
        home = Path(codex_home).expanduser()
        root = home / "sessions"
        if (not home.is_dir() or _reparse_or_symlink(home)
                or not root.is_dir() or _reparse_or_symlink(root)):
            return None
        # Windows hosted runner 的 TEMP 可能含 8.3 短目录名；realpath 会把它展开成
        # 长目录名。两种拼写仍指向同一棵树，不能仅因字符串不同就拒绝。home 与
        # sessions 本身的 reparse/symlink 已在上面拒绝，候选文件仍由
        # _safe_candidate 同时按原路径和 realpath 限制在 sessions 内。
        root_abs = os.path.abspath(os.fspath(root))
        return Path(root_abs)
    except (OSError, TypeError, ValueError):
        return None


def _safe_candidate(root, path):
    try:
        root_abs = os.path.abspath(os.fspath(root))
        path_abs = os.path.abspath(os.fspath(path))
        if not _inside(path_abs, root_abs):
            return False
        if not _inside(os.path.realpath(path_abs), os.path.realpath(root_abs)):
            return False
        current = root_abs
        relative = os.path.relpath(path_abs, root_abs)
        if relative == os.curdir:
            return False
        for part in Path(relative).parts:
            current = os.path.join(current, part)
            if _reparse_or_symlink(current):
                return False
        return True
    except (OSError, ValueError, TypeError):
        return False


def _thread_title(root, thread_id):
    """从 Codex 的公开 session index 读取原生任务标题；不读取 global state/cache。"""
    index = Path(root).parent / "session_index.jsonl"
    try:
        if (not index.is_file() or _reparse_or_symlink(index)
                or not _inside(index, Path(root).parent)):
            return ""
        if index.stat().st_size > 16 * 1024 * 1024:
            return ""
        found = ""
        with index.open("rb") as stream:
            while True:
                line = stream.readline(MAX_META_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_META_BYTES or not line.endswith(b"\n"):
                    continue
                row = _load_json_line(line)
                if isinstance(row, dict) and _canonical_uuid(row.get("id")) == thread_id:
                    found = _clip(row.get("thread_name"), 500)
        return found
    except OSError:
        return ""


def _iter_rollouts(root, thread_id):
    """只在 sessions 根下手动遍历，且不跟随任何 symlink/reparse point。"""
    suffix = "-" + thread_id + ".jsonl"
    pending = [root]
    visited = 0
    while pending and visited < 10000:
        directory = pending.pop()
        visited += 1
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                name = entry.name
                if (name.startswith("rollout-") and name.lower().endswith(suffix)
                        and _safe_candidate(root, entry.path)):
                    yield Path(entry.path)
            except OSError:
                continue


def _load_json_line(raw):
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8-sig", "replace")
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _session_meta_matches(root, path, thread_id):
    if not _safe_candidate(root, path):
        return False
    try:
        with open(path, "rb") as stream:
            first = stream.readline(MAX_META_BYTES + 1)
        if len(first) > MAX_META_BYTES:
            return False
        record = _load_json_line(first)
        if not isinstance(record, dict) or record.get("type") != "session_meta":
            return False
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return False
        # 只验证 id；base_instructions/cwd 等字段不向下游传播。
        return _canonical_uuid(payload.get("id")) == thread_id
    except (OSError, ValueError):
        return False


def _file_stat(path):
    try:
        info = os.stat(path, follow_symlinks=False)
        return {
            "size": int(info.st_size),
            "mtime_ns": int(getattr(info, "st_mtime_ns", int(info.st_mtime * 1000000000))),
            "inode": int(getattr(info, "st_ino", 0)),
        }
    except OSError:
        return None


def _cache_threads(cache):
    if not isinstance(cache, dict):
        return None
    state = cache.get(_CACHE_KEY)
    if not isinstance(state, dict):
        state = {"version": 1, "threads": {}}
        cache[_CACHE_KEY] = state
    threads = state.get("threads")
    if not isinstance(threads, dict):
        threads = {}
        state["threads"] = threads
    return threads


def _cache_get_path(root, thread_id, cache):
    threads = _cache_threads(cache)
    cached = threads.get(thread_id) if threads is not None else None
    if isinstance(cached, dict):
        path_text = cached.get("path")
        if path_text and _safe_candidate(root, path_text):
            path = Path(path_text)
            if _session_meta_matches(root, path, thread_id):
                return path, cached
        threads.pop(thread_id, None)

    candidates = list(_iter_rollouts(root, thread_id))
    candidates.sort(key=lambda p: (_file_stat(p) or {}).get("mtime_ns", 0), reverse=True)
    for path in candidates:
        if not _session_meta_matches(root, path, thread_id):
            continue
        meta = {"path": str(path), "turn_id": "", "terminal": False}
        if threads is not None:
            threads[thread_id] = meta
            while len(threads) > MAX_CACHE_THREADS:
                threads.pop(next(iter(threads)), None)
        return path, meta
    return None, None


def _visible_text(value, limit=MAX_TEXT_CHARS):
    """只抽取有 text 字段的可见内容；不会递归读取 encrypted_content。"""
    if isinstance(value, str):
        return _clip(value, limit)
    if isinstance(value, dict):
        pieces = []
        text = value.get("text")
        if isinstance(text, str):
            pieces.append(text)
        for key in ("content", "output"):
            nested = value.get(key)
            if nested is not None:
                part = _visible_text(nested, limit)
                if part:
                    pieces.append(part)
        return _clip("\n".join(pieces), limit)
    if isinstance(value, (list, tuple)):
        pieces = []
        for item in value[:128]:
            part = _visible_text(item, limit)
            if part:
                pieces.append(part)
            if sum(len(x) for x in pieces) >= limit:
                break
        return _clip("\n".join(pieces), limit)
    return ""


def _summary_text(value):
    return _visible_text(value, MAX_SUMMARY_CHARS).strip()


def _question_item_id(item_id, index):
    """与 Codex Desktop mqn 的 questionItemId 生成规则逐字一致。"""
    return json.dumps(["request_user_input_async", str(item_id or ""), int(index)],
                      ensure_ascii=False, separators=(",", ":"))


def _is_async_native_question_tool(name):
    """Accept the Desktop's legacy and namespaced async-question tool names."""
    return str(name or "").strip().casefold() in _ASYNC_QUESTION_TOOL_NAMES


def _normalize_questions(value, item_id):
    questions = []
    for index, raw in enumerate(value if isinstance(value, list) else []):
        if not isinstance(raw, dict):
            continue
        title = _clip(raw.get("title") or raw.get("question"), 2000)
        header = _clip(raw.get("header"), 160)
        options = []
        for option in raw.get("options") if isinstance(raw.get("options"), list) else []:
            if isinstance(option, str):
                options.append({"label": _clip(option, 1000), "description": ""})
            elif isinstance(option, dict):
                options.append({"label": _clip(option.get("label"), 1000),
                                "description": _clip(option.get("description"), 2000)})
        questions.append({
            "id": _clip(raw.get("id"), 500) or _question_item_id(item_id, index),
            "title": title, "header": header,
            "question": _clip(raw.get("question"), 2000) or title,
            "options": options,
        })
    return questions


def _question_answers_from_text(text):
    """只认 Desktop 自己生成的 exact envelope，普通用户消息绝不冒充问题回答。"""
    if not isinstance(text, str):
        return []
    start_tag = "<send_user_message_question_reply>"
    end_tag = "</send_user_message_question_reply>"
    text = text.strip()
    if not text.startswith(start_tag) or not text.endswith(end_tag):
        return []
    body = text[len(start_tag):-len(end_tag)].strip()
    try:
        rows = json.loads(body)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    answers = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not row.get("questionItemId"):
            continue
        answers.append({"question_id": _clip(row.get("questionItemId"), 500),
                        "question": _clip(row.get("question"), 2000),
                        "answer": _clip(row.get("answer"), 4000)})
    return answers


def _json_object(value):
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _safe_number(value):
    if isinstance(value, bool):
        return int(value)
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else 0


def _safe_usage(value):
    if not isinstance(value, dict):
        return {}
    return {key: number for key in _SAFE_TOKEN_KEYS
            if (number := _safe_number(value.get(key))) is not None}


def _context_from_info(info):
    if not isinstance(info, dict):
        return None
    total = _safe_usage(info.get("total_token_usage"))
    last = _safe_usage(info.get("last_token_usage"))
    limit = _safe_number(info.get("model_context_window"))
    if not total and not last and limit is None:
        return None
    out = {"total": total, "last": last, "source": "codex_last_token_usage"}
    out.update(last)
    if limit is not None:
        out["limit"] = limit
        out["model_context_window"] = limit
        # total_token_usage 累加每次模型调用，长任务会远超上下文窗口。
        # 最近调用的输入+输出才是占用近似值；缓存/思考已包含在各自总量里。
        used = last.get("total_tokens")
        if used is None and "input_tokens" in last:
            used = last["input_tokens"] + last.get("output_tokens", 0)
        if used is not None and limit > 0:
            pct = round(max(0.0, min(used * 100.0 / limit, 100.0)), 1)
            if pct >= 92.0:
                level, hint = "critical", "接近上下文窗口；Codex 可能进行自动压缩"
            elif pct >= 80.0:
                level, hint = "high", "上下文占用较高，后续可能自动压缩"
            elif pct >= 65.0:
                level, hint = "warn", "上下文占用已超过 65%"
            else:
                level, hint = "ok", ""
            out.update({
                "used": used,
                "pct": pct,
                "level": level,
                "label": "上下文约 {:.0f}%（{} / {}）".format(
                    pct, _token_label(used), _token_label(limit)),
                "hint": hint,
            })
    return out


def _desktop_context(info):
    """Normalize the desktop's camelCase token snapshot without exposing other state."""
    if not isinstance(info, dict):
        return None

    def usage(value):
        if not isinstance(value, dict):
            return {}
        aliases = {
            "input_tokens": "inputTokens", "cached_input_tokens": "cachedInputTokens",
            "cache_write_input_tokens": "cacheWriteInputTokens",
            "output_tokens": "outputTokens", "reasoning_output_tokens": "reasoningOutputTokens",
            "total_tokens": "totalTokens",
        }
        return {key: value.get(key, value.get(alias)) for key, alias in aliases.items()}

    return _context_from_info({
        "total_token_usage": usage(info.get("total_token_usage") or info.get("totalTokenUsage")
                                     or info.get("total")),
        "last_token_usage": usage(info.get("last_token_usage") or info.get("lastTokenUsage")
                                    or info.get("last")),
        "model_context_window": info.get("model_context_window", info.get("modelContextWindow")),
    })


def _token_label(value):
    value = int(value or 0)
    return "{:.0f}K".format(value / 1000.0) if value < 1000000 else "{:.2f}M".format(value / 1000000.0)


def _tool_input(payload):
    if payload.get("type") == "function_call":
        raw = payload.get("arguments", payload.get("input"))
    else:
        raw = payload.get("input", payload.get("arguments"))
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str):
        return ""
    text = _clip(raw, MAX_RESULT_CHARS)
    try:
        parsed = json.loads(text)
        return parsed
    except (TypeError, ValueError, json.JSONDecodeError):
        return text


def _tool_summary(name, argument):
    if isinstance(argument, dict):
        # code-mode 的原始 code 是时间线上最有用的「命令」，不要只显示一坨 JSON。
        if name in ("exec", "functions.exec") or name.endswith(".exec"):
            for key in ("code", "cmd", "command", "script"):
                value = argument.get(key)
                if isinstance(value, str) and value.strip():
                    return _one_line(value, 1000), _clip(value, MAX_RESULT_CHARS)
        for key in ("command", "cmd", "query", "path", "url", "prompt", "description"):
            value = argument.get(key)
            if isinstance(value, str) and value.strip():
                return _one_line(value, 1000), ""
        try:
            return _one_line(json.dumps(argument, ensure_ascii=False, sort_keys=True), 1000), ""
        except (TypeError, ValueError):
            return "", ""
    return _one_line(argument, 1000), ""


def _tool_status(value, default="running"):
    status = str(value or "").lower()
    if status in ("completed", "complete", "success", "succeeded", "done"):
        return "done"
    if status in ("error", "failed", "failure"):
        return "error"
    if status in ("aborted", "cancelled", "canceled"):
        return "aborted"
    if status in ("loading", "running", "pending", "in_progress", "in-progress", "inprogress"):
        return "running"
    return default


def _record(raw, at):
    """把一行 JSONL 压成不含原始大字段的小记录。"""
    if not isinstance(raw, dict):
        return None
    outer = raw.get("type")
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        return None
    ptype = payload.get("type")
    common = {"kind": "", "at": at}
    if outer == "compacted":
        return {**common, "kind": "context_compacted",
                "window_number": _safe_number(payload.get("window_number")),
                "first_window_id": _clip(payload.get("first_window_id"), 160),
                "previous_window_id": _clip(payload.get("previous_window_id"), 160),
                "window_id": _clip(payload.get("window_id"), 160),
                "response_id": _clip(payload.get("compaction_response_id"), 200)}
    if outer == "event_msg":
        if ptype == "task_started":
            return {**common, "kind": "task_started", "turn_id": _clip(payload.get("turn_id"), 100),
                    "started_at": _epoch(payload.get("started_at"))}
        if ptype in _TERMINAL_EVENTS:
            return {**common, "kind": ptype, "turn_id": _clip(payload.get("turn_id"), 100)}
        if ptype == "user_message":
            return {**common, "kind": "user", "text": _clip(_visible_text(payload.get("message")), MAX_TEXT_CHARS)}
        if ptype == "agent_message":
            return {**common, "kind": "agent_message", "text": _clip(_visible_text(payload.get("message")), MAX_TEXT_CHARS)}
        if ptype == "token_count":
            return {**common, "kind": "token_count", "info": _token_info(payload.get("info"))}
        if ptype == "item_completed" and isinstance(payload.get("item"), dict):
            item = payload["item"]
            item_type = str(item.get("type") or "")
            if item_type == "AgentMessage":
                questions = _normalize_questions(item.get("questions"), item.get("id"))
                text = _visible_text(item.get("content") or item.get("text"))
                if not questions and item.get("delivery") == "async" and text:
                    questions = [{"id": _clip(item.get("id"), 500), "title": _clip(text, 2000),
                                  "header": "", "question": _clip(text, 2000), "options": []}]
                if questions:
                    return {**common, "kind": "native_question_item",
                            "thread_id": _clip(payload.get("thread_id"), 160),
                            "turn_id": _clip(payload.get("turn_id"), 160),
                            "item_id": _clip(item.get("id"), 200),
                            "call_id": _clip(item.get("id"), 200),
                            "phase": _clip(item.get("phase"), 80),
                            "delivery": _clip(item.get("delivery"), 80),
                            "questions": questions}
                if text:
                    return {**common, "kind": "assistant", "text": _clip(text),
                            "id": _clip(item.get("id"), 120),
                            "phase": _clip(item.get("phase"), 80)}
        return None
    if outer == "turn_context":
        return {**common, "kind": "turn_context",
                "model": _clip(payload.get("model"), 200),
                "effort": _clip(payload.get("effort"), 80)}
    if outer != "response_item":
        return None
    if ptype == "message":
        role = str(payload.get("role") or "").lower()
        text = _visible_text(payload.get("content"))
        if role == "user":
            return {**common, "kind": "user", "text": _clip(text, MAX_TEXT_CHARS),
                    "id": _clip(payload.get("id"), 120),
                    "question_answers": _question_answers_from_text(text)}
        if role == "assistant" and text:
            return {**common, "kind": "assistant", "text": _clip(text, MAX_TEXT_CHARS),
                    "id": _clip(payload.get("id"), 120),
                    "phase": _clip(payload.get("phase"), 80)}
        return None
    if ptype == "agent_message":
        text = _visible_text(payload.get("content"))
        if text:
            return {**common, "kind": "subagent_message", "text": _clip(text),
                    "id": _clip(payload.get("id"), 120),
                    "author": _clip(payload.get("author"), 160),
                    "recipient": _clip(payload.get("recipient"), 160)}
        return None
    if ptype == "reasoning":
        text = _summary_text(payload.get("summary"))
        if not text:
            return None
        return {**common, "kind": "reasoning", "text": text,
                "id": _clip(payload.get("id"), 120)}
    if ptype in ("function_call", "custom_tool_call"):
        name = _clip(payload.get("name") or "tool", 240)
        argument = _tool_input({**payload, "type": ptype})
        summary, command = _tool_summary(name, argument)
        call_id = _clip(payload.get("call_id"), 240)
        step_id = _clip(payload.get("id") or call_id, 160)
        record = {**common, "kind": "tool_call", "id": step_id, "name": name,
                "call_id": call_id, "summary": summary, "command": command,
                "status": _tool_status(payload.get("status"), "running")}
        if _is_async_native_question_tool(name) and isinstance(argument, dict):
            metadata = payload.get("internal_chat_message_metadata_passthrough")
            metadata = metadata if isinstance(metadata, dict) else {}
            record["native_question"] = {
                "request_id": step_id, "call_id": call_id, "item_id": call_id,
                "thread_id": "", "turn_id": _clip(metadata.get("turn_id"), 160),
                "status": "pending", "delivery": "", "requested_at": at,
                "resolved_at": 0.0, "questions": _normalize_questions(
                    argument.get("questions"), call_id), "answers": {},
            }
        return record
    if ptype in ("function_call_output", "custom_tool_call_output"):
        text = _clip(_visible_text(payload.get("output")), MAX_RESULT_CHARS)
        return {**common, "kind": "tool_output", "id": _clip(payload.get("id"), 160),
                "call_id": _clip(payload.get("call_id"), 240),
                "text": text, "response": _json_object(text)}
    return None


def _token_info(info):
    if not isinstance(info, dict):
        return {}
    return {
        "total_token_usage": _safe_usage(info.get("total_token_usage")),
        "last_token_usage": _safe_usage(info.get("last_token_usage")),
        "model_context_window": _safe_number(info.get("model_context_window")),
    }


def _event_id(kind, turn_id, at, ordinal):
    seed = "{}|{}|{}".format(kind, turn_id or "", at or ordinal)
    return kind + ":" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def _event_step(record, status):
    kind = record["kind"]
    turn_id = record.get("turn_id") or ""
    label = {"task_started": "任务开始", "task_complete": "任务完成",
             "turn_aborted": "任务中止",
             "context_compacted": "上下文压缩完成"}.get(kind, kind)
    event = "context_compacted" if kind == "context_compacted" else kind
    native_id = record.get("window_id") or record.get("response_id")
    return {
        "kind": "event",
        "id": native_id or _event_id(kind, turn_id, record.get("at"), 0),
        "at": record.get("at") or 0.0,
        "event": event,
        "status": status,
        "text": label,
    }


def _append_tool_output(step, text):
    previous = step.get("result") or ""
    if text:
        step["result"] = _clip((previous + "\n" + text).strip() if previous else text,
                                MAX_RESULT_CHARS)
    step["status"] = "done"


def _make_steps(records, start_record, visible_start, terminal_record):
    steps = []
    calls = {}
    orphan_outputs = {}
    assistant_exists = any(r.get("kind") == "assistant" for r in records[visible_start:])
    ordinal = 0
    if start_record is not None:
        steps.append(_event_step(start_record, "running"))
    for record in records[visible_start:]:
        kind = record.get("kind")
        if kind == "assistant":
            steps.append({"kind": "text", "id": record.get("id") or _event_id("assistant", "", record.get("at"), ordinal),
                          "at": record.get("at") or 0.0, "text": record.get("text") or "",
                          "phase": record.get("phase") or ""})
        elif kind == "agent_message" and not assistant_exists and record.get("text"):
            steps.append({"kind": "text", "id": _event_id("agent_message", "", record.get("at"), ordinal),
                          "at": record.get("at") or 0.0, "text": record.get("text")})
        elif kind == "reasoning":
            steps.append({"kind": "thinking", "id": record.get("id") or _event_id("reasoning", "", record.get("at"), ordinal),
                          "at": record.get("at") or 0.0, "text": record.get("text") or ""})
        elif kind == "tool_call":
            step = {
                "kind": "tool", "id": record.get("id") or record.get("call_id") or _event_id("tool", "", record.get("at"), ordinal),
                "at": record.get("at") or 0.0, "tool": "shell" if (record.get("name") or "").endswith("exec") else "tool",
                "label": "运行命令" if (record.get("name") or "").endswith("exec") else "工具",
                "name": record.get("name") or "tool", "call_id": record.get("call_id") or "",
                "status": record.get("status") or "running", "summary": record.get("summary") or "",
                "result": "",
            }
            if record.get("command"):
                step["command"] = record["command"]
            calls[step["call_id"]] = step
            if step["call_id"] in orphan_outputs:
                for output in orphan_outputs.pop(step["call_id"]):
                    _append_tool_output(step, output)
            steps.append(step)
        elif kind == "tool_output":
            call_id = record.get("call_id") or ""
            target = calls.get(call_id)
            if target is None:
                # 尾部窗口可能从 output 开始，仍把已落盘结果显示出来；若后面
                # 同一窗口补到 call，再由 orphan_outputs 合并回原工具卡。
                if call_id:
                    orphan_outputs.setdefault(call_id, []).append(record.get("text") or "")
                else:
                    steps.append({"kind": "tool", "id": record.get("id") or _event_id("tool_output", "", record.get("at"), ordinal),
                                  "at": record.get("at") or 0.0, "tool": "tool", "label": "工具输出",
                                  "name": "tool output", "call_id": "", "status": "done",
                                  "summary": "", "result": record.get("text") or ""})
            else:
                _append_tool_output(target, record.get("text") or "")
        elif kind in _TERMINAL_EVENTS:
            steps.append(_event_step(record, _TERMINAL_EVENTS[kind]))
        elif kind == "context_compacted":
            steps.append(_event_step(record, "completed"))
        ordinal += 1
    for call_id, outputs in orphan_outputs.items():
        result = "\n".join(text for text in outputs if text)
        steps.append({"kind": "tool", "id": _event_id("tool_output", call_id, 0, ordinal),
                      "at": 0.0, "tool": "tool", "label": "工具输出", "name": "tool output",
                      "call_id": call_id, "status": "done", "summary": "",
                      "result": _clip(result, MAX_RESULT_CHARS)})
    # 如果工具调用落盘时没有 output，保留 running/done 原状；terminal 明确收口时，
    # 未完成调用是 aborted，供 UI 区分「安静」和「已中止」。
    if terminal_record and terminal_record.get("kind") == "turn_aborted":
        for step in steps:
            if step.get("kind") == "tool" and step.get("status") == "running":
                step["status"] = "aborted"
    return steps


def _reply(steps):
    texts = [step.get("text", "") for step in steps
             if step.get("kind") == "text" and step.get("role") != "user"
             and step.get("text")]
    if texts:
        step = next(step for step in reversed(steps)
                    if step.get("kind") == "text" and step.get("role") != "user"
                    and step.get("text"))
        return {"kind": "text", "text": step.get("text", ""),
                "step_id": step.get("id", ""), "phase": step.get("phase", "")}
    thoughts = [step.get("text", "") for step in steps
                if step.get("kind") == "thinking" and step.get("text")]
    if thoughts:
        return {"kind": "thinking", "text": thoughts[-1]}
    return None


def _sig(turn_id, steps, reply, terminal, model, effort, context, pending,
         native_questions=None, compaction=None, thread_title=""):
    parts = ["turn=" + str(turn_id or ""), "terminal=" + str(bool(terminal)),
             "model=" + str(model or ""), "effort=" + str(effort or ""),
             "pending=" + str(int(bool(pending))), "title=" + str(thread_title or "")]
    for step in steps:
        body = "|".join(str(step.get(key, "")) for key in
                         ("id", "kind", "event", "status", "call_id", "summary", "phase"))
        body += "|" + str(step.get("text") or step.get("result") or step.get("command") or "")
        parts.append(body)
    if reply:
        parts.append("reply=" + "|".join(str(reply.get(key, ""))
                                           for key in ("kind", "step_id", "phase", "text")))
    if context:
        parts.append("context=" + json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    if native_questions:
        parts.append("questions=" + json.dumps(native_questions, ensure_ascii=False,
                                                 sort_keys=True, separators=(",", ":")))
    if compaction:
        parts.append("compaction=" + json.dumps(compaction, ensure_ascii=False,
                                                  sort_keys=True, separators=(",", ":")))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:12]


def _new_stream_state():
    return {"offset": 0, "steps": deque(), "calls": {}, "count": 0, "turn_id": "",
            "started_at": 0, "updated_at": 0, "terminal": False, "status": "running",
            "model": "", "effort": "", "context": None, "user_text": "",
            "reply": None, "has_assistant": False, "fallback_count": 0,
            "native_questions": {}, "compaction": None}


def _stream_add(state, step):
    step_id = step.get("id")
    if step_id:
        existing = next((current for current in reversed(state["steps"])
                         if current.get("id") == step_id), None)
        if existing is not None:
            existing.update(step)
            return existing
    state["steps"].append(step)
    state["count"] += 1
    if step.get("call_id"):
        state["calls"][step["call_id"]] = step
    if len(state["steps"]) > MAX_STORED_STEPS:
        old = state["steps"].popleft()
        if old.get("call_id"):
            state["calls"].pop(old["call_id"], None)
    return step


def _merge_native_question(state, value):
    if not isinstance(value, dict):
        return None
    call_id = str(value.get("call_id") or value.get("item_id") or "")
    if not call_id:
        return None
    current = state["native_questions"].get(call_id)
    if current is None:
        current = {"request_id": "", "call_id": call_id, "item_id": call_id,
                   "thread_id": "", "turn_id": state.get("turn_id") or "",
                   "status": "pending", "delivery": "", "requested_at": 0.0,
                   "resolved_at": 0.0, "questions": [], "answers": {}}
        state["native_questions"][call_id] = current
    for key in ("request_id", "item_id", "thread_id", "turn_id", "delivery"):
        if value.get(key):
            current[key] = value[key]
    if value.get("requested_at"):
        current["requested_at"] = value["requested_at"]
    if value.get("questions"):
        current["questions"] = value["questions"]
    return current


def _native_question_complete(question):
    expected = {str(item.get("id")) for item in question.get("questions", [])
                if isinstance(item, dict) and item.get("id")}
    return bool(expected) and expected.issubset({str(key) for key in question.get("answers", {})})


def _apply_question_answers(state, rows, at):
    if not rows:
        return
    for row in rows:
        question_id = row.get("question_id")
        for question in state["native_questions"].values():
            if any(item.get("id") == question_id for item in question.get("questions", [])):
                question["answers"][question_id] = row.get("answer") or ""
                if _native_question_complete(question):
                    question["status"] = "resolved"
                    question["resolved_at"] = at or 0.0


def _stream_ingest(state, record, offset):
    state.setdefault("native_questions", {})
    state.setdefault("compaction", None)
    kind = record["kind"]
    if kind == "task_started":
        current_offset = state["offset"]
        state.clear()
        state.update(_new_stream_state())
        state["offset"] = current_offset
        state["turn_id"] = record.get("turn_id") or "offset-" + str(offset)
        state["started_at"] = record.get("started_at") or record.get("at") or 0
        _stream_add(state, _event_step(record, "running"))
    if not state["turn_id"]:
        return
    state["updated_at"] = max(state["updated_at"], record.get("at") or 0)
    if kind == "user":
        _apply_question_answers(state, record.get("question_answers"), record.get("at"))
        if not state["user_text"] and not record.get("question_answers"):
            state["user_text"] = _one_line(record.get("text"), 600)
    elif kind == "turn_context":
        state["model"] = record.get("model") or state["model"]
        state["effort"] = record.get("effort") or state["effort"]
    elif kind == "token_count":
        state["context"] = _context_from_info(record.get("info"))
    elif kind == "context_compacted":
        previous_count = (state.get("compaction") or {}).get("count", 0)
        compaction = {
            "count": record.get("window_number") or previous_count + 1,
            "status": "completed", "at": record.get("at") or 0.0,
            "window_number": record.get("window_number"),
            "first_window_id": record.get("first_window_id") or "",
            "previous_window_id": record.get("previous_window_id") or "",
            "window_id": record.get("window_id") or "",
            "response_id": record.get("response_id") or "",
        }
        state["compaction"] = compaction
        _stream_add(state, _event_step(record, "completed"))
    elif kind == "native_question_item":
        _merge_native_question(state, {
            "call_id": record.get("call_id"), "item_id": record.get("item_id"),
            "thread_id": record.get("thread_id"), "turn_id": record.get("turn_id"),
            "delivery": record.get("delivery"), "questions": record.get("questions"),
        })
    elif kind in _TERMINAL_EVENTS:
        # A late completion belonging to the previous turn must not end this one.
        if record.get("turn_id") and record["turn_id"] != state["turn_id"]:
            return
        state["terminal"] = True
        state["status"] = _TERMINAL_EVENTS[kind]
        _stream_add(state, _event_step(record, state["status"]))
        if kind == "turn_aborted":
            for step in state["steps"]:
                if step.get("kind") == "tool" and step.get("status") == "running":
                    step["status"] = "aborted"
    elif kind == "tool_output":
        call_id = record.get("call_id")
        target = state["calls"].get(call_id)
        if target is not None:
            _append_tool_output(target, record.get("text") or "")
        elif not call_id:
            for step in _make_steps([record], None, 0, None):
                _stream_add(state, step)
        question = state["native_questions"].get(call_id)
        response = record.get("response") or {}
        if question and response:
            explicit_status = str(response.get("status") or "").lower()
            answers = response.get("answers")
            if isinstance(answers, dict):
                question["answers"].update({str(k): _clip(v, 4000)
                                            for k, v in answers.items()})
            if _native_question_complete(question) or explicit_status in (
                    "resolved", "answered", "completed"):
                question["status"] = "resolved"
                question["resolved_at"] = record.get("at") or 0.0
            elif response.get("accepted") is False or explicit_status in (
                    "rejected", "cancelled", "canceled", "failed", "error"):
                question["status"] = "rejected"
                question["resolved_at"] = record.get("at") or 0.0
        # When an old call has left the display window its output is not a new step.
    elif kind in ("assistant", "agent_message", "reasoning", "tool_call"):
        if kind == "agent_message" and state["has_assistant"]:
            return
        if kind == "assistant" and not state["has_assistant"]:
            state["has_assistant"] = True
            state["count"] -= state["fallback_count"]
            state["steps"] = deque(s for s in state["steps"] if not s.pop("_fallback", False))
            state["fallback_count"] = 0
        # Stable offsets also distinguish records without native message IDs.
        record = dict(record)
        record.setdefault("id", kind + ":" + str(offset))
        for step in _make_steps([record], None, 0, None):
            if kind == "agent_message":
                step["_fallback"] = True
                state["fallback_count"] += 1
            step = _stream_add(state, step)
            if kind in ("assistant", "agent_message"):
                state["reply"] = {"kind": "text", "text": step.get("text") or "",
                                  "step_id": step.get("id") or "",
                                  "phase": step.get("phase") or ""}
            elif kind == "reasoning" and not state.get("has_assistant"):
                state["reply"] = {"kind": "thinking", "text": step.get("text") or ""}
        if kind == "tool_call" and record.get("native_question"):
            _merge_native_question(state, record["native_question"])


def read_turn(thread_id, max_steps=DEFAULT_MAX_STEPS, cache=None, codex_home=None, now=None):
    """Cold reads scan complete lines once; warm reads parse only appended bytes.

    Only sanitized, bounded step previews remain in memory. Counters cover the
    complete current turn even when older steps leave the 2000-step view window.
    """
    canonical = _canonical_uuid(thread_id)
    root = _safe_sessions_root(codex_home) if canonical else None
    if root is None:
        return None
    with _READ_LOCK:
        path, meta = _cache_get_path(root, canonical, cache)
        if path is None:
            return None
        stat_now = _file_stat(path)
        if not stat_now:
            return None
        state = meta.get("stream")
        previous = meta.get("stream_stat") or {}
        reset = not state or stat_now["size"] < previous.get("size", 0)
        reset = reset or (previous.get("inode") and previous["inode"] != stat_now["inode"])
        reset = reset or (stat_now["size"] == previous.get("size")
                          and stat_now["mtime_ns"] != previous.get("mtime_ns"))
        if reset:
            state = _new_stream_state()
        pending = False
        try:
            with path.open("rb") as stream:
                offset = state["offset"]
                # Detect replacement that grew beyond the previous size.
                anchor = meta.get("anchor") if not reset else None
                if anchor and offset:
                    stream.seek(max(0, offset - len(anchor)))
                    if stream.read(len(anchor)) != anchor:
                        state = _new_stream_state()
                        offset = 0
                stream.seek(offset)
                while stream.tell() < stat_now["size"]:
                    start = stream.tell()
                    line = stream.readline(MAX_META_BYTES + 1)
                    if len(line) > MAX_META_BYTES:
                        # Bound a corrupt or huge JSON row; never retain its raw body.
                        while line and not line.endswith(b"\n"):
                            line = stream.readline(MAX_META_BYTES + 1)
                        if not line.endswith(b"\n"):
                            pending = True
                            break
                        state["offset"] = stream.tell()
                        continue
                    if not line.endswith(b"\n"):
                        pending = bool(line.strip())
                        break
                    state["offset"] = stream.tell()
                    raw = _load_json_line(line)
                    if raw:
                        record = _record(raw, _iso_ts(raw.get("timestamp")))
                        if record is not None:
                            _stream_ingest(state, record, start)
                end = state["offset"]
                stream.seek(max(0, end - 64))
                meta["anchor"] = stream.read(min(64, end))
        except OSError:
            return None
        meta["stream"] = state
        meta["stream_stat"] = stat_now
        if not state["turn_id"]:
            return None
        try:
            n = int(max_steps or MAX_STORED_STEPS)
        except (TypeError, ValueError):
            n = DEFAULT_MAX_STEPS
        n = max(1, min(n if n > 0 else MAX_STORED_STEPS, MAX_STORED_STEPS))
        steps = [{k: v for k, v in step.items() if k != "_fallback"}
                 for step in list(state["steps"])[-n:]]
        current_now = time.time() if now is None else float(now)
        updated = state["updated_at"] or state["started_at"]
        live = not state["terminal"] and bool(updated) and 0 <= current_now - updated < LIVE_WINDOW_SECS
        if pending and not state["terminal"]:
            live = live or 0 <= current_now - stat_now["mtime_ns"] / 1e9 < LIVE_WINDOW_SECS
        thread_title = _thread_title(root, canonical)
        native_questions = []
        for question in state.get("native_questions", {}).values():
            item = dict(question)
            item["thread_id"] = item.get("thread_id") or canonical
            item["turn_id"] = item.get("turn_id") or state["turn_id"]
            native_questions.append(item)
        signature = _sig(state["turn_id"], steps, state["reply"], state["terminal"],
                         state["model"], state["effort"], state["context"], pending,
                         native_questions, state.get("compaction"), thread_title)
        return {"uuid": canonical, "runtime_kind": "codex", "generating": not state["terminal"],
                "context": state["context"], "reply": state["reply"], "live": live,
                "status": state["status"], "user_text": state["user_text"],
                "turn_id": state["turn_id"],
                "started_at": state["started_at"], "updated_at": updated,
                "total_steps": state["count"], "omitted": max(0, state["count"] - len(steps)),
                "pending_bubbles": 1 if pending else 0, "steps": steps, "sig": signature,
                "model": state["model"], "effort": state["effort"],
                "source": "codex_rollout", "terminal": state["terminal"],
                "native_questions": native_questions,
                "native_question_pending": sum(
                    1 for item in native_questions if item.get("status") == "pending"),
                "compaction": state.get("compaction"),
                "thread_title": thread_title,
                "title_source": "codex_session_index" if thread_title else ""}


def _desktop_latest_turn(state):
    if not isinstance(state, dict):
        return None
    history = state.get("turnHistory")
    if isinstance(history, dict) and str(history.get("kind") or "").lower() == "canonical":
        canonical = history.get("history")
        canonical = canonical if isinstance(canonical, dict) else history
        entities = canonical.get("entitiesByKey")
        if not isinstance(entities, dict):
            entities = state.get("entitiesByKey") if isinstance(state.get("entitiesByKey"), dict) else {}
        found = []
        for island in canonical.get("islands") if isinstance(canonical.get("islands"), list) else []:
            if not isinstance(island, dict):
                continue
            for entry in island.get("entries") if isinstance(island.get("entries"), list) else []:
                if not isinstance(entry, dict):
                    continue
                value = entry.get("value")
                turn = value if isinstance(value, dict) else entities.get(str(value))
                if isinstance(turn, dict):
                    found.append(turn)
        if found:
            return found[-1]
    turns = state.get("turns")
    if isinstance(turns, list):
        return next((item for item in reversed(turns) if isinstance(item, dict)), None)
    return None


def _desktop_type(item):
    return str(item.get("type") or "").replace("_", "").replace("-", "").casefold()


def _desktop_runtime_active(value):
    if isinstance(value, dict):
        flags = value.get("activeFlags")
        if isinstance(flags, dict) and any(bool(item) for item in flags.values()):
            return True
        if isinstance(flags, (list, tuple, set)) and bool(flags):
            return True
        value = value.get("type") or value.get("status")
    return str(value or "").casefold() in (
        "active", "running", "inprogress", "in_progress", "pending", "streaming")


def _desktop_request_answers(value):
    if not isinstance(value, dict):
        return {}
    raw = value.get("answers")
    if not isinstance(raw, dict):
        return {}
    answers = {}
    for question_id, answer in raw.items():
        if isinstance(answer, dict):
            answer = answer.get("answers")
        if isinstance(answer, list):
            answers[str(question_id)] = [_clip(item, 4000) for item in answer if isinstance(item, str)]
        elif isinstance(answer, str):
            answers[str(question_id)] = _clip(answer, 4000)
    return answers


def _desktop_async_question_item(item):
    """Return the call ID and questions for Desktop's async-question item variants."""
    kind = _desktop_type(item)
    if kind == "agentmessage":
        if item.get("delivery") != "async":
            return "", []
        call_id = _clip(item.get("id"), 200)
        return call_id, _normalize_questions(item.get("questions"), call_id)
    if kind not in ("functioncall", "customtoolcall"):
        return "", []
    name = item.get("name") or item.get("toolName") or item.get("tool")
    if not _is_async_native_question_tool(name):
        return "", []
    call_id = _clip(item.get("callId") or item.get("call_id") or item.get("id"), 200)
    argument = item.get("input", item.get("arguments"))
    return call_id, _normalize_questions(_json_object(argument).get("questions"), call_id)


def project_desktop_turn(state, max_steps=DEFAULT_MAX_STEPS, now=None):
    """把 Codex Desktop IPC state 安全投影成 read_turn 兼容结构。

    只读取 title/model/effort/runtime status、canonical 最新 turn 与白名单 items；
    current params、permissions、未知实体和 reasoning.content 一律不向外传播。
    """
    turn = _desktop_latest_turn(state)
    if not isinstance(turn, dict):
        return None
    thread_id = _canonical_uuid(state.get("id") or state.get("threadId") or state.get("thread_id"))
    turn_id = _clip(turn.get("turnId") or turn.get("id"), 160)
    runtime_status = state.get("threadRuntimeStatus")
    raw_status = str(turn.get("status") or
                     ("running" if _desktop_runtime_active(runtime_status) else
                      (runtime_status.get("type") if isinstance(runtime_status, dict)
                       else runtime_status)) or "").casefold()
    terminal = raw_status not in ("", "active", "running", "inprogress", "in_progress",
                                  "pending", "streaming")
    status = "running" if not terminal else _tool_status(raw_status, "completed")
    started_at = _epoch(turn.get("turnStartedAtMs") or turn.get("startedAtMs"))
    duration_ms = _safe_number(turn.get("durationMs")) or 0
    updated_at = started_at + duration_ms / 1000.0 if started_at else 0.0
    items = turn.get("items") if isinstance(turn.get("items"), list) else []
    steps = []
    native_questions = []
    question_by_id = {}
    compaction = None
    user_text = ""
    ordinal = 0
    for request in state.get("requests") if isinstance(state.get("requests"), list) else []:
        if not isinstance(request, dict) or request.get("method") != "item/tool/requestUserInput":
            continue
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        if str(params.get("turnId") or "") != turn_id:
            continue
        request_id = request.get("id")
        if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
            continue
        questions = _normalize_questions(params.get("questions"), request_id)
        if not questions:
            continue
        answers = _desktop_request_answers(request.get("response") or request.get("result"))
        question = {
            "request_id": request_id, "call_id": _clip(params.get("itemId"), 200),
            "item_id": _clip(params.get("itemId"), 200), "thread_id": thread_id,
            "turn_id": turn_id, "status": "pending", "delivery": "blocking",
            "requested_at": started_at, "resolved_at": updated_at if answers else 0.0,
            "questions": questions, "answers": answers,
        }
        if _native_question_complete(question):
            question["status"] = "resolved"
        native_questions.append(question)
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = _desktop_type(item)
        item_id = _clip(item.get("id"), 200) or _event_id(kind, turn_id, started_at, ordinal)
        # 桌面历史可能不含逐项时间；不能把整轮开始/结束时间标到每一步上。
        at = _epoch(item.get("completedAtMs") or item.get("startedAtMs"))
        if kind == "agentmessage":
            call_id, questions = _desktop_async_question_item(item)
            text = _clip(item.get("text") or _visible_text(item.get("content")))
            if not questions and item.get("delivery") == "async" and text:
                questions = [{"id": call_id or item_id, "title": text, "header": "",
                              "question": text, "options": []}]
            if questions:
                question = {
                    "request_id": _clip(item.get("requestId"), 200) or call_id or item_id,
                    "call_id": call_id or item_id, "item_id": call_id or item_id, "thread_id": thread_id,
                    "turn_id": turn_id, "status": "pending",
                    "delivery": _clip(item.get("delivery"), 80),
                    "requested_at": at, "resolved_at": 0.0,
                    "questions": questions, "answers": {},
                }
                native_questions.append(question)
                for question_item in questions:
                    question_by_id[question_item["id"]] = question
            else:
                if text:
                    steps.append({"kind": "text", "id": item_id, "at": at,
                                  "text": text, "phase": _clip(item.get("phase"), 80),
                                  "role": "assistant"})
        elif kind in ("functioncall", "customtoolcall"):
            call_id, questions = _desktop_async_question_item(item)
            if questions:
                question = {
                    "request_id": call_id or item_id, "call_id": call_id or item_id,
                    "item_id": call_id or item_id, "thread_id": thread_id, "turn_id": turn_id,
                    "status": "pending", "delivery": "async", "requested_at": at,
                    "resolved_at": 0.0, "questions": questions, "answers": {},
                }
                native_questions.append(question)
                for question_item in questions:
                    question_by_id[question_item["id"]] = question
        elif kind == "reasoning":
            summary = _summary_text(item.get("summary") or item.get("summary_text"))
            if summary:
                steps.append({"kind": "thinking", "id": item_id, "at": at,
                              "text": summary})
        elif kind == "mcptoolcall":
            name = _clip("{}.{}".format(item.get("server") or "mcp",
                                         item.get("tool") or "tool"), 240)
            argument = item.get("arguments")
            summary, command = _tool_summary(name, argument)
            result = _clip(_visible_text(item.get("result") or item.get("error")), MAX_RESULT_CHARS)
            step = {"kind": "tool", "id": item_id, "at": at, "tool": "tool",
                    "label": "工具", "name": name, "call_id": item_id,
                    "status": _tool_status(item.get("status")), "summary": summary,
                    "result": result}
            if command:
                step["command"] = command
            steps.append(step)
        elif kind == "commandexecution":
            command = _clip(item.get("command"), MAX_RESULT_CHARS)
            result = _clip(item.get("aggregatedOutput") or item.get("formattedOutput")
                           or item.get("stderr"), MAX_RESULT_CHARS)
            steps.append({"kind": "tool", "id": item_id, "at": at, "tool": "shell",
                          "label": "运行命令", "name": "exec", "call_id": item_id,
                          "status": _tool_status(item.get("status")),
                          "summary": _one_line(command, 1000), "command": command,
                          "result": result, "exit_code": _safe_number(item.get("exitCode"))})
        elif kind == "contextcompaction":
            count = (compaction or {}).get("count", 0) + 1
            compaction = {"count": count,
                          "status": "completed" if item.get("completed", True) else "running",
                          "at": at, "item_id": item_id,
                          "source": _clip(item.get("source"), 120)}
            steps.append({"kind": "event", "id": item_id, "at": at,
                          "event": "context_compacted", "status": compaction["status"],
                          "text": "上下文压缩完成" if compaction["status"] == "completed"
                          else "正在压缩上下文"})
        elif kind == "subagentactivity":
            steps.append({"kind": "event", "id": item_id, "at": at,
                          "event": "subagent_activity",
                          "status": _tool_status(item.get("status"), "done"),
                          "text": "子代理活动：" + _clip(item.get("kind"), 120),
                          "agent_thread_id": _clip(item.get("agentThreadId"), 160)})
        elif kind in ("usermessage", "steeringusermessage"):
            text = _visible_text(item.get("content") or item.get("input") or item.get("text"))
            question_answers = _question_answers_from_text(text)
            answers = (question_answers
                       if kind == "usermessage" or item.get("status") == "accepted" else [])
            if question_answers:
                for answer in answers:
                    question = question_by_id.get(answer["question_id"])
                    if question:
                        question["answers"][answer["question_id"]] = answer.get("answer") or ""
                        if _native_question_complete(question):
                            question["status"] = "resolved"
                            question["resolved_at"] = at
            elif text and (kind == "usermessage" or item.get("status") == "accepted"):
                steps.append({"kind": "text", "id": item_id, "at": at,
                              "text": _clip(text, MAX_TEXT_CHARS), "role": "user"})
                if not user_text:
                    user_text = _one_line(text, 600)
        ordinal += 1
    reply = _reply(steps)
    try:
        n = int(max_steps or MAX_STORED_STEPS)
    except (TypeError, ValueError):
        n = DEFAULT_MAX_STEPS
    n = max(1, min(n if n > 0 else MAX_STORED_STEPS, MAX_STORED_STEPS))
    total_steps = len(steps)
    steps = steps[-n:]
    current_now = time.time() if now is None else float(now)
    live = not terminal and _desktop_runtime_active(runtime_status)
    if not live and not terminal and updated_at:
        live = 0 <= current_now - updated_at < LIVE_WINDOW_SECS
    title = _clip(state.get("title"), 500)
    params = turn.get("params") if isinstance(turn.get("params"), dict) else {}
    collaboration = (params.get("collaborationMode")
                     if isinstance(params.get("collaborationMode"), dict) else {})
    settings = (collaboration.get("settings")
                if isinstance(collaboration.get("settings"), dict) else {})
    model = _clip(state.get("latestModel") or settings.get("model") or params.get("model"), 200)
    effort = _clip(state.get("latestReasoningEffort") or settings.get("reasoning_effort")
                   or params.get("effort"), 80)
    context = _desktop_context(state.get("latestTokenUsageInfo"))
    signature = _sig(turn_id, steps, reply, terminal, model, effort, context, False,
                     native_questions, compaction, title)
    return {
        "uuid": thread_id, "runtime_kind": "codex", "source": "codex_desktop",
        "generating": not terminal, "live": live, "terminal": terminal, "status": status,
        "context": context, "reply": reply, "user_text": user_text, "turn_id": turn_id,
        "started_at": started_at, "updated_at": updated_at,
        "total_steps": total_steps, "omitted": max(0, total_steps - len(steps)),
        "pending_bubbles": 0, "steps": steps, "sig": signature,
        "model": model, "effort": effort, "native_questions": native_questions,
        "native_question_pending": sum(1 for q in native_questions if q["status"] == "pending"),
        "compaction": compaction, "thread_title": title,
        "title_source": "codex_desktop_state" if title else "",
    }
