"""Tests for identity-checked synchronization into a live Vivado project."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from vivado_mcp.vivado.tcl_utils import TclResult


def _result(output: str = "ok") -> TclResult:
    return TclResult(output=output, return_code=0, is_error=False)


def _ctx():
    return MagicMock()


@pytest.mark.asyncio
async def test_dry_run_checks_identity_without_add_files(tmp_path):
    from vivado_mcp.tools.project_tools import sync_project_files

    xpr = tmp_path / "demo.xpr"
    rtl = tmp_path / "top.v"
    xpr.write_text("<Project/>", encoding="utf-8")
    rtl.write_text("module top; endmodule\n", encoding="utf-8")
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_result("VMCP_SYNC_RESULT|mode=DRY_RUN"))

    with patch("vivado_mcp.tools.project_tools._require_session", return_value=session):
        result = await sync_project_files(
            file_paths=[str(rtl)],
            expected_xpr_path=str(xpr),
            expected_project_name="demo",
            expected_part="xc7a35tfgg484-2",
            expected_top="top",
            expected_vivado_version="2024.2",
            ctx=_ctx(),
        )

    assert "DRY_RUN" in result
    tcl = session.execute.await_args.args[0]
    assert "XPR mismatch" in tcl
    assert "Part mismatch" in tcl
    assert "Top mismatch" in tcl
    assert "Vivado version mismatch" in tcl
    assert "add_files -fileset" in tcl
    assert "set __vmcp_apply 0" in tcl


@pytest.mark.asyncio
async def test_apply_enables_add_and_compile_order(tmp_path):
    from vivado_mcp.tools.project_tools import sync_project_files

    xpr = tmp_path / "demo.xpr"
    rtl = tmp_path / "new block [1].sv"
    xpr.write_text("<Project/>", encoding="utf-8")
    rtl.write_text("module new_block; endmodule\n", encoding="utf-8")
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_result("VMCP_SYNC_RESULT|mode=APPLY|added=1"))

    with patch("vivado_mcp.tools.project_tools._require_session", return_value=session):
        result = await sync_project_files(
            file_paths=[str(rtl)],
            expected_xpr_path=str(xpr),
            expected_project_name="demo",
            expected_part="xc7a35tfgg484-2",
            expected_top="top",
            expected_vivado_version="2024.2",
            apply=True,
            session_id="gui_2024_2",
            ctx=_ctx(),
        )

    assert "APPLY" in result
    tcl = session.execute.await_args.args[0]
    assert "set __vmcp_apply 1" in tcl
    assert "add_files -fileset $__vmcp_fileset -norecurse $__vmcp_norm" in tcl
    assert "update_compile_order -fileset $__vmcp_fileset" in tcl
    assert "\\[1\\]" in tcl


@pytest.mark.asyncio
async def test_rejects_missing_file_before_session_lookup(tmp_path):
    from vivado_mcp.tools.project_tools import sync_project_files

    result = await sync_project_files(
        file_paths=[str(tmp_path / "missing.v")],
        expected_xpr_path=str(tmp_path / "missing.xpr"),
        expected_project_name="demo",
        expected_part="xc7a35tfgg484-2",
        expected_top="top",
        expected_vivado_version="2024.2",
        ctx=_ctx(),
    )
    assert "待同步文件不存在" in result


@pytest.mark.asyncio
async def test_rejects_wrong_extension_for_constraints(tmp_path):
    from vivado_mcp.tools.project_tools import sync_project_files

    rtl = tmp_path / "top.v"
    rtl.write_text("module top; endmodule\n", encoding="utf-8")
    result = await sync_project_files(
        file_paths=[str(rtl)],
        expected_xpr_path=str(tmp_path / "demo.xpr"),
        expected_project_name="demo",
        expected_part="xc7a35tfgg484-2",
        expected_top="top",
        expected_vivado_version="2024.2",
        fileset="constrs_1",
        ctx=_ctx(),
    )
    assert "不能加入 constrs_1" in result


@pytest.mark.asyncio
async def test_setup_debug_dry_run_is_plan_only_without_design_change(tmp_path):
    from vivado_mcp.tools.project_tools import setup_debug_after_synth

    xpr = tmp_path / "demo.xpr"
    xdc = tmp_path / "debug.xdc"
    xpr.write_text("<Project/>", encoding="utf-8")
    xdc.write_text("# debug\n", encoding="utf-8")
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_result("VMCP_DEBUG_RESULT|mode=PLAN_ONLY"))

    with patch("vivado_mcp.tools.project_tools._require_session", return_value=session):
        result = await setup_debug_after_synth(
            probe_net_patterns=["w_data[*]", "w_de"],
            ila_clock_net="u_rx/o_pixel_clk",
            hub_clock_net="w_clk_200m",
            target_xdc_path=str(xdc),
            expected_xpr_path=str(xpr),
            expected_project_name="demo",
            expected_part="xc7a35tfgg484-2",
            expected_top="top",
            expected_vivado_version="2024.2",
            ctx=_ctx(),
        )

    assert "PLAN_ONLY" in result
    tcl = session.execute.await_args.args[0]
    assert "if {!$__vmcp_apply}" in tcl
    assert "netlist_inspected=0" in tcl
    assert tcl.index("return") < tcl.index("open_run $__vmcp_synth_run")
    assert "open_run $__vmcp_synth_run" in tcl
    assert "MARK_DEBUG" in tcl
    assert "set __vmcp_apply 0" in tcl
    assert "save_constraints" in tcl
    assert "w_data\\[*\\]" in tcl


@pytest.mark.asyncio
async def test_setup_debug_apply_rebuilds_core_and_saves_target_xdc(tmp_path):
    from vivado_mcp.tools.project_tools import setup_debug_after_synth

    xpr = tmp_path / "demo.xpr"
    xdc = tmp_path / "debug.xdc"
    xpr.write_text("<Project/>", encoding="utf-8")
    xdc.write_text("# debug\n", encoding="utf-8")
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_result("VMCP_DEBUG_RESULT|mode=APPLY"))

    with patch("vivado_mcp.tools.project_tools._require_session", return_value=session):
        result = await setup_debug_after_synth(
            probe_net_patterns=["w_status[*]"],
            ila_clock_net="u_rx/o_pixel_clk",
            hub_clock_net="w_clk_200m",
            target_xdc_path=str(xdc),
            expected_xpr_path=str(xpr),
            expected_project_name="demo",
            expected_part="xc7a35tfgg484-2",
            expected_top="top",
            expected_vivado_version="2024.2",
            data_depth=4096,
            hub_clock_frequency_hz=200_000_000,
            apply=True,
            ctx=_ctx(),
        )

    assert "APPLY" in result
    tcl = session.execute.await_args.args[0]
    assert "set __vmcp_apply 1" in tcl
    assert "delete_debug_core" in tcl
    assert "create_debug_core $__vmcp_ila_name ila" in tcl
    assert "set_property C_DATA_DEPTH 4096" in tcl
    assert "set_property C_CLK_INPUT_FREQ_HZ 200000000" in tcl
    assert "set_property TARGET_CONSTRS_FILE" in tcl
    assert "save_constraints" in tcl
