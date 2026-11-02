"""测试 fixtures：mock Vivado 进程、会话管理器等。"""

import logging
import tempfile

import pytest

from vivado_mcp.vivado.session_manager import SessionManager


@pytest.fixture(scope="session")
def _isolated_tempdir(tmp_path_factory):
    try:
        return tmp_path_factory.mktemp("session_tempdir")
    except OSError as exc:
        # 不让与 tmp_path 无关的测试也跟着报错;它们照旧使用系统临时目录。
        logging.getLogger(__name__).warning("无法创建隔离的临时目录: %s", exc)
        return None


@pytest.fixture(autouse=True)
def _no_real_tempdir(monkeypatch, _isolated_tempdir):
    """GuiSession spawn 会建工作目录和临时脚本,测试不能写到真实 %TEMP%。"""
    if _isolated_tempdir is not None:
        monkeypatch.setattr(tempfile, "tempdir", str(_isolated_tempdir))


@pytest.fixture
def session_manager() -> SessionManager:
    """创建一个不连接实际 Vivado 的 SessionManager 实例。"""
    return SessionManager(vivado_path="/fake/vivado")
