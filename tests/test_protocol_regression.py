"""哨兵协议回归测试（B1 修复验证）。

核心断言：wrap_command 生成的 Tcl 脚本中，VMCP_ERR 行必须在 sentinel 行之前，
否则 session.execute 读到 sentinel 即 break，VMCP_ERR 残留到下一条命令的输出。
"""

from __future__ import annotations

from vivado_mcp.vivado.tcl_utils import (
    generate_sentinel,
    make_sentinel_pattern,
    wrap_command,
)


def test_wrap_command_err_before_sentinel() -> None:
    """B1: VMCP_ERR 行必须先于 sentinel 行输出，保证失败命令的错误消息不溢出。"""
    sentinel = generate_sentinel()
    wrapped = wrap_command("this_command_fails", sentinel)

    sentinel_marker = f'<<<{sentinel}_RC=$__rc>>>'
    err_marker = 'VMCP_ERR:'

    sentinel_pos = wrapped.index(sentinel_marker)
    err_pos = wrapped.index(err_marker)

    assert err_pos < sentinel_pos, (
        f"VMCP_ERR 行位置 ({err_pos}) 必须早于 sentinel 行位置 ({sentinel_pos})。"
        "否则错误消息会溢出到下一条命令。"
    )


def test_wrap_command_encodes_special_chars() -> None:
    """十六进制编码确保命令含任何特殊字符都能安全传输。"""
    tricky_cmd = r'set x "$INVALID [unbalanced {brackets"'
    wrapped = wrap_command(tricky_cmd, "SENTINEL")

    # 命令本身不应原文出现（应当被十六进制化）
    assert tricky_cmd not in wrapped
    assert 'binary format H*' in wrapped


def test_sentinel_pattern_extracts_rc() -> None:
    """sentinel 模式能从输出中提取 return code。"""
    sentinel = generate_sentinel()
    pattern = make_sentinel_pattern(sentinel)

    line_ok = f"<<<{sentinel}_RC=0>>>"
    line_err = f"<<<{sentinel}_RC=1>>>"

    m_ok = pattern.search(line_ok)
    m_err = pattern.search(line_err)

    assert m_ok is not None and m_ok.group(1) == "0"
    assert m_err is not None and m_err.group(1) == "1"


def test_sentinel_unique_per_call() -> None:
    """每次生成的 sentinel 应唯一，避免多条命令共用导致误匹配。"""
    s1 = generate_sentinel()
    s2 = generate_sentinel()
    assert s1 != s2
    assert s1.startswith("VMCP_")
    assert s2.startswith("VMCP_")
