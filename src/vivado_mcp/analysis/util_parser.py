"""Vivado report_utilization 解析器。

把 ``report_utilization -return_string`` 输出的多个资源表格解析为
结构化数据,重点提取 xc7a35t 等小型 FPGA 常见瓶颈资源(LUT/FF/BRAM/DSP/IO)。

纯 Python 模块,不依赖 Vivado 进程。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

# ============================================================== #
#  数据结构
# ============================================================== #


@dataclass(frozen=True)
class ResourceUsage:
    """单项资源占用(如 LUT/FF/BRAM)。"""
    name: str        # 资源名称(如 "Slice LUTs")
    used: int        # 已用数量
    available: int   # 总可用数
    percent: float   # 使用百分比 0-100

    @property
    def is_critical(self) -> bool:
        """占用超过 90% 视为危险(布线/时序可能炸)。"""
        return self.percent >= 90.0

    @property
    def is_warning(self) -> bool:
        """占用 70-90% 区间视为预警。"""
        return 70.0 <= self.percent < 90.0


@dataclass
class UtilizationReport:
    """完整资源占用报告。"""
    resources: list[ResourceUsage] = field(default_factory=list)

    def get(self, name: str) -> ResourceUsage | None:
        """按名称(大小写不敏感)查找资源项。"""
        for r in self.resources:
            if r.name.lower() == name.lower():
                return r
        return None

    def to_dict(self) -> dict:
        return {
            "resources": [asdict(r) for r in self.resources],
            "critical": [r.name for r in self.resources if r.is_critical],
            "warning": [r.name for r in self.resources if r.is_warning],
        }


# ============================================================== #
#  正则
# ============================================================== #

# Vivado utilization 表格的数据行,例如:
# | Slice LUTs                 |  1234 |     0 |     20800 |  5.93 |
# 列固定顺序: Name | Used | Fixed | Available | Util%
_ROW_RE = re.compile(
    r"^\|\s*(?P<name>[^|]+?)\s*\|"
    r"\s*(?P<used>\d+)\s*\|"
    r"\s*\d+\s*\|"                 # Fixed(忽略)
    r"\s*(?P<avail>\d+)\s*\|"
    r"\s*(?P<pct>[\d.]+)\s*\|"
)

# 只关心这几个核心资源(xc7a35t 常见瓶颈,按重要度排)
_CORE_RESOURCES = (
    "Slice LUTs",
    "LUT as Logic",
    "LUT as Memory",
    "Slice Registers",
    "Register as Flip Flop",
    "Block RAM Tile",
    "DSPs",
    "Bonded IOB",
    "BUFGCTRL",
    "MMCME2_ADV",
    "PLLE2_ADV",
)


def parse_utilization(raw_text: str) -> UtilizationReport:
    """从 report_utilization 输出解析出核心资源占用。

    注意:Vivado 原始报告包含多个表格(CLB Logic/Memory/IO/Clocking...)
    我们只抽取核心资源行,避免噪音。
    """
    resources: list[ResourceUsage] = []
    seen: set[str] = set()

    for line in raw_text.splitlines():
        m = _ROW_RE.match(line)
        if m is None:
            continue

        name = m.group("name").strip()
        # 只收录核心资源,避免表头/子项噪音
        if name not in _CORE_RESOURCES:
            continue
        # 去重(同名资源可能在多个表格出现)
        if name in seen:
            continue
        seen.add(name)

        try:
            used = int(m.group("used"))
            avail = int(m.group("avail"))
            pct = float(m.group("pct"))
        except ValueError:
            continue

        resources.append(ResourceUsage(name=name, used=used, available=avail, percent=pct))

    return UtilizationReport(resources=resources)


def format_utilization_report(report: UtilizationReport) -> str:
    """格式化为人类可读的中文报告,高亮危险/预警项。"""
    if not report.resources:
        return ("未解析到资源占用数据。请确认已跑过综合或实现,"
                "再在 open_run 后调用本工具。")

    lines: list[str] = ["=== 资源占用摘要 ==="]
    has_critical = any(r.is_critical for r in report.resources)
    has_warning = any(r.is_warning for r in report.resources)

    if has_critical:
        lines.insert(0, "!! 资源超限风险 !!")
    elif has_warning:
        lines.insert(0, "[!] 资源接近上限")

    # 按百分比降序,用户一眼看到瓶颈
    sorted_res = sorted(report.resources, key=lambda r: r.percent, reverse=True)
    for r in sorted_res:
        flag = ""
        if r.is_critical:
            flag = " [CRITICAL]"
        elif r.is_warning:
            flag = " [WARN]"
        lines.append(
            f"  {r.name:<30s} {r.used:>7d} / {r.available:<7d}  "
            f"({r.percent:5.2f}%){flag}"
        )

    if has_critical:
        lines.append("")
        lines.append("建议:")
        lines.append("  - 资源占用 > 90% 会导致布线拥塞,甚至时序无法收敛")
        lines.append("  - 考虑优化 RTL(共享资源 / 减少并行度)或换更大芯片")
    elif has_warning:
        lines.append("")
        lines.append("建议: 资源占用偏高,留意后续功能扩展的余量。")

    return "\n".join(lines)
