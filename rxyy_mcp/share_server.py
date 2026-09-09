# -*- coding: utf-8 -*-
"""局域网会话分享 HTTP 服务

在 hub 内启动，绑定 0.0.0.0:<share_port>。两种入口：
1. 总控链接  http://<IP>:<端口>/?t=<总令牌>          —— 看到所有会话 tab
2. 单会话直连 http://<IP>:<端口>/s/<会话ID>?t=<会话令牌> —— 只能看到/回复这一个会话

会话令牌由总令牌 HMAC 派生，拿到直连链接的人无法访问其他会话。
分享开关关闭时全部拒绝。
"""
import gzip
import hashlib
import hmac
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from ipc import ExclusiveThreadingHTTPServer

APP_DIR = Path(__file__).resolve().parent
SHARE_PAGE = APP_DIR / "share.html"
MAX_BODY = 64 * 1024 * 1024  # 回复可携带粘贴图片(base64)，给足余量

DENY_PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<title>rxyy MCP 分享</title></head>
<body style="background:#101014;color:#9ca3af;font:14px 'Microsoft YaHei UI',sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
<div style="text-align:center"><div style="font-size:17px;color:#e5e7eb">分享未开启或链接无效</div>
<div style="margin-top:8px">请向发起人索取最新的分享链接</div></div></body></html>"""


def _rank_lan_ip(ip):
    """给候选 IP 排个优先级：越像「同事真能连上的公司网段」越大。"""
    if ip.startswith("192.168."):
        return 3
    if ip.startswith("10."):
        return 2
    try:  # 172.16-31.* 也是私网，但 Docker/WSL/加速器的虚拟网卡最爱占这一段
        a, b = ip.split(".")[:2]
        if a == "172" and 16 <= int(b) <= 31:
            return 1
    except ValueError:
        pass
    return 0


def lan_ip():
    """探测本机局域网 IP。

    别只信「连 8.8.8.8 时系统选了哪个源地址」：雷神这类加速器会往路由表里插
    `8.0.0.0/5`，默认出口整个被吸进虚拟网卡，探出来的是 172.21.0.2 这种同事根本
    连不上的地址（07-31 实测，分享链接和任务投递链接全废）。所以把所有网卡的
    IPv4 都列出来，按「像不像公司局域网」排序，默认出口只作为同分时的加分项。
    """
    probed = ""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            probed = s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        pass
    cands = []
    try:
        for res in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = res[4][0]
            if ip and not ip.startswith(("127.", "169.254.")) and ip not in cands:
                cands.append(ip)
    except OSError:
        pass
    if probed and probed not in cands:
        cands.append(probed)
    if not cands:
        return probed or "127.0.0.1"
    return max(cands, key=lambda ip: (_rank_lan_ip(ip), ip == probed))


# ---- 局域网优先：同一个办公网来的访客，别让他绕一圈公网 ----
# 发出去的只有一条公网链接（同事在哪都打得开），但办公室里的人点开时，
# 走公网等于「办公室→Cloudflare→办公室」：传附件慢，外网一断入口就废。
# 判据取「访客的公网出口 IP == 本机的公网出口 IP」——同一个出口基本就是同一
# 张办公网，那就 302 送去局域网地址。判错也不出事：不跳而已。
#
# 坑（09-01 实测）：HTTPS 公网 → HTTP 局域网是降级跳转，浏览器不带 Referer；
# 地址栏被换成 192.168.x.x 之后，手机切 4G / 人离开这张网，页面还在轮询死地址。
# /s/<sid> 已不再走局域网优先（同事拿到的就是公网链接，跳了会落到家机/公司机
# 各自的局域网 IP，两台抢隧道时进错机）。/ 和 /tasks 仍跳；跳转 URL 带 pub=
# 给死链切回；share.html 注入 SHARE_PUBLIC_BASE 兜剪贴板里的局域网地址。
_EGRESS = {"ip": "", "ts": 0.0}
_EGRESS_TTL = 3600.0
_EGRESS_URLS = ("https://api.ipify.org", "https://ifconfig.me/ip", "https://ipinfo.io/ip")
# 只有「页面」入口参与跳转：接口一跳就把 fetch 打断了。
# /s/<sid> 单会话分享故意不跳——发给同事的是 console.example.invalid 公网链接，
# 跳到局域网会换成 192.168.x.x：家机/公司机各跳各的 IP，两台都跑隧道时
# 还会落到没这个会话的那台（09-01 截图：公网链接变成 192.168.20.52 空白页）。
LAN_FIRST_PATHS = ("/", "/tasks")


def _is_ipv4(text):
    parts = str(text or "").split(".")
    return len(parts) == 4 and all(
        p.isdigit() and len(p) <= 3 and 0 <= int(p) <= 255 for p in parts)


def _probe_egress_ip(timeout=5):
    for url in _EGRESS_URLS:
        try:
            # 默认 UA 常被回声服务和 WAF 拦掉，报个大众脸
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                ip = resp.read(64).decode("utf-8", "ignore").strip()
            if _is_ipv4(ip):
                return ip
        except Exception:  # noqa: BLE001
            continue
    return ""


def refresh_egress_ip(force=False):
    """刷新本机出口公网 IP。绝不在请求线程里探测——公网回声慢起来好几秒，
    同事点开链接却在那转圈。后台线程定时刷，请求只读缓存。"""
    now = time.time()
    if not force and _EGRESS["ip"] and now - _EGRESS["ts"] < _EGRESS_TTL:
        return _EGRESS["ip"]
    ip = _probe_egress_ip()
    # 回声服务偶尔返回错误页/IPv6：脏值进了缓存就成了判据，宁可这轮不刷
    if _is_ipv4(ip):
        _EGRESS["ip"], _EGRESS["ts"] = ip, now
    return _EGRESS["ip"]


def office_egress_ip(cfg):
    """办公网出口 IP：配置里手填的优先（多出口/NAT 池只有本人清楚），否则用探测值。"""
    return str(cfg.get("office_egress_ip") or "").strip() or _EGRESS["ip"]


def lan_first_target(cfg, fwd_ip, cookie, path, query, port, lan=None):
    """该把这次访问送去局域网的话，返回目标地址；不该送就返回空串。

    只处理页面导航（见 LAN_FIRST_PATHS）：接口和图片不跳，免得 302 打断 fetch。
    """
    if not bool(cfg.get("lan_first", True)):
        return ""
    qs = parse_qs(query or "")
    if (qs.get("nolan") or [""])[0] == "1":
        return ""
    fwd = str(fwd_ip or "").strip()
    mine = office_egress_ip(cfg)
    if not fwd or not mine or fwd != mine:
        return ""
    # 跳过去打不开的人（办公室访客网段够不着办公机）会自己退回来点原链接，
    # 这时别再把他弹走一次——弹两回就成了死循环，他只会以为「这链接是坏的」
    if "lan_tried=1" in str(cookie or ""):
        return ""
    # lan=None 才去探；显式传空串表示「这机器压根没有能用的局域网地址」
    ip = lan_ip() if lan is None else lan
    if not ip or ip.startswith(("127.", "169.254.")):
        return ""
    tail = lan_redirect_query(query, cfg)
    return "http://{}:{}{}?{}".format(ip, int(port), path, tail)


def public_share_base(cfg):
    """分享页死在局域网上时该回到哪条 https 基址。非 https 的远程基址不算。"""
    https = str((cfg or {}).get("share_https_base") or "").strip().rstrip("/")
    if https.lower().startswith("https://"):
        return https
    return ""


def lan_redirect_query(query, cfg):
    """局域网跳转后的 query：nolan=1 防回流，pub= 给页面死链时切回公网。"""
    tail = (query + "&" if query else "") + "nolan=1"
    base = public_share_base(cfg)
    if base:
        tail += "&pub=" + quote(base, safe="")
    return tail


_SHARE_BASE_MARK = b'<script src="/highlight.min.js"'


def with_share_public_base(html, cfg):
    """往 share.html 注入公网基址。剪贴板局域网链接没有 pub= 参数，只能靠这个。"""
    base = public_share_base(cfg)
    if not base or not isinstance(html, (bytes, bytearray)):
        return html
    if _SHARE_BASE_MARK not in html:
        return html
    snippet = ("<script>window.SHARE_PUBLIC_BASE=%s;</script>\n"
               % json.dumps(base)).encode("utf-8")
    return html.replace(_SHARE_BASE_MARK, snippet + _SHARE_BASE_MARK, 1)


def _base(cfg):
    """https 基址最优先（Tailscale Serve；手机麦克风/语音输入只认安全上下文），
    其次远程基址（Tailscale/公网 http），否则用探测到的局域网 IP。"""
    https = (cfg.get("share_https_base") or "").strip().rstrip("/")
    if https:
        return https
    remote = (cfg.get("remote_base_url") or "").strip().rstrip("/")
    if remote:
        return remote
    return "http://{}:{}".format(lan_ip(), int(cfg.get("share_port", 39080)))


def share_url(cfg):
    return "{}/?t={}".format(_base(cfg), cfg.get("share_token", ""))


def _bases(cfg):
    """返回 [(标签, 基址)]：https（可用语音输入）排最前，局域网始终给出，
    配置了远程基址（Tailscale http）再补一条。"""
    lan = "http://{}:{}".format(lan_ip(), int(cfg.get("share_port", 39080)))
    out = []
    https = (cfg.get("share_https_base") or "").strip().rstrip("/")
    if https:
        out.append(("远程https·支持语音", https))
    out.append(("局域网", lan))
    remote = (cfg.get("remote_base_url") or "").strip().rstrip("/")
    if remote and remote != lan:
        out.append(("Tailscale/远程", remote))
    return out


def share_urls(cfg):
    t = cfg.get("share_token", "")
    return [{"label": lb, "url": "{}/?t={}".format(b, t)} for lb, b in _bases(cfg)]


def session_share_urls(cfg, sid):
    t = session_token(cfg, sid)
    return [{"label": lb, "url": "{}/s/{}?t={}".format(b, sid, t)} for lb, b in _bases(cfg)]


def intake_token(cfg):
    """任务投递令牌：由总令牌派生，只能进任务入站页与提交接口。

    发给同事的链接绝不能带总令牌——总令牌同时打开 `/`，等于把所有 AI 会话的
    内容和替你回复的权限一起送出去。总令牌一换，投递链接自动失效。"""
    key = str(cfg.get("share_token") or "").encode("utf-8")
    return hmac.new(key, b"task-intake", hashlib.sha256).hexdigest()[:12]


def intake_urls(cfg):
    k = intake_token(cfg)
    return [{"label": lb, "url": "{}/tasks?k={}".format(b, k)} for lb, b in _bases(cfg)]


def session_token(cfg, sid):
    """单会话令牌：由总令牌派生，只对该会话有效"""
    key = str(cfg.get("share_token") or "").encode("utf-8")
    return hmac.new(key, str(sid).encode("utf-8"), hashlib.sha256).hexdigest()[:12]


def session_share_url(cfg, sid):
    return "{}/s/{}?t={}".format(_base(cfg), sid, session_token(cfg, sid))


def owner_session_url(cfg, sid):
    """自己手机上打开某个会话：完整页面（总令牌）+ 直接选中它。

    以前推送点开的是 /s/<sid> 单会话链接，那个令牌按设计只对这一个会话有效——
    于是页面上的「全部对话」「团队」一点就 403（07-31 用户实测，界面只报「链接
    已失效」，看着像坏了）。发给同事的仍旧用单会话链接，自己的推送用这一条。
    """
    return "{}/?t={}&sid={}".format(_base(cfg), cfg.get("share_token", ""), sid)


# ---- 任务安排站（手机/局域网入站）：与 rxyy tools 桌面控制台共用同一任务库 ----
TASKS_PAGE = APP_DIR / "tasks.html"
_TASK_STORAGE = None
_TASK_STORAGE_RETRY_AT = 0.0


def _console_root():
    """rxyy tools 那一侧（`data\\`、`console\\`、`scripts\\`）的根目录。

    三种住法：打包版在 exe 旁、源码态在仓库根、**常驻区哪个都不是**。常驻区住在
    `%LOCALAPPDATA%\\rxyy-tools-community\\live\\`，从它自己的位置推不出 rxyy tools 在哪，
    所以物化时由 build.ps1 记一笔，这里优先照它走（见 live_runtime.console_root）。
    """
    try:
        import live_runtime
        recorded = live_runtime.console_root()
    except Exception:  # noqa: BLE001  老包里没有这个模块
        recorded = None
    if recorded:
        return recorded
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return APP_DIR.parent


def _console_data_dir():
    """复刻 console/src.config._data_dir 的解析顺序，让任务库落点与控制台完全一致：
    环境变量 RXYY_DATA_DIR → APP_ROOT/data-dir.txt 指针 →
    APP_ROOT/dist/rxyy-tools-community/data（存在才认）→ APP_ROOT/data。

    07-29 的坑：控制台读 data-dir.txt 指过去的 dist\\data，本模块却只按 APP_DIR
    硬推，源码态下算成仓库根的 data\\，两边不同处——同事投的需求成了桌面看不见的
    孤儿。必须和控制台走同一套解析，指针改一处两边一起动。"""
    raw = (os.getenv("RXYY_DATA_DIR") or "").strip()
    if raw and Path(raw).is_dir():
        return Path(raw)
    app_root = _console_root()
    try:
        # utf-8-sig：指针可能由 PowerShell 写出，Windows 会带 BOM。
        # UnicodeDecodeError 也要吞：公司透明加密盘同步过来的 data-dir.txt 在家机上是
        # 密文（`%TSD-Header-###%…`），读出来不是 utf-8。它不是 OSError，漏掉这一档
        # 会把异常抛给 _task_storage() 的兜底，任务库直接判成不可用。
        for line in (app_root / "data-dir.txt").read_text(encoding="utf-8-sig").splitlines():
            line = line.strip().strip('"')
            if line and not line.startswith("#") and Path(line).is_dir():
                return Path(line)
    except (OSError, UnicodeDecodeError):
        pass
    # 08-27 转告事故的加固：指针缺失/整行都是外机路径时，别急着另开仓库根 data\ ——
    # 打包控制台读的是 <console_root>\dist\rxyy-tools-community\data\，它存在就跟它走（与
    # src/config.py::_data_dir 的「源码态兑底」同款）。当天 data-dir.txt 只写了家机
    # 路径，本函数落到仓库根 data\、控制台落到 dist 库，hub 与控制台各写一份任务库：
    # 发给 查无名录、完成任务 找不到编号、同事投的任务成孤儿。指针再丢也不分家。
    bundled = app_root / "dist" / "rxyy-tools-community" / "data"
    if bundled.is_dir():
        return bundled
    return app_root / "data"


def _taskstage_dir():
    """任务库目录，必须与 rxyy tools 控制台的 src.config.DATA_DIR 指同一处。

    首选：与控制台同一套 data-dir 解析（env → data-dir.txt → APP_ROOT/data）。
    兜底：万一指针缺失/被拷到别的机器路径失效，再按已存在任务库的候选认一个
    （源码 hub + 打包控制台混跑时也能对上）。"""
    primary = _console_data_dir() / "taskstage"
    if (primary / ".task_stage.json").is_file():
        return primary
    # 指针指到的 data\ 里有 workflow.db，就说明这确实是控制台那份数据目录，只是任务库
    # 还没建过（第一条入站才落盘）。这时绝不能再往下认别处的旧库——那正是 07-29 那个
    # 坑的反面：同事投的需求会落进一个控制台根本不读的孤儿目录。
    if (primary.parent / "workflow.db").is_file():
        return primary
    candidates = [primary]
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.append(exe_dir / "data" / "taskstage")
    candidates.append(_console_root() / "data" / "taskstage")
    candidates.append(APP_DIR.parent / "data" / "taskstage")
    candidates.append(APP_DIR.parent.parent / "data" / "taskstage")
    for cand in candidates:
        if (cand / ".task_stage.json").is_file():
            return cand
    return primary


def _task_storage():
    """惰性加载桌面任务安排站的 TaskStageStorage，与控制台读写同一份任务库
    （原子写，跨进程安全）。直接按文件路径加载 console 的 storage.py，
    避免引入 console 包的任何副作用。"""
    global _TASK_STORAGE, _TASK_STORAGE_RETRY_AT
    if _TASK_STORAGE is not None:
        return _TASK_STORAGE
    # 加载失败不能记一辈子。8-07 16:26 切常驻区那趟就栽在这上面：hub 起来的头
    # 几秒 console-root.txt 还没落地，第一次请求没加载成，之后整整半小时投递站
    # 都回「任务库不可用」，直到 16:50 有人重启才好——而那期间同事提的需求进不来。
    # 隔 30 秒再试一次，代价不过是偶尔多一次 import。
    now = time.monotonic()
    if now < _TASK_STORAGE_RETRY_AT:
        return None
    _TASK_STORAGE_RETRY_AT = now + 30
    try:
        import importlib.util
        # 常驻区跑的是系统 Python，不是那个 exe——「从 exe 内部归档 import」那条
        # 兜底在它这儿一定失败，所以这里必须能找到真的 storage.py（08-07 16:26
        # 切过去后投递站当场读不到任务库，就是这一句按 APP_DIR.parent 找空了）
        storage_py = _console_root() / "console" / "api" / "taskstage" / "storage.py"
        if not storage_py.is_file():
            storage_py = APP_DIR.parent / "console" / "api" / "taskstage" / "storage.py"
        if storage_py.is_file():
            spec = importlib.util.spec_from_file_location("_ts_share_storage", storage_py)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        else:
            # 打包版没有 console 源码文件，模块被 PyInstaller 收进 exe 内部归档
            import importlib
            mod = importlib.import_module("api.taskstage.storage")
        _TASK_STORAGE = mod.TaskStageStorage(_taskstage_dir())
    except Exception:
        _TASK_STORAGE = None
    return _TASK_STORAGE


def _task_rev():
    """任务库的版本号：库文件的 (改动时间, 字节数)。

    任何一次增删改都会重写这个文件，所以这两个数一变就说明有新东西；不解析
    JSON、不碰截图，代价只是一次 stat。取不到就回空串，页面收到空串会退回到
    「切回页面时才刷」的老节奏，而不是把它当成「没变化」。
    """
    try:
        st = (_taskstage_dir() / ".task_stage.json").stat()
    except OSError:
        return ""
    return "{:.6f}-{}".format(st.st_mtime, st.st_size)


def _task_public(storage, task, with_images=True):
    """把内部任务转成手机端要显示的精简结构（截图转 data_uri）。"""
    images = []
    if with_images:
        for im in task.get("images") or []:
            uri = storage.image_data_uri(im.get("file"))
            if uri:
                images.append({"name": im.get("name"), "data_uri": uri})
    files = [{"name": f.get("name"), "size": int(f.get("size") or 0)}
             for f in (task.get("files") or [])]
    return {
        "id": task.get("id"),
        "title": task.get("title"),
        "description": task.get("description"),
        "project": task.get("project"),
        "repo": task.get("repo"),
        "priority": task.get("priority"),
        "status": task.get("status"),
        "tags": task.get("tags") or [],
        "created_at": task.get("created_at"),
        "dispatched_at": task.get("dispatched_at"),
        "session_name": task.get("session_name"),
        "image_count": len(task.get("images") or []),
        "images": images,
        "files": files,
    }


def _handle_oa_daily_submit(payload):
    """手机点「提交到 OA」：起 rxyy tools 的日报任务脚本去真提交。

    OA 在公司内网，家机够不着，提交那一跳由脚本经 SSH 交给公司机——分享服务只负责
    把这一下转过去，不碰 OA 逻辑。脚本自己会把结果再推回手机，所以这里不等它跑完
    （提交要十几秒，通知栏的按钮等不了那么久）。
    """
    script = None
    for cand in (_console_root() / "scripts" / "oa_daily_job.py",          # 记下的根
                 APP_DIR.parent / "scripts" / "oa_daily_job.py",           # 源码态
                 # 打包态：exe 在 <仓库>\dist\rxyy-tools-community\，脚本没进包，回仓库里找
                 Path(sys.executable).resolve().parent.parent.parent / "scripts" / "oa_daily_job.py"):
        if cand.is_file():
            script = cand
            break
    if script is None:
        return {"ok": False, "error": "找不到日报任务脚本 scripts/oa_daily_job.py"}
    date = str(payload.get("date") or "").strip()
    launcher = shutil.which("py")
    cmd = ([launcher, "-3.11", str(script)] if launcher
           else [sys.executable, str(script)]) + ["--submit"]
    if date:
        cmd += ["--date", date]
    try:
        subprocess.Popen(cmd, cwd=str(script.parent.parent),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "起提交脚本失败：{}".format(e)}
    return {"ok": True, "message": "已开始提交，结果会再推一条到手机"}


def _save_task_assets(storage, payload, existing_images=None, existing_files=None):
    """把入站请求里的图/文件落到任务库。失败回 {ok:False, error}。"""
    images = list(existing_images or [])
    files = list(existing_files or [])
    added_images = []
    added_files = []
    img_room = max(0, 8 - len(images))
    file_room = max(0, 5 - len(files))
    for im in (payload.get("images") or [])[:img_room]:
        if not isinstance(im, dict) or not im.get("b64"):
            continue
        try:
            saved = storage.save_image_file(
                im.get("b64"), im.get("mime"), im.get("name"))
        except Exception:
            continue
        images.append(saved)
        added_images.append(saved)
    for fi in (payload.get("files") or [])[:file_room]:
        if not isinstance(fi, dict) or not fi.get("b64"):
            continue
        try:
            saved = storage.save_attachment_file(
                fi.get("b64"), fi.get("name"), fi.get("mime"))
        except ValueError as e:
            for done in added_files:
                storage.delete_attachment_file(done.get("file"))
            for done in added_images:
                storage.delete_image_file(done.get("file"))
            return {"ok": False, "error": "附件「{}」{}".format(
                str(fi.get("name") or "未命名"), e)}
        except Exception:
            return {"ok": False, "error": "附件保存失败，请重试或改小文件"}
        files.append(saved)
        added_files.append(saved)
    return {
        "ok": True,
        "images": images,
        "files": files,
        "added_images": added_images,
        "added_files": added_files,
    }


def _handle_task_add(storage, payload):
    """手机/局域网入站新增任务：落到与桌面共用的任务库，桌面立即可见。"""
    title = str(payload.get("title") or "").strip()
    if not title:
        return {"ok": False, "error": "请填写任务标题"}
    assets = _save_task_assets(storage, payload)
    if not assets.get("ok"):
        return assets
    tags = payload.get("tags") or []
    if isinstance(tags, str):
        tags = [x.strip() for x in tags.replace("，", ",").split(",") if x.strip()]
    tags = ["远程入站"] + [str(t) for t in tags if str(t).strip() and str(t) != "远程入站"]
    who = str(payload.get("who") or "").strip()[:16]
    desc = str(payload.get("description") or "").strip()
    if who:
        desc = (desc + "\n\n（入站人：{}）".format(who)).strip()
    try:
        task = storage.add_task({
            "title": title,
            "description": desc,
            "project": payload.get("project"),
            "repo": payload.get("repo"),
            "requester": who,
            "priority": payload.get("priority") or "normal",
            "tags": tags,
            "images": assets["images"],
            "files": assets["files"],
        })
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "保存失败：{}".format(e)}
    return {"ok": True, "task": {"id": task.get("id"), "title": task.get("title")}}


def _task_mine_public(task):
    """投递人查自己那几条：只给进度，不内联截图、不给别人的正文。"""
    return {
        "id": task.get("id"),
        "title": task.get("title"),
        "status": task.get("status") or "draft",
        "priority": task.get("priority") or "normal",
        "project": task.get("project") or "",
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
        "session_name": task.get("session_name") or "",
        "image_count": len(task.get("images") or []),
        "files": [{"name": f.get("name")} for f in (task.get("files") or [])],
    }


def _handle_task_mine(storage, payload):
    """按 id 列表回自己提交过的任务进度。投递令牌拿不到别人的任务。"""
    raw = payload.get("ids") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        raw = []
    wanted = []
    seen = set()
    for item in raw:
        tid = str(item or "").strip()
        if not tid or tid in seen:
            continue
        seen.add(tid)
        wanted.append(tid)
        if len(wanted) >= 40:
            break
    by_id = {t["id"]: t for t in storage.list_tasks()}
    tasks = [_task_mine_public(by_id[tid]) for tid in wanted if tid in by_id]
    return {"ok": True, "tasks": tasks}


def _hub_files_of(storage, items):
    import base64
    out = []
    for item in items or []:
        path = storage.attachment_path(item.get("file"))
        if path is None:
            continue
        try:
            binary = path.read_bytes()
        except OSError:
            continue
        out.append({
            "name": item.get("name") or path.name,
            "data": "data:application/octet-stream;base64," +
                    base64.b64encode(binary).decode("ascii"),
        })
    return out


def _hub_images_of(storage, items):
    out = []
    for item in items or []:
        uri = storage.image_data_uri(item.get("file"))
        if uri:
            out.append({"data": uri, "filename": item.get("name") or item.get("file")})
    return out


def _handle_task_patch(storage, payload, hub_api=None):
    """入站人给已提交的任务追加说明 / 图 / 文件。处理中的会再塞一条给会话。

    已处理/已归档的任务一补充就是「打回重开」（08-31 江平：复测发现问题时
    任务恰恰已经是已处理，原来只能干瞪眼）：状态回到已提交重新排队，卡上留
    【重开】标记和「重开」标签，上一轮的派发凭据全部清掉——那个会话早就不在
    干这条了，留着只会让撤回/转告对着一个空号操作。
    """
    task_id = str((payload or {}).get("id") or "").strip()
    task = storage.find(task_id) if task_id else None
    if task is None:
        return {"ok": False, "error": "任务不存在或已被删除"}
    reopen = str(task.get("status") or "") in ("done", "archived")
    extra_desc = str(payload.get("description") or "").strip()
    who = str(payload.get("who") or "").strip()[:16]
    assets = _save_task_assets(
        storage, payload, task.get("images") or [], task.get("files") or [])
    if not assets.get("ok"):
        return assets
    added_images = assets.get("added_images") or []
    added_files = assets.get("added_files") or []
    if not extra_desc and not added_images and not added_files:
        return {"ok": False, "error": "请填写补充内容或附上图片/文件"}
    fields = {
        "images": assets["images"],
        "files": assets["files"],
    }
    marker = "【重开】" if reopen else "【补充】"
    # 重开时哪怕一个字没写（只丢了张复测截图），卡面上也得看得出为什么回来了
    body = extra_desc or ("复测仍有问题，见新附件" if reopen else "")
    if body:
        suffix = body
        if who:
            suffix = body + "\n（补充人：{}）".format(who)
        fields["description"] = (
            (str(task.get("description") or "").strip() + "\n\n" + marker + suffix).strip()
        )
    if reopen:
        fields.update({
            "status": "draft",
            "reopened_at": time.time(),
            "tags": list(task.get("tags") or []) + ["重开"],
            "dispatched_at": 0.0,
            "session_name": "",
            "dispatch_session_id": "",
            "dispatch_conv_key": "",
            "dispatch_qid": "",
            "dispatch_direct": False,
            "dispatch_batch": "",
        })
    try:
        updated = storage.update_task(task_id, fields)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "保存失败：{}".format(e)}
    forwarded = False
    if (updated and updated.get("status") == "dispatched"
            and updated.get("dispatch_session_id")
            and hub_api is not None and hasattr(hub_api, "queue_message")):
        sid = updated["dispatch_session_id"]
        text = "【任务补充】「{}」\n{}".format(
            updated.get("title") or "",
            extra_desc or "（无文字，见附件）")
        if who:
            text += "\n（入站人补充：{}）".format(who)
        try:
            hub_api.queue_message(
                sid, text,
                _hub_images_of(storage, added_images),
                "任务安排站",
                _hub_files_of(storage, added_files),
            )
            forwarded = True
        except Exception:
            forwarded = False
    return {
        "ok": True,
        "task": {
            "id": (updated or task).get("id"),
            "title": (updated or task).get("title"),
            "status": (updated or task).get("status") or "draft",
        },
        "forwarded": forwarded,
        "reopened": reopen,
    }


def start_share_server(hub, api):
    """启动分享服务线程；端口被占用时返回 None（不影响控制台本体）"""
    port = int(hub.cfg.get("share_port", 39080))

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _query_token(self):
            qs = parse_qs(urlparse(self.path).query)
            return (qs.get("t") or [""])[0] or self.headers.get("X-Share-Token", "")

        def _intake_ok(self):
            """投递专用令牌（?k=）：只认任务入站页与提交接口，进不了任何会话。"""
            if not bool(hub.cfg.get("share_enabled", True)):
                return False
            want = intake_token(hub.cfg)
            qs = parse_qs(urlparse(self.path).query)
            got = str((qs.get("k") or [""])[0] or self.headers.get("X-Intake-Token", ""))
            return bool(want) and hmac.compare_digest(got, want)

        def _task_ok(self):
            """任务库入口：总令牌（自己用）或投递令牌（发给同事）都放行。"""
            return self._scope() == "full" or self._intake_ok()

        def _scope(self, sid_hint=None):
            """鉴权：返回 'full'（总令牌）、会话ID（单会话令牌）或 None（拒绝）"""
            if not bool(hub.cfg.get("share_enabled", True)):
                return None
            want = str(hub.cfg.get("share_token") or "")
            tok = str(self._query_token())
            if not want:
                return None
            if hmac.compare_digest(tok, want):
                return "full"
            if sid_hint and hmac.compare_digest(tok, session_token(hub.cfg, sid_hint)):
                return sid_hint
            return None

        # 手机走隧道/4G 时带宽才是瓶颈：20 多个会话的 /api/state 每拍 20KB，
        # 一条长对话的 /api/messages 30KB+，压完都只剩一到两成。小响应不压
        # （头部开销加 CPU 反而不划算），图片本身已是压缩格式，压了纯浪费。
        GZIP_MIN = 1400

        def _send_unless_same(self, body, known):
            """内容跟客户端手里那份一样就只回一句「没变」。

            压缩解决的是「这一包多大」，解决不了「绝大多数拍其实什么都没发生」。
            /api/state 待机时也一两万字节（压完一两千），可手机揣兜里那几百拍
            里真有变化的没几拍。带上摘要一比，没变的拍缩到几十字节，待机流量
            又降一个数量级；页面收到「没变」连重绘都省了。

            摘要按序列化后的字节算，改一个字符都躲不过去，所以不会漏更新。
            """
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            etag = hashlib.sha1(data).hexdigest()[:16]
            if known and known == etag:
                self._send(200, {"ok": True, "same": True, "etag": etag})
                return
            body = dict(body)
            body["etag"] = etag
            self._send(200, body)

        def _send(self, code, body, ctype="application/json; charset=utf-8", cache=None):
            data = body if isinstance(body, bytes) else json.dumps(
                body, ensure_ascii=False).encode("utf-8")
            gz = (len(data) >= self.GZIP_MIN and not ctype.startswith("image/")
                  and "gzip" in (self.headers.get("Accept-Encoding") or "").lower())
            if gz:
                try:
                    data = gzip.compress(data, 6)
                except Exception:  # noqa: BLE001
                    gz = False
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if gz:
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", cache or "no-store")
            # 允许外部页面（如产品标注工具）跨域调用；接口本身有令牌鉴权
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            try:
                self.wfile.write(data)
            except Exception:
                pass

        def _send_page(self):
            try:
                self._send(200, with_share_public_base(SHARE_PAGE.read_bytes(), hub.cfg),
                           "text/html; charset=utf-8")
            except Exception:
                self._send(500, {"ok": False, "error": "share.html 缺失"})

        _IMG_CT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                   ".gif": "image/gif", ".webp": "image/webp"}

        def _send_image(self, raw_path):
            """安全返回记录目录 图片/ 子目录内的图片：resolve 后必须落在该目录下，
            扩展名受限，否则一律 404（防越权读任意文件）。"""
            try:
                if not raw_path:
                    self._send(404, {"ok": False, "error": "缺少 path"})
                    return
                img_root = (Path(hub.cfg["history_dir"]) / "图片").resolve()
                p = Path(raw_path).resolve()
                if img_root not in p.parents or p.suffix.lower() not in self._IMG_CT:
                    self._send(404, {"ok": False, "error": "图片不存在或不在允许目录"})
                    return
                self._send(200, p.read_bytes(), self._IMG_CT[p.suffix.lower()])
            except Exception:
                self._send(404, {"ok": False, "error": "图片读取失败"})

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Share-Token")
            self.send_header("Access-Control-Max-Age", "86400")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _lan_first(self, parsed):
            """经隧道/反代进来的才带转发头；局域网直连本来就没绕路，无须再跳。"""
            fwd = (self.headers.get("CF-Connecting-IP")
                   or (self.headers.get("X-Forwarded-For") or "").split(",")[0])
            return lan_first_target(hub.cfg, fwd, self.headers.get("Cookie"),
                                    parsed.path, parsed.query, port)

        def _redirect_lan(self, target):
            self.send_response(302)
            self.send_header("Location", target)
            # 这一跳只试一次：LAN 打不开的人退回来点原链接时不再被弹走
            self.send_header("Set-Cookie", "lan_tried=1; Max-Age=900; Path=/; SameSite=Lax")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)
            sid_q = (qs.get("sid") or [""])[0]

            if path in LAN_FIRST_PATHS:
                target = self._lan_first(parsed)
                if target:
                    self._redirect_lan(target)
                    return

            if path == "/":
                if self._scope() == "full":
                    self._send_page()
                else:
                    self._send(403, DENY_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif path.startswith("/s/"):
                sid = path[3:].split("/")[0]
                if self._scope(sid) is not None:
                    self._send_page()
                else:
                    self._send(403, DENY_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif path in ("/model_picker.js", "/transfer_dialog.js", "/native_questions.js"):
                try:
                    self._send(200, (APP_DIR / path.lstrip("/")).read_bytes(),
                               "application/javascript; charset=utf-8")
                except OSError:
                    self._send(404, {"ok": False, "error": "model_picker.js 缺失"})
            elif path == "/highlight.min.js":
                # 代码高亮库（无敏感内容，公开静态资源；页面本身仍有令牌门槛）。
                # 130KB 且从不变，却跟着接口一起 no-store，每次开页面都重下一遍——
                # 手机上开屏白等一两秒就耗在这里，给它一周的长缓存。
                try:
                    self._send(200, (APP_DIR / "highlight.min.js").read_bytes(),
                               "application/javascript; charset=utf-8",
                               cache="public, max-age=604800, immutable")
                except OSError:
                    self._send(404, {"ok": False, "error": "highlight.min.js 缺失"})
            elif path == "/api/state":
                scope = self._scope(sid_q)
                if scope is None:
                    self._send(403, {"ok": False, "error": "分享未开启或令牌无效"})
                    return
                st = api.get_state()
                sessions = st["sessions"]
                if scope != "full":
                    sessions = [x for x in sessions if x["id"] == scope]
                self._send_unless_same({
                    "sessions": sessions,
                    "continue_prompt": hub.cfg.get("continue_prompt", "请按照最佳实践继续"),
                    "quick_phrases": [str(p) for p in (hub.cfg.get("quick_phrases") or [])
                                      if str(p).strip()][:12],
                }, (qs.get("known") or [""])[0])
            elif path == "/api/messages":
                scope = self._scope(sid_q)
                if scope is None or (scope != "full" and scope != sid_q):
                    self._send(403, {"ok": False, "error": "分享未开启或令牌无效"})
                    return
                self._send(200, api.get_messages(sid_q))
            elif path == "/api/live_turn":
                # 手机上也看 agent 在 Cursor 里干到哪一步了（读库还原的时间线，与控制台同源）；
                # 权限口径同 /api/messages：单会话令牌只能看自己那个
                scope = self._scope(sid_q)
                if scope is None or (scope != "full" and scope != sid_q):
                    self._send(403, {"ok": False, "error": "分享未开启或令牌无效"})
                    return
                try:
                    max_steps = max(5, min(int((qs.get("max_steps") or ["100"])[0]), 2000))
                except (TypeError, ValueError):
                    max_steps = 100
                self._send(200, api.get_live_turn(sid_q, max_steps=max_steps))
            elif path == "/api/team":
                # 团队面板搬到手机：只认总令牌（它能看到全部项目的所有 agent）
                if self._scope() != "full":
                    self._send(403, {"ok": False, "error": "需要总令牌"})
                    return
                self._send(200, api.team_state())
            elif path == "/api/team/seat_prompt":
                if self._scope() != "full":
                    self._send(403, {"ok": False, "error": "需要总令牌"})
                    return
                self._send(200, api.team_seat_prompt((qs.get("seat") or [""])[0]))
            elif path == "/api/takeover_prompt":
                # 手机上要能把接手提示词复制走；只认总令牌（单会话令牌不该看到别的会话）
                if self._scope() != "full":
                    self._send(403, {"ok": False, "error": "需要总令牌"})
                    return
                self._send(200, api.get_takeover_prompt(sid_q))
            elif path == "/api/ext_instances":
                # 装了窗口总线扩展的 Cursor 窗口：手机派接手时可选「新开对话」；只认总令牌
                if self._scope() != "full":
                    self._send(403, {"ok": False, "error": "需要总令牌"})
                    return
                self._send(200, api.ext_instances())
            elif path == "/api/models":
                if self._scope() != "full":
                    self._send(403, {"ok": False, "error": "需要总令牌"})
                    return
                self._send(200, api.list_known_models())
            elif path == "/api/native_models":
                if self._scope() != "full":
                    self._send(403, {"ok": False, "error": "需要总令牌"})
                    return
                self._send(200, api.list_native_models(
                    (qs.get("project_path") or [""])[0]))
            elif path == "/api/image":
                # 手机端看用户发过的图：令牌校验 + 路径必须在记录目录的 图片/ 子目录内
                # （防路径穿越），达成「控制台看到的缩略图，手机也能看到」
                scope = self._scope(sid_q)
                if scope is None:
                    self._send(403, {"ok": False, "error": "分享未开启或令牌无效"})
                    return
                self._send_image((qs.get("path") or [""])[0])
            elif path == "/tasks":
                # 任务入站页：总令牌（自己）或投递令牌（同事）都能进
                if self._task_ok():
                    try:
                        self._send(200, TASKS_PAGE.read_bytes(), "text/html; charset=utf-8")
                    except Exception:
                        self._send(500, {"ok": False, "error": "tasks.html 缺失"})
                else:
                    self._send(403, DENY_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/tasks/list":
                if not self._task_ok():
                    self._send(403, {"ok": False, "error": "分享未开启或令牌无效"})
                    return
                st = _task_storage()
                if st is None:
                    self._send(200, {"ok": True, "tasks": [], "projects": [],
                                     "note": "任务库不可用"})
                    return
                try:
                    tasks = [_task_public(st, t) for t in st.list_tasks()]
                except Exception as e:  # noqa: BLE001
                    self._send(200, {"ok": False, "error": "读取任务库失败：{}".format(e)})
                    return
                projects = sorted({t["project"] for t in tasks if t.get("project")})
                if self._scope() != "full":
                    # 投递令牌只给项目名（填表要用），别人的需求一律不外露；
                    # 投递人自己提交了什么由页面在本地留痕
                    self._send(200, {"ok": True, "tasks": [], "projects": projects,
                                     "intake": True})
                    return
                self._send(200, {"ok": True, "tasks": tasks, "projects": projects})
            elif path == "/api/tasks/rev":
                # 「任务库变了没有」的轻问法。/api/tasks/list 把每张截图都内联成
                # data URI，一次约 500KB，拿它当轮询在手机上是灾难；这里只 stat
                # 一下库文件，几十字节就能答完，变了页面再去拉整份。
                if not self._task_ok():
                    self._send(403, {"ok": False, "error": "分享未开启或令牌无效"})
                    return
                self._send(200, {"ok": True, "rev": _task_rev()})
            else:
                self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0:
                self._send(400, {"ok": False, "error": "请求体无效"})
                return
            if length > MAX_BODY:
                self._send(413, {"ok": False, "error": "内容太大（上限 {}MB），请减少或分次提交附件".format(
                    MAX_BODY // (1024 * 1024))})
                return
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                self._send(400, {"ok": False, "error": "JSON 解析失败"})
                return
            # 日报「提交到 OA」：手机通知上的动作按钮直接打这里（锁屏上点一下就交）。
            # 只认总令牌；真正的活交给 rxyy tools 那个脚本（OA 在公司内网，它经 SSH 办），
            # 这里只管转发，不把 OA 逻辑塞进分享服务。
            if urlparse(self.path).path == "/api/oa/daily-submit":
                if self._scope() != "full":
                    self._send(403, {"ok": False, "error": "需要总令牌"})
                    return
                self._send(200, _handle_oa_daily_submit(payload))
                return
            # 任务安排站入站：无需 sid，总令牌或投递令牌均可
            path = urlparse(self.path).path
            if path in ("/api/tasks/add", "/api/tasks/mine", "/api/tasks/patch"):
                if not self._task_ok():
                    self._send(403, {"ok": False, "error": "分享未开启或令牌无效"})
                    return
                st = _task_storage()
                if st is None:
                    self._send(200, {"ok": False, "error": "任务库不可用"})
                    return
                if path == "/api/tasks/add":
                    self._send(200, _handle_task_add(st, payload))
                elif path == "/api/tasks/mine":
                    self._send(200, _handle_task_mine(st, payload))
                else:
                    self._send(200, _handle_task_patch(st, payload, api))
                return
            sid = payload.get("sid") or ""
            scope = self._scope(sid)
            if scope is None or (scope != "full" and scope != sid):
                self._send(403, {"ok": False, "error": "分享未开启或令牌无效"})
                return
            path = urlparse(self.path).path
            who = (payload.get("who") or "").strip()[:16] or "同事"
            try:
                if path.startswith("/api/takeover") or path.startswith("/api/team/") or path in ("/api/native_new", "/api/native_open", "/api/native_create"):
                    # 这些都会动到「别的」会话，单会话令牌一律不许
                    if scope != "full":
                        r = {"ok": False, "error": "需要总令牌"}
                    elif path == "/api/takeover":
                        r = api.share_takeover(sid, payload.get("target"), who=who)
                    elif path == "/api/takeover_batch":
                        r = api.share_takeover_batch(payload.get("pairs"), who=who)
                    elif path == "/api/takeover_new":
                        # 无头开一个新 Cursor 对话来接手（窗口总线扩展）；sids 给多个就合成一张单
                        sids = payload.get("sids") or [sid]
                        r = api.ext_takeover_new(sids, payload.get("instance") or "", who=who,
                                                 name=payload.get("name") or "",
                                                 model=payload.get("model"))
                    elif path == "/api/takeover_native":
                        r = api.prepare_native_transfer(sid)
                    elif path == "/api/native_new":
                        r = api.prepare_native_new(payload.get("project_path"), payload.get("prompt", ""))
                    elif path == "/api/native_create":
                        r = api.create_native_task(
                            payload.get("project_path"), payload.get("prompt", ""),
                            payload.get("model"), payload.get("effort"))
                    elif path == "/api/native_open":
                        r = api.open_codex_link(payload.get("url"))
                    elif path == "/api/team/role":
                        r = api.team_set_role(sid, payload.get("role"))
                    elif path == "/api/team/assign":
                        r = api.team_set_assign(sid, payload.get("text"))
                    elif path == "/api/team/root":
                        r = api.set_task_root(sid, payload.get("root") or "")
                    elif path == "/api/team/review_send":
                        r = api.team_review_send(sid, payload.get("note") or "")
                    elif path == "/api/team/verdict":
                        r = api.team_review_reply(sid, payload.get("verdict"),
                                                  payload.get("note") or "")
                    elif path == "/api/team/workflow":
                        # 工作流链上操作（躺被窝里也能继续/重派/确认完成）
                        r = api.workflow_action(payload.get("wid") or "",
                                                payload.get("act") or "",
                                                payload.get("arg") or "")
                    elif path == "/api/team/workflow_create":
                        r = api.workflow_create(payload.get("name") or "",
                                                payload.get("root") or "",
                                                payload.get("steps") or [],
                                                payload.get("timeout_min") or 45)
                    else:
                        r = {"ok": False, "error": "not found"}
                elif path == "/api/native_answer":
                    r = api.answer_native_question(sid, payload.get("thread_id"), payload.get("turn_id"),
                                                   payload.get("request_id"), payload.get("answers"))
                elif path == "/api/native_send":
                    r = api.send_native_text(
                        sid, payload.get("thread_id"), payload.get("turn_id"),
                        payload.get("text"), payload.get("delivery_id"),
                        images=payload.get("images"), files=payload.get("files"),
                        selected=payload.get("selected"), who=who)
                elif path == "/api/native_resume":
                    r = api.resume_native_task(
                        sid, payload.get("thread_id"))
                elif path == "/api/reply":
                    r = api.send_reply(
                        sid, payload.get("text"), payload.get("selected"),
                        payload.get("images"), bool(payload.get("is_continue")),
                        who=who, files=payload.get("files"),
                        recallable=bool(payload.get("recallable")))
                elif path == "/api/card":
                    # 决策卡片的答复：结构化 answers 由 hub 拼成 agent 读得懂的文本，走 send_reply 老路
                    r = api.answer_card(sid, payload.get("answers") or {},
                                        payload.get("mode") or "manual", who=who)
                elif path == "/api/queue":
                    r = api.queue_message(
                        sid, payload.get("text"), payload.get("images"),
                        who=who, files=payload.get("files"),
                        recallable=bool(payload.get("recallable")),
                        selected=payload.get("selected"))
                elif path == "/api/unqueue":
                    r = api.unqueue_message(sid, payload.get("qid"))
                elif path == "/api/draft":
                    # 手机端草稿同步：与控制台共用 hub 持久化，刷新/重启都不丢
                    r = api.save_draft(sid, payload.get("text"))
                elif path == "/api/asr":
                    # 语音输入转写：音频只在手机→本机→百炼之间流转，不进聊天记录
                    r = api.asr_transcribe(payload.get("audio") or "")
                else:
                    r = {"ok": False, "error": "not found"}
            except Exception as e:
                r = {"ok": False, "error": f"服务器错误: {e}"}
            self._send(200, r)

    try:
        srv = ExclusiveThreadingHTTPServer(("0.0.0.0", port), Handler)
    except OSError:
        return None
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def _egress_loop():
        # 出口 IP 是「访客是不是跟我同一张办公网」的唯一判据，宽带重拨就会变
        while not str(hub.cfg.get("office_egress_ip") or "").strip():
            refresh_egress_ip(force=True)
            time.sleep(_EGRESS_TTL)

    threading.Thread(target=_egress_loop, name="share-egress", daemon=True).start()
    return srv
