"""诊断工具：get_critical_warnings / verify_io_placement。

自动从 Vivado 日志中提取 CRITICAL WARNING，
对比 XDC 约束与实际 IO 布局，帮助快速定位引脚映射等问题。
"""

import logging

from mcp.server.fastmcp import Context

from vivado_mcp.analysis.io_parser import parse_report_io
from vivado_mcp.analysis.io_verifier import format_io_verification, verify_io_placement
from vivado_mcp.analysis.verilog_compile_check import compile_check, format_compile_report
from vivado_mcp.analysis.warning_parser import (
    WarningReport,
    format_warning_report,
    group_warnings,
    parse_critical_warnings,
    parse_diag_counts,
    parse_errors,
)
from vivado_mcp.analysis.warning_snapshot import (
    diff_warnings,
    format_diff_report,
    load_snapshot,
    snapshot_cw,
)
from vivado_mcp.analysis.xdc_auto_fixer import (
    BOARD_PROFILES,
    apply_fixes,
    format_fix_report,
    plan_fixes,
)
from vivado_mcp.analysis.xdc_linter import format_lint_report, lint_xdc_files
from vivado_mcp.analysis.xdc_parser import XdcConstraint, parse_xdc_file
from vivado_mcp.server import _NO_SESSION, _require_session, mcp
from vivado_mcp.tcl_scripts import (
    COUNT_WARNINGS,
    EXTRACT_CRITICAL_WARNINGS,
    EXTRACT_ERRORS,
    LIST_PROJECT_XDC_FILES,
)
from vivado_mcp.vivado.tcl_utils import validate_identifier

logger = logging.getLogger(__name__)

# 轻量 Tcl:查当前项目目录,没打开项目时输出 NONE。快照路径解析会用到。
_QUERY_PROJECT_DIR = (
    "if {[catch {current_project} __p]} { "
    'puts "VMCP_PROJDIR:NONE" '
    "} else { "
    'puts "VMCP_PROJDIR:[get_property DIRECTORY [current_project]]" '
    "}"
)


async def _get_project_dir(session) -> str | None:
    """查当前项目目录;未打开项目或查询失败返回 None(走 fallback 路径)。"""
    try:
        r = await session.execute(_QUERY_PROJECT_DIR, timeout=5.0)
    except Exception as e:
        logger.warning("查询项目目录失败,快照将走 fallback: %s", e)
        return None

    for line in r.output.splitlines():
        line = line.strip()
        if line.startswith("VMCP_PROJDIR:"):
            value = line[len("VMCP_PROJDIR:"):].strip()
            if value and value != "NONE":
                return value
    return None


async def _fetch_project_xdc_paths(session) -> tuple[list[str] | None, str]:
    """从当前 session 的项目里拉所有 XDC 约束文件路径。

    Returns:
        (paths, error_message) — 成功时 error_message 为空;
        失败时 paths=None,error_message 含 [ERROR] 前缀可直接 return 给用户。
    """
    try:
        r = await session.execute(LIST_PROJECT_XDC_FILES, timeout=15.0)
    except Exception as e:
        return None, f"[ERROR] 获取 XDC 文件列表失败: {e}"

    if r.is_error:
        return None, (
            f"[ERROR] 获取 XDC 文件列表失败(rc={r.return_code}):\n"
            f"{r.output}\n"
            "提示: 需要先打开项目(run_tcl 'open_project ...')"
        )

    paths = [
        line[len("VMCP_XDC_FILE:"):].strip()
        for line in r.output.splitlines()
        if line.startswith("VMCP_XDC_FILE:")
    ]
    return paths, ""


@mcp.tool()
async def get_critical_warnings(
    run_name: str = "impl_1",
    compare_with_last: bool = False,
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """提取并分类 CRITICAL WARNING。

    解析指定 run 的 runme.log，按 warning ID 聚合分类，返回中文诊断报告。
    包含已知 warning 的分类标签和修复建议。

    每次调用会静默把本次 CW 列表存成一份快照(存到项目目录 ``.vmcp/`` 下,
    或 fallback 到 ``~/.claude/vivado-mcp/``)。启用 ``compare_with_last=True`` 时,
    读上次快照与本次对比,报告消除/新增/仍存在的条目,帮你判断"修没修对"。

    Args:
        run_name: run 名称(如 "synth_1"、"impl_1"),默认 "impl_1"。
        compare_with_last: True 时追加一段与上次快照的差分报告。
        session_id: 目标会话 ID。
    """
    try:
        run_name = validate_identifier(run_name, "run_name")
    except ValueError as e:
        return f"[ERROR] {e}"

    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    # 第一步：获取计数
    try:
        count_result = await session.execute(
            COUNT_WARNINGS.format(run_name=run_name), timeout=30.0
        )
        errors, cw_count, w_count = parse_diag_counts(count_result.output)
    except Exception as e:
        return f"[ERROR] 读取警告计数失败: {e}"

    if cw_count == -1:
        return "[ERROR] 未找到 runme.log，请确认 run 已执行过。"

    # 第二步：按需提取详情(ERROR 优先,CW 次之)
    # 即使 cw_count == 0 也要把 cw_list(空) 写快照,下次才能识别"全部消除"
    error_groups = []
    cw_groups = []
    cw_list = []

    if errors > 0:
        try:
            err_result = await session.execute(
                EXTRACT_ERRORS.format(run_name=run_name), timeout=60.0
            )
            err_list = parse_errors(err_result.output)
            error_groups = group_warnings(err_list)
        except Exception as e:
            return f"[ERROR] 提取 ERROR 详情失败: {e}"

    if cw_count > 0:
        try:
            cw_result = await session.execute(
                EXTRACT_CRITICAL_WARNINGS.format(run_name=run_name), timeout=60.0
            )
            cw_list = parse_critical_warnings(cw_result.output)
            cw_groups = group_warnings(cw_list)
        except Exception as e:
            return f"[ERROR] 提取 CRITICAL WARNING 详情失败: {e}"

    # 第三步:构造 Report,写快照(无论 compare_with_last 与否,总是写)
    report = WarningReport(
        errors=errors,
        critical_warnings=cw_count,
        warnings=w_count,
        groups=cw_groups,
        error_groups=error_groups,
    )

    # 查项目目录,失败走 fallback
    project_dir = await _get_project_dir(session)

    # 读上次快照(diff 时要用,所以要在覆盖前读)
    prev_report, prev_cws = (None, [])
    if compare_with_last:
        prev_report, prev_cws = load_snapshot(run_name, project_dir)

    # 写本次快照(失败只警告,不打断主流程 —— 按铁律 1.4 要打印具体原因)
    try:
        snapshot_cw(report, cw_list, run_name, project_dir)
    except (OSError, PermissionError, ValueError) as e:
        logger.warning(
            "保存 CW 快照失败(run=%s, project_dir=%s): %s",
            run_name,
            project_dir,
            e,
        )

    # 第四步:格式化主报告 —— 无 CW 无 ERROR 的短路分支
    if errors == 0 and cw_count == 0:
        main = (
            f"诊断概览: errors=0, critical_warnings=0, warnings={w_count}\n"
            "未发现 ERROR 或 CRITICAL WARNING。"
        )
    else:
        main = format_warning_report(report)

    # 第五步:追加差分报告(仅当 compare_with_last 启用)
    if compare_with_last:
        if prev_report is None:
            return (
                main
                + "\n\n=== CW 差分报告 ===\n"
                "无上次快照,本次作为基线已保存。下次再调带 compare_with_last=True "
                "就能看到修复效果了。"
            )
        diff = diff_warnings(prev_cws, cw_list)
        return main + "\n\n" + format_diff_report(diff)

    return main


@mcp.tool()
async def verify_io_placement_tool(
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """验证 IO 引脚分配：比对 XDC 约束与实际布局。

    自动读取项目 XDC 文件中的 PACKAGE_PIN 约束（**支持 -dict 和传统两种语法**），
    与 report_io 的实际分配结果对比，发现 GT 引脚交叉等严重错误。

    GT 端口不匹配标记为 CRITICAL，GPIO 端口标记为 WARNING。

    Args:
        session_id: 目标会话 ID。
    """
    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    # 第一步：从 Vivado 获取 XDC 文件路径列表，然后 Python 读文件解析
    # B3 修复：不再走 Tcl 正则（不支持 -dict），改 Python 文件解析支持两种语法
    xdc_paths, err = await _fetch_project_xdc_paths(session)
    if err:
        return err

    if not xdc_paths:
        return "项目未添加任何 XDC 约束文件。"

    xdc_constraints: list[XdcConstraint] = []
    read_errors: list[str] = []
    for xdc_path in xdc_paths:
        try:
            xdc_constraints.extend(parse_xdc_file(xdc_path))
        except (FileNotFoundError, OSError) as e:
            read_errors.append(f"  {xdc_path}: {e}")

    if not xdc_constraints:
        msg = (
            "XDC 文件中未找到任何 PACKAGE_PIN 约束（已支持 -dict 和传统两种语法）。\n"
            f"已扫描文件: {len(xdc_paths)} 个"
        )
        if read_errors:
            msg += "\n读取失败:\n" + "\n".join(read_errors)
        return msg

    # 第二步：获取 report_io
    try:
        io_result = await session.execute(
            "report_io -return_string", timeout=60.0
        )
    except Exception as e:
        return f"[ERROR] 获取 IO 报告失败: {e}"

    if io_result.is_error:
        return (
            f"[ERROR] report_io 失败（rc={io_result.return_code}）：\n"
            f"{io_result.output}\n"
            "提示: 需要先打开综合或实现后的设计。"
        )

    io_report = parse_report_io(io_result.output)

    if not io_report.ports:
        return "report_io 未返回任何端口信息。请确认实现已完成。"

    # 第三步：对比验证
    verification = verify_io_placement(xdc_constraints, io_report)
    return format_io_verification(verification)


@mcp.tool()
async def xdc_lint(
    xdc_paths: list[str] | None = None,
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """对 XDC 约束文件做静态检查(pure Python,不依赖 Vivado 综合)。

    综合前就能捕到这些常见错误,省掉 30+ 秒的跑综合等待:
    - PIN_CONFLICT:同一物理引脚被多个 port 占用
    - MISSING_IOSTANDARD:有 PACKAGE_PIN 却没配 IOSTANDARD(NSTD-1 / BIVC-1 隐患)
    - DUPLICATE_PORT:同 port 被多次约束不同引脚(后者覆盖)
    - CLOCK_NO_PERIOD:create_clock 缺 -period
    - PIN_CONFLICT_CROSS_FILE:多个 XDC 文件间的引脚冲突

    Args:
        xdc_paths: 要检查的 XDC 文件路径列表。若不传,则从当前 session 的项目里
            自动抓取所有 constrs_1 下的 XDC 文件。
        session_id: 目标会话 ID(仅在不传 xdc_paths 时使用)。
    """
    # 路径来源:显式传入 > 从 session 拉取
    if xdc_paths is None or len(xdc_paths) == 0:
        session = _require_session(ctx, session_id)
        if not session:
            return (
                "[ERROR] 未传 xdc_paths 且 session 不存在。"
                "请传 xdc_paths=['xxx.xdc',...] 或先 start_session + 打开项目。"
            )
        xdc_paths, err = await _fetch_project_xdc_paths(session)
        if err:
            return err

    if not xdc_paths:
        return "项目未添加任何 XDC 约束文件,没什么可检查的。"

    report = lint_xdc_files(list(xdc_paths))
    return format_lint_report(report)


@mcp.tool()
async def xdc_auto_fix(
    xdc_paths: list[str] | None = None,
    board: str = "",
    dry_run: bool = True,
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """自动修复 XDC 文件中能安全自修的问题(MISSING_IOSTANDARD / CLOCK_NO_PERIOD)。

    默认 dry_run=True 只预览补丁,确认无误后调用 dry_run=False 实际写回。

    **只修**这两类问题(其他需要人工判断):
    - MISSING_IOSTANDARD —— 在 PACKAGE_PIN 行之后插入 IOSTANDARD 语句
    - CLOCK_NO_PERIOD    —— 仅当 board 已知时补 -period;未知板跳过

    **绝对不碰**:
    - PIN_CONFLICT / DUPLICATE_PORT / PIN_CONFLICT_CROSS_FILE(冲突问题必须人改)

    Args:
        xdc_paths: XDC 文件路径列表。不传则从当前 session 的项目里抓。
        board: 板卡名,影响默认 IOSTANDARD 和时钟周期。支持:
            basys3 / nexys-a7 / arty-a7 / zybo / kc705。留空用 LVCMOS33 兜底。
        dry_run: True(默认)只输出补丁预览不改文件;False 实际写回。
        session_id: 目标会话 ID(仅在不传 xdc_paths 时使用)。
    """
    # 校验 board
    if board and board.lower() not in BOARD_PROFILES:
        known = ", ".join(sorted(BOARD_PROFILES.keys()))
        return (
            f"[ERROR] 未知板卡 '{board}'。支持的 board: {known}\n"
            "或留空 board 参数(只修 IOSTANDARD,CLOCK 跳过)。"
        )

    # 路径来源:同 xdc_lint
    if xdc_paths is None or len(xdc_paths) == 0:
        session = _require_session(ctx, session_id)
        if not session:
            return (
                "[ERROR] 未传 xdc_paths 且 session 不存在。"
                "请传 xdc_paths=['xxx.xdc',...] 或先 start_session + 打开项目。"
            )
        xdc_paths, err = await _fetch_project_xdc_paths(session)
        if err:
            return err

    if not xdc_paths:
        return "项目未添加任何 XDC 约束文件,没什么可修的。"

    plan = plan_fixes(list(xdc_paths), board=board)
    if not dry_run and plan.patches:
        plan = apply_fixes(plan)
    return format_fix_report(plan)


@mcp.tool()
async def verilog_compile_check(
    files: list[str],
    tool: str = "auto",
    timeout: float = 30.0,
) -> str:
    """用 iverilog / verilator 做 Verilog 语法 + 连接性检查(比 Vivado 综合快 50 倍)。

    典型用途:写完或改完 RTL 想在几秒内确认"能不能过综合",不用等 30-60s Vivado。
    需要机器上装 iverilog 或 verilator:
        Windows: scoop install iverilog / choco install verilator
        Linux:   apt install iverilog / apt install verilator
        macOS:   brew install icarus-verilog / brew install verilator

    检查模式:
    - iverilog -t null:只做 parse + elaboration,不产物
    - verilator --lint-only -Wall:静态检查,风格警告也给(更严格)

    未装任何工具时返回 SKIP 并附安装指引,不报错。

    Args:
        files: Verilog / SystemVerilog 文件路径列表(.v / .sv)。
        tool: "auto"(默认,优先 iverilog) / "iverilog" / "verilator"。
        timeout: 子进程超时秒数,默认 30。
    """
    if not files:
        return "[ERROR] 至少需要一个文件路径"
    if tool not in ("auto", "iverilog", "verilator"):
        return f"[ERROR] 未知 tool '{tool}',应为 auto / iverilog / verilator"

    report = compile_check(files, tool=tool, timeout=timeout)
    return format_compile_report(report)
