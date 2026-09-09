# -*- coding: utf-8 -*-
"""手机推送目标判别：自建 ntfy 换任意域名都不能被误当 Bark（07-31 404 事故回归）"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub


class PushTargetKindTests(unittest.TestCase):
    def test_ntfy_sh_full_url(self):
        self.assertFalse(hub.Hub._push_target_is_bark("https://ntfy.sh/rxyy-topic"))

    def test_selfhosted_ntfy_domain_without_ntfy_in_name(self):
        # 07-31 事故现场：迁自建后域名不含 ntfy 字样，被误当 Bark 全 404
        self.assertFalse(hub.Hub._push_target_is_bark(
            "https://api-audioeditor.on-radio.cn:2443/rxyy-d376a49ccda0"))

    def test_selfhosted_ntfy_quietfern(self):
        self.assertFalse(hub.Hub._push_target_is_bark(
            "https://ntfy.example.invalid/rxyy-d376a49ccda0"))

    def test_bark_official_url(self):
        self.assertTrue(hub.Hub._push_target_is_bark("https://api.day.app/AbCdEfKey"))

    def test_bare_bark_key(self):
        self.assertTrue(hub.Hub._push_target_is_bark("AbCdEfGhBarkKey"))

    def test_bare_ntfy_topic_with_ntfy_in_name(self):
        # 旧行为保留：裸串含 ntfy 字样按 ntfy topic 处理
        self.assertFalse(hub.Hub._push_target_is_bark("my-ntfy-topic"))


class SplitPushTargetsTests(unittest.TestCase):
    """双通道拆分（08-01）：iOS 弹窗走 Bark、历史+按钮走 ntfy，分号隔开都发"""

    def test_single_target_unchanged(self):
        self.assertEqual(["https://ntfy.example.invalid/rxyy-x"],
                         hub.Hub._split_push_targets("https://ntfy.example.invalid/rxyy-x"))

    def test_semicolon_dual_channel(self):
        raw = "https://ntfy.example.invalid/rxyy-x; https://api.day.app/AbCdEf"
        self.assertEqual(["https://ntfy.example.invalid/rxyy-x",
                          "https://api.day.app/AbCdEf"],
                         hub.Hub._split_push_targets(raw))

    def test_chinese_semicolon_and_newline(self):
        raw = "https://ntfy.example.invalid/rxyy-x；\nAbCdEfBarkKey\n"
        self.assertEqual(["https://ntfy.example.invalid/rxyy-x", "AbCdEfBarkKey"],
                         hub.Hub._split_push_targets(raw))

    def test_dedup_and_blank(self):
        raw = " ; https://api.day.app/K;; https://api.day.app/K ;\n"
        self.assertEqual(["https://api.day.app/K"], hub.Hub._split_push_targets(raw))

    def test_empty_means_disabled(self):
        self.assertEqual([], hub.Hub._split_push_targets(""))
        self.assertEqual([], hub.Hub._split_push_targets(None))


class OutboundPushGuardTests(unittest.TestCase):
    """08-14：单测 daemon 线程在 patch.stopall 后把 sid=t1 打进真 ntfy。"""

    def test_unittest_process_is_blocked(self):
        self.assertTrue(hub._outbound_push_blocked())

    def test_live_hub_argv_is_not_blocked(self):
        with patch.object(hub.os, "environ", {}):
            with patch.object(hub.sys, "argv", [r"C:\live\hub.py", "--daemon"]):
                self.assertFalse(hub._outbound_push_blocked())

    def test_push_phone_does_not_open_network_when_blocked(self):
        with patch.object(hub.HUB, "cfg", {
            "push_enabled": True,
            "bark_url": "https://ntfy.sh/should-not-be-hit",
        }):
            with patch("urllib.request.urlopen") as urlopen:
                hub.HUB.push_phone("sid=t1 leak", "有新提问待你回复")
                urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
