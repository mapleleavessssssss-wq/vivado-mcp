"""Locate and read completed Vivado run reports without reopening a design."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vivado_mcp.vivado.tcl_utils import decode_vivado_output

_MAX_REPORT_BYTES = 50 * 1024 * 1024


@dataclass(frozen=True)
class GeneratedReport:
    """A validated report file selected from one completed run directory."""

    path: Path
    text: str
    size: int
    mtime: float


def _report_score(path: Path, report_type: str) -> int:
    name = path.name.lower()
    if report_type == "timing":
        return (
            (100 if "timing_summary" in name else 0)
            + (30 if "routed" in name else 0)
            + (10 if "timing" in name else 0)
        )
    if report_type == "utilization":
        return (
            (100 if "utilization" in name else 0)
            + (30 if "routed" in name or "placed" in name else 0)
            + (10 if "util" in name else 0)
        )
    raise ValueError(f"未知 generated report type: {report_type}")


def _content_matches(text: str, report_type: str) -> bool:
    if report_type == "timing":
        return "Design Timing Summary" in text
    if report_type == "utilization":
        return "Utilization" in text and (
            "Slice LUTs" in text or "CLB LUTs" in text or "Slice Logic" in text
        )
    return False


def find_generated_report(
    run_directory: str,
    report_type: str,
) -> GeneratedReport | None:
    """Return the best validated root-level ``*.rpt`` candidate, newest on ties."""
    run_dir = Path(run_directory)
    if not run_directory or not run_dir.is_dir():
        return None

    try:
        candidates = sorted(
            run_dir.glob("*.rpt"),
            key=lambda path: (_report_score(path, report_type), path.stat().st_mtime),
            reverse=True,
        )
    except OSError:
        return None

    for path in candidates:
        try:
            stat = path.stat()
            if stat.st_size <= 0 or stat.st_size > _MAX_REPORT_BYTES:
                continue
            text = decode_vivado_output(path.read_bytes())
        except OSError:
            continue
        if _content_matches(text, report_type):
            return GeneratedReport(
                path=path.resolve(),
                text=text,
                size=stat.st_size,
                mtime=stat.st_mtime,
            )
    return None
