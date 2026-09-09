# -*- coding: utf-8 -*-
"""团队面板事实层——08-24「你哪来的队友」两个洞的复现测试。

洞 1：归组只读 tab 名前缀。当天面板上 be748934 的 tab 叫「rxyy tools·看板验收」，
      就被算进 rxyy tools 这一队；它实际领的两张卡在「视频快编后端接口 /
      视频快编前端编辑器」，人也在往 ctest 发布。rxyy 当场问「这个项目就我一个
      人在搞，我哪来的队友」——名字晚改一步，队伍就假一步。
      修法：不替他改归属（按可能过期的旧卡搬人会错得更难查），而是把「它领的卡」
      摆在面板与派活提示词上，对不上就明写。

洞 2：工作树里的脏文件没有主人。谁都能说一句「这是队友未提交的改动」，没有任何
      归属证据；真去查才发现那批改动的主人 08-21 就断线了，代码却在生产上跑了三天。
      修法：hub 每次看见文件锁就往台账记一笔（锁只活几分钟，stop 钩子一收就没了），
      之后拿 git status 的脏文件去台账里反查最后是谁碰的、那人还在不在。
"""
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import hub  # noqa: E402
import team_facts  # noqa: E402

WS = r"d:\桌面\working\cursor工作流"


def _sess(sid, conv, name, project="", root=WS, connected=True, uuid=""):
    s = hub.Session.__new__(hub.Session)
    s.id, s.conv_key, s.name = sid, conv, name
    s.cwd = s.task_root = root
    s.task_root_locked = False
    s.connected = connected
    s.archived = False
    s.pending = None
    s.queued = []
    s.agent_project = project
    s.cursor_uuid = uuid
    s.lock = threading.Lock()
    return s


def _card(cid, project, conv, status="in_progress", title="卡"):
    return {"id": cid, "title": title, "project": project, "status": status,
            "archived": False,
            "assignee": {"conversation_id": conv, "agent_type": "cursor"}}


class ClaimedCardChecksTheSelfReportedProject(unittest.TestCase):
    """洞 1 复现：自报项目与「它领的卡」对不上，必须当场说出来。"""

    def _facts(self, session, cards):
        with patch.object(hub.Api, "_board_cards", lambda self: cards):
            return hub.Api()._agent_facts(session)

    def test_tab_says_rxyy_tools_but_cards_are_all_video_fastcut(self):
        # 08-24 现场：tab「rxyy tools·看板验收」，两张卡都在视频快编，
        # 而 rxyy tools 本身是面板上一个正经项目（8752aeaabdcf 在 b4eff2ee 手上）
        s = _sess("s1", "be748934", "rxyy tools·看板验收", project="rxyy tools")
        cards = [_card("3b81c0c2cc5b", "视频快编后端接口", "be748934", "in_review"),
                 _card("06d65c8b9103", "视频快编前端编辑器", "be748934", "in_review"),
                 _card("8752aeaabdcf", "rxyy tools", "b4eff2ee")]
        facts = self._facts(s, cards)
        self.assertTrue(facts["warn"], "自报与领的卡对不上时必须给出提示")
        self.assertIn("rxyy tools", facts["warn"])
        self.assertIn("视频快编后端接口", facts["warn"])
        self.assertEqual(2, len(facts["cards"]))

    def test_card_matching_the_self_report_is_quiet(self):
        s = _sess("s2", "b4eff2ee", "rxyy tools·团队功能整改", project="rxyy tools")
        facts = self._facts(s, [_card("8752aeaabdcf", "rxyy tools", "b4eff2ee")])
        self.assertEqual("", facts["warn"])
        self.assertEqual(1, len(facts["cards"]))

    def test_no_card_is_not_suspicious(self):
        # 没领卡是常态，不能因此说人家报错了名
        s = _sess("s3", "cccc3333", "快编·三亚JK部署", project="快编")
        self.assertEqual("", self._facts(s, [])["warn"])

    def test_done_and_archived_cards_do_not_count(self):
        s = _sess("s4", "dddd4444", "rxyy tools·收尾", project="rxyy tools")
        cards = [_card("x1", "视频快编后端接口", "dddd4444", "done"),
                 dict(_card("x2", "音频快编", "dddd4444"), archived=True)]
        facts = self._facts(s, cards)
        self.assertEqual([], facts["cards"])
        self.assertEqual("", facts["warn"], "验收完/归档的卡不能拿来指控现在报错了名")

    def test_project_alias_still_counts_as_a_match(self):
        # 「心理评测」是「心理」的历史别名，不该被判成对不上
        s = _sess("s5", "eeee5555", "心理·接口", project="心理")
        facts = self._facts(s, [_card("y1", "心理评测", "eeee5555")])
        self.assertEqual("", facts["warn"])

    def test_short_and_full_conversation_ids_match(self):
        # 卡上记全 ID、会话只有前 8 位（或反过来）都得认出是同一个人
        s = _sess("s6", "be748934", "rxyy tools·看板验收", project="rxyy tools")
        cards = [_card("z1", "视频快编后端接口", "be748934-1c2d-4e5f-8a9b-000000000000"),
                 _card("z2", "rxyy tools", "someone1")]
        self.assertTrue(self._facts(s, cards)["warn"])


class AShorthandTabNameIsNotAnAccusation(unittest.TestCase):
    """08-24 拿真实看板试出来的：只判「卡上没有这个名字」的话，8 个会话喊了 3 个，
    3 个全是虚的——人都在自己卡上，只是 tab 名写得比卡名粗。喊错三次之后，
    真正该喊的那一次就没人看了。所以再加一道：自报的名字得**本身就是**面板上
    一个项目，才敢说它「不在自己报的项目里」。"""

    def _warn(self, named, my_cards, everyone):
        s = _sess("s1", "aaaa1111", named + "·活", project=named)
        with patch.object(hub.Api, "_board_cards", lambda self: everyone):
            return hub.Api()._agent_facts(s)["warn"]

    def test_abbreviated_tab_name_stays_quiet(self):
        # 0e4c1b63 现场：tab 写「快编」，卡是「音频快编后端接口」
        cards = [_card("c1", "音频快编后端接口", "aaaa1111")]
        self.assertEqual("", self._warn("快编", cards, cards))

    def test_coarser_product_name_stays_quiet(self):
        # 1a89a79d 现场：tab 写「录播客户端」，卡是「智慧云广播录播播出端」
        cards = [_card("c1", "智慧云广播录播播出端", "aaaa1111"),
                 _card("c2", "智慧云广播播出客户端", "aaaa1111")]
        self.assertEqual("", self._warn("录播客户端", cards, cards))

    def test_it_still_fires_when_the_name_is_a_real_project(self):
        # be748934 现场：rxyy tools 本身就是面板上一个正经项目，它却一张卡都不在里面
        mine = [_card("c1", "视频快编后端接口", "aaaa1111", "in_review")]
        everyone = mine + [_card("c9", "rxyy tools", "b4eff2ee")]
        warn = self._warn("rxyy tools", mine, everyone)
        self.assertIn("rxyy tools", warn)
        self.assertIn("视频快编后端接口", warn)

    def test_a_finished_project_still_counts_as_a_real_name(self):
        # 项目不会因为卡做完就不存在了；只有归档的卡不算数
        mine = [_card("c1", "视频快编后端接口", "aaaa1111")]
        everyone = mine + [_card("c9", "rxyy tools", "b4eff2ee", "done")]
        self.assertTrue(self._warn("rxyy tools", mine, everyone))
        archived = mine + [dict(_card("c9", "rxyy tools", "b4eff2ee"), archived=True)]
        self.assertEqual("", self._warn("rxyy tools", mine, archived))


class TheWarningTravelsIntoTheDispatchPrompt(unittest.TestCase):
    """派活提示词里那句「同项目还有这些 agent 在跑」是接手方判断「谁能碰哪块代码」
    的依据。只写对方 tab 名，等于把 08-24 那次误判原样传给下一个人。"""

    def test_mate_line_carries_the_conflict(self):
        mate = _sess("s1", "be748934", "rxyy tools·看板验收", project="rxyy tools")
        cards = [_card("c1", "视频快编后端接口", "be748934", "in_review"),
                 _card("c9", "rxyy tools", "b4eff2ee")]
        with patch.object(hub.Api, "_board_cards", lambda self: cards):
            note = hub.Api()._mate_fact_note(mate)
        self.assertIn("视频快编后端接口", note)
        self.assertIn("别把它当本项目的人使唤", note)

    def test_mate_without_conflict_adds_nothing(self):
        mate = _sess("s2", "b4eff2ee", "rxyy tools·团队功能整改", project="rxyy tools")
        with patch.object(hub.Api, "_board_cards",
                          lambda self: [_card("c2", "rxyy tools", "b4eff2ee")]):
            self.assertEqual("", hub.Api()._mate_fact_note(mate))


class OldPackagesWithoutTheModuleKeepWorking(unittest.TestCase):
    """半套同步/老包里没有 team_facts：退回「只看自报」的老行为，不许连坐面板。"""

    def test_everything_degrades_to_quiet(self):
        s = _sess("s1", "be748934", "rxyy tools·看板验收", project="rxyy tools")
        with patch.object(hub, "team_facts", None):
            api = hub.Api()
            self.assertEqual({"cards": [], "warn": ""}, api._agent_facts(s))
            self.assertEqual("", api._mate_fact_note(s))
            self.assertIsNone(api._card_project_of(s))
            self.assertIsNone(api._wip_ledger())
            self.assertEqual(([], {"count": 0, "unowned": 0,
                                   "text": "", "owners": []}),
                             api._wip_rows(WS))


class CardFillsTheProjectOnlyWhenNobodyElseSpoke(unittest.TestCase):
    """归组：卡只捡漏，不抢自报——按可能过期的旧卡搬人会错得更难查。"""

    def _project(self, session, cards):
        with (patch.object(hub.Api, "_board_cards", lambda self: cards),
              patch.object(hub.Api, "_team_project_member", lambda self, r, c: None),
              patch.object(hub.Api, "_seat_lookup", lambda self, c: ("", None, None))):
            return hub.Api()._team_project(session)

    def test_card_fills_in_when_nothing_self_reported(self):
        s = _sess("s1", "aaaa1111", "待命·cursor工作流", project="")
        key, name = self._project(s, [_card("c1", "视频快编后端接口", "aaaa1111")])
        self.assertEqual(hub.Api._project_key("视频快编后端接口"), key)
        self.assertEqual("视频快编后端接口", name)

    def test_self_report_still_wins_over_the_card(self):
        s = _sess("s2", "bbbb2222", "rxyy tools·看板验收", project="rxyy tools")
        key, _name = self._project(s, [_card("c2", "视频快编后端接口", "bbbb2222")])
        self.assertEqual(hub.Api._project_key("rxyy tools"), key,
                         "自报说了话就轮不到卡；对不上只提示，不搬人")

    def test_no_card_falls_back_to_the_workspace_bucket(self):
        s = _sess("s3", "cccc3333", "待命·cursor工作流", project="")
        key, _name = self._project(s, [])
        self.assertEqual(hub.Api.TEAM_DEFAULT_PROJECT, key)


class LedgerRemembersWhoTouchedTheFile(unittest.TestCase):
    """洞 2 之一：锁只活几分钟，台账得活到发现脏文件那一刻。"""

    def test_owner_survives_after_the_lock_is_gone(self):
        led = team_facts.WipLedger()
        led.observe([{"root": WS, "file": "rxyy_mcp/hub.py", "owner": "8f6423db",
                      "tab": "控制台·打包自启", "edits": 3}], now=1000.0)
        # 下一拍锁已经被 stop 钩子收走了——台账里必须还认得这笔
        led.observe([], now=1100.0)
        row = led.lookup(WS, "rxyy_mcp/hub.py")
        self.assertIsNotNone(row, "锁没了就查不到最后是谁改的，等于洞还在")
        self.assertEqual("8f6423db", row["owner"])
        self.assertEqual("控制台·打包自启", row["tab"])

    def test_stale_rows_are_dropped(self):
        led = team_facts.WipLedger(ttl=60)
        led.observe([{"root": WS, "file": "a.py", "owner": "1111", "tab": "甲"}],
                    now=1000.0)
        led.observe([{"root": WS, "file": "b.py", "owner": "2222", "tab": "乙"}],
                    now=2000.0)
        self.assertIsNone(led.lookup(WS, "a.py"), "太久远的记录不该继续冒充证据")
        self.assertIsNotNone(led.lookup(WS, "b.py"))

    def test_survives_a_restart_via_export_load(self):
        led = team_facts.WipLedger()
        led.observe([{"root": WS, "file": "rxyy_mcp/hub.py", "owner": "8f6423db",
                      "tab": "控制台·打包自启"}], now=time.time())
        back = team_facts.WipLedger()
        back.load(json.loads(json.dumps(led.export())))
        self.assertEqual("8f6423db", back.lookup(WS, "rxyy_mcp/hub.py")["owner"])

    def test_paths_normalise_across_slash_and_case(self):
        led = team_facts.WipLedger()
        led.observe([{"root": WS, "file": "rxyy_mcp/Hub.py", "owner": "1111"}],
                    now=time.time())
        self.assertIsNotNone(led.lookup(WS, team_facts.norm_rel(WS, r"rxyy_mcp\hub.py")))


class OrphanWipIsCalledOut(unittest.TestCase):
    """洞 2 之二：脏文件标出「最后编辑会话 + 在不在」，没主的要告警。"""

    def _rows(self, dirty, ledger_rows, live):
        led = team_facts.WipLedger()
        led.observe(ledger_rows, now=time.time())
        return team_facts.attribute_dirty(WS, dirty, led, lambda o: live.get(o))

    def test_offline_editor_makes_it_an_orphan(self):
        # 08-21 那批的原样：写完没提交，会话 16:02 断了再没回来
        rows = self._rows(
            [("rxyy_mcp/hub.py", "rxyy_mcp/hub.py", "M")],
            [{"root": WS, "file": "rxyy_mcp/hub.py", "owner": "8f6423db",
              "tab": "控制台·打包自启"}],
            {"8f6423db": {"tab": "控制台·打包自启", "conv": "8f6423db", "online": False}})
        self.assertEqual(1, len(rows))
        self.assertTrue(rows[0]["orphan"])
        self.assertEqual("offline", rows[0]["state"])
        self.assertEqual("控制台·打包自启", rows[0]["tab"])

    def test_online_editor_is_not_an_orphan(self):
        rows = self._rows(
            [("rxyy_mcp/ui.html", "rxyy_mcp/ui.html", "M")],
            [{"root": WS, "file": "rxyy_mcp/ui.html", "owner": "b4eff2ee", "tab": "在跑的"}],
            {"b4eff2ee": {"tab": "在跑的", "conv": "b4eff2ee", "online": True}})
        self.assertFalse(rows[0]["orphan"])
        self.assertEqual("online", rows[0]["state"])

    def test_editor_no_longer_known_counts_as_gone(self):
        rows = self._rows(
            [("a.py", "a.py", "M")],
            [{"root": WS, "file": "a.py", "owner": "deadbeef", "tab": "早没了"}],
            {})
        self.assertEqual("gone", rows[0]["state"])
        self.assertTrue(rows[0]["orphan"])

    def test_file_nobody_ever_locked_is_listed_but_does_not_cry_wolf(self):
        # 08-24 部署后实测：不加这道限制，一棵树能喊出七八条虚的——
        # .cursor/、.playwright-mcp/、别的项目掉进来的 package.json 全被
        # 当成「没人守着的改动」。喊错的代价是下次真孤儿也没人看。
        rows = self._rows([("mystery.py", "mystery.py", "??")], [], {})
        self.assertEqual("unknown", rows[0]["state"])
        self.assertTrue(rows[0]["untracked"])
        self.assertFalse(rows[0]["orphan"], "没人锁过 ≠ 有人写完跑了")
        summary = team_facts.orphan_summary(rows)
        self.assertEqual(0, summary["count"], "查不到主的不该触发告警")
        self.assertEqual(1, summary["unowned"], "但要照样数出来、列出来")
        self.assertEqual("", summary["text"])

    def test_unowned_files_are_counted_beside_a_real_orphan(self):
        rows = self._rows(
            [("rxyy_mcp/hub.py", "rxyy_mcp/hub.py", "M"),
             ("package.json", "package.json", "??")],
            [{"root": WS, "file": "rxyy_mcp/hub.py", "owner": "8f6423db",
              "tab": "控制台·打包自启"}],
            {"8f6423db": {"tab": "控制台·打包自启", "conv": "8f6423db", "online": False}})
        summary = team_facts.orphan_summary(rows)
        self.assertEqual(1, summary["count"])
        self.assertEqual(1, summary["unowned"])
        self.assertIn("另有 1 份查不到", summary["text"])

    def test_each_row_says_which_repo_it_came_from(self):
        # 几个工作区的队伍合成一行时，不带仓名的文件清单是在撒谎：08-24 部署后
        # 实测「AI能力服务」（仓在 Mental_Health_Assessment）那行列出了
        # cursor工作流 的 package.json
        rows = self._rows([("a.py", "a.py", "M")], [], {})
        self.assertEqual(WS, rows[0]["root"])
        self.assertEqual("cursor工作流", rows[0]["ws"])

    def test_summary_names_the_last_editor(self):
        rows = self._rows(
            [("rxyy_mcp/hub.py", "rxyy_mcp/hub.py", "M"),
             ("src/oa/company_hop.py", "src/oa/company_hop.py", "M")],
            [{"root": WS, "file": "rxyy_mcp/hub.py", "owner": "8f6423db",
              "tab": "控制台·打包自启"},
             {"root": WS, "file": "src/oa/company_hop.py", "owner": "8f6423db",
              "tab": "控制台·打包自启"}],
            {"8f6423db": {"tab": "控制台·打包自启", "conv": "8f6423db", "online": False}})
        summary = team_facts.orphan_summary(rows)
        self.assertEqual(2, summary["count"])
        self.assertIn("控制台·打包自启", summary["text"])
        self.assertIn("已离线", summary["text"])

    def test_clean_tree_says_nothing(self):
        self.assertEqual(0, team_facts.orphan_summary([])["count"])
        self.assertEqual("", team_facts.orphan_summary([])["text"])

    def test_orphans_sort_above_unowned_which_sort_above_the_living(self):
        rows = self._rows(
            [("live.py", "live.py", "M"), ("nobody.py", "nobody.py", "??"),
             ("left.py", "left.py", "M")],
            [{"root": WS, "file": "live.py", "owner": "aaaa1111", "tab": "在跑"},
             {"root": WS, "file": "left.py", "owner": "bbbb2222", "tab": "走了"}],
            {"aaaa1111": {"tab": "在跑", "conv": "aaaa1111", "online": True},
             "bbbb2222": {"tab": "走了", "conv": "bbbb2222", "online": False}})
        self.assertEqual(["left.py", "live.py", "nobody.py"],
                         [r["file"] for r in rows])


class TeamStateCarriesTheEvidence(unittest.TestCase):
    """事实层算得再对，不进 team_state 就等于没有——面板读的是这一份。"""

    def _state(self, sessions, cards, lock_items, dirty):
        api = hub.Api()
        d = {s.id: s for s in sessions}
        live = {"state": "working", "label": "", "sure": True,
                "evidence": [], "death": None}
        hub_api = sys.modules[hub.Api.__module__]
        hub_api._FACTS["wip"] = {}
        hub_api._FACTS["ledger"] = None
        with (patch.object(hub.HUB, "sessions", d),
              patch.object(hub.HUB, "order", list(d)),
              patch.object(hub.HUB, "bulletin", {}),
              patch.object(hub.HUB, "_window_tag",
                           lambda pid, cwd=None, transcript_path=None: ""),
              patch.dict(hub.HUB.cfg, {"team_seats": {}, "wip_ledger": []}),
              patch.object(hub.Api, "_persist_wip_ledger", lambda self, led, **kw: None),
              patch.object(hub.Api, "_board_cards", lambda self: cards),
              patch.object(hub.Api, "agentboard", lambda self: {"items": lock_items}),
              patch.object(hub.Api, "_agent_liveness", lambda self, s, now: dict(live)),
              patch.object(hub.Api, "session_label", lambda self, s: s.name),
              patch.object(hub.Api, "_team_role", lambda self, s: ""),
              patch.object(hub.Api, "_team_assign", lambda self, s: ""),
              patch.object(hub.Api, "_seat_of", lambda self, conv: (None, {})),
              patch.object(hub.Api, "_team_board",
                           lambda self, root, project=None: {"text": "", "updated_at": 0}),
              patch.object(hub.Api, "_seats", lambda self, root, project=None: []),
              patch.object(team_facts, "git_dirty", lambda root, timeout=None: dirty)):
            # 第一拍只把扫描派出去（面板不许被 git 卡住），扫完再取一次
            api.team_state()
            api._wip_scan_now(WS, team_facts.hub_norm(WS))
            return api.team_state()

    def _sess_full(self, sid, conv, name, project, uuid=""):
        s = _sess(sid, conv, name, project=project, uuid=uuid)
        s.pid = 1
        s.rev = 1
        s.agent_status = ""
        s.agent_activity = ""
        s.last_heartbeat = 0
        s.recon_deadline = None
        s.transcript_path = None
        return s

    def test_the_agent_row_shows_its_cards_and_the_conflict(self):
        s = self._sess_full("s1", "be748934", "rxyy tools·看板验收", "rxyy tools")
        r = self._state([s], [_card("c1", "视频快编后端接口", "be748934", "in_review"),
                              _card("c9", "rxyy tools", "b4eff2ee")], [], [])
        row = [a for p in r["projects"] for a in p["agents"]][0]
        self.assertEqual(["视频快编后端接口"], [c["project"] for c in row["cards"]])
        self.assertIn("视频快编后端接口", row["fact_warn"])

    def test_orphan_wip_reaches_the_project_row(self):
        # 08-21 那批的原样：改文件的那个会话已经断了，改动还在树上
        gone = self._sess_full("s2", "8f6423db", "控制台·打包自启", "rxyy tools",
                               uuid="8f6423db-aaaa")
        gone.connected = False
        here = self._sess_full("s1", "b4eff2ee", "rxyy tools·团队功能整改", "rxyy tools")
        r = self._state(
            [here, gone], [],
            [{"root": WS, "file": "rxyy_mcp/hub.py", "owner": "8f6423db",
              "tab": "控制台·打包自启", "stale": False, "edits": 3}],
            [("rxyy_mcp/hub.py", "rxyy_mcp/hub.py", "M")])
        p = [x for x in r["projects"] if x["agents"]][0]
        self.assertEqual(1, p["wip_orphans"]["count"])
        self.assertIn("控制台·打包自启", p["wip_orphans"]["text"])
        self.assertEqual("offline", p["wip"][0]["state"])
        self.assertEqual("控制台·打包自启", p["wip"][0]["tab"])

    def test_a_file_its_own_live_agent_is_editing_is_not_an_orphan(self):
        here = self._sess_full("s1", "b4eff2ee", "rxyy tools·团队功能整改",
                               "rxyy tools", uuid="b4eff2ee-bbbb")
        r = self._state(
            [here], [],
            [{"root": WS, "file": "rxyy_mcp/hub.py", "owner": "b4eff2ee",
              "tab": "rxyy tools·团队功能整改", "stale": False}],
            [("rxyy_mcp/hub.py", "rxyy_mcp/hub.py", "M")])
        p = [x for x in r["projects"] if x["agents"]][0]
        self.assertEqual(0, p["wip_orphans"]["count"])
        self.assertEqual("online", p["wip"][0]["state"])


class ThePanelActuallyShowsIt(unittest.TestCase):
    """事实进了接口还得进屏幕：孤儿 WIP 的性质就是「没人会主动来找它」，
    藏在数据里等人去查等于没告警。桌面与手机两块都要有。"""

    @classmethod
    def setUpClass(cls):
        cls.ui = (MODULE_DIR / "ui.html").read_text(encoding="utf-8")
        cls.share = (MODULE_DIR / "share.html").read_text(encoding="utf-8")

    def test_desktop_renders_cards_conflict_and_wip(self):
        self.assertIn("function cardBadges(a)", self.ui)
        self.assertIn("(a.cards || [])", self.ui)
        self.assertIn("if (!a.fact_warn) return \"\";", self.ui)
        self.assertIn("const rows = p.wip || [], sum = p.wip_orphans || {};", self.ui)
        self.assertIn("+ cardBadges(a)", self.ui)
        self.assertIn("+ factWarn(a)", self.ui)
        self.assertIn("+ wipSection(p)", self.ui)

    def test_desktop_sidebar_flags_orphans_without_clicking_in(self):
        self.assertIn('(p.wip_orphans || {}).count ? " · ⚠ 没主改动 "', self.ui)

    def test_desktop_tags_each_dirty_file_with_its_repo(self):
        self.assertIn("const many = new Set(rows.map(w => w.ws || \"\")).size > 1;", self.ui)
        self.assertIn("(many && w.ws ? '<b>' + escHtml(w.ws) + '</b> / ' : '')", self.ui)

    def test_desktop_does_not_paint_unowned_files_red(self):
        self.assertIn('unknown: ["查不到主", "kept"],', self.ui)

    def test_phone_renders_them_too(self):
        self.assertIn("for (const c of (a.cards || []))", self.share)
        self.assertIn("if (a.fact_warn)", self.share)
        self.assertIn("const wsum = p.wip_orphans || {};", self.share)
        self.assertIn("(p.wip || []).filter(x => x.orphan)", self.share)
        self.assertIn('(w.ws ? w.ws + " / " : "")', self.share)


class GitDirtyReadsTheRealThing(unittest.TestCase):
    """脏文件清单必须来自 git 本身，且新增文件不能漏——孤儿 WIP 最爱藏在未跟踪里。"""

    def test_modified_and_untracked_both_show_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = lambda *a: subprocess.run(  # noqa: E731
                a, cwd=tmp, capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            run("git", "init", "-q")
            run("git", "config", "user.email", "t@t")
            run("git", "config", "user.name", "t")
            Path(tmp, "kept.py").write_text("x = 1\n", encoding="utf-8")
            run("git", "add", "-A")
            run("git", "commit", "-qm", "base")
            Path(tmp, "kept.py").write_text("x = 2\n", encoding="utf-8")
            Path(tmp, "brand_new.py").write_text("y = 1\n", encoding="utf-8")
            got = {key: code for key, _shown, code in team_facts.git_dirty(tmp)}
        self.assertEqual("M", got.get("kept.py"))
        self.assertEqual("??", got.get("brand_new.py"), "未跟踪的新文件也是未提交改动")

    def test_a_directory_without_git_is_simply_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual([], team_facts.git_dirty(tmp))


if __name__ == "__main__":
    unittest.main()
