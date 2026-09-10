# -*- coding: utf-8 -*-
"""hub 的 API 层（Api 类，151 个方法）：UI / 网关 / 分享站 / 工作流都经它操作 Hub。

2026-08-12 第一刀从 hub.py 整体抽出（docs/hub拆分手术方案-2026-08-12.md），行为零变化。

约定：
- 对 hub 命名空间里的一切符号——hub 自己定义的（HUB 单例、log_event 等工具、
  DEFAULTS 等常量），以及 hub 从本仓模块引进来的（session_locator / share_server
  的函数、DATA_DIR、workflow_mod、live_runtime）——一律走 `hub.xxx` 属性访问，
  顶层只 import 标准库。三个理由：
  1) 顶层必须 `import hub`（模块对象引用）而非 from-import：本模块在 hub 加载
     中途被导入，那一刻 hub 还是部分初始化模块；Api 方法都在运行期才执行，
     彼时 hub 必已加载完。
  2) 测试打桩历来打在 hub 上（patch.object(hub, "find_cursor_transcript") /
     "log_event" / "WORKFLOW" …），from-import 会让桩拦不住 Api 内部的调用。
  3) live_runtime 在 hub 顶部有 ImportError 兜底（老包缺模块时用假类顶替），
     自行 import 会破坏兜底语义。
- 本模块不是入口：永远 `import hub` 再用 hub.Api，别直接首个 import hub_api
  （hub 与 hub_api 的初始化互锁，直接进会撞部分初始化）。
"""
import base64
import datetime
import hashlib
import html as html_mod
import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import hub


def _html_text(h):
    """气泡 html → 一行纯文本（摘要 / 接手提示词共用的去标签写法）。"""
    t = re.sub(r"<[^>]+>", "", str(h or ""))
    return re.sub(r"[ \t\r\n]+", " ", html_mod.unescape(t)).strip()


# 事实层的缓存与台账都挂在模块上，不挂实例：Api() 在很多地方是现 new 的，
# 挂实例等于每次都冷启动（_board_cache 那处历史包袱已经踩过一次）。
_FACTS = {
    "ledger": None,          # team_facts.WipLedger，首次用到才建
    "cards": (0.0, []),      # (取的时刻, 任务面板卡片)
    "wip": {},               # normroot -> {"ts":…, "rows":[…], "summary":{…}}
    "scanning": set(),       # 正在后台扫的根，防同一根并发起多条 git
    "saved_ts": 0.0,         # 台账上次落盘的时刻（限频用）
}
_FACTS_LOCK = threading.Lock()


class Api:
    RECALL_DELAY_SECS = 5.0

    def __init__(self):
        self._locator_cache = {}
        self._board_cache = None  # (取的时刻, agentboard 结果)，见 agentboard 的说明

    def get_session_locator(self, session_id, locate_transcript=True):
        s = hub.HUB.sessions.get(session_id)
        if not s:
            return {"ok": False, "error": "会话不存在"}
        prompt = hub.build_locator_prompt(s.cwd, s.name, s.conv_key)
        if not locate_transcript:
            return {
                "ok": True,
                "prompt": prompt,
                "conversation_id": s.conv_key,
            }
        # 标题同步线程若已定位过该会话，直接复用其结果
        tp = getattr(s, "transcript_path", None)
        if tp and Path(tp).is_file():
            self._locator_cache[(s.cwd, s.conv_key)] = tp
        else:
            cursor_uuid = getattr(s, "cursor_uuid", None)
            if cursor_uuid:
                guess = (Path.home() / ".cursor" / "projects"
                         / hub.cursor_project_slug(s.cwd)
                         / "agent-transcripts" / cursor_uuid / (cursor_uuid + ".jsonl"))
                if guess.is_file():
                    self._locator_cache[(s.cwd, s.conv_key)] = str(guess)
        key = (s.cwd, s.conv_key)
        cached = self._locator_cache.get(key)
        path = Path(cached) if cached and Path(cached).is_file() else None
        if path is None:
            path = hub.find_cursor_transcript(s.cwd, s.conv_key)
            if path:
                self._locator_cache[key] = str(path)
        if not path:
            return {
                "ok": False,
                "error": "未找到对应 Cursor transcript（进行中的会话 Cursor 常常还没写盘，结束后再试）；可先复制定位提示词",
                "prompt": prompt,
                "conversation_id": s.conv_key,
            }
        return {
            "ok": True,
            "prompt": prompt,
            "conversation_id": s.conv_key,
            "cursor_session_id": path.stem,
            "cursor_transcript_path": str(path),
        }

    def agentboard(self):
        """同工作区文件互斥看板：谁占着哪个文件、占了多久、谁在排队。

        数据由 ~/.cursor/hooks 里的 agentboard 钩子写在
        `<工作区>/.chijiu-tmp/agentboard.json`，hub 只读不写（强解走
        agentboard_release）。钩子里的 agent 标识就是 Cursor 的会话 UUID，
        正好能跟 tab 的 cursor_uuid 对上，于是能显示成「哪个 tab 占着」。

        带 2 秒缓存：活跃度判定会按 agent 逐个来问它（见 unclaimed_lock_owner），
        每问一次就把所有工作区的 json 重读一遍的话，十来个 tab 就能把每秒一次的
        get_state 拖到超时（07-31 实测控制台直接卡住）。
        """
        cached = getattr(self, "_board_cache", None)
        if cached and time.time() - cached[0] < 2:
            return cached[1]
        now = time.time() * 1000
        roots, tabs = [], {}
        with hub.HUB.lock:
            for sid in hub.HUB.order:
                s = hub.HUB.sessions.get(sid)
                if not s:
                    continue
                if s.cwd and s.cwd not in roots:
                    roots.append(s.cwd)
                cu = getattr(s, "cursor_uuid", None)
                if cu:
                    tabs[cu] = s.name
        items = []
        for root in roots:
            bp = Path(root) / ".chijiu-tmp" / "agentboard.json"
            try:
                board = json.loads(bp.read_text(encoding="utf-8"))
            except Exception:
                continue
            agents = board.get("agents") or {}
            queue = board.get("queue") or {}
            for key, lk in (board.get("locks") or {}).items():
                owner = lk.get("owner") or ""
                info = agents.get(owner) or {}
                since = lk.get("since") or now
                idle = int(max(0, now - (lk.get("renewed") or since)) / 1000)
                items.append({
                    "root": root,
                    "project": Path(root).name,
                    "file": key,
                    "owner": owner[:8],
                    "tab": tabs.get(owner) or info.get("tab") or "",
                    "model": info.get("model") or "",
                    "held": int(max(0, now - since) / 1000),
                    "idle": idle,
                    "edits": lk.get("edits") or 0,
                    "waiting": len(queue.get(key) or []),
                    # 超过 TTL 没续期的锁其实已经拦不住人了（下一个来抢的会直接接管），
                    # 但它会一直挂在看板上，标出来免得看着以为还占着
                    "stale": idle > 600,
                })
        items.sort(key=lambda x: -x["held"])
        out = {"ok": True, "items": items, "count": len(items)}
        self._board_cache = (time.time(), out)
        return out

    def plugins_state(self):
        """插件生态面板数据源（日报插件方案批③，只读）：内置插件的自描述与状态。

        单插件异常不连坐由 plugin_registry.states() 保证；这里再兜一层——
        注册表本身炸了也只回 ok=False，不连坐 UI 轮询。"""
        try:
            import plugin_registry
            plugin_registry.ensure_builtin_plugins()
            return {"ok": True, "plugins": plugin_registry.states()}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": "插件状态不可用：{}".format(e)}

    # ---------- 团队面板：把「同工作区一堆 tab」看成「一个项目里的一支队伍」 ----------
    # 角色不只是标签：报到/接手提示词会按角色写明纪律，审查角色被明确要求不动代码。
    TEAM_ROLE_LABELS = {
        "owner": "负责人", "impl": "实现", "review": "审查",
        "test": "测试", "readonly": "只读",
    }
    TEAM_ROLE_RULES = {
        "owner": "统筹本项目：可以改代码，方案冲突时由你拍板，收工前确认其他人的活都落地了。",
        "impl": "只做分配给你的改动，别顺手重构无关代码；改完在团队面板点「送审」等审查结论。",
        "review": "只读审查，禁止改任何代码：逐条指出正确性/边界/回归风险，"
                  "最后单独一行给结论「【审查通过】」或「【打回】+ 一句话原因」。",
        "test": "只跑测试与复现问题，不改功能代码；把失败用例与复现步骤原样贴回来。",
        "readonly": "只读：可以看代码、回答问题，任何写文件/改配置/跑改动性命令都不要做。",
    }

    _ROLE_REVIEW_RE = re.compile(r"审页|审查|复审|(?<![A-Za-z])review(?![A-Za-z])", re.I)
    _ROLE_IMPL_RE = re.compile(r"剩余|实现|开发|(?<![A-Za-z])impl(?![A-Za-z])", re.I)

    @classmethod
    def _infer_role_from_name(cls, name):
        """从 tab 名猜角色：用户常把审查写成「·平台审页」，实现写成「·平台剩余页」。

        只在没手设角色时当兜底。审查优先：名字里同时出现「审」和「实现」时按审查。
        """
        text = str(name or "")
        if cls._ROLE_REVIEW_RE.search(text):
            return "review"
        if cls._ROLE_IMPL_RE.search(text):
            return "impl"
        return ""

    def _team_role(self, session):
        stored = str((hub.HUB.cfg.get("team_roles") or {}).get(
            getattr(session, "conv_key", "") or "", "") or "")
        if stored:
            return stored
        return self._infer_role_from_name(getattr(session, "name", "") or "")

    def unclaimed_lock_owner(self, s):
        """文件占用看板上「本工作区里没被任何 tab 认领」的活跃锁属于谁。

        agent 埋头改文件、既不 zt 上报、Cursor 流水又没定位到时，控制台只剩「无输出
        信号」可说（07-31 实测：AICodebrain 那个 tab 明明在干活，面板上是个灰点）。
        但改文件必然抢锁，锁上带着它的 Cursor 会话 ID——本工作区只剩它一个没对上号
        时，这把锁就是它的：既补上「在干活」的硬证据，也顺手把身份认了。
        只在「有且仅有一个未认领 owner、且没有第二个没对上号的 tab」时才敢下结论。
        """
        root = hub.norm_root(s.cwd)
        if not root:
            return ""
        # 只吃现成的缓存，绝不在这里读盘、更不能取 HUB.lock：本方法是在
        # team_state/get_state 已经持着 HUB.lock 的循环里被调用的，取第二次
        # 必然自锁死（HUB.lock 是普通 Lock 不是 RLock；07-31 实测把控制台整个卡死）。
        cached = getattr(self, "_board_cache", None)
        if not cached or time.time() - cached[0] > 5:
            return ""
        items = [x for x in (cached[1] or {}).get("items") or []
                 if not x.get("stale") and hub.norm_root(x.get("root")) == root]
        if not items:
            return ""
        peers = [x for x in list(hub.HUB.sessions.values())
                 if x.id != s.id and hub.norm_root(x.cwd) == root]
        if any(not getattr(x, "cursor_uuid", "") for x in peers if x.connected):
            return ""  # 还有别的 tab 也没对上号，这把锁归谁说不准
        claimed = {str(getattr(x, "cursor_uuid", "") or "")[:8] for x in peers}
        owners = {str(x.get("owner") or "") for x in items} - claimed - {""}
        return owners.pop() if len(owners) == 1 else ""

    def session_label(self, s):
        """tab 上显示的名字：哪个工作区的 agent、在哪条业务线上做哪块功能。

        用户的固定用法是「新对话一律先用报到提示词接入 → 再从团队面板派活」，于是
        所有 tab 都叫「待命·<工作区>N」，派完活名字也不变，一排看过去分不清谁在干嘛。
        分工/业务线/归属一改这里就跟着变——团队成员本来就是流动的：接手、原活干完
        被派新活，名字都得跟上。分工和业务线都没有时保持原名不动。

        **agent 自报的名字最大**（08-07 用户拍板）：它是唯一知道自己在做哪个项目、
        哪块功能的人。在它开口之前，才轮到「派活那句话截 24 字」这个占位分工顶上
        ——那东西只是没名字时的应急，一旦 agent 报了真名就该让位，否则用户看到的
        永远是自己第一句话的前 24 个字。用户在面板上手填的分工不受影响（那是人的
        意思，不是自动截来的），仍旧照显示。
        """
        assign = self._team_assign(s)
        if getattr(s, "agent_named", False) and self._assign_is_auto(s):
            assign = ""
        bits = [x for x in (self._team_track(s), assign) if x]
        if not bits:
            return s.name
        here = Path(s.cwd).name if s.cwd else ""
        proj = (getattr(s, "agent_project", "")
                or (Path(hub.task_root_of(s)).name if hub.task_root_of(s) else ""))
        core = "·".join(([proj] if proj else []) + bits)
        return "{}›{}".format(here, core) if here and here != proj else core

    def _team_assign(self, session):
        """这个 tab 负责哪块功能（多个「实现」并行时唯一能分清谁是谁的东西）。"""
        return str((hub.HUB.cfg.get("team_assign") or {}).get(
            getattr(session, "conv_key", "") or "", "") or "")

    def _assign_is_auto(self, session):
        """这条分工是自动截来的占位，还是用户/agent 真填的？

        分开记而不是靠猜内容：用户在面板上手填的分工必须原样保留，只有
        _auto_label_on_dispatch 那条「把派活第一句截 24 字」的应急占位才该被
        agent 的自报名顶掉。

        标记是 08-07 才加的，在那之前落库的分工一条标记都没有——08-24 实测
        team_assign 26 条里只有 5 条带标记，本会话那条「你先看下目前当前项目
        最新的开发进度、剩余任务。（…」就是没标记的存量，于是 agent 早就报了
        「rxyy tools·团队功能整改」，面板上仍旧显示用户第一句话的前 24 字。
        没标记时按形状兜底，见 _looks_auto_cut。
        """
        conv = getattr(session, "conv_key", "") or ""
        if (hub.HUB.cfg.get("team_assign_auto") or {}).get(conv):
            return True
        return self._looks_auto_cut(self._team_assign(session))

    # _auto_label_on_dispatch 截断分工用的长度；改这里两处一起走
    AUTO_ASSIGN_LEN = 24

    @classmethod
    def _looks_auto_cut(cls, text):
        """这串分工的形状是不是「派活第一句被截 24 字」留下的？

        `_one_line(raw, 24)` 只在真截断时补省略号，产物必然是「24 字 + …」，
        长度恰好 25。人手填的分工不会自己敲省略号，更不会不多不少正好卡在这个
        长度上——所以这条形状判定只认存量占位，不误伤用户在面板上填的分工。
        """
        t = str(text or "")
        return t.endswith("…") and len(t) == cls.AUTO_ASSIGN_LEN + 1

    def _mark_assign_auto(self, conv_key, auto):
        m = dict(hub.HUB.cfg.get("team_assign_auto") or {})
        if auto:
            m[conv_key] = True
        else:
            m.pop(conv_key, None)
        hub.HUB.cfg["team_assign_auto"] = m

    def _drop_auto_assign(self, s):
        """agent 报了真名 → 把那条自动截来的占位分工撤掉，让位给它。

        只撤自动的；用户手填的一个字不动。撤掉而不是留着不显示，是因为转告的
        名字匹配也会拿分工去比对——留着那截 24 字，点名照样打偏。
        """
        try:
            conv = getattr(s, "conv_key", "") or ""
            if not conv:
                return
            with hub.HUB._team_project_lock:
                if not self._assign_is_auto(s):
                    return
                m = dict(hub.HUB.cfg.get("team_assign") or {})
                old = m.pop(conv, "")
                hub.HUB.cfg["team_assign"] = m
                self._mark_assign_auto(conv, False)
                _root, _project, seat = self._seat_lookup(conv)
                if seat is not None and seat.get("assign", "") == old:
                    seat["assign"] = ""
                hub.save_config(hub.HUB.cfg)
            s.rev += 1
            hub.log_event("agent 自报名字「{}」，撤掉自动占位分工「{}」".format(s.name, old))
        except Exception as e:  # noqa: BLE001
            hub.log_event("撤占位分工异常（已忽略）: {}".format(e))

    def _team_track(self, session):
        """这个 tab 在哪条业务线（子项目）上干活。

        一个工作区里常同时跑着互不相干的几摊活（同在 cursor工作流 目录下，一个做
        视频编辑、一个做直播）：光按工作区路径分组，面板上它们就是一堆人。
        与角色/分工同口径按 conv_key 记，接手沿用原 ID 时自动继承。

        这是项目内的二级筛选口径，只认人手动设的；它不能决定 TeamScope 的项目
        归属，避免「业务线」误把同一席位/明确登记的人搬去另一个项目。"""
        return str((hub.HUB.cfg.get("team_tracks") or {}).get(
            getattr(session, "conv_key", "") or "", "") or "")

    def _group_key(self, session):
        """兼容旧调用的自报项目读取；业务线请直接使用 _team_track。"""
        return str(getattr(session, "agent_project", "") or "")

    # 新版团队资源的项目键。空项目不能和名字为空的真实项目混淆，专门留一个
    # 工作区默认桶承接旧配置；任务根目录仍只由 task_root_of 决定，绝不能塞项目名。
    TEAM_DEFAULT_PROJECT = "__workspace__"
    # 心理平台历史上被写成评测/测评/评估/心健/心评，08-21 用户拍板并进「心理」。
    # 08-26：引擎和后端是同一个大项目里做的不同东西，分两组看起来像两摊活。
    _PROJECT_ALIASES = {
        "心理评测": "心理",
        "心理测评": "心理",
        "心理评估": "心理",
        "心理健康": "心理",
        "心健": "心理",
        "心评": "心理",
        "心理引擎": "心理",
        "心理后端": "心理",
        "心理平台": "心理",
        "心理健康管理平台": "心理",
        # 09-07：控制台自己的活一半叫「rxyy MCP·…」一半叫「rxyy tools·…」，被分成两组——
        # 广播互相够不着、面板两摊。rxyy tools 是产品名，rxyy MCP 是它的常驻内核，同一个项目。
        "rxyy MCP": "rxyy tools",
        "持久plus": "rxyy tools",
        "持久 plus": "rxyy tools",
        "chijiu": "rxyy tools",
        "rxyy-tools-community": "rxyy tools",
        "rxyytools": "rxyy tools",
    }

    @classmethod
    def _display_project_name(cls, project):
        text = str(project or "").strip()[:24]
        if not text:
            return ""
        return (cls._PROJECT_ALIASES.get(text)
                or cls._PROJECT_ALIASES.get(text.casefold())
                or text)

    @classmethod
    def _project_key(cls, project):
        text = cls._display_project_name(project)
        return text.casefold() if text else cls.TEAM_DEFAULT_PROJECT

    @classmethod
    def _project_storage_keys(cls, project):
        """读席位/公告/黑板时，连历史别名键一起认，避免旧「心理评测」桶丢了。"""
        canon = cls._project_key(project)
        keys = [canon]
        for alias, target in cls._PROJECT_ALIASES.items():
            if cls._project_key(target) == canon:
                folded = alias.casefold()
                if folded not in keys:
                    keys.append(folded)
        return keys

    # ---------- 事实校验：自报的话得有第二个来源对得上 ----------

    @staticmethod
    def _facts_mod():
        """team_facts 可能不在老包里；缺了就退回「只看自报」的老行为。"""
        return getattr(hub, "team_facts", None)

    def _wip_ledger(self):
        mod = self._facts_mod()
        if mod is None:
            return None
        with _FACTS_LOCK:
            if _FACTS["ledger"] is None:
                _FACTS["ledger"] = mod.WipLedger()
                try:
                    _FACTS["ledger"].load(hub.HUB.cfg.get("wip_ledger") or [])
                except Exception:  # noqa: BLE001
                    pass
            return _FACTS["ledger"]

    def _persist_wip_ledger(self, ledger, every=120.0):
        """台账要跨重启活着，否则 hub 一重启「谁最后改的」就全忘了——而孤儿 WIP
        恰恰是在重启、断线之后才需要查。落盘限频，别每次开面板都写一遍 config。"""
        if time.time() - float(_FACTS.get("saved_ts") or 0) < every:
            return
        _FACTS["saved_ts"] = time.time()
        try:
            hub.HUB.cfg["wip_ledger"] = ledger.export()
            hub.save_config(hub.HUB.cfg)
        except Exception:  # noqa: BLE001
            pass

    def _board_cards(self):
        """任务面板的卡片（只读、带缓存）。控制台与 hub 两个进程，直接读它落的盘。"""
        mod = self._facts_mod()
        if mod is None:
            return []
        ts, cards = _FACTS["cards"]
        if time.time() - ts < mod.CARD_TTL:
            return cards
        try:
            cards = mod._read_cards(mod.board_store_path())
        except Exception:  # noqa: BLE001
            cards = []
        _FACTS["cards"] = (time.time(), cards)
        return cards

    def _agent_facts(self, session, cards=None):
        """这个 agent 自报之外能查证的东西：领的卡、以及自报项目对不对得上。

        只查证、不改归属。归组一旦按可能过期的信号自动搬人，错得比现在更难查
        （rxyy 08-24：「名字晚改一步，组就假一步」——但把人按旧卡搬走同样会假）。
        所以对不上就把两边都摆在面板上，让人一眼看见，别替他判。
        """
        mod = self._facts_mod()
        if mod is None:
            return {"cards": [], "warn": ""}
        conv = str(getattr(session, "conv_key", "") or "")
        if not conv:
            return {"cards": [], "warn": ""}
        cards = self._board_cards() if cards is None else cards
        mine = mod.active_cards_of(conv, cards)
        named = str(getattr(session, "agent_project", "") or "").strip()
        warn = mod.project_conflict(
            named, mine, vocab=mod.known_projects(cards, self._project_key),
            project_key=self._project_key)
        return {"cards": mine[:3], "warn": warn}

    def _card_project_of(self, session, cards=None):
        """没自报也没人手钉时，用「它领的卡」把项目补上——这是纯捡漏，不抢自报。"""
        mod = self._facts_mod()
        if mod is None:
            return None
        mine = mod.active_cards_of(getattr(session, "conv_key", "") or "",
                                   self._board_cards() if cards is None else cards)
        for card in mine:
            name = str(card.get("project") or "").strip()[:24]
            if not name:
                continue
            key = self._project_key(name)
            if key != self.TEAM_DEFAULT_PROJECT:
                return key, self._display_project_name(name)
        return None

    def _wip_rows(self, root):
        """这个仓里没提交的改动 + 最后是谁碰的。永不阻塞面板：过期就后台重扫。"""
        mod = self._facts_mod()
        ledger = self._wip_ledger()
        if mod is None or ledger is None or not root:
            return [], {"count": 0, "unowned": 0, "text": "", "owners": []}
        key = mod.hub_norm(root)
        cell = _FACTS["wip"].get(key) or {}
        if time.time() - float(cell.get("ts") or 0) > mod.DIRTY_TTL:
            self._spawn_wip_scan(root, key)
        return cell.get("rows") or [], cell.get("summary") or {
            "count": 0, "unowned": 0, "text": "", "owners": []}

    def _wip_scan_now(self, root, key):
        """真去 git 问一趟并落进缓存。同步执行，只该由 _spawn_wip_scan 的线程调。"""
        try:
            mod = self._facts_mod()
            ledger = self._wip_ledger()
            dirty = mod.git_dirty(root)
            rows = mod.attribute_dirty(root, dirty, ledger, self._live_of_owner)
            _FACTS["wip"][key] = {"ts": time.time(), "rows": rows,
                                  "summary": mod.orphan_summary(rows)}
        except Exception:  # noqa: BLE001
            _FACTS["wip"][key] = {"ts": time.time(), "rows": [],
                                  "summary": {"count": 0, "unowned": 0, "text": "", "owners": []}}
        finally:
            with _FACTS_LOCK:
                _FACTS["scanning"].discard(key)

    def _spawn_wip_scan(self, root, key):
        with _FACTS_LOCK:
            if key in _FACTS["scanning"]:
                return
            _FACTS["scanning"].add(key)
        threading.Thread(target=self._wip_scan_now, args=(root, key),
                         daemon=True).start()

    def _live_of_owner(self, owner8):
        """锁台账里那个 8 位 cursor uuid，现在还对得上哪个活着的 tab？

        这里在后台线程跑，不许取 HUB.lock（team_state 正持着它，普通 Lock 取
        第二次就是死锁——07-31 已经用整个控制台卡死付过一次学费）。
        HUB.sessions 是 dict，读一份浅拷贝就够。
        """
        owner = str(owner8 or "")[:8]
        if not owner:
            return None
        for s in list((hub.HUB.sessions or {}).values()):
            uid = str(getattr(s, "cursor_uuid", "") or "")[:8]
            if uid and uid == owner:
                return {"tab": s.name, "conv": s.conv_key,
                        "online": bool(s.connected and not s.archived)}
        return None

    def _team_project(self, session):
        """返回 (持久化项目键, 显示名)。

        项目归属是 TeamScope 的一级边界，必须比业务线稳定：
        intake 成员（人手钉的）> 自报项目（含心理别名）> 席位项目 > 默认桶。
        席位跟人走：人已经报了「心理」，不能再被旧席位钉在「智慧云广播」。
        team_tracks 只做项目内二级筛选，绝不能反过来改项目归属。
        """
        root = hub.task_root_of(session)
        member = self._team_project_member(root, getattr(session, "conv_key", "") or "")
        if member is not None:
            return member
        text = str(getattr(session, "agent_project", "") or "").strip()[:24]
        named = self._project_key(text) if text else ""
        if named and named != self.TEAM_DEFAULT_PROJECT:
            return named, self._display_project_name(text)
        seat_root, seat_project, _seat = self._seat_lookup(
            getattr(session, "conv_key", "") or "")
        if (seat_project is not None
                and hub.norm_root(seat_root) == hub.norm_root(root)):
            bucket = self._project_bucket(root, seat_project)
            return (self._project_key(seat_project),
                    str((bucket or {}).get("name")
                        or self._display_project_name(seat_project)
                        or seat_project))
        # 前面全落空才轮到事实：它领的卡写的哪个项目，就算哪个项目。
        # 这一档只捡漏——自报/席位/人手钉但凡有一个说了话，都轮不到这里，
        # 所以不存在「拿可能过期的旧卡把人从自报的项目里搬走」。
        card = self._card_project_of(session)
        if card is not None:
            return card
        return self.TEAM_DEFAULT_PROJECT, Path(root).name if root else "未分项目"

    @classmethod
    def _scope_key(cls, root, project):
        """供前端稳定选中用；调用 API 仍传 root + project，避免反解拼接字符串。"""
        raw = "{}\x1f{}".format(hub.norm_root(root),
                                cls._project_key(project) if project is not None else "")
        return "scope-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]

    def _team_scope(self, session):
        root = hub.task_root_of(session)
        project, name = self._team_project(session)
        return {"root": root, "project": project, "name": name,
                "key": self._scope_key(root, project)}

    def _team_project_member(self, root, conv_key):
        """读取 intake 对 conversation_id 的显式项目登记，兼容早期字符串格式。"""
        entry = (hub.HUB.cfg.get("team_project_members") or {}).get(conv_key)
        if isinstance(entry, str):
            text = entry.strip()[:24]
            return ((self._project_key(text), self._display_project_name(text))
                    if text else None)
        if not isinstance(entry, dict):
            return None
        member_root = hub.norm_root(entry.get("root") or "")
        if member_root and member_root != hub.norm_root(root):
            return None
        text = str(entry.get("project") or "").strip()[:24]
        if not text:
            return None
        key = self._project_key(text)
        canon = self._display_project_name(text)
        stored = str(entry.get("name") or "").strip()[:24]
        alias_from = {a.casefold() for a in self._PROJECT_ALIASES}
        if stored and stored.casefold() not in alias_from:
            name = stored
        else:
            name = canon or stored or (
                Path(root).name if key == self.TEAM_DEFAULT_PROJECT else text)
        return key, name

    def _register_team_project_member(self, root, conv_key, project):
        """在 intake 产生新对话 ID 时把它钉到项目，后续 task_name 不得改归属。"""
        conv_key = str(conv_key or "").strip()
        if not conv_key:
            return self.TEAM_DEFAULT_PROJECT, Path(root).name if root else "未分项目"
        name = self._display_project_name(project)
        key = self._project_key(name)
        if key == self.TEAM_DEFAULT_PROJECT:
            name = Path(root).name if root else "未分项目"
        with hub.HUB._team_project_lock:
            members = dict(hub.HUB.cfg.get("team_project_members") or {})
            members[conv_key] = {"root": hub.norm_root(root), "project": key, "name": name}
            hub.HUB.cfg["team_project_members"] = members
        return key, name

    def _move_team_project_member_root(self, conv_key, old_root, new_root):
        """任务归属换根时只搬显式成员登记；席位是固定资源，不隐式挪项目。"""
        conv_key = str(conv_key or "").strip()
        if not conv_key:
            return False
        with hub.HUB._team_project_lock:
            members = dict(hub.HUB.cfg.get("team_project_members") or {})
            entry = members.get(conv_key)
            if not isinstance(entry, dict):
                return False
            member_root = hub.norm_root(entry.get("root") or "")
            if member_root and member_root != hub.norm_root(old_root):
                return False
            moved = dict(entry)
            moved["root"] = hub.norm_root(new_root)
            members[conv_key] = moved
            hub.HUB.cfg["team_project_members"] = members
            return True

    def _move_explicit_team_scope_root(self, conv_key, old_root, new_root):
        """用户确认迁根时，同步迁移成员登记和固定席位。

        普通接手只会改变 cwd，不能动这些显式团队资源。这里仅由
        set_task_root 调用：seat 从旧项目桶移到同名的新根项目桶，旧桶（尤其是
        board 黑板历史）完整保留；新桶只承接 seat，后续 scope 才会一致落到新根。
        """
        conv_key = str(conv_key or "").strip()
        old_key, new_key = hub.norm_root(old_root), hub.norm_root(new_root)
        if not conv_key or not new_key or old_key == new_key:
            return False
        changed = False
        with hub.HUB._team_project_lock:
            members = dict(hub.HUB.cfg.get("team_project_members") or {})
            entry = members.get(conv_key)
            if isinstance(entry, dict):
                member_root = hub.norm_root(entry.get("root") or "")
                if not member_root or member_root == old_key:
                    moved = dict(entry)
                    moved["root"] = new_key
                    members[conv_key] = moved
                    hub.HUB.cfg["team_project_members"] = members
                    changed = True

            projects = hub.HUB.cfg.get("team_projects") or {}
            projects = dict(projects) if isinstance(projects, dict) else {}
            old_projects = dict(projects.get(old_key) or {})
            new_projects = dict(projects.get(new_key) or {})
            projects_changed = False
            for project, bucket in list(old_projects.items()):
                bucket = bucket if isinstance(bucket, dict) else {}
                seats = list(bucket.get("seats") or [])
                moved_seats = [seat for seat in seats
                               if isinstance(seat, dict) and seat.get("id") == conv_key]
                if not moved_seats:
                    continue
                old_bucket = dict(bucket)
                old_bucket["seats"] = [seat for seat in seats
                                       if not (isinstance(seat, dict)
                                               and seat.get("id") == conv_key)]
                old_projects[project] = old_bucket

                new_bucket = new_projects.get(project)
                new_bucket = dict(new_bucket) if isinstance(new_bucket, dict) else {
                    "name": str(bucket.get("name") or project)[:24],
                    "seats": [], "board": {},
                }
                new_seats = [seat for seat in (new_bucket.get("seats") or [])
                             if not (isinstance(seat, dict) and seat.get("id") == conv_key)]
                new_bucket["seats"] = new_seats + moved_seats
                new_projects[project] = new_bucket
                projects_changed = True
            if projects_changed:
                projects[old_key] = old_projects
                projects[new_key] = new_projects
                hub.HUB.cfg["team_projects"] = projects
                changed = True

            # 兼容旧根级 seat：没有命名项目时也必须让固定席位跟随用户确认的迁根。
            legacy = hub.HUB.cfg.get("team_seats") or {}
            legacy = dict(legacy) if isinstance(legacy, dict) else {}
            old_seats = list(legacy.get(old_key) or [])
            moved_seats = [seat for seat in old_seats
                           if isinstance(seat, dict) and seat.get("id") == conv_key]
            if moved_seats:
                legacy[old_key] = [seat for seat in old_seats
                                   if not (isinstance(seat, dict)
                                           and seat.get("id") == conv_key)]
                new_seats = [seat for seat in (legacy.get(new_key) or [])
                             if not (isinstance(seat, dict) and seat.get("id") == conv_key)]
                legacy[new_key] = new_seats + moved_seats
                hub.HUB.cfg["team_seats"] = legacy
                changed = True
        return changed

    def _follow_seat_to_named_project(self, session):
        """自报项目与席位项目不一致时，把席位迁到人正在干的那条线。

        角色留下。根跟当前 task_root/cwd。只动这一个 conversation_id。
        """
        conv = str(getattr(session, "conv_key", "") or "").strip()
        named = self._project_key(getattr(session, "agent_project", "") or "")
        if not conv or not named or named == self.TEAM_DEFAULT_PROJECT:
            return False
        dest_root = hub.norm_root(
            hub.task_root_of(session) or getattr(session, "cwd", "") or "")
        if not dest_root or hub.is_runtime_ws_path(dest_root):
            return False
        seat_root, seat_project, seat = self._seat_lookup(conv)
        if not seat:
            return False
        seat_key = (self._project_key(seat_project)
                    if seat_project is not None else self.TEAM_DEFAULT_PROJECT)
        if seat_key == named and hub.norm_root(seat_root) == dest_root:
            return False
        if hub.norm_root(seat_root) != dest_root:
            self._move_explicit_team_scope_root(conv, seat_root, dest_root)
            seat_root, seat_project, seat = self._seat_lookup(conv)
            seat_key = (self._project_key(seat_project)
                        if seat_project is not None else self.TEAM_DEFAULT_PROJECT)
            if not seat:
                return False
            if seat_key == named:
                return True
        with hub.HUB._team_project_lock:
            all_projects = dict(hub.HUB.cfg.get("team_projects") or {})
            rows = dict(all_projects.get(dest_root) or {})
            moved = None
            for project, bucket in list(rows.items()):
                seats = list((bucket or {}).get("seats") or [])
                take = [x for x in seats
                        if isinstance(x, dict) and x.get("id") == conv]
                if not take:
                    continue
                moved = take[0]
                nb = dict(bucket)
                nb["seats"] = [x for x in seats
                               if not (isinstance(x, dict) and x.get("id") == conv)]
                rows[project] = nb
            if moved is None:
                return False
            dest = rows.get(named)
            dest = dict(dest) if isinstance(dest, dict) else {
                "name": self._display_project_name(
                    getattr(session, "agent_project", "") or named)[:24],
                "seats": [], "board": {},
            }
            dest["seats"] = [x for x in (dest.get("seats") or [])
                             if not (isinstance(x, dict) and x.get("id") == conv)] + [moved]
            rows[named] = dest
            all_projects[dest_root] = rows
            hub.HUB.cfg["team_projects"] = all_projects
        return True

    def _project_bucket(self, root, project, create=False, name=""):
        """新版 team_projects 的一个项目桶；不凭旧 root 数据猜测项目归属。"""
        project = self._project_key(project)
        root_key = hub.norm_root(root)
        if not create:
            all_projects = hub.HUB.cfg.get("team_projects") or {}
            rows = all_projects.get(root_key) or {}
            for key in self._project_storage_keys(project):
                row = rows.get(key)
                if isinstance(row, dict):
                    return row
            return None
        # 不能在锁外先取 all_projects 再 copy-on-write：两个 intake 同时为不同项目
        # 建首桶时，后写者会用自己的旧副本把先写者整个根节点覆盖掉。
        with hub.HUB._team_project_lock:
            all_projects = hub.HUB.cfg.get("team_projects") or {}
            all_projects = dict(all_projects) if isinstance(all_projects, dict) else {}
            rows = all_projects.get(root_key) or {}
            rows = dict(rows) if isinstance(rows, dict) else {}
            row = rows.get(project)
            if not isinstance(row, dict):
                row = {"name": str(name or project)[:24], "seats": [], "board": {}}
                rows[project] = row
                all_projects[root_key] = rows
                hub.HUB.cfg["team_projects"] = all_projects
            return row

    @staticmethod
    def _board_entry(entry):
        if isinstance(entry, str):
            return {"text": entry, "updated_at": 0}
        entry = entry if isinstance(entry, dict) else {}
        return {"text": str(entry.get("text") or ""),
                "updated_at": float(entry.get("updated_at") or 0)}

    def _seats(self, root, project=None):
        """project=None 保留旧根级 API；指定项目才读取新版隔离桶。"""
        if project is not None:
            row = self._project_bucket(root, project)
            if row is not None:
                return list(row.get("seats") or [])
            if self._project_key(project) != self.TEAM_DEFAULT_PROJECT:
                return []
        return list((hub.HUB.cfg.get("team_seats") or {}).get(hub.norm_root(root)) or [])

    def _seat_of(self, conv_key):
        """按 conversation_id 反查席位，返回 (root, project|None, seat)。"""
        for root, projects in (hub.HUB.cfg.get("team_projects") or {}).items():
            for project, bucket in (projects or {}).items():
                for s in (bucket or {}).get("seats") or []:
                    if isinstance(s, dict) and s.get("id") == conv_key:
                        return root, project, s
        for root, seats in (hub.HUB.cfg.get("team_seats") or {}).items():
            for s in seats or []:
                if isinstance(s, dict) and s.get("id") == conv_key:
                    return root, None, s
        return "", None, None

    def _seat_lookup(self, conv_key):
        """兼容旧插件/测试替身返回的 (root, seat) 查找结果。"""
        found = self._seat_of(conv_key)
        if isinstance(found, (tuple, list)) and len(found) >= 3:
            return found[0], found[1], found[2]
        if isinstance(found, (tuple, list)) and len(found) == 2:
            return found[0], None, found[1]
        return "", None, None

    def _team_board(self, root, project=None):
        """project=None 保留旧根级读取；新命名项目不继承无法判属的旧公告。"""
        if project is not None:
            row = self._project_bucket(root, project)
            if row is not None:
                return self._board_entry(row.get("board"))
            if self._project_key(project) != self.TEAM_DEFAULT_PROJECT:
                return self._board_entry({})
        return self._board_entry((hub.HUB.cfg.get("team_boards") or {}).get(hub.norm_root(root)))

    def _bulletin_entries(self, root, project=None):
        """项目黑板读取；未分项目保留旧 root 级历史的可见性。

        命名项目按项目取流（08-31 用户拍板：一个项目常横跨几个文件夹/工作区）：
        先读跨工作区共用的 _projects 桶，再把历史上嵌在各个根下的
        _scopes[root][project] 旧条目一并归进来——迁根、多工作区的旧事件都还
        看得见。未分项目仍按任务根隔离，互不相干的摊子不混流。"""
        key = hub.norm_root(root)
        if project is not None:
            named = self._project_key(project) != self.TEAM_DEFAULT_PROJECT
            scopes = (hub.HUB.bulletin or {}).get("_scopes") or {}
            pks = self._project_storage_keys(project)
            buckets = []
            if named:
                projs = (hub.HUB.bulletin or {}).get("_projects") or {}
                buckets.extend(projs.get(pk) or [] for pk in pks)
                for by_root in scopes.values():
                    buckets.extend((by_root or {}).get(pk) or [] for pk in pks)
            else:
                scoped = scopes.get(key) or {}
                buckets.extend(scoped.get(pk) or [] for pk in pks)
            rows, seen = [], set()
            for bucket in buckets:
                for x in bucket:
                    mark = (x.get("ts"), x.get("text"), x.get("from8"))
                    if mark in seen:
                        continue
                    seen.add(mark)
                    rows.append(x)
            if rows or named:
                rows.sort(key=lambda x: float(x.get("ts") or 0))
                return list(rows)
        return list((hub.HUB.bulletin or {}).get(key) or [])

    def _team_sessions(self, root, project=None, roles=None, connected_only=True):
        """同项目的会话。命名项目按项目名找人（跨工作区也算一队）；
        未分项目的默认桶仍按任务根隔离，避免两个「待命」壳被收成一队。"""
        key = hub.norm_root(root)
        project = self._project_key(project) if project is not None else None
        named = project is not None and project != self.TEAM_DEFAULT_PROJECT
        out = []
        with hub.HUB.lock:
            for sid in hub.HUB.order:
                s = hub.HUB.sessions.get(sid)
                if not s:
                    continue
                scope = self._team_scope(s)
                if named:
                    if scope["project"] != project:
                        continue
                else:
                    if hub.norm_root(scope["root"]) != key:
                        continue
                    if project is not None and scope["project"] != project:
                        continue
                if connected_only and not s.connected:
                    continue
                if roles and self._team_role(s) not in roles:
                    continue
                out.append(s)
        return out

    # 广播的两个口径：整个项目 / 只本业务线（同一工作区里常并行着互不相干的几摊活）
    RELAY_ALL_WORDS = ("团队", "全体", "all", "broadcast", "广播")
    RELAY_TRACK_WORDS = ("本组", "组内", "本业务线", "同业务线", "本子项目", "track")

    def relay_from_agent(self, sender, to, message):
        """agent → agent 转告/广播（ji 借道 action=转告/广播 进来）。

        目标解析优先级：对话ID（全串或 ≥6 位前缀，跨项目也认——ID 是明确指名；
        接手时被收起的旧壳 ID 自动顺到现任身上）› 同项目队友的 tab 名/标签/分工/
        业务线 包含匹配 › 全局同名匹配 › 已终止 tab。
        「团队/全体/all/广播」= 发给同项目除自己外的所有在线队友；
        「本组/本业务线」= 只发同一条业务线的队友。
        投递一律走排队，对方下次调 zhi 或 zt 时送达（zt 顺路取信，见
        Api.take_agent_mail——不然它埋头干活的半小时里递过去的话一个字看不见）：
        它正阻塞在 zhi 等用户回话时也不
        插队（那个位置是用户的，抢了用户那条就被挤掉）。已终止的 tab 也收——
        队列随快照持久化，等它复活/被接手后送达。找不到目标或全部投递
        失败时，把失败原因排队回发送方自己的 tab——它下次 zhi 就知道了。
        每一笔（成/败）都记进 HUB.relay_log，团队面板「队内传话」区给用户看。"""
        message = str(message or "").strip()
        to = str(to or "").strip()
        if not message:
            return {"ok": False, "error": "转告内容为空"}
        sender_label = self.session_label(sender) or sender.name
        conv8 = (sender.conv_key or "")[:8]
        low_to = to.lower()
        kind = ("广播" if low_to in self.RELAY_ALL_WORDS + self.RELAY_TRACK_WORDS
                else "转告")
        team_scope = self._team_scope(sender)

        def _log_relay(ok, to_labels, note=""):
            # 用户要「看得到 agent 之间在互相说什么」——面板数据源就是这里
            try:
                hub.HUB.relay_log.append({
                    "ts": time.time(), "hms": hub.now_hms(), "kind": kind,
                    "from8": conv8, "from_label": sender_label,
                    "to": to, "to_labels": list(to_labels or []),
                    "text": message[:240], "ok": bool(ok), "note": str(note or "")[:160],
                    "scope": team_scope["key"], "root": team_scope["root"],
                    "project": team_scope["project"],
                })
                del hub.HUB.relay_log[:-100]
                hub.HUB._save_relays()  # 传话不频繁，直接落盘（黑板同款做法）
            except Exception:
                pass

        def _notify_sender(text):
            try:
                self.queue_message(sender.id, text, [], who="控制台")
            except Exception:
                pass

        if not to:
            _notify_sender("【转告失败】没写目标。用法：ji(action=\"转告\", "
                           "category=对方tab名或对话ID, content=消息)")
            _log_relay(False, [], "没写目标")
            return {"ok": False, "error": "缺少目标"}

        root, project = team_scope["root"], team_scope["project"]
        team = [x for x in self._team_sessions(root, project=project) if x.id != sender.id]
        note = ""
        if kind == "广播":
            # 待命壳手上没活，广播给它们是纯噪音：用户看到的是一排「待命」tab 各
            # 挂着未读角标，而壳自己下次醒来还要白读一遍队友的进度汇报。点名转告
            # 不受这条限制——那是明确要找它。
            targets = [x for x in team if not hub.HUB._is_checkin_shellish(x)]
            skipped = len(team) - len(targets)
            scope = "本项目"
            track = self._team_track(sender)
            if low_to in self.RELAY_TRACK_WORDS:
                if track:
                    targets = [x for x in targets if self._team_track(x) == track]
                    scope = "业务线「{}」".format(track)
                else:
                    note = "你没设业务线，按全项目发了"
            if skipped:
                note = "；".join([x for x in (note, "跳过 {} 个待命壳".format(skipped)) if x])
            if not targets:
                # 只回一句「没有」的话，发送方和看回执的用户都会以为控制台上那排
                # 在线 tab 全该收到——其实广播按「项目分组」收窄（08-07 拍板），
                # 同工作区别的项目组不在射程内。08-12 实测：发送方在「rxyy MCP」组
                # 广播，真队友在「rxyy tools」组，回执只写了根目录名 cursor工作流，
                # 看着像整个工作区都没人。把「你在哪个组、别的组里有谁、怎么够到
                # 他们」说全，这条死胡同才有出口。
                others = {}
                try:
                    for x in self._team_sessions(root):
                        if x.id == sender.id or hub.HUB._is_checkin_shellish(x):
                            continue
                        pname = self._team_scope(x)["name"]
                        others.setdefault(pname, []).append("{}（{}）".format(
                            self.session_label(x) or x.name, (x.conv_key or "")[:8]))
                except Exception:  # noqa: BLE001
                    pass  # 指路是附赠，塌了不能连累失败回执本身
                # 抬头点明这是「你自己发的广播」的回执：09-07 rxyy 在聊天里看到一串别组
                # 名单（含心理），以为是心理组把消息广播过来了、隔离坏了
                tip = ("【广播失败 · 本 agent 自己发的广播回执，不是别人发来的消息】"
                       "{}没有其他在线 agent——广播射程是你所在的项目组"
                       "「{}」（按 task_name 的「项目·」前缀分组），不是整个工作区 {}。"
                       .format(scope, team_scope["name"], Path(root).name or root))
                if others:
                    tip += "\n同工作区其它项目组还有在线 agent（没收到，仅供点名）：\n" + "\n".join(
                        "· {}：{}".format(k, "、".join(v)) for k, v in sorted(others.items()))
                    tip += ("\n要找他们：ji(action=\"转告\", category=对话ID前8位) 点名；"
                            "或把 task_name 改成「<那个项目>·<你的功能>」并入同组后再广播。")
                _notify_sender(tip)
                hub.log_event("agent 广播失败 {}：{}没有其他在线 agent".format(
                    sender.name, scope))
                _log_relay(False, [], "{}没有其他在线 agent".format(scope))
                return {"ok": False, "error": "没有队友"}
        else:
            targets, note = self._resolve_relay_target(sender, to, team)
            if targets is None:
                hub.log_event("agent 转告失败 {} → {}（目标解析失败，已回执发送方）".format(
                    sender.name, to))
                _notify_sender(note)  # 解析失败的人话说明
                _log_relay(False, [], note)
                return {"ok": False, "error": "目标解析失败"}

        body = ("【agent 转告 · 来自 {}（{}）】\n{}\n"
                "（回话：ji(action=\"转告\", category=\"{}\", content=\"…\")）"
                .format(sender_label, conv8, message, conv8))
        sent, failed, parked = [], [], []   # parked: (label, 会话) 对
        for t in targets:
            rd = getattr(t, "recon_deadline", 0) or 0
            dead = not (getattr(t, "connected", False)
                        or (rd and time.time() <= rd))
            try:
                r = self.queue_message(t.id, body, [],
                                       who="agent·" + (sender.name or "?"), force=dead)
            except Exception as e:
                r = {"ok": False, "error": str(e)}
            label = self.session_label(t) or t.name
            if r.get("ok"):
                if dead:
                    parked.append((label, t))
                    # 寄存档案（死会话处置②）：记发送方与时刻——滞留超时好找到
                    # 发起人捎提醒（08-13 实证：日报壳终止后派活寄存 1 小时，
                    # 发送方只有一条「已寄存」，活就此断线没人知道）
                    try:
                        qid = r.get("qid")
                        with t.lock:
                            for e in t.queued:
                                if e.get("id") == qid:
                                    e["from_conv"] = sender.conv_key
                                    e["parked_ts"] = time.time()
                                    break
                    except Exception:  # noqa: BLE001
                        pass
                else:
                    sent.append(label)
            else:
                failed.append(label)
        parked_labels = [x for x, _ in parked]
        hub.log_event("agent 转告 {} → {}（成 {} 滞 {} 败 {}）".format(
            sender.name, to, len(sent), len(parked), len(failed)))
        if not sent and not parked:
            _notify_sender("【转告失败】目标「{}」都没送进去：{}".format(
                to, "、".join(failed) or "无可投递对象"))
            _log_relay(False, failed, "投递失败")
            return {"ok": False, "error": "投递失败"}
        if parked:
            # 收件的 tab 眼下通道不在：话已寄存。措辞与判活融合同一口径（08-13
            # rxyy 实测被误导：回执说「已终止」，面板蓝点却说 IDE 里还活着）——
            # 断开 ≠ 死，融合判定还活着的说「通道断开」，真凉透的才说「已终止」
            still, gone, gone_sessions = [], [], []
            for label, t in parked:
                st = (getattr(t, "live_cache", None) or {}).get("state", "")
                if st not in ("dead", "died", ""):
                    still.append(label)
                else:
                    gone.append(label)
                    gone_sessions.append((label, t))
            parts = []
            if still:
                parts.append("{} 通道断开但 IDE 里可能还活着（面板蓝点态），话已寄存"
                             "——它下次调 zhi/zt 即送达".format("、".join(still)))
            if gone:
                # 死因 + 在线候选（死会话处置②）：光说「已终止」发送方没有下一步，
                # 把死因和能立刻转投的人一并给出，别让活在寄存里断线
                withwhy = []
                for label, t in gone_sessions:
                    d = getattr(t, "death_info", None) or {}
                    why = (d.get("reason") or getattr(t, "end_reason", "") or "").strip()
                    withwhy.append("{}（{}）".format(label, why[:40]) if why else label)
                parts.append("{} 已终止，话已排队——等它复活/被接手后送达".format(
                    "、".join(withwhy)))
                cands = []
                with hub.HUB.lock:
                    for x in hub.HUB.sessions.values():
                        if (x.id == sender.id or getattr(x, "archived", False)
                                or not getattr(x, "connected", False)
                                or hub.HUB._is_checkin_shellish(x)):
                            continue
                        cands.append("{}（{}）".format(
                            self.session_label(x) or x.name, (x.conv_key or "")[:8]))
                        if len(cands) >= 3:
                            break
                if cands:
                    parts.append("急事可转投在线：{}——ji(action=\"转告\", "
                                 "category=对话ID前8位) 即达".format("、".join(cands)))
            _notify_sender("【转告寄存】{}；或在团队面板派人接手死者。".format(
                "；".join(parts)))
        if note:
            # 换了身份还照旧 ID 发的，得让发送方知道现在该叫谁——否则它会一直用旧 ID
            _notify_sender("【转告已代投】{}".format(note))
        _log_relay(True, sent + parked_labels, "；".join(
            [x for x in (note,
                         ("寄存给通道不在的 " + "、".join(parked_labels)) if parked_labels else "") if x]))
        return {"ok": True, "sent": sent + parked_labels, "failed": failed,
                "parked": parked_labels, "note": note}

    def _relay_self_target_note(self, sender, to, kind):
        """转告目标解析出来是发送方自己时的真话回执。

        08-26 事故：73004179 被误当空壳并进 b4eff2ee 后发现撞车，发转告想警告
        「b4eff2ee」——可它的调用已被别名路由成 b4eff2ee 本人，四步查找全带
        s.id != sender.id，回执落到兜底「找不到、没有队友」，把它往「那个 tab
        不存在」上带，撞车警告就此蒸发。目标是自己时必须直说，并把「你们可能
        已被归并」这条唯一有用的线索给出去。"""
        return ("【转告没投】「{}」是你的{}——解析出来就是你自己所在的会话"
                "（tab「{}」，conv {}）。不用给自己转告；若你以为「{}」是另一个"
                " agent，说明你们两个的消息已被并进同一个 tab（控制台归并事故），"
                "别再按这个 ID/名字找了，直接把这一情况上报给用户，让用户拆分。"
                .format(to, kind, self.session_label(sender) or sender.name,
                        (sender.conv_key or "")[:8], to))

    def _resolve_relay_target(self, sender, to, team):
        """把「对方是谁」解析成 (会话列表, 附言)；解析不了时返回 (None, 人话说明)。

        在线的没对上时也认【已终止但还在列表里】的 tab（用户在列表里看得见它，
        递话就该能进——排队等接手者收）；壳被收走的老名字则按墓碑指路到现任；
        解析到发送方自己时说真话（见 _relay_self_target_note），不报「找不到」。"""
        low = to.lower()
        # ① 对话 ID：全串或 ≥6 位前缀（明确指名，跨项目也认，死活都认）
        if re.fullmatch(r"[0-9a-f]{6,32}", low):
            # conv_key 全局唯一：目标撞上发送方自己就不可能是别人
            if (sender.conv_key or "").lower().startswith(low):
                return (None, self._relay_self_target_note(sender, to, "当前对话 ID"))
            with hub.HUB.lock:
                hits = [s for s in hub.HUB.sessions.values()
                        if s.id != sender.id and (s.conv_key or "").lower().startswith(low)]
            if len(hits) == 1:
                return self._follow_handoff(hits[0], to)
            if len(hits) > 1:
                return (None, "【转告失败】ID 前缀「{}」对上了 {} 个会话，再多给几位：{}"
                        .format(to, len(hits),
                                "、".join((x.conv_key or "")[:8] for x in hits[:5])))
            # ①b 接手别名（退休 ID → 现任，落盘跨重启）：这张表是「旧 ID 现在归谁」
            # 的权威答案，id_history 只是它在会话身上的影子。08-26 拆分手术：
            # 73004179 被误归并进 b4eff2ee（影子留在那边的 id_history 里），活改派
            # 到 9e528993 后把别名改指现任——路由必须跟表走，不能跟影子走，
            # 否则队友按旧 ID 递话仍落进误归并的 tab，串台复发。
            alias_succs = {(v or "").strip().lower()
                           for k, v in (hub.HUB.takeover_aliases or {}).items()
                           if str(k or "").strip().lower().startswith(low)}
            alias_succs.discard("")
            if len(alias_succs) == 1:
                succ_conv = next(iter(alias_succs))
                if (sender.conv_key or "").lower() == succ_conv:
                    return (None, self._relay_self_target_note(sender, to, "曾用 ID"))
                with hub.HUB.lock:
                    tgt = [s for s in hub.HUB.sessions.values()
                           if (s.conv_key or "").lower() == succ_conv]
                if len(tgt) == 1:
                    return ([tgt[0]], "「{}」那轮对话已并入 {}（{}），已代为转投；"
                                      "以后直接用新 ID。".format(
                                          to, (tgt[0].conv_key or "")[:8],
                                          self.session_label(tgt[0]) or tgt[0].name))
            # 接手落地后那个报到用的临时 ID 已随空壳被收起，但队友手上多半正是它
            # （面板、传话记录里露过面）。现任把它记在「曾用ID」里，顺着找过去。
            with hub.HUB.lock:
                merged = [s for s in hub.HUB.sessions.values()
                          if s.id != sender.id
                          and any((x or "").lower().startswith(low)
                                  for x in (getattr(s, "id_history", None) or []))]
            if len(merged) == 1:
                t = merged[0]
                return ([t], "「{}」那轮对话已被接手，现在的 ID 是 {}（{}），"
                             "已代为转投；以后直接用新 ID。".format(
                                 to, (t.conv_key or "")[:8],
                                 self.session_label(t) or t.name))
            # 别人都不认识这个 ID、它却在发送方自己的曾用 ID 里：并进来的壳 ID
            if any((x or "").lower().startswith(low)
                   for x in (getattr(sender, "id_history", None) or [])):
                return (None, self._relay_self_target_note(sender, to, "曾用 ID"))
        # ② 名字/标签/分工/业务线/曾用名 完全相等或前缀：先同项目队友，再全局在线，
        # 最后已终止的 tab（曾用名：分工一改标签就变，别人还按旧名字发）
        def _match(pool):
            """返回 (命中, 只沾了几个字的弱命中)。

            名字/标签/分工/业务线/曾用名，一律要求「完全相等或前缀」才算命中。
            派活自动命名之后这几个字段常是一整句话（同一句话会同时落进 tab 名、
            分工，改名时还会连带项目前缀进曾用名），包含匹配很容易「只对上一个
            就投了」——发给「团队面板」命中「继续完善团队面板功能，我让这个会话
            的agent去…」、发给「雷神」命中「不能退出雷神哈，我cursor在使用雷神。」
            （08-04 用户报乱转发）。只沾了几个字的算弱命中：不替发送方拿主意，
            把候选列回去让它用全名或对话 ID 重发。"""
            out, loose = [], []
            for s in pool:
                fields = [f.lower() for f in
                          [s.name or "", self.session_label(s) or "",
                           self._team_assign(s) or "", self._team_track(s) or ""]
                          + list(getattr(s, "name_history", None) or []) if f]
                if any(f == low or f.startswith(low) for f in fields):
                    out.append(s)
                elif any(low in f for f in fields):
                    loose.append(s)
            return out, loose
        hits, loose = _match(team)
        if not hits:
            with hub.HUB.lock:
                everyone = [s for s in hub.HUB.sessions.values()
                            if s.id != sender.id and s.connected]
            hits, l2 = _match(everyone)
            loose = loose or l2
        dead_hit = False
        if not hits:
            # ③ 已终止但 tab 还在：照收，排队等复活/接手（07-31 用户实测
            # 「cursor工作流1 明明在列表里却说找不到」的修法）
            with hub.HUB.lock:
                gone = [s for s in hub.HUB.sessions.values()
                        if s.id != sender.id and not s.connected]
            hits, l3 = _match(gone)
            loose = loose or l3
            dead_hit = bool(hits)
        if len(hits) == 1:
            return self._follow_handoff(hits[0], to)
        if not hits and loose:
            return (None, "【转告没投】没有正好叫「{}」的人；名字/分工里沾着这几个字的有：{}。"
                          "这些字段现在常是一整句话，只沾几个字我不替你拿主意——"
                          "用完整名字或对话 ID 前 8 位再发一次。".format(
                              to, "、".join(self.session_label(x) or x.name
                                            for x in loose[:5])))
        if not hits:
            # ④ 壳被收走的老名字/老 ID：墓碑指路到现任，别让发送方干瞪眼
            tomb = None
            for name, info in (hub.HUB.name_tombstones or {}).items():
                if low in name.lower() or (info.get("conv") or "").lower().startswith(low):
                    tomb = (name, info)
                    break
            if tomb and (tomb[1].get("succ_label") or tomb[1].get("succ8")):
                return (None, "【转告失败】「{}」的壳已被收起，那个窗口现在的会话是"
                              "「{}（{}）」——用它的名字或 ID 再发一次。".format(
                                  tomb[0], tomb[1].get("succ_label") or "?",
                                  tomb[1].get("succ8") or "?"))
            # 谁都对不上、却正好是发送方自己的名字/标签/分工/曾用名：说真话，
            # 别报「找不到」（08-26：被归并的 agent 按共处一 tab 的名字找队友）
            own = [f.lower() for f in
                   [sender.name or "", self.session_label(sender) or "",
                    self._team_assign(sender) or "", self._team_track(sender) or ""]
                   + list(getattr(sender, "name_history", None) or []) if f]
            if any(f == low or f.startswith(low) for f in own):
                return (None, self._relay_self_target_note(sender, to, "名字"))
            names = "、".join(self.session_label(x) or x.name for x in team[:8])
            return (None, "【转告失败】找不到「{}」。同项目在线的有：{}；"
                          "也可以用对话 ID 前 8 位指名（已终止的 tab 也收，会排队等接手）。"
                    .format(to, names or "（没有队友）"))
        return (None, "【转告失败】「{}」对上了 {} 个{}：{}。换个更具体的名字或用对话 ID。"
                .format(to, len(hits), "（都已终止）" if dead_hit else "",
                        "、".join(self.session_label(x) or x.name for x in hits[:5])))

    def _follow_handoff(self, target, asked):
        """目标若是「活已交出去、还没被收起的待命空壳」，把话顺到接手者身上。

        空壳里有排队消息或用户又派了别的活时它不会被自动收起（见
        _reap_handed_off_shell）：这时按旧 ID 投递会「成功」进一个再也没人看的
        tab，话就这么丢了——比直接报错更坑。

        有真实历史的 tab 本来不跟（它多半只是被排了一条接手提示词，人还在原地）；
        但被 _handover_out 归档过的那种是已经拿身份自校准坐实了「Cursor 对话改去
        驱动 succ」，人确实走了，同样得跟。"""
        succ_id = str(getattr(target, "handed_off_to", "") or "")
        if not succ_id or succ_id == getattr(target, "conv_key", ""):
            return ([target], "")
        with hub.HUB.lock:
            succ = next((x for x in hub.HUB.sessions.values()
                         if x.conv_key == succ_id and x.id != target.id), None)
        if succ is None or not (hub.HUB._is_checkin_shellish(target)
                                or getattr(target, "archived", False)):
            return ([target], "")
        return ([succ], "「{}」那个 tab 已把活交给 {}（{}）接手，已代为转投。".format(
            asked, self.session_label(succ) or succ.name, succ_id[:8]))

    BOARD_KINDS = ("部署", "大改", "提交", "卡住", "事故", "收工")

    def board_from_agent(self, sender, kind, text):
        """agent 往团队黑板写一条重大情况（ji action=黑板 借道进来）。

        转告是「点对点、对方下次 zhi 才收到」；黑板是「落盘广而告之」：
        全队随时主动来读（ji 不带 content），团队面板对用户可见。
        部署/事故/大改这三类会顺带提醒同一条业务线的在线队友（见 _board_notify），
        别的类别只落盘不打断；要立刻惊动业务线之外的谁，写完黑板再用 转告/广播 点名。"""
        text = str(text or "").strip()
        if not text:
            return {"ok": False, "error": "内容为空"}
        kind = str(kind or "").strip()
        if kind not in self.BOARD_KINDS:
            kind = "大改" if kind in ("", "context") else kind[:6]
        scope = self._team_scope(sender)
        root, project = scope["root"], scope["project"]
        key = hub.norm_root(root)
        entry = {
            "ts": time.time(), "day": time.strftime("%m-%d"), "hms": hub.now_hms(),
            "from8": (sender.conv_key or "")[:8],
            "from_label": self.session_label(sender) or sender.name,
            "kind": kind, "text": text[:400],
            # 命名项目共用一条跨工作区事件流后，靠它说清这条是在哪个仓写的
            "root": key,
        }
        # 未分项目留在旧根级桶（工作区就是它唯一说得清的边界）。命名项目 08-31 起
        # 写进项目优先桶 _projects：一个项目常横跨几个文件夹/工作区（都按绝对路径
        # 干活），旧结构嵌在 _scopes[root][project] 下，同项目换个工作区就互相看不
        # 见——提醒明明按项目跨根找人，被提醒的来读黑板却读不到那条。
        if project == self.TEAM_DEFAULT_PROJECT:
            lst = hub.HUB.bulletin.setdefault(key, [])
        else:
            lst = hub.HUB.bulletin.setdefault("_projects", {}).setdefault(project, [])
        lst.append(entry)
        del lst[:-100]
        hub.HUB._save_bulletin()
        hub.log_event("黑板[{}] {}：{}".format(kind, entry["from_label"], text[:60]))
        # 工作流编排：收工/提交/卡住/事故 同时也是步骤信号——匹配到活跃工作流
        # 就自动推进下一步或标卡（引擎自带防误触：认「工作流#id」标记或被派人本人）
        try:
            if hub.WORKFLOW is not None:
                note = hub.WORKFLOW.on_board_post(sender.conv_key or "", root, kind, text)
                if note:
                    hub.log_event(note)
        except Exception as e:  # noqa: BLE001
            hub.log_event("工作流信号处理异常: {}".format(e))
        # 任务面板：「收工」也是一句「我名下那些卡可以送验收了」。异步投递，
        # 重复收工会被控制台按幂等拒掉，这里不管
        if kind == "收工":
            try:
                import board_hooks
                board_hooks.finish_session(sender.conv_key or "", text[:200])
            except Exception as e:  # noqa: BLE001
                hub.log_event("看板钩子（收工）异常: {}".format(e))
        return {"ok": True, "kind": kind,
                "pushed": self._board_notify(sender, kind, entry, root, project)}

    # 会顺带提醒队友的三类：不知会一声就容易互相踩到的事
    BOARD_URGENT_KINDS = ("部署", "事故", "大改")
    # 其中可降噪的两类：同一个人短时间连发（08-12 实测：一轮优化连发 6 条
    # 「部署」，队友每次 zt 取信都被塞一条提醒）窗口内只落盘，第一条已经把事
    # 说了。「事故」不降噪：连发事故往往是事态在升级，宁吵勿漏。
    BOARD_QUIET_KINDS = ("部署", "大改")
    BOARD_NOTIFY_COOLDOWN_SECS = 1800

    def _board_notify(self, sender, kind, entry, root, project=None):
        """部署/事故/大改：排队提醒同一条业务线的在线队友。

        08-03 曾把自动提醒整个去掉，因为分组键是工作区根目录，而同一工作区常挂着
        互不相干的项目（实测 ctest 那次事故推给了全部 agent）。现在面板有业务线了，
        按业务线收窄就够——没设业务线的人只提醒同样没设的，别去打断已经分好线的队伍。
        走排队且不占用户的回复位：这是机器替黑板递的话，不是用户在说话。
        """
        if kind not in self.BOARD_URGENT_KINDS:
            return []
        seen = getattr(hub.HUB, "_board_notify_ts", None)
        if seen is None:
            seen = hub.HUB._board_notify_ts = {}
        key = (hub.norm_root(root), str(project or ""),
               getattr(sender, "conv_key", "") or "", kind)
        now = time.time()
        if (kind in self.BOARD_QUIET_KINDS
                and now - float(seen.get(key, 0) or 0) < self.BOARD_NOTIFY_COOLDOWN_SECS):
            hub.log_event("黑板[{}] 同人同类冷却中，本条只落盘不提醒（{}）".format(
                kind, entry["from_label"]))
            return []
        track = self._team_track(sender)
        body = "【黑板·{}】{}\n（来自 {}·{}；全部条目见团队面板，或 ji(action=\"黑板\") 自取）".format(
            kind, entry["text"], entry["from_label"], entry["from8"])
        pushed = []
        for t in self._team_sessions(root, project=project):
            if t.id == sender.id or self._team_track(t) != track:
                continue
            if hub.HUB._is_checkin_shellish(t):
                continue  # 待命壳还没接活，黑板提醒对它没有意义，只会积未读
            try:
                if self.queue_message(t.id, body, [], who="黑板").get("ok"):
                    pushed.append(self.session_label(t) or t.name)
            except Exception:  # noqa: BLE001
                pass  # 提醒是附赠，塌了不能连累黑板本身
        if pushed:
            # 真推给过人才消耗冷却窗：第一条发出时恰好没人在线的话，
            # 窗口留给下一条，别让空推吃掉唯一一次提醒机会
            seen[key] = now
            hub.log_event("黑板[{}] 已提醒业务线「{}」{} 人：{}".format(
                kind, track or "未分", len(pushed), "、".join(pushed[:5])))
        return pushed

    @staticmethod
    def _age_text(secs):
        if secs is None:
            return "未知"
        secs = int(max(0, secs))
        if secs < 60:
            return "{}秒前".format(secs)
        if secs < 3600:
            return "{}分钟前".format(secs // 60)
        # 「21时13分前」读起来像时刻 21:13（用户实测看不懂）；超过一小时后
        # 分钟粒度也没有意义，直接说「21小时前」「3天前」
        if secs < 86400:
            return "{}小时前".format(secs // 3600)
        return "{}天前".format(secs // 86400)

    def _agent_liveness(self, s, now):
        """判活多信号融合裁决（第四刀已抽 session_core，此处委托）。"""
        if hub.runtime_adapter.is_native(s):
            return hub.runtime_adapter.native_liveness(s, now)
        return hub.session_core.agent_liveness(self, s, now)

    def _auto_root_by_locks(self, s, locks, now, persist=True):
        """在干哪个项目的活就归到哪个项目（07-31 用户拍板要自动）。

        文件锁是「它正在改谁的文件」的硬证据：这个 agent 的活跃锁全部落在
        另一个项目的看板上 → 把任务归属自动搬过去（跨工作区标记照亮）。
        只在证据唯一时动手；5 分钟内不反复搬，防止边界抖动。"""
        if not s.connected:
            return False
        uid8 = str(getattr(s, "cursor_uuid", "") or "")[:8]
        if not uid8:
            return False
        if now - float(getattr(s, "_auto_root_ts", 0) or 0) < 300:
            return False
        held = set()
        orig = {}
        for root_key, items in locks.items():
            for it in items:
                if it.get("owner") == uid8 and not it.get("stale"):
                    held.add(root_key)
                    orig[root_key] = it.get("root") or root_key
        if len(held) != 1:
            return False
        target = held.pop()
        if target == hub.norm_root(hub.task_root_of(s)):
            return False
        # 人坐在 cwd、活钉在 task_root：自己工作区里改两个文件太正常，
        # 不能凭文件锁把人从被派的项目拽走。这里看路径分叉本身，不看
        # cross_workspace（那要钉住才亮黄条，08-21 未钉住的接手也会分叉）。
        sitting = hub.norm_root(getattr(s, "cwd", "") or "")
        if sitting and hub.norm_root(hub.task_root_of(s)) != sitting and target == sitting:
            return False
        old = hub.task_root_of(s)
        s.task_root = orig[target]
        if self._move_team_project_member_root(s.conv_key, old, s.task_root):
            hub.save_config(hub.HUB.cfg)
        s._auto_root_ts = now
        hub.log_event("按文件锁自动归组 tab={} {} → {}".format(
            s.name, Path(old).name if old else "?", Path(s.task_root).name))
        hub.HUB.add_message(s, {
            "role": "sys", "ts": hub.now_hms(),
            "html": "它正占着 <b>{}</b> 的文件锁 → 任务归属已自动搬过去（团队面板可手动改回）"
                .format(html_mod.escape(Path(s.task_root).name)),
        })
        # team_state 调用本方法时正持有 HUB.lock，延迟到释放后落盘避免死锁。
        if persist:
            hub.HUB.save_state()
        return True

    def team_state(self):
        """团队视图数据：按项目聚合会话、角色、在干嘛、占着哪些文件。
        已终止的会话不占面板行（用户 07-31：死了的不用显示），折成 ended 计数。"""
        locks = {}
        lock_items = []
        try:
            lock_items = (self.agentboard() or {}).get("items") or []
            for item in lock_items:
                locks.setdefault(hub.norm_root(item["root"]), []).append(item)
        except Exception:
            pass
        # 锁只在人干活那几分钟存在，stop 钩子一收就没了。每看见一次就记一笔，
        # 等发现工作树脏时才有「最后是谁碰的」可查（见 team_facts.WipLedger）。
        try:
            ledger = self._wip_ledger()
            if ledger is not None and ledger.observe(lock_items, now=time.time()):
                self._persist_wip_ledger(ledger)
        except Exception:  # noqa: BLE001
            pass
        cards = self._board_cards()
        now = time.time()
        groups = {}
        root_changed = False
        with hub.HUB.lock:
            for sid in hub.HUB.order:
                s = hub.HUB.sessions.get(sid)
                if not s:
                    continue
                try:
                    root_changed = self._auto_root_by_locks(
                        s, locks, now, persist=False) or root_changed
                except Exception:
                    pass
                scope = self._team_scope(s)
                key = scope["key"]
                g = groups.setdefault(key, {"root": scope["root"], "project": scope["project"],
                                            "name": scope["name"], "key": key, "agents": [],
                                            "ended": 0, "offline": 0})
                hb_alive = s.connected and (now - getattr(s, "last_heartbeat", 0) < 15)
                uid8 = str(getattr(s, "cursor_uuid", "") or "")[:8]
                live = self._agent_liveness(s, now)
                if not s.connected and live["state"] in ("dead", "died"):
                    g["ended"] += 1
                    continue
                # 通道断了的也不占面板行（用户 08-04：「断掉的挂掉的会话就不要显示了」）。
                # 只放过「重连中」且 IDE 还热的：hub 刚重启那几秒活人还在。
                # 「重连中·多半已终止」是宽限期里的死壳，放进来历史项目会全员假活。
                if not s.connected:
                    hopeful = (live["state"] == "recon"
                               and "已终止" not in str(live.get("label") or ""))
                    if not hopeful:
                        if live["state"] == "recon" or live["state"] in ("dead", "died"):
                            g["ended"] += 1
                        else:
                            g["offline"] += 1
                        continue
                model = getattr(s, "model_info", None) or {}
                try:
                    facts = self._agent_facts(s, cards)
                except Exception:  # noqa: BLE001
                    facts = {"cards": [], "warn": ""}
                g["agents"].append({
                    "id": s.id,
                    "name": s.name,
                    "label": self.session_label(s),
                    "model": hub.sidebar_model_chip(model),
                    "model_full": model.get("model", ""),
                    "model_effort": model.get("effort", ""),
                    "conv": s.conv_key,
                    # 曾用 ID（接手时并进来的空壳 ID）：队友按旧 ID 找人时面板得说清
                    "id_history": list(getattr(s, "id_history", None) or [])[-4:],
                    "role": self._team_role(s),
                    "assign": self._team_assign(s),
                    # TeamScope 已按 task_root + project 顶层分组；这里的业务线只做
                    # 项目内二级节点/广播筛选，不能再借 task_name 或 team_tracks 改 scope。
                    "track": self._team_track(s),
                    # 单列 agent 自报项目用于展示与旧数据兼容，不参与 TeamScope 判定。
                    "project": str(getattr(s, "agent_project", "") or ""),
                    # 面板上那行「这人此刻在干嘛」，zt 没上报时也有话说
                    "desc": self._agent_desc(s, hb_alive),
                    "seat": (self._seat_lookup(s.conv_key)[2] or {}).get("name", ""),
                    "connected": bool(s.connected),
                    "pending": s.pending is not None,
                    "queued": len(s.queued),
                    "live": live["state"],
                    "live_label": live["label"],
                    "live_sure": live["sure"],
                    "evidence": live["evidence"],
                    "death": live.get("death"),
                    "status": getattr(s, "agent_status", "") if hb_alive else "",
                    "activity": getattr(s, "agent_activity", "") if hb_alive else "",
                    "wtag": hub.HUB._window_tag(s.pid, s.cwd, getattr(s, "transcript_path", None)),
                    "uid8": uid8,
                    # 跨工作区接手：这个 agent 人在别的工作区，够不着本项目的代码
                    "crossed": hub.cross_workspace(s),
                    "task_root": hub.task_root_of(s),
                    "task_root_locked": bool(getattr(s, "task_root_locked", False)),
                    "affiliation": hub.affiliation_of(s),
                    "agent_cwd": s.cwd or "",
                    "agent_project": Path(s.cwd).name if s.cwd else "",
                    # 工作区以 cwd 为准（窗口实际开在哪），不要用 task_root 冒充。
                    # 08-21 实测：心理两席窗口在 mh_admin_suite，归属却停在 cursor工作流。
                    "workspace": Path(s.cwd).name if s.cwd else "",
                    "workspace_path": s.cwd or "",
                    # 事实校验：它领的卡（最多 3 张）与「自报项目对不上卡」的提示。
                    # 分组仍按自报走，这里只把证据摆出来——08-24 那次误判里，
                    # 自报 rxyy tools 的那个 tab 领的两张卡都在视频快编。
                    "cards": facts["cards"],
                    "fact_warn": facts["warn"],
                    "files": [x["file"] for x in locks.get(hub.norm_root(scope["root"]), [])
                              if x["owner"] == uid8 and not x["stale"]],
                })
        # 自动迁根必须等 HUB.lock 释放后再保存；save_state 内部会重新获取同一把锁。
        if root_changed:
            hub.HUB.save_state()
        if hub.scrub_runtime_team_cfg(hub.HUB.cfg, hub.HUB.bulletin):
            hub.save_config(hub.HUB.cfg)
        # 祖先归并：task_root 落在另一个组根的子目录里（历史脏数据、或在子目录开的
        # 窗口）时并入祖先组——否则 dist\rxyy-tools-community\_internal\rxyy MCP 这类路径会
        # 凭空成组，团队面板看着像多了一个项目
        for k in sorted(groups, key=lambda x: len(groups[x]["root"]), reverse=True):
            for anc in sorted(groups, key=lambda x: len(groups[x]["root"])):
                if (anc != k and anc in groups
                        and groups[k]["project"] == groups[anc]["project"]
                        and hub.norm_root(groups[k]["root"]).startswith(
                            hub.norm_root(groups[anc]["root"]) + os.sep)):
                    groups[anc]["agents"].extend(groups[k]["agents"])
                    groups[anc]["ended"] += groups[k].get("ended", 0)
                    groups[anc]["offline"] += groups[k].get("offline", 0)
                    del groups[k]
                    break
        # 归并之后才丢运行时组：打包目录能剥回它所在的仓库（上面那一步已把人并
        # 过去），先丢就等于把那个 tab 从面板上抹掉；常驻区在面板里没有祖先，
        # 归并不到谁，正好在这里落地成「不成组」。
        groups = {key: group for key, group in groups.items()
                  if not hub.is_runtime_ws_path(group.get("root"))}
        # 席位可能一个人都没坐（预先摆好等人来接），也要在面板上看得见
        # 旧根级席位仍作为默认项目展示；新版的项目桶即使暂时没人也要露出来。
        legacy_roots = set((hub.HUB.cfg.get("team_seats") or {}).keys())
        legacy_roots.update((hub.HUB.cfg.get("team_boards") or {}).keys())
        legacy_roots.update(k for k, v in (hub.HUB.bulletin or {}).items()
                            if not str(k).startswith("_") and isinstance(v, list))
        for root in legacy_roots:
            if hub.is_runtime_ws_path(root):
                continue
            key = self._scope_key(root, self.TEAM_DEFAULT_PROJECT)
            groups.setdefault(key, {"root": root, "project": self.TEAM_DEFAULT_PROJECT,
                                    "name": Path(root).name or "未分项目", "key": key,
                                    "agents": [], "ended": 0, "offline": 0})
        for root, scoped in (hub.HUB.cfg.get("team_projects") or {}).items():
            if hub.is_runtime_ws_path(root):
                continue
            for project, bucket in (scoped or {}).items():
                pkey = self._project_key(project)
                key = self._scope_key(root, pkey)
                groups.setdefault(key, {"root": root, "project": pkey,
                                        "name": str((bucket or {}).get("name")
                                                    or self._display_project_name(project)
                                                    or project),
                                        "key": key, "agents": [], "ended": 0, "offline": 0})
        projects = []
        for key, g in groups.items():
            owners = {a["uid8"] for a in g["agents"] if a.get("uid8")}
            items = [x for x in locks.get(hub.norm_root(g["root"]), [])
                     if x.get("owner") in owners]
            board = self._team_board(g["root"], g["project"])
            try:
                wip_rows, wip_sum = self._wip_rows(g["root"])
            except Exception:  # noqa: BLE001
                wip_rows, wip_sum = [], {"count": 0, "unowned": 0, "text": "", "owners": []}
            occupied = {a["conv"] for a in g["agents"]}
            seats = [{**x, "occupied": x.get("id") in occupied}
                     for x in self._seats(g["root"], g["project"])]
            projects.append({
                "key": g["key"],
                "project": g["project"],
                "seats": seats,
                "root": g["root"],
                "name": g["name"] or (Path(g["root"]).name if g["root"] else "（未知工作区）"),
                "workspace_name": Path(g["root"]).name if g["root"] else "（未知工作区）",
                "agents": g["agents"],
                # 本项目下有哪几条业务线（在跑的 + 席位预留的）：面板左侧的二级节点，
                # 也是卡片上那个下拉的候选
                "tracks": self._tracks_of(g["agents"], seats),
                # 已终止的不占面板行，只给个数（记录在会话列表「已结束」组里可接手）
                "ended": g.get("ended", 0),
                # 通道断了但 Cursor 那边还在的：同样不占行，单独计数——跟「已终止」
                # 不是一回事，它们下次调 zhi 就会自己回来
                "offline": g.get("offline", 0),
                # online = 通道连着；alive = 真有证据在跑（等你回复/在输出/卡住）。
                # 看板上要用 alive，否则收工的 tab 会一直冒充「在线」
                "online": sum(1 for a in g["agents"] if a["connected"]),
                # recent（刚才还在动）也算：Cursor 那边几分钟前刚写过盘，人明明在，
                # 只是这一刻没在输出——不算进去就会出现「明明两个在跑却显示 1」
                "alive": sum(1 for a in g["agents"]
                             if a["live"] in ("waiting", "working", "stalled", "recent")),
                "locks": items,
                "waiting": sum(int(x.get("waiting") or 0) for x in items),
                "board": board["text"],
                "board_updated": board["updated_at"],
                # 团队黑板动态条目（公告板是静态上下文，黑板是事件流，两回事）
                "bulletin": self._bulletin_entries(g["root"], g["project"])[-20:],
                "relays": self._relays_for_row({g["key"]}, g["project"]),
                # 工作树里没提交的改动 + 最后编辑它的会话还在不在。
                # 没主的那些就是孤儿 WIP——08-24 那批在生产上跑了三天没人认。
                "wip": wip_rows[:40],
                "wip_orphans": wip_sum,
            })
        projects = [self._finish_project_row(p) for p in projects]
        # 命名项目按项目名合成一队：同一「心理」人在不同工作区也是一支队伍。
        # 未分项目的默认桶仍按工作区根分开。
        projects = self._merge_named_project_rows(projects)
        projects = [self._finish_project_row(p) for p in projects]
        # 无人、无结束计数、无席位的空壳组不进侧栏（换装后最爱冒出来）
        projects = [p for p in projects if not self._is_empty_history_shell(p)]
        # 真在跑的人多的项目排前面：一个项目只有一个 agent 时其实没什么协作可看
        projects.sort(key=lambda p: (-p["alive"], -p["online"], -len(p["agents"]),
                                     int(bool(p.get("history"))), p["name"]))
        live_rows = [p for p in projects if not p.get("history")]
        hist_rows = [p for p in projects if p.get("history")]
        hist_rows.sort(key=lambda p: (-int(p.get("ended") or 0), p.get("name") or ""))
        history_total = len(hist_rows)
        projects = live_rows + hist_rows[:8]
        legacy_relays = [dict(x) for x in (hub.HUB.relay_log or [])[-100:]
                         if not str(x.get("scope") or "").strip()][-40:]
        return {"ok": True, "projects": projects,
                "history_total": history_total,
                # 队内传话近况（新的在后，前端倒序展示）：谁转告了谁、成没成
                "relays": [dict(x) for x in (hub.HUB.relay_log or [])[-40:]],
                # 升级前的记录无法可靠推断属于哪个命名项目，单独展示，绝不混入项目流。
                "legacy_relays": legacy_relays,
                # 工作流编排链（桌面/手机团队面板共用这一份）
                "workflows": (hub.WORKFLOW.snapshot() if hub.WORKFLOW is not None else []),
                "roles": [{"key": k, "label": v} for k, v in self.TEAM_ROLE_LABELS.items()]}

    def _relays_for_row(self, keys, project):
        """队内传话：认 scope key，命名项目再按项目名收一遍跨工作区的旧记录。"""
        keys = {str(k) for k in (keys or ()) if k}
        proj = self._project_key(project) if project is not None else ""
        named = proj and proj != self.TEAM_DEFAULT_PROJECT
        out = []
        for x in (hub.HUB.relay_log or [])[-100:]:
            if x.get("scope") in keys:
                out.append(dict(x))
            elif named and self._project_key(x.get("project")) == proj:
                out.append(dict(x))
        return out[-40:]

    @staticmethod
    def _workspaces_of(agents):
        """当前在跑的人实际开着的工作区（cwd），不是任务归属根。"""
        counts, order = {}, []
        for a in agents or []:
            path = str(a.get("workspace_path") or a.get("agent_cwd") or "").strip()
            name = str(a.get("workspace") or "").strip()
            if not name and path:
                name = Path(path).name
            if not path and not name:
                continue
            key = hub.norm_root(path) if path else name.casefold()
            if key not in counts:
                counts[key] = {"name": name or "（未知工作区）", "path": path, "count": 0}
                order.append(key)
            counts[key]["count"] += 1
        return [counts[k] for k in order]

    @staticmethod
    def _workspace_label(workspaces, fallback=""):
        if not workspaces:
            return fallback or "（未知工作区）"
        if len(workspaces) == 1:
            return workspaces[0]["name"]
        return "{} 个工作区".format(len(workspaces))

    @staticmethod
    def _is_history_project(p):
        """无人、无席位、无公告 = 死会话残留组，侧栏折进「历史项目」。"""
        if p.get("agents"):
            return False
        if p.get("seats"):
            return False
        if str(p.get("board") or "").strip():
            return False
        return True

    @staticmethod
    def _is_empty_history_shell(p):
        """历史空壳：折进历史还没一条已结束，侧栏留着只会越积越长。"""
        if not p.get("history"):
            return False
        if int(p.get("ended") or 0) > 0:
            return False
        if p.get("seats") or p.get("agents"):
            return False
        if str(p.get("board") or "").strip():
            return False
        return True

    def _finish_project_row(self, p):
        p["workspaces"] = self._workspaces_of(p.get("agents"))
        p["workspace_name"] = self._workspace_label(
            p["workspaces"], p.get("workspace_name") or "")
        p["history"] = self._is_history_project(p)
        return p

    def _merge_named_project_rows(self, projects):
        """同名业务项目合成一行；默认工作区桶保持按根分开。"""
        named, kept = {}, []
        for p in projects:
            if p.get("project") == self.TEAM_DEFAULT_PROJECT:
                kept.append(p)
                continue
            named.setdefault(p["project"], []).append(p)
        for rows in named.values():
            if len(rows) == 1:
                kept.append(rows[0])
                continue
            kept.append(self._combine_project_rows(rows))
        return kept

    def _combine_project_rows(self, rows):
        rows = sorted(rows, key=lambda p: (
            -len(p.get("agents") or []), -int(p.get("alive") or 0),
            -int(p.get("online") or 0)))
        primary = dict(rows[0])
        keys = {primary.get("key")}
        roots = [primary.get("root")]
        seats, seen_seat = list(primary.get("seats") or []), {
            str(x.get("id") or "") for x in (primary.get("seats") or []) if x.get("id")}
        bulletin = list(primary.get("bulletin") or [])
        relays = list(primary.get("relays") or [])
        locks = list(primary.get("locks") or [])
        wip = list(primary.get("wip") or [])
        for extra in rows[1:]:
            primary["agents"] = list(primary.get("agents") or []) + list(extra.get("agents") or [])
            primary["ended"] = int(primary.get("ended") or 0) + int(extra.get("ended") or 0)
            primary["offline"] = int(primary.get("offline") or 0) + int(extra.get("offline") or 0)
            keys.add(extra.get("key"))
            if extra.get("root") and extra.get("root") not in roots:
                roots.append(extra.get("root"))
            if not str(primary.get("board") or "").strip() and extra.get("board"):
                primary["board"] = extra["board"]
                primary["board_updated"] = extra.get("board_updated")
            bulletin.extend(extra.get("bulletin") or [])
            relays.extend(extra.get("relays") or [])
            locks.extend(extra.get("locks") or [])
            wip.extend(extra.get("wip") or [])
            for st in extra.get("seats") or []:
                sid = str(st.get("id") or "")
                if sid and sid in seen_seat:
                    continue
                if sid:
                    seen_seat.add(sid)
                seats.append(st)
        occ = {a.get("conv") for a in primary["agents"] if a.get("conv")}
        for st in seats:
            st["occupied"] = st.get("id") in occ
        primary["seats"] = seats
        primary["roots"] = [r for r in roots if r]
        primary["locks"] = locks
        # 跨工作区合成一队时，几个仓的未提交改动也要一起算，否则合并那一下
        # 正好把别的仓里的孤儿 WIP 藏掉了
        seen_w, uniq_w = set(), []
        for x in wip:
            mark = (x.get("key"), x.get("file"))
            if mark in seen_w:
                continue
            seen_w.add(mark)
            uniq_w.append(x)
        primary["wip"] = uniq_w[:40]
        mod = self._facts_mod()
        primary["wip_orphans"] = (mod.orphan_summary(uniq_w) if mod is not None
                                  else {"count": 0, "unowned": 0, "text": "", "owners": []})
        primary["waiting"] = sum(int(x.get("waiting") or 0) for x in locks)
        primary["tracks"] = self._tracks_of(primary["agents"], seats)
        primary["online"] = sum(1 for a in primary["agents"] if a.get("connected"))
        primary["alive"] = sum(1 for a in primary["agents"]
                               if a.get("live") in ("waiting", "working", "stalled", "recent"))
        seen_b, uniq_b = set(), []
        for x in bulletin:
            mark = (x.get("ts"), x.get("text"), x.get("from8"))
            if mark in seen_b:
                continue
            seen_b.add(mark)
            uniq_b.append(x)
        primary["bulletin"] = uniq_b[-20:]
        seen_r, uniq_r = set(), []
        for x in relays + self._relays_for_row(keys, primary.get("project")):
            mark = (x.get("ts"), x.get("text"), x.get("from8"), x.get("to"))
            if mark in seen_r:
                continue
            seen_r.add(mark)
            uniq_r.append(x)
        primary["relays"] = uniq_r[-40:]
        return self._finish_project_row(primary)

    @staticmethod
    def _tracks_of(agents, seats):
        """本项目出现过的业务线，按「在跑的人多」排前面；没设业务线的不算一条。"""
        counts, order = {}, []
        for a in agents:
            t = (a.get("track") or "").strip()
            if not t:
                continue
            if t not in counts:
                counts[t] = 0
                order.append(t)
            counts[t] += 1
        for st in seats or []:
            t = str(st.get("track") or "").strip()
            if t and t not in counts:
                counts[t] = 0
                order.append(t)
        # 名次先固定下来再排：list.sort 期间 CPython 会把列表临时清空，
        # 拿 order.index 当次序键会当场 ValueError（有业务线就整个面板打不开）
        seen = {t: i for i, t in enumerate(order)}
        order.sort(key=lambda t: (-counts[t], seen[t]))
        return [{"name": t, "count": counts[t]} for t in order]

    # 控制台自己塞进 tab 的 role=user 气泡：转告回执、面板广播、黑板提醒、送审
    # 通知这类。队友的转告则统一带 who="agent·<对方名>"。都不是「用户派下来的活」
    MACHINE_WHO = ("控制台", "团队面板", "黑板")

    @classmethod
    def _is_machine_who(cls, who):
        return str(who or "") in cls.MACHINE_WHO or str(who or "").startswith("agent·")

    @classmethod
    def _is_machine_msg(cls, m):
        msg = m or {}
        return msg.get("role") == "sys" or cls._is_machine_who(msg.get("who"))

    @staticmethod
    def _may_take_the_reply_slot(who):
        """这条消息够不够格占掉「用户的回复位」（tab 正阻塞在 zhi 时当场答复它）。

        够格的只有用户本人：面板广播、送审、手机端回复都是用户在操作。队友的转告、
        控制台回执、黑板提醒不是——它们一抢答，tab 明明在等用户拍板，agent 收到的
        却是队友的话，用户那条反而被挤掉（08-04 实测两次：agent 只好把汇报重发一遍）。
        不抢答也不会丢：等用户真回话时由那一答捎带出去（见 send_reply）。"""
        who = str(who or "")
        return not (who in ("控制台", "黑板") or who.startswith("agent·"))

    @staticmethod
    def _queue_defer_expired(entry, now=None):
        """旧队列没有 defer_until，视为立即可发；坏值也不能卡死整条队列。"""
        try:
            return float((entry or {}).get("defer_until") or 0) <= float(
                time.time() if now is None else now)
        except (TypeError, ValueError):
            return True

    def _schedule_recall_flush(self, session_id):
        """撤回窗口到点后立刻冲刷这一条，不指望 state_tick 刚好没卡住。

        09-04 截图：待回复时点发送，条停在「排队中」，只有「继续」能送到。
        发送走的是 5 秒服务端缓冲，交付全靠每秒一拍的 _flush_queue；那一拍
        若被探活/读库拖住，人就会在窗口里看见「发出去了但还在排队」。
        到期后再冲一次，和 tick 重复调用是安全的（队列空就立刻 return）。"""
        delay = float(self.RECALL_DELAY_SECS) + 0.05

        def fire(sid=session_id):
            s = hub.HUB.sessions.get(sid)
            if s is None:
                return
            try:
                hub.HUB._flush_queue(s)
            except Exception as e:  # noqa: BLE001
                hub.log_event("撤回到期冲刷失败 sid={}: {}".format(sid, e))

        timer = threading.Timer(delay, fire)
        timer.daemon = True
        timer.start()
        return timer

    def _tag_along_for_reply(self, s, pending_taken, now=None):
        """立刻答复（继续 / 无撤回窗口）时，把该捎带的排队消息一起带走。

        三类：
        1. 队友转告 / 控制台回执（不够格占回复位）——不捎带会永远压在队列里
        2. 决策卡：用户看到卡之前说的话（_flush_queue 扣住的），答卡时一起走
        3. 用户刚点的「发送」还压在撤回缓冲里：点「继续」会抢走 pending，
           若不捎带，发送条会永远停在排队皮上（09-04 待回复发送事故）
        """
        now = time.time() if now is None else now
        pid = str((pending_taken or {}).get("id") or "")
        has_card = bool((pending_taken or {}).get("card"))
        out = []
        for e in list(s.queued or []):
            matches = bool(pid) and str(e.get("reply_to") or "") == pid
            who_ok = self._may_take_the_reply_slot(e.get("who"))
            if who_ok and matches:
                out.append(e)
                continue
            if not self._queue_defer_expired(e, now):
                continue
            if not who_ok:
                out.append(e)
                continue
            if has_card and not e.get("redelivery"):
                out.append(e)
        return out

    def _agent_desc(self, s, hb_alive):
        """卡片上「这人此刻在干嘛」。

        原先只认 agent 自己 zt 上报的 activity——不主动上报的 tab 那行就是空的
        （用户 08-04 实测：面板上四个 tab 只有一个说得出在干嘛）。这里按可信度
        逐级兜底：自报 › 它正等你回答的那句提问 › 你最后派下去的那句活。

        「你最后派下去的那句活」只认用户自己发的：队友的转告和控制台回执也是
        role=user 的气泡，不排掉的话卡片会把别人递的话当成这人的活显示出来
        （08-04 截图：团队面板修复那张卡写着「最后派的活：【agent 转告 · 来自…」）。"""
        act = (getattr(s, "agent_activity", "") or "").strip()
        if hb_alive and act:
            return act
        pend = getattr(s, "pending", None)
        if pend and (pend.get("message") or "").strip():
            return "在等你回：" + self._one_line(pend["message"])
        # 这里是在 team_state 已持着 HUB.lock 的循环里跑的，绝不能再取 s.lock
        # （另一侧 save_state 就是 HUB.lock→s.lock 的顺序，反着来会锁死）
        try:
            last = next((m for m in reversed(list(s.messages or []))
                         if m.get("role") == "user" and m.get("html")
                         and not self._is_machine_msg(m)), None)
        except Exception:
            last = None
        if last:
            return "最后派的活：" + self._one_line(
                re.sub(r"<[^>]+>", "", html_mod.unescape(last["html"])))
        return ""

    @staticmethod
    def _one_line(text, limit=48):
        t = re.sub(r"\s+", " ", str(text or "")).strip()
        return t[:limit] + ("…" if len(t) > limit else "")

    def _sidebar_last_chat(self, s):
        """侧栏最后一条人话，口径对齐 bajie 的 lastPreview / lastRole。

        get_state 持着 HUB.lock，不能再取 s.lock（和 _agent_desc 同一条）。
        机器气泡（sys / agent·转告）不算「你/AI」预览。"""
        if hub.runtime_adapter.kind(s) == "codex":
            turn = getattr(s, "native_turn_view", None) or {}
            if getattr(s, "native_turn_view_id", None) == getattr(s, "native_thread_id", ""):
                for step in reversed(turn.get("steps") or []):
                    if step.get("kind") == "text" and step.get("text"):
                        return "agent", self._one_line(step["text"], 72)
        try:
            msgs = list(s.messages or [])
        except Exception:
            return "", ""
        for m in reversed(msgs):
            role = m.get("role")
            if role not in ("user", "ai"):
                continue
            if self._is_machine_msg(m):
                continue
            text = self._one_line(_html_text(m.get("html")), 72)
            if not text:
                continue
            return ("user" if role == "user" else "agent"), text
        return "", ""

    def team_set_role(self, session_id, role):
        """给某个 tab 定角色。角色跟着对话 ID 走，掉线重连、重启控制台都还在。"""
        role = str(role or "").strip()
        if role and role not in self.TEAM_ROLE_LABELS:
            return {"ok": False, "error": "未知角色: {}".format(role)}
        s = hub.HUB.sessions.get(session_id)
        if not s:
            return {"ok": False, "error": "会话不存在"}
        with hub.HUB._team_project_lock:
            roles = dict(hub.HUB.cfg.get("team_roles") or {})
            if role:
                roles[s.conv_key] = role
            else:
                roles.pop(s.conv_key, None)
            hub.HUB.cfg["team_roles"] = roles
            # 坐在固定席位上的 tab 改角色时，席位提示词也必须同步更新。
            _root, _project, seat = self._seat_lookup(s.conv_key)
            if seat is not None:
                seat["role"] = role
            hub.save_config(hub.HUB.cfg)
        return {"ok": True, "role": role}

    def _remember_label(self, s):
        """名字/分工/归属要变之前，把旧称呼记进曾用名历史。

        团队成员不是固定的：分工一改标签就变（渲染升级→推送排障，07-31 实测），
        别的 agent 还按旧名字转告就会扑空。历史留着，转告匹配连曾用名一起认。"""
        try:
            hist = list(getattr(s, "name_history", []) or [])
            for v in (s.name, self.session_label(s)):
                v = (v or "").strip()
                if v and v not in hist:
                    hist.append(v)
            s.name_history = hist[-8:]
        except Exception:
            pass

    def team_set_assign(self, session_id, text):
        """给某个 tab 写「负责哪块功能」。与角色同样按对话 ID 记，接手自动继承。"""
        s = hub.HUB.sessions.get(session_id)
        if not s:
            return {"ok": False, "error": "会话不存在"}
        self._remember_label(s)
        text = str(text or "").strip()[:120]
        with hub.HUB._team_project_lock:
            m = dict(hub.HUB.cfg.get("team_assign") or {})
            if text:
                m[s.conv_key] = text
            else:
                m.pop(s.conv_key, None)
            hub.HUB.cfg["team_assign"] = m
            # 用户在面板上手填的分工是人的意思，不是自动截来的占位：清掉 auto 标记，
            # 此后 agent 再自报名字也不会把它顶掉
            self._mark_assign_auto(s.conv_key, False)
            # 该 tab 若占着某个席位，席位上的分工一并更新，接手的人才拿得到最新分工
            _root, _project, seat = self._seat_lookup(s.conv_key)
            if seat is not None:
                seat["assign"] = text
            hub.save_config(hub.HUB.cfg)
        return {"ok": True, "assign": text}

    # 「待命·<工作区>N」这种壳名说明这个 tab 还没被认领过活
    SHELL_NAME_RE = re.compile(r"^(待命|新任务|会话|Persistent Plus check-?in)")

    def _auto_label_on_dispatch(self, s, text, who=None):
        """派活落地时，把这份活的第一句写进空着的「负责哪块功能」，并给壳名改名。

        用户的固定用法是「所有新对话先用报到词接入 → 再从面板派活」，于是一排
        tab 全叫「待命·cursor工作流N」、分工全是空的，隔天自己都认不出谁在干嘛
        （08-04 实测截图：4 个 tab 3 个看不出在做什么）。这里只在「壳名 + 没分工」
        时补一次，用户手改过名字（name_locked）或已有分工的一概不动。

        who 闸门放在函数里而不是各调用点：队友的转告投给「正等用户回话」的 tab 时
        走的是 send_reply 而不是 queue_message，之前只有后者设了闸门，于是同一条
        转告落到闲着的 tab 就没事、落到正在等回复的 tab 就把人家的名字和分工改成
        了转告正文（08-04 用户报「转告在乱转发」）。"""
        if self._is_machine_who(who):
            return
        try:
            raw = re.sub(r"^\[[^\]]{1,20}\]\s*", "", str(text or ""))
            if raw.lstrip().startswith(hub.TAKEOVER_PROMPT_HEAD):
                # 接手派单不是「给这个壳派活」：它马上要改用被接手的原 ID 报到，
                # 壳本身会被 _reap_takeover_shell 收掉。在这儿改名的后果是壳名不再
                # 以「待命」打头，_is_checkin_shellish 认不出它 → 它的 uuid 认领
                # 变成排他的，被接手的原 tab 反而永远校准不回身份、复活不了；
                # 面板上还留下一排叫「你的任务：接手一个此前在…」的鬼标签，
                # team_assign 也被整段提示词污染（08-04 实测，一次派 4 个全中）。
                return
            # 任务安排站的派发文案是结构化的：「【任务派发】<人写的标题>」，批量则
            # 「【批量派发】共 N 个任务 … 【标题】<第一条标题>」。名字直接用人写的
            # 标题——08-27 用户问「不能让这个名字一次就正确吗」：此前拿整句
            # 「【任务派发】修转播延时 | 优先级…」截 24 字当名字，要等 agent 自报
            # 真名才换正，中间满屏口头禅。非派发文案维持旧口径：第一句截 24 字。
            m = re.match(r"^\s*【任务派发】\s*(\S[^\n]*)", raw)
            if m:
                raw = m.group(1)
            else:
                mb = re.match(r"^\s*【批量派发】共\s*(\d+)\s*个任务", raw)
                if mb:
                    mt = re.search(r"【标题】([^\n]+)", raw)
                    if mt:
                        raw = "{}（批量{}件）".format(mt.group(1).strip(), mb.group(1))
            txt = self._one_line(raw, self.AUTO_ASSIGN_LEN)
            with hub.HUB._team_project_lock:
                if not txt or self._team_assign(s):
                    return
                name = str(getattr(s, "name", "") or "")
                if getattr(s, "name_locked", False) or not self.SHELL_NAME_RE.match(name):
                    return
                if getattr(s, "agent_named", False):
                    return  # agent 已经明说过在做什么，别再拿这句话的前 24 字盖它
                m = dict(hub.HUB.cfg.get("team_assign") or {})
                m[s.conv_key] = txt
                hub.HUB.cfg["team_assign"] = m
                # 记一笔「这条是自动截来的」：agent 一报真名就撤掉它让位（见
                # _drop_auto_assign）。用户在面板上手填的走 team_set_assign，不打这个标。
                self._mark_assign_auto(s.conv_key, True)
                _root, _project, seat = self._seat_lookup(s.conv_key)
                if seat is not None:
                    seat["assign"] = txt
                hub.save_config(hub.HUB.cfg)
            self._remember_label(s)  # 老名字留档，队友按旧名字转告还找得到
            with hub.HUB.lock:
                s.name = hub.HUB._dedupe_name(txt, exclude_id=s.id,
                                          conv_key=s.conv_key)
            s.rev += 1
            hub.log_event("派活自动命名 tab={} → {}（分工同步写入）".format(name, s.name))
        except Exception:  # noqa: BLE001
            pass  # 自动命名塌了绝不能挡住派活本身

    def team_set_track(self, session_id, text):
        """给某个 tab 定业务线（子项目）。

        同一个工作区里常并行着互不相干的几摊活（cursor工作流 目录下一个在做视频
        编辑、一个在做直播）：只按路径分组，面板上它们混成一堆、广播也会互相打扰。
        与角色/分工同口径按对话 ID 记，接手沿用原 ID 时自动继承。"""
        s = hub.HUB.sessions.get(session_id)
        if not s:
            return {"ok": False, "error": "会话不存在"}
        self._remember_label(s)
        text = str(text or "").strip()[:24]
        with hub.HUB._team_project_lock:
            m = dict(hub.HUB.cfg.get("team_tracks") or {})
            if text:
                m[s.conv_key] = text
            else:
                m.pop(s.conv_key, None)
            hub.HUB.cfg["team_tracks"] = m
            _root, _project, seat = self._seat_lookup(s.conv_key)
            if seat is not None:
                seat["track"] = text
            hub.save_config(hub.HUB.cfg)
        return {"ok": True, "track": text}

    # ---------- 席位：把「团队里的一个位置」和「谁在坐」分开 ----------
    # 用户实际用法是「先用接手提示词拉个 agent 进来，回头再派活」。若每次接手都
    # 现给一个新对话 ID，角色/分工就得重新配一遍。席位自带固定 conversation_id：
    # 新人用它报到 → 原 tab 复活、角色分工原样继承，团队关系跟着位置走而不是跟着人走。
    def team_seat_add(self, root, name="", role="", assign="", track="", project=None):
        role = str(role or "").strip()
        if role and role not in self.TEAM_ROLE_LABELS:
            return {"ok": False, "error": "未知角色: {}".format(role)}
        # read-list-append-write 必须是一整个临界区。只锁 _project_bucket 的首次
        # 建桶仍会让两个同时「加席位」的请求各拿旧列表，后写者覆盖先写者。
        with hub.HUB._team_project_lock:
            key = hub.norm_root(root)
            bucket = None if project is None else self._project_bucket(
                root, project, create=True, name=project)
            rows = list(bucket.get("seats") or []) if bucket is not None else \
                list((hub.HUB.cfg.get("team_seats") or {}).get(key) or [])
            seat = {"id": uuid.uuid4().hex[:8], "created": time.time(),
                    "name": str(name or "").strip()[:24] or "{}号位".format(len(rows) + 1),
                    "role": role, "assign": str(assign or "").strip()[:120],
                    "track": str(track or "").strip()[:24]}
            rows.append(seat)
            if bucket is not None:
                bucket["seats"] = rows
            else:
                seats = dict(hub.HUB.cfg.get("team_seats") or {})
                seats[key] = rows
                hub.HUB.cfg["team_seats"] = seats
            if role:
                roles = dict(hub.HUB.cfg.get("team_roles") or {})
                roles[seat["id"]] = role
                hub.HUB.cfg["team_roles"] = roles
            if seat["assign"]:
                m = dict(hub.HUB.cfg.get("team_assign") or {})
                m[seat["id"]] = seat["assign"]
                hub.HUB.cfg["team_assign"] = m
            if seat["track"]:
                m = dict(hub.HUB.cfg.get("team_tracks") or {})
                m[seat["id"]] = seat["track"]
                hub.HUB.cfg["team_tracks"] = m
            hub.save_config(hub.HUB.cfg)
        return {"ok": True, "seat": seat}

    def team_seat_set(self, seat_id, name=None, role=None, assign=None, track=None):
        with hub.HUB._team_project_lock:
            _root, _project, seat = self._seat_lookup(seat_id)
            if seat is None:
                return {"ok": False, "error": "席位不存在"}
            if role is not None:
                role = str(role).strip()
                if role and role not in self.TEAM_ROLE_LABELS:
                    return {"ok": False, "error": "未知角色: {}".format(role)}
                seat["role"] = role
                roles = dict(hub.HUB.cfg.get("team_roles") or {})
                if role:
                    roles[seat_id] = role
                else:
                    roles.pop(seat_id, None)
                hub.HUB.cfg["team_roles"] = roles
            if name is not None:
                seat["name"] = str(name).strip()[:24] or seat.get("name") or "席位"
            if assign is not None:
                seat["assign"] = str(assign).strip()[:120]
                m = dict(hub.HUB.cfg.get("team_assign") or {})
                if seat["assign"]:
                    m[seat_id] = seat["assign"]
                else:
                    m.pop(seat_id, None)
                hub.HUB.cfg["team_assign"] = m
            if track is not None:
                seat["track"] = str(track).strip()[:24]
                m = dict(hub.HUB.cfg.get("team_tracks") or {})
                if seat["track"]:
                    m[seat_id] = seat["track"]
                else:
                    m.pop(seat_id, None)
                hub.HUB.cfg["team_tracks"] = m
            hub.save_config(hub.HUB.cfg)
            return {"ok": True, "seat": seat}

    def team_seat_remove(self, seat_id):
        # 删除会整体回写 seats/projects；必须和 add/set 及席位关联配置共用一把锁，
        # 否则删除线程拿到的旧副本会把并发新增的席位静默抹掉。
        with hub.HUB._team_project_lock:
            seats = dict(hub.HUB.cfg.get("team_seats") or {})
            hit = False
            for key, rows in list(seats.items()):
                keep = [x for x in (rows or []) if x.get("id") != seat_id]
                if len(keep) != len(rows or []):
                    hit = True
                    seats[key] = keep
            projects = dict(hub.HUB.cfg.get("team_projects") or {})
            for root, rows in list(projects.items()):
                rows = dict(rows or {})
                for project, bucket in list(rows.items()):
                    bucket = dict(bucket or {})
                    current = list(bucket.get("seats") or [])
                    keep = [x for x in current if x.get("id") != seat_id]
                    if len(keep) != len(current):
                        hit = True
                        bucket["seats"] = keep
                        rows[project] = bucket
                projects[root] = rows
            if not hit:
                return {"ok": False, "error": "席位不存在"}
            hub.HUB.cfg["team_seats"] = seats
            hub.HUB.cfg["team_projects"] = projects
            for cfgkey in ("team_roles", "team_assign", "team_tracks"):
                m = dict(hub.HUB.cfg.get(cfgkey) or {})
                m.pop(seat_id, None)
                hub.HUB.cfg[cfgkey] = m
            hub.save_config(hub.HUB.cfg)
            return {"ok": True}

    def team_seat_prompt(self, seat_id):
        """某个席位的入场提示词：固定 conversation_id + 角色纪律 + 分工 + 公告板。

        谁拿到它都能坐进同一个位置——首次拉人、接手换人、agent 挂了重开，全用这一份。
        """
        root, project, seat = self._seat_lookup(seat_id)
        if seat is None:
            return {"ok": False, "error": "席位不存在"}
        base = self.new_chat_prompt(cwd=root, write_mcp=True)
        if not base.get("ok"):
            return base
        # 席位的 ID 就是对话 ID：把预分配的那个换掉，团队关系才跟着位置走
        prompt = base["prompt"].replace(base["conversation_id"], seat_id)
        parts = [prompt, "【你的席位】{}（本项目团队固定席位，conversation_id 就是席位号 {}；"
                         "以后换人接手也用它，角色和分工自动继承）".format(
                             seat.get("name") or "席位", seat_id)]
        role = seat.get("role") or ""
        if role in self.TEAM_ROLE_LABELS:
            parts.append("【你的角色】{}：{}".format(
                self.TEAM_ROLE_LABELS[role], self.TEAM_ROLE_RULES[role]))
        if seat.get("track"):
            parts.append("【你在哪条业务线】{}（这个工作区里同时还跑着别的业务线，"
                         "只发本业务线的话用 ji(action=\"广播\", category=\"本组\")）"
                         .format(seat["track"]))
        if seat.get("assign"):
            parts.append("【你负责的功能】{}\n同项目还有别的 agent 在做其它功能，"
                         "别去动不属于你这块的代码。".format(seat["assign"]))
        board = self._team_board(root, project)["text"]
        if board:
            parts.append("【项目公告板】（同项目所有 agent 共用，请先读）\n" + board)
        return {"ok": True, "prompt": "\n\n".join(parts), "conversation_id": seat_id,
                "seat": seat}

    def team_set_board(self, root, text, project=None):
        """项目公告板：分支、禁改区、怎么跑测试、现阶段目标——新 agent 报到自动带上。"""
        text = str(text or "").strip()[:4000]
        with hub.HUB._team_project_lock:
            if project is not None:
                bucket = self._project_bucket(root, project, create=bool(text), name=project)
                if bucket is not None:
                    bucket["board"] = {"text": text, "updated_at": time.time()} if text else {}
            else:
                boards = dict(hub.HUB.cfg.get("team_boards") or {})
                if text:
                    boards[hub.norm_root(root)] = {"text": text, "updated_at": time.time()}
                else:
                    boards.pop(hub.norm_root(root), None)
                hub.HUB.cfg["team_boards"] = boards
            hub.save_config(hub.HUB.cfg)
        return {"ok": True}

    def team_broadcast(self, root, text, exclude_session_id="", track="", project=None):
        """对同项目所有在线 agent 发同一句话（「都停手，我要 rebase」这种）。

        走排队通道：AI 正在等回复就直送，正在干活就排到它下次提问时送达。
        与顶栏的停车/发车无关，那套是全局的，这里只影响一个项目。
        给了 track 就只发那条业务线——同工作区里另一摊活跟这事没关系，别打扰。"""
        text = str(text or "").strip()
        if not text:
            return {"ok": False, "error": "广播内容不能为空"}
        track = str(track or "").strip()
        targets = [s for s in self._team_sessions(root, project=project)
                   if s.id != str(exclude_session_id or "")
                   and (not track or self._team_track(s) == track)]
        if not targets:
            return {"ok": False, "error": "{}当前没有在线 agent".format(
                "业务线「{}」".format(track) if track else "该项目")}
        sent, failed = [], []
        body = "【{}广播】{}".format(track or "项目", text)
        for s in targets:
            r = self.queue_message(s.id, body, [], who="团队面板")
            (sent if r.get("ok") else failed).append(s.name)
        if not sent:
            return {"ok": False, "error": "全部投递失败：{}".format("、".join(failed))}
        return {"ok": True, "sent": sent, "failed": failed}

    def _git_change_digest(self, root):
        """本项目当前未提交的改动摘要（送审用）。不是 git 仓库就返回空。"""
        def run(args):
            try:
                p = subprocess.run(["git", "-C", str(root)] + args, capture_output=True,
                                   timeout=10, text=True, encoding="utf-8", errors="replace")
                return p.stdout.strip() if p.returncode == 0 else ""
            except Exception:
                return ""
        status = run(["status", "--porcelain"])
        if not status:
            return ""
        stat = run(["diff", "--stat", "HEAD"]) or run(["diff", "--stat"])
        lines = status.splitlines()[:60]
        out = ["改动文件（git status）："] + ["  " + x for x in lines]
        if len(status.splitlines()) > 60:
            out.append("  …（还有 {} 个文件）".format(len(status.splitlines()) - 60))
        if stat:
            out += ["", "改动规模（git diff --stat）："] + \
                   ["  " + x for x in stat.splitlines()[-25:]]
        return "\n".join(out)

    def team_review_send(self, session_id, note=""):
        """把实现方的改动推给同项目的审查 agent。"""
        s = hub.HUB.sessions.get(session_id)
        if not s:
            return {"ok": False, "error": "会话不存在"}
        scope = self._team_scope(s)
        root = scope["root"]
        reviewers = [x for x in self._team_sessions(root, project=scope["project"], roles=("review",))
                     if x.id != s.id]
        if not reviewers:
            return {"ok": False, "error": "该项目没有在线的「审查」角色，先给一个 tab 设成审查"}
        digest = self._git_change_digest(root) or "（没读到 git 改动，请让实现方自己说明改了什么）"
        assign = self._team_assign(s)
        body = "\n".join([
            "【代码审查请求】来自 {}（项目 {}）".format(s.name, Path(root or "").name or "未知"),
            "送审方负责的功能：{}".format(assign or "（未填分工）"),
            "注意：同项目可能有多个实现方并行改动，git 改动里可能混着别人的文件，"
            "只审与上面这块功能相关的部分。",
            "工作目录：{}".format(root or "未知"),
            ("⚠ 送审方是跨工作区接手的，它自己待在 {}，本项目的代码它不一定够得着——"
             "审的时候留意有没有改错仓库。".format(s.cwd) if hub.cross_workspace(s) else ""),
            "",
            digest,
            "",
            str(note or "").strip(),
            "",
            "请只做审查、不要改代码：逐条指出正确性/边界/回归风险，",
            "最后单独一行给结论「【审查通过】」或「【打回】+ 一句话原因」。",
        ]).replace("\n\n\n", "\n\n")
        sent, failed = [], []
        for r in reviewers:
            res = self.queue_message(r.id, body, [], who="团队面板")
            (sent if res.get("ok") else failed).append(r.name)
        if not sent:
            return {"ok": False, "error": "投递失败：{}".format("、".join(failed))}
        return {"ok": True, "sent": sent, "failed": failed}

    def team_review_reply(self, session_id, verdict, note=""):
        """审查方的结论回送给同项目的实现方（负责人也抄送）。"""
        s = hub.HUB.sessions.get(session_id)
        if not s:
            return {"ok": False, "error": "会话不存在"}
        verdict = "通过" if str(verdict) in ("pass", "通过", "ok", "True", "true") else "打回"
        scope = self._team_scope(s)
        targets = [x for x in self._team_sessions(scope["root"], project=scope["project"],
                                                   roles=("impl", "owner"))
                   if x.id != s.id]
        if not targets:
            return {"ok": False, "error": "该项目没有在线的「实现/负责人」角色可回执"}
        body = "【审查结论】{} · 来自 {}".format(verdict, s.name)
        note = str(note or "").strip()
        if note:
            body += "\n" + note
        if verdict == "打回":
            body += "\n请按上述意见修，改完再点一次送审。"
        sent, failed = [], []
        for t in targets:
            res = self.queue_message(t.id, body, [], who="团队面板")
            (sent if res.get("ok") else failed).append(t.name)
        if not sent:
            return {"ok": False, "error": "投递失败：{}".format("、".join(failed))}
        return {"ok": True, "verdict": verdict, "sent": sent, "failed": failed}

    def team_intake_prompt(self, root, role="", assign="", track="", project=None):
        """给这个项目拉新 agent 的接入提示词：报到词 + 角色纪律 + 业务线 + 分工 + 公告板。"""
        base = self.new_chat_prompt(cwd=root, write_mcp=True)  # 让路 + 报到潮 + 该项目 MCP
        if not base.get("ok"):
            return base
        role = str(role or "").strip()
        track = str(track or "").strip()
        assign = str(assign or "").strip()
        conv_key = base["conversation_id"]
        # scope 与预填角色/分工/业务线必须作为一个配置事务落盘；否则并发加删席位
        # 会让这些 companion mapping 覆盖彼此，或只留下半套 intake 配置。
        with hub.HUB._team_project_lock:
            project_key, project_name = self._register_team_project_member(
                root, conv_key, project)
            if role in self.TEAM_ROLE_LABELS:
                roles = dict(hub.HUB.cfg.get("team_roles") or {})
                roles[conv_key] = role
                hub.HUB.cfg["team_roles"] = roles
            if track:
                tracks = dict(hub.HUB.cfg.get("team_tracks") or {})
                tracks[conv_key] = track[:24]
                hub.HUB.cfg["team_tracks"] = tracks
            if assign:
                assignments = dict(hub.HUB.cfg.get("team_assign") or {})
                assignments[conv_key] = assign[:120]
                hub.HUB.cfg["team_assign"] = assignments
            # intake 无论是否预填业务线/分工都要持久化成员项目；task_name 后续改名不能
            # 再把它从这个 TeamScope 挪走。
            hub.save_config(hub.HUB.cfg)
        parts = [base["prompt"]]
        # 用户实际用法（07-31）：把这段贴给「已经报到过」的对话派活。不加这句的话，
        # 那个 agent 会拿新 ID 再报到一次——控制台里凭空多一个分身 tab，角色分工
        # 全挂在没人用的新 ID 上
        parts.append(
            "【已报到过的对话请注意】如果你本对话此前已经用某个 conversation_id 报到过，"
            "上面的报到步骤跳过、新 ID 作废：继续沿用你原来的 conversation_id，"
            "立刻调一次 zt(status=\"接令\", activity=本条提示词里的分工) 确认接令即可，"
            "不要换 ID 重新报到（换 ID = 控制台里多出一个分身，你的角色和记录会断掉）。")
        if role in self.TEAM_ROLE_LABELS:
            parts.append("【你的角色】{}：{}".format(
                self.TEAM_ROLE_LABELS[role], self.TEAM_ROLE_RULES[role]))
        if track:
            parts.append("【你在哪条业务线】{}（这个工作区里同时还跑着别的业务线，"
                         "只跟本业务线的人打招呼用 ji(action=\"广播\", category=\"本组\")；"
                         "task_name 起名带上它，控制台一眼能分清）".format(track))
        if assign:
            parts.append("【你负责的功能】{}\n同项目还有别的 agent 在做其它功能，"
                         "别去动不属于你这块的代码。".format(assign))
        board = self._team_board(root, project_key)["text"]
        if board:
            parts.append("【项目公告板】（{}，同项目所有 agent 共用，请先读）\n{}".format(
                project_name or Path(root).name or root, board))
        # 这两条是被实测反复咬到的：不上报状态的 agent 在面板上只能显示灰点「无输出
        # 信号」（控制台只能靠旁证猜）；被借来干别的项目的 agent 又常常忘了自己人在
        # 哪个工作区、改本项目文件要走绝对路径。
        parts.append(
            "【干活时必须上报】每完成一个动作就调一次 zt(status, activity)——非阻塞、"
            "即返回。不上报的话控制台判不出你是在思考还是已经挂了，团队面板上你就是"
            "一个灰点。")
        parts.append(
            "【你在哪、改哪】你要改的是项目 {}（{}）。如果你当前 Cursor 窗口的工作区不是"
            "它，说明你是被借过来的：文件照样能改，但一律走绝对路径，别用相对路径或 @ 引用"
            "（那些还指着你自己那个工作区）。改公共文件前先看控制台的文件占用看板，"
            "同项目可能有别人正占着。".format(Path(root).name or root, root))
        parts.append(
            "【跟队友互通】改公共文件前、发现别人范围的 bug、完成对接点时，直接递话："
            "ji(action=\"转告\", category=对方tab名或对话ID前8位, content=消息)；"
            "ji(action=\"广播\", content=消息) 发全队。对方下次调 zhi 或 zt 时收到"
            "（zt 顺路取信，它埋头干活时也收得到）。")
        parts.append(
            "【重大情况写黑板、动手前读黑板】转告是点对点的，黑板才是全队"
            "随时可见的布告：遇到 部署/大改/提交/卡住/事故/收工 六类情况，立即 "
            "ji(action=\"黑板\", category=类别, content=一句话)；要动公共文件、要部署、"
            "刚开工时，先 ji(action=\"黑板\")（content 留空）读一眼最近条目。"
            "黑板不会自动打断队友（同工作区常有互不相干的项目在跑）——事情涉及谁，"
            "写完黑板再用 ji(action=\"转告\"/\"广播\") 点名；用户在团队面板也看得见。")
        return {"ok": True, "prompt": "\n\n".join(parts),
                "conversation_id": base.get("conversation_id"), "role": role,
                "project": project_key}

    # ---------- 工作流编排 ----------
    def workflow_state(self):
        """工作流面板数据：链列表 + 角色表 + 「实现→审查→部署」预设模板。"""
        return {"ok": True,
                "workflows": (hub.WORKFLOW.snapshot() if hub.WORKFLOW is not None else []),
                "roles": [{"key": k, "label": v}
                          for k, v in self.TEAM_ROLE_LABELS.items()],
                "preset": hub.workflow_mod.STEP_TEMPLATE_PRESET}

    def workflow_create(self, name, root, steps, timeout_min=45):
        if hub.WORKFLOW is None:
            return {"ok": False, "error": "工作流引擎未启动"}
        return hub.WORKFLOW.create(name, root, steps, timeout_min)

    def workflow_action(self, wid, act, arg=""):
        """start=启动/继续 pause=暂停 delete=删除
        force_done=手动确认当前步完成 redispatch=重派当前步（arg=新执行人，可空）"""
        if hub.WORKFLOW is None:
            return {"ok": False, "error": "工作流引擎未启动"}
        acts = {
            "start": lambda: hub.WORKFLOW.start(wid),
            "pause": lambda: hub.WORKFLOW.pause(wid),
            "delete": lambda: hub.WORKFLOW.delete(wid),
            "force_done": lambda: hub.WORKFLOW.force_done(wid, note=arg),
            "redispatch": lambda: hub.WORKFLOW.redispatch(wid, executor=arg),
        }
        fn = acts.get(str(act or "").strip())
        return fn() if fn else {"ok": False, "error": "未知动作: {}".format(act)}

    def agentboard_release(self, root, file_key):
        """强解一个锁（占用方卡死/已退场时用）。下一次它再改这个文件会重新抢锁。"""
        bp = Path(root) / ".chijiu-tmp" / "agentboard.json"
        try:
            board = json.loads(bp.read_text(encoding="utf-8"))
        except Exception as exc:
            return {"ok": False, "error": "读看板失败: %s" % exc}
        if (board.get("locks") or {}).pop(file_key, None) is None:
            return {"ok": False, "error": "这个锁已经不在了"}
        (board.get("queue") or {}).pop(file_key, None)
        try:
            tmp = bp.with_suffix(".json.hubtmp")
            tmp.write_text(json.dumps(board, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, bp)
        except OSError as exc:
            return {"ok": False, "error": "写看板失败: %s" % exc}
        hub.log_event("强解文件锁 {} @ {}".format(file_key, root))
        return {"ok": True}

    # zt 是「每完成一个动作报一次」，一分钟内的自报都算新鲜
    SELF_REPORT_FRESH_SECS = 60

    def _self_report(self, s, now):
        """agent 自报的状态该怎么显示，返回 (状态, 活动, 上报距今秒数)。

        原先的闸门是 hb_alive（连接中且 15 秒内有心跳），心跳一断就把状态整个
        抹成空白。用意是「宁可不显示也别显示过期的」，可 15 秒这个窗口太窄——
        MCP 进程每次重启/被回收都会踩中，用户看到的是卡片上那行**忽然消失**
        （08-07 原话「右上角的状态有时候不会实时更新，不够准确」）。

        改成：闸门放宽到「通道还连着」，但超过一分钟没再报的，把「多久之前报的」
        缀在活动后面——是不是此刻正在干，一眼可辨，也不会被误当成实时。
        通道真断了仍旧抹空：那时该由死因与存活判定说话，自报状态不作数。
        """
        zt_ts = getattr(s, "agent_status_ts", 0) or 0
        age = int(now - zt_ts) if zt_ts else None
        if not getattr(s, "connected", False):
            return "", "", age
        status = getattr(s, "agent_status", "") or ""
        activity = getattr(s, "agent_activity", "") or ""
        if status and age is not None and age > self.SELF_REPORT_FRESH_SECS:
            stamp = self._age_text(age)
            activity = "{}（{}）".format(activity, stamp) if activity else stamp
        return status, activity, age

    @staticmethod
    def _takeover_pending_view(s, now):
        """「接手在途」的界面视图：派了谁、等了多久。落地/取消后为 None。"""
        tp = getattr(s, "takeover_dispatched", None)
        if not isinstance(tp, dict):
            return None
        view = {"to_name": str(tp.get("to_name") or ""),
                "age": int(now - float(tp.get("ts") or now))}
        if tp.get("batch_total"):
            # 批量派的：面板要能看出「它排第几张」，第 2 张起等得久是正常的
            view["batch_pos"] = int(tp.get("batch_pos") or 0)
            view["batch_total"] = int(tp.get("batch_total") or 0)
        return view

    def get_state(self):
        """纯读快照：零 I/O、零状态翻转、零副作用。
        所有基于时间的状态推进都在 HUB.state_tick_loop（每秒一拍）里做——
        UI 轮询再频繁也只是拷贝内存，不再拖慢核心、不再随机触发状态翻转。"""
        now = time.time()
        with hub.HUB.lock:
            sessions = []
            for sid in hub.HUB.order:
                s = hub.HUB.sessions.get(sid)
                if not s:
                    continue
                reconnecting = bool(not s.connected and s.recon_deadline
                                    and now <= s.recon_deadline)
                ide_active = bool(getattr(s, "ide_active_cache", False))
                processing_secs = None
                processing_stale = False
                if s.connected and s.pending is None and s.processing_since:
                    age = now - s.processing_since
                    limit = int(hub.HUB.cfg.get("processing_timeout_secs", 1800))
                    processing_secs = int(age)
                    processing_stale = age > max(45, limit // 2)
                # 心跳存活：连接中且最近 15s 内有 MCP 心跳 = 进程 100% 实时在世。
                # 心跳停但 socket 还连着 = 进程正被回收的过渡期。
                hb_alive = s.connected and (now - getattr(s, "last_heartbeat", 0) < 15)
                # 死因：state_tick 已经查好缓存在会话上，这里纯读（本方法零 I/O）
                dead = getattr(s, "death_info", None)
                # 存活判定同样由 state_tick 每秒算好缓存（本方法保持零 I/O）
                live = getattr(s, "live_cache", None) or {}
                a_status, a_activity, zt_age = self._self_report(s, now)
                model = getattr(s, "model_info", None) or {}
                last_role, last_preview = self._sidebar_last_chat(s)
                native_view = (getattr(s, "native_turn_view", None) or {}
                               if (hub.runtime_adapter.kind(s) == "codex"
                                   and getattr(s, "native_turn_view_id", None)
                                   == getattr(s, "native_thread_id", "")) else {})
                native_resume_locked = bool(self._native_resume_lock(
                    s, str(getattr(s, "native_thread_id", "") or ""),
                    str(native_view.get("turn_id") or "")))
                native_resume_disabled_reason = self._native_resume_reason(s, native_view)
                sessions.append({
                    "id": s.id,
                    "name": s.name,
                    "label": self.session_label(s),
                    # 这个 tab 用的是哪个模型（Cursor 库里的现时事实，tick 缓存好的）
                    "model": hub.sidebar_model_chip(model),
                    "model_full": model.get("model", ""),
                    "model_effort": model.get("effort", ""),
                    "cwd": s.cwd,
                    "task_root": hub.task_root_of(s),
                    "task_root_locked": bool(getattr(s, "task_root_locked", False)),
                    "affiliation": hub.affiliation_of(s),
                    "crossed": hub.cross_workspace(s),
                    # 并发闸单列：death_reason 一非空，前端就黑点+「已挂」+停转圈
                    # +踢出接手候选——排队态顶着这套死亡皮肤就是 08-27 的假失败观感
                    "death_reason": "" if (dead or {}).get("gate")
                                    else (dead or {}).get("reason", ""),
                    "death_advice": "" if (dead or {}).get("gate")
                                    else (dead or {}).get("advice", ""),
                    "gate_reason": ((dead or {}).get("reason", "")
                                    if (dead or {}).get("gate") else ""),
                    "gate_advice": ((dead or {}).get("advice", "")
                                    if (dead or {}).get("gate") else ""),
                    "peer_ip": s.peer_ip,
                    "pid": s.pid,
                    "conv_key": s.conv_key,
                    "runtime_kind": hub.runtime_adapter.kind(s),
                    "native_thread_id": str(getattr(s, "native_thread_id", "") or ""),
                    "native_desktop_connected": bool(
                        hub.runtime_adapter.kind(s) == "codex"
                        and getattr(s, "native_turn_view_id", None) == getattr(s, "native_thread_id", "")
                        and (getattr(s, "native_turn_view", None) or {}).get("desktop_connected")),
                    "native_resume_locked": native_resume_locked,
                    "native_resume_disabled_reason": native_resume_disabled_reason,
                    "capabilities": hub.runtime_adapter.capabilities(s),
                    "role": self._team_role(s),
                    "wtag": hub.HUB._window_tag(s.pid, s.cwd, getattr(s, "transcript_path", None)),
                    "connected": s.connected,
                    "archived": bool(getattr(s, "archived", False)),
                    # 已结束组里分清「系统交接收起」和「用户手动 ×」：08-12 误归档
                    # 排查时用户只看到空心圈，分不清是自己关的还是控制台收的。
                    # 只对归档中的 tab 有意义——活 tab 上残留的 handed_off_to
                    # （复活后未清的历史）不算
                    "end_kind": ("" if not getattr(s, "archived", False) else
                                 ("handover" if getattr(s, "handed_off_to", "") else "manual")),
                    # 接手在途：已派出接手提示词、还在等接手方用原 ID 报到
                    "takeover_pending": self._takeover_pending_view(s, now),
                    "reconnecting": reconnecting,
                    "ide_active": ide_active,
                    "heartbeat_alive": hb_alive,
                    "agent_status": a_status,
                    "agent_activity": a_activity,
                    "agent_status_age": zt_age,
                    "pending": s.pending is not None,
                    "pending_lost": s.pending_lost,
                    # 这条提问是 zhi(wait=false) 只发不等挂上的：agent 没在等，回复会先存着
                    "pending_deferred": bool(s.pending is not None
                                             and getattr(s, "wait_deferred", False)),
                    # 只发不等的提问用户已经回过了：回复存着等 agent 来取，输入区
                    # 文案要说「已回复」而不是还催人回
                    "pending_replied": bool(s.pending is not None
                                            and getattr(s, "buffered_reply", None) is not None),
                    "options": (s.pending or {}).get("options", []),
                    "card": (s.pending or {}).get("card"),
                    # 倒计时代决的到点时刻（epoch 秒）：服务端计时，界面只显示
                    "card_deadline": self._card_deadline(s.pending),
                    "rev": s.rev,
                    "seq": getattr(s, "msg_seq", 0),
                    "file": s.file_path,
                    "created": s.created_at,
                    "processing_secs": processing_secs,
                    "processing_stale": processing_stale,
                    "queued": len(s.queued),
                    "live_state": live.get("state", ""),
                    "live_label": live.get("label", ""),
                    # Cursor 正在生成 + 有绑定对话 = 实时时间线值得快拍（tick 缓存好的）
                    "live_generating": bool(live.get("generating")),
                    "has_cursor": bool(getattr(s, "cursor_uuid", None)),
                    "has_timeline": hub.runtime_adapter.capabilities(s)["timeline"],
                    # 出生窗口：窗口总线开出来的对话才有。控制台的「窗口作用域」按它
                    # 过滤（口径同 bajie 侧栏的来源窗口徽章）；手动开的对话没有这个
                    # 值，作用域退回按 cwd 认工作区，否则一过滤就把人手开的全藏了
                    "ext_instance": str(getattr(s, "ext_instance", "") or ""),
                    # tab 上那一句「第 N 步 · 运行命令 · …」（_tick_live_step 缓存好的）
                    "live_step": getattr(s, "live_step_cache", None),
                    # 「已结束」里分出「待续」用的三个信号（都零 I/O）：
                    # real=聊过真活（不是空壳报到）；done=agent 报过完工；
                    # idle_secs=多久没动静（last_heartbeat 随快照持久化，过夜重启后
                    # 仍是昨晚的时刻——这正是「关机过夜、明早重启找回未完成会话」的依据）
                    "real": hub.HUB._did_real_work(s),
                    "done": getattr(s, "agent_status", "") in ("task_complete", "dev_complete"),
                    "idle_secs": hub.session_core.session_idle_secs(s, now),
                    "last_role": last_role,
                    "last_preview": last_preview,
                })
        return {
            "sessions": sessions,
            "config": {
                "history_dir": hub.HUB.cfg["history_dir"],
                "always_on_top": bool(hub.HUB.cfg.get("always_on_top")),
                "audio_enabled": bool(hub.HUB.cfg.get("audio_enabled")),
                "share_enabled": bool(hub.HUB.cfg.get("share_enabled", True)),
                "max_messages": int(hub.HUB.cfg.get("max_messages", 20)),
                "max_history_files": int(hub.HUB.cfg.get("max_history_files", 20)),
                "history_keep_days": hub.HUB.history_keep_days(),
                "quiet_mode": bool(hub.HUB.cfg.get("quiet_mode")),
                "autostart_enabled": bool(hub.HUB.autostart_enabled),
                "config_error": hub.HUB.config_error or "",
                "token_freeze": bool(hub.HUB.cfg.get("token_freeze")),
                "frozen_count": len(getattr(hub.HUB, "_frozen_agents", {}) or {}),
            },
        }

    def get_messages(self, session_id):
        s = hub.HUB.sessions.get(session_id)
        if not s:
            return {"rev": 0, "messages": []}
        # 内存被 max_messages 裁过时，从 .md 回填完整对话（用户反馈「怎么只有这么多」）
        try:
            hub.HUB.hydrate_messages_from_file(s)
        except Exception:
            pass
        with s.lock:
            for m in s.messages:
                if isinstance(m, dict) and not m.get("mid"):
                    m["mid"] = uuid.uuid4().hex[:12]
            return {"rev": s.rev, "messages": list(s.messages),
                    "draft": getattr(s, "draft_text", ""),
                    "draft_images": getattr(s, "draft_images", []),
                    "draft_files": getattr(s, "draft_files", [])}

    # 每个对话一份「已定型气泡」缓存（bubbleId → step），跨拍复用；Api() 到处现 new，
    # 所以挂模块级而不是实例（同 _FACTS 的理由）
    _TURN_CACHE = {}
    _TURN_CACHE_MAX_CONVS = 64

    def get_live_turn(self, session_id, max_steps=None):
        """这个 tab 的 agent 此刻在 Cursor 里干到哪一步了（思考 / 工具调用 / 正文）。

        rxyy 09-03「看 bajie 是怎么做的，把rxyy MCP 也改成这样」：bajie 的 ReasoningTimeline
        靠注进 workbench 的钩子推事件；rxyy MCP 走读库（cursor_turns.read_turn），不改
        Cursor 任何文件。只给当前选中的 tab 按需读（UI 生成中 0.6s 一拍），一拍 40 条
        气泡实测 3~15ms，不进 get_state（那个是零 I/O 的纯读快照）。

        返回 {"ok", "turn": {...} | None, "why"}；没绑对话 / 库读不到时 ok=False 带原因，
        前端据此静默不画。"""
        s = hub.HUB.sessions.get(session_id)
        if not s:
            return {"ok": False, "why": "会话不存在"}
        if hub.runtime_adapter.is_native(s):
            if hub.runtime_adapter.kind(s) == "codex":
                import codex_desktop
                codex_desktop.watch(getattr(s, "native_thread_id", ""))
            turn = hub.runtime_adapter.read_native_turn(s, max_steps=max_steps or 100)
            if turn is None:
                why = ("ChatGPT 云端会话通过消息和进度上报显示状态"
                       if hub.runtime_adapter.kind(s) == "chatgpt" else
                       "尚未绑定可读取的 Codex 任务；首次调用请带 runtime 和 thread_id")
                return {"ok": False, "why": why, "runtime_kind": hub.runtime_adapter.kind(s)}
            return {"ok": True, "turn": turn, "now": time.time()}
        uid = getattr(s, "cursor_uuid", None)
        if not uid:
            return {"ok": False, "why": "还没定位到这个 tab 对应的 Cursor 对话"}
        if hub.cursor_turns is None:
            return {"ok": False, "why": "cursor_turns 模块不可用"}
        cache = self._TURN_CACHE.get(uid)
        if cache is None:
            if len(self._TURN_CACHE) >= self._TURN_CACHE_MAX_CONVS:
                self._TURN_CACHE.pop(next(iter(self._TURN_CACHE)), None)
            cache = self._TURN_CACHE[uid] = {}
        try:
            steps = int(max_steps or 0) or hub.cursor_turns.DEFAULT_MAX_STEPS
        except (TypeError, ValueError):
            steps = hub.cursor_turns.DEFAULT_MAX_STEPS
        turn = hub.cursor_turns.read_turn(uid, max_steps=max(5, min(steps, 2000)), cache=cache)
        if hub.wbhook is not None:
            try:
                hub.wbhook.watch(uid, True)
                snap = hub.wbhook.latest_turn(uid)
            except Exception:
                snap = None
            if snap:
                turn = hub.wbhook.overlay_turn(turn, snap)
        if turn is None:
            return {"ok": False, "why": "Cursor 库里读不到这个对话（可能已被回收）"}
        return {"ok": True, "turn": turn, "now": time.time()}

    def answer_native_question(self, session_id, thread_id, turn_id, request_id, answers):
        """Reply only to a live question belonging to this bound desktop task."""
        s = hub.HUB.sessions.get(session_id)
        if not s or hub.runtime_adapter.kind(s) != "codex":
            return {"ok": False, "error": "不是已绑定的 Codex 桌面任务"}
        with s.lock:
            if not thread_id or thread_id != getattr(s, "native_thread_id", ""):
                return {"ok": False, "error": "任务绑定已变化，请刷新问题"}
        import codex_desktop
        return codex_desktop.answer(thread_id, turn_id, request_id, answers)

    def send_native_text(self, session_id, thread_id, turn_id, text, delivery_id,
                         images=None, files=None, selected=None, who=None):
        """Send text and staged uploads to the exact Codex task once.

        A live MCP waiter must be answered through ``send_reply`` by the UI.  Once a
        native wait has timed out/detached, however, leaving the reply in
        ``buffered_reply`` cannot wake a completed Codex turn.  Native steer/start is
        then the delivery path; after Codex accepts it we retire that old pickup slot
        so a later empty zhi cannot consume the same user message a second time.
        """
        s = hub.HUB.sessions.get(session_id)
        if not s or hub.runtime_adapter.kind(s) != "codex":
            return {"ok": False, "error": "不是已绑定的 Codex 桌面任务"}
        if selected:
            return {"ok": False, "error": "请通过当前问题卡回答选项，再发送消息或附件"}
        if not isinstance(text, str):
            return {"ok": False, "error": "请输入文字"}
        text = text.strip()
        if not text and not images and not files:
            return {"ok": False, "error": "请输入文字或选择附件"}
        if len(text) > 12000:
            return {"ok": False, "error": "文字最多 12000 字"}
        resolved = False
        with s.lock:
            if not thread_id or thread_id != getattr(s, "native_thread_id", ""):
                return {"ok": False, "error": "任务绑定已变化，请刷新后再发送"}
            pending = s.pending
            if pending and not (getattr(s, "detached", False) or getattr(s, "wait_deferred", False)):
                return {"ok": False, "error": "当前仍在等待 MCP 回复，请刷新后通过当前等待发送"}
            pending_id = ((pending or {}).get("id")
                          if pending and (getattr(s, "detached", False)
                                          or getattr(s, "wait_deferred", False))
                          else None)
            buffered = getattr(s, "buffered_reply", None) if pending_id else None
            if isinstance(buffered, dict):
                images = list(buffered.get("images") or []) + list(images or [])
                files = list(buffered.get("files") or []) + list(files or [])
                prior = str(buffered.get("user_input") or "").strip()
                choices = [str(x) for x in (buffered.get("selected_options") or []) if x]
                if choices:
                    prior = (prior + "\n" if prior else "") + "选择的选项: " + ", ".join(choices)
                if prior:
                    text = prior + ("\n\n[补充] " + text if text else "")
            if who:
                text = "[{}] {}".format(str(who).strip()[:16], text)
            # detached 回复的读取、原生投递和旧领取位退休必须串行。若在原生 IPC
            # 期间释放锁，另一端可把新回复合进同一 buffered_reply；成功回执随后
            # 只核 pending.id 就会把那条刚接收的新回复整槽清掉。
            import codex_desktop
            if images or files:
                import native_uploads
                root = Path(hub.HUB.STATE_PATH).parent / "native-attachments"
                try:
                    uploads = native_uploads.prepare(root, thread_id, delivery_id, images, files)
                    if not native_uploads.reserve(root, thread_id, delivery_id):
                        return {"ok": False, "delivery_unknown": True,
                                "error": "此附件消息已提交或结果待核对，请回原任务查看；未重复发送"}
                except (ValueError, OSError, TypeError) as exc:
                    return {"ok": False, "error": str(exc)[:200]}
                try:
                    result = codex_desktop.send_text(thread_id, turn_id, text, delivery_id, uploads)
                except Exception:
                    result = {"ok": False, "delivery_unknown": True,
                              "error": "附件消息回执未确认，请在原任务核对；未自动重发"}
                try:
                    native_uploads.settle(root, thread_id, delivery_id, result)
                except OSError:
                    # The pre-dispatch receipt still prevents another delivery.
                    hub.log_event("Codex 附件回执暂未落盘，已保留投递锁")
            else:
                result = codex_desktop.send_text(thread_id, turn_id, text, delivery_id)
            if result.get("ok") and pending_id:
                if ((s.pending or {}).get("id") == pending_id
                        and (getattr(s, "detached", False)
                             or getattr(s, "wait_deferred", False))):
                    s.pending = None
                    s.buffered_reply = None
                    s.wait_deferred = False
                    s.detached = False
                    s.detached_since = None
                    s.processing_since = time.time()
                    s.last_reply_ts = time.time()
                    s.rev += 1
                    resolved = True
        if resolved:
            result = dict(result)
            result["resolved_deferred_wait"] = True
            hub.log_event("Codex 原生消息已接续当前任务 conv={} thread={}".format(
                s.conv_key, thread_id))
            hub.HUB.notify()
        return result

    @staticmethod
    def _native_resume_pending_reason(s):
        pending = getattr(s, "pending", None)
        if pending is None:
            return ""
        if getattr(s, "buffered_reply", None):
            return "已有回复等待领取，请先通过正常输入发送后再继续原任务"
        if (pending.get("options") or pending.get("card")
                or not (getattr(s, "detached", False)
                        or getattr(s, "wait_deferred", False))):
            return "当前仍有待回答问题，请先处理后再继续原任务"
        # An expired plain progress report is not an unanswered decision. Native
        # turn status is still checked by the owner before dispatch.
        return ""

    @staticmethod
    def _native_resume_reason(s, native_view):
        reason = Api._native_resume_pending_reason(s)
        if reason:
            return reason
        if Api._native_resume_lock(s, str(getattr(s, "native_thread_id", "") or ""),
                                   str(native_view.get("turn_id") or "")):
            return "该原轮次已继续或结果待核对，请回 Codex 查看"
        if any(q.get("status") == "pending" for q in
               native_view.get("native_questions") or []):
            return "Codex 原任务仍有待回答问题，请先处理"
        if native_view and not native_view.get("terminal"):
            return "Codex 原任务当前仍在运行，无需继续"
        return ""

    @staticmethod
    def _native_resume_lock(s, thread_id, turn_id):
        for item in reversed(getattr(s, "native_resume_ledger", []) or []):
            if (item.get("thread_id") == thread_id
                    and item.get("source_turn_id") == turn_id):
                return item
        return None

    @staticmethod
    def _persisted_resume_entry(session_id, delivery_id):
        """Confirm the pre-dispatch ledger reached the atomic session snapshot."""
        path = getattr(hub.HUB, "STATE_PATH", None)
        if not path:
            return False
        try:
            rows = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return False
        return any(
            row.get("id") == session_id
            and any(item.get("delivery_id") == delivery_id
                    for item in row.get("native_resume_ledger") or [])
            for row in rows if isinstance(row, dict))

    def resume_native_task(self, session_id, thread_id):
        """Open and continue only this session's bound Codex task on its current turn."""
        s = hub.HUB.sessions.get(session_id)
        if not s or hub.runtime_adapter.kind(s) != "codex":
            return {"ok": False, "error": "不是已绑定的 Codex 桌面任务"}
        bound = hub.runtime_adapter.valid_thread_id(
            getattr(s, "native_thread_id", ""))
        if not bound or thread_id != bound:
            return {"ok": False, "error": "Codex 原任务未绑定或绑定已变化，请刷新"}
        with s.lock:
            reason = self._native_resume_pending_reason(s)
            if reason:
                return {"ok": False, "error": reason}

        opened = self.open_codex_link("codex://threads/" + bound)
        if not opened.get("ok"):
            return {"ok": False, "opened": False,
                    "error": str(opened.get("error") or "无法打开 Codex 原任务")[:200]}
        delivery_id = str(uuid.uuid4())

        def reserve(source_turn_id, reserved_delivery_id):
            now = time.time()
            with s.lock:
                if getattr(s, "native_thread_id", "") != bound:
                    return {"ok": False, "error": "Codex 原任务绑定已变化；未发送"}
                reason = self._native_resume_pending_reason(s)
                if reason:
                    return {"ok": False, "error": reason}
                prior = self._native_resume_lock(s, bound, source_turn_id)
                if prior:
                    state = str(prior.get("state") or "unknown")
                    return {"ok": False, "delivery_unknown": state != "native_started",
                            "resume_locked": True, "source_turn_id": source_turn_id,
                            "error": "该原轮次已继续或结果待核对，请回 Codex 查看；未重复发送"}
                ledger = list(getattr(s, "native_resume_ledger", []) or [])
                ledger.append({"thread_id": bound, "source_turn_id": source_turn_id,
                               "delivery_id": reserved_delivery_id,
                               "retire_pending_id": (s.pending or {}).get("id"),
                               "state": "submitting", "at": now})
                s.native_resume_ledger = ledger[-8:]
                s.rev += 1
            hub.HUB.save_state()
            if not self._persisted_resume_entry(s.id, reserved_delivery_id):
                return {"ok": False, "error": "未能持久化继续记录；为避免重复投递，本次未发送"}
            return {"ok": True}

        def settle(source_turn_id, settled_delivery_id, state, new_turn_id):
            with s.lock:
                for item in reversed(getattr(s, "native_resume_ledger", []) or []):
                    if item.get("delivery_id") == settled_delivery_id:
                        item["state"] = state
                        item["updated_at"] = time.time()
                        if new_turn_id:
                            item["new_turn_id"] = new_turn_id
                        if (state == "native_started" and item.get("retire_pending_id")
                                and (s.pending or {}).get("id") == item["retire_pending_id"]
                                and not self._native_resume_pending_reason(s)):
                            s.pending = None
                            s.wait_deferred = False
                            s.detached = False
                            s.detached_since = None
                            s.processing_since = time.time()
                        break
                s.rev += 1
            hub.HUB.save_state()
            hub.HUB.notify()

        import codex_desktop
        result = codex_desktop.resume_thread(
            bound, "", delivery_id, reserve, settle)
        result = dict(result or {})
        result.update(opened=True, thread_id=bound, delivery_id=delivery_id)
        if result.get("ok"):
            hub.log_event("Codex 原任务已主动继续 conv={} thread={} source_turn={}".format(
                s.conv_key, bound, result.get("source_turn_id") or ""))
        return result

    def wbhook_beat(self, info=None):
        if hub.wbhook is None:
            return {"ok": False, "error": "wbhook 模块不可用"}
        return hub.wbhook.beat(info)

    def wbhook_poll(self):
        if hub.wbhook is None:
            return {"ok": False, "cmds": [], "watch": []}
        return hub.wbhook.poll_payload()

    def wbhook_ack(self, req_id, result=None):
        if hub.wbhook is None:
            return {"ok": False}
        return hub.wbhook.ack(req_id, result)

    def wbhook_ingest(self, snaps=None):
        if hub.wbhook is None:
            return {"ok": False}
        return hub.wbhook.ingest(snaps)

    def wbhook_status(self):
        if hub.wbhook is None:
            return {"ok": False, "installed": False, "alive": False}
        st = hub.wbhook.status()
        st["alive"] = hub.wbhook.hook_alive()
        return st

    def wbhook_install(self):
        if hub.wbhook is None:
            return {"ok": False, "error": "wbhook 模块不可用"}
        port = int((hub.HUB.cfg or {}).get("gateway_port", 38777) or 38777)
        return hub.wbhook.install(port=port)

    def wbhook_uninstall(self):
        if hub.wbhook is None:
            return {"ok": False, "error": "wbhook 模块不可用"}
        return hub.wbhook.uninstall()

    def wbhook_answer(self, session_id, answers=None):
        """控制台代答 Cursor AskQuestion。桥没挂上就老实说，别假装发出去了。"""
        if hub.wbhook is None or not hub.wbhook.hook_alive():
            return {"ok": False, "error": "桥没挂上。装完要 Reload 一次 Cursor 窗口，或回 Cursor 里点。"}
        s = hub.HUB.sessions.get(session_id)
        if not s:
            return {"ok": False, "error": "会话不存在"}
        uid = getattr(s, "cursor_uuid", None)
        if not uid:
            return {"ok": False, "error": "还没定位到这个 tab 对应的 Cursor 对话"}
        req = hub.wbhook.enqueue("answer", composerId=uid, answers=answers or {})
        deadline = time.time() + 2.2
        while time.time() < deadline:
            with hub.wbhook._LOCK:
                if req in hub.wbhook._ACKS:
                    return hub.wbhook._ACKS.pop(req)
            time.sleep(0.05)
        return {"ok": False, "error": "桥没回。窗口可能还没 Reload。"}

    # 未发送草稿里图片+文件的持久化上限（base64 字符数）：超过则不落盘，
    # 避免 .sessions.json 被一张大图撑爆拖慢每 15s 的快照。约 8MB base64。
    DRAFT_MEDIA_CAP = 8 * 1024 * 1024

    def save_draft(self, session_id, text, images=None, files=None):
        """输入框草稿实时落到会话并随快照持久化：重启控制台不再丢正在打的字，
        以及未发送的粘贴图片/文件（Q3 修复；总量超上限则只保文字）。"""
        s = hub.HUB.sessions.get(session_id)
        if not s:
            return {"ok": False}
        s.draft_text = str(text or "")[:20000]
        images = images or []
        files = files or []
        total = sum(len(str(i.get("data") or "")) for i in images) \
            + sum(len(str(f.get("data") or "")) for f in files)
        if total <= self.DRAFT_MEDIA_CAP:
            s.draft_images = images
            s.draft_files = files
        else:
            s.draft_images = []
            s.draft_files = []
        return {"ok": True, "media_persisted": total <= self.DRAFT_MEDIA_CAP}

    # ---------- 资料库：把技能/规则从「每一轮都付」改成「派任务时给个索引」 ----------
    # 报到那一轮（开 pro → 卡 mcp → 切号 → 退款，扣费就在这段）用不上任何技能和项目
    # 规则，可它们全被 Cursor 塞进系统提示词，实测技能目录 2,664、工作区规则上万。
    # 移出常驻后由这里补上：首次派活时附索引（名称+一句话+路径），并要求 agent 把
    # 列出的每一份都 Read 完再开工——rxyy 2026-08-12 定：只省报到那一轮的 token，
    # 派活后不再省，保证效果（此前写「需要哪一份自己打开」，agent 按字面只读了必读
    # 那份，v19 母本链条断在半路）。
    @staticmethod
    def _library_index_path():
        """资料库跟 rxyy tools 的业务数据同一个 data\\ 目录（见 rxyy_data_dirs）。

        读不到只是索引悄悄不下发，比配置丢失更难发现，所以候选路径一档都不能少。
        """
        for d in hub.rxyy_data_dirs():
            if (d / "library" / "index.json").is_file():
                return d / "library" / "index.json"
        return None

    _DIGEST_FILE_CAP = 200_000
    # 与 user_prompt（v19 母本）重复，工作区里那份 30-v19 不再附第二遍
    _DIGEST_SKIP_STEMS = frozenset({"30-v19-protocol"})

    @staticmethod
    def _read_digest_file(path):
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""
        cap = Api._DIGEST_FILE_CAP
        if len(raw) > cap:
            return raw[:cap] + "\n…(截断)"
        return raw

    @classmethod
    def _workspace_rule_files(cls, cwd):
        """工作区规则全文来源：磁盘上的 .mdc / .mdc.off / AGENTS.md(.off)。

        报到为省 token 把指针改成 .off 后，index.json 经常漏登记本仓；
        开干后要「和正常开局一样」，必须现读工作区，不能只信资料库目录。
        """
        root = Path(cwd or "")
        if not root.is_dir():
            return []
        found = []
        rules_dir = root / ".cursor" / "rules"
        if rules_dir.is_dir():
            try:
                names = list(rules_dir.iterdir())
            except OSError:
                names = []
            for p in names:
                if not p.is_file():
                    continue
                name = p.name
                if name.endswith(".mdc.off"):
                    stem = name[:-8]
                elif name.endswith(".mdc"):
                    stem = name[:-4]
                else:
                    continue
                if stem in cls._DIGEST_SKIP_STEMS:
                    continue
                found.append((stem, p))
        for ag in (root / "AGENTS.md", root / "AGENTS.md.off"):
            if ag.is_file():
                found.append(("AGENTS.md", ag))
                break
        by_stem = {}
        for stem, p in found:
            prev = by_stem.get(stem)
            # 同时有 .mdc 与 .mdc.off 时用未关闭的那份
            if prev is None or (str(prev).endswith(".off") and not str(p).endswith(".off")):
                by_stem[stem] = p
        return [(k, by_stem[k]) for k in sorted(by_stem)]

    @classmethod
    def _boardctl_cmd(cls, cwd):
        here = Path(cwd or "")
        for cand in (here / "scripts" / "boardctl.py",
                     here.parent / "cursor工作流" / "scripts" / "boardctl.py"):
            if cand.is_file():
                return "python " + str(cand)
        return "python scripts/boardctl.py"

    # 同一个接手方短时间内连收几张单时，规则全文只发第一张（见 _digest_already_sent）
    _DIGEST_REPEAT_WINDOW = 6 * 3600
    _digest_sent_at = {}

    @classmethod
    def _digest_already_sent(cls, target_id, cwd):
        """这个接手方最近是不是已经收过同一个工作区的规则全文了。

        规则全文按工作区取，同一工作区连派几张单，第二张之后再发就是原样重复。
        命中即返回 True，并把时间戳续上；窗口外的过期条目顺手清掉，别无限长。
        """
        key = (str(target_id or ""), str(Path(cwd or "")).rstrip("\\/").lower())
        now = time.time()
        for k, ts in list(cls._digest_sent_at.items()):
            if now - ts > cls._DIGEST_REPEAT_WINDOW:
                cls._digest_sent_at.pop(k, None)
        hit = key in cls._digest_sent_at
        cls._digest_sent_at[key] = now
        return hit

    @classmethod
    def library_digest(cls, cwd, tab_name="", repeat=False):
        """开干后恢复正常开局：用户规则 + 本仓指针全文，技能仍给目录。

        tab_name 只用来把侧栏改名指令写成字面标题——控制台派活时本来就知道这个
        tab 叫什么，让 agent 自己去凑「当前 task_name」是白让它多想一步。

        报到那一轮不调用这里。点「开始任务」/第一次派活附一次全文，
        不再只给路径让模型自己决定读不读（08-12 写过「不再省」，但索引仍被跳过）。

        repeat=True：这个接手方本轮已经收过同一工作区的全文了，只留「侧栏改名」
        那一块——它带着本单自己的标题、每单都不一样，砍了接手方就不知道该改成什么。
        其余（用户级规则 / 项目规则 / 看板 / 技能目录）逐字重复，换成一行指路。
        0901 实测：六张单叠给同一个 agent 共 5097 行，其中约 4700 行就是这几块
        重复六遍，接手方光读提示词就烧掉一大截上下文，真正的任务反而埋在最后。
        """
        p = cls._library_index_path()
        idx = {}
        if p:
            try:
                idx = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                idx = {}
        blocks = []
        if repeat:
            blocks.append(
                "【资料已在本轮前一张接手单里给过 · 不再重复】\n"
                "用户级规则全文、本项目规则全文、开干先看板、技能目录都与那一张逐字相同，"
                "按那份遵守即可，别因为这张单没带就当它们不存在。\n"
                "下面只留这张单自己的侧栏标题——每张单标题不同，这一块不能省。")
        up = idx.get("user_prompt") or {}
        up_path = up.get("path") if isinstance(up, dict) else ""
        if up_path and not repeat:
            body = cls._read_digest_file(up_path)
            if body:
                blocks.append("【用户级规则 · 必读必守 · 本对话只发这一次】\n"
                              "报到后已恢复为正常开局，下面是全文，按全文遵守：\n\n" + body)
            else:
                up_desc = (up.get("desc") or "用户级行为准则")[:80]
                blocks.append("【用户级规则 · 必读必守 · 本对话只发这一次】\n"
                              "先 Read 下面这份并完整遵守（%s）：\n· %s" % (up_desc, up_path))
        rule_parts = []
        seen_paths = set()
        for _stem, path in (() if repeat else cls._workspace_rule_files(cwd)):
            body = cls._read_digest_file(path)
            if not body:
                continue
            seen_paths.add(str(path.resolve()) if path.exists() else str(path))
            rule_parts.append("### %s\n%s" % (path.name, body))
        here = str(Path(cwd or "")).rstrip("\\/").lower()
        proj = Path(cwd or "").name.lower()
        for r in (() if repeat else (idx.get("rules") or [])):
            root = str(r.get("ws_root") or "").rstrip("\\/").lower()
            if root:
                if here and root != here:
                    continue
            elif proj and (r.get("project") or "").lower() != proj:
                continue
            rp = Path(r.get("path") or "")
            key = str(rp.resolve()) if rp.exists() else str(rp)
            if not rp.is_file() or key in seen_paths:
                continue
            stem = rp.name[:-8] if rp.name.endswith(".mdc.off") else rp.stem
            if stem in cls._DIGEST_SKIP_STEMS:
                continue
            body = cls._read_digest_file(rp)
            if not body:
                continue
            seen_paths.add(key)
            rule_parts.append("### %s\n%s" % (rp.name, body))
        if rule_parts:
            blocks.append("【本项目规则 · 全文 · 本对话只发这一次】\n"
                          "含为省报到 token 改成 .off 的指针/协作规则。按全文遵守"
                          "（含 KG 写入、服务器指针）。\n\n" + "\n\n".join(rule_parts))
        if not repeat:
            blocks.append(
                "【开干先看板 · 续接文档】\n"
                "写代码之前必须先列板。看板就是续接文档：卡的标题/描述/评论 = 做到哪了，"
                "不要另找 handoff 才开工。\n"
                "1. `%s list`（能确定项目名则加 --project 业务名或仓库名）\n"
                "2. 有相关待领/处理中的卡：claim 后再干；把卡上评论当交接\n"
                "3. 没有：create --title 当前任务 --project 业务名或仓库名 "
                "--desc 目标与验收\n"
                "4. 进展 comment；交付 deliver 进待验收\n"
                "thread-id 用本对话 conversation_id。提到才用看板 = 违反本条。"
                % cls._boardctl_cmd(cwd))
        title = (tab_name or "").strip() or "<当前 task_name>"
        blocks.append(
            "【Cursor 侧栏标题 · 实时跟随】\n"
            "用户要 Cursor 聊天列表跟着 task_name 立刻变，一排不能全叫 "
            "Persistent plus zhi report。写 sqlite 只改落盘，正在开着的窗口"
            "读的是内存，Reload Window 会断连，禁止让用户重载。\n"
            "**现在就改**：CallMcpTool server=cursor-app-control "
            "toolName=rename_chat arguments={\"title\":\"%s\"}\n"
            "· rename_chat 是 Cursor 内置工具，只有 IDE 主对话的工具表里有它；"
            "子代理／后台壳看不到，调不动也不会报错，只会静默跳过——"
            "08-27 侧栏一整屏「Persistent plus zhi report」就是这么来的。"
            "**所以要把这件事整体委派给子代理的话，先在派出去的同一轮里"
            "并行调这一次改名，别把它一起委派下去。**\n"
            "· 之后 task_name 再变（换真名／换项目），同样在那一轮并行再调一次。\n"
            "· zhi 会阻塞，不能等它返回再改名。\n"
            "· 工具自述写 never rename proactively，但用户已明确要求跟随 "
            "task_name，这是用户指令。工具表里确实没有就跳过，不要谎称已改名。"
            % title)
        skill_lines = []
        for s in (() if repeat else (idx.get("skills") or [])):
            skill_lines.append("· 技能 %s —— %s\n  %s" % (
                s["name"], (s.get("desc") or "")[:60], s["path"]))
        if skill_lines:
            blocks.append("【技能目录 · 命中再 Read SKILL.md】\n"
                          "与正常开局一样：先记住触发条件，命中再打开对应技能。\n"
                          + "\n".join(skill_lines))
        if not blocks:
            return ""
        return "\n\n".join(blocks) + "\n\n"

    def _with_library_digest(self, s, user_input, selected=None):
        """给开干后的第一条回复补全文资料，并原子领取一次性发送标记。

        调用方在发送失败时必须把返回的 ``claimed`` 传给
        ``_release_library_claim``，否则下一次重试会误认为资料已经送达。
        只点「开始任务」、没有打字，也要附上——否则规则永远进不了这轮上下文。
        """
        if not hub.HUB.cfg.get("library_autosend", True):
            return user_input, False
        starting = any(str(x).strip() == "开始任务" for x in (selected or []))
        if not user_input and not starting:
            return user_input, False
        # 接手提示词自己已经嵌了资料索引（新 agent 靠复制粘贴拿到，不走 send_reply）。
        # 再 prepend 一次会把同一份清单贴两遍。
        if user_input and "【资料索引 · 接手后必读】" in user_input:
            return user_input, False
        with s.lock:
            if getattr(s, "library_sent", False):
                return user_input, False
            try:
                digest = self.library_digest(getattr(s, "cwd", ""),
                                             getattr(s, "name", ""))
            except Exception:  # noqa: BLE001
                digest = ""
            if not digest:
                return user_input, False
            s.library_sent = True
            return digest + (user_input or ""), True

    @staticmethod
    def _release_library_claim(s, claimed):
        if not claimed:
            return
        with s.lock:
            s.library_sent = False

    _MEMORY_PIN = (
        "不能自动收工", "只有我手动", "禁止自动收工",
        "不要自动收工", "不能是简单粗暴",
    )

    @staticmethod
    def _plain_session_msg(msg):
        raw = str((msg or {}).get("html") or (msg or {}).get("text") or "")
        text = re.sub(r"<[^>]+>", " ", raw)
        text = html_mod.unescape(text).replace("\xa0", " ")
        return " ".join(text.split())

    def _user_fact_from_msg(self, msg, max_len=400):
        """一条用户气泡 → 回灌用的一行：点过的选项必须留下，不能只留打字。"""
        selected = []
        for item in (msg.get("selected") or []):
            text = str(item or "").strip()
            if text and text not in ("开始任务", "结束"):
                selected.append(text)
        body = self._plain_session_msg(msg)
        parts = []
        if selected:
            parts.append("选择: " + "、".join(selected))
        if body:
            parts.append(body)
        return " ".join(parts)[:max_len]

    def _facts_from_messages(self, s, limit=8):
        facts = []
        for msg in reversed(list(getattr(s, "messages", None) or [])):
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            text = self._user_fact_from_msg(msg)
            if not text:
                continue
            facts.append(text)
            if len(facts) >= limit:
                break
        facts.reverse()
        return facts

    def _facts_from_chat_log(self, path, limit=8):
        """聊天记录 md 才是完整记忆：控制台气泡常常只有打字、丢掉「选择:」。"""
        if not path:
            return []
        try:
            raw = Path(path).read_text(encoding="utf-8-sig")
        except OSError:
            return []
        facts = []
        for chunk in re.split(r"(?=^## )", raw, flags=re.M):
            head = chunk.split("\n", 1)[0]
            if "用户" not in head:
                continue
            body = chunk.split("\n", 1)[1] if "\n" in chunk else ""
            kept = []
            for line in body.splitlines():
                piece = line.strip()
                if not piece or piece.startswith((">", "（附带", "（提供", "🖼")):
                    continue
                kept.append(piece)
            text = " ".join(kept)
            if text:
                facts.append(text[:400])
        return facts[-limit:]

    def _pin_constraints(self, facts):
        pins = []
        for fact in facts:
            if not any(key in fact for key in self._MEMORY_PIN):
                continue
            for piece in re.split(r"[。，,？?\n]", fact):
                piece = piece.strip(" 、")
                if piece.startswith("选择:"):
                    continue
                if any(key in piece for key in self._MEMORY_PIN) and piece not in pins:
                    pins.append(piece[:160])
        return pins[:4]

    def session_memory_digest(self, s, limit=8):
        """Cursor 压缩对话后回灌：用户最近决定 + 本会话聊天记录路径。

        不全文重发 library_digest（那份只该开干发一次）。优先摘聊天记录 md
        （含「选择:」和硬约束），气泡里没有选项时也能灌回去。模型仍应先
        Read 那份全文，不要靠被裁过的摘要猜。
        """
        path = getattr(s, "file_path", "") or ""
        facts = self._facts_from_chat_log(path, limit)
        if not facts:
            facts = self._facts_from_messages(s, limit)
        lines = [
            "【会话要点 · 压缩后回灌】",
            "任务：" + str(getattr(s, "name", "") or ""),
            "工作区：" + str(getattr(s, "cwd", "") or ""),
            "对话ID：" + str(getattr(s, "conv_key", "") or ""),
        ]
        if path:
            lines.append("聊天记录：" + path)
            lines.append("若本轮出现对话被总结/压缩，先 Read 上面这份补记忆，不要靠被裁过的摘要猜。")
        pins = self._pin_constraints(facts)
        if pins:
            lines.append("硬约束（未点结束不得收工）：")
            lines.extend("- " + item for item in pins)
        if facts:
            lines.append("用户最近说：")
            lines.extend("- " + item for item in facts)
        return "\n".join(lines) + "\n\n"

    def _with_session_memory(self, s, user_input, selected=None):
        """开干之后每次用户回复都回灌要点，扛 Cursor 压缩丢记忆。

        只加在发给模型的那一截：控制台气泡和聊天记录仍是用户原文，
        不会污染下一轮要点，也不会自动收工。
        """
        if not getattr(s, "library_sent", False):
            return user_input
        if any(str(x).strip() == "结束" for x in (selected or [])):
            return user_input
        text = user_input or ""
        if text.startswith("【会话要点 · 压缩后回灌】"):
            return user_input
        digest = (self.session_memory_digest(s) or "").strip()
        if not digest:
            return user_input
        s.memory_sent_ts = time.time()
        return digest + "\n\n" + (user_input or "")

    @staticmethod
    def _card_deadline(pending):
        card = (pending or {}).get("card") if isinstance(pending, dict) else None
        ad = (card or {}).get("autoDecide") or {}
        if not ad.get("enabled") or not pending.get("created"):
            return None
        try:
            return float(pending["created"]) + float(ad.get("timeoutSec") or 0)
        except (TypeError, ValueError):
            return None

    def answer_card(self, session_id, answers, mode="manual", who=None):
        """决策卡片的答复：把用户点的选项 / 自由输入拼成 agent 读得懂的文本，走 send_reply 老路。

        answers = {questionId: {"selected": [optionId…], "text": "自由输入"}}；
        mode = manual（人点的）| adopt_recommended（倒计时到点采纳推荐项）| delegate_system（交回 AI）。
        选中的 label 另走 selected_options，只认老路的 agent 也拿得到。
        who：手机分享页答的带上答题人（与 /api/reply 同口径），控制台自己答的不带。
        """
        s = hub.HUB.sessions.get(session_id)
        if not s:
            return {"ok": False, "error": "会话不存在"}
        card = (s.pending or {}).get("card") if s.pending else None
        if not card or hub.decision_card is None:
            return {"ok": False, "error": "当前提问没有决策卡片"}
        if mode not in ("manual", "adopt_recommended", "delegate_system"):
            mode = "manual"
        text, labels = hub.decision_card.decision_reply(card, answers or {}, mode)
        return self.send_reply(session_id, text, labels, [], False, who, [])

    def send_reply(self, session_id, text, selected, images, is_continue, who=None,
                   files=None, recallable=False):
        s = hub.HUB.sessions.get(session_id)
        if not s:
            return {"ok": False, "error": "会话不存在"}
        text = (text or "").strip()
        selected = list(selected or [])
        images = images or []
        files = files or []
        if is_continue:
            user_input = hub.HUB.cfg.get("continue_prompt", "请按照最佳实践继续")
            selected, images, files, source = [], [], [], "popup_continue"
        else:
            if not text and not selected and not images and not files:
                return {"ok": False, "error": "请输入内容、选择选项或添加图片/文件"}
            # 局域网同事的消息带上昵称，让 AI 知道提问人是谁
            user_input = (f"[{who}] {text}" if who and text else text) or None
            source = "popup_share" if who else "popup"
        # 普通人工发送先进入现有持久队列，给桌面/手机同一段撤回窗口。pending
        # 不在这里取走，5 秒后由 _state_tick -> _flush_queue 交付；「继续」、决策卡
        # 和内部转告不传 recallable，仍走下面的即时老路。
        if recallable and not is_continue:
            return self.queue_message(
                session_id, text, images, who=who, files=files,
                recallable=True, selected=selected)
        # 原子取走 pending：本地控制台与分享页同时回复时只允许一份送达 AI
        with s.lock:
            if not s.connected or not s.pending:
                return {"ok": False, "error": "当前没有等待回复的请求"}
            pending_taken = s.pending
            detached = s.detached
            s.pending = None
            # 队友转告/控制台回执排在用户后面（见 _may_take_the_reply_slot）：
            # 用户这一答把它们捎带出去。不捎带的话它们就永远压在队列里了——
            # 下一次 zhi 依然只留给用户，没人再来放它们出去。
            # 决策卡片还多一类：用户看到卡之前说的话被 _flush_queue 扣住没答卡
            # （答不了卡上的问题），此刻随答卡一起送出；补送件除外——它们是
            # 更早那次回复的重复，用户此刻的新回复就是过期判据
            now = time.time()
            tag_along = self._tag_along_for_reply(s, pending_taken, now)
            if tag_along:
                # 按条剔除而不是按 who 重算：扣住件是用户自己的消息，按 who 算它
                # 「够格占回复位」会被留在队列里——送出去一份、队列里还躺一份，
                # 下次提问再送一遍
                s.queued = [e for e in s.queued if e not in tag_along]
                qids = {e.get("id") for e in tag_along if e.get("id")}
                for m in s.messages:
                    if m.get("qid") in qids:
                        m["queued"] = False
        if tag_along:
            extra = "\n\n".join(
                "[{}] {}".format(e["who"], e["text"]) if e.get("who") else e["text"]
                for e in tag_along if e.get("text"))
            if extra:
                user_input = (user_input + "\n\n" + extra) if user_input else extra
            for e in tag_along:
                for option in e.get("selected") or []:
                    if option not in selected:
                        selected.append(option)
        library_claimed = False
        if not is_continue:
            user_input, library_claimed = self._with_library_digest(
                s, user_input, selected=selected)
            if not library_claimed:
                user_input = self._with_session_memory(
                    s, user_input, selected=selected)
        img_payload = hub.build_img_payload(images)
        file_payload = hub.build_file_payload(files)
        answer = {
            "user_input": user_input,
            "selected_options": selected,
            "images": img_payload,
            "files": file_payload,
            "source": source,
        }
        if detached:
            # 保活脱离期：此刻没有等待方，缓存回复，下次 zhi 续期重呼时立刻交付。
            # 已经存着一条（只发不等期间用户连回几条）就合并，不许覆盖
            with s.lock:
                s.buffered_reply = hub.session_core.merge_buffered_reply(
                    s.buffered_reply, answer)
                s.pending = pending_taken  # 提问与回复位保留到交付为止
        else:
            resp = {"type": "zhi_response", "id": pending_taken["id"], **answer}
            try:
                s.send(resp)
            except Exception as e:
                self._release_library_claim(s, library_claimed)
                with s.lock:
                    if s.pending is None:
                        s.pending = pending_taken  # 发送失败还回去，允许用户重试
                # 捎带的队友转告也得还回去：它们已经拼进这次失败的 user_input，
                # 用户重试时重新组稿不会再带上，不放回就无声蒸发（转告方那头
                # 收到的还是「✓ 已提交」）。走统一的 put_back：仍排在用户后面
                self.put_back_agent_mail(s, tag_along)
                return {"ok": False, "error": f"发送失败，连接可能已断开: {e}"}
        if self._may_take_the_reply_slot(who):
            # 点报到卡「开始任务/结束」或把接手提示词贴进报到 zhi = 拆信封，
            # 不是给这个壳派活。08-14 晚：标成 claimed 后原会话复活，壳收不掉。
            if not hub.session_core.is_checkin_envelope(
                    pending_taken, selected, text if not is_continue else ""):
                hub.HUB._mark_claimed(s, "收过真实用户回复")
        disp = text if not is_continue else hub.HUB.cfg.get("continue_prompt")
        file_names = [f["name"] for f in file_payload]
        # 决策卡的答复：发给 agent 的原文带 <chijiu-decision …/> 机器标签，给人看的
        # 气泡换成「提问 N / 回答 N」答复卡、预览摘掉标签（09-07 rxyy：一行 JSON 糊在气泡里）
        disp_html = ""
        if disp:
            dc = getattr(hub, "decision_card", None)
            card_html = dc.reply_to_html(disp) if dc is not None and hasattr(dc, "reply_to_html") else None
            if card_html:
                disp_html = card_html
                disp = dc.strip_machine_tag(disp)
            else:
                disp_html = hub.render_markdown(disp, False)
        self_msg = {
            "role": "user",
            "ts": hub.now_hms(),
            "text": disp or "",
            "html": disp_html,
            "selected": selected,
            "img_count": len(img_payload),
            "img_files": hub.HUB.save_msg_images(img_payload),
            "file_names": file_names,
            "is_continue": bool(is_continue),
            "who": who,
        }
        # 送达探针：Cursor 端用户打断工具调用时不发任何取消通知，这条回复可能被
        # 静默吞掉。记录内容，若通道死掉前 agent 再无任何动静（没收到的铁证），
        # 自动转入队列在下次 zhi 时补送。有实质内容才值得补（纯选项重点一下就行）。
        if not is_continue and (text or img_payload or file_payload):
            s.last_reply_probe = {
                "ts": time.time(), "text": text,
                "images": img_payload, "files": file_payload,
                "who": who, "msg_ref": self_msg,
                # 重问识别凭据（08-26 回复蒸发案）：回复被吞时 agent 会把同一道
                # 题原样再问一遍——正文+选项与这里一致即重问，hub 当场补送本条
                "question": str(pending_taken.get("message") or ""),
                "q_options": list(pending_taken.get("options") or []),
            }
        # 最近一次真实回复的时刻：过期补送判据（死会话处置③）——rescue 入队的
        # 补送若早于它，说明用户其后已在新轮次里说过话，那条补送不该再顶掉
        # agent 挂着的 zhi（08-13 两起实证：09:47 旧回复、14:08 旧图重放）
        s.last_reply_ts = time.time()
        s.processing_since = time.time()
        hub.HUB.add_message(s, self_msg)
        if not is_continue:
            tgt = hub.takeover_prompt_target(text)
            if tgt:
                s.handed_off_to = tgt
        if not is_continue and text:
            self._auto_label_on_dispatch(s, text, who=who)
        hub.HUB.log_user(s, disp or "", selected, len(img_payload), source, who=who,
                     file_names=file_names)
        if who:
            hub.HUB.notify()  # 同事回复时提醒本机
        return {"ok": True}

    def queue_message(self, session_id, text, images, who=None, files=None,
                      force=False, recallable=False, selected=None,
                      reply_to=None):
        """无 pending 时用户提前发送的消息进入队列，下次提问自动送出。
        断线重连宽限期 / IDE 活跃期也允许排队：队列本就等下次 zhi 才交付，
        通道短暂不在不影响入队（07-27 用户实测「点不了发送去排队」的补丁）。
        force=True：agent 转告投给已终止的 tab 时用——队列随快照持久化，
        等它复活/被接手后照样送达（用户在列表里看得见这个 tab，递话就该能进）。
        recallable=True：至少等 RECALL_DELAY_SECS，再由 state tick 交付；selected 与
        reply_to 随队列持久化，撤回和决策卡匹配都靠它们。"""
        s = hub.HUB.sessions.get(session_id)
        if not s:
            return {"ok": False, "error": "会话不存在"}
        now = time.time()
        revivable = bool(
            s.connected
            or (s.recon_deadline and now <= s.recon_deadline)
            or getattr(s, "ide_active_cache", False))
        if not revivable and not force:
            return {"ok": False, "error": "会话已终止（通道断开且无近期活动），无法排队"}
        text = (text or "").strip()
        selected = list(selected or [])
        images = images or []
        files = files or []
        if not text and not selected and not images and not files:
            return {"ok": False, "error": "请输入内容、选择选项或添加图片/文件"}
        # 竞态：若此刻 AI 恰好已就绪，用户的话直接作为正常回复发送；
        # 队友转告/控制台回执不许占这个位置（见 _may_take_the_reply_slot）
        if s.pending and self._may_take_the_reply_slot(who) and not recallable:
            return self.send_reply(session_id, text, selected, images, False,
                                   who=who, files=files)
        img_payload = hub.build_img_payload(images)
        file_payload = hub.build_file_payload(files)
        qid = uuid.uuid4().hex[:8]
        defer_until = (time.time() + self.RECALL_DELAY_SECS) if recallable else 0.0
        msg = {
            "role": "user", "ts": hub.now_hms(),
            "text": text or "",
            "html": hub.render_markdown(text, False) if text else "",
            "selected": selected, "img_count": len(img_payload),
            "img_files": hub.HUB.save_msg_images(img_payload),
            "file_names": [f["name"] for f in file_payload],
            "is_continue": False, "queued": True, "qid": qid,
            "who": who,
        }
        if recallable:
            msg["recall_until"] = defer_until
        with s.lock:
            pending_id = (s.pending or {}).get("id")
            s.queued.append({
                "id": qid, "text": text or None, "selected": selected,
                "images": img_payload, "files": file_payload, "msg": msg,
                "who": who, "defer_until": defer_until,
                "reply_to": reply_to if reply_to is not None else pending_id,
            })
        hub.HUB.add_message(s, msg)
        tgt = hub.takeover_prompt_target(text)
        if tgt:
            s.handed_off_to = tgt
        if self._may_take_the_reply_slot(who):
            # 排队里的接手提示词同样只是拆信封，别把壳钉成「认领了活」
            if not str(text or "").lstrip().startswith(hub.TAKEOVER_PROMPT_HEAD):
                hub.HUB._mark_claimed(s, "收过真实用户任务（排队）")
        if text:
            self._auto_label_on_dispatch(s, text, who=who)
        if recallable:
            self._schedule_recall_flush(session_id)
        return {"ok": True, "qid": qid}

    # 摘要里每条对话最多留这么多字；再长的说明也读不进摘要，读原文去
    SUMMARY_MSG_CLIP = 300
    SUMMARY_SELF_WORDS = ("自己", "我", "me", "self", "本会话", "own")

    def session_summary_text(self, target, requester=None, max_chars=2000):
        """ji(action="摘要")：把某个会话「干到哪了」压成一段 ≤ max_chars 的话。

        对标 BajieAsk 的 get_session_summary。接手 / 协作前想知道那个 tab 的进度，
        以前只有两条路：让用户复制几千行接手提示词，或者自己去翻对方的记录文件。
        这里按内存实况给：在线与否、自报状态、zt 轨迹、正等谁回话、最近几句对话、
        记录文件在哪。只读，不动任何状态。

        目标解析沿用转告那套（_resolve_relay_target：对话 ID 前缀 › 同项目队友
        名字 › 全局），留空或「自己」= 请求方本人。多义 / 找不到都回人话，不猜。
        """
        try:
            max_chars = max(200, min(int(max_chars or 2000), 8000))
        except (TypeError, ValueError):
            max_chars = 2000
        target = str(target or "").strip()
        low = target.lower()
        s = None
        if not target or low in self.SUMMARY_SELF_WORDS:
            if requester is None:
                return ("没找到你自己的会话：这次 ji 没带 conversation_id，或者它还没在"
                        "控制台登记。带上 conversation_id 再调一次。")
            s = requester
        elif requester is not None and (
                (re.fullmatch(r"[0-9a-f]{6,32}", low)
                 and (requester.conv_key or "").lower().startswith(low))
                or low in {(requester.name or "").lower(),
                           (self.session_label(requester) or "").lower()}):
            s = requester
        else:
            if requester is not None:
                scope = self._team_scope(requester)
                team = [x for x in self._team_sessions(scope["root"], project=scope["project"])
                        if x.id != requester.id]
                probe = requester
            else:
                with hub.HUB.lock:
                    team = list(hub.HUB.sessions.values())
                probe = SimpleNamespace(id=None, conv_key="", name="", id_history=[])
            try:
                targets, note = self._resolve_relay_target(probe, target, team)
            except Exception as exc:  # noqa: BLE001
                targets, note = None, "目标解析失败：{!r}".format(exc)
            if not targets:
                return (str(note or "没找到「{}」".format(target))
                        .replace("【转告失败】", "【摘要失败】").replace("转告", "摘要"))
            if len(targets) > 1:
                return "【摘要失败】「{}」对上了 {} 个会话，用对话 ID 前 8 位指名：{}".format(
                    target, len(targets), "、".join(
                        "{}（{}）".format(self.session_label(x) or x.name,
                                         (x.conv_key or "")[:8]) for x in targets[:6]))
            s = targets[0]

        now = time.time()
        conv8 = (s.conv_key or "")[:8]
        label = self.session_label(s) or s.name or "未命名"
        if s.connected:
            state = "在线"
        elif getattr(s, "archived", False):
            state = "已归档"
        else:
            state = "断开"
        live = getattr(s, "live_cache", None) or {}
        if not live:
            try:
                live = self._agent_liveness(s, now) or {}
            except Exception:  # noqa: BLE001
                live = {}
        live_label = str(live.get("label") or "")
        model = (getattr(s, "model_info", None) or {}).get("label") or ""
        head = "📄 会话摘要 · {}（{}）· {}{}{}".format(
            label, conv8 or "无ID", state,
            " · " + live_label if live_label and live_label != state else "",
            " · 模型 " + model if model else "")
        lines = ["· 工作区：{}".format(s.cwd or "未知")]
        try:
            scope = self._team_scope(s)
            if scope.get("name"):
                lines.append("· 项目组：{}".format(scope["name"]))
        except Exception:  # noqa: BLE001
            pass
        a_status, a_activity, zt_age = self._self_report(s, now)
        if a_status or a_activity:
            lines.append("· 自报状态：{}{}".format(
                a_status or "", " · " + a_activity if a_activity else ""))
        trail = [str(t) for t in (getattr(s, "zt_trail", None) or [])][-5:]
        if trail:
            lines.append("· 最近进度（zt，旧→新）：\n" + "\n".join("  - " + t for t in trail))
        pending = getattr(s, "pending", None)
        if isinstance(pending, dict):
            q = self._one_line(_html_text(pending.get("message") or ""), 100)
            opts = [str(o) for o in (pending.get("options") or []) if str(o).strip()]
            lines.append("· 正等用户回话：「{}」{}".format(
                q or "（无正文）", "  选项：" + " / ".join(opts[:4]) if opts else ""))
        try:
            n_queued = len([q for q in (getattr(s, "queued", None) or [])
                            if isinstance(q, dict)])
        except Exception:  # noqa: BLE001
            n_queued = 0
        if n_queued:
            lines.append("· 排队还没送到它手上的消息：{} 条".format(n_queued))
        tail = "· 记录文件：{}".format(s.file_path) if getattr(s, "file_path", "") else ""

        msgs = [m for m in list(getattr(s, "messages", None) or [])
                if isinstance(m, dict) and m.get("role") in ("user", "ai")]
        fixed = head + "\n" + "\n".join(lines) + ("\n" + tail if tail else "")
        budget = max_chars - len(fixed) - 24  # 留给「最近对话」标题行
        picked = []
        if msgs and budget > 40:
            # 从最新往前装，装满为止；至少给最后一句一个机会（截得狠一点）
            for m in reversed(msgs):
                who = "🧑" if m.get("role") == "user" else "🤖"
                body = _html_text(m.get("html") or m.get("text") or "")
                clip = min(self.SUMMARY_MSG_CLIP, max(40, budget - 12))
                if len(body) > clip:
                    body = body[:clip] + "…"
                line = "  {} {} {}".format(who, m.get("ts") or "", body)
                if len(line) + 1 > budget:
                    break
                picked.append(line)
                budget -= len(line) + 1
            picked.reverse()
        parts = [head] + lines
        if picked:
            parts.append("· 最近对话（{} 条，旧→新）：".format(len(picked)))
            parts.extend(picked)
        elif msgs:
            parts.append("· 最近对话：有 {} 条，篇幅不够没带上（content 传更大的字数）"
                         .format(len(msgs)))
        if tail:
            parts.append(tail)
        text = "\n".join(parts)
        if len(text) > max_chars:
            text = text[:max_chars - 1] + "…"
        return text

    def take_agent_mail(self, s):
        """取走队列里「排在用户后面」的机器消息，交给 zt 的返回值捎回去。

        取的正是 send_reply 里 tag_along 那一批（队友转告 / 黑板提醒 / 控制台回执），
        判定同用 _may_take_the_reply_slot，所以用户自己发的消息一条都不会被顺走——
        那个位置永远留给 zhi。区别只是不再非等到「用户下次回话」：zt 本来就每完成
        一个动作调一次，顺路把信带走，递话延迟从几十分钟降到几十秒。

        返回 (要发的文本, 摘下来的原始条目)。后者是给 put_back_agent_mail 用的：
        信摘下来了却没送到 agent 手上，两头就都不存在这条转告了。
        """
        with s.lock:
            mail = [e for e in s.queued
                    if not self._may_take_the_reply_slot(e.get("who"))]
            if not mail:
                return [], []
            s.queued = [e for e in s.queued
                        if self._may_take_the_reply_slot(e.get("who"))]
            qids = {e.get("id") for e in mail if e.get("id")}
            for m in s.messages:
                if m.get("qid") in qids:
                    m["queued"] = False
            s.rev += 1
        hub.HUB.notify_status_only()
        texts = ["[{}] {}".format(e["who"], e["text"]) if e.get("who") else e["text"]
                 for e in mail if e.get("text")]
        return texts, mail

    def put_back_agent_mail(self, s, entries):
        """信摘下来了却没送出去（通道正好断了）→ 原样放回队列，等下一趟。

        不放回的话这条转告就凭空消失了：队列里没有、agent 也没收到，而发送方
        那头拿到的是「✓ 已提交转告」。宁可迟到，不可蒸发。
        仍旧排在用户后面——放回期间用户可能已经发了话，那条的位置动不得。
        """
        entries = [e for e in (entries or []) if e]
        if not entries:
            return
        with s.lock:
            s.queued = list(s.queued) + entries
            qids = {e.get("id") for e in entries if e.get("id")}
            for m in s.messages:
                if m.get("qid") in qids:
                    m["queued"] = True
            s.rev += 1

    def _nudge_rename_if_standby(self, s):
        """tab 还叫「待命·xxx」却已经在干真活了 → 往它队列里塞一句硬提醒。

        纪律里写了「接到真活后第一次 zhi/zt 必须换 task_name」，但没有任何东西
        拦得住不换：08-07 控制台上并排三个「待命·cursor工作流」，用户和队友都分不清
        谁在干嘛，转告点名也点不准。走队列是为了复用现成投递（zt 顺路取信 / 用户
        回话时捎带），并且用户在 tab 里看得见这条提醒。一个名字只提醒一次。
        """
        try:
            name = str(getattr(s, "name", "") or "")
            if getattr(s, "agent_named", False):
                return  # 已经自报过真名，别再啰嗦
            # 两类要催：还挂着报到壳名的，以及顶着「派活第一句截 24 字」那个
            # 自动占位的——后者用户看到的就是自己第一句话，跟没名字一样
            if not (name.startswith("待命") or self._assign_is_auto(s)):
                return
            # 真聊过的句子（不含 zt 上报、不含队友转告）≤3 就是报到那一两句，
            # 说明真在待命等派活，这时候催改名是冤枉它
            if not hub.HUB._did_real_work(s):
                return
            if getattr(s, "_rename_nudged", "") == name:
                return
            s._rename_nudged = name
            self.queue_message(s.id, (
                "⚠ 控制台纪律：你的 tab 现在显示为「{}」，这不是你起的名字"
                "（要么还是报到壳名，要么是系统拿派活第一句截了 24 个字凑的）。"
                "下一次 zhi/zt 请把 task_name 换成「**项目·功能**」两段式，"
                "如「rxyy tools·换装收尾」「智慧云广播·日报取数」。"
                "项目要写你**真正在开发的那个项目**，不是工作区目录名"
                "——同一个目录下常并行着好几摊互不相干的活。"
                "项目那半还会被当成业务线用来收窄广播，写对了大家都少挨吵。"
                .format(self._one_line(self.session_label(s) or name, 30))
            ), [], who="控制台")
        except Exception as e:  # noqa: BLE001
            hub.log_event("命名提醒异常（已忽略）: {}".format(e))

    def _nudge_cursor_rename(self, s, stale_title=""):
        """Cursor 侧栏还顶着自动标题、控制台写盘又被内存刷回来盖掉 → 催它自己改。

        为什么非得让 agent 自己动手（拆了 Cursor 主包才敢这么写）：
        · `renameComposer(id,name)` 内部只有一句 `updateComposerDataSetStore(h,
          s=>s("name",name))`——**内存是真身，磁盘只是它刷出去的影子**。对话还开着
          时我们写进库的名字下一次刷盘就被盖掉，一拍一拍地拉锯（08-25 22:26 实测
          写完 90 秒被盖回两个）。这个函数被调用本身就是那场拉锯的证据。
        · `handleRenameChat` 的目标完全取自调用方自己的 conversationId，拿不到就
          直接回「could not identify the calling conversation」——所以没有任何一个
          agent 能替别的 tab 改名，一个对话只能改自己那一个。
        · Cursor 进程没开 `--remote-debugging-port`（实测命令行只有 exe 一项），
          从外面注入 JS 那条路要重启才开得了，不能用。

        走队列不走回复位：zt 顺路取信就能拿到，不占用户的回复槽、不惊动待命壳
        （同 _nudge_rename_if_standby）。一个名字只催一次——agent 一旦真改了，
        Cursor 的 shouldRenameComposer 最后一档是 `!n.name`，从此不会再自动起名。
        """
        try:
            name = str(getattr(s, "name", "") or "").strip()
            if not name or getattr(s, "_title_nudged", "") == name:
                return
            s._title_nudged = name
            self.queue_message(s.id, (
                "⚠ Cursor 侧栏名字没跟上：这个对话在聊天列表里还显示为「{}」，"
                "控制台已经把「{}」写进库了，但你这个对话开着，Cursor 会拿内存那份"
                "刷回磁盘把它盖掉——只有你自己调一次改名才改得动内存。\n"
                "**并行调这一次**：CallMcpTool server=cursor-app-control "
                "toolName=rename_chat arguments={{\"title\":\"{}\"}}\n"
                "· 它是 Cursor 内置工具，只有 IDE 主对话的工具表里有；"
                "**你若是子代理（工具表里没有它），不要静默跳过**——把上面这行"
                "原样写进你交回主对话的总结里，让主对话调，这是它一整屏"
                "「Persistent plus zhi report」的唯一解。\n"
                "· 工具自述写 never rename proactively，但用户已明确要求跟随 "
                "task_name，这是用户指令。真调不到就照实说，别谎称已改名。"
                .format(self._one_line(stale_title or "自动标题", 30), name, name)
            ), [], who="控制台")
            hub.log_event("催侧栏改名 tab={} 库里还是「{}」".format(
                name, self._one_line(stale_title, 30)))
        except Exception as e:  # noqa: BLE001
            hub.log_event("侧栏改名提醒异常（已忽略）: {}".format(e))

    def edit_message(self, session_id, mid, text):
        """改控制台里已经画出来的一条气泡（对齐 Bajie /api/edit-message）。

        只改网页历史：agent 已经读走的那一轮不会被改写。排队中的条请走撤回。"""
        s = hub.HUB.sessions.get(session_id)
        if not s:
            return {"ok": False, "error": "会话不存在"}
        mid = str(mid or "")
        text = str(text or "")
        if not mid:
            return {"ok": False, "error": "消息不存在"}
        if not text.strip():
            return {"ok": False, "error": "内容不能为空"}
        if len(text) > 20000:
            text = text[:20000]
        with s.lock:
            msg = next((m for m in s.messages if str(m.get("mid") or "") == mid), None)
            if not msg:
                return {"ok": False, "error": "消息不存在"}
            if msg.get("queued") and not msg.get("cancelled"):
                return {"ok": False, "error": "排队中的消息请用撤回"}
            if msg.get("role") == "sys":
                return {"ok": False, "error": "系统消息不能改"}
            is_md = msg.get("role") == "ai"
            msg["text"] = text
            msg["html"] = hub.render_markdown(text, is_md)
            msg["edited"] = True
            s.rev += 1
            html = msg["html"]
        return {"ok": True, "html": html, "mid": mid}

    def delete_message(self, session_id, mid):
        """从控制台历史里删掉一条（对齐 Bajie /api/delete-message）。

        排队中的条会连带队列项一起拿掉，不把原文灌回输入框（那是撤回）。"""
        s = hub.HUB.sessions.get(session_id)
        if not s:
            return {"ok": False, "error": "会话不存在"}
        mid = str(mid or "")
        if not mid:
            return {"ok": False, "error": "消息不存在"}
        with s.lock:
            msg = next((m for m in s.messages if str(m.get("mid") or "") == mid), None)
            if not msg:
                return {"ok": False, "error": "消息不存在"}
            qid = msg.get("qid")
            if qid:
                s.queued = [e for e in s.queued if e.get("id") != qid]
            s.messages = [m for m in s.messages if str(m.get("mid") or "") != mid]
            s.rev += 1
        return {"ok": True, "mid": mid}

    def unqueue_message(self, session_id, qid):
        s = hub.HUB.sessions.get(session_id)
        if not s:
            return {"ok": True}
        with s.lock:
            entry = next((e for e in s.queued if e["id"] == qid), None)
            s.queued = [e for e in s.queued if e["id"] != qid]
            for m in s.messages:
                if m.get("qid") == qid:
                    m["queued"] = False
                    m["cancelled"] = True
            s.rev += 1
        # 撤回时把文本/图片/文件原样交还前端，恢复到输入框（而不是凭空丢失）
        restore = {"text": "", "selected": [], "images": [], "files": []}
        if entry:
            restore["text"] = entry.get("text") or ""
            restore["selected"] = list(entry.get("selected") or [])
            import base64 as _b64
            for img in entry.get("images") or []:
                media = img.get("media_type") or "image/png"
                restore["images"].append({
                    "data": "data:{};base64,{}".format(media, img.get("data") or ""),
                    "filename": img.get("filename"),
                })
            for f in entry.get("files") or []:
                b = f.get("data") or ""
                try:
                    size = len(_b64.b64decode(b))
                except Exception:
                    size = 0
                restore["files"].append({
                    "name": f.get("name") or "文件.bin",
                    "data": "data:application/octet-stream;base64,{}".format(b),
                    "size": size,
                })
        return {"ok": True, "restore": restore}

    def rename_tab(self, session_id, new_name):
        s = hub.HUB.sessions.get(session_id)
        if not s:
            return {"ok": False, "error": "会话不存在"}
        name = (new_name or "").strip()
        if not name:
            return {"ok": False, "error": "名称不能为空"}
        old = s.name
        s.name = name[:40]
        s.name_locked = True
        s.rev += 1
        if s.file_path:
            hub.HUB._append_file(s, f"\n> 标签改名: {old} → {s.name} · {hub.now_full()}\n")
            hub.HUB._rename_history_file(s)
        return {"ok": True}

    def rename_session(self, session_id, new_name):
        s = hub.HUB.sessions.get(session_id)
        if not s:
            return {"ok": False, "error": "会话不存在"}
        new_name = (new_name or "").strip()[:24]
        if not new_name:
            return {"ok": False, "error": "名称不能为空"}
        if new_name != s.name:
            self._remember_label(s)
            old = s.name
            s.name = new_name
            s.name_locked = True
            hub.HUB._append_file(s, f"\n> 会话重命名: {old} → {new_name} · {hub.now_full()}\n")
            hub.HUB._rename_history_file(s)
        return {"ok": True}

    def set_task_root(self, session_id, root=""):
        """改会话的「任务归属项目」（留空 = 认它当前报到的工作区）。

        跨工作区接手后，若用户确认「这活以后就归这个工作区」，点一下改归属：
        团队分组、席位、公告板、送审对象一起跟过去，串台提示随之消失。
        """
        s = hub.HUB.sessions.get(session_id)
        if not s:
            return {"ok": False, "error": "会话不存在"}
        new_root = str(root or "").strip() or (s.cwd or "")
        if not new_root:
            return {"ok": False, "error": "这个会话还没报到过工作区，无从改起"}
        self._remember_label(s)  # 归属变了标签跟着变，旧称呼记进曾用名
        old_root = hub.task_root_of(s)
        s.task_root = new_root
        s.task_root_locked = True
        if self._move_explicit_team_scope_root(s.conv_key, old_root, new_root):
            hub.save_config(hub.HUB.cfg)
        s.rev += 1  # 界面靠轮询 get_state 自己刷新，不走 notify（那会响铃闪窗）
        # 任务归属是跨重启恢复 TeamScope 的事实来源，改根后立即更新快照。
        hub.HUB.save_state()
        return {"ok": True, "task_root": new_root}

    def close_tab(self, session_id):
        """从列表里彻底移除标签（记录文件保留，「🕘 历史」仍能找回并接手）。"""
        s = hub.HUB.sessions.get(session_id)
        if not s:
            return {"ok": True}
        if s.connected:
            return {"ok": False, "error": "会话仍在连接中，无法关闭"}
        with hub.HUB.lock:
            hub.HUB.sessions.pop(session_id, None)
            if session_id in hub.HUB.order:
                hub.HUB.order.remove(session_id)
        hub.HUB.save_state()
        return {"ok": True, "removed": True}

    def _ext_close_composer_async(self, s):
        """控制台关会话 → 顺手把 Cursor 侧栏里那个对话 tab 收掉（Bajie 同款，09-07 rxyy 要的）。

        只在能确定对话身份（cursor_uuid 已验讫或是 hub 自己开的）且所在工作区有装了窗口
        总线的在线窗口时发；后台线程去做，归档本身不等扩展回执。Cursor 只暴露
        closeComposerTab（收 tab 进历史），没有删除/归档命令，所以叫「收」不叫「删」。
        """
        if not hub.HUB.cfg.get("ext_close_composer_on_archive",
                               hub.DEFAULTS.get("ext_close_composer_on_archive", True)):
            return False
        uid = str(getattr(s, "cursor_uuid", "") or "").strip()
        if not uid:
            return False
        if not (getattr(s, "uuid_verified", False) or getattr(s, "ext_instance", "")):
            return False  # 猜来的 uuid 不能拿去关别人的对话
        try:
            inst, _err = self._ext_pick_instance(getattr(s, "ext_instance", "") or "",
                                                 getattr(s, "cwd", "") or "")
            if inst is None:
                inst, _err = self._ext_pick_instance("", getattr(s, "cwd", "") or "")
        except Exception:  # noqa: BLE001
            inst = None
        if inst is None:
            return False
        name = s.name

        def _go():
            import ext_bus
            try:
                r = ext_bus.call(inst["id"], "closeComposer", {"composerId": uid}, timeout=8)
            except Exception as e:  # noqa: BLE001
                r = {"ok": False, "error": str(e)}
            hub.log_event("关会话顺带收 Cursor tab={} composer={} → {}".format(
                name, uid[:8], "ok" if r.get("ok") else ("失败：" + str(r.get("error") or ""))))
        threading.Thread(target=_go, name="ext-close-composer", daemon=True).start()
        return True

    def _archive_tab(self, s, save=True):
        """归档：断开这个 tab，但把它留在列表里（UI 收进「已结束」组）。

        归档后不再参与存活探测/判死提醒——它已经是用户主动关掉的，
        再去查 Cursor 库、响铃推手机只会吵人。
        """
        try:
            self._ext_close_composer_async(s)
        except Exception:  # noqa: BLE001
            pass  # 收 Cursor tab 是附赠，塌了不能连累归档
        s.archived = True
        s.recon_deadline = None      # 别再亮「重连中」的橙点
        s.lost_pending_on_drop = False
        s.pending_lost = False
        s.live_cache = None
        s.death_info = None
        s.ide_active_cache = False
        s.rev += 1  # 界面靠轮询 get_state 自己刷新，不走 notify（那会响铃闪窗）
        if save:
            hub.HUB.save_state()  # 关闭意图立刻落盘，别指望 15 秒后那趟快照
        return {"ok": True, "archived": True}

    def force_close(self, session_id):
        """关闭按钮，两段式（用户 08-04 要求：误关一次还能救回来）：

        第一次 × = 归档。连着的先断这个对话（给 AI 回「已关闭」），tab 收进
        「已结束」组，消息、右键接手、记录文件原样都在。
        第二次 × = 从列表里彻底删除，只剩记录文件（「🕘 历史」可搜可接手）。

        注意：同一 TCP 连接可能承载多个对话 tab，因此关闭单个 tab 绝不关 socket，
        只把该 conversation 标记为已关闭：有 pending 时立即回"已关闭"应答，
        AI 处理中时等它下次提问再自动回"已关闭"。
        """
        s = hub.HUB.sessions.get(session_id)
        if not s:
            return {"ok": True}
        if getattr(s, "archived", False):
            return self.close_tab(session_id)
        if not s.connected:
            return self._archive_tab(s)

        with s.lock:
            pending = s.pending
            s.pending = None
            s.queued = []
        s.client.closed_convs[s.conv_key] = True
        s.client.sessions.pop(s.conv_key, None)

        if pending:
            try:
                s.send({
                    "type": "zhi_response",
                    "id": pending["id"],
                    "user_input": hub.CLOSED_CONV_REPLY,
                    "selected_options": [],
                    "images": [],
                    "source": "popup_closed",
                })
            except Exception:
                pass
        s.connected = False
        s.processing_since = None
        hub.HUB.log_end(s, "用户在控制台关闭对话")
        return self._archive_tab(s)

    # 服务端复制的串行锁：网关是多线程的，并发往剪贴板塞东西没意义还互相打架
    _copy_lock = threading.Lock()

    def copy_text(self, text):
        """服务端兜底复制（前端 clipboard API 被拒时才走到这）。
        绝不可用 tkinter：在网关工作线程里并发建 Tk 根窗口会死锁整个进程持有 GIL
        ——07-27 17:14 僵尸事故实锤根因（用户在 iframe 里连点「复制提示词」触发）。"""
        try:
            with self._copy_lock:
                subprocess.run(
                    "clip",
                    input="\ufeff{}".format(text).encode("utf-16-le"),
                    check=True,
                    timeout=5,
                    creationflags=0x08000000,
                )
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # 剪贴板里放图片，一张 dataURL 解出来动辄几兆，别让人一路径把整个磁盘读进来
    COPY_IMAGE_CAP = 48 * 1024 * 1024

    def _image_for_clipboard(self, src):
        """把「要复制的那张图」落成一个 PowerShell 认得的磁盘文件。

        收两种来源：消息里的磁盘图（走 open_image 同一道闸——只认记录目录的 图片\\
        子目录）和输入区还没发出去的 dataURL 预览。返回 (路径, 是否临时文件)。
        """
        src = str(src or "")
        if src.startswith("data:image/"):
            head, _, b64 = src.partition(",")
            if not b64:
                raise ValueError("图片数据是空的")
            if len(b64) > self.COPY_IMAGE_CAP:
                raise ValueError("图片太大，复制不了")
            ext = re.sub(r"[^a-z0-9]", "", head[11:head.find(";") if ";" in head else len(head)])
            data = base64.b64decode(b64, validate=False)
            fd, tmp = tempfile.mkstemp(prefix="chijiu-copy-", suffix="." + (ext or "png"))
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            return Path(tmp), True
        p = Path(src).resolve()
        img_root = (Path(hub.HUB.cfg["history_dir"]) / "图片").resolve()
        if img_root not in p.parents or p.suffix.lower() not in (
                ".png", ".jpg", ".jpeg", ".gif", ".webp"):
            raise ValueError("路径不在图片目录内")
        if not p.is_file():
            raise ValueError("图片文件已经不在了")
        return p, False

    def copy_image(self, src):
        """把图片放进系统剪贴板。

        前端那条路走不通：这个 WebView 里 navigator.clipboard 整个不存在（08-12 实测，
        share.html 顶上那条注释就是为它写的），execCommand 又只搬得动文字。

        跟 copy_text 一样外派一个短命进程，绝不用 tkinter——网关是多线程的，在工作
        线程里建 Tk 根窗口会连 GIL 一起锁死整个进程（07-27 17:14 僵尸事故实锤）。
        SetDataObject 的第二个参数必须是 $true：不然剪贴板只拿到一个指向 PowerShell
        的引用，进程一退，粘出来就是空的。
        """
        tmp = None
        try:
            path, tmp_made = self._image_for_clipboard(src)
            tmp = path if tmp_made else None
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
                "$img=[System.Drawing.Image]::FromFile('{}');"
                "try{{[System.Windows.Forms.Clipboard]::SetDataObject($img,$true)}}"
                "finally{{$img.Dispose()}}"
            ).format(str(path).replace("'", "''"))
            with self._copy_lock:
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-STA", "-Command", ps],
                    capture_output=True, timeout=20, creationflags=0x08000000)
            if r.returncode != 0:
                err = (r.stderr or b"").decode("utf-8", "ignore").strip()
                return {"ok": False, "error": err.splitlines()[-1] if err else "复制失败"}
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            if tmp is not None:
                # SetDataObject($img,$true) 已经把位图刷进剪贴板了，源文件可以走
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def open_history_dir(self):
        try:
            os.startfile(hub.HUB.cfg["history_dir"])
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    def open_session_file(self, session_id):
        s = hub.HUB.sessions.get(session_id)
        if not s or not s.file_path:
            return {"ok": False, "error": "记录文件不存在"}
        try:
            os.startfile(s.file_path)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    def set_on_top(self, value):
        hub.HUB.cfg["always_on_top"] = bool(value)
        hub.save_config(hub.HUB.cfg)
        try:
            hub.HUB.window.on_top = bool(value)
        except Exception:
            pass
        return {"ok": True}

    def set_audio(self, value):
        hub.HUB.cfg["audio_enabled"] = bool(value)
        hub.save_config(hub.HUB.cfg)
        return {"ok": True}

    def get_share_info(self):
        return {
            "enabled": bool(hub.HUB.cfg.get("share_enabled", True)),
            "running": hub.HUB.share_server is not None,
            "url": hub.share_url(hub.HUB.cfg),
            "urls": hub.share_urls(hub.HUB.cfg),
            "port": int(hub.HUB.cfg.get("share_port", 39080)),
            "remote_base_url": (hub.HUB.cfg.get("remote_base_url") or "").strip(),
        }

    def push_share_link(self):
        """手动把分享页链接再推一条到手机（通知被清掉后重新挂一个入口）。"""
        return hub.HUB.push_share_link()

    def asr_transcribe(self, audio):
        """手机语音输入：base64 音频(data URL) → 百炼 qwen3-asr-flash → 文字。

        音频只在手机→本机→百炼之间流转，不经公共中继。计费按秒（0.0002 元/秒，
        一分钟约一分钱）。Key 优先级：cfg asr_api_key > 环境变量 > rxyy tools 设置页
        （workflow.db kv_config，与 AI 日报同一枚）。代码里不内置真 key——这份 hub.py
        是随分发包原样发出去的，内置就等于把 key 送给每个拿到包的人。"""
        import urllib.error
        import urllib.request
        audio = str(audio or "")
        if not audio.startswith("data:"):
            return {"ok": False, "error": "音频格式不对（需要 data URL）"}
        if len(audio) > 13 * 1024 * 1024:  # 百炼限 base64 后 10MB，报错给人话
            return {"ok": False, "error": "录音太长，一段最多约 4 分钟，请分段说"}
        key = (str(hub.HUB.cfg.get("asr_api_key") or "").strip()
               or os.environ.get("DASHSCOPE_API_KEY", "").strip()
               or hub.kv_config_get("bailian_api_key"))
        if not key:
            return {"ok": False,
                    "error": "还没配置百炼 API Key（rxyy tools → 设置 → 阿里百炼），"
                             "配好后手机语音输入即可用"}
        payload = {
            "model": str(hub.HUB.cfg.get("asr_model") or "qwen3-asr-flash"),
            "input": {"messages": [
                {"role": "system", "content": [{"text": ""}]},
                {"role": "user", "content": [{"audio": audio}]},
            ]},
            # ITN：中文口述的数字/日期规整成书面形态（「三点半」→「3点半」）
            "parameters": {"asr_options": {"enable_itn": True}},
        }
        req = urllib.request.Request(
            "https://dashscope.aliyuncs.com/api/v1/services/aigc"
            "/multimodal-generation/generation",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": "Bearer " + key,
                     "Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode("utf-8", "ignore")).get("message", "")
            except Exception:
                detail = ""
            hub.log_event("ASR 调用失败 HTTP {}: {}".format(e.code, detail[:200]))
            return {"ok": False, "error": "转写服务出错（HTTP {}）{}".format(e.code, detail[:120])}
        except Exception as e:
            hub.log_event("ASR 调用异常: {!r}".format(e))
            return {"ok": False, "error": "转写服务连不上，稍后再试"}
        try:
            content = body["output"]["choices"][0]["message"]["content"]
            text = "".join(c.get("text", "") for c in content if isinstance(c, dict)).strip()
        except Exception:
            text = ""
        if not text:
            return {"ok": False, "error": "没听清（空白音频或噪音太大），再说一次？"}
        return {"ok": True, "text": text}

    def pick_takeover_target(self, session_id, exclude=()):
        """自动挑一个接手方：优先「同项目 · 正等你回复 · 待命空壳」的 agent。

        通知栏上的一键派单没有界面可选人，服务端得自己挑对；挑不到就明说，
        别硬塞给一个正在忙别的活的 agent。

        exclude：这一批里已经被别的死会话挑走的接手方——批量「自动」时先把人
        分开，谁都挑不到了再合到同一个人头上（share_takeover_batch）。
        """
        s = hub.HUB.sessions.get(session_id)
        root = hub.norm_root(hub.task_root_of(s)) if s else ""
        best, best_key = None, None
        skip = set(str(x) for x in (exclude or ()))
        with hub.HUB.lock:
            cands = [x for x in hub.HUB.sessions.values()
                     if x.id != session_id and x.connected and x.id not in skip]
        for x in cands:
            key = (
                1 if x.pending is not None else 0,          # 正等回复的最好，立刻就能开工
                1 if hub.norm_root(hub.task_root_of(x)) == root else 0,
                1 if str(x.name or "").startswith("待命") else 0,
                -int(getattr(x, "msg_seq", 0) or 0),        # 越干净越像空闲待命
            )
            if best_key is None or key > best_key:
                best, best_key = x, key
        return best

    def share_takeover(self, session_id, target_id="", who=""):
        """手机上把一个中断的对话交给另一个活着的 agent 接手。

        控制台上的做法是「复制接手提示词 → 粘到 Cursor 窗口」，手机上没地方粘；
        这里直接把提示词当成一条消息投给目标 agent：它正等在 zhi 里就当场答复它，
        否则排队等它下次提问。于是躺在被窝里也能救一个挂掉的 agent。

        target_id 传空或 "auto" = 服务端自己挑（通知栏一键派单走这条）。
        """
        target_id = str(target_id or "").strip()
        if target_id in ("", "auto"):
            t = self.pick_takeover_target(session_id)
            if t is None:
                return {"ok": False, "error": "没有在线的 agent 可以接手，先开一个待命对话"}
            target_id = t.id
        if session_id == target_id:
            return {"ok": False, "error": "不能让它自己接手自己"}
        t = hub.HUB.sessions.get(target_id)
        if not t:
            return {"ok": False, "error": "接手方会话不存在"}
        if not t.connected:
            return {"ok": False, "error": "接手方不在线，换一个还连着的 agent"}
        # 连派几张单给同一个接手方时，规则全文只随第一张走：它按工作区取，
        # 后面几张逐字重复，白占接手方的上下文（0901 六张单 5097 行里约 4700 行是它）
        r = self.get_takeover_prompt(
            session_id,
            digest_repeat=self._digest_already_sent(target_id, t.cwd),
            **({"target_runtime": hub.runtime_adapter.kind(t)} if hub.runtime_adapter.is_native(t) else {}))
        if not r.get("ok"):
            return r
        label = (who or "手机").strip()[:16]
        # 壳判定必须取「派单前」的状态：接手提示词自己就是一条用户消息，送达
        # 即会把壳钉成「认领了活」（_mark_claimed），事后再判它就永远不是壳了
        target_was_shell = not hub.runtime_adapter.is_native(t) and hub.HUB._is_checkin_shellish(t)
        # 「当场答复 / 排队」同样取派单前：send_reply 送达即把 t.pending 清空，
        # 事后再看永远是「排队」——日志与 tab 上那句「已排队，它下次提问时收到」
        # 全说反了（09-02 记录里派单明明当场送达，气泡却写着排队）
        target_pending = t.pending is not None
        target_live_wait = target_pending and not (hub.runtime_adapter.is_native(t)
            and getattr(t, "wait_deferred", False))
        if target_pending:
            out = self.send_reply(target_id, r["prompt"], [], [], False, who=label)
        else:
            out = self.queue_message(target_id, r["prompt"], [], who=label)
        if not out.get("ok"):
            return out
        src = hub.HUB.sessions.get(session_id)
        src_conv = (r.get("conversation_id") or "").strip()
        target_name = t.name
        aliased = False
        if (src is not None and src_conv and t.conv_key
                and t.conv_key != src_conv and target_was_shell):
            # 派给待命壳（绝大多数情况）：不等它改 ID——壳 ID 当场登记为被接手
            # 会话的别名并归并壳，它带着壳 ID 来的每一次 zhi/zt 都自动落到原
            # tab。08-12 实测：靠「接手方肯改 ID」必失败（常驻协议要求全程复用
            # 首个 ID，两条规矩打架），机制兜底不靠服从。登记本身在
            # alias_shell_into 里（_retire_conv_into 单口径，A步收的双写）。
            hub.HUB.alias_shell_into(t, src)
            aliased = True
        else:
            # 派给正在干活的 agent：不抢它的身份，只记「这个 tab 的活交给了
            # 那个对话」——转告跟随（_follow_handoff）与面板「已交给 X」靠它
            t.handed_off_to = src_conv
        # 被接手的 tab 记「接手在途」：面板能看到派了谁、等了多久；接手方正忙
        # 自己的活时指令会被晾着（08-12 实测），超时提醒见 state_tick_loop
        if src is not None:
            src.takeover_dispatched = {"to_name": target_name,
                                       "to_conv": t.conv_key or "",
                                       "ts": time.time(), "warned": False}
            src.rev += 1
            hub.HUB.add_message(src, {
                "role": "sys", "ts": hub.now_hms(),
                "html": "⏳ 已把接手提示词派给 <b>{}</b>{}".format(
                    html_mod.escape(target_name),
                    "，其待命壳已就地归并——它开工即落到本 tab，无需改 ID"
                    if aliased else
                    ("（它正等回复，会立刻看到）" if target_live_wait
                     else "（已排队，它下次调用 zhi 时领取）" if hub.runtime_adapter.is_native(t)
                     else "（已排队，它下次提问/上报时收到）")),
            })
        hub.log_event("派接手 {} → {}（{}{}）".format(
            r.get("name") or session_id, target_name,
            "当场答复" if target_live_wait else "排队",
            "，壳已别名归并" if aliased else ""))
        return {"ok": True, "target": target_name, "name": r.get("name") or "",
                "queued": not target_live_wait}

    def share_takeover_batch(self, pairs, who=""):
        """一次派多个：pairs = [{sid, target}]，target 可留空让服务端自己挑。

        「两个新接入的各接一个死对话」是常态，一个个点菜单太慢（用户 07-31 提的）。

        同一个接手方收到 ≥2 张单时合成一条消息发（share_takeover_many）：派给待命壳
        的第一张会当场把壳归并进被接手的 tab（alias_shell_into → 壳从会话表消失），
        第二张再按壳 ID 派就是「接手方会话不存在」。手机批量派单里两个死会话选同一个
        壳一直会撞这个。「自动」挑人先把人分开，谁都挑不到了才合到同一个人头上。
        """
        groups, order, failed = {}, [], []
        taken = set()
        for p in pairs or []:
            if not isinstance(p, dict):
                continue
            sid = str(p.get("sid") or "").strip()
            if not sid:
                continue
            target = str(p.get("target") or "").strip()
            if target in ("", "auto"):
                t = (self.pick_takeover_target(sid, exclude=taken)
                     or self.pick_takeover_target(sid))
                if t is None:
                    name = (hub.HUB.sessions.get(sid).name
                            if hub.HUB.sessions.get(sid) else sid)
                    failed.append("{}：没有在线的 agent 可以接手，先开一个待命对话".format(name))
                    continue
                target = t.id
            taken.add(target)
            if target not in groups:
                groups[target] = []
                order.append(target)
            if sid not in groups[target]:
                groups[target].append(sid)
        done = []
        for target in order:
            sids = groups[target]
            if len(sids) == 1:
                sid = sids[0]
                r = self.share_takeover(sid, target, who=who)
                name = (hub.HUB.sessions.get(sid).name if hub.HUB.sessions.get(sid) else sid)
                if r.get("ok"):
                    done.append("{} → {}".format(name, r.get("target")))
                else:
                    failed.append("{}：{}".format(name, r.get("error")))
                continue
            r = self.share_takeover_many(sids, target, who=who)
            if r.get("ok"):
                done.extend("{} → {}".format(n, r.get("target")) for n in r.get("names") or [])
                failed.extend(r.get("failed") or [])
            else:
                names = [(hub.HUB.sessions.get(x).name if hub.HUB.sessions.get(x) else x)
                         for x in sids]
                failed.append("{}：{}".format("、".join(names), r.get("error")))
        return {"ok": bool(done), "done": done, "failed": failed}

    # 一次最多合并多少张接手单：再多就不是「接手」而是把一个 agent 埋了
    TAKEOVER_MANY_MAX = 12

    def get_takeover_prompt_many(self, session_ids, digest_repeat=False, target_runtime=None):
        """把几个中断会话的接手提示词合成一张单（复制给同一个 agent 用）。

        09-07：多选不再叫接手方「做哪张切哪张的原 ID」——那会把原对话复活，
        人不知道该回哪个会话。多张单一律 stay_put：活留在当前会话，原 ID 只作
        读记录。规则全文 / 项目规则 / 看板 / 技能目录只随第 1 张。
        """
        ids, seen = [], set()
        for x in session_ids or []:
            sid = str(x or "").strip()
            if sid and sid not in seen:
                seen.add(sid)
                ids.append(sid)
        if not ids:
            return {"ok": False, "error": "没有选中任何会话"}
        if len(ids) > self.TAKEOVER_MANY_MAX:
            return {"ok": False, "error": "一次最多合并 {} 张接手单（选了 {} 个）".format(
                self.TAKEOVER_MANY_MAX, len(ids))}
        stay_put = len(ids) > 1
        items, failed, chunks = [], [], []
        for sid in ids:
            r = self.get_takeover_prompt(
                sid, digest_repeat=bool(digest_repeat or chunks), stay_put=stay_put,
                **({"target_runtime": target_runtime} if target_runtime else {}))
            s = hub.HUB.sessions.get(sid)
            name = (s.name if s else "") or sid
            if not r.get("ok"):
                failed.append("{}：{}".format(name, r.get("error") or "生成失败"))
                continue
            items.append({"sid": sid, "name": r.get("name") or name,
                          "conversation_id": r.get("conversation_id") or ""})
            chunks.append(r["prompt"])
        if not chunks:
            return {"ok": False, "error": failed[0] if failed else "没有可接手的会话",
                    "failed": failed}
        if len(chunks) == 1:
            return {"ok": True, "prompt": chunks[0], "items": items, "failed": failed,
                    "count": 1, "conversation_id": items[0]["conversation_id"],
                    "name": items[0]["name"]}
        n = len(chunks)
        # 开头必须是 TAKEOVER_PROMPT_HEAD 原文：_auto_label_on_dispatch 靠 startswith
        # 认出「这是接手单不是派活」，否则壳被改成这段话当名字、收壳机制跟着失效
        # 会话名里可能有花括号（{项目名} 事故同类），清单只拼接、不过 .format()
        head = (
            hub.TAKEOVER_PROMPT_HEAD + "——准确说是 " + str(n) + " 个：下面 " + str(n)
            + " 张接手单一次交给你，全部在**当前这个会话**里按顺序做完。\n\n"
            "【批量接手 · 怎么做】\n"
            "- 全程沿用你当前会话的 conversation_id，不要改用下面任何一张单的原 ID；"
            "原 ID 只用来读记录和定位前任流水。改用原 ID 会把旧对话复活，"
            "控制台会出现多个活人，你也不知道该回哪张。\n"
            "- 做完一张先用当前 ID zhi 汇报，再接着做下一张，不要切会话。\n"
            "- 用户级规则全文 / 本项目规则 / 开干先看板 / 技能目录只随第 1 张给一次，"
            "后面几张逐字相同，按第 1 张那份遵守。\n"
            "- 清单（按这个顺序做）：\n"
            + "".join("  " + str(i + 1) + ". " + str(it["name"]) + "（原对话 ID "
                      + (it["conversation_id"] or "无固定ID") + "，只读记录）\n"
                      for i, it in enumerate(items))
            + "\n"
        )
        body = "\n\n".join(
            "═══════ 第 {}/{} 单 · {} ═══════\n{}".format(i + 1, n, items[i]["name"], c)
            for i, c in enumerate(chunks))
        return {"ok": True, "prompt": head + body, "items": items, "failed": failed,
                "count": n, "conversation_id": items[0]["conversation_id"],
                "name": items[0]["name"], "stay_put": True}

    def _commit_multi_takeover(self, sources, succ):
        """多选接手落地：原对话全部进已结束，活留在接手方当前会话。

        09-07 rxyy：两个未完成任务派给一个 agent 时，复活原对话会让人不知道
        该回哪个；应在新会话接着干，原来的丢进已结束而不是待续。
        """
        if succ is None:
            return
        for src in sources:
            if src is None or src.id == succ.id:
                continue
            src_conv = str(getattr(src, "conv_key", "") or "")
            if src_conv and src_conv != "__default__" and getattr(succ, "conv_key", ""):
                try:
                    hub.HUB._retire_conv_into(
                        src_conv, succ.conv_key,
                        old_name=src.name, succ_label=succ.name,
                        why="多选接手归档原对话")
                except Exception:
                    pass
            pending = None
            try:
                with src.lock:
                    pending = src.pending
                    src.pending = None
                    src.queued = []
                if src.client is not None:
                    src.client.closed_convs[src_conv] = True
                    src.client.sessions.pop(src_conv, None)
            except Exception:
                pass
            if pending:
                try:
                    src.send({
                        "type": "zhi_response",
                        "id": pending["id"],
                        "user_input": hub.CLOSED_CONV_REPLY,
                        "selected_options": [],
                        "images": [],
                        "source": "popup_closed",
                    })
                except Exception:
                    pass
            src.connected = False
            src.processing_since = None
            src.handed_off_to = succ.conv_key or ""
            src.handoff_final = True
            src.takeover_dispatched = None
            hub.HUB.add_message(src, {
                "role": "sys", "ts": hub.now_hms(),
                "html": "多选接手：活已交给 <b>{}</b> 在当前会话接着干，本 tab 收入「已结束」"
                        "（不再复活原对话）".format(
                            html_mod.escape(succ.name or succ.conv_key)),
            })
            hub.HUB.log_end(src, "多选接手归档给 {}".format(succ.name))
            try:
                hub.HUB._tombstone_shell(src, succ)
            except Exception:
                pass
            self._archive_tab(src, save=False)
        try:
            hub.HUB.save_state()
        except Exception:
            pass

    def share_takeover_many(self, session_ids, target_id="", who=""):
        """把几个中断的会话一次交给**同一个** agent：合成一张单、只发一条消息。

        不能循环调 share_takeover：派给待命壳的第一张就把壳归并掉了（见
        share_takeover_batch 的说明）。多选（≥2）不再 alias 壳、不再复活原对话：
        活留在接手方当前会话，原 tab 全部归档进已结束。
        """
        ids, seen = [], set()
        for x in session_ids or []:
            sid = str(x or "").strip()
            if sid and sid not in seen:
                seen.add(sid)
                ids.append(sid)
        if not ids:
            return {"ok": False, "error": "没有选中任何会话"}
        if len(ids) == 1:
            r = self.share_takeover(ids[0], target_id, who=who)
            if r.get("ok"):
                r.setdefault("names", [r.get("name") or ""])
                r.setdefault("count", 1)
                r.setdefault("failed", [])
            return r
        target_id = str(target_id or "").strip()
        if target_id in ("", "auto"):
            t = self.pick_takeover_target(ids[0], exclude=ids)
            if t is None:
                return {"ok": False, "error": "没有在线的 agent 可以接手，先开一个待命对话"}
            target_id = t.id
        if target_id in ids:
            return {"ok": False, "error": "不能让它自己接手自己（接手方也在选中的会话里）"}
        t = hub.HUB.sessions.get(target_id)
        if not t:
            return {"ok": False, "error": "接手方会话不存在"}
        if not t.connected:
            return {"ok": False, "error": "接手方不在线，换一个还连着的 agent"}
        r = self.get_takeover_prompt_many(
            ids, digest_repeat=self._digest_already_sent(target_id, t.cwd),
            **({"target_runtime": hub.runtime_adapter.kind(t)} if hub.runtime_adapter.is_native(t) else {}))
        if not r.get("ok"):
            return r
        items = r["items"]
        label = (who or "手机").strip()[:16]
        target_pending = t.pending is not None  # 派单前快照，理由同 share_takeover
        target_live_wait = target_pending and not (hub.runtime_adapter.is_native(t)
            and getattr(t, "wait_deferred", False))
        if target_pending:
            out = self.send_reply(target_id, r["prompt"], [], [], False, who=label)
        else:
            out = self.queue_message(target_id, r["prompt"], [], who=label)
        if not out.get("ok"):
            return out
        target_name = t.name
        sources = []
        for it in items:
            src = hub.HUB.sessions.get(it["sid"])
            if src is not None:
                sources.append(src)
        # 多选：壳留下当活人，原对话全部进已结束。单张仍走 share_takeover 的
        # alias_shell_into（上面 len==1 已提前 return）。
        self._commit_multi_takeover(sources, t)
        names = [it["name"] for it in items]
        n = len(items)
        hub.log_event("批量派接手 {} 张 → {}（{}，原对话进已结束）：{}".format(
            n, target_name, "当场答复" if target_live_wait else "排队",
            "、".join(names)))
        return {"ok": True, "target": target_name, "names": names, "count": n,
                "name": names[0], "queued": not target_live_wait,
                "failed": r.get("failed") or [], "stay_put": True}

    def get_task_intake_info(self):
        """任务投递链接（发给同事提需求用）：只开任务入站页，进不了任何会话。"""
        if hub.HUB.share_server is None:
            return {"ok": False, "error": "分享服务未启动（端口可能被占用）"}
        if not hub.HUB.cfg.get("share_enabled", True):
            return {"ok": False, "error": "分享已关闭，请先打开「共享」开关"}
        items = [{"label": u["label"], "url": u["url"], "svg": hub._qr_svg_data_uri(u["url"])}
                 for u in hub.intake_urls(hub.HUB.cfg)]
        return {"ok": True, "items": items, "url": items[-1]["url"] if items else ""}

    def get_quick_phrases(self):
        ph = hub.HUB.cfg.get("quick_phrases") or []
        return {"phrases": [str(p) for p in ph if str(p).strip()][:12]}

    def image_root(self):
        """消息图片的根目录（网关 /img 端点做路径白名单校验用）。
        浏览器/iframe（http 源）加载不了 file:/// 图片，缩略图改走网关代理。"""
        return {"ok": True, "root": str(Path(hub.HUB.cfg["history_dir"]).resolve())}

    def reorder_tabs(self, id_list):
        """按前端拖拽后的顺序重排 tab。只接受已存在的 id，缺失的保持原相对顺序在末尾。"""
        id_list = id_list or []
        with hub.HUB.lock:
            cur = list(hub.HUB.order)
            seen = set()
            new_order = []
            for sid in id_list:
                if sid in hub.HUB.sessions and sid not in seen:
                    new_order.append(sid)
                    seen.add(sid)
            for sid in cur:
                if sid not in seen:
                    new_order.append(sid)
                    seen.add(sid)
            hub.HUB.order = new_order
        return {"ok": True}

    def pin_tab(self, session_id):
        """置顶：把该 tab 移到最前。"""
        with hub.HUB.lock:
            if session_id in hub.HUB.order:
                hub.HUB.order.remove(session_id)
                hub.HUB.order.insert(0, session_id)
        return {"ok": True}

    def set_share(self, value):
        hub.HUB.cfg["share_enabled"] = bool(value)
        hub.save_config(hub.HUB.cfg)
        return {"ok": True}

    def get_session_share_info(self, session_id):
        """单会话直连链接：只授权访问这一个会话"""
        s = hub.HUB.sessions.get(session_id)
        if not s:
            return {"ok": False, "error": "会话不存在"}
        if hub.HUB.share_server is None:
            return {"ok": False, "error": "分享服务未启动（端口可能被占用）"}
        if not hub.HUB.cfg.get("share_enabled", True):
            return {"ok": False, "error": "分享已关闭，请先打开「共享」开关"}
        return {"ok": True, "url": hub.session_share_url(hub.HUB.cfg, session_id),
                "urls": hub.session_share_urls(hub.HUB.cfg, session_id), "name": s.name}

    def get_session_qr(self, session_id):
        """返回单会话直连链接的二维码（SVG data URI），手机扫码即可在手机上回复。"""
        info = self.get_session_share_info(session_id)
        if not info.get("ok"):
            return info
        return {"ok": True, "url": info["url"], "name": info["name"],
                "svg": hub._qr_svg_data_uri(info["url"])}

    def _mcp_port(self):
        try:
            return int(hub.HUB.cfg.get("mcp_http_port") or 39222)
        except Exception:
            return 39222

    def _ensure_one_project_mcp(self, cwd):
        """只给这一个工作区写项目级 URL。已是目标值则不写盘。不碰全局 nonce。"""
        import mcp_touch
        cwd = str(cwd or "").strip().strip('"')
        if not cwd:
            return False, "请指定工作目录"
        if hub._is_hub_install_dir(cwd):
            return False, "不能给控制台安装目录写项目级 MCP"
        return mcp_touch.ensure_workspace_mcp(cwd, port=self._mcp_port())

    def _ensure_project_mcps(self, cwd=None):
        """兼容旧调用：只写传入的那一个目录，不再扫全部已知项目。"""
        if cwd:
            try:
                self._ensure_one_project_mcp(cwd)
            except Exception:
                pass

    def list_known_project_roots(self):
        """「选目录后复制」弹窗：已知工作区 + 各自项目级 MCP 是否已经写对。"""
        import mcp_touch
        port = self._mcp_port()
        roots = []
        try:
            for s in list(hub.HUB.sessions.values()):
                if getattr(s, "archived", False):
                    continue
                for attr in ("cwd", "task_root"):
                    v = getattr(s, attr, None)
                    if v:
                        roots.append(v)
        except Exception:
            pass
        try:
            for info in (hub.HUB.cfg.get("team_project_members") or {}).values():
                if isinstance(info, dict) and info.get("root"):
                    roots.append(info["root"])
        except Exception:
            pass
        items, seen = [], set()
        for r in roots:
            try:
                key = hub.norm_root(r)
            except Exception:
                key = str(r)
            if not key or key in seen:
                continue
            seen.add(key)
            ok, st = mcp_touch.inspect_workspace_mcp(r, port=port)
            try:
                shown = str(Path(r).resolve()) if Path(r).exists() else str(r)
            except OSError:
                shown = str(r)
            items.append({
                "root": shown,
                "name": Path(shown).name or shown,
                "mcp": st if ok else "error",
                "error": "" if ok else st,
            })
        items.sort(key=lambda x: (x.get("name") or "").lower())
        return {"ok": True, "items": items, "port": port}

    # ---------------- 双活数据同步（P0 骨架，方案A 事件互灌） ----------------
    def sync_status(self):
        """双活同步状态：对端滞后量/坏行/停机原因/时钟漂移，P1 起并入配置线
        （wf）与运输线（ferry）。P4 才进 UI 常显，先给验收与排查用。"""
        bus = getattr(hub, "SYNCBUS", None)
        if bus is None:
            return {"ok": True, "enabled": False,
                    "note": "未启用：config sync_root 为空（P1 起用 SSH 摆渡当"
                            "运输层，见 sync_ferry.py 门头）"}
        out = {"ok": True, "enabled": True, **bus.status()}
        wf = getattr(hub, "WFSYNC", None)
        if wf is not None:
            out["wf"] = wf.status()
        ferry = getattr(hub, "FERRY", None)
        if ferry is not None:
            out["ferry"] = ferry.status()
        cs = getattr(hub, "CONSYNC", None)
        if cs is not None:
            out["console"] = cs.status()
        return out

    def sync_poll_now(self):
        """验收/排查用的手动一趟：摆渡拉一次 → 配置线差量一拍 → 重放一拍。
        平时不用点它，三条线都有自己的节拍；它只是把「等下一拍」压成立即。"""
        bus = getattr(hub, "SYNCBUS", None)
        if bus is None:
            return {"ok": False, "error": "未启用：config sync_root 为空"}
        out = {"ok": True}
        ferry = getattr(hub, "FERRY", None)
        if ferry is not None:
            out["ferry_changed"] = bool(ferry.pull_once())
        wf = getattr(hub, "WFSYNC", None)
        if wf is not None:
            out["wf_emitted"] = wf.poll_once()
        cs = getattr(hub, "CONSYNC", None)
        if cs is not None:
            out["console_emitted"] = cs.poll_once()
        out["replayed"] = bus.replayer.poll_once()
        return out

    def sync_demo_emit(self, text=""):
        """P0 验收口（设计稿 §4.4）：A 机手工发一条演示事件，B 机 5s 内重放落地；
        断网恢复后无重复无丢失（幂等台账保证）。"""
        bus = getattr(hub, "SYNCBUS", None)
        if bus is None:
            return {"ok": False, "error": "未启用：config sync_root 为空"}
        ev = bus.demo_emit(str(text or "ping"))
        return {"ok": True, "event": {k: ev.get(k) for k in
                                      ("uuid", "seq", "ts", "machine")}}

    def sync_peer_sessions(self):
        """对端机器的会话控制台列表（P2 业务线落的只读副本）：控制台把它按
        机器名标注展示；聊天记录不同步，点开要去对端机自己看（rxyy 08-31 拍板）。"""
        cs = getattr(hub, "CONSYNC", None)
        if cs is None:
            return {"ok": True, "enabled": False, "peers": []}
        return {"ok": True, "enabled": True, "peers": cs.sess.peers_snapshot()}

    def new_chat_prompt(self, cwd=None, write_mcp=False, target_runtime="cursor"):
        """预分配 MCP 对话 ID，按客户端生成手动接入提示词。

        默认只复制提示词，不写 mcp.json（已有项目级的工作区直接点「复制」）。
        write_mcp=True 时只给 cwd 这一个目录写项目级 URL；已是目标值则不写盘。
        """
        target = str(target_runtime or "cursor").strip().lower()
        if target not in ("cursor", "codex", "chatgpt"):
            return {"ok": False, "error": "请选择 Cursor、Codex 或 ChatGPT"}
        if target != "cursor":
            cid = uuid.uuid4().hex[:8]
            name = "Codex" if target == "codex" else "ChatGPT"
            prompt = (
                "请将当前 {} 任务接入rxyy MCP，并等待我的后续指令。\n"
                "conversation_id 固定使用 {}（这是 MCP 对话 ID，不是控制台标签 ID）；"
                "runtime={}；project_path={}。\n"
                "先用真实可用的 zt 工具上报上述字段，thread_id 填当前任务自身的真实原生 ID，"
                "model 填实际模型，不要复用来源任务 ID 或凭空生成原生 ID。\n"
                "接入后通过 zhi 报告已连接并等待回复；同一对话始终沿用上述 conversation_id。"
                "等待应有界，续接时 message 留空；允许我在原生客户端或rxyy MCP控制台继续。\n"
                "遵守当前宿主指令；工具未提供时明确说明接入未完成，不能以文字模拟工具调用。"
            ).format(name, cid, target, str(cwd or "当前任务的真实项目目录"))
            return {"ok": True, "prompt": prompt, "conversation_id": cid,
                    "runtime": target, "mcp": "", "mcp_url": ""}
        import mcp_touch
        # 点「+」= 马上要往窗口里发报到提示词：先让等待中的 zhi 腾出并发槽位，
        # 并开启报到潮窗口持续轮转（一次性开六个新对话时，后面几个是陆续到的）
        try:
            hub.HUB.yield_zhi_all_local()
            hub.HUB.start_yield_burst()
        except Exception:
            pass
        write_mcp = write_mcp if isinstance(write_mcp, bool) else (
            str(write_mcp or "").strip().lower() in ("1", "true", "yes", "on"))
        mcp_status = ""
        mcp_url = ""
        if write_mcp:
            ok, why = self._ensure_one_project_mcp(cwd)
            if not ok:
                return {"ok": False, "error": why}
            mcp_status = why
            mcp_url = mcp_touch.project_mcp_url(cwd, self._mcp_port())
        cid = uuid.uuid4().hex[:8]
        tpl = str(hub.HUB.cfg.get("new_chat_prompt") or hub.DEFAULTS["new_chat_prompt"])
        return {"ok": True, "prompt": hub.fill_checkin_prompt(tpl, cid, cwd),
                "conversation_id": cid, "runtime": target, "mcp": mcp_status, "mcp_url": mcp_url}

    # ---------- Cursor 窗口总线：在扩展宿主里无头开对话 / 调到前台 ----------
    # hub 是独立进程，进不了扩展宿主；装了「rxyy MCP 窗口总线」扩展的 Cursor 窗口
    # 会把自己登记到 %LOCALAPPDATA%\rxyy-tools-community\extbus\instances.json 并泵命令文件
    # （ext_bus.py / extbus-ext/）。这一组方法是控制台「一键开 N 个待命对话」
    # 「新开 Cursor 对话接手」的后端；bajie-chat 同款能力的rxyy MCP 版（#6 最小方案）。

    # 无头开出的对话从 createNew 到 agent 首次 zhi 报到通常 15~60s；超过这个数
    # 就当没起来（用户关了 / 模型排队 / MCP 没连上），派单线程收工并在 tab 上说明
    EXT_CHECKIN_WAIT_SECS = 240

    def ext_instances(self):
        """装了窗口总线扩展、且 60s 内还在登记的 Cursor 窗口。"""
        import ext_bus
        try:
            items = ext_bus.live_instances()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": "读窗口登记失败: {}".format(e),
                    "instances": [], "root": str(ext_bus.bus_root())}
        return {"ok": True, "instances": items, "root": str(ext_bus.bus_root())}

    def _ext_pick_instance(self, instance_id, cwd=""):
        """挑窗口：指定的就用它；空/auto = 工作区等于 cwd 的那个，否则唯一在线的那个。

        返回 (instance_dict, error)。两个以上在线又没指明 → 让 UI 列出来选，不猜。
        """
        import ext_bus
        want = str(instance_id or "").strip()
        items = ext_bus.live_instances()
        if not items:
            return None, ("没有装了「rxyy MCP 窗口总线」扩展的 Cursor 窗口在线"
                          "（装法：py rxyy_mcp\\extbus_build.py --install，已开着的窗口要 Reload Window）")
        if want and want != "auto":
            hit = next((x for x in items if x["id"] == want), None)
            return (hit, None) if hit else (None, "那个 Cursor 窗口已不在线，重新选一个")
        root = hub.norm_root(cwd) if cwd else ""
        if root:
            same = [x for x in items if hub.norm_root(x.get("workspace") or "") == root]
            if len(same) == 1:
                return same[0], None
            if len(same) > 1:
                items = same
        if len(items) == 1:
            return items[0], None
        return None, "有 {} 个 Cursor 窗口在线，请指明在哪个窗口开".format(len(items))

    def _ext_prepare_shell(self, inst, name, note):
        """给即将无头开出的对话预分配 conversation_id / composerId，预建断开态壳并预绑定。

        报到词走 fill_checkin_prompt（≤ 1000B 过 Cursor 首条消息字节闸），工作区路径
        取扩展登记的 workspace——agent 一睁眼就知道自己在哪，不用拿 shell 去问。
        """
        cid = uuid.uuid4().hex[:8]
        composer_id = str(uuid.uuid4())
        cwd = str(inst.get("workspace") or "").strip()
        tpl = str(hub.HUB.cfg.get("new_chat_prompt") or hub.DEFAULTS["new_chat_prompt"])
        prompt = hub.fill_checkin_prompt(tpl, cid, cwd)
        shell = hub.HUB.create_spawn_shell(cid, cwd, name, note=note)
        shell.ext_instance = inst["id"]
        # composerId 是 hub 拍板的事实，当场绑上（uuid_verified 留给报到那一下）：
        # 死因探针这就能读那个对话——09-03 17:25 实测无头开的对话被 AI 网关并发闸
        # 一句正文拒了，绑之前壳只会挂着「重连中」到超时，没人知道它根本没跑起来
        shell.cursor_uuid = composer_id
        shell.death_probe_uid = composer_id
        hub.HUB.prebound_composers[cid] = {"uuid": composer_id, "ts": time.time(),
                                           "instance": inst["id"]}
        return {"cid": cid, "composerId": composer_id, "prompt": prompt, "shell": shell,
                "name": name}

    @staticmethod
    def _ext_shell_is_live(shell):
        """这个预建壳是不是已经有人报到了（再收就把绿灯 tab 人间蒸发）。"""
        if shell is None:
            return False
        return bool(getattr(shell, "connected", False)
                    or getattr(shell, "pending", None) is not None
                    or getattr(shell, "client", None) is not None)

    @staticmethod
    def _ext_open_timeout(n):
        """batchOpen 回执上限。5 个带 autoSubmit 的 createNew 现网常 >25s（09-07 27/29s）。"""
        try:
            n = max(1, int(n or 1))
        except (TypeError, ValueError):
            n = 1
        return max(45.0, 25.0 + 10.0 * n)

    def _ext_drop_shell(self, shell, why):
        """createNew 没成：把刚预建的壳收掉，别留一个永远「重连中」的 tab。

        已经报到的壳不是「开失败」：09-07 批量新建回执超时后整排绿灯被收掉，
        agent 的 zhi 还堵在这个 Session 上，控制台只剩 Cursor 侧「报到成功」。
        """
        if self._ext_shell_is_live(shell):
            hub.log_event("窗口总线开对话回执失败但壳已报到，不收 tab conv={}：{}".format(
                getattr(shell, "conv_key", "?"), why))
            return
        try:
            hub.HUB.prebound_composers.pop(shell.conv_key, None)
            with hub.HUB.lock:
                hub.HUB.sessions.pop(shell.id, None)
                if shell.id in hub.HUB.order:
                    hub.HUB.order.remove(shell.id)
            hub.log_event("窗口总线开对话失败，收回预建壳 conv={}：{}".format(shell.conv_key, why))
        except Exception:  # noqa: BLE001
            pass

    def _normalize_open_models(self, models):
        """把新建对话的模型参数收成 spec 列表；空 / 非法项丢掉。

        允许重复：调用方按「3 个 grok + 2 个 opus」展开成
        [grok, grok, grok, opus, opus] 传进来，去重会把数量吞掉。
        总长仍封 50。旧调用传去重后的 [A, B] 再配 count=5，仍走下面的轮流。
        """
        raw = models
        if raw is None or raw == "":
            return []
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            return []
        out = []
        for item in raw:
            extra = {}
            if isinstance(item, dict):
                extra = item
                model = str(item.get("model") or item.get("id")
                            or item.get("modelName") or "").strip()
            elif isinstance(item, str):
                model = item[:-4].strip() if item.lower().endswith(" max") else item
            else:
                model = ""
            schema = extra.get("schema") if isinstance(extra.get("schema"), dict) \
                else hub.catalog_schema_for(model)
            spec = hub.normalize_open_model(item, schema=schema)
            if not spec:
                continue
            out.append(spec)
            if len(out) >= 50:
                break
        return out

    def list_known_models(self):
        """新建对话弹窗：优先本机 Cursor 目录，没有再回落预置档 + 见过的 tab。"""
        seen = {}
        catalog = list(hub.iter_catalog_open_specs())
        if catalog:
            auto = hub.normalize_open_model({"model": "default", "max": False})
            if auto:
                auto["schema"] = {
                    "loaded": True, "think_id": "", "think_values": [],
                    "has_fast": False, "supports_max": False, "supports_std": True,
                }
                seen[auto["model"]] = auto
            for spec in catalog:
                seen[spec["model"]] = spec
        else:
            for spec in hub.iter_model_presets():
                seen[spec["model"]] = spec
        for s in list(hub.HUB.sessions.values()):
            info = getattr(s, "model_info", None) or {}
            model = str(info.get("model") or "").strip()
            if not model or model in seen:
                continue
            schema = hub.catalog_schema_for(model)
            spec = hub.normalize_open_model({
                "model": model, "max": bool(info.get("max")),
            }, schema=schema)
            if spec:
                seen[spec["model"]] = spec
        models = []
        for spec in hub.pinned_open_specs(seen.values()):
            row = {
                "key": spec["key"], "model": spec["model"], "max": spec["max"],
                "label": spec["label"], "parameters": spec["parameters"],
                "effort": spec.get("effort") or "",
                "fast": spec.get("fast"),
                "thinking": spec.get("thinking") or "",
            }
            if spec.get("schema"):
                row["schema"] = spec["schema"]
            models.append(row)
        return {
            "ok": True, "models": models,
            "source": "cursor" if catalog else "preset",
            "efforts": [{"id": k, "label": lab} for k, lab in hub.EFFORT_OPTIONS],
        }

    def ext_batch_open(self, instance_id="", count=1, cwd="", names=None, who="",
                       name_prefix="", models=None):
        """在某个 Cursor 窗口里无头开 N 个待命对话（不用粘贴报到词、不抢焦点）。

        每个对话：预分配 conversation_id + composerId → 预建断开态壳（tab 立即可见，
        报到前显示「重连中」）→ 报到词当首条消息 autoSubmit。agent 报到那一下
        _bind_prebound_composer 直接认领 composerId，不扫库。

        `name_prefix` = bajie 侧栏「一键开多个对话」的起名前缀：`names` 没盖到的位置按
        「前缀·序号」补，只开一个时不带序号。前缀在服务端展开而不是只在控制台拼字符串，
        手机页和 MCP 直调才能拿到同一套命名；`names` 里显式给的名字仍然优先。

        `models` = 要开的模型档。重复项表示数量（3 个 grok 就传三次 grok）；
        去重后的短列表配 count>1 则按列表轮流。不选则沿用该 Cursor 窗口当前档。
        """
        import ext_bus
        specs = self._normalize_open_models(models)
        try:
            n = max(1, min(50, int(count or 1)))
        except (TypeError, ValueError):
            n = 1
        if len(specs) > 1 and n == 1:
            n = min(50, len(specs))
        inst, err = self._ext_pick_instance(instance_id, cwd)
        if inst is None:
            return {"ok": False, "error": err}
        ws_name = Path(inst.get("workspace") or "").name or "workspace"
        names = list(names or [])
        prefix = str(name_prefix or "").strip()[:24]
        label = (who or "控制台").strip()[:16]
        preps = []
        for i in range(n):
            spec = specs[i % len(specs)] if specs else None
            if prefix:
                fallback = prefix if n == 1 else "{}·{}".format(prefix, i + 1)
            elif spec:
                fallback = "待命·{}".format(spec["label"])
            else:
                fallback = "待命·{}".format(ws_name)
            nm = str(names[i]).strip()[:40] if i < len(names) and str(names[i]).strip() \
                else fallback[:40]
            prep = self._ext_prepare_shell(
                inst, nm,
                note="🪟 已在 Cursor 窗口「{}」无头开出这个对话（{} 发起）· 等它报到".format(
                    html_mod.escape(inst.get("label") or ""), html_mod.escape(label)))
            if spec:
                prep["modelConfig"] = spec["config"]
                prep["model_label"] = spec["label"]
                # 记在壳上：createNew 没开成（扩展泵卡死 / 窗口刚重载）时「重开」还能带同一档
                prep["shell"].ext_open_spec = {"modelConfig": spec["config"],
                                               "model_label": spec["label"]}
            preps.append(prep)
        opened = []
        verified = 0
        last_error = ""
        # 扩展自身一次 batchOpen 只接小批量。总入口跟 Bajie 一样允许 50 个，但每次最多
        # 喂 10 个，既不越过扩展的 12 个硬上限，也让每批结果能独立回收失败壳。
        for start in range(0, len(preps), 10):
            batch = preps[start:start + 10]
            items = []
            for p in batch:
                item = {"composerId": p["composerId"], "name": p["name"],
                        "prompt": p["prompt"]}
                if p.get("modelConfig"):
                    item["modelConfig"] = p["modelConfig"]
                items.append(item)
            r = ext_bus.call(inst["id"], "batchOpen", {
                "items": items,
                "autoSubmit": True,
                # 挂载确认最坏再等 3s，还可能让整批回执撞上 25s 墙；壳在不在以报到为准
                "skipMountWait": True,
            }, timeout=self._ext_open_timeout(len(batch)))
            verified += int(r.get("verified") or 0)
            last_error = r.get("error") or last_error
            # 请求已经丢给扩展：超时多半是回执没写回来，对话其实开出来了（09-07 两批）
            ack_lost = r.get("code") == "TIMEOUT"
            results = {str(x.get("composerId") or ""): x for x in (r.get("results") or [])
                       if isinstance(x, dict)}
            for p in batch:
                item = results.get(p["composerId"])
                ok = bool(r.get("ok")) and (item is None or item.get("ok", True))
                live = self._ext_shell_is_live(p["shell"])
                if not ok and not ack_lost and not live:
                    self._ext_drop_shell(
                        p["shell"], (item or {}).get("error") or r.get("error") or "createNew 失败")
                    continue
                if not ok:
                    hub.log_event("窗口总线开对话回执失败但壳留下 conv={}：{}".format(
                        p["cid"], r.get("error") or (item or {}).get("error") or "回执超时"))
                rec = {"conversation_id": p["cid"], "composerId": p["composerId"],
                       "session_id": p["shell"].id, "name": p["shell"].name,
                       "mounted": bool((item or {}).get("mounted", True))}
                if p.get("model_label"):
                    rec["model"] = p["model_label"]
                opened.append(rec)
        hub.log_event("窗口总线开对话 窗口={} 请求 {} 个 · 成功 {} 个（{}）".format(
            inst.get("label"), n, len(opened), label))
        if not opened:
            return {"ok": False, "error": last_error or "Cursor 窗口没开出对话",
                    "instance": inst}
        return {"ok": True, "opened": opened, "count": len(opened), "instance": inst,
                "verified": verified}

    def ext_reopen_shell(self, session_id, who=""):
        """无头开出的对话没起来（tab 一直「重连中」）→ 用同一个 conversation_id / composerId /
        标题再开一次，tab 不换、记录不换。

        09-07 13:35/13:37：扩展泵被一条永不 resolve 的 createNew 卡死，两次单开 grok 的请求被
        取走却没执行，壳留成死 tab。修好泵之后这两个壳没必要删了重建——名字和 id 都是用户
        看着的那个。壳已经有人报到（connected/pending）就拒绝，别给同一个 id 开两个 agent。
        """
        import ext_bus
        s = hub.HUB.sessions.get(str(session_id or ""))
        if s is None:
            return {"ok": False, "error": "会话不存在"}
        if self._ext_shell_is_live(s):
            return {"ok": False, "error": "这个对话已经有 agent 在跑，不用重开"}
        inst_id = str(getattr(s, "ext_instance", "") or "")
        composer_id = str(getattr(s, "cursor_uuid", "") or "")
        if not inst_id or not composer_id:
            return {"ok": False, "error": "这个 tab 不是无头开出来的（没有窗口 / composerId 记录），重开无从下手"}
        inst, err = self._ext_pick_instance(inst_id, getattr(s, "cwd", "") or "")
        if inst is None:
            # 出生窗口关了：同工作区还有别的在线窗口就挪过去开
            inst, err = self._ext_pick_instance("", getattr(s, "cwd", "") or "")
        if inst is None:
            return {"ok": False, "error": err}
        cid = str(getattr(s, "conv_key", "") or "")
        cwd = str(inst.get("workspace") or getattr(s, "cwd", "") or "").strip()
        tpl = str(hub.HUB.cfg.get("new_chat_prompt") or hub.DEFAULTS["new_chat_prompt"])
        prompt = hub.fill_checkin_prompt(tpl, cid, cwd)
        item = {"composerId": composer_id, "name": s.name, "prompt": prompt}
        spec = getattr(s, "ext_open_spec", None) or {}
        if spec.get("modelConfig"):
            item["modelConfig"] = spec["modelConfig"]
        s.ext_instance = inst["id"]
        hub.HUB.prebound_composers[cid] = {"uuid": composer_id, "ts": time.time(),
                                           "instance": inst["id"]}
        r = ext_bus.call(inst["id"], "batchOpen", {
            "items": [item], "autoSubmit": True, "skipMountWait": True,
        }, timeout=self._ext_open_timeout(1))
        results = [x for x in (r.get("results") or []) if isinstance(x, dict)]
        item_ok = results[0].get("ok", True) if results else True
        ok = bool(r.get("ok")) and item_ok
        ack_lost = r.get("code") == "TIMEOUT"
        label = (who or "控制台").strip()[:16]
        if not ok and not ack_lost:
            why = (results[0].get("error") if results else None) or r.get("error") or "createNew 失败"
            hub.log_event("重开无头对话失败 conv={} tab={}：{}".format(cid, s.name, why))
            return {"ok": False, "error": why, "instance": inst}
        hub.log_event("重开无头对话 conv={} tab={} 窗口={}（{}）{}".format(
            cid, s.name, inst.get("label"), label, "· 回执超时但请求已送达" if ack_lost else ""))
        try:
            hub.HUB.add_message(s, {
                "role": "sys", "ts": hub.now_hms(),
                "html": "🔁 已在 Cursor 窗口「{}」重新无头开出这个对话（{} 发起）· 等它报到".format(
                    html_mod.escape(inst.get("label") or ""), html_mod.escape(label)),
            })
            s.rev += 1
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "instance": inst, "conversation_id": cid, "composerId": composer_id,
                "ack_lost": ack_lost}

    def ext_takeover_new(self, session_ids, instance_id="", who="", name="", model=None):
        """新开一个 Cursor 对话来接手一个或几个中断的会话（bajie「新建并转移」的rxyy MCP 版）。

        步骤：在窗口里无头开 1 个待命对话 → 后台线程等它报到进 pending →
        原样走 share_takeover / share_takeover_many（接手单、别名归并、⏳接手在途、
        规则全文去重全复用）→ 把 hub 指定的 composerId 直接钉到被接手的 tab 上。
        `name` 覆盖新对话标题；留空仍用「接手·原名」。
        """
        ids = [str(x) for x in (session_ids if isinstance(session_ids, (list, tuple))
                                else [session_ids]) if str(x or "").strip()]
        if not ids:
            return {"ok": False, "error": "没有要接手的会话"}
        srcs = [hub.HUB.sessions.get(x) for x in ids]
        if any(s is None for s in srcs):
            return {"ok": False, "error": "要接手的会话不存在"}
        inst, err = self._ext_pick_instance(instance_id, hub.task_root_of(srcs[0]) or srcs[0].cwd)
        if inst is None:
            return {"ok": False, "error": err}
        custom = str(name or "").strip()[:40]
        if custom:
            tab_name = custom
        elif len(ids) == 1:
            tab_name = "接手·{}".format((srcs[0].name or "")[:24])
        else:
            tab_name = "接手·{}个会话".format(len(ids))
        # Transfer uses the same per-model validation and parameter mapping as
        # ordinary creation; the chosen model must survive into batchOpen.
        extra = {"models": [model]} if model else {}
        r = self.ext_batch_open(inst["id"], 1, names=[tab_name], who=who, **extra)
        if not r.get("ok"):
            return r
        opened = r["opened"][0]
        label = (who or "控制台").strip()[:16]
        for s in srcs:
            hub.HUB.add_message(s, {
                "role": "sys", "ts": hub.now_hms(),
                "html": "🪟 已在 Cursor 窗口「{}」无头开了新对话 <b>{}</b>，等它报到后自动把接手单派给它（{} 发起）".format(
                    html_mod.escape(inst.get("label") or ""), html_mod.escape(opened["name"]),
                    html_mod.escape(label)),
            })
            s.rev += 1
        threading.Thread(
            target=self._ext_dispatch_when_checked_in,
            args=(opened["session_id"], opened["composerId"], ids, label,
                  time.time() + self.EXT_CHECKIN_WAIT_SECS),
            daemon=True).start()
        hub.log_event("窗口总线接手：为 {} 在窗口「{}」开了新对话 conv={}，等报到派单".format(
            ",".join(ids), inst.get("label"), opened["conversation_id"]))
        return {"ok": True, "opened": opened, "instance": inst, "count": len(ids),
                "target": opened["name"], "pending_checkin": True}

    def _ext_dispatch_when_checked_in(self, shell_id, composer_id, session_ids, who, deadline):
        """等无头开出的对话报到（壳 connected 且挂着报到 zhi），然后原路派接手单。"""
        while time.time() < deadline:
            t = hub.HUB.sessions.get(shell_id)
            if t is None:
                hub.log_event("窗口总线接手：壳 {} 在报到前已不在（被收编或删除），停止等待".format(shell_id))
                return
            gate = getattr(t, "death_info", None) or {}
            if not t.connected and gate.get("gate"):
                # 对话开出来了但 AI 那头被并发闸拒了（网关口令名额用完等）：别干等到
                # 超时，当场把原因和下一步写到被接手的 tab 上
                for sid in session_ids:
                    s = hub.HUB.sessions.get(sid)
                    if s is not None:
                        hub.HUB.add_message(s, {
                            "role": "sys", "ts": hub.now_hms(),
                            "html": "⚠ 新开的对话没跑起来：{}。接手单没派出去——{}".format(
                                html_mod.escape(str(gate.get("reason") or "并发满")),
                                html_mod.escape(str(gate.get("advice") or "腾出名额后重试"))),
                        })
                        s.rev += 1
                hub.log_event("窗口总线接手：新对话撞并发闸（{}），停止等待".format(gate.get("reason")))
                return
            if t.connected and t.pending is not None:
                try:
                    if len(session_ids) > 1:
                        r = self.share_takeover_many(session_ids, shell_id, who=who)
                    else:
                        r = self.share_takeover(session_ids[0], shell_id, who=who)
                except Exception as e:  # noqa: BLE001
                    r = {"ok": False, "error": str(e)}
                if r.get("ok"):
                    # 壳已被 alias_shell_into 归并进第 1 张：它的 Cursor 对话就是这个
                    # composerId，直接钉上，别等身份校准再扫库
                    src = hub.HUB.sessions.get(session_ids[0])
                    if src is not None and composer_id:
                        with hub.HUB.lock:
                            for x in hub.HUB.sessions.values():
                                if x.id != src.id and x.cursor_uuid == composer_id:
                                    x.cursor_uuid, x.transcript_path, x.uuid_verified = None, None, False
                        src.cursor_uuid, src.transcript_path = composer_id, None
                        src.uuid_verified = True
                        src.rev += 1
                    hub.log_event("窗口总线接手：新对话已报到，接手单已派（{}）".format(
                        r.get("target") or shell_id))
                else:
                    hub.log_event("窗口总线接手：派单失败 {}".format(r.get("error")))
                    for sid in session_ids:
                        s = hub.HUB.sessions.get(sid)
                        if s is not None:
                            hub.HUB.add_message(s, {
                                "role": "sys", "ts": hub.now_hms(),
                                "html": "⚠ 新对话已报到，但派接手单失败：{}".format(
                                    html_mod.escape(str(r.get("error") or ""))),
                            })
                            s.rev += 1
                return
            time.sleep(1.0)
        for sid in session_ids:
            s = hub.HUB.sessions.get(sid)
            if s is not None:
                hub.HUB.add_message(s, {
                    "role": "sys", "ts": hub.now_hms(),
                    "html": "⚠ 无头开的新对话 {} 秒内没报到，接手单没派出去；到那个 Cursor 窗口看看它有没有开起来，或换一个在线 agent 接手".format(
                        int(self.EXT_CHECKIN_WAIT_SECS)),
                })
                s.rev += 1
        hub.log_event("窗口总线接手：壳 {} 超时未报到".format(shell_id))

    def ext_open_cursor(self, session_id):
        """把某个 tab 绑着的 Cursor 对话调到前台：优先它出生的窗口，否则挨个窗口试。"""
        import ext_bus
        s = hub.HUB.sessions.get(session_id)
        if s is None:
            return {"ok": False, "error": "会话不存在"}
        uid = str(getattr(s, "cursor_uuid", "") or "")
        if not uid:
            return {"ok": False, "error": "这个 tab 还没定位到 Cursor 对话"}
        items = ext_bus.live_instances()
        if not items:
            return {"ok": False, "error": "没有装了窗口总线扩展的 Cursor 窗口在线"}
        born = str(getattr(s, "ext_instance", "") or "")
        items.sort(key=lambda x: 0 if x["id"] == born else 1)
        errors = []
        for inst in items:
            r = ext_bus.call(inst["id"], "openCursor", {"composerId": uid}, timeout=8)
            if r.get("ok"):
                return {"ok": True, "instance": inst, "via": r.get("via")}
            errors.append("{}: {}".format(inst.get("label"), r.get("error")))
        return {"ok": False, "error": "；".join(errors) or "没有窗口能打开它"}

    def list_cursor_composers(self, session_id=""):
        """控制台「绑定 Cursor 窗口」：最近对话 + 哪个 tab 占着。

        名字来自 composerHeaders，不扫 cursorDiskKV。扩展 listComposers 只有 UUID。
        """
        s = hub.HUB.sessions.get(session_id) if session_id else None
        try:
            headers = hub.list_composer_headers()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e), "items": []}
        owners = {}
        with hub.HUB.lock:
            for x in hub.HUB.sessions.values():
                uid = str(getattr(x, "cursor_uuid", "") or "").strip()
                if uid:
                    owners[uid] = x
                    owners[uid.replace("-", "").lower()] = x
        items = []
        for it in headers:
            uid = it["id"]
            owner = owners.get(uid) or owners.get(uid.replace("-", "").lower())
            current = bool(s is not None and owner is not None and owner.id == s.id)
            if current:
                status = "current"
            elif owner is not None:
                status = "occupied"
            else:
                status = "available"
            items.append({
                "id": uid,
                "name": it.get("name") or uid[:8],
                "subtitle": it.get("subtitle") or "",
                "updated": it.get("updated") or 0,
                "status": status,
                "owner_id": owner.id if owner else "",
                "owner_name": (owner.name if owner else "") or "",
                "current": current,
            })
        bound = str(getattr(s, "cursor_uuid", "") or "") if s else ""
        return {"ok": True, "items": items, "bound": bound}

    def bind_cursor_composer(self, session_id, composer_id=""):
        """人手把网页 tab 钉到某个 Cursor 对话上（bajie「绑定 Cursor 窗口…」）。

        认领了活的 tab 持有的 uid 不许抢（08-12 铁律）；空 composer_id = 解绑。
        """
        s = hub.HUB.sessions.get(session_id)
        if s is None:
            return {"ok": False, "error": "会话不存在"}
        uid = str(composer_id or "").strip()
        if not uid:
            old = str(getattr(s, "cursor_uuid", "") or "")
            s.cursor_uuid = None
            s.transcript_path = None
            s.uuid_verified = False
            s.rev += 1
            hub.HUB.save_state()
            hub.HUB.add_message(s, {
                "role": "sys", "ts": hub.now_hms(),
                "html": "已解开与 Cursor 对话的绑定" + (
                    "（原 {}）".format(html_mod.escape(old[:8])) if old else ""),
            })
            return {"ok": True, "unbound": True}
        if not hub.looks_like_composer_id(uid):
            return {"ok": False, "error": "不是合法的 Cursor 会话 ID"}
        with hub.HUB.lock:
            owners = [x for x in hub.HUB.sessions.values()
                      if x.id != s.id and str(getattr(x, "cursor_uuid", "") or "") == uid]
            claimed = [x for x in owners if hub.HUB._claimed_task(x)]
            if claimed:
                return {"ok": False, "error": "已被「{}」占用（正在干活），不能抢".format(
                    claimed[0].name)}
            for x in owners:
                x.cursor_uuid = None
                x.transcript_path = None
                x.uuid_verified = False
                x.rev += 1
            s.cursor_uuid = uid
            s.transcript_path = None
            s.uuid_verified = True
            s.rev += 1
        shown = (hub.read_cursor_title(uid) or uid[:8]).strip()
        hub.HUB.save_state()
        hub.HUB.add_message(s, {
            "role": "sys", "ts": hub.now_hms(),
            "html": "已绑定 Cursor 对话 <b>{}</b>（{}）".format(
                html_mod.escape(shown), html_mod.escape(uid[:8])),
        })
        return {"ok": True, "composer_id": uid, "name": shown}

    # ---------- 「+」直拉本机 agent（Cursor SDK，独立 runner 进程） ----------
    _sdk_installed_cache = None

    def _sdk_installed(self):
        if Api._sdk_installed_cache is None:
            try:
                import importlib.util
                Api._sdk_installed_cache = importlib.util.find_spec("cursor_sdk") is not None
            except Exception:
                Api._sdk_installed_cache = False
        return Api._sdk_installed_cache

    def _spawn_sdk_runner(self, key, cwd, prompt_file, model, task_name,
                          resume_agent="", backend="cli"):
        from frozen_boot import hidden_popen_kwargs, spawn_argv
        env = dict(os.environ)
        if backend == "sdk":
            env["CURSOR_API_KEY"] = str(hub.HUB.cfg.get("cursor_api_key") or "").strip()
        else:
            # CLI 后端必须走已登录账号的订阅额度；环境里若飘着 API key 会被 CLI
            # 优先采用、悄悄变成 API 计费——这里强制摘掉
            env.pop("CURSOR_API_KEY", None)
        cmd = spawn_argv(hub.APP_DIR / "sdk_runner.py", "run", "--key", key,
                     "--cwd", cwd, "--prompt-file", str(prompt_file), "--model", model,
                     "--backend", backend)
        if task_name:
            cmd += ["--task-name", task_name]
        if resume_agent:
            cmd += ["--resume-agent", resume_agent]
        kw = hidden_popen_kwargs()
        kw["env"] = env
        subprocess.Popen(cmd, cwd=str(hub.APP_DIR), **kw)

    def _backend_ready_error(self, backend):
        """后端可用性检查；就绪返回 None，否则返回给用户看的报错。"""
        import sdk_runner as reg
        if backend == "cli":
            if not reg.find_agent_cmd():
                return ("Cursor CLI 未安装：Windows PowerShell 运行 "
                        "irm 'https://cursor.com/install?win32=true' | iex，"
                        "装完在终端 agent login 登录账号")
            return None
        if not self._sdk_installed():
            return "cursor-sdk 未安装：pip install cursor-sdk"
        if not str(hub.HUB.cfg.get("cursor_api_key") or "").strip():
            return ("未配置 Cursor API Key：⚙设置 → Cursor API Key（去 cursor.com/dashboard"
                    " → Integrations 生成）")
        return None

    def sdk_spawn(self, cwd, task_name="", first_task="", model=""):
        """直接在本机拉起一个 agent 会话：独立 runner 进程运行（不随 hub 重启死掉），
        agent 按接入提示词经 MCP 用 zhi 报到、tab 自动出现。
        后端 cli=登录账号订阅额度（默认，适配切号）；sdk=API key 计费。"""
        import sdk_runner as reg
        cwd = str(cwd or "").strip().strip('"')
        if not cwd or not Path(cwd).is_dir():
            return {"ok": False, "error": "工作目录不存在: {}".format(cwd or "（空）")}
        try:
            self._ensure_one_project_mcp(cwd)
        except Exception:
            pass
        backend = str(hub.HUB.cfg.get("agent_backend") or "cli").strip().lower()
        if backend not in ("cli", "sdk"):
            backend = "cli"
        err = self._backend_ready_error(backend)
        if err:
            return {"ok": False, "error": err}
        cid = uuid.uuid4().hex[:8]
        tpl = str(hub.HUB.cfg.get("new_chat_prompt") or hub.DEFAULTS["new_chat_prompt"])
        tn = (task_name or "").strip()[:40]
        ft = (first_task or "").strip()
        # 追加行与报到词同吃 1024B 字节顶——闸后裸拼会把刚过闸的报到词重新顶超
        # （08-31 同类路径）。首任务全文另有下面的预建壳队列兜底，截了不丢。
        prompt = hub.append_spawn_extras(hub.fill_checkin_prompt(tpl, cid, cwd), tn, ft)
        key = uuid.uuid4().hex[:8]
        model = (model or hub.HUB.cfg.get("sdk_model") or "auto").strip() or "auto"
        try:
            reg.PROMPT_DIR.mkdir(exist_ok=True)
            pf = reg.PROMPT_DIR / "{}.log".format(key)  # .log 防企业 DLP 加密 .txt
            pf.write_text(prompt, encoding="utf-8")
            reg.upsert_entry(key, conv=cid, cwd=cwd, task_name=tn or "新任务",
                             model=model, backend=backend, status="launching", note="",
                             created=hub.now_full(), agent_id="", pid=None)
            self._spawn_sdk_runner(key, cwd, pf, model, tn, backend=backend)
        except Exception as e:  # noqa: BLE001
            try:
                reg.upsert_entry(key, status="error", note="拉起 runner 失败: {}".format(e))
            except Exception:
                pass
            return {"ok": False, "error": "拉起 runner 失败: {}".format(e)}
        if ft:
            # 预建壳 + 任务排队：报到那一下 zhi 立即拿到任务。不排队的话，无头
            # agent 按报到纪律阻塞在「待命等派任务」上，KEEPALIVE 圈到天荒地老
            # （提示词尾部那份任务文本保留作兜底，两份内容相同不冲突）
            try:
                shell = hub.HUB.create_spawn_shell(cid, cwd, tn or "内置 agent")
                self.queue_message(shell.id, ft, [], who="派单", force=True)
            except Exception as e:  # noqa: BLE001
                hub.log_event("预建内置壳失败（任务仍在提示词里兜底）: {}".format(e))
        hub.log_event("本机 agent 拉起 key={} conv={} cwd={} model={} backend={}".format(
            key, cid, cwd, model, backend))
        return {"ok": True, "key": key, "conversation_id": cid}

    def sdk_list(self):
        """本机 agent 会话列表（注册表 + 存活探测 + 关联控制台 tab）。"""
        import sdk_runner as reg
        with hub.HUB.lock:
            conv_tab = {s.conv_key: s.id for s in hub.HUB.sessions.values()}
        out = []
        for e in sorted(reg.load_registry(),
                        key=lambda x: x.get("created") or "", reverse=True):
            e = dict(e)
            alive = reg.pid_alive(e.get("pid"))
            st = e.get("status") or ""
            if st in ("launching", "starting", "running") and not alive:
                st = "dead"  # runner 进程没了但状态没来得及更新（强杀/断电）
            e["alive"] = alive
            e["status_effective"] = st
            e["tab_id"] = conv_tab.get(e.get("conv") or "")
            out.append(e)
        import sdk_runner as reg2
        backend = str(hub.HUB.cfg.get("agent_backend") or "cli").strip().lower()
        return {"ok": True, "agents": out,
                "backend": backend if backend in ("cli", "sdk") else "cli",
                "cli_installed": bool(reg2.find_agent_cmd()),
                "sdk_installed": self._sdk_installed(),
                "api_key_set": bool(str(hub.HUB.cfg.get("cursor_api_key") or "").strip()),
                "model": hub.HUB.cfg.get("sdk_model") or "auto"}

    def sdk_stop(self, key):
        import sdk_runner as reg
        entry = next((e for e in reg.load_registry() if e.get("key") == key), None)
        if not entry:
            return {"ok": False, "error": "记录不存在"}
        pid = entry.get("pid")
        if pid and reg.pid_alive(pid):
            try:
                # /T 连带杀掉 SDK 拉起的本地执行器子进程
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, creationflags=0x08000000)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": "结束进程失败: {}".format(e)}
        reg.upsert_entry(key, status="stopped", note="用户手动停止")
        hub.log_event("SDK agent 停止 key={} pid={}".format(key, pid))
        return {"ok": True}

    def sdk_resume(self, key, message=""):
        """续聊：对已结束/出错的本机 agent 用原 agent_id 继续（SDK 保留完整上下文）。"""
        import sdk_runner as reg
        entry = next((e for e in reg.load_registry() if e.get("key") == key), None)
        if not entry:
            return {"ok": False, "error": "记录不存在"}
        if entry.get("pid") and reg.pid_alive(entry.get("pid")):
            return {"ok": False, "error": "该 agent 还在运行中，直接在对应会话 tab 里回复即可"}
        aid = entry.get("agent_id") or ""
        if not aid:
            return {"ok": False, "error": "无 agent_id（当初未成功启动），请重新拉起一个"}
        backend = str(entry.get("backend") or hub.HUB.cfg.get("agent_backend") or "cli").lower()
        if backend not in ("cli", "sdk"):
            backend = "cli"
        err = self._backend_ready_error(backend)
        if err:
            return {"ok": False, "error": err}
        msg = (message or "").strip() or (
            "继续未完成的工作。按接入规范用rxyy MCP的 zhi 报到沟通，"
            "conversation_id 沿用「{}」。".format(entry.get("conv") or ""))
        try:
            reg.PROMPT_DIR.mkdir(exist_ok=True)
            pf = reg.PROMPT_DIR / "{}-resume.log".format(key)
            pf.write_text(msg, encoding="utf-8")
            reg.upsert_entry(key, status="launching", note="续聊中")
            self._spawn_sdk_runner(key, entry.get("cwd") or str(hub.APP_DIR), pf,
                                   (entry.get("model") or hub.HUB.cfg.get("sdk_model")
                                    or "auto"),
                                   entry.get("task_name") or "", resume_agent=aid,
                                   backend=backend)
        except Exception as e:  # noqa: BLE001
            reg.upsert_entry(key, status="error", note="续聊拉起失败: {}".format(e))
            return {"ok": False, "error": "续聊拉起失败: {}".format(e)}
        hub.log_event("本机 agent 续聊 key={} agent_id={} backend={}".format(key, aid, backend))
        return {"ok": True}

    def sdk_remove(self, key):
        import sdk_runner as reg
        entry = next((e for e in reg.load_registry() if e.get("key") == key), None)
        if entry and entry.get("pid") and reg.pid_alive(entry.get("pid")):
            return {"ok": False, "error": "该 agent 还在运行，请先停止"}
        reg.remove_entry(key)
        return {"ok": True}

    def _mate_fact_note(self, mate):
        """队友清单里那一行的尾巴：它自报的项目跟它领的卡对不上就写明。

        「同项目还有谁」这句话是接手方判断「谁能碰哪块代码」的依据，光凭
        对方 tab 名就等于把误判原样传给下一个人（08-24 那次就是这么传的）。
        """
        try:
            warn = self._agent_facts(mate).get("warn") or ""
        except Exception:  # noqa: BLE001
            return ""
        return "\n  ⚠ {}——按事实它多半不在本项目，别把它当本项目的人使唤".format(warn) if warn else ""

    def _takeover_team_block(self, session):
        """接手提示词里的团队段：本会话角色纪律 + 项目公告板 + 同项目还有谁在干活。
        接手 agent 最容易踩的就是「不知道隔壁还有人在改同一个仓库」。"""
        blocks = []
        role = self._team_role(session)
        _, _seat_project, seat = self._seat_lookup(getattr(session, "conv_key", ""))
        if seat is not None:
            blocks.append("【你接手的是团队席位】{}（席位号就是上面那个 conversation_id；"
                          "用它报到，控制台会自动把你算进本项目团队，角色与分工原样继承）"
                          .format(seat.get("name") or "席位"))
        if role in self.TEAM_ROLE_LABELS:
            blocks.append("【你的角色】{}：{}".format(
                self.TEAM_ROLE_LABELS[role], self.TEAM_ROLE_RULES[role]))
        track = self._team_track(session)
        if track:
            blocks.append("【你在哪条业务线】{}（这个工作区里同时还跑着别的业务线，"
                          "只跟本业务线的人打招呼用 ji(action=\"广播\", category=\"本组\")）"
                          .format(track))
        assign = self._team_assign(session)
        if assign:
            blocks.append("【你负责的功能】{}\n同项目还有别的 agent 在做其它功能，"
                          "别去动不属于你这块的代码。".format(assign))
        scope = self._team_scope(session)
        board = self._team_board(scope["root"], scope["project"])["text"]
        if board:
            blocks.append("【项目公告板】（同项目所有 agent 共用，请先读）\n" + board)
        mates = [x for x in self._team_sessions(scope["root"], project=scope["project"])
                 if x.id != session.id]
        if mates:
            blocks.append("【同项目还有这些 agent 在跑】（改公共文件前先看控制台的文件占用看板）\n"
                          + "\n".join(
                              "- {}{}{}{}（{}，工作区 {}）：{}".format(
                                  x.name,
                                  "（{}）".format(self.TEAM_ROLE_LABELS[self._team_role(x)])
                                  if self._team_role(x) in self.TEAM_ROLE_LABELS else "",
                                  "〔业务线 {}〕".format(self._team_track(x))
                                  if self._team_track(x) else "",
                                  "〔负责 {}〕".format(self._team_assign(x))
                                  if self._team_assign(x) else "",
                                  (x.conv_key or "")[:8],
                                  Path(x.cwd).name if x.cwd else "未知",
                                  getattr(x, "agent_activity", "") or "在线")
                              + self._mate_fact_note(x)
                              for x in mates))
            blocks.append(
                "【同项目互聊】审查结论、送审、卡住、要对方停手，直接 "
                "ji(action=\"转告\", category=对方对话ID前8位)，不要等用户在中间传话。"
                "广播用 ji(action=\"广播\") 发给本项目全部队友。")
        blocks.append("【重大情况写黑板、动手前读黑板】遇到 部署/大改/提交/卡住/事故/收工 "
                      "六类情况立即 ji(action=\"黑板\", category=类别, content=一句话)；"
                      "动公共文件/部署/刚接手时先 ji(action=\"黑板\")（content 留空）读最近条目。")
        return ("\n" + "\n\n".join(blocks) + "\n") if blocks else ""

    def prepare_native_new(self, project_path, prompt=""):
        """Prepare a new native draft, independent of every existing session."""
        from urllib.parse import urlencode
        cwd = str(project_path or "").strip()
        if not cwd or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
            return {"ok": False, "error": "请选择可用的本地项目绝对路径"}
        if not isinstance(prompt, str) or len(prompt) > 24000:
            return {"ok": False, "error": "初始任务内容请控制在 24000 字以内"}
        text = prompt.strip()
        params = {"path": cwd}
        draft_path = ""
        if text:
            params["prompt"] = text
        if len(urlencode(params)) > 1400:
            directory = hub.DATA_DIR / "native-drafts"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / (uuid.uuid4().hex + ".md")
            path.write_text(text, encoding="utf-8")
            draft_path = str(path.resolve())
            params["prompt"] = "请读取 {}，完成其中的任务。".format(draft_path)
        return {"ok": True, "url": "codex://new?" + urlencode(params),
                "prompt": text, "path": draft_path, "dispatched": False,
                "model_selection": "native",
                "note": "打开 Codex 新任务草稿；模型与思考程度在原生界面选择，点击发送后才开始。"}

    def list_native_models(self, project_path=""):
        """Return the installed Codex app-server catalog for a new native task."""
        cwd = str(project_path or "").strip()
        if cwd and (not Path(cwd).is_absolute() or not Path(cwd).is_dir()):
            return {"ok": False, "models": [], "error": "项目目录不可用"}
        import codex_desktop
        return codex_desktop.list_new_task_models(cwd or None)

    def create_native_task(self, project_path, prompt="", model="gpt-6-astra", effort="max"):
        """Create, configure and open one Codex task; send non-empty text once via Desktop."""
        cwd = str(project_path or "").strip()
        if not cwd or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
            return {"ok": False, "error": "请选择可用的本地项目绝对路径"}
        if not isinstance(prompt, str) or len(prompt) > 24000:
            return {"ok": False, "error": "初始任务内容请控制在 24000 字以内"}
        model = str(model or "").strip()
        effort = str(effort or "").strip()
        if not model or len(model) > 120 or not effort or len(effort) > 40:
            return {"ok": False, "error": "请选择可用的 Codex 模型和思考程度"}
        import codex_desktop
        try:
            created = codex_desktop.create_configured_thread(cwd, model, effort)
        except Exception as exc:  # noqa: BLE001
            thread_id = str(getattr(exc, "thread_id", "") or "")
            if thread_id:
                return {"ok": False, "created": True, "opened": False,
                        "dispatched": False, "config_unknown": True,
                        "thread_id": thread_id, "error": str(exc)[:200]}
            return {"ok": False, "error": str(exc)[:200]}
        thread_id = created["thread_id"]
        url = "codex://threads/" + thread_id
        title = next((line.strip() for line in prompt.splitlines() if line.strip()), "")
        title = title[:40] or "Codex 空白任务"
        try:
            shell = hub.HUB.create_spawn_shell(
                "codex-native-" + thread_id, cwd, title,
                note="Codex 原生任务已创建，等待桌面连接。")
            hub.runtime_adapter.apply_metadata(shell, {
                "runtime_kind": "codex", "native_thread_id": thread_id})
            shell.recon_deadline = None
            shell.end_reason = ""
            hub.HUB.save_state()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "created": True, "opened": False,
                    "dispatched": False, "thread_id": thread_id, "url": url,
                    "error": "任务已创建，但未能登记到rxyy MCP：" + str(exc)[:160]}
        opened = self.open_codex_link(url)
        base = {"thread_id": thread_id, "url": url, "model": model,
                "effort": effort, "created": True, "opened": bool(opened.get("ok")),
                "prompt_present": bool(prompt.strip()), "session_id": shell.id}
        if not opened.get("ok"):
            return {**base, "ok": False, "dispatched": False,
                    "error": "任务已创建并保存设置，但未能打开 Codex：" +
                    str(opened.get("error") or "打开失败")[:160]}
        text = prompt.strip()
        if not text:
            return {**base, "ok": True, "dispatched": False,
                    "note": "已创建并打开空白任务；模型和思考程度已经保存。"}
        delivery_id = str(uuid.uuid4())
        sent = codex_desktop.start_initial_text(
            thread_id, cwd, text, model, effort, delivery_id)
        if not sent.get("ok"):
            return {**base, "ok": True, "dispatched": False,
                    "delivery_unknown": bool(sent.get("delivery_unknown")),
                    "warning": str(sent.get("error") or "任务内容未发送")[:200],
                    "delivery_id": delivery_id}
        return {**base, "ok": True, "dispatched": True,
                "delivery": sent.get("delivery"), "turn_id": sent.get("turn_id"),
                "delivery_id": delivery_id,
                "note": "已按所选模型和思考程度开始任务。"}

    def prepare_native_transfer(self, session_id):
        """Prepare a local native composer using the documented codex:// link.

        The desktop model selector remains authoritative. A prepared composer
        is not a dispatched task and does not retire the source session.
        """
        from urllib.parse import urlencode
        s = hub.HUB.sessions.get(session_id)
        if s is None:
            return {"ok": False, "error": "会话不存在"}
        cwd = str(hub.task_root_of(s) or s.cwd or "")
        if not cwd or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
            return {"ok": False, "error": "需要一个可用的本地项目目录才能打开 Codex 任务"}
        with s.lock:
            msgs = list(s.messages)
        recent = []
        for m in msgs:
            if m.get("role") in ("ai", "user"):
                text = _html_text(m.get("html"))
                if text:
                    recent.append("{}：{}".format("用户" if m["role"] == "user" else "AI", text))
        body = hub.runtime_adapter.takeover_prompt(s, "\n\n".join(recent[-12:]))
        directory = hub.DATA_DIR / "native-handoffs"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (uuid.uuid4().hex + ".md")
        path.write_text(body, encoding="utf-8")
        # Long histories in URI query strings exceed Windows URL-handler
        # limits. The durable local handoff contains the full text.
        prompt = "请读取 {} 并接手其中的任务，继续完成尚未完成的工作。".format(path.resolve())
        url = "codex://new?" + urlencode({"path": cwd, "prompt": prompt})
        source = (hub.runtime_adapter.valid_thread_id(getattr(s, "native_thread_id", ""))
                  if hub.runtime_adapter.kind(s) == "codex" else "")
        return {"ok": True, "url": url, "prompt": body, "path": str(path.resolve()),
                "source_url": "codex://threads/" + source if source else "",
                "model_selection": "native", "dispatched": False,
                "note": "将在 Codex 新任务中带入接续内容；可选择模型和思考强度后发送。"}

    def open_codex_link(self, url):
        """Open only documented local Codex deep links through the OS handler."""
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(str(url or ""))
        if parsed.scheme != "codex" or parsed.netloc not in ("new", "threads"):
            return {"ok": False, "error": "不是 Codex 任务链接"}
        if parsed.netloc == "threads":
            if not hub.runtime_adapter.valid_thread_id(parsed.path.strip("/")):
                return {"ok": False, "error": "Codex 任务 ID 无效"}
        else:
            cwd = (parse_qs(parsed.query).get("path") or [""])[0]
            if not cwd or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
                return {"ok": False, "error": "Codex 项目目录不可用"}
        try:
            os.startfile(str(url))
            return {"ok": True, "dispatched": False}
        except OSError as exc:
            return {"ok": False, "error": "无法打开 Codex，请确认已安装桌面应用：" + str(exc)}

    def get_takeover_prompt(self, session_id, digest_repeat=False, stay_put=False, target_runtime=None):
        """一键接手：生成让另一个「活着的」agent 接手本会话、继续未完成工作的提示词。

        不依赖 Cursor transcript 定位（进行中的会话 Cursor 懒写盘、常搜不到，正是
        「找挂掉的会话不准」的根因）——直接把 conversation_id + 聊天记录 .md 路径 +
        最近消息摘要打包。单张默认叫接手方改用原 ID 复活原 tab；stay_put=True
        （多选合并）则沿用当前会话，原 ID 只作读记录。
        """
        s = hub.HUB.sessions.get(session_id)
        if not s:
            return {"ok": False, "error": "会话不存在"}
        with s.lock:
            msgs = list(s.messages)

        def _plain(h):
            t = re.sub(r"<[^>]+>", "", h or "")
            return re.sub(r"[ \t\r\n]+", " ", html_mod.unescape(t)).strip()

        recent = []
        for m in msgs:
            role = m.get("role")
            if role not in ("ai", "user"):
                continue  # 跳过系统气泡，只留真正的对话内容
            txt = _plain(m.get("html"))
            if not txt:
                continue
            # 摘要截断必须带明确标记：此前 180 字硬切、句子断在半截，接手 agent
            # 和用户都会以为提示词本身不完整（实测反馈）
            clipped = txt[:400] + ("……〔此条已截断〕" if len(txt) > 400 else "")
            recent.append("- {}：{}".format("AI" if role == "ai" else "用户", clipped))
        recent_text = "\n".join(recent[-10:]) if recent else "（暂无消息摘要，以聊天记录文件为准）"

        if hub.runtime_adapter.is_native(s) or target_runtime in ("codex", "chatgpt"):
            text = hub.runtime_adapter.takeover_prompt(s, recent_text, stay_put=stay_put,
                                                       target=target_runtime or hub.runtime_adapter.kind(s))
            return {"ok": True, "prompt": text, "conversation_id": s.conv_key,
                    "task_name": s.name, "runtime_kind": hub.runtime_adapter.kind(s)}

        # 记录文件兜底重建：文件可能被旧版跨天清理删掉、或会话从未落盘成功。
        # 接手 agent 第一步就是读它，死链接=接手直接失明；用内存里的最近消息
        # 尽力重建（内存只保留最近 max_messages 条，故文件头标注「可能不完整」）。
        try:
            if not s.file_path:
                s.file_path = hub.HUB._alloc_history_path(s.name, exclude_id=s.id)
            if not os.path.exists(s.file_path):
                blocks = [
                    "# rxyy MCP 会话记录\n\n"
                    "- 会话: {} ({})\n- 工作目录: {}\n- 开始时间: {}\n\n---\n".format(
                        s.name, s.id, s.cwd or "未知", s.created_at),
                    "\n> ⚠ 原记录文件曾被清理，本文件由内存中最近消息重建，"
                    "早期内容可能缺失 · {}\n".format(hub.now_full()),
                ]
                for m in msgs:
                    role = m.get("role")
                    if role not in ("ai", "user"):
                        continue
                    label = "🤖 AI" if role == "ai" else "🧑 用户"
                    txt = _plain(m.get("html"))
                    if not txt:
                        continue
                    blocks.append("\n## {} · {}\n\n{}\n".format(label, m.get("ts") or "", txt))
                    opts = m.get("options") or []
                    if opts:
                        blocks.append("\n（提供选项: {}）\n".format(" | ".join(opts)))
                Path(s.file_path).write_text("".join(blocks), encoding="utf-8")
                hub.HUB.maybe_decrypt(s.file_path)
                hub.log_event("接手提示词：记录文件缺失，已由内存重建 {}".format(s.file_path))
        except Exception:
            pass

        cid = s.conv_key if s.conv_key != "__default__" else ""
        # 前一个 agent 的完整 Cursor 对话记录（jsonl）：Cursor 自动全量落盘，不依赖
        # agent 是否用 zhi 上报——接手却发现前一个 agent 埋头没报进度时（07-28 用户实测
        # 痛点：接手只能靠稀疏的 .md 猜到哪一步），这是唯一能还原它全部工作（工具调用/
        # 改了哪些文件/结论）的来源。
        # 08-28 改列清单（用户「新agent不能快速定位上一个agent做了什么」）：以前只给
        # 单指针（transcript_path → cursor_uuid 拼路径 → 兜底搜），f9c313ed 实测该
        # 指针指向的是报到失败空壳的 1.6KB 流水、与原对话毫无关系，接手方按它读了个
        # 寂寞。_locate_transcripts 逐份验过内容确实提到这个对话，比内存指针可信；
        # 单指针只在没被清单覆盖时垫底补进（老对话可能早于内容索引窗口）。
        listed = self._locate_transcripts(s.cwd, s.conv_key)
        tpath = getattr(s, "transcript_path", None)
        if not (tpath and os.path.isfile(tpath)):
            tpath = None
            _cu = getattr(s, "cursor_uuid", None)
            if _cu:
                _guess = (Path.home() / ".cursor" / "projects"
                          / hub.cursor_project_slug(s.cwd)
                          / "agent-transcripts" / _cu / (_cu + ".jsonl"))
                if _guess.is_file():
                    tpath = str(_guess)
            if not tpath:
                tpath = self._locate_transcript(s.cwd, s.conv_key)
        transcripts = list(listed)
        if tpath and not any(str(tpath) in x for x in transcripts):
            transcripts.append("{}（内存指针，内容未核对，仅供兜底）".format(tpath))
        prompt = self._build_takeover_prompt(
            cid, s.cwd or "未知", s.name or "未命名任务",
            s.file_path or "（无记录文件）", recent_text, transcripts,
            self._takeover_team_block(s),
            zt_trail=list(getattr(s, "zt_trail", []) or []),
            digest_repeat=digest_repeat,
            uncollected=self._takeover_uncollected_block(s, cid),
            stay_put=stay_put)
        return {"ok": True, "prompt": prompt, "conversation_id": cid,
                "name": s.name or "未命名任务", "file": s.file_path or ""}

    @staticmethod
    def _takeover_uncollected_block(s, cid):
        """接手单里的「有用户回复待收」段：存着什么、怎么收。没有就返回空串不占地方。

        09-02 17:09 第六任 zhi(wait=false, card) 后断线，rxyy 对那张卡回了三条（两条
        带图），全存在 buffered_reply 里没人取；接手单只有 400 字的对话摘要，一个字
        没提「有回复存着」。第七任按纪律一上来又发了张带卡的新提问，存着的旧回复被
        拿去当场答掉、新卡秒关（17:49）。三处会存着用户的话：
        ① 前任还连着、只发不等的提问挂着（pending + wait_deferred）、用户已回 → buffered_reply
        ② 前任断线时 buffered_reply 转进了 s.queued（_client_disconnect）
        ③ 用户提前排队 / 被吞回复救援转进 s.queued
        统一写进接手单，并把收法写死：先 zhi(message 留空) 收一次，再发新提问。"""
        pend = getattr(s, "pending", None)
        deferred = pend is not None and bool(getattr(s, "wait_deferred", False))
        buf = getattr(s, "buffered_reply", None)
        if not isinstance(buf, dict):
            buf = None
        queued = [e for e in (getattr(s, "queued", None) or [])
                  if isinstance(e, dict) and Api._may_take_the_reply_slot(e.get("who"))]
        if not deferred and buf is None and not queued:
            return ""

        def _fnames(files):
            return [str(f.get("name") or "") for f in (files or []) if isinstance(f, dict)]

        def _clip(t, n):
            t = str(t or "").strip()
            return (t[:n] + "…〔已截断，收到时是全文〕") if len(t) > n else t

        lines = ["【前任断线前 · 有用户回复待收 · 先收再开工】"]
        if deferred:
            asks = bool(pend.get("options") or pend.get("card"))
            lines.append("- 前任最后一条提问是 zhi(wait=false) 只发不等{}：{}".format(
                "、带选项/决策卡等用户拍板" if asks else "（纯进展汇报，没要用户答什么）",
                _clip(pend.get("message"), 200) or "（空正文）"))
            if buf is None:
                lines.append("- 用户{}回复，提问仍挂在控制台。".format("还没" if asks else "没有"))
        if buf is not None:
            _, body = hub.session_core.split_memory_digest(buf.get("user_input") or "")
            sel = [str(x) for x in (buf.get("selected_options") or []) if x]
            n_img = len(buf.get("images") or [])
            fn = _fnames(buf.get("files"))
            lines.append("- **用户已经回复了、回复存在控制台没人取**（前任没来收）：")
            if sel:
                lines.append("  · 选的选项：" + "、".join(sel))
            if body.strip():
                lines.append("  · 回复正文：" + _clip(body, 600))
            if n_img:
                lines.append("  · 附 {} 张图片（收的时候随回复一起给你）".format(n_img))
            if fn:
                lines.append("  · 附文件：" + "、".join(fn))
        if queued:
            lines.append("- 队列里有 {} 条用户消息等着送给 AI（含断线时从缓存转进来的回复）：".format(
                len(queued)))
            for e in queued[:5]:
                extra = []
                if e.get("images"):
                    extra.append("{} 张图".format(len(e.get("images") or [])))
                fn = _fnames(e.get("files"))
                if fn:
                    extra.append("文件 " + "、".join(fn))
                lines.append("  · " + (_clip(e.get("text"), 300) or "（无正文）")
                             + ("（附 " + "，".join(extra) + "）" if extra else ""))
            if len(queued) > 5:
                lines.append("  · …还有 {} 条".format(len(queued) - 5))
        lines.append(
            "- **怎么收**：zt 接令之后、发任何新提问之前，先调一次 "
            "zhi(conversation_id=\"{}\"，message 留空)（阻塞收）——存着的回复 / 排队消息"
            "会立刻交给你，正文开头带「[上一条提问的回复|…]」标记。别一上来就发带决策卡的"
            "新提问：旧回复答不了新卡、会被控制台扣住，你得多等一轮用户再答。".format(
                cid or "原会话 ID"))
        return "\n".join(lines) + "\n"

    @staticmethod
    def _locate_transcript(cwd, conv_key):
        try:
            found = hub.find_cursor_transcript(cwd, conv_key)
        except Exception:
            return None
        return str(found) if found else None

    @staticmethod
    def _locate_transcripts(cwd, conv_key, limit=3):
        """列出提到这个 conversation_id 的 Cursor 流水（新→旧，带体积和时间）。

        一个对话常留下好几份流水：原班 agent 干到一半挂了、后来又被接手过……
        只取最新那份会把「真正干了活的那份大流水」漏掉——7c6a4e74 就是这样：
        最新的只是接手 6 分钟后又欠费死掉的尝试，271KB 的主力流水排在第二。
        """
        cid = (conv_key or "").strip()
        if not cid or cid == "__default__":
            return []
        folder = (Path.home() / ".cursor" / "projects"
                  / hub.cursor_project_slug(cwd) / "agent-transcripts")
        try:
            entries = sorted(
                ((p.stat().st_mtime, p) for p in folder.glob("*/*.jsonl")
                 if p.parent.name == p.stem),  # 跳过子 agent 流水，接手用不上
                reverse=True)[:80]  # 只翻最近 80 份，别为找回一个对话把整盘扫穿
        except OSError:
            return []
        needle = cid.encode("utf-8")
        out = []
        for mtime, p in entries:
            try:
                if p.stat().st_size > 32 * 1024 * 1024:
                    continue
                if needle not in p.read_bytes():
                    continue
            except OSError:
                continue
            out.append("{}（{}KB · {}）".format(
                p, max(1, p.stat().st_size // 1024),
                datetime.datetime.fromtimestamp(mtime).strftime("%m-%d %H:%M")))
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def _takeover_git_evidence(cwd, limit=8):
        """接手词里的「git 现场」：原项目目录最近提交 + 未提交改动数。

        前任做到哪一步，最硬的证据在仓库里：聊天记录可能没收尾（08-28 f9c313ed
        实测——前任把 4 笔修复全提交推送了，记录里一个字没有，接手方翻了七八处
        才从 git log 里对出进度）；Cursor 流水又只有 Cursor agent 才留（Codex
        工人没有）。git 不挑 agent 类型，谁干活都留痕。跑不成（不是仓库/git
        超时/机器抢盘）就整段省略——一次 git 故障不许挡住接手词生成。
        """
        if not (cwd and os.path.isdir(str(cwd))):
            return ""

        def _run(*args):
            try:
                flags = 0x08000000 if os.name == "nt" else 0  # 无窗口：hub 是 pythonw
                r = subprocess.run(["git", "-C", str(cwd)] + list(args),
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=8, creationflags=flags)
                return r.stdout.strip() if r.returncode == 0 else ""
            except Exception:
                return ""

        log = _run("log", "--format=%h %ad %s", "--date=format:%m-%d %H:%M",
                   "-{}".format(int(limit)))
        if not log:
            return ""
        dirty = [x for x in _run("status", "--porcelain").splitlines() if x.strip()]
        block = ("\n【git 现场】原项目目录最近提交（新→旧）。判断前任做到哪一步，"
                 "先拿这里跟聊天记录对——对得上就不必翻流水：\n"
                 + "".join("- {}\n".format(x) for x in log.splitlines()[:limit]))
        if dirty:
            block += ("- ⚠ 工作树另有 {} 个未提交改动（可能是前任半成品，"
                      "git status 自己看）\n".format(len(dirty)))
        return block

    def _build_takeover_prompt(self, cid, cwd, name, file_path, recent_text,
                               transcript_path, board, zt_trail=None,
                               digest_repeat=False, uncollected="",
                               stay_put=False):
        if stay_put:
            # 多选：禁止写出「conversation_id=「原ID」」或「必须改用」，
            # 否则 takeover_prompt_target / 接手方都会把原对话复活。
            id_line = (
                "全程沿用你**当前会话**的 conversation_id（本单原 ID「{}」只用来读记录、"
                "定位前任流水；不要改用原 ID 报到——改了会把原对话从「已结束」里复活，"
                "控制台就会同时出现两个活人，你也不知道该回哪张）".format(cid) if cid
                else "全程沿用你当前会话自己的 conversation_id"
            )
            first_act = (
                "【第一个动作·先于读任何文件】现在立刻用**你当前会话已经在用的**"
                " conversation_id 调一次 zt（status=\"接令\"，activity=\"接手中\"）——"
                "不要切换到下面列出的原会话 ID。原 ID 只作读记录。"
                "此后本对话所有 zhi/zt/ji 一律沿用当前 ID。\n\n"
            )
            orig_id_line = (
                "- 原会话 ID（只读记录，不要改用）：" + (cid or "（无）") + "\n"
            )
        else:
            id_line = (
                "**conversation_id 必须改用「{}」**（这是被接手会话的原ID；哪怕你此前已用别的ID"
                "接入过控制台，从这一步起也一律切换到它——控制台里那个原标签页会立即复活、"
                "上下文接续；你之前那个接入用的空标签会被控制台自动收起，不用管）".format(cid) if cid
                else "用你自己新生成的 8 位 hex conversation_id（原会话无固定ID）"
            )
            # ID 切换写成「第一个动作」而不是埋在第 5 条的「一条规则」：08-12 实测，
            # 接手方按常驻协议「全程复用首个 ID」行事，读到第 5 条时早已用自己的 ID
            # 报过到，两条规矩打架它听了先来的那条——结果一直用旧 ID 干活，控制台
            # 判定接手始终没落地。面板派单场景另有别名机制兜底（takeover_aliases，
            # 不依赖它服从）；这段文案管的是「复制提示词手工粘贴」这条路。
            first_act = (
                "【第一个动作·先于读任何文件】现在立刻用 conversation_id=「{}」调一次 "
                "zt（status=\"接令\"，activity=\"接手中\"）——不是读完记录再说，是现在。"
                "此后本对话所有 zhi/zt/ji 一律用这个 ID；「全程复用首次 ID」的常驻规矩"
                "在接手场景下以本条为准。\n\n".format(cid) if cid else ""
            )
            orig_id_line = "- conversation_id：" + (cid or "（无）") + "\n"
        # 中文路径/GBK 乱码是本机常态不是故障：08-28 用户截图，接手 agent 为核实
        # 「D:\Desktop\cursor工作流」这种中文路径连跑几趟终端换语法，全耗在乱码上
        charset_line = ("7. 本机路径常含中文：Read/编辑工具直接用完整路径即可；"
                        "终端里中文输出乱码（GBK 控制台）≠命令失败，"
                        "别为核实路径/编码另起炉灶。\n")
        paths = [x for x in (transcript_path
                             if isinstance(transcript_path, (list, tuple))
                             else [transcript_path]) if x]
        if len(paths) == 1:
            transcript_line = (
                "- 前一个 agent 的完整 Cursor 对话记录（jsonl，含它干的每一步流水；聊天"
                "记录/摘要不足时读它还原全部进度）：{}\n".format(paths[0]))
        elif paths:
            transcript_line = (
                "- 这个对话留下的 Cursor 流水（jsonl，含每一步工具调用与改动；聊天记录/"
                "摘要不足时读它们还原全部进度。新→旧，最大的那份通常是干活主力）：\n"
                + "".join("  · {}\n".format(x) for x in paths))
        else:
            transcript_line = ""
        # 原会话 library_sent 已是 true，_with_library_digest 不会再附索引。
        # 接手是新 agent，复制粘贴这条提示词也不走 send_reply——必须把索引嵌进
        # 提示词本身。zt 仍是第一个动作（先于读任何文件）。
        try:
            digest = (self.library_digest(cwd, name,
                                          repeat=digest_repeat) or "").strip()
        except Exception:
            digest = ""
        lib_block = ""
        if digest:
            lib_block = (
                "【资料索引 · 接手后必读】原会话第一次派活已经发过一次；你是新 agent，"
                "报到后已恢复正常开局，下面是规则全文，按全文遵守。"
                "报到那轮为省 token 不带这些；接手=再派活，不再省。\n\n"
                + digest + "\n"
            )
            steps = (
                "1. 第一个动作（zt）做完后，先按上面【资料索引 · 接手后必读】的全文遵守"
                "（指针/KG 已在正文里）。\n"
                "2. 再用 Read 通读上面的聊天记录文件，弄清：原任务目标、已完成的部分、"
                "进行到哪一步、还剩什么没做。\n"
                "3. 若聊天记录/摘要不足以还原进度（前一个 agent 可能没勤上报），再用 Read 打开"
                "上面的「Cursor 对话记录 jsonl」——那是它干活的完整流水（工具调用/改了哪些文件/"
                "结论），据此接续，别从头重来。\n"
                "4. 记录里若提到具体代码文件，一并查看，恢复上下文。\n"
                "5. 接着把剩余工作干完。全程用rxyy MCP 的 zhi 工具与我交互：{id_line}；"
                "task_name 用「{name}」；project_path 传你当前工作区完整路径。\n"
                "6. 若 zhi 报 Connection closed/Not connected，先立即原参数重试一次，"
                "仍失败则每20-30秒重试、至少共6次，禁止放弃。\n"
                + charset_line
            )
            closer = "先 zt，再按资料全文遵守，然后读聊天记录。"
        else:
            steps = (
                "1. 先用 Read 工具通读上面的聊天记录文件，弄清：原任务目标、已完成的部分、"
                "进行到哪一步、还剩什么没做。\n"
                "2. 若聊天记录/摘要不足以还原进度（前一个 agent 可能没勤上报），再用 Read 打开"
                "上面的「Cursor 对话记录 jsonl」——那是它干活的完整流水（工具调用/改了哪些文件/"
                "结论），据此接续，别从头重来。\n"
                "3. 记录里若提到具体代码文件，一并查看，恢复上下文。\n"
                "4. 接着把剩余工作干完。\n"
                "5. 全程用rxyy MCP 的 zhi 工具与我交互：{id_line}；task_name 用「{name}」；"
                "project_path 传你当前工作区完整路径。\n"
                "6. 若 zhi 报 Connection closed/Not connected，先立即原参数重试一次，"
                "仍失败则每20-30秒重试、至少共6次，禁止放弃。\n"
                + charset_line
            )
            closer = "现在开始执行第 1 步：读聊天记录文件。"
        # 资料索引会原文带上 10-servers 里的 `/data/www/{项目名}/`。
        # 整段再 .format() 会把这些花括号当成占位符，复制接手提示词直接
        # KeyError('项目名')。注入段只拼接，步骤里那两个槽位也只做字面替换。
        steps = steps.replace("{id_line}", id_line).replace("{name}", name)
        # 前任断线前在做什么：zt 轨迹一句话就能把接手方带到现场（有才附，
        # 老会话/从文件接手没有轨迹就不占地方）
        zt_lines = [str(x).strip() for x in reversed(list(zt_trail or [])) if str(x).strip()][:3]
        zt_line = ("- 前任最后上报（zt 新→旧，断线前在做什么）：{}\n".format(
            "；".join(zt_lines)) if zt_lines else "")
        return (
            hub.TAKEOVER_PROMPT_HEAD + "，把它没做完的工作继续完成。\n\n"
            + first_act + lib_block
            + "【原会话信息】\n"
            + orig_id_line
            + "- 原项目目录：" + cwd + "\n"
            "- 任务标题：" + name + "\n"
            "- 完整聊天记录（Markdown，务必先读）：" + file_path + "\n"
            + transcript_line
            + zt_line
            + self._takeover_git_evidence(cwd)
            # 有回复待收才出现：接手方不先收就发新提问，存着的旧回复要么被当新提问
            # 的答复送错地方、要么（新提问带卡）被扣住——两头都是多绕一轮
            + (("\n" + uncollected) if uncollected else "")
            + "\n【先对一眼工作区】\n"
            "你当前 Cursor 窗口的工作区如果不是上面那个「原项目目录」，说明是跨工作区接手：\n"
            "你打不开它的代码，闷头干下去就是在错的仓库里改文件。这种情况先用 zhi 问用户"
            "「要我换到那个工作区的窗口去，还是就在这儿按绝对路径干」，别自己决定。\n"
            "\n【接手步骤】\n"
            + steps
            + "\n【最近对话摘要】（每条最多400字，超长处以〔此条已截断〕标注；完整内容一律以聊天记录文件为准）\n"
            + recent_text + "\n"
            + board
            + "\n——接手提示词到此完整结束。" + closer
        )

    def set_quiet(self, value):
        hub.HUB.cfg["quiet_mode"] = bool(value)
        hub.save_config(hub.HUB.cfg)
        return {"ok": True}

    def set_freeze(self, value):
        """省额度冻结开关。开：所有 agent 下次调 zhi/zt/ji 时阻塞挂起，零 token 消耗；
        关：立即解冻，agent 自动继续。config 热重载 + MCP 每 2s 轮询，故无需重启。"""
        hub.HUB.cfg["token_freeze"] = bool(value)
        hub.save_config(hub.HUB.cfg)
        hub.log_event("省额度冻结 {}".format("开启" if value else "解除"))
        if not value:
            hub.HUB._frozen_agents = {}
        return {"ok": True}

    def ping(self):
        """健康探针：不取任何锁、纯静态应答。看门狗/新实例/重启脚本用它区分
        「健康实例」和「端口被占着的冻结僵尸」——僵尸的网关线程连这个都回不了。"""
        return {"ok": True, "pid": os.getpid(), "ts": time.time()}

    def health(self):
        """全链体检（P2）：一次调用返回三进程/四端口状态，供 rxyy tools 做体检燈、
        排障时不必翻日志猜。走网关 POST /api/health 即可拿到。"""
        cfg = hub.HUB.cfg

        def _port_open(p):
            try:
                s = socket.create_connection(("127.0.0.1", int(p)), timeout=1.5)
                s.close()
                return True
            except OSError:
                return False

        # 看门狗守卫端口带主循环时间戳（协议升级后），能看出「假活/冻死」
        wd = {"state": "dead", "pid": None, "main_loop_age_secs": None}
        try:
            s = socket.create_connection(
                ("127.0.0.1", int(cfg.get("watchdog_port", 38996) or 38996)), timeout=1.5)
            raw = b""
            try:
                s.settimeout(2)
                raw = s.recv(64)
            except OSError:
                pass
            finally:
                s.close()
            try:
                pid_s, ts_s = raw.decode("ascii").strip().split(":", 1)
                wd["pid"] = int(pid_s)
                wd["main_loop_age_secs"] = round(time.time() - float(ts_s), 1)
                wd["state"] = "frozen" if wd["main_loop_age_secs"] > 180 else "alive"
            except (ValueError, UnicodeDecodeError):
                wd["state"] = "alive"  # 旧版守卫协议：端口应答即视为活
        except OSError:
            pass
        with hub.HUB.lock:
            sess = list(hub.HUB.sessions.values())
        now = time.time()
        return {
            "ok": True,
            "hub": {"pid": os.getpid(),
                    "uptime_secs": int(now - getattr(hub.HUB, "_boot_ts", now)),
                    "port": int(cfg.get("port", 38999)),
                    "window_alive": hub.HUB.window is not None},
            "mcp_daemon": {"port": int(cfg.get("mcp_http_port", 39222) or 0),
                           "listening": _port_open(cfg.get("mcp_http_port", 39222) or 0)},
            "share": {"port": int(cfg.get("share_port", 39080)),
                      "running": hub.HUB.share_server is not None},
            "watchdog": wd,
            "sessions": {
                "total": len(sess),
                "connected": sum(1 for x in sess if x.connected),
                "pending": sum(1 for x in sess if x.pending is not None),
                "hb_fresh": sum(1 for x in sess if x.connected
                                and now - getattr(x, "last_heartbeat", 0) < 15),
            },
        }

    def open_ui(self):
        """在默认浏览器打开控制台 UI（无头模式的「亮窗口」）。"""
        return {"ok": hub.open_ui_in_browser(hub.HUB.cfg)}

    # ---------- 停车/发车（parkgate 集成） ----------
    def park_status(self):
        try:
            import parkgate
            return parkgate.status()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def park_arm(self):
        try:
            import parkgate
            return parkgate.arm()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def park_launch(self):
        try:
            import parkgate
            return parkgate.launch()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def park_disarm(self):
        try:
            import parkgate
            return parkgate.disarm()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def park_install_hook(self):
        """装开车：把 hook.js 落到 ~/.salak，再把加载它的 ESM stub 注入 Cursor 扩展宿主入口。

        两步缺一不可。只注入 stub 不落脚本，stub 每次 require 一个不存在的文件都会抛
        MODULE_NOT_FOUND 被自己 catch 吞掉——按钮报「已装」，面板却一直「未安装」，
        开车按了也全放行。安装顺序与错误处理必须保持完整。
        只对之后**新开**的窗口生效（现有窗口的扩展宿主早已启动、不会重读入口），
        全程不重启 Cursor；装前自动留底，随时可用「还原 Cursor」按字节摘掉。
        """
        try:
            import parkgate
            ok, note1 = parkgate.install_hook()
            if not ok:
                # 脚本没落好时继续注入入口只会制造一段指向不存在文件的 stub。
                return {"ok": False, "error": note1, "note": note1}
            n, note2 = parkgate.ensure_stub()
            return {"ok": True, "injected": n, "note": note1 + "；" + note2}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def park_restore(self):
        """把 Cursor 恢复成没被动过的样子：摘掉注入的 stub，有留底就按字节还原。

        这个口子只会往「更干净」的方向走，任何时候按都安全——上次崩溃就是因为
        改了 Cursor 的安装文件却没有一条退回去的路。"""
        try:
            import parkgate
            n, note = parkgate.remove_stub()
            return {"ok": True, "restored": n, "note": note}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------- 账单 hook（billgate 集成；状态与开车完全隔离） ----------
    def bill_status(self):
        try:
            import billgate
            return billgate.status()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def bill_enable(self, mode="observe"):
        try:
            import billgate
            return billgate.enable(mode)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def bill_disable(self):
        try:
            import billgate
            return billgate.disable()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def bill_set_mode(self, mode="observe"):
        try:
            import billgate
            return billgate.set_mode(mode)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def bill_install_hook(self):
        """装账单 hook：把 billhook.js 安装到独立账单目录并注入加载 stub。
        只对之后新开的窗口生效，全程不重启 Cursor；账单与开车各自维护状态和留底。"""
        try:
            import billgate
            ok, note1 = billgate.install_hook()
            if not ok:
                # 脚本没落好时继续注入入口只会制造一段指向不存在文件的 stub。
                return {"ok": False, "error": note1, "note": note1}
            n, note2 = billgate.ensure_stub()
            return {"ok": True, "injected": n, "note": note1 + "；" + note2}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def bill_remove_hook(self):
        """完整卸载账单 hook：摘自身 stub，并清理自己的脚本、状态与观测残留。"""
        try:
            import billgate
            n, note = billgate.remove_stub()
            # restored 留给旧前端，removed 明确这是完整卸载而非只还原入口。
            return {"ok": True, "removed": n, "restored": n, "note": note}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def shutdown_core(self):
        """彻底停掉rxyy MCP核心（写正常关闭标记，看门狗不会复活它）。
        无头模式下没有窗口可关，这是唯一的体面下车通道。"""
        hub.log_event("用户请求关闭核心（shutdown_core）")
        try:
            hub.CLEAN_EXIT_MARK.write_text(hub.now_full(), encoding="utf-8")
        except OSError:
            pass
        hub.HUB.save_state()
        hub.save_config(hub.HUB.cfg)

        def die():
            time.sleep(0.5)
            os._exit(0)

        threading.Thread(target=die, daemon=True).start()
        return {"ok": True}

    def reload_mcp(self):
        """缓解 Cursor 偶发卡 Error：触碰 mcp.json 诱导 Cursor 重新 initialize rxyy MCP。"""
        if hub.touch_mcp_json():
            hub.log_event("用户点击重连 MCP（触碰 mcp.json nonce）")
            return {"ok": True}
        return {"ok": False, "error": "触碰 mcp.json 失败：{}".format(
            hub.HUB_LAST_TOUCH_ERROR.get("reason") or "未知原因")}

    def cursor_sidebar_drift(self):
        """控制台知道真名、Cursor 侧栏却顶着别的那些 tab。

        只认「控制台这边的名字是可信的」那批（agent 自报过 / 用户锁过），
        和 _push_name_to_cursor 同一道闸——不然会把还没定名的占位名推上去。
        """
        rows = []
        with hub.HUB.lock:
            sessions = list(hub.HUB.sessions.values())
        for s in sessions:
            name = (getattr(s, "name", "") or "").strip()[:40]
            uid = (getattr(s, "cursor_uuid", "") or "").strip()
            if not (name and uid):
                continue
            if not (getattr(s, "agent_named", False)
                    or getattr(s, "name_locked", False)):
                continue
            if hub.CHECKIN_SHELL_NAME_RE.match(name):
                continue
            try:
                shown = (hub.read_cursor_title(uid) or "").strip()
            except Exception:
                continue
            if shown and shown != name:
                rows.append({"session_id": s.id, "composer_id": uid,
                             "shown": shown, "want": name,
                             "cwd": getattr(s, "cwd", "") or ""})
        return rows

    def fix_cursor_sidebar(self, dry_run=False):
        """一键把 Cursor 侧栏上跑偏的 tab 名改对——不重启 Cursor、不 Reload Window。

        写盘（write_cursor_title）改的是影子：对话还开着时 Cursor 会把内存那份
        刷回磁盘、连名字一起盖回来，所以侧栏上那一排「Persistent plus …」怎么
        写都回不来。真正改内存的只有 composerService.renameComposer。

        拆 workbench.desktop.main.js 找到的口子：`composer.renameChat` 是注册过的
        命令，**收 composerId 当参数**（IDb(e,t): typeof t=="string" 就直接当 id
        用），所以不依赖「当前选中哪个 tab」；它 f1:!1 在命令面板里搜不到，但
        快捷键绑得上，而 keybindings.json 是热加载的。于是临时插一条绑定、按下、
        把名字敲进弹出的输入框、还原 keybindings.json 就成了。细节见
        cursor_live_rename。

        这条路要抢用户键盘一两秒，所以只做成手动按钮，不挂进标题同步循环。
        """
        rows = self.cursor_sidebar_drift()
        if dry_run or not rows:
            return {"ok": True, "drift": rows, "renamed": [], "dry_run": bool(dry_run)}
        notes = []

        def note(msg):
            notes.append(msg)
            hub.log_event(msg)

        done = set()
        if hub.wbhook is not None and hub.wbhook.hook_alive():
            done = set(hub.wbhook.rename_many(
                [(r["composer_id"], r["want"]) for r in rows], timeout=2.2) or ())
            if done:
                note("注入桥改了 {} 个侧栏名".format(len(done)))
        left = [r for r in rows if r["composer_id"] not in done]
        if left and hub.cursor_live_rename is not None:
            hint = ""
            for r in left:
                if r["cwd"]:
                    hint = Path(r["cwd"]).name
                    break
            keyed = set(hub.cursor_live_rename.rename_chats_live(
                [(r["composer_id"], r["want"]) for r in left],
                workspace_hint=hint, log=note) or ())
            done |= keyed
        elif left and not done:
            return {"ok": False, "drift": rows, "renamed": [],
                    "error": "注入桥没挂上（要 Reload 一次），也没有 cursor_live_rename 键盘驱动"}
        renamed = [r for r in rows if r["composer_id"] in done]
        hub.log_event("侧栏一键改名：跑偏 {} 个，按下去 {} 个".format(
            len(rows), len(renamed)))
        # 按下去 ≠ Cursor 已经认；renameComposer 是异步的，名字落回库要几秒。
        return {"ok": True, "drift": rows, "renamed": renamed,
                "left": len(rows) - len(renamed), "dry_run": False,
                "note": notes[-1] if notes else ""}

    def set_autostart(self, value):
        try:
            hub.autostart_set(bool(value))
            hub.HUB.autostart_enabled = bool(value)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": "写注册表失败: {}".format(e)}

    def close_disconnected(self):
        """一键关闭所有已断开的尸体 tab = 批量走第一段（收进「已结束」组）。

        不直接删：单个 × 只归档，批量却一键抹掉的话，「待续」里没干完的活会
        被同一个动作误杀。真要清空列表用 purge_archived。
        """
        with hub.HUB.lock:
            dead = [s for s in hub.HUB.sessions.values()
                    if not s.connected and not getattr(s, "archived", False)]
        for s in dead:
            self._archive_tab(s, save=False)
        if dead:
            hub.HUB.save_state()
        return {"ok": True, "archived": len(dead)}

    def purge_archived(self):
        """清空「已结束」组：把归档过的 tab 从列表里彻底删掉（记录文件保留）。"""
        removed = 0
        with hub.HUB.lock:
            gone = [sid for sid, s in hub.HUB.sessions.items()
                    if getattr(s, "archived", False) and not s.connected]
            for sid in gone:
                hub.HUB.sessions.pop(sid, None)
                if sid in hub.HUB.order:
                    hub.HUB.order.remove(sid)
                removed += 1
        if removed:
            hub.HUB.save_state()
        return {"ok": True, "removed": removed}

    def resurrect_closed_conv(self, conv_key):
        """误关 tab 后从记录文件把对话捡回来，并清掉「已关闭」拦截。

        两段式 × 会把 connected tab 打上 closed_convs，之后同 ID 的 zhi 一律
        回「请立即结束」。清垃圾时误关正在干活的 tab（08-14 28172cf0）必须能
        原 ID 复活，不能让用户再走一遍接手。"""
        cid = str(conv_key or "").strip()
        if not cid or cid == "__default__":
            return {"ok": False, "error": "缺少 conversation_id"}
        seen = set()
        for x in list(hub.HUB.sessions.values()):
            c = getattr(x, "client", None)
            if c is None or id(c) in seen:
                continue
            seen.add(id(c))
            try:
                getattr(c, "closed_convs", {}).pop(cid, None)
            except Exception:
                pass
        with hub.HUB.lock:
            live = next((x for x in hub.HUB.sessions.values()
                         if x.conv_key == cid), None)
        if live is not None:
            live.archived = False
            live.handed_off_to = ""
            live.end_reason = ""
            live.rev += 1
            hub.HUB.save_state()
            return {"ok": True, "restored": "unarchived", "id": live.id,
                    "name": live.name}
        hist = Path(hub.HUB.cfg.get("history_dir") or "")
        if not hist.is_dir():
            return {"ok": False, "error": "找不到聊天记录目录"}
        hit = None
        meta = None
        text = ""
        for p in hist.glob("*.md"):
            try:
                raw = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            parsed = hub.parse_history_md(raw)
            if parsed.get("conv") == cid:
                hit, meta, text = p, parsed, raw
                break
        if hit is None or meta is None:
            return {"ok": False, "error": "记录文件里没有这个对话 ID"}
        name = meta.get("name") or hit.stem
        renames = re.findall(r"> 标签改名: .*? → (.+?) · ", text)
        if renames:
            name = renames[-1].strip() or name
        shim = type("ResurrectClient", (), {
            "cwd": hub.sanitize_ws_path(meta.get("cwd") or ""),
            "pid": None, "peer_ip": None,
        })()
        s = hub.Session(shim, cid, name)
        s.client = None
        s.connected = False
        s.archived = False
        s.end_reason = ""
        s.file_path = str(hit)
        s.created_at = meta.get("created_at") or s.created_at
        s.shell_born = True
        if not hub.CHECKIN_SHELL_NAME_RE.match(str(name or "")):
            s.agent_named = True
            hub.HUB._mark_claimed(s, "误关后从记录复活")
        try:
            hub.HUB.hydrate_messages_from_file(s)
        except Exception:
            pass
        with hub.HUB.lock:
            s.name = hub.HUB._dedupe_name(s.name, exclude_id=s.id,
                                          conv_key=s.conv_key)
            hub.HUB.sessions[s.id] = s
            hub.HUB.order.append(s.id)
        hub.HUB.add_message(s, {
            "role": "sys", "ts": hub.now_hms(),
            "html": "误关已找回：记录从聊天文件回填，等 agent 下次 zhi/zt 即复活在线",
        })
        hub.log_event("误关找回 conv={} tab={} file={}".format(
            cid, s.name, hit.name))
        hub.HUB.save_state()
        return {"ok": True, "restored": "from_file", "id": s.id, "name": s.name}

    def list_history_files(self):
        """历史记录浏览：按修改时间倒序列出记录目录下的 .md 文件。"""
        try:
            d = Path(hub.HUB.cfg["history_dir"])
            items = []
            for p in d.glob("*.md"):
                try:
                    st = p.stat()
                except OSError:
                    continue
                items.append({
                    "name": p.stem,
                    "path": str(p),
                    "size": st.st_size,
                    "mtime": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%m-%d %H:%M"),
                    "_m": st.st_mtime,
                })
            items.sort(key=lambda x: x["_m"], reverse=True)
            for it in items:
                it.pop("_m", None)
            return {"ok": True, "files": items[:200]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def open_history_file(self, path):
        """打开历史记录文件（仅允许记录目录内的文件，防任意路径执行）。"""
        try:
            p = Path(path).resolve()
            if p.parent != Path(hub.HUB.cfg["history_dir"]).resolve() or p.suffix != ".md":
                return {"ok": False, "error": "路径不在记录目录内"}
            os.startfile(str(p))
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_takeover_prompt_from_file(self, path):
        """误关 tab 后一键找回：直接拿聊天记录 .md 生成接手提示词。

        tab 一关，会话对象就跟着从内存里消失，get_takeover_prompt 无从下手；但记录
        文件一直在（关闭只移除标签，从不删记录），conversation_id / 工作目录 / 最近
        对话都能从里面反解出来。拿原 ID 报到，那个 tab 就带着上下文回来了——关的是
        断开态 tab，走的是 close_tab 分支，不会给这个 conversation 打「已关闭」标记。
        """
        try:
            p = Path(path).resolve()
            if p.parent != Path(hub.HUB.cfg["history_dir"]).resolve() or p.suffix != ".md":
                return {"ok": False, "error": "路径不在记录目录内"}
            text = p.read_text(encoding="utf-8", errors="ignore")
        except OSError as e:
            return {"ok": False, "error": "记录文件读不了: {}".format(e)}
        meta = hub.parse_history_md(text)
        cid = meta["conv"]
        # 会话其实还在列表里（只是用户从历史记录进来的）：走原路径，那边能带上
        # transcript_path、团队分工这些只有内存里才有的信息
        if cid:
            with hub.HUB.lock:
                live = next((x for x in hub.HUB.sessions.values() if x.conv_key == cid), None)
            if live is not None:
                return self.get_takeover_prompt(live.id)
        recent = []
        for role, _ts, body in meta["msgs"][-10:]:
            recent.append("- {}：{}".format(
                "AI" if role == "ai" else "用户",
                body[:400] + ("……〔此条已截断〕" if len(body) > 400 else "")))
        recent_text = "\n".join(recent) if recent else "（没解析出对话气泡，一切以记录文件为准）"
        name = meta["name"] or p.stem
        ghost = hub.Session.__new__(hub.Session)  # 团队段只认 conv_key/cwd/id，够用
        ghost.conv_key, ghost.cwd, ghost.id, ghost.name = cid, meta["cwd"], "", name
        prompt = self._build_takeover_prompt(
            cid, meta["cwd"] or "未知", name, str(p), recent_text,
            self._locate_transcripts(meta["cwd"], cid),
            self._takeover_team_block(ghost))
        return {"ok": True, "prompt": prompt, "conversation_id": cid,
                "name": name, "file": str(p), "from_file": True}

    def open_image(self, path):
        """用系统查看器打开消息里的图片（仅允许记录目录的 图片/ 子目录）。"""
        try:
            p = Path(path).resolve()
            img_root = (Path(hub.HUB.cfg["history_dir"]) / "图片").resolve()
            if img_root not in p.parents or p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
                return {"ok": False, "error": "路径不在图片目录内"}
            os.startfile(str(p))
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def search_history(self, term):
        """跨会话全文搜索：在记录目录的 .md 里找关键词，返回文件+片段。"""
        term = (term or "").strip()
        if not term:
            return {"ok": True, "hits": []}
        low = term.lower()
        try:
            files = sorted(Path(hub.HUB.cfg["history_dir"]).glob("*.md"),
                           key=lambda p: p.stat().st_mtime, reverse=True)[:200]
        except OSError:
            return {"ok": False, "error": "记录目录不可读"}
        hits = []
        for p in files:
            try:
                if p.stat().st_size > 2 * 1024 * 1024:
                    continue
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            tl = text.lower()
            n = tl.count(low)
            if not n:
                continue
            i = tl.find(low)
            snippet = text[max(0, i - 40):i + len(term) + 70].replace("\n", " ").strip()
            hits.append({
                "name": p.stem, "path": str(p), "count": n, "snippet": snippet,
                "mtime": datetime.datetime.fromtimestamp(p.stat().st_mtime).strftime("%m-%d %H:%M"),
            })
            if len(hits) >= 50:
                break
        return {"ok": True, "hits": hits}

    @staticmethod
    def _dir_usage(p: Path, pattern=None):
        """(字节数, 文件数)。目录按 pattern 统计，缺省递归全部；单文件直接返回自身大小。"""
        try:
            if p.is_file():
                return p.stat().st_size, 1
            if not p.is_dir():
                return 0, 0
            total = count = 0
            for f in (p.glob(pattern) if pattern else p.rglob("*")):
                try:
                    if f.is_file():
                        total += f.stat().st_size
                        count += 1
                except OSError:
                    continue
            return total, count
        except OSError:
            return 0, 0

    @staticmethod
    def _app_dir_desc():
        """「程序与日志」那一行的说明。

        跑在常驻区里的时候必须把这件事说破：整个rxyy MCP已经不在 dist\\ 里了，
        **热拷得拷到这个目录**。不说的话，界面上只是一条陌生的路径，人照旧往
        `dist\\rxyy-tools-community\\_internal\\rxyy_mcp\\` 里拷——拷了不报错、也不生效
        （08-07 切过去当天，这一条我只能靠上黑板和挨个转告去喊）。

        判据：只有常驻区那份带 console-root.txt（materialize 写的，包内那份没有）。
        """
        if hub.live_runtime.console_root() is not None:
            return ("rxyy MCP 常驻区：换装碰不到这儿，所以热拷 .py / .html 要拷到"
                    "这个目录；.py 改完还得重启 hub/MCP 才生效")
        return "hub/server/watchdog 运行日志，不参与自动清理"

    def storage_paths(self):
        """设置面板「存储位置」：记录/图片/配置/会话状态/日志各自落在哪、占多大。

        数据目录随 datadir.py 四级解析走（源码版与打包版可能落在不同盘），文档里写死
        的路径靠不住，只能运行时报给界面——07-29 换打包版后配置和 tab 全空，就是因为
        没人看得见机器态实际被读写到了哪。"""
        hist = Path(hub.HUB.cfg["history_dir"])
        spec = [
            ("聊天记录", hist, "*.md", "每会话一个 .md，受保留天数与份数上限约束"),
            ("消息图片", hist / "图片", None, "按日期分文件夹，与记录同一套保留天数"),
            ("数据目录", hub.DATA_DIR, None, "机器态落盘根目录（配置、会话状态、sdk 注册表）"),
            ("配置文件", hub.CONFIG_PATH, None, "设置面板保存的就是它，只写与默认值不同的项"),
            ("会话状态", hub.DATA_DIR / ".sessions.json", None, "重启后靠它恢复 tab 与历史消息"),
            ("程序与日志", hub.APP_DIR, "*.log", self._app_dir_desc()),
        ]
        items = []
        for label, p, pattern, desc in spec:
            size, count = self._dir_usage(p, pattern)
            items.append({"label": label, "path": str(p), "desc": desc,
                          "exists": p.exists(), "is_dir": p.is_dir(),
                          "size": size, "count": count})
        return {"ok": True, "items": items,
                "keep_days": hub.HUB.history_keep_days(),
                "max_files": int(hub.HUB.cfg.get("max_history_files", 200) or 200)}

    def open_storage_path(self, path):
        """在资源管理器里打开存储位置（只认 storage_paths 报出的那几条，不接受任意路径）。"""
        try:
            allowed = {str(Path(it["path"]).resolve())
                       for it in self.storage_paths()["items"]}
            p = Path(path).resolve()
            if str(p) not in allowed:
                return {"ok": False, "error": "路径不在存储列表内"}
            if p.is_dir():
                os.startfile(str(p))
            elif p.exists():
                subprocess.Popen(["explorer", "/select,{}".format(p)])
            else:
                return {"ok": False, "error": "文件还没生成"}
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # 设置面板可编辑的键（端口/绑定涉及监听，改完必须重启，不放进来）
    SETTING_KEYS = ("keepalive_secs", "processing_timeout_secs", "title_sync_secs",
                    "sidebar_auto_rename", "sidebar_auto_rename_idle_secs",
                    "detach_grace_secs", "remote_base_url", "bark_url", "push_enabled",
                    "push_preview", "share_https_base",
                    "yield_on_burst", "mcp_instructions_full", "library_autosend",
                    "quick_phrases", "new_chat_prompt", "continue_prompt",
                    "cursor_api_key", "sdk_model", "agent_backend",
                    "max_messages", "max_history_files", "history_keep_days")

    def get_settings(self):
        out = {k: hub.HUB.cfg.get(k, hub.DEFAULTS.get(k)) for k in self.SETTING_KEYS}
        out["history_dir"] = hub.HUB.cfg.get("history_dir", "")
        out["port"] = int(hub.HUB.cfg.get("port", 38999))
        # 最近一次手机推送失败原因（如 ntfy 429 限流）：让「手机没动静」可诊断
        out["push_last_error"] = getattr(hub.HUB, "push_last_error", "")
        return {"ok": True, "settings": out}

    def save_settings(self, settings):
        """设置面板保存：类型校验后写回 config.json（全部是热生效项）。"""
        settings = settings or {}
        try:
            for k in ("keepalive_secs", "processing_timeout_secs", "title_sync_secs",
                      "sidebar_auto_rename_idle_secs",
                      "detach_grace_secs", "max_messages", "max_history_files"):
                if k in settings and str(settings[k]).strip() != "":
                    v = int(float(settings[k]))
                    if v < 0 or v > 24 * 3600:
                        return {"ok": False, "error": f"{k} 超出合理范围"}
                    hub.HUB.cfg[k] = v
            if "sidebar_auto_rename" in settings:
                hub.HUB.cfg["sidebar_auto_rename"] = bool(settings["sidebar_auto_rename"])
            for k in ("remote_base_url", "bark_url", "new_chat_prompt", "continue_prompt",
                      "cursor_api_key", "sdk_model", "share_https_base"):
                if k in settings:
                    hub.HUB.cfg[k] = str(settings[k]).strip()
            if "agent_backend" in settings:
                v = str(settings["agent_backend"]).strip().lower()
                if v in ("cli", "sdk"):
                    hub.HUB.cfg["agent_backend"] = v
            if "push_enabled" in settings:
                hub.HUB.cfg["push_enabled"] = bool(settings["push_enabled"])
            if "push_preview" in settings:
                hub.HUB.cfg["push_preview"] = bool(settings["push_preview"])
            if "yield_on_burst" in settings:
                hub.HUB.cfg["yield_on_burst"] = bool(settings["yield_on_burst"])
            if "mcp_instructions_full" in settings:
                hub.HUB.cfg["mcp_instructions_full"] = bool(settings["mcp_instructions_full"])
            if "library_autosend" in settings:
                hub.HUB.cfg["library_autosend"] = bool(settings["library_autosend"])
            if str(settings.get("history_keep_days", "")).strip() != "":
                v = int(float(settings["history_keep_days"]))
                if v < 0 or v > 365:
                    return {"ok": False, "error": "记录保留天数只能填 0～365"}
                hub.HUB.cfg["history_keep_days"] = v
            if "quick_phrases" in settings:
                ph = settings["quick_phrases"]
                if isinstance(ph, str):
                    ph = [x.strip() for x in ph.replace("，", ",").split(",")]
                hub.HUB.cfg["quick_phrases"] = [str(x) for x in ph if str(x).strip()][:12]
        except (TypeError, ValueError) as e:
            return {"ok": False, "error": f"参数不合法: {e}"}
        hub.save_config(hub.HUB.cfg)
        hub.HUB.config_error = ""
        hub.HUB.cleanup_history_files()  # 「保存即生效」：改小保留天数/份数当场看到结果
        return {"ok": True}

    def restart_hub(self):
        """升级代码后重启控制台：先落盘状态，再拉起新实例（--wait-pid 接力端口），最后自毁。

        会话 tab 靠 .sessions.json 恢复；MCP 侧 zhi 有断线重试，续上后 tab 自动复活。
        """
        now = time.time()
        if now - getattr(hub.HUB, "_restart_ts", 0) < 8:
            return {"ok": True, "note": "重启已在进行中，请稍候"}  # 防狂点拉起多个实例
        hub.HUB._restart_ts = now
        hub.HUB.save_state()
        hub.save_config(hub.HUB.cfg)
        hub.log_event("用户点击重启控制台")
        try:
            args = ["hub.py", "--wait-pid", os.getpid()]
            if not hub.HEADLESS:
                args.append("--windowed")  # 重启保持当前模式
            hub.spawn_detached(*args)
        except Exception as e:
            hub.HUB._restart_ts = 0
            return {"ok": False, "error": "拉起新实例失败: {}".format(e)}

        def die():
            time.sleep(0.5)

            # 强退兜底必须【先武装、独立线程】，绝不能依赖 destroy() 返回——
            # destroy 本身偶发卡死（webview 内部阻塞），若把 os._exit 排在它后面，
            # 旧进程就占着端口不死，新实例 --wait-pid 等到超时仍撞端口自杀，
            # 正是『重启两次也起不来、要管理员清进程』的死循环（14:03 实测复现）
            def force_exit():
                time.sleep(3.5)
                os._exit(0)

            threading.Thread(target=force_exit, daemon=True).start()
            try:
                hub.HUB.window.destroy()  # webview.start() 返回 → hub_closing() 收尾退出
            except Exception:
                pass

        threading.Thread(target=die, daemon=True).start()
        return {"ok": True}

    def restart_all(self):
        """一键重启整套 rxyy tools（hub + MCP守护 + rxyy tools 窗口）。

        用户明确要求（07-27 20:26）：点重启就整体重启 rxyy tools——单独重启 hub
        用户看不到任何可见反馈，无法确认恢复。流程：
        1. 本方法 = restart_hub 的全部机制（落盘 → 拉接力实例 → 自毁）
        2. 额外拉起 restart_rxyy.py：关掉 rxyy tools 窗口 → 等新 hub 网关上线
           → 重开 rxyy tools。窗口消失又回来 = 用户肉眼可见的「恢复完成」信号
        """
        now = time.time()
        if now - getattr(hub.HUB, "_restart_ts", 0) < 8:
            return {"ok": True, "note": "重启已在进行中，请稍候"}
        try:
            hub.spawn_detached("restart_rxyy.py", "--gateway-port",
                           int(hub.HUB.cfg.get("gateway_port", 38777) or 38777))
        except Exception as e:
            hub.log_event("restart_rxyy 拉起失败（继续只重启 hub）: {}".format(e))
        r = self.restart_hub()
        hub.log_event("用户点击一键重启 rxyy tools（hub + 窗口）")
        return r
