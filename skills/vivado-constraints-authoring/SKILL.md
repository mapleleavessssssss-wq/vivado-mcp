---
name: vivado-constraints-authoring
description: 为 Vivado 工程建立或审查 XDC 时钟、I/O 延迟和时序例外，依据板级与接口参数验证对象和约束覆盖；不猜引脚、频率或外设时序，不用例外掩盖违例。
---

# Vivado XDC 编写与覆盖核对

**可用入口**：`get_project_info`、`xdc_lint`、`get_timing_report`、`get_cdc_report`、`run_tcl`、`safe_tcl`。

用户要求编写或修复时，在已有授权范围内编辑工程约束；只要求检查时交付审查结果。保留原有接口、器件和时序意图。没有板级资料时可以分析时钟与对象，不能猜 PACKAGE_PIN、IOSTANDARD 或外设延迟。

## 补齐建立约束的信息

核对工程 top、器件、已启用 XDC/fileset、处理顺序、IP 自带约束，以及接口参考时钟和同步关系。输入包括：primary clock 周期/波形、generated clock 源与变换、外设 clock-to-out/setup/hold、板级数据/时钟延迟差、引脚与电气标准。

缺参数时列出受影响路径与最小缺失输入，继续检查其他部分。不能把未知 `-min`/`-max` 写成 0，也不能把内部时钟频率当成外部接口的完整预算。

```python
get_project_info(session_id="design")
xdc_lint(session_id="design")
run_tcl(command="report_clocks -return_string", session_id="design")
```

`xdc_lint` 是静态预检；报告没有打开设计是阶段限制。对象、时钟关系与时序覆盖须在合适的已展开/综合设计上核对，不能只凭静态 lint 通过作结论。

## 编写与验证

1. primary clock 绑定真实输入对象，周期与波形来自已确认规格。generated clock 绑定实际 source/target，记录分频、倍频、相位；核对 IP 自动生成时钟，避免重复约束或把相关时钟定义成独立 primary clock。
2. 分别推导 I/O 的 `-min` 和 `-max`，明确参考边沿、外设参数及板级差分延迟的符号。输出 hold 可能对应负 output delay，不能取绝对值“修正”。DDR/双边沿接口分别覆盖边沿并核对 `-add_delay`，不套单边沿模板。
3. 写入前用 `get_ports` / `get_pins` / `get_clocks` 核对目标对象与数量；处理空集合、通配符误匹配和重复时钟。参数经 `safe_tcl` 传递；“命令没报错”不代表对象都正确。
4. 例外逐条绑定功能证据。异步 clock group 与 false path 会覆盖重叠路径的 max delay；multicycle 同时影响 setup/hold，须按真实协议预算核对二者，不能只放宽 setup。总线 skew 独立验证。
5. 修订写入工程实际 XDC，确认属于已启用的目标 fileset；只设置会话属性不构成可重建交付。运行受影响阶段后重读报告。

固定只读命令示例；版本选项以当前 Vivado `help` 与实际返回为准：

```python
run_tcl(command="check_timing -verbose", session_id="design")
run_tcl(command="report_clock_interaction -return_string", session_id="design")
run_tcl(command="report_exceptions -return_string", session_id="design")
get_timing_report(session_id="design", output_format="json")
```

## 覆盖与完成条件

检查 no-clock、multiple-clock、未约束内部端点、缺 I/O delay、无效或被覆盖的 exceptions；跨域传输用 `get_cdc_report` 核对结构和协议。bus skew 单独取得 `report_bus_skew`。版本不支持检查时保留原错误并标记未验证，不把失败查询当零问题。

交付约束的参数依据、精确对象、文件与同条件复测。区分“XDC 已写入”“覆盖已核对”和“实现满足时序”：完整签核还需当前 post-route setup/hold/pulse-width、DRC/methodology 及适用 CDC。不得降低频率、删约束或隐藏真实违例制造通过；两轮无改善时重查假设。
