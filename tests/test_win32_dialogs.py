"""win32_dialogs 的过滤逻辑。假的是 EnumWindows 结果,不依赖真实窗口,Linux CI 也能跑。"""

from __future__ import annotations

import logging
import os
import sys
from unittest.mock import MagicMock

import pytest

from vivado_mcp.vivado import win32_dialogs
from vivado_mcp.vivado.win32_dialogs import _TopWindow

PID = 4321
TITLE = "Unable to Open File"


def _win(hwnd, title, class_name="SunAwtDialog", *, pid=PID, visible=True, enabled=True):
    return _TopWindow(hwnd, pid, title, class_name, visible, enabled)


@pytest.fixture
def windows(monkeypatch):
    """假 Windows 平台:返回的列表就是 EnumWindows 看到的全部顶层窗口。"""
    found: list[_TopWindow] = []
    monkeypatch.setattr(win32_dialogs, "SUPPORTED", True)
    monkeypatch.setattr(win32_dialogs, "_enum_top_windows", lambda: list(found))
    return found


class TestListDialogs:
    def test_keeps_only_dialogs_of_this_pid(self, windows):
        windows += [
            _win(1, "Unable to Open File"),
            _win(2, "Unable to Open File", pid=PID + 1),
        ]
        assert [w.hwnd for w in win32_dialogs.list_dialogs(PID)] == [1]

    def test_filters_main_window_and_untitled_and_hidden(self, windows):
        """依据实测:主窗口 SunAwtFrame;启动期 SunAwtWindow 标题为空,不是弹窗。"""
        windows += [
            _win(1, "Vivado 2019.1", "SunAwtFrame"),
            _win(2, "", "SunAwtWindow"),
            _win(3, "Hidden", visible=False),
            _win(4, "Background Task"),
        ]
        assert [(w.hwnd, w.title) for w in win32_dialogs.list_dialogs(PID)] == [
            (4, "Background Task")
        ]

    def test_enum_failure_returns_empty_and_warns(self, monkeypatch, caplog):
        def boom():
            raise OSError("EnumWindows failed")

        monkeypatch.setattr(win32_dialogs, "SUPPORTED", True)
        monkeypatch.setattr(win32_dialogs, "_enum_top_windows", boom)
        with caplog.at_level(logging.WARNING, logger="vivado_mcp.vivado.win32_dialogs"):
            assert win32_dialogs.list_dialogs(PID) == []
        assert "EnumWindows failed" in caplog.text


class TestMainWindowDisabled:
    def test_disabled_when_all_frames_disabled(self, windows):
        windows.append(_win(1, "Vivado", "SunAwtFrame", enabled=False))
        assert win32_dialogs.main_window_disabled(PID) is True

    def test_enabled_frame_means_not_disabled(self, windows):
        windows += [
            _win(1, "Vivado", "SunAwtFrame", enabled=False),
            _win(2, "Vivado 2", "SunAwtFrame", enabled=True),
        ]
        assert win32_dialogs.main_window_disabled(PID) is False

    def test_no_frame_means_not_disabled(self, windows):
        windows.append(_win(1, "Unable to Open File", enabled=False))
        assert win32_dialogs.main_window_disabled(PID) is False

    def test_other_pid_frame_ignored(self, windows):
        windows.append(_win(1, "Vivado", "SunAwtFrame", pid=PID + 1, enabled=False))
        assert win32_dialogs.main_window_disabled(PID) is False


class TestNonWindows:
    def test_all_functions_are_noops(self, monkeypatch):
        def must_not_run():
            raise AssertionError("非 Windows 不能碰 Win32")

        monkeypatch.setattr(win32_dialogs, "SUPPORTED", False)
        monkeypatch.setattr(win32_dialogs, "_enum_top_windows", must_not_run)
        monkeypatch.setattr(win32_dialogs, "_user32", must_not_run)
        assert win32_dialogs.list_dialogs(PID) == []
        assert win32_dialogs.main_window_disabled(PID) is False
        assert win32_dialogs.post_escape(1, PID, TITLE) is False


def _fake_user32(owner: int = PID, title: str = TITLE) -> MagicMock:
    """假 user32:hwnd 77 属于 owner,标题是 title。"""

    def get_pid(hwnd, out):
        out._obj.value = owner
        return 1

    def get_title(hwnd, buf, size):
        buf.value = title
        return len(title)

    user32 = MagicMock()
    user32.GetWindowThreadProcessId.side_effect = get_pid
    user32.GetWindowTextW.side_effect = get_title
    user32.PostMessageW.return_value = 1
    return user32


class TestPostEscape:
    def test_posts_key_down_and_up_only(self, monkeypatch):
        """只用 PostMessage,不调 SetForegroundWindow / SendInput,不抢前台。"""
        user32 = _fake_user32()
        monkeypatch.setattr(win32_dialogs, "SUPPORTED", True)
        monkeypatch.setattr(win32_dialogs, "_user32", lambda: user32)

        assert win32_dialogs.post_escape(77, PID, TITLE) is True

        assert [c.args for c in user32.PostMessageW.call_args_list] == [
            (77, 0x0100, 0x1B, 0),
            (77, 0x0101, 0x1B, 0),
        ]
        assert not user32.SetForegroundWindow.called

    @pytest.mark.parametrize(
        "owner, title",
        [(PID + 1, TITLE), (PID, "Save Project"), (0, "")],
        ids=["hwnd 被其他进程复用", "标题变了", "窗口已销毁"],
    )
    def test_rechecks_window_before_sending(self, monkeypatch, caplog, owner, title):
        """枚举和发键之间窗口可能已换人:不匹配就不发。"""
        user32 = _fake_user32(owner, title)
        monkeypatch.setattr(win32_dialogs, "SUPPORTED", True)
        monkeypatch.setattr(win32_dialogs, "_user32", lambda: user32)

        with caplog.at_level(logging.WARNING, logger="vivado_mcp.vivado.win32_dialogs"):
            assert win32_dialogs.post_escape(77, PID, TITLE) is False

        assert not user32.PostMessageW.called
        assert "不发 Esc" in caplog.text

    def test_failure_returns_false_and_warns(self, monkeypatch, caplog):
        def boom():
            raise OSError("user32 unavailable")

        monkeypatch.setattr(win32_dialogs, "SUPPORTED", True)
        monkeypatch.setattr(win32_dialogs, "_user32", boom)
        with caplog.at_level(logging.WARNING, logger="vivado_mcp.vivado.win32_dialogs"):
            assert win32_dialogs.post_escape(77, PID, TITLE) is False
        assert "user32 unavailable" in caplog.text


@pytest.mark.skipif(sys.platform != "win32", reason="只在 Windows 验证真实 ctypes 签名")
class TestRealWin32Smoke:
    def test_real_enum_returns_typed_windows(self):
        for window in win32_dialogs._enum_top_windows():
            assert isinstance(window.hwnd, int)
            assert isinstance(window.pid, int)
            assert isinstance(window.title, str)

    def test_real_recheck_rejects_invalid_hwnd(self):
        """真实 ctypes 签名:无效 hwnd 不发消息,也不抛异常。"""
        assert win32_dialogs.post_escape(0x7FFFFFF0, os.getpid(), TITLE) is False

    def test_this_process_has_no_vivado_main_window(self):
        assert win32_dialogs.main_window_disabled(os.getpid()) is False
