# -*- coding: utf-8 -*-
"""心跳名单跨 MCP 进程存活。

心跳是 hub 判「这个 agent 还活着」的实时信号：收到就把 tab 复活成在线
（hub.Hub._resolve_for_signal 里 connected=True）。而名单此前只在 MCP 进程内存里，
换装 / 热拷 / 看门狗救活一换掉进程，名单跟着没了——那些 agent 明明还在 Cursor 里
跑着，控制台却在重连宽限到期后一律翻成「已终止」。08-07 12:16 实测：19 个 tab 有
15 个被这么误判，用户在 Cursor 侧看得见它们全在 Generating。

反方向的失真同样要防住：早已收工的对话不该被一次重启凭空复活成「活着」。所以捞
回来的口径与上报那份严格一致——只认窗口内还活跃的。
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import server  # noqa: E402


class ConvRegistryTests(unittest.TestCase):
    """落盘 → 换一条命 → 捞回来。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(server, "DATA_DIR", self.dir)
        p.start()
        self.addCleanup(p.stop)
        # 窗口固定成 30 分钟，免得跟着机器上的 config.json 飘
        w = patch.object(server.HubBridge, "_conv_window", lambda self: 1800)
        w.start()
        self.addCleanup(w.stop)

    def _file(self):
        return self.dir / server.HubBridge.CONV_REGISTRY_FILE

    def _bridge_with(self, **convs):
        b = server.HubBridge()
        for c, ts in convs.items():
            b._active_convs.add(c)
            b._conv_activity[c] = ts
        return b

    def test_a_restarted_mcp_picks_the_live_conversations_back_up(self):
        # 这条就是 12:16 那次事故：进程一换，15 个还在干活的 tab 没人替它们报活
        now = time.time()
        self._bridge_with(aaaa1111=now - 10, bbbb2222=now - 60)._save_conv_registry()

        reborn = server.HubBridge()
        self.assertEqual({"aaaa1111", "bbbb2222"}, set(reborn.restore_conv_registry()))
        self.assertEqual({"aaaa1111", "bbbb2222"}, reborn._active_convs)
        # 捞回来就得真出现在下一拍的上报里，否则等于没捞
        self.assertEqual({"aaaa1111", "bbbb2222"},
                         set(reborn._convs_to_report(1800)))

    def test_a_conversation_that_finished_long_ago_stays_dead(self):
        # 反方向的失真：拿一次重启把早收工的对话复活成「活着」，比误判死更难发现
        now = time.time()
        self._file().write_text(json.dumps({"old99999": now - 7200}),
                                encoding="utf-8")
        reborn = server.HubBridge()
        self.assertEqual([], reborn.restore_conv_registry())
        self.assertEqual(set(), reborn._active_convs)

    def test_saving_does_not_wipe_another_mcp_process_entries(self):
        # 一台机器上不止一个 MCP 进程时，覆盖式写会把对方的对话抹掉，
        # 那些 tab 就又没人替它们报活了
        now = time.time()
        self._file().write_text(json.dumps({"other111": now - 30}),
                                encoding="utf-8")
        self._bridge_with(mine1111=now)._save_conv_registry()
        on_disk = json.loads(self._file().read_text(encoding="utf-8"))
        self.assertEqual({"other111", "mine1111"}, set(on_disk))

    def test_the_newer_timestamp_wins_on_merge(self):
        now = time.time()
        self._file().write_text(json.dumps({"aaaa1111": now - 600}),
                                encoding="utf-8")
        self._bridge_with(aaaa1111=now)._save_conv_registry()
        on_disk = json.loads(self._file().read_text(encoding="utf-8"))
        self.assertAlmostEqual(now, on_disk["aaaa1111"], places=3)

    def test_stale_entries_are_swept_on_save(self):
        # 不清的话，这份名单会随着守护进程常驻数周而无限长
        now = time.time()
        self._file().write_text(
            json.dumps({"old99999": now - 7200, "keep1111": now - 5}),
            encoding="utf-8")
        self._bridge_with()._save_conv_registry()
        self.assertEqual({"keep1111"},
                         set(json.loads(self._file().read_text(encoding="utf-8"))))

    def test_a_missing_or_broken_file_is_not_an_error(self):
        # 名单是锦上添花：读不出来只当没有，绝不能把 MCP 守护进程带崩
        self.assertEqual([], server.HubBridge().restore_conv_registry())
        self._file().write_text("{这不是 json", encoding="utf-8")
        self.assertEqual([], server.HubBridge().restore_conv_registry())
        self._file().write_text("[1, 2, 3]", encoding="utf-8")
        self.assertEqual([], server.HubBridge().restore_conv_registry())

    def test_an_unwritable_data_dir_never_breaks_the_agent(self):
        with patch.object(server, "DATA_DIR", self.dir / "根本不存在"):
            self._bridge_with(aaaa1111=time.time())._save_conv_registry()

    def test_restoring_never_backdates_a_fresh_call(self):
        # 捞回来的是旧时刻；如果本进程已经有更新的活动，别被盘上的旧值盖回去
        now = time.time()
        self._file().write_text(json.dumps({"aaaa1111": now - 900}),
                                encoding="utf-8")
        b = self._bridge_with(aaaa1111=now)
        b.restore_conv_registry()
        self.assertAlmostEqual(now, b._conv_activity["aaaa1111"], places=3)


class ReportFilterTests(unittest.TestCase):
    """上报口径：谁该被报活、谁不该。"""

    def setUp(self):
        self.b = server.HubBridge()

    def test_a_recently_active_conversation_is_reported(self):
        self.b._active_convs.add("aaaa1111")
        self.b._conv_activity["aaaa1111"] = time.time() - 30
        self.assertEqual(["aaaa1111"], self.b._convs_to_report(1800))

    def test_a_long_idle_conversation_drops_out(self):
        self.b._active_convs.add("aaaa1111")
        self.b._conv_activity["aaaa1111"] = time.time() - 3600
        self.assertEqual([], self.b._convs_to_report(1800))

    def test_a_zhi_still_in_flight_is_reported_however_long_it_waits(self):
        # 等用户回话可以等一整天，那期间它恰恰是最不能被判死的
        self.b._active_convs.add("aaaa1111")
        self.b._conv_activity["aaaa1111"] = time.time() - 99999
        self.b.waiters["r1"] = {"conversation_id": "aaaa1111"}
        self.assertEqual(["aaaa1111"], self.b._convs_to_report(1800))


if __name__ == "__main__":
    unittest.main(verbosity=2)
