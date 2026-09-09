# -*- coding: utf-8 -*-
"""决策卡片：zhi 的 `card` 参数（对标 BajieAsk wait_message 的 card）。

predefined_options 是一维字符串、单选、≤4 条——够用来「发 / 等一下」，不够用来
「三个纠结点一次问完、每个选项带取舍说明、还能让用户补一句」。09-01 rxyy 拍板把
BajieAsk 的决策卡搬进rxyy MCP：多问 + 每项 detail + recommended + 自由输入 +
（可选）倒计时代决。

server 与 hub 是两个进程，这个模块两边都 import：server 侧只做 normalize（agent
传得随意：字符串 JSON / 旧格式 {id,title,multiple,options} / 选项是纯字符串都收），
hub 侧负责渲染进气泡、写进记录文件，并把用户的答复拼成 agent 读得懂的文本。
"""
import hashlib
import html
import json
import re

MAX_QUESTIONS = 6
MAX_OPTIONS = 8
LABEL_MAX = 60
PROMPT_MAX = 300
TITLE_MAX = 200
# detail 不设上限（BajieAsk 规则：写透别省），只挡住明显是把整篇正文塞进来的
DETAIL_MAX = 4000
AUTO_TIMEOUT_MAX = 3600
MACHINE_TAG = "chijiu-decision"


def _stable_id(title, questions):
    h = hashlib.sha1()
    h.update((title or "").encode("utf-8"))
    for q in questions:
        h.update(("|" + q["prompt"]).encode("utf-8"))
        for o in q["options"]:
            h.update(("|" + o["label"]).encode("utf-8"))
    return "card-" + h.hexdigest()[:8]


def normalize_card(raw):
    """随手传的卡片 → 规范结构；不是卡片 / 一个可答的问题都没有 → None。

    规范结构：
      {"id", "title",
       "questions": [{"id", "prompt", "options": [{"id","label","detail","recommended"}],
                      "multiple", "allowFreeText"}],
       "autoDecide": {"enabled", "onResolve": "adopt"|"delegate", "timeoutSec"}}
    兼容旧格式 {id,title,multiple,options:[…]}（当成单问）；选项可以是纯字符串。
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "ignore")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    if not isinstance(raw, dict):
        return None
    qs = raw.get("questions")
    if not isinstance(qs, list) or not qs:
        if raw.get("options"):
            qs = [{"id": "q1", "prompt": raw.get("title") or "",
                   "options": raw.get("options"),
                   "multiple": raw.get("multiple", False),
                   "allowFreeText": raw.get("allowFreeText", True)}]
        else:
            return None
    out_qs = []
    for i, q in enumerate(qs[:MAX_QUESTIONS]):
        if isinstance(q, str):
            q = {"prompt": q, "options": []}
        if not isinstance(q, dict):
            continue
        opts = []
        seen = set()
        for j, o in enumerate(list(q.get("options") or [])[:MAX_OPTIONS]):
            if isinstance(o, str):
                o = {"label": o}
            if not isinstance(o, dict):
                continue
            label = str(o.get("label") or o.get("title") or "").strip()[:LABEL_MAX]
            if not label:
                continue
            oid = str(o.get("id") or "").strip() or "opt%d" % (j + 1)
            if oid in seen:
                oid = "%s_%d" % (oid, j + 1)
            seen.add(oid)
            opts.append({"id": oid, "label": label,
                         "detail": str(o.get("detail") or o.get("desc") or "").strip()[:DETAIL_MAX],
                         "recommended": bool(o.get("recommended"))})
        allow_free = bool(q.get("allowFreeText", True))
        if not opts and not allow_free:
            continue
        prompt = str(q.get("prompt") or q.get("title") or q.get("question") or "").strip()
        out_qs.append({"id": str(q.get("id") or "").strip() or "q%d" % (i + 1),
                       "prompt": prompt[:PROMPT_MAX],
                       "options": opts,
                       "multiple": bool(q.get("multiple")),
                       "allowFreeText": allow_free})
    if not out_qs:
        return None
    title = str(raw.get("title") or "").strip()[:TITLE_MAX]
    ad = raw.get("autoDecide") if isinstance(raw.get("autoDecide"), dict) else {}
    on_resolve = str(ad.get("onResolve") or "adopt").strip().lower()
    if on_resolve not in ("adopt", "delegate"):
        on_resolve = "adopt"
    try:
        timeout = int(float(ad.get("timeoutSec") or 0))
    except (TypeError, ValueError):
        timeout = 0
    timeout = max(0, min(timeout, AUTO_TIMEOUT_MAX))
    enabled = bool(ad.get("enabled")) and timeout > 0
    # 「让系统替我决定」采纳推荐项：一个推荐项都没有的卡片开倒计时等于到点空答
    if enabled and on_resolve == "adopt" and not any(
            o["recommended"] for q in out_qs for o in q["options"]):
        enabled = False
    return {"id": str(raw.get("id") or "").strip() or _stable_id(title, out_qs),
            "title": title, "questions": out_qs,
            "autoDecide": {"enabled": enabled, "onResolve": on_resolve,
                           "timeoutSec": timeout}}


def card_to_text(card):
    """纯文本版（记录文件 / 手机页 / 不会画卡片的客户端）。"""
    if not card:
        return ""
    lines = ["【决策卡片】" + (card.get("title") or "")]
    for qi, q in enumerate(card["questions"], 1):
        lines.append("%d. %s%s" % (qi, q["prompt"] or "（请选择）",
                                   "（可多选）" if q["multiple"] else ""))
        for o in q["options"]:
            lines.append("   - %s%s%s" % (
                o["label"], "（推荐）" if o["recommended"] else "",
                "：" + o["detail"] if o["detail"] else ""))
        if q["allowFreeText"]:
            lines.append("   - 其他…（自由输入）")
    ad = card.get("autoDecide") or {}
    if ad.get("enabled"):
        lines.append("（%d 秒无人作答则%s）" % (
            ad["timeoutSec"], "采纳推荐项" if ad["onResolve"] == "adopt" else "交回 AI 自定"))
    return "\n".join(lines)


def card_to_html(card):
    """气泡里的静态呈现（交互控件由控制台输入区画；这里保证历史记录 / 分享页看得懂）。"""
    if not card:
        return ""
    e = html.escape
    parts = ['<div class="dcard"><div class="dcard-title">🗂 决策卡片 · %s</div>'
             % e(card.get("title") or "")]
    for qi, q in enumerate(card["questions"], 1):
        parts.append('<div class="dcard-q"><div class="dcard-prompt">%d. %s%s</div>'
                     % (qi, e(q["prompt"] or "（请选择）"),
                        ' <span class="dcard-multi">可多选</span>' if q["multiple"] else ""))
        for o in q["options"]:
            parts.append('<div class="dcard-opt"><b>%s</b>%s%s</div>' % (
                e(o["label"]),
                ' <span class="dcard-rec">推荐</span>' if o["recommended"] else "",
                '<div class="dcard-detail">%s</div>' % e(o["detail"]) if o["detail"] else ""))
        if q["allowFreeText"]:
            parts.append('<div class="dcard-opt dcard-free">其他…（可自由输入）</div>')
        parts.append("</div>")
    parts.append("</div>")
    return "".join(parts)


def decision_reply(card, answers, mode="manual"):
    """用户的答复 → 发回 agent 的文本：一行人能读的 + 一个机器标签。

    answers: {questionId: {"selected": [optionId, …], "text": "自由输入"}}
    mode: manual（人点的）| adopt_recommended（倒计时到点采纳推荐）| delegate_system（交回 AI）
    返回 (文本, 选中的选项 label 列表)。label 列表另走 selected_options，
    只认 predefined_options 老路的 agent 也拿得到一份。
    """
    answers = answers if isinstance(answers, dict) else {}
    human, items, labels_all = [], [], []
    for q in card["questions"]:
        a = answers.get(q["id"]) or {}
        if not isinstance(a, dict):
            a = {}
        sel = [str(x) for x in (a.get("selected") or []) if str(x).strip()]
        if mode == "adopt_recommended" and not sel:
            sel = [o["id"] for o in q["options"] if o["recommended"]]
        by_id = {o["id"]: o["label"] for o in q["options"]}
        labels = [by_id[x] for x in sel if x in by_id]
        free = str(a.get("text") or "").strip()
        parts = labels + ([free] if free else [])
        if mode == "delegate_system" and not parts:
            parts = ["（交给 AI 自定）"]
        human.append("%s：%s" % (q["prompt"] or q["id"], "、".join(parts) if parts else "（未选）"))
        items.append({"questionId": q["id"], "optionIds": [x for x in sel if x in by_id],
                      "labels": labels, "freeText": free})
        labels_all.extend(labels)
    payload = {"requestId": card["id"], "mode": mode, "items": items}
    tag = "<%s answer='%s'/>" % (
        MACHINE_TAG, json.dumps(payload, ensure_ascii=False).replace("'", "&#39;"))
    return "[决策答复] " + "；".join(human) + "\n" + tag, labels_all


_TAG_RE = re.compile(r"\s*<%s answer='(.*?)'/>\s*" % MACHINE_TAG, re.S)
_MODE_LABEL = {"manual": "手动回答", "adopt_recommended": "到点采纳推荐项",
               "delegate_system": "交给 AI 自定"}


def strip_machine_tag(text):
    """把发给 agent 的机器标签从「给人看」的正文里摘掉（历史记录 / 侧栏预览 / 分享页）。"""
    return _TAG_RE.sub("", str(text or "")).rstrip()


def reply_to_html(text):
    """「[决策答复] …」+ 机器标签 → 聊天里的答复卡（Bajie 同款：提问 N / 回答 N 分块）。

    不是决策答复的文本返回 None，调用方照旧走 Markdown。09-07 rxyy 截图：控制台把
    `<chijiu-decision answer='{…}'/>` 原样糊在用户气泡里，一行 JSON 谁也不看。
    机器标签只给 agent，人看卡。
    """
    raw = str(text or "")
    if not raw.startswith("[决策答复]"):
        return None
    m = _TAG_RE.search(raw)
    payload = None
    if m:
        try:
            payload = json.loads(m.group(1).replace("&#39;", "'"))
        except Exception:  # noqa: BLE001
            payload = None
    e = html.escape
    parts = ['<div class="dc-reply"><div class="dc-reply-head"><span class="dc-reply-tag">决策答复</span>']
    mode = (payload or {}).get("mode") if isinstance(payload, dict) else None
    if mode in _MODE_LABEL:
        parts.append('<span class="dc-reply-mode">%s</span>' % e(_MODE_LABEL[mode]))
    parts.append("</div>")
    human = strip_machine_tag(raw)[len("[决策答复]"):].strip()
    rows = []
    items = (payload or {}).get("items") if isinstance(payload, dict) else None
    if isinstance(items, list) and items:
        # 问题文字在 human 行里（按「；」分段、「：」前一截），答案用机器标签里的 labels/freeText，
        # 免得答案里自带「：」「；」时切错
        prompts = [seg.split("：", 1)[0] for seg in human.split("；")] if human else []
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                continue
            ans = list(it.get("labels") or [])
            if it.get("freeText"):
                ans.append(str(it["freeText"]))
            q = prompts[i] if i < len(prompts) else (it.get("questionId") or "")
            rows.append((q, ans))
    else:
        for seg in human.split("；"):
            q, _, a = seg.partition("：")
            rows.append((q, [x for x in a.split("、") if x] if a else []))
    for i, (q, ans) in enumerate(rows, 1):
        parts.append('<div class="dc-reply-item"><div class="dc-reply-q"><span class="dc-reply-n">提问 %d</span>%s</div>'
                     '<div class="dc-reply-a"><span class="dc-reply-n">回答 %d</span>%s</div></div>' % (
                         i, e(q), i,
                         "".join('<span class="dc-reply-pick">✓ %s</span>' % e(x) for x in ans)
                         if ans else '<span class="dc-reply-none">（未选）</span>'))
    parts.append("</div>")
    return "".join(parts)
