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
            "VMCP_RUNLOG_ERR:120|ERROR: [DRC BIVC-1] Bank IO standard Vcc: "
            "Conflicting Vcc voltages in bank 14. [basys3.xdc:15]\n"
            "VMCP_RUNLOG_ERR:125|ERROR: [Vivado_Tcl 4-23] Error(s) found during DRC. "
            "Placer not run.\n"
            "VMCP_RUNLOG_ERR:130|ERROR: [Common 17-39] 'place_design' failed due to "
            "earlier errors.\n"
            "VMCP_RUNLOG_ERR_DONE"
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
            "VMCP_RUNLOG_ERR:100|ERROR: [DRC BIVC-1] Bank voltage conflict in bank 14.\n"
            "VMCP_RUNLOG_ERR_DONE"
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

    @pytest.mark.asyncio
    async def test_count_tcl_error_passes_through_raw_error(self):
        """计数命令 Tcl 报错(如无项目打开)→ 透传原始错误,不误报"未找到 runme.log"。"""
        from vivado_mcp.tools.diagnostic_tools import get_critical_warnings

        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=_make_tcl_result(
                "ERROR: [Common 17-53] User Exception: No open project.",
                return_code=1,
            )
        )
        ctx = _mock_context(session)

        with patch(
            "vivado_mcp.tools.diagnostic_tools._require_session",
            return_value=session,
        ):
            result = await get_critical_warnings(
                run_name="impl_1", session_id="default", ctx=ctx
            )

        assert "[ERROR]" in result
        assert "Common 17-53" in result
        # 不能给出误导性的"去跑 run"指引
        assert "未找到 runme.log" not in result

    @pytest.mark.asyncio
    async def test_tail_n_out_of_range_rejected(self):
        """tail_n 超出 1~500 被拦截(对齐 get_run_progress 的 tail_lines 校验)。"""
        from vivado_mcp.tools.diagnostic_tools import get_critical_warnings

        ctx = _mock_context(None)
        for bad in (0, -5, 501):
            result = await get_critical_warnings(
                run_name="impl_1", tail_n=bad, session_id="default", ctx=ctx
            )
            assert "[ERROR]" in result
            assert "tail_n" in result

    @pytest.mark.asyncio
    async def test_tail_n_boundary_values_accepted(self):
        """tail_n=1 / 500 边界值放行(走到正常计数流程)。"""
        from vivado_mcp.tools.diagnostic_tools import get_critical_warnings

        for ok in (1, 500):
            session = AsyncMock()
            session.execute = AsyncMock(
                return_value=_make_tcl_result(
                    "VMCP_DIAG:errors=0,critical_warnings=0,warnings=0"
                )
            )
            ctx = _mock_context(session)
            with patch(
                "vivado_mcp.tools.diagnostic_tools._require_session",
                return_value=session,
            ):
                result = await get_critical_warnings(
                    run_name="synth_1", tail_n=ok, session_id="default", ctx=ctx
                )
            assert "tail_n 必须" not in result


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


# B4: QUERY_FILESET_OVERRIDES 的"无覆盖"标准输出
_EMPTY_OVERRIDES = "VMCP_FS_OVERRIDE:generic=\nVMCP_FS_OVERRIDE:verilog_define="


class TestLaunchAndWaitDiag:
    """测试 _launch_and_wait 中的自动诊断逻辑。

    D5 重写后的流程：overrides 查询(B4) → reset+launch → poll STATUS/PROGRESS
    → open_run → 诊断。mock 需要按这个顺序提供返回值。
    """

    @pytest.mark.asyncio
    async def test_warns_on_critical_warnings(self):
        """综合/实现完成后检测到 CW 时，首行插入醒目提示。"""
        from vivado_mcp.tools.flow_tools import _launch_and_wait

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                # 1. QUERY_FILESET_OVERRIDES (B4)
                _make_tcl_result(_EMPTY_OVERRIDES),
                # 2. reset_run + launch_runs
                _make_tcl_result("VMCP_RUN_LAUNCH|started|Running"),
                # 3. 第一次 poll：已 Complete
                _make_tcl_result(
                    "VMCP_POLL|synth_design Complete!|100%|00:05:30"
                ),
                # 4. open_run（自动）
                _make_tcl_result(""),
                # 5. COUNT_WARNINGS 诊断
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
                # 1. QUERY_FILESET_OVERRIDES (B4)
                _make_tcl_result(_EMPTY_OVERRIDES),
                # 2. launch
                _make_tcl_result("VMCP_RUN_LAUNCH|started|Running"),
                # 3. poll 已完成
                _make_tcl_result(
                    "VMCP_POLL|route_design Complete!|100%|00:10:00"
                ),
                # 4. open_run
                _make_tcl_result(""),
                # 5. 诊断
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

    @pytest.mark.asyncio
    async def test_applied_overrides_shown(self):
        """B4: generic / verilog_define 有值时原样进结果,空值明示「(无)」。"""
        from vivado_mcp.tools.flow_tools import _launch_and_wait

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result(
                    "VMCP_FS_OVERRIDE:generic=WIDTH=8 DEPTH=16\n"
                    "VMCP_FS_OVERRIDE:verilog_define="
                ),
                _make_tcl_result("VMCP_RUN_LAUNCH|started|Running"),
                _make_tcl_result("VMCP_POLL|synth_design Complete!|100%|00:01:00"),
                _make_tcl_result(""),
                _make_tcl_result("VMCP_DIAG:errors=0,critical_warnings=0,warnings=0"),
            ]
        )
        ctx = _mock_context(session)

        result = await _launch_and_wait(
            session, "synth_1", jobs=4, timeout_minutes=30, label="综合", ctx=ctx
        )

        assert "applied_generic: WIDTH=8 DEPTH=16" in result
        assert "applied_verilog_define: (无)" in result
        # 审计 P2:generic 有值时行尾追加 quirk 提醒(显示有值 ≠ 实际生效)
        assert "不保证综合实际生效" in result
        assert "bound to:" in result

    @pytest.mark.asyncio
    async def test_no_quirk_note_when_generic_empty(self):
        """B4: generic 为 (无) 时不追加 quirk 提醒(提醒只针对「显示有值」场景)。"""
        from vivado_mcp.tools.flow_tools import _launch_and_wait

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result(_EMPTY_OVERRIDES),
                _make_tcl_result("VMCP_RUN_LAUNCH|started|Running"),
                _make_tcl_result("VMCP_POLL|synth_design Complete!|100%|00:01:00"),
                _make_tcl_result(""),
                _make_tcl_result("VMCP_DIAG:errors=0,critical_warnings=0,warnings=0"),
            ]
        )
        ctx = _mock_context(session)

        result = await _launch_and_wait(
            session, "synth_1", jobs=4, timeout_minutes=30, label="综合", ctx=ctx
        )

        assert "applied_generic: (无)" in result
        assert "不保证综合实际生效" not in result

    @pytest.mark.asyncio
    async def test_overrides_query_error_degraded(self):
        """B4: overrides 查询 Tcl 报错时降级为 [DEGRADED],不阻塞主流程。"""
        from vivado_mcp.tools.flow_tools import _launch_and_wait

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result("ERROR: no such fileset sources_1", return_code=1),
                _make_tcl_result("VMCP_RUN_LAUNCH|started|Running"),
                _make_tcl_result("VMCP_POLL|synth_design Complete!|100%|00:01:00"),
                _make_tcl_result(""),
                _make_tcl_result("VMCP_DIAG:errors=0,critical_warnings=0,warnings=0"),
            ]
        )
        ctx = _mock_context(session)

        result = await _launch_and_wait(
            session, "synth_1", jobs=4, timeout_minutes=30, label="综合", ctx=ctx
        )

        assert "[DEGRADED]" in result
        assert "generic/verilog_define" in result
        assert "no such fileset" in result
        # 主流程不受影响
        assert "综合结果" in result

    @pytest.mark.asyncio
    async def test_diag_count_error_degraded(self):
        """诊断计数命令 Tcl 报错时输出 [DEGRADED],不打 errors=-1 误导 AI。"""
        from vivado_mcp.tools.flow_tools import _launch_and_wait

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result(_EMPTY_OVERRIDES),
                _make_tcl_result("VMCP_RUN_LAUNCH|started|Running"),
                _make_tcl_result("VMCP_POLL|synth_design Complete!|100%|00:01:00"),
                _make_tcl_result(""),
                # 诊断计数:Tcl 报错(如项目被关掉)
                _make_tcl_result("ERROR: [Common 17-53] No open project", return_code=1),
            ]
        )
        ctx = _mock_context(session)

        result = await _launch_and_wait(
            session, "synth_1", jobs=4, timeout_minutes=30, label="综合", ctx=ctx
        )

        assert "[DEGRADED] 诊断计数不可用" in result
        assert "Common 17-53" in result
        assert "errors=-1" not in result

    @pytest.mark.asyncio
    async def test_diag_count_sentinel_missing_degraded(self):
        """诊断输出无 VMCP_DIAG 标记(-1 哨兵)时输出 [DEGRADED] 而非负数。"""
        from vivado_mcp.tools.flow_tools import _launch_and_wait

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result(_EMPTY_OVERRIDES),
                _make_tcl_result("VMCP_RUN_LAUNCH|started|Running"),
                _make_tcl_result("VMCP_POLL|synth_design Complete!|100%|00:01:00"),
                _make_tcl_result(""),
                # rc=0 但输出没有 VMCP_DIAG 行 → parse 返回 (-1,-1,-1)
                _make_tcl_result("some unrelated output"),
            ]
        )
        ctx = _mock_context(session)

        result = await _launch_and_wait(
            session, "synth_1", jobs=4, timeout_minutes=30, label="综合", ctx=ctx
        )

        assert "[DEGRADED] 诊断计数不可用" in result
        assert "errors=-1" not in result

    @pytest.mark.asyncio
    async def test_wait_false_returns_job_without_polling(self):
        """异步模式只提交 run，复用 get_run_progress，不启动后台轮询任务。"""
        from vivado_mcp.tools.flow_tools import _launch_and_wait

        session = AsyncMock()
        session.session_id = "build-a"
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result(_EMPTY_OVERRIDES),
                _make_tcl_result("VMCP_RUN_LAUNCH|started|Running"),
            ]
        )
        ctx = _mock_context(session)

        result = await _launch_and_wait(
            session,
            "synth_1",
            jobs=4,
            timeout_minutes=30,
            label="综合",
            ctx=ctx,
            wait=False,
        )

        assert "综合已异步启动" in result
        assert "job_id: build-a:synth_1" in result
        assert "get_run_progress" in result
        assert session.execute.await_count == 2
        launch_tcl = session.execute.await_args_list[1].args[0]
        assert "*Running*" in launch_tcl
        assert launch_tcl.index("*Running*") < launch_tcl.index("reset_run synth_1")
        assert "launch_runs synth_1 -jobs 4" in launch_tcl

    @pytest.mark.asyncio
    async def test_running_run_is_not_reset(self):
        """同名 run 运行中时返回 BUSY，原子 Tcl 分支不会执行 reset。"""
        from vivado_mcp.tools.flow_tools import _launch_and_wait

        session = AsyncMock()
        session.session_id = "build-a"
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result(_EMPTY_OVERRIDES),
                _make_tcl_result("VMCP_RUN_LAUNCH|busy|synth_design Running"),
            ]
        )
        ctx = _mock_context(session)

        result = await _launch_and_wait(
            session,
            "synth_1",
            jobs=4,
            timeout_minutes=30,
            label="综合",
            ctx=ctx,
            wait=False,
        )

        assert result.startswith("[BUSY]")
        assert "未执行 reset_run" in result
        assert "get_run_progress" in result
        assert session.execute.await_count == 2

    @pytest.mark.asyncio
    async def test_run_synthesis_forwards_wait_false(self):
        """公开工具保留原名，通过 wait=False 进入异步分支。"""
        from vivado_mcp.tools.flow_tools import run_synthesis

        session = AsyncMock()
        session.session_id = "public"
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result(_EMPTY_OVERRIDES),
                _make_tcl_result("VMCP_RUN_LAUNCH|started|Running"),
            ]
        )
        ctx = _mock_context(session)

        with patch("vivado_mcp.tools.flow_tools._require_session", return_value=session):
            result = await run_synthesis(ctx=ctx, wait=False)

        assert "job_id: public:synth_1" in result
        assert session.execute.await_count == 2

    @pytest.mark.asyncio
    async def test_run_implementation_forwards_wait_false(self):
        """实现工具与综合工具共享相同的异步提交契约。"""
        from vivado_mcp.tools.flow_tools import run_implementation

        session = AsyncMock()
        session.session_id = "public"
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result(_EMPTY_OVERRIDES),
                _make_tcl_result("VMCP_RUN_LAUNCH|started|Running"),
            ]
        )
        ctx = _mock_context(session)

        with patch("vivado_mcp.tools.flow_tools._require_session", return_value=session):
            result = await run_implementation(ctx=ctx, wait=False)

        assert "job_id: public:impl_1" in result
        assert session.execute.await_count == 2


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
        # 没有降级发生,不该出现 DEGRADED
        assert "[DEGRADED]" not in result

    @pytest.mark.asyncio
    async def test_precheck_tcl_error_appends_degraded(self):
        """安全检查 Tcl 报错(run 不存在/日志不可读)→ 放行但成功返回带 [DEGRADED]。"""
        from vivado_mcp.tools.flow_tools import generate_bitstream

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                # 1. CHECK_PRE_BITSTREAM:Tcl 报错
                _make_tcl_result(
                    "ERROR: [Common 17-53] No open project", return_code=1
                ),
                # 2. launch_runs
                _make_tcl_result("launched"),
                # 3. poll 完成
                _make_tcl_result("VMCP_POLL|write_bitstream Complete!|100%|00:05:00"),
                # 4. 查比特流目录
                _make_tcl_result("VMCP_BITDIR:/project/impl_1"),
            ]
        )
        ctx = _mock_context(session)

        with patch("vivado_mcp.tools.flow_tools._require_session", return_value=session):
            result = await generate_bitstream(
                impl_run="impl_1", force=False, session_id="default", ctx=ctx
            )

        assert "比特流生成结果" in result
        assert "[DEGRADED] 前置 CW 安全检查未能执行" in result
        assert "Common 17-53" in result
        assert "未经 CRITICAL WARNING 门禁" in result

    @pytest.mark.asyncio
    async def test_precheck_sentinel_missing_appends_degraded(self):
        """安全检查输出无 VMCP_PRE_BIT 标记(cw_count=-1 哨兵)→ 同样显式 [DEGRADED]。"""
        from vivado_mcp.tools.flow_tools import generate_bitstream

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                # 1. CHECK_PRE_BITSTREAM:rc=0 但没有 VMCP_PRE_BIT 行
                _make_tcl_result("garbage output without marker"),
                _make_tcl_result("launched"),
                _make_tcl_result("VMCP_POLL|write_bitstream Complete!|100%|00:05:00"),
                _make_tcl_result("VMCP_BITDIR:/project/impl_1"),
            ]
        )
        ctx = _mock_context(session)

        with patch("vivado_mcp.tools.flow_tools._require_session", return_value=session):
            result = await generate_bitstream(
                impl_run="impl_1", force=False, session_id="default", ctx=ctx
            )

        assert "[DEGRADED] 前置 CW 安全检查未能执行" in result
        assert "VMCP_PRE_BIT" in result

    @pytest.mark.asyncio
    async def test_precheck_exception_appends_degraded(self):
        """安全检查 execute 抛异常(如超时)→ 放行但成功返回带 [DEGRADED] + 原因。"""
        from vivado_mcp.tools.flow_tools import generate_bitstream

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                TimeoutError("命令执行超时（30.0s）"),
                _make_tcl_result("launched"),
                _make_tcl_result("VMCP_POLL|write_bitstream Complete!|100%|00:05:00"),
                _make_tcl_result("VMCP_BITDIR:/project/impl_1"),
            ]
        )
        ctx = _mock_context(session)

        with patch("vivado_mcp.tools.flow_tools._require_session", return_value=session):
            result = await generate_bitstream(
                impl_run="impl_1", force=False, session_id="default", ctx=ctx
            )

        assert "[DEGRADED] 前置 CW 安全检查失败" in result
        assert "超时" in result

    @pytest.mark.asyncio
    async def test_precheck_clean_no_degraded(self):
        """安全检查正常(cw=0)时放行且无 [DEGRADED](负例)。"""
        from vivado_mcp.tools.flow_tools import generate_bitstream

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result(
                    "VMCP_PRE_BIT:status=route_design Complete,critical_warnings=0\n"
                    "VMCP_PRE_BIT_DONE"
                ),
                _make_tcl_result("launched"),
                _make_tcl_result("VMCP_POLL|write_bitstream Complete!|100%|00:05:00"),
                _make_tcl_result("VMCP_BITDIR:/project/impl_1"),
            ]
        )
        ctx = _mock_context(session)

        with patch("vivado_mcp.tools.flow_tools._require_session", return_value=session):
            result = await generate_bitstream(
                impl_run="impl_1", force=False, session_id="default", ctx=ctx
            )

        assert "比特流生成结果" in result
        assert "[DEGRADED]" not in result


# ====================================================================== #
#  program_device 参数校验测试
# ====================================================================== #


class TestProgramDeviceValidation:
    """program_device 的 target / hw_server_url 轻量校验。"""

    def _make_bit(self, tmp_path):
        bit = tmp_path / "top.bit"
        bit.write_bytes(b"\x00fake bitstream")
        return str(bit)

    @pytest.mark.asyncio
    async def test_invalid_target_rejected(self, tmp_path):
        """target 含空格/Tcl 特殊字符 → 清晰报错,不碰 session。"""
        from vivado_mcp.tools.flow_tools import program_device

        session = AsyncMock()
        ctx = _mock_context(session)

        with patch("vivado_mcp.tools.flow_tools._require_session", return_value=session):
            result = await program_device(
                bitstream_path=self._make_bit(tmp_path),
                target="bad target;exec",
                ctx=ctx,
            )

        assert "[ERROR]" in result
        assert "target" in result
        # 审计 P3:报错文案是黑名单口吻(与黑名单实现对齐),不再宣称白名单
        assert "不允许空白" in result
        session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_hw_server_url_rejected(self, tmp_path):
        """hw_server_url 空串 → 清晰报错。"""
        from vivado_mcp.tools.flow_tools import program_device

        session = AsyncMock()
        ctx = _mock_context(session)

        with patch("vivado_mcp.tools.flow_tools._require_session", return_value=session):
            result = await program_device(
                bitstream_path=self._make_bit(tmp_path),
                hw_server_url="",
                ctx=ctx,
            )

        assert "[ERROR]" in result
        assert "hw_server_url" in result
        session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_default_params_pass_validation(self, tmp_path):
        """默认 target='*' + localhost:3121 合法,正常走到 Tcl 执行。"""
        from vivado_mcp.tools.flow_tools import program_device

        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=_make_tcl_result("编程完成: xc7a35t_0")
        )
        ctx = _mock_context(session)

        with patch("vivado_mcp.tools.flow_tools._require_session", return_value=session):
            result = await program_device(
                bitstream_path=self._make_bit(tmp_path), ctx=ctx
            )

        assert "编程完成" in result
        session.execute.assert_awaited_once()


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


# ====================================================================== #
#  get_critical_warnings: 非标错误 fallback(R1) + sim 路径(R3)
# ====================================================================== #


class TestNonStandardFallback:
    """errors=0 且 cw=0 但 STATUS=ERROR 时,tail runme.log 扫非标错误。"""

    @pytest.fixture(autouse=True)
    def _isolate_home(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

    @pytest.mark.asyncio
    async def test_tclstackfree_surfaced_when_messagedb_empty(self):
        """TclStackFree 在 messageDb 看不见时,通过 tail 兜底应当浮出来。"""
        from vivado_mcp.tools.diagnostic_tools import get_critical_warnings

        # COUNT_WARNINGS 显示干净,但 STATUS=ERROR + TAIL_RUNME_LOG 末尾含 TclStackFree
        tail_output = (
            "VMCP_TAIL:status=synth_design ERROR\n"
            "VMCP_TAIL:total=1500\n"
            "VMCP_TAIL_LINE:1499|Phase 1.5 GENERATE_TARGET\n"
            "VMCP_TAIL_LINE:1500|TclStackFree: incorrect freePtr 0x0123\n"
            "VMCP_TAIL_DONE\n"
        )

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result("VMCP_DIAG:errors=0,critical_warnings=0,warnings=2"),
                _make_tcl_result("VMCP_PROJDIR:NONE"),
                _make_tcl_result(tail_output),
            ]
        )
        ctx = _mock_context(session)

        with patch(
            "vivado_mcp.tools.diagnostic_tools._require_session",
            return_value=session,
        ):
            result = await get_critical_warnings(
                run_name="synth_1", session_id="default", ctx=ctx
            )

        assert "非标错误" in result
        assert "TclStackFree" in result
        assert "messageDb 显示无" in result
        assert "1500" in result
        assert "ASCII" in result

    @pytest.mark.asyncio
    async def test_no_nonstandard_when_status_is_complete(self):
        """STATUS 不含 ERROR 时,即使 tail 命中关键词也不展示,避免对健康 run 误报。"""
        from vivado_mcp.tools.diagnostic_tools import get_critical_warnings

        # tail 命中 abort 字样,但 STATUS=Complete → 不展示
        tail_output = (
            "VMCP_TAIL:status=synth_design Complete!\n"
            "VMCP_TAIL_LINE:50|INFO: graceful abort handler installed\n"
            "VMCP_TAIL_DONE\n"
        )

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result("VMCP_DIAG:errors=0,critical_warnings=0,warnings=2"),
                _make_tcl_result("VMCP_PROJDIR:NONE"),
                _make_tcl_result(tail_output),
            ]
        )
        ctx = _mock_context(session)

        with patch(
            "vivado_mcp.tools.diagnostic_tools._require_session",
            return_value=session,
        ):
            result = await get_critical_warnings(
                run_name="synth_1", session_id="default", ctx=ctx
            )

        assert "未发现 ERROR 或 CRITICAL WARNING" in result
        assert "非标错误" not in result

    @pytest.mark.asyncio
    async def test_skips_tail_when_errors_present(self):
        """errors>0 时走原 ERROR 流程,不调 TAIL_RUNME_LOG(开销控制)。"""
        from vivado_mcp.tools.diagnostic_tools import get_critical_warnings

        err_log = (
            "VMCP_RUNLOG_ERR:50|ERROR: [Common 17-39] real error here\n"
            "VMCP_RUNLOG_ERR_DONE"
        )

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result("VMCP_DIAG:errors=1,critical_warnings=0,warnings=0"),
                _make_tcl_result(err_log),
                _make_tcl_result("VMCP_PROJDIR:NONE"),
            ]
        )
        # errors>0 时调用顺序:count → extract_errors → projdir,无 tail
        ctx = _mock_context(session)

        with patch(
            "vivado_mcp.tools.diagnostic_tools._require_session",
            return_value=session,
        ):
            result = await get_critical_warnings(
                run_name="synth_1", session_id="default", ctx=ctx
            )

        # 只 3 次 execute: count + extract_errors + projdir,没调 TAIL_RUNME_LOG
        assert session.execute.call_count == 3
        assert "Common 17-39" in result


class TestSimDiagnose:
    """run_name 是 sim_* 时,改走 TAIL_SIM_LOGS 独立路径。"""

    @pytest.mark.asyncio
    async def test_xvlog_not_found_surfaced(self):
        """xvlog 不在 PATH 的中文 cmd 报错,要能从 xvlog.log 提取出来。"""
        from vivado_mcp.tools.diagnostic_tools import get_critical_warnings

        sim_out = (
            "VMCP_SIM:sim_dir=C:/proj/proj.sim/sim_1\n"
            "VMCP_SIM:log_count=2\n"
            "VMCP_SIM_LOG_START:C:/proj/proj.sim/sim_1/behav/xsim/xvlog.log\n"
            "VMCP_SIM_LOG_LINE:1|Running xvlog\n"
            "VMCP_SIM_LOG_LINE:2|xvlog 不是内部或外部命令,也不是可运行的程序\n"
            "VMCP_SIM_LOG_END:C:/proj/proj.sim/sim_1/behav/xsim/xvlog.log\n"
            "VMCP_SIM_LOG_START:C:/proj/proj.sim/sim_1/behav/xsim/compile.log\n"
            "VMCP_SIM_LOG_LINE:5|INFO: USF-XSim-69 compile finished\n"
            "VMCP_SIM_LOG_END:C:/proj/proj.sim/sim_1/behav/xsim/compile.log\n"
            "VMCP_SIM_DONE\n"
        )

        session = AsyncMock()
        # 审计修复后:guard 提前 → logs 非空也会查一次 target_simulator
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result(sim_out),
                _make_tcl_result("VMCP_TARGET_SIM:XSim"),
            ]
        )
        ctx = _mock_context(session)

        with patch(
            "vivado_mcp.tools.diagnostic_tools._require_session",
            return_value=session,
        ):
            result = await get_critical_warnings(
                run_name="sim_1", session_id="default", ctx=ctx
            )

        assert "仿真诊断" in result
        assert "xvlog.log" in result
        assert "不是内部或外部命令" in result
        assert "not_recognized_cmd_zh" in result
        # R4: 状态污染提示
        assert "stop_session" in result or "重启" in result
        # XSim 时不出现陈旧日志头部注记
        assert "陈旧" not in result
        # 两次 execute:TAIL_SIM_LOGS + QUERY_TARGET_SIMULATOR
        assert session.execute.call_count == 2

    @pytest.mark.asyncio
    async def test_fileset_not_found(self):
        """sim fileset 不存在时给清晰引导。"""
        from vivado_mcp.tools.diagnostic_tools import get_critical_warnings

        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=_make_tcl_result(
                "VMCP_SIM:error=fileset_not_found\nVMCP_SIM_DONE\n"
            )
        )
        ctx = _mock_context(session)

        with patch(
            "vivado_mcp.tools.diagnostic_tools._require_session",
            return_value=session,
        ):
            result = await get_critical_warnings(
                run_name="sim_1", session_id="default", ctx=ctx
            )

        assert "[ERROR]" in result
        assert "找不到 fileset" in result

    @pytest.mark.asyncio
    async def test_no_logs_triggers_scripts_only_fallback(self):
        """sim 目录存在但 glob 不到 xsim/*.log → 0.3.15 走 scripts-only fallback。"""
        from vivado_mcp.tools.diagnostic_tools import get_critical_warnings

        # 顺序:TAIL_SIM_LOGS(空) → QUERY_TARGET_SIMULATOR(XSim) →
        #       LAUNCH_SCRIPTS_AND_GLOB(也没 .bat,fallback 空走)
        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result(
                    "VMCP_SIM:sim_dir=C:/proj/proj.sim/sim_1\n"
                    "VMCP_SIM:log_count=0\n"
                    "VMCP_SIM_DONE\n"
                ),
                _make_tcl_result("VMCP_TARGET_SIM:XSim"),
                _make_tcl_result(
                    "VMCP_BAT:sim_dir=C:/proj/proj.sim/sim_1\n"
                    "VMCP_BAT:scripts_only=already_present\n"
                    "VMCP_BAT_DONE\n"
                ),
            ]
        )
        ctx = _mock_context(session)

        with patch(
            "vivado_mcp.tools.diagnostic_tools._require_session",
            return_value=session,
        ):
            result = await get_critical_warnings(
                run_name="sim_1", session_id="default", ctx=ctx
            )

        # 走了 fallback 路径,有相关标签
        assert "scripts_only" in result.lower() or "scripts-only" in result.lower()
        # 调了三次:TAIL_SIM_LOGS + QUERY_TARGET_SIMULATOR + LAUNCH_SCRIPTS_AND_GLOB
        assert session.execute.call_count == 3

    @pytest.mark.asyncio
    async def test_all_empty_logs_treated_as_missing_triggers_fallback(self):
        """issue #2:spawn 秒死可能留下 0 字节日志,全空日志视同未生成走 fallback。

        此前一个空 compile.log 就击穿 `not logs` 条件,掉进"未命中关键词、
        加大 tail_n"的死胡同(空日志里不可能有可诊断内容)。
        """
        from vivado_mcp.tools.diagnostic_tools import get_critical_warnings

        sim_out = (
            "VMCP_SIM:sim_dir=C:/proj/proj.sim/sim_1\n"
            "VMCP_SIM:log_count=1\n"
            "VMCP_SIM_LOG_START:C:/proj/proj.sim/sim_1/behav/xsim/compile.log\n"
            "VMCP_SIM_LOG_END:C:/proj/proj.sim/sim_1/behav/xsim/compile.log\n"
            "VMCP_SIM_DONE\n"
        )
        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result(sim_out),
                _make_tcl_result("VMCP_TARGET_SIM:XSim"),
                _make_tcl_result(
                    "VMCP_BAT:sim_dir=C:/proj/proj.sim/sim_1\n"
                    "VMCP_BAT:scripts_only=already_present\n"
                    "VMCP_BAT_DONE\n"
                ),
            ]
        )
        ctx = _mock_context(session)

        with patch(
            "vivado_mcp.tools.diagnostic_tools._require_session",
            return_value=session,
        ):
            result = await get_critical_warnings(
                run_name="sim_1", session_id="default", ctx=ctx
            )

        assert "全为空" in result
        assert "scripts_only" in result.lower() or "scripts-only" in result.lower()
        # 不该掉进 tail 展示路径的"加大 tail_n"死胡同
        assert "加大 tail_n" not in result
        assert session.execute.call_count == 3

    @pytest.mark.asyncio
    async def test_third_party_simulator_guard(self):
        """target_simulator 非 XSim 时:日志拍空是预期,返回指引而不是误导诊断。"""
        from vivado_mcp.tools.diagnostic_tools import get_critical_warnings

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result(
                    "VMCP_SIM:sim_dir=C:/proj/proj.sim/sim_1\n"
                    "VMCP_SIM:log_count=0\n"
                    "VMCP_SIM_DONE\n"
                ),
                _make_tcl_result("VMCP_TARGET_SIM:Questa"),
                # 不该有第三次调用(不走 bat fallback)
            ]
        )
        ctx = _mock_context(session)

        with patch(
            "vivado_mcp.tools.diagnostic_tools._require_session",
            return_value=session,
        ):
            result = await get_critical_warnings(
                run_name="sim_1", session_id="default", ctx=ctx
            )

        assert "Questa" in result
        assert "compile_simlib" in result
        assert "questa" in result  # 日志子目录指引用小写
        # 不能给出 "spawn .bat 之前就炸了" 这种 XSim 专属误导结论
        assert "spawn .bat" not in result
        assert session.execute.call_count == 2

    @pytest.mark.asyncio
    async def test_third_party_simulator_stale_xsim_logs_flagged(self):
        """审计 P2:切到第三方仿真器后 */xsim/ 残留旧日志 → guard 不能被绕过,
        报告头部必须注明这是陈旧 XSim 日志并指向真日志目录。"""
        from vivado_mcp.tools.diagnostic_tools import get_critical_warnings

        sim_out = (
            "VMCP_SIM:sim_dir=C:/proj/proj.sim/sim_1\n"
            "VMCP_SIM:log_count=1\n"
            "VMCP_SIM_LOG_START:C:/proj/proj.sim/sim_1/behav/xsim/xvlog.log\n"
            "VMCP_SIM_LOG_LINE:1|old xsim run output\n"
            "VMCP_SIM_LOG_END:C:/proj/proj.sim/sim_1/behav/xsim/xvlog.log\n"
            "VMCP_SIM_DONE\n"
        )

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result(sim_out),
                _make_tcl_result("VMCP_TARGET_SIM:Questa"),
            ]
        )
        ctx = _mock_context(session)

        with patch(
            "vivado_mcp.tools.diagnostic_tools._require_session",
            return_value=session,
        ):
            result = await get_critical_warnings(
                run_name="sim_1", session_id="default", ctx=ctx
            )

        # 头部注记:陈旧 XSim 日志 + 当前仿真器 + 真日志目录(小写)
        assert "陈旧" in result
        assert "target_simulator=Questa" in result
        assert "/questa/" in result
        # 日志本身仍展示(透传),但已被标注
        assert "xvlog.log" in result
        assert session.execute.call_count == 2

    @pytest.mark.asyncio
    async def test_target_simulator_no_marker_logs_warning(self, caplog):
        """审计 P3:输出无 VMCP_TARGET_SIM 标记的第三条降级路径要留 warning 痕迹。"""
        import logging

        from vivado_mcp.tools.diagnostic_tools import _get_target_simulator

        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=_make_tcl_result("some unrelated output without marker")
        )

        with caplog.at_level(logging.WARNING):
            result = await _get_target_simulator(session)

        assert result is None
        messages = [rec.getMessage() for rec in caplog.records]
        # 降级原因要点名标记缺失,且带输出前 200 字片段
        assert any(
            "VMCP_TARGET_SIM" in m and "unrelated output" in m for m in messages
        )

    @pytest.mark.asyncio
    async def test_simulator_query_error_still_falls_back(self):
        """target_simulator 查询出错 → 按 XSim 继续 fallback(降级不阻断诊断)。"""
        from vivado_mcp.tools.diagnostic_tools import get_critical_warnings

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result(
                    "VMCP_SIM:sim_dir=C:/proj/proj.sim/sim_1\n"
                    "VMCP_SIM:log_count=0\n"
                    "VMCP_SIM_DONE\n"
                ),
                _make_tcl_result("VMCP_TARGET_SIM:error=no current project"),
                _make_tcl_result(
                    "VMCP_BAT:sim_dir=C:/proj/proj.sim/sim_1\n"
                    "VMCP_BAT:scripts_only=already_present\n"
                    "VMCP_BAT_DONE\n"
                ),
            ]
        )
        ctx = _mock_context(session)

        with patch(
            "vivado_mcp.tools.diagnostic_tools._require_session",
            return_value=session,
        ):
            result = await get_critical_warnings(
                run_name="sim_1", session_id="default", ctx=ctx
            )

        # 查询失败按 XSim 继续走 fallback,共 3 次 execute
        assert session.execute.call_count == 3
        assert "scripts" in result.lower()

    @pytest.mark.asyncio
    async def test_sim_path_skips_messagedb_queries(self):
        """sim_* 走独立路径:不调 COUNT_WARNINGS / EXTRACT_ERRORS / 快照。"""
        from vivado_mcp.tools.diagnostic_tools import get_critical_warnings

        session = AsyncMock()
        # TAIL_SIM_LOGS 空 → target_simulator → fallback LAUNCH_SCRIPTS_AND_GLOB 也空
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result(
                    "VMCP_SIM:sim_dir=/tmp\nVMCP_SIM:log_count=0\nVMCP_SIM_DONE\n"
                ),
                _make_tcl_result("VMCP_TARGET_SIM:XSim"),
                _make_tcl_result(
                    "VMCP_BAT:sim_dir=/tmp\nVMCP_BAT:scripts_only=already_present\n"
                    "VMCP_BAT_DONE\n"
                ),
            ]
        )
        ctx = _mock_context(session)

        with patch(
            "vivado_mcp.tools.diagnostic_tools._require_session",
            return_value=session,
        ):
            await get_critical_warnings(
                run_name="sim_1", session_id="default", ctx=ctx
            )

        # 三次 execute:TAIL_SIM_LOGS + QUERY_TARGET_SIMULATOR + LAUNCH_SCRIPTS_AND_GLOB,
        # 没调 COUNT_WARNINGS / EXTRACT_ERRORS / 快照查询
        assert session.execute.call_count == 3


def _bat_run_ok(out_lines: list[str]) -> str:
    """构造 RUN_BAT_STEP 协议:rc=0 + body 行。"""
    body = "\n".join(f"VMCP_BAT_RUN_LINE:{ln}" for ln in out_lines)
    return f"VMCP_BAT_RUN:rc=0\nVMCP_BAT_RUN_OUT_START\n{body}\nVMCP_BAT_RUN_OUT_END\n"


def _bat_run_fail(rc: int, out_lines: list[str]) -> str:
    body = "\n".join(f"VMCP_BAT_RUN_LINE:{ln}" for ln in out_lines)
    return f"VMCP_BAT_RUN:rc={rc}\nVMCP_BAT_RUN_OUT_START\n{body}\nVMCP_BAT_RUN_OUT_END\n"


class TestSimBatFallback:
    """0.3.16:Vivado session 内 ``exec cmd /c <full>`` 跑 .bat。

    返工自 0.3.15 — 那版用 Python subprocess,MCP 进程 PATH 不带 vivado/bin
    导致 xvlog/xelab 假阳性。现在改走 Vivado session 内 exec,PATH 自带 vivado/bin。
    """

    @pytest.mark.asyncio
    async def test_all_bat_steps_succeed_means_wrapper_failed(self):
        """compile + elaborate 都 rc=0 → "wrapper 失败而非脚本失败"结论。"""
        from vivado_mcp.tools.diagnostic_tools import get_critical_warnings

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                # 1. TAIL_SIM_LOGS:目录存在但无 *.log
                _make_tcl_result(
                    "VMCP_SIM:sim_dir=C:/proj/proj.sim/sim_1\n"
                    "VMCP_SIM:log_count=0\n"
                    "VMCP_SIM_DONE\n"
                ),
                # 2. QUERY_TARGET_SIMULATOR:XSim → 继续 fallback
                _make_tcl_result("VMCP_TARGET_SIM:XSim"),
                # 3. LAUNCH_SCRIPTS_AND_GLOB:.bat 已存在,glob 出三步
                _make_tcl_result(
                    "VMCP_BAT:sim_dir=C:/proj/proj.sim/sim_1\n"
                    "VMCP_BAT:scripts_only=already_present\n"
                    "VMCP_BAT_FILE:compile|bat|C:/proj/proj.sim/sim_1/behav/xsim/compile.bat\n"
                    "VMCP_BAT_FILE:elaborate|bat|C:/proj/proj.sim/sim_1/behav/xsim/elaborate.bat\n"
                    "VMCP_BAT_FILE:simulate|bat|C:/proj/proj.sim/sim_1/behav/xsim/simulate.bat\n"
                    "VMCP_BAT_DONE\n"
                ),
                # 4. RUN_BAT_STEP compile → rc=0
                _make_tcl_result(_bat_run_ok(['"xvlog ..."', "Vivado Simulator 2019.1"])),
                # 5. RUN_BAT_STEP elaborate → rc=0
                _make_tcl_result(_bat_run_ok(['"xelab ..."', "Completed static elaboration"])),
                # simulate 跳过,不会再 execute
            ]
        )
        ctx = _mock_context(session)

        with patch(
            "vivado_mcp.tools.diagnostic_tools._require_session",
            return_value=session,
        ):
            result = await get_critical_warnings(
                run_name="sim_1", session_id="default", ctx=ctx
            )

        assert "wrapper 失败" in result or "wrapper" in result
        assert "compile" in result
        assert "elaborate" in result
        assert "simulate" in result  # 路径回带,但跳过
        # session.execute 共 5 次:TAIL_SIM_LOGS + QUERY_TARGET_SIMULATOR +
        # LAUNCH_SCRIPTS_AND_GLOB + 2 步 RUN_BAT_STEP
        assert session.execute.call_count == 5

    def test_run_bat_step_template_renders_spawn_dead_branch(self):
        """RUN_BAT_STEP 模板 .format 渲染不炸,且含 errorcode 三分支(issue #2)。"""
        from vivado_mcp.tcl_scripts import RUN_BAT_STEP

        tcl = RUN_BAT_STEP.format(bat_path="C:/proj/proj.sim/sim_1/behav/xsim/compile.bat")
        assert 'eq "CHILDSTATUS"' in tcl
        assert 'eq "NONE"' in tcl
        assert "set __rc -4" in tcl
        assert "Tcl exec errorcode" in tcl
        # Linux 下 glob 到 .sh,必须按平台分派 runner(cmd /c 仅 Windows)
        assert "$::tcl_platform(platform)" in tcl
        assert "list sh $__bat" in tcl

    @pytest.mark.asyncio
    async def test_bat_step_killed_gives_av_conclusion_and_stops(self):
        """compile 步被外部终止(rc=-4)→ 杀软定向结论,不出假 ✓,后续步骤跳过。"""
        from vivado_mcp.tools.diagnostic_tools import get_critical_warnings

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result(
                    "VMCP_SIM:sim_dir=C:/proj/proj.sim/sim_1\n"
                    "VMCP_SIM:log_count=0\n"
                    "VMCP_SIM_DONE\n"
                ),
                _make_tcl_result("VMCP_TARGET_SIM:XSim"),
                _make_tcl_result(
                    "VMCP_BAT:sim_dir=C:/proj/proj.sim/sim_1\n"
                    "VMCP_BAT:scripts_only=already_present\n"
                    "VMCP_BAT_FILE:compile|bat|C:/proj/proj.sim/sim_1/behav/xsim/compile.bat\n"
                    "VMCP_BAT_FILE:elaborate|bat|C:/proj/proj.sim/sim_1/behav/xsim/elaborate.bat\n"
                    "VMCP_BAT_DONE\n"
                ),
                # RUN_BAT_STEP compile → rc=-4(exec 的子进程被杀/spawn 失败)
                _make_tcl_result(
                    _bat_run_fail(
                        -4,
                        ["(Tcl exec errorcode: CHILDKILLED 123 SIGKILL killed)"],
                    )
                ),
                # elaborate 因 stop_on_fail 跳过,不会再 execute
            ]
        )
        ctx = _mock_context(session)

        with patch(
            "vivado_mcp.tools.diagnostic_tools._require_session",
            return_value=session,
        ):
            result = await get_critical_warnings(
                run_name="sim_1", session_id="default", ctx=ctx
            )

        assert "未正常退出" in result
        assert "安全软件" in result
        assert "全部正常退出" not in result
        # elaborate 被跳过:4 次 execute(TAIL + TARGET_SIM + GLOB + compile)
        assert session.execute.call_count == 4
        assert "前一步失败,跳过" in result

    @pytest.mark.asyncio
    async def test_compile_step_failure_surfaces_stderr(self):
        """compile.bat rc != 0 → "真错就在 compile" + 展示 stderr。"""
        from vivado_mcp.tools.diagnostic_tools import get_critical_warnings

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result(
                    "VMCP_SIM:sim_dir=/tmp/proj.sim/sim_1\n"
                    "VMCP_SIM:log_count=0\n"
                    "VMCP_SIM_DONE\n"
                ),
                _make_tcl_result("VMCP_TARGET_SIM:XSim"),
                _make_tcl_result(
                    "VMCP_BAT:sim_dir=/tmp/proj.sim/sim_1\n"
                    "VMCP_BAT:scripts_only=ok\n"
                    "VMCP_BAT_FILE:compile|sh|/tmp/proj.sim/sim_1/behav/xsim/compile.sh\n"
                    "VMCP_BAT_FILE:elaborate|sh|/tmp/proj.sim/sim_1/behav/xsim/elaborate.sh\n"
                    "VMCP_BAT_DONE\n"
                ),
                # compile → rc=1 + stderr
                _make_tcl_result(
                    _bat_run_fail(1, ["ERROR: cannot find module 'top.v'"])
                ),
                # elaborate **不该被调到**(遇错即停),如果调了就 raise
            ]
        )
        ctx = _mock_context(session)

        with patch(
            "vivado_mcp.tools.diagnostic_tools._require_session",
            return_value=session,
        ):
            result = await get_critical_warnings(
                run_name="sim_1", session_id="default", ctx=ctx
            )

        # 调用次数:TAIL + TARGET_SIM + GLOB + compile RUN_BAT_STEP = 4(elaborate 跳过)
        assert session.execute.call_count == 4
        assert "cannot find module" in result
        assert "compile" in result
        # "真错很可能就在这一步" 结论
        assert "真错" in result or "returncode=1" in result

    @pytest.mark.asyncio
    async def test_scripts_only_failed_no_bats_generated(self):
        """launch_simulation -scripts_only 自己 catch 失败 → 没 .bat,fallback 不再
        spawn 任何东西。"""
        from vivado_mcp.tools.diagnostic_tools import get_critical_warnings

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result(
                    "VMCP_SIM:sim_dir=C:/proj/proj.sim/sim_1\n"
                    "VMCP_SIM:log_count=0\n"
                    "VMCP_SIM_DONE\n"
                ),
                _make_tcl_result("VMCP_TARGET_SIM:XSim"),
                _make_tcl_result(
                    "VMCP_BAT:sim_dir=C:/proj/proj.sim/sim_1\n"
                    "VMCP_BAT:scripts_only=triggering\n"
                    "VMCP_BAT:scripts_only_failed=some xilinx internal error\n"
                    "VMCP_BAT_DONE\n"
                ),
                # 不该调第 4 次,如果调了 side_effect StopIteration
            ]
        )
        ctx = _mock_context(session)

        with patch(
            "vivado_mcp.tools.diagnostic_tools._require_session",
            return_value=session,
        ):
            result = await get_critical_warnings(
                run_name="sim_1", session_id="default", ctx=ctx
            )

        # 三次:TAIL + TARGET_SIM + GLOB,没有任何 RUN_BAT_STEP
        assert session.execute.call_count == 3
        assert "未跑任何 .bat" in result or "failed:some xilinx" in result
