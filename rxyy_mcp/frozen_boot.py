"""打包 exe（PyInstaller onedir，sys.frozen）下的子进程拉起适配。

rxyy MCP 的 hub/server/watchdog… 互相拉起时都用「解释器 + 脚本路径」；
dev 模式解释器是 pythonw.exe，打包后 exe 里没有独立解释器，统一改为
「rxyy-tools-community.exe --run <script.py> [args…]」，由 console/app.py 顶部的
调度入口用冻结解释器 runpy 执行目标脚本。dev 行为完全不变。

开机自启（HKCU Run）没有 CREATE_NO_WINDOW：必须让命令本身就是无窗进程
（pythonw，或 --windowed 的 exe）。有常驻区时优先 pythonw + 常驻脚本，
不要经 exe --run 绕一圈——那既可能弹出控制台子系统黑框，又会在
_internal 与 live 之间再交一次手。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def py_exe() -> str:
    """dev = pythonw（无窗口），frozen = 本 exe。"""
    python = Path(sys.executable)
    if is_frozen():
        return str(python)
    pythonw = python.with_name("pythonw.exe")
    return str(pythonw if pythonw.exists() else python)


def py_cmd(script, *args) -> list[str]:
    """拉起某个 .py 的完整 argv（script 传绝对路径）。"""
    if is_frozen():
        return [py_exe(), "--run", str(script), *[str(a) for a in args]]
    return [py_exe(), str(script), *[str(a) for a in args]]


def quoted_argv(argv) -> str:
    """开机自启（注册表 Run）用的命令行字符串，每段都带引号。"""
    return " ".join('"%s"' % p for p in argv)


def py_cmd_str(script, *args) -> str:
    return quoted_argv(py_cmd(script, *args))


def spawn_argv(script, *args) -> list[str]:
    """真正去拉脚本时用的 argv：有常驻区就交给那边的 pythonw，否则 py_cmd。

    GUI / 看门狗 / 开机自启共用这一条，避免打包版再 spawn 一份
    `rxyy-tools-community.exe --run _internal\\hub.py`（无 CREATE_NO_WINDOW 时就是黑框）。
    """
    script = Path(script)
    try:
        import live_runtime
        delegated = live_runtime.delegate_cmd(
            script.name, [str(a) for a in args], script.parent)
        if delegated:
            return delegated
    except Exception:
        pass
    return py_cmd(script, *args)


def script_dir(argv, fallback) -> str:
    """从 argv 里认出 .py，把它的目录当 cwd——HKCU Run 的默认 cwd 是 System32。"""
    for a in argv:
        s = str(a)
        if s.lower().endswith(".py"):
            try:
                return str(Path(s).resolve().parent)
            except OSError:
                break
    return str(fallback)


def hidden_popen_kwargs(stderr=None) -> dict:
    """Popen 用：无窗口 + 脱离进程组。HKCU Run 用不上这些，只对主动 spawn 有效。"""
    kw = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL if stderr is None else stderr,
        "close_fds": True,
    }
    if os.name == "nt":
        kw["creationflags"] = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        kw["startupinfo"] = si
    return kw
