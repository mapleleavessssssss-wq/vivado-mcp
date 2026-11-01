"""SessionManager：多 Vivado 实例管理。

每个 session_id 对应一个独立的会话实例。支持三种模式：
- ``mode="gui"`` (默认)—— MCP spawn ``vivado -mode gui``，你能看到 Vivado 图标
- ``mode="tcl"`` —— ``vivado -mode tcl`` 无头子进程（CI / 批处理友好）
- ``mode="attach"`` —— 连接到用户已手动打开的 Vivado GUI（需先 ``vivado-mcp install``）
"""

import asyncio
import logging
import re
from typing import Literal

from vivado_mcp.vivado.base_session import BaseSession
from vivado_mcp.vivado.gui_session import (
    _PENDING_SPAWN_PORTS,
    GuiSession,
    format_death_report,
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
# 已退出会话的记录上限,超过时淘汰最旧的。
_MAX_DEAD_RECORDS = 10
# 列表里每条记录的每段尾部上限。单条诊断保留完整长度。
_LISTED_TAIL_CHARS = 1000
_TAIL_FIELDS = ("hs_err_text", "log_tail", "launcher_tail")


def _validate_session_id(session_id: str) -> str:
    """验证 session_id 格式，拒绝非法字符。"""
    if not _SESSION_ID_RE.match(session_id):
        raise ValueError(
            f"session_id 格式非法: {session_id!r}。"
            f"仅允许字母、数字、下划线、连字符，长度 1~64。"
        )
    return session_id


def _build_death_report(session_id: str, session: BaseSession) -> dict:
    """生成退出诊断。诊断自身出错不能影响调用它的工具,失败时降级为最小记录。"""
    try:
        build = getattr(session, "death_report", None)
        if build is not None:
            return build()
        return {
            **session.status_dict(),
            "state": "dead",
            "is_alive": False,
            "exit_code": None,
            "note": "Tcl 模式会话,进程已退出或管道已断开,没有详细诊断。",
        }
    except Exception as e:
        logger.warning("生成会话 '%s' 的退出诊断失败: %s", session_id, e)
        return {
            "session_id": session_id,
            "state": "dead",
            "is_alive": False,
            "exit_code": None,
            "note": f"[DEGRADED] 生成退出诊断失败: {e}",
        }


def _brief_report(report: dict) -> dict:
    """生成列表用的诊断副本:每段尾部只留最后 1000 字符,存着的诊断不变。"""
    brief = dict(report)
    for key in _TAIL_FIELDS:
        text = brief.get(key)
        if text and len(text) > _LISTED_TAIL_CHARS:
            brief[key] = text[-_LISTED_TAIL_CHARS:]
            brief["tails_truncated"] = (
                f"每段尾部只显示最后 {_LISTED_TAIL_CHARS} 字符,"
                "完整内容看 vivado://session/<id>/status"
            )
    return brief


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
        # 已退出会话:session_id → (会话对象, 退出诊断),按退出先后排列。
        # 对象留着是因为外层进程和临时脚本要等 stop 才清理。
        self._dead: dict[str, tuple[BaseSession, dict]] = {}
        self._evicting: set[asyncio.Task[None]] = set()

    @property
    def default_vivado_path(self) -> str:
        return self._default_vivado_path

    def get(self, session_id: str) -> BaseSession | None:
        """获取已有会话（不自动创建）。"""
        _validate_session_id(session_id)
        session = self._sessions.get(session_id)
        if session and not session.is_alive:
            # 会话存在但进程已死，转入退出记录，保留诊断
            logger.warning("会话 '%s' 已失效，转入退出记录。", session_id)
            self._retire(session_id)
            return None
        return session

    def _retire(self, session_id: str) -> None:
        """把已退出的会话从 _sessions 移到 _dead,并限制记录数量。"""
        session = self._sessions.pop(session_id, None)
        self._port_map.pop(session_id, None)
        if session is None:
            return
        replaced = self._dead.pop(session_id, None)
        if replaced is not None:
            self._stop_in_background(session_id, replaced[0])
        self._dead[session_id] = (session, _build_death_report(session_id, session))
        while len(self._dead) > _MAX_DEAD_RECORDS:
            oldest = next(iter(self._dead))
            logger.warning("退出记录超过 %d 个,淘汰最旧的 '%s'", _MAX_DEAD_RECORDS, oldest)
            self._stop_in_background(oldest, self._dead.pop(oldest)[0])

    def _stop_in_background(self, session_id: str, session: BaseSession) -> None:
        """被淘汰的记录仍要 stop(外层进程可能还在)。_retire 是同步的,只能排任务。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("没有事件循环,未能清理被淘汰的会话 '%s'", session_id)
            return
        task = loop.create_task(self._stop_quietly(session_id, session))
        self._evicting.add(task)
        task.add_done_callback(self._evicting.discard)

    @staticmethod
    async def _stop_quietly(session_id: str, session: BaseSession) -> None:
        try:
            await session.stop()
        except Exception as e:
            logger.warning("清理已退出的会话 '%s' 失败: %s", session_id, e)

    def dead_report(self, session_id: str) -> dict | None:
        """取已退出会话的诊断,没有记录返回 None。"""
        entry = self._dead.get(session_id)
        if entry is None:
            return None
        session, report = entry
        # 退休时外层进程可能还没被回收,退出码之后才拿得到。
        if report.get("exit_code") is None and getattr(session, "exit_code", None) is not None:
            report = _build_death_report(session_id, session)
            self._dead[session_id] = (session, report)
        return report

    def no_session_message(self, session_id: str) -> str:
        """拿不到会话时给工具层的错误文本。"""
        report = self.dead_report(session_id)
        if report is not None:
            return format_death_report(report)
        return f"[ERROR] 会话 '{session_id}' 不存在。请先调用 start_session。"

    async def start_session(
        self,
        session_id: str = "default",
        vivado_path: str | None = None,
        timeout: float = 120.0,
        mode: str = "gui",
        port: int = 0,
    ) -> tuple[BaseSession, str]:
        """启动新会话或返回已有会话。

        Args:
            session_id: 会话标识符。
            vivado_path: 可选的自定义 Vivado 路径（覆盖默认值）。
            timeout: 启动超时秒数（GUI 模式建议 120s+）。
            mode: 会话模式，``"gui"`` / ``"tcl"`` / ``"attach"``。
            port: 端口哨兵(B 方案)。
                - gui 模式 port==0(默认)= auto-alloc 空闲端口启动**全新独立实例**,
                  跳过 probe(多开正解);port>0 = 先 probe 该端口命中则 attach,
                  否则 spawn 并绑该确切端口。
                - attach 模式 port 是要连的显式端口(attach 本就需知道连哪;
                  传 0 会去连 0 端口失败,attach 调用方应显式给端口)。

        Returns:
            (会话实例, 启动横幅/状态消息) 元组。
        """
        _validate_session_id(session_id)
        if mode not in _VALID_MODES:
            raise ValueError(
                f"无效的 mode: {mode!r}。支持: {_VALID_MODES}"
            )

        existing = self.get(session_id)
        if existing:
            return existing, (
                f"会话 '{session_id}' 已在运行中（mode={existing.mode}）。"
            )

        # 同 id 的退出记录作废。旧对象的外层进程和临时脚本要先清理。
        stale_note = ""
        stale = self._dead.pop(session_id, None)
        if stale is not None:
            try:
                await stale[0].stop()
            except Exception as e:
                logger.warning("清理已退出的旧会话 '%s' 失败: %s", session_id, e)
                stale_note = f"\n[DEGRADED] 清理已退出的旧会话失败: {e}"

        path = vivado_path or self._default_vivado_path

        session: BaseSession
        if mode == "tcl":
            session = SubprocessSession(vivado_path=path, session_id=session_id)
        elif mode == "gui":
            session = GuiSession(
                vivado_path=path,
                session_id=session_id,
                port=port,
                attach_only=False,
            )
        else:  # attach
            session = GuiSession(
                vivado_path=path,
                session_id=session_id,
                port=port,
                attach_only=True,
            )

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

        return session, banner + stale_note

    async def get_or_start(
        self,
        session_id: str = "default",
        vivado_path: str | None = None,
        mode: str = "gui",
    ) -> BaseSession:
        """获取已有会话，若不存在则自动启动。"""
        session = self.get(session_id)
        if session:
            return session

        session, _ = await self.start_session(
            session_id=session_id,
            vivado_path=vivado_path,
            mode=mode,
        )
        return session

    async def stop_session(self, session_id: str) -> str:
        """关闭指定会话。

        Returns:
            操作结果描述。
        """
        session = self._sessions.get(session_id)
        if session and not session.is_alive:
            self._retire(session_id)
            session = None
        if not session:
            return await self._stop_dead_session(session_id)

        # stop 成功后再 pop:stop 抛异常时会话保留在 _sessions/_port_map,
        # AI 看到 [ERROR] 后可重试 stop_session。pop-before-stop 会让重试
        # 拿到"会话不存在"而 Vivado 进程仍在跑(审计 P3:孤儿失联)。
        # 异常原样上抛,工具层 wrapper 兜底成 [ERROR] 文案。
        await session.stop()
        self._sessions.pop(session_id, None)
        self._port_map.pop(session_id, None)
        # stop 期间若有工具调用 get(),会把正在关闭的会话误转入退出记录。
        self._dead.pop(session_id, None)
        return f"会话 '{session_id}' 已关闭。"

    async def _stop_dead_session(self, session_id: str) -> str:
        """清理已退出的会话并清除记录。stop 失败时保留记录,异常上抛,可重试。"""
        report = self.dead_report(session_id)
        if report is None:
            return f"会话 '{session_id}' 不存在。"
        await self._dead[session_id][0].stop()
        self._dead.pop(session_id, None)
        code = report.get("exit_code")
        kept = f"工作目录 {report['cwd']} 里的文件未删除。" if report.get("cwd") else ""
        return (
            f"会话 '{session_id}' 的 Vivado 此前已退出"
            f"(退出码 {'未知' if code is None else code}),已清理并清除记录。{kept}"
        )

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

        for sid in list(self._dead):
            session, _ = self._dead.pop(sid)
            await self._stop_quietly(sid, session)
        if self._evicting:
            await asyncio.gather(*self._evicting)

        logger.info("所有 Vivado 会话已清理完毕。")

    async def list_sessions(self, probe_external: bool = True) -> list[dict]:
        """列出所有会话的状态信息(含已退出会话的诊断,标记 is_alive=False)。

        已退出的会话会转入退出记录并列出,不会丢失:AI 连续调 list → stop 时,
        stop_session 仍能找到它,不会得到 "会话不存在"。记录要 stop_session 或
        同 id 的 start_session 才清除。

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
        self.prune_dead()
        sessions = list(self._sessions.values())
        known = [s.status_dict() for s in sessions]
        dead = [_brief_report(self.dead_report(sid)) for sid in list(self._dead)]

        if not probe_external:
            return known + dead

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
                    probe_vmcp_server, "127.0.0.1", port, _EXTERNAL_PROBE_TIMEOUT
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
        for port, ok in zip(external_ports, ext_results):
            if not ok:
                continue
            external.append({
                "session_id": f"<external@{port}>",
                "mode": "external",
                "state": "ready",
                "vivado_path": "<unknown, not managed by MCP>",
                "is_alive": True,
                "uptime_seconds": None,
                "port": port,
                "owner": "external",
                "note": (
                    "未由 MCP 启动的 Vivado(可能是手动启动并装过 init.tcl)。"
                    "调 start_session(mode='gui') 会自动 attach 上去;"
                    "stop_session 无权关闭 —— 请在 GUI 内手动 exit。"
                ),
            })

        return known + dead + external

    def prune_dead(self) -> list[str]:
        """把已死亡的会话转入退出记录,返回被转入的 session_id 列表。"""
        dead = [
            sid for sid, s in self._sessions.items()
            if not s.is_alive
        ]
        for sid in dead:
            self._retire(sid)
        return dead
