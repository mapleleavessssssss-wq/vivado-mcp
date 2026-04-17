"""xdc_linter 单元测试。"""

from pathlib import Path

import pytest

from vivado_mcp.analysis.xdc_linter import (
    format_lint_report,
    lint_xdc_file,
    lint_xdc_files,
)


@pytest.fixture
def tmp_xdc(tmp_path: Path):
    """工厂:把内容写入临时 XDC 文件。"""
    def _make(name: str, content: str) -> Path:
        p = tmp_path / name
        p.write_text(content, encoding="utf-8")
        return p
    return _make


class TestMissingIostandard:
    def test_detects_missing_iostandard(self, tmp_xdc):
        p = tmp_xdc("bad.xdc", "set_property PACKAGE_PIN V14 [get_ports {led[7]}]\n")
        issues = lint_xdc_file(p)
        rules = {i.rule for i in issues}
        assert "MISSING_IOSTANDARD" in rules

    def test_iostandard_via_dict_is_ok(self, tmp_xdc):
        p = tmp_xdc("good.xdc",
            "set_property -dict { PACKAGE_PIN W5 IOSTANDARD LVCMOS33 } [get_ports clk]\n"
        )
        issues = lint_xdc_file(p)
        assert all(i.rule != "MISSING_IOSTANDARD" for i in issues)

    def test_iostandard_via_separate_statement_is_ok(self, tmp_xdc):
        p = tmp_xdc("sep.xdc", (
            "set_property PACKAGE_PIN W5 [get_ports clk]\n"
            "set_property IOSTANDARD LVCMOS33 [get_ports clk]\n"
        ))
        issues = lint_xdc_file(p)
        assert all(i.rule != "MISSING_IOSTANDARD" for i in issues)


class TestPinConflict:
    def test_same_pin_two_ports_error(self, tmp_xdc):
        p = tmp_xdc("conflict.xdc", (
            "set_property -dict { PACKAGE_PIN W5 IOSTANDARD LVCMOS33 } [get_ports clk]\n"
            "set_property -dict { PACKAGE_PIN W5 IOSTANDARD LVCMOS33 } [get_ports other]\n"
        ))
        issues = lint_xdc_file(p)
        errors = [i for i in issues if i.severity == "error" and i.rule == "PIN_CONFLICT"]
        assert len(errors) == 1
        assert "W5" in errors[0].message.upper()

    def test_same_pin_same_port_no_error(self, tmp_xdc):
        p = tmp_xdc("dup.xdc", (
            "set_property PACKAGE_PIN W5 [get_ports clk]\n"
            "set_property IOSTANDARD LVCMOS33 [get_ports clk]\n"
        ))
        issues = lint_xdc_file(p)
        assert all(i.rule != "PIN_CONFLICT" for i in issues)


class TestDuplicatePort:
    def test_port_assigned_two_pins_warning(self, tmp_xdc):
        p = tmp_xdc("duppin.xdc", (
            "set_property -dict { PACKAGE_PIN W5 IOSTANDARD LVCMOS33 } [get_ports clk]\n"
            "set_property -dict { PACKAGE_PIN W6 IOSTANDARD LVCMOS33 } [get_ports clk]\n"
        ))
        issues = lint_xdc_file(p)
        dup = [i for i in issues if i.rule == "DUPLICATE_PORT"]
        assert len(dup) == 1


class TestCreateClock:
    def test_create_clock_no_period_error(self, tmp_xdc):
        p = tmp_xdc("clk.xdc", "create_clock -name sys_clk [get_ports clk]\n")
        issues = lint_xdc_file(p)
        errors = [i for i in issues if i.rule == "CLOCK_NO_PERIOD"]
        assert len(errors) == 1

    def test_create_clock_with_period_ok(self, tmp_xdc):
        p = tmp_xdc("clk.xdc", (
            "create_clock -name sys_clk -period 10.000 [get_ports clk]\n"
        ))
        issues = lint_xdc_file(p)
        assert all(i.rule != "CLOCK_NO_PERIOD" for i in issues)


class TestCrossFileConflict:
    def test_cross_file_pin_conflict(self, tmp_xdc):
        f1 = tmp_xdc("a.xdc",
            "set_property -dict { PACKAGE_PIN W5 IOSTANDARD LVCMOS33 } [get_ports clk]\n")
        f2 = tmp_xdc("b.xdc",
            "set_property -dict { PACKAGE_PIN W5 IOSTANDARD LVCMOS33 } [get_ports led]\n")
        report = lint_xdc_files([f1, f2])
        cross = [i for i in report.issues if i.rule == "PIN_CONFLICT_CROSS_FILE"]
        assert len(cross) == 1


class TestFormat:
    def test_pass_report(self, tmp_xdc):
        p = tmp_xdc("good.xdc",
            "set_property -dict { PACKAGE_PIN W5 IOSTANDARD LVCMOS33 } [get_ports clk]\n"
            "create_clock -name sys_clk -period 10.000 [get_ports clk]\n"
        )
        report = lint_xdc_files([p])
        text = format_lint_report(report)
        assert "PASS" in text

    def test_fail_report(self, tmp_xdc):
        p = tmp_xdc("bad.xdc", (
            "set_property -dict { PACKAGE_PIN W5 IOSTANDARD LVCMOS33 } [get_ports clk]\n"
            "set_property -dict { PACKAGE_PIN W5 IOSTANDARD LVCMOS33 } [get_ports led]\n"
            "create_clock -name sys_clk [get_ports clk]\n"
        ))
        report = lint_xdc_files([p])
        text = format_lint_report(report)
        assert "FAIL" in text
        assert "PIN_CONFLICT" in text
        assert "CLOCK_NO_PERIOD" in text
