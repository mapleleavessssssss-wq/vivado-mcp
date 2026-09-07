"""Server-level safety metadata tests for every exposed MCP tool."""

import pytest


@pytest.mark.asyncio
async def test_every_exposed_tool_has_explicit_annotations():
    from vivado_mcp.server import mcp

    tools = await mcp.list_tools()
    assert len(tools) >= 39
    assert all(tool.annotations is not None for tool in tools)


@pytest.mark.asyncio
async def test_high_risk_and_read_only_tools_are_classified_conservatively():
    from vivado_mcp.server import mcp

    tools = {tool.name: tool for tool in await mcp.list_tools()}

    for name in {
        "run_tcl",
        "safe_tcl",
        "reset_project_run",
        "generate_bitstream",
        "program_device",
        "configure_hw_ila_basic_trigger",
        "capture_hw_ila_to_csv",
        "sync_project_files",
        "setup_debug_after_synth",
        "configure_incremental_compile",
    }:
        annotations = tools[name].annotations
        assert annotations.read_only_hint is False
        assert annotations.destructive_hint is True

    for name in {
        "parse_xpr",
        "parse_bit_header",
        "parse_ltx",
        "compare_xci",
        "get_project_info",
        "get_run_progress",
        "get_timing_report",
        "list_vivado_installations",
        "get_vivado_capabilities",
        "get_compile_profile",
        "list_sessions",
    }:
        annotations = tools[name].annotations
        assert annotations.read_only_hint is True
        assert annotations.destructive_hint is False

    assert tools["stop_session"].annotations.destructive_hint is True
    assert tools["run_tcl"].annotations.open_world_hint is True
    assert tools["program_device"].annotations.open_world_hint is True


@pytest.mark.asyncio
async def test_compile_flow_defaults_are_fast_and_explicitly_deepenable():
    """锁定默认不等待/不深扫；深度行为仍通过显式参数可见。"""
    from vivado_mcp.server import mcp

    tools = {tool.name: tool for tool in await mcp.list_tools()}

    synth = tools["run_synthesis"].input_schema["properties"]
    impl = tools["run_implementation"].input_schema["properties"]
    bitstream = tools["generate_bitstream"].input_schema["properties"]
    timing = tools["get_timing_report"].input_schema["properties"]

    assert synth["wait_for_completion"]["default"] is False
    assert synth["post_check"]["default"] == "none"
    assert synth["max_threads"]["default"] == 0
    assert impl["wait_for_completion"]["default"] is False
    assert impl["post_check"]["default"] == "none"
    assert impl["max_threads"]["default"] == 0
    assert bitstream["wait_for_completion"]["default"] is False
    assert timing["include_violating_paths"]["default"] is False


@pytest.mark.asyncio
async def test_ila_basic_trigger_schema_is_plan_only_and_identity_gated():
    from vivado_mcp.server import mcp

    tools = {tool.name: tool for tool in await mcp.list_tools()}
    schema = tools["configure_hw_ila_basic_trigger"].input_schema
    properties = schema["properties"]

    assert properties["apply"]["default"] is False
    assert properties["clear_unlisted_probes"]["default"] is True
    assert properties["expected_trigger_mode"]["default"] == "BASIC_ONLY"
    assert properties["probe_triggers"]["type"] == "object"
    assert {
        "expected_program_file_path",
        "expected_probes_file_path",
        "probe_triggers",
        "data_depth",
        "trigger_position",
    }.issubset(schema["required"])


def test_initialize_identity_uses_package_version():
    import vivado_mcp
    from vivado_mcp.server import mcp

    assert mcp.version == vivado_mcp.__version__
