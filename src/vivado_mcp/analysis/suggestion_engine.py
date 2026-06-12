"""下一步建议引擎:根据 ProjectInfo 推断用户应该做什么。

用途:``get_next_suggestion`` 工具。新手在不同阶段卡住最常问的
"我下一步该干啥",用一张决策表直接告诉他。

决策顺序(自上而下,命中即停):
  1. 没打开项目 → open_project / create_project
  2. 没源文件 → add_files
  3. 没顶层 → set_property TOP
  4. 没 XDC → 添加约束
  4.5 有 testbench 且没跑过行为仿真 → 先 launch_simulation 验证逻辑
  5. 没综合 → xdc_lint + run_synthesis(无 testbench 时附补仿真提醒)
  6. 综合失败 / 有 ERROR → get_critical_warnings
  7. 综合完成但没实现 → run_implementation
  8. 实现失败(place/route ERROR) → get_critical_warnings
  9. 布线完成但没 bitstream → check_bitstream_readiness + generate_bitstream
 10. bitstream 已生成 → program_device
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from vivado_mcp.analysis.project_parser import ProjectInfo

logger = logging.getLogger(__name__)


@dataclass
class Suggestion:
    """单个阶段的建议动作。"""
    stage: str              # 决策表匹配到的阶段 key
    summary: str            # 一句话概括
    reasons: list[str] = field(default_factory=list)   # 为什么判断到这里
    actions: list[str] = field(default_factory=list)   # 具体可执行动作(工具名或 Tcl)


def _has_top(info: ProjectInfo) -> bool:
    return bool(info.top) and info.top.strip() not in ("", "(未设置)")


def _source_count(info: ProjectInfo) -> int:
    return sum(1 for f in info.files if f.category == "source")


def _xdc_count(info: ProjectInfo) -> int:
    return sum(1 for f in info.files if f.category == "xdc")


def _sim_count(info: ProjectInfo) -> int:
    """sim_1 fileset 独有文件数(QUERY_PROJECT_INFO 已剔除继承自 sources_1 的)。"""
    return sum(1 for f in info.files if f.category == "sim")


def _has_sim_products(info: ProjectInfo) -> bool:
    """检查 <project_dir>/<project_name>.sim 下是否已有行为仿真产物。

    Vivado 默认 layout:跑过 launch_simulation 后生成 <proj>.sim/<fileset>/behav/。
    session 即本机 Vivado,目录本机可达,纯 os 检查即可,不依赖 session。
    """
    if not info.project_dir or not info.project_name:
        return False
    sim_root = Path(info.project_dir) / f"{info.project_name}.sim"
    try:
        return any(sim_root.glob("*/behav"))
    except OSError as e:
        # 目录不可读按"无产物"处理,但要留诊断痕迹(错误处理铁律)
        logger.warning("检查仿真产物目录失败(%s),按无产物处理: %s", sim_root, e)
        return False


def suggest_next(info: ProjectInfo) -> Suggestion:
    """根据项目当前状态推导下一步建议。"""
    # 规则 1: 没项目
    if info.error or not info.project_name:
        return Suggestion(
            stage="no_project",
            summary="当前没有打开的 Vivado 项目。",
            reasons=[
                f"query_project_info 返回: {info.error or '项目名为空'}",
            ],
            actions=[
                "已有 .xpr 文件 → run_tcl(\"open_project <path/to/proj.xpr>\")",
                "新建项目 → safe_tcl(\"create_project {0} {1} -part {2}\", "
                "args=[name, dir, part_id])",
            ],
        )

    src_n = _source_count(info)
    xdc_n = _xdc_count(info)

    # 规则 2: 没源文件
    if src_n == 0:
        return Suggestion(
            stage="no_source",
            summary="项目已打开但还没源文件。",
            reasons=[f"项目 '{info.project_name}' 的 sources_1 为空"],
            actions=[
                "用 Write 工具创建 .v/.sv 文件(例:top.v),然后:",
                "  run_tcl(\"add_files -fileset sources_1 {path}\")",
                "  run_tcl(\"update_compile_order -fileset sources_1\")",
            ],
        )

    # 规则 3: 没顶层
    if not _has_top(info):
        return Suggestion(
            stage="no_top",
            summary="未设置顶层模块。",
            reasons=[f"共 {src_n} 个源文件但 TOP 属性为空"],
            actions=[
                "run_tcl(\"set_property TOP <模块名> [current_fileset]\")",
                "run_tcl(\"update_compile_order -fileset sources_1\")",
            ],
        )

    # 规则 4: 没 XDC
    if xdc_n == 0:
        return Suggestion(
            stage="no_xdc",
            summary="还没添加 XDC 约束文件。",
            reasons=[f"顶层 '{info.top}' 已设但 constrs_1 为空"],
            actions=[
                "从板卡厂商官方仓库拷贝 .xdc(如 Basys3-Master.xdc / Nexys4DDR-Master.xdc),",
                "或用 Write 工具手写,然后:",
                "  run_tcl(\"add_files -fileset constrs_1 {path/to/board.xdc}\")",
            ],
        )

    synth = info.synth_status or ""
    impl = info.impl_status or ""

    sim_n = _sim_count(info)

    # 规则 4.5: 有 testbench 且没跑过行为仿真 → 先仿真验证逻辑再综合
    # (FPGA 基本纪律:烧完板才发现逻辑错的代价远高于先仿真)
    if (
        "Complete" not in synth
        and "ERROR" not in synth.upper()
        and sim_n > 0
        and not _has_sim_products(info)
    ):
        return Suggestion(
            stage="ready_to_sim",
            summary="发现 testbench 且尚未跑过行为仿真,建议先仿真验证逻辑再综合。",
            reasons=[
                f"sim_1 fileset 有 {sim_n} 个文件,但 "
                f"{info.project_name}.sim 下无行为仿真产物",
                f"synth_1 状态: {synth or '未启动'}",
            ],
            actions=[
                "run_tcl(\"launch_simulation\")  # 行为仿真(XSim)",
                "run_tcl(\"run 1us\")  # 跑一段仿真时间,观察波形/输出",
                "失败诊断用 get_critical_warnings(run_name='sim_1')",
                "仿真通过后再 run_synthesis()",
            ],
        )

    # 规则 5: 没综合
    if "Complete" not in synth and "ERROR" not in synth.upper():
        actions = [
            "xdc_lint()  # 先跑纯 Python 静态 XDC 检查(< 1s),捕捉低级错误",
            "run_synthesis()  # 正式综合(5-15 分钟,取决于设计规模)",
        ]
        if sim_n == 0:
            actions.append(
                "提醒: 未发现 testbench(sim_1 为空),建议先补行为仿真验证逻辑"
                "(add_files -fileset sim_1 tb.v 后 launch_simulation)。"
            )
        return Suggestion(
            stage="ready_to_synth",
            summary="源文件 + 顶层 + XDC 已就绪,可以综合。",
            reasons=[f"synth_1 状态: {synth or '未启动'}"],
            actions=actions,
        )

    # 规则 6: 综合失败
    if "ERROR" in synth.upper():
        return Suggestion(
            stage="synth_failed",
            summary="综合失败,需要先看 ERROR。",
            reasons=[f"synth_1 状态: {synth}"],
            actions=[
                "get_critical_warnings(run_name='synth_1')  # 会自动展开 ERROR 详情",
                "修复 RTL / XDC 问题后 reset_run synth_1 再 run_synthesis()",
            ],
        )

    # 综合完成
    # 规则 7: 没实现
    if "Complete" in synth and not impl:
        return Suggestion(
            stage="ready_to_impl",
            summary="综合完成,可以开始实现(place + route)。",
            reasons=[f"synth_1: {synth}, impl_1: (无 run)"],
            actions=[
                "get_utilization_report()  # 确认资源占用 < 90%(避免布线拥塞)",
                "run_implementation()  # place_design + route_design(10-30 分钟)",
            ],
        )

    if "Complete" in synth and "Not started" in impl:
        return Suggestion(
            stage="ready_to_impl",
            summary="综合完成,impl_1 尚未启动。",
            reasons=[f"synth_1: {synth}, impl_1: {impl}"],
            actions=[
                "get_utilization_report()",
                "run_implementation()",
            ],
        )

    # 规则 8: 实现失败
    if "ERROR" in impl.upper():
        return Suggestion(
            stage="impl_failed",
            summary="实现失败(place 或 route 阶段)。",
            reasons=[f"impl_1 状态: {impl}"],
            actions=[
                "get_critical_warnings(run_name='impl_1')  # 看 ERROR + CW",
                "常见原因: XDC 引脚冲突 / 时序不收敛 / 资源超限",
                "修复后 reset_run impl_1 再 run_implementation()",
            ],
        )

    # 规则 9: route 完成
    if "route_design Complete" in impl or "write_bitstream Complete" in impl:
        if "write_bitstream Complete" in impl:
            # 规则 10: bitstream 已生成
            return Suggestion(
                stage="ready_to_program",
                summary="比特流已生成,可以烧板。",
                reasons=[f"impl_1: {impl}"],
                actions=[
                    f"program_device(bitstream_path='{info.project_dir}/"
                    f"{info.project_name}.runs/impl_1/{info.top}.bit')",
                ],
            )
        return Suggestion(
            stage="ready_to_bitstream",
            summary="布线完成,可以生成比特流。",
            reasons=[f"impl_1: {impl}"],
            actions=[
                "get_timing_report()          # 确认 WNS >= 0",
                "check_bitstream_readiness()  # 综合判定 READY/WARN/BLOCK",
                "generate_bitstream()         # 输出 .bit 文件",
            ],
        )

    # 规则 11: place 完成但 route 未完成 / 在跑中
    if "place_design Complete" in impl or "Running" in impl:
        return Suggestion(
            stage="impl_running",
            summary=f"实现正在进行中: {impl}",
            reasons=["impl_1 仍在 place 或 route 阶段"],
            actions=[
                "get_run_progress('impl_1')  # 查看当前 Phase 和已用时",
                "等到 route_design Complete 再看 get_timing_report",
            ],
        )

    # 兜底
    return Suggestion(
        stage="unknown",
        summary="无法推断明确的下一步,建议手动检查状态。",
        reasons=[f"synth_1: {synth}", f"impl_1: {impl}"],
        actions=[
            "get_project_info()  # 看完整项目信息",
            "get_run_progress('synth_1' 或 'impl_1')",
        ],
    )


def format_suggestion(info: ProjectInfo, suggestion: Suggestion) -> str:
    """人类可读建议。"""
    out: list[str] = [f"=== 下一步建议: {suggestion.summary} ==="]

    if info.project_name:
        out.append(f"  项目: {info.project_name}  |  part: {info.part or '(未知)'}")
    if info.top:
        out.append(f"  顶层: {info.top}")
    if info.synth_status:
        out.append(f"  综合: {info.synth_status}")
    if info.impl_status:
        out.append(f"  实现: {info.impl_status}")

    out.append("")
    out.append(f"[阶段] {suggestion.stage}")
    if suggestion.reasons:
        out.append("[判断依据]")
        for r in suggestion.reasons:
            out.append(f"  - {r}")

    out.append("")
    out.append("[建议动作]")
    for a in suggestion.actions:
        out.append(f"  {a}")

    return "\n".join(out)
