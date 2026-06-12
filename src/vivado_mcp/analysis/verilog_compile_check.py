"""Verilog 外部编译检查(iverilog / verilator),比 Vivado 综合快 50 倍。

设计取舍:
- iverilog -t null:只做语法 + 连接性检查,不产物,毫秒级
- verilator --lint-only:更严格(style 检查、Lint_Async 等),几秒级
- auto 模式:优先 iverilog(快、普遍装),没有再 verilator

两个工具都是"装了才跑",没装不报错,只提示安装方法。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class CompileIssue:
    """一条 compile 诊断信息。"""
    severity: str   # "error" / "warning" / "info"
    file: str
    line: int       # 0 表示无行号
    message: str
    tool: str       # "iverilog" / "verilator"


@dataclass
class CompileReport:
    tool_used: str = ""         # 实际用到的工具名
    tool_available: bool = False
    files: list[str] = field(default_factory=list)
    issues: list[CompileIssue] = field(default_factory=list)
    return_code: int = 0
    raw_stderr: str = ""
    install_hint: str = ""
    # 输入文件本身不适用本检查时(如 VHDL)的跳过原因,正常为空
    skip_reason: str = ""

    @property
    def errors(self) -> list[CompileIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[CompileIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    def to_dict(self) -> dict:
        return {
            "tool_used": self.tool_used,
            "tool_available": self.tool_available,
            "files": self.files,
            "return_code": self.return_code,
            "issue_count": len(self.issues),
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "issues": [asdict(i) for i in self.issues],
            "install_hint": self.install_hint,
            "skip_reason": self.skip_reason,
        }


# iverilog 输出格式典型:
#   test.v:5: syntax error
#   test.v:5: error: Invalid module item.
#   test.v:7: warning: Array x unused.
# 策略:先抓 file:line:rest,再从 rest 里嗅 severity 关键字
# 注意 Windows 路径 `C:/path/test.v` 里的盘符冒号,所以 file 要兼容可选盘符前缀
_IVERILOG_RE = re.compile(
    r"^(?P<file>(?:[A-Za-z]:)?[^:]+):(?P<line>\d+):\s*(?P<rest>.*)$",
)

# verilator 输出格式:
#   %Error: test.v:5:1: syntax error
#   %Warning-UNUSED: test.v:7:5: Signal is not used: 'x'
_VERILATOR_RE = re.compile(
    r"^%(?P<sev>Error|Warning)(?:-\S+)?:\s*"
    r"(?P<file>(?:[A-Za-z]:)?[^:]+):(?P<line>\d+):(?:\d+:)?\s*(?P<msg>.*)$",
    re.IGNORECASE,
)


def _parse_iverilog(stderr: str, tool_name: str = "iverilog") -> list[CompileIssue]:
    issues: list[CompileIssue] = []
    for raw in stderr.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _IVERILOG_RE.match(line)
        if m is None:
            continue

        rest = m.group("rest").strip()
        lower = rest.lower()

        # 严重度嗅探:syntax error / error / sorry / warning
        if lower.startswith("syntax error") or lower.startswith("error:") or " error:" in lower:
            severity = "error"
            # 去掉 "error:" 前缀让 message 干净
            if lower.startswith("error:"):
                msg = rest[len("error:"):].strip()
            else:
                msg = rest
        elif lower.startswith("sorry:"):
            # iverilog 的 "sorry:" = 构造合法但本工具无法处理(常见于 -g2012
            # 打开的 SV 特性),本次检查对该文件已失效 → 归为 error,
            # 否则 rc=1 + 全 info 会产出 "WARN (0 warnings)" 的空报告
            severity = "error"
            msg = rest  # 保留 "sorry:" 前缀,让用户知道是工具能力限制而非代码错误
        elif lower.startswith("warning:") or " warning:" in lower:
            severity = "warning"
            if lower.startswith("warning:"):
                msg = rest[len("warning:"):].strip()
            else:
                msg = rest
        else:
            # 带 file:line: 但无级别关键字 → 视为 info(iverilog 偶尔输出进度信息)
            severity = "info"
            msg = rest

        try:
            lineno = int(m.group("line"))
        except ValueError:
            lineno = 0
        issues.append(CompileIssue(
            severity=severity,
            file=m.group("file").strip(),
            line=lineno,
            message=msg,
            tool=tool_name,
        ))
    return issues


def _parse_verilator(stderr: str) -> list[CompileIssue]:
    issues: list[CompileIssue] = []
    for raw in stderr.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _VERILATOR_RE.match(line)
        if m is None:
            continue
        severity = "error" if m.group("sev").lower() == "error" else "warning"
        try:
            lineno = int(m.group("line"))
        except ValueError:
            lineno = 0
        issues.append(CompileIssue(
            severity=severity,
            file=m.group("file").strip(),
            line=lineno,
            message=m.group("msg").strip(),
            tool="verilator",
        ))
    return issues


def _scoop_fallback(name: str) -> str | None:
    """shutil.which 失败时,扫 scoop 默认安装路径。

    Windows + scoop 的经典坑:User PATH 注册表已更新,但父进程
    (IDE / MCP host)启动时 snapshot 的 PATH 仍是旧的,`which` 失败。
    返回 scoop shim 的绝对路径,subprocess 能直接调。
    """
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    for suffix in (".exe", ".cmd", ""):
        cand = os.path.join(home, "scoop", "shims", name + suffix)
        if os.path.isfile(cand):
            return cand
    return None


def _scoop_apps_bin(name: str) -> str | None:
    """scoop 里真实 exe 的 bin 目录(不是 shims)。

    用途:补到 subprocess env["PATH"],让 iverilog.exe 启动时能找到同目录
    的 mingw/cygwin DLL。仅返回存在的目录。
    """
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    cand = os.path.join(home, "scoop", "apps", name, "current", "bin")
    return cand if os.path.isdir(cand) else None


def _detect_tool(preference: str = "auto") -> tuple[str, str]:
    """返回 (tool_name, install_hint)。tool_name='' 表示没找到。"""
    pref = preference.lower()
    iverilog = shutil.which("iverilog") or _scoop_fallback("iverilog")
    verilator = shutil.which("verilator") or _scoop_fallback("verilator")

    if pref == "iverilog":
        if iverilog:
            return ("iverilog", "")
        return ("", "iverilog 未安装。Windows: scoop install iverilog 或 choco install iverilog;"
                    " Linux: apt install iverilog;macOS: brew install icarus-verilog")
    if pref == "verilator":
        if verilator:
            return ("verilator", "")
        return ("", "verilator 未安装。Windows: choco install verilator;"
                    " Linux: apt install verilator;macOS: brew install verilator")

    # auto
    if iverilog:
        return ("iverilog", "")
    if verilator:
        return ("verilator", "")
    return ("", "未检测到 iverilog 或 verilator。推荐装 iverilog(轻量、快):"
                " Windows scoop install iverilog / Linux apt install iverilog / "
                " macOS brew install icarus-verilog")


def compile_check(
    files: list[str] | list[Path],
    tool: str = "auto",
    timeout: float = 30.0,
) -> CompileReport:
    """用 iverilog 或 verilator 做语法/静态检查。

    Args:
        files: 仅支持 Verilog / SystemVerilog 文件(.v / .sv)。
            含 .vhd/.vhdl 时直接返回 SKIP(skip_reason 附替代方案),不跑工具。
        tool: "auto" / "iverilog" / "verilator"。
        timeout: 子进程超时秒数。
    """
    file_strs = [str(f) for f in files]
    report = CompileReport(files=file_strs)

    # 扩展名守卫:iverilog/verilator 都不支持 VHDL,跑了只会产出误导性 FAIL
    vhdl_files = [f for f in file_strs if f.lower().endswith((".vhd", ".vhdl"))]
    if vhdl_files:
        names = ", ".join(Path(f).name for f in vhdl_files)
        report.skip_reason = (
            f"检测到 VHDL 文件: {names}。iverilog/verilator 不支持 VHDL;"
            '项目内快速语法检查用 run_tcl("check_syntax -fileset sources_1")'
            "(2019.1 可用,需项目已打开)。"
        )
        return report

    tool_name, hint = _detect_tool(tool)
    if not tool_name:
        report.tool_available = False
        report.install_hint = hint
        return report

    report.tool_used = tool_name
    report.tool_available = True

    # 拿到绝对路径(PATH 上找到优先,否则走 scoop fallback)
    # 这样即使父进程 PATH snapshot 过旧,subprocess 也能定位
    exe_path = shutil.which(tool_name) or _scoop_fallback(tool_name) or tool_name

    # 组装命令
    if tool_name == "iverilog":
        # -t null:不产物,只做 parse + elab + 连接性检查
        cmd = [exe_path, "-t", "null"] + file_strs
        # iverilog 默认按 IEEE1364-2005 解析,.sv 的 logic/always_ff 等会被
        # 误报 syntax error —— 含 .sv 文件时加 -g2012 开 SystemVerilog 支持
        if any(f.lower().endswith(".sv") for f in file_strs):
            cmd.insert(1, "-g2012")
    else:
        # verilator --lint-only:静态检查,不编译
        cmd = [exe_path, "--lint-only", "-Wall"] + file_strs

    # 准备 env:父进程 PATH snapshot 过旧时,iverilog.exe 启动会找不到
    # 依赖 DLL(0xC0000135 STATUS_DLL_NOT_FOUND)。把 exe 所在目录补到 PATH
    # 开头,保证 Windows DLL loader 能定位 mingw/cygwin 运行时 DLL。
    env = os.environ.copy()
    exe_dir = os.path.dirname(exe_path)
    if exe_dir and os.path.isdir(exe_dir):
        env["PATH"] = exe_dir + os.pathsep + env.get("PATH", "")
        # scoop 的真 bin 目录:apps/<name>/current/bin(shim 不是真 bin)
        # 通过 shim 间接调时,真 exe 目录也要进 PATH,否则 DLL 搜索失败
        scoop_bin = _scoop_apps_bin(tool_name)
        if scoop_bin and scoop_bin != exe_dir:
            env["PATH"] = scoop_bin + os.pathsep + env["PATH"]

    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except subprocess.TimeoutExpired:
        report.return_code = -1
        report.raw_stderr = f"[超时] {tool_name} 检查 {timeout}s 内未完成"
        return report
    except (OSError, FileNotFoundError) as e:
        report.return_code = -2
        report.raw_stderr = f"[启动失败] {e}"
        return report

    report.return_code = r.returncode
    report.raw_stderr = (r.stderr or "").strip()

    # iverilog 的诊断同时写 stderr 和 stdout;verilator 主要写 stderr
    combined = ((r.stderr or "") + "\n" + (r.stdout or "")).strip()
    if tool_name == "iverilog":
        report.issues = _parse_iverilog(combined)
    else:
        report.issues = _parse_verilator(combined)

    return report


def format_compile_report(report: CompileReport) -> str:
    if report.skip_reason:
        # 输入文件不适用(VHDL 等):明确说原因和替代路径,不误导成 FAIL
        return (
            "=== Verilog 编译检查: SKIP ===\n"
            f"{report.skip_reason}"
        )

    if not report.tool_available:
        return (
            "=== Verilog 编译检查: SKIP ===\n"
            f"{report.install_hint}\n"
            "(装上后再调用即可,MCP 工具自动检测。)"
        )

    n_err = len(report.errors)
    n_warn = len(report.warnings)

    if report.return_code == 0 and not report.issues:
        return (
            f"=== Verilog 编译检查 ({report.tool_used}): PASS ===\n"
            f"检查了 {len(report.files)} 个文件,无 error/warning。"
        )

    if report.return_code < 0:
        return (
            f"=== Verilog 编译检查 ({report.tool_used}): 运行异常 ===\n"
            f"{report.raw_stderr}"
        )

    # returncode 非 0 但没解析到任何 issue:要么 DLL 加载失败(Windows 0xC0000135)
    # 要么输出格式外的错误。不当 WARN,而是"运行异常"展示原始 stderr。
    if report.return_code != 0 and not report.issues:
        return (
            f"=== Verilog 编译检查 ({report.tool_used}): 运行异常 ===\n"
            f"返回码: {report.return_code}"
            f" (Windows 0xC0000135 = DLL 加载失败,通常是 PATH 缺 mingw/cygwin 运行时)\n"
            f"原始输出:\n{report.raw_stderr or '(空)'}"
        )

    header = f"=== Verilog 编译检查 ({report.tool_used}): "
    if n_err > 0:
        header += f"FAIL ({n_err} errors, {n_warn} warnings) ==="
    else:
        header += f"WARN ({n_warn} warnings) ==="

    out = [header]
    out.append(f"返回码: {report.return_code}  |  检查文件: {len(report.files)}")
    out.append("")

    for sev_key, sev_label in [("error", "ERROR"), ("warning", "WARNING")]:
        subset = [i for i in report.issues if i.severity == sev_key]
        if not subset:
            continue
        out.append(f"--- {sev_label} ({len(subset)} 条) ---")
        for i in subset:
            loc = f"{Path(i.file).name}:{i.line}" if i.line > 0 else Path(i.file).name
            out.append(f"  {loc}")
            out.append(f"    {i.message}")
        out.append("")

    if not report.issues and report.raw_stderr:
        # 没解析出结构化问题但返回码非零:输出原始 stderr
        out.append("原始输出:")
        out.append(report.raw_stderr[:1500])

    return "\n".join(out).rstrip()
