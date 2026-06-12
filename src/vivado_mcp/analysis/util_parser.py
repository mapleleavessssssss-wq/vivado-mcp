"""Vivado report_utilization 解析器。

把 ``report_utilization -return_string`` 输出的多个资源表格解析为
结构化数据,重点提取 xc7a35t 等小型 FPGA 常见瓶颈资源(LUT/FF/BRAM/DSP/IO)。

纯 Python 模块,不依赖 Vivado 进程。
"""

from __future__ import annotations

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
    # Block RAM 子表行(RAMB36/FIFO* 等),仅 detail 输出用,不进 resources。
    # "RAMB36E1 only" 这类行真实报告里 Available/Util% 单元格为空 → 记 0。
    bram_detail: list[ResourceUsage] = field(default_factory=list)
    # 格式不识别时的说明(有表格行但所有表头都无法定位列),正常为空
    parse_error: str = ""

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
#  表格解析
# ============================================================== #

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

# Memory 表的 Block RAM 子行(detail=True 时输出,不进 resources)
_BRAM_DETAIL_RESOURCES = (
    "RAMB36/FIFO*",
    "RAMB36E1 only",
    "RAMB18",
    "RAMB18E1 only",
)


def _split_cells(line: str) -> list[str] | None:
    """把 ``| a | b | c |`` 表格行拆成单元格列表;非表格行返回 None。"""
    s = line.strip()
    if len(s) < 2 or not (s.startswith("|") and s.endswith("|")):
        return None
    return [c.strip() for c in s[1:-1].split("|")]


def parse_utilization(raw_text: str) -> UtilizationReport:
    """从 report_utilization 输出解析出核心资源占用。

    注意:Vivado 原始报告包含多个表格(CLB Logic/Memory/IO/Clocking...)
    我们只抽取核心资源行,避免噪音。

    列索引从表头动态定位:2019.1 是 Name|Used|Fixed|Available|Util% 五列,
    Vivado 2020.1+ 在 Fixed 后多一列 Prohibited —— 按固定位置取列会整列错位
    (available=0、percent=资源总数,产出假 CRITICAL)。
    有表格行但解析不出任何核心资源时,设置 parse_error 显式报告降级
    (区分"表头不识别"与"表头可识别但行名不在收录集",如 UltraScale
    的 'CLB LUTs' 命名),而不是静默返回空结果。
    """
    resources: list[ResourceUsage] = []
    bram_detail: list[ResourceUsage] = []
    seen: set[str] = set()
    seen_detail: set[str] = set()

    # 当前表格的列索引 (used, available, util%),由最近一个表头行决定
    col: tuple[int, int, int] | None = None
    saw_table_row = False
    saw_known_header = False

    for line in raw_text.splitlines():
        cells = _split_cells(line)
        if cells is None:
            continue
        saw_table_row = True

        # 表头行:动态定位列索引(兼容 2020.1+ 的 Prohibited 列)
        if "Used" in cells:
            try:
                col = (cells.index("Used"), cells.index("Available"), cells.index("Util%"))
                saw_known_header = True
            except ValueError:
                # 有 Used 但缺 Available/Util% 的未知表头:本表数据行不解析
                col = None
            continue

        if col is None:
            continue
        used_i, avail_i, pct_i = col
        if len(cells) <= max(used_i, avail_i, pct_i):
            continue

        name = cells[0]
        # 去重(同名资源可能在多个表格出现),只收核心资源/BRAM 子行
        if name in _CORE_RESOURCES and name not in seen:
            try:
                used = int(cells[used_i])
                avail = int(cells[avail_i])
                pct = float(cells[pct_i])
            except ValueError:
                continue
            seen.add(name)
            resources.append(ResourceUsage(name=name, used=used, available=avail, percent=pct))
        elif name in _BRAM_DETAIL_RESOURCES and name not in seen_detail:
            # 子行(如 "RAMB36E1 only")的 Available/Util% 单元格可能为空 → 记 0
            try:
                used = int(cells[used_i])
            except ValueError:
                continue
            try:
                avail = int(cells[avail_i]) if cells[avail_i] else 0
                pct = float(cells[pct_i]) if cells[pct_i] else 0.0
            except ValueError:
                avail, pct = 0, 0.0
            seen_detail.add(name)
            bram_detail.append(
                ResourceUsage(name=name, used=used, available=avail, percent=pct)
            )

    parse_error = ""
    if not resources and saw_table_row:
        if saw_known_header:
            # 表头能定位列,但没有任何数据行命中收录的核心资源名:
            # 典型如 UltraScale 的 'CLB LUTs'/'CLB Registers'(非 7 系列命名)
            parse_error = (
                "资源报告表头可识别(Used/Available/Util%),但没有任何行名"
                "命中收录的核心资源(Slice LUTs 等 7 系列命名)。"
                "可能是非 7 系列器件(如 UltraScale 的 'CLB LUTs')的命名差异,"
                '请用 run_tcl("report_utilization -return_string") 查看原始报告。'
            )
        else:
            parse_error = (
                "资源报告格式不识别:检测到表格行,但没有任何表头包含 "
                "Used/Available/Util% 列。可能是不支持的 Vivado 版本格式,"
                '请用 run_tcl("report_utilization -return_string") 查看原始报告。'
            )

    return UtilizationReport(
        resources=resources, bram_detail=bram_detail, parse_error=parse_error
    )


def format_utilization_report(report: UtilizationReport, detail: bool = False) -> str:
    """格式化为人类可读的中文报告,高亮危险/预警项。

    Args:
        report: parse_utilization 的解析结果。
        detail: True 时末尾附加 Block RAM 明细段(RAMB36/FIFO* / RAMB36E1 only /
            RAMB18 的 used/available)。默认 False,输出与原有格式逐字节一致。
    """
    if not report.resources:
        if report.parse_error:
            return f"[DEGRADED] {report.parse_error}"
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

    # Block RAM 明细(detail=True 时输出;detail=False 路径完全不变)
    if detail:
        lines.append("")
        lines.append("--- Block RAM 明细 ---")
        if report.bram_detail:
            for r in report.bram_detail:
                if r.available > 0:
                    lines.append(
                        f"  {r.name:<30s} {r.used:>7d} / {r.available:<7d}  "
                        f"({r.percent:5.2f}%)"
                    )
                else:
                    # "RAMB36E1 only" 行原始报告无 Available/Util%,只显示 used
                    lines.append(f"  {r.name:<30s} {r.used:>7d}")
        else:
            lines.append("  (未解析到 Block RAM 子表行 —— 设计可能未使用 BRAM)")

    return "\n".join(lines)
