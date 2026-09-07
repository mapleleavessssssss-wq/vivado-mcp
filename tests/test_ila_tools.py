"""Offline tests for live ILA trigger and capture wrappers."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from vivado_mcp.vivado.tcl_utils import TclResult


def _ctx():
    return MagicMock()


def _capture_result() -> TclResult:
    return TclResult(
        output=(
            "VMCP_ILA_CAPTURE|target=target0|device=xc7a35t_0|ila=hw_ila_1\n"
            "VMCP_ILA_CSV:C:/capture.csv"
        ),
        return_code=0,
        is_error=False,
    )


def _trigger_result(*, apply: bool) -> TclResult:
    marker = (
        "VMCP_ILA_TRIGGER_CONFIGURED|ila=hw_ila_1|depth=16384|"
        "windows=1|position=8192|condition=AND"
        if apply
        else "VMCP_ILA_TRIGGER_PLAN_ONLY|ila=hw_ila_1|writes=0|armed=0"
    )
    return TclResult(
        output=(
            "VMCP_ILA_TRIGGER_PREFLIGHT|target=target0|device=xc7a35t_0|"
            "ila=hw_ila_1|mode=BASIC_ONLY|"
            "max_depth=16384|probes=13\n"
            "VMCP_ILA_TRIGGER_SPEC|probe=aggregate_format_valid|"
            "semantic=falling|width=1|compare=eq1'bF\n"
            f"{marker}"
        ),
        return_code=0,
        is_error=False,
    )


def _identity_files(tmp_path):
    bit = tmp_path / "production.bit"
    ltx = tmp_path / "production.ltx"
    bit.write_bytes(b"bit fixture")
    ltx.write_text("probe fixture", encoding="utf-8")
    return bit, ltx


@pytest.mark.asyncio
async def test_basic_trigger_plan_is_preflight_first_and_never_writes_mode(tmp_path):
    from vivado_mcp.tools.ila_tools import configure_hw_ila_basic_trigger

    bit, ltx = _identity_files(tmp_path)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_trigger_result(apply=False))
    with patch("vivado_mcp.tools.ila_tools._require_session", return_value=session):
        result = await configure_hw_ila_basic_trigger(
            expected_program_file_path=str(bit),
            expected_probes_file_path=str(ltx),
            probe_triggers={"aggregate_format_valid": "falling"},
            data_depth=16384,
            trigger_position=8192,
            ila_name="hw_ila_1",
            device_name="xc7a35t_0",
            target_name="target0",
            ctx=_ctx(),
        )

    assert result.startswith("[PLAN_ONLY]")
    tcl = session.execute.await_args.args[0]
    assert "get_property CONTROL.TRIGGER_MODE $__vmcp_ila" in tcl
    assert "set_property CONTROL.TRIGGER_MODE" not in tcl
    assert "__vmcp_property_read_only $__vmcp_ila CONTROL.TRIGGER_MODE" not in tcl
    assert "report_property -all -return_string" in tcl
    assert "PROBE_PORT_BIT_COUNT" in tcl
    assert "COMPARATOR_COUNT" in tcl
    assert "get_property STATUS.CORE_STATUS $__vmcp_ila" in tcl
    assert "get_property STATIC.MAX_DATA_DEPTH $__vmcp_ila" in tcl
    assert "get_hw_devices -quiet -of_objects $__vmcp_target" in tcl
    assert "get_hw_ilas -quiet -of_objects $__vmcp_device" in tcl
    assert "PROGRAM.FILE identity mismatch" in tcl
    assert "PROBES.FILE identity mismatch" in tcl
    assert "get_property SID $server" in tcl
    assert "__vmcp_select_hw_server" in tcl
    assert "get_hw_targets -quiet -of_objects $__vmcp_server" in tcl
    assert '[list "aggregate_format_valid" "falling"]' in tcl
    assert "set __vmcp_clear_unlisted 1" in tcl
    assert "VMCP_ILA_TRIGGER_SKIPPED|probe=$probe|reason=no_comparator" in tcl
    assert "set __vmcp_apply 0" in tcl
    assert "run_hw_ila" not in tcl
    assert "program_hw_devices" not in tcl
    assert "refresh_hw_device" not in tcl
    assert tcl.index("VMCP_ILA_TRIGGER_PREFLIGHT") < tcl.index(
        "if {$__vmcp_apply}"
    )
    session.execute.assert_awaited_once_with(tcl, timeout=60.0)


@pytest.mark.asyncio
async def test_basic_trigger_apply_configures_without_arming(tmp_path):
    from vivado_mcp.tools.ila_tools import configure_hw_ila_basic_trigger

    bit, ltx = _identity_files(tmp_path)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_trigger_result(apply=True))
    with patch("vivado_mcp.tools.ila_tools._require_session", return_value=session):
        result = await configure_hw_ila_basic_trigger(
            expected_program_file_path=str(bit),
            expected_probes_file_path=str(ltx),
            probe_triggers={"aggregate_format_valid": "FALLING"},
            data_depth=16384,
            trigger_position=8192,
            apply=True,
            ctx=_ctx(),
        )

    assert result.startswith("[CONFIGURED]")
    tcl = session.execute.await_args.args[0]
    assert "set __vmcp_apply 1" in tcl
    assert "set_property -dict [list" in tcl
    assert "set_property TRIGGER_COMPARE_VALUE $compare_value $probe" in tcl
    assert "VMCP_ILA_TRIGGER_CONFIGURED" in tcl
    assert "run_hw_ila" not in tcl


@pytest.mark.asyncio
async def test_basic_trigger_rejects_bad_request_before_session_lookup(tmp_path):
    from vivado_mcp.tools.ila_tools import configure_hw_ila_basic_trigger

    bit, ltx = _identity_files(tmp_path)
    with patch("vivado_mcp.tools.ila_tools._require_session") as require_session:
        result = await configure_hw_ila_basic_trigger(
            expected_program_file_path=str(bit),
            expected_probes_file_path=str(ltx),
            probe_triggers={"aggregate_format_valid": "negative_edge"},
            data_depth=16384,
            trigger_position=8192,
            ctx=_ctx(),
        )
        assert "不支持" in result

        result = await configure_hw_ila_basic_trigger(
            expected_program_file_path=str(bit),
            expected_probes_file_path=str(ltx),
            probe_triggers={"aggregate_format_valid": "dont_care"},
            data_depth=16384,
            trigger_position=8192,
            ctx=_ctx(),
        )
        assert "非 dont_care" in result

        result = await configure_hw_ila_basic_trigger(
            expected_program_file_path=str(bit),
            expected_probes_file_path=str(ltx),
            probe_triggers={"aggregate_format_valid": "falling"},
            data_depth=12000,
            trigger_position=8192,
            ctx=_ctx(),
        )
        assert "二次幂" in result

        result = await configure_hw_ila_basic_trigger(
            expected_program_file_path=str(bit),
            expected_probes_file_path=str(ltx),
            probe_triggers={"aggregate_format_valid": "falling"},
            data_depth=16384,
            trigger_position=8192,
            trigger_condition="XOR",
            ctx=_ctx(),
        )
        assert "AND、OR、NAND 或 NOR" in result

    require_session.assert_not_called()


@pytest.mark.asyncio
async def test_basic_trigger_requires_identity_files_and_remote_opt_in(tmp_path):
    from vivado_mcp.tools.ila_tools import configure_hw_ila_basic_trigger

    bit, ltx = _identity_files(tmp_path)
    with patch("vivado_mcp.tools.ila_tools._require_session") as require_session:
        result = await configure_hw_ila_basic_trigger(
            expected_program_file_path="relative.bit",
            expected_probes_file_path=str(ltx),
            probe_triggers={"aggregate_format_valid": "falling"},
            data_depth=16384,
            trigger_position=8192,
            ctx=_ctx(),
        )
        assert "必须是绝对路径" in result

        result = await configure_hw_ila_basic_trigger(
            expected_program_file_path=str(bit),
            expected_probes_file_path=str(ltx),
            probe_triggers={"aggregate_format_valid": "falling"},
            data_depth=16384,
            trigger_position=8192,
            hw_server_url="192.0.2.10:3121",
            ctx=_ctx(),
        )
        assert result.startswith("[BLOCKED]")

    require_session.assert_not_called()


@pytest.mark.asyncio
async def test_rejects_relative_output_root_before_session_lookup():
    from vivado_mcp.tools.ila_tools import capture_hw_ila_to_csv

    with patch("vivado_mcp.tools.ila_tools._require_session") as require_session:
        result = await capture_hw_ila_to_csv(output_root="relative/capture", ctx=_ctx())

    assert "必须是绝对路径" in result
    require_session.assert_not_called()


@pytest.mark.asyncio
async def test_rejects_source_repository_as_output_root():
    import vivado_mcp.tools.ila_tools as ila_tools

    repository_root = ila_tools.Path(ila_tools.__file__).resolve().parents[3]
    result = await ila_tools.capture_hw_ila_to_csv(
        output_root=str(repository_root / "captures"),
        ctx=_ctx(),
    )

    assert "不能写入 Vivado MCP 源码仓库" in result


@pytest.mark.asyncio
async def test_immediate_capture_builds_safe_one_shot_flow(tmp_path):
    from vivado_mcp.tools.ila_tools import capture_hw_ila_to_csv

    ltx = tmp_path / "debug probes [0].ltx"
    ltx.write_text("<probeData/>", encoding="utf-8")
    output_root = tmp_path / "ila_capture"
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_capture_result())

    with patch("vivado_mcp.tools.ila_tools._require_session", return_value=session):
        result = await capture_hw_ila_to_csv(
            output_root=str(output_root),
            probes_file_path=str(ltx),
            ila_name="hw_ila_$rx[0]",
            capture_label="HDMI RX / 1080p60",
            ctx=_ctx(),
        )

    assert result.startswith("[OK] ILA 采集完成")
    capture_dirs = list(output_root.glob("ila_capture_*_HDMI_RX_1080p60_*"))
    assert len(capture_dirs) == 1
    tcl = session.execute.await_args.args[0]
    assert "connect_hw_server -url $url" in tcl
    assert "__vmcp_select_hw_server" in tcl
    assert "get_hw_targets -quiet -of_objects $__vmcp_server" in tcl
    assert "Expected exactly one $kind" in tcl
    assert "refresh_hw_device $__vmcp_device" in tcl
    assert "if {[llength $__vmcp_ilas] == 0}" in tcl
    assert "get_hw_devices -quiet -of_objects $__vmcp_target" in tcl
    assert "get_hw_ilas -quiet -of_objects $__vmcp_device" in tcl
    assert "set_property PROBES.FILE" in tcl
    assert "run_hw_ila -trigger_now $__vmcp_ila" in tcl
    assert "wait_on_hw_ila -timeout 0.500000 $__vmcp_ila" in tcl
    assert "upload_hw_ila_data $__vmcp_ila" in tcl
    assert "write_hw_ila_data -force -csv_file" in tcl
    assert "program_hw_devices" not in tcl
    assert 'set __vmcp_ila_name "hw_ila_\\$rx\\[0\\]"' in tcl
    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_configured_trigger_omits_trigger_now(tmp_path):
    from vivado_mcp.tools.ila_tools import capture_hw_ila_to_csv

    session = AsyncMock()
    session.execute = AsyncMock(return_value=_capture_result())
    with patch("vivado_mcp.tools.ila_tools._require_session", return_value=session):
        result = await capture_hw_ila_to_csv(
            output_root=str(tmp_path / "captures"),
            trigger_now=False,
            timeout_sec=60,
            ctx=_ctx(),
        )

    assert result.startswith("[OK]")
    tcl = session.execute.await_args.args[0]
    assert "run_hw_ila $__vmcp_ila" in tcl
    assert "run_hw_ila -trigger_now" not in tcl
    assert "wait_on_hw_ila -timeout 1.000000" in tcl


@pytest.mark.asyncio
async def test_rejects_invalid_ltx_and_timeout_before_writing(tmp_path):
    from vivado_mcp.tools.ila_tools import capture_hw_ila_to_csv

    output_root = tmp_path / "captures"
    missing_ltx = tmp_path / "missing.ltx"
    result = await capture_hw_ila_to_csv(
        output_root=str(output_root),
        probes_file_path=str(missing_ltx),
        ctx=_ctx(),
    )
    assert "不是现存 .ltx" in result
    assert not output_root.exists()

    result = await capture_hw_ila_to_csv(
        output_root=str(output_root),
        timeout_sec=0,
        ctx=_ctx(),
    )
    assert "1..3600" in result
    assert not output_root.exists()


@pytest.mark.asyncio
async def test_remote_hw_server_requires_explicit_opt_in(tmp_path):
    from vivado_mcp.tools.ila_tools import capture_hw_ila_to_csv

    output_root = tmp_path / "captures"
    with patch("vivado_mcp.tools.ila_tools._require_session") as require_session:
        result = await capture_hw_ila_to_csv(
            output_root=str(output_root),
            hw_server_url="192.0.2.10:3121",
            ctx=_ctx(),
        )

    assert result.startswith("[BLOCKED]")
    assert "allow_remote_hw_server=True" in result
    assert not output_root.exists()
    require_session.assert_not_called()


@pytest.mark.asyncio
async def test_ipv6_loopback_hw_server_is_allowed(tmp_path):
    from vivado_mcp.tools.ila_tools import capture_hw_ila_to_csv

    session = AsyncMock()
    session.execute = AsyncMock(return_value=_capture_result())
    with patch("vivado_mcp.tools.ila_tools._require_session", return_value=session):
        result = await capture_hw_ila_to_csv(
            output_root=str(tmp_path / "captures"),
            hw_server_url="[::1]:3121",
            ctx=_ctx(),
        )

    assert result.startswith("[OK]")


@pytest.mark.asyncio
async def test_hardware_error_reports_disposable_capture_dir(tmp_path):
    from vivado_mcp.tools.ila_tools import capture_hw_ila_to_csv

    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=TclResult(output="No hardware target", return_code=1, is_error=True)
    )
    output_root = tmp_path / "captures"
    with patch("vivado_mcp.tools.ila_tools._require_session", return_value=session):
        result = await capture_hw_ila_to_csv(output_root=str(output_root), ctx=_ctx())

    assert "No hardware target" in result
    assert "采集目录:" in result
    assert len(list(output_root.iterdir())) == 1
