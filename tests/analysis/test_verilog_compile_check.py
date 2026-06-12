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


def test_iverilog_sorry_classified_as_error():
    """iverilog 的 'sorry:'(构造合法但工具无法处理)归为 error,不是 info。"""
    stderr = "tb.sv:12: sorry: constant selects in always_* processes are not currently supported (all bits will be included).\n"  # noqa: E501
    issues = _parse_iverilog(stderr)
    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert issues[0].line == 12
    # 保留 "sorry:" 前缀,让用户知道是工具能力限制而非代码错误
    assert "sorry:" in issues[0].message


def test_sorry_only_rc1_reports_fail_not_empty_warn():
    """rc=1 且诊断全是 sorry 时,报告是 FAIL + 可见原因,
    不再是 'WARN (0 warnings) 返回码: 1' 的空报告。"""
    with patch("vivado_mcp.analysis.verilog_compile_check.shutil.which",
               return_value="/fake/iverilog"), \
         patch("vivado_mcp.analysis.verilog_compile_check._scoop_fallback", return_value=None):
        with patch(
            "vivado_mcp.analysis.verilog_compile_check.subprocess.run",
            return_value=MagicMock(
                returncode=1,
                stdout="",
                stderr="tb.sv:12: sorry: case inside is not currently supported.\n",
            ),
        ):
            rep = compile_check(["tb.sv"], tool="iverilog")
    assert len(rep.errors) >= 1
    text = format_compile_report(rep)
    assert "FAIL" in text
    assert "WARN (0 warnings)" not in text
    assert "sorry" in text  # 具体原因可见


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
    with patch("vivado_mcp.analysis.verilog_compile_check.shutil.which", return_value=None), \
         patch("vivado_mcp.analysis.verilog_compile_check._scoop_fallback", return_value=None):
        rep = compile_check(["foo.v"], tool="auto")
    assert rep.tool_available is False
    assert "iverilog" in rep.install_hint


def test_specific_tool_request_missing():
    with patch("vivado_mcp.analysis.verilog_compile_check.shutil.which", return_value=None), \
         patch("vivado_mcp.analysis.verilog_compile_check._scoop_fallback", return_value=None):
        rep = compile_check(["foo.v"], tool="verilator")
    assert not rep.tool_available
    assert "verilator" in rep.install_hint


def test_auto_prefers_iverilog():
    def _which(name):
        return "/fake/iverilog" if name == "iverilog" else None
    with patch("vivado_mcp.analysis.verilog_compile_check.shutil.which", side_effect=_which), \
         patch("vivado_mcp.analysis.verilog_compile_check._scoop_fallback", return_value=None):
        with patch(
            "vivado_mcp.analysis.verilog_compile_check.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ) as sp:
            rep = compile_check(["foo.v"], tool="auto")
            assert rep.tool_used == "iverilog"
            cmd = sp.call_args[0][0]
            # cmd[0] 是绝对路径,可能是 shutil.which 结果或 scoop fallback
            assert "iverilog" in cmd[0]
            assert "-t" in cmd


def test_subprocess_env_gets_scoop_bin_on_path(tmp_path):
    """subprocess.run 的 env 应该把 exe 目录 + scoop apps bin 补到 PATH 开头,
    避免 iverilog 启动时 DLL 加载失败(0xC0000135)。"""
    fake_home = tmp_path
    # 准备一个假的 scoop apps bin + shims
    apps_bin = fake_home / "scoop" / "apps" / "iverilog" / "current" / "bin"
    apps_bin.mkdir(parents=True)
    (apps_bin / "iverilog.exe").write_text("")
    (apps_bin / "libmingw.dll").write_text("")
    shims = fake_home / "scoop" / "shims"
    shims.mkdir()
    shim = shims / "iverilog.exe"
    shim.write_text("")

    with patch.dict("os.environ", {"USERPROFILE": str(fake_home), "PATH": "/existing"}):
        with patch("vivado_mcp.analysis.verilog_compile_check.shutil.which",
                   return_value=None):
            with patch(
                "vivado_mcp.analysis.verilog_compile_check.subprocess.run",
                return_value=MagicMock(returncode=0, stdout="", stderr=""),
            ) as sp:
                compile_check(["foo.v"], tool="iverilog")

    env_passed = sp.call_args.kwargs.get("env")
    assert env_passed is not None, "env 应该被显式传入"
    path = env_passed.get("PATH", "")
    # apps bin 目录和 shims 目录都在 PATH 开头(前置 > 原 PATH)
    assert str(apps_bin) in path
    # 原 PATH 仍保留(不覆盖)
    assert "/existing" in path


def test_scoop_fallback_when_path_missing(tmp_path):
    """Windows+scoop 经典坑:which 找不到但 ~/scoop/shims/iverilog.exe 存在。"""
    fake_home = tmp_path
    shim_dir = fake_home / "scoop" / "shims"
    shim_dir.mkdir(parents=True)
    fake_shim = shim_dir / "iverilog.exe"
    fake_shim.write_text("")

    with patch.dict("os.environ", {"USERPROFILE": str(fake_home)}):
        with patch("vivado_mcp.analysis.verilog_compile_check.shutil.which",
                   return_value=None):
            with patch(
                "vivado_mcp.analysis.verilog_compile_check.subprocess.run",
                return_value=MagicMock(returncode=0, stdout="", stderr=""),
            ) as sp:
                rep = compile_check(["foo.v"], tool="iverilog")
    assert rep.tool_available is True
    assert rep.tool_used == "iverilog"
    # subprocess 拿到 shim 绝对路径
    cmd = sp.call_args[0][0]
    assert cmd[0] == str(fake_shim)


def test_auto_falls_back_to_verilator():
    def _which(name):
        return "/fake/verilator" if name == "verilator" else None
    with patch("vivado_mcp.analysis.verilog_compile_check.shutil.which", side_effect=_which), \
         patch("vivado_mcp.analysis.verilog_compile_check._scoop_fallback", return_value=None):
        with patch(
            "vivado_mcp.analysis.verilog_compile_check.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ):
            rep = compile_check(["foo.v"], tool="auto")
            assert rep.tool_used == "verilator"


def test_pass_when_returncode_0_and_no_issues():
    with patch("vivado_mcp.analysis.verilog_compile_check.shutil.which",
               return_value="/fake/iverilog"), \
         patch("vivado_mcp.analysis.verilog_compile_check._scoop_fallback", return_value=None):
        with patch(
            "vivado_mcp.analysis.verilog_compile_check.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ):
            rep = compile_check(["foo.v"], tool="iverilog")
    text = format_compile_report(rep)
    assert "PASS" in text


def test_error_passthrough():
    with patch("vivado_mcp.analysis.verilog_compile_check.shutil.which",
               return_value="/fake/iverilog"), \
         patch("vivado_mcp.analysis.verilog_compile_check._scoop_fallback", return_value=None):
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
               return_value="/fake/iverilog"), \
         patch("vivado_mcp.analysis.verilog_compile_check._scoop_fallback", return_value=None):
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


# -- SystemVerilog: iverilog 需要 -g2012 -------------------------------------- #

def test_sv_file_adds_g2012():
    """含 .sv 文件时 iverilog 命令必须带 -g2012(否则合法 SV 误报 FAIL)。"""
    with patch("vivado_mcp.analysis.verilog_compile_check.shutil.which",
               return_value="/fake/iverilog"), \
         patch("vivado_mcp.analysis.verilog_compile_check._scoop_fallback", return_value=None):
        with patch(
            "vivado_mcp.analysis.verilog_compile_check.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ) as sp:
            compile_check(["counter.sv"], tool="iverilog")
    cmd = sp.call_args[0][0]
    assert "-g2012" in cmd


def test_pure_v_files_no_g2012():
    """纯 .v 文件保持现状,不加 -g2012。"""
    with patch("vivado_mcp.analysis.verilog_compile_check.shutil.which",
               return_value="/fake/iverilog"), \
         patch("vivado_mcp.analysis.verilog_compile_check._scoop_fallback", return_value=None):
        with patch(
            "vivado_mcp.analysis.verilog_compile_check.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ) as sp:
            compile_check(["top.v", "uart.v"], tool="iverilog")
    cmd = sp.call_args[0][0]
    assert "-g2012" not in cmd


def test_mixed_v_sv_adds_g2012():
    """混合 .v + .sv 列表也要加 -g2012(.SV 大写后缀同样识别)。"""
    with patch("vivado_mcp.analysis.verilog_compile_check.shutil.which",
               return_value="/fake/iverilog"), \
         patch("vivado_mcp.analysis.verilog_compile_check._scoop_fallback", return_value=None):
        with patch(
            "vivado_mcp.analysis.verilog_compile_check.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ) as sp:
            compile_check(["top.v", "fifo.SV"], tool="iverilog")
    cmd = sp.call_args[0][0]
    assert "-g2012" in cmd


def test_verilator_sv_no_g2012_flag():
    """verilator 原生支持 SV,不需要(也不认识 iverilog 的)-g2012。"""
    def _which(name):
        return "/fake/verilator" if name == "verilator" else None
    with patch("vivado_mcp.analysis.verilog_compile_check.shutil.which", side_effect=_which), \
         patch("vivado_mcp.analysis.verilog_compile_check._scoop_fallback", return_value=None):
        with patch(
            "vivado_mcp.analysis.verilog_compile_check.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ) as sp:
            compile_check(["counter.sv"], tool="verilator")
    cmd = sp.call_args[0][0]
    assert "-g2012" not in cmd


# -- VHDL 扩展名守卫 ----------------------------------------------------------- #

def test_vhdl_file_returns_skip():
    """.vhd 文件直接 SKIP + 指引,不跑工具(守卫在工具检测之前)。"""
    rep = compile_check(["top.vhd"])
    assert rep.skip_reason != ""
    assert rep.tool_used == ""
    text = format_compile_report(rep)
    assert "SKIP" in text
    assert "VHDL" in text
    assert "check_syntax" in text
    assert "FAIL" not in text


def test_vhdl_uppercase_and_vhdl_extension_also_skip():
    """.VHDL 大写后缀同样触发守卫。"""
    rep = compile_check(["TOP.VHDL"])
    assert rep.skip_reason != ""


def test_mixed_verilog_vhdl_skips_with_filenames():
    """混合列表里只要有 VHDL 就 SKIP,且指出具体文件名。"""
    rep = compile_check(["top.v", "pkg.vhd"])
    assert "pkg.vhd" in rep.skip_reason


def test_v_file_not_caught_by_vhdl_guard():
    """negative: 纯 .v 不触发 VHDL 守卫。"""
    with patch("vivado_mcp.analysis.verilog_compile_check.shutil.which",
               return_value="/fake/iverilog"), \
         patch("vivado_mcp.analysis.verilog_compile_check._scoop_fallback", return_value=None):
        with patch(
            "vivado_mcp.analysis.verilog_compile_check.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ):
            rep = compile_check(["foo.v"], tool="iverilog")
    assert rep.skip_reason == ""
    assert rep.tool_used == "iverilog"
