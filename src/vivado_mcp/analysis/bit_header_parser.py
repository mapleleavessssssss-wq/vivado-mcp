"""Xilinx .bit 比特流文件头解析器。

纯 Python 模块，不依赖 Vivado。只解析 .bit 二进制文件的 TLV 头部
（不读取 payload bitstream 本体），抽取设计名 / 目标器件 / 日期 / 时间，
并计算整文件 SHA256，用于烧录前防错板（part 比对）和交付对账。

.bit 头部字节格式（Vivado 2019.1，已在真工件上验通）：

    [2B len=0x0009][9B = 0F F0 0F F0 0F F0 0F F0 00]   前导（位宽检测同步字）
    [2B len=0x0001][1B = 0x61 'a']                      key 'a' 引导
    [2B len][设计名串]                                   'a' 内容（含 ;COMPRESS=;UserID=;Version=）
    [1B 'b'][2B len][part 串]                            目标器件（如 7k325tffg900）
    [1B 'c'][2B len][date 串]                            日期（如 2026/03/07）
    [1B 'd'][2B len][time 串]                            时间（如 17:28:48）
    [1B 'e'][4B len]                                     payload 长度（只读到此即停）

关键坑：'b' 段里的 part = ``7k325tffg900`` —— 去掉了 ``xc`` 前缀和速度
等级 ``-2``，与 .xpr 里的 ``xc7k325tffg900-2`` 不同。本模块同时提供
``part_raw``（原样）和 ``part_normalized``（补回 ``xc`` 前缀）两种视图。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass

# 文件大小上限（10MB），防止误传超大文件耗尽内存
_MAX_FILE_SIZE = 10 * 1024 * 1024

# 前导魔数：2B 长度字段 0x0009 + 9 字节内容
_MAGIC_LEN = b"\x00\x09"
_MAGIC_BODY = b"\x0f\xf0\x0f\xf0\x0f\xf0\x0f\xf0\x00"

# 'a' 段引导：长度=1 的字段，内容为单字节 0x61（'a'）
_KEY_A_INTRO = b"\x00\x01\x61"


# ====================================================================== #
#  数据结构
# ====================================================================== #


@dataclass(frozen=True)
class BitHeader:
    """单个 .bit 文件头部的解析结果。"""

    file_path: str
    design: str             # 'a' 段设计名串（含 COMPRESS/UserID/Version 等元信息）
    part_raw: str           # 'b' 段原始 part（如 "7k325tffg900"）
    part_normalized: str    # 规整后 part（补回 xc 前缀，如 "xc7k325tffg900"）
    date: str               # 'c' 段日期（如 "2026/03/07"）
    time: str               # 'd' 段时间（如 "17:28:48"）
    bitstream_size: int     # 'e' 段声明的 payload 字节数
    file_size: int          # 整个 .bit 文件字节数
    sha256: str             # 整个 .bit 文件的 SHA256 十六进制摘要

    def to_dict(self) -> dict:
        """转换为可 JSON 序列化的字典。"""
        return {
            "file_path": self.file_path,
            "design": self.design,
            "part_raw": self.part_raw,
            "part_normalized": self.part_normalized,
            "date": self.date,
            "time": self.time,
            "bitstream_size": self.bitstream_size,
            "file_size": self.file_size,
            "sha256": self.sha256,
        }


# ====================================================================== #
#  解析函数
# ====================================================================== #


def _normalize_part(part_raw: str) -> str:
    """把 .bit 里的 part（如 "7k325tffg900"）规整为 .xpr 风格的器件名。

    .bit 头部省略了 ``xc`` 前缀（也不带速度等级 ``-2``）。这里仅补回
    ``xc`` 前缀以便与 .xpr / get_property 的 ``xc...`` 比对；速度等级
    在 .bit 中本就缺失，无法还原，故不补。
    """
    if not part_raw:
        return part_raw
    if part_raw.startswith("xc"):
        return part_raw
    return "xc" + part_raw


def parse_bit(file_path: str) -> BitHeader:
    """解析 .bit 文件头部，抽取设计名 / part / 日期 / 时间，并计算 SHA256。

    只读取头部 TLV 段，不解析 payload bitstream 本体。

    Args:
        file_path: .bit 文件的绝对路径。

    Returns:
        BitHeader 实例。

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: 文件过大、过短或不是合法的 .bit（缺少 0F F0 前导）。
    """
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f".bit 文件不存在: {file_path}")

    file_size = os.path.getsize(file_path)
    if file_size > _MAX_FILE_SIZE:
        raise ValueError(
            f".bit 文件过大 ({file_size / 1024 / 1024:.1f}MB)，"
            f"上限 {_MAX_FILE_SIZE / 1024 / 1024:.0f}MB"
        )

    with open(file_path, "rb") as f:
        data = f.read()

    # 整文件 SHA256（交付对账用）
    sha256 = hashlib.sha256(data).hexdigest()

    # 校验前导魔数：必须是 00 09 + 0F F0 0F F0 0F F0 0F F0 00
    intro = _MAGIC_LEN + _MAGIC_BODY
    if len(data) < len(intro):
        raise ValueError(
            f"文件过短，不足以容纳 .bit 头部前导 ({len(data)} 字节)"
        )
    if data[: len(intro)] != intro:
        raise ValueError(
            "不是合法的 .bit 文件（缺少 0F F0 前导魔数）: "
            f"前 {len(intro)} 字节为 {data[: len(intro)].hex(' ')}"
        )

    sections = _read_sections(data, len(intro))

    design = str(sections.get("a", ""))
    part_raw = str(sections.get("b", ""))
    date = str(sections.get("c", ""))
    time = str(sections.get("d", ""))
    bitstream_size = sections.get("e", 0)

    return BitHeader(
        file_path=file_path,
        design=design,
        part_raw=part_raw,
        part_normalized=_normalize_part(part_raw),
        date=date,
        time=time,
        bitstream_size=bitstream_size if isinstance(bitstream_size, int) else 0,
        file_size=file_size,
        sha256=sha256,
    )


def _read_section_body(data: bytes, pos: int, key: str) -> str:
    """读取 [2B 长度][内容] 段的内容（latin-1 解码，去尾部 NUL）。"""
    if pos + 2 > len(data):
        raise ValueError(f"'{key}' 段长度字段越界（头部被截断）")
    length = int.from_bytes(data[pos : pos + 2], "big")
    start = pos + 2
    end = start + length
    if end > len(data):
        raise ValueError(f"'{key}' 段内容越界（头部被截断）")
    body = data[start:end]
    return body.rstrip(b"\x00").decode("latin-1")


def _read_sections(data: bytes, pos: int) -> dict[str, object]:
    """从前导之后顺序读取 a/b/c/d/e 段。

    'a' 段格式特殊：[00 01 61][2B 长度][设计名串]；
    b/c/d 段格式：[key(1B)][2B 长度][内容]；
    'e' 段格式：[0x65 'e'][4B 长度] —— 读到长度即停，不读 payload。

    Returns:
        dict，键为段名（"a"/"b"/"c"/"d" → str，"e" → int payload 长度）。
        缺失的段不出现在结果里。
    """
    result: dict[str, object] = {}

    # 'a' 段引导
    if data[pos : pos + len(_KEY_A_INTRO)] != _KEY_A_INTRO:
        raise ValueError(
            "缺少 'a' 段引导（期望 00 01 61）: "
            f"实际为 {data[pos : pos + len(_KEY_A_INTRO)].hex(' ')}"
        )
    pos += len(_KEY_A_INTRO)
    result["a"] = _read_section_body(data, pos, "a")
    pos += 2 + len(data[pos + 2 : pos + 2 + int.from_bytes(data[pos : pos + 2], "big")])

    # 后续 b / c / d / e 段：[key 1B][长度][内容]
    while pos < len(data):
        key_byte = data[pos : pos + 1]
        if not key_byte:
            break
        key = key_byte.decode("latin-1")
        pos += 1

        if key == "e":
            # 'e' 段：4B 大端 payload 长度，读到即停
            if pos + 4 > len(data):
                raise ValueError("'e' 段长度字段越界（头部被截断）")
            result["e"] = int.from_bytes(data[pos : pos + 4], "big")
            break

        if key not in ("b", "c", "d"):
            # 未知段，停止（保守：避免把 payload 当段误读）
            break

        body = _read_section_body(data, pos, key)
        result[key] = body
        pos += 2 + int.from_bytes(data[pos : pos + 2], "big")

    return result


# ====================================================================== #
#  格式化
# ====================================================================== #


def format_bit(result: BitHeader) -> str:
    """将 BitHeader 格式化为人类可读的中文摘要。

    Args:
        result: 解析结果。

    Returns:
        多行中文摘要文本。
    """
    lines: list[str] = []
    lines.append("=== .bit 比特流头部 ===")
    lines.append(f"文件: {result.file_path}")
    lines.append(f"设计名: {result.design}")
    lines.append(f"器件 (原始): {result.part_raw}")
    lines.append(f"器件 (规整): {result.part_normalized}")
    lines.append(f"构建日期: {result.date}")
    lines.append(f"构建时间: {result.time}")
    lines.append(
        f"Bitstream 大小: {result.bitstream_size} 字节 "
        f"({result.bitstream_size / 1024 / 1024:.2f}MB)"
    )
    lines.append(f"文件大小: {result.file_size} 字节")
    lines.append(f"SHA256: {result.sha256}")
    return "\n".join(lines)
