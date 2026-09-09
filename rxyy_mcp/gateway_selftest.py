"""gateway 自测：用假 Api 起网关，不依赖真 hub，也不碰正在运行的实例。

    python gateway_selftest.py         # 起在 127.0.0.1:38779

然后浏览器/嵌入访问 http://127.0.0.1:38779/ui 应能看到控制台框架 + 一个演示会话。
仅用于开发期验证 http 网关 + shim 注入 + ui.html 在纯 http 下可渲染。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from gateway import start_gateway

UI = Path(__file__).with_name("ui.html")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("GW_PORT", "38779"))


class FakeApi:
    """只实现 ui.html 首屏渲染需要的几个只读方法，其余交给网关返回 404（前端多为容错调用）。"""

    def get_state(self):
        return {
            "sessions": [
                {
                    "id": "demo1", "name": "演示会话", "cwd": "D:/Desktop/demo",
                    "peer_ip": "127.0.0.1", "pid": 1234, "conv_key": "029705cd",
                    "wtag": "", "connected": True, "reconnecting": False,
                    "ide_active": False, "heartbeat_alive": True,
                    "agent_status": "ready", "agent_activity": "待命",
                    "pending": False, "pending_lost": False, "options": [],
                    "rev": 1, "file": "", "created": time.time(),
                    "processing_secs": None, "processing_stale": False, "queued": 0,
                }
            ],
            "config": {
                "history_dir": "D:/hist", "always_on_top": False, "audio_enabled": True,
                "share_enabled": True, "max_messages": 20, "max_history_files": 20,
                "quiet_mode": False, "autostart_enabled": False, "config_error": "",
                "token_freeze": False, "frozen_count": 0,
            },
        }

    def get_messages(self, session_id):
        return {
            "rev": 1,
            "messages": [
                {"role": "ai", "ts": "12:00:00", "html": "<p>你好，我是演示消息（网关自测）。</p>"},
                {"role": "user", "ts": "12:00:05", "html": "<p>收到，管道通。</p>"},
            ],
        }

    def get_quick_phrases(self):
        return {"phrases": ["继续", "用中文", "先测试再改"]}


if __name__ == "__main__":
    start_gateway(FakeApi(), UI, port=PORT)
    print("gateway selftest running: http://127.0.0.1:%d/ui" % PORT)
    while True:
        time.sleep(3600)
