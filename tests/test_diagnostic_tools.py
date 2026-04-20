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

    @pytest.fixture(autouse=True)
    def _isolate_home(self, tmp_path, monkeypatch):
        """把 Path.home() 劫持到 tmp_path,避免快照写入污染真实 ~/.claude/vivado-mcp/。

        每个 test 独立 tmp_path,互不干扰。
        """
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

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

        assert "未发现 ERROR 或 CRITICAL WARNING" in result
        assert "critical_warnings=0" in result

    @pytest.mark.asyncio
    async def test_extracts_error_details_when_errors_present(self):
        """errors>0 时必须提取 ERROR 详情(Bug 1 修复)。"""
        from vivado_mcp.tools.diagnostic_tools import get_critical_warnings

        # 模拟 DRC BIVC-1 场景:place_design 失败,3 条 ERROR
        err_log = (
            "VMCP_ERR:120|ERROR: [DRC BIVC-1] Bank IO standard Vcc: "
            "Conflicting Vcc voltages in bank 14. [basys3.xdc:15]\n"
            "VMCP_ERR:125|ERROR: [Vivado_Tcl 4-23] Error(s) found during DRC. "
            "Placer not run.\n"
            "VMCP_ERR:130|ERROR: [Common 17-39] 'place_design' failed due to "
            "earlier errors.\n"
            "VMCP_ERR_DONE"
        )

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result("VMCP_DIAG:errors=3,critical_warnings=0,warnings=0"),
                _make_tcl_result(err_log),
            ]
        )

        ctx = _mock_context(session)

        with patch("vivado_mcp.tools.diagnostic_tools._require_session", return_value=session):
            result = await get_critical_warnings(
                run_name="impl_1", session_id="default", ctx=ctx
            )

        assert "!! 发现 3 条 ERROR !!" in result
        assert "DRC BIVC-1" in result
        assert "IO_STANDARD_MISMATCH" in result
        assert "Vivado_Tcl 4-23" in result
        assert "DRC_FAILED" in result
        assert "errors=3" in result

    @pytest.mark.asyncio
    async def test_extracts_both_error_and_cw(self):
        """同时有 ERROR 和 CW 时两者都要展示,ERROR 先于 CW。"""
        from vivado_mcp.tools.diagnostic_tools import get_critical_warnings

        err_log = (
            "VMCP_ERR:100|ERROR: [DRC BIVC-1] Bank voltage conflict in bank 14.\n"
            "VMCP_ERR_DONE"
        )
        cw_log = (
            "VMCP_CW:50|CRITICAL WARNING: [DRC NSTD-1] port uart_rxd has no IOSTANDARD.\n"
            "VMCP_CW_DONE"
        )

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result("VMCP_DIAG:errors=1,critical_warnings=1,warnings=2"),
                _make_tcl_result(err_log),
                _make_tcl_result(cw_log),
            ]
        )

        ctx = _mock_context(session)

        with patch("vivado_mcp.tools.diagnostic_tools._require_session", return_value=session):
            result = await get_critical_warnings(
                run_name="impl_1", session_id="default", ctx=ctx
            )

        # ERROR 优先于 CW
        assert "!! 发现 1 条 ERROR !!" in result
        assert "=== ERROR 详情 ===" in result
        assert "=== CRITICAL WARNING 详情 ===" in result
        # ERROR 区块必须在 CW 区块之前
        assert result.index("=== ERROR 详情 ===") < result.index("=== CRITICAL WARNING 详情 ===")
        assert "DRC BIVC-1" in result
        assert "DRC NSTD-1" in result

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
#  get_critical_warnings 的 compare_with_last 差分功能
# ====================================================================== #


class TestCompareWithLast:
    """测试 compare_with_last=True 时的快照 + 差分行为。

    关键:用 monkeypatch 把 Path.home() 改到 tmp_path,避免污染真实 ~/。
    """

    def _make_session_with_cws(self, cw_messages: list[str], proj_dir: str = ""):
        """构造一个按序返回 counts → extract CW → query project_dir 的 session。"""
        vmcp_cw = "\n".join(
            f"VMCP_CW:{i + 1}|{msg}" for i, msg in enumerate(cw_messages)
        )
        vmcp_cw += "\nVMCP_CW_DONE"
        projdir_out = (
            f"VMCP_PROJDIR:{proj_dir}" if proj_dir else "VMCP_PROJDIR:NONE"
        )
        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                # 1. COUNT_WARNINGS
                _make_tcl_result(
                    f"VMCP_DIAG:errors=0,critical_warnings={len(cw_messages)},warnings=0"
                ),
                # 2. EXTRACT_CRITICAL_WARNINGS
                _make_tcl_result(vmcp_cw),
                # 3. _QUERY_PROJECT_DIR
                _make_tcl_result(projdir_out),
            ]
        )
        return session

    @pytest.mark.asyncio
    async def test_first_call_writes_snapshot_no_diff_section(
        self, tmp_path, monkeypatch
    ):
        """第一次调(无上次快照):写快照,报告不含差分段。"""
        from vivado_mcp.tools.diagnostic_tools import get_critical_warnings

        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        proj = tmp_path / "proj"
        proj.mkdir()

        session = self._make_session_with_cws(
            [
                "CRITICAL WARNING: [Vivado 12-1411] GT pin conflict.",
                "CRITICAL WARNING: [DRC BIVC-1] Bank voltage conflict.",
            ],
            proj_dir=str(proj),
        )

        ctx = _mock_context(session)

        # 默认 compare_with_last=False:不应含差分段
        with patch(
            "vivado_mcp.tools.diagnostic_tools._require_session",
            return_value=session,
        ):
            result = await get_critical_warnings(
                run_name="impl_1", session_id="default", ctx=ctx
            )

        assert "差分报告" not in result
        # 但快照必须已经落盘到 proj/.vmcp/
        snapshot_path = proj / ".vmcp" / "last_cw_impl_1.json"
        assert snapshot_path.exists()

    @pytest.mark.asyncio
    async def test_compare_without_prior_snapshot_shows_baseline_hint(
        self, tmp_path, monkeypatch
    ):
        """compare_with_last=True 但无上次快照:提示"基线已保存"。"""
        from vivado_mcp.tools.diagnostic_tools import get_critical_warnings

        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        proj = tmp_path / "proj2"
        proj.mkdir()

        session = self._make_session_with_cws(
            ["CRITICAL WARNING: [Vivado 12-1411] GT conflict."],
            proj_dir=str(proj),
        )
        ctx = _mock_context(session)

        with patch(
            "vivado_mcp.tools.diagnostic_tools._require_session",
            return_value=session,
        ):
            result = await get_critical_warnings(
                run_name="impl_1",
                compare_with_last=True,
                session_id="default",
                ctx=ctx,
            )

        assert "差分报告" in result
        assert "基线" in result or "无上次快照" in result

    @pytest.mark.asyncio
    async def test_second_call_compare_shows_resolved_and_persistent(
        self, tmp_path, monkeypatch
    ):
        """第一次调存 2 条 CW,第二次只剩 1 条 → 差分应显示 1 条消除 + 1 条仍存在。"""
        from vivado_mcp.tools.diagnostic_tools import get_critical_warnings

        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        proj = tmp_path / "proj3"
        proj.mkdir()

        # 第一次:2 条 CW
        session1 = self._make_session_with_cws(
            [
                "CRITICAL WARNING: [Vivado 12-1411] pcie GT pin conflict.",
                "CRITICAL WARNING: [DRC BIVC-1] bank 14 voltage conflict.",
            ],
            proj_dir=str(proj),
        )
        ctx1 = _mock_context(session1)

        with patch(
            "vivado_mcp.tools.diagnostic_tools._require_session",
            return_value=session1,
        ):
            await get_critical_warnings(
                run_name="impl_1", session_id="default", ctx=ctx1
            )

        # 第二次:用户修掉了 GT_PIN_CONFLICT,只剩 BIVC-1
        session2 = self._make_session_with_cws(
            ["CRITICAL WARNING: [DRC BIVC-1] bank 14 voltage conflict."],
            proj_dir=str(proj),
        )
        ctx2 = _mock_context(session2)

        with patch(
            "vivado_mcp.tools.diagnostic_tools._require_session",
            return_value=session2,
        ):
            result = await get_critical_warnings(
                run_name="impl_1",
                compare_with_last=True,
                session_id="default",
                ctx=ctx2,
            )

        assert "差分报告" in result
        assert "已消除 1 条" in result
        assert "仍存在 1 条" in result
        # 已消除里应提到 Vivado 12-1411
        assert "Vivado 12-1411" in result
        # 仍存在里应提到 BIVC-1
        assert "DRC BIVC-1" in result
        # 由于只减不增,结论应是"修复方向正确"
        assert "修复方向正确" in result

    @pytest.mark.asyncio
    async def test_compare_detects_newly_added(self, tmp_path, monkeypatch):
        """第一次 1 条,第二次变成另一条不同的 CW → 1 消除 + 1 新出现。"""
        from vivado_mcp.tools.diagnostic_tools import get_critical_warnings

        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        proj = tmp_path / "proj4"
        proj.mkdir()

        session1 = self._make_session_with_cws(
            ["CRITICAL WARNING: [Vivado 12-1411] GT pin conflict."],
            proj_dir=str(proj),
        )
        ctx1 = _mock_context(session1)

        with patch(
            "vivado_mcp.tools.diagnostic_tools._require_session",
            return_value=session1,
        ):
            await get_critical_warnings(
                run_name="impl_1", session_id="default", ctx=ctx1
            )

        # 第二次出现完全不同的 CW
        session2 = self._make_session_with_cws(
            ["CRITICAL WARNING: [DRC NSTD-1] port uart_tx no IOSTANDARD."],
            proj_dir=str(proj),
        )
        ctx2 = _mock_context(session2)

        with patch(
            "vivado_mcp.tools.diagnostic_tools._require_session",
            return_value=session2,
        ):
            result = await get_critical_warnings(
                run_name="impl_1",
                compare_with_last=True,
                session_id="default",
                ctx=ctx2,
            )

        assert "已消除 1 条" in result
        assert "新出现 1 条" in result
        assert "DRC NSTD-1" in result
        assert "新问题" in result or "检查" in result

    @pytest.mark.asyncio
    async def test_project_dir_none_still_snapshots_to_fallback(
        self, tmp_path, monkeypatch
    ):
        """没打开项目时快照也要落到 ~/.claude/vivado-mcp/ 不崩溃。"""
        from vivado_mcp.tools.diagnostic_tools import get_critical_warnings

        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        session = self._make_session_with_cws(
            ["CRITICAL WARNING: [Vivado 12-1411] GT conflict."],
            proj_dir="",  # 未打开项目,返回 NONE
        )
        ctx = _mock_context(session)

        with patch(
            "vivado_mcp.tools.diagnostic_tools._require_session",
            return_value=session,
        ):
            result = await get_critical_warnings(
                run_name="impl_1", session_id="default", ctx=ctx
            )

        # 没有错误、主报告正常
        assert "!! 发现 1 条 CRITICAL WARNING !!" in result
        # 快照落在 fallback 路径
        fallback = tmp_path / ".claude" / "vivado-mcp" / "last_cw_impl_1.json"
        assert fallback.exists()


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
        """force=True 时，跳过安全检查直接执行(0.3.8: Python 轮询架构)。"""
        from vivado_mcp.tools.flow_tools import generate_bitstream

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                # 1. launch_runs -to_step write_bitstream
                _make_tcl_result("Bitstream generation launched"),
                # 2. 轮询 — 一次就完成
                _make_tcl_result(
                    "VMCP_POLL|write_bitstream Complete!|100%|00:05:00"
                ),
                # 3. 查询比特流目录
                _make_tcl_result("VMCP_BITDIR:/project/impl_1"),
            ]
        )

        ctx = _mock_context(session)

        with patch("vivado_mcp.tools.flow_tools._require_session", return_value=session):
            result = await generate_bitstream(
                impl_run="impl_1", force=True, session_id="default", ctx=ctx
            )

        assert "安全检查未通过" not in result
        # force=True 跳过 CHECK_PRE_BITSTREAM,调用序列 = launch + poll + 查目录
        assert session.execute.call_count == 3
        assert "/project/impl_1" in result


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


class TestGetTimingReportWithPaths:
    """测试 get_timing_report 在 timing_met=False 时自动追加违例路径详情。"""

    # 构造一份 WNS 为负的 report_timing_summary 最小文本
    _VIOLATED_SUMMARY = """\
------------------------------------------------------------------------------------
| Design Timing Summary
| ---------------------
------------------------------------------------------------------------------------

    WNS(ns)      TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints      WHS(ns)      THS(ns)  THS Failing Endpoints  THS Total Endpoints
    -------      -------  ---------------------  -------------------      -------      -------  ---------------------  -------------------
     -1.234       -5.000                      5                  200       -0.080       -0.200                      2                  200
"""  # noqa: E501

    @pytest.mark.asyncio
    async def test_triggers_violating_paths_query_on_fail(self):
        """timing_met=False → 第三次 execute 必须跑 REPORT_VIOLATING_PATHS。"""
        from vivado_mcp.tcl_scripts import REPORT_VIOLATING_PATHS
        from vivado_mcp.tools.report_tools import get_timing_report

        violating_text = _load_fixture("sample_violating_paths.txt")

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                # 1. QUERY_DESIGN_STAGE
                _make_tcl_result(
                    "VMCP_STAGE:stage=post-route"
                    "|synth_status=synth_design Complete!"
                    "|impl_status=route_design Complete!"
                ),
                # 2. report_timing_summary -> 违例
                _make_tcl_result(self._VIOLATED_SUMMARY),
                # 3. REPORT_VIOLATING_PATHS
                _make_tcl_result(violating_text),
            ]
        )

        ctx = _mock_context(session)

        with patch("vivado_mcp.tools.report_tools._require_session", return_value=session):
            result = await get_timing_report(session_id="default", ctx=ctx)

        # 调用序列 = stage + summary + violating_paths
        assert session.execute.call_count == 3
        # 第三次调用必须是 REPORT_VIOLATING_PATHS 脚本本体
        third_call_cmd = session.execute.call_args_list[2].args[0]
        assert third_call_cmd == REPORT_VIOLATING_PATHS

        # 报告必须含违例路径段和具体模式标签
        assert "FAIL" in result
        assert "违例路径" in result
        assert "[CDC]" in result
        assert "建议:" in result

    @pytest.mark.asyncio
    async def test_skips_violating_paths_query_on_pass(self):
        """timing_met=True → 不跑 REPORT_VIOLATING_PATHS,省时间。"""
        from vivado_mcp.tools.report_tools import get_timing_report

        # sample_report_timing.txt 里 WNS=0.234, WHS=0.045 → timing_met=True
        timing_text = _load_fixture("sample_report_timing.txt")

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                # 1. QUERY_DESIGN_STAGE
                _make_tcl_result(
                    "VMCP_STAGE:stage=post-route"
                    "|synth_status=synth_design Complete!"
                    "|impl_status=route_design Complete!"
                ),
                # 2. report_timing_summary -> PASS
                _make_tcl_result(timing_text),
                # 不应该有第 3 次调用!
            ]
        )

        ctx = _mock_context(session)

        with patch("vivado_mcp.tools.report_tools._require_session", return_value=session):
            result = await get_timing_report(session_id="default", ctx=ctx)

        # 只调两次:stage + summary
        assert session.execute.call_count == 2
        assert "PASS" in result
        assert "违例路径 Top" not in result

    @pytest.mark.asyncio
    async def test_graceful_degrade_on_violating_paths_error(self):
        """违例路径查询失败不阻断主报告,末尾加一行提示。"""
        from vivado_mcp.tools.report_tools import get_timing_report

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result(
                    "VMCP_STAGE:stage=post-route"
                    "|synth_status=synth_design Complete!"
                    "|impl_status=route_design Complete!"
                ),
                _make_tcl_result(self._VIOLATED_SUMMARY),
                # 第三次返回错误
                _make_tcl_result("ERROR: some tcl error", return_code=1),
            ]
        )

        ctx = _mock_context(session)

        with patch("vivado_mcp.tools.report_tools._require_session", return_value=session):
            result = await get_timing_report(session_id="default", ctx=ctx)

        # 主报告仍然返回,但末尾有失败提示
        assert "FAIL" in result
        assert "违例路径查询失败" in result
        # 不应该含正常的违例段
        assert "违例路径 Top" not in result
