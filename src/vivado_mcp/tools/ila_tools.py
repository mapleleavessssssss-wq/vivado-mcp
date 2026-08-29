"""Live ILA trigger and capture tools with fail-closed hardware identity gates."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from mcp.server.mcpserver import Context

from vivado_mcp.server import _NO_SESSION, _require_session, mcp
from vivado_mcp.tools._hardware_safety import (
    is_loopback_hw_server,
    is_valid_hw_server_url,
    parse_hw_server_url,
    select_exact_tcl_proc,
    select_hw_server_tcl_proc,
)
from vivado_mcp.tools.annotations import HARDWARE_CHANGE
from vivado_mcp.vivado.tcl_utils import tcl_quote

_MAX_CAPTURE_TIMEOUT_SEC = 3600
_LABEL_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_BASIC_TRIGGER_ACTIONS = frozenset(
    {"low", "high", "rising", "falling", "either_edge", "no_change", "dont_care"}
)
_BASIC_TRIGGER_MODES = frozenset({"BASIC_ONLY", "BASIC_OR_TRIG_IN"})
_BASIC_TRIGGER_CONDITIONS = frozenset({"AND", "OR", "NAND", "NOR"})


def _is_relative_to(path: Path, parent: Path) -> bool:
    """Python 3.10-compatible ``Path.is_relative_to`` with resolved paths."""
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _validate_output_root(raw_path: str) -> tuple[Path | None, str]:
    """Validate a caller-owned capture root without creating anything."""
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        return None, "[ERROR] output_root 必须是绝对路径。"

    resolved = candidate.resolve()
    if resolved == Path(resolved.anchor):
        return None, "[ERROR] output_root 不能是磁盘根目录。"
    if resolved.exists() and not resolved.is_dir():
        return None, f"[ERROR] output_root 已存在但不是目录: {resolved}"

    repository_root = Path(__file__).resolve().parents[3]
    if _is_relative_to(resolved, repository_root):
        return None, (
            f"[ERROR] ILA 数据不能写入 Vivado MCP 源码仓库: {resolved}。"
            "请使用工程 artifacts 下的独立 ila_capture 目录。"
        )
    return resolved, ""


def _sanitize_capture_label(label: str) -> str:
    """Return a short filesystem-safe capture label."""
    normalized = _LABEL_UNSAFE_RE.sub("_", label.strip()).strip("._-")
    return normalized[:48] or "ila"


def _create_capture_dir(output_root: Path, capture_label: str) -> Path:
    """Create one uniquely named child directory under ``output_root``."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    dirname = f"ila_capture_{timestamp}_{capture_label}_{uuid4().hex[:8]}"
    capture_dir = output_root / dirname
    capture_dir.mkdir(parents=True, exist_ok=False)
    return capture_dir


def _resolve_identity_file(
    raw_path: str,
    *,
    suffix: str,
    label: str,
) -> tuple[Path | None, str]:
    """Resolve one required local hardware-identity artifact."""
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        return None, f"[ERROR] {label} 必须是绝对路径。"
    resolved = candidate.resolve()
    if resolved.suffix.lower() != suffix or not resolved.is_file():
        return None, f"[ERROR] {label} 不是现存 {suffix} 文件: {resolved}"
    return resolved, ""


def _validate_basic_trigger_request(
    *,
    probe_triggers: dict[str, str],
    data_depth: int,
    trigger_position: int,
    window_count: int,
    trigger_condition: str,
    expected_trigger_mode: str,
) -> tuple[dict[str, str] | None, str]:
    """Validate semantic trigger inputs before looking up a live session."""
    if not probe_triggers:
        return None, "[ERROR] probe_triggers 至少需要一个 probe 条件。"

    normalized: dict[str, str] = {}
    active_count = 0
    for raw_name, raw_action in probe_triggers.items():
        name = raw_name.strip()
        action = raw_action.strip().lower()
        if not name:
            return None, "[ERROR] probe_triggers 不能包含空 probe 名。"
        if action not in _BASIC_TRIGGER_ACTIONS:
            allowed = ", ".join(sorted(_BASIC_TRIGGER_ACTIONS))
            return None, (
                f"[ERROR] probe {name!r} 的 trigger action {raw_action!r} 不支持；"
                f"允许: {allowed}。"
            )
        normalized[name] = action
        if action != "dont_care":
            active_count += 1

    if active_count == 0:
        return None, "[ERROR] 至少需要一个非 dont_care 的有效触发条件。"
    if data_depth < 1 or data_depth & (data_depth - 1):
        return None, "[ERROR] data_depth 必须是正整数二次幂。"
    if window_count < 1:
        return None, "[ERROR] window_count 必须大于等于 1。"
    if not 0 <= trigger_position < data_depth:
        return None, "[ERROR] trigger_position 必须在 0..data_depth-1 范围内。"

    condition = trigger_condition.strip().upper()
    if condition not in _BASIC_TRIGGER_CONDITIONS:
        return None, "[ERROR] trigger_condition 仅支持 AND、OR、NAND 或 NOR。"
    mode = expected_trigger_mode.strip().upper()
    if mode not in _BASIC_TRIGGER_MODES:
        return None, (
            "[ERROR] expected_trigger_mode 仅支持 BASIC_ONLY 或 "
            "BASIC_OR_TRIG_IN。"
        )
    return normalized, ""


def _build_basic_trigger_tcl(
    *,
    expected_program_file: Path,
    expected_probes_file: Path,
    probe_triggers: dict[str, str],
    data_depth: int,
    trigger_position: int,
    window_count: int,
    trigger_condition: str,
    expected_trigger_mode: str,
    clear_unlisted_probes: bool,
    apply: bool,
    hw_server_url: str,
    target_name: str,
    device_name: str,
    ila_name: str,
) -> str:
    """Build a preflight-first basic-trigger configuration transaction."""
    trigger_entries = " ".join(
        f"[list {tcl_quote(name)} {tcl_quote(action)}]"
        for name, action in probe_triggers.items()
    )
    program_value = str(expected_program_file).replace("\\", "/")
    probes_value = str(expected_probes_file).replace("\\", "/")
    server_host, server_port = parse_hw_server_url(hw_server_url)
    return f"""{select_exact_tcl_proc()}
{select_hw_server_tcl_proc()}
proc __vmcp_paths_match {{expected actual}} {{
    if {{$expected eq "" || $actual eq ""}} {{return 0}}
    set lhs [file normalize $expected]
    set rhs [file normalize $actual]
    if {{$::tcl_platform(platform) eq "windows"}} {{
        return [string equal -nocase $lhs $rhs]
    }}
    return [string equal $lhs $rhs]
}}
proc __vmcp_property_read_only {{object property}} {{
    if {{[lsearch -exact [list_property $object] $property] < 0}} {{
        error "Required property is unavailable: $property on $object"
    }}
    set report [report_property -all -return_string $object $property]
    foreach line [split $report "\n"] {{
        set fields [regexp -all -inline {{\\S+}} [string trim $line]]
        if {{[llength $fields] >= 3 && [lindex $fields 0] eq $property}} {{
            set flag [string tolower [lindex $fields 2]]
            if {{$flag eq "true"}} {{return 1}}
            if {{$flag eq "false"}} {{return 0}}
        }}
    }}
    error "Could not determine read-only state for $property on $object"
}}
proc __vmcp_require_writable_property {{object property}} {{
    if {{[__vmcp_property_read_only $object $property]}} {{
        error "Required writable property is read-only: $property on $object"
    }}
}}
proc __vmcp_compare_value {{semantic width}} {{
    switch -- $semantic {{
        low {{set symbol 0}}
        high {{set symbol 1}}
        rising {{
            if {{$width != 1}} {{error "rising trigger requires a 1-bit probe"}}
            set symbol R
        }}
        falling {{
            if {{$width != 1}} {{error "falling trigger requires a 1-bit probe"}}
            set symbol F
        }}
        either_edge {{
            if {{$width != 1}} {{error "either_edge trigger requires a 1-bit probe"}}
            set symbol B
        }}
        no_change {{
            if {{$width != 1}} {{error "no_change trigger requires a 1-bit probe"}}
            set symbol N
        }}
        dont_care {{set symbol X}}
        default {{error "Unsupported trigger semantic: $semantic"}}
    }}
    return "eq${{width}}'b[string repeat $symbol $width]"
}}
set __vmcp_server_url {tcl_quote(hw_server_url)}
set __vmcp_server_host {tcl_quote(server_host)}
set __vmcp_server_port {server_port}
set __vmcp_target_name {tcl_quote(target_name)}
set __vmcp_device_name {tcl_quote(device_name)}
set __vmcp_ila_name {tcl_quote(ila_name)}
set __vmcp_expected_program_file {tcl_quote(program_value)}
set __vmcp_expected_probes_file {tcl_quote(probes_value)}
set __vmcp_expected_trigger_mode {tcl_quote(expected_trigger_mode)}
set __vmcp_trigger_condition {tcl_quote(trigger_condition)}
set __vmcp_data_depth {data_depth}
set __vmcp_window_count {window_count}
set __vmcp_trigger_position {trigger_position}
set __vmcp_clear_unlisted {1 if clear_unlisted_probes else 0}
set __vmcp_apply {1 if apply else 0}
set __vmcp_requested_triggers [list {trigger_entries}]

# Identity and capability preflight.  Nothing below this point writes hardware
# configuration until every object/property/value check has passed.
open_hw_manager
set __vmcp_server [__vmcp_select_hw_server \
    $__vmcp_server_url $__vmcp_server_host $__vmcp_server_port]
set __vmcp_targets [get_hw_targets -quiet -of_objects $__vmcp_server]
set __vmcp_target [__vmcp_select_exact $__vmcp_targets $__vmcp_target_name hw_target]
if {{![get_property IS_OPENED $__vmcp_target]}} {{open_hw_target $__vmcp_target}}
set __vmcp_devices [get_hw_devices -quiet -of_objects $__vmcp_target]
set __vmcp_device [__vmcp_select_exact $__vmcp_devices $__vmcp_device_name hw_device]
set __vmcp_actual_program_file [get_property PROGRAM.FILE $__vmcp_device]
set __vmcp_actual_probes_file [get_property PROBES.FILE $__vmcp_device]
if {{![__vmcp_paths_match $__vmcp_expected_program_file $__vmcp_actual_program_file]}} {{
    error [format \
        "PROGRAM.FILE identity mismatch: expected=%s actual=%s" \
        $__vmcp_expected_program_file $__vmcp_actual_program_file]
}}
if {{![__vmcp_paths_match $__vmcp_expected_probes_file $__vmcp_actual_probes_file]}} {{
    error [format \
        "PROBES.FILE identity mismatch: expected=%s actual=%s" \
        $__vmcp_expected_probes_file $__vmcp_actual_probes_file]
}}
set __vmcp_ilas [get_hw_ilas -quiet -of_objects $__vmcp_device]
set __vmcp_ila [__vmcp_select_exact $__vmcp_ilas $__vmcp_ila_name hw_ila]
set __vmcp_core_status [get_property STATUS.CORE_STATUS $__vmcp_ila]
if {{![string equal -nocase $__vmcp_core_status IDLE]}} {{
    error "ILA must be IDLE before trigger configuration: $__vmcp_core_status"
}}
set __vmcp_trigger_mode [get_property CONTROL.TRIGGER_MODE $__vmcp_ila]
if {{$__vmcp_trigger_mode ne $__vmcp_expected_trigger_mode}} {{
    error [format \
        "ILA trigger mode mismatch: expected=%s actual=%s; mode is observed, never rewritten" \
        $__vmcp_expected_trigger_mode $__vmcp_trigger_mode]
}}
set __vmcp_max_depth [get_property STATIC.MAX_DATA_DEPTH $__vmcp_ila]
if {{$__vmcp_data_depth * $__vmcp_window_count != $__vmcp_max_depth}} {{
    error [format \
        "DATA_DEPTH * WINDOW_COUNT must equal MAX_DATA_DEPTH: %s * %s != %s" \
        $__vmcp_data_depth $__vmcp_window_count $__vmcp_max_depth]
}}
set __vmcp_writable_ila_properties [list \
    CONTROL.DATA_DEPTH CONTROL.WINDOW_COUNT \
    CONTROL.TRIGGER_POSITION CONTROL.TRIGGER_CONDITION]
foreach property $__vmcp_writable_ila_properties {{
    __vmcp_require_writable_property $__vmcp_ila $property
}}
set __vmcp_all_probes [get_hw_probes -quiet -of_objects $__vmcp_ila]
if {{[llength $__vmcp_all_probes] == 0}} {{error "Selected ILA has no hardware probes"}}
set __vmcp_specs {{}}
set __vmcp_selected_probes {{}}
foreach request $__vmcp_requested_triggers {{
    lassign $request requested_name semantic
    set probe [__vmcp_select_exact $__vmcp_all_probes $requested_name hw_probe]
    if {{[lsearch -exact $__vmcp_selected_probes $probe] >= 0}} {{
        error "Multiple requested names resolve to the same hw_probe: $probe"
    }}
    set width [get_property PROBE_PORT_BIT_COUNT $probe]
    set comparator_count [get_property COMPARATOR_COUNT $probe]
    if {{$width < 1}} {{error "Invalid probe width for $probe: $width"}}
    if {{$comparator_count < 1}} {{error "Probe has no trigger comparator: $probe"}}
    __vmcp_require_writable_property $probe TRIGGER_COMPARE_VALUE
    set compare_value [__vmcp_compare_value $semantic $width]
    lappend __vmcp_selected_probes $probe
    lappend __vmcp_specs [list $probe $semantic $width $compare_value]
}}
if {{$__vmcp_clear_unlisted}} {{
    foreach probe $__vmcp_all_probes {{
        if {{[lsearch -exact $__vmcp_selected_probes $probe] >= 0}} {{continue}}
        set width [get_property PROBE_PORT_BIT_COUNT $probe]
        set comparator_count [get_property COMPARATOR_COUNT $probe]
        if {{$width < 1}} {{error "Invalid probe width for $probe: $width"}}
        if {{$comparator_count < 1}} {{
            puts "VMCP_ILA_TRIGGER_SKIPPED|probe=$probe|reason=no_comparator"
            continue
        }}
        __vmcp_require_writable_property $probe TRIGGER_COMPARE_VALUE
        lappend __vmcp_specs [list $probe dont_care $width [__vmcp_compare_value dont_care $width]]
    }}
}}
puts [join [list VMCP_ILA_TRIGGER_PREFLIGHT \
    "target=$__vmcp_target" "device=$__vmcp_device" "ila=$__vmcp_ila" \
    "mode=$__vmcp_trigger_mode" \
    "core_status=$__vmcp_core_status" \
    "max_depth=$__vmcp_max_depth" \
    "probes=[llength $__vmcp_all_probes]"] "|"]
foreach spec $__vmcp_specs {{
    lassign $spec probe semantic width compare_value
    puts "VMCP_ILA_TRIGGER_SPEC|probe=$probe|semantic=$semantic|width=$width|compare=$compare_value"
}}

if {{$__vmcp_apply}} {{
    set_property -dict [list \
        CONTROL.DATA_DEPTH $__vmcp_data_depth \
        CONTROL.WINDOW_COUNT $__vmcp_window_count \
        CONTROL.TRIGGER_POSITION $__vmcp_trigger_position \
        CONTROL.TRIGGER_CONDITION $__vmcp_trigger_condition] $__vmcp_ila
    foreach spec $__vmcp_specs {{
        lassign $spec probe semantic width compare_value
        set_property TRIGGER_COMPARE_VALUE $compare_value $probe
    }}
    puts [join [list VMCP_ILA_TRIGGER_CONFIGURED \
        "ila=$__vmcp_ila" "depth=$__vmcp_data_depth" \
        "windows=$__vmcp_window_count" \
        "position=$__vmcp_trigger_position" \
        "condition=$__vmcp_trigger_condition"] "|"]
}} else {{
    puts "VMCP_ILA_TRIGGER_PLAN_ONLY|ila=$__vmcp_ila|writes=0|armed=0"
}}
"""


def _build_capture_tcl(
    *,
    csv_path: Path,
    probes_file: Path | None,
    hw_server_url: str,
    target_name: str,
    device_name: str,
    ila_name: str,
    trigger_now: bool,
    timeout_sec: int,
) -> str:
    """Build one Tcl transaction for connect, capture, upload and CSV export."""
    wait_minutes = timeout_sec / 60.0
    ltx_value = str(probes_file).replace("\\", "/") if probes_file else ""
    csv_value = str(csv_path).replace("\\", "/")
    trigger_option = "-trigger_now " if trigger_now else ""
    server_host, server_port = parse_hw_server_url(hw_server_url)
    return f"""{select_exact_tcl_proc()}
{select_hw_server_tcl_proc()}
set __vmcp_server_url {tcl_quote(hw_server_url)}
set __vmcp_server_host {tcl_quote(server_host)}
set __vmcp_server_port {server_port}
set __vmcp_target_name {tcl_quote(target_name)}
set __vmcp_device_name {tcl_quote(device_name)}
set __vmcp_ila_name {tcl_quote(ila_name)}
set __vmcp_probes_file {tcl_quote(ltx_value)}
set __vmcp_csv_file {tcl_quote(csv_value)}
open_hw_manager
set __vmcp_server [__vmcp_select_hw_server \
    $__vmcp_server_url $__vmcp_server_host $__vmcp_server_port]
set __vmcp_targets [get_hw_targets -quiet -of_objects $__vmcp_server]
set __vmcp_target [__vmcp_select_exact $__vmcp_targets $__vmcp_target_name hw_target]
if {{![get_property IS_OPENED $__vmcp_target]}} {{open_hw_target $__vmcp_target}}
set __vmcp_devices [get_hw_devices -quiet -of_objects $__vmcp_target]
set __vmcp_device [__vmcp_select_exact $__vmcp_devices $__vmcp_device_name hw_device]
current_hw_device $__vmcp_device
if {{$__vmcp_probes_file ne ""}} {{
    set_property PROBES.FILE $__vmcp_probes_file $__vmcp_device
    set_property FULL_PROBES.FILE $__vmcp_probes_file $__vmcp_device
}}
set __vmcp_ilas [get_hw_ilas -quiet -of_objects $__vmcp_device]
if {{[llength $__vmcp_ilas] == 0}} {{
    refresh_hw_device $__vmcp_device
    set __vmcp_ilas [get_hw_ilas -quiet -of_objects $__vmcp_device]
}}
set __vmcp_ila [__vmcp_select_exact $__vmcp_ilas $__vmcp_ila_name hw_ila]
current_hw_ila $__vmcp_ila
run_hw_ila {trigger_option}$__vmcp_ila
wait_on_hw_ila -timeout {wait_minutes:.6f} $__vmcp_ila
set __vmcp_data [upload_hw_ila_data $__vmcp_ila]
if {{[llength $__vmcp_data] != 1}} {{
    error "Expected one uploaded hw_ila_data, found [llength $__vmcp_data]"
}}
write_hw_ila_data -force -csv_file $__vmcp_csv_file $__vmcp_data
if {{![file isfile $__vmcp_csv_file]}} {{error "ILA CSV was not created"}}
puts "VMCP_ILA_CAPTURE|target=$__vmcp_target|device=$__vmcp_device|ila=$__vmcp_ila"
puts "VMCP_ILA_CSV:$__vmcp_csv_file"
"""


@mcp.tool(annotations=HARDWARE_CHANGE)
async def configure_hw_ila_basic_trigger(
    expected_program_file_path: str,
    expected_probes_file_path: str,
    probe_triggers: dict[str, str],
    data_depth: int,
    trigger_position: int,
    window_count: int = 1,
    trigger_condition: str = "AND",
    expected_trigger_mode: str = "BASIC_ONLY",
    clear_unlisted_probes: bool = True,
    apply: bool = False,
    ila_name: str = "",
    device_name: str = "",
    target_name: str = "",
    hw_server_url: str = "localhost:3121",
    allow_remote_hw_server: bool = False,
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """预检并配置 ILA basic trigger；默认 PLAN_ONLY，且永不写 trigger mode。

    ``probe_triggers`` 是精确 probe 名到语义条件的映射，条件仅接受 ``low``、
    ``high``、``rising``、``falling``、``either_edge``、``no_change`` 和
    ``dont_care``。边沿/保持条件只允许 1-bit probe。默认把未列出的全部 probe
    设为 don't-care；没有 comparator 的 data-only probe 会跳过，不会误阻断。

    工具要求当前 ``PROGRAM.FILE``/``PROBES.FILE`` 与两个显式身份文件精确匹配，
    并在任何 ``set_property`` 前检查唯一 target/device/ILA/probe、probe width、
    comparator、depth/window/position、属性存在性和运行时只读状态。
    ``trigger_condition`` 支持 Vivado 官方的 ``AND/OR/NAND/NOR``。
    ``CONTROL.TRIGGER_MODE`` 只读取并核对 ``expected_trigger_mode``，不解析其
    只读元数据也不会改它；这兼容实际 core 把 ``BASIC_ONLY`` 固定为只读的情况。

    ``apply=False`` 只返回 ``PLAN_ONLY``；``apply=True`` 仅写 trigger 配置，不会
    ``run_hw_ila``、等待、上传、编程或刷新器件。配置 PASS 后调用
    ``capture_hw_ila_to_csv(trigger_now=False)`` 才会 arm/wait/upload。连接或打开
    Hardware Manager/target 仍属于硬件会话状态变化，调用前必须获得用户批准。
    """
    program_file, error = _resolve_identity_file(
        expected_program_file_path,
        suffix=".bit",
        label="expected_program_file_path",
    )
    if error:
        return error
    probes_file, error = _resolve_identity_file(
        expected_probes_file_path,
        suffix=".ltx",
        label="expected_probes_file_path",
    )
    if error:
        return error
    normalized, error = _validate_basic_trigger_request(
        probe_triggers=probe_triggers,
        data_depth=data_depth,
        trigger_position=trigger_position,
        window_count=window_count,
        trigger_condition=trigger_condition,
        expected_trigger_mode=expected_trigger_mode,
    )
    if error:
        return error
    if not hw_server_url.strip() or not is_valid_hw_server_url(hw_server_url):
        return "[ERROR] hw_server_url 必须是有效的 host:port（如 localhost:3121）。"
    if not is_loopback_hw_server(hw_server_url) and not allow_remote_hw_server:
        return (
            f"[BLOCKED] 默认禁止远程 hw_server: {hw_server_url!r}。"
            "请确认目标电脑、板卡和网络边界后显式设置 allow_remote_hw_server=True。"
        )

    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)
    tcl = _build_basic_trigger_tcl(
        expected_program_file=program_file,
        expected_probes_file=probes_file,
        probe_triggers=normalized,
        data_depth=data_depth,
        trigger_position=trigger_position,
        window_count=window_count,
        trigger_condition=trigger_condition.strip().upper(),
        expected_trigger_mode=expected_trigger_mode.strip().upper(),
        clear_unlisted_probes=clear_unlisted_probes,
        apply=apply,
        hw_server_url=hw_server_url,
        target_name=target_name,
        device_name=device_name,
        ila_name=ila_name,
    )
    try:
        result = await session.execute(tcl, timeout=60.0)
    except Exception as exc:
        return f"[ERROR] ILA basic trigger 预检/配置失败: {exc}"
    if result.is_error:
        return result.summary

    marker = (
        "VMCP_ILA_TRIGGER_CONFIGURED|"
        if apply
        else "VMCP_ILA_TRIGGER_PLAN_ONLY|"
    )
    if "VMCP_ILA_TRIGGER_PREFLIGHT|" not in result.output or marker not in result.output:
        return "[ERROR] ILA basic trigger 未返回完整阶段标记。\n" + result.output
    state = "CONFIGURED" if apply else "PLAN_ONLY"
    return f"[{state}] ILA basic trigger preflight PASS\n{result.output.strip()}"


@mcp.tool(annotations=HARDWARE_CHANGE)
async def capture_hw_ila_to_csv(
    output_root: str,
    probes_file_path: str = "",
    ila_name: str = "",
    device_name: str = "",
    target_name: str = "",
    capture_label: str = "",
    trigger_now: bool = True,
    hw_server_url: str = "localhost:3121",
    allow_remote_hw_server: bool = False,
    timeout_sec: int = 30,
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """连接现有硬件，采集唯一 ILA，并把数据写入独立时间戳目录的 CSV。

    工具不会编程器件，也不会修改 bitstream、LTX 或 Vivado 工程。默认使用
    ``run_hw_ila -trigger_now`` 立即采集；``trigger_now=False`` 时沿用 ILA 当前
    触发配置。调用会连接/刷新硬件并改变 ILA 采集状态，因此必须在用户明确批准
    板卡操作后使用。

    ``output_root`` 必须是调用方给出的绝对目录。每次调用只在其下创建一个
    ``ila_capture_<timestamp>_<label>_<id>`` 子目录，CSV 固定名为
    ``capture.csv``；删除整个 ``output_root`` 即可统一清除所有采集数据。

    ``target_name``、``device_name`` 或 ``ila_name`` 留空时，对应对象必须恰好
    只有一个；提供名称时也必须唯一精确匹配，工具不会静默选择第一个对象。
    ``probes_file_path`` 可留空以沿用当前 ``PROBES.FILE``，也可明确指定现存
    ``.ltx`` 文件。

    默认只允许 ``localhost``/``127.0.0.1``/``::1``。连接远程 hw_server 会把
    硬件操作发送到另一台主机，必须明确设置 ``allow_remote_hw_server=True``；
    该参数只解除地址门禁，不代表自动批准硬件操作。
    """
    output_path, error = _validate_output_root(output_root)
    if error:
        return error
    if not 1 <= timeout_sec <= _MAX_CAPTURE_TIMEOUT_SEC:
        return f"[ERROR] timeout_sec 必须在 1..{_MAX_CAPTURE_TIMEOUT_SEC} 秒之间。"
    if not hw_server_url.strip():
        return "[ERROR] hw_server_url 不能为空。"
    if not is_valid_hw_server_url(hw_server_url):
        return "[ERROR] hw_server_url 必须是有效的 host:port（如 localhost:3121）。"
    if not is_loopback_hw_server(hw_server_url) and not allow_remote_hw_server:
        return (
            f"[BLOCKED] 默认禁止远程 hw_server: {hw_server_url!r}。"
            "请确认目标电脑、板卡和网络边界后显式设置 allow_remote_hw_server=True。"
        )

    probes_file = None
    if probes_file_path:
        probes_file = Path(probes_file_path).expanduser().resolve()
        if probes_file.suffix.lower() != ".ltx" or not probes_file.is_file():
            return f"[ERROR] probes_file_path 不是现存 .ltx 文件: {probes_file}"

    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    label = _sanitize_capture_label(capture_label or ila_name or "ila")
    try:
        capture_dir = _create_capture_dir(output_path, label)
    except OSError as exc:
        return f"[ERROR] 创建 ILA 采集目录失败: {exc}"
    csv_path = capture_dir / "capture.csv"
    tcl = _build_capture_tcl(
        csv_path=csv_path,
        probes_file=probes_file,
        hw_server_url=hw_server_url,
        target_name=target_name,
        device_name=device_name,
        ila_name=ila_name,
        trigger_now=trigger_now,
        timeout_sec=timeout_sec,
    )
    try:
        result = await session.execute(tcl, timeout=float(timeout_sec + 20))
    except Exception as exc:
        return f"[ERROR] ILA 采集失败: {exc}\n采集目录: {capture_dir}"
    if result.is_error:
        return f"{result.summary}\n采集目录: {capture_dir}"
    if "VMCP_ILA_CAPTURE|" not in result.output or "VMCP_ILA_CSV:" not in result.output:
        return f"[ERROR] ILA 采集未返回完成标记。\n采集目录: {capture_dir}"
    return (
        f"[OK] ILA 采集完成\nCSV: {csv_path}\n采集目录: {capture_dir}\n"
        f"{result.output.strip()}"
    )
