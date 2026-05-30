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

    - **marker 复位 = 磁盘 wcfg 干净时重载**。marker 存在 .wcfg 的
      `<wave_markers><marker time="..fs"/></wave_markers>`,清掉/复位用一行:

      ```tcl
      close_wave_config -force          ;# 丢内存里的脏 marker
      open_wave_config C:/path/wave.wcfg ;# 从干净磁盘文件重载
      ```

    **set_property / radix 写脚本陷阱**(0.3.20 实战沉淀,无 err 静默踩):

    - **`-filter "name =~ {...[$var]...}"` 会污染后续 `set_property` 静默失败**。
      `[$var]` 触发 Tcl 命令替换,虽然 get_scopes 内部 fallback 仍返回正确对象,
      但污染后续 wave property 路径,`set_property RADIX dec $w` 静默不生效。
      改用 `foreach + regexp` 自己过滤,绕开 filter 字符串里的 `[$var]`。

    - **`set_property RADIX` value 大小写敏感**(大多数 Vivado property 是大小写
      不敏感的,这条是反直觉的例外):
      ```tcl
      set_property RADIX dec $w   ;# ✓ RADIX=dec
      set_property RADIX DEC $w   ;# ✗ 静默退回 RADIX=default,不报错
      ```

    - **`add_wave -radix` 和 `set_property RADIX` 接受的 value 集合不一致**:
      ```
      add_wave -radix : default | dec | bin | oct | hex | unsigned | ascii | smag
      set_property RADIX: dec | hex(其他实测未通过;大写一律不接受)
      ```
      signed decimal 在 XSim 叫 `dec`,**不是** `signed`(从 ModelSim/QuestaSim
      带过来的命名习惯会踩)。

    - **Analog 波形可纯 Tcl 渲染(早期文档误判"无 Tcl 接口"的真根因)**。
      WaveformStyle 不在 `list_property $w` / `set_property` 全集里,要用专用命令
      `set_wave_prop`。当年踩坑是因为值写成了裸 `ANALOG`——Vivado 收下不报错但
      渲染器不认,只改属性值不渲染(静默接受陷阱)。**正确值必须带 `STYLE_` 前缀**,
      且信号寻址有两个静默坑(实测 2019.1):

      ```tcl
      # ★ get_waves 按"显示名"(如 y0[15:0])/glob 匹配,传全路径 /tb/y0 返回空!
      #   且 set_wave_prop 对空对象 rc=0 静默接受 → 信号没 add 会伪装成功,务必先判空。
      set w [get_waves -quiet y0*]   ;# 用显示名/glob;或遍历 get_waves * 按 DESIGN_OBJECT 全路径过滤
      if {[llength $w]} {
        set_wave_prop WaveformStyle STYLE_ANALOG $w  ;# ★ 裸 ANALOG 静默吞值不渲染
        set_wave_prop AnalogMin -2048 $w             ;# 贴数据范围:太宽压平,太窄削顶
        set_wave_prop AnalogMax  2047 $w
        set_wave_prop AnalogInterpolation LINEAR $w
        set_property HEIGHT 80 $w                     ;# 存为 CellHeight
      }
      ```

      - **无法 Tcl 读回**:`get_wave_prop` 不存在、`report_wave_props` 输出不可捕获,
        设完只能人眼确认渲染(MCP `set_wave_analog` 工具已封装寻址 + STYLE_ 前缀)。
      - **重载冲掉 analog**:先定好 zoom 再实时上 analog,别先改 analog 再 open_wave_config。
      - **wcfg 磁盘路径属性是 `FILE_PATH`**(不是 FILE_NAME,后者报 [Common 17-54]):
        `get_property FILE_PATH [current_wave_config]`(MCP `set_wave_zoom` 改 zoom_setting 用此)。

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
