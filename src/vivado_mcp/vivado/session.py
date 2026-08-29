"""SubprocessSession：Vivado ``-mode tcl`` 子进程管理与哨兵通信协议。

subprocess 实现（两种会话模式之一）。负责：
- 启动/停止 Vivado TCL 子进程
- 通过 catch + sentinel 模式可靠地收发命令
- asyncio.Lock 串行化并发请求
- 超时控制与异常处理

另一种实现见 ``gui_session.py`` (GUI + TCP)。公共接口定义在 ``base_session.py``。
"""

import asyncio
import collections
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from vivado_mcp.config import validate_vivado_launcher
from vivado_mcp.vivado.base_session import BaseSession, SessionState
from vivado_mcp.vivado.tcl_utils import (
    TclResult,
    clean_output,
    decode_vivado_output,
    generate_sentinel,
    make_sentinel_pattern,
    wrap_command,
)

logger = logging.getLogger(__name__)


_CMD_META_CHARS = frozenset("^&|<>()%")
_CMD_QUOTE_TRIGGER_CHARS = frozenset(" \t^&|<>()%")


def _quote_windows_batch_arg(value: str) -> str:
    """Render one trusted launcher argument for cmd.exe batch re-parsing.

    ``subprocess.list2cmdline`` implements the Microsoft C runtime argv rules,
    not cmd.exe grammar.  In particular, its embedded quotes are surfaced as
    literal ``\"`` by ``cmd /c`` and metacharacters such as ``&`` split the
    command.  AMD's launcher also compares raw ``%1``/``%2`` values, so safe
    option tokens must remain unquoted.  Paths and metacharacter-bearing values
    are quoted only when required.  Double quotes/control characters and ``!``
    are rejected rather than guessed: AMD ``loader.bat`` enables delayed
    expansion before forwarding ``%*``, which silently removes or expands a
    literal exclamation mark even when the outer cmd.exe roundtrip succeeds.
    """
    if any(char in value for char in ('"', "!", "\x00", "\r", "\n")):
        raise ValueError(
            "Windows batch launcher 参数含不支持的引号、感叹号或控制字符: "
            f"{value!r}"
        )
    escaped = "".join(
        f"^{char}" if char in _CMD_META_CHARS else char
        for char in value
    )
    if value and not any(char in _CMD_QUOTE_TRIGGER_CHARS for char in value):
        return escaped
    return f'"{escaped}"'


def windows_batch_command(vivado_path: str, *args: str) -> str:
    """Build one cmd-safe command while preserving raw vendor option tokens."""
    return " ".join(
        _quote_windows_batch_arg(value) for value in (vivado_path, *args)
    )


async def create_vivado_subprocess(
    vivado_path: str,
    *args: str,
    **kwargs,
) -> asyncio.subprocess.Process:
    """Start a Vivado launcher without bypassing the vendor environment.

    Real executables use exact argv boundaries.  Windows ``.bat``/``.cmd``
    launchers use cmd's shell entry with explicit cmd escaping; attempting to
    pass the same nested command through ``create_subprocess_exec`` is not
    round-trip safe on Windows.
    """
    vivado_path = validate_vivado_launcher(vivado_path)
    if sys.platform == "win32" and vivado_path.lower().endswith((".bat", ".cmd")):
        return await asyncio.create_subprocess_shell(
            windows_batch_command(vivado_path, *args),
            executable=os.environ.get("COMSPEC", "cmd.exe"),
            **kwargs,
        )
    return await asyncio.create_subprocess_exec(vivado_path, *args, **kwargs)


def _paths_match(expected: str, actual: str) -> bool:
    """Compare resolved local paths using the host platform's case rules."""
    if not expected or not actual:
        return False
    expected_key = os.path.normcase(os.path.normpath(str(Path(expected).resolve())))
    actual_key = os.path.normcase(os.path.normpath(str(Path(actual).resolve())))
    return expected_key == actual_key

# stderr 缓冲区保留的最近行数（避免内存无限增长）
_STDERR_RING_SIZE = 200

_TCL_STARTUP_PROBE = """
puts "VMCP_READY"
set __vmcp_project ""
set __vmcp_xpr ""
if {[catch {current_project} __vmcp_project]} { set __vmcp_project "" }
if {$__vmcp_project ne ""} {
    if {![catch {get_property DIRECTORY $__vmcp_project} __vmcp_dir]
            && ![catch {get_property NAME $__vmcp_project} __vmcp_name]} {
        set __vmcp_xpr [file normalize [file join $__vmcp_dir "${__vmcp_name}.xpr"]]
    }
}
set __vmcp_ipi [expr {
    [llength [info commands open_bd_design]] > 0
    && [llength [info commands get_bd_pins]] > 0
}]
puts "VMCP_TCL_ID|project=$__vmcp_project|xpr=$__vmcp_xpr|ip_integrator=$__vmcp_ipi"
""".strip()


class SubprocessSession(BaseSession):
    """Vivado TCL 交互式子进程会话（`-mode tcl` 无头批处理）。

    通过 asyncio subprocess 管理一个 `vivado -mode tcl` 进程，
    使用 catch + UUID sentinel 协议实现可靠的命令执行与输出采集。
    """

    def __init__(
        self,
        vivado_path: str,
        session_id: str = "default",
        startup_project_path: str | None = None,
        require_ip_integrator: bool = False,
    ):
        super().__init__(vivado_path=vivado_path, session_id=session_id)
        self._startup_project_path = startup_project_path
        self._require_ip_integrator = require_ip_integrator
        self._identity: dict[str, str] = {}
        self._launch_args: tuple[str, ...] = ()
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._closing = False
        # 调用方超时后仍由该任务继续读取到本命令的 UUID sentinel。
        # 在任务完成前禁止发送下一条命令，避免迟到输出污染后续响应。
        self._inflight_task: asyncio.Task[TclResult] | None = None
        # B5 修复：持续采集 stderr，失败时附加到 output（否则 Vivado 错误消息全丢）
        self._stderr_buffer: collections.deque[str] = collections.deque(
            maxlen=_STDERR_RING_SIZE
        )
        self._stderr_task: asyncio.Task | None = None

    @property
    def mode(self) -> str:
        return "tcl"

    @property
    def is_alive(self) -> bool:
        return (
            self._process is not None
            and self._process.returncode is None
        )

    @property
    def startup_project_path(self) -> str | None:
        """Exact XPR requested on the Vivado Tcl command line, if any."""
        return self._startup_project_path

    def status_dict(self) -> dict:
        """Add project-first startup and Tcl identity evidence."""
        status = super().status_dict()
        if self._startup_project_path is not None:
            status["startup_project_path"] = self._startup_project_path
        if self._identity:
            status["identity"] = dict(self._identity)
        status["require_ip_integrator"] = self._require_ip_integrator
        return status

    async def start(self, timeout: float = 60.0) -> str:
        """启动 Vivado TCL 子进程。

        Args:
            timeout: 等待 Vivado 启动完成的超时秒数。

        Returns:
            Vivado 启动横幅（版本信息等）。

        Raises:
            RuntimeError: 进程启动失败或超时。
        """
        if self.is_alive:
            return f"会话 '{self.session_id}' 已在运行中。"

        self._closing = False
        self._state = SessionState.STARTING
        logger.info("启动 Vivado 会话 '%s': %s", self.session_id, self.vivado_path)

        try:
            launch_args = ["-mode", "tcl"]
            if self._startup_project_path is not None:
                launch_args.append(self._startup_project_path)
            launch_args.extend(("-nojournal", "-nolog"))
            self._launch_args = tuple(launch_args)
            self._process = await create_vivado_subprocess(
                self.vivado_path,
                *launch_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, FileNotFoundError) as e:
            self._state = SessionState.ERROR
            raise RuntimeError(f"无法启动 Vivado: {e}") from e

        # Drain stderr immediately.  Waiting until READY can deadlock startup if
        # the vendor launcher or Vivado fills the stderr pipe first.
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        # 等待 Vivado 启动完成（读取初始横幅）
        banner = await self._read_startup_banner(timeout)
        self._state = SessionState.READY
        self._start_time = time.time()

        logger.info("Vivado 会话 '%s' 启动成功", self.session_id)
        return banner

    async def _drain_stderr(self) -> None:
        """后台任务：持续读取 Vivado stderr，存入环形缓冲区。

        Vivado 的错误消息（含 ERROR:/CRITICAL WARNING:）部分走 stderr，
        若不持续读取则 pipe 可能阻塞，且错误信息丢失。
        """
        assert self._process and self._process.stderr
        try:
            while True:
                raw = await self._process.stderr.readline()
                if not raw:
                    break
                line = decode_vivado_output(raw).rstrip("\r\n")
                if line:
                    self._stderr_buffer.append(line)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("[%s] stderr drain exception: %s", self.session_id, e)

    def _recent_stderr(self, max_lines: int = 30) -> str:
        """返回 stderr 缓冲区最近 N 行，用于失败时附加诊断。"""
        lines = list(self._stderr_buffer)[-max_lines:]
        return "\n".join(lines)

    async def _read_startup_banner(self, timeout: float) -> str:
        """读取 Vivado 启动时的初始输出（横幅）。

        发送一个无害命令 + sentinel 来检测 Vivado 何时就绪。
        """
        sentinel = generate_sentinel()
        pattern = make_sentinel_pattern(sentinel)

        # 发送探测命令
        probe = wrap_command(_TCL_STARTUP_PROBE, sentinel)
        assert self._process and self._process.stdin and self._process.stdout
        self._process.stdin.write(probe.encode("utf-8"))
        await self._process.stdin.drain()

        lines: list[str] = []
        try:
            deadline = asyncio.get_event_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    raise asyncio.TimeoutError()

                raw = await asyncio.wait_for(
                    self._process.stdout.readline(),
                    timeout=remaining,
                )
                if not raw:
                    # EOF — 进程意外退出
                    if self._process.returncode is None:
                        try:
                            await asyncio.wait_for(self._process.wait(), timeout=2.0)
                        except asyncio.TimeoutError:
                            pass
                    if self._stderr_task and not self._stderr_task.done():
                        try:
                            await asyncio.wait_for(
                                asyncio.shield(self._stderr_task), timeout=2.0
                            )
                        except asyncio.TimeoutError:
                            pass
                    stderr_out = self._recent_stderr(max_lines=80)
                    stdout_out = clean_output("\n".join(lines[-80:]))
                    raise RuntimeError(
                        "Vivado Tcl launcher/process 在 READY 前退出。"
                        f"returncode={self._process.returncode}; "
                        f"launcher={self.vivado_path}; args={self._launch_args!r}"
                        + (
                            f"\n--- startup stdout tail ---\n{stdout_out}"
                            if stdout_out else ""
                        )
                        + (f"\n--- stderr tail ---\n{stderr_out}" if stderr_out else "")
                    )

                line = decode_vivado_output(raw).rstrip("\r\n")
                m = pattern.search(line)
                if m:
                    # 找到 sentinel，启动完成
                    break
                lines.append(line)

        except asyncio.TimeoutError:
            self._state = SessionState.ERROR
            stderr_out = self._recent_stderr(max_lines=80)
            stdout_out = clean_output("\n".join(lines[-80:]))
            raise RuntimeError(
                f"Vivado 启动超时（{timeout}s）。"
                f"returncode={self._process.returncode}; "
                f"launcher={self.vivado_path}; args={self._launch_args!r}。"
                + (
                    f"\n--- startup stdout tail ---\n{stdout_out}"
                    if stdout_out else ""
                )
                + (f"\n--- stderr tail ---\n{stderr_out}" if stderr_out else "")
            )

        identity: dict[str, str] = {}
        banner_lines: list[str] = []
        for line in lines:
            if line.startswith("VMCP_TCL_ID|"):
                for item in line.split("|")[1:]:
                    key, sep, value = item.partition("=")
                    if sep and key:
                        identity[key] = value
                continue
            banner_lines.append(line)

        if self._startup_project_path is not None:
            actual_xpr = identity.get("xpr", "")
            if not actual_xpr or not _paths_match(
                self._startup_project_path, actual_xpr
            ):
                raise RuntimeError(
                    "Vivado Tcl startup project 身份不匹配: "
                    f"expected={self._startup_project_path}, "
                    f"actual={actual_xpr or '<无>'}"
                )
        if (
            self._require_ip_integrator
            and identity.get("ip_integrator") != "1"
        ):
            raise RuntimeError(
                "Vivado Tcl session 的 IP Integrator commands 未就绪: "
                "open_bd_design/get_bd_pins 尚未注册"
            )
        self._identity = identity
        return clean_output("\n".join(banner_lines))

    async def execute(
        self,
        tcl_command: str,
        timeout: float = 120.0,
    ) -> TclResult:
        """执行一条 Tcl 命令并返回结果。

        通过 asyncio.Lock 确保同一时刻只有一条命令在执行。

        Args:
            tcl_command: Tcl 命令文本（可多行）。
            timeout: 命令执行超时秒数。

        Returns:
            TclResult 包含输出文本、返回码和错误标志。

        Raises:
            RuntimeError: 会话未启动或已停止。
            asyncio.TimeoutError: 命令执行超时。
        """
        if self._closing or not self.is_alive:
            raise RuntimeError(
                f"会话 '{self.session_id}' 正在关闭或未运行。请先调用 start_session。"
            )

        async with self._lock:
            if self._closing:
                raise RuntimeError(f"会话 '{self.session_id}' 正在关闭，拒绝执行新命令。")
            if self._inflight_task is not None and not self._inflight_task.done():
                raise RuntimeError(
                    f"会话 '{self.session_id}' 的上一条命令仍在执行。"
                    "请稍后重试；不会向同一 Vivado 会话并发发送新命令。"
                )

            self._state = SessionState.BUSY
            task = asyncio.create_task(self._execute_impl(tcl_command))
            self._inflight_task = task
            task.add_done_callback(self._on_inflight_done)
            try:
                result = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
                self._state = SessionState.READY
                return result
            except asyncio.TimeoutError:
                # shield 保证底层 reader 继续持有本命令的响应，直到读到专属 sentinel。
                self._state = SessionState.BUSY
                raise asyncio.TimeoutError(
                    f"命令执行超时（{timeout}s），Vivado 可能仍在执行。\n"
                    f"会话: {self.session_id}\n"
                    f"命令: {tcl_command[:200]}"
                ) from None
            except Exception:
                # 检查进程是否还活着
                if self.is_alive:
                    self._state = SessionState.READY
                else:
                    self._state = SessionState.ERROR
                raise

    def _on_inflight_done(self, task: asyncio.Task[TclResult]) -> None:
        """收尾迟到响应；回调会取走后台异常，避免未检索异常告警。"""
        try:
            exc = task.exception()
            if exc is not None:
                logger.debug("[%s] 在途命令结束时异常: %s", self.session_id, exc)
        except asyncio.CancelledError:
            pass

        if self._inflight_task is task:
            self._inflight_task = None
            if self._state == SessionState.BUSY:
                self._state = SessionState.READY if self.is_alive else SessionState.ERROR

    async def _execute_impl(
        self,
        tcl_command: str,
    ) -> TclResult:
        """内部执行实现（不加锁）。"""
        assert self._process and self._process.stdin and self._process.stdout

        sentinel = generate_sentinel()
        pattern = make_sentinel_pattern(sentinel)
        wrapped = wrap_command(tcl_command, sentinel)

        # 发送命令
        logger.debug(
            "[%s] 发送命令: %s", self.session_id, tcl_command[:200]
        )
        self._process.stdin.write(wrapped.encode("utf-8"))
        await self._process.stdin.drain()

        # 收集输出直到匹配 sentinel
        output_lines: list[str] = []
        return_code = -1

        while True:
            raw = await self._process.stdout.readline()
            if not raw:
                raise RuntimeError(
                    f"Vivado 进程意外终止（会话 '{self.session_id}'）。"
                )

            line = decode_vivado_output(raw).rstrip("\r\n")

            m = pattern.search(line)
            if m:
                return_code = int(m.group(1))
                break

            # 过滤掉 sentinel 相关的内部变量设置行
            if not line.startswith("VMCP_ERR:"):
                output_lines.append(line)
            else:
                # 错误信息行，去掉前缀后保留
                output_lines.append(line[len("VMCP_ERR: "):])

        output = clean_output("\n".join(output_lines))
        is_error = return_code != 0

        # B5 修复：出错时附加 stderr 最近几行，帮助 AI 看到完整错误原因
        if is_error:
            stderr_tail = self._recent_stderr(max_lines=30)
            if stderr_tail and stderr_tail not in output:
                output = (
                    f"{output}\n--- stderr (最近 30 行) ---\n{stderr_tail}"
                    if output
                    else f"--- stderr (最近 30 行) ---\n{stderr_tail}"
                )

        logger.debug(
            "[%s] 结果: rc=%d, output=%d chars",
            self.session_id, return_code, len(output),
        )

        return TclResult(
            output=output,
            return_code=return_code,
            is_error=is_error,
        )

    async def stop(self, timeout: float = 10.0) -> None:
        """优雅地关闭 Vivado 会话。

        先发送 exit 命令，超时后 kill。
        """
        if not self._process:
            return

        # 在第一次 await 前关闭命令入口。execute 在锁内再次检查该标志，
        # 因而即使它已通过外层存活检查，也不能在 stop 开始后发送命令。
        self._closing = True
        self._state = SessionState.STOPPING
        logger.info("正在关闭 Vivado 会话 '%s'...", self.session_id)

        inflight = self._inflight_task
        if inflight is None or inflight.done():
            # 无在途 reader 时与 execute 共用同一把锁，完整保护 exit 的发送。
            async with self._lock:
                if self.is_alive and self._process.stdin:
                    try:
                        self._process.stdin.write(b"exit\n")
                        await self._process.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass  # 进程可能已退出

        # 等待进程退出
        if self.is_alive:
            try:
                await asyncio.wait_for(
                    self._process.wait(), timeout=timeout
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Vivado 会话 '%s' 未在 %ss 内退出，强制终止。",
                    self.session_id, timeout,
                )
                if sys.platform == "win32":
                    kill_result = await asyncio.to_thread(
                        subprocess.run,
                        ["taskkill", "/F", "/T", "/PID", str(self._process.pid)],
                        capture_output=True,
                        timeout=10.0,
                    )
                    if kill_result.returncode != 0:
                        try:
                            await asyncio.wait_for(self._process.wait(), timeout=0.5)
                        except asyncio.TimeoutError as exc:
                            detail = decode_vivado_output(
                                kill_result.stderr or kill_result.stdout
                            )[-1000:].strip()
                            raise RuntimeError(
                                "taskkill 未能终止 Vivado 进程树: "
                                f"pid={self._process.pid}, "
                                f"returncode={kill_result.returncode}"
                                + (f", detail={detail}" if detail else "")
                            ) from exc
                else:
                    self._process.kill()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=10.0)
                except asyncio.TimeoutError as exc:
                    raise RuntimeError(
                        f"强制终止后进程仍未退出: pid={self._process.pid}"
                    ) from exc

        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):
                pass
        self._stderr_task = None

        self._state = SessionState.STOPPED
        self._process = None
        inflight = self._inflight_task
        if inflight is not None:
            if not inflight.done():
                inflight.cancel()
            try:
                await inflight
            except (asyncio.CancelledError, Exception):
                pass
            if self._inflight_task is inflight:
                self._inflight_task = None
        self._closing = False
        logger.info("Vivado 会话 '%s' 已关闭。", self.session_id)


# 向后兼容别名：0.1.x 代码可能还在引用 VivadoSession
VivadoSession = SubprocessSession
