"""LTX 文件解析器（离线 ILA 探针清单）。

纯 Python 模块，不依赖 Vivado。解析 Vivado 调试探针文件 .ltx，
提取各 ILA 核的 probe 名/位宽/类型/映射网线，供连板前离线核对触发设置。

关键事实（真机 Vivado 2019.1 验证）：
  .ltx 在 2019.1 是 **JSON**，不是 XML（旧版可能是 XML）。
  本解析器嗅探首个非空字符：`{` 视为 JSON 正常解析；`<` 视为旧版 XML，
  给出明确 SKIP 提示（raise ValueError）而非崩溃。

JSON 结构关键路径：
  ltx_root
    ├─ version / minor
    └─ ltx_data[]                  # 探针集（通常 1 个，name 如 "EDA_PROBESET"）
         └─ debug_cores[]
              ├─ type == "XSDB_V3" # 调试集线器 dbg_hub（无 pins）
              └─ type == "ILA_V3"  # ILA 核（含 pins）
                   └─ pins[]
                        ├─ name        # 如 "probe0"
                        ├─ type        # 如 "DATA_TRIGGER"
                        ├─ direction   # 如 "IN"
                        ├─ isVector
                        ├─ leftIndex / rightIndex   # 位宽 = right - left + 1
                        └─ nets[]{name}             # 映射到设计中的网线名
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

# 文件大小上限（10MB）
_MAX_FILE_SIZE = 10 * 1024 * 1024

# 调试核类型
_TYPE_DBG_HUB = "XSDB_V3"
_TYPE_ILA = "ILA_V3"


# ====================================================================== #
#  数据结构
# ====================================================================== #


@dataclass(frozen=True)
class LtxProbe:
    """单个 ILA probe（探针引脚）。"""

    name: str                  # probe 名（如 "probe0"）
    probe_type: str            # probe 类型（如 "DATA_TRIGGER"）
    direction: str             # 方向（如 "IN"）
    left_index: int            # 高位下标（leftIndex）
    right_index: int           # 低位下标（rightIndex）
    is_vector: bool            # 是否为向量
    nets: tuple[str, ...] = ()  # 映射到的网线名（顶层 net，不含逐位 subnets）

    @property
    def width(self) -> int:
        """probe 位宽 = rightIndex - leftIndex + 1。"""
        return self.right_index - self.left_index + 1


@dataclass(frozen=True)
class LtxCore:
    """单个调试核（ILA 或 dbg_hub）。"""

    name: str                       # 核实例名
    core_type: str                  # 核类型（ILA_V3 / XSDB_V3）
    spec: str                       # 核规格（如 "labtools_ila_v6"）
    ip_name: str                    # IP 名（如 "ila"，dbg_hub 为空）
    probes: tuple[LtxProbe, ...] = ()  # 探针列表（dbg_hub 为空）

    @property
    def is_ila(self) -> bool:
        """是否为 ILA 核（含 probe）。"""
        return self.core_type == _TYPE_ILA

    @property
    def is_dbg_hub(self) -> bool:
        """是否为调试集线器 dbg_hub。"""
        return self.core_type == _TYPE_DBG_HUB


@dataclass(frozen=True)
class LtxConfig:
    """单个 LTX 文件的解析结果。"""

    file_path: str
    version: str                    # ltx_root.version（原样字符串）
    minor: str                      # ltx_root.minor（原样字符串）
    cores: tuple[LtxCore, ...] = ()  # 所有调试核（ILA + dbg_hub）

    @property
    def ila_cores(self) -> tuple[LtxCore, ...]:
        """仅 ILA 核。"""
        return tuple(c for c in self.cores if c.is_ila)

    @property
    def dbg_hubs(self) -> tuple[LtxCore, ...]:
        """仅 dbg_hub 核。"""
        return tuple(c for c in self.cores if c.is_dbg_hub)

    def to_dict(self) -> dict:
        """转换为可 JSON 序列化的字典。"""
        return {
            "file_path": self.file_path,
            "version": self.version,
            "minor": self.minor,
            "cores": [
                {
                    "name": c.name,
                    "type": c.core_type,
                    "spec": c.spec,
                    "ip_name": c.ip_name,
                    "is_ila": c.is_ila,
                    "is_dbg_hub": c.is_dbg_hub,
                    "probes": [
                        {
                            "name": p.name,
                            "type": p.probe_type,
                            "direction": p.direction,
                            "left_index": p.left_index,
                            "right_index": p.right_index,
                            "width": p.width,
                            "is_vector": p.is_vector,
                            "nets": list(p.nets),
                        }
                        for p in c.probes
                    ],
                }
                for c in self.cores
            ],
        }


# ====================================================================== #
#  解析函数
# ====================================================================== #


def _parse_probe(pin: dict) -> LtxProbe:
    """从单个 pin 字典构造 LtxProbe。"""
    # leftIndex/rightIndex 在真文件里是整数，做容错转换（缺失按 0 / 单位宽处理）
    left = pin.get("leftIndex", 0)
    right = pin.get("rightIndex", 0)
    try:
        left_index = int(left)
    except (TypeError, ValueError):
        left_index = 0
    try:
        right_index = int(right)
    except (TypeError, ValueError):
        right_index = 0

    nets = tuple(
        str(n.get("name", ""))
        for n in pin.get("nets", [])
        if isinstance(n, dict) and n.get("name")
    )

    return LtxProbe(
        name=str(pin.get("name", "")),
        probe_type=str(pin.get("type", "")),
        direction=str(pin.get("direction", "")),
        left_index=left_index,
        right_index=right_index,
        is_vector=bool(pin.get("isVector", False)),
        nets=nets,
    )


def _parse_core(core: dict) -> LtxCore:
    """从单个 debug_core 字典构造 LtxCore。"""
    probes = tuple(
        _parse_probe(p)
        for p in core.get("pins", [])
        if isinstance(p, dict)
    )
    return LtxCore(
        name=str(core.get("name", "")),
        core_type=str(core.get("type", "")),
        spec=str(core.get("spec", "")),
        ip_name=str(core.get("ipName", "")),
        probes=probes,
    )


def parse_ltx(file_path: str) -> LtxConfig:
    """解析单个 LTX 文件，提取 ILA 探针清单。

    Args:
        file_path: LTX 文件的绝对路径。

    Returns:
        LtxConfig 实例。

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: 文件过大、为旧版 XML 格式、或 JSON 结构非法。
    """
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"LTX 文件不存在: {file_path}")

    file_size = os.path.getsize(file_path)
    if file_size > _MAX_FILE_SIZE:
        raise ValueError(
            f"LTX 文件过大 ({file_size / 1024 / 1024:.1f}MB)，"
            f"上限 {_MAX_FILE_SIZE / 1024 / 1024:.0f}MB"
        )

    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()

    # 嗅探首个非空字符判断格式：{ = JSON，< = 旧版 XML
    stripped = text.lstrip()
    if not stripped:
        raise ValueError(f"LTX 文件为空: {file_path}")

    first_char = stripped[0]
    if first_char == "<":
        raise ValueError(
            "检测到旧版 XML 格式 .ltx，本版本仅支持 Vivado 2019.1+ 的 JSON 格式，"
            f"暂跳过（SKIP）: {file_path}"
        )
    if first_char != "{":
        raise ValueError(
            f"无法识别的 LTX 格式（首字符为 {first_char!r}，"
            f"应为 '{{' JSON 或 '<' XML）: {file_path}"
        )

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 解析失败: {e}") from e

    if not isinstance(data, dict):
        raise ValueError("LTX JSON 根不是对象")

    root = data.get("ltx_root")
    if not isinstance(root, dict):
        raise ValueError("LTX 文件缺少 ltx_root 对象")

    version = str(root.get("version", ""))
    minor = str(root.get("minor", ""))

    cores: list[LtxCore] = []
    for block in root.get("ltx_data", []):
        if not isinstance(block, dict):
            continue
        for core in block.get("debug_cores", []):
            if isinstance(core, dict):
                cores.append(_parse_core(core))

    return LtxConfig(
        file_path=file_path,
        version=version,
        minor=minor,
        cores=tuple(cores),
    )


# ====================================================================== #
#  格式化
# ====================================================================== #


def format_ltx(result: LtxConfig) -> str:
    """将 LtxConfig 格式化为人类可读的中文摘要。

    按 ILA 分组列出每个 probe（名/位宽/类型/映射 net），并单列 dbg_hub。

    Args:
        result: 解析结果。

    Returns:
        中文摘要文本。
    """
    lines: list[str] = []

    lines.append("=== LTX 探针清单 ===")
    lines.append(f"文件: {result.file_path}")
    lines.append(f"版本: {result.version}.{result.minor}")

    ila_cores = result.ila_cores
    dbg_hubs = result.dbg_hubs
    total_probes = sum(len(c.probes) for c in ila_cores)
    lines.append(
        f"ILA 核: {len(ila_cores)} 个，"
        f"dbg_hub: {len(dbg_hubs)} 个，"
        f"probe 合计: {total_probes} 个"
    )
    lines.append("")

    if not ila_cores:
        lines.append("（未发现 ILA 核）")
    for core in ila_cores:
        lines.append(f"--- ILA: {core.name} ---")
        if core.ip_name:
            lines.append(f"  IP: {core.ip_name}  规格: {core.spec}")
        if not core.probes:
            lines.append("  （无 probe）")
        for p in core.probes:
            net_str = ", ".join(p.nets) if p.nets else "(未映射)"
            lines.append(
                f"  {p.name}  [{p.right_index}:{p.left_index}] "
                f"宽={p.width}  类型={p.probe_type}"
            )
            lines.append(f"    net: {net_str}")
        lines.append("")

    if dbg_hubs:
        lines.append("--- 调试集线器 (dbg_hub) ---")
        for hub in dbg_hubs:
            lines.append(f"  {hub.name}  规格: {hub.spec}")
        lines.append("")

    return "\n".join(lines)
