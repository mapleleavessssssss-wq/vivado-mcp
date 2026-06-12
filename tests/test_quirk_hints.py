"""server.py quirk hint 框架测试:B1/B2/B3 新 hint + rc=0 触发语义 + 超时 hint。

背景(2026-06 审计 P1):quirks §10 记载的 open_wave_database 误报场景真机
签名是 rc=0 + 输出含 ERROR,旧框架只在 is_error 时评估 hint → 目标场景永不
触发。本文件锁住新语义:
  - _append_quirk_hints 对所有输出调用,触发函数自己基于 output/command 判断
  - error_only 条目(run 失败兜底诊断)仍只在 is_error 时评估
  - 超时类异常追加 "超时≠命令失败" 指引
  - 旧 rc=1 行为不回归(tests/test_session.py 的旧测试另有覆盖,这里再锁一层)
"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from vivado_mcp.server import (
    _append_quirk_hints,
    _looks_like_project_not_found,
    _looks_like_scripts_only_regen,
    _looks_like_wcfg_bom_corrupt,
    _safe_execute,
)
from vivado_mcp.vivado.tcl_utils import TclResult


def _session_returning(output: str, rc: int) -> AsyncMock:
    """构造一个 execute 返回固定 TclResult 的 mock session。"""
    session = AsyncMock()
    session.execute = AsyncMock(
        return_value=TclResult(output=output, return_code=rc, is_error=(rc != 0))
    )
    return session


class TestB1ScriptsOnlyRegen:
    """B1:launch_simulation -scripts_only → 脚本被重生,手动 append 会被擦。"""

    def test_trigger_positive(self):
        assert _looks_like_scripts_only_regen(
            "INFO: scripts generated", "launch_simulation -scripts_only"
        )

    def test_trigger_negative_no_scripts_only(self):
        assert not _looks_like_scripts_only_regen("", "launch_simulation")

    def test_trigger_negative_other_command(self):
        # -scripts_only 出现但不是 launch_simulation → 不触发
        assert not _looks_like_scripts_only_regen("", "synth_design -scripts_only")

    @pytest.mark.asyncio
    async def test_hint_appended_even_on_success(self):
        # B1 是信息型 hint:命令成功(rc=0)也要提示脚本会被重生
        session = _session_returning("INFO: scripts generated", 0)
        result = await _safe_execute(
            session, "launch_simulation -scripts_only", 30.0, "run_tcl"
        )
        assert "xsim -tclbatch" in result
        assert "quit 0" in result

    @pytest.mark.asyncio
    async def test_no_hint_on_plain_launch_simulation_success(self):
        session = _session_returning("INFO: launched", 0)
        result = await _safe_execute(session, "launch_simulation", 30.0, "run_tcl")
        assert "xsim -tclbatch" not in result


class TestB2WcfgBomCorrupt:
    """B2:wcfg 混中文 BOM 损坏 → 删除/改名后 xsim 自动重生。"""

    def test_trigger_positive_wavedata_id(self):
        assert _looks_like_wcfg_bom_corrupt(
            "WARNING: [Wavedata 42-472] cannot parse wcfg", "run 100ns"
        )

    def test_trigger_positive_parsing_error(self):
        assert _looks_like_wcfg_bom_corrupt("WCFG parsing ERROR at line 3", "run 100ns")

    def test_trigger_positive_invalid_byte_with_wcfg(self):
        # 'invalid byte' 是泛化措辞,须同时含 '.wcfg' 才触发(审计 P3 收紧)
        assert _looks_like_wcfg_bom_corrupt(
            "invalid byte sequence in file sim_1.wcfg", "run 100ns"
        )

    def test_trigger_negative_invalid_byte_without_wcfg(self):
        # 无 .wcfg 上下文的通用编码错误不触发,避免误导去删 wcfg
        assert not _looks_like_wcfg_bom_corrupt(
            "error: invalid byte sequence reading data.bin", "run 100ns"
        )

    def test_trigger_negative(self):
        assert not _looks_like_wcfg_bom_corrupt("INFO: all good", "run 100ns")

    @pytest.mark.asyncio
    async def test_hint_appended_on_rc0(self):
        # B2 典型形态:命令本身成功(rc=0),但 log 持续刷 wcfg 损坏告警
        session = _session_returning(
            "WARNING: [Wavedata 42-472] cannot parse wcfg", 0
        )
        result = await _safe_execute(session, "run 100ns", 30.0, "run_tcl")
        assert ".wcfg.bak" in result
        assert "自动重生" in result

    @pytest.mark.asyncio
    async def test_no_hint_on_clean_output(self):
        session = _session_returning("run completed", 0)
        result = await _safe_execute(session, "run 100ns", 30.0, "run_tcl")
        assert ".wcfg.bak" not in result


class TestB3ProjectNotFound:
    """B3:[Coretcl 2-27] Can't find specified project → 引导 glob 列候选。"""

    def test_trigger_positive(self):
        out = "ERROR: [Coretcl 2-27] Can't find specified project: C:/x/y.xpr"
        assert _looks_like_project_not_found(out, "open_project C:/x/y.xpr")

    def test_trigger_negative_id_only(self):
        # 只有 [Coretcl 2-27] 没有关键句 → 不触发
        assert not _looks_like_project_not_found(
            "ERROR: [Coretcl 2-27] some other failure", "open_project x"
        )

    def test_trigger_negative_text_only(self):
        # 只有文案没有消息 ID → 不触发
        assert not _looks_like_project_not_found(
            "Can't find specified project", "open_project x"
        )

    @pytest.mark.asyncio
    async def test_hint_appended_on_error(self):
        session = _session_returning(
            "ERROR: [Coretcl 2-27] Can't find specified project.", 1
        )
        result = await _safe_execute(
            session, "open_project C:/wrong/p.xpr", 30.0, "run_tcl"
        )
        assert "glob -nocomplain" in result
        assert "pwd" in result
        # Tcl glob 无 ** 递归语义:hint 必须教多层显式 glob,不得再出现 **/*.xpr
        assert "*/*/*.xpr" in result
        assert "**/*.xpr" not in result


class TestA1SpuriousRc0:
    """核心修复:quirks §10 记载误报场景 rc=0,hint 必须在 rc=0 时也能触发。"""

    @pytest.mark.asyncio
    async def test_a1_hint_fires_on_rc0_with_error_text(self):
        # 真机签名(quirks §10):rc=0 但输出含这条 err → A1 verify 三连必须出现
        out = "ERROR: [Common 17-39] 'open_wave_config' failed due to earlier errors."
        session = _session_returning(out, 0)
        result = await _safe_execute(
            session, "open_wave_database C:/p/x.wdb", 30.0, "run_tcl"
        )
        assert "current_sim" in result
        assert "current_wave_config" in result
        assert "get_scopes" in result

    @pytest.mark.asyncio
    async def test_a2_cleanup_skipped_on_rc0_spurious(self):
        # 审计 P2:rc=0 误报场景 A1/A2 同触发会给矛盾指引 —— A2 的
        # close_sim -force 会把刚加载成功的 sim 关掉。该场景只出 A1,不出 A2。
        out = "ERROR: [Common 17-39] 'open_wave_config' failed due to earlier errors."
        session = _session_returning(out, 0)
        result = await _safe_execute(
            session, "open_wave_database C:/p/x.wdb", 30.0, "run_tcl"
        )
        # A1 verify 三连仍在
        assert "current_wave_config" in result
        # A2 清理指引(close_sim -force / 重启进程)不得出现
        assert "close_sim -force" not in result
        assert "孤儿 sim handle" not in result

    @pytest.mark.asyncio
    async def test_error_only_hint_not_fired_on_success(self):
        # error_only 条目(run 失败兜底)在 rc=0 时不评估:
        # 成功跑完 launch_runs 不该被追加失败兜底指引
        session = _session_returning("launch_runs completed", 0)
        result = await _safe_execute(session, "launch_runs synth_1", 30.0, "run_tcl")
        assert "get_critical_warnings" not in result

    def test_append_quirk_hints_error_only_gate(self):
        # 直接调框架:同一输入,is_error 翻转决定 error_only 条目是否评估
        out = "nothing"
        cmd = "launch_runs synth_1"
        assert "get_critical_warnings" not in _append_quirk_hints(
            out, cmd, is_error=False
        )
        assert "get_critical_warnings" in _append_quirk_hints(out, cmd, is_error=True)


class TestTimeoutHint:
    """超时类异常 → 追加 "超时≠命令失败" 指引。"""

    @pytest.mark.asyncio
    async def test_timeout_appends_hint(self):
        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=asyncio.TimeoutError("命令执行超时（120s）")
        )
        result = await _safe_execute(
            session, "synth_design -top top", 120.0, "命令执行失败"
        )
        assert result.startswith("[ERROR]")
        # 原始异常 message 保留(错误处理铁律:不吞原因)
        assert "命令执行超时" in result
        assert "超时≠命令失败" in result
        assert "run_synthesis" in result

    @pytest.mark.asyncio
    async def test_builtin_timeout_error_also_matched(self):
        # gui_session / 未来实现可能抛 builtin TimeoutError(3.11+ 与 asyncio 同类)
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=TimeoutError("读取响应长度头超时"))
        result = await _safe_execute(session, "route_design", 30.0, "命令执行失败")
        assert "超时≠命令失败" in result

    @pytest.mark.asyncio
    async def test_non_timeout_exception_no_timeout_hint(self):
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=RuntimeError("Vivado 进程意外终止"))
        result = await _safe_execute(session, "puts 1", 30.0, "命令执行失败")
        assert "[ERROR]" in result
        assert "Vivado 进程意外终止" in result
        assert "超时≠命令失败" not in result


class TestRc1NonRegression:
    """旧 rc=1 行为不回归(test_session.py 旧测试之外这里再锁一层)。"""

    @pytest.mark.asyncio
    async def test_a1_a2_still_fire_on_rc1(self):
        out = "ERROR: [Common 17-39] 'open_wave_config' failed due to earlier errors."
        session = _session_returning(out, 1)
        result = await _safe_execute(
            session, "open_wave_database C:/p/x.wdb", 30.0, "run_tcl"
        )
        # A1 verify 三连 + A2 清理片段都在
        assert "current_sim" in result
        assert "close_sim" in result
        assert "stop_session" in result
        # 原始 err 不被改写(透传契约)
        assert out in result

    @pytest.mark.asyncio
    async def test_run_failure_hint_still_fires_on_rc1(self):
        session = _session_returning(
            "ERROR: [Common 17-39] 'launch_simulation' failed", 1
        )
        result = await _safe_execute(session, "launch_simulation", 30.0, "run_tcl")
        assert "get_critical_warnings" in result
