"""Parse one-shot Vivado project/run compile profile snapshots."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_PROFILE_RE = re.compile(r"^VMCP_PROFILE:([^=]+)=(.*)$")
_RUN_RE = re.compile(r"^VMCP_PROFILE_RUN:([^|]+)\|([^=]+)=(.*)$")


def _optional_bool(value: str) -> bool | None:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    return None


def _optional_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class CompileRunProfile:
    """One Vivado project run and the cheap properties already held by Vivado."""

    name: str
    found: bool = False
    status: str = ""
    progress: str = ""
    needs_refresh: bool | None = None
    strategy: str = ""
    report_strategy: str = ""
    directory: str = ""
    auto_incremental_checkpoint: bool | None = None
    incremental_checkpoint: str = ""
    wns: float | None = None
    tns: float | None = None
    whs: float | None = None
    ths: float | None = None
    elapsed: str = ""
    properties: dict[str, str] = field(default_factory=dict)

    @property
    def is_complete(self) -> bool:
        return "complete" in self.status.lower()

    @property
    def is_running(self) -> bool:
        status = self.status.lower()
        return "running" in status or "queued" in status

    @property
    def is_error(self) -> bool:
        return "error" in self.status.lower()

    @property
    def is_out_of_date(self) -> bool:
        return self.needs_refresh is True or "out-of-date" in self.status.lower()

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "found": self.found,
            "status": self.status,
            "progress": self.progress,
            "needs_refresh": self.needs_refresh,
            "strategy": self.strategy,
            "report_strategy": self.report_strategy,
            "directory": self.directory,
            "auto_incremental_checkpoint": self.auto_incremental_checkpoint,
            "incremental_checkpoint": self.incremental_checkpoint,
            "timing_metrics": {
                "wns": self.wns,
                "tns": self.tns,
                "whs": self.whs,
                "ths": self.ths,
            },
            "elapsed": self.elapsed,
            "is_complete": self.is_complete,
            "is_running": self.is_running,
            "is_error": self.is_error,
            "is_out_of_date": self.is_out_of_date,
        }


@dataclass
class CompileProfile:
    """Project identity, process settings and selected synthesis/implementation runs."""

    project_name: str = ""
    project_dir: str = ""
    xpr_path: str = ""
    part: str = ""
    top: str = ""
    vivado_version: str = ""
    tcl_patchlevel: str = ""
    max_threads: int | None = None
    runs: dict[str, CompileRunProfile] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "project": {
                "name": self.project_name,
                "directory": self.project_dir,
                "xpr_path": self.xpr_path,
                "part": self.part,
                "top": self.top,
            },
            "vivado_version": self.vivado_version,
            "tcl_patchlevel": self.tcl_patchlevel,
            "general_max_threads": self.max_threads,
            "runs": {name: run.to_dict() for name, run in self.runs.items()},
            "error": self.error,
        }


def parse_compile_profile(raw: str) -> CompileProfile:
    """Parse ``VMCP_PROFILE*`` protocol lines without guessing missing properties."""
    profile = CompileProfile()
    scalar: dict[str, str] = {}

    for line in raw.splitlines():
        stripped = line.strip()
        run_match = _RUN_RE.match(stripped)
        if run_match is not None:
            run_name, key, value = run_match.groups()
            run = profile.runs.setdefault(run_name, CompileRunProfile(name=run_name))
            run.properties[key] = value.strip()
            continue

        profile_match = _PROFILE_RE.match(stripped)
        if profile_match is not None:
            key, value = profile_match.groups()
            scalar[key] = value.strip()

    profile.project_name = scalar.get("project_name", "")
    profile.project_dir = scalar.get("project_dir", "")
    profile.xpr_path = scalar.get("xpr_path", "")
    profile.part = scalar.get("part", "")
    profile.top = scalar.get("top", "")
    profile.vivado_version = scalar.get("vivado_version", "")
    profile.tcl_patchlevel = scalar.get("tcl_patchlevel", "")
    profile.error = scalar.get("error", "")
    try:
        profile.max_threads = int(scalar["general_max_threads"])
    except (KeyError, ValueError):
        profile.max_threads = None

    for run in profile.runs.values():
        props = run.properties
        run.found = _optional_bool(props.get("found", "")) is True
        run.status = props.get("STATUS", "")
        run.progress = props.get("PROGRESS", "")
        run.needs_refresh = _optional_bool(props.get("NEEDS_REFRESH", ""))
        run.strategy = props.get("STRATEGY", "")
        run.report_strategy = props.get("REPORT_STRATEGY", "")
        run.directory = props.get("DIRECTORY", "")
        run.auto_incremental_checkpoint = _optional_bool(
            props.get("AUTO_INCREMENTAL_CHECKPOINT", "")
        )
        run.incremental_checkpoint = props.get("INCREMENTAL_CHECKPOINT", "")
        run.wns = _optional_float(props.get("STATS.WNS", ""))
        run.tns = _optional_float(props.get("STATS.TNS", ""))
        run.whs = _optional_float(props.get("STATS.WHS", ""))
        run.ths = _optional_float(props.get("STATS.THS", ""))
        run.elapsed = props.get("STATS.ELAPSED", "")

    return profile
