# -*- coding: utf-8 -*-
"""新建对话的思考档必须跟 Cursor 本机目录走，不能全局共用一份。"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub  # noqa: E402
import session_locator  # noqa: E402

REACTIVE_KEY = session_locator._REACTIVE_STORAGE_KEY

GROK_EFFORT = {
    "id": "effort", "name": "Effort",
    "parameterType": {"enumParameter": {"values": [
        {"value": "low", "displayName": "Low"},
        {"value": "medium", "displayName": "Medium"},
        {"value": "high", "displayName": "High"},
        {"value": "xhigh", "displayName": "Extra High"},
    ]}},
}
GROK_FAST = {
    "id": "fast", "name": "Fast",
    "parameterType": {"booleanParameter": {"values": [
        {"value": "false"}, {"value": "true", "displayName": "Fast"},
    ]}},
}
COMPOSER_FAST = GROK_FAST
SOL_REASONING = {
    "id": "reasoning", "name": "Reasoning",
    "parameterType": {"enumParameter": {"values": [
        {"value": "none", "displayName": "None"},
        {"value": "low", "displayName": "Low"},
        {"value": "high", "displayName": "High"},
        {"value": "xhigh", "displayName": "Extra High"},
        {"value": "max", "displayName": "Max"},
    ]}},
}
FABLE_EFFORT = {
    "id": "effort", "name": "Effort",
    "parameterType": {"enumParameter": {"values": [
        {"value": "low", "displayName": "Low"},
        {"value": "max", "displayName": "Max"},
    ]}},
}


def _entry(name, defs, default_on=True, **extra):
    row = {
        "name": name, "clientDisplayName": name, "defaultOn": default_on,
        "supportsAgent": True, "supportsMaxMode": True, "supportsNonMaxMode": True,
        "parameterDefinitions": defs,
    }
    row.update(extra)
    return row


def _fake_catalog_db(tmp, entries, prefs=None):
    root = Path(tmp)
    gs = root / "Cursor" / "User" / "globalStorage"
    gs.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(gs / "state.vscdb"))
    con.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
    payload = {
        "availableDefaultModels2": entries,
        "aiSettings": {"modelParameterPreferences": prefs or {}},
    }
    con.execute("INSERT INTO ItemTable VALUES (?,?)",
                (REACTIVE_KEY, json.dumps(payload)))
    con.commit()
    con.close()
    return str(root)


class CatalogParseTests(unittest.TestCase):
    def setUp(self):
        session_locator.reset_catalog_cache()

    def tearDown(self):
        session_locator.reset_catalog_cache()

    def test_reads_per_model_effort_and_fast(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata = _fake_catalog_db(tmp, [
                _entry("grok-4.6", [GROK_EFFORT, GROK_FAST]),
                _entry("composer-2.5", [COMPOSER_FAST], default_on=False),
                _entry("gpt-5.6-sol", [SOL_REASONING, GROK_FAST]),
                _entry("claude-fable-5", [FABLE_EFFORT], default_on=False),
                _entry("auto-smart", [], default_on=True),
            ])
            data = session_locator.read_cursor_model_catalog(appdata=appdata)
        self.assertEqual("cursor", data["source"])
        by = {m["model"]: m for m in data["models"]}
        self.assertEqual("effort", by["grok-4.6"]["think_id"])
        self.assertEqual(
            ["low", "medium", "high", "xhigh"],
            [v["id"] for v in by["grok-4.6"]["think_values"]])
        self.assertTrue(by["grok-4.6"]["has_fast"])
        self.assertEqual("", by["composer-2.5"]["think_id"])
        self.assertTrue(by["composer-2.5"]["has_fast"])
        self.assertEqual("reasoning", by["gpt-5.6-sol"]["think_id"])
        self.assertFalse(by["claude-fable-5"]["has_fast"])
        self.assertTrue(any(m["model"] == "auto-smart" for m in data["models"]))

    def test_missing_itemtable_is_empty_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            gs = Path(tmp) / "Cursor" / "User" / "globalStorage"
            gs.mkdir(parents=True)
            sqlite3.connect(str(gs / "state.vscdb")).close()
            data = session_locator.read_cursor_model_catalog(appdata=tmp)
        self.assertEqual("empty", data["source"])
        self.assertEqual([], data["models"])

    def test_iter_skips_auto_smart(self):
        with tempfile.TemporaryDirectory() as tmp:
            appdata = _fake_catalog_db(tmp, [
                _entry("auto-smart", []),
                _entry("grok-4.6", [GROK_EFFORT, GROK_FAST]),
            ])
            specs = session_locator.iter_catalog_open_specs(appdata=appdata)
        names = [s["model"] for s in specs]
        self.assertEqual(["grok-4.6"], names)
        self.assertEqual("grok-4.6", specs[0]["label"])

    def test_pinned_chips_drop_the_long_tail(self):
        names = [s["model"] for s in session_locator.pinned_open_specs([
            {"model": "gpt-5.6-sol"}, {"model": "composer-2.5"},
            {"model": "grok-4.6"}, {"model": "default"},
            {"model": "claude-fable-5"},
        ])]
        self.assertEqual(["default", "grok-4.6", "gpt-5.6-sol"], names)


class SchemaMergeTests(unittest.TestCase):
    def test_drops_fast_when_model_has_none(self):
        schema = {"think_id": "effort",
                  "think_values": [{"id": "low"}, {"id": "max"}],
                  "has_fast": False}
        params = session_locator.merge_thinking_params(
            [{"id": "thinking", "value": "true"}],
            effort="max", fast=True, schema=schema)
        self.assertEqual({"thinking": "true", "effort": "max"},
                         {p["id"]: p["value"] for p in params})

    def test_maps_effort_onto_reasoning(self):
        schema = {"think_id": "reasoning",
                  "think_values": [{"id": "xhigh"}, {"id": "max"}],
                  "has_fast": True}
        params = session_locator.merge_thinking_params(
            [], effort="xhigh", fast=False, schema=schema)
        self.assertEqual({"reasoning": "xhigh", "fast": "false"},
                         {p["id"]: p["value"] for p in params})

    def test_rejects_effort_value_the_model_does_not_have(self):
        schema = {"think_id": "effort",
                  "think_values": [{"id": "low"}, {"id": "high"}],
                  "has_fast": True}
        params = session_locator.merge_thinking_params(
            [], effort="xhigh", fast=True, schema=schema)
        self.assertEqual({"fast": "true"}, {p["id"]: p["value"] for p in params})

    def test_no_schema_keeps_the_old_global_write(self):
        params = session_locator.merge_thinking_params(
            [], effort="xhigh", fast=True)
        self.assertEqual({"effort": "xhigh", "fast": "true"},
                         {p["id"]: p["value"] for p in params})


class ListKnownModelsCatalogTests(unittest.TestCase):
    def setUp(self):
        session_locator.reset_catalog_cache()
        self.sessions = {}
        self.patches = [
            patch.object(hub.HUB, "sessions", self.sessions),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        session_locator.reset_catalog_cache()

    def test_list_uses_cursor_catalog_not_the_global_effort_row(self):
        catalog = [
            session_locator._schema_from_entry(
                _entry("grok-4.6", [GROK_EFFORT, GROK_FAST])),
            session_locator._schema_from_entry(
                _entry("composer-2.5", [COMPOSER_FAST], default_on=False)),
        ]
        fake = {"ok": True, "source": "cursor", "models": catalog, "prefs": {}}
        with patch.object(hub, "read_cursor_model_catalog", return_value=fake), \
             patch.object(session_locator, "read_cursor_model_catalog",
                          return_value=fake):
            session_locator.reset_catalog_cache()
            r = hub.Api().list_known_models()
        self.assertEqual("cursor", r["source"])
        by = {m["model"]: m for m in r["models"]}
        self.assertEqual(["default", "grok-4.6"],
                         [m["model"] for m in r["models"]])
        self.assertEqual("effort", by["grok-4.6"]["schema"]["think_id"])
        self.assertFalse(any(v["id"] == "max"
                             for v in by["grok-4.6"]["schema"]["think_values"]))
        self.assertNotIn("composer-2.5", by)
        self.assertNotIn("composer-1", by)


if __name__ == "__main__":
    unittest.main()
