# -*- coding: utf-8 -*-
r"""双活数据同步 P0：每机单写事件日志 + 对端幂等重放（方案 A「事件互灌」骨架）。

设计稿：docs/plans/2026-08-27-双活数据同步-事件互灌设计.md（rxyy 拍板走 A）。
背景事故：08-27 家机挂机，代码有 repo-sync 兜着，但看板/任务/记忆/聊天全是
单机数据，接手 c0cfa23a 时全部断档。sqlite 活库不能文件级双向同步（半事务
互相覆盖必撕库，console/api 里还躺着两具 .sync-conflict 尸体），所以同步单位
从「文件」上升到「业务事件」，存储文件退化为各机的物化视图。

拓扑（§4.1）：
    公司机 hub ──emit──> <sync_root>\events\company.jsonl ─┐
                                                           ├─ Syncthing 双向夹
    家机   hub ──emit──> <sync_root>\events\home.jsonl   ──┘
    每机 replayer 只 tail 对端文件 → 本地重放

成立根基：**每机只追加写自己的 outbox**，对端只读——append-only + 单写者，
Syncthing 层面天然零冲突。重放靠 uuid 幂等（applied 台账私有落盘，不进同步夹：
它是「本机吃到哪了」的进度，不是共享事实）；定序 LWW 按 (ts, machine)。

P0 交付边界（§4.4）：outbox writer、replayer 骨架、applied 台账、时钟检查、
demo store 验收口。家机挂着期间只在公司机铺设：config `sync_root` 留空 =
整个模块不启动，对现网零影响；Syncthing 专用夹的两机配对等家机复活后做
（开放问题 §6.1）。看板/任务/聊天的真实 emit 挂接是 P1/P2，不在本文件。

纪律（§4.5 风险对策）：
- 坏行：每行独立 JSON，坏的跳过、计数、举手一次，绝不让一行坏字节堵死全队。
- 高版本事件（对端先升级了 schema）：立即停掉该对端的重放并举手，不瞎猜——
  停在原地是安全态，升级本机后从停点继续。
- 重放风暴（长断网恢复）：每拍每对端最多吃 MAX_BATCH 行，剩下的下一拍继续。
- handler 抛异常：停在该事件上下拍重试（举手一次）——P0 只有 demo store，
  宁可停住可见，不可跳过丢事件。
"""
from __future__ import annotations

import json
import os
import re
import socket
import struct
import threading
import time
import uuid as uuid_mod
from pathlib import Path

SCHEMA_V = 1          # 事件封装版本；对端事件 v 比它大 = 本机代码旧了，停机举手
MAX_BATCH = 500       # 每拍每对端最多重放行数（长断网恢复时的风暴闸）
LEDGER_UUID_KEEP = 4000   # applied 台账里保留的幂等键条数（约两周量，超出滚动）
EVENTS_DIRNAME = "events"

_MACHINE_RE = re.compile(r"[^0-9A-Za-z_-]+")


def default_machine() -> str:
    """机器名兜底：hostname 洗成文件名安全的短标识（设计稿用 company/home，
    config `sync_machine` 显式配置优先，这里只是没配时的默认）。"""
    name = _MACHINE_RE.sub("-", socket.gethostname().strip().lower()) or "machine"
    return name[:24]


def lww_wins(new_ts, new_machine, old_ts, old_machine) -> bool:
    """LWW 定序（§4.2）：按 (ts, machine) 全序，机器名只在时间戳完全相同时
    当决胜局——不管谁比谁，两机裁决结果一致就行。"""
    return (float(new_ts or 0), str(new_machine or "")) \
        >= (float(old_ts or 0), str(old_machine or ""))


# ---------------- outbox：本机唯一写入口 ----------------
class Outbox:
    """append-only 单写者。hub 是本机所有业务写入的收拢点（boardctl「所有人走
    同一个门」、聊天全走 hub 单进程），所以 outbox 只需要线程锁，不需要跨进程锁。"""

    def __init__(self, sync_root, machine):
        self.machine = machine
        self.path = Path(sync_root) / EVENTS_DIRNAME / (machine + ".jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq = self._restore_seq()

    def _restore_seq(self) -> int:
        """重启接续 seq：读自己文件的最后一条有效事件。文件是自己写的，一般
        整洁；万一尾部有半行（断电），往前找最后一条能解析的。"""
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0
        for line in reversed(lines):
            try:
                return int(json.loads(line).get("seq") or 0)
            except Exception:
                continue
        return 0

    def emit(self, store, op, entity, payload=None, version=None) -> dict:
        """追加一条业务事件并立即落盘（open-append-close：写完就关，Syncthing
        的变更扫描才追得上；P0 事件频率低，每次开关文件毫秒级，不值得优化）。"""
        with self._lock:
            self._seq += 1
            ev = {
                "v": SCHEMA_V,
                "seq": self._seq,
                "uuid": uuid_mod.uuid4().hex,
                "ts": time.time(),
                "machine": self.machine,
                "store": str(store),
                "op": str(op),
                "entity": str(entity),
            }
            if version is not None:
                ev["version"] = version
            if payload is not None:
                ev["payload"] = payload
            line = json.dumps(ev, ensure_ascii=False)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return ev


# ---------------- applied 台账：本机私有进度 ----------------
class AppliedLedger:
    """「对端 <peer> 的事件我吃到哪了」。落在本机 datadir（不进同步夹）：
    它是每台机各自的消费进度，同步出去毫无意义还会互相打架。

    offset 是快进用的字节位置；uuids 才是幂等的真判据——文件被重建/回滚时
    offset 会失效重置，靠 uuids 挡住重复应用（§4.2「断网只是积压」的另一半：
    恢复后无重复）。"""

    def __init__(self, datadir, peer):
        self.path = Path(datadir) / (".sync-applied-{}.json".format(peer))
        self.offset = 0
        self.uuids = []           # 有序（旧→新），滚动保留 LEDGER_UUID_KEEP 条
        self._seen = set()
        self.held = ""            # 非空 = 该对端已停机（高版本/handler 连败）
        self.bad_lines = 0
        self.applied_count = 0
        self.last_apply_ts = 0.0
        self._load()

    def _load(self):
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            self.offset = int(d.get("offset") or 0)
            self.uuids = [str(u) for u in (d.get("uuids") or [])]
            self.held = str(d.get("held") or "")
            self.bad_lines = int(d.get("bad_lines") or 0)
            self.applied_count = int(d.get("applied_count") or 0)
            self.last_apply_ts = float(d.get("last_apply_ts") or 0)
        except Exception:
            pass  # 台账坏了/不存在：从零开始，uuid 去重保证从零重放也不重复应用
        self._seen = set(self.uuids)

    def save(self):
        tmp = str(self.path) + ".tmp"
        data = {"offset": self.offset, "uuids": self.uuids[-LEDGER_UUID_KEEP:],
                "held": self.held, "bad_lines": self.bad_lines,
                "applied_count": self.applied_count,
                "last_apply_ts": self.last_apply_ts}
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, self.path)
        self.uuids = self.uuids[-LEDGER_UUID_KEEP:]

    def seen(self, ev_uuid) -> bool:
        return ev_uuid in self._seen

    def mark(self, ev_uuid):
        self._seen.add(ev_uuid)
        self.uuids.append(ev_uuid)
        self.applied_count += 1
        self.last_apply_ts = time.time()


# ---------------- replayer：只读对端，幂等应用 ----------------
class Replayer:
    def __init__(self, sync_root, machine, datadir, alert=None):
        self.events_dir = Path(sync_root) / EVENTS_DIRNAME
        self.machine = machine
        self.datadir = Path(datadir)
        self.alert = alert or (lambda msg: None)
        self.handlers = {}
        self._ledgers = {}
        self._alerted = set()     # 同一件事只举手一次（按 key 记）

    def register(self, store, fn):
        """fn(event) -> None；抛异常 = 停在该事件上，下一拍重试（见模块纪律）。"""
        self.handlers[str(store)] = fn

    def _alert_once(self, key, msg):
        if key in self._alerted:
            return
        self._alerted.add(key)
        try:
            self.alert(msg)
        except Exception:
            pass

    def _ledger(self, peer) -> AppliedLedger:
        if peer not in self._ledgers:
            self._ledgers[peer] = AppliedLedger(self.datadir, peer)
        return self._ledgers[peer]

    def peers(self):
        try:
            names = [p.stem for p in self.events_dir.glob("*.jsonl")]
        except OSError:
            return []
        return sorted(n for n in names if n and n != self.machine)

    def poll_once(self) -> int:
        """吃一轮所有对端的新事件，返回本轮应用条数。"""
        applied = 0
        for peer in self.peers():
            applied += self._poll_peer(peer)
        return applied

    def _poll_peer(self, peer) -> int:
        led = self._ledger(peer)
        if led.held:
            return 0
        path = self.events_dir / (peer + ".jsonl")
        try:
            size = path.stat().st_size
        except OSError:
            return 0
        if led.offset > size:
            # 对端文件变小=被重建/回滚（不该发生，但 Syncthing 冲突处理或人工
            # 修复都可能造成）：offset 作废从头重读，uuid 去重挡住重复应用
            self._alert_once("shrink:" + peer,
                             "双活同步：对端 {} 的事件文件变小（{}→{}B），"
                             "重置读位从头核对（幂等台账防重复）".format(
                                 peer, led.offset, size))
            led.offset = 0
        if led.offset == size:
            return 0
        with open(path, "rb") as f:
            f.seek(led.offset)
            blob = f.read()
        base = led.offset
        i = 0            # blob 内游标；led.offset 最终推进到 base + i
        applied = 0
        while applied < MAX_BATCH:   # 风暴闸：本拍到此为止，进度已保存，下拍继续
            nl = blob.find(b"\n", i)
            if nl < 0:
                break  # 尾部半行（对端正在写/Syncthing 传输中途）：不吃，等补全
            line = blob[i:nl].decode("utf-8", "replace").strip()
            next_i = nl + 1
            if not line:
                i = next_i
                continue
            try:
                ev = json.loads(line)
                if not isinstance(ev, dict) or not ev.get("uuid"):
                    raise ValueError("不是事件对象")
            except Exception:
                led.bad_lines += 1
                self._alert_once(
                    "bad:{}:{}".format(peer, base + i),
                    "双活同步：对端 {} 事件文件 offset={} 有坏行，已跳过"
                    "（累计 {} 行）".format(peer, base + i, led.bad_lines))
                i = next_i
                continue
            if int(ev.get("v") or 0) > SCHEMA_V:
                # 对端先升级了事件格式：停在这条行首，绝不瞎猜着应用；
                # 本机升级后 held 由升级流程清掉，从停点自然继续
                led.held = ("对端 {} 的事件 v={} 高于本机 v={}（对端先升级了），"
                            "该对端重放已停——升级本机 rxyy tools 后自动继续"
                            ).format(peer, ev.get("v"), SCHEMA_V)
                self._alert_once("heldv:" + peer, "双活同步停机：" + led.held)
                break
            if led.seen(ev["uuid"]):
                i = next_i   # 幂等：重放过的直接快进
                continue
            fn = self.handlers.get(str(ev.get("store") or ""))
            if fn is None:
                # 同版本却没有 handler：多半是 P1/P2 的 store 先于本机代码出现
                # （灰度窗口）。停机会把 demo 一起堵死，静默跳过会丢事件——
                # 举手一次+记账跳过；事件仍在对端文件里，补上 handler 后清
                # 台账可全量重放
                self._alert_once(
                    "nostore:{}:{}".format(peer, ev.get("store")),
                    "双活同步：对端 {} 发来未注册的 store「{}」事件，已跳过"
                    "（uuid={}…）".format(peer, ev.get("store"),
                                          str(ev.get("uuid"))[:8]))
                led.mark(ev["uuid"])
                i = next_i
                continue
            try:
                fn(ev)
            except Exception as e:  # noqa: BLE001
                # 停在这条行首，下一拍重试：P0 宁可停住可见，不可跳过丢事件
                self._alert_once(
                    "apply:{}".format(ev["uuid"]),
                    "双活同步：应用对端 {} 事件失败（store={} uuid={}…）：{}"
                    "——停在该事件上，下拍重试".format(
                        peer, ev.get("store"), ev["uuid"][:8], e))
                break
            led.mark(ev["uuid"])
            applied += 1
            i = next_i
        if i:
            led.offset = base + i
        if i or led.held:
            led.save()
        return applied

    def status(self):
        out = {}
        for peer in self.peers():
            led = self._ledger(peer)
            try:
                size = (self.events_dir / (peer + ".jsonl")).stat().st_size
            except OSError:
                size = 0
            out[peer] = {
                "offset": led.offset,
                "lag_bytes": max(0, size - led.offset),
                "applied": led.applied_count,
                "bad_lines": led.bad_lines,
                "held": led.held,
                "last_apply_ts": led.last_apply_ts,
            }
        return out


# ---------------- 时钟检查（P0 验收项：LWW 的前提是两机时钟漂移秒级） ----------------
def sntp_skew(host="time.windows.com", timeout=2.0):
    """SNTP 单发查询本机时钟偏移（秒，本机快为正）；网络不通返回 None。
    公司网若封 UDP/123 就拿不到——None 不算失败，只是没证据，别拿它吓人。"""
    try:
        addr = socket.getaddrinfo(host, 123, socket.AF_INET, socket.SOCK_DGRAM)[0][4]
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        try:
            t0 = time.time()
            s.sendto(b"\x1b" + 47 * b"\0", addr)
            data, _ = s.recvfrom(64)
            t3 = time.time()
        finally:
            s.close()
        if len(data) < 48:
            return None
        # transmit timestamp（秒.小数），NTP 纪元 1900 → epoch 差 2208988800
        secs, frac = struct.unpack("!II", data[40:48])
        server = secs - 2208988800 + frac / 2 ** 32
        return (t0 + t3) / 2 - server
    except Exception:
        return None


# ---------------- 总线：hub 只跟它打交道 ----------------
class SyncBus:
    def __init__(self, sync_root, machine, datadir, alert=None, poll_secs=2.0):
        self.sync_root = Path(sync_root)
        self.machine = machine
        self.datadir = Path(datadir)
        self.alert = alert or (lambda msg: None)
        self.poll_secs = max(0.5, float(poll_secs))
        self.outbox = Outbox(sync_root, machine)
        self.replayer = Replayer(sync_root, machine, datadir, alert=alert)
        self.clock_skew = None    # 最近一次 SNTP 结果（None=没证据）
        self._stop = threading.Event()
        self._thread = None
        self._register_builtin()

    # -- demo store：P0 验收专用（A 机 emit，B 机 5s 内落地，重放两遍不重复） --
    def _register_builtin(self):
        self.replayer.register("demo", self._apply_demo)

    def _demo_path(self) -> Path:
        return self.datadir / ".sync-demo.json"

    def _apply_demo(self, ev):
        path = self._demo_path()
        try:
            cur = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            cur = {}
        ent = str(ev.get("entity") or "demo")
        old = cur.get(ent) or {}
        # LWW：旧值更新的话来件不覆盖（两机各自裁决，结论一致）
        if old and not lww_wins(ev.get("ts"), ev.get("machine"),
                                old.get("ts"), old.get("machine")):
            return
        cur[ent] = {"ts": ev.get("ts"), "machine": ev.get("machine"),
                    "payload": ev.get("payload")}
        tmp = str(path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    def demo_emit(self, text="ping"):
        return self.outbox.emit("demo", "update", "demo", payload={"text": text})

    # -- 后台线程 --
    def start(self):
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _loop(self):
        # 第一拍先做时钟检查（LWW 前提）：漂移超 2s 才举手，拿不到证据不吓人
        self.clock_skew = sntp_skew()
        if self.clock_skew is not None and abs(self.clock_skew) > 2.0:
            try:
                self.alert("双活同步：本机时钟与 NTP 相差 {:+.1f}s，超过 LWW 容忍度"
                           "（秒级）——先校时再指望冲突定序".format(self.clock_skew))
            except Exception:
                pass
        while not self._stop.is_set():
            try:
                self.replayer.poll_once()
            except Exception as e:  # noqa: BLE001
                try:
                    self.alert("双活同步轮询异常：{}".format(e))
                except Exception:
                    pass
            self._stop.wait(self.poll_secs)

    def status(self):
        return {
            "machine": self.machine,
            "sync_root": str(self.sync_root),
            "outbox_seq": self.outbox._seq,
            "clock_skew": self.clock_skew,
            "peers": self.replayer.status(),
        }


def start_from_config(cfg, datadir, alert=None):
    """hub 启动接线：config `sync_root` 留空 = 双活未启用（P0 现状——Syncthing
    专用夹要等家机复活配对，见设计稿 §6.1），返回 None、全程零影响。"""
    root = str((cfg or {}).get("sync_root") or "").strip()
    if not root:
        return None
    machine = _MACHINE_RE.sub("-", str(cfg.get("sync_machine") or "").strip().lower())
    machine = (machine or default_machine())[:24]
    bus = SyncBus(root, machine, datadir, alert=alert,
                  poll_secs=float(cfg.get("sync_poll_secs", 2.0) or 2.0))
    return bus.start()
