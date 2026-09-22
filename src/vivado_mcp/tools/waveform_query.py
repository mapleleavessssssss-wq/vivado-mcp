"""无需 Vivado 会话的有界离线 VCD 查询工具。"""

import asyncio
import json
import logging

from vivado_mcp.analysis.vcd_parser import VcdError, query_vcd
from vivado_mcp.server import mcp

logger = logging.getLogger(__name__)


@mcp.tool()
async def query_waveform(
    file_path: str,
    signals: list[str] | None = None,
    start_time: int = 0,
    end_time: int | None = None,
    max_events: int = 100,
    condition: dict | None = None,
) -> str:
    """离线发现 VCD 信号或查询有界波形变化，返回 JSON。

    无需 Vivado 会话。仅 .vcd，最大 32 MiB，不支持 FST/WDB。
    event/real/string 可发现但不可查询，event 瞬时触发不能视为持久逻辑位。
    返回 timescale 与整数 ticks；同一时间戳更新合并，initial_values 是
    start_time 全部更新后的值，events 是之后的变化。条件查询返回 matches，
    equals/unknown/all_equals 也检查初值，change 仅检查之后的变化。
    scan_truncated/result_truncated 表示扫描/结果不完整，不能据此断言无异常；
    simulation_verdict 始终为 not_evaluated。

    Args:
        file_path: 本地 VCD 文件路径。
        signals: 最多 32 个精确层级路径，如 top.data；为空时发现信号/层级。
        start_time: 非负整数 VCD tick，默认 0。
        end_time: 包含此时刻的结束 tick，默认读到文件末尾。
        max_events: 1..1000，变化/条件匹配上限，发现模式用作信号上限。
        condition: 可选结构化条件，信号须已选中，位值为二进制字符串（可 x/z）。
            {"op":"equals","signal":"top.data","value":"0001"}；
            {"op":"change","signal":"top.data"}；
            {"op":"unknown","signal":"top.data"}；
            {"op":"all_equals","values":{"top.valid":"1","top.ready":"1"}}。
            不支持表达式、通配符或 eval；短位值按 VCD 宽度扩展。
    """
    try:
        result = await asyncio.to_thread(
            query_vcd, file_path, signals, start_time, end_time, max_events, condition,
        )
    except (VcdError, OSError, ValueError) as exc:
        logger.warning("离线 VCD 查询失败: %s", exc)
        result = {
            "schema_version": 1, "success": False,
            "error_code": exc.code if isinstance(exc, VcdError) else "file_error",
            "error": str(exc), "simulation_verdict": "not_evaluated",
        }
    return json.dumps(result, ensure_ascii=False)
