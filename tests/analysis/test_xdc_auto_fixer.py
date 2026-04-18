"""xdc_auto_fixer 单元测试。

覆盖:MISSING_IOSTANDARD / CLOCK_NO_PERIOD / 不可修问题跳过 / 未知板 /
dry_run vs apply / 多文件。
"""

from pathlib import Path

from vivado_mcp.analysis.xdc_auto_fixer import (
    apply_fixes,
    format_fix_report,
    plan_fixes,
)


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


# -- MISSING_IOSTANDARD ------------------------------------------------------- #

def test_missing_iostd_generates_insert_patch(tmp_path):
    xdc = _write(tmp_path, "basys3.xdc", """\
# clock
set_property PACKAGE_PIN W5 [get_ports clk]
set_property IOSTANDARD LVCMOS33 [get_ports clk]

# led without IOSTANDARD
set_property PACKAGE_PIN U16 [get_ports {led[0]}]
""")
    rep = plan_fixes([xdc], board="basys3")
    # 应该只为 led[0] 生成一条补丁,clk 已经有 IOSTANDARD 不补
    assert len(rep.patches) == 1
    p = rep.patches[0]
    assert p.rule == "MISSING_IOSTANDARD"
    assert p.action == "insert_after"
    assert "LVCMOS33" in p.after_text
    assert "led[0]" in p.after_text


def test_apply_writes_new_line_after_trigger(tmp_path):
    xdc = _write(tmp_path, "missing.xdc", """\
set_property PACKAGE_PIN U16 [get_ports {led[0]}]
""")
    rep = plan_fixes([xdc], board="basys3")
    applied = apply_fixes(rep)
    assert applied.dry_run is False
    assert str(xdc) in applied.files_modified
    new_lines = xdc.read_text(encoding="utf-8").splitlines()
    assert len(new_lines) == 2
    assert "PACKAGE_PIN U16" in new_lines[0]
    assert "IOSTANDARD LVCMOS33" in new_lines[1]
    assert "led[0]" in new_lines[1]


def test_default_iostd_is_lvcmos33_without_board(tmp_path):
    xdc = _write(tmp_path, "x.xdc", "set_property PACKAGE_PIN U16 [get_ports foo]\n")
    rep = plan_fixes([xdc], board="")
    assert len(rep.patches) == 1
    assert "LVCMOS33" in rep.patches[0].after_text


def test_kc705_uses_lvcmos25(tmp_path):
    xdc = _write(tmp_path, "x.xdc", "set_property PACKAGE_PIN AE5 [get_ports foo]\n")
    rep = plan_fixes([xdc], board="kc705")
    assert "LVCMOS25" in rep.patches[0].after_text


# -- CLOCK_NO_PERIOD ---------------------------------------------------------- #

def test_clock_no_period_known_board(tmp_path):
    xdc = _write(tmp_path, "clk.xdc", "create_clock -name sys_clk [get_ports clk]\n")
    rep = plan_fixes([xdc], board="basys3")
    # lint 检出 CLOCK_NO_PERIOD,board=basys3 => period=10.0
    clock_patches = [p for p in rep.patches if p.rule == "CLOCK_NO_PERIOD"]
    assert len(clock_patches) == 1
    assert "-period 10.0" in clock_patches[0].after_text


def test_clock_no_period_unknown_board_skipped(tmp_path):
    xdc = _write(tmp_path, "clk.xdc", "create_clock -name sys_clk [get_ports clk]\n")
    rep = plan_fixes([xdc], board="")   # 未指定板卡
    clock_patches = [p for p in rep.patches if p.rule == "CLOCK_NO_PERIOD"]
    assert len(clock_patches) == 0  # 不猜 period
    clock_skipped = [i for i in rep.skipped if i.rule == "CLOCK_NO_PERIOD"]
    assert len(clock_skipped) == 1


def test_clock_apply_modifies_line(tmp_path):
    xdc = _write(tmp_path, "clk.xdc", "create_clock -name sys_clk [get_ports clk]\n")
    rep = plan_fixes([xdc], board="basys3")
    apply_fixes(rep)
    new = xdc.read_text(encoding="utf-8")
    assert "-period 10.0" in new
    assert "create_clock" in new


# -- 不可修问题:全部进 skipped,不进 patches ---------------------------------- #

def test_pin_conflict_not_auto_fixed(tmp_path):
    xdc = _write(tmp_path, "x.xdc", """\
set_property PACKAGE_PIN W5 [get_ports a]
set_property IOSTANDARD LVCMOS33 [get_ports a]
set_property PACKAGE_PIN W5 [get_ports b]
set_property IOSTANDARD LVCMOS33 [get_ports b]
""")
    rep = plan_fixes([xdc], board="basys3")
    assert all(p.rule != "PIN_CONFLICT" for p in rep.patches)
    assert any(i.rule == "PIN_CONFLICT" for i in rep.skipped)


def test_duplicate_port_not_auto_fixed(tmp_path):
    xdc = _write(tmp_path, "x.xdc", """\
set_property PACKAGE_PIN W5 [get_ports clk]
set_property IOSTANDARD LVCMOS33 [get_ports clk]
set_property PACKAGE_PIN W7 [get_ports clk]
""")
    rep = plan_fixes([xdc], board="basys3")
    assert all(p.rule != "DUPLICATE_PORT" for p in rep.patches)


# -- dry_run 语义 ------------------------------------------------------------- #

def test_plan_does_not_modify_file(tmp_path):
    xdc = _write(tmp_path, "x.xdc", "set_property PACKAGE_PIN U16 [get_ports foo]\n")
    original = xdc.read_text(encoding="utf-8")
    rep = plan_fixes([xdc], board="basys3")
    assert rep.dry_run is True
    assert xdc.read_text(encoding="utf-8") == original
    assert rep.files_modified == []


# -- 多问题混合 -------------------------------------------------------------- #

def test_multi_issues_mixed(tmp_path):
    xdc = _write(tmp_path, "mixed.xdc", """\
create_clock -name sys_clk [get_ports clk]
set_property PACKAGE_PIN W5 [get_ports clk]
set_property PACKAGE_PIN U16 [get_ports foo]
set_property PACKAGE_PIN V16 [get_ports bar]
set_property PACKAGE_PIN W5 [get_ports baz]
""")
    # 有:CLOCK_NO_PERIOD(1) + MISSING_IOSTANDARD(4) + PIN_CONFLICT(W5 给 clk+baz)
    rep = plan_fixes([xdc], board="basys3")
    iostd_patches = [p for p in rep.patches if p.rule == "MISSING_IOSTANDARD"]
    clock_patches = [p for p in rep.patches if p.rule == "CLOCK_NO_PERIOD"]
    pin_skipped = [i for i in rep.skipped if i.rule == "PIN_CONFLICT"]
    assert len(iostd_patches) == 4
    assert len(clock_patches) == 1
    assert len(pin_skipped) >= 1


def test_apply_multi_insert_preserves_line_order(tmp_path):
    """多个 insert_after 按 line 降序插,行号不偏移。"""
    xdc = _write(tmp_path, "x.xdc", """\
set_property PACKAGE_PIN A1 [get_ports a]
set_property PACKAGE_PIN A2 [get_ports b]
set_property PACKAGE_PIN A3 [get_ports c]
""")
    rep = plan_fixes([xdc], board="basys3")
    apply_fixes(rep)
    lines = xdc.read_text(encoding="utf-8").splitlines()
    # 3 原行 + 3 插入行 = 6 行,每个 PACKAGE_PIN 后紧跟 IOSTANDARD
    assert len(lines) == 6
    assert "PACKAGE_PIN A1" in lines[0]
    assert "IOSTANDARD" in lines[1] and "[get_ports {a}]" in lines[1]
    assert "PACKAGE_PIN A2" in lines[2]
    assert "IOSTANDARD" in lines[3] and "[get_ports {b}]" in lines[3]
    assert "PACKAGE_PIN A3" in lines[4]
    assert "IOSTANDARD" in lines[5] and "[get_ports {c}]" in lines[5]


# -- format ------------------------------------------------------------------- #

def test_format_dry_run_mentions_confirm(tmp_path):
    xdc = _write(tmp_path, "x.xdc", "set_property PACKAGE_PIN U16 [get_ports foo]\n")
    rep = plan_fixes([xdc], board="basys3")
    text = format_fix_report(rep)
    assert "DRY-RUN" in text
    assert "dry_run=False" in text


def test_format_applied_lists_files(tmp_path):
    xdc = _write(tmp_path, "x.xdc", "set_property PACKAGE_PIN U16 [get_ports foo]\n")
    rep = plan_fixes([xdc], board="basys3")
    applied = apply_fixes(rep)
    text = format_fix_report(applied)
    assert "APPLIED" in text
    assert "x.xdc" in text
