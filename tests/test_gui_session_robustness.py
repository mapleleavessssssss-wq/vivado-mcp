"""GUI 会话:真实 pid、工作目录、弹窗、仿真日志、退出诊断。

穿透 session 层:用真实 TCP 连接接假 server,只假 spawn 和 Win32。
不启动 Vivado,不操作真实窗口,不用 9999 端口。
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_probe_then_attach import _serve_length_prefix
from vivado_mcp import server as server_module
from vivado_mcp.analysis.warning_parser import parse_launch_scripts_output
from vivado_mcp.server import AppContext
from vivado_mcp.tcl_scripts import LAUNCH_SCRIPTS_AND_GLOB
from vivado_mcp.tools import diagnostic_tools, session_tools, tcl_tools
from vivado_mcp.vivado import gui_session, win32_dialogs
from vivado_mcp.vivado import session_manager as sm_module
from vivado_mcp.vivado.gui_session import (
    _SIM_COMMAND_RE,
    GuiSession,
    _read_log_delta,
    format_death_report,
)
from vivado_mcp.vivado.session_manager import SessionManager
from vivado_mcp.vivado.win32_dialogs import VivadoWindow

VIVADO_PID = 4321
LAUNCHER_PID = 900001
DIALOG = "Unable to Open File"
LOGGER = "vivado_mcp.vivado.gui_session"


def _resp(output: str, rc: int = 0) -> bytes:
    return json.dumps({"rc": rc, "output": output}, ensure_ascii=False).encode("utf-8")


class FakeVivado:
    """假 Tcl server 的请求处理。每个连接一个线程,和真实 server 一样能接短连接。"""

    def __init__(self, pid: int | None = VIVADO_PID, cwd: str | None = None):
        self.pid = pid
        self.cwd = cwd
        self.commands: list[str] = []
        self.exit_received = False
        # 用户命令的钩子,在 server 线程里跑。返回 None = 断开连接。
        self.on_command = lambda text: _resp(f"OK:{text}")

    def __call__(self, req: bytes) -> bytes | None:
        text = req.decode("utf-8", errors="replace")
        if "VMCP_PROC_PID" in text:
            if self.pid is None:
                return None
            out = f"VMCP_PROC_PID:{self.pid}\n"
            if self.cwd is not None:
                out += f"VMCP_PROC_CWD:{self.cwd}\n"
            return _resp(out)
        if "VMCP_CURPROJ" in text:
            return _resp("VMCP_CURPROJ:\n")
        if text.startswith("puts VMCP_PROBE_"):
            return _resp(text)
        if text == "exit":
            self.exit_received = True
            return _resp("")
        self.commands.append(text)
        return self.on_command(text)


class FakeProc:
    """假外层进程。wait 默认立刻结束,stop 不会走到真实 taskkill。"""

    def __init__(self, hang: bool = False):
        self.pid = LAUNCHER_PID
        self.returncode: int | None = None
        self.hang = hang

    async def wait(self):
        if self.hang:
            await asyncio.Event().wait()
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self):
        self.returncode = -9


@pytest.fixture
def env(monkeypatch, tmp_path):
    """日志和工作目录都指到 tmp_path;taskkill 被拦下,绝不碰真实进程。"""
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    monkeypatch.setenv("VIVADO_MCP_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    kills: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, *a, **kw: kills.append(cmd))
    return SimpleNamespace(tmp=tmp_path, temp_root=temp_root, kills=kills)


@pytest.fixture
async def harness(env, monkeypatch):
    """启动工厂。结束时停掉所有会话和假 server。"""
    sessions: list[GuiSession] = []
    stops: list[threading.Event] = []

    def serve(fake: FakeVivado) -> tuple[int, threading.Event]:
        port, stop, _ = _serve_length_prefix(fake)
        stops.append(stop)
        return port, stop

    async def spawn(
        fake: FakeVivado | None = None,
        proc: FakeProc | None = None,
        session_id: str = "s",
        vivado_path: str = "/fake/vivado",
    ) -> SimpleNamespace:
        fake = fake or FakeVivado()
        proc = proc or FakeProc()
        port, stop = serve(fake)
        spawned: dict = {}

        async def fake_exec(*args, **kwargs):
            spawned.update(args=args, kwargs=kwargs)
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        sess = GuiSession(vivado_path=vivado_path, session_id=session_id, port=0)
        monkeypatch.setattr(sess, "_alloc_free_port", lambda: port)
        sessions.append(sess)
        banner = await sess.start(timeout=5.0)
        return SimpleNamespace(
            sess=sess, fake=fake, proc=proc, banner=banner, spawned=spawned, stop=stop, port=port
        )

    async def attach(fake: FakeVivado | None = None, attach_only: bool = True):
        fake = fake or FakeVivado()
        port, stop = serve(fake)
        sess = GuiSession(
            vivado_path="/fake/vivado", session_id="a", port=port, attach_only=attach_only
        )
        sessions.append(sess)
        banner = await sess.start(timeout=5.0)
        return SimpleNamespace(sess=sess, fake=fake, banner=banner, stop=stop, port=port)

    yield SimpleNamespace(spawn=spawn, attach=attach, serve=serve)

    for sess in sessions:
        await sess.stop()
    for stop in stops:
        stop.set()


class FakeWin32:
    """替换 win32_dialogs 的三个函数。windows 由测试和假 server 线程增减。"""

    def __init__(self):
        self.windows: dict[int, str] = {}
        self.escapes: list[int] = []
        self.escape_args: list[tuple[int, int, str]] = []
        self.queried_pids: set[int] = set()
        self.closes_on_escape = True
        self.post_succeeds = True
        self.closed = threading.Event()

    def list_dialogs(self, pid: int) -> list[VivadoWindow]:
        self.queried_pids.add(pid)
        return [VivadoWindow(h, t, "SunAwtDialog") for h, t in list(self.windows.items())]

    def main_window_disabled(self, pid: int) -> bool:
        return bool(self.windows)

    def post_escape(self, hwnd: int, pid: int, title: str) -> bool:
        self.escape_args.append((hwnd, pid, title))
        if not self.post_succeeds:
            return False
        self.escapes.append(hwnd)
        if self.closes_on_escape:
            self.windows.pop(hwnd, None)
            if not self.windows:
                self.closed.set()
        return True


@pytest.fixture
def win32(monkeypatch):
    """假 Windows 平台,巡检间隔缩短到毫秒级。"""
    fake = FakeWin32()
    monkeypatch.setattr(win32_dialogs, "SUPPORTED", True)
    monkeypatch.setattr(win32_dialogs, "list_dialogs", fake.list_dialogs)
    monkeypatch.setattr(win32_dialogs, "main_window_disabled", fake.main_window_disabled)
    monkeypatch.setattr(win32_dialogs, "post_escape", fake.post_escape)
    monkeypatch.setattr(gui_session, "_DIALOG_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(gui_session, "_DIALOG_MIN_AGE", 0.01)
    monkeypatch.setattr(gui_session, "_DIALOG_RETRY_INTERVAL", 0.02)
    return fake


async def _wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "等待条件超时"
        await asyncio.sleep(0.005)


def _ctx(manager: SessionManager):
    return SimpleNamespace(
        request_context=SimpleNamespace(lifespan_context=AppContext(session_manager=manager))
    )


# --------------------------------------------------------------------------- #
#  真实 pid(缺陷 5)
# --------------------------------------------------------------------------- #


class TestVivadoPid:
    async def test_status_replaces_pid_with_vivado_and_launcher(self, harness):
        h = await harness.spawn()
        status = h.sess.status_dict()
        assert status["vivado_pid"] == VIVADO_PID
        assert status["launcher_pid"] == LAUNCHER_PID
        assert "pid" not in status
        assert h.sess.pid == LAUNCHER_PID, "pid 属性保留,返回外层进程"

    async def test_attach_has_only_vivado_pid(self, harness):
        h = await harness.attach()
        status = h.sess.status_dict()
        assert status["vivado_pid"] == VIVADO_PID
        assert "launcher_pid" not in status

    async def test_query_failure_keeps_start_and_is_visible(self, harness, win32, caplog):
        """AC6:查询失败时启动成功,vivado_pid 为空,不启用巡检,原因可见。"""
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            h = await harness.spawn(FakeVivado(pid=None))
        assert h.sess.is_alive
        assert h.sess.status_dict()["vivado_pid"] is None
        assert "[DEGRADED]" in h.banner
        assert "vivado_pid" in h.banner
        assert "读响应头失败" in caplog.text

        result = await h.sess.execute("puts x", timeout=2.0)
        assert "OK:puts x" in result.output
        assert h.sess._dialog_watcher is None
        assert win32.escapes == []

    async def test_query_output_without_marker_degrades(self, harness):
        fake = FakeVivado()
        original = fake.__call__

        class NoMarker(FakeVivado):
            def __call__(self, req):
                if b"VMCP_PROC_PID" in req:
                    return _resp("garbage")
                return original(req)

        h = await harness.spawn(NoMarker())
        assert h.sess.status_dict()["vivado_pid"] is None
        assert "VMCP_PROC_PID" in h.banner

    async def test_query_uses_short_connection_not_main(self, harness):
        """进程查询不能出现在主连接上(B16 同类教训:迟到响应会让结果错位)。"""
        h = await harness.spawn()
        result = await h.sess.execute("puts first", timeout=2.0)
        assert result.output == "OK:puts first"
        assert h.fake.commands == ["puts first"]

    async def test_stop_kills_tree_by_launcher_pid(self, harness, env):
        """stop 仍按 launcher_pid 杀进程树,不是 vivado_pid。"""
        proc = FakeProc(hang=True)
        h = await harness.spawn(proc=proc)

        real_wait_for = asyncio.wait_for

        async def fast_wait_for(awaitable, timeout):
            return await real_wait_for(awaitable, min(timeout, 0.05))

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(gui_session.asyncio, "wait_for", fast_wait_for)
            await h.sess.stop()

        if gui_session.os.name == "nt":
            assert env.kills and str(LAUNCHER_PID) in env.kills[0]
            assert "/T" in env.kills[0]
            assert str(VIVADO_PID) not in env.kills[0]
        else:
            assert proc.returncode == -9


# --------------------------------------------------------------------------- #
#  工作目录(缺陷 3)
# --------------------------------------------------------------------------- #


class TestWorkdir:
    async def test_spawn_gets_own_directory(self, harness, env):
        h = await harness.spawn(session_id="my-sess")
        cwd = Path(h.spawned["kwargs"]["cwd"])
        assert cwd.parent == env.temp_root.resolve() / "vivado-mcp" / "sessions"
        assert cwd.name.startswith("my-sess_")
        assert cwd.is_dir()
        assert str(cwd) in h.banner
        assert h.sess.status_dict()["cwd"] == str(cwd)

    async def test_two_sessions_get_different_directories(self, harness):
        first = await harness.spawn()
        second = await harness.spawn()
        assert first.spawned["kwargs"]["cwd"] != second.spawned["kwargs"]["cwd"]

    async def test_status_prefers_tcl_pwd(self, harness):
        h = await harness.spawn(FakeVivado(cwd="C:/from/tcl/pwd"))
        assert h.sess.status_dict()["cwd"] == "C:/from/tcl/pwd"
        assert "C:/from/tcl/pwd" in h.banner

    async def test_attach_reports_queried_pwd(self, harness):
        h = await harness.attach(FakeVivado(cwd="C:/user/dir"))
        assert h.sess.status_dict()["cwd"] == "C:/user/dir"
        assert "C:/user/dir" in h.banner

    async def test_non_ascii_temp_falls_back(self, harness, env, monkeypatch, caplog):
        chinese = env.tmp / "临时目录"
        chinese.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(chinese))
        monkeypatch.chdir(env.tmp)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            h = await harness.spawn()
        assert h.spawned["kwargs"]["cwd"] is None, "退回继承调用方目录"
        assert "未使用独立工作目录" in h.banner
        assert "非 ASCII" in h.banner
        assert "非 ASCII" in caplog.text
        assert h.sess.status_dict()["cwd"] == str(Path.cwd())
        assert not (chinese / "vivado-mcp" / "sessions").exists()

    async def test_mkdir_failure_falls_back(self, harness, env, monkeypatch, caplog):
        real_mkdir = Path.mkdir

        def fail_sessions(self, *args, **kwargs):
            if "sessions" in self.parts:
                raise PermissionError("no write access (test)")
            return real_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", fail_sessions)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            h = await harness.spawn()
        assert h.spawned["kwargs"]["cwd"] is None
        assert "no write access (test)" in h.banner
        assert "no write access (test)" in caplog.text

    async def test_relative_vivado_path_made_absolute(self, harness, env, monkeypatch):
        monkeypatch.chdir(env.tmp)
        h = await harness.spawn(vivado_path="bin/vivado")
        assert h.spawned["args"][0] == str(env.tmp.resolve() / "bin" / "vivado")
        assert h.sess.vivado_path == "bin/vivado", "展示用的路径不变"

    async def test_bare_vivado_name_stays_for_path_lookup(self, harness):
        h = await harness.spawn(vivado_path="vivado")
        assert h.spawned["args"][0] == "vivado"

    async def test_attach_creates_no_directory(self, harness, env):
        await harness.attach()
        assert not (env.temp_root / "vivado-mcp").exists()


class TestStopCleanup:
    @staticmethod
    def _populate(cwd: Path) -> None:
        (cwd / f"vivado_pid{VIVADO_PID}.str").write_text("mine")
        (cwd / "vivado_pid999.str").write_text("other instance")
        (cwd / ".Xil").mkdir()
        (cwd / ".Xil" / "tmp.txt").write_text("x")
        (cwd / "user_output.txt").write_text("keep")

    async def test_normal_stop_removes_only_this_instance(self, harness):
        h = await harness.spawn()
        cwd = Path(h.spawned["kwargs"]["cwd"])
        self._populate(cwd)
        await h.sess.stop()
        assert not (cwd / f"vivado_pid{VIVADO_PID}.str").exists()
        assert (cwd / "vivado_pid999.str").exists(), "不能删其他实例的 .str"
        assert not (cwd / ".Xil").exists()
        assert (cwd / "user_output.txt").read_text() == "keep"

    async def test_normal_stop_removes_empty_directory(self, harness):
        h = await harness.spawn()
        cwd = Path(h.spawned["kwargs"]["cwd"])
        (cwd / f"vivado_pid{VIVADO_PID}.str").write_text("mine")
        (cwd / ".Xil").mkdir()
        await h.sess.stop()
        assert not cwd.exists()

    async def test_unknown_pid_keeps_str(self, harness):
        h = await harness.spawn(FakeVivado(pid=None))
        cwd = Path(h.spawned["kwargs"]["cwd"])
        (cwd / "vivado_pid123.str").write_text("whose?")
        (cwd / ".Xil").mkdir()
        await h.sess.stop()
        assert (cwd / "vivado_pid123.str").exists()
        assert not (cwd / ".Xil").exists()

    async def test_unexpected_exit_keeps_evidence(self, harness):
        h = await harness.spawn()
        cwd = Path(h.spawned["kwargs"]["cwd"])
        self._populate(cwd)
        (cwd / f"hs_err_pid{VIVADO_PID}.log").write_text("EXCEPTION_ACCESS_VIOLATION")
        h.stop.set()
        await _wait_until(lambda: not h.sess.is_alive)
        await h.sess.stop()
        assert (cwd / f"vivado_pid{VIVADO_PID}.str").exists()
        assert (cwd / ".Xil").exists()
        assert (cwd / f"hs_err_pid{VIVADO_PID}.log").exists()

    async def test_launcher_gone_but_connection_alive_still_asks_vivado_to_exit(self, harness):
        """外层进程已退但连接还通:Vivado 还活着,要发 exit,否则成孤儿;文件不删。"""
        h = await harness.spawn()
        cwd = Path(h.spawned["kwargs"]["cwd"])
        (cwd / f"vivado_pid{VIVADO_PID}.str").write_text("x")
        h.proc.returncode = 0
        await h.sess.stop()
        assert h.fake.exit_received
        assert (cwd / f"vivado_pid{VIVADO_PID}.str").exists()

    async def test_attach_deletes_nothing(self, harness, env, monkeypatch):
        monkeypatch.chdir(env.tmp)
        (env.tmp / f"vivado_pid{VIVADO_PID}.str").write_text("user's")
        h = await harness.attach(FakeVivado(cwd=str(env.tmp)))
        await h.sess.stop()
        assert (env.tmp / f"vivado_pid{VIVADO_PID}.str").exists()

    async def test_inherited_directory_removes_only_own_str(self, harness, env, monkeypatch):
        chinese = env.tmp / "临时目录"
        chinese.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(chinese))
        inherited = env.tmp / "project"
        inherited.mkdir()
        monkeypatch.chdir(inherited)
        h = await harness.spawn()
        self._populate(inherited)
        await h.sess.stop()
        assert not (inherited / f"vivado_pid{VIVADO_PID}.str").exists()
        assert (inherited / "vivado_pid999.str").exists()
        assert (inherited / ".Xil").exists(), "继承目录可能有别的实例在用 .Xil"

    async def test_unexpected_exit_rmdir_failure_warns(self, harness, monkeypatch, caplog):
        """目录非空是保留证据(info);其他原因删不掉要 warning。"""
        h = await harness.spawn()
        h.proc.returncode = 1

        def denied(path):
            raise PermissionError(13, "denied", str(path))

        monkeypatch.setattr(gui_session.os, "rmdir", denied)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            await h.sess.stop()
        assert "denied" in caplog.text

    async def test_unexpected_exit_with_evidence_is_quiet(self, harness, caplog):
        h = await harness.spawn()
        cwd = Path(h.sess.status_dict()["cwd"])
        (cwd / "hs_err_pid1.log").write_text("x")
        h.proc.returncode = 1
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            await h.sess.stop()
        assert "保留工作目录" not in caplog.text
        assert (cwd / "hs_err_pid1.log").exists()

    async def test_cleanup_failure_warns_and_stop_completes(self, harness, monkeypatch, caplog):
        h = await harness.spawn()
        cwd = Path(h.spawned["kwargs"]["cwd"])
        (cwd / ".Xil").mkdir()

        def boom(path):
            raise PermissionError("locked by xsim (test)")

        monkeypatch.setattr(gui_session.shutil, "rmtree", boom)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            await h.sess.stop()
        assert "locked by xsim (test)" in caplog.text
        assert (cwd / ".Xil").exists()


class TestAsciiCheck:
    def test_checks_actual_cwd_not_mcp_directory(self):
        warn = session_tools._check_ascii_paths("C:/Xilinx/vivado.bat", "D:/中文/目录")
        assert "D:/中文/目录" in warn

    def test_ascii_actual_cwd_ignores_mcp_directory(self, monkeypatch, env):
        chinese = env.tmp / "中文工程"
        chinese.mkdir()
        monkeypatch.chdir(chinese)
        assert session_tools._check_ascii_paths("C:/Xilinx/vivado.bat", "C:/ascii") == ""

    def test_without_cwd_falls_back_to_process_directory(self, monkeypatch, env):
        """Tcl 模式不报 cwd,Vivado 继承 MCP 进程目录。"""
        chinese = env.tmp / "中文工程"
        chinese.mkdir()
        monkeypatch.chdir(chinese)
        assert "中文工程" in session_tools._check_ascii_paths("C:/Xilinx/vivado.bat")


async def test_tcl_mode_spawn_does_not_set_cwd(monkeypatch):
    """R3.6:Tcl 模式不改工作目录。"""
    from vivado_mcp.vivado.session import SubprocessSession

    captured = {}

    async def fake_spawn(*args, **kwargs):
        captured.update(kwargs)
        raise OSError("spawn blocked in test")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
    with pytest.raises(RuntimeError, match="spawn blocked in test"):
        await SubprocessSession(vivado_path="/fake/vivado", session_id="t").start(timeout=1.0)
    assert captured.get("cwd") is None


# --------------------------------------------------------------------------- #
#  弹窗(缺陷 1)
# --------------------------------------------------------------------------- #


def _blocks_until_closed(win32: FakeWin32, *titles: str):
    """模拟 run:执行中弹窗,弹窗全部关闭后才返回。"""

    def on_command(text):
        for i, title in enumerate(titles):
            win32.windows[1000 + i] = title
        win32.closed.wait(3.0)
        return _resp(f"DONE:{text}")

    return on_command


class TestDialogDismiss:
    async def test_note_on_triggering_command(self, harness, win32):
        h = await harness.spawn()
        h.fake.on_command = _blocks_until_closed(win32, DIALOG, DIALOG)

        result = await h.sess.execute("run all", timeout=5.0)

        assert "DONE:run all" in result.output
        assert sorted(win32.escapes) == [1000, 1001]
        assert "本命令执行期间" in result.output
        assert "2 个" in result.output
        assert "$finish" in result.output
        assert win32.queried_pids == {VIVADO_PID}, "只查本会话的 vivado.exe"

    async def test_count_not_lost_when_command_returns_right_after_close(self, harness, win32):
        """Esc 后命令立刻返回,窗口消失的结算不能因为巡检结束而丢。"""
        h = await harness.spawn()
        h.fake.on_command = _blocks_until_closed(win32, DIALOG)
        result = await h.sess.execute("run all", timeout=5.0)
        assert "1 个" in result.output
        assert h.sess._dismissed_dialogs == 0

    async def test_window_gone_late_is_still_counted(self, harness, win32, monkeypatch):
        """窗口销毁略晚于命令返回时,收尾阶段继续确认。"""
        h = await harness.spawn()
        win32.closes_on_escape = False
        release = threading.Event()

        def on_command(text):
            win32.windows[1] = DIALOG
            release.wait(3.0)
            return _resp("DONE")

        h.fake.on_command = on_command
        task = asyncio.create_task(h.sess.execute("run all", timeout=5.0))
        await _wait_until(lambda: win32.escapes)
        release.set()
        await _wait_until(lambda: h.sess._inflight_task is None)
        win32.windows.clear()
        result = await task
        assert "1 个" in result.output

    async def test_escape_carries_pid_and_title_for_recheck(self, harness, win32):
        """发键前要重新确认窗口(hwnd 可能被复用),所以 pid 和标题必须传到 Win32 层。"""
        h = await harness.spawn()
        h.fake.on_command = _blocks_until_closed(win32, DIALOG)
        await h.sess.execute("run all", timeout=5.0)
        assert win32.escape_args == [(1000, VIVADO_PID, DIALOG)]

    async def test_failed_post_is_not_retried_or_counted(self, harness, win32):
        """发送失败(窗口已换人 / 权限不够):不能每秒重试,也不能算作 MCP 关闭的。"""
        h = await harness.spawn()
        win32.post_succeeds = False
        release = threading.Event()

        def on_command(text):
            win32.windows[1] = DIALOG
            release.wait(3.0)
            win32.windows.clear()
            return _resp("DONE")

        h.fake.on_command = on_command
        task = asyncio.create_task(h.sess.execute("run all", timeout=5.0))
        await _wait_until(lambda: win32.escape_args)
        await asyncio.sleep(0.1)
        release.set()
        result = await task
        assert len(win32.escape_args) == 1
        assert "[弹窗]" not in result.output

    async def test_no_dialog_no_note(self, harness, win32):
        h = await harness.spawn()
        result = await h.sess.execute("puts x", timeout=2.0)
        assert result.output == "OK:puts x"

    @pytest.mark.parametrize(
        "title", ["Background Task", "Synthesis Completed", "unable to open file"]
    )
    async def test_other_titles_not_dismissed(self, harness, win32, title):
        h = await harness.spawn()

        def on_command(text):
            win32.windows[7] = title
            time.sleep(0.1)
            return _resp("DONE")

        h.fake.on_command = on_command
        result = await h.sess.execute("run all", timeout=5.0)
        assert win32.escapes == []
        assert "[弹窗]" not in result.output

    @pytest.mark.parametrize("attach_only", [True, False], ids=["explicit", "probe-hit"])
    async def test_attach_reports_but_never_sends_keys(self, harness, win32, attach_only):
        """R1.3:显式 attach 和 probe 命中都只报告,不发任何按键。"""
        release = threading.Event()
        fake = FakeVivado()

        def on_command(text):
            win32.windows[5] = DIALOG
            release.wait(3.0)
            return _resp("DONE")

        fake.on_command = on_command
        h = await harness.attach(fake, attach_only=attach_only)
        assert h.sess.mode == "attach"
        with pytest.raises(asyncio.TimeoutError) as exc:
            await h.sess.execute("run all", timeout=0.1)
        assert DIALOG in str(exc.value)
        assert "主窗口已被禁用" in str(exc.value)
        assert "attach" in str(exc.value)
        await asyncio.sleep(0.05)
        assert win32.escapes == []
        assert h.sess._dialog_watcher is None
        release.set()

    async def test_gives_up_after_three_escapes(self, harness, win32, caplog):
        h = await harness.spawn()
        win32.closes_on_escape = False
        release = threading.Event()

        def on_command(text):
            win32.windows[9] = DIALOG
            release.wait(3.0)
            return _resp("DONE")

        h.fake.on_command = on_command
        task = asyncio.create_task(h.sess.execute("run all", timeout=5.0))
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            await _wait_until(lambda: "仍未关闭,不再重试" in caplog.text)
        assert win32.escapes == [9, 9, 9]
        win32.windows.clear()
        release.set()
        await task

    async def test_watcher_failure_does_not_affect_command(
        self, harness, win32, monkeypatch, caplog
    ):
        h = await harness.spawn()

        def boom(pid):
            raise ValueError("enum exploded (test)")

        monkeypatch.setattr(win32_dialogs, "list_dialogs", boom)
        h.fake.on_command = lambda text: (time.sleep(0.05), _resp("DONE"))[1]
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            result = await h.sess.execute("run all", timeout=2.0)
        assert result.output == "DONE"
        assert "enum exploded (test)" in caplog.text

    async def test_non_windows_starts_no_watcher(self, harness, win32, monkeypatch):
        monkeypatch.setattr(win32_dialogs, "SUPPORTED", False)
        h = await harness.spawn()
        await h.sess.execute("puts x", timeout=2.0)
        assert h.sess._dialog_watcher is None
        assert win32.queried_pids <= {VIVADO_PID}


class TestDialogTimeoutAttribution:
    async def test_timeout_reports_title_and_main_window(self, harness, win32):
        h = await harness.spawn()
        win32.closes_on_escape = False
        release = threading.Event()

        def on_command(text):
            win32.windows[3] = DIALOG
            release.wait(3.0)
            return _resp("DONE")

        h.fake.on_command = on_command
        with pytest.raises(asyncio.TimeoutError) as exc:
            await h.sess.execute("run all", timeout=0.2)
        message = str(exc.value)
        assert "读取响应长度头超时" in message
        assert DIALOG in message
        assert "主窗口已被禁用" in message

        with pytest.raises(RuntimeError, match="上一条命令仍在执行") as busy:
            await h.sess.execute("puts next", timeout=1.0)
        assert DIALOG in str(busy.value)

        win32.windows.clear()
        release.set()
        await _wait_until(lambda: h.sess._inflight_task is None)

    async def test_other_dialog_advice_says_manual(self, harness, win32):
        h = await harness.spawn()
        release = threading.Event()

        def on_command(text):
            win32.windows[4] = "Background Task"
            release.wait(3.0)
            return _resp("DONE")

        h.fake.on_command = on_command
        with pytest.raises(asyncio.TimeoutError) as exc:
            await h.sess.execute("run all", timeout=0.1)
        assert "Background Task" in str(exc.value)
        assert "手动关闭" in str(exc.value)
        assert win32.escapes == []
        win32.windows.clear()
        release.set()
        await _wait_until(lambda: h.sess._inflight_task is None)

    async def test_watcher_keeps_running_after_timeout_and_note_names_old_command(
        self, harness, win32
    ):
        """超时后才关闭的弹窗:附到下一条命令,但要写清是之前的命令。"""
        h = await harness.spawn()
        release = threading.Event()

        def on_command(text):
            if text == "run all":
                release.wait(3.0)
                return _resp("DONE")
            return _resp(f"OK:{text}")

        h.fake.on_command = on_command
        with pytest.raises(asyncio.TimeoutError) as exc:
            await h.sess.execute("run all", timeout=0.05)
        assert "[弹窗]" not in str(exc.value)

        win32.windows[8] = DIALOG
        await _wait_until(lambda: h.sess._dismissed_dialogs == 1)
        release.set()
        await _wait_until(lambda: h.sess._inflight_task is None)

        result = await h.sess.execute("puts next", timeout=2.0)
        assert "OK:puts next" in result.output
        assert "之前超时的命令(run all)" in result.output
        assert "本命令执行期间" not in result.output

        again = await h.sess.execute("puts again", timeout=2.0)
        assert again.output == "OK:puts again", "说明只附一次"

    async def test_dismissed_before_timeout_goes_in_timeout_error(self, harness, win32):
        h = await harness.spawn()
        release = threading.Event()

        def on_command(text):
            win32.windows[6] = DIALOG
            release.wait(3.0)
            return _resp("DONE")

        h.fake.on_command = on_command
        task = asyncio.create_task(h.sess.execute("run all", timeout=0.3))
        await _wait_until(lambda: h.sess._dismissed_dialogs == 1)
        with pytest.raises(asyncio.TimeoutError) as exc:
            await task
        assert "本命令执行期间" in str(exc.value)
        assert "1 个" in str(exc.value)
        release.set()
        await _wait_until(lambda: h.sess._inflight_task is None)

    async def test_busy_error_reports_dismissal_after_timeout(self, harness, win32):
        h = await harness.spawn()
        release = threading.Event()
        h.fake.on_command = lambda text: (release.wait(3.0), _resp("DONE"))[1]
        with pytest.raises(asyncio.TimeoutError):
            await h.sess.execute("run all", timeout=0.05)
        win32.windows[8] = DIALOG
        await _wait_until(lambda: h.sess._dismissed_dialogs == 1)
        with pytest.raises(RuntimeError, match="仍在执行") as busy:
            await h.sess.execute("puts next", timeout=1.0)
        assert "之前超时的命令(run all)" in str(busy.value)
        release.set()
        await _wait_until(lambda: h.sess._inflight_task is None)


class TestDialogWatcherIsolation:
    async def test_lock_and_inflight_untouched_after_timeout(self, harness, win32):
        """超时后 watcher 还在跑,但锁已释放,在途任务和 reader 只有一份。"""
        h = await harness.spawn()
        release = threading.Event()
        h.fake.on_command = lambda text: (release.wait(3.0), _resp("DONE"))[1]
        with pytest.raises(asyncio.TimeoutError):
            await h.sess.execute("run all", timeout=0.05)
        assert not h.sess._lock.locked()
        assert h.sess._inflight_task is not None
        assert h.sess._pending_response is True
        watcher = h.sess._dialog_watcher
        assert watcher is not None and not watcher.done()

        release.set()
        await _wait_until(lambda: h.sess._inflight_task is None)
        await _wait_until(watcher.done)
        assert h.sess._pending_response is False

    async def test_stop_cancels_watcher_with_inflight_command(self, harness, win32):
        h = await harness.spawn()
        release = threading.Event()
        h.fake.on_command = lambda text: (release.wait(3.0), _resp("DONE"))[1]
        with pytest.raises(asyncio.TimeoutError):
            await h.sess.execute("run all", timeout=0.05)
        watcher = h.sess._dialog_watcher
        inflight = h.sess._inflight_task

        await h.sess.stop()

        assert watcher.cancelled() or watcher.done()
        assert inflight.done()
        assert h.sess._dialog_watcher is None
        pending = asyncio.all_tasks() - {asyncio.current_task()}
        assert not pending, f"stop 后不能留未 await 的任务: {pending}"
        release.set()

    async def test_stop_sends_no_key_to_closing_vivado(self, harness, win32):
        h = await harness.spawn()
        release = threading.Event()
        h.fake.on_command = lambda text: (release.wait(3.0), _resp("DONE"))[1]
        with pytest.raises(asyncio.TimeoutError):
            await h.sess.execute("run all", timeout=0.05)
        await h.sess.stop()
        win32.windows[2] = DIALOG
        await asyncio.sleep(0.05)
        assert win32.escapes == []
        release.set()


class TestExecuteRaces:
    async def _timed_out_with_lingering_watcher(self, h, win32, monkeypatch):
        """制造"上一条超时、已结束,但 watcher 还在收尾"的窗口。"""
        monkeypatch.setattr(gui_session, "_DIALOG_SETTLE_POLLS", 1000)
        monkeypatch.setattr(gui_session, "_DIALOG_SETTLE_INTERVAL", 0.01)
        win32.closes_on_escape = False
        release = threading.Event()

        def first(text):
            win32.windows[1] = DIALOG
            release.wait(3.0)
            return _resp("DONE")

        h.fake.on_command = first
        with pytest.raises(asyncio.TimeoutError):
            await h.sess.execute("run all", timeout=0.05)
        await _wait_until(lambda: win32.escapes)
        release.set()
        await _wait_until(lambda: h.sess._inflight_task is None)

    async def test_stop_during_join_rejects_queued_command(self, harness, win32, monkeypatch):
        """execute 在 join 上让出时 stop 到达:命令不能再发出去,否则 stop 要等它跑完。"""
        h = await harness.spawn()
        await self._timed_out_with_lingering_watcher(h, win32, monkeypatch)
        h.fake.on_command = lambda text: (time.sleep(1.5), _resp("LATE"))[1]

        queued = asyncio.create_task(h.sess.execute("puts queued", timeout=5.0))
        await _wait_until(h.sess._lock.locked)
        started = time.monotonic()
        await h.sess.stop()
        assert time.monotonic() - started < 1.0, "stop 被排队的命令拖住了"

        with pytest.raises(RuntimeError, match="正在关闭"):
            await queued
        assert "puts queued" not in h.fake.commands
        win32.windows.clear()

    async def test_command_waiting_on_lock_after_stop_gets_clear_error(self, harness):
        """排队等锁期间 stop 已完成:要给说得清的错误,不是裸 AssertionError。"""
        h = await harness.spawn()
        release = threading.Event()
        h.fake.on_command = lambda text: (release.wait(3.0), _resp("DONE"))[1]
        first = asyncio.create_task(h.sess.execute("run all", timeout=5.0))
        await _wait_until(lambda: h.fake.commands)
        second = asyncio.create_task(h.sess.execute("puts second", timeout=5.0))
        await asyncio.sleep(0.01)

        await h.sess.stop()
        release.set()

        with pytest.raises(RuntimeError):
            await first
        with pytest.raises(RuntimeError, match="正在关闭"):
            await second

    async def test_cancel_during_wait_leaves_session_usable(self, harness, win32):
        """调用方取消:读任务继续独占这一帧,完成后下一条命令不串帧。"""
        h = await harness.spawn()
        release = threading.Event()
        h.fake.on_command = lambda text: (release.wait(3.0), _resp(f"R:{text}"))[1]

        task = asyncio.create_task(h.sess.execute("run all", timeout=5.0))
        await _wait_until(lambda: h.fake.commands)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert not h.sess._lock.locked()
        assert h.sess._inflight_task is not None
        with pytest.raises(RuntimeError, match="仍在执行"):
            await h.sess.execute("puts blocked", timeout=1.0)

        release.set()
        await _wait_until(lambda: h.sess._inflight_task is None)
        assert h.sess.state.value == "ready"
        result = await h.sess.execute("puts next", timeout=2.0)
        assert result.output == "R:puts next"

    async def test_cancel_during_final_join_is_clean(self, harness, win32, monkeypatch):
        """结果已到、watcher 收尾时被取消:锁放开,watcher 留给下一条 join,计数不丢。"""
        monkeypatch.setattr(gui_session, "_DIALOG_SETTLE_POLLS", 1000)
        monkeypatch.setattr(gui_session, "_DIALOG_SETTLE_INTERVAL", 0.01)
        h = await harness.spawn()
        win32.closes_on_escape = False
        release = threading.Event()

        def first(text):
            win32.windows[1] = DIALOG
            release.wait(3.0)
            return _resp("DONE")

        h.fake.on_command = first
        task = asyncio.create_task(h.sess.execute("run all", timeout=5.0))
        await _wait_until(lambda: win32.escapes)
        release.set()
        await _wait_until(lambda: h.sess._inflight_task is None)
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert not h.sess._lock.locked()
        assert h.sess.state.value == "ready"
        assert h.sess._inflight_task is None
        win32.windows.clear()
        h.fake.on_command = lambda text: _resp(f"R:{text}")
        result = await h.sess.execute("puts next", timeout=2.0)
        assert "R:puts next" in result.output
        assert "1 个" in result.output
        assert "之前超时的命令" in result.output

    async def test_watcher_adds_no_latency_without_dialogs(self, harness, win32, monkeypatch):
        """没有弹窗时,命令一返回 watcher 就结束,不等下一轮巡检。"""
        monkeypatch.setattr(gui_session, "_DIALOG_POLL_INTERVAL", 30.0)
        h = await harness.spawn()
        started = time.monotonic()
        result = await h.sess.execute("puts x", timeout=5.0)
        assert time.monotonic() - started < 5.0
        assert result.output == "OK:puts x"


class TestStatusBlockingDialogs:
    async def test_list_shows_dialogs(self, harness, win32):
        h = await harness.spawn()
        win32.closes_on_escape = False
        assert "blocking_dialogs" not in h.sess.status_dict()
        win32.windows[1] = "Synthesis Completed"
        status = h.sess.status_dict()
        assert status["blocking_dialogs"] == ["Synthesis Completed"]
        assert status["main_window_disabled"] is True
        win32.windows.clear()

    async def test_no_field_without_pid(self, harness, win32):
        h = await harness.spawn(FakeVivado(pid=None))
        win32.windows[1] = DIALOG
        assert "blocking_dialogs" not in h.sess.status_dict()
        win32.windows.clear()


# --------------------------------------------------------------------------- #
#  仿真日志(缺陷 4)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "command",
    [
        "run all",
        "run 10ns",
        "run",
        "catch {run 10ns}",
        "catch { run all }",
        "set x [run 1us]",
        "puts a; run all",
        "puts a\n  run all\n",
        "launch_simulation",
        "launch_simulation -simset sim_1",
        "relaunch_sim",
        "if {[catch {launch_simulation -scripts_only -simset $s} e]} {puts $e}",
    ],
)
def test_sim_regex_matches(command):
    assert _SIM_COMMAND_RE.search(command)


@pytest.mark.parametrize(
    "command",
    [
        "launch_runs impl_1",
        "run_tcl foo",
        "get_runs",
        "[get_runs synth_1]",
        "reset_run synth_1",
        "source x.tcl",
        "puts {running}",
        "# run all",
        'puts "run all"',
        "wait_on_run impl_1",
        "report_timing",
        "open_run impl_1 -name run",
        "foo -run bar",
        "get_runs run",
    ],
)
def test_sim_regex_does_not_match(command):
    assert not _SIM_COMMAND_RE.search(command)


def _appends_log(h, text: bytes, output: str = "RAW"):
    """模拟 Vivado:命令返回前,$display 已落盘到 -log 文件。"""

    def on_command(cmd):
        with h.sess._startup_log_path.open("ab") as log:
            log.write(text)
        return _resp(output)

    return on_command


class TestSimLog:
    async def test_run_appends_only_new_content(self, harness):
        h = await harness.spawn()
        h.sess._startup_log_path.write_bytes(b"OLD STARTUP LINE\n")
        h.fake.on_command = _appends_log(h, b"REPRO_DISPLAY tb_task\n")

        result = await h.sess.execute("run all", timeout=2.0)

        assert result.output.startswith("RAW")
        assert "REPRO_DISPLAY tb_task" in result.output
        assert "OLD STARTUP LINE" not in result.output
        assert "新增 22 字节" in result.output
        assert str(h.sess._startup_log_path) in result.output

    async def test_error_result_also_gets_log_and_keeps_rc(self, harness):
        h = await harness.spawn()

        def on_command(cmd):
            with h.sess._startup_log_path.open("ab") as log:
                log.write(b"ERROR: elaboration failed\n")
            return _resp("run failed", rc=1)

        h.fake.on_command = on_command
        result = await h.sess.execute("run all", timeout=2.0)
        assert result.is_error
        assert result.return_code == 1
        assert "elaboration failed" in result.summary

    @pytest.mark.parametrize("command", ["source x.tcl", "launch_runs impl_1", "get_runs"])
    async def test_other_commands_never_append(self, harness, command):
        h = await harness.spawn()
        h.fake.on_command = _appends_log(h, b"SHOULD NOT APPEAR\n")
        result = await h.sess.execute(command, timeout=2.0)
        assert result.output == "RAW"

    async def test_no_new_content_appends_nothing(self, harness):
        h = await harness.spawn()
        h.sess._startup_log_path.write_bytes(b"OLD\n")
        h.fake.on_command = lambda cmd: _resp("RAW")
        result = await h.sess.execute("run all", timeout=2.0)
        assert result.output == "RAW"

    async def test_log_never_created_appends_nothing(self, harness, caplog):
        """发送前后都没有日志文件:没有新增内容,不是读取失败。"""
        h = await harness.spawn()
        h.fake.on_command = lambda cmd: _resp("RAW")
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            result = await h.sess.execute("run all", timeout=2.0)
        assert result.output == "RAW"
        assert "读取仿真日志" not in caplog.text

    async def test_log_created_during_command_is_read_from_start(self, harness):
        h = await harness.spawn()
        assert not h.sess._startup_log_path.exists()
        h.fake.on_command = _appends_log(h, b"FIRST LINE\n")
        result = await h.sess.execute("run all", timeout=2.0)
        assert "FIRST LINE" in result.output

    async def test_truncated_keeps_tail_and_reports_total(self, harness):
        h = await harness.spawn()
        body = b"HEAD_MARKER\n" + b"x" * 100_000 + b"\nTAIL_MARKER\n"
        h.fake.on_command = _appends_log(h, body)
        result = await h.sess.execute("run all", timeout=2.0)
        assert "TAIL_MARKER" in result.output
        assert "HEAD_MARKER" not in result.output
        assert f"新增 {len(body)} 字节" in result.output
        assert "已截断" in result.output
        assert str(h.sess._startup_log_path) in result.output
        assert len(result.output) < 8000 + 500

    async def test_timeout_error_includes_log(self, harness):
        h = await harness.spawn()
        release = threading.Event()

        def on_command(cmd):
            with h.sess._startup_log_path.open("ab") as log:
                log.write(b"PROGRESS BEFORE HANG\n")
            release.wait(3.0)
            return _resp("DONE")

        h.fake.on_command = on_command
        with pytest.raises(asyncio.TimeoutError) as exc:
            await h.sess.execute("run all", timeout=0.2)
        assert "PROGRESS BEFORE HANG" in str(exc.value)
        release.set()
        await _wait_until(lambda: h.sess._inflight_task is None)

    async def test_read_failure_degrades_and_keeps_result(self, harness, caplog):
        h = await harness.spawn()

        def on_command(cmd):
            # 日志被删:读增量必然失败
            h.sess._startup_log_path.unlink(missing_ok=True)
            return _resp("RAW")

        h.sess._startup_log_path.write_bytes(b"OLD\n")
        h.fake.on_command = on_command
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            result = await h.sess.execute("run all", timeout=2.0)
        assert result.output.startswith("RAW")
        assert not result.is_error
        assert "[DEGRADED] 读取仿真日志失败" in result.output
        assert "读取仿真日志" in caplog.text
        assert str(h.sess._startup_log_path) in caplog.text

    async def test_stat_failure_before_send_degrades(self, harness, monkeypatch, caplog):
        h = await harness.spawn()
        log_path = h.sess._startup_log_path
        real_stat = Path.stat

        def fail_stat(self, *args, **kwargs):
            if self == log_path:
                raise PermissionError("stat denied (test)")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", fail_stat)
        h.fake.on_command = lambda cmd: _resp("RAW")
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            result = await h.sess.execute("run all", timeout=2.0)
        assert result.output.startswith("RAW")
        assert "stat denied (test)" in result.output
        assert "stat denied (test)" in caplog.text

    async def test_attach_degrades_only_for_sim_commands(self, harness):
        h = await harness.attach()
        result = await h.sess.execute("run all", timeout=2.0)
        assert result.output.startswith("OK:run all")
        assert "[DEGRADED]" in result.output
        assert "Tcl Console" in result.output
        other = await h.sess.execute("get_runs", timeout=2.0)
        assert "[DEGRADED]" not in other.output

    async def test_internal_scripts_only_parser_unaffected(self, harness):
        """内部 launch_simulation -scripts_only 会多附一段日志,按前缀的解析结果不变。"""
        h = await harness.spawn()
        raw = (
            "VMCP_BAT:sim_dir=C:/p/sim_1\n"
            "VMCP_BAT:scripts_only=ok\n"
            "VMCP_BAT_FILE:compile|bat|C:/p/sim_1/behav/xsim/compile.bat\n"
            "VMCP_BAT_DONE\n"
        )
        h.fake.on_command = _appends_log(
            h, b"INFO: [SIM-utils-54] Inspecting design source files\n", raw
        )
        script = LAUNCH_SCRIPTS_AND_GLOB.format(run_name="sim_1")
        assert _SIM_COMMAND_RE.search(script), "内部脚本确实会匹配"

        result = await h.sess.execute(script, timeout=2.0)

        assert "--- 仿真日志" in result.output
        assert parse_launch_scripts_output(result.output) == parse_launch_scripts_output(raw)


class TestReadLogDelta:
    def test_log_shorter_than_offset_is_read_from_start(self, tmp_path):
        """日志变短说明被轮转或截断,里面全是新内容,不能当成"没有新增"。"""
        log = tmp_path / "v.log"
        log.write_bytes(b"short")
        assert _read_log_delta(log, 100, 8000) == ("short", 5, False)

    def test_offset_at_end_is_empty(self, tmp_path):
        log = tmp_path / "v.log"
        log.write_bytes(b"short")
        assert _read_log_delta(log, 5, 8000) == ("", 0, False)

    def test_utf8_split_by_window_decodes_cleanly(self, tmp_path):
        log = tmp_path / "v.log"
        log.write_bytes("中".encode("utf-8") * 20_000 + "结束标记\n".encode("utf-8"))
        text, total, truncated = _read_log_delta(log, 0, 100)
        assert text.endswith("结束标记")
        assert "\ufffd" not in text
        assert total == log.stat().st_size
        assert truncated is True
        assert len(text) <= 100

    def test_exact_size_not_truncated(self, tmp_path):
        log = tmp_path / "v.log"
        log.write_bytes(b"prefix\nnew line\n")
        assert _read_log_delta(log, 7, 8000) == ("new line", 9, False)

    def test_missing_file_raises_for_caller_to_degrade(self, tmp_path):
        with pytest.raises(OSError):
            _read_log_delta(tmp_path / "missing.log", 0, 8000)


# --------------------------------------------------------------------------- #
#  退出诊断(缺陷 2)
# --------------------------------------------------------------------------- #


@pytest.fixture
async def managed(env, monkeypatch, session_manager):
    """经 SessionManager 完整启动一个假 Vivado。"""
    stops: list[threading.Event] = []

    async def start(session_id: str = "s", fake: FakeVivado | None = None, proc=None):
        fake = fake or FakeVivado()
        proc = proc or FakeProc()
        port, stop, _ = _serve_length_prefix(fake)
        stops.append(stop)

        async def fake_exec(*args, **kwargs):
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(GuiSession, "_alloc_free_port", staticmethod(lambda: port))
        sess, banner = await session_manager.start_session(session_id, mode="gui", port=0)
        return SimpleNamespace(sess=sess, fake=fake, proc=proc, stop=stop, banner=banner)

    yield start

    await session_manager.close_all()
    for stop in stops:
        stop.set()


async def _die(h, code: int | None = 1) -> None:
    """模拟 Vivado 退出:连接断开,外层进程结束。"""
    h.proc.returncode = code
    h.stop.set()
    await _wait_until(lambda: not h.sess.is_alive)


class TestDeathReport:
    async def test_tool_returns_diagnostics_not_missing(self, managed, session_manager, env):
        h = await managed()
        cwd = Path(h.sess.status_dict()["cwd"])
        (cwd / f"hs_err_pid{VIVADO_PID}.log").write_text("EXCEPTION_ACCESS_VIOLATION")
        (cwd / f"hs_err_pid{VIVADO_PID}.dmp").write_bytes(b"\x00")
        (cwd / "hs_err_pid999.log").write_text("OTHER INSTANCE")
        h.sess._startup_log_path.write_text("VIVADO LOG TAIL\n")
        h.sess._launcher_log_path.write_text("LAUNCHER LOG TAIL\n")
        await h.sess.execute("run all", timeout=2.0)
        await _die(h, 1)

        text = await tcl_tools.run_tcl("puts x", session_id="s", ctx=_ctx(session_manager))

        assert "不存在" not in text
        assert "已退出" in text
        assert "异常退出" in text
        assert "退出码 1" in text
        assert f"vivado_pid: {VIVADO_PID}" in text
        assert f"launcher_pid: {LAUNCHER_PID}" in text
        assert str(cwd) in text
        assert "最后一条命令: run all" in text
        assert f"hs_err_pid{VIVADO_PID}.log" in text
        assert f"hs_err_pid{VIVADO_PID}.dmp" in text
        assert "hs_err_pid999" not in text
        assert "EXCEPTION_ACCESS_VIOLATION" in text
        assert "VIVADO LOG TAIL" in text
        assert "LAUNCHER LOG TAIL" in text

    async def test_exit_is_detected_when_peer_closes_cleanly(self, managed):
        """手动关 GUI / Tcl exit 是 FIN,写端不会自动关,is_alive 也要翻转。"""
        h = await managed()
        h.stop.set()
        await _wait_until(lambda: not h.sess.is_alive)

    @pytest.mark.parametrize(
        ("code", "expected"), [(0, "正常退出"), (1, "异常退出"), (-1073741819, "异常退出")]
    )
    def test_exit_code_wording_is_neutral(self, code, expected):
        text = format_death_report({"session_id": "s", "mode": "gui", "exit_code": code})
        assert expected in text
        assert f"退出码 {code}" in text
        assert "崩溃" not in text

    def test_unknown_exit_code_wording(self):
        text = format_death_report({"session_id": "s", "mode": "attach", "exit_code": None})
        assert "退出码未知" in text
        assert "崩溃" not in text

    async def test_no_crash_files_says_where_it_looked(self, managed, session_manager):
        h = await managed()
        await _die(h)
        session_manager.get("s")
        text = session_manager.no_session_message("s")
        assert "未找到" in text

    async def test_unknown_pid_lists_any_hs_err(self, managed, session_manager):
        h = await managed(fake=FakeVivado(pid=None))
        cwd = Path(h.sess.status_dict()["cwd"])
        (cwd / "hs_err_pid777.log").write_text("SOME CRASH")
        await _die(h)
        session_manager.get("s")
        report = session_manager.dead_report("s")
        assert report["vivado_pid"] is None
        assert any("hs_err_pid777" in f for f in report["crash_files"])

    def test_missing_session_keeps_original_message(self, session_manager):
        assert session_manager.no_session_message("nope") == (
            "[ERROR] 会话 'nope' 不存在。请先调用 start_session。"
        )

    async def test_list_sessions_shows_dead_entry(self, managed, session_manager):
        h = await managed()
        await h.sess.execute("run 10ns", timeout=2.0)
        await _die(h, 1)

        text = await session_tools.list_sessions(ctx=_ctx(session_manager))

        entries = json.loads(text)
        entry = next(e for e in entries if e["session_id"] == "s")
        assert entry["state"] == "dead"
        assert entry["is_alive"] is False
        assert entry["exit_code"] == 1
        assert entry["vivado_pid"] == VIVADO_PID
        assert entry["last_command"] == "run 10ns"
        assert entry["startup_log"]

    async def test_status_resource_shows_dead_entry(self, managed, session_manager, monkeypatch):
        h = await managed()
        await _die(h, 1)
        monkeypatch.setattr(server_module, "_manager_ref", session_manager)
        data = json.loads(server_module.resource_session_status("s"))
        assert data["state"] == "dead"
        assert data["exit_code"] == 1
        assert "不存在" in json.loads(server_module.resource_session_status("other"))["error"]

    async def test_xdc_lint_without_paths_gives_diagnostics(self, managed, session_manager):
        h = await managed()
        await _die(h, 1)
        text = await diagnostic_tools.xdc_lint(session_id="s", ctx=_ctx(session_manager))
        assert "xdc_paths" in text
        assert "退出码 1" in text

    async def test_exit_code_filled_in_after_process_is_reaped(
        self, managed, session_manager, monkeypatch
    ):
        h = await managed()
        await _die(h, None)
        assert session_manager.get("s") is None
        assert session_manager.dead_report("s")["exit_code"] is None
        h.proc.returncode = 3
        assert session_manager.dead_report("s")["exit_code"] == 3

    async def test_tcl_session_falls_back_to_status_dict(self, session_manager):
        class FakeTcl:
            session_id = "t"
            is_alive = False

            def status_dict(self):
                return {"session_id": "t", "mode": "tcl", "state": "dead", "is_alive": False}

            async def stop(self):
                pass

        session_manager._sessions["t"] = FakeTcl()
        assert session_manager.get("t") is None
        text = session_manager.no_session_message("t")
        assert "Tcl 模式" in text
        assert "不存在" not in text

    async def test_report_failure_degrades_instead_of_breaking_tool(
        self, session_manager, caplog
    ):
        class Broken:
            session_id = "b"
            is_alive = False

            def death_report(self):
                raise ValueError("report exploded (test)")

            async def stop(self):
                pass

        session_manager._sessions["b"] = Broken()
        with caplog.at_level(logging.WARNING, logger="vivado_mcp.vivado.session_manager"):
            assert session_manager.get("b") is None
        assert "report exploded (test)" in caplog.text
        assert "[DEGRADED]" in session_manager.no_session_message("b")


class TestDisconnectDuringCommand:
    async def test_same_error_carries_diagnostics(self, managed):
        """R2.6:命令执行中断开,同一条错误里就带上诊断。"""
        h = await managed()

        def on_command(text):
            h.proc.returncode = 1
            return None

        h.fake.on_command = on_command
        with pytest.raises(RuntimeError, match="连接断开") as exc:
            await h.sess.execute("run all", timeout=3.0)
        message = str(exc.value)
        assert "退出码 1" in message
        assert "最后一条命令: run all" in message
        assert f"vivado_pid: {VIVADO_PID}" in message

    async def test_waits_for_process_before_reporting(self, managed, monkeypatch):
        """断开瞬间进程可能还没退完,等它结束再取退出码。"""
        h = await managed()

        class SlowExit(FakeProc):
            async def wait(self):
                await asyncio.sleep(0.05)
                self.returncode = 7
                return 7

        slow = SlowExit()
        h.sess._proc = slow
        h.fake.on_command = lambda text: None
        with pytest.raises(RuntimeError, match="退出码 7"):
            await h.sess.execute("run all", timeout=3.0)

    async def test_process_still_running_reports_unknown(
        self, managed, monkeypatch, caplog
    ):
        monkeypatch.setattr(gui_session, "_EXIT_WAIT_TIMEOUT", 0.05)
        h = await managed(proc=FakeProc(hang=True))
        h.fake.on_command = lambda text: None
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            with pytest.raises(RuntimeError, match="退出码未知"):
                await h.sess.execute("run all", timeout=3.0)
        assert "退出码未知" in caplog.text
        h.proc.hang = False

    async def test_held_session_gets_diagnostics_on_next_command(self, managed):
        """工具拿着 session 轮询时 Vivado 退出,下一条命令也要带诊断。"""
        h = await managed()
        await _die(h, 2)
        with pytest.raises(RuntimeError, match="未连接") as exc:
            await h.sess.execute("get_property STATUS [get_runs impl_1]", timeout=1.0)
        assert "退出码 2" in str(exc.value)

    async def test_stopped_session_has_no_diagnostics(self, managed, session_manager):
        h = await managed()
        await session_manager.stop_session("s")
        with pytest.raises(RuntimeError, match="未连接") as exc:
            await h.sess.execute("puts x", timeout=1.0)
        assert "退出码" not in str(exc.value)

    async def test_late_failure_after_timeout_is_logged_as_warning(self, managed, caplog):
        h = await managed()
        release = threading.Event()

        def on_command(text):
            release.wait(3.0)
            return None

        h.fake.on_command = on_command
        with pytest.raises(asyncio.TimeoutError):
            await h.sess.execute("run all", timeout=0.05)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            release.set()
            await _wait_until(lambda: h.sess._inflight_task is None)
        assert "在途命令结束时异常" in caplog.text

    async def test_stop_during_command_is_not_reported_as_failure(self, managed, caplog):
        h = await managed()
        release = threading.Event()
        h.fake.on_command = lambda text: (release.wait(3.0), _resp("DONE"))[1]
        with pytest.raises(asyncio.TimeoutError):
            await h.sess.execute("run all", timeout=0.05)
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            await h.sess.stop()
        assert "在途命令结束时异常" not in caplog.text
        release.set()


class TestListedTails:
    async def _dead_with_long_logs(self, managed, tail: str):
        h = await managed()
        cwd = Path(h.sess.status_dict()["cwd"])
        (cwd / f"hs_err_pid{VIVADO_PID}.log").write_text(tail)
        h.sess._startup_log_path.write_text(tail)
        h.sess._launcher_log_path.write_text(tail)
        await _die(h, 1)
        return h

    async def test_list_keeps_last_1000_chars_of_each_tail(self, managed, session_manager):
        await self._dead_with_long_logs(managed, "x" * 3000 + "END")
        text = await session_tools.list_sessions(ctx=_ctx(session_manager))
        entry = next(e for e in json.loads(text) if e["session_id"] == "s")
        for key in ("hs_err_text", "log_tail", "launcher_tail"):
            assert len(entry[key]) == 1000
            assert entry[key].endswith("END")
        assert "tails_truncated" in entry

    async def test_sessions_resource_also_short(self, managed, session_manager, monkeypatch):
        await self._dead_with_long_logs(managed, "x" * 3000 + "END")
        monkeypatch.setattr(server_module, "_manager_ref", session_manager)
        data = json.loads(await server_module.resource_sessions())
        entry = next(e for e in data["sessions"] if e["session_id"] == "s")
        assert len(entry["log_tail"]) == 1000

    async def test_single_report_keeps_full_length_after_list(
        self, managed, session_manager, monkeypatch
    ):
        """list 不能把存着的诊断改短:工具返回和单会话 status 仍是完整长度。"""
        await self._dead_with_long_logs(managed, "x" * 3000 + "END")
        await session_manager.list_sessions(probe_external=False)

        monkeypatch.setattr(server_module, "_manager_ref", session_manager)
        report = json.loads(server_module.resource_session_status("s"))
        assert len(report["log_tail"]) == 3003
        assert "tails_truncated" not in report
        text = await tcl_tools.run_tcl("puts x", session_id="s", ctx=_ctx(session_manager))
        assert "x" * 3000 in text

    async def test_short_tails_untouched(self, managed, session_manager):
        await self._dead_with_long_logs(managed, "short tail")
        entries = await session_manager.list_sessions(probe_external=False)
        assert entries[0]["log_tail"] == "short tail"
        assert "tails_truncated" not in entries[0]


class TestDeadRecordLifecycle:
    async def test_list_then_stop_never_says_missing(self, managed, session_manager):
        h = await managed()
        await _die(h, 1)
        entries = await session_manager.list_sessions(probe_external=False)
        assert [e["state"] for e in entries] == ["dead"]

        result = await session_manager.stop_session("s")

        assert "不存在" not in result
        assert "已退出" in result
        assert "退出码 1" in result
        assert await session_manager.list_sessions(probe_external=False) == []
        assert session_manager.no_session_message("s").endswith("请先调用 start_session。")
        assert await session_manager.stop_session("s") == "会话 's' 不存在。"

    async def test_stop_without_list_also_finds_dead_session(self, managed, session_manager):
        h = await managed()
        await _die(h, 1)
        assert "已退出" in await session_manager.stop_session("s")
        assert session_manager.dead_report("s") is None

    async def test_stop_keeps_evidence_files(self, managed, session_manager):
        h = await managed()
        cwd = Path(h.sess.status_dict()["cwd"])
        (cwd / f"vivado_pid{VIVADO_PID}.str").write_text("x")
        (cwd / f"hs_err_pid{VIVADO_PID}.log").write_text("crash")
        await _die(h, 1)
        message = await session_manager.stop_session("s")
        assert str(cwd) in message
        assert (cwd / f"vivado_pid{VIVADO_PID}.str").exists()
        assert (cwd / f"hs_err_pid{VIVADO_PID}.log").exists()

    async def test_stop_failure_keeps_record_for_retry(self, session_manager):
        class FailsStop:
            session_id = "f"
            is_alive = False
            calls = 0

            def death_report(self):
                return {"session_id": "f", "state": "dead", "is_alive": False, "exit_code": 1}

            async def stop(self):
                FailsStop.calls += 1
                if FailsStop.calls == 1:
                    raise RuntimeError("taskkill failed (test)")

        session_manager._sessions["f"] = FailsStop()
        with pytest.raises(RuntimeError, match="taskkill failed"):
            await session_manager.stop_session("f")
        assert session_manager.dead_report("f") is not None
        assert "已退出" in await session_manager.stop_session("f")
        assert session_manager.dead_report("f") is None

    async def test_start_same_id_clears_record(self, managed, session_manager):
        first = await managed()
        await _die(first, 1)
        session_manager.get("s")
        assert session_manager.dead_report("s") is not None

        second = await managed()

        assert second.sess is not first.sess
        assert second.sess.is_alive
        assert session_manager.dead_report("s") is None
        assert first.sess.state.value == "stopped", "旧对象要被 stop 清理"
        assert session_manager.get("s") is second.sess

    async def test_start_survives_old_session_stop_failure(
        self, managed, session_manager, caplog
    ):
        first = await managed()
        await _die(first, 1)
        session_manager.get("s")

        async def boom():
            raise RuntimeError("old stop failed (test)")

        first.sess.stop = boom
        with caplog.at_level(logging.WARNING, logger="vivado_mcp.vivado.session_manager"):
            second = await managed()
        assert second.sess.is_alive
        assert "[DEGRADED]" in second.banner
        assert "old stop failed (test)" in caplog.text

    async def test_records_capped_and_oldest_evicted(self, session_manager):
        stopped: list[str] = []

        class Dead:
            is_alive = False

            def __init__(self, sid):
                self.session_id = sid

            def death_report(self):
                return {"session_id": self.session_id, "state": "dead", "exit_code": 1}

            async def stop(self):
                stopped.append(self.session_id)

        for i in range(12):
            sid = f"d{i}"
            session_manager._sessions[sid] = Dead(sid)
            assert session_manager.get(sid) is None

        entries = await session_manager.list_sessions(probe_external=False)
        assert [e["session_id"] for e in entries] == [f"d{i}" for i in range(2, 12)]
        await _wait_until(lambda: not session_manager._evicting)
        assert stopped == ["d0", "d1"], "被淘汰的对象也要 stop,外层进程可能还在"

    async def test_close_all_stops_dead_sessions(self, session_manager):
        stopped: list[str] = []

        class Dead:
            session_id = "z"
            is_alive = False

            def death_report(self):
                return {"session_id": "z", "state": "dead", "exit_code": 1}

            async def stop(self):
                stopped.append("z")

        session_manager._sessions["z"] = Dead()
        session_manager.get("z")
        await session_manager.close_all()
        assert stopped == ["z"]
        assert session_manager.dead_report("z") is None

    async def test_cap_constant_matches_requirement(self):
        assert sm_module._MAX_DEAD_RECORDS == 10

    async def test_get_racing_with_normal_stop_leaves_no_false_record(
        self, managed, session_manager
    ):
        """stop 期间别的工具调 get():正常关闭不能留下"已退出"记录。"""
        h = await managed()
        original = h.sess.stop

        async def stop_with_race(*args, **kwargs):
            await original(*args, **kwargs)
            session_manager.get("s")

        h.sess.stop = stop_with_race
        assert await session_manager.stop_session("s") == "会话 's' 已关闭。"
        assert session_manager.dead_report("s") is None
