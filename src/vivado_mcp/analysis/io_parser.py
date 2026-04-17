"""IO 报告解析器：将 Vivado report_io -return_string 输出解析为结构化数据。

解析管道分隔的表格格式，提取端口名称、引脚分配、方向、IO 标准等信息。
自动区分 GT（高速收发器）端口和 GPIO 端口。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class IoPort:
    """单个 IO 端口的结构化信息。

    Attributes:
        port_name: 端口名称，如 "sys_clk_p" 或 "pcie_7x_mgt_rtl_0_rxp[0]"。
        package_pin: 封装引脚，如 "AB8"；未分配时为空字符串。
        site: FPGA 内部站点，如 "IOB_X0Y158" 或 "MGTXRXP3_116"。
        direction: 方向——"INPUT"、"OUTPUT" 或 "INOUT"。
        io_standard: IO 标准，如 "LVCMOS33"、"LVDS_25"；GT 端口通常为空字符串。
        bank: IO Bank 编号。
        fixed: 引脚约束是否已锁定。
        io_type: "GT"（高速收发器）或 "GPIO"（通用 IO）。
    """

    port_name: str
    package_pin: str
    site: str
    direction: str
    io_standard: str
    bank: int
    fixed: bool
    io_type: str


@dataclass
class IoReport:
    """report_io 的完整解析结果。

    Attributes:
        ports: 所有端口列表。
        total_ports: 总端口数。
        gt_ports: GT（高速收发器）端口数。
        gpio_ports: GPIO（通用 IO）端口数。
        unplaced_ports: 未分配引脚的端口数（package_pin 为空）。
    """

    ports: list[IoPort] = field(default_factory=list)
    total_ports: int = 0
    gt_ports: int = 0
    gpio_ports: int = 0
    unplaced_ports: int = 0

    def to_dict(self) -> dict:
        """转换为 JSON 可序列化的字典。"""
        return {
            "ports": [asdict(p) for p in self.ports],
            "total_ports": self.total_ports,
            "gt_ports": self.gt_ports,
            "gpio_ports": self.gpio_ports,
            "unplaced_ports": self.unplaced_ports,
        }


# 分隔线模式：+---+---+...+
_SEPARATOR_RE = re.compile(r"^\+[-+]+\+$")


def _detect_column_boundaries(separator_line: str) -> list[tuple[int, int]]:
    """从分隔线中检测列边界位置。

    分隔线格式如：+-------+-------+-------+
    每个 '+' 是列分隔符，两个 '+' 之间是一列的范围。

    Returns:
        列边界列表，每项为 (start, end) 位置索引。
    """
    boundaries: list[tuple[int, int]] = []
    plus_positions = [i for i, ch in enumerate(separator_line) if ch == "+"]

    for i in range(len(plus_positions) - 1):
        # 列内容在两个 '+' 之间（不含 '+'）
        start = plus_positions[i] + 1
        end = plus_positions[i + 1]
        boundaries.append((start, end))

    return boundaries


def _is_gt_site(site: str) -> bool:
    """判断站点是否为 GT（高速收发器）。

    GT 站点名称包含 "MGT"，例如 MGTXRXP3_116、MGTXTXP0_115。
    """
    return "MGT" in site.upper()


def _parse_bool(value: str) -> bool:
    """将字符串解析为布尔值（TRUE/true → True）。"""
    return value.strip().upper() == "TRUE"


def _parse_bank(value: str) -> int:
    """将 bank 字段解析为整数。空值或非数字返回 0。"""
    stripped = value.strip()
    if not stripped:
        return 0
    try:
        return int(stripped)
    except ValueError:
        return 0


def parse_report_io(raw_text: str) -> IoReport:
    """解析 Vivado report_io -return_string 输出。

    支持两种表格格式：
    - **按 Port**: 表头含 "Port Name"（新版 Vivado 或部分情况）
    - **按 Pin**: 表头含 "Pin Number" + "Signal Name"（Vivado 2019.1 等）
      这种格式每行是一个物理引脚，只取 "Signal Name" 非空的作为端口。

    解析策略：
    1. 查找分隔线以确定列边界
    2. 识别表头类型
    3. 在分隔线之间解析数据行
    4. 为每行数据创建 IoPort 实例

    Args:
        raw_text: Vivado report_io 的原始文本输出。

    Returns:
        解析后的 IoReport 实例。
    """
    if not raw_text or not raw_text.strip():
        return IoReport()

    lines = raw_text.splitlines()

    # 第一步：找到所有分隔线及其位置
    separator_indices: list[int] = []
    for i, line in enumerate(lines):
        if _SEPARATOR_RE.match(line.strip()):
            separator_indices.append(i)

    if len(separator_indices) < 2:
        # 表格至少需要表头上方、表头下方两条分隔线
        return IoReport()

    # 第二步：识别表头格式（Port Name 版 vs Pin Number 版）
    header_sep_idx = -1
    header_line_idx = -1
    header_style = ""  # "port" 或 "pin"
    for i, idx in enumerate(separator_indices):
        candidate = idx + 1
        if candidate >= len(lines):
            continue
        header_text = lines[candidate]
        if "Port Name" in header_text:
            header_sep_idx = i
            header_line_idx = candidate
            header_style = "port"
            break
        if "Pin Number" in header_text and "Signal Name" in header_text:
            header_sep_idx = i
            header_line_idx = candidate
            header_style = "pin"
            break

    if header_line_idx == -1:
        return IoReport()

    # 第三步：确定列边界（从表头上方的分隔线推断）
    boundaries = _detect_column_boundaries(lines[separator_indices[header_sep_idx]].strip())
    if len(boundaries) < 7:
        return IoReport()

    # 第四步：找到表头下方的分隔线（数据从它之后开始）
    data_start_sep_idx = header_sep_idx + 1
    if data_start_sep_idx >= len(separator_indices):
        return IoReport()

    data_start = separator_indices[data_start_sep_idx] + 1

    # 第五步：确定数据区域的结束位置（下一条分隔线或文件末尾）
    if data_start_sep_idx + 1 < len(separator_indices):
        data_end = separator_indices[data_start_sep_idx + 1]
    else:
        data_end = len(lines)

    # 第六步：解析数据行
    ports: list[IoPort] = []

    # 为 Pin 版找出关键列索引（表头顺序可能变化）
    pin_col_idx: dict[str, int] = {}
    if header_style == "pin":
        header_cells = _extract_cells(lines[header_line_idx])
        for i, cell in enumerate(header_cells):
            key = cell.strip()
            if key in ("Pin Number", "Signal Name", "Pin Name", "Use",
                       "IO Standard", "IO Bank", "Constraint"):
                pin_col_idx[key] = i

    for i in range(data_start, data_end):
        line = lines[i]
        if not line.strip() or _SEPARATOR_RE.match(line.strip()):
            continue

        # 按 '|' 分隔提取单元格内容
        cells = _extract_cells(line)

        if header_style == "port":
            if len(cells) < 7:
                continue
            port_name = cells[0].strip()
            package_pin = cells[1].strip()
            site = cells[2].strip()
            direction = cells[3].strip()
            io_standard = cells[4].strip()
            bank = _parse_bank(cells[5])
            fixed = _parse_bool(cells[6])
        else:  # "pin" 版
            def _col(name: str, default: str = "") -> str:
                ci = pin_col_idx.get(name, -1)
                if 0 <= ci < len(cells):
                    return cells[ci].strip()
                return default

            signal = _col("Signal Name")
            if not signal:
                # 物理引脚没有用户信号映射到它，跳过
                continue
            port_name = signal
            package_pin = _col("Pin Number")
            site = _col("Pin Name")
            direction = _col("Use")  # "INPUT" / "OUTPUT" / "INOUT"（或 "Gigabit" 等特殊）
            io_standard = _col("IO Standard")
            bank = _parse_bank(_col("IO Bank"))
            fixed = _col("Constraint").upper() == "FIXED"

        if not port_name:
            continue

        io_type = "GT" if _is_gt_site(site) else "GPIO"

        ports.append(
            IoPort(
                port_name=port_name,
                package_pin=package_pin,
                site=site,
                direction=direction,
                io_standard=io_standard,
                bank=bank,
                fixed=fixed,
                io_type=io_type,
            )
        )

    # 第七步：统计汇总
    gt_count = sum(1 for p in ports if p.io_type == "GT")
    gpio_count = sum(1 for p in ports if p.io_type == "GPIO")
    unplaced_count = sum(1 for p in ports if not p.package_pin)

    return IoReport(
        ports=ports,
        total_ports=len(ports),
        gt_ports=gt_count,
        gpio_ports=gpio_count,
        unplaced_ports=unplaced_count,
    )


def _extract_cells(line: str) -> list[str]:
    """从管道分隔的行中提取单元格内容。

    输入如：| value1 | value2 | value3 |
    返回：["value1", "value2", "value3"]

    去除首尾的 '|'，按 '|' 分隔，保留各单元格的原始空白（由调用方 strip）。
    """
    stripped = line.strip()
    # 去除首尾的 '|'
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]

    return stripped.split("|")
