---
name: vivado-cdc-audit
description: 使用 vivado-mcp 结构化读取 Vivado CDC 报告，核对跨时钟域的同步结构、协议和约束；支持离线报告审查，不把空报告或零严重项当作完整签核。
---

# Vivado CDC 审查

**可用入口**：`get_cdc_report`、`get_project_info`、`get_timing_report`、`get_critical_warnings`、`run_tcl`、`safe_tcl`。

已有报告时先离线分析，不为了读报告启动工程或构建。现场报告需要在选定会话中打开综合或实现后的设计。CDC 分析关注同步结构；STA 通过不能证明跨域传输正确。

## 获取当前证据

```python
get_cdc_report(report_file="/work/reports/cdc.rpt", max_details=100)
get_cdc_report(session_id="design", max_details=100)
```

文件应是完整的 `report_cdc -details` 文本。检查 `parse_status`、`provenance`、`summary`、`clock_pairs` 和 `details_truncated`。摘要与逐路径明细不能相加；`reported_checks` 是报告检查条目数，合并的总线条目不能当作每一位端点。读取时间不等于生成时间；工具不自动保存报告或证明其对应当前 RTL/XDC。

现场用 `get_project_info` 核对设计和阶段，补充时钟与例外：

```python
run_tcl(command="report_clocks -return_string", session_id="design")
run_tcl(command="report_clock_interaction -return_string", session_id="design")
run_tcl(command="report_exceptions -return_string", session_id="design")
```

将预期的 primary/generated clocks 与实际报告逐一对应。空报告、无跨域路径、缺时钟或只出现一种时钟都不能单独证明 CDC 干净；无时钟时工具也可能打印 “All paths are Safely Timed.”。相关时钟仍需核对具体路径，不能只凭“同源”跳过。

## 根据结构与协议定位

按 source clock → destination clock → 端点列出严重项，记录报告规则 ID、宽度、协议意图和同步结构。缺明细时补完整报告或查询目标设计，不能从汇总数量推测线路。

- 单 bit 电平检查同步级数及 ASYNC_REG；窄脉冲需确认目的时钟能采到，必要时用握手或脉冲传输结构。
- 总线、FIFO 指针或计数器核对 Gray 编码、稳定窗口或握手。每位各自双触发器不能保证总线一致性。
- 异步复位核对各目的域的同步释放；多个独立同步信号重汇合时检查是否在不同周期到达。
- IP 内部 crossing 结合具体参数与自带约束核对，不凭 IP 名称直接忽略告警。

用户要求修复时继续完成范围内有证据支持的 RTL/XDC 修改；只要求审查时交付发现与方案。一次处理一种根因，保持功能、接口和频率。同步器不足先修结构，约束不会增加同步硬件。

## 核对约束实际生效

不用 false path 或 waiver 隐藏真实 crossing。例外需提供协议假设、精确端点和影响范围，沿用已有授权；假设不清楚时继续独立调查。默认 CDC 报告可能隐藏已 waiver 项，须另行核对 waiver 与时钟覆盖。

`set_clock_groups` 优先级高于 `set_max_delay -datapath_only`，不能重叠使用后声称最大延迟仍有效；尤其检查 XPM/IP 自带约束是否被广泛例外覆盖。`set_bus_skew` 是独立的路径间约束，需单独取得 `report_bus_skew`，不能凭 timing summary 或 CDC 数量声称满足。

## 复测与交付

修改后重跑受影响阶段，重新读取同一组 CDC、clock interaction、exceptions 和 timing 报告；签核需补 post-route、DRC、methodology、约束覆盖及适用 bus-skew 证据。严重项减少时确认原对象仍被覆盖，没有新增隐藏路径。

交付来源与阶段、时钟对/端点、结构与协议证据、变更、复测差异、剩余严重项和未验证内容。`verdict.signoff=false` 表示本工具不授予完整签核；零严重项也不等于板上可靠。达到请求目标即停止；连续两轮无改善则保留当前证据并重新归因。
