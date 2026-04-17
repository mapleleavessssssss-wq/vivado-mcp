"""Vivado 项目信息解析器。

解析 QUERY_PROJECT_INFO Tcl 脚本的 ``VMCP_PROJ:*`` 输出,
为 ``get_project_info`` 工具提供结构化的项目摘要。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

_KV_RE = re.compile(r"VMCP_PROJ:(\w+)=(.*)")
_FILE_RE = re.compile(r"VMCP_PROJ_FILE:(\w+)\|([^|]+)\|(.+)")
_IP_RE = re.compile(r"VMCP_PROJ_IP:([^|]+)\|(.+)")


@dataclass(frozen=True)
class ProjectFile:
    category: str   # "source" / "xdc"
    file_type: str  # "Verilog" / "SystemVerilog" / "VHDL" / "XDC" 等
    path: str


@dataclass(frozen=True)
class ProjectIp:
    name: str
    vlnv: str


@dataclass
class ProjectInfo:
    project_name: str = ""
    project_dir: str = ""
    part: str = ""
    top: str = ""
    synth_status: str = ""
    impl_status: str = ""
    files: list[ProjectFile] = field(default_factory=list)
    ips: list[ProjectIp] = field(default_factory=list)
    error: str = ""  # 若项目未打开等错误场景

    def to_dict(self) -> dict:
        return {
            "project_name": self.project_name,
            "project_dir": self.project_dir,
            "part": self.part,
            "top": self.top,
            "synth_status": self.synth_status,
            "impl_status": self.impl_status,
            "files": [asdict(f) for f in self.files],
            "ips": [asdict(i) for i in self.ips],
            "error": self.error,
        }


def parse_project_info(raw: str) -> ProjectInfo:
    info = ProjectInfo()
    for line in raw.splitlines():
        line = line.strip()

        m_kv = _KV_RE.match(line)
        if m_kv is not None:
            key, value = m_kv.group(1), m_kv.group(2).strip()
            if key == "error":
                info.error = value
            elif key == "project_name":
                info.project_name = value
            elif key == "project_dir":
                info.project_dir = value
            elif key == "part":
                info.part = value
            elif key == "top":
                info.top = value
            elif key == "synth_status":
                info.synth_status = value
            elif key == "impl_status":
                info.impl_status = value
            # source_count / xdc_count / ip_count 由列表自行推出,忽略
            continue

        m_file = _FILE_RE.match(line)
        if m_file is not None:
            info.files.append(ProjectFile(
                category=m_file.group(1),
                file_type=m_file.group(2),
                path=m_file.group(3).strip(),
            ))
            continue

        m_ip = _IP_RE.match(line)
        if m_ip is not None:
            info.ips.append(ProjectIp(
                name=m_ip.group(1).strip(),
                vlnv=m_ip.group(2).strip(),
            ))

    return info


def format_project_info(info: ProjectInfo) -> str:
    if info.error:
        return f"[ERROR] {info.error}"

    lines: list[str] = ["=== 项目信息 ==="]
    lines.append(f"  名称:    {info.project_name}")
    lines.append(f"  目录:    {info.project_dir}")
    lines.append(f"  Part:    {info.part}")
    lines.append(f"  顶层模块: {info.top or '(未设置)'}")
    lines.append(f"  综合状态: {info.synth_status}")
    lines.append(f"  实现状态: {info.impl_status}")

    sources = [f for f in info.files if f.category == "source"]
    xdcs = [f for f in info.files if f.category == "xdc"]

    lines.append("")
    lines.append(f"源文件({len(sources)} 个):")
    for f in sources[:20]:
        lines.append(f"  [{f.file_type}] {f.path}")
    if len(sources) > 20:
        lines.append(f"  ... 还有 {len(sources) - 20} 个文件")

    lines.append("")
    lines.append(f"XDC 约束({len(xdcs)} 个):")
    for f in xdcs[:10]:
        lines.append(f"  {f.path}")
    if len(xdcs) > 10:
        lines.append(f"  ... 还有 {len(xdcs) - 10} 个文件")

    lines.append("")
    lines.append(f"IP 实例({len(info.ips)} 个):")
    for ip in info.ips[:10]:
        lines.append(f"  {ip.name}  —  {ip.vlnv}")
    if len(info.ips) > 10:
        lines.append(f"  ... 还有 {len(info.ips) - 10} 个 IP")

    return "\n".join(lines)
