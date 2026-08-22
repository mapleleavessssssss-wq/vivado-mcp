"""Project synchronization tools for an already-open Vivado session."""

from pathlib import Path

from mcp.server.mcpserver import Context

from vivado_mcp.server import _NO_SESSION, _require_session, _safe_execute, mcp
from vivado_mcp.tools.annotations import PROJECT_WRITE
from vivado_mcp.vivado.tcl_utils import tcl_quote, validate_identifier

_FILESET_EXTENSIONS = {
    "sources_1": {
        ".v",
        ".sv",
        ".vh",
        ".svh",
        ".vhd",
        ".vhdl",
        ".xci",
        ".bd",
        ".mem",
        ".coe",
    },
    "sim_1": {
        ".v",
        ".sv",
        ".vh",
        ".svh",
        ".vhd",
        ".vhdl",
        ".mem",
        ".coe",
    },
    "constrs_1": {".xdc"},
}


def _normalize_sync_paths(file_paths: list[str], fileset: str) -> tuple[list[Path], str]:
    """Validate local files before any Tcl is sent to Vivado."""
    if fileset not in _FILESET_EXTENSIONS:
        return [], (
            f"[ERROR] fileset={fileset!r} 不受支持。仅允许: {', '.join(_FILESET_EXTENSIONS)}。"
        )
    if not file_paths:
        return [], "[ERROR] file_paths 不能为空。"

    allowed = _FILESET_EXTENSIONS[fileset]
    normalized: list[Path] = []
    seen: set[str] = set()
    for raw_path in file_paths:
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file():
            return [], f"[ERROR] 待同步文件不存在或不是普通文件: {path}"
        if path.suffix.lower() not in allowed:
            return [], (
                f"[ERROR] 文件类型 {path.suffix or '<none>'} 不能加入 {fileset}: {path}。"
                f"允许扩展名: {', '.join(sorted(allowed))}。"
            )
        key = str(path).casefold()
        if key not in seen:
            normalized.append(path)
            seen.add(key)
    return normalized, ""


@mcp.tool(annotations=PROJECT_WRITE)
async def sync_project_files(
    file_paths: list[str],
    expected_xpr_path: str,
    expected_project_name: str,
    expected_part: str,
    expected_top: str,
    expected_vivado_version: str,
    fileset: str = "sources_1",
    apply: bool = False,
    session_id: str = "default",
    timeout: int = 120,
    ctx: Context = None,
) -> str:
    """将新增文件安全同步到当前 Vivado GUI 工程。

    工具先在本地验证文件存在和扩展名，再在同一段 Tcl 中核对当前工程的完整
    ``.xpr`` 路径、工程名、器件、顶层和 Vivado 版本。任一身份不匹配都会在
    ``add_files`` 之前报错，防止多 Vivado 实例时把文件加入错误工程。

    ``apply=False``（默认）只预览：列出已存在和待添加文件，不修改工程。
    ``apply=True`` 才执行 ``add_files``，并对 ``sources_1``/``sim_1`` 调用
    ``update_compile_order``，当前 GUI 的 Sources/Hierarchy 会立即更新。

    这是工程状态写操作。调用 ``apply=True`` 前必须向用户报告并确认目标工程
    身份，且获得明确批准。

    Args:
        file_paths: 本次明确要同步的文件绝对路径；不递归扫描目录。
        expected_xpr_path: 用户指定工程的完整 .xpr 绝对路径。
        expected_project_name: 预期工程名。
        expected_part: 预期 FPGA part。
        expected_top: 预期设计顶层。
        expected_vivado_version: ``version -short`` 预期值，如 ``2024.2``。
        fileset: ``sources_1``、``sim_1`` 或 ``constrs_1``。
        apply: False 仅预检，True 执行同步。
        session_id: 已连接的 Vivado MCP 会话。
        timeout: Tcl 超时秒数。
    """
    normalized, error = _normalize_sync_paths(file_paths, fileset)
    if error:
        return error

    xpr_path = Path(expected_xpr_path).expanduser().resolve()
    if xpr_path.suffix.lower() != ".xpr" or not xpr_path.is_file():
        return f"[ERROR] expected_xpr_path 不是现存 .xpr 文件: {xpr_path}"

    expected_values = {
        "expected_project_name": expected_project_name,
        "expected_part": expected_part,
        "expected_top": expected_top,
        "expected_vivado_version": expected_vivado_version,
    }
    for label, value in expected_values.items():
        if not value.strip():
            return f"[ERROR] {label} 不能为空。"

    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    path_list = " ".join(tcl_quote(path.as_posix()) for path in normalized)
    tcl = f"""
set __vmcp_expected_xpr {tcl_quote(xpr_path.as_posix())}
set __vmcp_expected_name {tcl_quote(expected_project_name)}
set __vmcp_expected_part {tcl_quote(expected_part)}
set __vmcp_expected_top {tcl_quote(expected_top)}
set __vmcp_expected_version {tcl_quote(expected_vivado_version)}
set __vmcp_fileset_name {tcl_quote(fileset)}
set __vmcp_apply {1 if apply else 0}
set __vmcp_requested [list {path_list}]

set __vmcp_projects [get_projects -quiet]
if {{[llength $__vmcp_projects] != 1}} {{
    error "Expected exactly one open project, found [llength $__vmcp_projects]"
}}
set __vmcp_project [current_project]
set __vmcp_name [get_property NAME $__vmcp_project]
set __vmcp_dir [get_property DIRECTORY $__vmcp_project]
set __vmcp_xpr [file normalize [file join $__vmcp_dir "${{__vmcp_name}}.xpr"]]
set __vmcp_part [get_property PART $__vmcp_project]
set __vmcp_sources [get_filesets sources_1]
set __vmcp_top [get_property TOP $__vmcp_sources]
set __vmcp_version [version -short]

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

set __vmcp_filesets [get_filesets -quiet $__vmcp_fileset_name]
if {{[llength $__vmcp_filesets] != 1}} {{
    error "Fileset not found or ambiguous: $__vmcp_fileset_name"
}}
set __vmcp_fileset [lindex $__vmcp_filesets 0]
set __vmcp_existing_norm {{}}
foreach __vmcp_file [get_files -quiet -of_objects $__vmcp_fileset] {{
    lappend __vmcp_existing_norm [file normalize $__vmcp_file]
}}

set __vmcp_existing {{}}
set __vmcp_pending {{}}
set __vmcp_added {{}}
foreach __vmcp_file $__vmcp_requested {{
    set __vmcp_norm [file normalize $__vmcp_file]
    if {{[lsearch -exact -nocase $__vmcp_existing_norm $__vmcp_norm] >= 0}} {{
        lappend __vmcp_existing $__vmcp_norm
    }} elseif {{$__vmcp_apply}} {{
        add_files -fileset $__vmcp_fileset -norecurse $__vmcp_norm
        lappend __vmcp_added $__vmcp_norm
        lappend __vmcp_existing_norm $__vmcp_norm
    }} else {{
        lappend __vmcp_pending $__vmcp_norm
    }}
}}

if {{$__vmcp_apply && [llength $__vmcp_added] > 0
        && $__vmcp_fileset_name ne "constrs_1"}} {{
    update_compile_order -fileset $__vmcp_fileset
}}

set __vmcp_identity [join [list \
    "VMCP_SYNC_IDENTITY" "xpr=$__vmcp_xpr" "project=$__vmcp_name" \
    "part=$__vmcp_part" "top=$__vmcp_top" "vivado=$__vmcp_version" \
    "session={session_id}"] "|"]
set __vmcp_mode [expr {{$__vmcp_apply ? "APPLY" : "DRY_RUN"}}]
set __vmcp_result [join [list \
    "VMCP_SYNC_RESULT" "mode=$__vmcp_mode" "fileset=$__vmcp_fileset_name" \
    "existing=[llength $__vmcp_existing]" \
    "pending=[llength $__vmcp_pending]" "added=[llength $__vmcp_added]"] "|"]
puts $__vmcp_identity
puts $__vmcp_result
foreach __vmcp_file $__vmcp_existing {{ puts "VMCP_SYNC_EXISTING|$__vmcp_file" }}
foreach __vmcp_file $__vmcp_pending {{ puts "VMCP_SYNC_PENDING|$__vmcp_file" }}
foreach __vmcp_file $__vmcp_added {{ puts "VMCP_SYNC_ADDED|$__vmcp_file" }}
""".strip()

    return await _safe_execute(
        session,
        tcl,
        float(timeout),
        "工程文件同步失败",
    )


@mcp.tool(annotations=PROJECT_WRITE)
async def setup_debug_after_synth(
    probe_net_patterns: list[str],
    ila_clock_net: str,
    hub_clock_net: str,
    target_xdc_path: str,
    expected_xpr_path: str,
    expected_project_name: str,
    expected_part: str,
    expected_top: str,
    expected_vivado_version: str,
    synth_run: str = "synth_1",
    ila_name: str = "u_ila_0",
    data_depth: int = 2048,
    hub_clock_frequency_hz: int = 100_000_000,
    apply: bool = False,
    session_id: str = "default",
    timeout: int = 120,
    ctx: Context = None,
) -> str:
    """基于综合后的真实网表完成 Vivado Set Up Debug 流程。

    本工具复刻 GUI 的可靠流程：核对工程身份，打开已完成的综合 run，在综合网表
    中解析 ILA/Debug Hub 时钟和 ``MARK_DEBUG`` 探针，创建 ILA，最后调用
    ``save_constraints`` 写入指定的目标 XDC。它不会运行综合、实现、bitstream 或
    Hardware Manager。

    ``probe_net_patterns`` 每项对应一个 probe；支持综合网表 glob，例如
    ``w_data[*]`` 或标量 ``w_de``。总线按 dictionary 顺序连接，保持 bit0..bitN
    顺序。所有命中的 probe net 都必须具有 ``MARK_DEBUG=1``。

    ``apply=False`` 只核对工程、run、目标 XDC 和参数，绝不打开/关闭 design，
    也不解析综合网表中的 net；返回的是执行计划而非网表预检通过。
    ``apply=True`` 才打开综合设计，并删除同名 ILA 和仅由本工具管理的 ``dbg_hub`` 后重建，
    因此调用前必须取得用户对打开 synth design、替换 Debug Core 和覆盖目标 XDC
    的明确批准。若发现其他 Debug Core，工具拒绝执行，避免破坏用户已有 ILA。

    Args:
        probe_net_patterns: 每个 ILA probe 对应的综合网表 net 名或 glob。
        ila_clock_net: 必须唯一匹配的 ILA 采样时钟综合网名。
        hub_clock_net: 必须唯一匹配的 Debug Hub 常开时钟综合网名。
        target_xdc_path: 已在 constrs_1 中的目标 Debug XDC。
        expected_xpr_path: 用户指定工程的完整 .xpr 路径。
        expected_project_name: 预期工程名。
        expected_part: 预期 FPGA part。
        expected_top: 预期顶层。
        expected_vivado_version: ``version -short`` 预期值。
        synth_run: 已完成的综合 run。
        ila_name: 要创建的 ILA 实例名。
        data_depth: ILA 采样深度。
        hub_clock_frequency_hz: Debug Hub 输入时钟真实频率。
        apply: False 仅预检，True 保存 Debug XDC。
        session_id: 已连接的 Vivado MCP 会话。
        timeout: Tcl 超时秒数。
    """
    if not probe_net_patterns or any(not pattern.strip() for pattern in probe_net_patterns):
        return "[ERROR] probe_net_patterns 必须包含至少一个非空 net 名或 glob。"
    if len(probe_net_patterns) > 64:
        return "[ERROR] probe 数量不能超过 64。"
    if not ila_clock_net.strip() or not hub_clock_net.strip():
        return "[ERROR] ila_clock_net 和 hub_clock_net 不能为空。"
    if data_depth not in {1024, 2048, 4096, 8192, 16384, 32768, 65536, 131072}:
        return "[ERROR] data_depth 必须是 Vivado ILA 支持的 1024..131072 二次幂。"
    if hub_clock_frequency_hz <= 0:
        return "[ERROR] hub_clock_frequency_hz 必须大于 0。"

    try:
        validate_identifier(synth_run, "synth_run")
        validate_identifier(ila_name, "ila_name")
    except ValueError as exc:
        return f"[ERROR] {exc}"

    xpr_path = Path(expected_xpr_path).expanduser().resolve()
    target_xdc = Path(target_xdc_path).expanduser().resolve()
    if xpr_path.suffix.lower() != ".xpr" or not xpr_path.is_file():
        return f"[ERROR] expected_xpr_path 不是现存 .xpr 文件: {xpr_path}"
    if target_xdc.suffix.lower() != ".xdc" or not target_xdc.is_file():
        return f"[ERROR] target_xdc_path 不是现存 .xdc 文件: {target_xdc}"

    expected_values = {
        "expected_project_name": expected_project_name,
        "expected_part": expected_part,
        "expected_top": expected_top,
        "expected_vivado_version": expected_vivado_version,
    }
    for label, value in expected_values.items():
        if not value.strip():
            return f"[ERROR] {label} 不能为空。"

    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    probe_list = " ".join(tcl_quote(pattern) for pattern in probe_net_patterns)
    tcl = f"""
set __vmcp_expected_xpr {tcl_quote(xpr_path.as_posix())}
set __vmcp_expected_name {tcl_quote(expected_project_name)}
set __vmcp_expected_part {tcl_quote(expected_part)}
set __vmcp_expected_top {tcl_quote(expected_top)}
set __vmcp_expected_version {tcl_quote(expected_vivado_version)}
set __vmcp_synth_run {tcl_quote(synth_run)}
set __vmcp_ila_name {tcl_quote(ila_name)}
set __vmcp_xdc {tcl_quote(target_xdc.as_posix())}
set __vmcp_ila_clock_selector {tcl_quote(ila_clock_net)}
set __vmcp_hub_clock_selector {tcl_quote(hub_clock_net)}
set __vmcp_probe_selectors [list {probe_list}]
set __vmcp_apply {1 if apply else 0}

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
if {{[llength [get_files -quiet $__vmcp_xdc]] != 1}} {{
    error "Target XDC is not present exactly once in the project: $__vmcp_xdc"
}}
set __vmcp_run [get_runs -quiet $__vmcp_synth_run]
if {{[llength $__vmcp_run] != 1}} {{ error "Synthesis run not found: $__vmcp_synth_run" }}
if {{![string match *Complete* [get_property STATUS $__vmcp_run]]}} {{
    error "Synthesis run is not complete: [get_property STATUS $__vmcp_run]"
}}

if {{!$__vmcp_apply}} {{
    puts [join [list "VMCP_DEBUG_RESULT" "mode=PLAN_ONLY" \
        "ila=$__vmcp_ila_name" "probe_selectors=[llength $__vmcp_probe_selectors]" \
        "xdc=$__vmcp_xdc" "netlist_inspected=0"] "|"]
    return
}}

if {{[current_design -quiet] ne ""}} {{ close_design }}
open_run $__vmcp_synth_run

set __vmcp_ila_clock [get_nets -hierarchical -quiet $__vmcp_ila_clock_selector]
set __vmcp_hub_clock [get_nets -hierarchical -quiet $__vmcp_hub_clock_selector]
if {{[llength $__vmcp_ila_clock] != 1}} {{
    error "ILA clock selector must match exactly one net: $__vmcp_ila_clock_selector"
}}
if {{[llength $__vmcp_hub_clock] != 1}} {{
    error "Hub clock selector must match exactly one net: $__vmcp_hub_clock_selector"
}}

set __vmcp_probe_groups {{}}
foreach __vmcp_selector $__vmcp_probe_selectors {{
    set __vmcp_nets [lsort -dictionary [get_nets -hierarchical -quiet $__vmcp_selector]]
    if {{[llength $__vmcp_nets] == 0}} {{
        error "Probe selector matched no nets: $__vmcp_selector"
    }}
    foreach __vmcp_net $__vmcp_nets {{
        if {{![get_property MARK_DEBUG $__vmcp_net]}} {{
            error "Probe net is not MARK_DEBUG: $__vmcp_net"
        }}
    }}
    lappend __vmcp_probe_groups $__vmcp_nets
    puts "VMCP_DEBUG_PROBE|selector=$__vmcp_selector|width=[llength $__vmcp_nets]"
}}
puts "VMCP_DEBUG_CLOCK|ila=$__vmcp_ila_clock|hub=$__vmcp_hub_clock"

if {{$__vmcp_apply}} {{
    set __vmcp_allowed [list dbg_hub $__vmcp_ila_name]
    foreach __vmcp_core [get_debug_cores -quiet] {{
        if {{[lsearch -exact $__vmcp_allowed $__vmcp_core] < 0}} {{
            error "Other debug core exists; refusing replacement: $__vmcp_core"
        }}
    }}
    if {{[llength [get_debug_cores -quiet $__vmcp_ila_name]]}} {{
        delete_debug_core [get_debug_cores $__vmcp_ila_name]
    }}
    if {{[llength [get_debug_cores -quiet dbg_hub]]}} {{
        delete_debug_core [get_debug_cores dbg_hub]
    }}

    create_debug_core $__vmcp_ila_name ila
    set __vmcp_ila [get_debug_cores $__vmcp_ila_name]
    set_property C_DATA_DEPTH {data_depth} $__vmcp_ila
    set_property C_TRIGIN_EN false $__vmcp_ila
    set_property C_TRIGOUT_EN false $__vmcp_ila
    set_property C_ADV_TRIGGER false $__vmcp_ila
    set_property C_INPUT_PIPE_STAGES 0 $__vmcp_ila
    set_property C_EN_STRG_QUAL false $__vmcp_ila
    set_property ALL_PROBE_SAME_MU true $__vmcp_ila
    set_property ALL_PROBE_SAME_MU_CNT 1 $__vmcp_ila
    connect_debug_port $__vmcp_ila_name/clk $__vmcp_ila_clock

    set __vmcp_probe_index 0
    foreach __vmcp_nets $__vmcp_probe_groups {{
        if {{$__vmcp_probe_index > 0}} {{ create_debug_port $__vmcp_ila_name probe }}
        set __vmcp_port [get_debug_ports $__vmcp_ila_name/probe$__vmcp_probe_index]
        set_property PORT_WIDTH [llength $__vmcp_nets] $__vmcp_port
        set_property PROBE_TYPE DATA_AND_TRIGGER $__vmcp_port
        connect_debug_port $__vmcp_port $__vmcp_nets
        incr __vmcp_probe_index
    }}

    set __vmcp_hub [get_debug_cores dbg_hub]
    set_property C_CLK_INPUT_FREQ_HZ {hub_clock_frequency_hz} $__vmcp_hub
    set_property C_ENABLE_CLK_DIVIDER false $__vmcp_hub
    set_property C_USER_SCAN_CHAIN 1 $__vmcp_hub
    connect_debug_port dbg_hub/clk $__vmcp_hub_clock
    set_property TARGET_CONSTRS_FILE $__vmcp_xdc [get_filesets constrs_1]
    save_constraints
    set __vmcp_result [join [list "VMCP_DEBUG_RESULT" "mode=APPLY" \
        "ila=$__vmcp_ila_name" "probes=[llength $__vmcp_probe_groups]" \
        "xdc=$__vmcp_xdc"] "|"]
    puts $__vmcp_result
}}
""".strip()

    return await _safe_execute(
        session,
        tcl,
        float(timeout),
        "综合后 Debug 设置失败",
    )
