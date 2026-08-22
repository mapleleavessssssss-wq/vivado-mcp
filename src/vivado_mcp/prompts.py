# Prompt 正文的段落需要保持为可读、可复制的完整语义单元。
# ruff: noqa: E501
"""面向 AI 客户端的紧凑 FPGA 工作流 Prompt。

公共规则由 :func:`_workflow_prompt` 统一生成，领域 Prompt 只描述各自的证据、
分类、动作和通过条件。这样既保持每个 Prompt 可独立使用，又避免复制安全规则。
"""

from collections.abc import Callable, Sequence
from typing import Protocol


class _PromptDecorator(Protocol):
    """FastMCP ``prompt()`` 返回的最小装饰器协议。"""

    def __call__(self, function: Callable[[], str]) -> Callable[[], str]: ...


class _PromptRegistrar(Protocol):
    """避免 Prompt 内容模块反向依赖全局 MCP server 实例。"""

    def prompt(self) -> _PromptDecorator: ...

_COMMON_SAFETY = """## 闭环与安全栏
1. **Fresh evidence**：只使用本轮从当前 session/工程取得的报告作为基线；旧日志、截图和经验只能作为线索，不能代替测量。
2. **最小改动**：按证据分类，一次只改一类问题，记录改动及理由，再用与基线相同的命令复测。未经用户确认，不做 RTL 架构、板级引脚或硬件连接的实质变更。
3. **禁止假绿**：不得通过删除或降级约束、`set_false_path`、multicycle、waiver、关闭 DRC、删除 assertion，或缩短测试到未覆盖目标场景来制造通过。只有功能意图和独立证据证明例外成立时，才提出约束例外并交由用户决定。
4. **证据门禁**：命令返回成功不等于任务通过；必须满足本 Prompt 的通过条件。报告过期、阶段错误、对象为空或结果无法归属当前工程时，结论一律为未验证。
5. **停止条件**：达到通过条件即停止；缺前置输入、需要高风险决定、发现相邻领域根因，或连续两轮改动没有净改善时也停止，不继续盲试。停止时保留当前 session，不擅自操作其他 session/job/process。

## 固定输出
按以下字段交付，不省略失败项：`范围与前置条件`、`新鲜基线`、`问题分类与证据`、`已做变更`、`同指标复测`、`最终状态(PASS/FAIL/BLOCKED)`、`未决风险`、`建议下一步`。每项附所用工具或 Tcl、设计阶段和关键数值；没有证据时明确写“未验证”，不得推测为 PASS。"""


def _bullets(items: Sequence[str]) -> str:
    """把领域步骤渲染成稳定的有序列表。"""
    return "\n".join(f"{index}. {item}" for index, item in enumerate(items, 1))


def _workflow_prompt(
    *,
    title: str,
    applies: str,
    not_for: str,
    tools: Sequence[str],
    prerequisites: Sequence[str],
    steps: Sequence[str],
    pass_condition: str,
    domain_safety: str,
) -> str:
    """生成一个包含完整证据闭环的独立 Prompt。"""
    tool_list = "、".join(f"`{name}`" for name in tools)
    return f"""# {title}

**适用**：{applies}
**不适用**：{not_for}
**可用入口**：{tool_list}。简单 Vivado 操作通过 `run_tcl`；含用户输入的 Tcl 参数优先用 `safe_tcl` 占位传参，不发明新的 facade 工具。

## 前置条件
{_bullets(prerequisites)}
任何前置条件缺失都返回 BLOCKED；先列明缺失项，不在半配置状态上继续。

## 证据闭环
{_bullets(steps)}

## 通过条件
{pass_condition}

## 领域门禁
{domain_safety}

{_COMMON_SAFETY}
"""


def fpga_workflow() -> str:
    """标准 FPGA 开发流程：从工程建立到可审计的 bitstream。"""
    return _workflow_prompt(
        title="FPGA 全流程",
        applies="创建或接管 Vivado 工程，并完成综合、实现、signoff、bitstream 与可选下载。",
        not_for="单独的时序、CDC、仿真、GT/IP/PCIe 或 ILA 深度诊断；进入对应 Prompt。",
        tools=(
            "start_session", "list_sessions", "get_project_info", "safe_tcl", "run_tcl",
            "get_compile_profile", "configure_incremental_compile", "run_synthesis",
            "run_implementation", "get_run_progress", "get_timing_report",
            "get_utilization_report", "get_critical_warnings", "check_bitstream_readiness",
            "generate_bitstream", "program_device",
        ),
        prerequisites=(
            "用 `list_sessions` 确认目标 session；没有会话才调用 `start_session`，并明确 GUI、Tcl 或 attach 模式。全流程始终携带同一 session_id。",
            "用 `get_project_info` 读取当前工程、器件、顶层、sources/XDC/simulation sources 和 run 状态。工程不存在时，用 `safe_tcl` 执行带参数的 `create_project`、`add_files`，再用 `run_tcl` 设置 top；不得猜器件、板卡或路径。",
            "确认用户给出的目标频率、器件/板卡、顶层、产物目录和是否允许编程硬件；缺任一关键输入先停止询问。",
        ),
        steps=(
            "Baseline：再次调用 `get_project_info` 与 `get_compile_profile`，一次记录 run freshness、策略、线程、已有报告/checkpoint 和 timing stats。先解决缺源文件、错误 top、失效 IP 和约束语法问题，不把所有报告串成固定检查。",
            "Synthesis：只有 run 为 `Not started` 才调用 `run_synthesis`；默认异步返回 job id，以 `get_run_progress` 低频确认完成。完成后优先复用生成的 utilization/timing 报告，仅作早期指标。过期或失败 run 不自动 reset。",
            "Implementation：综合门禁通过后调用 `run_implementation`，同样默认异步并低频查询。增量模式先用 `configure_incremental_compile(apply=False)` 评估，只有工程身份和复用条件明确时才单独审批 apply。",
            "Signoff：在 post-route run 上读取已有 `get_timing_report`、`get_critical_warnings` 和 DRC。`report_methodology` 只在首次综合或重要模块/XDC/clock 变化后运行；QoR Suggestions 仅在 fully routed 且 baseline strategy 适用时使用。post-synth WNS 不能替代 post-route 结论。",
            "Bitstream：先调用 `check_bitstream_readiness`；只有 readiness 与 signoff 证据一致时才调用 `generate_bitstream`。用 `parse_bit_header` 可核对产物头；只有用户明确要求且目标设备已确认时才 `program_device`。",
            "Reproducibility：需要入库时，用 `run_tcl` 执行 `write_project_tcl -force -no_copy_sources -paths_relative_to ...`；报告哪些 XCI、BD wrapper 和外部文件仍需纳入重建验证。",
        ),
        pass_condition="综合与实现 run 均完成；post-route timing、route/DRC/methodology 无阻断项；`check_bitstream_readiness` 通过；生成的 bitstream 与当前工程/器件对应。设备编程是可选的独立结果，不影响 bitstream 构建 PASS。",
        domain_safety="不得跳过失败阶段直接生成 bitstream，不得把自动打开旧 run 后得到的报告当作当前构建证据。`program_device` 会改变外部硬件状态，必须确认目标和 bitstream 后执行。",
    )


def debug_timing() -> str:
    """时序违例调试：从新鲜摘要到受控的根因修复。"""
    return _workflow_prompt(
        title="时序收敛调试",
        applies="已完成综合或实现，存在 setup/hold 违例、WNS/TNS 退化或时钟关系异常。",
        not_for="RTL 功能失败、未建立时钟的 CDC 审计或尚未完成对应 run 的工程。",
        tools=("get_project_info", "get_compile_profile", "get_timing_report", "get_utilization_report", "get_critical_warnings", "run_tcl"),
        prerequisites=(
            "用 `get_project_info` 确认工程、器件和当前 synth/impl 状态；明确分析 post-synth 还是 post-route，并优先使用 post-route signoff 证据。",
            "确认目标时钟、预期频率和约束来源。没有时钟定义或 generated clock 关系不清时，先停止并补功能意图。",
            "保证所分析 run 与当前源码/XDC 同步；run 过期或未完成时先重跑相应阶段。",
        ),
        steps=(
            "Baseline：先调用 `get_compile_profile` 确认 run 完成且未过期，再用 `get_timing_report(source=\"auto\")` 复用已有摘要；只有 FAIL 且要定位根因时才展开 violating paths。资源或告警能改变决策时再分别读取。",
            "Classify：按路径证据区分逻辑深度/长组合链、高扇出、拥塞、跨时钟、时钟定义错误、IO 约束或 hold。用 `run_tcl(\"report_clock_interaction -return_string\")` 和 `run_tcl(\"report_cdc -details -return_string\")` 验证 CDC 嫌疑。",
            "Escalate：必要时用 `run_tcl(\"report_methodology -return_string\")`、`run_tcl(\"report_high_fanout_nets -fanout_greater_than 200 -return_string\")` 和 `run_tcl(\"report_design_analysis -congestion -return_string\")` 补证据；命令不兼容当前 Vivado 时记录降级，不伪造结果。",
            "Fix：优先修复缺失/错误时钟与功能明确的 RTL 根因，再考虑流水线、复制驱动、布局或策略。每次只改一种原因，记录目标路径组和预期改善量。",
            "Re-measure：重跑受影响的 synth/impl，在同一设计阶段再次调用 `get_timing_report`；对比 WNS/TNS、违例数量及原 Top 路径是否真实消失，同时检查是否新增 hold 或 methodology 问题。",
        ),
        pass_condition="目标 signoff 阶段 setup 与 hold 均满足要求，约束覆盖完整，原违例未被隐藏，且 DRC/methodology 没有新的阻断项。只改善 WNS 但仍为负数属于 FAIL。",
        domain_safety="`set_false_path` 和 multicycle 不是性能优化手段。只有接口协议、时钟关系和端到端周期预算共同证明路径无需默认分析时，才能作为待用户审批的约束变更提出。",
    )


def debug_gt_mapping() -> str:
    """GT 高速收发器引脚与 lane 映射诊断。"""
    return _workflow_prompt(
        title="GT 引脚与 Lane 映射调试",
        applies="GTX/GTH/GTY 或 PCIe 链路无法建立，并怀疑 XDC、GT LOC、Bank/Site 或 lane 映射。",
        not_for="没有 PCB 原理图/板卡约束证据的任意改脚，或纯协议层、驱动层问题。",
        tools=("get_project_info", "get_critical_warnings", "verify_io_placement_tool", "get_io_report", "inspect_ip_params", "get_timing_report", "run_tcl"),
        prerequisites=(
            "用 `get_project_info` 确认器件、顶层和实现状态，收集可信 PCB 原理图、板卡 master XDC、连接器/lane 编号与收发方向。没有板级真值来源即 BLOCKED。",
            "确认目标 IP 实例、器件系列和 Vivado/IP 版本；7-Series `pcie_7x` 与 UltraScale+ GT Wizard 的 LOC 生成规则不同，不跨架构套参数。",
            "确保实现 run 对应当前 XDC/IP；未完成实现时只能做配置预检，不能声称实际 placement 已验证。",
        ),
        steps=(
            "Baseline：调用 `get_critical_warnings` 捕获 Vivado 12-1411 等冲突，再调用 `verify_io_placement_tool` 和 `get_io_report` 记录每个 rxp/rxn/txp/txn 的 package pin、Bank、Site 与差分极性。",
            "IP evidence：用 `inspect_ip_params` 分别按 `gt`、`lane`、`loc` 过滤目标实例，记录 lane width、lane reversal、refclk 与内部 GT location。参数为空时先核实实例名，不推断默认值。",
            "Classify：逐 lane 对照 PCB 真值，区分 XDC PACKAGE_PIN 顺序、IP 内部 LOC、lane reversal、器件/板卡型号不匹配和仅名称差异。形成“逻辑 lane → IP GT site → package pin → PCB lane”映射表。",
            "Fix：只在板级证据和 IP 生成规则一致时，提出删除冲突的重复 GT PACKAGE_PIN、修正 lane 顺序或 IP 参数。7-Series 不把 `disable_gt_loc` 当作有效修复；任何 XDC 修改先展示 diff 与影响。",
            "Re-measure：重新生成受影响 IP output products 并实现；再次运行 `verify_io_placement_tool`、`get_io_report`、`get_critical_warnings`、`run_tcl(\"report_drc -return_string\")` 和 `get_timing_report`，对比同一映射表。",
        ),
        pass_condition="每条 GT lane 的差分极性、Bank/Site、IP LOC 与 PCB 真值一致；无 GT pin/placement 阻断告警；实现 DRC 与相关时序通过。链路是否训练成功作为后续硬件证据单独报告。",
        domain_safety="禁止仅凭“链路不通”交换 lane 或删除约束。不得把普通 GPIO warning 与 GT 冲突混为一谈；没有原理图或 master XDC 时只输出待核实映射，不改工程。",
    )


def debug_ip_config() -> str:
    """Vivado IP 配置差异、版本与 output products 调试。"""
    return _workflow_prompt(
        title="IP 配置调试",
        applies="怀疑 XCI 参数、IP 版本、器件迁移或生成产物导致综合、实现或硬件行为异常。",
        not_for="没有明确目标 IP 的全工程盲比，或仅靠相似文件名认定 golden 配置。",
        tools=("get_project_info", "get_ip_status", "inspect_ip_params", "compare_xci", "get_critical_warnings", "get_timing_report", "run_tcl"),
        prerequisites=(
            "用 `get_project_info` 和 `get_ip_status` 确认目标 IP 实例、VLNV、锁定/升级状态、器件、Vivado 版本和 output products 状态。",
            "若使用 golden XCI，说明其来源、通过过的板卡/器件、Vivado/IP 版本与 commit；无法证明来源时只把差异当线索。",
            "备份或版本控制待改 XCI，确认用户允许 regenerate/upgrade；升级 IP 可能产生大范围不可逆 diff，未授权时停止。",
        ),
        steps=(
            "Baseline：调用 `inspect_ip_params` 获取目标实例当前 CONFIG 属性；按症状用 `filter_keyword` 收窄。用 `get_ip_status` 保存 locked、upgrade 和生成状态。",
            "Offline compare：有可信 golden 时调用 `compare_xci`，按功能参数、器件/版本元数据、自动生成字段分类差异；不同器件或 IP 版本的默认值变化不能直接判错。",
            "Classify：结合首个综合/实现错误和 `get_critical_warnings`，区分真实功能参数错误、版本迁移、目标器件不匹配、缺 output products、wrapper/compile order 过期及无关元数据漂移。",
            "Fix：只改有证据关联症状的最小参数集。需要 Tcl 设置属性时用 `safe_tcl` 传实例/值；先展示旧值、新值、依据和预期影响，IP upgrade 交由用户确认。",
            "Regenerate：通过 `run_tcl` 对目标 IP 执行 `generate_target all`，必要时更新 compile order；不得删除整个缓存或批量升级其他 IP 来掩盖单实例问题。",
            "Re-measure：重新综合/实现受影响范围，再取 `get_ip_status`、`get_critical_warnings` 与 `get_timing_report`；硬件相关问题还需对应链路或 ILA 证据，生成成功本身不是功能 PASS。",
        ),
        pass_condition="目标 IP 状态 current/unlocked，所需 output products 与 wrapper 可重建，相关 run 通过且原错误消失；功能参数与可信规格一致，无新增时序或 DRC 阻断项。",
        domain_safety="不把版本不同的 XCI 做逐字段机械覆盖，不直接编辑生成目录中的副本。`disable_gt_loc` 等参数必须先确认器件系列和子 IP 是否实际接收。",
    )


def debug_pcie() -> str:
    """PCIe 从物理层到协议观测的分层调试。"""
    return _workflow_prompt(
        title="PCIe 分层调试",
        applies="PCIe link down、速率/宽度降级、训练不稳定或枚举失败，需要定位 FPGA 侧原因。",
        not_for="没有板级规格和复位/参考时钟信息的猜测式调参，或主机驱动的独立软件诊断。",
        tools=("get_project_info", "get_critical_warnings", "verify_io_placement_tool", "get_io_report", "inspect_ip_params", "get_timing_report", "parse_ltx", "run_tcl"),
        prerequisites=(
            "记录器件/板卡、PCIe IP 实例、目标 generation/lane width、REFCLK 频率、PERST# 极性、主机与插槽；用 `get_project_info` 确认当前实现。",
            "准备 PCB lane/REFCLK/PERST# 真值和当前 bitstream 标识。硬件现象必须注明冷启动/热复位、稳定复现次数及主机侧观测。",
            "一次只调试一个层级；上一层没有证据通过时，不跳到 LTSSM 或驱动层归因。",
        ),
        steps=(
            "Physical baseline：运行 `get_critical_warnings`、`verify_io_placement_tool`、`get_io_report`，核对 GT lane、差分极性、Bank/Site 与 REFCLK。发现冲突时转入 `debug_gt_mapping`。",
            "IP baseline：用 `inspect_ip_params` 检查 generation、lane width、refclk、lane reversal、device ID 与 GT location；将配置与板卡和主机能力分开记录。",
            "Clock/reset：用 `run_tcl(\"report_clock_interaction -return_string\")` 和约束报告验证参考/用户时钟关系；核实 PERST# IOSTANDARD、极性、去断言时序和同步结构。静态报告不能证明板上时钟实际存在。",
            "Timing gate：调用 `get_timing_report`，确认 post-route setup/hold 与 PCIe user clocks 收敛；不以综合阶段或全局摘要掩盖相关路径违例。",
            "Protocol evidence：物理、时钟复位及时序均通过后，才从已有 ILA/LTX 或用户提供寄存器读数观察 LTSSM、link_up、speed/width。可先用 `parse_ltx` 核对探针清单；需要新增探针时转入 `ila_hardware_debug`。",
            "Re-measure：每个最小修复后重新生成实现/bitstream，明确 bit/ltx 与 commit 配对，再以相同上电流程重复观测；区分 FPGA 证据、主机枚举和驱动结果。",
        ),
        pass_condition="物理映射、REFCLK/PERST#、post-route timing 均有证据通过；板上 LTSSM 到 L0，协商 speed/width 达到双方共同能力且重复复位稳定。仅 Vivado 实现通过不能判 PCIe PASS。",
        domain_safety="不宣称固定比例的 PCIe 故障来自某一层；按层级证据排查。禁止在未核对 PCB 时交换 lane，也不通过放宽时序或复位要求制造偶发 link-up。",
    )


def simulation_bringup() -> str:
    """RTL 仿真从编译、展开到有限运行和判定的工作流。"""
    return _workflow_prompt(
        title="仿真 Bring-up",
        applies="建立或修复 Vivado/XSim behavioral simulation，定位 compile、elaborate、runtime 或 testbench 失败。",
        not_for="用仿真结果替代 post-route timing/CDC signoff，或没有 testbench 预期行为的开放式跑波形。",
        tools=("get_project_info", "verilog_compile_check", "run_tcl", "set_wave_zoom", "set_wave_analog"),
        prerequisites=(
            "用 `get_project_info` 确认 simulation sources、sim top、fileset、语言/define/include 与目标 simulator；没有 testbench 或预期结束条件即 BLOCKED。",
            "明确测试目标、时钟/复位、输入激励、最大仿真时间、PASS/FAIL assertion 或 scoreboard，以及允许的 warning。",
            "确认当前 session 没有正在使用的同名仿真 job；所有启动、运行和停止命令只作用于选定 session。",
        ),
        steps=(
            "Static baseline：先用 `verilog_compile_check` 做快速语法/编译检查，但把结果标为预检；外部编译器通过不等于 Vivado 文件集、elaboration 或 simulation 通过。",
            "Vivado compile/elaborate：用 `run_tcl` 检查 sim fileset/top 和 compile order，再执行 `launch_simulation -simset sim_1 -mode behavioral`。按首个真实错误分类为 source/order/define、elaboration、模型/库或 testbench，不预设为 RTL bug。",
            "Finite run：成功展开后用 `run_tcl(\"run <有限时长>\")` 执行用户定义窗口；优先让 testbench 自报 assertion/scoreboard 和完成标志。禁止无界 `run all`，除非 testbench 有可信终止机制且用户同意。",
            "Classify runtime：区分 assertion、timeout/deadlock、X/Z 传播、复位未释放、时钟未振荡、DUT 功能和 testbench/模型错误。需要波形时先添加最小信号集，再用 `set_wave_zoom` 和 `set_wave_analog` 辅助查看，不用目测替代断言。",
            "Stop/recover：卡住时只对当前 session 调用 `run_tcl(\"close_sim -force\")`，保留日志并记录最后仿真时间；不得结束机器上的全部 XSim 进程或影响其他工程。",
            "Re-measure：一次只修一类根因，重新 compile/elaborate 并运行同一 stimulus、seed 与时长；比较 assertion 数、完成标志、关键状态和仿真时间。",
        ),
        pass_condition="Vivado compile 与 elaboration 成功；仿真到达预定完成条件；必须通过的 assertion/scoreboard 为零失败；无 timeout、fatal 或未解释 X/Z。只有 compile success 属于 FAIL/未完成。",
        domain_safety="不得删除 assertion、改变 seed/输入或缩短运行窗口来躲避失败。`close_sim -force` 仅针对当前 Vivado session；若无法证明停止范围，先请求用户处理。",
    )


def cdc_audit() -> str:
    """跨时钟域结构、约束和 waiver 的证据化审计。"""
    return _workflow_prompt(
        title="CDC 审计",
        applies="审查异步或相关时钟域之间的 crossing、同步结构、时钟约束和 CDC waiver。",
        not_for="把普通单时钟时序违例统一标成 CDC，或在缺少协议意图时自动添加 false path。",
        tools=("get_project_info", "get_timing_report", "get_critical_warnings", "run_tcl"),
        prerequisites=(
            "用 `get_project_info` 确认当前工程和已完成的 synth/impl run；获取时钟列表、generated clocks、时钟关系及 crossing 的功能协议。",
            "明确每个 crossing 类型：单 bit level/pulse、multi-bit bus、counter/pointer、reset 或 handshake；不知道源/目的域和数据稳定性要求时停止。",
            "保证 CDC 报告来自当前源码/XDC；约束变更后旧 run 的报告全部作废。",
        ),
        steps=(
            "Baseline：调用 `get_timing_report` 和 `get_critical_warnings`，再用 `run_tcl(\"report_clock_interaction -return_string\")`、`run_tcl(\"report_cdc -details -return_string\")` 获取新鲜 crossing 清单和严重级别。",
            "Inventory：按 source clock、destination clock、信号/总线、宽度、同步级数和报告 ID 建表；对象为空时检查时钟是否正确定义，不能把空报告判为无 CDC。",
            "Classify：单 bit level 检查多级同步器及 ASYNC_REG；pulse 检查脉冲宽度/握手；multi-bit 检查稳定握手、FIFO 或 Gray 编码；reset 检查异步断言同步释放。不要把每一位独立双触发器当作总线一致性方案。",
            "Constraints：用 `run_tcl(\"report_exceptions -return_string\")` 和 methodology 报告核对 clock groups、false path、max delay 与 waiver 的覆盖对象。每个例外必须映射到功能协议和结构证据。",
            "Fix：优先修同步结构和缺失/错误时钟定义；需要 waiver 或例外时，提交 crossing、理由、审阅者和失效条件，不自动写入 XDC。",
            "Re-measure：重跑对应阶段及同一组 CDC/clock interaction/timing 报告；比较严重 crossing 数、对象集合和新警告，确认问题是修复而非被约束隐藏。",
        ),
        pass_condition="所有 crossing 均有已知协议和适配结构；CDC 报告无未解释严重项；时钟关系完整；每个例外/waiver 有可审计证据且未遮蔽真实同步问题。空报告只有在时钟与对象覆盖已验证后才可 PASS。",
        domain_safety="CDC waiver、asynchronous clock group 和 false path 都可能隐藏亚稳态或数据一致性风险。没有结构与协议证据时只提出调查项，不生成约束。",
    )


def ila_hardware_debug() -> str:
    """ILA 探针规划、重新实现、bit/ltx 配对和板上采集工作流。"""
    return _workflow_prompt(
        title="ILA 硬件调试",
        applies="需要在已实现 FPGA 上观察内部状态，规划/核对 ILA probes，并保证 bitstream 与 LTX 匹配。",
        not_for="没有可复现触发条件的无限抓取，或试图用 ILA 替代仿真、CDC 和 timing signoff。",
        tools=("get_project_info", "get_utilization_report", "get_timing_report", "check_bitstream_readiness", "generate_bitstream", "parse_bit_header", "parse_ltx", "program_device", "run_tcl"),
        prerequisites=(
            "明确硬件症状、触发条件、要证明/排除的假设、目标信号、所需前后触发窗口、采样时钟及预期事件频率；没有可证伪假设即 BLOCKED。",
            "用 `get_project_info` 确认器件与实现状态，用 `get_utilization_report` 评估 BRAM/LUT/时钟余量；确认新增 debug core 可能改变布局和 timing。",
            "确认当前设备/JTAG target、板卡供电和是否允许重新编程。已有产物先用 `parse_bit_header`、`parse_ltx` 记录器件、probe 清单与来源 commit。",
        ),
        steps=(
            "Probe plan：选择能区分假设的最小信号集，记录宽度、时钟域、触发表达式、depth 和采样时钟。跨域信号应在各自时钟域观察，不让 ILA 采样掩盖 CDC。",
            "Build impact：通过 `run_tcl` 检查/创建 debug core 和连接，仅按已批准计划修改；重新综合/实现后调用 `get_utilization_report` 与 `get_timing_report`，确认 ILA 未造成新的拥塞或违例。",
            "Artifact gate：调用 `check_bitstream_readiness` 后才 `generate_bitstream`。分别用 `parse_bit_header` 和 `parse_ltx` 核对器件、时间/来源及 probe，固定记录 bit/ltx/commit 三元组；任何一项不匹配即 BLOCKED。",
            "Program gate：列出目标 hardware server/device 与 DNA/part 等可核对信息；用户明确确认后才调用 `program_device`。不得仅按列表第一个 target 编程。",
            "Capture：通过 `run_tcl` 使用 Vivado hardware manager 命令选择明确的 device/ILA，配置触发、arm、等待和上传；每次采集记录触发配置、采样时钟、depth、bit/ltx 标识与环境条件。",
            "Re-measure：根据一次采集只更新一个假设或探针计划。若两轮采集均不能区分假设，停止并说明缺少的可观测性，而不是不断扩大 probe 集。",
        ),
        pass_condition="实现与 post-route timing 通过；bitstream、LTX、器件和 commit 可证明配对；触发条件可重复命中；采集证据能支持或否定明确假设。成功下载但没有有效采集不算 PASS。",
        domain_safety="新增 ILA 会改变实现，旧 timing 结论立即失效。编程设备属于外部状态变更，必须确认精确 target；不得操作其他 JTAG 链、session 或并行采集任务。",
    )


# 顺序是 MCP 对外兼容契约：旧 5 项保持原顺序，新工作流仅追加。
PROMPT_FUNCTIONS = (
    fpga_workflow,
    debug_timing,
    debug_gt_mapping,
    debug_ip_config,
    debug_pcie,
    simulation_bringup,
    cdc_audit,
    ila_hardware_debug,
)


def register_prompts(mcp: _PromptRegistrar) -> None:
    """按兼容顺序向 FastMCP 实例注册全部 Prompt。"""
    for prompt_function in PROMPT_FUNCTIONS:
        mcp.prompt()(prompt_function)
