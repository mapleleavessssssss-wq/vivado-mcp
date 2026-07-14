"""FastMCP 服务器实例、lifespan 管理、工具注册、Resources & Prompts。

架构：
  Claude Code ──(stdio)──▶ FastMCP Server
                                │
                          SessionManager (lifespan context)
                          ├─ "default" ──▶ vivado -mode tcl
                          └─ ...
"""

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from mcp.server.fastmcp import FastMCP

from vivado_mcp.config import find_vivado
from vivado_mcp.vivado.session import VivadoSession
from vivado_mcp.vivado.session_manager import SessionManager

# 配置日志:默认 WARNING(logging-guidelines §2:INFO/DEBUG 不出现在生产用户终端,
# 用户必须看到的信息走 WARNING+),调试时设环境变量 LOG_LEVEL=INFO/DEBUG 覆盖。
_LOG_LEVEL = os.environ.get("LOG_LEVEL", "WARNING").upper()
if _LOG_LEVEL not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
    _LOG_LEVEL = "WARNING"
logging.basicConfig(
    level=_LOG_LEVEL,
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


# --------------------------------------------------------------------------- #
#  W 模式 quirk hints:在 _safe_execute 返回前匹配关键词追加固定提示
#
#  哲学:不改写 Vivado 原始输出(透传契约),只在末尾追加"看到 X 时该怎么办"。
#  AI 在 run_tcl 返回里必然看到这段 hint,不需要主动查 quirks。
#
#  每条 hint = (触发函数 callable(output, command) -> bool, hint 文本, error_only)
#  对**所有**输出评估,不只 is_error:quirks §10 的 open_wave 误报场景真机 rc=0,
#  只在 is_error 时追加会让该 hint 在目标场景永不触发。触发函数自己基于
#  output/command 内容判断;error_only=True 的条目语义依赖"命令已失败",
#  仍只在 is_error 时评估(避免成功跑完 launch_runs 也被追加失败兜底指引)。
#  同一次返回可触发多条 hint(按 _QUIRK_HINTS 顺序追加,各加一段)。
# --------------------------------------------------------------------------- #


# 0.3.14:run/sim 失败时引导去 get_critical_warnings 兜底诊断
_HINT_RUN_FAILURE = (
    "\n\n提示: Tcl 命令失败时,综合/实现真错可能不在 Vivado messageDb"
    "(如中文路径触发的 TclStackFree),仿真真错在 <proj>.sim/<sim_fs>/*/xsim/*.log。"
    "调 get_critical_warnings(run_name='synth_1' / 'impl_1' / 'sim_1') 兜底诊断。"
)


# 0.3.20 A1:open_wave_database 报这条 err 多数情况是误报,wdb 实际已加载
_HINT_OPEN_WAVE_SPURIOUS = (
    "\n\n⚠ 已知 XSim 误报模式:'open_wave_config failed' 这条 err 多数情况下"
    "**不影响 wdb 实际加载**(裸 Vivado 无 sim project 时常发)。立即跑下面三条 verify:"
    "\n  run_tcl \"current_sim\"          → 应返回 simulation_N(非空)"
    "\n  run_tcl \"current_wave_config\"  → 应返回 .wcfg 名"
    "\n  run_tcl \"get_scopes /*\"        → 应列出 hierarchy"
    "\n三条都有效值 = wdb 已加载,忽略上面 err;否则才是真失败。"
)


# 0.3.20 A2:wave 操作失败可能留下孤儿 sim handle + GUI tab,重试前必须清理
_HINT_WAVE_STATE_CLEANUP = (
    "\n\n⚠ wave 操作失败可能在 GUI 留下孤儿 sim handle + tab(重试 3 次 → 3 个孤儿)。"
    "重试前先清理:"
    "\n  run_tcl \"while {[catch {current_sim} __c] == 0 && \\$__c ne {}} "
    "{ close_sim -force }; catch {close_wave_config}\""
    "\n清理后仍失败 → stop_session → start_session 重启 vivado 进程"
    "(close_sim 不关 GUI tab,残留 tab 无 Tcl 可清,只能重启进程)。"
)


def _looks_like_run_failure(output: str, command: str) -> bool:
    """粗判输出是否暗示 run / 仿真失败 → 触发 _HINT_RUN_FAILURE。

    保守判断:命中其一即认为该展示 hint。避免对纯查询的 ERROR(如 get_property
    on nonexistent)误展示 —— 但即使误展示也只是多一行文字,不破坏功能。
    """
    if not output:
        return False
    haystack = command + "\n" + output
    markers = (
        "failed due to earlier errors",
        "launch_simulation",
        "launch_runs",
        "synth_design",
        "impl_design",
        "place_design",
        "route_design",
        "write_bitstream",
    )
    return any(m in haystack for m in markers)


def _looks_like_open_wave_spurious(output: str, command: str) -> bool:
    """A1:open_wave_config failed due to earlier errors → 误报 verify hint。"""
    return "'open_wave_config' failed due to earlier errors" in output


def _looks_like_wave_failure_needs_cleanup(output: str, command: str) -> bool:
    """A2:任何 open_wave_database / wave 类操作失败都建议清理。

    触发条件:命令含 open_wave_database / add_wave / log_wave / close_sim 等
    wave/sim 相关,且输出含 fail / error 关键词。
    """
    cmd_markers = (
        "open_wave_database",
        "open_wave_config",
        "add_wave",
        "log_wave",
        "current_wave_config",
    )
    if not any(m in command for m in cmd_markers):
        return False
    err_markers = (
        "'open_wave_config' failed",
        "failed due to earlier errors",
        "ERROR:",
    )
    return any(m in output for m in err_markers)


# B1:-scripts_only 每次都重生仿真脚本,用户手动 append 的内容会被擦
_HINT_SCRIPTS_ONLY_REGEN = (
    "\n\n提示: tb_*.tcl / xsim_*.tcl 每次 -scripts_only 都会被 Vivado 重生,"
    "你手动 append 的 'quit 0' 会被擦。"
    "CI 跑请用 xsim -tclbatch <script.tcl> + 脚本里显式 quit 0"
)


def _looks_like_scripts_only_regen(output: str, command: str) -> bool:
    """B1:launch_simulation -scripts_only → 脚本会被重生(成功也提示)。"""
    return "launch_simulation" in command and "-scripts_only" in command


# B2:GUI 操作保存的 wcfg 混入中文 BOM,xsim 持续刷解析告警(rc 多为 0)
_HINT_WCFG_BOM_CORRUPT = (
    "\n\n提示: <proj>.sim/*/sim_*.wcfg 内部混了中文 BOM(GUI 操作时保存的损坏文件)。"
    "处理: 删除该 .wcfg 或改名 *.wcfg.bak,xsim 会自动重生干净版本。"
    "不致命,但持续干扰 log。"
)


def _looks_like_wcfg_bom_corrupt(output: str, command: str) -> bool:
    """B2:输出含 wcfg 解析损坏特征 → 删除/改名后 xsim 自动重生。

    'invalid byte' 是通用编码错误措辞(读任意损坏/二进制文件都可能出现),
    单独命中会把无关报错误导向删 .wcfg —— 须输出同时含 '.wcfg' 才触发。
    """
    if "[Wavedata 42-472]" in output or "WCFG parsing ERROR" in output:
        return True
    return "invalid byte" in output and ".wcfg" in output


# B3:open_project 路径错,Vivado 不做 fuzzy match,引导 glob 列候选。
# 注意 Tcl glob 没有 bash globstar 语义(** 等价单层 *),递归要逐层显式列
# pattern(glob 接受多 pattern,一条命令覆盖 1-3 层)。
_HINT_PROJECT_NOT_FOUND = (
    "\n\n提示: 路径错了。用 'glob -nocomplain <搜索根>/*.xpr <搜索根>/*/*.xpr"
    " <搜索根>/*/*/*.xpr' 列出候选(一条命令覆盖 1-3 层;Tcl glob 不支持 **"
    " 递归),或先 'pwd' 确认 cwd。Vivado 不做 fuzzy match。"
)


def _looks_like_project_not_found(output: str, command: str) -> bool:
    """B3:[Coretcl 2-27] Can't find specified project → 路径错。"""
    return (
        "[Coretcl 2-27]" in output
        and "Can't find specified project" in output
    )


# 0.3.24(issue #2):[Common 17-180] Spawn failed = Vivado 自身 spawn 子进程失败
# (compile.bat 起不来/秒死),真错不落在任何日志里,W-hint 层是唯一拦截点。
_HINT_SPAWN_FAILED = (
    "\n\n⚠ 'Spawn failed' 是 Vivado **自身 spawn 子进程失败**(如 compile.bat 起不来"
    "或刚启动就被终止),不是 HDL 编译错误,compile.log 多半没生成。常见根因按概率:"
    "\n  1. 杀软/安全软件拦截 .bat / xvlog 子进程('Broken pipe' 变体的已知公开案例根因)"
    "→ 查 Windows 安全中心保护历史 / 360 / 火绒拦截记录,把 Vivado 安装目录与工程目录加白"
    "\n  2. 工程在 Desktop / OneDrive 同步目录 → 移到 C:/vivado_proj/ 之类短 ASCII 非同步路径"
    "\n  3. NoDefaultCurrentDirectoryInExePath 策略('No such file or directory' 变体):"
    "Win10 及更早该变量**存在即生效**,用 reg delete \"HKCU\\Environment\" /v"
    " NoDefaultCurrentDirectoryInExePath /f 删除;Win11 24H2+ 默认开启,需 reg add"
    " \"HKCU\\Environment\" /v NoDefaultCurrentDirectoryInExePath /d 0 /f。改完注销重登"
    "\n下一步: 调 get_critical_warnings(run_name='sim_1') 兜底诊断 —— 日志全缺/全空时"
    "自动走 scripts-only fallback,在 Vivado session 内用完整路径复刻 compile/elaborate,"
    "既是绕过也是判别实验(fallback 能过 = wrapper/策略问题;fallback 也 spawn 失败 = "
    "杀软拦一切子进程)。"
)


def _looks_like_spawn_failed(output: str, command: str) -> bool:
    """[Common 17-180] Spawn failed:子进程起不来/秒死,非编译错误。

    error_only=False:AI 用 catch {launch_simulation} 包裹时 rc=0,
    但输出里的 ERROR 行仍在,hint 不能因 rc=0 静默。
    裸 'Spawn failed' 子串太泛(用户 grep 任意日志也可能含),须伴随
    Vivado 消息 ID 或仿真上下文才触发。
    """
    if "Spawn failed" not in output:
        return False
    haystack = command + "\n" + output
    return (
        "[Common 17-180]" in output
        or "[USF-XSim" in output
        or "launch_simulation" in haystack
    )


# 超时 hint:_safe_execute except 分支命中超时类异常时追加(超时≠命令失败)
_HINT_TIMEOUT = (
    "\n\n提示: 超时≠命令失败:Vivado 仍在执行,本 session 在该命令完成前不可用。"
    "同步长命令(synth_design/route_design/launch_simulation)请显式传大 timeout"
    "(如 3600);勿重发命令;工程模式长任务请改用 run_synthesis/run_implementation"
    "(Python 轮询不阻塞)。"
)


# (触发函数, hint 文本, error_only)。按顺序匹配,命中的都追加。
# error_only=True:hint 语义依赖"命令已失败"(rc!=0),成功输出不评估;
# error_only=False:对所有输出评估(quirks §10 的误报场景 rc=0 也要触发)。
_QUIRK_HINTS: tuple[tuple, ...] = (
    (_looks_like_run_failure, _HINT_RUN_FAILURE, True),
    (_looks_like_open_wave_spurious, _HINT_OPEN_WAVE_SPURIOUS, False),
    (_looks_like_wave_failure_needs_cleanup, _HINT_WAVE_STATE_CLEANUP, False),
    (_looks_like_scripts_only_regen, _HINT_SCRIPTS_ONLY_REGEN, False),
    (_looks_like_wcfg_bom_corrupt, _HINT_WCFG_BOM_CORRUPT, False),
    (_looks_like_project_not_found, _HINT_PROJECT_NOT_FOUND, False),
    (_looks_like_spawn_failed, _HINT_SPAWN_FAILED, False),
)


def _append_quirk_hints(summary: str, command: str, is_error: bool = False) -> str:
    """根据返回输出 + 原命令匹配关键词,追加已知 quirk hint。

    对**所有**输出调用(不只 is_error):open_wave 误报等场景真机 rc=0,
    触发函数自己基于 output/command 内容判断。error_only 条目(如 run 失败
    兜底诊断)仍只在 is_error=True 时评估。所有 hint 在原 summary 末尾按
    _QUIRK_HINTS 顺序追加,一条命中追加一段,多条命中追加多段。
    """
    additions = []
    # A1/A2 分流:rc=0 的 open_wave 误报场景(A1 命中且命令实际成功)下,
    # A2 的 close_sim -force 清理会把刚加载成功的 sim 关掉,恰好制造 A1 想
    # 避免的破坏 —— 该场景跳过 A2;真失败(rc!=0)时 A2 照常触发。
    skip_cleanup = not is_error and _looks_like_open_wave_spurious(summary, command)
    for trigger, hint, error_only in _QUIRK_HINTS:
        if error_only and not is_error:
            continue
        if skip_cleanup and hint is _HINT_WAVE_STATE_CLEANUP:
            continue
        try:
            if trigger(summary, command):
                additions.append(hint)
        except Exception as e:
            # hint 触发函数有 bug 时降级:打 warn 不让本次调用失败
            logger.warning("quirk hint 触发函数异常: %s", e)
    return summary + "".join(additions)


async def _safe_execute(
    session: VivadoSession,
    tcl: str,
    timeout: float,
    error_label: str,
) -> str:
    """安全执行 Tcl 命令，异常时返回错误字符串而非抛出。

    响应(成功与失败)在末尾追加已知 quirk hint(W 模式):AI 在 run_tcl 返回里
    必然看到 "这条 err 怎么办" 的固定指引,不需要主动查 quirks 文档。
    超时类异常额外追加 "超时≠命令失败" 指引(Vivado 仍在执行,勿重发)。
    """
    try:
        result = await session.execute(tcl, timeout=timeout)
        return _append_quirk_hints(result.summary, tcl, is_error=result.is_error)
    except Exception as e:
        msg = f"[ERROR] {error_label}: {e}"
        # 两类超时:asyncio.TimeoutError(3.10 与 builtin 不同类)/ builtin TimeoutError
        if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
            msg += _HINT_TIMEOUT
        return msg


# --------------------------------------------------------------------------- #
#  MCP Resources（会话状态查询）
#  注意：Resources 不支持 Context 注入，使用模块级 _manager_ref
# --------------------------------------------------------------------------- #

@mcp.resource("vivado://sessions")
async def resource_sessions() -> str:
    """所有 Vivado 会话的状态信息（JSON）。"""
    if _manager_ref is None:
        return json.dumps({"sessions": [], "message": "服务器未就绪"})
    # list_sessions 已 async 化(probe 并发跑,不阻塞 event loop),resource 同步跟进
    sessions = await _manager_ref.list_sessions()
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
        "10. **编程设备**: `program_device`\n"
        "11. **工程入库（git 版本控制）**: `run_tcl(\"write_project_tcl -force "
        "-no_copy_sources -paths_relative_to <repo_root> C:/proj/rebuild.tcl\")`\n"
        "    - .runs/.cache/.sim 全不入 git，重建用 `vivado -mode batch -source "
        "rebuild.tcl`\n"
        "    - 含 BD 的工程导出脚本会内联 BD 重建过程，但 wrapper 需重跑 "
        "make_wrapper\n"
        "    - IP 用户改动要确认 .xci 在 srcs 内、不在 .gen 内\n\n"
        "查询运行状态: `run_tcl(\"get_property STATUS [get_runs synth_1]\")`\n"
        "设计规则检查: `run_tcl(\"report_drc -return_string\")`\n"
        "遇到 CRITICAL WARNING: `get_critical_warnings` 提取分类 + 中文修复建议。"
    )


@mcp.prompt()
def debug_timing() -> str:
    """时序违例调试引导：2019.1 验证过的系统化报告升级链。"""
    return (
        "时序违例调试流程（2019.1 验证过的升级链，由浅入深）：\n\n"
        "1. **第一站**: `get_timing_report` — 结构化时序摘要，WNS < 0 即违例。\n"
        "   违例时已自动附带 Top10 违例路径 + 5 模式分类 + 具体修复 Tcl 命令，\n"
        "   无需再手动跑 report_timing\n"
        "2. **时钟交互矩阵**: `run_tcl(\"report_clock_interaction -return_string\")`\n"
        "   任何 CDC 嫌疑先看这张矩阵，重点关注 unsafe / partial 时钟对\n"
        "3. **CDC 结构检查**: `run_tcl(\"report_cdc -details -return_string\")`\n"
        "   定位缺同步器的跨时钟域路径（CDC-1 类结构问题）\n"
        "4. **方法学检查**: `run_tcl(\"report_methodology -return_string\")`\n"
        "   TIMING-6/7、XDC 约束类违例在这里暴露\n"
        "5. **QoR 体检**: `run_tcl(\"report_qor_assessment -return_string\")`\n"
        "   1-5 分整体评估（注意：report_qor_suggestions 需 2020.1+，2019.1 不可用）\n"
        "6. **资源与高扇出**: `get_utilization_report` 检查是否资源过度使用；\n"
        "   `run_tcl(\"report_high_fanout_nets -fanout_greater_than 200 "
        "-return_string\")`\n"
        "7. **复杂违例深挖**: `run_tcl(\"report_design_analysis -congestion "
        "-return_string\")` 分析布线拥塞（关键路径物理特征用 -timing）\n\n"
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
        "2. **验证 IO 布局**: `verify_io_placement_tool` 对比 XDC 约束与实际分配\n"
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
        "2. `verify_io_placement_tool` — 验证 XDC 约束与实际布局\n"
        "3. `get_io_report` — 确认所有 GT 端口的 Bank 和 Site\n\n"
        "## 第二层：时钟与复位\n"
        "4. `run_tcl(\"report_clock_interaction -return_string\")` — "
        "检查参考时钟 (REFCLK) 与时钟交互\n"
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
import vivado_mcp.tools.introspect_tools  # noqa: E402, F401
import vivado_mcp.tools.ip_tools  # noqa: E402, F401
import vivado_mcp.tools.report_tools  # noqa: E402, F401
import vivado_mcp.tools.session_tools  # noqa: E402, F401
import vivado_mcp.tools.tcl_tools  # noqa: E402, F401
import vivado_mcp.tools.wave_tools  # noqa: E402, F401
