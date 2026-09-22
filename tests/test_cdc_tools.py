"""CDC 工具现场/离线接口与错误 JSON 契约。"""

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from vivado_mcp.vivado.tcl_utils import TclResult

FIXTURE = Path(__file__).parent / "fixtures/cdc_details_2019.txt"


@pytest.mark.asyncio
async def test_offline_needs_no_session_and_writes_nothing(tmp_path):
    from vivado_mcp.tools.cdc_tools import get_cdc_report

    path = tmp_path / "cdc report.txt"
    path.write_bytes(FIXTURE.read_bytes())
    before = set(tmp_path.iterdir())
    with patch("vivado_mcp.tools.cdc_tools._require_session") as require:
        data = json.loads(await get_cdc_report(report_file=str(path), max_details=1))
    require.assert_not_called()
    assert before == set(tmp_path.iterdir())
    assert data["summary"]["reported_checks"] == 3
    assert data["details_truncated"] is True
    assert data["provenance"]["source"] == "report_file"


@pytest.mark.asyncio
async def test_live_single_command_and_positional_context():
    from vivado_mcp.tools.cdc_tools import get_cdc_report

    session, ctx = AsyncMock(), MagicMock()
    session.execute.return_value = TclResult(FIXTURE.read_text(), 0, False)
    with patch("vivado_mcp.tools.cdc_tools._require_session", return_value=session) as require:
        data = json.loads(await get_cdc_report("cdc-live", ctx))
    require.assert_called_once_with(ctx, "cdc-live")
    session.execute.assert_awaited_once_with("report_cdc -details -return_string", timeout=120.0)
    assert data["parse_status"] == "ok"
    assert data["provenance"]["session_id"] == "cdc-live"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["rc", "exception", "missing"])
async def test_live_failures_are_json_with_logged_reason(error, caplog):
    from vivado_mcp.tools.cdc_tools import get_cdc_report

    session = AsyncMock()
    if error == "rc":
        session.execute.return_value = TclResult("no design", 1, True)
    elif error == "exception":
        session.execute.side_effect = RuntimeError("broken transport")
    with caplog.at_level(logging.WARNING, logger="vivado_mcp.tools.cdc_tools"):
        with patch("vivado_mcp.tools.cdc_tools._require_session",
                   return_value=None if error == "missing" else session):
            data = json.loads(await get_cdc_report(ctx=MagicMock()))
    assert data["parse_status"] == "error"
    assert data["success"] is False
    assert data["verdict"]["signoff"] is False
    assert caplog.records


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["absent.txt", "bad\x00path", "bad\ud800path"])
async def test_bad_paths_return_json(filename):
    from vivado_mcp.tools.cdc_tools import get_cdc_report

    data = json.loads(await get_cdc_report(report_file=filename))
    assert data["parse_status"] == "error"


@pytest.mark.asyncio
async def test_partial_no_clock_report_logs_degradation(caplog):
    from vivado_mcp.tools.cdc_tools import get_cdc_report

    with caplog.at_level(logging.WARNING, logger="vivado_mcp.tools.cdc_tools"):
        data = json.loads(await get_cdc_report(
            report_file=str(FIXTURE.parent / "cdc_no_clocks_2019.txt"),
        ))
    assert data["parse_status"] == "partial"
    assert data["verdict"]["status"] == "unverified"
    assert any("解析降级" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [True, -1, 501])
async def test_invalid_limits_fail_before_io(limit):
    from vivado_mcp.tools.cdc_tools import get_cdc_report

    with patch("vivado_mcp.tools.cdc_tools._require_session") as require:
        data = json.loads(await get_cdc_report(max_details=limit))
    require.assert_not_called()
    assert data["parse_status"] == "error"
