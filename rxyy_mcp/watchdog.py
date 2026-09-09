# -*- coding: utf-8 -*-
"""rxyy MCP 独立看门狗进程（对 17:04-17:20 事故的根治）。

事故形态：hub 进程整体冻结（pywebview/WebView2 跨线程调用卡死持有 GIL）——
所有后台线程（网关、MCP 守护进程巡检、日志）一起停摆，但 38999 端口仍被内核
挂着；MCP 守护进程死后无人重拉，agent 全部 Not connected；用户狂点重启也
只是对着僵尸空喊，一堵就是 15+ 分钟。

hub 里任何自愈线程都可能随进程一起冻死，所以必须是【独立进程】来当法官：
- hub 端口(38999)有人占、但网关(38777)连续 ~30s 无响应 → 僵尸：按端口找
  pid 强杀（不带 /T，绝不误杀 hub 的子进程=MCP守护/看门狗自己）→ 拉起新 hub
- hub 端口没人占、且不是用户正常关闭（无 .hub-clean-exit 标记）→ 崩了 → 拉起
- 每次拉起 hub 前，先清「启动卡死的 hub 残骸」和「孤儿 msedgewebview2」——
  07-27 08:50 事故：死 hub 遗留的孤儿 WebView2 把连续 7 次新 hub 全部卡死在
  首条日志之前，只拉不清就是白拉
- MCP 守护进程端口(39222)连续 ~10s 不通 → 直接拉起 server.py --http
  （不依赖 hub 活着；server.py 自带端口独占，双拉起安全）

单例：独占绑定 watchdog_port（默认 38996），绑不上说明已有看门狗在跑，退出。
由 hub 启动时、重启rxyy MCP.py、rxyy tools 启动时分别尝试拉起，天然去重。
本进程绝不触碰 mcp.json（那是 hub 在守护进程「恢复上线」时做的事）。
"""
import datetime
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib import request as urlreq

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

from datadir import DATA_DIR  # noqa: E402

try:
    import instance_owner  # noqa: E402
except ImportError:  # 半套同步（只拷了部分文件）不该让看门狗整个起不来
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

LOG_PATH = APP_DIR / "watchdog-run.log"
CLEAN_EXIT_MARK = APP_DIR / ".hub-clean-exit"
SPAWN_STDERR = APP_DIR / "spawn-stderr.log"
# 正在启动的 hub 每 5s 续期这把锁，绑上端口才删（见 hub.py 的 _hold_spawn_gate）
SPAWN_GATE = APP_DIR / ".hub-spawn.lock"
SPAWN_GATE_FRESH = 20       # 超过它没续期 = 那个 hub 已经死了，闸自动开

CHECK_INTERVAL = 5          # 秒
ZOMBIE_CHECKS = 6           # 端口在、网关连续 6 次(约30s)无响应 = 僵尸
HUB_DOWN_CHECKS = 2         # 端口连续 2 次(约10s)不在 = hub 没了
MCP_DOWN_CHECKS = 2
SPAWN_COOLDOWN = 25         # 每次动手后的冷却，防拉起风暴


def log(msg):
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 256 * 1024:
            bak = LOG_PATH.with_suffix(".log.1")
            if bak.exists():
                bak.unlink()
            LOG_PATH.rename(bak)
    except OSError:
        pass
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("{} {}\n".format(
                datetime.datetime.now().strftime("%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def load_cfg():
    try:
        return json.loads((DATA_DIR / "config.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def port_open(port, timeout=1.5):
    try:
        s = socket.create_connection(("127.0.0.1", int(port)), timeout=timeout)
        s.close()
        return True
    except OSError:
        return False


def hub_booting():
    """已经有 hub 正在启动（它每 5s 续期这把锁）→ 别再拉一个。

    07-29 23:02 事故：机器一忙，hub 加载依赖要二十几秒，其间看门狗与 MCP 守护
    各自「见端口没人」又拉一个，新进程再一起抢 CPU，越拉越慢，滚到 9 个 hub、
    三分钟起不来。锁过期即开闸，所以 hub 真死了不会把救援永久挡在门外。"""
    try:
        return time.time() - SPAWN_GATE.stat().st_mtime < SPAWN_GATE_FRESH
    except OSError:
        return False


def gateway_alive(gport, timeout=6):
    """POST /api/ping：进程 Python 层活着才回得来；冻结/僵尸 = 超时。
    超时 6s：机器 thrash 时正常 hub 的响应也可能 4s+，给点余量减少误杀。"""
    try:
        req = urlreq.Request(
            "http://127.0.0.1:%d/api/ping" % int(gport),
            data=b"[]", headers={"Content-Type": "application/json"}, method="POST")
        with urlreq.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def port_owner_pid(port):
    """netstat 找 LISTENING 在指定端口上的 pid；找不到返回 None。"""
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, timeout=10,
            creationflags=0x08000000).stdout
        needle = ":%d" % int(port)
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0] == "TCP" and parts[3] == "LISTENING":
                if parts[1].endswith(needle):
                    return int(parts[4])
    except Exception:
        pass
    return None


def taskkill(pid):
    """强杀单个 pid。绝不加 /T：hub 的子进程里有 MCP 守护进程和本看门狗。"""
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=10, creationflags=0x08000000)
        return True
    except Exception:
        return False


def _pythonw():
    from frozen_boot import py_exe
    return py_exe()


def spawn_stderr_handle(script):
    """子进程 stderr 落盘句柄（07-27 事故：DEVNULL 让 7 次启动冻死死无对证）。
    先写一行 spawn 标记，再交给 Popen 继承；调用方在 Popen 后关闭自己的副本。"""
    try:
        if SPAWN_STDERR.exists() and SPAWN_STDERR.stat().st_size > 256 * 1024:
            bak = SPAWN_STDERR.with_suffix(".log.1")
            if bak.exists():
                bak.unlink()
            SPAWN_STDERR.rename(bak)
    except OSError:
        pass
    try:
        f = open(SPAWN_STDERR, "a", encoding="utf-8", errors="replace")
        f.write("--- {} spawn {} by pid={} ({}) ---\n".format(
            datetime.datetime.now().strftime("%m-%d %H:%M:%S"), script,
            os.getpid(), Path(sys.argv[0]).name or "?"))
        f.flush()
        return f
    except OSError:
        return subprocess.DEVNULL


def spawn(script, *args):
    from frozen_boot import hidden_popen_kwargs, script_dir, spawn_argv
    err = spawn_stderr_handle(script)
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


def _pid_command_line(pid):
    """读进程命令行。HKCU Run 拉起的 rxyy-tools-community.exe --run hub.py 也能看见。"""
    import ctypes
    from ctypes import wintypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    ProcessCommandLineInformation = 60
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return ""
    try:
        class UNICODE_STRING(ctypes.Structure):
            _fields_ = [
                ("Length", ctypes.c_ushort),
                ("MaximumLength", ctypes.c_ushort),
                ("Buffer", ctypes.c_void_p),
            ]
        length = wintypes.ULONG(0)
        ntdll.NtQueryInformationProcess(
            h, ProcessCommandLineInformation, None, 0, ctypes.byref(length))
        if not length.value:
            return ""
        buf = ctypes.create_string_buffer(length.value)
        status = ntdll.NtQueryInformationProcess(
            h, ProcessCommandLineInformation, buf, length.value, ctypes.byref(length))
        if status != 0:
            return ""
        hdr = ctypes.sizeof(UNICODE_STRING)
        us = UNICODE_STRING.from_buffer_copy(buf.raw[:hdr])
        if not us.Length:
            return ""
        buf_addr = ctypes.addressof(buf)
        offset = int(us.Buffer) - buf_addr if us.Buffer else hdr
        if offset < 0 or offset + us.Length > len(buf.raw):
            offset = hdr
        return buf.raw[offset:offset + us.Length].decode("utf-16le", "replace")
    except Exception:
        return ""
    finally:
        kernel32.CloseHandle(h)


def _pid_age_secs(pid):
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD),
                    ("dwHighDateTime", wintypes.DWORD)]

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return None
    try:
        ctime, etime = FILETIME(), FILETIME()
        ktime, utime = FILETIME(), FILETIME()
        if not k32.GetProcessTimes(h, ctypes.byref(ctime), ctypes.byref(etime),
                                   ctypes.byref(ktime), ctypes.byref(utime)):
            return None
        created = (ctime.dwHighDateTime << 32) | ctime.dwLowDateTime
        unix = created / 1e7 - 11644473600.0
        return max(0.0, time.time() - unix)
    finally:
        k32.CloseHandle(h)


def hub_py_pids(min_age_secs=75):
    """命令行含 hub.py、已活够 min_age_secs 的 python / pythonw / rxyy-tools-community.exe。

    未绑上端口 = 启动卡死的残骸。只报存活 ≥min_age_secs 的：正常 hub 几秒内就绑
    上端口，太年轻的可能正在启动，不动。

    以前只扫 Name like python*，开机自启留下的 `rxyy-tools-community.exe --run hub.py`
    黑框永远清不掉；也不再起 PowerShell（冷启动自己就会闪一窗）。
    """
    try:
        out = []
        for pid, _ppid, name in _snapshot_processes():
            n = (name or "").lower()
            if n not in ("python.exe", "pythonw.exe", "rxyy-tools-community.exe"):
                continue
            cmd = _pid_command_line(pid)
            if not cmd or "hub.py" not in cmd:
                continue
            age = _pid_age_secs(pid)
            if age is None:
                continue
            if age >= min_age_secs:
                out.append(int(pid))
        return out
    except Exception:
        return []


def _snapshot_processes():
    """Toolhelp 快照枚举全部进程 (pid, ppid, exe名)——毫秒级、零子进程。
    替代 PowerShell/WMI：07-27 17:15 事故实测高负载下 Get-CimInstance 30s 超时，
    把僵尸救援整整拖慢 2 分钟；PowerShell 冷启动本身在 thrash 机器上就要 10s+。"""
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    k32 = ctypes.windll.kernel32
    TH32CS_SNAPPROCESS = 0x2
    INVALID_HANDLE = ctypes.c_void_p(-1).value
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE:
        return []
    procs = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if k32.Process32FirstW(snap, ctypes.byref(entry)):
            while True:
                procs.append((int(entry.th32ProcessID),
                              int(entry.th32ParentProcessID),
                              str(entry.szExeFile)))
                if not k32.Process32NextW(snap, ctypes.byref(entry)):
                    break
    finally:
        k32.CloseHandle(snap)
    return procs


def _kill_pid_native(pid):
    """OpenProcess+TerminateProcess 直杀，免掉 taskkill 子进程开销。"""
    import ctypes
    PROCESS_TERMINATE = 0x0001
    h = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
    if not h:
        return False
    try:
        return bool(ctypes.windll.kernel32.TerminateProcess(h, 1))
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def kill_orphan_webview2():
    """清孤儿 msedgewebview2（父进程已死）并返回清掉的数量。hub 被 AppHang 机制关闭后，
    其 WebView2 渲染进程树可能残留并把新 hub 的 GUI 初始化卡死（07-27 事故：12 次拉起
    全部无声卡死在「hub 启动」日志之前的头号嫌疑）。只杀父进程不存在的，绝不伤活着的应用。"""
    try:
        procs = _snapshot_processes()
        if not procs:
            return 0
        alive = {p[0] for p in procs}
        orphans = [p[0] for p in procs
                   if p[2].lower() == "msedgewebview2.exe" and p[1] not in alive]
        n = 0
        for pid in orphans:
            if _kill_pid_native(pid) or taskkill(pid):
                n += 1
        if n:
            log("已清理 {} 个孤儿 msedgewebview2 进程（父进程已死的残留）".format(n))
        return n
    except Exception as e:
        log("清理孤儿 WebView2 失败: {}".format(e))
        return 0


def main():
    cfg = load_cfg()
    wport = int(cfg.get("watchdog_port", 38996) or 38996)
    # 端口单例只保证「全机一个看门狗」，不保证它属于用户正在用的那套安装。
    # 源码版与打包版各有一张互保网，谁抢到守卫端口就从自己目录复活 hub/MCP 守护，
    # 两套轮流坐庄——每换手一次，39222 上在飞的 zhi 全断（见 instance_owner）。
    # 故绑端口前先问归属：不是正主就别当这个看门狗。
    stand_down, why = instance_owner.should_stand_down(APP_DIR)
    if stand_down:
        log("不启动看门狗：{}".format(why))
        return 0
    # 单例守卫：独占绑定；绑不上=已有看门狗，静默退出
    guard = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        guard.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        guard.bind(("127.0.0.1", wport))
        guard.listen(8)
    except OSError:
        return 0

    # 守卫端口必须有人 accept：只 listen 不 accept 的话，探活连接会滞留 backlog，
    # 队列一满后续探活全部 connection refused——server.py 的互保被骗以为看门狗死了，
    # 每 30s 白拉一个注定 bind 失败的新看门狗（07-27 实测：一天误拉 536 次）。
    # 协议升级（07-27 15:03 事故）：accept 后回一行「pid:主循环时间戳」再关。
    # 纯 drain 会造成反向假活——主循环冻死了守卫线程还在应答，server 探活被骗、
    # 39222 挂了没人拉。server 读时间戳，停滞 >180s 判定冻死、强杀重拉。
    main_ts = {"ts": time.time()}

    def _drain_guard():
        while True:
            try:
                conn, _ = guard.accept()
                try:
                    conn.sendall("{}:{}".format(
                        os.getpid(), main_ts["ts"]).encode("ascii"))
                except OSError:
                    pass
                conn.close()
            except OSError:
                time.sleep(1)

    threading.Thread(target=_drain_guard, daemon=True).start()
    log("=== 看门狗启动 pid={} ===".format(os.getpid()))

    hub_port = int(cfg.get("port", 38999) or 38999)
    gport = int(cfg.get("gateway_port", 38777) or 38777)
    mcp_port = int(cfg.get("mcp_http_port", 39222) or 39222)

    zombie_cnt = 0
    hub_down_cnt = 0
    mcp_down_cnt = 0
    mcp_defer_cnt = 0
    cooldown_until = 0.0

    while True:
        time.sleep(CHECK_INTERVAL)
        now = time.time()
        main_ts["ts"] = now  # 主循环存活凭证（守卫端口回给探活方）
        in_cooldown = now < cooldown_until

        # 运行中换主（用户改用另一套安装、对面 hub 抢到端口并认领）：立刻退位、
        # 释放守卫端口，让正主那套的看门狗顶上。否则本进程会把已经退役的这套
        # hub/MCP 守护一遍遍复活回来，与正主互抢 39222。
        stand_down, why = instance_owner.should_stand_down(APP_DIR)
        if stand_down:
            log("看门狗退位：{}".format(why))
            return 0

        # ---- MCP 端点（无条件守护：agent 全靠它） ----
        if port_open(mcp_port):
            mcp_down_cnt = 0
            mcp_defer_cnt = 0
        else:
            mcp_down_cnt += 1
            if mcp_down_cnt >= MCP_DOWN_CHECKS and not in_cooldown:
                # 第二刀后 39222 的首选宿主是 hub 进程内线程（hub 每 10s 自巡自绑，
                # 见 hub.mcp_http_daemon_loop）。hub 活着就先让它接管——此刻 spawn
                # 外部 server.py 会抢住端口，让系统滞留在外挂降级态。连让 3 轮仍
                # 没人绑上 = hub 的内嵌巡检坏了，照旧外部顶班兜底（走 38999 TCP 桥）。
                if port_open(hub_port) and mcp_defer_cnt < 3:
                    mcp_defer_cnt += 1
                    log("MCP 端口 {} 不通但 hub 活着，让 hub 进程内接管（让行 {}/3）".format(
                        mcp_port, mcp_defer_cnt))
                    mcp_down_cnt = 0
                else:
                    log("MCP 端口 {} 连续 {} 次不通，拉起 server.py --http 顶班".format(
                        mcp_port, mcp_down_cnt))
                    try:
                        spawn("server.py", "--http", mcp_port)
                    except Exception as e:
                        log("拉起 server.py 失败: {}".format(e))
                    mcp_down_cnt = 0
                    mcp_defer_cnt = 0
                    cooldown_until = now + SPAWN_COOLDOWN

        # ---- hub ----
        if port_open(hub_port):
            hub_down_cnt = 0
            if gateway_alive(gport):
                zombie_cnt = 0
            else:
                zombie_cnt += 1
                if zombie_cnt >= ZOMBIE_CHECKS and not in_cooldown:
                    pid = port_owner_pid(hub_port)
                    if pid and pid != os.getpid():
                        log("hub 端口 {} 被 pid={} 占用但网关 {}s 无响应=僵尸，强杀并重拉".format(
                            hub_port, pid, ZOMBIE_CHECKS * CHECK_INTERVAL))
                        taskkill(pid)
                        time.sleep(2)
                        # 先拉后清：无头 hub 不初始化 GUI，不再怕孤儿 WebView2；
                        # 清扫改为拉起后的卫生工序，救援不再被慢扫描拖住
                        # （07-27 17:15 事故：清扫 30s 超时把恢复拖了 2 分钟）
                        try:
                            spawn("hub.py", "--daemon")
                            log("已拉起新 hub（无头核心）")
                        except Exception as e:
                            log("拉起 hub 失败: {}".format(e))
                        for p in hub_py_pids():
                            taskkill(p)
                            log("已清理启动卡死的 hub 残骸 pid={}".format(p))
                        kill_orphan_webview2()
                    zombie_cnt = 0
                    cooldown_until = now + SPAWN_COOLDOWN
        else:
            zombie_cnt = 0
            hub_down_cnt += 1
            if hub_down_cnt >= HUB_DOWN_CHECKS and not in_cooldown:
                if CLEAN_EXIT_MARK.exists():
                    pass  # 用户正常关闭的控制台，不越权复活
                elif hub_booting():
                    hub_down_cnt = 0  # 已有 hub 在加载依赖，等它，别火上浇油
                else:
                    log("hub 端口 {} 连续 {} 次不在且非正常关闭，拉起 hub".format(
                        hub_port, hub_down_cnt))
                    # 先拉后清（07-27 17:15 教训：清扫的慢扫描把救援拖 2 分钟）。
                    # 无头 hub 不初始化 GUI，孤儿 WebView2 卡不死它；残骸清理
                    # 只杀 ≥75s 还没绑上端口的老 hub.py，刚拉起的新 hub 不受影响
                    try:
                        spawn("hub.py", "--daemon")
                    except Exception as e:
                        log("拉起 hub 失败: {}".format(e))
                    for p in hub_py_pids():
                        taskkill(p)
                        log("已清理启动卡死的 hub 残骸 pid={}".format(p))
                    kill_orphan_webview2()
                    cooldown_until = now + SPAWN_COOLDOWN
                hub_down_cnt = 0


if __name__ == "__main__":
    # 常驻区那份才是这台机器的看门狗（理由见 hub.py 入口处）。这一句必须排在
    # should_stand_down 前面：退位判断问的是「谁是正主安装」，而让位问的是
    # 「这活该在哪个目录里干」——先站对地方，再谈归属。
    if live_runtime.hand_over("watchdog.py", sys.argv[1:]):
        sys.exit(0)
    try:
        os.chdir(APP_DIR)
    except OSError:
        pass
    sys.exit(main())
