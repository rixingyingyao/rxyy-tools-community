# -*- coding: utf-8 -*-
"""rxyy MCP 接入诊断 —— 「报到成功但控制台回复送不到 agent」时先跑它。

用法（新电脑上没有 Python 也能跑，用打包版自带的解释器）：
    rxyy-tools-community.exe --run _internal\\rxyy_mcp\\接入诊断.py
源码环境：
    python 接入诊断.py

只读：探端口、读配置与日志、问一次控制台状态，绝不改任何文件。
结果同时打印到屏幕并写到 exe 旁的「rxyy MCP诊断报告.txt」，回传这个文件即可定位。
"""
from __future__ import annotations

import json
import os
import platform
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
OUT = []


def w(line=""):
    print(line)
    OUT.append(str(line))


def data_dir() -> Path:
    """与 datadir.py 同口径地找机器态目录（独立实现，免得 import 失败就没诊断）。"""
    env = (os.environ.get("RXYY_MCP_DATA_DIR")
           or os.environ.get("CHIJIU_DATA_DIR") or "").strip()
    if env and Path(env).is_dir():
        return Path(env)
    try:
        for raw in (APP_DIR / "datadir.txt").read_text(encoding="utf-8-sig").splitlines():
            raw = raw.strip().strip('"')
            if raw and not raw.startswith("#") and Path(raw).is_dir():
                return Path(raw)
    except OSError:
        pass
    data_root = Path(sys.executable).resolve().parent / "data"
    exe_data = data_root / "rxyy_mcp"
    legacy_data = data_root / "持久plus"
    if not exe_data.exists() and legacy_data.is_dir():
        exe_data = legacy_data
    if getattr(sys, "frozen", False) or exe_data.is_dir():
        return exe_data
    return APP_DIR


def read_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"__error__": repr(e)}


def port_open(port: int, host="127.0.0.1", timeout=0.8) -> bool:
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


def tail(p: Path, n: int):
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        return ["<读不到 %s: %s>" % (p, e)]
    return lines[-n:]


def main():
    dd = data_dir()
    cfg = read_json(dd / "config.json")
    gw = int(cfg.get("gateway_port", 38777) or 38777)
    hub_port = int(cfg.get("port", 38999) or 38999)
    mcp_port = int(cfg.get("mcp_http_port", 39222) or 39222)

    w("=" * 72)
    w("rxyy MCP 接入诊断  %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    w("=" * 72)
    w("机器: %s / 用户: %s / %s" % (platform.node(), os.environ.get("USERNAME"),
                                    platform.platform()))
    w("运行形态: %s" % ("打包版 exe" if getattr(sys, "frozen", False) else "源码版"))
    w("代码目录: %s" % APP_DIR)
    w("数据目录: %s" % dd)
    w("")

    w("── 1. Cursor 的 mcp.json 接入条目 ──")
    mj_path = Path(os.environ.get("USERPROFILE") or Path.home()) / ".cursor" / "mcp.json"
    mj = read_json(mj_path)
    w("路径: %s（存在: %s）" % (mj_path, mj_path.is_file()))
    entry = None
    for k, v in (mj.get("mcpServers") or {}).items():
        if k in ("rxyy MCP", "持久plus", "chijiu-plus"):
            entry = (k, v)
    if not entry:
        w("!! 没有rxyy MCP 条目 —— Cursor 根本连不上，去设置页「环境自检」一键写入")
    else:
        k, v = entry
        w("条目 %r = %s" % (k, json.dumps(v, ensure_ascii=False)))
        if v.get("url"):
            w("形式: url（正确）→ %s" % v["url"])
            want = "http://127.0.0.1:%d/mcp" % mcp_port
            if v["url"].rstrip("/") != want:
                w("!! 端口对不上，配置里的 MCP 端口是 %d，应为 %s" % (mcp_port, want))
        else:
            w("!! 形式: command（stdio）—— 新版 Cursor 对单次工具调用有 120s 硬超时，")
            w("!! zhi 会被反复掐死、你的回复永远送不回 agent。改成 url 形式即可。")
    w("")

    w("── 2. 端口 ──")
    for name, p in (("hub 裸 TCP", hub_port), ("MCP 守护(Cursor 连它)", mcp_port),
                    ("控制台网关", gw), ("分享页", int(cfg.get("share_port", 39080) or 0))):
        if p:
            w("%-22s %-6d %s" % (name, p, "通" if port_open(p) else "!! 不通"))
    w("")

    w("── 3. 关键配置（影响保活/送达）──")
    for key, default in (("keepalive_secs", 3000), ("keepalive_first_secs", 45),
                         ("detach_grace_secs", 90), ("sse_call_budget_secs", "auto"),
                         ("token_freeze", False), ("reconnect_grace_secs", 300),
                         ("hub_host", "127.0.0.1")):
        w("%-22s %s%s" % (key, cfg.get(key, default),
                          "" if key in cfg else "   (未设置，用默认值)"))
    if cfg.get("token_freeze"):
        w("!! token_freeze=true：所有 zhi/zt/ji 都被冻结挂起，agent 当然没反应。")
        w("!! 去控制台把「❄ 冻结额度」关掉。")
    probe = dd / ".mcp-client-probe.json"
    w("超时探测结论: %s" % (json.dumps(read_json(probe), ensure_ascii=False)
                            if probe.is_file() else "无（按 95s 安全拍走，agent 每 95s 需续期一次）"))
    w("")

    w("── 4. 控制台里的会话（谁在等回复）──")
    try:
        req = urllib.request.Request("http://127.0.0.1:%d/api/get_state" % gw,
                                     data=b"[]", headers={"Content-Type": "application/json"})
        st = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
        sess = st.get("sessions") or []
        w("共 %d 个 tab：" % len(sess))
        for s in sess:
            w("  · %-18s 对话=%-10s 连接=%-5s 等回复=%-5s 脱离=%-5s 排队=%-3s 处理中=%s"
              % (str(s.get("name"))[:18], str(s.get("conv_key"))[:10],
                 s.get("connected"), bool(s.get("pending")), s.get("detached"),
                 s.get("queued"), s.get("processing_secs")))
        w("")
        w("怎么看：agent 卡在 zhi 里等你 = 该行 等回复=True；若 等回复=False 而你还在")
        w("发消息，消息只会进队列，要等 agent 下次提问才送达。")
    except Exception as e:  # noqa: BLE001
        w("!! 问不到控制台状态（网关 %d）：%r" % (gw, e))
    w("")

    w("── 5. MCP 守护进程日志尾部（server-run.log）──")
    for line in tail(APP_DIR / "server-run.log", 60):
        w("  " + line)
    w("")
    w("── 6. hub 日志尾部（hub-run.log）──")
    for line in tail(APP_DIR / "hub-run.log", 60):
        w("  " + line)
    w("")

    w("── 7. 相关进程 ──")
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "
             "'hub.py|server.py|watchdog.py|rxyy-tools-community' } | "
             "Select-Object ProcessId,Name,CommandLine | Format-List"],
            capture_output=True, text=True, timeout=40)
        w(out.stdout.strip() or "(无)")
    except Exception as e:  # noqa: BLE001
        w("进程列表获取失败: %r" % e)

    home = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
            else APP_DIR)
    report = home / "rxyy MCP诊断报告.txt"
    try:
        report.write_text("\n".join(OUT), encoding="utf-8")
    except OSError:
        report = Path(os.environ.get("TEMP") or ".") / "rxyy MCP诊断报告.txt"
        report.write_text("\n".join(OUT), encoding="utf-8")
    print("\n报告已写到: %s" % report)


if __name__ == "__main__":
    main()
