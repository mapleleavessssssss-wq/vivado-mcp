"""report_tools 工具层测试。

通过 mock VivadoSession 与 MCP Context,覆盖:
- get_utilization_report 的 detail 参数(C2:Block RAM 明细)与向后兼容
- get_timing_report / check_bitstream_readiness / get_pre_commit_summary
  对 NA 时序摘要(无约束设计)的降级行为 —— 不崩溃、不假 PASS、不假 READY
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from vivado_mcp.vivado.tcl_utils import TclResult

_FIXTURES = Path(__file__).parent / "fixtures"


def _make_tcl_result(output: str, return_code: int = 0) -> TclResult:
    """构造 TclResult 实例。"""
    return TclResult(output=output, return_code=return_code, is_error=return_code != 0)


def _load_fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _mock_context() -> MagicMock:
    ctx = MagicMock()
    ctx.report_progress = AsyncMock()
    return ctx


# 无时序约束设计的 report_timing_summary 摘要(数据行全 NA)
_NA_TIMING = """\
------------------------------------------------------------------------------------
| Design Timing Summary
| ---------------------
------------------------------------------------------------------------------------

    WNS(ns)      TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints      WHS(ns)      THS(ns)  THS Failing Endpoints  THS Total Endpoints
    -------      -------  ---------------------  -------------------      -------      -------  ---------------------  -------------------
         NA           NA                     NA                   NA           NA           NA                     NA                   NA
"""  # noqa: E501

_STAGE_POST_ROUTE = (
    "VMCP_STAGE:stage=post-route"
    "|synth_status=synth_design Complete!"
    "|impl_status=route_design Complete!"
)

_PROJ_INFO = (
    "VMCP_PROJ:project_name=demo\n"
    "VMCP_PROJ:part=xc7a35tcpg236-1\n"
    "VMCP_PROJ:top=top\n"
    "VMCP_PROJ:synth_status=synth_design Complete!\n"
    "VMCP_PROJ:impl_status=route_design Complete!\n"
)


# ====================================================================== #
#  get_utilization_report: detail 参数(C2)
# ====================================================================== #


class TestGetUtilizationReportDetail:
    @pytest.mark.asyncio
    async def test_default_no_bram_detail_section(self):
        """detail 默认 False:输出保持原有格式,无 Block RAM 明细段。"""
        from vivado_mcp.tools.report_tools import get_utilization_report

        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=_make_tcl_result(_load_fixture("sample_report_utilization.txt"))
        )
        with patch("vivado_mcp.tools.report_tools._require_session", return_value=session):
            result = await get_utilization_report(ctx=_mock_context())

        assert "资源占用摘要" in result
        assert "Slice LUTs" in result
        assert "Block RAM 明细" not in result

    @pytest.mark.asyncio
    async def test_detail_true_appends_bram_section(self):
        """detail=True:末尾追加 RAMB36/FIFO* / RAMB36E1 only / RAMB18 明细。"""
        from vivado_mcp.tools.report_tools import get_utilization_report

        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=_make_tcl_result(_load_fixture("sample_report_utilization.txt"))
        )
        with patch("vivado_mcp.tools.report_tools._require_session", return_value=session):
            result = await get_utilization_report(detail=True, ctx=_mock_context())

        assert "Block RAM 明细" in result
        assert "RAMB36/FIFO*" in result
        assert "RAMB36E1 only" in result
        assert "RAMB18" in result

    @pytest.mark.asyncio
    async def test_error_passthrough(self):
        """report_utilization 失败时返回 [ERROR] 而不是把错误文本喂给 parser。"""
        from vivado_mcp.tools.report_tools import get_utilization_report

        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=_make_tcl_result("ERROR: no open design", return_code=1)
        )
        with patch("vivado_mcp.tools.report_tools._require_session", return_value=session):
            result = await get_utilization_report(ctx=_mock_context())

        assert "[ERROR]" in result


# ====================================================================== #
#  get_timing_report: NA 摘要(无约束设计)
# ====================================================================== #


class TestGetTimingReportNa:
    @pytest.mark.asyncio
    async def test_na_no_crash_no_false_pass(self):
        """NA 摘要不再 float('NA') 崩溃,且不显示 PASS,解释真实原因。"""
        from vivado_mcp.tools.report_tools import get_timing_report

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result(_STAGE_POST_ROUTE),  # QUERY_DESIGN_STAGE
                _make_tcl_result(_NA_TIMING),         # report_timing_summary
            ]
        )
        with patch("vivado_mcp.tools.report_tools._require_session", return_value=session):
            result = await get_timing_report(ctx=_mock_context())

        assert "[ERROR]" not in result
        assert "PASS" not in result
        assert "无时序" in result
        # NA(timing_met=False 但 parse_status 非 ok)不应触发第三次违例路径查询
        assert session.execute.await_count == 2

    @pytest.mark.asyncio
    async def test_unrecognized_format_marked_degraded(self):
        """格式不识别时输出 [DEGRADED],不默认 PASS。"""
        from vivado_mcp.tools.report_tools import get_timing_report

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result(_STAGE_POST_ROUTE),
                _make_tcl_result("完全不是时序报告的输出"),
            ]
        )
        with patch("vivado_mcp.tools.report_tools._require_session", return_value=session):
            result = await get_timing_report(ctx=_mock_context())

        assert "[DEGRADED]" in result
        assert "PASS" not in result
        assert session.execute.await_count == 2


# ====================================================================== #
#  check_bitstream_readiness: NA 时序不参与 READY/BLOCK 判定
# ====================================================================== #


class TestCheckBitstreamReadinessNa:
    _PRE_OK = "VMCP_PRE_BIT:status=route_design Complete!,critical_warnings=0"

    @pytest.mark.asyncio
    async def test_na_timing_degrades_to_warn(self, caplog):
        """NA 摘要 → 时序不可读,verdict 为 WARN(显示原因),不是假 READY/假 BLOCK。"""
        import logging

        from vivado_mcp.tools.report_tools import check_bitstream_readiness

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result(self._PRE_OK),
                _make_tcl_result(_NA_TIMING),
            ]
        )
        with caplog.at_level(logging.WARNING, logger="vivado_mcp.tools.report_tools"):
            with patch(
                "vivado_mcp.tools.report_tools._require_session", return_value=session
            ):
                result = await check_bitstream_readiness(ctx=_mock_context())

        assert "WARN" in result
        assert "READY" not in result
        assert "BLOCK" not in result
        assert "未能读取时序摘要" in result
        assert "无时序约束" in result  # 具体原因可见
        # NA = 数据本来就不存在,不是子查询失败 → 不附 [DEGRADED] 标记
        assert "[DEGRADED]" not in result
        # 降级原因写进了日志(spec §5:caplog 断言具体原因)
        assert any("no_timing_data" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_unrecognized_timing_marks_degraded(self, caplog):
        """时序报告格式不识别(解析失败)→ 判定不可信,verdict 行附 [DEGRADED] 标记。"""
        import logging

        from vivado_mcp.tools.report_tools import check_bitstream_readiness

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result(self._PRE_OK),
                _make_tcl_result("完全不是时序报告的输出"),
            ]
        )
        with caplog.at_level(logging.WARNING, logger="vivado_mcp.tools.report_tools"):
            with patch(
                "vivado_mcp.tools.report_tools._require_session", return_value=session
            ):
                result = await check_bitstream_readiness(ctx=_mock_context())

        assert "[DEGRADED]" in result
        assert "WARN" in result
        assert "READY" not in result
        assert "格式不识别" in result  # 具体原因可见
        assert any("unrecognized" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_timing_query_rc_error_marks_degraded(self):
        """report_timing_summary rc 非 0(子查询失败)→ verdict 行附 [DEGRADED] 标记。"""
        from vivado_mcp.tools.report_tools import check_bitstream_readiness

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result(self._PRE_OK),
                _make_tcl_result("ERROR: no open design", return_code=1),
            ]
        )
        with patch("vivado_mcp.tools.report_tools._require_session", return_value=session):
            result = await check_bitstream_readiness(ctx=_mock_context())

        assert "[DEGRADED]" in result
        assert "WARN" in result
        assert "READY" not in result
        assert "rc=1" in result  # 具体原因可见

    @pytest.mark.asyncio
    async def test_ok_timing_stays_ready(self):
        """positive 对照:正常 PASS 时序 + route 完成 + 0 CW → READY。"""
        from vivado_mcp.tools.report_tools import check_bitstream_readiness

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result(self._PRE_OK),
                _make_tcl_result(_load_fixture("sample_report_timing.txt")),
            ]
        )
        with patch("vivado_mcp.tools.report_tools._require_session", return_value=session):
            result = await check_bitstream_readiness(ctx=_mock_context())

        assert "READY" in result


# ====================================================================== #
#  get_pre_commit_summary: NA 时序 → DEGRADED,不假 READY
# ====================================================================== #


class TestPreCommitSummaryNa:
    @pytest.mark.asyncio
    async def test_na_timing_marks_degraded(self, caplog):
        """NA 摘要计入采样失败,verdict 从 READY 降级为 DEGRADED。"""
        import logging

        from vivado_mcp.tools.report_tools import get_pre_commit_summary

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result(_PROJ_INFO),                                  # 项目信息
                _make_tcl_result(_NA_TIMING),                                  # 时序 → NA
                _make_tcl_result(_load_fixture("sample_report_utilization.txt")),  # 资源
                _make_tcl_result("VMCP_DIAG:errors=0,critical_warnings=0,warnings=0"),
            ]
        )
        with caplog.at_level(logging.WARNING, logger="vivado_mcp.tools.report_tools"):
            with patch(
                "vivado_mcp.tools.report_tools._require_session", return_value=session
            ):
                result = await get_pre_commit_summary(ctx=_mock_context())

        assert "[DEGRADED]" in result
        assert "时序解析降级" in result
        assert "[READY]" not in result
        # 降级原因写进了日志(spec §5:caplog 断言具体原因)
        assert any("时序摘要解析降级" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_util_parse_error_marks_degraded(self, caplog):
        """资源报告格式不识别 → 计入采样失败 + 日志告警 + 资源段不渲染,
        verdict 落 DEGRADED,不再假 READY(比照时序分支的处理)。"""
        import logging

        from vivado_mcp.tools.report_tools import get_pre_commit_summary

        # 有表格行但表头不识别 → parse_utilization 置 parse_error
        bad_util = (
            "+------------+------+\n"
            "| Site Type  | Cnt  |\n"
            "+------------+------+\n"
            "| Slice LUTs | 1440 |\n"
            "+------------+------+\n"
        )
        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result(_PROJ_INFO),                                # 项目信息
                _make_tcl_result(_load_fixture("sample_report_timing.txt")),  # 时序 OK
                _make_tcl_result(bad_util),                                  # 资源 → 格式不识别
                _make_tcl_result("VMCP_DIAG:errors=0,critical_warnings=0,warnings=0"),
            ]
        )
        with caplog.at_level(logging.WARNING, logger="vivado_mcp.tools.report_tools"):
            with patch(
                "vivado_mcp.tools.report_tools._require_session", return_value=session
            ):
                result = await get_pre_commit_summary(ctx=_mock_context())

        assert "[DEGRADED]" in result
        assert "[READY]" not in result          # 不再假 READY
        assert "资源解析降级" in result
        assert "- 资源:" not in result          # 资源段不渲染(空数据不能装有数据)
        # 降级原因写进了日志(spec §5:caplog 断言具体原因)
        assert any("资源报告解析降级" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_ok_timing_stays_ready(self):
        """positive 对照:四项采样全部成功且无问题 → READY。"""
        from vivado_mcp.tools.report_tools import get_pre_commit_summary

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result(_PROJ_INFO),
                _make_tcl_result(_load_fixture("sample_report_timing.txt")),
                _make_tcl_result(_load_fixture("sample_report_utilization.txt")),
                _make_tcl_result("VMCP_DIAG:errors=0,critical_warnings=0,warnings=0"),
            ]
        )
        with patch("vivado_mcp.tools.report_tools._require_session", return_value=session):
            result = await get_pre_commit_summary(ctx=_mock_context())

        assert "[READY]" in result
        assert "DEGRADED" not in result
