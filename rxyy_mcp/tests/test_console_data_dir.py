# -*- coding: utf-8 -*-
"""任务库落点解析 _console_data_dir 的顺位锁死。

08-27 转告事故：仓库根 data-dir.txt 只写了家机路径（本机不存在）→ 旧逻辑直接
兑底仓库根 data\\，而打包控制台读 dist\\rxyy-tools-community\\data\\ —— hub 与控制台各写
一份任务库：发给 查无名录直落飞鸽、完成任务 对入站编号一律「找不到」、同事经
分享站投的任务成了控制台看不见的孤儿。修法与 src/config.py::_data_dir 同款：
指针失效后先认 dist\\rxyy-tools-community\\data（存在才认），最后才另开 APP_ROOT/data。

这里把四级顺位一条条钉死：env → 指针 → dist 兑底 → APP_ROOT/data，
以及密文指针（透明加密盘同步来的非 utf-8 文件）不崩也不分家。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import share_server as ss


class ConsoleDataDirTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        # 环境变量顺位最高，跑测试的机器可能真设了它——摘干净，用完自动还原
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("RXYY_DATA_DIR", None)
        # app_root 固定指到沙箱，测试之间互不串
        rootp = patch.object(ss, "_console_root", lambda: self.root)
        rootp.start()
        self.addCleanup(rootp.stop)

    def _mk_bundled(self):
        bundled = self.root / "dist" / "rxyy-tools-community" / "data"
        bundled.mkdir(parents=True)
        return bundled

    def test_env_var_wins_over_everything(self):
        d = self.root / "envdata"
        d.mkdir()
        self._mk_bundled()
        os.environ["RXYY_DATA_DIR"] = str(d)
        self.assertEqual(ss._console_data_dir(), d)

    def test_valid_pointer_line_wins_over_bundled(self):
        target = self.root / "elsewhere"
        target.mkdir()
        self._mk_bundled()
        (self.root / "data-dir.txt").write_text(
            "# 注释行要跳过\n%s\n" % target, encoding="utf-8")
        self.assertEqual(ss._console_data_dir(), target)

    def test_foreign_machine_pointer_falls_to_bundled_dist_data(self):
        # 08-27 事故复现：指针整行是外机路径（本机不存在），dist 库在——
        # 必须跟 dist 走，绝不另开仓库根 data\ 分家
        bundled = self._mk_bundled()
        foreign = self.root / "不存在的外机目录" / "data"   # 故意不创建
        (self.root / "data-dir.txt").write_text(
            "%s\n" % foreign, encoding="utf-8")
        self.assertEqual(ss._console_data_dir(), bundled)

    def test_missing_pointer_still_prefers_bundled(self):
        bundled = self._mk_bundled()
        self.assertEqual(ss._console_data_dir(), bundled)

    def test_nothing_anywhere_falls_back_to_app_root_data(self):
        # dist 库不存在（同事拿走的纯源码拷贝）——保持老兑底，别指向一个空壳
        self.assertEqual(ss._console_data_dir(), self.root / "data")

    def test_ciphertext_pointer_does_not_crash_and_bundled_wins(self):
        # 透明加密盘同步来的 data-dir.txt 在别机上是密文（非 utf-8）：
        # 读失败要吞掉走兑底，不能把 UnicodeDecodeError 抛给任务库判成不可用
        (self.root / "data-dir.txt").write_bytes(
            b"%TSD-Header-###%\xff\xfe\x00\x01")
        bundled = self._mk_bundled()
        self.assertEqual(ss._console_data_dir(), bundled)


if __name__ == "__main__":
    unittest.main()
