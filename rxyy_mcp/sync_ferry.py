# -*- coding: utf-8 -*-
r"""双活 P1（运输线）：SSH 摆渡对端 outbox——Syncthing 未配对期间的事件运输。

设计稿 §4.1 的运输层写的是 Syncthing 双向夹；实拍（08-29）两台机都没跑
Syncthing，而两机 OpenSSH 免密互通是几个月的既成事实（家→公司别名 company
撑着 OA 中继，公司→家别名 sunrise 撑着代码对比/远程运维）。P1 拍板先用现成
的 SSH 把事件跑起来——运输层对 syncbus 完全透明，将来真配了 Syncthing，
把本模块的 config 清空即退役，事件文件一字不动。

模型：**只拉不推**，两机对称部署。每拍从对端 sync_root 抓对端自己的 outbox
（events/<peer>.jsonl）回本机同名位置：
- 写侧安全：远端只当 scp 源（只读）；本机落地走 tmp + os.replace 原子换，
  本机 replayer 永远看不到半个文件（syncbus 对「文件变小」另有幂等兜底）；
- 对端 hub 死了不碍事：SSH 是系统服务，文件在就拉得到；对端整机关机=拉不到，
  事件在对端盘上积压，开机后自然续上——「断网只是积压」，与幂等台账口径一致；
- 不推的原因：往对端写文件得在远端做原子换（scp 中途的半文件会被对端 replayer
  看见），多一套远端脚本；拉是纯本地原子操作，简单即正确。

纪律：
- scp 挂 BatchMode（绝不交互卡死）+ ConnectTimeout + 子进程硬超时；
- 对端还没发过事件（远端文件不存在）不算故障：对端可达、只是没话说；
- 连续失败到阈值举手一次、恢复再报一声平安——对端下班关机是常态，短时
  失败只躺在 status 里，绝不刷屏。
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path

from syncbus import EVENTS_DIRNAME, _MACHINE_RE

# 60s 拍 × 20 = 对端消失约 20 分钟才举手（下班关机是常态，别拿常态吓人）
FAIL_ALERT_AFTER = 20
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
# scp 找不到远端文件的措辞（OpenSSH 各版本不一）：这不算运输故障
_MISSING_RE = re.compile(r"no such file|not found|cannot stat", re.IGNORECASE)


class Ferry:
    """单对端拉取器：scp 对端 outbox → 本机 events 目录（tmp+原子换）。"""

    def __init__(self, sync_root, peer_ssh, peer_root, peer_machine,
                 secs=60.0, alert=None):
        self.peer_ssh = str(peer_ssh)
        self.peer_machine = str(peer_machine)
        self.local_path = (Path(sync_root) / EVENTS_DIRNAME
                           / (self.peer_machine + ".jsonl"))
        self.local_path.parent.mkdir(parents=True, exist_ok=True)
        # 远端路径统一正斜杠（oa_daily_job 摆渡同款写法，Windows OpenSSH 认）；
        # 路径要求 ASCII——scp 过中文路径在两头代码页不一致时会碎，sync_root
        # 选址时就该避开（config 注释有交代）
        root = str(peer_root).replace("\\", "/").rstrip("/")
        self.remote = "{}:{}/{}/{}.jsonl".format(
            self.peer_ssh, root, EVENTS_DIRNAME, self.peer_machine)
        self.secs = max(10.0, float(secs))
        self.alert = alert or (lambda msg: None)
        self._stop = threading.Event()
        self._thread = None
        self._down_alerted = False
        self.pulls = 0
        self.changed = 0
        self.consecutive_fails = 0
        self.remote_missing = False
        self.last_ok_ts = 0.0
        self.last_change_ts = 0.0
        self.last_error = ""

    # ---------- 单趟 ----------
    def pull_once(self) -> bool:
        """拉一趟；返回「本机副本是否有更新」。"""
        self.pulls += 1
        tmp = self.local_path.with_name(self.local_path.name + ".ferrytmp")
        cmd = ["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
               self.remote, str(tmp)]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=90,
                               creationflags=_CREATE_NO_WINDOW)
        except Exception as e:  # noqa: BLE001  scp 不在 PATH / 硬超时
            self._cleanup(tmp)
            self._fail("scp 拉不动: {}".format(e))
            return False
        if r.returncode != 0:
            self._cleanup(tmp)
            err = (r.stderr or b"").decode("utf-8", "replace").strip()
            if _MISSING_RE.search(err):
                # 对端可达、只是还没 emit 过：运输是健康的
                self.remote_missing = True
                self._recover()
                self.last_error = "对端尚无 outbox（还没发过事件）"
                return False
            self._fail(err or "scp 退出码 {}".format(r.returncode))
            return False
        self.remote_missing = False
        try:
            data = tmp.read_bytes()
        except OSError as e:
            self._cleanup(tmp)
            self._fail("读临时件失败: {}".format(e))
            return False
        try:
            old = self.local_path.read_bytes()
        except OSError:
            old = b""
        if data == old:
            self._cleanup(tmp)
            self._recover()
            self.last_error = ""
            return False
        os.replace(tmp, self.local_path)
        self._recover()
        self.last_error = ""
        self.changed += 1
        self.last_change_ts = time.time()
        return True

    @staticmethod
    def _cleanup(tmp: Path):
        try:
            tmp.unlink()
        except OSError:
            pass

    def _recover(self):
        if self._down_alerted:
            self._down_alerted = False
            try:
                self.alert("双活摆渡恢复：对端 {} 又拉得到了（中断了 {} 拍）"
                           .format(self.peer_ssh, self.consecutive_fails))
            except Exception:
                pass
        self.consecutive_fails = 0
        self.last_ok_ts = time.time()

    def _fail(self, err: str):
        self.consecutive_fails += 1
        self.last_error = str(err)[:300]
        if self.consecutive_fails == FAIL_ALERT_AFTER and not self._down_alerted:
            self._down_alerted = True
            try:
                self.alert("双活摆渡：连续 {} 拍拉不到对端 {}（{}）——对端关机属常态，"
                           "开机自动续上；若对端明明在线，先查 SSH（别名/密钥/服务）"
                           .format(self.consecutive_fails, self.peer_ssh,
                                   self.last_error))
            except Exception:
                pass

    # ---------- 线程 ----------
    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.pull_once()
            except Exception as e:  # noqa: BLE001  绝不让摆渡线程无声死掉
                self.last_error = str(e)[:300]
            self._stop.wait(self.secs)

    def status(self):
        return {"peer_ssh": self.peer_ssh, "remote": self.remote,
                "local": str(self.local_path), "pulls": self.pulls,
                "changed": self.changed,
                "consecutive_fails": self.consecutive_fails,
                "remote_missing": self.remote_missing,
                "last_ok_ts": self.last_ok_ts,
                "last_change_ts": self.last_change_ts,
                "last_error": self.last_error}


def start_from_config(cfg, alert=None, machine=""):
    """hub 接线：sync_peer_ssh/sync_peer_root/sync_peer_machine 三样齐才开；
    peer_machine 撞了本机名直接拒开——那会拿远端副本盖掉本机 outbox（单写者
    铁律，盖了=事件永久丢失）。"""
    cfg = cfg or {}
    root = str(cfg.get("sync_root") or "").strip()
    peer_ssh = str(cfg.get("sync_peer_ssh") or "").strip()
    peer_root = str(cfg.get("sync_peer_root") or "").strip()
    peer_machine = _MACHINE_RE.sub(
        "-", str(cfg.get("sync_peer_machine") or "").strip().lower())[:24]
    if not (root and peer_ssh and peer_root and peer_machine):
        return None
    if machine and peer_machine == machine:
        try:
            (alert or (lambda m: None))(
                "双活摆渡拒绝启动：sync_peer_machine「{}」与本机同名，拉回来会"
                "盖掉本机 outbox（单写者铁律）——改对 config 再来".format(peer_machine))
        except Exception:
            pass
        return None
    f = Ferry(root, peer_ssh, peer_root, peer_machine,
              secs=float(cfg.get("sync_ferry_secs", 60) or 60), alert=alert)
    return f.start()
