"""会话管理工具：start_session / stop_session / list_sessions。"""

import json

from mcp.server.fastmcp import Context

from vivado_mcp.server import _get_manager, mcp


@mcp.tool()
async def start_session(
    session_id: str = "default",
    mode: str = "gui",
    port: int = 9999,
    vivado_path: str = "",
    timeout: int = 120,
    ctx: Context = None,
) -> str:
    """启动一个新的 Vivado 会话。

    三种模式：
    - ``"gui"`` (默认) — MCP 自动 spawn ``vivado -mode gui``，你能看到 Vivado 图标
      并实时观察 Tcl Console / Block Design / 波形等 GUI 内容。首次使用会自动
      通过 ``-source`` 注入 TCP server，或先运行一次 ``vivado-mcp install`` 持久化。
    - ``"tcl"`` — ``vivado -mode tcl`` 无头子进程（无 GUI，适合 CI / 批处理）。
    - ``"attach"`` — 连接到用户已手动打开的 Vivado GUI（需先运行 ``vivado-mcp install``
      让 init.tcl 自动开启 TCP server）。

    每个 session_id 对应一个独立的 Vivado 实例，支持多会话并行。

    Args:
        session_id: 会话标识符，默认 "default"。
        mode: ``"gui"`` / ``"tcl"`` / ``"attach"``，默认 ``"gui"``。
        port: attach 模式的首选 TCP 端口，默认 9999。
        vivado_path: 可选，自定义 Vivado 可执行文件路径。留空则自动检测。
        timeout: 启动超时秒数，GUI 模式建议 120+。默认 120。
    """
    manager = _get_manager(ctx)

    path = vivado_path if vivado_path else None
    try:
        session, banner = await manager.start_session(
            session_id=session_id,
            vivado_path=path,
            timeout=float(timeout),
            mode=mode,
            port=int(port),
        )
        status = session.status_dict()
        return (
            f"会话 '{session_id}' 已就绪（mode={status['mode']}）。\n"
            f"Vivado: {status['vivado_path']}\n"
            f"状态: {status['state']}\n\n"
            f"--- 启动信息 ---\n{banner}"
        )
    except ValueError as e:
        return f"[ERROR] {e}"
    except Exception as e:
        return f"[ERROR] 启动会话 '{session_id}' 失败: {e}"


@mcp.tool()
async def stop_session(
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """关闭指定的 Vivado 会话。

    Args:
        session_id: 要关闭的会话标识符。
    """
    manager = _get_manager(ctx)
    return await manager.stop_session(session_id)


@mcp.tool()
async def list_sessions(ctx: Context = None) -> str:
    """列出所有活跃的 Vivado 会话及其状态。"""
    manager = _get_manager(ctx)
    sessions = manager.list_sessions()

    if not sessions:
        return "当前没有活跃的 Vivado 会话。使用 start_session 启动一个新会话。"

    return json.dumps(sessions, indent=2, ensure_ascii=False)
