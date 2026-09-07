"""离线 introspection 工具:parse_xpr / parse_bit_header / parse_ltx。

三个纯 Python 离线工具,不启动 Vivado 直接解析工程 / 比特流 / ILA 探针文件。
同 compare_xci 范式:解析逻辑在 analysis/,本文件只做薄壳(参数收集 + 调 parser
+ try/except 兜底 + 返回中文摘要)。无 Vivado 会话依赖。

为什么这三个值得做工具(都满足"Tcl 做不了或做不好"):
  - parse_xpr     —— get_project_info 需 start_session + open_project(中文路径会
                     TclStackFree 崩);离线读 .xpr 秒级摸底,CI 友好。
  - parse_bit_header —— Vivado 无任何 Tcl 命令读离线 .bit;烧前防错板 / 交付对账。
  - parse_ltx     —— get_hw_probes 需板子在手 + 活 hw session;离线读探针清单。
"""

from mcp.server.mcpserver import Context

from vivado_mcp.analysis.bit_header_parser import format_bit as _format_bit
from vivado_mcp.analysis.bit_header_parser import parse_bit as _parse_bit
from vivado_mcp.analysis.ltx_parser import format_ltx as _format_ltx
from vivado_mcp.analysis.ltx_parser import parse_ltx as _parse_ltx
from vivado_mcp.analysis.xpr_parser import format_xpr as _format_xpr
from vivado_mcp.analysis.xpr_parser import parse_xpr as _parse_xpr
from vivado_mcp.server import mcp
from vivado_mcp.tools.annotations import READ_ONLY_LOCAL


@mcp.tool(annotations=READ_ONLY_LOCAL)
async def parse_xpr(file_path: str, ctx: Context = None) -> str:
    """离线解析 Vivado 工程文件(.xpr),无需启动 Vivado。

    秒级摸底陌生工程 / CI 门禁:不启 Vivado(避开 120s GUI 冷启 + 中文路径
    TclStackFree 崩),纯 Python 读 .xpr 拿 part / 顶层 / 源文件(按 fileset 分组,
    含 .v/.mem/.xci IP)/ XDC 约束 / synth+impl runs 及 Strategy。
    对照 get_project_info(需先 start_session + open_project),本工具完全离线。

    Args:
        file_path: .xpr 工程文件的绝对路径。
    """
    try:
        cfg = _parse_xpr(file_path)
    except (FileNotFoundError, ValueError) as e:
        return f"[ERROR] .xpr 解析失败: {e}"
    return _format_xpr(cfg)


@mcp.tool(annotations=READ_ONLY_LOCAL)
async def parse_bit_header(file_path: str, ctx: Context = None) -> str:
    """离线解析 .bit 比特流文件头部,无需启动 Vivado。

    只读文件头(不读 payload):提取设计名 / 目标 part(原始 + 规整)/ 构建日期时间 /
    文件 SHA256。用于烧录前防错板(part 比对)、交付/返修对账(确认孤立 .bit 是不是
    声称的那版)。Vivado 无任何 Tcl 命令读离线 .bit。
    注意:.bit 里 part 去 'xc' 前缀 + 去速度等级(如 7k325tffg900);规整字段补回
    'xc' 但速度等级无法还原,与 .xpr 的 part 比对时只能比到 package 级。

    Args:
        file_path: .bit 文件的绝对路径。
    """
    try:
        header = _parse_bit(file_path)
    except (FileNotFoundError, ValueError) as e:
        return f"[ERROR] .bit 解析失败: {e}"
    return _format_bit(header)


@mcp.tool(annotations=READ_ONLY_LOCAL)
async def parse_ltx(file_path: str, ctx: Context = None) -> str:
    """离线解析 ILA 调试探针文件(.ltx),无需连板 / 启动 Vivado。

    连板 ILA 抓波前先离线拿清单:每个 hw_ila 挂哪些 probe、probe 名、位宽、映射的
    net。辅助在写 set_property TRIGGER_COMPARE_VALUE eq<位宽>'h.. [get_hw_probes
    <probe>] 之前确认正确的 probe 名和宽度。get_hw_probes 需板子在手 + 活 hw
    session,本工具完全离线。Vivado 2019.1 的 .ltx 是 JSON 格式。

    Args:
        file_path: .ltx 文件的绝对路径。
    """
    try:
        cfg = _parse_ltx(file_path)
    except (FileNotFoundError, ValueError) as e:
        return f"[ERROR] .ltx 解析失败: {e}"
    return _format_ltx(cfg)
