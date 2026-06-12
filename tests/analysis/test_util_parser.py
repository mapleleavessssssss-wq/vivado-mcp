"""util_parser 单元测试。"""

from pathlib import Path

import pytest

from vivado_mcp.analysis.util_parser import (
    ResourceUsage,
    UtilizationReport,
    format_utilization_report,
    parse_utilization,
)

_FIXTURE = Path(__file__).parent.parent / "fixtures" / "sample_report_utilization.txt"

SAMPLE = """
Design Information
------------------
This is a sample

1. Slice Logic
--------------

+----------------------------+------+-------+-----------+-------+
|          Site Type         | Used | Fixed | Available | Util% |
+----------------------------+------+-------+-----------+-------+
| Slice LUTs                 |  5234 |     0 |     20800 |  25.16 |
|   LUT as Logic             |  5100 |     0 |     20800 |  24.52 |
|   LUT as Memory            |   134 |     0 |      9600 |   1.40 |
| Slice Registers            |  3100 |     0 |     41600 |   7.45 |
|   Register as Flip Flop    |  3100 |     0 |     41600 |   7.45 |
+----------------------------+------+-------+-----------+-------+

3. Memory
---------

+----------------+------+-------+-----------+-------+
|   Site Type    | Used | Fixed | Available | Util% |
+----------------+------+-------+-----------+-------+
| Block RAM Tile |    2 |     0 |        50 |  4.00 |
+----------------+------+-------+-----------+-------+
"""


def test_parses_lut():
    r = parse_utilization(SAMPLE)
    lut = r.get("Slice LUTs")
    assert lut is not None
    assert lut.used == 5234
    assert lut.available == 20800
    assert lut.percent == pytest.approx(25.16)


def test_parses_registers():
    r = parse_utilization(SAMPLE)
    ff = r.get("Slice Registers")
    assert ff is not None
    assert ff.used == 3100


def test_parses_bram():
    r = parse_utilization(SAMPLE)
    bram = r.get("Block RAM Tile")
    assert bram is not None
    assert bram.used == 2


def test_is_critical_flag():
    r = ResourceUsage(name="LUT", used=19000, available=20000, percent=95.0)
    assert r.is_critical is True
    assert r.is_warning is False


def test_is_warning_flag():
    r = ResourceUsage(name="LUT", used=16000, available=20000, percent=80.0)
    assert r.is_critical is False
    assert r.is_warning is True


def test_format_with_critical():
    res = UtilizationReport(resources=[
        ResourceUsage(name="Slice LUTs", used=19000, available=20000, percent=95.0),
    ])
    text = format_utilization_report(res)
    assert "CRITICAL" in text
    assert "资源超限风险" in text


def test_format_pass():
    res = UtilizationReport(resources=[
        ResourceUsage(name="Slice LUTs", used=1000, available=20000, percent=5.0),
    ])
    text = format_utilization_report(res)
    assert "CRITICAL" not in text
    assert "超限" not in text


def test_empty_fallback():
    res = UtilizationReport(resources=[])
    text = format_utilization_report(res)
    assert "未解析到" in text or "请确认" in text


def test_to_dict_structure():
    r = parse_utilization(SAMPLE)
    d = r.to_dict()
    assert "resources" in d
    assert "critical" in d
    assert "warning" in d


# -- Vivado 2020.1+ Prohibited 列(列索引从表头动态定位) ----------------------- #

SAMPLE_2020 = """
1. CLB Logic
------------

+----------------------------+------+-------+------------+-----------+-------+
|          Site Type         | Used | Fixed | Prohibited | Available | Util% |
+----------------------------+------+-------+------------+-----------+-------+
| Slice LUTs                 | 1440 |     0 |          0 |     20800 |  6.92 |
| Slice Registers            | 2200 |     0 |          0 |     41600 |  5.29 |
+----------------------------+------+-------+------------+-----------+-------+
"""


def test_2020_prohibited_column_not_misaligned():
    """2020.1+ 含 Prohibited 列时,Available/Util% 不能整列错位。"""
    r = parse_utilization(SAMPLE_2020)
    lut = r.get("Slice LUTs")
    assert lut is not None
    assert lut.used == 1440
    assert lut.available == 20800          # 修复前会错位捕成 0
    assert lut.percent == pytest.approx(6.92)  # 修复前会捕成 20800.0
    assert lut.is_critical is False


def test_2020_format_no_false_critical():
    """2020.1+ 格式不应误报资源超限。"""
    text = format_utilization_report(parse_utilization(SAMPLE_2020))
    assert "CRITICAL" not in text
    assert "超限" not in text


def test_unrecognized_header_reports_degraded():
    """有表格行但表头不识别 → 显式报格式不识别,而非静默空结果。"""
    weird = (
        "+------------+------+\n"
        "| Site Type  | Cnt  |\n"
        "+------------+------+\n"
        "| Slice LUTs | 1440 |\n"
        "+------------+------+\n"
    )
    r = parse_utilization(weird)
    assert r.resources == []
    assert "格式不识别" in r.parse_error
    text = format_utilization_report(r)
    assert "[DEGRADED]" in text
    assert "格式不识别" in text


def test_no_table_keeps_plain_empty_message():
    """完全没有表格(如设计未打开)仍走原有"未解析到"提示,不报格式不识别。"""
    r = parse_utilization("这不是一个 utilization 报告")
    assert r.parse_error == ""
    text = format_utilization_report(r)
    assert "未解析到" in text


# -- 表头可识别但行名不在收录集(如 UltraScale 命名)---------------------------- #

SAMPLE_ULTRASCALE = """
1. CLB Logic
------------

+----------------------------+------+-------+-----------+-------+
|          Site Type         | Used | Fixed | Available | Util% |
+----------------------------+------+-------+-----------+-------+
| CLB LUTs                   | 1440 |     0 |    230400 |  0.63 |
| CLB Registers              | 2200 |     0 |    460800 |  0.48 |
+----------------------------+------+-------+-----------+-------+
"""


def test_known_header_zero_rows_sets_parse_error():
    """表头可识别但零行命中(UltraScale 'CLB LUTs' 命名)→ 置 parse_error,
    文案提示可能是非 7 系列命名并建议 run_tcl 看原文,而非误导性"请确认已跑过综合"。"""
    r = parse_utilization(SAMPLE_ULTRASCALE)
    assert r.resources == []
    assert r.parse_error != ""
    assert "非 7 系列" in r.parse_error
    assert "run_tcl" in r.parse_error
    text = format_utilization_report(r)
    assert "[DEGRADED]" in text
    assert "未解析到" not in text  # 不再走误导性的"请确认已跑过综合"提示


def test_two_parse_error_messages_are_distinct():
    """表头不识别 vs 行名不在收录集,两种降级文案区分开。"""
    weird_header = (
        "+------------+------+\n"
        "| Site Type  | Cnt  |\n"
        "+------------+------+\n"
        "| Slice LUTs | 1440 |\n"
        "+------------+------+\n"
    )
    r_header = parse_utilization(weird_header)
    r_rows = parse_utilization(SAMPLE_ULTRASCALE)
    assert "格式不识别" in r_header.parse_error
    assert "非 7 系列" not in r_header.parse_error
    assert "格式不识别" not in r_rows.parse_error


# -- Block RAM 明细(detail=True,C2) ------------------------------------------ #


@pytest.fixture
def fixture_report():
    """真实 2019.1 格式 fixture 的解析结果。"""
    return parse_utilization(_FIXTURE.read_text(encoding="utf-8"))


def test_fixture_parses_core_resources(fixture_report):
    lut = fixture_report.get("Slice LUTs")
    assert lut is not None
    assert lut.used == 1440
    assert lut.available == 20800
    bram = fixture_report.get("Block RAM Tile")
    assert bram is not None
    assert bram.used == 12


def test_parses_bram_detail_rows(fixture_report):
    """Memory 子表的 RAMB36/FIFO* 与 E1 only 等四条子行被解析到 bram_detail。"""
    by_name = {d.name: d for d in fixture_report.bram_detail}
    assert set(by_name) == {"RAMB36/FIFO*", "RAMB36E1 only", "RAMB18", "RAMB18E1 only"}
    assert by_name["RAMB36/FIFO*"].used == 10
    assert by_name["RAMB36/FIFO*"].available == 50
    assert by_name["RAMB18"].used == 4
    assert by_name["RAMB18"].available == 100
    # "RAMB36E1 only" / "RAMB18E1 only" 行 Available/Util% 单元格为空 → 记 0
    assert by_name["RAMB36E1 only"].used == 10
    assert by_name["RAMB36E1 only"].available == 0
    assert by_name["RAMB18E1 only"].used == 4
    assert by_name["RAMB18E1 only"].available == 0


def test_bram_detail_not_in_main_resources(fixture_report):
    """子行不进 resources,主摘要不受影响。"""
    assert fixture_report.get("RAMB36/FIFO*") is None
    assert fixture_report.get("RAMB18") is None


def test_format_detail_false_unchanged(fixture_report):
    """detail=False(含默认值)输出与现状逐字节等价,且无明细段。"""
    base = format_utilization_report(fixture_report)
    assert base == format_utilization_report(fixture_report, detail=False)
    assert "Block RAM 明细" not in base


def test_format_detail_true_appends_section(fixture_report):
    """detail=True 在原输出末尾追加 Block RAM 明细段,前缀部分不变。"""
    base = format_utilization_report(fixture_report)
    text = format_utilization_report(fixture_report, detail=True)
    assert text.startswith(base)
    assert "Block RAM 明细" in text
    assert "RAMB36/FIFO*" in text
    assert "RAMB36E1 only" in text
    assert "RAMB18" in text
    assert "RAMB18E1 only" in text


def test_format_detail_true_without_bram_rows():
    """没有 BRAM 子行时 detail=True 给出明确提示而非空段。"""
    r = parse_utilization(SAMPLE_2020)
    text = format_utilization_report(r, detail=True)
    assert "Block RAM 明细" in text
    assert "未解析到 Block RAM 子表行" in text
