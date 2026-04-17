"""xdc_parser.py 单元测试。

重点覆盖：
- 单行 / 多行约束解析
- 含方括号的端口名称（如 rxp[0]）
- VMCP_XDC_PIN_DONE 标记正确忽略
- 空输入容错
- **新：B3 修复** parse_xdc_file 支持 -dict 和传统两种语法
"""

from pathlib import Path

import pytest

from vivado_mcp.analysis.xdc_parser import parse_xdc_constraints, parse_xdc_file

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"

# ====================================================================== #
#  基本解析功能
# ====================================================================== #


class TestParseBasic:
    """基本解析测试。"""

    def test_parse_single_constraint(self):
        """解析单行约束。"""
        raw = "VMCP_XDC_PIN:C:/project/board.xdc|10|AB8|sys_clk_p\n"
        result = parse_xdc_constraints(raw)

        assert len(result) == 1
        c = result[0]
        assert c.source_file == "C:/project/board.xdc"
        assert c.line_number == 10
        assert c.pin == "AB8"
        assert c.port == "sys_clk_p"

    def test_parse_multiple_constraints(self):
        """解析多行约束。"""
        raw = (
            "VMCP_XDC_PIN:C:/project/board.xdc|15|AA4|pcie_rxp[0]\n"
            "VMCP_XDC_PIN:C:/project/board.xdc|16|AB6|pcie_rxp[1]\n"
            "VMCP_XDC_PIN:C:/project/board.xdc|17|AC4|pcie_rxp[2]\n"
            "VMCP_XDC_PIN_DONE\n"
        )
        result = parse_xdc_constraints(raw)

        assert len(result) == 3
        assert result[0].pin == "AA4"
        assert result[1].pin == "AB6"
        assert result[2].pin == "AC4"


# ====================================================================== #
#  特殊端口名称
# ====================================================================== #


class TestSpecialPorts:
    """特殊端口名称处理测试。"""

    def test_port_with_brackets(self):
        """端口名称含方括号 [0] 应正确解析。"""
        raw = "VMCP_XDC_PIN:C:/proj/pins.xdc|5|M6|pcie_7x_mgt_rtl_0_rxp[0]\n"
        result = parse_xdc_constraints(raw)

        assert len(result) == 1
        assert result[0].port == "pcie_7x_mgt_rtl_0_rxp[0]"
        assert result[0].pin == "M6"

    def test_port_without_brackets(self):
        """普通端口名称（无方括号）应正确解析。"""
        raw = "VMCP_XDC_PIN:C:/proj/pins.xdc|21|AB8|sys_clk_p\n"
        result = parse_xdc_constraints(raw)

        assert len(result) == 1
        assert result[0].port == "sys_clk_p"


# ====================================================================== #
#  标记和边界处理
# ====================================================================== #


class TestMarkers:
    """标记行和边界情况测试。"""

    def test_done_marker_ignored(self):
        """VMCP_XDC_PIN_DONE 不应被解析为约束。"""
        raw = (
            "VMCP_XDC_PIN:C:/proj/pins.xdc|1|AA4|port_a\n"
            "VMCP_XDC_PIN_DONE\n"
        )
        result = parse_xdc_constraints(raw)

        assert len(result) == 1
        # 确认没有误将 DONE 行解析为约束
        assert all(c.port != "VMCP_XDC_PIN_DONE" for c in result)

    def test_empty_output(self):
        """空字符串应返回空列表。"""
        assert parse_xdc_constraints("") == []

    def test_whitespace_only(self):
        """仅含空白字符应返回空列表。"""
        assert parse_xdc_constraints("   \n\n  ") == []

    def test_non_matching_lines_ignored(self):
        """非 VMCP_XDC_PIN 行应被忽略。"""
        raw = (
            "INFO: Reading constraints...\n"
            "VMCP_XDC_PIN:C:/proj/pins.xdc|1|AA4|port_a\n"
            "Some other output\n"
            "VMCP_XDC_PIN_DONE\n"
        )
        result = parse_xdc_constraints(raw)

        assert len(result) == 1
        assert result[0].port == "port_a"


# ====================================================================== #
#  数据类型验证
# ====================================================================== #


class TestDataTypes:
    """字段数据类型测试。"""

    def test_line_number_is_int(self):
        """line_number 应为 int 类型。"""
        raw = "VMCP_XDC_PIN:C:/proj/pins.xdc|42|AB8|sys_clk_p\n"
        result = parse_xdc_constraints(raw)

        assert isinstance(result[0].line_number, int)
        assert result[0].line_number == 42

    def test_constraint_is_frozen(self):
        """XdcConstraint 应为不可变（frozen）。"""
        raw = "VMCP_XDC_PIN:C:/proj/pins.xdc|1|AA4|port_a\n"
        result = parse_xdc_constraints(raw)

        with pytest.raises(AttributeError):
            result[0].pin = "BB5"  # type: ignore[misc]


# ====================================================================== #
#  B3 修复：parse_xdc_file 支持 -dict 和传统两种语法
# ====================================================================== #


class TestParseXdcFile:
    """直接读取 XDC 文件的新接口测试。"""

    def test_dict_and_traditional_syntax(self):
        """同时识别 -dict 和传统两种语法。"""
        fixture = FIXTURES_DIR / "sample_dict_xdc.xdc"
        result = parse_xdc_file(fixture)

        ports = {c.port: c for c in result}

        # -dict 语法
        assert "clk" in ports
        assert ports["clk"].pin == "W5"
        assert "rst_n" in ports
        assert ports["rst_n"].pin == "U18"

        # -dict 向量端口（花括号转义）
        assert "led[0]" in ports
        assert ports["led[0]"].pin == "U16"
        assert "led[1]" in ports
        assert ports["led[1]"].pin == "E19"

        # 传统语法
        assert "led[2]" in ports
        assert ports["led[2]"].pin == "U19"

        # 带尾注释的 -dict
        assert "led[3]" in ports
        assert ports["led[3]"].pin == "V19"

        # 传统语法 + 空格
        assert "led[4]" in ports
        assert ports["led[4]"].pin == "W18"

    def test_ignores_commented_out_constraints(self):
        """# 注释行的约束不应被解析。"""
        fixture = FIXTURES_DIR / "sample_dict_xdc.xdc"
        result = parse_xdc_file(fixture)

        ports = {c.port for c in result}
        # fixture 里有被注释掉的 fake 和 another_fake
        assert "fake" not in ports
        assert "another_fake" not in ports

    def test_file_not_found(self):
        """不存在的文件应抛 FileNotFoundError。"""
        with pytest.raises(FileNotFoundError):
            parse_xdc_file("/nonexistent/path.xdc")

    def test_line_numbers_match_source(self):
        """line_number 应对应 XDC 文件中的实际行号。"""
        fixture = FIXTURES_DIR / "sample_dict_xdc.xdc"
        result = parse_xdc_file(fixture)

        # 验证所有行号 > 0 且 <= 文件总行数
        file_lines = fixture.read_text(encoding="utf-8").splitlines()
        for c in result:
            assert 0 < c.line_number <= len(file_lines)
