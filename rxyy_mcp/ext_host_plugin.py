# -*- coding: utf-8 -*-
"""扩展宿主插件契约 —— 第一版 plugin manifest（hub 拆分第三刀，2026-08-12）。

## 这是什么

`parkgate.py`（停车/发车）与 `billgate.py`（账单 hook）是同一副骨架：都往 Cursor
扩展宿主入口 `extensionHostProcess.js` 注入一行 ESM stub，让宿主进程加载各自的
`hook.js`，再靠一份原子写的信号 JSON 与 hook 通信。本模块把这副**共享骨架**反推成
一个 `ExtHostPlugin` manifest + 一组以 manifest 为参数的生命周期函数，作为开源
「插件 API」的第一版种子（见 `docs/开源产品化蓝图-2026-08-12.md` 阶段 2）。

## 边界（第三刀 A 案：只抛接口，不改行为）

**本模块目前不被任何运行时代码 import**。parkgate/billgate 仍各自持有自己那份
实现，行为、契约测试、沙箱表现一字未动。这里是「可落地的接口 + 它自己的测试」，
供阶段 2 正式迁移时逐个把两模块的扩展宿主 plumbing 换成对本模块的调用。

## 为什么是「注入 Cursor 安装文件」这类插件先行

这类插件动的是 Cursor 的 `extensionHostProcess.js`——最危险的一类（改坏了要重装
Cursor）。历史事故与铁律已沉淀在 parkgate/billgate 里，原样固化进 manifest：

1. **只升不降**：已装 hook 版本更高就不覆盖（曾把能用的 v4 覆盖成废的 v3）。
2. **动安装文件前先留底**：按字节留底到插件自己家，`restore()` 能整文件还原。
3. **多插件共存只摸自己那行**：`strip_own_stub` 只删本插件的 stub 行，绝不误删
   别的插件的（停车与账单同时注入时，卸载账单不能把停车摘掉）。
4. **留底须是「没被任何插件动过」的原件**：留底里若含任一已知插件标记就不认，
   否则日后还原会把别的插件的 stub 一起还原回来。
5. **只对之后新开的窗口生效**：已开窗口的扩展宿主进程不会重读入口文件。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

# 扩展宿主入口相对 Cursor `resources/app` 的固定位置（两模块共用这一份常量）
REL_ENTRY = Path("out") / "vs" / "workbench" / "api" / "node" / "extensionHostProcess.js"

# 全部已注册插件的「身份子串」并集：判断一份留底是否「没被任何插件动过」用它。
# 每个 manifest 在构造时把自己的 identity_markers 注册进来（跨插件安全的单一真源）。
#
# 基线种子（阶段2 真迁移，2026-08-13）：两个首批插件的身份子串直接种在这里——
# 运行时常常只 import 其中一个 gate（如 hub_api.bill_* 只拉起 billgate），若只靠
# manifest 构造时注册，另一家的痕迹就认不出来，被它动过的入口会被误当"原件"留底，
# 日后按这份留底还原会把对方的 stub 一起搬回来。字符串须与 parkgate.py / billgate.py
# 顶部的 STUB_MARK* 常量保持一致（改任一处必同步）。
_ALL_IDENTITY_MARKERS: set[str] = {
    "/.salak/hook.js", "chijiu-parkgate-esm",   # parkgate 全代际
    "chijiu-billgate", "billhook.js",            # billgate 全代际
}


class PluginError(RuntimeError):
    """插件对 Hub 暴露的可预期操作失败。"""


class PluginCleanupError(PluginError):
    """卸载未能确认全部残留已清理，不能宣称卸载成功。"""


@dataclass(frozen=True)
class ExtHostPlugin:
    """一个「注入 Cursor 扩展宿主」的插件清单。

    字段就是 parkgate/billgate 之间**唯一真正不同**的那几处，其余逻辑全共享。

    - name：插件名（日志/缓存键）。
    - home：插件独占目录（信号 JSON、hook.js、留底都在这儿；如 ~/.salak）。
    - hook_src：随包携带的 hook.js 源文件。
    - hook_dst：hook.js 落地路径（注意 park=hook.js、bill=billhook.js，名字不同，
      故显式给而非从 src 推）。
    - stub_mark：**当前版本**的唯一 ESM 标记（判「已装」只认它）。
    - stub_line：注入到入口末尾的整行（内含 stub_mark，负责 require 加载 hook_dst）。
    - own_line_groups：判定「某一行是本插件的 stub」的规则，OR-of-AND——外层任一
      内层元组里的子串**全部命中**即算本插件的行（含当前版与历史版）。摘 stub 时
      按它过滤，从而只摸自己那行。
    - identity_markers：本插件「出现在任何字节里就代表它掺和过」的子串集合，
      供 `is_pristine_backup` 跨插件判定原件。
    """

    name: str
    home: Path
    hook_src: Path
    hook_dst: Path
    stub_mark: str
    stub_line: str
    own_line_groups: tuple[tuple[str, ...], ...]
    identity_markers: tuple[str, ...]
    _wire_cache: dict = field(default_factory=lambda: {"ts": 0.0, "wired": False, "note": ""},
                              compare=False, repr=False)

    WIRE_TTL = 30.0

    def __post_init__(self):
        _ALL_IDENTITY_MARKERS.update(self.identity_markers)

    @property
    def entry_cache(self) -> Path:
        return self.home / ".ext-hosts.json"

    @property
    def backup_dir(self) -> Path:
        return self.home / "ext-host-backups"


# ---------------- 无副作用小工具（两模块逐字相同，收拢到这里） ----------------
def atomic_write_bytes(path: Path, data: bytes) -> None:
    """同目录写临时文件再替换，避免扩展宿主读到半份脚本。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            Path(tmp_name).unlink(missing_ok=True)
        except OSError:
            pass


def tail(path: Path, n: int = 8192) -> str:
    """读文件尾部若干字节——stub 是 append 到末尾的，入口文件却有数 MB。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - n))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


def hook_version(text: str) -> int:
    """从 hook 源码抠主版本号（HOOK_VERSION = "v4-..." → 4）；抠不到算 0。"""
    m = re.search(r'HOOK_VERSION\s*=\s*"v(\d+)', text or "")
    return int(m.group(1)) if m else 0


def cursor_app_dirs_from_process() -> list[Path]:
    """从运行中的 Cursor.exe 反推 resources/app（用户装哪都找得到）。"""
    dirs = []
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process Cursor -ErrorAction SilentlyContinue | "
             "Select-Object -ExpandProperty Path -Unique"],
            capture_output=True, text=True, timeout=8,
            creationflags=0x08000000).stdout
        for ln in out.splitlines():
            ln = ln.strip()
            if ln.lower().endswith("cursor.exe"):
                app = Path(ln).parent / "resources" / "app"
                if app.is_dir():
                    dirs.append(app)
    except Exception:
        pass
    return dirs


def ext_host_entries(plugin: ExtHostPlugin, probe: bool = True) -> list[Path]:
    """定位 Cursor 扩展宿主入口（可能多版本并存）。

    probe=True：起 powershell 从运行进程反推并刷新缓存；probe=False：只读静态候选
    与上次缓存，供高频轮询的 status 使用（不起子进程）。
    """
    seen, out = set(), []
    bases = list(cursor_app_dirs_from_process()) if probe else []
    if not probe:
        try:
            for s in json.loads(plugin.entry_cache.read_text(encoding="utf-8")):
                p = Path(s)
                if p.is_file() and str(p) not in seen:
                    seen.add(str(p))
                    out.append(p)
        except (OSError, ValueError, TypeError):
            pass
    la = os.environ.get("LOCALAPPDATA")
    if la:
        bases.append(Path(la) / "Programs" / "cursor" / "resources" / "app")
    pf = os.environ.get("PROGRAMFILES")
    if pf:
        bases.append(Path(pf) / "cursor" / "resources" / "app")
    for drive in ("C", "D", "E"):
        bases.append(Path(r"{}:\cursor\resources\app".format(drive)))
    for b in bases:
        p = b / REL_ENTRY
        try:
            if p.is_file() and str(p) not in seen:
                seen.add(str(p))
                out.append(p)
        except OSError:
            continue
    if probe and out:
        try:
            plugin.home.mkdir(parents=True, exist_ok=True)
            plugin.entry_cache.write_text(json.dumps([str(p) for p in out], ensure_ascii=False),
                                          encoding="utf-8")
        except OSError:
            pass
    return out


# ---------------- hook.js 安装（只升不降） ----------------
def install_hook(plugin: ExtHostPlugin) -> tuple[bool, str]:
    """把 hook_src 复制到 hook_dst（每次覆盖=升级）。返回 (ok, note)。

    只准升级不准降级：已部署的 hook 若比源码新就不动它（曾把能用的 v4 覆盖成
    包了 http2 却不生效的 v3，还报「安装成功」）。
    """
    try:
        plugin.home.mkdir(parents=True, exist_ok=True)
        if not plugin.hook_src.is_file():
            return False, "找不到 {} 源文件".format(plugin.hook_src.name)
        src = plugin.hook_src.read_text(encoding="utf-8")
        if plugin.hook_dst.is_file():
            cur = plugin.hook_dst.read_text(encoding="utf-8")
            v_src, v_cur = hook_version(src), hook_version(cur)
            if v_cur > v_src:
                return False, ("已装的 {} 是 v{}，比源码 v{} 新，不覆盖"
                               "（避免降级成不生效的旧版）".format(
                                   plugin.hook_dst.name, v_cur, v_src))
            if cur == src:
                return True, "{} 已是最新（v{}）".format(plugin.hook_dst.name, v_cur)
        atomic_write_bytes(plugin.hook_dst, src.encode("utf-8"))
        if plugin.hook_dst.read_bytes() != src.encode("utf-8"):
            return False, "安装 {} 失败：写入后校验不一致".format(plugin.hook_dst.name)
        return True, "{} 已安装/更新（v{}）".format(plugin.hook_dst.name, hook_version(src))
    except Exception as e:
        return False, "安装 {} 失败: {}".format(plugin.hook_dst.name, e)


# ---------------- stub 检测（当前版 / 历史版 / 是否本插件的行） ----------------
def has_stub(plugin: ExtHostPlugin, path: Path) -> bool:
    """入口是否注入了本插件**当前版本**的 stub（判「已装」只认它）。"""
    return plugin.stub_mark in tail(path)


def is_own_line(plugin: ExtHostPlugin, line: bytes) -> bool:
    """某一行是不是本插件的 stub（当前版或历史版）。跨插件不误判的关键。"""
    text = line.decode("utf-8", "replace")
    return any(all(m in text for m in group) for group in plugin.own_line_groups)


def has_legacy_stub(plugin: ExtHostPlugin, path: Path) -> bool:
    """入口里有本插件的**旧版** stub 但没有当前版——旧版在 ESM 入口里不执行，
    装了等于没装，还让「装没装」的判断失真，遇到要换掉。"""
    if has_stub(plugin, path):
        return False
    return any(is_own_line(plugin, ln) for ln in tail(path).encode("utf-8").splitlines())


def is_pristine_backup(data: bytes) -> bool:
    """一份留底是不是「没被任何已注册插件动过」的原件。

    只要含任一插件的身份标记就不认——按坏留底还原会把别的插件的 stub 一起搬回来。
    """
    text = data.decode("utf-8", "replace")
    return not any(m in text for m in _ALL_IDENTITY_MARKERS)


def backup_path(plugin: ExtHostPlugin, p: Path) -> Path:
    """留底放插件自己家，按全路径哈希命名（多版本入口并存不打架）。"""
    tag = hashlib.md5(str(p).lower().encode("utf-8")).hexdigest()[:12]
    return plugin.backup_dir / "{}.{}.bak".format(p.name, tag)


# ---------------- 接通状态 ----------------
def wiring_state(plugin: ExtHostPlugin, probe: bool = False) -> tuple[bool, str]:
    """插件到底通不通：hook.js 在盘上 + Cursor 入口注入了当前版 stub，缺一不可。
    结果缓存 30s，避免面板轮询把磁盘读穿。"""
    now = time.time()
    c = plugin._wire_cache
    if not probe and now - c["ts"] < plugin.WIRE_TTL:
        return c["wired"], c["note"]
    if not plugin.hook_dst.is_file():
        wired, note = False, "未装 {}，插件不会生效".format(plugin.hook_dst.name)
    else:
        entries = ext_host_entries(plugin, probe=probe)
        if not entries:
            wired, note = False, "没找到 Cursor 扩展宿主入口，无法确认是否生效"
        else:
            hit = [p for p in entries if has_stub(plugin, p)]
            if hit:
                wired = True
                note = "已接通（{}/{} 个 Cursor 入口已注入）".format(len(hit), len(entries))
            elif any(has_legacy_stub(plugin, p) for p in entries):
                wired = False
                note = ("Cursor 入口里是旧版 stub，在 ESM 入口里不执行——"
                        "重装 hook 换成新版，再开个新窗口")
            else:
                wired = False
                note = ("Cursor 入口未注入 stub（重装/升级 Cursor 会冲掉，"
                        "重装 hook 后开个新窗口即可，不必 Reload 现有窗口）")
    c.update({"ts": now, "wired": wired, "note": note})
    return wired, note


def invalidate_wire_cache(plugin: ExtHostPlugin) -> None:
    plugin._wire_cache["ts"] = 0.0


# ---------------- 注入 / 摘除 ----------------
def strip_own_stub(plugin: ExtHostPlugin, p: Path) -> bool:
    """只删本插件的 stub 行，其余字节原样保留。删了返回 True。

    必须走二进制：read_text/write_text 在 Windows 上会把全文换行翻成 CRLF，
    数 MB 的入口会凭空长几百字节，从此再也对不回原始哈希。
    """
    try:
        raw = p.read_bytes()
    except OSError as e:
        raise PluginCleanupError("读取入口失败（{}）: {}".format(p, e)) from e
    if not any(is_own_line(plugin, ln) for ln in raw.splitlines()):
        return False
    kept = [ln for ln in raw.splitlines(keepends=True) if not is_own_line(plugin, ln)]
    while kept and kept[-1].strip() == b"":  # 摘掉后末尾常剩我们加的空行
        kept.pop()
    try:
        p.write_bytes(b"".join(kept))
        return True
    except OSError as e:
        raise PluginCleanupError("移除 stub 失败（{}）: {}".format(p, e)) from e


def ensure_stub(plugin: ExtHostPlugin) -> tuple[int, str]:
    """确保各入口注入了本插件当前版 ESM stub（幂等）。只对之后新开的窗口生效。

    动安装文件前先按字节留底（留底须是没被任何插件动过的原件）；遇到本插件旧版
    stub 先摘干净再留底，否则留底带着旧 stub，将来还原会把它搬回来。
    返回 (injected, note)。
    """
    # 先走缓存/静态路径。点「安装开车」走 http 网关，普通按钮 15s 超时；
    # Get-Process Cursor 的 powershell 曾经 timeout=20，前端会先 abort 报「安装失败」。
    entries = ext_host_entries(plugin, probe=False)
    if not entries:
        entries = ext_host_entries(plugin, probe=True)
    if not entries:
        return 0, "未找到 Cursor 扩展宿主入口（跳过；hook 已就位，装 Cursor 后重跑一次）"
    injected, replaced = 0, 0
    for p in entries:
        try:
            if has_stub(plugin, p):
                continue
            if has_legacy_stub(plugin, p):
                strip_own_stub(plugin, p)  # 先摘旧版再留底，别把旧 stub 留进原件
                replaced += 1
            bak = backup_path(plugin, p)
            if not bak.is_file() and is_pristine_backup(p.read_bytes()):
                plugin.backup_dir.mkdir(parents=True, exist_ok=True)
                bak.write_bytes(p.read_bytes())
            atomic_write_bytes(p, p.read_bytes() + b"\n" + plugin.stub_line.encode("utf-8"))
            injected += 1
        except PluginCleanupError:
            raise
        except Exception:
            continue
    invalidate_wire_cache(plugin)
    note = "已注入 {} 个扩展宿主入口（对之后新开的窗口生效，现有窗口不受影响）".format(injected)
    if replaced:
        note += "；其中 {} 个换掉了不生效的旧版 stub".format(replaced)
    return injected, note


def restore(plugin: ExtHostPlugin) -> tuple[int, int, str]:
    """把 Cursor 恢复成没被本插件动过的样子——有干净留底按字节整文件还原，
    否则退而求其次摘掉本插件的 stub 行。保留 hook.js（parkgate 风格：随时能再开）。
    返回 (restored, from_backup, note)。需 Reload Window。"""
    entries = ext_host_entries(plugin)
    restored, from_backup = 0, 0
    for p in entries:
        try:
            bak = backup_path(plugin, p)
            if bak.is_file() and is_pristine_backup(bak.read_bytes()):
                if p.read_bytes() != bak.read_bytes():
                    p.write_bytes(bak.read_bytes())
                    from_backup += 1
                    restored += 1
                continue
            if strip_own_stub(plugin, p):
                restored += 1
        except PluginCleanupError:
            raise
        except Exception:
            continue
    invalidate_wire_cache(plugin)
    return restored, from_backup, "已还原 {} 个入口（其中 {} 个按留底整文件还原），需 Reload Window".format(
        restored, from_backup)


def uninstall(plugin: ExtHostPlugin, owned_files=()) -> tuple[int, int, str]:
    """完整卸载（billgate 风格）：摘掉本插件 stub + 删除本插件独占文件。

    只清理本插件；别的插件的文件、状态、留底一律不碰。任何未确认的清理失败抛
    PluginCleanupError，以便 Hub 返回 ok=false，而不是假称卸载完成。
    owned_files：本插件写出的、卸载时该删的文件路径清单（信号 JSON、hook.js、日志…）。
    返回 (removed_stub_entries, removed_files, note)。需 Reload Window。
    """
    try:
        entries = ext_host_entries(plugin)
    except Exception as e:
        raise PluginCleanupError("定位 Cursor 入口失败，无法确认 stub 已清理: {}".format(e)) from e
    removed = sum(1 for p in entries if strip_own_stub(plugin, p))
    invalidate_wire_cache(plugin)
    files_removed = 0
    for f in owned_files:
        try:
            existed = Path(f).is_file()
            Path(f).unlink(missing_ok=True)
            files_removed += int(existed)
        except OSError as e:
            raise PluginCleanupError("删除插件文件失败（{}）: {}".format(f, e)) from e
    return (removed, files_removed,
            "已卸载 {}：摘掉 {} 个入口、清理 {} 个文件，其它插件未动，需 Reload Window".format(
                plugin.name, removed, files_removed))
