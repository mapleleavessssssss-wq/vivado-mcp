"""基于任务独占双时钟设计的 Vivado 2019.1 原始报告测试。"""

import hashlib
import json
from pathlib import Path

import pytest

from vivado_mcp.analysis.cdc_parser import (
    MAX_CDC_BYTES,
    MAX_CDC_OUTPUT_BYTES,
    cdc_error,
    parse_cdc_report,
    serialize_cdc_report,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"


def fixture(name="details"):
    return (FIXTURES / f"cdc_{name}_2019.txt").read_text(encoding="utf-8")


def test_real_details_counts_checks_once_and_keeps_bus_grouping():
    raw = fixture()
    data = parse_cdc_report(raw)
    assert data["schema_version"] == 1
    assert data["kind"] == "vivado_cdc"
    assert data["parse_status"] == "ok"
    assert data["provenance"]["sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert data["summary"]["reported_checks"] == 3
    assert data["summary"]["count_basis"] == "rule_summary_checks"
    assert data["summary"]["by_severity"] == {"Critical": 2, "Info": 1}
    assert data["detail_rows_observed"] == 3
    assert len(data["details"]) == 3
    assert data["details"][-1]["source"] == "source_reg[3:1]/C"
    pair = data["clock_pairs"][0]
    assert pair["source_clock"] == "clk_a"
    assert pair["destination_clock"] == "clk_b"
    assert pair["by_rule"] == {"CDC-1": 1, "CDC-3": 1, "CDC-4": 1}
    assert data["constraints_verified"] is False
    assert data["verdict"]["signoff"] is False


@pytest.mark.parametrize("limit", [0, 1, 2, 3, 100])
def test_detail_limit_does_not_truncate_aggregation(limit):
    data = parse_cdc_report(fixture(), limit)
    assert len(data["details"]) == min(3, limit)
    assert data["summary"]["reported_checks"] == 3
    assert data["clock_pairs"][0]["detail_checks"] == 3
    assert data["details_truncated"] is (limit < 3)


def test_real_no_clocks_safely_timed_is_unverified_not_zero_checks():
    raw = fixture("no_clocks")
    assert "All paths are Safely Timed." in raw
    data = parse_cdc_report(raw)
    assert data["parse_status"] == "partial"
    assert data["summary"]["reported_checks"] is None
    assert data["verdict"]["status"] == "unverified"
    assert data["verdict"]["signoff"] is False


def test_real_shown_waiver_is_not_counted_in_unwaived_rule_total():
    data = parse_cdc_report(fixture("waived"))
    assert data["summary"]["reported_checks"] == 2
    assert data["summary"]["by_severity"] == {"Critical": 1, "Info": 1}
    assert data["summary"]["waived_endpoints_by_rule"] == {"CDC-1": 1}
    assert data["detail_rows_observed"] == 3
    assert data["waived_detail_rows"] == 1
    assert data["details"][1]["waived"] is True
    assert data["clock_pairs"][0]["waived_detail_checks"] == 1
    assert data["parse_status"] == "partial"
    assert not any("计数不一致" in message for message in data["diagnostics"])


def test_real_hidden_waiver_retains_waiver_summary():
    data = parse_cdc_report(fixture("waiver_hidden"))
    assert data["summary"]["reported_checks"] == 2
    assert data["summary"]["waived_endpoints_by_rule"] == {"CDC-1": 1}
    assert data["detail_rows_observed"] == 2
    assert data["waived_detail_rows"] == 0
    assert data["waiver_visibility"] == "may_be_hidden"
    assert data["parse_status"] == "partial"
    assert data["verdict"]["signoff"] is False


def test_endpoint_summary_is_not_treated_as_rule_check_count():
    data = parse_cdc_report(fixture("summary_only"))
    assert data["parse_status"] == "partial"
    assert data["summary"]["reported_checks"] is None


@pytest.mark.parametrize("raw,status", [
    ("", "unrecognized"), ("garbage", "unrecognized"),
    ("ERROR: no design", "error"), ("CDC Report\n\x00", "error"),
])
def test_empty_invalid_or_error_reports_cannot_pass(raw, status):
    data = parse_cdc_report(raw)
    assert data["parse_status"] == status
    assert data["success"] is False
    assert data["verdict"]["signoff"] is False
    assert data["verdict"]["status"] == "unverified"


@pytest.mark.parametrize("mutation", [
    lambda s: s.rsplit("\n", 2)[0],
    lambda s: s.replace("Source Clock: clk_a", ""),
    lambda s: s.replace("Destination Clock: clk_b", ""),
    lambda s: s.replace("CDC Type: No Common Primary Clock", ""),
    lambda s: s + "\n[output truncated]\n",
    lambda s: s + s,
])
def test_truncated_missing_context_and_repeated_reports_are_partial(mutation):
    data = parse_cdc_report(mutation(fixture()))
    assert data["parse_status"] == "partial"
    assert data["verdict"]["signoff"] is False
    assert data["diagnostics"]


def test_rule_summary_without_details_is_partial_with_known_counts():
    raw = fixture().split("Source Clock:")[0]
    data = parse_cdc_report(raw)
    assert data["summary"]["reported_checks"] == 3
    assert data["details"] == []
    assert data["parse_status"] == "partial"


def test_details_without_summary_are_explicitly_lower_bound():
    raw = fixture()
    start = raw.index("ID     Severity  Count")
    end = raw.index("Source Clock:")
    data = parse_cdc_report(raw[:start] + raw[end:])
    assert data["summary"]["count_basis"] == "detail_rows_only"
    assert data["summary"]["reported_checks"] == 3
    assert data["parse_status"] == "partial"


def test_multiple_clock_pairs_aggregate_separately_without_double_count():
    raw = fixture()
    detail = raw[raw.index("Source Clock:"):]
    # 两份真实明细块分配到两个独立时钟对，总摘要每种规则增加为 2。
    raw = raw[:raw.index("Source Clock:")].replace("      1  ", "      2  ")
    raw += detail + "\n" + detail.replace("clk_a", "clk_c").replace("clk_b", "clk_d")
    data = parse_cdc_report(raw)
    assert data["parse_status"] == "ok"
    assert data["summary"]["reported_checks"] == 6
    assert len(data["clock_pairs"]) == 2
    assert [p["detail_checks"] for p in data["clock_pairs"]] == [3, 3]


@pytest.mark.parametrize("value", [True, False, -1, 501, 1.5, "2", None])
def test_invalid_detail_limit_is_rejected(value):
    with pytest.raises(ValueError, match="max_details"):
        parse_cdc_report(fixture(), value)


def test_oversized_input_rejected_before_parsing():
    with pytest.raises(ValueError, match="16 MiB"):
        parse_cdc_report("a" * (MAX_CDC_BYTES + 1))


def test_output_byte_bound_is_explicit_and_preserves_aggregate():
    data = parse_cdc_report(fixture())
    data["details"] = [dict(data["details"][0], description="大" * 3000) for _ in range(500)]
    text = serialize_cdc_report(data)
    assert len(text.encode()) <= MAX_CDC_OUTPUT_BYTES
    result = json.loads(text)
    assert result["details_truncated"] is True
    assert result["summary"]["reported_checks"] == 3
    assert result["verdict"]["signoff"] is False


def test_error_schema_does_not_resolve_invalid_paths():
    data = cdc_error("failed", report_file="bad\x00path", source="report_file")
    assert json.loads(serialize_cdc_report(data))["parse_status"] == "error"


def test_field_and_group_bounds_are_reported():
    raw = fixture().replace("Source Clock: clk_a", "Source Clock: " + "a" * 900)
    data = parse_cdc_report(raw)
    assert data["fields_truncated"] is True
    assert len(data["details"][0]["source_clock"]) == 512
    detail = fixture()[fixture().index("Source Clock:"):]
    many_pairs = "CDC Report\n" + "\n".join(
        detail.replace("clk_a", f"source_clk_{index}") for index in range(140)
    )
    data = parse_cdc_report(many_pairs, 0)
    assert data["detail_rows_observed"] == 420
    assert len(data["clock_pairs"]) == 128
    assert data["groups_truncated"] is True
    assert data["parse_status"] == "partial"


def test_missing_report_metadata_is_partial():
    raw = fixture()
    data = parse_cdc_report(raw[raw.index("CDC Report"):])
    assert data["parse_status"] == "partial"
    assert data["summary"]["reported_checks"] == 3
    assert any("上下文" in note for note in data["diagnostics"])


def test_malformed_separator_column_bomb_is_rejected():
    raw = "CDC Report\nID     Severity  Count  Description\n" + "- " * 100000
    data = parse_cdc_report(raw)
    assert data["parse_status"] == "partial"
    assert data["details"] == []
    assert any("列数异常" in note for note in data["diagnostics"])
