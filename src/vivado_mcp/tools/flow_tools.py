"""设计流程工具。

run_synthesis / run_implementation / generate_bitstream / program_device。
封装 Vivado 长时间运行的操作，提供超时管理和进度反馈。
综合/实现后的日志诊断按需执行，bitstream 生成前保留自动安全检查。

**D5 架构**：长任务使用 Python 侧轮询 STATUS/PROGRESS，不再依赖 Tcl `wait_on_run`
阻塞事件循环。这样 subprocess 和 GuiSession 两种实现共用同一套轮询代码，
GUI 模式下 Vivado 界面保持响应。
"""

import asyncio
import logging
import time

from mcp.server.mcpserver import Context

from vivado_mcp.analysis.warning_parser import parse_diag_counts, parse_pre_bitstream
from vivado_mcp.server import _NO_SESSION, _require_session, _safe_execute, mcp
from vivado_mcp.tcl_scripts import (
    CHECK_PRE_BITSTREAM,
    COUNT_WARNINGS,
    LAUNCH_RUN_IF_IDLE,
    POLL_RUN_STATUS,
    QUERY_FILESET_OVERRIDES,
    QUERY_RUN_LAUNCH_STATE,
)
from vivado_mcp.tools._hardware_safety import (
    is_loopback_hw_server,
    is_valid_hw_server_url,
    select_exact_tcl_proc,
)
from vivado_mcp.tools.annotations import HARDWARE_CHANGE, PROJECT_WRITE
from vivado_mcp.vivado.tcl_utils import tcl_quote, to_tcl_path, validate_identifier

logger = logging.getLogger(__name__)

# 轮询间隔（秒）。vendor run 通常以分钟计；5 秒足够反馈进度，同时避免把 GUI
# Tcl event loop 当作高频状态接口。需要更细进度时使用显式 get_run_progress。
_POLL_INTERVAL_SEC = 5.0

# --------------------------------------------------------------------------- #
#  内部辅助：综合 / 实现 / bitstream 共享的轮询逻辑(单一来源,勿复制)
# --------------------------------------------------------------------------- #


def _parse_launch_state(output: str) -> dict[str, str]:
    """Parse the single ``VMCP_LAUNCH_STATE`` protocol line."""
    line = next(
        (line for line in output.splitlines() if line.startswith("VMCP_LAUNCH_STATE|")),
        "",
    )
    if not line:
        return {}
    fields: dict[str, str] = {}
    for item in line.split("|")[1:]:
        key, separator, value = item.partition("=")
        if separator:
            fields[key] = value
    return fields


async def _get_run_launch_decision(session, run_name: str) -> str | None:
    """Return a no-launch result, or ``None`` when the run can be started."""
    try:
        result = await session.execute(
            QUERY_RUN_LAUNCH_STATE.format(run_name=run_name),
            timeout=15.0,
        )
    except Exception as exc:
        return f"[ERROR] 查询 run 启动状态失败: {type(exc).__name__}: {exc}"
    if result.is_error:
        return (
            f"[ERROR] 查询 run 启动状态失败(rc={result.return_code}):\n"
            f"{result.output}"
        )

    fields = _parse_launch_state(result.output)
    if not fields:
        return "[ERROR] run 启动状态缺少 VMCP_LAUNCH_STATE 标记，未执行 launch。"
    if fields.get("found") != "1":
        return (
            f"[ERROR] Run '{run_name}' 不存在或不唯一"
            f"(count={fields.get('count', 'unknown')})，未执行 launch。"
        )

    status = fields.get("status", "")
    progress = fields.get("progress", "")
    needs_refresh_raw = fields.get("needs_refresh", "").strip().lower()
    needs_refresh = (
        True
        if needs_refresh_raw in {"1", "true", "yes"}
        else False
        if needs_refresh_raw in {"0", "false", "no"}
        else None
    )
    lowered = status.lower()

    if "running" in lowered or "queued" in lowered:
        return (
            f"[ALREADY_RUNNING] {run_name}: status={status}, "
            f"progress={progress or 'unknown'}。没有重复 launch；"
            "请按需低频调用 get_run_progress。"
        )
    if "error" in lowered:
        return (
            f"[BLOCKED] {run_name} 当前为 ERROR: {status}。未自动 reset；"
            "请先诊断，确需删除旧结果时单独批准 reset_project_run。"
        )
    if "out-of-date" in lowered or needs_refresh is True:
        return (
            f"[OUT_OF_DATE] {run_name}: status={status}, "
            f"needs_refresh={needs_refresh_raw or 'unknown'}。"
            "未强制标记 up-to-date，也未自动 reset；请先确认变更范围。"
        )
    if "complete" in lowered:
        if needs_refresh is False:
            return (
                f"[UP_TO_DATE] {run_name}: {status}。未重复 launch；"
                "可直接复用该 run 的状态、指标和已生成报告。"
            )
        return (
            f"[COMPLETE] {run_name}: {status}，但当前 Vivado 未提供可判定的"
            " NEEDS_REFRESH。为避免无意义 reset/relaunch，本次未启动。"
        )
    return None

async def _poll_run_until_done(
    session,
    run_name: str,
    timeout_sec: float,
    ctx: Context,
    expected_complete_step: str | None = None,
) -> tuple[str, str, str, str]:
    """每 5s 轮询 run STATUS/PROGRESS 直到终态(Complete/ERROR)或超时。

    Tcl 片段在 tcl_scripts.POLL_RUN_STATUS(单一定义);本 helper 是
    _launch_and_wait 和 generate_bitstream 的共用轮询循环。

    Returns:
        ``(outcome, final_status, final_progress, final_elapsed)``,
        outcome ∈ {"done", "timeout"}。outcome=="done" 时已上报 100% 进度。

    Raises:
        轮询 execute 抛出的异常原样上抛,由调用方格式化错误消息。
    """
    deadline = time.time() + timeout_sec
    final_status = "UNKNOWN"
    final_progress = "0%"
    final_elapsed = ""
    last_progress_int = 0

    await ctx.report_progress(progress=0, total=100)

    while time.time() < deadline:
        poll = await session.execute(
            POLL_RUN_STATUS.format(run_name=run_name), timeout=15.0
        )

        line = next(
            (ln for ln in poll.output.splitlines() if ln.startswith("VMCP_POLL|")),
            None,
        )
        if line:
            parts = line[len("VMCP_POLL|"):].split("|")
            if len(parts) >= 2:
                final_status = parts[0]
                final_progress = parts[1]
                final_elapsed = parts[2] if len(parts) >= 3 else ""

        # 进度更新
        try:
            progress_int = int(final_progress.rstrip("%").strip() or "0")
        except ValueError:
            progress_int = last_progress_int
        if progress_int != last_progress_int:
            await ctx.report_progress(progress=progress_int, total=100)
            last_progress_int = progress_int

        # 终态判断。bitstream 启动后 STATUS 可能短暂保留旧的
        # ``route_design Complete!``；只有目标 step 完成才允许返回。
        completed = "Complete" in final_status
        if expected_complete_step is not None:
            completed = completed and expected_complete_step in final_status
        if completed:
            break
        if "ERROR" in final_status.upper():
            break

        await asyncio.sleep(_POLL_INTERVAL_SEC)
    else:
        return ("timeout", final_status, final_progress, final_elapsed)

    await ctx.report_progress(progress=100, total=100)
    return ("done", final_status, final_progress, final_elapsed)


async def _query_fileset_overrides(session) -> list[str]:
    """PRD B4:查 sources_1 的 generic / verilog_define,格式化成结果行。

    Returns:
        要追加进结果的行列表:成功时两行 ``applied_generic: ...`` /
        ``applied_verilog_define: ...``(空值明示「(无)」;generic 有值时
        行尾追加 vivado-quirks §3 提醒 —— fileset generic 显示有值不保证
        综合实际生效);查询失败时一行 ``[DEGRADED]``(含具体原因,不阻塞主流程)。
    """
    try:
        ov = await session.execute(QUERY_FILESET_OVERRIDES, timeout=15.0)
    except Exception as e:
        logger.warning("查询 fileset 参数覆盖(generic/verilog_define)异常: %s", e)
        return [f"[DEGRADED] generic/verilog_define 查询异常: {e}"]

    if ov.is_error:
        logger.warning(
            "查询 fileset 参数覆盖失败(rc=%d): %s", ov.return_code, ov.output[:200]
        )
        return [
            f"[DEGRADED] generic/verilog_define 查询失败"
            f"(rc={ov.return_code}): {ov.output[:200]}"
        ]

    applied_generic = "(无)"
    applied_vdefine = "(无)"
    for ln in ov.output.splitlines():
        s = ln.strip()
        if s.startswith("VMCP_FS_OVERRIDE:generic="):
            applied_generic = s[len("VMCP_FS_OVERRIDE:generic="):].strip() or "(无)"
        elif s.startswith("VMCP_FS_OVERRIDE:verilog_define="):
            applied_vdefine = (
                s[len("VMCP_FS_OVERRIDE:verilog_define="):].strip() or "(无)"
            )
    generic_line = f"applied_generic: {applied_generic}"
    if applied_generic != "(无)":
        # vivado-quirks §3:fileset 级 generic 即便显示有值(综合日志都打印
        # bound to:)也可能没真生效(类型不匹配/缓存走错分支),行尾明示防误信。
        generic_line += (
            "(注意 2019.1 quirk: fileset generic 显示有值不保证综合实际生效,"
            "关键参数请核对 runme.log 的 'bound to:' 行,见 vivado-quirks §3)"
        )
    return [
        generic_line,
        f"applied_verilog_define: {applied_vdefine}",
    ]


# --------------------------------------------------------------------------- #
#  内部辅助：综合 / 实现共享的 launch-and-wait 逻辑
# --------------------------------------------------------------------------- #

async def _launch_and_wait(
    session,
    run_name: str,
    jobs: int,
    timeout_minutes: int,
    label: str,
    ctx: Context,
    wait_for_completion: bool = True,
    post_check: str = "none",
    inspect_fileset_overrides: bool = True,
    max_threads: int = 0,
) -> str:
    """原子启动 run；按需由 Python 轮询，且从不隐式 reset/open design。

    不再调用 Tcl 的 `wait_on_run`（它会阻塞 Vivado event loop，
    GUI 模式下会冻住界面）。改用 Python 每 5 秒查一次 STATUS/PROGRESS。
    """
    if post_check not in {"none", "on_failure", "always"}:
        return (
            f"[ERROR] post_check={post_check!r} 非法；"
            "支持 none / on_failure / always。"
        )
    if not 0 <= max_threads <= 8:
        return "[ERROR] max_threads 必须为 0..8；0 表示继承当前 Vivado 设置。"

    timeout_sec = timeout_minutes * 60.0

    # ------------------- 0. PRD B4:读取实际生效的参数覆盖 -------------------
    # 在 launch_runs 之前查 generic / verilog_define,结果里明示,
    # 防止"以为 set_property generic 生效了实际没设上"的隐性坑。
    override_lines = (
        await _query_fileset_overrides(session)
        if inspect_fileset_overrides
        else []
    )

    # ------------------- 1. 启动 -------------------
    launch_tcl = LAUNCH_RUN_IF_IDLE.format(run_name=run_name, jobs=jobs)
    thread_lines: list[str] = []
    if max_threads > 0:
        # Project runs snapshot general.maxThreads into the child run Tcl at launch.
        # Restore the parent interpreter immediately so one call does not silently
        # change later work in the same GUI session.  This sequence is compatible
        # with Tcl 8.5 used by Vivado 2018.3/2020.2.
        indented_launch_tcl = "\n".join(
            f"    {line}" for line in launch_tcl.splitlines()
        )
        launch_tcl = (
            "set __vmcp_prev_threads [get_param general.maxThreads]\n"
            "set __vmcp_launch_rc [catch {\n"
            f"    set_param general.maxThreads {max_threads}\n"
            f"{indented_launch_tcl}\n"
            "} __vmcp_launch_result]\n"
            "set __vmcp_restore_rc [catch {\n"
            "    set_param general.maxThreads $__vmcp_prev_threads\n"
            "} __vmcp_restore_result]\n"
            "puts \"VMCP_THREAD_CONTROL|requested="
            f"{max_threads}|previous=$__vmcp_prev_threads|"
            "launch_rc=$__vmcp_launch_rc|restore_rc=$__vmcp_restore_rc\"\n"
            "if {$__vmcp_launch_rc != 0} {error $__vmcp_launch_result}\n"
            "if {$__vmcp_restore_rc != 0} {error $__vmcp_restore_result}"
        )
        thread_lines.append(
            f"单 run CPU 线程请求: {max_threads}；launch 后已请求恢复父会话设置。"
        )

    try:
        launch_result = await session.execute(
            launch_tcl,
            timeout=60.0,
        )
        if launch_result.is_error:
            return f"[ERROR] 启动 {label} 失败:\n{launch_result.output}"
    except Exception as e:
        return f"[ERROR] 启动 {label} 失败: {e}"

    launch_line = next(
        (
            line for line in launch_result.output.splitlines()
            if line.startswith("VMCP_RUN_LAUNCH|")
        ),
        None,
    )
    if launch_line is None:
        return f"[ERROR] 启动 {label} 失败: Vivado 未返回 VMCP_RUN_LAUNCH 状态标记"

    launch_parts = launch_line.split("|", 2)
    launch_state = launch_parts[1] if len(launch_parts) >= 2 else ""
    launch_status = launch_parts[2] if len(launch_parts) >= 3 else ""
    if launch_state == "busy":
        return (
            f"[BUSY] {label} run '{run_name}' 已在运行，未执行 reset_run。\n"
            f"状态: {launch_status}\n"
            f"请用 get_run_progress(run_name='{run_name}', "
            f"session_id='{session.session_id}') 查询。"
        )
    if launch_state == "missing":
        return f"[ERROR] 启动 {label} 失败: 找不到 run '{run_name}'"
    if launch_state == "not_ready":
        return (
            f"[BLOCKED] {label} run '{run_name}' 当前状态不允许直接 launch: "
            f"{launch_status or '<unknown>'}。未执行 reset_run。"
        )
    if launch_state != "started":
        return f"[ERROR] 启动 {label} 失败: 未知启动状态 {launch_state!r}"

    if not wait_for_completion:
        result_parts = [
            f"[STARTED] {label}已异步启动: {run_name}",
            f"job_id: {session.session_id}:{run_name}",
            f"状态: {launch_status or '已提交'}",
            "未重置 run，未打开 design，也未等待完成。",
            (
                f"查询: get_run_progress(run_name='{run_name}', "
                f"session_id='{session.session_id}')"
            ),
            "完成后只请求能改变下一步决策的报告。",
        ]
        result_parts.extend(thread_lines)
        result_parts.extend(override_lines)
        return "\n".join(result_parts)

    # ------------------- 2. 轮询 -------------------
    try:
        outcome, final_status, final_progress, final_elapsed = (
            await _poll_run_until_done(session, run_name, timeout_sec, ctx)
        )
    except Exception as e:
        return f"[ERROR] 轮询 {label} 状态失败: {e}"
    if outcome == "timeout":
        return f"[ERROR] {label}超时（{timeout_minutes} 分钟），最后状态: {final_status}"

    # ------------------- 3. 诊断概览 -------------------
    result_parts: list[str] = [
        f"--- {label}结果 ---",
        f"状态: {final_status}",
        f"进度: {final_progress}",
        f"耗时: {final_elapsed}",
    ]
    result_parts.extend(override_lines)
    result_parts.extend(thread_lines)
    result_parts.append("未自动 open_run；需要设计报告时请单独明确打开目标 run。")

    run_failed = "ERROR" in final_status.upper()
    should_check = post_check == "always" or (
        post_check == "on_failure" and run_failed
    )
    if not should_check:
        result_parts.append(
            f"后置日志扫描: NOT_RUN (post_check={post_check})；"
            "需要 warning/error 统计时显式调用 get_critical_warnings。"
        )
    else:
        try:
            diag_result = await session.execute(
                COUNT_WARNINGS.format(run_name=run_name), timeout=30.0
            )
            if diag_result.is_error:
                # 计数命令本身失败:不能把 -1 哨兵打成数字误导 AI,显式降级
                result_parts.append(
                    f"\n[DEGRADED] 诊断计数不可用(rc={diag_result.return_code}): "
                    f"{diag_result.output[:200]}"
                )
            else:
                errors, cw, w = parse_diag_counts(diag_result.output)
                if errors < 0 or cw < 0:
                    # -1 哨兵 = runme.log 缺失或输出无 VMCP_DIAG 标记
                    result_parts.append(
                        "\n[DEGRADED] 诊断计数不可用: "
                        "runme.log 缺失或未匹配到 VMCP_DIAG 标记"
                    )
                else:
                    if cw > 0:
                        result_parts.insert(
                            0,
                            f"!! 发现 {cw} 条 CRITICAL WARNING !! "
                            "建议立即运行 get_critical_warnings 查看分类详情和修复建议。",
                        )
                    if errors > 0:
                        result_parts.insert(
                            0,
                            f"!! 发现 {errors} 条 ERROR !! 请检查 runme.log 详情。",
                        )
                    result_parts.append(
                        f"\n诊断概览: errors={errors},"
                        f" critical_warnings={cw}, warnings={w}"
                    )
        except Exception as e:
            # 诊断失败不阻塞主流程，但要告诉用户原因（1.4 错误处理铁律）
            result_parts.append(f"\n（诊断统计失败: {e}）")

    return "\n".join(result_parts)


# --------------------------------------------------------------------------- #
#  工具定义
# --------------------------------------------------------------------------- #

@mcp.tool(annotations=PROJECT_WRITE)
async def run_synthesis(
    run_name: str = "synth_1",
    jobs: int = 4,
    max_threads: int = 0,
    timeout_minutes: int = 30,
    wait_for_completion: bool = False,
    post_check: str = "none",
    session_id: str = "default",
    ctx: Context = None,
    wait: bool | None = None,
) -> str:
    """启动综合；不隐式 reset run，也不隐式打开综合设计。

    不调用 Tcl wait_on_run(会阻塞 Vivado event loop,GUI 模式冻住界面);
    默认 ``wait_for_completion=False``，启动后立即返回，让用户在长编译期间
    保持可见、可控；使用 ``get_run_progress`` 查询所有 run 状态。只有明确传入
    ``wait_for_completion=True`` 才在本次 MCP 调用中轮询至终态。

    Args:
        run_name: 综合 run 名称，默认 "synth_1"。
        jobs: 同时调度的并行 run 槽位，默认 4；不是单个 run 的 CPU 线程数。
        max_threads: 单个 run 请求使用的 CPU 线程上限，1..8；默认 0 继承当前
            Vivado 设置。显式设置只影响本次 launch，随后恢复父 GUI 会话参数。
        timeout_minutes: 超时分钟数，默认 30。
        wait_for_completion: 是否在本次调用中等待完成，默认 False。
        post_check: 等待完成后的日志扫描策略：``none``(默认)、
            ``on_failure`` 或 ``always``。启动即返回时不执行后置扫描。
        session_id: 目标会话 ID。
        wait: v0.3.25 兼容别名；显式传入时覆盖 wait_for_completion。
    """
    try:
        run_name = validate_identifier(run_name, "run_name")
    except ValueError as e:
        return f"[ERROR] {e}"

    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    if wait is not None:
        wait_for_completion = wait

    decision = await _get_run_launch_decision(session, run_name)
    if decision is not None:
        return decision

    return await _launch_and_wait(
        session, run_name, jobs, timeout_minutes, "综合", ctx,
        wait_for_completion,
        post_check,
        True,
        max_threads,
    )


@mcp.tool(annotations=PROJECT_WRITE)
async def run_implementation(
    run_name: str = "impl_1",
    jobs: int = 4,
    max_threads: int = 0,
    timeout_minutes: int = 60,
    wait_for_completion: bool = False,
    post_check: str = "none",
    session_id: str = "default",
    ctx: Context = None,
    wait: bool | None = None,
) -> str:
    """启动实现（布局布线）；不隐式 reset run，也不隐式打开设计。

    不调用 Tcl wait_on_run(会阻塞 Vivado event loop,GUI 模式冻住界面);
    默认只启动后返回；只有 ``wait_for_completion=True`` 才在本次调用中轮询。

    Args:
        run_name: 实现 run 名称，默认 "impl_1"。
        jobs: 同时调度的并行 run 槽位，默认 4；不是单个 run 的 CPU 线程数。
        max_threads: 单个 run 请求使用的 CPU 线程上限，1..8；默认 0 继承当前
            Vivado 设置。显式设置只影响本次 launch，随后恢复父 GUI 会话参数。
        timeout_minutes: 超时分钟数，默认 60。
        wait_for_completion: 是否在本次调用中等待完成，默认 False。
        post_check: 等待完成后的日志扫描策略：``none``(默认)、
            ``on_failure`` 或 ``always``。
        session_id: 目标会话 ID。
        wait: v0.3.25 兼容别名；显式传入时覆盖 wait_for_completion。
    """
    try:
        run_name = validate_identifier(run_name, "run_name")
    except ValueError as e:
        return f"[ERROR] {e}"

    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    if wait is not None:
        wait_for_completion = wait

    decision = await _get_run_launch_decision(session, run_name)
    if decision is not None:
        return decision

    return await _launch_and_wait(
        session, run_name, jobs, timeout_minutes, "实现", ctx,
        wait_for_completion,
        post_check,
        False,
        max_threads,
    )


@mcp.tool(annotations=PROJECT_WRITE)
async def reset_project_run(
    run_name: str,
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """显式重置一个 Vivado project run。

    这是会删除该 run 既有综合/实现结果的破坏性操作，绝不由
    ``run_synthesis``、``run_implementation`` 或 ``generate_bitstream`` 隐式调用。
    """
    try:
        run_name = validate_identifier(run_name, "run_name")
    except ValueError as exc:
        return f"[ERROR] {exc}"

    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)
    return await _safe_execute(
        session,
        f"reset_runs {run_name}",
        60.0,
        "重置 project run 失败",
    )


@mcp.tool(annotations=PROJECT_WRITE)
async def generate_bitstream(
    impl_run: str = "impl_1",
    jobs: int = 4,
    timeout_minutes: int = 30,
    force: bool = False,
    wait_for_completion: bool = False,
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """生成比特流文件。在实现完成后执行。

    默认启用前置安全检查：检测 CRITICAL WARNING 后阻止生成，
    需确认无风险后使用 force=True 跳过检查。默认只启动后返回；显式
    ``wait_for_completion=True`` 才在本次调用内低频轮询到完成。

    Args:
        impl_run: 实现 run 名称，默认 "impl_1"。
        jobs: 同时调度的并行 run 槽位，默认 4；不是单个 run 的 CPU 线程数。
        timeout_minutes: 超时分钟数，默认 30。
        force: 跳过 CRITICAL WARNING 安全检查，默认 False。
        wait_for_completion: 是否等待 Bitstream 完成，默认 False。
        session_id: 目标会话 ID。
    """
    try:
        impl_run = validate_identifier(impl_run, "impl_run")
    except ValueError as e:
        return f"[ERROR] {e}"

    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    # 前置安全检查：force=False 时检测 CRITICAL WARNING。
    # 安全门禁 fail-closed：无法证明 run 状态和 CW 数量时，不允许启动 bitstream。
    if not force:
        try:
            pre_result = await session.execute(
                CHECK_PRE_BITSTREAM.format(impl_run=impl_run), timeout=30.0
            )
            status, cw_count, samples = parse_pre_bitstream(pre_result.output)

            if pre_result.is_error or cw_count < 0:
                # Tcl 报错(run 不存在/无项目)或输出无 VMCP_PRE_BIT 标记(-1 哨兵)
                reason = (
                    f"rc={pre_result.return_code}, 输出: {pre_result.output[:200]}"
                    if pre_result.is_error
                    else "输出未匹配到 VMCP_PRE_BIT 标记(run 不存在或日志不可读)"
                )
                return (
                    f"[BLOCKED] 前置 CW 安全检查未能执行: {reason}。"
                    "未启动 bitstream；请修复检查条件，或在人工核对后显式 force=True。"
                )
            elif cw_count > 0:
                lines = [
                    f"!! 安全检查未通过: 发现 {cw_count} 条 CRITICAL WARNING !!",
                    f"实现状态: {status}",
                    "",
                    "前 10 条 CRITICAL WARNING 样本:",
                ]
                for s in samples:
                    lines.append(f"  - {s}")
                lines.append("")
                lines.append(
                    "建议: 先运行 get_critical_warnings 查看详情并修复。"
                )
                lines.append(
                    "如确认可忽略，请使用 force=True 跳过安全检查。"
                )
                return "\n".join(lines)
        except Exception as e:
            logger.warning(
                "bitstream 前置安全检查失败并阻止启动: %s: %s",
                type(e).__name__, e,
            )
            return (
                f"[BLOCKED] 前置 CW 安全检查失败: {type(e).__name__}: {e}。"
                "未启动 bitstream；请修复检查条件，或在人工核对后显式 force=True。"
            )

    # D5 架构同步到 bitstream:不再用 Tcl wait_on_run(阻塞 Vivado event loop,
    # GUI 模式下界面冻住)。改 Python 轮询 STATUS/PROGRESS,界面保持响应 +
    # 提供进度反馈。
    timeout_sec = timeout_minutes * 60.0

    # 启动 —— 到 write_bitstream step 为止,不重置 route 结果
    try:
        launch_result = await session.execute(
            f"launch_runs {impl_run} -to_step write_bitstream -jobs {jobs}",
            timeout=60.0,
        )
        if launch_result.is_error:
            return f"[ERROR] 启动比特流生成失败:\n{launch_result.output}"
    except Exception as e:
        return f"[ERROR] 启动比特流生成失败: {e}"

    if not wait_for_completion:
        precheck = (
            "前置 CW 安全检查: 已按 force=True 显式跳过。"
            if force
            else "前置 CW 安全检查: PASS。"
        )
        return (
            f"[STARTED] 已启动 Bitstream: {impl_run} -> write_bitstream。\n"
            f"{precheck}\n"
            "未等待完成；请稍后用 get_run_progress(run_name='"
            f"{impl_run}') 按需查询，不要高频轮询。"
        )

    # 轮询(与 _launch_and_wait 共用 _poll_run_until_done,单一来源)
    try:
        outcome, final_status, final_progress, final_elapsed = (
            await _poll_run_until_done(
                session,
                impl_run,
                timeout_sec,
                ctx,
                expected_complete_step="write_bitstream",
            )
        )
    except Exception as e:
        return f"[ERROR] 轮询比特流状态失败: {e}"
    if outcome == "timeout":
        return (
            f"[ERROR] 生成比特流超时({timeout_minutes} 分钟)。"
            f"最后状态: {final_status},进度: {final_progress}"
        )

    if "ERROR" in final_status.upper():
        return (
            f"[ERROR] 生成比特流失败。\n状态: {final_status}\n"
            f"进度: {final_progress}\n耗时: {final_elapsed}\n"
            "建议:运行 get_critical_warnings impl_1 查看详情。"
        )

    # 查比特流输出目录
    try:
        bit_result = await session.execute(
            f'set d [get_property DIRECTORY [get_runs {impl_run}]]\n'
            f'puts "VMCP_BITDIR:$d"',
            timeout=10.0,
        )
        bit_dir = next(
            (ln[len("VMCP_BITDIR:"):].strip()
             for ln in bit_result.output.splitlines()
             if ln.startswith("VMCP_BITDIR:")),
            "(未能读取)",
        )
    except Exception as e:
        bit_dir = f"(查询失败: {e})"

    result_text = (
        f"--- 比特流生成结果 ---\n"
        f"状态: {final_status}\n"
        f"进度: {final_progress}\n"
        f"耗时: {final_elapsed}\n"
        f"比特流目录: {bit_dir}"
    )
    return result_text


@mcp.tool(annotations=HARDWARE_CHANGE)
async def program_device(
    bitstream_path: str,
    target: str = "*",
    hw_target_name: str = "",
    hw_server_url: str = "localhost:3121",
    allow_remote_hw_server: bool = False,
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """编程 FPGA 设备。封装 open_hw_manager → connect → program 多步操作。

    只烧 .bit 进 FPGA(掉电即丢)。要掉电自启动须烧 SPI flash,见下面配方。

    **烧 flash 配方(2019.1,run_tcl 逐步执行)**:
    1. 查 flash 型号: ``get_cfgmem_parts -of [lindex [get_hw_devices] 0]``
       (或按板上 flash 用 -filter 选,如 mt25ql128-spi-x1_x2_x4)
    2. 生成 .mcs: ``write_cfgmem -format mcs -size 16 -interface SPIx4
       -loadbit {up 0x0 <top>.bit} -force out.mcs``
    3. 建 cfgmem 对象: ``create_hw_cfgmem -hw_device [current_hw_device]
       [lindex [get_cfgmem_parts <part>] 0]``
    4. 设属性四件套: ``set_property PROGRAM.FILES {out.mcs} [current_hw_cfgmem]``
       + PROGRAM.ERASE 1 / PROGRAM.CFG_PROGRAM 1 / PROGRAM.VERIFY 1
    5. 烧写: ``program_hw_cfgmem``
    6. 烧后 ``boot_hw_device [current_hw_device]`` 或断电重启从 flash 加载。
    (Zynq 用 .bin: ``write_cfgmem -format bin -interface SMAPx32 ...``)

    Args:
        bitstream_path: 比特流文件路径（.bit 文件）。
        target: 精确设备对象名/NAME；默认 "*" 表示设备必须恰好只有一个。
        hw_target_name: 可选的精确 hw_target 对象名/NAME；留空时必须恰好一个。
        hw_server_url: 硬件服务器地址，默认 "localhost:3121"。
        allow_remote_hw_server: 是否明确允许远程 hw_server，默认 False。
        session_id: 目标会话 ID。
    """
    # 路径预检:避免半路 program_hw_devices 才报 "file not found",
    # 此时 hw_server / hw_target 已连上,留下脏状态。
    import os
    if not os.path.isfile(bitstream_path):
        return (
            f"[ERROR] 比特流文件不存在: {bitstream_path}\n"
            "提示:先 generate_bitstream 或确认路径(常见位置:"
            "<proj>.runs/impl_1/<top>.bit)"
        )
    if not bitstream_path.lower().endswith(".bit"):
        return (
            f"[ERROR] 文件扩展名不是 .bit: {bitstream_path}\n"
            "program_device 只接受 .bit 文件(.bin/.mcs 用 write_cfgmem 烧 flash,"
            "配方见本工具 docstring)"
        )

    # 轻量参数校验:两者都会裸拼进 Tcl,空串/含 Tcl 特殊字符直接报清晰错误,
    # 而不是半路炸出难懂的 Tcl 解析错(此时 hw_server 可能已连上,留脏状态)。
    _TCL_UNSAFE = set(' \t\n;$[]{}"\\')
    if not target or any(c in _TCL_UNSAFE for c in target):
        # 文案与实现对齐:实现是黑名单(只拒会破坏 Tcl 裸拼词法的字符)
        return (
            f"[ERROR] target 为空或含非法字符: {target!r}。"
            "不允许空白/;/$/[]/{}/引号/反斜杠"
            "(如 '*' 或 'localhost:3121/xilinx_tcf/...')"
        )
    if not is_valid_hw_server_url(hw_server_url):
        return (
            f"[ERROR] hw_server_url 格式非法: {hw_server_url!r}。"
            "应形如 'localhost:3121'、'[::1]:3121' 或 '<主机名/IP>:<端口>'"
        )
    if not is_loopback_hw_server(hw_server_url) and not allow_remote_hw_server:
        return (
            f"[BLOCKED] 默认禁止远程 hw_server: {hw_server_url!r}。"
            "请确认目标电脑、板卡和网络边界后显式设置 allow_remote_hw_server=True。"
        )

    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    bit_tcl = to_tcl_path(bitstream_path)
    server_tcl = tcl_quote(hw_server_url)
    hw_target_tcl = tcl_quote(hw_target_name)
    # Backward-compatible '*' no longer means "silently take index 0".  It
    # means no selector was supplied, so the exact-selection helper requires
    # the current hardware session to contain exactly one device.
    device_tcl = tcl_quote("" if target == "*" else target)

    tcl = (
        f'{select_exact_tcl_proc()}\n'
        f'set __vmcp_server_url {server_tcl}\n'
        f'set __vmcp_target_name {hw_target_tcl}\n'
        f'set __vmcp_device_name {device_tcl}\n'
        f'open_hw_manager\n'
        f'if {{[llength [get_hw_servers -quiet]] == 0}} {{ '
        f'connect_hw_server -url $__vmcp_server_url }}\n'
        f'set __vmcp_targets [get_hw_targets -quiet]\n'
        f'set __vmcp_target [__vmcp_select_exact $__vmcp_targets '
        f'$__vmcp_target_name hw_target]\n'
        f'if {{![get_property IS_OPENED $__vmcp_target]}} {{ '
        f'open_hw_target $__vmcp_target }}\n'
        f'set __vmcp_devs [get_hw_devices -quiet]\n'
        f'set dev [__vmcp_select_exact $__vmcp_devs '
        f'$__vmcp_device_name hw_device]\n'
        f'current_hw_device $dev\n'
        f'set probes [get_property PROBES.FILE $dev]\n'
        f'if {{$probes ne "" && ![file isfile $probes]}} {{ '
        f'set_property PROBES.FILE {{}} $dev; set_property FULL_PROBES.FILE {{}} $dev }}\n'
        f'set_property PROGRAM.FILE {bit_tcl} $dev\n'
        f'program_hw_devices $dev\n'
        f'puts "编程完成: $dev"'
    )

    return await _safe_execute(session, tcl, 60.0, "编程设备失败")
