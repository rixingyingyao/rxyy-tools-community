# -*- coding: utf-8 -*-
"""插件注册表 —— 通用最小层（开源蓝图阶段2，日报插件方案批①，2026-08-13）。

设计判断（docs/插件接口-日报工作流插件方案-2026-08-13.md 第二节，rxyy 拍板）：
**不做大一统 plugin 基类**——ext_host 族（park/bill，注入 Cursor 安装文件）与
workflow-data 族（日报，数据/管线/UI）的 manifest 字段、install/uninstall 语义、
危险面全部异构，强扭进一个基类字段十有八九互为 None。通用层只收敛为一份
**描述性 manifest（PluginInfo）+ 注册表**，回答三个问题：装了哪些插件、各自
什么状态、生命周期入口在哪。

纪律（与 hub._bootstrap_local_hooks 同款）：状态聚合时单插件异常绝不连坐——
一个插件的 status() 坏了，别的插件与注册表本身照常应答。

ExtHostPlugin（ext_host 族的族接口）原样保留在 ext_host_plugin.py，本模块只给
每个插件配一份 PluginInfo 描述；各插件的个性生命周期动词（park 的 arm/launch、
bill 的 enable/set_mode、日报的 enable/disable）留在各自模块，注册表不强制统一
签名——接口宁可小而真，不可大而空。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass(frozen=True)
class PluginInfo:
    """一个插件的自描述（通用最小核，纯元数据 + 状态入口）。

    - name：唯一键（日志/注册表/面板定位用）。
    - kind：族名——"ext_host"（注入 Cursor 扩展宿主）/ "workflow-data"（数据工作流）。
    - title：面板显示名（人话）。
    - version：manifest 版本（不是 hook 代际——那是 ext_host 族自己的概念）。
    - provides：人话能力清单（"ui:日报生产页"、"db:report_notes" 这种，开源
      文档与面板直接渲染）。
    - status：无参可调，返回该插件自己拼的状态 dict（沿用各 gate 既有
      status() 语义）；None = 该插件不提供状态查询。
    """

    name: str
    kind: str
    title: str
    version: str
    provides: tuple = field(default_factory=tuple)
    status: Optional[Callable[[], dict]] = None


_REGISTRY: dict[str, PluginInfo] = {}


def register(info: PluginInfo) -> PluginInfo:
    """登记一个插件；重名直接拒绝（None 名/空名同罪）。

    重名注册十有八九是打包/热拷把两份代码同时挂进来了——静默覆盖会让
    面板显示的状态来自「不知道哪一份」，宁可当场炸出来。
    """
    name = str(getattr(info, "name", "") or "").strip()
    if not name:
        raise ValueError("插件必须有名字")
    if name in _REGISTRY:
        raise ValueError("插件重名：{} 已注册".format(name))
    _REGISTRY[name] = info
    return info


def plugins() -> list:
    """按注册顺序返回全部 PluginInfo（Python dict 保序）。"""
    return list(_REGISTRY.values())


def states() -> dict:
    """聚合全部插件状态：{name: {kind,title,version,provides,status}}。

    单插件 status() 异常不连坐：坏的那个记 {"error": 人话}，其余照常。
    """
    out = {}
    for name, info in _REGISTRY.items():
        entry = {
            "kind": info.kind, "title": info.title, "version": info.version,
            "provides": list(info.provides or ()),
        }
        if info.status is None:
            entry["status"] = None
        else:
            try:
                entry["status"] = info.status()
            except Exception as e:  # noqa: BLE001
                entry["status"] = {"error": "状态查询失败：{}".format(e)}
        out[name] = entry
    return out


def _reset_for_tests():
    """仅测试用：清空注册表（运行时没有任何路径该调它）。"""
    _REGISTRY.clear()


# 日报插件开关的 kv 键（落 console 域的 workflow.db kv_config，不进 hub
# config.json——与 1a49a6ec 的 config schema 零重叠，方案书第四节核对过）
DAILY_REPORT_ENABLED_KEY = "plugin_daily_report_enabled"


def _find_workflow_db():
    """按 hub 的候选数据目录定位 console 域的 workflow.db；找不到返回 None。

    路径解析不自造（07-29「项目管理一夜之间全没了」教训）：直接吃
    hub.rxyy_data_dirs() 那套 RXYY_DATA_DIR → data-dir.txt → 根/data 的顺序。
    """
    import hub
    for d in hub.rxyy_data_dirs():
        try:
            p = d / "workflow.db"
            if p.is_file():
                return p
        except Exception:  # noqa: BLE001
            continue
    return None


def daily_report_status() -> dict:
    """日报插件状态（workflow-data 族的 status 形态：查数据面，不碰执行面）。

    跨域只读：workflow.db 是 console 的家当，这里用 sqlite immutable 只读连接
    看一眼（与 session_locator 读 Cursor 状态库同款），绝不写。缺环境（db 不在/
    表没建）如实报告而不炸——插件状态的「不知道」也是一种状态。
    """
    import sqlite3
    import time as _time
    out = {"enabled": True, "db": "", "notes_7d": None, "oa_ready": None}
    db = _find_workflow_db()
    if db is None:
        out["db"] = "未找到 workflow.db（console 未初始化或数据目录指针缺失）"
        return out
    out["db"] = str(db)
    try:
        con = sqlite3.connect(
            "file:{}?immutable=1".format(str(db).replace("\\", "/")),
            uri=True, timeout=2)
        try:
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "report_notes" in tables:
                since = _time.strftime(
                    "%Y-%m-%d", _time.localtime(_time.time() - 7 * 86400))
                out["notes_7d"] = con.execute(
                    "SELECT COUNT(*) FROM report_notes WHERE note_date >= ?",
                    (since,)).fetchone()[0]
            else:
                out["notes_7d"] = None
            if "kv_config" in tables:
                kv = dict(con.execute(
                    "SELECT k, v FROM kv_config WHERE k IN (?,?,?)",
                    ("oa_user_account", "oa_password",
                     DAILY_REPORT_ENABLED_KEY)))
                out["oa_ready"] = bool((kv.get("oa_user_account") or "").strip()
                                       and (kv.get("oa_password") or "").strip())
                out["enabled"] = (kv.get(DAILY_REPORT_ENABLED_KEY, "1")
                                  or "1").strip() != "0"
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        out["db"] = "读取失败：{}".format(e)
    return out


def ensure_builtin_plugins() -> list:
    """登记内置插件描述（幂等；import 放函数内，注册表自身不背运行时依赖）。

    批①接两个 ext_host 活样例（93873ab/47457e0 真迁移完的 park/bill），
    status 直接引用各 gate 既有的模块级 status()——本函数不改它们一个字。
    批②接 workflow-data 族第一例：日报插件（宿主在 console，这里只挂描述与
    只读状态，不搬任何日报代码——方案书第三节）。
    """
    added = []
    if "parkgate" not in _REGISTRY:
        import parkgate
        added.append(register(PluginInfo(
            name="parkgate", kind="ext_host", title="停车发车",
            version="1.0",
            provides=(
                "hook:extensionHostProcess.js 注入（chijiu-parkgate-esm）",
                "signal:~/.salak 停车/发车信号",
                "ui:控制台停车按钮",
            ),
            status=parkgate.status)))
    if "billgate" not in _REGISTRY:
        import billgate
        added.append(register(PluginInfo(
            name="billgate", kind="ext_host", title="账单守门",
            version="1.0",
            provides=(
                "hook:extensionHostProcess.js 注入（chijiu-billgate-esm-v2）",
                "signal:~/.rxyy-billgate 观测/拦截信号",
                "ui:控制台账单开关（observe/block/debug）",
            ),
            status=billgate.status)))
    if "daily_report" not in _REGISTRY:
        added.append(register(PluginInfo(
            name="daily_report", kind="workflow-data", title="日报生产",
            version="1.0",
            provides=(
                "db:workflow.db/report_notes(+migrations)",
                "cli:console/tools/report_note.py",
                "skills:daily-note,daily-report,weekly-report",
                "ui:console 日报生产页",
                "api:projects_api 日报段 + oa_api 提交",
            ),
            status=daily_report_status)))
    return added
