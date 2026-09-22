"""VCD 解析、条件语义及资源限制测试。"""

import json
from pathlib import Path

import pytest

from vivado_mcp.analysis import vcd_parser as parser
from vivado_mcp.analysis.vcd_parser import VcdError, query_vcd

FIXTURE = Path(__file__).parents[1] / "fixtures" / "waveform_handshake.vcd"


def _vcd(tmp_path, body, header=None):
    path = tmp_path / "input.vcd"
    path.write_text(header or (
        "$timescale 10ps $end\n$scope module top $end\n"
        "$var wire 1 ! clk $end\n$upscope $end\n$enddefinitions $end\n"
    ), encoding="utf-8")
    with path.open("a", encoding="utf-8") as stream:
        stream.write(body)
    return str(path)


def test_discovery_and_timescale():
    result = query_vcd(str(FIXTURE))
    assert result["timescale"] == {"magnitude": 1, "unit": "ns"}
    assert result["hierarchy"] == ["top", "top.dut"]
    assert result["signal_count"] == 6
    assert result["signals"][3]["range"] == "[7:0]"
    assert result["simulation_verdict"] == "not_evaluated"


def test_aliases_initial_values_and_same_timestamp_coalescing():
    result = query_vcd(str(FIXTURE), ["top.data", "top.dut.data_alias"], 10, 20)
    assert result["initial_values"] == {
        "top.data": "00000011", "top.dut.data_alias": "00000011",
    }
    assert result["events"] == [
        {"time": 15, "values": {"top.data": "xxxxxxxx", "top.dut.data_alias": "xxxxxxxx"}},
        {"time": 20, "values": {"top.data": "zzzzzzzz", "top.dut.data_alias": "zzzzzzzz"}},
    ]
    assert result["initial_values_complete"]


@pytest.mark.parametrize(("condition", "times"), [
    ({"op": "equals", "signal": "top.data", "value": "11"}, [10]),
    ({"op": "change", "signal": "top.data"}, [5, 10, 15, 20, 25]),
    ({"op": "unknown", "signal": "top.data"}, [0, 15, 20]),
    ({"op": "all_equals", "values": {"top.valid": "1", "top.ready": "1"}}, [10]),
])
def test_predicates(condition, times):
    result = query_vcd(str(FIXTURE), ["top.data", "top.valid", "top.ready"], condition=condition)
    assert [item["time"] for item in result["matches"]] == times
    assert result["events"] == []


def test_start_between_timestamps():
    result = query_vcd(str(FIXTURE), ["top.data"], 7, 10)
    assert result["initial_values"] == {"top.data": "00000001"}
    assert result["events"] == [{"time": 10, "values": {"top.data": "00000011"}}]


def test_result_limits_for_events_and_discovery():
    assert query_vcd(str(FIXTURE), max_events=2)["result_truncated"]
    result = query_vcd(str(FIXTURE), ["top.clk"], max_events=2)
    assert len(result["events"]) == 2
    assert result["result_truncated"]
    assert not result["scan_truncated"]


def test_scan_limit_does_not_flush_incomplete_timestamp(monkeypatch):
    monkeypatch.setattr(parser, "MAX_TOKENS", 88)
    result = query_vcd(str(FIXTURE), ["top.data"])
    assert result["scan_truncated"]
    assert result["simulation_verdict"] == "not_evaluated"
    assert all(item["time"] < result["last_scanned_time"] for item in result["events"])


@pytest.mark.parametrize("body", [
    "#0 0! #5 1! #3 0!", "#no 0!", "#0 0?", "$dumpvars 0!",
    "$end", "garbage", "#0 b100 !", "#0 $dumpvars #1 1! $end",
])
def test_malformed_data(tmp_path, body):
    with pytest.raises(VcdError):
        query_vcd(_vcd(tmp_path, body), ["top.clk"])


@pytest.mark.parametrize("header", [
    "", "$timescale 7 ns $end", "$scope module top $end $enddefinitions $end",
    "$var wire 1 ! clk $end $var wire 2 ! alias $end $enddefinitions $end",
    "$var wire 1 ! clk $end $var wire 1 ? clk $end $enddefinitions $end",
])
def test_malformed_headers(tmp_path, header):
    path = tmp_path / "bad.vcd"
    path.write_text(header, encoding="utf-8")
    with pytest.raises(VcdError):
        query_vcd(str(path))


@pytest.mark.parametrize("kwargs", [
    {"start_time": -1}, {"start_time": True}, {"end_time": -1}, {"max_events": 0},
    {"signals": ["top.clk"] * 33}, {"signals": ["top.clk", "top.clk"]},
    {"signals": "top.clk"}, {"signals": ["missing"]},
    {"signals": ["top.clk"], "condition": "__import__('os')"},
    {"signals": ["top.clk"], "condition": {"op": []}},
    {"signals": ["top.clk"], "condition": {"op": "eval", "expression": "1"}},
    {"signals": ["top.clk"], "condition": {"op": "equals", "signal": "top.data", "value": "1"}},
    {"condition": {"op": "change", "signal": "top.clk"}},
])
def test_invalid_queries(kwargs):
    with pytest.raises(VcdError):
        query_vcd(str(FIXTURE), **kwargs)


def test_file_limits_and_encoding(tmp_path, monkeypatch):
    path = tmp_path / "large.vcd"
    path.write_bytes(b"a" * 20)
    monkeypatch.setattr(parser, "MAX_FILE_BYTES", 10)
    with pytest.raises(VcdError, match="32 MiB"):
        query_vcd(str(path))
    path.write_bytes(b"\xff")
    with pytest.raises(VcdError, match="UTF-8"):
        query_vcd(str(path))


def test_empty_trace_does_not_become_pass(tmp_path):
    result = query_vcd(_vcd(tmp_path, ""), ["top.clk"])
    assert result["initial_values"] == {"top.clk": None}
    assert not result["data_seen"]
    assert result["warnings"]
    assert result["simulation_verdict"] == "not_evaluated"


def test_unknown_short_vectors_and_redundant_updates(tmp_path):
    path = _vcd(tmp_path, "#0 0! #1 1! 0! #2 0! #3 1!")
    result = query_vcd(path, ["top.clk"])
    assert result["events"] == [{"time": 3, "values": {"top.clk": "1"}}]


def test_missing_timescale_is_explicit(tmp_path):
    path = _vcd(tmp_path, "#0 0!", "$var wire 1 ! clk $end $enddefinitions $end\n")
    result = query_vcd(path, ["clk"])
    assert result["timescale"] is None
    assert result["warnings"]


def test_real_signal_selection_is_rejected(tmp_path):
    path = _vcd(tmp_path, "#0 r1.25 !", "$var real 1 ! v $end $enddefinitions $end\n")
    assert query_vcd(path)["signal_count"] == 1
    with pytest.raises(VcdError, match="real/string"):
        query_vcd(path, ["v"])


def test_output_is_json_serializable():
    assert json.loads(json.dumps(query_vcd(str(FIXTURE))))["success"]


def test_queries_after_trace_do_not_extrapolate_matches():
    result = query_vcd(str(FIXTURE), ["top.clk"], 30, condition={
        "op": "equals", "signal": "top.clk", "value": "1",
    })
    assert result["matches"] == []
    assert result["initial_values"] == {}
    assert not result["initial_values_complete"]
    assert result["last_complete_time"] == 25
    assert result["warnings"]


def test_output_byte_budget_is_enforced(monkeypatch):
    monkeypatch.setattr(parser, "MAX_OUTPUT_BYTES", 1800)
    result = query_vcd(str(FIXTURE), ["top.data", "top.dut.data_alias", "top.clk"])
    assert len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= 1800
    assert result["result_truncated"]


def test_long_token_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(parser, "MAX_TOKEN_LENGTH", 20)
    with pytest.raises(VcdError, match="token"):
        query_vcd(_vcd(tmp_path, "$comment " + "a" * 21 + " $end"))


def test_dump_control_and_comments(tmp_path):
    path = _vcd(tmp_path, "#0 $dumpvars 0! $end #5 $dumpoff x! $end "
                "$comment stopped $end #10 $dumpon 1! $end #15 $dumpall 0! $end")
    result = query_vcd(path, ["top.clk"])
    assert [item["values"]["top.clk"] for item in result["events"]] == ["x", "1", "0"]


def test_scan_cut_inside_header_is_error(monkeypatch):
    monkeypatch.setattr(parser, "MAX_TOKENS", 3)
    with pytest.raises(VcdError, match="头部"):
        query_vcd(str(FIXTURE))


def test_nul_is_not_accepted_as_text(tmp_path):
    with pytest.raises(VcdError, match="NUL"):
        query_vcd(_vcd(tmp_path, "\x00"))


def test_individual_bit_declarations_have_distinct_paths(tmp_path):
    path = _vcd(tmp_path, "#0 0! 1?", "$var wire 1 ! bus [0] $end "
                "$var wire 1 ? bus [1] $end $enddefinitions $end\n")
    result = query_vcd(path, ["bus[0]", "bus[1]"])
    assert result["initial_values"] == {"bus[0]": "0", "bus[1]": "1"}


def test_malformed_real_is_rejected_even_when_not_selected(tmp_path):
    path = _vcd(tmp_path, "#0 rgarbage !", "$var real 1 ! v $end $enddefinitions $end\n")
    with pytest.raises(VcdError, match="real"):
        query_vcd(path)


def test_directory_is_not_read_as_file(tmp_path):
    path = tmp_path / "directory.vcd"
    path.mkdir()
    with pytest.raises(VcdError, match="普通文件"):
        query_vcd(str(path))


def test_transient_events_are_discoverable_but_not_logic_queries(tmp_path):
    path = _vcd(tmp_path, "#0 #5 1! #10 1! #15 1!",
                "$var event 1 ! tick $end $enddefinitions $end\n")
    assert query_vcd(path)["signals"][0]["type"] == "event"
    with pytest.raises(VcdError) as error:
        query_vcd(path, ["tick"], condition={"op": "change", "signal": "tick"})
    assert error.value.code == "unsupported_value_type"


@pytest.mark.parametrize("kind", ["event", "real", "string"])
@pytest.mark.parametrize("reverse", [False, True])
def test_wire_alias_cannot_bypass_unsupported_type(tmp_path, kind, reverse):
    declarations = [f"$var {kind} 1 ! source $end ", "$var wire 1 ! alias $end "]
    if reverse:
        declarations.reverse()
    path = _vcd(tmp_path, "#0 #5 1!", "".join(declarations) + "$enddefinitions $end\n")
    assert query_vcd(path)["signal_count"] == 2
    with pytest.raises(VcdError) as error:
        query_vcd(path, ["alias"])
    assert error.value.code == "unsupported_value_type"


@pytest.mark.parametrize("bit_range", ["[ 7 : 0 ]", "[7 :0]", "[ 7:0 ]"])
def test_spaced_vector_ranges_are_supported(tmp_path, bit_range):
    path = _vcd(tmp_path, "#0 b1 ! #5 b10 !",
                f"$var wire 8 ! data {bit_range} $end $enddefinitions $end\n")
    result = query_vcd(path, ["data"])
    assert result["signals"][0]["range"] == "[7:0]"
    assert result["initial_values"] == {"data": "00000001"}
    assert result["events"] == [{"time": 5, "values": {"data": "00000010"}}]


def test_spaced_invalid_vector_range_is_still_rejected(tmp_path):
    path = _vcd(tmp_path, "", "$var wire 8 ! data [ 7 : ] $end $enddefinitions $end\n")
    with pytest.raises(VcdError, match="位范围"):
        query_vcd(path)
