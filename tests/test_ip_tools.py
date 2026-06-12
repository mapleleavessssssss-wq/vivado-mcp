"""IP 工具集成测试。

通过 mock VivadoSession 和 MCP Context，测试 ip_tools 中
inspect_ip_params / compare_xci 工具的端到端行为。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from vivado_mcp.vivado.tcl_utils import TclResult

_FIXTURES = Path(__file__).parent / "fixtures"


# ====================================================================== #
#  共享 mock 辅助
# ====================================================================== #


def _make_tcl_result(output: str, return_code: int = 0) -> TclResult:
    """构造 TclResult 实例。"""
    return TclResult(output=output, return_code=return_code, is_error=return_code != 0)


def _mock_context(session=None):
    """创建模拟的 MCP Context。"""
    ctx = MagicMock()
    ctx.report_progress = AsyncMock()
    return ctx


# ====================================================================== #
#  compare_xci 测试
# ====================================================================== #


class TestCompareXci:
    """测试 compare_xci 工具。"""

    @pytest.mark.asyncio
    async def test_compare_fixtures(self):
        """对比两个 fixture XCI 文件，返回差异报告。"""
        from vivado_mcp.tools.ip_tools import compare_xci

        file_a = str(_FIXTURES / "sample_a.xci")
        file_b = str(_FIXTURES / "sample_b.xci")

        ctx = _mock_context()

        result = await compare_xci(
            file_a=file_a, file_b=file_b, show_all=False, ctx=ctx
        )

        assert "XCI 配置对比" in result
        assert "PF0_DEVICE_ID" in result
        assert "9024" in result
        assert "9038" in result
        assert "差异参数" in result

    @pytest.mark.asyncio
    async def test_compare_show_all(self):
        """show_all=True 显示所有参数。"""
        from vivado_mcp.tools.ip_tools import compare_xci

        file_a = str(_FIXTURES / "sample_a.xci")
        file_b = str(_FIXTURES / "sample_b.xci")

        ctx = _mock_context()

        result = await compare_xci(
            file_a=file_a, file_b=file_b, show_all=True, ctx=ctx
        )

        assert "所有参数" in result
        assert "REF_CLK_FREQ" in result

    @pytest.mark.asyncio
    async def test_compare_same_file(self):
        """同一文件对比，显示完全相同。"""
        from vivado_mcp.tools.ip_tools import compare_xci

        file_a = str(_FIXTURES / "sample_a.xci")

        ctx = _mock_context()

        result = await compare_xci(
            file_a=file_a, file_b=file_a, show_all=False, ctx=ctx
        )

        assert "完全相同" in result

    @pytest.mark.asyncio
    async def test_file_not_found(self):
        """文件不存在时返回错误。"""
        from vivado_mcp.tools.ip_tools import compare_xci

        ctx = _mock_context()

        result = await compare_xci(
            file_a="/nonexistent/a.xci",
            file_b=str(_FIXTURES / "sample_b.xci"),
            ctx=ctx,
        )

        assert "[ERROR]" in result
        assert "文件 A" in result

    @pytest.mark.asyncio
    async def test_invalid_extension(self, tmp_path):
        """非 .xci 扩展名返回错误。"""
        from vivado_mcp.tools.ip_tools import compare_xci

        bad_file = tmp_path / "test.xml"
        bad_file.write_text("<root/>")

        ctx = _mock_context()

        result = await compare_xci(
            file_a=str(bad_file),
            file_b=str(_FIXTURES / "sample_b.xci"),
            ctx=ctx,
        )

        assert "[ERROR]" in result
        assert "扩展名" in result


# ====================================================================== #
#  inspect_ip_params 测试
# ====================================================================== #


_SAMPLE_IP_OUTPUT = """\
VMCP_IP_INFO:xilinx.com:ip:xdma:4.1
VMCP_IP_PARAM:CONFIG.PCIE_BOARD_INTERFACE|pci_express_x4
VMCP_IP_PARAM:CONFIG.PF0_DEVICE_ID|9024
VMCP_IP_PARAM:CONFIG.PL_LINK_CAP_MAX_LINK_SPEED|5.0_GT/s
VMCP_IP_PARAM:CONFIG.PCIE_GT_DEVICE|GTX
VMCP_IP_PARAM:CONFIG.GT_LOC_NUM|4
VMCP_IP_PARAM_DONE
"""

_IP_NOT_FOUND_OUTPUT = """\
VMCP_IP_PARAM_ERROR:IP 'bad_ip' not found
"""


class TestInspectIpParams:
    """测试 inspect_ip_params 工具。"""

    @pytest.mark.asyncio
    async def test_returns_param_report(self):
        """正常查询返回参数报告。"""
        from vivado_mcp.tools.ip_tools import inspect_ip_params

        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=_make_tcl_result(_SAMPLE_IP_OUTPUT)
        )

        ctx = _mock_context(session)

        with patch("vivado_mcp.tools.ip_tools._require_session", return_value=session):
            result = await inspect_ip_params(
                ip_name="xdma_0", session_id="default", ctx=ctx
            )

        assert "xdma_0" in result
        assert "xilinx.com:ip:xdma:4.1" in result
        assert "PF0_DEVICE_ID" in result
        assert "9024" in result

    @pytest.mark.asyncio
    async def test_filter_keyword(self):
        """使用 filter_keyword 过滤参数。"""
        from vivado_mcp.tools.ip_tools import inspect_ip_params

        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=_make_tcl_result(_SAMPLE_IP_OUTPUT)
        )

        ctx = _mock_context(session)

        with patch("vivado_mcp.tools.ip_tools._require_session", return_value=session):
            result = await inspect_ip_params(
                ip_name="xdma_0", filter_keyword="gt",
                session_id="default", ctx=ctx,
            )

        assert "过滤关键词" in result
        assert "PCIE_GT_DEVICE" in result
        assert "GT_LOC_NUM" in result
        # 不应包含不匹配的参数
        assert "REF_CLK_FREQ" not in result

    @pytest.mark.asyncio
    async def test_ip_not_found(self):
        """IP 不存在时返回错误。"""
        from vivado_mcp.tools.ip_tools import inspect_ip_params

        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=_make_tcl_result(_IP_NOT_FOUND_OUTPUT)
        )

        ctx = _mock_context(session)

        with patch("vivado_mcp.tools.ip_tools._require_session", return_value=session):
            result = await inspect_ip_params(
                ip_name="bad_ip", session_id="default", ctx=ctx
            )

        assert "[ERROR]" in result
        assert "未找到" in result

    @pytest.mark.asyncio
    async def test_no_session(self):
        """会话不存在时返回错误。"""
        from vivado_mcp.tools.ip_tools import inspect_ip_params

        ctx = _mock_context(None)

        with patch("vivado_mcp.tools.ip_tools._require_session", return_value=None):
            result = await inspect_ip_params(
                ip_name="xdma_0", session_id="nonexistent", ctx=ctx
            )

        assert "[ERROR]" in result

    @pytest.mark.asyncio
    async def test_invalid_ip_name(self):
        """非法 IP 名称被拦截。"""
        from vivado_mcp.tools.ip_tools import inspect_ip_params

        ctx = _mock_context(None)
        result = await inspect_ip_params(
            ip_name="xdma;rm -rf /", session_id="default", ctx=ctx
        )
        assert "[ERROR]" in result

    @pytest.mark.asyncio
    async def test_execute_exception(self):
        """Tcl 执行异常时返回错误。"""
        from vivado_mcp.tools.ip_tools import inspect_ip_params

        session = AsyncMock()
        session.execute = AsyncMock(side_effect=TimeoutError("超时"))

        ctx = _mock_context(session)

        with patch("vivado_mcp.tools.ip_tools._require_session", return_value=session):
            result = await inspect_ip_params(
                ip_name="xdma_0", session_id="default", ctx=ctx
            )

        assert "[ERROR]" in result
        assert "查询 IP 参数失败" in result

    @pytest.mark.asyncio
    async def test_tcl_error_not_fed_to_parser(self):
        """Tcl 报错(如无项目打开)→ 透传原始错误,不产出"0 参数"假报告。"""
        from vivado_mcp.tools.ip_tools import inspect_ip_params

        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=_make_tcl_result(
                "ERROR: [Common 17-53] User Exception: No open project. "
                "Please open or create a project before executing this command.",
                return_code=1,
            )
        )
        ctx = _mock_context(session)

        with patch("vivado_mcp.tools.ip_tools._require_session", return_value=session):
            result = await inspect_ip_params(
                ip_name="xdma_0", session_id="default", ctx=ctx
            )

        assert "[ERROR]" in result
        # 真错误必须透传
        assert "Common 17-53" in result
        # 不能伪装成干净的"没有参数"报告
        assert "没有 CONFIG" not in result
        assert "总参数数: 0" not in result
