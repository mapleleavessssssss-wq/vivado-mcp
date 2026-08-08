"""诊断工具：get_critical_warnings / verify_io_placement。

自动从 Vivado 日志中提取 CRITICAL WARNING，
对比 XDC 约束与实际 IO 布局，帮助快速定位引脚映射等问题。
"""

import logging

from mcp.server.mcpserver import Context

from vivado_mcp.analysis.io_parser import parse_report_io
from vivado_mcp.analysis.io_verifier import format_io_verification, verify_io_placement
from vivado_mcp.analysis.verilog_compile_check import compile_check, format_compile_report
from vivado_mcp.analysis.warning_parser import (
    BatStepResult,
    WarningReport,
    format_bat_steps_section,
    format_nonstandard_section,
    format_warning_report,
    group_warnings,
    parse_bat_run_output,
    parse_critical_warnings,
    parse_diag_counts,
    parse_errors,
    parse_launch_scripts_output,
    parse_sim_logs_output,
    parse_tail_runme_output,
    scan_nonstandard_errors,
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
    LAUNCH_SCRIPTS_AND_GLOB,
    LIST_PROJECT_XDC_FILES,
    QUERY_TARGET_SIMULATOR,
    RUN_BAT_STEP,
    TAIL_RUNME_LOG,
    TAIL_SIM_LOGS,
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


async def _scan_runme_nonstandard(
    session, run_name: str, tail_n: int
) -> tuple[str, str]:
    """tail runme.log 末尾扫非标错误关键词。

    用途:errors=0 且 cw=0 但 run STATUS 含 ERROR 时,Vivado messageDb 看不见
    真正失败原因(典型 TclStackFree / 子进程报错),走这一路兜底。

    Returns:
        ``(status, nonstandard_section)``:
        - ``status``: run STATUS 原文(用于上游判断是否真的失败)
        - ``nonstandard_section``: 格式化后的非标错误段;无命中或脚本失败时为空串
    """
    try:
        out = await session.execute(
            TAIL_RUNME_LOG.format(run_name=run_name, tail_n=tail_n), timeout=20.0
        )
    except Exception as e:
        logger.warning("tail runme.log 失败(run=%s): %s", run_name, e)
        return "", ""

    status, start_line, body = parse_tail_runme_output(out.output)
    if not body:
        return status, ""

    errs = scan_nonstandard_errors(body, start_line=start_line)
    return status, format_nonstandard_section(errs)


async def _get_target_simulator(session) -> str | None:
    """查当前项目 target_simulator;查询失败返回 None(按 XSim 继续,不阻断诊断)。"""
    try:
        r = await session.execute(QUERY_TARGET_SIMULATOR, timeout=10.0)
    except Exception as e:
        logger.warning("查询 target_simulator 失败,按 XSim 继续诊断: %s", e)
        return None

    for line in r.output.splitlines():
        s = line.strip()
        if s.startswith("VMCP_TARGET_SIM:"):
            value = s[len("VMCP_TARGET_SIM:"):].strip()
            if value.startswith("error="):
                logger.warning(
                    "查询 target_simulator 出错,按 XSim 继续诊断: %s",
                    value[len("error="):],
                )
                return None
            return value or None
    # 第三条降级路径:输出里完全没有 VMCP_TARGET_SIM 标记(如传输层吃行)。
    # 同样按 XSim 继续,但要留诊断痕迹(error-handling §1:降级前说明原因)。
    logger.warning(
        "target_simulator 查询输出未含 VMCP_TARGET_SIM 标记,按 XSim 继续诊断,"
        "输出前 200 字: %r",
        r.output[:200],
    )
    return None


async def _run_sim_bat_fallback(session, run_name: str, tail_n: int) -> str:
    """xsim/*.log 缺失时:用 **Vivado session 内 exec** 跑 compile/elaborate.bat。

    Vivado launch_simulation wrapper 在 spawn .bat 之前炸 → xsim/*.log 一个不剩,
    TAIL_SIM_LOGS 拍空。这条路径自己接管:
    1. 调 ``LAUNCH_SCRIPTS_AND_GLOB`` 触发 ``launch_simulation -scripts_only``,
       glob 出 compile/elaborate/simulate .bat
    2. 逐个 ``session.execute(RUN_BAT_STEP)`` —— **Vivado session 自己 exec**,
       PATH 自带 vivado/bin + 完整路径绕开 Win 11 24H2
       ``NoDefaultCurrentDirectoryInExePath`` 安全策略,这两个根因都被绕开
    3. simulate 跳过(拉 xsim/GUI 阻塞),只回带路径

    历史:0.3.15 用 Python subprocess(MCP 进程 spawn .bat),实测因为 MCP 进程
    PATH 没 vivado/bin → xvlog/xelab not_recognized 假阳性误导诊断,返工。
    """
    try:
        out = await session.execute(
            LAUNCH_SCRIPTS_AND_GLOB.format(run_name=run_name), timeout=60.0
        )
    except Exception as e:
        return f"[ERROR] 触发 launch_simulation -scripts_only 失败: {e}"

    sim_dir, scripts_only_status, bat_files = parse_launch_scripts_output(out.output)

    if scripts_only_status == "fileset_not_found":
        return (
            f"[ERROR] 找不到 fileset '{run_name}'。请确认 fileset 名(默认 sim_1)。"
        )

    if not bat_files:
        return format_bat_steps_section([], scripts_only_status, sim_dir)

    # compile → elaborate → simulate 顺序(simulate 跳过)
    step_order = {"compile": 0, "elaborate": 1, "simulate": 2}
    bat_files.sort(key=lambda x: (step_order.get(x[0], 99), x[2]))

    results: list[BatStepResult] = []
    stop_on_fail = False
    for step, _ext, bat_path in bat_files:
        if step == "simulate":
            results.append(
                BatStepResult(
                    step=step,
                    bat_path=bat_path,
                    returncode=-1,
                    stdout_tail="",
                    stderr_tail=(
                        "simulate 会启动 xsim/GUI 阻塞,fallback 不自动跑;"
                        "前两步通过即可手动跑"
                    ),
                )
            )
            continue
        if stop_on_fail:
            results.append(
                BatStepResult(
                    step=step,
                    bat_path=bat_path,
                    returncode=-1,
                    stdout_tail="",
                    stderr_tail="前一步失败,跳过",
                )
            )
            continue
        # Vivado session 内 exec —— Tcl 模板里 bat_path 双引号包,正反斜杠都行
        normalized = bat_path.replace("\\", "/")
        try:
            r = await session.execute(
                RUN_BAT_STEP.format(bat_path=normalized), timeout=180.0
            )
        except Exception as e:
            results.append(
                BatStepResult(
                    step=step,
                    bat_path=bat_path,
                    returncode=-3,
                    stdout_tail="",
                    stderr_tail=f"Tcl exec 调用失败: {e}",
                )
            )
            stop_on_fail = True
            continue

        rc, output = parse_bat_run_output(r.output)
        body_lines = output.splitlines()
        tail = "\n".join(body_lines[-tail_n:])
        results.append(
            BatStepResult(
                step=step,
                bat_path=bat_path,
                returncode=rc,
                # 合并通道,stdout/stderr 不可分,放在 stderr_tail 让诊断段优先展示
                stdout_tail="",
                stderr_tail=tail,
            )
        )
        if rc != 0:
            stop_on_fail = True

    return format_bat_steps_section(results, scripts_only_status, sim_dir)


async def _diagnose_sim_run(session, run_name: str, tail_n: int) -> str:
    """诊断 simulation fileset(sim_*)失败:tail 所有 xsim 子日志 + 扫非标。

    Vivado launch_simulation 走 .bat/.sh 子进程调 xvlog/xelab/xsim,真错误在
    ``<proj>.sim/<sim_fs>/<phase>/xsim/*.log``(**不在 runme.log**)。本函数 glob
    扫这些日志,tail 末尾 N 行并扫非标错误关键词(如 xvlog not in PATH 的中文
    cmd 报错),把 messageDb 看不见的真错暴露给 AI。

    Args:
        session: vivado session
        run_name: simulation fileset 名(如 ``sim_1``)
        tail_n: 每个日志 tail 多少行
    """
    try:
        out = await session.execute(
            TAIL_SIM_LOGS.format(run_name=run_name, tail_n=tail_n), timeout=30.0
        )
    except Exception as e:
        return f"[ERROR] 读取 simulation 日志失败: {e}"

    sim_dir, logs = parse_sim_logs_output(out.output)

    if not sim_dir:
        return (
            f"[ERROR] 找不到 fileset '{run_name}'。"
            "请确认 launch_simulation 已执行过,或检查 fileset 名(默认 sim_1)。"
        )

    # guard target_simulator 提前到 logs 判空之前:本诊断(TAIL_SIM_LOGS / bat
    # fallback)硬编码 XSim 日志布局(*/xsim/*.log)。第三方仿真器
    # (ModelSim/Questa/VCS)场景下,拍空是预期;而 logs 非空时命中的只可能是
    # 上一轮 XSim 的**陈旧日志**(本次真日志在 */<simulator>/ 下),把它当本次
    # 失败的证据展示正是「误导性结论比没结论更糟」。
    simulator = await _get_target_simulator(session)
    non_xsim = bool(simulator) and simulator.lower() != "xsim"

    # 日志"存在但 tail 区间全为空白"视同未生成:spawn 秒死时 launcher 可能先
    # touch 了空 log,一个空文件就击穿 `not logs` 会掉进"未命中关键词、加大
    # tail_n"的死胡同。注意判据是 TAIL_SIM_LOGS 回传的 tail 区间(非文件大小),
    # 文件有内容但尾部全空行的场景会被一并触发,reason 里给出逃生口。
    logs_all_empty = all(not lg.body.strip() for lg in logs)

    if not logs or logs_all_empty:
        if non_xsim:
            sim_lc = simulator.lower()
            return (
                f"仿真目录: {sim_dir}\n"
                f"当前 target_simulator={simulator}(非 XSim),本诊断仅覆盖 XSim "
                "日志布局(*/xsim/*.log),拍空是预期而非故障。\n"
                f"请手查 {sim_dir}/*/{sim_lc}/ 下的日志;\n"
                f"另:第三方仿真器需先 compile_simlib -simulator {sim_lc} "
                "-directory <libs> 编译仿真库,"
                f"并 set_property compxlib.{sim_lc}_compiled_library_dir <libs> "
                "[current_project]。"
            )

        # launch_simulation wrapper 在 spawn .bat **之前**炸时,xsim/*.log
        # 完全没生成。走 scripts-only fallback:跑 -scripts_only 生成 .bat,
        # 再 **Vivado session 内 exec** 逐个跑 compile/elaborate.bat
        # (见 _run_sim_bat_fallback;0.3.15 的 MCP 进程 spawn 方案因 PATH
        # 假阳性已返工),把 Vivado 吞掉的真错暴露出来。
        fallback = await _run_sim_bat_fallback(session, run_name, tail_n)
        reason = (
            f"未找到 xsim 日志文件(预期 {sim_dir}/*/xsim/*.log)"
            if not logs
            else (
                f"xsim 日志存在但 tail 区间全为空白({len(logs)} 个文件,视同未生成;"
                "若确认文件实际有内容,改用更大 tail_n 直接查看)"
            )
        )
        return (
            f"仿真目录: {sim_dir}\n"
            f"{reason} —— "
            "launch_simulation 可能在 spawn .bat 之前就炸了,走 scripts-only fallback:\n\n"
            f"{fallback}"
        )

    lines: list[str] = []
    lines.append(f"=== 仿真诊断: {run_name} ===")
    lines.append(f"仿真目录: {sim_dir}")
    if non_xsim:
        # 第三方仿真器 + logs 非空 = 命中的是上一轮 XSim 的陈旧日志,头部明示
        sim_lc = simulator.lower()
        lines.append(
            f"⚠ 以下为 */xsim/ 下的陈旧 XSim 日志,当前 target_simulator="
            f"{simulator},本次失败的真日志请查 {sim_dir}/*/{sim_lc}/。"
        )
    lines.append(f"扫描到 {len(logs)} 个日志文件")
    lines.append("")

    total_nonstd = 0
    for log in logs:
        basename = log.log_path.replace("\\", "/").rsplit("/", 1)[-1]
        errs = scan_nonstandard_errors(log.body, start_line=log.start_line)
        total_nonstd += len(errs)
        lines.append(f"--- {basename} (tail {tail_n} 行) ---")
        if errs:
            lines.append(f"  命中非标错误 {len(errs)} 条:")
            for e in errs:
                lines.append(f"    [{e.keyword}] 第 {e.line_number} 行: {e.text}")
        body_lines = log.body.splitlines()
        if body_lines:
            preview = body_lines[-8:]
            lines.append("  尾部内容预览:")
            for ln in preview:
                lines.append(f"    | {ln}")
        else:
            lines.append("  (日志为空)")
        lines.append("")

    if total_nonstd == 0:
        lines.append(
            "ℹ 未在 tail 区间命中非标错误关键词。如果仿真确实失败,真错误可能在"
            "更早的日志区间 —— 加大 tail_n(如 tail_n=200)或手动打开日志查看。"
        )
        lines.append("")

    # R4: launch_simulation 多次失败的状态污染提示
    lines.append(
        "提示: launch_simulation 连续失败可能存在 session 内部状态污染。"
        "如本次修复后仍失败,试试 close_sim,或 stop_session → start_session 重启 Vivado。"
    )
    return "\n".join(lines)


@mcp.tool()
async def get_critical_warnings(
    run_name: str = "impl_1",
    compare_with_last: bool = False,
    tail_n: int = 50,
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """提取并分类 CRITICAL WARNING / ERROR / 非标错误,统一失败诊断入口。

    解析指定 run 的 runme.log,按 warning ID 聚合分类,返回中文诊断报告。
    包含已知 warning 的分类标签和修复建议。

    **三种诊断模式根据 run_name 自动选择**:

    1. ``synth_*`` / ``impl_*`` 等综合实现 run:走原流程(``runme.log`` 解析
       ERROR/CRITICAL WARNING)。**额外**:errors=0 且 cw=0 但 STATUS 含 ERROR 时,
       自动 tail ``runme.log`` 最后 N 行扫非标错误关键词(TclStackFree / segfault /
       FATAL 等不带 ERROR: 前缀的内部异常),解决"messageDb 显示干净但 run 实际
       崩了"的盲区。
    2. ``sim_*`` simulation fileset:改去 tail ``<proj>.sim/<sim_fs>/*/xsim/*.log``
       (Vivado launch_simulation 的真错误位置,**不在** runme.log),扫非标
       关键词,自动暴露 xvlog 未找到等子进程错误。
    3. 任何 run:无论结果如何,都会静默把本次 CW 列表写快照(存到项目目录 ``.vmcp/`` 下,
       或 fallback 到 ``~/.claude/vivado-mcp/``)。启用 ``compare_with_last=True`` 时,
       读上次快照与本次对比,报告消除/新增/仍存在的条目。(sim 模式不写快照)

    Args:
        run_name: run / fileset 名称(如 ``synth_1`` / ``impl_1`` / ``sim_1``),
            默认 ``impl_1``。``sim_*`` 走 simulation 日志诊断路径。
        compare_with_last: True 时追加一段与上次快照的差分报告(仅对综合/实现有效)。
        tail_n: 非标错误扫描时每个日志 tail 的末尾行数,默认 50,范围 1~500。
            仿真模式适用于每个 xsim 子日志。
        session_id: 目标会话 ID。
    """
    try:
        run_name = validate_identifier(run_name, "run_name")
    except ValueError as e:
        return f"[ERROR] {e}"

    # 范围校验(对齐 get_run_progress 的 tail_lines):0 在 sim fallback 里
    # 切片语义会反转成"全量回灌",负值让 Tcl lrange 静默拍空,直接拒
    if tail_n < 1 or tail_n > 500:
        return f"[ERROR] tail_n 必须在 1~500 之间(收到 {tail_n})。"

    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    # ============== 仿真分支:走独立路径 ==============
    if run_name.startswith("sim_"):
        return await _diagnose_sim_run(session, run_name, tail_n)

    # ============== 综合 / 实现流程 ==============
    # 第一步：获取计数
    try:
        count_result = await session.execute(
            COUNT_WARNINGS.format(run_name=run_name), timeout=30.0
        )
    except Exception as e:
        return f"[ERROR] 读取警告计数失败: {e}"

    # Tcl 报错(run 不存在/无项目打开)时透传原始错误,不要落进 -1 分支
    # 误报"未找到 runme.log"——那会把 AI 引向"去跑 run"而不是"先开项目"
    if count_result.is_error:
        return (
            f"[ERROR] 读取警告计数失败(rc={count_result.return_code}):\n"
            f"{count_result.output[:200]}\n"
            "提示: 请确认项目已打开且 run 名正确(run_tcl 'get_runs' 可列出)。"
        )

    errors, cw_count, w_count = parse_diag_counts(count_result.output)

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

    # 第四步:errors=0 且 cw=0 时,tail runme.log 兜底扫非标错误
    # 解决 0.3.13 实战发现的"中文路径 TclStackFree → messageDb 看不见"漏报
    nonstandard_section = ""
    if errors == 0 and cw_count == 0:
        status, nonstandard_section = await _scan_runme_nonstandard(
            session, run_name, tail_n
        )
        # 只有 STATUS 含 ERROR 关键字且确实命中关键词时才展示,避免对健康 run 误报
        if nonstandard_section and "ERROR" not in status.upper() \
                and "ABORT" not in status.upper():
            nonstandard_section = ""

    # 第五步:格式化主报告 —— 无 CW 无 ERROR 的短路分支
    if errors == 0 and cw_count == 0:
        if nonstandard_section:
            main = (
                f"诊断概览: errors=0, critical_warnings=0, warnings={w_count}\n"
                f"!! Vivado messageDb 显示无 ERROR/CW,但日志末尾发现非标错误 !!\n\n"
                + nonstandard_section
            )
        else:
            main = (
                f"诊断概览: errors=0, critical_warnings=0, warnings={w_count}\n"
                "未发现 ERROR 或 CRITICAL WARNING。"
            )
    else:
        main = format_warning_report(report)

    # 第六步:追加差分报告(仅当 compare_with_last 启用)
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
    写回前会为每个被修改文件生成同名 .bak 备份;注意 **.bak 只保留最近一次
    修改前的版本**(再次写回会覆盖上一次的 .bak,与 ``sed -i.bak`` 同语义)。

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
            .vhd/.vhdl 不支持,返回 SKIP 并附替代方案指引(check_syntax)。
        tool: "auto"(默认,优先 iverilog) / "iverilog" / "verilator"。
        timeout: 子进程超时秒数,默认 30。
    """
    if not files:
        return "[ERROR] 至少需要一个文件路径"
    if tool not in ("auto", "iverilog", "verilator"):
        return f"[ERROR] 未知 tool '{tool}',应为 auto / iverilog / verilator"

    report = compile_check(files, tool=tool, timeout=timeout)
    return format_compile_report(report)
