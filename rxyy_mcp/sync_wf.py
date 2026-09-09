# -*- coding: utf-8 -*-
"""双活 P1（配置线）：workflow.db 白名单实体的双向同步（差量轮询 emit + 幂等应用）。

设计稿 §4.3：「workflow.db 的 kv：白名单键走事件；密码/token/凭证不进事件流」。
rxyy 08-28 拍板把这条线提前（原排 P3）：当晚已把家机报表配置整库手工合并进
公司机（备份 workflow.db.bak-20260828-235131），两边一致成为基线；此后谁改了
项目映射/口径键/素材，靠本模块在分钟级内灌到对端。家机优先、公司机备用的
OA 分工也吃这份数据——oa_*_last_draft 标记同步过去后，公司机 18:00 备份档的
让路闸看到的是两台机共同的事实，摆渡回执之外多一条独立的路。

为什么是「轮询差量」而不是在写点挂 emit：kv/projects 的写入口分散在 console
进程（设置页、项目管理页）、外部脚本（OA job、合并工具、report_note）好几处，
而事件总线住在 hub。hub 侧每拍对白名单实体算指纹，与上次基线不同才发事件——
配置类数据不在乎秒级延迟，换来零侵入：console 一行不改、不用重启，脚本改库
也一样被捕获。

回声抑制：应用对端事件成功后同步把本机基线更新成来件指纹，轮询器便不会把
「刚从对端来的值」再发回去。LWW 的时间用行内 updated_at（真实写入时刻），
不是事件产生时刻——轮询有延迟，拿轮询时刻定序会把旧改动洗成新的。

首跑规矩：基线文件不存在时，只按当前库面立基线、一条事件不发。两边的存量
一致性由人工合并保证（见上），存量不走事件流——否则每次清基线都会把几百行
配置重播一遍，对端靠 LWW 全部丢弃，纯属噪音。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

from syncbus import lww_wins

STORE = "wf"

# kv 白名单：口径/清单/缓存/开关/进度标记。加键前先过 DENY_RE 的心智检查：
# 这个值落到另一台机器上会不会泄露凭证或指错路径？（desktop_root、*_key、
# oa_token 这类永远不进来。）
KV_WHITELIST = (
    "oa_report_projects",        # OA 项目下拉清单缓存（同一 OA，越新越全）
    "oa_default_project",
    "report_exclude_keywords",   # 素材排除词
    "report_internal_repos",     # 自研内部工具仓清单
    "report_sys_extra",          # 写作口径补充
    "report_name_blocklist",
    "report_default_row_text",
    "report_total_hours",
    "report_git_author",         # 手工覆盖口（通常留空走 git config 自动身份）
    "bailian_model",
    "plugin_daily_report_enabled",
    "oa_flow_catalog",           # OA 流程目录/绑定/加班元数据（服务端事实的缓存）
    "oa_flow_bind_goods",
    "oa_overtime_meta",
    "oa_daily_last_draft",       # 「今天谁写过草稿」——备份档让路闸的判据
    "oa_weekly_last_draft",
    "sync_canary",               # 验收专用哨兵键：往一边写时间戳，另一边等着看
    # 工单（jira）本体在服务器上两机都拉得到，本地只有这份派活记录（派给谁/
    # 撤回凭据）。整键 LWW：断连期间两边都派过活的话后写的整份赢，输的那几条
    # 在对端显示回「未派」——短期记忆（封顶 200 条），可接受。qid/session_id
    # 只在原机 hub 队列里有效，对端撤回会明确报会话不存在，不算泄露不算坏。
    "jira_dispatch_records",
)

# 凭证硬闸：白名单谁手滑加了敏感键，这里最后一道拦下（emit 与 apply 双侧都查）。
DENY_RE = re.compile(r"(pass|token|secret|credential|cookie|_key$|api_key|apikey)",
                     re.IGNORECASE)

PROJECT_FIELDS = ("enabled", "biz_name", "oa_project_id", "oa_project_text")


def _iso_to_epoch(s) -> float:
    """updated_at（isoformat 文本）→ epoch 秒；解析不了返回 0（当成最旧）。
    两机同在 Asia/Shanghai，本地时间直接比是安全的。"""
    txt = str(s or "").strip()
    if not txt:
        return 0.0
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return time.mktime(time.strptime(txt[:19], fmt))
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(txt).timestamp()
    except ValueError:
        return 0.0


def _fp(payload) -> str:
    """实体指纹：内容哈希（不含时间戳——同值重写不该产生事件）。"""
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _connect(db_path, ro=False):
    if ro:
        return sqlite3.connect("file:{}?mode=ro".format(
            str(db_path).replace("\\", "/")), uri=True, timeout=3)
    return sqlite3.connect(str(db_path), timeout=5)


def snapshot(db_path) -> dict:
    """白名单实体的当前库面：{entity: {"ts_iso","body"}}。
    entity 命名：kv:<键> / proj:<项目名> / note:<日期>|<项目名>。"""
    out = {}
    con = _connect(db_path, ro=True)
    try:
        marks = ",".join("?" * len(KV_WHITELIST))
        for k, v, at in con.execute(
                "SELECT k, v, updated_at FROM kv_config WHERE k IN (%s)" % marks,
                KV_WHITELIST):
            if DENY_RE.search(k):
                continue
            out["kv:" + k] = {"ts_iso": at or "", "body": {"v": v or ""}}
        for name, en, biz, oid, otx, at in con.execute(
                "SELECT name, enabled, biz_name, oa_project_id, oa_project_text,"
                " updated_at FROM projects"):
            out["proj:" + name] = {"ts_iso": at or "", "body": {
                "enabled": int(en or 0), "biz_name": biz or "",
                "oa_project_id": oid or "", "oa_project_text": otx or ""}}
        for nd, proj, content, source, at in con.execute(
                "SELECT note_date, project, content, source, updated_at "
                "FROM report_notes"):
            out["note:{}|{}".format(nd, proj)] = {"ts_iso": at or "", "body": {
                "content": content or "", "source": source or "agent"}}
    finally:
        con.close()
    return out


class WfSync:
    """emit 差量轮询 + 对端事件应用，共用一份基线做回声抑制。"""

    def __init__(self, bus, db_locator, datadir, alert=None, poll_secs=20.0):
        self.bus = bus
        self.db_locator = db_locator          # () -> Path|None（console 域的 workflow.db）
        self.alert = alert or (lambda msg: None)
        self.poll_secs = max(3.0, float(poll_secs))
        self.baseline_path = Path(datadir) / ".sync-wf-baseline.json"
        self.baseline = {}                    # {entity: fingerprint}
        self._seeded = False
        self._lock = threading.Lock()         # 基线被轮询线程与重放线程两头碰
        self._stop = threading.Event()
        self._thread = None
        self.emitted = 0
        self.applied = 0
        self.skipped_lww = 0
        self.last_poll_ts = 0.0
        self.last_error = ""
        self._load_baseline()
        bus.replayer.register(STORE, self.apply_event)

    # ---------- 基线 ----------
    def _load_baseline(self):
        try:
            d = json.loads(self.baseline_path.read_text(encoding="utf-8"))
            self.baseline = {str(k): str(v) for k, v in (d.get("fp") or {}).items()}
            self._seeded = bool(d.get("seeded"))
        except Exception:  # noqa: BLE001  基线丢了=当首跑：重立基线不发事件
            self.baseline = {}
            self._seeded = False

    def _save_baseline(self):
        tmp = str(self.baseline_path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"seeded": self._seeded, "fp": self.baseline},
                      f, ensure_ascii=False)
        os.replace(tmp, self.baseline_path)

    # ---------- emit 侧 ----------
    def poll_once(self) -> int:
        db = self.db_locator()
        if db is None:
            self.last_error = "workflow.db 未找到"
            return 0
        try:
            snap = snapshot(db)
        except Exception as e:  # noqa: BLE001
            self.last_error = "快照失败: {}".format(e)
            return 0
        self.last_error = ""
        self.last_poll_ts = time.time()
        emitted = 0
        with self._lock:
            if not self._seeded:
                # 首跑只立基线：存量一致性由 08-28 的人工合并保证，不重播存量
                self.baseline = {ent: _fp(cur["body"]) for ent, cur in snap.items()}
                self._seeded = True
                self._save_baseline()
                return 0
            for ent, cur in snap.items():
                fp = _fp(cur["body"])
                if self.baseline.get(ent) == fp:
                    continue
                payload = dict(cur["body"])
                payload["row_ts"] = cur["ts_iso"]
                self.bus.outbox.emit(STORE, "update", ent, payload=payload)
                self.baseline[ent] = fp
                emitted += 1
            if emitted:
                self._save_baseline()
        self.emitted += emitted
        return emitted

    # ---------- apply 侧（挂在 bus.replayer，异常=停位重试，见 syncbus 纪律） ----------
    def apply_event(self, ev):
        ent = str(ev.get("entity") or "")
        payload = ev.get("payload") or {}
        kind, _, ident = ent.partition(":")
        if kind == "kv" and DENY_RE.search(ident):
            return                            # 凭证硬闸：对端手滑也不落地
        db = self.db_locator()
        if db is None:
            raise RuntimeError("workflow.db 未找到，事件暂不应用")
        row_ts = str(payload.get("row_ts") or "")
        new_ts = _iso_to_epoch(row_ts) or float(ev.get("ts") or 0)
        con = _connect(db)
        try:
            cur_ts, exists = self._current_ts(con, kind, ident)
            # LWW 用行内写入时刻；同刻由机器名决胜（两边裁决一致即可）
            if exists and not lww_wins(new_ts, ev.get("machine"),
                                       cur_ts, self.bus.machine):
                self.skipped_lww += 1
                return
            body = {k: v for k, v in payload.items() if k != "row_ts"}
            applied = self._write(con, kind, ident, body, row_ts)
            con.commit()
        finally:
            con.close()
        if applied:
            self.applied += 1
            with self._lock:
                self.baseline[ent] = _fp(body)   # 回声抑制：来件不再回发
                if self._seeded:
                    self._save_baseline()

    def _current_ts(self, con, kind, ident):
        if kind == "kv":
            row = con.execute("SELECT updated_at FROM kv_config WHERE k=?",
                              (ident,)).fetchone()
        elif kind == "proj":
            row = con.execute("SELECT updated_at FROM projects WHERE name=?",
                              (ident,)).fetchone()
        elif kind == "note":
            nd, _, proj = ident.partition("|")
            row = con.execute(
                "SELECT updated_at FROM report_notes WHERE note_date=? AND project=?",
                (nd, proj)).fetchone()
        else:
            return 0.0, False
        return (_iso_to_epoch(row[0]) if row else 0.0), row is not None

    def _write(self, con, kind, ident, body, row_ts) -> bool:
        ts = row_ts or datetime.now().isoformat(timespec="seconds")
        if kind == "kv":
            con.execute(
                "INSERT INTO kv_config(k, v, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v, "
                "updated_at=excluded.updated_at",
                (ident, str(body.get("v") or ""), ts))
            return True
        if kind == "proj":
            # 只认两边都有的项目（按 name 对齐）：对端有、本机没有的仓，路径在
            # 本机不存在，凭空插行会造出没法扫的幽灵项目——等本机自己扫到它，
            # 映射自然跟上（事件还在对端文件里，清基线可重放）
            r = con.execute(
                "UPDATE projects SET enabled=?, biz_name=?, oa_project_id=?, "
                "oa_project_text=?, updated_at=? WHERE name=?",
                (int(body.get("enabled") or 0), str(body.get("biz_name") or ""),
                 str(body.get("oa_project_id") or ""),
                 str(body.get("oa_project_text") or ""), ts, ident))
            return r.rowcount > 0
        if kind == "note":
            nd, _, proj = ident.partition("|")
            con.execute(
                "INSERT INTO report_notes(note_date, project, content, source,"
                " created_at, updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(note_date, project) DO UPDATE SET "
                "content=excluded.content, source=excluded.source, "
                "updated_at=excluded.updated_at",
                (nd, proj, str(body.get("content") or ""),
                 str(body.get("source") or "agent"), ts, ts))
            return True
        return False                          # 未知 kind：本机代码旧，跳过不装懂

    # ---------- 线程 ----------
    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)
            self._stop.wait(self.poll_secs)

    def status(self):
        return {"emitted": self.emitted, "applied": self.applied,
                "skipped_lww": self.skipped_lww, "seeded": self._seeded,
                "baseline_entities": len(self.baseline),
                "last_poll_ts": self.last_poll_ts, "last_error": self.last_error}


def attach(bus, cfg, alert=None):
    """hub 接线：bus 在跑才有意义；workflow.db 定位吃 plugin_registry 那套
    （RXYY_DATA_DIR → data-dir.txt → 根/data，07-29 教训：路径解析不自造）。"""
    from plugin_registry import _find_workflow_db
    ws = WfSync(bus, _find_workflow_db, bus.datadir, alert=alert,
                poll_secs=float((cfg or {}).get("sync_wf_poll_secs", 20) or 20))
    return ws.start()
