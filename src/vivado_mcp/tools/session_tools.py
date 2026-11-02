"""会话管理工具：start_session / stop_session / list_sessions。"""

import json
import logging
import os
import sys

from mcp.server.mcpserver import Context

from vivado_mcp.server import _get_manager, mcp

logger = logging.getLogger(__name__)


def _check_win_curdir_policy() -> str:
    """检测注册表里的 Windows ``NoDefaultCurrentDirectoryInExePath``。

    该变量 **存在即生效**(值为 0 也一样,只有删除才放行):cmd 不再在当前目录找
    ``compile.bat``,Vivado 2019.1 spawn .bat 不带路径,launch_simulation 全挂。
    MCP spawn 的 Vivado 已在子进程环境里移除它(``vivado_child_env``);这里只为
    attach 用户自己开的 Vivado 时提示 —— 注册表里有值,那个进程多半继承了它。

    本函数 **只读** 注册表,**不改**:

    1. HKCU/HKLM 任一处存在该值(任意值) → 警告
    2. 两处都不存在 → 不警告
    3. 非 Windows → 不警告
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

    found_at: list[str] = []
    for root, subkey, label in locations:
        try:
            with winreg.OpenKey(root, subkey, 0, winreg.KEY_READ) as k:
                v, _ = winreg.QueryValueEx(k, "NoDefaultCurrentDirectoryInExePath")
                found_at.append(f"{label} 值 {v}")
        except FileNotFoundError:
            # 键或值不存在 = 未设置
            pass
        except OSError as e:
            # 读失败 = 检测能力降级(可能漏警告),warning 留痕带具体原因
            logger.warning(
                "读注册表 %s\\%s 失败,NoDefaultCurrentDirectoryInExePath "
                "检测降级: %s", label, subkey, e,
            )

    if not found_at:
        return ""

    return (
        "\n⚠  警告:注册表中存在 NoDefaultCurrentDirectoryInExePath "
        f"({', '.join(found_at)})。"
        "\n   该变量存在即生效,设成 0 无效。Vivado 2019.1 spawn compile.bat 时不带路径,"
        "继承了它的 Vivado 跑 launch_simulation 会挂。"
        "\n   即时绕过:在该 Vivado 的 Tcl Console 执行"
        "\n   unset ::env(NoDefaultCurrentDirectoryInExePath)"
        "\n   根治:删除该值(HKLM 需管理员),改完重开 Vivado:"
        '\n   reg delete "HKCU\\Environment" /v NoDefaultCurrentDirectoryInExePath /f'
    )





def _check_ascii_paths(vivado_path: str | None, cwd: str | None = None) -> str:
    """检测 Vivado 路径 / 工作目录是否含非 ASCII 字符。

    Vivado 2019.x 在中文路径下已知会触发 ``TclStackFree: incorrect freePtr``
    内部异常(0.3.13 实战发现)。本检测只 warn,不 block —— 因为不是所有 2019.x
    都必崩,且用户可能在某些场景下能用(源文件中文 OK,只有 .runs/ .sim/ 输出目录
    必须 ASCII)。检测对象:
    - vivado_path (可执行文件路径)
    - 工作目录(create_project / open_project 多半在这里)

    Args:
        vivado_path: Vivado 可执行文件路径。
        cwd: Vivado 实际工作目录。Tcl 模式不提供,此时 Vivado 继承 MCP 进程的目录。

    Returns:
        警告文本(若需要),否则空串。
    """
    non_ascii_paths: list[str] = []
    if vivado_path and not vivado_path.isascii():
        non_ascii_paths.append(f"vivado_path: {vivado_path}")
    try:
        actual_cwd = cwd or os.getcwd()
        if not actual_cwd.isascii():
            non_ascii_paths.append(f"工作目录: {actual_cwd}")
    except OSError as exc:
        # 极少见:cwd 失效。检测能力降级,不让 start_session 失败
        logger.warning("读取工作目录失败,中文路径检测跳过这一项: %s", exc)

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

    每个 session_id 对应一个独立的会话句柄；同 session_id 再次调用会复用现有会话
    (会话模式自动复用)。**多开独立 GUI 实例**见下方 ``port`` 说明。

    Args:
        session_id: 会话标识符，默认 "default"。
        mode: ``"gui"`` / ``"tcl"`` / ``"attach"``，默认 ``"gui"``。
        port: TCP 端口语义(B 方案):
            - ``gui`` 默认 ``9999``：先 probe 9999，命中现有 vmcp server 则直接复用/
              attach(单 GUI 自动复用，也避免抢端口产生孤儿)；无则 spawn 并绑 9999。
            - **多开独立实例**：传 ``port=0`` 自动分配一个空闲端口启动全新实例(零手动
              配端口、跳过 probe)，或给不同端口的显式值。注意:不传 port 时即便换
              session_id 也会 probe/attach 到 9999 的同一个 GUI(不会自动多开)，要独立
              实例就传 ``port=0`` 或显式不同端口。
            - ``attach`` 模式：要连接的现有 GUI 的显式端口(默认 9999)。
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
        ascii_warn = _check_ascii_paths(
            status.get("vivado_path") or vivado_path, status.get("cwd")
        )
        # MCP 自己 spawn 的 Vivado 已去掉该变量,只有 attach 外部进程才可能受影响
        curdir_warn = _check_win_curdir_policy() if status["mode"] == "attach" else ""
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

    对已退出的会话:清理并清除退出记录,不删工作目录里的文件(它们是诊断证据)。

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


@mcp.tool()
async def list_sessions(ctx: Context = None) -> str:
    """列出 Vivado 会话及其状态,包括已退出会话的退出诊断(``state="dead"``)。"""
    manager = _get_manager(ctx)
    sessions = await manager.list_sessions()

    if not sessions:
        return "当前没有活跃的 Vivado 会话。使用 start_session 启动一个新会话。"

    return json.dumps(sessions, indent=2, ensure_ascii=False)
