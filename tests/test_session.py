"""session 与 session_manager 单元测试。

测试 session_id 验证和基本管理逻辑（不启动实际 Vivado 进程）。
"""

import asyncio

import pytest

from vivado_mcp.vivado.base_session import SessionState
from vivado_mcp.vivado.session import SubprocessSession
from vivado_mcp.vivado.session_manager import SessionManager, _validate_session_id
from vivado_mcp.vivado.tcl_utils import TclResult


class _AliveProcess:
    returncode = None


class TestSubprocessInflightOwnership:
    """调用方超时后，原任务必须继续独占该会话的响应流。"""

    @pytest.mark.asyncio
    async def test_timeout_rejects_next_command_until_original_finishes(self):
        session = SubprocessSession("/fake/vivado", "owned")
        session._process = _AliveProcess()
        session._state = SessionState.READY
        release = asyncio.Event()

        async def fake_execute(command: str) -> TclResult:
            if command == "FIRST":
                await release.wait()
            return TclResult(output=command, return_code=0, is_error=False)

        session._execute_impl = fake_execute

        with pytest.raises(asyncio.TimeoutError, match="命令执行超时"):
            await session.execute("FIRST", timeout=0.01)
        first_task = session._inflight_task
        assert first_task is not None
        assert session.state == SessionState.BUSY

        with pytest.raises(RuntimeError, match="上一条命令仍在执行"):
            await session.execute("SECOND", timeout=1.0)

        release.set()
        first = await asyncio.wait_for(asyncio.shield(first_task), timeout=1.0)
        assert first.output == "FIRST"
        await asyncio.sleep(0)

        second = await session.execute("SECOND", timeout=1.0)
        assert second.output == "SECOND"
        assert session.state == SessionState.READY

    @pytest.mark.asyncio
    async def test_same_session_serializes_but_different_sessions_overlap(self):
        active = 0
        max_active = 0
        both_started = asyncio.Event()
        release = asyncio.Event()

        async def fake_execute(command: str) -> TclResult:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            if active == 2:
                both_started.set()
            await release.wait()
            active -= 1
            return TclResult(output=command, return_code=0, is_error=False)

        first = SubprocessSession("/fake/vivado", "first")
        second = SubprocessSession("/fake/vivado", "second")
        first._process = _AliveProcess()
        second._process = _AliveProcess()
        first._state = SessionState.READY
        second._state = SessionState.READY
        first._execute_impl = fake_execute
        second._execute_impl = fake_execute

        task_a = asyncio.create_task(first.execute("A", timeout=1.0))
        task_b = asyncio.create_task(second.execute("B", timeout=1.0))
        await asyncio.wait_for(both_started.wait(), timeout=1.0)
        assert max_active == 2
        release.set()
        await asyncio.gather(task_a, task_b)

    @pytest.mark.asyncio
    async def test_same_session_executes_in_order(self):
        session = SubprocessSession("/fake/vivado", "ordered")
        session._process = _AliveProcess()
        session._state = SessionState.READY
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        calls: list[str] = []

        async def fake_execute(command: str) -> TclResult:
            calls.append(command)
            if command == "FIRST":
                first_started.set()
                await release_first.wait()
            return TclResult(output=command, return_code=0, is_error=False)

        session._execute_impl = fake_execute
        first = asyncio.create_task(session.execute("FIRST", timeout=1.0))
        await first_started.wait()
        second = asyncio.create_task(session.execute("SECOND", timeout=1.0))
        await asyncio.sleep(0)
        assert calls == ["FIRST"]

        release_first.set()
        await asyncio.gather(first, second)
        assert calls == ["FIRST", "SECOND"]

    @pytest.mark.asyncio
    async def test_stop_blocks_new_commands_without_waiting_for_inflight_lock(self):
        class _StoppingProcess:
            def __init__(self):
                self.returncode = None
                self.waiting = asyncio.Event()
                self.release = asyncio.Event()

            async def wait(self):
                self.waiting.set()
                await self.release.wait()
                self.returncode = 0
                return 0

            def kill(self):
                self.returncode = -9
                self.release.set()

        process = _StoppingProcess()
        session = SubprocessSession("/fake/vivado", "stopping")
        session._process = process
        session._state = SessionState.READY
        first_started = asyncio.Event()
        calls: list[str] = []

        async def fake_execute(command: str) -> TclResult:
            calls.append(command)
            first_started.set()
            await asyncio.Event().wait()

        session._execute_impl = fake_execute
        first = asyncio.create_task(session.execute("FIRST", timeout=10.0))
        await first_started.wait()

        stopping = asyncio.create_task(session.stop(timeout=1.0))
        await process.waiting.wait()
        assert session.state == SessionState.STOPPING
        with pytest.raises(RuntimeError, match="正在关闭"):
            await session.execute("SECOND", timeout=1.0)
        assert calls == ["FIRST"]

        process.release.set()
        await stopping
        with pytest.raises(asyncio.CancelledError):
            await first
        assert session.state == SessionState.STOPPED


class TestValidateSessionId:
    """session_id 格式验证测试。"""

    def test_valid_ids(self):
        """合法 session_id 通过验证。"""
        valid = ["default", "session-1", "my_session", "ABC123", "a"]
        for sid in valid:
            assert _validate_session_id(sid) == sid

    def test_rejects_empty(self):
        """拒绝空字符串。"""
        with pytest.raises(ValueError, match="session_id"):
            _validate_session_id("")

    def test_rejects_spaces(self):
        """拒绝含空格的 ID。"""
        with pytest.raises(ValueError, match="session_id"):
            _validate_session_id("my session")

    def test_rejects_special_chars(self):
        """拒绝特殊字符。"""
        for bad in ["a;b", "a/b", "../etc", "a$b", "a[b]"]:
            with pytest.raises(ValueError, match="session_id"):
                _validate_session_id(bad)

    def test_rejects_too_long(self):
        """拒绝超过 64 字符的 ID。"""
        with pytest.raises(ValueError, match="session_id"):
            _validate_session_id("a" * 65)

    def test_accepts_max_length(self):
        """接受正好 64 字符的 ID。"""
        assert _validate_session_id("a" * 64) == "a" * 64


class TestSessionManager:
    """SessionManager 基本逻辑测试。"""

    def test_get_nonexistent(self, session_manager: SessionManager):
        """获取不存在的会话返回 None。"""
        assert session_manager.get("nonexistent") is None

    async def test_list_empty(self, session_manager: SessionManager):
        """空管理器列表为空(关闭外部 probe,只看 MCP 自己管的)。

        0.3.19 起 ``list_sessions()`` 默认会 probe 9999..10003 发现用户手动
        启动 + init.tcl 注入的外部 GUI,本机若真有 Vivado 跑会让本断言失败。
        显式 ``probe_external=False`` 关掉网络探测,只验证字典层逻辑。
        (0.3.22 起 list_sessions 为 async:probe 并发化,不阻塞 event loop)
        """
        assert await session_manager.list_sessions(probe_external=False) == []

    def test_default_vivado_path(self, session_manager: SessionManager):
        """默认路径正确存储。"""
        assert session_manager.default_vivado_path == "/fake/vivado"

    def test_get_validates_session_id(self, session_manager: SessionManager):
        """get 方法会验证 session_id 格式。"""
        with pytest.raises(ValueError, match="session_id"):
            session_manager.get("invalid;id")

    async def test_gui_default_passes_port_zero_to_session(
        self, session_manager: SessionManager, monkeypatch
    ):
        """B 方案多开语义:mode='gui' 不传 port 时 manager 把 port=0 透传给 GuiSession。

        port=0 = auto-alloc 独立新实例(连开两次 = 两个独立 GUI)。这里 mock
        GuiSession 的构造 + start,只验证透传的 port 参数,不起真 Vivado。
        """
        from vivado_mcp.vivado import session_manager as sm_mod

        captured: dict[str, int] = {}

        class _FakeSession:
            mode = "gui"
            connected_port = None
            pid = None

            def __init__(self, *, vivado_path, session_id, port, attach_only):
                captured["port"] = port
                captured["attach_only"] = attach_only

            async def start(self, timeout):
                return "fake banner"

            @property
            def is_alive(self):
                return True

            def status_dict(self):
                return {"mode": "gui", "state": "ready"}

        monkeypatch.setattr(sm_mod, "GuiSession", _FakeSession)

        await session_manager.start_session(session_id="multi", mode="gui")
        assert captured["port"] == 0, "默认 gui 应透传 port=0 走 auto-alloc 独立实例"
        assert captured["attach_only"] is False

    async def test_port_map_records_and_clears(
        self, session_manager: SessionManager, monkeypatch
    ):
        """start_session 成功后 _port_map 记 (port, pid);stop 后清掉。"""
        from vivado_mcp.vivado import session_manager as sm_mod

        class _FakeSession:
            mode = "gui"
            connected_port = 54321
            pid = 9090

            def __init__(self, *, vivado_path, session_id, port, attach_only):
                pass

            async def start(self, timeout):
                return "banner"

            async def stop(self):
                return None

            @property
            def is_alive(self):
                return True

            def status_dict(self):
                return {"mode": "gui", "state": "ready"}

        monkeypatch.setattr(sm_mod, "GuiSession", _FakeSession)

        await session_manager.start_session(session_id="rec", mode="gui")
        assert session_manager._port_map["rec"] == (54321, 9090)

        await session_manager.stop_session("rec")
        assert "rec" not in session_manager._port_map


class TestAsciiPathCheck:
    """_check_ascii_paths:Vivado 2019.x 中文路径预警。"""

    def test_pure_ascii_returns_empty(self):
        from vivado_mcp.tools.session_tools import _check_ascii_paths
        assert _check_ascii_paths("C:/Xilinx/Vivado/2019.1/bin/vivado.bat") == ""

    def test_chinese_path_returns_warning(self):
        from vivado_mcp.tools.session_tools import _check_ascii_paths
        warn = _check_ascii_paths("D:/项目/Vivado/2019.1/bin/vivado.bat")
        assert "警告" in warn
        assert "非 ASCII" in warn
        assert "TclStackFree" in warn
        assert "ASCII" in warn
        assert "D:/项目/Vivado" in warn

    def test_empty_vivado_path_still_checks_cwd(self, tmp_path, monkeypatch):
        """vivado_path 为 None 也要检查 cwd(攻击面更广)。"""
        from vivado_mcp.tools.session_tools import _check_ascii_paths
        # cwd 为 ASCII → 无警告
        monkeypatch.chdir(tmp_path)
        assert _check_ascii_paths(None) == ""

    def test_none_path_pure_ascii_cwd_empty(self, tmp_path, monkeypatch):
        from vivado_mcp.tools.session_tools import _check_ascii_paths
        monkeypatch.chdir(tmp_path)
        assert _check_ascii_paths(None) == ""

    def test_warning_mentions_gui_session_scope_extension(self):
        """0.3.18 锁:警告必须告知 GUI session cd/open 中文路径也会炸。

        0.3.17 实战发现:旧警告只覆盖综合 .runs/.sim/ 输出目录,但 GUI
        session 内 cd/open_project 中文路径同样触发 TclStackFree。
        """
        from vivado_mcp.tools.session_tools import _check_ascii_paths
        warn = _check_ascii_paths("D:/项目/Vivado/2019.1/bin/vivado.bat")
        # GUI session 范围扩展提示必须存在
        assert "GUI session" in warn or "open_project" in warn
        # 仍保留原有综合输出目录提示
        assert ".runs/" in warn or "输出目录" in warn


_CURDIR_VAR = "NoDefaultCurrentDirectoryInExePath"


def _install_fake_winreg(monkeypatch, values=None, open_error=None):
    """用假 winreg 模拟注册表,Linux CI 上也能跑。

    values: {"HKCU": 值, "HKLM": 值},缺的键 = 该处无此值。
    open_error: OpenKey 抛出的异常(模拟读注册表失败)。
    """
    import sys
    from types import SimpleNamespace

    values = values or {}
    roots = {"hkcu": "HKCU", "hklm": "HKLM"}

    class FakeKey:
        def __init__(self, label):
            self.label = label

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(root, subkey, *a, **kw):
        if open_error is not None:
            raise open_error
        return FakeKey(roots[root])

    def fake_query(key, name):
        assert name == _CURDIR_VAR
        if key.label not in values:
            raise FileNotFoundError("value not set")
        return (values[key.label], 4)

    fake = SimpleNamespace(
        HKEY_CURRENT_USER="hkcu",
        HKEY_LOCAL_MACHINE="hklm",
        KEY_READ=0,
        OpenKey=fake_open,
        QueryValueEx=fake_query,
    )
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(sys, "platform", "win32")


class TestWinCurdirPolicyCheck:
    """NoDefaultCurrentDirectoryInExePath:注册表存在即警告(值为 0 也算)。"""

    def test_non_windows_returns_empty(self, monkeypatch):
        """非 Windows 直接空,不读注册表。"""
        from vivado_mcp.tools import session_tools
        monkeypatch.setattr(session_tools.sys, "platform", "linux")
        assert session_tools._check_win_curdir_policy() == ""

    def test_no_registry_value_returns_empty(self, monkeypatch):
        """两处都无值 → 不警告(不再按 Windows 版本推断)。"""
        from vivado_mcp.tools import session_tools

        _install_fake_winreg(monkeypatch, values={})
        assert session_tools._check_win_curdir_policy() == ""

    @pytest.mark.parametrize("values", [
        {"HKCU": 1},
        {"HKCU": 0},
        {"HKLM": 1},
        {"HKCU": "0", "HKLM": 1},
    ])
    def test_value_present_returns_warning(self, monkeypatch, values):
        """任一处存在该值(含 0)→ 警告,给 reg delete + unset,不给"设 0"。"""
        from vivado_mcp.tools import session_tools

        _install_fake_winreg(monkeypatch, values=values)
        warn = session_tools._check_win_curdir_policy()
        assert _CURDIR_VAR in warn
        assert "compile.bat" in warn
        assert "存在即生效" in warn
        assert f'reg delete "HKCU\\Environment" /v {_CURDIR_VAR} /f' in warn
        assert f"unset ::env({_CURDIR_VAR})" in warn
        assert "/d 0" not in warn
        assert "=0" not in warn
        assert "reg add" not in warn
        for label in values:
            assert label in warn

    def test_registry_oserror_logged_as_warning(self, monkeypatch, caplog):
        """读注册表 OSError 必须 logger.warning 留痕(错误处理铁律,禁 debug)。"""
        import logging

        from vivado_mcp.tools import session_tools

        _install_fake_winreg(monkeypatch, open_error=OSError("拒绝访问(模拟)"))
        with caplog.at_level(
            logging.WARNING, logger="vivado_mcp.tools.session_tools"
        ):
            result = session_tools._check_win_curdir_policy()

        assert result == ""
        assert any(r.levelno == logging.WARNING for r in caplog.records)
        assert "读注册表" in caplog.text
        assert "拒绝访问(模拟)" in caplog.text, "必须带具体异常信息"

    def test_filenotfound_path_no_warning(self, monkeypatch, caplog):
        """反例:键不存在(FileNotFoundError)走正常 fallthrough,不发 warning。"""
        import logging

        from vivado_mcp.tools import session_tools

        _install_fake_winreg(
            monkeypatch, open_error=FileNotFoundError("no key")
        )
        with caplog.at_level(
            logging.WARNING, logger="vivado_mcp.tools.session_tools"
        ):
            assert session_tools._check_win_curdir_policy() == ""

        assert "读注册表" not in caplog.text


class TestStartSessionCurdirWarning:
    """start_session 只在 attach 外部 Vivado 时附加注册表警告。"""

    async def _start(self, monkeypatch, mode):
        from types import SimpleNamespace

        from vivado_mcp.tools import session_tools

        session = SimpleNamespace(status_dict=lambda: {
            "mode": mode, "vivado_path": "C:/Xilinx/vivado.bat", "state": "ready",
        })

        class FakeManager:
            async def start_session(self, **kw):
                return session, "banner"

        monkeypatch.setattr(session_tools, "_get_manager", lambda ctx: FakeManager())
        monkeypatch.setattr(session_tools, "_check_ascii_paths", lambda p, cwd=None: "")
        monkeypatch.setattr(
            session_tools, "_check_win_curdir_policy", lambda: "\nCURDIR_WARN"
        )
        return await session_tools.start_session(session_id="s", mode=mode)

    async def test_attach_appends_warning(self, monkeypatch):
        assert "CURDIR_WARN" in await self._start(monkeypatch, "attach")

    @pytest.mark.parametrize("mode", ["gui", "tcl"])
    async def test_spawned_session_no_warning(self, monkeypatch, mode):
        assert "CURDIR_WARN" not in await self._start(monkeypatch, mode)


class TestVivadoChildEnv:
    """vivado_child_env:spawn Vivado 时去掉 NoDefaultCurrentDirectoryInExePath。"""

    def _patch(self, monkeypatch, platform, environ):
        from types import SimpleNamespace

        from vivado_mcp.vivado import base_session

        # 只替换 base_session 看到的 sys / os,不影响全局
        monkeypatch.setattr(base_session, "sys", SimpleNamespace(platform=platform))
        monkeypatch.setattr(base_session, "os", SimpleNamespace(environ=environ))
        return base_session

    @pytest.mark.parametrize("key", [
        _CURDIR_VAR, _CURDIR_VAR.upper(), "nodefaultcurrentdirectoryinexepath",
    ])
    def test_windows_removes_var_case_insensitive(self, monkeypatch, caplog, key):
        import logging

        bs = self._patch(monkeypatch, "win32", {key: "1", "PATH": "C:/x", "Foo": "bar"})
        with caplog.at_level(logging.INFO, logger="vivado_mcp.vivado.base_session"):
            env = bs.vivado_child_env()
        assert env == {"PATH": "C:/x", "Foo": "bar"}
        assert _CURDIR_VAR in caplog.text

    def test_windows_var_absent_returns_none(self, monkeypatch):
        bs = self._patch(monkeypatch, "win32", {"PATH": "C:/x"})
        assert bs.vivado_child_env() is None

    def test_non_windows_returns_none(self, monkeypatch):
        bs = self._patch(monkeypatch, "linux", {_CURDIR_VAR: "1", "PATH": "/bin"})
        assert bs.vivado_child_env() is None

    async def test_tcl_spawn_passes_filtered_env(self, monkeypatch):
        """SubprocessSession.start 把去掉该变量的 env 传给子进程。"""
        from types import SimpleNamespace

        from vivado_mcp.vivado import base_session

        monkeypatch.setattr(base_session, "sys", SimpleNamespace(platform="win32"))
        monkeypatch.setenv(_CURDIR_VAR, "1")
        monkeypatch.setenv("VMCP_TEST_CANARY", "keep")
        captured = {}

        async def fake_spawn(*args, **kwargs):
            captured.update(kwargs)
            raise OSError("spawn blocked in test")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_spawn)
        session = SubprocessSession(vivado_path="/fake/vivado", session_id="envt")
        with pytest.raises(RuntimeError, match="spawn blocked in test"):
            await session.start(timeout=1.0)

        env = captured["env"]
        assert not any(k.upper() == _CURDIR_VAR.upper() for k in env)
        assert any(k.upper() == "VMCP_TEST_CANARY" for k in env)


class TestAsciiPathScopeAnnotation:
    """0.3.20:_check_ascii_paths 警告文本含命令范围说明,避免狼来了。"""

    def test_warning_lists_affected_write_commands(self):
        from vivado_mcp.tools.session_tools import _check_ascii_paths
        warn = _check_ascii_paths("D:/项目/Vivado/2019.1/bin/vivado.bat")
        # 写入类命令明确列出
        assert "create_project" in warn
        assert "synth_design" in warn or "launch_runs" in warn
        assert "launch_simulation" in warn

    def test_warning_lists_safe_readonly_commands(self):
        from vivado_mcp.tools.session_tools import _check_ascii_paths
        warn = _check_ascii_paths("D:/项目/Vivado/2019.1/bin/vivado.bat")
        # 只读 op 明确不踩,可忽略
        assert "open_wave_database" in warn
        assert "report_" in warn or "report_*" in warn or "report" in warn
        # 必须有"可忽略"的明确语义
        assert "可忽略" in warn or "不踩" in warn or "不受影响" in warn


class TestStartSessionFailureCleanup:
    """start_session 失败兜底:session 不进 _sessions,必须调 stop() 清理孤儿。"""

    async def test_start_failure_calls_stop_cleanup(
        self, session_manager: SessionManager, monkeypatch
    ):
        from vivado_mcp.vivado import session_manager as sm_mod

        calls = {"stop": 0}

        class _FailSession:
            mode = "gui"
            connected_port = None
            pid = None

            def __init__(self, *, vivado_path, session_id, port, attach_only):
                pass

            async def start(self, timeout):
                raise RuntimeError("连接超时(模拟)")

            async def stop(self):
                calls["stop"] += 1

            @property
            def is_alive(self):
                return False

        monkeypatch.setattr(sm_mod, "GuiSession", _FailSession)

        with pytest.raises(RuntimeError, match="连接超时"):
            await session_manager.start_session(session_id="failing", mode="gui")

        assert calls["stop"] == 1, "start 失败必须调 stop() 兜底清理"
        assert "failing" not in session_manager._sessions
        assert "failing" not in session_manager._port_map

    async def test_cleanup_failure_does_not_mask_original_error(
        self, session_manager: SessionManager, monkeypatch
    ):
        """反例:兜底 stop() 自己也炸时,上传的仍是原始 start 异常。"""
        from vivado_mcp.vivado import session_manager as sm_mod

        class _DoubleFailSession:
            mode = "gui"

            def __init__(self, *, vivado_path, session_id, port, attach_only):
                pass

            async def start(self, timeout):
                raise RuntimeError("原始启动错误")

            async def stop(self):
                raise OSError("清理也炸了")

            @property
            def is_alive(self):
                return False

        monkeypatch.setattr(sm_mod, "GuiSession", _DoubleFailSession)

        with pytest.raises(RuntimeError, match="原始启动错误"):
            await session_manager.start_session(session_id="dbl", mode="gui")


class TestStopSessionToolErrorWrap:
    """stop_session 工具层兜底:session.stop() 异常不裸出 MCP 层。"""

    @staticmethod
    def _fake_ctx(manager: SessionManager):
        from types import SimpleNamespace

        from vivado_mcp.server import AppContext

        return SimpleNamespace(
            request_context=SimpleNamespace(
                lifespan_context=AppContext(session_manager=manager)
            )
        )

    async def test_stop_exception_returns_error_text(
        self, session_manager: SessionManager, monkeypatch
    ):
        from vivado_mcp.tools.session_tools import stop_session

        async def boom(session_id):
            raise RuntimeError("taskkill 炸了(模拟)")

        monkeypatch.setattr(session_manager, "stop_session", boom)

        result = await stop_session(
            session_id="default", ctx=self._fake_ctx(session_manager)
        )
        assert result.startswith("[ERROR]")
        assert "taskkill 炸了(模拟)" in result, "必须带具体原因"
        assert "RuntimeError" in result
        # 审计 P3:manager 已是 stop 成功后才 pop,失败文案必须告知可重试
        assert "会话仍保留" in result
        assert "可重试 stop_session" in result

    async def test_stop_normal_path_unchanged(
        self, session_manager: SessionManager
    ):
        """正例:正常路径语义不变(不存在的会话返回原文案)。"""
        from vivado_mcp.tools.session_tools import stop_session

        result = await stop_session(
            session_id="nope", ctx=self._fake_ctx(session_manager)
        )
        assert result == "会话 'nope' 不存在。"


class TestStopSessionPopAfterStop:
    """manager.stop_session:stop 成功后再 pop(审计 P3)。

    pop-before-stop 的问题:stop 抛异常时会话已从 _sessions/_port_map 移除,
    AI 重试 stop_session 拿到「会话不存在」,而 Vivado 进程可能仍在跑 ——
    auto-alloc 端口的孤儿从此对 list_sessions 完全不可见。
    """

    @staticmethod
    def _make_fake_session(stop_error: Exception | None = None):
        class _Fake:
            mode = "gui"

            def __init__(self):
                self.stop_calls = 0

            @property
            def is_alive(self):
                return True

            async def stop(self):
                self.stop_calls += 1
                if stop_error is not None:
                    raise stop_error

            def status_dict(self):
                return {"session_id": "x", "mode": "gui", "state": "ready"}

        return _Fake()

    async def test_stop_failure_keeps_session_registered(
        self, session_manager: SessionManager
    ):
        """stop 抛异常 → 会话保留在 _sessions/_port_map,异常原样上抛,可重试。"""
        fake = self._make_fake_session(RuntimeError("taskkill 失败(模拟)"))
        session_manager._sessions["s1"] = fake
        session_manager._port_map["s1"] = (1234, 99)

        with pytest.raises(RuntimeError, match="taskkill 失败"):
            await session_manager.stop_session("s1")

        assert "s1" in session_manager._sessions, "stop 失败必须保留会话以便重试"
        assert "s1" in session_manager._port_map
        assert fake.stop_calls == 1

        # 重试不再是「会话不存在」:第二次 stop 仍能触达同一会话
        with pytest.raises(RuntimeError, match="taskkill 失败"):
            await session_manager.stop_session("s1")
        assert fake.stop_calls == 2

    async def test_stop_success_pops_session(
        self, session_manager: SessionManager
    ):
        """正例:stop 成功 → _sessions/_port_map 都清掉。"""
        fake = self._make_fake_session()
        session_manager._sessions["s2"] = fake
        session_manager._port_map["s2"] = (5678, 11)

        result = await session_manager.stop_session("s2")
        assert result == "会话 's2' 已关闭。"
        assert "s2" not in session_manager._sessions
        assert "s2" not in session_manager._port_map


class TestTclServerScript:
    """scripts/vivado_mcp_server.tcl 回归锁:控制字符转义 + 只绑回环。"""

    @pytest.fixture
    def tcl_path(self):
        from pathlib import Path

        return (
            Path(__file__).resolve().parent.parent
            / "scripts"
            / "vivado_mcp_server.tcl"
        )

    @pytest.fixture
    def tcl_source(self, tcl_path) -> str:
        return tcl_path.read_text(encoding="utf-8")

    def test_server_binds_loopback_only(self, tcl_source):
        """socket -server 行必须带 -myaddr 127.0.0.1(防局域网任意 Tcl 执行)。"""
        import re

        assert re.search(
            r"socket -server \S+ -myaddr 127\.0\.0\.1", tcl_source
        ), "TCP server 必须只绑回环(-myaddr 127.0.0.1)"

    def test_json_escape_map_covers_control_chars(self, tcl_source):
        """转义映射必须含 0x00-0x1F 控制字符循环(\\u00XX)。"""
        assert "JSON_ESC_MAP" in tcl_source
        assert "$__i < 32" in tcl_source, "必须循环覆盖 0x00-0x1F"
        assert "%04x" in tcl_source, "控制字符必须转成 \\u00XX 形式"

    def test_start_has_rebind_guard(self, tcl_source):
        """::vmcp::start 重入守卫:server_sock 非空时先关旧 server 再绑新端口。

        消除「init.tcl 先绑 9999 + spawn -source 再绑 X → 一进程双端口」
        (0.3.22 审计 docs P1 根因):双端口会让默认 9999 的 probe/attach
        串到本应独立的实例上,且手动 GUI 接力因 9999 被占而失效。
        """
        import re

        assert re.search(r'if \{\$server_sock ne ""\}', tcl_source), (
            "::vmcp::start 必须有 server_sock 非空的重入守卫"
        )
        assert re.search(r"catch \{close \$server_sock\}", tcl_source), (
            "重入守卫必须先 close 旧 server socket"
        )
        assert "rebind" in tcl_source, "重入时必须 log 一行 rebind(关旧绑新)"
        # 守卫必须出现在 socket -server 绑定之前(先关旧再绑新的顺序)
        guard_pos = tcl_source.index('if {$server_sock ne ""}')
        bind_pos = tcl_source.index("socket -server")
        assert guard_pos < bind_pos, "守卫必须在绑新端口之前执行"

    def test_json_escape_roundtrip_via_tclsh(self, tcl_path, tmp_path):
        """功能验证:真 tclsh 跑 json_escape,Python json.loads 必须能解析。

        覆盖全部 0x00-0x1F(含 ESC 0x1B)+ 反斜杠 + 引号。
        本机 / CI 无 tclsh 时跳过(已有静态断言兜底)。
        """
        import json as json_mod
        import shutil
        import subprocess

        tclsh = shutil.which("tclsh")
        if tclsh is None:
            pytest.skip("tclsh 不在 PATH,跳过 Tcl 功能验证")

        out_path = tmp_path / "out.json"
        driver = tmp_path / "driver.tcl"
        driver_src = (
            "namespace eval ::vmcp {}\n"
            'set f [open "' + tcl_path.as_posix() + '" r]\n'
            "set src [read $f]\n"
            "close $f\n"
            "# 屏蔽末尾的 ::vmcp::start 调用,不真起 socket server\n"
            'set patched [string map [list "::vmcp::start\\n"'
            ' "#::vmcp::start\\n"] $src]\n'
            "eval $patched\n"
            'set s "a"\n'
            "for {set i 0} {$i < 32} {incr i} { append s [format %c $i] }\n"
            'append s [format "%c %c end" 92 34]\n'
            "set escaped [::vmcp::json_escape $s]\n"
            'set out [open "' + out_path.as_posix() + '" w]\n'
            "fconfigure $out -translation binary\n"
            "puts -nonewline $out [encoding convertto utf-8 "
            '"\\{\\"rc\\":0,\\"output\\":\\"$escaped\\"\\}"]\n'
            "close $out\n"
        )
        driver.write_text(driver_src, encoding="utf-8")
        proc = subprocess.run(
            [tclsh, str(driver)], capture_output=True, timeout=30
        )
        assert proc.returncode == 0, f"tclsh 执行失败: {proc.stderr!r}"

        obj = json_mod.loads(out_path.read_bytes().decode("utf-8"))
        expect = (
            "a" + "".join(chr(i) for i in range(32)) + "\\ \" end"
        )
        assert obj["output"] == expect, "控制字符必须无损往返"

    def test_start_call_guarded_by_gui_mode(self, tcl_source):
        """脚本末尾的 ::vmcp::start 只在 GUI(或无 rdi::mode)立即调用,其余推迟判断。

        已 install 的机器上 tcl/batch 模式也会 source 本脚本;不拦就会抢 9999,
        mode="gui" 默认 probe 9999 时接管到看不见的无界面会话。
        但 init.tcl 阶段 GUI 的 rdi::mode 也是 "tcl",所以不能直接放弃,只能 after idle 推迟。
        """
        import re

        assert re.search(
            r'if \{!\[info exists ::rdi::mode\] \|\| \$::rdi::mode eq "gui"\} \{\s*'
            r"::vmcp::start\s*\} else \{[^}]*after idle ::vmcp::deferred_start\s*\}",
            tcl_source,
        ), "非 GUI 加载时必须 after idle 推迟,而不是直接放弃或立即启动"
        body = tcl_source[tcl_source.index("proc ::vmcp::deferred_start") :]
        assert re.search(r'if \{\$server_sock ne ""\} \{\s*return', body), (
            "延迟回调必须在 server 已启动时跳过,不能 rebind 关掉分配端口"
        )

    @staticmethod
    def _run_tclsh(tmp_path, lines: list[str]) -> str:
        import shutil
        import subprocess

        tclsh = shutil.which("tclsh")
        if tclsh is None:
            pytest.skip("tclsh 不在 PATH,跳过 Tcl 功能验证")
        driver = tmp_path / "driver.tcl"
        driver.write_text("\n".join(lines) + "\n", encoding="utf-8")
        proc = subprocess.run(
            [tclsh, str(driver)], capture_output=True, text=True, timeout=30
        )
        assert proc.returncode == 0, f"tclsh 执行失败: {proc.stderr!r}"
        return proc.stdout

    @staticmethod
    def _free_port() -> int:
        import socket

        # 测试绝不用 9999(开发机可能真有 Vivado 在跑),用 OS 分配的空闲端口
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    @pytest.mark.parametrize("mode", ["tcl", "batch"])
    def test_headless_mode_never_listens_via_tclsh(self, tcl_path, tmp_path, mode):
        """tcl/batch:加载时不监听;事件循环跑到延迟回调后仍不监听。"""
        port = self._free_port()
        out = self._run_tclsh(tmp_path, [
            "namespace eval ::rdi {}",
            f"set ::rdi::mode {mode}",
            f"set ::VMCP_PORT_PREF {port}",
            f'source "{tcl_path.as_posix()}"',
            'puts "LOAD_PORT=$::vmcp::active_port"',
            "update",
            'puts "IDLE_PORT=$::vmcp::active_port"',
        ])
        assert "LOAD_PORT=0" in out and "IDLE_PORT=0" in out, out
        assert f"vivado-mcp: {mode} mode, server not started" in out

    @pytest.mark.parametrize("mode", ["gui", None])
    def test_gui_or_plain_tclsh_starts_immediately_via_tclsh(
        self, tcl_path, tmp_path, mode
    ):
        """-source 阶段 mode 已是 gui / 裸 tclsh:加载时立即监听(不依赖事件循环)。"""
        port = self._free_port()
        lines = ["namespace eval ::rdi {}", f"set ::rdi::mode {mode}"] if mode else []
        out = self._run_tclsh(tmp_path, lines + [
            f"set ::VMCP_PORT_PREF {port}",
            f'source "{tcl_path.as_posix()}"',
            'puts "LOAD_PORT=$::vmcp::active_port"',
        ])
        assert f"LOAD_PORT={port}" in out, out

    def test_gui_from_init_tcl_starts_in_event_loop_via_tclsh(self, tcl_path, tmp_path):
        """用户手动开的 GUI:init.tcl 阶段 mode 是 tcl,事件循环里变成 gui 后才启动。"""
        port = self._free_port()
        out = self._run_tclsh(tmp_path, [
            "namespace eval ::rdi {}",
            "set ::rdi::mode tcl",
            f"set ::VMCP_PORT_PREF {port}",
            f'source "{tcl_path.as_posix()}"',
            'puts "LOAD_PORT=$::vmcp::active_port"',
            "set ::rdi::mode gui",
            "update",
            'puts "IDLE_PORT=$::vmcp::active_port"',
        ])
        assert "LOAD_PORT=0" in out and f"IDLE_PORT={port}" in out, out

    def test_mcp_spawned_gui_keeps_allocated_port_via_tclsh(self, tcl_path, tmp_path):
        """MCP 拉起的 GUI:init.tcl 推迟 + -source 立即绑分配端口,延迟回调不得 rebind。"""
        init_port = self._free_port()
        alloc_port = self._free_port()
        out = self._run_tclsh(tmp_path, [
            "namespace eval ::rdi {}",
            "set ::rdi::mode tcl",
            f"set ::VMCP_PORT_PREF {init_port}",
            f'source "{tcl_path.as_posix()}"',
            "set ::rdi::mode gui",
            f"set ::VMCP_PORT_PREF {alloc_port}",
            f'source "{tcl_path.as_posix()}"',
            "update",
            'puts "IDLE_PORT=$::vmcp::active_port"',
        ])
        assert f"IDLE_PORT={alloc_port}" in out, out
        assert "rebind" not in out, "延迟回调不能关掉分配端口重开"
        assert out.count("server ready on port") == 1, out
