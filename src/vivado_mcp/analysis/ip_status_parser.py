"""IP 状态解析器:把 Vivado ``report_ip_status -return_string`` 的表格翻成结构化数据。

用途:``get_ip_status`` 工具。老项目打开后 Vivado 常会提示"N 个 IP 需要升级",
但用户不知道是哪些 / 升级风险多大。

Vivado 2019.1 report_ip_status 典型输出:

    IP STATUS
    ---------
    IP                 Status                     Lock Status
    ------------------------------------------------------------
    axi_gpio_0         IP upgrade is required    Unlocked
    axi_bram_ctrl_0    Current                    Unlocked

老的 Vivado 或 xci-only 工程可能没有 lock 列,所以 parser 容忍缺列。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

_HEADER_RE = re.compile(r"^\s*IP\s+Status", re.IGNORECASE)
_SEP_RE = re.compile(r"^[\-=\s]+$")
_UPGRADE_KEYWORDS = (
    "upgrade is required",
    "ip upgrade required",
    "major change",
    "minor change",
    "re-customization required",
)
_CURRENT_KEYWORDS = ("current",)
_LOCKED_KEYWORDS = ("locked", "lock is on")


@dataclass(frozen=True)
class IpStatus:
    name: str
    status: str       # 原始 status 列
    lock: str         # 原始 lock 列
    needs_upgrade: bool
    is_locked: bool


@dataclass
class IpStatusReport:
    ips: list[IpStatus] = field(default_factory=list)
    raw_output: str = ""

    @property
    def need_upgrade(self) -> list[IpStatus]:
        return [ip for ip in self.ips if ip.needs_upgrade]

    @property
    def locked(self) -> list[IpStatus]:
        return [ip for ip in self.ips if ip.is_locked]

    @property
    def current(self) -> list[IpStatus]:
        return [ip for ip in self.ips if not ip.needs_upgrade]

    def to_dict(self) -> dict:
        return {
            "total": len(self.ips),
            "upgrade_required": len(self.need_upgrade),
            "locked": len(self.locked),
            "current": len(self.current),
            "ips": [asdict(ip) for ip in self.ips],
        }


def _categorize(status_text: str, lock_text: str) -> tuple[bool, bool]:
    s = status_text.lower()
    ll = lock_text.lower()
    needs_upgrade = any(k in s for k in _UPGRADE_KEYWORDS)
    # "Current" + needs_upgrade 不应该共存,以 needs_upgrade 为准
    is_locked = any(k in ll for k in _LOCKED_KEYWORDS) or "locked" in s
    return needs_upgrade, is_locked


def parse_ip_status(raw: str) -> IpStatusReport:
    """解析 report_ip_status 输出。

    策略:
    - 找到表头行(含 "IP" + "Status")
    - 跳过分隔符行
    - 每个数据行按 2+ 个连续空格切分列,前 3 列分别是 name / status / lock
    """
    report = IpStatusReport(raw_output=raw)

    lines = raw.splitlines()
    header_idx = -1
    for i, line in enumerate(lines):
        if _HEADER_RE.match(line):
            header_idx = i
            break

    if header_idx < 0:
        return report

    # 扫描 header 之后的行
    for raw_line in lines[header_idx + 1:]:
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if _SEP_RE.match(line):
            continue
        # 遇到空行或分隔符后的新标题块就停
        if line.startswith("INFO:") or line.startswith("WARNING:"):
            continue
        # 2+ 连续空格作为列分隔符
        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) < 2:
            continue
        name = parts[0].strip()
        status = parts[1].strip()
        lock = parts[2].strip() if len(parts) >= 3 else ""
        # 忽略仍然是 header 的行(header 分隔符之下 Vivado 偶尔重复输出)
        if name.lower() in ("ip", ""):
            continue
        needs_upgrade, is_locked = _categorize(status, lock)
        report.ips.append(IpStatus(
            name=name,
            status=status,
            lock=lock,
            needs_upgrade=needs_upgrade,
            is_locked=is_locked,
        ))

    return report


def format_ip_status_report(report: IpStatusReport) -> str:
    if not report.ips:
        return (
            "=== IP 状态: 无 IP 或未能解析 ===\n"
            "可能原因: 项目没有 IP 实例,或 Vivado 版本输出格式不兼容。\n"
            "原始输出前 500 字节:\n"
            + (report.raw_output[:500] if report.raw_output else "(空)")
        )

    n_upg = len(report.need_upgrade)
    n_lock = len(report.locked)
    header = "=== IP 状态: "
    if n_upg == 0:
        header += f"全部最新 ({len(report.ips)} 个 IP) ==="
    else:
        header += f"{n_upg}/{len(report.ips)} 个需要升级 ==="

    out = [header]

    if report.need_upgrade:
        out.append("")
        out.append(f"--- 需要升级 ({n_upg} 个) ---")
        for ip in report.need_upgrade:
            tag = "[锁定]" if ip.is_locked else ""
            out.append(f"  {ip.name:<30}{ip.status}  {tag}".rstrip())
        out.append("")
        out.append("建议:")
        out.append("  1. 备份 XCI 文件(在工程目录下 .srcs/sources_1/ip/)")
        out.append("  2. run_tcl(\"upgrade_ip [get_ips <ip_name>]\")  # 单个升级,验证后再批量")
        out.append("  3. 或 run_tcl(\"upgrade_ip [get_ips]\")  # 全部一次性升级(风险:配置可能失效)")
        out.append("  4. 升级后跑 run_synthesis 验证功能")

    if report.locked:
        out.append("")
        out.append(f"--- 已锁定 ({n_lock} 个) ---")
        for ip in report.locked:
            out.append(f"  {ip.name:<30}{ip.lock or ip.status}")
        out.append("锁定通常是手动设的,要改 IP 需先 `set_property IS_LOCKED 0 [get_ips <name>]`。")

    if report.current:
        out.append("")
        out.append(f"--- 已是最新 ({len(report.current)} 个) ---")
        for ip in report.current[:10]:
            out.append(f"  {ip.name}")
        if len(report.current) > 10:
            out.append(f"  ... 还有 {len(report.current) - 10} 个")

    return "\n".join(out)
