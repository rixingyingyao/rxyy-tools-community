# -*- coding: utf-8 -*-
"""rxyy MCP 内置「账单 hook」控制。

它是干嘛的：Cursor 会周期性地问服务器「额度用完没 / 有没有待付账单 / 硬上限多少」
（GetHardLimit / GetUsageLimitStatusAndActiveGrants / ListBlockingCheckoutInvoices …），
本模块把这些调用在 Cursor.exe 扩展宿主进程内截下来，供本机沙箱观察/实验。

它不拦 Agent 生成，也不能绕过服务端计费或恢复已耗尽额度。AgentService.Run 的
Connect/WS/SSE 请求一旦发出，服务端会在推理过程中计量；客户端不存在另一条可单独
掐掉而仍保留生成结果的「出账请求」。需要暂停 Agent 请求由独立的开车功能负责。

账单功能自己的三条铁律：
1. **默认关**。bill.json 里 enabled=false 时 hook 完全透传，对 Cursor 零影响——
   装了不开，跟没装一样，绝不碰正常流量。
2. **控制端只写信号文件**。本模块（在 hub 进程里跑）只原子写
   ``~/.rxyy-billgate/bill.json``，真正的拦截发生在扩展宿主里的
   ``~/.rxyy-billgate/billhook.js``，纯文件信道、无第三方依赖。
3. **动 Cursor 安装文件前先留底**。注入的 stub 行有独立 mark，卸载只摘自己那行，
   不碰开车注入的那行；脚本、状态、日志和留底均由账单功能单独维护。

mode 三档：
- observe（默认）：只把看到的账单请求记到 ~/.rxyy-billgate/bill-seen/<pid>.json 供面板计数，
  请求照常放行——用来看清 Cursor 到底轮询哪些接口、多久一次。
- block：直接 RST 掉这些请求（谨慎用，可能让 Cursor 相关面板转圈）。
- debug：observe 之上再把扩展宿主发出的**每一条** h2 路径记进 ~/.rxyy-billgate/billhook.log，
  带 count 和 delta_ms。用来回答「账单接口到底走不走扩展宿主、多久一次」——只有走，
  这个 hook 才可能拦得到；不走的话拦截点就选错了进程，得换别的路子。
"""
import json
import os
import time
from pathlib import Path

import ext_host_plugin as _ehp

# 账单是独立功能：不读取/写入开车或其它控制器的目录和状态。
BILLGATE_DIR = Path(os.environ.get("USERPROFILE") or Path.home()) / ".rxyy-billgate"
BILL_JSON = BILLGATE_DIR / "bill.json"
SEEN_DIR = BILLGATE_DIR / "bill-seen"
HOOK_DST = BILLGATE_DIR / "billhook.js"
HOOK_SRC = Path(__file__).resolve().parent / "billgate_hook.js"
BILL_TMP = Path(str(BILL_JSON) + ".tmp")
BILL_LOG = BILLGATE_DIR / "billhook.log"
BILL_LOG_ROTATED = BILLGATE_DIR / "billhook.log.1"
ENTRY_CACHE = BILLGATE_DIR / ".ext-hosts.json"
BACKUP_DIR = BILLGATE_DIR / "ext-host-backups"
STALE_MS = 120 * 1000

_VALID_MODES = ("observe", "block", "debug")
_state = {"enabled": False, "mode": "observe", "seeded": False}


class BillgateError(RuntimeError):
    """billgate 对 Hub 暴露的可预期操作失败。"""


class BillgateCleanupError(BillgateError):
    """卸载未能确认全部残留都已清理，不能宣称卸载成功。"""


def _seed_from_disk():
    try:
        j = json.loads(BILL_JSON.read_text(encoding="utf-8"))
        _state["enabled"] = bool(j.get("enabled"))
        m = j.get("mode")
        _state["mode"] = m if m in _VALID_MODES else "observe"
    except Exception:
        pass
    _state["seeded"] = True


def ensure_seeded():
    if not _state["seeded"]:
        _seed_from_disk()


def _write_bill():
    try:
        BILLGATE_DIR.mkdir(parents=True, exist_ok=True)
        BILL_TMP.write_text(json.dumps({
            "enabled": bool(_state["enabled"]),
            "mode": _state["mode"] if _state["mode"] in _VALID_MODES else "observe",
        }) + "\n", encoding="utf-8")
        os.replace(BILL_TMP, BILL_JSON)
        return True
    except Exception:
        return False


def _state_after_write_failure(previous):
    """原子替换失败时磁盘仍是旧值，内存也必须回到同一份旧值。"""
    _state.clear()
    _state.update(previous)
    return {
        "ok": False,
        "enabled": bool(_state["enabled"]),
        "mode": _state["mode"],
        "error": "写入 bill.json 失败，账单 hook 状态已回滚",
    }


def _update_state(**changes):
    """仅在 bill.json 原子写入成功后保留新的内存态。"""
    previous = dict(_state)
    _state.update(changes)
    if _write_bill():
        return None
    return _state_after_write_failure(previous)


def seen_count():
    """聚合各扩展宿主进程上报的「看到的账单请求」条数（忽略 >120s 陈旧项）。"""
    total = 0
    now = time.time() * 1000
    try:
        for f in SEEN_DIR.iterdir():
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


# ---------- stub：注入到 Cursor 扩展宿主入口，加载 billhook.js ----------
# 账单 stub 有自己的标记和留底目录。旧版账单曾经使用同一个文件名和旧签名，
# 升级时只按账单自身签名摘掉那一行；这里不读取或管理任何其它插件的目录/状态。
STUB_MARK = "chijiu-billgate"
LEGACY_STUB_MARK = "chijiu-billgate-esm"
STUB_MARK_ESM = "chijiu-billgate-esm-v2"
STUB_LINE = (
    'try{(function(){var g=process.getBuiltinModule.bind(process),'
    'o=g("node:os"),m=g("node:module");'
    'm.createRequire(process.execPath)(o.homedir()+"/.rxyy-billgate/billhook.js")})()}'
    'catch(e){/* chijiu-billgate-esm-v2 */}\n'
)


def _plugin() -> "_ehp.ExtHostPlugin":
    """按当前模块全局构建 manifest（阶段2 真迁移：扩展宿主 plumbing 全走共享层）。

    每次调用现建而非模块级常量：BILLGATE_DIR/HOOK_DST 等全局是契约测试按用例
    整包替换的口子，冻成常量会让替换失效。_wire_cache 传模块级字典，30s 接通
    缓存跨调用仍有效。own_line_groups 单组（STUB_MARK+"billhook.js"）即覆盖
    base/esm/esm-v2 三代 stub 行（ESM 标记都含 STUB_MARK 子串）。
    """
    return _ehp.ExtHostPlugin(
        name="billgate", home=BILLGATE_DIR,
        hook_src=HOOK_SRC, hook_dst=HOOK_DST,
        stub_mark=STUB_MARK_ESM, stub_line=STUB_LINE,
        own_line_groups=((STUB_MARK, "billhook.js"),),
        identity_markers=(STUB_MARK, "billhook.js"),
        _wire_cache=_WIRE_CACHE)


def install_hook():
    """把 billgate_hook.js 原子复制到独立目录（只升不降，共享层实现）。"""
    return _ehp.install_hook(_plugin())


def _has_stub(path: Path) -> bool:
    return _ehp.has_stub(_plugin(), path)


def _has_legacy_stub(path: Path) -> bool:
    return _ehp.has_legacy_stub(_plugin(), path)


def _cursor_ext_host_entries(probe: bool = True):
    """定位 Cursor 扩展宿主入口（共享层实现；模块级包装供契约测试按名替换）。"""
    return _ehp.ext_host_entries(_plugin(), probe=probe)


_WIRE_CACHE = {"ts": 0.0, "wired": False, "note": ""}
_WIRE_TTL = 30.0


def wiring_state(probe: bool = False):
    """账单 hook 通不通：billhook.js 在盘上 + Cursor 扩展宿主入口注入了 stub，缺一不可。"""
    now = time.time()
    if not probe and now - _WIRE_CACHE["ts"] < _WIRE_TTL:
        return _WIRE_CACHE["wired"], _WIRE_CACHE["note"]
    if not HOOK_DST.is_file():
        wired, note = False, "未装 billhook.js，账单 hook 不会生效"
    else:
        entries = _cursor_ext_host_entries(probe=probe)
        if not entries:
            wired, note = False, "没找到 Cursor 扩展宿主入口，无法确认是否生效"
        else:
            hit = [p for p in entries if _has_stub(p)]
            if hit:
                wired = True
                note = "已接通（{}/{} 个 Cursor 入口已注入）".format(len(hit), len(entries))
            else:
                wired = False
                note = ("Cursor 入口未注入 stub，账单 hook 不生效"
                        "（点「安装账单hook」后开个新窗口即可，不必 Reload 现有窗口）")
    _WIRE_CACHE.update({"ts": now, "wired": wired, "note": note})
    return wired, note


def ensure_stub():
    """确保扩展宿主入口注入了加载 billhook.js 的 ESM stub（幂等，共享层实现）。

    只对之后新开的窗口生效。留底/旧版替换/跨插件原件判定按共享层铁律走
    （比旧实现更严：留底若带开车痕迹也不认，不再只查账单自己的 stub）。"""
    try:
        injected, note = _ehp.ensure_stub(_plugin())
    except _ehp.PluginCleanupError as e:
        raise BillgateCleanupError(str(e)) from e
    _WIRE_CACHE["ts"] = 0.0
    return injected, note


def _strip_own_stub(p: Path) -> bool:
    """只摘账单 hook 自己的当前/历史 stub，其余字节原样保留（共享层实现）。

    模块级包装：契约测试按名替换它模拟摘除失败；共享层的 PluginCleanupError
    在这里翻译成 BillgateCleanupError，维持 Hub 侧「卸载失败绝不假称成功」的类型契约。"""
    try:
        return _ehp.strip_own_stub(_plugin(), p)
    except _ehp.PluginCleanupError as e:
        raise BillgateCleanupError(str(e)) from e


def _unlink_own(path: Path) -> bool:
    """删除账单 hook 自己写出的单个文件；不存在也视为已清理。"""
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError as e:
        raise BillgateCleanupError("删除账单 hook 文件失败（{}）: {}".format(path, e)) from e


def _remove_seen_files() -> int:
    """只删 billgate 自己按 PID 写出的观测 JSON，不碰目录里的其它文件。"""
    removed = 0
    try:
        files = list(SEEN_DIR.glob("*.json"))
    except OSError as e:
        raise BillgateCleanupError("枚举账单 hook 观测文件失败（{}）: {}".format(SEEN_DIR, e)) from e
    for p in files:
        _unlink_own(p)
        removed += 1
        # 目录仅在确实空了时才拿掉，防止误删人工留下的诊断文件。
    try:
        SEEN_DIR.rmdir()
    except OSError:
        pass
    return removed


def remove_stub():
    """完整卸载账单 hook，只清理 billgate 独占残留。

    只清理本功能目录和本功能 stub；开车及其它控制器的文件、状态和留底均不触碰。

    成功时维持既有的 ``(removed_count, note)`` 返回值；任何未确认的清理失败
    抛 ``BillgateCleanupError``，以便 Hub 返回 ok=false，而不是假称卸载完成。
    """
    ensure_seeded()
    # 先把已安装的信号文件落成关闭态。若这一步失败，旧 enabled=true 仍可能被扩展
    # 宿主读到，绝不能继续删文件后告诉 UI 已卸载。
    if BILL_JSON.is_file():
        failed = _update_state(enabled=False, mode="observe", seeded=True)
        if failed:
            raise BillgateCleanupError("无法先安全关闭账单 hook：{}".format(failed["error"]))
    else:
        _state.update({"enabled": False, "mode": "observe", "seeded": True})
    try:
        entries = _cursor_ext_host_entries()
    except Exception as e:
        raise BillgateCleanupError("定位 Cursor 入口失败，无法确认 stub 已清理: {}".format(e)) from e
    removed = 0
    for p in entries:
        if _strip_own_stub(p):
            removed += 1
    _WIRE_CACHE["ts"] = 0.0
    for p in (HOOK_DST, BILL_JSON, BILL_TMP, BILL_LOG, BILL_LOG_ROTATED):
        _unlink_own(p)
    seen = _remove_seen_files()
    return removed, ("已卸载账单 hook：摘掉 {} 个入口，清理 {} 份观测；"
                     "开车与其它控制器未动，需 Reload Window").format(removed, seen)


def status():
    ensure_seeded()
    wired, why = wiring_state()
    return {
        "ok": True,
        "enabled": bool(_state["enabled"]),
        "mode": _state["mode"],
        "seen": seen_count(),
        "hook_installed": HOOK_DST.is_file(),
        "wired": wired,
        "wiring_note": why,
    }


def enable(mode: str = "observe"):
    """启用账单 hook；未接通时拒绝写 enabled=true，避免面板制造假状态。"""
    ensure_seeded()
    wired, why = wiring_state(probe=True)
    if not wired:
        return {
            "ok": False,
            "enabled": bool(_state["enabled"]),
            "mode": _state["mode"],
            "hook_installed": HOOK_DST.is_file(),
            "wired": False,
            "error": "账单 hook 尚未接通，不能开启：{}".format(why),
        }
    failed = _update_state(mode=mode if mode in _VALID_MODES else "observe", enabled=True)
    if failed:
        return failed
    return status()


def disable():
    """停用账单 hook；写盘失败时返回 ok=false 且包含恢复后的旧状态。"""
    ensure_seeded()
    failed = _update_state(enabled=False)
    if failed:
        return failed
    return status()


def set_mode(mode: str):
    """更新模式；写盘失败时返回 ok=false 且包含恢复后的旧状态。"""
    ensure_seeded()
    failed = _update_state(mode=mode if mode in _VALID_MODES else "observe")
    if failed:
        return failed
    return status()


def bootstrap():
    """hub 启动时保持安全态，不为未安装的 billgate 制造痕迹。"""
    ensure_seeded()
    # 缺少状态和 hook 表明已完整卸载（或从未安装）。文件不存在本身就是安全关闭态，
    # 不要因 Hub 重启又创建 bill.json。
    if not BILL_JSON.is_file() and not HOOK_DST.is_file():
        _state.update({"enabled": False, "mode": "observe"})
        return {"ok": True, "enabled": False, "note": "账单 hook 未安装，保持无痕关闭态"}
    failed = _update_state(enabled=False)
    if failed:
        return {
            "ok": False,
            "enabled": bool(_state["enabled"]),
            "mode": _state["mode"],
            "error": "启动时无法安全关闭账单 hook：{}".format(failed["error"]),
            "note": "账单 hook 安全关闭失败，保留原状态",
        }
    return {"ok": True, "enabled": False, "note": "账单 hook 已置安全态（默认关，不自动注入）"}
