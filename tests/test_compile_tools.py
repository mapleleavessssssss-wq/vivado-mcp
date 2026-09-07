"""Compile profile and incremental configuration tests."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from vivado_mcp.analysis.compile_profile import parse_compile_profile
from vivado_mcp.tools.compile_tools import (
    build_compile_profile_query,
    configure_incremental_compile,
    get_compile_profile,
)
from vivado_mcp.vivado.tcl_utils import TclResult


def _result(output: str, rc: int = 0) -> TclResult:
    return TclResult(output=output, return_code=rc, is_error=rc != 0)


def test_compile_profile_parser_preserves_unknown_properties():
    raw = """\
VMCP_PROFILE:project_name=demo
VMCP_PROFILE:project_dir=C:/work/demo
VMCP_PROFILE:xpr_path=C:/work/demo/demo.xpr
VMCP_PROFILE:part=xc7a35tcpg236-1
VMCP_PROFILE:top=top
VMCP_PROFILE:vivado_version=2024.2
VMCP_PROFILE:tcl_patchlevel=8.6.13
VMCP_PROFILE:general_max_threads=2
VMCP_PROFILE_RUN:synth_1|found=1
VMCP_PROFILE_RUN:synth_1|STATUS=synth_design Complete!
VMCP_PROFILE_RUN:synth_1|NEEDS_REFRESH=0
VMCP_PROFILE_RUN:impl_1|found=1
VMCP_PROFILE_RUN:impl_1|STATUS=route_design Complete!
VMCP_PROFILE_RUN:impl_1|STATS.WNS=0.125
VMCP_PROFILE_RUN:impl_1|UNKNOWN_FUTURE_PROP=value
"""
    profile = parse_compile_profile(raw)
    assert profile.max_threads == 2
    assert profile.runs["synth_1"].is_complete
    assert not profile.runs["synth_1"].is_out_of_date
    assert profile.runs["impl_1"].wns == pytest.approx(0.125)
    assert profile.runs["impl_1"].properties["UNKNOWN_FUTURE_PROP"] == "value"


def test_compile_profile_query_is_read_only_and_tcl85_compatible():
    query = build_compile_profile_query(["synth_1", "impl_1"])
    assert "get_param general.maxThreads" in query
    assert "AUTO_INCREMENTAL_CHECKPOINT" in query
    assert "set_property" not in query
    assert "launch_runs" not in query
    assert "dict " not in query


@pytest.mark.asyncio
async def test_get_compile_profile_returns_jobs_semantics(tmp_path):
    run_dir = tmp_path / "demo.runs" / "impl_1"
    run_dir.mkdir(parents=True)
    report = run_dir / "top_timing_summary_routed.rpt"
    report.write_text("Design Timing Summary", encoding="utf-8")
    output = (
        "VMCP_PROFILE:project_name=demo\n"
        f"VMCP_PROFILE:project_dir={tmp_path.as_posix()}\n"
        f"VMCP_PROFILE:xpr_path={(tmp_path / 'demo.xpr').as_posix()}\n"
        "VMCP_PROFILE:part=xc7a35tcpg236-1\n"
        "VMCP_PROFILE:top=top\n"
        "VMCP_PROFILE:vivado_version=2024.2\n"
        "VMCP_PROFILE:tcl_patchlevel=8.6.13\n"
        "VMCP_PROFILE:general_max_threads=2\n"
        "VMCP_PROFILE_RUN:synth_1|found=0\n"
        "VMCP_PROFILE_RUN:impl_1|found=1\n"
        "VMCP_PROFILE_RUN:impl_1|STATUS=route_design Complete!\n"
        "VMCP_PROFILE_RUN:impl_1|NEEDS_REFRESH=0\n"
        f"VMCP_PROFILE_RUN:impl_1|DIRECTORY={run_dir.as_posix()}\n"
    )
    session = SimpleNamespace(execute=AsyncMock(return_value=_result(output)))
    with patch("vivado_mcp.tools.compile_tools._require_session", return_value=session):
        raw = await get_compile_profile(ctx=SimpleNamespace())
    payload = json.loads(raw)
    assert payload["jobs_semantics"] == "parallel run slots; not threads inside one run"
    assert payload["general_max_threads"] == 2
    assert payload["artifacts"]["impl_1"]["reports"] == [str(report.resolve())]


@pytest.mark.asyncio
async def test_incremental_plan_only_guards_set_property():
    session = SimpleNamespace(execute=AsyncMock(return_value=_result("PLAN")))
    with patch("vivado_mcp.tools.compile_tools._require_session", return_value=session):
        result = await configure_incremental_compile(
            apply=False,
            ctx=SimpleNamespace(),
        )
    assert result == "PLAN"
    sent = session.execute.await_args.args[0]
    assert "set_property AUTO_INCREMENTAL_CHECKPOINT" in sent
    assert "if {$__vmcp_apply}" in sent
    assert "set __vmcp_apply 0" in sent


@pytest.mark.asyncio
async def test_incremental_apply_requires_existing_xpr(tmp_path):
    result = await configure_incremental_compile(
        apply=True,
        expected_xpr_path=str(tmp_path / "missing.xpr"),
        expected_project_name="demo",
        expected_part="xc7a35tcpg236-1",
        expected_top="top",
        expected_vivado_version="2024.2",
        ctx=SimpleNamespace(),
    )
    assert result.startswith("[ERROR]")
    assert "不是现存 .xpr" in result
