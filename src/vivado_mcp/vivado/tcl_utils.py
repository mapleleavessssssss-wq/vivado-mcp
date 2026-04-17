"""Tcl 命令包装、输出清洗、路径转换、安全引用工具。

核心职责：
- 生成带 catch + sentinel 的包装命令（十六进制编码防注入）
- 清洗 Vivado 输出（去除 Vivado% 提示符、ANSI 序列等）
- Windows 路径 → Tcl 正斜杠路径转换（含特殊字符转义）
- Tcl 字符串安全引用与标识符白名单验证
"""

import re
import uuid
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
#  常量 & 正则
# --------------------------------------------------------------------------- #

# Vivado 提示符正则
_VIVADO_PROMPT_RE = re.compile(r"^Vivado%?\s*", re.MULTILINE)
# ANSI 转义序列
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
# Vivado 标识符白名单：字母、数字、下划线、点、冒号、连字符
_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_.:\-]+$")

# 输出截断阈值（字符数）
MAX_OUTPUT_CHARS = 50_000


# --------------------------------------------------------------------------- #
#  TclResult 数据类
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class TclResult:
    """Tcl 命令执行结果。"""
    output: str       # 命令输出文本
    return_code: int  # Tcl return code（0=OK, 1=ERROR）
    is_error: bool    # 是否执行出错

    @property
    def summary(self) -> str:
        """生成适合 MCP 返回的摘要文本，超长输出自动截断。"""
        text = self.output
        if len(text) > MAX_OUTPUT_CHARS:
            text = (
                text[:MAX_OUTPUT_CHARS]
                + f"\n\n... [输出已截断，共 {len(self.output)} 字符，"
                f"显示前 {MAX_OUTPUT_CHARS} 字符。"
                f"建议使用 -file 选项将完整输出写入文件] ..."
            )
        if self.is_error:
            return f"[ERROR] (rc={self.return_code})\n{text}"
        return text if text.strip() else "[OK] 命令执行成功（无输出）"


# --------------------------------------------------------------------------- #
#  安全引用 & 验证
# --------------------------------------------------------------------------- #

def validate_identifier(value: str, param_name: str) -> str:
    """白名单验证 Vivado 标识符（run_name / part / fileset 等）。

    仅允许字母、数字、下划线、点、冒号、连字符。
    拒绝任何可能构成 Tcl 注入的字符（空格、分号、括号等）。

    Args:
        value: 待验证的标识符值。
        param_name: 参数名称（用于错误消息）。

    Returns:
        原样返回的合法标识符。

    Raises:
        ValueError: 标识符含非法字符。
    """
    if not value or not _SAFE_ID_RE.match(value):
        raise ValueError(
            f"参数 '{param_name}' 含非法字符: {value!r}。"
            f"仅允许字母、数字、下划线、点、冒号、连字符。"
        )
    return value


def tcl_quote(value: str) -> str:
    """安全引用 Tcl 字符串值，转义所有特殊字符后用双引号包裹。

    转义规则（反斜杠前缀）：
    - ``\\`` → ``\\\\``
    - ``"``  → ``\\"``
    - ``$``  → ``\\$``
    - ``[``  → ``\\[``
    - ``]``  → ``\\]``
    - ``{``  → ``\\{``
    - ``}``  → ``\\}``

    Returns:
        双引号包裹的安全 Tcl 字符串，如 ``"C:/path/to/\\$dir"``。
    """
    # 反斜杠必须最先处理，避免二次转义
    value = value.replace("\\", "\\\\")
    value = value.replace('"', '\\"')
    value = value.replace("$", "\\$")
    value = value.replace("[", "\\[")
    value = value.replace("]", "\\]")
    value = value.replace("{", "\\{")
    value = value.replace("}", "\\}")
    return f'"{value}"'


# --------------------------------------------------------------------------- #
#  哨兵协议
# --------------------------------------------------------------------------- #

def generate_sentinel() -> str:
    """生成唯一的哨兵标记。"""
    return f"VMCP_{uuid.uuid4().hex[:12]}"


def wrap_command(tcl_command: str, sentinel: str) -> str:
    """将用户 Tcl 命令包装为带 catch + sentinel 的完整命令。

    **安全机制**：先将命令十六进制编码（仅含 ``[0-9a-f]``），Tcl 端
    通过 ``binary format H*`` 解码后 ``uplevel #0`` 执行。
    无论用户命令包含什么字符（不平衡花括号、反斜杠、美元符号等），
    都不可能突破 catch 块。

    为什么用十六进制而非 Base64：Vivado 2019.1 使用 Tcl 8.5，
    ``binary decode base64`` 是 Tcl 8.6 才有的命令，
    而 ``binary format H*`` 在所有 Tcl 版本可用。

    Args:
        tcl_command: 原始 Tcl 命令（可多行）。
        sentinel: 唯一哨兵标记字符串。

    Returns:
        包装后的完整 Tcl 脚本。
    """
    hex_encoded = tcl_command.encode("utf-8").hex()
    # 关键：VMCP_ERR 行必须在 sentinel 之前输出，否则会被下一条命令读取（B1 修复）
    return (
        f'set __cmd [encoding convertfrom utf-8 '
        f'[binary format H* {hex_encoded}]]\n'
        f'set __rc [catch {{uplevel #0 $__cmd}} __out __opts]\n'
        f'if {{$__rc == 0}} {{ puts $__out }}\n'
        f'if {{$__rc != 0}} {{ puts "VMCP_ERR: $__out" }}\n'
        f'puts "<<<{sentinel}_RC=$__rc>>>"\n'
        f'flush stdout\n'
    )


def make_sentinel_pattern(sentinel: str) -> re.Pattern:
    """生成匹配哨兵行的正则模式。"""
    return re.compile(rf"<<<{re.escape(sentinel)}_RC=(\d+)>>>")


# --------------------------------------------------------------------------- #
#  输出清洗
# --------------------------------------------------------------------------- #

def clean_output(raw: str) -> str:
    """清洗 Vivado 原始输出。

    去除：
    - ANSI 转义序列
    - Vivado% 提示符
    - 多余空行（连续 3+ 空行合并为 2 行）
    - 首尾空白
    """
    text = _ANSI_ESCAPE_RE.sub("", raw)
    text = _VIVADO_PROMPT_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# --------------------------------------------------------------------------- #
#  路径转换
# --------------------------------------------------------------------------- #

def to_tcl_path(windows_path: str) -> str:
    """将文件路径转换为安全的 Tcl 字符串。

    处理步骤：
    1. 反斜杠 → 正斜杠
    2. 使用 ``tcl_quote()`` 转义 ``$``、``[``、``]`` 等 Tcl 特殊字符
       并用双引号包裹

    这样即使路径含空格、美元符号、方括号等字符也能正确解析。
    """
    normalized = windows_path.replace("\\", "/")
    return tcl_quote(normalized)
