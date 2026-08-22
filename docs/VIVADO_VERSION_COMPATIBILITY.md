# Vivado 版本兼容与会话边界

本文档记录 `vivado-mcp` 对常用 Vivado 版本的已知边界。它区分本机只读证据、
官方文档事实和仍需真实空会话验证的项目，不把“能启动”写成“全流程已验证”。

## 目标版本

| Vivado | 本机 launcher | Tcl runtime | 当前结论 |
|---|---|---|---|
| 2018.3 | `C:\Xilinx\Vivado\2018.3\bin\vivado.bat` | 8.5.14 | 空 GUI + 隔离空工程 synth/route PASS；实际 release 为 `2018.3_AR71898` |
| 2020.2 | `C:\Xilinx\Vivado\2020.2\bin\vivado.bat` | 8.5.14 | 空 GUI + 隔离空工程 synth/route PASS |
| 2024.2 | `C:\Xilinx\Vivado\2024.2\bin\vivado.bat` | 8.6.13 | 空 GUI + 隔离空工程 synth/route PASS |

Tcl family 先由本机各版本 `bin/loader.bat` 只读确认，patchlevel 再由空 GUI
`info patchlevel` 验证。`2022.1`、`2023.2` 也存在于本机，但不属于本轮重点版本；
MCP 会列出它们，不会擅自选择。

## 产品边界与软件工具链交接

`vivado-mcp` 的在线执行端是 Vivado Tcl 解释器。Vivado 工程、综合、实现、XSim、
Bitstream 和 Vivado Hardware Manager/ILA 属于本仓库；以下软件侧状态不属于：

- SDK/Vitis workspace、platform、domain、BSP、application；
- C/C++ build、ELF 管理和处理器调试；
- 独立 XSCT、XSDB、Vitis Unified Python、Vitis HLS、`vitis-run`、`v++` 会话。

三个目标年代的官方接口不同：2018.3 的软件侧是 Xilinx SDK/XSCT，XSCT 是独立的
Tcl 命令行；2020.2 Vitis Embedded 仍提供 XSCT Tcl 脚本；2024.2 Vitis Unified
通过 `vitis -i` / `vitis -s` 运行 Python API。它们不能交给 Vivado `run_tcl`。
推荐交接为：Vivado 显式产生 Bitstream/XSA，独立 Vitis MCP 显式消费 XSA并产生或
调试 ELF；两个 MCP 不共享 session manager，也不隐式选择 workspace 或 JTAG target。

官方参考：

- [UG1208 2018.3: XSCT Reference Guide](https://docs.amd.com/v/u/2018.3-English/ug1208-xsct-reference-guide)
- [UG1400 2020.2: Running Tcl Scripts](https://docs.amd.com/r/2020.2-English/ug1400-vitis-embedded/Running-Tcl-Scripts)
- [UG1702 2024.2: Launching Vitis Unified IDE](https://docs.amd.com/r/2024.2-English/ug1702-vitis-accelerated-reference/Launching-Vitis-Unified-IDE)
- [UG1702 2024.2: Python API Managing Vitis IDE Components](https://docs.amd.com/r/2024.2-English/ug1702-vitis-accelerated-reference/Python-API-Managing-Vitis-IDE-Components?contentId=jWP1yz7anTWcpbhBgE1zMw)

## 命令级 capability gate

版本号只能标识 release，不能证明某条命令、Tcl app、许可功能或当前设计阶段可用。
`get_vivado_capabilities` 对当前会话执行 Tcl 8.5/8.6 均支持的精确查询：

```tcl
llength [info commands <exact-command-name>]
```

它只检查命令是否注册，不执行 `<exact-command-name>`。AMD UG894 也使用
`foreach cmd [lsort [info commands *]]` 枚举 Vivado Tcl 命令。默认矩阵覆盖：

| 分组 | 代表命令 | 探测的工程问题 |
|---|---|---|
| identity | `version`、`current_project`、`current_design` | 当前会话能否提供身份和设计上下文 |
| project | `open_project`、`get_files`、`update_compile_order` | 工程 API 是否注册；不代表已批准打开或修改工程 |
| runs | `launch_runs`、`open_run`、`reset_runs`、`write_bitstream` | run/Bitstream API 是否注册；不执行任何 run |
| reports | `report_timing_summary`、`report_cdc`、`report_methodology`、QoR/analysis 命令 | 当前 release 的报告命令面 |
| simulation | `launch_simulation`、`current_sim`、wave 查询 | XSim 命令面；不启动仿真 |
| hardware_handoff | `write_hwdef`、`write_sysdef`、`write_hw_platform` | HDF/XSA 年代差异，不按版本名猜导出命令 |
| hardware_manager | `connect_hw_server`、`program_hw_devices`、ILA 命令 | 只查命令存在；绝不连接或修改硬件 |

工具返回规则：全部命令存在为 `gate=PASS`；明确缺失为 `FAIL`；输出不完整或无法
确认时为 `UNKNOWN`。只有 `PASS` 能进入后续独立审批；`PASS` 不是执行授权，也不
证明命令适用于当前工程阶段。`run_tcl` 是任意 Tcl 逃生口，无法可靠静态解析任意
多行脚本，因此 server instructions 要求 AI 在版本敏感调用前显式使用该 gate。

版本化命令参考：

- [UG835 2018.3: Vivado Tcl Command Reference](https://docs.amd.com/v/u/2018.3-English/ug835-vivado-tcl-commands)
- [UG835 2020.2: Vivado Tcl Command Reference](https://docs.amd.com/r/2020.2-English/ug835-vivado-tcl-commands)
- [UG835 2024.2: Vivado Tcl Command Reference](https://docs.amd.com/r/2024.2-English/ug835-vivado-tcl-commands)
- [UG894 2020.2: Finding Vivado Tcl Commands by Options](https://docs.amd.com/r/2020.2-English/ug894-vivado-tcl-scripting/Finding-Vivado-Tcl-Commands-by-Options)

### 当前证据矩阵

| 证据 | 2018.3 | 2020.2 | 2024.2 |
|---|---|---|---|
| 空 GUI bootstrap / identity / detach | PASS | PASS | PASS |
| `version -short` / `info patchlevel` / `current_project` | PASS | PASS | PASS |
| `get_vivado_capabilities` Python/Tcl 生成与解析单测 | PASS | PASS | PASS |
| 默认 capability 矩阵在对应真实 Vivado 中执行 | NOT_RUN | NOT_RUN | NOT_RUN |
| 隔离空工程 create/add/update/synth/route/report | PASS | PASS | PASS |
| 默认 run 的 timing/utilization 报告由当前 parser 读取 | PASS | PASS | PASS |
| `AUTO_INCREMENTAL_CHECKPOINT` 属性 set/read/restore | PASS | PASS | PASS |
| XSim、Bitstream、Hardware Manager | NOT_RUN | NOT_RUN | NOT_RUN |

这里第三行表示 capability probe 的共用 Tcl 8.5-compatible 语法已有自动化测试，
不等于默认 capability 矩阵已在三个厂商进程执行。project/run/report 行则来自
2026-08-22 的专用临时工程，不包含用户工程，不代表任意器件/策略已经认证。

## 多版本选择

- `list_vivado_installations` 离线列出全部发现的 launcher 和已知 compatibility
  profile，不启动 Vivado。
- 多版本并存且没有 `VIVADO_PATH` 时，MCP server 仍可初始化；启动会话必须显式传
  `vivado_version="2018.3"`、`"2020.2"`、`"2024.2"` 或绝对 `vivado_path`。
- 路径中版本与 `vivado_version` 不一致时拒绝启动。
- TCP 握手返回 `protocol`、`kind`、`pid`、`vivado` 和 `tcl`；请求 GUI 却命中
  batch/OOC endpoint，或命中错误版本时拒绝 attach。
- 旧版官方补丁可能让 `version -short` 返回 `2018.3_AR71898`。MCP 只接受
  `<expected>_AR*` 这种 Answer Record 后缀；不会把 `2018.3.1` 或其他 release
  当作 2018.3。

## 初始化脚本与 GUI 生命周期

AMD UG894 说明 `Vivado_init.tcl` 在 Vivado 启动时自动加载，安装目录脚本会影响
从该安装启动的全部用户和会话。因此它不是 GUI-only hook，project run 的独立
Vivado 进程也可能加载它。

普通交互流程不要求安装初始化脚本：

```text
start_session(mode="gui", vivado_version="2024.2", port=0)
```

该路径通过一次性 `-source` bootstrap 启动仅绑定 `127.0.0.1` 的 endpoint。
MCP 生命周期结束只 detach TCP，不向可见 GUI 发送 `exit`，也不 `taskkill`。
`stop_session` 是单独的显式破坏性工具。

`vivado-mcp install` 仅为“手工启动 GUI 后再 attach”的高级兼容方式。执行前应
明确目标版本并备份 `Vivado_init.tcl`；它会影响该安装的所有启动模式，不应为了
普通 `start_session(mode="gui")` 而安装。

本机曾在 2024.2 安装级 init 中注入 localhost:9999。隔离验证直接观察到父 Vivado、
综合子进程和实现子进程都会加载该脚本；子进程随后因端口占用退出 server 分支，虽不
阻断编译，却制造重复初始化和噪声。2026-08-22 已备份并运行官方 uninstall，确认
空白残留后恢复为安装前“文件不存在”。2018.3/2020.2 的用户自定义 init 内容未修改。

官方参考：

- [UG894 2020.2: Initializing Tcl Scripts](https://docs.amd.com/r/2020.2-English/ug894-vivado-tcl-scripting/Initializing-Tcl-Scripts)
- [UG894 2020.2: Loading and Running Tcl Scripts](https://docs.amd.com/r/2020.2-English/ug894-vivado-tcl-scripting/Loading-and-Running-Tcl-Scripts)
- [UG973 2024.2: Supported Operating Systems](https://docs.amd.com/r/2024.2-English/ug973-vivado-release-notes-install-license/Supported-Operating-Systems)

## 文件格式边界

- `.xpr` 和 `.xci` 离线工具只解析它们实际支持的 XML 结构；不按 Vivado 版本
  猜格式。解析失败会明确报错，不回退为“空工程/无参数”。
- `.ltx` 离线解析器支持已验证的 JSON 结构。遇到旧版 XML 会明确拒绝；因此不宣称
  2018.3 的离线 LTX 已兼容。
- `.wcfg` 的 zoom 修改只做最小文本替换；仍需对目标版本生成的真实文件逐版本验证。
- timing/utilization parser 已读取本机三版本隔离 run 的真实默认报告；其他器件族、
  report strategy 或未知/NA 格式仍必须返回 `DEGRADED` 或解析失败，不能制造全零 PASS。

## 操作系统风险

本机系统版本高于 AMD 2024.2 文档列出的 Windows 11 23H2 支持项；2018.3 和
2020.2 与当前 Windows 的代际差距更大。因此，旧版出现 GUI 秒退、子进程 spawn
失败或 Hardware Manager 驱动异常时，先看 MCP 返回的独立 startup log，再区分
MCP bootstrap、Vivado/OS 兼容性、许可和驱动问题。不得用关闭安全软件或降低系统
安全策略作为默认修复。

## 真实空会话验证结果

2026-08-20 已按版本串行完成以下验证，均未传 `.xpr`：

1. 三个版本的 bootstrap、`kind=gui` identity handshake 和空 project 查询 PASS。
2. 测试 listener 均只绑定 `127.0.0.1`；端口分别为一次性随机端口。
3. 三个版本 detach 后 fresh probe 均仍为 True，证明 MCP transport 结束不会关闭 GUI。
4. 每个测试只执行 `current_project`、`version -short`、`info patchlevel` 查询。
5. 每个测试实例都按握手返回的精确 PID 清理，随后端口 probe 为 False。

2024.2 验证期间还存在一个测试前已运行的 PID 12840，占用 localhost:9999；它与
测试 PID/随机端口不同，未被 attach、关闭或修改。这也验证了默认 `port=0` 不会
误接已有 GUI。

## 真实隔离编译验证结果

2026-08-22 在唯一临时目录中创建 32-bit counter 测试工程，固定 part
`xc7a35tcpg236-1`，只运行 synth 和 implementation 到 `route_design`：

| 证据 | 2018.3 | 2020.2 | 2024.2 |
|---|---:|---:|---:|
| 父会话 `general.maxThreads` 默认值 | 32 | 32 | 2 |
| 显式请求 / 实际 synth、DRC、route、timing update | 4 / 4 | 4 / 4 | 4 / 4 |
| synth / route wall time（秒，仅此小工程） | 25 / 35 | 30 / 40 | 30 / 40 |
| implementation `.rpt` 数量 | 11 | 11 | 11 |
| implementation `.dcp` 数量 | 3 | 4 | 4 |
| generic 改变后 synth/impl `NEEDS_REFRESH` | 1 / 1 | 1 / 1 | 1 / 1 |

三个版本默认 report strategy 都已经生成 timing summary、utilization、DRC、methodology、
power、route status 等报告，因此 MCP 默认优先读现有 `.rpt`，而不是再次 live 生成。
`launch_runs -jobs 4` 只代表并行 run 槽位；实际单 run 线程由
`general.maxThreads` 控制。2024.2 Windows 官方范围为 1..8，所以 MCP 对跨版本显式
参数采用共同安全范围 1..8，默认 0 表示不覆盖。

增量实现属性在三个版本都可读写并恢复，但这只证明接口兼容，不证明当前用户工程能
获得加速。官方建议增量编译依赖足够高的复用率；incremental synthesis 还要求设计
足够大。MCP 因此默认只规划，不自动对任意工程启用。

官方参考：

- [UG901 2024.2: Running Synthesis (`launch_runs -jobs`)](https://docs.amd.com/r/2024.2-English/ug901-vivado-synthesis/Running-Synthesis)
- [UG904 2024.2: Multithreading with the Vivado Tools](https://docs.amd.com/r/2024.2-English/ug904-vivado-implementation/Multithreading-with-the-Vivado-Tools)
- [UG906 2024.2: Setting Run Report Strategies](https://docs.amd.com/r/2024.2-English/ug906-vivado-design-analysis/Setting-Run-Report-Strategies)
- [UG949 2024.2: Incremental Synthesis](https://docs.amd.com/r/2024.2-English/ug949-vivado-design-methodology/Incremental-Synthesis)
- [UG949 2024.2: Compile Time Considerations](https://docs.amd.com/r/2024.2-English/ug949-vivado-design-methodology/Compile-Time-Considerations)

## 仍未验证

- 未运行任何现有工程；专用临时工程已在验证完成后删除。未运行仿真、Bitstream 或 Hardware Manager。
- 离线 `.ltx`/`.wcfg` 等格式仍需使用三个版本各自产生的真实 fixture 扩充验证。
- 因此 compatibility profile 仍为 `targeted`，不写成 certified。
