"""报告工具：get_io_report / get_timing_report。

两个工具都把 Vivado 原始表格文本解析为结构化数据（JSON / 格式化文本），
便于 LLM 精确提取数值。通用的报告命令（utilization / power / drc 等）
请直接用 ``run_tcl("report_xxx -return_string")``，无需包装。
"""

import json

from mcp.server.fastmcp import Context

from vivado_mcp.analysis.io_parser import parse_report_io
from vivado_mcp.analysis.timing_parser import format_timing_report, parse_timing_summary
from vivado_mcp.server import _NO_SESSION, _require_session, mcp


@mcp.tool()
async def get_io_report(
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """获取结构化 IO 引脚报告（JSON）。

    执行 report_io 并解析为结构化数据，包含：
    - 每个端口的引脚、站点、方向、IO 标准、Bank
    - GT / GPIO 类型自动判定
    - 汇总统计（总数、GT 数、GPIO 数、未分配数）

    Args:
        session_id: 目标会话 ID。
    """
    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    # 直接获取完整输出（不经 _safe_execute，避免 .summary 截断）
    try:
        result = await session.execute(
            "report_io -return_string", timeout=60.0
        )
        # B2 修复：命令失败时不要把错误文本送进 parser（否则解析出 0 个端口等假结果）
        if result.is_error:
            return (
                f"[ERROR] 获取 IO 报告失败（rc={result.return_code}）：\n"
                f"{result.output}\n\n"
                "提示: report_io 需要打开综合或实现后的设计。"
                "请先运行 run_synthesis 或 run_implementation。"
            )
        io_report = parse_report_io(result.output)
        return json.dumps(io_report.to_dict(), ensure_ascii=False, indent=2)
    except Exception as e:
        return f"[ERROR] 获取 IO 报告失败: {e}"


@mcp.tool()
async def get_timing_report(
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """获取结构化时序报告。

    执行 report_timing_summary 并解析为结构化摘要 + 关键路径详情。
    返回人类可读的中文时序分析报告，包含 PASS/FAIL 状态判定。

    Args:
        session_id: 目标会话 ID。
    """
    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    # 直接获取完整输出（不经 _safe_execute，避免 .summary 截断）
    try:
        result = await session.execute(
            "report_timing_summary -return_string", timeout=120.0
        )
        # B2 修复：命令失败时直接返回错误，避免 parser 默认值 WNS=0 被误判为 PASS
        if result.is_error:
            return (
                f"[ERROR] 获取时序报告失败（rc={result.return_code}）：\n"
                f"{result.output}\n\n"
                "提示: report_timing_summary 需要打开综合或实现后的设计。"
                "请先运行 run_synthesis 或 run_implementation。"
            )
        timing_report = parse_timing_summary(result.output)
        return format_timing_report(timing_report)
    except Exception as e:
        return f"[ERROR] 获取时序报告失败: {e}"
