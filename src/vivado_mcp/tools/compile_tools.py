"""Compile profile inspection and explicit incremental-flow configuration."""

from __future__ import annotations

import json
import os
from pathlib import Path

from mcp.server.mcpserver import Context

from vivado_mcp.analysis.compile_profile import CompileProfile, parse_compile_profile
from vivado_mcp.server import _NO_SESSION, _require_session, mcp
from vivado_mcp.tools.annotations import PROJECT_WRITE, READ_ONLY_SESSION
from vivado_mcp.vivado.tcl_utils import tcl_quote, validate_identifier

_RUN_PROPERTIES = (
    "STATUS",
    "PROGRESS",
    "NEEDS_REFRESH",
    "STRATEGY",
    "REPORT_STRATEGY",
    "DIRECTORY",
    "AUTO_INCREMENTAL_CHECKPOINT",
    "INCREMENTAL_CHECKPOINT",
    "STATS.WNS",
    "STATS.TNS",
    "STATS.WHS",
    "STATS.THS",
    "STATS.ELAPSED",
)


def build_compile_profile_query(run_names: list[str]) -> str:
    """Build one Tcl 8.5-compatible query for project, process and run state."""
    run_list = " ".join(tcl_quote(name) for name in run_names)
    prop_list = " ".join(tcl_quote(name) for name in _RUN_PROPERTIES)
    return f"""
set __vmcp_projects [get_projects -quiet]
if {{[llength $__vmcp_projects] != 1}} {{
    puts "VMCP_PROFILE:error=expected_one_open_project_actual_[llength $__vmcp_projects]"
}} else {{
    set __vmcp_project [current_project]
    set __vmcp_name [get_property NAME $__vmcp_project]
    set __vmcp_dir [get_property DIRECTORY $__vmcp_project]
    set __vmcp_xpr [file normalize [file join $__vmcp_dir "${{__vmcp_name}}.xpr"]]
    set __vmcp_part [get_property PART $__vmcp_project]
    set __vmcp_top [get_property TOP [get_filesets sources_1]]
    set __vmcp_threads ""
    catch {{set __vmcp_threads [get_param general.maxThreads]}}
    puts "VMCP_PROFILE:project_name=$__vmcp_name"
    puts "VMCP_PROFILE:project_dir=$__vmcp_dir"
    puts "VMCP_PROFILE:xpr_path=$__vmcp_xpr"
    puts "VMCP_PROFILE:part=$__vmcp_part"
    puts "VMCP_PROFILE:top=$__vmcp_top"
    puts "VMCP_PROFILE:vivado_version=[version -short]"
    puts "VMCP_PROFILE:tcl_patchlevel=[info patchlevel]"
    puts "VMCP_PROFILE:general_max_threads=$__vmcp_threads"

    foreach __vmcp_run_name [list {run_list}] {{
        set __vmcp_runs [get_runs -quiet $__vmcp_run_name]
        if {{[llength $__vmcp_runs] != 1}} {{
            puts "VMCP_PROFILE_RUN:$__vmcp_run_name|found=0"
        }} else {{
            set __vmcp_run [lindex $__vmcp_runs 0]
            puts "VMCP_PROFILE_RUN:$__vmcp_run_name|found=1"
            foreach __vmcp_prop [list {prop_list}] {{
                set __vmcp_value ""
                catch {{set __vmcp_value [get_property $__vmcp_prop $__vmcp_run]}}
                puts "VMCP_PROFILE_RUN:$__vmcp_run_name|$__vmcp_prop=$__vmcp_value"
            }}
        }}
    }}
}}
""".strip()


def _artifact_summary(profile: CompileProfile) -> dict[str, dict[str, object]]:
    """List small metadata for existing run reports/checkpoints without reading them."""
    artifacts: dict[str, dict[str, object]] = {}
    for run_name, run in profile.runs.items():
        record: dict[str, object] = {"reports": [], "checkpoints": []}
        run_dir = Path(run.directory)
        if run.directory and run_dir.is_dir():
            try:
                reports = sorted(
                    run_dir.glob("*.rpt"),
                    key=lambda path: path.stat().st_mtime,
                    reverse=True,
                )[:20]
                checkpoints = sorted(
                    run_dir.glob("*.dcp"),
                    key=lambda path: path.stat().st_mtime,
                    reverse=True,
                )[:20]
                record["reports"] = [str(path.resolve()) for path in reports]
                record["checkpoints"] = [str(path.resolve()) for path in checkpoints]
            except OSError as exc:
                record["artifact_error"] = f"{type(exc).__name__}: {exc}"
        artifacts[run_name] = record
    return artifacts


def _compile_recommendations(profile: CompileProfile) -> list[str]:
    recommendations = [
        "launch_runs -jobs 表示并行 run 槽位，不等于单个 run 的 CPU 线程数。",
        "不要自动修改 general.maxThreads；先验证 project run 子进程的实际继承值。",
    ]
    cpu_count = os.cpu_count()
    if profile.max_threads is not None and cpu_count is not None:
        recommendations.append(
            f"当前进程 general.maxThreads={profile.max_threads}，"
            f"Python 看到 logical_cpu_count={cpu_count}；子 run 仍需独立证据。"
        )
    for run in profile.runs.values():
        if run.is_complete and not run.is_out_of_date:
            recommendations.append(
                f"{run.name} 已完成且未显示过期；再次 launch 前应返回 UP_TO_DATE。"
            )
        report_strategy = run.report_strategy.lower()
        if any(
            token in report_strategy
            for token in ("timing closure", "ultrafast", "explore")
        ):
            recommendations.append(
                f"{run.name} report_strategy={run.report_strategy!r} 可能包含额外报告；"
                "保留工程设置，但将报告耗时与核心实现耗时分开记录。"
            )
    return recommendations


@mcp.tool(annotations=READ_ONLY_SESSION)
async def get_compile_profile(
    synth_run: str = "synth_1",
    impl_run: str = "impl_1",
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """一次只读查询编译线程、工程身份、run、报告和增量设置。

    该工具不启动 run、不打开 design、不生成报告，也不修改 project。``jobs`` 在
    Vivado Project Mode 中是并行 run 槽位；本工具同时返回当前进程的
    ``general.maxThreads``，但不会假定它已被编译子进程继承。
    """
    try:
        synth_run = validate_identifier(synth_run, "synth_run")
        impl_run = validate_identifier(impl_run, "impl_run")
    except ValueError as exc:
        return f"[ERROR] {exc}"

    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    try:
        result = await session.execute(
            build_compile_profile_query([synth_run, impl_run]),
            timeout=30.0,
        )
    except Exception as exc:
        return f"[ERROR] 获取 compile profile 失败: {type(exc).__name__}: {exc}"
    if result.is_error:
        return f"[ERROR] 获取 compile profile 失败(rc={result.return_code}):\n{result.output}"

    profile = parse_compile_profile(result.output)
    if profile.error:
        return f"[ERROR] compile profile 不可用: {profile.error}"
    payload = profile.to_dict()
    payload["logical_cpu_count"] = os.cpu_count()
    payload["jobs_semantics"] = "parallel run slots; not threads inside one run"
    payload["artifacts"] = _artifact_summary(profile)
    payload["recommendations"] = _compile_recommendations(profile)
    return json.dumps(payload, ensure_ascii=False, indent=2)


@mcp.tool(annotations=PROJECT_WRITE)
async def configure_incremental_compile(
    run_name: str = "impl_1",
    mode: str = "automatic",
    apply: bool = False,
    expected_xpr_path: str = "",
    expected_project_name: str = "",
    expected_part: str = "",
    expected_top: str = "",
    expected_vivado_version: str = "",
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """检查或显式配置 Project Mode automatic incremental implementation。

    ``apply=False``（默认）只返回 PLAN_ONLY：实际工程身份、run 状态、参考 DCP、
    当前属性及本版本是否支持 ``AUTO_INCREMENTAL_CHECKPOINT``。``apply=True`` 才
    设置该 run 的属性，并要求完整 `.xpr`、project、part、top、Vivado version
    全部匹配。不会 reset 或 launch run，也不会选择参考 checkpoint。

    ``mode`` 仅允许 ``automatic`` 或 ``disabled``。增量复用收益取决于实际 reuse
    与基线 QoR；本工具不把“已启用”写成“必然更快”。
    """
    try:
        run_name = validate_identifier(run_name, "run_name")
    except ValueError as exc:
        return f"[ERROR] {exc}"
    if mode not in {"automatic", "disabled"}:
        return "[ERROR] mode 仅允许 automatic / disabled。"

    xpr_path: Path | None = None
    if apply:
        xpr_path = Path(expected_xpr_path).expanduser().resolve()
        if xpr_path.suffix.lower() != ".xpr" or not xpr_path.is_file():
            return f"[ERROR] expected_xpr_path 不是现存 .xpr 文件: {xpr_path}"
        required = {
            "expected_project_name": expected_project_name,
            "expected_part": expected_part,
            "expected_top": expected_top,
            "expected_vivado_version": expected_vivado_version,
        }
        for label, value in required.items():
            if not value.strip():
                return f"[ERROR] apply=True 时 {label} 不能为空。"

    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    expected_xpr = xpr_path.as_posix() if xpr_path is not None else ""
    desired = 1 if mode == "automatic" else 0
    tcl = f"""
set __vmcp_apply {1 if apply else 0}
set __vmcp_desired {desired}
set __vmcp_expected_xpr {tcl_quote(expected_xpr)}
set __vmcp_expected_name {tcl_quote(expected_project_name)}
set __vmcp_expected_part {tcl_quote(expected_part)}
set __vmcp_expected_top {tcl_quote(expected_top)}
set __vmcp_expected_version {tcl_quote(expected_vivado_version)}
set __vmcp_projects [get_projects -quiet]
if {{[llength $__vmcp_projects] != 1}} {{
    error "Expected exactly one open project, found [llength $__vmcp_projects]"
}}
set __vmcp_project [current_project]
set __vmcp_name [get_property NAME $__vmcp_project]
set __vmcp_dir [get_property DIRECTORY $__vmcp_project]
set __vmcp_xpr [file normalize [file join $__vmcp_dir "${{__vmcp_name}}.xpr"]]
set __vmcp_part [get_property PART $__vmcp_project]
set __vmcp_top [get_property TOP [get_filesets sources_1]]
set __vmcp_version [version -short]
set __vmcp_runs [get_runs -quiet {run_name}]
if {{[llength $__vmcp_runs] != 1}} {{
    error "Run not found or ambiguous: {run_name}"
}}
set __vmcp_run [lindex $__vmcp_runs 0]
set __vmcp_props [list_property $__vmcp_run]
set __vmcp_supported [expr {{
    [lsearch -exact $__vmcp_props AUTO_INCREMENTAL_CHECKPOINT] >= 0
}}]
set __vmcp_current ""
if {{$__vmcp_supported}} {{
    catch {{set __vmcp_current [get_property AUTO_INCREMENTAL_CHECKPOINT $__vmcp_run]}}
}}
set __vmcp_status [get_property STATUS $__vmcp_run]
set __vmcp_run_dir [get_property DIRECTORY $__vmcp_run]
set __vmcp_dcps [glob -nocomplain -directory $__vmcp_run_dir *.dcp]

if {{$__vmcp_apply}} {{
    if {{![string equal -nocase $__vmcp_xpr [file normalize $__vmcp_expected_xpr]]}} {{
        error "XPR mismatch: actual=$__vmcp_xpr expected=$__vmcp_expected_xpr"
    }}
    if {{$__vmcp_name ne $__vmcp_expected_name}} {{
        error "Project mismatch: actual=$__vmcp_name expected=$__vmcp_expected_name"
    }}
    if {{$__vmcp_part ne $__vmcp_expected_part}} {{
        error "Part mismatch: actual=$__vmcp_part expected=$__vmcp_expected_part"
    }}
    if {{$__vmcp_top ne $__vmcp_expected_top}} {{
        error "Top mismatch: actual=$__vmcp_top expected=$__vmcp_expected_top"
    }}
    if {{$__vmcp_version ne $__vmcp_expected_version
            && ![string match "${{__vmcp_expected_version}}_AR*" $__vmcp_version]}} {{
        error "Vivado version mismatch: actual=$__vmcp_version expected=$__vmcp_expected_version"
    }}
    if {{!$__vmcp_supported}} {{
        error "AUTO_INCREMENTAL_CHECKPOINT is not supported by this run/release"
    }}
    set_property AUTO_INCREMENTAL_CHECKPOINT $__vmcp_desired $__vmcp_run
    set __vmcp_current [get_property AUTO_INCREMENTAL_CHECKPOINT $__vmcp_run]
}}

puts "VMCP_INCREMENTAL:mode=[expr {{$__vmcp_apply ? \"APPLY\" : \"PLAN_ONLY\"}}]"
puts "VMCP_INCREMENTAL:xpr=$__vmcp_xpr"
puts "VMCP_INCREMENTAL:project=$__vmcp_name"
puts "VMCP_INCREMENTAL:part=$__vmcp_part"
puts "VMCP_INCREMENTAL:top=$__vmcp_top"
puts "VMCP_INCREMENTAL:vivado=$__vmcp_version"
puts "VMCP_INCREMENTAL:run={run_name}"
puts "VMCP_INCREMENTAL:status=$__vmcp_status"
puts "VMCP_INCREMENTAL:supported=$__vmcp_supported"
puts "VMCP_INCREMENTAL:current=$__vmcp_current"
puts "VMCP_INCREMENTAL:desired=$__vmcp_desired"
puts "VMCP_INCREMENTAL:checkpoint_count=[llength $__vmcp_dcps]"
foreach __vmcp_dcp $__vmcp_dcps {{ puts "VMCP_INCREMENTAL_DCP:$__vmcp_dcp" }}
""".strip()

    try:
        result = await session.execute(tcl, timeout=30.0)
    except Exception as exc:
        return f"[ERROR] incremental profile 失败: {type(exc).__name__}: {exc}"
    if result.is_error:
        return f"[ERROR] incremental profile 失败(rc={result.return_code}):\n{result.output}"
    return result.output
