"""Skills 与 MCP Prompts 的同源正文加载及显式目录导出。"""

import os
import stat
from importlib import resources
from pathlib import Path

WORKFLOW_PROMPTS = {
    "vivado-project-bringup": "project_bringup",
    "vivado-timing-closure": "debug_timing",
    "vivado-waveform-debug": "waveform_debug",
    "vivado-cdc-audit": "cdc_audit",
    "vivado-constraints-authoring": "constraints_authoring",
}
MAX_SKILL_BYTES = 256 * 1024


def _source_root():
    """优先读发行包资源；仅验证通过的源码布局可回退到仓库 skills。"""
    packaged = resources.files("vivado_mcp").joinpath("skills")
    if packaged.is_dir():
        return packaged
    project = Path(__file__).resolve().parents[2]
    manifest = project / "pyproject.toml"
    if manifest.is_file():
        try:
            import tomllib
        except ModuleNotFoundError:  # Python 3.10 使用项目已有兼容依赖。
            import tomli as tomllib
        with manifest.open("rb") as stream:
            metadata = tomllib.load(stream)
        project_metadata = metadata.get("project")
        if isinstance(project_metadata, dict) and project_metadata.get("name") == "vivado-mcp":
            source = project / "skills"
            if source.is_dir():
                return source
    raise FileNotFoundError("找不到打包的 Skills，且当前不是有效的 vivado-mcp 源码目录。")


def _canonical_name(name: str) -> str:
    if not isinstance(name, str):
        raise ValueError("Skill 名称必须为字符串。")
    if name in WORKFLOW_PROMPTS:
        return name
    for skill, prompt in WORKFLOW_PROMPTS.items():
        if prompt == name:
            return skill
    raise ValueError(f"未知 Skill: {name!r}；请先运行 skills list。")


def _load(name: str) -> tuple[bytes, str, str]:
    """返回原始文件、单行描述及移除 frontmatter 的正文。"""
    name = _canonical_name(name)
    source = _source_root().joinpath(name).joinpath("SKILL.md")
    if not source.is_file():
        raise FileNotFoundError(f"缺少 Skill 文件: {name}/SKILL.md")
    with source.open("rb") as stream:
        raw = stream.read(MAX_SKILL_BYTES + 1)
    if len(raw) > MAX_SKILL_BYTES:
        raise ValueError(f"Skill 文件过大: {name}")
    text = raw.decode("utf-8-sig")
    if "\x00" in text:
        raise ValueError(f"Skill 文件不是有效文本: {name}")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"Skill 缺少 frontmatter: {name}")
    metadata = {}
    for index, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            body = "".join(lines[index + 1:])
            break
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if not separator or key not in {"name", "description"} or key in metadata or not value:
            raise ValueError(f"Skill 元数据必须为单行 name/description: {name}")
        if value in {"|", ">", "|-", ">-", "|+", ">+"}:
            raise ValueError(f"Skill 元数据不支持多行 YAML: {name}")
        metadata[key] = value
    else:
        raise ValueError(f"Skill frontmatter 未结束: {name}")
    if metadata.get("name") != name or not metadata.get("description") or not body.strip():
        raise ValueError(f"Skill 名称、描述或正文缺失/不匹配: {name}")
    return raw, metadata["description"], body


def read_workflow(name: str) -> str:
    """读取原始 Skill 正文（移除 frontmatter），接受 Skill 名或固定 Prompt 别名。"""
    return _load(name)[2]


def list_workflows() -> list[dict[str, str]]:
    """按固定顺序列出五项 Skill 的名称、描述和对应 Prompt。"""
    return [
        {"name": name, "description": _load(name)[1], "prompt": prompt}
        for name, prompt in WORKFLOW_PROMPTS.items()
    ]


def _check_plain_path(path: Path) -> None:
    """拒绝目标及祖先的链接/Windows junction，避免导出逃逸。"""
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"导出路径不允许符号链接或 junction: {component}")


def _existing_state(folder: Path, raw: bytes) -> str:
    _check_plain_path(folder)
    if not folder.exists():
        return "new"
    if not folder.is_dir():
        raise FileExistsError(f"导出目标已被文件占用: {folder}")
    entries = iter(folder.iterdir())
    first, second = next(entries, None), next(entries, None)
    target = folder / "SKILL.md"
    if first is None or first.name != "SKILL.md" or second is not None:
        raise FileExistsError(f"Skill 目录存在冲突内容，拒绝覆盖: {folder}")
    _check_plain_path(target)
    if not target.is_file():
        raise FileExistsError(f"Skill 文件路径冲突: {target}")
    with target.open("rb") as stream:
        existing = stream.read(MAX_SKILL_BYTES + 1)
    if existing != raw:
        raise FileExistsError(f"Skill 已存在且内容不同，保留用户文件: {target}")
    return "identical"


def export_workflows(destination: str | Path, names: list[str] | None = None) -> dict:
    """全体预检后导出原始 SKILL.md；相同则跳过，冲突不覆盖，不修改客户端配置。"""
    if not isinstance(destination, (str, Path)) or not str(destination).strip():
        raise ValueError("必须提供明确的导出目录。")
    if names is not None and (not isinstance(names, list) or not names):
        raise ValueError("names 必须为非空名称列表，或 None 表示全部。")
    selected = list(dict.fromkeys(_canonical_name(name) for name in names or WORKFLOW_PROMPTS))
    destination = Path(os.path.abspath(os.path.expanduser(str(destination))))
    _check_plain_path(destination)
    if destination.exists() and not destination.is_dir():
        raise FileExistsError(f"导出目录已被文件占用: {destination}")
    # 读取所有源文件并预检全部目标，避免后面的冲突留下前面已经写入的 Skill。
    contents = {name: _load(name)[0] for name in selected}
    states = {
        name: _existing_state(destination / name, raw) for name, raw in contents.items()
    }
    result = {"destination": str(destination), "exported": [], "skipped": []}
    destination.mkdir(parents=True, exist_ok=True)
    _check_plain_path(destination)
    for name, raw in contents.items():
        if states[name] == "identical":
            result["skipped"].append(name)
            continue
        folder = destination / name
        # 独占创建：即使预检后有人写入，也不覆盖其文件或复用其目录。
        folder.mkdir()
        _check_plain_path(folder)
        with (folder / "SKILL.md").open("xb") as stream:
            stream.write(raw)
        result["exported"].append(name)
    return result
