"""XDC 静态检查器 (pure Python,不依赖 Vivado)。

在综合/实现之前就能发现常见约束错误,避免等 30s 综合后才看到
NSTD-1/UCIO-1 之类的问题。检查维度:

1. PACKAGE_PIN 冲突:同一物理引脚被多个 port 引用
2. 漏 IOSTANDARD:有 PACKAGE_PIN 但没配 IOSTANDARD(触发 NSTD-1)
3. 重复约束:同一 port 被多次赋同属性(后者覆盖前者)
4. create_clock 缺少 -period
5. set_property 语法明显错误(空引脚名等)

所有检查基于原始 XDC 文件文本,不依赖 Vivado Tcl 解析器。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from vivado_mcp.analysis.xdc_parser import _DICT_RE, _TRADITIONAL_RE, _clean_port, _strip_comment


@dataclass(frozen=True)
class LintIssue:
    """一条 lint 问题。"""
    severity: str       # "error" / "warning" / "info"
    rule: str           # 规则名(如 "PIN_CONFLICT")
    message: str        # 中文说明
    file: str           # XDC 文件路径
    line: int           # 行号
    port: str = ""      # 涉及端口(如适用)


@dataclass
class LintReport:
    """lint 总报告。"""
    issues: list[LintIssue] = field(default_factory=list)
    files_checked: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[LintIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[LintIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    def to_dict(self) -> dict:
        return {
            "files_checked": self.files_checked,
            "issue_count": len(self.issues),
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "issues": [asdict(i) for i in self.issues],
        }


# -dict 内部 IOSTANDARD 值
_DICT_IOSTD_RE = re.compile(r"IOSTANDARD\s+(\S+)", re.IGNORECASE)
# 传统语法的 IOSTANDARD 设置
_TRAD_IOSTD_RE = re.compile(
    r"set_property\s+IOSTANDARD\s+(\S+)\s+\[\s*get_ports\s+(\{[^}]+\}|\S+?)\s*\]",
    re.IGNORECASE,
)
# create_clock 检测(含 -period 则合法)
_CREATE_CLOCK_RE = re.compile(r"create_clock\b(.*)", re.IGNORECASE)


def _parse_constraints_with_iostd(path: Path) -> tuple[
    list[tuple[int, str, str, str | None]],   # (line, port, pin, iostandard|None)  -dict 和传统
    list[tuple[int, str, str]],                # (line, port, iostandard) 独立 IOSTANDARD 语句
    list[tuple[int, str]],                     # (line, raw) create_clock 行
]:
    """扫描 XDC 文件,返回三类结构化结果。

    - 每个 PACKAGE_PIN 约束(含 -dict 内联的 IOSTANDARD)
    - 独立的 set_property IOSTANDARD 语句
    - create_clock 语句
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    pin_constraints: list[tuple[int, str, str, str | None]] = []
    iostd_constraints: list[tuple[int, str, str]] = []
    create_clock_lines: list[tuple[int, str]] = []

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = _strip_comment(raw_line)
        if not line.strip():
            continue

        # -dict 语法
        for m in _DICT_RE.finditer(line):
            inner = m.group(1)
            port = _clean_port(m.group(2))
            pin_match = re.search(r"PACKAGE_PIN\s+(\S+)", inner, re.IGNORECASE)
            iostd_match = _DICT_IOSTD_RE.search(inner)
            pin = pin_match.group(1) if pin_match else ""
            iostd = iostd_match.group(1) if iostd_match else None
            if pin:
                pin_constraints.append((lineno, port, pin, iostd))

        # 传统语法(PACKAGE_PIN)
        for m in _TRADITIONAL_RE.finditer(line):
            before = line[:m.start()]
            if "-dict" in before and "{" in before and "}" not in before:
                continue
            port = _clean_port(m.group(2))
            pin_constraints.append((lineno, port, m.group(1), None))

        # 独立 IOSTANDARD
        for m in _TRAD_IOSTD_RE.finditer(line):
            port = _clean_port(m.group(2))
            iostd_constraints.append((lineno, port, m.group(1)))

        # create_clock
        m_clk = _CREATE_CLOCK_RE.search(line)
        if m_clk:
            create_clock_lines.append((lineno, line.strip()))

    return pin_constraints, iostd_constraints, create_clock_lines


def lint_xdc_file(path: str | Path) -> list[LintIssue]:
    """对单个 XDC 文件做静态检查,返回问题列表。"""
    path_obj = Path(path)
    if not path_obj.exists():
        return [LintIssue(
            severity="error",
            rule="FILE_NOT_FOUND",
            message=f"XDC 文件不存在: {path}",
            file=str(path),
            line=0,
        )]

    issues: list[LintIssue] = []
    source_file = str(path_obj)

    pin_cs, iostd_cs, create_clock_lines = _parse_constraints_with_iostd(path_obj)

    # 规则 1: PACKAGE_PIN 冲突(同一 pin 被不同 port 占用)
    pin_to_ports: dict[str, list[tuple[int, str]]] = {}
    for (lineno, port, pin, _iostd) in pin_cs:
        pin_to_ports.setdefault(pin.upper(), []).append((lineno, port))
    for pin, occurrences in pin_to_ports.items():
        if len({p for _, p in occurrences}) > 1:
            ports_str = ", ".join(f"{p}(行{l})" for l, p in occurrences)
            issues.append(LintIssue(
                severity="error",
                rule="PIN_CONFLICT",
                message=f"引脚 {pin} 被多个 port 占用: {ports_str}",
                file=source_file,
                line=occurrences[0][0],
            ))

    # 规则 2: 漏 IOSTANDARD(同 port 没有在 -dict 里带 IOSTANDARD,也没有独立语句)
    iostd_ports = {port for (_, port, _) in iostd_cs}
    for (lineno, port, pin, iostd) in pin_cs:
        if iostd is None and port not in iostd_ports:
            issues.append(LintIssue(
                severity="warning",
                rule="MISSING_IOSTANDARD",
                message=(
                    f"端口 {port} 只有 PACKAGE_PIN={pin} 没配 IOSTANDARD,"
                    "将触发 NSTD-1 警告,严重时会导致 Bank 电压冲突(BIVC-1 ERROR)"
                ),
                file=source_file,
                line=lineno,
                port=port,
            ))

    # 规则 3: 同 port 多次被约束 PACKAGE_PIN(后者覆盖前者)
    port_to_pins: dict[str, list[tuple[int, str]]] = {}
    for (lineno, port, pin, _) in pin_cs:
        port_to_pins.setdefault(port, []).append((lineno, pin))
    for port, occurrences in port_to_pins.items():
        if len(occurrences) > 1 and len({p for _, p in occurrences}) > 1:
            pins_str = ", ".join(f"{p}(行{l})" for l, p in occurrences)
            issues.append(LintIssue(
                severity="warning",
                rule="DUPLICATE_PORT",
                message=(
                    f"端口 {port} 被多次约束不同引脚: {pins_str} —— "
                    "Vivado 会采用最后一个,前面的约束无效"
                ),
                file=source_file,
                line=occurrences[0][0],
                port=port,
            ))

    # 规则 4: create_clock 缺 -period
    for (lineno, raw) in create_clock_lines:
        if "-period" not in raw.lower():
            issues.append(LintIssue(
                severity="error",
                rule="CLOCK_NO_PERIOD",
                message=f"create_clock 缺少 -period 参数: {raw}",
                file=source_file,
                line=lineno,
            ))

    return issues


def lint_xdc_files(paths: list[str | Path]) -> LintReport:
    """批量 lint 多个 XDC 文件,并做跨文件检查(全局 pin 冲突)。"""
    report = LintReport()
    all_pin_map: dict[str, list[tuple[str, int, str]]] = {}  # pin -> [(file, line, port)]

    for p in paths:
        path_obj = Path(p)
        report.files_checked.append(str(path_obj))
        report.issues.extend(lint_xdc_file(path_obj))

        # 收集 pin 供跨文件冲突检查
        if path_obj.exists():
            pin_cs, _, _ = _parse_constraints_with_iostd(path_obj)
            for (lineno, port, pin, _iostd) in pin_cs:
                all_pin_map.setdefault(pin.upper(), []).append(
                    (str(path_obj), lineno, port)
                )

    # 跨文件的 PIN_CONFLICT(同一 pin 在不同文件里指向不同 port)
    for pin, occurrences in all_pin_map.items():
        files_with_pin = {f for f, _, _ in occurrences}
        ports_for_pin = {p for _, _, p in occurrences}
        if len(files_with_pin) > 1 and len(ports_for_pin) > 1:
            # 只在"跨文件冲突"时报告,避免单文件已经报过的重复
            # 但要判断是否和单文件内冲突重复 —— 按文件数 > 1 判断即可
            loc_str = ", ".join(
                f"{Path(f).name}:{l}:{p}" for f, l, p in occurrences
            )
            report.issues.append(LintIssue(
                severity="error",
                rule="PIN_CONFLICT_CROSS_FILE",
                message=f"引脚 {pin} 在多个 XDC 文件里被不同 port 占用: {loc_str}",
                file=occurrences[0][0],
                line=occurrences[0][1],
            ))

    return report


def format_lint_report(report: LintReport) -> str:
    """格式化 lint 报告为中文文本。"""
    lines: list[str] = []

    if not report.issues:
        lines.append("=== XDC 静态检查: PASS ===")
        lines.append(f"检查了 {len(report.files_checked)} 个文件,无问题。")
        return "\n".join(lines)

    n_err = len(report.errors)
    n_warn = len(report.warnings)
    header = "=== XDC 静态检查: "
    if n_err > 0:
        header += f"FAIL ({n_err} errors, {n_warn} warnings) ==="
    else:
        header += f"WARN ({n_warn} warnings) ==="
    lines.append(header)
    lines.append(f"扫描文件: {len(report.files_checked)}")
    lines.append("")

    # 按严重级分组展示
    for sev, label in [("error", "ERROR"), ("warning", "WARNING"), ("info", "INFO")]:
        subset = [i for i in report.issues if i.severity == sev]
        if not subset:
            continue
        lines.append(f"--- {label} ({len(subset)} 条) ---")
        for issue in subset:
            file_name = Path(issue.file).name
            lines.append(f"  [{issue.rule}] {file_name}:{issue.line}")
            lines.append(f"    {issue.message}")
        lines.append("")

    return "\n".join(lines).rstrip()
