# -*- coding: utf-8 -*-
"""rxyy MCP MCP 服务器（stdio）

工具与寸止对齐：zhi（交互）+ ji（记忆）。
zhi 不再每次弹一个新窗口，而是把请求发给常驻控制台（hub.py），
控制台不在运行时自动拉起，回复后窗口保持存在。
"""
import json
import copy
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

from datadir import DATA_DIR  # noqa: E402
from ipc import ExclusiveThreadingHTTPServer, InProcSock, send_msg, recv_msg  # noqa: E402
from mcp_touch import touch_mcp_json  # noqa: E402
from memory import MemoryManager  # noqa: E402
from client_runtime import (  # noqa: E402
    HttpRuntimeRegistry,
    NATIVE_RUNTIME_KINDS,
    RUNTIME_KINDS,
    RuntimeContext,
    infer_runtime,
    instructions_for,
    is_native,
    normalize_runtime,
    profile_from_path,
    SESSION_HEADER,
    header_value,
)

try:
    import ask_quality  # noqa: E402
except ImportError:  # 半套同步时宁可不拦，也不能让 zhi 起不来
    class ask_quality:  # type: ignore[no-redef]
        @staticmethod
        def bounce_text(*_a, **_kw):
            return None

try:
    import decision_card  # noqa: E402
except ImportError:  # 半套同步：卡片退化成不传，老路 predefined_options 照旧
    decision_card = None

try:
    import tsd_decrypt  # noqa: E402
except Exception:
    tsd_decrypt = None

try:
    import instance_owner  # noqa: E402
except ImportError:  # 半套同步（只拷了部分文件）不该让 MCP 守护整个起不来
    class instance_owner:  # type: ignore[no-redef]
        @staticmethod
        def should_stand_down(_app_dir):
            return False, ""

try:
    import live_runtime  # noqa: E402
except ImportError:  # 同上：老包里没有这个模块，照旧在原地跑
    class live_runtime:  # type: ignore[no-redef]
        @staticmethod
        def hand_over(*_a, **_kw):
            return False

SERVER_NAME = "rxyy MCP"
SERVER_VERSION = "1.0.0"

# hub 拆分第二刀（2026-08-12）：39222 端点以线程挂进 hub 进程时，hub 的
# mcp_http_daemon_loop 会把 Hub 单例注入到这里。非 None 时 HubBridge 对本机
# 目标不再走 127.0.0.1:38999 的 TCP，改用进程内管道（ipc.InProcSock）直达
# Hub.handle_client——协议、握手、per-connection 串行语义一字不动。
# stdio 模式（同事接入/本机遗留）里它恒为 None，行为与今天完全一致。
_INPROC_HUB = None

# HTTP 端点可能同时承载多个 Codex/ChatGPT/旧 Cursor 连接；运行时身份只能
# 放在各自的 session/connection 上，不能再由最后一次 initialize 覆盖全局。
HTTP_RUNTIME_REGISTRY = HttpRuntimeRegistry()

# 逃生门：自动化/批处理场景不需要人守着控制台时，
def _rxyy_env(suffix, default=""):
    """新环境变量优先，旧 CHIJIU_* 仅作升级兼容。"""
    return (os.environ.get("RXYY_MCP_" + suffix)
            or os.environ.get("CHIJIU_" + suffix) or default)


# 在该项目 mcp.json 的 env 里设 RXYY_MCP_DISABLE=1，zhi/ji 变成免打扰直通
DISABLED = _rxyy_env("DISABLE").strip() in ("1", "true", "yes")

# 图片内联阈值（base64 字符数）：默认 0 = 全部落盘给路径（Read 工具可直接看图），
# 一张截图就是几十万字符 base64，内联会白烧上下文；要恢复内联可设该环境变量
INLINE_IMAGE_B64_MAX = int(_rxyy_env("INLINE_IMAGE_MAX", "0"))

# ---- Cursor 120s 硬超时对策（2026-08-03 实测）----
# 新版 Cursor IDE 给 tools/call 加了 120s 协议层硬超时：到点客户端发
# notifications/cancelled、agent 收 -32001 Request timed out。SSE 注释心跳只在
# 传输层，协议层看不见，挡不住。日志铁证：每个 zhi 到达后恰好 120s 被取消。
# 双层防线：
# ① 请求带 progressToken 时，SSE 流里发真 notifications/progress——客户端若
#    honor resetTimeoutOnProgress，超时钟被不断重置，zhi 恢复无限挂（零 token）；
# ② progress 未证实有效（或已证实无效）时，每次 tools/call 最多阻塞
#    sse_call_budget_secs（默认 95s，含 5s 拍粒度余量 ≤100s < 120s）就把
#    KEEPALIVE 抛回 agent 续期——体面返回比被 -32001 掐死好：错误会触发
#    agent 的粗重试纪律（20-30s×6），KEEPALIVE 是既有静默续期路径。
# progress 是否有效由探测持久化在 .mcp-client-probe.json：
# - 探测法：zhi 带 task_name="__timeout_probe__"、message="150" → 服务端不进
#   hub、不打扰用户，睡 150s 自动应答；活过 120s = progress 有效（自动记 True）。
# - 自愈：线上 zhi 在无预算模式下于 100-150s 间被客户端取消 = progress 又失效
#   （Cursor 升级行为回退），自动记 False，下一拍即回安全模式。
_PROBE_STATE_PATH = DATA_DIR / ".mcp-client-probe.json"
_probe_state_cache = {"val": None, "mtime": None}
_active_sse_calls = {}   # (session/connection scope, rpc_id) -> info：在飞的 zhi SSE 请求
_sse_calls_lock = threading.Lock()


def _progress_resets():
    """持久化探测结论：True=progress 能重置客户端超时；False=不能；None=未知。"""
    try:
        m = _PROBE_STATE_PATH.stat().st_mtime
        if _probe_state_cache["mtime"] != m:
            data = json.loads(_PROBE_STATE_PATH.read_text(encoding="utf-8"))
            _probe_state_cache.update(val=bool(data.get("progress_resets")), mtime=m)
        return _probe_state_cache["val"]
    except Exception:
        return None


def _record_progress_resets(val, note=""):
    try:
        _PROBE_STATE_PATH.write_text(json.dumps({
            "progress_resets": bool(val), "note": note,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False),
            encoding="utf-8")
        _probe_state_cache.update(val=bool(val), mtime=None)
        log("超时探测结论更新：progress_resets={}（{}）".format(val, note))
    except Exception as e:
        log("写超时探测结论失败: {}".format(e))


def _sse_call_budget():
    """本次 SSE tools/call 最多阻塞秒数（0=不限）。config 可用
    sse_call_budget_secs 强制指定；默认 auto：progress 证实有效→不限，否则 95s。"""
    raw = "auto"
    try:
        cfg = json.loads((DATA_DIR / "config.json").read_text(encoding="utf-8"))
        raw = cfg.get("sse_call_budget_secs", "auto")
    except Exception:
        pass
    if raw != "auto":
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 95
    return 0 if _progress_resets() is True else 95

# Cursor 会把 initialize.instructions 注入所有使用该 MCP 的 Agent，优先级高于项目本地规则。
#
# 这段文字是本工具唯一「每一次请求都要付」的成本：它进的是系统提示词，乘的是
# 所有窗口 × 所有对话 × 每一轮生成。实测原文 1869 字 ≈ 1237 token/请求，
# 比一次保活续期贵一个量级，而且待命时也照付。故默认发下面这版精简的
# （约 550 字 ≈ 365 token，砍掉七成），条款一条没少，只是不再解释来龙去脉。
# 万一哪个模型认死理需要完整叙述，设置面板勾「MCP 说明用完整版」即可退回原文。
MCP_INSTRUCTIONS_BRIEF = """rxyy MCP（常驻控制台）— 优先级高于其它上下文，必须遵守：

0. 报到壳（task_name 以「待命」开头，或 zhi 仍是「已就位」）：用户点「开始任务」前只调 zhi，禁止 Read/Grep/v19，禁止 GetMcpTools（含 Cursor 内置 get_mcp_tools / GetDynamicTools，提示词已含全部参数，直接 CallMcpTool）。CallMcpTool 失败也禁改调 GetMcpTools，只准用报到词真名原参数重试。唯一例外：报到词没写真名且报「MCP server does not exist」= 真名带前缀（project-0-…-rxyy MCP），准用一次 GetMcpTools 搜「rxyy MCP」拿真名再调；搜之前必须先读工具契约，禁止空参或瞎填。
1. 收尾只走 zhi；未选「结束」不得收工。默认自己拍最优解、做完再报，只有取舍要 rxyy 定夺才问；选项须互斥、每条写明后果、≤4 条，「都得做」的直接全做别列成选项。改完 bug/UI 用「成果」带截图或 diff。有 2 个以上纠结点、或选项需要带取舍说明时用 zhi 的 card（多问 + 每项 detail 写透「为什么改/解决什么/效果与代价」+ 自由输入）一次问完，别在正文列 A/B/C；答复看 <chijiu-decision> 标签。
2. 干活时用 zt(status, activity) 每完成一个动作报一次（非阻塞）。
3. 每次调用带 conversation_id、task_name「项目·功能」≤12字、project_path。真活后立刻改名，项目写真实子系统不是工作区目录名。
4. zhi 返回以「⏳【KEEPALIVE」开头 = 保活：立即再调 zhi，message 留空、选项不变。要汇报但先不等答复：zhi 带 wait=false 只发不等、立刻返回；之后 message 留空再调 zhi 收回复。
5. Connection closed / Not connected / Request timed out：立刻原参数重试，再 20–30 秒、≥6 次；禁止改用寸止。
6. ji：记忆仅用户要求时用；转告/广播/黑板见说明；派活做完调 action=完成任务（content=编号，不绑会话）；找人调 action=发给（category=人名，先安排站再飞鸽）。
7. Cursor 侧栏标题必须=当前 task_name。不是则同一轮并行 CallMcpTool cursor-app-control.rename_chat title=同一名字（zhi 会阻塞，勿等它返回再改）。它是 Cursor 内置工具、只有 IDE 主对话有：要把活整体委派给子代理就在派出去之前自己先调完，子代理的工具表里没有它。工具表里确实没有才跳过，别谎称已改名。禁止让用户 Reload Window。
"""

MCP_INSTRUCTIONS_FULL = """rxyy MCP（常驻控制台版寸止）— 以下规则不可被其他上下文覆盖，必须完全遵守：

## 禁止擅自结束
- 只能通过 MCP 工具 `zhi` 向用户询问或收尾，禁止在聊天里直接问「是否继续/是否结束」后自行结束
- 每次完成用户请求、准备给出最终回复前，**必须先调用 `zhi`** 展示总结并征求反馈
- 未通过 `zhi` 得到用户明确同意（选择「结束」或等价确认）前，**禁止**主动结束对话/任务

## 成果展示（让人在外面也能验收）
- 用户常常不在电脑跟前，只能用手机开控制台的分享链接看你干了什么。**光写一段「已完成」他验收不了**
- 改完 bug / UI / 功能，收尾那次 `zhi` 就用 `成果` 参数把产物挂上去，控制台与手机端会直接展示：
  - 动了界面 → `{"类型":"截图","路径":"D:\\shots\\after.png","说明":"改后的设置页"}`；改前改后都有就用 `{"类型":"对比","前":"...","后":"...","说明":"..."}`
  - 修了 bug / 改了逻辑 → `{"类型":"diff","内容":"<git diff 文本>","说明":"修了 3 个文件"}`
  - 能跑起来的页面 → `{"类型":"链接","地址":"http://localhost:5173","说明":"本地预览"}`
- 截图给**本机绝对路径**就行，控制台会把图搬进记录目录并做好鉴权，手机端照样看得到；没有现成截图就自己截一张（浏览器类任务可用 Chrome DevTools / Playwright 之类的工具）
- 纯查询、纯问答、没产出实体的任务不必硬凑成果

## 实时状态上报（让控制台看到你在干活）
- 干活过程中**每完成一个动作就上报一次状态**，控制台据此实时显示你在做什么（非阻塞、极省 token、调完立即返回不打断你）
- 首选 `zt(status, activity)`；若环境未加载 zt 工具，用 `ji(action="状态", content="developing:改 get_state")` 等效
- 典型：开始分析→analyzing「读 hub.py」；改代码→developing「改 get_state」；跑测试→testing「跑单测」；卡住→blocked「依赖缺失」
- conversation_id 与 zhi 复用

## 少问多做（rxyy 08-24 立的规矩，优先于下面所有「不确定就问」的表述）
- 原话：「就按最完善、完美的解决方式，自行决定，不要一直问我了，要求结果正确，不存在任何隐患跟 bug」「按正确的最优的选项给、解决、去做就行了」
- **默认不问**：能从上下文、代码和实测判定哪条路最优，就直接做完，再用 `zhi` 报结果。几件事都得做，就全做完再报，不要摆成选项让他挑
- **该问才问**：路线实质不同且取舍取决于他的口味/优先级；动作不可逆或有生产风险；根因靠现有证据判不出来
- 真要问，选项得让人选得动：**互相排斥**（选了 A 就不该再选 B）、**每条一句话写明选了会怎样**、**最多 4 条**（「结束」不占额度）
- 三种形状会被服务端的选项闸当场退回，退回时不会送到 rxyy 眼前：
  - **伞形**：出现「都干 / 全部 N 批都办 / 两个都堵」这类把前面几条并起来的选项——它在场就等于承认那些条本来不互斥，直接全做
  - **雷同**：两条几乎是同一句话，或短的整个包在长的里
  - **超量**：要动手的选项超过 4 条
- 被退回就照它说的办：要么直接把活干完，要么把选项收敛成互斥的几条重发

## zhi 使用细节
- 需求不明确、有多方案、策略变更时，用 `zhi` 询问，提供 predefined_options（先过一遍上面「少问多做」）
- 有 2 个以上纠结点、或每个选项需要带取舍说明时，改传 `card`（多问 / 每项 detail / recommended / 自由输入 / 可选倒计时代决）一次问完，不要在正文里列 A/B/C；用户答复以「[决策答复] …」一行 + `<chijiu-decision answer='…'/>` 机器标签返回，优先读标签
- 要汇报进展、先说结论但暂时不等答复：`zhi` 带 `wait=false`（只发不等）——消息照常落到控制台，调用立刻返回，你接着干活；之后同 conversation_id、message 留空再调 `zhi` 即阻塞收回复。期间用户的回复由控制台先存着，来取时立刻交付；中途再发带正文的 zhi 会顶掉上一条提问，用户若已回复会随新提问的答复一起补送，不会丢
- 同一对话内所有 `zhi`/`ji` 调用复用同一 conversation_id（首次随机 8 位 hex）
- **用户消息中若已给出 conversation_id（如「conversation_id 固定用 xxxx」），必须沿用该 ID，禁止另行生成**
- zhi 返回末尾会回显本对话的 conversation_id，后续调用以它为准
- task_name 作为控制台 tab 标题，格式是「**项目·功能**」两段式（如「rxyy tools·换装收尾」「智慧云广播·日报取数」），共 12 字以内
  - **项目** = 你真正在开发的那个项目，**不是工作区目录名**。同一个目录下常并行着好几摊互不相干的活（cursor工作流 目录下同时有 rxyy tools、直播线、视频快编），拿目录名当项目等于没分。拿不准就看自己在改哪个子系统。
  - **功能** = 你手上这件事，动词开头最好认（「换装收尾」「日报取数」「外链修复」）。
  - 项目那半还会被当成业务线用来收窄广播与黑板提醒——写对了，不相干的队友就不会被你的进度汇报刷屏。
- 报到时叫「待命·<工作区>」没问题，但**接到真活后第一次 zhi/zt 就要把 task_name 换成「项目·功能」**：控制台一排 tab 全叫「待命·xxx」时，用户和队友都认不出谁在干什么
- **Cursor 侧栏标题必须等于当前 task_name**（用户 08-17 点名：一排全是 Persistent plus zhi report，分不清谁在干什么）。只把名字写进 Cursor 的 sqlite **不会**刷新正在运行的列表——界面读的是内存。真活改名时**同一轮并行** `CallMcpTool`：server=`cursor-app-control`，toolName=`rename_chat`，arguments=`{"title":"<当前 task_name>"}`。zhi 会阻塞，不能等它返回再改。`rename_chat` 是 Cursor 自带的内置工具，**只有 IDE 主对话的工具表里有它**——子代理／后台壳看不到，调不动也不报错，只是静默跳过（08-27 侧栏一整屏「Persistent plus zhi report」的根因就在这：活全被委派给子代理，主对话那一手改名从来没人做）。**要把任务整体委派给子代理，就在派出去的同一轮里并行把这次改名做掉，别把它一起委派下去。** 工具自述写 never rename proactively，但用户已经要求跟随 task_name，这是用户指令。工具表里确实没有才跳过，不要假装已改名。**禁止让用户 Reload Window**（会断 MCP/zhi）。
- 不自己改名的话，控制台会拿「派活那句话的前 24 个字」凑一个占位名顶上（用户 08-07 实测：一屏 tab 全是他自己第一句话的截断，谁也认不出谁）。你一报真名，那个占位就自动撤掉，以你报的为准。
- **每次调用都带 project_path=当前工作区完整路径**（控制台按它标注会话归属；多窗口共用 MCP 进程时尤其重要）
- 首次调用的 message 需一行写明：📍 <项目名>（<工作区完整路径>）· 对话 <conversation_id>

## zhi 保活续期（重要）
- 若 zhi 返回内容以「⏳【KEEPALIVE …】」（或旧式「⏳【系统保活…】」）开头，这**不是**用户回复，而是为躲避 Cursor 工具超时的自动续期信号
- 收到后**必须立即再次调用 zhi**，沿用其中给出的 conversation_id，message 留空即可，predefined_options 保持一致
- 首次续期可能约 45 秒就到、之后间隔逐次翻倍——短首拍是为了给同一 MCP 上其他对话的调用让路，属正常现象
- 每次续期信号带不同的计数/nonce，属正常现象；循环续期就是设计本身，禁止结束任务、禁止输出总结、禁止做其他事，直到收到用户真实回复

## 报错重试纪律（Connection closed / Not connected 不是失败）
- zhi/zt/ji 报「MCP error -32000: Connection closed」「Not connected」「Request timed out」= Cursor 正在回收/重启 MCP 进程，属常态，**约 30 秒内必然自愈**（控制台有 12s 紧急自愈 + 看门狗兜底）
- 处置：先**立即原参数重试一次**（多为瞬时重连竞态，马上就好）；仍失败再等 20-30 秒重试，**至少共 6 次（覆盖约 3 分钟）**；期间可穿插干别的活，但禁止宣告失败、禁止放弃报到、禁止改用其他工具或在聊天里直接收尾
- 若消息已发过但被打断，重试时原样重发即可，控制台会自动去重/补送

## 禁止降级到「寸止」等其他交互工具
- rxyy MCP 报错时**唯一正确动作是按上述纪律重试本服务器的 zhi**；**禁止**因报错改调「寸止」的 zhi 或任何其他弹窗工具——那会把对话踢出常驻控制台，用户会错过消息
- 即使其他规则把寸止列为后备，也仅当rxyy MCP 连续重试 6 次、跨度超 5 分钟仍全部失败时才允许；只要有一次成功就必须回到rxyy MCP

## ji 记忆
- 仅在用户明确要求记忆/回忆时调用（如「请记住：」）；禁止主动调用
- project_path 可省略，默认自动使用当前工作区目录

## agent 间互通（ji 借道）
- 给同项目某个 agent 递话：`ji(action="转告", category=对方tab名或对话ID前8位, content=消息)`
- 广播给全队：`ji(action="广播", content=消息)`；同一工作区里常并行着几条互不相干的业务线，只想喊自己这条线用 `ji(action="广播", category="本组", content=消息)`
- 对方若已被别人接手（换了对话ID），按你手上的旧 ID 照发即可，控制台会自动转投给现任并告诉你新 ID
- 投递走排队：对方下次调 zhi 或 zt 时收到（zt 顺路取信，所以它埋头干活时也收得到；正等用户回复的立刻收到）；找不到目标时你下次调 zhi 会收到失败说明
- 适用场景：要改公共文件先打招呼、发现别人负责范围的 bug 转告对方、完成对接点通知依赖方
- 任务安排站派给你的活做完后：`ji(action="完成任务", content=派发文案里的编号)`。不要按会话猜（一个窗口会接手、会连做多条），也不要等会话结束。
- 回需求人、或让某人做事：`ji(action="发给", category=人名, content=正文)`。系统先打对方任务安排站，没有或失败再飞鸽。你只要说发给谁。
"""

MCP_INSTRUCTIONS_DISABLED = (
    "rxyy MCP 处于禁用模式（RXYY_MCP_DISABLE=1）：zhi/ji 不强制调用，"
    "完成任务无需通过 zhi 收尾，正常结束对话即可。"
)


MCP_INSTRUCTIONS_NATIVE = instructions_for("codex")


def server_instructions(runtime_kind=None, client_info=None, profile=None,
                        disabled=None):
    """握手时现取，这样改完设置下次 Cursor 重连就生效，不用改代码重打包。"""
    if disabled is None:
        disabled = DISABLED
    # 没有连接上下文的旧调用者默认按 Cursor 兼容路径处理；真正的 HTTP/stdio
    # initialize 会显式传入 runtime_kind，未知 native client 也走短说明。
    legacy_default = runtime_kind is None and client_info is None and not profile
    if legacy_default:
        kind = "cursor"
    elif runtime_kind is not None:
        kind = normalize_runtime(runtime_kind)
    else:
        kind = infer_runtime(client_info, profile)
    if kind != "cursor":
        return instructions_for(kind, disabled=bool(disabled))
    if disabled:
        return MCP_INSTRUCTIONS_DISABLED
    try:
        full = bool(load_cfg().get("mcp_instructions_full", False))
    except Exception:
        full = False
    return MCP_INSTRUCTIONS_FULL if full else MCP_INSTRUCTIONS_BRIEF

_stdout_lock = threading.Lock()

# ---------------- 项目目录探测 ----------------
CLIENT_CAPS = {}
CLIENT_ROOTS = {"dir": None}
ROOTS_REQ_ID = "rxyy-mcp-roots-1"
ROOTS_EVENT = threading.Event()  # roots/list 已应答（Cursor 重启后 zhi 常抢在 roots 之前）


def _uri_to_path(uri):
    """file:///D:/xx 形式转本地路径；普通路径原样返回"""
    if not uri:
        return None
    if uri.startswith("file://"):
        p = unquote(urlparse(uri).path or "")
        if re.match(r"^/[A-Za-z]:", p):
            p = p[1:]
        return str(Path(p)) if p else None
    return uri


def _is_runtime_cwd(path):
    """MCP 进程 cwd 经常是常驻区/包内，不能当作用户工作区。"""
    parts = [x.lower() for x in str(path or "").replace("/", "\\").split("\\") if x]
    for i, part in enumerate(parts):
        if part == "rxyy-tools-community" and i + 1 < len(parts) and parts[i + 1] in ("live", "_internal"):
            return True
    return False


def detect_project_dir(client_context=None):
    """项目目录优先级：MCP roots > Cursor 工作区环境变量 > 进程 cwd。

    Cursor 重启后 agent 立即调 zhi 时，roots/list 应答常常还没回来——
    此时短暂等待，避免 hello 带着 os.getcwd()（家目录或 live 常驻区）接入
    控制台导致 tab 对不上号。
    """
    context_root = str(getattr(client_context, "root_path", "") or "").strip()
    if context_root and not _is_runtime_cwd(context_root):
        return context_root
    if CLIENT_ROOTS["dir"] and not _is_runtime_cwd(CLIENT_ROOTS["dir"]):
        return CLIENT_ROOTS["dir"]
    env = (os.environ.get("WORKSPACE_FOLDER_PATHS") or "").strip()
    if env:
        for sep in (os.pathsep, ","):
            if sep in env:
                parts = [x.strip() for x in env.split(sep) if x.strip()]
                if parts:
                    got = _uri_to_path(parts[0])
                    if got and not _is_runtime_cwd(got):
                        return got
                    break
        else:
            got = _uri_to_path(env)
            if got and not _is_runtime_cwd(got):
                return got
    caps = getattr(client_context, "capabilities", None)
    caps = caps if isinstance(caps, dict) else CLIENT_CAPS
    if client_context is None and "roots" in caps and not ROOTS_EVENT.is_set():
        ROOTS_EVENT.wait(2.5)
        if CLIENT_ROOTS["dir"] and not _is_runtime_cwd(CLIENT_ROOTS["dir"]):
            return CLIENT_ROOTS["dir"]
    cwd = os.getcwd()
    if _is_runtime_cwd(cwd):
        return ""
    return cwd


def load_hub_target():
    """返回 (host, port, token)。

    hub_host 为远程 IP 时，本 MCP 把会话接入那台机器上的rxyy MCP控制台
    （AI 仍在本机干活，控制台/分享页在对方机器上显示）；token 用于远程接入鉴权。
    环境变量 RXYY_MCP_HUB_HOST / RXYY_MCP_HUB_TOKEN 优先于 config.json；旧名仍兼容。
    """
    cfg = {}
    try:
        cfg = json.loads((DATA_DIR / "config.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    host = (_rxyy_env("HUB_HOST") or cfg.get("hub_host") or "127.0.0.1").strip()
    token = (_rxyy_env("HUB_TOKEN") or cfg.get("hub_token")
             or cfg.get("share_token") or "")
    try:
        port = int(cfg.get("port", 38999))
    except Exception:
        port = 38999
    return host or "127.0.0.1", port, str(token)


def hub_is_local(host):
    return host in ("127.0.0.1", "localhost", "::1")


class ZhiCancelled(Exception):
    """Cursor 取消了 tools/call（用户在 IDE 继续对话/按停止），本次 zhi 作废"""


class ZhiKeepAlive(Exception):
    """用户长时间未回复：为躲开 Cursor tool-call 硬超时，本次 zhi 提前返回，
    要求 AI 立即用同一 conversation_id 再次调用 zhi 续期（控制台的提问与绿灯保持不变）。

    借鉴 kc-chat 的 poll_tick：带续期次数 ticks 和累计已等待秒数 elapsed，
    让每次续期返回内容都不同，杜绝 AI 因「消息重复」而偷懒不再续期。"""

    def __init__(self, conversation_id, ticks=1, elapsed=0, hub_down=False):
        super().__init__(conversation_id)
        self.conversation_id = conversation_id
        self.ticks = ticks
        self.elapsed = elapsed
        # hub_down=True：本次续期不是「用户未回复」而是「控制台不可达」——zhi 转入
        # 排队等待（07-27 事故根治：hub 挂掉期间 agent 不再收到报错，续期节拍即重试轮询）
        self.hub_down = hub_down


_SRV_LOG = APP_DIR / "server-run.log"
_srv_log_lock = threading.Lock()


def log(msg):
    # 只写 server-run.log，绝不碰 stderr：Cursor 把 MCP 进程的任何 stderr 输出
    # 一律记成 [error] 级别刷进 MCP Logs（还因编码显示成乱码），像崩溃一样吓人。
    # 寸止等单工具 MCP 从不写 stderr 所以从无此类"报错"，这里对齐。
    # 512KB 轮转（watchdog/hub 早有，server 此前缺失，误拉事故一天就能刷几百 KB）。
    try:
        with _srv_log_lock:
            try:
                if _SRV_LOG.exists() and _SRV_LOG.stat().st_size > 512 * 1024:
                    bak = _SRV_LOG.with_suffix(".log.1")
                    if bak.exists():
                        bak.unlink()
                    _SRV_LOG.rename(bak)
            except OSError:
                pass
            with open(_SRV_LOG, "a", encoding="utf-8") as f:
                f.write("{} pid={} {}\n".format(
                    time.strftime("%m-%d %H:%M:%S"), os.getpid(), msg))
    except Exception:
        pass


def load_cfg():
    """读控制台配置（与 hub 同一份 config.json）。读不到就当默认值，绝不抛。"""
    try:
        return json.loads((DATA_DIR / "config.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


# ---------------- Hub 连接管理 ----------------
class HubBridge:
    """与常驻控制台的连接桥。

    单条 TCP 连接上支持多个对话（conversation_id）并发提问：
    读取线程按请求 id 把应答分发给各自的等待方，发送用锁串行化。
    """

    # 取信连丢这么多次就认定「这版 hub 不认 agent_mail_fetch」，不再为每次 zt 白等。
    # 源码版 server 配打包版 hub 是会真实发生的（见 _reader_dispatch 里的版本混跑
    # 注释），那种组合下老 hub 对这个消息类型没有分支、一声不吭，而 zt 是 agent
    # 每完成一个动作就调的——白等的两秒会加在每一步上。
    MAIL_MISS_LIMIT = 3

    # 心跳名单的落盘位置，见 restore_conv_registry
    CONV_REGISTRY_FILE = ".mcp-convs.json"

    # pre_cancelled 兜底记录的有效期（秒）：只覆盖「notifications/cancelled 抢在
    # 对应 tools/call 之前到达」这一竞态；超时的取消号视为陈旧（多半是别的窗口
    # 留下的同号），一律不再命中，杜绝跨窗口 rpc_id 撞号导致 zhi 秒挂 -32800。
    PRE_CANCEL_TTL = 15.0

    def __init__(self):
        self.sock = None
        self.conn_lock = threading.Lock()    # 保护连接建立/替换
        self.send_lock = threading.Lock()
        self.waiters = {}                    # req_id -> {event, resp, sock, rpc_id, conversation_id, cancelled}
        self.waiters_lock = threading.Lock()
        # 取消通知先于 waiter 注册到达时的兜底：rpc_id -> 取消时刻。带时间戳按
        # TTL 过期是关键——一个 MCP 端点服务所有 Cursor 窗口，rpc_id 是每个连接
        # 各自从小整数递增的，纯数字长期记忆会让 A 窗口取消的号误杀 B 新窗口从
        # 同号起步的 zhi（连请求都不发直接 -32800）。详见 _purge_expired_cancels。
        self.cancelled_rpc_ids = {}
        # 漏传 conversation_id 的调用统一记到这个「本进程自己 mint 的」ID 上：
        # 它是唯一敢写进回执/兜底的 ID。绝不存别的对话报过的显式 ID 给漏传者
        # 复用——那就是 08-12 串台事故（见 _ensure_conversation_id）。
        self._minted_conv_id = None
        self._idle_timers = {}               # conversation_id -> Timer（一个 MCP 承载多对话，必须分开计时）
        self._idle_timer_lock = threading.Lock()
        self._keepalive_active = set()       # conversation_id：上次 zhi 因保活提前返回，本轮为续期重呼
        self._keepalive_stats = {}           # conversation_id -> {"ticks", "started"}：续期计数与起始时刻（poll_tick 风格）
        # conversation_id：上一条 zhi 是 wait=false「只发不等」，提问挂在控制台等 agent 回来收。
        # 与 _keepalive_active 的区别：它期间 agent 再发一条带正文的 zhi 是「新提问」而不是续期
        self._deferred_convs = set()
        # conversation_id -> 最近一次真提问的 payload 要素。hub 重启后 pending 内存态
        # 已丢，续期空 message 重呼会渲染成空气泡——用缓存原文重建提问（hub 零改动）
        self._zhi_payload_cache = {}
        self._active_convs = set()           # 本 MCP 进程服务过的 conversation_id（供心跳上报，让 hub 实时确知进程存活）
        self._conv_activity = {}             # conversation_id -> 最近一次 zhi/zt 时刻（心跳只上报活跃对话）
        self._reconn_last_fail = 0.0         # 静默续连上次失败时刻（10s 退避，避免每个心跳周期白撞）
        self._hb_started = False             # 心跳线程只启动一次
        self._yield_until = 0.0              # 让路窗口截止时刻：期间等待中的 zhi 用短拍快速轮转
        self._yield_returned = {}            # conversation_id -> 已为哪个让路窗口真返回过一次（腾槽位只需一次）
        self._late_mail = {}                 # conversation_id -> 取信应答迟到的那些话（hub 已从队列摘走，丢了就没了）
        self._mail_sock = None               # 取信支持性是按连接判定的，换连接就重新给一次机会
        self._mail_misses = 0
        self._mail_dead = False              # 这版 hub 不认取信，别再为每次 zt 白等超时
        self._registry_sig = None            # 上次写盘的心跳名单，没变就别每 5s 白写一次
        self._registry_saved = 0.0

    def _cancel_idle_timer(self, conversation_id):
        with self._idle_timer_lock:
            t = self._idle_timers.pop(conversation_id, None)
        if t:
            t.cancel()

    def _send_detach(self, sock, req_id, conversation_id):
        """告诉 hub 本请求暂时没有等待方（提问与绿灯保留，用户回复走缓存）。发送失败
        不抬错：hub 那边下一拍看门狗/续期重呼都能自愈，这里失败只会多亮一会儿绿灯。"""
        try:
            with self.send_lock:
                send_msg(sock, {
                    "type": "zhi_detach",
                    "id": req_id,
                    "conversation_id": conversation_id,
                })
        except Exception:
            pass

    @staticmethod
    def _deferred_peek_secs(cfg):
        """wait=false 时抓 hub 即时答复的窗口。hub 在同一读线程里落卡后立刻
        _flush_queue（排队消息 / 脱离期缓存回复），本机 socket 一来一回加上身份校准
        的 sqlite 读也就几十毫秒，1.5s 绰绰有余；下限 0.2s 防配置写 0 把即时答复漏掉。"""
        try:
            return max(0.2, float((cfg or {}).get("deferred_peek_secs", 1.5)))
        except Exception:
            return 1.5

    def _prune_conv_caches(self, max_convs=64, max_age=86400):
        """守护进程常驻数周不死，对话簿记（payload 缓存/续期统计/活跃时刻）只增不减
        会缓慢漏内存。超过 max_convs 时，把 24h 无活动的对话从各簿记里剔除。"""
        try:
            if len(self._active_convs) <= max_convs:
                return
            now = time.time()
            with self.waiters_lock:
                inflight = {w.get("conversation_id") for w in self.waiters.values()}
            stale = [c for c in list(self._active_convs)
                     if c not in inflight
                     and now - self._conv_activity.get(c, 0) > max_age]
            for c in stale:
                self._active_convs.discard(c)
                self._conv_activity.pop(c, None)
                self._zhi_payload_cache.pop(c, None)
                self._keepalive_stats.pop(c, None)
                self._keepalive_active.discard(c)
                self._deferred_convs.discard(c)
        except Exception:
            pass

    def _conv_window(self):
        """心跳认定「这个对话还活跃」的时间窗（秒）。上报与落盘共用一个口径。"""
        try:
            return max(300, int(_read_cfg().get("processing_timeout_secs", 1800)))
        except Exception:
            return 1800

    def _convs_to_report(self, window, now=None):
        """这一拍该报哪些对话还活着：有 zhi 在飞的，或窗口内有过 zhi/zt 的。

        守护进程常驻不死、_active_convs 只增不减——若全量上报，早已收工的对话会在
        控制台重启后被心跳错误复活成「活着」，状态反向失真。
        """
        now = now or time.time()
        with self.waiters_lock:
            inflight = {w.get("conversation_id") for w in self.waiters.values()}
        return [c for c in list(self._active_convs)
                if c in inflight or now - self._conv_activity.get(c, 0) < window]

    def _save_conv_registry(self):
        """把心跳名单写盘，与盘上已有的合并（同一对话取较新的时刻）。

        合并而不是覆盖：一台机器上不止一个 MCP 进程时（守护进程 + 偶发的 stdio
        实例），直接覆盖会把对方的对话抹掉，那些 tab 就又没人替它们报活了。
        """
        try:
            now, window = time.time(), self._conv_window()
            path = DATA_DIR / self.CONV_REGISTRY_FILE
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            if not isinstance(data, dict):
                data = {}
            for c in list(self._active_convs):
                ts = float(self._conv_activity.get(c, 0) or 0)
                try:
                    if ts > float(data.get(c) or 0):
                        data[c] = ts
                except (TypeError, ValueError):
                    data[c] = ts
            keep = {}
            for c, ts in data.items():
                try:
                    ts = float(ts or 0)
                except (TypeError, ValueError):
                    continue
                if c and now - ts < window:
                    keep[c] = ts
            tmp = DATA_DIR / (self.CONV_REGISTRY_FILE + ".tmp")
            tmp.write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")
            os.replace(str(tmp), str(path))
        except Exception:
            pass  # 名单是锦上添花，写不进去也绝不能碍着 agent 干活

    def restore_conv_registry(self):
        """MCP 进程换了一条命之后，把上一条命服务过的对话捞回心跳名单。

        心跳是 hub 判「这些 agent 还活着」的实时信号：收到就把 tab 复活成在线
        （hub.Hub._resolve_for_signal）。而名单此前只在内存里，换装 / 热拷 / 看门狗
        救活一换掉 MCP 进程，名单跟着进程一起没了——那些 agent 明明还在 Cursor 里
        跑着，控制台却在重连宽限（hub 重启后 90s）到期后一律翻成「已终止」，只能
        等它们各自下次调 zhi/zt 才一个个复活。08-07 12:16 实测：19 个 tab 有 15 个
        被这么误判，而用户在 Cursor 里看得见它们全在 Generating。

        只捞窗口内还活跃的，口径与上报那份完全一致：早已收工的对话不该被一次重启
        凭空复活成「活着」——那是反方向的失真，比误判死更难发现。
        """
        try:
            data = json.loads(
                (DATA_DIR / self.CONV_REGISTRY_FILE).read_text(encoding="utf-8"))
        except Exception:
            return []
        if not isinstance(data, dict):
            return []
        now, window, back = time.time(), self._conv_window(), []
        for c, ts in data.items():
            try:
                ts = float(ts or 0)
            except (TypeError, ValueError):
                continue
            if c and now - ts < window:
                self._active_convs.add(c)
                self._conv_activity.setdefault(c, ts)
                back.append(c)
        return back

    def _ensure_heartbeat(self):
        """启动心跳线程：每 5s 向 hub 上报本进程服务的所有 conversation_id 仍存活。
        hub 据此把「MCP 进程被 Cursor 回收」（心跳停）和「对话在 IDE 里正常进行」
        （心跳持续）区分开——这是不 patch 内核也能拿到的、进程级 100% 实时信号。"""
        with self._idle_timer_lock:  # 复用现成锁，防两个线程并发各起一个心跳线程
            if self._hb_started:
                return
            self._hb_started = True

        def loop():
            cfg_cache = {"ts": 0.0, "window": 1800}
            while True:
                time.sleep(5)
                if not self._active_convs:
                    continue
                # config 读取 30s 缓存一次：别让心跳每 5s 白读一次磁盘。
                if time.time() - cfg_cache["ts"] > 30:
                    cfg_cache["window"] = self._conv_window()
                    cfg_cache["ts"] = time.time()
                    self._prune_conv_caches()
                window = cfg_cache["window"]
                now = time.time()
                convs = self._convs_to_report(window, now)
                # 名单落盘：本进程被换掉后，新进程靠它把这些 tab 接着报活，
                # 而不是让控制台把一屋子还在干活的 agent 判成已终止
                sig = tuple(sorted(convs))
                if sig != self._registry_sig or now - self._registry_saved > 60:
                    self._registry_sig, self._registry_saved = sig, now
                    self._save_conv_registry()
                # 无论有无活跃对话都静默续连（仅当 hub 已在运行，绝不拉起窗口）：
                # 控制台重启后哪怕全员闲置，连接级心跳也要尽快续上，否则 hub 一直
                # 显示「心跳中断/假死」横幅（误报）。hub 没跑时本机 connect 立即被拒、
                # 纳秒级返回并有 10s 退避，代价可忽略。
                sock = self._reconnect_if_hub_up()
                if sock is None:
                    continue
                try:
                    with self.send_lock:
                        send_msg(sock, {"type": "mcp_heartbeat", "conversations": convs,
                                        "pid": os.getpid()})
                except Exception:
                    pass  # 发送失败=连接刚死，下个心跳周期自动续连

        t = threading.Thread(target=loop, daemon=True)
        t.start()

    def _schedule_idle_clear(self, conversation_id):
        """用户回复后 AI 若长时间不再 zhi，自动清 processing 状态（避免控制台假死）"""
        cid = conversation_id or self._minted_conv_id
        if not cid:
            return
        self._cancel_idle_timer(cid)
        cfg = {}
        try:
            cfg = json.loads((DATA_DIR / "config.json").read_text(encoding="utf-8"))
        except Exception:
            pass
        delay = int(cfg.get("processing_timeout_secs", 1800))

        def fire():
            with self._idle_timer_lock:
                self._idle_timers.pop(cid, None)
            # 只在连接还活着时发送；绝不在这里 _connect_locked——
            # 否则控制台已被用户关闭时，10 分钟后定时器会凭空把窗口拉起来
            with self.conn_lock:
                sock = self.sock
            if sock is None:
                return
            try:
                with self.send_lock:
                    send_msg(sock, {"type": "processing_clear", "conversation_id": cid})
            except Exception:
                pass

        timer = threading.Timer(delay, fire)
        timer.daemon = True
        with self._idle_timer_lock:
            self._idle_timers[cid] = timer
        timer.start()

    def _try_connect(self, deadline=None):
        def budget(seconds):
            remaining = seconds if deadline is None else min(seconds, deadline - time.monotonic())
            if remaining <= 0:
                raise ConnectionError("本次连接等待时间已用尽")
            return remaining
        host, port, token = load_hub_target()
        if _INPROC_HUB is not None and hub_is_local(host):
            # 39222 挂在 hub 进程内（第二刀）：不走 TCP，进程内管道直达 Hub。
            # hub 端与 TCP 时代一样是一条专属读取线程跑 handle_client——
            # hello 握手、串行处理、断连语义全部复用，只是没了网络栈。
            s, hub_end = InProcSock.pair()
            threading.Thread(
                target=_INPROC_HUB.handle_client, args=(hub_end, ("127.0.0.1", 0)),
                daemon=True, name="hub-inproc-mcp").start()
        else:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(budget(5))
            s.connect((host, port))
        # 握手阶段保留超时：若 hub 接受了 TCP 却迟迟不回 hello_ack（hub 卡住/半死），
        # 不能无限阻塞——否则整个 MCP 卡死。10s 拿不到 ack 就判失败重来。
        try:
            send_msg(s, {
                "type": "hello",
                "cwd": detect_project_dir(),
                "pid": os.getpid(),
                "token": token,
            })
            s.settimeout(budget(10))
            ack = recv_msg(s)
        except Exception:
            try:
                s.close()
            except Exception:
                pass
            raise ConnectionError("hub 握手超时/失败")
        if not ack or ack.get("type") != "hello_ack":
            s.close()
            raise ConnectionError("hub 握手失败（远程接入时请检查令牌是否正确）")
        s.settimeout(None)  # 握手成功后转阻塞常驻（reader 线程要阻塞 recv）
        return s

    def _spawn_hub(self):
        # 07-27 15:06 事故：本路径拉起的 hub 在孤儿 WebView2 沼泽里卡了 2 分钟才写出
        # 首条日志——孤儿清理此前只接在看门狗路径上。这里异步清（powershell 要几秒，
        # 不能卡 zhi），清理与 hub 的 import 阶段并行，GUI 初始化前多半已清完。
        def _cleanup():
            try:
                from watchdog import kill_orphan_webview2
                kill_orphan_webview2()
            except Exception:
                pass

        threading.Thread(target=_cleanup, daemon=True).start()
        from frozen_boot import hidden_popen_kwargs, script_dir, spawn_argv
        try:
            from watchdog import spawn_stderr_handle
            err = spawn_stderr_handle("hub.py")
        except Exception:
            err = subprocess.DEVNULL
        try:
            cmd = spawn_argv(APP_DIR / "hub.py", "--daemon")
            subprocess.Popen(
                cmd, cwd=script_dir(cmd, APP_DIR),
                **hidden_popen_kwargs(stderr=err),
            )
        finally:
            if err is not subprocess.DEVNULL:
                try:
                    err.close()
                except OSError:
                    pass

    _SPAWN_LOCK = APP_DIR / ".hub-spawn.lock"

    def _should_spawn_hub(self):
        """跨 MCP 进程去重拉起 hub：控制台彻底关闭后，多个 MCP 进程（HTTP 守护 +
        各 stdio）会同时发现连不上而各自拉 hub → 瞬间 N 个 hub 抢端口、收敛混乱
        （正是『重启后要管理员清进程才好』的诱因之一）。用原子锁文件让只有一个进程
        负责拉起，其余只等待。返回 True=由本进程负责拉起。"""
        now = time.time()
        try:
            if self._SPAWN_LOCK.exists() and now - self._SPAWN_LOCK.stat().st_mtime < 20:
                return False  # 20s 内已有进程在拉起，本进程只等待
        except OSError:
            pass
        try:
            fd = os.open(str(self._SPAWN_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, str(os.getpid()).encode())
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            try:
                if now - self._SPAWN_LOCK.stat().st_mtime < 20:
                    return False
                os.utime(self._SPAWN_LOCK, None)  # 旧残留锁（上次拉起失败），接管
            except OSError:
                pass
            return True
        except OSError:
            return True  # 锁机制不可用则退化为原行为（宁可多拉也不能不拉）

    def _release_spawn_lock(self):
        try:
            self._SPAWN_LOCK.unlink()
        except OSError:
            pass

    def _connect_locked(self, wait_secs=45, deadline=None):
        """持有 conn_lock 时调用；返回可用 socket，必要时拉起控制台。

        控制台在远程机器（hub_host 非本机）时不能替对方拉起，只能重试等待。
        wait_secs：拉起后等待接入的窗口。zhi 保活开启时传短窗（约 8s）——连不上会转
        KEEPALIVE 排队而非报错，agent 续期节拍就是重试轮询；持锁 45s 死等反而会把
        并发对话全部串行卡住（07-27 事故教训）。
        deadline：可选的单次调用绝对 monotonic 截止时刻，供原生客户端把连接建立
        时间与用户回复等待合并计入 45 秒上限；旧 Cursor 路径不传此参数。
        """
        if self.sock is not None:
            return self.sock
        host, _, _ = load_hub_target()
        try:
            sock = self._try_connect(deadline=deadline) if deadline is not None else self._try_connect()
        except Exception:
            if not hub_is_local(host):
                raise ConnectionError(
                    f"无法连接远程rxyy MCP控制台 {host}（请确认对方控制台在运行且防火墙放行）")
            # 快速失败：几秒前刚有一轮「拉起+等待」失败收场，hub 必然还在启动中——
            # 别再持着 conn_lock 干等 8s（多对话保活轮转时会串行连坐，每个都卡一轮），
            # 直接抛错让调用方转 KEEPALIVE 排队，下一拍自然重试
            if time.time() - getattr(self, "_connect_fail_ts", 0) < 4:
                raise ConnectionError("控制台仍在启动中（快速失败，下一拍重试）")
            # 跨进程去重：只有抢到锁的那个进程真正拉起 hub，其余只等待，避免抢端口
            spawned = self._should_spawn_hub()
            if spawned:
                log("控制台未运行，正在启动…")
                self._spawn_hub()
            else:
                log("控制台未运行，另一进程正在拉起，等待接入…")
            connect_deadline = (deadline if deadline is not None else
                                time.monotonic() + max(3, wait_secs))
            sock = None
            try:
                while sock is None:
                    remaining = connect_deadline - time.monotonic()
                    if remaining <= 0:
                        self._connect_fail_ts = time.time()
                        raise ConnectionError("无法启动或连接rxyy MCP控制台")
                    time.sleep(min(0.5, remaining))
                    try:
                        sock = self._try_connect(deadline=deadline) if deadline is not None else self._try_connect()
                    except Exception:
                        if time.monotonic() >= connect_deadline:
                            self._connect_fail_ts = time.time()
                            raise ConnectionError("无法启动或连接rxyy MCP控制台")
            finally:
                if spawned:
                    self._release_spawn_lock()  # 已连上/放弃，释放锁供后续重连
        self._connect_fail_ts = 0.0
        self.sock = sock
        threading.Thread(target=self._reader, args=(sock,), daemon=True).start()
        return sock

    def _reader(self, sock):
        """常驻读取线程：把 zhi_response / mail_response 按请求 id 分发给等待方"""
        while True:
            try:
                msg = recv_msg(sock)
            except Exception:
                msg = None
            if msg is None:
                break
            self._reader_dispatch(msg)
        self._drop(sock)
        with self.waiters_lock:
            for w in self.waiters.values():
                if w["sock"] is sock:
                    w["event"].set()  # resp 保持 None，等待方按断连处理

    def _reader_dispatch(self, msg):
        """处理 hub 发来的一条消息（从 _reader 拆出来，便于直接喂消息做测试）。"""
        if msg.get("type") in ("zhi_response", "mail_response", "summary_response"):
            with self.waiters_lock:
                w = self.waiters.get(msg.get("id"))
            if w is not None:
                w["resp"] = msg
                w["event"].set()
            elif msg.get("type") == "mail_response":
                self._stash_late_mail(msg)
            # 迟到的 summary_response 直接丢：摘要是即时问答，下一趟重问就是
        elif msg.get("type") == "yield_zhi":
            # hub 通知让路：有新报到刚到/用户点了「+」，同批很可能还有调用被
            # Cursor 共享 MCP 的并发上限堵在门外——立刻把等待中的 zhi 全部保活
            # 续期（释放并发槽位），并在窗口期内用短拍轮转，让后续调用秒进。
            #
            # 「并发上限」这个前提已证伪（Cursor 的 withInFlightOp 只计数不限流、
            # 8 个 zhi 挂着时新调用 2ms 应答、六个真实窗口 5 秒内全部抵达），
            # 而每让一次路就有 agent 白跑一轮 LLM 生成，故默认忽略。这里也判一次
            # 而不只靠 hub 不发：新旧版本可能混跑（源码版 server + 打包版 hub）。
            if not load_cfg().get("yield_on_burst", False):
                return
            try:
                secs = float(msg.get("secs") or 90)
            except Exception:
                secs = 90.0
            self._yield_until = time.time() + max(10.0, min(secs, 600.0))
            excl = (msg.get("exclude") or "").strip()
            # hub 会点名「等得最久的那一两个」：腾一两个槽位就够放行新报到，
            # 全叫醒等于让所有待命 agent 白跑一轮生成。旧版 hub 不带 convs，
            # 那就沿用老语义（全叫）
            only = [c for c in (msg.get("convs") or []) if c]
            with self.waiters_lock:
                for w in self.waiters.values():
                    if w.get("resp") is not None or w.get("cancelled"):
                        continue
                    if w.get("kind") == "mail":
                        continue  # 取信不占并发槽，叫醒它只是白丢一轮信
                    conv = w.get("conversation_id")
                    if excl and conv == excl:
                        continue
                    if only and conv not in only:
                        continue
                    w["force_ka"] = True
                    w["event"].set()

    def _stash_late_mail(self, msg):
        """取信应答迟到了（超时那一刻等待方已经走了）：先收着，下一趟 zt 交出去。

        hub 那头是「先从队列摘下来、再往 socket 上写」，所以这条应答里的话在
        队列里已经没有了——这里再丢掉，这条转告就两头都不存在了。

        只认 hub 明确回带的 conversation_id：一个 MCP 进程服务本机所有 Cursor
        窗口，认不出归属宁可不收。把别人的转告塞给这个 agent 比丢一条严重得多
        （见 own_conv_id_hint 那次一路改绑的事故）。
        """
        cid = (msg.get("conversation_id") or "").strip()
        items = [x for x in (msg.get("items") or [])
                 if isinstance(x, str) and x.strip()]
        if not cid or not items:
            return
        with self.waiters_lock:
            box = self._late_mail.setdefault(cid, [])
            box.extend(items)
            del box[:-20]  # 没人来取就别无限涨（tab 早已终止的情况）

    def _drop(self, sock):
        with self.conn_lock:
            if self.sock is sock:
                self.sock = None
        try:
            sock.close()
        except Exception:
            pass

    def _reconnect_if_hub_up(self):
        """静默续连：仅当 hub 已在运行时重建连接；绝不 spawn、失败立即返回。

        控制台重启后所有 MCP 进程的旧连接同时死亡，过去要等各 agent 下一次 zhi
        才重连，期间 tab 全显示断开（agent 明明活着，状态失真）。心跳线程与 zt
        上报借此主动续上。控制台是用户主动关掉的话，本机 connect 立即被拒、
        静默跳过（10s 退避），绝不会凭空把窗口拉起来。

        conn_lock 只等 1s：hub 重启期间 ask() 的「拉起+等待」会持锁最长 8s，
        心跳/zt 是锦上添花，绝不为它排队（排队会让心跳周期抖到 >60s，
        触发控制台「心跳中断」横幅误报——07-27 状态误报诱因之一）。
        """
        if not self.conn_lock.acquire(timeout=1):
            return None
        try:
            if self.sock is not None:
                return self.sock
            if time.time() - self._reconn_last_fail < 10:
                return None
            try:
                sock = self._try_connect()
            except Exception:
                self._reconn_last_fail = time.time()
                return None
            self.sock = sock
            threading.Thread(target=self._reader, args=(sock,), daemon=True).start()
            return sock
        finally:
            self.conn_lock.release()

    def _ensure_conversation_id(self, conversation_id):
        """显式 ID 原样用、互不影响；漏传时只补「本进程自己 mint 的」那一个 ID，
        绝不复用别的对话报过的 ID。

        旧实现把最后一个显式 ID 存进进程级隐式槽给漏传者复用——HTTP 模式下一个
        进程服务本机所有窗口，漏传者会直接冒用「最后说话的那个对话」的身份：
        08-12 09:03 实测，OA对接 的 zhi 漏带 conversation_id，5 秒前 defbeed7
        刚 zt 过，整段探查汇报连提问带选项落进了 video-editor 的 tab，用户当场
        抓到串台。08-04 的一路改绑事故只把回执这半边修了（own_conv_id_hint），
        消息本体这半边就是本条。宁可给漏传者按新对话记账（开新 tab，一眼可见、
        回执还会教它带 ID），也不能冒名顶替污染别人的会话。"""
        cid = (conversation_id or "").strip()
        if cid:
            return cid
        if not self._minted_conv_id:
            self._minted_conv_id = uuid.uuid4().hex[:8]
        return self._minted_conv_id

    def own_conv_id_hint(self, conversation_id):
        """回执结尾那句「本对话 conversation_id: X，后续必须沿用」该写谁。

        一个 MCP 进程服务本机所有 Cursor 窗口，_implicit_conv_id 是「最后一个报上来
        的人」的 ID——没传 ID 的调用方拿它填回执，等于把别人的 ID 塞给这个 agent，
        而 agent 会老实照办。08-04 实测：一个 agent 被一路改绑
        ba71f55d → d17f7c53 → 8e154366 → 542c7fdc → 627f39b7，最后收到的是别的
        项目那条线的回复，它自己的提问反被判成「已被更新的提问取代」。
        所以只有两种情况敢说：调用方自己传了 ID（原样回给它，是它自己的），
        或者这个 ID 就是本进程替某个没传 ID 的调用方现开的。别人的一律不说。
        """
        return (conversation_id or "").strip() or self._minted_conv_id

    def report_status(self, conversation_id, status, activity, task_name=None,
                      model=None, cwd=None, runtime_kind="cursor",
                      native_thread_id=None, rpc_scope=None):
        """真·非阻塞：向 hub 上报 agent 干活状态。
        绝不 spawn hub、绝不等待——hub 没连上就直接跳过（状态上报是锦上添花，
        不能因为它卡住 agent 的正常工作）。只在已有连接上尝试发送。

        task_name 一并捎上：纪律要求「接到真活后第一次 zhi/zt 就换成项目·功能」，
        而 zhi 是收尾才调的，中间几十分钟全靠 zt。以前 zt 这条路根本不带名字，
        agent 照做也白做，控制台还反过来催它改名（_nudge_rename_if_standby）。
        model：Codex 没有 Cursor 库可探，靠这一口把模型牌点亮。"""
        cid = self._ensure_conversation_id(conversation_id)
        self._active_convs.add(cid)
        self._conv_activity[cid] = time.time()
        self._ensure_heartbeat()
        # 不触发 _connect_locked 的 spawn+等待；但 hub 已在运行时顺手静默续连——
        # 控制台重启后的 zt 不再整段丢失（hub 不在则本机 connect 立即被拒，纳秒级返回）
        sock = self._reconnect_if_hub_up()
        if sock is None:
            return []
        try:
            payload = {
                "type": "agent_status",
                "conversation_id": cid,
                "status": status or "",
                "activity": activity or "",
                "cwd": cwd or detect_project_dir(),
                "runtime_kind": normalize_runtime(runtime_kind),
                "native_thread_id": (str(native_thread_id).strip() or None
                                      if native_thread_id is not None else None),
            }
            if (task_name or "").strip():
                payload["task_name"] = task_name.strip()
            if (model or "").strip():
                payload["model"] = model.strip()
            with self.send_lock:
                send_msg(sock, payload)
        except Exception:
            return []
        return self.fetch_mail(cid, sock)

    def fetch_mail(self, conversation_id, sock=None, timeout=2.0):
        """顺路把队里等着捎给这个 tab 的话取回来（队友转告 / 黑板提醒 / 控制台回执）。

        这些消息此前只有「用户下次在这个 tab 回话」时才发得出去，agent 埋头干活
        期间递过去的话它一个字看不见（08-07 实测：10:31 发的转告，对方 10:35 还在
        改文件、根本没收到）。zt 本来就每完成一个动作调一次，让它顺路取信，递话
        延迟从几十分钟降到几十秒。

        取不到就当没有：超时短、任何异常都吞掉——zt 是锦上添花，绝不能卡住 agent。
        """
        cid = (conversation_id or "").strip()
        sock = sock or self.sock
        with self.waiters_lock:
            late = self._late_mail.pop(cid, []) if cid else []
        if not cid or sock is None:
            return late
        if sock is not self._mail_sock:   # 换连接了，重新给这版 hub 一次机会
            self._mail_sock, self._mail_misses, self._mail_dead = sock, 0, False
        if self._mail_dead:
            return late
        req_id = uuid.uuid4().hex
        ev = threading.Event()
        with self.waiters_lock:
            self.waiters[req_id] = {
                "event": ev, "resp": None, "sock": sock, "kind": "mail",
                "rpc_id": None, "conversation_id": cid, "cancelled": False,
            }
        try:
            with self.send_lock:
                send_msg(sock, {"type": "agent_mail_fetch", "id": req_id,
                                "conversation_id": cid})
            if not ev.wait(timeout=max(0.2, float(timeout))):
                # 应答要是随后到了，_stash_late_mail 会替下一趟收着
                self._mail_misses += 1
                self._mail_dead = self._mail_misses >= self.MAIL_MISS_LIMIT
                return late
            self._mail_misses = 0
            with self.waiters_lock:
                resp = (self.waiters.get(req_id) or {}).get("resp") or {}
            return late + [x for x in (resp.get("items") or [])
                           if isinstance(x, str) and x.strip()]
        except Exception:
            return late
        finally:
            with self.waiters_lock:
                self.waiters.pop(req_id, None)

    def fetch_summary(self, conversation_id, target="", max_chars=2000, sock=None,
                      timeout=4.0):
        """向 hub 要某个会话的压缩摘要（BajieAsk get_session_summary 的对等物）。

        接手 / 协作前想知道「那个 tab 干到哪了」，以前只能让用户去复制接手提示词
        （几千行）或者 read 别人的记录文件。这里由 hub 按内存里的实况（自报状态、
        zt 轨迹、最近几句对话、是否正等用户回话）压成 ≤ max_chars 的一段话。
        hub 不应答（老版本没这个分支）返回 None，调用方自己说人话。
        """
        cid = self._ensure_conversation_id(conversation_id)
        self._active_convs.add(cid)
        self._conv_activity[cid] = time.time()
        self._ensure_heartbeat()
        if sock is None:
            with self.conn_lock:
                sock = self._connect_locked()
        req_id = uuid.uuid4().hex
        ev = threading.Event()
        with self.waiters_lock:
            self.waiters[req_id] = {
                "event": ev, "resp": None, "sock": sock, "kind": "summary",
                "rpc_id": None, "conversation_id": cid, "cancelled": False,
            }
        try:
            with self.send_lock:
                send_msg(sock, {"type": "agent_summary_fetch", "id": req_id,
                                "conversation_id": cid, "target": str(target or ""),
                                "max_chars": int(max_chars or 2000)})
            if not ev.wait(timeout=max(0.5, float(timeout))):
                return None
            with self.waiters_lock:
                resp = (self.waiters.get(req_id) or {}).get("resp") or {}
            text = resp.get("text")
            return str(text) if text is not None else None
        except Exception:
            return None
        finally:
            with self.waiters_lock:
                self.waiters.pop(req_id, None)

    def relay_message(self, conversation_id, to, message):
        """agent → agent 转告：交给 hub 路由到目标 tab 的队列。
        hub 不在时 _connect_locked 会拉起它（转告不能静默丢，宁可多等一两秒）。"""
        cid = self._ensure_conversation_id(conversation_id)
        self._active_convs.add(cid)
        self._conv_activity[cid] = time.time()
        self._ensure_heartbeat()
        with self.conn_lock:
            sock = self._connect_locked()
        with self.send_lock:
            send_msg(sock, {
                "type": "agent_relay",
                "conversation_id": cid,
                "to": str(to or ""),
                "message": str(message or ""),
                "cwd": detect_project_dir(),
            })

    def board_post(self, conversation_id, kind, text):
        """agent → 团队黑板：交给 hub 落盘展示，全队主动来读；部署/事故/大改
        另由 hub 顺带提醒同一条业务线的在线队友。"""
        cid = self._ensure_conversation_id(conversation_id)
        self._active_convs.add(cid)
        self._conv_activity[cid] = time.time()
        self._ensure_heartbeat()
        with self.conn_lock:
            sock = self._connect_locked()
        with self.send_lock:
            send_msg(sock, {
                "type": "agent_board",
                "conversation_id": cid,
                "kind": str(kind or ""),
                "text": str(text or ""),
                "cwd": detect_project_dir(),
            })

    def notify_session(self, message, conversation_id=None, task_name=None, is_markdown=True):
        """非阻塞：在控制台创建/更新 tab 并写入系统消息（用于 ji 回忆等）"""
        with self.conn_lock:
            sock = self._connect_locked()
        payload = {
            "type": "session_notify",
            "conversation_id": self._ensure_conversation_id(conversation_id),
            "message": message,
            "is_markdown": is_markdown,
            "cwd": detect_project_dir(),
        }
        if task_name:
            payload["task_name"] = task_name
        with self.send_lock:
            send_msg(sock, payload)

    def ask(self, message, options, is_markdown, conversation_id=None, task_name=None,
            rpc_id=None, cwd=None, beat_cap=None, artifacts=None, model=None, card=None,
            wait=True, runtime_kind="cursor", native_thread_id=None,
            rpc_scope=None):
        """发送 zhi 请求并等待用户回复。

        wait=False（只发不等，对标 BajieAsk 的 reply_message / wait_message 分离）：
        提问照常落到控制台（绿灯、手机页都有），但本方法只等 deferred_peek_secs 抓
        hub 的即时答复（排队消息补送 / 脱离期缓存的回复），没有就发 zhi_detach 立刻
        返回 {"deferred": True}——agent 继续干活；之后 message 留空再调（走既有的
        resume 续期路径）阻塞收回复，期间用户的回复由 hub 存在 buffered_reply。

        保活：Cursor 对 tool call 有硬超时（2026-08 实测新版 IDE 为 120s，旧版约
        60min；CLI/ACP 约 60s），SSE 注释心跳挡不住协议层超时；notifications/progress
        能否重置超时钟由探测持久化（见 _progress_resets）。共享 MCP 进程对同一 server
        的并发工具调用另有排队上限。
        本方法采用自适应阻塞：首拍 keepalive_first_secs（默认 45s），之后逐次翻倍、
        keepalive_secs 封顶；到点仍无回复就 detach 并抛 ZhiKeepAlive，让 AI 立即用同一
        conversation_id 再次调用 zhi 续期——控制台里的提问和绿灯全程保持不变，你的回复
        会在下一次续期时立刻交付。keepalive_secs=0 关闭保活。
        beat_cap：本拍睡眠上限秒数（tool_zhi 按 120s 预算传入剩余额度），
        保证单拍绝不睡过客户端断头台。
        """
        runtime_kind = normalize_runtime(runtime_kind)
        bounded = runtime_kind != "cursor"
        native_wait = bounded and bool(wait)
        native_started = time.monotonic()
        native_deadline = (native_started + max(0.0, float(NATIVE_WAIT_MAX_SECS if wait else 12))
                           if bounded else None)
        conversation_id = self._ensure_conversation_id(conversation_id)
        self._cancel_idle_timer(conversation_id)
        self._active_convs.add(conversation_id)
        self._conv_activity[conversation_id] = time.time()
        self._ensure_heartbeat()
        cfg = {}
        try:
            cfg = json.loads((DATA_DIR / "config.json").read_text(encoding="utf-8"))
        except Exception:
            pass
        try:
            keepalive_cap = int(cfg.get("keepalive_secs", 0))
        except Exception:
            keepalive_cap = 0
        try:
            keepalive_first = int(cfg.get("keepalive_first_secs", 45))
        except Exception:
            keepalive_first = 45
        # 自适应保活：首拍 keepalive_first_secs（默认 45s）就续期，之后逐次翻倍、
        # 到 keepalive_secs 封顶。Cursor 的共享 MCP 进程对同一 server 的工具调用有
        # 排队/并发上限——一个 zhi 长期占住槽位，会把其他窗口 agent 的报到/zt 卡住
        # 几分钟（实测 4 窗口同时报到只进来 1 个，其余要靠点「重连MCP」才挤进来）。
        # 短首拍让槽位快速轮转给别的对话；指数退避让长时间空等的 token 成本可控。
        keepalive = 0
        if keepalive_cap > 0 and not bounded:
            # 等待起点从本轮提问开始计（stats 在真回复/取消时清除），
            # KEEPALIVE 里的「已等待 Ns」才是真实等待时长
            st0 = self._keepalive_stats.get(conversation_id)
            if st0 is None:
                st0 = {"ticks": 0, "started": time.time()}
                self._keepalive_stats[conversation_id] = st0
            ticks0 = st0["ticks"]
            keepalive = min(max(5, keepalive_first) * (2 ** min(ticks0, 12)), keepalive_cap)
            if time.time() < self._yield_until:
                keepalive = min(keepalive, 15)  # 让路窗口：短拍轮转，给排队的调用放行
        if beat_cap:
            # 120s 预算兜底：单拍睡眠不得超过本次 tools/call 的剩余预算
            # （即使 keepalive_secs=0 配置关闭了保活，也要守住客户端断头台）
            keepalive = min(keepalive or 10 ** 9, max(5, int(beat_cap)))
        resume = (conversation_id in self._keepalive_active
                  or conversation_id in self._deferred_convs)
        if (message or "").strip() and conversation_id in self._deferred_convs:
            # 上一条是「只发不等」、agent 还没来收就又带正文来了：这是新提问，不是续期
            # ——续期会让 hub 沿用旧提问、把这条正文吞掉。旧提问由 hub 按 superseded
            # 收掉；用户若已回了旧提问，hub 会把那条回复作为本条的答复补送（见
            # Hub._requeue_buffered_reply），一个字都不丢
            resume = False
            self._deferred_convs.discard(conversation_id)
            self._keepalive_active.discard(conversation_id)
            self._keepalive_stats.pop(conversation_id, None)
        # 真提问入缓存；续期空重呼用缓存补发——hub 若在等待期间重启过，其内存里的
        # pending 提问已丢，空 message 会渲染成空气泡，用原文重建后用户无感
        if (message or "").strip():
            self._zhi_payload_cache[conversation_id] = {
                "message": message, "options": options,
                "is_markdown": is_markdown, "task_name": task_name,
                "artifacts": artifacts, "model": model, "card": card}
        elif resume:
            _c = self._zhi_payload_cache.get(conversation_id)
            if _c:
                message = _c["message"]
                options = options or _c["options"]
                is_markdown = _c["is_markdown"]
                task_name = task_name or _c["task_name"]
                # 续期重呼按纪律不带成果，得从缓存补回来，否则 hub 重启后
                # 重建的气泡只剩文字、成果凭空消失
                artifacts = artifacts or _c.get("artifacts") or []
                model = model or _c.get("model")
                card = card or _c.get("card")
        last_err = None
        for _ in range(2):
            try:
                connect_kwargs = {"wait_secs": 8 if keepalive > 0 else 45}
                if native_deadline is not None:
                    remaining = native_deadline - time.monotonic()
                    if remaining <= 0:
                        raise ConnectionError("原生客户端等待时间已用尽")
                    connect_kwargs.update(wait_secs=remaining, deadline=native_deadline)
                if native_deadline is None:
                    with self.conn_lock:
                        sock = self._connect_locked(**connect_kwargs)
                else:
                    remaining = native_deadline - time.monotonic()
                    if not self.conn_lock.acquire(timeout=max(0, remaining)):
                        raise ConnectionError("原生客户端等待时间已用尽")
                    try:
                        sock = self._connect_locked(**connect_kwargs)
                    finally:
                        self.conn_lock.release()
            except ConnectionError:
                if keepalive > 0:
                    # 控制台不可达（挂起被杀/重启中）：不再向 agent 报错，转 KEEPALIVE
                    # 排队——agent 按续期节拍重呼即自动重试，hub 一回来提问立刻送达。
                    # 07-27 事故根治：报错会让 agent 走「等 20-30s 重试 6 次」的粗纪律，
                    # 排队则全程无感且不丢会话上下文
                    st = self._keepalive_stats.get(conversation_id)
                    if not st:
                        st = {"ticks": 0, "started": time.time()}
                        self._keepalive_stats[conversation_id] = st
                    st["ticks"] += 1
                    self._keepalive_active.add(conversation_id)
                    raise ZhiKeepAlive(conversation_id, st["ticks"],
                                       int(time.time() - st["started"]),
                                       hub_down=True)
                raise
            req_id = uuid.uuid4().hex
            payload = {
                "type": "zhi_request",
                "id": req_id,
                "message": message,
                "predefined_options": options,
                "is_markdown": is_markdown,
                "conversation_id": conversation_id,
                "resume": resume,
                # 每次请求都带上最新工作区：hello 时 roots 可能还没应答（回退成了家目录），
                # 且 Cursor 的共享 MCP 进程可能服务多个窗口——优先用 agent 显式传的路径
                "cwd": cwd or detect_project_dir(),
                "runtime_kind": runtime_kind,
                "native_thread_id": (str(native_thread_id).strip() or None
                                      if native_thread_id is not None else None),
            }
            if artifacts:
                payload["artifacts"] = artifacts
            if task_name:
                payload["task_name"] = task_name
            if model:
                payload["model"] = model
            if card:
                payload["card"] = card
            if not wait:
                # 告诉 hub 这条提问马上就要脱离：失联看门狗别把它当「本轮失联」清掉
                payload["deferred"] = True
            ev = threading.Event()
            with self.waiters_lock:
                pre_cancelled = rpc_id is not None and self._consume_pre_cancel(
                    str(rpc_id), conversation_id, rpc_scope=rpc_scope)
                self.waiters[req_id] = {
                    "event": ev, "resp": None, "sock": sock,
                    "rpc_id": rpc_id, "conversation_id": conversation_id,
                    "rpc_scope": rpc_scope, "cancelled": pre_cancelled,
                }
            try:
                if pre_cancelled:
                    self._keepalive_active.discard(conversation_id)
                    self._keepalive_stats.pop(conversation_id, None)
                    raise ZhiCancelled()  # 取消通知先到（如拉起控制台耗时期间），请求不再发出
                try:
                    with self.send_lock:
                        send_msg(sock, payload)
                except OSError:
                    self._drop(sock)
                    last_err = ConnectionError("控制台连接中断")
                    continue
                if wait:
                    if native_deadline is not None:
                        timeout = max(0, native_deadline - time.monotonic())
                    else:
                        timeout = keepalive if keepalive > 0 else None
                    got = ev.wait(timeout=timeout)
                else:
                    # 只发不等：只抓 hub 的即时答复（排队补送 / 脱离期缓存），别真等人
                    peek_timeout = self._deferred_peek_secs(cfg)
                    if native_deadline is not None:
                        peek_timeout = min(peek_timeout, max(0, native_deadline - time.monotonic()))
                    got = ev.wait(timeout=peek_timeout)
                w = self.waiters[req_id]
                if w.get("cancelled"):
                    # 兜底重发 zhi_cancel：取消可能发生在请求刚发出的窗口期，
                    # 此时 hub 侧第一次 zhi_cancel 可能早于 zhi_request 到达而未生效
                    try:
                        with self.send_lock:
                            send_msg(sock, {
                                "type": "zhi_cancel",
                                "id": req_id,
                                "conversation_id": conversation_id,
                            })
                    except Exception:
                        pass
                    self._keepalive_active.discard(conversation_id)
                    self._keepalive_stats.pop(conversation_id, None)
                    self._deferred_convs.discard(conversation_id)
                    raise ZhiCancelled()
                if not wait and w.get("resp") is None:
                    # 只发不等：提问已落到控制台，立刻脱离（提问与绿灯保留，用户回复
                    # 由 hub 缓存），当场返回让 agent 接着干活；下次 message 留空来收
                    # 就是既有的续期路径
                    self._send_detach(sock, req_id, conversation_id)
                    if w.get("resp") is None:
                        self._keepalive_active.add(conversation_id)
                        self._deferred_convs.add(conversation_id)
                        self._keepalive_stats.setdefault(
                            conversation_id, {"ticks": 0, "started": time.time()})
                        self._conv_activity[conversation_id] = time.time()
                        return {"deferred": True, "resumed": resume}
                    got = True  # 脱离电文发出的瞬间答复恰好到了：按正常回复交付
                if native_wait and (not got or w.get("force_ka")) and w.get("resp") is None:
                    # Codex/ChatGPT 明确 wait=true 只允许等待一拍；超时后把提问
                    # 留在控制台，返回可稍后领取的结果，不把 Cursor 的 KEEPALIVE
                    # 纪律泄漏到 native 客户端。
                    self._send_detach(sock, req_id, conversation_id)
                    if w.get("resp") is None:
                        self._deferred_convs.add(conversation_id)
                        self._keepalive_active.discard(conversation_id)
                        self._keepalive_stats.pop(conversation_id, None)
                        self._conv_activity[conversation_id] = time.time()
                        return {
                            "deferred": True,
                            "resumed": resume,
                            "wait_timeout": True,
                            "waited_secs": round(max(0, time.monotonic() - native_started), 1),
                        }
                forced_ka = bool(w.get("force_ka")) and w.get("resp") is None
                if not got or forced_ka:
                    # 保活到点仍无回复（或被让路机制强制续期）：告诉 hub 本请求暂时脱离
                    # （保留提问与绿灯，期间用户回复由 hub 缓存），抛 ZhiKeepAlive 让 AI 立即续期重呼
                    self._send_detach(sock, req_id, conversation_id)
                    if w.get("resp") is None:
                        self._keepalive_active.add(conversation_id)
                        st = self._keepalive_stats.get(conversation_id)
                        if not st:
                            st = {"ticks": 0, "started": time.time()}
                            self._keepalive_stats[conversation_id] = st
                        st["ticks"] += 1
                        raise ZhiKeepAlive(conversation_id, st["ticks"],
                                           int(time.time() - st["started"]))
                    # 回复恰在「等待到点 → 脱离电文发出」的窄窗里到达：hub 那边 pending
                    # 已清、detach 成了空操作，这条回复再抛 KEEPALIVE 就永远送不出去了
                    # （迟到的 zhi_response 没有等待方会被丢），照常交付
                resp = w["resp"]
                if resp is None:
                    last_err = ConnectionError("控制台连接中断")
                    continue
                self._keepalive_active.discard(conversation_id)
                self._keepalive_stats.pop(conversation_id, None)  # 真回复到达，续期计数清零
                self._deferred_convs.discard(conversation_id)
                self._conv_activity[conversation_id] = time.time()
                return resp
            finally:
                with self.waiters_lock:
                    self.waiters.pop(req_id, None)
        self._keepalive_active.discard(conversation_id)
        self._keepalive_stats.pop(conversation_id, None)
        self._deferred_convs.discard(conversation_id)
        raise last_err or ConnectionError("控制台连接中断")

    def cancel_request(self, rpc_id, conv=None, rpc_scope=None):
        """Cursor 发来 notifications/cancelled：作废对应 zhi 等待，并让控制台清掉绿点。

        不处理会导致：控制台 tab 永远停在「等待你输入」（绿点），
        且用户此后在控制台的回复被发进已取消的请求、静默丢失。

        conv：HTTP 层从 _active_sse_calls 捞到的 conversation_id 旁证——取消通知
        本身只带 requestId，而 rpc_id 是每个客户端连接各自从小整数递增的，一个
        39222 端点服务本机所有窗口时纯 rpc_id 就是撞号之源（08-12 -32800 事故）。
        带 conv 时，兜底记录与等待方匹配都按 (conv, rpc_id) 精确隔离：跨会话同
        rpc_id 从机制上互不可见（身份根治②，TTL 降级为同会话内的兜底防线）。
        HTTP 新客户端还会带 rpc_scope（session/connection）；它优先于 conv，
        因而两个客户端即使意外复用同一个 conversation_id 也不能互相取消。
        """
        if rpc_id is None:
            return
        key = str(rpc_id)
        conv = (str(conv or "").strip() or None)
        rpc_scope = (str(rpc_scope or "").strip() or None)
        target = None
        with self.waiters_lock:
            now = time.time()
            self._purge_expired_cancels(now)
            hits = [(rid, w) for rid, w in self.waiters.items()
                    if w.get("rpc_id") is not None and str(w["rpc_id"]) == key]
            if rpc_scope is not None:
                hits = [(rid, w) for rid, w in hits
                        if (w.get("rpc_scope") or "") == rpc_scope]
            if conv is not None:
                hits = [(rid, w) for rid, w in hits
                        if (w.get("conversation_id") or "") == conv]
            elif rpc_scope is None and len(hits) > 1:
                # 身份不明还同时命中多个同号等待方（只有共享端点跨窗口才会这样）：
                # 谁都不杀——错杀活人比让真被取消的那个多挂一会儿严重得多
                log(f"cancelled rpc_id={key} 命中 {len(hits)} 个等待方且无会话旁证，均不作废")
                return
            target = hits[0] if hits else None
            if not target:
                # 只有取消抢在 tools/call 之前到达（waiter 还没注册）才需要兜底
                # 记录；杀到了活等待方就不留残迹，免得毒到同号的后续请求
                if len(self.cancelled_rpc_ids) > 256:
                    self.cancelled_rpc_ids.clear()
                if rpc_scope is None:
                    self.cancelled_rpc_ids[(conv, key)] = now
                else:
                    self.cancelled_rpc_ids[(rpc_scope, conv, key)] = now
        if not target:
            return
        req_id, w = target
        w["cancelled"] = True
        try:
            with self.conn_lock:
                sock = self.sock
            if sock is not None:
                with self.send_lock:
                    send_msg(sock, {
                        "type": "zhi_cancel",
                        "id": req_id,
                        "conversation_id": w.get("conversation_id") or self._minted_conv_id,
                    })
        except Exception:
            pass
        w["event"].set()
        log(f"zhi 已被客户端取消（rpc_id={rpc_id}）")

    def _purge_expired_cancels(self, now):
        """删掉超过 PRE_CANCEL_TTL 的兜底取消记录（须在 waiters_lock 下调用）。

        记录按 (conversation_id|None, rpc_id) 分桶后跨会话撞号已从机制上隔离
        （身份根治②）；TTL 退居兜底——同一会话客户端重连后 rpc 计数器归零、
        15s 内复用小号这类残余竞态仍靠它斩断。
        """
        for k in [k for k, ts in self.cancelled_rpc_ids.items()
                  if now - ts > self.PRE_CANCEL_TTL]:
            self.cancelled_rpc_ids.pop(k, None)

    def _consume_pre_cancel(self, key, conv=None, rpc_scope=None):
        """rpc_id 是否落在有效竞态窗口内；命中即消费（须在 waiters_lock 下调用）。

        命中即 pop：对应 zhi 请求已到达并作废，这条兜底使命完成，不能反复误杀
        后续（哪怕同号被别的连接复用）。查询按本会话的桶 (conv, key) 优先，
        (None, key) 是没有会话旁证的取消（stdio 路径 / SSE 登记前的极端竞态）
        留下的，只对同号请求兜底命中——别的会话留下的带 conv 记录永远查不到。
        有 rpc_scope 时只查该 HTTP session/connection 的桶，不向旧的无 scope 桶
        猜测回退。
        """
        now = time.time()
        self._purge_expired_cancels(now)
        conv = (str(conv or "").strip() or None)
        rpc_scope = (str(rpc_scope or "").strip() or None)
        if rpc_scope is not None:
            buckets = ((rpc_scope, conv, key), (rpc_scope, None, key))
        else:
            buckets = ((conv, key), (None, key))
        for k in buckets:
            if k in self.cancelled_rpc_ids:
                self.cancelled_rpc_ids.pop(k, None)
                return True
        return False


BRIDGE = HubBridge()


# ---------------- 省额度冻结 ----------------
def _read_cfg():
    try:
        return json.loads((DATA_DIR / "config.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _is_frozen():
    """省额度冻结开关：升级 Pro+ 期间用，冻结时把 agent 卡在工具调用里，
    使其不生成任何新 token（LLM 不推进），直到用户在控制台手动恢复。"""
    return bool(_read_cfg().get("token_freeze"))


def wait_if_frozen():
    """若处于冻结态，阻塞在此直到解冻。agent 停在工具调用里 = 零 token 消耗。
    返回 True 表示曾被冻结（供调用方提示），False 表示没冻结。
    通过 hub 上报冻结态，让控制台亮出「已冻结」横幅。"""
    if not _is_frozen():
        return False
    # 通知 hub：本进程有 agent 被冻结（非阻塞，失败无所谓）
    try:
        with BRIDGE.conn_lock:
            sock = BRIDGE.sock
        if sock is not None:
            with BRIDGE.send_lock:
                send_msg(sock, {"type": "freeze_state", "frozen": True, "pid": os.getpid()})
    except Exception:
        pass
    # 阻塞轮询 config：每 2s 看一次是否解冻。期间 agent 完全不动 = 不烧 token。
    while _is_frozen():
        time.sleep(2)
    try:
        with BRIDGE.conn_lock:
            sock = BRIDGE.sock
        if sock is not None:
            with BRIDGE.send_lock:
                send_msg(sock, {"type": "freeze_state", "frozen": False, "pid": os.getpid()})
    except Exception:
        pass
    return True


# ---------------- 工具实现 ----------------
def format_size(n):
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _incoming_batch_dir(cwd=None):
    """传入文件/图片的落盘目录。

    优先放当前项目下 .chijiu-tmp/（Cursor 读起来最顺手、路径短），
    目录内自带 .gitignore 全量忽略；项目目录未知时退回系统 TEMP。
    共享 MCP 进程服务多窗口时 detect 不可靠，调用方应传本次请求的 cwd。
    """
    proj = cwd or detect_project_dir()
    home = str(Path.home())
    if proj and os.path.isdir(proj) and str(Path(proj)) != home:
        root = Path(proj) / ".chijiu-tmp"
    else:
        root = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".") / "rxyy-mcp-incoming"
    batch = root / time.strftime("%Y%m%d-%H%M%S")
    batch.mkdir(parents=True, exist_ok=True)
    try:
        gi = root / ".gitignore"
        if not gi.exists():
            gi.write_text("*\n", encoding="utf-8")
    except OSError:
        pass
    _cleanup_old_batches(root)
    return batch


def _cleanup_old_batches(root, keep_days=3):
    """清理 3 天前的传入文件批次目录，避免项目里越积越多"""
    import shutil
    cutoff = time.time() - keep_days * 86400
    try:
        for d in root.iterdir():
            if d.is_dir() and re.match(r"^\d{8}-\d{6}$", d.name):
                try:
                    if d.stat().st_mtime < cutoff:
                        shutil.rmtree(d, ignore_errors=True)
                except OSError:
                    continue
    except OSError:
        pass


def _unique_path(batch, name):
    target = batch / name
    i = 2
    while target.exists():
        stem, suffix = os.path.splitext(name)
        target = batch / f"{stem} ({i}){suffix}"
        i += 1
    return target


def save_incoming_files(files, cwd=None):
    """把控制台/分享页发来的文件落盘到本机临时目录，返回 (路径, 大小) 列表"""
    import base64
    saved = []
    if not files:
        return saved
    batch = _incoming_batch_dir(cwd)
    for item in files:
        name = os.path.basename(str(item.get("name") or "文件.bin")) or "文件.bin"
        target = _unique_path(batch, name)
        try:
            raw = base64.b64decode(item.get("data") or "")
            target.write_bytes(raw)
            # 本机 IPGuard/TSD 会把 Python 写的受控扩展名文件透明加密，
            # 落盘后原地解密一次，保证 AI 用 Read 之外的工具也读得到明文
            if tsd_decrypt is not None:
                try:
                    tsd_decrypt.decrypt_file(str(target))
                except Exception:
                    pass
            saved.append((str(target), len(raw)))
        except Exception as e:
            log(f"保存传入文件 {name} 失败: {e}")
    return saved


_MEDIA_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
              "image/webp": ".webp"}


def save_incoming_image(img, batch):
    """大图落盘，返回 (路径, 字节数) 或 None"""
    import base64
    try:
        raw = base64.b64decode(img.get("data") or "")
        name = os.path.basename(str(img.get("filename") or ""))
        if not name:
            name = "图片" + _MEDIA_EXT.get(img.get("media_type", ""), ".png")
        target = _unique_path(batch, name)
        target.write_bytes(raw)
        return str(target), len(raw)
    except Exception as e:
        log(f"保存传入图片失败: {e}")
        return None


_TAKEOVER_SWITCH_RE = re.compile(
    r"conversation_id\s*必须改用\s*[「『\"']([0-9a-fA-F]{6,32})[」』\"']")


def _takeover_switch_target(text):
    """接手派单正文里点名「必须改用」的原会话 ID；不是派单则返回 ""。

    回执结尾那句「本对话 conversation_id: X，后续必须沿用」原样回填调用方传来的
    ID，而接手派单的正文说的是「改用另一个 ID」——两句当场打架，agent 多半听离
    自己最近的结尾那句，于是继续拿报到壳的 ID 干活；壳被收起后它就撞上「此对话
    已关闭」并停手（08-25 实测）。派单这一次的回执必须跟正文口径一致。

    只认「必须改用」这个措辞：报到提示词里也有 conversation_id=「xxx」，那句说的
    是「全程沿用」，认宽了会把正常报到也改写成切 ID。
    """
    m = _TAKEOVER_SWITCH_RE.search(str(text or ""))
    return m.group(1) if m else ""


def _parse_wait(value):
    """zhi 的 wait 参数：缺省 True。模型把布尔写成字符串（"false" / "0" / "否"）的
    也认，别因为一个引号就把「只发不等」当成阻塞等——那会让 agent 卡在自己的进展
    汇报上。"""
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() not in ("false", "0", "no", "n", "off", "否", "不等")


NATIVE_WAIT_MAX_SECS = 45


def _call_runtime(args, client_context=None):
    """本次工具调用的运行时：显式参数优先，其次是本连接 initialize。"""
    args = args if isinstance(args, dict) else {}
    raw = args.get("runtime")
    if raw is None or not str(raw).strip():
        raw = args.get("runtime_kind")
    if raw is None or not str(raw).strip():
        raw = getattr(client_context, "runtime_kind", None)
        if client_context is None or (raw == "unknown"
                and not getattr(client_context, "initialized", False)
                and not getattr(client_context, "profile", "")):
            raw = "cursor"  # legacy direct calls / sessionless HTTP connections
    return normalize_runtime(raw)


def _call_thread_id(args):
    args = args if isinstance(args, dict) else {}
    raw = args.get("thread_id")
    if raw is None:
        raw = args.get("native_thread_id")
    text = str(raw or "").strip()
    return text or None


def _native_conversation_id(conversation_id, runtime_kind, thread_id):
    if conversation_id or runtime_kind == "cursor":
        return conversation_id
    if thread_id:
        return uuid.uuid5(uuid.NAMESPACE_URL, runtime_kind + ":" + thread_id).hex
    # The MCP transport may be shared by many tasks. No process-wide fallback
    # is safe when the caller omits both IDs; report a fresh ID for later reuse.
    return uuid.uuid4().hex[:8]


def _call_project_path(args, client_context=None):
    args = args if isinstance(args, dict) else {}
    project_path = str(args.get("project_path") or "").strip()
    if project_path and os.path.isdir(project_path):
        return str(Path(project_path))
    context_root = str(getattr(client_context, "root_path", "") or "").strip()
    if context_root and os.path.isdir(context_root):
        return str(Path(context_root))
    return None


def _call_scope(client_context=None):
    scope = str(getattr(client_context, "scope", "") or "").strip()
    return scope or None


def tool_zhi(args, rpc_id=None, sse_loop=False, client_context=None):
    message = args.get("message") or ""
    options = args.get("predefined_options") or []
    # 「成果」是给人看的中文键，但模型偶尔会自作主张翻成英文，两种都收
    artifacts = args.get("成果")
    if not isinstance(artifacts, list) or not artifacts:
        english_artifacts = args.get("artifacts")
        if isinstance(english_artifacts, list):
            artifacts = english_artifacts
    if not isinstance(artifacts, list):
        artifacts = []
    is_markdown = bool(args.get("is_markdown", True))
    conversation_id = (args.get("conversation_id") or "").strip() or None
    task_name = (args.get("task_name") or "").strip() or None
    model = (args.get("model") or "").strip() or None
    runtime_kind = _call_runtime(args, client_context)
    native_thread_id = _call_thread_id(args)
    conversation_id = _native_conversation_id(conversation_id, runtime_kind, native_thread_id)
    # 决策卡片：多问 / 带说明 / 自由输入 / 倒计时代决。传得随意也收（字符串 JSON、
    # 旧格式 {title,options}），不成卡片的就当没传，predefined_options 老路照旧
    card = None
    if decision_card is not None and args.get("card") is not None:
        try:
            card = decision_card.normalize_card(args.get("card"))
        except Exception as exc:  # noqa: BLE001
            log(f"card 解析失败（已忽略）: {exc!r}")
            card = None
    # agent 显式给出的工作区 = 最可靠来源（Cursor 有时不应答 roots，detect 会错报成家目录）。
    # 只作为本次请求的 cwd 使用，绝不写回全局：Cursor 的 Shared MCP process 会让
    # 多个窗口共用本进程，全局覆盖会把别的工作区的会话归属也带偏。
    req_cwd = _call_project_path(args, client_context)
    # wait=false：只发不等（reply/wait 分离）。Codex/ChatGPT 默认只发不等，
    # 已识别的 Cursor 沿用阻塞默认值；其他客户端使用短调用。
    default_wait = runtime_kind == "cursor"
    wait = _parse_wait(args["wait"] if "wait" in args else default_wait)

    if DISABLED:
        return [{"type": "text", "text": (
            "[禁用模式 RXYY_MCP_DISABLE=1] 无需通过 zhi 征求反馈，请直接完成任务并正常结束。"
        )}]

    if not wait and not message.strip() and runtime_kind == "cursor":
        # 「只发不等」却没正文：既没东西可发，也不是来收回复的（收回复是 message 留空
        # 且不带 wait=false）。有待收的提问就替它收一次（短窗探一眼，等于 peek）；
        # 什么都没有就直说，别让 hub 渲染一个空气泡
        _cid = BRIDGE.own_conv_id_hint(conversation_id)
        if not (_cid and (
                _cid in BRIDGE._keepalive_active
                or _cid in getattr(BRIDGE, "_deferred_convs", set()))):
            return [{"type": "text", "text": (
                "wait=false 是「只发不等」，需要 message 正文；要收回复请 message 留空且不带 "
                "wait=false（阻塞等用户回话）。当前没有挂在控制台待收的提问。"
                + (f"（本对话 conversation_id: {_cid}）" if _cid else ""))}]

    # 选项闸：伞形/雷同/超量的选项集在送到 rxyy 眼前之前退回给 agent（详见 ask_quality）。
    # 空 message 是保活续期或重呼，选项没变过，别重复拦。
    if message.strip() and options:
        _bounce = ask_quality.bounce_text(options, conversation_id or "")
        if _bounce:
            log(f"选项闸拦下一次提问（conv={conversation_id or '-'}，{len(options)} 条选项）")
            return [{"type": "text", "text": _bounce}]

    # 省额度冻结：升级 Pro+ 期间阻塞在此，agent 不推进 = 零 token，直到手动恢复
    if runtime_kind == "cursor":
        wait_if_frozen()
    elif _is_frozen():
        return [{"type": "text", "text": "rxyy MCP 当前已冻结，本次操作未执行；解除冻结后可按需重试。"}]

    # SSE 传输（sse_loop=True）：HTTP 响应头已即时送出，Cursor(Undici) 的 300s 响应头
    # 硬超时永不触发。但新版 Cursor（2026-08）另有 120s 协议层硬超时，对策分两层
    # （详见 _sse_call_budget 注释）：
    # ① progress 证实有效 → 预算 0（不限），keepalive 续期全在服务端内部消化
    #    （不把 KEEPALIVE 抛回 agent），agent 全程零往返、零 token、待命期免费；
    # ② 未证实/无效 → 每次 tools/call 最多阻塞 ~95s 就把 KEEPALIVE 抛回 agent
    #    续期，赶在 120s 断头台前体面返回（-32001 错误会触发粗重试纪律，更贵）。
    # stdio 传输（sse_loop=False）没有这条常开连接，仍按老规矩把 KEEPALIVE 返回给 agent。
    _t0 = time.time()
    # 95 秒/progress 探测是 Cursor 的兼容防线；native 客户端走自己的 45 秒
    # 明确等待上限，不得把 Cursor 的长挂拍子带过去。
    _budget = (_sse_call_budget()
               if sse_loop and runtime_kind == "cursor" else 0)
    _msg = message
    while True:
        _cap = None
        if sse_loop and _budget > 0:
            _cap = _budget - (time.time() - _t0)
        try:
            resp = BRIDGE.ask(_msg, options, is_markdown, conversation_id, task_name,
                              rpc_id=rpc_id, cwd=req_cwd, beat_cap=_cap,
                              artifacts=artifacts, model=model, card=card, wait=wait,
                              runtime_kind=runtime_kind,
                              native_thread_id=native_thread_id,
                              rpc_scope=_call_scope(client_context))
            break
        except ZhiKeepAlive as ka:
            if runtime_kind != "cursor":
                return [{"type": "text", "text": "本次等待已让出，可稍后用同一 conversation_id 领取回复。"}]
            # 让路窗口内必须真的返回一次：SSE 下在服务端内部续期虽然省 token，但
            # 这条 HTTP 请求始终挂着，Cursor 那边的并发槽位一直被占——「同时开六个
            # 新对话只进来一个」正是因此。窗口期（新报到/点「+」时开）把 KEEPALIVE
            # 抛回 agent，让它重呼一次腾出槽位；平时仍走内部续期，零往返零 token。
            _yield_win = getattr(BRIDGE, "_yield_until", 0)
            _conv_key = conversation_id or BRIDGE._minted_conv_id or ""
            # 一个让路窗口只需真返回一次就够腾出槽位；同一窗口内继续内部续期，
            # 否则窗口期一长（新报到会不断续窗）就退化成老的「每拍烧一次 token」
            _need_yield = (_yield_win > BRIDGE._yield_returned.get(_conv_key, 0)
                           and time.time() < _yield_win)
            if _need_yield:
                BRIDGE._yield_returned[_conv_key] = _yield_win
            _budget_spent = (_budget > 0
                             and time.time() - _t0 >= _budget - 5)
            if sse_loop and not _need_yield and not _budget_spent:
                _msg = ""  # 续期重呼：空 message 走 resume+缓存重建（与 agent 续期同路径）
                if getattr(ka, "hub_down", False):
                    time.sleep(2)  # hub 重启中：别忙等空转，缓一拍再重连
                continue
            # 保活续期（poll_tick 风格）：每次带不同的续期计数与已等待时长，杜绝 AI 因
            # 「内容重复」偷懒不再续期。这不是用户回复，指示 AI 立即再次调用 zhi 继续等待。
            nonce = uuid.uuid4().hex[:6]
            # 文案刻意压到最短：每次续期的返回都会永久留在 agent 上下文里，
            # 四个待命 agent 挂一晚上，长文案的输入/输出 token 都是白烧钱
            tag = "·hub重启中" if getattr(ka, "hub_down", False) else ""
            return [{"type": "text", "text": (
                f"⏳【KEEPALIVE #{ka.ticks} · 已等待 {ka.elapsed}s{tag} · nonce:{nonce}】\n"
                f"非用户回复，勿总结勿结束。立即原样再调 zhi 续期：conversation_id=\"{ka.conversation_id}\"，"
                "message 留空，选项不变。"
            )}]
    if resp.get("deferred"):
        # 只发不等：提问已挂在控制台，agent 接着干活。文案压短——它会永久留在上下文里
        cid = BRIDGE.own_conv_id_hint(conversation_id) or conversation_id or ""
        peeked = bool(resp.get("resumed"))
        if resp.get("wait_timeout"):
            head = ("⏳ 已等待最多{}秒，暂未回复；提问仍挂在控制台，"
                    "可稍后再次调 zhi 领取。".format(
                        resp.get("waited_secs", NATIVE_WAIT_MAX_SECS)))
        else:
            head = ("⏳ 尚无回复，提问仍挂在控制台。" if peeked
                    else "📨 已发到控制台，未等回复（wait=false）。")
        return [{"type": "text", "text": (
            head + f"继续干活；要收回复再调 zhi：conversation_id=\"{cid}\"、message 留空"
            + ("（可用 wait=true 短等一次）。" if runtime_kind != "cursor" else "（阻塞等到用户回话）。")
            + "中途再发带正文的 zhi 会顶掉这条提问，用户若已回复会"
            "随新提问的答复一起补送给你，不会丢。")}]
    BRIDGE._schedule_idle_clear(conversation_id or BRIDGE._minted_conv_id)

    content = []
    text_parts = []
    selected = resp.get("selected_options") or []
    if selected:
        text_parts.append("选择的选项: " + ", ".join(selected))
    user_input = resp.get("user_input")
    if user_input and user_input.strip():
        text_parts.append(user_input.strip())

    saved_files = save_incoming_files(resp.get("files"), cwd=req_cwd)
    if saved_files:
        lines = "\n".join(f"- {p}（{format_size(n)}）" for p, n in saved_files)
        text_parts.append(
            f"📎 用户发来 {len(saved_files)} 个文件，已保存到本机，请直接读取这些路径处理：\n{lines}"
        )

    images = resp.get("images") or []
    inline_count = 0
    disk_imgs = []
    img_batch = None
    for img in images:
        data = img.get("data", "")
        media = img.get("media_type", "image/png")
        if len(data) <= INLINE_IMAGE_B64_MAX:
            content.append({"type": "image", "data": data, "mimeType": media})
            inline_count += 1
        else:
            # 大图不塞上下文：落盘给路径，AI 用 Read 工具查看
            if img_batch is None:
                img_batch = _incoming_batch_dir(req_cwd)
            saved = save_incoming_image(img, img_batch)
            if saved:
                disk_imgs.append(saved)
            else:
                content.append({"type": "image", "data": data, "mimeType": media})
                inline_count += 1
    if inline_count:
        text_parts.append(f"🖼 用户提供了 {inline_count} 张图片（已随本消息内联）。")
    if disk_imgs:
        lines = "\n".join(f"- {p}（{format_size(n)}）" for p, n in disk_imgs)
        text_parts.append(
            f"🖼 用户发来 {len(disk_imgs)} 张大图，为节省上下文已保存到本机，"
            f"请用 Read 工具查看这些路径：\n{lines}"
        )
    cid = BRIDGE.own_conv_id_hint(conversation_id)
    switch_to = _takeover_switch_target(user_input)
    if switch_to and switch_to != cid:
        text_parts.append(
            f"（接手派单：从这一步起 zhi/zt/ji 一律用 conversation_id: {switch_to}。"
            f"你报到用的 {cid or '旧 ID'} 已并入它，控制台会自动路由，"
            f"哪怕漏改也不会掉线，但请以 {switch_to} 为准）")
    elif cid:
        text_parts.append(f"（本对话 conversation_id: {cid}，后续 zhi/ji 调用必须沿用）")
    if not (conversation_id or "").strip():
        # 漏传 ID 已按新对话记账（见 _ensure_conversation_id）：明说，别让 agent
        # 以为消息进了原来的 tab，也别让它下次继续裸调
        text_parts.append("⚠ 你本次调用没带 conversation_id，已按独立新对话记账。"
                          "如果你本有自己的 ID，这条消息没有进你原来的 tab——"
                          "下次调用务必带上你自己的 conversation_id。")
    if text_parts:
        content.append({"type": "text", "text": "\n\n".join(text_parts)})
    if not content:
        content.append({"type": "text", "text": "用户未提供任何内容"})
    return content


VALID_STATUSES = {
    "analyzing", "developing", "testing", "deploying", "reviewing", "searching",
    "ready", "waiting", "dev_complete", "task_complete", "blocked",
}


def _mail_suffix(mail):
    """把 zt 顺路取回来的队友转告/黑板提醒拼在状态回执后面。

    放在回执里而不是另开一个工具：agent 不会为了「看看有没有人找我」专门发起调用，
    只有搭上它本来就要调的 zt，递话才真的会被读到。"""
    mail = [m for m in (mail or []) if m]
    if not mail:
        return ""
    return ("\n\n📬 队里有 {} 条话捎给你（转告/黑板/控制台回执，"
            "以前要等你下次调 zhi 才收得到）：\n\n".format(len(mail))
            + "\n\n".join(mail))


def tool_zt(args, client_context=None):
    """状态上报（轻量、非阻塞、不打断 agent）。agent 干活时每完成一步调一次，
    控制台实时显示它在干什么（analyzing/developing/testing + 一句话）。
    这是让「干活中状态变准」的关键——真实状态由 agent 主动报，不靠外部猜。
    顺带把队友转告/黑板提醒捎回来（见 Bridge.fetch_mail）。"""
    conversation_id = (args.get("conversation_id") or "").strip() or None
    status = (args.get("status") or "").strip()
    activity = (args.get("activity") or "").strip()
    task_name = (args.get("task_name") or "").strip() or None
    model = (args.get("model") or "").strip() or None
    runtime_kind = _call_runtime(args, client_context)
    native_thread_id = _call_thread_id(args)
    conversation_id = _native_conversation_id(conversation_id, runtime_kind, native_thread_id)
    # zt 的 project_path 是调用方对当前工作区的明确声明；只有真实目录才
    # 覆盖进程 cwd，空/无效值继续走原有探测，避免把用户输入的任意字符串送进 hub。
    req_cwd = _call_project_path(args, client_context)
    if DISABLED:
        return [{"type": "text", "text": "[禁用模式] 状态上报已忽略。"}]
    if runtime_kind == "cursor":
        wait_if_frozen()
    elif _is_frozen():
        return [{"type": "text", "text": "rxyy MCP 当前已冻结，本次操作未执行；解除冻结后可按需重试。"}]
    mail = BRIDGE.report_status(conversation_id, status, activity, task_name,
                                model=model, cwd=req_cwd,
                                runtime_kind=runtime_kind,
                                native_thread_id=native_thread_id,
                                rpc_scope=_call_scope(client_context))
    cid = BRIDGE.own_conv_id_hint(conversation_id)
    warn = ("" if conversation_id else
            "\n⚠ 本次没带 conversation_id，已按独立新对话记账（没进你原来的 tab）；"
            "下次务必带上你自己的 ID。")
    return [{"type": "text", "text": (
        f"✓ 状态已更新: {status or '(空)'}"
        + (f" · {activity}" if activity else "")
        + (f"（conversation_id: {cid}）" if cid else "")
        + "。这是非阻塞上报，请继续手头工作，无需等待。"
        + warn
        + _mail_suffix(mail)
    )}]


def _norm_root(path):
    """与 hub.norm_root 同口径：board.json 的分组键就是这么归一化出来的。"""
    try:
        return os.path.normcase(os.path.normpath(str(path or "").strip()))
    except Exception:
        return str(path or "")


_TASK_PROJECT_SPLIT = re.compile(r"\s*[·:：/|]\s*|\s+-\s+")


def _task_project_key(task_name=""):
    parts = [x for x in _TASK_PROJECT_SPLIT.split(str(task_name or "").strip()) if x]
    return parts[0].strip()[:24].casefold() if len(parts) >= 2 else "__workspace__"


def _registered_board_scope(cfg, conv_key):
    """按 Hub 同口径读取席位/intake 的显式 root + project。"""
    for root, projects in (cfg.get("team_projects") or {}).items():
        for project, bucket in (projects or {}).items():
            if any(isinstance(seat, dict) and seat.get("id") == conv_key
                   for seat in (bucket or {}).get("seats") or []):
                return _norm_root(root), str(project or "__workspace__").casefold()
    for root, seats in (cfg.get("team_seats") or {}).items():
        if any(isinstance(seat, dict) and seat.get("id") == conv_key
               for seat in seats or []):
            return _norm_root(root), "__workspace__"
    member = (cfg.get("team_project_members") or {}).get(conv_key)
    if isinstance(member, dict) and member.get("root"):
        return (_norm_root(member.get("root")),
                str(member.get("project") or "__workspace__").strip()[:24].casefold())
    if isinstance(member, str) and member.strip():
        return "", member.strip()[:24].casefold()
    return "", ""


def _board_scope(project_path="", task_name="", conversation_id=""):
    """优先用 Hub 快照里的真实 task_root/project，支持跨工作区接手。"""
    root = _norm_root(project_path)
    project = _task_project_key(task_name)
    cid = str(conversation_id or "").strip()
    if not cid:
        return root, project
    try:
        cfg = json.loads((DATA_DIR / "config.json").read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    try:
        sessions = json.loads((DATA_DIR / ".sessions.json").read_text(encoding="utf-8"))
    except Exception:
        sessions = []
    row = next((x for x in sessions if isinstance(x, dict)
                and cid in (str(x.get("conv_key") or ""), str(x.get("id") or ""))), None)
    conv_key = str((row or {}).get("conv_key") or cid)
    registered_root, registered_project = _registered_board_scope(cfg, conv_key)
    if registered_root:
        root = registered_root
    elif row:
        root = _norm_root(row.get("task_root") or row.get("cwd") or project_path)
    # team_tracks 是项目内的二级业务线；拿它当项目会把成员和黑板移出原项目。
    reported = registered_project or str((row or {}).get("agent_project") or "").strip()[:24]
    if reported:
        project = reported.casefold()
    return root, project


def _board_read_text(project_path="", task_name="", conversation_id=""):
    """直读黑板落盘文件（与 hub 同机同目录），主动读不用等 hub 回包。

    只回本项目那一组：读取端原先把所有项目揉在一起取最近 15 条——一个工作区忙
    起来就能把别人的黑板挤没，读到的还全是不相干项目的事（08-04 用户报「黑板在
    乱转发」）。命名项目按项目取流（08-31 用户拍板：一个项目常横跨几个文件夹/
    工作区，都按绝对路径干活）：新条目在 _projects[project]，历史条目仍嵌在各根
    的 _scopes[root][project] 下，一并归进来。未分项目只看自己任务根的桶，不再
    兜底吞下别的工作区的历史。"""
    try:
        data = json.loads((DATA_DIR / "board.json").read_text(encoding="utf-8"))
    except Exception:
        data = {}

    def _rows(groups):
        out = []
        for root, entries in groups:
            pname = Path(root).name or str(root)
            for e in entries or []:
                if isinstance(e, dict):
                    out.append((float(e.get("ts") or 0), pname, e))
        out.sort(key=lambda x: -x[0])
        return out

    def _rows_pooled(entries, label):
        """项目流条目：来源仓写在条目自身的 root 里（旧条目没有就标项目名）。"""
        out, seen = [], set()
        for e in entries or []:
            if not isinstance(e, dict):
                continue
            mark = (e.get("ts"), e.get("text"), e.get("from8"))
            if mark in seen:
                continue
            seen.add(mark)
            pname = Path(str(e.get("root") or "")).name or label
            out.append((float(e.get("ts") or 0), pname, e))
        out.sort(key=lambda x: -x[0])
        return out

    # 旧格式是 {root: [entries]}；下划线开头是保留桶（_scopes/_projects）。
    # 旧 root 级历史只作为未分项目的兼容桶，不会混进命名项目。
    legacy = [(root, entries) for root, entries in (data or {}).items()
              if not str(root).startswith("_") and isinstance(entries, list)]
    mine, project = _board_scope(project_path, task_name, conversation_id)
    # normpath("") 会归一成 "."：那不是工作区，是「什么都没报」。不澄清的话
    # 下面「没传工作区」的兼容兜底永远走不到，空报与查无分组混成一个分支。
    if mine in ("", "."):
        mine = ""
    scope = "本项目"
    if project and project != "__workspace__":
        pooled = list(((data or {}).get("_projects") or {}).get(project) or [])
        for by_root in (((data or {}).get("_scopes")) or {}).values():
            pooled.extend((by_root or {}).get(project) or [])
        rows = _rows_pooled(pooled, project)
    else:
        scoped = (((data or {}).get("_scopes") or {}).get(mine) or {}).get(project) or []
        rows = _rows([(mine, scoped)]) if mine and scoped else []
        if not rows and mine:
            # 未分项目只看本任务根的桶。08-31 前这里还会在桶空时兜底吞下全部
            # 工作区的历史——「写黑板给了全工作区的报告」的读侧来源，已收掉。
            rows = _rows([g for g in legacy if _norm_root(g[0]) == mine])
        if not rows and not mine:
            # 没传工作区时无法安全判定项目；保留历史全量兜底，但绝不把命名项目混进来。
            rows, scope = _rows(legacy), "工作区历史"
    if not rows:
        return ("（团队黑板还是空的。重大情况写法：ji(action=\"黑板\", "
                "category=部署/大改/提交/卡住/事故/收工, content=一句话)）")
    out = ["📋 团队黑板 · {}（新→旧，最多 15 条）：".format(scope)]
    for _ts, pname, e in rows[:15]:
        out.append("- [{} {}] {}〔{}〕{}：{}".format(
            e.get("day") or "", e.get("hms") or "", pname, e.get("kind") or "?",
            e.get("from_label") or e.get("from8") or "?", e.get("text") or ""))
    return "\n".join(out)


def _complete_taskstage_for_agent(conversation_id, needle="", task_name=""):
    """按派发文案里的编号把处理中的任务标成已处理。不绑会话。"""
    del conversation_id, task_name  # 一个窗口会接手、会连做多条，不能靠会话对号
    needle = str(needle or "").strip()
    if not needle:
        return "content 填派发文案里的任务编号。不要靠会话猜。"
    try:
        import share_server as ss
        storage = ss._task_storage()
    except Exception:
        storage = None
    if storage is None:
        return "任务库不可用，没法把任务安排站标成已处理。"
    reply = storage.complete_dispatched(needle)
    if not reply.get("ok"):
        extra = ""
        items = reply.get("tasks") or []
        if items:
            extra = "：" + "；".join(
                "{} {}".format(t.get("id") or "", t.get("title") or "") for t in items)
        return (reply.get("error") or "没法标已处理") + extra
    titles = "、".join(t.get("title") or t.get("id") for t in (reply.get("tasks") or []))
    if reply.get("already"):
        return "这条已经是已处理：{}".format(titles or needle)
    return "✓ 任务安排站已标成已处理：{}".format(titles or needle)


def _ensure_console_on_path():
    """让 MCP 进程能 import ``console/api``。

    常驻区住在 ``%LOCALAPPDATA%\\rxyy-tools-community\\live\\rxyy MCP``，``APP_DIR.parent``
    并不是 rxyy tools 仓库根，底下也没有 ``console\\``。任务库已经靠
    ``share_server._console_root()``（物化时写下的 console-root.txt）找对地方，
    import 必须走同一处，否则 ``发给`` 在现网会报发信模块不可用。
    """
    roots = []
    try:
        import share_server as ss
        recorded = ss._console_root()
        if recorded is not None:
            roots.append(Path(recorded))
    except Exception:
        pass
    parent = Path(APP_DIR).parent
    if parent not in roots:
        roots.append(parent)
    for root in roots:
        console = Path(root) / "console"
        if not console.is_dir():
            continue
        # 后插的在最前：必须让 console/ 压过仓库根，api.taskstage 才找得到
        for path in (str(root), str(console)):
            if path in sys.path:
                sys.path.remove(path)
            sys.path.insert(0, path)
        return


def _task_contact_book():
    try:
        import share_server as ss
        storage = ss._task_storage()
    except Exception:
        storage = None
    if storage is None:
        return None
    _ensure_console_on_path()
    from api.taskstage.contacts import ContactBook
    return ContactBook(storage.data_dir)


def _feige_send(name, text):
    _ensure_console_on_path()
    try:
        from api.feige_api import FeigeApi
        return FeigeApi().fg_send(name, text=text)
    except Exception as error:
        return {"ok": False, "error": str(error)}


# 发件底账的回执判定：以这些开头的是「答复/销账」，落「已处理」留档即可；
# 其余一律按「派出去的活」落「处理中」等确认。只认行首的明确标记——宁可把
# 回执误留成待确认（用户多点一次销账），也绝不把真派单误判成回执（那等于
# 它从待确认名单里消失，正是用户要治的「发出去就断线」）。
_RECEIPT_HEADS = ("✓", "√", "【回执】", "回执：", "回执:", "回复：", "回复:",
                  "已完成", "已修复", "已处理", "已解决", "已办结")


def _outbound_kind(text):
    return "receipt" if str(text or "").lstrip().startswith(_RECEIPT_HEADS) else "task"


def _record_outbound_delivery(who, text, via):
    """发件底账：agent 经「发给」送出去的每一笔，在自家任务库留档。

    回答用户 08-27 的两问——「派出去给别人的任务有哪些」：任务安排站·处理中
    页签直接可见（session_name=同事·谁，飞鸽通道会标注）；「怎么确认完成」：
    对方说做完了就 ji(action="完成任务", content=底账编号) 销账，或用户在站里
    一键「标记为已处理」（按钮本来就有）。此前这条路只在名录里刷个
    last_sent_at 时间戳，发出去就断线，谁也说不清「上周让阿龟做的那件事到底
    完没完」。回执样开头的直接落「已处理」留档，不在处理中攒永远不会关的卡。

    记账失败绝不拦投递：消息本身已经送达，底账只是查账用。
    返回 (底账任务id或空串, 是否回执)。"""
    kind = _outbound_kind(text)
    try:
        import share_server as ss
        storage = ss._task_storage()
    except Exception:
        storage = None
    if storage is None:
        return "", kind == "receipt"
    body = str(text or "").strip()
    first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    try:
        task = storage.add_task({
            "title": "发给{}：{}".format(who, first[:60] or "（无正文）"),
            "description": body,
            "requester": "发件底账",
            "status": "dispatched" if kind == "task" else "done",
        })
        storage.update_task(task["id"], {
            "dispatched_at": time.time(),
            "session_name": "同事·{}{}".format(
                who, "（飞鸽）" if via == "feige" else ""),
        })
        return str(task["id"]), kind == "receipt"
    except Exception:
        return "", kind == "receipt"


def _deliver_to_person_for_agent(name, text):
    _ensure_console_on_path()
    try:
        from api.taskstage.contacts import deliver_to_person
    except Exception as error:
        return "发信模块不可用：{}".format(error)
    book = _task_contact_book()
    reply = deliver_to_person(book, name, text, feige_send=_feige_send)
    if not reply.get("ok"):
        return reply.get("error") or "没发出去"
    via = reply.get("via")
    who = reply.get("name") or name
    tid, is_receipt = _record_outbound_delivery(who, text, via)
    if not tid:
        tail = ""
    elif is_receipt:
        tail = "；回执已留档任务安排站（已处理）"
    else:
        tail = ("；发件底账已挂任务安排站·处理中（编号 {}）——对方确认完成后调 "
                "ji(action=\"完成任务\", content=该编号) 销账，用户也可在站里手动"
                "标已处理").format(tid)
    if via == "inbox":
        return "✓ 已通过任务安排站发给 {}{}".format(who, tail)
    if via == "feige":
        extra = reply.get("inbox_error")
        if extra:
            return "✓ 任务安排站没通（{}），已改走飞鸽发给 {}{}".format(extra, who, tail)
        return "✓ 已通过飞鸽发给 {}{}".format(who, tail)
    return "✓ 已发给 {}{}".format(who, tail)


def tool_ji(args, client_context=None):
    if _call_runtime(args, client_context) == "cursor":
        wait_if_frozen()
    elif _is_frozen():
        return [{"type": "text", "text": "rxyy MCP 当前已冻结，本次操作未执行；解除冻结后可按需重试。"}]
    action = args.get("action") or ""
    project_path = args.get("project_path") or ""
    content = args.get("content") or ""
    category = args.get("category") or "context"
    conversation_id = (args.get("conversation_id") or "").strip() or None
    task_name = (args.get("task_name") or "").strip() or None
    runtime_kind = _call_runtime(args, client_context)
    native_thread_id = _call_thread_id(args)
    conversation_id = _native_conversation_id(conversation_id, runtime_kind, native_thread_id)

    # 团队黑板（借道 ji）：重大情况主动写、动手前主动读——不依赖对方正好在等回复。
    # 写：ji(action="黑板", category=类别, content=一句话)，类别∈部署/大改/提交/卡住/事故/收工
    # 读：ji(action="黑板")（content 留空）→ 返回全队最近条目
    if action in ("黑板", "board", "留言"):
        if DISABLED:
            return [{"type": "text", "text": "[禁用模式] 黑板已忽略。"}]
        if content.strip():
            kind = (category or "").strip()
            BRIDGE.board_post(conversation_id, kind, content.strip())
            urgent = kind in ("部署", "事故", "大改") or not kind
            return [{"type": "text", "text": (
                "✓ 已写上团队黑板（{}）。全部条目在团队面板「团队黑板」区，队友动手前"
                "会主动来读；{}。要惊动业务线之外的人，再用 ji(action=\"转告\"/"
                "\"广播\") 点名。".format(
                    kind or "大改",
                    "同一条业务线的在线队友已排队收到提醒" if urgent
                    else "这一类只落盘、不打断谁"))}]
        return [{"type": "text", "text": _board_read_text(
            project_path, task_name, conversation_id)}]

    # 任务安排站：派活所在 agent 做完这条就自己标已处理。不跟会话结束挂钩。
    if action in ("完成任务", "任务完成", "complete_task", "done_task"):
        if DISABLED:
            return [{"type": "text", "text": "[禁用模式] 完成任务已忽略。"}]
        return [{"type": "text", "text": _complete_taskstage_for_agent(
            conversation_id, content, task_name)}]

    if action in ("发给", "发给同事", "deliver", "send_to"):
        if DISABLED:
            return [{"type": "text", "text": "[禁用模式] 发给已忽略。"}]
        target = (category or "").strip()
        if target in ("context", "rule", "preference", "pattern"):
            target = ""
        return [{"type": "text", "text": _deliver_to_person_for_agent(target, content)}]

    # agent → agent 互通（借道 ji，无需 Cursor 重新发现新工具）：
    # 转告：ji(action="转告", category=对方tab名或对话ID前8位, content=消息)
    # 广播：ji(action="广播", content=消息) → 同项目所有在线 agent
    #      ji(action="广播", category="本组", content=消息) → 只发同一条业务线
    # 投递走排队（对方下次调 zhi 或 zt 时送达）；找不到目标时控制台会在你下次 zhi 时告知。
    TRACK_WORDS = ("本组", "组内", "本业务线", "同业务线", "本子项目", "track")
    if action in ("转告", "喊话", "relay", "广播", "broadcast"):
        if DISABLED:
            return [{"type": "text", "text": "[禁用模式] 转告已忽略。"}]
        to = (category or "").strip()
        if action in ("广播", "broadcast"):
            to = to if to.lower() in TRACK_WORDS else "团队"
        if not content.strip():
            raise ValueError("缺少消息内容（content）")
        if not to:
            raise ValueError("缺少目标（category=对方tab名/对话ID，或用 action=广播）")
        BRIDGE.relay_message(conversation_id, to, content)
        return [{"type": "text", "text": (
            "✓ 已提交转告 → {}。对方下次调 zhi 或 zt 时送达（zt 顺路取信，它埋头"
            "干活时也收得到）；它正等用户回话时不插队"
            "（那个位置留给用户），排在用户后面。找不到目标的话，你下次调 zhi "
            "会收到失败说明。".format(to)
        )}]

    # 会话摘要（借道 ji）：ji(action="摘要", category=对方tab名或对话ID前8位[留空=自己],
    # content=最大字数[默认 2000]) → hub 按内存实况压一段话回来：干到哪了、正等谁、
    # 最近说过什么。接手/协作前看一眼，比复制几千行接手提示词或翻别人的记录文件省。
    if action in ("摘要", "会话摘要", "summary", "get_session_summary"):
        if DISABLED:
            return [{"type": "text", "text": "[禁用模式] 摘要已忽略。"}]
        target = (category or "").strip()
        if target in ("context", "rule", "preference", "pattern"):
            target = ""  # category 的记忆分类默认值，不是目标
        try:
            max_chars = int(float(str(content).strip())) if str(content).strip() else 2000
        except (TypeError, ValueError):
            max_chars = 2000
        max_chars = max(200, min(max_chars, 8000))
        text = BRIDGE.fetch_summary(conversation_id, target, max_chars)
        if text is None:
            text = ("控制台没有应答摘要请求（hub 可能是老版本或正在重启）。"
                    "退路：读对方的聊天记录文件（历史兼容目录 D:\\持久plus聊天记录\\<tab名>.md）"
                    "或 ji(action=\"转告\") 直接问它。")
        return [{"type": "text", "text": text}]

    # 状态上报（借道 ji，无需 Cursor 重新发现新工具）：action=状态/zt/status 时，
    # content 形如「developing:改 get_state」或「developing」，非阻塞上报到控制台紫点。
    if action in ("状态", "zt", "status", "上报"):
        if DISABLED:
            return [{"type": "text", "text": "[禁用模式] 状态上报已忽略。"}]
        raw = (content or category or "").strip()
        if ":" in raw or "：" in raw:
            st, act = re.split(r"[:：]", raw, 1)
            st, act = st.strip(), act.strip()
        else:
            st, act = raw, ""
        mail = BRIDGE.report_status(
            conversation_id, st, act, task_name,
            model=(args.get("model") or "").strip() or None,
            cwd=_call_project_path(args, client_context),
            runtime_kind=runtime_kind,
            native_thread_id=native_thread_id,
            rpc_scope=_call_scope(client_context),
        )
        cid = BRIDGE.own_conv_id_hint(conversation_id)
        warn = ("" if conversation_id else
                "\n⚠ 本次没带 conversation_id，已按独立新对话记账（没进你原来的 tab）；"
                "下次务必带上你自己的 ID。")
        return [{"type": "text", "text": (
            f"✓ 状态已更新: {st or '(空)'}" + (f" · {act}" if act else "")
            + (f"（conversation_id: {cid}）" if cid else "")
            + "。非阻塞上报，请继续手头工作。"
            + warn
            + _mail_suffix(mail)
        )}]

    if not project_path.strip():
        # 未显式给出时自动用当前工作区（MCP roots 探测），不再强迫 AI 手填
        project_path = detect_project_dir(client_context)
    manager = MemoryManager(project_path)

    if action == "记忆":
        if not content.strip():
            raise ValueError("缺少记忆内容")
        mem_id = manager.add(content, category)
        text = f"✅ 记忆已添加，ID: {mem_id}\n📝 内容: {content}\n📂 分类: {category}"
    elif action == "回忆":
        text = manager.recall()
        if not DISABLED:
            text += (
                "\n\n⚠️ **强制**：完成本请求前必须调用 `zhi` 征求用户反馈；"
                "未获用户明确同意前禁止结束对话。"
            )
        try:
            # 不给默认 task_name：ji 与 zhi 共用 tab 时，
            # 传「记忆」会把用户已命名的任务 tab 强行改名（如 呱声治理→记忆）
            BRIDGE.notify_session(
                f"📚 **记忆已读取**\n\n{text}",
                conversation_id=conversation_id,
                task_name=task_name,
            )
        except Exception as e:
            log(f"ji 回忆同步到控制台失败: {e}")
    else:
        raise ValueError(f"未知的操作类型: {action}")
    return [{"type": "text", "text": text}]


# 工具说明与参数表跟 instructions 一样按请求计费（客户端不走 meta-tool 模式时全量注入），
# 所以这里只写「怎么填」，不写「为什么」——规矩已经在 instructions 里说过一遍了。
TOOLS = [
    {
        "name": "zhi",
        "description": (
            "向用户提问并等回复（阻塞到用户在rxyy MCP控制台回复；wait=false 则只发不等、立刻返回）。"
            "完成请求、准备给最终回复前必须先调它收尾；"
            "改完 bug/UI/功能时用「成果」把产物一并呈上，用户在外面用手机就能验收。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "给用户看的消息"},
                "predefined_options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": ("可点选的选项（可选）。能自己判定最优解就别问，直接做完再报；"
                                    "真要问：互斥、每条写明后果、≤4 条（「结束」不占额度），"
                                    "不要「都干」那种把前面几条并起来的选项——服务端会退回"),
                },
                "成果": {
                    "type": "array",
                    "description": "产物：截图{类型,路径,说明}/对比{前,后,说明}/diff{内容,说明}/链接{地址,说明}。图片用本机绝对路径。",
                    "items": {"type": "object"},
                },
                "card": {
                    "type": "object",
                    "description": ("决策卡片（可选，取代 predefined_options）：几个纠结点一次问完、"
                                    "每个选项带取舍说明、可自由输入。"
                                    "{title, questions:[{id, prompt, options:[{id, label, detail, "
                                    "recommended}], multiple, allowFreeText}], "
                                    "autoDecide:{enabled, onResolve:adopt|delegate, timeoutSec}}。"
                                    "detail 按「为什么这么改；解决什么问题；实现什么效果」写透。"
                                    "答复形如「[决策答复] 问1：选项A；问2：选项B」+ "
                                    "<chijiu-decision answer='{…}'/> 机器标签，优先读标签"),
                },
                "is_markdown": {"type": "boolean", "description": "消息是否 Markdown，默认 true"},
                "wait": {
                    "type": "boolean",
                    "description": ("默认 true=阻塞等回复。false=只发不等：消息落到控制台立刻返回，"
                                    "你接着干活（汇报进展/先说结论后收答复）；之后再调 zhi、"
                                    "同 conversation_id、message 留空即阻塞收回复。"
                                    "期间用户的回复先存着，来取时立刻交付"),
                },
                "conversation_id": {
                    "type": "string",
                    "description": "对话 ID：用户指定则沿用，否则首次随机 8 位 hex，本对话全程复用（控制台按它分 tab）",
                },
                "task_name": {
                    "type": "string",
                    "description": "tab 标题「项目·功能」≤12字；项目写真实子系统。真活改名时同一轮并行 cursor-app-control.rename_chat",
                },
                "model": {
                    "type": "string",
                    "description": "当前模型（Codex 必填，如 gpt-5.6-sol；Cursor 可省略，控制台自己读库）",
                },
                "project_path": {"type": "string", "description": "当前工作区完整路径"},
            },
            "required": ["message"],
        },
    },
    {
        "name": "zt",
        "description": ("上报干活状态（非阻塞、立即返回）。每完成一个动作调一次；"
                        "队友转告/黑板提醒会随返回值一起捎给你。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "analyzing/developing/testing/deploying/reviewing/searching/blocked/ready 等",
                },
                "activity": {"type": "string", "description": "一句话在做什么，≤30 字"},
                "conversation_id": {"type": "string", "description": "与 zhi 同一个"},
                "task_name": {
                    "type": "string",
                    "description": "tab 标题「项目·功能」≤12字；真活后第一次 zt 带上。侧栏标题不对则同一轮并行 rename_chat",
                },
                "model": {
                    "type": "string",
                    "description": "当前模型（Codex 必填，如 gpt-5.6-sol；Cursor 可省略）",
                },
            },
            "required": ["status"],
        },
    },
    {
        "name": "ji",
        "description": ("记忆/回忆（仅在用户明确要求时调用）；"
                        "也是 agent 间互通的通道：action=转告/广播 给同项目其他 agent 递话；"
                        "action=黑板 读写团队黑板（重大情况：部署/大改/提交/卡住/事故/收工，"
                        "content 留空=读）；"
                        "action=完成任务 把任务安排站标已处理（content=派发编号，不绑会话）；"
                        "action=发给 把消息送给某人（category=人名；先任务安排站再飞鸽）；"
                        "action=摘要 取某个会话干到哪了的压缩摘要（category=对方tab名或"
                        "对话ID前8位，留空=自己；接手/协作前先看这个，别翻几千行记录）。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "description": "记忆 / 回忆 / 状态（=zt）/ 转告 / 广播 / 黑板 / 完成任务 / 发给 / 摘要"},
                "project_path": {"type": "string", "description": "项目路径，可省略"},
                "content": {
                    "type": "string",
                    "description": ("记忆内容；action=状态 传「developing:改 get_state」；"
                                    "action=转告/广播 传要递的话；"
                                    "action=完成任务 填派发文案里的任务编号；"
                                    "action=发给 填要发出去的正文；"
                                    "action=摘要 可填最大字数（默认 2000）"),
                },
                "category": {"type": "string",
                             "description": ("记忆分类 rule/preference/pattern/context；"
                                             "action=转告/摘要 时=对方tab名或对话ID前8位；"
                                             "action=广播 时填「本组」=只发同业务线；"
                                             "action=发给 时=人名（如肖宇轩）")},
                "conversation_id": {"type": "string", "description": "与 zhi 同一个，可选"},
                "task_name": {"type": "string", "description": "tab 标题，可选"},
            },
            "required": ["action"],
        },
    },
]


_RUNTIME_PROPERTY = {
    "type": "string",
    "enum": list(RUNTIME_KINDS),
    "description": "调用来源运行时（可选）：cursor、codex、chatgpt 或 unknown",
}
_THREAD_ID_PROPERTY = {
    "type": "string",
    "description": "宿主原生线程/会话 ID（可选；Codex/ChatGPT 可传）",
}


def _decorate_tool_schemas():
    """在原 schema 上补跨客户端字段，保持旧字段、required 和中文键不变。"""
    for tool in TOOLS:
        props = tool.setdefault("inputSchema", {}).setdefault("properties", {})
        props.setdefault("runtime", copy.deepcopy(_RUNTIME_PROPERTY))
        props.setdefault("thread_id", copy.deepcopy(_THREAD_ID_PROPERTY))
        # 三个工具都可能写 hub/记忆/消息；保守地明确不是只读工具。
        tool.setdefault("annotations", {"readOnlyHint": False})
        if tool.get("name") == "zt":
            props.setdefault("project_path", {
                "type": "string",
                "description": "当前工作区完整路径（可选；有效目录会作为 cwd 上报）",
            })
    zhi_props = next(t for t in TOOLS if t["name"] == "zhi")["inputSchema"]["properties"]
    if "artifacts" not in zhi_props:
        zhi_props["artifacts"] = copy.deepcopy(zhi_props.get("成果") or {
            "type": "array", "items": {"type": "object"},
        })
        zhi_props["artifacts"]["description"] = (
            "Artifacts 的英文同义键；与「成果」相同，保留截图/对比/diff/链接对象。")


_decorate_tool_schemas()


def tools_for_runtime(runtime_kind):
    """返回本运行时看到的工具描述；Cursor 继续使用原有完整文案。"""
    kind = normalize_runtime(runtime_kind)
    if kind == "cursor":
        return TOOLS
    tools = copy.deepcopy(TOOLS)
    by_name = {tool["name"]: tool for tool in tools}
    if kind != "cursor":
        by_name["zhi"]["description"] = (
            "向用户提问或汇报。默认 wait=false 只发不等；明确 wait=true 最多等待45秒，"
            "未收到回复时提问仍保留，可稍后再次调用领取。")
    else:
        by_name["zhi"]["description"] = (
            "向用户提问或汇报；wait=true 等待回复，wait=false 只发不等，"
            "未收到回复时提问仍保留，可稍后再次调用领取。")
    by_name["zhi"]["inputSchema"]["properties"]["task_name"]["description"] = (
        "可选的会话标题「项目·功能」，用于控制台识别当前工作。")
    by_name["zhi"]["inputSchema"]["properties"]["model"]["description"] = (
        "当前模型名称（可选）。")
    if kind != "cursor":
        by_name["zhi"]["inputSchema"]["properties"]["wait"]["description"] = (
            "默认 false=只发不等；true=最多等待45秒，未回复时问题仍保留，"
            "之后用同 conversation_id、message 留空再次领取。")
    else:
        by_name["zhi"]["inputSchema"]["properties"]["wait"]["description"] = (
            "默认 true=等待回复；false=只发不等，之后用同 conversation_id、"
            "message 留空再次领取。")
    by_name["zt"]["description"] = "非阻塞状态上报；可携带项目路径和宿主线程信息。"
    by_name["zt"]["inputSchema"]["properties"]["task_name"]["description"] = (
        "可选的会话标题「项目·功能」。")
    by_name["zt"]["inputSchema"]["properties"]["model"]["description"] = (
        "当前模型名称（可选）。")
    by_name["ji"]["description"] = (
        "按需访问记忆、转告、广播、黑板或会话摘要；具体 action 见参数说明。")
    return tools


# ---------------- JSON-RPC over stdio ----------------
def write_message(obj):
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    with _stdout_lock:
        try:
            sys.stdout.buffer.write(data + b"\n")
            sys.stdout.buffer.flush()
        except (BrokenPipeError, OSError, ValueError):
            # stdout 被 Cursor 关闭（进程正被回收）：静默忽略，别把工作线程搞崩
            pass


def reply_result(req_id, result):
    write_message({"jsonrpc": "2.0", "id": req_id, "result": result})


def reply_error(req_id, code, message):
    write_message({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def _rpc_result(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _sse_progress_frame(tok, progress, message):
    """一帧 SSE：带 progressToken 时是真 notifications/progress，否则退回注释心跳。

    抽成纯函数是为了能单测「首包即时化」这条缓解（身份根治⑦）的载荷形状。"""
    if tok is not None:
        note = json.dumps({
            "jsonrpc": "2.0", "method": "notifications/progress",
            "params": {"progressToken": tok, "progress": progress,
                       "message": message}}, ensure_ascii=False)
        return ("event: message\r\ndata: " + note + "\r\n\r\n").encode("utf-8")
    return (": " + (message or "keepalive") + "\r\n\r\n").encode("utf-8")


def _sse_prime_frame(event_id, retry_ms=15000):
    """2025-11-25 Streamable HTTP：SSE 首事件带 id + 空 data，给客户端
    Last-Event-ID 续流当锚点。空 data 会让更老的客户端按 JSON 解析炸掉，
    只给协议 ≥ 2025-11-25 的请求发（与官方 python-sdk 一致）。"""
    return ("id: {}\r\nretry: {}\r\ndata: \r\n\r\n".format(
        event_id, int(retry_ms))).encode("utf-8")


def _protocol_from_headers(headers):
    if headers is None:
        return ""
    get = headers.get if hasattr(headers, "get") else lambda k, d="": d
    return (get("MCP-Protocol-Version") or get("mcp-protocol-version") or "").strip()


def _protocol_at_least(headers, want="2025-11-25"):
    ver = _protocol_from_headers(headers)
    return bool(ver) and ver >= want


def _mcp_path_ok(path):
    p = (path or "").split("?")[0].rstrip("/")
    return p in ("/mcp", "") or p.startswith("/mcp/")


def _accepts_sse(accept):
    a = (accept or "").lower()
    return "text/event-stream" in a or "*/*" in a


def _zhi_sse_disconnect_cancels():
    """POST SSE 传输断开要不要 cancel 这条 zhi。

    2025-11-25：Disconnection SHOULD NOT be interpreted as the client cancelling.
    旧实现断线即 cancel，Cursor reinit 拆掉长挂流时会把还在等的提问杀掉，
    agent 只能再开一枪——第七节 4–6 次重试的服务端放大器。显式
    notifications/cancelled 仍走 cancel_request。"""
    return False


_sse_id_seq = 0
_sse_id_lock = threading.Lock()
_zhi_event_index = {}
_zhi_event_lock = threading.Lock()
# 每条流只留最近几个事件 id 供 Last-Event-ID 续流（客户端只会拿它收到的最后
# 一个 id 来续）。心跳 15s 一个 id，不修剪的话挂几小时的待命 zhi 会在索引里
# 攒下几百条；被 cancelled/断流后没人清，常驻进程里就是纯泄漏。
_ZHI_IDS_KEEP = 8
# 结果已出但迟迟没送出去（客户端既不续流也不取消 = agent 进程多半没了）：
# 超过这个时长就回收登记，别让 _active_sse_calls/_zhi_event_index 只涨不落。
_ZHI_ORPHAN_TTL = 600


def _alloc_sse_id(kind="zhi"):
    global _sse_id_seq
    with _sse_id_lock:
        _sse_id_seq += 1
        return "{}-{}".format(kind, _sse_id_seq)


def _sse_activity_key(scope, rpc_id):
    """为活动 SSE 生成隔离键；旧的无 scope 调用保留裸 id 兼容形态。"""
    rid = str(rpc_id)
    scope = str(scope or "").strip()
    return (scope, rid) if scope else rid


def _get_active_sse_call(scope, rpc_id):
    """只按精确 session/connection scope 查活动调用，禁止跨客户端猜测。"""
    with _sse_calls_lock:
        return _active_sse_calls.get(_sse_activity_key(scope, rpc_id))


def _index_zhi_event(event_id, stream):
    with _zhi_event_lock:
        stream.ids.append(event_id)
        _zhi_event_index[event_id] = stream
        while len(stream.ids) > _ZHI_IDS_KEEP:
            _zhi_event_index.pop(stream.ids.pop(0), None)


def _lookup_zhi_stream(event_id, scope=None):
    with _zhi_event_lock:
        stream = _zhi_event_index.get(event_id)
        if stream is None or scope is None:
            return stream
        return stream if getattr(stream, "scope", None) == scope else None


def _drop_zhi_stream(stream):
    with _zhi_event_lock:
        for eid in list(getattr(stream, "ids", []) or []):
            _zhi_event_index.pop(eid, None)


def _sweep_zhi_orphans():
    """回收「结果已出却永远送不出去」的 zhi 流。

    流断开且客户端再没来续（Cursor 窗口关了/agent 进程没了）时，work 线程
    照常算完结果、done 置位，但 result_sent 永远等不到 True——pump 的 finally
    只在送达后清登记。这类条目会连着几百个心跳事件 id 一起滞留。被吞的回复
    本身有 hub 的补送兜底，这里只负责把登记表清干净。"""
    now = time.time()
    doomed = []
    with _sse_calls_lock:
        for rid, info in list(_active_sse_calls.items()):
            st = info.get("stream")
            if (st is not None and st.done.is_set()
                    and not getattr(st, "result_sent", False)
                    and now - getattr(st, "done_at", now) > _ZHI_ORPHAN_TTL):
                doomed.append((rid, info))
                _active_sse_calls.pop(rid, None)
    for rid, info in doomed:
        _drop_zhi_stream(info["stream"])
        log("回收孤儿 zhi 流 rpc_id={} conv={}（结果无人接收超 {} 秒）".format(
            rid, (info.get("conv") or "-")[:8], _ZHI_ORPHAN_TTL))


def dispatch_request(req, sse=False, client_context=None):
    """处理一条 JSON-RPC 请求并【返回】应答 dict（传输层无关，stdio/HTTP 共用）。
    sse=True：本次是 HTTP SSE 传输，zhi 走服务端内部续期循环（agent 零往返）。
    client_context：HTTP/stdio 本连接的 RuntimeContext；不传时保留旧的直接调用语义。"""
    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        if client_context is not None:
            client_context.apply_initialize(
                params, profile=getattr(client_context, "profile", "") or None,
                session_id=getattr(client_context, "session_id", "") or None)
            runtime_kind = client_context.runtime_kind
        else:
            # 仅供 stdio/直接单连接调用的旧兼容路径；HTTP 始终传自己的 context，
            # 不会再把某个 HTTP 客户端的握手写进共享全局。
            CLIENT_CAPS.clear()
            CLIENT_CAPS.update(params.get("capabilities") or {})
            runtime_kind = infer_runtime(params.get("clientInfo") or {})
        proto = params.get("protocolVersion") or "2024-11-05"
        # 记下客户端协商的协议版本与能力：要判断能不能改用 2026-07-28 的 Tasks 扩展
        # （tools/call 直接返回任务句柄、客户端轮询，彻底不用长占并发槽位），
        # 唯一依据就是 Cursor 这边到底声明支持到哪一版
        try:
            ci = params.get("clientInfo") or {}
            log("initialize: proto={} client={}/{} caps={} ext={}".format(
                proto, ci.get("name"), ci.get("version"),
                sorted((params.get("capabilities") or {}).keys()),
                sorted((params.get("capabilities") or {}).get("extensions", {}) or {})))
        except Exception:
            pass
        return _rpc_result(req_id, {
            "protocolVersion": proto,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": server_instructions(
                runtime_kind=runtime_kind,
                client_info=params.get("clientInfo") or {},
                profile=getattr(client_context, "profile", None),
            ),
        })
    elif method == "ping":
        return _rpc_result(req_id, {})
    elif method == "tools/list":
        kind = (getattr(client_context, "runtime_kind", None)
                if client_context is not None else "cursor")
        return _rpc_result(req_id, {"tools": tools_for_runtime(kind)})
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            if name == "zhi":
                content = tool_zhi(args, rpc_id=req_id, sse_loop=sse,
                                   client_context=client_context)
            elif name == "zt":
                content = tool_zt(args, client_context=client_context)
            elif name == "ji":
                content = tool_ji(args, client_context=client_context)
            else:
                return _rpc_error(req_id, -32602, f"未知的工具: {name}")
            return _rpc_result(req_id, {"content": content, "isError": False})
        except ZhiCancelled:
            # 客户端已取消该请求，任何应答都会被忽略；按协议回一个取消错误即可
            return _rpc_error(req_id, -32800, "请求已被客户端取消")
        except ValueError as e:
            return _rpc_error(req_id, -32602, str(e))
        except ConnectionError as e:
            return _rpc_result(req_id, {
                "content": [{"type": "text", "text": f"控制台通信失败: {e}"}],
                "isError": True,
            })
        except Exception as e:
            return _rpc_result(req_id, {
                "content": [{"type": "text", "text": f"工具执行失败: {e}"}],
                "isError": True,
            })
    else:
        if req_id is not None:
            return _rpc_error(req_id, -32601, f"方法未实现: {method}")
        return None


def handle_request(req, client_context=None):
    resp = dispatch_request(req, client_context=client_context)
    if resp is not None:
        write_message(resp)


def request_roots(client_context=None):
    """向客户端请求工作区根目录（客户端须声明 roots 能力）"""
    caps = getattr(client_context, "capabilities", None)
    caps = caps if isinstance(caps, dict) else CLIENT_CAPS
    if "roots" not in caps:
        return
    write_message({"jsonrpc": "2.0", "id": ROOTS_REQ_ID, "method": "roots/list"})


def handle_client_response(msg, client_context=None):
    """处理客户端对我们发出的请求的应答（目前只有 roots/list）"""
    if msg.get("id") != ROOTS_REQ_ID:
        return
    roots = (msg.get("result") or {}).get("roots") or []
    if roots:
        path = _uri_to_path(roots[0].get("uri") or "")
        if path:
            if client_context is not None:
                client_context.root_path = path
            CLIENT_ROOTS["dir"] = path
            log(f"工作区目录: {path}")
    if client_context is None:
        ROOTS_EVENT.set()


def _install_crash_forensics():
    """崩溃取证：今早 pid=31160 死掉时没有留下「stdin 关闭」正常退出日志，
    说明是硬杀或崩溃。faulthandler 抓致命错误、excepthook 抓线程未捕获异常、
    atexit 记录正常退出——下次进程消失时日志能区分：正常回收/崩溃/被硬杀（无任何记录=硬杀）。"""
    try:
        import faulthandler
        import atexit
        fh = open(APP_DIR / "server-crash.log", "a", encoding="utf-8", buffering=1)
        fh.write("--- pid={} 启动 {} ---\n".format(os.getpid(), time.strftime("%m-%d %H:%M:%S")))
        faulthandler.enable(file=fh)

        def _thread_hook(args):
            log("线程崩溃: {} {}: {}".format(
                getattr(args.thread, "name", "?"),
                args.exc_type.__name__, args.exc_value))
        threading.excepthook = _thread_hook
        atexit.register(lambda: log("进程正常退出（atexit）"))
    except Exception:
        pass


def main():
    log("=== MCP 服务器启动 pid={} python={} ===".format(
        os.getpid(), sys.version.split()[0]))
    _t0 = time.time()
    _install_crash_forensics()
    # stdio 一进程只承载一条 MCP 连接；身份状态随本连接传给 dispatch_request，
    # 不借用 HTTP registry，也不被其它客户端的 initialize 改写。
    stdio_context = RuntimeContext(
        connection_id="stdio-" + uuid.uuid4().hex)
    # 注意：不要主动向 Cursor 发协议级 ping——实测 Cursor 的 Shared MCP process
    # 不认服务端发起的请求，会直接掐掉 transport（9:12-9:23 每 30s 断一次的元凶）。
    # 只因 stdin 被 Cursor 关闭而退出（进程被回收）；任何单行处理异常都吞掉、继续读，
    # 绝不让内部错误导致进程自杀——否则表现为 Cursor 面板反复红「Error」。
    while True:
        try:
            line = sys.stdin.buffer.readline()
        except Exception:
            break
        if not line:  # EOF：Cursor 关闭了 stdin（回收进程），正常退出
            break
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line.decode("utf-8"))
            method = req.get("method")
            if method is None:
                handle_client_response(req, stdio_context)  # roots/list 结果
                continue
            if req.get("id") is None:
                if method == "notifications/initialized":
                    request_roots(stdio_context)
                elif method == "notifications/roots/list_changed":
                    request_roots(stdio_context)
                elif method == "notifications/cancelled":
                    BRIDGE.cancel_request(
                        (req.get("params") or {}).get("requestId"),
                        rpc_scope=stdio_context.scope)
                continue
            threading.Thread(
                target=handle_request, args=(req, stdio_context), daemon=True).start()
        except Exception as e:
            log(f"处理请求异常（已忽略，进程继续）: {e}")
            continue
    log("stdin 关闭，服务器退出（存活 {:.0f}s）—— 通常是 Cursor 回收了 MCP 进程".format(
        time.time() - _t0))


# ---------------- Streamable HTTP 传输（根治 stdio 进程被 Cursor 回收） ----------------
def _cleanup_stale_spawn_lock():
    """清上一代进程死亡残留的 .hub-spawn.lock（20s 过期机制之外的双保险；
    07-27 事故中锁文件残留着已死进程的 pid）。"""
    try:
        lk = BRIDGE._SPAWN_LOCK
        if lk.exists() and time.time() - lk.stat().st_mtime > 60:
            lk.unlink()
            log("已清理残留的 .hub-spawn.lock")
    except OSError:
        pass


def _touch_mcp_json_nonce():
    """成功绑定端口后触碰 ~/.cursor/mcp.json 的rxyy MCP nonce：各 Cursor 窗口的
    HTTP 客户端立即重新 initialize（毫秒级握手），不必在 error 退避态里干等几分钟。
    hub 的守护巡检也会在「离线→上线」时触碰，但那要求 hub 活着——本进程被看门狗
    或重启脚本单独救活（hub 死透）的场景没人叫醒客户端，所以上线时自己触碰一次。
    实现统一收口到 mcp_touch（带重试与失败原因）。"""
    ok, reason = touch_mcp_json()
    if not ok:
        log("触碰 mcp.json 失败: {}".format(reason))
    return ok


def _probe_watchdog(wport):
    """探看门狗守卫端口。返回 ("dead"|"alive"|"frozen", pid)。

    07-27 15:03 事故：看门狗主循环已死，但守卫端口的 accept-drain 线程还在应答——
    纯 connect 探活被「假活」骗过，39222 死了没人拉。守卫协议升级后 drain 线程
    会回一行「pid:主循环时间戳」，这里读它：时间戳停滞 >180s = 主循环冻死。
    旧版看门狗（不回 token）连接成功即视为活着（向后兼容）。"""
    try:
        s = socket.create_connection(("127.0.0.1", wport), timeout=1.5)
    except OSError:
        return "dead", None
    try:
        s.settimeout(2)
        raw = b""
        try:
            raw = s.recv(64)
        except OSError:
            pass
    finally:
        try:
            s.close()
        except OSError:
            pass
    try:
        pid_s, ts_s = raw.decode("ascii").strip().split(":", 1)
        pid, ts = int(pid_s), float(ts_s)
    except (ValueError, UnicodeDecodeError):
        return "alive", None  # 旧版协议：端口应答即视为活
    if time.time() - ts > 180:
        return "frozen", pid
    return "alive", pid


def _taskkill(pid):
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=10, creationflags=0x08000000)
        return True
    except Exception:
        return False


def _spawn_watchdog():
    from frozen_boot import hidden_popen_kwargs, script_dir, spawn_argv
    cmd = spawn_argv(APP_DIR / "watchdog.py")
    subprocess.Popen(
        cmd, cwd=script_dir(cmd, APP_DIR),
        **hidden_popen_kwargs())


# watchdog 互保线程整个进程只准一条：hub 内嵌形态下 serve_http 会因半死自愈
# 被巡检反复重建，每次重建都无脑起一条就会越攒越多
_WD_GUARD_STARTED = False
_wd_guard_lock = threading.Lock()


def _watchdog_guard_loop():
    """互保：看门狗死了/冻死了由本进程拉回（watchdog 自带端口单例守卫，双拉安全）。
    对应 07-27 事故：看门狗与本守护进程同时被清进程带走后，全链再无第一推动；
    以及 15:03 事故：看门狗主循环冻死但守卫端口假活，39222 挂了没人管。"""
    wport = 38996
    try:
        _cfg = json.loads((DATA_DIR / "config.json").read_text(encoding="utf-8"))
        wport = int(_cfg.get("watchdog_port", 38996) or 38996)
    except Exception:
        pass
    frozen_seen = 0
    while True:
        time.sleep(30)
        # 换主后本进程就是「退役那套」的残留，别再把自己那套的看门狗拉回来打架
        stand_down, why = instance_owner.should_stand_down(APP_DIR)
        if stand_down:
            log("不再守护看门狗：{}".format(why))
            return
        state, wpid = _probe_watchdog(wport)
        if state == "alive":
            frozen_seen = 0
            continue
        if state == "frozen":
            # 连续两轮（约 1 分钟）都停滞才动手，防单次探测撞上它正在跑慢命令
            frozen_seen += 1
            if frozen_seen < 2:
                continue
            log("看门狗主循环停滞 >180s（pid={}），强杀后重拉".format(wpid))
            if wpid:
                _taskkill(wpid)
                time.sleep(1)
            frozen_seen = 0
        if not (APP_DIR / "watchdog.py").is_file():
            continue
        try:
            _spawn_watchdog()
            log("看门狗离线，已拉起 watchdog.py")
        except Exception as e:
            log(f"拉起看门狗失败: {e}")


def serve_http(port, in_hub=False):
    """以常驻方式提供 MCP Streamable HTTP 端点（mcp.json 用 url 接入）。

    stdio 模式下进程由 Cursor spawn，生杀大权在 Cursor 手里：闲置回收、FSM 竞态、
    共享进程重启……任何一刀都是全部对话一起断（官方论坛多个已确认 bug，无客户端解法）。
    HTTP 模式下本端点常驻本机，Cursor 手里没有进程可杀——它顶多断 HTTP
    连接，重连是毫秒级 initialize，所有 in-flight zhi 的等待线程原地不动。
    实现按 MCP Streamable HTTP **2025-11-25**：POST /mcp 收 JSON-RPC；
    zhi 的应答走 SSE；GET /mcp 开一条常驻 SSE 长连接（规范「Listening for
    Messages」；旧实现回 405，Cursor 会话层会拿去当「这路传输不行」打进
    reinit）。旧 Cursor /mcp 连接继续兼容无 MCP-Session-Id；Codex/ChatGPT 或
    显式 native profile 会收到 MCP-Session-Id，以便跨 HTTP 连接恢复上下文。
    POST SSE 断线按规范不断 zhi。

    in_hub=True（hub 拆分第二刀）：本函数跑在 hub 进程的一条线程上（宿主 =
    hub.mcp_http_daemon_loop），BRIDGE 经进程内管道直达 Hub。差异只有两处：
    ① 不装进程级崩溃取证（faulthandler/excepthook/atexit 归宿主 hub 管）；
    ② 半死自愈不再 os._exit 自杀（那会把整个 hub 陪葬）——关掉本 HTTP 实例
    退出线程，hub 的 10s 巡检随即重建。其余（SSE/progress/KEEPALIVE/让路、
    watchdog 互保、心跳名单捞回）与独立进程形态一字不差。"""
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass  # 不写 stderr（Cursor 会记成 error 红字）

        def finish(self):
            """连接关闭时释放无 session 的连接索引，保留可恢复的 session。"""
            try:
                super().finish()
            finally:
                HTTP_RUNTIME_REGISTRY.release_connection(
                    "handler-{}".format(id(self)))

        def _reply(self, code, obj=None, headers=None):
            body = b"" if obj is None else json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            if body:
                self.send_header("Content-Type", "application/json")
            for name, value in (headers or {}).items():
                if value is not None and str(value) != "":
                    self.send_header(str(name), str(value))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _runtime_context(self, initialize=False, params=None):
            # BaseHTTPRequestHandler 实例对应一条 TCP 连接，id(self) 可作为旧
            # sessionless 客户端的最后一道隔离边界；有 Mcp-Session-Id 时则由
            # registry 跨连接恢复同一份上下文。
            key = "handler-{}".format(id(self))
            profile = profile_from_path(self.path)
            if initialize:
                context, _issued = HTTP_RUNTIME_REGISTRY.initialize(
                    key, params or {}, profile=profile,
                    incoming_session_id=header_value(self.headers, SESSION_HEADER))
                self._mcp_runtime = context
                return context
            context = HTTP_RUNTIME_REGISTRY.resolve(key, self.headers, profile=profile)
            self._mcp_runtime = context
            return context

        @staticmethod
        def _runtime_headers(context):
            if context is not None and getattr(context, "advertise_session", False):
                return {SESSION_HEADER: context.session_id}
            return {}

        def do_POST(self):
            # 允许 /mcp/<任意后缀>：Cursor 把「不同 URL 的条目」当成不同的 MCP server，
            # 各有各的请求队列。于是可以给每个工作区配一条 /mcp/<项目名>，
            # 跨项目互相堵塞直接消失（同一工作区内多对话仍共用一条队列）。
            path = self.path.split("?")[0].rstrip("/")
            if path not in ("/mcp", "") and not path.startswith("/mcp/"):
                self._reply(404, {"error": "not found"})
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception:
                self._reply(400, _rpc_error(None, -32700, "parse error"))
                return
            if not isinstance(req, dict):
                self._reply(400, _rpc_error(None, -32600, "batch 请求暂不支持"))
                return
            context = self._runtime_context(
                initialize=req.get("method") == "initialize",
                params=(req.get("params") or {}),
            )
            if req.get("id") is None or req.get("method") is None:
                # 通知或客户端应答：处理后按规范回 202（无 body）
                method = req.get("method") or ""
                if method == "notifications/cancelled":
                    rid = (req.get("params") or {}).get("requestId")
                    activity_key = _sse_activity_key(context.scope, rid)
                    with _sse_calls_lock:
                        info = _active_sse_calls.pop(activity_key, None)
                    if info is not None and info.get("stream") is not None:
                        # 事件 id 索引也得清：取消后这条流不会再被续，滞留的
                        # 心跳 id（15s 一个，挂几小时=几百条）就是纯泄漏
                        _drop_zhi_stream(info["stream"])
                    if info is not None:
                        el = time.time() - info["t0"]
                        if info.get("probe"):
                            log("超时探测被客户端取消 rpc_id={} elapsed={:.0f}s".format(rid, el))
                            if info.get("token") is not None and 60 <= el <= 200:
                                _record_progress_resets(
                                    False, "探测在{:.0f}s被客户端取消".format(el))
                        elif (info.get("token") is not None and 100 <= el <= 150
                                and _progress_resets() is True):
                            # 无预算模式下线上 zhi 在 120s 特征区间被掐 = Cursor 行为
                            # 又变了（progress 失效），自动回退安全拍，下一拍即生效
                            _record_progress_resets(
                                False, "线上zhi在{:.0f}s被取消，自动回退安全拍".format(el))
                    # 会话旁证随取消下传：cancelled 只带 requestId，HTTP session/
                    # connection scope 由本端补上，跨客户端同号互不可见。
                    BRIDGE.cancel_request(
                        rid, conv=(info or {}).get("conv"),
                        rpc_scope=context.scope)
                self._reply(202)
                return
            # 到达日志：判断「同时开六个新对话只进来一个」到底卡在哪，唯一硬证据就是
            # 各窗口的 tools/call 实际抵达时刻（客户端排队的话，抵达会被拉成一串）
            try:
                if (req.get("params") or {}).get("name") in ("zhi", "zt"):
                    a = (req.get("params") or {}).get("arguments") or {}
                    log("到达 {} conv={} peer={} path={} progressToken={}".format(
                        (req.get("params") or {}).get("name"),
                        (a.get("conversation_id") or "-")[:8],
                        "%s:%s" % self.client_address, self.path,
                        "有" if ((req.get("params") or {}).get("_meta")
                                 or {}).get("progressToken") is not None else "无"))
            except Exception:
                pass
            # zhi 走 SSE 流式：立即回响应头（绕过 Cursor 300s Undici 头超时），用户回复后
            # 再从流里送 JSON-RPC 结果；期间心跳保活。agent 一次调用等到底，零往返零 token。
            if req.get("method") == "tools/call" \
                    and (req.get("params") or {}).get("name") == "zhi":
                self._serve_zhi_sse(req, context)
                return
            # 其余请求（zt/ji/initialize/tools_list…）秒回，走普通 JSON
            resp = dispatch_request(req, client_context=context)
            try:
                self._reply(200, resp if resp is not None else {},
                            headers=(self._runtime_headers(context)
                                     if req.get("method") == "initialize" else None))
            except (BrokenPipeError, ConnectionError, OSError):
                pass  # 客户端已放弃本次请求（agent 被打断），静默丢弃

        def _serve_zhi_sse(self, req, context=None):
            """zhi 专用 Streamable HTTP SSE：headers 立即送出 → 心跳/进度 →
            用户回复后送一条 message 事件（JSON-RPC 结果）→ 关闭连接。
            headers 即时到 = Undici 300s 头超时不触发。
            请求带 progressToken 时发真 notifications/progress——新版 Cursor 的
            120s 协议层硬超时若 honor resetTimeoutOnProgress 即被不断重置。
            task_name="__timeout_probe__" 为超时探测：不进 hub、睡 N 秒自动应答，
            用于实测 progress 能否续命（结论持久化，见 _record_progress_resets）。"""
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache, no-transform")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
            except (BrokenPipeError, ConnectionError, OSError):
                return
            self.close_connection = False
            params = req.get("params") or {}
            tok = (params.get("_meta") or {}).get("progressToken")
            args = params.get("arguments") or {}
            rid = req.get("id")
            conv = (str(args.get("conversation_id") or "").strip() or None)
            probe_secs = None
            if (args.get("task_name") or "") == "__timeout_probe__":
                try:
                    probe_secs = max(5, min(900, int(
                        str(args.get("message") or "150").strip() or "150")))
                except ValueError:
                    probe_secs = 150
                log("超时探测开始 rpc_id={} 时长={}s progressToken={}".format(
                    rid, probe_secs, "有" if tok is not None else "无"))
            stream = type("ZhiSseStream", (), {})()
            stream.rid, stream.conv, stream.tok = rid, conv, tok
            stream.scope = getattr(context, "scope", None) or None
            stream.ids, stream.n = [], 0
            stream.box, stream.done = {}, threading.Event()
            stream.write_lock = threading.Lock()
            stream.result_sent = False
            activity_key = _sse_activity_key(stream.scope, rid)
            with _sse_calls_lock:
                _active_sse_calls[activity_key] = {
                    "t0": time.time(), "token": tok,
                    "probe": probe_secs is not None,
                    "conv": conv, "scope": stream.scope, "stream": stream,
                }
            # 新 zhi 到达 = 天然的回收节拍：顺手清掉早已死透的孤儿流登记
            _sweep_zhi_orphans()
            # 2025-11-25 先发 priming（id + 空 data），再发 progress=0。
            # 首包即时化（身份根治⑦）仍然成立：headers 之后立刻有帧。
            try:
                if _protocol_at_least(self.headers):
                    eid0 = _alloc_sse_id("zhi")
                    _index_zhi_event(eid0, stream)
                    self.wfile.write(_sse_prime_frame(eid0))
                eid = _alloc_sse_id("zhi")
                _index_zhi_event(eid, stream)
                self.wfile.write(b"id: " + eid.encode() + b"\r\n")
                self.wfile.write(_sse_progress_frame(tok, 0, "已就绪，等待用户回复"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionError, OSError):
                log("zhi SSE 首包未写出 rpc_id={}（请求保留，等 GET 续流/重呼）".format(rid))
                self._start_zhi_work(req, stream, probe_secs, tok, context)
                return
            self._start_zhi_work(req, stream, probe_secs, tok, context)
            self._pump_zhi_sse(self.wfile, stream, probe_secs, tok)

        def _start_zhi_work(self, req, stream, probe_secs, tok, context=None):
            if getattr(stream, "work_started", False):
                return
            stream.work_started = True
            rid = stream.rid

            def work():
                try:
                    if probe_secs is not None:
                        time.sleep(probe_secs)
                        stream.box["resp"] = _rpc_result(rid, {"content": [{
                            "type": "text",
                            "text": "PROBE_OK 阻塞{}s 未被客户端超时（progressToken={}）".format(
                                probe_secs, "有" if tok is not None else "无")}],
                            "isError": False})
                    else:
                        stream.box["resp"] = dispatch_request(
                            req, sse=True, client_context=context)
                except Exception as e:
                    stream.box["resp"] = _rpc_error(rid, -32603, "工具执行失败: {}".format(e))
                finally:
                    stream.done_at = time.time()  # 孤儿回收的计时起点
                    stream.done.set()

            threading.Thread(target=work, daemon=True).start()

        def _pump_zhi_sse(self, wfile, stream, probe_secs, tok):
            rid = stream.rid
            try:
                while not stream.done.wait(timeout=15):
                    stream.n += 1
                    eid = _alloc_sse_id("zhi")
                    _index_zhi_event(eid, stream)
                    wfile.write(b"id: " + eid.encode() + b"\r\n")
                    wfile.write(_sse_progress_frame(tok, stream.n, "等待用户回复中"))
                    wfile.flush()
                self._write_zhi_result(wfile, stream)
                if probe_secs is not None and tok is not None and probe_secs > 125:
                    _record_progress_resets(True, "探测阻塞{}s 存活".format(probe_secs))
            except (BrokenPipeError, ConnectionError, OSError):
                # 2025-11-25：断线 ≠ 取消。旧实现这里 cancel_request 会在
                # Cursor reinit 时把还在等的 zhi 杀掉。
                if _zhi_sse_disconnect_cancels():
                    BRIDGE.cancel_request(rid, conv=stream.conv,
                                          rpc_scope=stream.scope)
                else:
                    log("zhi SSE 传输断开 rpc_id={} conv={}（不断请求，等 GET 续流或 agent 重呼）".format(
                        rid, (stream.conv or "-")[:8]))
            finally:
                if stream.done.is_set() and stream.result_sent:
                    with _sse_calls_lock:
                        _active_sse_calls.pop(
                            _sse_activity_key(stream.scope, rid), None)

        def _write_zhi_result(self, wfile, stream):
            with stream.write_lock:
                if stream.result_sent:
                    return
                payload = json.dumps(stream.box.get("resp") or _rpc_result(stream.rid, {}),
                                     ensure_ascii=False)
                eid = _alloc_sse_id("zhi")
                _index_zhi_event(eid, stream)
                wfile.write(("id: {}\r\nevent: message\r\ndata: {}\r\n\r\n".format(
                    eid, payload)).encode("utf-8"))
                wfile.flush()
                stream.result_sent = True
                _drop_zhi_stream(stream)

        def do_GET(self):
            # 2025-11-25 Listening for Messages：GET 必须能开 SSE 长连接。
            # 旧实现一律 405，规范允许但 Cursor 会话层会把它当成传输失败去 reinit。
            if not _mcp_path_ok(self.path):
                self._reply(404, {"error": "not found"})
                return
            if not _accepts_sse(self.headers.get("Accept")):
                self._reply(405)
                return
            last_id = (self.headers.get("Last-Event-ID")
                       or self.headers.get("Last-Event-Id") or "").strip()
            context = self._runtime_context()
            if last_id:
                stream = _lookup_zhi_stream(last_id, scope=context.scope)
                if stream is not None:
                    self._resume_zhi_sse(stream)
                    return
            self._serve_listener_sse()

        def _resume_zhi_sse(self, stream):
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache, no-transform")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
            except (BrokenPipeError, ConnectionError, OSError):
                return
            self.close_connection = False
            log("zhi SSE 按 Last-Event-ID 续流 rpc_id={}".format(stream.rid))
            self._pump_zhi_sse(self.wfile, stream, None, stream.tok)

        def _serve_listener_sse(self):
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache, no-transform")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
            except (BrokenPipeError, ConnectionError, OSError):
                return
            self.close_connection = False
            try:
                if _protocol_at_least(self.headers):
                    self.wfile.write(_sse_prime_frame(_alloc_sse_id("get")))
                else:
                    self.wfile.write(b": listener\r\n\r\n")
                self.wfile.flush()
                while True:
                    time.sleep(15)
                    self.wfile.write(b": keepalive\r\n\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionError, OSError):
                return

        def do_DELETE(self):
            context = self._runtime_context()
            HTTP_RUNTIME_REGISTRY.remove(
                "handler-{}".format(id(self)), getattr(context, "session_id", ""))
            self._reply(200)  # 会话终止

    globals()["McpHttpHandler"] = Handler

    # 不是正主那套安装就别抢 39222：抢到手也只会把用户正在用的那套顶下去，
    # 代价是所有窗口在飞的 zhi 全断、每个待命 agent 白烧一轮（见 instance_owner）
    stand_down, why = instance_owner.should_stand_down(APP_DIR)
    if stand_down:
        log("不启动 MCP 守护进程：{}".format(why))
        return
    try:
        # 独占绑定 + backlog 64：Windows 上默认 SO_REUSEADDR 允许第二个进程劫持端口、
        # 默认 backlog=5 在进程短暂卡顿时让新连接直接被 RST（agent 端 ERR_CONNECTION_REFUSED）
        httpd = ExclusiveThreadingHTTPServer(("127.0.0.1", int(port)), Handler)
    except OSError:
        log(f"HTTP 端口 {port} 已被占用（已有守护进程在跑），本实例退出")
        return
    log("=== MCP HTTP 端点启动 pid={} port={} python={}{} ===".format(
        os.getpid(), port, sys.version.split()[0],
        " 宿主=hub进程内线程" if in_hub else " 宿主=独立守护进程"))
    if _touch_mcp_json_nonce():
        log("已触碰 mcp.json 唤醒各窗口客户端（守护进程上线自报）")
    # 上一条命服务过的对话立刻接着报活：本进程刚被换掉的这几秒里，hub 那边它们
    # 还在重连宽限内，心跳一到就原地复活，用户看不见「一屋子 agent 集体已终止」
    _back = BRIDGE.restore_conv_registry()
    if _back:
        log("捞回上一条命的 {} 个对话进心跳名单：{}".format(
            len(_back), "、".join(sorted(_back)[:12])))
        BRIDGE._ensure_heartbeat()
    if not in_hub:
        _install_crash_forensics()  # 进程级取证；宿主是 hub 时归 hub 自己管
    _cleanup_stale_spawn_lock()
    global _WD_GUARD_STARTED
    with _wd_guard_lock:
        # 内嵌形态下半死重建会再次进入本函数，watchdog 互保线程只准有一条
        if not _WD_GUARD_STARTED:
            _WD_GUARD_STARTED = True
            threading.Thread(target=_watchdog_guard_loop, daemon=True).start()

    def _listener_selfcheck():
        """半死状态根治（07-27 15:03 事故）：accept 循环死了但保姆线程还活着时，
        进程占着尸位却不接客，39222 对外拒连整整 5 分钟。每 30s 自连一次端口，
        连续 3 次失败 = 监听已死：独立进程自杀交由看门狗拉起干净实例；
        hub 内嵌线程则只关掉本 HTTP 实例（绝不能 os._exit 把整个 hub 陪葬），
        hub 的 10s 巡检随即重建一个干净实例。"""
        fails = 0
        while True:
            time.sleep(30)
            try:
                s = socket.create_connection(("127.0.0.1", int(port)), timeout=3)
                s.close()
                fails = 0
            except OSError:
                fails += 1
                log(f"监听自检失败 {fails}/3（本机连 {port} 不通）")
                if fails >= 3:
                    if in_hub:
                        log("监听已死而 hub 进程仍在：关闭本 HTTP 实例，交由 hub 巡检重建")
                        try:
                            httpd.shutdown()
                        except Exception:
                            pass
                        return
                    log("监听已死而进程仍在（半死状态），自杀交由看门狗拉起干净实例")
                    os._exit(1)

    threading.Thread(target=_listener_selfcheck, daemon=True).start()
    # accept 循环兜底：Windows 上 backlog 里的连接被 RST 时 accept() 可能抛
    # OSError（WSAECONNABORTED 等），serve_forever 跑在主线程、一炸整个监听就没了
    # （但保姆/心跳线程还活着 → 半死）。15:03:11 用户点「重连MCP」触发全窗口重连
    # 风暴后 39222 无声死亡，正是此形态。异常就地记录并重进循环，监听永不无声消失。
    while True:
        try:
            httpd.serve_forever()
            log("serve_forever 正常返回（shutdown 被调用），HTTP 服务结束")
            break
        except Exception as e:
            log(f"HTTP accept 循环异常（已自动重启循环）: {e!r}")
            time.sleep(1)


if __name__ == "__main__":
    if "--http" in sys.argv:
        # 只有守护进程这条路能让位给常驻区（理由见 hub.py 入口处）。
        # **stdio 那条路绝不能让**：它的 stdin/stdout 就是 MCP 的传输通道本身，
        # 而让位是拉一个脱离了这两个管道的子进程——客户端会永远等不到回应，
        # 且看不出是谁的错。本机走的是 http://127.0.0.1:39222/mcp，不受影响；
        # 还在用 stdio 接入的同事，行为与今天一字不差。
        if live_runtime.hand_over("server.py", sys.argv[1:]):
            sys.exit(0)
        try:
            os.chdir(APP_DIR)
        except OSError:
            pass
        try:
            _port = int(sys.argv[sys.argv.index("--http") + 1])
        except (ValueError, IndexError):
            _port = 39222
        serve_http(_port)
    else:
        main()
