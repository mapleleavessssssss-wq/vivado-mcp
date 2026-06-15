"""LTX 文件解析器测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from vivado_mcp.analysis.ltx_parser import (
    LtxConfig,
    LtxCore,
    LtxProbe,
    format_ltx,
    parse_ltx,
)

_FIXTURES = Path(__file__).parent.parent / "fixtures"
_SAMPLE = str(_FIXTURES / "sample_probes.ltx")


# ====================================================================== #
#  核心解析
# ====================================================================== #


class TestParseLtx:
    """合成 fixture 的核心字段解析。"""

    def test_parse_root_version(self):
        """解析 ltx_root 的 version/minor。"""
        cfg = parse_ltx(_SAMPLE)
        assert isinstance(cfg, LtxConfig)
        assert cfg.version == "4"
        assert cfg.minor == "0"

    def test_core_count_and_types(self):
        """共 2 个核：1 ILA + 1 dbg_hub。"""
        cfg = parse_ltx(_SAMPLE)
        assert len(cfg.cores) == 2
        assert len(cfg.ila_cores) == 1
        assert len(cfg.dbg_hubs) == 1

    def test_dbg_hub_distinguished(self):
        """dbg_hub 是 XSDB_V3，且无 probe。"""
        cfg = parse_ltx(_SAMPLE)
        hub = cfg.dbg_hubs[0]
        assert hub.core_type == "XSDB_V3"
        assert hub.name == "dbg_hub"
        assert hub.is_dbg_hub
        assert not hub.is_ila
        assert hub.probes == ()

    def test_ila_core_fields(self):
        """ILA 核字段。"""
        cfg = parse_ltx(_SAMPLE)
        ila = cfg.ila_cores[0]
        assert ila.core_type == "ILA_V3"
        assert ila.name == "u_top/ila_sample"
        assert ila.spec == "labtools_ila_v6"
        assert ila.ip_name == "ila"
        assert ila.is_ila
        assert len(ila.probes) == 3

    def test_vector_probe_width(self):
        """probe0：64 位向量，位宽 = right - left + 1 = 64。"""
        cfg = parse_ltx(_SAMPLE)
        probe0 = cfg.ila_cores[0].probes[0]
        assert probe0.name == "probe0"
        assert probe0.left_index == 0
        assert probe0.right_index == 63
        assert probe0.width == 64
        assert probe0.is_vector is True
        assert probe0.probe_type == "DATA_TRIGGER"
        assert probe0.direction == "IN"

    def test_probe_net_mapping(self):
        """probe 映射到顶层 net 名（不含逐位 subnets）。"""
        cfg = parse_ltx(_SAMPLE)
        probes = cfg.ila_cores[0].probes
        assert probes[0].nets == ("u_top/data_bus_a",)
        assert probes[1].nets == ("u_top/counter_q",)
        assert probes[2].nets == ("u_top/valid_flag",)

    def test_scalar_probe_width_one(self):
        """probe2：非向量单 bit，位宽 = 1。"""
        cfg = parse_ltx(_SAMPLE)
        probe2 = cfg.ila_cores[0].probes[2]
        assert probe2.is_vector is False
        assert probe2.width == 1

    def test_to_dict_serializable(self):
        """to_dict 可 JSON 序列化，含 width 派生字段。"""
        import json

        cfg = parse_ltx(_SAMPLE)
        d = cfg.to_dict()
        json.dumps(d)  # 不抛异常即可序列化
        ila = next(c for c in d["cores"] if c["is_ila"])
        assert ila["probes"][0]["width"] == 64
        assert ila["probes"][0]["nets"] == ["u_top/data_bus_a"]


# ====================================================================== #
#  格式化
# ====================================================================== #


class TestFormatLtx:
    """中文摘要格式化。"""

    def test_format_contains_probes(self):
        cfg = parse_ltx(_SAMPLE)
        text = format_ltx(cfg)
        assert "LTX 探针清单" in text
        assert "u_top/ila_sample" in text
        assert "probe0" in text
        assert "宽=64" in text
        assert "u_top/data_bus_a" in text
        assert "dbg_hub" in text

    def test_format_no_ila(self, tmp_path):
        """无 ILA 时给出明确提示，不崩溃。"""
        p = tmp_path / "empty_cores.ltx"
        p.write_text(
            '{"ltx_root": {"version": 4, "minor": 0, "ltx_data": []}}',
            encoding="utf-8",
        )
        cfg = parse_ltx(str(p))
        assert cfg.ila_cores == ()
        text = format_ltx(cfg)
        assert "未发现 ILA 核" in text


# ====================================================================== #
#  降级路径与边界
# ====================================================================== #


class TestEdgeCases:
    """文件不存在 / 空 / 截断 / 过大 / 格式错 / XML。"""

    def test_file_not_found(self):
        """文件不存在 raise FileNotFoundError。"""
        with pytest.raises(FileNotFoundError):
            parse_ltx(str(Path("nonexistent_dir") / "missing.ltx"))

    def test_empty_file(self, tmp_path):
        """空文件 raise ValueError。"""
        p = tmp_path / "empty.ltx"
        p.write_text("", encoding="utf-8")
        with pytest.raises(ValueError, match="为空"):
            parse_ltx(str(p))

    def test_whitespace_only(self, tmp_path):
        """仅空白也按空处理。"""
        p = tmp_path / "ws.ltx"
        p.write_text("   \n\t  \n", encoding="utf-8")
        with pytest.raises(ValueError, match="为空"):
            parse_ltx(str(p))

    def test_xml_legacy_skip(self, tmp_path):
        """旧版 XML 格式给明确 SKIP 提示，不崩溃。"""
        p = tmp_path / "legacy.ltx"
        p.write_text(
            '<?xml version="1.0"?>\n<LTX></LTX>\n', encoding="utf-8"
        )
        with pytest.raises(ValueError, match="XML"):
            parse_ltx(str(p))

    def test_truncated_json(self, tmp_path):
        """截断的 JSON raise ValueError。"""
        p = tmp_path / "truncated.ltx"
        p.write_text('{"ltx_root": {"version": 4, "ltx_da', encoding="utf-8")
        with pytest.raises(ValueError, match="JSON 解析失败"):
            parse_ltx(str(p))

    def test_unknown_format(self, tmp_path):
        """首字符既非 { 也非 < 时报无法识别。"""
        p = tmp_path / "garbage.ltx"
        p.write_text("not a real ltx file", encoding="utf-8")
        with pytest.raises(ValueError, match="无法识别"):
            parse_ltx(str(p))

    def test_missing_ltx_root(self, tmp_path):
        """缺少 ltx_root raise ValueError。"""
        p = tmp_path / "noroot.ltx"
        p.write_text('{"something_else": 1}', encoding="utf-8")
        with pytest.raises(ValueError, match="ltx_root"):
            parse_ltx(str(p))

    def test_json_array_root(self, tmp_path):
        """JSON 根是数组（非对象）raise ValueError。

        首字符 '[' 不是 '{'/'<'，应在嗅探阶段被无法识别拦下。
        """
        p = tmp_path / "arr.ltx"
        p.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(ValueError, match="无法识别"):
            parse_ltx(str(p))

    def test_oversized_file(self, tmp_path, monkeypatch):
        """超过大小上限 raise ValueError。"""
        import vivado_mcp.analysis.ltx_parser as mod

        p = tmp_path / "big.ltx"
        p.write_text('{"ltx_root": {}}', encoding="utf-8")
        # 把上限压到 4 字节触发过大分支，避免真造 10MB 文件
        monkeypatch.setattr(mod, "_MAX_FILE_SIZE", 4)
        with pytest.raises(ValueError, match="过大"):
            parse_ltx(str(p))


# ====================================================================== #
#  数据结构直接构造（不经解析）
# ====================================================================== #


class TestDataclasses:
    """dataclass 派生属性。"""

    def test_probe_width(self):
        probe = LtxProbe(
            name="probe9",
            probe_type="DATA_TRIGGER",
            direction="IN",
            left_index=0,
            right_index=15,
            is_vector=True,
            nets=("net_x",),
        )
        assert probe.width == 16

    def test_core_classification(self):
        ila = LtxCore(name="i", core_type="ILA_V3", spec="s", ip_name="ila")
        hub = LtxCore(name="dbg_hub", core_type="XSDB_V3", spec="s", ip_name="")
        assert ila.is_ila and not ila.is_dbg_hub
        assert hub.is_dbg_hub and not hub.is_ila
