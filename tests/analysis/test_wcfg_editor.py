"""Vivado .wcfg 缩放区间编辑器测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from vivado_mcp.analysis.wcfg_editor import (
    get_zoom_range,
    set_zoom_range,
)

_FIXTURES = Path(__file__).parent.parent / "fixtures"
_SAMPLE_WCFG = _FIXTURES / "sample_wave.wcfg"


def _copy_fixture(tmp_path: Path) -> Path:
    """把 fixture 复制到 tmp_path(逐字节,保留 CRLF/中文),返回可写副本路径。"""
    dst = tmp_path / "wave.wcfg"
    dst.write_bytes(_SAMPLE_WCFG.read_bytes())
    return dst


# ====================================================================== #
#  set_zoom_range 测试
# ====================================================================== #


class TestSetZoomRange:
    """测试设置缩放区间。"""

    def test_happy_path(self, tmp_path):
        """整数 ns 入参写出正确的 fs 值。"""
        wcfg = _copy_fixture(tmp_path)
        set_zoom_range(str(wcfg), 990, 1042)

        text = wcfg.read_text(encoding="utf-8")
        assert 'time="990000000fs"' in text
        assert 'time="1042000000fs"' in text

    def test_float_input_rounds(self, tmp_path):
        """float 入参 990.5ns → round 到 990500000fs。"""
        wcfg = _copy_fixture(tmp_path)
        set_zoom_range(str(wcfg), 990.5, 1042)

        text = wcfg.read_text(encoding="utf-8")
        assert 'time="990500000fs"' in text

    def test_large_time_value(self, tmp_path):
        """大时间(1e9 ns 级)无溢出,fs 整数表达正确。"""
        wcfg = _copy_fixture(tmp_path)
        set_zoom_range(str(wcfg), 1_000_000_000, 2_000_000_000)

        text = wcfg.read_text(encoding="utf-8")
        assert 'time="1000000000000000fs"' in text
        assert 'time="2000000000000000fs"' in text

    def test_returns_written_path(self, tmp_path):
        """返回写入的文件路径。"""
        wcfg = _copy_fixture(tmp_path)
        result = set_zoom_range(str(wcfg), 990, 1042)
        assert result == str(wcfg)

    def test_preserves_chinese_signal_name(self, tmp_path):
        """写回保留中文信号名节点(UTF-8 不报编码错)。"""
        wcfg = _copy_fixture(tmp_path)
        set_zoom_range(str(wcfg), 990, 1042)

        text = wcfg.read_text(encoding="utf-8")
        assert "复位信号" in text
        assert "标记A" in text

    def test_preserves_wave_markers(self, tmp_path):
        """写回不动 <wave_markers><marker> 块。"""
        wcfg = _copy_fixture(tmp_path)
        set_zoom_range(str(wcfg), 990, 1042)

        text = wcfg.read_text(encoding="utf-8")
        assert '<marker time="1000000000fs" name="标记A" />' in text

    def test_preserves_crlf(self, tmp_path):
        """读原始 CRLF,写回仍 CRLF(守门:禁用 splitlines 重组)。"""
        wcfg = _copy_fixture(tmp_path)
        before = wcfg.read_bytes()
        assert b"\r\n" in before  # fixture 本身是 CRLF

        set_zoom_range(str(wcfg), 990, 1042)
        after = wcfg.read_bytes()
        assert b"\r\n" in after
        # LF 总数应仍全部带 CR(没有裸 LF 被引入)
        assert after.count(b"\n") == after.count(b"\r\n")

    def test_only_zoom_bytes_changed(self, tmp_path):
        """除两个 time 值外其余字节逐行未改。"""
        wcfg = _copy_fixture(tmp_path)
        before = wcfg.read_text(encoding="utf-8").splitlines()
        # fixture 本身是 990/1042,这里换成不同值以触发两行变化
        set_zoom_range(str(wcfg), 500, 800)
        after = wcfg.read_text(encoding="utf-8").splitlines()

        assert len(before) == len(after)
        changed = [i for i in range(len(before)) if before[i] != after[i]]
        # 只有 ZoomStartTime / ZoomEndTime 两行变化
        assert len(changed) == 2
        for i in changed:
            assert "Zoom" in after[i]

    def test_start_ge_end_raises(self, tmp_path):
        """start_ns > end_ns 抛 ValueError。"""
        wcfg = _copy_fixture(tmp_path)
        with pytest.raises(ValueError, match="小于"):
            set_zoom_range(str(wcfg), 1042, 990)

    def test_start_eq_end_raises(self, tmp_path):
        """start_ns == end_ns 抛 ValueError。"""
        wcfg = _copy_fixture(tmp_path)
        with pytest.raises(ValueError, match="小于"):
            set_zoom_range(str(wcfg), 990, 990)

    def test_file_not_found(self):
        """文件不存在抛 FileNotFoundError。"""
        with pytest.raises(FileNotFoundError):
            set_zoom_range("/nonexistent/path/wave.wcfg", 990, 1042)

    def test_wrong_extension(self, tmp_path):
        """非 .wcfg 扩展名抛 ValueError。"""
        bad = tmp_path / "wave.xml"
        bad.write_text("<wave_config/>")
        with pytest.raises(ValueError, match="扩展名"):
            set_zoom_range(str(bad), 990, 1042)

    def test_file_too_large(self, tmp_path):
        """文件超 10MB 抛 ValueError。"""
        big = tmp_path / "huge.wcfg"
        big.write_bytes(b"x" * (10 * 1024 * 1024 + 1))
        with pytest.raises(ValueError, match="过大"):
            set_zoom_range(str(big), 990, 1042)

    def test_single_quote_attr(self, tmp_path):
        """单引号 time='...' 也能替换(引号种类容错)。"""
        wcfg = tmp_path / "single.wcfg"
        wcfg.write_text(
            "<wave_config>\n"
            "  <zoom_setting>\n"
            "    <ZoomStartTime time='100000000fs'></ZoomStartTime>\n"
            "    <ZoomEndTime time='200000000fs'></ZoomEndTime>\n"
            "  </zoom_setting>\n"
            "</wave_config>\n",
            encoding="utf-8",
        )
        set_zoom_range(str(wcfg), 990, 1042)

        text = wcfg.read_text(encoding="utf-8")
        # 替换值正确,且引号种类保留为单引号
        assert "time='990000000fs'" in text
        assert "time='1042000000fs'" in text

    def test_multiple_spaces_before_attr(self, tmp_path):
        """属性间多空格 <ZoomStartTime   time= 仍匹配。"""
        wcfg = tmp_path / "spaced.wcfg"
        wcfg.write_text(
            "<wave_config>\n"
            "  <zoom_setting>\n"
            '    <ZoomStartTime   time="100000000fs"></ZoomStartTime>\n'
            '    <ZoomEndTime\ttime="200000000fs"></ZoomEndTime>\n'
            "  </zoom_setting>\n"
            "</wave_config>\n",
            encoding="utf-8",
        )
        set_zoom_range(str(wcfg), 990, 1042)

        text = wcfg.read_text(encoding="utf-8")
        assert 'time="990000000fs"' in text
        assert 'time="1042000000fs"' in text

    def test_duplicate_node_raises(self, tmp_path):
        """出现 2 个 ZoomStartTime 抛 ValueError(明示未唯一匹配)。"""
        wcfg = tmp_path / "dup.wcfg"
        wcfg.write_text(
            "<wave_config>\n"
            '  <ZoomStartTime time="1fs"></ZoomStartTime>\n'
            '  <ZoomStartTime time="2fs"></ZoomStartTime>\n'
            '  <ZoomEndTime time="3fs"></ZoomEndTime>\n'
            "</wave_config>\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="ZoomStartTime"):
            set_zoom_range(str(wcfg), 990, 1042)

    def test_missing_node_creates_block(self, tmp_path):
        """zoom_setting 节点缺失 → 创建成功且 get 读得回。"""
        wcfg = tmp_path / "nozoom.wcfg"
        wcfg.write_text(
            "<wave_config>\n"
            "  <wave_config_version>10</wave_config_version>\n"
            '  <wvobject type="logic" fp_name="/tb/clk"></wvobject>\n'
            "</wave_config>\n",
            encoding="utf-8",
        )
        set_zoom_range(str(wcfg), 990, 1042)

        text = wcfg.read_text(encoding="utf-8")
        assert "<zoom_setting>" in text
        assert 'time="990000000fs"' in text
        assert 'time="1042000000fs"' in text
        # 注入块在根闭合标签前
        assert text.index("<zoom_setting>") < text.index("</wave_config>")
        # 原有内容未丢
        assert "wave_config_version" in text

        assert get_zoom_range(str(wcfg)) == (990.0, 1042.0)

    def test_missing_node_no_root_close_raises(self, tmp_path):
        """节点缺失且无根闭合标签 → 抛 ValueError 明示原因。"""
        wcfg = tmp_path / "noroot.wcfg"
        wcfg.write_text("<wave_config>\n  <wave_config_version>10</wave_config_version>\n",
                        encoding="utf-8")
        with pytest.raises(ValueError, match="wave_config"):
            set_zoom_range(str(wcfg), 990, 1042)

    def test_atomic_write_no_orphan_tmp(self, tmp_path):
        """原子写后目录无残留临时文件。"""
        wcfg = _copy_fixture(tmp_path)
        set_zoom_range(str(wcfg), 990, 1042)
        leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []

    def test_failed_replace_keeps_original(self, tmp_path):
        """节点未唯一匹配抛错时,原文件保持不变(校验在写入前完成)。"""
        wcfg = tmp_path / "dup.wcfg"
        original = (
            "<wave_config>\n"
            '  <ZoomStartTime time="1fs"></ZoomStartTime>\n'
            '  <ZoomStartTime time="2fs"></ZoomStartTime>\n'
            '  <ZoomEndTime time="3fs"></ZoomEndTime>\n'
            "</wave_config>\n"
        )
        wcfg.write_text(original, encoding="utf-8")
        with pytest.raises(ValueError):
            set_zoom_range(str(wcfg), 990, 1042)
        # 原文件未被破坏
        assert wcfg.read_text(encoding="utf-8") == original


# ====================================================================== #
#  get_zoom_range 测试
# ====================================================================== #


class TestGetZoomRange:
    """测试只读缩放区间。"""

    def test_read_fixture(self, tmp_path):
        """读 fixture 返回正确的 (start_ns, end_ns)。"""
        wcfg = _copy_fixture(tmp_path)
        result = get_zoom_range(str(wcfg))
        assert result == (990.0, 1042.0)

    def test_set_then_get_roundtrip(self, tmp_path):
        """set 后 get 闭环。"""
        wcfg = _copy_fixture(tmp_path)
        set_zoom_range(str(wcfg), 990, 1042)
        assert get_zoom_range(str(wcfg)) == (990.0, 1042.0)

    def test_missing_node_returns_none(self, tmp_path):
        """节点缺失返回 None。"""
        wcfg = tmp_path / "nozoom.wcfg"
        wcfg.write_text(
            "<wave_config>\n"
            "  <wave_config_version>10</wave_config_version>\n"
            "</wave_config>\n",
            encoding="utf-8",
        )
        assert get_zoom_range(str(wcfg)) is None

    def test_utf8_chinese_no_decode_error(self, tmp_path):
        """含中文信号名的 UTF-8 文件能正常读不报编码错。"""
        wcfg = _copy_fixture(tmp_path)
        # 不抛异常即可
        result = get_zoom_range(str(wcfg))
        assert result is not None

    def test_file_not_found(self):
        """文件不存在抛 FileNotFoundError。"""
        with pytest.raises(FileNotFoundError):
            get_zoom_range("/nonexistent/path/wave.wcfg")

    def test_wrong_extension(self, tmp_path):
        """非 .wcfg 扩展名抛 ValueError。"""
        bad = tmp_path / "wave.txt"
        bad.write_text("<wave_config/>")
        with pytest.raises(ValueError, match="扩展名"):
            get_zoom_range(str(bad))


# ====================================================================== #
#  辅助:确认 fixture 存在(防 CI 漏带 fixture)
# ====================================================================== #


def test_fixture_exists():
    """sample_wave.wcfg fixture 应存在且为 CRLF + UTF-8。"""
    assert _SAMPLE_WCFG.is_file()
    raw = _SAMPLE_WCFG.read_bytes()
    assert b"\r\n" in raw
    assert "复位信号".encode("utf-8") in raw
