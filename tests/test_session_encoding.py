"""decode_vivado_output: UTF-8 first + 系统 ANSI fallback 解码策略。

0.3.18 修复:Vivado 在 Windows 中文系统 stdout 默认 CP936/GBK,旧代码
`raw.decode("utf-8", errors="replace")` 会让中文输出大面积变 `�`。
"""

import sys

import pytest

from vivado_mcp.vivado.tcl_utils import decode_vivado_output


class TestDecodeVivadoOutput:
    """字节流解码策略测试(协议层穿透,不依赖 subprocess)。"""

    def test_pure_ascii(self):
        """纯 ASCII 走 UTF-8 路径,完整还原。"""
        assert decode_vivado_output(b"hello world") == "hello world"

    def test_utf8_chinese(self):
        """合法 UTF-8 中文字节,UTF-8 decode 成功,直接返回。"""
        text = "项目路径"
        raw = text.encode("utf-8")
        assert decode_vivado_output(raw) == text

    def test_utf8_mixed(self):
        """ASCII + UTF-8 中文混排。"""
        text = "VMCP_PROJ:project_dir=D:/项目/test"
        raw = text.encode("utf-8")
        assert decode_vivado_output(raw) == text

    def test_empty(self):
        """空字节流返回空字符串。"""
        assert decode_vivado_output(b"") == ""

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="mbcs fallback 只在 Windows 上有意义",
    )
    def test_gbk_chinese_fallback_windows(self):
        """GBK 字节流在 Windows 上 fallback 到 mbcs(CP936),完整还原。

        模拟 Vivado 在中文 Windows stdout 实际输出:
        `puts "项目"` 经 Tcl 走 stdout 时是 GBK 编码字节
        b'\\xcf\\xee\\xc4\\xbf' 而非 UTF-8 b'\\xe9\\xa1\\xb9\\xe7\\x9b\\xae'。
        """
        text = "项目"
        gbk_bytes = text.encode("gbk")
        # 关键断言:GBK 字节不能被 UTF-8 解码成原文
        # (否则用例无效,根本测不出 fallback 路径)
        assert gbk_bytes != text.encode("utf-8")
        assert decode_vivado_output(gbk_bytes) == text

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="mbcs fallback 只在 Windows 上有意义",
    )
    def test_gbk_mixed_ascii_chinese_windows(self):
        """GBK 字节流含 ASCII + 中文混排:VMCP 前缀 + 中文 value 场景。"""
        text = "VMCP_PROJ:project_dir=D:/项目/test"
        gbk_bytes = text.encode("gbk")
        assert decode_vivado_output(gbk_bytes) == text

    def test_invalid_bytes_safe_replace(self):
        """完全非法的字节流不抛异常,走 utf-8 with errors=replace 兜底。"""
        # 0xff 在 UTF-8 / mbcs / GBK 里都不是合法起始字节
        raw = b"\xff\xfe ok"
        result = decode_vivado_output(raw)
        # 不抛异常即可,内容包含 ok 即可
        assert "ok" in result

    def test_utf8_already_contains_replacement_char(self):
        """边界:输入 UTF-8 里已经含 U+FFFD,启发式会触发 fallback。

        这是已知 trade-off:Vivado 实际输出几乎不可能含 U+FFFD,所以
        启发式安全。本测试只锁定"不抛异常",fallback 后的具体内容
        依赖系统 code page 规则,不强求。
        """
        raw = "before�after".encode("utf-8")
        result = decode_vivado_output(raw)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_does_not_raise_on_any_input(self):
        """robustness:任意字节输入不应抛异常。"""
        candidates = [
            b"",
            b"\x00",
            b"\xff" * 10,
            b"normal text",
            b"\xe9\xa1\xb9",  # utf-8 '项'
            b"\xcf\xee",      # gbk '项' 首字节序列
            b"\n\r\t",
        ]
        for raw in candidates:
            # 不抛异常即可
            assert isinstance(decode_vivado_output(raw), str)
