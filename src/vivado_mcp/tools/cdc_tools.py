"""现场和离线 CDC 报告的结构化、有限证据入口。"""

import asyncio
import logging

from mcp.server.mcpserver import Context

from vivado_mcp.analysis.cdc_parser import (
    MAX_CDC_BYTES,
    cdc_error,
    parse_cdc_report,
    serialize_cdc_report,
)
from vivado_mcp.analysis.timing_evidence import read_bounded_text
from vivado_mcp.server import _NO_SESSION, _require_session, mcp

logger = logging.getLogger(__name__)


@mcp.tool()
async def get_cdc_report(
    session_id: str = "default", ctx: Context = None,
    report_file: str = "", max_details: int = 100,
) -> str:
    """解析 CDC 报告，按时钟对、严重级别和规则计数并返回有界明细 JSON。

    现场执行 report_cdc -details -return_string；离线读取原始 UTF-8 报告。
    计数单位为报告检查项，摘要与明细不重复累加，不能当总线位/端点数。
    未验证完整时钟/IO 约束覆盖，默认报告可能隐藏豁免；signoff 始终 false，
    空结果或 All paths are Safely Timed 不代表 CDC clean。本工具不修改约束。

    Args:
        session_id: 现场查询会话 ID。
        report_file: 可选本地原始 report_cdc -details 文本，最多 16 MiB，无需会话。
        max_details: 返回明细上限，0..500；超过上限仍统计完整输入并明确截断。
    """
    provenance = {"source": "report_file" if report_file else "live",
                  "report_file": report_file, "session_id": "" if report_file else session_id}
    try:
        if (isinstance(max_details, bool) or not isinstance(max_details, int)
                or not 0 <= max_details <= 500):
            raise ValueError("max_details 必须是 0..500 的整数")
        if report_file:
            raw = await asyncio.to_thread(read_bounded_text, report_file, MAX_CDC_BYTES)
        else:
            session = _require_session(ctx, session_id)
            if not session:
                raise ValueError(_NO_SESSION.format(sid=session_id))
            response = await session.execute("report_cdc -details -return_string", timeout=120.0)
            if response.is_error:
                raise RuntimeError(
                    f"report_cdc rc={response.return_code}: {response.output[:2000]}"
                )
            raw = response.output
        result = await asyncio.to_thread(parse_cdc_report, raw, max_details, **provenance)
        if result["parse_status"] != "ok":
            logger.warning("CDC 报告解析降级: %s", "; ".join(result["diagnostics"]))
    except Exception as exc:
        logger.warning("CDC 报告查询失败: %s", exc)
        result = cdc_error(str(exc), **provenance)
    return serialize_cdc_report(result)
