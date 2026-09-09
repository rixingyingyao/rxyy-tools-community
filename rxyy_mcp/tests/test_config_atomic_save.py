# -*- coding: utf-8 -*-
"""事故面：config.json 是全仓唯一一个直接覆盖写的落盘口。

`save_config` 直接 `CONFIG_PATH.write_text(...)`——先把文件截成 0 再往里写。
同一份仓里别的所有状态都不是这么写的：

    save_state / _save_relays / _save_takeover_aliases   tmp + os.replace
    mcp_touch._atomic_write_json                          tmp + os.replace
    console/api/board/storage._write                      tmp + os.replace
    console/api/taskstage/storage._write                  tmp + os.replace
    hub_api 落 board.json                                 tmp + os.replace

只有 save_config 例外，而它装的恰恰是最不能丢的那几样：

* `share_token` —— 手机分享链接和同事投递链接全从它派生，丢了全队的链接一起失效
* `team_project_members` / `team_roles` / `team_tracks` / `team_assign` —— 团队归属
* `wip_ledger` —— 脏文件溯源台账（上限 4000 行）

三个问题叠在一起：

1. **不原子**：截断与写入之间任何一次断电/强杀/磁盘满，留下的就是半截文件；
   下次 load_config 解析失败，整套配置回退默认值。
2. **没有锁**：26 个调用点里既有 API 线程，也有 `threading.Timer(0.8, ...)`
   这条后台线程和 `_persist_wip_ledger` 的 git 扫描线程，两个线程各写各的。
3. **静默**：`except Exception: pass`，写失败一个字都不说。

这一条不是「理论上」：hub 本来就会被 /api/restart_hub 接力重启、被看门狗收，
而退出路径上最后一件事正是 save_config（hub.py 的 on_closing）。
"""
import json
import os
import sys
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parent.parent
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

import hub  # noqa: E402


class AtomicSaveTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "config.json"
        patcher = patch.object(hub, "CONFIG_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.logged = []
        log = patch.object(hub, "log_event", lambda *a, **k: self.logged.append(a))
        log.start()
        self.addCleanup(log.stop)

    def _text(self):
        return self.path.read_text(encoding="utf-8")

    def test_the_live_config_is_untouched_until_the_new_one_is_complete(self):
        # 换名那一刻之前，盘上必须还是旧的那份——这就是「原子」的全部含义
        hub.save_config({"share_token": "keepme"})
        seen = {}
        real_replace = os.replace

        def spy(src, dst):
            seen["on_disk"] = Path(dst).read_text(encoding="utf-8")
            return real_replace(src, dst)

        with patch.object(hub.os, "replace", spy):
            hub.save_config({"share_token": "newone"})
        self.assertIn("keepme", seen.get("on_disk", ""),
                      "换名前盘上已经不是旧配置了，说明是直接覆盖写的")
        self.assertIn("newone", self._text())

    def test_a_write_that_dies_halfway_leaves_the_previous_config_intact(self):
        hub.save_config({"share_token": "keepme"})
        with patch.object(hub.os, "replace", side_effect=OSError("模拟断电")):
            hub.save_config({"share_token": "lost"})
        self.assertIn("keepme", self._text(),
                      "写失败把好好的配置顶没了：share_token 一丢，全队分享链接失效")
        self.assertEqual({"share_token": "keepme"}, json.loads(self._text()))

    def test_a_failed_save_says_so_instead_of_swallowing_it(self):
        hub.save_config({"share_token": "keepme"})
        with patch.object(hub.os, "replace", side_effect=OSError("模拟断电")):
            hub.save_config({"share_token": "lost"})
        self.assertTrue(self.logged, "写盘失败一个字都不说，等发现时已经无从查起")

    def test_no_temp_file_is_left_behind(self):
        hub.save_config({"share_token": "a"})
        with patch.object(hub.os, "replace", side_effect=OSError("模拟断电")):
            hub.save_config({"share_token": "b"})
        leftovers = [p.name for p in self.dir.iterdir() if p.name != "config.json"]
        self.assertEqual([], leftovers)

    def test_concurrent_saves_never_leave_an_unparseable_config(self):
        # 26 个调用点里有 API 线程、0.8s 防抖 Timer、git 扫描线程，它们会撞上
        payloads = [{"share_token": "t{}".format(i), "team_assign": {"c": "x" * 200}}
                    for i in range(12)]
        errors = []

        def run(cfg):
            for _ in range(8):
                try:
                    hub.save_config(cfg)
                    json.loads(self._text())   # 任何一拍读到半截都算失败
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        threads = [threading.Thread(target=run, args=(p,)) for p in payloads]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual([], errors)
        self.assertIn(json.loads(self._text())["share_token"],
                      [p["share_token"] for p in payloads])

    def test_it_still_only_persists_what_differs_from_the_defaults(self):
        # 原有语义别改没了：全量落盘会把 DEFAULTS 钉死，之后升级默认值永不生效
        key = next(iter(hub.DEFAULTS))
        hub.save_config({key: hub.DEFAULTS[key], "share_token": "x"})
        self.assertEqual({"share_token": "x"}, json.loads(self._text()))


if __name__ == "__main__":
    unittest.main()
