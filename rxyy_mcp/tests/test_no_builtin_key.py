# -*- coding: utf-8 -*-
"""hub.py 是随分发包原样发出去的源码文件，里面不能内置任何真 key。

08-03 可分发化改造把 src/config.py 的内置 key 清了，却漏了 hub.py 手机语音输入
那一枚兜底 key：拿到包的人不填自己的 key 照样能用，烧的是 rxyy 的百炼额度。
key 一律走 rxyy tools 设置页写在 workflow.db kv_config 里的那份。
"""
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub  # noqa: E402

# 百炼 sk-、退款 UK-：两家都是「一串字面量就能花钱」的形态
KEY_LITERAL = re.compile(r"""['"](sk-[0-9a-zA-Z]{20,}|UK-[0-9A-Za-z-]{10,})['"]""")


class NoBuiltinKeyTests(unittest.TestCase):
    def test_source_carries_no_literal_key(self):
        src = (MODULE_DIR / "hub.py").read_text(encoding="utf-8")
        self.assertEqual([], KEY_LITERAL.findall(src),
                         "hub.py 里不能内置真 key——这份文件随分发包一起发给别人")

    def test_kv_config_get_reads_settings_page_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / "data"
            data.mkdir()
            conn = sqlite3.connect(str(data / "workflow.db"))
            conn.execute("CREATE TABLE kv_config (k TEXT PRIMARY KEY, v TEXT)")
            conn.execute("INSERT INTO kv_config VALUES ('bailian_api_key', 'sk-test-value')")
            conn.commit()
            conn.close()
            with patch.object(hub, "rxyy_data_dirs", lambda: [data]):
                self.assertEqual("sk-test-value", hub.kv_config_get("bailian_api_key"))
                self.assertEqual("", hub.kv_config_get("没这个键"))

    def test_kv_config_get_survives_missing_db(self):
        """新装机首启时 data\\ 还是空的，读不到只能给空串，不能把调用方崩掉。"""
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(hub, "rxyy_data_dirs", lambda: [Path(tmp)]):
                self.assertEqual("", hub.kv_config_get("bailian_api_key"))


if __name__ == "__main__":
    unittest.main()
