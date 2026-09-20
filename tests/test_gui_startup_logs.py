"""穿透 GuiSession.start 验证启动日志，不启动实际 Vivado。"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from vivado_mcp.vivado import gui_session
from vivado_mcp.vivado.base_session import SessionState
from vivado_mcp.vivado.gui_session import GuiSession


@pytest.fixture
def startup_env(monkeypatch, tmp_path):
    """只模拟进程和连接边界，真实执行 start 的日志创建及错误报告。"""
    log_root = tmp_path / "GUI startup logs"
    monkeypatch.setenv("VIVADO_MCP_LOG_DIR", str(log_root))
    monkeypatch.setattr(
        asyncio,
        "open_connection",
        AsyncMock(side_effect=ConnectionRefusedError("test listener not ready")),
    )
    sessions = []

    def make_session(*, attach_only=False, port=0):
        session = GuiSession(
            vivado_path=str(tmp_path / "vendor directory" / "vivado"),
            session_id="startup-logs",
            attach_only=attach_only,
            port=port,
        )
        monkeypatch.setattr(session, "_alloc_free_port", lambda: 45123)
        # 假 PID 绝不能进入真实 taskkill / kill 路径。
        monkeypatch.setattr(session, "_cleanup_failed_spawn", AsyncMock())
        monkeypatch.setattr(session, "_current_project_hint", AsyncMock(return_value=""))
        sessions.append(session)
        return session

    yield log_root, make_session

    for session in sessions:
        if session._tmp_script is not None:
            Path(session._tmp_script).unlink(missing_ok=True)
            gui_session._TMP_SCRIPTS.discard(session._tmp_script)


def _spawn_log_paths(args, kwargs):
    """从实际子进程参数发现日志，不依赖实现的文件命名或 helper。"""
    assert "-nolog" not in args
    assert "-log" in args
    startup_log = Path(args[args.index("-log") + 1])
    stdout = kwargs["stdout"]
    assert hasattr(stdout, "write"), "launcher stdout 必须持久保存，不能丢到 DEVNULL"
    assert kwargs["stderr"] == asyncio.subprocess.STDOUT
    launcher_log = Path(stdout.name)
    assert startup_log.is_absolute()
    assert launcher_log.is_absolute()
    assert startup_log != launcher_log
    return startup_log, launcher_log


def _install_launcher(
    monkeypatch,
    *,
    returncode=23,
    vivado_output=b"ERROR: vendor startup failed\n",
    launcher_output=b"ERROR: launcher startup failed\n",
):
    captured = {}

    async def spawn(*args, **kwargs):
        startup_log, launcher_log = _spawn_log_paths(args, kwargs)
        captured.update(
            startup_log=startup_log,
            launcher_log=launcher_log,
            stdout=kwargs["stdout"],
        )
        if vivado_output is not None:
            startup_log.write_bytes(vivado_output)
        kwargs["stdout"].write(launcher_output)
        return SimpleNamespace(pid=900001, returncode=returncode)

    spawn_mock = AsyncMock(side_effect=spawn)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn_mock)
    return spawn_mock, captured


def _connect_successfully(monkeypatch, session):
    writer = MagicMock()
    writer.is_closing.return_value = False
    monkeypatch.setattr(
        asyncio, "open_connection", AsyncMock(return_value=(asyncio.StreamReader(), writer))
    )
    monkeypatch.setattr(session, "_handshake", AsyncMock(return_value=True))


def _assert_reported_paths(message, captured):
    assert str(captured["startup_log"]) in message
    assert str(captured["launcher_log"]) in message
    assert captured["stdout"].closed, "父进程不能在 spawn 后继续持有 launcher 日志句柄"


async def test_success_keeps_both_logs_and_reports_paths(startup_env, monkeypatch):
    """成功启动仍保留双日志，状态只暴露路径，父进程释放输出句柄。"""
    log_root, make_session = startup_env
    session = make_session()
    spawn, captured = _install_launcher(monkeypatch, returncode=None)
    _connect_successfully(monkeypatch, session)

    await session.start(timeout=2.0)

    spawn.assert_awaited_once()
    assert session.state == SessionState.READY
    assert captured["startup_log"].parent == log_root
    assert captured["launcher_log"].parent == log_root
    assert captured["startup_log"].read_bytes() == b"ERROR: vendor startup failed\n"
    assert captured["launcher_log"].read_bytes() == b"ERROR: launcher startup failed\n"
    status = session.status_dict()
    assert status["startup_log"] == str(captured["startup_log"])
    assert status["launcher_log"] == str(captured["launcher_log"])
    assert "vendor startup failed" not in str(status)
    assert captured["stdout"].closed
    session._cleanup_failed_spawn.assert_not_awaited()


async def test_early_exit_includes_both_tails_and_paths(startup_env, monkeypatch):
    """先退出的 launcher 保留返回码及两种来源的错误，含 UTF-8 中文。"""
    _, make_session = startup_env
    session = make_session()
    _, captured = _install_launcher(
        monkeypatch,
        vivado_output="ERROR: 工程初始化失败\n".encode("utf-8"),
        launcher_output=b"ERROR: could not initialize the vendor runtime\n",
    )

    with pytest.raises(RuntimeError, match="returncode=23") as exc:
        await session.start(timeout=2.0)

    message = str(exc.value)
    assert "工程初始化失败" in message
    assert "could not initialize the vendor runtime" in message
    _assert_reported_paths(message, captured)
    assert session.state == SessionState.ERROR


async def test_timeout_keeps_diagnostics_and_calls_only_mock_cleanup(startup_env, monkeypatch):
    """超时仍报告双日志；测试拦截清理，不会操作真实进程。"""
    _, make_session = startup_env
    session = make_session()
    _, captured = _install_launcher(monkeypatch, returncode=None)

    with pytest.raises(RuntimeError, match="超时") as exc:
        await session.start(timeout=0.0)

    message = str(exc.value)
    assert "vendor startup failed" in message
    assert "launcher startup failed" in message
    _assert_reported_paths(message, captured)
    session._cleanup_failed_spawn.assert_awaited_once()
    assert session.state == SessionState.ERROR


async def test_missing_vendor_log_still_reports_launcher_failure(startup_env, monkeypatch):
    """Vivado 本体尚未启动也能保留包装器报错，不能只显示 code 1。"""
    _, make_session = startup_env
    session = make_session()
    _, captured = _install_launcher(
        monkeypatch,
        returncode=1,
        vivado_output=None,
        launcher_output=b"ERROR: required executable could not be found\n",
    )

    with pytest.raises(RuntimeError, match="returncode=1") as exc:
        await session.start(timeout=2.0)

    assert not captured["startup_log"].exists()
    assert "required executable could not be found" in str(exc.value)
    _assert_reported_paths(str(exc.value), captured)


async def test_spawn_failure_keeps_attempted_log_paths_and_closes_output(
    startup_env, monkeypatch
):
    """操作系统拒绝创建进程时保留原始错误和尝试路径，不泄漏句柄。"""
    _, make_session = startup_env
    session = make_session()
    captured = {}

    async def fail_spawn(*args, **kwargs):
        startup_log, launcher_log = _spawn_log_paths(args, kwargs)
        captured.update(
            startup_log=startup_log, launcher_log=launcher_log, stdout=kwargs["stdout"]
        )
        raise OSError("test executable cannot be started")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)

    with pytest.raises(RuntimeError, match="test executable cannot be started") as exc:
        await session.start(timeout=2.0)

    _assert_reported_paths(str(exc.value), captured)
    assert session.state == SessionState.ERROR


def _has_cp936_ansi():
    if sys.platform != "win32":
        return False
    import ctypes

    return ctypes.windll.kernel32.GetACP() == 936


@pytest.mark.skipif(not _has_cp936_ansi(), reason="GBK 解码依赖中文 Windows 的 CP936 ANSI")
async def test_cp936_launcher_and_vendor_logs_keep_chinese(startup_env, monkeypatch):
    """使用真实 GBK 文件字节穿透启动错误报告，不模拟解码函数。"""
    _, make_session = startup_env
    session = make_session()
    _, captured = _install_launcher(
        monkeypatch,
        vivado_output="ERROR: 无法初始化工程\n".encode("gbk"),
        launcher_output="ERROR: 找不到指定模块\n".encode("gbk"),
    )

    with pytest.raises(RuntimeError, match="returncode=23") as exc:
        await session.start(timeout=2.0)

    assert "无法初始化工程" in str(exc.value)
    assert "找不到指定模块" in str(exc.value)
    _assert_reported_paths(str(exc.value), captured)


async def test_log_read_failure_preserves_original_startup_error(
    startup_env, monkeypatch, caplog
):
    """诊断文件不可读时仍返回原始启动错误，并记录读取失败。"""
    log_root, make_session = startup_env
    session = make_session()
    _, captured = _install_launcher(monkeypatch)
    original_open = Path.open

    def fail_log_read(path, mode="r", *args, **kwargs):
        if path.parent == log_root and "r" in mode:
            raise PermissionError("test log read denied")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_log_read)

    with pytest.raises(RuntimeError, match="returncode=23") as exc:
        await session.start(timeout=2.0)

    _assert_reported_paths(str(exc.value), captured)
    assert "test log read denied" in caplog.text
    assert session.state == SessionState.ERROR


async def test_large_logs_read_only_bounded_tails(startup_env, monkeypatch):
    """限制真实 read 大小，而非读完整文件后仅裁剪输出。"""
    log_root, make_session = startup_env
    session = make_session()
    padding = b"x" * (2 * 1024 * 1024)
    _, captured = _install_launcher(
        monkeypatch,
        vivado_output=b"OLD_VENDOR_HEADER\n" + padding + b"\nFINAL_VENDOR_ERROR\n",
        launcher_output=b"OLD_LAUNCHER_HEADER\n" + padding + b"\nFINAL_LAUNCHER_ERROR\n",
    )
    original_open = Path.open
    read_sizes = []

    class TrackedRead:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def read(self, size=-1):
            read_sizes.append(size)
            assert 0 < size <= 16 * 1024, "启动诊断每次最多读取 16 KiB，不能 read() 全文件"
            return self.stream.read(size)

    def track_reads(path, mode="r", *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        if path.parent == log_root and "r" in mode:
            return TrackedRead(stream)
        return stream

    monkeypatch.setattr(Path, "open", track_reads)

    with pytest.raises(RuntimeError, match="returncode=23") as exc:
        await session.start(timeout=2.0)

    message = str(exc.value)
    assert read_sizes, "测试必须观察到诊断文件的实际读取"
    assert sum(read_sizes) <= 2 * 16 * 1024
    assert "FINAL_VENDOR_ERROR" in message
    assert "FINAL_LAUNCHER_ERROR" in message
    assert "OLD_VENDOR_HEADER" not in message
    assert "OLD_LAUNCHER_HEADER" not in message
    assert len(message) < 12000
    _assert_reported_paths(message, captured)


async def test_utf8_tail_handles_partial_characters_at_both_edges(startup_env, monkeypatch):
    """有界读取切开字符或文件最后一字符尚未写完，都不应弄乱中间错误。"""
    _, make_session = startup_env
    session = make_session()
    text = ("中" * 10000 + "\nERROR: 无法加载源文件\n").encode("utf-8")
    _, captured = _install_launcher(
        monkeypatch, vivado_output=text + b"\xe9", launcher_output=b"ERROR: launcher failed\n"
    )

    with pytest.raises(RuntimeError, match="returncode=23") as exc:
        await session.start(timeout=2.0)

    assert "无法加载源文件" in str(exc.value)
    assert "launcher failed" in str(exc.value)
    _assert_reported_paths(str(exc.value), captured)


@pytest.mark.parametrize("attach_only", [False, True], ids=["probe-hit", "explicit-attach"])
async def test_attach_does_not_spawn_or_create_logs(startup_env, monkeypatch, attach_only):
    """显式 attach 和 probe 命中都不能新建目录或伪造启动日志。"""
    log_root, make_session = startup_env
    session = make_session(attach_only=attach_only, port=45124)
    _connect_successfully(monkeypatch, session)
    spawn = AsyncMock(side_effect=AssertionError("attach 不允许启动进程"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)

    await session.start(timeout=2.0)

    assert session.state == SessionState.READY
    assert session.mode == "attach"
    assert session.connected_port == 45124
    assert session.attached_external is not attach_only
    spawn.assert_not_awaited()
    assert not log_root.exists()
    assert "startup_log" not in session.status_dict()
    assert "launcher_log" not in session.status_dict()
    assert session._tmp_script is None


async def test_retry_attaches_without_reporting_previous_spawn_logs(startup_env, monkeypatch):
    """同一对象失败后命中外部 GUI，不能把上一次启动日志当成 attach 证据。"""
    log_root, make_session = startup_env
    session = make_session(port=45124)
    spawn, captured = _install_launcher(monkeypatch)
    with pytest.raises(RuntimeError, match="returncode=23"):
        await session.start(timeout=2.0)

    previous_files = set(log_root.iterdir())
    _connect_successfully(monkeypatch, session)
    banner = await session.start(timeout=2.0)

    spawn.assert_awaited_once()
    assert session.mode == "attach"
    assert session.state == SessionState.READY
    assert set(log_root.iterdir()) == previous_files
    assert captured["startup_log"].exists(), "重试不能删除原始诊断证据"
    assert "startup_log" not in session.status_dict()
    assert "launcher_log" not in session.status_dict()
    assert str(captured["startup_log"]) not in banner


async def test_real_child_stdout_and_stderr_survive_launcher_exit(startup_env, monkeypatch):
    """真实受控 Python 子进程验证 OS 输出重定向，不运行 Vivado。"""
    _, make_session = startup_env
    session = make_session()
    original_spawn = asyncio.create_subprocess_exec
    captured = {}

    async def spawn_python(*args, **kwargs):
        startup_log, launcher_log = _spawn_log_paths(args, kwargs)
        captured.update(
            startup_log=startup_log, launcher_log=launcher_log, stdout=kwargs["stdout"]
        )
        # 只替换可执行文件与参数，保留 GuiSession 提供的真实重定向句柄。
        code = (
            "import os; "
            "os.write(1, b'CONTROLLED_CHILD_STDOUT\\n'); "
            "os.write(2, b'CONTROLLED_CHILD_STDERR\\n'); "
            "raise SystemExit(17)"
        )
        process = await original_spawn(sys.executable, "-c", code, **kwargs)
        try:
            await asyncio.wait_for(process.wait(), timeout=10.0)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn_python)

    with pytest.raises(RuntimeError, match="returncode=17") as exc:
        await session.start(timeout=2.0)

    message = str(exc.value)
    assert "CONTROLLED_CHILD_STDOUT" in message
    assert "CONTROLLED_CHILD_STDERR" in message
    assert not captured["startup_log"].exists()
    _assert_reported_paths(message, captured)
    launcher_bytes = captured["launcher_log"].read_bytes()
    assert b"CONTROLLED_CHILD_STDOUT" in launcher_bytes
    assert b"CONTROLLED_CHILD_STDERR" in launcher_bytes
