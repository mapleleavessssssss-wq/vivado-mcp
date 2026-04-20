"""报告工具：get_io_report / get_timing_report。

两个工具都把 Vivado 原始表格文本解析为结构化数据（JSON / 格式化文本），
便于 LLM 精确提取数值。通用的报告命令（utilization / power / drc 等）
请直接用 ``run_tcl("report_xxx -return_string")``，无需包装。
"""

import json
import logging

from mcp.server.fastmcp import Context

from vivado_mcp.analysis.io_parser import parse_report_io
from vivado_mcp.analysis.ip_status_parser import format_ip_status_report, parse_ip_status
from vivado_mcp.analysis.project_parser import format_project_info, parse_project_info
from vivado_mcp.analysis.run_progress_parser import format_run_progress, parse_run_progress
from vivado_mcp.analysis.suggestion_engine import format_suggestion, suggest_next
from vivado_mcp.analysis.timing_parser import (
    derive_stage_warning,
    format_timing_report,
    parse_design_stage,
    parse_timing_summary,
)
from vivado_mcp.analysis.util_parser import format_utilization_report, parse_utilization
from vivado_mcp.analysis.warning_parser import parse_diag_counts, parse_pre_bitstream
from vivado_mcp.server import _NO_SESSION, _require_session, mcp
from vivado_mcp.tcl_scripts import (
    CHECK_PRE_BITSTREAM,
    COUNT_WARNINGS,
    QUERY_DESIGN_STAGE,
    QUERY_PROJECT_INFO,
    QUERY_RUN_PROGRESS,
)
from vivado_mcp.vivado.tcl_utils import validate_identifier

logger = logging.getLogger(__name__)


@mcp.tool()
async def get_io_report(
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """获取结构化 IO 引脚报告（JSON）。

    执行 report_io 并解析为结构化数据，包含：
    - 每个端口的引脚、站点、方向、IO 标准、Bank
    - GT / GPIO 类型自动判定
    - 汇总统计（总数、GT 数、GPIO 数、未分配数）

    Args:
        session_id: 目标会话 ID。
    """
    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    # 直接获取完整输出（不经 _safe_execute，避免 .summary 截断）
    try:
        result = await session.execute(
            "report_io -return_string", timeout=60.0
        )
        # B2 修复：命令失败时不要把错误文本送进 parser（否则解析出 0 个端口等假结果）
        if result.is_error:
            return (
                f"[ERROR] 获取 IO 报告失败（rc={result.return_code}）：\n"
                f"{result.output}\n\n"
                "提示: report_io 需要打开综合或实现后的设计。"
                "请先运行 run_synthesis 或 run_implementation。"
            )
        io_report = parse_report_io(result.output)
        return json.dumps(io_report.to_dict(), ensure_ascii=False, indent=2)
    except Exception as e:
        return f"[ERROR] 获取 IO 报告失败: {e}"


@mcp.tool()
async def get_timing_report(
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """获取结构化时序报告。

    执行 report_timing_summary 并解析为结构化摘要 + 关键路径详情。
    返回人类可读的中文时序分析报告，包含 PASS/FAIL 状态判定。

    Args:
        session_id: 目标会话 ID。
    """
    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    # 第一步:查询当前设计阶段,后面附加到 TimingReport 让用户知道数据来源
    # Bug 2 修复:区分 post-synth 估算 vs post-route 最终,避免误判
    stage, synth_status, impl_status = "unknown", "", ""
    try:
        stage_result = await session.execute(QUERY_DESIGN_STAGE, timeout=15.0)
        if not stage_result.is_error:
            stage, synth_status, impl_status = parse_design_stage(stage_result.output)
    except Exception as e:
        # 阶段查询失败不致命,继续跑时序报告,source_stage 保持 "unknown"
        # 但把具体原因打出来,避免报告里 stage=unknown 让人困惑
        logger.warning("查询 design stage 失败,stage 降级为 unknown: %s", e)

    # 第二步:跑时序报告
    try:
        result = await session.execute(
            "report_timing_summary -return_string", timeout=120.0
        )
        if result.is_error:
            return (
                f"[ERROR] 获取时序报告失败（rc={result.return_code}）：\n"
                f"{result.output}\n\n"
                "提示: report_timing_summary 需要打开综合或实现后的设计。"
                "请先运行 run_synthesis 或 run_implementation。"
            )
        timing_report = parse_timing_summary(result.output)

        # 注入阶段信息
        source_detail, stage_warning = derive_stage_warning(stage, synth_status, impl_status)
        timing_report.source_stage = stage
        timing_report.source_detail = source_detail
        timing_report.stage_warning = stage_warning

        return format_timing_report(timing_report)
    except Exception as e:
        return f"[ERROR] 获取时序报告失败: {e}"


@mcp.tool()
async def check_bitstream_readiness(
    impl_run: str = "impl_1",
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """烧板前一键检查:综合判断工程是否可以安全生成比特流。

    这个工具是"发车前的最后一瞥":在你打算 generate_bitstream 或 program_device
    之前,一次性给出 PASS/BLOCK/WARN 的综合结论,避免烧板后才发现问题。

    检查维度:
    - impl_1 run 是否已到达 route_design Complete(没布线 = 无法生成比特流)
    - route 后的 CRITICAL WARNING 数量(> 0 通常意味着潜在功能风险)
    - 时序是否收敛(WNS/WHS 是否 met)

    返回结论:
    - READY:可以安全烧板
    - BLOCK:存在阻塞性问题(route 未完成 / 时序违例 / 大量 CW)
    - WARN:可以生成但有风险(少量 CW 或估算时序偏低)

    Args:
        impl_run: 实现 run 名称,默认 "impl_1"。
        session_id: 目标会话 ID。
    """
    try:
        impl_run = validate_identifier(impl_run, "impl_run")
    except ValueError as e:
        return f"[ERROR] {e}"

    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    # 1. 查询实现状态 + CW 计数 + 样本
    try:
        pre_result = await session.execute(
            CHECK_PRE_BITSTREAM.format(impl_run=impl_run), timeout=30.0
        )
        status, cw_count, samples = parse_pre_bitstream(pre_result.output)
    except Exception as e:
        return f"[ERROR] 查询实现状态失败: {e}"

    # 2. 查询时序摘要(尽力而为,失败不致命 —— 但一定要打出原因)
    timing_met = None
    timing_line = ""
    timing_err: str = ""
    try:
        timing_raw = await session.execute(
            "report_timing_summary -return_string", timeout=60.0
        )
        if not timing_raw.is_error:
            tr = parse_timing_summary(timing_raw.output)
            timing_met = tr.summary.timing_met
            timing_line = (
                f"  WNS = {tr.summary.wns:+.3f} ns  WHS = {tr.summary.whs:+.3f} ns  "
                f"失败端点 = {tr.summary.failing_endpoints}/{tr.summary.total_endpoints}"
            )
        else:
            timing_err = (
                f"report_timing_summary rc={timing_raw.return_code}: "
                f"{timing_raw.output[:200]}"
            )
    except Exception as e:
        timing_err = f"{type(e).__name__}: {e}"
        logger.warning("check_bitstream_readiness 时序查询失败: %s", timing_err)

    # 3. 判定总体结论
    is_routed = "route_design Complete" in status or "write_bitstream" in status
    has_impl_error = "ERROR" in status.upper()

    blockers: list[str] = []
    warnings_list: list[str] = []

    if has_impl_error:
        blockers.append(f"impl_1 执行错误: {status}")
    elif not is_routed:
        blockers.append(f"impl_1 未完成布线(当前状态: {status or '未启动'})")

    if timing_met is False:
        blockers.append("时序违例(WNS/WHS 为负)")
    elif timing_met is None and is_routed:
        # 把具体失败原因显示出来,而不是笼统的"不可用"
        detail = f": {timing_err}" if timing_err else ""
        warnings_list.append(f"未能读取时序摘要{detail}")

    if cw_count > 0:
        if cw_count >= 5:
            blockers.append(f"CRITICAL WARNING 数量过多: {cw_count} 条")
        else:
            warnings_list.append(f"存在 {cw_count} 条 CRITICAL WARNING,建议排查")

    # 4. 构造报告
    if blockers:
        verdict = "BLOCK (阻塞,不建议生成比特流)"
    elif warnings_list:
        verdict = "WARN (可生成,但有风险)"
    else:
        verdict = "READY (可以安全生成比特流)"

    out: list[str] = [f"=== 烧板前检查: {verdict} ==="]
    out.append(f"实现状态: {status or 'UNKNOWN'}")
    out.append(f"CRITICAL WARNING: {cw_count if cw_count >= 0 else '无法读取'}")
    if timing_line:
        out.append("时序摘要:")
        out.append(timing_line)

    if blockers:
        out.append("")
        out.append("阻塞问题:")
        for b in blockers:
            out.append(f"  [X] {b}")
    if warnings_list:
        out.append("")
        out.append("风险提示:")
        for w in warnings_list:
            out.append(f"  [!] {w}")

    if samples and cw_count > 0:
        out.append("")
        out.append(f"CRITICAL WARNING 样本(前 {min(len(samples), 5)} 条):")
        for s in samples[:5]:
            out.append(f"  - {s}")

    if blockers:
        out.append("")
        out.append("建议: 运行 get_critical_warnings 查看详情,修复后再烧板。")

    return "\n".join(out)


@mcp.tool()
async def get_utilization_report(
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """获取资源占用摘要(LUT/FF/BRAM/DSP/IO)。

    执行 ``report_utilization -return_string`` 并从多个表格里抽取核心资源行,
    高亮超过 90% 占用的 [CRITICAL] 项和 70-90% 的 [WARN] 项。

    典型用途:
    - 综合后检查"LUT 够不够 / BRAM 够不够"
    - 时序收敛困难时先看资源是否超限(> 90% 会导致拥塞)

    Args:
        session_id: 目标会话 ID。
    """
    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    try:
        result = await session.execute(
            "report_utilization -return_string", timeout=60.0
        )
        if result.is_error:
            return (
                f"[ERROR] 获取资源占用失败（rc={result.return_code}）：\n"
                f"{result.output}\n\n"
                "提示: report_utilization 需要打开综合或实现后的设计。"
            )
        report = parse_utilization(result.output)
        return format_utilization_report(report)
    except Exception as e:
        return f"[ERROR] 获取资源占用失败: {e}"


@mcp.tool()
async def get_project_info(
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """获取当前 Vivado 项目的综合信息(项目名 / part / 顶层 / 文件列表 / IP / run 状态)。

    一次查询完成"摸底":AI 接手陌生项目时的起点。包含:
    - 项目名称、目录、Part 型号、顶层模块
    - 所有源文件(按类型分组)
    - XDC 约束文件列表
    - IP 实例列表(含 VLNV)
    - synth_1 / impl_1 的当前状态

    Args:
        session_id: 目标会话 ID。
    """
    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    try:
        result = await session.execute(QUERY_PROJECT_INFO, timeout=30.0)
        if result.is_error:
            return (
                f"[ERROR] 获取项目信息失败（rc={result.return_code}）：\n"
                f"{result.output}"
            )
        info = parse_project_info(result.output)
        return format_project_info(info)
    except Exception as e:
        return f"[ERROR] 获取项目信息失败: {e}"


@mcp.tool()
async def get_run_progress(
    run_name: str = "impl_1",
    tail_lines: int = 30,
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """查看 run 的运行进度(适合长任务等待时看"走到哪一步")。

    综合或实现常跑 10-30 分钟,这个工具让你不用开 GUI 就能看到:
    - 当前状态(running / complete / error)与 PROGRESS 百分比
    - runme.log 里最近若干条 Phase 行(Phase 1 → Phase 2.1 → Phase 3 ...)
    - 日志尾部 N 行(含最新 WARNING / CRITICAL WARNING 原文)
    - 日志最后更新时间(判断 Vivado 是否还在活跃)

    Args:
        run_name: run 名称(如 "synth_1" / "impl_1"),默认 "impl_1"。
        tail_lines: 日志尾部要读多少行,默认 30。
        session_id: 目标会话 ID。
    """
    try:
        run_name = validate_identifier(run_name, "run_name")
    except ValueError as e:
        return f"[ERROR] {e}"

    if tail_lines < 0 or tail_lines > 500:
        return "[ERROR] tail_lines 必须在 0~500 之间"

    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    try:
        result = await session.execute(
            QUERY_RUN_PROGRESS.format(run_name=run_name, tail_n=tail_lines),
            timeout=30.0,
        )
        if result.is_error:
            return (
                f"[ERROR] 查询 run 进度失败（rc={result.return_code}）：\n"
                f"{result.output}"
            )
        rp = parse_run_progress(result.output, run_name=run_name)
        return format_run_progress(rp)
    except Exception as e:
        return f"[ERROR] 查询 run 进度失败: {e}"


@mcp.tool()
async def get_next_suggestion(
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """根据当前项目状态推断下一步应该做什么。

    适合新手、刚打开老项目、或者不知道从哪下手的场景。规则:
    - 没项目 → 建议 open_project / create_project
    - 有项目没源文件 → 建议 add_files
    - 没顶层 → 建议 set_property TOP
    - 没 XDC → 建议添加约束
    - 可综合 → xdc_lint + run_synthesis
    - 综合完成 → run_implementation
    - 布线完成 → check_bitstream_readiness + generate_bitstream
    - 比特流就绪 → program_device
    - 任何阶段失败 → 引导到 get_critical_warnings

    Args:
        session_id: 目标会话 ID。
    """
    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    try:
        result = await session.execute(QUERY_PROJECT_INFO, timeout=30.0)
        if result.is_error:
            return (
                f"[ERROR] 获取项目信息失败（rc={result.return_code}）：\n"
                f"{result.output}"
            )
        info = parse_project_info(result.output)
        sug = suggest_next(info)
        return format_suggestion(info, sug)
    except Exception as e:
        return f"[ERROR] 生成下一步建议失败: {e}"


@mcp.tool()
async def get_ip_status(
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """检查项目中所有 IP 的版本状态(哪个需要升级、哪个已锁定)。

    老项目打开后 Vivado 常提示"N 个 IP 需要升级"。这个工具一次性列出:
    - 需要升级的 IP(Vivado 更新了更好的版本)
    - 已锁定的 IP(IS_LOCKED 属性为 TRUE,改动需先解锁)
    - 已最新的 IP

    附带升级建议(单个升级 / 全部升级 / 升级后验证)。

    Args:
        session_id: 目标会话 ID。
    """
    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    try:
        result = await session.execute(
            "report_ip_status -return_string", timeout=60.0
        )
        if result.is_error:
            return (
                f"[ERROR] 获取 IP 状态失败（rc={result.return_code}）：\n"
                f"{result.output}\n\n"
                "提示: report_ip_status 需要项目已打开且含 IP 实例。"
            )
        rep = parse_ip_status(result.output)
        return format_ip_status_report(rep)
    except Exception as e:
        return f"[ERROR] 获取 IP 状态失败: {e}"


@mcp.tool()
async def get_pre_commit_summary(
    impl_run: str = "impl_1",
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """生成一段可以贴进 git commit body 的工程摘要(时序/资源/CW)。

    典型用途:做完 RTL 改动、跑完 impl 之后,想把关键数字写进 commit body,
    避免 "改了 UART 模块" 这种无信息量的 commit。本工具一次性采样:
    - 项目 + part + 顶层
    - 时序摘要(WNS / WHS / 失败端点数)
    - 资源占用(LUT / FF / BRAM / DSP / IOB 百分比)
    - CW / ERROR 计数(若有 impl_run)
    - 综合生成 READY/WARN/FAIL 门禁标签

    输出为 markdown 片段,直接粘贴到 commit 描述。

    Args:
        impl_run: 用来查 runme.log 计数的 run(默认 impl_1)。
        session_id: 目标会话 ID。
    """
    try:
        impl_run = validate_identifier(impl_run, "impl_run")
    except ValueError as e:
        return f"[ERROR] {e}"

    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    # 采样失败列表:任何一项失败都降级 verdict,commit 摘要不能撒谎
    # 空列表 = 全部采样成功,有元素 = 至少一项降级
    sample_failures: list[str] = []

    # 1. 项目信息
    info = None
    try:
        proj = await session.execute(QUERY_PROJECT_INFO, timeout=30.0)
        if not proj.is_error:
            info = parse_project_info(proj.output)
        else:
            sample_failures.append(f"项目信息 rc={proj.return_code}")
    except Exception as e:
        sample_failures.append(f"项目信息: {type(e).__name__}")
        logger.warning("pre_commit 查项目信息失败: %s", e)

    # 2. 时序
    timing = None
    try:
        tr = await session.execute(
            "report_timing_summary -return_string", timeout=60.0
        )
        if not tr.is_error:
            timing = parse_timing_summary(tr.output)
        else:
            sample_failures.append(f"时序 rc={tr.return_code}")
    except Exception as e:
        sample_failures.append(f"时序: {type(e).__name__}")
        logger.warning("pre_commit 查时序失败: %s", e)

    # 3. 资源
    util = None
    try:
        ur = await session.execute(
            "report_utilization -return_string", timeout=60.0
        )
        if not ur.is_error:
            util = parse_utilization(ur.output)
        else:
            sample_failures.append(f"资源 rc={ur.return_code}")
    except Exception as e:
        sample_failures.append(f"资源: {type(e).__name__}")
        logger.warning("pre_commit 查资源失败: %s", e)

    # 4. CW 计数
    errs, cws, warns = -1, -1, -1
    try:
        cr = await session.execute(
            COUNT_WARNINGS.format(run_name=impl_run), timeout=15.0
        )
        if not cr.is_error:
            errs, cws, warns = parse_diag_counts(cr.output)
        else:
            sample_failures.append(f"CW 计数 rc={cr.return_code}")
    except Exception as e:
        sample_failures.append(f"CW 计数: {type(e).__name__}")
        logger.warning("pre_commit 查 CW 计数失败: %s", e)

    # 门禁判定
    verdict = "READY"
    problems: list[str] = []
    if timing is not None:
        if timing.summary.timing_met is False:
            problems.append("时序违例")
    if errs > 0:
        problems.append(f"{errs} 条 ERROR")
    if cws > 5:
        problems.append(f"{cws} 条 CW")

    if problems:
        verdict = "BLOCK" if (timing and timing.summary.timing_met is False) or errs > 0 else "WARN"

    # 关键:采样失败不能默认 READY —— 降级为 DEGRADED,避免误导 commit 摘要
    # 例如项目没打开,之前会输出"[READY]"但其实全是空洞,现在会明确告警。
    if sample_failures and verdict == "READY":
        verdict = "DEGRADED"

    # 格式化为 markdown
    out: list[str] = [f"## 工程摘要 [{verdict}]"]
    if info:
        out.append(f"- 项目: `{info.project_name}` / part `{info.part}` / top `{info.top}`")
        out.append(
            f"- 综合: {info.synth_status or '(未运行)'}"
            f" | 实现: {info.impl_status or '(未运行)'}"
        )
    if timing:
        s = timing.summary
        met = "✅" if s.timing_met else "❌"
        out.append(
            f"- 时序 {met}: WNS `{s.wns:+.3f} ns`, WHS `{s.whs:+.3f} ns`, "
            f"失败端点 `{s.failing_endpoints}/{s.total_endpoints}`"
        )
    if util and util.resources:
        # 挑最关心的 5 种
        core = {r.name: r for r in util.resources}
        nice = []
        for name in ("Slice LUTs", "Slice Registers", "Block RAM Tile", "DSPs", "Bonded IOB"):
            for full_name, row in core.items():
                if name.lower() in full_name.lower():
                    nice.append(f"`{full_name}` {row.used}/{row.available} ({row.percent:.1f}%)")
                    break
        if nice:
            out.append("- 资源: " + "; ".join(nice))
    if errs >= 0 or cws >= 0:
        out.append(f"- 诊断: ERROR `{errs}`, CRITICAL WARNING `{cws}`, WARNING `{warns}`")
    if problems:
        out.append(f"- ⚠️ 阻塞/风险: {' / '.join(problems)}")
    if sample_failures:
        out.append(
            f"- ⚠️ 采样不完整({len(sample_failures)} 项失败): "
            + ", ".join(sample_failures)
            + " —— 摘要可能不准,勿直接贴进 commit"
        )

    out.append("")
    out.append("_Generated by vivado-mcp get_pre_commit_summary._")
    return "\n".join(out)
