# Prompt 正文的段落需要保持为可读、可复制的完整语义单元。
# ruff: noqa: E501
"""面向 AI 客户端的紧凑 FPGA 工作流 Prompt。

五个可分发工作流直接读取 Skill 正文，避免两份内容漂移。
其余领域 Prompt 通过 :func:`_workflow_prompt` 共享证据闭环与适用规则。
"""

from collections.abc import Callable, Sequence
from typing import Protocol

from vivado_mcp.workflows import read_workflow


class _PromptDecorator(Protocol):
    """FastMCP ``prompt()`` 返回的最小装饰器协议。"""

    def __call__(self, function: Callable[[], str]) -> Callable[[], str]: ...


class _PromptRegistrar(Protocol):
    """避免 Prompt 内容模块反向依赖全局 MCP server 实例。"""

    def prompt(self) -> _PromptDecorator: ...

_COMMON_SAFETY = """## 闭环与安全栏
1. **Fresh evidence**：现场结论使用当前 session/工程的新报告；离线分析记录输入文件来源、阶段和覆盖范围，不能把历史数据当作当前实现。截图和经验不能代替测量。
2. **最小改动**：按证据分类，一次只改一类问题，记录改动及理由，再以同条件复测。已授权范围内的常规修复继续完成；改变接口、目标频率、板级接线或编程硬件需相应明确授权。
3. **禁止假绿**：不得通过删除或降级约束、`set_false_path`、multicycle、waiver、关闭 DRC、删除 assertion，或缩短测试到未覆盖目标场景来制造通过。约束例外须有功能意图与独立证据；沿用已有授权，新增功能假设或扩大影响范围时才请求用户决定。
4. **证据门禁**：命令返回成功不等于任务通过；必须满足本 Prompt 的通过条件。报告过期、阶段错误、对象为空或结果无法归属当前工程时，结论一律为未验证。
5. **停止条件**：达到通过条件即停止；缺输入或需要范围外决定时仅暂停依赖步骤，继续独立分析。发现相邻领域根因时转入对应调查；连续两轮改动没有净改善时停止盲试并重新归因。保留当前 session，不擅自操作其他 session/job/process。

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
缺少必要输入时仅暂停依赖它的步骤，列明缺失项并继续已有证据支持的分析；不能据此判 PASS。

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
            "verilog_compile_check", "xdc_lint", "get_cdc_report", "run_synthesis", "run_implementation", "get_run_progress", "get_timing_report",
            "get_utilization_report", "get_critical_warnings", "check_bitstream_readiness",
            "generate_bitstream", "program_device",
        ),
        prerequisites=(
            "用 `list_sessions` 确认目标 session；没有会话才调用 `start_session`，并明确 GUI、Tcl 或 attach 模式。全流程始终携带同一 session_id。",
            "用 `get_project_info` 读取当前工程、器件、顶层、sources/XDC/simulation sources 和 run 状态。工程不存在时，用 `safe_tcl` 执行带参数的 `create_project`、`add_files`，再用 `run_tcl` 设置 top；不得猜器件、板卡或路径。",
            "确认目标频率、器件/板卡、顶层和产物目录；缺必要输入时仅暂停依赖步骤，继续可做检查。未请求下载时不编程硬件，继续已授权构建；请求下载时再核对精确设备及授权。",
        ),
        steps=(
            "Baseline：调用 `get_project_info`、`get_critical_warnings` 保存工程基线；已打开合适设计时再用 `run_tcl(\"report_drc -return_string\")`。新工程尚无设计时标记 DRC 未验证并继续预检。先解决缺源文件、错误 top、失效 IP 和约束语法问题。",
            "Preflight：用 `verilog_compile_check`、`xdc_lint` 检查源码与约束。有 testbench 且请求含功能验证时，经 simulation_bringup 完成有限自检仿真；记录 assertion/scoreboard 与完成标志。没有 testbench 时明确功能未验证，按已有授权继续构建；真实仿真失败不得被构建成功掩盖。",
            "Synthesis：调用 `run_synthesis`，以 `get_run_progress` 确认完成；失败则按首个根因分类，不把进程退出或日志存在当作成功。完成后记录 `get_utilization_report` 和综合阶段时序，仅作早期指标。",
            "Implementation：综合门禁通过后调用 `run_implementation`，同样用 `get_run_progress` 取得最终状态。每次变更后只重跑受影响阶段，禁止并发撞同一 Vivado session。",
            "Signoff：在 post-route 设计上重新获取 `get_timing_report`、`get_critical_warnings`、`run_tcl(\"report_drc -return_string\")` 和 `run_tcl(\"report_methodology -return_string\")`；跨域补 `get_cdc_report`、时钟关系及适用 bus-skew 证据。post-synth WNS 不能替代 post-route 结论。逐阶段记录 PASS/FAIL/未验证与报告来源，功能验证和构建分开交付。",
            "Bitstream：先调用 `check_bitstream_readiness`；只有 readiness 与 signoff 证据一致时才调用 `generate_bitstream`。用 `parse_bit_header` 可核对产物头；只有用户明确要求且目标设备已确认时才 `program_device`。",
            "Reproducibility：需要入库时，用 `run_tcl` 执行 `write_project_tcl -force -no_copy_sources -paths_relative_to ...`；报告哪些 XCI、BD wrapper 和外部文件仍需纳入重建验证。",
        ),
        pass_condition="综合与实现 run 均完成；post-route timing、route/DRC/methodology 无阻断项；`check_bitstream_readiness` 通过；生成的 bitstream 与当前工程/器件对应。设备编程是可选的独立结果，不影响 bitstream 构建 PASS。",
        domain_safety="不得跳过失败阶段直接生成 bitstream，不得把自动打开旧 run 后得到的报告当作当前构建证据。`program_device` 会改变外部硬件状态，必须确认目标和 bitstream 后执行。",
    )


def debug_timing() -> str:
    """时序收敛：与分发 Skill 共用完整正文。"""
    return read_workflow("vivado-timing-closure")


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
        tools=("get_project_info", "verilog_compile_check", "query_waveform", "safe_tcl", "run_tcl", "set_wave_zoom", "set_wave_analog"),
        prerequisites=(
            "已有 VCD 时直接离线 `query_waveform(file_path=...)` 列出信号，不要求会话；需要重跑时用 `get_project_info` 核对 sim top/fileset、语言/define/include 和目标 simulator。",
            "明确测试目标、时钟/复位、输入激励、最大仿真时间、PASS/FAIL assertion 或 scoreboard，以及允许的 warning。",
            "需要运行仿真时确认没有正在使用的同名 job；所有启动、运行和停止命令只作用于选定 session。离线 VCD 查询不要求会话。",
        ),
        steps=(
            "Static baseline：先用 `verilog_compile_check` 做快速语法/编译检查，但把结果标为预检；外部编译器通过不等于 Vivado 文件集、elaboration 或 simulation 通过。",
            "Vivado compile/elaborate：用 `run_tcl` 检查 sim fileset/top 和 compile order，再执行 `launch_simulation -simset sim_1 -mode behavioral`。按首个真实错误分类为 source/order/define、elaboration、模型/库或 testbench，不预设为 RTL bug。",
            "Finite run：成功展开后在目标 XSim 中依次 `open_vcd`、`log_vcd` 选择已核对信号、有限 `run`、`close_vcd`；路径和 scope 经 `safe_tcl` 参数传入。缺失过去的事件需要同激励重跑，不能从当前值补造历史；保留 assertion/scoreboard 和完成标志。",
            "Classify runtime：用 `query_waveform` 选择精确信号、start_time/end_time 和 max_events；时间为 VCD ticks，按 timescale 换算。通过 condition 的 equals/change/unknown/all_equals 查复位、X/Z 或握手；先读 initial_values，再读 events/matches。WDB/FST 不支持，截断或空 matches 均不能证明测试通过。",
            "Stop/recover：卡住时只对当前 session 调用 `run_tcl(\"close_sim -force\")`，保留日志并记录最后仿真时间；不得结束机器上的全部 XSim 进程或影响其他工程。",
            "Re-measure：一次只修一类根因，重新 compile/elaborate 并运行同一 stimulus、seed 与时长；比较 assertion 数、完成标志、关键状态和仿真时间。",
        ),
        pass_condition="Vivado compile/elaboration 成功；仿真到达预定完成条件；必要 assertion/scoreboard 零失败，无 timeout、fatal 或未解释 X/Z。查询的 simulation_verdict=not_evaluated 不提供测试判定；只有 compile success 属于 FAIL/未完成。",
        domain_safety="不得删除 assertion、改变 seed/输入或缩短运行窗口来躲避失败。`close_sim -force` 仅针对当前 Vivado session；若无法证明停止范围，先请求用户处理。",
    )


def cdc_audit() -> str:
    """CDC 结构与约束审查：与分发 Skill 共用完整正文。"""
    return read_workflow("vivado-cdc-audit")


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


def project_bringup() -> str:
    """接管或建立工程，按请求范围推进构建与产物检查。"""
    return read_workflow("vivado-project-bringup")


def waveform_debug() -> str:
    """离线 VCD 查询与需要时的有限 XSim 导出。"""
    return read_workflow("vivado-waveform-debug")


def constraints_authoring() -> str:
    """依据板级和接口参数建立 XDC，并验证约束覆盖。"""
    return read_workflow("vivado-constraints-authoring")


# 顺序是 MCP 对外兼容契约：旧八项保持原顺序，新工作流仅追加。
PROMPT_FUNCTIONS = (
    fpga_workflow,
    debug_timing,
    debug_gt_mapping,
    debug_ip_config,
    debug_pcie,
    simulation_bringup,
    cdc_audit,
    ila_hardware_debug,
    project_bringup,
    waveform_debug,
    constraints_authoring,
)


def register_prompts(mcp: _PromptRegistrar) -> None:
    """按兼容顺序向 FastMCP 实例注册全部 Prompt。"""
    for prompt_function in PROMPT_FUNCTIONS:
        mcp.prompt()(prompt_function)
