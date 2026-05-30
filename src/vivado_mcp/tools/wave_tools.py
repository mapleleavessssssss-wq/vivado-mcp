"""波形(wave)显示工具：set_wave_zoom / set_wave_analog。

**设计哲学(quality §1.3)**：这两个工具不是 facade，它们封装的是"Tcl 单脚本做不了 /
AI 极易搞砸"的本地价值：

- ``set_wave_zoom`` —— **跨命令协议**(§1.3 条件3)。设缩放窗要"解析 wcfg 磁盘路径 →
  改 XML(Vivado 2019.1 无 Tcl zoom 命令)→ close -force → open 重载"这条多步序列，
  漏 -force 报 [Wavedata 42-26]、同名已开报 42-52，AI 拼一次就踩。
- ``set_wave_analog`` —— **本地知识库**(§1.3 条件2)。三个实测静默坑:(1) WaveformStyle
  值必须 ``STYLE_ANALOG``,裸传 ``ANALOG`` Vivado 收下不报错但渲染器不认;(2) ``get_waves``
  只匹配显示名(``y0[15:0]``),传全路径 ``/tb/y0`` 返回空,工具自动按 DESIGN_OBJECT/
  显示名解析;(3) ``set_wave_prop`` 对空对象 rc=0 静默接受,故必须先判空。wave 属性无
  get 接口(``get_wave_prop`` 不存在),无法 Tcl 读回,渲染靠人眼确认。

两工具的 XML 改写下沉到 ``analysis/wcfg_editor.py``(纯 Python 可单测)，长 Tcl 在
``tcl_scripts.py``，本文件只留薄壳 + 解析 helper。
"""

from __future__ import annotations

from mcp.server.fastmcp import Context

from vivado_mcp.analysis import wcfg_editor
from vivado_mcp.server import _NO_SESSION, _require_session, mcp
from vivado_mcp.tcl_scripts import RELOAD_WCFG, RESOLVE_WCFG_PATH, SET_ANALOG_PROPS

# AnalogInterpolation 白名单。0.3.2x 用户实测仅验证 LINEAR；HOLD 是 Vivado 另一
# 合法值一并放行，其余值直接拒(避免静默退回 default)。
_INTERP_WHITELIST = ("LINEAR", "HOLD")

# ====================================================================== #
#  内部 helper(解析 / 算幅 / 转义)——逻辑放这里，工具壳保持 ≤80 行
# ====================================================================== #


def _parse_wcfg_path(raw: str) -> tuple[str, str | None]:
    """解析 RESOLVE_WCFG_PATH 输出。

    Returns:
        (status, path)：status ∈ {"none", "unsaved", "path"}；
        status=="path" 时 path 为磁盘路径，否则为 None。
        解析不到任何 VMCP_WCFG 行时返回 ("none", None)。
    """
    for line in raw.splitlines():
        s = line.strip()
        if s == "VMCP_WCFG:none":
            return ("none", None)
        if s == "VMCP_WCFG:unsaved":
            return ("unsaved", None)
        if s.startswith("VMCP_WCFG:path="):
            return ("path", s[len("VMCP_WCFG:path="):])
    return ("none", None)


def _parse_reload(raw: str) -> str | None:
    """解析 RELOAD_WCFG 输出，返回错误原因(中文 None 表示成功)。"""
    for line in raw.splitlines():
        s = line.strip()
        if s.startswith("VMCP_RELOAD_OK:"):
            return None
        if s.startswith("VMCP_RELOAD_ERR:"):
            return s[len("VMCP_RELOAD_ERR:"):]
    return "重载未返回任何结果标记(VMCP_RELOAD_OK/ERR 都缺失)"


def _build_signals_block(signals: list[str]) -> str:
    """把 signals 列表转成 Tcl ``lappend __sigs {<信号>}`` 行块。

    用 ``{...}`` 包裹信号字面量，绕开 Tcl 对 ``[N]`` / ``$`` 的命令/变量替换
    (run_tcl docstring 既有坑)。信号名含 ``{`` / ``}`` 时拒绝(防破坏花括号配对)。
    """
    lines = []
    for sig in signals:
        if "{" in sig or "}" in sig:
            raise ValueError(f"信号名含花括号，无法安全传入 Tcl: {sig!r}")
        lines.append("lappend __sigs {" + sig + "}")
    return "\n".join(lines)


def _resolve_amplitude(
    vmin: float | None, vmax: float | None
) -> tuple[float, float] | None:
    """决定 AnalogMin / AnalogMax：显式 min/max 都给齐才用，否则 None。

    单边(只给 min 或只给 max)不足以定对称幅度，按缺失处理。

    注:从仿真数据自动算幅(absmax×1.1)需 live 采样，数据源未定(get_value 采样 /
    读 wdb / testbench 输出文件各有取舍)，本版本不实现 auto，要求显式传值。

    Returns:
        (amin, amax) 或 None(未给齐，需调用方显式传值)。
    """
    if vmin is not None and vmax is not None:
        return (vmin, vmax)
    return None


def _parse_analog_result(raw: str, signals: list[str]) -> tuple[list[str], list[str]]:
    """解析 SET_ANALOG_PROPS 输出(格式见 tcl_scripts.SET_ANALOG_PROPS 注释)。

    Returns:
        (ok_lines, problem_lines)：
        - ok_lines: 成功设为 STYLE_ANALOG 的信号(附命中的全路径)
        - problem_lines: 未命中 add_wave / set 报错 / Tcl 未回报 的中文说明
    """
    ok_lines: list[str] = []
    problem_lines: list[str] = []
    seen: set[str] = set()

    for line in raw.splitlines():
        s = line.strip()
        if not s.startswith("VMCP_ANALOG:sig="):
            continue
        body = s[len("VMCP_ANALOG:sig="):]
        sig, _, rest = body.partition("|")
        seen.add(sig)
        if rest.startswith("ok="):
            resolved = rest[len("ok="):]
            note = f"(命中 {resolved})" if resolved and resolved != sig else ""
            ok_lines.append(f"  ✓ {sig}{note} → STYLE_ANALOG")
        elif rest == "found=0":
            problem_lines.append(
                f"  ✗ {sig} → 波形里找不到该信号(请先 add_wave)。工具已自动按全路径/"
                f"显示名(如 y0[15:0])解析仍未命中,说明信号确实不在波形中。"
            )
        elif rest.startswith("set_err="):
            problem_lines.append(f"  ✗ {sig} → set_wave_prop 失败: {rest[len('set_err='):]}")

    for sig in signals:
        if sig not in seen:
            problem_lines.append(f"  ✗ {sig} → Tcl 未回报该信号(脚本可能中途异常)。")

    return ok_lines, problem_lines


# ====================================================================== #
#  MCP 工具壳
# ====================================================================== #


@mcp.tool()
async def set_wave_zoom(
    start_ns: float,
    end_ns: float,
    wcfg: str = None,
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """设置波形时间缩放窗口(改 wcfg XML 后 close -force + open 重载)。

    本地价值(§1.3 条件3 跨命令协议):Vivado 2019.1 无 Tcl zoom 命令，缩放窗存在
    .wcfg 的 <zoom_setting> 里，要改它再让 XSim 重载。重载顺序极易踩——
    close 漏 -force 报 [Wavedata 42-26];同名 wcfg 已 open 时直接 open 报 42-52，
    故必须先 close 再 open。本工具封装这条多步协议。

    ⚠ wcfg 未保存(current_wave_config 的 FILE_PATH 为空)时返回明确错误，要求先
    save_wave_config——不自动存盘(避免擅自改用户磁盘文件)。
    ⚠ close_wave_config -force 会丢弃所有未存盘的 live wave prop(含手设的 radix/
    analog),故先 set_wave_zoom 再 set_wave_analog。

    Args:
        start_ns: 缩放起始时间(ns)，须 < end_ns。
        end_ns: 缩放结束时间(ns)。
        wcfg: 显式 wcfg 磁盘路径；缺省自动解析 current_wave_config。
        session_id: 目标会话 ID。
    """
    if start_ns >= end_ns:
        return f"[ERROR] start_ns 须小于 end_ns(收到 start={start_ns}, end={end_ns})。"

    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    wcfg_path = wcfg
    if not wcfg_path:
        try:
            r = await session.execute(RESOLVE_WCFG_PATH, timeout=30.0)
        except Exception as e:
            return f"[ERROR] 解析当前 wcfg 路径失败: {e}"
        status, wcfg_path = _parse_wcfg_path(r.output)
        if status == "none":
            return "[ERROR] 当前无 wave_config。请先 open_wave_config 或运行仿真。"
        if status == "unsaved":
            return (
                "[ERROR] 当前 wave_config 从未保存(FILE_NAME 为空)，磁盘上没有可改的 "
                ".wcfg。请先 save_wave_config <路径>，再调 set_wave_zoom。"
            )

    try:
        wcfg_editor.set_zoom_range(wcfg_path, start_ns, end_ns)
    except (FileNotFoundError, ValueError) as e:
        return f"[ERROR] 改写 wcfg 缩放区间失败: {e}"

    tcl_path = wcfg_path.replace("\\", "/")
    try:
        r2 = await session.execute(RELOAD_WCFG.format(wcfg_path=tcl_path), timeout=60.0)
    except Exception as e:
        return f"[ERROR] 重载 wcfg 失败: {e}(XML 已改，可手动 open_wave_config {tcl_path})"

    err = _parse_reload(r2.output)
    if err:
        return f"[ERROR] 重载 wcfg 失败: {err}"
    return f"[OK] 已设缩放窗 {start_ns}~{end_ns} ns 并重载: {tcl_path}"


@mcp.tool()
async def set_wave_analog(
    signals: list[str],
    min: float = None,
    max: float = None,
    interp: str = "LINEAR",
    height: int = 120,
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """把信号设为模拟(Analog)波形显示(纯 Tcl 配方,实测 Vivado 2019.1 可渲染)。

    本地价值(§1.3 条件2 本地知识库,三个实测静默坑):
    1. WaveformStyle 必须传 STYLE_ANALOG(带 STYLE_ 前缀);裸传 ANALOG 时 Vivado 收下
       不报错但渲染器不认,只显示数字格。推翻早期"Analog 无 Tcl 接口只能 GUI 右键"的误判。
    2. get_waves 只匹配显示名(y0[15:0])/glob,直接传全路径 /tb/y0 返回空;工具自动按
       DESIGN_OBJECT(全路径)/FULL_NAME/显示名解析,不命中才报 add_wave。
    3. set_wave_prop 对空对象 rc=0 静默接受(信号没 add 会伪装成功),工具先判空再 set。
    wave 属性无 get 接口(get_wave_prop 不存在),无法 Tcl 读回校验,渲染请人眼确认。

    ⚠ 顺序硬约束:必须在 set_wave_zoom 之后调——zoom 重载(close+open wcfg)会冲掉
    先设的 analog prop。
    ⚠ AnalogMin/Max 必须成对显式传(贴数据范围:太宽压平、太窄削顶)。从仿真数据
    自动算幅需 live 采样、数据源未定，本版本不做 auto。

    Args:
        signals: 信号列表(短名或全路径，须已 add_wave 进波形)。
        min: AnalogMin(必传，与 max 成对)。
        max: AnalogMax(必传，与 min 成对)。
        interp: 插值方式，白名单 LINEAR / HOLD。
        height: 行高像素(存盘后在 wcfg 里叫 CellHeight)。
        session_id: 目标会话 ID。
    """
    if not signals:
        return "[ERROR] signals 为空，请至少给一个信号。"
    if interp.upper() not in _INTERP_WHITELIST:
        return (
            f"[ERROR] interp '{interp}' 不在白名单 {_INTERP_WHITELIST}"
            "(其余值 Vivado 会静默退回)。"
        )
    if not isinstance(height, int) or height <= 0:
        return f"[ERROR] height 须为正整数(收到 {height!r})。"

    amplitude = _resolve_amplitude(min, max)
    if amplitude is None:
        return (
            "[ERROR] 需成对显式传 min= 与 max=(贴数据范围:太宽压平、太窄削顶)。"
            "从仿真数据自动算幅需 live 采样、数据源未定，本版本不做 auto。"
        )

    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    try:
        signals_block = _build_signals_block(signals)
    except ValueError as e:
        return f"[ERROR] {e}"

    tcl = SET_ANALOG_PROPS.format(
        signals_block=signals_block,
        amin=amplitude[0],
        amax=amplitude[1],
        interp=interp.upper(),
        height=height,
    )
    try:
        r = await session.execute(tcl, timeout=60.0)
    except Exception as e:
        return f"[ERROR] 设置 analog 属性失败: {e}"

    ok_lines, problem_lines = _parse_analog_result(r.output, signals)
    header = (
        f"Analog 设置(min={amplitude[0]}, max={amplitude[1]}, "
        f"interp={interp.upper()}, height={height}):"
    )
    body = "\n".join(ok_lines + problem_lines)
    if problem_lines:
        return f"[WARN] {header}\n{body}"
    return f"[OK] {header}\n{body}"
