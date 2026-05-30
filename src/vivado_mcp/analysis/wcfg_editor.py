"""Vivado 波形配置文件(.wcfg)缩放区间编辑器。

纯 Python 模块,不依赖 Vivado。Vivado 2019.1 没有 Tcl zoom 命令,要把波形
缩放到指定时间窗只能改 .wcfg 里的 ``<zoom_setting>`` 块再让 XSim 重载,所以这里
提供脱机的"外科手术式"读写。

为什么用正则就地替换而不是 ElementTree:
  ElementTree 会重排属性顺序、丢注释、丢 XML 声明、可能改动其余字节,破坏 Vivado
  解析。本任务只需精确替换两个 time 属性值,正则锚定 ``<ZoomStartTime time="...">``
  / ``<ZoomEndTime time="...">`` 是最稳的做法 —— 文件其余全部字节(含中文信号名、
  CRLF 行尾、注释、BOM)逐字保留。

.wcfg 缩放节点结构:
  <wave_config>
    ...
    <zoom_setting>
      <ZoomStartTime time="990000000fs"></ZoomStartTime>
      <ZoomEndTime   time="1042000000fs"></ZoomEndTime>
    </zoom_setting>
    ...
  </wave_config>

时间单位:.wcfg 里写飞秒(fs);本模块对外 API 用纳秒(ns),内部 ×1e6 换算。
"""

from __future__ import annotations

import os
import re

# 文件大小上限(10MB,沿用 xci_parser)
_MAX_FILE_SIZE = 10 * 1024 * 1024

# 1 ns = 1e6 fs
_FS_PER_NS = 1_000_000

# 匹配 <ZoomStartTime time="...fs"> / <ZoomEndTime time='...fs'>:
#   group(1) 前缀(含 time=)、group(2) 引号、group(3) 值、group(4) 同种闭合引号。
# 容忍属性间多空白(\s+)、单/双引号([\"'] 反向引用保证成对)。
_ZOOM_START_RE = re.compile(r"(<ZoomStartTime\s+time=)([\"'])(.*?)(\2)")
_ZOOM_END_RE = re.compile(r"(<ZoomEndTime\s+time=)([\"'])(.*?)(\2)")

# 缺失 <zoom_setting> 时注入用的根闭合标签锚点
_ROOT_CLOSE = "</wave_config>"


# ====================================================================== #
#  内部 helper
# ====================================================================== #


def _validate_path(wcfg_path: str) -> None:
    """校验文件存在性 / 扩展名 / 大小(沿用 xci_parser 异常风格)。

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: 扩展名不是 .wcfg 或文件过大。
    """
    if not os.path.isfile(wcfg_path):
        raise FileNotFoundError(f".wcfg 文件不存在: {wcfg_path}")

    if not wcfg_path.lower().endswith(".wcfg"):
        raise ValueError(f"文件扩展名不是 .wcfg: {wcfg_path}")

    file_size = os.path.getsize(wcfg_path)
    if file_size > _MAX_FILE_SIZE:
        raise ValueError(
            f".wcfg 文件过大 ({file_size / 1024 / 1024:.1f}MB)，"
            f"上限 {_MAX_FILE_SIZE / 1024 / 1024:.0f}MB"
        )


def _ns_to_fs(ns: float) -> str:
    """ns → '<int>fs'(round 取整)。"""
    return f"{round(ns * _FS_PER_NS)}fs"


def _fs_to_ns(fs_literal: str) -> float:
    """'<int>fs' → ns(去掉 fs 后缀后 ÷ 1e6)。

    Raises:
        ValueError: 字面量不是合法的 '<数字>fs' 形式。
    """
    text = fs_literal.strip()
    if not text.lower().endswith("fs"):
        raise ValueError(f"time 值不是飞秒格式(缺 fs 后缀): {fs_literal!r}")
    num = text[:-2].strip()
    try:
        return float(num) / _FS_PER_NS
    except ValueError as e:
        raise ValueError(f"time 值无法解析为数字: {fs_literal!r}") from e


def _read_text(wcfg_path: str) -> str:
    """读原始字节并按 UTF-8 解码(保留 BOM,以便写回时原样还原)。"""
    raw = open(wcfg_path, "rb").read()
    # utf-8-sig 会吃掉 BOM;这里故意用 utf-8 让 BOM 以 '﻿' 留在文本里,
    # 写回时再编回 UTF-8 字节,保证 BOM 状态前后一致。
    return raw.decode("utf-8")


def _atomic_write(wcfg_path: str, text: str) -> None:
    """原子写回:先写同目录临时文件,再 os.replace 落盘。

    避免工具层让 XSim 重载时读到半截文件。
    """
    directory = os.path.dirname(os.path.abspath(wcfg_path))
    tmp_path = os.path.join(directory, f".{os.path.basename(wcfg_path)}.tmp")
    data = text.encode("utf-8")
    with open(tmp_path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, wcfg_path)


def _sub_unique(pattern: re.Pattern[str], repl_value: str, text: str, node_name: str) -> str:
    """把 pattern 匹配的 time 值替换成 repl_value,要求恰好匹配 1 次。

    匹配 0 次或多次都抛 ValueError 明示原因,避免静默替换失败
    (error-handling.md:降级前打印具体原因)。
    """
    matches = pattern.findall(text)
    if len(matches) == 0:
        raise ValueError(f"未找到 {node_name} 节点,无法替换 time 值")
    if len(matches) > 1:
        raise ValueError(f"找到 {len(matches)} 个 {node_name} 节点(期望唯一),拒绝替换")
    # group(1)=前缀, group(2)=引号, repl_value, group(4)=同种引号
    return pattern.sub(rf"\g<1>\g<2>{repl_value}\g<4>", text, count=1)


def _build_zoom_block(start_fs: str, end_fs: str, newline: str, indent: str = "   ") -> str:
    """构造一个 <zoom_setting> 块文本(行尾跟随原文件 CRLF/LF)。"""
    inner = indent + indent
    return (
        f"{indent}<zoom_setting>{newline}"
        f'{inner}<ZoomStartTime time="{start_fs}"></ZoomStartTime>{newline}'
        f'{inner}<ZoomEndTime time="{end_fs}"></ZoomEndTime>{newline}'
        f"{indent}</zoom_setting>{newline}"
    )


def _detect_newline(text: str) -> str:
    """探测主导行尾:含 \\r\\n 用 CRLF,否则 LF。"""
    return "\r\n" if "\r\n" in text else "\n"


# ====================================================================== #
#  公开 API
# ====================================================================== #


def set_zoom_range(wcfg_path: str, start_ns: float, end_ns: float) -> str:
    """设置 .wcfg 的波形缩放区间(入参 ns,内部 ×1e6 写成 '<int>fs')。

    正则就地替换 ZoomStartTime/ZoomEndTime 的 time 值,文件其余全部字节逐字保留
    (中文信号名、CRLF 行尾、注释、BOM、wave_markers 不变);若 <zoom_setting>
    节点缺失则在根闭合标签 </wave_config> 前注入一个新块。原子写回(os.replace)。

    Args:
        wcfg_path: .wcfg 文件绝对路径。
        start_ns: 缩放起始时间(纳秒)。
        end_ns: 缩放结束时间(纳秒),必须大于 start_ns。

    Returns:
        写入的文件路径(便于工具层回显)。

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: 扩展名非 .wcfg / 文件过大 / start_ns >= end_ns /
            ZoomStartTime|ZoomEndTime 节点未唯一匹配。
    """
    _validate_path(wcfg_path)

    if start_ns >= end_ns:
        raise ValueError(f"start_ns 必须小于 end_ns(收到 start={start_ns}, end={end_ns})")

    start_fs = _ns_to_fs(start_ns)
    end_fs = _ns_to_fs(end_ns)

    text = _read_text(wcfg_path)
    newline = _detect_newline(text)

    has_start = _ZOOM_START_RE.search(text) is not None
    has_end = _ZOOM_END_RE.search(text) is not None

    if not has_start and not has_end:
        # 节点完全缺失 → 构造一个 <zoom_setting> 块注入到根闭合标签前。
        if _ROOT_CLOSE not in text:
            raise ValueError(f"缺少 {_ROOT_CLOSE} 根闭合标签,无法注入 zoom_setting 节点")
        block = _build_zoom_block(start_fs, end_fs, newline)
        # 只替换最后一个根闭合标签(rsplit 保证从尾部切,容错文件含多余文本)
        head, _, tail = text.rpartition(_ROOT_CLOSE)
        new_text = head + block + _ROOT_CLOSE + tail
    else:
        # 至少有一个节点存在 → 逐节点唯一替换。
        new_text = _sub_unique(_ZOOM_START_RE, start_fs, text, "ZoomStartTime")
        new_text = _sub_unique(_ZOOM_END_RE, end_fs, new_text, "ZoomEndTime")

    _atomic_write(wcfg_path, new_text)
    return wcfg_path


def get_zoom_range(wcfg_path: str) -> tuple[float, float] | None:
    """只读当前缩放区间,返回 (start_ns, end_ns);fs ÷ 1e6 转回 ns。

    节点缺失返回 None(供工具层判断是否需创建/复位)。

    Args:
        wcfg_path: .wcfg 文件绝对路径。

    Returns:
        (start_ns, end_ns) 元组;ZoomStartTime/ZoomEndTime 任一缺失返回 None。

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: 扩展名非 .wcfg / 文件过大 / time 值无法解析为飞秒。
    """
    _validate_path(wcfg_path)

    text = _read_text(wcfg_path)

    start_match = _ZOOM_START_RE.search(text)
    end_match = _ZOOM_END_RE.search(text)
    if start_match is None or end_match is None:
        return None

    start_ns = _fs_to_ns(start_match.group(3))
    end_ns = _fs_to_ns(end_match.group(3))
    return (start_ns, end_ns)
