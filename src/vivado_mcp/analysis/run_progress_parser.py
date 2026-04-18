"""Run 进度解析器:把 QUERY_RUN_PROGRESS 的 Tcl 输出翻成结构化数据。

用途:``get_run_progress`` 工具。用户起了 run_synthesis / run_implementation
但 10-30 分钟的黑盒等待里想知道"当前走到第几步"。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

_META_RE = re.compile(r"VMCP_RUN:(\w+)=(.*)")
_PHASE_RE = re.compile(r"VMCP_RUN_PHASE:(\d+)\|(.*)")
_TAIL_RE = re.compile(r"VMCP_RUN_TAIL:(\d+)\|(.*)")
_ERR_RE = re.compile(r"VMCP_RUN_ERROR:(.*)")


@dataclass(frozen=True)
class PhaseLine:
    """runme.log 中一条 Phase/Starting/Finished 关键阶段行。"""
    lineno: int
    text: str


@dataclass
class RunProgress:
    """单个 run(synth_1 / impl_1)的运行快照。"""
    run_name: str = ""
    found: bool = False
    error: str = ""

    # Vivado run 属性
    status: str = ""          # 如 "route_design Running" / "synth_design Complete!"
    progress: str = ""        # 如 "50%"

    # log 元信息
    log_path: str = ""
    log_exists: bool = False
    log_size: int = 0
    log_mtime: int = 0        # Unix epoch(Tcl file mtime)
    total_lines: int = 0

    # 结构化阶段 + 尾部
    phases: list[PhaseLine] = field(default_factory=list)
    tail: list[PhaseLine] = field(default_factory=list)

    def is_running(self) -> bool:
        s = self.status.lower()
        return "running" in s or "queued" in s

    def is_complete(self) -> bool:
        return "Complete!" in self.status

    def is_error(self) -> bool:
        return "ERROR" in self.status.upper()

    def current_phase(self) -> str:
        """最后一条 Phase/Starting 行,表示'当前在哪一步'。"""
        if not self.phases:
            return ""
        return self.phases[-1].text

    def elapsed_since_last_update(self) -> int:
        """日志最后修改距现在的秒数(判断 run 是否还在活跃)。"""
        if self.log_mtime == 0:
            return -1
        return int(time.time()) - self.log_mtime


def parse_run_progress(raw: str, run_name: str = "") -> RunProgress:
    """解析 QUERY_RUN_PROGRESS 的 Tcl 输出。"""
    rp = RunProgress(run_name=run_name)

    for line in raw.splitlines():
        line = line.rstrip()

        m_err = _ERR_RE.match(line)
        if m_err is not None:
            rp.error = m_err.group(1).strip()
            rp.found = False
            continue

        m_meta = _META_RE.match(line)
        if m_meta is not None:
            rp.found = True
            key, value = m_meta.group(1), m_meta.group(2).strip()
            if key == "status":
                rp.status = value
            elif key == "progress":
                rp.progress = value
            elif key == "dir":
                rp.log_path = f"{value}/runme.log" if value else ""
            elif key == "log_exists":
                rp.log_exists = value == "1"
            elif key == "log_size":
                try:
                    rp.log_size = int(value)
                except ValueError:
                    pass
            elif key == "log_mtime":
                try:
                    rp.log_mtime = int(value)
                except ValueError:
                    pass
            elif key == "total_lines":
                try:
                    rp.total_lines = int(value)
                except ValueError:
                    pass
            continue

        m_phase = _PHASE_RE.match(line)
        if m_phase is not None:
            rp.phases.append(PhaseLine(
                lineno=int(m_phase.group(1)),
                text=m_phase.group(2).strip(),
            ))
            continue

        m_tail = _TAIL_RE.match(line)
        if m_tail is not None:
            rp.tail.append(PhaseLine(
                lineno=int(m_tail.group(1)),
                text=m_tail.group(2).rstrip(),
            ))
            continue

    return rp


def _fmt_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _fmt_age(seconds: int) -> str:
    if seconds < 0:
        return "未知"
    if seconds < 60:
        return f"{seconds} 秒前"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    return f"{seconds // 3600} 小时 {(seconds % 3600) // 60} 分前"


def format_run_progress(rp: RunProgress, phase_window: int = 5) -> str:
    """人类可读报告。phase_window = 显示最近几条 Phase 行。"""
    if rp.error:
        return f"[ERROR] {rp.error}"
    if not rp.found:
        return f"[ERROR] Run '{rp.run_name}' 未找到或无状态数据。"

    # 状态标签
    if rp.is_error():
        label = "失败"
    elif rp.is_complete():
        label = "已完成"
    elif rp.is_running():
        label = "运行中"
    else:
        label = rp.status or "未启动"

    out: list[str] = [f"=== {rp.run_name} 运行进度: {label} ==="]
    out.append(f"状态:   {rp.status or '(无)'}")
    if rp.progress:
        out.append(f"进度:   {rp.progress}")

    if not rp.log_exists:
        out.append("")
        out.append("日志:   runme.log 不存在(run 可能未启动)")
        return "\n".join(out)

    age = rp.elapsed_since_last_update()
    out.append(
        f"日志:   {rp.log_path}"
        f"  ({_fmt_size(rp.log_size)}, "
        f"{rp.total_lines} 行, 最近 {_fmt_age(age)}更新)"
    )

    if rp.phases:
        recent = rp.phases[-phase_window:]
        out.append("")
        out.append(f"阶段序列(最近 {len(recent)} 条):")
        for i, p in enumerate(recent):
            arrow = " ← 当前" if i == len(recent) - 1 and rp.is_running() else ""
            out.append(f"  L{p.lineno}: {p.text}{arrow}")

    if rp.tail:
        out.append("")
        out.append(f"日志尾部(最后 {len(rp.tail)} 行):")
        for t in rp.tail:
            out.append(f"  {t.text}")

    # 建议
    out.append("")
    if rp.is_error():
        out.append(f"建议: 运行 get_critical_warnings(run_name='{rp.run_name}') 查看 ERROR 详情。")
    elif rp.is_running():
        out.append(f"建议: run 仍在进行,稍后再查。运行 get_run_progress('{rp.run_name}') 刷新。")
        if age > 120:
            out.append(
                f"[!] 注意: log 最近 {_fmt_age(age)}才更新,"
                "若长时间无变化,可能 Vivado 卡住或进程已退。"
            )
    elif rp.is_complete():
        out.append(
            "建议: 运行 get_next_suggestion 看下一步,"
            "或 get_timing_report / get_utilization_report。"
        )

    return "\n".join(out)
