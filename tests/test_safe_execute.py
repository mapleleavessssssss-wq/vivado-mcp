"""Tcl 返回透传与异常处理，不追加基于关键词的踩坑建议。"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from vivado_mcp.server import _safe_execute
from vivado_mcp.vivado.tcl_utils import TclResult


@pytest.mark.asyncio
@pytest.mark.parametrize("return_code", [0, 1])
@pytest.mark.parametrize(("command", "output"), [
    ("puts 3.3", "3.3"),
    ("puts {}", ""),
    ("launch_runs synth_1", "ERROR: failed due to earlier errors"),
    ("open_wave_database example.wdb",
     "ERROR: [Common 17-39] 'open_wave_config' failed due to earlier errors."),
    ("add_wave /tb/clk", "ERROR: signal unavailable"),
    ("launch_simulation -scripts_only", "INFO: scripts generated"),
    ("run 100ns", "ERROR: [Wavedata 42-472] WCFG parsing ERROR"),
    ("open_project missing.xpr",
     "ERROR: [Coretcl 2-27] Can't find specified project"),
    ("launch_simulation", "ERROR: [Common 17-180] Spawn failed: Broken pipe"),
])
async def test_tcl_summary_returned_without_appended_advice(command, output, return_code):
    """错误文本与返回码独立传递，原先会触发建议的结果也原样返回。"""
    tcl_result = TclResult(output, return_code, is_error=return_code != 0)
    session = AsyncMock()
    session.execute.return_value = tcl_result

    result = await _safe_execute(session, command, 30.0, "命令执行失败")

    assert result == tcl_result.summary
    session.execute.assert_awaited_once_with(command, timeout=30.0)


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [asyncio.TimeoutError, TimeoutError])
async def test_timeout_preserves_cause_and_execution_uncertainty(error_type):
    session = AsyncMock()
    session.execute.side_effect = error_type("读取响应超时（120s）")

    result = await _safe_execute(session, "route_design", 120.0, "命令执行失败")

    assert result.startswith("[ERROR] 命令执行失败: 读取响应超时（120s）")
    assert "不代表命令已停止" in result
    session.execute.assert_awaited_once_with("route_design", timeout=120.0)


@pytest.mark.asyncio
async def test_transport_error_preserves_cause():
    session = AsyncMock()
    session.execute.side_effect = RuntimeError("Vivado 进程意外终止")

    result = await _safe_execute(session, "puts 1", 30.0, "命令执行失败")

    assert result == "[ERROR] 命令执行失败: Vivado 进程意外终止"


@pytest.mark.asyncio
async def test_cancellation_propagates():
    session = AsyncMock()
    session.execute.side_effect = asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _safe_execute(session, "route_design", 30.0, "命令执行失败")
