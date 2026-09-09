# -*- coding: utf-8 -*-
"""文件占用锁作为第四条「在干活」证据（Cursor 流水没定位到时的兜底）"""
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub

WS = r"c:\Users\Administrator\AICodebrain"


def _s(sid, uuid=None, cwd=WS, connected=True):
    s = hub.Session.__new__(hub.Session)
    s.id, s.cwd, s.connected = sid, cwd, connected
    s.cursor_uuid = uuid
    s.lock = threading.Lock()
    return s


def _board(*owners):
    return {"ok": True, "items": [{"root": WS, "owner": o, "file": "a.py", "stale": False}
                                  for o in owners]}


class UnclaimedLockOwnerTests(unittest.TestCase):
    def _run(self, target, peers, board):
        # 只吃现成缓存、绝不自己去读盘取锁（那样会跟调用方持有的 HUB.lock 自锁死）
        d = {x.id: x for x in [target] + peers}
        api = hub.Api()
        api._board_cache = (time.time(), board)
        with patch.object(hub.HUB, "sessions", d):
            return api.unclaimed_lock_owner(target)

    def test_ignores_a_stale_cache_instead_of_reading_disk(self):
        api = hub.Api()
        api._board_cache = (time.time() - 30, _board("bbbbbbbb"))
        with patch.object(hub.HUB, "sessions", {"s1": _s("s1")}):
            self.assertEqual("", api.unclaimed_lock_owner(_s("s1")))

    def test_lone_unidentified_tab_takes_the_unclaimed_lock(self):
        me = _s("s1")
        peer = _s("s2", uuid="aaaaaaaa")
        self.assertEqual("bbbbbbbb", self._run(me, [peer], _board("aaaaaaaa", "bbbbbbbb")))

    def test_stays_silent_when_another_tab_is_also_unidentified(self):
        me, blind = _s("s1"), _s("s2")
        self.assertEqual("", self._run(me, [blind], _board("bbbbbbbb")))

    def test_stays_silent_when_two_locks_are_unclaimed(self):
        me = _s("s1")
        self.assertEqual("", self._run(me, [], _board("bbbbbbbb", "cccccccc")))

    def test_stale_locks_prove_nothing(self):
        me = _s("s1")
        board = {"ok": True, "items": [{"root": WS, "owner": "bbbbbbbb",
                                        "file": "a.py", "stale": True}]}
        self.assertEqual("", self._run(me, [], board))

    def test_other_workspaces_locks_are_ignored(self):
        me = _s("s1")
        board = {"ok": True, "items": [{"root": r"d:\other", "owner": "bbbbbbbb",
                                        "file": "a.py", "stale": False}]}
        self.assertEqual("", self._run(me, [], board))


if __name__ == "__main__":
    unittest.main()
