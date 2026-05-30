"""波形工具集成测试：set_wave_zoom / set_wave_analog。

通过 mock VivadoSession + MCP Context 测试工具壳行为，无需真实 Vivado：
- set_wave_zoom 调对了 wcfg_editor、unsaved/none 报错、重载错误路径、显式 wcfg；
- set_wave_analog 信号解析(全路径/显示名/glob)、设置成功/未命中/报错、min/max 缺失降级、参数拦截。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from vivado_mcp.vivado.tcl_utils import TclResult

# ====================================================================== #
#  共享 mock 辅助
# ====================================================================== #


def _make_tcl_result(output: str, return_code: int = 0) -> TclResult:
    return TclResult(output=output, return_code=return_code, is_error=return_code != 0)


def _mock_context():
    ctx = MagicMock()
    ctx.report_progress = AsyncMock()
    return ctx


# ====================================================================== #
#  set_wave_zoom 测试
# ====================================================================== #


class TestSetWaveZoom:
    """测试 set_wave_zoom 工具。"""

    @pytest.mark.asyncio
    async def test_happy_path_resolves_and_reloads(self):
        """自动解析 wcfg → 改 XML → 重载成功。"""
        from vivado_mcp.tools import wave_tools

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result("VMCP_WCFG:path=C:/proj/wave.wcfg"),  # RESOLVE
                _make_tcl_result("VMCP_RELOAD_OK:C:/proj/wave.wcfg"),  # RELOAD
            ]
        )
        ctx = _mock_context()

        with patch.object(wave_tools, "_require_session", return_value=session), \
             patch.object(wave_tools.wcfg_editor, "set_zoom_range") as mock_set:
            result = await wave_tools.set_wave_zoom(
                start_ns=100, end_ns=200, session_id="default", ctx=ctx
            )

        # 调对了 wcfg_editor，参数透传
        mock_set.assert_called_once_with("C:/proj/wave.wcfg", 100, 200)
        assert "[OK]" in result
        assert "100~200" in result

    @pytest.mark.asyncio
    async def test_explicit_wcfg_skips_resolve(self):
        """显式 wcfg 路径时不跑 RESOLVE，直接改 XML + 重载。"""
        from vivado_mcp.tools import wave_tools

        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=_make_tcl_result("VMCP_RELOAD_OK:D:/w/x.wcfg")
        )
        ctx = _mock_context()

        with patch.object(wave_tools, "_require_session", return_value=session), \
             patch.object(wave_tools.wcfg_editor, "set_zoom_range") as mock_set:
            result = await wave_tools.set_wave_zoom(
                start_ns=10, end_ns=20, wcfg="D:/w/x.wcfg", ctx=ctx
            )

        mock_set.assert_called_once_with("D:/w/x.wcfg", 10, 20)
        # 只调一次 execute(仅 RELOAD，无 RESOLVE)
        assert session.execute.await_count == 1
        assert "[OK]" in result

    @pytest.mark.asyncio
    async def test_unsaved_wcfg_returns_error(self):
        """current_wave_config FILE_NAME 空 → 报错要求先 save_wave_config。"""
        from vivado_mcp.tools import wave_tools

        session = AsyncMock()
        session.execute = AsyncMock(return_value=_make_tcl_result("VMCP_WCFG:unsaved"))
        ctx = _mock_context()

        with patch.object(wave_tools, "_require_session", return_value=session), \
             patch.object(wave_tools.wcfg_editor, "set_zoom_range") as mock_set:
            result = await wave_tools.set_wave_zoom(start_ns=1, end_ns=2, ctx=ctx)

        assert "[ERROR]" in result
        assert "save_wave_config" in result
        # 未保存时不应去改 XML
        mock_set.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_wave_config_returns_error(self):
        """无 current_wave_config → 报错。"""
        from vivado_mcp.tools import wave_tools

        session = AsyncMock()
        session.execute = AsyncMock(return_value=_make_tcl_result("VMCP_WCFG:none"))
        ctx = _mock_context()

        with patch.object(wave_tools, "_require_session", return_value=session), \
             patch.object(wave_tools.wcfg_editor, "set_zoom_range") as mock_set:
            result = await wave_tools.set_wave_zoom(start_ns=1, end_ns=2, ctx=ctx)

        assert "[ERROR]" in result
        assert "wave_config" in result
        mock_set.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_ge_end_rejected(self):
        """start_ns >= end_ns 被拦截(在拿 session 前)。"""
        from vivado_mcp.tools import wave_tools

        ctx = _mock_context()
        result = await wave_tools.set_wave_zoom(start_ns=200, end_ns=100, ctx=ctx)
        assert "[ERROR]" in result
        assert "小于" in result

    @pytest.mark.asyncio
    async def test_no_session(self):
        """会话不存在报错。"""
        from vivado_mcp.tools import wave_tools

        ctx = _mock_context()
        with patch.object(wave_tools, "_require_session", return_value=None):
            result = await wave_tools.set_wave_zoom(
                start_ns=1, end_ns=2, session_id="ghost", ctx=ctx
            )
        assert "[ERROR]" in result

    @pytest.mark.asyncio
    async def test_xml_edit_failure_propagates_reason(self):
        """wcfg_editor 抛 ValueError → 返回具体原因，不静默吞。"""
        from vivado_mcp.tools import wave_tools

        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=_make_tcl_result("VMCP_WCFG:path=C:/p/w.wcfg")
        )
        ctx = _mock_context()

        with patch.object(wave_tools, "_require_session", return_value=session), \
             patch.object(
                 wave_tools.wcfg_editor,
                 "set_zoom_range",
                 side_effect=ValueError("未找到 ZoomStartTime 节点"),
             ):
            result = await wave_tools.set_wave_zoom(start_ns=1, end_ns=2, ctx=ctx)

        assert "[ERROR]" in result
        assert "ZoomStartTime" in result

    @pytest.mark.asyncio
    async def test_reload_close_missing_force_error(self):
        """RELOAD 报 close 失败(漏 -force 类 42-26)→ 返回错误原因。"""
        from vivado_mcp.tools import wave_tools

        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[
                _make_tcl_result("VMCP_WCFG:path=C:/p/w.wcfg"),
                _make_tcl_result("VMCP_RELOAD_ERR:close|[Wavedata 42-26] ..."),
            ]
        )
        ctx = _mock_context()

        with patch.object(wave_tools, "_require_session", return_value=session), \
             patch.object(wave_tools.wcfg_editor, "set_zoom_range"):
            result = await wave_tools.set_wave_zoom(start_ns=1, end_ns=2, ctx=ctx)

        assert "[ERROR]" in result
        assert "42-26" in result


# ====================================================================== #
#  set_wave_analog 测试
# ====================================================================== #


class TestSetWaveAnalog:
    """测试 set_wave_analog 工具。"""

    @pytest.mark.asyncio
    async def test_set_ok(self):
        """信号解析+设置成功(ok=<resolved>)→ [OK]。"""
        from vivado_mcp.tools import wave_tools

        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=_make_tcl_result(
                "VMCP_ANALOG:sig=/tb/adc|ok=/tb/adc\nVMCP_ANALOG_DONE"
            )
        )
        ctx = _mock_context()

        with patch.object(wave_tools, "_require_session", return_value=session):
            result = await wave_tools.set_wave_analog(
                signals=["/tb/adc"], min=-2048, max=2047, ctx=ctx
            )

        assert "[OK]" in result
        assert "/tb/adc" in result
        assert "STYLE_ANALOG" in result

    @pytest.mark.asyncio
    async def test_resolution_reports_matched_path(self):
        """传 glob/显示名时 ok= 回报命中的全路径(get_waves 只匹配显示名的坑)。"""
        from vivado_mcp.tools import wave_tools

        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=_make_tcl_result(
                "VMCP_ANALOG:sig=y0*|ok=/tb/y0\nVMCP_ANALOG_DONE"
            )
        )
        ctx = _mock_context()

        with patch.object(wave_tools, "_require_session", return_value=session):
            result = await wave_tools.set_wave_analog(
                signals=["y0*"], min=-3200, max=3200, ctx=ctx
            )

        assert "[OK]" in result
        assert "/tb/y0" in result  # 命中的全路径回显

    @pytest.mark.asyncio
    async def test_signal_not_in_wave_warns(self):
        """get_waves 未命中(found=0)→ 提示先 add_wave。"""
        from vivado_mcp.tools import wave_tools

        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=_make_tcl_result(
                "VMCP_ANALOG:sig=/tb/missing|found=0\nVMCP_ANALOG_DONE"
            )
        )
        ctx = _mock_context()

        with patch.object(wave_tools, "_require_session", return_value=session):
            result = await wave_tools.set_wave_analog(
                signals=["/tb/missing"], min=-1, max=1, ctx=ctx
            )

        assert "[WARN]" in result
        assert "add_wave" in result

    @pytest.mark.asyncio
    async def test_missing_minmax_returns_error(self):
        """无显式 min/max → 报错引导显式传值(本版本不实现 auto 算幅)。"""
        from vivado_mcp.tools import wave_tools

        session = AsyncMock()
        ctx = _mock_context()

        with patch.object(wave_tools, "_require_session", return_value=session):
            result = await wave_tools.set_wave_analog(
                signals=["/tb/adc"], ctx=ctx
            )

        assert "[ERROR]" in result
        assert "min" in result and "max" in result
        # 没拿到幅度就不应执行 Tcl
        session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_only_min_given_treated_as_missing(self):
        """只给 min 不给 max → 仍按缺失处理(对称幅度需两边)。"""
        from vivado_mcp.tools import wave_tools

        session = AsyncMock()
        ctx = _mock_context()

        with patch.object(wave_tools, "_require_session", return_value=session):
            result = await wave_tools.set_wave_analog(
                signals=["/tb/adc"], min=-100, ctx=ctx
            )

        assert "[ERROR]" in result
        session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_signals_rejected(self):
        """signals 为空被拦截。"""
        from vivado_mcp.tools import wave_tools

        ctx = _mock_context()
        result = await wave_tools.set_wave_analog(signals=[], min=-1, max=1, ctx=ctx)
        assert "[ERROR]" in result

    @pytest.mark.asyncio
    async def test_invalid_interp_rejected(self):
        """interp 不在白名单被拦截(不去碰 session)。"""
        from vivado_mcp.tools import wave_tools

        ctx = _mock_context()
        result = await wave_tools.set_wave_analog(
            signals=["/tb/adc"], min=-1, max=1, interp="CUBIC", ctx=ctx
        )
        assert "[ERROR]" in result
        assert "白名单" in result

    @pytest.mark.asyncio
    async def test_hold_interp_accepted(self):
        """HOLD 在白名单，正常透传。"""
        from vivado_mcp.tools import wave_tools

        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=_make_tcl_result(
                "VMCP_ANALOG:sig=/tb/adc|ok=/tb/adc\nVMCP_ANALOG_DONE"
            )
        )
        ctx = _mock_context()

        with patch.object(wave_tools, "_require_session", return_value=session):
            result = await wave_tools.set_wave_analog(
                signals=["/tb/adc"], min=-1, max=1, interp="HOLD", ctx=ctx
            )

        assert "[OK]" in result
        # 校验 Tcl 里 interp 大写透传
        sent_tcl = session.execute.await_args.args[0]
        assert "AnalogInterpolation HOLD" in sent_tcl

    @pytest.mark.asyncio
    async def test_invalid_height_rejected(self):
        """height 非正整数被拦截。"""
        from vivado_mcp.tools import wave_tools

        ctx = _mock_context()
        result = await wave_tools.set_wave_analog(
            signals=["/tb/adc"], min=-1, max=1, height=0, ctx=ctx
        )
        assert "[ERROR]" in result

    @pytest.mark.asyncio
    async def test_no_session(self):
        """会话不存在报错。"""
        from vivado_mcp.tools import wave_tools

        ctx = _mock_context()
        with patch.object(wave_tools, "_require_session", return_value=None):
            result = await wave_tools.set_wave_analog(
                signals=["/tb/adc"], min=-1, max=1, session_id="ghost", ctx=ctx
            )
        assert "[ERROR]" in result

    @pytest.mark.asyncio
    async def test_signal_with_brace_rejected(self):
        """信号名含花括号被拒(防破坏 Tcl 花括号配对)。"""
        from vivado_mcp.tools import wave_tools

        session = AsyncMock()
        ctx = _mock_context()

        with patch.object(wave_tools, "_require_session", return_value=session):
            result = await wave_tools.set_wave_analog(
                signals=["/tb/sig{bad}"], min=-1, max=1, ctx=ctx
            )

        assert "[ERROR]" in result
        assert "花括号" in result
        session.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_bracket_signal_wrapped_in_braces(self):
        """含 [N] 的信号名用 {} 包裹进 Tcl(绕开命令替换)。"""
        from vivado_mcp.tools import wave_tools

        session = AsyncMock()
        session.execute = AsyncMock(
            return_value=_make_tcl_result(
                "VMCP_ANALOG:sig=/tb/gen[0].u/sig|ok=/tb/gen[0].u/sig\nVMCP_ANALOG_DONE"
            )
        )
        ctx = _mock_context()

        with patch.object(wave_tools, "_require_session", return_value=session):
            result = await wave_tools.set_wave_analog(
                signals=["/tb/gen[0].u/sig"], min=-1, max=1, ctx=ctx
            )

        assert "[OK]" in result
        sent_tcl = session.execute.await_args.args[0]
        assert "lappend __sigs {/tb/gen[0].u/sig}" in sent_tcl

    @pytest.mark.asyncio
    async def test_execute_exception_returns_reason(self):
        """Tcl 执行异常返回具体原因。"""
        from vivado_mcp.tools import wave_tools

        session = AsyncMock()
        session.execute = AsyncMock(side_effect=TimeoutError("超时"))
        ctx = _mock_context()

        with patch.object(wave_tools, "_require_session", return_value=session):
            result = await wave_tools.set_wave_analog(
                signals=["/tb/adc"], min=-1, max=1, ctx=ctx
            )

        assert "[ERROR]" in result
        assert "失败" in result


# ====================================================================== #
#  helper 单元测试
# ====================================================================== #


class TestHelpers:
    """直接测内部解析 helper。"""

    def test_parse_wcfg_path_variants(self):
        from vivado_mcp.tools.wave_tools import _parse_wcfg_path

        assert _parse_wcfg_path("VMCP_WCFG:none") == ("none", None)
        assert _parse_wcfg_path("VMCP_WCFG:unsaved") == ("unsaved", None)
        assert _parse_wcfg_path("VMCP_WCFG:path=C:/a/b.wcfg") == ("path", "C:/a/b.wcfg")
        # 无标记时降级 none
        assert _parse_wcfg_path("garbage") == ("none", None)

    def test_parse_reload(self):
        from vivado_mcp.tools.wave_tools import _parse_reload

        assert _parse_reload("VMCP_RELOAD_OK:x") is None
        assert _parse_reload("VMCP_RELOAD_ERR:open|boom") == "open|boom"
        # 缺标记不静默成功，返回明确说明
        assert _parse_reload("nothing") is not None

    def test_build_signals_block(self):
        from vivado_mcp.tools.wave_tools import _build_signals_block

        block = _build_signals_block(["/tb/a", "/tb/b[0]"])
        assert "lappend __sigs {/tb/a}" in block
        assert "lappend __sigs {/tb/b[0]}" in block

    def test_build_signals_block_rejects_brace(self):
        from vivado_mcp.tools.wave_tools import _build_signals_block

        with pytest.raises(ValueError, match="花括号"):
            _build_signals_block(["/tb/x}evil"])
