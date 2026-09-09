# -*- coding: utf-8 -*-
"""rxyy MCP 的常驻区：把它从「会被换装整块 rename 掉的包目录」里挪出来。

## 为什么

`dist\\rxyy-tools-community\\` 里焊死了四样变化速率完全不同的东西——控制台 UI、console 的
PYZ、**rxyy MCP**、`data\\`。换装是整块 rename，于是改一行日报代码，全队的 MCP
跟着陪绑：08-07 12:29 那次换装本身只 13 秒，全队却在 Not connected 里挂了十分钟。

`2ff3997` 让换装完主动叫醒 Cursor，把十分钟压到了一次换装的量级；但那治的是症。
根治是让 rxyy MCP 压根不在被换的那块地里：**换装只换控制台，hub / MCP / 看门狗 /
分享站一个都不重启，在飞的 zhi 也不掉。**

顺带解掉两件老账：
- 热拷不再被换装刷掉。现在大家热拷进包内，下次打包又拿工作树盖回去——今天全队
  反复核对「包内 = HEAD」防的就是这个。
- 机器上**不再有两份 rxyy MCP**。源码树一份、包内一份，两套各带一张互保网轮流
  坐庄，正是 07-29 事故（见 instance_owner.py）。常驻区是唯一一份。

## 怎么定位

跟本仓已有的两个指针（`data-dir.txt`、`datadir.txt`）同一个套路，别再发明第三种：
包内的 rxyy MCP 带一个 `live-dir.txt`，里面写常驻区的绝对路径。没有这个文件、路径
不存在、或者那边没有 hub.py，一律当常驻区不存在，**原样退回今天的行为**——同事
拿到的拷贝本来就不换装，包内那份照旧跑。

常驻区自己不需要指针：新安装使用 `<live>\\rxyy_mcp\\`，升级时也识别旧的
`<live>\\持久plus\\`；机器态都在 `<live>\\state\\`
（靠常驻区里的 `datadir.txt` 指过去，datadir.py 早就认这个文件）。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
POINTER_NAME = "live-dir.txt"
PYTHON_NAME = "python-exe.txt"
CONSOLE_ROOT_NAME = "console-root.txt"

# 常驻区的目录名，build.ps1 与运行时共用这一份约定。旧目录只用于升级读取。
CODE_SUBDIR = "rxyy_mcp"
LEGACY_CODE_SUBDIR = "持久plus"
STATE_SUBDIR = "state"

# 常驻区必须自带的东西：缺了说明上一次物化被打断，宁可退回包内那份也别跑半拉子
_REQUIRED = ("hub.py", "hub_api.py", "session_core.py", "config_schema.py",
             "plugin_registry.py", "server.py", "watchdog.py", "decision_card.py",
             "datadir.py", "frozen_boot.py", "live_runtime.py", "parkgate.py",
             "parkgate_hook.js", "billgate.py", "billgate_hook.js")

# 物化时要带走的：代码与它直接要读的静态件。日志、锁、机器态一概不带——
# 机器态另有 seed_state 走「hub 已停」那个窗口，torn read 输不起。
CODE_SUFFIXES = (".py", ".html", ".js", ".wav")
# 名字长得像代码、其实是运行期产物，别混进常驻区
_NEVER_COPY = {POINTER_NAME, "datadir.txt", PYTHON_NAME, CONSOLE_ROOT_NAME}

# 机器态：全队的 tab 都在 .sessions.json 里（现役 844 KB），搬错就是 07-29 重演。
# .mcp-convs.json 是切换那一下最要紧的一份：MCP 进程换一条命后靠它把上一条命服务过
# 的对话捞回心跳名单，漏搬就是 08-07 12:16 那一幕——19 个 tab 有 15 个被判「已终止」，
# 而那些 agent 明明还在 Cursor 里跑着（见 server.py::restore_conv_registry）。
# 刻意不带 .instance-owner.json：它是「谁是正主」的 pid 标记，常驻区起来后自己认领，
# 搬过去等于让常驻区一上来就看见一个指向包内的旧正主，看门狗当场退位。
STATE_NAMES = (".sessions.json", "config.json", "board.json", "relays.json",
               "workflows.json", ".sdk-agents.json", ".mcp-convs.json")


def _read_pointer(app_dir: Path) -> Path | None:
    """读 live-dir.txt。utf-8-sig：它由 build.ps1 用 PowerShell 写出，会带 BOM。"""
    try:
        lines = (app_dir / POINTER_NAME).read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    for line in lines:
        line = line.strip().strip('"')
        if line and not line.startswith("#"):
            return Path(line)
    return None


def is_complete(code_dir) -> bool:
    """这个目录是不是一份能跑的 rxyy MCP。"""
    code_dir = Path(code_dir)
    return all((code_dir / n).is_file() for n in _REQUIRED)


def _installed_code_dir(live_root, *, allow_legacy=True) -> Path:
    root = Path(live_root)
    current = root / CODE_SUBDIR
    if is_complete(current) or not allow_legacy:
        return current
    legacy = root / LEGACY_CODE_SUBDIR
    return legacy if is_complete(legacy) else current


def live_code_dir(app_dir=None) -> Path | None:
    """常驻区里的 rxyy MCP 目录；没有完整常驻区则返回 None。

    环境变量 RXYY_MCP_LIVE_DIR 优先，旧的 CHIJIU_LIVE_DIR 继续兼容。
    """
    app_dir = Path(app_dir) if app_dir else APP_DIR
    raw = (os.environ.get("RXYY_MCP_LIVE_DIR")
           or os.environ.get("CHIJIU_LIVE_DIR") or "").strip()
    root = Path(raw) if raw else _read_pointer(app_dir)
    if root is None:
        return None
    code = _installed_code_dir(root)
    if code.resolve() == app_dir.resolve():
        return None  # 我自己就是常驻区，别绕回来指向自己
    return code if is_complete(code) else None


def console_root(app_dir=None) -> Path | None:
    """rxyy tools 那一侧（`data\\`、`console\\`、`scripts\\`）的根目录；没记就 None。

    rxyy MCP 一直是「rxyy tools 装哪儿它就跟到哪儿」，所以仓里有六处直接拿
    `APP_DIR.parent` 当那个根——打包版是 exe 旁的 `_internal\\`，源码态是仓库根，
    两条推断一直都成立。**常驻区是第三种住法，两条都不成立**：
    `%LOCALAPPDATA%\\rxyy-tools-community\\live\\` 底下既没有 `data-dir.txt` 也没有 `console\\`。

    08-07 16:26 切过去之后当场就现了原形：投递站 `/tasks` 回「任务库不可用」，
    同事提的需求进不来——`share_server._task_storage` 按 `APP_DIR.parent` 找
    `console/api/taskstage/storage.py`，找不到就退回「从 exe 内部归档 import」，
    而常驻区跑的是系统 Python、没有那个归档，于是任务库判成不可用（库里 6 条
    任务一条都读不出来）。

    治法是别再从位置反推：物化时 build.ps1 知道仓库在哪，记一笔就是了。
    """
    app_dir = Path(app_dir) if app_dir else APP_DIR
    try:
        raw = (app_dir / CONSOLE_ROOT_NAME).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None
    for line in raw.splitlines():
        line = line.strip().strip('"')
        if line and not line.startswith("#"):
            p = Path(line)
            return p if p.is_dir() else None
    return None


def state_dir(live_root) -> Path:
    """机器态目录。常驻区里的 datadir.txt 指到这儿，datadir.py 早就认那个文件。"""
    return Path(live_root) / STATE_SUBDIR


def windowless_python(python_exe) -> Path:
    """python.exe → 同目录 pythonw.exe。读旧笔记、写新笔记都走这里，避免漏换。"""
    py = Path(python_exe)
    pyw = py.with_name("pythonw.exe")
    return pyw if pyw.is_file() else py


def is_live_install(app_dir=None) -> bool:
    """当前目录是不是常驻区那份 rxyy MCP（新旧目录名都识别）。

    源码仓的 rxyy_mcp 旁边是 console/；包内是 _internal/。两者都不能改写
    HKCU Run，否则又把开机自启指回开发目录（07-29 事故）。
    """
    app_dir = Path(app_dir) if app_dir else APP_DIR
    if app_dir.name not in (CODE_SUBDIR, LEGACY_CODE_SUBDIR):
        return False
    root = app_dir.parent
    return (root / STATE_SUBDIR).is_dir() and is_complete(app_dir)


def live_python(live_root) -> Path | None:
    """常驻区跑在哪个 Python 上；没记下或那个 Python 已经不在则返回 None。

    **不在运行时现找。** 这台机器 PATH 上排在最前的是 miniconda 3.8，没有
    PyInstaller 也没有 markdown——hub 一 import markdown 当场就崩（build.ps1 的
    第 0 步为同一件事栽过，见 a04c611）。正确的时机是物化那一刻：build.ps1 第 0 步
    刚刚验过 markdown/segno/tomlkit/webview 全在，把那个解释器的路径记下来就行。
    """
    try:
        raw = (_installed_code_dir(live_root) / PYTHON_NAME).read_text(
            encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return None
    for line in raw.splitlines():
        line = line.strip().strip('"')
        if line and not line.startswith("#"):
            p = Path(line)
            if p.is_file():
                return windowless_python(p)
            return None
    return None


def record_python(live_root, python_exe) -> Path | None:
    """记下常驻区该用的解释器。给 python.exe 会自动换成同目录的 pythonw.exe——
    带控制台窗口的解释器会让 hub/MCP 每次启动都闪一个黑框。"""
    py = Path(python_exe)
    if not py.is_file():
        return None  # 给进来的那个必须真在：记一个没验过的解释器比不记更危险
    py = windowless_python(py)
    dst = Path(live_root) / CODE_SUBDIR / PYTHON_NAME
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("# 常驻区用的解释器，由 build.ps1 在依赖自检通过后记下\n%s\n" % py,
                   encoding="utf-8")
    return py


def delegate_cmd(script_name, args=(), app_dir=None) -> list[str] | None:
    """本进程该不该把这活让给常驻区那份跑；该让就给完整 argv，不该让给 None。

    hub / server / watchdog 在入口处各问一句。四道闸，任何一道不过就照旧在原地跑
    （= 今天的行为，同事拿到的拷贝不受影响）：没有常驻区、常驻区那份不完整、
    常驻区没有这个脚本、没记下可用的解释器。

    常驻区自己问出来必然是 None（live_code_dir 挡了「指向自己」，而且物化根本
    不把 live-dir.txt 带过去）——两道独立的闸，避免自己拉自己拉成一串。
    """
    code = live_code_dir(app_dir)
    if code is None:
        return None
    script = code / script_name
    if not script.is_file():
        return None
    py = live_python(code.parent)
    if py is None:
        return None
    return [str(py), str(script), *[str(a) for a in args]]


def hand_over(script_name, args=(), app_dir=None, settle_secs=3.0) -> bool:
    """把本次启动让给常驻区那份跑。让成了返回 True（调用方随即退出）。

    一旦跑在常驻区里，家族内部互相拉起就自动对了：frozen_boot.py_cmd 在非 frozen
    下用 `Path(sys.executable)` 旁边的 pythonw，而那正是常驻区的解释器；
    APP_DIR 也已经是常驻区。所以只需要在三个入口各问一句，不用逐个改 spawn。

    拉起来之后**盯它几秒**：常驻那份要是当场就退（解释器坏了、少个依赖、
    ImportError），就当没让成、回到原地自己跑。不盯的话 hub 让位后一退，
    38999 上没人，全队干等——宁可跑在旧位置，也不能谁都不跑。
    """
    import subprocess
    import time

    try:
        cmd = delegate_cmd(script_name, args, app_dir)
    except Exception:  # noqa: BLE001  这一句挡在 hub 启动的最前面，它自己绝不能是故障源
        return False
    if not cmd:
        return False
    try:
        from frozen_boot import hidden_popen_kwargs, script_dir
        p = subprocess.Popen(cmd, cwd=script_dir(cmd, Path(cmd[1]).parent),
                             **hidden_popen_kwargs())
    except Exception:  # noqa: BLE001  拉不起来就当没这回事，照旧在原地跑
        return False
    deadline = time.time() + max(0.0, settle_secs)
    while time.time() < deadline:
        if p.poll() is not None:
            return False  # 当场就退了 = 常驻那份跑不起来
        time.sleep(0.2)
    return True


def materialize(src_code_dir, live_root, console_root_dir=None) -> list[str]:
    """把 src 那份 rxyy MCP 代码刷进常驻区，返回实际写了哪些文件名。

    可以在常驻的 hub/MCP **正跑着的时候**重刷——Python import 完就不再持有 .py，
    覆盖是安全的；新代码等下次显式重启才生效，这正是要的语义（换装不再顺手把
    全队的 MCP 一起重启，那么升级就得是一个有人按下的动作）。

    只带代码与静态件。日志/锁/机器态一概不带：常驻区跑起来后自己会在那儿写
    hub-run.log 之类，拿源目录的旧日志盖掉纯属添乱。
    """
    src, live = Path(src_code_dir), Path(live_root)
    code = live / CODE_SUBDIR
    code.mkdir(parents=True, exist_ok=True)
    state_dir(live).mkdir(parents=True, exist_ok=True)
    written = []
    for f in sorted(src.iterdir()):
        if not f.is_file() or f.name in _NEVER_COPY:
            continue
        if f.suffix.lower() not in CODE_SUFFIXES:
            continue
        shutil.copy2(f, code / f.name)
        written.append(f.name)
    # datadir.py 认这个文件（utf-8-sig，无 BOM 也读得了），机器态就此跟着常驻区走，
    # 不再留在 dist\ 里——换装要整个 Move data\，常驻进程在那边攥着任何一个句柄
    # 都会让换装当场失败。
    (code / "datadir.txt").write_text(
        "# rxyy MCP 常驻区的机器态目录，由 live_runtime.materialize 写出\n%s\n"
        % state_dir(live), encoding="utf-8")
    # 常驻区脱离了 rxyy tools 的目录树，「那一侧在哪」只能靠记，不能靠推（见
    # console_root 的注释：靠推的那六处在常驻区全错，投递站当场读不到任务库）
    if console_root_dir:
        (code / CONSOLE_ROOT_NAME).write_text(
            "# rxyy tools 那一侧（data\\、console\\、scripts\\）的根，"
            "由 build.ps1 物化时记下\n%s\n" % Path(console_root_dir), encoding="utf-8")
    return written


def seed_state(from_dir, live_root) -> list[str]:
    """一次性把现役机器态搬进常驻区；已经有了就一个字不动。

    **只能在 hub 停着的时候调**（build.ps1 换装里 Stop-Console 之后那个窗口）。
    .sessions.json 现役 844 KB 且每几秒就重写一次，热着拷会拷到撕裂的一半，
    而它装的是全队所有 tab——07-29 那次「13 个会话连同消息一起消失」就是这个东西。
    """
    src, dst = Path(from_dir), state_dir(live_root)
    dst.mkdir(parents=True, exist_ok=True)
    if (dst / "config.json").is_file():
        return []  # 已经种过了，绝不用旧的去盖新的
    seeded = []
    for name in STATE_NAMES:
        s = src / name
        if s.is_file():
            shutil.copy2(s, dst / name)
            seeded.append(name)
    return seeded


def write_pointer(packed_code_dir, live_root) -> Path:
    """在包内那份 rxyy MCP 里写下 live-dir.txt，让它启动时把活让给常驻区。"""
    p = Path(packed_code_dir) / POINTER_NAME
    p.write_text("# rxyy MCP 常驻区（换装碰不到它），由 build.ps1 写出\n%s\n"
                 % Path(live_root), encoding="utf-8")
    return p


def smoke_import(live_root, python_exe=None) -> tuple[bool, str]:
    """常驻区那份，用它自己那个解释器真的 import 得起来吗。

    hub.py 在模块级就 `HUB = Hub()`，所以 import 一遍等于把整条依赖链走完一次。
    拿一个临时 RXYY_MCP_DATA_DIR 跑，绝不碰真机器态。

    这道闸买的是「不再有悄悄的降级」。缺一个依赖时，没有它的表现是：每次启动白拉
    一个三秒就死的子进程，然后照旧在包里跑——看上去一切正常，而你以为已经切过去了。
    有它，切换当场就说清楚，且指针根本不会被写下。
    """
    import subprocess
    import tempfile

    py = Path(python_exe) if python_exe else live_python(live_root)
    if py is None:
        return False, "常驻区没记下可用的解释器"
    code = Path(live_root) / CODE_SUBDIR
    gate, log = code / ".hub-spawn.lock", code / "hub-run.log"
    had_gate = gate.exists()
    log_was = log.stat().st_size if log.is_file() else 0
    with tempfile.TemporaryDirectory() as probe:
        env = dict(os.environ, RXYY_MCP_DATA_DIR=probe, PYTHONIOENCODING="utf-8")
        try:
            r = subprocess.run(
                [str(py), "-X", "utf8", "-c", "import hub, server, watchdog"],
                cwd=str(code), env=env, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=180)
        except Exception as e:  # noqa: BLE001
            return False, "起不来：{}".format(e)
        finally:
            _tidy_after_probe(gate, had_gate, log, log_was)
    if r.returncode == 0:
        return True, ""
    tail = "\n".join((r.stderr or r.stdout or "").strip().splitlines()[-6:])
    return False, "{} import 不起来（exit={}）：\n{}".format(py, r.returncode, tail)


def _tidy_after_probe(gate: Path, had_gate: bool, log: Path, log_was: int) -> None:
    """把自检留下的痕迹收掉。

    自检是「import 一遍 hub」，而 hub 在模块级就做两件事：touch 启动闸
    `.hub-spawn.lock`，并往 hub-run.log 写一行「hub 进程已创建…开始加载依赖」。

    闸留着会让看门狗在之后 20 秒里以为「已经有 hub 在起来了」而不去救援。日志那行
    更坏：它看起来像一次启动到一半死掉的 hub，而这个自检每次换装都跑一遍——等于
    每次换装都在日志里埋一条假线索，谁查启动问题都会追上去。
    """
    if not had_gate:
        try:
            gate.unlink(missing_ok=True)
        except OSError:
            pass
    try:
        if log.is_file() and log.stat().st_size > log_was:
            with log.open("a", encoding="utf-8") as f:
                f.write("        ↑ 上面这条来自常驻区就绪自检（import 一遍就退），"
                        "不是一次真的启动\n")
    except OSError:
        pass


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_HUB = "RxyyMcp"
RUN_WATCHDOG = "RxyyMcpWatchdog"
LEGACY_RUN_HUB = "ChijiuPlus"
LEGACY_RUN_WATCHDOG = "ChijiuPlusWatchdog"


def autostart_commands(app_dir=None):
    """无窗拉起 hub（带 --autostart，网关就绪后补开 rxyy tools）与 watchdog。
    源码仓返回 None（07-29：不许悄悄改写 Run）。

    常驻区或打包版：优先 pythonw + 常驻脚本。没有常驻区的同事拷贝才退回
    ``rxyy-tools-community.exe --run``（exe 必须是 WINDOWS 子系统，见 verify_exe_windowed）。
    """
    from frozen_boot import quoted_argv, spawn_argv
    app_dir = Path(app_dir) if app_dir else APP_DIR
    live = live_code_dir(app_dir)
    target = live if live is not None else app_dir
    if not is_live_install(target) and not getattr(sys, "frozen", False):
        return None
    code = Path(target)
    recorded = live_python(code.parent) if is_live_install(code) else None
    # hub 带 --autostart：登录自启才补开 rxyy tools 窗口。看门狗复活只传
    # --daemon，避免每次救援都再弹一扇抢焦点。
    if recorded is not None:
        return (
            quoted_argv([str(recorded), str(code / "hub.py"), "--daemon", "--autostart"]),
            quoted_argv([str(recorded), str(code / "watchdog.py")]),
        )
    return (
        quoted_argv(spawn_argv(code / "hub.py", "--daemon", "--autostart")),
        quoted_argv(spawn_argv(code / "watchdog.py")),
    )


def refresh_run_keys_if_enabled(app_dir=None) -> str:
    """开机自启若已打开，把 Run 改写成无窗命令。不主动打开自启。

    返回 rewritten / off / skipped。skipped = 源码仓，动了就会把自启指回开发目录。
    """
    cmds = autostart_commands(app_dir)
    if not cmds:
        return "skipped"
    import winreg
    enabled = False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            for name in (RUN_HUB, LEGACY_RUN_HUB):
                try:
                    winreg.QueryValueEx(k, name)
                    enabled = True
                    break
                except OSError:
                    pass
    except OSError:
        pass
    if not enabled:
        return "off"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                        winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, RUN_HUB, 0, winreg.REG_SZ, cmds[0])
        winreg.SetValueEx(k, RUN_WATCHDOG, 0, winreg.REG_SZ, cmds[1])
        for name in (LEGACY_RUN_HUB, LEGACY_RUN_WATCHDOG):
            try:
                winreg.DeleteValue(k, name)
            except OSError:
                pass
    return "rewritten"


def _cli(argv) -> int:
    """build.ps1 用的门面。

    换装脚本是 PowerShell，而这里每一条判断都在 Python 里有测试。与其在 ps1 里
    照抄一遍（三处各写一份 touch 实现就是这么长出来的），不如让 ps1 只调一句。
    """
    if len(argv) < 1:
        print("用法: live_runtime.py publish|seed|point|where|refresh-autostart <参数…>")
        return 2
    if argv[0] == "refresh-autostart":
        app = argv[1] if len(argv) > 1 else None
        state = refresh_run_keys_if_enabled(app)
        print("    开机自启 Run：{}".format(state))
        return 0 if state in ("rewritten", "off", "skipped") else 1
    if len(argv) < 2:
        print("用法: live_runtime.py publish|seed|point|where|refresh-autostart <参数…>")
        return 2
    cmd, args = argv[0], argv[1:]
    if cmd == "where":
        code = live_code_dir(args[0])
        if code is None:
            return 1
        print(code)
        return 0
    if cmd == "publish":
        packed, live = args[0], args[1]
        names = materialize(packed, live, args[2] if len(args) > 2 else None)
        py = record_python(live, sys.executable)
        if py is None:
            print("    记不下解释器（{} 不在？），常驻区不算就绪".format(sys.executable))
            return 1
        ok, why = smoke_import(live, py)
        if not ok:
            print("    常驻区那份跑不起来，指针不会写下，一切照旧：\n{}".format(why))
            return 1
        print("    常驻区已就绪：{} 个文件 → {}".format(len(names), Path(live) / CODE_SUBDIR))
        print("    解释器 {}".format(py))
        return 0
    if cmd == "seed":
        seeded = seed_state(args[0], args[1])
        print("    机器态已搬进常驻区：{}".format("、".join(seeded)) if seeded
              else "    常驻区已有机器态，一个字没动")
        return 0
    if cmd == "point":
        packed, live = args[0], args[1]
        if live_code_dir(packed) is None and not is_complete(Path(live) / CODE_SUBDIR):
            print("    常驻区不完整，拒绝写指针")
            return 1
        write_pointer(packed, live)
        print("    包内已写下指针，此后启动一律让给常驻区")
        return 0
    print("不认识的子命令: {}".format(cmd))
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
