"""``vivado-mcp doctor`` 成功、失败、降级和安全修复测试。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from vivado_mcp import doctor
from vivado_mcp.install import _BEGIN_MARK, _build_injection_block


def _fake_vivado(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "Xilinx" / "Vivado" / "2024.1"
    exe = root / "bin" / "vivado.bat"
    init_tcl = root / "scripts" / "Vivado_init.tcl"
    exe.parent.mkdir(parents=True)
    init_tcl.parent.mkdir(parents=True)
    exe.write_text("fake", encoding="utf-8")
    return exe, init_tcl


def _patch_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, probe: bool) -> Path:
    import vivado_mcp.install as install_module

    script = tmp_path / "vivado_mcp_server.tcl"
    script.write_text("# server", encoding="utf-8")
    monkeypatch.setattr(doctor, "_locate_server_script", lambda: script)
    monkeypatch.setattr(install_module, "_locate_server_script", lambda: script)
    monkeypatch.setattr(doctor, "probe_vmcp_server", lambda host, port: probe)
    monkeypatch.setattr(doctor, "_is_tcp_open", lambda host, port: False)
    monkeypatch.setattr(doctor, "__version__", "0.3.24")
    return script


def _write_valid_clients(home: Path, vivado_path: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "vivado": {
                        "command": "python",
                        "args": ["-m", "vivado_mcp"],
                        "env": {"VIVADO_PATH": str(vivado_path)},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    codex = home / ".codex" / "config.toml"
    codex.parent.mkdir()
    codex.write_text(
        '[mcp_servers.vivado]\ncommand = "python"\nargs = ["-m", "vivado_mcp"]\n',
        encoding="utf-8",
    )


@pytest.mark.skipif(doctor.tomllib is None, reason="Python 环境没有结构化 TOML 解析器")
def test_all_checks_success_and_stable_json(monkeypatch, tmp_path):
    exe, _ = _fake_vivado(tmp_path)
    _patch_runtime(monkeypatch, tmp_path, probe=True)
    home = tmp_path / "home"
    _write_valid_clients(home, exe)

    report = doctor.run_doctor(str(exe), home=home)

    assert report.exit_code == 0
    assert report.overall_status == "ok"
    assert [item.id for item in report.checks] == [
        "package_version",
        "vivado_executable",
        "vivado_init_tcl",
        "third_party_injection",
        "vivado_tcp_server",
        "mcp_claude_code",
        "mcp_codex",
    ]
    data = json.loads(doctor.format_report(report, as_json=True))
    assert set(data) == {
        "schema_version",
        "package_version",
        "overall_status",
        "exit_code",
        "checks",
        "fixes",
    }
    assert set(data["checks"][0]) == {
        "id",
        "status",
        "message",
        "fixable",
        "proposed_action",
        "evidence",
    }


def test_missing_vivado_degrades_dependent_checks(monkeypatch, tmp_path):
    _patch_runtime(monkeypatch, tmp_path, probe=False)
    monkeypatch.setattr(
        doctor, "find_vivado", lambda path=None: (_ for _ in ()).throw(FileNotFoundError("missing"))
    )

    report = doctor.run_doctor(home=tmp_path / "home")
    checks = {item.id: item for item in report.checks}

    assert report.exit_code == 2
    assert checks["vivado_executable"].status == "critical"
    assert checks["vivado_init_tcl"].status == "degraded"
    assert checks["third_party_injection"].status == "degraded"


def test_protocol_probe_rejects_plain_tcp_listener(monkeypatch, tmp_path):
    exe, _ = _fake_vivado(tmp_path)
    _patch_runtime(monkeypatch, tmp_path, probe=False)
    monkeypatch.setattr(doctor, "_is_tcp_open", lambda host, port: True)

    report = doctor.run_doctor(str(exe), home=tmp_path / "home")
    port_check = next(item for item in report.checks if item.id == "vivado_tcp_server")

    assert port_check.status == "critical"
    assert port_check.evidence["protocol_verified"] is False
    assert port_check.evidence["tcp_open"] is True


def test_third_party_injection_is_report_only(monkeypatch, tmp_path):
    exe, init_tcl = _fake_vivado(tmp_path)
    _patch_runtime(monkeypatch, tmp_path, probe=False)
    original = "# SynthPilot injection\nputs keep_me\n"
    init_tcl.write_text(original, encoding="utf-8")

    doctor.run_doctor(str(exe), fix=True, client="claude-code", home=tmp_path / "home")

    content = init_tcl.read_text(encoding="utf-8")
    assert "SynthPilot injection" in content
    assert "puts keep_me" in content
    assert _BEGIN_MARK not in content


@pytest.mark.skipif(doctor.tomllib is None, reason="Python 环境没有结构化 TOML 解析器")
def test_fix_uses_install_and_atomic_client_writes(monkeypatch, tmp_path):
    exe, init_tcl = _fake_vivado(tmp_path)
    script = _patch_runtime(monkeypatch, tmp_path, probe=True)
    init_tcl.write_text("# user init\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    claude = home / ".claude.json"
    claude.write_text('{"theme": "dark"}\n', encoding="utf-8")
    codex = home / ".codex" / "config.toml"
    codex.parent.mkdir()
    codex.write_text('model = "example"\n', encoding="utf-8")

    report = doctor.run_doctor(str(exe), fix=True, install_init=True, home=home)

    assert {item.id for item in report.fixes if item.status == "applied"} == {
        "vivado_init_tcl",
        "mcp_claude_code",
        "mcp_codex",
    }
    assert _build_injection_block(script, 9999) in init_tcl.read_text(encoding="utf-8")
    assert json.loads(claude.read_text(encoding="utf-8"))["theme"] == "dark"
    assert (home / ".claude.json.vmcp_backup").read_text(encoding="utf-8") == '{"theme": "dark"}\n'
    assert doctor.tomllib.loads(codex.read_text(encoding="utf-8"))["model"] == "example"
    assert (home / ".codex" / "config.toml.vmcp_backup").read_text(
        encoding="utf-8"
    ) == 'model = "example"\n'
    assert not list(home.rglob("*.tmp"))


def test_fix_refuses_invalid_existing_entries(monkeypatch, tmp_path):
    exe, init_tcl = _fake_vivado(tmp_path)
    script = _patch_runtime(monkeypatch, tmp_path, probe=True)
    init_tcl.write_text(_build_injection_block(script, 9999), encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    claude = home / ".claude.json"
    original = '{"mcpServers":{"vivado":{"command":"other"}}}\n'
    claude.write_text(original, encoding="utf-8")
    _write_valid_clients(home, exe)
    claude.write_text(original, encoding="utf-8")

    report = doctor.run_doctor(str(exe), fix=True, client="claude-code", home=home)

    check = next(item for item in report.checks if item.id == "mcp_claude_code")
    assert check.status == "critical"
    assert not any(item.id == "mcp_claude_code" for item in report.fixes)
    assert claude.read_text(encoding="utf-8") == original


def test_default_run_is_strictly_read_only(monkeypatch, tmp_path):
    exe, init_tcl = _fake_vivado(tmp_path)
    _patch_runtime(monkeypatch, tmp_path, probe=False)
    init_tcl.write_text("# untouched\n", encoding="utf-8")
    home = tmp_path / "missing-home"

    doctor.run_doctor(str(exe), home=home)

    assert init_tcl.read_text(encoding="utf-8") == "# untouched\n"
    assert not home.exists()


def test_fix_failure_is_audited_and_rechecked(monkeypatch, tmp_path):
    exe, _ = _fake_vivado(tmp_path)
    _patch_runtime(monkeypatch, tmp_path, probe=True)
    monkeypatch.setattr(
        doctor, "_fix_claude_config", lambda *args: (_ for _ in ()).throw(OSError("denied"))
    )

    report = doctor.run_doctor(str(exe), fix=True, client="claude-code", home=tmp_path / "home")

    assert report.exit_code == 2
    assert any(item.id == "mcp_claude_code" and item.status == "failed" for item in report.fixes)


def test_python310_without_toml_parser_degrades_codex_check(monkeypatch, tmp_path):
    exe, _ = _fake_vivado(tmp_path)
    _patch_runtime(monkeypatch, tmp_path, probe=True)
    monkeypatch.setattr(doctor, "tomllib", None)
    monkeypatch.setattr(doctor, "_TOML_DECODE_ERRORS", ())

    report = doctor.run_doctor(str(exe), fix=True, client="codex", home=tmp_path / "home")
    codex_check = next(item for item in report.checks if item.id == "mcp_codex")

    assert codex_check.status == "degraded"
    assert codex_check.fixable is False
    assert not any(item.id == "mcp_codex" for item in report.fixes)
    assert not (tmp_path / "home" / ".codex").exists()


def test_invalid_port_rejected(tmp_path):
    with pytest.raises(ValueError, match="1..65535"):
        doctor.run_doctor(port=0, home=tmp_path)


def test_install_init_requires_explicit_fix(tmp_path):
    with pytest.raises(ValueError, match="--install-init.*--fix"):
        doctor.run_doctor(install_init=True, home=tmp_path)


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"command": "vivado-mcp", "args": []}, True),
        ({"command": "vivado-mcp", "args": ["serve"]}, True),
        ({"command": "python", "args": ["-m", "vivado_mcp"]}, True),
        ({"command": "python", "args": ["-m", "vivado_mcp", "serve"]}, True),
        ({"command": "python", "args": ["-m", "vivado_mcp", "doctor"]}, False),
        ({"command": "python", "args": ["-m", "vivado_mcp", "uninstall"]}, False),
        (
            {"command": "python", "args": ["-c", "print(1)", "-m", "vivado_mcp"]},
            False,
        ),
    ],
)
def test_server_entry_requires_exact_serve_argv(entry, expected):
    assert doctor._valid_server_entry(entry) is expected


def test_client_fix_refuses_concurrent_config_change(monkeypatch, tmp_path):
    exe, init_tcl = _fake_vivado(tmp_path)
    script = _patch_runtime(monkeypatch, tmp_path, probe=True)
    init_tcl.write_text(_build_injection_block(script, 9999), encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    claude = home / ".claude.json"
    claude.write_text('{"theme": "dark"}\n', encoding="utf-8")
    concurrent = '{"theme": "light", "concurrent": true}\n'
    real_atomic_write = doctor._atomic_write_with_backup

    def mutate_before_cas(path, text, *, expected_bytes):
        path.write_text(concurrent, encoding="utf-8")
        return real_atomic_write(path, text, expected_bytes=expected_bytes)

    monkeypatch.setattr(doctor, "_atomic_write_with_backup", mutate_before_cas)

    report = doctor.run_doctor(str(exe), fix=True, client="claude-code", home=home)

    assert claude.read_text(encoding="utf-8") == concurrent
    assert any(
        item.id == "mcp_claude_code"
        and item.status == "failed"
        and "发生变化" in item.message
        for item in report.fixes
    )


def test_install_uses_same_directory_fsync_replace_and_backup(monkeypatch, tmp_path):
    import vivado_mcp.install as install_module

    exe, init_tcl = _fake_vivado(tmp_path)
    server_script = tmp_path / "server.tcl"
    server_script.write_text("# server", encoding="utf-8")
    original = "# user init\n"
    init_tcl.write_text(original, encoding="utf-8")
    real_fsync = os.fsync
    real_replace = os.replace
    calls = {"fsync": 0, "replace": 0}

    def spy_fsync(fd):
        calls["fsync"] += 1
        return real_fsync(fd)

    def spy_replace(source, target):
        source_path = Path(source)
        target_path = Path(target)
        assert source_path.parent == target_path.parent == init_tcl.parent
        assert target_path.read_text(encoding="utf-8") == original
        calls["replace"] += 1
        return real_replace(source, target)

    monkeypatch.setattr(install_module, "_locate_server_script", lambda: server_script)
    monkeypatch.setattr(install_module.os, "fsync", spy_fsync)
    monkeypatch.setattr(install_module.os, "replace", spy_replace)

    install_module.install(str(exe), port=9999)

    assert calls == {"fsync": 1, "replace": 1}
    assert _BEGIN_MARK in init_tcl.read_text(encoding="utf-8")
    assert init_tcl.with_suffix(".tcl.vmcp_backup").read_text(encoding="utf-8") == original
    assert not list(init_tcl.parent.glob(f".{init_tcl.name}.*.tmp"))
