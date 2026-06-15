""".bit 比特流头部解析器测试。

覆盖核心解析（对合成 fixture 断言字段）、降级路径（缺段）、
边界（文件不存在 / 空 / 截断 / 过大 / 非 .bit）。
"""

from __future__ import annotations

import hashlib
import struct
from pathlib import Path

import pytest

from vivado_mcp.analysis.bit_header_parser import (
    BitHeader,
    format_bit,
    parse_bit,
)

_FIXTURES = Path(__file__).parent.parent / "fixtures"
_SAMPLE = _FIXTURES / "sample_header.bit"

# 前导魔数
_INTRO = b"\x00\x09" + b"\x0f\xf0\x0f\xf0\x0f\xf0\x0f\xf0\x00"
_KEY_A_INTRO = b"\x00\x01\x61"


def _section(key: str, content: str) -> bytes:
    """构造 [key(1B)][2B 大端长度][内容\\x00] 段。"""
    body = content.encode("latin-1") + b"\x00"
    return key.encode("latin-1") + struct.pack(">H", len(body)) + body


def _build_bit(
    design: str = "sample_top;COMPRESS=TRUE;UserID=0XFFFFFFFF;Version=2019.1",
    part: str = "7k325tffg900",
    date: str = "2026/06/15",
    time: str = "12:00:00",
    payload_len: int = 64,
    include_b: bool = True,
    include_c: bool = True,
    include_d: bool = True,
    include_e: bool = True,
) -> bytes:
    """合成一个忠于已验 schema 的 .bit 头部字节串（可选裁掉某些段）。"""
    out = bytearray()
    out += _INTRO
    # 'a' 段：引导 + 2B 长度 + 设计名串
    out += _KEY_A_INTRO
    a_body = design.encode("latin-1") + b"\x00"
    out += struct.pack(">H", len(a_body)) + a_body
    if include_b:
        out += _section("b", part)
    if include_c:
        out += _section("c", date)
    if include_d:
        out += _section("d", time)
    if include_e:
        out += b"e" + struct.pack(">I", payload_len)
        out += b"\xff\xff\xff\xff\xaa\x99\x55\x66" + b"\x00" * max(0, payload_len - 8)
    return bytes(out)


# ====================================================================== #
#  核心解析
# ====================================================================== #


class TestParseBitCore:
    """对合成 fixture 与就地构造样本断言字段。"""

    def test_parse_sample_fixture(self):
        """解析仓库内 fixture，验证全部字段。"""
        r = parse_bit(str(_SAMPLE))
        assert isinstance(r, BitHeader)
        assert r.design == "sample_top;COMPRESS=TRUE;UserID=0XFFFFFFFF;Version=2019.1"
        assert r.part_raw == "7k325tffg900"
        assert r.part_normalized == "xc7k325tffg900"
        assert r.date == "2026/06/15"
        assert r.time == "12:00:00"
        assert r.bitstream_size == 64

    def test_part_normalization(self, tmp_path):
        """关键坑：part_raw 去 xc 前缀，part_normalized 补回 xc。"""
        bit = tmp_path / "p.bit"
        bit.write_bytes(_build_bit(part="7k325tffg900"))
        r = parse_bit(str(bit))
        assert r.part_raw == "7k325tffg900"          # 原样（无 xc，无速度等级）
        assert r.part_normalized == "xc7k325tffg900"  # 补回 xc 前缀

    def test_sha256_matches_whole_file(self, tmp_path):
        """SHA256 应是整个文件（含 payload）的摘要，而非仅头部。"""
        raw = _build_bit()
        bit = tmp_path / "s.bit"
        bit.write_bytes(raw)
        r = parse_bit(str(bit))
        assert r.sha256 == hashlib.sha256(raw).hexdigest()
        assert r.file_size == len(raw)

    def test_to_dict_structure(self):
        """to_dict 输出包含全部字段且可序列化。"""
        r = parse_bit(str(_SAMPLE))
        d = r.to_dict()
        for key in (
            "file_path", "design", "part_raw", "part_normalized",
            "date", "time", "bitstream_size", "file_size", "sha256",
        ):
            assert key in d
        assert d["part_normalized"] == "xc7k325tffg900"

    def test_format_bit_contains_key_fields(self):
        """中文摘要应包含 part / date / time / sha256 等关键字段。"""
        r = parse_bit(str(_SAMPLE))
        text = format_bit(r)
        assert "7k325tffg900" in text
        assert "xc7k325tffg900" in text
        assert "2026/06/15" in text
        assert "12:00:00" in text
        assert r.sha256 in text

    def test_part_already_normalized_not_doubled(self, tmp_path):
        """part 已含 xc 前缀时不重复添加。"""
        bit = tmp_path / "pre.bit"
        bit.write_bytes(_build_bit(part="xc7k325tffg900"))
        r = parse_bit(str(bit))
        assert r.part_normalized == "xc7k325tffg900"


# ====================================================================== #
#  降级路径（缺段）
# ====================================================================== #


class TestParseBitDegraded:
    """部分段缺失时优雅降级，不抛异常。"""

    def test_missing_e_section(self, tmp_path):
        """缺 'e' 段时 bitstream_size 为 0，其余字段正常。"""
        bit = tmp_path / "no_e.bit"
        bit.write_bytes(_build_bit(include_e=False))
        r = parse_bit(str(bit))
        assert r.part_raw == "7k325tffg900"
        assert r.bitstream_size == 0

    def test_missing_date_time(self, tmp_path):
        """缺 'c'/'d' 段时 date/time 为空串，仍能读到 part。"""
        bit = tmp_path / "no_cd.bit"
        bit.write_bytes(_build_bit(include_c=False, include_d=False))
        r = parse_bit(str(bit))
        assert r.part_raw == "7k325tffg900"
        assert r.date == ""
        assert r.time == ""

    def test_only_design_and_part(self, tmp_path):
        """仅有 'a' 和 'b' 段时其余为空/0。"""
        bit = tmp_path / "min.bit"
        bit.write_bytes(_build_bit(include_c=False, include_d=False, include_e=False))
        r = parse_bit(str(bit))
        assert r.design.startswith("sample_top")
        assert r.part_raw == "7k325tffg900"
        assert r.date == ""
        assert r.time == ""
        assert r.bitstream_size == 0


# ====================================================================== #
#  边界与错误
# ====================================================================== #


class TestParseBitErrors:
    """边界条件与非法输入。"""

    def test_file_not_found(self, tmp_path):
        """文件不存在抛 FileNotFoundError。"""
        missing = tmp_path / "nope.bit"
        with pytest.raises(FileNotFoundError):
            parse_bit(str(missing))

    def test_empty_file(self, tmp_path):
        """空文件抛 ValueError（过短）。"""
        empty = tmp_path / "empty.bit"
        empty.write_bytes(b"")
        with pytest.raises(ValueError):
            parse_bit(str(empty))

    def test_truncated_before_magic(self, tmp_path):
        """前导未读完即截断，抛 ValueError。"""
        trunc = tmp_path / "trunc.bit"
        trunc.write_bytes(_INTRO[:5])
        with pytest.raises(ValueError):
            parse_bit(str(trunc))

    def test_truncated_mid_section(self, tmp_path):
        """段长度声明越界（内容被截断），抛 ValueError。"""
        # 前导 + a 引导 + 声明 a 长度=100 但只给 4 字节内容
        raw = _INTRO + _KEY_A_INTRO + struct.pack(">H", 100) + b"abcd"
        trunc = tmp_path / "midtrunc.bit"
        trunc.write_bytes(raw)
        with pytest.raises(ValueError):
            parse_bit(str(trunc))

    def test_not_a_bit_no_magic(self, tmp_path):
        """无 0F F0 前导（非 .bit）抛 ValueError。"""
        notbit = tmp_path / "fake.bit"
        notbit.write_bytes(b"\x00\x09" + b"\x12\x34" * 8)
        with pytest.raises(ValueError, match="0F F0|前导"):
            parse_bit(str(notbit))

    def test_missing_a_intro(self, tmp_path):
        """前导正确但缺 'a' 段引导（00 01 61），抛 ValueError。"""
        raw = _INTRO + b"\x00\x02\x61\x62"  # 引导处不是 00 01 61
        bad = tmp_path / "no_a.bit"
        bad.write_bytes(raw)
        with pytest.raises(ValueError, match="'a' 段引导"):
            parse_bit(str(bad))

    def test_file_too_large(self, tmp_path):
        """文件超过 10MB 上限抛 ValueError。"""
        big = tmp_path / "huge.bit"
        big.write_bytes(b"\x00" * (10 * 1024 * 1024 + 1))
        with pytest.raises(ValueError, match="过大"):
            parse_bit(str(big))
