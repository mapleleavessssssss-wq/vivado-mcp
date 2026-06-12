"""XDC 自动修复器 (pure Python)。

把 xdc_linter 检出的"可安全自动修"问题转成补丁,dry-run 预览 / 实际写回。

**只修**这两类(其他问题太依赖人工判断,坚决不自动改):
- MISSING_IOSTANDARD —— 在 PACKAGE_PIN 行之后插入 IOSTANDARD 语句
- CLOCK_NO_PERIOD    —— 仅当 board 已知时补 -period(未知板跳过)

**绝对不碰**:
- PIN_CONFLICT / PIN_CONFLICT_CROSS_FILE —— 不知道哪条是对的
- DUPLICATE_PORT —— 后者覆盖前者,但可能是用户故意(多板复用)
- FILE_NOT_FOUND —— 没文件没法修
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from vivado_mcp.analysis.xdc_linter import LintIssue, lint_xdc_files
from vivado_mcp.analysis.xdc_parser import _ends_with_continuation, _read_xdc_text

logger = logging.getLogger(__name__)

# 板卡 profile:已知板的常用 IOSTANDARD + 时钟频率
# 加新板时:iostd 按板上 bank 电压(3.3V→LVCMOS33,2.5V→LVCMOS25,1.8V→LVCMOS18);
# clock_period_ns = 1000 / 频率MHz;clock_port_name 匹配 XDC 里常用端口名。
BOARD_PROFILES: dict[str, dict] = {
    "basys3":   {"iostd": "LVCMOS33", "clock_period_ns": 10.0},   # 100 MHz
    "nexys-a7": {"iostd": "LVCMOS33", "clock_period_ns": 10.0},   # 100 MHz
    "arty-a7":  {"iostd": "LVCMOS33", "clock_period_ns": 10.0},   # 100 MHz
    "zybo":     {"iostd": "LVCMOS33", "clock_period_ns": 8.0},    # 125 MHz
    "kc705":    {"iostd": "LVCMOS25", "clock_period_ns": 5.0},    # 200 MHz
}

DEFAULT_IOSTD = "LVCMOS33"  # 未知板时的兜底


@dataclass(frozen=True)
class Patch:
    """一条修复补丁。"""
    file: str
    rule: str              # 原 lint rule(MISSING_IOSTANDARD / CLOCK_NO_PERIOD)
    action: str            # "insert_after" / "modify"
    line: int              # 参考行号(原文件)
    before_text: str       # 原文(insert_after 时为触发行,modify 时为原行)
    after_text: str        # 修后文本(insert_after 时为新插入行,modify 时为替换后行)
    reason: str            # 人类可读说明


@dataclass
class FixReport:
    """总修复报告。"""
    dry_run: bool = True
    board: str = ""
    patches: list[Patch] = field(default_factory=list)
    skipped: list[LintIssue] = field(default_factory=list)  # 检出但不能自动修
    files_modified: list[str] = field(default_factory=list)
    failed_files: list[tuple[str, str]] = field(default_factory=list)  # (路径, 失败原因)

    def to_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "board": self.board,
            "patch_count": len(self.patches),
            "skipped_count": len(self.skipped),
            "files_modified": self.files_modified,
            "failed_files": [{"file": f, "reason": r} for f, r in self.failed_files],
            "patches": [asdict(p) for p in self.patches],
            "skipped": [asdict(i) for i in self.skipped],
        }


def _iostd_for_board(board: str) -> str:
    return BOARD_PROFILES.get(board.lower(), {}).get("iostd", DEFAULT_IOSTD)


def _clock_period_for_board(board: str) -> float | None:
    return BOARD_PROFILES.get(board.lower(), {}).get("clock_period_ns")


def _tag() -> str:
    return f"# auto-fixed by xdc_auto_fix {date.today().isoformat()}"


def _build_iostd_patch(issue: LintIssue, iostd: str) -> Patch | None:
    """为 MISSING_IOSTANDARD 构造"在原行后插入新行"的补丁。"""
    path = Path(issue.file)
    if not path.exists():
        return None
    try:
        text, encoding = _read_xdc_text(path)
    except OSError as e:
        logger.warning("读取 %s 失败,无法构造 IOSTANDARD 补丁: %s", path, e)
        return None
    if encoding is None:
        logger.warning("%s 编码未知(非 UTF-8/GBK),拒绝构造补丁以免写坏文件", path)
        return None
    lines = text.splitlines()
    if issue.line < 1 or issue.line > len(lines):
        return None
    trigger_line = lines[issue.line - 1]
    # 续行约束(行尾反斜杠):插入点推进到续行链最后一行,避免插进续行中间
    insert_line = issue.line
    while insert_line < len(lines) and _ends_with_continuation(lines[insert_line - 1]):
        insert_line += 1
    # 终端守卫:续行链悬挂到文件末尾(插入点前一行仍以反斜杠结尾)时,
    # Tcl 会把 backslash-newline 折叠成空格,新行被拼进上一条命令变成非法 Tcl
    # —— 拒绝自动修,归入 skipped 由人工处理(与 _build_clock_patch 守卫同款)
    if _ends_with_continuation(lines[insert_line - 1]):
        return None
    # 对齐缩进
    indent = ""
    for ch in trigger_line:
        if ch in (" ", "\t"):
            indent += ch
        else:
            break
    new_line = (
        f"{indent}set_property IOSTANDARD {iostd} "
        f"[get_ports {{{issue.port}}}]   {_tag()}"
    )
    return Patch(
        file=str(path),
        rule="MISSING_IOSTANDARD",
        action="insert_after",
        line=insert_line,
        before_text=trigger_line,
        after_text=new_line,
        reason=f"补 IOSTANDARD={iostd}:消除 NSTD-1/BIVC-1 隐患",
    )


def _build_clock_patch(issue: LintIssue, period_ns: float) -> Patch | None:
    """为 CLOCK_NO_PERIOD 构造"就地修改 create_clock 行"的补丁。"""
    path = Path(issue.file)
    if not path.exists():
        return None
    try:
        text, encoding = _read_xdc_text(path)
    except OSError as e:
        logger.warning("读取 %s 失败,无法构造 create_clock 补丁: %s", path, e)
        return None
    if encoding is None:
        logger.warning("%s 编码未知(非 UTF-8/GBK),拒绝构造补丁以免写坏文件", path)
        return None
    lines = text.splitlines()
    if issue.line < 1 or issue.line > len(lines):
        return None
    original = lines[issue.line - 1]
    # 续行的 create_clock(行尾反斜杠):就地改写会把注释 tag 拼到反斜杠之后,
    # 断掉 Tcl 续行结构 —— 拒绝自动修,归入 skipped 由人工处理
    if _ends_with_continuation(original):
        return None
    # 简单策略:在 "create_clock" 关键字之后插 -period <ns>
    # 已有 -period 的行压根儿不会进这里(lint 不会报)
    idx = original.lower().find("create_clock")
    if idx < 0:
        return None
    insert_at = idx + len("create_clock")
    modified = (
        original[:insert_at]
        + f" -period {period_ns}"
        + original[insert_at:]
        + f"   {_tag()}"
    )
    return Patch(
        file=str(path),
        rule="CLOCK_NO_PERIOD",
        action="modify",
        line=issue.line,
        before_text=original,
        after_text=modified,
        reason=f"补 -period {period_ns} ns:Vivado 没 period 不会做时序分析",
    )


def plan_fixes(
    xdc_paths: list[str | Path],
    board: str = "",
) -> FixReport:
    """扫描 XDC 文件并生成修复计划(不落盘)。"""
    report = FixReport(dry_run=True, board=board)

    lint_report = lint_xdc_files(xdc_paths)
    iostd = _iostd_for_board(board)
    period = _clock_period_for_board(board)

    for issue in lint_report.issues:
        if issue.rule == "MISSING_IOSTANDARD":
            patch = _build_iostd_patch(issue, iostd)
            if patch is not None:
                report.patches.append(patch)
            else:
                report.skipped.append(issue)
        elif issue.rule == "CLOCK_NO_PERIOD":
            if period is None:
                # 未知板,不猜 period
                report.skipped.append(issue)
            else:
                patch = _build_clock_patch(issue, period)
                if patch is not None:
                    report.patches.append(patch)
                else:
                    report.skipped.append(issue)
        else:
            # PIN_CONFLICT / DUPLICATE_PORT / FILE_NOT_FOUND / CROSS_FILE
            # 一律不自动改
            report.skipped.append(issue)

    return report


def apply_fixes(report: FixReport) -> FixReport:
    """把 plan_fixes 的计划实际写回磁盘。返回更新了 dry_run/files_modified 的新报告。

    策略:
    - 按文件分组 patches
    - 同一文件内,按 line 降序处理 insert_after(从下往上插,行号不会偏移)
    - modify 直接就地改
    - 编码按读取时探测的结果原样写回(UTF-8/GBK),编码未知则拒绝写,不损坏文件
    - 保留原文件主导行尾(CRLF/LF);写回前留 .bak 备份,临时文件 + os.replace 原子落盘
    - .bak 只保留最近一次修改前的版本:再次 apply 会覆盖旧 .bak(sed -i.bak 同款语义)
    - 任一文件失败(读取/备份/写回)记入 failed_files(路径+具体原因),
      写回失败时顺带删掉本次刚建的 .bak(原文件未变,留着会误导"已经修过")
    """
    by_file: dict[str, list[Patch]] = {}
    for p in report.patches:
        by_file.setdefault(p.file, []).append(p)

    modified_files: list[str] = []
    failed_files: list[tuple[str, str]] = []

    for file_path, patches in by_file.items():
        path = Path(file_path)
        try:
            text, encoding = _read_xdc_text(path)
        except OSError as e:
            logger.warning("读取 %s 失败,跳过该文件的修复: %s", path, e)
            failed_files.append((str(path), f"读取失败: {e}"))
            continue
        if encoding is None:
            logger.warning(
                "[DEGRADED] %s 编码既不是 UTF-8 也不是 GBK,拒绝写回以免损坏文件内容", path
            )
            failed_files.append(
                (str(path), "编码既不是 UTF-8 也不是 GBK,拒绝写回以免损坏文件内容")
            )
            continue
        # 保留原文件主导行尾:出现过 CRLF 就按 CRLF 写回(Windows XDC 普遍 CRLF)
        newline = "\r\n" if "\r\n" in text else "\n"
        lines = text.splitlines()

        # 先处理 modify(就地改),再处理 insert_after 但按 line 降序
        # insert_after 降序避免行号偏移
        inserts = sorted(
            [p for p in patches if p.action == "insert_after"],
            key=lambda x: x.line,
            reverse=True,
        )
        modifies = [p for p in patches if p.action == "modify"]

        for p in modifies:
            if 1 <= p.line <= len(lines):
                lines[p.line - 1] = p.after_text

        for p in inserts:
            if 1 <= p.line <= len(lines):
                lines.insert(p.line, p.after_text)

        new_text = newline.join(lines)
        if text.endswith("\n"):
            new_text += newline

        # 写回前留 .bak 备份(用户约束文件,改坏可恢复)
        backup = Path(str(path) + ".bak")
        try:
            shutil.copy2(path, backup)
        except OSError as e:
            logger.warning("创建备份 %s 失败,跳过该文件的修复: %s", backup, e)
            failed_files.append((str(path), f"创建备份 {backup.name} 失败: {e}"))
            continue

        # 临时文件 + os.replace 原子替换,避免写一半进程被杀截断原文件
        tmp_path = path.with_name(f".{path.name}.tmp")
        try:
            tmp_path.write_bytes(new_text.encode(encoding))
            os.replace(tmp_path, path)
            modified_files.append(str(path))
        except OSError as e:
            logger.warning("写回 %s 失败: %s", path, e)
            failed_files.append((str(path), f"写回失败: {e}"))
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError as e2:
                logger.warning("清理临时文件 %s 失败: %s", tmp_path, e2)
            # 原文件没改成,刚建的 .bak 与原文件内容相同 —— 删掉,
            # 留着会误导用户以为"已经修过一轮"
            try:
                backup.unlink(missing_ok=True)
            except OSError as e3:
                logger.warning("清理备份 %s 失败: %s", backup, e3)
            continue

    # 复制一份,把 dry_run=False
    result = FixReport(
        dry_run=False,
        board=report.board,
        patches=report.patches,
        skipped=report.skipped,
        files_modified=modified_files,
        failed_files=failed_files,
    )
    return result


def format_fix_report(report: FixReport) -> str:
    """格式化修复报告。"""
    mode = "DRY-RUN(仅预览,未写盘)" if report.dry_run else "APPLIED(已写入)"
    header = f"=== XDC 自动修复: {mode} ==="

    out: list[str] = [header]
    out.append(f"板卡 profile: {report.board or '(未指定,只用默认 IOSTANDARD=LVCMOS33)'}")
    out.append(f"补丁: {len(report.patches)} 条  |  跳过(需人工处理): {len(report.skipped)} 条")

    if not report.dry_run:
        out.append(f"已修改文件: {len(report.files_modified)} 个")
        for f in report.files_modified:
            out.append(f"  - {f}")

    # 写回失败必须在用户可见报告里露出,只靠"已修改文件: 0 个"极易被当成成功
    if report.failed_files:
        out.append("")
        out.append(f"[DEGRADED] 以下文件写回失败({len(report.failed_files)} 个),约束未被修改:")
        for f, reason in report.failed_files:
            out.append(f"  - {f}: {reason}")

    if report.patches:
        out.append("")
        out.append("--- 修复补丁 ---")
        by_file: dict[str, list[Patch]] = {}
        for p in report.patches:
            by_file.setdefault(p.file, []).append(p)
        for file_path, patches in by_file.items():
            out.append(f"[{Path(file_path).name}]  ({len(patches)} 条)")
            for p in patches:
                out.append(f"  行 {p.line} [{p.rule}] {p.reason}")
                out.append(f"    - 原 : {p.before_text.strip()}")
                if p.action == "insert_after":
                    out.append(f"    + 新 : {p.after_text.strip()}")
                else:
                    out.append(f"    + 改 : {p.after_text.strip()}")

    if report.skipped:
        out.append("")
        out.append("--- 跳过(需手动处理) ---")
        for i in report.skipped:
            out.append(
                f"  [{i.rule}] {Path(i.file).name}:{i.line}  "
                f"{i.message[:80]}"
            )

    if report.dry_run and report.patches:
        out.append("")
        out.append("确认无误后,重新调用:xdc_auto_fix(..., dry_run=False)")

    return "\n".join(out)
