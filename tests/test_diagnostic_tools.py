"""诊断工具集成测试。

通过 mock VivadoSession 和 MCP Context，测试 diagnostic_tools / flow_tools / report_tools
中工具函数的端到端行为（解析 + 格式化 + 错误处理），无需启动真实 Vivado 进程。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from vivado_mcp.vivado.tcl_utils import TclResult

# fixture 数据路径
_FIXTURES = Path(__file__).parent / "fixtures"


# ====================================================================== #
#  共享 mock 辅助
# ====================================================================== #


def _make_tcl_result(output: str, return_code: int = 0) -> TclResult:
    """构造 TclResult 实例。"""
    return TclResult(output=output, return_code=return_code, is_error=return_code != 0)


def _load_fixture(name: str) -> str:
    """加载 fixture 文件内容。"""
    return (_FIXTURES / name).read_text(encoding="utf-8")


def _make_vmcp_cw_output(log_text: str) -> str:
    """模拟 EXTRACT_CRITICAL_WARNINGS Tcl 脚本的输出格式。

    将 runme.log 中的 CRITICAL WARNING 行转换为 ``VMCP_CW:行号|消息`` 格式。
    """
    lines = []
    for i, line in enumerate(log_text.splitlines(), 1):
        if line.startswith("CRITICAL WARNING:"):
            lines.append(f"VMCP_CW:{i}|{line}")
    lines.append("VMCP_CW_DONE")
    return "\n".join(lines)


def _mock_context(session):
    """创建模拟的 MCP Context，使 _require_session 返回指定 session。"""
    ctx = MagicMock()
    ctx.report_progress = AsyncMock()
    return ctx


# ====================================================================== #
#  get_critical_warnings 测试
# ====================================================================== #


class TestGetCriticalWarnings:
    """测试 get_critical_warnings 工具。"""

    @pytest.mark.asyncio
    async def test_returns_report_with_cw(self):
        """有 CRITICAL WARNING 时，返回包含分类和建议的报告。"""
        from vivado_mcp.tools.diagnostic_tools import get_critical_warnings

        log_text = _load_fixture("sample_runme_log.txt")
        vmcp_cw = _make_vmcp_cw_output(log_text)

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                # 第一次调用：COUNT_WARNINGS 结果
                _make_tcl_result(
                    "VMCP_DIAG:errors=0,critical_warnings=16,warnings=3"
                ),
                # 第二次调用：EXTRACT_CRITICAL_WARNINGS 结果
                _make_tcl_result(vmcp_cw),
            ]
        )

        ctx = _mock_context(session)

        with patch("vivado_mcp.tools.diagnostic_tools._require_session", return_value=session):
            result = await get_critical_warnings(
                run_name="impl_1", session_id="default", ctx=ctx
            )

        # 验证首行醒目提示
        assert "!! 发现 16 条 CRITICAL WARNING !!" in result
        # 验证包含分类标签
        assert "GT_PIN_CONFLICT" in result
        # 验证包含建议
        assert "GT Location" in result or "GT LOC" in result or "GT端口" in result
        # 验证提到了受影响端口
        assert "pcie_7x_mgt_rtl_0_rxp[0]" in result

    @pytest.mark.asyncio
    async def test_returns_clean_when_no_cw(self):
        """无 CRITICAL WARNING 时，返回干净的诊断概览。"""
        from vivado_mcp.tools.diagnostic_tools import get_critical_warnings

        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=_make_tcl_result(
                "VMCP_DIAG:errors=0,critical_warnings=0,warnings=5"
            )
        )

        ctx = _mock_context(session)

        with patch("vivado_mcp.tools.diagnostic_tools._require_session", return_value=session):
            result = await get_critical_warnings(
                run_name="synth_1", session_id="default", ctx=ctx
            )

        assert "未发现 CRITICAL WARNING" in result
        assert "critical_warnings=0" in result

    @pytest.mark.asyncio
    async def test_returns_error_when_no_session(self):
        """会话不存在时，返回错误提示。"""
        from vivado_mcp.tools.diagnostic_tools import get_critical_warnings

        ctx = _mock_context(None)

        with patch("vivado_mcp.tools.diagnostic_tools._require_session", return_value=None):
            result = await get_critical_warnings(
                run_name="impl_1", session_id="nonexistent", ctx=ctx
            )

        assert "[ERROR]" in result

    @pytest.mark.asyncio
    async def test_returns_error_for_missing_log(self):
        """runme.log 不存在时，返回友好错误。"""
        from vivado_mcp.tools.diagnostic_tools import get_critical_warnings

        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=_make_tcl_result(
                "VMCP_DIAG:errors=-1,critical_warnings=-1,warnings=-1"
            )
        )

        ctx = _mock_context(session)

        with patch("vivado_mcp.tools.diagnostic_tools._require_session", return_value=session):
            result = await get_critical_warnings(
                run_name="impl_1", session_id="default", ctx=ctx
            )

        assert "未找到 runme.log" in result

    @pytest.mark.asyncio
    async def test_rejects_invalid_run_name(self):
        """非法 run_name 被拦截。"""
        from vivado_mcp.tools.diagnostic_tools import get_critical_warnings

        ctx = _mock_context(None)
        result = await get_critical_warnings(
            run_name="impl;rm -rf /", session_id="default", ctx=ctx
        )
        assert "[ERROR]" in result


# ====================================================================== #
#  verify_io_placement_tool 测试
# ====================================================================== #


class TestVerifyIoPlacement:
    """测试 verify_io_placement_tool 工具。

    B3 修复后：第一次 execute 返回 VMCP_XDC_FILE:<path> 行，
    Python 代码直接读取该文件（支持 -dict 和传统两种语法）。
    """

    @pytest.mark.asyncio
    async def test_detects_gt_mismatch(self):
        """GT 端口引脚不匹配时，报告包含 CRITICAL。"""
        from vivado_mcp.tools.diagnostic_tools import verify_io_placement_tool

        # 新实现：第一次 execute 返回 XDC 文件路径列表
        xdc_fixture = str(_FIXTURES / "sample_board_pins.xdc")
        list_output = f"VMCP_XDC_FILE:{xdc_fixture}"

        io_text = _load_fixture("sample_report_io.txt")

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result(list_output),  # XDC 文件路径
                _make_tcl_result(io_text),       # report_io
            ]
        )

        ctx = _mock_context(session)

        with patch("vivado_mcp.tools.diagnostic_tools._require_session", return_value=session):
            result = await verify_io_placement_tool(
                session_id="default", ctx=ctx
            )

        # rxp[0] XDC=AA4 但 report_io=M6 → 不匹配且是 GT → CRITICAL
        assert "CRITICAL" in result
        assert "pcie_7x_mgt_rtl_0_rxp[0]" in result

    @pytest.mark.asyncio
    async def test_no_xdc_files(self):
        """项目未添加 XDC 文件时，返回提示信息。"""
        from vivado_mcp.tools.diagnostic_tools import verify_io_placement_tool

        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=_make_tcl_result("")  # 空输出，无 VMCP_XDC_FILE 行
        )

        ctx = _mock_context(session)

        with patch("vivado_mcp.tools.diagnostic_tools._require_session", return_value=session):
            result = await verify_io_placement_tool(
                session_id="default", ctx=ctx
            )

        assert "未添加任何 XDC" in result


# ====================================================================== #
#  _launch_and_wait 自动诊断测试
# ====================================================================== #


class TestLaunchAndWaitDiag:
    """测试 _launch_and_wait 中的自动诊断逻辑。

    D5 重写后的流程：reset+launch → poll STATUS/PROGRESS → open_run → 诊断。
    mock 需要按这个顺序提供返回值。
    """

    @pytest.mark.asyncio
    async def test_warns_on_critical_warnings(self):
        """综合/实现完成后检测到 CW 时，首行插入醒目提示。"""
        from vivado_mcp.tools.flow_tools import _launch_and_wait

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                # 1. reset_run + launch_runs
                _make_tcl_result("Synthesis launched"),
                # 2. 第一次 poll：已 Complete
                _make_tcl_result(
                    "VMCP_POLL|synth_design Complete!|100%|00:05:30"
                ),
                # 3. open_run（自动）
                _make_tcl_result(""),
                # 4. COUNT_WARNINGS 诊断
                _make_tcl_result(
                    "VMCP_DIAG:errors=0,critical_warnings=16,warnings=3"
                ),
            ]
        )

        ctx = _mock_context(session)

        result = await _launch_and_wait(
            session, "synth_1", jobs=4, timeout_minutes=30, label="综合", ctx=ctx
        )

        assert "!! 发现 16 条 CRITICAL WARNING !!" in result
        assert "get_critical_warnings" in result

    @pytest.mark.asyncio
    async def test_no_warning_when_clean(self):
        """无 CW 时，不插入额外提示。"""
        from vivado_mcp.tools.flow_tools import _launch_and_wait

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                # 1. launch
                _make_tcl_result("Implementation launched"),
                # 2. poll 已完成
                _make_tcl_result(
                    "VMCP_POLL|route_design Complete!|100%|00:10:00"
                ),
                # 3. open_run
                _make_tcl_result(""),
                # 4. 诊断
                _make_tcl_result(
                    "VMCP_DIAG:errors=0,critical_warnings=0,warnings=5"
                ),
            ]
        )

        ctx = _mock_context(session)

        result = await _launch_and_wait(
            session, "impl_1", jobs=4, timeout_minutes=60, label="实现", ctx=ctx
        )

        assert "CRITICAL WARNING" not in result
        assert "critical_warnings=0" in result


# ====================================================================== #
#  generate_bitstream 安全检查测试
# ====================================================================== #


class TestGenerateBitstreamSafety:
    """测试 generate_bitstream 的前置安全检查。"""

    @pytest.mark.asyncio
    async def test_blocks_on_critical_warnings(self):
        """force=False + CW > 0 时，阻止生成并显示样本。"""
        from vivado_mcp.tools.flow_tools import generate_bitstream

        pre_bit_output = (
            "VMCP_PRE_BIT:status=route_design Complete,critical_warnings=16\n"
            "VMCP_PRE_BIT_CW:CRITICAL WARNING: [Vivado 12-1411] Cannot set LOC...\n"
            "VMCP_PRE_BIT_DONE"
        )

        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=_make_tcl_result(pre_bit_output)
        )

        ctx = _mock_context(session)

        with patch("vivado_mcp.tools.flow_tools._require_session", return_value=session):
            result = await generate_bitstream(
                impl_run="impl_1", force=False, session_id="default", ctx=ctx
            )

        assert "安全检查未通过" in result
        assert "16 条 CRITICAL WARNING" in result
        assert "force=True" in result

    @pytest.mark.asyncio
    async def test_allows_with_force(self):
        """force=True 时，跳过安全检查直接执行。"""
        from vivado_mcp.tools.flow_tools import generate_bitstream

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                # 不执行安全检查，直接 launch_runs
                _make_tcl_result("Bitstream generation complete"),
                # 查询比特流目录
                _make_tcl_result("比特流目录: /project/impl_1"),
            ]
        )

        ctx = _mock_context(session)

        with patch("vivado_mcp.tools.flow_tools._require_session", return_value=session):
            result = await generate_bitstream(
                impl_run="impl_1", force=True, session_id="default", ctx=ctx
            )

        assert "安全检查未通过" not in result
        # force=True 跳过 CHECK_PRE_BITSTREAM，execute 只被调用 2 次（launch+dir）
        assert session.execute.call_count == 2


# ====================================================================== #
#  get_io_report 测试
# ====================================================================== #


class TestGetIoReport:
    """测试 get_io_report 结构化报告工具。"""

    @pytest.mark.asyncio
    async def test_returns_valid_json(self):
        """返回可解析的 JSON，包含正确的统计数据。"""
        from vivado_mcp.tools.report_tools import get_io_report

        io_text = _load_fixture("sample_report_io.txt")

        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=_make_tcl_result(io_text)
        )

        ctx = _mock_context(session)

        with patch("vivado_mcp.tools.report_tools._require_session", return_value=session):
            result = await get_io_report(session_id="default", ctx=ctx)

        data = json.loads(result)
        assert data["total_ports"] == 20
        assert data["gt_ports"] == 16
        assert data["gpio_ports"] == 4


# ====================================================================== #
#  get_timing_report 测试
# ====================================================================== #


class TestGetTimingReport:
    """测试 get_timing_report 结构化报告工具。"""

    @pytest.mark.asyncio
    async def test_returns_formatted_report(self):
        """返回包含 PASS/FAIL 状态和路径详情的格式化报告。"""
        from vivado_mcp.tools.report_tools import get_timing_report

        timing_text = _load_fixture("sample_report_timing.txt")

        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=_make_tcl_result(timing_text)
        )

        ctx = _mock_context(session)

        with patch("vivado_mcp.tools.report_tools._require_session", return_value=session):
            result = await get_timing_report(session_id="default", ctx=ctx)

        assert "PASS" in result
        assert "WNS" in result
        assert "userclk2" in result
