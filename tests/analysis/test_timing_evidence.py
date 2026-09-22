"""离线时序证据：真实摘要结构、部分数据和不可比基线。"""

import copy
import json
from pathlib import Path

import pytest

from vivado_mcp.analysis.timing_evidence import (
    compare_timing_baseline,
    make_timing_evidence,
    physical_design_stage,
    read_bounded_text,
    report_context,
    summary_metrics,
)
from vivado_mcp.analysis.timing_parser import parse_timing_summary

_FIXTURE = (Path(__file__).parents[1] / "fixtures/sample_report_timing.txt").read_text(
    encoding="utf-8",
)


def report(row="0.234 0.000 0 150 0.045 0.000 0 150 0.300 0.000 0 2", state="Fully Routed"):
    """保留 Vivado 总表表头，并追加完整版本的 pulse-width 列。"""
    header = next(line for line in _FIXTURE.splitlines() if "WNS(ns)" in line)
    return (
        f"Design : top\nDevice : xc7a35tcpg236-1\nDesign State : {state}\n"
        f"| Design Timing Summary\n{header} WPWS(ns) TPWS(ns) "
        f"TPWS Failing Endpoints TPWS Total Endpoints\n--------\n{row}\n"
    )


def evidence(raw=None):
    raw = raw or report()
    return make_timing_evidence(raw, parse_timing_summary(raw), report_file="timing.rpt")


def test_complete_metrics_include_hold_and_pulse_width():
    data = evidence()
    assert data["schema_version"] == 1
    assert data["metrics"]["hold"]["total_endpoints"] == 150
    assert data["metrics"]["pulse_width"]["worst_slack_ns"] == 0.3
    assert data["stage"] == "post-route"
    assert data["verdict"]["status"] == "met_observed_checks"
    assert data["verdict"]["signoff"] is False
    assert len(data["provenance"]["sha256"]) == 64


def test_pulse_width_only_failure_is_not_pass():
    raw = report("0.1 0 0 5 0.1 0 0 5 -0.2 -0.2 1 3")
    data = evidence(raw)
    assert data["verdict"]["status"] == "violated"
    assert parse_timing_summary(raw).summary.timing_met is False


def test_hold_endpoint_failure_is_not_pass_even_when_rounded_slack_zero():
    raw = report("0.1 0 0 5 0.000 0.000 1 5 0.2 0 0 3")
    assert evidence(raw)["verdict"]["status"] == "violated"
    assert parse_timing_summary(raw).summary.timing_met is False


def test_partial_na_preserves_real_hold_violation():
    data = evidence(report("NA NA NA NA -0.2 -0.4 2 5 NA NA NA NA"))
    assert data["parse_status"] == "partial"
    assert data["metrics"]["setup"]["worst_slack_ns"] is None
    assert data["metrics"]["hold"]["worst_slack_ns"] == -0.2
    assert data["verdict"]["status"] == "violated"


@pytest.mark.parametrize("row", [
    "0 0 0 0 0 0 0 0", "NA NA NA NA NA NA NA NA",
])
def test_empty_design_is_not_timing_success(row):
    data = evidence(report(row))
    assert data["parse_status"] == "no_timing_data"
    assert data["verdict"]["status"] == "unavailable"
    assert parse_timing_summary(report(row)).summary.parse_status == "no_timing_data"


@pytest.mark.parametrize("row", [
    "nan 0 0 5 0 0 0 5", "inf 0 0 5 0 0 0 5", "0 0 -1 5 0 0 0 5",
    "0 0 6 5 0 0 0 5", "0 0 0 5 0 0 0.5 5", "0 1 0 5 0 0 0 5",
])
def test_invalid_numbers_cannot_pass(row):
    data = evidence(report(row))
    assert data["parse_status"] == "invalid"
    assert data["verdict"]["status"] == "unavailable"
    json.dumps(data, allow_nan=False)


def test_legacy_report_absent_pulse_is_null_not_zero():
    data = evidence(_FIXTURE)
    assert data["stage"] == "unknown"
    assert data["context"]["design"] is None
    assert all(v is None for v in data["metrics"]["pulse_width"].values())


@pytest.mark.parametrize("state,expected", [
    ("Synthesized", "post-synth"), ("Fully Placed", "post-place"),
    ("Fully Routed", "post-route"), ("Partially Routed", "unknown"),
])
def test_only_explicit_report_design_state_is_used(state, expected):
    raw = report(state=state)
    assert report_context(raw)[0] == expected
    assert report_context("VMCP_STAGE:stage=post-route\n" + _FIXTURE)[0] == "unknown"


def route_fixture(name):
    text = (Path(__file__).parents[1] / f"fixtures/timing_route_status_{name}_2019.txt").read_text()
    return "\n".join("VMCP_TIMING_ROUTE:" + line for line in text.splitlines())


@pytest.mark.parametrize("name,stage", [("synth", "post-synth"), ("routed", "post-route")])
def test_live_stage_from_real_vivado_2019_route_counts(name, stage):
    physical = route_fixture(name)
    assert physical_design_stage(physical)[0] == stage
    result = make_timing_evidence(
        _FIXTURE, parse_timing_summary(_FIXTURE), live_stage_output=physical,
    )
    assert result["stage"] == stage
    assert result["provenance"]["stage_source"] == "live_route_status"
    offline = make_timing_evidence(
        _FIXTURE, parse_timing_summary(_FIXTURE), report_file="report.rpt",
        live_stage_output=physical,
    )
    assert offline["stage"] == "unknown"


@pytest.mark.parametrize("old,new", [
    ("fully routed nets", "unknown category"),
    ("logical nets.......................... :          31", "logical nets : 32"),
    ("routing errors.......... :           0", "routing errors.......... :           1"),
    ("routable nets..................... :          21", "routable nets : 0"),
])
def test_incomplete_or_conflicting_physical_evidence_stays_unknown(old, new):
    raw = route_fixture("routed").replace(old, new)
    assert physical_design_stage(raw)[0] == "unknown"


def test_route_query_error_and_run_status_cannot_prove_current_stage():
    assert physical_design_stage("VMCP_STAGE:stage=post-route")[0] == "unknown"
    raw = route_fixture("routed") + "\nVMCP_TIMING_ROUTE_ERROR:failed"
    assert physical_design_stage(raw)[0] == "unknown"


def test_clock_row_cannot_substitute_for_design_summary():
    raw = _FIXTURE.split("| Intra Clock Table", 1)[1]
    assert summary_metrics(raw)[1] == "unrecognized"


def test_error_with_stale_table_is_invalid():
    assert evidence("ERROR: failed\n" + report())["parse_status"] == "invalid"


def save_baseline(tmp_path, value):
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return str(path)


def test_matching_context_only_gives_observational_deltas(tmp_path):
    before, after = evidence(), evidence(report("0.5 0 0 150 0.1 0 0 150 0.3 0 0 2"))
    result = compare_timing_baseline(after, save_baseline(tmp_path, before))
    assert result["status"] == "observational"
    assert result["authoritative"] is False
    assert result["deltas"]["setup"]["worst_slack_ns"]["delta"] == pytest.approx(0.266)
    assert "约束" in result["reasons"][0]


@pytest.mark.parametrize("field,value", [
    ("design", "different"), ("design", None), ("device", "different"),
    ("stage", "unknown"), ("stage", "post-synth"),
])
def test_unknown_or_mismatched_context_is_not_compared(tmp_path, field, value):
    before, after = evidence(), evidence()
    (before if field == "stage" else before["context"])[field] = value
    result = compare_timing_baseline(after, save_baseline(tmp_path, before))
    assert result["status"] == "incomparable"
    assert result["deltas"] == {}


def test_constraint_mismatch_blocks_even_observation(tmp_path):
    before, after = evidence(), evidence()
    before["context"]["constraint_fingerprint"] = "old"
    after["context"]["constraint_fingerprint"] = "new"
    result = compare_timing_baseline(after, save_baseline(tmp_path, before))
    assert result["status"] == "incomparable"


@pytest.mark.parametrize("invalid", [[], {}, {"schema_version": 3}, "text"])
def test_bad_baseline_schema_is_explicit_error(tmp_path, invalid):
    current = evidence()
    original = copy.deepcopy(current)
    result = compare_timing_baseline(current, save_baseline(tmp_path, invalid))
    assert result["status"] == "error"
    assert current == original


@pytest.mark.parametrize("invalid", [True, float("nan"), "0", float("inf")])
def test_bad_baseline_metric_is_explicit_error(tmp_path, invalid):
    baseline = evidence()
    baseline["metrics"]["setup"]["worst_slack_ns"] = invalid
    result = compare_timing_baseline(evidence(), save_baseline(tmp_path, baseline))
    assert result["status"] == "error"


def test_bounded_read_does_not_accept_large_or_binary_data(tmp_path):
    path = tmp_path / "report.txt"
    path.write_text("a" * 12)
    with pytest.raises(ValueError, match="超过"):
        read_bounded_text(str(path), 10)
    path.write_bytes(b"abc\x00def")
    with pytest.raises(ValueError, match="NUL"):
        read_bounded_text(str(path), 10)


def test_huge_integer_and_deep_json_are_controlled_errors(tmp_path):
    huge = 10**400
    assert evidence(report(f"0 0 0 {huge} 0 0 0 5"))["parse_status"] == "invalid"
    baseline = evidence()
    baseline["metrics"]["setup"]["total_endpoints"] = huge
    result = compare_timing_baseline(evidence(), save_baseline(tmp_path, baseline))
    assert result["status"] == "error"
    path = tmp_path / "deep.json"
    path.write_text("[" * 12000 + "0" + "]" * 12000)
    assert compare_timing_baseline(evidence(), str(path))["status"] == "error"
