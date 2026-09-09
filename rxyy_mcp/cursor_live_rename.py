# -*- coding: utf-8 -*-
"""给正开着的 Cursor 侧栏改名——不重启、不 Reload Window。

08-28 用户：「侧栏，你想想办法，你要是现在给侧栏改名要怎么做？不能重启cursor」。

前情：控制台一直是拿 write_cursor_title 直接改 state.vscdb。那只改磁盘，
对话还开在 Cursor 里时内存里的 composerDataService 才是权威，过一会儿刷回盘
就把名字盖回去（用户 08-27 截图里只有两个闲置对话留住了真名）。
真正改内存的只有 composerService.renameComposer。

拆 D:\\cursor 的 workbench.desktop.main.js 找到三件事，这个模块就建在上面：

① `composer.renameChat` 是注册过的 workbench 命令，**收一个 composerId 参数**：
     function IDb(e,t){ if(typeof t=="string") return t; ... }
   —— 传字符串就直接当 composerId 用，不依赖「当前选中哪个 tab」。
② 它 `f1:!1`（命令面板里搜不到），但**快捷键照样能绑**。
③ 它没有「顺便把新名字也传进来」的口子，名字恒定走
     quickInputService.input({prompt:"Enter new chat name", value:当前名, ignoreFocusLost:!0})
   —— 所以名字只能往那个输入框里敲。

于是：往 keybindings.json 临时插一条绑定（这个文件是热加载的，改完几百毫秒
生效，不用 Reload Window——Reload 会断 MCP，是禁手），按下它，往弹出的输入框
里敲名字回车，最后把 keybindings.json 还原。

安全垫（37 个会话同时在线，按错一下就是事故）：
- **按快捷键之前先 ctrl+shift+e 把焦点挪进文件树**。这一步用的是 Cursor 自带的
  默认绑定，不依赖我们刚写进去的那条热加载成没成。万一临时绑定还没生效、
  或者 renameChat 提前 return（"No chat tab selected to rename."），后面的
  ctrl+a／打字／回车就落在文件树上——顶多多开几个文件，什么都不会坏。
  不垫这一下，那串按键会落进当时有焦点的地方：编辑器（改坏代码）、聊天输入框
  （回车 = 替全队发一条消息出去）、最坏是终端（回车 = 执行一条命令）。
- 全程不碰剪贴板：名字用 SendInput 的 KEYEVENTF_UNICODE 逐码元发，
  中文直接进得去，也不会把用户正复制着的东西冲掉。
- 不发 Esc。Cursor 里 Esc 会打断正在生成的 agent。
- 敲键之前确认前台窗口真的是 Cursor.exe，不是就直接放弃。
- keybindings.json 一律在 finally 里还原，中途抛异常也不会留下野快捷键。
"""

import ctypes
import json
import os
import re
import time
from ctypes import wintypes
from pathlib import Path

RENAME_COMMAND = "composer.renameChat"
_MARK = "rxyy MCP-live-rename"

# 一次改一批时给每个 tab 分一个 F 键，keybindings.json 只写一次。
# 早先是「写一条→按→还原→再写下一条」，一轮 13 秒还不稳：Cursor 的文件监视
# 会把连着的几次改动并成一次，第二条绑定可能根本没热加载上，那时按下去要么
# 没反应、要么还指着上一个 composerId——改到隔壁 tab 头上去。
CHORD_KEYS = ["f9", "f10", "f11", "f12", "f1", "f2", "f3", "f4",
              "f5", "f6", "f7", "f8"]
CHORD_PREFIX = "ctrl+alt+shift+"
CHORD = CHORD_PREFIX + CHORD_KEYS[0]
BATCH_MAX = len(CHORD_KEYS)

# keybindings.json 热加载要一点时间；Cursor 实测 ~150ms，给到 900ms 留余量。
KEYBIND_RELOAD_WAIT = 0.9
# 按下快捷键到输入框拿到焦点。
INPUT_OPEN_WAIT = 0.55
# 刚把 Cursor 从别的程序底下提上来时，Electron 还要一会儿才真正收键盘。
FOCUS_SETTLE = 0.45


# ---------------------------------------------------------------- keybindings

def keybindings_path(appdata=None):
    roaming = Path(appdata or os.environ.get("APPDATA")
                   or Path.home() / "AppData" / "Roaming")
    return roaming / "Cursor" / "User" / "keybindings.json"


_LINE_COMMENT = re.compile(r"^\s*//.*$", re.M)
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def parse_keybindings(text):
    """keybindings.json 是 JSONC（Cursor 自己生成的头一行就是 // 注释）。

    读不出来就当空表——绝不把用户的绑定当成「解析失败=没有」写回去，
    调用方负责原样还原原文。
    """
    if not (text or "").strip():
        return []
    stripped = _BLOCK_COMMENT.sub("", _LINE_COMMENT.sub("", text))
    stripped = re.sub(r",(\s*[\]}])", r"\1", stripped)
    try:
        data = json.loads(stripped)
    except Exception:
        return []
    return data if isinstance(data, list) else []


def binding_for(composer_id, chord=CHORD):
    """临时绑定：composerId 当参数直接传给 composer.renameChat。

    不带 when——这条只活几百毫秒，且要在任何焦点下都按得动。
    """
    return {
        "key": chord,
        "command": RENAME_COMMAND,
        "args": composer_id,
        _MARK: True,
    }


def with_temp_bindings(text, composer_ids):
    """把原文里我们上次可能漏下的绑定清掉，再给每个 composerId 插一条，返回新全文。

    返回 (新全文, [(composerId, 和弦)])。超出 BATCH_MAX 的直接不发绑定，
    调用方分批。
    """
    existing = [b for b in parse_keybindings(text)
                if isinstance(b, dict) and not b.get(_MARK)]
    pairs = []
    for cid, key in zip(composer_ids, CHORD_KEYS):
        chord = CHORD_PREFIX + key
        existing.append(binding_for(cid, chord))
        pairs.append((cid, chord))
    return json.dumps(existing, ensure_ascii=False, indent=4) + "\n", pairs


def with_temp_binding(text, composer_id, chord=CHORD):
    existing = [b for b in parse_keybindings(text)
                if isinstance(b, dict) and not b.get(_MARK)]
    existing.append(binding_for(composer_id, chord))
    return json.dumps(existing, ensure_ascii=False, indent=4) + "\n"


# ------------------------------------------------------------------- win32

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
VK_CONTROL, VK_MENU, VK_SHIFT = 0x11, 0x12, 0x10
VK_A, VK_E, VK_RETURN = 0x41, 0x45, 0x0D
VK_F = {"f{}".format(i): 0x70 + i - 1 for i in range(1, 13)}
SW_RESTORE = 9
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _send(inputs):
    arr = (_INPUT * len(inputs))(*inputs)
    _user32.SendInput(len(inputs), arr, ctypes.sizeof(_INPUT))


def _vk(code, up=False):
    return _INPUT(type=INPUT_KEYBOARD,
                  u=_INPUTUNION(ki=_KEYBDINPUT(
                      wVk=code, wScan=0,
                      dwFlags=KEYEVENTF_KEYUP if up else 0,
                      time=0, dwExtraInfo=None)))


def _unicode_unit(unit, up=False):
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    return _INPUT(type=INPUT_KEYBOARD,
                  u=_INPUTUNION(ki=_KEYBDINPUT(
                      wVk=0, wScan=unit, dwFlags=flags,
                      time=0, dwExtraInfo=None)))


def press_chord(chord=CHORD):
    """按下 ctrl+alt+shift+fN。只认这一族和弦，别的形状直接拒。"""
    key = chord.rsplit("+", 1)[-1].lower()
    if not chord.startswith(CHORD_PREFIX) or key not in VK_F:
        raise ValueError("unsupported chord: {}".format(chord))
    mods = [VK_CONTROL, VK_MENU, VK_SHIFT]
    seq = [_vk(m) for m in mods] + [_vk(VK_F[key]), _vk(VK_F[key], up=True)]
    seq += [_vk(m, up=True) for m in reversed(mods)]
    _send(seq)


def press_focus_explorer():
    """ctrl+shift+e = workbench.view.explorer，Cursor 自带的默认绑定。

    这是安全垫，不是功能：见模块 docstring。用默认绑定的意义就在于它不依赖
    我们刚写进 keybindings.json 的那条有没有热加载成。
    """
    mods = [VK_CONTROL, VK_SHIFT]
    seq = [_vk(m) for m in mods] + [_vk(VK_E), _vk(VK_E, up=True)]
    seq += [_vk(m, up=True) for m in reversed(mods)]
    _send(seq)


def press_select_all():
    _send([_vk(VK_CONTROL), _vk(VK_A), _vk(VK_A, up=True),
           _vk(VK_CONTROL, up=True)])


def utf16_units(text):
    """按 UTF-16 码元拆开。KEYEVENTF_UNICODE 一次只收一个码元，
    BMP 外的字符（emoji）得按代理对分两次发。"""
    raw = text.encode("utf-16-le")
    return [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]


def type_text(text):
    """逐码元发 KEYEVENTF_UNICODE，中文直接进；不经剪贴板。"""
    seq = []
    for unit in utf16_units(text):
        seq.append(_unicode_unit(unit))
        seq.append(_unicode_unit(unit, up=True))
    if seq:
        _send(seq)


def press_enter():
    _send([_vk(VK_RETURN), _vk(VK_RETURN, up=True)])


def _exe_of_window(hwnd):
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        _kernel32.CloseHandle(h)


def _window_title(hwnd):
    n = _user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    _user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def cursor_windows():
    """所有可见的 Cursor.exe 顶层窗口，返回 [(hwnd, 标题)]。"""
    found = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if not _user32.IsWindowVisible(hwnd):
            return True
        title = _window_title(hwnd)
        if not title:
            return True
        if Path(_exe_of_window(hwnd)).name.lower() == "cursor.exe":
            found.append((hwnd, title))
        return True

    _user32.EnumWindows(proto(cb), 0)
    return found


def pick_window(windows, workspace_hint=""):
    """挑要改名的那个窗口：标题里带工作区名的优先，否则第一个。"""
    hint = (workspace_hint or "").strip()
    if hint:
        for hwnd, title in windows:
            if hint in title:
                return hwnd, title
    return windows[0] if windows else (None, "")


def focus_window(hwnd):
    """尽力把 Cursor 提到前台。提不上来返回 False——调用方还得再问一次
    foreground_is_cursor()，因为「本来就在前台」同样算数。

    Windows 的前台锁：只有当前前台窗口所属的那个线程才放得动前台。所以要
    AttachThreadInput 到**当前前台窗口**的线程（早先错挂到目标窗口的线程上，
    等于没挂）。跨完整性级别时这一手也会失败——Cursor 在这台机器上是「以管理员
    身份」跑的——那就老实返回 False，别硬抢。
    """
    if _user32.IsIconic(hwnd):
        _user32.ShowWindow(hwnd, SW_RESTORE)
    cur = _kernel32.GetCurrentThreadId()
    fg = _user32.GetForegroundWindow()
    fg_thread = _user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = bool(fg_thread and fg_thread != cur
                    and _user32.AttachThreadInput(cur, fg_thread, True))
    try:
        _user32.BringWindowToTop(hwnd)
        _user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            _user32.AttachThreadInput(cur, fg_thread, False)
    return _user32.GetForegroundWindow() == hwnd


def foreground_is_cursor():
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return False
    return Path(_exe_of_window(hwnd)).name.lower() == "cursor.exe"


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def user_idle_secs():
    """用户多少秒没碰键盘鼠标；查不到（非 Windows / 调用失败）返回 None。

    自动改名要抢一两秒键盘，只能挑用户手不在键盘上的时候下手——这是那道闸的
    依据。GetLastInputInfo 给的是本会话最近一次输入的 tick，与 GetTickCount
    同一时基，都是 32 位毫秒计数（49.7 天回绕一次），按无符号减法算差值。
    """
    if os.name != "nt":
        return None
    try:
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not _user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        now = _kernel32.GetTickCount() & 0xFFFFFFFF
        return ((now - info.dwTime) & 0xFFFFFFFF) / 1000.0
    except Exception:
        return None


# -------------------------------------------------------------------- driver

def rename_chats_live(renames, workspace_hint="", appdata=None, log=None):
    """让正在跑的 Cursor 把这批对话的侧栏名改掉。renames = [(composerId, 新名)]。

    返回真的按下去了的 composerId 集合。只做「按得动就按」，按不动一律放弃让
    调用方回落到写盘那条老路——这条路要抢用户的键盘几百毫秒，绝不能因为它失败
    就把改名整件事卡死。

    注意「按下去了」不等于「Cursor 认了」：composer.renameChat 是异步的，
    名字落回 state.vscdb 还要几秒（实测 5s）。要确认得回头读库。
    """
    def say(msg):
        if log:
            log(msg)

    todo = [(cid, (name or "").strip()[:40])
            for cid, name in (renames or []) if cid and (name or "").strip()]
    if not todo:
        return set()
    if os.name != "nt":
        say("侧栏改名跳过：只在 Windows 上实现了")
        return set()
    if len(todo) > BATCH_MAX:
        say("侧栏改名只处理前 {} 个，其余下一轮".format(BATCH_MAX))
        todo = todo[:BATCH_MAX]

    windows = cursor_windows()
    if not windows:
        say("侧栏改名放弃：没找到 Cursor 窗口")
        return set()
    hwnd, wtitle = pick_window(windows, workspace_hint)

    kb = keybindings_path(appdata)
    try:
        original = kb.read_text(encoding="utf-8") if kb.is_file() else ""
    except Exception as exc:
        say("侧栏改名放弃：读不了 keybindings.json {!r}".format(exc))
        return set()

    done = set()
    try:
        text, pairs = with_temp_bindings(original, [cid for cid, _ in todo])
        kb.parent.mkdir(parents=True, exist_ok=True)
        kb.write_text(text, encoding="utf-8")
        time.sleep(KEYBIND_RELOAD_WAIT)

        # 抢焦点失败不等于干不了。Cursor 在这台机器上是「以管理员身份」跑的，
        # 而 hub 是看门狗拉起来的普通进程——Windows 的 UIPI／前台锁不让它把
        # 那个窗口提到前台（10:50 实测：SetForegroundWindow 直接没反应）。
        # 但只要**已经**有个 Cursor 窗口在前台（用户就是在 Cursor 里点的按钮，
        # 控制台 UI 本身就是 Cursor 里的 iframe），按键照样送得进去。
        # composer.renameChat 收的是 composerId，不挑窗口，所以不必非得是我们
        # 挑中的那一个。
        focus_window(hwnd)
        if not foreground_is_cursor():
            say("侧栏改名放弃：Cursor 窗口没在前台，也提不上来（{}）。"
                "先手动点一下 Cursor 窗口再按，或在 Cursor 的终端里跑 "
                "scripts/fix-cursor-sidebar.py".format(wtitle))
            return done
        time.sleep(FOCUS_SETTLE)
        press_focus_explorer()
        time.sleep(0.25)

        for (cid, title), (_, chord) in zip(todo, pairs):
            if not foreground_is_cursor():
                say("侧栏改名中止：前台窗口已经不是 Cursor 了")
                break
            press_chord(chord)
            time.sleep(INPUT_OPEN_WAIT)
            if not foreground_is_cursor():
                say("侧栏改名中止：按下快捷键后焦点跑了")
                break
            press_select_all()
            type_text(title)
            press_enter()
            done.add(cid)
            time.sleep(0.2)
        return done
    except Exception as exc:
        say("侧栏改名异常 {!r}".format(exc))
        return done
    finally:
        try:
            if original:
                kb.write_text(original, encoding="utf-8")
            elif kb.is_file():
                kb.unlink()
        except Exception as exc:
            say("keybindings.json 还原失败（有野快捷键残留）{!r}".format(exc))


def rename_chat_live(composer_id, title, workspace_hint="", appdata=None,
                     log=None):
    return composer_id in rename_chats_live(
        [(composer_id, title)], workspace_hint=workspace_hint,
        appdata=appdata, log=log)
