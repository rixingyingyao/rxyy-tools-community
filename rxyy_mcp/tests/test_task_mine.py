# -*- coding: utf-8 -*-
"""投递人看自己的任务进度，并给已提交的任务追加图/文件。

投递令牌故意拿不到 /api/tasks/list 里别人的任务，所以进度必须按 id 列表问
/api/tasks/mine；补充走 /api/tasks/patch，不能改标题。状态平时也动不了，
唯一的例外是已处理/已归档的任务：一补充就是「打回重开」，回到已提交重新排队
（08-31 江平：复测发现问题时任务已经是已处理，页面上既补不了也重开不了）。
"""
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
CONSOLE_DIR = MODULE_DIR.parent / "console"
sys.path.insert(0, str(MODULE_DIR))
sys.path.insert(0, str(CONSOLE_DIR))

import share_server as ss
from api.taskstage.storage import TaskStageStorage


TOKEN = "t0k3nmine"
PORT = 39189


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
    def __init__(self):
        self.queued = []

    def get_state(self):
        return {"sessions": []}

    def get_messages(self, sid):
        return {"rev": 1, "messages": [], "draft": ""}

    def queue_message(self, session_id, text, images, who=None, files=None):
        self.queued.append({
            "sid": session_id, "text": text, "images": images or [],
            "who": who, "files": files or [],
        })
        return {"ok": True, "qid": "q1"}


def post_json(path, payload, token_qs):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s?%s" % (PORT, path, token_qs),
        data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept-Encoding", "identity")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def get_json(path):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path))
    req.add_header("Accept-Encoding", "identity")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


class TaskMineHandlerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = TaskStageStorage(Path(self.tmp.name))
        self.mine = self.storage.add_task({"title": "郭的需求", "description": "原描述"})
        self.other = self.storage.add_task({"title": "别人的需求", "description": "秘密"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_mine_returns_only_requested_ids_without_bodies(self):
        reply = ss._handle_task_mine(self.storage, {"ids": [self.mine["id"], "nope"]})
        self.assertTrue(reply["ok"])
        self.assertEqual(1, len(reply["tasks"]))
        item = reply["tasks"][0]
        self.assertEqual(self.mine["id"], item["id"])
        self.assertEqual("draft", item["status"])
        self.assertNotIn("description", item)
        self.assertNotIn("images", item)

    def test_mine_does_not_leak_unrequested_tasks(self):
        reply = ss._handle_task_mine(self.storage, {"ids": [self.mine["id"]]})
        ids = [t["id"] for t in reply["tasks"]]
        self.assertNotIn(self.other["id"], ids)

    def test_patch_appends_description_and_keeps_status(self):
        reply = ss._handle_task_patch(self.storage, {
            "id": self.mine["id"],
            "description": "再补一句",
            "who": "郭",
        })
        self.assertTrue(reply["ok"], reply)
        stored = self.storage.find(self.mine["id"])
        self.assertIn("【补充】", stored["description"])
        self.assertIn("再补一句", stored["description"])
        self.assertIn("原描述", stored["description"])
        self.assertEqual("draft", stored["status"])

    def test_patch_on_dispatched_forwards_to_the_session(self):
        self.storage.update_task(self.mine["id"], {
            "status": "dispatched",
            "dispatch_session_id": "sess-1",
            "session_name": "直播·联调",
        })
        api = FakeApi()
        reply = ss._handle_task_patch(self.storage, {
            "id": self.mine["id"],
            "description": "处理中再补图",
        }, hub_api=api)
        self.assertTrue(reply["ok"], reply)
        self.assertTrue(reply["forwarded"])
        self.assertEqual(1, len(api.queued))
        self.assertEqual("sess-1", api.queued[0]["sid"])
        self.assertIn("任务补充", api.queued[0]["text"])
        self.assertEqual("任务安排站", api.queued[0]["who"])

    def test_patch_empty_is_refused(self):
        reply = ss._handle_task_patch(self.storage, {"id": self.mine["id"]})
        self.assertFalse(reply["ok"])

    def test_0831_patch_reopens_a_done_task_like_jira(self):
        # 08-31 江平：已提交的能补充，已处理的却只能干瞪眼——复测发现问题时
        # 任务恰恰已经是已处理。补充已处理的任务＝打回重开，回到已提交重新排队。
        self.storage.update_task(self.mine["id"], {
            "status": "done",
            "dispatched_at": 123.0,
            "session_name": "直播·联调",
            "dispatch_session_id": "sess-1",
            "dispatch_conv_key": "ck",
            "dispatch_qid": "q9",
            "dispatch_direct": True,
            "dispatch_batch": "b1",
        })
        reply = ss._handle_task_patch(self.storage, {
            "id": self.mine["id"],
            "description": "复测还有问题：导出的音频还是没声",
            "who": "江平",
        })
        self.assertTrue(reply["ok"], reply)
        self.assertTrue(reply.get("reopened"), reply)
        self.assertEqual("draft", reply["task"]["status"])
        stored = self.storage.find(self.mine["id"])
        self.assertEqual("draft", stored["status"])
        self.assertIn("【重开】", stored["description"])
        self.assertIn("复测还有问题", stored["description"])
        self.assertIn("江平", stored["description"])
        self.assertIn("重开", stored["tags"])
        self.assertGreater(stored["reopened_at"], 0)
        # 上一轮的派发凭据全部清干净：那个会话早不在干这条了，
        # 留着只会让撤回/转告对着一个空号操作
        self.assertEqual("", stored["dispatch_session_id"])
        self.assertEqual("", stored["dispatch_qid"])
        self.assertEqual("", stored["session_name"])
        self.assertFalse(stored["dispatch_direct"])

    def test_0831_reopen_with_only_attachments_still_leaves_a_mark(self):
        # 只丢一张复测截图不写字，卡面上也得看得出「为什么回来了」
        self.storage.update_task(self.mine["id"], {"status": "done"})
        reply = ss._handle_task_patch(self.storage, {
            "id": self.mine["id"],
            "images": [{"b64": "eA==", "mime": "image/png", "name": "复测.png"}],
        })
        self.assertTrue(reply["ok"], reply)
        self.assertTrue(reply.get("reopened"), reply)
        stored = self.storage.find(self.mine["id"])
        self.assertEqual("draft", stored["status"])
        self.assertIn("【重开】", stored["description"])

    def test_0831_archived_task_reopens_the_same_way(self):
        self.storage.update_task(self.mine["id"], {"status": "archived"})
        reply = ss._handle_task_patch(self.storage, {
            "id": self.mine["id"], "description": "归档的也要能打回",
        })
        self.assertTrue(reply["ok"], reply)
        self.assertTrue(reply.get("reopened"), reply)
        self.assertEqual("draft", self.storage.find(self.mine["id"])["status"])

    def test_0831_ordinary_patch_is_not_marked_as_reopen(self):
        reply = ss._handle_task_patch(self.storage, {
            "id": self.mine["id"], "description": "普通补充",
        })
        self.assertTrue(reply["ok"], reply)
        self.assertFalse(reply.get("reopened"))
        stored = self.storage.find(self.mine["id"])
        self.assertNotIn("【重开】", stored["description"])
        self.assertNotIn("重开", stored["tags"])


class TaskIntakeReopenUiTests(unittest.TestCase):
    """入站页那半：改坏了不报错，只是「重开」按钮永远不出现。"""

    @classmethod
    def setUpClass(cls):
        cls.html = (MODULE_DIR / "tasks.html").read_text(encoding="utf-8")

    def test_0831_done_tasks_are_no_longer_locked_out_of_patching(self):
        # 原来 mineAsTasks 把 done/archived 的 _canPatch 直接写死成 false
        self.assertNotIn("st !== 'done' && st !== 'archived'", self.html)

    def test_0831_the_button_reads_reopen_on_finished_tasks(self):
        self.assertIn("打回重开", self.html)
        self.assertIn("提交重开", self.html)

    def test_0831_reopen_feedback_says_it_went_back_to_the_queue(self):
        self.assertIn("已打回重开", self.html)


class TaskMineHttpTests(unittest.TestCase):
    srv = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / "图片").mkdir(parents=True, exist_ok=True)
        cls.store_dir = root / "taskstage"
        cls.storage = TaskStageStorage(cls.store_dir)
        cls.mine = cls.storage.add_task({"title": "郭的需求"})
        cls.other = cls.storage.add_task({"title": "别人的需求", "description": "秘密"})
        cls._real_dir = ss._taskstage_dir
        cls._storage = ss._TASK_STORAGE
        cls._retry = ss._TASK_STORAGE_RETRY_AT
        ss._taskstage_dir = lambda: cls.store_dir
        ss._TASK_STORAGE = cls.storage
        ss._TASK_STORAGE_RETRY_AT = 0.0
        cls.api = FakeApi()
        cls.srv = ss.start_share_server(FakeHub(root), cls.api)
        if cls.srv is None:
            ss._taskstage_dir = cls._real_dir
            ss._TASK_STORAGE = cls._storage
            ss._TASK_STORAGE_RETRY_AT = cls._retry
            cls.tmp.cleanup()
            raise unittest.SkipTest("端口 %d 被占用" % PORT)

    @classmethod
    def tearDownClass(cls):
        ss._taskstage_dir = cls._real_dir
        ss._TASK_STORAGE = cls._storage
        ss._TASK_STORAGE_RETRY_AT = cls._retry
        if cls.srv is not None:
            cls.srv.shutdown()
            cls.srv.server_close()
        cls.tmp.cleanup()

    def test_intake_list_stays_empty(self):
        k = ss.intake_token({"share_token": TOKEN})
        body = get_json("/api/tasks/list?k=" + k)
        self.assertTrue(body.get("intake"))
        self.assertEqual([], body.get("tasks"))

    def test_intake_mine_returns_own_task_status(self):
        k = ss.intake_token({"share_token": TOKEN})
        body = post_json("/api/tasks/mine", {"ids": [self.mine["id"], self.other["id"]]},
                         "k=" + k)
        self.assertTrue(body["ok"], body)
        ids = [t["id"] for t in body["tasks"]]
        self.assertEqual([self.mine["id"], self.other["id"]], ids)
        self.assertNotIn("description", body["tasks"][0])
        self.assertLess(len(json.dumps(body)), 2000)

    def test_intake_patch_appends(self):
        k = ss.intake_token({"share_token": TOKEN})
        body = post_json("/api/tasks/patch", {
            "id": self.mine["id"], "description": "手机再补一句",
        }, "k=" + k)
        self.assertTrue(body["ok"], body)
        stored = self.storage.find(self.mine["id"])
        self.assertIn("手机再补一句", stored["description"])

    def test_mine_needs_a_token(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            post_json("/api/tasks/mine", {"ids": [self.mine["id"]]}, "")
        self.assertEqual(403, caught.exception.code)


if __name__ == "__main__":
    unittest.main()
