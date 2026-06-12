"""XDC 约束解析器。

两种来源：
1. ``parse_xdc_constraints(raw)`` — 旧接口，解析 Tcl 脚本预处理后的
   ``VMCP_XDC_PIN:<file>|<line>|<pin>|<port>`` 格式（保留向后兼容）。
2. ``parse_xdc_file(path)`` — **新接口**，直接读取原始 XDC 文件，支持两种语法：

   - 传统：``set_property PACKAGE_PIN W5 [get_ports clk]``
   - -dict：``set_property -dict { PACKAGE_PIN W5 IOSTANDARD LVCMOS33 } [get_ports clk]``

   **B3 修复**：原 Tcl 正则只支持传统语法，导致 90% 的现代项目（用 -dict 语法）
   `verify_io_placement` 返回"未找到 PACKAGE_PIN 约束"。改走纯 Python 后两种都支持。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class XdcConstraint:
    """单条 PACKAGE_PIN 约束的结构化信息。

    Attributes:
        source_file: 约束来源 XDC 文件路径。
        line_number: 在 XDC 文件中的行号。
        pin: 封装引脚名称，如 "AA4"。
        port: 端口名称，如 "pcie_7x_mgt_rtl_0_rxp[0]"。
    """

    source_file: str
    line_number: int
    pin: str
    port: str


# 旧：VMCP_XDC_PIN 结构化格式
_XDC_PIN_RE = re.compile(
    r"^VMCP_XDC_PIN:"
    r"([^|]+)"
    r"\|"
    r"(\d+)"
    r"\|"
    r"([^|]+)"
    r"\|"
    r"(.+)$"
)

# get_ports 后可能是 {含方括号的名字} 或裸字——两种都要支持
# 比如 [get_ports clk] 或 [get_ports {led[0]}] 或 [ get_ports { led[4] } ]
# 用非捕获组匹配后由 _clean_port 剥掉花括号
_GET_PORTS_TOKEN = r"(\{[^}]+\}|[^\s\]]+)"

# 新：传统语法 set_property PACKAGE_PIN <pin> [get_ports <port>]
_TRADITIONAL_RE = re.compile(
    r"set_property\s+PACKAGE_PIN\s+(\S+)\s+\[\s*get_ports\s+"
    + _GET_PORTS_TOKEN
    + r"\s*\]",
    re.IGNORECASE,
)

# 新：-dict 语法 set_property -dict { ... PACKAGE_PIN <pin> ... } [get_ports <port>]
_DICT_RE = re.compile(
    r"set_property\s+-dict\s+\{\s*([^}]+?)\s*\}\s+\[\s*get_ports\s+"
    + _GET_PORTS_TOKEN
    + r"\s*\]",
    re.IGNORECASE | re.DOTALL,
)

# 从 -dict 内部提取 PACKAGE_PIN 值
_DICT_PIN_KEY_RE = re.compile(r"PACKAGE_PIN\s+(\S+)", re.IGNORECASE)


def _read_xdc_text(path: str | Path) -> tuple[str, str | None]:
    """读 XDC 文件并探测编码,返回 (文本, 编码名)。

    依次尝试 UTF-8 严格解码、GBK 严格解码(中文 Windows 记事本默认 ANSI=GBK
    存盘很常见)。两者都失败时降级为 UTF-8 + errors="replace" **仅供只读解析**,
    此时编码名返回 None —— 调用方不得用该结果写回文件(会把替换符落盘损坏内容)。
    """
    data = Path(path).read_bytes()
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        # 非法 UTF-8 → 继续尝试 GBK,这是预期路径不算降级
        pass
    try:
        return data.decode("gbk"), "gbk"
    except UnicodeDecodeError as e:
        logger.warning(
            "[DEGRADED] %s 既不是合法 UTF-8 也不是合法 GBK,按 UTF-8 替换符降级解码(只读): %s",
            path, e,
        )
        return data.decode("utf-8", errors="replace"), None


def _ends_with_continuation(line: str) -> bool:
    """判断物理行是否以 Tcl 续行反斜杠结尾(奇数个尾随反斜杠 = 续行)。

    偶数个尾随反斜杠是转义的字面反斜杠,不构成续行;反斜杠后若有空白
    也不是续行(Tcl 里 backslash-space 是转义空格),故不做 rstrip。
    """
    return (len(line) - len(line.rstrip("\\"))) % 2 == 1


def _fold_continuations(text: str) -> list[tuple[int, str]]:
    """把反斜杠续行折叠成逻辑行,返回 [(首个物理行号, 逻辑行)] 列表。

    Tcl 中 backslash-newline 等价于一个空格,XDC 里
    ``create_clock -name sys_clk \\`` + 续行 ``-period 10 [get_ports clk]``
    是合法写法。逐物理行的正则扫描会看不见续行内容,这里先折叠。
    行号取续行链的首个物理行,供 lint 报告与 fixer 定位。
    """
    physical = text.splitlines()
    logical: list[tuple[int, str]] = []
    i = 0
    while i < len(physical):
        start_lineno = i + 1
        line = physical[i]
        while _ends_with_continuation(line) and i + 1 < len(physical):
            i += 1
            line = line[:-1].rstrip() + " " + physical[i].lstrip()
        logical.append((start_lineno, line))
        i += 1
    return logical


def _clean_port(port_raw: str) -> str:
    """清理端口名：去除外围的花括号和空白。

    XDC 写法示例:
      [get_ports clk]              → port_raw = "clk"
      [get_ports {led[0]}]         → port_raw = "{led[0]}" → "led[0]"
      [get_ports { led[0] }]       → port_raw = "{ led[0] }" → "led[0]"
    """
    port = port_raw.strip()
    if port.startswith("{") and port.endswith("}"):
        port = port[1:-1].strip()
    return port


def parse_xdc_file(path: str | Path) -> list[XdcConstraint]:
    """直接读取 XDC 文件，解析所有 PACKAGE_PIN 约束。

    支持传统语法 和 ``-dict`` 语法两种写法。忽略 ``#`` 行注释。

    Args:
        path: XDC 文件路径。

    Returns:
        XdcConstraint 列表（可能为空）。

    Raises:
        FileNotFoundError: 文件不存在。
        OSError: 文件读取失败。
    """
    path_obj = Path(path)
    text = _read_xdc_text(path_obj)[0]
    source_file = str(path_obj)

    constraints: list[XdcConstraint] = []

    # 先折叠反斜杠续行:跨行 -dict 约束逐物理行扫描会静默解析为 0 条
    for lineno, raw_line in _fold_continuations(text):
        # 去除行尾注释（# 后面的内容），但保留引号内的 #
        line = _strip_comment(raw_line)
        if not line.strip():
            continue

        # 先尝试 -dict 语法
        for m in _DICT_RE.finditer(line):
            inner = m.group(1)
            port = _clean_port(m.group(2))
            pin_match = _DICT_PIN_KEY_RE.search(inner)
            if pin_match:
                constraints.append(
                    XdcConstraint(
                        source_file=source_file,
                        line_number=lineno,
                        pin=pin_match.group(1),
                        port=port,
                    )
                )

        # 再尝试传统语法（与 -dict 互斥，因为 -dict 行含 "-dict" 关键字，不会误匹配）
        # 但为保险起见用 if 而非 elif，允许两种语法混在同一行（罕见但合法）
        # -dict 行不会匹配传统正则因为 "PACKAGE_PIN" 前没有独立的 set_property 后直接接
        for m in _TRADITIONAL_RE.finditer(line):
            # 排除 -dict 的误匹配：如果同一段已被 _DICT_RE 覆盖，跳过
            # 简单办法：看前面是不是 "-dict {" 开头
            before = line[:m.start()]
            if "-dict" in before and "{" in before and "}" not in before:
                continue
            port = _clean_port(m.group(2))
            constraints.append(
                XdcConstraint(
                    source_file=source_file,
                    line_number=lineno,
                    pin=m.group(1),
                    port=port,
                )
            )

    return constraints


def _strip_comment(line: str) -> str:
    """去掉 XDC 行尾注释（# 开始），保留引号/花括号内的 #。

    为简化处理，当 # 出现在最外层时截断。XDC 语法中很少在字符串里用 #，
    实在遇到可以让用户改为转义。
    """
    in_single = False
    in_double = False
    brace_depth = 0
    escaped = False
    for i, ch in enumerate(line):
        if escaped:
            # 上一个字符是反斜杠,本字符被转义,不参与引号/花括号/# 判定
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "'" and not in_double:
            in_single = not in_single
        elif ch == "{" and not in_single and not in_double:
            brace_depth += 1
        elif ch == "}" and not in_single and not in_double:
            brace_depth -= 1
        elif ch == "#" and not in_single and not in_double and brace_depth == 0:
            return line[:i]
    return line


def parse_xdc_constraints(raw: str) -> list[XdcConstraint]:
    """解析 VMCP_XDC_PIN 格式的约束输出（旧接口，向后兼容）。

    逐行扫描，匹配 VMCP_XDC_PIN: 前缀的行，提取约束信息。
    忽略 VMCP_XDC_PIN_DONE 标记和其他非匹配行。

    **建议新代码使用 `parse_xdc_file(path)` 直接读文件**，因为 VMCP_XDC_PIN
    Tcl 脚本不支持 -dict 语法。

    Args:
        raw: 包含 VMCP_XDC_PIN 行的原始文本。

    Returns:
        解析后的 XdcConstraint 列表。
    """
    if not raw or not raw.strip():
        return []

    constraints: list[XdcConstraint] = []

    for line in raw.splitlines():
        line = line.strip()

        # 跳过结束标记和空行
        if not line or line == "VMCP_XDC_PIN_DONE":
            continue

        match = _XDC_PIN_RE.match(line)
        if match:
            constraints.append(
                XdcConstraint(
                    source_file=match.group(1).strip(),
                    line_number=int(match.group(2)),
                    pin=match.group(3).strip(),
                    port=match.group(4).strip(),
                )
            )

    return constraints
