# -*- coding: utf-8 -*-
"""Syncthing 的冲突副本别参与收集。

同 console/tests/conftest.py：`xxx.sync-conflict-<日期>-<设备>.py` 也匹配
`test_*.py`，但名字里的点让 pytest 拼不出合法模块名，一个副本就能让整个目录
收集失败。这里的副本目前是零，先把门堵上——下一次两端同时改测试就会出现。
"""

collect_ignore_glob = ["*.sync-conflict-*"]
