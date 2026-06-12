"""session_manager.py 单元测试。

测试 session_id 验证和基本管理逻辑（不启动实际 Vivado 进程）。
"""

import pytest

from vivado_mcp.vivado.session_manager import SessionManager, _validate_session_id


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


class TestWinCurdirPolicyCheck:
    """0.3.16:Win 11 24H2+ NoDefaultCurrentDirectoryInExePath 检测。"""

    def test_non_windows_returns_empty(self, monkeypatch):
        """非 Windows 直接空,不读注册表。"""
        from vivado_mcp.tools import session_tools
        monkeypatch.setattr(session_tools.sys, "platform", "linux")
        assert session_tools._check_win_curdir_policy() == ""

    def _mock_no_registry(self, monkeypatch):
        """两处注册表都 FileNotFoundError(无显式键)。"""
        import sys

        if sys.platform != "win32":
            import pytest
            pytest.skip("Windows only")
        import winreg

        def fake_open(root, subkey, *a, **kw):
            raise FileNotFoundError("simulated empty registry")

        monkeypatch.setattr(winreg, "OpenKey", fake_open)

    def test_no_registry_old_win_returns_empty(self, monkeypatch):
        """注册表无值 + 老 Win(build < 26100) → 不警告(默认 0)。"""
        from vivado_mcp.tools import session_tools

        self._mock_no_registry(monkeypatch)
        monkeypatch.setattr(
            session_tools, "_is_win11_24h2_or_newer", lambda: False
        )
        assert session_tools._check_win_curdir_policy() == ""

    def test_no_registry_win11_24h2_returns_warning(self, monkeypatch):
        """注册表无值 + Win 11 24H2+ → 警告(默认 1,0.3.16 实测漏报的修复)。"""
        from vivado_mcp.tools import session_tools

        self._mock_no_registry(monkeypatch)
        monkeypatch.setattr(
            session_tools, "_is_win11_24h2_or_newer", lambda: True
        )
        warn = session_tools._check_win_curdir_policy()
        assert "24H2" in warn
        assert "reg add" in warn
        assert "compile.bat" in warn

    def test_explicit_disabled_returns_empty(self, monkeypatch):
        """HKCU 显式 = 0 → 不警告(用户已 opt-out,根治命令的效果)。"""
        import sys

        if sys.platform != "win32":
            import pytest
            pytest.skip("Windows only")
        import winreg

        from vivado_mcp.tools import session_tools

        class FakeKey:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(winreg, "OpenKey", lambda *a, **kw: FakeKey())
        monkeypatch.setattr(
            winreg, "QueryValueEx", lambda k, n: (0, winreg.REG_DWORD)
        )
        # 即使在 24H2 上,显式 = 0 也不警告
        monkeypatch.setattr(
            session_tools, "_is_win11_24h2_or_newer", lambda: True
        )
        assert session_tools._check_win_curdir_policy() == ""

    def test_windows_policy_enabled_returns_warning(self, monkeypatch):
        """注册表 =1 → 警告含 reg add 命令、键值名、根治步骤。"""
        import sys

        if sys.platform != "win32":
            import pytest
            pytest.skip("Windows only")
        import winreg

        from vivado_mcp.tools import session_tools

        class FakeKey:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_open(root, subkey, *a, **kw):
            return FakeKey()

        def fake_query(key, name):
            assert name == "NoDefaultCurrentDirectoryInExePath"
            return (1, winreg.REG_DWORD)

        monkeypatch.setattr(winreg, "OpenKey", fake_open)
        monkeypatch.setattr(winreg, "QueryValueEx", fake_query)

        warn = session_tools._check_win_curdir_policy()
        assert "NoDefaultCurrentDirectoryInExePath" in warn
        assert "reg add" in warn
        assert "HKCU" in warn
        # Vivado 真根因提示
        assert "compile.bat" in warn


class TestSafeExecuteDiagHint:
    """server._safe_execute:Tcl run/sim 失败时追加诊断 hint。"""

    def test_looks_like_run_failure_synth(self):
        from vivado_mcp.server import _looks_like_run_failure
        assert _looks_like_run_failure("synth_design ERROR foo", "")
        assert _looks_like_run_failure("ERROR: launch_simulation failed", "")
        assert _looks_like_run_failure(
            "Common 17-39 failed due to earlier errors", ""
        )
        # 0.3.20:命令文本也能命中(haystack = command + output)
        assert _looks_like_run_failure("nothing", "launch_runs synth_1")

    def test_looks_like_run_failure_negative(self):
        from vivado_mcp.server import _looks_like_run_failure
        assert not _looks_like_run_failure(
            "ERROR: get_property failed: no such object", ""
        )
        assert not _looks_like_run_failure("", "")
        assert not _looks_like_run_failure("INFO: all good", "puts 3.3")

    @pytest.mark.asyncio
    async def test_safe_execute_appends_hint_on_run_failure(self):
        from unittest.mock import AsyncMock

        from vivado_mcp.server import _safe_execute
        from vivado_mcp.vivado.tcl_utils import TclResult

        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=TclResult(
                output="ERROR: [Common 17-39] 'launch_simulation' failed",
                return_code=1,
                is_error=True,
            )
        )

        result = await _safe_execute(session, "launch_simulation", 30.0, "run_tcl")
        assert "get_critical_warnings" in result
        assert "TclStackFree" in result or "messageDb" in result

    @pytest.mark.asyncio
    async def test_safe_execute_no_hint_on_success(self):
        from unittest.mock import AsyncMock

        from vivado_mcp.server import _safe_execute
        from vivado_mcp.vivado.tcl_utils import TclResult

        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=TclResult(
                output="3.3", return_code=0, is_error=False
            )
        )
        result = await _safe_execute(session, "puts 3.3", 30.0, "run_tcl")
        assert "get_critical_warnings" not in result

    @pytest.mark.asyncio
    async def test_safe_execute_no_hint_on_unrelated_error(self):
        from unittest.mock import AsyncMock

        from vivado_mcp.server import _safe_execute
        from vivado_mcp.vivado.tcl_utils import TclResult

        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=TclResult(
                output="ERROR: get_property failed: no such object 'foo'",
                return_code=1,
                is_error=True,
            )
        )
        result = await _safe_execute(session, "get_property foo bar", 30.0, "run_tcl")
        assert "get_critical_warnings" not in result


class TestQuirkHintsA1A2:
    """0.3.20 W hints:A1 open_wave_config 误报 + A2 wave 失败状态泄漏。"""

    def test_open_wave_spurious_trigger_match(self):
        from vivado_mcp.server import _looks_like_open_wave_spurious
        out = "ERROR: [Common 17-39] 'open_wave_config' failed due to earlier errors."
        assert _looks_like_open_wave_spurious(out, "open_wave_database foo.wdb")
        # 不含该关键串 → 不命中
        assert not _looks_like_open_wave_spurious("ERROR: other failure", "any cmd")

    def test_wave_failure_cleanup_trigger_match(self):
        from vivado_mcp.server import _looks_like_wave_failure_needs_cleanup
        # wave 类命令 + err
        assert _looks_like_wave_failure_needs_cleanup(
            "ERROR: bad",
            "open_wave_database foo.wdb",
        )
        assert _looks_like_wave_failure_needs_cleanup(
            "'open_wave_config' failed",
            "add_wave /tb/clk",
        )
        # 非 wave 类命令即使 err 也不触发 cleanup 提示
        assert not _looks_like_wave_failure_needs_cleanup(
            "ERROR: synth_design failed",
            "synth_design",
        )
        # wave 类命令但无 err 不触发
        assert not _looks_like_wave_failure_needs_cleanup(
            "info: ok",
            "open_wave_database foo.wdb",
        )

    @pytest.mark.asyncio
    async def test_safe_execute_appends_a1_a2_hints_on_open_wave_spurious(self):
        from unittest.mock import AsyncMock

        from vivado_mcp.server import _safe_execute
        from vivado_mcp.vivado.tcl_utils import TclResult

        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=TclResult(
                output=(
                    "ERROR: [Common 17-39] 'open_wave_config' failed "
                    "due to earlier errors."
                ),
                return_code=1,
                is_error=True,
            )
        )
        result = await _safe_execute(
            session, "open_wave_database C:/path/x.wdb", 30.0, "run_tcl"
        )
        # A1 hint 关键标记:current_sim + current_wave_config + get_scopes
        assert "current_sim" in result
        assert "current_wave_config" in result
        assert "get_scopes" in result
        # A2 hint 关键标记:close_sim 清理片段 + stop_session 重启兜底
        assert "close_sim" in result
        assert "stop_session" in result
        # 同时也触发 A2(wave 类命令 + err)
        assert "close_wave_config" in result

    @pytest.mark.asyncio
    async def test_safe_execute_no_hint_on_open_wave_success(self):
        from unittest.mock import AsyncMock

        from vivado_mcp.server import _safe_execute
        from vivado_mcp.vivado.tcl_utils import TclResult

        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=TclResult(
                output="", return_code=0, is_error=False
            )
        )
        result = await _safe_execute(
            session, "open_wave_database C:/path/x.wdb", 30.0, "run_tcl"
        )
        assert "current_sim" not in result
        assert "close_sim" not in result

    @pytest.mark.asyncio
    async def test_safe_execute_does_not_rewrite_original_err(self):
        """W 模式核心契约:原始 Vivado 输出永远不改写,只追加。"""
        from unittest.mock import AsyncMock

        from vivado_mcp.server import _safe_execute
        from vivado_mcp.vivado.tcl_utils import TclResult

        original = (
            "ERROR: [Common 17-39] 'open_wave_config' failed due to earlier errors."
        )
        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=TclResult(
                output=original, return_code=1, is_error=True
            )
        )
        result = await _safe_execute(
            session, "open_wave_database foo.wdb", 30.0, "run_tcl"
        )
        # 原 err 字符串必须完整保留在返回里
        assert original in result


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


class TestWinCurdirPolicyOSErrorLogging:
    """读注册表 OSError 必须 logger.warning 留痕(错误处理铁律,禁 debug)。"""

    def test_registry_oserror_logged_as_warning(self, monkeypatch, caplog):
        import logging
        import sys

        if sys.platform != "win32":
            pytest.skip("Windows only")
        import winreg

        from vivado_mcp.tools import session_tools

        def fake_open(root, subkey, *a, **kw):
            raise OSError("拒绝访问(模拟)")

        monkeypatch.setattr(winreg, "OpenKey", fake_open)
        # 老 Win 默认 0:两处读失败 + 无显式值 → 返回空(语义不变)
        monkeypatch.setattr(
            session_tools, "_is_win11_24h2_or_newer", lambda: False
        )

        with caplog.at_level(
            logging.WARNING, logger="vivado_mcp.tools.session_tools"
        ):
            result = session_tools._check_win_curdir_policy()

        assert result == "", "读失败时返回值语义不变(降级为按版本默认判)"
        warning_records = [
            r for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert warning_records, "OSError 必须以 WARNING 级别留痕"
        assert "读注册表" in caplog.text
        assert "拒绝访问(模拟)" in caplog.text, "必须带具体异常信息"

    def test_filenotfound_path_no_warning(self, monkeypatch, caplog):
        """反例:键不存在(FileNotFoundError)走正常 fallthrough,不发 warning。"""
        import logging
        import sys

        if sys.platform != "win32":
            pytest.skip("Windows only")
        import winreg

        from vivado_mcp.tools import session_tools

        def fake_open(root, subkey, *a, **kw):
            raise FileNotFoundError("no key")

        monkeypatch.setattr(winreg, "OpenKey", fake_open)
        monkeypatch.setattr(
            session_tools, "_is_win11_24h2_or_newer", lambda: False
        )

        with caplog.at_level(
            logging.WARNING, logger="vivado_mcp.tools.session_tools"
        ):
            assert session_tools._check_win_curdir_policy() == ""

        assert "读注册表" not in caplog.text


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
