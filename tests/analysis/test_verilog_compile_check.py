"""verilog_compile_check 单元测试。

策略:主要测 parser 和格式化(不依赖实际工具安装);
detect_tool / 整体流程用 monkeypatch 模拟。
"""

from unittest.mock import MagicMock, patch

from vivado_mcp.analysis.verilog_compile_check import (
    CompileIssue,
    CompileReport,
    _parse_iverilog,
    _parse_verilator,
    compile_check,
    format_compile_report,
)

# -- parser: iverilog --------------------------------------------------------- #

def test_iverilog_parses_error():
    stderr = "C:/path/test.v:5: syntax error\nC:/path/test.v:5: error: Invalid module item.\n"
    issues = _parse_iverilog(stderr)
    # 同一行可能触发两条诊断,只要至少识别到 error
    assert any(i.severity == "error" for i in issues)
    assert any(i.line == 5 for i in issues)


def test_iverilog_parses_warning():
    stderr = "test.v:7: warning: array x declared but not used.\n"
    issues = _parse_iverilog(stderr)
    assert len(issues) == 1
    assert issues[0].severity == "warning"
    assert "array x" in issues[0].message


def test_iverilog_handles_empty():
    assert _parse_iverilog("") == []


def test_iverilog_ignores_irrelevant_lines():
    stderr = "not a diag line\nanother garbage\n"
    assert _parse_iverilog(stderr) == []


# -- parser: verilator -------------------------------------------------------- #

def test_verilator_parses_error():
    stderr = "%Error: test.v:5:1: syntax error, unexpected endmodule\n"
    issues = _parse_verilator(stderr)
    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert issues[0].line == 5


def test_verilator_parses_warning_with_category():
    stderr = "%Warning-UNUSED: test.v:7:5: Signal is not used: 'x'\n"
    issues = _parse_verilator(stderr)
    assert len(issues) == 1
    assert issues[0].severity == "warning"
    assert "'x'" in issues[0].message


# -- detect_tool 流程 --------------------------------------------------------- #

def test_no_tool_available_returns_skip_report():
    with patch("vivado_mcp.analysis.verilog_compile_check.shutil.which", return_value=None):
        rep = compile_check(["foo.v"], tool="auto")
    assert rep.tool_available is False
    assert "iverilog" in rep.install_hint


def test_specific_tool_request_missing():
    with patch("vivado_mcp.analysis.verilog_compile_check.shutil.which", return_value=None):
        rep = compile_check(["foo.v"], tool="verilator")
    assert not rep.tool_available
    assert "verilator" in rep.install_hint


def test_auto_prefers_iverilog():
    def _which(name):
        return "/fake/iverilog" if name == "iverilog" else None
    with patch("vivado_mcp.analysis.verilog_compile_check.shutil.which", side_effect=_which):
        with patch(
            "vivado_mcp.analysis.verilog_compile_check.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ) as sp:
            rep = compile_check(["foo.v"], tool="auto")
            assert rep.tool_used == "iverilog"
            cmd = sp.call_args[0][0]
            assert cmd[0] == "iverilog"
            assert "-t" in cmd


def test_auto_falls_back_to_verilator():
    def _which(name):
        return "/fake/verilator" if name == "verilator" else None
    with patch("vivado_mcp.analysis.verilog_compile_check.shutil.which", side_effect=_which):
        with patch(
            "vivado_mcp.analysis.verilog_compile_check.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ):
            rep = compile_check(["foo.v"], tool="auto")
            assert rep.tool_used == "verilator"


def test_pass_when_returncode_0_and_no_issues():
    with patch("vivado_mcp.analysis.verilog_compile_check.shutil.which",
               return_value="/fake/iverilog"):
        with patch(
            "vivado_mcp.analysis.verilog_compile_check.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ):
            rep = compile_check(["foo.v"], tool="iverilog")
    text = format_compile_report(rep)
    assert "PASS" in text


def test_error_passthrough():
    with patch("vivado_mcp.analysis.verilog_compile_check.shutil.which",
               return_value="/fake/iverilog"):
        with patch(
            "vivado_mcp.analysis.verilog_compile_check.subprocess.run",
            return_value=MagicMock(
                returncode=1,
                stdout="",
                stderr="foo.v:3: syntax error\n",
            ),
        ):
            rep = compile_check(["foo.v"], tool="iverilog")
    assert rep.return_code == 1
    assert len(rep.errors) >= 1
    text = format_compile_report(rep)
    assert "FAIL" in text


def test_timeout_returns_negative_returncode():
    import subprocess
    with patch("vivado_mcp.analysis.verilog_compile_check.shutil.which",
               return_value="/fake/iverilog"):
        with patch(
            "vivado_mcp.analysis.verilog_compile_check.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="iverilog", timeout=1.0),
        ):
            rep = compile_check(["foo.v"], tool="iverilog", timeout=1.0)
    assert rep.return_code == -1
    assert "超时" in rep.raw_stderr


# -- report properties ------------------------------------------------------- #

def test_report_error_warning_split():
    rep = CompileReport(
        tool_used="iverilog",
        tool_available=True,
        issues=[
            CompileIssue("error", "a.v", 1, "msg", "iverilog"),
            CompileIssue("warning", "a.v", 2, "msg", "iverilog"),
            CompileIssue("error", "a.v", 3, "msg", "iverilog"),
        ],
    )
    assert len(rep.errors) == 2
    assert len(rep.warnings) == 1


def test_format_skip_shows_install_hint():
    rep = CompileReport(
        tool_available=False,
        install_hint="iverilog 未安装。Windows: scoop install iverilog",
    )
    text = format_compile_report(rep)
    assert "SKIP" in text
    assert "scoop install iverilog" in text
