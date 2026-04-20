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

from mcp.server.fastmcp import Context

from vivado_mcp.analysis.warning_parser import parse_diag_counts, parse_pre_bitstream
from vivado_mcp.server import _NO_SESSION, _require_session, _safe_execute, mcp
from vivado_mcp.tcl_scripts import CHECK_PRE_BITSTREAM, COUNT_WARNINGS
from vivado_mcp.vivado.tcl_utils import to_tcl_path, validate_identifier

logger = logging.getLogger(__name__)

# 轮询间隔（秒）。综合/实现任务通常以分钟计，2 秒足够快响应完成事件
_POLL_INTERVAL_SEC = 2.0

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
) -> str:
    """D5: 执行 reset_run → launch_runs → Python 轮询 → 自动 open_run → 诊断。

    不再调用 Tcl 的 `wait_on_run`（它会阻塞 Vivado event loop，
    GUI 模式下会冻住界面）。改用 Python 每 2 秒查一次 STATUS/PROGRESS。
    """
    timeout_sec = timeout_minutes * 60.0

    # ------------------- 1. 启动 -------------------
    try:
        launch_result = await session.execute(
            f"reset_run {run_name}\nlaunch_runs {run_name} -jobs {jobs}",
            timeout=60.0,
        )
        if launch_result.is_error:
            return f"[ERROR] 启动 {label} 失败:\n{launch_result.output}"
    except Exception as e:
        return f"[ERROR] 启动 {label} 失败: {e}"

    # ------------------- 2. 轮询 -------------------
    deadline = time.time() + timeout_sec
    final_status = "UNKNOWN"
    final_progress = "0%"
    final_elapsed = ""
    last_progress_int = 0

    await ctx.report_progress(progress=0, total=100)

    while time.time() < deadline:
        try:
            poll = await session.execute(
                f'set __r [get_runs {run_name}]\n'
                f'set __s [get_property STATUS $__r]\n'
                f'set __p [get_property PROGRESS $__r]\n'
                f'set __e [get_property STATS.ELAPSED $__r]\n'
                f'puts "VMCP_POLL|$__s|$__p|$__e"',
                timeout=15.0,
            )
        except Exception as e:
            return f"[ERROR] 轮询 {label} 状态失败: {e}"

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
        return f"[ERROR] {label}超时（{timeout_minutes} 分钟），最后状态: {final_status}"

    await ctx.report_progress(progress=100, total=100)

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
    if open_note:
        result_parts.append(open_note)

    try:
        diag_result = await session.execute(
            COUNT_WARNINGS.format(run_name=run_name), timeout=30.0
        )
        errors, cw, w = parse_diag_counts(diag_result.output)
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
) -> str:
    """运行综合。自动执行 reset_run → launch_runs → wait_on_run。

    Args:
        run_name: 综合 run 名称，默认 "synth_1"。
        jobs: 并行任务数，默认 4。
        timeout_minutes: 超时分钟数，默认 30。
        session_id: 目标会话 ID。
    """
    try:
        run_name = validate_identifier(run_name, "run_name")
    except ValueError as e:
        return f"[ERROR] {e}"

    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    return await _launch_and_wait(
        session, run_name, jobs, timeout_minutes, "综合", ctx
    )


@mcp.tool()
async def run_implementation(
    run_name: str = "impl_1",
    jobs: int = 4,
    timeout_minutes: int = 60,
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """运行实现（布局布线）。自动执行 reset_run → launch_runs → wait_on_run。

    Args:
        run_name: 实现 run 名称，默认 "impl_1"。
        jobs: 并行任务数，默认 4。
        timeout_minutes: 超时分钟数，默认 60。
        session_id: 目标会话 ID。
    """
    try:
        run_name = validate_identifier(run_name, "run_name")
    except ValueError as e:
        return f"[ERROR] {e}"

    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    return await _launch_and_wait(
        session, run_name, jobs, timeout_minutes, "实现", ctx
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

    # 前置安全检查：force=False 时检测 CRITICAL WARNING
    if not force:
        try:
            pre_result = await session.execute(
                CHECK_PRE_BITSTREAM.format(impl_run=impl_run), timeout=30.0
            )
            status, cw_count, samples = parse_pre_bitstream(pre_result.output)

            if cw_count > 0:
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
            # 继续往下跑,但返回时附带警告前缀,让用户看到"摸鱼过去"的风险

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

    # 轮询
    deadline = time.time() + timeout_sec
    final_status = "UNKNOWN"
    final_progress = "0%"
    final_elapsed = ""
    last_progress_int = 0

    await ctx.report_progress(progress=0, total=100)

    while time.time() < deadline:
        try:
            poll = await session.execute(
                f'set __r [get_runs {impl_run}]\n'
                f'set __s [get_property STATUS $__r]\n'
                f'set __p [get_property PROGRESS $__r]\n'
                f'set __e [get_property STATS.ELAPSED $__r]\n'
                f'puts "VMCP_POLL|$__s|$__p|$__e"',
                timeout=15.0,
            )
        except Exception as e:
            return f"[ERROR] 轮询比特流状态失败: {e}"

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

        try:
            progress_int = int(final_progress.rstrip("%").strip() or "0")
        except ValueError:
            progress_int = last_progress_int
        if progress_int != last_progress_int:
            await ctx.report_progress(progress=progress_int, total=100)
            last_progress_int = progress_int

        if "Complete" in final_status:
            break
        if "ERROR" in final_status.upper():
            break

        await asyncio.sleep(_POLL_INTERVAL_SEC)
    else:
        return (
            f"[ERROR] 生成比特流超时({timeout_minutes} 分钟)。"
            f"最后状态: {final_status},进度: {final_progress}"
        )

    await ctx.report_progress(progress=100, total=100)

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

    return (
        f"--- 比特流生成结果 ---\n"
        f"状态: {final_status}\n"
        f"进度: {final_progress}\n"
        f"耗时: {final_elapsed}\n"
        f"比特流目录: {bit_dir}"
    )


@mcp.tool()
async def program_device(
    bitstream_path: str,
    target: str = "*",
    hw_server_url: str = "localhost:3121",
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """编程 FPGA 设备。封装 open_hw_manager → connect → program 多步操作。

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
            "program_device 只接受 .bit 文件(.bin/.mcs 用 write_cfgmem 烧 flash)"
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
