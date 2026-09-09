# -*- coding: utf-8 -*-
"""团队面板的事实层：agent 自报的话必须有第二个来源对得上。

08-24 rxyy 当场戳破的两个洞，都是「只信自报、不查事实」：

1. 归组只读 tab 名前缀。be748934 的 tab 叫「rxyy tools·看板验收」，就被算进
   rxyy tools 这一队；它实际领的两张卡在「视频快编后端接口 / 视频快编前端
   编辑器」，人也在往 ctest 发布。名字晚改一步，队伍就假一步——rxyy 那句
   「这个项目就我一个人在搞，我哪来的队友」问的正是这个。
2. 工作树里的脏文件没有主人。谁都能说一句「这是队友未提交的改动」，没有任何
   归属证据；等真去查，那批改动的主人 08-21 就断线了，代码却在生产上跑了三天。

所以这里只做一件事：把「能查证的东西」摆出来——它领的是哪张卡、正在改哪个仓、
工作树里那些没提交的文件最后是谁碰的、那个人还在不在。判断留给面板和人，
本模块不替谁改归属。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# 卡片「还没验收完」= 人还在这张卡上。done/backlog 之类不算数。
ACTIVE_CARD_STATUS = ("in_progress", "in_review")
# in_progress 是正在干，比 in_review（可能挂了好几天）更能说明此刻在哪条线上。
STRONG_CARD_STATUS = ("in_progress",)

CARD_TTL = 20.0
DIRTY_TTL = 90.0
# 锁台账保留多久：超过就当「太久远，说不清是不是他改的」。
LEDGER_TTL = 14 * 24 * 3600
LEDGER_MAX = 4000
# git status 扫不动的巨型仓就放弃，别把面板拖住
GIT_TIMEOUT = 20


# ---------- 任务面板的卡（控制台落的盘，hub 只读） ----------

def default_board_store_path():
    """任务面板 .board.json 在这台机器上的真实位置。

    真相在 console/api/board/storage.py 的 default_data_dir()+STORE_NAME；控制台
    与 hub 是两个进程，没法 import，只能照着算一遍。改那边记得同步改这里。
    """
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if base:
        return Path(base) / "rxyy-tools-community" / "board" / ".board.json"
    return Path.home() / "rxyy-board" / ".board.json"


def board_store_path():
    """本次该读哪个 .board.json。

    测试里默认读不到真面板。console 那边早有 `_refuse_writing_the_real_board`
    （08-13 被 6 张漏写进真面板的 [SCB-*] 垃圾卡教训过），hub 这边只读、没有
    对应的闸，于是rxyy MCP 整套测试一直在读 rxyy 的真库：08-25 他建了张
    assignee 为 "c1ef737c" 的卡，test_team_track / test_task_root 里 conv 叫
    "c1" 的假会话当场被判成卡主，3 条测试凭空转红。要喂卡的测试自己传
    RXYY_BOARD_STORE 或打桩 Api._board_cards（现有测试本来就这么写）。
    """
    override = (os.environ.get("RXYY_BOARD_STORE") or "").strip()
    if override:
        return Path(override)
    test_argv = " ".join(sys.argv).lower()
    if ("PYTEST_CURRENT_TEST" in os.environ or os.environ.get("CHIJIU_UNDER_TEST")
            or "unittest" in test_argv or "pytest" in test_argv
            or Path(sys.argv[0]).name.startswith("test_")):
        return Path(tempfile.gettempdir()) / "rxyy-tools-community-tests" / "no-such-board.json"
    return default_board_store_path()


def _read_cards(path):
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    cards = data.get("cards") if isinstance(data, dict) else data
    return [c for c in (cards or []) if isinstance(c, dict)]


def active_cards_of(conv_key, cards):
    """这个会话名下还没验收完的卡，in_progress 排前面。"""
    conv = str(conv_key or "").strip()
    if not conv:
        return []
    out = []
    for card in cards or []:
        if card.get("archived"):
            continue
        if str(card.get("status") or "") not in ACTIVE_CARD_STATUS:
            continue
        who = str(((card.get("assignee") or {}) if isinstance(
            card.get("assignee"), dict) else {}).get("conversation_id") or "")
        if not who or not _same_conv(who, conv):
            continue
        out.append({
            "id": str(card.get("id") or "")[:12],
            "title": str(card.get("title") or "")[:60],
            "project": str(card.get("project") or "")[:40],
            "status": str(card.get("status") or ""),
        })
    out.sort(key=lambda c: 0 if c["status"] in STRONG_CARD_STATUS else 1)
    return out


def _same_conv(a, b):
    """会话 ID 有时记全、有时只记前 8 位——认这两种写法，别的一律要全等。

    旧写法 ``min(len(a), len(b), 8)`` 把「短」既当宽容又当漏洞：短的那侧有
    几位就只比几位，ID 越短命中越多（两位十六进制平均套住 1/256 张卡，一位
    1/16）。conversation_id 是 agent 报到时自己填的，填短了它就会在团队面板上
    顶着别人的卡、被 _card_project_of 归进别人的项目。08-25 实测：conv 只有
    "c1" 的会话被判成看板上 "c1ef737c" 那张卡的主人，广播回「没有队友」。

    非十六进制的记号（控制台自己是 "console"，走查 agent 是 "walkthru-agent"）
    按前 8 位比同样是错的，所以短写只认「正好 8 位」这一种形态。
    """
    a, b = str(a or "").strip().casefold(), str(b or "").strip().casefold()
    if not a or not b:
        return False
    if a == b:
        return True
    short, full = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) == 8 and full.startswith(short)


def known_projects(all_cards, project_key=None):
    """任务面板上真实存在过的项目名（含已完成的卡：项目不会因为卡做完就不存在）。"""
    key = project_key or (lambda x: str(x or "").strip().casefold())
    out = set()
    for card in all_cards or []:
        if card.get("archived"):
            continue
        name = str(card.get("project") or "").strip()
        if name:
            out.add(key(name))
    return out


def project_conflict(self_project, cards, vocab=None, project_key=None):
    """自报项目与「领的卡」对不对得上。对得上或无从判断都返回空串。

    两道闸，缺一不可，否则这条告警会自己把自己喊废：

    1. 有卡可查，且一张都不在它自报的项目下——没领卡是常态，不算证据。
    2. 它自报的那个名字，在任务面板上**确实是一个项目**（vocab）。

    第 2 道是 08-24 拿真实数据试出来的：只做第 1 道时，8 个会话里喊了 3 个，
    全是虚的——「快编」是「音频快编后端接口」的简称、「录播客户端」对
    「智慧云广播录播播出端」、「心理评测」对「心理健康管理平台」，人都在自己
    卡上，只是名字写得粗。真正该喊的那个（自报 rxyy tools、两张卡全在视频快编）
    的特征恰恰是：rxyy tools 本身就是面板上一个正经项目，它却一张卡都不在里面。
    说不出这句话的时候就闭嘴，比喊错三次强。
    """
    key = project_key or (lambda x: str(x or "").strip().casefold())
    mine = key(self_project)
    if not mine:
        return ""
    cards = [c for c in (cards or []) if c.get("project")]
    if not cards:
        return ""
    if any(key(c["project"]) == mine for c in cards):
        return ""
    if vocab is not None and mine not in {key(v) for v in vocab}:
        return ""
    names, seen = [], set()
    for c in cards:
        if c["project"] in seen:
            continue
        seen.add(c["project"])
        names.append(c["project"])
    return "自报「{}」，但它领的卡在「{}」".format(
        str(self_project).strip(), "」「".join(names[:2]))


# ---------- 脏文件溯源：谁最后碰的、他还在不在 ----------

def norm_rel(root, path_text):
    """把 git 报的路径折成锁台账那套键：相对、正斜杠、小写。"""
    text = str(path_text or "").strip().strip('"')
    if not text:
        return ""
    text = text.replace("\\", "/")
    root_s = str(root or "").replace("\\", "/").rstrip("/")
    if root_s and text.lower().startswith(root_s.lower() + "/"):
        text = text[len(root_s) + 1:]
    return text.lstrip("./").lower()


def git_dirty(root, timeout=GIT_TIMEOUT):
    """工作树里没提交的文件。返回 [(相对键, 原样路径, 状态码)]。

    带 --untracked-files=normal：孤儿 WIP 里最容易被忘掉的恰恰是新增文件。
    """
    root = str(root or "")
    if not root or not Path(root, ".git").exists():
        return []
    try:
        proc = subprocess.run(
            ["git", "-c", "core.quotepath=false", "status", "--porcelain",
             "--untracked-files=normal"],
            cwd=root, timeout=timeout, capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    out = []
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        if len(line) < 4:
            continue
        code, path_text = line[:2].strip() or "?", line[3:]
        # 改名是 "old -> new"，只认新名
        if " -> " in path_text:
            path_text = path_text.split(" -> ", 1)[1]
        key = norm_rel(root, path_text)
        if key:
            out.append((key, path_text.strip('"'), code))
    return out


class WipLedger:
    """文件 → 最后是谁在改。

    锁只在 agent 干活那几分钟存在，stop 钩子一收就没了；等发现工作树脏，锁
    早不在了。所以 hub 每次看见锁就往台账记一笔，锁没了记录还在——这才有
    「这个脏文件最后是谁碰的」可查。
    """

    def __init__(self, ttl=LEDGER_TTL, cap=LEDGER_MAX):
        self.ttl = ttl
        self.cap = cap
        self.rows = {}
        self._lock = threading.Lock()

    def observe(self, items, now=None):
        """吃一遍文件占用看板的条目（lock_board()["items"] 那个形状）。"""
        now = now or time.time()
        changed = False
        with self._lock:
            for item in items or []:
                root = str(item.get("root") or "")
                key = norm_rel(root, item.get("file"))
                if not root or not key:
                    continue
                cell = (hub_norm(root), key)
                prev = self.rows.get(cell)
                row = {
                    "root": root,
                    "file": key,
                    "owner": str(item.get("owner") or "")[:8],
                    "tab": str(item.get("tab") or "")[:40],
                    "ts": float(now),
                    "edits": int(item.get("edits") or 0),
                }
                if prev and prev.get("owner") == row["owner"]:
                    row["ts"] = float(now)
                    row["edits"] = max(int(prev.get("edits") or 0), row["edits"])
                self.rows[cell] = row
                changed = True
            self._evict(now)
        return changed

    def _evict(self, now):
        for cell, row in list(self.rows.items()):
            if now - float(row.get("ts") or 0) > self.ttl:
                self.rows.pop(cell, None)
        if len(self.rows) > self.cap:
            for cell, _row in sorted(
                    self.rows.items(),
                    key=lambda kv: float(kv[1].get("ts") or 0))[:len(self.rows) - self.cap]:
                self.rows.pop(cell, None)

    def lookup(self, root, key):
        with self._lock:
            return self.rows.get((hub_norm(root), key))

    def export(self):
        with self._lock:
            return [dict(v, root=k[0], file=k[1]) for k, v in self.rows.items()]

    def load(self, rows):
        with self._lock:
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                root, key = str(row.get("root") or ""), str(row.get("file") or "")
                if not root or not key:
                    continue
                self.rows[(hub_norm(root), key)] = {
                    "root": root, "file": key,
                    "owner": str(row.get("owner") or "")[:8],
                    "tab": str(row.get("tab") or "")[:40],
                    "ts": float(row.get("ts") or 0),
                    "edits": int(row.get("edits") or 0),
                }
            self._evict(time.time())


def hub_norm(path):
    """和 hub.norm_root 同口径，但不 import hub（本模块要能单独测）。"""
    try:
        return os.path.normcase(os.path.normpath(str(path or "")))
    except Exception:  # noqa: BLE001
        return str(path or "")


def attribute_dirty(root, dirty, ledger, live_of, now=None):
    """把脏文件和「最后编辑它的会话」对上，并判断那个会话还在不在。

    live_of(owner8) 返回 None（查无此人/早没了）或
    {"tab":…, "conv":…, "online":bool, "live":"waiting|working|…"}。

    **孤儿只算「有主而主不在」**（offline / gone）。台账里压根没记过的
    （unknown）照样列出来，但不进告警：那多半是工具产物、你自己新建的文件、
    或者台账过期，凭「没人锁过」就喊「没人守着」，一棵树能喊出七八条虚的
    （08-24 部署后实测：.cursor/、.playwright-mcp/、别的项目掉进来的
    package.json 全被喊成孤儿）。喊错的代价是下次真孤儿也没人看。
    """
    now = now or time.time()
    ws = str(root or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    rows = []
    for key, shown, code in dirty or []:
        mark = ledger.lookup(root, key)
        owner = (mark or {}).get("owner") or ""
        who = live_of(owner) if owner else None
        if who:
            state = "online" if who.get("online") else "offline"
        elif owner:
            state = "gone"
        else:
            state = "unknown"
        rows.append({
            "file": shown,
            "key": key,
            "code": code,
            "untracked": code == "??",
            # 几个工作区的队伍合成一行时，不带仓名的文件清单是在撒谎
            "root": str(root or ""),
            "ws": ws,
            "owner": owner,
            "tab": (who or {}).get("tab") or (mark or {}).get("tab") or "",
            "conv": (who or {}).get("conv") or owner,
            "state": state,
            "orphan": state in ("offline", "gone"),
            "last_edit": float((mark or {}).get("ts") or 0),
            "age": max(0.0, now - float((mark or {}).get("ts") or now)),
        })
    rows.sort(key=lambda r: (not r["orphan"], r["state"] == "unknown",
                             -r["last_edit"]))
    return rows


def orphan_summary(rows):
    """给面板顶一句话：几份有主而主已不在，另有几份查不到主。"""
    rows = rows or []
    orphans = [r for r in rows if r.get("orphan")]
    unowned = [r for r in rows if r.get("state") == "unknown"]
    if not orphans:
        return {"count": 0, "unowned": len(unowned), "text": "", "owners": []}
    owners, seen = [], set()
    for r in orphans:
        tag = r.get("tab") or (r.get("owner") and r["owner"][:8]) or ""
        if not tag or tag in seen:
            continue
        seen.add(tag)
        owners.append(tag)
    text = "{} 份未提交的改动没人守着（最后编辑：{}，都已离线）".format(
        len(orphans), "、".join(owners[:3]) or "查不到 tab 名")
    if unowned:
        text += "；另有 {} 份查不到最后是谁改的".format(len(unowned))
    return {"count": len(orphans), "unowned": len(unowned),
            "text": text, "owners": owners[:3]}
