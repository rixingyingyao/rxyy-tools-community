# -*- coding: utf-8 -*-
"""局域网优先跳转（08-04 用户要求：一条链接走天下，优先局域网、不行再走外网）。

发给同事的是公网 https 链接，但办公室里的人点开它等于「办公室→Cloudflare→
办公室」兜一圈。这里锁死那条判据与它的全部边界：判据是「访客公网出口 IP ==
本机公网出口 IP」，判不准一律不跳；接口不跳；跳不过去的人退回来不再被弹走。
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import share_server as ss

OFFICE = "222.244.145.7"
LAN = "192.168.20.52"
PORT = 39080


def target(cfg=None, fwd=OFFICE, cookie="", path="/tasks", query="k=abc123", lan=LAN):
    base = {"lan_first": True, "office_egress_ip": OFFICE}
    base.update(cfg or {})
    return ss.lan_first_target(base, fwd, cookie, path, query, PORT, lan=lan)


class LanFirstTests(unittest.TestCase):
    def test_same_office_egress_goes_to_the_lan_address(self):
        self.assertEqual(
            "http://192.168.20.52:39080/tasks?k=abc123&nolan=1", target())

    def test_query_less_url_still_gets_the_loop_guard(self):
        self.assertEqual("http://192.168.20.52:39080/?nolan=1",
                         target(path="/", query=""))

    def test_visitor_from_anywhere_else_stays_on_the_public_link(self):
        self.assertEqual("", target(fwd="114.114.8.8"))

    def test_direct_lan_or_tailscale_hit_has_no_forwarded_ip_so_no_jump(self):
        # 没有 CF-Connecting-IP / X-Forwarded-For = 人家本来就是直连进来的
        self.assertEqual("", target(fwd=""))

    def test_unknown_own_egress_never_guesses(self):
        with patch.dict(ss._EGRESS, {"ip": ""}, clear=False):
            self.assertEqual("", target(cfg={"office_egress_ip": ""}))

    def test_probed_egress_is_used_when_config_has_none(self):
        with patch.dict(ss._EGRESS, {"ip": OFFICE}, clear=False):
            self.assertTrue(target(cfg={"office_egress_ip": ""}))

    def test_nolan_marker_stops_the_second_hop(self):
        # 跳过去之后带着 nolan=1，局域网那边不能再往回跳，否则来回打转
        self.assertEqual("", target(query="k=abc123&nolan=1"))

    def test_a_visitor_who_came_back_is_not_bounced_again(self):
        self.assertEqual("", target(cookie="foo=1; lan_tried=1"))

    def test_switch_off_disables_everything(self):
        self.assertEqual("", target(cfg={"lan_first": False}))

    def test_useless_lan_ip_is_not_worth_jumping_to(self):
        for ip in ("127.0.0.1", "169.254.11.2", ""):
            self.assertEqual("", target(lan=ip), ip)

    def test_https_base_is_tucked_into_the_lan_jump(self):
        got = target(cfg={"share_https_base": "https://console.example.invalid"})
        self.assertTrue(got.startswith("http://192.168.20.52:39080/tasks?"))
        self.assertIn("nolan=1", got)
        self.assertIn("pub=" + quote("https://console.example.invalid", safe=""), got)

    def test_session_share_path_can_still_compute_a_lan_target(self):
        got = target(path="/s/fae731c5", query="t=abc",
                     cfg={"share_https_base": "https://plus.example"})
        self.assertIn("/s/fae731c5?", got)
        self.assertIn("nolan=1", got)

    def test_handler_does_not_lan_first_session_share_pages(self):
        src = (MODULE_DIR / "share_server.py").read_text(encoding="utf-8")
        get = src.split("def do_GET", 1)[1].split("def do_POST", 1)[0]
        self.assertIn("if path in LAN_FIRST_PATHS:", get)
        self.assertNotIn('or path.startswith("/s/")', get)

    def test_session_share_menu_copies_https_only(self):
        ui = (MODULE_DIR / "ui.html").read_text(encoding="utf-8")
        self.assertIn("公网分享链接已复制", ui)
        self.assertNotIn("直连链接已复制（局域网+远程）", ui)

    def test_http_remote_base_is_not_a_public_bounce_target(self):
        self.assertEqual("", ss.public_share_base(
            {"share_https_base": "http://100.1.2.3:39080"}))
        self.assertEqual("https://console.example.invalid", ss.public_share_base(
            {"share_https_base": " https://console.example.invalid/ "}))

    def test_share_html_gets_the_https_base_injected(self):
        raw = b'<html><script src="/highlight.min.js" defer></script></html>'
        out = ss.with_share_public_base(raw, {"share_https_base": "https://console.example.invalid"})
        self.assertIn(b'window.SHARE_PUBLIC_BASE="https://console.example.invalid"', out)
        self.assertIn(b'<script src="/highlight.min.js"', out)

    def test_injection_is_a_no_op_without_https_base(self):
        raw = b'<script src="/highlight.min.js">'
        self.assertEqual(raw, ss.with_share_public_base(raw, {}))


class EgressProbeTests(unittest.TestCase):
    def test_config_value_wins_over_probe(self):
        with patch.dict(ss._EGRESS, {"ip": "1.2.3.4"}, clear=False):
            self.assertEqual(OFFICE, ss.office_egress_ip(
                {"office_egress_ip": " {} ".format(OFFICE)}))

    def test_probe_rejects_garbage_answers(self):
        # 回声服务偶尔返回 HTML 错误页/IPv6，喂进判据里会把全员都当成办公网
        with patch.object(ss, "_probe_egress_ip", lambda timeout=5: "<html>oops"):
            with patch.dict(ss._EGRESS, {"ip": "", "ts": 0.0}, clear=False):
                self.assertEqual("", ss.refresh_egress_ip(force=True))

    def test_bad_shapes_are_rejected(self):
        for bad in ("", "1.2.3", "1.2.3.4.5", "999.1.1.1", "abc", "::1", "1.2.3.a"):
            self.assertFalse(ss._is_ipv4(bad), bad)
        self.assertTrue(ss._is_ipv4("222.244.145.7"))


class ShareLanBounceUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (MODULE_DIR / "share.html").read_text(encoding="utf-8")
        start = cls.html.index("function isPrivateHost(host) {")
        end = cls.html.index("function bounceLanToPublic()")
        cls.fns = cls.html[start:end]

    def test_share_page_has_the_lan_dead_bounce(self):
        self.assertIn("function bounceLanToPublic()", self.html)
        self.assertIn("if (offline) return 1200;", self.html)
        self.assertIn("链路闪断也会这样", self.html)
        self.assertNotIn("发起人控制台可能已关闭", self.html)

    def _eval_bounce(self, loc, extra, public_base=""):
        script = """
const window = { SHARE_PUBLIC_BASE: %s };
const localStorage = { getItem() { return ""; }, setItem() {} };
const location = %s;
%s
%s
console.log('__DONE__');
""" % (json.dumps(public_base), json.dumps(loc), self.fns, extra)
        proc = subprocess.run(
            ["node", "--input-type=module", "--eval", script],
            check=True, text=True, capture_output=True,
        )
        self.assertIn("__DONE__", proc.stdout)

    def test_private_host_with_pub_bounces_to_https_and_drops_pub(self):
        loc = {
            "hostname": "192.168.0.104",
            "port": "39080",
            "origin": "http://192.168.0.104:39080",
            "pathname": "/s/fae731c5",
            "search": "?t=abc&nolan=1&pub=" + quote("https://console.example.invalid", safe=""),
        }
        self._eval_bounce(loc, r"""
if (!isPrivateHost("192.168.0.104")) throw new Error("lan not private");
if (isPrivateHost("console.example.invalid")) throw new Error("public host marked private");
const dest = publicBounceUrl();
if (dest !== "https://console.example.invalid/s/fae731c5?t=abc&nolan=1") {
  throw new Error("bounce dest=" + dest);
}
""")

    def test_share_port_fallback_saves_clipboard_lan_links(self):
        loc = {
            "hostname": "192.168.0.104",
            "port": "39080",
            "origin": "http://192.168.0.104:39080",
            "pathname": "/s/fae731c5",
            "search": "?t=abc",
        }
        self._eval_bounce(loc, r"""
const dest = publicBounceUrl();
if (dest !== "https://console.example.invalid/s/fae731c5?t=abc&nolan=1") {
  throw new Error("fallback dest=" + dest);
}
""")

    def test_already_on_https_public_host_does_not_bounce(self):
        loc = {
            "hostname": "console.example.invalid",
            "port": "",
            "origin": "https://console.example.invalid",
            "pathname": "/s/fae731c5",
            "search": "?t=abc&nolan=1",
        }
        self._eval_bounce(loc, r"""
if (publicBounceUrl() !== "") throw new Error("public host bounced");
""", public_base="https://console.example.invalid")


if __name__ == "__main__":
    unittest.main()
