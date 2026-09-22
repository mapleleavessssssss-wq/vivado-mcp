"""时序证据、离线输入及保守基线比较；不依赖 Vivado，不写入文件。"""

import hashlib
import json
import math
import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from vivado_mcp.analysis.timing_parser import TimingReport, summary_tokens
from vivado_mcp.analysis.warning_parser import parse_pre_bitstream

MAX_REPORT_BYTES = 16 * 1024 * 1024
MAX_BASELINE_BYTES = 4 * 1024 * 1024
_FIELDS = (
    "worst_slack_ns", "total_negative_slack_ns", "failing_endpoints", "total_endpoints",
)
_GROUPS = ("setup", "hold", "pulse_width")


def read_bounded_text(filename: str, limit: int) -> str:
    """限量读取 UTF-8 文本，拒绝目录、二进制及过大文件。"""
    path = Path(filename).expanduser()
    if not path.is_file():
        raise ValueError(f"文件不存在或不是普通文件: {path}")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"文件超过 {limit} 字节限制: {path}")
    text = data.decode("utf-8-sig")
    if "\x00" in text:
        raise ValueError("输入包含 NUL，需提供 UTF-8 报告文本")
    return text


def report_context(raw: str) -> tuple[str, dict]:
    """只接受报告头的设计阶段，不从 run 完成状态推断当前打开设计。"""
    header = raw.split("Design Timing Summary", 1)[0]

    def field(name: str) -> str | None:
        match = re.search(rf"^\s*\|?\s*{name}\s*:\s*(.+?)\s*$", header, re.M | re.I)
        return match.group(1) if match else None

    state = (field("Design State") or "").lower()
    stage = {
        "synthesized": "post-synth", "synthesised": "post-synth",
        "fully placed": "post-place", "placed": "post-place",
        "fully routed": "post-route", "routed": "post-route",
    }.get(state, "unknown")
    return stage, {
        "design": field("Design"), "device": field("Device"),
        "constraint_fingerprint": None,
    }


def physical_design_stage(raw: str) -> tuple[str, dict]:
    """Vivado 2019 无 Design State 时，从当前网表路由计数识别两种确定状态。

    不推断中间布局状态。缺项、空设计、错误、部分路由均返回 unknown。
    全部未放置的当前网表标记为 post-synth（布线前估算），并保留判断依据。
    """
    lines = [line.removeprefix("VMCP_TIMING_ROUTE:") for line in raw.splitlines()
             if line.startswith("VMCP_TIMING_ROUTE:")]
    route_report = "\n".join(lines)
    counts = {}
    for name, value in re.findall(r"# of ([\w ]+?)\.+\s*:\s*(\d+)\s*:", route_report):
        counts[name.strip().replace(" ", "_")] = int(value)
    evidence = {"method": "current_design_route_counts", "counts": counts,
                "report": route_report[:4096]}
    if "VMCP_TIMING_ROUTE_ERROR:" in raw or not counts:
        return "unknown", evidence
    logical = counts.get("logical_nets", 0)
    routable = counts.get("routable_nets")
    unnecessary = counts.get("nets_not_needing_routing")
    if not logical or counts.get("nets_with_routing_errors") != 0:
        return "unknown", evidence
    if (routable is not None and routable > 0 and unnecessary is not None
            and counts.get("fully_routed_nets") == routable
            and logical == routable + unnecessary
            and counts.get("nets_with_no_placed_pins", 0) == 0):
        return "post-route", evidence
    if (counts.get("nets_with_no_placed_pins") == logical
            and routable == 0 and unnecessary == 0):
        return "post-synth", evidence
    return "unknown", evidence


def summary_metrics(raw: str) -> tuple[dict, str, list[str]]:
    """分别解析 setup/hold/pulse 数值；缺失不是零，部分违例仍保留。"""
    tokens = summary_tokens(raw)
    metrics = {group: dict.fromkeys(_FIELDS) for group in _GROUPS}
    diagnostics = []
    if tokens is None:
        return metrics, "unrecognized", ["未识别 Design Timing Summary 总表"]
    if re.search(r"^\s*ERROR\s*:", raw, re.M):
        diagnostics.append("报告包含 ERROR，数值不可作为成功证据")
    for group, offset in zip(_GROUPS, (0, 4, 8), strict=True):
        if len(tokens) < offset + 4:
            continue
        for index, name in enumerate(_FIELDS):
            token = tokens[offset + index]
            if token.upper() in ("NA", "N/A", "--"):
                continue
            try:
                value = float(token) if index < 2 else int(token)
                if ((index < 2 and not math.isfinite(value))
                        or (index >= 2 and not 0 <= value <= 2**63 - 1)):
                    raise ValueError("非法数值")
                metrics[group][name] = value
            except ValueError:
                diagnostics.append(f"{group}.{name} 非法数值: {token[:40]}")
        values = metrics[group]
        failing, total = values["failing_endpoints"], values["total_endpoints"]
        if failing is not None and total is not None and failing > total:
            diagnostics.append(f"{group} 失败端点大于总端点")
        if values["total_negative_slack_ns"] is not None:
            if values["total_negative_slack_ns"] > 0:
                diagnostics.append(f"{group} 累计负裕量不应为正数")
    if diagnostics:
        return metrics, "invalid", diagnostics
    populated = any(v is not None for group in metrics.values() for v in group.values())
    if not populated:
        return metrics, "no_timing_data", []
    if not any((group["total_endpoints"] or 0) > 0 for group in metrics.values()):
        return metrics, "no_timing_data", ["摘要没有可分析端点"]
    required = [metrics[group][key] for group in ("setup", "hold") for key in _FIELDS]
    partial = any(value is None for value in required)
    partial |= any(metrics[g]["total_endpoints"] == 0 for g in ("setup", "hold"))
    return metrics, "partial" if partial else "ok", []


def evidence_verdict(metrics: dict, parse_status: str, stage: str) -> dict:
    """结论仅覆盖已观察检查，完整签核需要约束覆盖、CDC 和 DRC 等证据。"""
    reasons = []
    if parse_status in ("unrecognized", "invalid", "error", "no_timing_data"):
        status = "unavailable"
        reasons.append(f"时序证据不可用: {parse_status}")
    elif any(
        (g["worst_slack_ns"] is not None and g["worst_slack_ns"] < 0)
        or (g["total_negative_slack_ns"] is not None and g["total_negative_slack_ns"] < 0)
        or (g["failing_endpoints"] is not None and g["failing_endpoints"] > 0)
        for g in metrics.values()
    ):
        status = "violated"
        reasons.append("至少一个已观察的 setup/hold/pulse-width 检查存在违例")
    elif parse_status != "ok":
        status = "incomplete"
        reasons.append("setup/hold 摘要缺少有效端点或数值")
    else:
        status = "met_observed_checks"
        reasons.append("已观察的摘要数值满足要求；未观察的检查不视为通过")
    if stage != "post-route":
        reasons.append(f"当前阶段 {stage}，不代表布线后结果")
    if any(v is None for v in metrics["pulse_width"].values()):
        reasons.append("pulse-width 数据不完整")
    reasons.append("未证明约束覆盖、CDC、DRC 或硬件功能，不能作为完整签核")
    return {"status": status, "signoff": False, "reasons": reasons}


def make_timing_evidence(
    raw: str, report: TimingReport, *, session_id: str = "", report_file: str = "",
    live_stage_output: str = "",
) -> dict:
    """构造版本化 JSON 快照，由调用方自行保存作基线。"""
    stage, context = report_context(raw)
    stage_source = "report_header" if stage != "unknown" else "unknown"
    physical_stage, stage_evidence = physical_design_stage(live_stage_output)
    if not report_file and stage == "unknown" and physical_stage != "unknown":
        stage = physical_stage
        stage_source = "live_route_status"
    metrics, parse_status, diagnostics = summary_metrics(raw)
    if report.violating_paths_error:
        diagnostics.append(f"违例路径查询降级: {report.violating_paths_error}")
    return {
        "schema_version": 1, "kind": "vivado_timing",
        "provenance": {
            "source": "report_file" if report_file else "live",
            "session_id": session_id or None,
            "report_file": str(Path(report_file).expanduser().resolve()) if report_file else None,
            "sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "stage_source": stage_source,
            "stage_evidence": stage_evidence if live_stage_output else None,
        },
        "stage": stage, "context": context, "parse_status": parse_status,
        "metrics": metrics, "verdict": evidence_verdict(metrics, parse_status, stage),
        "comparison": None, "diagnostics": diagnostics,
        "paths": [asdict(path) for path in report.paths[:50]],
        "paths_truncated": len(report.paths) > 50,
        "violating_paths": [asdict(path) for path in report.violating_paths[:15]],
    }


def timing_error(message: str, session_id: str = "", report_file: str = "") -> dict:
    """即使会话/文件失败，JSON 模式仍返回同一证据结构。"""
    from vivado_mcp.analysis.timing_parser import parse_timing_summary

    result = make_timing_evidence(
        "", parse_timing_summary(""), session_id=session_id,
    )
    # 文件名本身可能非法，错误处理不能再次 resolve 该路径。
    result["provenance"]["source"] = "report_file" if report_file else "live"
    result["provenance"]["report_file"] = report_file or None
    result["parse_status"] = "error"
    result["error"] = message
    result["provenance"]["sha256"] = None
    result["verdict"] = evidence_verdict(result["metrics"], "error", "unknown")
    result["diagnostics"] = [message]
    return result


def compare_timing_baseline(current: dict, filename: str) -> dict:
    """核验旧快照，匹配上下文才输出观察差值，绝不声称约束等价或优化成功。"""
    result = {"status": "error", "authoritative": False, "reasons": [], "deltas": {}}
    try:
        baseline = json.loads(read_bounded_text(filename, MAX_BASELINE_BYTES))
        if (not isinstance(baseline, dict) or baseline.get("schema_version") != 1
                or baseline.get("kind") != "vivado_timing"):
            raise ValueError("baseline_file 必须是 schema_version=1 的完整 vivado_timing JSON")
        for key in ("context", "metrics", "provenance"):
            if not isinstance(baseline.get(key), dict):
                raise ValueError(f"基线缺少 {key} 对象")
        for group in _GROUPS:
            values = baseline["metrics"].get(group)
            if not isinstance(values, dict) or any(key not in values for key in _FIELDS):
                raise ValueError(f"基线缺少 {group} 完整指标")
            for name, value in values.items():
                if name not in _FIELDS or value is None:
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"基线 {group}.{name} 不是数值")
                if isinstance(value, float) and not math.isfinite(value):
                    raise ValueError(f"基线 {group}.{name} 不是有限数值")
                if isinstance(value, int) and abs(value) > 2**63 - 1:
                    raise ValueError(f"基线 {group}.{name} 超出整数范围")
                if "endpoints" in name and (
                    not isinstance(value, int) or not 0 <= value <= 2**63 - 1
                ):
                    raise ValueError(f"基线 {group}.{name} 不是非负整数")
            failing, total = values["failing_endpoints"], values["total_endpoints"]
            if failing is not None and total is not None and failing > total:
                raise ValueError(f"基线 {group} 失败端点大于总端点")
            if group in ("setup", "hold") and (
                any(values[key] is None for key in _FIELDS) or not total
            ):
                result["status"] = "incomparable"
                result["reasons"].append(f"基线 {group} 无完整端点证据")
                return result
        if baseline.get("parse_status") != "ok" or current["parse_status"] != "ok":
            result["status"] = "incomparable"
            result["reasons"].append("当前或基线摘要缺少可比较的完整数值")
            return result
        for field in ("design", "device"):
            old, new = baseline["context"].get(field), current["context"].get(field)
            if not old or not new or old != new:
                result["reasons"].append(f"{field} 未知或不匹配")
        old_stage, stage = baseline.get("stage"), current["stage"]
        if stage == "unknown" or old_stage != stage:
            result["reasons"].append("设计阶段未知或不匹配")
        old_fp = baseline["context"].get("constraint_fingerprint")
        new_fp = current["context"].get("constraint_fingerprint")
        if old_fp and new_fp and old_fp != new_fp:
            result["reasons"].append("约束上下文不匹配")
        if result["reasons"]:
            result["status"] = "incomparable"
            return result
        result["status"] = "observational"
        result["reasons"].append(
            "设计名/器件/阶段匹配，但完整应用约束及工程身份未验证；差值不证明优化成功"
        )
        for group in _GROUPS:
            changes = {}
            for name in _FIELDS:
                old, new = baseline["metrics"][group][name], current["metrics"][group][name]
                if old is not None and new is not None:
                    changes[name] = {"baseline": old, "current": new, "delta": new - old}
            result["deltas"][group] = changes
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        result["reasons"].append(f"读取基线失败: {exc}")
    return result


def format_readiness_evidence(
    pre_raw: str, timing_raw: str | None, timing_error_message: str, impl_run: str,
) -> tuple[str, list[str]]:
    """纯函数生成有限范围的 readiness 判定及需记录的降级原因。"""
    status, cw_count, samples = parse_pre_bitstream(pre_raw)
    is_routed = bool(re.fullmatch(r"(?:route_design|write_bitstream) Complete!?", status))
    blockers, warnings, diagnostics = [], [], []
    degraded = False
    if "ERROR" in status.upper():
        blockers.append(f"{impl_run} 执行错误: {status}")
    elif not status or status.upper() == "UNKNOWN":
        warnings.append(f"{impl_run} 状态无法读取")
        degraded = True
    elif not is_routed:
        blockers.append(f"{impl_run} 未完成布线(当前状态: {status})")
    if cw_count >= 5:
        blockers.append(f"CRITICAL WARNING 数量过多: {cw_count} 条")
    elif cw_count > 0:
        warnings.append(f"存在 {cw_count} 条 CRITICAL WARNING,建议排查")
    elif cw_count < 0:
        warnings.append("CRITICAL WARNING 无法读取，不能视为 0 条")
        degraded = True

    timing_line = ""
    if timing_raw is None:
        warnings.append(f"未能读取时序摘要: {timing_error_message}")
        diagnostics.append(f"check_bitstream_readiness 时序查询失败: {timing_error_message}")
        degraded = True
    else:
        metrics, parse_status, parse_diagnostics = summary_metrics(timing_raw)
        stage, context = report_context(timing_raw)
        if stage == "unknown":
            stage, _ = physical_design_stage(pre_raw)
        verdict = evidence_verdict(metrics, parse_status, stage)
        if verdict["status"] == "violated":
            blockers.append("时序违例(setup/hold/pulse-width 摘要未满足)")
        if parse_status == "ok":
            setup, hold = metrics["setup"], metrics["hold"]
            timing_line = (
                f"  WNS = {setup['worst_slack_ns']:+.3f} ns  "
                f"WHS = {hold['worst_slack_ns']:+.3f} ns  "
                f"失败端点 = {setup['failing_endpoints']}/{setup['total_endpoints']}"
            )
            if stage != "post-route":
                warnings.append(f"当前报告阶段 {stage}，未确认布线后时序")
                degraded = True
            top_match = re.search(r"^VMCP_PRE_BIT_TOP:(.*)$", pre_raw, re.M)
            run_top = top_match.group(1).strip() if top_match else ""
            if not run_top or run_top != context["design"]:
                warnings.append("报告设计与所选实现 run 的顶层未知或不匹配")
                degraded = True
            pulse = metrics["pulse_width"]
            if any(v is None for v in pulse.values()) or not pulse["total_endpoints"]:
                warnings.append("缺少完整 pulse-width 时序检查")
                degraded = True
        else:
            if parse_status in ("no_timing_data", "partial"):
                reason = "时序摘要为 NA 或不完整(设计无时序约束或无可分析端点)"
            else:
                reason = "时序报告格式不识别或数值无效,未能解析 Design Timing Summary"
                if parse_diagnostics:
                    reason += ": " + "; ".join(parse_diagnostics)
                degraded = True
            warnings.append(f"未能读取时序摘要: {reason}")
            diagnostics.append(
                f"check_bitstream_readiness 时序摘要降级(parse_status={parse_status}): {reason}"
            )
    if blockers:
        verdict_text = "BLOCK (阻塞,不建议生成比特流)"
    elif warnings:
        verdict_text = "WARN (存在风险或证据不足，需补充检查)"
    else:
        verdict_text = "READY (本次有限检查通过，非完整签核)"
    if degraded and not blockers:
        verdict_text += " [DEGRADED]"
    out = [f"=== 烧板前检查: {verdict_text} ===", f"实现状态: {status or 'UNKNOWN'}",
           f"CRITICAL WARNING: {cw_count if cw_count >= 0 else '无法读取'}",
           "范围: 不代替约束覆盖、CDC、DRC、供电/引脚及硬件功能验证。"]
    if timing_line:
        out.extend(["时序摘要:", timing_line])
    for title, marker, messages in (("阻塞问题:", "X", blockers), ("风险提示:", "!", warnings)):
        if messages:
            out.extend(["", title])
            out.extend(f"  [{marker}] {message}" for message in messages)
    if samples and cw_count > 0:
        out.extend(["", f"CRITICAL WARNING 样本(前 {min(len(samples), 5)} 条):"])
        out.extend(f"  - {sample}" for sample in samples[:5])
    if blockers:
        out.extend(["", "建议: 运行 get_critical_warnings 查看详情,修复后再烧板。"])
    return "\n".join(out), diagnostics
