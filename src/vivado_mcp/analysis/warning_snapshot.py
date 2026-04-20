"""CRITICAL WARNING 快照与差分工具。

用途:每次调用 ``get_critical_warnings`` 后,把当次 CW 列表持久化成 JSON 快照;
下一次如果用户启用 ``compare_with_last``,读上次快照与本次对比,报告:

- 已消除 (resolved):上次有这次没 —— 告诉用户"你修对了"
- 新出现 (newly_added):这次有上次没 —— 警告"可能改坏了什么"
- 仍存在 (persistent):两次都有 —— 需要继续排查

存储位置优先级:
1. ``<project_dir>/.vmcp/last_cw_{run_name}.json``
2. ``~/.claude/vivado-mcp/last_cw_{run_name}.json`` (fallback)

本模块不依赖 Vivado,不做任何 I/O 之外的副作用,方便单元测试。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from vivado_mcp.analysis.warning_parser import (
    CriticalWarning,
    WarningGroup,
    WarningReport,
    group_warnings,
)

logger = logging.getLogger(__name__)


# ====================================================================== #
#  数据结构
# ====================================================================== #


@dataclass
class WarningDiff:
    """两次 CW 快照的差分结果。

    - ``resolved``:上次有这次没 → 已消除
    - ``newly_added``:这次有上次没 → 新出现
    - ``persistent``:两次都有 → 仍存在

    每个列表的元素是 ``CriticalWarning``,便于后续按 warning_id 分组展示。
    """

    resolved: list[CriticalWarning] = field(default_factory=list)
    newly_added: list[CriticalWarning] = field(default_factory=list)
    persistent: list[CriticalWarning] = field(default_factory=list)


# ====================================================================== #
#  指纹算法:给每条 CW 生成稳定 key,用于集合差集
# ====================================================================== #

# 用于从 message 里剥掉噪声(行号、时间戳)的正则
# Vivado 消息里常见的"瞬态片段":
#   - "at 2023-01-01 ..."(时间戳,通常不带但预留)
#   - "[filename.xdc:15]" —— 行号位会随 XDC 改动漂移,但同一文件名应保留
_RE_FILE_LINE = re.compile(r"\[(\S+?\.\w+):\d+\]")


def _normalize_message(msg: str) -> str:
    """把消息规范化,用于指纹:

    - ``[foo.xdc:15]`` → ``[foo.xdc]`` (去掉行号,避免行号漂移导致误判"新 CW")
    - 压缩多空白
    """
    msg = _RE_FILE_LINE.sub(r"[\1]", msg)
    msg = re.sub(r"\s+", " ", msg).strip()
    return msg


def _fingerprint(cw: CriticalWarning) -> str:
    """给一条 CW 生成稳定指纹,作为集合 key。

    指纹组成:``warning_id | port | pin | source_file | normalized_message``。
    ``message`` 先去行号再 hash,这样 XDC 里行号变化不会被误判为"新 CW"。
    消息哈希只取前 16 hex 即可(冲突概率极低,且保持 key 短)。
    """
    norm = _normalize_message(cw.message)
    msg_hash = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]
    return f"{cw.warning_id}|{cw.port}|{cw.pin}|{cw.source_file}|{msg_hash}"


# ====================================================================== #
#  持久化路径
# ====================================================================== #


def _resolve_snapshot_path(run_name: str, project_dir: str | None) -> Path:
    """按优先级决定快照 JSON 路径。

    1. ``<project_dir>/.vmcp/last_cw_{run_name}.json`` —— 如果 project_dir 可用且目录可写
    2. ``~/.claude/vivado-mcp/last_cw_{run_name}.json`` —— 兜底

    目录不存在会自动 ``mkdir(exist_ok=True, parents=True)``。
    ``project_dir`` 创建失败(只读/不存在等)时静默降级到 fallback,保证快照逻辑
    不会中断主流程。
    """
    filename = f"last_cw_{run_name}.json"

    if project_dir:
        try:
            vmcp_dir = Path(project_dir) / ".vmcp"
            vmcp_dir.mkdir(parents=True, exist_ok=True)
            # 权限探针:确认目录可写(Windows 上只读盘会卡这一步)
            probe = vmcp_dir / ".vmcp_write_probe"
            probe.touch()
            probe.unlink()
            return vmcp_dir / filename
        except (OSError, PermissionError) as e:
            logger.warning(
                "项目目录 %s 不可写,快照降级到 ~/.claude/vivado-mcp/: %s",
                project_dir,
                e,
            )

    fallback_dir = Path.home() / ".claude" / "vivado-mcp"
    fallback_dir.mkdir(parents=True, exist_ok=True)
    return fallback_dir / filename


# ====================================================================== #
#  序列化 / 反序列化
# ====================================================================== #


def _report_to_dict(report: WarningReport) -> dict:
    """把 WarningReport 打包成可 JSON 化的 dict。

    只保留"未来做 diff 需要用到"的信息:计数 + 分组 metadata。真正用来算 diff
    的是和 report 一起写入的 raw_cws(原始 CW 列表),见 ``snapshot_cw``。
    """
    return {
        "errors": report.errors,
        "critical_warnings": report.critical_warnings,
        "warnings": report.warnings,
        # groups 留着做 metadata 展示,算 diff 不靠它们
        "groups": [asdict(g) for g in report.groups],
        "error_groups": [asdict(g) for g in report.error_groups],
    }


def _raw_cws_to_list(cws: list[CriticalWarning]) -> list[dict]:
    """把 CriticalWarning 列表序列化为可 JSON 化的 list。"""
    return [asdict(cw) for cw in cws]


def _list_to_raw_cws(data: list[dict]) -> list[CriticalWarning]:
    """反序列化回 CriticalWarning 列表,忽略未知字段,容错升级。"""
    result: list[CriticalWarning] = []
    for d in data:
        try:
            result.append(
                CriticalWarning(
                    warning_id=d.get("warning_id", "UNKNOWN"),
                    message=d.get("message", ""),
                    line_number=int(d.get("line_number", 0)),
                    source_file=d.get("source_file", ""),
                    port=d.get("port", ""),
                    pin=d.get("pin", ""),
                )
            )
        except (ValueError, TypeError) as e:
            logger.warning("快照中某条 CW 反序列化失败,已跳过: %s", e)
    return result


# ====================================================================== #
#  公开 API
# ====================================================================== #


def snapshot_cw(
    report: WarningReport,
    raw_cws: list[CriticalWarning],
    run_name: str,
    project_dir: str | None,
) -> Path:
    """把当次 CW 原始列表 + 报告快照写入 JSON 文件。

    Args:
        report: 本次生成的 WarningReport(计数 + 分组)。
        raw_cws: 本次 parse_critical_warnings 得到的原始 CW 列表
            (diff 要用它来算指纹,不能只拿分组后的 message_template)。
        run_name: run 名(如 impl_1 / synth_1),作为文件名后缀。
        project_dir: Vivado 项目目录,可为 None(走 fallback)。

    Returns:
        实际写入的 Path,调用方可以据此提示用户"快照已保存到 xxx"。
    """
    path = _resolve_snapshot_path(run_name, project_dir)
    payload = {
        "version": 1,
        "run_name": run_name,
        "report": _report_to_dict(report),
        "raw_cws": _raw_cws_to_list(raw_cws),
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def load_snapshot(
    run_name: str, project_dir: str | None
) -> tuple[WarningReport | None, list[CriticalWarning]]:
    """读上次快照,不存在或损坏返回 ``(None, [])``。

    Returns:
        (report, raw_cws) —— 其中 raw_cws 是做 diff 需要的原始列表。
    """
    path = _resolve_snapshot_path(run_name, project_dir)
    if not path.exists():
        return None, []

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.warning("读取快照 %s 失败: %s", path, e)
        return None, []

    try:
        r = payload.get("report", {})
        raw_cws = _list_to_raw_cws(payload.get("raw_cws", []))

        # groups/error_groups 只是展示用;反序列化后不回填,因为调用方只需要
        # raw_cws 做 diff。但仍保留 counts 以备展示。
        report = WarningReport(
            errors=int(r.get("errors", 0)),
            critical_warnings=int(r.get("critical_warnings", 0)),
            warnings=int(r.get("warnings", 0)),
            groups=[],  # 懒得还原 WarningGroup,用不上
            error_groups=[],
        )
        return report, raw_cws
    except (KeyError, ValueError, TypeError) as e:
        logger.warning("快照 %s 结构异常: %s", path, e)
        return None, []


def diff_warnings(
    prev_cws: list[CriticalWarning],
    curr_cws: list[CriticalWarning],
) -> WarningDiff:
    """按指纹对比两次 CW 列表,分出 resolved / newly_added / persistent。

    同一指纹在同一列表里可能出现多次(同类 CW 多条),我们按"出现次数"的集合差
    处理:

    - prev 有 3 条,curr 有 1 条 → resolved 得 2 条(减少的)
    - prev 无,curr 有 2 条 → newly_added 2 条
    - 两边都有相同指纹 → persistent 取较小次数

    这样做的好处:用户把 16 条 GT_PIN_CONFLICT 改到 10 条,diff 能明确显示
    "消除了 6 条"而不是"仍存在"。
    """
    # 建两个 fingerprint → [CW, ...] 的字典(保留原序)
    prev_buckets: dict[str, list[CriticalWarning]] = {}
    for cw in prev_cws:
        prev_buckets.setdefault(_fingerprint(cw), []).append(cw)

    curr_buckets: dict[str, list[CriticalWarning]] = {}
    for cw in curr_cws:
        curr_buckets.setdefault(_fingerprint(cw), []).append(cw)

    resolved: list[CriticalWarning] = []
    newly_added: list[CriticalWarning] = []
    persistent: list[CriticalWarning] = []

    all_fps = set(prev_buckets.keys()) | set(curr_buckets.keys())
    for fp in all_fps:
        prev_list = prev_buckets.get(fp, [])
        curr_list = curr_buckets.get(fp, [])
        common = min(len(prev_list), len(curr_list))

        # 两边都有的那部分算 persistent,取 curr 侧(message 最新)
        persistent.extend(curr_list[:common])

        # prev 多出来的 → resolved
        if len(prev_list) > common:
            resolved.extend(prev_list[common:])

        # curr 多出来的 → newly_added
        if len(curr_list) > common:
            newly_added.extend(curr_list[common:])

    return WarningDiff(
        resolved=resolved,
        newly_added=newly_added,
        persistent=persistent,
    )


# ====================================================================== #
#  格式化
# ====================================================================== #


def _group_summary_line(group: WarningGroup, prefix: str) -> str:
    """一行摘要:``[prefix][Vivado 12-1411] (3 次) — GT_PIN_CONFLICT``。"""
    # message_template 太长会把输出撑爆,截到 120 字
    snippet = group.message_template
    if len(snippet) > 120:
        snippet = snippet[:117] + "..."
    return (
        f"  {prefix}[{group.warning_id}] ({group.count} 次) "
        f"— {group.category}\n"
        f"    示例: {snippet}"
    )


def format_diff_report(diff: WarningDiff) -> str:
    """把 WarningDiff 渲染成中文 markdown 片段。

    输出格式示例::

        === CW 差分报告(对比上次快照)===
        修复效果:已消除 3 条 / 新出现 1 条 / 仍存在 8 条

        [-] 已消除(3):
          - [Vivado 12-1411] (3 次) — GT_PIN_CONFLICT
        [+] 新出现(1):
          - [Vivado 18-5014] (1 次) — UNKNOWN
        [=] 仍存在(8):
          - [Vivado 12-1395] (8 次) — UNKNOWN

    消除的要放在最前面,强化用户"修对了"的正反馈。
    """
    lines: list[str] = []
    lines.append("=== CW 差分报告(对比上次快照)===")
    lines.append(
        f"修复效果:已消除 {len(diff.resolved)} 条 / "
        f"新出现 {len(diff.newly_added)} 条 / "
        f"仍存在 {len(diff.persistent)} 条"
    )

    # 消除的(按 warning_id 聚合展示)
    if diff.resolved:
        lines.append("")
        resolved_groups = group_warnings(diff.resolved)
        lines.append(f"[-] 已消除({len(diff.resolved)}):")
        for g in resolved_groups:
            lines.append(_group_summary_line(g, "- "))
        lines.append("  → 修复生效,继续保持。")
    else:
        # 显式告诉用户"啥都没消除",避免他以为差分没跑
        lines.append("")
        lines.append("[-] 已消除(0):(无)")

    # 新出现的(需要警告)
    if diff.newly_added:
        lines.append("")
        new_groups = group_warnings(diff.newly_added)
        lines.append(f"[+] 新出现({len(diff.newly_added)}):")
        for g in new_groups:
            lines.append(_group_summary_line(g, "+ "))
        lines.append("  !! 这些是改动后才冒出来的,检查最近的修改是否引入了新问题。")

    # 仍存在的
    if diff.persistent:
        lines.append("")
        persist_groups = group_warnings(diff.persistent)
        lines.append(f"[=] 仍存在({len(diff.persistent)}):")
        for g in persist_groups:
            lines.append(_group_summary_line(g, "= "))

    # 终极判语
    lines.append("")
    if not diff.resolved and not diff.newly_added:
        lines.append("结论:CW 列表与上次完全一致,本次修改未对 CW 造成影响。")
    elif diff.resolved and not diff.newly_added:
        lines.append("结论:CW 只减不增,修复方向正确。")
    elif diff.newly_added and not diff.resolved:
        lines.append("结论:出现了新 CW 但没消除任何旧 CW,建议回滚检查。")
    else:
        lines.append("结论:有消除也有新增,逐条核对 [+] 分组是否是预期变化。")

    return "\n".join(lines)
