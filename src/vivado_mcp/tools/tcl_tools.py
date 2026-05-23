"""通用 Tcl 执行工具：run_tcl / safe_tcl。

**设计哲学**：能用 run_tcl 做的事就不包装成专用工具——
一个工具的存在应该是因为它提供"Tcl 做不了或做不好"的本地价值（如结构化解析、
跨命令协议、本地知识库）。绝大多数 Vivado 操作就是一行 Tcl，让 AI 自己拼。

- ``run_tcl`` — 执行任意 Tcl 命令（AI 自己负责引号 / 路径 / 标识符安全性）
- ``safe_tcl`` — 带参数模板的版本，自动用 Tcl ``list`` 规则转义参数，
  Windows 路径含空格 / 中文 / ``$`` / ``[]`` / ``{}`` / 反斜杠也能安全执行
"""

from mcp.server.fastmcp import Context

from vivado_mcp.server import _NO_SESSION, _require_session, _safe_execute, mcp
from vivado_mcp.vivado.tcl_utils import tcl_quote


@mcp.tool()
async def run_tcl(
    command: str,
    session_id: str = "default",
    timeout: int = 120,
    ctx: Context = None,
) -> str:
    r"""执行任意 Vivado Tcl 命令。支持所有 Vivado Tcl API。

    这是最通用的工具，可以执行任何 Vivado Tcl 命令，包括：

    - 项目: ``create_project``, ``open_project``, ``add_files``, ``set_property top``
    - 约束: ``create_clock``, ``set_property PACKAGE_PIN``
    - IP: ``create_ip``, ``generate_target``, ``set_property CONFIG.*``
    - Block Design: ``create_bd_design``, ``create_bd_cell``, ``connect_bd_intf_net``
    - 查询: ``get_ports``, ``get_cells``, ``get_property STATUS [get_runs]``
    - 报告: ``report_utilization -return_string``, ``report_timing_summary -return_string``
    - 仿真: ``launch_simulation``, ``run 100ns``, ``add_wave``
    - 以及任何其他 Vivado Tcl 命令

    支持多行脚本（用换行符分隔）。

    **路径含特殊字符时请用 safe_tcl 而非 run_tcl**，避免 Tcl 解析错误。

    **XSim 仿真常见坑摘要**（0.3.17 实战沉淀,完整版见 vivado-quirks.md §8）:

    - **`add_wave_group` 必须配 `-into $g`**,否则信号全跑到顶层,group 是空的:

      ```tcl
      set g [add_wave_group sig_grp]
      add_wave -into $g /tb/clk     ;# ✓ 进 group
      add_wave /tb/rst              ;# ✗ 跑到顶层
      ```

    - **`add_wave` / `get_objects` 拒 escaped id**,必须先 `current_scope` 切到目标
      scope 再用 short name:

      ```tcl
      # ✗ add_wave {\u_dut/sig}
      current_scope /tb/u_dut       ;# ✓ 切上下文
      add_wave sig
      ```

    - **清空波形只认 `remove_wave [get_waves *]`**,`-all` / `*` / `-of_objects` 都
      不工作(XSim 2019.1 bug)

    - **`xsim -tclbatch` 文件必须显式 `quit`**,EOF 不自动退出,会卡死 CI

    - **`if-generate` 命名块不是 scope** —— 内部 reg 无 add_wave 寻址路径

    - **`[N]` / `[X]` 在 Tcl 字符串里会触发命令替换**,用 `{}` 包字面路径:

      ```tcl
      # ✗ add_wave /tb/gen_ch[0].u/sig    invalid command "0"
      add_wave {/tb/gen_ch[0].u/sig}
      ```

    Args:
        command: Tcl 命令文本（支持多行）。
        session_id: 目标会话 ID，默认 "default"。
        timeout: 命令执行超时秒数，默认 120。
    """
    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    return await _safe_execute(session, command, float(timeout), "命令执行失败")


@mcp.tool()
async def safe_tcl(
    template: str,
    args: list[str] = None,
    session_id: str = "default",
    timeout: int = 120,
    ctx: Context = None,
) -> str:
    """执行带参数的 Tcl 命令模板，自动用 Tcl list 规则转义参数。

    适用场景：命令中含文件路径、端口名、字符串值等可能有特殊字符的输入。
    Tcl 的 ``$``、``[]``、``{}``、反斜杠、空格都会被正确转义，防注入且防解析错。

    用法示例：

    - ``safe_tcl("create_project {0} {1} -part {2}",
      args=["my_proj", "C:/path with space", "xc7a35tcpg236-1"])``
    - ``safe_tcl("read_verilog {0}", args=["C:/files/top with $dollar.v"])``
    - ``safe_tcl("set_property PACKAGE_PIN {0} [get_ports {1}]",
      args=["W5", "clk"])``

    template 用 Python format 的 ``{0}`` / ``{1}`` 占位符，args 中每个元素会被
    ``tcl_quote()`` 包装成 ``"..."`` 并转义所有特殊字符。

    Args:
        template: Tcl 命令模板，用 {0}/{1}/... 表示参数位置。
        args: 参数值列表，将被自动转义。
        session_id: 目标会话 ID，默认 "default"。
        timeout: 命令执行超时秒数，默认 120。
    """
    if args is None:
        args = []
    quoted = [tcl_quote(str(a)) for a in args]
    try:
        cmd = template.format(*quoted)
    except (IndexError, KeyError) as e:
        return (
            f"[ERROR] safe_tcl 模板占位符不匹配: {e}。"
            f"template={template!r}, args={args!r}"
        )

    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    return await _safe_execute(session, cmd, float(timeout), "safe_tcl 执行失败")
