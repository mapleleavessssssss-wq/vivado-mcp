"""Read-only Vivado Tcl capability discovery tests."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vivado_mcp.server import AppContext
from vivado_mcp.tools.session_tools import get_vivado_capabilities
from vivado_mcp.vivado.capabilities import (
    build_capability_probe,
    normalize_capability_commands,
    parse_capability_probe,
)
from vivado_mcp.vivado.tcl_utils import TclResult


def test_probe_is_exact_name_read_only_tcl_85_compatible():
    probe = build_capability_probe(["report_timing_summary", "write_hw_platform"])
    assert "info commands $__vmcp_cmd" in probe
    assert "binary decode" not in probe
    assert "report_timing_summary -return_string" not in probe
    assert "write_hw_platform " not in probe


def test_command_validation_rejects_tcl_script_syntax():
    with pytest.raises(ValueError, match="非法 Tcl command name"):
        normalize_capability_commands(["report_timing_summary; exit"])


def test_probe_parser_preserves_unknown_rows():
    parsed = parse_capability_probe(
        "report_timing_summary=1\nwrite_hw_platform=0\nnoise",
        ["report_timing_summary", "write_hw_platform", "report_qor_assessment"],
    )
    assert parsed == {
        "report_timing_summary": True,
        "write_hw_platform": False,
        "report_qor_assessment": None,
    }


@pytest.mark.asyncio
async def test_capability_tool_blocks_on_missing_command_without_invoking_it():
    class FakeSession:
        def __init__(self):
            self.execute = AsyncMock(
                return_value=TclResult(
                    output="report_timing_summary=1\nwrite_hw_platform=0",
                    return_code=0,
                    is_error=False,
                )
            )

        @staticmethod
        def status_dict():
            return {
                "vivado_version": "2018.3",
                "vivado_path": "C:/Xilinx/Vivado/2018.3/bin/vivado.bat",
            }

    session = FakeSession()
    manager = SimpleNamespace(get=lambda session_id: session)
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context=AppContext(session_manager=manager)
        )
    )

    raw = await get_vivado_capabilities(
        commands=["report_timing_summary", "write_hw_platform"],
        session_id="legacy",
        ctx=ctx,
    )
    result = json.loads(raw)

    assert result["gate"] == "FAIL"
    assert result["unavailable"] == ["write_hw_platform"]
    session.execute.assert_awaited_once()
    probe = session.execute.await_args.args[0]
    assert "info commands" in probe
    assert "write_hw_platform " not in probe
