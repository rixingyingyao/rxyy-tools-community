# -*- coding: utf-8 -*-
"""常驻区的定位规则。

这个模块决定「hub/MCP 到底从哪个目录跑」，判错的后果不是报错而是**机器上多出一套
rxyy MCP**——两套各带一张互保网轮流坐庄，39222 上在飞的 zhi 全断（07-29 事故，
见 instance_owner.py 开头）。所以每一条「什么情况下不算数」都得钉住：宁可退回包内
那份（今天的行为），也不能半信半疑地指过去。
"""
import ast
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

import live_runtime  # noqa: E402


class LiveRuntimeLocationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.packed = root / "dist" / "rxyy-tools-community" / "_internal" / "rxyy_mcp"
        self.packed.mkdir(parents=True)
        self.live = root / "live"
        os.environ.pop("CHIJIU_LIVE_DIR", None)
        self.addCleanup(lambda: os.environ.pop("CHIJIU_LIVE_DIR", None))

    def _make_live(self, missing=()):
        code = self.live / live_runtime.CODE_SUBDIR
        code.mkdir(parents=True, exist_ok=True)
        (self.live / live_runtime.STATE_SUBDIR).mkdir(parents=True, exist_ok=True)
        for n in live_runtime._REQUIRED:
            if n not in missing:
                (code / n).write_text("# " + n, encoding="utf-8")
        return code

    def _pointer(self, text, encoding="utf-8"):
        (self.packed / live_runtime.POINTER_NAME).write_text(text, encoding=encoding)

    def test_no_pointer_means_keep_running_from_the_package(self):
        self._make_live()
        self.assertIsNone(live_runtime.live_code_dir(self.packed))

    def test_a_pointer_to_a_complete_copy_wins(self):
        code = self._make_live()
        self._pointer(str(self.live))
        self.assertEqual(code, live_runtime.live_code_dir(self.packed))

    def test_a_half_materialised_copy_is_refused_rather_than_half_run(self):
        # 上一次物化被打断（断电/换装中途），缺文件就退回包内那份
        self._make_live(missing=("watchdog.py",))
        self._pointer(str(self.live))
        self.assertIsNone(live_runtime.live_code_dir(self.packed))

    def test_a_copy_without_billgate_dependencies_is_not_complete(self):
        self._make_live(missing=("billgate_hook.js",))
        self._pointer(str(self.live))
        self.assertIsNone(live_runtime.live_code_dir(self.packed))

    def test_a_pointer_to_nowhere_is_ignored(self):
        self._pointer(str(self.live))  # 压根没建
        self.assertIsNone(live_runtime.live_code_dir(self.packed))

    def test_comments_quotes_and_a_powershell_bom_are_tolerated(self):
        code = self._make_live()
        self._pointer('# build.ps1 写的\n"%s"\n' % self.live, encoding="utf-8-sig")
        self.assertEqual(code, live_runtime.live_code_dir(self.packed))

    def test_an_undecodable_pointer_does_not_crash_the_boot(self):
        self._make_live()
        (self.packed / live_runtime.POINTER_NAME).write_bytes(b"\xff\xfe\x00\x81\x9d\x8e")
        self.assertIsNone(live_runtime.live_code_dir(self.packed))

    def test_the_live_copy_never_delegates_to_itself(self):
        # 常驻区里那份要是也带着指针（物化时整目录拷过去很容易带上），
        # 不挡住就会自己指自己，绕成死循环
        code = self._make_live()
        (code / live_runtime.POINTER_NAME).write_text(str(self.live), encoding="utf-8")
        self.assertIsNone(live_runtime.live_code_dir(code))

    def test_the_environment_variable_outranks_the_pointer(self):
        code = self._make_live()
        other = Path(self._tmp.name) / "另一处"
        (other / live_runtime.CODE_SUBDIR).mkdir(parents=True)
        self._pointer(str(other))  # 指针指向一份不完整的
        os.environ["CHIJIU_LIVE_DIR"] = str(self.live)
        self.assertEqual(code, live_runtime.live_code_dir(self.packed))

    def test_state_dir_sits_beside_the_code_not_inside_the_swapped_tree(self):
        # 机器态跟着常驻区走，绝不能留在 dist\ 里——换装时 data\ 会被整个 Move，
        # 常驻进程要是还攥着那边的文件，换装当场失败
        self.assertEqual(self.live / "state", live_runtime.state_dir(self.live))
        self.assertNotIn("dist", str(live_runtime.state_dir(self.live)))


class MaterialiseTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.src = root / "srcrxyy MCP"
        self.src.mkdir()
        self.live = root / "live"
        for n in live_runtime._REQUIRED:
            (self.src / n).write_text("# " + n, encoding="utf-8")
        (self.src / "ui.html").write_text("<html>", encoding="utf-8")
        (self.src / "notify.wav").write_bytes(b"RIFF")
        (self.src / "highlight.min.js").write_text("//js", encoding="utf-8")

    def test_code_and_static_files_come_over(self):
        written = live_runtime.materialize(self.src, self.live)
        self.assertIn("hub.py", written)
        self.assertIn("ui.html", written)
        self.assertIn("notify.wav", written)
        self.assertTrue(live_runtime.is_complete(self.live / "rxyy_mcp"))

    def test_logs_locks_and_machine_state_are_left_behind(self):
        # 拿源目录的旧日志/旧配置去盖常驻区，轻则添乱重则把 tab 冲掉
        for junk in ("hub-run.log", ".hub-spawn.lock", "config.json",
                     ".sessions.json", ".instance-owner.json", "requirements.txt"):
            (self.src / junk).write_text("x", encoding="utf-8")
        live_runtime.materialize(self.src, self.live)
        for junk in ("hub-run.log", ".hub-spawn.lock", "config.json",
                     ".sessions.json", ".instance-owner.json", "requirements.txt"):
            self.assertFalse((self.live / "rxyy_mcp" / junk).exists(), junk)

    def test_the_stale_live_pointer_is_never_carried_into_the_live_copy(self):
        # 带过去就成了常驻区自己指自己
        (self.src / live_runtime.POINTER_NAME).write_text("x", encoding="utf-8")
        live_runtime.materialize(self.src, self.live)
        self.assertFalse((self.live / "rxyy_mcp" / live_runtime.POINTER_NAME).exists())

    def test_it_points_machine_state_out_of_the_swapped_tree(self):
        live_runtime.materialize(self.src, self.live)
        txt = (self.live / "rxyy_mcp" / "datadir.txt").read_text(encoding="utf-8")
        self.assertIn(str(live_runtime.state_dir(self.live)), txt)
        self.assertTrue(live_runtime.state_dir(self.live).is_dir())

    def test_refreshing_over_a_running_copy_replaces_code_without_touching_state(self):
        live_runtime.materialize(self.src, self.live)
        live_runtime.state_dir(self.live).joinpath("config.json").write_text(
            '{"port": 38999}', encoding="utf-8")
        (self.src / "hub.py").write_text("# 新版 hub", encoding="utf-8")
        live_runtime.materialize(self.src, self.live)
        self.assertEqual("# 新版 hub",
                         (self.live / "rxyy_mcp" / "hub.py").read_text(encoding="utf-8"))
        self.assertEqual('{"port": 38999}',
                         live_runtime.state_dir(self.live).joinpath(
                             "config.json").read_text(encoding="utf-8"))


class SeedStateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.packed_state = root / "dist" / "rxyy-tools-community" / "data" / "rxyy_mcp"
        self.packed_state.mkdir(parents=True)
        self.live = root / "live"
        (self.packed_state / "config.json").write_text('{"port": 38999}', encoding="utf-8")
        (self.packed_state / ".sessions.json").write_text('{"tabs": 19}', encoding="utf-8")
        (self.packed_state / "board.json").write_text("{}", encoding="utf-8")
        (self.packed_state / ".mcp-convs.json").write_text('{"d66f1bf0": 1}',
                                                           encoding="utf-8")
        (self.packed_state / ".instance-owner.json").write_text(
            '{"app_dir": "包内那份"}', encoding="utf-8")
        (self.packed_state / "hub-run.log").write_text("noise", encoding="utf-8")

    def test_everyones_tabs_come_across_on_the_first_cutover(self):
        seeded = live_runtime.seed_state(self.packed_state, self.live)
        self.assertIn(".sessions.json", seeded)
        self.assertEqual('{"tabs": 19}', live_runtime.state_dir(self.live).joinpath(
            ".sessions.json").read_text(encoding="utf-8"))

    def test_the_heartbeat_roster_comes_across_or_a_roomful_of_agents_is_buried(self):
        # 切换那一下 MCP 进程必然换命，名单没跟过来 = 那些还在 Generating 的 agent
        # 被 hub 一律翻成「已终止」（08-07 12:16：19 个 tab 埋了 15 个）
        self.assertIn(".mcp-convs.json",
                      live_runtime.seed_state(self.packed_state, self.live))

    def test_the_owner_marker_is_left_behind_so_the_live_copy_can_claim_freely(self):
        # 搬过去 = 常驻区一起来就看见一个指向包内的「正主」，看门狗当场退位不干活
        live_runtime.seed_state(self.packed_state, self.live)
        self.assertFalse(live_runtime.state_dir(self.live)
                         .joinpath(".instance-owner.json").exists())

    def test_logs_are_not_machine_state(self):
        live_runtime.seed_state(self.packed_state, self.live)
        self.assertFalse(live_runtime.state_dir(self.live).joinpath("hub-run.log").exists())

    def test_a_second_run_never_overwrites_what_the_live_hub_has_since_written(self):
        live_runtime.seed_state(self.packed_state, self.live)
        live_runtime.state_dir(self.live).joinpath(".sessions.json").write_text(
            '{"tabs": 23}', encoding="utf-8")
        self.assertEqual([], live_runtime.seed_state(self.packed_state, self.live))
        self.assertEqual('{"tabs": 23}', live_runtime.state_dir(self.live).joinpath(
            ".sessions.json").read_text(encoding="utf-8"))

    def test_nothing_to_seed_from_is_not_an_error(self):
        empty = Path(self._tmp.name) / "空的"
        empty.mkdir()
        self.assertEqual([], live_runtime.seed_state(empty, self.live))


class DelegationTests(unittest.TestCase):
    """入口处「这活归不归常驻区」的判断。

    判错不是报错，是机器上多出一套rxyy MCP——两套互保网轮流坐庄，39222 上在飞的
    zhi 全断（07-29 事故）。所以每一道闸都单独钉住，不确定一律留在原地跑。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.packed = root / "packedrxyy MCP"
        self.packed.mkdir()
        self.live = root / "live"
        self.code = self.live / live_runtime.CODE_SUBDIR
        self.code.mkdir(parents=True)
        (self.live / live_runtime.STATE_SUBDIR).mkdir()
        for n in live_runtime._REQUIRED:
            (self.code / n).write_text("# " + n, encoding="utf-8")
        (self.packed / live_runtime.POINTER_NAME).write_text(
            str(self.live), encoding="utf-8")
        self.py = root / "pythonw.exe"
        self.py.write_bytes(b"MZ")
        os.environ.pop("CHIJIU_LIVE_DIR", None)
        self.addCleanup(lambda: os.environ.pop("CHIJIU_LIVE_DIR", None))

    def test_the_packaged_copy_hands_the_job_over_with_its_arguments(self):
        live_runtime.record_python(self.live, self.py)
        cmd = live_runtime.delegate_cmd("server.py", ["--http", "39222"], self.packed)
        self.assertEqual([str(self.py), str(self.code / "server.py"), "--http", "39222"],
                         cmd)

    def test_without_a_recorded_interpreter_it_stays_put(self):
        # 宁可留在包里跑（今天的行为），也不能拿 PATH 上那个 miniconda 3.8 去赌
        self.assertIsNone(live_runtime.delegate_cmd("hub.py", [], self.packed))

    def test_an_interpreter_that_has_since_been_uninstalled_stays_put(self):
        live_runtime.record_python(self.live, self.py)
        self.py.unlink()
        self.assertIsNone(live_runtime.delegate_cmd("hub.py", [], self.packed))

    def test_a_script_the_live_copy_does_not_have_stays_put(self):
        live_runtime.record_python(self.live, self.py)
        self.assertIsNone(live_runtime.delegate_cmd("重启rxyy MCP.py", [], self.packed))

    def test_the_live_copy_itself_never_hands_the_job_on(self):
        # 这是防「自己拉自己拉成一串」的那道闸
        live_runtime.record_python(self.live, self.py)
        self.assertIsNone(live_runtime.delegate_cmd("hub.py", [], self.code))

    def test_python_exe_is_swapped_for_the_windowless_pythonw(self):
        exe = Path(self._tmp.name) / "python.exe"
        exe.write_bytes(b"MZ")
        (Path(self._tmp.name) / "pythonw.exe").write_bytes(b"MZ")
        got = live_runtime.record_python(self.live, exe)
        self.assertEqual("pythonw.exe", got.name)

    def test_reading_a_stale_python_exe_note_still_uses_pythonw(self):
        """旧笔记写的是 python.exe 时，读出来也必须换成 pythonw，不能只在写入时换。"""
        exe = Path(self._tmp.name) / "python.exe"
        exe.write_bytes(b"MZ")
        (Path(self._tmp.name) / "pythonw.exe").write_bytes(b"MZ")
        note = self.code / live_runtime.PYTHON_NAME
        note.write_text(str(exe), encoding="utf-8")
        got = live_runtime.live_python(self.live)
        self.assertEqual("pythonw.exe", got.name)

    def test_a_live_layout_counts_as_the_live_install(self):
        self.assertTrue(live_runtime.is_live_install(self.code))
        self.assertFalse(live_runtime.is_live_install(self.packed))

    def test_autostart_from_the_live_copy_uses_pythonw_not_the_exe(self):
        live_runtime.record_python(self.live, self.py)
        cmds = live_runtime.autostart_commands(self.code)
        self.assertIsNotNone(cmds)
        hub_cmd, wd_cmd = cmds
        self.assertIn("pythonw.exe", hub_cmd.lower())
        self.assertIn(str(self.py), hub_cmd)
        self.assertIn("hub.py", hub_cmd)
        self.assertIn("--daemon", hub_cmd)
        self.assertIn("--autostart", hub_cmd)
        self.assertIn("watchdog.py", wd_cmd)
        self.assertNotIn("--autostart", wd_cmd)
        self.assertNotIn("--run", hub_cmd)

    def test_autostart_from_the_package_prefers_the_live_pythonw(self):
        live_runtime.record_python(self.live, self.py)
        cmds = live_runtime.autostart_commands(self.packed)
        self.assertIsNotNone(cmds)
        self.assertIn(str(self.code / "hub.py"), cmds[0])
        self.assertIn(str(self.py), cmds[0])
        self.assertIn("--autostart", cmds[0])
        self.assertNotIn("--run", cmds[0])

    def test_source_tree_must_not_rewrite_autostart(self):
        # 源码仓：没有旁边的 state/，autostart_commands 必须是 None
        src = Path(self._tmp.name) / "repo" / live_runtime.CODE_SUBDIR
        src.mkdir(parents=True)
        for n in live_runtime._REQUIRED:
            (src / n).write_text("#", encoding="utf-8")
        self.assertFalse(live_runtime.is_live_install(src))
        self.assertIsNone(live_runtime.autostart_commands(src))

    def test_recording_a_python_that_is_not_there_records_nothing(self):
        self.assertIsNone(live_runtime.record_python(
            self.live, Path(self._tmp.name) / "查无此人.exe"))

    def test_materialise_never_carries_a_stale_interpreter_note_across(self):
        src = Path(self._tmp.name) / "src"
        src.mkdir()
        for n in live_runtime._REQUIRED:
            (src / n).write_text("# " + n, encoding="utf-8")
        (src / live_runtime.PYTHON_NAME).write_text("C:\\别人机器的\\pythonw.exe",
                                                    encoding="utf-8")
        live_runtime.record_python(self.live, self.py)
        live_runtime.materialize(src, self.live)
        self.assertEqual(self.py, live_runtime.live_python(self.live))


class HandOverTests(unittest.TestCase):
    """让位这一下最怕的不是失败，是「让了位、对面又没起来」——那样谁都不跑。"""

    def setUp(self):
        # 让位成功后我们**故意**不 wait 那个子进程（调用方随即退出），Python 会为此
        # 提醒「subprocess N is still running」。这里那正是预期行为，别让它一屏一屏
        # 地盖住真正的失败。
        self.enterContext(warnings.catch_warnings())
        warnings.filterwarnings("ignore", category=ResourceWarning)
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.packed = root / "packed"
        self.packed.mkdir()
        self.live = root / "live"
        self.code = self.live / live_runtime.CODE_SUBDIR
        self.code.mkdir(parents=True)
        (self.live / live_runtime.STATE_SUBDIR).mkdir()
        for n in live_runtime._REQUIRED:
            (self.code / n).write_text("", encoding="utf-8")
        (self.packed / live_runtime.POINTER_NAME).write_text(str(self.live),
                                                             encoding="utf-8")
        live_runtime.record_python(self.live, Path(sys.executable))
        os.environ.pop("CHIJIU_LIVE_DIR", None)
        self.addCleanup(lambda: os.environ.pop("CHIJIU_LIVE_DIR", None))
        # 被拉起的替身把常驻区当 cwd 占着，不先收掉它，临时目录删不动
        self.pidfile = root / "spawned-pids.txt"
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._reap)

    def _reap(self):
        try:
            pids = self.pidfile.read_text(encoding="utf-8").split()
        except OSError:
            return
        for pid in pids:
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True,
                           creationflags=0x08000000)
        time.sleep(0.4)

    def _script(self, name, body):
        (self.code / name).write_text(
            "import os\n"
            "open(r'%s', 'a', encoding='utf-8').write(str(os.getpid()) + '\\n')\n"
            % self.pidfile + body, encoding="utf-8")

    def test_a_live_copy_that_keeps_running_takes_the_job(self):
        self._script("hub.py", "import time\ntime.sleep(30)\n")
        self.assertTrue(live_runtime.hand_over("hub.py", [], self.packed,
                                               settle_secs=1.5))

    def test_a_live_copy_that_dies_on_the_spot_gives_the_job_back(self):
        # 少个依赖 / 解释器不对 / ImportError：让了位又没人跑，比不让位坏得多
        self._script("hub.py", "raise SystemExit(1)\n")
        self.assertFalse(live_runtime.hand_over("hub.py", [], self.packed,
                                                settle_secs=2.0))

    def test_arguments_are_carried_across(self):
        out = self.code / "seen.txt"
        self._script("server.py",
                     "import sys, time\n"
                     "open(r'%s','w',encoding='utf-8').write(' '.join(sys.argv[1:]))\n"
                     "time.sleep(30)\n" % out)
        self.assertTrue(live_runtime.hand_over("server.py", ["--http", "39222"],
                                               self.packed, settle_secs=1.5))
        self.assertEqual("--http 39222", out.read_text(encoding="utf-8"))

    def test_no_live_copy_means_carry_on_where_you_are(self):
        (self.packed / live_runtime.POINTER_NAME).unlink()
        self.assertFalse(live_runtime.hand_over("hub.py", [], self.packed))

    def test_the_live_copy_never_hands_the_job_to_itself(self):
        self._script("hub.py", "import time\ntime.sleep(30)\n")
        self.assertFalse(live_runtime.hand_over("hub.py", [], self.code))


class ConsoleRootTests(unittest.TestCase):
    """常驻区脱离了 rxyy tools 的目录树，「那一侧在哪」只能记不能推。

    仓里有六处直接拿 APP_DIR.parent 当那个根（打包版 = exe 旁，源码态 = 仓库根，
    两条一直都成立）。08-07 16:26 切到常驻区之后两条同时失效，投递站 /tasks 当场
    回「任务库不可用」——库里 6 条任务一条都读不出来，同事提的需求也进不来。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.repo = root / "cursor工作流"
        (self.repo / "console" / "api" / "taskstage").mkdir(parents=True)
        self.src = self.repo / "rxyy_mcp"
        self.src.mkdir()
        for n in live_runtime._REQUIRED:
            (self.src / n).write_text("# " + n, encoding="utf-8")
        self.live = root / "live"

    def test_the_live_copy_is_told_where_rxyy_tools_lives(self):
        live_runtime.materialize(self.src, self.live, self.repo)
        self.assertEqual(self.repo,
                         live_runtime.console_root(self.live / live_runtime.CODE_SUBDIR))

    def test_not_recording_it_is_not_an_error_just_unknown(self):
        # 同事拿到的拷贝没有常驻区，谁都不该因为少一个指针而崩
        live_runtime.materialize(self.src, self.live)
        self.assertIsNone(
            live_runtime.console_root(self.live / live_runtime.CODE_SUBDIR))

    def test_a_root_that_has_since_moved_away_reads_as_unknown(self):
        # 宁可答「不知道」让调用方走自己的兜底，也别把一个不存在的路径递出去
        live_runtime.materialize(self.src, self.live, self.repo)
        shutil.rmtree(self.repo)
        self.assertIsNone(
            live_runtime.console_root(self.live / live_runtime.CODE_SUBDIR))

    def test_a_stale_note_from_another_machine_is_never_carried_across(self):
        (self.src / live_runtime.CONSOLE_ROOT_NAME).write_text(
            "D:\\别人机器的\\cursor工作流", encoding="utf-8")
        live_runtime.materialize(self.src, self.live, self.repo)
        self.assertEqual(self.repo,
                         live_runtime.console_root(self.live / live_runtime.CODE_SUBDIR))


class SmokeImportTests(unittest.TestCase):
    """物化完之后，常驻区那份真的能被 import 起来吗。

    这道闸买的是「不再有悄悄的降级」：少一个依赖时，没有它的表现是每次启动白拉一个
    三秒就死的子进程、然后照旧在包里跑，看上去一切正常——而你以为已经切过去了。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.live = Path(self._tmp.name) / "live"
        self.code = self.live / live_runtime.CODE_SUBDIR
        self.code.mkdir(parents=True)
        for n in live_runtime._REQUIRED:
            (self.code / n).write_text("", encoding="utf-8")
        live_runtime.record_python(self.live, Path(sys.executable))

    def _hub(self, body):
        (self.code / "hub.py").write_text(body, encoding="utf-8")

    def test_a_copy_that_imports_cleanly_passes(self):
        ok, why = live_runtime.smoke_import(self.live)
        self.assertTrue(ok, why)

    def test_a_missing_dependency_is_reported_instead_of_silently_tolerated(self):
        self._hub("import 这个包并不存在\n")
        ok, why = live_runtime.smoke_import(self.live)
        self.assertFalse(ok)
        self.assertIn("import", why)

    def test_the_probe_does_not_leave_the_spawn_gate_shut_behind_it(self):
        # hub 在模块级就 touch 这把闸；留着它，之后 20 秒里看门狗都不会去救 hub
        self._hub("import pathlib\n"
                  "pathlib.Path('.hub-spawn.lock').touch()\n")
        self.assertTrue(live_runtime.smoke_import(self.live)[0])
        self.assertFalse((self.code / ".hub-spawn.lock").exists())

    def test_a_spawn_gate_that_was_already_held_is_left_alone(self):
        # 真有个 hub 正在起来时，闸是它的，自检不许替它开
        (self.code / ".hub-spawn.lock").touch()
        self.assertTrue(live_runtime.smoke_import(self.live)[0])
        self.assertTrue((self.code / ".hub-spawn.lock").exists())

    def test_the_boot_line_the_probe_writes_is_marked_as_not_a_real_boot(self):
        # 不标一下，日志里就是一次「启动到一半死掉的 hub」——而每次换装都埋一条
        self._hub("open('hub-run.log', 'a', encoding='utf-8')"
                  ".write('[boot] hub 进程已创建\\n')\n")
        self.assertTrue(live_runtime.smoke_import(self.live)[0])
        self.assertIn("不是一次真的启动",
                      (self.code / "hub-run.log").read_text(encoding="utf-8"))


class LiveCopyStillFindsRxyyToolsTests(unittest.TestCase):
    """从常驻区里真跑一次：它还找不找得到 rxyy tools 那一侧（`data\\` / `console\\`）。

    这个测试补的是一个方法上的口子，不是某一行代码的口子。08-07 16:26 切到常驻区那天，
    live_runtime 的 47 个测试全绿，投递站却当场读不到任务库——库里 6 条任务一条都出不来，
    同事提的需求进不来，持续到 17:05。**全绿是因为没有一个测试真的从常驻区里跑过一次**：
    每一个都在测「物化对不对」「指针读得对不对」，没有一个把物化出来的那份当真启动起来。

    所以这里刻意用真的 `rxyy_mcp/` 物化，再开子进程从常驻区里 import，走完整条
    「找 console/api/taskstage/storage.py → 加载 → 认任务库目录」的链路。
    """

    def test_the_task_store_still_resolves_when_running_from_the_live_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 假的 rxyy tools 那一侧：常驻区从自己位置推不出它，只能靠物化时记下的
            repo = root / "cursor工作流"
            (repo / "console" / "api" / "taskstage").mkdir(parents=True)
            (repo / "console" / "api" / "taskstage" / "storage.py").write_text(
                "class TaskStageStorage:\n"
                "    def __init__(self, root):\n"
                "        self.root = root\n", encoding="utf-8")
            stage = repo / "data" / "taskstage"
            stage.mkdir(parents=True)
            (stage / ".task_stage.json").write_text("{}", encoding="utf-8")

            live = root / "live"
            live_runtime.materialize(APP_DIR, live, repo)
            code = live / live_runtime.CODE_SUBDIR

            probe = "\n".join([
                "import sys",
                "sys.path.insert(0, r'%s')" % code,
                "import share_server as s",
                "print('root=' + str(s._console_root()))",
                "print('stage=' + str(s._taskstage_dir()))",
                "print('store=' + str(s._task_storage() is not None))",
            ])
            r = subprocess.run([sys.executable, "-X", "utf8", "-c", probe],
                               cwd=str(code), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=180)
            self.assertEqual(0, r.returncode, r.stderr)
            self.assertIn("root=" + str(repo), r.stdout, r.stderr)
            self.assertIn("stage=" + str(stage), r.stdout, r.stderr)
            # 这一行就是 08-07 那次事故的形状：它当时是 False
            self.assertIn("store=True", r.stdout, r.stderr)


class EntryPointWiringTests(unittest.TestCase):
    """三个入口是不是真问过「这活归不归常驻区」。

    机制层写得再对，没人调用就等于没做。而这三行日后极容易在重构入口时被顺手删掉，
    删了当场什么都不会报——只会让换装重新开始杀 MCP，且要等下一次换装才看得出来。
    所以直接扫源码，不靠人记得。
    """

    def _main_block(self, filename):
        tree = ast.parse((APP_DIR / filename).read_text(encoding="utf-8"))
        for node in tree.body:
            if (isinstance(node, ast.If)
                    and ast.unparse(node.test) == "__name__ == '__main__'"):
                return node
        self.fail("{} 里找不到 __main__ 入口".format(filename))

    @staticmethod
    def _hands_over(node):
        return any(isinstance(n, ast.Call)
                   and ast.unparse(n.func) == "live_runtime.hand_over"
                   for n in ast.walk(node))

    def test_the_hub_asks_before_it_does_anything(self):
        self.assertTrue(self._hands_over(self._main_block("hub.py")))

    def test_the_watchdog_asks(self):
        self.assertTrue(self._hands_over(self._main_block("watchdog.py")))

    def test_the_mcp_daemon_asks(self):
        self.assertTrue(self._hands_over(self._main_block("server.py")))

    def test_the_stdio_mcp_is_never_handed_over(self):
        """stdio 那条路的 stdin/stdout 就是 MCP 的传输通道本身。

        让位是拉一个脱离了这两个管道的子进程——客户端会永远等不到回应，而且看不出
        是谁的错。本机走 http://127.0.0.1:39222/mcp 不受影响，但同事那边可能还是
        stdio，所以这道界必须钉住。
        """
        for node in ast.walk(self._main_block("server.py")):
            if isinstance(node, ast.If) and "--http" in ast.unparse(node.test):
                self.assertTrue(any(self._hands_over(s) for s in node.body),
                                "守护进程这条路应该让位")
                self.assertFalse(any(self._hands_over(s) for s in node.orelse),
                                 "stdio 那条路让了位，客户端会永远挂着")
                return
        self.fail("server.py 的入口里找不到 --http 分支")


class PointerWritingTests(unittest.TestCase):
    def test_the_package_learns_where_the_live_copy_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            packed = Path(tmp) / "packed"
            packed.mkdir()
            live = Path(tmp) / "live"
            live_runtime.write_pointer(packed, live)
            self.assertEqual(
                live, Path((packed / live_runtime.POINTER_NAME).read_text(
                    encoding="utf-8").strip().splitlines()[-1]))


if __name__ == "__main__":
    unittest.main()
