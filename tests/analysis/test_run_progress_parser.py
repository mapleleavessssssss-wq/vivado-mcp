"""run_progress_parser 单元测试。"""

import time

from vivado_mcp.analysis.run_progress_parser import (
    RunProgress,
    format_run_progress,
    parse_run_progress,
)

# 模拟 impl_1 正在运行时 QUERY_RUN_PROGRESS 的输出
RUNNING_SAMPLE = """\
VMCP_RUN:status=route_design Running
VMCP_RUN:progress=60%
VMCP_RUN:dir=C:/proj/basys3.runs/impl_1
VMCP_RUN:log_exists=1
VMCP_RUN:log_size=2048576
VMCP_RUN:log_mtime=1729123456
VMCP_RUN:total_lines=1234
VMCP_RUN_PHASE:120|Starting Placer
VMCP_RUN_PHASE:256|Phase 1 Placer Initialization
VMCP_RUN_PHASE:312|Phase 2 Global Placement
VMCP_RUN_PHASE:389|Phase 3 Detail Placement
VMCP_RUN_PHASE:421|Starting Routing
VMCP_RUN_PHASE:456|Phase 1 Build RT Design
VMCP_RUN_TAIL:1220|Phase 2 Router Initialization
VMCP_RUN_TAIL:1221|Elapsed: 00:05:43
VMCP_RUN_TAIL:1222|INFO: [Route 35-5] Router utilization...
VMCP_RUN_DONE
"""

COMPLETE_SAMPLE = """\
VMCP_RUN:status=write_bitstream Complete!
VMCP_RUN:progress=100%
VMCP_RUN:dir=C:/proj/basys3.runs/impl_1
VMCP_RUN:log_exists=1
VMCP_RUN:log_size=3145728
VMCP_RUN:log_mtime=1729120000
VMCP_RUN:total_lines=2000
VMCP_RUN_PHASE:100|Starting Placer
VMCP_RUN_PHASE:500|Starting Routing
VMCP_RUN_PHASE:1800|Finished Writing Bitstream
VMCP_RUN_DONE
"""

ERROR_SAMPLE = """\
VMCP_RUN:status=place_design ERROR
VMCP_RUN:progress=30%
VMCP_RUN:dir=C:/proj/basys3.runs/impl_1
VMCP_RUN:log_exists=1
VMCP_RUN:log_size=512000
VMCP_RUN:log_mtime=1729123000
VMCP_RUN:total_lines=400
VMCP_RUN_PHASE:120|Starting Placer
VMCP_RUN_PHASE:390|ERROR: [Place 30-58] IO pin constraint conflict
VMCP_RUN_DONE
"""

NOT_FOUND_SAMPLE = "VMCP_RUN_ERROR:run 'impl_42' not found\n"


def test_parses_running_status():
    rp = parse_run_progress(RUNNING_SAMPLE, run_name="impl_1")
    assert rp.found is True
    assert rp.is_running() is True
    assert rp.is_complete() is False
    assert rp.progress == "60%"
    assert rp.log_exists is True
    assert rp.log_size == 2048576
    assert rp.total_lines == 1234


def test_parses_phases_and_tail():
    rp = parse_run_progress(RUNNING_SAMPLE)
    assert len(rp.phases) == 6
    assert rp.phases[0].lineno == 120
    assert rp.phases[-1].text == "Phase 1 Build RT Design"
    assert len(rp.tail) == 3
    assert "Router Initialization" in rp.tail[0].text


def test_current_phase_is_last():
    rp = parse_run_progress(RUNNING_SAMPLE)
    assert "Phase 1 Build RT Design" in rp.current_phase()


def test_complete_sample_detected():
    rp = parse_run_progress(COMPLETE_SAMPLE)
    assert rp.is_complete() is True
    assert rp.is_running() is False
    assert rp.is_error() is False


def test_error_sample_detected():
    rp = parse_run_progress(ERROR_SAMPLE)
    assert rp.is_error() is True
    assert rp.is_running() is False


def test_run_not_found():
    rp = parse_run_progress(NOT_FOUND_SAMPLE, run_name="impl_42")
    assert rp.found is False
    assert "impl_42" in rp.error


def test_format_shows_key_info():
    rp = parse_run_progress(RUNNING_SAMPLE, run_name="impl_1")
    text = format_run_progress(rp)
    assert "impl_1" in text
    assert "运行中" in text
    assert "60%" in text
    assert "Phase" in text


def test_format_error_path():
    rp = parse_run_progress(NOT_FOUND_SAMPLE, run_name="impl_42")
    text = format_run_progress(rp)
    assert "ERROR" in text
    assert "impl_42" in text


def test_elapsed_age_nonnegative_when_mtime_present():
    rp = RunProgress(log_mtime=int(time.time()) - 120)
    assert rp.elapsed_since_last_update() >= 119


def test_elapsed_age_minus_one_when_mtime_missing():
    rp = RunProgress()
    assert rp.elapsed_since_last_update() == -1


def test_empty_input_no_exception():
    rp = parse_run_progress("")
    assert rp.found is False
    assert rp.phases == []
