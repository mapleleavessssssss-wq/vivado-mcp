"""IP 调试工具：inspect_ip_params / compare_xci。

inspect_ip_params 通过 Vivado Tcl API 查询 IP 实例的所有 CONFIG.* 参数。
compare_xci 纯 Python 解析两个 XCI 文件并对比参数差异，无需 Vivado 会话。
"""

from mcp.server.fastmcp import Context

from vivado_mcp.analysis.ip_param_parser import parse_ip_params
from vivado_mcp.analysis.xci_parser import (
    compare_xci_configs,
    format_xci_compare,
    parse_xci,
)
from vivado_mcp.server import _NO_SESSION, _require_session, mcp
from vivado_mcp.tcl_scripts import INSPECT_IP_PARAMS
from vivado_mcp.vivado.tcl_utils import validate_identifier


@mcp.tool()
async def compare_xci(
    file_a: str,
    file_b: str,
    show_all: bool = False,
    ctx: Context = None,
) -> str:
    """对比两个 XCI 文件的 IP 配置差异。

    无需 Vivado 会话，直接读取 XML 文件对比参数。
    适用于版本对比、不同板卡间配置迁移验证、调试 IP 参数差异。

    Args:
        file_a: 第一个 XCI 文件路径（如基准/正常配置）。
        file_b: 第二个 XCI 文件路径（如待检查/异常配置）。
        show_all: 是否显示所有参数（默认仅显示差异）。
    """
    # 解析两个 XCI 文件
    try:
        config_a = parse_xci(file_a)
    except (FileNotFoundError, ValueError) as e:
        return f"[ERROR] 文件 A 解析失败: {e}"

    try:
        config_b = parse_xci(file_b)
    except (FileNotFoundError, ValueError) as e:
        return f"[ERROR] 文件 B 解析失败: {e}"

    # 对比并格式化
    result = compare_xci_configs(config_a, config_b)
    return format_xci_compare(result, show_all=show_all)


@mcp.tool()
async def inspect_ip_params(
    ip_name: str,
    filter_keyword: str = "",
    session_id: str = "default",
    ctx: Context = None,
) -> str:
    """查询 IP 实例的所有配置参数（含 GUI 中隐藏的参数）。

    通过 Vivado Tcl API 获取指定 IP 的所有 CONFIG.* 属性及其当前值。
    支持按关键词过滤（如 "gt"、"loc"、"lane"），不区分大小写。

    Args:
        ip_name: IP 实例名称（如 "xdma_0"）。
        filter_keyword: 可选过滤关键词（如 "gt"、"loc"、"lane"），不区分大小写。
        session_id: 目标会话 ID。
    """
    try:
        ip_name = validate_identifier(ip_name, "ip_name")
    except ValueError as e:
        return f"[ERROR] {e}"

    session = _require_session(ctx, session_id)
    if not session:
        return _NO_SESSION.format(sid=session_id)

    # 执行 Tcl 脚本
    tcl = INSPECT_IP_PARAMS.format(ip_name=ip_name)
    try:
        result = await session.execute(tcl, timeout=30.0)
    except Exception as e:
        return f"[ERROR] 查询 IP 参数失败: {e}"

    # B2 同款守卫:命令失败时不要把错误文本送进 parser,
    # 否则会解析出"0 个参数"的假报告,真错误(如无项目打开)被丢弃
    if result.is_error:
        return (
            f"[ERROR] 查询 IP 参数失败(rc={result.return_code}):\n"
            f"{result.output}\n"
            "提示: 需要项目已打开(run_tcl 'open_project <path>.xpr')"
        )

    # 解析输出
    report = parse_ip_params(result.output, ip_name)

    if report is None:
        return f"[ERROR] IP '{ip_name}' 未找到。请确认 IP 实例名称正确且项目已打开。"

    return report.format(keyword=filter_keyword)
