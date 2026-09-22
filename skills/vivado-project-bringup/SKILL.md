---
name: vivado-project-bringup
description: 接管或建立 Vivado FPGA 工程，核对源文件、约束、顶层和构建状态，通过 vivado-mcp 完成请求范围内的综合、实现和产物检查。
---

# Vivado 工程 Bring-up

**可用入口**：`list_sessions`、`start_session`、`get_project_info`、`get_critical_warnings`、`verilog_compile_check`、`xdc_lint`、`get_cdc_report`、`get_timing_report`、`get_utilization_report`、`run_synthesis`、`run_implementation`、`get_run_progress`、`check_bitstream_readiness`、`generate_bitstream`、`parse_bit_header`、`run_tcl`、`safe_tcl`。

使用已连接的 vivado-mcp。先区分用户要接管已有工程、创建工程还是只查看报告；分析请求不自动启动构建。复用已有授权完成常规修复，不因切换诊断工具重复确认。只有明确请求才编程硬件。

## 确定工程与基线

- 用 `list_sessions` 找到目标会话，必要时才 `start_session`；后续所有现场工具携带同一 `session_id`，不要并行修改同一工程。
- 用 `get_project_info` 核对工程、器件、top 和 run 状态；再经 `run_tcl` 查询 source/constraint/simulation fileset。不能从工程文件名猜测器件或板卡引脚。
- 缺少器件、接口约束或期望行为时，指出哪些步骤受阻，继续源文件、日志等可做的检查。保留已有源文件和用户改动。
- 用 `get_critical_warnings` 定位首个根因。综合尚未完成时，设计报告不可用是阶段限制，不是无问题。

## 建立或修复

创建工程只在用户要求时进行。路径、实例名和用户输入经 `safe_tcl` 参数转义；固定的查询命令可用 `run_tcl`。以下是 MCP 调用示例，需替换为已核对路径和会话：

```python
safe_tcl(template="open_project {0}", args=["/work/design/design.xpr"], session_id="design")
safe_tcl(template="add_files -fileset constrs_1 {0}", args=["/work/design/board.xdc"], session_id="design")
```

PL 工程先核对 source/top 与 XDC 的端口对应关系。BD 工程须核对时钟、复位、接口和地址，完成 `validate_bd_design`、生成 output products 与 wrapper，再进入综合。不要为单个缺失 IP 产物批量升级所有 IP。

## 构建、复测和交付

1. 综合前执行 `verilog_compile_check` 与 `xdc_lint`，核对实际语言、文件集和工具覆盖；这是静态预检，不能替代 Vivado elaboration 或行为验证。
2. 有可用 testbench 且请求包含功能验证时，执行 compile/elaborate 和有限仿真，记录 assertion/scoreboard、测试完成标志、时长和失败数。没有 testbench 或只请求构建时标记“功能未验证”，按已有授权继续构建，不把它算作仿真 PASS；真实失败不得被下游成功掩盖。
3. `run_synthesis` 后以 `get_run_progress` 确认实际结果，保留首个错误；有跨域传输时用 `get_cdc_report` 建立结构检查基线，并核对时钟覆盖。进程结束不等于成功。
4. 仅在请求范围需要时 `run_implementation`，检查实际 run 状态。变更后重跑受影响阶段，记录本轮输入和报告来源。
5. `get_timing_report(output_format="json")` 与 `get_utilization_report` 记录新基线；综合数据只能作早期估计。post-route 还需约束覆盖、route、DRC、methodology 和适用 CDC/bus-skew 证据。
6. 请求生成 bitstream 时先 `check_bitstream_readiness`，与本轮报告一致后才 `generate_bitstream`。需要时用 `parse_bit_header` 核对器件；文件存在不证明板上功能正常。

按阶段列出“执行/未执行、PASS/FAIL/未验证、证据来源、阻塞原因”。构建成功与功能验证分开报告，不能把缺测试的阶段补成 PASS。

每次修复记录“原问题 → 最小变更 → 相同条件复测”。达到请求结果即停止；连续两轮无改善则保留最佳结果并重新分析，不能循环重建或放宽约束制造通过。交付工程/器件、完成阶段、实际产物路径、关键报告与未验证项。未请求硬件下载时，到构建和产物验证为止。
