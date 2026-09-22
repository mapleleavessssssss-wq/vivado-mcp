---
name: vivado-waveform-debug
description: 使用 vivado-mcp 离线查询 VCD 波形，定位复位、X/Z、信号变化和握手问题；需要新证据时从已选 XSim 仿真导出有限窗口，不把波形查询当测试判定。
---

# Vivado 波形诊断

**可用入口**：`query_waveform`、`get_project_info`、`run_tcl`、`safe_tcl`。

先提出可证伪的问题，例如“复位释放后 valid 是否出现未知值”。已有 VCD 时直接离线查询，不启动 Vivado。不支持直接读 WDB/FST，也不执行任意表达式或完整 SVA；此流程不编程硬件。

## 发现信号与时间单位

```python
query_waveform(file_path="/work/sim/trace.vcd")
```

从返回清单选择精确层级名，不能猜测 `top` 或把向量的位范围拼成未列出的路径。`start_time`/`end_time` 是整数 VCD ticks；先读 `timescale` 再换算物理时间，例如 10 ps/tick 的 100 ticks 是 1 ns。

## 缩小窗口并查询

```python
query_waveform(file_path="/work/sim/trace.vcd", signals=["top.valid", "top.ready"], start_time=0, end_time=1000, max_events=100, condition={"op": "all_equals", "values": {"top.valid": "1", "top.ready": "1"}})
query_waveform(file_path="/work/sim/trace.vcd", signals=["top.data"], start_time=100, end_time=200, max_events=50, condition={"op": "unknown", "signal": "top.data"})
```

条件还有 `equals`（signal/value）和 `change`（signal）；value 为二进制字符串，可含 x/z。条件引用的信号必须包含在 `signals` 中。`all_equals` 只表示采样值同时满足，不能单独证明某时钟沿发生有效握手。

`initial_values` 是窗口起点所有同刻更新后的状态；`events` 记录之后的变化。同刻只保留最终值，不能证明 delta-cycle 或毛刺不存在。equals/unknown/all_equals 也检查起点。依次检查 `initial_values_complete`、`last_complete_time`、`scan_truncated` 和 `result_truncated`，再解释 `matches`；截断、未记录信号或空匹配都不能证明问题不存在。`simulation_verdict` 为 `not_evaluated`，测试 PASS 仍需 assertion/scoreboard、完成条件和日志。

## 需要新 VCD 时

只对已选会话和已展开的 XSim 仿真操作。核对 testbench、激励、seed、目标信号与有限运行时间；已有仿真运行过目标窗口时，应保留现有结果，再按原激励重跑，不能补造过去事件。

依据 [AMD UG900 的 VCD 命令说明](https://docs.amd.com/r/2023.2-%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87/ug900-vivado-logic-simulation/%E4%BD%BF%E7%94%A8%E5%80%BC%E6%9B%B4%E6%94%B9%E8%BD%AC%E5%82%A8%E5%8A%9F%E8%83%BD)，按以下顺序导出；路径、scope 和时长须替换为本次已核对值，文件使用新的任务路径：

```python
safe_tcl(template="open_vcd {0}", args=["/work/sim/trace.vcd"], session_id="design")
safe_tcl(template="log_vcd {0}", args=["/tb/dut/*"], session_id="design")
safe_tcl(template="run {0}", args=["100ns"], session_id="design")
run_tcl(command="close_vcd", session_id="design")
```

每步检查实际返回；运行失败后也应关闭本次打开的 VCD 并保留日志。不要向共享会话发送 `quit`，也不要杀掉机器上全部模拟器。不同 Vivado 版本或会话若不接受仿真命令，记录实际错误并在该工程的 XSim Tcl 上下文使用同一导出序列，不声称 VCD 已生成。

## 修复与复测

区分 DUT、testbench、模型、时钟/复位和导出覆盖问题；一次修一个原因，以相同激励/seed/时长重新仿真、导出并查询相同窗口。两轮无改善则重新分析或报告缺少的可观测性。交付文件来源、timescale、信号/窗口、关键事件、截断状态、修复前后差异及独立测试结论；不能靠删除 assertion 或缩短测试制造通过。
