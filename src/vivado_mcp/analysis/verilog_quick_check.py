"""Verilog/SystemVerilog 极速预检(纯 Python,零依赖)。

保存 .v/.sv 时立即跑,捕到最常见的低级错误——不等综合 30 秒。
覆盖不了语义,只覆盖"打眼就能看出"的毛病:
- 模块名和文件名不一致(项目约定)
- 没有 `endmodule`
- 圆/方/花括号数量不匹配(简单计数,不处理字符串里的括号——误报率低,能救命)
- 完全空文件

如果用户机器装了 `iverilog`,可以在外层 hook 里额外调用它做真语法检查。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

# 注释剥离(粗略,保留语义不关键):// 行注释 / /* 块注释 */
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_MODULE_NAME_RE = re.compile(r"\bmodule\s+(\w+)", re.MULTILINE)


@dataclass(frozen=True)
class VerilogIssue:
    severity: str    # "error" / "warning"
    rule: str
    message: str
    file: str
    line: int = 0


@dataclass
class VerilogCheckReport:
    issues: list[VerilogIssue] = field(default_factory=list)
    file: str = ""

    @property
    def errors(self) -> list[VerilogIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[VerilogIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "issue_count": len(self.issues),
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "issues": [asdict(i) for i in self.issues],
        }


def _strip_comments(text: str) -> str:
    """去掉 // 和 /* */ 注释,避免括号计数被字符串/注释干扰。

    粗处理:不严格处理字符串字面量里的括号(罕见于 Verilog),换来实现简单。
    """
    text = _BLOCK_COMMENT_RE.sub("", text)
    text = _LINE_COMMENT_RE.sub("", text)
    return text


def quick_check_verilog(path: str | Path) -> VerilogCheckReport:
    """对单个 .v / .sv 文件做轻量静态检查。

    返回 VerilogCheckReport,issues 空就是 PASS。
    """
    path_obj = Path(path)
    report = VerilogCheckReport(file=str(path_obj))

    if not path_obj.exists():
        report.issues.append(VerilogIssue(
            severity="error",
            rule="FILE_NOT_FOUND",
            message=f"文件不存在: {path}",
            file=str(path),
        ))
        return report

    try:
        raw = path_obj.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        report.issues.append(VerilogIssue(
            severity="error",
            rule="READ_ERROR",
            message=f"读取失败: {e}",
            file=str(path_obj),
        ))
        return report

    if not raw.strip():
        report.issues.append(VerilogIssue(
            severity="warning",
            rule="EMPTY_FILE",
            message="文件为空",
            file=str(path_obj),
        ))
        return report

    stripped = _strip_comments(raw)

    # 规则 1:endmodule 必须存在
    if "endmodule" not in stripped:
        report.issues.append(VerilogIssue(
            severity="error",
            rule="MISSING_ENDMODULE",
            message="未找到 endmodule 关键字,模块未闭合",
            file=str(path_obj),
        ))

    # 规则 2:module 名必须和文件名一致(第一个顶层 module)
    modules = _MODULE_NAME_RE.findall(stripped)
    if not modules:
        report.issues.append(VerilogIssue(
            severity="error",
            rule="NO_MODULE",
            message="未找到 module 声明",
            file=str(path_obj),
        ))
    else:
        expected = path_obj.stem  # 去掉后缀的纯文件名
        # 约定:文件里至少有一个 module 名和文件名匹配(通常是主 module)
        if expected not in modules:
            report.issues.append(VerilogIssue(
                severity="warning",
                rule="MODULE_NAME_MISMATCH",
                message=(
                    f"文件名 '{expected}' 不在 module 列表 {modules} 里。"
                    f"Vivado 综合时按 module 名找文件,不匹配会找不到;建议至少保留一个同名 module。"
                ),
                file=str(path_obj),
            ))

    # 规则 3:括号计数(去掉注释后)
    paren_balance = stripped.count("(") - stripped.count(")")
    brace_balance = stripped.count("{") - stripped.count("}")
    bracket_balance = stripped.count("[") - stripped.count("]")

    if paren_balance != 0:
        report.issues.append(VerilogIssue(
            severity="error",
            rule="PAREN_MISMATCH",
            message=(
                f"圆括号 '()' 不配对,差值 {paren_balance:+d}"
                f"({'多左括号' if paren_balance > 0 else '多右括号'})"
            ),
            file=str(path_obj),
        ))
    if brace_balance != 0:
        report.issues.append(VerilogIssue(
            severity="error",
            rule="BRACE_MISMATCH",
            message=(
                f"花括号 '{{}}' 不配对,差值 {brace_balance:+d}"
                f"({'多左括号' if brace_balance > 0 else '多右括号'})"
            ),
            file=str(path_obj),
        ))
    if bracket_balance != 0:
        report.issues.append(VerilogIssue(
            severity="error",
            rule="BRACKET_MISMATCH",
            message=(
                f"方括号 '[]' 不配对,差值 {bracket_balance:+d}"
                f"({'多左括号' if bracket_balance > 0 else '多右括号'})"
            ),
            file=str(path_obj),
        ))

    return report


def format_report(report: VerilogCheckReport) -> str:
    """格式化为中文单行/多行文本。"""
    if not report.issues:
        return ""  # PASS 情况返回空字符串,hook 层据此判断静默

    lines = ["=== Verilog 预检: FAIL ==="]
    name = Path(report.file).name
    for i in report.issues:
        tag = "[ERROR]" if i.severity == "error" else "[WARN]"
        lines.append(f"  {tag} [{i.rule}] {name}: {i.message}")
    return "\n".join(lines)
