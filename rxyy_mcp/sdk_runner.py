# -*- coding: utf-8 -*-
"""rxyy MCP 本机 agent 运行器（独立包装进程）。

控制台「+ → 直接拉起本机 agent」时，hub 把本脚本 spawn 成【独立进程】来跑
agent——不作为 hub 子进程，控制台随便重启都不影响正在跑的 agent（延续
「重启不断 agent」的设计）。运行状态写入注册表 .sdk-agents.json，hub 轮询
注册表展示列表；对话交互本身仍走 MCP：agent 按接入提示词用 zhi 报到，
控制台 tab 自动出现，后续沟通全在 tab 里进行。

两种后端（--backend）：
- cli（默认）：Cursor CLI（`agent` 命令）+ 已登录的 Cursor 账号 ——
  用量计入该账号的【订阅额度】，与 IDE 同一个池子（适配多开+切号用法）；
  切号 = 终端里 agent logout / agent login，无需改控制台任何东西
- sdk：cursor-sdk + CURSOR_API_KEY —— 走 API 用量计费（与订阅额度分开）

用法（由 hub 调用，无需手动执行）：
  pythonw sdk_runner.py run --key <注册表键> --cwd <工作目录> --prompt-file <提示词文件>
      [--backend cli|sdk] [--resume-agent <id>] [--model auto] [--task-name 名字]
API key（仅 sdk 后端）通过环境变量 CURSOR_API_KEY 传入，不落命令行。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

from datadir import DATA_DIR  # noqa: E402

REG_PATH = DATA_DIR / ".sdk-agents.json"
REG_LOCK = DATA_DIR / ".sdk-agents.lock"
PROMPT_DIR = DATA_DIR / ".sdk-prompts"
# 扩展名用 .log：这台机器的企业 DLP 按扩展名透明加密 .txt，.log 不受影响
LOG_PATH = APP_DIR / "sdk-agents.log"


def _log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("{} [pid {}] {}\n".format(
                time.strftime("%m-%d %H:%M:%S"), os.getpid(), msg))
    except Exception:
        pass


# ---------------- 注册表（hub 与各 runner 进程共用，文件锁串行化写入） ----------------
def _acquire_lock(timeout=5.0):
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(str(REG_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - REG_LOCK.stat().st_mtime > 10:
                    REG_LOCK.unlink()  # 残留锁（持锁进程已死），接管
                    continue
            except OSError:
                pass
            if time.time() > deadline:
                return False
            time.sleep(0.05)
        except OSError:
            return False


def _release_lock():
    try:
        REG_LOCK.unlink()
    except OSError:
        pass


def load_registry():
    try:
        data = json.loads(REG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_registry(entries):
    tmp = str(REG_PATH) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=1)
    os.replace(tmp, REG_PATH)


def upsert_entry(key, **fields):
    """按 key 更新（或插入）一条注册表记录；带跨进程文件锁。"""
    got = _acquire_lock()
    try:
        entries = load_registry()
        for e in entries:
            if e.get("key") == key:
                e.update(fields)
                e["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
                break
        else:
            fields = dict(fields)
            fields["key"] = key
            fields["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            entries.append(fields)
        _write_registry(entries)
    finally:
        if got:
            _release_lock()


def remove_entry(key):
    got = _acquire_lock()
    try:
        entries = [e for e in load_registry() if e.get("key") != key]
        _write_registry(entries)
    finally:
        if got:
            _release_lock()


def pid_alive(pid):
    """Windows 下判断进程是否存活（STILL_ACTIVE=259）。"""
    if not pid:
        return False
    try:
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED
        if not h:
            return False
        try:
            code = ctypes.c_ulong(0)
            ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            return bool(ok) and code.value == 259
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        return False


# ---------------- runner 主流程 ----------------
def _mcp_port():
    try:
        cfg = json.loads((DATA_DIR / "config.json").read_text(encoding="utf-8"))
        return int(cfg.get("mcp_http_port", 39222) or 39222)
    except Exception:
        return 39222


def find_agent_cmd():
    """定位 Cursor CLI 可执行文件（PATH 优先，退回默认安装目录）。"""
    for name in ("agent.cmd", "agent.exe", "agent"):
        p = shutil.which(name)
        if p:
            return p
    cand = Path(os.environ.get("LOCALAPPDATA", "")) / "cursor-agent" / "agent.cmd"
    if cand.is_file():
        return str(cand)
    return None


def _extract_chat_id(obj):
    """从 CLI stream-json 事件里尽力挖出会话 ID（字段名跨版本兜底）。"""
    for k in ("session_id", "sessionId", "chat_id", "chatId", "id"):
        v = obj.get(k)
        if isinstance(v, str) and len(v) >= 8 and obj.get("type") in ("system", "result", None):
            return v
    return None


def run_cli(args, key, prompt):
    """CLI 后端：`agent -p` 无头跑一轮（用已登录 Cursor 账号 = 订阅额度）。
    MCP 走 ~/.cursor/mcp.json（rxyy MCP 的 HTTP 条目已在里面），与 IDE 完全同款。"""
    agent_cmd = find_agent_cmd()
    if not agent_cmd:
        upsert_entry(key, status="error", note=(
            "Cursor CLI 未安装：Windows PowerShell 运行 "
            "irm 'https://cursor.com/install?win32=true' | iex"))
        return 1
    cmd = [agent_cmd, "-p", "--force", "--output-format", "stream-json"]
    if args.model and args.model not in ("", "auto"):
        cmd += ["--model", args.model]
    if args.resume_agent:
        cmd += ["--resume", args.resume_agent]
    cmd += [prompt]
    _log("CLI 启动 key={} cwd={} model={} resume={}".format(
        key, args.cwd, args.model or "auto", args.resume_agent or "-"))
    try:
        proc = subprocess.Popen(
            cmd, cwd=args.cwd,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
            creationflags=0x08000000)  # CREATE_NO_WINDOW
    except Exception as e:  # noqa: BLE001
        upsert_entry(key, status="error", note="CLI 拉起失败: {}".format(e)[:300])
        return 1
    upsert_entry(key, status="running", backend="cli")
    chat_id = ""
    tail = []  # 最近输出（报错时给用户看得懂的现场）
    try:
        for line in proc.stdout:
            line = (line or "").strip()
            if not line:
                continue
            tail.append(line[:200])
            if len(tail) > 8:
                tail.pop(0)
            if chat_id:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            cid = _extract_chat_id(obj) if isinstance(obj, dict) else None
            if cid:
                chat_id = cid
                upsert_entry(key, agent_id=chat_id)
                _log("CLI chat_id={} key={}".format(chat_id, key))
    except Exception:
        pass
    rc = proc.wait()
    if rc == 0:
        upsert_entry(key, status="finished", note="")
        _log("CLI 完成 key={}".format(key))
    else:
        note = "agent 退出码 {}；末尾输出: {}".format(rc, " | ".join(tail[-3:]))[:300]
        upsert_entry(key, status="error", note=note)
        _log("CLI 失败 key={} rc={}".format(key, rc))
    return 0 if rc == 0 else 2


def run(args):
    key = args.key
    upsert_entry(key, pid=os.getpid(), status="starting", note="")
    try:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    except Exception as e:
        upsert_entry(key, status="error", note="读提示词文件失败: {}".format(e))
        return 1
    if (args.backend or "cli") == "cli":
        return run_cli(args, key, prompt)
    try:
        from cursor_sdk import (Agent, AgentOptions, LocalAgentOptions,
                                HttpMcpServerConfig, CursorAgentError)
    except ImportError:
        upsert_entry(key, status="error",
                     note="cursor-sdk 未安装：pip install cursor-sdk 后重试")
        return 1
    if not os.environ.get("CURSOR_API_KEY"):
        upsert_entry(key, status="error",
                     note="未配置 CURSOR_API_KEY（控制台 ⚙设置 → Cursor API Key）")
        return 1

    # agent 通过我们的 MCP HTTP 守护进程接入rxyy MCP（与桌面 Cursor 完全同款通道）
    mcp = {"rxyy MCP": HttpMcpServerConfig(url="http://127.0.0.1:{}/mcp".format(_mcp_port()))}
    opts = AgentOptions(
        model=(args.model or "auto"),
        api_key=os.environ["CURSOR_API_KEY"],
        name=(args.task_name or None),
        local=LocalAgentOptions(cwd=args.cwd),
        mcp_servers=mcp,
    )
    _log("启动 key={} cwd={} model={} resume={}".format(
        key, args.cwd, args.model, args.resume_agent or "-"))
    try:
        if args.resume_agent:
            agent = Agent.resume(args.resume_agent, opts)
        else:
            agent = Agent.create(opts)
    except CursorAgentError as e:
        upsert_entry(key, status="error", note="SDK 启动失败: {}".format(
            getattr(e, "message", None) or e)[:300])
        _log("启动失败 key={}: {}".format(key, e))
        return 1
    except Exception as e:  # noqa: BLE001
        upsert_entry(key, status="error", note="SDK 启动异常: {}".format(e)[:300])
        _log("启动异常 key={}: {}".format(key, e))
        return 1

    exit_code = 0
    try:
        upsert_entry(key, agent_id=agent.agent_id, status="running")
        _log("agent_id={} key={}".format(agent.agent_id, key))
        run_handle = agent.send(prompt)
        result = run_handle.wait()
        status = getattr(result, "status", "") or ""
        if status == "finished":
            upsert_entry(key, status="finished", note="")
            _log("完成 key={}".format(key))
        else:
            upsert_entry(key, status="error", note="run 结束状态: {}".format(status or "未知"))
            _log("run 非正常结束 key={} status={}".format(key, status))
            exit_code = 2
    except Exception as e:  # noqa: BLE001
        upsert_entry(key, status="error", note=str(e)[:300])
        _log("运行异常 key={}: {}".format(key, e))
        exit_code = 2
    finally:
        try:
            agent.close()
        except Exception:
            pass
    return exit_code


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")
    r = sub.add_parser("run")
    r.add_argument("--key", required=True)
    r.add_argument("--cwd", required=True)
    r.add_argument("--prompt-file", required=True)
    r.add_argument("--backend", default="cli", choices=("cli", "sdk"))
    r.add_argument("--model", default="auto")
    r.add_argument("--task-name", default="")
    r.add_argument("--resume-agent", default="")
    args = p.parse_args()
    if args.cmd != "run":
        p.print_help()
        return 1
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
