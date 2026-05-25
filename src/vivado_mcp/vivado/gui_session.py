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
import atexit
import importlib.resources
import json
import logging
import os
import socket
import time
import uuid
from pathlib import Path

from vivado_mcp.vivado.base_session import BaseSession, SessionState
from vivado_mcp.vivado.tcl_utils import TclResult, clean_output

logger = logging.getLogger(__name__)

# 端口池大小（从 port_preference 起连续 N 个）
_PORT_POOL_SIZE = 5

# 默认最大响应大小（10MB）
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024

# 握手响应合理上限(超过这个值 = 端口上是别的协议,把 ASCII 当 length 解释)
_HANDSHAKE_MAX_RESP = 8192

# 进程退出兜底:记录所有临时 tcl 脚本,强杀 MCP 时也会被 atexit 清掉
# 避免 /tmp/tmp*.tcl 堆积。正常路径 stop() 会主动 unlink 并从此集合移除。
_TMP_SCRIPTS: set[str] = set()


def _recv_exact(sock: socket.socket, n: int) -> bytes | None:
    """同步阻塞收满 n 字节,失败返回 None。"""
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except (OSError, socket.timeout):
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def probe_vmcp_server(host: str, port: int, timeout: float = 0.5) -> bool:
    """同步探测 host:port 是否在跑 vivado-mcp 的 length-prefix TCP server。

    用 ``puts VMCP_PROBE_<uuid>`` 发起握手 —— vmcp 服务端会把 token 反射到
    响应 output(走 captured_buf 路径),probe 验响应 output 含该 uuid 才算
    vmcp 兼容。无副作用、只读探测。

    0.3.21 修:加 magic token 验证。0.3.19 实测中曾观察到 VMware vNIC 虚拟
    接口 listener 在某种 Windows 多接口/firewall race 下被错判为 vmcp server
    (PID 6408 绑 192.168.159.1:10000)。仅验"响应是 dict + 有 rc/output 字段"
    挡不住此类假阳性,必须验响应**内容**包含本次探测的随机 token。

    Returns:
        True 表示成功握手且响应 output 含 magic token(对面是同协议 server);
        False 表示连不上 / 协议不匹配 / 响应 output 缺 magic。
    """
    token = "VMCP_PROBE_" + uuid.uuid4().hex[:16]
    payload = f"puts {token}".encode("utf-8")
    header = len(payload).to_bytes(4, "big")
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(header + payload)
            hdr = _recv_exact(s, 4)
            if hdr is None:
                return False
            resp_len = int.from_bytes(hdr, "big")
            if resp_len <= 0 or resp_len > _HANDSHAKE_MAX_RESP:
                return False
            body = _recv_exact(s, resp_len)
            if body is None:
                return False
            obj = json.loads(body.decode("utf-8"))
            if not (isinstance(obj, dict) and "rc" in obj and "output" in obj):
                return False
            # magic token 反射校验:挡掉 echo-type / 非 vmcp 服务的假阳性
            return token in str(obj.get("output", ""))
    except (OSError, socket.timeout, json.JSONDecodeError, UnicodeDecodeError):
        return False


def _cleanup_tmp_scripts_atexit() -> None:
    """atexit 钩子:清理遗留的临时 Tcl 脚本。"""
    for path in list(_TMP_SCRIPTS):
        try:
            os.unlink(path)
        except OSError:
            pass
    _TMP_SCRIPTS.clear()


atexit.register(_cleanup_tmp_scripts_atexit)


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
        # probe-then-attach 命中外部 GUI(用户手动启动 + init.tcl 已注入)时为 True
        # 与 _attach_only 区别:_attach_only 是用户显式请求,_attached_external
        # 是 mode="gui" 时的隐式 attach。两者对 mode/stop 行为意义相同。
        self._attached_external: bool = False
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected_port: int | None = None
        self._lock = asyncio.Lock()
        self._tmp_script: str | None = None

    @property
    def mode(self) -> str:
        # 实际行为而非用户请求:外部 attach(显式 attach_only 或 probe 命中)
        # 都报 "attach",让上层(AI / list_sessions)能直接看出命令落到哪
        if self._attach_only or self._attached_external:
            return "attach"
        return "gui"

    @property
    def connected_port(self) -> int | None:
        """已连接的 TCP 端口(尚未连接为 None)。"""
        return self._connected_port

    @property
    def attached_external(self) -> bool:
        """是否 attach 到了非 MCP spawn 的 Vivado(用户手动启动 + init.tcl)。"""
        return self._attached_external

    @property
    def is_alive(self) -> bool:
        if self._state in (SessionState.DEAD, SessionState.STOPPED):
            return False
        if self._writer is None:
            return False
        # StreamWriter.is_closing() 反映 socket 状态
        return not self._writer.is_closing()

    async def _try_attach_existing(
        self,
        port: int,
        timeout: float = 3.0,
    ) -> bool:
        """非 attach 模式下,试探 port 上是否已有 vivado-mcp server。

        命中(连得上 + 握手通过)→ reader/writer/_connected_port 就绪,设
        ``_attached_external=True``,返回 True。
        失败 → 释放半开连接,返回 False。

        修复 0.3.19 Bug:防止用户已装 init.tcl 且手动开了 GUI 时,MCP
        spawn 一份新 Vivado,但 Python 端"先连上 9999 谁就赢"的逻辑
        把客户端连到原 GUI,新 spawn 出来的进程变成孤儿。
        """
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port),
                timeout=timeout,
            )
        except (ConnectionRefusedError, asyncio.TimeoutError, OSError):
            return False

        ok = await self._handshake(reader, writer)
        if not ok:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return False

        self._reader = reader
        self._writer = writer
        self._connected_port = port
        self._attached_external = True
        return True

    async def start(self, timeout: float = 120.0) -> str:
        """启动 Vivado GUI（或 attach 已有实例），建立 TCP 连接。"""
        if self.is_alive:
            return f"会话 '{self.session_id}' 已在运行中。"

        self._state = SessionState.STARTING
        logger.info(
            "启动 GUI 会话 '%s' (attach=%s, port_pref=%d)",
            self.session_id, self._attach_only, self._port_preference,
        )

        # ---- 0. 非 attach 模式:先 probe 首选端口,若已有 server 则直接 attach ----
        # 避免和用户手动启动的 GUI 抢端口最后 spawn 出孤儿(0.3.19 Bug)
        if not self._attach_only:
            if await self._try_attach_existing(self._port_preference, timeout=3.0):
                self._state = SessionState.READY
                self._start_time = time.time()
                msg = (
                    f"GUI 会话就绪(attach 到现有 GUI):端口 "
                    f"{self._connected_port}。"
                    "检测到该端口已有 vivado-mcp server,跳过 spawn 直接接管。"
                    "可能是你手动启动并装过 init.tcl 的 Vivado。"
                    "stop_session 不会关闭这个 GUI。"
                )
                logger.info(
                    "会话 '%s' attach 到外部 GUI(端口 %d),跳过 spawn",
                    self.session_id, self._connected_port,
                )
                return msg
            logger.info(
                "端口 %d 无 vivado-mcp server,走 spawn 新 GUI 路径",
                self._port_preference,
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
                # atexit 兜底:MCP 进程被强杀时仍会清理
                _TMP_SCRIPTS.add(tmp_script)

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
        """关闭 TCP 连接 + 终止 spawn 的 GUI 进程（attach 模式不终止外部进程）。

        B13 修复:原 ``_proc.terminate()`` 只杀 ``vivado.bat`` 的 cmd.exe 外壳,
        Windows 没有进程组概念,子进程 vivado.exe 会变成孤儿继续占 800MB+ 内存,
        且 Vivado 自己写的 ``vivado_pid<PID>.str`` 文件不被清理。

        新策略:
        1. 先通过 TCP 发 Tcl ``exit`` 让 Vivado 优雅退出(会自动清 pid 文件)
        2. 若超时,Windows 用 ``taskkill /F /T`` 递归杀进程树,Unix 用 SIGKILL
        3. 兜底扫工作目录 ``vivado_pid*.str`` 强删
        """
        import glob as glob_mod
        import os
        import subprocess
        import sys

        logger.info("正在关闭 GUI 会话 '%s'...", self.session_id)

        # 步骤 1:尝试优雅退出 —— 发 Tcl `exit`,Vivado 自己清 pid/journal
        # attach 模式 OR probe-then-attach 命中外部 GUI 时,都是用户的 Vivado,不主动 exit
        if (
            not self._attach_only
            and not self._attached_external
            and self._writer is not None
            and self._state in (SessionState.READY, SessionState.BUSY)
        ):
            try:
                # 不走 execute()(它对 SessionState 有校验),直接裸发
                payload = b"exit"
                header = len(payload).to_bytes(4, "big")
                self._writer.write(header + payload)
                await self._writer.drain()
                # 等 socket 被对端关闭(Vivado 退出时自动关连接)
                await asyncio.wait_for(
                    self._reader.read(4) if self._reader else asyncio.sleep(0),
                    timeout=5.0,
                )
            except Exception as e:
                logger.debug("优雅 exit 失败(将走强杀): %s", e)

        # 步骤 2:关 socket
        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception as e:
                logger.debug("关闭 writer 异常: %s", e)
            self._writer = None
            self._reader = None

        # 步骤 3:确保进程真退出。Windows 用 taskkill /T 递归杀树
        # 外部 attach(显式 attach_only 或 probe 命中)不杀进程
        if (
            self._proc is not None
            and not self._attach_only
            and not self._attached_external
        ):
            if self._proc.returncode is None:
                # 先给 Vivado 一点时间自己退(响应 Tcl exit)
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    # 没退,强杀
                    try:
                        if sys.platform == "win32":
                            # 关键:/T 递归杀进程树,捕获 cmd.exe 下的 vivado.exe
                            subprocess.run(
                                ["taskkill", "/F", "/T", "/PID", str(self._proc.pid)],
                                capture_output=True,
                                timeout=timeout,
                            )
                        else:
                            # Unix: kill 进程组
                            self._proc.kill()
                        await asyncio.wait_for(self._proc.wait(), timeout=timeout)
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Vivado 进程 PID=%s 未在 %ss 内退出,可能成为孤儿进程",
                            self._proc.pid, timeout,
                        )
                    except Exception as e:
                        logger.warning("强杀 Vivado 进程异常: %s", e)
            self._proc = None

        # 步骤 4:兜底清理 vivado_pid*.str(Vivado 强杀时不会自己删)
        for pid_file in glob_mod.glob("vivado_pid*.str"):
            try:
                os.remove(pid_file)
                logger.debug("已清理 %s", pid_file)
            except OSError as e:
                logger.debug("清理 %s 失败: %s", pid_file, e)

        # 步骤 5:清理临时脚本(正常路径,同时从 atexit 集合移除)
        if self._tmp_script:
            try:
                os.unlink(self._tmp_script)
            except OSError:
                pass
            _TMP_SCRIPTS.discard(self._tmp_script)
            self._tmp_script = None

        self._state = SessionState.STOPPED
        logger.info("GUI 会话 '%s' 已关闭。", self.session_id)
