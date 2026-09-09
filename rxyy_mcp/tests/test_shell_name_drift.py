# -*- coding: utf-8 -*-
"""壳名被改掉之后，接手落地照样能把待命空壳收起来。

08-07 实测事故：用户报「接手之后，没有把待命壳会话关掉」。根因不在收壳逻辑，
在认壳——sync_cursor_titles 每隔一阵把【Cursor 自动生成的聊天标题】同步成 tab 名，
而新对话里只有报到提示词，Cursor 起的标题千奇百怪（当天两例：
「Persistent task reporting」「Cursor extension installation」），
_title_is_boilerplate 那张关键词表堵不全。名字一旦不以「待命」打头，
_is_takeover_shell / _is_checkin_shellish 就永远认不出它是壳：
  · 接手落地收不掉这个空壳 tab；
  · 更坏的是它的 uuid 认领变成排他的，被接手的原 tab 反而校准不回身份。

修复契约（本测试锁死）：
1. 认壳只认【出生名】(Session.shell_born)，当前叫什么都不影响；
2. 出生名标记随快照持久化，重启不丢；老快照没有该字段时回落到当前名；
3. 还在待命、没干过活的壳，压根不许被 Cursor 标题改名（从源头掐掉漂移）；
4. 干过真活的 tab 照旧同步标题，且旧名进 name_history（队友按旧名转告还找得到）。
"""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub

WS = r"d:\Desktop\cursor工作流"


def _s(sid, conv, name, msg_seq=1, uuid=None, born=None, pending=None, queued=None):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = sid, conv, name
    s.name_locked = False
    s.shell_born = (hub.CHECKIN_SHELL_NAME_RE.match(name) is not None
                    if born is None else born)
    s.cwd = s.task_root = WS
    s.peer_ip = None
    s.pid = 1
    s.msg_seq = msg_seq
    s.messages = []
    s.queued = queued or []
    s.pending = pending
    s.pending_lost = False
    s.cursor_uuid = uuid
    s.uuid_verified = bool(uuid)
    s.cursor_title = None
    s.transcript_path = None
    s.created_at = "2026-08-07 11:41:36"
    s.created_ts = time.time()
    s.file_path = None
    s.connected = True
    s.client = None
    s.rev = 0
    s.lock = threading.Lock()
    return s


class ShellBornMarkTests(unittest.TestCase):
    """出生名标记：认壳不再看当前叫什么。"""

    def test_renamed_shell_is_still_recognised(self):
        # 当天实况：待命·smart_cloud_broadcasting_live_api7 被 Cursor 标题
        # 改成了「Persistent task reporting1」，但它一句真话都没说过
        x = _s("s1", "912382e6", "Persistent task reporting1",
               msg_seq=2, born=True)
        self.assertTrue(hub.HUB._is_checkin_shellish(x))
        self.assertTrue(hub.HUB._is_takeover_shell(x))

    def test_plain_standby_name_still_works_without_the_mark(self):
        # 存量会话（修复前建的，内存里没有 shell_born）：按当前名字兜底
        x = _s("s1", "1b4877bf", "待命·cursor工作流1", msg_seq=2, born=False)
        self.assertTrue(hub.HUB._is_checkin_shellish(x))

    def test_a_real_tab_is_never_mistaken_for_a_shell(self):
        x = _s("s1", "b4eff2ee", "换装收尾", msg_seq=22)
        self.assertFalse(hub.HUB._is_checkin_shellish(x))
        self.assertFalse(hub.HUB._is_takeover_shell(x))

    def test_shell_that_got_real_work_is_no_longer_reapable(self):
        # 出生名是壳，但用户后来在这个 tab 里派了活 → 有历史，不许再当空壳收
        x = _s("s1", "266bdbe4", "先测试再改", msg_seq=9, born=True)
        self.assertFalse(hub.HUB._is_checkin_shellish(x))
        self.assertFalse(hub.HUB._is_takeover_shell(x))

    def test_pending_or_queued_shell_is_not_reapable(self):
        # 收壳的安全边界一条没松：收错就把未答提问/排队消息一起丢了
        p = _s("s1", "c1", "改过名的壳", msg_seq=2, born=True,
               pending={"id": "q"})
        q = _s("s2", "c2", "改过名的壳2", msg_seq=2, born=True,
               queued=[{"message": "另派的活"}])
        for x in (p, q):
            self.assertTrue(hub.HUB._is_checkin_shellish(x))
            self.assertFalse(hub.HUB._is_takeover_shell(x))

    def test_birth_name_marks_the_shell_at_construction(self):
        client = type("C", (), {"cwd": WS, "pid": 1, "peer_ip": None})()
        self.assertTrue(hub.Session(client, "c1", "待命·cursor工作流").shell_born)
        self.assertTrue(hub.Session(client, "c2", "Persistent Plus check-in").shell_born)
        self.assertFalse(hub.Session(client, "c3", "换装收尾").shell_born)


class ReapRenamedShellTests(unittest.TestCase):
    """接手落地：壳改过名也要收掉（本次事故的直接复现）。"""

    def setUp(self):
        # B：被接手的原会话，有真实历史，uuid 已校准到接手者当前对话
        self.B = _s("tab-B", "d66f1bf0", "Persistent task reporting", 18,
                    uuid="U-dialog")
        # A：同一个 Cursor 对话里的报到壳，名字已被 Cursor 标题冲掉
        self.A = _s("tab-A", "912382e6", "Persistent task reporting1", 2,
                    born=True)
        self._patches = [
            patch.object(hub.HUB, "sessions", {"tab-B": self.B, "tab-A": self.A}),
            patch.object(hub.HUB, "order", ["tab-B", "tab-A"]),
            patch.object(hub.HUB, "_relocate_takeover_uuid", MagicMock()),
            patch.object(hub.HUB, "_tombstone_shell", MagicMock()),
            patch.object(hub.HUB, "log_end", MagicMock()),
            patch.object(hub, "log_event", MagicMock()),
            # 账本隔离：收壳的 _retire_conv_into 写台账，不隔离会把测试数据
            # 灌进真 HUB 的账并污染同进程后跑的用例（台账兜底指路）
            patch.object(hub.HUB, "takeover_aliases", {}),
            patch.object(hub.HUB, "takeover_ledger", {}, create=True),
            patch.object(hub.Hub, "_save_takeover_aliases", lambda self: None),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])

    def test_renamed_shell_in_the_takeover_dialog_is_reaped(self):
        with patch.object(hub, "composer_mentions_conv", return_value=True):
            hub.HUB._reap_takeover_shell(self.B)
        self.assertNotIn("tab-A", hub.HUB.sessions)
        self.assertIn("tab-B", hub.HUB.sessions)

    def test_renamed_shell_of_another_dialog_is_left_alone(self):
        with patch.object(hub, "composer_mentions_conv", return_value=False):
            hub.HUB._reap_takeover_shell(self.B)
        self.assertIn("tab-A", hub.HUB.sessions)


class ShellBornPersistenceTests(unittest.TestCase):
    """出生名标记必须跨重启：不落盘的话，重启后改过名的壳又认不回来了。"""

    def _roundtrip(self, session):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "snap.json"
            with (patch.object(hub.Hub, "STATE_PATH", tmp),
                  patch.object(hub.HUB, "sessions", {session.id: session}),
                  patch.object(hub.HUB, "order", [session.id])):
                hub.HUB.save_state()
            raw = json.loads(tmp.read_text(encoding="utf-8"))
            restored, order = {}, []
            with (patch.object(hub.Hub, "STATE_PATH", tmp),
                  patch.object(hub.HUB, "sessions", restored),
                  patch.object(hub.HUB, "order", order)):
                hub.HUB.load_state()
            return raw, restored[order[0]]

    def test_mark_survives_a_restart(self):
        raw, r = self._roundtrip(
            _s("s1", "912382e6", "Persistent task reporting1", 2, born=True))
        self.assertTrue(raw[0]["shell_born"])
        self.assertTrue(r.shell_born)
        self.assertTrue(hub.HUB._is_checkin_shellish(r))

    def test_old_snapshot_without_the_field_falls_back_to_the_name(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / "snap.json"
            tmp.write_text(json.dumps([{
                "id": "s1", "conv_key": "1b4877bf", "name": "待命·cursor工作流1",
                "cwd": WS, "created_ts": time.time(), "messages": [],
            }], ensure_ascii=False), encoding="utf-8")
            restored, order = {}, []
            with (patch.object(hub.Hub, "STATE_PATH", tmp),
                  patch.object(hub.HUB, "sessions", restored),
                  patch.object(hub.HUB, "order", order)):
                hub.HUB.load_state()
        self.assertTrue(restored[order[0]].shell_born)

    def test_a_real_tab_is_not_marked_by_the_fallback(self):
        raw, r = self._roundtrip(_s("s1", "b4eff2ee", "换装收尾", 22))
        self.assertFalse(raw[0]["shell_born"])
        self.assertFalse(r.shell_born)


class CursorTitleSyncTests(unittest.TestCase):
    """从源头掐掉漂移：待命壳不许被 Cursor 自动标题改名。"""

    def _sync(self, sessions, title):
        d = {x.id: x for x in sessions}
        with (patch.object(hub.HUB, "sessions", d),
              patch.object(hub.HUB, "order", [x.id for x in sessions]),
              patch.object(hub.HUB, "_relocate_if_stale", MagicMock()),
              patch.object(hub, "read_cursor_title", lambda uid: title),
              patch.object(hub, "log_event", MagicMock())):
            hub.HUB.sync_cursor_titles()

    def test_standby_shell_keeps_its_name(self):
        shell = _s("s1", "912382e6", "待命·smart_cloud_broadcasting_live_api7",
                   msg_seq=2, uuid="U-shell")
        self._sync([shell], "Persistent task reporting")
        self.assertEqual("待命·smart_cloud_broadcasting_live_api7", shell.name)
        self.assertIsNone(shell.cursor_title)

    def test_already_renamed_shell_is_not_renamed_again(self):
        shell = _s("s1", "912382e6", "Persistent task reporting1",
                   msg_seq=2, uuid="U-shell", born=True)
        self._sync([shell], "Something else entirely")
        self.assertEqual("Persistent task reporting1", shell.name)

    def test_a_working_tab_still_syncs_and_keeps_its_old_name_on_record(self):
        s = _s("s1", "b4eff2ee", "换装收尾", msg_seq=22, uuid="U-real")
        self._sync([s], "Swap finalisation")
        self.assertEqual("Swap finalisation", s.name)
        self.assertEqual("Swap finalisation", s.cursor_title)
        self.assertIn("换装收尾", getattr(s, "name_history", []))


class PushNameOutToCursorTests(unittest.TestCase):
    """agent 自报过真名的 tab，名字要一直写回 Cursor 那边，别只写一次就算完。

    08-25 现场清点：控制台上 42 个已自报真名、且已绑上 cursor_uuid 的会话，
    Cursor 库里的标题只有 1 个跟 tab 名对得上，其余 41 个仍是
    「Persistent plus zhi report」——用户看到的就是侧栏里一整排同名。

    根子是回写只挂在「改名那一刻」（maybe_apply_task_name 末尾）这一个边沿上，
    而 cursor_uuid 是**后来才绑定、而且会重绑的**：首次报真名时它常常还是空的；
    接手、换窗口、身份自校准都会把它挪到另一个 composer 上。名字此后不再变，
    那条边沿就永远不再触发，新 composer 于是一辈子顶着 Cursor 自动起的标题。

    库里能查到旁证：`rxyy tools·全面体检` 这个名字确实被写进过 composer
    c6c4d53a（13:48:45），可该会话现在绑的是另一个 composer，那边写着
    「Persistent plus zhi report」（18:30 还在更新）。写得进去、只是写晚了一步
    就再也不补——所以用户会看到「有几个改成功了，大部分没有」。

    改成每拍对齐：30 秒一趟的标题同步循环里，agent_named 的 tab 一律往外写。
    """

    def _sync(self, sessions, *, ok=True, ide_title="Swap finalisation"):
        calls = []

        def _write(uid, title, appdata=None):
            calls.append((uid, title))
            return ok

        d = {x.id: x for x in sessions}
        with (patch.object(hub.HUB, "sessions", d),
              patch.object(hub.HUB, "order", [x.id for x in sessions]),
              patch.object(hub.HUB, "_relocate_if_stale", MagicMock()),
              patch.object(hub, "read_cursor_title", lambda uid: ide_title),
              patch.object(hub, "write_cursor_title", _write),
              patch.object(hub, "log_event", MagicMock())):
            hub.HUB.sync_cursor_titles()
            hub.HUB.sync_cursor_titles()      # 第二趟：不该重复写同一个
        return calls

    def _named(self, uuid="U-a"):
        s = _s("s1", "b4eff2ee", "rxyy tools·全面体检", msg_seq=22, uuid=uuid)
        s.agent_named = True
        return s

    def test_a_named_tab_is_realigned_on_every_tick(self):
        """每一拍都得往外对齐，不能「写成功过一次就记账、此后再不写」。

        08-25 22:26 现场实验（往 Cursor 库里写完 90 秒后再读）：

            uid=d154b3a0 写之后='rxyy tools·全面体检' → 90 秒后='Persistent plus zhi report'  被盖回去了
            uid=e3ccc6c5 写之后='视频快编·ctest收尾'  → 90 秒后='Persistent plus zhi report'  被盖回去了
            uid=f2c959c2 写之后='心理后端·接口补齐'  → 90 秒后='心理后端·接口补齐'          还在

        前两个是当时正开在 Cursor 里的会话：Cursor 自己会把内存里那份
        composer 状态刷回磁盘，连名字一起盖。第三个当时没人动，就留住了。
        「写成功即记账」于是变成一次性的——被盖回去以后永远不补，用户看到的
        还是一排「Persistent plus zhi report」。去重下沉给 write_cursor_title
        自己做（它本来就先读后比，名字一样时不发 UPDATE）。
        """
        s = self._named()
        calls = self._sync([s])
        self.assertEqual([("U-a", "rxyy tools·全面体检"),
                          ("U-a", "rxyy tools·全面体检")], calls,
                         "被 Cursor 盖回去以后要补写，所以每拍都得核对一次")
        self.assertEqual("rxyy tools·全面体检", s.cursor_title)

    def test_rebinding_to_another_composer_pushes_again(self):
        """接手/换窗口把 uuid 挪到新 composer——新那边也得改名。"""
        s = self._named()
        self._sync([s])
        s.cursor_uuid = "U-b"                  # 接手落地，认到了新窗口
        calls = self._sync([s])
        self.assertEqual([("U-b", "rxyy tools·全面体检")], calls[:1],
                         "换了 composer 还不补写，新窗口就一辈子顶着自动标题")
        self.assertEqual({"U-b"}, {uid for uid, _ in calls})

    def test_a_name_the_user_locked_is_pushed_out_too(self):
        """用户亲手锁死的名字是最硬的那一档，更该写到 Cursor 那边去。

        08-25 现场：`机构管理端` 这个 tab 名是人自己锁的，Cursor 侧栏里却仍是
        「Persistent plus zhi report」。原因是同步循环第一道闸就是
        `if s.name_locked: continue`——那道闸本来只为挡住「把 Cursor 标题读进来
        冲掉锁死的名字」，却把往外写也一并挡了。
        """
        s = _s("s4", "69c15899", "机构管理端", msg_seq=30, uuid="U-d")
        s.name_locked = True
        s.agent_named = False
        calls = self._sync([s], ide_title="Persistent plus zhi report")
        self.assertEqual([("U-d", "机构管理端"), ("U-d", "机构管理端")], calls)
        self.assertEqual("机构管理端", s.name, "锁死的名字不许被 Cursor 标题冲掉")

    def test_a_failed_write_is_retried_next_tick(self):
        s = self._named()
        calls = self._sync([s], ok=False)
        self.assertEqual(2, len(calls), "写不进去要下一拍再试，不能记成已完成")
        self.assertIsNone(s.cursor_title)

    def test_a_locked_shell_name_is_still_left_alone(self):
        """壳照旧两个方向都不同步——锁不锁都一样。

        壳的 uuid 是靠「同工作区 + 出生时间」猜出来的，它自己没有任何可辨识的
        动静，猜错的概率在所有 tab 里最高；一旦猜错，往外写就等于把别人那个
        真在干活的对话改名叫「待命·xxx」。壳名本来也没什么可看的。
        """
        shell = _s("s5", "f34c1777", "待命·cursor工作流·1777", msg_seq=2, uuid="U-e")
        shell.name_locked = True
        shell.agent_named = True
        self.assertEqual([], self._sync([shell]))

    def test_a_standby_shell_is_never_pushed_out(self):
        shell = _s("s2", "912382e6", "待命·cursor工作流", msg_seq=2, uuid="U-s")
        shell.agent_named = False
        self.assertEqual([], self._sync([shell]))

    def test_the_ide_to_console_direction_is_untouched(self):
        """没自报过名的照旧是「读进来」，不能反过来把占位名写出去。"""
        s = _s("s3", "b4eff2ee", "换装收尾", msg_seq=22, uuid="U-c")
        s.agent_named = False
        calls = self._sync([s])
        self.assertEqual([], calls)
        self.assertEqual("Swap finalisation", s.name)


if __name__ == "__main__":
    unittest.main()
