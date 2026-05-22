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

    def test_list_empty(self, session_manager: SessionManager):
        """空管理器列表为空。"""
        assert session_manager.list_sessions() == []

    def test_default_vivado_path(self, session_manager: SessionManager):
        """默认路径正确存储。"""
        assert session_manager.default_vivado_path == "/fake/vivado"

    def test_get_validates_session_id(self, session_manager: SessionManager):
        """get 方法会验证 session_id 格式。"""
        with pytest.raises(ValueError, match="session_id"):
            session_manager.get("invalid;id")


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


class TestWinCurdirPolicyCheck:
    """0.3.16:Win 11 24H2+ NoDefaultCurrentDirectoryInExePath 检测。"""

    def test_non_windows_returns_empty(self, monkeypatch):
        """非 Windows 直接空,不读注册表。"""
        from vivado_mcp.tools import session_tools
        monkeypatch.setattr(session_tools.sys, "platform", "linux")
        assert session_tools._check_win_curdir_policy() == ""

    def test_windows_policy_disabled_returns_empty(self, monkeypatch):
        """注册表都没值 → 默认 = 不警告(避免老 Win 误报)。"""
        import sys

        if sys.platform != "win32":
            import pytest
            pytest.skip("Windows only")
        import winreg

        from vivado_mcp.tools import session_tools

        def fake_open(root, subkey, *a, **kw):
            raise FileNotFoundError("simulated empty registry")

        monkeypatch.setattr(winreg, "OpenKey", fake_open)
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
        assert _looks_like_run_failure("synth_design ERROR foo")
        assert _looks_like_run_failure("ERROR: launch_simulation failed")
        assert _looks_like_run_failure("Common 17-39 failed due to earlier errors")

    def test_looks_like_run_failure_negative(self):
        from vivado_mcp.server import _looks_like_run_failure
        assert not _looks_like_run_failure("ERROR: get_property failed: no such object")
        assert not _looks_like_run_failure("")
        assert not _looks_like_run_failure("INFO: all good")

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
