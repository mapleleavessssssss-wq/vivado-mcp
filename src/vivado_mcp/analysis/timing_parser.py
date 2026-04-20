"""Vivado report_timing_summary 解析器。

将 Vivado `report_timing_summary -return_string` 的原始文本输出
解析为结构化 Python 数据对象，供 MCP 工具层直接返回给 LLM。

解析策略：
1. Design Timing Summary 表格 → TimingSummary（WNS/TNS/WHS/THS + 端点计数）
2. 逐条 Slack 路径块 → TimingPath 列表（slack / source / dest / 时钟域等）

不依赖 Vivado 进程，可独立单元测试。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

# 匹配 VMCP_STAGE:stage=X|synth_status=Y|impl_status=Z
_STAGE_RE = re.compile(
    r"VMCP_STAGE:stage=([^|]*)\|synth_status=([^|]*)\|impl_status=(.*)"
)

# ====================================================================== #
#  正则模式
# ====================================================================== #

# Design Timing Summary 表头行（用来定位数据行）
_SUMMARY_HEADER_RE = re.compile(
    r"WNS\(ns\)\s+TNS\(ns\)\s+TNS Failing Endpoints\s+TNS Total Endpoints"
    r"\s+WHS\(ns\)\s+THS\(ns\)\s+THS Failing Endpoints\s+THS Total Endpoints"
)

# 路径块各字段
_SLACK_RE = re.compile(r"Slack\s+\((MET|VIOLATED)\)\s*:\s*([-\d.]+)\s*ns")
_SOURCE_RE = re.compile(r"^\s+Source:\s+(.+)")
_DEST_RE = re.compile(r"^\s+Destination:\s+(.+)")
_PATH_GROUP_RE = re.compile(r"^\s+Path Group:\s+(\S+)")
_PATH_TYPE_RE = re.compile(r"^\s+Path Type:\s+(\S+)")
_REQUIREMENT_RE = re.compile(r"^\s+Requirement:\s+([-\d.]+)\s*ns")
_DATA_DELAY_RE = re.compile(r"^\s+Data Path Delay:\s+([-\d.]+)\s*ns")

# ViolatingPath 专用正则
# "Data Path Delay:  3.766ns  (logic 1.234ns (32.8%)  route 2.532ns (67.2%))"
_DATA_DELAY_SPLIT_RE = re.compile(
    r"Data Path Delay:\s+[-\d.]+\s*ns\s*"
    r"\(\s*logic\s+([-\d.]+)\s*ns.*?route\s+([-\d.]+)\s*ns"
)
# "Logic Levels:   5  (LUT2=1 LUT4=2 ...)"
_LOGIC_LEVELS_RE = re.compile(r"Logic Levels:\s+(\d+)")
# "Clock Path Skew:  -0.042ns (DCD - SCD + CPR)"
_CLOCK_SKEW_RE = re.compile(r"Clock Path Skew:\s+([-\d.]+)\s*ns")
# 续行格式: "  (rising edge-triggered cell FDRE clocked by userclk2  {rise@...})"
_CLOCK_BY_RE = re.compile(r"clocked by\s+(\S+?)\s")

# 块分隔标记 (REPORT_VIOLATING_PATHS 脚本输出的)
_BLOCK_START_RE = re.compile(r"VMCP_PATH_START:type=(setup|hold)")
_BLOCK_END_RE = re.compile(r"VMCP_PATH_END:type=(setup|hold)")
_BLOCK_ERR_RE = re.compile(r"VMCP_PATH_ERROR:(setup|hold)\|(.+)")

# ====================================================================== #
#  数据结构
# ====================================================================== #


@dataclass(frozen=True)
class TimingSummary:
    """时序摘要指标（来自 Design Timing Summary 表格）。"""

    wns: float  # Worst Negative Slack (setup)
    tns: float  # Total Negative Slack (setup)
    whs: float  # Worst Hold Slack
    ths: float  # Total Hold Slack
    failing_endpoints: int  # setup failing endpoints
    total_endpoints: int  # setup total endpoints
    timing_met: bool  # wns >= 0 and whs >= 0


@dataclass(frozen=True)
class TimingPath:
    """单条时序路径信息（来自 Slack 块）。"""

    slack_ns: float
    met: bool  # True = MET, False = VIOLATED
    source: str  # 源寄存器/单元路径
    destination: str  # 目标寄存器/单元路径
    path_group: str  # 时钟域名称（如 "userclk2"）
    path_type: str  # "Setup" 或 "Hold"
    requirement_ns: float
    data_delay_ns: float


@dataclass(frozen=True)
class ViolatingPath:
    """违例路径详情(来自 report_timing -max_paths 的详细输出)。

    比 TimingPath 多了几个修复决策所需的维度:
    - 时钟域(start_clock / end_clock): 识别跨时钟域
    - logic/route delay 分解: 识别组合逻辑过长 vs 走线过长
    - logic levels: 识别需不需要切流水线
    - clock skew: 识别时钟偏斜问题(hold 违例常见)
    """

    slack: float  # ns, 负数 = 违例
    startpoint: str  # 源路径 (reg/Q, port, or RAM output)
    endpoint: str  # 目标路径
    start_clock: str  # 发射端时钟名 (空字符串 = 未识别)
    end_clock: str  # 捕获端时钟名
    logic_delay: float  # ns, 组合逻辑延迟
    route_delay: float  # ns, 走线延迟
    clock_skew: float  # ns, 可正可负
    levels: int  # 逻辑层级数
    type: str  # "setup" 或 "hold"


@dataclass
class TimingReport:
    """完整时序报告：摘要 + 路径列表 + 设计来源元信息。

    ``source_stage`` / ``stage_warning`` 用于解决 Bug 2:告知用户当前时序数据
    的设计阶段(post-synth 估算 vs post-route 最终)。避免 impl 失败时
    用户把 synth 估算误判为"时序通过可以烧板"。
    """

    summary: TimingSummary
    paths: list[TimingPath] = field(default_factory=list)
    # source_stage: "post-synth" / "post-place" / "post-route" / "unknown"
    source_stage: str = "unknown"
    # source_detail: 自由文本,如 "impl_1=place_design ERROR, 显示 synth_1 估算"
    source_detail: str = ""
    # stage_warning: 有风险时填警告文案,否则空字符串
    stage_warning: str = ""
    # violating_paths: 仅在 timing_met=False 时由 get_timing_report 二次查询填充
    violating_paths: list[ViolatingPath] = field(default_factory=list)
    # violating_paths_error: 违例路径查询失败时的错误文本,正常为空
    violating_paths_error: str = ""

    def to_dict(self) -> dict:
        """返回可直接 ``json.dumps`` 序列化的字典。"""
        return {
            "summary": asdict(self.summary),
            "paths": [asdict(p) for p in self.paths],
            "source_stage": self.source_stage,
            "source_detail": self.source_detail,
            "stage_warning": self.stage_warning,
            "violating_paths": [asdict(v) for v in self.violating_paths],
            "violating_paths_error": self.violating_paths_error,
        }


# ====================================================================== #
#  内部辅助
# ====================================================================== #


def _parse_summary_table(text: str) -> TimingSummary:
    """从 Design Timing Summary 表格解析摘要行。

    定位策略：
    1. 找到同时包含 ``WNS(ns)`` 与 ``WHS(ns)`` 的表头行
    2. 跳过紧随其后的 ``-------`` 分隔行
    3. 解析下一行的 8 个数值字段

    如果找不到表头或数据行，返回全零的默认摘要。
    """
    lines = text.splitlines()

    # 寻找 Design Timing Summary 区段内的表头行
    header_idx: int | None = None
    for i, line in enumerate(lines):
        if _SUMMARY_HEADER_RE.search(line):
            header_idx = i
            break

    if header_idx is None:
        return TimingSummary(
            wns=0.0, tns=0.0, whs=0.0, ths=0.0,
            failing_endpoints=0, total_endpoints=0, timing_met=True,
        )

    # 跳过分隔线（一行或多行 -------），找到第一行数据
    data_line: str | None = None
    for j in range(header_idx + 1, min(header_idx + 5, len(lines))):
        stripped = lines[j].strip()
        if stripped and not stripped.startswith("---"):
            data_line = stripped
            break

    if data_line is None:
        return TimingSummary(
            wns=0.0, tns=0.0, whs=0.0, ths=0.0,
            failing_endpoints=0, total_endpoints=0, timing_met=True,
        )

    tokens = data_line.split()
    if len(tokens) < 8:
        return TimingSummary(
            wns=0.0, tns=0.0, whs=0.0, ths=0.0,
            failing_endpoints=0, total_endpoints=0, timing_met=True,
        )

    wns = float(tokens[0])
    tns = float(tokens[1])
    failing_setup = int(tokens[2])
    total_setup = int(tokens[3])
    whs = float(tokens[4])
    ths = float(tokens[5])
    # tokens[6] = THS Failing, tokens[7] = THS Total（暂不单独存储）

    return TimingSummary(
        wns=wns,
        tns=tns,
        whs=whs,
        ths=ths,
        failing_endpoints=failing_setup,
        total_endpoints=total_setup,
        timing_met=(wns >= 0 and whs >= 0),
    )


def _parse_paths(text: str) -> list[TimingPath]:
    """逐块解析 ``Slack (MET/VIOLATED)`` 路径信息。

    每个路径块以 ``Slack (MET)`` 或 ``Slack (VIOLATED)`` 行开头，
    后续字段由缩进的 ``Key: Value`` 行组成。Source / Destination
    可能跨越多行（续行以大量空格 + ``(`` 开头），需要拼接。
    """
    lines = text.splitlines()
    paths: list[TimingPath] = []

    i = 0
    while i < len(lines):
        m_slack = _SLACK_RE.search(lines[i])
        if not m_slack:
            i += 1
            continue

        # 解析当前路径块
        met = m_slack.group(1) == "MET"
        slack_ns = float(m_slack.group(2))

        source = ""
        destination = ""
        path_group = ""
        path_type = ""
        requirement_ns = 0.0
        data_delay_ns = 0.0

        # 从 Slack 行之后逐行扫描，直到遇到下一个 Slack 行或文件末尾
        j = i + 1
        while j < len(lines):
            line = lines[j]

            # 遇到下一个 Slack 块，停止
            if _SLACK_RE.search(line):
                break

            m_src = _SOURCE_RE.match(line)
            if m_src:
                source = m_src.group(1).strip()
                # 检查续行
                j += 1
                while j < len(lines) and _is_continuation(lines[j]):
                    j += 1
                continue

            m_dst = _DEST_RE.match(line)
            if m_dst:
                destination = m_dst.group(1).strip()
                # 检查续行
                j += 1
                while j < len(lines) and _is_continuation(lines[j]):
                    j += 1
                continue

            m_pg = _PATH_GROUP_RE.match(line)
            if m_pg:
                path_group = m_pg.group(1).strip()
                j += 1
                continue

            m_pt = _PATH_TYPE_RE.match(line)
            if m_pt:
                path_type = m_pt.group(1).strip()
                j += 1
                continue

            m_req = _REQUIREMENT_RE.match(line)
            if m_req:
                requirement_ns = float(m_req.group(1))
                j += 1
                continue

            m_dd = _DATA_DELAY_RE.match(line)
            if m_dd:
                data_delay_ns = float(m_dd.group(1))
                j += 1
                continue

            j += 1

        paths.append(
            TimingPath(
                slack_ns=slack_ns,
                met=met,
                source=source,
                destination=destination,
                path_group=path_group,
                path_type=path_type,
                requirement_ns=requirement_ns,
                data_delay_ns=data_delay_ns,
            )
        )

        # 移动到下一个 Slack 块的起始位置
        i = j

    return paths


def _is_continuation(line: str) -> bool:
    """判断是否为 Source/Destination 的续行。

    续行特征：大量前导空格后紧跟 ``(``，例如
    ``                            (rising edge-triggered ...)``
    """
    stripped = line.lstrip()
    return stripped.startswith("(") and len(line) - len(stripped) >= 10


# ====================================================================== #
#  公共 API
# ====================================================================== #


def parse_design_stage(raw: str) -> tuple[str, str, str]:
    """解析 QUERY_DESIGN_STAGE 脚本输出。

    返回 ``(stage, synth_status, impl_status)`` 三元组。
    若未匹配,返回 ``("unknown", "", "")``。
    """
    m = _STAGE_RE.search(raw)
    if m is None:
        return ("unknown", "", "")
    return (m.group(1).strip(), m.group(2).strip(), m.group(3).strip())


def derive_stage_warning(stage: str, synth_status: str, impl_status: str) -> tuple[str, str]:
    """根据设计阶段和 run 状态,生成用户友好的 (source_detail, stage_warning) 文案。

    规则:
    - post-route:最终数据,不加警告
    - post-place:布线前,提示数据可能不准
    - post-synth:估算,强警告不要当最终结果
    - impl_1 是 ERROR 状态:特别说明失败原因,强警告
    """
    has_impl_error = "ERROR" in impl_status.upper()

    if has_impl_error and stage in ("post-synth", "post-place"):
        detail = f"impl_1 状态={impl_status},未完成布线;当前显示的是前置阶段的估算时序"
        warning = (
            "注意: impl_1 失败,下面的时序是综合/布局后的估算,不等同于布线后的最终结果。"
            "不要据此判断能否烧板,先修复 impl_1 错误。"
        )
        return (detail, warning)

    if stage == "post-route":
        return (f"impl_1 状态={impl_status}", "")

    if stage == "post-place":
        return (
            f"impl_1 状态={impl_status}",
            "注意: 尚未布线,时序数据是布局后估算,可能与 post-route 结果有差异。",
        )

    if stage == "post-synth":
        return (
            f"synth_1 状态={synth_status},impl_1 状态={impl_status or 'Not started'}",
            "注意: 仅有综合估算时序,未经布局布线。不要作为最终判据。",
        )

    return ("", "")


def parse_timing_summary(raw_text: str) -> TimingReport:
    """解析 ``report_timing_summary -return_string`` 的完整输出。

    参数:
        raw_text: Vivado 返回的原始多行文本。

    返回:
        TimingReport —— 包含 summary（TimingSummary）和 paths（TimingPath 列表）。

    空输入或无法识别的格式不会抛异常，而是返回全零的默认报告。
    """
    summary = _parse_summary_table(raw_text)
    paths = _parse_paths(raw_text)
    return TimingReport(summary=summary, paths=paths)


def format_timing_report(report: TimingReport) -> str:
    """将 TimingReport 格式化为人类可读的摘要文本。

    重点高亮违例信息，方便 LLM 和用户快速判断时序状态。
    在报告开头明示数据来源阶段(post-synth/post-route),避免用户把估算时序
    误判为最终时序。
    """
    s = report.summary
    lines: list[str] = []

    # 总体状态
    status = "PASS (时序满足)" if s.timing_met else "FAIL (时序违例)"
    lines.append(f"=== 时序分析摘要 === 状态: {status}")

    # Bug 2 修复:数据来源元信息(在 PASS/FAIL 旁边,用户不会漏看)
    if report.source_stage and report.source_stage != "unknown":
        stage_label = {
            "post-synth": "post-synth (综合后估算,非最终结果)",
            "post-place": "post-place (布局后估算,尚未布线)",
            "post-route": "post-route (布线后最终结果)",
        }.get(report.source_stage, report.source_stage)
        lines.append(f"数据来源: {stage_label}")
        if report.source_detail:
            lines.append(f"  详情: {report.source_detail}")
    if report.stage_warning:
        lines.append(f"[!] {report.stage_warning}")
    lines.append("")

    # Setup 指标
    lines.append(f"  Setup  WNS = {s.wns:+.3f} ns   TNS = {s.tns:.3f} ns")
    lines.append(f"         失败端点: {s.failing_endpoints} / {s.total_endpoints}")

    # Hold 指标
    lines.append(f"  Hold   WHS = {s.whs:+.3f} ns   THS = {s.ths:.3f} ns")
    lines.append("")

    # 违例警告
    if s.wns < 0:
        lines.append(f"  [!] Setup 违例: WNS = {s.wns:.3f} ns，需优化关键路径。")
    if s.whs < 0:
        lines.append(f"  [!] Hold 违例: WHS = {s.whs:.3f} ns，需检查时钟偏斜。")

    # 路径详情
    if report.paths:
        lines.append("")
        lines.append(f"--- 关键路径 ({len(report.paths)} 条) ---")
        for idx, p in enumerate(report.paths, 1):
            flag = "MET" if p.met else "VIOLATED"
            lines.append(f"  [{idx}] Slack {p.slack_ns:+.3f} ns ({flag})")
            lines.append(f"      {p.source} -> {p.destination}")
            lines.append(f"      时钟域: {p.path_group}  类型: {p.path_type}")
            lines.append(
                f"      Requirement: {p.requirement_ns:.3f} ns"
                f"  Data Delay: {p.data_delay_ns:.3f} ns"
            )

    # 违例路径详情(仅在 timing_met=False 且查询成功时填充)
    if report.violating_paths:
        lines.append("")
        lines.append(f"--- 违例路径 Top {len(report.violating_paths)} ---")
        for idx, vp in enumerate(report.violating_paths, 1):
            tag, advice = analyze_path_pattern(vp)
            cdc_hint = ""
            if vp.start_clock and vp.end_clock and vp.start_clock != vp.end_clock:
                cdc_hint = f"  [CDC: {vp.start_clock}→{vp.end_clock}]"
            lines.append(
                f"  [{idx}] {vp.type.upper()} slack {vp.slack:+.3f} ns"
                f"  [{tag}]{cdc_hint}"
            )
            lines.append(f"      起点: {vp.startpoint}")
            lines.append(f"      终点: {vp.endpoint}")
            lines.append(
                f"      延迟分解: logic {vp.logic_delay:.3f} ns  "
                f"route {vp.route_delay:.3f} ns  "
                f"skew {vp.clock_skew:+.3f} ns  levels={vp.levels}"
            )
            lines.append(f"      建议: {advice}")
    elif report.violating_paths_error:
        lines.append("")
        lines.append(f"(违例路径查询失败: {report.violating_paths_error})")

    return "\n".join(lines)


# ====================================================================== #
#  违例路径解析(REPORT_VIOLATING_PATHS Tcl 脚本 → ViolatingPath 列表)
# ====================================================================== #


def _looks_like_port(name: str) -> bool:
    """判断 startpoint/endpoint 是否是顶层端口(而非寄存器引脚)。

    经验规则:
    - 寄存器引脚末尾常见为 /C /Q /D /CE /R 等单字母
    - 顶层端口不带 / 或末尾是 [N] 形式的 bit slice 或就是裸名
    - FDRE/FDCE/FDSE 等 cell 名作为内部节点,带 / 分隔符

    返回 True 表示更像端口(建议 IOB 寄存器)。
    """
    if not name:
        return False
    if "/" not in name:
        return True
    # 形如 "din[3]" / "clk" 的端口 — 无 / 分隔
    # 形如 "reg_a/Q" / "mem/douta[0]" 的内部节点 — 有 /
    return False


def _parse_one_path_block(block_lines: list[str], path_type: str) -> ViolatingPath | None:
    """解析单条路径块(从 Slack 行开始到下一个 Slack 行之前)。

    Args:
        block_lines: 已切分好的路径块行
        path_type: "setup" / "hold"

    返回 ViolatingPath,解析失败返回 None。
    """
    slack: float | None = None
    startpoint = ""
    endpoint = ""
    start_clock = ""
    end_clock = ""
    logic_delay = 0.0
    route_delay = 0.0
    clock_skew = 0.0
    levels = 0

    i = 0
    while i < len(block_lines):
        line = block_lines[i]

        m_slack = _SLACK_RE.search(line)
        if m_slack and slack is None:
            slack = float(m_slack.group(2))
            i += 1
            continue

        m_src = _SOURCE_RE.match(line)
        if m_src and not startpoint:
            startpoint = m_src.group(1).strip()
            # 续行里找时钟名
            j = i + 1
            while j < len(block_lines) and _is_continuation(block_lines[j]):
                m_clk = _CLOCK_BY_RE.search(block_lines[j])
                if m_clk and not start_clock:
                    start_clock = m_clk.group(1)
                j += 1
            i = j
            continue

        m_dst = _DEST_RE.match(line)
        if m_dst and not endpoint:
            endpoint = m_dst.group(1).strip()
            j = i + 1
            while j < len(block_lines) and _is_continuation(block_lines[j]):
                m_clk = _CLOCK_BY_RE.search(block_lines[j])
                if m_clk and not end_clock:
                    end_clock = m_clk.group(1)
                j += 1
            i = j
            continue

        m_dd = _DATA_DELAY_SPLIT_RE.search(line)
        if m_dd:
            logic_delay = float(m_dd.group(1))
            route_delay = float(m_dd.group(2))

        m_ll = _LOGIC_LEVELS_RE.search(line)
        if m_ll:
            levels = int(m_ll.group(1))

        m_skew = _CLOCK_SKEW_RE.search(line)
        if m_skew:
            clock_skew = float(m_skew.group(1))

        i += 1

    if slack is None or not startpoint or not endpoint:
        return None

    return ViolatingPath(
        slack=slack,
        startpoint=startpoint,
        endpoint=endpoint,
        start_clock=start_clock,
        end_clock=end_clock,
        logic_delay=logic_delay,
        route_delay=route_delay,
        clock_skew=clock_skew,
        levels=levels,
        type=path_type,
    )


def parse_violating_paths(output: str) -> list[ViolatingPath]:
    """解析 REPORT_VIOLATING_PATHS Tcl 脚本的输出,返回违例路径列表。

    仅保留 slack < 0 的路径(违例)。slack >= 0 的路径会被跳过,
    因为即使 max_paths=10 也可能包含最差但仍满足的路径,和"违例"概念不符。

    Args:
        output: Tcl 脚本原始 stdout,含 VMCP_PATH_START/END 块分隔标记。

    返回:
        ViolatingPath 列表,按 slack 从最差(最负)到最好排序。
    """
    lines = output.splitlines()
    paths: list[ViolatingPath] = []

    i = 0
    current_type: str | None = None
    block_start: int | None = None

    while i < len(lines):
        line = lines[i]

        m_start = _BLOCK_START_RE.search(line)
        if m_start:
            current_type = m_start.group(1)
            block_start = i + 1
            i += 1
            continue

        m_end = _BLOCK_END_RE.search(line)
        if m_end and block_start is not None and current_type is not None:
            block_lines = lines[block_start:i]
            # 块内可能有多个 Slack 段,按 Slack 行切分
            slack_indices = [
                k for k, ln in enumerate(block_lines) if _SLACK_RE.search(ln)
            ]
            for idx, start in enumerate(slack_indices):
                end = slack_indices[idx + 1] if idx + 1 < len(slack_indices) else len(block_lines)
                sub_block = block_lines[start:end]
                vp = _parse_one_path_block(sub_block, current_type)
                # 只收违例(slack < 0);健康路径意义不大
                if vp is not None and vp.slack < 0:
                    paths.append(vp)
            current_type = None
            block_start = None
            i += 1
            continue

        i += 1

    # 按 slack 升序(最差违例排前面)
    paths.sort(key=lambda p: p.slack)
    return paths


def analyze_path_pattern(path: ViolatingPath) -> tuple[str, str]:
    """嗅探违例路径的模式,返回 (pattern_tag, 中文修复建议)。

    模式(按优先级从高到低):
    - CDC          跨时钟域 → 加同步器或 false_path
    - IO_UNREGISTERED  起/止点是顶层端口 → 加 IOB 寄存器
    - HIGH_FANOUT  route 延迟 >= 3x logic 延迟 → max_fanout/物理约束
    - LONG_COMBO   levels > 15 或 logic >= 2x route → 切流水线寄存器
    - UNKNOWN      兜底

    规则按优先级排序:CDC 最优先(正确性问题),然后 IO(物理约束),
    再判断 fanout vs combo(走线 vs 组合)。
    """
    # 1. CDC - 跨时钟域
    if (
        path.start_clock and path.end_clock
        and path.start_clock != path.end_clock
    ):
        tag = "CDC"
        advice = (
            f"跨时钟域路径({path.start_clock}→{path.end_clock})。"
            "建议在 {} 前加 2 级同步器(FF/FF),或对该路径设 "
            "`set_false_path -from [get_clocks {}] -to [get_clocks {}]`(若逻辑上允许异步)。".format(
                path.endpoint, path.start_clock, path.end_clock
            )
        )
        return (tag, advice)

    # 2. IO_UNREGISTERED - 起/止点是顶层端口
    src_is_port = _looks_like_port(path.startpoint)
    dst_is_port = _looks_like_port(path.endpoint)
    if src_is_port or dst_is_port:
        port = path.startpoint if src_is_port else path.endpoint
        side = "输入" if src_is_port else "输出"
        tag = "IO_UNREGISTERED"
        advice = (
            f"{side}端口 {port} 未经 IOB 寄存器,IO 延迟直接算进数据路径。"
            f"建议在 {port} 后立刻插入一级寄存器,"
            "并设 `set_property IOB TRUE [get_cells <那个寄存器>]` 把该寄存器压到 IOB 里。"
        )
        return (tag, advice)

    # 3. HIGH_FANOUT - route 延迟远大于 logic 延迟
    if path.logic_delay > 0 and path.route_delay >= 3 * path.logic_delay:
        tag = "HIGH_FANOUT"
        advice = (
            f"走线延迟({path.route_delay:.3f} ns)远大于组合延迟({path.logic_delay:.3f} ns),"
            "通常是高扇出或跨芯片走线。建议先 `report_high_fanout_nets -fanout_greater_than 1000` "
            "定位,然后对驱动端加 `set_property MAX_FANOUT 50 [get_cells <...>]`,"
            "或在综合阶段加 `-fanout_limit 50`,让工具自动复制寄存器。"
        )
        return (tag, advice)

    # 4. LONG_COMBO - 逻辑层级过深或组合延迟占主导
    if path.levels > 15 or (path.route_delay > 0 and path.logic_delay >= 2 * path.route_delay):
        tag = "LONG_COMBO"
        advice = (
            f"逻辑层级 {path.levels} 级,组合路径过长(logic={path.logic_delay:.3f} ns "
            f"vs route={path.route_delay:.3f} ns)。"
            f"建议在 {path.startpoint} 到 {path.endpoint} 中间切一级流水线寄存器,"
            "或重构表达式减少 LUT 层数(如用加法树替换串行累加)。"
        )
        return (tag, advice)

    # 5. 兜底
    tag = "UNKNOWN"
    advice = (
        f"未匹配到常见模式。可先用 `report_timing -from {{{path.startpoint}}} "
        f"-to {{{path.endpoint}}} -max_paths 1` 看完整路径,"
        "再决定是加寄存器、约束时钟还是调综合策略。"
    )
    return (tag, advice)
