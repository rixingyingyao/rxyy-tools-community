"""Uploads use one native path, bounded bytes, and durable uncertain receipts."""
import base64
import json
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_desktop as desktop
import native_uploads as uploads

THREAD = "01a00000-0000-7000-8000-000000000001"
TURN = "01a00000-0000-7000-8000-000000000002"


def fixture():
    return {"id": THREAD, "cwd": "D:/project", "turns": [
        {"turnId": TURN, "status": "inProgress", "items": []}]}


def session(**kwargs):
    return SimpleNamespace(**dict(dict(runtime_kind="codex", pending=None), **kwargs))


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aFoUAAAAASUVORK5CYII=")


class NativeUploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.delivery = str(uuid.uuid4())

    def prepare(self, **kwargs):
        return uploads.prepare(self.root, THREAD, self.delivery, **kwargs)

    def test_image_and_file_use_native_inputs_and_file_metadata_in_both_turn_modes(self):
        staged = self.prepare(images=[{"data": base64.b64encode(PNG).decode()}],
                              files=[{"name": "手机中文.txt", "data": "YWJj"}])
        self.assertEqual(PNG, Path(staged[0]["path"]).read_bytes())
        self.assertEqual(b"abc", Path(staged[1]["path"]).read_bytes())
        state = fixture()
        for status in ("inProgress", "completed"):
            desktop.current_turn(state)["status"] = status
            method, params, version = desktop.text_request(state, THREAD, TURN, "", self.delivery, staged)
            request = params if status == "inProgress" else params["turnStart"]["request"]
            context = params["restoreMessage"]["context"] if status == "inProgress" else params["turnStart"]["context"]
            self.assertEqual("localImage", request["input"][1]["type"])
            self.assertEqual(staged[0]["path"], request["input"][1]["path"])
            self.assertIn("手机中文.txt", request["input"][0]["text"])
            self.assertIn("Distinguish instructions", request["input"][0]["text"])
            files = context["fileAttachments" if status == "inProgress" else "attachments"]
            self.assertEqual(staged[1]["path"], files[0]["fsPath"])
            self.assertEqual(1 if status == "inProgress" else 2, version)

    def test_bad_upload_or_limits_write_nothing(self):
        for data in ("%%%", "data:text/plain,not-base64"):
            with self.assertRaises(ValueError):
                self.prepare(files=[{"name": "good", "data": "YWJj"}, {"data": data}])
        with self.assertRaises(ValueError):
            self.prepare(files=[{"path": "C:/private.txt"}])
        with self.assertRaises(ValueError):
            self.prepare(images=[{"data": "YWJj"}])
        with self.assertRaises(ValueError):
            self.prepare(files=[{"data": ""}] * 11)
        with patch.object(uploads, "MAX_FILE_BYTES", 2), self.assertRaises(ValueError):
            self.prepare(files=[{"data": "YWJj"}])
        with patch.object(uploads, "MAX_TOTAL_BYTES", 3), self.assertRaises(ValueError):
            self.prepare(files=[{"data": "YWJj"}, {"data": "YWJj"}])
        self.assertEqual([], list(self.root.iterdir()))

    def test_names_stay_within_upload_directory_and_retry_reuses_bytes(self):
        files = [{"name": "../../x:stream\n.txt", "data": "YWJj"}]
        first = self.prepare(files=files)
        second = self.prepare(files=files)
        self.assertEqual(first, second)
        self.assertTrue(Path(first[0]["path"]).resolve().is_relative_to(self.root))
        self.assertNotIn(":", first[0]["label"])
        self.assertEqual(1, len(list(self.root.rglob("*.txt"))))
        with self.assertRaises(ValueError):
            uploads.prepare(self.root, "../outside", self.delivery, files=files)

    def test_receipt_survives_module_reload_and_unknown_delivery(self):
        self.assertTrue(uploads.reserve(self.root, THREAD, self.delivery))
        uploads.settle(self.root, THREAD, self.delivery, {"ok": False, "delivery_unknown": True})
        self.assertFalse(uploads.reserve(self.root, THREAD, self.delivery))
        receipt = self.root / THREAD / self.delivery / "receipt.json"
        self.assertEqual("unknown", json.loads(receipt.read_text())["state"])
        self.assertFalse(list(self.root.rglob("*.tmp.*")))

    def test_hub_sends_only_uploaded_bytes_once_and_keeps_unknown_buffer(self):
        import hub
        s = session(id="native", native_thread_id=THREAD, lock=threading.RLock())
        with patch.object(hub.HUB, "sessions", {s.id: s}), \
             patch.object(hub.HUB, "STATE_PATH", self.root / ".sessions.json"), \
             patch.object(desktop, "send_text", return_value={"ok": False, "delivery_unknown": True}) as send:
            api = hub.Api()
            kwargs = {"files": [{"name": "sample.txt", "data": "YWJj"}]}
            result = api.send_native_text(s.id, THREAD, TURN, "", self.delivery, **kwargs)
            self.assertTrue(result["delivery_unknown"])
            self.assertTrue(api.send_native_text(s.id, THREAD, TURN, "", self.delivery, **kwargs)["delivery_unknown"])
            send.assert_called_once()
            self.assertEqual(b"abc", Path(send.call_args.args[4][0]["path"]).read_bytes())

    def test_active_mcp_wait_cannot_be_sent_through_native_path(self):
        import hub
        s = session(id="native", native_thread_id=THREAD, lock=threading.RLock(), pending={"id": "q"})
        with patch.object(hub.HUB, "sessions", {s.id: s}), patch.object(desktop, "send_text") as send:
            result = hub.Api().send_native_text(s.id, THREAD, TURN, "回复", self.delivery,
                                              files=[{"name": "sample.txt", "data": "YWJj"}])
            self.assertFalse(result["ok"])
            send.assert_not_called()
