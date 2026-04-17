"""FastMCP 服务器实例、lifespan 管理、工具注册、Resources & Prompts。

架构：
  Claude Code ──(stdio)──▶ FastMCP Server
                                │
                          SessionManager (lifespan context)
                          ├─ "default" ──▶ vivado -mode tcl
                          └─ ...
"""

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from mcp.server.fastmcp import FastMCP

from vivado_mcp.config import find_vivado
from vivado_mcp.vivado.session import VivadoSession
from vivado_mcp.vivado.session_manager import SessionManager

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

# 模块级 SessionManager 引用，供 Resources 使用（lifespan 中设置）
_manager_ref: SessionManager | None = None


@dataclass
class AppContext:
    """应用上下文，通过 lifespan 注入到所有工具函数中。"""
    session_manager: SessionManager


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """MCP 服务器生命周期管理。

    启动时初始化 SessionManager，关闭时清理所有 Vivado 会话。
    """
    global _manager_ref

    # 检测 Vivado 路径（启动时即验证，快速报错）
    try:
        vivado_path = find_vivado()
        logger.info("检测到 Vivado: %s", vivado_path)
    except FileNotFoundError as e:
        logger.warning("Vivado 路径检测失败: %s", e)
        logger.warning("工具仍可使用，但需要在 start_session 时手动指定路径。")
        vivado_path = ""

    manager = SessionManager(vivado_path=vivado_path)
    _manager_ref = manager
    try:
        yield AppContext(session_manager=manager)
    finally:
        _manager_ref = None
        await manager.close_all()


# 创建 FastMCP 实例
mcp = FastMCP(
    "vivado-mcp",
    lifespan=app_lifespan,
)


# --------------------------------------------------------------------------- #
#  辅助函数（DRY：所有工具共享）
# --------------------------------------------------------------------------- #

def _get_manager(ctx) -> SessionManager:
    """从 MCP Context 中提取 SessionManager。"""
    app_ctx: AppContext = ctx.request_context.lifespan_context
    return app_ctx.session_manager


_NO_SESSION = "[ERROR] 会话 '{sid}' 不存在。请先调用 start_session。"


def _require_session(ctx, session_id: str) -> VivadoSession | None:
    """获取会话，不存在返回 None。"""
    return _get_manager(ctx).get(session_id)


async def _safe_execute(
    session: VivadoSession,
    tcl: str,
    timeout: float,
    error_label: str,
) -> str:
    """安全执行 Tcl 命令，异常时返回错误字符串而非抛出。"""
    try:
        result = await session.execute(tcl, timeout=timeout)
        return result.summary
    except Exception as e:
        return f"[ERROR] {error_label}: {e}"


# --------------------------------------------------------------------------- #
#  MCP Resources（会话状态查询）
#  注意：Resources 不支持 Context 注入，使用模块级 _manager_ref
# --------------------------------------------------------------------------- #

@mcp.resource("vivado://sessions")
def resource_sessions() -> str:
    """所有 Vivado 会话的状态信息（JSON）。"""
    if _manager_ref is None:
        return json.dumps({"sessions": [], "message": "服务器未就绪"})
    sessions = _manager_ref.list_sessions()
    if not sessions:
        return json.dumps({"sessions": [], "message": "当前没有活跃会话"})
    return json.dumps({"sessions": sessions}, ensure_ascii=False)


@mcp.resource("vivado://session/{session_id}/status")
def resource_session_status(session_id: str) -> str:
    """单个 Vivado 会话的详细状态（JSON）。"""
    if _manager_ref is None:
        return json.dumps({"error": "服务器未就绪"})
    session = _manager_ref.get(session_id)
    if not session:
        return json.dumps({"error": f"会话 '{session_id}' 不存在"})
    return json.dumps(session.status_dict(), ensure_ascii=False)


# --------------------------------------------------------------------------- #
#  MCP Prompts（工作流引导）
# --------------------------------------------------------------------------- #

@mcp.prompt()
def fpga_workflow() -> str:
    """标准 FPGA 开发流程引导：从创建项目到生成比特流。

    **0.2.0 变更**：项目操作全部用 run_tcl/safe_tcl，不再有专用 facade 工具。
    """
    return (
        "请按以下标准 FPGA 开发流程操作（0.2.0 起所有项目操作走 run_tcl/safe_tcl）：\n\n"
        "1. **启动会话**: `start_session(mode='gui')` — 默认启动 GUI Vivado 可视化。\n"
        "   CI 批处理用 `mode='tcl'`；attach 到已有 Vivado 用 `mode='attach'`。\n"
        "2. **创建项目**: `safe_tcl(\"create_project {0} {1} -part {2}\", \n"
        "   args=['my_proj', 'C:/proj', 'xc7a35tcpg236-1'])`\n"
        "3. **添加源文件**: `safe_tcl(\"add_files -fileset [get_filesets sources_1] {0}\", \n"
        "   args=['C:/src/top.v'])`\n"
        "4. **设置顶层**: `run_tcl(\"set_property top my_top [current_fileset]\")`\n"
        "5. **综合**: `run_synthesis` — 完成后自动 open_run，后续 report_* 可直接用\n"
        "6. **查看资源**: `run_tcl(\"report_utilization -return_string\")`\n"
        "7. **实现**: `run_implementation`\n"
        "8. **时序检查**: `get_timing_report` — 结构化中文报告，PASS/FAIL 判定\n"
        "9. **生成比特流**: `generate_bitstream` — 前置 CRITICAL WARNING 安全检查\n"
        "10. **编程设备**: `program_device`\n\n"
        "查询运行状态: `run_tcl(\"get_property STATUS [get_runs synth_1]\")`\n"
        "设计规则检查: `run_tcl(\"report_drc -return_string\")`\n"
        "遇到 CRITICAL WARNING: `get_critical_warnings` 提取分类 + 中文修复建议。"
    )


@mcp.prompt()
def debug_timing() -> str:
    """时序违例调试引导：定位和修复时序问题。"""
    return (
        "时序违例调试流程：\n\n"
        "1. **查看时序报告**: `get_timing_report` 获取结构化时序摘要\n"
        "2. **分析关键路径**: 关注 WNS (Worst Negative Slack)\n"
        "   - WNS < 0 表示时序违例\n"
        "   - 查看违例路径的起点和终点\n"
        "3. **检查时钟约束**: `run_tcl('report_clocks')` 确认时钟定义正确\n"
        "4. **查看利用率**: `report(type='utilization')` 检查是否资源过度使用\n"
        "5. **检查拥塞**: `report(type='congestion')` 分析布线拥塞\n\n"
        "常见修复方法：\n"
        "- 添加流水线寄存器拆分长路径\n"
        "- 调整时钟频率约束\n"
        "- 使用 `set_false_path` / `set_multicycle_path` 排除非关键路径\n"
        "- 手动布局关键模块 (`set_property LOC`)"
    )


@mcp.prompt()
def debug_gt_mapping() -> str:
    """GT 高速收发器引脚映射调试引导。"""
    return (
        "GT 引脚映射调试流程：\n\n"
        "当 PCIe/GTX/GTH 链路无法建立时，首先排除物理层引脚问题：\n\n"
        "1. **检查 CRITICAL WARNING**: `get_critical_warnings` 查看是否有 "
        "[Vivado 12-1411] 引脚冲突\n"
        "   - 此 warning 表示 XDC 的 PACKAGE_PIN 约束与 IP 内部 GT LOC 冲突\n"
        "   - 常见原因：XDC 引脚顺序与 IP 配置的 Lane 映射不一致\n\n"
        "2. **验证 IO 布局**: `verify_io_placement` 对比 XDC 约束与实际分配\n"
        "   - CRITICAL 级别不匹配 = GT 引脚错误（必须修复）\n"
        "   - WARNING 级别不匹配 = GPIO 引脚偏差（通常不影响链路）\n\n"
        "3. **查看 IO 报告**: `get_io_report` 获取所有端口的实际引脚分配\n"
        "   - 重点检查 rxp/rxn/txp/txn 各 lane 的 Bank 和 Site\n\n"
        "4. **核实 Lane 映射**:\n"
        "   - 对照 PCB 原理图确认 GT 引脚与物理走线的对应关系\n"
        "   - 检查 IP Customization 中的 Lane Reversal 设置\n"
        "   - 查看 GT Location 约束是否正确\n\n"
        "修复方法：\n"
        "- 删除 XDC 中的 GT PACKAGE_PIN 约束（让 IP 自动放置）\n"
        "- 或修正 XDC 引脚顺序使其与 IP 内部 LOC 一致\n\n"
        "5. **查看 IP GT 配置**: `inspect_ip_params(ip_name='<name>', filter_keyword='gt')`\n"
        "   - 列出所有 GT 相关的 CONFIG.* 参数（含 GUI 中隐藏的参数）\n"
        "   - 重点关注 PCIE_GT_DEVICE / GT_LOC / LANE_WIDTH 等参数\n\n"
        "6. **生成 GT 通道映射表**: 组合 `get_io_report` + `inspect_ip_params` 数据\n"
        "   - 将 rxp/rxn/txp/txn 各 lane 的 Bank/Site 与 IP 内部 GT Location 对照\n"
        "   - 验证物理走线与 IP 配置的 Lane 映射是否一致\n\n"
        "**架构差异提醒**：\n"
        "- 7-Series `pcie_7x` 的 GT LOC 由 `.ttcl` 模板无条件生成，"
        "`disable_gt_loc` 参数不会传递到子 IP，设了也无效\n"
        "- 只有 UltraScale+(GT Wizard)才支持 `disable_gt_loc` 参数\n"
        "- 7-Series 修复方法：只能删除 XDC 中 GT PACKAGE_PIN 或修正引脚顺序"
    )


@mcp.prompt()
def debug_ip_config() -> str:
    """IP 配置调试引导：诊断 Vivado IP 参数问题。"""
    return (
        "IP 配置调试流程：\n\n"
        "当怀疑 IP 配置不正确时（如 PCIe 链路不通、GT 通道映射错误）：\n\n"
        "## 1. 查看 IP 所有配置参数\n"
        "```\n"
        "inspect_ip_params(ip_name='xdma_0')\n"
        "```\n"
        "- 列出所有 CONFIG.* 参数（含 GUI 中不可见的隐藏参数）\n"
        "- 通过 Vivado Tcl API `list_property + get_property` 直接获取\n\n"
        "## 2. 按关键词过滤\n"
        "```\n"
        "inspect_ip_params(ip_name='xdma_0', filter_keyword='gt')\n"
        "inspect_ip_params(ip_name='xdma_0', filter_keyword='lane')\n"
        "inspect_ip_params(ip_name='xdma_0', filter_keyword='loc')\n"
        "inspect_ip_params(ip_name='xdma_0', filter_keyword='pcie')\n"
        "```\n\n"
        "## 3. 对比两个 XCI 配置（无需 Vivado 会话）\n"
        "```\n"
        "compare_xci(\n"
        "    file_a='path/to/golden.xci',  # 基准/正常配置\n"
        "    file_b='path/to/suspect.xci', # 待检查/异常配置\n"
        ")\n"
        "```\n"
        "- XCI 是 XML 格式，直接解析对比参数差异\n"
        "- 适用于：版本对比、不同板卡间配置迁移验证\n\n"
        "## 4. 查看 xgui/*.tcl 文件（高级）\n"
        "- 位置: `<IP_DIR>/xgui/<ip_name>_v*.tcl`\n"
        "- 包含参数的条件可见性逻辑（哪些参数在什么条件下显示/隐藏）\n"
        "- 搜索 `PARAM_VALUE.` 可找到所有可配置参数\n\n"
        "## 架构差异警告\n"
        "- **`disable_gt_loc`** 仅对 UltraScale+(GT Wizard IP)有效\n"
        "- 7-Series 使用 `pcie_7x` IP，其 `.ttcl` 模板**无条件生成** GT LOC 约束\n"
        "- 7-Series 设置 `disable_gt_loc=true` 不会传递到子 IP，无任何效果\n\n"
        "## 常见 IP 配置问题\n"
        "| 问题 | 检查参数 |\n"
        "|------|----------|\n"
        "| Lane Width 不对 | CONFIG.PF0_DEVICE_ID, LANE_WIDTH |\n"
        "| RefClk 频率错误 | CONFIG.REF_CLK_FREQ, CONFIG.PCIE_REFCLK_FREQ |\n"
        "| Lane 翻转 | CONFIG.PCIE_LANE_REVERSAL |\n"
        "| GT 位置冲突 | CONFIG.PCIE_GT_DEVICE, CONFIG.*GT_LOC* |"
    )


@mcp.prompt()
def debug_pcie() -> str:
    """PCIe 调试引导：从物理层到协议层的系统化排查。"""
    return (
        "PCIe 系统化调试流程（从底层到上层）：\n\n"
        "## 第一层：物理引脚（最常见问题源）\n"
        "1. `get_critical_warnings` — 检查 GT 引脚冲突警告\n"
        "2. `verify_io_placement` — 验证 XDC 约束与实际布局\n"
        "3. `get_io_report` — 确认所有 GT 端口的 Bank 和 Site\n\n"
        "## 第二层：时钟与复位\n"
        "4. `report(type='clock')` — 检查参考时钟 (REFCLK) 频率\n"
        "5. 确认 PERST# 复位信号的 IOSTANDARD 和极性\n\n"
        "## 第三层：时序\n"
        "6. `get_timing_report` — 检查时序是否收敛\n"
        "   - GT 内部时钟 (userclk2) 是否 MET\n\n"
        "## 第四层：协议\n"
        "7. 检查 LTSSM 状态: 使用 DRP 读取 GT 状态寄存器\n"
        "8. 检查 Link Speed / Width 是否达到预期\n\n"
        "关键经验：\n"
        "- 80% 的 PCIe 链路问题源于第一层（引脚映射错误）\n"
        "- 在检查协议层之前，务必先确认物理层无误\n"
        "- [Vivado 12-1411] 是最需要关注的 CRITICAL WARNING"
    )


# --------------------------------------------------------------------------- #
#  导入工具模块，触发 @mcp.tool() 装饰器注册
# --------------------------------------------------------------------------- #

import vivado_mcp.tools.diagnostic_tools  # noqa: E402, F401
import vivado_mcp.tools.flow_tools  # noqa: E402, F401
import vivado_mcp.tools.ip_tools  # noqa: E402, F401
import vivado_mcp.tools.report_tools  # noqa: E402, F401
import vivado_mcp.tools.session_tools  # noqa: E402, F401
import vivado_mcp.tools.tcl_tools  # noqa: E402, F401
