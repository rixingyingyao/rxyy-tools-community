# -*- coding: utf-8 -*-
r"""双活 P2（业务线）：看板 / 任务安排站 / 会话控制台列表的两机同步。

rxyy 08-31 派单（对话 872e2b81）：同步任务安排站、工单、OA、会话控制台列表；
聊天记录明确不同步（各机本地自找）。工单本体在 JIRA 服务器上两机都拉得到，
本地只有派活记录（workflow.db kv `jira_dispatch_records`），走 P1 配置线的
白名单（见 sync_wf.KV_WHITELIST），不在本文件。

三条线共用 P1 的骨架：差量轮询 emit + 幂等应用 + 基线回声抑制（sync_wf 的
「轮询差量」理由在这里同样成立——看板/任务站的写入口横跨 console 进程与
hub 里的投递站两个进程，事件总线住在 hub，挂写点做不到零侵入）。

与 P1 配置线的三个刻意不同：

1. **首跑重播存量**。kv 线首跑不重播，是因为 08-28 已人工把两库合并成一致
   基线，重播只会制造 LWW 噪音。看板/任务站两机存量本来就不同（各自建过卡、
   收过任务），且应用侧是「缺了就补插、有了才合并」的加法语义——重播一遍
   存量正是两边补齐的手段，代价一次性、上限就是卡片/任务总数。
   已知代价：对端还留着的陈旧任务会在首跑时灌回来（同步前的删除没有墓碑，
   无从分辨「没同步过」与「删过了」）；一次性人工清理，此后删除走事件。
2. **看板应用是合并不是覆盖**。卡片事件流（评论/领卡/交付）是 append-only
   审计链，两边断连期间各自写了评论的话，整卡 LWW 会丢掉一边的字——所以
   标量字段按行内 updated_at LWW，events 按 (ts,kind,conversation_id,text)
   去重取并集：并集幂等且交换，两边各再回放一圈后收敛。
3. **任务站有真删除**（remove_task），必须发 delete 事件——否则 A 删掉的
   任务会被 B 的下一次 re-emit 复活。删除与本地更新撞车时按 LWW：本地行
   updated_at 比删除时刻新就不删（后写的编辑赢过先按的删除）。

会话控制台列表是单机单写数据（home 的会话只有 home 的 hub 在写），没有
合并问题：整表投影成一条实体互发，对端原样落成 `.peer-sessions-<machine>.json`
只读副本，控制台按机器名标注展示。投影只取列表字段（名字/对话ID/工作区/
项目），messages、草稿、队列一概不进事件——聊天记录不同步是本次拍板的边界。

派发凭据（dispatch_qid/session_id）随任务原样复制：它们只在原机的 hub 队列
里有效，对端撤回会明确报「会话不存在」——复制的是事实，不替对端改写语义；
若应用侧擅自剥掉这些字段，回声一圈会把原机的撤回凭据也洗掉。

任务站图片的二进制走 TaskAssetFerry（08-31 用户实拍：对端投的任务在本机
任务站里缩略图全是破图）——出港区 + 按缺认领，不进事件日志：outbox 是
append-only 且 scp 每拍整文件拉，塞 base64 等于每一拍都重传全部历史图片。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from sync_ferry import _CREATE_NO_WINDOW, _MISSING_RE
from syncbus import lww_wins

STORE_BOARD = "board"
STORE_TASK = "task"
STORE_SESS = "sess"

# 看板卡片进事件体的字段（version/updated_at 是本机簿记，不算内容；
# events 必须进来——并集合并靠它）
_CARD_FIELDS = ("title", "desc", "project", "priority", "labels", "status",
                 "assignee", "review", "archived", "blocked_by",
                 "last_activity", "created_at", "workflow_updated_at", "events")
# 标量按 LWW 整组覆盖的字段（events 单独走并集）
_CARD_LWW_FIELDS = ("title", "desc", "project", "priority", "labels",
                    "archived", "blocked_by", "last_activity")
_CARD_WORKFLOW_FIELDS = ("status", "assignee", "review")

# 任务站任务进事件体的字段：附件只带元数据（文件名/显示名/大小），二进制
# 不进事件——图片本体由 TaskAssetFerry 沿 scp 航线按缺认领（见文件尾），
# files 附件仍留在原机（体积无上限，等有实需再议）
_TASK_FIELDS = ("title", "description", "project", "repo", "requester",
                "priority", "status", "tags", "images", "files", "order",
                "created_at", "reopened_at", "dispatched_at", "dispatch_note",
                "session_name", "dispatch_session_id", "dispatch_conv_key",
                "dispatch_qid", "dispatch_direct", "dispatch_batch")

# 会话列表投影字段：够控制台把「对端有哪些会话」列出来即可。
# 刻意不带 messages/draft/queued（聊天不同步），也不带 pid/端口这类只在
# 原机有意义的运行时字段；last 活跃时间不进投影——它每拍都变，带上就是
# 每 15s 一条事件，outbox 会被灌成流水账。
_SESS_FIELDS = ("conv_key", "name", "cwd", "task_root", "agent_project",
                "created_ts", "shell_born", "agent_named", "cursor_title")


def _fp(payload) -> str:
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]


def _atomic_write_json(path: Path, value) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False)
    os.replace(tmp, path)


class _DiffLine:
    """差量轮询线的公共骨架：基线（实体→指纹）落盘、回声抑制、首跑策略。

    与 WfSync 的差别只有一个开关：replay_stock_on_seed——业务线首跑重播存量
    （门头理由 1），配置线不重播。子类只需给出 snapshot() 与 apply_event()。
    """

    store = ""
    replay_stock_on_seed = True

    def __init__(self, bus, datadir, alert=None):
        self.bus = bus
        self.alert = alert or (lambda msg: None)
        self.baseline_path = Path(datadir) / ".sync-{}-baseline.json".format(self.store)
        self.baseline = {}
        self._seeded = False
        self._lock = threading.Lock()
        self.emitted = 0
        self.applied = 0
        self.skipped_lww = 0
        self.last_poll_ts = 0.0
        self.last_error = ""
        self._load_baseline()
        bus.replayer.register(self.store, self.apply_event)

    # ---------- 基线 ----------
    def _load_baseline(self):
        try:
            d = json.loads(self.baseline_path.read_text(encoding="utf-8"))
            self.baseline = {str(k): str(v) for k, v in (d.get("fp") or {}).items()}
            self._seeded = bool(d.get("seeded"))
        except Exception:  # noqa: BLE001  基线丢了=当首跑
            self.baseline = {}
            self._seeded = False

    def _save_baseline(self):
        _atomic_write_json(self.baseline_path,
                           {"seeded": self._seeded, "fp": self.baseline})

    def _remember(self, entity, body):
        """应用成功后把本机基线钉到应用后的实际内容上（回声抑制）。"""
        with self._lock:
            if body is None:
                self.baseline.pop(entity, None)
            else:
                self.baseline[entity] = _fp(body)
            if self._seeded:
                self._save_baseline()

    # ---------- emit ----------
    def snapshot(self) -> dict:
        """{entity: {"ts": LWW用的时间, "body": 事件体}}；子类实现。
        拿不到库（console 半启动/文件缺失）就抛，poll_once 记 last_error。"""
        raise NotImplementedError

    def emit_op(self, entity, cur):
        self.bus.outbox.emit(self.store, "update", entity,
                             payload={"row_ts": cur["ts"], **cur["body"]})

    def emit_delete(self, entity):
        """默认线没有删除语义；需要的子类（任务站）覆写并真的发事件。"""

    def poll_once(self) -> int:
        try:
            snap = self.snapshot()
        except Exception as e:  # noqa: BLE001
            self.last_error = "快照失败: {}".format(e)
            return 0
        self.last_error = ""
        self.last_poll_ts = time.time()
        emitted = 0
        with self._lock:
            if not self._seeded:
                if self.replay_stock_on_seed:
                    for ent, cur in snap.items():
                        self.emit_op(ent, cur)
                        self.baseline[ent] = _fp(cur["body"])
                        emitted += 1
                else:
                    self.baseline = {ent: _fp(cur["body"])
                                     for ent, cur in snap.items()}
                self._seeded = True
                self._save_baseline()
                self.emitted += emitted
                return emitted
            for ent, cur in snap.items():
                fp = _fp(cur["body"])
                if self.baseline.get(ent) == fp:
                    continue
                self.emit_op(ent, cur)
                self.baseline[ent] = fp
                emitted += 1
            for ent in [e for e in self.baseline if e not in snap]:
                self.emit_delete(ent)
                del self.baseline[ent]
                emitted += 1
            if emitted:
                self._save_baseline()
        self.emitted += emitted
        return emitted

    # ---------- apply ----------
    def apply_event(self, ev):
        raise NotImplementedError

    def status(self):
        return {"emitted": self.emitted, "applied": self.applied,
                "skipped_lww": self.skipped_lww, "seeded": self._seeded,
                "baseline_entities": len(self.baseline),
                "last_poll_ts": self.last_poll_ts, "last_error": self.last_error}


# ---------------- 看板线 ----------------
def _event_key(ev):
    """卡片事件的去重键。ts 取到毫秒：同一条事件两边各转一手浮点，微秒尾数
    可能漂；两条真不同的事件在同一毫秒同人同字，业务上就是同一条。"""
    return (round(float(ev.get("ts") or 0), 3), str(ev.get("kind") or ""),
            str(ev.get("conversation_id") or ""), str(ev.get("text") or ""))


def _merge_events(local, remote):
    seen = {}
    for item in list(local or []) + list(remote or []):
        if isinstance(item, dict):
            seen.setdefault(_event_key(item), item)
    return sorted(seen.values(), key=lambda e: float(e.get("ts") or 0))


def _workflow_lww_wins(remote_ts, remote, local_ts, local) -> bool:
    """workflow 同时刻冲突用内容指纹决胜，不能用中转机 machine。

    合并回声由当前接收机重新 emit，machine 会变；以它作等时刻决胜会在两端来回
    翻转。内容指纹固定且两端可重算，时间相等时仍能得到同一个唯一赢家。
    """
    remote_ts, local_ts = float(remote_ts or 0), float(local_ts or 0)
    if remote_ts != local_ts:
        return remote_ts > local_ts
    remote_fp, local_fp = _fp(remote), _fp(local)
    return remote_fp != local_fp and remote_fp > local_fp


class BoardSync(_DiffLine):
    """看板卡片：普通标量 LWW、workflow 独立 LWW、事件流并集。

    ``updated_at`` 会因评论或合并而变化，不能再用它判定谁最后交付；workflow
    时钟只由实际 status/assignee/review 改动推进，旧卡以它原有 updated_at 兼容。
    """

    store = STORE_BOARD

    def __init__(self, bus, storage_getter, datadir, alert=None):
        self._storage = storage_getter      # () -> BoardStorage|None
        super().__init__(bus, datadir, alert=alert)

    def _card_body(self, card) -> dict:
        return {k: card.get(k) for k in _CARD_FIELDS}

    def snapshot(self) -> dict:
        st = self._storage()
        if st is None:
            raise RuntimeError("看板库未就绪")
        out = {}
        for card in st.list_cards():
            out["card:" + card["id"]] = {"ts": float(card.get("updated_at") or 0),
                                         "body": self._card_body(card)}
        return out

    def apply_event(self, ev):
        st = self._storage()
        if st is None:
            raise RuntimeError("看板库未就绪，事件暂不应用")
        entity = str(ev.get("entity") or "")
        card_id = entity.partition(":")[2]
        payload = ev.get("payload") or {}
        row_ts = float(payload.get("row_ts") or 0) or float(ev.get("ts") or 0)
        body = {k: payload.get(k) for k in _CARD_FIELDS}
        incoming = dict(body)
        incoming["id"] = card_id
        incoming["version"] = 1
        incoming["updated_at"] = row_ts
        incoming["created_at"] = float(body.get("created_at") or 0) or row_ts
        changed = {"value": False, "remote_wins": False}

        def merge(card):
            # 取锁后重新读取当前卡：同步期间本地刚写的评论/交付不能被 apply 前的
            # 快照盖掉。版本仅是本机乐观锁，不能作为跨机时钟。
            remote_scalar_wins = lww_wins(
                row_ts, ev.get("machine"), float(card.get("updated_at") or 0),
                self.bus.machine)
            remote_workflow_ts = (float(body.get("workflow_updated_at") or 0)
                                  or row_ts)
            local_workflow_ts = float(card.get("workflow_updated_at")
                                      or card.get("updated_at") or 0)
            remote_workflow = {k: body.get(k) for k in _CARD_WORKFLOW_FIELDS}
            local_workflow = {k: card.get(k) for k in _CARD_WORKFLOW_FIELDS}
            remote_workflow_wins = _workflow_lww_wins(
                remote_workflow_ts, remote_workflow, local_workflow_ts, local_workflow)
            merged_events = _merge_events(card.get("events"), body.get("events"))
            changed["remote_wins"] = remote_scalar_wins or remote_workflow_wins
            if _fp(merged_events) != _fp(card.get("events") or []):
                changed["value"] = True
            if remote_scalar_wins:
                for k in _CARD_LWW_FIELDS:
                    if _fp(card.get(k)) != _fp(body.get(k)):
                        changed["value"] = True
                        card[k] = body.get(k)
            if remote_workflow_wins:
                for k in _CARD_WORKFLOW_FIELDS:
                    if _fp(card.get(k)) != _fp(body.get(k)):
                        changed["value"] = True
                        card[k] = body.get(k)
                if remote_workflow_ts != local_workflow_ts:
                    changed["value"] = True
                # 显式替换，令存储层保留来件时间而不是写成本机 now。
                card["workflow_updated_at"] = remote_workflow_ts
            if not changed["value"]:
                return "NO_CHANGE"
            card["events"] = merged_events
            # 合并只融合远端来源时间与本地已有时间；不制造一个假的“现在”。
            card["updated_at"] = max(float(card.get("updated_at") or 0), row_ts)
            return None

        merged, err, inserted = st.merge_or_insert_card_replica(incoming, merge)
        if merged is None:
            if err == "NO_CHANGE":
                self.skipped_lww += not changed["remote_wins"]
                return
            raise RuntimeError("看板合并失败: {}".format(err))
        self.applied += 1
        # 基线钉「来件」的指纹而不是合并结果：合并若比来件多出东西（本机
        # 独有的评论），下一拍指纹对不上就会把并集再发出去——对端正是靠
        # 这一圈拿到本机的那份，两边各多跑一圈后收敛（门头 2）
        self._remember(entity, body)


# ---------------- 任务安排站线 ----------------
class TaskSync(_DiffLine):
    """任务整体 LWW（行内 updated_at）+ 真删除事件。

    任务没有内嵌审计流，两边同时改同一条的窗口只在断连期间，整体 LWW 丢的
    最多是一次编辑（会被对端更新的整份盖掉）；比起给每个字段做三方合并，
    先要正确、简单。附件只随元数据走（见 _TASK_FIELDS 注释）。
    """

    store = STORE_TASK

    def __init__(self, bus, storage_getter, datadir, alert=None):
        self._storage = storage_getter      # () -> TaskStageStorage|None
        super().__init__(bus, datadir, alert=alert)

    def _task_body(self, task) -> dict:
        return {k: task.get(k) for k in _TASK_FIELDS}

    def snapshot(self) -> dict:
        st = self._storage()
        if st is None:
            raise RuntimeError("任务站库未就绪")
        out = {}
        for task in st.list_tasks():
            out["task:" + task["id"]] = {"ts": float(task.get("updated_at") or 0),
                                         "body": self._task_body(task)}
        return out

    def emit_delete(self, entity):
        # row_ts 用检测到消失的时刻：删除动作本身没有行内时间可用；轮询延迟
        # 最多让删除的 LWW 时刻晚 poll_secs 秒，语义是「删除赢过这之前的编辑」
        self.bus.outbox.emit(self.store, "delete", entity,
                             payload={"row_ts": time.time()})

    def apply_event(self, ev):
        st = self._storage()
        if st is None:
            raise RuntimeError("任务站库未就绪，事件暂不应用")
        entity = str(ev.get("entity") or "")
        task_id = entity.partition(":")[2]
        payload = ev.get("payload") or {}
        row_ts = float(payload.get("row_ts") or 0) or float(ev.get("ts") or 0)
        local = st.find(task_id)
        if str(ev.get("op") or "") == "delete":
            if local is None:
                self._remember(entity, None)
                return
            if not lww_wins(row_ts, ev.get("machine"),
                            float(local.get("updated_at") or 0),
                            self.bus.machine):
                self.skipped_lww += 1
                return
            st.remove_task(task_id)
            self.applied += 1
            self._remember(entity, None)
            return
        body = {k: payload.get(k) for k in _TASK_FIELDS}
        if local is not None and not lww_wins(row_ts, ev.get("machine"),
                                              float(local.get("updated_at") or 0),
                                              self.bus.machine):
            # 不动基线（理由同看板线）：本机的胜出值可能还躺在待发差量里
            self.skipped_lww += 1
            return
        if local is not None and _fp(self._task_body(local)) == _fp(body):
            self._remember(entity, body)
            return
        task = dict(body)
        task["id"] = task_id
        task["updated_at"] = row_ts
        st.upsert_task_replica(task)
        self.applied += 1
        self._remember(entity, body)


# ---------------- 会话控制台列表线 ----------------
class SessSync(_DiffLine):
    """会话列表：单机单写，整表投影成一条实体互发，对端落只读副本。

    数据源是 hub 每 15s 落盘的 .sessions.json（消息裁短后的会话快照）——
    读文件而不读 hub 内存，是刻意的：本模块跑在同一进程，但吃落盘快照就
    天然拿到「重启也认的那份」，且与 hub.sessions 的锁、生命周期完全解耦。
    """

    store = STORE_SESS
    # 单实体整表投影：首跑本来就发全量，与差量路径无异
    replay_stock_on_seed = True

    def __init__(self, bus, sessions_path, datadir, alert=None):
        self.sessions_path = Path(sessions_path)
        self.peer_dir = Path(datadir)
        super().__init__(bus, datadir, alert=alert)

    def _project(self) -> list:
        try:
            snap = json.loads(self.sessions_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        rows = []
        for d in snap or []:
            if not isinstance(d, dict):
                continue
            ck = str(d.get("conv_key") or "")
            if not ck or ck == "__default__":
                continue
            rows.append({k: d.get(k) for k in _SESS_FIELDS})
        rows.sort(key=lambda r: (float(r.get("created_ts") or 0),
                                 str(r.get("conv_key") or "")))
        return rows

    def snapshot(self) -> dict:
        return {"list:" + self.bus.machine:
                {"ts": time.time(), "body": {"sessions": self._project()}}}

    def peer_file(self, machine) -> Path:
        return self.peer_dir / ".peer-sessions-{}.json".format(machine)

    def apply_event(self, ev):
        machine = str(ev.get("machine") or "")
        entity = str(ev.get("entity") or "")
        # 实体名里的机器名以事件封装里的 machine 为准（它是运输层身份，
        # 伪造不了）；两者不一致说明对端配置写错，宁可不落
        if not machine or entity != "list:" + machine:
            return
        payload = ev.get("payload") or {}
        cur = self.read_peer(machine)
        if cur and float(cur.get("ts") or 0) > float(ev.get("ts") or 0):
            self.skipped_lww += 1
            return
        _atomic_write_json(self.peer_file(machine), {
            "machine": machine,
            "ts": float(ev.get("ts") or 0),
            "sessions": list(payload.get("sessions") or []),
        })
        self.applied += 1

    def read_peer(self, machine) -> dict:
        try:
            return json.loads(self.peer_file(machine).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001  没收到过/半截文件：当没有
            return {}

    def peers_snapshot(self) -> list:
        """给 hub_api / UI 用：所有对端的会话列表副本（带机器名与最后更新）。"""
        out = []
        try:
            files = sorted(self.peer_dir.glob(".peer-sessions-*.json"))
        except OSError:
            return out
        for p in files:
            machine = p.name[len(".peer-sessions-"):-len(".json")]
            if machine == self.bus.machine:
                continue
            d = self.read_peer(machine)
            if d:
                out.append(d)
        return out


# ---------------- 任务站图片摆渡 ----------------
# 图片文件名由 save_image_file 生成：uuid4().hex + 白名单扩展名。正则同时是
# 安全闸——名字来自同步过来的任务元数据（对端数据，边界数据），不匹配的
# 一律不碰文件系统，路径穿越连门都摸不到。
_IMG_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(?:png|jpe?g|gif|webp|bmp)$")
ASSET_SUBDIR = "assets/taskstage"       # sync_root 下的出港区（ASCII，scp 纪律）
MAX_ASSET_BYTES = 5 * 1024 * 1024       # 与 taskstage.storage.MAX_IMAGE_BYTES 对齐
_ASSET_RETRY_SECS = 180.0    # 对端还没发布这张图：单图退避，等它下一拍出港
_ASSET_DOWN_SECS = 300.0     # 对端连不上：整线退避，别拿 8s 超时乘以缺图数
_ASSET_PULLS_PER_POLL = 3    # 一拍最多认领几张（首跑积压分拍摊，不堵别的线）


class TaskAssetFerry:
    """任务站图片的两机摆渡：出港区发布 + 按缺认领。

    元数据（images 字段里的文件名）走事件线，二进制走这里——两条腿各自
    幂等：文件名 = uuid+内容一次写死（save_image_file 从不覆写），所以
    「本地已有」就是认领的终点条件，无需 LWW、无需回声抑制。

    出港：本机任务引用的图片，复制一份到 <sync_root>/assets/taskstage/
    （sync_root 按 config 注释必须 ASCII——images_dir 常在中文路径下，
    scp 直拉它会碎在代码页上，这正是出港区存在的原因）。任务删除后
    引用消失，出港副本顺手清掉，不让出港区变成没人扫的仓库。

    认领：本机任务引用、本地却没有的图片（对端投的任务刚同步过来），逐张
    scp 对端出港区。对端还没出港不算故障（它的发布拍还没跑到），单图退避；
    连不上是常态（对端下班关机），整线退避且不举手——事件线的 Ferry 已经
    负责「对端消失 20 分钟才报一声」，这里再叫就是重复响铃。
    """

    def __init__(self, storage_getter, sync_root, peer_ssh, peer_root,
                 alert=None):
        self._storage = storage_getter      # () -> TaskStageStorage|None
        self.enabled = bool(sync_root and peer_ssh and peer_root)
        self.port_dir = (Path(sync_root) / ASSET_SUBDIR) if sync_root else None
        self.peer_ssh = str(peer_ssh or "")
        root = str(peer_root or "").replace("\\", "/").rstrip("/")
        self.remote_dir = "{}/{}".format(root, ASSET_SUBDIR) if root else ""
        self.alert = alert or (lambda msg: None)
        self.published = 0
        self.fetched = 0
        self.pruned = 0
        self.last_error = ""
        self._next_try = {}                 # 文件名 -> 下次尝试时刻
        self._down_until = 0.0

    @staticmethod
    def _refs(st) -> set:
        names = set()
        for task in st.list_tasks():
            for img in task.get("images") or []:
                name = str((img or {}).get("file") or "")
                if _IMG_NAME_RE.match(name):
                    names.add(name)
        return names

    def poll_once(self) -> int:
        if not self.enabled:
            return 0
        st = self._storage()
        if st is None:
            self.last_error = "任务站库未就绪"
            return 0
        self.last_error = ""
        refs = self._refs(st)
        return self._publish(st, refs) + self._claim(st, refs)

    # ---------- 出港 ----------
    def _publish(self, st, refs) -> int:
        n = 0
        for name in sorted(refs):
            src = Path(st.images_dir) / name
            dst = self.port_dir / name
            if dst.exists() or not src.is_file():
                continue
            try:
                self.port_dir.mkdir(parents=True, exist_ok=True)
                tmp = dst.with_name(dst.name + ".ferrytmp")
                shutil.copyfile(src, tmp)
                os.replace(tmp, dst)
                self.published += 1
                n += 1
            except OSError as e:
                self.last_error = "出港 {} 失败: {}".format(name, e)
        try:
            for p in self.port_dir.iterdir() if self.port_dir.is_dir() else ():
                if p.name not in refs and _IMG_NAME_RE.match(p.name):
                    p.unlink(missing_ok=True)
                    self.pruned += 1
        except OSError:
            pass
        return n

    # ---------- 认领 ----------
    def _claim(self, st, refs) -> int:
        now = time.time()
        if now < self._down_until:
            return 0
        got = 0
        for name in sorted(refs):
            if got >= _ASSET_PULLS_PER_POLL:
                break
            if (Path(st.images_dir) / name).exists() \
                    or now < self._next_try.get(name, 0):
                continue
            if self._fetch_one(st, name, now):
                got += 1
            elif now < self._down_until:
                break                       # 连接级故障，这一拍不用再试别的
        if got:
            # 图片落齐的那一刻任务元数据早就在库里了，控制台下一拍自然亮出来
            self._next_try = {k: v for k, v in self._next_try.items()
                              if v > now}
        return got

    def _fetch_one(self, st, name, now) -> bool:
        images_dir = Path(st.images_dir)
        tmp = images_dir / (name + ".ferrytmp")
        cmd = ["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
               "{}:{}/{}".format(self.peer_ssh, self.remote_dir, name),
               str(tmp)]
        try:
            images_dir.mkdir(parents=True, exist_ok=True)
            r = subprocess.run(cmd, capture_output=True, timeout=90,
                               creationflags=_CREATE_NO_WINDOW)
        except Exception as e:  # noqa: BLE001  scp 不在 PATH / 硬超时
            self._rm(tmp)
            self._down_until = now + _ASSET_DOWN_SECS
            self.last_error = "scp 拉不动: {}".format(e)
            return False
        if r.returncode != 0:
            self._rm(tmp)
            err = (r.stderr or b"").decode("utf-8", "replace").strip()
            if _MISSING_RE.search(err):
                # 对端可达、只是还没出港——它的发布拍最长 poll_secs 后跑到
                self._next_try[name] = now + _ASSET_RETRY_SECS
                return False
            self._down_until = now + _ASSET_DOWN_SECS
            self.last_error = err or "scp 退出码 {}".format(r.returncode)
            return False
        try:
            size = tmp.stat().st_size
        except OSError:
            size = -1
        if not (0 < size <= MAX_ASSET_BYTES):
            # 空件/超限件不落地：save_image_file 从不产出这种文件，出现即
            # 说明对端出港区被污染，退避一天，别每拍搬一次垃圾
            self._rm(tmp)
            self._next_try[name] = now + 86400
            self.last_error = "认领 {} 尺寸异常（{}B），弃".format(name, size)
            return False
        os.replace(tmp, images_dir / name)
        self._next_try.pop(name, None)
        self.fetched += 1
        return True

    @staticmethod
    def _rm(tmp: Path):
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass

    def status(self):
        return {"enabled": self.enabled, "published": self.published,
                "fetched": self.fetched, "pruned": self.pruned,
                "waiting": len(self._next_try), "last_error": self.last_error}


# ---------------- 存储定位与 hub 接线 ----------------
_BOARD_STORAGE = None
_BOARD_RETRY_AT = 0.0


def _board_storage():
    """console 域的看板库：与 share_server._task_storage 同一套住法（importlib
    直读源码文件，打包态回 exe 内部归档），库目录用 console 自己的
    default_data_dir()——两边永远指同一个 .board.json。"""
    global _BOARD_STORAGE, _BOARD_RETRY_AT
    if _BOARD_STORAGE is not None:
        return _BOARD_STORAGE
    now = time.time()
    if now < _BOARD_RETRY_AT:
        return None
    _BOARD_RETRY_AT = now + 30
    try:
        import importlib.util
        from share_server import _console_root
        app_dir = Path(__file__).resolve().parent
        storage_py = _console_root() / "console" / "api" / "board" / "storage.py"
        if not storage_py.is_file():
            storage_py = app_dir.parent / "console" / "api" / "board" / "storage.py"
        if storage_py.is_file():
            spec = importlib.util.spec_from_file_location("_board_sync_storage",
                                                          storage_py)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        else:
            import importlib
            mod = importlib.import_module("api.board.storage")
        _BOARD_STORAGE = mod.BoardStorage()
    except Exception:  # noqa: BLE001  console 半套/路径没就绪：下拍再试
        _BOARD_STORAGE = None
    return _BOARD_STORAGE


class ConsoleSync:
    """三条业务线的编排：一根线程按同一拍长轮询（各线独立 try，谁塌了谁自己
    躺 last_error，不连坐）。"""

    def __init__(self, bus, cfg, datadir, sessions_path, alert=None,
                 board_getter=None, task_getter=None):
        cfg = cfg or {}
        poll = float(cfg.get("sync_console_poll_secs", 20) or 20)
        self.poll_secs = max(3.0, poll)
        if task_getter is None:
            import share_server
            task_getter = share_server._task_storage
        self.board = BoardSync(bus, board_getter or _board_storage,
                               datadir, alert=alert)
        self.task = TaskSync(bus, task_getter, datadir, alert=alert)
        self.sess = SessSync(bus, sessions_path, datadir, alert=alert)
        # 图片摆渡与事件线同一拍：凭据不齐（单机跑/没配对端）就静默停用
        self.assets = TaskAssetFerry(task_getter, cfg.get("sync_root"),
                                     cfg.get("sync_peer_ssh"),
                                     cfg.get("sync_peer_root"), alert=alert)
        self._stop = threading.Event()
        self._thread = None

    def poll_once(self) -> int:
        n = 0
        for line in (self.board, self.task, self.sess, self.assets):
            try:
                n += line.poll_once()
            except Exception as e:  # noqa: BLE001
                line.last_error = str(e)
        return n

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.poll_secs)

    def status(self):
        return {"board": self.board.status(), "task": self.task.status(),
                "sess": self.sess.status(), "assets": self.assets.status(),
                "poll_secs": self.poll_secs}


def attach(bus, cfg, datadir, sessions_path, alert=None):
    """hub 接线：bus 在跑才有意义；datadir 是 hub 机器态目录（基线与对端
    会话副本都落这儿），sessions_path 是本机 .sessions.json。"""
    return ConsoleSync(bus, cfg, datadir, sessions_path, alert=alert).start()
