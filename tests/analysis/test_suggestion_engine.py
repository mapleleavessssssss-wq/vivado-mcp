"""suggestion_engine 单元测试。"""

from vivado_mcp.analysis.project_parser import ProjectFile, ProjectInfo
from vivado_mcp.analysis.suggestion_engine import format_suggestion, suggest_next


def _make_info(**kw) -> ProjectInfo:
    info = ProjectInfo(
        project_name=kw.get("project_name", "test_proj"),
        project_dir=kw.get("project_dir", "C:/test"),
        part=kw.get("part", "xc7a35tcpg236-1"),
        top=kw.get("top", ""),
        synth_status=kw.get("synth_status", ""),
        impl_status=kw.get("impl_status", ""),
    )
    if kw.get("with_source"):
        info.files.append(ProjectFile("source", "Verilog", "C:/test/top.v"))
    if kw.get("with_xdc"):
        info.files.append(ProjectFile("xdc", "XDC", "C:/test/basys3.xdc"))
    return info


def test_no_project_suggests_open_or_create():
    info = ProjectInfo(error="no_project_open")
    sug = suggest_next(info)
    assert sug.stage == "no_project"
    assert any("open_project" in a for a in sug.actions)
    assert any("create_project" in a for a in sug.actions)


def test_empty_project_name_treated_as_no_project():
    info = ProjectInfo()
    sug = suggest_next(info)
    assert sug.stage == "no_project"


def test_no_source_files():
    info = _make_info()
    sug = suggest_next(info)
    assert sug.stage == "no_source"
    assert any("add_files" in a for a in sug.actions)


def test_no_top():
    info = _make_info(with_source=True)
    sug = suggest_next(info)
    assert sug.stage == "no_top"
    assert any("set_property TOP" in a for a in sug.actions)


def test_no_xdc():
    info = _make_info(with_source=True, top="top")
    sug = suggest_next(info)
    assert sug.stage == "no_xdc"
    assert any(".xdc" in a.lower() or "XDC" in a for a in sug.actions)


def test_ready_to_synth():
    info = _make_info(with_source=True, with_xdc=True, top="top")
    sug = suggest_next(info)
    assert sug.stage == "ready_to_synth"
    assert any("run_synthesis" in a for a in sug.actions)
    assert any("xdc_lint" in a for a in sug.actions)


def test_ready_to_synth_with_not_started():
    info = _make_info(
        with_source=True, with_xdc=True, top="top",
        synth_status="Not started",
    )
    sug = suggest_next(info)
    assert sug.stage == "ready_to_synth"


def test_synth_failed():
    info = _make_info(
        with_source=True, with_xdc=True, top="top",
        synth_status="synth_design ERROR",
    )
    sug = suggest_next(info)
    assert sug.stage == "synth_failed"
    assert any("get_critical_warnings" in a for a in sug.actions)


def test_ready_to_impl():
    info = _make_info(
        with_source=True, with_xdc=True, top="top",
        synth_status="synth_design Complete!",
        impl_status="",
    )
    sug = suggest_next(info)
    assert sug.stage == "ready_to_impl"
    assert any("run_implementation" in a for a in sug.actions)


def test_ready_to_impl_not_started():
    info = _make_info(
        with_source=True, with_xdc=True, top="top",
        synth_status="synth_design Complete!",
        impl_status="Not started",
    )
    sug = suggest_next(info)
    assert sug.stage == "ready_to_impl"


def test_impl_failed():
    info = _make_info(
        with_source=True, with_xdc=True, top="top",
        synth_status="synth_design Complete!",
        impl_status="place_design ERROR",
    )
    sug = suggest_next(info)
    assert sug.stage == "impl_failed"
    assert any("get_critical_warnings" in a for a in sug.actions)


def test_ready_to_bitstream():
    info = _make_info(
        with_source=True, with_xdc=True, top="top",
        synth_status="synth_design Complete!",
        impl_status="route_design Complete!",
    )
    sug = suggest_next(info)
    assert sug.stage == "ready_to_bitstream"
    assert any("generate_bitstream" in a for a in sug.actions)
    assert any("check_bitstream_readiness" in a for a in sug.actions)


def test_ready_to_program():
    info = _make_info(
        with_source=True, with_xdc=True, top="top",
        synth_status="synth_design Complete!",
        impl_status="write_bitstream Complete!",
    )
    sug = suggest_next(info)
    assert sug.stage == "ready_to_program"
    assert any("program_device" in a for a in sug.actions)
    # 路径应组合 project_dir/project_name.runs/impl_1/top.bit
    assert any(".bit" in a for a in sug.actions)


def test_impl_running():
    info = _make_info(
        with_source=True, with_xdc=True, top="top",
        synth_status="synth_design Complete!",
        impl_status="route_design Running",
    )
    sug = suggest_next(info)
    assert sug.stage == "impl_running"
    assert any("get_run_progress" in a for a in sug.actions)


def test_format_contains_stage_and_actions():
    info = _make_info(with_source=True, with_xdc=True, top="top")
    sug = suggest_next(info)
    text = format_suggestion(info, sug)
    assert "ready_to_synth" in text
    assert "建议动作" in text
    assert "run_synthesis" in text
