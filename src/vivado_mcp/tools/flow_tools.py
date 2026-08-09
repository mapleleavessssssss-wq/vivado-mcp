"""设计流程工具。

run_synthesis / run_implementation / generate_bitstream / program_device。
封装 Vivado 长时间运行的操作，提供超时管理和进度反馈。
综合/实现完成后自动执行警告诊断，bitstream 生成前自动安全检查。

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
)
from vivado_mcp.vivado.tcl_utils import to_tcl_path, validate_identifier

logger = logging.getLogger(__name__)

# 轮询间隔（秒）。综合/实现任务通常以分钟计，2 秒足够快响应完成事件
_POLL_INTERVAL_SEC = 2.0

# --------------------------------------------------------------------------- #
#  内部辅助：综合 / 实现 / bitstream 共享的轮询逻辑(单一来源,勿复制)
# --------------------------------------------------------------------------- #

async def _poll_run_until_done(
    session,
    run_name: str,
    timeout_sec: float,
    ctx: Context,
) -> tuple[str, str, str, str]:
    """每 2s 轮询 run STATUS/PROGRESS 直到终态(Complete/ERROR)或超时。

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

        # 终态判断：Complete! 表示成功；ERROR 表示失败；其余继续轮询
        if "Complete" in final_status:
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
    wait: bool = True,
) -> str:
    """原子启动 run；按 wait 选择立即返回或轮询、open_run、诊断。

    不再调用 Tcl 的 `wait_on_run`（它会阻塞 Vivado event loop，
    GUI 模式下会冻住界面）。wait=True 时改用 Python 每 2 秒查一次
    STATUS/PROGRESS；wait=False 时返回 job_id，由 get_run_progress 查询。
    """
    timeout_sec = timeout_minutes * 60.0

    # ------------------- 0. PRD B4:读取实际生效的参数覆盖 -------------------
    # 在 reset_run 之前查 generic / verilog_define,结果里明示,
    # 防止"以为 set_property generic 生效了实际没设上"的隐性坑。
    override_lines = await _query_fileset_overrides(session)

    # ------------------- 1. 启动 -------------------
    try:
        launch_result = await session.execute(
            LAUNCH_RUN_IF_IDLE.format(run_name=run_name, jobs=jobs), timeout=60.0
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
    if launch_state != "started":
        return f"[ERROR] 启动 {label} 失败: 未知启动状态 {launch_state!r}"

    if not wait:
        result_parts = [
            f"{label}已异步启动。",
            f"job_id: {session.session_id}:{run_name}",
            f"状态: {launch_status or '已提交'}",
            (
                f"查询: get_run_progress(run_name='{run_name}', "
                f"session_id='{session.session_id}')"
            ),
        ]
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

    # ------------------- 3. B4 修复：自动 open_run -------------------
    # 综合/实现完成后自动打开设计，让紧随其后的 report_* / report_io 能工作。
    # catch 保护：run 可能已经打开（无害），或有其他运行时错误
    # 注意:catch 吞异常后外层 return_code=0,所以不能只看 is_error。
    # 必须把 $__open_err 的内容 puts 出来,Python 侧检测 VMCP_OPEN_ERR: 前缀。
    open_note = ""
    if "Complete" in final_status and "ERROR" not in final_status.upper():
        try:
            open_result = await session.execute(
                f"if {{[catch {{ open_run {run_name} }} __open_err]}} "
                f'{{ puts "VMCP_OPEN_ERR:$__open_err" }}',
                timeout=120.0,
            )
            # 外层 is_error (Tcl 语法错等) 和内层 VMCP_OPEN_ERR 都要看
            err_line = next(
                (ln for ln in open_result.output.splitlines()
                 if ln.startswith("VMCP_OPEN_ERR:")),
                None,
            )
            if open_result.is_error:
                open_note = f"(open_run 自动打开失败: {open_result.output[:200]})"
            elif err_line:
                inner = err_line[len("VMCP_OPEN_ERR:"):].strip()
                # "already open" 这类无害信息不告警
                if "already" not in inner.lower():
                    open_note = f"(open_run 返回错误: {inner[:200]})"
        except Exception as e:
            open_note = f"(open_run 自动打开异常: {e})"

    # ------------------- 4. 诊断概览 -------------------
    result_parts: list[str] = [
        f"--- {label}结果 ---",
        f"状态: {final_status}",
        f"进度: {final_progress}",
        f"耗时: {final_elapsed}",
    ]
    result_parts.extend(override_lines)
    if open_note:
        result_parts.append(open_note)

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

@mcp.tool()
async def run_synthesis(
    run_name: str = "synth_1",
    jobs: int = 4,
    timeout_minutes: int = 30,
    session_id: str = "default",
    ctx: Context = None,
    wait: bool = True,
) -> str:
    """启动综合；默认等待完成，也可异步提交后查询状态。

    不调用 Tcl wait_on_run(会阻塞 Vivado event loop,GUI 模式冻住界面);
    wait=True 时 Python 每 2 秒查一次状态并上报进度；wait=False 时立即
    返回 job_id，随后用 get_run_progress 查询。

    Args:
        run_name: 综合 run 名称，默认 "synth_1"。
        jobs: 并行任务数，默认 4。
        timeout_minutes: 超时分钟数，默认 30。
        session_id: 目标会话 ID。
        wait: True 等待完成并诊断；False 启动后立即返回 job_id。
    """
    try:
        run_name = validate_identifier(run_name, "run_name")
    except ValueError as e:
        return f"[ERROR] {e}"

    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    return await _launch_and_wait(
        session, run_name, jobs, timeout_minutes, "综合", ctx, wait=wait
    )


@mcp.tool()
async def run_implementation(
    run_name: str = "impl_1",
    jobs: int = 4,
    timeout_minutes: int = 60,
    session_id: str = "default",
    ctx: Context = None,
    wait: bool = True,
) -> str:
    """启动实现（布局布线）；默认等待完成，也可异步提交。

    不调用 Tcl wait_on_run(会阻塞 Vivado event loop,GUI 模式冻住界面);
    wait=True 时 Python 每 2 秒查一次 STATUS/PROGRESS；wait=False 时立即
    返回 job_id，随后用 get_run_progress 查询。

    Args:
        run_name: 实现 run 名称，默认 "impl_1"。
        jobs: 并行任务数，默认 4。
        timeout_minutes: 超时分钟数，默认 60。
        session_id: 目标会话 ID。
        wait: True 等待完成并诊断；False 启动后立即返回 job_id。
    """
    try:
        run_name = validate_identifier(run_name, "run_name")
    except ValueError as e:
        return f"[ERROR] {e}"

    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    return await _launch_and_wait(
        session, run_name, jobs, timeout_minutes, "实现", ctx, wait=wait
    )


@mcp.tool()
async def generate_bitstream(
    impl_run: str = "impl_1",
    jobs: int = 4,
    timeout_minutes: int = 30,
    force: bool = False,
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """生成比特流文件。在实现完成后执行。

    默认启用前置安全检查：检测 CRITICAL WARNING 后阻止生成，
    需确认无风险后使用 force=True 跳过检查。

    Args:
        impl_run: 实现 run 名称，默认 "impl_1"。
        jobs: 并行任务数，默认 4。
        timeout_minutes: 超时分钟数，默认 30。
        force: 跳过 CRITICAL WARNING 安全检查，默认 False。
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
    # 检查本身失败时不拦截,但必须降级为显式 [DEGRADED](追加进最终返回),
    # 不允许静默放行 —— 否则"未布线/日志不可读"这类信号会被吞掉。
    precheck_warn = ""
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
                logger.warning("bitstream 前置安全检查未能执行: %s", reason)
                precheck_warn = (
                    f"[DEGRADED] 前置 CW 安全检查未能执行: {reason}。"
                    "本次生成未经 CRITICAL WARNING 门禁。"
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
            # 安全检查本身失败不应阻塞——降级为跳过检查。但一定要告诉用户:
            # 否则"未布线"这种致命信号会被静默吞,用户以为一切正常继续跑。
            logger.warning(
                "bitstream 前置安全检查失败,降级跳过: %s: %s",
                type(e).__name__, e,
            )
            precheck_warn = (
                f"[DEGRADED] 前置 CW 安全检查失败: {type(e).__name__}: {e}。"
                "本次生成未经 CRITICAL WARNING 门禁。"
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

    # 轮询(与 _launch_and_wait 共用 _poll_run_until_done,单一来源)
    try:
        outcome, final_status, final_progress, final_elapsed = (
            await _poll_run_until_done(session, impl_run, timeout_sec, ctx)
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
    if precheck_warn:
        # 安全门检查失败时的显式降级标记:不拦截,但必须让 AI/用户看见
        result_text += f"\n{precheck_warn}"
    return result_text


@mcp.tool()
async def program_device(
    bitstream_path: str,
    target: str = "*",
    hw_server_url: str = "localhost:3121",
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
        target: 目标设备过滤器，默认 "*"（第一个可用设备）。
        hw_server_url: 硬件服务器地址，默认 "localhost:3121"。
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
    if not hw_server_url or any(c in _TCL_UNSAFE or c == "*" for c in hw_server_url):
        return (
            f"[ERROR] hw_server_url 为空或含非法字符: {hw_server_url!r}。"
            "应形如 'localhost:3121' 或 '<主机名/IP>:<端口>'"
        )

    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    bit_tcl = to_tcl_path(bitstream_path)

    tcl = (
        f'open_hw_manager\n'
        f'connect_hw_server -url {hw_server_url}\n'
        f'open_hw_target [lindex [get_hw_targets {target}] 0]\n'
        f'set dev [lindex [get_hw_devices] 0]\n'
        f'current_hw_device $dev\n'
        f'set_property PROGRAM.FILE {bit_tcl} $dev\n'
        f'program_hw_devices $dev\n'
        f'puts "编程完成: $dev"'
    )

    return await _safe_execute(session, tcl, 60.0, "编程设备失败")
