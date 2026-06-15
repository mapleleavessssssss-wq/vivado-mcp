"""XPR 工程文件解析器。

纯 Python 模块，不依赖 Vivado。离线解析 Vivado 工程文件 .xpr（XML 格式），
在不启动 Vivado（``open_project`` 需 120s 且中文路径会触发 TclStackFree 崩）的
前提下，秒级抽取 part / 顶层 / 分组源文件 / XDC / IP(.xci) / runs 摘要，
供 AI 同 ``get_project_info`` 风格消费。

XPR 结构关键路径：
  <Project Version= Minor= Path=>
    ├─ <Configuration>
    │     └─ <Option Name="Part" Val="xc7k325tffg676-2"/>、ActiveSimSet、XSimRadix ...
    ├─ <FileSets>
    │     └─ <FileSet Type="DesignSrcs"|"Constrs"|"SimulationSrcs">
    │           ├─ <File Path="$PPRDIR/../sources/rtl/x.v">  （路径含宏）
    │           └─ <Config><Option Name="TopModule" Val="gauss_noise_top"/></Config>
    └─ <Runs>
          └─ <Run Id="synth_1" Type="Ft3:Synth" Part= State= ...>
                └─ <Strategy><StratHandle Name= Flow=/></Strategy>

路径宏（Vivado 工程相对宏）：
  $PPRDIR  → .xpr 所在工程目录（可离线确定地展开）
  $PSRCDIR / $PRUNDIR / $PCACHEDIR / $PIPUSERFILESDIR → 默认相对工程目录，
            真实展开依赖 live Vivado 的目录属性，离线仅按默认约定推断并原样标注。
"""

from __future__ import annotations

import os
import posixpath
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field

# 文件大小上限（10MB）
_MAX_FILE_SIZE = 10 * 1024 * 1024

# 路径宏默认展开约定（相对 $PPRDIR=工程目录）。
# 仅 $PPRDIR 可离线确定地展开；其余按 Vivado 默认布局推断（工程同名 .srcs/.runs/.cache）。
_MACRO_DEFAULTS = {
    "$PSRCDIR": "{proj}.srcs",
    "$PRUNDIR": "{proj}.runs",
    "$PCACHEDIR": "{proj}.cache",
    "$PIPUSERFILESDIR": "{proj}.ip_user_files",
}

# 视为 IP 实例的文件扩展名
_IP_EXTS = (".xci", ".bd")


# ====================================================================== #
#  数据结构
# ====================================================================== #


@dataclass(frozen=True)
class XprFile:
    """工程中的单个文件条目。"""

    fileset_name: str   # FileSet 的 Name（如 "sources_1"）
    fileset_type: str   # "DesignSrcs" / "Constrs" / "SimulationSrcs" / ...
    path_raw: str       # 原样保留的路径（含 $PPRDIR 等宏）
    path_resolved: str  # 已解析视图（$PPRDIR 展开为工程目录，规整后的绝对/相对路径）
    is_ip: bool         # 是否为 IP 实例文件（.xci / .bd）
    used_in: tuple[str, ...] = ()  # UsedIn 属性集合（synthesis/implementation/simulation）


@dataclass(frozen=True)
class XprRun:
    """单个 Run（synth_1 / impl_1 等）。"""

    run_id: str          # "synth_1" / "impl_1"
    run_type: str        # "Ft3:Synth" / "Ft2:EntireDesign"
    part: str            # 该 run 的 Part（可能与工程 Part 一致）
    state: str           # "current" 等
    strategy: str        # Strategy StratHandle 的 Name
    flow: str            # Strategy StratHandle 的 Flow
    synth_run: str = ""  # impl run 关联的综合 run（SynthRun 属性）
    constrs_set: str = ""  # ConstrsSet
    src_set: str = ""      # SrcSet


@dataclass
class XprConfig:
    """单个 XPR 工程文件的解析结果。"""

    file_path: str = ""        # .xpr 文件本身的路径
    project_dir: str = ""      # 工程目录（.xpr 父目录，即 $PPRDIR 的展开）
    project_path_attr: str = ""  # <Project Path=> 属性原值
    version: str = ""          # <Project Version=>
    minor: str = ""            # <Project Minor=>
    part: str = ""             # 工程 Part（Configuration 下 Option Name="Part"）
    top: str = ""              # 顶层模块（DesignSrcs 的 TopModule）
    sim_top: str = ""          # 仿真顶层（SimulationSrcs 的 TopModule）
    active_sim_set: str = ""   # ActiveSimSet
    xsim_radix: str = ""       # XSimRadix
    default_lib: str = ""      # DefaultLib
    files: list[XprFile] = field(default_factory=list)
    runs: list[XprRun] = field(default_factory=list)
    macros: dict[str, str] = field(default_factory=dict)  # 宏 → 展开值（标注约定）

    @property
    def design_srcs(self) -> list[XprFile]:
        """设计源文件（DesignSrcs）。"""
        return [f for f in self.files if f.fileset_type == "DesignSrcs"]

    @property
    def constrs(self) -> list[XprFile]:
        """约束文件（Constrs，主要为 XDC）。"""
        return [f for f in self.files if f.fileset_type == "Constrs"]

    @property
    def sim_srcs(self) -> list[XprFile]:
        """仿真源文件（SimulationSrcs）。"""
        return [f for f in self.files if f.fileset_type == "SimulationSrcs"]

    @property
    def ips(self) -> list[XprFile]:
        """IP 实例文件（.xci / .bd）。"""
        return [f for f in self.files if f.is_ip]

    def to_dict(self) -> dict:
        """转换为可 JSON 序列化的字典。"""
        return {
            "file_path": self.file_path,
            "project_dir": self.project_dir,
            "project_path_attr": self.project_path_attr,
            "version": self.version,
            "minor": self.minor,
            "part": self.part,
            "top": self.top,
            "sim_top": self.sim_top,
            "active_sim_set": self.active_sim_set,
            "xsim_radix": self.xsim_radix,
            "default_lib": self.default_lib,
            "files": [asdict(f) for f in self.files],
            "design_srcs": [asdict(f) for f in self.design_srcs],
            "constrs": [asdict(f) for f in self.constrs],
            "sim_srcs": [asdict(f) for f in self.sim_srcs],
            "ips": [asdict(f) for f in self.ips],
            "runs": [asdict(r) for r in self.runs],
            "macros": self.macros,
        }


# ====================================================================== #
#  路径宏解析
# ====================================================================== #


def _build_macros(project_dir: str, proj_name: str) -> dict[str, str]:
    """根据工程目录推断各路径宏的展开值（标注约定）。

    仅 ``$PPRDIR`` 可离线确定地展开为工程目录；其余宏按 Vivado 默认
    布局（工程同名 ``.srcs`` / ``.runs`` / ``.cache`` 等）推断。

    注意：Vivado 的 ``{proj}`` 占位 = .xpr **文件名**（去扩展名），
    **不是**工程目录名（如 .xpr 为 ``gauss_noise_proj.xpr``、目录为 ``work``
    时，默认布局是 ``gauss_noise_proj.srcs`` 而非 ``work.srcs``）。

    Args:
        project_dir: 工程目录（.xpr 父目录），POSIX 风格。
        proj_name: .xpr 文件名（去 ``.xpr`` 扩展名），即 Vivado ``{proj}`` 占位。

    Returns:
        宏名 → 展开值的字典（如 ``{"$PPRDIR": ".../work"}``）。
    """
    macros = {"$PPRDIR": project_dir}
    for macro, tmpl in _MACRO_DEFAULTS.items():
        macros[macro] = posixpath.join(project_dir, tmpl.format(proj=proj_name))
    return macros


def _resolve_path(raw: str, macros: dict[str, str]) -> str:
    """将含宏的原始路径展开并规整为 POSIX 风格路径。

    仅展开 ``$PPRDIR``（确定）；含其他无法确定展开的宏时保留原样宏前缀
    （避免给出误导性的绝对路径）。

    Args:
        raw: 原始路径（如 ``$PPRDIR/../sources/rtl/x.v``）。
        macros: 宏展开表。

    Returns:
        解析后的路径；仅含 $PPRDIR 时为规整后的相对/绝对路径，
        含其他宏时仅替换 $PPRDIR、其余宏原样保留。
    """
    path = raw.replace("\\", "/")
    ppr = macros.get("$PPRDIR", "")
    if path.startswith("$PPRDIR") and ppr:
        path = ppr + path[len("$PPRDIR"):]
        # 规整 .. 等（normpath 会转成 OS 分隔符，再统一回 POSIX 便于跨平台展示）
        return posixpath.normpath(path).replace("\\", "/")
    # 含其他宏时不强行展开（离线无法确定），原样返回
    return path


# ====================================================================== #
#  解析函数
# ====================================================================== #


def _is_ip_file(path: str) -> bool:
    """判断文件路径是否为 IP 实例文件（.xci / .bd）。"""
    return path.lower().endswith(_IP_EXTS)


def _parse_fileset(fs: ET.Element, macros: dict[str, str]) -> tuple[list[XprFile], str]:
    """解析单个 <FileSet>，返回文件列表与 TopModule。

    Args:
        fs: <FileSet> 元素。
        macros: 宏展开表。

    Returns:
        (文件列表, 该 FileSet 的 TopModule 名)。
    """
    fs_name = fs.get("Name", "")
    fs_type = fs.get("Type", "")

    files: list[XprFile] = []
    for file_el in fs.findall("File"):
        path_raw = file_el.get("Path", "")
        if not path_raw:
            continue
        used_in = tuple(
            attr.get("Val", "")
            for attr in file_el.findall("./FileInfo/Attr")
            if attr.get("Name") == "UsedIn"
        )
        files.append(XprFile(
            fileset_name=fs_name,
            fileset_type=fs_type,
            path_raw=path_raw,
            path_resolved=_resolve_path(path_raw, macros),
            is_ip=_is_ip_file(path_raw),
            used_in=used_in,
        ))

    # TopModule 在 <Config> 下的 <Option Name="TopModule">
    top = ""
    for opt in fs.findall("./Config/Option"):
        if opt.get("Name") == "TopModule":
            top = opt.get("Val", "")
            break

    return files, top


def _parse_run(run_el: ET.Element) -> XprRun:
    """解析单个 <Run> 元素。"""
    strat = run_el.find("./Strategy/StratHandle")
    strategy = strat.get("Name", "") if strat is not None else ""
    flow = strat.get("Flow", "") if strat is not None else ""

    return XprRun(
        run_id=run_el.get("Id", ""),
        run_type=run_el.get("Type", ""),
        part=run_el.get("Part", ""),
        state=run_el.get("State", ""),
        strategy=strategy,
        flow=flow,
        synth_run=run_el.get("SynthRun", ""),
        constrs_set=run_el.get("ConstrsSet", ""),
        src_set=run_el.get("SrcSet", ""),
    )


def parse_xpr(file_path: str) -> XprConfig:
    """离线解析 Vivado 工程文件 .xpr，提取工程摘要。

    Args:
        file_path: .xpr 文件的路径。

    Returns:
        XprConfig 实例。

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: 文件格式错误、超过大小限制或非合法 Vivado 工程文件。
    """
    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"XPR 文件不存在: {file_path}")

    file_size = os.path.getsize(file_path)
    if file_size > _MAX_FILE_SIZE:
        raise ValueError(
            f"XPR 文件过大 ({file_size / 1024 / 1024:.1f}MB)，"
            f"上限 {_MAX_FILE_SIZE / 1024 / 1024:.0f}MB"
        )

    if not file_path.lower().endswith(".xpr"):
        raise ValueError(f"文件扩展名不是 .xpr: {file_path}")

    try:
        tree = ET.parse(file_path)
    except ET.ParseError as e:
        raise ValueError(f"XML 解析失败: {e}") from e

    root = tree.getroot()
    if root.tag != "Project":
        raise ValueError(f"根元素不是 <Project>，疑似非 Vivado 工程文件: <{root.tag}>")

    # 工程目录 = .xpr 父目录（$PPRDIR 的确定展开），统一 POSIX 风格
    abs_path = os.path.abspath(file_path)
    project_dir = os.path.dirname(abs_path).replace("\\", "/")
    # Vivado {proj} 占位 = .xpr 文件名去扩展名（非工程目录名）
    proj_name = os.path.splitext(os.path.basename(abs_path))[0]
    macros = _build_macros(project_dir, proj_name)

    cfg = XprConfig(
        file_path=file_path,
        project_dir=project_dir,
        project_path_attr=root.get("Path", ""),
        version=root.get("Version", ""),
        minor=root.get("Minor", ""),
        macros=macros,
    )

    # Configuration 下的 Option
    for opt in root.findall("./Configuration/Option"):
        name = opt.get("Name", "")
        val = opt.get("Val", "")
        if name == "Part":
            cfg.part = val
        elif name == "ActiveSimSet":
            cfg.active_sim_set = val
        elif name == "XSimRadix":
            cfg.xsim_radix = val
        elif name == "DefaultLib":
            cfg.default_lib = val

    # FileSets
    for fs in root.findall("./FileSets/FileSet"):
        fs_files, fs_top = _parse_fileset(fs, macros)
        cfg.files.extend(fs_files)
        fs_type = fs.get("Type", "")
        if fs_type == "DesignSrcs" and fs_top:
            cfg.top = fs_top
        elif fs_type == "SimulationSrcs" and fs_top:
            cfg.sim_top = fs_top

    # Runs
    for run_el in root.findall("./Runs/Run"):
        cfg.runs.append(_parse_run(run_el))

    return cfg


# ====================================================================== #
#  格式化
# ====================================================================== #


def format_xpr(cfg: XprConfig) -> str:
    """将 XprConfig 格式化为人类可读的中文摘要（对齐 get_project_info 风格）。

    Args:
        cfg: 解析结果。

    Returns:
        中文摘要文本。
    """
    lines: list[str] = ["=== 工程信息（离线解析 .xpr）==="]
    lines.append(f"  文件:    {cfg.file_path}")
    lines.append(f"  工程目录: {cfg.project_dir}")
    lines.append(f"  Vivado 工程版本: {cfg.version}.{cfg.minor}")
    lines.append(f"  Part:    {cfg.part}")
    lines.append(f"  顶层模块: {cfg.top or '(未设置)'}")
    lines.append(f"  仿真顶层: {cfg.sim_top or '(未设置)'}")
    lines.append(
        f"  仿真集:   {cfg.active_sim_set or '(默认)'}  "
        f"XSimRadix={cfg.xsim_radix or '(默认)'}"
    )

    design = cfg.design_srcs
    constrs = cfg.constrs
    sims = cfg.sim_srcs
    ips = cfg.ips

    lines.append("")
    lines.append(f"设计源文件({len(design)} 个):")
    for f in design[:30]:
        tag = " [IP]" if f.is_ip else ""
        lines.append(f"  {f.path_raw}{tag}")
    if len(design) > 30:
        lines.append(f"  ... 还有 {len(design) - 30} 个文件")

    lines.append("")
    lines.append(f"约束文件 XDC({len(constrs)} 个):")
    for f in constrs[:10]:
        lines.append(f"  {f.path_raw}")
    if len(constrs) > 10:
        lines.append(f"  ... 还有 {len(constrs) - 10} 个文件")

    lines.append("")
    lines.append(f"仿真文件/testbench({len(sims)} 个):")
    for f in sims[:20]:
        lines.append(f"  {f.path_raw}")
    if len(sims) > 20:
        lines.append(f"  ... 还有 {len(sims) - 20} 个文件")

    lines.append("")
    lines.append(f"IP 实例 .xci/.bd({len(ips)} 个):")
    if ips:
        for f in ips[:20]:
            lines.append(f"  {f.path_raw}")
        if len(ips) > 20:
            lines.append(f"  ... 还有 {len(ips) - 20} 个 IP")
    else:
        lines.append("  (无 IP)")

    lines.append("")
    lines.append(f"Runs({len(cfg.runs)} 个):")
    for r in cfg.runs:
        extra = f" → {r.synth_run}" if r.synth_run else ""
        lines.append(
            f"  {r.run_id} [{r.run_type}] part={r.part or '(同工程)'} "
            f"state={r.state or '?'}{extra}"
        )
        if r.strategy:
            lines.append(f"      策略: {r.strategy}  ({r.flow})")

    lines.append("")
    lines.append("路径宏展开（$PPRDIR 已确定展开，其余按 Vivado 默认布局推断）:")
    for macro, val in cfg.macros.items():
        lines.append(f"  {macro} = {val}")

    return "\n".join(lines)
