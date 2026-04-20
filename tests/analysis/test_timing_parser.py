"""timing_parser.py 单元测试。

重点覆盖：
- Design Timing Summary 表格解析（WNS / TNS / WHS / THS / 端点数）
- 单条路径块解析（slack / source / dest / 时钟域 / 类型 / requirement / delay）
- 多路径场景
- timing_met 布尔判定
- to_dict JSON 可序列化
- 空输入容错
- format_timing_report 人类可读格式
"""

import json
from pathlib import Path

import pytest

from vivado_mcp.analysis.timing_parser import (
    TimingReport,
    TimingSummary,
    ViolatingPath,
    analyze_path_pattern,
    derive_stage_warning,
    format_timing_report,
    parse_design_stage,
    parse_timing_summary,
    parse_violating_paths,
)

# ====================================================================== #
#  Fixture 加载
# ====================================================================== #

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures"
_SAMPLE_TIMING = _FIXTURE_DIR / "sample_report_timing.txt"
_SAMPLE_VIOLATING = _FIXTURE_DIR / "sample_violating_paths.txt"


@pytest.fixture
def sample_text() -> str:
    """读取 sample_report_timing.txt fixture 文件。"""
    return _SAMPLE_TIMING.read_text(encoding="utf-8")


@pytest.fixture
def report(sample_text: str) -> TimingReport:
    """解析 fixture 并返回 TimingReport。"""
    return parse_timing_summary(sample_text)


# ====================================================================== #
#  Design Timing Summary 表格解析
# ====================================================================== #


class TestParseSummaryValues:
    """Design Timing Summary 数值解析测试。"""

    def test_parse_summary_values(self, report: TimingReport):
        """WNS / TNS / WHS / THS 数值正确提取。"""
        s = report.summary
        assert s.wns == pytest.approx(0.234)
        assert s.tns == pytest.approx(0.0)
        assert s.whs == pytest.approx(0.045)
        assert s.ths == pytest.approx(0.0)

    def test_failing_endpoints(self, report: TimingReport):
        """failing_endpoints 与 total_endpoints 正确提取。"""
        s = report.summary
        assert s.failing_endpoints == 0
        assert s.total_endpoints == 150

    def test_timing_met_true(self, report: TimingReport):
        """WNS >= 0 且 WHS >= 0 时 timing_met 为 True。"""
        assert report.summary.timing_met is True

    def test_timing_met_false(self):
        """构造 WNS 为负值的输入，timing_met 应为 False。"""
        violated_text = """\
------------------------------------------------------------------------------------
| Design Timing Summary
| ---------------------
------------------------------------------------------------------------------------

    WNS(ns)      TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints      WHS(ns)      THS(ns)  THS Failing Endpoints  THS Total Endpoints
    -------      -------  ---------------------  -------------------      -------      -------  ---------------------  -------------------
     -0.150       -1.200                      5                  200        0.030        0.000                      0                  200
"""  # noqa: E501
        r = parse_timing_summary(violated_text)
        assert r.summary.timing_met is False
        assert r.summary.wns == pytest.approx(-0.150)
        assert r.summary.tns == pytest.approx(-1.200)
        assert r.summary.failing_endpoints == 5


# ====================================================================== #
#  路径块解析
# ====================================================================== #


class TestParseFirstPath:
    """第一条路径块（userclk2 Setup）解析测试。"""

    def test_parse_first_path(self, report: TimingReport):
        """第一条路径 slack / met / path_type 正确。"""
        assert len(report.paths) >= 1
        p = report.paths[0]
        assert p.slack_ns == pytest.approx(0.234)
        assert p.met is True
        assert p.path_type == "Setup"

    def test_parse_path_source(self, report: TimingReport):
        """source 字段包含 reg_a/C。"""
        p = report.paths[0]
        assert "reg_a/C" in p.source

    def test_parse_path_destination(self, report: TimingReport):
        """destination 字段包含 reg_b/D。"""
        p = report.paths[0]
        assert "reg_b/D" in p.destination

    def test_parse_path_group(self, report: TimingReport):
        """path_group 为 userclk2。"""
        p = report.paths[0]
        assert p.path_group == "userclk2"

    def test_parse_requirement(self, report: TimingReport):
        """requirement_ns 为 4.0。"""
        p = report.paths[0]
        assert p.requirement_ns == pytest.approx(4.0)

    def test_parse_data_delay(self, report: TimingReport):
        """data_delay_ns 为 3.766。"""
        p = report.paths[0]
        assert p.data_delay_ns == pytest.approx(3.766)


class TestParseMultiplePaths:
    """多路径场景测试。"""

    def test_parse_multiple_paths(self, report: TimingReport):
        """fixture 中含 2 条路径，均应被解析。"""
        assert len(report.paths) == 2

    def test_second_path_values(self, report: TimingReport):
        """第二条路径（sys_clk Setup）数值验证。"""
        p = report.paths[1]
        assert p.slack_ns == pytest.approx(2.456)
        assert p.met is True
        assert p.path_group == "sys_clk"
        assert p.path_type == "Setup"
        assert p.requirement_ns == pytest.approx(10.0)
        assert p.data_delay_ns == pytest.approx(7.544)

    def test_second_path_source(self, report: TimingReport):
        """第二条路径 source 包含 CLKOUT0。"""
        p = report.paths[1]
        assert "CLKOUT0" in p.source

    def test_second_path_destination(self, report: TimingReport):
        """第二条路径 destination 包含 sync_reg。"""
        p = report.paths[1]
        assert "sync_reg" in p.destination


# ====================================================================== #
#  序列化
# ====================================================================== #


class TestSerialization:
    """to_dict / JSON 序列化测试。"""

    def test_to_dict_serializable(self, report: TimingReport):
        """to_dict() 返回值可被 json.dumps 序列化。"""
        d = report.to_dict()
        serialized = json.dumps(d)
        assert isinstance(serialized, str)
        # 反序列化校验关键字段
        loaded = json.loads(serialized)
        assert loaded["summary"]["wns"] == pytest.approx(0.234)
        assert len(loaded["paths"]) == 2

    def test_to_dict_structure(self, report: TimingReport):
        """to_dict() 结构正确：顶层含 summary 和 paths。"""
        d = report.to_dict()
        assert "summary" in d
        assert "paths" in d
        assert isinstance(d["summary"], dict)
        assert isinstance(d["paths"], list)
        # summary 字段完整性
        for key in ("wns", "tns", "whs", "ths", "failing_endpoints",
                     "total_endpoints", "timing_met"):
            assert key in d["summary"]


# ====================================================================== #
#  边界情况
# ====================================================================== #


class TestEdgeCases:
    """空输入与异常输入容错测试。"""

    def test_empty_input(self):
        """空字符串不抛异常，返回全零默认报告。"""
        r = parse_timing_summary("")
        assert r.summary.wns == 0.0
        assert r.summary.tns == 0.0
        assert r.summary.whs == 0.0
        assert r.summary.ths == 0.0
        assert r.summary.failing_endpoints == 0
        assert r.summary.total_endpoints == 0
        assert r.summary.timing_met is True
        assert r.paths == []

    def test_garbage_input(self):
        """无法识别的随机文本不抛异常。"""
        r = parse_timing_summary("这不是一个 Vivado 报告\n随机文字 123")
        assert r.summary.wns == 0.0
        assert r.paths == []

    def test_summary_only_no_paths(self):
        """只有 Summary 表格没有 Slack 路径块时，paths 为空列表。"""
        text = """\
    WNS(ns)      TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints      WHS(ns)      THS(ns)  THS Failing Endpoints  THS Total Endpoints
    -------      -------  ---------------------  -------------------      -------      -------  ---------------------  -------------------
      1.000        0.000                      0                   50        0.500        0.000                      0                   50
"""  # noqa: E501
        r = parse_timing_summary(text)
        assert r.summary.wns == pytest.approx(1.0)
        assert r.summary.total_endpoints == 50
        assert r.paths == []


# ====================================================================== #
#  格式化输出
# ====================================================================== #


class TestFormatTimingReport:
    """format_timing_report 人类可读格式测试。"""

    def test_format_timing_report(self, report: TimingReport):
        """格式化输出包含关键信息。"""
        text = format_timing_report(report)
        assert "时序分析摘要" in text
        assert "PASS" in text
        assert "WNS" in text or "0.234" in text
        assert "userclk2" in text
        assert "关键路径" in text

    def test_format_violation_highlighted(self):
        """违例报告中出现 [!] 警告标记。"""
        violated_summary = TimingSummary(
            wns=-0.5, tns=-2.0, whs=0.1, ths=0.0,
            failing_endpoints=3, total_endpoints=100, timing_met=False,
        )
        r = TimingReport(summary=violated_summary, paths=[])
        text = format_timing_report(r)
        assert "FAIL" in text
        assert "[!]" in text
        assert "Setup 违例" in text

    def test_format_hold_violation(self):
        """Hold 违例时显示 Hold 警告。"""
        violated_summary = TimingSummary(
            wns=0.5, tns=0.0, whs=-0.1, ths=-0.3,
            failing_endpoints=0, total_endpoints=100, timing_met=False,
        )
        r = TimingReport(summary=violated_summary, paths=[])
        text = format_timing_report(r)
        assert "Hold 违例" in text

    def test_format_path_details(self, report: TimingReport):
        """格式化输出包含路径的 source -> destination 信息。"""
        text = format_timing_report(report)
        assert "reg_a/C" in text
        assert "reg_b/D" in text
        assert "sys_clk" in text


# ====================================================================== #
#  Bug 2 修复测试:设计阶段感知
# ====================================================================== #


class TestParseDesignStage:
    def test_parses_post_route(self):
        raw = ("VMCP_STAGE:stage=post-route|synth_status=synth_design Complete!"
               "|impl_status=route_design Complete!")
        stage, synth, impl = parse_design_stage(raw)
        assert stage == "post-route"
        assert "synth_design Complete" in synth
        assert "route_design Complete" in impl

    def test_parses_post_synth_with_impl_error(self):
        raw = ("VMCP_STAGE:stage=post-synth|synth_status=synth_design Complete!"
               "|impl_status=place_design ERROR")
        stage, synth, impl = parse_design_stage(raw)
        assert stage == "post-synth"
        assert "ERROR" in impl

    def test_unknown_on_empty(self):
        stage, synth, impl = parse_design_stage("")
        assert stage == "unknown"
        assert synth == ""
        assert impl == ""


class TestDeriveStageWarning:
    def test_post_route_no_warning(self):
        detail, warn = derive_stage_warning(
            "post-route", "synth_design Complete!", "route_design Complete!"
        )
        assert warn == ""
        assert "route_design Complete" in detail

    def test_impl_error_triggers_strong_warning(self):
        """Bug 2 核心场景:impl 失败但还有 synth 估算时序。"""
        detail, warn = derive_stage_warning(
            "post-synth", "synth_design Complete!", "place_design ERROR"
        )
        assert "不要据此判断能否烧板" in warn
        assert "impl_1 失败" in warn
        assert "ERROR" in detail

    def test_post_synth_has_estimate_warning(self):
        detail, warn = derive_stage_warning(
            "post-synth", "synth_design Complete!", "Not started"
        )
        assert "综合估算" in warn
        assert "不要作为最终判据" in warn


class TestFormatWithStage:
    def test_format_shows_post_route(self):
        summary = TimingSummary(
            wns=0.5, tns=0.0, whs=0.1, ths=0.0,
            failing_endpoints=0, total_endpoints=100, timing_met=True,
        )
        report = TimingReport(
            summary=summary, paths=[],
            source_stage="post-route",
            source_detail="impl_1 状态=route_design Complete!",
            stage_warning="",
        )
        text = format_timing_report(report)
        assert "数据来源: post-route" in text
        assert "PASS" in text

    def test_format_shows_warning_when_impl_failed(self):
        """Bug 2 的核心保障:impl 失败时必须有醒目警告。"""
        summary = TimingSummary(
            wns=5.8, tns=0.0, whs=0.1, ths=0.0,
            failing_endpoints=0, total_endpoints=128, timing_met=True,
        )
        report = TimingReport(
            summary=summary, paths=[],
            source_stage="post-synth",
            source_detail="impl_1=place_design ERROR",
            stage_warning="注意: impl_1 失败,下面的时序是综合估算,不等同于布线后的最终结果。",
        )
        text = format_timing_report(report)
        assert "数据来源: post-synth" in text
        # 关键:用户必须看到警告
        assert "[!]" in text
        assert "impl_1 失败" in text


# ====================================================================== #
#  违例路径解析与模式嗅探
# ====================================================================== #


@pytest.fixture
def violating_text() -> str:
    """读取 sample_violating_paths.txt fixture。"""
    return _SAMPLE_VIOLATING.read_text(encoding="utf-8")


@pytest.fixture
def violating_paths(violating_text: str) -> list[ViolatingPath]:
    """解析 fixture 得到的违例路径列表(已按 slack 升序)。"""
    return parse_violating_paths(violating_text)


class TestViolatingPath:
    """parse_violating_paths 解析测试。

    fixture 含 5 条违例路径(4 setup + 1 hold),每条覆盖一种典型模式。
    """

    def test_parse_returns_five_paths(self, violating_paths: list[ViolatingPath]):
        """4 setup + 1 hold 共 5 条都能解析出来。"""
        assert len(violating_paths) == 5

    def test_parse_sorted_by_slack_ascending(self, violating_paths: list[ViolatingPath]):
        """按 slack 升序:最差(最负)在前。"""
        slacks = [p.slack for p in violating_paths]
        assert slacks == sorted(slacks)
        assert slacks[0] == pytest.approx(-1.234)

    def test_parse_setup_path_fields(self, violating_paths: list[ViolatingPath]):
        """第一条 setup 路径字段完整解析。"""
        p = violating_paths[0]  # CDC 路径
        assert p.type == "setup"
        assert p.slack == pytest.approx(-1.234)
        assert "result_reg[31]/C" in p.startpoint
        assert "wr_data_reg[31]/D" in p.endpoint
        assert p.start_clock == "sys_clk_100mhz"
        assert p.end_clock == "dma_clk_200mhz"
        assert p.logic_delay == pytest.approx(1.500)
        assert p.route_delay == pytest.approx(4.420)
        assert p.levels == 4
        assert p.clock_skew == pytest.approx(-0.314)

    def test_parse_hold_path(self, violating_paths: list[ViolatingPath]):
        """hold 违例路径被识别为 type='hold'。"""
        hold_paths = [p for p in violating_paths if p.type == "hold"]
        assert len(hold_paths) == 1
        h = hold_paths[0]
        assert h.slack == pytest.approx(-0.080)
        assert h.clock_skew == pytest.approx(-0.500)

    def test_parse_empty_input(self):
        """空输入不抛异常。"""
        assert parse_violating_paths("") == []

    def test_parse_ignores_met_paths(self):
        """即使输出里含 Slack (MET),也不应出现在违例列表里。"""
        raw = """\
VMCP_PATH_START:type=setup
Slack (MET) :             0.500ns  (required time - arrival time)
  Source:                 reg_a/C
                            (rising edge-triggered cell FDRE clocked by clk1)
  Destination:            reg_b/D
                            (rising edge-triggered cell FDRE clocked by clk1)
  Data Path Delay:        2.000ns  (logic 0.500ns (25.0%)  route 1.500ns (75.0%))
  Logic Levels:           2  (LUT2=1)
  Clock Path Skew:        0.000ns (DCD - SCD + CPR)
VMCP_PATH_END:type=setup
VMCP_PATH_DONE
"""
        assert parse_violating_paths(raw) == []

    def test_parse_handles_tcl_error(self):
        """Tcl 层报错(VMCP_PATH_ERROR)不会抛异常,返回空列表。"""
        raw = (
            "VMCP_PATH_START:type=setup\n"
            "VMCP_PATH_ERROR:setup|no_design_loaded\n"
            "VMCP_PATH_END:type=setup\n"
            "VMCP_PATH_DONE\n"
        )
        assert parse_violating_paths(raw) == []


class TestAnalyzePath:
    """analyze_path_pattern 模式嗅探测试。

    用 fixture 中 5 条路径分别验证 CDC / LONG_COMBO / IO_UNREGISTERED /
    HIGH_FANOUT / UNKNOWN 这 5 种 tag 都能命中,并且中文建议具体(含路径名)。
    """

    def _by_slack(
        self, paths: list[ViolatingPath], slack: float
    ) -> ViolatingPath:
        """按 slack 近似匹配取路径(fixture 里每条 slack 唯一)。"""
        for p in paths:
            if abs(p.slack - slack) < 1e-6:
                return p
        raise AssertionError(f"fixture 中未找到 slack={slack} 的路径")

    def test_cdc_detected(self, violating_paths: list[ViolatingPath]):
        """路径 1: start_clock != end_clock → CDC。"""
        p = self._by_slack(violating_paths, -1.234)
        tag, advice = analyze_path_pattern(p)
        assert tag == "CDC"
        # 建议要具体:提到两个时钟名和 set_false_path
        assert "sys_clk_100mhz" in advice
        assert "dma_clk_200mhz" in advice
        assert "set_false_path" in advice or "同步器" in advice

    def test_long_combo_detected(self, violating_paths: list[ViolatingPath]):
        """路径 2: levels=18 > 15,logic >= 2*route → LONG_COMBO。"""
        p = self._by_slack(violating_paths, -0.876)
        tag, advice = analyze_path_pattern(p)
        assert tag == "LONG_COMBO"
        # 建议要具体:提到起点/终点和"流水线"
        assert "流水线" in advice
        assert "state_reg" in advice or "opcode_reg" in advice

    def test_io_unregistered_detected(self, violating_paths: list[ViolatingPath]):
        """路径 3: source 是顶层 port(data_in[7]) → IO_UNREGISTERED。"""
        p = self._by_slack(violating_paths, -0.512)
        tag, advice = analyze_path_pattern(p)
        assert tag == "IO_UNREGISTERED"
        assert "data_in[7]" in advice
        assert "IOB" in advice

    def test_high_fanout_detected(self, violating_paths: list[ViolatingPath]):
        """路径 4: route=8.9 >= 3*logic(1.2) → HIGH_FANOUT。"""
        p = self._by_slack(violating_paths, -0.300)
        tag, advice = analyze_path_pattern(p)
        assert tag == "HIGH_FANOUT"
        # 建议要具体:MAX_FANOUT 或 report_high_fanout_nets
        assert "MAX_FANOUT" in advice or "fanout" in advice.lower()

    def test_unknown_fallback(self, violating_paths: list[ViolatingPath]):
        """路径 5(hold, slack=-0.080): 不命中任何规则 → UNKNOWN。"""
        p = self._by_slack(violating_paths, -0.080)
        tag, advice = analyze_path_pattern(p)
        assert tag == "UNKNOWN"
        # 兜底建议应该告诉用户怎么手动继续诊断
        assert "report_timing" in advice

    def test_cdc_priority_over_other_patterns(self):
        """CDC 优先级最高:即使 levels 巨大也先出 CDC tag。"""
        p = ViolatingPath(
            slack=-2.0,
            startpoint="src_clk_domain/reg_a/C",
            endpoint="dst_clk_domain/reg_b/D",
            start_clock="clk_a",
            end_clock="clk_b",
            logic_delay=5.0,
            route_delay=1.0,
            clock_skew=0.0,
            levels=20,  # LONG_COMBO 级别,但 CDC 优先
            type="setup",
        )
        tag, _ = analyze_path_pattern(p)
        assert tag == "CDC"


class TestFormatViolatingPaths:
    """format_timing_report 对 violating_paths 字段的格式化。"""

    def _make_failing_report(
        self, paths: list[ViolatingPath]
    ) -> TimingReport:
        summary = TimingSummary(
            wns=-1.234, tns=-5.0, whs=-0.080, ths=-0.200,
            failing_endpoints=5, total_endpoints=200, timing_met=False,
        )
        return TimingReport(summary=summary, paths=[], violating_paths=paths)

    def test_format_includes_violating_section(
        self, violating_paths: list[ViolatingPath]
    ):
        """timing_met=False 且有违例路径 → 输出含"违例路径 Top N"段。"""
        r = self._make_failing_report(violating_paths)
        text = format_timing_report(r)
        assert "违例路径" in text
        assert f"Top {len(violating_paths)}" in text

    def test_format_shows_pattern_tag(
        self, violating_paths: list[ViolatingPath]
    ):
        """格式化输出带 [CDC] / [IO_UNREGISTERED] 等 tag。"""
        r = self._make_failing_report(violating_paths)
        text = format_timing_report(r)
        assert "[CDC]" in text
        assert "[LONG_COMBO]" in text
        assert "[IO_UNREGISTERED]" in text
        assert "[HIGH_FANOUT]" in text

    def test_format_shows_cdc_hint(
        self, violating_paths: list[ViolatingPath]
    ):
        """CDC 路径额外显示 [CDC: src_clk→dst_clk] 标注。"""
        r = self._make_failing_report(violating_paths)
        text = format_timing_report(r)
        assert "sys_clk_100mhz" in text
        assert "dma_clk_200mhz" in text
        # 箭头形式的 CDC 提示
        assert "→" in text or "CDC" in text

    def test_format_shows_advice(
        self, violating_paths: list[ViolatingPath]
    ):
        """每条违例都附中文建议。"""
        r = self._make_failing_report(violating_paths)
        text = format_timing_report(r)
        assert "建议:" in text

    def test_format_shows_delay_breakdown(
        self, violating_paths: list[ViolatingPath]
    ):
        """格式化输出含 logic/route/skew/levels 分解。"""
        r = self._make_failing_report(violating_paths)
        text = format_timing_report(r)
        assert "logic" in text
        assert "route" in text
        assert "levels=" in text

    def test_format_no_section_when_no_violating_paths(self):
        """timing_met=True 时不追加违例路径段。"""
        summary = TimingSummary(
            wns=0.5, tns=0.0, whs=0.1, ths=0.0,
            failing_endpoints=0, total_endpoints=100, timing_met=True,
        )
        r = TimingReport(summary=summary, paths=[], violating_paths=[])
        text = format_timing_report(r)
        assert "违例路径 Top" not in text

    def test_format_shows_error_when_query_failed(self):
        """timing_met=False 但查询违例路径失败时,显示错误提示。"""
        summary = TimingSummary(
            wns=-0.5, tns=-1.0, whs=0.1, ths=0.0,
            failing_endpoints=3, total_endpoints=100, timing_met=False,
        )
        r = TimingReport(
            summary=summary,
            paths=[],
            violating_paths=[],
            violating_paths_error="TimeoutError: 60s",
        )
        text = format_timing_report(r)
        assert "违例路径查询失败" in text
        assert "TimeoutError" in text
