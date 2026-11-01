"""Win32 窗口枚举:发现 Vivado 弹窗、判断主窗口是否被禁用、向弹窗发 Esc。

依据 Vivado 2019.1 实测(Java AWT 窗口类名):
- 弹窗 class=``SunAwtDialog``,主窗口 class=``SunAwtFrame``。
- 启动期有 class=``SunAwtWindow``、标题为空的窗口,不是弹窗,靠"标题非空"排除。
- 模态弹窗存在时,主窗口的 ``IsWindowEnabled`` 为 False。

非 Windows 平台所有函数都是空操作。``ctypes.WinDLL`` 只在 Windows 分支内访问,
保证 Linux 上能正常 import。
"""

from __future__ import annotations

import ctypes
import functools
import logging
import sys
from dataclasses import dataclass

logger = logging.getLogger(__name__)

SUPPORTED = sys.platform == "win32"

_MAIN_WINDOW_CLASS = "SunAwtFrame"
_WM_KEYDOWN = 0x0100
_WM_KEYUP = 0x0101
_VK_ESCAPE = 0x1B


@dataclass(frozen=True)
class VivadoWindow:
    """Vivado 进程的一个顶层窗口。"""
    hwnd: int
    title: str
    class_name: str


@dataclass(frozen=True)
class _TopWindow:
    """EnumWindows 的原始结果,过滤逻辑只依赖它,可脱离 Win32 测试。"""
    hwnd: int
    pid: int
    title: str
    class_name: str
    visible: bool
    enabled: bool


@functools.lru_cache(maxsize=1)
def _user32():
    """加载 user32 并声明签名。独立实例,不改 ctypes.windll 的全局状态。"""
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.EnumWindows.argtypes = [
        ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
        wintypes.LPARAM,
    ]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsWindowEnabled.argtypes = [wintypes.HWND]
    user32.IsWindowEnabled.restype = wintypes.BOOL
    user32.PostMessageW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.PostMessageW.restype = wintypes.BOOL
    return user32


def _enum_top_windows() -> list[_TopWindow]:
    """枚举所有顶层窗口。只读,不发任何窗口消息。"""
    from ctypes import wintypes

    user32 = _user32()
    found: list[_TopWindow] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _lparam):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, 512)
        class_name = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_name, 256)
        found.append(_TopWindow(
            hwnd=int(hwnd or 0),
            pid=pid.value,
            title=title.value,
            class_name=class_name.value,
            visible=bool(user32.IsWindowVisible(hwnd)),
            enabled=bool(user32.IsWindowEnabled(hwnd)),
        ))
        return True

    if not user32.EnumWindows(callback, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return found


def _windows_of(pid: int) -> list[_TopWindow]:
    """取属于 pid 的可见顶层窗口。Win32 调用失败时记 warning 并返回空。"""
    if not SUPPORTED:
        return []
    try:
        windows = _enum_top_windows()
    except Exception as exc:
        logger.warning("枚举 Vivado (pid=%s) 窗口失败,本次不报告弹窗: %s", pid, exc)
        return []
    return [w for w in windows if w.pid == pid and w.visible]


def list_dialogs(pid: int) -> list[VivadoWindow]:
    """列出 pid 的弹窗:可见、标题非空、不是主窗口。"""
    return [
        VivadoWindow(hwnd=w.hwnd, title=w.title, class_name=w.class_name)
        for w in _windows_of(pid)
        if w.title and w.class_name != _MAIN_WINDOW_CLASS
    ]


def main_window_disabled(pid: int) -> bool:
    """主窗口是否被禁用:存在 ``SunAwtFrame`` 且全部 disabled。"""
    frames = [w for w in _windows_of(pid) if w.class_name == _MAIN_WINDOW_CLASS]
    return bool(frames) and not any(w.enabled for w in frames)


def post_escape(hwnd: int, pid: int, title: str) -> bool:
    """向窗口投递 Esc。PostMessage 不抢前台,也不影响用户当前窗口。

    枚举之后窗口可能已销毁,hwnd 还可能被别的进程复用。所以发键前重新确认
    窗口仍属于 pid、标题仍是 title,不一致就不发。
    """
    if not SUPPORTED:
        return False
    try:
        from ctypes import wintypes

        user32 = _user32()
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        current = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, current, 512)
        if owner.value != pid or current.value != title:
            logger.warning(
                "窗口 hwnd=%s 已变化(pid=%s, 标题=%r),期望 pid=%s, 标题=%r,不发 Esc",
                hwnd, owner.value, current.value, pid, title,
            )
            return False
        if not user32.PostMessageW(hwnd, _WM_KEYDOWN, _VK_ESCAPE, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        if not user32.PostMessageW(hwnd, _WM_KEYUP, _VK_ESCAPE, 0):
            raise ctypes.WinError(ctypes.get_last_error())
    except Exception as exc:
        logger.warning("向窗口 hwnd=%s 发 Esc 失败: %s", hwnd, exc)
        return False
    return True
