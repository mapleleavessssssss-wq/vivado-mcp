"""util_parser 单元测试。"""

import pytest

from vivado_mcp.analysis.util_parser import (
    ResourceUsage,
    UtilizationReport,
    format_utilization_report,
    parse_utilization,
)


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
