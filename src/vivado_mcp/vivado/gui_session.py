"""GuiSession：连接到 Vivado GUI 的 TCP 会话。

两种启动方式：
1. ``attach_only=False`` （默认）—— MCP 自己 spawn ``vivado -mode gui``，
   GUI 启动时 source 注入脚本开启 TCP server，然后 MCP 连上。用户**会看到 Vivado 图标**。
2. ``attach_only=True`` —— 假设用户已手动打开 Vivado（需先 ``vivado-mcp install``
   让 init.tcl 自动开 server），MCP 直接 TCP 连。

协议：length-prefix framing（4 字节 big-endian + UTF-8 payload）
- 请求 payload = Tcl 命令文本
- 响应 payload = JSON: ``{"rc": int, "output": string}``
"""

from __future__ import annotations

import asyncio
import importlib.resources
import json
import logging
import time
from pathlib import Path

from vivado_mcp.vivado.base_session import BaseSession, SessionState
from vivado_mcp.vivado.tcl_utils import TclResult, clean_output

logger = logging.getLogger(__name__)

# 端口池大小（从 port_preference 起连续 N 个）
_PORT_POOL_SIZE = 5

# 默认最大响应大小（10MB）
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024


def _locate_server_script() -> Path:
    """定位打包进 wheel 的 vivado_mcp_server.tcl 文件。

    优先级：
    1. 与源码同级的 scripts/ 目录（editable 安装 / 源码运行）
    2. 已安装的 wheel 内的 ``vivado_mcp/scripts/``（importlib.resources）
    """
    # 路径 1：仓库根的 scripts/（editable 模式）
    here = Path(__file__).resolve().parent
    # here = .../vivado_mcp/vivado/，上上级是仓库根
    candidate = here.parent.parent.parent / "scripts" / "vivado_mcp_server.tcl"
    if candidate.is_file():
        return candidate

    # 路径 2：package data（wheel 安装模式）
    try:
        with importlib.resources.as_file(
            importlib.resources.files("vivado_mcp").joinpath(
                "scripts/vivado_mcp_server.tcl"
            )
        ) as p:
            if p.is_file():
                return p
    except (ModuleNotFoundError, FileNotFoundError, AttributeError):
        pass

    raise FileNotFoundError(
        "找不到 vivado_mcp_server.tcl。请重新安装 vivado-mcp 或检查包完整性。"
    )


class GuiSession(BaseSession):
    """连接到 Vivado GUI 的 TCP 会话。"""

    def __init__(
        self,
        vivado_path: str,
        session_id: str = "default",
        port: int = 9999,
        attach_only: bool = False,
    ):
        super().__init__(vivado_path=vivado_path, session_id=session_id)
        self._port_preference = port
        self._attach_only = attach_only
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected_port: int | None = None
        self._lock = asyncio.Lock()
        self._tmp_script: str | None = None

    @property
    def mode(self) -> str:
        return "attach" if self._attach_only else "gui"

    @property
    def is_alive(self) -> bool:
        if self._state in (SessionState.DEAD, SessionState.STOPPED):
            return False
        if self._writer is None:
            return False
        # StreamWriter.is_closing() 反映 socket 状态
        return not self._writer.is_closing()

    async def start(self, timeout: float = 120.0) -> str:
        """启动 Vivado GUI（或 attach 已有实例），建立 TCP 连接。"""
        if self.is_alive:
            return f"会话 '{self.session_id}' 已在运行中。"

        self._state = SessionState.STARTING
        logger.info(
            "启动 GUI 会话 '%s' (attach=%s, port_pref=%d)",
            self.session_id, self._attach_only, self._port_preference,
        )

        # ---- 1. 如果非 attach 模式，spawn Vivado GUI ----
        if not self._attach_only:
            try:
                script_path = _locate_server_script()
            except FileNotFoundError as e:
                self._state = SessionState.ERROR
                raise RuntimeError(str(e)) from e

            try:
                # 关键：-source 临时注入 tcl server（即使用户没跑 install 也能工作）
                # 通过 -source 传入 tcl 脚本，并在之前 `-tclargs` 或 env 传端口偏好
                # 但 -source 本身不支持参数，我们直接写一个临时脚本设置 PORT_PREF
                import tempfile
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".tcl", delete=False, encoding="utf-8"
                ) as tmp:
                    tmp.write(f"set ::VMCP_PORT_PREF {self._port_preference}\n")
                    tmp.write(f'source "{script_path.as_posix()}"\n')
                    tmp_script = tmp.name
                self._tmp_script = tmp_script

                self._proc = await asyncio.create_subprocess_exec(
                    self.vivado_path,
                    "-mode", "gui",
                    "-source", tmp_script,
                    "-nojournal", "-nolog",
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                logger.info(
                    "已 spawn Vivado GUI (pid=%s), 等待 TCP server 就绪...",
                    self._proc.pid,
                )
            except (OSError, FileNotFoundError) as e:
                self._state = SessionState.ERROR
                raise RuntimeError(f"启动 Vivado GUI 失败: {e}") from e

        # ---- 2. 轮询端口池直到连上 ----
        # 严格从 preference 开始连续 N 个，避免连上其他产品的 server（如 SynthPilot）
        ports_to_try = [
            self._port_preference + i for i in range(_PORT_POOL_SIZE)
        ]

        deadline = time.time() + timeout
        connect_err: Exception | None = None
        while time.time() < deadline:
            for port in ports_to_try:
                try:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection("127.0.0.1", port),
                        timeout=2.0,
                    )
                except (ConnectionRefusedError, asyncio.TimeoutError, OSError) as e:
                    connect_err = e
                    continue

                # 连上后必须握手验证：确认对面说的是我们的 length-prefix 协议
                # （避免连到 SynthPilot 等其他产品的 server 上）
                handshake_ok = await self._handshake(reader, writer)
                if not handshake_ok:
                    logger.debug(
                        "端口 %d 握手失败（可能是其他产品的 server），跳过",
                        port,
                    )
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except Exception:
                        pass
                    continue

                self._reader = reader
                self._writer = writer
                self._connected_port = port
                self._state = SessionState.READY
                self._start_time = time.time()
                msg = (
                    f"GUI 会话就绪：attach={self._attach_only}，"
                    f"端口 {port}"
                )
                logger.info(msg)
                return msg

            # 本轮端口池全部失败，进程还活吗
            if self._proc is not None and self._proc.returncode is not None:
                self._state = SessionState.ERROR
                raise RuntimeError(
                    f"Vivado GUI 进程提前退出 (returncode={self._proc.returncode})"
                )
            await asyncio.sleep(2.0)

        # 超时
        self._state = SessionState.ERROR
        raise RuntimeError(
            f"连接 Vivado GUI 超时（{timeout}s，端口池 {ports_to_try}）。"
            f"最后一次错误: {connect_err}"
        )

    async def _handshake(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        timeout: float = 3.0,
    ) -> bool:
        """发送探测命令验证对端说的是我们的 length-prefix 协议。

        成功：收到格式正确的 JSON 响应（含 rc 和 output 字段）
        失败：超时 / 长度头异常大 / JSON 解析失败 / 字段缺失
        → 说明对面可能是 SynthPilot 或其他产品的 server
        """
        payload = b"puts VMCP_HANDSHAKE_ACK"
        header = len(payload).to_bytes(4, "big")
        try:
            writer.write(header + payload)
            await writer.drain()

            # 读 4 字节响应头
            resp_hdr = await asyncio.wait_for(
                reader.readexactly(4), timeout=timeout
            )
            resp_len = int.from_bytes(resp_hdr, "big")
            # 合理响应通常 <1KB；超过这值大概率是把 ASCII 当长度解释的
            if resp_len < 0 or resp_len > 8192:
                return False

            body = await asyncio.wait_for(
                reader.readexactly(resp_len), timeout=timeout
            )
            obj = json.loads(body.decode("utf-8"))
            return "output" in obj and "rc" in obj
        except Exception:
            return False

    async def execute(
        self,
        tcl_command: str,
        timeout: float = 120.0,
    ) -> TclResult:
        """发送 Tcl 命令并等待响应。"""
        if not self.is_alive:
            raise RuntimeError(
                f"会话 '{self.session_id}' 未连接。请先调用 start_session。"
            )

        assert self._reader and self._writer

        async with self._lock:
            self._state = SessionState.BUSY
            try:
                result = await self._execute_impl(tcl_command, timeout)
                self._state = SessionState.READY
                return result
            except (ConnectionError, asyncio.IncompleteReadError) as e:
                # D4: 连接断开，标记为 DEAD，不自动重连
                self._state = SessionState.DEAD
                raise RuntimeError(
                    f"GUI 会话连接断开（Vivado 可能被关闭或崩溃）: {e}。"
                    "请重新调用 start_session。"
                ) from e
            except Exception:
                if self.is_alive:
                    self._state = SessionState.READY
                else:
                    self._state = SessionState.DEAD
                raise

    async def _execute_impl(
        self,
        tcl_command: str,
        timeout: float,
    ) -> TclResult:
        assert self._reader and self._writer

        # 发送：[4 字节长度][UTF-8 payload]
        payload = tcl_command.encode("utf-8")
        header = len(payload).to_bytes(4, "big")
        self._writer.write(header + payload)
        await self._writer.drain()

        # 接收：[4 字节长度][UTF-8 JSON payload]
        try:
            resp_hdr = await asyncio.wait_for(
                self._reader.readexactly(4),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            raise asyncio.TimeoutError(
                f"读取响应长度头超时（{timeout}s）。命令: {tcl_command[:200]}"
            )

        resp_len = int.from_bytes(resp_hdr, "big")
        if resp_len < 0 or resp_len > _MAX_RESPONSE_BYTES:
            raise RuntimeError(
                f"非法响应长度 {resp_len}（限 {_MAX_RESPONSE_BYTES} 字节以内）。"
            )

        resp_body = await asyncio.wait_for(
            self._reader.readexactly(resp_len),
            timeout=timeout,
        )
        try:
            obj = json.loads(resp_body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise RuntimeError(
                f"响应 JSON 解析失败: {e}。原始响应前 200 字节: "
                f"{resp_body[:200]!r}"
            ) from e

        rc = int(obj.get("rc", -1))
        output = clean_output(str(obj.get("output", "")))
        return TclResult(
            output=output,
            return_code=rc,
            is_error=(rc != 0),
        )

    async def stop(self, timeout: float = 10.0) -> None:
        """关闭 TCP 连接 + 终止 spawn 的 GUI 进程（attach 模式不终止外部进程）。"""
        logger.info("正在关闭 GUI 会话 '%s'...", self.session_id)

        # 关 socket
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception as e:
                logger.debug("关闭 writer 异常: %s", e)
            self._writer = None
            self._reader = None

        # 如果是我们自己 spawn 的 GUI，优雅关闭
        # attach 模式下进程是用户管理的，不动它
        if self._proc is not None and not self._attach_only:
            if self._proc.returncode is None:
                try:
                    self._proc.terminate()
                    await asyncio.wait_for(self._proc.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    logger.warning(
                        "Vivado GUI 进程未在 %ss 内退出，强制 kill。", timeout
                    )
                    self._proc.kill()
                    await self._proc.wait()
                except Exception as e:
                    logger.debug("停止 GUI 进程异常: %s", e)
            self._proc = None

        # 清理临时脚本
        if self._tmp_script:
            try:
                import os
                os.unlink(self._tmp_script)
            except OSError:
                pass
            self._tmp_script = None

        self._state = SessionState.STOPPED
        logger.info("GUI 会话 '%s' 已关闭。", self.session_id)
