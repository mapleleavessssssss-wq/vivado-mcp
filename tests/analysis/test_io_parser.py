"""io_parser.py 单元测试。

重点覆盖：
- 基本表格解析（fixture 文件中 20 个端口）
- GT / GPIO 端口类型判定
- 空 IO 标准处理（GT 端口无 IO 标准）
- 汇总统计数值正确性
- 字段类型（bank=int, fixed=bool）
- to_dict() JSON 序列化
- 空输入容错
"""

import json
from pathlib import Path

import pytest

from vivado_mcp.analysis.io_parser import IoPort, IoReport, parse_report_io

# 测试 fixture 路径
FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
SAMPLE_REPORT_IO = FIXTURES_DIR / "sample_report_io.txt"
SAMPLE_REPORT_IO_2019_1 = FIXTURES_DIR / "sample_report_io_2019_1.txt"


@pytest.fixture
def report_io_text() -> str:
    """读取 sample_report_io.txt fixture 文件。"""
    return SAMPLE_REPORT_IO.read_text(encoding="utf-8")


@pytest.fixture
def parsed_report(report_io_text: str) -> IoReport:
    """解析 fixture 文件得到的 IoReport 实例。"""
    return parse_report_io(report_io_text)


# ====================================================================== #
#  B6 修复：Vivado 2019.1 的 Pin 版格式
# ====================================================================== #


class TestVivado2019_1Format:
    """Vivado 2019.1 的 report_io 按 Pin 格式（Pin Number | Signal Name | ...）。"""

    def test_parses_counter_design(self):
        """极简 counter 设计：1 个 clk + 1 个 rst + 8 个 LED = 10 个端口。"""
        text = SAMPLE_REPORT_IO_2019_1.read_text(encoding="utf-8")
        result = parse_report_io(text)

        # counter 设计有 10 个端口
        assert result.total_ports == 10
        assert result.gpio_ports == 10
        assert result.gt_ports == 0

        ports = {p.port_name: p for p in result.ports}

        # 验证 clk 映射到 W5（传统语法）
        assert "clk" in ports
        assert ports["clk"].package_pin == "W5"
        assert ports["clk"].direction == "INPUT"
        assert ports["clk"].io_standard == "LVCMOS33"
        assert ports["clk"].bank == 34
        assert ports["clk"].fixed is True

        # 验证 led[0] 映射到 U16（-dict 语法）
        assert "led[0]" in ports
        assert ports["led[0]"].package_pin == "U16"
        assert ports["led[0]"].direction == "OUTPUT"

        # rst_n 映射到 U18
        assert "rst_n" in ports
        assert ports["rst_n"].package_pin == "U18"

    def test_skips_unused_physical_pins(self):
        """只有 Signal Name 非空的物理引脚才算端口，其他（GND、VCC 等）跳过。"""
        text = SAMPLE_REPORT_IO_2019_1.read_text(encoding="utf-8")
        result = parse_report_io(text)

        # xc7a35t-cpg236 有 236 个引脚，但只有 10 个被设计使用
        # 确保 GND / VCC / 未使用的 IO 都没被误当成端口
        assert result.total_ports < 236


# ====================================================================== #
#  基本解析功能
# ====================================================================== #


class TestParseBasicTable:
    """基本表格解析测试。"""

    def test_parse_basic_table(self, parsed_report: IoReport):
        """解析 fixture 文件应得到 20 个端口。"""
        assert parsed_report.total_ports == 20
        assert len(parsed_report.ports) == 20

    def test_port_names_unique(self, parsed_report: IoReport):
        """所有端口名称应唯一。"""
        names = [p.port_name for p in parsed_report.ports]
        assert len(names) == len(set(names))


# ====================================================================== #
#  端口类型判定
# ====================================================================== #


class TestPortTypes:
    """GT / GPIO 端口类型判定测试。"""

    def test_gt_port_type(self, parsed_report: IoReport):
        """站点含 MGT 的端口应标记为 GT 类型。"""
        gt_ports = [p for p in parsed_report.ports if p.io_type == "GT"]
        assert len(gt_ports) > 0

        # 所有 GT 端口的站点应含 "MGT"
        for port in gt_ports:
            assert "MGT" in port.site, f"GT 端口 {port.port_name} 的站点 {port.site} 不含 MGT"

    def test_gpio_port_type(self, parsed_report: IoReport):
        """站点含 IOB 的端口应标记为 GPIO 类型。"""
        gpio_ports = [p for p in parsed_report.ports if p.io_type == "GPIO"]
        assert len(gpio_ports) > 0

        # GPIO 端口的站点不应含 "MGT"
        for port in gpio_ports:
            assert "MGT" not in port.site, (
                f"GPIO 端口 {port.port_name} 的站点 {port.site} 含 MGT"
            )


# ====================================================================== #
#  IO 标准处理
# ====================================================================== #


class TestIoStandard:
    """IO 标准字段测试。"""

    def test_empty_io_standard(self, parsed_report: IoReport):
        """GT 端口的 io_standard 应为空字符串。"""
        gt_ports = [p for p in parsed_report.ports if p.io_type == "GT"]
        for port in gt_ports:
            assert port.io_standard == "", (
                f"GT 端口 {port.port_name} 的 io_standard 应为空，实际为 '{port.io_standard}'"
            )

    def test_gpio_has_io_standard(self, parsed_report: IoReport):
        """GPIO 端口应有非空的 io_standard。"""
        gpio_ports = [p for p in parsed_report.ports if p.io_type == "GPIO"]
        for port in gpio_ports:
            assert port.io_standard != "", (
                f"GPIO 端口 {port.port_name} 的 io_standard 不应为空"
            )


# ====================================================================== #
#  汇总统计
# ====================================================================== #


class TestSummaryCounts:
    """汇总统计测试。"""

    def test_summary_counts(self, parsed_report: IoReport):
        """验证总数、GT 数、GPIO 数的统计。"""
        assert parsed_report.total_ports == 20
        assert parsed_report.gt_ports == 16
        assert parsed_report.gpio_ports == 4

    def test_gt_gpio_sum(self, parsed_report: IoReport):
        """GT + GPIO 应等于总端口数。"""
        assert parsed_report.gt_ports + parsed_report.gpio_ports == parsed_report.total_ports


# ====================================================================== #
#  具体端口详情验证
# ====================================================================== #


class TestPortDetails:
    """特定端口属性验证测试。"""

    def test_port_details(self, parsed_report: IoReport):
        """验证 sys_clk_p 端口的详细信息。"""
        port = _find_port(parsed_report, "sys_clk_p")
        assert port is not None, "未找到 sys_clk_p 端口"

        assert port.package_pin == "AB8"
        assert port.site == "IOB_X0Y158"
        assert port.direction == "INPUT"
        assert port.io_standard == "LVDS_25"
        assert port.bank == 33
        assert port.fixed is True
        assert port.io_type == "GPIO"

    def test_gt_port_details(self, parsed_report: IoReport):
        """验证 PCIe GT 接收端口的详细信息。"""
        port = _find_port(parsed_report, "pcie_7x_mgt_rtl_0_rxp[0]")
        assert port is not None, "未找到 pcie_7x_mgt_rtl_0_rxp[0] 端口"

        assert port.package_pin == "M6"
        assert port.site == "MGTXRXP3_116"
        assert port.direction == "INPUT"
        assert port.io_standard == ""
        assert port.bank == 116
        assert port.io_type == "GT"


# ====================================================================== #
#  字段类型检查
# ====================================================================== #


class TestFieldTypes:
    """字段数据类型验证测试。"""

    def test_bank_is_int(self, parsed_report: IoReport):
        """bank 字段应为 int 类型。"""
        for port in parsed_report.ports:
            assert isinstance(port.bank, int), (
                f"端口 {port.port_name} 的 bank 类型为 {type(port.bank).__name__}，应为 int"
            )

    def test_fixed_is_bool(self, parsed_report: IoReport):
        """fixed 字段应为 bool 类型。"""
        for port in parsed_report.ports:
            assert isinstance(port.fixed, bool), (
                f"端口 {port.port_name} 的 fixed 类型为 {type(port.fixed).__name__}，应为 bool"
            )


# ====================================================================== #
#  JSON 序列化
# ====================================================================== #


class TestSerialization:
    """JSON 序列化测试。"""

    def test_to_dict_serializable(self, parsed_report: IoReport):
        """to_dict() 结果应可被 json.dumps 序列化。"""
        d = parsed_report.to_dict()

        # 不应抛出异常
        json_str = json.dumps(d, ensure_ascii=False)
        assert isinstance(json_str, str)

        # 反序列化后结构正确
        restored = json.loads(json_str)
        assert restored["total_ports"] == 20
        assert len(restored["ports"]) == 20


# ====================================================================== #
#  边界情况
# ====================================================================== #


class TestEdgeCases:
    """边界情况测试。"""

    def test_empty_input(self):
        """空字符串输入应返回空报告。"""
        report = parse_report_io("")
        assert report.total_ports == 0
        assert len(report.ports) == 0
        assert report.gt_ports == 0
        assert report.gpio_ports == 0

    def test_whitespace_only(self):
        """仅含空白的输入应返回空报告。"""
        report = parse_report_io("   \n\n   ")
        assert report.total_ports == 0

    def test_no_table(self):
        """不含表格的文本应返回空报告。"""
        report = parse_report_io("Report IO\nSome random text\nNo table here")
        assert report.total_ports == 0


# ====================================================================== #
#  辅助函数
# ====================================================================== #


def _find_port(report: IoReport, port_name: str) -> IoPort | None:
    """按名称查找端口。"""
    for port in report.ports:
        if port.port_name == port_name:
            return port
    return None
