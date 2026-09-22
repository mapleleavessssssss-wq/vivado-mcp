"""MCPServer 服务器实例、lifespan 管理、工具注册、Resources & Prompts。

架构：
  Claude Code ──(stdio)──▶ MCPServer
                                │
                          SessionManager (lifespan context)
                          ├─ "default" ──▶ vivado -mode tcl
                          └─ ...
"""

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from mcp.server import MCPServer

from vivado_mcp import prompts as _prompts
from vivado_mcp.config import find_vivado
from vivado_mcp.vivado.session import VivadoSession
from vivado_mcp.vivado.session_manager import SessionManager

# 兼容历史上从 server 模块直接导入 Prompt 函数的调用方。
fpga_workflow = _prompts.fpga_workflow
debug_timing = _prompts.debug_timing
debug_gt_mapping = _prompts.debug_gt_mapping
debug_ip_config = _prompts.debug_ip_config
debug_pcie = _prompts.debug_pcie
simulation_bringup = _prompts.simulation_bringup
cdc_audit = _prompts.cdc_audit
ila_hardware_debug = _prompts.ila_hardware_debug

# 配置日志:默认 WARNING(logging-guidelines §2:INFO/DEBUG 不出现在生产用户终端,
# 用户必须看到的信息走 WARNING+),调试时设环境变量 LOG_LEVEL=INFO/DEBUG 覆盖。
_LOG_LEVEL = os.environ.get("LOG_LEVEL", "WARNING").upper()
if _LOG_LEVEL not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
    _LOG_LEVEL = "WARNING"
logging.basicConfig(
    level=_LOG_LEVEL,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

# 模块级 SessionManager 引用，供 Resources 使用（lifespan 中设置）
_manager_ref: SessionManager | None = None


@dataclass
class AppContext:
    """应用上下文，通过 lifespan 注入到所有工具函数中。"""
    session_manager: SessionManager


@asynccontextmanager
async def app_lifespan(server: MCPServer) -> AsyncIterator[AppContext]:
    """MCP 服务器生命周期管理。

    启动时初始化 SessionManager，关闭时清理所有 Vivado 会话。
    """
    global _manager_ref

    # 检测 Vivado 路径（启动时即验证，快速报错）
    try:
        vivado_path = find_vivado()
        logger.info("检测到 Vivado: %s", vivado_path)
    except FileNotFoundError as e:
        logger.warning("Vivado 路径检测失败: %s", e)
        logger.warning("工具仍可使用，但需要在 start_session 时手动指定路径。")
        vivado_path = ""

    manager = SessionManager(vivado_path=vivado_path)
    _manager_ref = manager
    try:
        yield AppContext(session_manager=manager)
    finally:
        _manager_ref = None
        await manager.close_all()


# 创建 MCPServer 实例
mcp = MCPServer(
    "vivado-mcp",
    lifespan=app_lifespan,
)


# --------------------------------------------------------------------------- #
#  辅助函数（DRY：所有工具共享）
# --------------------------------------------------------------------------- #

def _get_manager(ctx) -> SessionManager:
    """从 MCP Context 中提取 SessionManager。"""
    app_ctx: AppContext = ctx.request_context.lifespan_context
    return app_ctx.session_manager


_NO_SESSION = "[ERROR] 会话 '{sid}' 不存在。请先调用 start_session。"


def _require_session(ctx, session_id: str) -> VivadoSession | None:
    """获取会话，不存在返回 None。"""
    return _get_manager(ctx).get(session_id)


async def _safe_execute(
    session: VivadoSession,
    tcl: str,
    timeout: float,
    error_label: str,
) -> str:
    """返回 Tcl 执行摘要；异常时保留原因，超时不推断命令已停止。"""
    try:
        result = await session.execute(tcl, timeout=timeout)
        return result.summary
    except Exception as e:
        msg = f"[ERROR] {error_label}: {e}"
        # Python 3.10 的 asyncio.TimeoutError 与内置 TimeoutError 是不同类型。
        if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
            msg += "\n等待响应超时不代表命令已停止，请先确认会话状态。"
        return msg


# --------------------------------------------------------------------------- #
#  MCP Resources（会话状态查询）
#  注意：Resources 不支持 Context 注入，使用模块级 _manager_ref
# --------------------------------------------------------------------------- #

@mcp.resource("vivado://sessions")
async def resource_sessions() -> str:
    """所有 Vivado 会话的状态信息（JSON）。"""
    if _manager_ref is None:
        return json.dumps({"sessions": [], "message": "服务器未就绪"})
    # list_sessions 已 async 化(probe 并发跑,不阻塞 event loop),resource 同步跟进
    sessions = await _manager_ref.list_sessions()
    if not sessions:
        return json.dumps({"sessions": [], "message": "当前没有活跃会话"})
    return json.dumps({"sessions": sessions}, ensure_ascii=False)


@mcp.resource("vivado://session/{session_id}/status")
def resource_session_status(session_id: str) -> str:
    """单个 Vivado 会话的详细状态（JSON）。"""
    if _manager_ref is None:
        return json.dumps({"error": "服务器未就绪"})
    session = _manager_ref.get(session_id)
    if not session:
        return json.dumps({"error": f"会话 '{session_id}' 不存在"})
    return json.dumps(session.status_dict(), ensure_ascii=False)


# --------------------------------------------------------------------------- #
#  MCP Prompts（工作流引导）
# --------------------------------------------------------------------------- #

# 注册顺序是对外兼容契约：旧 5 项顺序不变，新工作流仅追加。
_prompts.register_prompts(mcp)


# --------------------------------------------------------------------------- #
#  导入工具模块，触发 @mcp.tool() 装饰器注册
# --------------------------------------------------------------------------- #

import vivado_mcp.tools.cdc_tools  # noqa: E402, F401
import vivado_mcp.tools.diagnostic_tools  # noqa: E402, F401
import vivado_mcp.tools.flow_tools  # noqa: E402, F401
import vivado_mcp.tools.introspect_tools  # noqa: E402, F401
import vivado_mcp.tools.ip_tools  # noqa: E402, F401
import vivado_mcp.tools.report_tools  # noqa: E402, F401
import vivado_mcp.tools.session_tools  # noqa: E402, F401
import vivado_mcp.tools.tcl_tools  # noqa: E402, F401
import vivado_mcp.tools.wave_tools  # noqa: E402, F401
import vivado_mcp.tools.waveform_query  # noqa: E402, F401
