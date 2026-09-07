"""Read-only Vivado Tcl command capability discovery.

Vivado releases do not expose one permanently stable Tcl surface.  Rather
than guess support from the release string, query the active interpreter with
Tcl's ``info commands``.  The probe only asks whether exact command names are
registered; it never invokes the target commands.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING

from vivado_mcp.vivado.tcl_utils import tcl_quote

if TYPE_CHECKING:
    from vivado_mcp.vivado.base_session import BaseSession


DEFAULT_CAPABILITY_GROUPS: dict[str, tuple[str, ...]] = {
    "identity": (
        "version",
        "current_project",
        "current_design",
        "get_projects",
    ),
    "project": (
        "open_project",
        "close_project",
        "get_files",
        "get_filesets",
        "update_compile_order",
    ),
    "runs": (
        "get_runs",
        "launch_runs",
        "wait_on_run",
        "open_run",
        "reset_runs",
        "write_bitstream",
    ),
    "reports": (
        "report_timing_summary",
        "report_utilization",
        "report_io",
        "report_ip_status",
        "report_clock_interaction",
        "report_cdc",
        "report_methodology",
        "report_qor_assessment",
        "report_qor_suggestions",
        "report_design_analysis",
        "report_high_fanout_nets",
    ),
    "simulation": (
        "launch_simulation",
        "close_sim",
        "current_sim",
        "open_wave_database",
        "current_wave_config",
        "get_scopes",
    ),
    "hardware_handoff": (
        "write_hwdef",
        "write_sysdef",
        "write_hw_platform",
    ),
    "hardware_manager": (
        "open_hw_manager",
        "connect_hw_server",
        "get_hw_targets",
        "open_hw_target",
        "get_hw_devices",
        "program_hw_devices",
        "get_hw_ilas",
        "run_hw_ila",
        "upload_hw_ila_data",
    ),
}

_COMMAND_NAME_RE = re.compile(r"^[:A-Za-z_][A-Za-z0-9_:.-]*$")
_MAX_COMMANDS = 128


def default_capability_commands() -> list[str]:
    """Return the de-duplicated default matrix in stable display order."""
    return list(
        dict.fromkeys(
            command
            for commands in DEFAULT_CAPABILITY_GROUPS.values()
            for command in commands
        )
    )


def normalize_capability_commands(commands: Iterable[str] | None) -> list[str]:
    """Validate exact Tcl command names and preserve first-seen order."""
    selected = default_capability_commands() if not commands else list(commands)
    if len(selected) > _MAX_COMMANDS:
        raise ValueError(f"一次最多探测 {_MAX_COMMANDS} 个 Tcl 命令。")

    normalized: list[str] = []
    seen: set[str] = set()
    for command in selected:
        if not isinstance(command, str) or not _COMMAND_NAME_RE.fullmatch(command):
            raise ValueError(
                f"非法 Tcl command name: {command!r}。只接受不含空白或脚本语法的精确命令名。"
            )
        if command not in seen:
            normalized.append(command)
            seen.add(command)
    return normalized


def build_capability_probe(commands: Iterable[str]) -> str:
    """Build a Tcl 8.5-compatible, read-only exact-name probe."""
    normalized = normalize_capability_commands(commands)
    command_list = " ".join(tcl_quote(command) for command in normalized)
    return (
        "set __vmcp_caps {}\n"
        f"foreach __vmcp_cmd [list {command_list}] {{\n"
        "    set __vmcp_present [expr {[llength [info commands $__vmcp_cmd]] > 0}]\n"
        "    lappend __vmcp_caps \"$__vmcp_cmd=$__vmcp_present\"\n"
        "}\n"
        "join $__vmcp_caps \"\\n\""
    )


def parse_capability_probe(output: str, commands: Iterable[str]) -> dict[str, bool | None]:
    """Parse probe output; missing or malformed rows remain ``None`` (unknown)."""
    normalized = normalize_capability_commands(commands)
    availability: dict[str, bool | None] = {
        command: None for command in normalized
    }
    for raw_line in output.splitlines():
        name, separator, value = raw_line.strip().partition("=")
        if separator and name in availability and value in {"0", "1"}:
            availability[name] = value == "1"
    return availability


async def probe_command_capabilities(
    session: BaseSession,
    commands: Iterable[str] | None = None,
    *,
    timeout: float = 15.0,
) -> dict[str, bool | None]:
    """Probe an active Vivado interpreter without executing target commands."""
    normalized = normalize_capability_commands(commands)
    result = await session.execute(
        build_capability_probe(normalized),
        timeout=timeout,
    )
    if result.is_error:
        raise RuntimeError(f"Vivado capability probe 失败: {result.summary}")
    return parse_capability_probe(result.output, normalized)


def group_capabilities(
    availability: dict[str, bool | None],
) -> dict[str, dict[str, bool | None]]:
    """Project the default flat result into its documented command groups."""
    return {
        group: {
            command: availability[command]
            for command in commands
            if command in availability
        }
        for group, commands in DEFAULT_CAPABILITY_GROUPS.items()
        if any(command in availability for command in commands)
    }
