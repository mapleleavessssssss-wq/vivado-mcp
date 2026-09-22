"""同源 Skill 加载、保守导出、CLI 及发行包资源回归。"""

import json
import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from vivado_mcp import workflows

PROJECT = Path(__file__).resolve().parents[1]


def _skill(name, body="\r\n# 原始正文\r\n\r\n保持换行。\r\n"):
    return f"---\r\nname: {name}\r\ndescription: 单行描述: 示例\r\n---\r\n{body}".encode()


@pytest.fixture
def packaged(tmp_path, monkeypatch):
    package = tmp_path / "package"
    for name in workflows.WORKFLOW_PROMPTS:
        folder = package / "skills" / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_bytes(_skill(name))
    monkeypatch.setattr(workflows.resources, "files", lambda _: package)
    return package


def _run_cli(*args, cwd=PROJECT, pythonpath=None):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    if pythonpath is not None:
        env["PYTHONPATH"] = str(pythonpath)
    return subprocess.run(
        [sys.executable, "-m", "vivado_mcp", *args], cwd=cwd, env=env,
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )


def test_read_preserves_body_and_accepts_prompt_alias(packaged):
    assert workflows.read_workflow("project_bringup") == "\r\n# 原始正文\r\n\r\n保持换行。\r\n"
    assert workflows.read_workflow("vivado-project-bringup") == workflows.read_workflow(
        "project_bringup",
    )


def test_list_is_fixed_order_metadata_not_a_duplicate_description(packaged):
    result = workflows.list_workflows()
    assert [item["name"] for item in result] == list(workflows.WORKFLOW_PROMPTS)
    assert [item["prompt"] for item in result] == list(workflows.WORKFLOW_PROMPTS.values())
    assert all(item["description"] == "单行描述: 示例" for item in result)


def test_export_all_is_byte_identical_and_idempotent(packaged, tmp_path):
    destination = tmp_path / "exported"
    first = workflows.export_workflows(destination)
    assert first["exported"] == list(workflows.WORKFLOW_PROMPTS)
    assert first["skipped"] == []
    mtimes = {}
    for name in first["exported"]:
        target = destination / name / "SKILL.md"
        assert target.read_bytes() == _skill(name)
        mtimes[name] = target.stat().st_mtime_ns
    second = workflows.export_workflows(destination)
    assert second["exported"] == []
    assert second["skipped"] == first["exported"]
    assert all((destination / name / "SKILL.md").stat().st_mtime_ns == timestamp
               for name, timestamp in mtimes.items())


@pytest.mark.parametrize("conflict", ["modified", "empty", "extra", "file", "skill_directory"])
def test_all_conflicts_preflight_before_any_writes(packaged, tmp_path, conflict):
    destination = tmp_path / "exported"
    names = list(workflows.WORKFLOW_PROMPTS)
    target = destination / names[-1]
    target.parent.mkdir()
    if conflict == "file":
        target.write_bytes(b"user-owned")
    else:
        target.mkdir()
        if conflict == "modified":
            (target / "SKILL.md").write_bytes(b"user-owned")
        elif conflict == "extra":
            (target / "SKILL.md").write_bytes(_skill(names[-1]))
            (target / "notes.txt").write_bytes(b"user-owned")
        elif conflict == "skill_directory":
            (target / "SKILL.md").mkdir()
    before = sorted(str(path.relative_to(destination)) for path in destination.rglob("*"))
    with pytest.raises(FileExistsError):
        workflows.export_workflows(destination)
    after = sorted(str(path.relative_to(destination)) for path in destination.rglob("*"))
    assert before == after
    assert not (destination / names[0]).exists()
    if conflict == "modified":
        assert (target / "SKILL.md").read_bytes() == b"user-owned"


def test_subset_deduplicates_aliases(packaged, tmp_path):
    result = workflows.export_workflows(tmp_path / "out", [
        "project_bringup", "vivado-project-bringup", "cdc_audit",
    ])
    assert result["exported"] == ["vivado-project-bringup", "vivado-cdc-audit"]


def test_missing_source_prevents_all_exports(packaged, tmp_path):
    missing = list(workflows.WORKFLOW_PROMPTS)[-1]
    (packaged / "skills" / missing / "SKILL.md").unlink()
    destination = tmp_path / "out"
    with pytest.raises(FileNotFoundError, match=missing):
        workflows.export_workflows(destination)
    assert not destination.exists()


@pytest.mark.parametrize("name", ["../escape", "unknown", "", 123])
def test_unknown_names_are_rejected(packaged, tmp_path, name):
    with pytest.raises(ValueError):
        workflows.read_workflow(name)
    with pytest.raises(ValueError):
        workflows.export_workflows(tmp_path / "out", [name])


@pytest.mark.parametrize("content", [
    b"# no metadata", b"---\nname: wrong\ndescription: hi\n---\nbody",
    b"---\nname: vivado-project-bringup\ndescription: |\n---\nbody",
    b"---\nname: vivado-project-bringup\ndescription: hi\n",
    b"---\nname: vivado-project-bringup\ndescription: hi\n---\n",
])
def test_malformed_metadata_is_explicit(packaged, content):
    (packaged / "skills/vivado-project-bringup/SKILL.md").write_bytes(content)
    with pytest.raises(ValueError):
        workflows.read_workflow("project_bringup")


def test_editable_fallback_requires_matching_project(tmp_path, monkeypatch):
    project = tmp_path / "checkout"
    source = project / "src/vivado_mcp"
    source.mkdir(parents=True)
    monkeypatch.setattr(workflows, "__file__", str(source / "workflows.py"))
    monkeypatch.setattr(workflows.resources, "files", lambda _: source)
    folder = project / "skills/vivado-project-bringup"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_bytes(_skill("vivado-project-bringup"))
    with pytest.raises(FileNotFoundError):
        workflows.read_workflow("project_bringup")
    manifest = project / "pyproject.toml"
    manifest.write_text('[project]\nname="unrelated"', encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        workflows.read_workflow("project_bringup")
    manifest.write_text('[project]\nname="vivado-mcp"', encoding="utf-8")
    assert "原始正文" in workflows.read_workflow("project_bringup")


def test_zip_traversable_resources_do_not_need_filesystem_fallback(tmp_path, monkeypatch):
    archive = tmp_path / "resources.whl"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("vivado_mcp/skills/vivado-project-bringup/SKILL.md",
                        _skill("vivado-project-bringup"))
    with zipfile.ZipFile(archive) as stream:
        root = zipfile.Path(stream, "vivado_mcp/")
        monkeypatch.setattr(workflows.resources, "files", lambda _: root)
        assert "原始正文" in workflows.read_workflow("project_bringup")
        with pytest.raises(FileNotFoundError, match="vivado-cdc-audit"):
            workflows.read_workflow("cdc_audit")


def test_symlink_destination_is_rejected(packaged, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("此 Windows 环境没有创建符号链接的权限")
    with pytest.raises(ValueError, match="junction"):
        workflows.export_workflows(link / "skills")
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("target_kind", ["ancestor", "skill_file"])
def test_windows_reparse_points_are_rejected_without_link_privileges(
    packaged, tmp_path, monkeypatch, target_kind,
):
    destination = tmp_path / "out"
    destination.mkdir()
    name = "vivado-project-bringup"
    target = destination
    if target_kind == "skill_file":
        target = destination / name / "SKILL.md"
        target.parent.mkdir()
        target.write_bytes(_skill(name))
    original_lstat = Path.lstat

    def fake_lstat(path, *args, **kwargs):
        if path == target:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    with pytest.raises(ValueError, match="junction"):
        workflows.export_workflows(destination, [name])


def test_skills_cli_does_not_import_server(monkeypatch, capsys):
    from vivado_mcp.__main__ import main

    imported_before = "vivado_mcp.server" in sys.modules
    monkeypatch.setattr(sys, "argv", ["vivado-mcp", "skills", "list", "--json"])
    main()
    assert len(json.loads(capsys.readouterr().out)) == 5
    assert ("vivado_mcp.server" in sys.modules) == imported_before


def test_real_cli_list_and_export(tmp_path):
    result = _run_cli("skills", "list", "--json")
    assert result.returncode == 0, result.stderr
    entries = json.loads(result.stdout)
    assert len(entries) == 5
    result = _run_cli("skills", "export", str(tmp_path / "out"),
                      "--skill", "vivado-project-bringup", "--skill", "vivado-cdc-audit")
    assert result.returncode == 0, result.stderr
    for name in ("vivado-project-bringup", "vivado-cdc-audit"):
        assert (tmp_path / "out" / name / "SKILL.md").read_bytes() == (
            PROJECT / "skills" / name / "SKILL.md"
        ).read_bytes()


def test_real_cli_rejects_unknown_and_conflicting_skill(tmp_path):
    unknown = _run_cli("skills", "export", str(tmp_path / "out"), "--skill", "unknown")
    assert unknown.returncode == 2
    assert not (tmp_path / "out").exists()
    target = tmp_path / "out/vivado-project-bringup"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_bytes(b"user-edited")
    conflict = _run_cli("skills", "export", str(tmp_path / "out"),
                        "--skill", "vivado-project-bringup")
    assert conflict.returncode == 2
    assert (target / "SKILL.md").read_bytes() == b"user-edited"


def test_built_wheel_loads_skills_without_checkout_or_vivado(tmp_path):
    pytest.importorskip("hatchling")
    built = subprocess.run(
        [sys.executable, "-m", "hatchling", "build", "-t", "wheel", "-d", str(tmp_path)],
        cwd=PROJECT, capture_output=True, text=True, timeout=60,
    )
    assert built.returncode == 0, built.stderr
    wheel = next(tmp_path.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        for name in workflows.WORKFLOW_PROMPTS:
            assert archive.read(f"vivado_mcp/skills/{name}/SKILL.md") == (
                PROJECT / "skills" / name / "SKILL.md"
            ).read_bytes()
    result = _run_cli("skills", "list", "--json", cwd=tmp_path, pythonpath=wheel)
    assert result.returncode == 0, result.stderr
    assert len(json.loads(result.stdout)) == 5
    probe = subprocess.run(
        [sys.executable, "-c", "import sys; from vivado_mcp import workflows; "
         "assert '.whl' in workflows.__file__; "
         "assert 'vivado_mcp.server' not in sys.modules; "
         "assert workflows.read_workflow('cdc_audit'); "
         "from vivado_mcp.prompts import PROMPT_FUNCTIONS; "
         "assert len(PROMPT_FUNCTIONS) == 11; "
         "functions = {f.__name__: f for f in PROMPT_FUNCTIONS}; "
         "assert all(functions[p]() == workflows.read_workflow(s) "
         "for s, p in workflows.WORKFLOW_PROMPTS.items())"],
        cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(wheel)},
        capture_output=True, text=True, timeout=30,
    )
    assert probe.returncode == 0, probe.stderr
