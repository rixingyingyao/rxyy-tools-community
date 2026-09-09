# -*- coding: utf-8 -*-
"""死因牌跟对话走：换绑/被收走之后不许再挂前任对话的死亡。

08-25 全面体检查出，与 9efb6e6（模型牌换绑当拍换牌）同根同源的另一半。
`tick_death_probe` 和 `tick_model_probe` 是同一条只读通道上的一对，模型牌那半
已经收好了，死因这半还照旧：

* `cursor_uuid` 换绑（接手重定位 / 身份自校准 / 认领被收走）时 `death_info`
  与 10 秒限频戳原样留着，接手方一上来就顶着前任对话的死亡告警；
* uuid 被收走（`cursor_uuid=None`，hub.py 那两处身份仲裁真会这么干）时更狠——
  `if not uid: return` 让它连重查的机会都没有，旧死因**永远**钉在那个 tab 上；
* 24h 响铃冷却只按死因代码记，换绑后新 agent 真的欠费挂了，却因为前任 24h 内
  挂过同一种死法而不响铃、不推手机——「不吵人」办成了「漏报」。

契约：换绑当拍清死因、清限频；没有对话就没有死因；冷却按「对话+死因」计。
另一头也锁死：hub 重启不算换绑，挂了一天的会话不能每次重启都再响一遍。
"""
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub  # noqa: E402
import session_core  # noqa: E402

QUOTA = {"bubble_id": "b-quota", "reason": "额度用尽", "code": "quota",
         "at_ts": 1, "is_last": True}


class _Host:
    """tick_death_probe 要的那点宿主行为，别的一律不提供。"""

    def __init__(self, every=10.0):
        self.DEATH_PROBE_EVERY = every
        self.alerted = []
        self.rescued = []

    def _fusion_recent_activity(self, s, now):
        return ""  # 四路信号都不新鲜 = 不让路，让判死走到底

    def _rescue_swallowed_reply(self, s, why="", note=""):
        self.rescued.append(why)

    def alert_session_death(self, s, dead):
        self.alerted.append(dead)


def _sess(uid="u-old"):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = "s1", "b4eff2ee", "rxyy tools·全面体检"
    s.cursor_uuid = uid
    s.death_probe_uid = uid   # 「死因就是从这个对话查的」，换绑测试再改 uuid
    s.death_probe_at = 0.0
    s.death_info = None
    s.death_alerts = {}
    s.uuid_verified = False
    s.agent_status_ts = 0
    s.processing_since = None
    s.last_zhi_ts = 0
    s.pending = None
    return s


class DeathChipFollowsTheDialogTests(unittest.TestCase):
    def setUp(self):
        self.patches = [
            patch.object(hub, "log_event", lambda *a, **k: None),
            patch.object(hub, "WORKFLOW", None),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])
        # 判死会去退卡；本文件只管死因牌，把看板那一路掐掉
        import board_hooks
        rel = patch.object(board_hooks, "release_session", lambda *a, **k: None)
        rel.start()
        self.addCleanup(rel.stop)

    def _dead(self, uid="u-old", **over):
        """一个刚被判过死的会话：死因新鲜、限频戳还压着整整一个周期。"""
        s = _sess(uid)
        s.death_info = dict(QUOTA, **over)
        s.death_probe_at = 100.0
        return s

    def test_rebind_drops_the_predecessors_death_this_tick(self):
        # 接手落地：新对话没报错，那块「额度用尽」当拍就该摘掉，不等限频周期
        s = self._dead()
        s.cursor_uuid = "u-new"
        reads = []

        def reader(uid):
            reads.append(uid)
            return None

        with patch.object(hub, "read_cursor_error", reader):
            session_core.tick_death_probe(_Host(), s, 101.0)
        self.assertEqual(["u-new"], reads, "换绑当拍就该改查新对话")
        self.assertIsNone(s.death_info)

    def test_dispossessed_tab_loses_the_death_badge(self):
        # 认领被收走（hub.py 身份仲裁真会把 cursor_uuid 置空）：没有对话就没有
        # 死因。修复前这里连查都不查就 return，旧死因永远钉在这个 tab 上
        s = self._dead()
        s.cursor_uuid = None
        with patch.object(hub, "read_cursor_error",
                          side_effect=AssertionError("没对话不该查")):
            session_core.tick_death_probe(_Host(), s, 101.0)
        self.assertIsNone(s.death_info)

    def test_rebind_rings_again_for_the_same_death_code(self):
        # 前任因欠费挂过、响过铃；换绑后的新 agent 又真的欠费挂了 —— 必须再响
        host = _Host(every=0)
        s = _sess()
        with patch.object(hub, "read_cursor_error", lambda uid: dict(QUOTA)):
            session_core.tick_death_probe(host, s, 100.0)
            self.assertEqual(1, len(host.alerted))
            s.cursor_uuid = "u-new"
            session_core.tick_death_probe(host, s, 101.0)
        self.assertEqual(2, len(host.alerted), "换了对话还压着前任的冷却 = 漏报")

    def test_same_dialog_still_only_rings_once_a_day(self):
        # 冷却本身不能被修没了：同一对话、同一死因，僵尸窗口每被戳一下就出一个
        # 新气泡，那不该变成一天响几十次（08-03 用户点名过）
        host = _Host(every=0)
        s = _sess()
        bubbles = iter(["b1", "b2", "b3"])
        with patch.object(hub, "read_cursor_error",
                          lambda uid: dict(QUOTA, bubble_id=next(bubbles))):
            for i in range(3):
                session_core.tick_death_probe(host, s, 100.0 + i)
        self.assertEqual(1, len(host.alerted))

    def test_same_dialog_keeps_the_throttle(self):
        # 没换绑就维持 10 秒一拍，别把限频修没了
        s = self._dead()
        with patch.object(hub, "read_cursor_error",
                          side_effect=AssertionError("限频期内不该查")):
            session_core.tick_death_probe(_Host(every=10.0), s, 105.0)
        self.assertEqual("额度用尽", s.death_info["reason"])

    def test_expired_cooldown_entries_do_not_pile_up(self):
        # 换绑口一多，冷却表的键会越攒越多；过了 24h 的条目留着也不影响判断
        host = _Host(every=0)
        s = _sess()
        s.death_alerts = {"u-ancient:quota": time.time() - 48 * 3600}
        with patch.object(hub, "read_cursor_error", lambda uid: dict(QUOTA)):
            session_core.tick_death_probe(host, s, 100.0)
        self.assertEqual(["u-old:quota"], sorted(s.death_alerts))


class RestartIsNotARebindTests(unittest.TestCase):
    """反方向的锁：hub 重启不是换绑。

    死因牌本来就不落快照（下一拍重查），但 24h 冷却要跨重启活着。快照恢复时
    若不把「上次查死因用的 uid」对齐成恢复出来的那个，重启后第一拍会被当成
    换绑清掉冷却——挂了一天的会话于是每次重启都再响一遍铃、再推一次手机。
    """

    def test_snapshot_aligns_the_probe_uid_with_the_restored_dialog(self):
        s = session_core.session_from_snapshot(
            hub.HUB, {"id": "s1", "name": "x", "cwd": str(MODULE_DIR),
                      "cursor_uuid": "u-old",
                      "death_alerts": {"u-old:quota": 123.0}})
        self.assertEqual("u-old", s.death_probe_uid)
        self.assertEqual({"u-old:quota": 123.0}, s.death_alerts)

    def test_a_dialogless_snapshot_stays_dialogless(self):
        s = session_core.session_from_snapshot(
            hub.HUB, {"id": "s1", "name": "x", "cwd": str(MODULE_DIR)})
        self.assertIsNone(s.death_probe_uid)


class RestartDoesNotReviveConfirmedOfflineTests(unittest.TestCase):
    """换装恢复的名单是历史广播，不能盖过快照里已经确认的离线/死亡。"""

    def _live_session(self):
        client = hub.Client(None, str(MODULE_DIR), 101)
        s = hub.Session(client, "confirmed-offline", "已结束任务")
        s.messages = []
        return s

    def _restore(self, **overrides):
        snapshot = {"id": "s-offline", "name": "已结束任务",
                    "cwd": str(MODULE_DIR), "conv_key": "confirmed-offline",
                    "messages": [], **overrides}
        with patch.object(hub.HUB, "_bind_registered_team_root"), \
             patch.object(hub, "heal_session_paths"), \
             patch.object(hub, "heal_named_agent_root"):
            return session_core.session_from_snapshot(hub.HUB, snapshot)

    def test_confirmed_death_is_persisted_as_blocked(self):
        s = self._live_session()
        s.death_info = dict(QUOTA)
        snapshot = session_core.session_to_snapshot(hub.HUB, s)
        self.assertTrue(snapshot["auto_reconnect_blocked"])

    def test_confirmed_offline_restore_has_no_reconnect_grace(self):
        restored = self._restore(auto_reconnect_blocked=True)
        self.assertTrue(restored.auto_reconnect_blocked)
        self.assertIsNone(restored.recon_deadline)
        self.assertIn("等待手动接续", restored.end_reason)

    def test_legacy_snapshot_migrates_only_stale_explicit_disconnect(self):
        restored = self._restore(
            last_heartbeat=time.time() - 16 * 60,
            messages=[{"role": "sys",
                       "html": "会话已断开。记录已保存: C:/logs/task.md"}])
        self.assertTrue(restored.auto_reconnect_blocked)
        self.assertIsNone(restored.recon_deadline)

    def test_legacy_stale_heartbeat_without_terminal_record_keeps_grace(self):
        restored = self._restore(last_heartbeat=time.time() - 16 * 60)
        self.assertFalse(restored.auto_reconnect_blocked)
        self.assertIsNotNone(restored.recon_deadline)

    def test_historical_death_alert_alone_does_not_block_legacy_snapshot(self):
        restored = self._restore(
            last_heartbeat=time.time() - 16 * 60,
            death_alerts={"old-thread:quota": time.time() - 60})
        self.assertFalse(restored.auto_reconnect_blocked)
        self.assertIsNotNone(restored.recon_deadline)

    def test_recovery_after_historical_disconnect_keeps_legacy_grace(self):
        restored = self._restore(
            last_heartbeat=time.time() - 16 * 60,
            messages=[
                {"role": "sys", "html": "会话已断开。记录已保存: C:/logs/task.md"},
                {"role": "sys", "html": "连接已恢复（MCP 重连）"},
            ])
        self.assertFalse(restored.auto_reconnect_blocked)
        self.assertIsNotNone(restored.recon_deadline)

    def test_restored_registry_heartbeat_cannot_revive_but_active_report_can(self):
        restored = self._restore(auto_reconnect_blocked=True)
        client = hub.Client(None, str(MODULE_DIR), 202)
        with patch.object(hub.HUB, "sessions", {restored.id: restored}), \
             patch.object(hub.HUB, "order", [restored.id]), \
             patch.object(hub.HUB, "_bind_registered_team_root"), \
             patch.object(hub.HUB, "_ensure_listed"), \
             patch.object(hub, "heal_session_paths"):
            # server.restore_conv_registry 的下一拍就是这条被动广播。
            hub.HUB._handle_client_msg(client, None, {
                "type": "mcp_heartbeat", "conversations": [restored.conv_key]})
            self.assertFalse(restored.connected)
            self.assertNotIn(restored.conv_key, client.sessions)

            # 真 agent 主动 zt/ji 是新的存活证据，允许手动接回。
            revived = hub.HUB._resolve_for_signal(
                client, restored.conv_key, revive_handed=True)
        self.assertIs(restored, revived)
        self.assertTrue(restored.connected)
        self.assertFalse(restored.auto_reconnect_blocked)

    def test_bound_codex_desktop_connection_prevents_offline_snapshot_block(self):
        thread_id = "11111111-1111-4111-8111-111111111111"
        s = self._live_session()
        s.runtime_kind = "codex"
        s.native_thread_id = thread_id
        s.native_turn_view_id = thread_id
        s.native_turn_view = {"desktop_connected": True}
        s.connected = False
        s.recon_deadline = None
        s.ide_active_cache = False
        snapshot = session_core.session_to_snapshot(hub.HUB, s)
        self.assertFalse(snapshot["auto_reconnect_blocked"])

    def test_direct_codex_desktop_connection_prevents_offline_snapshot_block(self):
        s = self._live_session()
        s.runtime_kind = "codex"
        s.native_thread_id = "11111111-1111-4111-8111-111111111111"
        s.native_desktop_connected = True
        s.connected = False
        s.recon_deadline = None
        s.ide_active_cache = False
        snapshot = session_core.session_to_snapshot(hub.HUB, s)
        self.assertFalse(snapshot["auto_reconnect_blocked"])

    def test_other_thread_native_cache_cannot_prevent_offline_block(self):
        s = self._live_session()
        s.runtime_kind = "codex"
        s.native_thread_id = "11111111-1111-4111-8111-111111111111"
        s.native_turn_view_id = "22222222-2222-4222-8222-222222222222"
        s.native_turn_view = {"desktop_connected": True}
        s.connected = False
        s.recon_deadline = None
        s.ide_active_cache = False
        snapshot = session_core.session_to_snapshot(hub.HUB, s)
        self.assertTrue(snapshot["auto_reconnect_blocked"])


if __name__ == "__main__":
    unittest.main()
