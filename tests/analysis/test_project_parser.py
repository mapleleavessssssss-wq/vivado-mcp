"""project_parser 单元测试。"""

from vivado_mcp.analysis.project_parser import (
    ProjectInfo,
    format_project_info,
    parse_project_info,
)

SAMPLE = """\
VMCP_PROJ:project_name=basys3_uart
VMCP_PROJ:project_dir=C:/Users/NJ/Desktop/test
VMCP_PROJ:part=xc7a35tcpg236-1
VMCP_PROJ:top=top
VMCP_PROJ:source_count=2
VMCP_PROJ_FILE:source|Verilog|C:/path/top.v
VMCP_PROJ_FILE:source|Verilog|C:/path/uart_tx.v
VMCP_PROJ:xdc_count=1
VMCP_PROJ_FILE:xdc|XDC|C:/path/basys3.xdc
VMCP_PROJ:sim_count=1
VMCP_PROJ_FILE:sim|Verilog|C:/path/tb_top.v
VMCP_PROJ:ip_count=0
VMCP_PROJ:synth_status=synth_design Complete!
VMCP_PROJ:impl_status=place_design ERROR
VMCP_PROJ_DONE
"""


def test_parses_project_meta():
    info = parse_project_info(SAMPLE)
    assert info.project_name == "basys3_uart"
    assert info.part == "xc7a35tcpg236-1"
    assert info.top == "top"
    assert info.synth_status == "synth_design Complete!"
    assert "ERROR" in info.impl_status


def test_parses_files():
    info = parse_project_info(SAMPLE)
    sources = [f for f in info.files if f.category == "source"]
    xdcs = [f for f in info.files if f.category == "xdc"]
    sims = [f for f in info.files if f.category == "sim"]
    assert len(sources) == 2
    assert len(xdcs) == 1
    assert len(sims) == 1
    assert sources[0].file_type == "Verilog"
    assert sims[0].path == "C:/path/tb_top.v"


def test_format_shows_key_fields():
    info = parse_project_info(SAMPLE)
    text = format_project_info(info)
    assert "basys3_uart" in text
    assert "xc7a35tcpg236-1" in text
    assert "top" in text
    assert "place_design ERROR" in text


def test_format_shows_sim_section():
    """审计 P3:sim 文件采集后展示层不能静默丢弃,要有 testbench 段。"""
    info = parse_project_info(SAMPLE)
    text = format_project_info(info)
    assert "仿真文件/testbench(1 个):" in text
    assert "[Verilog] C:/path/tb_top.v" in text


def test_format_sim_section_truncates_like_sources():
    """sim 段沿用 sources 段的截断逻辑:超过 20 个折叠为「还有 N 个」。"""
    raw = "\n".join(
        f"VMCP_PROJ_FILE:sim|Verilog|C:/path/tb_{i}.v" for i in range(25)
    )
    info = parse_project_info(raw)
    text = format_project_info(info)
    assert "仿真文件/testbench(25 个):" in text
    assert "... 还有 5 个文件" in text


def test_error_passthrough():
    err_raw = "VMCP_PROJ:error=no_project_open\n"
    info = parse_project_info(err_raw)
    assert info.error == "no_project_open"
    text = format_project_info(info)
    assert "no_project_open" in text


def test_empty_report_no_exception():
    info = parse_project_info("")
    assert isinstance(info, ProjectInfo)
    assert info.project_name == ""
    assert info.files == []
