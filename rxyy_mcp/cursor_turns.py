# -*- coding: utf-8 -*-
"""实时还原某个 Cursor 对话「这一轮」agent 在干什么——思考 / 工具调用 / 正文（读库版）。

rxyy 09-03：「看 bajie 这个 mcp 当前是怎么做的，把rxyy MCP 也改成这样」——bajie-chat 的
ReasoningTimeline 靠往 workbench 注钩子（bajie-hook-client.js 轮询 getTurnProcess）拿到
agent 的每一步。rxyy MCP 不改 Cursor 安装文件（parkgate 已明写「未修改 Cursor 安装文件」），
走读库：Cursor 把每一步都写进 state.vscdb 的 cursorDiskKV——

  composerData:<uuid>            对话头：fullConversationHeadersOnly[{bubbleId,type}]
                                 （type 1 用户 / 2 AI）、generatingBubbleIds、status
  bubbleId:<uuid>:<bubbleId>     单条气泡。AI 气泡三种形态：
    · 思考   thinking.text + thinkingDurationMs（capabilityType 30）
    · 工具   toolFormerData{name,status(loading|completed|error),rawArgs,params,result,
             additionalData,tool,toolCallId}（capabilityType 15）
    · 正文   text
    另有 errorDetails（生成报错，判死那套读的就是它）

「这一轮」= 最后一条用户气泡之后的全部 AI 气泡。BajieAsk / rxyy MCP 这类 MCP 对话把用户的话
当工具结果送进去，一轮能有几百步（09-03 实测 FRAP 会话 223 步），所以只返回最后 max_steps
步 + 总数；已定型的气泡（工具已完成 / 报错、不是最后一条的思考与正文）按 bubbleId 缓存，
每拍只重读新气泡和最后几条。

读法沿用 session_locator：mode=ro 优先（看得到 WAL 里刚写的），打不开退 immutable=1；
单键查询 1~3ms，一拍 40 条气泡也就几十毫秒。新气泡从写 WAL 到可读约 8s（09-03 实测），
这是读库路的天花板——要 <2s 得像 bajie 那样注钩子，不在这一刀里。
"""
import difflib
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

# (匹配函数, 类别, 中文标签)。类别给前端配图标/配色，标签直接显示。
_TOOL_RULES = (
    (lambda n: n in ("run_terminal_command_v2", "run_terminal_cmd", "shell", "Shell",
                     "bash", "execute_command"), "shell", "运行命令"),
    (lambda n: n in ("await", "AwaitShell", "await_shell"), "shell", "等命令结束"),
    (lambda n: n in ("read_file_v2", "read_file", "Read", "view_file", "read_lints",
                     "ReadLints", "list_dir", "ls"), "read", "读文件"),
    (lambda n: n in ("edit_file_v2", "edit_file", "search_replace", "StrReplace", "write",
                     "Write", "MultiEdit", "delete_file", "Delete", "apply_patch",
                     "edit_notebook", "EditNotebook"), "edit", "改文件"),
    (lambda n: n in ("ripgrep_raw_search", "grep_search", "grep", "Grep", "glob_file_search",
                     "Glob", "codebase_search", "file_search", "semantic_search"),
     "search", "搜索"),
    (lambda n: n in ("web_search", "WebSearch", "fetch", "WebFetch", "web_fetch"), "web", "联网"),
    (lambda n: n in ("todo_write", "TodoWrite", "update_todos"), "todo", "待办"),
    (lambda n: n in ("task", "Task", "launch_agent", "spawn_agent"), "agent", "子代理"),
    (lambda n: n in ("ask_question", "AskQuestion"), "ask", "提问"),
    (lambda n: n in ("switch_mode", "SwitchMode"), "ask", "切换模式"),
    (lambda n: n.startswith("mcp-") or n.startswith("mcp_") or n.startswith("user-")
     or n == "CallMcpTool", "mcp", "MCP"),
)

# 一拍最多回多少步（前面的只报总数）；缓存最多记多少条气泡
DEFAULT_MAX_STEPS = 40
CACHE_LIMIT = 4000
# 最后一条气泡多久没更新就算「这轮已停」（bajie 的 rtStale 也是 30s）
LIVE_WINDOW_SECS = 30.0
# 改文件 diff：前后全文都存在 composer.content.<sha> 键里（09-03 实测 32KB 的测试文件
# 一次编辑就是两个键），超过这个体积不算 diff、只报大小，别让一拍读几 MB
DIFF_MAX_BYTES = 400 * 1024
DIFF_PREVIEW_LINES = 60

# 上下文用量的三档门槛（占 contextTokenLimit 的比例）。
# 为什么不照 bajie 的「≥120 条消息就提示接力」：那是拿条数猜，同样 120 条可能是 20K
# 也可能是 600K。Cursor 自己就在 composerData 上记着精确值（09-03 实测四个对话都有：
# contextUsagePercent / contextTokensUsed / contextTokenLimit，如 484306/1000000=48.43%），
# 而 read_turn 本来就要读 composerData 那一行——精确值零额外 I/O 就在手边。
# 档位口径：Cursor 上下文快满时会自动摘要压缩（composerData 里那个
# speculativeSummarizationEncryptionKey 就是它），压过之后前文细节就只剩摘要了，
# 接手方拿到的东西会变薄。所以 high 档就该动手交接，别等 critical。
CTX_WARN_PCT = 65.0
CTX_HIGH_PCT = 80.0
CTX_CRITICAL_PCT = 92.0


def classify_tool(name):
    """工具名 → (类别, 中文标签)。不认识的按原名显示、类别 tool。"""
    n = str(name or "")
    for match, kind, label in _TOOL_RULES:
        try:
            if match(n):
                return kind, label
        except Exception:
            continue
    return "tool", (n or "工具")


def _global_db(appdata=None):
    roaming = Path(appdata or os.environ.get("APPDATA")
                   or Path.home() / "AppData" / "Roaming")
    db = roaming / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    return db if db.is_file() else None


def _connect(db):
    path = str(db).replace("\\", "/")
    for uri in ("?mode=ro", "?immutable=1"):
        try:
            return sqlite3.connect("file:{}{}".format(path, uri), uri=True, timeout=2)
        except Exception:
            continue
    return None


def _loads(s):
    if s is None:
        return None
    if isinstance(s, (dict, list)):
        return s
    try:
        return json.loads(s)
    except Exception:
        return None


def _iso_ts(text):
    """Cursor 的 createdAt 是 ISO-8601（带 Z）→ epoch 秒；解析不了回 0。"""
    t = str(text or "").strip()
    if not t:
        return 0.0
    try:
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        return datetime.fromisoformat(t).astimezone(timezone.utc).timestamp()
    except Exception:
        return 0.0


def _clip(text, n):
    text = str(text or "")
    return text if len(text) <= n else text[:n] + "…"


def _one_line(text, n=160):
    return _clip(" ".join(str(text or "").split()), n)


def _base(path):
    p = str(path or "").replace("\\", "/").rstrip("/")
    return p.rsplit("/", 1)[-1] if "/" in p else p


# Cursor 原生提问（AskQuestion）的字段落点，2026-09-03 从本机库实测的唯一一条（对话
# 47435c3d，08-31 08:31「OpenMontage 派单路线」两问五选）逐字抄下来：
#   params          {"title", "questions":[{"id","prompt","options":[{"id","label"}],
#                                           "multiple"?,"allowFreeText"?}]}
#   result          {"answers":[{"questionId","selectedOptionIds":[…],"freeformText"}]}
#   additionalData  {"status":"submitted","currentSelections":{qid:[oid]},
#                    "freeformTexts":{qid:str},"completionDelivered":bool}
#   rawArgs         **这个工具没有**（只有 params）
# 最后那条正是 09-01 §六「AskQuestion 显示」这个待办的根：summarize_args 只读 rawArgs，
# 于是提问步骤的摘要恒为空串，控制台时间线上只剩一个光秃秃的「❓ 提问」——问的是什么、
# 有哪几个选项、答了没有，全看不见。而这恰恰是最该让人看见的一步：agent 用原生提问时是
# **卡住不动**的，人不去 Cursor 里点，它就一直等着（用户在手机/控制台上根本不知道为什么卡住）。
_ASK_STATE_LABEL = {"answered": "你已答", "waiting": "等你答", "cancelled": "已取消/未答"}


def parse_ask(tfd):
    """AskQuestion 工具气泡 → {"title","state","questions":[…]}；没有题目就回 None。

    state：answered（已提交答案）/ cancelled（取消或报错收场）/ waiting（正卡着等人点）。
    每题带 options（picked 标出当前选中）、answer（选中项的文字）、freeform（自由输入）。
    """
    tfd = tfd if isinstance(tfd, dict) else {}
    params = _loads(tfd.get("params")) or _loads(tfd.get("rawArgs")) or {}
    if not isinstance(params, dict):
        return None
    qs_raw = params.get("questions")
    if not isinstance(qs_raw, list) or not qs_raw:
        return None
    res = _loads(tfd.get("result"))
    add = tfd.get("additionalData") if isinstance(tfd.get("additionalData"), dict) else {}
    answers = {}
    if isinstance(res, dict):
        for a in res.get("answers") or []:
            if isinstance(a, dict) and a.get("questionId") is not None:
                answers[str(a["questionId"])] = a
    sel_now = add.get("currentSelections") if isinstance(add.get("currentSelections"), dict) else {}
    free_now = add.get("freeformTexts") if isinstance(add.get("freeformTexts"), dict) else {}
    st = _tool_status(tfd)
    if answers or str(add.get("status") or "").lower() == "submitted":
        state = "answered"
    elif st in ("error", "cancelled"):
        state = "cancelled"
    else:
        state = "waiting"

    out = []
    for q in qs_raw:
        if not isinstance(q, dict):
            continue
        qid = str(q.get("id") or "")
        ans = answers.get(qid) or {}
        picked = [str(x) for x in (ans.get("selectedOptionIds")
                                   or sel_now.get(qid) or []) if x is not None]
        opts, labels = [], []
        for o in q.get("options") or []:
            if not isinstance(o, dict):
                continue
            oid = str(o.get("id") or "")
            label = _one_line(o.get("label") or oid, 200)
            hit = oid in picked
            opts.append({"id": oid, "label": label, "picked": hit})
            if hit:
                labels.append(label)
        free = str(ans.get("freeformText") or free_now.get(qid) or "").strip()
        out.append({"id": qid, "prompt": _one_line(q.get("prompt") or "", 300),
                    "options": opts, "answer": labels, "freeform": _one_line(free, 300),
                    "multiple": bool(q.get("multiple")),
                    "free_ok": q.get("allowFreeText") is not False})
    if not out:
        return None
    return {"title": _one_line(params.get("title") or "", 200), "state": state, "questions": out}


def render_ask(ask):
    """提问结构 → 时间线详情区那段人话（.lt-detail 是 pre-wrap，换行原样显示）。"""
    if not ask:
        return ""
    nq = len(ask["questions"])
    nopt = sum(len(q["options"]) for q in ask["questions"])
    if ask["state"] == "waiting":
        head = "⏳ 等你在 Cursor 对话里点选（{} 问 · {} 个选项）——没人答它就一直卡着".format(nq, nopt)
    elif ask["state"] == "answered":
        head = "✓ 已答（{} 问）".format(nq)
    else:
        head = "⊘ 这次提问没答成（取消或报错）"
    lines = [head]
    marks = "①②③④⑤⑥⑦⑧⑨⑩"
    for i, q in enumerate(ask["questions"]):
        lines.append("{} {}".format(marks[i] if i < len(marks) else "·", q["prompt"]))
        for o in q["options"]:
            lines.append("   {} {}".format("●" if o["picked"] else "○", o["label"]))
        if q["answer"]:
            lines.append("   → 选了：{}".format("、".join(q["answer"])))
        if q["freeform"]:
            lines.append("   → 补充：{}".format(q["freeform"]))
    return _clip("\n".join(lines), 2000)


def _arg_path(raw, params):
    """读/改文件工具的目标路径（rawArgs 与 params 两边键名都认）。"""
    raw = raw if isinstance(raw, dict) else {}
    params = params if isinstance(params, dict) else {}
    return str(raw.get("path") or raw.get("file_path") or raw.get("target_file")
               or params.get("relativeWorkspacePath") or params.get("targetFile")
               or params.get("path") or "")


def _arg_why(raw, params):
    """模型给这次工具调用写的一句说明（Cursor 各工具 schema 里的 explanation / description /
    instructions；MCP 工具一般没有）。取到就是人话，不做加工，只截一行。"""
    raw = raw if isinstance(raw, dict) else {}
    params = params if isinstance(params, dict) else {}
    # 09-07 真机（Cursor 1.128）实测键名：终端在 params.commandDescription；MCP 调用的说明在
    # rawArgs.args.mcpDetails.description（CallMcpTool 让模型写的一句人话）；读/改/搜索这版没有
    mcp_args = raw.get("args") if isinstance(raw.get("args"), dict) else {}
    mcp_details = mcp_args.get("mcpDetails") if isinstance(mcp_args.get("mcpDetails"), dict) else {}
    for src in (raw, params, mcp_details, mcp_args):
        for key in ("explanation", "commandDescription", "description", "instructions", "reason", "purpose"):
            v = src.get(key)
            if isinstance(v, str) and v.strip():
                return _one_line(v.strip(), 240)
    return ""


def summarize_args(name, kind, raw, params):
    """一行参数摘要：命令 / 文件名 / 搜索词 / MCP 工具名，UI 上和工具名并排。"""
    raw = raw if isinstance(raw, dict) else {}
    params = params if isinstance(params, dict) else {}
    if kind == "shell":
        cmd = raw.get("command") or params.get("command") or ""
        return _one_line(cmd, 200)
    if kind in ("read", "edit"):
        path = _arg_path(raw, params)
        rng = ""
        if raw.get("offset") or raw.get("limit"):
            rng = " L{}{}".format(raw.get("offset") or 1,
                                 ("~+" + str(raw.get("limit"))) if raw.get("limit") else "")
        return _one_line((_base(path) + rng) if path else json.dumps(raw or params, ensure_ascii=False), 160)
    if kind == "search":
        q = (raw.get("pattern") or raw.get("query") or raw.get("glob_pattern")
             or params.get("pattern") or params.get("query") or "")
        where = raw.get("path") or params.get("path") or ""
        return _one_line((q or "") + (("  ·  " + _base(where)) if where and q else ""), 160)
    if kind == "web":
        return _one_line(raw.get("search_term") or raw.get("url") or raw.get("query")
                         or params.get("search_term") or params.get("url") or "", 160)
    if kind == "mcp":
        inner = raw.get("name") or ""
        args = raw.get("args")
        if not inner:
            tools = params.get("tools") or []
            if tools and isinstance(tools[0], dict):
                inner = "{}/{}".format(tools[0].get("serverName") or "", tools[0].get("name") or "")
                args = _loads(tools[0].get("parameters"))
        tail = ""
        if isinstance(args, dict) and args:
            tail = "  " + _one_line(json.dumps(args, ensure_ascii=False), 120)
        return _one_line(str(inner).replace("user-", "", 1) + tail, 200)
    if kind == "todo":
        return ""
    if kind == "agent":
        return _one_line(raw.get("description") or raw.get("prompt") or "", 160)
    if kind == "ask":
        # 提问的题目在 params，rawArgs 这个工具压根没有（见 _ASK_STATE_LABEL 上面那段实测）
        src = params or raw
        qs = src.get("questions") if isinstance(src.get("questions"), list) else []
        title = src.get("title") or src.get("prompt") or ""
        if not title and qs and isinstance(qs[0], dict):
            title = qs[0].get("prompt") or ""
        tail = "  ·  {} 问".format(len(qs)) if len(qs) > 1 else ""
        return _one_line(str(title) + tail, 200)
    blob = raw or params
    return _one_line(json.dumps(blob, ensure_ascii=False) if blob else "", 160)


def summarize_result(name, kind, tfd):
    """结果预览（≤600 字）：命令输出 / 行数 / 匹配数 / MCP 回文 / 报错。"""
    res = _loads(tfd.get("result"))
    add = tfd.get("additionalData") if isinstance(tfd.get("additionalData"), dict) else {}
    err = tfd.get("error")
    if tfd.get("status") == "error" and err:
        return _clip(err if isinstance(err, str) else json.dumps(err, ensure_ascii=False), 600)
    if kind == "shell":
        if isinstance(res, dict):
            out = res.get("output")
            if out is None:
                out = res.get("stdout") or ""
            code = res.get("exitCode", res.get("exit_code"))
            head = "" if code in (None, "") else "退出码 {} · ".format(code)
            return head + _clip(str(out).strip(), 600)
        return _clip(json.dumps(res, ensure_ascii=False) if res is not None else "", 600)
    if kind == "read":
        if isinstance(res, dict) and res.get("totalLinesInFile") is not None:
            return "全文 {} 行".format(res.get("totalLinesInFile"))
        return _clip(json.dumps(res, ensure_ascii=False) if res is not None else "", 300)
    if kind == "search":
        if add.get("totalMatches") is not None:
            return "{} 处匹配 · {} 个文件".format(add.get("totalMatches"), add.get("totalFiles", "?"))
        if isinstance(res, dict) and res.get("totalMatches") is not None:
            return "{} 处匹配".format(res.get("totalMatches"))
        return _clip(json.dumps(res, ensure_ascii=False) if res is not None else "", 300)
    if kind == "edit":
        if isinstance(res, dict) and res.get("afterContentId"):
            return "已写入"   # read_turn 拿到前后内容后会换成 +N / −M（见 _attach_edit_diff）
        return _clip(json.dumps(res, ensure_ascii=False) if res is not None else "", 300)
    if kind == "mcp":
        inner = res.get("result") if isinstance(res, dict) else None
        inner = _loads(inner) if isinstance(inner, str) else inner
        if isinstance(inner, dict):
            parts = [c.get("text") for c in (inner.get("content") or [])
                     if isinstance(c, dict) and c.get("text")]
            if parts:
                return _clip("\n".join(parts), 600)
            if inner.get("isError"):
                return _clip(json.dumps(inner, ensure_ascii=False), 600)
        return _clip(json.dumps(res, ensure_ascii=False) if res is not None else "", 600)
    if kind == "ask":
        # 提问步骤的「结果」= 问了什么 + 答了没有。这是整条时间线上最该被人看见的一步：
        # agent 用 Cursor 原生提问时是阻塞的，人不去点它就永远等着
        rendered = render_ask(parse_ask(tfd))
        if rendered:
            return rendered
        return _clip(json.dumps(res, ensure_ascii=False) if res is not None else "", 400)
    if kind == "todo":
        if isinstance(res, dict) and isinstance(res.get("finalTodos"), list):
            todos = res["finalTodos"]
            done = sum(1 for t in todos if isinstance(t, dict) and t.get("status") == "completed")
            doing = [t.get("content") for t in todos
                     if isinstance(t, dict) and t.get("status") == "in_progress"]
            return "{}/{} 完成".format(done, len(todos)) + \
                ("；进行中：" + _one_line("；".join(str(x) for x in doing if x), 200) if doing else "")
        return ""
    return _clip(json.dumps(res, ensure_ascii=False) if res is not None else "", 400)


def _tool_status(tfd):
    """toolFormerData.status 为主：loading=在跑。additionalData.status 只在跑完后才可信——
    09-03 实测命令还在跑时 additionalData 就先写着 "cancelled"，以它为准会把在跑的标成已取消。"""
    st = str(tfd.get("status") or "").lower()
    add = tfd.get("additionalData") if isinstance(tfd.get("additionalData"), dict) else {}
    if st == "error":
        return "error"
    if st in ("loading", "running", "pending", "in_progress", ""):
        return "running"
    if add.get("status") == "error":
        return "error"
    if add.get("status") == "cancelled":
        return "cancelled"
    if st in ("completed", "success", "done"):
        return "done"
    return st


def parse_bubble(bubble):
    """一条 AI 气泡 → step dict；用户气泡 / 空气泡 → None。纯函数，便于用真实形态的夹具测。"""
    if not isinstance(bubble, dict) or bubble.get("type") == 1:
        return None
    at = _iso_ts(bubble.get("createdAt"))
    bid = str(bubble.get("bubbleId") or "")
    tfd = bubble.get("toolFormerData")
    if isinstance(tfd, dict) and (tfd.get("name") or tfd.get("tool") is not None):
        name = str(tfd.get("name") or "tool#{}".format(tfd.get("tool")))
        kind, label = classify_tool(name)
        raw = _loads(tfd.get("rawArgs"))
        params = _loads(tfd.get("params"))
        add = tfd.get("additionalData") if isinstance(tfd.get("additionalData"), dict) else {}
        started = add.get("startedAtMs")
        step = {
            "kind": "tool", "id": bid, "at": at,
            "tool": kind, "label": label, "name": name,
            "status": _tool_status(tfd),
            "summary": summarize_args(name, kind, raw, params),
            "result": summarize_result(name, kind, tfd),
        }
        if started:
            try:
                step["started"] = float(started) / 1000.0
            except (TypeError, ValueError):
                pass
        why = _arg_why(raw, params)
        if why:
            # Cursor 让模型给每个工具调用带一句「为什么/干什么」（explanation / description /
            # instructions）。Bajie 工具卡把它单列一行，人不看命令就知道这一步在干什么；
            # 09-07 rxyy 拍板时间线改卡片流，这一行是核心
            step["why"] = why
        if kind in ("read", "edit"):
            # 摘要里只有文件名；完整路径另给，前端「复制路径」用（09-07 rxyy 定的读库做透清单）
            full = _arg_path(raw, params)
            if full:
                step["path"] = _clip(full, 400)
        if kind == "ask":
            ask = parse_ask(tfd)
            if ask:
                # 结构也一并给前端（将来要渲成卡片就不用再解一遍）；waiting = agent 真卡着，
                # tab 上那一句要说清「等你答」，否则用户只看到「第 N 步 · 提问」还以为在跑
                step["ask"] = ask
                step["blocking"] = ask["state"] == "waiting"
        if kind == "edit":
            res = _loads(tfd.get("result"))
            if isinstance(res, dict) and res.get("afterContentId"):
                # 前后全文的键名，read_turn 有库连接时据此算 diff（纯函数这里拿不到库）
                step["content_ids"] = [str(res.get("beforeContentId") or ""),
                                       str(res.get("afterContentId") or "")]
        return step
    th = bubble.get("thinking")
    typed_think = bubble.get("capabilityType") == 30 or bubble.get("thinkingStyle") is not None
    if isinstance(th, str) and th.lstrip().startswith("{"):
        # 09-07 真机（8adae4ba）：部分模型的 thinking 落库是整段 JSON 字符串
        # {"text": "...", "isLastThinkingChunk": true}，不解开时间线上就糊一坨花括号
        inner = _loads(th)
        if isinstance(inner, dict) and "text" in inner:
            th = inner
    if isinstance(th, dict):
        raw = th.get("text") or ""
        if isinstance(raw, str) and raw.lstrip().startswith("{\"text\""):
            inner = _loads(raw)
            if isinstance(inner, dict) and isinstance(inner.get("text"), str):
                raw = inner["text"]
        raw = raw.strip() if isinstance(raw, str) else ""
        # 流式刚起盘时常是 capabilityType 30 + text 空串；丢掉的话时间线要等第一句
        # 落库才出现「思考中」，对不齐 Bajie 的 thinking.delta / Cursor 侧栏「思考」
        if raw or typed_think:
            return {"kind": "thinking", "id": bid, "at": at,
                    "text": _clip(raw, 3000) if raw else "",
                    "ms": int(bubble.get("thinkingDurationMs") or 0)}
    if isinstance(th, str) and th.strip():
        return {"kind": "thinking", "id": bid, "at": at, "text": _clip(th.strip(), 3000),
                "ms": int(bubble.get("thinkingDurationMs") or 0)}
    ed = bubble.get("errorDetails")
    if isinstance(ed, dict) and ed:
        err = ed.get("error")
        if isinstance(err, str) and err.lstrip().startswith("{"):
            # 09-03 线上实测：errorDetails.error 常是一段 JSON 字符串（{"error":"ERROR_CUSTOM_MESSAGE",
            # "details":{...}}）而不是 dict，不解开就只能整段原样糊上去
            err = _loads(err) or err
        det = err.get("details") if isinstance(err, dict) else {}
        det = det if isinstance(det, dict) else {}
        title = det.get("title") or (err if isinstance(err, str) else "") or "报错"
        return {"kind": "error", "id": bid, "at": at,
                "text": _one_line("{}：{}".format(title, det.get("detail") or ""), 400).rstrip("："),
                "code": err.get("error") if isinstance(err, dict) else err}
    text = bubble.get("text")
    if isinstance(text, str) and text.strip():
        return {"kind": "text", "id": bid, "at": at, "text": _clip(text.strip(), 4000)}
    return None


def _final(step, is_last):
    """这条 step 还会不会变：工具跑完 / 报错 / 不是最后一条的思考与正文 → 定型可缓存。"""
    if step is None:
        return False
    if step["kind"] == "tool":
        return step["status"] != "running"
    return not is_last


def compute_diff(before, after, preview_lines=DIFF_PREVIEW_LINES):
    """两段全文 → {"plus","minus","preview"}：行数统计 + 前 preview_lines 行 unified diff 正文
    （不带 ---/+++ 头，@@ 行保留好定位）。纯函数，便于测。"""
    a = (before or "").splitlines()
    b = (after or "").splitlines()
    plus = minus = 0
    out = []
    for line in difflib.unified_diff(a, b, n=1, lineterm=""):
        if line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("+"):
            plus += 1
        elif line.startswith("-"):
            minus += 1
        if len(out) < preview_lines:
            out.append(line)
    more = (plus + minus) - sum(1 for x in out if x[:1] in "+-")
    if more > 0:
        out.append("… 还有 {} 行改动".format(more))
    return {"plus": plus, "minus": minus, "preview": _clip("\n".join(out), 6000)}


def _attach_edit_diff(con, step, content_cache=None):
    """改文件步骤：读 composer.content.<sha> 前后全文，算 +N/−M 与预览挂到 step 上。
    读不到 / 太大就把原因写进 result，不抛。"""
    ids = step.get("content_ids") or []
    if len(ids) != 2:
        return
    texts = []
    for key in ids:
        if not key:
            texts.append("")
            continue
        try:
            row = con.execute("SELECT length(value), value FROM cursorDiskKV WHERE key=?",
                              (key,)).fetchone()
        except Exception:
            row = None
        if not row:
            texts.append(None)
            continue
        if (row[0] or 0) > DIFF_MAX_BYTES:
            step["result"] = "已写入（文件 {:.0f} KB，太大没算 diff）".format((row[0] or 0) / 1024)
            step.pop("content_ids", None)
            return
        v = row[1]
        if isinstance(v, bytes):
            try:
                v = v.decode("utf-8")
            except UnicodeDecodeError:
                v = v.decode("utf-8", "replace")
        texts.append(v if isinstance(v, str) else "")
    if any(t is None for t in texts):
        step["result"] = "已写入（前后内容 Cursor 还没落盘）"
        return   # 留着 content_ids，下拍再试
    d = compute_diff(texts[0], texts[1])
    step["diff"] = d
    step["result"] = "+{} / −{}".format(d["plus"], d["minus"]) + \
        ("（新文件）" if not texts[0] and texts[1] else "")
    step.pop("content_ids", None)


def context_usage(comp):
    """composerData → 这个对话的上下文用量 {used, limit, pct, level, label, hint}；量不出回 None。

    纯函数（入参就是 composerData 那个 dict），便于用真实形态测。
    level：ok / warn / high / critical，前端据此上色；label 是给人看的一句话。
    """
    if not isinstance(comp, dict):
        return None
    limit = comp.get("contextTokenLimit")
    used = comp.get("contextTokensUsed")
    pct = comp.get("contextUsagePercent")
    try:
        limit = int(limit or 0)
        used = int(used or 0)
    except (TypeError, ValueError):
        return None
    if limit <= 0:
        return None
    try:
        pct = float(pct)
    except (TypeError, ValueError):
        pct = 0.0
    if pct <= 0:
        pct = used * 100.0 / limit
    pct = round(max(0.0, min(pct, 100.0)), 1)
    if pct >= CTX_CRITICAL_PCT:
        level, hint = "critical", "随时可能被截断或失忆，立刻交接"
    elif pct >= CTX_HIGH_PCT:
        level, hint = "high", "该交接了：再往下 Cursor 会自动摘要压缩，前文细节只剩摘要"
    elif pct >= CTX_WARN_PCT:
        level, hint = "warn", "过半了，找个收口的地方准备接力"
    else:
        level, hint = "ok", ""

    def _k(n):
        return "{:.0f}K".format(n / 1000.0) if n < 1000000 else "{:.2f}M".format(n / 1000000.0)

    return {"used": used, "limit": limit, "pct": pct, "level": level,
            "label": "上下文 {:.0f}%（{} / {}）".format(pct, _k(used), _k(limit)),
            "hint": hint}


def read_context_usage(uuid, appdata=None):
    """按 uuid 单独量一次上下文用量（一行查询，不读任何气泡）。

    给「不需要整条时间线、只想知道这个 tab 撑不撑得住」的调用方用（tab 上那颗牌、
    接手前的体检）。读不到回 None。"""
    if not uuid:
        return None
    db = _global_db(appdata)
    if db is None:
        return None
    con = _connect(db)
    if con is None:
        return None
    try:
        row = con.execute("SELECT value FROM cursorDiskKV WHERE key=?",
                          ("composerData:" + uuid,)).fetchone()
        return context_usage(_loads(row[0])) if row else None
    except Exception:
        return None
    finally:
        try:
            con.close()
        except Exception:
            pass


def compose_reply(steps):
    """这一轮给控制台看的「Cursor 详细回复」。

    rxyy 09-03 决策卡自由输入：「还有我说的前端实时输出 cursor 的详细回复呢？」
    工具芯片在时间线里，正文却混在最多 40 步里、收起面板后完全看不见。这里把所有
    text 步按序拼成一段；还没写出正文时，把最新一段思考顶上，避免干活中面板空白。
    纯函数，便于测。
    """
    texts = []
    thinks = []
    for s in steps or []:
        if not isinstance(s, dict):
            continue
        t = (s.get("text") or "").strip()
        if not t:
            continue
        if s.get("kind") == "text":
            texts.append(t)
        elif s.get("kind") == "thinking":
            thinks.append(t)
    if texts:
        return {"kind": "text", "text": "\n\n".join(texts)}
    if thinks:
        return {"kind": "thinking", "text": thinks[-1]}
    return None


def last_step_view(turn):
    """turn → tab 列表上那一句「第 N 步 · 运行命令 · pytest…」的料（零 I/O，纯函数）。"""
    if not isinstance(turn, dict) or not turn.get("steps"):
        return None
    st = turn["steps"][-1]
    if st["kind"] == "tool":
        label, summary = st.get("label") or st.get("name") or "工具", st.get("summary") or ""
        if st.get("blocking"):
            # Cursor 原生提问正等人点：tab 上必须说是「等你答」而不是含糊的「提问」——
            # 这一步不动不是它慢，是在等人（用户在手机上只看得见 tab 那一行）
            label += "·等你答"
    elif st["kind"] == "thinking":
        label, summary = "思考", st.get("text") or ""
    elif st["kind"] == "error":
        label, summary = "报错", st.get("text") or ""
    else:
        label, summary = "输出", st.get("text") or ""
    return {"n": int(turn.get("total_steps") or 0), "kind": st["kind"], "label": label,
            "summary": _one_line(summary, 80), "status": st.get("status", ""),
            "live": bool(turn.get("live")), "at": float(turn.get("updated_at") or 0)}


def read_turn(uuid, appdata=None, max_steps=DEFAULT_MAX_STEPS, cache=None, now=None):
    """还原 uuid 这个对话「这一轮」。返回 dict，库不可用 / 没这个对话 → None。

    cache：调用方给的 dict（bubbleId → step），跨拍复用；本函数只增不删（超过
    CACHE_LIMIT 才整体腾一半）。"""
    if not uuid:
        return None
    db = _global_db(appdata)
    if db is None:
        return None
    con = _connect(db)
    if con is None:
        return None
    now = now or time.time()
    try:
        row = con.execute("SELECT value FROM cursorDiskKV WHERE key=?",
                          ("composerData:" + uuid,)).fetchone()
        if not row:
            return None
        comp = _loads(row[0])
        if not isinstance(comp, dict):
            return None
        heads = [h for h in (comp.get("fullConversationHeadersOnly") or [])
                 if isinstance(h, dict) and h.get("bubbleId")]
        generating = bool(comp.get("generatingBubbleIds"))
        # 这一轮 = 最后一条用户气泡之后
        ui = -1
        for i in range(len(heads) - 1, -1, -1):
            if heads[i].get("type") == 1:
                ui = i
                break
        turn_heads = heads[ui + 1:]
        user_text, user_at = "", 0.0
        if ui >= 0:
            ub = con.execute("SELECT value FROM cursorDiskKV WHERE key=?",
                             ("bubbleId:{}:{}".format(uuid, heads[ui]["bubbleId"]),)).fetchone()
            u = _loads(ub[0]) if ub else None
            if isinstance(u, dict):
                user_text = _one_line(u.get("text") or "", 300)
                user_at = _iso_ts(u.get("createdAt"))
        if cache is None:
            cache = {}
        elif len(cache) > CACHE_LIMIT:
            for k in list(cache.keys())[: CACHE_LIMIT // 2]:
                cache.pop(k, None)
        window = turn_heads[-max_steps:] if max_steps else turn_heads
        steps, missing = [], 0
        n = len(window)
        for i, h in enumerate(window):
            bid = h["bubbleId"]
            is_last = (i == n - 1)
            step = cache.get(bid)
            if step is None or not _final(step, is_last):
                br = con.execute("SELECT value FROM cursorDiskKV WHERE key=?",
                                 ("bubbleId:{}:{}".format(uuid, bid),)).fetchone()
                if not br:
                    missing += 1   # 头已登记、正文还没落盘：正在写的那条
                    continue
                step = parse_bubble(_loads(br[0]))
                if step is None:
                    continue
                if step.get("content_ids") and step["status"] != "running":
                    _attach_edit_diff(con, step)
                # 前后内容还没落盘的改文件步骤不缓存（content_ids 还挂着），下拍重读再试
                if _final(step, is_last) and not step.get("content_ids"):
                    cache[bid] = step
            steps.append(step)
        # 还在生成的最后一步思考：标 running，前端才能画「思考中」闪光而不是收成一行省略号
        if generating and steps and steps[-1].get("kind") == "thinking":
            last = dict(steps[-1])
            last["status"] = "running"
            steps[-1] = last
        last_at = max([s.get("at") or 0 for s in steps] + [user_at])
        running = any(s["kind"] == "tool" and s["status"] == "running" for s in steps[-3:])
        live = bool(generating or running or missing
                    or (last_at and now - last_at < LIVE_WINDOW_SECS))
        sig_src = "|".join("{}:{}:{}".format(s.get("id", "")[:8], s.get("kind"),
                                            s.get("status", "")) for s in steps)
        sig_src += "|{}|{}|{}".format(len(turn_heads), int(generating), missing)
        if steps:
            sig_src += "|" + str(len(steps[-1].get("text") or steps[-1].get("result") or ""))
        # 上下文用量：composerData 这一行已经在手上，顺手量出来（零额外查询）。
        # 进 sig 只带 level 不带 pct：pct 每答一句都在涨，带上等于每拍都判「变了」、
        # UI 白重建整段 DOM（面板本来就是靠 sig 省重绘的）
        ctx = context_usage(comp)
        if ctx:
            sig_src += "|" + ctx["level"]
        reply = compose_reply(steps)
        if reply:
            sig_src += "|r:{}:{}".format(reply["kind"], len(reply["text"]))
        return {
            "uuid": uuid,
            "generating": generating,
            "context": ctx,
            "reply": reply,
            "live": live,
            "status": str(comp.get("status") or ""),
            "user_text": user_text,
            "started_at": user_at,
            "updated_at": last_at,
            "total_steps": len(turn_heads),
            "omitted": max(0, len(turn_heads) - len(window)),
            "pending_bubbles": missing,
            "steps": steps,
            "sig": hashlib.md5(sig_src.encode("utf-8")).hexdigest()[:12],
        }
    except Exception:
        return None
    finally:
        try:
            con.close()
        except Exception:
            pass
