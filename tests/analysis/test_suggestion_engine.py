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
    if kw.get("with_sim"):
        info.files.append(ProjectFile("sim", "Verilog", "C:/test/tb.v"))
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


# ---------------------------------------------------------------------- #
#  规则 4.5: 仿真档(场景吸收)
# ---------------------------------------------------------------------- #


def test_ready_to_sim_when_tb_present_and_no_products(tmp_path):
    """有 testbench(sim fileset 非空)且无仿真产物 → 建议先行为仿真。"""
    info = _make_info(
        with_source=True, with_xdc=True, top="top", with_sim=True,
        project_dir=str(tmp_path),  # 空目录,确定无 .sim 产物
    )
    sug = suggest_next(info)
    assert sug.stage == "ready_to_sim"
    assert any("launch_simulation" in a for a in sug.actions)
    assert any("sim_1" in a for a in sug.actions)
    # 引导仿真通过后再综合
    assert any("run_synthesis" in a for a in sug.actions)


def test_sim_rung_skipped_when_products_exist(tmp_path):
    """已有 <proj>.sim/*/behav 产物 → 不再纠缠仿真,正常进综合档。"""
    sim_behav = tmp_path / "test_proj.sim" / "sim_1" / "behav"
    sim_behav.mkdir(parents=True)

    info = _make_info(
        with_source=True, with_xdc=True, top="top", with_sim=True,
        project_dir=str(tmp_path),
    )
    sug = suggest_next(info)
    assert sug.stage == "ready_to_synth"


def test_sim_rung_not_fired_after_synth_complete():
    """综合已完成时不再回头建议仿真(命中后续档位)。"""
    info = _make_info(
        with_source=True, with_xdc=True, top="top", with_sim=True,
        synth_status="synth_design Complete!",
    )
    sug = suggest_next(info)
    assert sug.stage == "ready_to_impl"


def test_no_testbench_note_in_synth_suggestion():
    """sim fileset 为空 → 综合建议里附"未发现 testbench"提醒。"""
    info = _make_info(with_source=True, with_xdc=True, top="top")
    sug = suggest_next(info)
    assert sug.stage == "ready_to_synth"
    assert any("未发现 testbench" in a for a in sug.actions)


def test_no_testbench_note_absent_when_tb_exists(tmp_path):
    """有 testbench(且已有产物,落进综合档)时不再附"未发现 testbench"。"""
    sim_behav = tmp_path / "test_proj.sim" / "sim_1" / "behav"
    sim_behav.mkdir(parents=True)

    info = _make_info(
        with_source=True, with_xdc=True, top="top", with_sim=True,
        project_dir=str(tmp_path),
    )
    sug = suggest_next(info)
    assert sug.stage == "ready_to_synth"
    assert not any("未发现 testbench" in a for a in sug.actions)


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
