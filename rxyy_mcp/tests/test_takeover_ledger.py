# -*- coding: utf-8 -*-
"""接手台账收口（四账合并方案 A 步，2026-08-26）。

08-26 拆分手术暴露的账本乱象：「旧 ID/旧名字现在归谁」记在四套账里
（takeover_aliases / id_history / name_tombstones / handed_off_to），三次事故
（08-03 错绑、08-12 c00f0d0a、08-26 73004179）都是账对不上。A 步先把写入最散的
两套收成一本落盘的台账：

* 退休登记只走 ``_retire_conv_into``，条目带 ``{succ, old_name, succ_label, ts, why}``
  ——事后查「这个 ID 什么时候、为什么、并去了谁那儿」不用再翻日志拼线索；
* ``name_tombstones`` 并进同一份文件：此前它只活在内存，hub 一重启全清，
  按旧名转告就退化成「找不到」（07-31 待命·105628d8 那类现场每次重启都重演）；
* 旧平表文件（{退休conv: 现任conv}）照读，升级不丢账；首次落盘自动写成 v2。

平表视图 ``takeover_aliases`` 保持原形——入站换名、转告路由、链压缩全部不动。
"""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub  # noqa: E402

WS = r"d:\桌面\working\cursor工作流"


def _s(sid, conv, name):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = sid, conv, name
    s.cwd = s.task_root = WS
    s.connected = True
    s.pending = None
    s.queued = []
    s.messages = []
    s.id_history = []
    s.client = None
    s.lock = threading.Lock()
    return s


class _BookHarness(unittest.TestCase):
    """台账读写都打到临时文件上，真机的 takeover-aliases.json 一个字不碰。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.book_path = Path(self._tmp.name) / "takeover-aliases.json"
        self._patches = [
            patch.object(hub.Hub, "ALIASES_PATH", self.book_path),
            patch.object(hub.HUB, "takeover_ledger", {}, create=True),
            patch.object(hub.HUB, "takeover_aliases", {}),
            patch.object(hub.HUB, "name_tombstones", {}),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(self._teardown)

    def _teardown(self):
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()


class LedgerWriteTests(_BookHarness):

    def test_retire_records_metadata_and_flat_view(self):
        """退休登记必须同时落台账（带元数据）与平表（路由视图）。"""
        hub.HUB._retire_conv_into(
            "aaaa1111", "bbbb2222",
            old_name="待命·cursor工作流3",
            succ_label="rxyy tools·全面体检",
            why="测试登记")
        e = hub.HUB.takeover_ledger.get("aaaa1111")
        self.assertIsNotNone(e, "台账里必须有这一笔，不能只写平表")
        self.assertEqual(e["succ"], "bbbb2222")
        self.assertEqual(e["old_name"], "待命·cursor工作流3")
        self.assertEqual(e["succ_label"], "rxyy tools·全面体检")
        self.assertEqual(e["why"], "测试登记")
        self.assertGreater(float(e["ts"]), 0)
        self.assertEqual(hub.HUB.takeover_aliases.get("aaaa1111"), "bbbb2222")

    def test_rewrite_keeps_earlier_metadata(self):
        """同一退休 ID 再登记（改指新现任）时，旧名字等元数据不许被空值冲掉。"""
        hub.HUB._retire_conv_into("aaaa1111", "bbbb2222",
                                  old_name="待命·cursor工作流3", why="第一次")
        hub.HUB._retire_conv_into("aaaa1111", "cccc3333")
        e = hub.HUB.takeover_ledger["aaaa1111"]
        self.assertEqual(e["succ"], "cccc3333")
        self.assertEqual(e["old_name"], "待命·cursor工作流3")
        self.assertEqual(hub.HUB.takeover_aliases["aaaa1111"], "cccc3333")

    def test_book_roundtrip_v2(self):
        """落盘 → 重读，台账两个索引都得完整回来（v2 格式）。"""
        hub.HUB._retire_conv_into("aaaa1111", "bbbb2222", why="第一笔")
        book = hub.HUB._load_takeover_book()
        self.assertEqual(book["aliases"]["aaaa1111"]["succ"], "bbbb2222")
        self.assertEqual(book["aliases"]["aaaa1111"]["why"], "第一笔")

    def test_legacy_flat_file_still_loads(self):
        """08-26 前的平表文件照读：升级不丢账（拆分手术那笔就在里面）。"""
        self.book_path.write_text(
            json.dumps({"73004179": "9e528993"}), encoding="utf-8")
        book = hub.HUB._load_takeover_book()
        self.assertEqual(book["aliases"]["73004179"]["succ"], "9e528993")
        self.assertEqual(book["names"], {})

    def test_prune_keeps_ledger_and_flat_in_step(self):
        """封顶裁旧时两个视图一起裁，不许一边有一边没有。"""
        for i in range(205):
            hub.HUB._retire_conv_into("conv%04d" % i, "succ0000", why="灌容量")
        self.assertLessEqual(len(hub.HUB.takeover_ledger), 200)
        self.assertEqual(set(hub.HUB.takeover_ledger),
                         set(hub.HUB.takeover_aliases))


class TombstonePersistTests(_BookHarness):

    def test_tombstone_written_into_book_on_disk(self):
        """墓碑必须随台账落盘：修前只活在内存，hub 重启后按旧名转告变「找不到」。"""
        shell = _s("s1", "aaaa1111", "待命·cursor工作流3")
        succ = _s("s2", "bbbb2222", "rxyy tools·全面体检")
        hub.HUB._tombstone_shell(shell, succ)
        info = hub.HUB.name_tombstones.get("待命·cursor工作流3")
        self.assertIsNotNone(info)
        self.assertEqual(info["conv"], "aaaa1111")
        self.assertEqual(info["succ8"], "bbbb2222")
        self.assertIn("aaaa1111", succ.id_history, "曾用ID 展示副本照旧要写")
        book = hub.HUB._load_takeover_book()
        disk = book["names"].get("待命·cursor工作流3")
        self.assertIsNotNone(disk, "墓碑没落盘——重启后按旧名指路又会失效")
        self.assertEqual(disk["conv"], "aaaa1111")


class _FakeTSD:
    """假的透明加密后端：记录 decrypt_file 调用，可选地把目标改写成明文
    （模拟「密文态被原地解密后就能读了」）。"""

    def __init__(self, rewrite_to=None):
        self.calls = []
        self._rewrite = rewrite_to

    def decrypt_file(self, path):
        self.calls.append(str(path))
        if self._rewrite is not None:
            Path(path).write_text(self._rewrite, encoding="utf-8")
        return True

    def available(self):
        return True


class ResilientJsonLoadTests(unittest.TestCase):
    """_load_json_resilient：文件缺失 / 正常 / 密文可解 / 真不可读 四条路各自锁死。

    08-27 转告事故根因：takeover-aliases.json 处于 TSD 密文态时，读 JSON 抛异常被
    静默吞成空账，全部接手别名失效、开出重复 tab。这里锁死「不可读」绝不等同于
    「不存在」——前者必须举手、且先试解密救一把。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.p = Path(self._tmp.name) / "x.json"

    def test_absent_file_is_not_an_error(self):
        data, status = hub._load_json_resilient(self.p, what="测试")
        self.assertIsNone(data)
        self.assertEqual(status, "absent")

    def test_valid_json_reads_back(self):
        self.p.write_text(json.dumps({"a": 1}), encoding="utf-8")
        data, status = hub._load_json_resilient(self.p, what="测试")
        self.assertEqual(status, "ok")
        self.assertEqual(data, {"a": 1})

    def test_ciphertext_is_decrypted_then_read(self):
        # 首读是「密文」(坏 JSON)，解密后台账把它改写成合法明文 → 读回
        self.p.write_text("%TSD-Header-###% 密文乱码", encoding="utf-8")
        fake = _FakeTSD(rewrite_to=json.dumps({"ok": True}))
        logs = []
        with (patch.object(hub, "tsd_decrypt", fake),
              patch.object(hub, "log_event", lambda *a, **k: logs.append(a))):
            data, status = hub._load_json_resilient(self.p, what="接手台账")
        self.assertEqual(status, "ok")
        self.assertEqual(data, {"ok": True})
        self.assertEqual(fake.calls, [str(self.p)], "该对密文文件调一次原地解密")

    def test_truly_unreadable_raises_the_hand_not_silence(self):
        # 坏 JSON + 无解密能力 = 真读不了：返回 unreadable（绝不当空），并 log 举手
        self.p.write_text("{不是合法 json", encoding="utf-8")
        logs = []
        with (patch.object(hub, "tsd_decrypt", None),
              patch.object(hub, "log_event", lambda *a, **k: logs.append(a))):
            data, status = hub._load_json_resilient(self.p, what="接手台账")
        self.assertIsNone(data)
        self.assertEqual(status, "unreadable")
        self.assertTrue(logs, "读不了必须 log 举手，不能无声降级")


class LedgerLoadResilienceTests(_BookHarness):
    """_load_takeover_book 直接受益：密文能救回、真坏了也举手而非静默清空。"""

    def test_unreadable_book_logs_the_duplicate_tab_risk(self):
        self.book_path.write_text("{坏掉的台账", encoding="utf-8")
        logs = []
        with (patch.object(hub, "tsd_decrypt", None),
              patch.object(hub, "log_event", lambda *a, **k: logs.append(a[0]))):
            book = hub.HUB._load_takeover_book()
        # 读不了只能退空，但必须留下「会开重复 tab」的告警，不再无声
        self.assertEqual(book, {"aliases": {}, "names": {}})
        self.assertTrue(any("重复 tab" in m for m in logs),
                        "台账读不了要点名重复 tab 风险，08-27 转告事故同因")

    def test_ciphertext_book_is_recovered(self):
        # 盘上是「密文」，解密后台账把它改写成一笔真台账 → 别名恢复，不再退空
        real = json.dumps({"_v": 2,
                           "aliases": {"aaaa1111": {"succ": "bbbb2222", "why": "x"}},
                           "names": {}})
        self.book_path.write_text("%TSD-Header-###% 密文", encoding="utf-8")
        fake = _FakeTSD(rewrite_to=real)
        with (patch.object(hub, "tsd_decrypt", fake),
              patch.object(hub, "log_event", lambda *a, **k: None)):
            book = hub.HUB._load_takeover_book()
        self.assertEqual(book["aliases"]["aaaa1111"]["succ"], "bbbb2222")


class LedgerSaveEnsuresPlaintextTests(_BookHarness):
    """落盘即解密：DATA_DIR 若在被 TSD 监控的目录，写出的 .json 会被透明加密，
    下次冻结 exe 读到密文就退回空账——写完原地解密一次堵死这条复发路。"""

    def test_retire_triggers_ondisk_decrypt(self):
        fake = _FakeTSD()
        with (patch.object(hub, "tsd_decrypt", fake),
              patch.object(hub, "log_event", lambda *a, **k: None)):
            hub.HUB._retire_conv_into("aaaa1111", "bbbb2222", why="测试")
        self.assertIn(str(self.book_path), fake.calls,
                      "台账落盘后必须对它原地解密一次")


class LogPathFallbackTests(unittest.TestCase):
    """LOG_PATH 选址：APP_DIR 可写优先（源码态即现状），不可写才回退 DATA_DIR
    ——打包冻结形态 APP_DIR=_internal\\rxyy MCP 常只读，旧写法 log_event 在那儿
    静默写失败、重启现场无日志可查（08-27 转告事故的顺手发现）。"""

    def test_prefers_appdir_but_falls_back_to_datadir(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            good_app = d / "app"
            good_app.mkdir()
            data = d / "data"
            data.mkdir()
            with (patch.object(hub, "APP_DIR", good_app),
                  patch.object(hub, "DATA_DIR", data)):
                self.assertEqual(hub._pick_log_path(), good_app / "hub-run.log")
            # 用一个文件冒充 APP_DIR 的父目录 → mkdir/open 必失败 → 回退 DATA_DIR
            blocker = d / "blocker"
            blocker.write_text("x", encoding="utf-8")
            bad_app = blocker / "sub"
            with (patch.object(hub, "APP_DIR", bad_app),
                  patch.object(hub, "DATA_DIR", data)):
                self.assertEqual(hub._pick_log_path(), data / "hub-run.log")


if __name__ == "__main__":
    unittest.main()
