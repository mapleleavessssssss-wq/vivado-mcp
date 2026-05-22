"""会话管理工具：start_session / stop_session / list_sessions。"""

import json
import logging
import os
import sys

from mcp.server.fastmcp import Context

from vivado_mcp.server import _get_manager, mcp

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
            logger.debug("读注册表失败 %s\\%s: %s", label, subkey, e)

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





def _check_ascii_paths(vivado_path: str | None) -> str:
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
        "\n   建议工程目录搬到纯 ASCII(如 C:/vivado_work/)。"
        "\n   源文件中文 OK,但 .runs/ .sim/ 等输出目录必须 ASCII。"
    )


@mcp.tool()
async def start_session(
    session_id: str = "default",
    mode: str = "gui",
    port: int = 9999,
    vivado_path: str = "",
    timeout: int = 120,
    ctx: Context = None,
) -> str:
    """启动一个新的 Vivado 会话。

    三种模式：
    - ``"gui"`` (默认) — MCP 自动 spawn ``vivado -mode gui``，你能看到 Vivado 图标
      并实时观察 Tcl Console / Block Design / 波形等 GUI 内容。首次使用会自动
      通过 ``-source`` 注入 TCP server，或先运行一次 ``vivado-mcp install`` 持久化。
    - ``"tcl"`` — ``vivado -mode tcl`` 无头子进程（无 GUI，适合 CI / 批处理）。
    - ``"attach"`` — 连接到用户已手动打开的 Vivado GUI（需先运行 ``vivado-mcp install``
      让 init.tcl 自动开启 TCP server）。

    每个 session_id 对应一个独立的 Vivado 实例，支持多会话并行。

    Args:
        session_id: 会话标识符，默认 "default"。
        mode: ``"gui"`` / ``"tcl"`` / ``"attach"``，默认 ``"gui"``。
        port: attach 模式的首选 TCP 端口，默认 9999。
        vivado_path: 可选，自定义 Vivado 可执行文件路径。留空则自动检测。
        timeout: 启动超时秒数，GUI 模式建议 120+。默认 120。
    """
    manager = _get_manager(ctx)

    path = vivado_path if vivado_path else None
    try:
        session, banner = await manager.start_session(
            session_id=session_id,
            vivado_path=path,
            timeout=float(timeout),
            mode=mode,
            port=int(port),
        )
        status = session.status_dict()
        ascii_warn = _check_ascii_paths(status.get("vivado_path") or vivado_path)
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


@mcp.tool()
async def stop_session(
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """关闭指定的 Vivado 会话。

    Args:
        session_id: 要关闭的会话标识符。
    """
    manager = _get_manager(ctx)
    return await manager.stop_session(session_id)


@mcp.tool()
async def list_sessions(ctx: Context = None) -> str:
    """列出所有活跃的 Vivado 会话及其状态。"""
    manager = _get_manager(ctx)
    sessions = manager.list_sessions()

    if not sessions:
        return "当前没有活跃的 Vivado 会话。使用 start_session 启动一个新会话。"

    return json.dumps(sessions, indent=2, ensure_ascii=False)
