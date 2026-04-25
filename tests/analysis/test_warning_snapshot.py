"""warning_snapshot.py 单元测试。

覆盖:
- ``snapshot_cw`` + ``load_snapshot`` 往返一致
- ``diff_warnings`` 三种分类(resolved / newly_added / persistent)
- ``format_diff_report`` 输出包含关键标记
- 上次快照不存在时 load 返回 ``(None, [])`` 不抛异常
- 项目目录不可写时 fallback 到 ``~/.claude/vivado-mcp/``
- 指纹对文件行号漂移免疫(防误判)
"""

from __future__ import annotations

from pathlib import Path

from vivado_mcp.analysis.warning_parser import CriticalWarning, WarningReport
from vivado_mcp.analysis.warning_snapshot import (
    WarningDiff,
    diff_warnings,
    format_diff_report,
    load_snapshot,
    snapshot_cw,
)

# ====================================================================== #
#  构造器
# ====================================================================== #


def _cw(
    wid: str,
    msg: str = "",
    line: int = 1,
    source_file: str = "",
    port: str = "",
    pin: str = "",
) -> CriticalWarning:
    """生成测试用 CriticalWarning,默认填充空字段。"""
    return CriticalWarning(
        warning_id=wid,
        message=msg or f"CRITICAL WARNING: [{wid}] test message",
        line_number=line,
        source_file=source_file,
        port=port,
        pin=pin,
    )


def _empty_report(errors=0, cw_count=0, warnings=0) -> WarningReport:
    """生成不含 groups 的 WarningReport(快照只需要计数)。"""
    return WarningReport(
        errors=errors,
        critical_warnings=cw_count,
        warnings=warnings,
        groups=[],
        error_groups=[],
    )


# ====================================================================== #
#  snapshot + load 往返
# ====================================================================== #


class TestSnapshotRoundTrip:
    """快照写入 + 读取往返一致性。"""

    def test_roundtrip_preserves_cws(self, tmp_path):
        """写进去什么读出来还是什么。"""
        project_dir = tmp_path / "proj"
        project_dir.mkdir()

        cws = [
            _cw("Vivado 12-1411", port="rxp[0]", pin="AA4"),
            _cw("DRC BIVC-1", port="uart_tx", source_file="basys3.xdc"),
        ]
        report = _empty_report(errors=0, cw_count=2, warnings=3)

        path = snapshot_cw(report, cws, "impl_1", str(project_dir))

        # 落盘路径应该在 project_dir/.vmcp/ 下
        assert path.parent == project_dir / ".vmcp"
        assert path.name == "last_cw_impl_1.json"
        assert path.exists()

        loaded_report, loaded_cws = load_snapshot("impl_1", str(project_dir))
        assert loaded_report is not None
        assert loaded_report.critical_warnings == 2
        assert loaded_report.warnings == 3
        assert len(loaded_cws) == 2
        assert loaded_cws[0].warning_id == "Vivado 12-1411"
        assert loaded_cws[0].port == "rxp[0]"
        assert loaded_cws[1].warning_id == "DRC BIVC-1"
        assert loaded_cws[1].source_file == "basys3.xdc"

    def test_load_nonexistent_returns_none(self, tmp_path, monkeypatch):
        """没有上次快照时 load 返回 (None, []) 不抛异常。"""
        monkeypatch.setenv("HOME", str(tmp_path))  # POSIX
        monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows
        project_dir = tmp_path / "empty_proj"
        project_dir.mkdir()

        report, cws = load_snapshot("impl_1", str(project_dir))
        assert report is None
        assert cws == []

    def test_project_dir_none_falls_back_to_home(self, tmp_path, monkeypatch):
        """project_dir=None 时写到 ~/.claude/vivado-mcp/。"""
        # 劫持 Path.home() 返回 tmp_path
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        cws = [_cw("Vivado 12-1411")]
        report = _empty_report(cw_count=1)

        path = snapshot_cw(report, cws, "synth_1", None)

        expected_dir = tmp_path / ".claude" / "vivado-mcp"
        assert path.parent == expected_dir
        assert path.exists()

        # 反向读取
        loaded_report, loaded_cws = load_snapshot("synth_1", None)
        assert loaded_report is not None
        assert len(loaded_cws) == 1

    def test_readonly_project_dir_falls_back(self, tmp_path, monkeypatch):
        """项目目录不可写时自动降级到 fallback。

        跨平台一致:monkeypatch ``Path.mkdir`` 只对项目目录下的 ``.vmcp`` 抛 OSError,
        fallback 目录下的 mkdir 走原行为。原版用 ``Z:/`` 假盘符,Linux 下 ``Z:`` 是
        合法目录名能直接创建,fallback 不触发(0.3.9 子代理写测试时只考虑了 Windows)。
        """
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        bad_project = str(tmp_path / "readonly_project")
        real_mkdir = Path.mkdir

        def fake_mkdir(self, *args, **kwargs):
            if "readonly_project" in str(self) and ".vmcp" in str(self):
                raise OSError("simulated read-only project dir")
            return real_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", fake_mkdir)

        cws = [_cw("Vivado 12-1411")]
        report = _empty_report(cw_count=1)

        path = snapshot_cw(report, cws, "impl_1", bad_project)

        expected_dir = tmp_path / ".claude" / "vivado-mcp"
        assert path.parent == expected_dir
        assert path.exists()

    def test_corrupted_snapshot_returns_none(self, tmp_path, monkeypatch):
        """快照文件损坏时 load 返回 (None, []) 不抛异常。"""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # 手动创建一个非 JSON 垃圾文件
        fallback_dir = tmp_path / ".claude" / "vivado-mcp"
        fallback_dir.mkdir(parents=True)
        (fallback_dir / "last_cw_impl_1.json").write_text("this is not json {{{")

        report, cws = load_snapshot("impl_1", None)
        assert report is None
        assert cws == []


# ====================================================================== #
#  diff_warnings 三种分类
# ====================================================================== #


class TestDiffWarnings:
    """diff_warnings 的三种分类逻辑。"""

    def test_all_resolved(self):
        """prev 有,curr 没 → 全进 resolved。"""
        prev = [_cw("Vivado 12-1411"), _cw("DRC BIVC-1")]
        curr = []

        diff = diff_warnings(prev, curr)
        assert len(diff.resolved) == 2
        assert len(diff.newly_added) == 0
        assert len(diff.persistent) == 0

    def test_all_newly_added(self):
        """prev 没,curr 有 → 全进 newly_added。"""
        prev = []
        curr = [_cw("Vivado 12-1411"), _cw("DRC NSTD-1")]

        diff = diff_warnings(prev, curr)
        assert len(diff.resolved) == 0
        assert len(diff.newly_added) == 2
        assert len(diff.persistent) == 0

    def test_all_persistent(self):
        """两次完全相同 → 全进 persistent。"""
        prev = [_cw("Vivado 12-1411", port="rxp[0]")]
        curr = [_cw("Vivado 12-1411", port="rxp[0]")]

        diff = diff_warnings(prev, curr)
        assert len(diff.resolved) == 0
        assert len(diff.newly_added) == 0
        assert len(diff.persistent) == 1

    def test_mixed_case(self):
        """典型场景:消除 1 条,新出现 1 条,仍存在 1 条。"""
        prev = [
            _cw("Vivado 12-1411", port="rxp[0]"),    # 会被消除
            _cw("DRC BIVC-1", port="uart"),          # 仍存在
        ]
        curr = [
            _cw("DRC BIVC-1", port="uart"),          # 仍存在
            _cw("DRC NSTD-1", port="led"),           # 新出现
        ]

        diff = diff_warnings(prev, curr)
        assert len(diff.resolved) == 1
        assert diff.resolved[0].warning_id == "Vivado 12-1411"
        assert len(diff.newly_added) == 1
        assert diff.newly_added[0].warning_id == "DRC NSTD-1"
        assert len(diff.persistent) == 1
        assert diff.persistent[0].warning_id == "DRC BIVC-1"

    def test_count_reduction(self):
        """同类 CW 数量从 3 减到 1 → resolved 得 2 条,persistent 1 条。"""
        prev = [
            _cw("Vivado 12-1411", port="rxp[0]"),
            _cw("Vivado 12-1411", port="rxp[0]"),
            _cw("Vivado 12-1411", port="rxp[0]"),
        ]
        curr = [_cw("Vivado 12-1411", port="rxp[0]")]

        diff = diff_warnings(prev, curr)
        assert len(diff.resolved) == 2
        assert len(diff.persistent) == 1
        assert len(diff.newly_added) == 0

    def test_fingerprint_ignores_line_number_drift(self):
        """message 里 [foo.xdc:15] 变成 [foo.xdc:22] 不应误判为新 CW。"""
        prev = [
            _cw(
                "Vivado 12-1411",
                msg="CRITICAL WARNING: [Vivado 12-1411] ... [basys3.xdc:15]",
                port="rxp",
            )
        ]
        curr = [
            _cw(
                "Vivado 12-1411",
                msg="CRITICAL WARNING: [Vivado 12-1411] ... [basys3.xdc:22]",
                port="rxp",
            )
        ]

        diff = diff_warnings(prev, curr)
        # 同一 CW 只是 XDC 里挪了行,应该算 persistent 而不是 resolved+newly
        assert len(diff.persistent) == 1
        assert len(diff.resolved) == 0
        assert len(diff.newly_added) == 0


# ====================================================================== #
#  format_diff_report
# ====================================================================== #


class TestFormatDiffReport:
    """format_diff_report 输出格式。"""

    def test_contains_header_and_counts(self):
        """包含"差分报告"标题和三项计数。"""
        diff = WarningDiff(
            resolved=[_cw("Vivado 12-1411")],
            newly_added=[_cw("DRC NSTD-1")],
            persistent=[_cw("DRC BIVC-1"), _cw("DRC BIVC-1")],
        )
        text = format_diff_report(diff)
        assert "CW 差分报告" in text
        assert "已消除 1 条" in text
        assert "新出现 1 条" in text
        assert "仍存在 2 条" in text

    def test_resolved_section_has_positive_nudge(self):
        """已消除区块要有正反馈鼓励语。"""
        diff = WarningDiff(
            resolved=[_cw("Vivado 12-1411")],
            newly_added=[],
            persistent=[],
        )
        text = format_diff_report(diff)
        assert "[-]" in text and "已消除" in text
        # 正反馈提示
        assert "修复生效" in text or "修复方向正确" in text

    def test_newly_added_has_warning(self):
        """新出现区块要给警告,避免用户忽视。"""
        diff = WarningDiff(
            resolved=[],
            newly_added=[_cw("DRC NSTD-1")],
            persistent=[],
        )
        text = format_diff_report(diff)
        assert "[+]" in text and "新出现" in text
        assert "新问题" in text or "检查" in text or "回滚" in text

    def test_all_empty_no_change_message(self):
        """三项全空给出"无变化"结论。"""
        diff = WarningDiff(resolved=[], newly_added=[], persistent=[])
        text = format_diff_report(diff)
        assert "未对 CW 造成影响" in text or "完全一致" in text

    def test_reduction_only_shows_positive_conclusion(self):
        """只减不增 → 结论"修复方向正确"。"""
        diff = WarningDiff(
            resolved=[_cw("Vivado 12-1411"), _cw("Vivado 12-1411")],
            newly_added=[],
            persistent=[_cw("DRC BIVC-1")],
        )
        text = format_diff_report(diff)
        assert "修复方向正确" in text

    def test_regression_only_shows_warning_conclusion(self):
        """只增不减 → 结论建议回滚。"""
        diff = WarningDiff(
            resolved=[],
            newly_added=[_cw("DRC NSTD-1")],
            persistent=[_cw("DRC BIVC-1")],
        )
        text = format_diff_report(diff)
        assert "回滚" in text or "检查" in text
