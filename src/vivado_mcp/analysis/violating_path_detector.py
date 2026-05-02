"""违例路径模式嗅探器 —— 从 ``REPORT_VIOLATING_PATHS`` Tcl 脚本输出
解析单条违例路径,并按 5 种模式给出中文修复建议。

该模块从 ``timing_parser.py`` 拆出,只关心"违例路径深度分析"这一职责:
``TimingReport`` 表格分析仍在 ``timing_parser.py``,两者通过
``timing_parser.py`` re-export 保持向后兼容。

模式优先级(高→低):
    1. CDC          跨时钟域 → 加同步器或 false_path
    2. IO_UNREGISTERED  起/止点是顶层端口 → 加 IOB 寄存器
    3. HIGH_FANOUT  route 延迟 >= 3x logic 延迟 → max_fanout/物理约束
    4. LONG_COMBO   levels > 15 或 logic >= 2x route → 切流水线寄存器
    5. UNKNOWN      兜底
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 违例路径块用到的正则(与 timing_parser 中重名的几条故意各自维护一份,
# 让两个解析器互不依赖,后续单独演化时不会互相牵动)
_SLACK_RE = re.compile(r"Slack\s+\((MET|VIOLATED)\)\s*:\s*([-\d.]+)\s*ns")
_SOURCE_RE = re.compile(r"^\s+Source:\s+(.+)")
_DEST_RE = re.compile(r"^\s+Destination:\s+(.+)")

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


def _is_continuation(line: str) -> bool:
    """判断是否为 Source/Destination 的续行(与 timing_parser 内部保持一致语义)。"""
    stripped = line.lstrip()
    return stripped.startswith("(") and len(line) - len(stripped) >= 10


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
