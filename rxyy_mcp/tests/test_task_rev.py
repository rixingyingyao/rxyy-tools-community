# -*- coding: utf-8 -*-
"""投递站的「任务库变了没有」轻接口（08-07：让它自己会更新）。

以前列表只在打开页面时拉一次，同事投进来的需求得手动点↻才看得见。想让它自己
更新就得轮询，可 /api/tasks/list 把每张截图都内联成 data URI——线上实测一次
511,861 字节，按秒拉在手机上是灾难。所以先问一句 /api/tasks/rev（只 stat 一下
库文件，46 字节），变了才去拉整份。

这里锁死三件错了就「表面正常、实际一直不刷」的事：
1. rev 必须真的跟着库文件变，且答复要足够小，否则轻接口就没有意义；
2. 库文件不在时必须回空串而不是某个固定值——空串是「不知道」，页面会当它没
   答上来；要是回了个稳定的假值，页面会一直以为「没变」，一天都不刷新；
3. 加载任务库失败后不能永久放弃：8-07 16:26 切常驻区那趟就是第一次请求没加载
   成，之后半小时投递站全回「任务库不可用」，直到有人重启才好。
"""
import json
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import share_server as ss

TOKEN = "t0k3nrev"
PORT = 39188


class FakeHub:
    def __init__(self, tmp):
        self.cfg = {
            "share_port": PORT,
            "share_token": TOKEN,
            "share_enabled": True,
            "lan_first": False,
            "history_dir": str(tmp),
            "continue_prompt": "继续",
            "quick_phrases": [],
        }


class FakeApi:
    def get_state(self):
        return {"sessions": []}

    def get_messages(self, sid):
        return {"rev": 1, "messages": [], "draft": ""}


def get_json(path):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path))
    req.add_header("Accept-Encoding", "identity")
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read()
    return json.loads(raw), len(raw)


class TaskRevTests(unittest.TestCase):
    srv = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / "图片").mkdir(parents=True, exist_ok=True)
        cls.store_dir = root / "taskstage"
        cls.store_dir.mkdir(parents=True, exist_ok=True)
        cls.store = cls.store_dir / ".task_stage.json"
        cls.store.write_text('{"tasks": []}', encoding="utf-8")
        cls._real_dir = ss._taskstage_dir
        ss._taskstage_dir = lambda: cls.store_dir
        cls.srv = ss.start_share_server(FakeHub(root), FakeApi())
        if cls.srv is None:
            ss._taskstage_dir = cls._real_dir
            cls.tmp.cleanup()
            raise unittest.SkipTest("端口 %d 被占用" % PORT)
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        ss._taskstage_dir = cls._real_dir
        if cls.srv is not None:
            cls.srv.shutdown()
            cls.srv.server_close()
        cls.tmp.cleanup()

    def test_rev_reply_is_tiny(self):
        body, size = get_json("/api/tasks/rev?t=" + TOKEN)
        self.assertTrue(body["ok"])
        self.assertTrue(body["rev"])
        # 线上 /api/tasks/list 是 51 万字节。这个但凡上了 KB 级就失去意义了
        self.assertLess(size, 200)

    def test_rev_changes_when_the_store_changes(self):
        before = get_json("/api/tasks/rev?t=" + TOKEN)[0]["rev"]
        time.sleep(0.02)
        self.store.write_text('{"tasks": [{"id": "x"}]}', encoding="utf-8")
        after = get_json("/api/tasks/rev?t=" + TOKEN)[0]["rev"]
        self.assertNotEqual(before, after)

    def test_rev_holds_still_when_nothing_happens(self):
        # 每一拍都「变了」的话，页面会一直去拉那 51 万字节，比不轮询还糟
        first = get_json("/api/tasks/rev?t=" + TOKEN)[0]["rev"]
        time.sleep(0.05)
        self.assertEqual(first, get_json("/api/tasks/rev?t=" + TOKEN)[0]["rev"])

    def test_missing_store_answers_empty_not_a_fake_value(self):
        # 空串＝「不知道」，页面会退回到「切回来才刷」；要是回个稳定的假值，
        # 页面会一直以为没变，同事投的需求一天都不出现
        self.store.unlink()
        try:
            body, _ = get_json("/api/tasks/rev?t=" + TOKEN)
            self.assertTrue(body["ok"])
            self.assertEqual("", body["rev"])
        finally:
            self.store.write_text('{"tasks": []}', encoding="utf-8")

    def test_rev_needs_a_token(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            get_json("/api/tasks/rev")
        self.assertEqual(403, caught.exception.code)


class TaskStorageRetryTests(unittest.TestCase):
    """加载任务库失败后要肯再试——8-07 16:26 那半小时空窗就是栽在这上面。"""

    def setUp(self):
        self._storage = ss._TASK_STORAGE
        self._retry_at = ss._TASK_STORAGE_RETRY_AT
        ss._TASK_STORAGE = None
        ss._TASK_STORAGE_RETRY_AT = 0.0

    def tearDown(self):
        ss._TASK_STORAGE = self._storage
        ss._TASK_STORAGE_RETRY_AT = self._retry_at

    def test_a_failed_load_is_retried_later_not_given_up_on_forever(self):
        has_console = ((ss._console_root() / "console" / "api" / "taskstage" / "storage.py").is_file()
                       or (ss.APP_DIR.parent / "console" / "api" / "taskstage"
                           / "storage.py").is_file())
        if not has_console:
            # console 源码不在场（AgentDeck 独立仓/干净环境）时，_task_storage 在
            # 找 storage.py 那步就降级返回，走不到被打桩的 _taskstage_dir——
            # 任务安排站本就是 rxyy tools console 域的跨域功能，此环境跳过
            self.skipTest("console 域不在场，任务库加载路径测不到")
        calls = []
        real = ss._taskstage_dir

        def boom():
            calls.append(1)
            raise OSError("常驻区刚起来，指针还没落地")

        ss._taskstage_dir = boom
        try:
            self.assertIsNone(ss._task_storage())
            self.assertEqual(1, len(calls))
            # 冷却期内不重复试，免得每个请求都去 import 一遍
            self.assertIsNone(ss._task_storage())
            self.assertEqual(1, len(calls))
            # 冷却过后必须肯再试一次
            ss._TASK_STORAGE_RETRY_AT = 0.0
            self.assertIsNone(ss._task_storage())
            self.assertEqual(2, len(calls))
        finally:
            ss._taskstage_dir = real


if __name__ == "__main__":
    unittest.main()
