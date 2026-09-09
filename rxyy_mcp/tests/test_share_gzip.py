# -*- coding: utf-8 -*-
"""分享服务的响应压缩与静态缓存（08-07：手机端体感优化）。

手机走隧道/4G 时带宽才是瓶颈：20 多个会话的 /api/state 一拍 17KB，压完 2KB 不到。
这里锁死三件容易压错就整页打不开的事：
1. 客户端说支持 gzip 才压，且 Content-Length 必须是压缩后的字节数（HTTP/1.1
   长连接下写错一个字节，后面所有请求全错位）；
2. 不支持 gzip 的客户端拿到的仍是原文；
3. 图片本来就是压缩格式，别再压一遍白烧 CPU；小响应也不压。
另外 highlight.min.js 130KB 且从不变，必须给长缓存，不能跟接口一样 no-store。
"""
import gzip
import json
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import share_server as ss

TOKEN = "t0k3n"
PORT = 39187


class FakeHub:
    def __init__(self, tmp):
        self.cfg = {
            "share_port": PORT,
            "share_token": TOKEN,
            "share_enabled": True,
            "lan_first": False,          # 测试里别让它 302 跑去局域网地址
            "history_dir": str(tmp),
            "continue_prompt": "继续",
            "quick_phrases": ["用中文"],
        }


class FakeApi:
    """会话数量与字段量比着真实规模来，才能压出真实的比值。"""

    def get_state(self):
        return {"sessions": [{
            "id": "s%02d" % i, "name": "待命·项目%d" % i, "label": "待命·项目%d" % i,
            "cwd": "d:\\桌面\\working\\项目%d" % i, "connected": True, "pending": False,
            "rev": i, "options": [], "live_state": "idle", "live_label": "待机中",
        } for i in range(23)]}

    def get_messages(self, sid):
        return {"rev": 1, "messages": [], "draft": ""}


def get(path, gzip_ok=True):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path))
    req.add_header("Accept-Encoding", "gzip" if gzip_ok else "identity")
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read(), dict(r.headers)


class ShareGzipTests(unittest.TestCase):
    srv = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        (Path(cls.tmp.name) / "图片").mkdir(parents=True, exist_ok=True)
        cls.srv = ss.start_share_server(FakeHub(Path(cls.tmp.name)), FakeApi())
        if cls.srv is None:
            cls.tmp.cleanup()
            raise unittest.SkipTest("端口 %d 被占用" % PORT)
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        if cls.srv is not None:
            cls.srv.shutdown()
            cls.srv.server_close()
        cls.tmp.cleanup()

    def test_state_is_gzipped_and_decodes_back_to_the_same_json(self):
        raw, hdr = get("/api/state?t=" + TOKEN)
        self.assertEqual("gzip", hdr.get("Content-Encoding"))
        self.assertEqual("Accept-Encoding", hdr.get("Vary"))
        # urllib 不会自动解压，raw 就是线上真正传的字节
        body = json.loads(gzip.decompress(raw))
        self.assertEqual(23, len(body["sessions"]))
        # 压不下三成就说明压根没生效（JSON 这种重复键的文本实测能省九成）
        self.assertLess(len(raw), len(json.dumps(body, ensure_ascii=False).encode()) * 0.7)

    def test_content_length_counts_the_compressed_bytes(self):
        # HTTP/1.1 长连接下这个数一错，后面每个请求都会读到上一个的残尾
        raw, hdr = get("/api/state?t=" + TOKEN)
        self.assertEqual(len(raw), int(hdr["Content-Length"]))

    def test_client_without_gzip_still_gets_plain_json(self):
        raw, hdr = get("/api/state?t=" + TOKEN, gzip_ok=False)
        self.assertIsNone(hdr.get("Content-Encoding"))
        self.assertEqual(23, len(json.loads(raw)["sessions"]))

    def test_small_replies_are_left_alone(self):
        raw, hdr = get("/api/messages?sid=s01&t=" + TOKEN)
        self.assertIsNone(hdr.get("Content-Encoding"))
        self.assertEqual([], json.loads(raw)["messages"])

    def test_highlight_js_is_cacheable_for_a_long_time(self):
        _, hdr = get("/highlight.min.js")
        self.assertIn("max-age=", hdr.get("Cache-Control", ""))
        self.assertNotIn("no-store", hdr.get("Cache-Control", ""))

    def test_api_replies_stay_uncached(self):
        _, hdr = get("/api/state?t=" + TOKEN)
        self.assertEqual("no-store", hdr.get("Cache-Control"))

    def test_unchanged_state_costs_almost_nothing(self):
        """压缩管不了「这一拍其实什么都没发生」，那才是待机时的大头。

        手机揣兜里那几百拍绝大多数毫无变化，却每拍都在重传整份状态。带上上一拍
        的摘要，没变就只回一句「没变」——差两个数量级。
        """
        full, _ = get("/api/state?t=" + TOKEN, gzip_ok=False)
        etag = json.loads(full)["etag"]
        same, _ = get("/api/state?t=%s&known=%s" % (TOKEN, etag), gzip_ok=False)
        body = json.loads(same)
        self.assertTrue(body["same"])
        self.assertNotIn("sessions", body)
        self.assertLess(len(same), len(full) / 50)

    def test_a_stale_etag_still_gets_the_whole_thing(self):
        # 摘要对不上就必须给整份，否则页面会永远停在旧画面上
        raw, _ = get("/api/state?t=%s&known=%s" % (TOKEN, "0" * 16), gzip_ok=False)
        body = json.loads(raw)
        self.assertNotIn("same", body)
        self.assertEqual(23, len(body["sessions"]))

    def test_etag_moves_when_the_state_moves(self):
        first = json.loads(get("/api/state?t=" + TOKEN, gzip_ok=False)[0])["etag"]
        self.assertEqual(first, json.loads(get("/api/state?t=" + TOKEN, gzip_ok=False)[0])["etag"])
        # 会话名改一个字都得算变了，否则就是「偶尔一条更新永远不出现」
        original = FakeApi.get_state
        try:
            FakeApi.get_state = lambda self: {"sessions": [{"id": "s00", "name": "改过名了"}]}
            moved = json.loads(get("/api/state?t=" + TOKEN, gzip_ok=False)[0])["etag"]
        finally:
            FakeApi.get_state = original
        self.assertNotEqual(first, moved)

    def test_page_is_gzipped_too(self):
        raw, hdr = get("/?t=" + TOKEN)
        self.assertEqual("gzip", hdr.get("Content-Encoding"))
        self.assertEqual(len(raw), int(hdr["Content-Length"]))
        self.assertIn(b"<!DOCTYPE html>", gzip.decompress(raw)[:64])


if __name__ == "__main__":
    unittest.main()
