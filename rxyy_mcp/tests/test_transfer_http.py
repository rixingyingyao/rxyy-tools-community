"""The mobile transfer APIs retain the existing full-token boundary."""
import http.client
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import share_server


class FakeApi:
    def __init__(self):
        self.calls = []

    def list_known_models(self):
        self.calls.append("models")
        return {"ok": True, "models": [{"model": "test-model"}]}

    def list_native_models(self, path=""):
        self.calls.append(("native-models", path))
        return {"ok": True, "models": [{"model": "gpt-6-astra", "efforts": ["max"]}]}

    def answer_native_question(self, *args):
        self.calls.append(("answer", args))
        return {"ok": True, "delivery": "native_accepted"}

    def send_native_text(self, *args, **kwargs):
        self.calls.append(("send", args, kwargs))
        return {"ok": True, "delivery": "native_steered"}

    def resume_native_task(self, *args):
        self.calls.append(("resume", args))
        return {"ok": True, "delivery": "native_started"}

    def prepare_native_transfer(self, sid):
        self.calls.append(("native", sid))
        return {"ok": True, "dispatched": False, "url": "codex://new?prompt=test"}

    def prepare_native_new(self, path, prompt):
        self.calls.append(("new", path, prompt))
        return {"ok": True, "dispatched": False, "url": "codex://new?path=test"}

    def create_native_task(self, path, prompt, model, effort):
        self.calls.append(("create-native", path, prompt, model, effort))
        return {"ok": True, "created": True, "opened": True,
                "dispatched": bool(prompt), "thread_id": "thread"}

    def open_codex_link(self, url):
        self.calls.append(("open", url))
        return {"ok": True, "dispatched": False}

    def ext_takeover_new(self, sids, instance, **kwargs):
        self.calls.append((sids, instance, kwargs))
        return {"ok": True}


class TransferHttpTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.hub = SimpleNamespace(cfg={"share_port": 0, "share_token": "test-only",
            "share_enabled": True, "lan_first": False})
        self.server = share_server.start_share_server(self.hub, self.api)
        self.assertIsNotNone(self.server)
        self.conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)

    def tearDown(self):
        self.conn.close()
        self.server.shutdown()
        self.server.server_close()

    def request(self, path, body=None, token="test-only"):
        headers = {"X-Share-Token": token, "Content-Type": "application/json"}
        self.conn.request("GET" if body is None else "POST", path,
                          None if body is None else json.dumps(body), headers)
        response = self.conn.getresponse()
        data = response.read()
        return response.status, json.loads(data)

    def test_models_require_full_token(self):
        code, body = self.request("/api/models", token="wrong")
        self.assertEqual(403, code)
        self.assertEqual([], self.api.calls)
        self.assertEqual(200, self.request("/api/models")[0])

    def test_native_models_use_effective_project_and_require_full_token(self):
        path = "/api/native_models?project_path=C%3A%2Fproject"
        self.assertEqual(403, self.request(path, token="wrong")[0])
        self.assertEqual([], self.api.calls)
        code, body = self.request(path)
        self.assertEqual(200, code)
        self.assertTrue(body["ok"])
        self.assertEqual([("native-models", "C:/project")], self.api.calls)
        self.api.calls.clear()

    def test_native_answer_is_scoped_to_the_shared_session(self):
        token = share_server.session_token(self.hub.cfg, "source")
        payload = {"sid": "source", "thread_id": "thread", "turn_id": "turn",
                   "request_id": "call", "answers": {"question": "user answer"}}
        code, body = self.request("/api/native_answer", payload, token=token)
        self.assertEqual(200, code)
        self.assertEqual("native_accepted", body["delivery"])
        self.assertEqual([("answer", ("source", "thread", "turn", "call", payload["answers"]))], self.api.calls)
        self.api.calls.clear()
        self.assertEqual(403, self.request("/api/native_answer", {**payload, "sid": "another"}, token=token)[0])
        self.assertEqual(403, self.request("/api/native_answer", payload, token="wrong")[0])
        self.assertEqual([], self.api.calls)

    def test_native_text_is_scoped_to_session_and_keeps_exact_binding(self):
        token = share_server.session_token(self.hub.cfg, "source")
        payload = {"sid": "source", "thread_id": "thread", "turn_id": "turn",
                   "text": "继续", "delivery_id": "message-id", "images": [],
                   "files": [], "selected": []}
        code, body = self.request("/api/native_send", payload, token=token)
        self.assertEqual(200, code)
        self.assertEqual("native_steered", body["delivery"])
        self.assertEqual("send", self.api.calls[0][0])
        self.assertEqual(("source", "thread", "turn", "继续", "message-id"), self.api.calls[0][1])
        self.assertEqual("同事", self.api.calls[0][2]["who"])
        self.api.calls.clear()
        self.assertEqual(403, self.request(
            "/api/native_send", {**payload, "sid": "another"}, token=token)[0])
        self.assertEqual([], self.api.calls)

    def test_native_resume_is_scoped_to_the_same_shared_session(self):
        token = share_server.session_token(self.hub.cfg, "source")
        payload = {"sid": "source", "thread_id": "thread"}
        code, body = self.request("/api/native_resume", payload, token=token)
        self.assertEqual(200, code)
        self.assertEqual("native_started", body["delivery"])
        self.assertEqual([("resume", ("source", "thread"))], self.api.calls)
        self.api.calls.clear()
        self.assertEqual(403, self.request(
            "/api/native_resume", {**payload, "sid": "another"}, token=token)[0])
        self.assertEqual([], self.api.calls)

    def test_single_session_token_cannot_prepare_native_transfer(self):
        token = share_server.session_token(self.hub.cfg, "source")
        code, body = self.request("/api/takeover_native", {"sid": "source"}, token=token)
        self.assertEqual(200, code)  # 已有业务 API 用 ok=false 表示单会话权限不足
        self.assertFalse(body["ok"])
        self.assertIn("总令牌", body["error"])
        self.assertEqual([], self.api.calls)

    def test_native_preparation_does_not_claim_dispatched(self):
        code, body = self.request("/api/takeover_native", {"sid": "source"})
        self.assertEqual(200, code)
        self.assertFalse(body["dispatched"])
        self.assertEqual([("native", "source")], self.api.calls)

    def test_native_new_and_host_open_keep_draft_semantics(self):
        code, body = self.request("/api/native_new", {"sid": "source", "project_path": "C:/project", "prompt": "new"})
        self.assertEqual(200, code)
        self.assertFalse(body["dispatched"])
        code, opened = self.request("/api/native_open", {"sid": "source", "url": body["url"]})
        self.assertEqual(200, code)
        self.assertFalse(opened["dispatched"])
        self.assertEqual([("new", "C:/project", "new"), ("open", body["url"])], self.api.calls)

    def test_native_create_passes_exact_model_and_effort(self):
        code, body = self.request("/api/native_create", {"sid": "source",
            "project_path": "C:/project", "prompt": "new",
            "model": "gpt-6-astra", "effort": "max"})
        self.assertEqual(200, code)
        self.assertTrue(body["created"])
        self.assertTrue(body["dispatched"])
        self.assertEqual([("create-native", "C:/project", "new",
                           "gpt-6-astra", "max")], self.api.calls)
        self.api.calls.clear()

    def test_single_session_token_cannot_create_or_open_host_tasks(self):
        token = share_server.session_token(self.hub.cfg, "source")
        for path, payload in (
            ("/api/native_new", {"sid": "source", "project_path": "C:/project", "prompt": "new"}),
            ("/api/native_open", {"sid": "source", "url": "codex://new?path=test"}),
            ("/api/native_create", {"sid": "source", "project_path": "C:/project",
                                    "prompt": "", "model": "gpt-6-astra", "effort": "max"}),
        ):
            with self.subTest(path=path):
                code, body = self.request(path, payload, token=token)
                self.assertEqual(200, code)
                self.assertFalse(body["ok"])
        self.assertEqual([], self.api.calls)

    def test_mobile_model_reaches_transfer_api_unchanged(self):
        model = {"model": "test", "max": True,
                 "parameters": [{"id": "effort", "value": "high"}]}
        code, _ = self.request("/api/takeover_new", {
            "sid": "source", "instance": "window", "name": "next", "model": model})
        self.assertEqual(200, code)
        self.assertEqual(model, self.api.calls[0][2]["model"])
        self.assertEqual("next", self.api.calls[0][2]["name"])


if __name__ == "__main__":
    unittest.main()
