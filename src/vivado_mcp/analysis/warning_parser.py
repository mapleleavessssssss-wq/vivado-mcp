"""Vivado CRITICAL WARNING 解析器。

纯 Python 模块，不依赖 Vivado 进程。解析 Tcl 脚本产生的 VMCP_ 前缀结构化输出，
将 CRITICAL WARNING 分类、聚合，并生成中文诊断报告。

输入来源（参见 tcl_scripts.py）:
- COUNT_WARNINGS  → ``VMCP_DIAG:errors=E,critical_warnings=CW,warnings=W``
- EXTRACT_CRITICAL_WARNINGS → ``VMCP_CW:行号|原始消息``
- EXTRACT_ERRORS → ``VMCP_RUNLOG_ERR:行号|原始消息``
- CHECK_PRE_BITSTREAM → ``VMCP_PRE_BIT:status=S,critical_warnings=N``
- TAIL_RUNME_LOG → ``VMCP_TAIL:status=...`` + ``VMCP_TAIL_LINE:行号|文本``
- TAIL_SIM_LOGS → ``VMCP_SIM:sim_dir=...`` + ``VMCP_SIM_LOG_{START,LINE,END}:...``
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ====================================================================== #
#  数据结构
# ====================================================================== #


@dataclass(frozen=True)
class CriticalWarning:
    """单条 CRITICAL WARNING 的结构化表示。"""

    warning_id: str  # 例如 "Vivado 12-1411"
    message: str  # 完整消息文本
    line_number: int  # runme.log 中的行号
    source_file: str  # 从消息末尾 [file.xdc:line] 提取的文件名
    port: str  # 从消息提取的端口名（如有）
    pin: str  # 从消息提取的引脚名（如有）


@dataclass
class SimLogTail:
    """单个 xsim 子日志的 tail 结果(用于 launch_simulation 失败诊断)。"""

    log_path: str       # 完整路径,如 ".../sim_1/behav/xsim/xvlog.log"
    body: str           # tail 出的行拼接(无前缀 / 无行号,适合喂给 scan_nonstandard_errors)
    start_line: int     # tail 第一行在原文件中的行号偏移(0-based,scan 会 +1)


@dataclass
class BatStepResult:
    """单步 .bat/.sh 子进程结果(launch_simulation -scripts_only fallback)。

    Vivado launch_simulation wrapper 在 spawn .bat 之前炸时,MCP 进程直接接管这
    几个脚本(compile.bat / elaborate.bat),把它们 spawn 出来抓 stderr,
    暴露被 Vivado 吞掉的真错。simulate 步骤会启动 xsim/GUI,默认跳过(标记为
    skipped),只把脚本路径回带,让用户决定是否手动跑。
    """

    step: str           # "compile" / "elaborate" / "simulate"
    bat_path: str       # 完整路径
    returncode: int     # 子进程退出码;-1=skipped, -2=timeout, -3=spawn 异常
    stdout_tail: str    # tail N 行的 stdout
    stderr_tail: str    # tail N 行的 stderr


@dataclass(frozen=True)
class NonStandardError:
    """日志中不带 ERROR:/CRITICAL WARNING: 前缀的"明显错误"。

    用途:补 Vivado messageDb 看不见的内部异常 / 子进程错误(如中文路径触发的
    TclStackFree、xvlog 不在 PATH 的 cmd 报错、segfault 等)。get_critical_warnings
    在 errors=0 且 cw=0 但 STATUS=ERROR 时,tail runme.log 扫这类条目,
    避免 AI 看到"未发现 ERROR"的误导反馈。
    """

    keyword: str       # 命中的关键词标签,如 "TclStackFree"
    line_number: int   # 在原文件中的行号(1-indexed)
    text: str          # 完整行文本
    severity: str      # "high" / "medium"


@dataclass
class WarningGroup:
    """按 warning_id 聚合后的分组。"""

    warning_id: str
    category: str  # "GT_PIN_CONFLICT" 等分类标签
    count: int
    first_line: int  # 该分组在 runme.log 中首次出现的行号
    message_template: str  # 代表性消息
    affected_ports: list[str] = field(default_factory=list)
    source_files: list[str] = field(default_factory=list)
    suggestion: str = ""  # 中文修复建议


@dataclass
class WarningReport:
    """整合诊断计数与分组后的完整报告。

    - ``groups``      —— CRITICAL WARNING 按 warning_id 分组
    - ``error_groups`` —— ERROR 按 warning_id 分组（严重级别 > CW）
    """

    errors: int
    critical_warnings: int
    warnings: int
    groups: list[WarningGroup] = field(default_factory=list)
    error_groups: list[WarningGroup] = field(default_factory=list)


# ====================================================================== #
#  已知分类表——内置中文修复建议
# ====================================================================== #

_KNOWN_CATEGORIES: dict[str, tuple[str, str]] = {
    "Vivado 12-1411": (
        "GT_PIN_CONFLICT",
        "GT端口PACKAGE_PIN约束与IP内部LOC冲突。\n"
        "  原因: IP 内部 GT Location 约束已锁定通道映射，XDC 中的 PACKAGE_PIN 与其冲突。\n"
        "  修复方法:\n"
        "  1. [推荐] 删除 XDC 中 GT 端口的 PACKAGE_PIN 约束，让 IP 自动分配\n"
        "  2. 修正 XDC 引脚顺序使其与 IP 内部 Lane 映射一致\n"
        "  注意: disable_gt_loc 仅对 UltraScale+(GT Wizard)有效，"
        "7-Series(pcie_7x)的 LOC 由 .ttcl 模板无条件生成，该参数无效。\n"
        "  诊断: 运行 inspect_ip_params(filter_keyword='gt') 确认 IP 支持哪些 GT 参数",
    ),
    "Vivado 12-2285": (
        "GT_LOC_CONFLICT",
        "Cell级LOC与已占用BEL冲突。检查是否有重复GT LOC约束。",
    ),
    "Vivado 12-4739": (
        "TIMING_CONSTRAINT",
        "时钟约束问题。检查create_clock定义。",
    ),
    "DRC RTSTAT-1": (
        "UNROUTED_NET",
        "存在未布线网络。检查管脚约束是否完整。",
    ),
    "Timing 38-282": (
        "TIMING_VIOLATION",
        "时序违例。运行 report_timing_summary 查看详情。",
    ),
    "Vivado 12-180": (
        "CLOCK_NOT_FOUND",
        "时钟约束目标不存在。检查create_clock的端口名是否与RTL一致。",
    ),
    "Synth 8-3295": (
        "UNCONNECTED_PORT",
        "模块端口未连接。检查RTL实例化的端口连接。",
    ),
    "DRC BIVC-1": (
        "IO_STANDARD_MISMATCH",
        "Bank 内 IOSTANDARD 不一致(同一 Bank 的端口用了不同电压,如 LVCMOS18 和 LVCMOS33)。\n"
        "  常见原因: 某端口漏写 IOSTANDARD,Vivado 默认 LVCMOS18 与同 Bank 其他端口冲突。\n"
        "  修复: 在 XDC 给所有端口显式指定 IOSTANDARD,同 Bank 保持电平一致。",
    ),
    "Vivado_Tcl 4-23": (
        "DRC_FAILED",
        "DRC(设计规则检查)失败,布局/布线阶段被阻止。\n"
        "  修复: 查看同一日志里前面的 [DRC xxx-N] 条目定位根因,常见是 BIVC-1/NSTD-1/UCIO-1。",
    ),
    "Common 17-39": (
        "STAGE_ABORT",
        "前置阶段失败导致后续阶段未能启动(例如 place_design 失败后 route_design 被中止)。\n"
        "  修复: 查看日志里更早的 ERROR 条目定位真正原因。",
    ),
    "Synth 8-27": (
        "SYNTH_SYNTAX_ERROR",
        "综合时发现 HDL 语法错误。修复: 查看错误前后的文件名和行号。",
    ),
    "Synth 8-439": (
        "PORT_MISSING",
        "实例化模块端口缺失或不匹配。检查模块声明和实例化端口列表是否一致。",
    ),
    "Place 30-58": (
        "PLACE_FAILED",
        "布局失败。常见原因: 资源超限、引脚冲突、时钟网络不合法。\n"
        "  修复: 先跑 report_utilization 看资源,再看是否有更早的 DRC ERROR。",
    ),
    "Route 35-162": (
        "ROUTE_FAILED",
        "布线失败(可能是拥塞或资源冲突)。\n"
        "  修复: 报 report_route_status,考虑降低时钟频率或增加布线优先级。",
    ),
    "Vivado 12-1790": (
        "MISSING_PIN_CONSTRAINT",
        "端口缺少 PACKAGE_PIN 或 LOC 约束。检查 XDC 文件是否遗漏了该端口的引脚分配。",
    ),
    "Vivado 12-4385": (
        "CLOCK_PLACEMENT",
        "时钟输入未放置在专用时钟引脚（MRCC/SRCC）。\n"
        "  修复: 将时钟信号分配到 MRCC 或 SRCC 类型的引脚。",
    ),
    "DRC NSTD-1": (
        "UNSPECIFIED_IOSTANDARD",
        "端口未指定 IOSTANDARD，将使用默认值。\n"
        "  修复: 在 XDC 中为所有端口添加 set_property IOSTANDARD 约束。",
    ),
    "DRC UCIO-1": (
        "UNCONSTRAINED_IO",
        "端口未约束到物理引脚。\n"
        "  修复: 在 XDC 中为该端口添加 PACKAGE_PIN 约束，或确认是否为未使用端口。",
    ),
}

# ====================================================================== #
#  编译正则——模块加载时一次编译
# ====================================================================== #

# 匹配 VMCP_DIAG 行
_RE_DIAG = re.compile(
    r"VMCP_DIAG:errors=(-?\d+),critical_warnings=(-?\d+),warnings=(-?\d+)"
)

# 匹配 VMCP_CW 行：行号|消息
_RE_CW_LINE = re.compile(r"VMCP_CW:(\d+)\|(.+)")

# 匹配 VMCP_RUNLOG_ERR 行：行号|消息(ERROR 详情提取,严重级别 > CW)
# 不用 VMCP_ERR: 前缀:会被 SubprocessSession 的 sentinel 协议层吞掉
# (session.py 对 VMCP_ERR: 前缀做前缀剥离)。0.3.10 field test 发现。
_RE_ERR_LINE = re.compile(r"VMCP_RUNLOG_ERR:(\d+)\|(.+)")

# 从消息中提取 warning ID，支持:
#   - 纯数字 ID:    [Vivado 12-1411] / [Timing 38-282]
#   - 字母数字 ID:  [DRC BIVC-1] / [DRC NSTD-1] / [DRC UCIO-1]
#   - 下划线 ID:    [Vivado_Tcl 4-23] / [Common 17-39]
_RE_WARNING_ID = re.compile(r"\[(\w+[\s\-][\w\-]+)\]")

# 从消息末尾提取源文件引用，如 [board_pins.xdc:15]
_RE_SOURCE_FILE = re.compile(r"\[(\S+?\.\w+):(\d+)\]")

# 提取端口名：port xxxx
_RE_PORT = re.compile(r"port\s+(\S+)", re.IGNORECASE)

# 提取引脚名：package_pin XXXX
_RE_PIN = re.compile(r"package_pin\s+(\w+)", re.IGNORECASE)

# 非标错误关键词模式表(用于 scan_nonstandard_errors)
# 严重度 high = 几乎肯定是失败原因;medium = 需上下文判断
# 命中时 keyword 字段取这里第三列的稳定标签,而非原始正则,避免大小写漂移
_NONSTANDARD_PATTERNS: tuple[tuple[re.Pattern, str, str], ...] = (
    (re.compile(r"TclStackFree", re.IGNORECASE), "TclStackFree", "high"),
    (re.compile(r"incorrect\s+freePtr", re.IGNORECASE), "incorrect freePtr", "high"),
    (re.compile(r"\bsegmentation\s*fault\b|\bsegfault\b", re.IGNORECASE), "segfault", "high"),
    (re.compile(r"access\s+violation", re.IGNORECASE), "access violation", "high"),
    (re.compile(r"\bFATAL\b"), "FATAL", "high"),
    (re.compile(r"\babort(ed)?\b\(?\)?", re.IGNORECASE), "abort", "high"),
    (re.compile(r"out\s+of\s+memory|bad_alloc", re.IGNORECASE), "OOM", "high"),
    (re.compile(r"is not recognized as an internal or external command", re.IGNORECASE),
     "not_recognized_cmd", "high"),
    (re.compile(r"不是内部或外部命令"), "not_recognized_cmd_zh", "high"),
    (re.compile(r"command not found", re.IGNORECASE), "cmd_not_found", "high"),
    (re.compile(r"permission denied", re.IGNORECASE), "permission_denied", "medium"),
)

# 匹配 VMCP_PRE_BIT 状态行
_RE_PRE_BIT = re.compile(r"VMCP_PRE_BIT:status=([^,]+),critical_warnings=(-?\d+)")

# 匹配 VMCP_PRE_BIT_CW 样本行
_RE_PRE_BIT_CW = re.compile(r"VMCP_PRE_BIT_CW:(.+)")


# ====================================================================== #
#  解析函数
# ====================================================================== #


def parse_diag_counts(raw: str) -> tuple[int, int, int]:
    """解析 COUNT_WARNINGS 脚本输出中的 VMCP_DIAG 行。

    返回 ``(errors, critical_warnings, warnings)`` 三元组。
    若未找到匹配行，返回 ``(-1, -1, -1)``。
    """
    m = _RE_DIAG.search(raw)
    if m is None:
        return (-1, -1, -1)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _parse_log_entries(raw: str, line_re: re.Pattern) -> list[CriticalWarning]:
    """通用解析器:匹配 ``<prefix>:行号|消息`` 格式,抽取 warning_id/port/pin/source_file。

    同时服务 CRITICAL WARNING(``VMCP_CW:``)和 ERROR(``VMCP_RUNLOG_ERR:``)两类日志条目,
    数据结构复用 ``CriticalWarning``(字段语义通用,仅严重级别不同)。
    """
    results: list[CriticalWarning] = []
    for line in raw.splitlines():
        m = line_re.match(line.strip())
        if m is None:
            continue

        line_number = int(m.group(1))
        message = m.group(2)

        # 提取 warning ID（取第一个匹配）
        wid_match = _RE_WARNING_ID.search(message)
        warning_id = wid_match.group(1) if wid_match else "UNKNOWN"

        # 提取源文件（取最后一个匹配，因为 warning ID 也用方括号）
        source_file = ""
        for sf_match in _RE_SOURCE_FILE.finditer(message):
            source_file = sf_match.group(1)

        # 提取端口名（取第一个匹配）
        port_match = _RE_PORT.search(message)
        port = port_match.group(1) if port_match else ""

        # 提取引脚名
        pin_match = _RE_PIN.search(message)
        pin = pin_match.group(1) if pin_match else ""

        results.append(
            CriticalWarning(
                warning_id=warning_id,
                message=message,
                line_number=line_number,
                source_file=source_file,
                port=port,
                pin=pin,
            )
        )
    return results


def parse_critical_warnings(raw: str) -> list[CriticalWarning]:
    """解析 EXTRACT_CRITICAL_WARNINGS 脚本输出的 ``VMCP_CW:`` 行。"""
    return _parse_log_entries(raw, _RE_CW_LINE)


def parse_errors(raw: str) -> list[CriticalWarning]:
    """解析 EXTRACT_ERRORS 脚本输出的 ``VMCP_RUNLOG_ERR:`` 行(结构与 CW 同构)。"""
    return _parse_log_entries(raw, _RE_ERR_LINE)


def group_warnings(warnings: list[CriticalWarning]) -> list[WarningGroup]:
    """按 warning_id 对 CriticalWarning 列表进行聚合分组。

    每组查询 ``_KNOWN_CATEGORIES`` 获取分类标签和中文建议。
    未知 ID 使用 ``"UNKNOWN"`` 分类和通用建议。
    """
    # 按 warning_id 分桶，保持首次出现顺序
    buckets: dict[str, list[CriticalWarning]] = {}
    for cw in warnings:
        buckets.setdefault(cw.warning_id, []).append(cw)

    groups: list[WarningGroup] = []
    for wid, cw_list in buckets.items():
        if wid in _KNOWN_CATEGORIES:
            category, suggestion = _KNOWN_CATEGORIES[wid]
        else:
            category = "UNKNOWN"
            suggestion = "未知警告类型，请查阅 Vivado 日志获取详细信息。"

        # 收集受影响的端口（去重，保持顺序）
        seen_ports: set[str] = set()
        affected_ports: list[str] = []
        for cw in cw_list:
            if cw.port and cw.port not in seen_ports:
                seen_ports.add(cw.port)
                affected_ports.append(cw.port)

        # 收集源文件（去重，保持顺序）
        seen_files: set[str] = set()
        source_files: list[str] = []
        for cw in cw_list:
            if cw.source_file and cw.source_file not in seen_files:
                seen_files.add(cw.source_file)
                source_files.append(cw.source_file)

        groups.append(
            WarningGroup(
                warning_id=wid,
                category=category,
                count=len(cw_list),
                first_line=cw_list[0].line_number,
                message_template=cw_list[0].message,
                affected_ports=affected_ports,
                source_files=source_files,
                suggestion=suggestion,
            )
        )
    return groups


def _format_group_block(g: WarningGroup, severity: str) -> list[str]:
    """格式化单个分组区块(ERROR 或 CRITICAL WARNING 通用)。"""
    block = [f"--- [{severity}][{g.warning_id}] {g.category} ({g.count} 条) ---"]
    block.append(f"  首次出现: 第 {g.first_line} 行")
    block.append(f"  示例消息: {g.message_template}")
    if g.affected_ports:
        port_str = ", ".join(g.affected_ports[:10])
        if len(g.affected_ports) > 10:
            port_str += f" ... 共 {len(g.affected_ports)} 个"
        block.append(f"  受影响端口: {port_str}")
    if g.source_files:
        block.append(f"  约束文件: {', '.join(g.source_files)}")
    block.append(f"  建议: {g.suggestion}")
    block.append("")
    return block


def format_warning_report(report: WarningReport) -> str:
    """将 WarningReport 格式化为人类可读的中文文本。

    级别顺序: ERROR(最严重) → CRITICAL WARNING → 概览。如果存在 ERROR,
    首行提示 ``!! 发现 N 条 ERROR !!``;否则若有 CW,首行 ``!! 发现 N 条 CRITICAL WARNING !!``。
    """
    lines: list[str] = []

    # 概览行(始终展示)
    lines.append(
        f"诊断概览: errors={report.errors}, "
        f"critical_warnings={report.critical_warnings}, "
        f"warnings={report.warnings}"
    )

    # ERROR 详情区块(严重级别最高,优先展示)
    if report.errors > 0 and report.error_groups:
        lines.append("")
        lines.append("=== ERROR 详情 ===")
        for g in report.error_groups:
            lines.extend(_format_group_block(g, "ERROR"))

    # CRITICAL WARNING 详情区块
    if report.critical_warnings > 0 and report.groups:
        lines.append("")
        lines.append("=== CRITICAL WARNING 详情 ===")
        for g in report.groups:
            lines.extend(_format_group_block(g, "CRITICAL WARNING"))

    # 顶部醒目提示:ERROR 优先于 CW
    if report.errors > 0:
        lines.insert(0, f"!! 发现 {report.errors} 条 ERROR !!")
    elif report.critical_warnings > 0:
        lines.insert(0, f"!! 发现 {report.critical_warnings} 条 CRITICAL WARNING !!")

    return "\n".join(lines)


def parse_tail_runme_output(raw: str) -> tuple[str, int, str]:
    """解析 TAIL_RUNME_LOG 输出。

    Returns:
        ``(status, start_line, body)``:
        - ``status``: run 的 STATUS 字符串(如 ``"synth_design ERROR"``);run 不存在
          或脚本失败时为空串
        - ``start_line``: tail 区间第一行对应原文件的行号偏移(0-based)。喂给
          ``scan_nonstandard_errors(body, start_line=start_line)`` 能还原原始行号
        - ``body``: tail 区间所有行的拼接(无 VMCP_ 前缀、无行号),
          可直接喂给 ``scan_nonstandard_errors``
    """
    status = ""
    start_line = 0
    body_lines: list[str] = []

    for line in raw.splitlines():
        if line.startswith("VMCP_TAIL:status="):
            status = line[len("VMCP_TAIL:status="):].strip()
        elif line.startswith("VMCP_TAIL_LINE:"):
            rest = line[len("VMCP_TAIL_LINE:"):]
            sep = rest.find("|")
            if sep < 0:
                continue
            try:
                ln = int(rest[:sep])
            except ValueError:
                continue
            text = rest[sep + 1:]
            # 只在第一条数据行时初始化 start_line(原文件 ln=1 时 offset=0,
            # 用 body_lines 是否为空判断,避免用 0 作"未设置"哨兵被合法 ln=1 覆盖)
            if not body_lines:
                start_line = ln - 1
            body_lines.append(text)

    return status, start_line, "\n".join(body_lines)


def parse_sim_logs_output(raw: str) -> tuple[str, list[SimLogTail]]:
    """解析 TAIL_SIM_LOGS 输出。

    Returns:
        ``(sim_dir, logs)``:
        - ``sim_dir``: simulation fileset 目录;fileset 不存在时为空串
        - ``logs``: 每个 xsim 子日志的 tail 结果(``SimLogTail`` 列表)
    """
    sim_dir = ""
    logs: list[SimLogTail] = []

    current_path = ""
    current_body: list[str] = []
    current_start = 0

    for line in raw.splitlines():
        if line.startswith("VMCP_SIM:sim_dir="):
            sim_dir = line[len("VMCP_SIM:sim_dir="):].strip()
        elif line.startswith("VMCP_SIM_LOG_START:"):
            current_path = line[len("VMCP_SIM_LOG_START:"):].strip()
            current_body = []
            current_start = 0
        elif line.startswith("VMCP_SIM_LOG_LINE:"):
            rest = line[len("VMCP_SIM_LOG_LINE:"):]
            sep = rest.find("|")
            if sep < 0:
                continue
            try:
                ln = int(rest[:sep])
            except ValueError:
                continue
            text = rest[sep + 1:]
            # 同 parse_tail_runme_output:用 body 是否为空判断首行
            if not current_body:
                current_start = ln - 1
            current_body.append(text)
        elif line.startswith("VMCP_SIM_LOG_END:") and current_path:
            logs.append(
                SimLogTail(
                    log_path=current_path,
                    body="\n".join(current_body),
                    start_line=current_start,
                )
            )
            current_path = ""
            current_body = []
            current_start = 0

    return sim_dir, logs


def parse_launch_scripts_output(
    raw: str,
) -> tuple[str, str, list[tuple[str, str, str]]]:
    """解析 LAUNCH_SCRIPTS_AND_GLOB 输出。

    Returns:
        ``(sim_dir, scripts_only_status, bat_files)``:
        - ``sim_dir``: simulation fileset 目录;fileset 不存在时为空串
        - ``scripts_only_status``: ``already_present`` / ``ok`` / ``triggering`` /
          ``failed:<原因>`` / ``unknown``;fileset 缺失时为 ``fileset_not_found``,
          仿真目录缺失时为 ``dir_missing``
        - ``bat_files``: ``[(step, ext, path), ...]``,step ∈ compile/elaborate/simulate
    """
    sim_dir = ""
    status = "unknown"
    files: list[tuple[str, str, str]] = []

    for line in raw.splitlines():
        if line.startswith("VMCP_BAT:sim_dir="):
            sim_dir = line[len("VMCP_BAT:sim_dir="):].strip()
        elif line.startswith("VMCP_BAT:scripts_only="):
            status = line[len("VMCP_BAT:scripts_only="):].strip()
        elif line.startswith("VMCP_BAT:scripts_only_failed="):
            reason = line[len("VMCP_BAT:scripts_only_failed="):].strip()
            status = f"failed:{reason}"
        elif line.startswith("VMCP_BAT:error="):
            status = line[len("VMCP_BAT:error="):].strip()
        elif line.startswith("VMCP_BAT:dir_missing="):
            status = "dir_missing"
        elif line.startswith("VMCP_BAT_FILE:"):
            rest = line[len("VMCP_BAT_FILE:"):]
            parts = rest.split("|", 2)
            if len(parts) == 3:
                step, ext, path = parts[0].strip(), parts[1].strip(), parts[2].strip()
                if step and ext and path:
                    files.append((step, ext, path))

    return sim_dir, status, files


def parse_bat_run_output(raw: str) -> tuple[int, str]:
    """解析 RUN_BAT_STEP 输出。

    Returns:
        ``(rc, output)``:
        - ``rc``:子进程退出码,-1 表示协议解析失败(没看到 VMCP_BAT_RUN:rc=)
        - ``output``:stdout + stderr 合并的完整文本(逐行从 VMCP_BAT_RUN_LINE 拼)
    """
    rc = -1
    lines: list[str] = []
    in_body = False

    for line in raw.splitlines():
        if line.startswith("VMCP_BAT_RUN:rc="):
            try:
                rc = int(line[len("VMCP_BAT_RUN:rc="):].strip())
            except ValueError:
                pass
        elif line == "VMCP_BAT_RUN_OUT_START":
            in_body = True
        elif line == "VMCP_BAT_RUN_OUT_END":
            in_body = False
        elif in_body and line.startswith("VMCP_BAT_RUN_LINE:"):
            lines.append(line[len("VMCP_BAT_RUN_LINE:"):])

    return rc, "\n".join(lines)


def format_bat_steps_section(
    results: list[BatStepResult],
    scripts_only_status: str,
    sim_dir: str,
) -> str:
    """拼接 -scripts_only fallback 诊断段。

    主线展示:每步 .bat 路径 + returncode + stderr 尾。任一步 returncode!=0
    时给"真错可能在这一步"的提示;全部为 0 时给"launch_simulation wrapper 失败
    但 .bat 集合本身可跑"的关键结论。
    """
    lines: list[str] = []
    lines.append("=== launch_simulation -scripts_only fallback ===")
    lines.append(f"仿真目录: {sim_dir}")
    lines.append(f"-scripts_only 触发状态: {scripts_only_status}")
    lines.append("")

    if not results:
        lines.append("未跑任何 .bat / .sh —— 可能 fileset 不对、目录缺失或脚本没生成。")
        return "\n".join(lines)

    all_ok = all(r.returncode == 0 for r in results if r.returncode != -1)
    failing = next((r for r in results if r.returncode > 0), None)

    for r in results:
        lines.append(f"--- {r.step} ({r.bat_path}) ---")
        if r.returncode == -1:
            lines.append(f"  跳过: {r.stderr_tail or 'skipped'}")
        elif r.returncode == -2:
            lines.append("  ✗ 超时(子进程未在限定时间内结束)")
        elif r.returncode == -3:
            lines.append(f"  ✗ spawn 失败: {r.stderr_tail}")
        else:
            lines.append(f"  returncode = {r.returncode}")
            if r.stderr_tail.strip():
                lines.append("  stderr 尾部:")
                for ln in r.stderr_tail.splitlines():
                    lines.append(f"    | {ln}")
            if r.stdout_tail.strip():
                lines.append("  stdout 尾部:")
                for ln in r.stdout_tail.splitlines()[-8:]:
                    lines.append(f"    | {ln}")
        lines.append("")

    if failing:
        lines.append(
            f"⚠ {failing.step} 步骤 returncode={failing.returncode},真错很可能就在这一步的 stderr。"
        )
    elif all_ok:
        lines.append(
            "✓ 复刻的 .bat 步骤全部正常退出。说明 launch_simulation 的 wrapper 失败"
            "(脚本生成之后、spawn 之前的某一步)而非 xvlog/xelab/xsim 本身。"
        )
        lines.append(
            "  绕过工作流: 用户自己 `launch_simulation -scripts_only` 后,在 shell"
            "(PowerShell / bash)里逐个跑 compile.bat → elaborate.bat → simulate.bat,"
            "或直接 `xsim.bat <snap> -gui` 拉仿真器。"
        )

    return "\n".join(lines)


def scan_nonstandard_errors(
    text: str, start_line: int = 0
) -> list[NonStandardError]:
    """扫描日志文本,捕获不带 ERROR:/CRITICAL WARNING: 前缀的"明显错误"行。

    典型场景:中文路径触发 ``TclStackFree``、xvlog 不在 PATH 导致 ``'xvlog'
    不是内部或外部命令``、Vivado 段错误等。Vivado messageDb 看不见这些条目,
    需要直接 tail 日志末尾按关键词扫描。

    Args:
        text: 待扫描的日志文本(通常是日志末尾 N 行)
        start_line: 原文件中的起始行号(0-based),用于把相对位置回算成原始行号。
            如果传 0,返回的 ``line_number`` 就是 text 内的 1-indexed 行号。

    Returns:
        命中条目列表,按原文出现顺序。一行只算一条(取首个命中的关键词)。
        带 ``ERROR:`` / ``CRITICAL WARNING:`` 前缀的行会被跳过 —— 那些走
        ``parse_errors`` / ``parse_critical_warnings``。
    """
    results: list[NonStandardError] = []
    for idx, line in enumerate(text.splitlines()):
        stripped = line.lstrip()
        # 跳过有明确标准前缀的行 —— 那些由 parse_errors/parse_critical_warnings 处理
        if stripped.startswith(("ERROR:", "CRITICAL WARNING:")):
            continue
        for pattern, label, severity in _NONSTANDARD_PATTERNS:
            if pattern.search(line):
                results.append(
                    NonStandardError(
                        keyword=label,
                        line_number=start_line + idx + 1,
                        text=line.rstrip(),
                        severity=severity,
                    )
                )
                break  # 一行只算一条
    return results


def format_nonstandard_section(errors: list[NonStandardError]) -> str:
    """格式化非标错误段(供 get_critical_warnings 追加到主报告)。

    输入为空时返回空串(调用方判断为空就不拼这一段)。
    """
    if not errors:
        return ""

    lines = ["=== 非标错误(日志末尾关键词扫描) ==="]
    lines.append(
        f"在日志末尾发现 {len(errors)} 条非标错误"
        "(不带 ERROR:/CRITICAL WARNING: 前缀,Vivado messageDb 看不见,"
        "但内容明显指向失败原因)。"
    )
    for e in errors:
        lines.append(f"  [{e.keyword}] 第 {e.line_number} 行: {e.text}")

    # 关键词聚合,给定向修复 hint
    high_kw = {e.keyword for e in errors if e.severity == "high"}
    hints: list[str] = []
    if {"TclStackFree", "incorrect freePtr"} & high_kw:
        hints.append(
            "  • TclStackFree / incorrect freePtr → 工程目录可能含中文或非 ASCII 字符,"
            "Vivado 2019.x 已知 bug。把工程搬到纯 ASCII 路径(如 C:/vivado_work/)。"
        )
    if {"not_recognized_cmd", "not_recognized_cmd_zh", "cmd_not_found"} & high_kw:
        hints.append(
            "  • 子进程命令未找到 → 一般是 xvlog/xelab 不在 PATH。"
            "尝试在 Tcl 里 puts $::env(PATH) 确认是否含 <vivado>/bin。"
        )
    if {"segfault", "access violation"} & high_kw:
        hints.append(
            "  • Vivado 段错误/访问违规 → 通常是 IP 配置异常或工程文件损坏,"
            "尝试 reset_project / 重新生成 IP / 干净 reopen。"
        )
    if {"OOM"} & high_kw:
        hints.append(
            "  • 内存不足 → 降低综合/实现 jobs 数,关闭其他大型程序后重试。"
        )
    if hints:
        lines.append("")
        lines.append("可能的修复方向:")
        lines.extend(hints)

    return "\n".join(lines)


def parse_pre_bitstream(raw: str) -> tuple[str, int, list[str]]:
    """解析 CHECK_PRE_BITSTREAM 脚本输出。

    返回 ``(status, cw_count, sample_lines)``：
    - status: 实现状态字符串（如 "route_design Complete"）
    - cw_count: CRITICAL WARNING 计数
    - sample_lines: 前 N 条 CRITICAL WARNING 样本文本

    若未匹配到状态行，返回 ``("UNKNOWN", -1, [])``。
    """
    status = "UNKNOWN"
    cw_count = -1
    sample_lines: list[str] = []

    m = _RE_PRE_BIT.search(raw)
    if m is not None:
        status = m.group(1)
        cw_count = int(m.group(2))

    for line in raw.splitlines():
        cm = _RE_PRE_BIT_CW.match(line.strip())
        if cm is not None:
            sample_lines.append(cm.group(1))

    return (status, cw_count, sample_lines)
