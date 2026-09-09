# -*- coding: utf-8 -*-
"""rxyy MCP 内置「停车/发车」控制（参考 D:\\FeigeDownload\\parkgate 的 server.js）。

原理见配套 parkgate_hook.js 顶部注释与《停车控制-设计与实现.md》：
- 真正的「扣 body/放行」发生在 Cursor.exe 扩展宿主进程内的 ~/.salak/hook.js，
  保住按进程加速通道（转发若在别的进程会被判「地区不可用」）。
- 本模块只做「控制端」：原子写 ~/.salak/park.json 三字段信号 + 聚合各进程
  上报的扣住条数。纯文件信道，无第三方依赖，跨窗口天然可用。
- 集成方式：hub 的 Api 暴露 park_status/park_arm/park_launch/park_disarm，
  控制台/rxyy tools 前端调用；hub 启动时把 parkgate_hook.js 安装/更新到
  ~/.salak/hook.js，并确保 Cursor 扩展宿主入口注入了加载 stub。

epoch 单调递增，且启动时从盘上续值——绝不清零（否则发车 epoch 变小，
正在扣住的请求永远等不到放行，是文档里明确的坑）。
"""
import json
import os
import time
from pathlib import Path

import ext_host_plugin as _ehp

SALAK_DIR = Path(os.environ.get("USERPROFILE") or Path.home()) / ".salak"
PARK_JSON = SALAK_DIR / "park.json"
PARKED_DIR = SALAK_DIR / "parked"
HOOK_DST = SALAK_DIR / "hook.js"
HOOK_SRC = Path(__file__).resolve().parent / "parkgate_hook.js"
STALE_MS = 120 * 1000

_state = {"armed": False, "launchEpoch": 0, "cancelEpoch": 0, "seeded": False}


def _seed_from_disk():
    try:
        j = json.loads(PARK_JSON.read_text(encoding="utf-8"))
        _state["launchEpoch"] = int(j.get("launchEpoch") or 0)
        _state["cancelEpoch"] = int(j.get("cancelEpoch") or 0)
    except Exception:
        pass
    _state["seeded"] = True


def _write_park():
    try:
        SALAK_DIR.mkdir(parents=True, exist_ok=True)
        tmp = str(PARK_JSON) + ".tmp"
        Path(tmp).write_text(json.dumps({
            "armed": bool(_state["armed"]),
            "launchEpoch": int(_state["launchEpoch"]),
            "cancelEpoch": int(_state["cancelEpoch"]),
        }) + "\n", encoding="utf-8")
        os.replace(tmp, PARK_JSON)
        return True
    except Exception:
        return False


def parked_count():
    """聚合各扩展宿主进程上报的扣住条数（忽略 >120s 陈旧项）。"""
    total = 0
    now = time.time() * 1000
    try:
        for f in PARKED_DIR.iterdir():
            if f.suffix != ".json":
                continue
            try:
                j = json.loads(f.read_text(encoding="utf-8").lstrip("\ufeff"))
                if isinstance(j.get("count"), int) and isinstance(j.get("ts"), (int, float)) \
                        and now - j["ts"] < STALE_MS:
                    total += j["count"]
            except Exception:
                continue
    except Exception:
        pass
    return total


def ensure_seeded():
    if not _state["seeded"]:
        _seed_from_disk()


def status():
    ensure_seeded()
    wired, why = wiring_state()
    return {
        "ok": True,
        "armed": bool(_state["armed"]),
        "parked": parked_count(),
        "launchEpoch": int(_state["launchEpoch"]),
        "cancelEpoch": int(_state["cancelEpoch"]),
        "hook_installed": HOOK_DST.is_file(),
        # hook.js 在磁盘上 ≠ 停车真能生效：还得 Cursor 扩展宿主入口里那行 stub 在。
        # 重装/升级 Cursor 会把 stub 冲掉，此时按「开车」毫无反应——面板必须说实话。
        "wired": wired,
        "wiring_note": why,
    }


def arm():
    ensure_seeded()
    wired, why = wiring_state(probe=True)
    if not wired:
        return {
            "ok": False,
            "armed": bool(_state["armed"]),
            "parked": parked_count(),
            "launchEpoch": int(_state["launchEpoch"]),
            "cancelEpoch": int(_state["cancelEpoch"]),
            "hook_installed": HOOK_DST.is_file(),
            "wired": False,
            "wiring_note": why,
            "error": "开车 hook 尚未接通，不能开启：{}".format(why),
        }
    _state["armed"] = True
    _write_park()
    return status()


def launch():
    ensure_seeded()
    n = parked_count()
    _state["launchEpoch"] += 1
    _state["armed"] = False
    _write_park()
    r = status()
    r["launched"] = n
    return r


def disarm():
    ensure_seeded()
    n = parked_count()
    _state["cancelEpoch"] += 1
    _state["armed"] = False
    _write_park()
    r = status()
    r["dropped"] = n
    return r


# ---------- hook 安装（注入到 Cursor 扩展宿主） ----------
# 摘 stub 时按这个认行（新旧两种写法都含它）
STUB_MARK = "/.salak/hook.js"
# 判「装没装」只认这个。Cursor 3.x 的 extensionHostProcess.js 是 ESM
# （package.json "type":"module"，全文 import/export、零个 require），旧版 stub 写的
# `try { require(...) } catch {}` 在 ESM 里必然抛 ReferenceError 又被自己 catch 吞掉 ——
# 装了等于没装，面板还显示已接通。所以新旧 stub 必须能区分，见到旧的要换掉。
STUB_MARK_ESM = "chijiu-parkgate-esm"
STUB_LINE = (
    'try{(function(){var g=process.getBuiltinModule.bind(process),'
    'o=g("node:os"),m=g("node:module");'
    'm.createRequire(process.execPath)(o.homedir()+"/.salak/hook.js")})()}'
    'catch(e){/* chijiu-parkgate-esm */}\n'
)


def _plugin() -> "_ehp.ExtHostPlugin":
    """按当前模块全局构建 manifest（阶段2 真迁移：扩展宿主 plumbing 全走共享层）。

    每次调用现建而非模块级常量：路径全局是测试/沙箱按用例整包替换的口子，
    冻成常量会让替换失效。_wire_cache 传模块级字典，30s 接通缓存跨调用仍有效。
    """
    return _ehp.ExtHostPlugin(
        name="parkgate", home=SALAK_DIR,
        hook_src=HOOK_SRC, hook_dst=HOOK_DST,
        stub_mark=STUB_MARK_ESM, stub_line=STUB_LINE,
        own_line_groups=((STUB_MARK,),),
        identity_markers=(STUB_MARK, STUB_MARK_ESM),
        _wire_cache=_WIRE_CACHE)


def install_hook():
    """把 parkgate_hook.js 复制到 ~/.salak/hook.js（只升不降，共享层实现）。

    历史坑（现由 ext_host_plugin.install_hook 铁律锁定）：仓库里躺过一份 v3
    （包 require("http2")），而 Cursor 扩展宿主用 process.getBuiltinModule，
    包了等于没包；点「重装 hook」曾把能用的 v4 覆盖成废的 v3 还报"安装成功"。
    """
    return _ehp.install_hook(_plugin())


# 入口定位/stub 检测/留底/注入摘除全在 ext_host_plugin 共享层（阶段2 真迁移）。
# ENTRY_CACHE 与共享层 manifest.entry_cache 同路径（home/.ext-hosts.json），留作兼容引用。
ENTRY_CACHE = SALAK_DIR / ".ext-hosts.json"
_WIRE_CACHE = {"ts": 0.0, "wired": False, "note": ""}
_WIRE_TTL = 30.0


def wiring_state(probe: bool = False):
    """停车到底通不通：hook.js 在盘上 + Cursor 扩展宿主入口注入了 stub，缺一不可。

    返回 (通不通, 人话)。结果缓存 30s，避免面板轮询把磁盘读穿。
    检测走共享层，文案是开车自己的（要把"按了不会扣住请求"的后果说清楚）。
    """
    now = time.time()
    if not probe and now - _WIRE_CACHE["ts"] < _WIRE_TTL:
        return _WIRE_CACHE["wired"], _WIRE_CACHE["note"]
    pl = _plugin()
    if not HOOK_DST.is_file():
        wired, note = False, "未装 hook.js，开车不会生效"
    else:
        entries = _ehp.ext_host_entries(pl, probe=probe)
        if not entries:
            wired, note = False, "没找到 Cursor 扩展宿主入口，无法确认是否生效"
        else:
            hit = [p for p in entries if _ehp.has_stub(pl, p)]
            if hit:
                wired = True
                note = "已接通（{}/{} 个 Cursor 入口已注入）".format(len(hit), len(entries))
            elif any(_ehp.has_legacy_stub(pl, p) for p in entries):
                wired = False
                note = ("Cursor 入口里是旧版 stub（require 写法），在 ESM 入口里不执行，"
                        "开车按了也不会扣住请求 —— 点「重装 hook」换成新版，再开个新窗口")
            else:
                wired = False
                note = ("Cursor 入口未注入 stub，开车按了也不会扣住请求"
                        "（重装/升级 Cursor 会冲掉，点「重装 hook」后开个新窗口即可，"
                        "不必 Reload 现有窗口）")
    _WIRE_CACHE.update({"ts": now, "wired": wired, "note": note})
    return wired, note


BACKUP_DIR = SALAK_DIR / "ext-host-backups"


def ensure_stub():
    """确保扩展宿主入口注入了加载 hook 的 ESM stub（幂等，共享层实现）。

    共享层铁律原样生效：动安装文件前先留底、留底须是没被任何插件动过的原件
    （跨插件判定，账单痕迹也认得）、遇本插件旧版先摘再留底、只对之后新开的窗口
    生效。返回 (injected_count, note)。"""
    injected, note = _ehp.ensure_stub(_plugin())
    _WIRE_CACHE["ts"] = 0.0  # 面板别再拿 30 秒前的旧结论
    return injected, note


def remove_stub():
    """把 stub 摘掉，让 Cursor 回到没被动过的样子（需 Reload Window）。

    parkgate 语义是「随时能再开」：走共享层 restore——有干净留底按字节整文件
    还原，否则退回只摘本插件的行；保留 hook.js。返回 (restored_count, note)。"""
    restored, _from_backup, note = _ehp.restore(_plugin())
    _WIRE_CACHE["ts"] = 0.0  # 面板别再拿 30 秒前的旧结论
    return restored, note


def bootstrap():
    """hub 启动时只保持安全态，不再改写 Cursor 扩展宿主。
    fire-and-forget，失败不影响 hub。返回摘要 dict。"""
    ensure_seeded()
    ok_hook, note_hook = False, "已停用扩展宿主 hook 注入（安全恢复模式）"
    n_stub, note_stub = 0, "未修改 Cursor 安装文件"
    _state["armed"] = False  # 强制关闭；绝不自动开启
    _write_park()
    return {"hook": note_hook, "hook_ok": ok_hook, "stub": note_stub,
            "stub_injected": n_stub, "armed": False}
