# vivado-mcp 实测审计报告

> 日期：2026-04-17
> 测试环境：Vivado 2019.1 / Windows 11 / xc7a35tcpg236-1 / 极简 counter 项目
> 测试脚本：`C:/Users/NJ/Desktop/vivado_mcp_test/run_full_test.py`
> 总步骤：22 步；未显式失败（但有多个"沉默出错"，下面详述）

---

## TL;DR — 两句话结论

1. **哨兵协议本身有 bug**：一旦某条 Tcl 命令失败，错误消息会"溢出"到下一条命令的输出里，导致后续所有结果错位。这是一切怪象的根。
2. **解析器有严重假阳性**：当 `report_timing_summary` 因"设计未打开"失败时，`get_timing_report` 居然返回 **"PASS 时序满足"**，会误导 AI 得出错误结论。

---

## 一、严重 bug 清单（按优先级）

### 🔴 B1 [P0 核心协议] sentinel 协议命令输出错位

**位置**：`src/vivado_mcp/vivado/tcl_utils.py` `wrap_command()`

**现象**：
- 第 13 步 `report: timing` 正确报 `No open design`
- 第 14 步 `get_timing_report` 无中生有返回 `PASS 时序满足`
- 第 15 步 `run_tcl: open_run` 开头多出一行 `ERROR: [Common 17-53] No open design`（上一条的残留）
- 第 21 步 `close_project` 返回 `invalid command name "this_command_does_not_exist"`（第 19 步的残留）

**根因**（`tcl_utils.py` 第 138-147 行）：

```python
f'set __rc [catch {{uplevel #0 $__cmd}} __out __opts]\n'
f'if {{$__rc == 0}} {{ puts $__out }}\n'
f'puts "<<<{sentinel}_RC=$__rc>>>"\n'        # ← sentinel 先打印
f'if {{$__rc != 0}} {{ puts "VMCP_ERR: $__out" }}\n'  # ← VMCP_ERR 后打印
```

session.execute 读到 sentinel 就 `break` 退出循环，**VMCP_ERR 行留在 stdout 缓冲区，被下一条 execute 读到**。

**修复**：把 VMCP_ERR 放到 sentinel 之前，或者 sentinel 行就包含错误消息摘要。

```python
# 正确顺序
f'if {{$__rc != 0}} {{ puts "VMCP_ERR: $__out" }}\n'
f'if {{$__rc == 0}} {{ puts $__out }}\n'
f'puts "<<<{sentinel}_RC=$__rc>>>"\n'
```

**影响面**：所有会失败的 Tcl 命令，后续所有 execute 结果都不可信。这是**一级灾难**。

---

### 🔴 B2 [P0 假阳性] `get_timing_report` 无数据时返回 PASS

**位置**：`src/vivado_mcp/tools/report_tools.py` 第 128-135 行 + `analysis/timing_parser.py`

**现象**（从测试报告第 14 步）：
```
report_timing_summary 底层失败 → result.output = "ERROR: No open design..."
↓
parse_timing_summary 从错误文本里解析不出 WNS/TNS 数字 → 默认值 0
↓
format_timing_report(默认值) → "状态: PASS (时序满足)"
```

**风险**：AI 拿到"时序 PASS"的结论，会认为设计通过，跳过所有时序检查流程，**实际上设计根本没综合进 design 内存**。

**修复**：
```python
result = await session.execute("report_timing_summary -return_string", timeout=120.0)
if result.is_error:
    return f"[ERROR] 获取时序报告失败: {result.output}"
timing_report = parse_timing_summary(result.output)
```

对 `get_io_report` 同样适用。

---

### 🔴 B3 [P0 实用性] `verify_io_placement` 不支持 `-dict` 写法

**位置**：`src/vivado_mcp/tcl_scripts.py` `EXTRACT_XDC_PACKAGE_PINS`

**现象**：我写的测试 XDC 使用标准的 `-dict` 语法（90% 的 FPGA 项目都这么写）：
```tcl
set_property -dict { PACKAGE_PIN W5 IOSTANDARD LVCMOS33 } [get_ports clk]
```
→ 验证工具返回"未找到 PACKAGE_PIN 约束"。

**根因**（tcl_scripts.py 第 77 行）：
```tcl
set __re {set_property\s+PACKAGE_PIN\s+(\S+)\s+\[get_ports\s+(.+?)\]}
```
只能匹配 `set_property PACKAGE_PIN W5 [get_ports clk]`，**匹配不到 `-dict` 语法**。

**修复**：新增一个 `-dict` 分支的正则，或放弃用 Tcl 正则、改用 Python 解析（xdc_parser.py 已经有，但它没被这里调用；这里绕了个远路用 Tcl 解析，性价比极低，建议直接让 Python 读文件）。

---

### 🟠 B4 [P1 体验] 综合完成后没自动 `open_run` 导致后续 report 全失败

**位置**：`src/vivado_mcp/tools/flow_tools.py` `_launch_and_wait`

**现象**：run_synthesis 成功返回，但立刻调用 `report(type="utilization")` / `report(type="timing")` 都报 `No open design`。用户必须手动 `run_tcl("open_run synth_1")` 才能继续。

**AI 踩坑链条**：
1. AI 调 run_synthesis → 成功
2. AI 调 report → "No open design"
3. AI 看不懂错误，可能重试或放弃

**修复**：`_launch_and_wait` 在综合完成后自动执行 `open_run {run_name}`（或在 report_tools 里加自动 open 保护）。

---

### 🟠 B5 [P1 可观测性] Vivado stderr 完全被丢弃

**位置**：`src/vivado_mcp/vivado/session.py` `_execute_impl`

**现象**：第 12 步 `report: utilization` 返回 `[ERROR] (rc=1)\n` 后面**什么都没有**（因为 Vivado 把错误信息写到了 stderr，而我们只读了 stdout）。

**违反全局指令 1.4**：错误处理铁律要求必须打印具体原因。

**修复**：
- `asyncio.create_subprocess_exec` 保留 `stderr=PIPE`（已有）
- 独立一个 async 任务持续读 stderr，出错时拼到 output

---

### 🟡 B6 [P2 待验证] `get_io_report` 解析不到任何端口

**现象**：打开 synth_1 后调用 `report_io -return_string`，解析出来 0 个端口。

**可能原因**（二选一）：
- B1 协议 bug 导致 `report_io` 的输出没完整拿到（大概率）
- io_parser 对 Vivado 2019.1 的 report_io 格式不兼容

**待 B1 修复后复测确认**。

---

### 🟢 B7 [P3 环境] 你机器上装着 SynthPilot

**现象**：每次 `start_session` 的启动横幅都有：
```
Sourcing tcl script 'D:/Xilinx/Vivado/2019.1/scripts/Vivado_init.tcl'
SynthPilot Server: Ready on port 9999
```
且 `run_synthesis` 内部 `launch_runs` 触发的子 Vivado 也执行 init.tcl，导致端口冲突（输出里有 `SynthPilot: Port 9999 is already in use`）。

**影响**：综合本身不受影响（子进程失败后继续跑 synth）；但每次 Vivado 启动都会卡顿。

**建议**：
- 如果你**不用** SynthPilot：运行 `synthpilot uninstall` 或手动编辑 `D:/Xilinx/Vivado/2019.1/scripts/Vivado_init.tcl` 删掉注入的部分
- 如果你**保留**作为对照：建议在 init.tcl 里加 `if {![info exists env(VIVADO_BATCH_MODE)]}` 之类的守卫，避免子 Vivado 启动它

---

## 二、21 个工具的"保留/重构/删除"打标

| # | 工具 | 标签 | 理由 |
|---|---|---|---|
| 1 | `start_session` | **保留** | 核心会话管理，不可替代 |
| 2 | `stop_session` | **保留** | 同上 |
| 3 | `list_sessions` | **保留** | 同上，实用且轻量 |
| 4 | `run_tcl` | **保留（重点加强）** | 你的哲学核心。大模型可以拼 Tcl，这是最强杠杆 |
| 5 | `vivado_help` | **可删** | 内置参考只有 11 条，大模型基础知识就够；Vivado 原生 help 需要 session，调 run_tcl 就行 |
| 6 | `create_project` | **可删** | 就一行 `create_project name dir -part xx`，让 AI 调 run_tcl 即可，省掉白名单验证的复杂性 |
| 7 | `open_project` | **可删** | 同上，一行 Tcl |
| 8 | `close_project` | **可删** | 同上 |
| 9 | `add_files` | **可删** | 同上 |
| 10 | `run_synthesis` | **重构** | **必须保留**（长任务需要异步 + 诊断），但需修 B4（自动 open_run）+ 可选加 `async_mode` 参数不阻塞 |
| 11 | `run_implementation` | **重构** | 同上 |
| 12 | `generate_bitstream` | **重构** | 前置安全检查是真需求，保留；修 B1 后回归测试 |
| 13 | `get_status` | **可删** | 就一行 `get_property STATUS [get_runs]`，让 run_tcl 做 |
| 14 | `program_device` | **保留** | 多步操作（open_hw_manager → connect → program）打包有价值 |
| 15 | `get_critical_warnings` | **保留（最核心差异化）** | 本地化 warning 分类 + 中文建议，这是你对标 SynthPilot 的**独家护城河** |
| 16 | `verify_io_placement` | **重构** | 理念非常好，但 B3 让它当前不可用；修完 -dict 解析后保留 |
| 17 | `inspect_ip_params` | **保留** | 列 CONFIG.* 参数含 GUI 隐藏项，有价值；可考虑让 run_tcl 直接代替，但这个包装省了 AI 拼 list_property+get_property 的步骤，值得 |
| 18 | `compare_xci` | **保留（明显差异化）** | 纯 Python 跨 XCI 对比，不需要 Vivado 会话，SynthPilot 都没有这个，**留** |
| 19 | `report` | **可删** | 11 种 report_type 就是一个 switch，让 AI 调 `run_tcl("report_utilization -return_string")` 即可 |
| 20 | `get_io_report` | **保留（修 B6 后）** | 结构化 JSON 输出比原始表格省 token，值得 |
| 21 | `get_timing_report` | **重构** | 理念好，但 B2 假阳性太危险，修 is_error 判断后保留 |

**建议瘦身后工具数**：21 → **10~11 个**。削掉的全是"一行 Tcl 就能做"的 facade 工具，符合你的哲学。

---

## 三、建议新增（如果将来要扩）

你原则上**不要**往工具数量军备竞赛走，但有两类有本地价值的可以考虑：

1. **`run_long_task(tcl, timeout_min)`** — 异步长任务包装器，返回 task_id，用 `get_task_status(task_id)` 轮询。解决综合/实现阻塞问题（目前阻塞整个 MCP session）。
2. **`read_log_tail(run_name, tail_lines=200)`** — 读取 runme.log 尾部，让 AI 能看综合失败时的原始日志（当 CW 为 0 但依然失败时有用）。

**不建议**做的（都是 run_tcl 能做的）：
- Block Design 工具（create_bd_cell/connect_bd_intf_net/...）
- IP 配置工具（create_ip/config_ip）
- 仿真 xsim 工具
- XSCT 嵌入式工具
- 硬件运行时 ILA/VIO/JTAG-AXI 工具

→ 全部交给 `run_tcl` + 给 AI 一份精心写的 Prompt / Resource。

---

## 四、文档层建议

1. 在 `fpga_workflow` prompt 里**明确告诉 AI**：`run_synthesis` 后要 `open_run synth_1` 才能跑 report（或等 B4 修完删掉这句）
2. 加一个 `tcl_cheatsheet` prompt，列高频 Tcl 命令片段，降低 AI 乱试的概率
3. README 里**明确声明**："本项目不做 run_tcl 能做的 facade 工具"——这是你的设计哲学，写出来让使用者不会期待错

---

## 五、下一步（请你选）

我把 bug 按优先级列好了，你告诉我哪些要立刻修、哪些先不管：

- [ ] **B1 sentinel 协议错位**（一切之根，强烈建议立刻修）
- [ ] **B2 `get_timing_report` 假阳性**（危险，建议立刻修）
- [ ] **B3 XDC `-dict` 语法解析**（影响 `verify_io_placement` 实用性）
- [ ] **B4 综合后自动 open_run**（改善 AI 体验）
- [ ] **B5 stderr 采集**（改善错误可读性）
- [ ] **B6 `get_io_report` 端口解析**（B1 修完后先复测再决定）
- [ ] **工具瘦身**：删除标记为"可删"的 8 个（create_project / open_project / close_project / add_files / vivado_help / get_status / report / ...）
- [ ] **文档更新**（workflow prompt、README）

---

## 附录：测试原始数据

- 完整 JSON 日志：`C:/Users/NJ/Desktop/vivado_mcp_test/test_report.json`
- stdout 日志：`C:/Users/NJ/Desktop/vivado_mcp_test/test_stdout.log`
- 项目产物：`C:/Users/NJ/Desktop/vivado_mcp_test/proj/`
- 测试驱动脚本：`C:/Users/NJ/Desktop/vivado_mcp_test/run_full_test.py`

你可以复看 JSON 里每一步的 1000 字符 preview 自行核实我的判断。
