"""SessionManager：多 Vivado 实例管理。

每个 session_id 对应一个独立的会话实例。支持三种模式：
- ``mode="gui"`` (默认)—— MCP spawn ``vivado -mode gui``，你能看到 Vivado 图标
- ``mode="tcl"`` —— ``vivado -mode tcl`` 无头子进程（CI / 批处理友好）
- ``mode="attach"`` —— 连接到用户已手动打开的 Vivado GUI（需先 ``vivado-mcp install``）
"""

import logging
import re
from typing import Literal

from vivado_mcp.vivado.base_session import BaseSession
from vivado_mcp.vivado.gui_session import GuiSession
from vivado_mcp.vivado.session import SubprocessSession

# 向后兼容：0.1.x 代码可能 import VivadoSession
VivadoSession = SubprocessSession

logger = logging.getLogger(__name__)

# session_id 格式：1~64 个字母、数字、下划线、连字符
_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")

SessionMode = Literal["gui", "tcl", "attach"]
_VALID_MODES: tuple[str, ...] = ("gui", "tcl", "attach")


def _validate_session_id(session_id: str) -> str:
    """验证 session_id 格式，拒绝非法字符。"""
    if not _SESSION_ID_RE.match(session_id):
        raise ValueError(
            f"session_id 格式非法: {session_id!r}。"
            f"仅允许字母、数字、下划线、连字符，长度 1~64。"
        )
    return session_id


class SessionManager:
    """管理多个 Vivado 会话实例。"""

    def __init__(self, vivado_path: str):
        """
        Args:
            vivado_path: 默认 Vivado 可执行文件路径。
        """
        self._default_vivado_path = vivado_path
        self._sessions: dict[str, BaseSession] = {}

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
            return None
        return session

    async def start_session(
        self,
        session_id: str = "default",
        vivado_path: str | None = None,
        timeout: float = 120.0,
        mode: str = "gui",
        port: int = 9999,
    ) -> tuple[BaseSession, str]:
        """启动新会话或返回已有会话。

        Args:
            session_id: 会话标识符。
            vivado_path: 可选的自定义 Vivado 路径（覆盖默认值）。
            timeout: 启动超时秒数（GUI 模式建议 120s+）。
            mode: 会话模式，``"gui"`` / ``"tcl"`` / ``"attach"``。
            port: attach 模式首选端口（默认 9999）。

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

        banner = await session.start(timeout=timeout)
        self._sessions[session_id] = session

        return session, banner

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
        session = self._sessions.pop(session_id, None)
        if not session:
            return f"会话 '{session_id}' 不存在。"

        await session.stop()
        return f"会话 '{session_id}' 已关闭。"

    async def close_all(self) -> None:
        """关闭所有会话（lifespan cleanup）。"""
        session_ids = list(self._sessions.keys())
        for sid in session_ids:
            session = self._sessions.pop(sid, None)
            if session:
                try:
                    await session.stop()
                except Exception as e:
                    logger.error("关闭会话 '%s' 失败: %s", sid, e)

        logger.info("所有 Vivado 会话已清理完毕。")

    def list_sessions(self) -> list[dict]:
        """列出所有会话的状态信息(含死会话,标记 is_alive=False)。

        纯只读,不会清理死会话 —— 否则 AI 连续调 list → stop 时第二次会
        拿到 "会话不存在" 的误导反馈。需要清理时显式调 prune_dead()。
        """
        return [s.status_dict() for s in self._sessions.values()]

    def prune_dead(self) -> list[str]:
        """清理已死亡的会话条目,返回被清理的 session_id 列表。"""
        dead = [
            sid for sid, s in self._sessions.items()
            if not s.is_alive
        ]
        for sid in dead:
            del self._sessions[sid]
        return dead
