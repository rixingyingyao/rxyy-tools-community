# -*- coding: utf-8 -*-
"""会话状态机（hub 拆分第四刀，2026-08-13）。

hub.py 三刀+身份七守卫+判活融合后涨到 ≈8.6k 行，会话生命周期逻辑（判活融合 /
认领判定 / Session 快照 / detach·死亡清算）与团队协调、聊天落盘、UI 托管仍搅在
一个文件里。本模块承接「会话状态机」——hub.py 只留编排与团队协调。

约定（第一刀定稿，与 hub_api.py 同款，docs/hub拆分手术方案-2026-08-12.md）：
- 顶层只 import 标准库 + `import hub`；hub 的模块符号一律运行时 `hub.xxx` 访问
  ——测试对 hub 的打桩（patch.object(hub, "read_cursor_activity") 等）才拦得住
  本模块内部的调用；
- hub.Hub / hub_api.Api 的原方法保留为一行委托，所有 patch.object(hub.Hub, …) /
  patch.object(hub.Api, …) 打桩点原样有效；
- 语义一字不动：七守卫+判活融合全部有以事故命名的测试锁着（基线 1304），
  搬错一处测试立刻红。
"""
import json
import os
import threading
import time
import uuid
from pathlib import Path

import hub


# ---------------- Session：一个对话 tab 的全部状态 ----------------

class Session:
    """一个对话 tab（同一 MCP 连接上按 conversation_id 区分；无 ID 时为默认对话）"""

    def __init__(self, client, conv_key, name):
        self.id = uuid.uuid4().hex[:8]
        self.client = client
        self.conv_key = conv_key
        self.cwd = client.cwd
        # 任务归属项目：建 tab 那一刻的工作区，此后不再被报到覆盖。
        # cwd 跟着「这一刻是谁在报到」走，task_root 跟着「这个活是谁家的」走——
        # 工作区2 的闲置 agent 接手工作区1 挂掉的活时，两者就会分叉（用户实测串台）。
        self.task_root = client.cwd
        # 没钉住时归属下拉跟 cwd；set_task_root / 席位 / 成员登记会把它钉住。
        self.task_root_locked = False
        self.memory_sent_ts = 0.0
        self.pid = client.pid
        self.peer_ip = client.peer_ip
        self.name = name
        self.name_locked = False  # 用户手动重命名后，不再被 task_name 自动覆盖
        # 出生时就是个「报到壳」（名字是 待命·<工作区>）。随快照持久化，
        # 此后无论 tab 被改成什么名字都不再变——收壳与身份校准只认它
        self.shell_born = bool(hub.CHECKIN_SHELL_NAME_RE.match(str(name or "")))
        # agent 明说过自己叫什么（task_name 不是报到壳名）。随快照持久化。
        # 立起来之后：Cursor 自动标题不再冲它，session_label 也不再拿那截
        # 自动分工盖它——只有 agent 知道自己在做哪个项目的哪块功能。
        self.agent_named = False
        self.agent_project = ""   # 从「项目·功能」里取出的项目那半，团队分组用
        # 已经拿「你不是待命壳、接着干」叫醒过一次（见 checkin_takeover_notice）。
        # 刻意不进快照：hub 重启后 agent 的上下文往往也是新的，那就该再叫一次。
        # 另两个是「叫醒那一刻它干到哪儿了」的存根，用来分辨下一次报到到底是
        # 「压根没叫醒」还是「又失忆了一回」。
        self.checkin_bounced_ts = 0.0
        self.checkin_bounced_seq = 0
        self.checkin_bounced_zt = 0.0
        self.runtime_kind = "cursor"  # 无元数据的旧客户端仍走 Cursor 路径
        self.native_thread_id = ""
        # 用户从控制台主动“继续原任务”的单次投递账本。按原 thread + terminal
        # turn 锁定；必须随快照保存，换装后也不能把未知回执当成可重试。
        self.native_resume_ledger = []
        self.cursor_uuid = None   # 定位到的 Cursor 会话 UUID（transcript 目录名）
        self.uuid_verified = False  # UUID 是否被「到达时刻在生成」铁证验过（验过的不许被抢）
        self.cursor_title = None  # 上次从 Cursor 同步过来的标题（防抖：改名后不反复重命名）
        self.transcript_path = None  # 定位到的 transcript 文件路径
        self.created_ts = time.time()
        self.connected = True
        # 用户在控制台点过一次 × ：会话收进「已结束」组待清理，仍可回看/接手；
        # 在组里再点一次 × 才真从列表里删（记录文件任何时候都不动）
        self.archived = False
        # 多选接手落地：原 tab 进已结束且不得被原 ID 报到复活（09-07）
        self.handoff_final = False
        self.end_reason = ""
        self.pending_lost = False  # 断开时是否有未回复的提问（决定 UI 用弹窗还是轻提示）
        self.recon_deadline = None  # 断线重连宽限截止；期内 UI 显示「重连中」不报断开
        self.auto_reconnect_blocked = False  # 已确认死亡/离线的快照不靠换装心跳复活
        self.lost_pending_on_drop = False  # 掉线瞬间是否有未回复提问（宽限过后才升级成 pending_lost）
        self.last_heartbeat = time.time()  # 最近一次 MCP 心跳时刻（进程存活的实时证据）
        self.agent_status = ""             # agent 主动上报的干活状态（analyzing/developing…）
        self.agent_activity = ""           # 一句话活动描述
        self.agent_status_ts = 0           # 上报时刻
        # 最近几条 zt 的轨迹（"08-28 13:40 testing · 全量测试跑着"）。agent_status 只有
        # 最后一条、重启即清；接手词要靠这几行告诉接手方「前任断线前正在做什么」
        # （08-28 f9c313ed 实测：前任提交完就断、没走 zhi，聊天记录里零线索，
        # 接手方翻了七八处才对上进度）。有界、随快照持久化。
        self.zt_trail = []
        self.death_info = None    # 主动查到的致命报错（欠费/额度到顶/被模型方拒…）
        self.death_probe_at = 0.0  # 上次查死因的时刻（限频用）
        self.death_probe_uid = None  # 上次查死因用的对话 uid（换绑当拍清死因，见 tick_death_probe）
        self.model_info = None     # 这个 tab 用的模型（读 Cursor 库 / Codex 配置，面板纯读）
        self.model_probe_at = 0.0  # 上次查模型的时刻（限频用）
        self.model_probe_uid = None  # 上次读牌用的对话 uid（换绑当拍换牌，见 tick_model_probe）
        self.reported_model = ""   # agent 在 zt/zhi 里报的模型（Codex 没有 Cursor 库）
        self.messages = []
        self.msg_seq = 0  # 消息序号（不含 zt 状态上报），未读角标数它
        # 真正由用户/agent 产生的消息数。它不能从 msg_seq - machine_seq 推导：
        # 快照会裁消息列表，而 machine_seq 是整个会话的累计值，二者相减会倒退到 0。
        self.real_seq = 0
        # 其中有多少条是机器塞进来的（队友转告 / 黑板提醒 / 控制台回执）。
        # 未读角标该数它们（用户确实有新东西要看）。随快照持久化，供旧快照兼容。
        self.machine_seq = 0
        self.pending = None  # {'id', 'message', 'options', 'is_markdown', 'artifacts'}
        self.detached = False  # 保活脱离期：MCP 已提前返回、暂无等待方，用户回复先缓存
        self.detached_since = None  # 进入脱离期的时刻；超过 grace 仍无续期即判定该轮失联
        self.buffered_reply = None  # 脱离期缓存的用户回复，等下次 zhi 续期重呼时交付
        # 当前 pending 是 zhi(wait=false) 只发不等挂上的：agent 明说了「我先走、回头来收」，
        # 失联看门狗对它只挂起不清（与「认领了活」同等待遇），不随快照持久化（pending 也不）
        self.wait_deferred = False
        self.disconnected_at = None  # 最近一次通道断开时刻（自愈看门狗用）
        # 最近一次已发出的回复探针：{"ts","text","images","files","who",
        # "msg_ref","question","q_options"}。Cursor 中断工具调用时不发 cancelled
        # 通知，回复会被吞——靠它检测并补送。question/q_options 是这条回复所
        # 应答的那道题：agent 原样重问同一道题即回复没送到的铁证（重问补送闸）
        self.last_reply_probe = None
        self.processing_since = None  # 用户回复后等待 AI 再次 zhi；None 表示待机
        self.queued = []  # AI 处理中用户提前发送、等待下次提问自动送出的消息
        self.draft_text = ""  # 输入框未发送草稿（随快照持久化，重启控制台不丢字）
        self.draft_images = []  # 输入框未发送的图片（dataURL），随快照持久化
        self.draft_files = []   # 输入框未发送的文件，随快照持久化
        self.library_sent = False  # 资料索引是否已随派活发过（随快照持久化，只发一次）
        self.handed_off_to = ""    # 交接在途：本 tab 的 agent 被派去驱动哪个对话。
        # 落地即清（takeover_landed，B步 08-26）；只有归档 tab 上它才长期留着
        # ——那是 _handover_out 的史实指针，转告跟随/误判自愈靠它
        self.takeover_dispatched = None  # 接手在途：{"to_name","to_conv","ts","warned"}
        # 曾用对话ID：接手落地时被收起的那些空壳的 ID 都并到这里。队友手上常拿着
        # 接手方报到时的临时 ID（面板/传话记录里见过），按它转告不该扑空
        self.id_history = []
        # 「认领了活」的时刻（0=还没有）：收过真实用户任务 / 报过 zt / agent 自报过名。
        # 随快照持久化——agent_status_ts 重启即清零，靠内存字段判「开没开工」会在
        # 重启后失忆。认领过的会话永不被自动收壳/归并/改身份（见 Hub._claimed_task；
        # 2026-08-12 事故：已开工的 c00f0d0a 被接手落地当空壳归并进 1a49a6ec）
        self.claimed_task_ts = 0.0
        self.last_zhi_ts = time.time()  # 本对话最近一次调 zhi 的时刻（静默判定用）
        self.rev = 0
        self.created_at = hub.now_full()
        self.file_path = None
        self.lock = threading.Lock()

    def send(self, obj):
        self.client.send(obj)


# ---------------- Session ↔ 快照 dict（save_state/load_state 的会话半边） ----------------

_LEGACY_OFFLINE_MIGRATION_SECS = 15 * 60


def _bound_native_desktop_connected(s):
    """当前绑定的 Codex thread 是否有桌面连接证据；拒绝别的 thread 的旧缓存。"""
    if hub.runtime_adapter.kind(s) != "codex":
        return False
    thread_id = hub.runtime_adapter.valid_thread_id(
        getattr(s, "native_thread_id", ""))
    if not thread_id:
        return False
    view_id = hub.runtime_adapter.valid_thread_id(
        getattr(s, "native_turn_view_id", ""))
    if (getattr(s, "native_desktop_connected", False)
            and (not view_id or view_id == thread_id)):
        return True
    view = getattr(s, "native_turn_view", None)
    return bool(view_id == thread_id and isinstance(view, dict)
                and view.get("desktop_connected"))


def _legacy_snapshot_reconnect_blocked(d, now=None):
    """迁移首次换装时由旧 hub 写出的快照，不用历史提醒表猜当前生死。

    老快照没有 auto_reconnect_blocked，也没有可持久化的 death_info。唯一可靠的
    当前终态记录是 _finalize_disconnect 写入的系统气泡；若后来复活，又会写入明确
    的恢复气泡。只有心跳已长期过期、且最近的生命周期气泡仍是正式断线，才跳过
    90 秒重连宽限。这样历史 death_alerts 和一次短抖动都不会封住活会话。
    """
    if "auto_reconnect_blocked" in d:
        return False
    try:
        last_heartbeat = float(d.get("last_heartbeat") or 0)
    except (TypeError, ValueError):
        return False
    now = time.time() if now is None else float(now)
    if last_heartbeat <= 0 or now - last_heartbeat < _LEGACY_OFFLINE_MIGRATION_SECS:
        return False
    for msg in reversed(d.get("messages") or []):
        if not isinstance(msg, dict) or msg.get("role") != "sys":
            continue
        text = str(msg.get("html") or msg.get("text") or "").strip()
        if text.startswith("连接已恢复") or text.startswith("交接误判自愈"):
            return False
        if text.startswith("会话已断开") and "记录已保存:" in text:
            return True
    return False


def session_to_snapshot(hub_self, s):
    """单个会话 → 快照 dict（save_state 的会话半边；坏字段抛出去由调用方计 broken）。"""
    with s.lock:
        cap = max(40, int(hub_self.cfg.get("max_messages", 200) or 200))
        msgs = list(s.messages)[-cap:]
        msg_seq = max(0, int(getattr(s, "msg_seq", 0) or 0))
        machine_seq = max(0, int(getattr(s, "machine_seq", 0) or 0))
        real_seq_val = hub_self._real_seq(s)
        # 排队中的用户消息必须随快照走：07-31 实测用户 14:10 排队的长消息
        # 因重启丢失，agent 压根没收到，用户只看到一条「已撤回」
        queued = [dict(q) for q in (s.queued or [])]
        # 只发不等期间用户回过、agent 还没来取的那条回复同理：pending 不落盘
        # （它是活请求的一半），但用户的话不能跟着 hub 重启一起蒸发——恢复出来
        # 是没有提问的孤儿缓存，agent 重连那一刻 create_session 复活分支把它转
        # 队列、第一次 zhi 就送到（09-03 14:50 家机重启前 3 个 tab 正挂着只发不等）
        buffered = getattr(s, "buffered_reply", None)
        buffered = dict(buffered) if isinstance(buffered, dict) else None
        dead = getattr(s, "death_info", None)
        confirmed_dead = bool(dead and not _is_transient_death(dead))
        recon = bool(getattr(s, "recon_deadline", None)
                     and time.time() <= float(s.recon_deadline))
        native_alive = _bound_native_desktop_connected(s)
        known_offline = bool(not getattr(s, "connected", True) and not recon
                             and not getattr(s, "ide_active_cache", False)
                             and not native_alive)
    # 换装只能接回换装前仍有存活证据的会话。已判死/已定案离线的 tab 若也给
    # 90s 宽限，再叠加 MCP 进程恢复的历史心跳名单，就会被每次换装凭空点亮。
    # 这个闸只挡被动心跳；原 agent 真正再次 zhi/zt 或用户手动接手续跑仍会解除。
    auto_reconnect_blocked = bool(
        getattr(s, "auto_reconnect_blocked", False)
        or confirmed_dead or known_offline)
    return {
        "id": s.id, "conv_key": s.conv_key, "name": s.name,
        "name_locked": s.name_locked, "cwd": s.cwd, "peer_ip": s.peer_ip,
        # 出生时是不是报到壳：不落盘的话，重启后已改过名的壳就再也
        # 认不回来（收壳只认这个，不认当前名字）
        "shell_born": bool(getattr(s, "shell_born", False)),
        # agent 自报过真名：不落盘的话，控制台一重启 Cursor 自动标题
        # 又会把它冲回「Persistent task reporting」那种鬼名字
        "agent_named": bool(getattr(s, "agent_named", False)),
        "agent_project": getattr(s, "agent_project", "") or "",
        # 三个累计序号不跟随消息数组裁短而倒退。real_seq 是「干过真活」
        # 的唯一依据；machine_seq 留给旧快照迁移与诊断。
        "msg_seq": msg_seq,
        "machine_seq": machine_seq,
        "real_seq": real_seq_val,
        "messages_truncated": msg_seq > sum(
            1 for m in msgs if isinstance(m, dict) and m.get("kind") != "status"),
        "task_root": getattr(s, "task_root", "") or s.cwd,
        "task_root_locked": bool(getattr(s, "task_root_locked", False)),
        "memory_sent_ts": float(getattr(s, "memory_sent_ts", 0) or 0),
        "pid": s.pid,
        "created_at": s.created_at, "file_path": s.file_path,
        "had_pending": s.pending is not None or s.pending_lost,
        "runtime_kind": hub.runtime_adapter.kind(s),
        "native_thread_id": getattr(s, "native_thread_id", "") or "",
        "native_resume_ledger": [dict(item) for item in
                                 (getattr(s, "native_resume_ledger", []) or [])
                                 if isinstance(item, dict)][-8:],
        "cursor_uuid": s.cursor_uuid, "cursor_title": s.cursor_title,
        "uuid_verified": bool(getattr(s, "uuid_verified", False)),
        "transcript_path": s.transcript_path,
        "created_ts": s.created_ts,
        "draft_text": getattr(s, "draft_text", ""),
        "draft_images": getattr(s, "draft_images", []),
        "draft_files": getattr(s, "draft_files", []),
        # 资料索引只该发一次，这个标记不落盘的话，控制台一重启
        # 老对话下次派活又收一遍（实测挨了两回）
        "library_sent": bool(getattr(s, "library_sent", False)),
        "handed_off_to": getattr(s, "handed_off_to", ""),
        # 接手在途状态跨重启保留：hub 重启是常态，重启就丢的话
        # 「派了没落地」的提醒等于没有
        "takeover_dispatched": getattr(s, "takeover_dispatched", None),
        # 用户手动关过的标签：重启后照样待在「已结束」组里等第二次 ×，
        # 不落盘的话一重启就冒充「待续」跳回列表顶上
        "archived": bool(getattr(s, "archived", False)),
        "handoff_final": bool(getattr(s, "handoff_final", False)),
        # 判死提醒冷却表（死因code→上次提醒ts）：不落盘的话 hub 一重启
        # 冷却清零，死了一天的会话又挨个响铃推手机吵一遍
        "death_alerts": dict(getattr(s, "death_alerts", None) or {}),
        # 已提醒过的死亡气泡（bubble_id→ts）：死因牌不落盘、重启后每拍都当
        # 「新出现的死亡」重走一遍提醒分支，24h 冷却一过（多半就是次日开机
        # 那次重启）几十个僵尸会话齐刷刷再推一轮手机（09-02 家机 08:41 46 条、
        # 公司机 12:38 26 条）。同一个气泡 = 同一次死亡，提醒过就永远不再提醒
        "death_alerted_bubbles": dict(getattr(s, "death_alerted_bubbles", None) or {}),
        # 曾用名：转告按旧名字找人靠它，重启不丢
        "name_history": list(getattr(s, "name_history", []) or [])[-8:],
        # 曾用对话ID（接手时被收起的空壳 ID）：转告按旧 ID 找人靠它
        "id_history": list(getattr(s, "id_history", []) or [])[-8:],
        # 「认领了活」时刻：不落盘的话重启即失忆，已开工的壳又会被
        # 接手落地当空壳归并（c00f0d0a 事故的持久化半边）
        "claimed_task_ts": float(getattr(s, "claimed_task_ts", 0) or 0),
        # Codex 没有 Cursor 库可探，agent 自报的模型跨重启还得在
        "reported_model": getattr(s, "reported_model", "") or "",
        # zt 轨迹随快照走：接手几乎总发生在 hub 重启之后，重启即清就白存了
        "zt_trail": list(getattr(s, "zt_trail", []) or [])[-5:],
        # 重启后判断「谁本该接回来却掉队了」的依据，见 wake_stragglers_after_restart
        "last_heartbeat": float(getattr(s, "last_heartbeat", 0) or 0),
        "auto_reconnect_blocked": auto_reconnect_blocked,
        "queued": queued,
        "buffered_reply": buffered,
        "messages": msgs,
    }


def session_from_snapshot(hub_self, d):
    """快照 dict → 恢复成「已断开」Session（load_state 的会话半边；不负责挂进
    sessions/order——那是 hub 编排的事）。"""
    s = Session.__new__(Session)
    s.id = d["id"]
    s.client = None
    s.conv_key = d.get("conv_key", "__default__")
    s.cwd = hub.sanitize_ws_path(d.get("cwd", ""))
    # 老快照没有 task_root：回落到 cwd，等于「按老规矩归属」，不制造假串台
    # （存量脏数据也在这里顺手治：打包目录路径折回真项目根）
    s.task_root = hub.sanitize_ws_path(d.get("task_root") or "") or s.cwd
    # 老快照没有这个字段：视为未钉住，归属下拉跟 cwd，不制造假跨工作区。
    s.task_root_locked = bool(d.get("task_root_locked"))
    s.memory_sent_ts = float(d.get("memory_sent_ts") or 0)
    # intake/席位是显式归属，优先于旧快照。否则跨工作区第一次读黑板
    # 会先落进 cwd，直到后续碰巧占文件锁才被纠正。
    hub_self._bind_registered_team_root(s)
    hub.heal_session_paths(s)
    hub.heal_named_agent_root(s)
    s.pid = d.get("pid")
    s.peer_ip = d.get("peer_ip")
    s.name = d.get("name") or "会话"
    s.name_locked = bool(d.get("name_locked"))
    # 老快照没有这个字段：回落到「当前名还是待命名」，与修复前同义
    s.shell_born = bool(d.get("shell_born")) or bool(
        hub.CHECKIN_SHELL_NAME_RE.match(s.name))
    # 老快照没有这两个字段：回落到「当前名既不是壳名也不是自动分工」
    # ——那样的名字只可能是 agent 或用户自己起的
    s.agent_named = bool(d.get("agent_named"))
    s.agent_project = d.get("agent_project") or ""
    s.cursor_uuid = d.get("cursor_uuid")
    s.uuid_verified = bool(d.get("uuid_verified"))
    s.cursor_title = d.get("cursor_title")
    s.transcript_path = d.get("transcript_path")
    s.created_ts = d.get("created_ts")
    s.connected = False
    s.archived = bool(d.get("archived"))
    s.handoff_final = bool(d.get("handoff_final"))
    s.auto_reconnect_blocked = bool(
        d.get("auto_reconnect_blocked")
        or _legacy_snapshot_reconnect_blocked(d))
    s.end_reason = ("用户已关闭（留在「已结束」组待清理）"
                    if s.archived else
                    "换装前已确认离线/挂掉，等待手动接续"
                    if s.auto_reconnect_blocked else
                    "hub 重启前的历史会话")
    s.pending_lost = False
    # 启动重连宽限：先亮橙点「重连中」而非直接黑点「已断开」——活着的 MCP
    # 心跳几秒内会把 tab 复活（server 侧会主动静默续连）；宽限内没等到的
    # 才静默定案为断开，状态从头到尾不失真。
    # 已归档的不给宽限：用户亲手关的，没有「等它接回来」这回事
    s.recon_deadline = (None if s.archived or s.auto_reconnect_blocked
                        else time.time() + 90)
    s.lost_pending_on_drop = False
    s.last_heartbeat = float(d.get("last_heartbeat") or 0)
    s.agent_status = ""
    s.agent_activity = ""
    s.agent_status_ts = 0
    # 轨迹跨重启保留（当前状态清零、轨迹不清）：接手词还原前任进度靠它
    s.zt_trail = [str(x) for x in (d.get("zt_trail") or [])][-5:]
    s.disconnected_at = None
    s.last_reply_probe = None
    s.draft_text = d.get("draft_text") or ""
    s.draft_images = d.get("draft_images") or []
    s.draft_files = d.get("draft_files") or []
    s.library_sent = bool(d.get("library_sent"))
    s.handed_off_to = d.get("handed_off_to") or ""
    tp = d.get("takeover_dispatched")
    s.takeover_dispatched = tp if isinstance(tp, dict) else None
    s.death_alerts = dict(d.get("death_alerts") or {})
    s.death_alerted_bubbles = dict(d.get("death_alerted_bubbles") or {})
    # hub 重启不是换绑：死因牌本来就不落快照（下一拍重查），但 24h 响铃冷却
    # 要跨重启活着，所以这里把「上次查死因用的 uid」对齐成恢复出来的那个，
    # 否则重启后第一拍会被当成换绑，挂了一天的会话每次重启都再响一遍。
    s.death_probe_uid = s.cursor_uuid
    # 模型牌不落快照：Cursor 侧是库里的现时事实，重启后下一拍就重读。
    # Codex 没有那份库，靠 agent 自报的 reported_model 跨重启补牌。
    s.model_info = None
    s.model_probe_at = 0.0
    s.model_probe_uid = None
    s.reported_model = d.get("reported_model") or ""
    s.name_history = list(d.get("name_history") or [])
    s.id_history = list(d.get("id_history") or [])
    s.claimed_task_ts = float(d.get("claimed_task_ts") or 0)
    s.detached = False
    s.detached_since = None
    # 提问随重启清掉（pending 不落盘），用户回过的话留着：孤儿缓存，复活时
    # create_session 转队列补送；接手单的「有回复待收」块也认它
    br = d.get("buffered_reply")
    s.buffered_reply = dict(br) if isinstance(br, dict) else None
    s.wait_deferred = False
    s.messages = list(d.get("messages") or [])
    visible = [m for m in s.messages
               if isinstance(m, dict) and m.get("kind") != "status"]
    visible_machine = sum(1 for m in visible if hub.Api._is_machine_msg(m))
    visible_real = len(visible) - visible_machine

    def _saved_seq(key):
        try:
            return max(0, int(d.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    saved_msg_seq = _saved_seq("msg_seq")
    saved_machine_seq = _saved_seq("machine_seq")
    # 新快照直接带 real_seq，消息数组即使被 max_messages 裁掉也绝不
    # 回退。旧快照没有它时只能从还看得见的消息重建；若证据显示数组
    # 已被裁短，宁可把壳保住也不把可能干过活的对话误删。
    if "real_seq" in d:
        s.real_seq = max(visible_real, _saved_seq("real_seq"))
    else:
        legacy_truncated = bool(d.get("messages_truncated")) \
            or saved_msg_seq > len(visible) \
            or saved_machine_seq > visible_machine \
            or len(s.messages) >= 40
        s.real_seq = max(visible_real, hub_self.REAL_WORK_SEQ + 1) \
            if legacy_truncated else visible_real
    s.machine_seq = max(visible_machine, saved_machine_seq)
    # 老快照只存了 machine_seq 时也维持计数不倒退；新快照则复用原序号。
    s.msg_seq = max(len(visible), saved_msg_seq,
                    s.machine_seq + s.real_seq)
    s.pending = None
    s.processing_since = None
    # 排队消息跨重启保留：agent 重连后第一次 zhi 就能拿到
    s.queued = [q for q in (d.get("queued") or [])
                if isinstance(q, dict) and q.get("id")]
    # JSON 落盘把队列条目里的 msg 和 messages 里的气泡拆成了两个对象，
    # 恢复时按 qid 重新挂钩，送达/撤回改状态才能落到用户看的那个气泡上
    by_qid = {m.get("qid"): m for m in s.messages
              if isinstance(m, dict) and m.get("qid")}
    for q in s.queued:
        linked = by_qid.get(q.get("id"))
        if linked is not None:
            q["msg"] = linked
    # 自愈历史遗留：还标着「排队中」却已不在队列里的气泡 = 其实早就
    # 送达/撤回了，只是修复前的脱钩把标记固化进了快照（07-31 22:48
    # 用户截图里那批）。启动时一次清掉，别让用户以为消息还没发出去
    live_qids = {q.get("id") for q in s.queued}
    for m in s.messages:
        if (isinstance(m, dict) and m.get("queued")
                and m.get("qid") not in live_qids):
            m["queued"] = False
    s.rev = 1
    s.created_at = d.get("created_at") or hub.now_full()
    s.file_path = d.get("file_path")
    s.lock = threading.Lock()
    # 身份随会话保存；恢复的 Codex / ChatGPT 不得继承旧 Cursor 绑定。
    # 日志解析缓存刻意不落盘，恢复后按精确原生任务 ID 重新读取。
    s.runtime_kind = "cursor"
    s.native_thread_id = ""
    hub.runtime_adapter.apply_metadata(s, {
        "runtime_kind": d.get("runtime_kind") or "cursor",
        "native_thread_id": d.get("native_thread_id") or "",
    })
    s.native_resume_ledger = [dict(item) for item in
                              (d.get("native_resume_ledger") or [])
                              if isinstance(item, dict)][-8:]
    s.reported_model = d.get("reported_model") or ""
    return s


# ---------------- 判活融合：agentboard 两路实时信号 ----------------

# agentboard 原始信号缓存：root -> (读取时刻, {"agents":…, "locks":…})。
# 判活融合（方案书第九节）按 15s/1s 的节奏反复问同一份 json，5s TTL 把读盘
# 压到每工作区每 5 秒一次；竞态最多重复读一次小文件，无害，不加锁。
_AGENTBOARD_SIG_CACHE = {}
AGENTBOARD_SIG_TTL = 5.0


def read_agentboard_signals(root):
    """读 <root>/.chijiu-tmp/agentboard.json 的 agents/locks 原始段（带 TTL 缓存）。

    这是判活四路信号里「在写文件」（agents[].lastSeen，钩子每次写类工具都刷）与
    「锁在续期」（locks[].renewed，afterFileEdit 刷）两路的数据源；时间戳都是
    Date.now() 毫秒。文件不存在/坏 JSON 一律回空 dict——信号缺席≠死，判活侧
    自己拿捏。"""
    root = str(root or "")
    if not root:
        return {}
    now = time.time()
    hit = _AGENTBOARD_SIG_CACHE.get(root)
    if hit and now - hit[0] < AGENTBOARD_SIG_TTL:
        return hit[1]
    out = {}
    try:
        bp = Path(root) / ".chijiu-tmp" / "agentboard.json"
        b = json.loads(bp.read_text(encoding="utf-8"))
        out = {"agents": b.get("agents") or {}, "locks": b.get("locks") or {}}
    except Exception:
        out = {}
    _AGENTBOARD_SIG_CACHE[root] = (now, out)
    return out


def board_write_ages(hub_self, s, now):
    """agentboard 看板上属于这个会话的两路实时信号年龄（秒，判活融合用）：
    (最近一次写文件 agents[].lastSeen, 最近一次文件锁续期 locks[].renewed)，
    查不到的路返回 None。

    这是方案书第九节四路信号里最实时的「在写文件」硬信号——08-12 23:28 实案：
    c00f0d0a 活跃写文件，它的 Cursor transcript 却停在 22:25（滞后 63 分钟），
    旧判活只看 transcript 把它判成收工。归属匹配三条路（钩子 agents 键因
    payload 版本而异）：键==conversation_id、键==cursor_uuid、条目.uuid==
    cursor_uuid（transcript uuid）；pid-xxx 兜底键无法归属，忽略。看板按
    「文件所在工作区」落盘，跨工作区接手时人坐在 cwd、活记在 task_root，
    两块板都查、取最新。"""
    conv = str(getattr(s, "conv_key", "") or "")
    uid = str(getattr(s, "cursor_uuid", "") or "")
    if not conv and not uid:
        return None, None
    best_seen = best_renew = 0.0
    roots = []
    for r in (getattr(s, "cwd", ""), getattr(s, "task_root", "")):
        r = hub.norm_root(r)
        if r and r not in roots:
            roots.append(r)
    for root in roots:
        b = hub.read_agentboard_signals(root)
        agents = b.get("agents") or {}
        keys = set()
        for k, a in agents.items():
            if not isinstance(a, dict):
                continue
            if ((conv and k == conv)
                    or (uid and (k == uid or str(a.get("uuid") or "") == uid))):
                keys.add(k)
                best_seen = max(best_seen, float(a.get("lastSeen") or 0))
        if not keys:
            continue
        for lk in (b.get("locks") or {}).values():
            if isinstance(lk, dict) and str(lk.get("owner") or "") in keys:
                best_renew = max(best_renew,
                                 float(lk.get("renewed") or lk.get("since") or 0))
    seen_age = max(0.0, now - best_seen / 1000.0) if best_seen else None
    renew_age = max(0.0, now - best_renew / 1000.0) if best_renew else None
    return seen_age, renew_age


def fusion_recent_activity(hub_self, s, now, within=180):
    """判活信号里最近 within 秒内是否有活跃；命中返回信号名（人话），
    全静默返回 None。判死提醒/失联清理的融合收口共用这一份判据（第九节③）：
    判「死/失联」必须全信号静默，任何一路还热着都轮不到清算。"""
    board_age, lock_age = hub_self.board_write_ages(s, now)
    if board_age is not None and board_age < within:
        return "看板写文件 {:.0f}s 前".format(board_age)
    if lock_age is not None and lock_age < within:
        return "文件锁续期 {:.0f}s 前".format(lock_age)
    for label, ts in (("zt 上报", getattr(s, "agent_status_ts", 0)),
                      ("zhi 到达", getattr(s, "last_zhi_ts", 0))):
        ts = float(ts or 0)
        if ts and now - ts < within:
            return "{} {:.0f}s 前".format(label, now - ts)
    # 第五路（云端/后台 agent 专属）：AI 改档流水。不挂看板钩子、从不调 MCP 的
    # 长跑 agent，本机唯一持续更新的活证就是它（08-28 转世实案）
    try:
        edit_ts = float(hub.agent_last_edit_ts(getattr(s, "cursor_uuid", None)) or 0)
    except Exception:
        edit_ts = 0.0
    if edit_ts and now - edit_ts < within:
        return "AI 改档流水 {:.0f}s 前".format(now - edit_ts)
    return None


# ---------------- 认领判定（身份根治①的判定核心） ----------------

# 「干过真活」的门槛：报到那一两句之下算空壳
REAL_WORK_SEQ = 3


def real_seq(x):
    """这个 tab 真正聊过多少句——机器塞进来的一律不算。

    real_seq 从写入时单调累加并随快照保存，不能再用 msg_seq - machine_seq
    推导：消息数组可能被裁短，后者却是整个会话的累计值。没有 real_seq 的
    内存旧对象才临时走旧算法，保证升级过程不中断。
    """
    value = getattr(x, "real_seq", None)
    if value is not None:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            pass
    try:
        return max(0, int(getattr(x, "msg_seq", 0) or 0)
                   - int(getattr(x, "machine_seq", 0) or 0))
    except (TypeError, ValueError):
        return 0


def did_real_work(hub_self, x):
    return hub_self._real_seq(x) > hub_self.REAL_WORK_SEQ


def claimed_task(hub_self, x):
    """x 是否已「认领了活」——2026-08-12 根治铁律（方案书第二节）的判定核心：

    会话一旦认领了活（收过真实用户任务 / 报过 zt 进度 / 被 agent 改过名 /
    干过真活），就永不被任何自动逻辑收起、归并、改身份、清状态——只能由
    用户显式操作或它自己退出。08-12 实案：c00f0d0a 已被派活开工（改过名、
    报过 zt），但真实消息数还没过 REAL_WORK_SEQ，接手落地把它当「待命空壳」
    归并进 1a49a6ec，tab 消失、消息串台。空壳与开工的分界不是「聊了几句」，
    是「有没有认领过活」。"""
    return (float(getattr(x, "claimed_task_ts", 0) or 0) > 0
            or bool(getattr(x, "agent_named", False))
            or float(getattr(x, "agent_status_ts", 0) or 0) > 0
            or hub_self._did_real_work(x))


def mark_claimed(hub_self, s, why=""):
    """把会话钉成「认领了活」（幂等；随快照持久化，见 Session.claimed_task_ts）。"""
    if float(getattr(s, "claimed_task_ts", 0) or 0) > 0:
        return
    s.claimed_task_ts = time.time()
    if why:
        hub.log_event("会话认领了活 conv={} tab={}（{}）".format(
            getattr(s, "conv_key", "?"), getattr(s, "name", "?"), why))


# 报到壳挂着的 zhi 选项。活着的待命永远卡在这句上；不是 agent 问用户的真问题。
CHECKIN_PENDING_OPTIONS = frozenset({"开始任务", "结束"})
# 这些 zt 状态说明人已经在干自己的活（即便 tab 还叫待命）。Cursor 里直接开干、
# 控制台还挂着报到 zhi 的 c00f0d0a 变体靠它拦住，避免又被当成空壳归并。
_WORKING_ZT = frozenset({
    "developing", "testing", "deploying", "reviewing", "searching", "blocked",
})


def is_checkin_envelope(pending, selected=None, text=""):
    """这次回复是不是在拆报到信封（开始任务/结束，或把接手提示词贴进报到 zhi）。

    不是给这个壳派活。点了之后 pending 清空、旧逻辑会 _mark_claimed，
    接手落地就再也收不掉这个 tab。"""
    if not isinstance(pending, dict):
        return False
    fake = type("P", (), {"pending": pending})()
    if not is_checkin_pending(fake):
        return False
    raw = str(text or "").strip()
    if raw.startswith("你的任务：接手一个此前在rxyy MCP控制台中断的会话"):
        return True
    sel = [str(o).strip() for o in (selected or []) if str(o).strip()]
    if raw in CHECKIN_PENDING_OPTIONS or not raw:
        return (not sel) or set(sel) <= CHECKIN_PENDING_OPTIONS
    return False


def is_checkin_pending(x):
    """挂着的 zhi 是报到那句「已就位 / 开始任务|结束」，不是真提问。

    08-14 实案：rxyy tools 接手落地后待命 tab 关不掉。活着的待命壳永远 pending
    在报到 zhi 上，旧逻辑把「有未答提问」一律当成还在跟人对话，收壳/_is_takeover_shell
    全部让路。真提问（agent 问了用户一件具体的事）仍然要拦住。"""
    pending = getattr(x, "pending", None)
    if not isinstance(pending, dict):
        return False
    opts = [str(o).strip() for o in (pending.get("options") or []) if str(o).strip()]
    if opts and set(opts) <= CHECKIN_PENDING_OPTIONS:
        return True
    return "已就位" in str(pending.get("message") or "")


def is_checkin_shellish(hub_self, x):
    """x 像一个「报到空壳」（宽判）：出生时是待命名、除报到外没说过话。

    认的是【出生名】(shell_born) 而不是当前名字：tab 名有好几条路会被改掉，
    08-07 实测 sync_cursor_titles 把「待命·smart…7」同步成了 Cursor 自动标题
    「Persistent task reporting」，此后 startswith("待命") 恒假 → 接手落地收不掉
    壳，它的 uuid 认领还反过来挡住被接手原 tab 的身份校准。当前名仍兼容判一次：
    修复前的存量快照没有 shell_born 这个字段。

    与 _is_takeover_shell 的分工：那个是「能不能收掉」（还要求无未答提问、
    无排队，收错就丢消息）；这个只回答「它的 uuid 认领可不可以不当排他」——
    空壳的 Cursor 对话按接手设计就是会切给接手方的，哪怕壳上还挂着排队
    消息，也不能拿它的认领去挡真身校准（08-03 错绑实案的另一半根因）。

    「认领了活」的会话不是壳（_claimed_task，2026-08-12 根治）：改过名、
    干过真活、或已经在用 developing 等状态干活的，哪怕真实消息数还没过阈值
    也已经开工——再按壳处理就会重演 c00f0d0a 被归并的事故。

    08-14 开口：还挂着报到 zhi 的待命壳，允许它报过 zt（ready/analyzing 报到
    进度）仍算壳。否则派接手时 target_was_shell 恒假，壳不归并；事后收壳又
    被 pending 拦住，rxyy tools 复活后待命 tab 永远留着。

    08-26 反转 08-14 晚的旧例外（73004179 事故）：当年「点开始任务/把接手词
    贴进报到 zhi」会写 claimed_task_ts，只好规定 claimed_ts 单独出现仍算壳。
    但拆信封两条路早已在上游闸掉（send_reply 的 is_checkin_envelope、
    queue_message 的 TAKEOVER_PROMPT_HEAD），如今 claimed_task_ts 只可能来自
    真实开工信号：真用户回复 / 排队真任务 / zt / 自报真名 / 误关复活。旧例外
    留着的唯一作用就是误伤——08-26 09:32:42，收过真活（08:31「收过真实用户
    回复」）的 73004179 被接手落地按这行判成壳、归并进 b4eff2ee，此后它的每
    句话都串进别人 tab：c00f0d0a（08-12）原地重演。按 08-12 铁律收口：
    认领了活的会话，报到 zhi 一旦不挂着，就绝不是壳。"""
    born_shell = (bool(getattr(x, "shell_born", False))
                  or str(getattr(x, "name", "") or "").startswith("待命"))
    if not born_shell:
        return False
    if bool(getattr(x, "agent_named", False)) or hub_self._did_real_work(x):
        return False
    status = str(getattr(x, "agent_status", "") or "").strip().lower()
    if status in _WORKING_ZT:
        return False
    if hub_self._claimed_task(x) and not is_checkin_pending(x):
        return False
    return True


def is_takeover_shell(hub_self, x):
    """x 是不是一个「待命空壳」——报到成 待命·xxx 后还没派真活的新对话：
    出生时是待命名、没有用户派下来的活、真实消息 ≤3。

    「无排队」这条原先取的是队列字面为空，结果被团队功能自己顶死了：广播、
    黑板提醒、控制台回执都往队列里塞。08-07 实测一条广播同时投给 4 个待命壳
    （9275/5e82/841b/2cea），它们的队列各 5 条——从此永远判不成空壳、永远
    收不掉，团队越热闹死壳越多。而这些都是机器替别人递的话，壳收起时会随
    _reap_shell_into 顺给现任，不是「用户在这个壳里派了活」的证据。
    只有用户自己发的消息才拦（判定与回复位同口径）。

    08-15：AI 挂了之后「用户级规则」会以 who=None + redelivery 补送进队列，
    _may_take_the_reply_slot(None) 为真，四个派接手壳（ed54/2f47/6813/d271）
    因此永远判不成空壳。补送和接手提示词本身都不是「给这个壳派了新活」。

    未答提问：真提问仍拦；报到那句「已就位 / 开始任务|结束」不拦（08-14）。"""
    if not hub_self._is_checkin_shellish(x):
        return False
    if getattr(x, "pending", None) is not None and not is_checkin_pending(x):
        return False
    return not [e for e in (getattr(x, "queued", None) or [])
                if _queued_blocks_takeover_reap(e)]


def looks_like_checkin(message, options):
    """这次 zhi 是不是「报到那一句」——待命壳专用的那张卡。

    判据刻意收得比 is_checkin_pending 紧：那个是在【已知是壳】的前提下认卡，
    这里要在一个【正在干活的 tab】上认，认宽了就会把真话吃掉。只认两种形状：
    ① 选项里有「开始任务」——干活的 tab 不会给用户递一个「开始任务」按钮；
    ② 正文写着「已就位」，且没有别的选项（报到模板固定是「开始任务/结束」或无选项）。

    单独一个「结束」不算：那是干完活收尾的常见形状（"都改完了，验收看这里"），
    认成报到就等于把成果直接吞掉，用户一个字都看不见。
    空 message 是保活续期/重呼，选项没变过，也不算新报到。
    """
    if not str(message or "").strip():
        return False
    opts = [str(o).strip() for o in (options or []) if str(o).strip()]
    if "开始任务" in opts:
        return True
    return "已就位" in str(message or "") and set(opts) <= CHECKIN_PENDING_OPTIONS


# 叫醒之后这么久之内不重新上膛。zt 极便宜，一个懵住的 agent 完全做得出
# 「zt 一声、马上又报一次到」——没有这道下限，闸就成了无限叫醒，用户永远
# 看不见它其实卡住了。
CHECKIN_REARM_MIN_SECS = 60


def _checkin_gate_rearmed(hub_self, s):
    """上一次叫醒之后，它到底有没有真醒过来干活。

    有=这次报到是一次全新的失忆（Cursor 又压缩了一次上下文），该再叫醒一次；
    没有=真叫不醒，放行给用户。判据取「说过真话」和「报过 zt」两路：长任务
    整段只 zt 不 zhi 是常态，只认前者会把它们漏掉。
    """
    if time.time() - float(getattr(s, "checkin_bounced_ts", 0) or 0) \
            < CHECKIN_REARM_MIN_SECS:
        return False
    if hub_self._real_seq(s) > int(getattr(s, "checkin_bounced_seq", 0) or 0):
        return True
    return (float(getattr(s, "agent_status_ts", 0) or 0)
            > float(getattr(s, "checkin_bounced_zt", 0) or 0))


def checkin_takeover_notice(hub_self, s, message, options):
    """报到那句 zhi 落到一个【已经认领过活】的 tab 上时，要当场回给 agent 的话。

    这是接手落地后的常态而不是异常：派接手会把待命壳 ID 别名并进被接手的原
    tab（alias_shell_into），此后接手方带着壳 ID 说的每一句都路由到原 tab。可它
    手上那份报到提示词还在，下一轮（尤其 Cursor 压缩过上下文之后）它会照着模板
    再报一次到——08-25 13:31:35 现场，用户点完接手，等来的是一句「📍 已就位待命，
    点开始任务我就开工」，然后问「你这是什么情况？失忆了还是什么？」。

    挂成一张等用户点的卡是最坏的处理：用户已经点过接手了，让他再点一次「开始
    任务」纯属白等，而 agent 就卡在那儿什么也不干。当场答复它、把原任务名和聊天
    记录路径塞回去，它下一秒就能接着干。

    返回要回给 agent 的正文；不该拦时返回 ""。
    """
    if not looks_like_checkin(message, options):
        return ""
    # 真待命壳照旧挂给用户：那张「开始任务 / 结束」的卡是用户派活的入口，不能没了
    if is_checkin_shellish(hub_self, s) or not hub_self._claimed_task(s):
        return ""
    # 防死锁：叫不醒说明这条路走不通，那就放行让用户自己看见，绝不能因为这道闸
    # 把 agent 卡成谁也够不着（与选项闸同一条纪律）。但「叫不醒」和「一小时后又
    # 失忆一回」不是一件事——后者中间干完了一整段活，Cursor 只是又压缩了一次
    # 上下文，把它打回那条永远留在第一条消息里的报到提示词。两者共用一个一次性
    # 标记，就是 08-26 09:37 那张幽灵卡：接手时用掉的那一次，让压缩后的报到直接
    # 穿过闸门挂给了用户（而它报的还是壳 ID，看上去就像串了台）。
    if getattr(s, "checkin_bounced_ts", 0) and not _checkin_gate_rearmed(hub_self, s):
        return ""
    s.checkin_bounced_ts = time.time()
    s.checkin_bounced_seq = hub_self._real_seq(s)
    s.checkin_bounced_zt = float(getattr(s, "agent_status_ts", 0) or 0)
    name = str(getattr(s, "name", "") or "").strip() or "原任务"
    conv = str(getattr(s, "conv_key", "") or "").strip()
    path = str(getattr(s, "file_path", "") or "").strip()
    lines = [
        "【接手已生效 · 这个 tab 不是待命壳】",
        "你带的这个对话已经接手了「{}」，控制台里它是一个正在干活的 tab。"
        "报到壳那套规矩（点「开始任务」前只调 zhi、不许 Read/Grep）在这儿不适用，"
        "现在就接着干，别再报「已就位」等人点。".format(name),
        "- 原对话 ID：{}（你手上的 ID 已被登记为它的接手别名，照旧用，"
        "控制台自动路由，不用改）".format(conv or "（无）"),
    ]
    if path:
        lines.append("- 聊天记录：{}".format(path))
        lines.append("  先 Read 它，弄清原任务目标、已完成的部分、还剩什么没做，"
                     "别从头重来。")
    lines.append(
        "- 下一次 zhi/zt 的 task_name 请写「{}」这类「项目·功能」两段式，"
        "不要再报「待命·…」——壳名会把这个干活 tab 降级成待命，"
        "下一个接手的人跟着以为自己在待命。".format(name))
    lines.append("把剩下的活干完，再用 zhi 收尾。")
    return "\n".join(lines)


def _queued_blocks_takeover_reap(e):
    """队列里这条算不算「用户又在这个壳里派了活」——是才拦住收壳。"""
    if not isinstance(e, dict):
        return False
    if not hub.Api._may_take_the_reply_slot(e.get("who")):
        return False
    text = str(e.get("text") or "")
    if hub.takeover_prompt_target(text):
        return False
    if e.get("redelivery"):
        return False
    return True


# ---------------- 脱离期回复缓存（buffered_reply） ----------------

MEMORY_DIGEST_MARK = "【会话要点 · 压缩后回灌】"


def split_memory_digest(text):
    """把 _with_session_memory 拼在回复前面的「会话要点」摘要与用户原话拆开。

    摘要各行之间只有单个换行，与正文之间是一个空行——第一个空行就是分界。"""
    text = text or ""
    if not text.startswith(MEMORY_DIGEST_MARK):
        return "", text
    head, _, rest = text.partition("\n\n")
    return head, rest


def merge_buffered_reply(prev, new):
    """两条脱离期回复合成一条，不许后到的把先到的顶掉。

    09-02 17:12 / 17:36 / 17:38 rxyy 对同一条只发不等的提问回了三条（第二、三条
    带图），send_reply 的脱离分支每次都 `buffered_reply = dict(answer)` 整槽覆盖，
    agent 来收时只剩最后那条「空正文 + 1 图」——前两条的正文全丢。

    合并口径：正文按到达顺序拼接；「会话要点」摘要每条回复都会被拼上一份，只留
    最新那份放在最前；选项去重累加；图片 / 文件累加；source 取最新。"""
    if not isinstance(prev, dict):
        return dict(new) if isinstance(new, dict) else None
    if not isinstance(new, dict):
        return dict(prev)
    d_prev, t_prev = split_memory_digest(prev.get("user_input") or "")
    d_new, t_new = split_memory_digest(new.get("user_input") or "")
    digest = d_new or d_prev
    bodies = [t for t in (t_prev.strip(), t_new.strip()) if t]
    body = "\n\n".join(bodies)
    text = (digest + "\n\n" + body) if digest else body
    out = dict(new)
    out["user_input"] = text or None
    sel = [str(x) for x in (prev.get("selected_options") or []) if x]
    for x in new.get("selected_options") or []:
        if x and str(x) not in sel:
            sel.append(str(x))
    out["selected_options"] = sel
    out["images"] = list(prev.get("images") or []) + list(new.get("images") or [])
    out["files"] = list(prev.get("files") or []) + list(new.get("files") or [])
    out["source"] = new.get("source") or prev.get("source")
    return out


# ---------------- detach / 死亡清算路径 ----------------

def detach_watchdog_tick(hub_self, s, now):
    """保活脱离期看门狗（_state_tick 第3段，第四刀批D抽出）：detach 后超宽限仍无
    续期重呼 → 本轮失联（证据：agent 自己停止了续期），清绿灯切待机，避免回复
    送进黑洞。认领了活/融合信号还热着的只挂起不清（身份根治③+第九节③）。"""
    if not (s.connected and s.detached and s.detached_since):
        return
    grace = int(hub_self.cfg.get("detach_grace_secs", 30))
    if now - s.detached_since <= grace:
        return
    # zhi(wait=false) 只发不等：agent 明说了「我先走、回头来收」，脱离是设计不是失联。
    # 不算这一条的话，报到壳刚汇报一句进展就被判「本轮失联」清了绿灯，用户回话打空
    deferred = bool(getattr(s, "wait_deferred", False))
    fusion_sig = (None if (deferred or hub_self._claimed_task(s))
                  else hub_self._fusion_recent_activity(s, now))
    if deferred or hub_self._claimed_task(s) or fusion_sig:
        # ③（2026-08-12 漏读事故根治）：认领了活的会话 detach 超
        # 宽限只挂起、不清 pending/身份。清了 pending，用户稍后的
        # 回复就打在「当前没有等待回复的请求」上悬空；留着它，
        # 回复走 buffered_reply 缓存，agent 续期重呼时原样交付。
        # 失联误判的代价（绿灯多亮一会儿）远小于丢用户的话。
        # 融合收口（第九节③）：没认领过活但四路信号还热着的同样
        # 只挂起——人明明在写文件/刚说过话，只是没来得及续期
        if not getattr(s, "_detach_overdue_logged", False):
            s._detach_overdue_logged = True
            hub.log_event("保活脱离超宽限，只挂起不清 pending conv={} "
                          "tab={}（{}）".format(
                              s.conv_key, s.name,
                              "只发不等 wait=false" if deferred
                              else (fusion_sig or "认领了活")))
    else:
        lost = False
        with s.lock:
            if s.detached and not s.buffered_reply:
                s.detached = False
                s.detached_since = None
                s.pending = None
                s.processing_since = None
                lost = True
        if lost:
            hub_self.add_message(s, {
                "role": "sys", "ts": hub.now_hms(),
                "html": "AI 本轮已失联（超时未续期保活），已切回待机；"
                        "如需继续，请在 IDE 里让 AI 重新发起提问",
            })


def tick_model_probe(hub_self, s, now):
    """这个 tab 用的是哪个模型，缓存在会话上供面板纯读。

    模型是 tab 的「身份牌」而不是状态：用户 08-24 提的是「一排 tab 分不出谁是
    opus 谁是 composer」。Cursor 侧单次是一次单键 sqlite 查询。

    拍频：默认 8 秒（08-26 用户「切档后牌更新慢」——旧值 60 秒，UI 自己都写着
    「一分钟内跟上」）。zt/zhi 到达会清限频立刻重探，因为那通常就是用户刚发
    过话、刚切过档。

    牌跟的是对话，不是 tab：cursor_uuid 换绑后旧牌属于前任对话。这里记住上次
    读牌用的 uid，一见换绑就清牌清限频、本拍立即改读新对话。

    Codex / CLI 没有 composerData：优先用 agent 在 zt/zhi 里报的 model，否则
    读 ~/.codex/config.toml 顶层默认档。以前 uuid 为空直接 return，牌永远空白。
    """
    uid = getattr(s, "cursor_uuid", None)
    if uid != getattr(s, "model_probe_uid", None):
        s.model_probe_uid = uid
        s.model_info = None
        s.model_probe_at = 0.0
    if now - getattr(s, "model_probe_at", 0) < hub_self.MODEL_PROBE_EVERY:
        return
    s.model_probe_at = now
    if uid:
        try:
            info = hub.read_cursor_model(uid)
        except Exception:
            return
        # 读不到就保留上一次的结论：库正被 Cursor 独占写入时会短暂失败，
        # 那一拍把已知的模型抹成空白，面板上的标就会一闪一闪。
        if info:
            s.model_info = info
        return
    reported = str(getattr(s, "reported_model", "") or "").strip()
    if reported:
        info = hub.model_info_from_name(reported)
        if info:
            s.model_info = info
        return
    try:
        info = hub.read_codex_model()
    except Exception:
        return
    if info:
        s.model_info = info


def _lineage_ids(x):
    """这个会话名下的所有对话 ID：现用的 conv_key + 接手时收进来的曾用 ID。"""
    ids = {str(getattr(x, "conv_key", "") or "")}
    ids.update(str(v) for v in (getattr(x, "id_history", None) or []))
    ids.discard("")
    return ids


# 转世换绑的采信窗口：候选对话必须在这段时间内有过动静（AI 改档流水 / 存根刚
# 落盘）。没有这道闸，两个都已凉透的旧存根会被轮流绑上又轮流判死，10 秒一换
# 来回抖；凉透的对话本来也不该被绑——绑上等于把死因从「消失」改成「静默」。
RELOCATE_FRESH_SECS = 2 * 3600
# 已绑着壳时的换壳门槛：挑战者必须比现任新鲜出这么多秒才换。两个都活跃的壳
# 分数会来回互超几秒，没有这道 margin 就是 10 秒一换的抖动。
RELOCATE_UPGRADE_MARGIN = 300.0


def relocate_reincarnated(hub_self, s, now):
    """把会话绑到它在 Cursor 里最新鲜的那个「转世壳」上。

    窗口重载/上下文汇总会让正在跑的对话换一个 composer uuid 接着干，旧 uuid 的
    composerData 被整个回收——判死探针看到的就是「对话已消失」。但转世不改户口：
    新对话的 transcript 存根开头仍是原始第一条用户消息（内含本会话 conversation_id），
    AI 改档流水按新 uuid 记着它此刻还在写文件。两样对上就零打扰换绑，从不调 MCP
    的长跑 agent 也能被找回来（08-28 实案：音视频编辑·env审计·30d6 转世成新对话后
    一直在干活，控制台却顶着「已消失」在待续组躺了 5 小时）。

    三个入口共用这一个函数（08-31 实案补齐后两个）：
    ① gone 判死前最后一搏——经典转世，composerData 被回收；
    ② cursor_uuid 为空——接手/合并出来的会话从没绑过壳，判死探针以前一进门
       就退出，Cursor 三路活性信号永远黑着，agent 一跑长命令就被降成灰点
       「在线·没在输出」（用户看到的就是「明明在干活却显示待机」）；
    ③ 绑着的壳还在库里但早凉了（标题治愈等旁路绑上的旧壳），真身在新壳里
       干活——只有挑战者比现任新鲜出 RELOCATE_UPGRADE_MARGIN 才换，防抖。
    换绑成功返回新 uuid，找不到可信的转世返回 None（调用方原逻辑照旧）。
    """
    try:
        my_ids = _lineage_ids(s)
        if not my_ids:
            return None
        stubs = hub.scan_agent_stub_convs()
        if not stubs:
            return None
        sessions = list(getattr(hub.HUB, "sessions", {}).values()) if hub.HUB else []
        taken = {str(getattr(o, "cursor_uuid", "") or "") for o in sessions}
        taken.discard("")
        edits = hub.agent_edit_ts_map()

        def _score(uid, info):
            return max(float(edits.get(uid, 0) or 0),
                       float((info or {}).get("mtime") or 0))

        cur_uid = str(getattr(s, "cursor_uuid", "") or "")
        # 现任的新鲜度也按同一把尺量；没绑壳 = 0，谁来都算升级
        floor = _score(cur_uid, stubs.get(cur_uid)) + RELOCATE_UPGRADE_MARGIN \
            if cur_uid else 0.0
        best = None
        for uid, info in stubs.items():
            if uid in taken:      # 含现任自己（它在 sessions 里挂着）
                continue
            convs = info.get("convs") or set()
            if not convs & my_ids:
                continue
            # 同一个存根还能对上别的会话 = 证据脏了，宁可不绑
            if any(o is not s and convs & _lineage_ids(o) for o in sessions):
                continue
            score = _score(uid, info)
            if now - score > RELOCATE_FRESH_SECS or score < floor:
                continue
            if best is None or score > best[0]:
                best = (score, uid, info)
        if best is None:
            return None
        _, uid, info = best
        old = cur_uid
        s.cursor_uuid = uid
        # 存根首消息带着本会话的 conversation_id、uuid 又无人认领——证据强度
        # 不低于「到达时刻在生成」，直接给验讫；判死探针据此继续盯新对话
        s.uuid_verified = True
        if info.get("path"):
            s.transcript_path = info["path"]
        s.death_probe_uid = uid
        s.death_info = None
        s.death_probe_at = now
        hub.log_event("对话转世换绑 tab={} conv={}：{} → {}（依据：存根含本会话ID"
                      "，最近动静 {:.0f}s 前）".format(
                          getattr(s, "name", "?"), getattr(s, "conv_key", "?"),
                          old[:8] or "（无）", uid[:8], max(0.0, now - best[0])))
        return uid
    except Exception as e:
        try:
            hub.log_event("转世换绑探测异常（忽略，判死照旧）: {}".format(e))
        except Exception:
            pass
        return None


def tick_death_probe(hub_self, s, now):
    """查这个 tab 对应的 Cursor 对话是不是死在了一条致命报错上。

    单次约 2ms（只读 Cursor 库的最后 3 条气泡），10 秒一拍足够及时；
    结果缓存在 session 上，get_state / 团队面板纯读，不给轮询加 I/O。
    新出现的死亡（气泡 ID 变了）才提醒一次，避免反复响铃。

    死因跟的是对话，不是 tab（与 tick_model_probe 同一条纪律）：cursor_uuid
    换绑或被收走后，挂着的死因属于前任对话——面板上就是「接手方一上来就顶着
    前任的死亡告警」，而 uuid 被收走那种连重查的机会都没有（下面 uid 为空直接
    返回），旧死因会永远钉在那儿。换绑口散在几处还会再长，收在读死因这一处
    自愈，不追着每个赋值点补。
    """
    uid = getattr(s, "cursor_uuid", None)
    if uid != getattr(s, "death_probe_uid", None):
        s.death_probe_uid = uid
        s.death_info = None
        s.death_probe_at = 0.0
    if now - getattr(s, "death_probe_at", 0) < hub_self.DEATH_PROBE_EVERY:
        return
    s.death_probe_at = now
    if not uid:
        # 空绑定不是免检金牌（换绑入口②）：接手/合并出来的会话从没绑过壳，
        # 以前这里直接 return、换绑又只挂在 gone 分支——这类会话的 Cursor
        # 三路信号永远黑着，agent 一跑长命令就被降成灰点（08-31 实案：
        # 任务重开会话正跑全量回归，侧栏点却是「待机」灰）。
        relocate_reincarnated(hub_self, s, now)
        return
    try:
        err = hub.read_cursor_error(uid)
    except Exception:
        return
    dead = err if (err and err.get("is_last")) else None
    if dead is None and getattr(s, "uuid_verified", False):
        # 无声死法：对话被删/被 Cursor 政策回收，composerData 键整个没了，
        # 一条报错都不留（07-31 用户实测「这个没了你都不知道」）。
        # 只对验过身份的 uid 判——猜来的 uid 查不到不能算数
        try:
            if hub.cursor_conversation_exists(uid) is False:
                dead = {"code": "gone", "bubble_id": "gone:" + str(uid),
                        "reason": "对话已从 Cursor 里消失（被删或被政策回收）",
                        "advice": "这种死法无法原地复活：右键这个 tab「🤝 接手」，"
                                  "把聊天记录交给别的 agent 接着干。",
                        "at_ts": now}
        except Exception:
            pass
    if dead:
        # 报错之后 agent 又跟控制台说过话（阻塞在 zhi / 上报过 zt / 正等它回话 /
        # 调过 zhi）= 它其实缓过来了。这道闸放在这里，tab 列表与团队面板才不会
        # 各说各话
        seen = max(getattr(s, "agent_status_ts", 0) or 0,
                   getattr(s, "processing_since", None) or 0,
                   (s.pending or {}).get("created", 0) or 0,
                   float(getattr(s, "last_zhi_ts", 0) or 0))
        at = dead.get("at_ts") or 0
        if s.pending is not None or (at and seen > at + 5):
            dead = None
        else:
            sig = hub_self._fusion_recent_activity(s, now)
            if sig:
                # 判死走融合（第九节③）：四路信号任一新鲜 = 人还活着，读到的
                # 报错只可能是历史残影或 uuid 错配——不落死因、不推「挂了」。
                # 真死的 agent 刷不动这些信号，最多晚 3 分钟照样判死
                hub.log_event("判死让路 tab={}：{}，忽略报错气泡（{}）".format(
                    s.name, sig, dead.get("reason")))
                dead = None
    if dead and str(dead.get("code") or "") == "gone":
        # 「消失」死法先查转世：uuid 被回收但对话换壳还活着的话，换绑即复活，
        # 不落死因（08-28 窗口重载实案；换绑函数自己会清死因、对齐限频戳）
        if relocate_reincarnated(hub_self, s, now):
            return
    if dead:
        # 死因分级（死会话处置①）：气泡/推送/回执都要能回答「下一步干什么」。
        # gone 在构造处已带 advice（不可复活只能接手），别的死法在这里统一补
        revivable, advice = classify_death(dead)
        dead["revivable"] = revivable
        if advice and not dead.get("advice"):
            dead["advice"] = advice
    else:
        # 换绑入口③：壳没死不代表绑得对。标题治愈等旁路会把接手会话绑到
        # 转世前的旧壳上——composerData 还躺在库里（治愈刚写过标题），判死
        # 探不出 gone，可信号全是死水，真身在新壳里干活。升级扫描吃的都是
        # 60s/20s 缓存的存根与流水字典，margin 闸挡抖动。
        relocate_reincarnated(hub_self, s, now)
    prev = getattr(s, "death_info", None)
    s.death_info = dead
    if dead and (not prev or prev.get("bubble_id") != dead.get("bubble_id")):
        gate = bool(dead.get("gate"))
        hub.log_event("{} tab={} 原因={} code={}".format(
            "会话撞并发闸（按排队处理）" if gate else "会话死于报错",
            s.name, dead.get("reason"), dead.get("code")))
        # 通道还连着、agent 却被 Cursor 杀了：刚发出去的回复此刻正躺在死请求里，
        # 救回队列并把气泡标「未送达」，否则人和 AI 会各等一小时（08-03 同事机实测）。
        # 并发闸时缺省文案「AI 已经挂了」是撒谎——它在门口排队，进来就自动补送
        if gate and dead.get("gate_kind") == "gateway_text":
            gate_note = ("⏳ 你上一条回复暂时没人接：AI 网关并发满把这条请求拒了，"
                         "它不会自动重试。该回复已转入队列；腾出名额后回这个对话"
                         "重发一句，AI 一进来就自动补送。")
        elif gate:
            gate_note = ("⏳ 你上一条回复暂时没人接：Cursor 并发满/限频，AI 在排队等"
                         "空位。该回复已转入队列，它一进来就自动补送，不用重发。")
        else:
            gate_note = ""
        hub_self._rescue_swallowed_reply(
            s, why=dead.get("reason") or "报错中断", note=gate_note)
        # 同一死因 24h 内只响一次铃/推一次手机：欠费、额度这类死法，僵尸窗口
        # 每被重连或重新生成戳一下就出一个新错误气泡（新 bubble_id），提醒就
        # 再来一轮——挂了一天的会话还在反复弹（08-03 用户实测点名）。冷却期内
        # 死亡标记照常更新（tab 角标、面板可见），只是不再吵人。
        # 冷却按「对话+死因」记而不是只按死因：同一个 tab 换绑到新对话后，
        # 新 agent 真的欠费挂了却因为前任 24h 内挂过同一种死法而不响铃，
        # 那是把「不吵人」办成了「漏报」。带上 uid 后换绑天然重新计时，
        # uuid 在两个对话间来回抖也不会把已响过的那条又响一遍。
        code = str(dead.get("code") or "?")
        akey = "{}:{}".format(uid, code)
        alerts = getattr(s, "death_alerts", None) or {}
        now_ts = time.time()
        # 同一个气泡 = 同一次死亡。死因牌不落盘，hub 一重启这里就把老死亡当
        # 「新出现」再走一遍；24h 冷却只挡得住当天，次日开机那次重启就是
        # 几十个僵尸会话齐刷刷再推一轮手机（09-02 家机 46 条 / 公司机 26 条）。
        # 处理过（响过铃、或被冷却压掉）的气泡记在快照里，跨重启永不二次提醒；
        # 真正的新气泡照旧走 24h 冷却
        # 键带上对话 uid：气泡属于对话，换绑后新对话哪怕撞了同一个气泡号也算新死亡
        bubble = ("{}:{}".format(uid, dead.get("bubble_id"))
                  if dead.get("bubble_id") else "")
        seen = getattr(s, "death_alerted_bubbles", None) or {}
        if bubble and bubble in seen:
            hub.log_event("同一死亡气泡已处理过（多半是重启重查），跳过判死提醒 "
                          "tab={} code={}".format(s.name, code))
        else:
            if bubble:
                # 只留一周：僵尸会话归档前气泡一般不再变，条目不会攒多
                seen = {k: v for k, v in seen.items()
                        if now_ts - float(v or 0) <= 7 * 24 * 3600}
                seen[bubble] = now_ts
                s.death_alerted_bubbles = seen
            if now_ts - float(alerts.get(akey, 0) or 0) > 24 * 3600:
                # 顺手扫掉过期条目：换绑多了键会越攒越多，而超过冷却期的条目
                # 留着也不再影响任何判断
                alerts = {k: v for k, v in alerts.items()
                          if now_ts - float(v or 0) <= 24 * 3600}
                alerts[akey] = now_ts
                s.death_alerts = alerts
                hub_self.alert_session_death(s, dead)
            else:
                hub.log_event("同死因冷却中，跳过判死提醒 tab={} code={}".format(
                    s.name, code))
        # 工作流联动：死的是某条链当前步的执行人 → 立即标卡+提醒，
        # 别让链干等 45 分钟超时才发现（欠费挂掉一天六次是常态）。
        # 并发闸不算：标卡会引来「重派（可换人）」，重派=再开会话=给闸口加压
        try:
            if hub.WORKFLOW is not None and s.conv_key and not gate:
                wf_note = hub.WORKFLOW.on_executor_dead(
                    s.conv_key, dead.get("reason") or "报错中断")
                if wf_note:
                    hub.log_event(wf_note)
        except Exception as e:
            hub.log_event("工作流死亡联动异常: {}".format(e))
        # 任务面板联动：人没了，他名下的卡别占着坑，退回去让别人接。
        # 只挂在这个「新出现的死亡」分支上——重连宽限、清空壳那几处天天误伤，
        # 把只是抄线重连的 agent 的卡退掉比不退更坏
        try:
            if s.conv_key and not _is_transient_death(dead):
                import board_hooks
                board_hooks.release_session(
                    s.conv_key, "会话判死：" + (dead.get("reason") or "报错中断"))
        except Exception as e:
            hub.log_event("看板钩子（判死）异常: {}".format(e))


# 网络抖一下就退卡是误伤：provider 不可达、连接中断这类几十秒就自己缓过来，
# 而 agent 缓过来时卡已经不在它名下，它接着交付会被顶回来（08-13 实测，本人中招）。
# 退卡只留给回不来的死法：对话被删、额度耗尽、欠费那种。
_TRANSIENT_DEATH_MARKS = (
    "unable to reach", "connection", "连接中断", "network", "timeout", "超时",
    "temporarily", "稍后再试",
)


def _is_transient_death(dead):
    """网络/无名中断：面板可记一笔，手机别跟着抖。

    08-26 快编 tab：对话被总结后 Cursor 留下 code=None 的「报错中断」气泡，
    判死仍 push_phone；Bark 上就像视频编辑项目反复报挂。欠费/额度仍要响。
    """
    dead = dead or {}
    if dead.get("gate"):
        # 并发闸（会话上限/限频）：空位一出自动续，比网络瞬断还「瞬」——
        # 退卡必误伤（08-27 心理健康那张卡就是这么被「会话判死：报错中断」退回的）
        return True
    reason = str(dead.get("reason") or "").lower()
    if any(mark in reason for mark in _TRANSIENT_DEATH_MARKS):
        return True
    code = dead.get("code")
    if code in (None, "", "?") and reason in ("", "报错中断"):
        return True
    return False


# 死因分级规则（死会话处置①，08-13 一天 4 起实证）：匹配子串 → 能否原地复活 +
# 下一步人话。模型切换/安全过滤器（当天 2 起）原窗口直接续；额度类充值后可续
_REVIVE_RULES = (
    (("safety filter", "switched to", "error_custom_message",
      "start a new conversation"), True,
     "模型被安全过滤器/故障自动切换，上下文还在：回原 Cursor 窗口直接继续"
     "（或换回想用的模型重试）；反复触发就右键 tab「🤝 接手」换壳续跑"),
    (("欠费", "额度", "quota", "payment", "insufficient", "billing"), True,
     "额度/账单类：充值或换号后回原窗口点重试可原地续跑；"
     "不处理就右键 tab「🤝 接手」换壳接着干"),
)


def classify_death(dead):
    """死因分级（死会话处置①）：这个死法能不能原地复活 + 下一步该干什么的人话。

    返回 (revivable, advice)。gone（对话被删/回收）是唯一回不去的死法，advice
    沿用构造处那句「只能接手」；其余死法 Cursor 原窗口的上下文都还在，点重试/
    回一句就能原地续——用户不知道这一点时，只能干等或整壳重来（08-13 四起全靠
    人工盘点才发现，本方案的起点）。
    """
    if not dead:
        return True, ""
    if dead.get("code") == "gone":
        return False, str(dead.get("advice") or "")
    if dead.get("gate"):
        if dead.get("gate_kind") == "gateway_text":
            # AI 网关（会员口令）的并发闸：一条正文拒稿后对话就 completed 了，
            # **没有任何东西会自动重试**——与 Cursor 自家闸口「空位自动续」不同，
            # 这里必须让人动手：关掉别的在跑窗口，回这个对话重发一句
            return True, ("AI 网关把这条请求拒了：{}。它不会自动重试——先关掉/结束"
                          "别的正在跑的 Cursor 对话腾出名额，再回这个对话重发一句"
                          "（或对没开起来的待命壳重开一个）；别再加派，名额就那几个"
                          .format(str(dead.get("detail") or "并发窗口已满").strip()[:80]))
        # 必须排在 _REVIVE_RULES 之前：并发闸的 code 也是 ERROR_CUSTOM_MESSAGE，
        # 会被第一条「安全过滤器」规则劫走，给出「回窗口重试/接手」——接手
        # 就是再开会话，正是把 11:42 事故越搅越堵的那个动作
        return True, ("平台并发闸拒了新请求，不是这个 agent 坏了：空位一出"
                      "自动重试，排队的消息也会自动送达。急就先收掉一个在跑的"
                      " tab；别再加派新会话，越派越堵")
    blob = " ".join(str(dead.get(k) or "")
                    for k in ("code", "reason", "title", "detail")).lower()
    for marks, revivable, advice in _REVIVE_RULES:
        if any(m in blob for m in marks):
            return revivable, advice
    if _is_transient_death(dead):
        return True, ("网络类瞬断，多半几十秒自己缓过来；"
                      "几分钟没动静就回原窗口点重试")
    return True, ("回原 Cursor 窗口点重试/回一句即可原地续跑；"
                  "反复失败就右键 tab「🤝 接手」交给新壳接着干")


# ---------------- 判活融合：多信号裁决 ----------------

def session_idle_secs(s, now):
    """侧栏「刚刚 / N 分钟前」：看这个对话自己多久没动静，不看 MCP 进程心跳。

    mcp_heartbeat 每 5s 把 30 分钟窗口内的所有 conversation_id 刷一遍
    last_heartbeat。用它当 idle_secs，还在名单里的 tab 会永远显示「刚刚」
    （09-07：干活中的 tab 自报「4 分钟前」，牌上却写刚刚）。
    """
    live = getattr(s, "live_cache", None) or {}
    ages = []
    for key in ("ide_age", "write_age", "zt_age", "proc_age"):
        v = live.get(key)
        if v is not None:
            try:
                ages.append(int(v))
            except (TypeError, ValueError):
                pass
    seen_ts = max(
        float(getattr(s, "last_zhi_ts", 0) or 0),
        float(getattr(s, "agent_status_ts", 0) or 0),
        float(getattr(s, "processing_since", None) or 0),
        float((getattr(s, "pending", None) or {}).get("created", 0) or 0),
    )
    if seen_ts:
        ages.append(int(max(0.0, now - seen_ts)))
    if ages:
        return min(ages)
    if hub.runtime_adapter.is_native(s):
        return None
    hb = getattr(s, "last_heartbeat", 0) or 0
    return int(now - hb) if hb else None


def agent_liveness(api_self, s, now):
    """这个 agent 到底是活着、在干活、卡住了还是没了——并附上判据。

    只看 MCP 通道会把「收工的 agent」当成在线：agent 一轮结束后 Cursor 仍把
    MCP 进程留着，socket 一直连着，看板就一直显示「在线待机」。
    判活走多信号融合（方案书第九节，2026-08-13）：四路信号——看板写文件
    （agentboard lastSeen）/ Cursor 实时 generating（直读状态库）/ zhi·zt 到达 /
    文件锁续期——任一活跃即判活；判「死/收工/卡住」必须全信号静默 + 超时阈值。
    transcript 文件 mtime 降级为辅助：它新鲜仍可佐证在跑，但它停滞不再单独
    支撑任何死亡/收工结论（08-12 23:28 实案：c00f0d0a 活跃写文件，transcript
    停在 22:25 滞后 63 分钟，旧判定把它判成收工）。
    """
    import ext_bus
    window_seen_ts = 0.0

    def probe_cursor_window_live(session, at):
        """返回绑定 Cursor 窗口的总线判定：True 在线、False 已离线、None 不适用。"""
        # MCP 心跳属于共享进程证据，不能证明某个 Cursor 窗口还开着；扩展登记的
        # instance 心跳才是窗口级证据。runtime_kind 为空是旧快照，按 Cursor 兼容；
        # 显式的 Codex / ChatGPT / unknown 不经过这条 Cursor 窗口判断。
        nonlocal window_seen_ts
        runtime_kind = str(getattr(session, "runtime_kind", "") or "").strip().lower()
        if runtime_kind and runtime_kind != "cursor":
            return None
        instance_id = str(getattr(session, "ext_instance", "") or "").strip()
        if not instance_id:
            return None
        try:
            registered = ext_bus.read_instances()
            row = next((row for row in registered if isinstance(row, dict)
                        and str(row.get("id") or "").strip() == instance_id), None)
            if row is None:
                return None
            window_seen_ts = float(row.get("updatedAt") or 0) / 1000
            if window_seen_ts <= 0:
                return None
            live = ext_bus.live_instances(now_ms=at * 1000)
            return any(str(row.get("id") or "").strip() == instance_id
                       for row in live if isinstance(row, dict))
        except Exception:
            # 总线读失败不是可验证的窗口离线证据，保留原有判活路径。
            return None

    hb = getattr(s, "last_heartbeat", 0) or 0
    hb_age = (now - hb) if hb else None
    zt_ts = getattr(s, "agent_status_ts", 0) or 0
    zt_age = (now - zt_ts) if zt_ts else None
    # Cursor 自己记录的对话活动：generating 来自持久化 composerData，异常退出后
    # 可能长期残留，必须和同一记录的更新时间一起判断，不能当无时限硬信号。
    # transcript 文件时间只是它的影子，落后且会因错配而失真。
    act = hub.read_cursor_activity(getattr(s, "cursor_uuid", None)) or {}
    act_age = (now - act["updated_ts"]) if act.get("updated_ts") else None
    generating_recorded = bool(act.get("generating"))
    generating = bool(generating_recorded and act_age is not None
                      and 0 <= act_age < 180)
    stale_generating = bool(generating_recorded and not generating)
    ide_age = act_age
    tp = getattr(s, "transcript_path", None)
    if tp:
        try:
            file_age = now - os.stat(tp).st_mtime
            ide_age = file_age if ide_age is None else min(ide_age, file_age)
        except OSError:
            pass
    # 第五路（云端/后台 agent 专属）：AI 改档流水。转世对话的 composerData 会被
    # 回收、transcript 存根冻结在首消息，本机唯一持续更新的活证就是这本流水——
    # 不折进 ide_age，从不调 MCP 的长跑 agent 在断开侧必被判成「已终止」
    # （08-28 音视频编辑·env审计·30d6 实案）
    edit_age = None
    try:
        edit_ts = float(hub.agent_last_edit_ts(getattr(s, "cursor_uuid", None)) or 0)
    except Exception:
        edit_ts = 0.0
    if edit_ts:
        edit_age = max(0.0, now - edit_ts)
        ide_age = edit_age if ide_age is None else min(ide_age, edit_age)
    # 四路信号里最实时的「在写文件」：看板 lastSeen / 文件锁 renewed
    board_age, lock_age = hub.HUB.board_write_ages(s, now)
    write_age = min(x for x in (board_age, lock_age) if x is not None) \
        if (board_age is not None or lock_age is not None) else None
    proc_age = (now - s.processing_since) if getattr(s, "processing_since", None) else None
    recon = bool(not s.connected and s.recon_deadline and now <= s.recon_deadline)
    # 与控制台的最近一次真实交互（提问 / 上报 / 收到回复 / 本对话上一次调 zhi），
    # transcript 定位不到时它是唯一还能说明「这个 agent 多久没动静」的证据
    seen_ts = max(zt_ts, getattr(s, "processing_since", None) or 0,
                  (s.pending or {}).get("created", 0) or 0,
                  float(getattr(s, "last_zhi_ts", 0) or 0))
    seen_age = (now - seen_ts) if seen_ts else None
    zhi_ts = float(getattr(s, "last_zhi_ts", 0) or 0)
    zhi_age = (now - zhi_ts) if zhi_ts else None

    # 共享 MCP 进程可以在 Cursor 窗口关闭后继续报心跳。绑定窗口已离线时，
    # 旧 pending / generating 不再能把会话伪装成在线；但刚到达的真实 zhi/zt、
    # 看板写入和后台改档仍是独立活证，不能被窗口总线抢掉。
    cursor_window_live = probe_cursor_window_live(s, now)
    cursor_window_offline = cursor_window_live is False
    # 窗口过期前的 zhi/zt 也可能冻结，不能再给它额外延长 10–15 分钟绿灯。
    # 只有过期之后产生的独立证据才能说明后台任务仍在运行。
    window_expired_at = window_seen_ts + ext_bus.INSTANCE_FRESH_MS / 1000
    fresh_activity = any(
        age is not None and 0 <= age < limit and now - age > window_expired_at
        for age, limit in ((zhi_age, 900), (zt_age, 600), (write_age, 180), (edit_age, 180))
    )

    ev = []
    if s.connected:
        ev.append("通道在线")
    if hb_age is not None and s.connected:
        ev.append("MCP 心跳 " + api_self._age_text(hb_age))
    if generating:
        ev.append("Cursor 正在生成")
    elif stale_generating:
        if act_age is None:
            ev.append("Cursor 生成标记陈旧（缺少更新时间）")
        else:
            ev.append("Cursor 生成标记陈旧 " + api_self._age_text(max(0, act_age)))
    if board_age is not None:
        ev.append("看板写文件 " + api_self._age_text(board_age))
    if lock_age is not None:
        ev.append("文件锁续期 " + api_self._age_text(lock_age))
    if edit_age is not None:
        ev.append("AI 改档 " + api_self._age_text(edit_age))
    if ide_age is not None:
        ev.append("Cursor 输出 " + api_self._age_text(ide_age))
    else:
        ev.append("没定位到 Cursor 对话文件")
    if act.get("subtitle"):
        ev.append("最近动作：" + act["subtitle"][:40])
    if zt_age is not None and getattr(s, "agent_status", ""):
        ev.append("自报「{}」{}".format(s.agent_status, api_self._age_text(zt_age)))
    if proc_age is not None:
        ev.append("等它回话 " + api_self._age_text(proc_age))
    if seen_age is not None:
        ev.append("上次交互 " + api_self._age_text(seen_age))

    dead = getattr(s, "death_info", None)
    if dead:
        ev.append("Cursor 报错：{}".format(dead.get("title") or dead.get("reason")))

    # 控制台状态分级（第九节④）：活跃（哪路信号）/静默（多久）/疑似失联
    # （判据）/死亡（原因）。state 细分给圆点配色，tier 给面板分级归组。
    TIERS = {"waiting": "active", "working": "active", "online": "active",
             "recent": "active",
             "idle": "quiet", "turn_done": "quiet", "unknown": "quiet",
             "stalled": "suspect", "lost": "suspect", "recon": "suspect",
             "ide": "suspect",
             "died": "dead", "dead": "dead"}

    # 只发不等（wait=false）的提问挂着时，真实判活结论后面带的一截说明
    deferred_note = ""

    def out(state, label, sure):
        if deferred_note:
            label = label + " · " + deferred_note
        return {"state": state, "label": label, "sure": sure, "evidence": ev,
                "tier": TIERS.get(state, "quiet"),
                # Cursor 正在生成（composerData.generatingBubbleIds 非空）：实时时间线
                # 据此决定要不要快拍 get_live_turn
                "generating": bool(generating),
                "hb_age": None if hb_age is None else int(hb_age),
                "ide_age": None if ide_age is None else int(ide_age),
                "zt_age": None if zt_age is None else int(zt_age),
                "write_age": None if write_age is None else int(write_age),
                "proc_age": None if proc_age is None else int(proc_age),
                 "death": dead if state == "died" else None}

    if cursor_window_offline:
        ev.append("Cursor 窗口已关闭（扩展心跳超过 60 秒）")
        if not fresh_activity:
            generating = False
            return out("idle", "Cursor 窗口已关闭（扩展心跳超过 60 秒）", True)

    if s.pending is not None:
        # 绿灯「等你回复」只给真在等人的提问。zhi(wait=false) 只发不等的提问也挂
        # pending（提问与回复位要保留到 agent 来收），但 agent 明说了它没在等：
        # ① 用户已经回过（buffered_reply 存着）→ 再亮绿灯就是「我回了它还说等我」
        #    （09-02 17:36 rxyy 实测：回了三条，tab 绿了 40 分钟）；
        # ② 纯进展汇报（无选项无卡片）→ 没有要用户答的东西。
        # 这两种落到下面的真实判活（干活/卡住/已挂），label 带一截说明；
        # 带选项/卡片且还没人答的仍是绿灯——那确实等着用户拍板。
        pend = s.pending or {}
        deferred = bool(getattr(s, "wait_deferred", False))
        replied = getattr(s, "buffered_reply", None) is not None
        asks = bool(pend.get("options") or pend.get("card"))
        if not deferred:
            return out("waiting", "等你回复", True)
        if replied:
            ev.append("你已回复，回复存着等它来取（AI 只发不等）")
            deferred_note = "已回复·等 AI 来取"
        elif asks:
            return out("waiting", "等你回复（AI 只发不等，回复先存着）", True)
        else:
            ev.append("AI 只发不等的进展汇报（可回复，先存着）")
            deferred_note = "有进展可回复"
    if generating:
        return out("working", "干活中（Cursor 正在生成）", True)
    # generating=0 且 status=aborted 是工具阶段常态（08-15 现网所有干活 tab
    # 都这样），不是死。下面用 composer 刷新 / 看板 / zt 接着判。
    # 融合判活（第九节）：看板 lastSeen / 锁续期是「正在调写类工具」的实时
    # 硬证据——钩子只在真实工具调用时刷新，Cursor 写报错气泡不会触发它，
    # 所以可以放在 died 之前：真死的 agent 不可能再刷这两路信号，还新鲜就
    # 说明报错是历史残影或 uuid 错配（08-12 23:28 c00f0d0a 实案的主修复）
    if write_age is not None and write_age < 180:
        src = "看板写文件" if write_age == board_age else "文件锁续期"
        return out("working", "干活中（{} {}）".format(
            src, api_self._age_text(write_age)), True)
    # 死于致命报错：必须排在「transcript 刚动过」之前——写这条报错本身就会
    # 更新对话时间，否则刚咽气的 tab 还会被判成「干活中」整整三分钟
    if dead:
        at = dead.get("at_ts") or 0
        # 报错之后 agent 还跟控制台说过话 = 它其实缓过来了，别误判死亡
        if not (at and seen_ts and seen_ts > at + 5):
            if dead.get("gate"):
                if dead.get("gate_kind") == "gateway_text":
                    # 网关拒稿不会自动重试：不能标「空位自动续」哄人，橙闪但说清要人动手
                    return out("recon", "并发满·AI 网关拒了（不自动重试，腾出名额后重发）", True)
                # 并发闸=在门口排队，不是死（recon 橙闪：会自己回来的语义）。
                # 报成「已挂」用户就去点接手，再开会话把闸口越搅越堵
                return out("recon", "排队中·Cursor 并发满/限频（空位自动续）", True)
            return out("died", "已挂·" + (dead.get("reason") or "报错中断"), True)
    # 直接探测（不用问 agent）：对话流水最后一个事件是 turn_ended = 这一轮
    # 真的收工了，它不会再自己说话——等派活/用户在 IDE 里发话才会醒。
    # 不标出来的话，这种 tab 顶着「在线·待机」和随时会动的干活 tab 长得一样。
    # 三道闸防误判（07-31 实测两连翻车后收紧的）：
    # ① 验明正身：transcript 有一条「就近猜」的定位兜底，猜来的可能是别人的
    #    收工文件（cursor工作流1 被隔壁死会话的 turn_ended 判成「已中断」）。
    #    agent 调 zhi 时 conversation_id 必进工具参数随流水落盘——文件里
    #    找得到本对话 ID 才认账；
    # ② 近 10 分钟跟控制台有过真实交互（zhi/zt/回复）的一律不判，它明摆着活着
    #    （AICodebrain集成 刚 zhi 完就被判中断的教训）；没跟本次 hub 说过一句话
    #    的也不判——重启后 last_zhi_ts 归零，那是失忆不是沉默（同一个 tab 又中了
    #    一次的教训）；
    # ③ Cursor 库说正在生成的不判（generating 分支在上面已拦）。
    if (s.connected and s.pending is None
            and not getattr(s, "processing_since", None)
            and seen_age is not None and seen_age > 600
            and (write_age is None or write_age > 600)):
        # write_age 闸（融合）：还在写文件的 agent 显然没收工——流水尾部的
        # turn_ended 多半是错配到了别人的旧文件
        tp = getattr(s, "transcript_path", None)
        tstate, tstatus = hub.read_turn_state(tp)
        if tstate == "ended" and hub.transcript_mentions(tp, s.conv_key):
            ev.append("流水尾部 turn_ended({})，流水含本对话ID".format(tstatus or "?"))
            if tstatus == "error":
                return out("turn_done", "本轮已中断（报错结束）·派活或去 IDE 喊它", True)
            return out("turn_done", "本轮已结束·不会再自己说话（派活即醒）", True)
    # 180s 而不是几十秒：agent 跑一条长命令（打包、测试）几分钟没有输出是常态，
    # 卡太紧会把埋头干活的 tab 判成收工。
    # generating=0 时这条就是工具阶段（08-15 现网：status=aborted、checkpoint
    # 仍在刷）。文案写明，避免跟「Cursor 正在生成」或「已挂」混淆。
    if s.connected and ide_age is not None and ide_age < 180:
        return out("working", "干活中（工具阶段）", True)
    # composerData 会持久化，陈旧的 generating=True 只能说明过去曾在生成。没有
    # 看板/锁、后台改档或 zhi·zt 这类近期独立证据时，不能永久亮绿灯，也不能仅凭
    # transcript/记录 mtime 陈旧就反推已死；明确降为 unknown，交给后续新证据纠正。
    recent_independent = any(
        age is not None and 0 <= age < limit
        for age, limit in ((write_age, 900), (edit_age, 900),
                           (zt_age, 600), (zhi_age, 900))
    )
    if stale_generating and not recent_independent:
        return out("unknown", "生成标记已陈旧·当前状态不确定", False)
    if s.connected and proc_age is not None:
        # 已经把话交给它了：全信号长时间静默 = 多半卡住/被回收，别再显示「干活中」。
        # 但刚交出去一两分钟就喊卡住是误报——agent 起步、读文件本来就没输出；
        # 埋头写文件（write_age 新鲜）的更不算卡（融合：判「卡」也要全信号静默）。
        #
        # zt 也是本函数开头列的那四路信号之一，这条判定当初却只写了 ide 和 write
        # 两路。Cursor 侧 ide_age 常年新鲜，漏掉的这一路一直没露头；换成不是
        # Cursor 的客户端就必现——Codex / CLI / ACP 既没有 Cursor 状态库可读
        # （cursor_uuid 为空、也没有 transcript 文件），也不挂 agentboard 钩子，
        # ide_age 与 write_age 永远是 None。于是用户在控制台回一句话之后满 3 分钟，
        # 那个 tab 必定翻成「疑似卡住」，哪怕 agent 上一秒才报过进度。08-25 实测：
        # Codex 窗口里正跑着「第 3/5 步 · 8 个文件已更改」，控制台这边灰着。
        alive_age = min([x for x in (ide_age, write_age, zt_age) if x is not None],
                        default=None)
        if proc_age > 180 and (alive_age is None or alive_age > 300):
            return out("stalled", "疑似卡住", False)
        return out("working", "干活中", alive_age is not None and alive_age < 300)
    # zt 自报的采信窗口给到 10 分钟：agent 是「每完成一个动作报一次」，跑一条
    # 长命令（打包/测试）期间不报是常态。原来只给 180s，实测正好错过——
    # 3 分钟前刚自报「developing」的 tab 被退档成「刚才还在动（14分钟前）」，
    # 面板上像收工了。transcript 与 zt 谁新听谁。
    if s.connected and zt_age is not None and zt_age < 600 and getattr(s, "agent_status", ""):
        if ide_age is None or zt_age <= ide_age:
            return out("working", "干活中（自报 {}）".format(
                api_self._age_text(zt_age)), zt_age < 180)
    if s.connected:
        # 没定位到 Cursor 对话文件、看板上也没它的写文件记录时，我们其实什么都
        # 不知道：MCP 进程是按窗口共享的，通道在线证明不了这个对话还在跑。
        # 别把「不知道」说成「已收工」（write_age 有值时交给下面的融合静默判定）
        if ide_age is None and write_age is None:
            # 第四条证据：它正占着本工作区的文件锁 = 确实在改代码
            owner = api_self.unclaimed_lock_owner(s)
            if owner:
                if not getattr(s, "cursor_uuid", None):
                    s.cursor_uuid = owner  # 顺手认下身份，文件占用列表也能对上了
                ev.append("占着文件锁（看板 owner {}）".format(owner))
                return out("working", "干活中（在改文件）", True)
            # MCP 进程心跳每 5s 上报 30 分钟窗口内的全部 conversation_id，
            # 不能当「这个对话还活着」：收工的对话会在窗口内一直被刷成在线
            # （09-07）。Bajie 口径是 200s 没碰工具就离线；我们对齐对话自己
            # 的交互（zhi/zt/提问），没有 Cursor 文件时也按这个时间窗走。
            if seen_age is not None and seen_age < 200:
                return out("online", "在线·没在输出", True)
            if seen_age is not None and seen_age < 900:
                return out("recent", "刚才还在动（{}）".format(
                    api_self._age_text(seen_age)), True)
            if s.connected:
                return out("idle", "已收工（通道还在）", True)
            return out("unknown", "无输出信号", False)
        if ((ide_age is not None and ide_age < 900)
                or (write_age is not None and write_age < 900)
                or (seen_age is not None and seen_age < 900)):
            # 显示所有证据里最新的那个时刻，别拿旧的 transcript 时间盖过
            # 更新的 zt/写文件/交互（「自报 3分钟前」却写着「14分钟前」会被当成不准）
            shown = min(x for x in (ide_age, zt_age, seen_age, write_age)
                        if x is not None)
            return out("recent", "刚才还在动（{}）".format(api_self._age_text(shown)), True)
        # 判「收工」是判静默（第九节）：四路信号全部凉透才轮到这里——
        # transcript 停滞只是它自己的滞后，不再单独支撑收工结论
        return out("idle", "已收工（通道还在）", True)
    limit = int(hub.HUB.cfg.get("ide_active_secs", 900) or 0)
    # 断开侧同样按融合看「IDE 里还有没有动静」：transcript / 看板写文件谁新算谁
    ide_or_write = min(x for x in (ide_age, write_age) if x is not None) \
        if (ide_age is not None or write_age is not None) else None
    if recon:
        # 重启 hub 后所有历史 tab 都会先进宽限期；Cursor 那边也很久没动静的，
        # 别让「重连中」听起来像还有戏
        if ide_or_write is None or (limit and ide_or_write > limit):
            return out("recon", "重连中·多半已终止", False)
        return out("recon", "重连中", False)
    if ide_or_write is not None and limit and ide_or_write < limit:
        return out("ide", "Cursor 里还活着·通道未接", False)
    # 通道没了但刚刚还在跟控制台交互/写文件：多半是 Cursor 回收了 MCP 进程，
    # agent 本人还在干活（它下次调 zhi 就会自己接回来）。这种别判死刑
    if seen_age is not None and seen_age < 900:
        return out("lost", "失联（近期还在报到）", False)
    # 判「死」是判静默（第九节）：到这里 = 通道断 + 全部信号超阈值静默
    return out("dead", "已终止", ide_or_write is not None)
