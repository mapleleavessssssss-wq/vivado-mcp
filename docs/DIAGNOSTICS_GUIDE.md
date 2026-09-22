# 用证据调试 Vivado 工程

本指南对应时序证据、结构化 CDC 报告、VCD 查询和五个可分发 Skills。`get_timing_report()` 默认仍返回中文文本；查询工具不会自动改 RTL、约束或写入基线文件。

## 时序：保存一次测量，再比较下一次

先确认当前工程、打开的设计和阶段。连接当前会话后调用：

```python
get_timing_report(session_id="default", output_format="json")
```

用客户端的文件工具将**完整 JSON 响应**保存为工程内一个新的文件，例如 `reports/baseline.json`。修改并重新运行相应阶段，打开重新生成的设计，再调用：

```python
get_timing_report(
    session_id="default",
    output_format="json",
    baseline_file="reports/baseline.json",
)
```

相对路径基于 MCP 服务的工作目录；实际工程优先传绝对路径。基线不会被自动覆盖。

返回内容包括：

| 字段 | 如何使用 |
|---|---|
| `provenance` | 来源类型、采集时间、报告内容摘要；用于区分这次读取了哪份报告，不证明设计源文件没有变化 |
| `stage` / `context` | 当前设计阶段、设计名称和器件；使用报告页头或实时查询的当前设计路由状态，不能由其他 run 已完成推断。`provenance.stage_source` 说明依据 |
| `metrics` | setup、hold、pulse-width 的最差裕量、总负裕量、失败和总端点数；未解析到的指标为 `null` |
| `verdict` | 违例、已观察检查满足、证据不完整或不可用；`signoff` 不由这份摘要单独授予 |
| `comparison` | 已知且匹配的设计、器件、阶段之间的可观察差值；缺少约束身份时不会宣称严格等价实验 |
| `diagnostics` / `paths` | 缺失、降级原因与路径证据 |

比较前后应保持目标时钟和约束意图一致。WNS 增加可能来自降低频率，不能单凭差值宣称 RTL 改进。综合后的时序估算不能与布线后的时序混作同一基线；空报告、NA、零端点或缺少检查维度也不是通过。

### 只拿到 `.rpt` 文件时

无需启动 Vivado：

```python
get_timing_report(
    report_file="reports/post_route_timing.rpt",
    output_format="json",
    baseline_file="reports/baseline.json",
)
```

`report_file` 应是 Vivado `report_timing_summary` 的完整文本输出，尽量保留 Design、Device 和 Design State 页头。Vivado 2019.1 的实测报告没有 Design State，离线解析会保持阶段未知；实时查询可用当前设计的路由计数补充判断。离线解析不会额外查询 Vivado；缺失的路径和约束信息必须另行补充。采集时间表示本次读取时间，不等于原报告的生成时间。

## CDC：从报告条目回到具体跨域结构

```python
get_cdc_report(session_id="design", max_details=100)
get_cdc_report(report_file="reports/cdc.rpt", max_details=100)
```

现场查询当前打开设计的 `report_cdc -details -return_string`，不自动打开 run 或启动综合。离线输入为原始 UTF-8 报告，最多 16 MiB；`max_details` 为 0..500。报告错误也返回 JSON。

读取 `parse_status`、`provenance` 后检查 `summary`、`clock_pairs`、`details`、截断与 `diagnostics`。`reported_checks` 是报告检查条目数；摘要与明细不重复相加，合并的总线条目也不代表每位端点。豁免数量与未豁免检查分开记录，不能将 waiver 当作修复。

`verdict.signoff` 始终为 false。缺少时钟定义时 Vivado 2019.1 也可能输出 “All paths are Safely Timed.”；默认报告还可能隐藏已豁免的路径。因此空报告、零严重项或命令成功都不单独构成 CDC clean。需要完整时钟/对象覆盖、同步结构、协议、例外与适用 post-route/bus-skew 证据。

## 波形：先发现信号，再查一个问题

`query_waveform` 只读本地 **VCD**，不需要 GUI 或 Vivado 会话。它不直接读取 WDB/FST，也不是完整 SystemVerilog assertion 引擎。

```python
# 发现信号和层次，拿到准确路径、位宽与 timescale
query_waveform(file_path="sim/trace.vcd")

# 指定时间窗，查看初值和后续变化
query_waveform(
    file_path="sim/trace.vcd",
    signals=["tb.valid", "tb.ready", "tb.data"],
    start_time=0,
    end_time=100000,
    max_events=40,
)

# 找 data 出现 X/Z 的位置
query_waveform(
    file_path="sim/trace.vcd",
    signals=["tb.data"],
    condition={"op": "unknown", "signal": "tb.data"},
    max_events=20,
)

# 查 valid 与 ready 同时为 1 的采样状态
query_waveform(
    file_path="sim/trace.vcd",
    signals=["tb.valid", "tb.ready"],
    condition={"op": "all_equals", "values": {"tb.valid": "1", "tb.ready": "1"}},
    max_events=20,
)
```

条件中的信号必须出现在 `signals` 中。还支持 `{"op":"equals","signal":"tb.data","value":"00000001"}` 和 `{"op":"change","signal":"tb.data"}`。不执行自由文本表达式。

**时间参数单位是 VCD tick。** 比如 timescale 为 1 ps 时，100000 ticks = 100 ns；先读 timescale 再选窗口。信号值为保留 X/Z 的二进制字符串，不能把未知值当成 0。

`initial_values` 是窗口起点完成该时刻更新后的值，`events` 是之后的合并变化；同一时刻的多个更新按最终值呈现，不能用于证明不存在 delta-cycle 变化或毛刺。条件匹配是采样状态，不自动代表上升沿握手或每周期事务计数。输出中的 `scan_truncated`、`result_truncated` 表示覆盖限制，必须一起阅读。没有匹配不代表测试通过，`simulation_verdict` 保持 `not_evaluated`。

当前上限：32 MiB VCD、32 个选中信号、每次 1–1000 条结果、最多 512 KiB 输出；额外设有扫描 token 与声明数量限制。查询支持数字逻辑信号，`event` / `real` / `string` 类型可以发现但不能按位值查询。达到扫描限制时，尚未读完的时间戳不会被当成完整样本。

### 从 XSim 采集 VCD

先在目标会话启动仿真，并确认真实 scope。以下为 Tcl 配方，**在所需的运行区间开始前**执行，文件路径应使用新文件：

```tcl
open_vcd {D:/my_project/reports/trace.vcd}
log_vcd /tb/dut/*
run 1 us
close_vcd
```

路径与 scope 换成实际工程值；通过 MCP 操作时用 `safe_tcl` 参数传值。已运行过的区间不会因此补齐，需要重放时先确认是否可以重启当前仿真。导出后核对文件存在且包含目标信号，再用 `query_waveform` 读取。此流程依据 [AMD UG900 的 VCD 采集说明](https://docs.amd.com/api/khub/documents/SqVYqIHFQHbVQsut~a3JCQ/content)。

## 可分发的 Skills

仓库的 `skills/` 目录包含：

| Skill | 适用请求 |
|---|---|
| [vivado-project-bringup](../skills/vivado-project-bringup/SKILL.md) | 接手工程、确认源文件和约束、定位首个阻塞项 |
| [vivado-timing-closure](../skills/vivado-timing-closure/SKILL.md) | 保存基线、分类时序问题、最小修复、同阶段复测 |
| [vivado-waveform-debug](../skills/vivado-waveform-debug/SKILL.md) | 从失败场景确定信号和时间窗，查询 VCD，结合 testbench 证据判断 |
| [vivado-cdc-audit](../skills/vivado-cdc-audit/SKILL.md) | 结构化 CDC 报告、时钟对覆盖、同步协议、例外与复测 |
| [vivado-constraints-authoring](../skills/vivado-constraints-authoring/SKILL.md) | 根据板级事实建立时钟、I/O min/max 与例外，并检查约束覆盖 |

这些文件是唯一正文来源，随 Python 包分发。用 `vivado-mcp skills list --json` 查看名称、描述和对应 Prompt；`vivado-mcp skills export ./fpga-skills` 导出全部，或追加 `--skill vivado-cdc-audit` 选择某项。导出相同内容会跳过，不同内容或冲突目录会报错并保留用户文件，不自动改客户端配置。支持 Skills 的客户端可导入对应目录，实际工具前缀以客户端注册为准。

原八个 MCP Prompt 名称和顺序保留，追加 `project_bringup`、`waveform_debug`、`constraints_authoring`。其中五项直接读取 Skill 正文，因此两种入口不会各自漂移；无需同时加载所有文本。

## 波形显示与图片

需要在 XSim 看趋势时，先用 `set_wave_zoom` 设置时间窗，再用 `set_wave_analog` 设置显示样式和幅度。重载 WCFG 可能重置 analog 样式；这些工具会处理 STYLE_ANALOG 值、信号寻址和空对象检查。

本项目没有封装桌面截图工具。需要图片时可以使用可用的截图功能；需要分析信号值时优先使用 VCD 数据。是否能自动截图取决于具体桌面工具、权限和会话环境，不能表述为系统一律禁止。

## 设计参考

本轮进一步借鉴 oh-my-fpga 的工作流分发方式、全流程阶段门禁和约束审查，针对本项目接口原创重写。约束语义依据 [AMD UG903 例外优先级](https://docs.amd.com/r/2024.2-English/ug903-vivado-using-constraints/Exceptions-Priority)：clock groups 可覆盖重叠路径的 max delay，bus skew 独立验证。CDC 采集接口依据 [AMD UG835 report_cdc](https://docs.amd.com/r/2023.1-English/ug835-vivado-tcl-commands/report_cdc)，并以 Vivado 2019.1 实测文本验证。

本轮采用原创实现，参考了 [oh-my-fpga](https://github.com/LNC0831/oh-my-fpga) 的测量与复测流程、[waveform-mcp](https://github.com/jiegec/waveform-mcp) 的条件波形查询、[fpgaZeroMCP](https://github.com/lcapossio/fpgaZeroMCP) 对运行成功与验证结论的区分，以及 [Sigasi](https://www.sigasi.com/news/sigasi_visual_hdl_2026.2/) 向 AI 提供工程分析证据的方式。这里只说明本项目已实现的范围，不意味着具备这些项目的全部能力。
