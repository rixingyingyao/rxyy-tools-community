# -*- coding: utf-8 -*-
"""rxyy MCP 工作流编排引擎：任务链「实现 → 审查 → 部署」这类多步活的自动接力。

设计取舍（07-31 与用户对齐的用法）：
- 一条工作流 = 顺序步骤链，每步派给一个执行人：在线 agent（auto/指名/按角色）
  或「内置」（sdk_spawn 现场拉起本机 agent）。
- 步骤完成信号**复用团队黑板的既有纪律**：agent 干完阶段活本来就被要求
  ji(action="黑板", category="收工", …)。派单提示词里再要求带上「工作流#<id>」
  标记；就算忘了标记，只要是**被派的那个 agent** 写的收工/提交也认。
  好处：MCP 守护进程零改动（新增 ji 动作要重启全队的守护进程），agent 零新词。
- 引擎状态落盘 workflows.json，跨 hub 重启接续；hub 每秒 tick 检查超时。

钩子（hub 注入；测试用假钩子即可，不依赖 hub）：
  team_agents(root)   -> [{conv,label,role,waiting,shell,seq}] 同项目在线 agent
  find_conv(prefix)   -> {conv,label} | None   全局按对话ID前缀找在线 agent
  send_to_conv(conv, text) -> {ok, label?, error?}   给指定 agent 投话
  spawn_builtin(root, task_name, first_task) -> {ok, conversation_id?, key?, error?}
  builtin_status(key) -> {status, note} | None   内置 runner 注册表健康度
  notify_user(title, body)   完成/卡住时提醒用户（ntfy+日志）
  log(text)                  运行日志
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

# 收工/提交 = 步骤完成；卡住/事故 = 步骤失败。其余类别只当动静、不推进。
DONE_KINDS = ("收工", "提交")
FAIL_KINDS = ("卡住", "事故")
MARK_RE = re.compile(r"工作流#(wf[0-9a-f]{6})")

BUILTIN_CHECK_EVERY = 20   # 秒：内置 runner 健康巡检节流（tick 每秒来，别每秒读注册表）
BUILTIN_FINISH_GRACE = 90  # 秒：runner 正常跑完后等黑板收工信号的宽限（信号先于进程退出时不误卡）

STEP_TEMPLATE_PRESET = [
    {"title": "实现", "detail": "按需求完成代码改动，跑通相关测试。", "executor": "auto"},
    {"title": "审查", "detail": "只审不改：逐条指出正确性/边界/回归风险，结论写明通过或打回。",
     "executor": "role:review"},
    {"title": "部署", "detail": "把已审的改动部署/热拷到运行副本并重启验证，部署前写黑板预告。",
     "executor": "auto"},
]


def _now_hms():
    return time.strftime("%H:%M:%S")


class WorkflowEngine:
    def __init__(self, path, hooks=None):
        self.path = Path(path)
        self.hooks = dict(hooks or {})
        self.lock = threading.RLock()
        self.workflows = []
        self._load()

    # ---------- 钩子 ----------
    def _hook(self, name, *args, default=None):
        fn = self.hooks.get(name)
        if not callable(fn):
            return default
        try:
            return fn(*args)
        except Exception as e:  # noqa: BLE001
            self._log("钩子 {} 异常: {}".format(name, e))
            return default

    def _log(self, text):
        fn = self.hooks.get("log")
        if callable(fn):
            try:
                fn("[工作流] " + text)
            except Exception:
                pass

    def _notify(self, title, body):
        fn = self.hooks.get("notify_user")
        if callable(fn):
            try:
                fn(title, body)
            except Exception:
                pass

    # ---------- 落盘 ----------
    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.workflows = data if isinstance(data, list) else []
        except Exception:
            self.workflows = []

    def _save(self):
        try:
            tmp = str(self.path) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.workflows, f, ensure_ascii=False, indent=1, default=str)
            os.replace(tmp, self.path)
        except Exception as e:  # noqa: BLE001
            self._log("落盘失败: {}".format(e))

    # ---------- 查询 ----------
    def _get(self, wid):
        return next((w for w in self.workflows if w.get("id") == wid), None)

    def snapshot(self):
        """给 UI 的完整视图（新的在前）。"""
        with self.lock:
            out = []
            for w in reversed(self.workflows):
                out.append({
                    "id": w["id"], "name": w["name"], "root": w["root"],
                    "root_name": Path(w["root"]).name or w["root"],
                    "status": w["status"], "current": w["current"],
                    "timeout_min": w.get("timeout_min", 0),
                    "created": w.get("created", ""),
                    "steps": [dict(s) for s in w["steps"]],
                    "log": list(w.get("log") or [])[-6:],
                })
            return out

    # ---------- 生命周期 ----------
    def create(self, name, root, steps, timeout_min=45):
        name = str(name or "").strip()[:40]
        root = str(root or "").strip()
        if not name:
            return {"ok": False, "error": "工作流名称不能为空"}
        if not root:
            return {"ok": False, "error": "缺少项目目录"}
        rows = []
        for st in steps or []:
            if not isinstance(st, dict):
                continue
            title = str(st.get("title") or "").strip()[:30]
            if not title:
                continue
            rows.append({
                "title": title,
                "detail": str(st.get("detail") or "").strip()[:2000],
                "executor": str(st.get("executor") or "auto").strip()[:80],
                "status": "pending", "assigned_conv": "", "assigned_label": "",
                "started": 0, "ended": 0, "result": "",
            })
        if not rows:
            return {"ok": False, "error": "至少要有一个带标题的步骤"}
        wf = {
            "id": "wf" + uuid.uuid4().hex[:6],
            "name": name, "root": root, "status": "draft", "current": 0,
            "timeout_min": max(0, int(timeout_min or 0)),
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "steps": rows, "log": [],
        }
        with self.lock:
            self.workflows.append(wf)
            self._wf_log(wf, "创建（{} 步）".format(len(rows)))
            self._save()
        return {"ok": True, "id": wf["id"]}

    def start(self, wid):
        """启动 / 从暂停·卡住处继续。"""
        with self.lock:
            wf = self._get(wid)
            if not wf:
                return {"ok": False, "error": "工作流不存在"}
            if wf["status"] == "done":
                return {"ok": False, "error": "已经全链完成了"}
            if wf["current"] >= len(wf["steps"]):
                return self._finish(wf)
            step = wf["steps"][wf["current"]]
            wf["status"] = "running"
            if step["status"] == "running":
                # 暂停期间步骤还在跑（agent 手上有活），恢复只是重新接收信号
                self._wf_log(wf, "继续（第{}步仍在执行人手上）".format(wf["current"] + 1))
                self._save()
                return {"ok": True, "note": "已继续，等待第{}步信号".format(wf["current"] + 1)}
            return self._dispatch(wf)

    def pause(self, wid):
        with self.lock:
            wf = self._get(wid)
            if not wf:
                return {"ok": False, "error": "工作流不存在"}
            if wf["status"] not in ("running", "stuck"):
                return {"ok": False, "error": "当前状态（{}）不用暂停".format(wf["status"])}
            wf["status"] = "paused"
            self._wf_log(wf, "已暂停（执行人若继续交付，完成会记账但不派下一步）")
            self._save()
            return {"ok": True}

    def delete(self, wid):
        with self.lock:
            before = len(self.workflows)
            self.workflows = [w for w in self.workflows if w.get("id") != wid]
            if len(self.workflows) == before:
                return {"ok": False, "error": "工作流不存在"}
            self._save()
            return {"ok": True}

    def force_done(self, wid, note=""):
        """用户在面板手动确认当前步骤已完成（agent 忘写黑板时的兜底）。"""
        with self.lock:
            wf = self._get(wid)
            if not wf:
                return {"ok": False, "error": "工作流不存在"}
            if wf["current"] >= len(wf["steps"]):
                return self._finish(wf)
            result = "用户手动确认完成" + ("：" + str(note).strip() if str(note or "").strip() else "")
            return self._complete_step(wf, result)

    def redispatch(self, wid, executor=""):
        """重派当前步骤（换人或原执行人重来）。"""
        with self.lock:
            wf = self._get(wid)
            if not wf:
                return {"ok": False, "error": "工作流不存在"}
            if wf["current"] >= len(wf["steps"]):
                return {"ok": False, "error": "没有待执行的步骤"}
            step = wf["steps"][wf["current"]]
            executor = str(executor or "").strip()
            if executor:
                step["executor"] = executor[:80]
            step["status"] = "pending"
            step["assigned_conv"] = ""
            step["assigned_label"] = ""
            wf["status"] = "running"
            self._wf_log(wf, "重派第{}步（执行人 {}）".format(wf["current"] + 1, step["executor"]))
            return self._dispatch(wf)

    # ---------- 派单 ----------
    def _wf_log(self, wf, text):
        wf.setdefault("log", []).append({
            "hms": _now_hms(), "day": time.strftime("%m-%d"), "text": str(text)[:200]})
        del wf["log"][:-40]

    def _busy_convs(self, exclude_wid=""):
        """其它工作流正占用的执行人：auto 挑人时避开，别把两条链压给同一个 agent。"""
        out = set()
        for w in self.workflows:
            if w.get("id") == exclude_wid or w.get("status") != "running":
                continue
            idx = w.get("current", 0)
            if 0 <= idx < len(w.get("steps") or []):
                c = w["steps"][idx].get("assigned_conv") or ""
                if c:
                    out.add(c)
        return out

    def _resolve_executor(self, wf, step):
        """执行人说明 → (conv, label) 或 (None, 报错文案)。builtin 单独走。"""
        spec = str(step.get("executor") or "auto").strip()
        if spec.startswith("conv:"):
            hit = self._hook("find_conv", spec[5:].strip())
            if not hit:
                return None, "指名的 agent（{}）不在线".format(spec[5:].strip()[:12])
            return hit["conv"], hit.get("label") or hit["conv"][:8]
        agents = self._hook("team_agents", wf["root"], default=[]) or []
        busy = self._busy_convs(exclude_wid=wf["id"])
        agents = [a for a in agents if a.get("conv") and a["conv"] not in busy]
        if spec.startswith("role:"):
            role = spec[5:].strip()
            agents = [a for a in agents if (a.get("role") or "") == role]
            if not agents:
                return None, "项目里没有在线的「{}」角色 agent".format(role)
        elif spec not in ("", "auto"):
            return None, "看不懂的执行人写法：{}".format(spec[:20])
        if not agents:
            return None, "项目里没有可派的在线 agent（可改成「内置」现场拉一个）"
        agents.sort(key=lambda a: (
            0 if a.get("waiting") else 1,      # 正等回复的立刻能开工
            0 if a.get("shell") else 1,        # 待命空壳最闲
            int(a.get("seq") or 0),            # 越干净越像空闲
        ))
        top = agents[0]
        return top["conv"], top.get("label") or top["conv"][:8]

    def _step_prompt(self, wf, idx):
        step = wf["steps"][idx]
        n, total = idx + 1, len(wf["steps"])
        prev = wf["steps"][idx - 1].get("result") if idx > 0 else ""
        lines = [
            "【工作流派单 · {} · 第 {}/{} 步：{}】".format(wf["name"], n, total, step["title"]),
            str(step.get("detail") or "").strip() or "（没写详情，按步骤标题理解）",
            "",
            "项目：{}（你的窗口若不在这个工作区，一律走绝对路径改文件）".format(wf["root"]),
            "上一步交付：{}".format(prev) if prev else "这是首步，没有上一步交付。",
            "",
            "干完后【必须】写黑板收工——编排引擎靠它自动把下一步派出去，忘了整条链就停：",
            'ji(action="黑板", category="收工", content="工作流#{} 第{}步完成：<一句话结果>")'.format(
                wf["id"], n),
            '卡住/翻车时：ji(action="黑板", category="卡住", content="工作流#{} <原因>")'.format(
                wf["id"]),
            "干活期间照常 zt 上报状态；动公共文件前先读黑板。",
        ]
        return "\n".join(lines)

    def _dispatch(self, wf):
        idx = wf["current"]
        step = wf["steps"][idx]
        prompt = self._step_prompt(wf, idx)
        spec = str(step.get("executor") or "auto").strip()
        # 重派/换人时清掉上一任内置 runner 的痕迹，别拿旧 runner 的死活判新执行人
        for k in ("builtin_key", "bi_checked", "bi_finished"):
            step.pop(k, None)
        if spec in ("builtin", "内置"):
            r = self._hook("spawn_builtin", wf["root"],
                           "工作流·" + step["title"], prompt, default=None) or {}
            if not r.get("ok"):
                return self._mark_stuck(wf, "拉起内置 agent 失败：{}".format(
                    r.get("error") or "spawn 钩子不可用"))
            step["assigned_conv"] = r.get("conversation_id") or ""
            step["assigned_label"] = "内置 agent（{}）".format(
                (r.get("conversation_id") or "")[:8])
            step["builtin_key"] = r.get("key") or ""
        else:
            conv, label_or_err = self._resolve_executor(wf, step)
            if not conv:
                return self._mark_stuck(wf, label_or_err)
            sent = self._hook("send_to_conv", conv, prompt, default=None) or {}
            if not sent.get("ok"):
                return self._mark_stuck(wf, "给 {} 投话失败：{}".format(
                    label_or_err, sent.get("error") or "send 钩子不可用"))
            step["assigned_conv"] = conv
            step["assigned_label"] = sent.get("label") or label_or_err
        step["status"] = "running"
        step["started"] = time.time()
        step["ended"] = 0
        wf["status"] = "running"
        self._wf_log(wf, "第{}步「{}」已派给 {}".format(
            idx + 1, step["title"], step["assigned_label"]))
        self._log("{} 第{}步派给 {}".format(wf["name"], idx + 1, step["assigned_label"]))
        self._save()
        return {"ok": True, "assigned": step["assigned_label"]}

    def _mark_stuck(self, wf, reason):
        idx = wf["current"]
        if 0 <= idx < len(wf["steps"]):
            wf["steps"][idx]["status"] = "stuck"
        wf["status"] = "stuck"
        self._wf_log(wf, "卡住：{}".format(reason))
        self._save()
        self._notify("⛓ 工作流「{}」卡住".format(wf["name"]),
                     "第{}步：{}\n面板上可重派或改执行人后继续。".format(idx + 1, reason))
        return {"ok": False, "error": reason, "stuck": True}

    def _complete_step(self, wf, result):
        idx = wf["current"]
        step = wf["steps"][idx]
        step["status"] = "done"
        step["ended"] = time.time()
        step["result"] = str(result or "").strip()[:300]
        self._wf_log(wf, "第{}步「{}」完成：{}".format(idx + 1, step["title"],
                                                  step["result"][:80] or "（无摘要）"))
        wf["current"] = idx + 1
        if wf["current"] >= len(wf["steps"]):
            return self._finish(wf)
        if wf["status"] == "paused":
            # 暂停期间只记账不派单，等用户点「继续」
            self._wf_log(wf, "已暂停：第{}步待用户继续后派出".format(wf["current"] + 1))
            self._save()
            return {"ok": True, "paused": True}
        return self._dispatch(wf)

    def _finish(self, wf):
        wf["status"] = "done"
        self._wf_log(wf, "全链完成")
        self._save()
        done = "\n".join("第{}步 {}：{}".format(i + 1, s["title"], s.get("result") or "完成")
                         for i, s in enumerate(wf["steps"]))
        self._notify("⛓ 工作流「{}」全链完成".format(wf["name"]), done[:500])
        self._log("{} 全链完成".format(wf["name"]))
        return {"ok": True, "done": True}

    # ---------- 信号 ----------
    def on_board_post(self, conv, root, kind, text):
        """黑板新条目进来时由 hub 调用：匹配到活跃工作流就推进/标卡。
        返回给 hub 的说明文字（None = 与工作流无关）。"""
        text = str(text or "")
        kind = str(kind or "")
        conv = str(conv or "")
        if kind not in DONE_KINDS + FAIL_KINDS:
            return None
        with self.lock:
            wf = None
            m = MARK_RE.search(text)
            if m:
                wf = self._get(m.group(1))
            if wf is None:
                # 没带标记：认「被派的那个 agent」本人写的收工/卡住——对话 ID 全局
                # 唯一，身份即证据；不比对项目根（借调 agent 的归属常在别的项目）
                for w in self.workflows:
                    if w["status"] not in ("running", "paused", "stuck"):
                        continue
                    idx = w.get("current", 0)
                    if idx >= len(w["steps"]):
                        continue
                    st = w["steps"][idx]
                    if (st.get("assigned_conv") and conv
                            and st["assigned_conv"] == conv):
                        wf = w
                        break
            if wf is None:
                return None
            if wf["status"] == "done" or wf["current"] >= len(wf["steps"]):
                return None
            step = wf["steps"][wf["current"]]
            # 标记指名的工作流：别人代报也认（例如负责人替执行人收工）；
            # 没标记的在上面已限定必须是被派人本人
            if kind in FAIL_KINDS:
                self._mark_stuck(wf, "执行人报告{}：{}".format(kind, text[:160]))
                return "工作流「{}」第{}步已标记卡住".format(wf["name"], wf["current"] + 1)
            r = self._complete_step(wf, text)
            if r.get("done"):
                return "工作流「{}」全链完成".format(wf["name"])
            if r.get("ok"):
                nxt = wf["steps"][wf["current"]] if wf["current"] < len(wf["steps"]) else None
                return "工作流「{}」推进到第{}步{}".format(
                    wf["name"], wf["current"] + 1,
                    "（已派给 {}）".format(nxt.get("assigned_label")) if nxt and nxt.get(
                        "assigned_label") else "")
            return "工作流「{}」推进失败：{}".format(wf["name"], r.get("error") or "未知")

    def on_executor_dead(self, conv, reason):
        """执行人会话被判死（欠费/额度/对话消失…）→ 当前步立即标卡。

        由 hub 的死亡探测在判死那一刻调用；没有它，链要干等 timeout_min
        （默认 45 分钟）才发现执行人早就断气了——而欠费挂掉一天能来六次。
        返回给 hub 记日志的说明（None = 死者与任何活跃工作流无关）。"""
        conv = str(conv or "")
        if not conv:
            return None
        with self.lock:
            for wf in self.workflows:
                # paused 也管：暂停只是不派下一步，当前步仍在执行人手上
                if wf.get("status") not in ("running", "paused"):
                    continue
                idx = wf.get("current", 0)
                if idx >= len(wf.get("steps") or []):
                    continue
                step = wf["steps"][idx]
                if (step.get("status") == "running"
                        and step.get("assigned_conv") == conv):
                    self._mark_stuck(wf, "执行人（{}）挂了：{}——面板上重派"
                                     "（可换人）后继续".format(
                                         step.get("assigned_label") or conv[:8],
                                         str(reason or "报错中断")[:120]))
                    return "工作流「{}」第{}步执行人挂了，已标卡".format(
                        wf["name"], idx + 1)
        return None

    # ---------- 超时 / 内置 runner 巡检 ----------
    def tick(self, now=None):
        """hub 每秒调用：运行中的步骤超时无信号 → 标卡 + 提醒（只提醒一次）；
        内置执行人的 runner 进程死了/跑完一轮没写收工 → 标卡，不用等超时。"""
        now = now or time.time()
        with self.lock:
            for wf in self.workflows:
                if wf.get("status") != "running":
                    continue
                idx = wf.get("current", 0)
                if idx >= len(wf.get("steps") or []):
                    continue
                step = wf["steps"][idx]
                if step.get("status") != "running":
                    continue
                if self._tick_builtin(wf, step, now):
                    continue
                tmo = int(wf.get("timeout_min") or 0)
                if (tmo > 0 and step.get("started")
                        and now - step["started"] > tmo * 60):
                    self._mark_stuck(wf, "第{}步超过 {} 分钟没有收工/卡住信号"
                                     "（执行人可能已挂，重派或去它窗口看看）".format(
                                         idx + 1, tmo))

    def _tick_builtin(self, wf, step, now):
        """内置执行人的 runner 健康巡检。返回 True = 本步已被标卡。

        runner 注册表是「agent 进程还在不在」的硬事实：error/dead/stopped =
        进程没了，链不可能再收到收工信号；finished = 一轮正常跑完，宽限期后
        还没收工信号就是它忘了写黑板（活可能干完了，让用户确认而不是干等）。"""
        key = step.get("builtin_key") or ""
        if not key:
            return False
        if now - (step.get("bi_checked") or 0) < BUILTIN_CHECK_EVERY:
            return False
        step["bi_checked"] = now
        st = self._hook("builtin_status", key, default=None)
        if not isinstance(st, dict):
            return False
        status = str(st.get("status") or "")
        if status in ("error", "dead", "stopped", "missing"):
            self._mark_stuck(wf, "内置 agent 没干成：{}（重派可换执行人）".format(
                st.get("note") or status))
            return True
        if status == "finished":
            if not step.get("bi_finished"):
                step["bi_finished"] = now
            elif now - step["bi_finished"] > BUILTIN_FINISH_GRACE:
                self._mark_stuck(wf, "内置 agent 已跑完一轮但没写黑板收工——"
                                 "活验过没问题就点「确认完成」，否则重派")
                return True
        return False
