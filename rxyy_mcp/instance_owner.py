# -*- coding: utf-8 -*-
"""整机单实例归属：谁占着 hub 端口，谁就是这台机器上的正主安装。

07-29 事故复盘：机器上同时存在两套rxyy MCP——源码版（`rxyy_mcp/`，由
`console/app.py` 拉起）与打包版（`dist/rxyy-tools-community/_internal/rxyy_mcp/`）。
两套各自带一张互保网（hub ↔ 看门狗 ↔ MCP 守护互相拉起），而单例守卫只认端口、
不认安装：谁先绑上 38996 谁就当看门狗，之后它只会从**自己那个目录**复活 hub 和
MCP 守护。于是两套轮流坐庄，每换一次手：

- 39222 上所有在飞的 zhi 全断 → 每个待命 agent 收到一次 reinit 报错、被迫重呼
  一轮（22:13 实测：六个新对话报到后全被打断，各白烧一轮 LLM 生成）；
- 用户看到「新对话进不来」，习惯性杀后台重启——而这恰恰又制造一次换手。

治法：把「正主」这件事显式记到共享数据目录（DATA_DIR 两套安装共用）。hub 抢到
端口即认领并持续心跳；看门狗 / MCP 守护 / console 在动手拉起前先问一句「我是不是
正主那一套」，不是就退位，绝不复活自己那一套。

心跳过期（默认 5 分钟）视为正主已经不在，任何安装都可以接管——崩溃自愈的兜底
不受影响。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from datadir import DATA_DIR

OWNER_PATH = DATA_DIR / ".instance-owner.json"
# 心跳间隔 30s；给三拍余量后判过期，避免 hub 卡顿一下就被别的安装抢走
STALE_SECS = 300


def _norm(path) -> str:
    try:
        return str(Path(path).resolve()).lower()
    except (OSError, ValueError):
        return str(path).lower()


def claim(app_dir) -> None:
    """认领正主（hub 抢到端口后调用，之后由心跳线程反复调用刷新时间戳）。"""
    try:
        OWNER_PATH.write_text(json.dumps({
            "app_dir": str(Path(app_dir).resolve()),
            "pid": os.getpid(),
            "ts": time.time(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def read_owner():
    try:
        data = json.loads(OWNER_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("app_dir") else None


def owner_age() -> float:
    """正主心跳距今多少秒；没有记录返回 inf。"""
    owner = read_owner()
    if not owner:
        return float("inf")
    try:
        return max(0.0, time.time() - float(owner.get("ts") or 0))
    except (TypeError, ValueError):
        return float("inf")


def should_stand_down(app_dir):
    """本安装该不该退位不干（不拉起、不复活、不抢端口）。

    返回 (要不要退位, 人话原因)。四种情形放行：没有归属记录、记录就是自己、
    正主心跳过期、正主目录已经不存在（安装被删/搬走）。
    """
    owner = read_owner()
    if not owner:
        return False, ""
    mine, theirs = _norm(app_dir), _norm(owner["app_dir"])
    if mine == theirs:
        return False, ""
    if not Path(owner["app_dir"]).is_dir():
        return False, ""
    age = owner_age()
    if age > STALE_SECS:
        return False, ""
    return True, "正主是另一套安装 {}（心跳 {:.0f}s 前），本安装退位".format(
        owner["app_dir"], age)
