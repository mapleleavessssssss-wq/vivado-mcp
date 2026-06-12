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

from vivado_mcp.tcl_scripts import QUERY_CURRENT_PROJECT
from vivado_mcp.vivado.base_session import BaseSession, SessionState
from vivado_mcp.vivado.tcl_utils import TclResult, clean_output

logger = logging.getLogger(__name__)

# 默认最大响应大小（10MB）
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024

# 握手响应合理上限(超过这个值 = 端口上是别的协议,把 ASCII 当 length 解释)
_HANDSHAKE_MAX_RESP = 8192

# 进程退出兜底:记录所有临时 tcl 脚本,强杀 MCP 时也会被 atexit 清掉
# 避免 /tmp/tmp*.tcl 堆积。正常路径 stop() 会主动 unlink 并从此集合移除。
_TMP_SCRIPTS: set[str] = set()

# 正在 spawn、尚未注册进 SessionManager._sessions 的目标端口集合。
# list_sessions 外部探测据此跳过,避免把自己正在启动的 GUI 误报为 external。
# start() 在 spawn 后登记,连接循环结束(成功/失败)即移除。
# 已知窗口(审计 P3,接受现状):普通 set 非引用计数 —— 同端口并发 spawn 时
# (AI 重试/双发),先结束的 start 在 finally discard 会摘掉后者的标记,后者
# 余下启动窗口内可能被外部探测误报为 external 一次。瞬时且无害:同端口的
# 第二个 spawn 本就注定 bind 失败,不值得为此换 Counter。
_PENDING_SPAWN_PORTS: set[int] = set()

# current_project 一次性查询(PRD A2)的独立短连接超时。模块级常量便于测试覆盖。
_CURPROJ_TIMEOUT = 5.0


def _make_probe_payload() -> tuple[str, bytes]:
    """生成一次性握手探测 payload:(magic token, 未加 length 头的 payload 字节)。

    vmcp 服务端会把 ``puts`` 的 token 反射进响应 output(captured_buf 路径),
    校验方用 :func:`_verify_probe_resp` 验反射。probe(同步)与 _handshake(异步)
    **必须共用**这一份逻辑 —— 0.3.21 token 校验只打在 probe 漏了 _handshake,
    正是 B16「逻辑 fork 改一处漏一处」的重演。
    """
    token = "VMCP_PROBE_" + uuid.uuid4().hex[:16]
    return token, f"puts {token}".encode("utf-8")


def _verify_probe_resp(obj: object, token: str) -> bool:
    """校验握手响应:必须是含 rc/output 的 dict,且 output 反射了本次 token。

    仅验"dict + rc/output 字段"挡不住 VMware vNIC 等假阳性 listener
    (0.3.21 真机实测),必须验响应**内容**包含本次探测的随机 token。
    """
    if not (isinstance(obj, dict) and "rc" in obj and "output" in obj):
        return False
    return token in str(obj.get("output", ""))


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
    token, payload = _make_probe_payload()
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
            # magic token 反射校验:挡掉 echo-type / 非 vmcp 服务的假阳性
            return _verify_probe_resp(obj, token)
    except (OSError, socket.timeout, json.JSONDecodeError, UnicodeDecodeError):
        return False


def _query_current_project(
    host: str, port: int, timeout: float = _CURPROJ_TIMEOUT
) -> str | None:
    """一次性独立短连接查询 current_project(PRD A2 横幅提示用)。

    **绝不走会话主连接**:主连接上的查询一旦超时,迟到的响应会残留在流上,
    让后续所有 execute 的请求/响应永久错位一格(0.3.22 审计 P1,fake server
    实测复现)。本函数与 :func:`probe_vmcp_server` 同款同步收发:
    连接 → 发一帧 QUERY_CURRENT_PROJECT → 读一帧 → 关闭,失败即丢弃连接。

    Returns:
        current_project 名称(无项目时为 ``""``);任何失败返回 None 并
        logger.warning 具体原因 —— 失败只意味着 banner 无提示。
    """
    payload = QUERY_CURRENT_PROJECT.encode("utf-8")
    header = len(payload).to_bytes(4, "big")
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(header + payload)
            hdr = _recv_exact(s, 4)
            if hdr is None:
                logger.warning(
                    "current_project 查询读响应头失败(对端关闭或 %ss 超时),"
                    "banner 省略项目提示", timeout,
                )
                return None
            resp_len = int.from_bytes(hdr, "big")
            if resp_len <= 0 or resp_len > _HANDSHAKE_MAX_RESP:
                logger.warning(
                    "current_project 查询响应长度非法: %d,banner 省略项目提示",
                    resp_len,
                )
                return None
            body = _recv_exact(s, resp_len)
            if body is None:
                logger.warning(
                    "current_project 查询读响应体失败(对端关闭或 %ss 超时),"
                    "banner 省略项目提示", timeout,
                )
                return None
            obj = json.loads(body.decode("utf-8"))
            output = str(obj.get("output", ""))
    except (OSError, socket.timeout) as e:
        logger.warning(
            "current_project 查询连接/收发失败(banner 省略项目提示): %s", e
        )
        return None
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning(
            "current_project 查询响应解析失败(banner 省略项目提示): %s", e
        )
        return None

    for line in output.splitlines():
        if line.startswith("VMCP_CURPROJ:"):
            return line[len("VMCP_CURPROJ:"):].strip()
    logger.warning(
        "current_project 查询输出缺 VMCP_CURPROJ 标记,前 200 字: %r",
        output[:200],
    )
    return None


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
        port: int = 0,
        attach_only: bool = False,
    ):
        super().__init__(vivado_path=vivado_path, session_id=session_id)
        # 端口意图哨兵(B 方案):
        #   port == 0 = 未指定 → auto-alloc 一个空闲端口 spawn 全新独立实例,**跳过 probe**
        #               (多开正解:连开两次 = 两个独立 GUI,不会被 probe 抢成 attach)
        #   port  > 0 = 显式目标端口 → 先 probe→命中则 attach,否则 spawn 并绑该确切端口
        # attach 模式下 port 始终是要连的显式端口(attach 本就需知道连哪)。
        self._port_preference = port
        self._attach_only = attach_only
        # probe-then-attach 命中外部 GUI(用户手动启动 + init.tcl 已注入)时为 True
        # 与 _attach_only 区别:_attach_only 是用户显式请求,_attached_external
        # 是 mode="gui" 时的隐式 attach。两者对 mode/stop 行为意义相同。
        self._attached_external: bool = False
        self._proc: asyncio.subprocess.Process | None = None
        # auto-alloc 出来的确切端口(port==0 路径);显式端口路径下保持 None。
        self._allocated_port: int | None = None
        # spawn 的 vivado pid,供 stop() 在 self._proc 引用丢失时按 pid 精杀。
        # attach / 命中外部 GUI 路径本就不 spawn,_pid 保持 None,被 stop 双守卫挡住。
        self._pid: int | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected_port: int | None = None
        self._lock = asyncio.Lock()
        self._tmp_script: str | None = None
        # 上一条 execute 超时且响应尚未收到时为 True(下次成功收到一帧即清除)。
        # list_sessions 探活守卫据此跳过 fresh probe:Vivado 单线程 event loop
        # 正在跑已超时的长命令时,新连接得不到服务,1s 探活必然落空 ≠ 挂死。
        self._pending_response: bool = False

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
    def pid(self) -> int | None:
        """本 session spawn 的 vivado 进程 pid(attach / 外部命中路径为 None)。"""
        return self._pid

    def status_dict(self) -> dict:
        """在基类字段上补 pid,便于 list_sessions / 诊断区分自己 spawn 的实例。"""
        d = super().status_dict()
        if self._pid is not None:
            d["pid"] = self._pid
        return d

    @staticmethod
    def _alloc_free_port() -> int:
        """bind(("127.0.0.1", 0)) 抢一个 OS 分配的空闲端口号,立即释放后返回。

        留一个极小的 TOCTOU 竞争窗口(取号→释放→tcl server 真正 bind 之间别的
        进程可能抢走),但这是**可观测失败**:tcl server 绑不上会退出 + Python 连
        该端口超时报错,不会像旧池逻辑那样静默串台连到别人的端口。
        """
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]
        finally:
            s.close()

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

        # ---- 0. 非 attach + 显式端口(port>0):先 probe,命中则直接 attach ----
        # 避免和用户手动启动的 GUI 抢端口最后 spawn 出孤儿(0.3.19 Bug,§6 第1条)。
        # port==0(未指定)= 明确要"独立新实例",**跳过 probe**,否则第2个实例会被
        # attach 到第1个。两种意图都成立靠这个哨兵区分。
        if not self._attach_only and self._port_preference > 0:
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
                return msg + await self._current_project_hint()
            logger.info(
                "端口 %d 无 vivado-mcp server,走 spawn 新 GUI 路径",
                self._port_preference,
            )

        # ---- 1. 如果非 attach 模式，spawn Vivado GUI(绑确切端口) ----
        if not self._attach_only:
            # 确定要注入给新 vivado 的**确切目标端口**:
            #   port>0 → 用这个显式端口;port==0 → auto-alloc 一个空闲端口(独立实例)
            if self._port_preference > 0:
                target_port = self._port_preference
            else:
                self._allocated_port = self._alloc_free_port()
                target_port = self._allocated_port
                logger.info(
                    "会话 '%s' 未指定端口,auto-alloc 空闲端口 %d 启动独立实例",
                    self.session_id, target_port,
                )

            try:
                script_path = _locate_server_script()
            except FileNotFoundError as e:
                self._state = SessionState.ERROR
                raise RuntimeError(str(e)) from e

            try:
                # 关键：-source 临时注入 tcl server（即使用户没跑 install 也能工作）
                # 注入 VMCP_PORT_PREF = 确切端口,tcl server 绑这个端口否则退出
                # (不再池滑动 → 杜绝新 vivado 监听在没人连的端口=孤儿)
                import tempfile
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".tcl", delete=False, encoding="utf-8"
                ) as tmp:
                    tmp.write(f"set ::VMCP_PORT_PREF {target_port}\n")
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
                # 记下 pid:stop() 在 self._proc 引用因故丢失时仍能按 pid 精杀
                self._pid = self._proc.pid
                logger.info(
                    "已 spawn Vivado GUI (pid=%s, 目标端口=%d), 等待 TCP server 就绪...",
                    self._proc.pid, target_port,
                )
            except (OSError, FileNotFoundError) as e:
                self._state = SessionState.ERROR
                raise RuntimeError(f"启动 Vivado GUI 失败: {e}") from e

        # ---- 2. 只连那一个确切端口,轮询直到新 vivado 起完 ----
        # 不再扫端口池:连别人的端口正是 0.3.19 串台的根因。
        #   spawn 路径 → 连 auto-alloc / 显式注入的确切端口
        #   attach 路径 → 连用户给的显式端口
        target_port = (
            self._allocated_port
            if self._allocated_port is not None
            else self._port_preference
        )

        # spawn 中的目标端口登记进 pending 集合:此时 session 尚未进
        # SessionManager._sessions,list_sessions 外部探测会把自己正在启动的
        # GUI 误报为 external,靠这个集合跳过。try/finally 保证异常路径也移除。
        spawned = self._proc is not None
        if spawned:
            _PENDING_SPAWN_PORTS.add(target_port)
        try:
            deadline = time.time() + timeout
            connect_err: Exception | None = None
            while time.time() < deadline:
                try:
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection("127.0.0.1", target_port),
                        timeout=2.0,
                    )
                except (ConnectionRefusedError, asyncio.TimeoutError, OSError) as e:
                    # 新 vivado 还没起完 → 等下一轮重试同一个确切端口
                    connect_err = e
                else:
                    # 连上后必须握手验证：确认对面说的是我们的 length-prefix 协议
                    # （避免连到 SynthPilot 等其他产品的 server 上）
                    handshake_ok = await self._handshake(reader, writer)
                    if handshake_ok:
                        self._reader = reader
                        self._writer = writer
                        self._connected_port = target_port
                        self._state = SessionState.READY
                        self._start_time = time.time()
                        msg = (
                            f"GUI 会话就绪：attach={self._attach_only}，"
                            f"端口 {target_port}"
                        )
                        logger.info(msg)
                        return msg + await self._current_project_hint()
                    logger.debug(
                        "端口 %d 握手失败（可能是其他产品的 server），重试",
                        target_port,
                    )
                    writer.close()
                    try:
                        await writer.wait_closed()
                    except Exception:
                        pass

                # 进程还活吗
                if self._proc is not None and self._proc.returncode is not None:
                    self._state = SessionState.ERROR
                    raise RuntimeError(
                        f"Vivado GUI 进程提前退出 "
                        f"(returncode={self._proc.returncode})"
                    )
                await asyncio.sleep(2.0)

            # 超时:先杀掉自己 spawn 的 Vivado 再抛 —— 异常上传后 session 不会
            # 进 SessionManager._sessions,stop_session 无从清理,不杀就是真孤儿
            await self._cleanup_failed_spawn()
            self._state = SessionState.ERROR
            raise RuntimeError(
                f"连接 Vivado GUI 超时（{timeout}s，确切端口 {target_port}）。"
                f"该端口可能被其他进程抢占,请重试。最后一次错误: {connect_err}"
            )
        finally:
            if spawned:
                _PENDING_SPAWN_PORTS.discard(target_port)

    async def _cleanup_failed_spawn(self) -> None:
        """start() 超时/失败时杀掉自己 spawn 的 Vivado,防止产生孤儿进程。

        start 失败异常上传后 session 不会进 SessionManager._sessions,
        stop_session 此后无从清理 —— 必须在抛错前自己收尾。
        attach / 外部命中路径不 spawn(_proc is None),天然跳过。
        """
        import subprocess
        import sys

        if self._proc is None or self._proc.returncode is not None:
            return
        kill_pid = self._pid if self._pid is not None else self._proc.pid
        logger.warning(
            "会话 '%s' 启动失败,清理已 spawn 的 Vivado 进程 (pid=%s)",
            self.session_id, kill_pid,
        )
        try:
            if sys.platform == "win32":
                # /T 递归杀进程树:vivado.bat 的 cmd.exe 外壳下还有 vivado.exe
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(kill_pid)],
                    capture_output=True,
                    timeout=10.0,
                )
            else:
                # Unix: 单进程 kill(非 killpg)。vivado 是 shell 包装脚本且
                # 子进程未被 exec 接管时可能留孤儿 —— 项目主战场是 Windows
                # 真机,暂不做 start_new_session+killpg 改造(与 stop() 步骤 3 同)
                self._proc.kill()
            await asyncio.wait_for(self._proc.wait(), timeout=10.0)
        except Exception as e:
            logger.warning(
                "清理 spawn 失败的 Vivado (pid=%s) 异常: %s", kill_pid, e
            )

    async def _current_project_hint(self) -> str:
        """启动横幅的项目状态提示(PRD A2)。

        spawn 出来的全新 GUI / 用户停在 Start Page 的 GUI 没打开任何项目
        (current_project 为空或 "New Project"),AI 直接跑 report_* 只会
        拿到一串错。此时提示先 open_project。

        查询走 :func:`_query_current_project` 的**一次性独立短连接**,主连接
        零接触 —— 在主连接上 execute 查询一旦超时,残留响应会让后续所有命令
        的结果永久错位(0.3.22 审计 P1)。查询失败不阻塞启动,降级为无提示
        (helper 内已 log 具体原因)。同步收发包进 to_thread,不阻塞 event loop。
        """
        if self._connected_port is None:
            return ""
        proj = await asyncio.to_thread(
            _query_current_project,
            "127.0.0.1",
            self._connected_port,
            _CURPROJ_TIMEOUT,
        )
        if proj is None:
            return ""
        if proj in ("", "New Project"):
            shown = proj if proj else "<无>"
            return (
                f"\n提示: 当前 project={shown},如非预期请先 "
                "close_project -quiet 再 open_project <绝对路径>"
            )
        return ""

    async def _handshake(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        timeout: float = 3.0,
    ) -> bool:
        """发送探测命令验证对端说的是我们的 length-prefix 协议。

        与同步版 :func:`probe_vmcp_server` 共用同一份 token 生成 + 反射校验
        (_make_probe_payload / _verify_probe_resp)。0.3.21 的 magic token
        修复曾只打在 probe 这半边,_handshake 仍是弱校验(只验 rc/output
        字段存在),VMware vNIC 类假阳性 listener 在 attach 路径上仍能骗过
        —— 本次归一,杜绝逻辑 fork。

        成功:JSON 响应是 dict、含 rc/output 字段,且 output 反射了本次 token
        失败:超时 / 长度头异常大 / JSON 解析失败 / token 未反射
        → 说明对面可能是 SynthPilot / VMware vNIC 等其他服务
        """
        token, payload = _make_probe_payload()
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
            if resp_len <= 0 or resp_len > _HANDSHAKE_MAX_RESP:
                return False

            body = await asyncio.wait_for(
                reader.readexactly(resp_len), timeout=timeout
            )
            obj = json.loads(body.decode("utf-8"))
            return _verify_probe_resp(obj, token)
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
            # 超时 ≠ 命令失败:Vivado 多半仍在跑,响应稍后才到并残留在流上。
            # 标记 pending,list_sessions 探活据此跳过(避免"忙=死"误判)
            self._pending_response = True
            raise asyncio.TimeoutError(
                f"读取响应长度头超时（{timeout}s）。命令: {tcl_command[:200]}"
            )

        resp_len = int.from_bytes(resp_hdr, "big")
        if resp_len < 0 or resp_len > _MAX_RESPONSE_BYTES:
            raise RuntimeError(
                f"非法响应长度 {resp_len}（限 {_MAX_RESPONSE_BYTES} 字节以内）。"
            )

        try:
            resp_body = await asyncio.wait_for(
                self._reader.readexactly(resp_len),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            # 比照头部读包一层带 message 的超时:裸 TimeoutError str() 为空,
            # 工具层只会显示 "[ERROR] 命令执行失败: "(审计 P3,缺具体原因)
            self._pending_response = True
            raise asyncio.TimeoutError(
                f"读取响应体超时（{timeout}s）。命令: {tcl_command[:200]}"
            )
        # 成功收满一帧响应 → 清除 pending 标记(流上不再有在途残留)
        self._pending_response = False
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
                # taskkill 用记录的 self._pid(spawn 时存),即使 self._proc 引用因故
                # 丢失也能按确切 pid 精杀;正常情况 self._pid == self._proc.pid。
                kill_pid = self._pid if self._pid is not None else self._proc.pid
                # 先给 Vivado 一点时间自己退(响应 Tcl exit)
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    # 没退,强杀
                    try:
                        if sys.platform == "win32":
                            # 关键:/T 递归杀进程树,捕获 cmd.exe(vivado.bat)下的 vivado.exe
                            subprocess.run(
                                ["taskkill", "/F", "/T", "/PID", str(kill_pid)],
                                capture_output=True,
                                timeout=timeout,
                            )
                        else:
                            # Unix: 单进程 kill(非 killpg,旧注释"kill 进程组"
                            # 与实现不符已订正)。vivado 包装脚本场景可能留孤儿,
                            # 主战场 Windows,暂不做 killpg 改造
                            self._proc.kill()
                        await asyncio.wait_for(self._proc.wait(), timeout=timeout)
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Vivado 进程 PID=%s 未在 %ss 内退出,可能成为孤儿进程",
                            kill_pid, timeout,
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
