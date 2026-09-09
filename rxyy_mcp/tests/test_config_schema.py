# -*- coding: utf-8 -*-
"""配置/凭证分离层（config_schema）的契约测试。

锁两类东西：
1. 一致性——SCHEMA 声明的每个 key / 默认值都与 hub.DEFAULTS 对齐，防「说明书」漂移。
2. 行为——env 覆盖只在设了变量时生效且类型正确、不设时逐字节不变；脱敏不外泄凭证
   也不改原 dict；example 敏感项留空；required 检测准确。
"""
import sys
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import config_schema as cs  # noqa: E402
import hub  # noqa: E402


class SchemaConsistencyTests(unittest.TestCase):
    def test_every_schema_key_exists_in_hub_defaults(self):
        for f in cs.SCHEMA:
            self.assertIn(f.key, hub.DEFAULTS, "SCHEMA 键 {} 不在 hub.DEFAULTS".format(f.key))

    def test_schema_default_matches_hub_default(self):
        for f in cs.SCHEMA:
            self.assertEqual(f.default, hub.DEFAULTS[f.key],
                             "SCHEMA 默认值与 hub.DEFAULTS 漂移：{}".format(f.key))

    def test_secret_and_env_maps_are_derived_from_schema(self):
        self.assertEqual(cs.SECRET_KEYS, frozenset(f.key for f in cs.SCHEMA if f.secret))
        self.assertEqual(cs.ENV_MAP, {f.env: f.key for f in cs.SCHEMA if f.env})

    def test_known_secrets_are_flagged(self):
        for key in ("share_token", "cursor_api_key", "bark_url"):
            self.assertIn(key, cs.SECRET_KEYS, "{} 应标为凭证".format(key))


class EnvOverrideTests(unittest.TestCase):
    def test_no_env_leaves_cfg_byte_identical(self):
        cfg = {"remote_base_url": "http://a", "mcp_http_port": 39222, "push_enabled": False}
        before = dict(cfg)
        cs.apply_env_overrides(cfg, environ={})
        self.assertEqual(before, cfg)

    def test_env_overrides_string(self):
        cfg = {"remote_base_url": "http://old"}
        cs.apply_env_overrides(cfg, environ={"CHIJIU_REMOTE_BASE_URL": "http://new:39080"})
        self.assertEqual("http://new:39080", cfg["remote_base_url"])

    def test_env_overrides_int_port(self):
        cfg = {"mcp_http_port": 39222}
        cs.apply_env_overrides(cfg, environ={"CHIJIU_MCP_HTTP_PORT": "40000"})
        self.assertEqual(40000, cfg["mcp_http_port"])
        self.assertIsInstance(cfg["mcp_http_port"], int)

    def test_env_bad_int_keeps_current_value_not_schema_default(self):
        # 4b0a9d81 互审建议：env 是叠加语义，非法值不该把 config.json 的用户现值
        # 顶成 SCHEMA 默认——当它没设，现值原样保留。
        cfg = {"mcp_http_port": 40001}  # 用户在 config.json 里改过的现值
        cs.apply_env_overrides(cfg, environ={"CHIJIU_MCP_HTTP_PORT": "not-a-number"})
        self.assertEqual(40001, cfg["mcp_http_port"])

    def test_env_overrides_bool(self):
        cfg = {"push_enabled": False}
        cs.apply_env_overrides(cfg, environ={"CHIJIU_PUSH_ENABLED": "true"})
        self.assertIs(True, cfg["push_enabled"])
        cfg2 = {"push_enabled": True}
        cs.apply_env_overrides(cfg2, environ={"CHIJIU_PUSH_ENABLED": "0"})
        self.assertIs(False, cfg2["push_enabled"])

    def test_empty_env_value_explicitly_clears(self):
        # 空串也算「设了」——允许用 env 显式清空一个配置
        cfg = {"bark_url": "https://api.day.app/xxxx"}
        cs.apply_env_overrides(cfg, environ={"CHIJIU_BARK_URL": ""})
        self.assertEqual("", cfg["bark_url"])


class RedactTests(unittest.TestCase):
    def test_secrets_masked_nonsecrets_intact(self):
        cfg = {"bark_url": "https://api.day.app/secrettoken", "remote_base_url": "http://host",
               "share_token": "abcdef123456", "push_enabled": True}
        out = cs.redact(cfg)
        self.assertNotIn("secrettoken", out["bark_url"])
        self.assertNotIn("123456", out["share_token"])
        self.assertEqual("http://host", out["remote_base_url"])
        self.assertIs(True, out["push_enabled"])

    def test_redact_does_not_mutate_input(self):
        cfg = {"bark_url": "https://api.day.app/tok"}
        cs.redact(cfg)
        self.assertEqual("https://api.day.app/tok", cfg["bark_url"])

    def test_empty_secret_stays_empty(self):
        self.assertEqual("", cs.redact({"bark_url": ""})["bark_url"])


class ExampleAndRequiredTests(unittest.TestCase):
    def test_example_leaves_secrets_empty(self):
        ex = cs.example_config()
        for key in cs.SECRET_KEYS:
            self.assertEqual("", ex[key], "example 里凭证 {} 必须留空".format(key))

    def test_example_covers_all_schema_keys(self):
        ex = cs.example_config()
        for f in cs.SCHEMA:
            self.assertIn(f.key, ex)

    def test_render_example_json_is_valid_json(self):
        import json
        doc = json.loads(cs.render_example_json())
        self.assertIn("remote_base_url", doc)
        self.assertIn("_comment_remote_base_url", doc)

    def test_missing_required_detects_empty_remote_base_url(self):
        miss = cs.missing_required({"remote_base_url": ""})
        self.assertIn("remote_base_url", [f.key for f in miss])

    def test_missing_required_satisfied_when_filled(self):
        miss = cs.missing_required({"remote_base_url": "http://host:39080"})
        self.assertEqual([], [f.key for f in miss if f.key == "remote_base_url"])


if __name__ == "__main__":
    unittest.main()
