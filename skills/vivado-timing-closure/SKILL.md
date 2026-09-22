---
name: vivado-timing-closure
description: 使用 vivado-mcp 分析 Vivado setup、hold 和脉宽时序，比较保存的基线并按证据迭代修复；支持离线报告分析，不将摘要指标视为完整签核。
---

# Vivado 时序收敛

**可用入口**：`get_project_info`、`get_timing_report`、`get_utilization_report`、`get_critical_warnings`、`get_cdc_report`、`run_tcl`、`safe_tcl`。

只分析时不启动构建；用户要求优化时继续完成已授权的 RTL/XDC/策略修复。保持接口、目标频率和功能语义；改变这些条件需要对应授权。此流程不包含硬件编程。

## 记录可比较的基线

现场用 `get_project_info` 确认工程、器件与 run；报告必须对应当前 source/XDC。离线输入不需要 Vivado 会话，但结论只覆盖所给文件。

```python
get_timing_report(session_id="design", output_format="json")
get_timing_report(output_format="json", report_file="/work/reports/before.rpt")
```

记录 `provenance`、`stage`、`context`、`parse_status`、`metrics` 和 `verdict`。setup、hold、pulse_width 的缺失值是 `null`，不能当作零。`met_observed_checks` 只说明已观察指标满足；`verdict.signoff` 为 false，不等于完整签核。

需要比较时，用宿主文件工具把返回的完整 JSON 正文原样保存为新基线文件，包含 schema、来源和上下文；不要只存 metrics，也不要把原始 Tcl 文本传给 `baseline_file`。MCP 不会自动写入报告。

## 分类后修复

- 先核对时钟与约束覆盖，再解释最差路径。有现场会话时用 `run_tcl` 获取 `report_clock_interaction -return_string`、`report_methodology -return_string`；离线缺少旁证时列为未验证。CDC 嫌疑需要结构与协议证据。
- 用返回路径与可用资源/告警报告区分逻辑深度、扇出/布线、拥塞、I/O 约束和时钟问题；现场可调用 `get_utilization_report`、`get_critical_warnings`。布局策略不能修复逻辑协议错误。
- 一次只验证一个根因假设。保留 baseline 和最佳候选，记录具体 RTL/XDC/策略变更。用户输入的路径/对象通过 `safe_tcl`，不要拼入原始 Tcl。
- `set_false_path` 和 multicycle 不是性能优化手段；它们与 waiver 只能依据真实功能关系，不能用于消除真实违例。没有协议证据时报告不确定性，继续不依赖该例外的分析。
- CDC 嫌疑通过 `get_cdc_report` 与实际同步结构核对。`set_clock_groups` 会覆盖重叠路径的 `set_max_delay -datapath_only`；用 `report_exceptions` 核实生效范围。`set_bus_skew` 需用独立 `report_bus_skew` 检查，不能从 WNS 推断。

## 同条件复测

重跑受影响阶段后读取新报告，或对新保存的原始报告进行离线比较：

```python
get_timing_report(session_id="design", output_format="json", baseline_file="/work/reports/baseline.json")
get_timing_report(output_format="json", report_file="/work/reports/after.rpt", baseline_file="/work/reports/baseline.json")
```

先读 `comparison.status` 与 `reasons`，再解释 `deltas`。不同/未知设计或阶段不能比较；即使 design/device/stage 相同，约束身份未知时差值仅为观察，不证明优化效果。该接口的比较 `authoritative` 为 false，须用独立的输入、约束和路径覆盖记录说明可比性。

收敛验收需同一当前 post-route 设计的 setup/hold/pulse-width、完整约束覆盖、route、DRC/methodology 和适用 CDC 检查。仅 WNS 改善或摘要非负不够。达到目标后交付证据；连续两轮无实质改善时保留最佳结果并重新归因，受总预算限制停止，不把停滞称成功。报告数值变化、可比性、变更、剩余违例和未验证检查。
