"""warning_parser.py 单元测试。

重点覆盖：
- parse_diag_counts 诊断计数解析
- parse_critical_warnings CRITICAL WARNING 逐行解析
- group_warnings 按 warning_id 聚合分组
- format_warning_report 中文报告格式化
- parse_pre_bitstream Bitstream 前置检查解析
- 边界情况：空输入、未知 ID、缺失字段
"""

from __future__ import annotations

import pathlib

from vivado_mcp.analysis.warning_parser import (
    BatStepResult,
    CriticalWarning,
    NonStandardError,
    WarningGroup,
    WarningReport,
    format_bat_steps_section,
    format_nonstandard_section,
    format_warning_report,
    group_warnings,
    parse_bat_run_output,
    parse_critical_warnings,
    parse_diag_counts,
    parse_launch_scripts_output,
    parse_pre_bitstream,
    parse_sim_logs_output,
    parse_tail_runme_output,
    scan_nonstandard_errors,
)

# ====================================================================== #
#  测试 fixture 辅助
# ====================================================================== #

FIXTURES = pathlib.Path(__file__).parent.parent / "fixtures"


def _make_vmcp_cw_output(log_path: pathlib.Path) -> str:
    """模拟 Tcl 脚本 EXTRACT_CRITICAL_WARNINGS 从 runme.log 产生的 VMCP_CW 输出。

    逐行扫描文件，遇到 CRITICAL WARNING 行就输出 ``VMCP_CW:行号|原文``，
    最后追加 ``VMCP_CW_DONE``。
    """
    lines = log_path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    for i, line in enumerate(lines, 1):
        if "CRITICAL WARNING:" in line:
            out.append(f"VMCP_CW:{i}|{line}")
    out.append("VMCP_CW_DONE")
    return "\n".join(out)


# ====================================================================== #
#  parse_diag_counts：诊断计数解析
# ====================================================================== #


class TestParseDiagCounts:
    """parse_diag_counts 诊断计数解析测试。"""

    def test_parse_diag_counts_normal(self):
        """正常输出解析为三元组。"""
        raw = "some info\nVMCP_DIAG:errors=0,critical_warnings=16,warnings=42\nmore info"
        assert parse_diag_counts(raw) == (0, 16, 42)

    def test_parse_diag_counts_not_found(self):
        """不包含 VMCP_DIAG 行时返回 (-1,-1,-1)。"""
        raw = "INFO: Vivado complete\nno diag line here"
        assert parse_diag_counts(raw) == (-1, -1, -1)

    def test_parse_diag_counts_log_missing(self):
        """runme.log 不存在时 Tcl 脚本输出 -1，解析结果也为 -1。"""
        raw = "VMCP_DIAG:errors=-1,critical_warnings=-1,warnings=-1"
        assert parse_diag_counts(raw) == (-1, -1, -1)

    def test_parse_diag_counts_empty(self):
        """空字符串不崩溃，返回 (-1,-1,-1)。"""
        assert parse_diag_counts("") == (-1, -1, -1)


# ====================================================================== #
#  parse_critical_warnings：CRITICAL WARNING 解析
# ====================================================================== #


class TestParseCriticalWarnings:
    """parse_critical_warnings CRITICAL WARNING 解析测试。"""

    def test_parse_single_cw(self):
        """解析单条 VMCP_CW 行，验证所有字段正确提取。"""
        raw = (
            "VMCP_CW:3|CRITICAL WARNING: [Vivado 12-1411] Cannot set LOC property "
            "of port pcie_7x_mgt_rtl_0_rxp[0] to package_pin AA4, because the "
            "MGTXRXP0_115 is occupied by port pcie_7x_mgt_rtl_0_rxn[7]. "
            "The conflicting port was constrained by [board_pins.xdc:15].\n"
            "VMCP_CW_DONE"
        )
        result = parse_critical_warnings(raw)
        assert len(result) == 1

        cw = result[0]
        assert cw.warning_id == "Vivado 12-1411"
        assert cw.line_number == 3
        assert cw.source_file == "board_pins.xdc"
        assert cw.port == "pcie_7x_mgt_rtl_0_rxp[0]"
        assert cw.pin == "AA4"
        assert "Cannot set LOC" in cw.message

    def test_parse_multiple_cw(self):
        """解析 fixture 文件中 16 条 CRITICAL WARNING。"""
        log_path = FIXTURES / "sample_runme_log.txt"
        raw = _make_vmcp_cw_output(log_path)
        result = parse_critical_warnings(raw)
        assert len(result) == 16

    def test_extract_warning_id(self):
        """验证 warning_id 从 [Vivado 12-1411] 正确提取。"""
        raw = (
            "VMCP_CW:10|CRITICAL WARNING: [Vivado 12-1411] Cannot set LOC "
            "of port test_port to package_pin B5. [test.xdc:1]."
        )
        result = parse_critical_warnings(raw)
        assert result[0].warning_id == "Vivado 12-1411"

    def test_extract_source_file(self):
        """验证源文件从消息末尾 [board_pins.xdc:15] 提取。"""
        raw = (
            "VMCP_CW:5|CRITICAL WARNING: [Vivado 12-1411] some text "
            "[board_pins.xdc:15]."
        )
        result = parse_critical_warnings(raw)
        assert result[0].source_file == "board_pins.xdc"

    def test_extract_port(self):
        """验证端口名从 'port xxx' 正确提取。"""
        raw = (
            "VMCP_CW:7|CRITICAL WARNING: [Vivado 12-1411] Cannot set LOC "
            "property of port my_port_rx[3] to package_pin C7. [test.xdc:2]."
        )
        result = parse_critical_warnings(raw)
        assert result[0].port == "my_port_rx[3]"

    def test_extract_pin(self):
        """验证引脚名从 'package_pin XX' 正确提取。"""
        raw = (
            "VMCP_CW:9|CRITICAL WARNING: [Vivado 12-1411] Cannot set LOC "
            "property of port test to package_pin AB6. [test.xdc:3]."
        )
        result = parse_critical_warnings(raw)
        assert result[0].pin == "AB6"

    def test_empty_input(self):
        """空字符串不崩溃，返回空列表。"""
        assert parse_critical_warnings("") == []

    def test_no_matching_lines(self):
        """不含 VMCP_CW 行时返回空列表。"""
        raw = "INFO: normal log line\nVMCP_CW_DONE"
        assert parse_critical_warnings(raw) == []

    def test_warning_without_source_file(self):
        """消息中不含源文件引用时 source_file 为空。"""
        raw = "VMCP_CW:1|CRITICAL WARNING: [Vivado 12-4739] Clock constraint issue."
        result = parse_critical_warnings(raw)
        assert result[0].source_file == ""

    def test_warning_without_port_or_pin(self):
        """消息中不含 port/pin 时对应字段为空。"""
        raw = "VMCP_CW:1|CRITICAL WARNING: [Timing 38-282] Timing violation detected."
        result = parse_critical_warnings(raw)
        assert result[0].port == ""
        assert result[0].pin == ""


# ====================================================================== #
#  group_warnings：聚合分组
# ====================================================================== #


class TestGroupWarnings:
    """group_warnings 分组测试。"""

    def test_group_same_id(self):
        """相同 warning_id 的 16 条警告聚合为 1 组。"""
        log_path = FIXTURES / "sample_runme_log.txt"
        raw = _make_vmcp_cw_output(log_path)
        cw_list = parse_critical_warnings(raw)
        groups = group_warnings(cw_list)
        # fixture 文件中所有 CW 都是 Vivado 12-1411
        assert len(groups) == 1
        assert groups[0].count == 16
        assert groups[0].warning_id == "Vivado 12-1411"

    def test_group_mixed_ids(self):
        """不同 warning_id 分为不同组。"""
        cw_list = [
            CriticalWarning("Vivado 12-1411", "msg A", 1, "a.xdc", "portA", "A1"),
            CriticalWarning("Vivado 12-1411", "msg B", 2, "a.xdc", "portB", "A2"),
            CriticalWarning("Timing 38-282", "msg C", 5, "", "", ""),
            CriticalWarning("DRC RTSTAT-1", "msg D", 8, "", "", ""),
            CriticalWarning("Timing 38-282", "msg E", 10, "", "", ""),
        ]
        groups = group_warnings(cw_list)
        assert len(groups) == 3

        # 验证各组计数
        by_id = {g.warning_id: g for g in groups}
        assert by_id["Vivado 12-1411"].count == 2
        assert by_id["Timing 38-282"].count == 2
        assert by_id["DRC RTSTAT-1"].count == 1

    def test_known_category(self):
        """已知 warning_id 映射到正确的分类标签。"""
        cw_list = [
            CriticalWarning("Vivado 12-1411", "msg", 1, "a.xdc", "p", "A1"),
        ]
        groups = group_warnings(cw_list)
        assert groups[0].category == "GT_PIN_CONFLICT"
        assert "GT端口" in groups[0].suggestion

    def test_unknown_category(self):
        """未知 warning_id 分类为 UNKNOWN，使用通用建议。"""
        cw_list = [
            CriticalWarning("Vivado 99-9999", "unknown msg", 42, "", "", ""),
        ]
        groups = group_warnings(cw_list)
        assert groups[0].category == "UNKNOWN"
        assert "未知" in groups[0].suggestion

    def test_group_affected_ports_dedup(self):
        """受影响端口去重，保持出现顺序。"""
        cw_list = [
            CriticalWarning("Vivado 12-1411", "msg1", 1, "a.xdc", "portA", "A1"),
            CriticalWarning("Vivado 12-1411", "msg2", 2, "a.xdc", "portA", "A2"),
            CriticalWarning("Vivado 12-1411", "msg3", 3, "a.xdc", "portB", "A3"),
        ]
        groups = group_warnings(cw_list)
        assert groups[0].affected_ports == ["portA", "portB"]

    def test_group_source_files_dedup(self):
        """源文件去重，保持出现顺序。"""
        cw_list = [
            CriticalWarning("Vivado 12-1411", "msg1", 1, "a.xdc", "p1", "A1"),
            CriticalWarning("Vivado 12-1411", "msg2", 2, "b.xdc", "p2", "A2"),
            CriticalWarning("Vivado 12-1411", "msg3", 3, "a.xdc", "p3", "A3"),
        ]
        groups = group_warnings(cw_list)
        assert groups[0].source_files == ["a.xdc", "b.xdc"]

    def test_group_empty_list(self):
        """空列表输入返回空分组列表。"""
        assert group_warnings([]) == []


# ====================================================================== #
#  format_warning_report：报告格式化
# ====================================================================== #


class TestFormatWarningReport:
    """format_warning_report 报告格式化测试。"""

    def test_format_with_cw(self):
        """存在 CRITICAL WARNING 时首行包含 '!! 发现'。"""
        groups = [
            WarningGroup(
                warning_id="Vivado 12-1411",
                category="GT_PIN_CONFLICT",
                count=16,
                first_line=3,
                message_template="Cannot set LOC ...",
                affected_ports=["portA", "portB"],
                source_files=["board_pins.xdc"],
                suggestion="GT端口PACKAGE_PIN约束与IP内部LOC冲突。",
            ),
        ]
        report = WarningReport(errors=0, critical_warnings=16, warnings=42, groups=groups)
        text = format_warning_report(report)

        assert text.startswith("!! 发现 16 条 CRITICAL WARNING !!")
        assert "GT_PIN_CONFLICT" in text
        assert "portA" in text
        assert "board_pins.xdc" in text
        assert "建议:" in text

    def test_format_clean(self):
        """无 CRITICAL WARNING 时不出现警告头。"""
        report = WarningReport(errors=0, critical_warnings=0, warnings=5, groups=[])
        text = format_warning_report(report)

        assert "!! 发现" not in text
        assert "critical_warnings=0" in text

    def test_format_multiple_groups(self):
        """多个分组都出现在报告中。"""
        groups = [
            WarningGroup(
                warning_id="Vivado 12-1411",
                category="GT_PIN_CONFLICT",
                count=8,
                first_line=3,
                message_template="msg A",
                suggestion="建议A",
            ),
            WarningGroup(
                warning_id="Timing 38-282",
                category="TIMING_VIOLATION",
                count=2,
                first_line=20,
                message_template="msg B",
                suggestion="建议B",
            ),
        ]
        report = WarningReport(errors=0, critical_warnings=10, warnings=0, groups=groups)
        text = format_warning_report(report)

        assert "GT_PIN_CONFLICT" in text
        assert "TIMING_VIOLATION" in text
        assert "建议A" in text
        assert "建议B" in text


# ====================================================================== #
#  parse_pre_bitstream：Bitstream 前置检查
# ====================================================================== #


class TestParsePreBitstream:
    """parse_pre_bitstream Bitstream 前置检查解析测试。"""

    def test_parse_pre_bitstream(self):
        """正常 VMCP_PRE_BIT 输出解析正确。"""
        raw = (
            "VMCP_PRE_BIT:status=route_design Complete,critical_warnings=3\n"
            "VMCP_PRE_BIT_CW:CRITICAL WARNING: [Vivado 12-1411] pin conflict 1\n"
            "VMCP_PRE_BIT_CW:CRITICAL WARNING: [Vivado 12-1411] pin conflict 2\n"
            "VMCP_PRE_BIT_CW:CRITICAL WARNING: [Timing 38-282] timing issue\n"
            "VMCP_PRE_BIT_DONE"
        )
        status, cw_count, samples = parse_pre_bitstream(raw)
        assert status == "route_design Complete"
        assert cw_count == 3
        assert len(samples) == 3
        assert "pin conflict 1" in samples[0]
        assert "timing issue" in samples[2]

    def test_parse_pre_bitstream_no_cw(self):
        """无 CRITICAL WARNING 时样本列表为空。"""
        raw = (
            "VMCP_PRE_BIT:status=route_design Complete,critical_warnings=0\n"
            "VMCP_PRE_BIT_DONE"
        )
        status, cw_count, samples = parse_pre_bitstream(raw)
        assert status == "route_design Complete"
        assert cw_count == 0
        assert samples == []

    def test_parse_pre_bitstream_empty(self):
        """空字符串不崩溃，返回默认值。"""
        status, cw_count, samples = parse_pre_bitstream("")
        assert status == "UNKNOWN"
        assert cw_count == -1
        assert samples == []


# ====================================================================== #
#  集成：从 fixture 文件端到端解析
# ====================================================================== #


class TestEndToEnd:
    """端到端集成测试：fixture → parse → group → format。"""

    def test_fixture_full_pipeline(self):
        """从 sample_runme_log.txt 完整走一遍解析流程。"""
        log_path = FIXTURES / "sample_runme_log.txt"
        raw_cw = _make_vmcp_cw_output(log_path)
        raw_diag = "VMCP_DIAG:errors=0,critical_warnings=16,warnings=42"

        # 解析计数
        errors, cw_count, w_count = parse_diag_counts(raw_diag)
        assert errors == 0
        assert cw_count == 16

        # 解析 CW 详情
        cw_list = parse_critical_warnings(raw_cw)
        assert len(cw_list) == 16

        # 分组
        groups = group_warnings(cw_list)
        assert len(groups) == 1
        assert groups[0].category == "GT_PIN_CONFLICT"

        # 格式化
        report = WarningReport(
            errors=errors,
            critical_warnings=cw_count,
            warnings=w_count,
            groups=groups,
        )
        text = format_warning_report(report)
        assert "!! 发现 16 条 CRITICAL WARNING !!" in text
        assert "board_pins.xdc" in text


# ====================================================================== #
#  scan_nonstandard_errors / format_nonstandard_section
# ====================================================================== #


class TestScanNonstandardErrors:
    """非标错误关键词扫描器。补 Vivado messageDb 看不见的内部异常。"""

    def test_detects_tclstackfree_and_abort(self):
        text = (
            "Phase 1.5 GENERATE_TARGET\n"
            "TclStackFree: incorrect freePtr 0x0123. Call out of sequence?\n"
            "abort() has been called\n"
        )
        results = scan_nonstandard_errors(text)
        kws = {r.keyword for r in results}
        # 第二行同时匹配 TclStackFree 和 incorrect freePtr,
        # 但实现保证一行只算一条(取首个 pattern) → 只命中 TclStackFree
        assert "TclStackFree" in kws
        assert "abort" in kws

    def test_skips_standard_error_and_cw_prefixes(self):
        """ERROR: / CRITICAL WARNING: 前缀的行不归非标(它们走 messageDb 解析)。"""
        text = (
            "ERROR: [Common 17-39] failed due to earlier errors\n"
            "CRITICAL WARNING: [DRC NSTD-1] port has no IOSTANDARD\n"
            "FATAL: panic in elaborate\n"
        )
        results = scan_nonstandard_errors(text)
        # 只有 FATAL 行算非标
        assert len(results) == 1
        assert results[0].keyword == "FATAL"

    def test_detects_chinese_cmd_not_found(self):
        text = "'xvlog' 不是内部或外部命令,也不是可运行的程序\n"
        results = scan_nonstandard_errors(text)
        assert any(r.keyword == "not_recognized_cmd_zh" for r in results)

    def test_detects_english_cmd_not_found(self):
        text = "'xvlog' is not recognized as an internal or external command\n"
        results = scan_nonstandard_errors(text)
        assert any(r.keyword == "not_recognized_cmd" for r in results)

    def test_detects_segfault_and_oom(self):
        text = (
            "Segmentation fault\n"
            "std::bad_alloc: out of memory\n"
        )
        results = scan_nonstandard_errors(text)
        kws = {r.keyword for r in results}
        assert "segfault" in kws
        assert "OOM" in kws

    def test_empty_input_returns_empty(self):
        assert scan_nonstandard_errors("") == []

    def test_no_match_returns_empty(self):
        text = "INFO: all good\nPhase 1 Complete\nINFO: [Synth 8-7079] launched\n"
        assert scan_nonstandard_errors(text) == []

    def test_start_line_offset_applies(self):
        """start_line=100 时,文本中第 1 行 → 报告中第 101 行。"""
        text = "TclStackFree: incorrect freePtr\n"
        results = scan_nonstandard_errors(text, start_line=100)
        assert results[0].line_number == 101

    def test_one_line_one_record(self):
        """一行多关键词只算一条,避免重复轰炸。"""
        text = "TclStackFree FATAL abort segfault\n"
        results = scan_nonstandard_errors(text)
        assert len(results) == 1

    def test_fixture_tclstackfree_log(self):
        log_path = FIXTURES / "runme_tclstackfree.txt"
        text = log_path.read_text(encoding="utf-8")
        results = scan_nonstandard_errors(text)
        kws = {r.keyword for r in results}
        assert "TclStackFree" in kws
        assert "abort" in kws

    def test_fixture_xvlog_not_found_log(self):
        log_path = FIXTURES / "xvlog_not_found.txt"
        text = log_path.read_text(encoding="utf-8")
        results = scan_nonstandard_errors(text)
        kws = {r.keyword for r in results}
        # 中文 cmd 提示必须命中
        assert "not_recognized_cmd_zh" in kws


class TestFormatNonstandardSection:
    """非标错误段格式化。"""

    def test_empty_returns_empty_string(self):
        assert format_nonstandard_section([]) == ""

    def test_includes_tclstackfree_hint(self):
        errs = [
            NonStandardError(
                keyword="TclStackFree",
                line_number=42,
                text="TclStackFree: incorrect freePtr",
                severity="high",
            )
        ]
        out = format_nonstandard_section(errs)
        assert "非标错误" in out
        assert "第 42 行" in out
        assert "ASCII" in out  # hint 提到中文路径迁移建议

    def test_includes_cmd_not_found_hint(self):
        errs = [
            NonStandardError(
                keyword="not_recognized_cmd_zh",
                line_number=10,
                text="'xvlog' 不是内部或外部命令",
                severity="high",
            )
        ]
        out = format_nonstandard_section(errs)
        assert "PATH" in out  # 提示查 $::env(PATH)

    def test_multiple_errors_aggregate_hints(self):
        errs = [
            NonStandardError("TclStackFree", 1, "TclStackFree: ...", "high"),
            NonStandardError("OOM", 50, "bad_alloc", "high"),
        ]
        out = format_nonstandard_section(errs)
        # 两类 hint 都要在
        assert "ASCII" in out
        assert "jobs" in out  # OOM hint 提示降低 jobs

    def test_medium_severity_does_not_trigger_high_hint(self):
        """只有 permission_denied(medium)时,不应触发 TclStackFree/OOM hint。"""
        errs = [
            NonStandardError("permission_denied", 1, "permission denied", "medium")
        ]
        out = format_nonstandard_section(errs)
        # medium 没有 hint,只有头部 + 条目
        assert "可能的修复方向" not in out
        assert "permission_denied" in out


# ====================================================================== #
#  parse_tail_runme_output / parse_sim_logs_output
# ====================================================================== #


class TestParseTailRunmeOutput:
    """TAIL_RUNME_LOG 输出解析。"""

    def test_full_output(self):
        raw = (
            "VMCP_TAIL:status=synth_design ERROR\n"
            "VMCP_TAIL:total=1500\n"
            "VMCP_TAIL_LINE:1451|Phase 1.5\n"
            "VMCP_TAIL_LINE:1452|TclStackFree: incorrect freePtr\n"
            "VMCP_TAIL_LINE:1453|abort()\n"
            "VMCP_TAIL_DONE\n"
        )
        status, start_line, body = parse_tail_runme_output(raw)
        assert status == "synth_design ERROR"
        # tail 起始为 1451 → start_line=1450(0-based 偏移)
        assert start_line == 1450
        assert body.splitlines() == [
            "Phase 1.5",
            "TclStackFree: incorrect freePtr",
            "abort()",
        ]

    def test_log_missing(self):
        raw = (
            "VMCP_TAIL:status=Not started\n"
            "VMCP_TAIL:log_missing=1\n"
            "VMCP_TAIL_DONE\n"
        )
        status, start_line, body = parse_tail_runme_output(raw)
        assert status == "Not started"
        assert start_line == 0
        assert body == ""

    def test_run_not_found(self):
        raw = "VMCP_TAIL:error=run_not_found\nVMCP_TAIL_DONE\n"
        status, start_line, body = parse_tail_runme_output(raw)
        assert status == ""
        assert body == ""

    def test_body_feeds_scan_nonstandard(self):
        """tail 输出经过解析后,start_line 让 scan 报出的行号还原原始位置。"""
        raw = (
            "VMCP_TAIL:status=synth_design ERROR\n"
            "VMCP_TAIL_LINE:1247|TclStackFree: incorrect freePtr\n"
            "VMCP_TAIL_DONE\n"
        )
        status, start_line, body = parse_tail_runme_output(raw)
        errs = scan_nonstandard_errors(body, start_line=start_line)
        assert len(errs) == 1
        assert errs[0].line_number == 1247  # 原始行号还原


class TestParseSimLogsOutput:
    """TAIL_SIM_LOGS 输出解析。"""

    def test_fileset_not_found(self):
        raw = "VMCP_SIM:error=fileset_not_found\nVMCP_SIM_DONE\n"
        sim_dir, logs = parse_sim_logs_output(raw)
        assert sim_dir == ""
        assert logs == []

    def test_dir_exists_no_logs(self):
        raw = (
            "VMCP_SIM:sim_dir=C:/proj/proj.sim/sim_1\n"
            "VMCP_SIM:log_count=0\n"
            "VMCP_SIM_DONE\n"
        )
        sim_dir, logs = parse_sim_logs_output(raw)
        assert sim_dir == "C:/proj/proj.sim/sim_1"
        assert logs == []

    def test_two_logs(self):
        raw = (
            "VMCP_SIM:sim_dir=C:/proj/proj.sim/sim_1\n"
            "VMCP_SIM:log_count=2\n"
            "VMCP_SIM_LOG_START:C:/proj/proj.sim/sim_1/behav/xsim/xvlog.log\n"
            "VMCP_SIM_LOG_LINE:1|Running xvlog\n"
            "VMCP_SIM_LOG_LINE:2|'xvlog' 不是内部或外部命令\n"
            "VMCP_SIM_LOG_END:C:/proj/proj.sim/sim_1/behav/xsim/xvlog.log\n"
            "VMCP_SIM_LOG_START:C:/proj/proj.sim/sim_1/behav/xsim/compile.log\n"
            "VMCP_SIM_LOG_LINE:8|ERROR: [USF-XSim-62] compile failed\n"
            "VMCP_SIM_LOG_END:C:/proj/proj.sim/sim_1/behav/xsim/compile.log\n"
            "VMCP_SIM_DONE\n"
        )
        sim_dir, logs = parse_sim_logs_output(raw)
        assert sim_dir == "C:/proj/proj.sim/sim_1"
        assert len(logs) == 2
        # 第一份日志
        assert logs[0].log_path.endswith("xvlog.log")
        assert logs[0].start_line == 0  # tail 第一行是原文件第 1 行 → offset=0
        assert "不是内部或外部命令" in logs[0].body
        # 第二份日志
        assert logs[1].log_path.endswith("compile.log")
        assert logs[1].start_line == 7  # tail 第一行是原文件第 8 行 → offset=7

    def test_log_lines_feed_scan(self):
        """tail 的 body 给 scan 后,行号能还原(基于 start_line 偏移)。"""
        raw = (
            "VMCP_SIM:sim_dir=C:/proj/proj.sim/sim_1\n"
            "VMCP_SIM_LOG_START:xvlog.log\n"
            "VMCP_SIM_LOG_LINE:42|'xvlog' 不是内部或外部命令\n"
            "VMCP_SIM_LOG_END:xvlog.log\n"
            "VMCP_SIM_DONE\n"
        )
        _, logs = parse_sim_logs_output(raw)
        errs = scan_nonstandard_errors(logs[0].body, start_line=logs[0].start_line)
        assert len(errs) == 1
        assert errs[0].line_number == 42
        assert errs[0].keyword == "not_recognized_cmd_zh"


# ====================================================================== #
#  parse_launch_scripts_output / format_bat_steps_section
#  (0.3.15:launch_simulation -scripts_only fallback)
# ====================================================================== #


class TestParseLaunchScriptsOutput:
    """LAUNCH_SCRIPTS_AND_GLOB 协议解析。"""

    def test_already_present_with_three_bats(self):
        raw = (
            "VMCP_BAT:sim_dir=C:/p/p.sim/sim_1\n"
            "VMCP_BAT:scripts_only=already_present\n"
            "VMCP_BAT_FILE:compile|bat|C:/p/p.sim/sim_1/behav/xsim/compile.bat\n"
            "VMCP_BAT_FILE:elaborate|bat|C:/p/p.sim/sim_1/behav/xsim/elaborate.bat\n"
            "VMCP_BAT_FILE:simulate|bat|C:/p/p.sim/sim_1/behav/xsim/simulate.bat\n"
            "VMCP_BAT_DONE\n"
        )
        sim_dir, status, files = parse_launch_scripts_output(raw)
        assert sim_dir == "C:/p/p.sim/sim_1"
        assert status == "already_present"
        assert len(files) == 3
        assert files[0] == ("compile", "bat", "C:/p/p.sim/sim_1/behav/xsim/compile.bat")

    def test_scripts_only_failed_carries_reason(self):
        raw = (
            "VMCP_BAT:sim_dir=/x\n"
            "VMCP_BAT:scripts_only=triggering\n"
            "VMCP_BAT:scripts_only_failed=xilinx internal error\n"
            "VMCP_BAT_DONE\n"
        )
        sim_dir, status, files = parse_launch_scripts_output(raw)
        assert sim_dir == "/x"
        assert status == "failed:xilinx internal error"
        assert files == []

    def test_fileset_not_found(self):
        raw = "VMCP_BAT:error=fileset_not_found\nVMCP_BAT_DONE\n"
        sim_dir, status, files = parse_launch_scripts_output(raw)
        assert sim_dir == ""
        assert status == "fileset_not_found"
        assert files == []

    def test_dir_missing(self):
        raw = (
            "VMCP_BAT:sim_dir=/missing\n"
            "VMCP_BAT:dir_missing=1\n"
            "VMCP_BAT_DONE\n"
        )
        sim_dir, status, files = parse_launch_scripts_output(raw)
        assert sim_dir == "/missing"
        assert status == "dir_missing"

    def test_malformed_bat_file_lines_ignored(self):
        """少 | 分隔 / 字段空的行不抛异常,直接跳过。"""
        raw = (
            "VMCP_BAT:sim_dir=/x\n"
            "VMCP_BAT:scripts_only=ok\n"
            "VMCP_BAT_FILE:compile_only_one_pipe\n"
            "VMCP_BAT_FILE:|bat|/x/empty_step.bat\n"
            "VMCP_BAT_FILE:compile|bat|/x/ok.bat\n"
            "VMCP_BAT_DONE\n"
        )
        _, _, files = parse_launch_scripts_output(raw)
        assert files == [("compile", "bat", "/x/ok.bat")]


class TestFormatBatStepsSection:
    """诊断段渲染。"""

    def test_all_steps_pass_means_wrapper_failure(self):
        results = [
            BatStepResult("compile", "/x/compile.bat", 0, "ok", ""),
            BatStepResult("elaborate", "/x/elaborate.bat", 0, "ok", ""),
            BatStepResult(
                "simulate", "/x/simulate.bat", -1, "", "skipped"
            ),
        ]
        out = format_bat_steps_section(results, "already_present", "/x")
        assert "wrapper 失败" in out
        assert "/x/compile.bat" in out
        assert "/x/elaborate.bat" in out

    def test_compile_failure_shows_stderr_and_real_error_hint(self):
        results = [
            BatStepResult(
                "compile", "/x/compile.bat", 1, "", "ERROR: foo missing"
            ),
        ]
        out = format_bat_steps_section(results, "ok", "/x")
        assert "ERROR: foo missing" in out
        assert "真错" in out or "returncode=1" in out

    def test_no_results_returns_clean_empty_message(self):
        out = format_bat_steps_section([], "already_present", "/x")
        assert "未跑任何 .bat" in out

    def test_timeout_marker(self):
        results = [
            BatStepResult("compile", "/x/c.bat", -2, "", "超时(>120s)"),
        ]
        out = format_bat_steps_section(results, "ok", "/x")
        assert "超时" in out

    def test_spawn_failure_marker(self):
        results = [
            BatStepResult(
                "compile", "/x/c.bat", -3, "", "spawn 失败: [WinError 2]"
            ),
        ]
        out = format_bat_steps_section(results, "ok", "/x")
        assert "spawn 失败" in out


class TestParseBatRunOutput:
    """RUN_BAT_STEP 协议(0.3.16)解析。"""

    def test_rc0_with_body(self):
        raw = (
            "VMCP_BAT_RUN:rc=0\n"
            "VMCP_BAT_RUN_OUT_START\n"
            "VMCP_BAT_RUN_LINE:line1\n"
            "VMCP_BAT_RUN_LINE:line2\n"
            "VMCP_BAT_RUN_OUT_END\n"
        )
        rc, out = parse_bat_run_output(raw)
        assert rc == 0
        assert out == "line1\nline2"

    def test_rc_nonzero(self):
        raw = (
            "VMCP_BAT_RUN:rc=7\n"
            "VMCP_BAT_RUN_OUT_START\n"
            "VMCP_BAT_RUN_LINE:ERROR: boom\n"
            "VMCP_BAT_RUN_OUT_END\n"
        )
        rc, out = parse_bat_run_output(raw)
        assert rc == 7
        assert "boom" in out

    def test_missing_rc_returns_minus_one(self):
        """协议解析失败兜底:rc=-1。"""
        raw = "garbage\nVMCP_BAT_RUN_LINE:nope\n"
        rc, out = parse_bat_run_output(raw)
        assert rc == -1
        # OUT_START 没看到 → body 不会被收
        assert out == ""

    def test_lines_outside_body_ignored(self):
        """VMCP_BAT_RUN_LINE: 出现在 OUT_START 之前的不计入 body。"""
        raw = (
            "VMCP_BAT_RUN_LINE:ghost\n"
            "VMCP_BAT_RUN:rc=0\n"
            "VMCP_BAT_RUN_OUT_START\n"
            "VMCP_BAT_RUN_LINE:real\n"
            "VMCP_BAT_RUN_OUT_END\n"
            "VMCP_BAT_RUN_LINE:also ghost\n"
        )
        rc, out = parse_bat_run_output(raw)
        assert rc == 0
        assert out == "real"
