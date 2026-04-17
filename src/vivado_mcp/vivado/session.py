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
import time

from vivado_mcp.vivado.base_session import BaseSession, SessionState
from vivado_mcp.vivado.tcl_utils import (
    TclResult,
    clean_output,
    generate_sentinel,
    make_sentinel_pattern,
    wrap_command,
)

logger = logging.getLogger(__name__)

# stderr 缓冲区保留的最近行数（避免内存无限增长）
_STDERR_RING_SIZE = 200


class SubprocessSession(BaseSession):
    """Vivado TCL 交互式子进程会话（`-mode tcl` 无头批处理）。

    通过 asyncio subprocess 管理一个 `vivado -mode tcl` 进程，
    使用 catch + UUID sentinel 协议实现可靠的命令执行与输出采集。
    """

    def __init__(self, vivado_path: str, session_id: str = "default"):
        super().__init__(vivado_path=vivado_path, session_id=session_id)
        self._process: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
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

        self._state = SessionState.STARTING
        logger.info("启动 Vivado 会话 '%s': %s", self.session_id, self.vivado_path)

        try:
            self._process = await asyncio.create_subprocess_exec(
                self.vivado_path,
                "-mode", "tcl",
                "-nojournal", "-nolog",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, FileNotFoundError) as e:
            self._state = SessionState.ERROR
            raise RuntimeError(f"无法启动 Vivado: {e}") from e

        # 等待 Vivado 启动完成（读取初始横幅）
        banner = await self._read_startup_banner(timeout)
        self._state = SessionState.READY
        self._start_time = time.time()

        # B5 修复：启动 stderr 持续读取任务
        self._stderr_task = asyncio.create_task(self._drain_stderr())

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
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
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
        probe = wrap_command('puts "VMCP_READY"', sentinel)
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
                    stderr_out = ""
                    if self._process.stderr:
                        try:
                            stderr_out = (await asyncio.wait_for(
                                self._process.stderr.read(), timeout=2.0
                            )).decode("utf-8", errors="replace")
                        except asyncio.TimeoutError:
                            pass
                    raise RuntimeError(
                        f"Vivado 进程意外退出。stderr: {stderr_out}"
                    )

                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                m = pattern.search(line)
                if m:
                    # 找到 sentinel，启动完成
                    break
                lines.append(line)

        except asyncio.TimeoutError:
            self._state = SessionState.ERROR
            raise RuntimeError(
                f"Vivado 启动超时（{timeout}s）。"
                "请检查 Vivado 路径是否正确，或尝试增大超时值。"
            )

        return clean_output("\n".join(lines))

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
        if not self.is_alive:
            raise RuntimeError(
                f"会话 '{self.session_id}' 未运行。请先调用 start_session。"
            )

        async with self._lock:
            self._state = SessionState.BUSY
            try:
                result = await self._execute_impl(tcl_command, timeout)
                self._state = SessionState.READY
                return result
            except Exception:
                # 检查进程是否还活着
                if self.is_alive:
                    self._state = SessionState.READY
                else:
                    self._state = SessionState.ERROR
                raise

    async def _execute_impl(
        self,
        tcl_command: str,
        timeout: float,
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
                    raise RuntimeError(
                        f"Vivado 进程意外终止（会话 '{self.session_id}'）。"
                    )

                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")

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

        except asyncio.TimeoutError:
            raise asyncio.TimeoutError(
                f"命令执行超时（{timeout}s）。\n"
                f"会话: {self.session_id}\n"
                f"命令: {tcl_command[:200]}"
            )

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

        logger.info("正在关闭 Vivado 会话 '%s'...", self.session_id)

        # 取消 stderr drain 任务
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):
                pass
            self._stderr_task = None

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
                self._process.kill()
                await self._process.wait()

        self._state = SessionState.STOPPED
        self._process = None
        logger.info("Vivado 会话 '%s' 已关闭。", self.session_id)


# 向后兼容别名：0.1.x 代码可能还在引用 VivadoSession
VivadoSession = SubprocessSession
