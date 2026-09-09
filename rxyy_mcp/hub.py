# -*- coding: utf-8 -*-
"""rxyy MCP 常驻控制台：TCP 会话接入 + 多 tab GUI + 聊天记录落盘"""
import datetime
import hashlib
import hmac
import html as html_mod
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

# 启动痕迹（P0 可观测）：必须在 markdown/webview 等重 import 之前落一行——
# 07-27 两次事故里 hub 全部「无声冻死在首条日志之前」，事后无法区分是冻在
# import（WebView2 孤儿/磁盘 thrash）还是 GUI 初始化。这行一出，日志里
# 「进程已创建 → hub 启动」之间的耗时就是 import 阶段的体检数据。
_BOOT_T0 = time.time()
try:
    with open(Path(__file__).resolve().parent / "hub-run.log", "a", encoding="utf-8") as _f:
        _f.write("{} [boot] hub 进程已创建 pid={}，开始加载依赖…\n".format(
            time.strftime("%m-%d %H:%M:%S"), os.getpid()))
except OSError:
    pass

# 启动闸（07-29 23:02 事故）：hub 一慢，看门狗与 MCP 守护会各自「见端口没人就再拉一个」，
# 新 hub 又一起抢 CPU/磁盘去加载 CLR，越拉越慢——实测滚到 9 个 hub、控制台三分钟起不来。
# 已有的 .hub-spawn.lock 只由 MCP 守护写、且 20s 就过期，盖不住冷启动。
# 改由**正在启动的 hub 自己**每 5 秒续期这把锁，直到绑上端口才删：
# 启动多慢闸就关多久，进程一死锁就自然过期，谁也不会被永久挡住。
_SPAWN_GATE = Path(__file__).resolve().parent / ".hub-spawn.lock"
_SPAWN_GATE_HELD = threading.Event()


def _hold_spawn_gate():
    while not _SPAWN_GATE_HELD.wait(timeout=5):
        try:
            _SPAWN_GATE.touch()
        except OSError:
            pass


def _release_spawn_gate():
    """绑上端口 = 启动成功，放闸（顺带清掉锁文件，别让残留挡住下一次救援）。"""
    _SPAWN_GATE_HELD.set()
    try:
        _SPAWN_GATE.unlink(missing_ok=True)
    except OSError:
        pass


try:
    _SPAWN_GATE.touch()
    threading.Thread(target=_hold_spawn_gate, daemon=True).start()
except OSError:
    pass

import markdown as md
import webview

from datadir import DATA_DIR
import workflow as workflow_mod
import runtime_adapter
from session_locator import (build_locator_prompt, composer_mentions_conv,
                             agent_edit_ts_map, agent_last_edit_ts,
                             cursor_conversation_exists,
                             cursor_db_session_for_conv, cursor_project_slug,
                             find_cursor_transcript, freshest_active_session,
                             generating_now_session, generating_session_for_conv,
                             jsonl_path_for_uuid, jsonl_tool_arg_session_for_conv,
                             locate_cursor_session,
                             read_cursor_activity, read_cursor_error,
                             read_cursor_model, read_codex_model,
                             model_info_from_name, pretty_model_label,
                             sidebar_model_chip,
                             normalize_open_model,
                             iter_model_presets, iter_catalog_open_specs,
                             catalog_schema_for, read_cursor_model_catalog,
                             pinned_open_specs, PINNED_OPEN_MODELS,
                             EFFORT_OPTIONS,
                             merge_thinking_params, thinking_label,
                             read_cursor_title, read_turn_state,
                             scan_agent_stub_convs,
                             write_cursor_title, list_composer_headers,
                             looks_like_composer_id,
                             transcript_has_tool_arg, transcript_mentions)
from share_server import (intake_urls, owner_session_url, session_share_url,
                          session_share_urls, share_url, share_urls,
                          start_share_server)
from window_state import WindowStateTracker, build_window_options

try:
    import tsd_decrypt
except Exception:
    tsd_decrypt = None

try:
    import cursor_live_rename
except Exception:  # 半套同步 / 非 Windows：侧栏实时改名退化成写盘那条老路
    cursor_live_rename = None

try:
    import wbhook
except Exception:  # 半套同步：没有注入桥就只走键盘兜底
    wbhook = None

try:
    import decision_card
except Exception:  # 半套同步：卡片当没传，predefined_options 老路照旧
    decision_card = None

try:
    import cursor_turns
except Exception:  # 半套同步：实时时间线不可用，控制台照旧只见 zhi 气泡
    cursor_turns = None

try:
    import instance_owner
except ImportError:  # 半套同步（只拷了部分文件）不该让控制台整个起不来
    class instance_owner:  # type: ignore[no-redef]
        @staticmethod
        def claim(_app_dir):
            pass

        @staticmethod
        def should_stand_down(_app_dir):
            return False, ""

try:
    import live_runtime
except ImportError:  # 同上：老包里没有这个模块，照旧在原地跑
    class live_runtime:  # type: ignore[no-redef]
        @staticmethod
        def hand_over(*_a, **_kw):
            return False

        @staticmethod
        def console_root(*_a, **_kw):
            return None

try:
    import team_facts
except ImportError:  # 同上：老包里没有它，团队面板退回「只看自报」的老行为
    team_facts = None

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = DATA_DIR / "config.json"
UI_PATH = APP_DIR / "ui.html"


def rxyy_data_dirs():
    return [DATA_DIR]


def kv_config_get(key):
    """读 rxyy tools 设置页写在 workflow.db kv_config 里的值（只读，读不到给空串）。

    代码里不内置任何真实 key：分发包发出去后，别人填自己的即可，不会烧到 rxyy 的额度。
    """
    import sqlite3
    for d in rxyy_data_dirs():
        db = d / "workflow.db"
        if not db.is_file():
            continue
        try:
            conn = sqlite3.connect("file:{}?mode=ro".format(db.as_posix()), uri=True)
            try:
                row = conn.execute("SELECT v FROM kv_config WHERE k=?", (key,)).fetchone()
            finally:
                conn.close()
        except sqlite3.Error:
            continue
        if row and (row[0] or "").strip():
            return str(row[0]).strip()
    return ""

# ---------- 运行模式（P1 核心化：WebView2 从关键进程物理移除） ----------
# 默认 = 无头核心：会话路由/MCP守护/网关/分享全部照跑，唯独不建 pywebview 窗口。
# UI 一律走浏览器/rxyy tools iframe（http://127.0.0.1:38777/ui，网关 shim 已双栈半年）。
# 历次大事故（07-24、07-27×2）根源全是 WebView2 冻死拖垮整个 hub 进程——
# 无头后核心进程的可靠性等级与 server.py 对齐（纯 Python+socket，从未冻死过）。
#   --windowed  沿用旧 pywebview 窗口（过渡期后备）
#   --daemon    由自启动/看门狗/console 等自动化拉起：绝不弹浏览器
#   --autostart 仅 HKCU Run 登录拉起带这个旗：网关就绪后补开 rxyy tools 窗口
#               （看门狗/手动 --daemon 救援不加，避免每次复活都抢焦点）
#   （无参数双击 = 无头核心 + 自动开一次浏览器 UI，保留「双击就能看到控制台」手感）
HEADLESS = "--windowed" not in sys.argv
_AUTOMATED_SPAWN = ("--daemon" in sys.argv) or ("--wait-pid" in sys.argv)
OPEN_UI_ON_BOOT = HEADLESS and not _AUTOMATED_SPAWN


def should_open_console_on_autostart(argv=None):
    """登录自启才亮 rxyy tools。看门狗 --daemon 救援不抢焦点。"""
    argv = sys.argv if argv is None else argv
    return "--autostart" in argv


def open_console_window_if_absent(*, _find=None, _spawn=None):
    """登录自启补开 rxyy tools。窗口已在就不动（中午 restart_hub 不弹第二扇）。"""
    try:
        if _find is None:
            import ctypes
            _find = lambda: ctypes.windll.user32.FindWindowW(None, "rxyy tools")
        if _find():
            log_event("开机自启：rxyy tools 窗口已在，不重复打开")
            return "skipped"
        if _spawn is None:
            import restart_rxyy
            _spawn = restart_rxyy._spawn_rxyy
        _spawn()
        log_event("开机自启：已补开 rxyy tools 窗口")
        return "opened"
    except Exception as e:
        log_event("开机自启补开窗口失败: {}".format(e))
        return "failed"


def open_ui_in_browser(cfg=None):
    """用默认浏览器打开网关控制台 UI（无头模式的「亮窗口」动作）。"""
    try:
        import webbrowser
        gport = 38777
        try:
            gport = int((cfg or {}).get("gateway_port", 38777) or 38777)
        except Exception:
            pass
        webbrowser.open("http://127.0.0.1:%d/ui" % gport)
        return True
    except Exception:
        return False

DEFAULTS = {
    "port": 38999,
    "bind_host": "0.0.0.0",  # 允许局域网同事的 MCP 直接接入（非本机连接需令牌）
    # 历史目录名属于既有存储协议，品牌更名后继续沿用，避免旧记录失联。
    "history_dir": "D:\\持久plus聊天记录",
    "max_messages": 200,
    "max_history_files": 200,
    # 记录/图片保留天数（含当天）：7 = 留最近一周，0 = 不按天清理，只受份数上限约束
    "history_keep_days": 7,
    "continue_prompt": "请按照最佳实践继续",
    "always_on_top": True,
    "audio_enabled": True,
    "share_enabled": False,
    "share_port": 39080,
    "share_token": "",
    # Tailscale Serve 的 https 基址（如 https://computer.tailxxxx.ts.net）：
    # 手机浏览器的麦克风只在安全上下文(https)下开放，语音输入靠它
    "share_https_base": "",
    "push_preview": False,  # 手机推送是否带提问摘要（默认不外发内容，保持隐私）
    "asr_model": "qwen3-asr-flash",  # 语音转文字模型（百炼）
    "win_width": 920,
    "win_height": 1040,
    "win_x": None,
    "win_y": None,
    "win_state": "normal",
    # Agent 干活动辄几分钟到几十分钟，太短会把干活中的会话误判成待机
    # （Cursor 侧 composerData/transcript 都是懒写盘，磁盘上拿不到实时状态，只能靠超时语义）
    "processing_timeout_secs": 1800,
    # 保活策略【复盘后定论：设成"略小于 Cursor 硬超时"的大值，几乎永久阻塞 + 躲超时】
    # 根因：一个 zhi 只要【一直阻塞不返回】，Cursor 就把它当"进行中的活跃请求"，期间绝不
    #   回收 MCP 进程 → 不掉线。反面：keepalive 太短会让"返回→重呼"空隙频繁，被 Cursor 趁
    #   空隙回收（越短越勤挂）。所以要尽量大。
    # 但纯 0（ev.wait 永久）会在 Cursor 60min 硬超时时报错。折中：设 3000s(50min)——
    #   50min 内一直阻塞不掉线，到 50min 才【返回一次再重呼】（一次极短空隙，可忽略），
    #   刚好赶在 60min 硬超时前续上。既不掉线、又躲过 60min 超时、等待期零 token。
    #   CLI/ACP 环境（硬超时仅 60s）需改成 ≤45。
    # 取 600：这是本机长期实跑调出来的值，新装机直接继承，别让每台新机器从
    # 未调过的值重走一遍踩坑（08-03 同事机排查：他那份 config 只有两行，其余全默认）。
    "keepalive_secs": 600,
    # 保活脱离期宽限：detach 后若这么久还没等到 AI 续期重呼，判定本轮失联，
    # 清掉绿灯切回待机（否则连接还在但 agent 不再续期时，绿灯长亮、你的回复永远送不到）。
    # keepalive 短(40s)后续期频繁，但 agent 偶尔忙(跑长命令)会晚几十秒续期，给足宽限免误判。
    # 取 660（略大于 keepalive_secs）：同上，与本机实跑值对齐。90s 对忙碌的 agent 太紧，
    # 一被误判失联，绿灯灭掉、提问清空，用户后面发的话就只能排队等下一次提问。
    "detach_grace_secs": 660,
    # 报到潮让路：新会话报到/点「+」时，叫醒等待中的 zhi 续期一次「腾出并发槽位」。
    # 默认关——它赖以成立的前提（Cursor 对同一 server 的工具调用有并发上限）已被证伪：
    #   · Cursor 的 mcpProcessMain.js 里 withInFlightOp 只是给空闲回收计数，全无信号量；
    #   · 8 个 zhi 挂着时新调用仍 2ms 应答（scripts/_probe_mcp_concurrency.py）；
    #   · 六个真实窗口的报到各走各的 TCP 连接，5 秒内全部抵达。
    # 而让路每触发一次，被点名的 agent 就要白跑一轮 LLM 生成，与「待命期零 token」相悖。
    # 万一哪天真撞上报到被堵，设置面板勾回来即可（热生效，不用重启）。
    "yield_on_burst": False,
    # MCP 说明文案：默认发精简版（约 365 token/请求，原文 1237）。这段进的是系统提示词，
    # 所有窗口 × 所有对话 × 每一轮都要付，是本工具真正的 token 大头。条款一条没删，
    # 只是不再解释来龙去脉；万一哪个模型认死理不照做，勾上退回完整叙述版。
    "mcp_instructions_full": False,
    # 派活时补发资料索引（技能/项目规则的名称+一句话+路径）。它们已从 Cursor 的
    # 常驻注入里搬走——报到那一轮用不上却每轮都付，实测技能目录 2664、规则上万。
    "library_autosend": True,
    # 团队面板：角色按对话 ID 记（conv_key 跨重连稳定）。旧版席位/公告板按
    # 工作区根目录记；team_projects + team_project_members 是新版「根目录 + 项目」
    # 作用域。team_tracks 只表示项目内的二级业务线，不能再拿来决定项目归属。
    "team_roles": {},
    "team_boards": {},
    "team_projects": {},
    "team_project_members": {},
    # 分工：同一项目常有好几个「实现」各做各的功能，光看角色分不清谁在干哪块，
    # 也按 conv_key 记（与角色同口径，接手沿用原 ID 时自动跟着走）
    "team_assign": {},
    # 席位：按项目预先摆好「几号位、什么角色、负责哪块」，每个席位自带一个固定
    # conversation_id。人换了（接手/重开对话）只要报到时用席位 ID，角色和分工就
    # 原样继承——不必每次接手都重新分配一遍。{项目根: [{id,name,role,assign,created}]}
    "team_seats": {},
    # 快捷短语：输入区一键发送的常用回复
    "quick_phrases": ["用中文", "继续", "先测试再改", "提交并 push", "收工，辛苦"],
    # tab 名自动同步 Cursor 会话标题的周期（秒）；0 关闭
    "title_sync_secs": 30,
    # 反方向（控制台 → Cursor 侧栏）自动改：写盘赢不了内存。优先走 workbench 桥
    # （wbhook，内存里 updateComposerData，不抢键盘）；桥没心跳再退回
    # composer.renameChat 那条抢键盘的路（cursor_live_rename）。键盘路只在
    # 「用户空闲 ≥ idle 秒 且 Cursor 在前台」时按。0902 rxyy：一排待命壳 / 干活 tab
    # 全顶着自动标题，而规则里让 agent 调的 cursor-app-control.rename_chat
    # 这台 Cursor 3.17 根本没有。
    "sidebar_auto_rename": False,
    "sidebar_auto_rename_idle_secs": 45,
    # 控制台点 × 关会话时，顺手让窗口总线扩展把 Cursor 侧栏里那个对话 tab 收掉
    # （composer.closeComposerTab，进历史不删）。09-07 rxyy：「关会话顺带归档窗口，跟 Bajie 一样」
    "ext_close_composer_on_archive": True,
    # 断线重连宽限（秒）：MCP 掉线后先静默等重连（Cursor 回收/重启 MCP 极常见），
    # 期内 tab 停在「重连中」橙点、输入框保持可用，超时才判定真断开。
    # 给足 300s：Cursor 回收 MCP 到下次 agent 重新 zhi 常隔几分钟，短了会中途误报「已终止」。
    "reconnect_grace_secs": 300,
    # 通道断开后，若该对话的 Cursor transcript 在此秒数内有过更新，
    # tab 显示蓝点「IDE 活跃」而非黑点「已终止」（对话还在，只是没接通道）；0 关闭
    "ide_active_secs": 900,
    # 免打扰：新提问不还原窗口、不闪任务栏、不响提示音（手机推送不受影响）
    "quiet_mode": False,
    # 省额度冻结：为 True 时所有 agent 的 zhi/zt/ji 调用阻塞挂起（LLM 不推进=零 token），
    # 直到改回 False（控制台「❄ 冻结额度」开关）。升级 Pro+ 期间用，防误耗 api 额度。
    "token_freeze": False,
    # 卡死自动重连（默认关）：触碰 mcp.json 是全局大锤——Cursor 会掐掉所有工作区的
    # rxyy MCP连接、杀死进行中的 zhi。07-27 用户实测它带来的重连风暴比它救的卡死
    # 更多（新对话到不了控制台/全窗口掉线），按用户要求默认关闭；确有需要可在
    # config.json 手动打开。
    "auto_reload_on_stuck": False,
    "stuck_threshold_secs": 120,
    "auto_reload_cooldown_secs": 300,
    # 真要打开它，这两条决定这一锤能造多大孽：允许几个 agent 陪葬（默认 0 —— 待命中的
    # agent 全是「阻塞在 zhi 里」这个状态），以及一小时最多挥几次。
    "auto_reload_max_collateral": 0,
    "auto_reload_max_per_hour": 2,
    # MCP Streamable HTTP 守护进程端口（0=关闭）。mcp.json 用 url 接入后，
    # MCP 进程由 hub 拉起常驻，Cursor 手里没有进程可杀——stdio 回收问题整类消失。
    "mcp_http_port": 39222,
    # 双活数据同步（方案A 事件互灌，docs/plans/2026-08-27 设计稿）P0：
    # sync_root 指向本机事件夹（只放事件与快照）；留空=未启用。machine 留空=按主机名。
    # 路径选 ASCII（scp 摆渡过中文路径在两头代码页不一致时会碎）。
    "sync_root": "",
    "sync_machine": "",
    "sync_poll_secs": 2.0,
    # P1 运输线：Syncthing 两机没跑，先用现成 SSH 免密摆渡对端 outbox（只拉不推，
    # 见 sync_ferry.py）。三样齐才开：对端 ssh 别名（公司机填 sunrise、家机填
    # company）、对端 sync_root（远端路径，ASCII）、对端 machine 名。
    "sync_peer_ssh": "",
    "sync_peer_root": "",
    "sync_peer_machine": "",
    "sync_ferry_secs": 60.0,
    # P1 配置线：workflow.db 白名单实体（kv/项目映射/日报素材）差量轮询的拍长
    "sync_wf_poll_secs": 20.0,
    # P2 业务线：看板/任务安排站/会话控制台列表差量轮询的拍长
    "sync_console_poll_secs": 20.0,
    # 「+」按钮生成的新对话接入提示词模板；{conversation_id} 换成预分配的对话 ID，
    # {workspace}/{workspace_name} 换成工作区完整路径与目录名，{mcp_name} 换成
    # Cursor 工具表里的真名（见 fill_checkin_prompt / registered_mcp_name）。
    # 刻意压短：报到提示词会成为该对话的永久上下文，每省一字、四个壳各省一字；
    # 而且 Cursor 新对话首条消息有长度硬顶，且口径变过——08-28 按字符拒
    # 「提示词过长 780/768，错误码 1003」，08-31 按 UTF-8 字节拒「输入过长
    # 1.38K/1.00K」（上限 1024B，中文一字 3B）。全文组装后必须过
    # fill_checkin_prompt 的字节闸（≤ CURSOR_FIRST_MSG_MAX_UTF8）。
    # 唯独路径不省——留成占位符，agent 就得拿 shell 去问「我在哪」，那是几十秒一趟
    # 的三趟往返，比多出来的这几十个字贵得多（08-26 实测，见 fill_checkin_prompt）。
    "new_chat_prompt": (
        "立即调 MCP「{mcp_name}」的 zhi 报到（不思考不读文件，禁 GetMcpTools）："
        "conversation_id=「{conversation_id}」全程沿用；"
        "task_name=「待命·{workspace_name}」；project_path=「{workspace}」；"
        "message=「📍 {workspace_name}（{workspace}）· 对话 {conversation_id} 已就位」；"
        "predefined_options=[\"开始任务\",\"结束\"]。"
        "点「开始任务」前禁 Read/Grep（含 v19），最优先。"
        "报 Connection closed 一类：重试1次，仍败每20-30秒续试≥6次，禁放弃。"
        "报到后待命。"
    ),
    # 「+ → 直接拉起本机 agent」的后端：
    # cli = Cursor CLI（agent 命令）+ 已登录账号 → 用量走【订阅额度】，与 IDE 同池
    #       （多开+切号玩法适用；切号=终端 agent logout/login，控制台无需改动）
    # sdk = cursor-sdk + API key → 走 API 用量计费（与订阅分开），key 在
    #       cursor.com/dashboard → Integrations 生成
    "agent_backend": "cli",
    "cursor_api_key": "",
    "sdk_model": "auto",
    # 远程访问基址：填 Tailscale/公网可达的 http://host:port，二维码与分享链接会用它
    # （留空则用探测到的局域网 IP；手机不在公司局域网时必须填 Tailscale 地址）
    "remote_base_url": "",
    # Bark 手机推送：AI 发起提问时推到 iPhone。填你的 Bark key 或完整前缀
    # 如 https://api.day.app/xxxxx ；留空关闭。company 只放行 443，Bark 走 https 出站可用
    "bark_url": "",
    "push_enabled": False,
}


CONFIG_LOAD_ERROR = ""  # config.json 存在但解析失败时的提示（否则静默回退默认值，用户毫无感知）


def load_config():
    global CONFIG_LOAD_ERROR
    import copy
    cfg = copy.deepcopy(DEFAULTS)  # 深拷贝：防列表等可变默认值被运行时原地改动污染 DEFAULTS
    CONFIG_LOAD_ERROR = ""
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    except FileNotFoundError:
        pass
    except Exception as e:
        CONFIG_LOAD_ERROR = "config.json 解析失败，已回退默认配置: {}".format(e)
    cfg.pop("clean_previous_days", None)  # 旧版「隔天清理」布尔开关已并入 history_keep_days
    # 配置/凭证分离（蓝图阶段2）：对应环境变量存在时覆盖 config.json——凭证可只走
    # env 不落盘。没设任何 CHIJIU_* 变量时 cfg 一字不动，故对现有部署零影响。
    try:
        import config_schema
        config_schema.apply_env_overrides(cfg)
    except Exception:
        pass
    return cfg


_CONFIG_SAVE_LOCK = threading.Lock()


def save_config(cfg):
    """只持久化与默认值不同的键。旧版全量落盘会把 DEFAULTS 整套钉死在 config.json，
    之后代码升级默认值永远不生效——07-28 实测：接入提示词压缩版模板上线后，
    config.json 里钉着的旧长版模板仍在被使用，省 token 改进形同虚设。

    落盘走 tmp + os.replace，跟本仓其余每一处状态一个写法（save_state / _save_relays /
    _save_takeover_aliases / board / taskstage 全是）。`write_text` 是先把 config.json
    截成 0 再往里灌，中间被打断留下的就是半截 JSON，下次 load_config 解析失败整套回退
    DEFAULTS——`share_token` 一变，手机分享链接和同事投递链接全队一起失效；
    team_project_members / team_roles / team_assign 那些团队归属也一并归零。
    而 hub 恰恰是常被打断的那个进程：/api/restart_hub 接力重启、看门狗回收，
    两条退出路径的最后一件事都是 save_config。

    要锁是因为写它的不止一条线程：API 线程、防抖用的 threading.Timer(0.8, ...)、
    _persist_wip_ledger 那条 git 扫描线程。没锁时它们各写各的 tmp 再互相 replace，
    换名本身虽是原子的，但两份内容会来回覆盖。
    """
    # 接力重启那几秒新旧两个 hub 同时在世，tmp 名带 pid 才不会互相写进对方那份半成品
    tmp = "{}.{}.tmp".format(CONFIG_PATH, os.getpid())
    try:
        slim = {k: v for k, v in cfg.items() if k not in DEFAULTS or DEFAULTS[k] != v}
        body = json.dumps(slim, ensure_ascii=False, indent=2)
        with _CONFIG_SAVE_LOCK:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(body)
            os.replace(tmp, CONFIG_PATH)
            tmp = ""
    except Exception as e:
        # 原来这里是 except: pass。配置存不下去是「团队归属白改了」级别的事，
        # 至少得在 hub-run.log 上留一行，否则等发现时已经无从查起。
        log_event("config.json 写盘失败，盘上仍是上一份完整配置: {}".format(e))
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def send_msg(sock, obj):
    import struct
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sock.sendall(struct.pack(">I", len(data)) + data)


def recv_msg(sock):
    import struct

    def recv_exact(n):
        buf = b""
        while len(buf) < n:
            try:
                chunk = sock.recv(n - len(buf))
            except OSError:
                # 对端进程被强杀时连接以 RST 中断，必须按正常断开处理，
                # 否则 handle_client 线程崩溃，会话永远停留在"回复中"状态
                return None
            if not chunk:
                return None
            buf += chunk
        return buf

    header = recv_exact(4)
    if header is None:
        return None
    (length,) = struct.unpack(">I", header)
    if length > 256 * 1024 * 1024:
        return None
    data = recv_exact(length)
    if data is None:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except Exception:
        return None


def _md_extension_instances():
    """扩展必须传实例而不是字符串名：字符串名会让 markdown 走
    get_installed_extensions() → importlib.metadata.entry_points()，全量读
    site-packages/_internal 里每个 dist-info 的 entry_points.txt。这台机器的
    企业 DLP（TSD）按扩展名加密 .txt，打包产物里部分 entry_points.txt 落盘
    即成非 UTF-8 密文，扫描一读就 UnicodeDecodeError → 全部消息静默退化成
    <pre> 纯文本（07-31 实锤：exe 版控制台气泡里全是裸 ** / ##）。
    直接 import 扩展类实例化，完全绕开 entry_points 扫描。"""
    from markdown.extensions import Extension
    from markdown.extensions.fenced_code import FencedCodeExtension
    from markdown.extensions.nl2br import Nl2BrExtension
    from markdown.extensions.sane_lists import SaneListExtension
    from markdown.extensions.tables import TableExtension
    from markdown.inlinepatterns import InlineProcessor, SimpleTagInlineProcessor
    import xml.etree.ElementTree as etree

    class _BareUrl(InlineProcessor):
        """裸 http(s) URL 自动成链（AI 汇报常直接贴链接）。inline pattern
        跑在代码块/行内代码被摘走之后，不会误伤代码里的 URL。"""
        _TRAIL = "。，、；：！？.,;:!?)】》>\"'"

        def handleMatch(self, m, data):
            url = m.group(0)
            while url and url[-1] in self._TRAIL:
                url = url[:-1]
            if len(url) < 10:
                return None, None, None
            el = etree.Element("a")
            el.set("href", url)
            el.set("target", "_blank")
            el.set("rel", "noopener noreferrer")
            el.text = url
            return el, m.start(0), m.start(0) + len(url)

    class _ChatExtras(Extension):
        """GFM 删除线 ~~x~~ + 裸链接自动成链（Cursor 常用而 python-markdown 不带）"""

        def extendMarkdown(self, md_inst):
            md_inst.inlinePatterns.register(
                SimpleTagInlineProcessor(r"(~~)(.+?)~~", "del"), "chat_del", 175)
            # 只收 ASCII URL 字符：中文标点/汉字天然截断（AI 中文汇报里贴链接的常态）
            md_inst.inlinePatterns.register(
                _BareUrl(r"https?://[A-Za-z0-9\-._~:/?#@!$&*+,;=%\[\]]+", md_inst),
                "chat_bareurl", 50)

    return [FencedCodeExtension(), TableExtension(), Nl2BrExtension(),
            SaneListExtension(), _ChatExtras()]


# Cursor 代码引用围栏：```12:14:app/components/Todo.tsx（行号+文件路径做围栏信息）
_CITE_FENCE_RE = re.compile(r"^(`{3,})(\d+):(\d+):(.+?)\s*$")
_EXT_LANG = {
    ".py": "python", ".ts": "typescript", ".tsx": "typescript", ".js": "javascript",
    ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".json": "json",
    ".html": "html", ".htm": "html", ".vue": "html", ".xml": "xml", ".css": "css",
    ".scss": "scss", ".less": "less", ".md": "markdown", ".mdc": "markdown",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".ps1": "powershell",
    ".psm1": "powershell", ".bat": "dos", ".cmd": "dos", ".yml": "yaml",
    ".yaml": "yaml", ".toml": "ini", ".ini": "ini", ".cfg": "ini", ".conf": "ini",
    ".sql": "sql", ".go": "go", ".rs": "rust", ".java": "java", ".c": "c",
    ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp", ".rb": "ruby",
    ".php": "php", ".swift": "swift", ".kt": "kotlin", ".lua": "lua",
    ".dockerfile": "dockerfile", ".diff": "diff", ".patch": "diff",
}


# 列表行：-/*/+ 或 1. / 1) 开头（不含围栏内部，调用方负责跳过）
_LIST_LINE_RE = re.compile(r"^([ ]{1,15})([-*+]|\d{1,3}[.)])[ ]")


def _normalize_list_indent(text):
    """AI 圈习惯 2 空格嵌套列表（GFM 语义），python-markdown 要 4 空格才算嵌套，
    结果子项全被拍平成同级。检测到 2/3 空格缩进的列表行时，把所有列表行缩进 ×2
    对齐到 4 空格制；纯 4 空格制的消息不含 2/3 缩进行，原样不动。"""
    lines = (text or "").split("\n")
    has_shallow, in_fence, mark = False, False, ""
    for ln in lines:
        s = ln.strip()
        if in_fence:
            if s.startswith(mark) and not s.strip("`"):
                in_fence = False
            continue
        if s.startswith("```"):
            in_fence, mark = True, "`" * (len(s) - len(s.lstrip("`")))
            continue
        m = _LIST_LINE_RE.match(ln)
        if m and len(m.group(1)) in (2, 3):
            has_shallow = True
            break
    if not has_shallow:
        return text
    out, in_fence, mark = [], False, ""
    for ln in lines:
        s = ln.strip()
        if in_fence:
            out.append(ln)
            if s.startswith(mark) and not s.strip("`"):
                in_fence = False
            continue
        if s.startswith("```"):
            in_fence, mark = True, "`" * (len(s) - len(s.lstrip("`")))
            out.append(ln)
            continue
        m = _LIST_LINE_RE.match(ln)
        out.append(" " * min(len(m.group(1)) * 2, 16) + ln.lstrip(" ") if m else ln)
    return "\n".join(out)


def _upgrade_code_citations(text):
    """把 Cursor 代码引用块转成「📄 路径 · 行号」说明行 + 按扩展名标语言的普通代码块。
    不动已在围栏内部的内容；说明行用块级 <div>（前后留空行），nl2br 不会碰它。"""
    out, in_fence, mark = [], False, ""
    for ln in (text or "").split("\n"):
        s = ln.strip()
        if in_fence:
            out.append(ln)
            if s.startswith(mark) and not s.strip("`"):
                in_fence = False
            continue
        if s.startswith("```"):
            m = _CITE_FENCE_RE.match(s)
            if m and any(c in m.group(4) for c in "/\\."):
                ticks, a, b, path = m.group(1), m.group(2), m.group(3), m.group(4)
                lang = _EXT_LANG.get(Path(path.replace("\\", "/")).suffix.lower(), "")
                out += ["", '<div class="codecite">📄 {} · 第 {}–{} 行</div>'.format(
                    html_mod.escape(path), a, b), "", ticks + lang]
                in_fence, mark = True, ticks
                continue
            in_fence, mark = True, "`" * (len(s) - len(s.lstrip("`")))
        out.append(ln)
    return "\n".join(out)


# GFM 任务清单：python-markdown 渲染成 <li>[x] … → 换成真复选框
_TASK_LI_RE = re.compile(r"(<li>(?:<p>)?)\[([ xX])\]\s*")
# 消息里内嵌的本地图片（AI 引用截图等）：<img src="D:\..."> 浏览器/手机加载不了
_IMG_SRC_RE = re.compile(r'(<img [^>]*?src=")([^"]+)(")', re.I)
# 与 gateway /img、share /api/image 的可服务扩展名保持一致（否则复制了也 404）
_LOCAL_IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

# ![说明](D:\路径\图.png) 里的 Windows 绝对路径（盘符或 UNC）。
# 允许成对的括号，否则 C:\Program Files (x86)\… 这种极常见的路径会在第一个
# 右括号处被截断（markdown 链接目标本来就按配对括号解析）
_MD_LOCAL_IMG_RE = re.compile(
    r"!\[([^\]\n]*)\]\(\s*((?:[A-Za-z]:[\\/]|\\\\)(?:[^()\n]|\([^()\n]*\))*?)\s*\)")


def _protect_local_img_paths(text):
    """把 ![](D:\\...) 提前变成裸 <img>，绕开 markdown 的反斜杠转义。

    python-markdown 会把 \\. \\_ \\- \\( 等当成转义序列吃掉那个反斜杠，于是
    `D:\\a\\.chijiu-tmp\\x.png` 渲染完成了 `D:\\a.chijiu-tmp\\x.png`——路径当场
    失效，_localize_imgs 找不到文件就不搬运，前端 onerror 把图一藏，用户那头
    就是「AI 说发了图，我啥也没看见」（08-07 实测，一张都没显示出来）。
    裸 HTML 不经转义原样穿过 markdown，交给后面的 _localize_imgs 正常搬运。
    围栏内的内容不动，代码块里写路径就该原样显示。"""
    if "![" not in (text or ""):
        return text
    out, in_fence, mark = [], False, ""
    for ln in (text or "").split("\n"):
        s = ln.strip()
        if in_fence:
            out.append(ln)
            if s.startswith(mark) and not s.strip("`"):
                in_fence = False
            continue
        if s.startswith("```"):
            in_fence, mark = True, "`" * (len(s) - len(s.lstrip("`")))
            out.append(ln)
            continue
        out.append(_MD_LOCAL_IMG_RE.sub(
            lambda m: '<img alt="{}" src="{}">'.format(
                html_mod.escape(m.group(1), quote=True),
                html_mod.escape(m.group(2), quote=True)), ln))
    return "\n".join(out)


# 控制台常被 rxyy tools「rxyy MCP」整页 iframe 嵌住。普通 <a> 一点就把
# iframe 导航到外站；Figma 等带 X-Frame-Options 的站会整页变成
# 「www.figma.com 拒绝了我们的连接请求」，看起来像控制台坏了。
_A_TAG_RE = re.compile(r"<a\s+([^>]*?)>", re.I)
_A_HREF_RE = re.compile(r"""href\s*=\s*(['"])(.*?)\1""", re.I)
_A_TARGET_RE = re.compile(r"(?i)\btarget\s*=")
_A_REL_RE = re.compile(r"(?i)\brel\s*=")
_HTTP_HREF_RE = re.compile(r"(?i)^https?://")


def _force_http_links_new_tab(html):
    """http(s) 外链补 target=_blank；已有 target/rel 的不覆盖。"""
    def repl(m):
        attrs = m.group(1)
        hm = _A_HREF_RE.search(attrs)
        if not hm or not _HTTP_HREF_RE.match(hm.group(2)):
            return m.group(0)
        if not _A_TARGET_RE.search(attrs):
            attrs += ' target="_blank"'
        if not _A_REL_RE.search(attrs):
            attrs += ' rel="noopener noreferrer"'
        return "<a %s>" % attrs
    return _A_TAG_RE.sub(repl, html)


def _polish_html(html):
    """渲染后修饰：任务清单复选框 + 宽表格包一层水平滚动容器（手机端不撑破气泡）
    + 外链新标签（避免嵌在 rxyy tools iframe 里点开把控制台顶掉）。"""
    html = _TASK_LI_RE.sub(
        lambda m: m.group(1).replace("<li>", '<li class="task">', 1)
        + '<input type="checkbox" disabled{}> '.format(
            " checked" if m.group(2) in "xX" else ""), html)
    html = html.replace("<table>", '<div class="tblwrap"><table>') \
               .replace("</table>", "</table></div>")
    return _force_http_links_new_tab(html)


def _localize_imgs(html):
    """markdown 内嵌的本地绝对路径图片复制进 聊天记录/图片/<日期>/（与用户发图同一套
    保留策略与代理白名单），前端改走 /img · /api/image 就都能显示，手机也看得到。"""
    def repl(m):
        src = html_mod.unescape(m.group(2))
        if not re.match(r"^([A-Za-z]:[\\/]|\\\\)", src):
            return m.group(0)
        try:
            hub = globals().get("HUB")
            p = Path(src)
            if (hub is None or p.suffix.lower() not in _LOCAL_IMG_EXTS
                    or not p.is_file() or p.stat().st_size > 20 * 1024 * 1024):
                return m.group(0)
            import hashlib
            import shutil
            d = Path(hub.cfg["history_dir"]) / "图片" / time.strftime("%Y%m%d")
            d.mkdir(parents=True, exist_ok=True)
            tag = hashlib.md5("{}|{}".format(
                p.resolve(), p.stat().st_mtime_ns).encode("utf-8")).hexdigest()[:16]
            target = d / "md_{}{}".format(tag, p.suffix.lower())
            if not target.exists():
                shutil.copyfile(p, target)
            return m.group(1) + html_mod.escape(str(target)) + m.group(3)
        except Exception:
            return m.group(0)
    return _IMG_SRC_RE.sub(repl, html)


def push_summary(text, limit=100):
    """提问原文压成锁屏能读的一句话：抹掉代码块/图片/markdown 记号，收敛空白。"""
    t = re.sub(r"```[\s\S]*?(?:```|$)", " 〔代码〕 ", text or "")
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "〔图〕", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)  # 链接只留文字
    t = re.sub(r"^[ \t]*(#{1,6}|>+|[-*+]|\d+[.)])\s+", "", t, flags=re.M)
    t = t.replace("**", "").replace("`", "").replace("~~", "")
    t = re.sub(r"\s+", " ", t).strip()
    return (t[:limit] + "…") if len(t) > limit else t


def render_markdown(text, is_markdown=True):
    if not is_markdown:
        return "<pre class='plain'>{}</pre>".format(html_mod.escape(text or ""))
    try:
        prepped = _protect_local_img_paths(
            _upgrade_code_citations(_normalize_list_indent(text or "")))
        html = md.markdown(prepped, extensions=_md_extension_instances())
        return _localize_imgs(_polish_html(html))
    except Exception as e:
        log_event("markdown 渲染失败，退化纯文本: {!r}".format(e))
        return "<pre class='plain'>{}</pre>".format(html_mod.escape(text or ""))


# ---------------- 成果展示 ----------------
# agent 改完 bug/UI/功能后，把产物挂在这次 zhi 上，人在外面用手机就能直接验收。
# 样式一律内联而不是走 CSS 类：控制台 ui.html 与分享页 share.html 各有一套样式表，
# 两边还常被并行改动，内联样式让两端零改动就长一个样，也不会被谁的重构冲掉。
_DIFF_MAX = 20000          # 单条 diff 上限，超了截断（手机上翻不动，也撑爆快照）
_ARTIFACTS_MAX = 12        # 一次最多展示几件，防止刷屏
_SAFE_URL_RE = re.compile(r"^(https?://|/)", re.I)

_ART_BOX = ('<div class="artifacts" style="margin-top:10px;border:1px solid '
            'rgba(127,127,127,.28);border-radius:10px;overflow:hidden">')
_ART_HEAD = ('<div style="padding:6px 10px;font-weight:600;font-size:13px;'
             'background:rgba(127,127,127,.12)">🎁 本次成果</div>')
_ART_BODY = ('<div style="padding:10px;display:flex;flex-direction:column;gap:12px">')
_ART_NOTE = 'font-size:12px;opacity:.75;line-height:1.5'
_ART_IMG = ('max-width:100%;border-radius:8px;display:block;cursor:zoom-in;'
            'border:1px solid rgba(127,127,127,.2)')


def _art_get(item, *names):
    """成果字段取值：中英文键名都认。agent 想不起来准确拼写时也不至于整条丢掉。"""
    for n in names:
        v = item.get(n)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _art_kind(item):
    """类型缺失时按已有字段猜：给了地址就是链接，给了前后两张图就是对比，等等。"""
    kind = _art_get(item, "类型", "type", "kind").lower()
    alias = {"screenshot": "截图", "图": "截图", "图片": "截图", "image": "截图",
             "对比图": "对比", "before_after": "对比", "compare": "对比",
             "差异": "diff", "代码": "diff", "patch": "diff",
             "link": "链接", "url": "链接", "网页": "链接", "预览": "链接"}
    kind = alias.get(kind, kind)
    if kind in ("截图", "对比", "diff", "链接"):
        return kind
    if _art_get(item, "前", "before") and _art_get(item, "后", "after"):
        return "对比"
    if _art_get(item, "地址", "url", "链接"):
        return "链接"
    if _art_get(item, "内容", "content", "diff"):
        return "diff"
    return "截图" if _art_get(item, "路径", "path", "file") else ""


def _art_note_html(note, extra=""):
    if not note:
        return ""
    return '<div style="{}{}">{}</div>'.format(
        _ART_NOTE, extra, html_mod.escape(note))


def _art_img(path, style=_ART_IMG):
    """本地路径原样写进 src：随后统一过 _localize_imgs 拷进 图片/ 目录，
    两个前端都会把它改写成自己的图片代理（控制台 /img、分享页 /api/image）。"""
    return '<img src="{}" style="{}">'.format(html_mod.escape(path), style)


_IMG_EXT_RE = re.compile(r"\.(png|jpe?g|gif|webp|bmp|svg)(\?.*)?$", re.I)


def _art_is_image_ref(p):
    """这个值像不像一张图：http(s)/站内 URL、本机绝对路径（盘符 / UNC）、或带图片后缀。

    09-07 真机：cb2eefee 把「25s 墙 -> TIMEOUT -> 一律收壳 -> …」这种一句话填进「对比」的 前/后，
    原样进 <img src> 后控制台每次重绘都对 /25s%20墙… 发 404、卡片里两张裂图。一句话就当文字画。"""
    p = (p or "").strip()
    if not p or "\n" in p or len(p) > 500:
        return False
    if _SAFE_URL_RE.match(p) or re.match(r"^([A-Za-z]:[\\/]|\\\\)", p):
        return True
    return bool(_IMG_EXT_RE.search(p))


def _art_img_or_text(p, style=_ART_IMG):
    """图就 <img>；不是图（agent 填了一句话）就按文字画，别让浏览器去请求一句中文。"""
    if _art_is_image_ref(p):
        return _art_img(p, style)
    return '<div style="{};white-space:pre-wrap;word-break:break-word">{}</div>'.format(
        _ART_NOTE, html_mod.escape(p))


def _render_artifact(item):
    kind = _art_kind(item)
    note = _art_get(item, "说明", "note", "desc", "title")
    if kind == "截图":
        p = _art_get(item, "路径", "path", "file")
        if not p:
            return ""
        return "<figure style='margin:0'>{}{}</figure>".format(
            _art_img_or_text(p), _art_note_html(note, ";margin-top:4px"))
    if kind == "对比":
        before, after = _art_get(item, "前", "before"), _art_get(item, "后", "after")
        if not (before or after):
            return ""
        cell = ("<figure style='flex:1 1 220px;margin:0'>"
                "<figcaption style='{};margin-bottom:4px'>{}</figcaption>{}</figure>")
        pair = ""
        if before:
            pair += cell.format(_ART_NOTE, "改前", _art_img_or_text(before))
        if after:
            pair += cell.format(_ART_NOTE, "改后", _art_img_or_text(after))
        return ("<div><div style='display:flex;gap:8px;flex-wrap:wrap'>{}</div>{}</div>"
                .format(pair, _art_note_html(note, ";margin-top:6px")))
    if kind == "diff":
        body = _art_get(item, "内容", "content", "diff", "文本", "text")
        if not body:
            return ""
        if len(body) > _DIFF_MAX:
            body = body[:_DIFF_MAX] + "\n…（已截断，完整改动请在电脑上看）"
        return ("<div>{}<pre><code class='language-diff'>{}</code></pre></div>"
                .format(_art_note_html(note, ";margin-bottom:4px"),
                        html_mod.escape(body)))
    if kind == "链接":
        url = _art_get(item, "地址", "url", "链接", "link")
        # javascript: / data: 一律不放行——成果由 agent 生成，仍按不可信内容对待
        if not url or not _SAFE_URL_RE.match(url):
            return ""
        label = note or url
        return ('<a href="{}" target="_blank" rel="noopener" '
                'style="display:inline-block;padding:7px 12px;border-radius:8px;'
                'background:rgba(64,132,255,.14);border:1px solid rgba(64,132,255,.35);'
                'text-decoration:none;font-size:13px;word-break:break-all">🌐 {}</a>'
                .format(html_mod.escape(url), html_mod.escape(label)))
    return ""


def render_artifacts(artifacts):
    """把 zhi 带来的成果清单渲染成一段可直接塞进气泡的 HTML；没有可展示的就返回空串。"""
    if not artifacts:
        return ""
    try:
        items = [x for x in artifacts if isinstance(x, dict)][:_ARTIFACTS_MAX]
        parts = [h for h in (_render_artifact(x) for x in items) if h]
        if not parts:
            return ""
        return _localize_imgs(
            _ART_BOX + _ART_HEAD + _ART_BODY + "".join(parts) + "</div></div>")
    except Exception as e:
        log_event("成果渲染失败，本次跳过: {!r}".format(e))
        return ""


def artifacts_digest(artifacts):
    """成果的一行文字摘要：写进聊天记录 .md，也用于手机推送。"""
    out = []
    for x in (artifacts or []):
        if not isinstance(x, dict):
            continue
        kind = _art_kind(x)
        if not kind:
            continue
        note = _art_get(x, "说明", "note", "desc", "title")
        ref = _art_get(x, "路径", "path", "file", "地址", "url", "后", "after")
        out.append("{}：{}".format(kind, note or ref or ""))
    return out


# 注意扩展名必须是 .log：这台机器装有按扩展名透明加密的企业 DLP（TSD），
# .txt 落盘即被加密成乱码，.log/.md/.jsonl 不受影响
def _pick_log_path():
    """运行日志落地位置：APP_DIR 可写就用它（源码态即现状，与顶部 boot 日志同
    文件、历史连续），不可写才回退 DATA_DIR。

    08-27 转告事故的顺手发现：打包冻结形态 APP_DIR=_internal\\rxyy MCP 常只读/
    被 build 清空，旧写法 `LOG_PATH = APP_DIR / "hub-run.log"` 让 log_event 在那儿
    静默写失败，重启现场无一行日志可查。DATA_DIR 跟机器态走、必可写
    （datadir.resolve_data_dir 保证 mkdir 成功）——探测一次挑出能写的那个。"""
    for cand in (APP_DIR / "hub-run.log", DATA_DIR / "hub-run.log"):
        try:
            cand.parent.mkdir(parents=True, exist_ok=True)
            with open(cand, "a", encoding="utf-8"):
                pass
            return cand
        except OSError:
            continue
    return APP_DIR / "hub-run.log"


LOG_PATH = _pick_log_path()
_log_lock = threading.Lock()
_log_write_warned = False


def log_event(msg):
    """轻量运行日志：连接/断开/复活/重启等关键事件，便于事后排查（非仅崩溃）。
    超过 512KB 滚动为 .1 备份，最多占用约 1MB。"""
    try:
        with _log_lock:
            try:
                if LOG_PATH.exists() and LOG_PATH.stat().st_size > 512 * 1024:
                    bak = LOG_PATH.with_suffix(".log.1")
                    if bak.exists():
                        bak.unlink()
                    LOG_PATH.rename(bak)
            except OSError:
                pass
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write("{} {}\n".format(
                    datetime.datetime.now().strftime("%m-%d %H:%M:%S"), msg))
    except Exception as e:
        # 连日志都写不出去是排障的死角（转告事故里重启现场无日志可查就是这么来的）：
        # 至少往 stderr（spawn-stderr.log 接着）抖一次，之后同类失败不再刷屏。
        global _log_write_warned
        if not _log_write_warned:
            _log_write_warned = True
            try:
                sys.stderr.write(
                    "[log_event] 运行日志写盘失败，后续同类不再告警 "
                    "path={} err={!r}\n".format(LOG_PATH, e))
                sys.stderr.flush()
            except Exception:
                pass


def _load_json_resilient(path, what="状态文件"):
    """读 JSON，抗 TSD 透明加密。返回 (data, status)：

      status="ok"         —— 读到并解析成功，data 是解析结果
      status="absent"     —— 文件不存在（正常：首次运行/尚未落盘），data=None
      status="unreadable" —— 文件在盘上却读/解析不了、连解密也没救回来，data=None

    unreadable 与 absent 必须分开：调用方绝不能把「盘上有账却读不了」静默当空。
    08-27 转告事故根因正是——takeover-aliases.json 处于 TSD 密文态（冻结 exe 读到
    %TSD-Header%）时，_load_takeover_book 吞异常返回空台账，全部接手别名失效、
    被归并壳重连被当新报到、开出重复 tab。这里在读失败时先尝试原地 TSD 解密再读，
    真救不回来也会 log_event 举手，不再无声降级。"""
    p = Path(path)
    if not p.exists():
        return None, "absent"

    def _read():
        return json.loads(p.read_text(encoding="utf-8"))

    try:
        return _read(), "ok"
    except Exception as e1:
        if tsd_decrypt is not None:
            try:
                tsd_decrypt.decrypt_file(str(p))
                data = _read()
                log_event("{} 读时为密文，已原地 TSD 解密后读回 path={}".format(what, p))
                return data, "ok"
            except Exception as e2:
                log_event("{} 解密后仍不可读 path={} err={!r}".format(what, p, e2))
        else:
            log_event("{} 读失败且本机无 TSD 解密能力 path={} err={!r}".format(
                what, p, e1))
        return None, "unreadable"


def _ensure_plaintext_ondisk(path):
    """本机装了 IPGuard/TSD 时，Python 写出的文件会被透明加密，冻结 exe/certutil
    等非授信进程读到密文。落盘后原地解密一次（幂等、对未加密文件安全 no-op），
    保证任何进程都读得到明文。无解密能力时静默降级。"""
    if tsd_decrypt is None:
        return
    try:
        tsd_decrypt.decrypt_file(str(path))
    except Exception:
        pass


def now_hms():
    return datetime.datetime.now().strftime("%H:%M:%S")


def now_full():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def sanitize_filename(name):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_")[:24] or "会话"


# 接手提示词的开场白。派单/粘贴落到某个 tab 时靠它认出「这不是给这个壳派的活」
# （见 Api._auto_label_on_dispatch），改文案时两处一起走
TAKEOVER_PROMPT_HEAD = "你的任务：接手一个此前在rxyy MCP控制台中断的会话"

# 用户在控制台 × 掉一个对话后，这个 ID 上的 zhi 一律回这句。
# 只封 ID，不封 agent：旧文案「请立即结束当前任务，不要再调用 zhi」被接手方读成
# 「rxyy MCP 连不上了」，当场停手并把整个 MCP 判死（08-25 实测：壳 6176cedf 干了
# 49 分钟，收壳后末一次 zhi 撞上封条就收工，手里的原会话 ID 一次都没再用）。
CLOSED_CONV_REPLY = (
    "[用户已在rxyy MCP控制台关闭此对话：这个 conversation_id 到此为止，不要再拿它调 zhi。"
    "注意这不是 MCP 故障，rxyy MCP 仍然在线——若你手上还有别的 conversation_id"
    "（例如接手来的原会话 ID），继续用那个照常干活收尾；确实没有别的 ID 时才结束任务。"
    "任何情况下都不要停用或断开rxyy MCP。]"
)


def takeover_prompt_target(text):
    """从接手提示词里取出原会话 conversation_id。不是接手词则返回 None。

    提示词里会多次出现同一个 ID（「立刻用 conversation_id=「xxx」」和
    「- conversation_id：xxx」）。取第一个即可。html 气泡先剥标签再认，
    控制台发出去的用户消息存的是 render_markdown 后的 html。
    """
    raw = str(text or "")
    if TAKEOVER_PROMPT_HEAD not in raw:
        return None
    if "<" in raw:
        raw = html_mod.unescape(re.sub(r"<[^>]+>", "\n", raw))
    m = _MD_ASKED_CONV.search(raw)
    return m.group(1) if m else None

# 云端/子代理运行时会把项目级 MCP 名展成「project-0-<目录名>-rxyy MCP」（用户级
# 则是「user-rxyy MCP」），直呼「rxyy MCP」必报 MCP server does not exist；而报到词
# 又禁 GetMcpTools，agent 就地锁死（08-27 壳 f9c313ed 实测 + 用户「有的 agent 说
# 找不到rxyy MCP mcp，耗时很久才接入」）。逃生口必须写进报到词本身——连不上
# 服务器的 agent 看不到服务器侧的任何 instructions。
# 08-28 再进一步：控制台自己读 mcp.json 就能算出真名（registered_mcp_name），
# 直接写进报到词首调即中，逃生口退居兜底（真名也可能过期——用户改过键名）。
MCP_NAME_ESCAPE = ("若报「MCP server does not exist」：真名带前缀（如 user-rxyy MCP），"
                   "准用一次 GetMcpTools 搜「rxyy MCP」取真名再调。")

# 报到词里已写了真名：再搜工具表只会白烧一轮。失败只准原名重试。
MCP_NAME_ESCAPE_KNOWN = ("CallMcpTool 的 server 填报到词里那个真名，失败只重试禁搜。")

# 手动复制可能被粘到 Cursor、Codex 或别的宿主。接入的判据是宿主确实暴露并执行了
# zhi，不是模型在正文里打印一段长得像工具调用的文字；原生任务还必须带真实 thread
# ID，否则桌面流会绑定到错误/空任务。放在模板外，用户保存过旧模板也会获得这条闸。
CHECKIN_REAL_TOOL_HINT = (
    "真调zhi+thread_id")


def registered_mcp_name(port=None):
    """Cursor 工具表里这台服务器的真名（CallMcpTool 的 server 参数）。

    Cursor 按 mcp.json 归属加前缀：用户级 %USERPROFILE%/.cursor/mcp.json 的键
    「rxyy MCP」在工具表里叫「user-rxyy MCP」，项目级叫「project-0-<目录>-rxyy MCP」。
    报到词以前只写「rxyy MCP」，agent 直呼必报 does not exist，再花一整轮
    GetMcpTools 搜真名（08-28 壳 8828b74d / ecd46331 各白烧一轮，用户追问
    「真名的问题还存在啊？」）。只认用户级：本机项目级条目是防双连占位。
    读不出（文件没有/透明加密/键被改）返回空串，调用方回退老文案——算不出
    名字不能把报到词搞崩。"""
    try:
        p = Path.home() / ".cursor" / "mcp.json"
        servers = (json.loads(p.read_text(encoding="utf-8")) or {}).get("mcpServers") or {}
        want = ":{}/mcp".format(int(port or DEFAULTS["mcp_http_port"]))
        for key, v in servers.items():
            if isinstance(v, dict) and not v.get("disabled") \
                    and want in str(v.get("url") or ""):
                return "user-" + str(key)
    except Exception:
        pass
    return ""

# 报到壳的【出生名】：new_chat_prompt 让 agent 用「待命·<工作区>」报到。
# 认壳只能认出生名，绝不能认当前名——tab 名有好几条路会被改掉（Cursor 自动
# 标题同步、派活自动命名），一改就不再以「待命」打头，收壳/身份校准全瞎
# （08-07 实测：待命·smart…7 被 Cursor 标题改成「Persistent task reporting」，
# 从此永远收不掉）。见 Session.shell_born / Hub._is_checkin_shellish。
CHECKIN_SHELL_NAME_RE = re.compile(r"^(待命|Persistent Plus check-?in)", re.I)

# Cursor 按对话开头自动生成的标题。报到词是每个壳的第一条消息，于是 Cursor 给
# 全场起同一个名字——08-27 用户截图里侧栏一整屏「Persistent plus zhi report」
# 「Persistent plus task report」就是它。这类名字有两条路会灌进控制台：
# ① sync_cursor_titles 从 Cursor 读回来（早有 _title_is_boilerplate 挡）；
# ② agent 自己照抄它，拿去当 task_name 报上来（08-28 09:07 实测 d7044dd1 就这么
#    把 tab 改成了「Persistent plus task report」）——这条一直没挡，而且更糟：
#    自报名字会立 agent_named，_push_name_to_cursor 反手又把它写回 Cursor 库。
# 旧的固定关键词表（"persistent plus report"）夹不住中间那个 zhi/task，改成正则。
AUTO_TITLE_RE = re.compile(
    r"persistent[\s\-_]*plus\b.*\breport|persistent\b.*\btask\s*report", re.I)


# 报到那一刻就把 Cursor 侧栏名字定死。Cursor 的自动起名有个决定性前提——
# workbench 里 shouldRenameComposer 最后一档是 `!n.name`：**只有还没有名字的
# 对话才会被自动起名**。所以名字不是「事后改回来」的问题，是「谁先落下」的问题：
# 第一轮就把名字落下，Cursor 那句 Persistent plus zhi report 根本不会产生。
# 标题带上 conversation_id 前 4 位（控制台撞名时用的也是这个后缀），四个壳并排
# 才分得出谁是谁——08-28 用户截图里编辑器顶栏四个 tab 全叫「Persistent plus
# task re...」，问的就是「这个真名，不能一开始就给他设置对吗」。
CHECKIN_RENAME_LINE = (
    "并行多做一件：CallMcpTool cursor-app-control 的 rename_chat，"
    "arguments={{\"title\":\"{}\"}}；无此工具就跳过（子代理没有）。")


def checkin_chat_title(conversation_id, cwd="", ws_name=""):
    """报到壳在 Cursor 侧栏该叫什么：待命·<工作区>·<对话ID前4位>。

    ws_name 给「控制台这一侧不知道工作区」的场合用：传占位符进来，让 agent
    自己把它换成上下文里的真目录名。这里绝不能自作主张写死「workspace」——
    报到词其余部分（task_name/message）发下去的是占位符，agent 手上有真路径
    时会填成「待命·cursor工作流」，改名那句却拿着字面「待命·workspace·d704」
    去改 Cursor 侧栏，两边当场对不上（08-28 用户截图：控制台是真名、侧栏还
    是 workspace）。占位符一致，两边就永远同一个答案。
    """
    name = (str(ws_name or "").strip()
            or os.path.basename(str(cwd or "").strip().strip('"').rstrip("\\/"))
            or "workspace")
    cid = str(conversation_id or "").strip()
    return "待命·{}·{}".format(name, cid[:4]) if cid else "待命·{}".format(name)


# 「待命·<工作区>」里那半个工作区还是占位符（控制台生成报到词时不知道目录，
# agent 也没能从上下文里填出来）。这类名字在侧栏里一排全一样，等真工作区落地
# 后必须补正——见 Hub.heal_standby_tab_name。
_PLACEHOLDER_WS = {"workspace", "工作区目录名", "<工作区目录名>", "工作区", "未知"}


def standby_name_is_placeholder(name):
    """tab 名是不是「待命·<还没填上的工作区>」（含去重后缀的各种变体）。"""
    parts = str(name or "").strip().split("·")
    if len(parts) < 2 or parts[0] != "待命":
        return False
    ws = re.sub(r"\d+$", "", parts[1]).strip().lower()
    return ws in _PLACEHOLDER_WS or not ws


# Cursor 新对话首条消息的长度硬顶按 UTF-8 字节计：08-31 用户截图
# 「failed_precondition 输入过长，请精简后重试（1.38K/1.00K）」——1410/1024=1.377
# 恰是当时兜底版报到词的 UTF-8 字节数，上限即 1024B（1.00K）；同晨一条 763B 的
# 旧版报到词发送成功，两点互证。08-28 那次「提示词过长 780/768 错误码 1003」
# 还按字符计——口径变过一次。老闸 CURSOR_FIRST_MSG_MAX=768 按字符计，中文
# 一字 3B，最多放行 2304B，这就是 08-31 全变体（1089~1479B）一起被拒的原因。
# 预算 1000B，留 24B 余量防口径小幅漂移。
CURSOR_FIRST_MSG_MAX_UTF8 = 1000


def _utf8_len(s):
    return len(str(s or "").encode("utf-8"))


# 报到词兜底版「怎么填占位符」的教程尾巴。每个词都是事故换来的：照抄上下文
# （08-26 三趟乱码终端）、填「workspace」+不传+探测（08-27 子代理壳没上下文，
# 白耗四十多秒）、禁止+终端/列目录/搜索（08-27 二补：只点名终端就绕道列目录）。
CHECKIN_FILL_HINT = (
    "占位符照抄上下文里的 workspace 路径；没有或拿不准就填「workspace」、"
    "project_path 不传，后台会探测。禁止动任何工具查/核路径（终端、列目录、搜索都算）。")


def fill_checkin_prompt(tpl, conversation_id, cwd=""):
    """把报到提示词里的占位符换成实打实的值，并保证全文过 UTF-8 字节闸。

    控制台生成这句话时本来就知道 cwd——它是「+」那一步的入参，此前却只拿去写
    mcp.json，从没填进提示词。于是发下去的是 task_name=「待命·<工作区目录名>」、
    project_path=工作区完整路径 这种半成品，而同一句话又写着「不思考、不读文件」
    「点开始任务前禁止 Read/Grep」——agent 手里就只剩 shell 能用了。08-26 用户实测
    「第一次报到要思考这么久」，那段时间全花在这上面：

        pwd && basename "$(pwd)"     PowerShell 不认 &&，InvalidEndOfLine
        (Get-Location).Path          中文路径吐成乱码
        第三趟才拿到                  每趟几十秒

    cwd 为空（「+ → 复制」没选目录，实测这是最常用的一条路）时退回说明式
    占位符，但必须把「怎么填」一并写死：工作区路径 Cursor 开场就写在 agent
    的上下文里，照抄即可，绝不许跑终端查——不写这句它就去 pwd/Get-Location，
    正是上面那三趟乱码往返。

    08-27 补：子代理壳（hh-wait 一类）的上下文里根本没有 workspace 路径，
    「照抄上下文」对它是死路，实测它只好连跑两趟慢终端（pwd 空输出 + echo
    才拿到，白耗四十多秒）。兜底必须再给一条合法出路：占位符一律填
    「workspace」、project_path 不传，直接报到——工程侧本来就兜得住：
    zhi 对空 project_path 走 MCP 归属探测，「待命·workspace」也匹配
    CHECKIN_SHELL_NAME_RE（11:44 壳 8653eee8 就这样被正确归并派活）。

    08-27 二补（壳 22a030d6，用户截图）：禁令只点名「终端」、理由只写「乱码」，
    模型就有缝可钻——它先列目录核实路径（listing 超时白等 21 秒），再
    printf|base64 专治乱码接着查，前后两分多钟才报到。要禁的是「查/核路径」
    这个动作本身，列目录、搜索一并点名；配上「拿不准就填 workspace」这条
    出路，绕的动机才真正消失。

    08-28 三补：{mcp_name} 换成 Cursor 工具表里的真名（registered_mcp_name），
    首调即中、不再白烧一轮搜名；全文加 768 字长度闸——带真路径的版本会随
    路径变长，超顶就整体退回占位符版（当时认定占位符版长度固定、必然合规）。

    08-31 四补（用户截图「failed_precondition 输入过长 1.38K/1.00K」）：上限
    口径从「768 字符」变成「1024 UTF-8 字节」，中文一字 3B，老字符闸完全失防；
    且三补那个「占位符版必然合规」的假设被打破——尾巴/改名行/逃生口逐次加码后
    兜底版长到 1410B，比填充版还长，全变体一起被拒、+号根本接不进来。改法：
    所有变体（含兜底版）统一过字节闸；超预算按「改名行 → 逃生口」顺序让位；
    地板（模板+填法教程）无条件发。
    """
    out = str(tpl or "").replace("{conversation_id}", str(conversation_id or ""))
    if "真调zhi" not in out:
        out = CHECKIN_REAL_TOOL_HINT + out
    try:
        port = (HUB.cfg or {}).get("mcp_http_port") if HUB else None
    except Exception:
        port = None
    real = registered_mcp_name(port)
    escape = MCP_NAME_ESCAPE
    if "{mcp_name}" in out:
        out = out.replace("{mcp_name}", real or "rxyy MCP")
        if real:
            escape = MCP_NAME_ESCAPE_KNOWN

    def _pick(base, rename):
        # 超预算逐段让位：改名行纯锦上添花先弃；逃生口是调名失败时唯一活路，
        # 再超才弃；地板无条件发——发得出去的短版永远好过发不出去的全版。
        for cand in (base + rename + escape, base + escape, base):
            if _utf8_len(cand) <= CURSOR_FIRST_MSG_MAX_UTF8:
                return cand
        return base

    cwd = str(cwd or "").strip().strip('"')
    name = os.path.basename(cwd.rstrip("\\/"))

    def _placeholder_variant():
        # 不知道目录时，改名那句里的工作区也走占位符——和 task_name/message 用
        # 同一个记号，agent 一次替换、三处一致。写死 workspace 的话，手上有真
        # 路径的主对话会把控制台改成「待命·cursor工作流」、侧栏改成
        # 「待命·workspace·d704」，两边分家。
        # message 里「（{workspace}）」那截纯展示（路径另有 project_path 参数），
        # 兜底版整段摘掉省 29B；task_name/message 的目录名占位符照留。
        rn = CHECKIN_RENAME_LINE.format(
            checkin_chat_title(conversation_id, ws_name="<工作区目录名>"))
        base = (out.replace("（{workspace}）", "")
                   .replace("{workspace_name}", "<工作区目录名>")
                   .replace("{workspace}", "<工作区完整路径>")
                + CHECKIN_FILL_HINT)
        return _pick(base, rn)

    if not (cwd and name):
        return _placeholder_variant()
    # 改名那句附在模板之外（和逃生口同理）：用户在设置面板改过 new_chat_prompt
    # 的话，模板里内嵌的任何新条款都不会生效。
    rename = CHECKIN_RENAME_LINE.format(checkin_chat_title(conversation_id, cwd))
    filled = out
    # 用户在设置面板改过模板时，config.json 里存的是老写法。只认 {} 新写法的话，
    # 这个修复对改过模板的人等于不存在——两种写法一并认掉。
    for old, new in (("{workspace_name}", name),
                     ("{workspace}", cwd),
                     ("project_path=工作区完整路径", "project_path=「{}」".format(cwd)),
                     ("<工作区完整路径>", cwd),
                     ("<完整路径>", cwd),
                     ("<工作区目录名>", name),
                     ("<项目名>", name)):
        filled = filled.replace(old, new)
    # 真路径优先保（08-26 教训：占位符=agent 跑三趟乱码终端）：超预算先让
    # 改名行、再让逃生口，都让完还装不下（路径本身太长）才整体退占位符版。
    full = _pick(filled, rename)
    if _utf8_len(full) <= CURSOR_FIRST_MSG_MAX_UTF8:
        return full
    return _placeholder_variant()


def _utf8_trim(s, budget):
    """按 UTF-8 字节截字符串，不切半个字；截过的结尾补「…」。"""
    s = str(s or "")
    if _utf8_len(s) <= budget:
        return s
    out, used, tail = [], 0, _utf8_len("…")
    for ch in s:
        n = len(ch.encode("utf-8"))
        if used + n + tail > budget:
            break
        out.append(ch)
        used += n
    return "".join(out) + "…"


def append_spawn_extras(prompt, task_name="", first_task=""):
    """「+ 直拉本机 agent」在报到词后追加的 task_name/首任务两行同吃字节顶。

    旧代码在长度闸之后裸拼这两行，报到词刚过闸、追加完又超（08-31 同类路径
    排查补上）。task_name 行短且决定 tab 归属，优先保；首任务行装不下就按
    字节截——全文另有预建壳队列兜底（见 hub_api.sdk_spawn），队列失败时截断版
    至少能开工；连一句都塞不下就整行不加，别为兜底把整条报到词噎死。
    """
    out = str(prompt or "")
    tn = str(task_name or "").strip()
    if tn:
        line = "\ntask_name 改用「{}」。".format(tn)
        if _utf8_len(out) + _utf8_len(line) <= CURSOR_FIRST_MSG_MAX_UTF8:
            out += line
    ft = str(first_task or "").strip()
    if ft:
        head = "\n报到后无需等待控制台指令，立即开始执行以下任务："
        room = CURSOR_FIRST_MSG_MAX_UTF8 - _utf8_len(out) - _utf8_len(head)
        if room >= 30:
            out += head + _utf8_trim(ft, room)
    return out

# agent 自报的 task_name 约定为「项目·功能」（如「rxyy tools·换装收尾」）。
# 项目那半是团队分组的唯一可靠来源：工作区目录当不了项目——同一个 cursor工作流
# 目录下并行着 rxyy tools、直播线、视频快编好几摊互不相干的活，按目录分组时
# 一条广播全中（08-07 用户原话「有的不是在干同一个项目」）。
_TASK_NAME_SEP_RE = re.compile(r"\s*[·:：/|]\s*|\s+-\s+")


def _project_of_task_name(name):
    """从「项目·功能」里取出项目那半；没写项目就返回空（不瞎猜）。"""
    parts = [x for x in _TASK_NAME_SEP_RE.split(str(name or "").strip()) if x]
    return parts[0][:24] if len(parts) >= 2 else ""


# ---------- 聊天记录 .md 反解（tab 被误关后，这个文件就是唯一的现场） ----------
_MD_HEAD_CONV = re.compile(r"^-\s*对话ID:\s*(\S+)\s*$", re.M)
_MD_SAID_CONV = re.compile(r"·\s*对话\s*([0-9A-Za-z_-]{4,32})\s*已就位")
_MD_ASKED_CONV = re.compile(r"conversation_id\s*[=:：]\s*[「『\"']?([0-9A-Za-z_-]{4,32})")
_MD_MSG_SPLIT = re.compile(r"\n(?=## (?:🤖 AI|🧑 [^\n]*?) · )")
_MD_MSG_HEAD = re.compile(r"## (🤖 AI|🧑 [^\n]*?) · ([^\n]+)\n+([\s\S]*)")


def parse_history_md(text):
    """把聊天记录 .md 反解成 {conv, name, cwd, created_at, msgs}。

    conversation_id 认三层，顺序要紧：头部字段 → AI 自己报出来的「· 对话 X 已就位」
    → 用户提示词里的 conversation_id=「X」。最后这层最不可信：用户粘的常是「本想让它
    用、但它没换过去」的 ID（7c6a4e74 的记录里就躺着一个从未生效的 dec3efdb）。
    """
    text = text or ""
    conv = ""
    m = _MD_HEAD_CONV.search(text)
    if m and m.group(1) != "__default__":
        conv = m.group(1)
    if not conv:
        said = _MD_SAID_CONV.findall(text)
        if said:
            conv = max(said, key=said.count)  # 报到句可能出现多次，认最常见的那个
    if not conv:
        m = _MD_ASKED_CONV.search(text)
        conv = m.group(1) if m else ""

    def head(label):
        m = re.search(r"^-\s*{}:\s*(.+)$".format(label), text, re.M)
        return m.group(1).strip() if m else ""

    name = head("会话")
    m = re.match(r"^(.*?)\s*\([0-9a-f]{6,12}\)$", name)  # 头部里带着会话内部 id，去掉
    if m:
        name = m.group(1).strip()
    msgs = []
    for part in _MD_MSG_SPLIT.split(text):
        m = _MD_MSG_HEAD.match(part.strip())
        if not m:
            continue
        body = re.sub(r"\n*（提供选项:.*?）\s*$", "", (m.group(3) or "").strip()).strip()
        if body:
            msgs.append(("ai" if "AI" in m.group(1) else "user",
                         (m.group(2) or "").strip(), body))
    return {"conv": conv, "name": name, "cwd": head("工作目录"),
            "created_at": head("开始时间"), "msgs": msgs}


def task_root_of(session):
    """会话归属的项目根目录（团队分组/席位/公告板一律按它）。

    不能用 cwd：cwd 记的是「最后一次报到的 agent 待在哪个工作区」。工作区2 的闲置
    agent 接手工作区1 挂掉的活时，cwd 会跟着搬到工作区2，于是这个 tab 从项目1 的
    团队里消失、凭空出现在项目2 里——席位、公告板、文件占用全跟着串台。
    """
    return getattr(session, "task_root", "") or getattr(session, "cwd", "") or ""


def affiliation_of(session):
    """团队面板「归属」下拉当前该显示的路径。

    没钉住（没手划过、也没有席位/成员登记）就跟窗口 cwd——08-21 心理两席
    窗口在 mh_admin_suite，task_root 却冻在建 tab 时的 cursor工作流，下拉
    看着像划错组。钉住了才显示 task_root（跨工作区接手的真分叉）。
    """
    if getattr(session, "task_root_locked", False):
        return task_root_of(session)
    return getattr(session, "cwd", "") or task_root_of(session)


def cross_workspace(session):
    """agent 实际所在工作区 ≠ 已钉住的任务归属：得让用户看见。

    没钉住时不算跨工作区：MCP 共享进程的 hello cwd 会抖、建 tab 时的
    task_root 也常跟当前窗口不是一处，黄条会误报「人在 XX」。只有用户
    手划过归属、或席位/成员把活钉在某个根上，分叉才是真的。
    """
    if not getattr(session, "task_root_locked", False):
        return False
    root = getattr(session, "task_root", "") or ""
    cwd = getattr(session, "cwd", "") or ""
    return bool(root and cwd and norm_root(root) != norm_root(cwd))


def norm_root(path):
    """项目根目录归一化：Windows 下同一个仓库常以不同大小写/斜杠出现在各 tab 的 cwd 里，
    不归一化就会把同一个项目拆成好几组。"""
    try:
        return os.path.normcase(os.path.normpath(str(path or "").strip()))
    except Exception:
        return str(path or "")


def is_runtime_ws_path(path):
    """这条路径是不是rxyy MCP自己住的地方，不是用户仓库。

    MCP 共享进程的 hello / os.getcwd() 经常是
    %LOCALAPPDATA%\\rxyy-tools-community\\live\\rxyy MCP 或 dist\\rxyy-tools-community\\_internal\\rxyy MCP。
    把它写进会话 cwd，重启后所有 tab 顶栏都会变成「人在rxyy MCP」。
    源码树里的 rxyy MCP 目录是真仓库，不算运行时。
    """
    p = str(path or "").strip()
    if not p:
        return False
    try:
        parts = [x.lower() for x in re.split(r"[\\/]+", os.path.normpath(p)) if x]
    except Exception:
        parts = [x.lower() for x in re.split(r"[\\/]+", p) if x]
    for i, part in enumerate(parts):
        if part != "rxyy-tools-community" or i + 1 >= len(parts):
            continue
        nxt = parts[i + 1]
        if nxt in ("live", "_internal"):
            return True
    return False


def scrub_runtime_team_cfg(cfg, bulletin=None):
    """把运行时目录从席位/公告/项目桶里抠掉，空「rxyy MCP」组才不会在历史里复活。"""
    changed = False
    if not isinstance(cfg, dict):
        return False
    for key in ("team_seats", "team_boards", "team_projects",
                "team_project_members", "team_tracks"):
        bucket = cfg.get(key)
        if not isinstance(bucket, dict):
            continue
        for root in [item for item in list(bucket) if is_runtime_ws_path(item)]:
            bucket.pop(root, None)
            changed = True
    if isinstance(bulletin, dict):
        # 下划线开头的是保留桶（_scopes 旧项目作用域 / _projects 跨工作区项目流），
        # 不是工作区根，别当运行时路径抠掉
        for root in [item for item in list(bulletin)
                     if not str(item).startswith("_") and is_runtime_ws_path(item)]:
            bulletin.pop(root, None)
            changed = True
    return changed


def sanitize_ws_path(path):
    """把「打包产物 / 常驻区」的假工作区路径清掉，不让它冒充用户仓库。

    打包版 MCP 的 cwd 是 dist\\rxyy-tools-community\\_internal\\rxyy MCP：剥到 dist 的父目录
    （07-31：否则团队面板凭空多一个「rxyy MCP」组）。常驻区
    %LOCALAPPDATA%\\rxyy-tools-community\\live\\rxyy MCP 没有可剥回的用户仓库，返回空，
    调用方必须保留会话上一次的真 cwd，不许用运行时目录覆盖。
    """
    p = str(path or "").strip()
    if not p:
        return p
    parts = re.split(r"[\\/]+", p)
    low = [x.lower() for x in parts]
    for i in range(len(low) - 1):
        if low[i] == "dist" and low[i + 1] == "rxyy-tools-community" and i > 0:
            p = os.sep.join(parts[:i]).rstrip(os.sep)
            break
    if is_runtime_ws_path(p):
        return ""
    return p


def heal_session_paths(session):
    """把已经写进会话的运行时假路径折回上一次真工作区。"""
    cwd = getattr(session, "cwd", "") or ""
    root = getattr(session, "task_root", "") or ""
    if (not cwd or is_runtime_ws_path(cwd)) and root and not is_runtime_ws_path(root):
        session.cwd = root
        cwd = root
    if is_runtime_ws_path(getattr(session, "task_root", "") or ""):
        keep = cwd if cwd and not is_runtime_ws_path(cwd) else ""
        session.task_root = keep
    return session


def apply_session_cwd(session, incoming):
    """只接受用户仓库路径。运行时/空路径不得覆盖已有 cwd。"""
    cleaned = sanitize_ws_path(incoming or "")
    if not cleaned:
        heal_session_paths(session)
        return False
    if getattr(session, "cwd", "") != cleaned:
        session.cwd = cleaned
        session.rev = int(getattr(session, "rev", 0) or 0) + 1
        return True
    return False


def heal_named_agent_root(session):
    """自报了业务项目时，席位不得把 task_root 钉在别人的仓库。"""
    named = str(getattr(session, "agent_project", "") or "").strip()
    if not named:
        return False
    try:
        key = Api._project_key(named)
    except Exception:
        key = ""
    if not key or key == getattr(Api, "TEAM_DEFAULT_PROJECT", "__workspace__"):
        return False
    cwd = sanitize_ws_path(getattr(session, "cwd", "") or "")
    if not cwd:
        return False
    registered = ""
    try:
        registered = HUB._registered_team_root(getattr(session, "conv_key", "") or "")
    except Exception:
        registered = ""
    if registered and norm_root(registered) == norm_root(getattr(session, "task_root", "") or ""):
        session.task_root = cwd
        session.task_root_locked = False
        return True
    return False


def _qr_svg_data_uri(text):
    """把文本生成二维码 SVG，返回 data URI（前端 <img src> 直接用）。segno 纯 Python 无依赖。"""
    try:
        import base64
        import io
        import segno
        buf = io.BytesIO()
        segno.make(text, error="m").save(buf, kind="svg", scale=5, border=2, dark="#e5e7eb", light="#101014")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return "data:image/svg+xml;base64," + b64
    except Exception:
        return ""


def build_img_payload(images):
    """把前端传来的 dataURL 图片转换为发回 AI 的 base64 载荷"""
    out = []
    for item in images or []:
        data = item.get("data", "")
        if "," in data and data.startswith("data:"):
            head, b64 = data.split(",", 1)
            media = head.split(";")[0][5:] or "image/png"
        else:
            b64, media = data, item.get("media_type", "image/png")
        out.append({"data": b64, "media_type": media, "filename": item.get("filename")})
    return out


def build_file_payload(files):
    """把前端传来的任意文件（dataURL/base64）转为发回 MCP 的载荷；MCP 端会落盘"""
    out = []
    for item in files or []:
        data = item.get("data", "")
        if "," in data and data.startswith("data:"):
            data = data.split(",", 1)[1]
        name = os.path.basename(str(item.get("name") or "文件.bin")) or "文件.bin"
        out.append({"name": name, "data": data})
    return out


# ---------- 开机自启（HKCU Run，无需管理员权限） ----------
AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = live_runtime.RUN_HUB
AUTOSTART_WATCHDOG_NAME = live_runtime.RUN_WATCHDOG
LEGACY_AUTOSTART_NAMES = (live_runtime.LEGACY_RUN_HUB,
                          live_runtime.LEGACY_RUN_WATCHDOG)


def _hub_launch_command():
    cmds = live_runtime.autostart_commands(APP_DIR)
    if cmds:
        return cmds[0]
    from frozen_boot import quoted_argv, spawn_argv
    return quoted_argv(spawn_argv(APP_DIR / "hub.py", "--daemon", "--autostart"))


def _watchdog_launch_command():
    cmds = live_runtime.autostart_commands(APP_DIR)
    if cmds:
        return cmds[1]
    from frozen_boot import quoted_argv, spawn_argv
    return quoted_argv(spawn_argv(APP_DIR / "watchdog.py"))


def autostart_get():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as k:
            for name in (AUTOSTART_NAME, live_runtime.LEGACY_RUN_HUB):
                try:
                    winreg.QueryValueEx(k, name)
                    return True
                except OSError:
                    pass
        return False
    except OSError:
        return False


def autostart_set(enabled):
    """写/删 HKCU Run 的两个条目：hub 本体 + 独立看门狗。
    看门狗单独注册的意义（07-27 事故）：登录时若 hub 在首条日志前就冻死，
    没有看门狗就没人清残留、没人守护 MCP 端口，agent 全灭直到用户手动介入；
    看门狗自带端口单例守卫，与 hub 各自拉起互不冲突。"""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0,
                        winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, AUTOSTART_NAME, 0, winreg.REG_SZ, _hub_launch_command())
            winreg.SetValueEx(k, AUTOSTART_WATCHDOG_NAME, 0, winreg.REG_SZ,
                              _watchdog_launch_command())
            for name in LEGACY_AUTOSTART_NAMES:
                try:
                    winreg.DeleteValue(k, name)
                except FileNotFoundError:
                    pass
        else:
            for name in (AUTOSTART_NAME, AUTOSTART_WATCHDOG_NAME,
                         *LEGACY_AUTOSTART_NAMES):
                try:
                    winreg.DeleteValue(k, name)
                except FileNotFoundError:
                    pass


def _project_mcp_json_paths():
    """已知工作区里已经写下的项目级 .cursor/mcp.json（叫醒时要一起碰 nonce）。"""
    roots, paths, seen = [], [], set()
    try:
        for s in list(HUB.sessions.values()):
            for attr in ("cwd", "task_root"):
                v = getattr(s, attr, None)
                if v:
                    roots.append(v)
    except Exception:
        pass
    try:
        for info in (HUB.cfg.get("team_project_members") or {}).values():
            if isinstance(info, dict) and info.get("root"):
                roots.append(info["root"])
    except Exception:
        pass
    for r in roots:
        try:
            p = Path(r) / ".cursor" / "mcp.json"
            if not p.is_file():
                continue
            key = str(p.resolve())
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        paths.append(p)
    return paths


def touch_mcp_json():
    """触碰全局 + 项目级 mcp.json 的rxyy MCP nonce，诱导 Cursor 重新 initialize。
    返回 True=至少一处成功。实现统一收口到 mcp_touch（带重试与失败原因）——
    此前 Cursor 恰好占着文件时会瞬时失败并弹误导性红条（07-27 15:10 实测）。"""
    from mcp_touch import touch_mcp_json as _touch, touch_mcp_json_at
    ok, reason = _touch()
    extra = 0
    for p in _project_mcp_json_paths():
        o, _ = touch_mcp_json_at(p, retries=2, delay=0.1)
        if o:
            extra += 1
    if ok or extra:
        if not ok:
            log_event("全局 mcp.json 触碰失败（{}），已改 {} 个项目级条目".format(
                reason, extra))
        return True
    log_event("触碰 mcp.json 失败: {}".format(reason))
    HUB_LAST_TOUCH_ERROR["reason"] = reason
    return False


HUB_LAST_TOUCH_ERROR = {"reason": ""}


# agentboard 信号读取已抽到 session_core.py（第四刀）：这里保留别名，
# hub.read_agentboard_signals / hub._AGENTBOARD_SIG_CACHE 的既有引用与测试
# 打桩（含 .clear()——别名指向同一个 dict 对象）照旧成立。
# 注意 import 时序：session_core 顶层 `import hub`，直跑 hub.py（__main__）时
# 必须先把本模块登记成 "hub" 再 import 它（与文件尾部 hub_api 同一治法），
# 否则 hub.py 会被再执行一遍、造出第二个 HUB 单例。
if "hub" not in sys.modules:
    sys.modules["hub"] = sys.modules[__name__]
import session_core  # noqa: E402
read_agentboard_signals = session_core.read_agentboard_signals
_AGENTBOARD_SIG_CACHE = session_core._AGENTBOARD_SIG_CACHE
AGENTBOARD_SIG_TTL = session_core.AGENTBOARD_SIG_TTL


CLEAN_EXIT_MARK = APP_DIR / ".hub-clean-exit"


def port_owner_pid(port):
    """netstat 找 LISTENING 在指定端口上的 pid；找不到返回 None。"""
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, timeout=10,
            creationflags=0x08000000).stdout
        needle = ":%d" % int(port)
        for ln in out.splitlines():
            parts = ln.split()
            if len(parts) >= 5 and parts[0] == "TCP" and parts[3] == "LISTENING":
                if parts[1].endswith(needle):
                    return int(parts[4])
    except Exception:
        pass
    return None


def taskkill_pid(pid):
    """强杀单个 pid。绝不加 /T：hub 的子进程里有 MCP 守护进程和看门狗。"""
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=10, creationflags=0x08000000)
        return True
    except Exception:
        return False


def gateway_alive(gport, timeout=3):
    """探既有实例的网关 /api/ping：Python 层活着才回得来；整进程冻结 = 超时。"""
    try:
        from urllib import request as _urlreq
        req = _urlreq.Request(
            "http://127.0.0.1:%d/api/ping" % int(gport),
            data=b"[]", headers={"Content-Type": "application/json"}, method="POST")
        with _urlreq.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def spawn_detached(script, *args):
    """拉起本目录脚本（无窗口、脱离进程组）。stderr 统一落 spawn-stderr.log——
    07-27 事故：DEVNULL 让 7 次启动冻死死无对证，P0 可观测的核心一环。"""
    from frozen_boot import hidden_popen_kwargs, script_dir, spawn_argv
    try:
        from watchdog import spawn_stderr_handle
        err = spawn_stderr_handle(script)
    except Exception:
        err = subprocess.DEVNULL
    try:
        cmd = spawn_argv(APP_DIR / script, *args)
        subprocess.Popen(
            cmd, cwd=script_dir(cmd, APP_DIR),
            **hidden_popen_kwargs(stderr=err))
    finally:
        if err is not subprocess.DEVNULL:
            try:
                err.close()
            except OSError:
                pass


def ensure_watchdog():
    """Community service lifecycle is managed explicitly by its CLI."""
    return None


def _owner_heartbeat_loop():
    """持续刷新「本安装是正主」的时间戳（见 instance_owner）。

    心跳一停（hub 崩了/被杀），另一套安装超过 STALE_SECS 就能接管，
    崩溃自愈的兜底不受影响。"""
    while True:
        time.sleep(30)
        try:
            instance_owner.claim(APP_DIR)
        except Exception:
            pass


def cleanup_orphan_webview2_async():
    """后台清理孤儿 WebView2（父进程已死的残留渲染树）。
    07-27 15:06 事故：孤儿沼泽把新 hub 的启动拖到 2 分钟。此前清理只挂在
    看门狗重拉路径上，hub 自身启动/重启接力路径没人清——现在自己起来也清，
    与 GUI 初始化并行（只杀父进程已死的，绝不伤活进程，含自己刚拉的 WebView2）。"""
    def run():
        try:
            from watchdog import kill_orphan_webview2
            n = kill_orphan_webview2()
            if n:
                log_event("启动清扫：清理了 {} 个孤儿 WebView2 进程".format(n))
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


def wait_pid_exit(pid, timeout_secs=20):
    """等待指定进程退出（重启接力：新 hub 等旧 hub 释放端口后再绑定）"""
    try:
        import ctypes
        SYNCHRONIZE = 0x00100000
        h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if not h:
            return  # 进程已不存在
        try:
            ctypes.windll.kernel32.WaitForSingleObject(h, int(timeout_secs * 1000))
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        time.sleep(2)


class Client:
    """一条 MCP 进程 TCP 连接；同一连接可承载多个对话，每个对话一个 tab"""

    def __init__(self, sock, cwd, pid):
        self.sock = sock
        self.cwd = sanitize_ws_path(cwd or "")
        self.pid = pid
        self.peer_ip = None  # 局域网接入的 MCP 来源机器 IP；本机连接为 None
        self.send_lock = threading.Lock()
        self.sessions = {}      # conv_key -> Session
        self.closed_convs = {}  # conv_key -> True（用户已关闭，下次提问自动回"已关闭"）
        self.last_heartbeat = 0.0  # 连接级 MCP 心跳（区分「进程假死」与「单个对话闲置」）
        self.lock = threading.Lock()

    def send(self, obj):
        with self.send_lock:
            send_msg(self.sock, obj)


# Session 类已抽到 session_core.py（第四刀批C）：别名保证 hub.Session /
# patch("hub.Session") / hub.Session.__new__ 全部照旧成立
Session = session_core.Session


def running_under_test():
    """单测/一次性脚本：禁止出站推手机，也禁止报到时改本机 mcp.json。

    08-14 实锤：test_checkin_redelivery_dedup 的 `_alert` 走 daemon 线程，
    patch.stopall 之后才跑到真 push_phone。夹具 sid=t1、空令牌、局域网
    `192.168.0.104:39080` 连弹进生产 ntfy（12:51×5、14:38×5、15:19×7）。
    进程 argv 带 unittest/pytest/test_*.py，或显式 RXYY_MCP_UNDER_TEST=1，
    一律视为测试。常驻 hub（argv=hub.py --daemon）不受影响。
    """
    if os.environ.get("RXYY_MCP_UNDER_TEST") or os.environ.get("CHIJIU_UNDER_TEST"):
        return True
    argv = " ".join(sys.argv).lower()
    if "unittest" in argv or "pytest" in argv:
        return True
    if re.search(r"(^|[\\/\s])test_[^\\/\s]+\.py\b", argv):
        return True
    return False


def _outbound_push_blocked():
    return running_under_test()


def _is_hub_install_dir(root):
    """MCP 进程自己的安装目录，不能当成用户工作区去写项目级 mcp.json。"""
    if not root:
        return False
    try:
        p = Path(root).resolve()
    except OSError:
        return False
    try:
        if p == APP_DIR.resolve():
            return True
    except OSError:
        pass
    return (p.name in ("rxyy_mcp", "持久plus")
            and p.parent.name in ("_internal", "live"))


def maybe_ensure_project_mcp(root):
    """报到到达后给该工作区写项目级 URL。不碰全局 nonce，避免拆掉正在挂的 zhi。

    url 已对则不写盘。单测进程直接跳过，免得夹具 cwd 把仓库里的 mcp.json 改掉。
    """
    if running_under_test():
        return False, "under_test"
    if _is_hub_install_dir(root):
        return False, "install_dir"
    try:
        port = int((HUB.cfg.get("mcp_http_port") if HUB else None) or 39222)
    except Exception:
        port = 39222
    try:
        from mcp_touch import ensure_workspace_mcp
        return ensure_workspace_mcp(root, port=port)
    except Exception as e:
        return False, str(e)


class Hub:
    def __init__(self):
        self.cfg = load_config()
        self.config_error = CONFIG_LOAD_ERROR
        if not (self.cfg.get("share_token") or "").strip():
            self.cfg["share_token"] = uuid.uuid4().hex[:12]
            save_config(self.cfg)
        self.sessions = {}
        self.order = []
        # agent 互通传话的近况（团队面板「队内传话」区展示用；落盘跨重启保留——
        # 昨晚 hub 十几次重启，用户刚看到的传话区一重启就清空）
        self.relay_log = self._load_relays()
        # 接手台账（四账合并A步，2026-08-26）：一本账两个索引，落盘跨重启。
        # aliases——退休conv → {succ, old_name, succ_label, ts, why}：路由的权威答案。
        # 08-12 实测「接手方肯改 ID」必失败（常驻协议要求全程复用首个 ID），机制兜底；
        # names——被收起的壳名 → 现任是谁：按老名字转告时指路（07-31 实测：给
        # 「待命·105628d8」递话失败，其实壳已收、现任叫「渲染升级」）。08-26 前它
        # 只活在内存，hub 一重启按旧名指路全失效——并入台账后随盘活着。
        _book = self._load_takeover_book()
        self.takeover_ledger = _book["aliases"]
        self.name_tombstones = _book["names"]
        # 平表视图 {退休conv: 现任conv}：入站换名/转告路由/链压缩照旧读它，
        # 与台账同生同灭（写入只走 _retire_conv_into，两个视图一起动）
        self.takeover_aliases = {k: str(e.get("succ") or "")
                                 for k, e in self.takeover_ledger.items()
                                 if e.get("succ")}
        # 团队黑板：norm_root → [{ts,day,hms,from8,from_label,kind,text}]，落盘跨重启
        self.bulletin = self._load_bulletin()
        # 窗口总线无头开的对话：conv_key → {uuid, ts, instance}（见 _bind_prebound_composer）
        # 不落盘：对话没在一小时内报到就作废，重启后重开即可
        self.prebound_composers = {}
        self.lock = threading.Lock()
        # Api 每次请求都会新建实例；项目桶首次创建必须共用这一把锁，不能各自
        # copy-on-write 后把别的并发创建覆盖掉。
        self._team_project_lock = threading.RLock()
        self.window = None
        self.window_tracker = None
        self.share_server = None
        self._save_timer = None
        self._save_lock = threading.Lock()
        self.autostart_enabled = autostart_get()
        self._yield_burst_until = 0.0
        self._yield_burst_last = 0.0
        hist = Path(self.cfg["history_dir"])
        try:
            hist.mkdir(parents=True, exist_ok=True)
        except Exception:
            self.cfg["history_dir"] = str(DATA_DIR / "聊天记录")
            Path(self.cfg["history_dir"]).mkdir(parents=True, exist_ok=True)

    # ---------- 聊天记录落盘 ----------
    def _alloc_history_path(self, name, exclude_id=None):
        """按 tab 名生成记录文件路径：<tab名>.md；与其它会话或已存在文件冲突时加 (n)。"""
        d = Path(self.cfg["history_dir"])
        base = sanitize_filename(name)
        used = {x.file_path for sid, x in self.sessions.items()
                if sid != exclude_id and x.file_path}
        cand = d / f"{base}.md"
        if str(cand) not in used and not cand.exists():
            return str(cand)
        i = 2
        while True:
            cand = d / f"{base} ({i}).md"
            if str(cand) not in used and not cand.exists():
                return str(cand)
            i += 1

    def _rename_history_file(self, s: Session):
        """tab 改名后把记录文件一并改名，保持文件名与 tab 名一致。"""
        if not s.file_path:
            return
        new_path = self._alloc_history_path(s.name, exclude_id=s.id)
        if new_path == s.file_path:
            return
        try:
            if os.path.exists(s.file_path):
                os.replace(s.file_path, new_path)
            s.file_path = new_path
        except OSError:
            pass

    def _init_file(self, s: Session):
        s.file_path = self._alloc_history_path(s.name, exclude_id=s.id)
        # 对话ID 必须进头部：tab 被误关后会话就从内存里没了，只剩这个文件；
        # 没有它就只能去正文里猜 conversation_id，而正文里常混着「用户本想用、
        # 但没生效」的另一个 ID（实测 7c6a4e74 的记录里就躺着一个 dec3efdb）
        head = (
            f"# rxyy MCP 会话记录\n\n"
            f"- 会话: {s.name} ({s.id})\n"
            f"- 对话ID: {s.conv_key}\n"
            f"- 工作目录: {s.cwd or '未知'}\n"
            f"- 开始时间: {s.created_at}\n\n---\n"
        )
        self._append_file(s, head)
        self.cleanup_history_files()

    def _append_file(self, s: Session, text):
        try:
            new_file = not os.path.exists(s.file_path)
            with open(s.file_path, "a", encoding="utf-8") as f:
                f.write(text)
            if new_file:
                self.maybe_decrypt(s.file_path)  # 记录 .md 首次落盘后解密，git 才能存明文
        except Exception:
            pass

    def log_ai(self, s: Session, message, options, artifacts=None):
        block = f"\n## 🤖 AI · {now_full()}\n\n{message}\n"
        digest = artifacts_digest(artifacts)
        if digest:
            block += "\n（本次成果: {}）\n".format("；".join(digest))
        if options:
            block += "\n（提供选项: {}）\n".format(" | ".join(options))
        self._append_file(s, block)

    def log_user(self, s: Session, text, selected, img_count, source, who=None, file_names=None):
        label = f"🧑 {who}（局域网）" if who else "🧑 用户"
        block = f"\n## {label} · {now_full()}\n\n"
        if selected:
            block += "选择: {}\n\n".format("、".join(selected))
        if text:
            block += f"{text}\n"
        if img_count:
            block += f"\n（附带 {img_count} 张图片）\n"
        if file_names:
            block += "\n（附带文件: {}）\n".format("、".join(file_names))
        if source == "popup_continue":
            block += "（点击了「继续」按钮）\n"
        self._append_file(s, block)

    def log_end(self, s: Session, reason):
        self._append_file(s, f"\n---\n> 会话结束 · {now_full()} · {reason}\n")

    def hydrate_messages_from_file(self, s: Session):
        """内存里的对话被 max_messages 裁短时，用聊天记录 .md 把早期那截补回来。

        只往前补、绝不替换：内存里的气泡是渲染好的（Markdown、图片、选项），而 .md
        里只有纯文本，拿它换掉活气泡等于把已经好好显示的消息打回原形。文件天然是
        内存的超集（每条消息落盘在前、裁剪在后），所以「文件比内存多出来的那一截」
        就是被裁掉的早期对话，补在前面即可。

        已 hydrate 过且文件 mtime 未变则跳过。"""
        if not s or not s.file_path or not os.path.exists(s.file_path):
            return False
        try:
            mtime = os.path.getmtime(s.file_path)
        except OSError:
            return False
        if getattr(s, "_hydrated_mtime", None) == mtime and getattr(s, "_hydrated_ok", False):
            return False
        try:
            raw = Path(s.file_path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return False
        # ## 🤖 AI · ts  /  ## 🧑 用户 · ts  /  ## 🧑 某某（局域网） · ts
        # 局域网回复必须一起认：只认「🧑 用户」的话，从手机/分享页回的消息在回填后
        # 会整片消失，恢复出来的对话看着像 AI 在自言自语
        parts = _MD_MSG_SPLIT.split(raw)
        loaded = []
        for part in parts:
            m = _MD_MSG_HEAD.match(part.strip())
            if not m:
                continue
            role = "ai" if "AI" in m.group(1) else "user"
            ts = (m.group(2) or "").strip()
            body = (m.group(3) or "").strip()
            # 去掉尾部「（提供选项: …）」行，选项不进气泡正文
            body = re.sub(r"\n*（提供选项:.*?）\s*$", "", body).strip()
            if not body:
                continue
            # 截掉过长单条，避免一次回填把 webview 撑爆
            if len(body) > 12000:
                body = body[:12000] + "\n\n…〔此条已截断〕"
            html = "<div class='md'>{}</div>".format(
                html_mod.escape(body).replace("\n", "<br>"))
            loaded.append({"role": role, "ts": ts, "html": html, "from_file": True})
        if not loaded:
            s._hydrated_mtime = mtime
            s._hydrated_ok = True
            return False
        with s.lock:
            live = list(s.messages)
            convo = sum(1 for m in live if m.get("role") in ("ai", "user"))
            missing = len(loaded) - convo
            if missing <= 0:
                s._hydrated_mtime = mtime
                s._hydrated_ok = True
                return False  # 内存不比文件少，没有可补的
            cap = max(40, int(self.cfg.get("max_messages", 200) or 200))
            s.messages = (loaded[:missing] + live)[-cap:]
            s.rev += 1
            s._hydrated_mtime = mtime
            s._hydrated_ok = True
        return True

    IMG_EXT = {"image/png": ".png", "image/jpeg": ".jpg",
               "image/gif": ".gif", "image/webp": ".webp"}

    def save_msg_images(self, img_payload):
        """把用户发给 AI 的图片存到记录目录（按日期分文件夹），供控制台缩略图预览。
        返回文件路径列表；失败的图片跳过（消息仍按 img_count 显示占位）。"""
        out = []
        if not img_payload:
            return out
        import base64
        d = Path(self.cfg["history_dir"]) / "图片" / time.strftime("%Y%m%d")
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            return out
        for item in img_payload:
            try:
                raw = base64.b64decode(item.get("data") or "")
                ext = self.IMG_EXT.get(item.get("media_type"), ".png")
                p = d / "{}-{}{}".format(time.strftime("%H%M%S"), uuid.uuid4().hex[:6], ext)
                p.write_bytes(raw)
                out.append(str(p))
            except Exception:
                continue
        return out
        # 注：图片扩展名(.png/.jpg)不在 TSD 监控名单，无需解密

    def maybe_decrypt(self, path):
        """本机若装了 IPGuard/TSD，Python 写盘的文件会被透明加密，certutil/git
        等非授信进程读到密文。落盘后原地解密一次，保证任何进程都读得到明文。"""
        if tsd_decrypt is None:
            return
        try:
            tsd_decrypt.decrypt_file(path)
        except Exception:
            pass

    def history_keep_days(self):
        """记录/图片保留天数（含当天）。0 = 不按天清理，只受份数上限约束。"""
        try:
            return max(0, int(self.cfg.get("history_keep_days",
                                           DEFAULTS["history_keep_days"])))
        except (TypeError, ValueError):
            return DEFAULTS["history_keep_days"]

    def cleanup_history_files(self):
        """清理策略：1) 删除保留天数之外的记录 2) 剩下的超过份数上限时删最旧

        活跃会话（含断线待接手的）的记录文件一律豁免——07-28 事故：中断会话的记录
        mtime 停在昨天，早晨新会话触发跨天清理把它删了，随后生成的接手提示词
        指向已不存在的 .md，接手 agent 第一步读文件就扑空。"""
        keep_days = self.history_keep_days()
        try:
            live = set()
            for x in list(self.sessions.values()):
                if x.file_path:
                    try:
                        live.add(str(Path(x.file_path).resolve()))
                    except OSError:
                        live.add(str(x.file_path))
            cutoff = datetime.date.today() - datetime.timedelta(days=max(keep_days, 1) - 1)
            files = sorted(
                Path(self.cfg["history_dir"]).glob("*.md"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            kept = []
            for p in files:
                try:
                    if str(p.resolve()) in live:
                        continue  # 活会话的记录：不清理、也不占当日保留份额
                except OSError:
                    pass
                fdate = None
                m = re.match(r"^(\d{8})-", p.name)
                if m:
                    try:
                        fdate = datetime.datetime.strptime(m.group(1), "%Y%m%d").date()
                    except ValueError:
                        pass
                if fdate is None:
                    fdate = datetime.date.fromtimestamp(p.stat().st_mtime)
                if keep_days and fdate < cutoff:
                    p.unlink(missing_ok=True)
                else:
                    kept.append(p)
            for p in kept[int(self.cfg.get("max_history_files", 200) or 200):]:
                p.unlink(missing_ok=True)
        except Exception:
            pass
        # 消息图片：与记录同一套保留天数
        try:
            import shutil
            if not keep_days:
                return
            cutoff = datetime.date.today() - datetime.timedelta(days=keep_days - 1)
            img_root = Path(self.cfg["history_dir"]) / "图片"
            if img_root.is_dir():
                for d in img_root.iterdir():
                    if not d.is_dir() or not re.match(r"^\d{8}$", d.name):
                        continue
                    try:
                        if datetime.datetime.strptime(d.name, "%Y%m%d").date() < cutoff:
                            shutil.rmtree(d, ignore_errors=True)
                    except ValueError:
                        continue
        except Exception:
            pass

    def daily_cleanup_loop(self):
        """每小时检查一次，清掉超出保留天数/份数上限的记录"""
        while True:
            time.sleep(3600)
            self.cleanup_history_files()

    def schedule_save(self):
        """防抖保存配置（窗口拖动会高频触发 resize，避免频繁写盘）"""
        with self._save_lock:
            if self._save_timer:
                self._save_timer.cancel()
            self._save_timer = threading.Timer(0.8, lambda: save_config(self.cfg))
            self._save_timer.daemon = True
            self._save_timer.start()

    # ---------- 配置热重载 ----------
    HOT_KEYS = ("keepalive_secs", "keepalive_first_secs", "detach_grace_secs", "processing_timeout_secs",
                "yield_on_burst", "mcp_instructions_full", "library_autosend",
                "max_messages", "max_history_files", "continue_prompt", "quick_phrases",
                "remote_base_url", "bark_url", "push_enabled", "history_keep_days",
                "team_roles", "team_boards", "team_projects", "team_project_members",
                "title_sync_secs", "quiet_mode", "new_chat_prompt",
                "sidebar_auto_rename", "sidebar_auto_rename_idle_secs",
                "reconnect_grace_secs", "ide_active_secs", "token_freeze",
                "auto_reload_on_stuck", "stuck_threshold_secs", "auto_reload_cooldown_secs",
                "auto_reload_max_collateral", "auto_reload_max_per_hour")

    def config_watch_loop(self):
        """监听 config.json 外部改动，热更新可安全在线调整的项（端口/绑定除外）。"""
        try:
            last = CONFIG_PATH.stat().st_mtime
        except OSError:
            last = 0
        while True:
            time.sleep(2)
            try:
                m = CONFIG_PATH.stat().st_mtime
            except OSError:
                continue
            if m == last:
                continue
            last = m
            try:
                disk = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                self.config_error = ""
            except Exception as e:
                # 手改 config.json 写坏时在界面亮出横幅，而不是静默沿用旧值
                self.config_error = "config.json 解析失败（改动未生效）: {}".format(e)
                continue
            changed = []
            for k in self.HOT_KEYS:
                if k in disk and disk[k] != self.cfg.get(k):
                    self.cfg[k] = disk[k]
                    changed.append(k)
            if changed:
                log_event("配置热重载: {}".format("、".join(changed)))
                # 广播给控制台一条轻提示（首个会话即可）
                for s in list(self.sessions.values()):
                    self.add_message(s, {
                        "role": "sys", "ts": now_hms(),
                        "html": "配置已热重载: {}".format("、".join(changed)),
                    })
                    break

    # ---------- Cursor 标题同步 ----------
    def title_sync_loop(self):
        """周期把 tab 名同步成 Cursor 会话标题，控制台与 Cursor 侧边栏从此同名。"""
        while True:
            try:
                secs = int(self.cfg.get("title_sync_secs", 30) or 0)
            except Exception:
                secs = 30
            time.sleep(max(5, secs) if secs > 0 else 60)
            if secs <= 0:
                continue
            try:
                self.sync_cursor_titles()
            except Exception:
                pass

    def sync_cursor_titles(self):
        with self.lock:
            sessions = [self.sessions[sid] for sid in self.order if sid in self.sessions]
        claimed = {s.cursor_uuid for s in sessions if s.cursor_uuid}
        for s in sessions:
            if runtime_adapter.is_native(s):
                continue
            if s.conv_key == "__default__":
                continue
            if s.cursor_uuid is None:
                # 断开的 tab 也定位：transcript 路径是「IDE 活跃」蓝点判定的依据
                located = locate_cursor_session(s.cwd, s.conv_key, s.created_ts,
                                                exclude=claimed)
                if not located:
                    continue
                s.cursor_uuid, s.transcript_path = located
                claimed.add(s.cursor_uuid)
            else:
                self._relocate_if_stale(s, claimed)
            if self._is_checkin_shellish(s):
                # 还在待命、没干过活的报到壳：它那个 Cursor 对话里只有报到提示词，
                # Cursor 据此生成的标题千奇百怪（08-07 实测「Persistent task
                # reporting」「Cursor extension installation」），_title_is_boilerplate
                # 那张关键词表堵不全。而壳名一旦被冲掉，接手落地就再也收不掉它。
                # 壳本来就该保持「待命·xxx」直到被派活/被接手，这里一概不同步。
                # 往外写也不给：壳的 uuid 是靠「同工作区 + 出生时间」猜的，它自己
                # 没有任何可辨识的动静，猜错概率在所有 tab 里最高——猜错还往外写，
                # 就是把别人正在干活的那个对话改名叫「待命·xxx」。
                #
                # 但控制台这一侧的名字要补正：壳报到时可能连自己在哪个工作区都
                # 说不出（「+ → 复制」没选目录 + 子代理上下文里没有路径），
                # 于是一排全叫「待命·workspace」。刚刚定位到的 transcript 正好
                # 指认了它所在的工作区槽位，这里是唯一能拿到答案的地方。
                self.heal_standby_tab_name(s)
                continue
            if s.name_locked or getattr(s, "agent_named", False):
                # 名字是人锁的、或 agent 自己报的：两种都比 Cursor 按对话开头猜出来的
                # 标题准得多（08-07 实测就是同步把一个自报过名字的 tab 冲成了英文的
                # 「Persistent task reporting」）。这一档不读进来，改成往外写——
                # 两个方向按这个条件互斥，不会打架。
                #
                # name_locked 原先在上面单独一道 `continue`，把往外写也一并挡了：
                # 那道闸本意只是「别拿 Cursor 标题冲掉锁死的名字」，结果用户亲手
                # 锁的名字反倒是全场唯一一个永远同步不出去的（08-25 现场：`机构管理端`）。
                self._push_name_to_cursor(s)
                continue
            title = (read_cursor_title(s.cursor_uuid) or "").strip()[:40]
            if not title or title == s.cursor_title:
                continue  # 没标题 / 上次已同步过同名，防抖
            if self._title_is_boilerplate(title):
                continue  # Cursor 自动标题=报到提示词截断，同步过来毫无辨识度且很丑
            if title == s.name:
                s.cursor_title = title
                continue
            old = s.name
            Api()._remember_label(s)  # 老名字留档，队友按旧名字转告还找得到
            with self.lock:
                s.name = self._dedupe_name(title, exclude_id=s.id,
                                           conv_key=s.conv_key)
            s.cursor_title = title
            s.rev += 1
            if s.file_path:
                self._append_file(
                    s, f"\n> 标签改名（同步 Cursor 标题）: {old} → {s.name} · {now_full()}\n")
                self._rename_history_file(s)
        try:
            self._auto_fix_sidebar()
        except Exception as exc:  # noqa: BLE001
            log_event("侧栏自动改名异常（已忽略）: {!r}".format(exc))

    # 同一个 (composerId, 名字) 按过一次后多久内不再按：按下去 ≠ Cursor 认了，
    # 但每拍都按等于每 30 秒抢一次键盘；真没改成的等这个窗口过了再试
    SIDEBAR_RETRY_SECS = 600

    def _sidebar_autofix_ready(self):
        """这台机器能不能由控制台自己去改侧栏（开着 + 桥或键盘驱动）。"""
        if not self.cfg.get("sidebar_auto_rename", DEFAULTS.get("sidebar_auto_rename", True)):
            return False
        hook_on = wbhook is not None and wbhook.hook_alive()
        return hook_on or cursor_live_rename is not None

    def _sidebar_stale_rows(self):
        """这一拍里「写了名字又被 Cursor 盖回去」的 tab（_push_name_to_cursor 标的）。

        不在这里重读库：_push_name_to_cursor 刚把名字写进去，此刻读出来的就是它，
        看不出跑偏——跑偏只在写之前那一读里露头，所以信号由那里标在会话上。
        名字的闸门（agent 自报过 / 用户锁过、不是待命壳）在 _push_name_to_cursor
        进门处已经过了一遍，这里只再挡一次壳名以防万一。
        """
        rows = []
        with self.lock:
            sessions = list(self.sessions.values())
        for s in sessions:
            stale = getattr(s, "sidebar_stale", "") or ""
            uid = (getattr(s, "cursor_uuid", "") or "").strip()
            name = (getattr(s, "name", "") or "").strip()[:40]
            if not (stale and uid and name):
                continue
            if CHECKIN_SHELL_NAME_RE.match(name):
                continue
            rows.append({"composer_id": uid, "want": name, "shown": stale,
                         "cwd": getattr(s, "cwd", "") or ""})
        return rows

    def _auto_fix_sidebar(self):
        """标题同步循环每拍顺带：Cursor 侧栏还顶着自动标题的 tab，趁用户空闲改回来。

        只在两个条件同时成立时按键：用户 ≥ N 秒没碰键盘鼠标（不会敲进他正打的字里）
        且前台窗口是 Cursor（按键不会落到别的程序）。返回这一拍按下去的 composerId 集合。
        """
        if not self._sidebar_autofix_ready():
            return set()
        rows = self._sidebar_stale_rows()
        if not rows:
            return set()
        if wbhook is not None and wbhook.hook_alive():
            hooked = set(wbhook.rename_many(
                [(r["composer_id"], r["want"]) for r in rows], timeout=1.6) or ())
            if hooked:
                with self.lock:
                    sessions = list(self.sessions.values())
                for s in sessions:
                    if (getattr(s, "cursor_uuid", "") or "") in hooked:
                        s.sidebar_stale = ""
                log_event("侧栏自动改名（注入桥）：跑偏 {} 个，改了 {} 个".format(
                    len(rows), len(hooked)))
                rows = [r for r in rows if r["composer_id"] not in hooked]
                if not rows:
                    return hooked
            # 桥没改完的，下面仍走键盘兜底（要空闲+前台）
        if cursor_live_rename is None:
            return set()
        try:
            idle_need = float(self.cfg.get("sidebar_auto_rename_idle_secs",
                                           DEFAULTS.get("sidebar_auto_rename_idle_secs", 45)))
        except (TypeError, ValueError):
            idle_need = 45.0
        idle = cursor_live_rename.user_idle_secs()
        if idle is None or idle < idle_need:
            return set()
        if not cursor_live_rename.foreground_is_cursor():
            return set()
        now = time.time()
        tried = getattr(self, "_sidebar_tried", None)
        if tried is None:
            tried = self._sidebar_tried = {}
        for k, ts in list(tried.items()):
            if now - ts > self.SIDEBAR_RETRY_SECS:
                tried.pop(k, None)
        rows = [r for r in rows if (r["composer_id"], r["want"]) not in tried]
        if not rows:
            return set()
        for r in rows:
            tried[(r["composer_id"], r["want"])] = now
        hint = next((Path(r["cwd"]).name for r in rows if r.get("cwd")), "")
        done = set(cursor_live_rename.rename_chats_live(
            [(r["composer_id"], r["want"]) for r in rows],
            workspace_hint=hint, log=log_event) or ())
        if done:
            with self.lock:
                sessions = list(self.sessions.values())
            for s in sessions:
                if (getattr(s, "cursor_uuid", "") or "") in done:
                    s.sidebar_stale = ""  # 下一拍写之前那一读重新判断
        log_event("侧栏自动改名：跑偏 {} 个，按下去 {} 个（用户空闲 {:.0f}s）".format(
            len(rows), len(done), idle))
        return done

    # 窗口总线无头开出来的对话，composerId 是 hub 自己指定的：conv_key → uuid 先记在
    # 这里，agent 第一次 zhi/zt 到达时直接认领，不用扫库猜（ext_batch_open 写入）。
    # 一小时没人来认的条目作废——对话没开起来（createNew 失败、用户关了）就别留着
    # 等某天同一个 conv_key 复用时张冠李戴。
    PREBOUND_TTL_SECS = 3600

    def _bind_prebound_composer(self, s, now):
        """预绑定快路：hub 自己开的对话，uuid 是拍板过的事实，不是猜测。

        返回 True = 已据预绑定定下身份（或早已是同一个），调用方不必再扫库。
        被「认领了活」的会话持有的 uid 照旧不抢（08-12 铁律），此时放弃预绑定
        回到扫库老路。
        """
        table = getattr(self, "prebound_composers", None)
        if not table:
            return False
        pre = table.get(s.conv_key)
        if not isinstance(pre, dict):
            return False
        if now - float(pre.get("ts") or 0) > self.PREBOUND_TTL_SECS:
            table.pop(s.conv_key, None)
            return False
        uid = str(pre.get("uuid") or "").strip()
        if not uid:
            table.pop(s.conv_key, None)
            return False
        table.pop(s.conv_key, None)
        if uid == getattr(s, "cursor_uuid", None):
            s.uuid_verified = True
            return True
        with self.lock:
            owners = [x for x in self.sessions.values()
                      if x.id != s.id and x.cursor_uuid == uid]
            if any(self._claimed_task(x) for x in owners):
                log_event("预绑定 uid 被认领会话持有，放弃预绑定改走扫库 s={} uid={}".format(
                    s.name, uid[:8]))
                return False
            for x in owners:
                x.cursor_uuid = None
                x.transcript_path = None
                x.uuid_verified = False
        old = getattr(s, "cursor_uuid", None)
        s.cursor_uuid, s.transcript_path = uid, None
        s.uuid_verified = True
        s.ext_instance = str(pre.get("instance") or "")
        s.death_probe_uid = uid
        s.death_info = None
        log_event("身份自校准 tab={} Cursor会话 {}→{}（窗口总线预绑定，composerId 由 hub 指定）".format(
            s.name, (old or "无")[:8], uid[:8]))
        return True

    def _verify_identity_by_generating(self, s):
        """身份自校准（07-31 用户问「能不能直接读 Cursor 的真实状态」的答案落点）。

        直读 Cursor 状态早就有（生成中/时间戳/报错/流水），不准的根子是
        「tab ↔ Cursor 会话」的映射靠猜。这里补上决定性证据：agent 调 zhi/zt
        到达的那一瞬，它的 Cursor 会话必然「正在生成」——本工作区里未被认领、
        且在生成的会话恰好只有一个时，叫话的就是它。每次调用自动校准，
        接手/重开对话后的错配在下一次调用时自愈。
        已被这条铁证验过身份的（uuid_verified）不许被别的 tab 抢走。"""
        if runtime_adapter.is_native(s):
            return
        now = time.time()
        if self._bind_prebound_composer(s, now):
            return
        if now - float(getattr(s, "_ident_ts", 0) or 0) < 60:
            return
        s._ident_ts = now
        try:
            with self.lock:
                # 只把「验过身份」的认领当成排他；猜来的认领允许被真主抢回。
                # 报到空壳的认领也不排他（08-03 错绑根因：接手方的真对话被它
                # 自己的报到空壳认领着，被排除后，「唯一在生成」的幸存者只剩
                # 隔壁挂着等派活的 check-in 对话，铁证反而误伤）
                claimed = {x.cursor_uuid for x in self.sessions.values()
                           if x.cursor_uuid and x.id != s.id
                           and getattr(x, "uuid_verified", False)
                           and not self._is_checkin_shellish(x)}
                shell_uuids = {x.cursor_uuid for x in self.sessions.values()
                               if x.cursor_uuid and x.id != s.id
                               and self._is_checkin_shellish(x)}
                multi_agent_cwd = any(x.id != s.id and x.cwd == s.cwd
                                      for x in self.sessions.values())
            hit = None
            tried_param = False
            if not getattr(s, "agent_named", False) and self._is_checkin_shellish(s):
                # 报到潮里的壳：报到词里必带 conversation_id=「xxx」且用户一贴
                # 就进 composerData，参数形态精确认领比「唯一在生成」稳得多——
                # 08-12 08:37 实测：两个对话同时报到，「唯一在生成」的快照恰好
                # 只捕到对方，把甲绑到了乙的对话上，直到两小时后铁证到达才被
                # 掰回来（期间接手提示词都生成错了对象）。猜测退居二线。
                how = "报到词参数上下文"
                hit = (cursor_db_session_for_conv(s.cwd, s.conv_key, exclude=claimed)
                       or generating_session_for_conv(s.cwd, s.conv_key, exclude=claimed))
                tried_param = True
            if not hit:
                how = "到达时刻唯一在生成"
                hit = generating_now_session(s.cwd, exclude=claimed)
                if hit and (multi_agent_cwd or hit[0] in shell_uuids) \
                        and not composer_mentions_conv(hit[0], s.conv_key, param_context=True):
                    # 多 agent 同工作区（或幸存者是空壳认领的对话）时，「唯一在生成」
                    # 不再是铁证：hub 重启重连风暴里 generating 快照忽隐忽现，08-03
                    # 实测让 4 个 tab 的绑定连环易主。此时必须再见到 s 的对话 ID 以
                    # conversation_id 参数形态出现在那个对话里（zhi/zt 参数、接手提示
                    # 词必有；队友通知的裸 ID 冒充不了），否则交给下面的参数匹配定夺。
                    # 单 agent 工作区不受影响，维持 07-31 以来的原行为。
                    hit = None
            if not hit and not tried_param:
                # "唯一在生成"分不清（多 agent 同工作区并发接手时常见）：改用
                # conversation_id 精确认领本会话的对话——先查 Cursor 实时库(即时、
                # 用户刚贴的接手提示词立即可查)，再退回 transcript 正文匹配
                how = "conversation_id 参数上下文"
                hit = (cursor_db_session_for_conv(s.cwd, s.conv_key, exclude=claimed)
                       or generating_session_for_conv(s.cwd, s.conv_key, exclude=claimed))
            if not hit:
                # 接手交接兜底：目标对话此刻正被另一个【已验证】的真 tab 占着时，
                # 上面两条路都拿 claimed 把它排除掉了（08-03 加排他是为了防错绑），
                # 于是「agent 被派去接手别的活」这种正当换绑永远校准不过来——08-04
                # 实测：团队面板修复的 agent 接了直播项目的活，面板上原 tab 一直挂着
                # 「疑似还活着·通道未接」，被接手的 tab 也一直不复活。
                # 只放行最硬那条证据：对方对话里以 conversation_id 参数形态出现过本
                # 会话的 ID（接手提示词/zhi 参数才有这形态，队友通知里的裸 ID 冒充不了），
                # 且该对话近 30 分钟还在动（死对话虽也含这 ID，天然被时间窗排除）。
                # claimed 为空时这一查与上面那次逐字节等价，白扫一遍库，跳过。
                if claimed:
                    how = "接手交接（对方对话里有本会话 ID 参数）"
                    hit = cursor_db_session_for_conv(s.cwd, s.conv_key)
            if not hit:
                return
            uid, path = hit[0], hit[1]
            if uid == getattr(s, "cursor_uuid", None):
                s.uuid_verified = True
                return
            old = getattr(s, "cursor_uuid", None)
            handed = []
            with self.lock:
                owners = [x for x in self.sessions.values()
                          if x.id != s.id and x.cursor_uuid == uid]
                # 活性闸：真在说话的 verified 本尊，谁的证据都抢不走。
                # 「接手交接」的前提是原主的 agent 被派走、再也不会回来说话；
                # 一个通道在线、几分钟前还在 zhi/zt（或正阻塞等用户回复）的 tab
                # 显然不满足。08-12 实测：排查串台的 agent 对话里叙述了一句
                # 「OA对接 的 conversation_id是1d07989b」，正中参数形态正则，
                # OA对接 下一次 zt 校准就把这个活人的 uuid 抢走了，两个活 tab
                # 的身份来回互抢，用户在面板上看到 tab 说「已被接手」而 agent
                # 明明还在干自己的活。
                # ④ 根治铁律（2026-08-12）：cursor_uuid 退出身份决策，只做显示与
                # transcript 定位。任何「认领了活」的会话对某个 uid 的持有都是
                # 权威的，不能被别人据 uuid 推断夺走——参数形态正则会被对话叙述
                # 误中（08-12 实测：排查串台 的对话里叙述「OA对接 的 conversation_id
                # 是 1d07989b」，正中正则，OA对接 一 zt 就把活人 排查串台 的 uid
                # 抢走并把它 _handover_out 归档，两个活 tab 身份来回互抢）。命中的
                # uid 被认领了活的会话持有 = 这条推断不可信，整轮拒绝，一个字节不动。
                # 真交接（agent 被派去接别的活）此后由它自己安静退出或用户显式处置。
                for x in owners:
                    if self._claimed_task(x):
                        log_event("uuid 命中被认领会话持有，拒绝据 uuid 改身份 "
                                  "s={} 持有者={} uid={}".format(
                                      s.name, x.name, uid[:8]))
                        return
                # 活性闸（保留）：验过身份、正在说话的非空壳原主，谁都不许抢
                for x in owners:
                    if not (getattr(x, "uuid_verified", False)
                            and not self._is_checkin_shellish(x)):
                        continue
                    on_line = x.connected or (
                        float(getattr(x, "recon_deadline", 0) or 0) >= now)
                    spoke_ts = max(
                        float(getattr(x, "last_zhi_ts", 0) or 0),
                        float(getattr(x, "agent_status_ts", 0) or 0))
                    talking = (x.pending is not None
                               or now - spoke_ts < self.HANDOVER_QUIET_SECS)
                    if on_line and talking:
                        return
                for x in owners:
                    # 到这里的 owner 都没认领过活（claimed 的已在上面整轮返回）：
                    # 多半是报到空壳/未验证的猜测认领，从它手里把本尊线索拿回来，
                    # 它下次调用会再自校准。verified 非空壳的走 _handover_out 归档
                    # ——但那种此刻已不可能是 claimed，只会是历史遗留的验证残留。
                    if (getattr(x, "uuid_verified", False)
                            and not self._is_checkin_shellish(x)):
                        handed.append(x)
                    x.cursor_uuid = None
                    x.transcript_path = None
                    x.uuid_verified = False
            s.cursor_uuid, s.transcript_path = uid, path
            s.uuid_verified = True
            log_event("身份自校准 tab={} Cursor会话 {}→{}（{}）".format(
                s.name, (old or "无")[:8], uid[:8], how))
            for x in handed:
                self._handover_out(x, s)
        except Exception:
            pass

    # 定位过的 transcript 多久不动就怀疑「这个 tab 已经换了一个 Cursor 对话」
    RELOCATE_STALE_SECS = 180
    # verified 原主最近一次 zhi/zt 在这个窗口内就算「还在说话」，接手交接不许
    # 抢它的 uuid（活性闸，见 _verify_identity_by_generating）。窗口别取太短：
    # 埋头干长活的 agent 两次 zt 间隔常有几分钟——08-12 实测被抢时原主 7 分钟
    # 前刚 zt 过。真被接手的对话此后再无 zhi/zt，最多延迟一个窗口就能落地。
    HANDOVER_QUIET_SECS = 900

    def _relocate_if_stale(self, s, claimed):
        """接手/重开对话后，同一个 conversation_id 会落到新的 Cursor 会话文件上，
        而定位只在首次做过一次——老路径从此永远不再更新。后果是「这个 agent 在不在
        干活」的判据永久失真：正在猛干的 tab 会被判成「已收工」（07-29 实测本会话）。
        所以：通道活着、心跳新鲜，但 transcript 长时间没动 → 重新找一次，只有找到
        更新更勤的文件才替换。"""
        now = time.time()
        if not s.connected or now - getattr(s, "last_heartbeat", 0) > 60:
            return
        if getattr(s, "uuid_verified", False):
            # 被「到达时刻在生成」铁证验过的身份，不许被「谁最新算谁」的猜测覆盖；
            # 它真换了对话的话，下一次 zhi/zt 到达会重新自校准
            return
        try:
            age = now - os.stat(s.transcript_path).st_mtime if s.transcript_path else None
        except OSError:
            age = None
        if age is not None and age < self.RELOCATE_STALE_SECS:
            return
        others = claimed - {s.cursor_uuid}
        located = locate_cursor_session(s.cwd, s.conv_key, s.created_ts, exclude=others)
        if not located or located[0] == s.cursor_uuid:
            # 按 conversation_id 搜正文经常只能搜到「上一任」的旧文件：agent 通常只把
            # ID 写在工具参数里，不落进 transcript 正文（本会话实测就是这样，于是
            # 接手过的 tab 永远指着旧对话）。退而求其次：认 Cursor 自己记录的
            # 「本项目里刚刚还在动、且没被别的 tab 认领」的那个对话。
            fresh = freshest_active_session(s.cwd, exclude=others, within_secs=120)
            if not fresh or fresh[0] == s.cursor_uuid:
                return
            located = (fresh[0], fresh[1])
        try:
            new_age = now - os.stat(located[1]).st_mtime
        except OSError:
            new_age = None
        act = read_cursor_activity(located[0]) or {}
        if act.get("updated_ts"):
            act_age = now - act["updated_ts"]
            new_age = act_age if new_age is None else min(new_age, act_age)
        if new_age is None or (age is not None and new_age >= age):
            return
        claimed.discard(s.cursor_uuid)
        s.cursor_uuid, s.transcript_path = located
        claimed.add(s.cursor_uuid)
        log_event("会话「{}」换了 Cursor 对话，transcript 重定位到 {}（{:.0f}s 前刚写过）".format(
            s.name, located[0][:8], new_age))

    def _title_is_boilerplate(self, title):
        """新 Cursor 对话在生成正式标题前，标题往往就是首条消息（报到提示词）的截断，
        或被总结成「Persistent plus reporting setup」一类模板句——这类标题同步成 tab 名
        只会制造一排看不出差别的长名（实测 15:19 一口气出现 3 个同名 tab）。"""
        t = (title or "").strip()
        if not t:
            return True
        low = t.lower()
        if AUTO_TITLE_RE.search(low):
            return True
        for probe in ("zhi工具报到", "rxyy MCP的zhi"):
            if probe in low:
                return True
        # 报到提示词前缀（new_chat_prompt 可能被用户改过，动态比对）
        ncp = re.sub(r"\s+", "", str(self.cfg.get("new_chat_prompt") or ""))
        tn = re.sub(r"\s+", "", t).rstrip("…...")
        return bool(ncp) and len(tn) >= 8 and ncp.startswith(tn)

    # ---------- 团队黑板（重大情况主动读写，跨重启持久化） ----------
    BULLETIN_PATH = DATA_DIR / "board.json"
    # 所有类别一律安静落板，等人主动来读：同一工作区常挂着互不相干的项目
    # （08-03 实测：ctest 的事故推给了全部 agent），按 root 无差别打断只会
    # 制造噪音——要立刻惊动谁，用 转告/广播 点名

    def _load_bulletin(self):
        try:
            data = json.loads(self.BULLETIN_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_bulletin(self):
        try:
            tmp = str(self.BULLETIN_PATH) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.bulletin, f, ensure_ascii=False, default=str)
            os.replace(tmp, self.BULLETIN_PATH)
        except Exception as e:
            log_event("黑板写盘失败: {}".format(e))

    # ---------- 队内传话记录（团队面板「队内传话」区，跨重启持久化） ----------
    RELAYS_PATH = DATA_DIR / "relays.json"

    def _load_relays(self):
        try:
            data = json.loads(self.RELAYS_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def _save_relays(self):
        try:
            tmp = str(self.RELAYS_PATH) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.relay_log, f, ensure_ascii=False, default=str)
            os.replace(tmp, self.RELAYS_PATH)
        except Exception as e:
            log_event("传话记录写盘失败: {}".format(e))

    # ---------- 接手别名（派接手给待命壳时登记，跨重启持久化） ----------
    ALIASES_PATH = DATA_DIR / "takeover-aliases.json"

    def _load_takeover_book(self):
        """读接手台账（四账合并A步，2026-08-26）。

        v2 格式：{"_v":2, "aliases":{退休conv:{succ,old_name,succ_label,ts,why}},
        "names":{壳名:{ts,conv,succ_label,succ8}}}。08-26 前的平表
        {退休conv: 现任conv} 照读——升级不丢账，首次落盘自动写成 v2。

        读走 _load_json_resilient：密文态先解密再读，真读不了会举手而非静默清空
        （08-27 转告事故根因，见该函数 docstring）。"""
        data, status = _load_json_resilient(self.ALIASES_PATH, what="接手台账")
        if status == "unreadable":
            log_event(
                "接手台账在盘上却读不回来，本次只能以空台账启动——入站别名换名将"
                "失效、被归并壳重连可能被当新报到而开出重复 tab（08-27 转告事故同因）；"
                "已尝试 TSD 解密仍未果，请查 {}".format(self.ALIASES_PATH))
        if not isinstance(data, dict):
            return {"aliases": {}, "names": {}}
        if data.get("_v") == 2:
            return {
                "aliases": {str(k): dict(v) for k, v in
                            (data.get("aliases") or {}).items()
                            if isinstance(v, dict)},
                "names": {str(k): dict(v) for k, v in
                          (data.get("names") or {}).items()
                          if isinstance(v, dict)},
            }
        return {"aliases": {str(k): {"succ": str(v), "old_name": "",
                                     "succ_label": "", "ts": 0.0, "why": ""}
                            for k, v in data.items()},
                "names": {}}

    def _save_takeover_aliases(self):
        """台账落盘：别名+名字墓碑一本账、一次原子写（A步收口）。
        方法名保持旧称——测试与调用点都按它打桩/调用，收口不改契约。"""
        try:
            tmp = str(self.ALIASES_PATH) + ".tmp"
            book = {"_v": 2,
                    "aliases": getattr(self, "takeover_ledger", None) or {},
                    "names": self.name_tombstones or {}}
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(book, f, ensure_ascii=False)
            os.replace(tmp, self.ALIASES_PATH)
            # 落盘即解密：DATA_DIR 若落在被 TSD 监控的目录，pythonw 写出的 .json
            # 会被透明加密，下次冻结 exe 读到密文 → _load_takeover_book 救不回来就
            # 退回空台账（转告事故根因）。原地解密一次保证任何进程读到明文。
            _ensure_plaintext_ondisk(self.ALIASES_PATH)
        except Exception as e:
            log_event("接手台账写盘失败: {}".format(e))

    # ---------- 会话跨重启持久化 ----------
    STATE_PATH = DATA_DIR / ".sessions.json"

    def save_state(self):
        """把当前会话元数据+消息+排队中的用户消息落盘，供 hub 重启后恢复 tab。

        故障隔离（07-31）：原来整个函数一层 try——任何一个会话的字段坏了，
        整份快照就静默不写，文件从此停在旧状态；下次重启按旧快照恢复，
        之后注册的会话全部人间蒸发（实测：重启后 4 个待命 tab 消失）。
        现在：坏会话单独跳过并记日志，其余照存；写盘失败也要喊出来。"""
        try:
            with self.lock:
                order = list(self.order)
                sess = {sid: self.sessions.get(sid) for sid in order}
        except Exception:
            return
        snap = []
        broken = []
        for sid in order:
            s = sess.get(sid)
            if not s:
                continue
            try:
                # 单会话 → dict 已抽到 session_core（第四刀批C）
                snap.append(session_core.session_to_snapshot(self, s))
            except Exception as e:
                broken.append("{}({})".format(getattr(s, "name", sid), e))
        try:
            tmp = str(self.STATE_PATH) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                # default=str：个别不可序列化的字段退化成字符串，别废掉整份快照
                json.dump(snap, f, ensure_ascii=False, default=str)
            os.replace(tmp, self.STATE_PATH)
        except Exception as e:
            self._warn_save_failure("快照写盘失败: {}".format(e))
            return
        if broken:
            self._warn_save_failure("快照跳过 {} 个坏会话: {}".format(
                len(broken), "; ".join(broken[:3])))

    def _warn_save_failure(self, msg):
        """快照异常要喊出来，但 15s 一次的循环里别刷屏：同样的话 10 分钟只记一次。"""
        now = time.time()
        last_ts, last_msg = getattr(self, "_save_warn", (0, ""))
        if msg == last_msg and now - last_ts < 600:
            return
        self._save_warn = (now, msg)
        log_event("会话快照异常：{}".format(msg))

    def load_state(self):
        """hub 启动时把上次的会话作为「已断开」tab 恢复；MCP 用同 conv_key 重连即复活。"""
        try:
            if not self.STATE_PATH.is_file():
                return
            snap = json.loads(self.STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return
        snap = snap or []
        # 同 conversation_id 只恢复最新一份：多实例竞态曾在快照里留下同对话的重复
        # 会话（实测同一对话同时存着「新任务」「新任务2」两个 tab），全恢复必出双 tab
        best = {}
        for i, d in enumerate(snap):
            ck = d.get("conv_key", "__default__")
            if ck == "__default__":
                continue
            b = best.get(ck)
            if b is None or (d.get("created_ts") or 0) >= (snap[b].get("created_ts") or 0):
                best[ck] = i
        for i, d in enumerate(snap):
            ck = d.get("conv_key", "__default__")
            if ck != "__default__" and best.get(ck) != i:
                continue  # 同对话的旧重复会话，跳过
            try:
                # dict → 已断开 Session 已抽到 session_core（第四刀批C）；
                # 挂进 sessions/order 的编排留在这里
                s = session_core.session_from_snapshot(self, d)
                with self.lock:
                    self.sessions[s.id] = s
                    self.order.append(s.id)
            except Exception:
                continue
        # 恢复了几个要留痕：快照缺人时（如 07-31 丢 4 个待命 tab）拿这行日志
        # 对照重启前的会话数，一眼定位是「没存上」还是「没恢复」
        log_event("从快照恢复 {} 个会话（快照共 {} 条）".format(
            len(self.order), len(snap)))

    def _state_save_loop(self):
        while True:
            time.sleep(15)
            self.save_state()

    def auto_reload_loop(self):
        """卡死自愈（最后手段）：Cursor 强杀 MCP 后偶发卡 Error 不自动重拉时，触碰
        mcp.json 强制重连。

        !!! 触碰 mcp.json 是全局大锤：Cursor 会掐掉【所有】工作区的rxyy MCP连接，
        进行中的 zhi 会直接报 Connection closed。规则：
        1. hub 启动后 180s 内绝不动手（MCP 活着的话心跳几秒内就会自己重连回来）
        2. 只救「断开后 transcript 仍在更新」的会话：agent 断后还在写盘 = 真卡住；
           对话正常结束的（不再写盘）重载也救不活，纯属白折腾
        3. 断开超阈值才算卡（默认 120s），两次重载至少隔 300s
        注：不再因「有其他健康连接」而拒绝动手——多窗口下总有窗口是健康的，
        否则卡死的窗口永远没人救（实测盲区）。健康窗口被重启后 agent 按
        重试纪律 30s 内自动挂回，代价可控。

        但「代价可控」有前提，见 _auto_reload_verdict：正阻塞在 zhi 里的 agent
        会当场 Connection closed，开车扣着请求时更是一锤子买卖。"""
        last_reload = 0.0
        started = time.time()
        while True:
            time.sleep(15)
            try:
                stuck, why = self._auto_reload_verdict(time.time(), started, last_reload)
                if stuck is None:
                    if why:
                        log_event("卡死自愈：忍住没动手 —— " + why)
                    continue
                if touch_mcp_json():
                    last_reload = time.time()
                    self._reload_swings.append(last_reload)
                    log_event("卡死自愈：对话 {} 断开后 transcript 仍在更新，触碰 mcp.json 重连（{}）".format(
                        stuck.conv_key, why))
            except Exception:
                pass

    def _auto_reload_verdict(self, now, started, last_reload):
        """要不要挥这一锤：返回 (卡死的会话或 None, 人话)。

        抽成纯判断是为了能自测。这一锤打下去，【所有】工作区的rxyy MCP连接一起断，
        正阻塞在 zhi 里的 agent 当场 Connection closed——救一个、伤一片。所以除了
        原有的四道闸（开关/冻结/启动宽限/冷却），再加三条：

        1. 有 agent 正阻塞在 zhi 里就不挥（默认容忍 0 个陪葬；真要挥可调
           auto_reload_max_collateral）。待命中的 agent 全在这个状态。
        2. 开车扣着请求时绝不挥——那一拍断了，扣住的请求就再也放不出去了。
        3. 同一个会话要连着两拍都判卡死才算数，避免抓到一瞬间的抖动。
        再加一个总闸：一小时最多挥 auto_reload_max_per_hour 次（默认 2）。
        """
        if not self.cfg.get("auto_reload_on_stuck", False):
            return None, ""
        if self.cfg.get("token_freeze"):
            return None, ""      # 冻结期本就该挂起，别自动重连
        if now - started < 180:
            return None, ""      # 启动宽限：给活着的 MCP 心跳重连留时间
        threshold = max(90, int(self.cfg.get("stuck_threshold_secs", 120) or 120))
        cooldown = max(180, int(self.cfg.get("auto_reload_cooldown_secs", 300) or 300))
        if now - last_reload < cooldown:
            return None, ""
        per_hour = max(1, int(self.cfg.get("auto_reload_max_per_hour", 2) or 2))
        self._reload_swings = [t for t in getattr(self, "_reload_swings", []) if now - t < 3600]
        if len(self._reload_swings) >= per_hour:
            return None, "一小时内已经挥过 %d 次，先停手" % len(self._reload_swings)

        with self.lock:
            sessions = [self.sessions[sid] for sid in self.order if sid in self.sessions]
        stuck = None
        for s in sessions:
            if s.conv_key == "__default__" or s.connected:
                continue     # 已复活（心跳/zhi 已续上）的会话绝不再按卡死处理
            dropped_at = getattr(s, "disconnected_at", None)
            if not dropped_at or now - dropped_at < threshold:
                continue     # 本次 hub 存活期内没见过它断开，或断得还不够久
            tp = getattr(s, "transcript_path", None)
            if not tp:
                continue
            try:
                mt = os.stat(tp).st_mtime
            except OSError:
                continue
            # 断开【之后】agent 还在写 transcript 且最近 5min 内写过 = 活着但通道死了
            if mt > dropped_at and now - mt < 300:
                stuck = s
                break

        seen = getattr(self, "_stuck_seen", None)
        if stuck is None:
            self._stuck_seen = None
            return None, ""
        if not seen or seen[0] != stuck.conv_key:
            self._stuck_seen = (stuck.conv_key, now)
            return None, "对话 %s 疑似卡死，再看一拍确认" % stuck.conv_key

        try:
            import parkgate
            if parkgate.status().get("armed") or parkgate.parked_count() > 0:
                return None, "开车正扣着请求，这一锤会让它们永远放不出去"
        except Exception:
            pass

        at_risk = [s for s in sessions if s.connected and s.pending is not None]
        allowed = max(0, int(self.cfg.get("auto_reload_max_collateral", 0) or 0))
        if len(at_risk) > allowed:
            return None, "有 %d 个 agent 正阻塞在 zhi 里（%s），救一个伤一片，不挥" % (
                len(at_risk), "、".join(x.name or x.conv_key for x in at_risk[:3]))
        return stuck, "连着两拍都卡死，且没有 agent 在 zhi 里"

    def state_tick_loop(self):
        """状态机唯一推进者（每秒一拍）。

        重构动机（07-27 20:26 用户实测「所有操作都很卡、状态不准」）：过去所有
        基于时间的状态判定塞在 get_state 里——UI 每 700ms 轮询一次，每次都在
        全局锁内做 os.stat 文件 I/O、send 网络写、add_message 落盘，多客户端
        轮询叠加后锁排队，zhi/回复/切页全被拖慢；且「谁先轮询谁触发状态翻转」
        导致翻转时机随机、状态漂移。现在：状态推进集中在本线程一处，get_state
        变成纯读，锁内零 I/O。"""
        while True:
            time.sleep(1.0)
            try:
                self._state_tick()
            except Exception as e:
                log_event("state_tick 异常: {}".format(e))
            try:
                self.sweep_dead_shells()
            except Exception as e:  # noqa: BLE001
                log_event("清扫待命空壳异常（已忽略）: {}".format(e))
            try:
                # 跟 sweep_dead_shells 分开：那个有 120s 早退，挂在后面等于
                # 原会话复活后最多再晾两分钟。本 sweep 自己 8s 节流。
                self.sweep_revived_takeover_shells()
            except Exception as e:  # noqa: BLE001
                log_event("清扫复活接手壳异常（已忽略）: {}".format(e))

    # 归档的待命空壳搁多久算没人要了
    SHELL_SWEEP_AFTER_SECS = 1800
    SHELL_SWEEP_EVERY_SECS = 120
    # 派出的接手提示词多久没落地（没人拿原 ID 报到）就提醒用户换人
    TAKEOVER_STALL_SECS = 900
    # 提醒发出去之后再挂多久，「⏳接手在途」这块牌子就静默下架（见 _state_tick 2.75）
    TAKEOVER_EXPIRE_SECS = 3600

    def sweep_dead_shells(self):
        """清掉早就没人要的待命空壳，别让列表越积越长。

        为什么会积一屋子：收壳只在「接手落地」那一刻触发，而用户开个 Cursor
        对话让 agent 报到、随后直接关掉窗口的壳，根本等不到那一刻——08-07 现场
        21 个，其中 20 个已归档还挂在「已结束」组里。

        判据一条比一条严，宁可少收：出生名是待命 · 没干过真活（机器消息不算，
        见 _real_seq）· 没有未答提问 · 队列里没有用户的话 · 已归档 · 断线超过
        半小时。记录文件一个字不动，那是现场；只是不再占列表。
        """
        now = time.time()
        if now - float(getattr(self, "_shell_sweep_ts", 0) or 0) < self.SHELL_SWEEP_EVERY_SECS:
            return
        self._shell_sweep_ts = now
        with self.lock:
            doomed = [x for x in list(self.sessions.values())
                      if getattr(x, "archived", False)
                      and not x.connected
                      and self._is_takeover_shell(x)
                      # 拿「最后一次还有动静」而不是创建时间：刚归档就清掉的话，
                      # 用户误点一下 × 就再也找不回来了
                      and now - max(float(getattr(x, "last_heartbeat", 0) or 0),
                                    float(getattr(x, "created_ts", 0) or 0))
                      > self.SHELL_SWEEP_AFTER_SECS]
        for x in doomed:
            with self.lock:
                self.sessions.pop(x.id, None)
                if x.id in self.order:
                    self.order.remove(x.id)
            log_event("清扫待命空壳 tab={} conv={}（已归档、断线、没干过活）".format(
                x.name, x.conv_key))
        if doomed:
            self.save_state()

    # 报到潮：曾以为「先到的 zhi 会占住 Cursor 对本 MCP 的并发槽位，后面的报到几分钟
    # 都进不来」，故开一个窗口期持续叫醒等待中的 zhi 轮转槽位。该前提现已证伪
    # （见 DEFAULTS["yield_on_burst"] 的三条实测），默认关闭；留开关是因为「同时开
    # 六个新对话只到一个」曾真实发生过，万一另有成因，一勾就能退回旧行为。
    YIELD_BURST_SECS = 240
    YIELD_BURST_EVERY = 12

    def yield_enabled(self):
        return bool(self.cfg.get("yield_on_burst", False))

    def start_yield_burst(self, secs=None):
        if not self.yield_enabled():
            return
        self._yield_burst_until = time.time() + (secs or self.YIELD_BURST_SECS)

    def _tick_yield_burst(self, now):
        if now >= getattr(self, "_yield_burst_until", 0):
            return
        if now - getattr(self, "_yield_burst_last", 0) < self.YIELD_BURST_EVERY:
            return
        self._yield_burst_last = now
        try:
            self.yield_zhi_all_local()
        except Exception:
            pass

    DEATH_PROBE_EVERY = 10.0
    # 08-26 用户「切档后牌更新慢」：旧值 60 秒，面板自己都写着「一分钟内跟上」。
    # 单键 sqlite ~1ms，8 秒一拍够跟手；zt/zhi 到达还会清限频立刻重探。
    MODEL_PROBE_EVERY = 8.0

    def board_write_ages(self, s, now):
        """会话的看板两路实时信号年龄（第四刀已抽 session_core，此处委托）。"""
        return session_core.board_write_ages(self, s, now)

    def _fusion_recent_activity(self, s, now, within=180):
        """四路信号 within 秒内的活跃判据（第四刀已抽 session_core，此处委托）。"""
        return session_core.fusion_recent_activity(self, s, now, within)

    def _tick_death_probe(self, s, now):
        """Cursor 对话死因探测与判死清算（第四刀批D已抽 session_core，此处委托）。"""
        if runtime_adapter.is_native(s):
            return
        return session_core.tick_death_probe(self, s, now)

    def _tick_model_probe(self, s, now):
        """这个 tab 用的是哪个模型（委托 session_core，结果缓存供面板纯读）。"""
        if runtime_adapter.is_native(s):
            return runtime_adapter.update_native_model(s, now)
        return session_core.tick_model_probe(self, s, now)

    def _tick_card_auto_decide(self, s, now):
        """决策卡片开了 autoDecide 且到点还没人答：采纳推荐项 / 交回 AI 自定。

        计时以提问到达 hub（pending.created）为起点。答复走 Api.answer_card → send_reply，
        与人点的同一条路，pending 原子取走，界面倒计时那头若同时到点只会白报一次错。
        """
        pending = getattr(s, "pending", None)
        if not isinstance(pending, dict) or not s.connected:
            return
        card = pending.get("card")
        if not card:
            return
        ad = card.get("autoDecide") or {}
        if not ad.get("enabled"):
            return
        try:
            deadline = float(pending.get("created") or 0) + float(ad.get("timeoutSec") or 0)
        except (TypeError, ValueError):
            return
        if not pending.get("created") or now < deadline:
            return
        mode = "delegate_system" if ad.get("onResolve") == "delegate" else "adopt_recommended"
        try:
            r = Api().answer_card(s.id, {}, mode)
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "error": repr(e)}
        log_event("决策卡片到点代决 tab={} card={} mode={} ok={}{}".format(
            s.name, card.get("id"), mode, bool(r.get("ok")),
            "" if r.get("ok") else " err=" + str(r.get("error"))))
        if not r.get("ok"):
            # 没答成（多半是通道刚断）：别每秒重试刷日志，关掉这张卡的自动代决
            ad["enabled"] = False

    def alert_session_death(self, s, dead):
        """会话被判死刑时提醒用户——这类死法本来一点声音都没有。"""
        try:
            self.notify()
        except Exception:
            pass
        # 并发闸：明示一次「排队中」（24h 冷却在调用方那本账），但绝不推骷髅、
        # 不给「一键派单」按钮——派单=再开会话，正是 08-27 11:42 事故里
        # 5+ tab 互相加压、57 连败 26 分钟的那个正反馈。
        if (dead or {}).get("gate"):
            try:
                body = "排队中：{}".format(dead.get("reason") or "Cursor 并发满/限频")
                if dead.get("advice"):
                    body += "\n→ " + str(dead["advice"])
                self.push_phone(self._push_label(s), body,
                                tags="hourglass_flowing_sand", priority=3)
            except Exception:
                pass
            return
        # 连接抖/无名报错中断：控制台角标照亮即可，别推 Bark（08-26 快编误报）。
        if session_core._is_transient_death(dead):
            return
        try:
            # 标题=哪个 agent，正文=死因+下一步（死会话处置①：光知道挂了没用，
            # 人在外面要能直接判断是回窗口点重试还是派接手）；skull 图标 + 高优先级
            body = "挂了：{}".format(dead.get("reason", "报错中断"))
            if dead.get("advice"):
                body += "\n→ " + str(dead["advice"])
            self.push_phone(self._push_label(s), body,
                            tags="skull,warning", priority=4,
                            extra_actions=[self._takeover_action(s.id)])
        except Exception:
            pass

    # 「零信号自动问话」已删（07-31 用户拍板）：问话要 agent 回答，可它正在干活
    # 或轮次已结束时都答不了，误伤大于价值。改为直接探测：读对话流水尾部的
    # turn_ended 事件（见 _agent_liveness 的 turn_done 档），不需要 agent 配合。

    # 寄存滞留提醒周期（死会话处置②）：转告在死/断会话队列里躺满这么久仍无人收
    # 就给发送方捎一句，每条只提醒一次
    PARKED_RELAY_REMIND_SECS = 1800

    def _tick_parked_relay_reminders(self, now):
        """寄存滞留提醒（死会话处置②）。08-13 实证：日报壳终止后「README+安装
        方案」派活寄存一个多小时，发送方手里只有一条「✓ 已提交转告」，活就此
        断线没人知道。每分钟扫一轮不在线会话的队列，from_conv 可溯源的寄存条目
        滞留超时 → 往发送方队列塞一条控制台提醒（who=控制台，走 zt 顺路取信，
        不占用户回复位）。"""
        if now - getattr(self, "_parked_scan_at", 0) < 60:
            return
        self._parked_scan_at = now
        with self.lock:
            sessions = list(self.sessions.values())
        for t in sessions:
            if getattr(t, "connected", False) or getattr(t, "archived", False):
                continue
            rd = getattr(t, "recon_deadline", 0) or 0
            if rd and now <= rd:
                continue  # 重连宽限内的别催，多半马上自己回来
            for e in list(getattr(t, "queued", None) or []):
                fc = e.get("from_conv")
                ts = float(e.get("parked_ts") or 0)
                if not fc or not ts or e.get("stale_reminded"):
                    continue
                if now - ts < self.PARKED_RELAY_REMIND_SECS:
                    continue
                e["stale_reminded"] = True
                sender = next((x for x in sessions
                               if x.conv_key == fc
                               and not getattr(x, "archived", False)), None)
                if sender is None:
                    continue
                mins = int((now - ts) // 60)
                note = ("【寄存滞留提醒】你 {} 分钟前转告「{}」的话还躺在它队列里"
                        "（它仍未复活）。可改投在线 agent（ji(action=\"转告\", "
                        "category=对话ID前8位)），或提醒用户在团队面板派人接手。"
                        .format(mins, t.name))
                with sender.lock:
                    sender.queued.append({
                        "id": uuid.uuid4().hex[:8], "text": note,
                        "images": [], "files": [], "who": "控制台",
                    })
                log_event("寄存滞留提醒 → {}（转告在 {} 队列已 {} 分钟无人收）".format(
                    sender.name, t.name, mins))

    LIVE_STEP_INTERVAL = 3.0       # generating 的 tab 多久读一次最后一步
    LIVE_STEP_LINGER = 45.0        # 停止生成后再保留多久，然后从 tab 上摘掉

    def _tick_live_step(self, s, now):
        """tab 列表一句话进度（rxyy 09-03 打磨时间线 ③）。缓存在 s.live_step_cache，
        get_state 纯读。只在 Cursor 正在生成时读库（每 3s 一次、只读最后 3 条气泡，
        实测 3~6ms）；停了之后那句话留 45s 再摘，免得一停就闪没。"""
        if runtime_adapter.is_native(s):
            turn = runtime_adapter.read_native_turn(s, now=now)
            if cursor_turns is not None:
                view = cursor_turns.last_step_view(turn)
                if view:
                    view["seen"] = now
                s.live_step_cache = view
            return
        if cursor_turns is None:
            return
        uid = getattr(s, "cursor_uuid", None)
        cur = getattr(s, "live_step_cache", None)
        if not uid:
            if cur is not None:
                s.live_step_cache = None
            return
        generating = bool((getattr(s, "live_cache", None) or {}).get("generating"))
        last_at = float(getattr(s, "_live_step_probe_at", 0) or 0)
        if not generating:
            # 没在生成：不读库；旧的那句话过了 linger 就摘
            if cur is not None and now - float(cur.get("seen") or 0) > self.LIVE_STEP_LINGER:
                s.live_step_cache = None
            return
        if now - last_at < self.LIVE_STEP_INTERVAL:
            return
        s._live_step_probe_at = now
        try:
            cache = Api._TURN_CACHE.setdefault(uid, {})
            turn = cursor_turns.read_turn(uid, max_steps=3, cache=cache, now=now)
            view = cursor_turns.last_step_view(turn)
        except Exception:
            view = None
        if view is None:
            return
        view["seen"] = now
        # 不动 s.rev：rev 一变 UI 就重拉整段消息，这里只是 tab 上一句话——UI 的
        # tabsSignature 自己把 live_step 算进去，变了就重画那一行
        s.live_step_cache = view

    def _state_tick(self):
        now = time.time()
        self._tick_yield_burst(now)
        self._tick_parked_relay_reminders(now)
        with self.lock:
            sessions = [self.sessions[sid] for sid in self.order if sid in self.sessions]
        for s in sessions:
            # 0) 用户已手动关掉的标签只剩回看价值：不探活、不判死、不提醒，
            #    省下每秒的 Cursor 库读盘，也免得关掉的会话还在响铃推手机
            if getattr(s, "archived", False):
                continue
            # 1) 重连宽限到期 → 定案断开（唯一自动翻转 connected 的地方；
            #    依据是硬事实：TCP 已断 + 宽限内没有任何重连/心跳/zhi）
            if not s.connected and s.recon_deadline and now > s.recon_deadline:
                if s.client is None:
                    # hub 重启恢复的快照会话：宽限内没等到复活 = agent 确实不在了。
                    # 静默定案，断开记录在它真正断开时已写过
                    s.recon_deadline = None
                    s.lost_pending_on_drop = False
                    s.end_reason = s.end_reason or "hub 重启前的历史会话"
                else:
                    self._finalize_disconnect(s, s.lost_pending_on_drop)
            # 2) IDE 活跃：os.stat 在锁外做，结果缓存供 get_state 纯读
            ide_active = False
            if not s.connected and not (s.recon_deadline and now <= s.recon_deadline):
                limit = int(self.cfg.get("ide_active_secs", 900) or 0)
                tp = s.transcript_path
                if limit > 0 and tp:
                    try:
                        ide_active = (now - os.stat(tp).st_mtime) < limit
                    except OSError:
                        pass
            s.ide_active_cache = ide_active
            # 2.5) 主动查死因：账号欠费 / 额度到顶 / 被 Anthropic 拒这类致命报错，
            #      Cursor 只写进对话最后一条气泡，界面没有任何动静、agent 也不会再报到，
            #      不主动查就只能眼睁睁看着 tab 一直显示「干活中」
            self._tick_death_probe(s, now)
            # 2.55) 模型牌：跟死因同一条只读通道（Cursor 库单键查），60 秒一拍
            self._tick_model_probe(s, now)
            # 2.56) 决策卡片倒计时代决：到点没人答就按 onResolve 替用户答（服务端计时，
            #       用户没开着那个 tab 也照样生效；界面上的倒计时只是显示）
            self._tick_card_auto_decide(s, now)
            # 2.57) 普通人工发送有 5 秒撤回缓冲。等待回复中的消息不会再由
            # send_reply 当场取走 pending；到期后在这里自动交付，用户不开着页面
            # 也照样发送。AI 处理中没有 pending 的消息则继续留作普通排队。
            self._flush_queue(s)
            # 2.6) 存活判定算好缓存在会话上：tab 圆点和团队面板用同一套结论。
            #      （07-31 用户实测：面板说「干活中」、tab 点却灰色待机——因为
            #      tab 点只认 zt 自报，从不 zt 的 agent 永远灰点，两套逻辑打架）
            try:
                s.live_cache = Api()._agent_liveness(s, now)
            except Exception:
                pass
            # 2.65) tab 列表上那一句「第 N 步 · 运行命令 · pytest…」：只对 Cursor 正在生成
            #       （或这轮刚停、还在 30s 窗口内）的 tab 读最后一条气泡，3s 一拍；
            #       get_state 照旧零 I/O 读缓存
            self._tick_live_step(s, now)
            # 2.7) 接手在途超时：派出的接手提示词等了 15 分钟还没落地，多半是
            #      接手方正忙自己的活、把指令晾着了（08-12 实测：用户等到自己
            #      点开 tab 才发现「接手混乱了」）——提醒一次，好换人。
            #      派给待命壳的场景已有别名机制当场落地，走到这儿的基本都是
            #      「派给了正在干活的 agent」。
            tp = getattr(s, "takeover_dispatched", None)
            if (isinstance(tp, dict) and tp.get("warned")
                    and now - float(tp.get("ts") or now) > self.TAKEOVER_EXPIRE_SECS):
                # 2.75) 牌子到期下架。摘牌本来只有一条路：接手方开口（takeover_landed）。
                #       可那条路只对将来的接手管用——修复上线前落地的那些没人记过账，
                #       重启后照样从快照里恢复出来接着挂。08-25 22:30 现场四块：
                #       650 分钟、19079 分钟（13 天）、297 分钟、649 分钟，接手方
                #       全是早已不在会话表里的待命壳。用户看到「派出去几小时没人接」，
                #       照着它重新派人，才是真把接手搞乱。
                #       牌子的两件正事在 15 分钟那一下就做完了（面板留言 + 手机推送），
                #       之后只剩「派给了谁」，而这条已经写在 tab 历史里。所以静默下架：
                #       不再提醒、不动任何别的状态。
                s.takeover_dispatched = None
                s.rev += 1
                tp = None
            if (isinstance(tp, dict) and not tp.get("warned")
                    and now - float(tp.get("ts") or now) > self.TAKEOVER_STALL_SECS):
                tp["warned"] = True
                mins = int((now - float(tp.get("ts") or now)) // 60)
                to_name = str(tp.get("to_name") or "?")
                self.add_message(s, {
                    "role": "sys", "ts": now_hms(),
                    "html": "⚠ 派给 <b>{}</b> 的接手已等 {} 分钟没落地——它可能正忙"
                            "自己的活、把指令晾着了。可重新派给「正等回复」或空闲"
                            "待命的 agent。".format(html_mod.escape(to_name), mins),
                })
                self.notify()
                # 这条提醒写在一个已经没人看的死 tab 里等于没提醒（08-12 实测
                # 用户是自己点进去才发现的）——必须推到人手上
                try:
                    purl = (owner_session_url(self.cfg, s.id)
                            if self.cfg.get("share_enabled", True) else None)
                except Exception:
                    purl = None
                try:
                    self.push_phone(
                        "接手没落地：{}".format(s.name),
                        "派给「{}」已等 {} 分钟没人接管，可能被晾着了；"
                        "点开可重新派人。".format(to_name, mins),
                        url=purl, tags="warning",
                        extra_actions=[self._takeover_action(s.id)])
                except Exception as e:  # noqa: BLE001
                    log_event("接手超时推送失败（已忽略）: {}".format(e))
            # 3) 保活脱离期看门狗（第四刀批D已抽 session_core）：detach 超宽限
            #    的失联清算——认领了活/融合信号还热着的只挂起不清
            session_core.detach_watchdog_tick(self, s, now)
            # 4) 处理态超时兜底：长时间没回来只切【显示】并如实标注是推断，
            #    不再有任何「闲置定案」「IDE 打断猜测」之类把连接翻成断开/
            #    伪造应答的魔法（07-27 用户实测这些推断经常判错，宁可少动）
            if s.connected and s.pending is None and s.processing_since:
                age = now - s.processing_since
                limit = int(self.cfg.get("processing_timeout_secs", 1800))
                if age > limit:
                    s.processing_since = None
                    self.add_message(s, {
                        "role": "sys", "ts": now_hms(),
                        "html": "AI 超过 {} 分钟未再提问，转为待机显示（推断：本轮"
                                "可能已在 IDE 端结束；若 AI 回来提问会自动恢复）".format(
                                    max(1, int(limit // 60))),
                    })
        # 人手开的对话补钉出生窗口（工作区只对应一个在线 Cursor 时才钉，不猜）
        try:
            self._tick_window_bind(now)
        except Exception:
            pass
        # 工作流编排：运行中的步骤超时无收工信号 → 标卡+提醒（引擎自带锁与去重）
        try:
            if WORKFLOW is not None:
                WORKFLOW.tick(now)
        except Exception:
            pass

    def mcp_http_daemon_loop(self):
        """39222 MCP 端点宿主（2026-08-12 hub 拆分第二刀：server.py 并入 core）。

        Streamable HTTP handler 以线程挂在本进程里（server.serve_http(in_hub=True)），
        HubBridge 经进程内管道直达本 Hub（server._INPROC_HUB + ipc.InProcSock），
        不再拉起独立 server.py：少一层进程、一层 TCP、一类重连竞态——07-27
        「39222 半死没人管」那一类故障源整个消除。SSE/progress/KEEPALIVE/让路
        逻辑在 server 模块里原样未动。

        10s 巡检保留，它同时兜住两件事：
        · 内嵌实例半死自愈（_listener_selfcheck 关掉实例后，本巡检 10s 内重建）；
        · 端口被外部 server.py 占着时（hub 死亡期间看门狗拉起的降级外挂，走的
          还是 38999 TCP 桥），内嵌绑定失败秒退、不抢——外挂一死本巡检立刻
          接管回来，系统自动收敛回进程内形态。

        端点【离线→上线】时触碰一次 mcp.json：Cursor 的每窗口 HTTP 客户端在
        服务不可达时会锁死在 error 退避态（实测多窗口 agent 因此卡 3 分钟才接上），
        触碰强制所有窗口立即重新 initialize——HTTP 下这只是毫秒级握手，无进程可杀。"""
        port = int(self.cfg.get("mcp_http_port", 39222) or 0)
        if not port:
            return
        import server as server_mod
        server_mod._INPROC_HUB = self  # MCP 桥从此进程内直达，不再连 38999
        was_alive = None  # None=首巡未知
        while True:
            alive = False
            try:
                s = socket.create_connection(("127.0.0.1", port), timeout=1)
                s.close()
                alive = True
            except OSError:
                pass
            if not alive:
                try:
                    threading.Thread(
                        target=server_mod.serve_http, args=(port,),
                        kwargs={"in_hub": True}, daemon=True,
                        name="mcp-http-in-hub").start()
                    log_event("MCP HTTP 端点线程已在 hub 进程内拉起 port={}"
                              "（绑定结果见 server-run.log）".format(port))
                    time.sleep(5)
                except Exception as e:
                    log_event("进程内拉起 MCP HTTP 端点失败: {}".format(e))
                    time.sleep(30)
            elif was_alive is False:
                # 离线→上线：唤醒所有 Cursor 窗口的 HTTP 客户端（脱离 error 退避态）
                if touch_mcp_json():
                    log_event("MCP HTTP 端点恢复上线，已触碰 mcp.json 唤醒各窗口客户端")
            was_alive = alive
            time.sleep(10)

    # ---------- 会话（对话 tab）管理 ----------
    def _registered_team_root(self, conv_key):
        """返回 intake/席位为 conversation_id 明确登记的任务根。

        这段必须留在 Hub 内部，不能借 Api._team_scope：Hub 构造时会先恢复快照，
        此时 Api 类尚未定义。席位是固定资源，优先级高于 intake 成员登记。
        """
        conv_key = str(conv_key or "").strip()
        if not conv_key or conv_key == "__default__":
            return ""
        for root, projects in (self.cfg.get("team_projects") or {}).items():
            for bucket in (projects or {}).values():
                if any(isinstance(seat, dict) and seat.get("id") == conv_key
                       for seat in (bucket or {}).get("seats") or []):
                    return sanitize_ws_path(root)
        for root, seats in (self.cfg.get("team_seats") or {}).items():
            if any(isinstance(seat, dict) and seat.get("id") == conv_key
                   for seat in seats or []):
                return sanitize_ws_path(root)
        member = (self.cfg.get("team_project_members") or {}).get(conv_key)
        if isinstance(member, dict):
            return sanitize_ws_path(member.get("root") or "")
        return ""

    def _bind_registered_team_root(self, session):
        """席位/成员登记只在「还没自报项目」时认领根。

        人已经用 task_name 报了「心理·…」，不能再被旧席位拖回「智慧云广播」
        （08-21：心理评测·任务统筹人在 mh_admin_suite，分组却钉在广播）。
        席位跟人走，见 Api._follow_seat_to_named_project。
        """
        named = str(getattr(session, "agent_project", "") or "").strip()
        if named:
            try:
                key = Api._project_key(named)
            except Exception:
                key = ""
            if key and key != getattr(Api, "TEAM_DEFAULT_PROJECT", "__workspace__"):
                return ""
        root = self._registered_team_root(getattr(session, "conv_key", ""))
        if root:
            session.task_root = root
            session.task_root_locked = True
        return root

    def _dedupe_name(self, name, exclude_id=None, conv_key=""):
        """需在持有 self.lock 时调用。
        exclude_id：改名场景传会话自身 id——否则自己的旧名也算「已占用」，
        同名 task_name 反复调用会把 tab 改名成 新任务1→新任务3→新任务4…（实测事故）
        conv_key：重名时优先拿对话 ID 后 4 位做后缀。「待命·cursor工作流1 / 2」
        这种编号在列表里认不出谁是谁，而对话 ID 跟报到消息、转告点名用的是同一个。"""
        existing = {x.name for sid, x in self.sessions.items() if sid != exclude_id}
        if name not in existing:
            return name
        conv = str(conv_key or "")
        # 没带 conversation_id 的连接一律是 "__default__"，截出来的「ault」谁也认不出，
        # 那还不如退回编号
        tag = conv[-4:] if conv != "__default__" else ""
        if tag and f"{name}·{tag}" not in existing:
            return f"{name}·{tag}"
        i = 1
        while f"{name}{i}" in existing:
            i += 1
        return f"{name}{i}"

    def _resolve_for_signal(self, client: Client, conv_key, revive_handed=False):
        """给「非 zhi 的信号」（心跳/状态上报）找会话：优先本连接已有的；
        找不到就从 hub 全局注册表按 conv_key+来源 复活并挂到本连接——
        解决「MCP 进程重连后、首个 zhi 之前，状态/心跳被静默丢弃」的 bug。

        revive_handed：zt/ji 这类「agent 主动说话」的信号设 True——被「接手交接」
        自动归档的 tab（handed_off_to 非空）还带着自己的 conv_key 来说话，说明它
        的 agent 明明活着、交接是误判，就地撤销归档（08-12 实测：OA对接 被叙述
        污染误判交接后，zt 全被丢弃，tab 一直躺在已结束组，用户以为会话死了）。
        心跳不设：它是进程级自动信号，服务过的死对话也在名单里，认了就会把
        用户手动关的 tab 弹回来。用户手动 × 的没有 handed_off_to，永远不自动复活。"""
        if conv_key == "__default__":
            return None
        s = client.sessions.get(conv_key)
        if s is not None:
            if (getattr(s, "auto_reconnect_blocked", False)
                    and not revive_handed):
                return None
            if getattr(s, "archived", False):
                if getattr(s, "handoff_final", False):
                    return None
                if not (revive_handed and getattr(s, "handed_off_to", "")):
                    return None
            else:
                # revive_handed 这一档就是「agent 主动说话」（zt/ji），见上面的
                # 说明；心跳走的是 False，不能拿它当落地（takeover_landed 里写了
                # 为什么）
                if revive_handed:
                    self.takeover_landed(s)
                self._ensure_listed(s)
                return s
        if s is None:
            with self.lock:
                # 归档的 tab 不认心跳/状态：用户刚关掉的对话，其 MCP 进程往往还会
                # 心跳几轮（每 5s 报一次它服务的 conv），认了就会把 tab 重新标成
                # 「连接中」弹回列表，第二次 × 还会被「仍在连接中」挡住
                cands = [x for x in self.sessions.values()
                         if x.conv_key == conv_key and x.peer_ip == client.peer_ip
                         and not getattr(x, "archived", False)
                         and (revive_handed
                              or not getattr(x, "auto_reconnect_blocked", False))]
                if not cands and revive_handed:
                    # 交接归档的例外（理由见 docstring）
                    # handoff_final：多选接手已把原对话钉死在「已结束」，
                    # 原 ID 再说话也不得把旧 tab 拉回待续（09-07）
                    cands = [x for x in self.sessions.values()
                             if x.conv_key == conv_key and x.peer_ip == client.peer_ip
                             and getattr(x, "archived", False)
                             and getattr(x, "handed_off_to", "")
                             and not getattr(x, "handoff_final", False)]
            if not cands:
                return None
            s = max(cands, key=lambda x: (x.cwd == client.cwd, x.pid == client.pid,
                                          x.created_at))
        if getattr(s, "handoff_final", False) and getattr(s, "archived", False):
            return None
        if getattr(s, "archived", False):
            # 走到这里只可能是 revive_handed 放行的交接归档 tab：撤销误判。
            # closed_convs 也要清——_handover_out 归档时把这个 conv 标成了
            # 「已关闭」，不清的话它下一次 zhi 会被拦截、被告知「立即结束任务」
            s.archived = False
            s.handed_off_to = ""
            s.takeover_dispatched = None
            s.end_reason = ""
            s.rev += 1
            try:
                client.closed_convs.pop(s.conv_key, None)
                if s.client is not None and s.client is not client:
                    s.client.closed_convs.pop(s.conv_key, None)
            except Exception:
                pass
            self.add_message(s, {
                "role": "sys", "ts": now_hms(),
                "html": "交接误判自愈：这个对话的 agent 还在上报状态，已把 tab 从「已结束」组接回来",
            })
            log_event("交接误判自愈 conv={} tab={}（zt/ji 仍在到达，撤销归档）".format(
                conv_key, s.name))
        # 挂到本连接并标记在线（MCP 此刻确实连着），但不发「连接已恢复」气泡——
        # 那是 zhi 复活的职责；这里只为让状态/心跳有处可落
        old_pid = s.pid
        s.client = client
        # 心跳/状态信号不带对话级 cwd（守护进程 hello 的 cwd 是进程级的，多窗口共用
        # 一个 MCP 时不可信）：只在会话还没有 cwd 时兜底，避免控制台重启后心跳复活
        # 把所有 tab 标成同一个目录。真正的纠偏由下一次 zhi 请求随带的 cwd 完成。
        if client.cwd and not s.cwd:
            apply_session_cwd(s, client.cwd)
        if client.cwd and not getattr(s, "task_root", ""):
            cleaned = sanitize_ws_path(client.cwd)
            if cleaned:
                s.task_root = cleaned
        heal_session_paths(s)
        self._bind_registered_team_root(s)
        if client.pid and old_pid and client.pid != old_pid:
            self._migrate_window_tags(old_pid, client.pid)
        s.pid = client.pid
        s.connected = True
        s.auto_reconnect_blocked = False
        s.end_reason = ""
        s.recon_deadline = None
        s.lost_pending_on_drop = False
        s.disconnected_at = None  # 已复活：别再被卡死自愈当成「断开后仍在写盘」误触全局重连
        client.sessions[conv_key] = s
        if revive_handed:
            self.takeover_landed(s)
        self._ensure_listed(s)
        return s

    def _tombstone_shell(self, shell, succ):
        """壳被收起后留一块墓碑：以后有人按老名字转告，能告诉它现任是谁。

        同时把壳的对话ID并进现任的「曾用ID」：接手的实操里，队友手上很可能是
        接手方【报到时那个临时 ID】（面板、传话记录里都露过面），而接手落地后
        真正活着的是被接手的原 ID——不认曾用ID 就只能回一句「找不到」。

        A步（08-26）：墓碑随接手台账一起落盘。此前只活在内存，hub 一重启全清，
        按旧名转告退化成「找不到」（07-31 给「待命·105628d8」递话那类现场，
        每次重启都重演一遍）。
        """
        try:
            self.name_tombstones[str(shell.name or "")] = {
                "ts": time.time(),
                "conv": str(getattr(shell, "conv_key", "") or ""),
                "succ_label": (getattr(succ, "name", "") or "") if succ else "",
                "succ8": ((getattr(succ, "conv_key", "") or "")[:8]) if succ else "",
            }
            shell_conv = str(getattr(shell, "conv_key", "") or "")
            if succ is not None and shell_conv and shell_conv != "__default__":
                hist = list(getattr(succ, "id_history", None) or [])
                if shell_conv not in hist:
                    hist.append(shell_conv)
                succ.id_history = hist[-8:]
            if len(self.name_tombstones) > 60:
                for k in sorted(self.name_tombstones,
                                key=lambda k: self.name_tombstones[k].get("ts", 0))[:20]:
                    self.name_tombstones.pop(k, None)
            self._save_takeover_aliases()
        except Exception:
            pass

    def _retire_conv_into(self, retired_conv, succ_conv,
                          old_name="", succ_label="", why=""):
        """一个 conversation_id 因为「活挪去了后任会话」而退休时的唯一正确收法。

        绝不能改用 closed_convs：那是「用户 × 掉了这个对话」的语义，下一次带旧 ID
        的 zhi 会收到 CLOSED_CONV_REPLY，接手方就地停手。登记成接手别名即可——
        _handle_client_msg 开头会把旧 ID 原地换成后任，迟到的 zhi/zt/ji/心跳照常
        落到现在真正活着的那个 tab，不依赖 agent 肯改 ID（alias_shell_into 的
        docstring 早写死了这条纪律，本方法把它推到另外两条收壳路径上）。

        A步（08-26）：退休登记的唯一入口。一笔写两个视图——台账 takeover_ledger
        带 {succ, old_name, succ_label, ts, why}，事后查「这个 ID 什么时候、为
        什么、并给了谁」不用再翻日志拼线索（拆 73004179 时翻了一小时）；平表
        takeover_aliases 供路由热读。再登记（改指新现任）时空参数不许冲掉已有
        元数据——改指只换 succ，来历留着。
        """
        retired = str(retired_conv or "").strip()
        succ = str(succ_conv or "").strip()
        if (not retired or not succ or retired == succ
                or retired == "__default__" or succ == "__default__"):
            return
        # 后任自己也是别名时顺着指到终点，别连成链——换名只做一跳
        seen = {retired}
        while succ in self.takeover_aliases and succ not in seen:
            seen.add(succ)
            succ = self.takeover_aliases[succ]
        if succ == retired:
            return
        prev = self.takeover_ledger.get(retired) or {}
        self.takeover_ledger[retired] = {
            "succ": succ,
            "old_name": str(old_name or "") or str(prev.get("old_name") or ""),
            "succ_label": (str(succ_label or "")
                           or str(prev.get("succ_label") or "")),
            "ts": time.time(),
            "why": str(why or "") or str(prev.get("why") or ""),
        }
        self.takeover_aliases[retired] = succ
        if len(self.takeover_ledger) > 200:
            # 两个视图一起裁：一边有一边没有，就又回到「四账对不上」的老病
            for k in list(self.takeover_ledger)[:-200]:
                self.takeover_ledger.pop(k, None)
                self.takeover_aliases.pop(k, None)
        self._save_takeover_aliases()

    def _live_handoff_successor(self, s):
        """多选接手钉死后，原 tab 的后任（当前还活着的那个会话）。"""
        conv = str(getattr(s, "handed_off_to", "") or "")
        if not conv:
            conv = str((self.takeover_aliases or {}).get(
                str(getattr(s, "conv_key", "") or ""), "") or "")
        if not conv or conv == "__default__":
            return None
        with self.lock:
            lives = [x for x in self.sessions.values()
                     if x is not s
                     and str(getattr(x, "conv_key", "") or "") == conv
                     and not getattr(x, "archived", False)]
        return max(lives, key=lambda x: str(getattr(x, "created_at", "") or "")) if lives else None

    def _revive_handed_off_conv(self, client: Client, conv_key):
        """带着「已交接」的 ID 回来提问 = 交接判错了，把 tab 接回来，别判死它。

        _handover_out 归档老 tab 时会封 conv（closed_convs），理由是「这个 Cursor
        对话已改去驱动别的会话」。可它此刻正拿这个 ID 调 zhi，说明人还在原地。
        zt/ji 早有这条自愈（_resolve_for_signal revive_handed=True），zhi 是唯一
        还会回「此对话已关闭」的入口——接手方读到就停手，还顺手把 MCP 判死。

        只救 handed_off_to 非空的交接归档；用户手动 × 的没有它，语义不变。
        """
        try:
            s = self._resolve_for_signal(client, conv_key, revive_handed=True)
        except Exception:
            s = None
        if s is None:
            return None
        try:
            client.closed_convs.pop(conv_key, None)
        except Exception:
            pass
        return s

    def takeover_landed(self, s):
        """有活人替这个会话开口了 = 派出去的接手已经落地（或原主自己回来了），
        把「⏳接手在途」摘掉。

        清牌子原先只写在 create_session 的复活分支里。可自 08-12 起，主路早就
        换成了「派给待命壳 → alias_shell_into 当场归并」：接手方带着壳 ID 说的
        每一句都在 _handle_client_msg 里被别名换成本会话，resolve_session /
        _resolve_for_signal 在 client.sessions 里直接命中就返回了，那条复活分支
        根本不经过。于是落地这件事从来没人记账。

        08-25 现场（.sessions.json 落盘 3 秒后读出来的）：

            name=rxyy tools·全面体检 conv=b4eff2ee
            takeover_dispatched to=待命·cursor工作流·f7d6 warned=True age=552min

        接手 9 小时前就落地了（takeover-aliases.json 里 5cbbf7d6→b4eff2ee 白纸
        黑字），接手方一直在这个 tab 里干活，牌子却一直挂着；15 分钟那条「已等 N
        分钟没落地，可重新派给别人」的告警和手机推送照样发了出去。用户照它说的
        再派一次，才是真把接手搞乱——「接手老是出问题」就是这么来的。当时全队
        5 个 tab 同时挂着这块假牌子，最久的一个 18982 分钟。

        **只认 agent 自己开口**（zhi / zt / ji）。心跳不算：那是 MCP 进程级的
        自动信号，一个进程服务过的死对话全在它的名单里躺着，认了就等于「派出去
        的接手永远不会超时」，那条提醒直接废掉。

        B步（08-26）：落地同时销「在途交接」的账——handed_off_to 从此只表示
        【交接在途】，所有指着本会话、还活着的 tab 身上这枚标记一起摘。拆分
        手术的实景：b4eff2ee 活着干活，身上却挂着旧标记，按它旧 ID/旧名的
        转告被 _follow_handoff 转走、每次有人拿原 ID 报到它都进收壳候选名单。
        归档 tab 的不碰：那是 _handover_out 留下的「已交接」史实指针，按旧名
        转告跟随与交接误判自愈（_resolve_for_signal revive_handed）都靠它。
        """
        if s is None:
            return
        if getattr(s, "takeover_dispatched", None) is not None:
            s.takeover_dispatched = None
            s.rev += 1    # 面板上那枚 ⏳ 牌子当拍就得消失，不能等下一次全量刷新
        conv = str(getattr(s, "conv_key", "") or "")
        if not conv or conv == "__default__":
            return
        with self.lock:
            stale = [x for x in self.sessions.values()
                     if x.id != s.id and not getattr(x, "archived", False)
                     and str(getattr(x, "handed_off_to", "") or "") == conv]
        for x in stale:
            x.handed_off_to = ""
            x.rev += 1    # 面板「已交给 X」的角标同拍消失

    def _reap_handed_off_shell(self, client: Client, conv_key):
        """接手方拿原对话ID 报到了 → 把它自己那个已经交出去的待命空壳 tab 收掉。

        不收的话，每派一次接手就在列表里留一个再也不会说话的空标签（接手提示词里
        原本写的是「由用户手动关闭」，一天派几回就积一排）。只收「确实是空壳」的：
        见 _is_takeover_shell（报到 zhi 不算未答提问，08-14）。
        """
        if conv_key == "__default__":
            return
        with self.lock:
            shells = [x for x in self.sessions.values()
                      if x.conv_key != conv_key
                      and (getattr(x, "handed_off_to", "") == conv_key
                           or self._shell_takeover_target(x) == conv_key)]
            succ = next((y for y in self.sessions.values()
                         if y.conv_key == conv_key), None)
        # 有人拿原 ID 报到了，这条路日志里写的就是「接手落地」四个字
        self.takeover_landed(succ)
        for x in shells:
            # 队列里只有机器留言（广播/黑板/回执）不算「用户又派了活」——同
            # _is_takeover_shell 的口径，否则一条广播就能把这个壳钉死在列表里。
            # 认领了活的（改过名/干过真活/正在 developing）同样不收。
            # 08-14：还挂着报到 zhi 的待命壳可以收（那句不是真提问）。
            if not self._is_takeover_shell(x):
                continue  # 用户后来又在这个 tab 里派了别的活，留着
            self._reap(
                x, succ, succ_conv=conv_key,
                why="接手方拿原ID报到，报到壳收编",
                end_note="活已交给 {} 接手，空标签自动收起".format(conv_key),
                event="接手落地，自动收起空壳 tab={} conv={}".format(
                    x.name, x.conv_key),
                land=False)  # 上面已经对 succ 销过在途账，壳循环里别再刷一遍

    def _relocate_takeover_uuid(self, s):
        """接手复活的会话，cursor_uuid 常停在【被接手的旧对话文件】（快照恢复的原值），
        据此收空壳 / 判「谁在干活」全会错位（08-03 实测：待命2 的 uuid 指着前任已关
        窗口 a1b2a833，真身其实在接手者的新对话 2972e376，于是空壳收不起）。

        08-15：MCP 工具参数通常不进 composer 气泡，接手词正文却进。
        现任窗口改由 jsonl 里 CallMcpTool 的 conversation_id 认领；composer
        只认 tool_arg JSON。宁可晚收壳，不把「只贴了接手词当上下文」的窗口抢走。

        排除集合只挡「非空壳 tab 的已验证认领」（08-03 二修）：接手方的真对话
        正被它自己的报到空壳认领着——把所有别家 uuid 一刀切排除，等于把真身
        排除在外，兜底匹配只能落到被队友通知污染的无辜对话上（错绑 090b9fa3
        实案）。空壳的认领本来就该被接手方拿走，不能当排他。"""
        try:
            with self.lock:
                others = {x.cursor_uuid for x in self.sessions.values()
                          if x.cursor_uuid and x.id != s.id
                          and getattr(x, "uuid_verified", False)
                          and not self._is_checkin_shellish(x)}
            # 08-15：MCP 工具参数不进 composer 气泡，接手词正文却进。
            # 先查 jsonl 里 CallMcpTool 的 conversation_id；composer 只认
            # tool_arg JSON，避免把「贴了接手词当上下文」的窗口抢走。
            hit = jsonl_tool_arg_session_for_conv(
                getattr(s, "cwd", "") or "", s.conv_key, exclude=others)
            if not hit:
                hit = cursor_db_session_for_conv(
                    s.cwd, s.conv_key, exclude=others, param_context="tool_arg")
            if not hit:
                return
            if hit[0] == getattr(s, "cursor_uuid", None):
                s.uuid_verified = True
                return
            old = getattr(s, "cursor_uuid", None)
            with self.lock:
                # 原认领者（多半是自己的报到空壳）交出 uuid，防同一对话双认领
                for x in self.sessions.values():
                    if x.id != s.id and x.cursor_uuid == hit[0]:
                        x.cursor_uuid = None
                        x.transcript_path = None
                        x.uuid_verified = False
            s.cursor_uuid, s.transcript_path = hit[0], hit[1]
            s.uuid_verified = True
            log_event("接手重定位 tab={} uuid {}→{}（Cursor实时库确认含 {}）".format(
                s.name, (old or "无")[:8], hit[0][:8], s.conv_key))
        except Exception:
            pass

    # ---- 认领判定已抽到 session_core.py（第四刀批B），以下全部一行委托 ----
    # 「干过真活」的门槛：报到那一两句之下算空壳
    REAL_WORK_SEQ = session_core.REAL_WORK_SEQ

    @staticmethod
    def _real_seq(x):
        """tab 真正聊过多少句（第四刀已抽 session_core，此处委托）。"""
        return session_core.real_seq(x)

    def _did_real_work(self, x):
        return session_core.did_real_work(self, x)

    def _claimed_task(self, x):
        """「认领了活」判定——身份根治铁律核心（已抽 session_core，此处委托）。"""
        return session_core.claimed_task(self, x)

    def _mark_claimed(self, s, why=""):
        """钉「认领了活」标记（已抽 session_core，此处委托）。"""
        return session_core.mark_claimed(self, s, why)

    def _is_checkin_shellish(self, x):
        """「报到空壳」宽判（已抽 session_core，此处委托）。"""
        return session_core.is_checkin_shellish(self, x)

    @staticmethod
    def _is_checkin_pending(x):
        """挂着的 zhi 是报到壳那句，不是真提问（已抽 session_core）。"""
        return session_core.is_checkin_pending(x)

    def _is_takeover_shell(self, x):
        """「可收掉的待命空壳」严判（已抽 session_core，此处委托）。"""
        return session_core.is_takeover_shell(self, x)

    def _dismiss_shell_pending(self, x, succ_conv=""):
        """收壳前把报到 zhi 应答掉，避免 MCP 那头永远堵在提问上。

        只在确认要收这个壳之后调用。真提问不会走到收壳（_is_takeover_shell
        仍拦非报到 pending）。发送失败也清 pending：壳马上从列表消失，
        连不上的 agent 本来也不会再等到回复。

        **绝不能回 selected_options=["结束"]**（08-25 之前一直这么回）：那和 rxyy
        亲手点「结束」按钮在 agent 眼里一模一样，而报到卡的选项恰恰就是「开始任务/
        结束」。于是接手方蹲了 52 分钟的报到 zhi 一被收壳，读到的就是「用户点了
        结束」——它当场停手，接手提示词还在后任的队列里躺着没人取。选项留空，
        把「活在哪、下一步调什么」直接写进正文。
        """
        pending = getattr(x, "pending", None)
        if not isinstance(pending, dict):
            x.pending = None
            return
        req_id = pending.get("id")
        succ = str(succ_conv or "").strip()
        if req_id and getattr(x, "client", None) is not None:
            if succ:
                note = (
                    "[本待命壳已并入对话 {0}，这个标签收起了。**活没结束**：要接的"
                    "活和接手提示词都在 {0} 那边，请立刻改用 conversation_id=\"{0}\" "
                    "再调一次 zhi（message 留空、选项不变）把它取回来，接着干。"
                    "这不是 MCP 故障，也不是用户点了「结束」——别停手，"
                    "别断开rxyy MCP。]".format(succ))
            else:
                note = ("[该待命壳已交接，本标签已收起。这不是 MCP 故障，"
                        "也不是用户点了「结束」；你若手上还有别的 conversation_id，"
                        "继续用那个干活。]")
            try:
                x.send({
                    "type": "zhi_response",
                    "id": req_id,
                    "user_input": note,
                    "selected_options": [],
                    "images": [],
                    "source": "reaped",
                })
            except Exception:
                pass
        x.pending = None

    def _forward_shell_mail(self, x, succ):
        """把壳 x 队列里没送出去的话转给现任 succ，返回转走的条数。

        走 force=True：succ 可能正处在重连宽限里，而这些话本来就是冲着「这个
        agent」去的，人跟着活走了，话也该跟过去。
        """
        moved = 0
        for entry in list(getattr(x, "queued", None) or []):
            text = entry.get("text") or ""
            # 壳上卡着的「用户级规则」补送不要转给现任——现任早就过了开局，
            # 08-15 四个派接手壳各卡一条，转过去会在干活 tab 里连弹四份 v19。
            if "【用户级规则" in text or "用户级规则 · 必读必守" in text:
                continue
            try:
                r = Api().queue_message(succ.id, text,
                                        entry.get("images"), who=entry.get("who"),
                                        files=entry.get("files"), force=True)
                moved += 1 if r.get("ok") else 0
            except Exception:  # noqa: BLE001
                pass  # 转投失败不能挡住收壳本身
        if moved:
            with x.lock:
                x.queued = []
                for m in x.messages:
                    if m.get("queued"):
                        m["queued"] = False
        return moved

    def _reap(self, shell, succ, *, why, end_note, event="",
              land=True, unseal=True, forward="mail", persist=False,
              guard=True, succ_conv=""):
        """收壳统一引擎。四条触发器（派单 / 原ID报到 / 同窗证据 / 巡检）只负责
        找出 (shell, succ)，动手一律走这里——漏一步就是新事故。

        固化顺序：
          1. takeover_landed(succ)（派单当场归并不算落地，land=False）
          2. _retire_conv_into —— 路由立刻切到后任，壳 ID 继续当别名用
          3. _dismiss_shell_pending(shell, succ_conv) —— 答复报到 zhi，写明活在哪
          4. 从 client.sessions 摘掉；unseal 则撕掉 closed_convs 封条
          5. 转队列：mail=机器信顺给现任；raw=原条目搬迁（含接手词）
          6. disconnect / log_end / 从 sessions+order 删除
          7. _tombstone_shell

        收壳判定只认 is_takeover_shell。派单那条路在排队接手词之前已经拍过
        target_was_shell 快照，guard=False，避免「刚塞进接手词」被误判拦下。
        """
        if shell is None:
            return 0
        if guard and not self._is_takeover_shell(shell):
            return 0
        succ_obj = None if isinstance(succ, str) else succ
        succ_conv = str(succ_conv or (
            getattr(succ_obj, "conv_key", "") if succ_obj is not None else succ
        ) or "")
        if land and succ_obj is not None:
            self.takeover_landed(succ_obj)
        self._retire_conv_into(
            shell.conv_key, succ_conv,
            old_name=str(shell.name or ""),
            succ_label=str(getattr(succ_obj, "name", "") or ""),
            why=why)
        self._dismiss_shell_pending(shell, succ_conv)
        try:
            if shell.client is not None:
                if unseal:
                    shell.client.closed_convs.pop(shell.conv_key, None)
                shell.client.sessions.pop(shell.conv_key, None)
        except Exception:
            pass
        moved = 0
        if forward == "raw" and succ_obj is not None:
            with shell.lock:
                moved_entries = list(shell.queued or [])
                shell.queued = []
            if moved_entries:
                with succ_obj.lock:
                    succ_obj.queued = list(succ_obj.queued or []) + moved_entries
                succ_obj.rev += 1
            moved = len(moved_entries)
        elif forward == "mail" and succ_obj is not None:
            moved = self._forward_shell_mail(shell, succ_obj) or 0
        shell.connected = False
        # 曾用 ID 在引擎里写一份：_tombstone_shell 也会写，但派单测试把墓碑桩
        # 掉了（test_dispatch_to_shell_aliases_and_merges_it）——不在这里写，
        # 老 ID 转告就断。重复 append 有去重，无害。
        if (succ_obj is not None and shell.conv_key
                and shell.conv_key not in (getattr(succ_obj, "id_history", None) or [])
                and shell.conv_key != "__default__"):
            succ_obj.id_history = (
                list(getattr(succ_obj, "id_history", []) or [])
                + [shell.conv_key])[-8:]
        self.log_end(shell, end_note)
        with self.lock:
            self.sessions.pop(shell.id, None)
            if shell.id in self.order:
                self.order.remove(shell.id)
        self._tombstone_shell(shell, succ_obj)
        if event:
            log_event(event)
        if persist:
            threading.Thread(target=self.save_state, daemon=True).start()
        return moved

    def alias_shell_into(self, x, src):
        """派接手给待命壳的**当场归并**：壳的队列（含刚排入的接手提示词）搬给
        被接手的 tab，壳从列表消失；壳 ID 已被登记为 src 的接手别名，接手方
        带着壳 ID 来的每一次调用都自动落到 src——它什么都不用改。

        与 _reap_shell_into 的差别：那是「接手方已改用原 ID 说话之后」的事后
        收壳；这里是「派单那一刻」的事前归并（08-12 实测事后收壳等不来——
        接手方按常驻协议全程复用自己的首个 ID，永远不改）。且**绝不能设
        closed_convs**：这个壳 ID 还要继续用（作为别名活着），封了它下一次
        zhi 就会被拦截要求结束任务。"""
        # 登记落在引擎里的 _retire_conv_into（不放调用方）：手工复制接手词贴
        # 过去那条路不经 share_takeover，只有收壳这一跳能替壳 ID 记上账
        # （回归钉在 test_alias_shell_into_registers_the_alias_itself）。
        moved = self._reap(
            x, src,
            why="派接手即归并壳",
            end_note="派接手即归并：壳 ID {} 已成为 {} 的接手别名".format(
                x.conv_key, src.conv_key),
            land=False,          # 派单那一刻后任还没开口，⏳ 不能提前摘
            unseal=True,         # 撕封条，绝不新贴（壳 ID 还要当别名活着）
            forward="raw",       # 接手词必须原条目搬过去，mail 会跳过规则补送
            persist=True,
            guard=False)         # 派单前已拍 target_was_shell，队列刚塞进接手词
        log_event("派接手即归并 壳 tab={} conv={} → {}（队列随迁 {} 条）".format(
            x.name, x.conv_key, src.conv_key, moved))

    def _reap_shell_into(self, x, succ):
        """收起一个待命空壳 x（它那个 Cursor 对话已切用 succ 的原对话 ID 接手了）。"""
        self._reap(
            x, succ,
            why="同窗口切回原ID，待命壳收编",
            end_note="接手落地（同 Cursor 对话切到原对话 {}），空壳自动收起".format(
                getattr(succ, "conv_key", "")),
            event="手动接手落地，自动收起待命空壳 tab={} conv={}（并入 {}）".format(
                x.name, x.conv_key, getattr(succ, "conv_key", "")))

    def _handover_out(self, old, succ):
        """老 tab 的 Cursor 对话被证实改去驱动另一个会话了 = 它的 agent 已被派去接手
        别的活，这个 tab 再不会有人应答。

        与 _reap_shell_into 的分工：那个收的是「没干过活的待命空壳」，直接从列表删掉；
        这里的 old 有真实历史（08-04 实测的「团队面板修复」），删了就把上下文一起丢了，
        所以只归档进「已结束」组——消息、记录文件、右键接手都还在，面板也不再拿它当
        活人（此前它一直显示「疑似 Cursor 里还活着·通道未接」，用户以为接手没生效）。
        """
        if getattr(old, "archived", False) or old.pending is not None:
            return  # 还等着用户回话的不动它，免得把人家的提问埋进「已结束」组
        try:
            if old.client is not None:
                old.client.closed_convs[old.conv_key] = True
                old.client.sessions.pop(old.conv_key, None)
        except Exception:
            pass
        old.connected = False
        # 队列里没送出去的话顺到现任身上：转告本来就是冲着「这个 agent」去的，
        # 人跟着活走了，话也该跟过去（不然它永远躺在一个没人会再看的 tab 里）
        moved = 0
        for entry in list(getattr(old, "queued", None) or []):
            try:
                r = Api().queue_message(succ.id, entry.get("text") or "",
                                        entry.get("images"), who=entry.get("who"),
                                        files=entry.get("files"), force=True)
                moved += 1 if r.get("ok") else 0
            except Exception:
                pass
        with old.lock:
            old.queued = []
            for m in old.messages:
                if m.get("queued"):
                    m["queued"] = False
        # 与派单接手同义：这个 tab 的活已经交给 succ 那个对话了。往后队友再按老
        # ID/老名字转告，_follow_handoff 顺着它投给现任，不会再掉进这个没人看的 tab
        old.handed_off_to = succ.conv_key
        tail = "，{} 条没送出去的排队消息已转给它".format(moved) if moved else ""
        self.add_message(old, {
            "role": "sys", "ts": now_hms(),
            "html": "本 tab 的 agent 已接手 <b>{}</b>，这里不再有人应答{}".format(
                html_mod.escape(str(succ.name or succ.conv_key)), tail),
        })
        self.log_end(old, "活已交接给 {}（同一 Cursor 对话改驱动 {}）".format(
            succ.name, succ.conv_key))
        Api()._archive_tab(old, save=False)
        self._tombstone_shell(old, succ)
        log_event("接手交接 tab={} conv={} → {} conv={}{}".format(
            old.name, old.conv_key, succ.name, succ.conv_key,
            "，转走排队 {} 条".format(moved) if moved else ""))

    def _reap_takeover_shell(self, s):
        """手动接手落地后自动收起那个「待命空壳」——不靠派单也能收。

        用户的实操（07-31）：开个新 Cursor 对话 → agent 先自动报到成「待命·xxx」
        （空壳 A，新 ID）→ 用户把接手提示词贴进【同一个】对话 → agent 改用原对话 ID
        报到，原对话 B 复活。A 和 B 是同一个 Cursor 对话。

        怎么认出「A 是 B 的接手前身」（08-03 修对）：不靠 A 的 cursor_uuid——它会被
        _verify_identity_by_generating 认领 B 时清空（同一对话 uuid 被 B 拿走）。改用
        「A 的报到 ID 出现在 B 当前对话里」：A 是在 B 这个 Cursor 对话里先报到的，它的
        NEW_ID 必然落进该对话的 composerData bubble，用实时库一查便知，即时可靠。

        安全边界：只有 s 带真实历史才触发；只收待命空壳；每 15s 一次防 zt 高频刷库。"""
        if not self._did_real_work(s):
            return
        now = time.time()
        if now - float(getattr(s, "_reap_ts", 0) or 0) < 15:
            return
        s._reap_ts = now
        # 先把 s 的 cursor_uuid 修到接手者当前对话（它常停在被接手的旧对话）
        self._relocate_takeover_uuid(s)
        uid = getattr(s, "cursor_uuid", None)
        if not uid:
            return
        with self.lock:
            cands = [x for x in self.sessions.values()
                     if x.id != s.id and x.conv_key != s.conv_key
                     and self._is_takeover_shell(x)]
        for x in cands:
            # A 是 B 的接手前身 ⇔ 同一个 Cursor 对话：uuid 相同，或 A 的报到 ID 落在
            # B 当前对话的 composerData 里（A 的 uuid 被 _verify 清掉后靠这条兜底）。
            # 必须用参数上下文档：A 报到时它的 conversation_id 就在这个对话的 zhi
            # 参数里；而队友通知/团队快照里的裸 ID 会落进不相干对话（08-03 实测
            # 裸包含把两个还在等派活的待命 tab 连锅端错收）。
            same_dialog = getattr(x, "cursor_uuid", None) == uid
            if not same_dialog:
                try:
                    same_dialog = composer_mentions_conv(uid, x.conv_key,
                                                         param_context=True)
                except Exception:
                    same_dialog = False
            # 复制接手提示词贴进新窗口：现任 uuid 常停在旧对话，壳才是新窗口。
            # 只认壳窗口里 zhi/zt 工具参数 JSON（agent 真切到了原 ID），
            # 不认接手提示词正文——那串 conversation_id=「原ID」贴上去当
            # 上下文、并写明「不用接手就在本对话答」时，用 True 会把活 tab 误收。
            if not same_dialog:
                shell_uid = getattr(x, "cursor_uuid", None)
                if shell_uid:
                    try:
                        cwd = getattr(x, "cwd", None) or getattr(s, "cwd", "") or ""
                        tp = getattr(x, "transcript_path", None) \
                            or jsonl_path_for_uuid(cwd, shell_uid)
                        same_dialog = transcript_has_tool_arg(tp, s.conv_key)
                        if not same_dialog:
                            same_dialog = composer_mentions_conv(
                                shell_uid, s.conv_key, param_context="tool_arg")
                    except Exception:
                        same_dialog = False
            # 08-15：原会话自己复活后，被派去接手它的待命壳还在。那些壳是
            # 新开的 Cursor 窗口，uuid / composer 对不上；提示词里点名的
            # 原 ID 才是「它就是来接这个活的」铁证。
            if not same_dialog:
                same_dialog = (self._shell_takeover_target(x) == s.conv_key)
            if same_dialog:
                self._reap_shell_into(x, s)

    def _shell_takeover_target(self, x):
        """这个壳被派去接手的原对话 ID。

        B步（08-26）：证据只认两样——贴给它的接手提示词正文（队列+消息，
        派单那一刻写下的事实）与接手台账（A步落盘，跨重启）。不再读壳身上的
        handed_off_to：它如今只表示「交接在途」、落地即清（takeover_landed），
        拿单字段残留判归属，正是把正在干活的 73004179 併进别人 tab 的路数。"""
        for e in (getattr(x, "queued", None) or []):
            if isinstance(e, dict):
                tid = takeover_prompt_target(e.get("text") or "")
                if tid:
                    return tid
        for m in (getattr(x, "messages", None) or []):
            if not isinstance(m, dict) or m.get("role") != "user":
                continue
            tid = takeover_prompt_target(m.get("html") or "")
            if tid:
                return tid
        # 崩溃恢复/消息被裁后提示词可能不在了：台账里的退休登记还能指路
        entry = (getattr(self, "takeover_ledger", None) or {}).get(
            str(getattr(x, "conv_key", "") or ""))
        if isinstance(entry, dict):
            return str(entry.get("succ") or "")
        return ""

    def sweep_revived_takeover_shells(self):
        """原会话已经在干活时，收掉还挂着的「被派去接手它」的待命壳。

        收壳原先只在原会话 zhi/zt 那一刻靠 uuid 对窗口。原会话自己重连复活、
        接手词贴进另一个新窗口时，那一刻对不上，壳就永远留着（08-15 现场：
        录播/直播/MCP长连都活了，底下还挂着 2f47/6813/d271/ed54 四个待命）。
        """
        now = time.time()
        if now - float(getattr(self, "_takeover_sweep_ts", 0) or 0) < 8:
            return
        self._takeover_sweep_ts = now
        with self.lock:
            items = list(self.sessions.values())
        live = {s.conv_key: s for s in items
                if s.conv_key and self._did_real_work(s)}
        reaped = 0
        for x in items:
            if x.conv_key in live:
                continue
            if not self._is_takeover_shell(x):
                continue
            succ = live.get(self._shell_takeover_target(x) or "")
            if succ is None:
                continue
            self._reap_shell_into(x, succ)
            reaped += 1
        if reaped:
            self.save_state()

    def _ensure_listed(self, s):
        """被误收的壳（还挂在 MCP 连接上、不在列表里）下一声 zhi/zt 拉回侧栏。

        09-07：批量新建回执超时后 _ext_drop_shell 把已报到的 tab 从 sessions 摘掉，
        agent 的 zhi 还堵在这个 Session 上，控制台绿灯闪一下就没了。
        """
        if s is None or getattr(s, "archived", False):
            return False
        with self.lock:
            if s.id in self.sessions:
                return False
            self.sessions[s.id] = s
            if s.id not in self.order:
                self.order.append(s.id)
        log_event("误收壳自愈，tab 回到列表 conv={} tab={}".format(s.conv_key, s.name))
        threading.Thread(target=self.save_state, daemon=True).start()
        return True

    def resolve_session(self, client: Client, conv_key, task_name, metadata=None):
        """按 conv_key 找 tab。

        注意：同一 MCP 进程（同一连接）可能同时服务多个 Cursor 对话，
        绝不能把不同 conversation_id 合并到同一个 tab，否则两个对话互相串消息。
        """
        self._reap_handed_off_shell(client, conv_key)
        s = client.sessions.get(conv_key)
        if s is not None:
            runtime_adapter.apply_metadata(s, metadata or {})
            # zhi 是 agent 主动带着精确对话 ID 回来，不属于换装被动重连。
            s.auto_reconnect_blocked = False
            self._ensure_listed(s)
            # 拿这个 ID 调 zhi 的人就是活着的接手方（或回来的原主）——派出去的
            # 接手到此为止，别再让它 15 分钟后发假告警（见 takeover_landed）
            self.takeover_landed(s)
            self.maybe_apply_task_name(s, task_name)
            # 只接受用户仓库。hello/getcwd 落到 live 常驻区时 sanitize 为空，
            # 不得覆盖会话上一次的真工作区（08-21：重启后工单 tab 人在rxyy MCP）。
            apply_session_cwd(s, client.cwd)
            if not getattr(s, "task_root", ""):
                cleaned = sanitize_ws_path(client.cwd)
                if cleaned:
                    s.task_root = cleaned
            heal_session_paths(s)
            self._bind_registered_team_root(s)
            heal_named_agent_root(s)
            # 报到时不知道工作区、名字还顶着「待命·workspace」的壳：这次调用
            # 但凡带上了真目录（project_path / 项目级 MCP 的 cwd），当场补正
            self.heal_standby_tab_name(s)
            try:
                Api()._follow_seat_to_named_project(s)
            except Exception:
                pass
            return s
        if metadata is None:
            return self.create_session(client, conv_key, task_name)
        return self.create_session(client, conv_key, task_name, metadata=metadata)

    def create_session(self, client: Client, conv_key, task_name, metadata=None):
        # 显式 conversation_id：优先复活同一对话的断开 tab（MCP 重连后保持连续）。
        # 复活条件 = 同对话ID + 同来源(peer_ip) + 已断开。conv_key 是 8 位随机 hex，
        # 已足够唯一——【绝不再要求 cwd/pid 相等】：Cursor 的共享 MCP 进程会让一个
        # server.py 同时服务多个窗口，hello 的 cwd 在多对话间交替抖动、pid 每次回收都变，
        # 之前用 cwd/pid 做复活门槛正是"回复没回原对话、反而开新 tab"的根因。
        # 同 cwd 的候选仍优先选中（下面的 max key）。
        if conv_key != "__default__":
            # 同对话ID + 同来源的会话【无论断开还是仍标着连接】都收编，绝不重复建 tab：
            # 重试/重连会让同一对话经由新的 Client 连接再次到达，旧会话此刻往往还挂着
            # connected=True——若只认断开的，就会给同一个 conversation_id 开出第二个
            # tab（实测 16:00 同一对话冒出「新任务」「新任务2」两个会话）。
            with self.lock:
                cands = [x for x in self.sessions.values()
                         if x.conv_key == conv_key and x.peer_ip == client.peer_ip]
            if cands:
                s = max(cands, key=lambda x: (not x.connected, x.cwd == client.cwd,
                                              x.pid == client.pid, x.created_at))
                if getattr(s, "handoff_final", False) and getattr(s, "archived", False):
                    # 多选接手已把原对话钉在「已结束」：原 ID 再报到不得复活旧 tab。
                    # 有后任就改走后任（并登记别名）；没有后任就原样返回归档对象。
                    succ = self._live_handoff_successor(s)
                    if (succ is not None and succ is not s
                            and str(getattr(succ, "conv_key", "") or "")
                            and succ.conv_key != conv_key):
                        try:
                            self._retire_conv_into(
                                conv_key, succ.conv_key,
                                old_name=s.name, succ_label=succ.name,
                                why="handoff_final 原 ID 报到改走后任")
                        except Exception:
                            pass
                        return self.create_session(client, succ.conv_key, task_name)
                    log_event("handoff_final 拒绝复活 conv={} tab={}".format(
                        conv_key, s.name))
                    return s
                was_connected = s.connected
                old_pid = s.pid
                s.client = client
                s.peer_ip = client.peer_ip
                if client.cwd:
                    # cwd 跟着接手方走（须是用户仓库），task_root 不动：活仍是原项目的
                    apply_session_cwd(s, client.cwd)
                    if not getattr(s, "task_root", ""):
                        cleaned = sanitize_ws_path(client.cwd)
                        if cleaned:
                            s.task_root = cleaned
                heal_session_paths(s)
                self._bind_registered_team_root(s)
                if client.pid and old_pid and client.pid != old_pid:
                    # MCP 进程重启：把窗口标记转给新 pid，窗口①不因重启变成②
                    self._migrate_window_tags(old_pid, client.pid)
                s.pid = client.pid
                s.connected = True
                s.auto_reconnect_blocked = False
                s.archived = False  # 原对话ID又报到了 = 用户把它接回来了，撤销归档
                self.takeover_landed(s)  # 有人拿原 ID 报到 = 接手已落地/原主回归
                s.handed_off_to = ""  # 交接关系随复活失效：残留会让活 tab 被标成
                                      # 「已交接」、按它旧 ID 的转告被错误转走
                s.end_reason = ""
                s.pending_lost = False
                s.recon_deadline = None
                s.lost_pending_on_drop = False
                s.detached = False
                s.detached_since = None
                if s.pending is None:
                    # 提问已不在（断线时清掉）却还存着回复 = 孤儿：转队列别丢，
                    # 下次落卡由 _flush_queue 送出（正常路径 _client_disconnect 已先
                    # 转过一次，这里只兜快照恢复等旁路）
                    if getattr(s, "buffered_reply", None) is not None:
                        self._requeue_buffered_reply(
                            s,
                            note="AI 没来取你的回复就断了链接：那条回复已转入队列，"
                                 "它下次提问时自动补送。",
                            prefix="[上一条提问的回复|AI 只发不等、没来取就断了，用户已回话] ",
                            why="复活时发现孤儿缓存")
                    s.wait_deferred = False
                # 提问还挂着（前任只发不等、还没判死，新进程拿同一 ID 来了）：
                # buffered_reply / wait_deferred 原样保留——它这次多半就是来收回复的，
                # zhi_request 的续期路径当场交付；发的是新提问也由 _requeue_buffered_reply
                # 转队列补送。以前这里一律置 None，用户回过的话在接手瞬间蒸发
                s.processing_since = None
                s.disconnected_at = None  # 复活即清：防卡死自愈拿旧断开时刻误判「仍卡着」
                client.sessions[conv_key] = s
                runtime_adapter.apply_metadata(s, metadata or {})
                self.maybe_apply_task_name(s, task_name)
                self.heal_standby_tab_name(s)
                if not was_connected:
                    self.add_message(s, {"role": "sys", "ts": now_hms(), "html": "连接已恢复（MCP 重连）"})
                    log_event("会话复活 conv={} tab={} pid {}→{}".format(
                        conv_key, s.name, old_pid, client.pid))
                else:
                    log_event("会话收编（同对话重复接入，不另开 tab） conv={} tab={}".format(
                        conv_key, s.name))
                # 复活同样是「一批调用正在往里挤」的信号（接手报到、守护进程重启后
                # 各 agent 重呼），给同连接等待中的 zhi 让路，别让兄弟们卡在门外
                self.yield_zhi(client, exclude_conv=conv_key)
                return s
        base = (task_name or "").strip() or (Path(client.cwd).name if client.cwd else "") or "会话"
        s = Session(client, conv_key, base[:40])
        runtime_adapter.apply_metadata(s, metadata or {})
        self._bind_registered_team_root(s)
        with self.lock:
            s.name = self._dedupe_name(s.name, conv_key=conv_key)
            self.sessions[s.id] = s
            self.order.append(s.id)
        client.sessions[conv_key] = s
        self._init_file(s)
        # 开场公告：窗口标记 + 项目 + 完整路径 + 对话 ID，让用户不依赖 agent 自觉就能对上号
        src = f"来自 {s.peer_ip} · " if s.peer_ip else ""
        wtag = self._window_tag(s.pid, s.cwd, getattr(s, "transcript_path", None))
        wpart = f"窗口{wtag} · " if wtag else ""
        proj = html_mod.escape(Path(s.cwd).name if s.cwd else "未知项目")
        idpart = ("" if conv_key == "__default__"
                  else f" · 对话ID <code>{html_mod.escape(conv_key)}</code>")
        self.add_message(s, {
            "role": "sys", "ts": now_hms(),
            "html": (f"对话已开始 · {src}{wpart}<b>{proj}</b>{idpart}"
                     f"<br>{html_mod.escape(s.cwd or '未知目录')}"),
        })
        log_event("新会话 conv={} tab={} cwd={}".format(conv_key, s.name, s.cwd))
        # 立刻落一次快照：15s 循环的空档里 hub 重启/被杀，这个刚报到的会话
        # 就不在快照里，重启后 tab 人间蒸发（07-31：重启后 4 个待命 tab 丢失）
        threading.Thread(target=self.save_state, daemon=True).start()
        # 新报到到达 = 同批可能还有兄弟会话被同一共享 MCP 的长等待 zhi 堵在门外，
        # 通知该连接上等待中的 zhi 立刻续期让路，后续报到几秒内就能进来。
        # 一次让路只放行「此刻已排队」的；同批报到往往陆续到达，故顺带开一小段
        # 轮转窗口，让接下来一两分钟里的兄弟们也不用等
        self.yield_zhi(client, exclude_conv=conv_key)
        self.start_yield_burst(90)
        return s

    def create_spawn_shell(self, conv_key, cwd, name, note=None):
        """「拉起本机 agent」当场预建一个断开态的会话壳（tab 立即可见）。

        为什么必须有它：拉起的无头 agent 按报到纪律先调【阻塞的】zhi
        「待命等派任务」——任务只拼在提示词尾部的话，没人回复 zhi，agent 会被
        KEEPALIVE 圈在原地永远开不了工。预建壳后把首个任务排进队列，报到那
        一下 zhi 立即拿到任务（复活匹配：同 conv_key + 本机 peer_ip=None）。

        note：开场系统气泡的文案；不传沿用「本机 agent 拉起中」那句。窗口总线
        无头开的 Cursor 对话（ext_batch_open）也走这里预建壳，文案不同。"""
        shim = type("ShellClient", (), {
            "cwd": sanitize_ws_path(cwd or ""), "pid": None, "peer_ip": None})()
        s = Session(shim, conv_key, str(name or "内置 agent")[:40])
        s.client = None
        s.connected = False
        s.end_reason = ""
        # runner 启动 + agent 首呼要几十秒：宽限内 UI 显示「重连中」而不是黑点
        s.recon_deadline = time.time() + 180
        with self.lock:
            s.name = self._dedupe_name(s.name, conv_key=s.conv_key)
            self.sessions[s.id] = s
            self.order.append(s.id)
        self._init_file(s)
        self.add_message(s, {
            "role": "sys", "ts": now_hms(),
            "html": str(note or "本机 agent 拉起中（runner 已启动）· 首个任务已排队，报到即送达"),
        })
        log_event("预建内置壳 conv={} tab={} cwd={}".format(conv_key, s.name, s.cwd))
        threading.Thread(target=self.save_state, daemon=True).start()
        return s

    def _note_model(self, s, raw=None, force=False):
        """把 agent 报的模型记下，需要时立刻重探牌。

        zt/zhi 到达 ≈ 用户刚发过话，常常刚切过档。Cursor 侧清限频再读库；
        Codex 侧没有库，就靠这一口把牌点亮。
        """
        name = str(raw or "").strip()[:80]
        if name and name != getattr(s, "reported_model", ""):
            s.reported_model = name
            force = True
        if force:
            s.model_probe_at = 0.0
        try:
            self._tick_model_probe(s, time.time())
        except Exception:
            pass

    def _known_ws_roots(self):
        """控制台手上这份「本机真实存在过的工作区」名单。"""
        roots = []
        try:
            for x in list(self.sessions.values()):
                for attr in ("cwd", "task_root"):
                    v = sanitize_ws_path(getattr(x, attr, "") or "")
                    if v:
                        roots.append(v)
        except Exception:
            pass
        try:
            for info in (self.cfg.get("team_project_members") or {}).values():
                if isinstance(info, dict) and info.get("root"):
                    roots.append(str(info["root"]))
        except Exception:
            pass
        return roots

    def _standby_cwd_from_transcript(self, s):
        """壳从没带过 project_path 时，拿它 Cursor 流水所在的工作区反查真目录。

        「+ → 复制」时用户没选目录（实测最常用的一条路），控制台根本不知道这段
        报到词会被粘到哪个窗口；壳自己也不一定说得出来（子代理的上下文里没有
        工作区路径）。但它一开口，流水就落在自己窗口的 .cursor/projects/<slug>
        槽位下——这是铁证。

        slug 是有损的（中文目录被压掉，D:\\Desktop\\cursor工作流 → d-desktop-cursor），
        不能直接反解成路径；对着已知工作区名单正算一遍 slug 才能精确认出是哪个。
        同一个 slug 撞上两个已知目录时一律放弃：纯中文目录名同级并列就会撞
        （D:\\Desktop\\视频快编 与 D:\\Desktop\\音频快编 都是 d-desktop），
        猜错就是把 tab 名改成隔壁项目，比继续叫 workspace 还糟。
        """
        slug = self._transcript_slug(getattr(s, "transcript_path", "") or "")
        if not slug:
            return ""
        hits = set()
        for root in self._known_ws_roots():
            try:
                if cursor_project_slug(root) == slug:
                    hits.add(os.path.normcase(os.path.normpath(root)))
            except Exception:
                continue
        if len(hits) != 1:
            return ""
        for root in self._known_ws_roots():
            if os.path.normcase(os.path.normpath(root)) in hits:
                return root
        return ""

    def heal_standby_tab_name(self, s: Session):
        """报到时还不知道工作区、名字先顶着「待命·workspace」的壳，
        等真工作区落地就把 tab 名补正成「待命·<工作区>·<对话ID前4位>」。

        08-28 用户截图：四个新壳的 tab 全叫「待命·workspace」，分不出谁是谁。
        报到那一刻控制台确实不知道目录（「+ → 复制」没选），但工作区随后总会
        到——agent 下一次调用带上 project_path、项目级 MCP 的 hello 报上来、
        或者它的 Cursor 流水被定位到某个工作区槽位。补正只动占位名：
        用户锁过名字的、agent 自报过真名的、已经认领了活的一律不碰
        （「待命·xxx」在真干活的 tab 上是降级，08-25 出过事故）。
        """
        if getattr(s, "name_locked", False) or getattr(s, "agent_named", False):
            return False
        if not standby_name_is_placeholder(getattr(s, "name", "")):
            return False
        if not self._is_checkin_shellish(s):
            return False
        cwd = (sanitize_ws_path(getattr(s, "cwd", "") or "")
               or self._standby_cwd_from_transcript(s))
        ws = os.path.basename(str(cwd or "").rstrip("\\/"))
        if not ws or ws.lower() in _PLACEHOLDER_WS:
            return False
        want = checkin_chat_title(getattr(s, "conv_key", "") or "", cwd)
        old = s.name
        with self.lock:
            s.name = self._dedupe_name(want, exclude_id=s.id, conv_key=s.conv_key)
        if s.name == old:
            return False
        s.rev += 1
        if s.file_path:
            self._append_file(
                s, f"\n> 标签改名（工作区补正）: {old} → {s.name} · {now_full()}\n")
            self._rename_history_file(s)
        log_event("报到壳工作区补正 {} → {} cwd={}".format(old, s.name, cwd))
        return True

    def maybe_apply_task_name(self, s: Session, task_name):
        """AI 后续调用补充/更新 task_name 时同步 tab 标题（用户手动改名后不覆盖）。

        08-07 用户实测「一排 tab 的名字全是我第一句话的前 24 个字」，根子是
        agent 自己报的名字被两头盖掉，改了也白改：
        ① `s.cursor_title` 一旦有值就直接 return —— Cursor 给报到对话自动生成的
           标题（实测「Persistent task reporting」这种英文功能描述）同步过来后，
           agent 此后永远改不动自己的名字；
        ② 就算改了 s.name 也不显示 —— session_label() 只要看见「分工」就整个
           绕开 s.name，而分工是派活时把用户那句话截 24 字填的。
        现在反过来：**agent 明说的 task_name 最权威**（只有它知道自己在做哪个
        项目的哪块功能），盖过 Cursor 自动标题，并顶掉那条自动截来的分工。
        """
        name = (task_name or "").strip()[:40]
        if not name or s.name_locked or s.name == name:
            return
        # agent 照抄 Cursor 自动标题当 task_name 报上来（08-28 09:07 实测：
        # 「agent 自报名字「Persistent plus task report」，撤掉自动占位分工」）。
        # 收下就是三重损失：tab 名变成全场同款的英文模板句、agent_named 立起来
        # 让 Cursor 标题同步这条路彻底失效、_push_name_to_cursor 还把它写回
        # Cursor 库。用户 08-27 抱怨的「一排 Persistent plus，你看这就有点混乱了」
        # 里有一半是这么来的——自动标题绕一圈从 agent 嘴里回流。
        if self._title_is_boilerplate(name):
            log_event("拒收自动标题当名字 tab={} 报的是「{}」".format(s.name, name))
            return
        # 「待命·xxx」是报到时的占位名，不含任何信息，只能往上盖不能往下降。
        # 08-25 12:26:22 实测：接手把壳 ID 别名并进 b4eff2ee 之后，继任 agent 手上
        # 那份报到提示词还在，它每次 zhi 仍带 task_name=「待命·cursor工作流」——
        # 一个干了半个月活的 tab 就此被改名成「待命·cursor工作流·f2ee」。
        # 代价远不止标题难看：常驻协议第 0 条规定「名字以待命打头 = 报到壳，
        # 用户点开始任务前只调 zhi、禁止读文件」，于是下一个读到这个 tab 的
        # agent（包括它自己下一轮）一律认为自己在待命，只回一句「已就位」就不动了
        # ——用户看到的就是「接手完就失忆」。认领过活的 tab 一律拒绝降级成壳。
        if CHECKIN_SHELL_NAME_RE.match(name) and self._claimed_task(s):
            return
        # agent 自报的真名（不是报到壳名）：立此存照，从此
        # ① sync_cursor_titles 不再拿 Cursor 自动标题冲它；
        # ② session_label 让位给它，不再显示那截自动分工。
        if not CHECKIN_SHELL_NAME_RE.match(name):
            # 已有团队身份的 tab 才谈得上「换项目卸任」；首次从壳名立真名的
            # 不算（用户可能刚在面板给这个壳预设了角色，那是给新团队的任命）
            old_scope_key = (Api()._team_project(s)[0]
                             if getattr(s, "agent_named", False) else None)
            s.agent_named = True
            self._mark_claimed(s, "agent 自报了真名")
            s.agent_project = _project_of_task_name(name)
            if old_scope_key is not None:
                self._retire_team_role_on_move(s, old_scope_key)
            Api()._drop_auto_assign(s)
            try:
                Api()._follow_seat_to_named_project(s)
            except Exception:
                pass
        # 当前名已是同一 base 的去重变体（如「新任务1」之于「新任务」）= 无实质改名，
        # 跳过——否则并发报到时每次 zhi 都会重新去重出一个新编号（改名风暴）
        if re.fullmatch(re.escape(name) + r"\d+", s.name or ""):
            return
        old = s.name
        with self.lock:
            s.name = self._dedupe_name(name, exclude_id=s.id,
                                       conv_key=s.conv_key)
        if s.name == old:
            return
        s.rev += 1
        if s.file_path:
            self._append_file(s, f"\n> 标签改名: {old} → {s.name} · {now_full()}\n")
        self._push_name_to_cursor(s)

    def _push_name_to_cursor(self, s):
        """把 tab 名回写进 Cursor 的会话标题。改名那一刻调，标题同步循环里也每拍调。

        必须每拍都对齐，不能只挂在「改名那一刻」这一个边沿上——cursor_uuid 是
        **后来才绑上、而且会重绑的**：agent 头一次报真名时它多半还是空的（要等
        身份自校准或定位循环认出窗口），而接手、换窗口、uuid 挪位都会让它指向
        另一个 composer。名字此后不再变，那条边沿就永远不再触发，新 composer
        于是一辈子顶着 Cursor 自动起的标题。

        08-25 现场清点：42 个已自报真名且已绑 uuid 的会话，库里标题只有 1 个跟
        tab 名对得上，其余全是「Persistent plus zhi report」——用户看到的就是
        侧栏一整排同名。库里查得到旁证：「rxyy tools·全面体检」确实被写进过
        composer c6c4d53a，可该会话现在绑的是另一个 composer。写得进去，只是
        写晚一步就再也不补，所以现象是「有几个改成功了，大部分没有」。

        **不许记账「这个已经写过了」**。改成每拍对齐之后，一度用 (uid, 名字) 记过
        一笔账来省调用，当天 22:26 的实验证明那是错的——写完 90 秒再读：

            uid=d154b3a0 'rxyy tools·全面体检' → 'Persistent plus zhi report'  被盖回
            uid=e3ccc6c5 '视频快编·ctest收尾'  → 'Persistent plus zhi report'  被盖回
            uid=f2c959c2 '心理后端·接口补齐'  → '心理后端·接口补齐'            还在

        前两个正开在 Cursor 里：Cursor 会把内存中的 composer 状态刷回磁盘，连
        标题一起盖。记了账就永不补写，用户看到的还是一排自动标题。去重下沉给
        write_cursor_title 自己做——它本来就先读后比，名字一样时不发 UPDATE，
        代价只有一次主键 SELECT。
        """
        if runtime_adapter.is_native(s):
            return
        if not (getattr(s, "agent_named", False) or getattr(s, "name_locked", False)):
            return
        if CHECKIN_SHELL_NAME_RE.match(s.name or ""):
            return
        uid = getattr(s, "cursor_uuid", None)
        if not uid:
            try:
                located = locate_cursor_session(
                    s.cwd, s.conv_key, getattr(s, "created_ts", None))
            except Exception:
                located = None
            if located:
                s.cursor_uuid, s.transcript_path = located
                uid = s.cursor_uuid
        if not uid:
            return
        try:
            # 上一拍已经把这个名字写进库了，这一拍库里却不是它——只有一种解释：
            # Cursor 把内存那份刷回磁盘、连名字一起盖了回来（见上面 08-25 现场）。
            # 也就是说这个对话还开着，磁盘这条路对它永远赢不了：Cursor 本体里
            # renameComposer 只动 composerDataService 的内存 store，磁盘是影子。
            # 改得动内存的有两条路，都不在这个循环里：
            #   ① cursor-app-control.rename_chat——只认调用方自己的
            #      conversationId（handleRenameChat 拿不到就报错），所以只有这个
            #      tab 自己的 agent 干得了。催它，就是下面这句。
            #   ② 设置面板「Cursor 侧栏名字·把跑偏的改回来」——走 Cursor 自己的
            #      composer.renameChat 命令，能指名道姓改任何 composerId
            #      （见 Api.fix_cursor_sidebar / cursor_live_rename）。那条要抢
            #      一两秒键盘焦点，只能手动按，不能挂在这个每拍都跑的循环上。
            stale = ""
            if getattr(s, "cursor_title", None) == s.name:
                cur = (read_cursor_title(uid) or "").strip()
                if cur and cur != s.name:
                    stale = cur
            # 「写了又被盖」这个信号要留在会话上给 _auto_fix_sidebar 用：它跑在
            # 这一拍的末尾，那时库里躺着的是我们刚写的名字，再读一遍看不出跑偏。
            s.sidebar_stale = stale
            if write_cursor_title(uid, s.name):
                s.cursor_title = s.name
            # 控制台自己能改（sidebar_auto_rename）就不催 agent：这台 Cursor 3.17 的
            # 工具表里没有 cursor-app-control.rename_chat，催一次就换回一句
            # 「本环境没有…」，白占对话（0902 rxyy 截图）。_auto_fix_sidebar 会在
            # 用户空闲时自己按下去。
            if stale and not self._sidebar_autofix_ready():
                Api()._nudge_cursor_rename(s, stale)
        except Exception as exc:
            log_event("回写 Cursor 标题失败 uuid={} err={!r}".format(uid, exc))

    def _retire_team_role_on_move(self, s, old_scope_key):
        """换项目 = 离开原团队：卸下原团队的角色/分工/业务线。

        角色/分工按对话 ID 记，而项目归属跟着 task_name 的项目前缀走——人被派去
        别的项目后「负责人」徽章会一路粘着（08-12 实测：上午 rxyy tools 组的
        owner 改名去 index-tts2，新项目组里凭空顶着负责人，其实新团队从没任命过
        它）。席位/intake 显式登记的成员归属不随改名变（_team_project 优先级
        更高），生效归属没变就不触发，天然不误伤。"""
        try:
            new_key = Api()._team_project(s)[0]
            if new_key == old_scope_key:
                return
            conv = getattr(s, "conv_key", "") or ""
            if not conv:
                return
            dropped = []
            with self._team_project_lock:
                for cfg_key, label in (("team_roles", "角色"),
                                       ("team_assign", "分工"),
                                       ("team_assign_auto", ""),
                                       ("team_tracks", "业务线")):
                    m = dict(self.cfg.get(cfg_key) or {})
                    if conv in m:
                        m.pop(conv, None)
                        self.cfg[cfg_key] = m
                        if label:
                            dropped.append(label)
                if dropped:
                    save_config(self.cfg)
            if dropped:
                self.add_message(s, {
                    "role": "sys", "ts": now_hms(),
                    "html": "已随项目切换卸下原团队的{}（新团队的角色请在团队面板重新任命）"
                        .format("、".join(dropped)),
                })
                log_event("项目切换卸任 tab={} 卸下{}（{} → {}）".format(
                    s.name, "、".join(dropped), old_scope_key, new_key))
        except Exception as e:  # noqa: BLE001
            log_event("项目切换卸任异常（已忽略）: {}".format(e))

    # ---------- 会话消息 ----------
    def add_message(self, s: Session, msg):
        with s.lock:
            # 控制台消息操作（编辑/删除/收藏）靠稳定 mid；老快照没有就补一个
            if isinstance(msg, dict) and not msg.get("mid"):
                msg["mid"] = uuid.uuid4().hex[:12]
            s.messages.append(msg)
            overflow = len(s.messages) - int(self.cfg.get("max_messages", 20))
            if overflow > 0:
                del s.messages[:overflow]
            s.rev += 1
            if msg.get("kind") != "status":
                is_machine = Api._is_machine_msg(msg)
                prior_real = self._real_seq(s) if not is_machine else None
                s.msg_seq += 1  # 状态上报不算新消息，别把未读角标灌成 9+
                if is_machine:
                    # getattr 兜底：修复前落盘的快照里没有这个字段
                    s.machine_seq = int(getattr(s, "machine_seq", 0) or 0) + 1
                else:
                    # 这不是 msg_seq - machine_seq：前者会随着消息数组裁短而无法
                    # 重建，真实工作量必须独立单调累计。
                    s.real_seq = prior_real + 1

    @staticmethod
    def _answers_this_question(entry, pending):
        """排队里的消息，答的是不是眼前这道题（决策卡扣住规则的放行口）。

        普通提问：5 秒缓冲消息带 reply_to=request id，只有那道题仍挂着时才放行。
        决策卡：reply_to 也不算数——文字答不了卡，必须走 answer_card，排队的话
        答卡时再捎带。补送件（redelivery）仍带原题正文+选项，agent 原样重问时
        才算现成答案。宁可扣住，也不能让旧消息把新决策卡抢答掉。"""
        if not isinstance(entry, dict):
            return False
        pend = pending or {}
        # 决策卡必须走 answer_card：5 秒缓冲 / 提前排队的文字答不了卡上的题。
        # 09-07 rxyy：卡还挂着时发送条一到点就把卡秒关，选项没一起送出去。
        # 放行只留给「同一道题的补送」（agent 原样重问）。
        if pend.get("card"):
            if not entry.get("redelivery"):
                return False
            q = entry.get("question")
            if q is None:
                return False
            return (str(q) == str(pend.get("message") or "")
                    and list(entry.get("q_options") or []) == list(pend.get("options") or []))
        reply_to = entry.get("reply_to")
        if reply_to is not None:
            return str(reply_to) == str(pend.get("id") or "")
        if not entry.get("redelivery"):
            return False
        q = entry.get("question")
        if q is None:
            return False
        return (str(q) == str(pend.get("message") or "")
                and list(entry.get("q_options") or []) == list(pend.get("options") or []))

    def _flush_queue(self, s: Session):
        """AI 发来新提问时，若队列里有用户提前发送的消息，自动合并送出。返回是否已发送

        队列里全是队友转告/控制台回执时不算数：那样 agent 刚把问题问出口就被队友
        的话「答」掉了，用户根本没机会看见（08-04 实测，一轮里连中两次）。它们等
        用户真回话时被捎带出去，见 Api.send_reply。

        新提问带决策卡时同样不算数（除了答这道题的补送件）：排队的话是用户看到卡
        之前说的，答不了卡上的问题——照旧当场答上去，卡就在用户眼前闪一下被一句
        陈旧的「继续」关掉（09-02 16:56 / 17:09 / 17:49 三张卡全这么死的）。扣住、
        卡保持打开，用户答卡时随答复一起捎带。"""
        stale = []
        with s.lock:
            if not s.pending or not s.queued:
                return False
            # 过期补送剔除（死会话处置③）：rescue 入队之后用户又真实回复过 =
            # 对话已进入新轮次，那条补送只剩重复价值——不许再占「用户回复」的位
            # 把 agent 刚问出口的问题答掉（08-13 实证：16:23 的 zhi 被 10:55 入队
            # 的旧补送顶掉，真回复没了着落）。剔除后气泡标过期，锁外补系统备注。
            last_reply = float(getattr(s, "last_reply_ts", 0) or 0)
            if last_reply:
                stale = [e for e in s.queued
                         if e.get("redelivery")
                         and last_reply > float(e.get("ts") or 0)]
                if stale:
                    s.queued = [e for e in s.queued if e not in stale]
                    qids = {e.get("id") for e in stale if e.get("id")}
                    for m in s.messages:
                        if m.get("qid") in qids:
                            m["queued"] = False
            # 剔完后没有可占「用户回复位」的消息（剔空了/只剩机器转告）：
            # pending 保持挂着等真实回复，机器信照旧等 zt 顺路捎带
            pending_taken = s.pending
            card = (pending_taken or {}).get("card")
            held = 0
            now = time.time()
            ready = [e for e in s.queued if Api._queue_defer_expired(e, now)]
            ready_users = [e for e in ready
                           if Api._may_take_the_reply_slot(e.get("who"))]
            if not ready_users:
                entries = None
            elif card and not any(self._answers_this_question(e, pending_taken)
                                  for e in ready_users):
                # 决策卡片：扣住不答、卡保持打开（见 docstring）；用户答卡时随答复
                # 一起捎带（Api.send_reply）。只放行显式答当前 request 的缓冲消息，
                # 或「同一道题的补送」（_answers_this_question）
                entries = None
                held = len(ready_users)
                if pending_taken.get("held_noted"):
                    held = 0
                else:
                    pending_taken["held_noted"] = True
            else:
                entries = ready
                s.queued = [e for e in s.queued if e not in entries]
                req_id = pending_taken["id"]
                # 等待方已脱离（只发不等的提问生来就是脱离态）：socket 那头没人等，
                # 直发会被丢。改存 buffered_reply、提问保留，agent 来收时原样交付
                buffer_only = bool(getattr(s, "detached", False))
                if not buffer_only:
                    s.pending = None
                    s.processing_since = time.time()
        if stale:
            self.add_message(s, {
                "role": "sys", "ts": now_hms(),
                "html": "已跳过 {} 条过期补送（你其后已有新回复，避免它顶掉"
                        "当前提问的回复位）".format(len(stale)),
            })
            log_event("过期补送剔除 conv={} 条数={}".format(s.conv_key, len(stale)))
        if held:
            self.add_message(s, {
                "role": "sys", "ts": now_hms(),
                "html": ("{} 条排队消息已扣住、没拿去答这张决策卡（那是你看到卡之前说的，"
                         "答不了卡上的问题）；你答卡时会随答复一起送给 AI").format(held),
            })
            log_event("决策卡扣住排队消息 conv={} tab={} 条数={}".format(
                s.conv_key, s.name, held))
        if entries is None:
            return False
        texts, selected, images, files = [], [], [], []
        for e in entries:
            if e.get("text"):
                # 局域网同事的消息带上昵称，让 AI 和记录都能区分提问人
                texts.append(f"[{e['who']}] {e['text']}" if e.get("who") else e["text"])
            for option in e.get("selected") or []:
                if option not in selected:
                    selected.append(option)
            images.extend(e.get("images") or [])
            files.extend(e.get("files") or [])
        user_input = "\n\n".join(texts) if texts else None
        # 没选项时保持旧调用形状；历史测试/外部小扩展常把这个 helper 打成
        # 只接 (session, text) 的桩，空列表也硬传新关键字会平白打断兼容。
        if selected:
            user_input, library_claimed = Api()._with_library_digest(
                s, user_input, selected=selected)
        else:
            user_input, library_claimed = Api()._with_library_digest(s, user_input)
        resp = {
            "type": "zhi_response",
            "id": req_id,
            "user_input": user_input,
            "selected_options": selected,
            "images": images,
            "files": files,
            "source": "popup_queued",
        }
        if buffer_only:
            # 与 send_reply 脱离分支同口径：已经存着一条就合并，不许覆盖
            with s.lock:
                s.buffered_reply = session_core.merge_buffered_reply(
                    s.buffered_reply,
                    {k: v for k, v in resp.items() if k not in ("type", "id")})
        else:
            try:
                s.send(resp)
            except Exception:
                Api()._release_library_claim(s, library_claimed)
                # 通道断了：条目放回队列（agent 按纪律重试 zhi 时仍能送达），不能白丢
                with s.lock:
                    s.queued = entries + s.queued
                    if s.pending is None:
                        s.pending = pending_taken
                        s.processing_since = None
                return False
        # 送达后把队列气泡改回普通消息。不能改 e["msg"] 那个引用：hub 重启后队列
        # 随快照恢复，引用与 messages 里的气泡已脱钩（改了也白改），用户侧就会
        # 出现「AI 明明收到了、我这还显示排队中」（07-31 22:48 用户实测）。
        # 按 qid 在 messages 里找才作数，与 unqueue_message 同款写法
        qids = {e.get("id") for e in entries if e.get("id")}
        with s.lock:
            for m in s.messages:
                if m.get("qid") in qids:
                    m["queued"] = False
        self.add_message(s, {
            "role": "sys", "ts": now_hms(),
            "html": (f"{len(entries)} 条排队消息已作为回复存着，AI 只发不等、来取时送达"
                     if buffer_only else f"已自动发送 {len(entries)} 条排队消息给 AI"),
        })
        self.log_user(s, user_input or "", selected, len(images), "popup_queued",
                      file_names=[f["name"] for f in files])
        return True

    def wake_window(self):
        """新提问需要用户输入时，若窗口被最小化则自动还原（否则用户以为没弹窗）"""
        if self.cfg.get("quiet_mode"):
            return
        try:
            if self.window and self.window_tracker and self.window_tracker.state == "minimized":
                self.window.restore()
        except Exception:
            pass

    @staticmethod
    def _transcript_slug(transcript_path):
        """对话流水路径里的 .cursor/projects/<slug> 段 = 会话真实所在的工作区。
        这是铁证：报到时 cwd 可能没带（回落成 MCP 进程目录），但流水一定写在
        它自己窗口的工作区槽位下。"""
        parts = re.split(r"[\\/]+", str(transcript_path or ""))
        low = [x.lower() for x in parts]
        try:
            i = low.index("projects")
            return parts[i + 1].lower() if i + 1 < len(parts) else ""
        except ValueError:
            return ""

    def _tick_window_bind(self, now):
        """人手开的对话没有 ext_instance，窗口作用域只能按 cwd 糊成「整个工作区」。

        该工作区此刻只有一个装了窗口总线的 Cursor 在线时，自动钉上 instanceId
        （和 Bajie 的出生窗口同构）。两个以上同目录窗口绝不猜——让用户自己绑。
        """
        if now - float(getattr(self, "_ext_bind_ts", 0) or 0) < 15:
            return
        self._ext_bind_ts = now
        try:
            import ext_bus
            items = ext_bus.live_instances()
        except Exception:
            return
        by_root = {}
        for it in items or []:
            root = norm_root(it.get("workspace") or "")
            if root:
                by_root.setdefault(root, []).append(it)
        for sid in list(self.order):
            s = self.sessions.get(sid)
            if not s or getattr(s, "archived", False):
                continue
            if runtime_adapter.is_native(s):
                continue
            if str(getattr(s, "ext_instance", "") or ""):
                continue
            root = norm_root(getattr(s, "cwd", "") or "")
            cands = by_root.get(root) or []
            if len(cands) == 1:
                s.ext_instance = cands[0]["id"]

    def _window_tag(self, pid, cwd=None, transcript_path=None):
        """把「工作区」映射成稳定短标签（①②③…），帮用户区分会话开在哪个窗口。

        HTTP 守护进程常驻后所有窗口共用同一个 MCP pid，进程号已经分不出窗口；
        用户的实际用法是一个 Cursor 窗口开一个工作区目录。取证优先级：
        流水路径里的工作区 slug（铁证，报到没带 project_path 时 cwd 是错的）
        › 报到带的 cwd（换算成同一套 slug，与铁证天然合流）› pid 兜底。
        （07-31 用户指正：旧的 (pid,项目名) 键在共享进程下编号全乱）"""
        if not pid and not cwd and not transcript_path:
            return ""
        if not hasattr(self, "_pid_tags"):
            self._pid_tags = {}
        slug = self._transcript_slug(transcript_path) \
            or (cursor_project_slug(cwd) if cwd else "")
        key = ("ws", slug) if slug else ("pid", pid)
        tag = self._pid_tags.get(key)
        if tag is None:
            marks = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫"
            i = len(self._pid_tags)
            tag = marks[i] if i < len(marks) else f"#{i + 1}"
            self._pid_tags[key] = tag
        return tag

    def _migrate_window_tags(self, old_pid, new_pid):
        """MCP 进程重启换 pid：pid 兜底键跟着迁移（有 cwd 的键按目录算，天然不受影响）"""
        tags = getattr(self, "_pid_tags", None)
        if not tags or not old_pid or not new_pid or old_pid == new_pid:
            return
        for key, tag in list(tags.items()):
            if isinstance(key, tuple) and key == ("pid", old_pid):
                nk = ("pid", new_pid)
                if nk not in tags:
                    tags[nk] = tag

    def flash_taskbar(self):
        """新提问且窗口不在前台时，闪烁任务栏图标（比提示音更显眼）。
        无头模式下没有自己的窗口，改闪承载 iframe 的 rxyy tools 窗口（若在跑）。"""
        if self.cfg.get("quiet_mode"):
            return
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            hwnd = user32.FindWindowW(None, "rxyy MCP 控制台")
            if not hwnd:
                hwnd = user32.FindWindowW(None, "rxyy tools")
            if not hwnd:
                return
            if user32.GetForegroundWindow() == hwnd:
                return  # 已在前台，无需闪

            class FLASHWINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND),
                            ("dwFlags", wintypes.DWORD), ("uCount", wintypes.UINT),
                            ("dwTimeout", wintypes.DWORD)]
            FLASHW_ALL, FLASHW_TIMERNOFG = 0x3, 0xC
            info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd,
                              FLASHW_ALL | FLASHW_TIMERNOFG, 5, 0)
            user32.FlashWindowEx(ctypes.byref(info))
        except Exception:
            pass

    def _push_label(self, s):
        """锁屏标题：业务项目 · tab，不要用窗口 cwd 冒充项目。

        人坐在 cursor工作流 干快编时，cwd 是控制台仓；再用 Path(cwd).name
        会显示「cursor工作流 · 快编·…」，手机上看着像工作流和视频编辑分叉。
        自报了 agent_project 就用它；tab 名已含项目名则不再前缀。
        """
        named = str(getattr(s, "agent_project", "") or "").strip()
        folder = Path(affiliation_of(s) or getattr(s, "cwd", "") or "").name
        proj = named or folder or "未知项目"
        who = s.name or "未命名"
        assign = str((self.cfg.get("team_assign") or {}).get(
            getattr(s, "conv_key", "") or "", "") or "")
        label = who if proj and proj in who else "{} · {}".format(proj, who)
        if assign:
            label += "（{}）".format(assign[:20])
        return label

    def _takeover_action(self, session_id):
        """「挂了」推送上的一键派单按钮：在通知栏点一下就把活转给待命 agent。

        ntfy 的 http 动作直接打分享服务的接口，手机连页面都不用开——半夜看见
        某个 agent 黑了，锁屏上点一下就接上了。target 交给服务端自己挑。
        """
        try:
            if not self.cfg.get("share_enabled", True):
                return None
            base = share_url(self.cfg).split("/?")[0]
            token = str(self.cfg.get("share_token") or "")
            if not (base and token):
                return None
        except Exception:
            return None
        return {
            "action": "http", "label": "派给待命agent", "method": "POST",
            "url": "{}/api/takeover?t={}".format(base, token),
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"sid": session_id, "target": "auto", "who": "手机通知"},
                               ensure_ascii=False),
            "clear": True,
        }

    def _share_action(self):
        """每条推送都挂一个「全部对话」按钮：手机上不用先找到某条提问推送，
        随便点开哪条通知都能一步进到分享页看全部对话（用户 07-31 提的）。"""
        try:
            if not self.cfg.get("share_enabled", True):
                return None
            u = share_url(self.cfg)
        except Exception:
            return None
        return {"action": "view", "label": "全部对话", "url": u, "clear": False} if u else None

    @staticmethod
    def _push_target_is_bark(target):
        """bark_url 填的是 Bark 还是 ntfy？——Bark 只认 day.app 域名或「裸 KEY」。

        原先按「URL 含 ntfy 字样」判：自建 ntfy 换了域名（如 ntfy 迁到
        api-audioeditor.on-radio.cn:2443）立刻被误当 Bark，按 /标题/正文 拼路径
        全部 404（07-31 实锤）。自建 ntfy 域名随便起，域名判不出协议——改成
        反向规则：day.app（Bark 官方服务）或不带 scheme 的裸 KEY 才是 Bark，
        其余 http(s) 地址一律按 ntfy 发。"""
        if "day.app" in target:
            return True
        return "://" not in target and "ntfy" not in target

    @staticmethod
    def _split_push_targets(raw):
        """推送地址拆多通道：分号/换行分隔，逐个都发（去重、保序）。

        为什么要多通道（08-01 实测）：iPhone 上自建 ntfy 的实时弹窗要靠
        ntfy.sh 上游中转 APNs 唤醒，信令发出去了手机就是不弹（国内网络下
        出名的不稳）；Bark 直连苹果推送条条必弹，但没有动作按钮、消息也不
        存服务器。ntfy 管历史+按钮，Bark 管弹窗，两边一起发各取所长。"""
        out = []
        for part in re.split(r"[;\n；]+", str(raw or "")):
            p = part.strip()
            if p and p not in out:
                out.append(p)
        return out

    @staticmethod
    def _ntfy_json(target, title, body, url=None, tags="bell", priority=None, actions=None):
        """ntfy 走 JSON 发布（POST 根路径 + topic 字段），成功返回 True。

        原先靠 HTTP 头发布，Title 头只能 ASCII——中文标题会变成一串 %XX，所以标题
        一直只能写死英文 rxyy-mcp。JSON body 是 UTF-8：标题、按钮文字都能用中文，
        还能带 actions 按钮。失败时返回 False，由调用方退回旧的头部发布方式。
        """
        import urllib.request
        base = target if target.startswith("http") else "https://ntfy.sh/" + target
        base = base.rstrip("/")
        root, _, topic = base.rpartition("/")
        if not topic or not root:
            return False
        payload = {"topic": topic, "message": body or "", "tags": [t for t in (tags or "").split(",") if t]}
        if title:
            payload["title"] = title
        if url:
            payload["click"] = url
        if priority:
            payload["priority"] = int(priority)
        if actions:
            payload["actions"] = actions
        req = urllib.request.Request(
            root + "/", data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "rxyy-mcp"},
            method="POST")
        with urllib.request.urlopen(req, timeout=8) as r:
            return 200 <= r.status < 300

    def push_share_link(self, auto=False):
        """往手机推一条「常驻」分享页链接：静音最低优先级，只为在通知历史里留个入口。

        auto=True（每次 hub 启动的定时推）每天最多发一条——今晚重启了四五次就白烧
        四五条 ntfy 免费额度（250条/天/IP），设置页手动按钮不受限。"""
        try:
            u = share_url(self.cfg)
        except Exception:
            return {"ok": False, "error": "分享地址取不到"}
        if not (self.cfg.get("push_enabled") and (self.cfg.get("bark_url") or "").strip()):
            return {"ok": False, "error": "手机推送没开或没填地址"}
        if auto:
            today = time.strftime("%Y%m%d")
            if self.cfg.get("share_link_pushed_date") == today:
                return {"ok": True, "note": "今天已推过常驻入口，跳过"}
            self.cfg["share_link_pushed_date"] = today
            self.schedule_save()
        self.push_phone("rxyy MCP · 分享页", "点这里打开全部对话（常驻入口）",
                        url=u, tags="link", priority=1)
        return {"ok": True, "url": u}

    def push_phone(self, title, body, url=None, tags="bell", priority=None,
                   extra_actions=None):
        """AI 发起提问时推到手机。出站 https，手机无需接入公司局域网。
        支持 ntfy(免账号，ntfy.sh 或任意自建地址) 与 Bark(api.day.app 或裸 KEY)。

        正文带「项目 · tab（分工）」用于区分是哪个 agent 在叫你——原先所有推送
        长得一模一样，六七个 tab 同时在跑时根本认不出该点哪条（用户实测反馈）。
        提问内容本身仍然不外发，留在 Tailscale+令牌保护的分享页里（点击打开）。
        tags 改变手机通知的图标：提问用铃铛🔔、会话挂了用警告⚠️，扫一眼就知道轻重。"""
        if _outbound_push_blocked():
            return
        if not self.cfg.get("push_enabled"):
            return
        targets = self._split_push_targets(self.cfg.get("bark_url"))
        if not targets:
            return

        def send_one(target):
            import urllib.parse
            import urllib.request
            if not self._push_target_is_bark(target):
                text = (title + "：" + body) if (title and body) else (body or title or "有新提问待回复")
                # ntfy 最多 3 个动作按钮；派单这类「就地能办的事」排在打开页面前面
                acts = [a for a in list(extra_actions or []) if a]
                share_act = self._share_action()
                if share_act:
                    acts.append(share_act)
                try:
                    if self._ntfy_json(target, title, body or text, url=url, tags=tags,
                                       priority=priority, actions=acts[:3] or None):
                        return
                except Exception:
                    pass  # JSON 发布不通（老版 ntfy / 自建反代）就退回头部方式
                # 兜底：POST 到 https://ntfy.sh/<topic>，正文=body（UTF-8 中文正常显示）。
                # Title header 只能 ASCII——中文会显示成 %XX，故标题固定英文，中文放正文。
                base = target if target.startswith("http") else "https://ntfy.sh/" + target
                headers = {
                    "Title": "rxyy-mcp",
                    "Tags": tags,
                    "User-Agent": "rxyy-mcp",
                }
                if url:
                    headers["Click"] = url
                if priority:
                    headers["Priority"] = str(int(priority))
                req = urllib.request.Request(base, data=text.encode("utf-8"), headers=headers, method="POST")
                urllib.request.urlopen(req, timeout=8).read()
            else:
                base = target.rstrip("/")
                if not base.startswith("http"):
                    base = "https://api.day.app/" + base
                t = urllib.parse.quote((title or "rxyy MCP")[:60])
                b = urllib.parse.quote((body or "有新提问待回复")[:120])
                link = f"{base}/{t}/{b}"
                params = {"group": "rxyy MCP"}
                if url:
                    params["url"] = url
                # Bark 通知级别对齐 ntfy priority：4+=时效性通知（可穿透专注模式），
                # 1=静默入历史（常驻分享入口那条别半夜响）
                if priority and int(priority) >= 4:
                    params["level"] = "timeSensitive"
                elif priority and int(priority) <= 1:
                    params["level"] = "passive"
                link += "?" + urllib.parse.urlencode(params)
                req = urllib.request.Request(link, headers={"User-Agent": "rxyy-mcp"})
                urllib.request.urlopen(req, timeout=8).read()

        def fire():
            errs = []
            for target in targets:
                try:
                    send_one(target)
                    label = target.split("://")[-1].split("/")[0][:40]
                    log_event("手机推送 ok channel={} title={!r}".format(
                        label, (title or "")[:60]))
                except Exception as e:
                    # 07-31 教训：推送失败静默吞掉，ntfy 免费额度打爆后用户只觉得
                    # 「手机怎么没动静了」。落日志 + 记到 push_last_error（设置页可见）。
                    # 双通道下一个挂了另一个照发，错误标注是哪个通道
                    code = getattr(e, "code", None)
                    if code == 429:
                        note = ("ntfy 免费额度用完（250条/天/每出口IP，公司同网都算一个桶），"
                                "按 12 小时滚动窗自动恢复")
                    else:
                        note = "推送服务返回 HTTP {}".format(code) if code \
                            else "推送发不出去：{!r}".format(e)[:140]
                    label = target.split("://")[-1].split("/")[0][:40]
                    errs.append("{}：{}".format(label, note))
                    log_event("手机推送失败 target={} code={} {!r}".format(label, code, e))
            self.push_last_error = "{} {}".format(now_hms(), "；".join(errs)) if errs else ""

        threading.Thread(target=fire, daemon=True).start()

    def _oldest_waiting_convs(self, client, exclude_conv=None, limit=2):
        """该连接上等得最久的几个对话。让路只需要腾出一两个槽位，全叫醒等于让所有
        待命 agent 白跑一轮（每一轮都是真金白银的 LLM 生成）。"""
        rows = []
        with self.lock:
            for s in self.sessions.values():
                if s.client is not client or not s.connected or s.pending is None:
                    continue
                if exclude_conv and s.conv_key == exclude_conv:
                    continue
                rows.append((float(s.pending.get("created") or 0), s.conv_key))
        rows.sort()
        return [c for _, c in rows[:max(1, limit)]]

    def yield_zhi(self, client, exclude_conv=None, secs=90):
        """让路：叫该 MCP 连接上所有等待中的 zhi 立刻保活续期、窗口期内改用短拍。

        Cursor 的共享 MCP 进程对同一 server 的工具调用有并发上限，长等待的 zhi
        占着槽位不放，会把同批后续调用（其他窗口的报到/zt）卡住几分钟。触发点：
        新报到到达/会话复活（同批多半还有兄弟排在门外）、用户点「+」（马上要群发提示词）。

        不做时间冷却（曾因冷却把真正需要的让路挡掉、实测两次翻车）：信号本身只是
        一条极小的 TCP 消息，防抖由 MCP 侧完成——只强制续期已等待超过 10s 的 zhi，
        刚续期过的靠让路窗口内的 15s 短拍自然轮转。

        默认不发：并发上限那个前提已证伪，而每发一次就有 agent 白跑一轮 LLM 生成。"""
        if not self.yield_enabled():
            return
        try:
            convs = self._oldest_waiting_convs(client, exclude_conv)
            client.send({"type": "yield_zhi", "secs": secs,
                         "exclude": exclude_conv or "", "convs": convs})
            log_event("让路信号已发 exclude={} 点名={}".format(
                exclude_conv or "无", "、".join(c[:8] for c in convs) or "全部"))
        except Exception:
            pass

    def yield_zhi_all_local(self):
        """向所有本机 MCP 连接广播让路（「+」点击时用）"""
        with self.lock:
            clients = {s.client for s in self.sessions.values()
                       if s.client is not None and s.connected and not s.peer_ip}
        for c in clients:
            self.yield_zhi(c)

    def notify_status_only(self):
        """agent 状态上报只刷新界面数据，绝不弹窗/闪烁/响铃（干活播报是被动信息）。"""
        pass  # get_state 轮询会自动带出最新 agent_status，无需主动推

    def notify(self):
        if self.cfg.get("quiet_mode"):
            return
        if self.cfg.get("audio_enabled"):
            try:
                import winsound
                # 自定义提示音（gen_notify_wav.py 生成，可换成任意 wav）；缺失时退回系统音
                sound = APP_DIR / "notify.wav"
                if sound.is_file():
                    winsound.PlaySound(str(sound), winsound.SND_FILENAME | winsound.SND_ASYNC)
                else:
                    winsound.PlaySound("SystemAsterisk", winsound.SND_ALIAS | winsound.SND_ASYNC)
            except Exception:
                pass

    # ---------- TCP 服务 ----------
    def bind_listen(self):
        """独占绑定端口决定单例——【必须在主线程、创建 webview 窗口之前调用】。

        根因修复：原来在 serve 线程里才绑定，绑不上才 os._exit，但主线程不等它、
        已经继续创建了窗口/分享服务——多个 hub 几乎同时启动时（重启狂点、控制台
        彻底关闭后多个 MCP 各自拉起）就会短暂多窗口、抢端口，收敛过程混乱，
        表现为『重启两次仍不正常，要管理员清进程才好』。提前到窗口创建前独占绑定，
        绑不上就立即叫醒既有窗口并退出，杜绝重复实例。"""
        def try_bind():
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # Windows 上 SO_REUSEADDR 会允许重复绑定同一端口，导致多开；必须用独占绑定
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                srv.bind((str(self.cfg.get("bind_host", "0.0.0.0")), int(self.cfg["port"])))
                return srv
            except OSError:
                try:
                    srv.close()
                except Exception:
                    pass
                return None

        # 重启接力（--wait-pid）时旧实例可能等超时仍没死透、几秒后才释放端口：
        # 多等最多 25s 反复重试，绝不能一撞就自杀——一自杀就新旧两个实例全没了
        # （正是 14:03 复现的『重启后控制台消失』）。普通启动（双击快捷方式）
        # 只重试 1.5s，保证「叫醒已有窗口」的手感不变。
        deadline = time.time() + (25 if "--wait-pid" in sys.argv else 1.5)
        srv = try_bind()
        while srv is None and time.time() < deadline:
            time.sleep(0.5)
            srv = try_bind()
        if srv is None:
            # 已有控制台在运行：叫醒它的窗口再退出（桌面快捷方式二次点击 = 唤起窗口）。
            # 若对面其实是无头僵尸（窗口已死、进程还占着端口）或正在退出的旧实例，
            # 它收到 wake 会自杀/几秒内释放端口——这里最多补绑 8s 接管过来，
            # 双击一次就能救活（原先只等 2.5s 补一次，端口晚 1 秒释放就前功尽弃，
            # 表现为「双击没反应、要点第二次」）
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(2)
                s.connect(("127.0.0.1", int(self.cfg["port"])))
                send_msg(s, {"type": "wake"})
                s.close()
            except Exception:
                pass
            rebind_deadline = time.time() + 8
            while srv is None and time.time() < rebind_deadline:
                time.sleep(0.5)
                srv = try_bind()
            if srv is None:
                # 最后一道防线：既有实例既不让位、网关也无响应 = 整进程冻结的僵尸
                # （17:04 事故：疯狂双击 15 分钟起不来就是它）。按端口找 pid 强杀接管；
                # 网关活着则说明对面是健康实例，按防多开正常退出。
                gport = int(self.cfg.get("gateway_port", 38777) or 38777)
                if not gateway_alive(gport):
                    pid = port_owner_pid(int(self.cfg["port"]))
                    if pid and pid != os.getpid():
                        log_event("端口 {} 属主 pid={} 网关无响应=僵尸，强杀接管".format(
                            self.cfg.get("port"), pid))
                        taskkill_pid(pid)
                        force_deadline = time.time() + 6
                        while srv is None and time.time() < force_deadline:
                            time.sleep(0.5)
                            srv = try_bind()
            if srv is None:
                log_event("端口 {} 已被占用，已叫醒既有控制台后退出本实例（防多开）".format(
                    self.cfg.get("port")))
                os._exit(0)
            log_event("既有实例已让位/退出，本实例接管端口 {}".format(self.cfg.get("port")))
        srv.listen(16)
        return srv

    def serve(self, srv):
        while True:
            try:
                client, addr = srv.accept()
            except OSError:
                break
            threading.Thread(target=self.handle_client, args=(client, addr), daemon=True).start()

    def handle_client(self, sock, addr=("127.0.0.1", 0)):
        try:
            # TCP keepalive：客户端进程消失但没发 RST 时（半开连接），
            # 让 recv 在 ~15s 内感知断开，避免会话永远显示"回复中"
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            if hasattr(socket, "SIO_KEEPALIVE_VALS"):
                sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 10000, 2000))
        except Exception:
            pass
        hello = recv_msg(sock)
        if not hello or hello.get("type") != "hello":
            if hello and hello.get("type") == "wake":
                # 桌面快捷方式二次启动：唤起 UI。
                # 无头核心：开一次浏览器控制台页即视为唤醒成功（进程本身没有窗口，
                # 绝不能再走「窗口已死=无头僵尸自杀」的老判定）。
                # 窗口模式：pywebview 跨线程调用（restore/show）有卡死前科——放独立
                # 线程最多等 3s，卡死只牺牲那条线程，僵尸由看门狗强杀兜底。
                woke = False
                if HEADLESS:
                    threading.Thread(
                        target=lambda: open_ui_in_browser(self.cfg), daemon=True).start()
                    woke = True
                elif self.window:
                    _done = {"ok": False}

                    def _wake_ui():
                        try:
                            self.window.restore()
                            self.window.show()
                            _done["ok"] = True
                        except Exception:
                            pass

                    t = threading.Thread(target=_wake_ui, daemon=True)
                    t.start()
                    t.join(3)
                    woke = _done["ok"]
                if not woke and time.time() - getattr(self, "_boot_ts", time.time()) > 120:
                    # 窗口模式下端口被本进程占着、窗口却已死/不存在 = 无头僵尸
                    # （destroy 卡死等遗留）。它挡着新实例永远起不来——自杀释放端口，
                    # 对面 bind_listen 的补绑立即接管。
                    log_event("收到 wake 但窗口已死（无头僵尸），自杀释放端口让新实例接管")
                    threading.Thread(
                        target=lambda: (time.sleep(0.2), os._exit(1)), daemon=True).start()
            sock.close()
            return
        # 局域网接入的 MCP 必须持有分享令牌；本机连接免验证
        peer_ip = (addr[0] if addr else "") or "127.0.0.1"
        if peer_ip not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            want = str(self.cfg.get("share_token") or "")
            got = str(hello.get("token") or "")
            if not (bool(self.cfg.get("share_enabled", True)) and want
                    and hmac.compare_digest(got, want)):
                sock.close()
                return
        client = Client(sock, hello.get("cwd", ""), hello.get("pid"))
        if peer_ip not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            client.peer_ip = peer_ip
        log_event("MCP 接入 pid={} peer={} cwd={}".format(
            hello.get("pid"), peer_ip, hello.get("cwd", "")))
        try:
            client.send({"type": "hello_ack"})
        except Exception:
            sock.close()
            return
        while True:
            msg = recv_msg(sock)
            if msg is None:
                break
            try:
                self._handle_client_msg(client, sock, msg)
            except Exception as e:
                # 单条消息处理异常绝不断开整条连接（否则触发无谓重连churn）
                log_event("处理消息异常（已忽略，连接保持）: {}".format(e))
                continue
        self._client_disconnect(client, sock)

    def _handle_client_msg(self, client, sock, msg):
            # 任何入站消息都是「MCP 进程活着」的一手证据，一律刷新连接级心跳。
            # 此前只认 mcp_heartbeat 一种消息——机器高负载时心跳线程哪怕只是被
            # 调度延迟，zhi 流量明明在跑，横幅也会误报「心跳中断」（07-27 15:11 实测）
            client.last_heartbeat = time.time()
            # 接手别名统一换名（一处改、所有消息类型受益）：派接手给待命壳后，
            # 接手方仍带着壳 ID 说话——zhi/zt/ji/detach/cancel/心跳全部路由到
            # 被接手的原会话，它一个字都不用改（见 takeover_aliases）
            if self.takeover_aliases:
                _ck = (msg.get("conversation_id") or "").strip()
                if _ck and _ck in self.takeover_aliases:
                    msg["conversation_id"] = self.takeover_aliases[_ck]
                _convs = msg.get("conversations")
                if isinstance(_convs, list) and any(
                        c in self.takeover_aliases for c in _convs):
                    msg["conversations"] = [
                        self.takeover_aliases.get(c, c) for c in _convs]
            if msg.get("type") in ("zhi_request", "session_notify"):
                # 按 conversation_id 分 tab；服务端未带 ID 时会自动生成唯一 ID
                conv_key = (msg.get("conversation_id") or "").strip() or "__default__"
                # 工作区纠偏：MCP hello 时 roots 常未应答（cwd 回退成家目录），
                # 每个请求都带最新 cwd，此处跟进修正，避免 tab 归属错乱
                req_cwd = sanitize_ws_path((msg.get("cwd") or "").strip())
                if req_cwd and req_cwd != client.cwd:
                    client.cwd = req_cwd
                if (client.closed_convs.get(conv_key)
                        and self._revive_handed_off_conv(client, conv_key) is None):
                    if msg.get("type") == "zhi_request":
                        try:
                            client.send({
                                "type": "zhi_response",
                                "id": msg.get("id"),
                                "user_input": CLOSED_CONV_REPLY,
                                "selected_options": [],
                                "images": [],
                                "source": "popup_closed",
                            })
                        except Exception:
                            pass
                    return
                s = (self.resolve_session(client, conv_key, msg.get("task_name"), metadata=msg)
                     if msg.get("runtime_kind") else
                     self.resolve_session(client, conv_key, msg.get("task_name")))
                runtime_adapter.apply_metadata(s, msg)
                self._note_model(s, msg.get("model"), force=True)
                s.last_heartbeat = time.time()  # 本对话有真实请求 = 对话级也在世
                s.last_zhi_ts = s.last_heartbeat  # 「这个对话本身多久没说话」的唯一准信号
                # 报到成功后自动写项目级 MCP（不 touch 全局）。已有正确 URL 则不写盘。
                # 保活续期不写：那不是新报到，没必要每 120s 读一次 mcp.json。
                if (msg.get("type") == "zhi_request" and not msg.get("resume")
                        and not runtime_adapter.is_native(s)):
                    try:
                        maybe_ensure_project_mcp(s.cwd or client.cwd)
                    except Exception:
                        pass
                if msg.get("type") == "session_notify":
                    s.processing_since = None
                    note = (msg.get("message") or "").strip()
                    if note:
                        self.add_message(s, {
                            "role": "sys",
                            "ts": now_hms(),
                            "html": render_markdown(note, bool(msg.get("is_markdown", True))),
                        })
                        self._append_file(s, f"\n## 📚 系统 · {now_full()}\n\n{note}\n")
                    self.notify()
                    return
                options = msg.get("predefined_options") or []
                resume = bool(msg.get("resume"))
                if (runtime_adapter.is_native(s) and not s.pending and not s.buffered_reply
                        and not (msg.get("message") or "").strip()
                        and not options and not msg.get("card")):
                    s.send({"type": "zhi_response", "id": msg.get("id"),
                            "user_input": "当前没有待领取的控制台回复。", "selected_options": []})
                    return
                if (not resume and s.pending is not None
                        and not (msg.get("message") or "").strip()
                        and not options and not msg.get("card")):
                    # 空正文、没选项没卡、tab 上还挂着提问 = 来收回复的，按续期处理。
                    # resume 标记是 MCP 进程自己记的（_keepalive_active）：接手方是新
                    # 进程、用壳 ID 经别名落到原 tab 的也是新进程，它们按接手单
                    # 「zhi(message 留空) 收回复」来收时 resume 永远是 False——以前
                    # 这里把它当「不同的新提问」：旧 waiter superseded、渲染一个空
                    # 气泡、存着的回复被转队列再当新气泡的答复送回，绕了一圈还多出
                    # 两条气泡。报到那句永远带正文（checkin 信封），空正文不可能是
                    # 合法的新提问，转成续期没有误伤面
                    resume = True
                    log_event("空正文来收回复按续期处理 conv={} tab={}（新进程无 resume 记忆）"
                              .format(conv_key, s.name))
                # 保活续期重呼：沿用同一个提问，不重发气泡、不动绿灯
                if resume and s.pending is not None:
                    if s.buffered_reply is not None:
                        # 脱离期用户已回复 → 立刻把缓存交给这次续期请求
                        r = s.buffered_reply
                        s.buffered_reply = None
                        s.detached = False
                        s.wait_deferred = False
                        s.pending = None
                        s.processing_since = time.time()
                        try:
                            s.send({"type": "zhi_response", "id": msg.get("id"), **r})
                        except Exception:
                            pass
                    else:
                        # 仍无回复：只把等待方 id 换成新请求，绿灯与提问原样保留。
                        # 来收回复的（不带 deferred）= agent 真在等了，失联看门狗照常管；
                        # 探一眼就走的（wait=false 空 message）仍是只发不等
                        s.pending["id"] = msg.get("id")
                        s.detached = False
                        s.detached_since = None
                        s.wait_deferred = bool(msg.get("deferred"))
                    return
                s.processing_since = None
                stale = s.pending
                # 报到/提问重发去重（身份根治⑥，方案书第七节）：同会话未答复期间
                # 又收到「正文 + 选项完全相同」的新 zhi——报到期 reinit 重试的典型
                # 形态（客户端连试 4-6 次，每次都是同一句报到）。不再新开气泡：把旧
                # waiter 以 superseded 收掉，绿灯和那张卡原地保留，只把等待方换成这次
                # 的新请求。实测一张报到卡堆成 6 张（第七节）就是每次重试都 add_message。
                if (stale and stale.get("id")
                        and stale.get("id") != msg.get("id")
                        and (stale.get("message") or "") == (msg.get("message") or "")
                        and list(stale.get("options") or []) == list(options)):
                    try:
                        s.send({
                            "type": "zhi_response",
                            "id": stale["id"],
                            "user_input": "[同一提问的重复投递已合并，请以最新一次的等待为准]",
                            "selected_options": [],
                            "images": [],
                            "source": "superseded",
                        })
                    except Exception:
                        pass
                    stale["id"] = msg.get("id")   # 等待方换成新请求，卡不动
                    s.detached = False
                    s.detached_since = None
                    log_event("报到/提问重发去重 conv={} tab={}：正文相同，合并为同一张卡".format(
                        conv_key, s.name))
                    return
                if stale and stale.get("id") and stale.get("id") != msg.get("id"):
                    # 同会话旧提问未回答又来了「不同的」新提问（并行 zhi / 换了问题）：
                    # 给旧请求补一个应答，否则 MCP 侧那个等待线程永远阻塞
                    try:
                        s.send({
                            "type": "zhi_response",
                            "id": stale["id"],
                            "user_input": "[该提问已被同一会话更新的提问取代，请以最新提问的回复为准]",
                            "selected_options": [],
                            "images": [],
                            "source": "superseded",
                        })
                    except Exception:
                        pass
                # 报到闸：报到那句落到一个已经认领过活的 tab 上 = 接手早就落地了，
                # 只是 agent 手上那份报到提示词还在（Cursor 压缩过上下文之后尤其
                # 常见）。挂成一张等用户点的卡是最坏的处理——用户明明已经点过接手，
                # agent 却卡在「等人点开始任务」上什么都不干（08-25 13:31 实测，
                # 用户的原话是「你这是什么情况？失忆了还是什么？」）。当场答复它，
                # 把原任务名和聊天记录塞回去，它下一秒就能接着干。
                _wake = session_core.checkin_takeover_notice(
                    self, s, msg.get("message") or "", options)
                if _wake:
                    # 先校准身份再答复，一步都不能省：这一瞬是「叫话的 Cursor 会话
                    # 必在生成中」的唯一铁证窗口，答复一发出去 agent 就不生成了。
                    # _reap_takeover_shell 里的 _relocate_takeover_uuid 更是把
                    # cursor_uuid 从被接手的旧对话挪到接手方新对话的那一步——跳过它，
                    # 模型牌/死因牌就一直挂着前任对话的结论（08-25 rxyy 实测：
                    # fable-5 接手 opus-5 的会话，牌迟迟不换）。
                    # 各自兜异常：校准失败也必须把话答出去，否则 agent 一直卡在 zhi 上。
                    for _step in (self._verify_identity_by_generating,
                                  self._reap_takeover_shell):
                        try:
                            _step(s)
                        except Exception:
                            pass
                    try:
                        s.send({
                            "type": "zhi_response",
                            "id": msg.get("id"),
                            "user_input": _wake,
                            "selected_options": [],
                            "images": [],
                            "source": "takeover_wake",
                        })
                    except Exception:
                        pass
                    # 话已经交回给 agent 了，这个 tab 的状态必须跟着走完：
                    # ① 上面那句 stale 的 waiter 刚被 superseded 收掉，卡再留在
                    #    界面上就是张死卡——用户答它，回复会送去一个没人等的 id；
                    # ② 不置回处理态，tab 点会变灰待机，可 agent 正埋头干活，
                    #    面板与真相相反（processing_since 是「等 AI 再提问」的唯一信号）；
                    # ③ 它刚说过话，脱离态自然结束。
                    s.pending = None
                    s.processing_since = time.time()
                    s.detached = False
                    s.detached_since = None
                    self.add_message(s, {
                        "role": "sys", "ts": now_hms(),
                        "html": "接手方又报了一次到（手上还留着报到提示词），"
                                "已当场告诉它「接手已生效，接着干」——无需你再点一次开始任务。",
                    })
                    log_event("报到闸叫醒接手方 conv={} tab={}（报到落在干活 tab 上）"
                              .format(conv_key, s.name))
                    return
                # 重问补送闸（08-26 回复蒸发实证）：用户的回复送进了一个已被
                # Cursor 打断的 zhi——打断不发 cancelled 通知，socket 还活着，
                # 断线救援永远不会醒。agent 拿不到回复，只能把同一道题原样再问
                # 一遍。下面那行「新提问到达 = 正常闭环」会先把探针清了，用户
                # 那条回复就此蒸发，还得亲手重发。其实「原样重问」恰是没送到的
                # 铁证——反着用它：把探针转进队列，本次落卡后 _flush_queue 立刻
                # 自动答上去，用户一个字不用重打。真送到过的（回复之后 agent
                # 还 zt 过）由救援函数里的闭环判据拦下，照旧开新卡等人答。
                probe = getattr(s, "last_reply_probe", None)
                if (probe and (msg.get("message") or "").strip()
                        and (msg.get("message") or "") == (probe.get("question") or "")
                        and list(options) == list(probe.get("q_options") or [])):
                    self._rescue_swallowed_reply(
                        s, why="同一提问原样重发",
                        note="⚠ AI 把同一道题原样重问了一遍：你上一条回复送进了"
                             "一个已被打断的调用，没送到（Cursor 端打断不通知）。"
                             "那条回复已自动补送给这次重问，无需重发。")
                if getattr(s, "buffered_reply", None) is not None:
                    # 脱离期缓存的回复还没被取走就来了新提问（wait=false 连发进展、
                    # 或续期路上换了问题）：以前这里直接置 None 静默丢掉用户的话。
                    # 转进队列，本条落卡后 _flush_queue 立刻把它作为答复送出去
                    self._requeue_buffered_reply(s)
                s.detached = False
                s.detached_since = None
                s.buffered_reply = None
                s.last_reply_probe = None  # 新提问到达 = 上轮回复周期正常闭环
                s.agent_status = ""      # agent 转为提问，干活状态清空（绿点接管）
                s.agent_activity = ""
                # 决策卡片：server 侧已规范化过，这里再过一遍——版本混跑时 server
                # 可能比 hub 旧/新，形状以 hub 认得的为准；认不出就当没卡
                card = None
                if decision_card is not None and msg.get("card"):
                    try:
                        card = decision_card.normalize_card(msg.get("card"))
                    except Exception:  # noqa: BLE001
                        card = None
                s.pending = {
                    "id": msg.get("id"),
                    "message": msg.get("message") or "",
                    "options": options,
                    "card": card,
                    "is_markdown": bool(msg.get("is_markdown", True)),
                    "artifacts": msg.get("artifacts") or [],
                    "created": time.time(),
                }
                # 只发不等（wait=false）：MCP 侧只等一个 peek 窗口（1.5s）就发 zhi_detach
                # 走人，而本 handler 下面的身份校准要读 Cursor 的 sqlite，常常比那个窗口
                # 还长——窗口内到达的用户回复若照常走 socket 直发，server 那头已无等待方，
                # 会被丢（16:06 e2e 实测：回复 1s 后到，靠被吞回复救援才捞回来）。
                # 所以 deferred 提问生来就是脱离态：回复直接进 buffered_reply，agent 来收
                # 时交付；失联看门狗据 wait_deferred 把它当「认领了活」只挂起不清
                s.wait_deferred = bool(msg.get("deferred"))
                if s.wait_deferred:
                    s.detached = True
                    s.detached_since = time.time()
                    s._detach_overdue_logged = False
                if (s.pending["message"].strip() or options or card
                        or s.pending["artifacts"]):
                    body_html = render_markdown(s.pending["message"], s.pending["is_markdown"])
                else:
                    # 空正文、没选项没卡、tab 上也没挂着提问：是新进程（接手方/复活）
                    # 按接手单「zhi(message 留空) 收回复」来收前任那条只发不等的答复，
                    # 而提问本身已随前任断线清掉。pending 照常挂（队列里的回复靠它的
                    # id 送出），只是气泡别渲染成一片空白——正文原样留空，去重/重问
                    # 比对仍按真值
                    body_html = ('<span style="opacity:.6;font-style:italic">'
                                 '（AI 来收上一条提问的回复）</span>')
                self.add_message(s, {
                    "role": "ai",
                    "ts": now_hms(),
                    "html": (body_html
                             + render_artifacts(s.pending["artifacts"])
                             + (decision_card.card_to_html(card) if card else "")),
                    "options": options,
                    "card": card,
                })
                self.log_ai(s, s.pending["message"]
                            + ("\n\n" + decision_card.card_to_text(card) if card else ""),
                            options, s.pending["artifacts"])
                # 记录每条提问到达 hub：下次「agent 说调了 zhi 但控制台没显示」时，
                # 日志能一眼证明消息到底有没有送达 hub（区分是 MCP 侧问题还是 UI 问题）
                log_event("收到提问 conv={} tab={} pid={}".format(
                    conv_key, s.name, client.pid))
                Api()._nudge_rename_if_standby(s)
                # 到达时刻身份自校准：叫话的 Cursor 会话此刻必在生成中
                self._verify_identity_by_generating(s)
                # 校准出 cursor_uuid 后：若同一 Cursor 窗口还挂着个待命空壳（用户在
                # 这个窗口先自动报到、再贴接手提示词切到原对话），把空壳收掉
                self._reap_takeover_shell(s)
                # 若有排队消息，立即自动送出，无需等待用户；否则提醒用户。
                # 提醒动作（唤醒窗口/响铃/闪任务栏/推手机）必须放独立线程：
                # 本函数运行在客户端连接的唯一读取线程上，四个窗口共用一条守护进程
                # 连接——wake_window 的 pywebview 跨线程调用一旦卡住，后续所有消息
                # （其他 agent 的报到、心跳）全部堆在 socket 里，表现为「四个报到只
                # 进来一个 + 心跳中断横幅」（15:51 实测事故）
                if not self._flush_queue(s):
                    # 只发不等且没给选项/卡片 = 纯进展汇报，agent 并没在等人拍板：
                    # 桌面照常提醒，但别推手机（一条任务几次汇报就是几条推送）
                    skip_phone = (self._is_checkin_pending(s)
                                  or (getattr(s, "wait_deferred", False)
                                      and not options and not card))
                    # 把当前绑定的 push_phone 钉死在闭包里：单测 patch.stopall
                    # 之后 daemon 线程才跑的话，不能回落到真 HUB.push_phone
                    # （08-14：sid=t1 夹具连弹真 ntfy）。
                    pusher = self.push_phone
                    def _alert(sid=s.id, who=self._push_label(s),
                               q=str(msg.get("message") or ""),
                               skip_phone=skip_phone, pusher=pusher):
                        self.wake_window()
                        self.notify()
                        self.flash_taskbar()
                        # 报到壳那句「已就位 / 开始任务|结束」不是真提问。
                        if skip_phone:
                            return
                        try:
                            # 自己的推送点开走总令牌页面（顺带选中这个会话）：单会话
                            # 链接进去后「全部对话」和团队面板都会 403
                            purl = owner_session_url(self.cfg, sid) if self.cfg.get("share_enabled", True) else None
                        except Exception:
                            purl = None
                        # 标题=谁在叫（项目·tab·分工），一眼分清该点哪条；正文默认
                        # 只说事不带内容（走公共中继，隐私优先），设置页开「推送带
                        # 提问摘要」后带前 100 字，锁屏上直接判断急不急
                        body = "有新提问待你回复，点开直达可回复页面"
                        if self.cfg.get("push_preview"):
                            body = push_summary(q) or body
                        pusher(who, body, url=purl,
                               tags="speech_balloon", priority=4)
                    threading.Thread(target=_alert, daemon=True).start()
            elif msg.get("type") == "ping":
                try:
                    client.send({"type": "pong"})
                except Exception:
                    pass
            elif msg.get("type") == "mcp_heartbeat":
                # MCP 进程每 5s 上报它服务的所有 conversation_id 仍存活。
                # 记录到会话，get_state 据此实时区分「进程被回收」（心跳停）
                # 与「对话在 IDE 里正常进行」（心跳持续）——进程级 100% 实时信号。
                # 用 _resolve_for_signal 兜底：MCP 重连后首个 zhi 之前也能挂上会话。
                now = time.time()
                client.last_heartbeat = now  # 连接级：conversations 为空也算进程活着
                for ck in msg.get("conversations") or []:
                    s = self._resolve_for_signal(client, ck)
                    if s is not None:
                        s.last_heartbeat = now
            elif msg.get("type") == "freeze_state":
                # MCP 上报有 agent 被省额度冻结阻塞（仅用于控制台横幅展示）
                self._frozen_agents = getattr(self, "_frozen_agents", {})
                if msg.get("frozen"):
                    self._frozen_agents[msg.get("pid")] = time.time()
                else:
                    self._frozen_agents.pop(msg.get("pid"), None)
            elif msg.get("type") == "agent_status":
                # agent 主动上报干活状态（zt / ji 借道）——干活中状态由此变准：
                # 真实状态是 agent 报的，不是 hub 从外部猜的。
                # 关键：MCP 进程重连后，首个 zhi 之前本连接 client.sessions 为空，
                # 必须从 hub 全局注册表复活并挂到本 client，否则状态被静默丢弃。
                conv_key = (msg.get("conversation_id") or "").strip() or "__default__"
                req_cwd = sanitize_ws_path((msg.get("cwd") or "").strip())
                if req_cwd:
                    client.cwd = req_cwd
                s = self._resolve_for_signal(client, conv_key, revive_handed=True)
                if (s is None and conv_key != "__default__"
                        and msg.get("runtime_kind") in ("codex", "chatgpt", "unknown")
                        and not client.closed_convs.get(conv_key)
                        and not any(x.conv_key == conv_key for x in self.sessions.values())):
                    # Native clients report progress before publishing their first
                    # question; that first zt is enough to create their own tab.
                    s = self.resolve_session(client, conv_key, msg.get("task_name"), metadata=msg)
                if s is not None:
                    runtime_adapter.apply_metadata(s, msg)
                    if req_cwd:
                        apply_session_cwd(s, req_cwd)
                    s.agent_status = (msg.get("status") or "").strip()
                    s.agent_activity = (msg.get("activity") or "").strip()
                    s.agent_status_ts = time.time()
                    s.last_heartbeat = time.time()
                    # 轨迹（接手词用）：只记状态有变的，zt 很频繁、同状态刷屏没信息量
                    entry = "{} {}{}".format(
                        time.strftime("%m-%d %H:%M"), s.agent_status or "工作中",
                        " · " + s.agent_activity if s.agent_activity else "")
                    trail = getattr(s, "zt_trail", None)
                    if trail is None:
                        trail = s.zt_trail = []
                    if not trail or trail[-1][12:] != entry[12:]:
                        trail.append(entry)
                        del trail[:-5]
                    # 报过 zt = 认领了活（agent_status_ts 不落盘，这个标记落盘）
                    self._mark_claimed(s, "报过 zt 进度")
                    # agent 在 zt 里报的名字跟 zhi 一样算数。纪律写的是「接到真活后
                    # 第一次 zhi/zt 就换 task_name」，但 zhi 按纪律是收尾才调的——
                    # 中间那几十分钟只有 zt。这条线以前根本不接，agent 老实照办也
                    # 白办，面板上仍是「派活第一句的前 24 字」，控制台还回头催它改名。
                    # 必须排在 _auto_label_on_dispatch / _nudge_rename 之前：那两个
                    # 都看 agent_named 决定要不要动手。
                    self.maybe_apply_task_name(s, msg.get("task_name"))
                    self._note_model(s, msg.get("model"), force=True)
                    # zt 到达同样是「此刻正在生成」的铁证，顺手自校准身份
                    self._verify_identity_by_generating(s)
                    # 接手方报到后常先 zt 上报再 zhi：zt 也触发收壳，别让待命空壳
                    # 一直杵在列表里（08-03 用户实测：4 个接手只 2 个把空壳收掉）
                    self._reap_takeover_shell(s)
                    # 直接在 Cursor 聊天里接活的 agent 走不到「面板派活」那条命名
                    # 路径，tab 就一直叫「待命·xxx」（08-04 截图：4 个 tab 3 个看不
                    # 出在干嘛）。拿它第一句 zt 活动补上；「分析中」这种太短的不算
                    if len(s.agent_activity) >= 6:
                        Api()._auto_label_on_dispatch(s, s.agent_activity)
                    Api()._nudge_rename_if_standby(s)
                    # 任务面板：这条 zt 同时也是「他名下那张卡的进度」。异步投递，
                    # 控制台没开着或答不上来都不影响这里（见 board_hooks 的说明）
                    try:
                        import board_hooks
                        board_hooks.note_progress(
                            s.conv_key,
                            "{} · {}".format(s.agent_status or "工作中", s.agent_activity)
                            if s.agent_activity else (s.agent_status or "工作中"))
                    except Exception as e:  # noqa: BLE001
                        log_event("看板钩子（进度）异常: {}".format(e))
                    act = html_mod.escape(s.agent_activity) if s.agent_activity else ""
                    status_html = "⚙ {}{}".format(
                        html_mod.escape(s.agent_status or "工作中"),
                        " · " + act if act else "")
                    # 连续的状态上报合并成一条气泡原地更新：agent 干活时 zt 很频繁，
                    # 逐条追加会把真正的对话消息挤出内存上限（max_messages）
                    merged = False
                    with s.lock:
                        last = s.messages[-1] if s.messages else None
                        if last is not None and last.get("kind") == "status":
                            last["html"] = status_html
                            last["ts"] = now_hms()
                            s.rev += 1
                            merged = True
                    if not merged:
                        self.add_message(s, {
                            "role": "sys", "ts": now_hms(),
                            "html": status_html, "kind": "status",
                        })
                    self.notify_status_only()
            elif msg.get("type") == "agent_relay":
                # agent → agent 互通（ji 借道 转告/广播）：路由到目标 tab 的队列
                conv_key = (msg.get("conversation_id") or "").strip() or "__default__"
                s = self._resolve_for_signal(client, conv_key, revive_handed=True)
                if s is not None:
                    s.last_heartbeat = time.time()
                    # ji 到达同样是「这个对话此刻正在生成」的铁证，与 zt 同权：
                    # 接手方落地后第一件事常是 ji（黑板/转告），不在这儿校准身份+收壳，
                    # 面板就一直停在交接前的样子（08-04 实测）
                    self._verify_identity_by_generating(s)
                    self._reap_takeover_shell(s)
                    try:
                        Api().relay_from_agent(s, msg.get("to") or "",
                                               msg.get("message") or "")
                    except Exception as e:
                        log_event("agent 转告处理异常: {}".format(e))
            elif msg.get("type") == "agent_summary_fetch":
                # ji(action="摘要")：某个会话干到哪了，按内存实况压一段话即时回给 agent。
                # 只读，不动任何会话状态；找不到 / 多义都在 text 里说人话。
                conv_key = (msg.get("conversation_id") or "").strip() or "__default__"
                s = self._resolve_for_signal(client, conv_key)
                try:
                    text = Api().session_summary_text(
                        msg.get("target") or "", requester=s,
                        max_chars=msg.get("max_chars") or 2000)
                except Exception as e:  # noqa: BLE001
                    log_event("会话摘要生成异常: {!r}".format(e))
                    text = "摘要生成失败：{!r}".format(e)
                try:
                    client.send({"type": "summary_response", "id": msg.get("id"),
                                 "conversation_id": conv_key, "text": text})
                except Exception as e:  # noqa: BLE001
                    log_event("会话摘要应答失败: {}".format(e))
            elif msg.get("type") == "agent_mail_fetch":
                # zt 顺路取信：把队列里那些「排在用户后面」的机器消息（队友转告 /
                # 黑板提醒 / 控制台回执）当场交给这次 zt 的返回值。
                # 在此之前它们只能等用户下次回话时被捎带出去——agent 埋头干活的
                # 半小时里递过去的话，它一个字都看不见（08-07 实测：10:31 发的转告，
                # 对方 10:35 还在改文件，压根没收到）。用户自己的消息一条不动。
                conv_key = (msg.get("conversation_id") or "").strip() or "__default__"
                s = self._resolve_for_signal(client, conv_key)
                items, taken = [], []
                if s is not None:
                    try:
                        items, taken = Api().take_agent_mail(s)
                    except Exception as e:
                        log_event("zt 取信异常: {}".format(e))
                try:
                    # conversation_id 原样回带：一个 MCP 进程服务本机所有 Cursor
                    # 窗口，应答迟到时对面得认得出这信是谁的（见 own_conv_id_hint）
                    client.send({"type": "mail_response", "id": msg.get("id"),
                                 "conversation_id": conv_key, "items": items})
                except Exception as e:
                    # 摘下来了却没送出去 = 这条转告两头都不存在了，放回队列等下一趟
                    if taken:
                        Api().put_back_agent_mail(s, taken)
                    log_event("zt 取信应答失败，已放回 {} 条: {}".format(len(taken), e))
            elif msg.get("type") == "agent_board":
                # agent → 团队黑板（ji 借道 action=黑板）：落盘 + 面板可见，全队主动来读
                conv_key = (msg.get("conversation_id") or "").strip() or "__default__"
                s = self._resolve_for_signal(client, conv_key, revive_handed=True)
                if s is not None:
                    s.last_heartbeat = time.time()
                    self._verify_identity_by_generating(s)
                    self._reap_takeover_shell(s)
                    try:
                        Api().board_from_agent(s, msg.get("kind") or "",
                                               msg.get("text") or "")
                    except Exception as e:
                        log_event("黑板处理异常: {}".format(e))
            elif msg.get("type") == "zhi_detach":
                # 保活脱离：MCP 提前返回躲超时，提问与绿灯保留，
                # 标记 detached 让此期间的用户回复走缓存、下次续期重呼时交付
                conv_key = (msg.get("conversation_id") or "").strip() or "__default__"
                s = client.sessions.get(conv_key)
                if s and s.pending and s.pending.get("id") == msg.get("id"):
                    s.detached = True
                    s.detached_since = time.time()
                    if runtime_adapter.is_native(s):
                        s.wait_deferred = True  # 原生短等待结束，问题与回复保留到后续领取。
                    s._detach_overdue_logged = False  # 新一轮脱离期，允许再记一次超宽限日志
            elif msg.get("type") == "processing_clear":
                conv_key = (msg.get("conversation_id") or "").strip() or "__default__"
                s = client.sessions.get(conv_key)
                if s and s.connected and s.processing_since is not None:
                    s.processing_since = None
                    s.rev += 1
                    self.add_message(s, {
                        "role": "sys", "ts": now_hms(),
                        "html": "AI 本轮可能已结束（超时未再次提问），已切回待机",
                    })
            elif msg.get("type") == "zhi_cancel":
                # Cursor 取消了 zhi（用户在 IDE 里继续对话/按停止）：
                # 清掉「等待你输入」绿点，否则 tab 永远卡在等待态，
                # 且用户之后在控制台的回复会静默丢失
                conv_key = (msg.get("conversation_id") or "").strip() or "__default__"
                s = client.sessions.get(conv_key)
                cancelled = False
                if s is not None:
                    with s.lock:
                        if s.pending and s.pending.get("id") == msg.get("id"):
                            s.pending = None
                            s.processing_since = time.time()
                            cancelled = True
                if cancelled:
                    client_label = {"codex": "Codex", "chatgpt": "ChatGPT"}.get(
                        runtime_adapter.kind(s), "Cursor")
                    self.add_message(s, {
                        "role": "sys", "ts": now_hms(),
                        "html": "该提问已在 {} 端取消，此处无需回复".format(client_label),
                    })
                    self._append_file(s, f"\n> 提问被 IDE 端取消 · {now_full()}\n")

    # 两次「唤醒掉队会话」的全局触碰至少隔这么久（身份根治⑤ 去抖）
    WAKE_TOUCH_DEBOUNCE_SECS = 60

    def _has_inflight_zhi(self, exclude_id=None):
        """当前有没有真·在飞 zhi（连接在、正等用户回复、未脱离）的会话。

        一次 touch_mcp_json 会强拆本机所有窗口的 MCP 会话，把这些正长挂的 zhi
        连线全部撕断（协议层看不见的强拆）——08-13 reinit 风暴事故第七节的
        touch-wake 误伤就是这么来的。返回命中的会话（供日志），没有则 None。
        detached 的不算：MCP 已提前返回、此刻没有挂着的请求可被撕断。"""
        for s in list(self.sessions.values()):
            if s.id == exclude_id or getattr(s, "archived", False):
                continue
            if (s.connected and s.pending is not None
                    and not getattr(s, "detached", False)):
                return s
        return None

    def _wake_touch(self, reason, exclude_id=None):
        """为唤醒掉队的 MCP 客户端而全局触碰 mcp.json——但带两道护栏（身份根治⑤）：

        ① 有别人的在飞 zhi 时绝不触碰：掉队会话本会按 agent 重试纪律（20-30s×6）
           自愈回连，为追它而强拆一片在飞 zhi 是「救一个伤一片」（第七节实测：
           23:42-23:44 为追一个 23:46 自己就回连的会话，2 分钟三次全局触碰，
           把在飞长挂 zhi 撕断、客户端打进 initialize 风暴）；
        ② 去抖：两次唤醒触碰至少隔 WAKE_TOUCH_DEBOUNCE_SECS，限住追单会话时的连发。
        端点自身离线→上线的触碰不走这里（那时全员 zhi 已随端点一起断，无可误伤）。
        返回是否真触碰了。"""
        busy = self._has_inflight_zhi(exclude_id)
        if busy is not None:
            log_event("唤醒触碰让路：有在飞 zhi（tab={}），掉队会话靠自愈重连、"
                      "不强拆（{}）".format(busy.name, reason))
            return False
        now = time.time()
        if now - float(getattr(self, "_wake_touch_last", 0) or 0) < self.WAKE_TOUCH_DEBOUNCE_SECS:
            log_event("唤醒触碰去抖：距上次 <{}s，跳过（{}）".format(
                self.WAKE_TOUCH_DEBOUNCE_SECS, reason))
            return False
        if self.cfg.get("token_freeze"):
            return False
        if touch_mcp_json():
            self._wake_touch_last = now
            log_event("唤醒触碰 mcp.json（{}）".format(reason))
            return True
        return False

    def wake_stragglers_after_restart(self):
        """重启后还有窗口没接回来的话，触碰一次 mcp.json 把它们叫醒（走 _wake_touch
        护栏：有在飞 zhi 就让路，掉队的靠自愈）。

        07-31 实测：整体重启后部分 Cursor 窗口卡在「Connection dropped，25 秒后
        重试」的退避里——有的秒接、有的干等。判据取「上次快照里还活着（近 15 分钟
        有心跳）却仍没接回来」的会话：真有掉队的才动手，全接回来了就不打扰。"""
        now = time.time()
        stale = [s for s in list(self.sessions.values())
                 if not s.connected
                 and not getattr(s, "archived", False)  # 用户关掉的不叫醒
                 and not getattr(s, "auto_reconnect_blocked", False)
                 and 0 < (now - (getattr(s, "last_heartbeat", 0) or 0)) < 900]
        if not stale:
            return
        self._wake_touch("重启后 {} 个会话没接回来".format(len(stale)))

    def _emergency_reload(self, s: Session):
        """紧急自愈：正在等用户回复（绿灯）的会话通道死亡 = agent 的 zhi 刚被掐断，
        它会在 ~30s 内按重试纪律重呼。若 Cursor 的 FSM 卡在 Error 不自动重拉，
        重试会继续失败、agent 可能放弃。这里在 12s 后仍未复活时触碰 mcp.json，
        保证 agent 第一次重试就能接上——把最坏恢复时间从看门狗的 2min 压到 <30s。

        但触碰是全局强拆（身份根治⑤）：别的窗口若正有在飞 zhi，追这一个反而伤一片
        （第七节事故）。改走 _wake_touch 护栏——有在飞 zhi 就让路，本会话按重试纪律
        自愈；排除自己（它此刻已断连，本不在在飞集里，双保险）。"""
        time.sleep(12)
        if s.connected:
            return  # 已自己重连回来（正常回收路径），不动手
        now = time.time()
        if now - getattr(self, "_emg_reload_last", 0.0) < 90:
            return
        if self._wake_touch("等回复中的会话 {} 通道死亡 12s 未复活".format(s.conv_key),
                            exclude_id=s.id):
            self._emg_reload_last = now

    def _rescue_swallowed_reply(self, s: Session, why="", note=""):
        """被吞回复救援。用户在 IDE 里打断进行中的工具调用时，Cursor 不发任何
        cancelled 通知——控制台此后送出的回复会进入已死的请求、静默丢失。
        判据：回复送出后 agent 再没来新提问（探针未被清除）就赶上了通道死亡
        → 大概率没送到。转入队列，session 复活后下次 zhi 自动补送（带可重复标记，
        万一其实已送到，agent 看到标记自会忽略）。

        why：死因（判死刑那条路径传进来）。通道没断、agent 却被 Cursor 在 API 层
        杀掉时（欠费/额度/被拒），此前只有通道断开会触发救援，这类死法一声不吭——
        08-03 同事机实测：回复正常发出、tab 转黄「处理中」，人和 AI 各等一小时。

        note：给用户看的系统气泡整句替换（重问补送闸传进来）。缺省两句话都是
        「AI 断了/挂了、等下次提问」的口径，可重问路上的 AI 活得好好的、补送
        也是当场送达——沿用缺省文案等于对用户撒谎。"""
        probe = getattr(s, "last_reply_probe", None)
        if not probe:
            return
        s.last_reply_probe = None
        # zt 闭环判据（死会话处置③，08-13 实证 09:47/14:08 两起重复补送的源头）：
        # zhi 是阻塞调用，agent 挂在里面等回复时不可能同时干活——回复送出**之后**
        # 它还 zt 上报过，就说明它已经从那个 zhi 里带着回复出来了（没送到的话，
        # 按重试纪律它会重挂 zhi 而不是继续干活）。这种情况不补送：补出去的必是
        # 重复，还会在它下一次提问时顶掉真回复的位置。
        probe_ts = float(probe.get("ts") or 0)
        zt_ts = float(getattr(s, "agent_status_ts", 0) or 0)
        if probe_ts and zt_ts > probe_ts + 3:
            log_event("回复送出后 agent 仍有 zt 上报（{:.0f}s 后），视为已送达不补送 "
                      "conv={}".format(zt_ts - probe_ts, s.conv_key))
            return
        text = (probe.get("text") or "").strip()
        entry_text = ("[补送|此回复此前可能因 IDE 端中断未送达，若已收到请忽略重复] " + text) if text \
            else "[补送|用户此前有一条回复可能未送达，请查看随附图片/文件]"
        qid = uuid.uuid4().hex[:8]
        with s.lock:
            s.queued.append({
                "id": qid, "text": entry_text,
                "images": probe.get("images") or [],
                "files": probe.get("files") or [],
                "msg": probe.get("msg_ref"), "who": probe.get("who"),
                # 过期判据用（死会话处置③）：入队晚于用户最近真实回复才有资格占位
                "redelivery": True, "ts": time.time(),
                # 这条答的是哪道题（正文+选项，从送达探针抄来）：新提问带决策卡时
                # _flush_queue 只放行「同一道题的补送」，别的补送件扣住不答
                # （_answers_this_question）——没这两项它就永远过不了那道闸
                "question": probe.get("question"),
                "q_options": list(probe.get("q_options") or []),
            })
        # 原气泡打上「未送达」，让人一眼看出这条根本没人接（此前它和送到了的长得一样）
        ref = probe.get("msg_ref")
        if isinstance(ref, dict):
            ref["undelivered"] = True
            s.rev += 1
        if not note:
            note = ("⚠ 你上一条回复没人接收：AI 已经挂了（{}）。".format(why) if why
                    else "⚠ 你上一条回复送出后 AI 无任何后续动静就断了链接"
                         "（多半是你在 IDE 里打断了它，Cursor 不通知我们）。"
                    ) + "该回复已转入队列，AI 下次提问（复活/接手/换号重开）时自动补送。"
        self.add_message(s, {
            "role": "sys", "ts": now_hms(),
            "html": note,
        })
        log_event("被吞回复救援 conv={} 原因={} 已转入队列待补送".format(
            s.conv_key, why or "通道断开"))

    def _requeue_buffered_reply(self, s: Session, note=None, prefix=None, why=None):
        """脱离期缓存的用户回复（buffered_reply）还没被 agent 取走，agent 却发来了
        新提问——wait=false 连发几条进展、或续期路上换了问题都会这样。以前新提问
        落地时这条缓存直接置 None，用户在控制台明明回了话、agent 却永远没收到。

        转进 s.queued（与被吞回复救援同一条队列），新提问落卡后 _flush_queue 立刻
        把它作为答复送出去：agent 拿到的是用户对上一条的回话，正好接着往下干；
        用户侧留一句系统气泡说明去向。选项折进正文（队列条目没有 selected 位）。

        note / prefix / why：断线路径（_client_disconnect）传进来换口径——那时不是
        「它又发了新消息」而是「它没来取就断了」，沿用缺省文案等于对用户撒谎。"""
        r = s.buffered_reply
        s.buffered_reply = None
        if not isinstance(r, dict):
            return False
        text = (r.get("user_input") or "").strip()
        sel = [str(x) for x in (r.get("selected_options") or []) if x]
        if sel:
            text = "选择的选项: " + ", ".join(sel) + ("\n" + text if text else "")
        images = r.get("images") or []
        files = r.get("files") or []
        if not (text or images or files):
            return False
        qid = uuid.uuid4().hex[:8]
        with s.lock:
            s.queued.append({
                "id": qid,
                "text": (prefix or "[上一条提问的回复|你只发不等、还没来取，用户已回话] ") + text,
                "images": images, "files": files,
                "msg": None, "who": None, "ts": time.time(),
            })
        self.add_message(s, {
            "role": "sys", "ts": now_hms(),
            "html": note or "你上一条回复 AI 还没来取，它又发了新消息：那条回复已作为新消息的答复自动补送。",
        })
        log_event("脱离期缓存回复转队列 conv={} tab={}（{}）".format(
            s.conv_key, s.name, why or "agent 未取先发新提问"))
        return True

    def _client_disconnect(self, client, sock):
        # 连接断开：先进入静默重连宽限（Cursor 重启/回收 MCP 后往往几十秒内就回来，
        # 期间 tab 显示「重连中」；宽限内重连成功则用户全程无感，超时才正式报断开）
        try:
            sock.close()
        except Exception:
            pass
        grace = int(self.cfg.get("reconnect_grace_secs", 120) or 0)
        for s in list(client.sessions.values()):
            if not s.connected:
                continue
            s.connected = False
            s.disconnected_at = time.time()
            if getattr(s, "buffered_reply", None) is not None:
                # 只发不等期间用户回过、agent 没来取就断了：以前 pending 在下面清掉、
                # buffered_reply 原地不动，下次 create_session 复活分支直接置 None——
                # 用户那几条回复（09-02 17:12/17:36/17:38 三条、两条带图）就此蒸发，
                # 接手方只能靠接手单里 400 字的摘要猜。转进队列：复活/接手后第一次
                # zhi 落卡时 _flush_queue 送出去。送达探针一并清掉——它记的是其中
                # 最后一条，让下面的救援再入一次队就是重复补送
                if self._requeue_buffered_reply(
                        s,
                        note="你回过的话 AI 还没来取就断了链接：那条回复已转入队列，"
                             "AI 下次提问（复活/接手/换号重开）时自动补送。",
                        prefix="[上一条提问的回复|AI 只发不等、没来取就断了，用户已回话] ",
                        why="agent 未取就断线"):
                    s.last_reply_probe = None
            self._rescue_swallowed_reply(s)
            if s.pending is not None:
                # 绿灯会话被掐 = agent 马上要重试；后台盯 12s，没复活就紧急重载
                threading.Thread(target=self._emergency_reload, args=(s,), daemon=True).start()
            had_pending = s.pending is not None
            s.pending = None
            s.processing_since = None
            s.detached = False
            s.detached_since = None
            s.wait_deferred = False
            with self.lock:
                still_visible = s.id in self.sessions
            if not still_visible:
                continue
            if grace > 0:
                s.lost_pending_on_drop = had_pending
                s.recon_deadline = time.time() + grace
                s.rev += 1
                log_event("连接掉线进入重连宽限 conv={} tab={}".format(s.conv_key, s.name))
            else:
                self._finalize_disconnect(s, had_pending)

    def _finalize_disconnect(self, s: Session, had_pending):
        """宽限超时（或未启用宽限）后正式宣告断开：写记录、提示用户。"""
        s.recon_deadline = None
        s.lost_pending_on_drop = False
        s.pending_lost = had_pending
        reason = "连接断开（IDE 会话结束或 MCP 服务停止）"
        s.end_reason = reason
        log_event("会话断开定案 conv={} tab={} 有未答提问={}".format(
            s.conv_key, s.name, had_pending))
        self.add_message(s, {
            "role": "sys", "ts": now_hms(),
            "html": "会话已断开{}。记录已保存: {}".format(
                "（有未回复的提问）" if had_pending else "", s.file_path),
        })
        self.log_end(s, reason)
        self.notify()

    def hub_closing(self):
        log_event("=== hub 关闭 pid={} ===".format(os.getpid()))
        try:
            # 用户正常关闭的标记：看门狗看到它就不越权复活控制台
            # （重启接力的新实例、下次手动启动会把它清掉）
            CLEAN_EXIT_MARK.write_text(now_full(), encoding="utf-8")
        except OSError:
            pass
        if self.window_tracker:
            self.window_tracker.stop()
        save_config(self.cfg)  # 持久化最终窗口大小等设置
        self.save_state()  # 关闭前存一次会话，重开可恢复
        with self.lock:
            sessions = list(self.sessions.values())
        for s in sessions:
            if s.connected:
                self.log_end(s, "控制台被关闭")
                try:
                    s.client.sock.close()
                except Exception:
                    pass


HUB = Hub()


# ---------- Api 类已抽到 hub_api.py（2026-08-12 第一刀，docs/hub拆分手术方案-2026-08-12.md） ----------
# 直跑 hub.py（__main__）时先把本模块登记成 "hub"，hub_api 的 `import hub` 才拿到
# 同一个模块实例，而不是把 hub.py 再执行一遍（那会造出第二个 HUB 单例）。
if "hub" not in sys.modules:
    sys.modules["hub"] = sys.modules[__name__]
from hub_api import Api  # noqa: E402  兼容别名：hub.Api / patch("hub.Api") 照旧成立


# ---------- 工作流编排引擎（钩子把 hub 的派话/拉起/推送能力借给引擎） ----------
def _workflow_hooks():
    api = Api()

    def team_agents(root):
        out = []
        for s in api._team_sessions(root):
            out.append({
                "conv": s.conv_key or "",
                "label": api.session_label(s) or s.name,
                "role": api._team_role(s),
                "waiting": s.pending is not None,
                "shell": str(s.name or "").startswith("待命"),
                "seq": int(getattr(s, "msg_seq", 0) or 0),
            })
        return out

    def find_conv(prefix):
        p = str(prefix or "").strip().lower()
        if len(p) < 6:
            return None  # 前缀太短容易指错人，宁可当没找到
        with HUB.lock:
            cands = [x for x in HUB.sessions.values() if x.connected]
        for x in cands:
            ck = (x.conv_key or "").lower()
            if ck == p or ck.startswith(p):
                return {"conv": x.conv_key, "label": api.session_label(x) or x.name}
        return None

    def send_to_conv(conv, text):
        with HUB.lock:
            s = next((x for x in HUB.sessions.values()
                      if (x.conv_key or "") == conv), None)
        if s is None:
            return {"ok": False, "error": "会话不存在"}
        if not s.connected and not (
                getattr(s, "recon_deadline", 0)
                and time.time() <= s.recon_deadline):
            return {"ok": False, "error": "执行人已离线"}
        if s.pending is not None:
            r = api.send_reply(s.id, text, [], [], False, who="工作流")
        else:
            r = api.queue_message(s.id, text, [], who="工作流")
        out = dict(r or {})
        out.setdefault("ok", False)
        out["label"] = api.session_label(s) or s.name
        return out

    def spawn_builtin(root, task_name, first_task):
        return api.sdk_spawn(root, task_name=task_name, first_task=first_task)

    def builtin_status(key):
        # 内置执行人的 runner 健康度：进程没了/报错退出 = 链不可能再有收工信号
        import sdk_runner as reg
        e = next((x for x in reg.load_registry() if x.get("key") == key), None)
        if not e:
            return {"status": "missing", "note": "runner 注册表里没这条记录"}
        st = str(e.get("status") or "")
        if st in ("launching", "starting", "running") and not reg.pid_alive(e.get("pid")):
            st = "dead"  # 状态没来得及更新就死了（强杀/断电）
        return {"status": st, "note": e.get("note") or ""}

    def notify_user(title, body):
        log_event("{}：{}".format(title, str(body)[:160]))
        try:
            HUB.push_phone(title, str(body)[:400], tags="chains")
        except Exception:
            pass

    return {"team_agents": team_agents, "find_conv": find_conv,
            "send_to_conv": send_to_conv, "spawn_builtin": spawn_builtin,
            "builtin_status": builtin_status,
            "notify_user": notify_user, "log": log_event}


try:
    WORKFLOW = workflow_mod.WorkflowEngine(DATA_DIR / "workflows.json",
                                           hooks=_workflow_hooks())
except Exception as _wf_e:  # noqa: BLE001
    WORKFLOW = None
    log_event("工作流引擎启动失败: {}".format(_wf_e))

# 双活事件总线（P0）：main() 里按 config sync_root 启动；未启用一直是 None
SYNCBUS = None
# 双活 P1：配置线（workflow.db 白名单同步）与运输线（SSH 摆渡）；随 SYNCBUS 启停
WFSYNC = None
FERRY = None
# 双活 P2：业务线（看板/任务安排站/会话控制台列表，见 sync_console.py 门头）
CONSYNC = None


def _bootstrap_local_hooks():
    """Editor modifications require an explicit action in the community edition."""
    return None


def main():
    # 重启接力：新实例等旧实例退出释放端口后再绑定（否则 serve() 撞端口自杀）
    if "--wait-pid" in sys.argv:
        try:
            _old_pid = int(sys.argv[sys.argv.index("--wait-pid") + 1])
            # 先落日志再等：排队等待必须可见（17:04 事故里排队的实例全程无日志，
            # 用户只看到「疯狂点启动毫无反应」）
            log_event("hub 接力实例 pid={} 等待旧实例 pid={} 退出…".format(
                os.getpid(), _old_pid))
            wait_pid_exit(_old_pid)
        except (ValueError, IndexError):
            pass
    HUB._boot_ts = time.time()  # 僵尸判定基准：启动 120s 内收到 wake 不视为无头僵尸
    log_event("=== hub 启动 pid={} python={} 端口={} 依赖加载耗时={:.1f}s ===".format(
        os.getpid(), sys.version.split()[0], HUB.cfg.get("port"),
        time.time() - _BOOT_T0))
    if tsd_decrypt is not None:
        log_event("TSD 解密能力: {}".format(
            "可用" if tsd_decrypt.available() else "不可用（非公司机器/DLL 缺失，已降级）"))
    if HUB.config_error:
        log_event("配置加载警告: {}".format(HUB.config_error))
    # 任务库实际落点打一行（08-27 转告事故：hub 与控制台各写一份任务库，分家时
    # ji 完成任务/发给 全线找不到人，事后无凭据。启动就把真实路径抖出来，分家秒诊）。
    try:
        import share_server
        log_event("任务库实际路径: {}".format(share_server._taskstage_dir()))
    except Exception as e:
        log_event("任务库路径探测失败: {!r}".format(e))
    # 顺序不能反：cleanup 的「活跃会话豁免」看的是 self.sessions，先清理就等于豁免为空，
    # 待恢复会话的记录会被当日 20 份上限当作孤儿删掉（07-29 实测：一次重启吃掉最旧 6 份，
    # 含 39KB 的 工作流.md）——正是 cleanup_history_files 文档里要防的那类事故
    HUB.load_state()  # 恢复上次未关闭的会话 tab（断开态，MCP 重连即复活）
    HUB.cleanup_history_files()
    # 主线程独占绑定端口 = 单例守卫：绑不上（已有实例）立即叫醒既有窗口并退出，
    # 绝不再往下创建第二个窗口/分享服务，从源头杜绝多开抢端口的混乱
    _srv = HUB.bind_listen()
    _release_spawn_gate()  # 端口到手 = 启动成功，放开「别再拉新 hub」的闸
    # 抢到端口 = 本实例是正主：清掉「正常关闭」标记（看门狗据此判断要不要复活），
    # 并拉起独立看门狗（僵尸强杀/崩溃复活/MCP守护进程复活都靠它，进程级兜底）
    try:
        CLEAN_EXIT_MARK.unlink(missing_ok=True)
    except OSError:
        pass
    # 同时认领整机「正主安装」：看门狗/MCP 守护/console 拉起前会查它，
    # 另一套安装（源码版 vs 打包版）从此不再复活自己那一套来抢 39222
    instance_owner.claim(APP_DIR)
    threading.Thread(target=_owner_heartbeat_loop, daemon=True).start()
    ensure_watchdog()
    # 孤儿 WebView2 清扫与 GUI 初始化并行：hub 自启动路径此前无人清（15:06 事故）
    cleanup_orphan_webview2_async()
    # 停车/发车与账单 hook 各自进入安全态，互不连坐。
    _bootstrap_local_hooks()
    # 自启动已开启的机器：按当前代码路径刷新 Run 条目（含看门狗条目）——
    # 目录迁移/新增条目后自动跟上，无需用户重新开关一次。
    # 常驻区（pythonw）也必须能改写：以前只允许 frozen，结果 live 成了正主之后
    # Run 一直停在 `rxyy-tools-community.exe --run _internal\\hub.py`，登录弹黑框且交不了手。
    # 源码仓 refresh_run_keys 直接 skipped，避免再把自启指回开发目录（07-29）。
    try:
        live_runtime.refresh_run_keys_if_enabled(APP_DIR)
    except Exception:
        pass
    threading.Thread(target=HUB.serve, args=(_srv,), daemon=True).start()
    threading.Thread(target=HUB.daily_cleanup_loop, daemon=True).start()
    threading.Thread(target=HUB.config_watch_loop, daemon=True).start()
    threading.Thread(target=HUB._state_save_loop, daemon=True).start()
    threading.Thread(target=HUB.title_sync_loop, daemon=True).start()
    threading.Thread(target=HUB.state_tick_loop, daemon=True).start()
    threading.Thread(target=HUB.auto_reload_loop, daemon=True).start()
    threading.Thread(target=HUB.mcp_http_daemon_loop, daemon=True).start()
    # 双活事件总线（P0 骨架）：sync_root 没配就返回 None，现网零参与。
    # 塌了只举手不连坐——同步是锦上添花，绝不能拖垮控制台本体
    global SYNCBUS, WFSYNC, FERRY, CONSYNC
    try:
        import syncbus as syncbus_mod
        SYNCBUS = syncbus_mod.start_from_config(HUB.cfg, DATA_DIR, alert=log_event)
        if SYNCBUS is not None:
            log_event("双活事件总线已启动 machine={} root={}".format(
                SYNCBUS.machine, SYNCBUS.sync_root))
    except Exception as e:  # noqa: BLE001
        log_event("双活事件总线启动失败（不影响其它功能）: {}".format(e))
    if SYNCBUS is not None:
        # P1 两条线各自 try：配置线塌了不连坐运输线，反之亦然
        try:
            import sync_wf
            WFSYNC = sync_wf.attach(SYNCBUS, HUB.cfg, alert=log_event)
            log_event("双活配置线已启动（workflow.db 白名单，{}s 差量轮询）".format(
                int(WFSYNC.poll_secs)))
        except Exception as e:  # noqa: BLE001
            log_event("双活配置线启动失败（不影响其它功能）: {}".format(e))
        try:
            import sync_ferry
            FERRY = sync_ferry.start_from_config(HUB.cfg, alert=log_event,
                                                 machine=SYNCBUS.machine)
            if FERRY is not None:
                log_event("双活 SSH 摆渡已启动：每 {}s 拉 {}".format(
                    int(FERRY.secs), FERRY.remote))
        except Exception as e:  # noqa: BLE001
            log_event("双活摆渡启动失败（不影响其它功能）: {}".format(e))
        try:
            import sync_console
            CONSYNC = sync_console.attach(SYNCBUS, HUB.cfg, DATA_DIR,
                                          HUB.STATE_PATH, alert=log_event)
            log_event("双活业务线已启动（看板/任务站/会话列表，{}s 差量轮询）"
                      .format(int(CONSYNC.poll_secs)))
        except Exception as e:  # noqa: BLE001
            log_event("双活业务线启动失败（不影响其它功能）: {}".format(e))
    api = Api()
    HUB.share_server = start_share_server(HUB, api)
    # 分享页链接常驻手机：每次起来静音推一条（priority=1，不响不弹横幅），
    # ntfy 的通知历史里就总有一个入口，不必等到有提问才点得进去
    if HUB.share_server is not None:
        threading.Timer(6, lambda: HUB.push_share_link(auto=True)).start()

    # 两趟：8 秒那趟赶在 Cursor 自己的 25 秒退避之前，绝大多数窗口能秒回；
    # 30 秒那趟兜住启动慢/第一趟没赶上的（实测只等第二趟的话，用户要干等近一分钟）。
    # 触碰走 wake_stragglers_after_restart 的护栏：有在飞 zhi 就让路（身份根治⑤）
    for _delay in (8, 30):
        threading.Timer(_delay, HUB.wake_stragglers_after_restart).start()
    # http 网关：无头模式下它就是唯一 UI 入口（浏览器 / rxyy tools iframe），
    # 窗口模式下作为旁挂增量。失败不崩核心，但无头时要在日志里喊出来。
    gateway_ok = False
    try:
        from gateway import start_gateway
        start_gateway(api, UI_PATH, port=int(HUB.cfg.get("gateway_port", 38777) or 38777),
                      logger=log_event)
        gateway_ok = True
    except Exception as e:
        log_event("网关启动失败（无头模式下 UI 将不可用）: {}".format(e))

    if HEADLESS:
        # ---- 无头核心（默认，P1 落地）：不建 pywebview 窗口，WebView2 零参与 ----
        log_event("以无头核心模式运行 pid={}（UI: http://127.0.0.1:{}/ui，rxyy tools iframe 同源）".format(
            os.getpid(), HUB.cfg.get("gateway_port", 38777)))
        if OPEN_UI_ON_BOOT and gateway_ok:
            # 双击启动的用户想「看到」控制台：网关就绪后开一次浏览器页
            threading.Thread(
                target=lambda: (time.sleep(1.0), open_ui_in_browser(HUB.cfg)),
                daemon=True).start()
        if should_open_console_on_autostart() and gateway_ok:
            # 登录自启：无头 hub 已起，补开 rxyy tools（不是浏览器页）。
            # 窗口已在（例如中午只 restart_hub）FindWindow 命中则跳过。
            threading.Thread(
                target=lambda: (time.sleep(1.0), open_console_window_if_absent()),
                daemon=True).start()
        threading.Event().wait()  # 常驻；退出通道=shutdown_core/重启接力/进程被杀
        return

    # ---- 窗口模式（--windowed，过渡期后备）：原 pywebview 路径原样保留 ----
    try:
        screens = list(webview.screens)
    except Exception:
        screens = []
    placement = build_window_options(HUB.cfg, screens)
    window = webview.create_window(
        "rxyy MCP 控制台",
        url=str(UI_PATH),
        js_api=api,
        width=placement["width"],
        height=placement["height"],
        x=placement["x"],
        y=placement["y"],
        min_size=(520, 640),
        minimized=placement["minimized"],
        maximized=placement["maximized"],
        on_top=bool(HUB.cfg.get("always_on_top")),
        confirm_close=True,
    )
    HUB.window = window
    tracker = WindowStateTracker(HUB.cfg, window, HUB.schedule_save)
    HUB.window_tracker = tracker

    try:
        window.events.moved += tracker.geometry_changed
        window.events.resized += tracker.geometry_changed
        window.events.minimized += tracker.mark_minimized
        window.events.maximized += tracker.mark_maximized
        window.events.restored += tracker.mark_restored
        window.events.closing += tracker.flush
    except Exception:
        pass

    webview.start()
    HUB.hub_closing()


if __name__ == "__main__":
    # 常驻区里已经有一份rxyy MCP时，本进程只负责把活交出去、随即退场。
    # 换装是把整个 dist\rxyy-tools-community 改名，跑在包里的 hub 必然陪葬；跑在常驻区的
    # 那份换装碰不到，全队的 zhi 一次都不断。让不成（没常驻区/那份跑不起来）
    # 就原样在这儿跑 —— 同事拿到的拷贝没有常驻区，行为跟今天一模一样。
    if live_runtime.hand_over("hub.py", sys.argv[1:]):
        sys.exit(0)
    try:
        os.chdir(APP_DIR)
    except OSError:
        pass
    try:
        main()
    except Exception:
        # pythonw 无控制台，崩溃原本无迹可寻；落一份日志便于排查
        import traceback
        try:
            with open(APP_DIR / "hub-crash.log", "a", encoding="utf-8") as f:
                f.write("\n=== {} ===\n{}".format(now_full(), traceback.format_exc()))
        except Exception:
            pass
        raise
