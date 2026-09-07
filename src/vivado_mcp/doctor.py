"""``vivado-mcp doctor`` 的只读诊断与受限修复编排。"""

from __future__ import annotations

import contextlib
import io
import json
import socket
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10：项目不强制引入额外 TOML 依赖
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]

from vivado_mcp import __version__
from vivado_mcp.config import find_vivado, get_vivado_version
from vivado_mcp.install import (
    _BEGIN_MARK,
    _END_MARK,
    _atomic_write_text,
    _has_other_vendor_injection,
    _resolve_init_tcl,
    install,
)
from vivado_mcp.vivado.gui_session import _locate_server_script, probe_vmcp_server

_SCHEMA_VERSION = 1
_DEFAULT_PORT = 9999
_BACKUP_SUFFIX = ".vmcp_backup"
_STATUS_RANK = {"ok": 0, "warn": 1, "degraded": 1, "critical": 2}
_TOML_DECODE_ERRORS = (tomllib.TOMLDecodeError,) if tomllib is not None else ()


@dataclass(frozen=True)
class DoctorCheck:
    """单项诊断结果；字段是 ``--json`` 的稳定公共结构。"""

    id: str
    status: str
    message: str
    fixable: bool
    proposed_action: str | None
    evidence: dict[str, Any]


@dataclass(frozen=True)
class FixResult:
    """一次受限修复动作的审计记录。"""

    id: str
    status: str
    message: str


@dataclass(frozen=True)
class DoctorReport:
    """完整诊断报告。"""

    schema_version: int
    package_version: str
    overall_status: str
    exit_code: int
    checks: list[DoctorCheck]
    fixes: list[FixResult]

    def to_dict(self) -> dict[str, Any]:
        """转为可 JSON 序列化的稳定字典。"""
        return asdict(self)


def _check(
    check_id: str,
    status: str,
    message: str,
    *,
    fixable: bool = False,
    proposed_action: str | None = None,
    **evidence: Any,
) -> DoctorCheck:
    return DoctorCheck(
        id=check_id,
        status=status,
        message=message,
        fixable=fixable,
        proposed_action=proposed_action,
        evidence=evidence,
    )


def _is_tcp_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """仅在协议探针失败后区分“未监听”和“被其他协议占用”。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def _find_server_entry(servers: object) -> tuple[str | None, object]:
    """在任意键名下查找可验证的 vivado-mcp stdio 启动项。"""
    if not isinstance(servers, dict):
        return None, None
    for name, entry in servers.items():
        if _valid_server_entry(entry):
            return str(name), entry
    for name in ("vivado", "vivado-mcp"):
        if name in servers:
            return name, servers[name]
    return None, None


def _valid_server_entry(entry: object) -> bool:
    """验证配置确实启动本包，而不是只按条目名称猜测。"""
    if not isinstance(entry, dict):
        return False
    command = entry.get("command")
    args = entry.get("args", [])
    if not isinstance(command, str) or not isinstance(args, list):
        return False
    if not all(isinstance(arg, str) for arg in args):
        return False
    if entry.get("type", "stdio") != "stdio":
        return False
    command_name = Path(command).name.lower()
    if command_name in {"vivado-mcp", "vivado-mcp.exe"}:
        return not args or args == ["serve"]
    if command_name not in {"python", "python.exe", "python3", "python3.exe", "py", "py.exe"}:
        return False
    return args in (["-m", "vivado_mcp"], ["-m", "vivado_mcp", "serve"])


def _check_claude_config(path: Path) -> DoctorCheck:
    check_id = "mcp_claude_code"
    action = f"在 {path} 的 mcpServers 中添加 vivado-mcp stdio 配置"
    if not path.is_file():
        return _check(
            check_id,
            "warn",
            "未找到 Claude Code 用户配置。",
            fixable=True,
            proposed_action=action,
            path=str(path),
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return _check(
            check_id,
            "critical",
            f"Claude Code 配置无法验证：{exc}",
            path=str(path),
        )
    if not isinstance(data, dict):
        return _check(
            check_id,
            "critical",
            "Claude Code 配置根节点不是 JSON 对象。",
            path=str(path),
        )
    servers = data.get("mcpServers")
    if servers is None:
        return _check(
            check_id,
            "warn",
            "Claude Code 配置缺少 mcpServers。",
            fixable=True,
            proposed_action=action,
            path=str(path),
        )
    if not isinstance(servers, dict):
        return _check(
            check_id,
            "critical",
            "Claude Code 的 mcpServers 不是 JSON 对象。",
            path=str(path),
        )
    name, entry = _find_server_entry(servers)
    if name is None:
        return _check(
            check_id,
            "warn",
            "Claude Code 尚未注册 vivado-mcp。",
            fixable=True,
            proposed_action=action,
            path=str(path),
        )
    if not _valid_server_entry(entry):
        return _check(
            check_id,
            "critical",
            f"Claude Code 的 {name!r} 条目存在，但 command/args 无法验证。",
            path=str(path),
            entry=name,
        )
    return _check(
        check_id,
        "ok",
        f"Claude Code 已注册 vivado-mcp（{name}）。",
        path=str(path),
        entry=name,
    )


def _check_codex_config(path: Path) -> DoctorCheck:
    check_id = "mcp_codex"
    action = f"在 {path} 中追加 mcp_servers.vivado stdio 配置"
    if tomllib is None:
        return _check(
            check_id,
            "degraded",
            "当前 Python 缺少 TOML 解析器，拒绝猜测 Codex 配置状态。",
            proposed_action="使用 Python 3.11+ 运行 doctor",
            path=str(path),
        )
    if not path.is_file():
        return _check(
            check_id,
            "warn",
            "未找到 Codex 用户配置。",
            fixable=True,
            proposed_action=action,
            path=str(path),
        )
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, *_TOML_DECODE_ERRORS) as exc:
        return _check(
            check_id,
            "critical",
            f"Codex 配置无法验证：{exc}",
            path=str(path),
        )
    servers = data.get("mcp_servers")
    if servers is None:
        return _check(
            check_id,
            "warn",
            "Codex 配置缺少 mcp_servers。",
            fixable=True,
            proposed_action=action,
            path=str(path),
        )
    if not isinstance(servers, dict):
        return _check(check_id, "critical", "Codex 的 mcp_servers 不是 TOML 表。", path=str(path))
    name, entry = _find_server_entry(servers)
    if name is None:
        return _check(
            check_id,
            "warn",
            "Codex 尚未注册 vivado-mcp。",
            fixable=True,
            proposed_action=action,
            path=str(path),
        )
    if not _valid_server_entry(entry):
        return _check(
            check_id,
            "critical",
            f"Codex 的 {name!r} 条目存在，但 command/args 无法验证。",
            path=str(path),
            entry=name,
        )
    return _check(
        check_id,
        "ok",
        f"Codex 已注册 vivado-mcp（{name}）。",
        path=str(path),
        entry=name,
    )


def _atomic_write_with_backup(
    path: Path, text: str, *, expected_bytes: bytes | None
) -> None:
    """以调用方读取到的原内容为 CAS 条件提交客户端配置。"""
    _atomic_write_text(
        path,
        text,
        expected_bytes=expected_bytes,
        backup_suffix=_BACKUP_SUFFIX,
    )


def _server_entry(vivado_path: str | None) -> dict[str, object]:
    entry: dict[str, object] = {
        "command": sys.executable,
        "args": ["-m", "vivado_mcp"],
        "type": "stdio",
    }
    if vivado_path:
        entry["env"] = {"VIVADO_PATH": vivado_path}
    return entry


def _fix_claude_config(path: Path, vivado_path: str | None) -> None:
    if path.is_file():
        original_bytes = path.read_bytes()
        data = json.loads(original_bytes.decode("utf-8"))
    else:
        original_bytes = None
        data = {}
    if not isinstance(data, dict):
        raise ValueError("配置根节点不是 JSON 对象")
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("mcpServers 不是 JSON 对象")
    name, existing = _find_server_entry(servers)
    if name is not None and not _valid_server_entry(existing):
        raise ValueError(f"已有无法验证的 {name!r} 条目，拒绝覆盖")
    if name is None:
        servers["vivado"] = _server_entry(vivado_path)
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    _atomic_write_with_backup(path, text, expected_bytes=original_bytes)


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _fix_codex_config(path: Path, vivado_path: str | None) -> None:
    if tomllib is None:
        raise RuntimeError("当前 Python 缺少 TOML 解析器")
    original_bytes = path.read_bytes() if path.is_file() else None
    content = original_bytes.decode("utf-8") if original_bytes is not None else ""
    data = tomllib.loads(content) if content.strip() else {}
    servers = data.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise ValueError("mcp_servers 不是 TOML 表")
    name, existing = _find_server_entry(servers)
    if name is not None:
        if not _valid_server_entry(existing):
            raise ValueError(f"已有无法验证的 {name!r} 条目，拒绝覆盖")
        return
    lines = [
        "[mcp_servers.vivado]",
        'type = "stdio"',
        f"command = {_toml_string(sys.executable)}",
        'args = ["-m", "vivado_mcp"]',
    ]
    if vivado_path:
        lines.extend(
            [
                "",
                "[mcp_servers.vivado.env]",
                f"VIVADO_PATH = {_toml_string(vivado_path)}",
            ]
        )
    prefix = content.rstrip()
    text = (prefix + "\n\n" if prefix else "") + "\n".join(lines) + "\n"
    # 写入前必须由标准解析器验证生成结果。
    tomllib.loads(text)
    _atomic_write_with_backup(path, text, expected_bytes=original_bytes)


def _collect_checks(
    vivado_path: str | None, port: int, home: Path
) -> tuple[list[DoctorCheck], str | None]:
    checks = [
        _check(
            "package_version",
            "ok" if __version__ != "unknown" else "degraded",
            f"vivado-mcp 版本：{__version__}",
            version=__version__,
        )
    ]
    resolved_vivado: str | None = None
    init_tcl: Path | None = None
    init_content: str | None = None
    try:
        resolved_vivado = find_vivado(vivado_path)
        checks.append(
            _check(
                "vivado_executable",
                "ok",
                f"已找到 Vivado {get_vivado_version(resolved_vivado)}。",
                path=resolved_vivado,
                version=get_vivado_version(resolved_vivado),
            )
        )
        init_tcl = _resolve_init_tcl(resolved_vivado)
    except (FileNotFoundError, OSError, ValueError) as exc:
        checks.append(
            _check(
                "vivado_executable",
                "critical",
                str(exc),
                proposed_action="设置 VIVADO_PATH 或向 doctor 传入 Vivado executable 路径",
            )
        )

    if init_tcl is None:
        checks.append(_check("vivado_init_tcl", "degraded", "未定位 Vivado，无法检查 init Tcl。"))
        checks.append(
            _check("third_party_injection", "degraded", "未定位 init Tcl，无法检查第三方注入。")
        )
    else:
        try:
            init_content = (
                init_tcl.read_text(encoding="utf-8", errors="replace") if init_tcl.is_file() else ""
            )
            expected_script = _locate_server_script().as_posix()
            has_marks = _BEGIN_MARK in init_content and _END_MARK in init_content
            current = (
                has_marks
                and f"set ::VMCP_PORT_PREF {port}" in init_content
                and expected_script in init_content
            )
            if current:
                checks.append(
                    _check(
                        "vivado_init_tcl",
                        "warn",
                        "检测到可用的安装级 init 注入；它也会被 project run 子进程加载。",
                        path=str(init_tcl),
                        port=port,
                    )
                )
            else:
                checks.append(
                    _check(
                        "vivado_init_tcl",
                        "ok",
                        "未安装有效的 vivado-mcp init 注入；普通 GUI 使用一次性 "
                        "bootstrap，属于推荐状态。",
                        proposed_action=(
                            "仅在必须先手工启动 GUI 再 attach 时，显式运行 "
                            f"vivado-mcp install --port {port}"
                        ),
                        path=str(init_tcl),
                        port=port,
                    )
                )
            vendor = _has_other_vendor_injection(init_content)
            if vendor:
                checks.append(
                    _check(
                        "third_party_injection",
                        "warn",
                        f"检测到 {vendor} 注入；doctor 不会删除第三方内容。",
                        vendor=vendor,
                        path=str(init_tcl),
                    )
                )
            else:
                checks.append(
                    _check(
                        "third_party_injection",
                        "ok",
                        "未检测到已知第三方 MCP 注入。",
                        path=str(init_tcl),
                    )
                )
        except (OSError, UnicodeError) as exc:
            checks.append(
                _check(
                    "vivado_init_tcl",
                    "critical",
                    f"无法读取 init Tcl：{exc}",
                    path=str(init_tcl),
                )
            )
            checks.append(
                _check(
                    "third_party_injection",
                    "degraded",
                    "init Tcl 不可读，无法检查第三方注入。",
                    path=str(init_tcl),
                )
            )

    if probe_vmcp_server("127.0.0.1", port):
        checks.append(
            _check(
                "vivado_tcp_server",
                "ok",
                "端口通过 vivado-mcp 随机 token 协议探针。",
                host="127.0.0.1",
                port=port,
                protocol_verified=True,
            )
        )
    elif _is_tcp_open("127.0.0.1", port):
        checks.append(
            _check(
                "vivado_tcp_server",
                "critical",
                "端口已监听，但随机 token 协议探针失败；可能被其他服务占用。",
                host="127.0.0.1",
                port=port,
                protocol_verified=False,
                tcp_open=True,
            )
        )
    else:
        checks.append(
            _check(
                "vivado_tcp_server",
                "warn",
                "当前没有可响应随机 token 探针的 Vivado GUI；未启动 Vivado 时属于正常状态。",
                host="127.0.0.1",
                port=port,
                protocol_verified=False,
                tcp_open=False,
            )
        )

    checks.append(_check_claude_config(home / ".claude.json"))
    checks.append(_check_codex_config(home / ".codex" / "config.toml"))
    return checks, resolved_vivado


def _overall(checks: list[DoctorCheck]) -> tuple[str, int]:
    worst = max((_STATUS_RANK[item.status] for item in checks), default=0)
    if worst == 2:
        return "critical", 2
    if worst == 1:
        return "warning", 1
    return "ok", 0


def run_doctor(
    vivado_path: str | None = None,
    port: int = _DEFAULT_PORT,
    *,
    fix: bool = False,
    install_init: bool = False,
    client: str = "all",
    home: Path | None = None,
) -> DoctorReport:
    """运行诊断，可选执行安全、可审计的有限修复。"""
    if not 1 <= port <= 65535:
        raise ValueError("port 必须在 1..65535 范围内")
    if client not in {"all", "claude-code", "codex"}:
        raise ValueError("client 必须是 all、claude-code 或 codex")
    if install_init and not fix:
        raise ValueError("--install-init 必须与 --fix 一起使用")
    user_home = home if home is not None else Path.home()
    checks, resolved_vivado = _collect_checks(vivado_path, port, user_home)
    fixes: list[FixResult] = []
    if fix:
        by_id = {item.id: item for item in checks}
        if install_init and resolved_vivado:
            try:
                # install 的人类输出不得污染 doctor --json。
                with contextlib.redirect_stdout(io.StringIO()):
                    install(vivado_path=resolved_vivado, port=port)
                fixes.append(
                    FixResult("vivado_init_tcl", "applied", "已复用 install 更新 init Tcl。")
                )
            except (FileNotFoundError, PermissionError, OSError) as exc:
                fixes.append(FixResult("vivado_init_tcl", "failed", f"init Tcl 修复失败：{exc}"))

        client_fixes = {
            "mcp_claude_code": (
                "claude-code",
                user_home / ".claude.json",
                _fix_claude_config,
            ),
            "mcp_codex": ("codex", user_home / ".codex" / "config.toml", _fix_codex_config),
        }
        for check_id, (client_name, path, fixer) in client_fixes.items():
            item = by_id[check_id]
            if client not in {"all", client_name} or not item.fixable:
                continue
            try:
                fixer(path, resolved_vivado)
                fixes.append(FixResult(check_id, "applied", f"已安全写入 {client_name} 配置。"))
            except (
                OSError,
                UnicodeError,
                ValueError,
                RuntimeError,
                json.JSONDecodeError,
                *_TOML_DECODE_ERRORS,
            ) as exc:
                fixes.append(FixResult(check_id, "failed", f"{client_name} 配置修复失败：{exc}"))

        checks, _ = _collect_checks(vivado_path, port, user_home)

    overall_status, exit_code = _overall(checks)
    if any(item.status == "failed" for item in fixes):
        overall_status, exit_code = "critical", 2
    return DoctorReport(
        schema_version=_SCHEMA_VERSION,
        package_version=__version__,
        overall_status=overall_status,
        exit_code=exit_code,
        checks=checks,
        fixes=fixes,
    )


def format_report(report: DoctorReport, *, as_json: bool = False) -> str:
    """格式化人类文本或稳定 JSON。"""
    if as_json:
        return json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
    labels = {"ok": "OK", "warn": "WARN", "degraded": "DEGRADED", "critical": "CRITICAL"}
    lines = [f"vivado-mcp doctor {report.package_version}"]
    for item in report.checks:
        lines.append(f"[{labels[item.status]}] {item.id}: {item.message}")
        if item.proposed_action:
            lines.append(f"  fix: {item.proposed_action}")
    for item in report.fixes:
        lines.append(f"[{item.status.upper()}] {item.id}: {item.message}")
    lines.append(f"overall: {report.overall_status} (exit {report.exit_code})")
    return "\n".join(lines)
