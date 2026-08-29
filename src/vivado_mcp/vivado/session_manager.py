"""SessionManager：多 Vivado 实例管理。

每个 session_id 对应一个独立的会话实例。支持三种模式：
- ``mode="gui"`` (默认)—— MCP spawn ``vivado -mode gui``，你能看到 Vivado 图标
- ``mode="tcl"`` —— ``vivado -mode tcl`` 无头子进程（CI / 批处理友好）
- ``mode="attach"`` —— 连接到用户已手动打开的 Vivado GUI（需先 ``vivado-mcp install``）
"""

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Literal

from vivado_mcp.config import (
    get_vivado_version,
    normalize_path,
    resolve_vivado,
    vivado_versions_match,
)
from vivado_mcp.vivado.base_session import BaseSession
from vivado_mcp.vivado.gui_session import (
    _PENDING_SPAWN_PORTS,
    GuiSession,
    probe_vmcp_endpoint,
    probe_vmcp_server,
)
from vivado_mcp.vivado.session import SubprocessSession

# 向后兼容：0.1.x 代码可能 import VivadoSession
VivadoSession = SubprocessSession

logger = logging.getLogger(__name__)

# session_id 格式：1~64 个字母、数字、下划线、连字符
_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")

SessionMode = Literal["gui", "tcl", "attach"]
_VALID_MODES: tuple[str, ...] = ("gui", "tcl", "attach")

# list_sessions 主动 probe 的端口范围(默认 GUI server 池 9999..10003)
# 0.3.19 修复:用户手动启动 + init.tcl 注入的 GUI 不在 _sessions 字典中,
# 不主动 probe 就会被报"无活跃会话",误导后续 start_session 抢占端口
_EXTERNAL_PROBE_PORTS: tuple[int, ...] = tuple(range(9999, 9999 + 5))
_EXTERNAL_PROBE_TIMEOUT = 0.3  # 单端口超时,并发 probe,RST 会立即返回
# 已管理会话的探活超时(PRD A1):并发 fresh-connection probe,每个 <=1s。
# is_alive 只看 socket 半边状态,Vivado 挂死时仍报 True,必须真发一次请求验证
_KNOWN_PROBE_TIMEOUT = 1.0


def _validate_session_id(session_id: str) -> str:
    """验证 session_id 格式，拒绝非法字符。"""
    if not _SESSION_ID_RE.match(session_id):
        raise ValueError(
            f"session_id 格式非法: {session_id!r}。"
            f"仅允许字母、数字、下划线、连字符，长度 1~64。"
        )
    return session_id


def _resolve_startup_project(project_path: str | None) -> str | None:
    """Validate an exact absolute XPR without opening or modifying the project."""
    if not project_path:
        return None
    candidate = Path(project_path).expanduser()
    if not candidate.is_absolute():
        raise ValueError("project_path 必须是准确 XPR 的绝对路径。")
    if candidate.suffix.casefold() != ".xpr":
        raise ValueError("project_path 必须指向 .xpr 文件。")
    if not candidate.is_file():
        raise FileNotFoundError(f"Vivado XPR 不存在: {project_path}")
    return normalize_path(str(candidate.resolve()))


def _same_local_path(left: str, right: str) -> bool:
    """Compare local paths using the host platform's case-sensitivity rules."""
    return os.path.normcase(os.path.normpath(left)) == os.path.normcase(
        os.path.normpath(right)
    )


class SessionManager:
    """管理多个 Vivado 会话实例。"""

    def __init__(self, vivado_path: str):
        """
        Args:
            vivado_path: 默认 Vivado 可执行文件路径。
        """
        self._default_vivado_path = vivado_path
        self._sessions: dict[str, BaseSession] = {}
        # session_id → (port, pid) 轻量映射,供诊断 / stop 按 pid 兜底清理。
        # 主清理路径仍是 GuiSession 自持 pid + 自洽 stop;这里只做"self._proc
        # 引用失联时仍知道该清哪个端口/进程"的二级记录。
        self._port_map: dict[str, tuple[int | None, int | None]] = {}
        # Serialize start/stop/detach for the same logical session.  Without this,
        # two concurrent start_session calls can both pass the registry check,
        # spawn two Vivado processes and overwrite one registry entry.
        self._lifecycle_locks: dict[str, asyncio.Lock] = {}

    @property
    def default_vivado_path(self) -> str:
        return self._default_vivado_path

    def get(self, session_id: str) -> BaseSession | None:
        """获取已有会话（不自动创建）。"""
        _validate_session_id(session_id)
        session = self._sessions.get(session_id)
        if session and not session.is_alive:
            # 会话存在但进程已死，清理掉
            logger.warning("会话 '%s' 已失效，自动清理。", session_id)
            del self._sessions[session_id]
            self._port_map.pop(session_id, None)
            return None
        return session

    async def start_session(
        self,
        session_id: str = "default",
        vivado_path: str | None = None,
        vivado_version: str | None = None,
        timeout: float = 120.0,
        mode: str = "gui",
        port: int = 0,
        project_path: str | None = None,
        require_ip_integrator: bool = False,
    ) -> tuple[BaseSession, str]:
        """启动新会话或返回已有会话。

        Args:
            session_id: 会话标识符。
            vivado_path: 可选的自定义 Vivado 路径（覆盖默认值）。
            vivado_version: 可选的明确版本（如 ``2024.2``）。与显式路径同时
                提供时会验证二者一致；多版本并存时不按目录名自动选择。
            timeout: 启动超时秒数（GUI 模式建议 120s+）。
            mode: 会话模式，``"gui"`` / ``"tcl"`` / ``"attach"``。
            port: 端口哨兵(B 方案)。
                - gui 模式 port==0(默认)= auto-alloc 空闲端口启动**全新独立实例**,
                  跳过 probe(多开正解);port>0 = 先 probe 该端口命中则 attach,
                  否则 spawn 并绑该确切端口。
                - attach 模式 port 是要连的显式端口(attach 本就需知道连哪;
                  传 0 会去连 0 端口失败,attach 调用方应显式给端口)。
            project_path: GUI/Tcl/attach 模式可选的准确 XPR 绝对路径。GUI/Tcl
                会先打开它再建立 READY；attach 只核对现有 endpoint 的 XPR。
            require_ip_integrator: 要求握手证明 ``open_bd_design`` 和
                ``get_bd_pins`` 已注册；必须与 ``project_path`` 同用。

        Returns:
            (会话实例, 启动横幅/状态消息) 元组。
        """
        _validate_session_id(session_id)
        if mode not in _VALID_MODES:
            raise ValueError(
                f"无效的 mode: {mode!r}。支持: {_VALID_MODES}"
            )
        if mode == "attach" and port <= 0:
            raise ValueError("attach 模式必须提供 1..65535 的显式非零端口。")
        if port < 0 or port > 65535:
            raise ValueError("port 必须在 0..65535 范围内。")

        startup_project = _resolve_startup_project(project_path)
        project_modes = ("gui", "tcl", "attach")
        if startup_project is not None and mode not in project_modes:
            raise ValueError(
                "project_path 只允许 mode='gui'、mode='tcl' 或 mode='attach'。"
            )
        if require_ip_integrator and mode not in project_modes:
            raise ValueError(
                "require_ip_integrator 只允许 mode='gui'、mode='tcl' 或 "
                "mode='attach'。"
            )
        if require_ip_integrator and startup_project is None:
            raise ValueError(
                "require_ip_integrator=True 时必须同时提供准确 project_path。"
            )

        lifecycle_lock = self._lifecycle_locks.setdefault(
            session_id, asyncio.Lock()
        )
        async with lifecycle_lock:
            return await self._start_session_locked(
                session_id=session_id,
                vivado_path=vivado_path,
                vivado_version=vivado_version,
                timeout=timeout,
                mode=mode,
                port=port,
                project_path=startup_project,
                require_ip_integrator=require_ip_integrator,
            )

    async def _start_session_locked(
        self,
        *,
        session_id: str,
        vivado_path: str | None,
        vivado_version: str | None,
        timeout: float,
        mode: str,
        port: int,
        project_path: str | None,
        require_ip_integrator: bool,
    ) -> tuple[BaseSession, str]:
        """Start/reuse one session while its per-id lifecycle lock is held."""

        existing = self.get(session_id)
        if existing:
            self._validate_existing_request(
                existing,
                mode=mode,
                port=port,
                vivado_path=vivado_path,
                vivado_version=vivado_version,
                project_path=project_path,
                require_ip_integrator=require_ip_integrator,
            )
            return existing, (
                f"会话 '{session_id}' 已在运行中（mode={existing.mode}）。"
            )

        if vivado_path:
            path = resolve_vivado(
                vivado_version=vivado_version,
                vivado_path=vivado_path,
            )
        elif vivado_version:
            path = resolve_vivado(vivado_version=vivado_version)
        elif self._default_vivado_path:
            # Lifespan resolved this once at MCP startup; retain the exact value.
            # Re-validating here would make a later filesystem race look like a
            # version-selection decision and complicate deterministic tests.
            path = self._default_vivado_path
        else:
            path = resolve_vivado()

        session: BaseSession
        if mode == "tcl":
            tcl_kwargs: dict[str, object] = {
                "vivado_path": path,
                "session_id": session_id,
            }
            if project_path is not None:
                tcl_kwargs["startup_project_path"] = project_path
            if require_ip_integrator:
                tcl_kwargs["require_ip_integrator"] = True
            session = SubprocessSession(**tcl_kwargs)
        elif mode == "gui":
            gui_kwargs: dict[str, object] = {
                "vivado_path": path,
                "session_id": session_id,
                "port": port,
                "attach_only": False,
            }
            if project_path is not None:
                gui_kwargs["startup_project_path"] = project_path
            if require_ip_integrator:
                gui_kwargs["require_ip_integrator"] = True
            session = GuiSession(
                **gui_kwargs,
            )
        else:  # attach
            attach_kwargs: dict[str, object] = {
                "vivado_path": path,
                "session_id": session_id,
                "port": port,
                "attach_only": True,
            }
            if project_path is not None:
                attach_kwargs["startup_project_path"] = project_path
            if require_ip_integrator:
                attach_kwargs["require_ip_integrator"] = True
            session = GuiSession(**attach_kwargs)

        try:
            banner = await session.start(timeout=timeout)
        except Exception as e:
            # 兜底清理:start 失败的 session 不会进 _sessions,此后 stop_session
            # 无从触达;若 session 自己的失败清理没兜住(或 tcl 模式半启动),
            # 这里再调一次 stop() 收尾。原始异常原样上传。
            logger.warning(
                "会话 '%s' 启动失败(%s: %s),执行兜底清理",
                session_id, type(e).__name__, e,
            )
            try:
                await session.stop()
            except Exception as cleanup_err:
                logger.warning(
                    "会话 '%s' 启动失败后的兜底清理也失败: %s",
                    session_id, cleanup_err,
                )
            raise
        self._sessions[session_id] = session
        # 记一份 (port, pid) 二级映射供诊断 / stop 兜底(从 session 读,不另算)
        self._port_map[session_id] = (
            getattr(session, "connected_port", None),
            getattr(session, "pid", None),
        )

        return session, banner

    def _validate_existing_request(
        self,
        existing: BaseSession,
        *,
        mode: str,
        port: int,
        vivado_path: str | None,
        vivado_version: str | None,
        project_path: str | None,
        require_ip_integrator: bool,
    ) -> None:
        """Reject silent reuse when the requested Vivado identity differs."""
        actual_mode = existing.mode
        incompatible_mode = (
            (mode == "tcl" and actual_mode != "tcl")
            or (mode == "attach" and actual_mode != "attach")
            or (
                mode == "gui"
                and (
                    actual_mode == "tcl"
                    or getattr(existing, "_attach_only", False)
                )
            )
        )
        if incompatible_mode:
            raise ValueError(
                "同名会话 mode 不匹配: "
                f"requested={mode}, existing={actual_mode}。请换 session_id。"
            )

        if vivado_version:
            actual_version = get_vivado_version(existing.vivado_path)
            if not vivado_versions_match(vivado_version, actual_version):
                raise ValueError(
                    "同名会话 Vivado version 不匹配: "
                    f"requested={vivado_version}, existing={actual_version}。"
                )

        if vivado_path:
            requested_launcher = resolve_vivado(
                vivado_version=vivado_version,
                vivado_path=vivado_path,
            )
            if not _same_local_path(requested_launcher, existing.vivado_path):
                raise ValueError(
                    "同名会话 Vivado launcher 不匹配: "
                    f"requested={requested_launcher}, existing={existing.vivado_path}。"
                )

        if port > 0:
            existing_port = getattr(existing, "connected_port", None)
            if existing_port != port:
                raise ValueError(
                    "同名会话 port 不匹配: "
                    f"requested={port}, existing={existing_port}。"
                )

        if project_path is not None:
            existing_project = getattr(existing, "startup_project_path", None)
            if not existing_project or not _same_local_path(
                project_path, existing_project
            ):
                raise ValueError(
                    "同名会话 startup project 不匹配: "
                    f"requested={project_path}, existing={existing_project or '<无>'}。"
                )

        if require_ip_integrator:
            identity = getattr(existing, "_identity", {})
            if identity.get("ip_integrator") != "1":
                raise ValueError(
                    "同名会话未证明 IP Integrator commands 已就绪。"
                )

    async def get_or_start(
        self,
        session_id: str = "default",
        vivado_path: str | None = None,
        vivado_version: str | None = None,
        mode: str = "gui",
    ) -> BaseSession:
        """获取已有会话，若不存在则自动启动。"""
        session = self.get(session_id)
        if session:
            return session

        session, _ = await self.start_session(
            session_id=session_id,
            vivado_path=vivado_path,
            vivado_version=vivado_version,
            mode=mode,
        )
        return session

    async def stop_session(self, session_id: str) -> str:
        """关闭指定会话。

        Returns:
            操作结果描述。
        """
        _validate_session_id(session_id)
        lifecycle_lock = self._lifecycle_locks.setdefault(
            session_id, asyncio.Lock()
        )
        async with lifecycle_lock:
            session = self._sessions.get(session_id)
            if not session:
                return f"会话 '{session_id}' 不存在。"

            # stop 成功后再 pop:stop 抛异常时会话保留在 _sessions/_port_map,
            # AI 看到 [ERROR] 后可重试 stop_session。pop-before-stop 会让重试
            # 拿到"会话不存在"而 Vivado 进程仍在跑(审计 P3:孤儿失联)。
            # 异常原样上抛,工具层 wrapper 兜底成 [ERROR] 文案。
            await session.stop()
            self._sessions.pop(session_id, None)
            self._port_map.pop(session_id, None)
            return f"会话 '{session_id}' 已关闭。"

    async def close_all(self) -> None:
        """关闭所有会话（lifespan cleanup）。"""
        session_ids = list(self._sessions.keys())
        for sid in session_ids:
            session = self._sessions.pop(sid, None)
            self._port_map.pop(sid, None)
            if session:
                try:
                    await session.stop()
                except Exception as e:
                    logger.error("关闭会话 '%s' 失败: %s", sid, e)

        logger.info("所有 Vivado 会话已清理完毕。")

    async def detach_session(self, session_id: str) -> str:
        """Detach one session without terminating a visible GUI process."""
        _validate_session_id(session_id)
        lifecycle_lock = self._lifecycle_locks.setdefault(
            session_id, asyncio.Lock()
        )
        async with lifecycle_lock:
            session = self._sessions.get(session_id)
            if not session:
                return f"会话 '{session_id}' 不存在。"
            await session.detach()
            self._sessions.pop(session_id, None)
            self._port_map.pop(session_id, None)
            return f"会话 '{session_id}' 已断开；Vivado GUI 保持运行。"

    async def detach_all(self) -> None:
        """MCP shutdown cleanup: detach GUIs, stop non-persistent Tcl sessions."""
        session_ids = list(self._sessions.keys())
        for sid in session_ids:
            session = self._sessions.pop(sid, None)
            self._port_map.pop(sid, None)
            if session:
                try:
                    await session.detach()
                except Exception as exc:
                    logger.error("detach 会话 '%s' 失败: %s", sid, exc)
        logger.info("所有 Vivado 会话已 detach/清理完毕。")

    async def list_sessions(self, probe_external: bool = True) -> list[dict]:
        """列出所有会话的状态信息(含死会话,标记 is_alive=False)。

        纯只读,不会清理死会话 —— 否则 AI 连续调 list → stop 时第二次会
        拿到 "会话不存在" 的误导反馈。需要清理时显式调 prune_dead()。

        Args:
            probe_external: 是否主动 probe 9999..10003 发现未被 MCP 管理的
                外部 Vivado GUI(用户手动启动 + init.tcl 注入),同时对已管理
                的 GUI/attach 会话探活(PRD A1)。所有 probe 包进 to_thread
                并发跑,总耗时 ≈ 单个最大超时,不阻塞 event loop。默认
                True。关掉 probe 在不需要网络访问的场景(测试 / 离线诊断)更快。

        Returns:
            列表中每条 dict 至少含 ``session_id`` / ``mode`` / ``state``。
            外部 GUI 的 entry 会带 ``owner="external"`` + ``port`` 字段,
            ``session_id`` 形如 ``"<external@9999>"``,**不能**作为 stop_session
            的参数(MCP 没管它,无权关闭)。探活失败的已管理会话带
            ``responsive=False`` + 中性 note(未响应 ≠ 已死,is_alive 不翻转)。
        """
        sessions = list(self._sessions.values())
        known = [s.status_dict() for s in sessions]

        if not probe_external:
            return known

        # —— PRD A1:已管理会话探活目标筛选 ——
        # 用 fresh-connection 的 probe_vmcp_server 而非在会话自有连接上探活:
        # execute 超时后自有连接上可能残留迟到的响应,在其上发探测会把旧响应
        # 误读成探活响应 → 把活着的会话误标死亡;fresh 连接对主连接零污染。
        # 守卫:BUSY(MCP 侧命令在飞)或 _pending_response(上一条命令已超时
        # 但 Vivado 仍在跑,单线程 event loop 不会服务新连接)都跳过探活,
        # 避免把"忙"误判成"死"(审计 P1)。
        probe_targets: list[tuple[dict, int]] = []
        for s, entry in zip(sessions, known):
            port = entry.get("port")
            if (
                port is not None
                and entry.get("is_alive")
                and entry.get("state") not in {"busy", "stopping"}
                and not getattr(s, "_pending_response", False)
            ):
                probe_targets.append((entry, port))

        # 已被 MCP 管理的端口跳过外部 probe,避免把同一个 server 列两次;
        # 正在 spawn 中、尚未注册进 _sessions 的自己的 GUI 端口同样跳过,
        # 避免把它误报为 owner="external"(实际是 MCP 自己正在启动的)
        occupied = {
            entry["port"] for entry in known
            if entry.get("port") is not None
        }
        external_ports = [
            port for port in _EXTERNAL_PROBE_PORTS
            if port not in occupied and port not in _PENDING_SPAWN_PORTS
        ]

        # 同步 socket probe 包进 to_thread 并发执行:不再阻塞 event loop,
        # 总耗时 ≈ 单个最大超时(此前串行最坏 1s×N + 1.5s,审计 P2)
        results = await asyncio.gather(
            *[
                asyncio.to_thread(
                    probe_vmcp_server, "127.0.0.1", port, _KNOWN_PROBE_TIMEOUT
                )
                for _, port in probe_targets
            ],
            *[
                asyncio.to_thread(
                    probe_vmcp_endpoint,
                    "127.0.0.1",
                    port,
                    _EXTERNAL_PROBE_TIMEOUT,
                )
                for port in external_ports
            ],
        )
        known_results = results[: len(probe_targets)]
        ext_results = results[len(probe_targets):]

        for (entry, _), ok in zip(probe_targets, known_results):
            if not ok:
                # 不翻转 is_alive、不无条件建议 stop:「未响应」≠「已死」——
                # 用户在 GUI 里跑长 Tcl / 已超时命令仍在跑都会让短探测落空,
                # 此时建议 stop_session 会引导 AI 杀掉健康会话(审计 P1)
                entry["responsive"] = False
                entry["note"] = (
                    f"探活失败:未在 {_KNOWN_PROBE_TIMEOUT:g}s 内响应。"
                    "可能正在执行长命令(含已超时仍在跑的命令)或已挂死;"
                    "若你最近有命令超时,请等它完成,勿立即 stop_session。"
                )

        external = []
        for port, result in zip(external_ports, ext_results):
            ok, identity = result
            if not ok:
                continue
            entry = {
                "session_id": f"<external@{port}>",
                "mode": "external",
                "state": "ready",
                "vivado_path": "<unknown, not managed by MCP>",
                "vivado_version": identity.get("vivado", "unknown"),
                "is_alive": True,
                "uptime_seconds": None,
                "port": port,
                "owner": "external",
                "note": (
                    "未由 MCP 启动的 Vivado(可能是手动启动并装过 init.tcl)。"
                    "attach 前先核对 identity.project/xpr/vivado;"
                    "stop_session 无权关闭 —— 请在 GUI 内手动 exit。"
                ),
            }
            if identity:
                entry["identity"] = identity
                entry["project"] = identity.get("project", "")
                entry["xpr"] = identity.get("xpr", "")
            external.append(entry)

        return known + external

    def prune_dead(self) -> list[str]:
        """清理已死亡的会话条目,返回被清理的 session_id 列表。"""
        dead = [
            sid for sid, s in self._sessions.items()
            if not s.is_alive
        ]
        for sid in dead:
            del self._sessions[sid]
            self._port_map.pop(sid, None)
        return dead
