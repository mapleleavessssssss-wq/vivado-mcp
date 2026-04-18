"""verilog_quick_check 单元测试。"""

from pathlib import Path

import pytest

from vivado_mcp.analysis.verilog_quick_check import (
    format_report,
    quick_check_verilog,
)


@pytest.fixture
def tmp_v(tmp_path: Path):
    def _make(name: str, content: str) -> Path:
        p = tmp_path / name
        p.write_text(content, encoding="utf-8")
        return p
    return _make


class TestEndmodule:
    def test_missing_endmodule_error(self, tmp_v):
        p = tmp_v("foo.v", "module foo(); // 忘了 endmodule\n")
        r = quick_check_verilog(p)
        assert any(i.rule == "MISSING_ENDMODULE" for i in r.errors)

    def test_endmodule_present_ok(self, tmp_v):
        p = tmp_v("foo.v", "module foo();\nendmodule\n")
        r = quick_check_verilog(p)
        assert all(i.rule != "MISSING_ENDMODULE" for i in r.issues)


class TestModuleName:
    def test_name_mismatch_warning(self, tmp_v):
        p = tmp_v("foo.v", "module bar();\nendmodule\n")
        r = quick_check_verilog(p)
        assert any(i.rule == "MODULE_NAME_MISMATCH" for i in r.warnings)

    def test_name_match_ok(self, tmp_v):
        p = tmp_v("top.v", "module top();\nendmodule\n")
        r = quick_check_verilog(p)
        assert all(i.rule != "MODULE_NAME_MISMATCH" for i in r.issues)

    def test_no_module_error(self, tmp_v):
        p = tmp_v("foo.v", "// 注释而已,没有 module\n")
        r = quick_check_verilog(p)
        assert any(i.rule == "NO_MODULE" for i in r.errors)


class TestBracketBalance:
    def test_paren_mismatch_detected(self, tmp_v):
        p = tmp_v("foo.v", "module foo(input clk;\nendmodule\n")  # 少 )
        r = quick_check_verilog(p)
        assert any(i.rule == "PAREN_MISMATCH" for i in r.errors)

    def test_balanced_ok(self, tmp_v):
        p = tmp_v("foo.v", "module foo(input clk);\nendmodule\n")
        r = quick_check_verilog(p)
        assert all(i.rule != "PAREN_MISMATCH" for i in r.issues)

    def test_comment_braces_ignored(self, tmp_v):
        """注释里的括号不应计数,避免误报。"""
        p = tmp_v("foo.v", "module foo();\n// 这里(括号)不算\nendmodule\n")
        r = quick_check_verilog(p)
        assert all(
            i.rule not in ("PAREN_MISMATCH", "BRACE_MISMATCH", "BRACKET_MISMATCH")
            for i in r.issues
        )


class TestEmpty:
    def test_empty_file_warning(self, tmp_v):
        p = tmp_v("foo.v", "")
        r = quick_check_verilog(p)
        assert any(i.rule == "EMPTY_FILE" for i in r.warnings)

    def test_missing_file(self, tmp_path):
        r = quick_check_verilog(tmp_path / "does_not_exist.v")
        assert any(i.rule == "FILE_NOT_FOUND" for i in r.errors)


class TestFormat:
    def test_pass_returns_empty(self, tmp_v):
        p = tmp_v("foo.v", "module foo();\nendmodule\n")
        r = quick_check_verilog(p)
        assert format_report(r) == ""

    def test_fail_returns_text(self, tmp_v):
        p = tmp_v("foo.v", "module foo(;\n")  # 缺 endmodule + 括号错
        r = quick_check_verilog(p)
        text = format_report(r)
        assert "FAIL" in text
        assert "MISSING_ENDMODULE" in text or "PAREN_MISMATCH" in text
