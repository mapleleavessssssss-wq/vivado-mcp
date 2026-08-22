"""Live ILA capture tools with explicit, disposable output storage."""

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
    select_exact_tcl_proc,
)
from vivado_mcp.tools.annotations import HARDWARE_CHANGE
from vivado_mcp.vivado.tcl_utils import tcl_quote

_MAX_CAPTURE_TIMEOUT_SEC = 3600
_LABEL_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


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
    return f"""{select_exact_tcl_proc()}
set __vmcp_server_url {tcl_quote(hw_server_url)}
set __vmcp_target_name {tcl_quote(target_name)}
set __vmcp_device_name {tcl_quote(device_name)}
set __vmcp_ila_name {tcl_quote(ila_name)}
set __vmcp_probes_file {tcl_quote(ltx_value)}
set __vmcp_csv_file {tcl_quote(csv_value)}
open_hw_manager
if {{[llength [get_hw_servers -quiet]] == 0}} {{
    connect_hw_server -url $__vmcp_server_url
}}
set __vmcp_targets [get_hw_targets -quiet]
set __vmcp_target [__vmcp_select_exact $__vmcp_targets $__vmcp_target_name hw_target]
if {{![get_property IS_OPENED $__vmcp_target]}} {{open_hw_target $__vmcp_target}}
set __vmcp_devices [get_hw_devices -quiet]
set __vmcp_device [__vmcp_select_exact $__vmcp_devices $__vmcp_device_name hw_device]
current_hw_device $__vmcp_device
if {{$__vmcp_probes_file ne ""}} {{
    set_property PROBES.FILE $__vmcp_probes_file $__vmcp_device
    set_property FULL_PROBES.FILE $__vmcp_probes_file $__vmcp_device
}}
set __vmcp_ilas [get_hw_ilas -quiet]
if {{[llength $__vmcp_ilas] == 0}} {{
    refresh_hw_device $__vmcp_device
    set __vmcp_ilas [get_hw_ilas -quiet]
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
