"""解析 Vivado report_cdc -details 的表格证据，不把报告无告警等同 CDC 签核。"""

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone

MAX_CDC_BYTES = 16 * 1024 * 1024
MAX_CDC_DETAILS = 500
_MAX_GROUPS = 128
_MAX_FIELD = 512
_SEVERITIES = {"critical": "Critical", "warning": "Warning", "info": "Info"}
_RULE_RE = re.compile(r"CDC-\d{1,6}\Z")
MAX_CDC_OUTPUT_BYTES = 2 * 1024 * 1024


def _base(source: str, report_file: str, session_id: str) -> dict:
    return {
        "schema_version": 1, "kind": "vivado_cdc", "success": False,
        "parse_status": "unrecognized",
        "provenance": {
            "source": source, "report_file": report_file[:2048] or None,
            "session_id": session_id[:512] or None, "sha256": None,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        },
        "design": None, "device": None, "tool_version": None,
        "summary": {"count_basis": "unavailable", "reported_checks": None,
                    "by_severity": {}, "by_rule": [], "waived_endpoints_by_rule": {}},
        "clock_pairs": [], "details": [], "detail_rows_observed": 0,
        "details_truncated": False, "groups_truncated": False, "fields_truncated": False,
        "waived_detail_rows": 0, "waiver_visibility": "unknown",
        "constraints_verified": False,
        "verdict": {"status": "unverified", "signoff": False, "reasons": [
            "report_cdc 仅覆盖源端和目的端均有时钟定义的路径；未验证完整约束覆盖",
            "同步结构的报告分类、豁免及功能协议仍需审查；本结果不证明 CDC clean",
        ]},
        "diagnostics": [],
    }


def cdc_error(
    message: str, *, source: str = "live", report_file: str = "", session_id: str = "",
) -> dict:
    """所有工具失败路径保留 JSON 结构，不再次解析可能非法的文件路径。"""
    result = _base(source, report_file, session_id)
    result["parse_status"] = "error"
    result["error"] = message[:2000]
    result["diagnostics"] = [message[:2000]]
    return result


def parse_cdc_report(
    raw: str, max_details: int = 100, *, source: str = "report_file",
    report_file: str = "", session_id: str = "",
) -> dict:
    """解析真实固定宽度摘要/明细表；计数单位是检查项，不是总线位或端点。

    rule summary 与 detail rows 是同一批检查的两个视图，不能相加。
    max_details 只限制返回明细，完整输入仍扫描计数。未知/损坏行和上下文不完整
    返回 partial；缺时钟时 Vivado 也会输出 All paths are Safely Timed，仍未签核。
    """
    if isinstance(max_details, bool) or not isinstance(max_details, int):
        raise ValueError("max_details 必须为整数")
    if not 0 <= max_details <= MAX_CDC_DETAILS:
        raise ValueError(f"max_details 必须在 0..{MAX_CDC_DETAILS} 之间")
    if len(raw.encode("utf-8")) > MAX_CDC_BYTES:
        raise ValueError("CDC 报告超过 16 MiB 限制")
    result = _base(source, report_file, session_id)
    result["provenance"]["sha256"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    diagnostics = result["diagnostics"]

    def note(message: str) -> None:
        if len(diagnostics) < 20 and message not in diagnostics:
            diagnostics.append(message)

    def bounded(value: str) -> str:
        if len(value) > _MAX_FIELD:
            result["fields_truncated"] = True
        return value[:_MAX_FIELD]

    for key, header in (("design", "Design"), ("device", "Device"),
                        ("tool_version", "Tool Version")):
        match = re.search(rf"^\s*\|?\s*{header}\s*:\s*(.+)$", raw, re.M)
        if match:
            result[key] = bounded(match.group(1).strip())
    command = re.search(r"^\s*\|?\s*Command\s*:\s*(.+)$", raw, re.M)
    if command:
        command_text = command.group(1)
        result["waiver_visibility"] = (
            "shown" if "-show_waiver" in command_text or "-waived" in command_text
            else "ignored" if "-no_waiver" in command_text else "may_be_hidden"
        )
        if re.search(r"\s-(?:from|to|cells|severity|waived)\b", command_text):
            note("报告命令限定了查询范围，不能代表整个设计")
        if "-all_checks_per_endpoint" in command_text:
            note("报告包含每个端点的多个检查项；计数不是独立端点数量")
    if not command or any(result[key] is None for key in ("design", "device", "tool_version")):
        note("报告头缺少设计、器件、工具版本或命令，来源上下文不完整")
    if "\x00" in raw or re.search(r"^\s*ERROR\s*:", raw, re.M):
        result["parse_status"] = "error"
        note("报告包含 NUL 或 ERROR，不能作为成功证据")
        return result
    if "CDC Report" not in raw:
        note("未识别 Vivado CDC Report 标题")
        return result
    if raw.count("CDC Report") > 1:
        note("输入包含多个 CDC Report，可能拼接或重复")
    if re.search(r"\b(?:truncated|omitted|limit reached|output limit)\b", raw, re.I):
        note("原始报告带截断/省略提示，证据可能不完整")

    summary_rules = {}
    detail_rules = Counter()
    unwaived_detail_rules = Counter()
    pairs = {}
    source_clock = destination_clock = cdc_type = ""
    source_pending = False
    columns = []
    table_kind = ""
    pending_header = ""
    saw_detail_header = False
    rule_rows_seen = 0
    for line_number, line in enumerate(raw.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("Source Clock:"):
            source_clock = stripped.partition(":")[2].strip()
            destination_clock = cdc_type = ""
            table_kind, columns = "", []
            source_pending = True
            continue
        if stripped.startswith("Destination Clock:"):
            if not source_pending:
                source_clock = ""
                note(f"第 {line_number} 行：目的时钟之前缺少源时钟声明")
            destination_clock = stripped.partition(":")[2].strip()
            source_pending = False
            continue
        if stripped.startswith("CDC Type:"):
            cdc_type = stripped.partition(":")[2].strip()
            continue
        if re.match(r"ID\s+Severity\s+Count\s+Description", stripped):
            pending_header, table_kind = line, "summary"
            continue
        if re.match(r"ID\s+Waived Endpoints", stripped):
            pending_header, table_kind = line, "waivers"
            continue
        if re.match(r"Row\s+ID\s+Severity\s+Description", stripped):
            pending_header, table_kind = line, "details"
            saw_detail_header = True
            continue
        if pending_header:
            if stripped and set(stripped) <= {"-", " "}:
                starts = []
                for match in re.finditer(r"-+", line):
                    starts.append(match.start())
                    if len(starts) > 9:
                        break
                expected_columns = {"summary": (4,), "waivers": (2,), "details": (8, 9)}
                if len(starts) not in expected_columns[table_kind]:
                    note(f"第 {line_number} 行：表格列数异常")
                    pending_header, columns, table_kind = "", [], ""
                    continue
                columns = [(pending_header[start:(starts[i + 1] if i + 1 < len(starts)
                                                   else None)].strip(), start,
                            starts[i + 1] if i + 1 < len(starts) else None)
                           for i, start in enumerate(starts)]
                pending_header = ""
                continue
            note(f"第 {line_number} 行：表头缺少列分隔线")
            pending_header, columns = "", []
        if not stripped:
            table_kind, columns = "", []
            continue
        if not columns or not table_kind:
            if re.match(r"(?:\d+\s+)?CDC-\d+\b", stripped):
                note(f"第 {line_number} 行：CDC 数据没有可识别表头")
            continue
        if stripped.startswith(("INFO:", "WARNING:", "CRITICAL WARNING:")):
            continue
        values = {name: line[start:end].strip() for name, start, end in columns}
        rule = values.get("ID", "")
        if table_kind == "waivers":
            count = values.get("Waived Endpoints", "")
            if not _RULE_RE.fullmatch(rule) or not re.fullmatch(r"\d{1,12}", count):
                note(f"第 {line_number} 行：豁免摘要格式无法识别")
                continue
            waived_counts = result["summary"]["waived_endpoints_by_rule"]
            if rule in waived_counts:
                note(f"规则 {rule} 的豁免摘要重复，未重复累计")
            elif len(waived_counts) < _MAX_GROUPS:
                waived_counts[rule] = int(count)
            else:
                result["groups_truncated"] = True
            continue
        severity = _SEVERITIES.get(values.get("Severity", "").lower())
        if not _RULE_RE.fullmatch(rule) or severity is None:
            note(f"第 {line_number} 行：无法识别 CDC 规则/严重级别")
            continue
        description = bounded(values.get("Description", ""))
        if table_kind == "summary":
            count = values.get("Count", "")
            if not re.fullmatch(r"\d{1,12}", count):
                note(f"第 {line_number} 行：规则计数无效")
                continue
            if rule in summary_rules:
                note(f"规则 {rule} 的摘要重复，未重复累计")
                continue
            rule_rows_seen += 1
            if len(summary_rules) >= _MAX_GROUPS:
                result["groups_truncated"] = True
                note("规则分组超过上限，摘要分组不完整")
                continue
            summary_rules[rule] = {
                "rule": rule, "severity": severity, "count": int(count),
                "description": description,
            }
            continue
        if not re.fullmatch(r"\d{1,12}", values.get("Row", "")):
            note(f"第 {line_number} 行：明细行号无效")
            continue
        if not source_clock or not destination_clock or not cdc_type:
            note(f"第 {line_number} 行：明细缺少时钟对/CDC 类型")
        startpoint, endpoint = values.get("Source (From)", ""), values.get("Destination (To)", "")
        if not startpoint or not endpoint:
            note(f"第 {line_number} 行：明细缺少源端或目的端")
            continue
        depth = values.get("Depth", "")
        if not re.fullmatch(r"\d{1,6}", depth):
            note(f"第 {line_number} 行：同步深度未知或无效")
        waived_text = values.get("Waived")
        waived = None if waived_text is None else waived_text.upper() in ("Y", "YES", "TRUE", "1")
        if waived_text is not None and waived_text.upper() not in ("Y", "YES", "TRUE", "1",
                                                                  "N", "NO", "FALSE", "0"):
            waived = None
            note(f"第 {line_number} 行：豁免标记无法识别")
        if waived:
            result["waived_detail_rows"] += 1
        rule_key = (rule, severity)
        if rule_key in detail_rules or len(detail_rules) < _MAX_GROUPS:
            detail_rules[rule_key] += 1
            if waived is not True:
                unwaived_detail_rules[rule_key] += 1
        else:
            result["groups_truncated"] = True
        result["detail_rows_observed"] += 1
        pair_key = (source_clock, destination_clock, cdc_type)
        if pair_key not in pairs and len(pairs) < _MAX_GROUPS:
            pairs[pair_key] = {"source_clock": bounded(source_clock),
                               "destination_clock": bounded(destination_clock),
                               "cdc_type": bounded(cdc_type), "detail_checks": 0,
                               "waived_detail_checks": 0,
                               "by_severity": Counter(), "by_rule": Counter()}
        if pair_key in pairs:
            pair = pairs[pair_key]
            pair["detail_checks"] += 1
            pair["waived_detail_checks"] += bool(waived)
            pair["by_severity"][severity] += 1
            if rule in pair["by_rule"] or len(pair["by_rule"]) < _MAX_GROUPS:
                pair["by_rule"][rule] += 1
            else:
                result["groups_truncated"] = True
        else:
            result["groups_truncated"] = True
        if len(result["details"]) < max_details:
            result["details"].append({
                "row": int(values["Row"]), "rule": rule, "severity": severity,
                "description": description, "source_clock": bounded(source_clock),
                "destination_clock": bounded(destination_clock), "cdc_type": bounded(cdc_type),
                "depth": int(depth) if re.fullmatch(r"\d{1,6}", depth) else None,
                "exception": bounded(values.get("Exception", "")),
                "source": bounded(startpoint), "destination": bounded(endpoint), "waived": waived,
            })

    if pending_header:
        note("输入结束于表头，报告不完整")
    result["details_truncated"] = result["detail_rows_observed"] > len(result["details"])
    result["clock_pairs"] = list(pairs.values())
    summary = result["summary"]
    if summary_rules:
        summary["count_basis"] = "rule_summary_checks"
        summary["by_rule"] = list(summary_rules.values())
        summary["reported_checks"] = sum(row["count"] for row in summary_rules.values())
        severity_counts = Counter()
        for row in summary_rules.values():
            severity_counts[row["severity"]] += row["count"]
        summary["by_severity"] = dict(severity_counts)
        expected = Counter({(row["rule"], row["severity"]): row["count"]
                            for row in summary_rules.values()})
        if expected != unwaived_detail_rules:
            note("规则摘要与已解析明细的计数不一致；可能缺失、截断或包含豁免")
    elif detail_rules:
        summary["count_basis"] = "detail_rows_only"
        summary["reported_checks"] = result["detail_rows_observed"]
        counts = Counter()
        for (rule, severity), count in detail_rules.items():
            counts[severity] += count
            if len(summary["by_rule"]) < _MAX_GROUPS:
                summary["by_rule"].append({"rule": rule, "severity": severity, "count": count})
        summary["by_severity"] = dict(counts)
        note("缺少可交叉核对的规则摘要，计数仅覆盖解析到的明细")
    else:
        note("没有可解析的 CDC 明细；安全计时文案不证明时钟已定义或不存在跨域风险")
    if not saw_detail_header and rule_rows_seen:
        note("仅有规则摘要，缺少 -details 明细")
    if result["waived_detail_rows"] or result["summary"]["waived_endpoints_by_rule"]:
        note("报告含已豁免路径；豁免不修复 CDC 结构")
    if result["groups_truncated"]:
        note("分组返回达到上限，不能据此证明没有其他风险")
    result["parse_status"] = "partial" if diagnostics else "ok"
    result["success"] = True
    if detail_rules or (summary["reported_checks"] or 0) > 0:
        result["verdict"]["status"] = "findings_observed"
    if result["waiver_visibility"] in ("may_be_hidden", "unknown"):
        result["verdict"]["reasons"].append("默认报告可能隐藏豁免项，完整豁免范围未核查")
    if result["details_truncated"] or result["fields_truncated"]:
        result["verdict"]["reasons"].append("返回明细或字段已截断，需缩小查询范围或查看原始报告")
    return result


def serialize_cdc_report(result: dict) -> str:
    """JSON 输出上限 2 MiB；超出时减去明细，再减去分组并标注截断。"""
    while True:
        text = json.dumps(result, ensure_ascii=False, allow_nan=False)
        try:
            output_size = len(text.encode("utf-8"))
        except UnicodeEncodeError:
            # JSON 允许转义代理码点；非法文件名也必须得到可传输的错误 JSON。
            text = json.dumps(result, ensure_ascii=True, allow_nan=False)
            output_size = len(text)
        if output_size <= MAX_CDC_OUTPUT_BYTES:
            return text
        if result["details"]:
            result["details"] = result["details"][:len(result["details"]) // 2]
            result["details_truncated"] = True
        elif result["clock_pairs"]:
            result["clock_pairs"] = result["clock_pairs"][:len(result["clock_pairs"]) // 2]
            result["groups_truncated"] = True
        else:
            # 正常字段均有单独上限，这里只兜底异常元数据（如过长输入路径）。
            return json.dumps(cdc_error("CDC JSON 超过 2 MiB 输出限制"), ensure_ascii=False)
        reason = "JSON 输出达到 2 MiB 限制，返回内容已进一步截断"
        if reason not in result["diagnostics"]:
            result["diagnostics"].append(reason)
