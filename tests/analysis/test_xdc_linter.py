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


class TestWildcardIostandard:
    """审计 P0:通配符 / 纯 -dict / 多端口列表形式的 IOSTANDARD 必须被认出来。"""

    def test_wildcard_iostd_covers_indexed_port(self, tmp_xdc):
        """led[*] 的 IOSTANDARD 覆盖 led[0],不应误报 MISSING_IOSTANDARD。"""
        p = tmp_xdc("wild.xdc", (
            "set_property IOSTANDARD LVCMOS18 [get_ports {led[*]}]\n"
            "set_property PACKAGE_PIN W5 [get_ports {led[0]}]\n"
        ))
        issues = lint_xdc_file(p)
        assert all(i.rule != "MISSING_IOSTANDARD" for i in issues)

    def test_wildcard_iostd_does_not_cover_other_port(self, tmp_xdc):
        """通配符只覆盖匹配到的端口,不匹配的端口仍要报。"""
        p = tmp_xdc("wild2.xdc", (
            "set_property IOSTANDARD LVCMOS18 [get_ports {led[*]}]\n"
            "set_property PACKAGE_PIN T17 [get_ports btn]\n"
        ))
        issues = lint_xdc_file(p)
        assert any(
            i.rule == "MISSING_IOSTANDARD" and i.port == "btn" for i in issues
        )

    def test_question_mark_wildcard(self, tmp_xdc):
        """? 单字符通配符也按 Tcl glob 语义匹配。"""
        p = tmp_xdc("wild3.xdc", (
            "set_property IOSTANDARD LVCMOS33 [get_ports {sw?}]\n"
            "set_property PACKAGE_PIN V17 [get_ports sw0]\n"
        ))
        issues = lint_xdc_file(p)
        assert all(i.rule != "MISSING_IOSTANDARD" for i in issues)

    def test_dict_only_iostd_with_separate_pin(self, tmp_xdc):
        """纯 -dict IOSTANDARD(无 PACKAGE_PIN)+ 独立 PACKAGE_PIN 行,不应误报。"""
        p = tmp_xdc("dictonly.xdc", (
            "set_property -dict { IOSTANDARD LVCMOS18 SLEW FAST } [get_ports btn]\n"
            "set_property PACKAGE_PIN T17 [get_ports btn]\n"
        ))
        issues = lint_xdc_file(p)
        assert all(i.rule != "MISSING_IOSTANDARD" for i in issues)

    def test_multiport_list_iostd(self, tmp_xdc):
        """[get_ports {a b c}] 多端口列表要拆开逐个入账。"""
        p = tmp_xdc("multi.xdc", (
            "set_property IOSTANDARD LVCMOS33 [get_ports {a b c}]\n"
            "set_property PACKAGE_PIN U1 [get_ports a]\n"
        ))
        issues = lint_xdc_file(p)
        assert all(i.rule != "MISSING_IOSTANDARD" for i in issues)

    def test_dict_inline_iostd_covers_redundant_bare_pin_line(self, tmp_xdc):
        """审计 P3:-dict 内联 IOSTANDARD 入账后,同 port 的重复裸 PACKAGE_PIN 行不误报。

        Vivado 属性按 port 累积,第一行已设置 clk 的 IOSTANDARD。
        """
        p = tmp_xdc("inline.xdc", (
            "set_property -dict { PACKAGE_PIN W5 IOSTANDARD LVCMOS33 } [get_ports clk]\n"
            "set_property PACKAGE_PIN W5 [get_ports clk]\n"
        ))
        issues = lint_xdc_file(p)
        assert all(i.rule != "MISSING_IOSTANDARD" for i in issues)


class TestCrossFileIostandard:
    """审计 P1:IOSTANDARD 与 PACKAGE_PIN 分属不同 XDC 文件时,规则 2 必须看全局聚合。

    否则误报 MISSING_IOSTANDARD,fixer 进而向引脚 XDC 写入覆盖真实值的约束。
    """

    def test_exact_iostd_in_other_file_no_false_positive(self, tmp_xdc):
        f_iostd = tmp_xdc("iostd.xdc",
            "set_property IOSTANDARD LVCMOS18 [get_ports clk]\n")
        f_pins = tmp_xdc("pins.xdc",
            "set_property PACKAGE_PIN W5 [get_ports clk]\n")
        report = lint_xdc_files([f_iostd, f_pins])
        assert all(i.rule != "MISSING_IOSTANDARD" for i in report.issues)

    def test_wildcard_iostd_in_other_file_no_false_positive(self, tmp_xdc):
        """评审复现场景:公共 XDC 放通配 IOSTANDARD,引脚 XDC 放 PACKAGE_PIN。"""
        f_iostd = tmp_xdc("iostd.xdc",
            "set_property IOSTANDARD LVCMOS18 [get_ports {led[*]}]\n")
        f_pins = tmp_xdc("pins.xdc",
            "set_property PACKAGE_PIN U16 [get_ports {led[0]}]\n")
        report = lint_xdc_files([f_iostd, f_pins])
        assert all(i.rule != "MISSING_IOSTANDARD" for i in report.issues)

    def test_uncovered_port_still_reported_across_files(self, tmp_xdc):
        """跨文件聚合不放过真正没配 IOSTANDARD 的端口。"""
        f_iostd = tmp_xdc("iostd.xdc",
            "set_property IOSTANDARD LVCMOS18 [get_ports {led[*]}]\n")
        f_pins = tmp_xdc("pins.xdc", (
            "set_property PACKAGE_PIN U16 [get_ports {led[0]}]\n"
            "set_property PACKAGE_PIN T17 [get_ports btn]\n"
        ))
        report = lint_xdc_files([f_iostd, f_pins])
        missing = [i for i in report.issues if i.rule == "MISSING_IOSTANDARD"]
        assert len(missing) == 1
        assert missing[0].port == "btn"

    def test_single_file_api_semantics_unchanged(self, tmp_xdc):
        """lint_xdc_file 单文件直接调用的语义不变:本文件内没配仍要报。"""
        p = tmp_xdc("alone.xdc", "set_property PACKAGE_PIN U16 [get_ports foo]\n")
        issues = lint_xdc_file(p)
        assert any(i.rule == "MISSING_IOSTANDARD" for i in issues)


class TestEncodingDegraded:
    """审计 P2:编码降级必须在用户可见报告里露出,不只进 logger。"""

    def _write_undecodable(self, tmp_path):
        p = tmp_path / "weird.xdc"
        # \xff\xff 既非合法 UTF-8 也非合法 GBK
        p.write_bytes(b"set_property PACKAGE_PIN U16 [get_ports foo]\n# \xff\xff\n")
        return p

    def test_undecodable_file_emits_warning_issue(self, tmp_path):
        p = self._write_undecodable(tmp_path)
        issues = lint_xdc_file(p)
        deg = [i for i in issues if i.rule == "ENCODING_DEGRADED"]
        assert len(deg) == 1
        assert deg[0].severity == "warning"
        # 消息含具体解码情况:哪两种编码失败、用了什么降级方式
        assert "UTF-8" in deg[0].message
        assert "GBK" in deg[0].message
        assert "替换符" in deg[0].message

    def test_degraded_visible_in_format_output(self, tmp_path):
        """输出断言(不只 caplog):format_lint_report 里能看到降级。"""
        p = self._write_undecodable(tmp_path)
        report = lint_xdc_files([p])
        text = format_lint_report(report)
        assert "ENCODING_DEGRADED" in text
        assert "替换符" in text

    def test_normal_files_no_degraded_issue(self, tmp_path):
        """UTF-8 / GBK 正常解码的文件不得出现 ENCODING_DEGRADED。"""
        utf8 = tmp_path / "ok.xdc"
        utf8.write_text(
            "set_property -dict { PACKAGE_PIN W5 IOSTANDARD LVCMOS33 } [get_ports clk]\n",
            encoding="utf-8",
        )
        gbk = tmp_path / "gbk.xdc"
        gbk.write_bytes("# 时钟约束\n".encode("gbk"))
        for p in (utf8, gbk):
            issues = lint_xdc_file(p)
            assert all(i.rule != "ENCODING_DEGRADED" for i in issues)


class TestContinuationCreateClock:
    """审计 P1:create_clock 反斜杠续行不应误报 CLOCK_NO_PERIOD。"""

    def test_continued_create_clock_with_period_ok(self, tmp_xdc):
        """-period 在续行上的合法 create_clock 不应报错。"""
        p = tmp_xdc("cont.xdc", (
            "create_clock -name sys_clk \\\n"
            "    -period 10.000 [get_ports clk]\n"
        ))
        issues = lint_xdc_file(p)
        assert all(i.rule != "CLOCK_NO_PERIOD" for i in issues)

    def test_continued_create_clock_missing_period_still_reported(self, tmp_xdc):
        """折叠后真缺 -period 的续行 create_clock 仍要报,行号取首行。"""
        p = tmp_xdc("cont2.xdc", (
            "create_clock -name sys_clk \\\n"
            "    [get_ports clk]\n"
        ))
        issues = lint_xdc_file(p)
        errors = [i for i in issues if i.rule == "CLOCK_NO_PERIOD"]
        assert len(errors) == 1
        assert errors[0].line == 1


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
