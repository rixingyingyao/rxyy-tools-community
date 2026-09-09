# -*- coding: utf-8 -*-
"""一键重启rxyy MCP（真重启 + 可见反馈）。

旧版只改 mcp.json 的 nonce 提醒 Cursor 重连，且用 pythonw 运行时 print 全部不可见，
双击后「毫无动静」——控制台本体根本没被重启，与快捷方式名字不符（实测反馈）。

现在双击后依次做三件事，最后弹一个 3.5 秒自动消失的提示框告诉你每一步结果：
1. 控制台在跑 → 通过本机网关触发「重启控制台」（等同右下角 🔄，会话 tab 靠快照恢复）；
   控制台没在跑 → 直接拉起新实例。
2. 触碰 ~/.cursor/mcp.json 的rxyy MCP nonce，让 Cursor 立即重连 MCP（原有功能保留）。
3. 弹框反馈结果。

用法：双击桌面「重启rxyy MCP.lnk」，或 python 重启rxyy MCP.py
"""
import ctypes
import json
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib import request as urlreq

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

# 复用统一实现（P2 瘦身）：僵尸判定/清残骸/杀孤儿/stderr 落盘与看门狗共用一份代码，
# touch 与 hub/server 共用一份——三处代码漂移的历史问题就此断根
from datadir import DATA_DIR  # noqa: E402
from mcp_touch import touch_mcp_json  # noqa: E402
from watchdog import (hub_py_pids, kill_orphan_webview2,  # noqa: E402
                      spawn_stderr_handle, taskkill)


def load_cfg():
    try:
        return json.loads((DATA_DIR / "config.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def hub_running(port):
    try:
        s = socket.create_connection(("127.0.0.1", int(port)), timeout=1.5)
        s.close()
        return True
    except OSError:
        return False


def restart_via_gateway(gport):
    """通过 hub 内嵌网关调用 Api.restart_hub（等同控制台右下角 🔄）"""
    try:
        req = urlreq.Request(
            "http://127.0.0.1:%d/api/restart_hub" % int(gport),
            data=b"[]", headers={"Content-Type": "application/json"}, method="POST")
        with urlreq.urlopen(req, timeout=5) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def gateway_ping(gport):
    """探 /api/ping：区分「健康实例」与「端口被占着的冻结僵尸」。"""
    try:
        req = urlreq.Request(
            "http://127.0.0.1:%d/api/ping" % int(gport),
            data=b"[]", headers={"Content-Type": "application/json"}, method="POST")
        with urlreq.urlopen(req, timeout=4) as r:
            return 200 <= r.status < 300
    except Exception:
        return False


def port_owner_pid(port):
    """netstat 找 LISTENING 在指定端口上的 pid。"""
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


def spawn_script(script, *args):
    """拉起本目录脚本，stderr 落 spawn-stderr.log（启动冻死可观测）。"""
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


def spawn_hub(clean_first=False):
    """拉起 hub（无头核心）。clean_first=True 时先清启动卡死残骸和孤儿 WebView2
    （与看门狗同一套函数）——僵尸接管场景不清就是白拉（07-27 事故教训）。"""
    if clean_first:
        for p in hub_py_pids():
            taskkill(p)
        kill_orphan_webview2()
    spawn_script("hub.py", "--daemon")


def notify(text, title="rxyy MCP"):
    """pythonw 下 print 不可见，弹 3.5s 自动关闭的提示框是唯一可靠反馈"""
    try:
        MB_ICONINFORMATION, MB_SETFOREGROUND, MB_TOPMOST = 0x40, 0x10000, 0x40000
        ctypes.windll.user32.MessageBoxTimeoutW(
            0, str(text), str(title),
            MB_ICONINFORMATION | MB_SETFOREGROUND | MB_TOPMOST, 0, 3500)
    except Exception:
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            print(text)
        except Exception:
            pass


def mcp_daemon_running(mcp_port):
    try:
        s = socket.create_connection(("127.0.0.1", int(mcp_port)), timeout=1.5)
        s.close()
        return True
    except OSError:
        return False


def main():
    cfg = load_cfg()
    port = int(cfg.get("port", 38999) or 38999)
    gport = int(cfg.get("gateway_port", 38777) or 38777)
    mcp_port = int(cfg.get("mcp_http_port", 39222) or 39222)
    steps = []
    if hub_running(port):
        if gateway_ping(gport):
            if restart_via_gateway(gport):
                steps.append("✓ 已触发控制台重启（tab 靠快照恢复，几秒内回来）")
            else:
                steps.append("⚠ 网关活着但重启调用失败，请点控制台右下角 🔄 重启")
        else:
            # 端口被占 + 网关无响应 = 冻结僵尸（17:04 事故形态）。
            # 旧版只提示「请点 🔄」——对僵尸毫无意义。现在直接强杀接管，
            # 且拉起前清残骸+孤儿 WebView2（不清就是白拉）。
            pid = port_owner_pid(port)
            if pid:
                taskkill(pid)
                time.sleep(2)
                spawn_hub(clean_first=True)
                steps.append("✓ 检测到僵尸控制台(pid={})，已强杀清扫并重新拉起".format(pid))
            else:
                spawn_hub(clean_first=True)
                steps.append("⚠ 端口被占但找不到属主，已清扫并尝试拉起新实例")
    else:
        spawn_hub()
        steps.append("✓ 控制台未在运行，已拉起新实例")

    # MCP 守护进程兜底：死了直接拉（不等 hub 巡检；agent 全靠这个端口活着）
    if not mcp_daemon_running(mcp_port):
        try:
            spawn_script("server.py", "--http", mcp_port)
            steps.append("✓ MCP 守护进程(:{})未在跑，已拉起".format(mcp_port))
        except Exception:
            steps.append("⚠ MCP 守护进程拉起失败")

    # 看门狗（单例，重复拉起自动退出）：以后僵尸/崩溃 30s 内自动救，不用你狂点
    try:
        spawn_script("watchdog.py")
    except Exception:
        pass

    # 只有 MCP 守护进程端口活着才触碰 mcp.json——对着死端口触碰只会把所有
    # agent 的连接掐断、打进更深的重试退避（17:04 事故里越点越糟的原因）
    deadline = time.time() + 8
    while not mcp_daemon_running(mcp_port) and time.time() < deadline:
        time.sleep(0.5)
    if mcp_daemon_running(mcp_port):
        ok, reason = touch_mcp_json()
        steps.append("✓ 已提醒 Cursor 重连 MCP" if ok
                     else "⚠ 触碰 mcp.json 失败：{}".format(reason))
    else:
        steps.append("⚠ MCP 守护进程仍未上线，跳过重连提醒（看门狗会继续救）")
    notify("\n".join(steps))
    return 0


if __name__ == "__main__":
    sys.exit(main())
