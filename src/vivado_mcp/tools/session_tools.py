"""会话管理工具：启动、停止、列举与只读 capability 探测。"""

import json
import logging
import os
import sys

from mcp.server.mcpserver import Context

from vivado_mcp.config import discover_vivado_installations, get_vivado_compatibility
from vivado_mcp.server import _get_manager, mcp
from vivado_mcp.tools.annotations import (
    READ_ONLY_LOCAL,
    READ_ONLY_SESSION,
    SESSION_CHANGE,
    SESSION_STOP,
)
from vivado_mcp.vivado.capabilities import (
    group_capabilities,
    normalize_capability_commands,
    probe_command_capabilities,
)

logger = logging.getLogger(__name__)


_WIN11_24H2_BUILD = 26100  # Win 11 24H2 起,NoDefaultCurrentDirectoryInExePath 默认 = 1


def _is_win11_24h2_or_newer() -> bool:
    """是否 Win 11 24H2 及更新(build >= 26100)。

    24H2 起微软把 ``NoDefaultCurrentDirectoryInExePath`` 默认值从 0 改成 1,即使
    注册表里没显式写键,行为也按 1 走。识别这条边界,无键时按版本判默认。
    """
    if sys.platform != "win32":
        return False
    try:
        return sys.getwindowsversion().build >= _WIN11_24H2_BUILD
    except Exception:
        return False


def _check_win_curdir_policy() -> str:
    """检测 Windows ``NoDefaultCurrentDirectoryInExePath`` 安全策略。

    Win 11 24H2+ 默认开启 = 1。开启后 cmd 即使 cwd 已在脚本目录,跑 ``compile.bat``
    (无路径前缀)也会报 "'compile.bat' 不是内部或外部命令"。Vivado 2019.1 内部
    spawn .bat 不传完整路径,直接受此策略 block,launch_simulation 全挂(0.3.16
    实战实测确认)。

    本函数 **只读** 注册表,**不改**。决策:

    1. HKCU/HKLM 任一显式 = 1 → 警告(用户/管理员显式开启)
    2. HKCU 显式 = 0 → 不警告(用户已 opt-out,根治命令的效果)
    3. 两处都无值 + Win 11 24H2+ → 警告(微软默认改成开,无键 = 开)
    4. 两处都无值 + 老 Win → 不警告(老系统默认 0)
    5. 非 Windows → 不警告
    """
    if sys.platform != "win32":
        return ""

    try:
        import winreg  # 标准库,仅 Windows 可用
    except ImportError:
        return ""

    locations = [
        (winreg.HKEY_CURRENT_USER, r"Environment", "HKCU"),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
            "HKLM",
        ),
    ]

    explicit_enabled_at: list[str] = []
    explicit_disabled = False
    for root, subkey, label in locations:
        try:
            with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ) as k:
                v, _ = winreg.QueryValueEx(k, "NoDefaultCurrentDirectoryInExePath")
                iv = int(v)
                if iv == 1:
                    explicit_enabled_at.append(label)
                elif iv == 0:
                    explicit_disabled = True
        except FileNotFoundError:
            # 值不存在 → 由 fallthrough 逻辑根据 Win 版本判默认
            pass
        except OSError as e:
            # 读失败 = 检测能力降级(可能漏警告),warning 留痕带具体原因
            logger.warning(
                "读注册表 %s\\%s 失败,NoDefaultCurrentDirectoryInExePath "
                "检测降级: %s", label, subkey, e,
            )

    if explicit_enabled_at:
        source = f"({' + '.join(explicit_enabled_at)} = 1)"
    elif explicit_disabled:
        # 用户已显式关闭,不警告(根治命令已生效)
        return ""
    elif _is_win11_24h2_or_newer():
        source = "(Win 11 24H2+ 默认开启,注册表未显式覆盖)"
    else:
        return ""

    return (
        "\n⚠  警告:Windows NoDefaultCurrentDirectoryInExePath 策略已开启 "
        f"{source}。"
        "\n   Vivado 2019.1 spawn compile.bat 时不带路径,本策略下会报"
        "'compile.bat 不是内部或外部命令',launch_simulation 全挂(0.3.16 实测)。"
        "\n   根治命令(用户级,不需要管理员,只影响你自己,下次登录生效):"
        '\n   reg add "HKCU\\Environment" /v NoDefaultCurrentDirectoryInExePath /d 0 /f'
        "\n   或暂时在当前 cmd 临时绕过:set NoDefaultCurrentDirectoryInExePath=0"
        "\n   MCP get_critical_warnings(run_name='sim_1') 会用 Tcl exec + 完整路径"
        "兜底,绕开此策略,但根治更省事。"
    )





def _check_ascii_paths(
    vivado_path: str | None,
    project_path: str | None = None,
) -> str:
    """检测 Vivado 路径 / 当前工作目录是否含非 ASCII 字符。

    Vivado 2019.x 在中文路径下已知会触发 ``TclStackFree: incorrect freePtr``
    内部异常(0.3.13 实战发现)。本检测只 warn,不 block —— 因为不是所有 2019.x
    都必崩,且用户可能在某些场景下能用(源文件中文 OK,只有 .runs/ .sim/ 输出目录
    必须 ASCII)。检测对象:
    - vivado_path (可执行文件路径)
    - 当前工作目录(create_project / open_project 多半在这里)

    Returns:
        警告文本(若需要),否则空串。
    """
    non_ascii_paths: list[str] = []
    if vivado_path and not vivado_path.isascii():
        non_ascii_paths.append(f"vivado_path: {vivado_path}")
    if project_path and not project_path.isascii():
        non_ascii_paths.append(f"project_path: {project_path}")
    try:
        cwd = os.getcwd()
        if not cwd.isascii():
            non_ascii_paths.append(f"工作目录: {cwd}")
    except OSError:
        # 极少见:cwd 失效。检测能力降级,不让 start_session 失败
        pass

    if not non_ascii_paths:
        return ""

    return (
        "\n⚠  警告:检测到路径含非 ASCII 字符:\n"
        + "\n".join(f"   {p}" for p in non_ascii_paths)
        + "\n   Vivado 2019.x 在中文路径下可能触发 TclStackFree 崩溃(0.3.13 实战见过)。"
        "\n   范围:综合 .runs/ .sim/ 输出目录 + GUI session 内 cd/open_project"
        " 同样会触发(0.3.17 实战补充)。"
        "\n   建议工程目录搬到纯 ASCII(如 C:/vivado_work/)。"
        "\n   源文件中文 OK,但工程根目录 / 输出目录必须 ASCII。"
        "\n   ─── 受影响命令(0.3.20 实战澄清)───"
        "\n   ✗ 会踩:create_project / synth_design / launch_runs / "
        "launch_simulation / open_project / cd 到中文目录"
        "\n   ✓ 不踩(可忽略此警告):open_wave_database / get_* / report_* / "
        "list_* / current_* 等纯读取/查询命令,以及在已打开的 GUI session 里跑"
        "上述只读 op"
    )


@mcp.tool(annotations=SESSION_CHANGE)
async def start_session(
    session_id: str = "default",
    mode: str = "gui",
    port: int = 0,
    vivado_version: str = "",
    vivado_path: str = "",
    timeout: int = 120,
    project_path: str = "",
    require_ip_integrator: bool = False,
    ctx: Context = None,
) -> str:
    """启动一个新的 Vivado 会话。

    三种模式：
    - ``"gui"`` (默认) — MCP 自动 spawn ``vivado -mode gui``，你能看到 Vivado 图标
      并实时观察 Tcl Console / Block Design / 波形等 GUI 内容。首次使用会自动
      通过 ``-source`` 注入 TCP server，或先运行一次 ``vivado-mcp install`` 持久化。
    - ``"tcl"`` — ``vivado -mode tcl`` 无头子进程（无 GUI，适合 CI / 批处理）。
    - ``"attach"`` — 连接到已由专用 launcher/bootstrap 开启 VMCP endpoint 的
      持久 Vivado GUI。MCP 退出只 detach，不关闭该 GUI。

    每个 session_id 对应一个独立的会话句柄；同 session_id 只有在 mode、port、
    Vivado launcher/version 和 startup project 约束一致时才复用。身份不一致会拒绝，
    不会静默把命令发往另一工程。**多开独立 GUI 实例**见下方 ``port`` 说明。

    Args:
        session_id: 会话标识符，默认 "default"。
        mode: ``"gui"`` / ``"tcl"`` / ``"attach"``，默认 ``"gui"``。
        port: TCP 端口语义(B 方案):
            - ``gui`` 默认 ``0``：自动分配本机回环端口并启动独立实例，不探测固定
              9999，避免误接到 batch/OOC Vivado。
            - 传显式非零端口时，只有握手身份确认 ``kind=gui`` 且 Vivado 版本匹配
              才允许复用；否则拒绝 attach。
            - ``attach`` 模式：必须提供要连接的现有 GUI 的显式非零端口。
        vivado_version: 明确版本，如 ``2018.3``、``2020.2`` 或 ``2024.2``。
            多版本并存时优先使用本字段，禁止按目录名猜测。
        vivado_path: 可选，自定义 Vivado launcher 路径。Windows 必须使用官方
            ``bin/vivado.bat``，Linux 使用 ``bin/vivado``；``bin/unwrapped``
            内部 executable 会在进程创建前被拒绝，禁止绕过 vendor loader。
        project_path: GUI/Tcl/attach 模式可选；准确 XPR 的绝对路径。GUI/Tcl 会先加载
            工程再建立 READY；attach 不打开或修改工程，只用该路径核对现有 endpoint
            身份。避免 endpoint 过早接管 Tcl 或误连另一工程。
        require_ip_integrator: GUI/Tcl/attach + project_path 专用。为 True 时，握手必须
            同时证明 ``open_bd_design`` 和 ``get_bd_pins`` 已注册，否则不进入 READY。
        timeout: 启动超时秒数，GUI 模式建议 120+。默认 120。
    """
    manager = _get_manager(ctx)

    path = vivado_path if vivado_path else None
    try:
        session, banner = await manager.start_session(
            session_id=session_id,
            vivado_path=path,
            vivado_version=vivado_version or None,
            timeout=float(timeout),
            mode=mode,
            port=int(port),
            project_path=project_path or None,
            require_ip_integrator=require_ip_integrator,
        )
        status = session.status_dict()
        ascii_warn = _check_ascii_paths(
            status.get("vivado_path") or vivado_path,
            status.get("startup_project_path") or project_path,
        )
        curdir_warn = _check_win_curdir_policy()
        return (
            f"会话 '{session_id}' 已就绪（mode={status['mode']}）。\n"
            f"Vivado: {status['vivado_path']}\n"
            f"状态: {status['state']}\n\n"
            f"--- 启动信息 ---\n{banner}"
            f"{ascii_warn}"
            f"{curdir_warn}"
        )
    except ValueError as e:
        return f"[ERROR] {e}"
    except Exception as e:
        return f"[ERROR] 启动会话 '{session_id}' 失败: {e}"


@mcp.tool(annotations=READ_ONLY_LOCAL)
async def list_vivado_installations() -> str:
    """离线列出本机发现的全部 Vivado 版本和官方 launcher 绝对路径。

    本工具不启动 Vivado、不连接会话、不读取工程。发现多个版本时只列出，
    不选择默认版本。
    """
    items = []
    for item in discover_vivado_installations():
        profile = get_vivado_compatibility(item.version)
        record: dict[str, object] = {"version": item.version, "path": item.path}
        if profile is not None:
            record["compatibility"] = {
                "support_level": profile.support_level,
                "tcl_runtime": profile.tcl_runtime,
                "notes": list(profile.notes),
            }
        else:
            record["compatibility"] = {
                "support_level": "unprofiled",
                "tcl_runtime": "unknown",
                "notes": ["Requires explicit read-only validation before use."],
            }
        items.append(record)
    return json.dumps(items, indent=2, ensure_ascii=False)


@mcp.tool(annotations=READ_ONLY_SESSION)
async def get_vivado_capabilities(
    commands: list[str] | None = None,
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """只读检查当前 Vivado Tcl 解释器是否注册了指定命令。

    不传 ``commands`` 时返回项目、run、报告、仿真、硬件交接和 Hardware Manager
    的默认能力矩阵。传入命令时只检查这些精确命令名。实现仅执行 Tcl
    ``info commands``，不会执行被检查的命令，也不会打开或修改工程。

    ``gate`` 含义：全部存在为 ``PASS``；任一不存在为 ``FAIL``；探测输出不完整
    为 ``UNKNOWN``。为具体动作预检时，``commands`` 只传该动作需要的命令；
    默认完整矩阵用于环境盘点，其中无关功能缺失不阻塞当前任务。
    同一版本/会话的有效结果可复用；仅当前所需命令不可用或未知时停止该动作。
    """
    manager = _get_manager(ctx)
    session = manager.get(session_id)
    if session is None:
        return f"[ERROR] 会话 '{session_id}' 不存在。请先调用 start_session。"

    try:
        selected = normalize_capability_commands(commands)
        availability = await probe_command_capabilities(session, selected)
    except (RuntimeError, ValueError) as exc:
        return f"[ERROR] capability 探测失败: {exc}"

    unavailable = [
        command for command, available in availability.items() if available is False
    ]
    unknown = [
        command for command, available in availability.items() if available is None
    ]
    gate = "UNKNOWN" if unknown else ("FAIL" if unavailable else "PASS")
    status = session.status_dict()
    matrix = (
        group_capabilities(availability)
        if not commands
        else {"requested": availability}
    )
    return json.dumps(
        {
            "gate": gate,
            "probe": "Tcl info commands (exact name; target command not executed)",
            "session_id": session_id,
            "vivado_version": status.get("vivado_version", "unknown"),
            "vivado_path": status.get("vivado_path", ""),
            "capabilities": matrix,
            "unavailable": unavailable,
            "unknown": unknown,
        },
        indent=2,
        ensure_ascii=False,
    )


@mcp.tool(annotations=SESSION_STOP)
async def stop_session(
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """关闭指定的 Vivado 会话。

    Args:
        session_id: 要关闭的会话标识符。
    """
    manager = _get_manager(ctx)
    try:
        return await manager.stop_session(session_id)
    except Exception as e:
        # 与 close_all 的兜底一致:stop 异常不能裸出 MCP 工具层。
        # manager 是 stop 成功后才 pop,失败时会话仍在 _sessions,可重试
        logger.error("关闭会话 '%s' 失败: %s", session_id, e)
        return (
            f"[ERROR] 关闭会话 '{session_id}' 失败: {type(e).__name__}: {e}。"
            "会话仍保留,可重试 stop_session。"
        )


@mcp.tool(annotations=SESSION_CHANGE)
async def detach_session(
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """仅断开 MCP 传输，保留可见 Vivado GUI 与其当前工程/会话状态。"""
    manager = _get_manager(ctx)
    try:
        return await manager.detach_session(session_id)
    except Exception as exc:
        return (
            f"[ERROR] detach 会话 '{session_id}' 失败: "
            f"{type(exc).__name__}: {exc}"
        )


@mcp.tool(annotations=READ_ONLY_SESSION)
async def list_sessions(ctx: Context = None) -> str:
    """列出所有活跃的 Vivado 会话及其状态。"""
    manager = _get_manager(ctx)
    sessions = await manager.list_sessions()

    if not sessions:
        return "当前没有活跃的 Vivado 会话。使用 start_session 启动一个新会话。"

    return json.dumps(sessions, indent=2, ensure_ascii=False)
